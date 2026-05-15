"""Slice 03-07 — ladder creation integration test.

Tests the full flow: coins in DB → get_free_coins() → plan_ladder() →
slot assignments verified. Confirms the ladder planner correctly maps
DB-backed coins to tier slots, handles shortfalls, and marks consumed coins.

Uses real SQLite temp DB. plan_ladder() is pure so no wallet mocking needed.
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db
    from database import upsert_coin, get_free_coins, init_database

    _SKIP_DB = None
except ModuleNotFoundError as exc:
    _db = None
    _SKIP_DB = str(exc)

try:
    from ladder_planner import plan_ladder, SlotStatus, LadderPlan

    _SKIP_LP = None
except ModuleNotFoundError as exc:
    plan_ladder = None
    _SKIP_LP = str(exc)


# ---------------------------------------------------------------------------
# Tier size constants (mojos) — match typical smart-defaults values
# ---------------------------------------------------------------------------

_INNER_XCH = 1_000_000_000_000  # 1 XCH
_MID_XCH = 2_000_000_000_000  # 2 XCH
_OUTER_XCH = 4_000_000_000_000  # 4 XCH
_EXTREME_XCH = 8_000_000_000_000  # 8 XCH

_TIER_SIZES = {
    "inner": _INNER_XCH,
    "mid": _MID_XCH,
    "outer": _OUTER_XCH,
    "extreme": _EXTREME_XCH,
}

_MID_PRICE = Decimal("0.001")  # 0.001 XCH per CAT


def _slot_prices(n: int) -> list:
    """Return n fake slot prices (not used for slot logic, just required)."""
    return [_MID_PRICE * (Decimal("1") - Decimal("0.001") * i) for i in range(n)]


# ---------------------------------------------------------------------------
# Temp-DB base class
# ---------------------------------------------------------------------------


class _TempDB(unittest.TestCase):
    def setUp(self):
        sys.modules["database"] = _db
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path
        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.init_database()

    def tearDown(self):
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path
        sys.modules["database"] = _db
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass

    def _add_xch_coin(self, coin_id: str, amount: int, tier: str = "inner"):
        upsert_coin(
            coin_id, "xch", amount, tier=tier, designation="tier_trading", status="free"
        )


# ---------------------------------------------------------------------------
# 1. DB → get_free_coins → plan_ladder wiring
# ---------------------------------------------------------------------------


@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_LP is not None,
    f"dependencies unavailable: db={_SKIP_DB} lp={_SKIP_LP}",
)
class TestLadderCreationFromDB(_TempDB):
    def test_empty_db_produces_all_no_coin_slots(self):
        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 3},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=coins,
        )
        self.assertEqual(len(plan.slots), 3)
        for s in plan.slots:
            self.assertEqual(s.status, SlotStatus.NO_COIN_AVAILABLE)
        self.assertFalse(plan.is_viable())

    def test_exact_coins_produce_all_ready_slots(self):
        # Add one inner-tier coin per slot
        for i in range(3):
            self._add_xch_coin(f"0xinner{i}", _INNER_XCH, "inner")

        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 3},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count(), 3)
        self.assertTrue(plan.is_viable())
        self.assertEqual(len(plan.consumed_coin_ids), 3)

    def test_consumed_coin_ids_match_db_coin_ids(self):
        coin_ids = [f"0xcoin{i}" for i in range(2)]
        for cid in coin_ids:
            self._add_xch_coin(cid, _INNER_XCH, "inner")

        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 2},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(2),
            available_coins=coins,
        )
        self.assertEqual(set(plan.consumed_coin_ids), {c.lower() for c in coin_ids})

    def test_mixed_tiers_assigned_to_correct_slots(self):
        self._add_xch_coin("0xinner_a", _INNER_XCH, "inner")
        self._add_xch_coin("0xmid_a", _MID_XCH, "mid")
        self._add_xch_coin("0xouter_a", _OUTER_XCH, "outer")

        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 1, "mid": 1, "outer": 1},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count(), 3)
        tiers_assigned = [s.tier for s in plan.slots]
        self.assertEqual(tiers_assigned, ["inner", "mid", "outer"])

    def test_shortfall_in_one_tier_produces_no_coin_slot(self):
        # Supply inner coins but no mid coins
        self._add_xch_coin("0xi0", _INNER_XCH, "inner")
        self._add_xch_coin("0xi1", _INNER_XCH, "inner")

        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 2, "mid": 1},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count(), 2)
        self.assertEqual(plan.unready_count(), 1)
        # Shortfall registered
        self.assertEqual(len(plan.needed_reshapes), 1)
        self.assertEqual(plan.needed_reshapes[0]["tier"], "mid")

    def test_more_coins_than_slots_only_consumes_needed(self):
        # 5 inner coins but only 2 inner slots
        for i in range(5):
            self._add_xch_coin(f"0xextra{i}", _INNER_XCH, "inner")

        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 2},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(2),
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count(), 2)
        self.assertEqual(len(plan.consumed_coin_ids), 2)

    def test_coin_not_double_consumed_across_slots(self):
        # Only 1 coin but 3 slots — remaining should be NO_COIN
        self._add_xch_coin("0xonly_one", _INNER_XCH, "inner")

        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 3},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count(), 1)
        self.assertEqual(plan.unready_count(), 2)
        self.assertEqual(len(plan.consumed_coin_ids), 1)

    def test_sell_side_uses_cat_coins(self):
        upsert_coin(
            "0xcat_a",
            "cat",
            1_000_000,
            tier="inner",
            designation="tier_trading",
            status="free",
        )

        cat_tier_sizes = {
            "inner": 1_000_000,
            "mid": 2_000_000,
            "outer": 4_000_000,
            "extreme": 8_000_000,
        }
        coins = get_free_coins("cat")
        plan = plan_ladder(
            side="sell",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 1},
            tier_sizes_asset_mojos=cat_tier_sizes,
            slot_prices=_slot_prices(1),
            available_coins=coins,
        )
        self.assertEqual(plan.ready_count(), 1)
        self.assertEqual(plan.slots[0].coin_id, "0xcat_a")


# ---------------------------------------------------------------------------
# 2. Plan viability and summary
# ---------------------------------------------------------------------------


@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_LP is not None,
    f"dependencies unavailable: db={_SKIP_DB} lp={_SKIP_LP}",
)
class TestLadderPlanViability(_TempDB):
    def test_fully_stocked_plan_is_viable(self):
        for i in range(4):
            self._add_xch_coin(f"0xfull{i}", _INNER_XCH, "inner")
        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 4},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(4),
            available_coins=coins,
        )
        self.assertTrue(plan.is_viable())

    def test_empty_plan_is_not_viable(self):
        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 4},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(4),
            available_coins=coins,
        )
        self.assertFalse(plan.is_viable())

    def test_ninety_percent_threshold_respected(self):
        # 9 of 10 slots filled → viable (≥90%)
        for i in range(9):
            self._add_xch_coin(f"0xn{i}", _INNER_XCH, "inner")
        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 10},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(10),
            available_coins=coins,
        )
        # 9/10 = 90% ≥ 90% threshold
        self.assertTrue(plan.is_viable(min_ready_fraction=0.9))

    def test_below_threshold_not_viable(self):
        # 8 of 10 slots filled → not viable at 90%
        for i in range(8):
            self._add_xch_coin(f"0xm{i}", _INNER_XCH, "inner")
        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 10},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(10),
            available_coins=coins,
        )
        self.assertFalse(plan.is_viable(min_ready_fraction=0.9))

    def test_summary_totals_correct(self):
        for i in range(3):
            self._add_xch_coin(f"0xs{i}", _INNER_XCH, "inner")
        coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 3, "mid": 2},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(5),
            available_coins=coins,
        )
        s = plan.summary()
        self.assertEqual(s["total_slots"], 5)
        self.assertEqual(s["ready"], 3)
        self.assertEqual(s["unready"], 2)
        self.assertEqual(s["side"], "buy")


# ---------------------------------------------------------------------------
# 3. Full bot-start cycle: DB populated → plan → verify no double-allocation
# ---------------------------------------------------------------------------


@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_LP is not None,
    f"dependencies unavailable: db={_SKIP_DB} lp={_SKIP_LP}",
)
class TestBotStartLadderCycle(_TempDB):
    """Simulates a bot-start scenario: two-sided ladder planning from DB."""

    def test_buy_and_sell_plans_use_separate_coin_pools(self):
        # XCH coins for buy ladder
        for i in range(3):
            self._add_xch_coin(f"0xbuy{i}", _INNER_XCH, "inner")
        # CAT coins for sell ladder
        cat_tier_sizes = {"inner": 1_000_000}
        for i in range(3):
            upsert_coin(
                f"0xsell{i}",
                "cat",
                1_000_000,
                tier="inner",
                designation="tier_trading",
                status="free",
            )

        buy_coins = get_free_coins("xch")
        sell_coins = get_free_coins("cat")

        buy_plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 3},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=buy_coins,
        )
        sell_plan = plan_ladder(
            side="sell",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 3},
            tier_sizes_asset_mojos=cat_tier_sizes,
            slot_prices=_slot_prices(3),
            available_coins=sell_coins,
        )

        self.assertEqual(buy_plan.ready_count(), 3)
        self.assertEqual(sell_plan.ready_count(), 3)
        # No coin overlap between sides
        buy_ids = set(buy_plan.consumed_coin_ids)
        sell_ids = set(sell_plan.consumed_coin_ids)
        self.assertEqual(buy_ids & sell_ids, set())

    def test_tier_filtered_query_matches_plan_input(self):
        # Add coins at various tiers
        self._add_xch_coin("0xi0", _INNER_XCH, "inner")
        self._add_xch_coin("0xm0", _MID_XCH, "mid")
        self._add_xch_coin("0xo0", _OUTER_XCH, "outer")

        all_coins = get_free_coins("xch")
        plan = plan_ladder(
            side="buy",
            mid_price=_MID_PRICE,
            tier_counts={"inner": 1, "mid": 1, "outer": 1},
            tier_sizes_asset_mojos=_TIER_SIZES,
            slot_prices=_slot_prices(3),
            available_coins=all_coins,
        )
        # All 3 slots filled, each with the matching tier coin
        self.assertTrue(plan.is_viable())
        assigned = {s.tier: s.coin_id for s in plan.slots if s.coin_id}
        self.assertEqual(assigned["inner"], "0xi0")
        self.assertEqual(assigned["mid"], "0xm0")
        self.assertEqual(assigned["outer"], "0xo0")


if __name__ == "__main__":
    unittest.main()
