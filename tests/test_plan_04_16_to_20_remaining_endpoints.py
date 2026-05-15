"""Slices 04-16 through 04-20 — remaining API endpoint contract tests.

Tests:
  04-16: GET /api/market/intel (market-intel)
  04-17: GET /api/spacescan/status, POST /api/spacescan/setup
  04-18: GET /api/fees/status
  04-19: Sniper stats embedded in /api/pnl (no dedicated endpoint)
  04-20: GET /api/risk/spreads (covered) + circuit-breaker via inventory
         (no separate CB reset endpoint exists)
"""

import os
import sys
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


def _make_bot():
    bot = MagicMock()
    bot.is_running.return_value = True
    bot.market_intel.refresh_orderbook.return_value = None
    bot.market_intel.get_market_summary.return_value = {
        "orderbook_refreshes": 1,
        "num_competitor_buys": 0,
        "num_competitor_sells": 0,
    }
    bot.market_intel.check_dbx_eligibility.return_value = {}
    bot.risk_manager.get_adjusted_spread.return_value = Decimal("0.003")
    bot.risk_manager.get_inventory_state.return_value = {
        "net_position_cat": "0",
        "circuit_breaker_active": False,
    }
    bot.price_engine.get_last_price.return_value = Decimal("0.001")
    bot.sniper.get_stats.return_value = {}
    # Additional attributes accessed by /api/market/intel
    bot.splash_manager.get_stats.return_value = {}
    bot.splash_manager.check_health.return_value = {}
    bot.splash_node.get_status.return_value = {}
    bot.get_splash_receive_stats.return_value = {}
    return bot


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


