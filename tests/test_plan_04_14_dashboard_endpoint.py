"""Slice 04-14 — dashboard endpoint contract tests.

Tests GET /api/dashboard:
  - No auth required (read-only aggregator)
  - Returns 200 with all required top-level keys
  - bot=None returns safe empty shapes for bot-dependent fields
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    from blueprints import dashboard as dashboard_bp
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    dashboard_bp = None
    _SKIP = str(exc)


def _empty_spacescan():
    return {"enabled": False, "has_data": False, "holder_count": 0,
            "activity_level": "unknown", "risk_level": "unknown",
            "price_gap_bps": 0}


def _priced_spacescan():
    data = _empty_spacescan()
    data.update({
        "enabled": True,
        "has_data": True,
        "price_xch": "0.005",
        "price_usd": "0.0125",
    })
    return data


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

    def test_response_has_fiat_price_summary(self):
        fake_stats = {
            "realised_pnl_xch": "0", "total_fills": 0, "buy_fills": 0,
            "sell_fills": 0, "round_trips": 0, "win_rate": 0,
            "fill_rate_per_hour": 0, "avg_spread_capture": "0",
            "pending_verification_count": 0, "volume_xch": "0",
        }
        fake_summary = {"xch_free_count": 0, "cat_free_count": 0, "xch_total": 0, "cat_total": 0}
        price_result = {
            "has_data": True,
            "xch_usd": 2.5,
            "source": "spacescan",
            "fetched_at": 1777824000.0,
        }
        with patch("database.get_stats", return_value=fake_stats), \
             patch("database.get_coin_summary", return_value=fake_summary), \
             patch("database.get_open_offers", return_value=[]), \
             patch("database.get_connection", return_value=_make_mock_db_conn()), \
             patch("market_data_collector.get_cached_xch_usd_price",
                   return_value=price_result), \
             patch.object(api_server, "_get_spacescan_market_context",
                          return_value=_priced_spacescan()), \
             patch.object(api_server, "bot", None):
            resp = self.client.get("/api/dashboard",
                                   environ_base=self._LOOPBACK)

        self.assertEqual(resp.status_code, 200)
        fiat = resp.get_json()["fiat_prices"]
        self.assertEqual(fiat["xch_usd_price"], "2.5")
        self.assertEqual(fiat["xch_usd_source"], "spacescan")
        self.assertEqual(fiat["cat_usd_price"], "0.0125")
        self.assertEqual(fiat["cat_price_xch"], "0.005")
        self.assertEqual(fiat["cat_usd_source"], "spacescan")

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

    def test_market_health_uses_live_offer_edges_for_inner_spread(self):
        risk_manager = MagicMock()
        risk_manager.get_inventory_state.return_value = {}
        risk_manager.get_circuit_breaker_blocked_side.return_value = ""
        risk_manager.get_market_health.return_value = {
            "status": "green",
            "message": "ok",
            "conditions": [],
            "metrics": {
                "your_spread_bps": "1770.5",
                "buy_spread_bps": "798.4",
                "sell_spread_bps": "972.1",
            },
        }
        bot = MagicMock()
        bot.risk_manager = risk_manager
        bot._loop_count = 5
        bot._start_time = 0
        bot._bot_state = {"mid_price": "0.0001318526026886049206032406980"}
        bot._probe_state = {}
        bot.market_intel = None
        bot.coin_manager = None
        bot.sniper = None
        bot.boost_manager = None
        bot.price_engine.get_last_price.return_value = "0.0001318526026886049206032406980"

        fake_stats = {
            "realised_pnl_xch": "0", "total_fills": 0, "buy_fills": 0,
            "sell_fills": 0, "round_trips": 0, "win_rate": 0,
            "fill_rate_per_hour": 0, "avg_spread_capture": "0",
            "pending_verification_count": 0, "volume_xch": "0",
        }
        fake_summary = {"xch_free_count": 0, "cat_free_count": 0, "xch_total": 0, "cat_total": 0}
        live_edges = {
            "our_best_bid": api_server.Decimal("0.0001297758078408030669426051158"),
            "our_best_ask": api_server.Decimal("0.0001349368190860945260879005506"),
            "our_open_buys": 23,
            "our_open_sells": 23,
            "source": "wallet_sync",
        }

        with patch("database.get_stats", return_value=fake_stats), \
             patch("database.get_coin_summary", return_value=fake_summary), \
             patch("database.get_open_offers", return_value=[]), \
             patch("database.get_connection", return_value=_make_mock_db_conn()), \
             patch.object(api_server, "_get_spacescan_market_context",
                          return_value=_empty_spacescan()), \
             patch.object(api_server, "_get_live_local_offer_edges", return_value=live_edges), \
             patch.object(api_server, "_active_cat", {"asset_id": "aa" * 32, "wallet_id": 2, "decimals": 3}), \
             patch.object(api_server, "bot", bot):
            resp = self.client.get("/api/dashboard", environ_base=self._LOOPBACK)

        self.assertEqual(resp.status_code, 200)
        metrics = resp.get_json()["market_health"]["metrics"]
        self.assertEqual(metrics["our_best_bid"], str(live_edges["our_best_bid"]))
        self.assertEqual(metrics["our_best_ask"], str(live_edges["our_best_ask"]))
        expected_bps = (
            (live_edges["our_best_ask"] - live_edges["our_best_bid"])
            / api_server.Decimal(bot._bot_state["mid_price"])
            * api_server.Decimal("10000")
        )
        self.assertAlmostEqual(float(metrics["your_spread_bps"]), float(expected_bps), places=6)

    def test_cat_topup_pool_empty_recommendation_does_not_suggest_coin_prep(self):
        cfg = types.SimpleNamespace(
            TIER_ENABLED=True,
            ENABLE_SELL=True,
            SNIPER_ENABLED=True,
            SNIPER_PREP_COUNT=25,
            SNIPER_SIZE_XCH="0.001",
            SELL_INNER_TIER_SPARE_COUNT=8,
            SELL_MID_TIER_SPARE_COUNT=4,
            SELL_OUTER_TIER_SPARE_COUNT=5,
            SELL_EXTREME_TIER_SPARE_COUNT=2,
        )
        coins = {
            "tier_counts": {
                "enabled": True,
                "cat": {
                    "inner": 8,
                    "mid": 4,
                    "outer": 5,
                    "extreme": 2,
                    "sniper": 24,
                    "reserve": 0,
                    "dust": 0,
                },
                "xch": {},
            }
        }

        recs = dashboard_bp._build_coin_recommendations(cfg, coins, is_running=True)

        self.assertTrue(recs)
        rec = recs[0]
        self.assertEqual(rec["id"], "cat_topup_pool_empty")
        self.assertEqual(rec["action"], "reviewTopupPool")
        self.assertIn("CAT top-up pool", rec["title"])
        self.assertIn("allocate an incoming CAT coin", rec["message"])
        self.assertNotIn("Coin Prep", rec["message"])

    def test_shape_fix_coin_prep_halt_copy_is_explicitly_nuclear(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_gui.html"),
                  encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn(
            "Stop the bot, review Smart Settings, then run Coin Prep",
            html,
        )
        self.assertNotIn(
            "Could not produce tier-correct coins (run coin prep)",
            html,
        )
        self.assertIn("'reviewTopupPool'", html)
        self.assertIn("CAT top-up pool empty", html)

    def test_dashboard_has_fiat_price_display_hooks(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_gui.html"),
                  encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn('id="ccFiatPrices"', html)
        self.assertIn('id="ccXchUsdPrice"', html)
        self.assertIn('id="ccCatUsdPrice"', html)
        self.assertIn('id="snapshotXchUsd"', html)
        self.assertIn('id="snapshotCatUsd"', html)
        self.assertIn("updateFiatPriceSummary", html)

    def test_status_sync_requests_dashboard_fiat_prices_after_pair_loads(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_gui.html"),
                  encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn("function ensureFiatPricesFromDashboard", html)
        status_sync = html.split("function syncCommandCentreFromStatus(data)")[1]
        status_sync = status_sync.split("let _sseConnection")[0]
        self.assertIn("ensureFiatPricesFromDashboard", status_sync)

    def test_fiat_price_summary_clears_snapshot_fields_when_prices_missing(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "bot_gui.html"),
                  encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn("function resetFiatPriceSummary", html)
        summary = html.split("function updateFiatPriceSummary")[1]
        summary = summary.split("function updateCommandCentre")[0]
        self.assertIn("resetFiatPriceSummary(currentCat);", summary)

        reset = html.split("function resetFiatPriceSummary")[1]
        reset = reset.split("function updateFiatPriceSummary")[0]
        for field_id in (
            "ccXchUsdPrice",
            "ccCatUsdPrice",
            "ccFiatPriceSource",
            "snapshotXchUsd",
            "snapshotCatUsd",
            "snapshotCatUsdLabel",
        ):
            self.assertIn(field_id, reset)


if __name__ == "__main__":
    unittest.main()
