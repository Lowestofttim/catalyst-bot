"""Slice 04-15 — inventory + risk endpoints contract tests.

Tests GET /api/inventory and GET /api/risk/spreads:
  - bot=None → 500
  - Response shapes and required keys
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
    bot.risk_manager.get_inventory_state.return_value = {
        "net_position_cat": "0",
        "circuit_breaker_active": False,
        "circuit_breaker_reason": "",
        "buy_fills": 0,
        "sell_fills": 0,
    }
    bot.risk_manager.get_adjusted_spread.return_value = Decimal("0.003")
    return bot


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()


# ---------------------------------------------------------------------------
# 1. GET /api/inventory
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestInventory(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/inventory", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_returns_200_with_bot(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/inventory", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_net_position(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/inventory", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("net_position_cat", body)

    def test_response_has_circuit_breaker(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/inventory", environ_base=self._LOOPBACK)
        self.assertIn("circuit_breaker_active", resp.get_json())


# ---------------------------------------------------------------------------
# 2. GET /api/risk/spreads
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestRiskSpreads(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/risk/spreads", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_returns_200_with_bot(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/risk/spreads", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_spread_keys(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/risk/spreads", environ_base=self._LOOPBACK)
        body = resp.get_json()
        for key in (
            "buy_spread_bps",
            "sell_spread_bps",
            "buy_spread_pct",
            "sell_spread_pct",
            "dynamic_enabled",
            "inventory_enabled",
        ):
            self.assertIn(key, body)

    def test_get_adjusted_spread_called_for_both_sides(self):
        bot = _make_bot()
        with patch.object(api_server, "bot", bot):
            self.client.get("/api/risk/spreads", environ_base=self._LOOPBACK)
        calls = [str(c) for c in bot.risk_manager.get_adjusted_spread.call_args_list]
        self.assertTrue(any("buy" in c for c in calls))
        self.assertTrue(any("sell" in c for c in calls))


if __name__ == "__main__":
    unittest.main()
