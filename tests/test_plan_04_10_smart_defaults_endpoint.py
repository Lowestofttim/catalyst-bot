"""Slice 04-10 — smart-defaults endpoint contract tests.

Tests GET /api/smart-defaults:
  - No auth required (read-only)
  - liquidity_mode parameter routing (two_sided/buy_only/sell_only)
  - Invalid liquidity_mode falls back to two_sided
  - risk_profile parameter forwarded
  - Exception path returns 500
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    from flask import jsonify as _jsonify
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


def _fake_defaults_response(**kwargs):
    """Return a Flask Response mimicking _calculate_smart_defaults output."""
    with api_server.app.app_context():
        return _jsonify({
            "success": True,
            "spread_bps": 200,
            "default_trade_xch": "0.5",
            "liquidity_mode": kwargs.get("liquidity_mode", "two_sided"),
            "risk_profile": kwargs.get("risk_profile", "balanced"),
        })


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSmartDefaults(_FlaskBase):

    def test_returns_200(self):
        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=_fake_defaults_response):
            resp = self.client.get("/api/smart-defaults",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_default_liquidity_mode_two_sided(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get("/api/smart-defaults",
                            environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("liquidity_mode"), "two_sided")

    def test_buy_only_mode_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get("/api/smart-defaults?liquidity_mode=buy_only",
                            environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("liquidity_mode"), "buy_only")

    def test_sell_only_mode_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get("/api/smart-defaults?liquidity_mode=sell_only",
                            environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("liquidity_mode"), "sell_only")

    def test_invalid_liquidity_mode_falls_back_to_two_sided(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get("/api/smart-defaults?liquidity_mode=invalid_mode",
                            environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("liquidity_mode"), "two_sided")

    def test_risk_profile_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get("/api/smart-defaults?risk_profile=conservative",
                            environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("risk_profile"), "conservative")

    def test_exception_returns_500(self):
        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=Exception("market data unavailable")):
            resp = self.client.get("/api/smart-defaults",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertIn("error", body)

    def test_reserve_params_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get(
                "/api/smart-defaults?xch_reserve=0.5&cat_reserve=100",
                environ_base=self._LOOPBACK,
            )
        self.assertEqual(str(captured.get("xch_reserve")), "0.5")
        self.assertEqual(str(captured.get("cat_reserve")), "100")

    def test_selected_cat_params_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get(
                "/api/smart-defaults?"
                "asset_id=abc123&cat_wallet_id=7&cat_decimals=5&"
                "cat_ticker_id=FOO_XCH&cat_name=Foo",
                environ_base=self._LOOPBACK,
            )

        self.assertEqual(captured.get("asset_id"), "abc123")
        self.assertEqual(captured.get("cat_wallet_id"), 7)
        self.assertEqual(captured.get("cat_decimals"), 5)
        self.assertEqual(captured.get("cat_ticker_id"), "FOO_XCH")
        self.assertEqual(captured.get("cat_name"), "Foo")

    def test_zero_cat_decimals_are_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return _fake_defaults_response(**kwargs)

        with patch.object(api_server, "_calculate_smart_defaults",
                          side_effect=capture):
            self.client.get(
                "/api/smart-defaults?asset_id=abc123&cat_decimals=0",
                environ_base=self._LOOPBACK,
            )

        self.assertEqual(captured.get("cat_decimals"), 0)


class TestSmartDefaultsSourceContract(unittest.TestCase):

    def test_tibet_shock_trigger_derives_from_inner_edge(self):
        from blueprints.smart_defaults import _smart_tibet_shock_trigger_pct

        self.assertEqual(_smart_tibet_shock_trigger_pct(324), 1.62)
        self.assertEqual(_smart_tibet_shock_trigger_pct(50), 0.5)

    def test_price_resolver_uses_orderbook_when_ticker_and_tibet_missing(self):
        from blueprints.smart_defaults import _resolve_smart_mid_price

        messages = []
        resolved = _resolve_smart_mid_price(
            ticker={},
            tibet={},
            spacescan={},
            trades={},
            orderbook={"best_bid": 0.90, "best_ask": 1.10},
            messages=messages,
        )

        self.assertEqual(resolved["mid_price"], 1.0)
        self.assertEqual(resolved["dexie_price"], 1.0)
        self.assertEqual(resolved["price_source"], "dexie_orderbook")
        self.assertIn("Dexie orderbook", messages[0])

    def test_price_resolver_uses_trade_vwap_as_last_resort(self):
        from blueprints.smart_defaults import _resolve_smart_mid_price

        messages = []
        resolved = _resolve_smart_mid_price(
            ticker={},
            tibet={},
            spacescan={},
            trades={"trades": [
                {"price": 2.0, "xch_amount": 1.0},
                {"price": 4.0, "xch_amount": 3.0},
                {"price": 10.0, "xch_amount": 0.0},
            ]},
            orderbook={},
            messages=messages,
        )

        self.assertEqual(resolved["mid_price"], 3.5)
        self.assertEqual(resolved["dexie_price"], 3.5)
        self.assertEqual(resolved["price_source"], "dexie_trade_vwap")
        self.assertIn("Dexie trade VWAP", messages[0])

    def test_response_contract_includes_safety_fields(self):
        root = Path(__file__).resolve().parents[1]
        src = (root / "src" / "catalyst" / "blueprints" / "smart_defaults.py").read_text(encoding="utf-8")
        result_block = src.split("    result = {\n        # Smart Pricing", 1)[1].split(
            'print(f"[SMART_DEFAULTS v2]', 1
        )[0]

        self.assertIn('"tibet_shock_cancel_trigger_pct"', result_block)
        self.assertIn('"arb_alert_threshold_bps"', result_block)
        self.assertIn('"market_toxicity_enabled"', result_block)
        self.assertIn('"toxicity_protection_level"', result_block)
        self.assertIn('"toxicity_max_spread_multiplier"', result_block)
        self.assertIn('"toxicity_throttle_secs"', result_block)

    def test_frontend_has_safety_fields_and_matches_backend_keys(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn('id="configTibetShockCancelPct"', html)
        self.assertIn('id="configArbThreshold"', html)
        self.assertIn("data.tibet_shock_cancel_trigger_pct", html)
        self.assertIn("data.arb_alert_threshold_bps", html)
        self.assertNotIn("data.arb_threshold_bps", html)
        self.assertIn("'ARB_ALERT_THRESHOLD_BPS':    'configArbThreshold'", html)

    def test_frontend_has_market_toxicity_settings_and_save_mapping(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "bot_gui.html").read_text(encoding="utf-8")

        for field_id in (
            "configMarketToxicityEnabled",
            "configToxicityProtectionLevel",
            "configToxicityMaxSpreadMultiplier",
            "configToxicityThrottleSecs",
        ):
            self.assertIn(f'id="{field_id}"', html)
            self.assertIn(f"'{field_id}'", html)

        self.assertIn("data.market_toxicity_enabled", html)
        self.assertIn("data.toxicity_protection_level", html)
        self.assertIn("market_toxicity_enabled:", html)
        self.assertIn("toxicity_protection_level:", html)
        self.assertIn("toxicity_max_spread_multiplier:", html)
        self.assertIn("toxicity_throttle_secs:", html)

    def test_smart_toxicity_defaults_defensive_for_small_thin_one_sided_wallet(self):
        from blueprints.smart_defaults import _smart_toxicity_defaults

        rec = _smart_toxicity_defaults(
            avail_xch=1.5,
            avail_cat=500,
            liquidity_mode="buy_only",
            risk_level="thin",
            activity_level="quiet",
            fills_per_day=0.2,
            daily_volume=0.05,
            regime="volatile",
            arb_gap_bps=350,
            orderbook={"has_data": True, "num_buy_offers": 1, "num_sell_offers": 1},
        )

        self.assertTrue(rec["market_toxicity_enabled"])
        self.assertEqual(rec["toxicity_protection_level"], "defensive")
        self.assertLessEqual(rec["toxicity_throttle_start"], 65)
        self.assertEqual(rec["toxicity_min_throttle_signals"], 1)
        self.assertEqual(rec["toxicity_cancel_enabled"], False)

    def test_smart_toxicity_defaults_gentle_for_deep_healthy_market(self):
        from blueprints.smart_defaults import _smart_toxicity_defaults

        rec = _smart_toxicity_defaults(
            avail_xch=50,
            avail_cat=1_000_000,
            liquidity_mode="two_sided",
            risk_level="healthy",
            activity_level="active",
            fills_per_day=12,
            daily_volume=15,
            regime="normal",
            arb_gap_bps=20,
            orderbook={"has_data": True, "num_buy_offers": 30, "num_sell_offers": 28},
        )

        self.assertEqual(rec["toxicity_protection_level"], "gentle")
        self.assertGreaterEqual(rec["toxicity_throttle_start"], 85)
        self.assertLessEqual(rec["toxicity_max_spread_multiplier"], 1.5)

    def test_frontend_sends_selected_cat_to_smart_settings(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "bot_gui.html").read_text(encoding="utf-8")
        smart_block = html.split("async function getSmartDefaults()", 1)[1].split(
            "const resp = await apiFetch(`${API_URL}/smart-defaults?${params}`);", 1
        )[0]

        for key in (
            "asset_id",
            "cat_wallet_id",
            "cat_decimals",
            "cat_ticker_id",
            "cat_name",
        ):
            self.assertIn(key, smart_block)
        self.assertIn("selectedCAT.decimals ?? 3", smart_block)
        self.assertIn("currentCAT = { ...selectedCAT }", smart_block)

    def test_single_sided_smart_defaults_disable_inventory_management(self):
        root = Path(__file__).resolve().parents[1]
        src = (root / "src" / "catalyst" / "blueprints" / "smart_defaults.py").read_text(
            encoding="utf-8"
        )
        buy_block = src.split('if liquidity_mode == "buy_only":', 1)[1].split(
            'elif liquidity_mode == "sell_only":', 1
        )[0]
        sell_block = src.split('elif liquidity_mode == "sell_only":', 1)[1].split(
            "# ── UNIVERSAL MAX_POSITION_XCH", 1
        )[0]

        self.assertIn('result["inventory_enabled"] = False', buy_block)
        self.assertIn('result["inventory_enabled"] = False', sell_block)

    def test_frontend_smart_settings_watches_safety_fields(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "bot_gui.html").read_text(encoding="utf-8")
        watched = html.split("const SMART_SETTINGS_WATCHED_INPUTS = [", 1)[1].split("];", 1)[0]

        for field_id in (
            "configBaseSpreadBps",
            "configMinEdgeBps",
            "configRequoteBps",
            "configRequoteCooldown",
            "configDynamicLimitPct",
            "configMaxStepChange",
            "configTibetShockCancelPct",
            "configArbThreshold",
            "configCompetitorEnabled",
            "configDbxMaxSpreadBps",
            "configTopupPoolXch",
            "configTopupPoolCat",
            "configBuyInnerSizeXch",
            "configBuyExtremeSizeXch",
            "configSniperRearmPriceMovePct",
            "configSniperRearmGapMovePct",
            "configTransactionFeeTargetSecs",
            "configSplashEnabled",
            "configCoinPrepEnabled",
            "configRuntimeCoinHealth",
            "configSageChangeAddress",
        ):
            self.assertIn(field_id, watched)

    def test_frontend_reserve_advisor_preserves_zero_reserve(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn("function readDashboardReserve", html)
        self.assertIn("readDashboardReserve(safety.xch_reserve)", html)
        self.assertIn("readDashboardReserve(safety.cat_reserve)", html)
        self.assertNotIn("parseFloat(safety.xch_reserve) || 25", html)
        self.assertNotIn("parseFloat(safety.cat_reserve) || 25", html)

    def test_frontend_reserve_action_scrolls_to_reserve_section(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn('id="settings-section-reserves"', html)
        self.assertIn("getElementById('settings-section-reserves')", html)


class TestSmartDefaultsSmallWalletSizing(unittest.TestCase):

    def test_small_wallet_position_limit_has_no_five_xch_floor(self):
        from blueprints.smart_defaults import _smart_initial_max_position

        self.assertEqual(_smart_initial_max_position(1, 0, "healthy"), 0.4)
        self.assertEqual(_smart_initial_max_position(5, 0, "healthy"), 2.0)
        self.assertEqual(_smart_initial_max_position(5, 0, "moderate"), 1.5)
        self.assertLess(_smart_initial_max_position(5, 0, "healthy"), 5.0)

    def test_stale_trade_size_floor_is_capped_by_small_wallet(self):
        from blueprints.smart_defaults import _smart_initial_max_position

        self.assertLessEqual(_smart_initial_max_position(1, 0.5, "healthy"), 0.5)
        self.assertLessEqual(_smart_initial_max_position(5, 1.0, "healthy"), 2.5)

    def test_large_wallet_position_limit_keeps_existing_shape(self):
        from blueprints.smart_defaults import _smart_initial_max_position

        self.assertEqual(_smart_initial_max_position(100, 0, "healthy"), 40.0)
        self.assertEqual(_smart_initial_max_position(100, 0, "thin"), 20.0)

    def test_small_wallet_prep_counts_are_scaled_down(self):
        from blueprints.smart_defaults import (
            _smart_fee_prep_count,
            _smart_sniper_prep_plan,
        )

        self.assertLessEqual(_smart_fee_prep_count(1, 0.001), 10)

        self.assertLessEqual(_smart_fee_prep_count(5, 0.001), 20)

        one_xch_sniper = _smart_sniper_prep_plan(1, fills_per_day=12, sniper_size_xch=0.01)
        self.assertLessEqual(one_xch_sniper["count"], 2)
        self.assertLessEqual(one_xch_sniper["pool_xch"], 0.02)

        sniper = _smart_sniper_prep_plan(5, fills_per_day=12, sniper_size_xch=0.01)
        self.assertLessEqual(sniper["count"], 12)
        self.assertLessEqual(sniper["pool_xch"], 0.12)


if __name__ == "__main__":
    unittest.main()
