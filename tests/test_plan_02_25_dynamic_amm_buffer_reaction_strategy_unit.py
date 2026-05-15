"""Slice 02-25 — dynamic_amm_buffer.py + reaction_strategy.py unit tests.

reaction_strategy:  RequoteSeverity, CycleBudget, compute_offer_staleness,
                    classify_drift, tiers_for_severity, filter_offers_by_tiers.
dynamic_amm_buffer: DynamicAMMBuffer sweep tracking, multiplier tiers,
                    reset_buffer, get_effective_buffer_bps.
"""

import unittest
from decimal import Decimal

try:
    from reaction_strategy import (
        RequoteSeverity,
        CycleBudget,
        compute_offer_staleness,
        classify_drift,
        tiers_for_severity,
        filter_offers_by_tiers,
        TIER_PRIORITY,
    )

    _SKIP_RS = None
except ModuleNotFoundError as exc:
    _SKIP_RS = str(exc)

try:
    from dynamic_amm_buffer import (
        DynamicAMMBuffer,
        reset_buffer,
        _get_buffer_instance,
        record_sweep,
        get_buffer,
    )

    _SKIP_DAB = None
except ModuleNotFoundError as exc:
    _SKIP_DAB = str(exc)


# ---------------------------------------------------------------------------
# RequoteSeverity enum
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestRequoteSeverity(unittest.TestCase):
    def test_five_members(self):
        self.assertEqual(len(RequoteSeverity), 5)

    def test_member_names(self):
        names = {m.name for m in RequoteSeverity}
        self.assertEqual(names, {"NONE", "INNER", "INNER_MID", "FULL", "EMERGENCY"})

    def test_none_is_least_severe(self):
        # NONE should be accessible and distinct
        self.assertIsInstance(RequoteSeverity.NONE, RequoteSeverity)

    def test_emergency_is_most_severe(self):
        self.assertIsInstance(RequoteSeverity.EMERGENCY, RequoteSeverity)


# ---------------------------------------------------------------------------
# CycleBudget dataclass
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestCycleBudget(unittest.TestCase):
    def test_defaults(self):
        b = CycleBudget()
        self.assertEqual(b.max_cancels, 6)
        self.assertEqual(b.max_creates, 6)
        self.assertEqual(b.cancels_used, 0)
        self.assertEqual(b.creates_used, 0)

    def test_custom_limits(self):
        b = CycleBudget(max_cancels=3, max_creates=3)
        self.assertEqual(b.max_cancels, 3)

    def test_can_cancel_within_budget(self):
        b = CycleBudget(max_cancels=2)
        self.assertTrue(b.can_cancel(1))
        self.assertTrue(b.can_cancel(2))

    def test_can_cancel_exceeds_budget(self):
        b = CycleBudget(max_cancels=2)
        self.assertFalse(b.can_cancel(3))

    def test_use_cancel_increments(self):
        b = CycleBudget()
        b.use_cancel()
        self.assertEqual(b.cancels_used, 1)
        b.use_cancel(2)
        self.assertEqual(b.cancels_used, 3)

    def test_can_create_within_budget(self):
        b = CycleBudget(max_creates=3)
        self.assertTrue(b.can_create(3))
        self.assertFalse(b.can_create(4))

    def test_use_create_increments(self):
        b = CycleBudget()
        b.use_create(3)
        self.assertEqual(b.creates_used, 3)

    def test_remaining_cancels(self):
        b = CycleBudget(max_cancels=6)
        b.use_cancel(4)
        self.assertEqual(b.remaining_cancels, 2)

    def test_remaining_creates(self):
        b = CycleBudget(max_creates=6)
        b.use_create(6)
        self.assertEqual(b.remaining_creates, 0)

    def test_remaining_total(self):
        b = CycleBudget(max_cancels=3, max_creates=3)
        self.assertEqual(b.remaining_total, 6)
        b.use_cancel(1)
        b.use_create(2)
        self.assertEqual(b.remaining_total, 3)

    def test_not_exhausted_initially(self):
        self.assertFalse(CycleBudget().exhausted)

    def test_exhausted_when_both_used_up(self):
        b = CycleBudget(max_cancels=1, max_creates=1)
        b.use_cancel()
        b.use_create()
        self.assertTrue(b.exhausted)

    def test_remaining_never_negative(self):
        b = CycleBudget(max_cancels=1)
        b.use_cancel(10)
        self.assertEqual(b.remaining_cancels, 0)


