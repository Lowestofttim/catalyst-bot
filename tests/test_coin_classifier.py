"""Tests for the single-source-of-truth coin classifier.

Focuses on the regression from 2026-04-17: the 23.4k CAT change coin that
was mis-designated `tier_spare/inner` by reconcile (using ±20% bounds) but
flagged as a misfit by the absorber (using 0.98/1.5 bounds). Under the new
classifier, both paths use the same 0.98/1.5 bounds and agree it's a misfit.
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from decimal import Decimal

from coin_classifier import (
    classify_coin,
    is_misfit_coin,
    infer_designation_by_size,
    CoinFit,
    CoinDesignation,
    DEFAULT_FLOOR_TOLERANCE,
    DEFAULT_MAX_RATIO,
)


# The tier sizes used in the live pair that hit tonight's bug (MZ/XCH).
# Numbers are in mojos — inner is the largest for this reverse-ladder
# configuration.
MZ_TIERS = {
    "inner": 26_678_000,
    "mid": 13_339_000,
    "outer": 5_802_000,
    "extreme": 2_901_000,
}


class TestTheRegressionCoin:
    """The 23.4k CAT coin that broke tonight's ladder."""

    def test_23_4k_cat_is_a_misfit_under_strict_bounds(self):
        """Under the NEW unified 0.98/1.5 bounds, the 23.4k change coin is
        classified as unknown/misfit — not tier_spare/inner."""
        cls = classify_coin(23_400_000, MZ_TIERS)
        assert cls.is_misfit is True
        assert cls.best_tier is None
        assert cls.designation == CoinDesignation.UNKNOWN
        # Diagnostic: the nearest tier is inner (closest by distance)
        assert cls.nearest_tier == "inner"
        # Every tier should be rejected
        assert cls.candidates["inner"] == CoinFit.UNDER_FLOOR  # 23.4k < 26.16k floor
        assert cls.candidates["mid"] == CoinFit.OVER_CEILING  # 23.4k > 20.0k ceiling
        assert cls.candidates["outer"] == CoinFit.OVER_CEILING
        assert cls.candidates["extreme"] == CoinFit.OVER_CEILING

    def test_old_inference_would_have_accepted_it(self):
        """Sanity check: the OLD ±20% bounds (used by reconcile pre-fix)
        would have accepted 23.4k as tier_spare/inner. This is the bug."""
        # Simulate old bounds: 0.80 floor, 1.20 ceiling
        cls_old = classify_coin(
            23_400_000,
            MZ_TIERS,
            floor_tolerance=Decimal("0.80"),
            max_ratio=Decimal("1.20"),
        )
        # With 0.80 floor: 26.7k × 0.80 = 21.3k. 23.4k > 21.3k → within inner.
        # So under old bounds this would have been tier_spare/inner.
        assert cls_old.best_tier == "inner"
        assert cls_old.designation == CoinDesignation.TIER_SPARE
        assert cls_old.is_misfit is False

    def test_infer_designation_now_returns_unknown(self):
        """The compat wrapper used by reconcile now returns unknown/none
        for the regression coin, forcing the absorber path on the next cycle."""
        desig, tier = infer_designation_by_size(23_400_000, MZ_TIERS)
        assert desig == "unknown"
        assert tier == "none"


class TestExactMatches:
    """Tier-sized coins should always match their own tier."""

    @pytest.mark.parametrize("tier_name,tier_size", MZ_TIERS.items())
    def test_exact_tier_size_is_exact_fit(self, tier_name, tier_size):
        cls = classify_coin(tier_size, MZ_TIERS)
        assert cls.best_tier == tier_name
        assert cls.fit == CoinFit.EXACT
        assert cls.designation == CoinDesignation.TIER_SPARE
        assert cls.is_misfit is False

    def test_just_below_tier_at_floor_still_exact(self):
        """A coin 1% below inner is still within the 0.98 floor."""
        amount = int(Decimal(MZ_TIERS["inner"]) * Decimal("0.99"))
        cls = classify_coin(amount, MZ_TIERS)
        assert cls.best_tier == "inner"
        assert cls.fit == CoinFit.EXACT


