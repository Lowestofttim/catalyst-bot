"""Slice 02-13 — ladder_planner.py + ladder_watchdog.py unit tests.

All pure functions — no mocking needed. Tests LadderPlan dataclass methods,
plan_ladder allocation logic, amount_fmt, AuditResult helpers,
audit_ladder_shape shape/inversion/taper logic, and check_coin_invariants.
"""

import unittest
from decimal import Decimal
from typing import List, Dict, Any

try:
    from ladder_planner import (
        SlotStatus,
        SlotPlan,
        LadderPlan,
        plan_ladder,
        amount_fmt,
    )

    _SKIP_LP = None
except ModuleNotFoundError as exc:
    _SKIP_LP = str(exc)

try:
    from ladder_watchdog import (
        Severity,
        Issue,
        AuditResult,
        audit_ladder_shape,
        check_coin_invariants,
    )

    _SKIP_LW = None
except ModuleNotFoundError as exc:
    _SKIP_LW = str(exc)


# ===========================================================================
# amount_fmt
# ===========================================================================


@unittest.skipIf(_SKIP_LP is not None, f"ladder_planner unavailable: {_SKIP_LP}")
class TestAmountFmt(unittest.TestCase):
    def test_xch_range(self):
        s = amount_fmt(2_000_000_000_000)
        self.assertIn("XCH", s)

    def test_cat_range(self):
        s = amount_fmt(5_000_000)
        self.assertIn("CAT", s)

    def test_mojos_range(self):
        s = amount_fmt(999)
        self.assertIn("mojos", s)

    def test_exactly_one_xch(self):
        self.assertEqual(amount_fmt(1_000_000_000_000), "1.0000 XCH")

    def test_zero_mojos(self):
        s = amount_fmt(0)
        self.assertIn("mojos", s)


# ===========================================================================
# LadderPlan dataclass methods
# ===========================================================================


def _make_plan(statuses: List[SlotStatus]) -> LadderPlan:
    plan = LadderPlan(side="buy", mid_price=Decimal("0.001"))
    for i, st in enumerate(statuses):
        plan.slots.append(
            SlotPlan(
                slot_idx=i,
                tier="mid",
                target_size_mojos=1_000_000_000_000,
                target_price=Decimal("0.001"),
                status=st,
            )
        )
    return plan


@unittest.skipIf(_SKIP_LP is not None, f"ladder_planner unavailable: {_SKIP_LP}")
class TestLadderPlanMethods(unittest.TestCase):
    def test_ready_count(self):
        plan = _make_plan(
            [SlotStatus.READY, SlotStatus.READY, SlotStatus.NO_COIN_AVAILABLE]
        )
        self.assertEqual(plan.ready_count(), 2)

    def test_oversize_count(self):
        plan = _make_plan([SlotStatus.OVERSIZE_ACCEPTABLE, SlotStatus.READY])
        self.assertEqual(plan.oversize_count(), 1)

    def test_unready_count(self):
        plan = _make_plan(
            [
                SlotStatus.NO_COIN_AVAILABLE,
                SlotStatus.MISFIT_COIN_AVAILABLE,
                SlotStatus.READY,
            ]
        )
        self.assertEqual(plan.unready_count(), 2)

    def test_is_viable_all_ready(self):
        plan = _make_plan([SlotStatus.READY] * 10)
        self.assertTrue(plan.is_viable())

    def test_is_viable_threshold_met(self):
        # 9 ready, 1 no_coin → 90% → exactly at threshold
        plan = _make_plan([SlotStatus.READY] * 9 + [SlotStatus.NO_COIN_AVAILABLE])
        self.assertTrue(plan.is_viable(min_ready_fraction=0.9))

    def test_is_viable_threshold_not_met(self):
        # 8 ready, 2 no_coin → 80% < 90%
        plan = _make_plan([SlotStatus.READY] * 8 + [SlotStatus.NO_COIN_AVAILABLE] * 2)
        self.assertFalse(plan.is_viable(min_ready_fraction=0.9))

    def test_is_viable_empty_returns_false(self):
        plan = LadderPlan(side="buy", mid_price=Decimal("0.001"))
        self.assertFalse(plan.is_viable())

    def test_summary_keys(self):
        plan = _make_plan([SlotStatus.READY] * 3)
        s = plan.summary()
        self.assertIn("ready", s)
        self.assertIn("total_slots", s)
        self.assertIn("unready", s)
        self.assertEqual(s["total_slots"], 3)


# ===========================================================================
# plan_ladder
# ===========================================================================


