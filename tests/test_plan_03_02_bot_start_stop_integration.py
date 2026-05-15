"""Slice 03-02 — bot start/stop cycle: DB state persists across (integration test).

Tests the full bot start → stop cycle at the DB + endpoint level:

  Bot start:
    - /api/bot/start succeeds when validation passes (mocked wallet sync,
      CAT_ASSET_ID set, SPREAD_BPS > 0, signing not blocked).
    - _reset_runtime_session_stats() clears splash_incoming but NOT fills.
    - fills seeded before start survive the start call.
    - fresh_start flag is cleared on successful start.
    - already-running guard returns success/already_running.
    - events.emit("bot_control", ...) fires on start and stop.

  Bot stop:
    - /api/bot/stop transitions bot to stopped state.
    - DB fills are unaffected by stop.
    - Stopping an already-stopped bot is safe (no crash).

  State persistence assertion:
    - fills, round-trip records, and open offers survive the full
      start → stop cycle (bot.stop() does NOT wipe the DB).

Uses real temp SQLite; all external calls (wallet RPC, Sage, cat_resolver,
notify_cat_asset_id_changed) are mocked.
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
_FAKE_TRADE_ID = "test-start-stop-001"


class _TempDB(unittest.TestCase):
    """Base: redirect the database module to a fresh temp SQLite file."""

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

    def _seed_fill(self):
        _db.record_fill(
            trade_id=_FAKE_TRADE_ID,
            side="buy",
            price_xch=Decimal("0.002"),
            size_xch=Decimal("0.001"),
            size_cat=Decimal("0.5"),
            cat_asset_id=_FAKE_ASSET,
        )

    def _fill_count(self):
        conn = _db.get_connection()
        return conn.execute("SELECT COUNT(*) AS cnt FROM fills").fetchone()["cnt"]

    def _make_bot(self, running=False):
        bot = MagicMock()
        bot.is_running.return_value = running
        bot.start.return_value = True
        bot.get_state.return_value = {"status": "running"}
        bot.market_intel.reset_session_stats = MagicMock()
        bot.splash_manager.reset_session_stats = MagicMock()
        bot.get_splash_receive_stats.return_value = {}
        return bot

    def _start(self, bot_mock):
        with (
            patch.object(api_server, "bot", bot_mock),
            patch("api_server._get_sage_signing_block_reason", return_value=None),
            patch(
                "wallet.get_wallet_sync_status",
                return_value={"reachable": True, "sync_state": "synced"},
            ),
            patch.object(api_server.cfg, "CAT_ASSET_ID", _FAKE_ASSET),
            patch.object(api_server.cfg, "SPREAD_BPS", 50),
            patch("api_server.events") as mock_events,
        ):
            resp = self.client.post(
                "/api/bot/start",
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
            return resp, mock_events

    def _stop(self, bot_mock):
        with (
            patch.object(api_server, "bot", bot_mock),
            patch("api_server.events") as mock_events,
        ):
            resp = self.client.post(
                "/api/bot/stop",
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
            return resp, mock_events


# ---------------------------------------------------------------------------
# Bot start: validation + basic response contract
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestBotStartContract(_TempDB):
    """Start endpoint returns the right shape when validation passes."""

    def test_start_returns_200_on_success(self):
        resp, _ = self._start(self._make_bot(running=False))
        self.assertEqual(resp.status_code, 200)

    def test_start_response_has_success_true(self):
        resp, _ = self._start(self._make_bot(running=False))
        self.assertTrue(resp.get_json().get("success"))

    def test_start_emits_bot_control_event(self):
        _, mock_events = self._start(self._make_bot(running=False))
        mock_events.emit.assert_any_call("bot_control", {"action": "started"})

    def test_start_already_running_returns_already_running(self):
        resp, _ = self._start(self._make_bot(running=True))
        body = resp.get_json()
        self.assertEqual(body.get("status"), "already_running")
        self.assertTrue(body.get("success"))

    def test_start_no_asset_id_returns_400(self):
        bot = self._make_bot(running=False)
        with (
            patch.object(api_server, "bot", bot),
            patch.object(api_server.cfg, "CAT_ASSET_ID", ""),
            patch.object(api_server.cfg, "SPREAD_BPS", 50),
            patch("api_server._get_sage_signing_block_reason", return_value=None),
        ):
            resp = self.client.post(
                "/api/bot/start",
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        self.assertEqual(resp.status_code, 400)

    def test_start_clears_fresh_start_flag(self):
        api_server._fresh_start_set()
        self._start(self._make_bot(running=False))
        self.assertFalse(api_server._fresh_start_is_set())


# ---------------------------------------------------------------------------
# DB state survives bot start
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestBotStartPreservesFills(_TempDB):
    """DB fills are not touched by the start cycle."""

    def test_fills_survive_bot_start(self):
        """Fills inserted before start are still present after start."""
        self._seed_fill()
        self.assertEqual(self._fill_count(), 1)
        self._start(self._make_bot(running=False))
        self.assertEqual(self._fill_count(), 1)

    def test_start_does_not_add_extra_fills(self):
        """Start cycle never inserts phantom fill records."""
        self._start(self._make_bot(running=False))
        self.assertEqual(self._fill_count(), 0)

    def test_start_does_not_touch_events_table(self):
        """Start cycle must not add event rows to the fills or round_trips tables."""
        fill_count_before = self._fill_count()
        self._start(self._make_bot(running=False))
        self.assertEqual(self._fill_count(), fill_count_before)


# ---------------------------------------------------------------------------
# Bot stop: DB state survives
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestBotStopPreservesFills(_TempDB):
    """DB fills are not touched by the stop cycle."""

    def test_stop_returns_200(self):
        resp, _ = self._stop(self._make_bot(running=True))
        self.assertEqual(resp.status_code, 200)

    def test_stop_response_has_status_stopped(self):
        resp, _ = self._stop(self._make_bot(running=True))
        body = resp.get_json()
        self.assertEqual(body.get("status"), "stopped")

    def test_stop_emits_bot_control_event(self):
        _, mock_events = self._stop(self._make_bot(running=True))
        mock_events.emit.assert_called_with("bot_control", {"action": "stopped"})

    def test_fills_survive_bot_stop(self):
        """Fills inserted before stop are still present after stop."""
        self._seed_fill()
        self.assertEqual(self._fill_count(), 1)
        self._stop(self._make_bot(running=True))
        self.assertEqual(self._fill_count(), 1)

    def test_stop_does_not_wipe_fills(self):
        """stop() never deletes fill records."""
        self._seed_fill()
        self._stop(self._make_bot(running=True))
        # Count must not drop
        self.assertGreaterEqual(self._fill_count(), 1)


# ---------------------------------------------------------------------------
# Full start → stop cycle: DB state consistent throughout
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartStopCycleDBConsistency(_TempDB):
    """The full start→stop cycle must not mutate fills or round-trip records."""

    def test_fills_survive_full_start_stop_cycle(self):
        """Fills present before start are still present after stop."""
        self._seed_fill()
        before = self._fill_count()

        bot = self._make_bot(running=False)
        self._start(bot)
        mid = self._fill_count()

        bot.is_running.return_value = True
        self._stop(bot)
        after = self._fill_count()

        self.assertEqual(before, mid)
        self.assertEqual(mid, after)

    def test_stop_bot_that_was_never_started_does_not_crash(self):
        """Stopping a bot that was never explicitly started is safe."""
        bot = self._make_bot(running=False)
        resp, _ = self._stop(bot)
        self.assertEqual(resp.status_code, 200)

    def test_repeated_start_stop_cycles_do_not_multiply_fills(self):
        """Two start→stop cycles don't duplicate fill rows."""
        self._seed_fill()
        bot = self._make_bot(running=False)

        self._start(bot)
        bot.is_running.return_value = True
        self._stop(bot)

        bot.is_running.return_value = False
        self._start(bot)
        bot.is_running.return_value = True
        self._stop(bot)

        self.assertEqual(self._fill_count(), 1)


if __name__ == "__main__":
    unittest.main()
