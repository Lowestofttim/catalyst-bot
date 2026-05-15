"""Tests for the pre-flight ladder planner."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from decimal import Decimal

from ladder_planner import (
    plan_ladder,
    SlotStatus,
)


# Test fixture: MZ_XCH reverse-ladder configuration (CAT denominated)
MZ_TIER_SIZES = {
    "inner": 26_678_000,
    "mid": 13_339_000,
    "outer": 5_802_000,
    "extreme": 2_901_000,
}
MZ_TIER_COUNTS = {"inner": 10, "mid": 5, "outer": 3, "extreme": 2}

# Slot prices (20 total, ascending for sell side, one per slot in inner→extreme order)
MZ_SELL_PRICES = [Decimal("0.000127") + Decimal("0.00000001") * i for i in range(20)]


def _coin(
    coin_id: str, amount: int, tier: str = "inner", designation: str = "tier_spare"
) -> dict:
    return {
        "coin_id": coin_id,
        "amount_mojos": amount,
        "designation": designation,
        "assigned_tier": tier,
    }


class TestHealthyPlan:
    """When all tier-correct coins are available, the plan is fully viable."""

    def test_full_ladder_all_ready(self):
        coins = []
        # 10 inner + 5 mid + 3 outer + 2 extreme (exact tier sizes)
        for i in range(10):
            coins.append(_coin(f"i_{i}", MZ_TIER_SIZES["inner"], "inner"))
        for i in range(5):
            coins.append(_coin(f"m_{i}", MZ_TIER_SIZES["mid"], "mid"))
        for i in range(3):
            coins.append(_coin(f"o_{i}", MZ_TIER_SIZES["outer"], "outer"))
        for i in range(2):
            coins.append(_coin(f"e_{i}", MZ_TIER_SIZES["extreme"], "extreme"))

        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=coins,
        )

        assert len(plan.slots) == 20
        assert plan.ready_count() == 20
        assert plan.unready_count() == 0
        assert plan.is_viable() is True
        assert len(plan.consumed_coin_ids) == 20


class TestMisfitRejection:
    """Tonight's regression scenario: inner coins are 23.4k (misfits under
    0.98/1.5 bounds). Planner must reject them and mark inner slots as
    needing reshape."""

    def test_regression_23_4k_inner_coins_rejected(self):
        coins = []
        # 10 "inner" coins at 23.4k mojos — misfit under strict bounds
        # (0.98 floor = 26.16k; 23.4k is below)
        for i in range(10):
            coins.append(_coin(f"misfit_{i}", 23_400_000, "inner"))
        # Correct mid/outer/extreme
        for i in range(5):
            coins.append(_coin(f"m_{i}", MZ_TIER_SIZES["mid"], "mid"))
        for i in range(3):
            coins.append(_coin(f"o_{i}", MZ_TIER_SIZES["outer"], "outer"))
        for i in range(2):
            coins.append(_coin(f"e_{i}", MZ_TIER_SIZES["extreme"], "extreme"))

        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=coins,
            reject_misfit_coins=True,
        )

        # All 10 inner slots should be NO_COIN_AVAILABLE (misfit 23.4k coins
        # were rejected). Mid/outer/extreme are fine.
        inner_slots = [s for s in plan.slots if s.tier == "inner"]
        assert all(s.status == SlotStatus.NO_COIN_AVAILABLE for s in inner_slots)
        assert plan.ready_count() == 10  # mid + outer + extreme
        # Plan NOT viable (only 10/20 = 50% < 90% threshold)
        assert plan.is_viable() is False
        # Should report an "inner tier reshape needed" entry
        reshape_tiers = [r["tier"] for r in plan.needed_reshapes]
        assert "inner" in reshape_tiers
        # Find the shortfall
        for r in plan.needed_reshapes:
            if r["tier"] == "inner":
                assert r["shortfall"] == 10

    def test_reject_misfit_false_uses_them_with_flag(self):
        """When opt-in, misfits get used but slot.status is MISFIT_COIN_AVAILABLE
        so the caller knows the ladder will be off-shape."""
        coins = [_coin(f"misfit_{i}", 23_400_000, "inner") for i in range(10)]
        for i in range(5):
            coins.append(_coin(f"m_{i}", MZ_TIER_SIZES["mid"], "mid"))
        for i in range(3):
            coins.append(_coin(f"o_{i}", MZ_TIER_SIZES["outer"], "outer"))
        for i in range(2):
            coins.append(_coin(f"e_{i}", MZ_TIER_SIZES["extreme"], "extreme"))

        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=coins,
            reject_misfit_coins=False,
        )

        inner_slots = [s for s in plan.slots if s.tier == "inner"]
        # Misfits were used but flagged
        assert all(s.status == SlotStatus.MISFIT_COIN_AVAILABLE for s in inner_slots)
        assert all(s.coin_id is not None for s in inner_slots)


class TestOversizeFit:
    """Coins slightly oversize (within the 1.5 ceiling) are OVERSIZE_ACCEPTABLE,
    not rejected — tolerates change-coin slack from past fills."""

    def test_1_2x_inner_coin_accepted_as_oversize(self):
        # 1.2× inner size: 32.01M mojos (within 1.5× ceiling of 40.0M)
        over_size = int(MZ_TIER_SIZES["inner"] * 1.2)
        coins = [_coin(f"over_{i}", over_size, "inner") for i in range(10)]
        for i in range(5):
            coins.append(_coin(f"m_{i}", MZ_TIER_SIZES["mid"], "mid"))
        for i in range(3):
            coins.append(_coin(f"o_{i}", MZ_TIER_SIZES["outer"], "outer"))
        for i in range(2):
            coins.append(_coin(f"e_{i}", MZ_TIER_SIZES["extreme"], "extreme"))

        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=coins,
        )
        inner_slots = [s for s in plan.slots if s.tier == "inner"]
        assert all(s.status == SlotStatus.OVERSIZE_ACCEPTABLE for s in inner_slots)
        # Plan is still viable — oversize-fit counts toward healthy
        assert plan.is_viable() is True


class TestViabilityThreshold:
    """Plan viability respects the 90% default threshold."""

    def test_80_percent_ready_not_viable_by_default(self):
        coins = []
        # 8 inner + 5 mid + 3 outer + 2 extreme = 18 (of 20 = 90%)
        for i in range(8):
            coins.append(_coin(f"i_{i}", MZ_TIER_SIZES["inner"], "inner"))
        for i in range(5):
            coins.append(_coin(f"m_{i}", MZ_TIER_SIZES["mid"], "mid"))
        for i in range(3):
            coins.append(_coin(f"o_{i}", MZ_TIER_SIZES["outer"], "outer"))
        for i in range(2):
            coins.append(_coin(f"e_{i}", MZ_TIER_SIZES["extreme"], "extreme"))

        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=coins,
        )
        assert plan.ready_count() == 18
        # 18/20 = 90% — at threshold, is_viable() True
        assert plan.is_viable() is True

    def test_50_percent_ready_not_viable(self):
        coins = []
        for i in range(5):
            coins.append(_coin(f"i_{i}", MZ_TIER_SIZES["inner"], "inner"))
        for i in range(3):
            coins.append(_coin(f"m_{i}", MZ_TIER_SIZES["mid"], "mid"))
        # Missing outer + extreme
        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=coins,
        )
        assert plan.is_viable() is False


class TestSummary:
    """summary() dict shape for logging."""

    def test_summary_has_expected_keys(self):
        plan = plan_ladder(
            side="buy",
            mid_price=Decimal("0.00012617"),
            tier_counts=MZ_TIER_COUNTS,
            tier_sizes_asset_mojos=MZ_TIER_SIZES,
            slot_prices=MZ_SELL_PRICES,
            available_coins=[],
        )
        s = plan.summary()
        assert s["side"] == "buy"
        assert "total_slots" in s
        assert "ready" in s
        assert "unready" in s
        assert "blockers" in s