def _coin(
    coin_id: str,
    amount_mojos: int,
    designation: str = "tier_spare",
    assigned_tier: str = "mid",
) -> Dict[str, Any]:
    return {
        "coin_id": coin_id,
        "amount_mojos": amount_mojos,
        "designation": designation,
        "assigned_tier": assigned_tier,
    }


_TIER_COUNTS = {"inner": 1, "mid": 2, "outer": 1, "extreme": 0}
_TIER_SIZES = {"inner": 500_000_000, "mid": 1_000_000_000, "outer": 2_000_000_000}
_PRICES = [Decimal("0.001"), Decimal("0.0009"), Decimal("0.0008"), Decimal("0.0007")]


@unittest.skipIf(_SKIP_LP is not None, f"ladder_planner unavailable: {_SKIP_LP}")
class TestPlanLadder(unittest.TestCase):
    def test_all_slots_assigned_when_coins_available(self):
        coins = [
            _coin("c0", 500_000_000, assigned_tier="inner"),
            _coin("c1", 1_000_000_000, assigned_tier="mid"),
            _coin("c2", 1_000_000_000, assigned_tier="mid"),
            _coin("c3", 2_000_000_000, assigned_tier="outer"),
        ]
        plan = plan_ladder(
            side="buy",
            mid_price=Decimal("0.001"),
            tier_counts=_TIER_COUNTS,
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_PRICES,
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count() + plan.oversize_count(), 4)

    def test_empty_coins_all_no_coin(self):
        plan = plan_ladder(
            side="buy",
            mid_price=Decimal("0.001"),
            tier_counts=_TIER_COUNTS,
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_PRICES,
            available_coins=[],
        )
        self.assertEqual(plan.unready_count(), 4)
        self.assertFalse(plan.is_viable())

    def test_consumed_coin_ids_not_reused(self):
        # Only one coin available — can only fill one slot
        coins = [_coin("c0", 1_000_000_000, assigned_tier="mid")]
        plan = plan_ladder(
            side="buy",
            mid_price=Decimal("0.001"),
            tier_counts={"inner": 0, "mid": 3, "outer": 0, "extreme": 0},
            tier_sizes_asset_mojos={"mid": 1_000_000_000},
            slot_prices=[Decimal("0.001"), Decimal("0.0009"), Decimal("0.0008")],
            available_coins=coins,
        )
        # Only 1 coin → only 1 READY slot
        self.assertEqual(plan.ready_count(), 1)
        self.assertEqual(plan.unready_count(), 2)

    def test_is_viable_true_when_enough_coins(self):
        coins = [_coin(f"c{i}", 1_000_000_000, assigned_tier="mid") for i in range(5)]
        plan = plan_ladder(
            side="buy",
            mid_price=Decimal("0.001"),
            tier_counts={"inner": 0, "mid": 5, "outer": 0, "extreme": 0},
            tier_sizes_asset_mojos={"mid": 1_000_000_000},
            slot_prices=[Decimal("0.001")] * 5,
            available_coins=coins,
        )
        self.assertTrue(plan.is_viable())

    def test_needed_reshapes_populated_when_no_coins(self):
        plan = plan_ladder(
            side="buy",
            mid_price=Decimal("0.001"),
            tier_counts={"inner": 2, "mid": 0, "outer": 0, "extreme": 0},
            tier_sizes_asset_mojos={"inner": 500_000_000},
            slot_prices=[Decimal("0.001"), Decimal("0.0009")],
            available_coins=[],
        )
        self.assertEqual(len(plan.needed_reshapes), 1)
        self.assertEqual(plan.needed_reshapes[0]["tier"], "inner")

    def test_plan_side_is_set(self):
        plan = plan_ladder(
            side="sell",
            mid_price=Decimal("0.001"),
            tier_counts={"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
            tier_sizes_asset_mojos={},
            slot_prices=[],
            available_coins=[],
        )
        self.assertEqual(plan.side, "sell")


# ===========================================================================
# AuditResult helpers
# ===========================================================================


@unittest.skipIf(_SKIP_LW is not None, f"ladder_watchdog unavailable: {_SKIP_LW}")
class TestAuditResult(unittest.TestCase):
    def test_ok_true_when_no_issues(self):
        ar = AuditResult(ok=True)
        self.assertFalse(ar.has_errors())
        self.assertFalse(ar.has_warnings())

    def test_has_errors_when_error_issue(self):
        ar = AuditResult(
            ok=False, issues=[Issue(severity=Severity.ERROR, code="x", message="msg")]
        )
        self.assertTrue(ar.has_errors())

    def test_has_warnings_when_warn_issue(self):
        ar = AuditResult(
            ok=True, issues=[Issue(severity=Severity.WARN, code="x", message="msg")]
        )
        self.assertTrue(ar.has_warnings())
        self.assertFalse(ar.has_errors())

    def test_info_is_not_error_or_warning(self):
        ar = AuditResult(
            ok=True, issues=[Issue(severity=Severity.INFO, code="x", message="msg")]
        )
        self.assertFalse(ar.has_errors())
        self.assertFalse(ar.has_warnings())


# ===========================================================================
# audit_ladder_shape
# ===========================================================================


def _offers_with_sizes(prices_and_sizes: List[tuple]) -> List[Dict]:
    """Build fake offer dicts with price and size_xch."""
    return [
        {"trade_id": f"t{i}", "price": str(p), "size_xch": str(s)}
        for i, (p, s) in enumerate(prices_and_sizes)
    ]


@unittest.skipIf(_SKIP_LW is not None, f"ladder_watchdog unavailable: {_SKIP_LW}")
class TestAuditLadderShape(unittest.TestCase):
    def test_empty_offers_no_errors(self):
        result = audit_ladder_shape(
            "buy", [], {}, {"inner": 0, "mid": 0, "outer": 0, "extreme": 0}
        )
        self.assertFalse(result.has_errors())

    def test_correct_count_no_count_warning(self):
        # 2 offers, expected 2
        offers = _offers_with_sizes(
            [
                (Decimal("0.001"), Decimal("0.5")),
                (Decimal("0.0009"), Decimal("1.0")),
            ]
        )
        result = audit_ladder_shape(
            "buy",
            offers,
            {"inner": Decimal("0.5"), "mid": Decimal("1.0")},
            {"inner": 1, "mid": 1, "outer": 0, "extreme": 0},
        )
        codes = [i.code for i in result.issues]
        self.assertNotIn("ladder_count_mismatch", codes)

    def test_count_mismatch_warns(self):
        # Expected 5, got 1
        offers = _offers_with_sizes([(Decimal("0.001"), Decimal("1.0"))])
        result = audit_ladder_shape(
            "buy",
            offers,
            {"inner": Decimal("1.0")},
            {"inner": 5, "mid": 0, "outer": 0, "extreme": 0},
        )
        codes = [i.code for i in result.issues]
        self.assertIn("ladder_count_mismatch", codes)

    def test_size_taper_violation_warns(self):
        # Offer sizes don't match tier sizes (huge drift)
        offers = _offers_with_sizes(
            [
                (Decimal("0.001"), Decimal("9.0")),  # expected inner=0.5
            ]
        )
        result = audit_ladder_shape(
            "buy",
            offers,
            {"inner": Decimal("0.5")},
            {"inner": 1, "mid": 0, "outer": 0, "extreme": 0},
        )
        codes = [i.code for i in result.issues]
        self.assertIn("ladder_size_taper_violated", codes)

    def test_standard_inversion_detected(self):
        # Standard: inner < mid. Give inner=2 XCH, mid=0.5 XCH → ERROR
        offers = _offers_with_sizes(
            [
                (
                    Decimal("0.001"),
                    Decimal("2.0"),
                ),  # inner slot (buy: highest price = innermost)
                (Decimal("0.0009"), Decimal("0.5")),  # mid slot
            ]
        )
        result = audit_ladder_shape(
            "buy",
            offers,
            {"inner": Decimal("2.0"), "mid": Decimal("0.5")},
            {"inner": 1, "mid": 1, "outer": 0, "extreme": 0},
            reversed_ladder=False,
        )
        codes = [i.code for i in result.issues]
        self.assertIn("ladder_inversion_standard", codes)
        self.assertTrue(result.has_errors())

    def test_reverse_inversion_detected(self):
        # Reverse: inner > mid. Give inner=0.5 XCH, mid=2 XCH → ERROR
        offers = _offers_with_sizes(
            [
                (Decimal("0.001"), Decimal("0.5")),  # inner slot (highest price)
                (Decimal("0.0009"), Decimal("2.0")),  # mid slot
            ]
        )
        result = audit_ladder_shape(
            "buy",
            offers,
            {"inner": Decimal("0.5"), "mid": Decimal("2.0")},
            {"inner": 1, "mid": 1, "outer": 0, "extreme": 0},
            reversed_ladder=True,
        )
        codes = [i.code for i in result.issues]
        self.assertIn("ladder_inversion_reverse", codes)

    def test_ok_false_when_has_errors(self):
        # Standard inversion → has_errors → ok=False
        offers = _offers_with_sizes(
            [
                (Decimal("0.001"), Decimal("2.0")),
                (Decimal("0.0009"), Decimal("0.5")),
            ]
        )
        result = audit_ladder_shape(
            "buy",
            offers,
            {"inner": Decimal("2.0"), "mid": Decimal("0.5")},
            {"inner": 1, "mid": 1, "outer": 0, "extreme": 0},
        )
        self.assertFalse(result.ok)


# ===========================================================================
# check_coin_invariants
# ===========================================================================


@unittest.skipIf(_SKIP_LW is not None, f"ladder_watchdog unavailable: {_SKIP_LW}")
class TestCheckCoinInvariants(unittest.TestCase):
    def _clean(self):
        return check_coin_invariants(
            wallet_totals={"xch_total": 10, "cat_total": 20},
            inventory={
                "xch": {"free": 5, "locked": 5},
                "cat": {"free": 15, "locked": 5},
            },
            open_offers_count={"buy": 5, "sell": 5},
            db_locked_count={"xch": 5, "cat": 5},
        )

    def test_all_balanced_no_issues(self):
        result = self._clean()
        self.assertEqual(len(result.issues), 0)
        self.assertTrue(result.ok)

    def test_inventory_mismatch_warns(self):
        result = check_coin_invariants(
            wallet_totals={"xch_total": 10, "cat_total": 20},
            inventory={
                "xch": {"free": 3, "locked": 3},
                "cat": {"free": 15, "locked": 5},
            },
            open_offers_count={"buy": 5, "sell": 5},
            db_locked_count={"xch": 5, "cat": 5},
        )
        codes = [i.code for i in result.issues]
        self.assertIn("inventory_count_mismatch", codes)

    def test_xch_locked_vs_buys_mismatch_warns(self):
        result = check_coin_invariants(
            wallet_totals={"xch_total": 10, "cat_total": 20},
            inventory={
                "xch": {"free": 5, "locked": 5},
                "cat": {"free": 15, "locked": 5},
            },
            open_offers_count={"buy": 1, "sell": 5},  # 1 buy vs 5 locked → diff=4>2
            db_locked_count={"xch": 5, "cat": 5},
        )
        codes = [i.code for i in result.issues]
        self.assertIn("xch_locked_vs_buys_mismatch", codes)

    def test_cat_locked_vs_sells_mismatch_warns(self):
        result = check_coin_invariants(
            wallet_totals={"xch_total": 10, "cat_total": 20},
            inventory={
                "xch": {"free": 5, "locked": 5},
                "cat": {"free": 15, "locked": 5},
            },
            open_offers_count={"buy": 5, "sell": 1},  # 1 sell vs 5 cat locked
            db_locked_count={"xch": 5, "cat": 5},
        )
        codes = [i.code for i in result.issues]
        self.assertIn("cat_locked_vs_sells_mismatch", codes)

    def test_within_tolerance_no_mismatch(self):
        # ±2 is allowed
        result = check_coin_invariants(
            wallet_totals={"xch_total": 10, "cat_total": 20},
            inventory={
                "xch": {"free": 5, "locked": 5},
                "cat": {"free": 15, "locked": 5},
            },
            open_offers_count={"buy": 4, "sell": 4},  # diff=1≤2 → OK
            db_locked_count={"xch": 5, "cat": 5},
        )
        codes = [i.code for i in result.issues]
        self.assertNotIn("xch_locked_vs_buys_mismatch", codes)
        self.assertNotIn("cat_locked_vs_sells_mismatch", codes)

    def test_zero_wallet_total_skips_inventory_check(self):
        # wallet_total=0 → we skip the inventory mismatch check
        result = check_coin_invariants(
            wallet_totals={"xch_total": 0, "cat_total": 0},
            inventory={
                "xch": {"free": 99, "locked": 99},
                "cat": {"free": 99, "locked": 99},
            },
            open_offers_count={"buy": 0, "sell": 0},
            db_locked_count={"xch": 0, "cat": 0},
        )
        codes = [i.code for i in result.issues]
        self.assertNotIn("inventory_count_mismatch", codes)

    def test_summary_contains_expected_keys(self):
        result = self._clean()
        self.assertIn("wallet_totals", result.summary)
        self.assertIn("inventory_totals", result.summary)
        self.assertIn("open_offers", result.summary)


if __name__ == "__main__":
    unittest.main()