class TestBoundaryConditions:
    """Verify the 0.98 floor and 1.5 ceiling behave exactly."""

    def test_exactly_at_floor_is_exact(self):
        """A coin exactly at floor_tolerance × tier_size is still EXACT."""
        amount = int(Decimal(MZ_TIERS["inner"]) * DEFAULT_FLOOR_TOLERANCE)
        cls = classify_coin(amount, MZ_TIERS)
        assert cls.candidates["inner"] == CoinFit.EXACT
        assert cls.best_tier == "inner"

    def test_one_below_floor_fails_this_tier(self):
        """One mojo below floor → UNDER_FLOOR for this tier."""
        amount = int(Decimal(MZ_TIERS["inner"]) * DEFAULT_FLOOR_TOLERANCE) - 1
        cls = classify_coin(amount, MZ_TIERS)
        assert cls.candidates["inner"] == CoinFit.UNDER_FLOOR

    def test_exactly_at_ceiling_is_oversize_fit(self):
        """A coin exactly at max_ratio × tier_size is OVERSIZE_FIT."""
        amount = int(Decimal(MZ_TIERS["inner"]) * DEFAULT_MAX_RATIO)
        cls = classify_coin(amount, MZ_TIERS)
        assert cls.candidates["inner"] == CoinFit.OVERSIZE_FIT

    def test_one_above_ceiling_is_rejected(self):
        """One mojo above ceiling → OVER_CEILING for this tier."""
        amount = int(Decimal(MZ_TIERS["inner"]) * DEFAULT_MAX_RATIO) + 1
        cls = classify_coin(amount, MZ_TIERS)
        assert cls.candidates["inner"] == CoinFit.OVER_CEILING


class TestDustAndReserve:
    """Explicit categories for out-of-range sizes."""

    def test_tiny_coin_is_dust(self):
        """50% of smallest tier is the dust threshold. Below that is dust."""
        cls = classify_coin(100_000, MZ_TIERS)  # 100k mojos, well below extreme (2.9M)
        assert cls.designation == CoinDesignation.DUST
        assert cls.best_tier is None
        assert cls.is_misfit is False  # dust is its own category

    def test_huge_coin_is_reserve(self):
        """2× largest tier is the reserve threshold. Above that is reserve."""
        cls = classify_coin(100_000_000, MZ_TIERS)  # 100M mojos, 3.7× inner
        assert cls.designation == CoinDesignation.RESERVE
        assert cls.best_tier is None
        assert cls.is_misfit is False  # reserve is its own category

    def test_reserve_compat_returns_reserve(self):
        desig, tier = infer_designation_by_size(100_000_000, MZ_TIERS)
        assert desig == "reserve"
        assert tier == "none"

    def test_dust_compat_returns_dust(self):
        desig, tier = infer_designation_by_size(100_000, MZ_TIERS)
        assert desig == "dust"
        assert tier == "none"


class TestPreferSmallestFittingTier:
    """When a coin fits multiple tiers (e.g. 1.2× extreme is within 1.5× extreme
    ceiling AND above mid floor), prefer the SMALLEST tier that fits exactly so
    we don't waste a large coin on a small-tier slot."""

    def test_coin_matching_mid_exactly_is_assigned_to_mid(self):
        cls = classify_coin(MZ_TIERS["mid"], MZ_TIERS)
        assert cls.best_tier == "mid"
        assert cls.fit == CoinFit.EXACT

    def test_oversize_only_falls_back_to_that_tier(self):
        """A coin 1.3× mid size (no exact match, oversize fit for mid) gets mid."""
        amount = int(Decimal(MZ_TIERS["mid"]) * Decimal("1.3"))  # 17.3k
        cls = classify_coin(amount, MZ_TIERS)
        # 17.3k < 26.16k inner floor → under inner
        # 17.3k > 13.3k mid size and < 20k ceiling → oversize_fit for mid
        assert cls.best_tier == "mid"
        assert cls.fit == CoinFit.OVERSIZE_FIT

    def test_exact_beats_oversize(self):
        """If coin is oversize_fit for mid AND exact for outer (unlikely but
        possible in weird configs), exact wins."""
        # Construct a tier config where tier sizes overlap to test preference
        tiers = {"tiny": 100, "small": 120}
        cls = classify_coin(120, tiers)
        # 120 is exact for "small", oversize_fit for "tiny" (120 > 100, < 150)
        assert cls.best_tier == "small"
        assert cls.fit == CoinFit.EXACT


