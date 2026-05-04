"""Slice 02-15 — unit tests for coin_prep_utils.py and coin_prep_worker pure functions."""

import hashlib
import sys
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coin_prep_utils as _utils
from coin_prep_worker import (
    CoinPrepWorker,
    CoinPrepStatus,
    PrepPhase,
    parse_arguments,
)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestCoinPrepWorkerCli(unittest.TestCase):

    def test_help_exits_cleanly(self):
        with patch.object(sys, "argv", ["coin_prep_worker.py", "--help"]):
            with self.assertRaises(SystemExit) as cm:
                parse_arguments()

        self.assertEqual(cm.exception.code, 0)


# ---------------------------------------------------------------------------
# should_retry_unconsumed_split
# ---------------------------------------------------------------------------

class TestShouldRetryUnconsumedSplit(unittest.TestCase):

    def _call(self, **kw):
        defaults = dict(
            elapsed_s=120,
            pool_coin_visible=True,
            pool_coin_selectable=True,
            outputs_selectable=False,
            retries_used=0,
        )
        defaults.update(kw)
        return _utils.should_retry_unconsumed_split(**defaults)

    def test_returns_true_when_all_conditions_met(self):
        self.assertTrue(self._call())

    def test_false_when_retries_exhausted(self):
        self.assertFalse(self._call(retries_used=1))

    def test_false_when_retries_above_max(self):
        self.assertFalse(self._call(retries_used=2))

    def test_false_when_not_enough_elapsed(self):
        self.assertFalse(self._call(elapsed_s=30))

    def test_true_exactly_at_retry_after(self):
        # guard is elapsed_s < retry_after_s, so ==60 is allowed through
        self.assertTrue(self._call(elapsed_s=60))

    def test_false_when_outputs_already_selectable(self):
        self.assertFalse(self._call(outputs_selectable=True))

    def test_false_when_pool_coin_not_visible(self):
        self.assertFalse(self._call(pool_coin_visible=False))

    def test_false_when_pool_coin_not_selectable(self):
        self.assertFalse(self._call(pool_coin_selectable=False))

    def test_custom_retry_after_s(self):
        # elapsed=50 < retry_after_s=100 → False
        self.assertFalse(self._call(elapsed_s=50, retry_after_s=100))
        # elapsed=101 > retry_after_s=100 → True
        self.assertTrue(self._call(elapsed_s=101, retry_after_s=100))

    def test_custom_max_retries(self):
        self.assertTrue(self._call(retries_used=1, max_retries=2))
        self.assertFalse(self._call(retries_used=2, max_retries=2))

    def test_false_when_outputs_were_already_observed(self):
        self.assertFalse(self._call(owned_output_high_water=14, expected_count=14))


# ---------------------------------------------------------------------------
# should_extend_pending_consumed_split_grace
# ---------------------------------------------------------------------------

