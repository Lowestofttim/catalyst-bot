"""Slice 04-03 — bot lifecycle endpoint contract tests.

Tests /api/bot/start, /api/bot/stop, /api/shutdown:
  - Auth required for all (token)
  - bot=None → 500 for start/stop
  - Already-running state returns correct status
  - Validation errors block start
  - Stop/shutdown return success shapes
"""

import os
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        self.auth = {"X-Bot-Local-Token": self.token}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def _post(self, path, body=None, auth=True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body or {},
            headers=headers,
            environ_base=self._LOOPBACK,
        )


def _make_bot(running=False, start_returns=True):
    bot = MagicMock()
    bot.is_running.return_value = running
    bot.start.return_value = start_returns
    bot.stop.return_value = None
    bot.get_state.return_value = {
        "running": running, "status": "running" if running else "idle",
        "loop_count": 0,
    }
    return bot


def _fake_cfg(cat_asset_id="abc123", spread_bps=200):
    return types.SimpleNamespace(
        CAT_ASSET_ID=cat_asset_id,
        SPREAD_BPS=spread_bps,
        HARD_MIN_PRICE_XCH=Decimal("0.001"),
        HARD_MAX_PRICE_XCH=Decimal("1.0"),
    )


# ---------------------------------------------------------------------------
# 1. POST /api/bot/start
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestBotStart(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/bot/start", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 500)
        self.assertIn("error", resp.get_json())

    def test_already_running_returns_200_and_already_running_status(self):
        with patch.object(api_server, "bot", _make_bot(running=True)):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "already_running")

    def test_no_cat_asset_id_returns_400_with_errors(self):
        fake_cfg = _fake_cfg(cat_asset_id="")
        with patch.object(api_server, "bot", _make_bot(running=False)), \
             patch.object(api_server, "cfg", fake_cfg):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("errors", body)
        self.assertGreater(len(body["errors"]), 0)
        joined_errors = " ".join(str(error) for error in body["errors"])
        self.assertIn("Choose a trading pair", joined_errors)
        self.assertNotIn(".env", joined_errors)
        self.assertNotIn("CAT_ASSET_ID", joined_errors)

    def test_zero_spread_returns_400_with_errors(self):
        fake_cfg = _fake_cfg(spread_bps=0)
        with patch.object(api_server, "bot", _make_bot(running=False)), \
             patch.object(api_server, "cfg", fake_cfg):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("errors", body)

    def test_successful_start_returns_200_started_status(self):
        fake_cfg = _fake_cfg()
        bot = _make_bot(running=False, start_returns=True)
        with patch.object(api_server, "bot", bot), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "_get_sage_signing_block_reason", return_value=None), \
             patch("wallet.get_wallet_sync_status", return_value={"reachable": True, "sync_state": "synced"}):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "started")

    def test_start_blocked_by_bot_returns_400(self):
        fake_cfg = _fake_cfg()
        bot = _make_bot(running=False, start_returns=False)
        with patch.object(api_server, "bot", bot), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "_get_sage_signing_block_reason", return_value=None), \
             patch("wallet.get_wallet_sync_status", return_value={"reachable": True, "sync_state": "synced"}):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("errors", body)

    def test_tier_size_drift_returns_coin_prep_response(self):
        fake_cfg = _fake_cfg()
        bot = _make_bot(running=False)
        drift = [{
            "side": "xch",
            "tier": "inner",
            "ratio": 0.457,
            "coin_count": 11,
        }]
        with patch.object(api_server, "bot", bot), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "_get_sage_signing_block_reason", return_value=None), \
             patch("wallet.get_wallet_sync_status",
                   return_value={"reachable": True, "sync_state": "synced"}), \
             patch("coin_manager.check_tier_size_drift_standalone",
                   return_value=drift):
            resp = self._post("/api/bot/start")

        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertTrue(body.get("needs_coin_prep"))
        self.assertEqual(body.get("reason"), "tier_size_drift")
        self.assertEqual(body.get("tier_size_drift"), drift)
        bot.start.assert_not_called()

    def test_failed_coin_prep_blocks_start(self):
        fake_cfg = _fake_cfg()
        fake_cfg.ENABLE_COIN_PREP = True
        bot = _make_bot(running=False)
        failed_state = {
            "running": False,
            "complete": False,
            "phase": "error",
            "error": "Sage tier pool creation + splitting failed",
        }
        with patch.object(api_server, "bot", bot), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch.dict(api_server._coin_prep_state, failed_state, clear=True), \
             patch.object(api_server, "_get_sage_signing_block_reason", return_value=None), \
             patch("wallet.get_wallet_sync_status",
                   return_value={"reachable": True, "sync_state": "synced"}), \
             patch("coin_manager.check_tier_size_drift_standalone",
                   return_value=[]):
            resp = self._post("/api/bot/start")

        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertTrue(body.get("needs_coin_prep"))
        self.assertEqual(body.get("reason"), "coin_prep_failed")
        self.assertEqual(
            body.get("message"),
            "Coin Prep failed - rerun Coin Prep before starting the bot",
        )
        self.assertNotIn("Sage tier pool creation", resp.get_data(as_text=True))
        bot.start.assert_not_called()

    def test_signing_block_reason_prevents_start(self):
        """If _get_sage_signing_block_reason returns a string, start is blocked."""
        fake_cfg = _fake_cfg()
        bot = _make_bot(running=False)
        with patch.object(api_server, "bot", bot), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "_get_sage_signing_block_reason",
                          return_value="Sage cannot sign"):
            resp = self._post("/api/bot/start")
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("Sage cannot sign", body.get("errors", []))


# ---------------------------------------------------------------------------
# 2. POST /api/bot/stop
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestBotStop(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/bot/stop", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self._post("/api/bot/stop")
        self.assertEqual(resp.status_code, 500)

    def test_bot_running_stop_returns_200(self):
        bot = _make_bot(running=True)
        with patch.object(api_server, "bot", bot):
            resp = self._post("/api/bot/stop")
        self.assertEqual(resp.status_code, 200)

    def test_stop_response_has_status_key(self):
        bot = _make_bot(running=True)
        with patch.object(api_server, "bot", bot):
            resp = self._post("/api/bot/stop")
        body = resp.get_json()
        self.assertIn("status", body)
        self.assertEqual(body["status"], "stopped")

    def test_stop_calls_bot_stop(self):
        bot = _make_bot(running=True)
        with patch.object(api_server, "bot", bot):
            self._post("/api/bot/stop")
        bot.stop.assert_called_once()


# ---------------------------------------------------------------------------
# 3. POST /api/shutdown
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestShutdown(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/shutdown", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200(self):
        with patch.object(api_server, "bot", None), \
             patch("threading.Thread"):
            resp = self._post("/api/shutdown")
        self.assertEqual(resp.status_code, 200)

    def test_response_has_success_key(self):
        with patch.object(api_server, "bot", None), \
             patch("threading.Thread"):
            resp = self._post("/api/shutdown")
        body = resp.get_json()
        self.assertIsInstance(body, dict)

    def test_cancel_offers_false_by_default(self):
        """Default request body has cancel_offers=False."""
        with patch.object(api_server, "bot", None), \
             patch("threading.Thread") as mock_thread:
            self._post("/api/shutdown")
        # Thread should have been started for the background shutdown
        mock_thread.assert_called()


if __name__ == "__main__":
    unittest.main()
