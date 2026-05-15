"""Slice 04-01 — status endpoints contract tests.

Tests /api/bot/state, /api/bot/price, and /api/status response contracts:
  - correct status codes for bot=None vs bot-set
  - required keys present in successful responses
  - error shape on failure paths
  - wallet calls skipped when bot provides sufficient state
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Fake bots
# ---------------------------------------------------------------------------


def _fake_bot_stopped():
    """Minimal fake bot in a stopped state with non-zero coin counts."""
    return types.SimpleNamespace(
        is_running=lambda: False,
        _start_time=None,
        get_state=lambda: {
            "running": False,
            "loop_count": 0,
            "loop_duration": 0,
            "loop_seconds": 30,
            "dry_run": True,
            # Non-zero coins so api_bot_state() doesn't fall back to wallet RPC
            "coins": {
                "xch_coins": 5,
                "cat_coins": 3,
                "xch_total_coins": 7,
                "cat_total_coins": 4,
                "xch_locked_coins": 2,
                "cat_locked_coins": 1,
                "xch_balance": {"spendable": 5.0, "total": 7.0},
                "cat_balance": {"spendable": 3.0, "total": 4.0},
                "inventory": {},
            },
            "risk": {"circuit_breaker_tripped": False},
            "stats": {"total_fills": 0, "errors": 0},
            "fills": {"recent": [], "counts": {}},
            "sniper": {"total_snipes": 0},
            "market_intel": {},
            "diagnostics": {},
            "splash": {},
            "splash_node": {"running": False},
            "chia_health": {"status": "not_started"},
            "wallet_type": "sage",
        },
        get_price_info=lambda: {
            "mid_price": "0",
            "last_quoted_buy": "0",
            "last_quoted_sell": "0",
        },
    )


def _fake_bot_running():
    """Minimal fake bot in a running state."""
    return types.SimpleNamespace(
        is_running=lambda: True,
        _start_time=1700000000.0,
        get_state=lambda: {
            "running": True,
            "loop_count": 42,
            "loop_duration": 1.5,
            "loop_seconds": 30,
            "dry_run": False,
            "coins": {
                "xch_coins": 10,
                "cat_coins": 8,
                "xch_total_coins": 15,
                "cat_total_coins": 12,
                "xch_locked_coins": 5,
                "cat_locked_coins": 4,
                "xch_balance": {"spendable": 10.0, "total": 15.0},
                "cat_balance": {"spendable": 8.0, "total": 12.0},
                "inventory": {},
            },
            "risk": {"circuit_breaker_tripped": False},
            "stats": {"total_fills": 5, "errors": 0},
            "fills": {"recent": [], "counts": {"total": 5}},
            "sniper": {"total_snipes": 1},
            "market_intel": {},
            "diagnostics": {},
            "splash": {},
            "splash_node": {"running": False},
            "chia_health": {"status": "healthy"},
            "wallet_type": "sage",
        },
        get_price_info=lambda: {
            "mid_price": "0.00123456",
            "last_quoted_buy": "0.00122000",
            "last_quoted_sell": "0.00124900",
        },
    )


# ---------------------------------------------------------------------------
# Base — Flask test client
# ---------------------------------------------------------------------------


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()


# ---------------------------------------------------------------------------
# 1. /api/bot/state
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestBotStateContract(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_bot_none_error_body_has_error_key(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("error", body)

    def test_stopped_bot_returns_200(self):
        with patch.object(api_server, "bot", _fake_bot_stopped()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_stopped_bot_running_field_false(self):
        with patch.object(api_server, "bot", _fake_bot_stopped()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body.get("running"))

    def test_running_bot_returns_200(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_running_bot_running_field_true(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body.get("running"))

    def test_response_is_dict(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        self.assertIsInstance(resp.get_json(), dict)

    def test_loop_count_present(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("loop_count", body)
        self.assertEqual(body["loop_count"], 42)

    def test_coins_key_present(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/state", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("coins", body)


# ---------------------------------------------------------------------------
# 2. /api/bot/price
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestBotPriceContract(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_bot_none_error_key(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        self.assertIn("error", resp.get_json())

    def test_bot_set_returns_200(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_mid_price_key_present(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("mid_price", body)

    def test_last_quoted_buy_key_present(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("last_quoted_buy", body)

    def test_last_quoted_sell_key_present(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("last_quoted_sell", body)

    def test_price_values_are_strings(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIsInstance(body["mid_price"], str)
        self.assertIsInstance(body["last_quoted_buy"], str)
        self.assertIsInstance(body["last_quoted_sell"], str)

    def test_price_values_match_fake_bot(self):
        with patch.object(api_server, "bot", _fake_bot_running()):
            resp = self.client.get("/api/bot/price", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertEqual(body["mid_price"], "0.00123456")


# ---------------------------------------------------------------------------
# 3. /api/status — smoke contract (bot=None, no asset_id, startup not authorised)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestStatusEndpointSmoke(_FlaskBase):
    def setUp(self):
        super().setUp()
        # Patch get_wallet_type to avoid live wallet call
        self._wt_patcher = patch.object(
            api_server, "get_wallet_type", return_value="sage"
        )
        self._wt_patcher.start()
        # Clear active_cat so no TibetSwap/Dexie calls are made
        self._orig_cat = dict(api_server._active_cat)
        api_server._active_cat.clear()

    def tearDown(self):
        self._wt_patcher.stop()
        api_server._active_cat.update(self._orig_cat)
        super().tearDown()

    def test_returns_200_with_no_bot(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_is_dict(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        self.assertIsInstance(resp.get_json(), dict)

    def test_running_key_present(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("running", body)

    def test_stats_key_present(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("stats", body)

    def test_balances_key_present(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("balances", body)

    def test_offers_key_present(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("offers", body)

    def test_current_cat_key_present(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("current_cat", body)

    def test_running_false_when_no_bot(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body.get("running"))


# ---------------------------------------------------------------------------
# 4. Write-guard — POST without token returns 401 (before Flask method-check)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestStatusEndpointWriteGuards(_FlaskBase):
    """The before_request guard intercepts POST requests without a valid token
    and returns 401 — before Flask can return 405 for a GET-only route."""

    def test_bot_state_post_no_token_returns_401(self):
        resp = self.client.post("/api/bot/state", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 401)

    def test_bot_price_post_no_token_returns_401(self):
        resp = self.client.post("/api/bot/price", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 401)

    def test_status_post_no_token_returns_401(self):
        resp = self.client.post("/api/status", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 401)

    def test_bot_state_post_with_token_returns_405(self):
        """Valid token passes auth but Flask still rejects POST on GET-only route."""
        headers = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
        resp = self.client.post(
            "/api/bot/state", headers=headers, environ_base=self._LOOPBACK
        )
        self.assertEqual(resp.status_code, 405)


if __name__ == "__main__":
    unittest.main()