class TestShouldExtendPendingConsumedSplitGrace(unittest.TestCase):

    def _call(self, **kw):
        # Base: extension should succeed (all conditions satisfied)
        defaults = dict(
            elapsed_s=200,
            current_deadline_s=180,
            pool_coin_visible=False,
            pool_coin_selectable=False,
            tx_known=True,
            tx_confirmed=False,
            owned_output_count=9,
            selectable_output_count=9,
            expected_count=10,
            extensions_used=0,
        )
        defaults.update(kw)
        return _utils.should_extend_pending_consumed_split_grace(**defaults)

    def test_returns_true_when_nearly_complete(self):
        self.assertTrue(self._call())

    def test_false_when_extension_already_used(self):
        self.assertFalse(self._call(extensions_used=1))

    def test_false_when_deadline_not_reached(self):
        self.assertFalse(self._call(elapsed_s=100, current_deadline_s=180))

    def test_false_when_expected_count_zero(self):
        self.assertFalse(self._call(expected_count=0))

    def test_false_when_tx_confirmed(self):
        self.assertFalse(self._call(tx_confirmed=True))

    def test_false_when_tx_not_known(self):
        self.assertFalse(self._call(tx_known=False))

    def test_false_when_pool_coin_still_intact(self):
        # pool_coin_visible AND pool_coin_selectable → still intact, don't extend
        self.assertFalse(self._call(pool_coin_visible=True, pool_coin_selectable=True))

    def test_true_when_pool_coin_visible_but_not_selectable(self):
        # Not strictly selectable — condition is BOTH visible AND selectable
        self.assertTrue(self._call(pool_coin_visible=True, pool_coin_selectable=False))

    def test_false_when_too_many_missing_owned(self):
        # 3 missing > extension_missing_limit=2
        self.assertFalse(self._call(owned_output_count=7, expected_count=10))

    def test_false_when_completion_ratio_too_low(self):
        # 8/10 = 0.80 < min_completion_ratio=0.90
        self.assertFalse(self._call(owned_output_count=8, expected_count=10,
                                     selectable_output_count=8))

    def test_true_at_exact_ratio_threshold(self):
        # 9/10 = 0.90 >= 0.90 → True
        self.assertTrue(self._call(owned_output_count=9, expected_count=10,
                                    selectable_output_count=9))

    def test_all_owned_none_selectable_still_extends(self):
        # all_owned=True, so missing_selectable not checked against limit
        self.assertTrue(self._call(owned_output_count=10, expected_count=10,
                                    selectable_output_count=0))

    def test_custom_extension_missing_limit(self):
        # 4 missing, limit=5 → passes missing_owned check; ratio=6/10=0.60 < 0.90 → False
        self.assertFalse(self._call(owned_output_count=6, expected_count=10,
                                     selectable_output_count=6, extension_missing_limit=5))
        # 8/10=0.80 < 0.90 still False
        self.assertFalse(self._call(owned_output_count=8, expected_count=10,
                                     selectable_output_count=8, extension_missing_limit=5))

    def test_custom_min_completion_ratio(self):
        # 8/10=0.80 >= 0.75 with custom min → True
        self.assertTrue(self._call(owned_output_count=8, expected_count=10,
                                    selectable_output_count=8,
                                    min_completion_ratio=0.75))


# ---------------------------------------------------------------------------
# PrepPhase enum
# ---------------------------------------------------------------------------

class TestPrepPhase(unittest.TestCase):

    def test_all_members_present(self):
        names = {m.name for m in PrepPhase}
        self.assertEqual(names, {
            "IDLE", "ANALYZING", "CONSOLIDATING", "CREATING_POOL",
            "SPLITTING", "VERIFYING", "COMPLETE", "ERROR",
        })

    def test_string_values(self):
        self.assertEqual(PrepPhase.IDLE.value, "idle")
        self.assertEqual(PrepPhase.COMPLETE.value, "complete")
        self.assertEqual(PrepPhase.ERROR.value, "error")

    def test_member_count(self):
        self.assertEqual(len(PrepPhase), 8)


# ---------------------------------------------------------------------------
# CoinPrepStatus dataclass + to_dict
# ---------------------------------------------------------------------------

class TestCoinPrepStatus(unittest.TestCase):

    def _make(self, **kw):
        defaults = dict(
            phase="idle",
            progress=0.5,
            message="running",
            xch_coins_current=3,
            xch_coins_target=5,
            cat_coins_current=2,
            cat_coins_target=4,
        )
        defaults.update(kw)
        return CoinPrepStatus(**defaults)

    def test_to_dict_has_percentage(self):
        s = self._make(progress=0.75)
        d = s.to_dict()
        self.assertEqual(d["percentage"], 75)

    def test_to_dict_includes_all_fields(self):
        s = self._make()
        d = s.to_dict()
        for key in ("phase", "progress", "message", "xch_coins_current",
                    "xch_coins_target", "cat_coins_current", "cat_coins_target"):
            self.assertIn(key, d)

    def test_percentage_truncates(self):
        s = self._make(progress=0.999)
        d = s.to_dict()
        self.assertEqual(d["percentage"], 99)

    def test_optional_error_defaults_none(self):
        s = self._make()
        self.assertIsNone(s.error)

    def test_error_present_in_dict(self):
        s = self._make(error="boom")
        d = s.to_dict()
        self.assertEqual(d["error"], "boom")


# ---------------------------------------------------------------------------
# CoinPrepWorker._prepared_coin_count_from_total
# ---------------------------------------------------------------------------

class TestPreparedCoinCountFromTotal(unittest.TestCase):

    def _call(self, v):
        return CoinPrepWorker._prepared_coin_count_from_total(v)

    def test_normal_value(self):
        self.assertEqual(self._call(5), 4)

    def test_one_returns_zero(self):
        self.assertEqual(self._call(1), 0)

    def test_zero_returns_zero(self):
        self.assertEqual(self._call(0), 0)

    def test_none_returns_zero(self):
        self.assertEqual(self._call(None), 0)

    def test_large_value(self):
        self.assertEqual(self._call(100), 99)


