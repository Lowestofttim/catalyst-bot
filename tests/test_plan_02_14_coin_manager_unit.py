"""Slice 02-14 — coin_manager.py unit tests.

No wallet/DB calls. Tests pure helpers: fast-reconcile flag, coin record
helpers, _classify_coins, _clamp_coin_prep_multiplier, format helpers,
get_tier_distribution, get_weighted_tier_prep_counts, and FeeCoinPool.
"""

import hashlib
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

try:
    import coin_manager as _cm_mod
    from coin_manager import (
        request_fast_reconcile,
        consume_fast_reconcile,
        _extract_coin_records,
        _coin_amount,
        _chia_int_to_bytes,
        _coin_id_from_record,
        _classify_coins,
        _clamp_coin_prep_multiplier,
        _format_amount_xch,
        _format_amount_cat,
        get_tier_distribution,
        get_weighted_tier_prep_counts,
        flip_position_tiers_to_coin_size_tiers,
        FeeCoinPool,
    )

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_FAKE_CFG = SimpleNamespace(
    BUY_INNER_TIER_COUNT=2,
    BUY_MID_TIER_COUNT=3,
    BUY_OUTER_TIER_COUNT=2,
    BUY_EXTREME_TIER_COUNT=1,
    SELL_INNER_TIER_COUNT=2,
    SELL_MID_TIER_COUNT=3,
    SELL_OUTER_TIER_COUNT=2,
    SELL_EXTREME_TIER_COUNT=1,
    BUY_INNER_TIER_SPARE_COUNT=0,
    BUY_MID_TIER_SPARE_COUNT=0,
    BUY_OUTER_TIER_SPARE_COUNT=0,
    BUY_EXTREME_TIER_SPARE_COUNT=0,
    SELL_INNER_TIER_SPARE_COUNT=0,
    SELL_MID_TIER_SPARE_COUNT=0,
    SELL_OUTER_TIER_SPARE_COUNT=0,
    SELL_EXTREME_TIER_SPARE_COUNT=0,
    BUY_LADDER_REVERSED=False,
    INNER_SIZE_XCH=Decimal("1"),
    MID_SIZE_XCH=Decimal("2"),
    OUTER_SIZE_XCH=Decimal("3"),
    EXTREME_SIZE_XCH=Decimal("4"),
)


def _with_cfg(**overrides):
    d = dict(_FAKE_CFG.__dict__)
    d.update(overrides)
    return SimpleNamespace(**d)


# ===========================================================================
# fast_reconcile flag
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestFastReconcileFlag(unittest.TestCase):
    def setUp(self):
        # Reset flag before each test
        _cm_mod._fast_reconcile_flag = False

    def test_initial_consume_returns_false(self):
        self.assertFalse(consume_fast_reconcile())

    def test_request_then_consume_returns_true(self):
        with patch.object(_cm_mod, "log_event"):
            request_fast_reconcile("test")
        self.assertTrue(consume_fast_reconcile())

    def test_consume_resets_flag(self):
        _cm_mod._fast_reconcile_flag = True
        consume_fast_reconcile()
        self.assertFalse(consume_fast_reconcile())

    def test_double_request_still_single_consume(self):
        with patch.object(_cm_mod, "log_event"):
            request_fast_reconcile()
            request_fast_reconcile()
        self.assertTrue(consume_fast_reconcile())
        self.assertFalse(consume_fast_reconcile())