# ---------------------------------------------------------------------------
# 04-16: GET /api/market/intel
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestMarketIntel(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/market/intel", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def _get_intel(self):
        empty_sc = {"enabled": False, "has_data": False}
        fake_local = {
            "our_best_bid": Decimal("0"),
            "our_best_ask": Decimal("0"),
            "our_open_buys": 0,
            "our_open_sells": 0,
            "source": "db",
        }
        return self.client.get(
            "/api/market/intel",
            environ_base=self._LOOPBACK,
        )

    def test_returns_200_with_bot(self):
        empty_sc = {"enabled": False, "has_data": False}
        fake_local = {
            "our_best_bid": Decimal("0"),
            "our_best_ask": Decimal("0"),
            "our_open_buys": 0,
            "our_open_sells": 0,
            "source": "db",
        }
        with (
            patch.object(api_server, "bot", _make_bot()),
            patch("api_server._get_live_local_offer_edges", return_value=fake_local),
            patch("api_server._get_spacescan_market_context", return_value=empty_sc),
            patch("api_server._fetch_dbx_pair_status", return_value={}),
        ):
            resp = self.client.get("/api/market/intel", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_is_dict(self):
        empty_sc = {"enabled": False, "has_data": False}
        fake_local = {
            "our_best_bid": Decimal("0"),
            "our_best_ask": Decimal("0"),
            "our_open_buys": 0,
            "our_open_sells": 0,
            "source": "db",
        }
        with (
            patch.object(api_server, "bot", _make_bot()),
            patch("api_server._get_live_local_offer_edges", return_value=fake_local),
            patch("api_server._get_spacescan_market_context", return_value=empty_sc),
            patch("api_server._fetch_dbx_pair_status", return_value={}),
        ):
            resp = self.client.get("/api/market/intel", environ_base=self._LOOPBACK)
        self.assertIsInstance(resp.get_json(), dict)


# ---------------------------------------------------------------------------
# 04-17: GET /api/spacescan/status + POST /api/spacescan/setup
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSpacescanStatus(_FlaskBase):
    def test_returns_200(self):
        with patch("spacescan.get_api_stats", return_value={}):
            resp = self.client.get("/api/spacescan/status", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_configured_key(self):
        with patch("spacescan.get_api_stats", return_value={}):
            resp = self.client.get("/api/spacescan/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("configured", body)
        self.assertIn("tier", body)


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSpacescanSetup(_FlaskBase):
    def test_requires_token(self):
        resp = self._post("/api/spacescan/setup", {"api_key": ""}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_skip_returns_free_tier(self):
        with patch.object(api_server.cfg, "update"):
            resp = self._post("/api/spacescan/setup", {"skip": True})
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("tier"), "free")

    def test_clear_key_returns_success(self):
        with patch.object(api_server.cfg, "update"):
            resp = self._post("/api/spacescan/setup", {"api_key": ""})
        self.assertTrue(resp.get_json().get("success"))

    def test_invalid_body_returns_400(self):
        resp = self.client.post(
            "/api/spacescan/setup",
            data="not json",
            content_type="text/plain",
            headers=self.auth,
            environ_base=self._LOOPBACK,
        )
        self.assertIn(resp.status_code, (400, 415))


# ---------------------------------------------------------------------------
# 04-18: GET /api/fees/status
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestFeesStatus(_FlaskBase):
    def test_returns_200(self):
        with patch("api_server.get_fee_settings_snapshot", return_value={}):
            resp = self.client.get("/api/fees/status", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_success_key_true(self):
        with patch("api_server.get_fee_settings_snapshot", return_value={}):
            resp = self.client.get("/api/fees/status", environ_base=self._LOOPBACK)
        self.assertTrue(resp.get_json().get("success"))

    def test_no_auth_required(self):
        # GET endpoint — no token needed
        with patch("api_server.get_fee_settings_snapshot", return_value={}):
            resp = self.client.get("/api/fees/status", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 04-19: Sniper stats embedded in /api/pnl (verified via sniper key)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSniperViaPlnl(_FlaskBase):
    """Sniper stats are not a dedicated endpoint — they're in /api/pnl."""

    def _fake_stats(self):
        return {
            "realised_pnl_xch": "0",
            "total_fills": 0,
            "buy_fills": 0,
            "sell_fills": 0,
            "round_trips": 0,
            "win_rate": 0,
            "fill_rate_per_hour": 0,
            "avg_spread_capture": "0",
            "unmatched_buy_fills": 0,
            "unmatched_sell_fills": 0,
            "volume_xch": "0",
            "volume_cat": "0",
            "buy_volume_xch": "0",
            "buy_volume_cat": "0",
            "sell_volume_xch": "0",
            "sell_volume_cat": "0",
            "net_xch_flow": "0",
            "net_cat_flow": "0",
            "avg_fill_size_xch": "0",
            "avg_round_trip_secs": 0,
            "avg_pnl_per_trip_xch": "0",
        }

    def test_pnl_has_sniper_key(self):
        bot = _make_bot()
        with (
            patch.object(api_server, "bot", bot),
            patch("api_server.get_stats", return_value=self._fake_stats()),
        ):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("sniper", body)
        self.assertIsInstance(body["sniper"], dict)

    def test_sniper_stats_from_bot(self):
        bot = _make_bot()
        bot.sniper.get_stats.return_value = {"total_snipes": 5}
        with (
            patch.object(api_server, "bot", bot),
            patch("api_server.get_stats", return_value=self._fake_stats()),
        ):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertEqual(body["sniper"].get("total_snipes"), 5)


# ---------------------------------------------------------------------------
# 04-20: Circuit-breaker state (no dedicated CB endpoint; via /api/inventory)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCircuitBreakerViaInventory(_FlaskBase):
    """Circuit-breaker state is exposed through /api/inventory, not a dedicated endpoint."""

    def test_cb_false_when_inactive(self):
        bot = _make_bot()
        bot.risk_manager.get_inventory_state.return_value = {
            "net_position_cat": "0",
            "circuit_breaker_active": False,
            "circuit_breaker_reason": "",
        }
        with patch.object(api_server, "bot", bot):
            resp = self.client.get("/api/inventory", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body["circuit_breaker_active"])

    def test_cb_true_when_tripped(self):
        bot = _make_bot()
        bot.risk_manager.get_inventory_state.return_value = {
            "net_position_cat": "50",
            "circuit_breaker_active": True,
            "circuit_breaker_reason": "position_limit_exceeded",
        }
        with patch.object(api_server, "bot", bot):
            resp = self.client.get("/api/inventory", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body["circuit_breaker_active"])
        self.assertEqual(body["circuit_breaker_reason"], "position_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
