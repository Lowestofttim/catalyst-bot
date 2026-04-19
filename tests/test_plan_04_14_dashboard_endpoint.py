"""Slice 04-14 — dashboard endpoint contract tests.

Tests GET /api/dashboard:
  - No auth required (read-only aggregator)
  - Returns 200 with all required top-level keys
  - bot=None returns safe empty shapes for bot-dependent fields
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


def _empty_spacescan():
    return {"enabled": False, "has_data": False, "holder_count": 0,
            "activity_level": "unknown", "risk_level": "unknown",
            "price_gap_bps": 0}


def _make_mock_db_conn():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = None
    mock_cur.fetchall.return_value = []
    mock_cur.__iter__ = MagicMock(return_value=iter([]))
    mock_conn.execute.return_value = mock_cur
    return mock_conn


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def _get_dashboard(self):
        fake_stats = {
            "realised_pnl_xch": "0", "total_fills": 0, "buy_fills": 0,
            "sell_fills": 0, "round_trips": 0, "win_rate": 0,
            "fill_rate_per_hour": 0, "avg_spread_capture": "0",
            "pending_verification_count": 0, "volume_xch": "0",
        }
        fake_summary = {"xch_free_count": 0, "cat_free_count": 0, "xch_total": 0, "cat_total": 0}
        with patch("database.get_stats", return_value=fake_stats), \
             patch("database.get_coin_summary", return_value=fake_summary), \
             patch("database.get_open_offers", return_value=[]), \
             patch("database.get_connection", return_value=_make_mock_db_conn()), \
             patch.object(api_server, "_get_spacescan_market_context",
                          return_value=_empty_spacescan()), \
             patch.object(api_server, "bot", None):
            return self.client.get("/api/dashboard",
                                   environ_base=self._LOOPBACK)


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestDashboard(_FlaskBase):

    def test_returns_200(self):
        resp = self._get_dashboard()
        self.assertEqual(resp.status_code, 200)

    def test_response_has_top_level_keys(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        for key in ("settings", "market_health", "wallet", "coins",
                    "performance", "current_cat", "links"):
            self.assertIn(key, body)

    def test_settings_has_trading_section(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIn("trading", body["settings"])
        self.assertIn("spreads", body["settings"])

    def test_market_health_has_status(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIn("status", body["market_health"])

    def test_wallet_has_balance_keys(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        wallet = body["wallet"]
        for key in ("xch_spendable", "xch_total", "cat_spendable", "cat_total"):
            self.assertIn(key, wallet)

    def test_coins_has_count_keys(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        coins = body["coins"]
        for key in ("xch_free", "xch_locked", "xch_total"):
            self.assertIn(key, coins)

    def test_links_has_dexie_orderbook(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIn("dexie_orderbook", body["links"])

    def test_current_cat_is_dict(self):
        resp = self._get_dashboard()
        body = resp.get_json()
        self.assertIsInstance(body["current_cat"], dict)


if __name__ == "__main__":
    unittest.main()