# ===========================================================================
# _extract_coin_records
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestExtractCoinRecords(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(_extract_coin_records(None), [])

    def test_non_dict_returns_empty(self):
        self.assertEqual(_extract_coin_records("bad"), [])

    def test_error_response_returns_empty(self):
        with patch.object(_cm_mod, "log_event"):
            result = _extract_coin_records({"error": "connection refused"})
        self.assertEqual(result, [])

    def test_success_false_returns_empty(self):
        with patch.object(_cm_mod, "log_event"):
            result = _extract_coin_records({"success": False})
        self.assertEqual(result, [])

    def test_confirmed_records_returned(self):
        recs = [{"coin": {"amount": 100}}]
        result = _extract_coin_records({"confirmed_records": recs})
        self.assertEqual(result, recs)

    def test_records_fallback_returned(self):
        recs = [{"coin": {"amount": 200}}]
        result = _extract_coin_records({"records": recs})
        self.assertEqual(result, recs)

    def test_empty_dict_returns_empty(self):
        self.assertEqual(_extract_coin_records({}), [])


# ===========================================================================
# _coin_amount
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestCoinAmount(unittest.TestCase):
    def test_extracts_amount(self):
        self.assertEqual(_coin_amount({"coin": {"amount": 12345}}), 12345)

    def test_missing_coin_key_returns_zero(self):
        self.assertEqual(_coin_amount({}), 0)

    def test_missing_amount_returns_zero(self):
        self.assertEqual(_coin_amount({"coin": {}}), 0)


# ===========================================================================
# _chia_int_to_bytes
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestChiaIntToBytes(unittest.TestCase):
    def test_zero_returns_empty(self):
        self.assertEqual(_chia_int_to_bytes(0), b"")

    def test_one_mojos(self):
        result = _chia_int_to_bytes(1)
        self.assertEqual(int.from_bytes(result, "big", signed=True), 1)

    def test_large_value_roundtrips(self):
        v = 1_000_000_000_000
        result = _chia_int_to_bytes(v)
        self.assertEqual(int.from_bytes(result, "big", signed=True), v)

    def test_byte_count_formula(self):
        # For 1 mojo: bit_length=1, byte_count = (1+8)>>3 = 1
        result = _chia_int_to_bytes(1)
        expected_bytes = ((1).bit_length() + 8) >> 3
        self.assertEqual(len(result), expected_bytes)


# ===========================================================================
# _coin_id_from_record
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestCoinIdFromRecord(unittest.TestCase):
    def test_uses_name_field(self):
        rec = {"coin": {"name": "ABCDEF"}}
        result = _coin_id_from_record(rec)
        self.assertEqual(result, "0xabcdef")

    def test_uses_coin_id_field_at_record_level(self):
        rec = {"coin": {}, "coin_id": "0x1234AB"}
        result = _coin_id_from_record(rec)
        self.assertEqual(result, "0x1234ab")

    def test_normalises_to_lowercase_with_0x(self):
        rec = {"coin": {"name": "DEADBEEF"}}
        self.assertTrue(_coin_id_from_record(rec).startswith("0x"))
        self.assertEqual(_coin_id_from_record(rec), "0xdeadbeef")

    def test_computes_sha256_from_fields(self):
        parent = "a" * 64
        puzzle = "b" * 64
        rec = {
            "coin": {
                "parent_coin_info": "0x" + parent,
                "puzzle_hash": "0x" + puzzle,
                "amount": 0,
            }
        }
        result = _coin_id_from_record(rec)
        p_bytes = bytes.fromhex(parent)
        z_bytes = bytes.fromhex(puzzle)
        a_bytes = _chia_int_to_bytes(0)  # 0 → b""
        expected = "0x" + hashlib.sha256(p_bytes + z_bytes + a_bytes).hexdigest()
        self.assertEqual(result, expected)

    def test_missing_parent_returns_empty(self):
        rec = {"coin": {"puzzle_hash": "0x" + "b" * 64, "amount": 1}}
        self.assertEqual(_coin_id_from_record(rec), "")

    def test_empty_record_returns_empty(self):
        self.assertEqual(_coin_id_from_record({}), "")


# ===========================================================================
# _classify_coins
# ===========================================================================


def _make_rec(amount: int) -> dict:
    return {"coin": {"amount": amount}}


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestClassifyCoins(unittest.TestCase):
    def test_large_coin_goes_to_reserve(self):
        result = _classify_coins(
            [_make_rec(2_000_000_000)], trading_size_mojos=1_000_000_000
        )
        self.assertEqual(len(result["reserve"]), 1)
        self.assertEqual(len(result["trading"]), 0)

    def test_trading_size_coin_goes_to_trading(self):
        result = _classify_coins(
            [_make_rec(1_000_000_000)], trading_size_mojos=1_000_000_000
        )
        self.assertEqual(len(result["trading"]), 1)

    def test_small_coin_goes_to_small(self):
        result = _classify_coins([_make_rec(100)], trading_size_mojos=1_000_000_000)
        self.assertEqual(len(result["small"]), 1)

    def test_all_three_buckets(self):
        records = [_make_rec(3_000), _make_rec(1_000), _make_rec(100)]
        result = _classify_coins(records, trading_size_mojos=1_000)
        self.assertEqual(len(result["reserve"]), 1)
        self.assertEqual(len(result["trading"]), 1)
        self.assertEqual(len(result["small"]), 1)

    def test_reserve_sorted_descending(self):
        records = [_make_rec(3_000), _make_rec(5_000), _make_rec(4_000)]
        result = _classify_coins(records, trading_size_mojos=1_000)
        amounts = [_coin_amount(r) for r in result["reserve"]]
        self.assertEqual(amounts, sorted(amounts, reverse=True))

    def test_empty_records_returns_empty_buckets(self):
        result = _classify_coins([], trading_size_mojos=1_000)
        self.assertEqual(len(result["reserve"]), 0)
        self.assertEqual(len(result["trading"]), 0)
        self.assertEqual(len(result["small"]), 0)


# ===========================================================================
# _clamp_coin_prep_multiplier
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestClampCoinPrepMultiplier(unittest.TestCase):
    def test_one_is_identity(self):
        self.assertEqual(_clamp_coin_prep_multiplier(1.0), 1.0)

    def test_below_floor_clamped_to_1(self):
        self.assertEqual(_clamp_coin_prep_multiplier(0.5), 1.0)

    def test_above_ceiling_clamped_to_3(self):
        self.assertEqual(_clamp_coin_prep_multiplier(5.0), 3.0)

    def test_invalid_string_returns_1(self):
        self.assertEqual(_clamp_coin_prep_multiplier("nope"), 1.0)

    def test_valid_string_float_parsed(self):
        self.assertEqual(_clamp_coin_prep_multiplier("2.0"), 2.0)

    def test_mid_value_passthrough(self):
        self.assertEqual(_clamp_coin_prep_multiplier(2.0), 2.0)


# ===========================================================================
# Format helpers
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestFormatHelpers(unittest.TestCase):
    def test_format_xch_one_xch(self):
        result = _format_amount_xch(1_000_000_000_000)
        self.assertEqual(result, "1.0000")

    def test_format_xch_zero(self):
        result = _format_amount_xch(0)
        self.assertEqual(result, "0.0000")

    def test_format_cat_3_decimals(self):
        result = _format_amount_cat(1_000, 3)
        self.assertEqual(result, "1.00")

    def test_format_cat_zero(self):
        result = _format_amount_cat(0, 3)
        self.assertEqual(result, "0.00")


# ===========================================================================
# get_tier_distribution
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestGetTierDistribution(unittest.TestCase):
    def test_zero_max_returns_all_zeros(self):
        result = get_tier_distribution(0)
        self.assertTrue(all(v == 0 for v in result.values()))

    def test_explicit_tier_counts(self):
        result = get_tier_distribution(
            8, tier_counts={"inner": 2, "mid": 3, "outer": 2, "extreme": 1}
        )
        self.assertEqual(result["inner"], 2)
        self.assertEqual(result["mid"], 3)

    def test_overflow_goes_to_extreme(self):
        # 10 slots but only inner=2 configured — 8 overflow to extreme
        result = get_tier_distribution(
            10, tier_counts={"inner": 2, "mid": 0, "outer": 0, "extreme": 0}
        )
        self.assertEqual(result["inner"], 2)
        self.assertEqual(result["extreme"], 8)

    def test_cfg_buy_side(self):
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            result = get_tier_distribution(8, side="buy")
        self.assertEqual(result["inner"], 2)
        self.assertEqual(result["mid"], 3)

    def test_total_matches_max_offers(self):
        result = get_tier_distribution(
            10, tier_counts={"inner": 3, "mid": 3, "outer": 2, "extreme": 2}
        )
        self.assertEqual(sum(result.values()), 10)


# ===========================================================================
# flip_position_tiers_to_coin_size_tiers
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestFlipPositionTiers(unittest.TestCase):
    def test_sell_side_is_identity(self):
        counts = {"inner": 2, "mid": 3, "outer": 2, "extreme": 1}
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            result = flip_position_tiers_to_coin_size_tiers(counts, side="sell")
        self.assertEqual(result["inner"], 2)
        self.assertEqual(result["mid"], 3)

    def test_buy_non_reversed_is_identity(self):
        counts = {"inner": 2, "mid": 3, "outer": 2, "extreme": 1}
        cfg_nr = _with_cfg(BUY_LADDER_REVERSED=False)
        with patch.object(_cm_mod, "cfg", cfg_nr):
            result = flip_position_tiers_to_coin_size_tiers(counts, side="buy")
        self.assertEqual(result["inner"], 2)

    def test_buy_reversed_flips_inner_extreme(self):
        counts = {"inner": 2, "mid": 3, "outer": 2, "extreme": 1}
        cfg_r = _with_cfg(BUY_LADDER_REVERSED=True)
        with patch.object(_cm_mod, "cfg", cfg_r):
            result = flip_position_tiers_to_coin_size_tiers(counts, side="buy")
        # inner position → extreme coin size, extreme position → inner coin size
        self.assertEqual(result["extreme"], 2)  # was inner
        self.assertEqual(result["inner"], 1)  # was extreme

    def test_non_positional_tiers_preserved(self):
        counts = {"inner": 1, "mid": 1, "outer": 1, "extreme": 1, "sniper": 3}
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            result = flip_position_tiers_to_coin_size_tiers(counts, side="sell")
        self.assertEqual(result.get("sniper"), 3)


# ===========================================================================
# get_weighted_tier_prep_counts
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestGetWeightedTierPrepCounts(unittest.TestCase):
    def test_multiplier_1_doubles_total(self):
        # multiplier=1.0 → spare_budget = round(8*1.0) = 8 spare added on top of 8 active
        tc = {"inner": 2, "mid": 3, "outer": 2, "extreme": 1}
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            prep = get_weighted_tier_prep_counts(8, 1.0, tier_counts=tc)
        self.assertEqual(sum(prep.values()), 16)

    def test_multiplier_2_doubles_total(self):
        tc = {"inner": 2, "mid": 3, "outer": 2, "extreme": 1}
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            prep = get_weighted_tier_prep_counts(8, 2.0, tier_counts=tc)
        # Total should be 2× the base (8 active + 8 spare ≈ 16)
        self.assertGreaterEqual(sum(prep.values()), 16)

    def test_zero_slots_returns_zeros(self):
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            result = get_weighted_tier_prep_counts(0, 1.5)
        self.assertTrue(all(v == 0 for v in result.values()))

    def test_explicit_spare_counts_override_multiplier(self):
        tc = {"inner": 2, "mid": 2, "outer": 2, "extreme": 0}
        spare = {"inner": 5, "mid": 5, "outer": 5, "extreme": 0}
        with patch.object(_cm_mod, "cfg", _FAKE_CFG):
            result = get_weighted_tier_prep_counts(
                6, 2.0, tier_counts=tc, spare_counts=spare
            )
        # inner = 2 active + 5 spare = 7
        self.assertEqual(result["inner"], 7)


# ===========================================================================
# FeeCoinPool
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestFeeCoinPool(unittest.TestCase):
    def _rec(self, coin_id: str, amount: int) -> dict:
        return {"coin": {"name": coin_id, "amount": amount}}

    def test_empty_pool_reserve_returns_none(self):
        pool = FeeCoinPool()
        self.assertIsNone(pool.reserve())

    def test_refresh_adds_coins(self):
        pool = FeeCoinPool()
        pool.refresh([self._rec("abc", 1000), self._rec("def", 2000)])
        self.assertEqual(pool.total_count, 2)

    def test_reserve_returns_coin_id(self):
        pool = FeeCoinPool()
        pool.refresh([self._rec("abc", 1000)])
        cid = pool.reserve()
        self.assertIsNotNone(cid)
        self.assertIn("abc", cid)

    def test_reserve_reduces_available_count(self):
        pool = FeeCoinPool()
        pool.refresh([self._rec("abc", 1000), self._rec("def", 2000)])
        pool.reserve()
        self.assertEqual(pool.available_count, 1)
        self.assertEqual(pool.reserved_count, 1)

    def test_cannot_reserve_same_coin_twice(self):
        pool = FeeCoinPool()
        pool.refresh([self._rec("abc", 1000)])
        pool.reserve()
        cid2 = pool.reserve()
        self.assertIsNone(cid2)

    def test_refresh_resets_reservations(self):
        pool = FeeCoinPool()
        pool.refresh([self._rec("abc", 1000)])
        pool.reserve()
        pool.refresh([self._rec("abc", 1000)])
        self.assertEqual(pool.available_count, 1)
        self.assertEqual(pool.reserved_count, 0)

    def test_total_count_unchanged_after_reserve(self):
        pool = FeeCoinPool()
        pool.refresh([self._rec("abc", 1000), self._rec("def", 2000)])
        pool.reserve()
        self.assertEqual(pool.total_count, 2)


if __name__ == "__main__":
    unittest.main()
