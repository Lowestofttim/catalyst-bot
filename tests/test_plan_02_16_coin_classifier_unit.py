"""Slice 02-16 — coin_classifier.py unit tests.

Pure module (no cfg/HTTP/DB). Tests classify_coin across all
designation outcomes (dust, reserve, exact, oversize, misfit),
is_misfit_coin convenience wrapper, and infer_designation_by_size.
"""

import unittest
from decimal import Decimal

try:
    from coin_classifier import (
        CoinFit,
        CoinDesignation,
        CoinClassification,
        classify_coin,
        is_misfit_coin,
        infer_designation_by_size,
        DEFAULT_FLOOR_TOLERANCE,
        DEFAULT_MAX_RATIO,
        DEFAULT_DUST_FRACTION,
        DEFAULT_RESERVE_MULTIPLE,
    )

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

# Realistic tier sizes (mojos):  inner=1 XCH, mid=2 XCH, outer=4 XCH, extreme=8 XCH
_TIERS = {
    "inner": 1_000_000_000_000,
    "mid": 2_000_000_000_000,
    "outer": 4_000_000_000_000,
    "extreme": 8_000_000_000_000,
}

# Pre-computed thresholds for clarity
_INNER_FLOOR = int(Decimal("1000000000000") * DEFAULT_FLOOR_TOLERANCE)  # 980B
_INNER_CEILING = int(Decimal("1000000000000") * DEFAULT_MAX_RATIO)  # 1.5T
_DUST_THRESHOLD = int(Decimal("1000000000000") * DEFAULT_DUST_FRACTION)  # 500B
_RESERVE_THRESH = int(Decimal("8000000000000") * DEFAULT_RESERVE_MULTIPLE)  # 16T


# ===========================================================================
# classify_coin — edge / boundary cases
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinEdgeCases(unittest.TestCase):
    def test_zero_amount_is_misfit(self):
        cls = classify_coin(0, _TIERS)
        self.assertTrue(cls.is_misfit)
        self.assertIsNone(cls.best_tier)

    def test_negative_amount_is_misfit(self):
        cls = classify_coin(-1, _TIERS)
        self.assertTrue(cls.is_misfit)

    def test_empty_tier_dict_is_misfit(self):
        cls = classify_coin(1_000_000_000_000, {})
        self.assertTrue(cls.is_misfit)

    def test_all_zero_tier_sizes_filtered_out(self):
        cls = classify_coin(1_000_000_000_000, {"inner": 0, "mid": 0})
        self.assertTrue(cls.is_misfit)


# ===========================================================================
# classify_coin — dust
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinDust(unittest.TestCase):
    def test_below_dust_threshold_is_dust(self):
        cls = classify_coin(_DUST_THRESHOLD - 1, _TIERS)
        self.assertEqual(cls.designation, CoinDesignation.DUST)
        self.assertFalse(cls.is_misfit)
        self.assertIsNone(cls.best_tier)

    def test_dust_nearest_tier_is_smallest(self):
        cls = classify_coin(100_000_000_000, _TIERS)
        self.assertEqual(cls.nearest_tier, "inner")

    def test_at_dust_threshold_is_not_dust(self):
        cls = classify_coin(_DUST_THRESHOLD, _TIERS)
        self.assertNotEqual(cls.designation, CoinDesignation.DUST)


# ===========================================================================
# classify_coin — reserve
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinReserve(unittest.TestCase):
    def test_above_reserve_threshold_is_reserve(self):
        # Strict `>` in classifier: exactly at threshold stays OVERSIZE_FIT,
        # strictly above falls into RESERVE.
        cls = classify_coin(_RESERVE_THRESH + 1, _TIERS)
        self.assertEqual(cls.designation, CoinDesignation.RESERVE)
        self.assertFalse(cls.is_misfit)
        self.assertIsNone(cls.best_tier)

    def test_reserve_nearest_tier_is_largest(self):
        cls = classify_coin(_RESERVE_THRESH + 1, _TIERS)
        self.assertEqual(cls.nearest_tier, "extreme")

    def test_just_below_reserve_threshold_is_not_reserve(self):
        cls = classify_coin(_RESERVE_THRESH - 1, _TIERS)
        self.assertNotEqual(cls.designation, CoinDesignation.RESERVE)


# ===========================================================================
# classify_coin — exact match
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinExact(unittest.TestCase):
    def test_exact_inner_size_matches_inner(self):
        cls = classify_coin(1_000_000_000_000, _TIERS)
        self.assertEqual(cls.fit, CoinFit.EXACT)
        self.assertEqual(cls.best_tier, "inner")
        self.assertEqual(cls.designation, CoinDesignation.TIER_SPARE)
        self.assertFalse(cls.is_misfit)

    def test_exact_mid_size(self):
        cls = classify_coin(2_000_000_000_000, _TIERS)
        self.assertEqual(cls.fit, CoinFit.EXACT)
        self.assertEqual(cls.best_tier, "mid")
        self.assertEqual(cls.designation, CoinDesignation.TIER_SPARE)

    def test_at_inner_floor_is_exact(self):
        cls = classify_coin(_INNER_FLOOR, _TIERS)
        self.assertEqual(cls.fit, CoinFit.EXACT)
        self.assertEqual(cls.best_tier, "inner")


