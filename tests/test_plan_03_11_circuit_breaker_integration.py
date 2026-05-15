"""Slice 03-11 — circuit breaker trip + recover (integration test).

Tests the full trip → halted → hysteresis → cleared cycle, using RiskManager
with both hard price limits and a mock PriceEngine dynamic limit.
No Flask server needed — exercises module wiring in-process.
"""

import sys
import os
import threading
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import risk_manager as _rm_mod
    from risk_manager import RiskManager

    _SKIP = None
except ModuleNotFoundError as exc:
    _rm_mod = None
    RiskManager = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Shared fake config
# ---------------------------------------------------------------------------


def _make_cfg(**overrides):
    defaults = dict(
        INVENTORY_ENABLED=False,
        MAX_POSITION_XCH=Decimal("0"),
        SKEW_INTENSITY=Decimal("0.3"),
        DYNAMIC_SPREAD_ENABLED=False,
        MIN_SPREAD_BPS=Decimal("200"),
        MAX_SPREAD_BPS=Decimal("2000"),
        MIN_EDGE_BPS=Decimal("100"),
        BASE_SPREAD_BPS=Decimal("700"),
        SPREAD_BPS=Decimal("700"),
        HARD_MIN_PRICE_XCH=Decimal("0"),
        HARD_MAX_PRICE_XCH=Decimal("0"),
        DYNAMIC_LIMIT_PCT=Decimal("0"),
        COMPETITOR_AWARE_ENABLED=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# 1. Hard price limit — trip and confirm halt
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestHardPriceLimitTrip(unittest.TestCase):
    """HARD_MAX_PRICE_XCH trips circuit breaker on a single call."""

    def setUp(self):
        self._cfg = _make_cfg(HARD_MAX_PRICE_XCH=Decimal("0.50"))
        self._patch = patch.object(_rm_mod, "cfg", self._cfg)
        self._patch.start()
        self.rm = RiskManager()

    def tearDown(self):
        self._patch.stop()

    def test_no_halt_below_limit(self):
        result = self.rm.check_circuit_breakers(Decimal("0.40"))
        self.assertFalse(result)
        self.assertFalse(self.rm.circuit_breaker_active())

    def test_trip_on_price_above_limit(self):
        result = self.rm.check_circuit_breakers(Decimal("0.60"))
        self.assertTrue(result)
        self.assertTrue(self.rm.circuit_breaker_active())

    def test_reason_contains_price(self):
        self.rm.check_circuit_breakers(Decimal("0.60"))
        self.assertIn("0.60", self.rm._circuit_breaker_reason)

    def test_is_full_halt_after_trip(self):
        self.rm.check_circuit_breakers(Decimal("0.60"))
        self.assertTrue(self.rm.is_full_halt())

    def test_hard_min_trips_below_floor(self):
        self._cfg.HARD_MIN_PRICE_XCH = Decimal("0.10")
        result = self.rm.check_circuit_breakers(Decimal("0.05"))
        self.assertTrue(result)
        self.assertTrue(self.rm.circuit_breaker_active())

    def test_zero_price_does_not_trip(self):
        # price <= 0 skips limit checks
        result = self.rm.check_circuit_breakers(Decimal("0"))
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 2. Hysteresis — must have N consecutive OK cycles to clear
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestCircuitBreakerHysteresis(unittest.TestCase):
    """CB clears only after _cb_clear_threshold consecutive OK cycles."""

    def setUp(self):
        self._cfg = _make_cfg(HARD_MAX_PRICE_XCH=Decimal("0.50"))
        self._patch = patch.object(_rm_mod, "cfg", self._cfg)
        self._patch.start()
        self.rm = RiskManager()
        # Trip the CB
        self.rm.check_circuit_breakers(Decimal("0.60"))
        self.assertTrue(self.rm.circuit_breaker_active(), "precondition: CB tripped")

    def tearDown(self):
        self._patch.stop()

    def test_still_halted_after_one_ok_cycle(self):
        self.rm.check_circuit_breakers(Decimal("0.40"))
        self.assertTrue(self.rm.circuit_breaker_active())

    def test_still_halted_after_two_ok_cycles(self):
        self.rm.check_circuit_breakers(Decimal("0.40"))
        self.rm.check_circuit_breakers(Decimal("0.40"))
        self.assertTrue(self.rm.circuit_breaker_active())

    def test_clears_after_three_ok_cycles(self):
        threshold = self.rm._cb_clear_threshold  # default 3
        for _ in range(threshold):
            self.rm.check_circuit_breakers(Decimal("0.40"))
        self.assertFalse(self.rm.circuit_breaker_active())

    def test_bad_price_during_hysteresis_does_not_reset_streak(self):
        # 2 good cycles (streak=2), then a bad-price call exits early (streak stays 2).
        # Only 1 more good cycle is needed to reach the threshold of 3.
        self.rm.check_circuit_breakers(Decimal("0.40"))  # streak=1
        self.rm.check_circuit_breakers(Decimal("0.40"))  # streak=2
        # Bad price: _check_price_limits returns True, no streak update
        self.rm.check_circuit_breakers(Decimal("0.60"))
        # Still active (streak=2, not enough yet)
        self.assertTrue(self.rm.circuit_breaker_active())
        # One more good cycle reaches threshold → clears
        self.rm.check_circuit_breakers(Decimal("0.40"))  # streak=3 → clear
        self.assertFalse(self.rm.circuit_breaker_active())

    def test_trading_resumes_after_clear(self):
        for _ in range(self.rm._cb_clear_threshold):
            result = self.rm.check_circuit_breakers(Decimal("0.40"))
        # Last call should return False (clear)
        self.assertFalse(result)
        self.assertFalse(self.rm.is_full_halt())


# ---------------------------------------------------------------------------
# 3. Dynamic limit via PriceEngine integration
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestDynamicLimitPriceEngineIntegration(unittest.TestCase):
    """RiskManager + mock PriceEngine dynamic limit wiring."""

    def setUp(self):
        self._cfg = _make_cfg(DYNAMIC_LIMIT_PCT=Decimal("10"))
        self._patch = patch.object(_rm_mod, "cfg", self._cfg)
        self._patch.start()
        self._pe = MagicMock()
        self.rm = RiskManager(price_engine=self._pe)

    def tearDown(self):
        self._patch.stop()

    def test_no_trip_when_price_engine_returns_none_limits(self):
        self._pe.get_dynamic_limits.return_value = (None, None)
        result = self.rm.check_circuit_breakers(Decimal("1.0"))
        self.assertFalse(result)

    def test_trip_when_price_above_dynamic_max(self):
        self._pe.get_dynamic_limits.return_value = (Decimal("0.90"), Decimal("1.10"))
        result = self.rm.check_circuit_breakers(Decimal("1.50"))
        self.assertTrue(result)
        self.assertTrue(self.rm.circuit_breaker_active())

    def test_trip_when_price_below_dynamic_min(self):
        self._pe.get_dynamic_limits.return_value = (Decimal("0.90"), Decimal("1.10"))
        result = self.rm.check_circuit_breakers(Decimal("0.50"))
        self.assertTrue(result)

    def test_no_trip_within_dynamic_band(self):
        self._pe.get_dynamic_limits.return_value = (Decimal("0.90"), Decimal("1.10"))
        result = self.rm.check_circuit_breakers(Decimal("1.00"))
        self.assertFalse(result)

    def test_dynamic_limit_checked_before_hard_limit(self):
        # Dynamic max = 1.10, hard max = 2.00 → dynamic trips first
        self._cfg.HARD_MAX_PRICE_XCH = Decimal("2.00")
        self._pe.get_dynamic_limits.return_value = (Decimal("0.90"), Decimal("1.10"))
        result = self.rm.check_circuit_breakers(Decimal("1.50"))
        self.assertTrue(result)
        self.assertIn("dynamic maximum", self.rm._circuit_breaker_reason)


# ---------------------------------------------------------------------------
# 4. Full trip-recover cycle
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestFullTripRecoverCycle(unittest.TestCase):
    """End-to-end: trip → halted → recover → can trade → trip again."""

    def setUp(self):
        self._cfg = _make_cfg(HARD_MAX_PRICE_XCH=Decimal("1.00"))
        self._patch = patch.object(_rm_mod, "cfg", self._cfg)
        self._patch.start()
        self.rm = RiskManager()

    def tearDown(self):
        self._patch.stop()

    def _ok_cycles(self, n=None, price=Decimal("0.50")):
        if n is None:
            n = self.rm._cb_clear_threshold
        for _ in range(n):
            self.rm.check_circuit_breakers(price)

    def test_full_cycle_once(self):
        # Trip
        self.rm.check_circuit_breakers(Decimal("2.00"))
        self.assertTrue(self.rm.is_full_halt())
        # Recover
        self._ok_cycles()
        self.assertFalse(self.rm.is_full_halt())

    def test_can_trip_again_after_recovery(self):
        # First trip + recover
        self.rm.check_circuit_breakers(Decimal("2.00"))
        self._ok_cycles()
        self.assertFalse(self.rm.is_full_halt())
        # Second trip
        self.rm.check_circuit_breakers(Decimal("2.00"))
        self.assertTrue(self.rm.is_full_halt())

    def test_multiple_full_cycles(self):
        for _ in range(3):
            self.rm.check_circuit_breakers(Decimal("2.00"))
            self.assertTrue(self.rm.is_full_halt())
            self._ok_cycles()
            self.assertFalse(self.rm.is_full_halt())

    def test_reason_cleared_after_recovery(self):
        self.rm.check_circuit_breakers(Decimal("2.00"))
        self.assertTrue(self.rm._circuit_breaker_reason)
        self._ok_cycles()
        self.assertFalse(self.rm._circuit_breaker_reason)


# ---------------------------------------------------------------------------
# 5. Thread safety
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"risk_manager unavailable: {_SKIP}")
class TestCircuitBreakerThreadSafety(unittest.TestCase):
    """Concurrent reads and writes don't corrupt CB state."""

    def setUp(self):
        self._cfg = _make_cfg(HARD_MAX_PRICE_XCH=Decimal("1.00"))
        self._patch = patch.object(_rm_mod, "cfg", self._cfg)
        self._patch.start()
        self.rm = RiskManager()

    def tearDown(self):
        self._patch.stop()

    def test_concurrent_check_circuit_breakers(self):
        errors = []
        results = []

        def _worker(price):
            try:
                r = self.rm.check_circuit_breakers(price)
                results.append(r)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(20):
            price = Decimal("2.00") if i % 3 == 0 else Decimal("0.50")
            t = threading.Thread(target=_worker, args=(price,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        # At least one call should have tripped
        self.assertTrue(any(results))

    def test_concurrent_reads_while_tripping(self):
        self.rm.check_circuit_breakers(Decimal("2.00"))
        errors = []

        def _read():
            try:
                self.rm.circuit_breaker_active()
                self.rm.is_full_halt()
                self.rm.get_circuit_breaker_blocked_side()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