# ---------------------------------------------------------------------------
# CoinPrepWorker._sage_submit_succeeded
# ---------------------------------------------------------------------------

class TestSageSubmitSucceeded(unittest.TestCase):

    def _call(self, v):
        return CoinPrepWorker._sage_submit_succeeded(v)

    def test_none_returns_false(self):
        self.assertFalse(self._call(None))

    def test_empty_dict_returns_true(self):
        self.assertTrue(self._call({}))

    def test_dict_with_error_string_returns_false(self):
        self.assertFalse(self._call({"error": "something went wrong"}))

    def test_dict_with_success_false_returns_false(self):
        self.assertFalse(self._call({"success": False}))

    def test_status_error_returns_false(self):
        self.assertFalse(self._call({"status": "error"}))

    def test_status_failed_returns_false(self):
        self.assertFalse(self._call({"status": "FAILED"}))

    def test_status_ok_returns_true(self):
        self.assertTrue(self._call({"status": "ok", "success": True}))

    def test_non_dict_truthy_returns_true(self):
        self.assertTrue(self._call("ok"))


# ---------------------------------------------------------------------------
# CoinPrepWorker._extract_sage_transaction_ids
# ---------------------------------------------------------------------------

class TestExtractSageTransactionIds(unittest.TestCase):

    def _call(self, v):
        return CoinPrepWorker._extract_sage_transaction_ids(v)

    def test_none_returns_empty(self):
        self.assertEqual(self._call(None), [])

    def test_transaction_ids_list(self):
        result = self._call({"transaction_ids": ["abc", "def"]})
        self.assertIn("0xabc", result)
        self.assertIn("0xdef", result)

    def test_single_transaction_id(self):
        result = self._call({"transaction_id": "0xfeed"})
        self.assertEqual(result, ["0xfeed"])

    def test_nested_transaction_dict(self):
        result = self._call({"transaction": {"transaction_id": "0xcafe"}})
        self.assertIn("0xcafe", result)

    def test_deduplication(self):
        result = self._call({"transaction_ids": ["0xaaa"], "transaction_id": "0xaaa"})
        self.assertEqual(result.count("0xaaa"), 1)

    def test_0x_prefix_added(self):
        result = self._call({"transaction_ids": ["deadbeef"]})
        self.assertEqual(result, ["0xdeadbeef"])

    def test_already_0x_not_doubled(self):
        result = self._call({"transaction_id": "0xdeadbeef"})
        self.assertEqual(result, ["0xdeadbeef"])

    def test_empty_ids_skipped(self):
        result = self._call({"transaction_ids": ["", None, "0xgood"]})
        self.assertEqual(result, ["0xgood"])


# ---------------------------------------------------------------------------
# CoinPrepWorker._ensure_0x
# ---------------------------------------------------------------------------

class TestEnsure0x(unittest.TestCase):

    def _call(self, v):
        return CoinPrepWorker._ensure_0x(v)

    def test_adds_prefix_when_missing(self):
        self.assertEqual(self._call("deadbeef"), "0xdeadbeef")

    def test_preserves_existing_prefix(self):
        self.assertEqual(self._call("0xdeadbeef"), "0xdeadbeef")

    def test_empty_string_passthrough(self):
        self.assertEqual(self._call(""), "")

    def test_none_passthrough(self):
        self.assertIsNone(self._call(None))


# ---------------------------------------------------------------------------
# CoinPrepWorker._compute_coin_id
# ---------------------------------------------------------------------------

