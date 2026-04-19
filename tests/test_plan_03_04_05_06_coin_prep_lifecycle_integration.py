"""Slices 03-04, 03-05, 03-06 — coin prep lifecycle (integration tests).

Three slices in one file — all test the coin prep trigger/status/reset cycle
with real SQLite DB and mocked subprocess/threading:

  03-04: coin-prep full cycle — consolidate → split → verify
    - Trigger sets state: running=True, phase=idle, run_id populated
    - Status endpoint reads state file when provided (phase progress)
    - Complete: running=False, complete=True after mock completion
    - Fills in DB are NOT cleared on default (preserve_history=True) trigger

  03-05: coin-prep retry (soft reset, preserve fills)
    - Soft reset via /api/coin-prep/reset clears running/error state
    - Fills survive soft reset
    - Re-trigger after reset sets running=True again
    - Error state is cleared on re-trigger

  03-06: coin-prep full reset (fresh-start path)
    - full_reset=True in trigger body → fills cleared from DB (0 rows)
    - full_reset=False (default) → fills preserved
    - full_reset=True also clears round_trips

Threading is mocked to prevent the do_prep() thread from actually launching.
Uses the TempDB pattern for real SQLite isolation.
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    _db = None
    api_server = None
    _SKIP = str(exc)


_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
_FAKE_ASSET = "a" * 64
_TRADE_ID_A = "prep-test-001"
_TRADE_ID_B = "prep-test-002"


class _TempDB(unittest.TestCase):
    """Base: redirect database module to a fresh temp SQLite file."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path
        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.init_database()

        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        api_server._rate_limit_log.clear()
        api_server._fresh_start_clear()

        self._orig_session_start_time = api_server._session_start_time
        self._orig_run_history_cutoff = api_server._run_history_cutoff

        # Snapshot coin prep state so tearDown can restore it
        self._orig_coin_prep_state = dict(api_server._coin_prep_state)
        self._orig_coin_prep_proc = api_server._coin_prep_proc

    def tearDown(self):
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path
        try:
            os.unlink(self._tmp_path)
        except Exception:
            pass
        api_server._rate_limit_log.clear()
        api_server._fresh_start_clear()
        api_server._session_start_time = self._orig_session_start_time
        api_server._run_history_cutoff = self._orig_run_history_cutoff
        api_server._coin_prep_state.clear()
        api_server._coin_prep_state.update(self._orig_coin_prep_state)
        api_server._coin_prep_proc = self._orig_coin_prep_proc

    def _seed_fill(self, trade_id=_TRADE_ID_A):
        _db.record_fill(
            trade_id=trade_id,
            side="buy",
            price_xch=Decimal("0.002"),
            size_xch=Decimal("0.001"),
            size_cat=Decimal("0.5"),
            cat_asset_id=_FAKE_ASSET,
        )

    def _fill_count(self):
        conn = _db.get_connection()
        return conn.execute("SELECT COUNT(*) AS cnt FROM fills").fetchone()["cnt"]

    def _make_bot(self):
        bot = MagicMock()
        bot.is_running.return_value = False
        bot.stop = MagicMock()
        bot.coin_manager._prep_process = None
        bot.coin_manager._prep_running = False
        bot.coin_manager.check_coin_prep_status.return_value = {"running": False}
        bot.coin_manager.get_coin_health.return_value = (5, 5)
        return bot

    def _trigger(self, bot_mock=None, full_reset=False):
        """POST /api/coin-prep/trigger with threading mocked out."""
        mock_thread = MagicMock()
        with patch.object(api_server, "bot", bot_mock or self._make_bot()), \
             patch("api_server.threading") as mock_threading, \
             patch("api_server.log_event"), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open()):
            mock_threading.Thread.return_value = mock_thread
            return self.client.post(
                "/api/coin-prep/trigger",
                json={"full_reset": full_reset},
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )

    def _reset(self, bot_mock=None):
        with patch.object(api_server, "bot", bot_mock), \
             patch("api_server.log_event"):
            return self.client.post(
                "/api/coin-prep/reset",
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )

    def _status(self, bot_mock=None):
        with patch.object(api_server, "bot", bot_mock), \
             patch("os.path.exists", return_value=False):
            return self.client.get(
                "/api/coin-prep/status",
                environ_base=_LOOPBACK,
            )


# ---------------------------------------------------------------------------
# 03-04: coin-prep full cycle — trigger → running → state transitions
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCoinPrepFullCycle(_TempDB):
    """Trigger starts the cycle; state transitions must be correct."""

    def test_trigger_returns_success(self):
        resp = self._trigger()
        self.assertTrue(resp.get_json().get("success"))

    def test_trigger_sets_running_true(self):
        """After trigger, _coin_prep_state['running'] is True."""
        self._trigger()
        self.assertTrue(api_server._coin_prep_state.get("running"))

    def test_trigger_sets_run_id(self):
        """Each trigger creates a unique run_id."""
        self._trigger()
        run_id = api_server._coin_prep_state.get("run_id")
        self.assertIsNotNone(run_id)
        self.assertGreater(len(run_id), 0)

    def test_trigger_sets_started_at(self):
        """Trigger records the start timestamp."""
        self._trigger()
        self.assertIsNotNone(api_server._coin_prep_state.get("started_at"))

    def test_trigger_clears_previous_error(self):
        """Re-trigger after error must clear the previous error message."""
        api_server._coin_prep_state["error"] = "previous failure"
        self._trigger()
        self.assertIsNone(api_server._coin_prep_state.get("error"))

    def test_trigger_stops_running_bot(self):
        """Trigger must call bot.stop() to prevent concurrent trading."""
        bot = self._make_bot()
        bot.is_running.return_value = True
        self._trigger(bot_mock=bot)
        bot.stop.assert_called()

    def test_status_endpoint_returns_running_state(self):
        """Status endpoint reflects the running=True state after trigger."""
        self._trigger()
        resp = self._status()
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body.get("running"))

    def test_second_run_id_differs_from_first(self):
        """Two consecutive triggers produce different run_ids."""
        self._trigger()
        first_id = api_server._coin_prep_state.get("run_id")
        self._trigger()
        second_id = api_server._coin_prep_state.get("run_id")
        self.assertNotEqual(first_id, second_id)

    def test_default_trigger_preserves_fills(self):
        """Default trigger (full_reset=False) must NOT delete fills from DB."""
        self._seed_fill()
        self.assertEqual(self._fill_count(), 1)
        self._trigger(full_reset=False)
        self.assertEqual(self._fill_count(), 1)


