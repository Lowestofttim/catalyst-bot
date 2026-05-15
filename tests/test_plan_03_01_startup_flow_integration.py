"""Slice 03-01 — startup flow: fresh app → risk disclosure → Sage → dashboard (integration).

Tests the full startup sequence at the API level with mocked externals:

  Phase 1 — check-resume on fresh DB:
    - GET /api/check-resume with no wallet offers → can_resume=False
    - GET /api/check-resume with wallet offers → can_resume=True
    - Bot already running → skip check-resume (returns can_resume=False)

  Phase 2 — user acknowledges risk / chooses session mode:
    - POST /api/session/fresh-start (or /api/session/resume-chosen) succeeds
    - Fresh-start sets the flag, clears fills
    - Resume-chosen clears the flag, preserves fills

  Phase 3 — Sage wallet connection:
    - GET /api/wallet/sage-running → running=True when port reachable
    - POST /api/wallet/begin-startup → started=True

  Phase 4 — CAT pair selection:
    - POST /api/cat/select with valid asset_id succeeds
    - _active_cat updated with the selected pair

  Phase 5 — dashboard initial state:
    - GET /api/dashboard returns 200 with known keys (settings, market_health, wallet)
    - No bot present → most fields degrade gracefully (no 500)

  Phase 6 — pre-start validation:
    - POST /api/bot/start without CAT_ASSET_ID → 400 (setup not complete)
    - POST /api/bot/start with all config valid → 200 success

Uses real temp SQLite for DB-level assertions; external calls (wallet RPC, Sage,
chia_node) are mocked. All tests are independent (no shared state across classes).
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
_FAKE_ASSET = "b" * 64
_FAKE_TRADE_ID = "startup-test-001"


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

        # Isolate the fresh-start flag to a per-test path. The default
        # path is a fixed file under src/catalyst/, which is shared
        # across pytest-xdist workers and causes cross-worker races
        # (one worker's tearDown deletes another worker's flag mid-test).
        # Each test gets its own temp file; tearDown restores the
        # original module path.
        self._orig_fresh_start_flag = api_server._FRESH_START_FLAG
        self._fresh_start_flag_tmp = tempfile.NamedTemporaryFile(
            suffix=".fresh_start_chosen", delete=False
        )
        self._fresh_start_flag_tmp.close()
        # Start clean: remove the file so _fresh_start_is_set() returns False
        try:
            os.unlink(self._fresh_start_flag_tmp.name)
        except Exception:
            pass
        api_server._FRESH_START_FLAG = self._fresh_start_flag_tmp.name

        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        api_server._rate_limit_log.clear()
        api_server._fresh_start_clear()

        self._orig_session_start_time = api_server._session_start_time
        self._orig_run_history_cutoff = api_server._run_history_cutoff
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
        # Restore the per-test fresh-start flag isolation
        try:
            os.unlink(self._fresh_start_flag_tmp.name)
        except Exception:
            pass
        api_server._FRESH_START_FLAG = self._orig_fresh_start_flag
        api_server._session_start_time = self._orig_session_start_time
        api_server._run_history_cutoff = self._orig_run_history_cutoff
        api_server._active_cat.clear()
        api_server._active_cat.update(self._orig_active_cat)

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


# ---------------------------------------------------------------------------
# Phase 1: check-resume on fresh DB
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartupPhase1CheckResume(_TempDB):
    """check-resume must return the correct can_resume flag on a fresh DB."""

    def _check(self, wallet_offers=None, classified_buy=None):
        wallet_offers = wallet_offers or []
        classified_buy = classified_buy or [
            {"trade_id": o["trade_id"]} for o in wallet_offers
        ]
        with (
            patch("api_server.bot", None),
            patch("api_server._fresh_start_is_set", return_value=False),
            patch("wallet.get_all_offers", return_value=wallet_offers),
            patch(
                "wallet.classify_offers_from_list",
                return_value=(classified_buy, [], []),
            ),
        ):
            return self.client.get("/api/check-resume", environ_base=_LOOPBACK)

    def test_fresh_db_no_offers_returns_can_resume_false(self):
        """With no open wallet offers on fresh DB, can_resume=False."""
        resp = self._check([])
        self.assertFalse(resp.get_json().get("can_resume"))

    def test_fresh_db_check_returns_200(self):
        resp = self._check([])
        self.assertEqual(resp.status_code, 200)

    def test_existing_offers_returns_can_resume_true(self):
        """If wallet has open offers from a previous session, can_resume=True."""
        resp = self._check([{"trade_id": "prev-buy-1"}])
        self.assertTrue(resp.get_json().get("can_resume"))

    def test_running_bot_returns_can_resume_false(self):
        """If bot is already running, skip the resume modal."""
        bot = MagicMock()
        bot.is_running.return_value = True
        bot._loop_count = 1  # MagicMock default would fail the > 0 comparison
        with (
            patch("api_server.bot", bot),
            patch("wallet.get_all_offers", return_value=[{"trade_id": "b1"}]),
            patch(
                "wallet.classify_offers_from_list",
                return_value=([{"trade_id": "b1"}], [], []),
            ),
        ):
            resp = self.client.get("/api/check-resume", environ_base=_LOOPBACK)
        self.assertFalse(resp.get_json().get("can_resume"))

    def test_response_has_buy_and_sell_counts(self):
        """Response includes buy_count and sell_count fields."""
        resp = self._check([{"trade_id": "b1"}], classified_buy=[{"trade_id": "b1"}])
        body = resp.get_json()
        self.assertIn("buy_count", body)
        self.assertIn("sell_count", body)


# ---------------------------------------------------------------------------
# Phase 2: session mode selection (fresh-start vs resume-chosen)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartupPhase2SessionMode(_TempDB):
    """Session mode selection must apply the right DB and flag changes."""

    def _fresh_start(self):
        return self.client.post(
            "/api/session/fresh-start",
            headers={"X-Bot-Local-Token": self.token},
            environ_base=_LOOPBACK,
        )

    def _resume_chosen(self):
        return self.client.post(
            "/api/session/resume-chosen",
            headers={"X-Bot-Local-Token": self.token},
            environ_base=_LOOPBACK,
        )

    def test_fresh_start_returns_success(self):
        self.assertTrue(self._fresh_start().get_json().get("success"))

    def test_fresh_start_clears_fills(self):
        """Fresh-start path deletes all fills — user chose to start over."""
        self._seed_fill()
        self._fresh_start()
        self.assertEqual(self._fill_count(), 0)

    def test_fresh_start_sets_session_flag(self):
        """After fresh-start, the flag prevents re-showing the resume modal."""
        self._fresh_start()
        self.assertTrue(api_server._fresh_start_is_set())

    def test_resume_chosen_returns_success(self):
        self.assertTrue(self._resume_chosen().get_json().get("success"))

    def test_resume_chosen_preserves_fills(self):
        """Resume-chosen path must NOT delete fills."""
        self._seed_fill()
        self._resume_chosen()
        self.assertEqual(self._fill_count(), 1)

    def test_resume_chosen_clears_fresh_start_flag(self):
        """After resume-chosen, fresh_start flag is removed."""
        api_server._fresh_start_set()
        self._resume_chosen()
        self.assertFalse(api_server._fresh_start_is_set())


# ---------------------------------------------------------------------------
# Phase 3: Sage wallet connection probes
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartupPhase3SageConnection(_TempDB):
    """Sage running probe and begin-startup must behave correctly."""

    def test_sage_running_returns_true_when_reachable(self):
        """GET /api/wallet/sage-running returns running=True when port open."""
        with patch("sage_node._is_sage_rpc_available", return_value=True):
            resp = self.client.get("/api/wallet/sage-running", environ_base=_LOOPBACK)
        self.assertTrue(resp.get_json().get("running"))

    def test_sage_running_returns_false_when_unreachable(self):
        """GET /api/wallet/sage-running returns running=False when port closed."""
        with (
            patch("sage_node._is_sage_rpc_available", return_value=False),
            patch("sage_node._is_sage_rpc_port_listening", return_value=False),
        ):
            resp = self.client.get("/api/wallet/sage-running", environ_base=_LOOPBACK)
        self.assertFalse(resp.get_json().get("running"))

    def test_sage_running_returns_200(self):
        with patch("sage_node._is_sage_rpc_available", return_value=True):
            resp = self.client.get("/api/wallet/sage-running", environ_base=_LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_begin_startup_returns_started_true(self):
        """POST /api/wallet/begin-startup fires the preload and returns started=True."""
        with patch("chia_node.set_auto_launch"), patch("chia_node.start_preload"):
            resp = self.client.post(
                "/api/wallet/begin-startup",
                json={"auto_launch": True},
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        self.assertTrue(resp.get_json().get("started"))

    def test_begin_startup_returns_200(self):
        with patch("chia_node.set_auto_launch"), patch("chia_node.start_preload"):
            resp = self.client.post(
                "/api/wallet/begin-startup",
                json={},
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# Phase 4: CAT pair selection
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartupPhase4PairSelection(_TempDB):
    """User selects a CAT pair after connecting wallet."""

    def _select_cat(self, asset_id=_FAKE_ASSET, name="TestToken"):
        with (
            patch.object(api_server, "bot", None),
            patch("api_server.cfg.update"),
            patch("wallet_sage.notify_cat_asset_id_changed", create=True),
            patch("api_server.threading") as mock_threading,
        ):
            mock_threading.Thread.return_value = MagicMock()
            return self.client.post(
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

    def test_cat_select_returns_success(self):
        self.assertTrue(self._select_cat().get_json().get("success"))

    def test_cat_select_updates_active_cat(self):
        """After pair selection, _active_cat reflects the chosen token."""
        self._select_cat(asset_id=_FAKE_ASSET, name="TestToken")
        self.assertEqual(api_server._active_cat.get("asset_id"), _FAKE_ASSET)
        self.assertEqual(api_server._active_cat.get("name"), "TestToken")


# ---------------------------------------------------------------------------
# Phase 5: dashboard initial state
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartupPhase5DashboardLoad(_TempDB):
    """Dashboard must load without 500 even before bot is started."""

    def _get_dashboard(self, bot=None):
        with (
            patch.object(api_server, "bot", bot),
            patch(
                "wallet.get_wallet_sync_status",
                return_value={"reachable": False, "synced": False},
            ),
        ):
            return self.client.get("/api/dashboard", environ_base=_LOOPBACK)

    def test_dashboard_returns_200_with_no_bot(self):
        """Dashboard endpoint returns 200 even when bot=None."""
        resp = self._get_dashboard(bot=None)
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_response_has_settings_key(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIn("settings", body)

    def test_dashboard_response_has_wallet_key(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIn("wallet", body)

    def test_dashboard_response_has_market_health_key(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIn("market_health", body)


# ---------------------------------------------------------------------------
# Phase 6: pre-start validation (setup incomplete → error; complete → start)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestStartupPhase6BotStartValidation(_TempDB):
    """Bot start must fail gracefully when setup is incomplete."""

    def _try_start(self, asset_id="", spread_bps=50, bot=None):
        if bot is None:
            bot = MagicMock()
            bot.is_running.return_value = False
            bot.start.return_value = True
            bot.market_intel.reset_session_stats = MagicMock()
            bot.splash_manager.reset_session_stats = MagicMock()
            bot.get_splash_receive_stats.return_value = {}
        with (
            patch.object(api_server, "bot", bot),
            patch("api_server._get_sage_signing_block_reason", return_value=None),
            patch(
                "wallet.get_wallet_sync_status",
                return_value={"reachable": True, "sync_state": "synced"},
            ),
            patch.object(api_server.cfg, "CAT_ASSET_ID", asset_id),
            patch.object(api_server.cfg, "SPREAD_BPS", spread_bps),
            patch("api_server.events"),
        ):
            return self.client.post(
                "/api/bot/start",
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )

    def test_start_without_cat_asset_id_returns_400(self):
        """No CAT_ASSET_ID → setup incomplete → 400 error."""
        resp = self._try_start(asset_id="")
        self.assertEqual(resp.status_code, 400)

    def test_start_without_cat_includes_error_message(self):
        """400 response gives a user-facing setup action."""
        resp = self._try_start(asset_id="")
        body = resp.get_json()
        errors = body.get("errors", [])
        joined_errors = " ".join(str(error) for error in errors)
        self.assertIn("Choose a trading pair", joined_errors)
        self.assertNotIn(".env", joined_errors)
        self.assertNotIn("CAT_ASSET_ID", joined_errors)

    def test_start_with_valid_config_returns_success(self):
        """All config valid → 200 success."""
        resp = self._try_start(asset_id=_FAKE_ASSET, spread_bps=50)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

    def test_full_startup_sequence_db_consistent(self):
        """Fills seeded before startup are intact after the whole sequence."""
        self._seed_fill()

        # Phase 2: resume chosen
        self.client.post(
            "/api/session/resume-chosen",
            headers={"X-Bot-Local-Token": self.token},
            environ_base=_LOOPBACK,
        )

        # Phase 6: bot start
        self._try_start(asset_id=_FAKE_ASSET, spread_bps=50)

        self.assertEqual(self._fill_count(), 1)


if __name__ == "__main__":
    unittest.main()