# ---------------------------------------------------------------------------
# compute_offer_staleness
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestComputeOfferStaleness(unittest.TestCase):
    def test_perfect_match_is_zero(self):
        offer = {"price_xch": "0.001"}
        self.assertEqual(compute_offer_staleness(offer, Decimal("0.001")), Decimal("0"))

    def test_5pct_deviation(self):
        offer = {"price_xch": "0.00105"}
        result = compute_offer_staleness(offer, Decimal("0.001"))
        self.assertAlmostEqual(float(result), 0.05, places=5)

    def test_missing_price_key_returns_zero(self):
        self.assertEqual(compute_offer_staleness({}, Decimal("0.001")), Decimal("0"))

    def test_zero_ideal_price_returns_zero(self):
        self.assertEqual(
            compute_offer_staleness({"price_xch": "0.001"}, Decimal("0")), Decimal("0")
        )

    def test_zero_actual_price_returns_zero(self):
        self.assertEqual(
            compute_offer_staleness({"price_xch": "0"}, Decimal("0.001")), Decimal("0")
        )

    def test_invalid_price_string_returns_zero(self):
        self.assertEqual(
            compute_offer_staleness({"price_xch": "bad"}, Decimal("0.001")),
            Decimal("0"),
        )

    def test_staleness_is_always_non_negative(self):
        # Price below ideal
        offer = {"price_xch": "0.0009"}
        result = compute_offer_staleness(offer, Decimal("0.001"))
        self.assertGreaterEqual(result, Decimal("0"))


# ---------------------------------------------------------------------------
# classify_drift
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestClassifyDrift(unittest.TestCase):
    def test_zero_is_none(self):
        self.assertEqual(classify_drift(Decimal("0")), RequoteSeverity.NONE)

    def test_below_inner_threshold_is_none(self):
        self.assertEqual(classify_drift(Decimal("0.002")), RequoteSeverity.NONE)

    def test_at_inner_threshold_is_inner(self):
        self.assertEqual(classify_drift(Decimal("0.003")), RequoteSeverity.INNER)

    def test_between_inner_and_mid_is_inner(self):
        self.assertEqual(classify_drift(Decimal("0.005")), RequoteSeverity.INNER)

    def test_at_mid_threshold_is_inner_mid(self):
        self.assertEqual(classify_drift(Decimal("0.008")), RequoteSeverity.INNER_MID)

    def test_at_full_threshold_is_full(self):
        self.assertEqual(classify_drift(Decimal("0.02")), RequoteSeverity.FULL)

    def test_at_emergency_threshold_is_emergency(self):
        self.assertEqual(classify_drift(Decimal("0.05")), RequoteSeverity.EMERGENCY)

    def test_above_emergency_is_emergency(self):
        self.assertEqual(classify_drift(Decimal("0.10")), RequoteSeverity.EMERGENCY)

    def test_custom_thresholds(self):
        result = classify_drift(
            Decimal("0.01"),
            inner_threshold=Decimal("0.001"),
            mid_threshold=Decimal("0.005"),
            full_threshold=Decimal("0.02"),
            emergency_threshold=Decimal("0.05"),
        )
        self.assertEqual(result, RequoteSeverity.INNER_MID)


# ---------------------------------------------------------------------------
# tiers_for_severity
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestTiersForSeverity(unittest.TestCase):
    def test_none_returns_empty_set(self):
        self.assertEqual(tiers_for_severity(RequoteSeverity.NONE), set())

    def test_inner_returns_inner_only(self):
        self.assertEqual(tiers_for_severity(RequoteSeverity.INNER), {"inner"})

    def test_inner_mid_returns_inner_and_mid(self):
        self.assertEqual(
            tiers_for_severity(RequoteSeverity.INNER_MID), {"inner", "mid"}
        )

    def test_full_returns_all_four_tiers(self):
        result = tiers_for_severity(RequoteSeverity.FULL)
        self.assertEqual(result, {"inner", "mid", "outer", "extreme"})

    def test_emergency_returns_all_four_tiers(self):
        result = tiers_for_severity(RequoteSeverity.EMERGENCY)
        self.assertEqual(result, {"inner", "mid", "outer", "extreme"})