class TestComputeCoinId(unittest.TestCase):

    _PARENT = "a" * 64
    _PUZZLE = "b" * 64

    def _call(self, amount):
        return CoinPrepWorker._compute_coin_id(self._PARENT, self._PUZZLE, amount)

    def _expected(self, parent_hex, puzzle_hex, amount):
        parent_bytes = bytes.fromhex(parent_hex)
        puzzle_bytes = bytes.fromhex(puzzle_hex)
        if amount == 0:
            amount_bytes = b""
        else:
            byte_count = (amount.bit_length() + 8) >> 3
            amount_bytes = amount.to_bytes(byte_count, byteorder="big", signed=True)
        return "0x" + hashlib.sha256(parent_bytes + puzzle_bytes + amount_bytes).hexdigest()

    def test_returns_0x_prefixed_hex(self):
        result = self._call(1000)
        self.assertTrue(result.startswith("0x"))
        self.assertEqual(len(result), 66)  # 0x + 64 hex chars

    def test_deterministic(self):
        self.assertEqual(self._call(999), self._call(999))

    def test_different_amounts_differ(self):
        self.assertNotEqual(self._call(1), self._call(2))

    def test_amount_zero(self):
        result = self._call(0)
        expected = self._expected(self._PARENT, self._PUZZLE, 0)
        self.assertEqual(result, expected)

    def test_amount_128_uses_two_bytes(self):
        # 128 has high bit set → needs leading 0x00 byte in Chia encoding
        result = self._call(128)
        expected = self._expected(self._PARENT, self._PUZZLE, 128)
        self.assertEqual(result, expected)

    def test_strips_0x_from_inputs(self):
        r1 = CoinPrepWorker._compute_coin_id("0x" + self._PARENT, "0x" + self._PUZZLE, 1)
        r2 = CoinPrepWorker._compute_coin_id(self._PARENT, self._PUZZLE, 1)
        self.assertEqual(r1, r2)


class TestPartitionCoinsForDesignation(unittest.TestCase):

    def test_xch_fee_outputs_with_full_fee_delta_still_match_fee_tier(self):
        worker = object.__new__(CoinPrepWorker)
        worker.tier_enabled = True
        worker.tier_order = ["fees"]
        worker.xch_tier_counts = {"fees": 2}
        worker.cat_tier_counts = {}
        worker.tier_xch_sizes = {"fees": Decimal("0.00115")}
        worker.tier_cat_sizes = {}
        worker.cat_decimals = 3
        worker._tx_fee_mojos = lambda: 13_079_100

        assigned, unmatched = CoinPrepWorker._partition_coins_for_designation(
            worker,
            [
                {"coin_id": "a", "amount": 1_136_920_900},
                {"coin_id": "b", "amount": 1_136_920_900},
                {"coin_id": "reserve", "amount": 45_000_000_000_000},
            ],
            "xch",
        )

        self.assertEqual(len(assigned["fees"]), 2)
        self.assertEqual([coin["coin_id"] for coin in unmatched], ["reserve"])


class TestWaitForExpectedLocalCoinCounts(unittest.TestCase):

    def _worker_with_counts(self, xch_counts, cat_counts):
        worker = object.__new__(CoinPrepWorker)
        worker.xch_wallet_id = 1
        worker.cat_wallet_id = 2
        worker.xch_expected_total_coins = 120
        worker.cat_expected_total_coins = 70
        worker.status_counts = []
        worker.status_messages = []
        worker.log_lines = []

        xch_iter = iter(xch_counts)
        cat_iter = iter(cat_counts)
        state = {"xch": xch_counts[-1], "cat": cat_counts[-1]}

        def get_confirmed_coin_count(wallet_id):
            if wallet_id == worker.xch_wallet_id:
                try:
                    state["xch"] = next(xch_iter)
                except StopIteration:
                    pass
                return state["xch"]
            try:
                state["cat"] = next(cat_iter)
            except StopIteration:
                pass
            return state["cat"]

        worker.get_confirmed_coin_count = get_confirmed_coin_count
        worker._set_status_coin_counts = (
            lambda xch_total=None, cat_total=None: worker.status_counts.append(
                (xch_total, cat_total)
            )
        )
        worker.update_status = (
            lambda phase, progress, message: worker.status_messages.append(
                (phase, progress, message)
            )
        )
        worker.log = lambda message: worker.log_lines.append(message)
        return worker

    def test_returns_false_when_sage_local_counts_remain_short(self):
        worker = self._worker_with_counts([120, 120, 120], [61, 61, 61])

        with patch("coin_prep_worker.time.sleep", lambda _seconds: None):
            result = worker._wait_for_expected_local_coin_counts(
                timeout_s=2,
                poll_s=1,
            )

        self.assertFalse(result)
        self.assertIn("Local wallet still short", "\n".join(worker.log_lines))

    def test_returns_true_after_sage_local_counts_catch_up(self):
        worker = self._worker_with_counts([119, 120], [61, 70])

        with patch("coin_prep_worker.time.sleep", lambda _seconds: None):
            result = worker._wait_for_expected_local_coin_counts(
                timeout_s=5,
                poll_s=1,
            )

        self.assertTrue(result)
        self.assertIn((120, 70), worker.status_counts)


if __name__ == "__main__":
    unittest.main()