# ===========================================================================
# classify_coin — oversize fit
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinOversize(unittest.TestCase):
    def test_slightly_over_inner_is_oversize(self):
        # 1.2 XCH: > inner (1T) but <= inner ceiling (1.5T), also under mid floor (1.96T)
        cls = classify_coin(1_200_000_000_000, _TIERS)
        self.assertEqual(cls.fit, CoinFit.OVERSIZE_FIT)
        self.assertEqual(cls.best_tier, "inner")
        self.assertFalse(cls.is_misfit)

    def test_at_inner_ceiling_is_oversize(self):
        cls = classify_coin(_INNER_CEILING, _TIERS)
        self.assertEqual(cls.fit, CoinFit.OVERSIZE_FIT)
        self.assertEqual(cls.best_tier, "inner")


# ===========================================================================
# classify_coin — misfit (between tiers)
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinMisfit(unittest.TestCase):
    def test_between_inner_ceiling_and_mid_floor_is_misfit(self):
        # 1.7 XCH: > inner ceiling (1.5T), < mid floor (1.96T)
        cls = classify_coin(1_700_000_000_000, _TIERS)
        self.assertTrue(cls.is_misfit)
        self.assertIsNone(cls.best_tier)
        self.assertEqual(cls.designation, CoinDesignation.UNKNOWN)

    def test_just_below_inner_floor_is_misfit(self):
        # Just below 98% of inner — not dust (above 50%), not fitting any tier
        cls = classify_coin(_INNER_FLOOR - 1, _TIERS)
        self.assertTrue(cls.is_misfit)

    def test_misfit_has_candidates_populated(self):
        cls = classify_coin(1_700_000_000_000, _TIERS)
        self.assertGreater(len(cls.candidates), 0)


# ===========================================================================
# classify_coin — prefer smallest exact match
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestClassifyCoinBestTierPreference(unittest.TestCase):
    def test_exact_match_preferred_over_oversize(self):
        # With 2 tiers where coin fits inner exactly and mid oversize,
        # exact should win
        tiers = {"inner": 1_000_000_000_000, "mid": 2_000_000_000_000}
        cls = classify_coin(1_000_000_000_000, tiers)
        self.assertEqual(cls.best_tier, "inner")
        self.assertEqual(cls.fit, CoinFit.EXACT)

    def test_smallest_exact_match_used(self):
        # Coin that is exactly inner size — should use inner, not mid (even if mid also accepts it)
        tiers = {"inner": 1_000_000_000_000, "mid": 2_000_000_000_000}
        cls = classify_coin(1_000_000_000_000, tiers)
        self.assertEqual(cls.best_tier, "inner")


# ===========================================================================
# is_misfit_coin
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestIsMisfitCoin(unittest.TestCase):
    def test_exact_match_is_not_misfit(self):
        self.assertFalse(is_misfit_coin(1_000_000_000_000, _TIERS))

    def test_between_tier_is_misfit(self):
        self.assertTrue(is_misfit_coin(1_700_000_000_000, _TIERS))

    def test_dust_is_not_misfit(self):
        self.assertFalse(is_misfit_coin(100_000_000_000, _TIERS))

    def test_reserve_is_not_misfit(self):
        self.assertFalse(is_misfit_coin(_RESERVE_THRESH, _TIERS))

    def test_infinity_max_ratio_handled(self):
        # Should not raise or hang
        result = is_misfit_coin(1_000_000_000_000, _TIERS, max_size_ratio=float("inf"))
        self.assertIsInstance(result, bool)


# ===========================================================================
# infer_designation_by_size
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_classifier unavailable: {_SKIP}")
class TestInferDesignationBySize(unittest.TestCase):
    def test_exact_inner_returns_tier_spare_inner(self):
        desig, tier = infer_designation_by_size(1_000_000_000_000, _TIERS)
        self.assertEqual(desig, "tier_spare")
        self.assertEqual(tier, "inner")

    def test_dust_returns_dust_none(self):
        desig, tier = infer_designation_by_size(100_000_000_000, _TIERS)
        self.assertEqual(desig, "dust")
        self.assertEqual(tier, "none")

    def test_reserve_returns_reserve_none(self):
        desig, tier = infer_designation_by_size(_RESERVE_THRESH + 1, _TIERS)
        self.assertEqual(desig, "reserve")
        self.assertEqual(tier, "none")

    def test_misfit_returns_unknown_none(self):
        desig, tier = infer_designation_by_size(1_700_000_000_000, _TIERS)
        self.assertEqual(desig, "unknown")
        self.assertEqual(tier, "none")


if __name__ == "__main__":
    unittest.main()
