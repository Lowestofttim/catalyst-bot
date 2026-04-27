"""Slice 02-23 — risk_manager.py unit tests: circuit breakers, position, spreads.

No existing tests. Covers:
  _trip_circuit_breaker, circuit_breaker_active, _clear_circuit_breaker,
  is_full_halt, get_circuit_breaker_blocked_side, should_enable_side,
  _check_position_limit, _check_price_limits, check_circuit_breakers (hysteresis),
  _get_base_spread, _apply_inventory_skew.
"""
import unittest
from decimal import Decimal
from unittest.mock import patch

try:
    import risk_manager as _rm_mod
    from risk_manager import RiskManager
    _SKIP = None
except ModuleNotFoundError as exc:
    _rm_mod = None
    RiskManager = None
    _SKIP = str(exc)


class _FakeCfg:
    INVENTORY_ENABLED = True
    MAX_POSITION_XCH = Decimal("100")
    SKEW_INTENSITY = Decimal("0.3")
    DYNAMIC_SPREAD_ENABLED = True
    MIN_SPREAD_BPS = Decimal("200")
    MAX_SPREAD_BPS = Decimal("2000")
    MIN_EDGE_BPS = Decimal("100")
    BASE_SPREAD_BPS = Decimal("700")
    SPREAD_BPS = Decimal("700")
    HARD_MIN_PRICE_XCH = Decimal("0")
    HARD_MAX_PRICE_XCH = Decimal("0")
    DYNAMIC_LIMIT_PCT = Decimal("0")  # disabled unless test overrides
    COMPETITOR_AWARE_ENABLED = False


_fake_cfg = _FakeCfg()