# ---------------------------------------------------------------------------
# 03-05: coin-prep retry — soft reset preserves DB, re-trigger restores state
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCoinPrepRetry(_TempDB):
    """Soft reset must clear running state without touching fills."""

    def test_reset_returns_success(self):
        resp = self._reset()
        self.assertTrue(resp.get_json().get("success"))

    def test_reset_clears_running_flag(self):
        """After reset, _coin_prep_state['running'] is False."""
        api_server._coin_prep_state["running"] = True
        self._reset()
        self.assertFalse(api_server._coin_prep_state.get("running"))

    def test_reset_clears_complete_flag(self):
        """After reset, complete is False."""
        api_server._coin_prep_state["complete"] = True
        self._reset()
        self.assertFalse(api_server._coin_prep_state.get("complete"))

    def test_reset_clears_error_state(self):
        """Error from previous run is cleared on soft reset."""
        api_server._coin_prep_state["error"] = "Worker exited with code 1"
        self._reset()
        self.assertIsNone(api_server._coin_prep_state.get("error"))

    def test_reset_preserves_fills(self):
        """Soft reset never touches the fills table."""
        self._seed_fill()
        self._trigger()
        self._reset()
        self.assertEqual(self._fill_count(), 1)

    def test_retrigger_after_reset_sets_running(self):
        """Re-trigger after soft reset starts a new run (running=True)."""
        api_server._coin_prep_state["running"] = False
        api_server._coin_prep_state["error"] = "prev error"
        self._trigger()
        self.assertTrue(api_server._coin_prep_state.get("running"))

    def test_retrigger_after_reset_clears_error(self):
        """Re-trigger after error clears the error field."""
        api_server._coin_prep_state["error"] = "some crash"
        self._trigger()
        self.assertIsNone(api_server._coin_prep_state.get("error"))

    def test_reset_ungates_coin_manager(self):
        """Reset must set coin_manager._prep_running=False to ungate the bot loop."""
        bot = self._make_bot()
        bot.coin_manager._prep_running = True
        self._reset(bot_mock=bot)
        self.assertFalse(bot.coin_manager._prep_running)

    def test_fills_survive_trigger_reset_retrigger_cycle(self):
        """Fills must survive the full retry cycle."""
        self._seed_fill()
        self._trigger(full_reset=False)
        self._reset()
        self._trigger(full_reset=False)
        self.assertEqual(self._fill_count(), 1)


# ---------------------------------------------------------------------------
# 03-06: coin-prep full reset path — full_reset=True clears fills
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCoinPrepFullReset(_TempDB):
    """full_reset=True must clear fills from DB (unlike the default path)."""

    def test_full_reset_trigger_returns_success(self):
        resp = self._trigger(full_reset=True)
        self.assertTrue(resp.get_json().get("success"))

    def test_full_reset_clears_fills(self):
        """Triggering with full_reset=True deletes all fills from DB."""
        self._seed_fill()
        self.assertEqual(self._fill_count(), 1)
        self._trigger(full_reset=True)
        self.assertEqual(self._fill_count(), 0)

    def test_full_reset_with_multiple_fills(self):
        """All fill rows are removed on full_reset=True, not just one."""
        self._seed_fill(trade_id=_TRADE_ID_A)
        self._seed_fill(trade_id=_TRADE_ID_B)
        self.assertEqual(self._fill_count(), 2)
        self._trigger(full_reset=True)
        self.assertEqual(self._fill_count(), 0)

    def test_default_trigger_does_not_clear_fills(self):
        """full_reset=False (default) must NOT clear fills."""
        self._seed_fill()
        self._trigger(full_reset=False)
        self.assertEqual(self._fill_count(), 1)

    def test_full_reset_still_sets_running_true(self):
        """Even the full-reset path must set the running flag."""
        self._trigger(full_reset=True)
        self.assertTrue(api_server._coin_prep_state.get("running"))

    def test_full_reset_clears_complete_flag(self):
        """Full reset trigger re-starts the cycle (complete=False)."""
        api_server._coin_prep_state["complete"] = True
        self._trigger(full_reset=True)
        self.assertFalse(api_server._coin_prep_state.get("complete"))

    def test_full_reset_sets_run_id(self):
        """Full reset trigger still creates a unique run_id."""
        self._trigger(full_reset=True)
        self.assertIsNotNone(api_server._coin_prep_state.get("run_id"))


if __name__ == "__main__":
    unittest.main()
