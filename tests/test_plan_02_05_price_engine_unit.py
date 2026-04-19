"""Slice 02-05 — price_engine.py unit tests: pure math, EMA, guards.

No existing tests. Covers:
  _decimal_sqrt, _update_reference_price, get_dynamic_limits,
  _apply_safety_guards (dynamic / hard / step), pricing strategy selection,
  arb gap calculation, constant product slippage, get_pool_depth_ratio.
"""
import unittest
from decimal import Decimal
from unittest.mock import patch

try:
    import price_engine as _pe_mod
    from price_engine import PriceEngine, _decimal_sqrt
    _SKIP = None
except ModuleNotFoundError as exc:
    _pe_mod = None
    PriceEngine = None
    _decimal_sqrt = None
    _SKIP = str(exc)


def _make_engine():
    """Return a fresh PriceEngine with no prior price history."""
    return PriceEngine()


class _CfgPatch:
    """Context manager that patches price_engine.cfg with a minimal fake."""
    DYNAMIC_LIMIT_PCT = Decimal("10")
    HARD_MIN_PRICE_XCH = Decimal("0")
    HARD_MAX_PRICE_XCH = Decimal("0")
    MAX_STEP_CHANGE_FRACTION = Decimal("0.15")
    ARB_ALERT_THRESHOLD_BPS = Decimal("200")
    TIBET_WEIGHT = Decimal("0.85")
    PRICE_STRATEGY = "weighted"
    CAT_ASSET_ID = "abc123"
    CAT_DECIMALS = 3
    CAT_TICKER_ID = "MZ"
    PRICE_LIMIT_NUDGE_ALPHA = Decimal("0.02")


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestDecimalSqrt(unittest.TestCase):
    """_decimal_sqrt — Newton's method square root."""

    def test_perfect_square(self):
        result = _decimal_sqrt(Decimal("4"))
        self.assertAlmostEqual(float(result), 2.0, places=10)

    def test_non_perfect_square(self):
        result = _decimal_sqrt(Decimal("2"))
        self.assertAlmostEqual(float(result), 1.4142135623730951, places=8)

    def test_zero(self):
        self.assertEqual(_decimal_sqrt(Decimal("0")), Decimal("0"))

    def test_one(self):
        self.assertAlmostEqual(float(_decimal_sqrt(Decimal("1"))), 1.0, places=10)

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            _decimal_sqrt(Decimal("-1"))

    def test_large_value(self):
        result = _decimal_sqrt(Decimal("1000000"))
        self.assertAlmostEqual(float(result), 1000.0, places=6)


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestUpdateReferencePrice(unittest.TestCase):
    """_update_reference_price — EMA tracking."""

    def setUp(self):
        self._p = patch.object(_pe_mod, "cfg", _CfgPatch())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_first_price_sets_reference_directly(self):
        eng = _make_engine()
        eng._update_reference_price(Decimal("1.10"))
        self.assertEqual(eng._reference_price, Decimal("1.10"))

    def test_ema_nudges_toward_new_price(self):
        eng = _make_engine()
        eng._update_reference_price(Decimal("1.00"))
        # Use a 1% move — below half_band×0.5 threshold (2.5%), so normal alpha=0.01 applies
        eng._update_reference_price(Decimal("1.01"))
        # ref = 1.00 × 0.99 + 1.01 × 0.01 = 0.99 + 0.0101 = 1.0001
        expected = Decimal("1.00") * Decimal("0.99") + Decimal("1.01") * Decimal("0.01")
        self.assertAlmostEqual(float(eng._reference_price), float(expected), places=6)

    def test_fast_catchup_applied_on_large_deviation(self):
        eng = _make_engine()
        eng._update_reference_price(Decimal("1.00"))
        # Move 20% up — half-band at 10% DYNAMIC_LIMIT_PCT is 5%; 20% > 5%*0.5 → fast alpha
        ref_before = eng._reference_price
        eng._update_reference_price(Decimal("1.20"))
        ref_after = eng._reference_price
        # Fast alpha (5× = 0.05 capped at 0.10): reference should move more than 0.01 per step
        normal_move = float(ref_after - ref_before)
        # With normal alpha (0.01), move would be 0.20 * 0.01 = 0.002
        self.assertGreater(abs(normal_move), 0.002)

    def test_reference_monotonically_approaches_constant_price(self):
        eng = _make_engine()
        eng._update_reference_price(Decimal("1.00"))
        for _ in range(10):
            eng._update_reference_price(Decimal("2.00"))
        # After 10 steps, reference should have moved toward 2.00
        self.assertGreater(eng._reference_price, Decimal("1.00"))


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestGetDynamicLimits(unittest.TestCase):
    """get_dynamic_limits — band calculation around reference price."""

    def setUp(self):
        self._p = patch.object(_pe_mod, "cfg", _CfgPatch())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_no_reference_returns_none_pair(self):
        eng = _make_engine()
        lo, hi = eng.get_dynamic_limits()
        self.assertIsNone(lo)
        self.assertIsNone(hi)

    def test_band_is_symmetric(self):
        eng = _make_engine()
        eng._reference_price = Decimal("1.00")
        lo, hi = eng.get_dynamic_limits()
        self.assertAlmostEqual(float(hi - Decimal("1.00")), float(Decimal("1.00") - lo), places=8)

    def test_band_width_matches_pct(self):
        eng = _make_engine()
        eng._reference_price = Decimal("1.00")
        lo, hi = eng.get_dynamic_limits()
        # 10% of 1.00 = 0.10
        self.assertAlmostEqual(float(hi - lo), 0.20, places=8)

    def test_zero_pct_returns_none_pair(self):
        cfg_patch = _CfgPatch()
        cfg_patch.DYNAMIC_LIMIT_PCT = Decimal("0")
        with patch.object(_pe_mod, "cfg", cfg_patch):
            eng = _make_engine()
            eng._reference_price = Decimal("1.00")
            lo, hi = eng.get_dynamic_limits()
        self.assertIsNone(lo)
        self.assertIsNone(hi)


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestApplySafetyGuards(unittest.TestCase):
    """_apply_safety_guards — dynamic/hard/step rejection logic."""

    def setUp(self):
        self._p = patch.object(_pe_mod, "cfg", _CfgPatch())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _eng_with_ref(self, ref_price: str) -> PriceEngine:
        eng = _make_engine()
        eng._reference_price = Decimal(ref_price)
        return eng

    def test_price_within_band_passes(self):
        eng = self._eng_with_ref("1.00")
        result = eng._apply_safety_guards(Decimal("1.05"))
        self.assertEqual(result, Decimal("1.05"))

    def test_price_below_dynamic_min_rejected(self):
        eng = self._eng_with_ref("1.00")
        # 10% band: min=0.90, max=1.10
        result = eng._apply_safety_guards(Decimal("0.85"))
        self.assertIsNone(result)
        self.assertEqual(eng._last_rail_breach, "below")
        self.assertEqual(eng._last_rail_breach_kind, "dyn_min")

    def test_price_above_dynamic_max_rejected(self):
        eng = self._eng_with_ref("1.00")
        result = eng._apply_safety_guards(Decimal("1.15"))
        self.assertIsNone(result)
        self.assertEqual(eng._last_rail_breach, "above")
        self.assertEqual(eng._last_rail_breach_kind, "dyn_max")

    def test_hard_min_rejects_below(self):
        cfg_patch = _CfgPatch()
        cfg_patch.DYNAMIC_LIMIT_PCT = Decimal("0")  # disable dynamic limits
        cfg_patch.HARD_MIN_PRICE_XCH = Decimal("0.50")
        with patch.object(_pe_mod, "cfg", cfg_patch):
            eng = _make_engine()
            result = eng._apply_safety_guards(Decimal("0.40"))
        self.assertIsNone(result)
        self.assertEqual(eng._last_rail_breach_kind, "hard_min")

    def test_hard_max_rejects_above(self):
        cfg_patch = _CfgPatch()
        cfg_patch.DYNAMIC_LIMIT_PCT = Decimal("0")
        cfg_patch.HARD_MAX_PRICE_XCH = Decimal("2.00")
        with patch.object(_pe_mod, "cfg", cfg_patch):
            eng = _make_engine()
            result = eng._apply_safety_guards(Decimal("2.50"))
        self.assertIsNone(result)
        self.assertEqual(eng._last_rail_breach_kind, "hard_max")

    def test_step_change_rejects_jump(self):
        cfg_patch = _CfgPatch()
        cfg_patch.DYNAMIC_LIMIT_PCT = Decimal("0")  # no dynamic limits
        cfg_patch.MAX_STEP_CHANGE_FRACTION = Decimal("0.05")
        with patch.object(_pe_mod, "cfg", cfg_patch):
            eng = _make_engine()
            eng._last_mid_price = Decimal("1.00")
            result = eng._apply_safety_guards(Decimal("1.20"))  # 20% jump > 5%
        self.assertIsNone(result)
        self.assertEqual(eng._last_rail_breach_kind, "step")

    def test_step_within_limit_passes(self):
        cfg_patch = _CfgPatch()
        cfg_patch.DYNAMIC_LIMIT_PCT = Decimal("0")
        cfg_patch.MAX_STEP_CHANGE_FRACTION = Decimal("0.15")
        with patch.object(_pe_mod, "cfg", cfg_patch):
            eng = _make_engine()
            eng._last_mid_price = Decimal("1.00")
            result = eng._apply_safety_guards(Decimal("1.10"))  # 10% < 15%
        self.assertEqual(result, Decimal("1.10"))

    def test_no_reference_no_dynamic_limits(self):
        eng = _make_engine()
        # reference_price=None → dynamic limits disabled → price passes if no hard limits
        result = eng._apply_safety_guards(Decimal("1.00"))
        self.assertEqual(result, Decimal("1.00"))

    def test_successful_price_clears_breach_state(self):
        eng = self._eng_with_ref("1.00")
        eng._apply_safety_guards(Decimal("0.80"))  # fails
        self.assertIsNotNone(eng._last_rail_breach)
        eng._apply_safety_guards(Decimal("1.05"))  # passes
        self.assertIsNone(eng._last_rail_breach)


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestPricingStrategySelection(unittest.TestCase):
    """Strategy selection logic in get_price() — patching fetch methods."""

    def setUp(self):
        self._p = patch.object(_pe_mod, "cfg", _CfgPatch())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _eng_with_prices(self, dexie, tibet):
        eng = _make_engine()
        eng._fetch_dexie_price = lambda *a, **kw: (Decimal(dexie) if dexie else None)
        eng._fetch_tibet_price = lambda *a, **kw: (Decimal(tibet) if tibet else None)
        # Disable safety guards for strategy tests
        eng._apply_safety_guards = lambda p: p
        # Disable DB side-effects
        eng._update_reference_price = lambda p: None
        with patch("database.record_price", return_value=None):
            return eng

    def test_weighted_strategy_blends_prices(self):
        eng = self._eng_with_prices("1.00", "1.20")
        with patch("database.record_price"):
            result = eng.get_price()
        expected = Decimal("1.00") * Decimal("0.15") + Decimal("1.20") * Decimal("0.85")
        self.assertAlmostEqual(float(result["mid_price"]), float(expected), places=6)

    def test_dexie_only_when_tibet_unavailable(self):
        eng = self._eng_with_prices("1.05", None)
        with patch("database.record_price"):
            result = eng.get_price()
        self.assertEqual(result["strategy_used"], "dexie_only")
        self.assertEqual(result["mid_price"], Decimal("1.05"))

    def test_tibet_only_when_dexie_unavailable(self):
        cfg_patch = _CfgPatch()
        cfg_patch.PRICE_STRATEGY = "tibet_only"
        eng = _make_engine()
        eng._fetch_dexie_price = lambda *a, **kw: None
        eng._fetch_tibet_price = lambda *a, **kw: Decimal("1.15")
        eng._apply_safety_guards = lambda p: p
        eng._update_reference_price = lambda p: None
        with patch.object(_pe_mod, "cfg", cfg_patch), patch("database.record_price"):
            result = eng.get_price()
        self.assertEqual(result["strategy_used"], "tibet_only")

    def test_both_unavailable_returns_none(self):
        eng = _make_engine()
        eng._fetch_dexie_price = lambda *a, **kw: None
        eng._fetch_tibet_price = lambda *a, **kw: None
        eng._apply_safety_guards = lambda p: p
        with patch("database.record_price"):
            result = eng.get_price()
        self.assertIsNone(result)

    def test_arb_gap_calculated_in_bps(self):
        eng = self._eng_with_prices("1.00", "1.05")
        with patch("database.record_price"):
            result = eng.get_price()
        expected_gap = abs(Decimal("1.00") - Decimal("1.05")) / Decimal("1.00") * Decimal("10000")
        self.assertAlmostEqual(float(result["arb_gap_bps"]), float(expected_gap), places=4)

    def test_no_arb_when_prices_equal(self):
        eng = self._eng_with_prices("1.00", "1.00")
        with patch("database.record_price"):
            result = eng.get_price()
        self.assertEqual(result["arb_gap_bps"], Decimal("0"))
        self.assertIsNone(result["arb_opportunity"])


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestConstantProductFormula(unittest.TestCase):
    """_estimate_slippage_from_reserves — constant product AMM math.

    pair dict uses mojos: xch_reserve in mojos (÷1e12 for XCH),
    token_reserve in token-mojos (÷10^CAT_DECIMALS for CAT units).
    """

    def setUp(self):
        self._p = patch.object(_pe_mod, "cfg", _CfgPatch())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _pair(self, xch_xch=1000, cat_units=500000):
        """Build a pair dict from human-readable pool sizes."""
        return {
            "xch_reserve": int(xch_xch * 1e12),    # mojos
            "token_reserve": cat_units * 1000,      # token-mojos (decimals=3)
        }

    def test_buy_side_has_positive_slippage(self):
        eng = _make_engine()
        result = eng._estimate_slippage_from_reserves(self._pair(), Decimal("10"), "buy")
        self.assertIsNotNone(result)
        self.assertGreater(float(result["slippage_bps"]), 0)

    def test_sell_side_has_positive_slippage(self):
        eng = _make_engine()
        result = eng._estimate_slippage_from_reserves(self._pair(), Decimal("10"), "sell")
        self.assertIsNotNone(result)
        self.assertGreater(float(result["slippage_bps"]), 0)

    def test_larger_trade_has_more_slippage(self):
        eng = _make_engine()
        small = eng._estimate_slippage_from_reserves(self._pair(), Decimal("1"), "buy")
        large = eng._estimate_slippage_from_reserves(self._pair(), Decimal("100"), "buy")
        self.assertGreater(
            float(large["slippage_bps"]),
            float(small["slippage_bps"]),
        )

    def test_zero_reserves_returns_none(self):
        eng = _make_engine()
        pair = {"xch_reserve": 0, "token_reserve": 0}
        result = eng._estimate_slippage_from_reserves(pair, Decimal("10"), "buy")
        self.assertIsNone(result)

    def test_result_has_required_keys(self):
        eng = _make_engine()
        result = eng._estimate_slippage_from_reserves(self._pair(), Decimal("1"), "buy")
        for key in ("input_amount", "output_amount", "effective_price", "slippage_bps"):
            self.assertIn(key, result)


@unittest.skipIf(_SKIP is not None, f"price_engine unavailable: {_SKIP}")
class TestPoolDepthRatio(unittest.TestCase):
    """get_pool_depth_ratio — trade size vs pool depth."""

    def setUp(self):
        self._p = patch.object(_pe_mod, "cfg", _CfgPatch())
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_ratio_is_trade_over_depth(self):
        eng = _make_engine()
        eng.get_tibet_pool_info = lambda: {"xch_reserve": Decimal("1000")}
        ratio = eng.get_pool_depth_ratio(Decimal("10"))
        self.assertAlmostEqual(float(ratio), 0.01, places=6)

    def test_no_pool_returns_zero(self):
        eng = _make_engine()
        eng.get_tibet_pool_info = lambda: None
        ratio = eng.get_pool_depth_ratio(Decimal("10"))
        self.assertEqual(ratio, Decimal("0"))

    def test_zero_depth_returns_one(self):
        eng = _make_engine()
        eng.get_tibet_pool_info = lambda: {"xch_reserve": Decimal("0")}
        ratio = eng.get_pool_depth_ratio(Decimal("10"))
        self.assertEqual(ratio, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
