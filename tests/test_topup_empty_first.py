"""Tests for empty-first tier priority in runtime topup.

When a tier has zero free coins, offers on that slot can't be posted.
Single-action topup (one split per 90s cycle) means iteration order
determines which tier gets help first. An empty tier must win over a
partially-filled-but-below-threshold tier.
"""

import unittest


def _empty_first_key_factory(default_order, xch_dist, cat_dist, xch_inv, cat_inv):
    """Mirror of the inline _empty_first_key used in coin_manager._topup_tiers.

    Kept in sync manually. If the production code changes, update here too.
    """

    def key(tier_name):
        xch_slots_n = int(xch_dist.get(tier_name, 0) or 0)
        cat_slots_n = int(cat_dist.get(tier_name, 0) or 0)
        xch_have_n = len(xch_inv.get(tier_name, []))
        cat_have_n = len(cat_inv.get(tier_name, []))
        xch_empty = xch_slots_n > 0 and xch_have_n == 0
        cat_empty = cat_slots_n > 0 and cat_have_n == 0
        empty_rank = 0 if (xch_empty or cat_empty) else 1
        try:
            default_idx = default_order.index(tier_name)
        except ValueError:
            default_idx = 99
        return (empty_rank, default_idx)

    return key


class TestEmptyFirstPriority(unittest.TestCase):
    def setUp(self):
        self.default_order = ["inner", "mid", "outer", "extreme"]
        self.dist = {"inner": 10, "mid": 5, "outer": 3, "extreme": 2}

    def _sort(self, xch_inv, cat_inv):
        key = _empty_first_key_factory(
            self.default_order, self.dist, self.dist, xch_inv, cat_inv
        )
        return sorted(self.default_order, key=key)

    def test_no_empties_preserves_default_order(self):
        inv = {"inner": [1, 2], "mid": [1], "outer": [1], "extreme": [1]}
        self.assertEqual(self._sort(inv, inv), ["inner", "mid", "outer", "extreme"])

    def test_cat_empty_on_mid_wins_over_inner_below_threshold(self):
        # Inner has 3 coins (below threshold but not empty); mid is empty on CAT.
        xch_inv = {"inner": [1, 2, 3], "mid": [1], "outer": [1], "extreme": [1]}
        cat_inv = {"inner": [1, 2, 3], "mid": [], "outer": [1], "extreme": [1]}
        order = self._sort(xch_inv, cat_inv)
        self.assertEqual(order[0], "mid")

    def test_xch_empty_on_outer_wins_over_full_inner(self):
        xch_inv = {"inner": [1, 2, 3], "mid": [1], "outer": [], "extreme": [1]}
        cat_inv = {"inner": [1], "mid": [1], "outer": [1], "extreme": [1]}
        order = self._sort(xch_inv, cat_inv)
        self.assertEqual(order[0], "outer")

    def test_multiple_empties_fall_back_to_default_order(self):
        # Both outer and extreme empty on XCH — default order breaks the tie.
        xch_inv = {"inner": [1], "mid": [1], "outer": [], "extreme": []}
        cat_inv = {"inner": [1], "mid": [1], "outer": [1], "extreme": [1]}
        order = self._sort(xch_inv, cat_inv)
        # outer beats extreme because outer comes first in default_order
        self.assertLess(order.index("outer"), order.index("extreme"))

    def test_reversed_default_preserved_when_all_empty(self):
        reversed_default = ["extreme", "outer", "mid", "inner"]
        xch_inv = {"inner": [], "mid": [], "outer": [], "extreme": []}
        cat_inv = {"inner": [], "mid": [], "outer": [], "extreme": []}
        key = _empty_first_key_factory(
            reversed_default, self.dist, self.dist, xch_inv, cat_inv
        )
        self.assertEqual(sorted(reversed_default, key=key), reversed_default)

    def test_empty_but_tier_unused_does_not_win(self):
        # Tier has 0 slots allocated on either side — empty is irrelevant.
        xch_inv = {"inner": [1], "mid": [1], "outer": [1], "extreme": []}
        cat_inv = {"inner": [1], "mid": [1], "outer": [1], "extreme": []}
        dist = {"inner": 10, "mid": 5, "outer": 3, "extreme": 0}
        key = _empty_first_key_factory(self.default_order, dist, dist, xch_inv, cat_inv)
        order = sorted(self.default_order, key=key)
        # Extreme has slots=0, so it's NOT treated as empty-with-active-slots.
        # Default order applies → inner first.
        self.assertEqual(order[0], "inner")


if __name__ == "__main__":
    unittest.main()