class TestLegacyCompat:
    """The compat wrappers preserve behaviour for callers that haven't
    migrated to classify_coin()."""

    def test_is_misfit_compat_matches_classify(self):
        """is_misfit_coin() agrees with classify_coin().is_misfit."""
        # The regression coin
        assert is_misfit_coin(23_400_000, MZ_TIERS) is True
        # An exact inner coin
        assert is_misfit_coin(MZ_TIERS["inner"], MZ_TIERS) is False
        # Dust
        assert is_misfit_coin(100_000, MZ_TIERS) is False  # dust, not misfit
        # Reserve
        assert is_misfit_coin(100_000_000, MZ_TIERS) is False  # reserve, not misfit

    def test_is_misfit_with_custom_ratio(self):
        """Passing a looser ratio accepts more coins (caller-specific)."""
        # With default 1.5 ratio: 23.4k is a misfit
        assert is_misfit_coin(23_400_000, MZ_TIERS) is True
        # With very loose 2.0 ratio and 0.5 floor: now 23.4k passes inner
        # (26.7k × 0.5 = 13.3k floor, 26.7k × 2.0 = 53.4k ceiling, 23.4k is in range)
        assert (
            is_misfit_coin(
                23_400_000,
                MZ_TIERS,
                max_size_ratio=2.0,
                floor_tolerance=0.5,
            )
            is False
        )

    def test_is_misfit_with_inf_ratio_treats_as_large_finite(self):
        """When callers pass float('inf') to disable the ratio guard, we
        should still classify sensibly without divide-by-zero issues."""
        # The infinite-ratio disables the upper bound, so a huge coin at the
        # reserve threshold becomes RESERVE not misfit.
        assert (
            is_misfit_coin(
                500_000_000,
                MZ_TIERS,
                max_size_ratio=float("inf"),
            )
            is False
        )  # it's a reserve candidate, not a misfit


class TestEdgeCases:
    """Degenerate inputs that shouldn't crash."""

    def test_zero_amount_returns_misfit(self):
        cls = classify_coin(0, MZ_TIERS)
        assert cls.is_misfit is True
        assert cls.best_tier is None

    def test_negative_amount_returns_misfit(self):
        cls = classify_coin(-100, MZ_TIERS)
        assert cls.is_misfit is True

    def test_empty_tiers_returns_misfit(self):
        cls = classify_coin(1_000_000, {})
        assert cls.is_misfit is True
        assert cls.best_tier is None
        assert cls.nearest_tier is None

    def test_all_zero_tier_sizes_handled(self):
        """Tiers with 0 size are silently filtered out."""
        cls = classify_coin(1_000_000, {"inner": 0, "mid": 0})
        assert cls.is_misfit is True
        # nothing to classify against — no candidates
        assert cls.candidates == {}

    def test_mixed_zero_and_valid_tiers(self):
        """Zero-sized tiers are filtered, valid ones still work."""
        cls = classify_coin(26_678_000, {"inner": 26_678_000, "broken": 0})
        assert cls.best_tier == "inner"
        assert "broken" not in cls.candidates


class TestDiagnosticNearestTier:
    """`nearest_tier` is for logging/debug — should be populated even
    when `best_tier` is None (misfits)."""

    def test_misfit_still_has_nearest(self):
        cls = classify_coin(23_400_000, MZ_TIERS)
        assert cls.is_misfit is True
        assert cls.nearest_tier == "inner"  # 23.4k is closest to inner (26.7k)

    def test_dust_nearest_is_smallest_tier(self):
        cls = classify_coin(100_000, MZ_TIERS)
        assert cls.designation == CoinDesignation.DUST
        assert cls.nearest_tier == "extreme"  # smallest tier

    def test_reserve_nearest_is_largest_tier(self):
        cls = classify_coin(100_000_000, MZ_TIERS)
        assert cls.designation == CoinDesignation.RESERVE
        assert cls.nearest_tier == "inner"  # largest tier