# ---------------------------------------------------------------------------
# filter_offers_by_tiers
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestFilterOffersByTiers(unittest.TestCase):
    def _offers(self, tiers):
        return [{"tier": t, "id": i} for i, t in enumerate(tiers)]

    def test_empty_list_returns_empty(self):
        self.assertEqual(filter_offers_by_tiers([], {"inner"}), [])

    def test_all_match(self):
        offers = self._offers(["inner", "inner", "inner"])
        result = filter_offers_by_tiers(offers, {"inner"})
        self.assertEqual(len(result), 3)

    def test_some_match(self):
        offers = self._offers(["inner", "mid", "outer"])
        result = filter_offers_by_tiers(offers, {"inner", "mid"})
        self.assertEqual(len(result), 2)

    def test_no_match_returns_empty(self):
        offers = self._offers(["outer", "extreme"])
        result = filter_offers_by_tiers(offers, {"inner"})
        self.assertEqual(result, [])

    def test_missing_tier_defaults_to_mid(self):
        # `o.get("tier") or "mid"` → missing tier treated as "mid"
        offers = [{"id": 0}]  # no "tier" key
        result = filter_offers_by_tiers(offers, {"mid"})
        self.assertEqual(len(result), 1)

    def test_case_insensitive(self):
        offers = [{"tier": "INNER"}]
        result = filter_offers_by_tiers(offers, {"inner"})
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# TIER_PRIORITY constant
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_RS is not None, f"reaction_strategy unavailable: {_SKIP_RS}")
class TestTierPriority(unittest.TestCase):
    def test_inner_is_highest_priority(self):
        self.assertEqual(TIER_PRIORITY["inner"], 0)

    def test_extreme_is_lowest_priority(self):
        self.assertEqual(TIER_PRIORITY["extreme"], 3)

    def test_four_tiers(self):
        self.assertEqual(len(TIER_PRIORITY), 4)


# ---------------------------------------------------------------------------
# DynamicAMMBuffer — sweep tracking + multiplier
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP_DAB is not None, f"dynamic_amm_buffer unavailable: {_SKIP_DAB}")
class TestDynamicAMMBuffer(unittest.TestCase):
    def setUp(self):
        reset_buffer()

    def tearDown(self):
        reset_buffer()

    def _buf(self) -> DynamicAMMBuffer:
        return _get_buffer_instance()

    def test_fresh_buffer_has_zero_sweeps(self):
        self.assertEqual(self._buf().sweep_count_in_window(), 0)

    def test_record_sweep_increments_count(self):
        buf = self._buf()
        buf.record_sweep()
        self.assertEqual(buf.sweep_count_in_window(), 1)

    def test_multiple_sweeps(self):
        buf = self._buf()
        for _ in range(5):
            buf.record_sweep()
        self.assertEqual(buf.sweep_count_in_window(), 5)

    def test_reset_clears_state(self):
        record_sweep()
        reset_buffer()
        self.assertEqual(self._buf().sweep_count_in_window(), 0)

    def test_no_sweeps_multiplier_is_1x(self):
        # 0 sweeps → multiplier = 1.0 → effective = base
        buf = self._buf()
        result = buf.get_effective_buffer_bps(30)
        self.assertEqual(result, Decimal("30.0"))

    def test_1_sweep_applies_med_multiplier(self):
        # 1 sweep → med (default 1.5x) → 30 * 1.5 = 45.0
        buf = self._buf()
        buf.record_sweep()
        result = buf.get_effective_buffer_bps(30)
        self.assertEqual(result, Decimal("45.0"))

    def test_3_sweeps_applies_hi_multiplier(self):
        # 3 sweeps → hi (default 2.0x) → 30 * 2.0 = 60.0
        buf = self._buf()
        for _ in range(3):
            buf.record_sweep()
        result = buf.get_effective_buffer_bps(30)
        self.assertEqual(result, Decimal("60.0"))

    def test_6_sweeps_applies_cap_multiplier(self):
        # 6 sweeps → cap (default 2.5x) → 30 * 2.5 = 75.0
        buf = self._buf()
        for _ in range(6):
            buf.record_sweep()
        result = buf.get_effective_buffer_bps(30)
        self.assertEqual(result, Decimal("75.0"))

    def test_get_effective_accepts_string_bps(self):
        result = self._buf().get_effective_buffer_bps("30")
        self.assertIsInstance(result, Decimal)

    def test_module_level_record_and_get(self):
        record_sweep()
        result = get_buffer(30)
        self.assertEqual(result, Decimal("45.0"))


if __name__ == "__main__":
    unittest.main()
