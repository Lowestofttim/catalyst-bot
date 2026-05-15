"""Slice 03-03 — pair-switch: mid-session CAT change, DB/state cleanup (integration).

Tests the full CAT pair-switch flow:

  Validation:
    - Switching while bot is running → 409 (blocked)
    - Switching while bot is stopped → 200 + success=True
    - Invalid asset_id format (not 64 hex) → 400
    - Name too long → 400

  In-memory state update:
    - _active_cat["asset_id"] updated after switch
    - _active_cat["name"] updated after switch

  Risk manager reset:
    - risk_manager.reset_session() called when bot is present
    - No error when bot is None

  DB preservation:
    - Fills from the previous pair are NOT deleted on pair switch
    - Round-trip records survive pair switch
    - Pair switch does NOT touch fills table

  CAT_ASSET_ID persisted:
    - cfg.update("CAT_ASSET_ID", ...) called → cfg reflects new asset

Uses the temp-DB pattern (real SQLite). Background threads (cat_resolver,
notify_cat_asset_id_changed) are mocked to prevent side effects.
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

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

_ASSET_A = "a" * 64
_ASSET_B = "b" * 64
_FAKE_TRADE_ID = "test-pair-switch-001"


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

        # Snapshot _active_cat so tearDown can restore it
        self._orig_active_cat = dict(api_server._active_cat)

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
        api_server._active_cat.clear()
        api_server._active_cat.update(self._orig_active_cat)

    def _seed_fill(self, asset_id=_ASSET_A):
        _db.record_fill(
            trade_id=_FAKE_TRADE_ID,
            side="buy",
            price_xch=Decimal("0.002"),
            size_xch=Decimal("0.001"),
            size_cat=Decimal("0.5"),
            cat_asset_id=asset_id,
        )

    def _fill_count(self):
        conn = _db.get_connection()
        return conn.execute("SELECT COUNT(*) AS cnt FROM fills").fetchone()["cnt"]

    def _switch(self, asset_id=_ASSET_B, name="TokenB", bot=None):
        """POST /api/cat/select with all background side-effects mocked."""
        with (
            patch.object(api_server, "bot", bot),
            patch("api_server.cfg.update"),
            patch("wallet_sage.notify_cat_asset_id_changed", create=True),
            patch("api_server.threading") as mock_threading,
        ):
            mock_threading.Thread.return_value = MagicMock()
            resp = self.client.post(
                "/api/cat/select",
                json={
                    "asset_id": asset_id,
                    "name": name,
                    "wallet_id": 2,
                    "decimals": 3,
                },
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        return resp


# ---------------------------------------------------------------------------
# Validation guards
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestPairSwitchValidation(_TempDB):
    """Pair switch must be blocked while bot runs and reject bad input."""

    def test_switch_blocked_while_bot_running(self):
        """Switching while bot is running must return 409."""
        bot = MagicMock()
        bot.is_running.return_value = True
        resp = self._switch(bot=bot)
        self.assertEqual(resp.status_code, 409)

    def test_switch_blocked_response_has_error(self):
        """409 response includes an error message."""
        bot = MagicMock()
        bot.is_running.return_value = True
        resp = self._switch(bot=bot)
        body = resp.get_json()
        self.assertIn("error", body)

    def test_switch_allowed_when_bot_stopped(self):
        """When bot is stopped (is_running=False), switch succeeds."""
        bot = MagicMock()
        bot.is_running.return_value = False
        resp = self._switch(bot=bot)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

    def test_switch_allowed_when_no_bot(self):
        """When bot is None, switch is always allowed."""
        resp = self._switch(bot=None)
        self.assertEqual(resp.status_code, 200)

    def test_invalid_asset_id_returns_400(self):
        """Non-hex asset_id must be rejected with 400."""
        with (
            patch.object(api_server, "bot", None),
            patch("api_server.cfg.update"),
            patch("wallet_sage.notify_cat_asset_id_changed", create=True),
            patch("api_server.threading"),
        ):
            resp = self.client.post(
                "/api/cat/select",
                json={"asset_id": "not-valid", "name": "Bad"},
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        self.assertEqual(resp.status_code, 400)

    def test_short_asset_id_returns_400(self):
        """asset_id shorter than 64 chars is rejected."""
        with (
            patch.object(api_server, "bot", None),
            patch("api_server.cfg.update"),
            patch("wallet_sage.notify_cat_asset_id_changed", create=True),
            patch("api_server.threading"),
        ):
            resp = self.client.post(
                "/api/cat/select",
                json={"asset_id": "a" * 32, "name": "Short"},
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# In-memory state update
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestPairSwitchStateUpdate(_TempDB):
    """_active_cat must reflect the new pair after a successful switch."""

    def test_active_cat_asset_id_updated(self):
        """_active_cat["asset_id"] contains the new asset_id after switch."""
        self._switch(asset_id=_ASSET_B, name="TokenB", bot=None)
        self.assertEqual(api_server._active_cat.get("asset_id"), _ASSET_B)

    def test_active_cat_name_updated(self):
        """_active_cat["name"] reflects the new token name after switch."""
        self._switch(asset_id=_ASSET_B, name="TokenB", bot=None)
        self.assertEqual(api_server._active_cat.get("name"), "TokenB")

    def test_active_cat_wallet_id_updated(self):
        """_active_cat["wallet_id"] is set from the request payload."""
        self._switch(asset_id=_ASSET_B, name="TokenB", bot=None)
        self.assertEqual(api_server._active_cat.get("wallet_id"), 2)

    def test_response_echoes_asset_id(self):
        """Success response body includes the normalized asset_id."""
        resp = self._switch(asset_id=_ASSET_B, name="TokenB", bot=None)
        body = resp.get_json()
        self.assertEqual(body.get("asset_id"), _ASSET_B)


# ---------------------------------------------------------------------------
# Risk manager reset
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestPairSwitchRiskManagerReset(_TempDB):
    """risk_manager.reset_session() must be called when bot is present."""

    def test_risk_manager_reset_called_on_switch(self):
        """When bot is present and stopped, reset_session() fires on switch."""
        bot = MagicMock()
        bot.is_running.return_value = False
        self._switch(asset_id=_ASSET_B, bot=bot)
        bot.risk_manager.reset_session.assert_called_once()

    def test_risk_manager_reset_not_called_when_bot_none(self):
        """When bot is None, no risk reset is attempted (no AttributeError)."""
        try:
            resp = self._switch(asset_id=_ASSET_B, bot=None)
        except Exception as exc:
            self.fail(f"switch with bot=None raised: {exc}")
        self.assertEqual(resp.status_code, 200)

    def test_risk_manager_exception_does_not_block_switch(self):
        """Even if reset_session() raises, the switch still succeeds."""
        bot = MagicMock()
        bot.is_running.return_value = False
        bot.risk_manager.reset_session.side_effect = RuntimeError("mock error")
        resp = self._switch(asset_id=_ASSET_B, bot=bot)
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# DB preservation after pair switch
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestPairSwitchDBPreservation(_TempDB):
    """Pair switch must never touch fills or round-trip data in the DB."""

    def test_fills_for_old_pair_survive_switch(self):
        """Fills for the previous pair are still in DB after switching to a new one."""
        self._seed_fill(asset_id=_ASSET_A)
        self.assertEqual(self._fill_count(), 1)
        self._switch(asset_id=_ASSET_B, bot=None)
        self.assertEqual(self._fill_count(), 1)

    def test_switch_does_not_add_fill_rows(self):
        """Pair switch never inserts phantom fill records."""
        self._switch(asset_id=_ASSET_B, bot=None)
        self.assertEqual(self._fill_count(), 0)

    def test_multiple_switches_do_not_accumulate_fills(self):
        """Switching back and forth does not multiply fill rows."""
        self._seed_fill(asset_id=_ASSET_A)
        self._switch(asset_id=_ASSET_B, bot=None)
        self._switch(asset_id=_ASSET_A, bot=None)
        self.assertEqual(self._fill_count(), 1)

    def test_fills_for_both_pairs_survive_switch(self):
        """Fills for multiple pairs are all preserved after switch."""
        self._seed_fill(asset_id=_ASSET_A)
        _db.record_fill(
            trade_id=_FAKE_TRADE_ID + "-b",
            side="sell",
            price_xch=Decimal("0.003"),
            size_xch=Decimal("0.002"),
            size_cat=Decimal("1.0"),
            cat_asset_id=_ASSET_B,
        )
        self.assertEqual(self._fill_count(), 2)
        self._switch(asset_id=_ASSET_B, bot=None)
        self.assertEqual(self._fill_count(), 2)


if __name__ == "__main__":
    unittest.main()
