"""Tests for pool-rebuild source selection — must not poach from below-target tiers.

Previously `_smart_topup_wallet`'s rebuild path treated every tier's coins
beyond `_POOL_REBUILD_KEEP=1` as excess, so a freshly-refilled inner tier
(say 5/10) could be stripped to rebuild CAT-mid — undoing the split we
just completed. A coin should only be eligible for rebuild when the tier
is already AT OR ABOVE its own target.

These tests exercise the sort/filter logic in isolation. The production
implementation is in `coin_manager.py:_smart_topup_wallet` Strategy 3.
Kept in sync manually — update here if the inline logic changes.
"""

import unittest


_POOL_REBUILD_KEEP = 1


def _select_rebuild_candidates(inventory, per_tier_targets):
    """Port of the post-fix rebuild candidate selection.

    A tier contributes coins to the rebuild only if it has >= target. We
    also never let the bucket drop below max(_POOL_REBUILD_KEEP, target).
    """
    candidates = []
    for tname in ["inner", "mid", "outer", "extreme"]:
        bucket = inventory.get(tname, [])
        if not bucket:
            continue
        have = len(bucket)
        target = int(per_tier_targets.get(tname, 0) or 0)
        if target > 0 and have < target:
            continue
        floor = max(_POOL_REBUILD_KEEP, target)
        take = max(0, have - floor)
        candidates.extend(bucket[:take])
    return candidates


class PoolRebuildTargetRespectTests(unittest.TestCase):
    def setUp(self):
        # Typical CAT targets from Smart Settings moderate fill rate.
        self.targets = {"inner": 10, "mid": 5, "outer": 3, "extreme": 2}

    def _make_bucket(self, n):
        return [{"coin_id": f"0x{i:064x}"} for i in range(n)]

    def test_refilled_inner_below_target_is_not_poached(self):
        # The bug scenario: inner was just refilled to 5, target is 10.
        inv = {
            "inner": self._make_bucket(5),
            "mid": self._make_bucket(1),
            "outer": self._make_bucket(1),
            "extreme": self._make_bucket(1),
        }
        candidates = _select_rebuild_candidates(inv, self.targets)
        # All 4 tiers are below target — no candidates.
        self.assertEqual(candidates, [])

    def test_tier_at_target_contributes_excess_only(self):
        # inner has exactly target (10) — may contribute only the amount
        # above `floor = max(keep, target) = target`.
        inv = {
            "inner": self._make_bucket(10),
            "mid": self._make_bucket(5),
            "outer": self._make_bucket(3),
            "extreme": self._make_bucket(2),
        }
        candidates = _select_rebuild_candidates(inv, self.targets)
        # No tier is ABOVE target → zero excess.
        self.assertEqual(candidates, [])

    def test_tier_above_target_contributes_excess(self):
        # inner at 12, target 10 → 2 excess.
        inv = {
            "inner": self._make_bucket(12),
            "mid": self._make_bucket(5),
            "outer": self._make_bucket(3),
            "extreme": self._make_bucket(2),
        }
        candidates = _select_rebuild_candidates(inv, self.targets)
        self.assertEqual(len(candidates), 2)

    def test_sniper_and_fees_never_contribute_even_if_present(self):
        # The production loop iterates only over ["inner","mid","outer","extreme"].
        # sniper/fees coins in `inventory` must never be consumed.
        inv = {
            "inner": self._make_bucket(12),
            "mid": self._make_bucket(5),
            "outer": self._make_bucket(3),
            "extreme": self._make_bucket(2),
            "sniper": self._make_bucket(25),
            "fees": self._make_bucket(50),
        }
        candidates = _select_rebuild_candidates(inv, self.targets)
        # Only the 2 excess inner coins qualify.
        self.assertEqual(len(candidates), 2)

    def test_no_target_info_falls_back_to_keep_only(self):
        # Empty targets dict → target defaults to 0 → `have < target` skipped
        # (0 < 0 is False), and `floor = max(KEEP, 0) = 1` → legacy behaviour.
        inv = {
            "inner": self._make_bucket(5),
            "mid": self._make_bucket(3),
            "outer": self._make_bucket(2),
            "extreme": self._make_bucket(1),
        }
        candidates = _select_rebuild_candidates(inv, {})
        # (5-1) + (3-1) + (2-1) + (1-1) = 4 + 2 + 1 + 0 = 7
        self.assertEqual(len(candidates), 7)

    def test_mixed_healthy_and_needy_only_healthy_contributes(self):
        # inner=12 (above 10 target), mid=2 (below 5 target), others healthy.
        inv = {
            "inner": self._make_bucket(12),  # +2 excess
            "mid": self._make_bucket(2),  # below target → skip
            "outer": self._make_bucket(4),  # +1 excess (target 3)
            "extreme": self._make_bucket(2),  # at target → 0 excess
        }
        candidates = _select_rebuild_candidates(inv, self.targets)
        self.assertEqual(len(candidates), 3)


if __name__ == "__main__":
    unittest.main()
