"""Tests for dynamic_amm_buffer.py (Tier 3)."""

import sys
import time
import types
import unittest


def _install_config(window_mins=60, enabled=True, med=1.5, hi=2.0, cap=2.5):
    fake_config = types.ModuleType("config")
    fake_config.cfg = types.SimpleNamespace(
        DYNAMIC_BUFFER_WINDOW_MINS=window_mins,
        DYNAMIC_BUFFER_ENABLED=enabled,
        DYNAMIC_BUFFER_MULTIPLIER_MED=med,
        DYNAMIC_BUFFER_MULTIPLIER_HIGH=hi,
        DYNAMIC_BUFFER_MULTIPLIER_CAP=cap,
        AMM_BUFFER_BPS=30,
        AMM_DRIFT_REQUOTE_BPS=40,
    )
    sys.modules["config"] = fake_config


class DynamicAMMBufferTests(unittest.TestCase):
    def setUp(self):
        _install_config()
        sys.modules.pop("dynamic_amm_buffer", None)
        import dynamic_amm_buffer

        self.mod = dynamic_amm_buffer
        self.mod.reset_buffer()

    def tearDown(self):
        sys.modules.pop("dynamic_amm_buffer", None)
        sys.modules.pop("config", None)

    # ------------------------------------------------------------------
    # Baseline (no sweeps)
    # ------------------------------------------------------------------

    def test_no_sweeps_returns_base_bps_unchanged(self):
        from decimal import Decimal

        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("30.0"))

    def test_no_sweeps_multiplier_is_1(self):
        buf = self.mod._get_buffer_instance()
        from decimal import Decimal

        self.assertEqual(buf._get_multiplier(), Decimal("1"))

    # ------------------------------------------------------------------
    # Multiplier tiers
    # ------------------------------------------------------------------

    def test_one_sweep_applies_medium_multiplier(self):
        from decimal import Decimal

        self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("45.0"))  # 30 × 1.5

    def test_two_sweeps_applies_medium_multiplier(self):
        from decimal import Decimal

        self.mod.record_sweep()
        self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("45.0"))  # still 1.5×

    def test_three_sweeps_applies_high_multiplier(self):
        from decimal import Decimal

        for _ in range(3):
            self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("60.0"))  # 30 × 2.0

    def test_five_sweeps_applies_high_multiplier(self):
        from decimal import Decimal

        for _ in range(5):
            self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("60.0"))  # 30 × 2.0

    def test_six_sweeps_applies_cap_multiplier(self):
        from decimal import Decimal

        for _ in range(6):
            self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("75.0"))  # 30 × 2.5

    def test_many_sweeps_capped_at_cap_multiplier(self):
        from decimal import Decimal

        for _ in range(20):
            self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("75.0"))  # still 2.5×

    # ------------------------------------------------------------------
    # Disabled toggle
    # ------------------------------------------------------------------

    def test_disabled_always_returns_base(self):
        from decimal import Decimal

        sys.modules.pop("dynamic_amm_buffer", None)
        _install_config(enabled=False)
        import dynamic_amm_buffer

        self.mod = dynamic_amm_buffer
        self.mod.reset_buffer()

        for _ in range(10):
            self.mod.record_sweep()
        result = self.mod.get_buffer(30)
        self.assertEqual(result, Decimal("30.0"))

    # ------------------------------------------------------------------
    # Rolling window expiry
    # ------------------------------------------------------------------

    def test_sweeps_outside_window_not_counted(self):
        """Sweeps with zero-length window expire immediately."""
        sys.modules.pop("dynamic_amm_buffer", None)
        _install_config(window_mins=0.0)  # 0-minute window → all expire at once
        import dynamic_amm_buffer

        self.mod = dynamic_amm_buffer
        self.mod.reset_buffer()

        buf = self.mod._get_buffer_instance()
        # Inject a stale entry directly (monotonic time in the distant past)
        from collections import deque

        buf._sweeps = deque([(time.monotonic() - 9999, 1)])

        # After pruning, count should be 0 → multiplier 1
        count = buf.sweep_count_in_window()
        self.assertEqual(count, 0)

    # ------------------------------------------------------------------
    # get_state
    # ------------------------------------------------------------------

    def test_get_state_reflects_sweep_count_and_multiplier(self):
        self.mod.record_sweep()
        self.mod.record_sweep()
        state = self.mod.get_state()
        self.assertEqual(state["sweep_count_in_window"], 2)
        self.assertAlmostEqual(state["multiplier"], 1.5)
        self.assertEqual(state["effective_bps"], "45.0")
        self.assertEqual(state["base_bps"], "30")
        self.assertTrue(state["enabled"])

    def test_get_state_no_sweeps(self):
        state = self.mod.get_state()
        self.assertEqual(state["sweep_count_in_window"], 0)
        self.assertAlmostEqual(state["multiplier"], 1.0)
        self.assertEqual(state["effective_bps"], "30.0")

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    def test_singleton_returns_same_instance(self):
        b1 = self.mod._get_buffer_instance()
        b2 = self.mod._get_buffer_instance()
        self.assertIs(b1, b2)

    def test_reset_creates_new_instance(self):
        b1 = self.mod._get_buffer_instance()
        self.mod.reset_buffer()
        b2 = self.mod._get_buffer_instance()
        self.assertIsNot(b1, b2)

    # ------------------------------------------------------------------
    # Input type flexibility
    # ------------------------------------------------------------------

    def test_accepts_string_base_bps(self):
        from decimal import Decimal

        result = self.mod.get_buffer("50")
        self.assertEqual(result, Decimal("50.0"))

    def test_accepts_float_base_bps(self):
        from decimal import Decimal

        result = self.mod.get_buffer(25.0)
        self.assertEqual(result, Decimal("25.0"))


if __name__ == "__main__":
    unittest.main()