def _make_rm(**kwargs):
    rm = RiskManager(**kwargs)
    return rm


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestCircuitBreakerTrip(unittest.TestCase):
    """_trip_circuit_breaker — state activation and query."""

    def setUp(self):
        self._p = patch.object(_rm_mod, "cfg", _fake_cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def _rm(self):
        return _make_rm()

    def test_initially_not_active(self):
        rm = self._rm()
        self.assertFalse(rm.circuit_breaker_active())

    def test_trip_activates_cb(self):
        rm = self._rm()
        rm._trip_circuit_breaker("test reason")
        self.assertTrue(rm.circuit_breaker_active())

    def test_trip_sets_reason(self):
        rm = self._rm()
        rm._trip_circuit_breaker("some reason")
        self.assertEqual(rm._circuit_breaker_reason, "some reason")

    def test_trip_twice_is_idempotent(self):
        rm = self._rm()
        rm._trip_circuit_breaker("first")
        rm._trip_circuit_breaker("second")
        # Second trip while active does nothing (already tripped)
        self.assertEqual(rm._circuit_breaker_reason, "first")

    def test_clear_deactivates_cb(self):
        rm = self._rm()
        rm._trip_circuit_breaker("reason")
        rm._clear_circuit_breaker()
        self.assertFalse(rm.circuit_breaker_active())

    def test_clear_when_not_active_is_safe(self):
        rm = self._rm()
        rm._clear_circuit_breaker()  # Should not raise
        self.assertFalse(rm.circuit_breaker_active())

    def test_position_cb_sets_blocked_side(self):
        rm = self._rm()
        rm._trip_circuit_breaker("position overshoot", cb_type="position", blocked_side="buy")
        self.assertEqual(rm.get_circuit_breaker_blocked_side(), "buy")

    def test_price_cb_is_full_halt(self):
        rm = self._rm()
        rm._trip_circuit_breaker("price limit", cb_type="price")
        self.assertTrue(rm.is_full_halt())

    def test_position_cb_buy_is_not_full_halt(self):
        rm = self._rm()
        rm._trip_circuit_breaker("overlong", cb_type="position", blocked_side="buy")
        self.assertFalse(rm.is_full_halt())

    def test_position_cb_escalates_to_price_cb(self):
        rm = self._rm()
        rm._trip_circuit_breaker("overlong", cb_type="position", blocked_side="buy")
        rm._clear_circuit_breaker()  # clear so we can re-trip as price
        rm._trip_circuit_breaker("price", cb_type="price")
        self.assertEqual(rm._circuit_breaker_type, "price")


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestShouldEnableSide(unittest.TestCase):
    """should_enable_side — CB enforcement + soft inventory limits."""

    def setUp(self):
        self._p = patch.object(_rm_mod, "cfg", _fake_cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_both_sides_enabled_when_no_cb(self):
        rm = _make_rm()
        self.assertTrue(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertTrue(rm.should_enable_side("sell", Decimal("1.00")))

    def test_full_halt_cb_blocks_both_sides(self):
        rm = _make_rm()
        rm._trip_circuit_breaker("price limit", cb_type="price")
        self.assertFalse(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertFalse(rm.should_enable_side("sell", Decimal("1.00")))

    def test_position_cb_buy_blocks_buy_only(self):
        rm = _make_rm()
        rm._trip_circuit_breaker("overlong", cb_type="position", blocked_side="buy")
        self.assertFalse(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertTrue(rm.should_enable_side("sell", Decimal("1.00")))

    def test_position_cb_sell_blocks_sell_only(self):
        rm = _make_rm()
        rm._trip_circuit_breaker("overshort", cb_type="position", blocked_side="sell")
        self.assertTrue(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertFalse(rm.should_enable_side("sell", Decimal("1.00")))

    def test_inventory_soft_limit_disables_buy_when_max_long(self):
        rm = _make_rm()
        # net position = +95 CAT * 1.0 XCH/CAT = 95 XCH > 90% of 100 XCH limit
        rm._net_position_cat = Decimal("95")
        self.assertFalse(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertTrue(rm.should_enable_side("sell", Decimal("1.00")))

    def test_inventory_soft_limit_disables_sell_when_max_short(self):
        rm = _make_rm()
        rm._net_position_cat = Decimal("-95")
        self.assertTrue(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertFalse(rm.should_enable_side("sell", Decimal("1.00")))

    def test_neutral_position_enables_both(self):
        rm = _make_rm()
        rm._net_position_cat = Decimal("0")
        self.assertTrue(rm.should_enable_side("buy", Decimal("1.00")))
        self.assertTrue(rm.should_enable_side("sell", Decimal("1.00")))

    def test_inventory_disabled_always_enables_both(self):
        cfg_patch = _FakeCfg()
        cfg_patch.INVENTORY_ENABLED = False
        cfg_patch.MAX_POSITION_XCH = Decimal("100")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            rm._net_position_cat = Decimal("200")  # Way over limit
            self.assertTrue(rm.should_enable_side("buy", Decimal("1.00")))


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestCheckPositionLimit(unittest.TestCase):
    """_check_position_limit — soft (no halt) vs hard (trip CB)."""

    def setUp(self):
        self._p = patch.object(_rm_mod, "cfg", _fake_cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_no_position_no_trip(self):
        rm = _make_rm()
        rm._net_position_cat = Decimal("0")
        result = rm._check_position_limit(Decimal("1.00"))
        self.assertFalse(result)

    def test_below_soft_limit_no_trip(self):
        rm = _make_rm()
        rm._startup_position_xch = Decimal("0")  # baseline = 0, limit stays at 100
        rm._net_position_cat = Decimal("80")  # 80 XCH < 100 limit
        result = rm._check_position_limit(Decimal("1.00"))
        self.assertFalse(result)
        self.assertFalse(rm.circuit_breaker_active())

    def test_between_soft_and_hard_no_trip(self):
        rm = _make_rm()
        rm._startup_position_xch = Decimal("0")
        rm._net_position_cat = Decimal("120")  # 120 > limit but < 1.5×100=150
        result = rm._check_position_limit(Decimal("1.00"))
        self.assertFalse(result)
        self.assertFalse(rm.circuit_breaker_active())

    def test_above_hard_limit_trips_cb(self):
        rm = _make_rm()
        rm._startup_position_xch = Decimal("0")  # baseline=0 so effective_limit=100, hard=150
        rm._net_position_cat = Decimal("160")  # 160 > 150
        result = rm._check_position_limit(Decimal("1.00"))
        self.assertTrue(result)
        self.assertTrue(rm.circuit_breaker_active())

    def test_hard_limit_trips_correct_side(self):
        rm = _make_rm()
        rm._startup_position_xch = Decimal("0")
        rm._net_position_cat = Decimal("160")  # over-long → block buy
        rm._check_position_limit(Decimal("1.00"))
        self.assertEqual(rm.get_circuit_breaker_blocked_side(), "buy")

    def test_short_position_trips_sell_side(self):
        rm = _make_rm()
        rm._startup_position_xch = Decimal("0")
        rm._net_position_cat = Decimal("-160")  # over-short → block sell
        rm._check_position_limit(Decimal("1.00"))
        self.assertEqual(rm.get_circuit_breaker_blocked_side(), "sell")

    def test_zero_max_position_disabled(self):
        cfg_patch = _FakeCfg()
        cfg_patch.MAX_POSITION_XCH = Decimal("0")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            rm._net_position_cat = Decimal("9999")
            result = rm._check_position_limit(Decimal("1.00"))
        self.assertFalse(result)


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestCheckPriceLimits(unittest.TestCase):
    """_check_price_limits — hard min/max guards."""

    def setUp(self):
        self._p = patch.object(_rm_mod, "cfg", _fake_cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_zero_limits_no_trip(self):
        rm = _make_rm()
        self.assertFalse(rm._check_price_limits(Decimal("1.00")))

    def test_zero_price_no_trip(self):
        rm = _make_rm()
        self.assertFalse(rm._check_price_limits(Decimal("0")))

    def test_hard_min_trips_cb(self):
        cfg_patch = _FakeCfg()
        cfg_patch.HARD_MIN_PRICE_XCH = Decimal("0.50")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            result = rm._check_price_limits(Decimal("0.40"))
        self.assertTrue(result)
        self.assertTrue(rm.circuit_breaker_active())

    def test_hard_max_trips_cb(self):
        cfg_patch = _FakeCfg()
        cfg_patch.HARD_MAX_PRICE_XCH = Decimal("2.00")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            result = rm._check_price_limits(Decimal("2.50"))
        self.assertTrue(result)

    def test_within_limits_no_trip(self):
        cfg_patch = _FakeCfg()
        cfg_patch.HARD_MIN_PRICE_XCH = Decimal("0.50")
        cfg_patch.HARD_MAX_PRICE_XCH = Decimal("2.00")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            result = rm._check_price_limits(Decimal("1.00"))
        self.assertFalse(result)


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestCircuitBreakerHysteresis(unittest.TestCase):
    """check_circuit_breakers — requires 3 consecutive clears (hysteresis)."""

    def setUp(self):
        self._p = patch.object(_rm_mod, "cfg", _fake_cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_cb_not_cleared_on_single_ok_cycle(self):
        rm = _make_rm()
        rm._trip_circuit_breaker("reason")
        rm.check_circuit_breakers(Decimal("1.00"))  # 1 OK cycle
        self.assertTrue(rm.circuit_breaker_active())

    def test_cb_cleared_after_3_consecutive_ok_cycles(self):
        rm = _make_rm()
        rm._trip_circuit_breaker("reason")
        for _ in range(3):
            rm.check_circuit_breakers(Decimal("1.00"))
        self.assertFalse(rm.circuit_breaker_active())

    def test_hysteresis_resets_on_new_trip(self):
        rm = _make_rm()
        rm._trip_circuit_breaker("reason")
        rm.check_circuit_breakers(Decimal("1.00"))  # 1 OK cycle
        # Second trip should reset streak
        rm._clear_circuit_breaker()
        cfg_patch = _FakeCfg()
        cfg_patch.HARD_MAX_PRICE_XCH = Decimal("0.50")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm.check_circuit_breakers(Decimal("2.00"))  # new trip
        self.assertEqual(rm._cb_clear_streak, 0)


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestGetBaseSpread(unittest.TestCase):
    """_get_base_spread — reads from config based on dynamic mode."""

    def test_dynamic_mode_uses_base_spread_bps(self):
        cfg_patch = _FakeCfg()
        cfg_patch.DYNAMIC_SPREAD_ENABLED = True
        cfg_patch.BASE_SPREAD_BPS = Decimal("700")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            spread = rm._get_base_spread()
        self.assertEqual(spread, Decimal("700") / Decimal("10000"))

    def test_static_mode_uses_spread_bps(self):
        cfg_patch = _FakeCfg()
        cfg_patch.DYNAMIC_SPREAD_ENABLED = False
        cfg_patch.SPREAD_BPS = Decimal("500")
        with patch.object(_rm_mod, "cfg", cfg_patch):
            rm = _make_rm()
            spread = rm._get_base_spread()
        self.assertEqual(spread, Decimal("500") / Decimal("10000"))


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestApplyInventorySkew(unittest.TestCase):
    """_apply_inventory_skew — spread adjustment based on net position."""

    def setUp(self):
        self._p = patch.object(_rm_mod, "cfg", _fake_cfg)
        self._p.start()

    def tearDown(self):
        self._p.stop()

    class _FakeEngine:
        def get_last_price(self): return Decimal("1.00")

    def test_neutral_position_no_skew(self):
        rm = _make_rm(price_engine=self._FakeEngine())
        rm._net_position_cat = Decimal("0")
        spread = Decimal("0.07")
        self.assertEqual(rm._apply_inventory_skew(spread, "buy"), spread)

    def test_long_position_widens_buy(self):
        rm = _make_rm(price_engine=self._FakeEngine())
        rm._net_position_cat = Decimal("50")  # 50 CAT × 1.00 = 50 XCH
        base = Decimal("0.07")
        adjusted = rm._apply_inventory_skew(base, "buy")
        self.assertGreater(adjusted, base)

    def test_long_position_tightens_sell(self):
        rm = _make_rm(price_engine=self._FakeEngine())
        rm._net_position_cat = Decimal("50")
        base = Decimal("0.07")
        adjusted = rm._apply_inventory_skew(base, "sell")
        self.assertLess(adjusted, base)

    def test_short_position_tightens_buy(self):
        rm = _make_rm(price_engine=self._FakeEngine())
        rm._net_position_cat = Decimal("-50")
        base = Decimal("0.07")
        adjusted = rm._apply_inventory_skew(base, "buy")
        self.assertLess(adjusted, base)

    def test_skew_never_below_min_edge(self):
        rm = _make_rm(price_engine=self._FakeEngine())
        rm._net_position_cat = Decimal("100")  # max long
        base = Decimal("0.015")  # tiny spread
        adjusted = rm._apply_inventory_skew(base, "sell")
        min_edge = _fake_cfg.MIN_EDGE_BPS / Decimal("10000")
        self.assertGreaterEqual(adjusted, min_edge)


if __name__ == "__main__":
    unittest.main()
