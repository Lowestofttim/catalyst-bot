"""Slice 02-21 — wallet_sage.py pure-function unit tests.

Only tests functions that require no network I/O:
  _rpc_succeeded, _is_cat_wallet, _extract_sage_coin_list,
  _normalize_sage_coin_records, is_offer_time_expired,
  get_offer_expiry_info, cat_to_mojos, xch_to_mojos,
  _is_open_status, classify_offers_from_list, _normalize_offer_lock_id.
"""

import math
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

try:
    import wallet_sage as _ws
    from wallet_sage import (
        _rpc_succeeded,
        _is_cat_wallet,
        _extract_sage_coin_list,
        _normalize_sage_coin_records,
        is_offer_time_expired,
        get_offer_expiry_info,
        cat_to_mojos,
        xch_to_mojos,
        _is_open_status,
        classify_offers_from_list,
        _normalize_offer_lock_id,
        WALLET_ID_XCH,
    )
    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_SKIP_MSG = f"wallet_sage unavailable: {_SKIP}"
_ASSET = "abc123def456abc123def456abc123def456abc123def456abc123def456ab12"


# ---------------------------------------------------------------------------
# _rpc_succeeded
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestRpcSucceeded(unittest.TestCase):
    def test_none_returns_false(self):
        self.assertFalse(_rpc_succeeded(None))

    def test_non_dict_returns_false(self):
        self.assertFalse(_rpc_succeeded("ok"))
        self.assertFalse(_rpc_succeeded(42))
        self.assertFalse(_rpc_succeeded([]))

    def test_success_false_returns_false(self):
        self.assertFalse(_rpc_succeeded({"success": False}))

    def test_error_key_returns_false(self):
        self.assertFalse(_rpc_succeeded({"error": "boom"}))

    def test_error_message_key_returns_false(self):
        self.assertFalse(_rpc_succeeded({"error_message": "bad"}))

    def test_status_error_variants_return_false(self):
        for s in ("error", "failed", "failure", "ERROR", "FAILED"):
            with self.subTest(status=s):
                self.assertFalse(_rpc_succeeded({"status": s}))

    def test_empty_dict_returns_true(self):
        self.assertTrue(_rpc_succeeded({}))

    def test_success_true_returns_true(self):
        self.assertTrue(_rpc_succeeded({"success": True, "data": []}))

    def test_success_with_error_field_returns_false(self):
        self.assertFalse(_rpc_succeeded({"success": True, "error": "oops"}))


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestSageMempoolConflictClassification(unittest.TestCase):
    def test_cancel_conflict_is_expected_settlement_noise(self):
        self.assertEqual(
            _ws._sage_tx_error_level("MEMPOOL_CONFLICT", "cancel_offer"),
            "info",
        )

    def test_create_conflict_remains_warning(self):
        self.assertEqual(
            _ws._sage_tx_error_level("MEMPOOL_CONFLICT", "make_offer"),
            "warning",
        )


# ---------------------------------------------------------------------------
# _is_cat_wallet
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestIsCatWallet(unittest.TestCase):
    def test_xch_wallet_id_returns_false(self):
        self.assertFalse(_is_cat_wallet(WALLET_ID_XCH))

    def test_cat_wallet_id_returns_true(self):
        self.assertTrue(_is_cat_wallet(WALLET_ID_XCH + 1))

    def test_wallet_id_2_returns_true(self):
        self.assertTrue(_is_cat_wallet(2))


# ---------------------------------------------------------------------------
# _extract_sage_coin_list
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestExtractSageCoinList(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(_extract_sage_coin_list(None), [])

    def test_non_dict_returns_empty(self):
        self.assertEqual(_extract_sage_coin_list([{"a": 1}]), [])

    def test_coins_key(self):
        coins = [{"amount": "100"}]
        result = _extract_sage_coin_list({"coins": coins})
        self.assertEqual(result, coins)

    def test_records_key(self):
        records = [{"amount": "200"}]
        result = _extract_sage_coin_list({"records": records})
        self.assertEqual(result, records)

    def test_data_key(self):
        data = [{"amount": "300"}]
        result = _extract_sage_coin_list({"data": data})
        self.assertEqual(result, data)

    def test_fallback_to_first_list_value(self):
        coins = [{"amount": "400"}]
        result = _extract_sage_coin_list({"foo": coins})
        self.assertEqual(result, coins)

    def test_non_dict_items_filtered_out(self):
        result = _extract_sage_coin_list({"coins": [{"a": 1}, "bad", None]})
        self.assertEqual(result, [{"a": 1}])

    def test_empty_dict_returns_empty(self):
        self.assertEqual(_extract_sage_coin_list({}), [])


# ---------------------------------------------------------------------------
# _normalize_sage_coin_records
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestNormalizeSageCoinRecords(unittest.TestCase):
    def _coin(self, **kwargs):
        return kwargs

    def test_basic_conversion(self):
        coin = {"amount": "1000", "parent_coin_info": "0xpar", "puzzle_hash": "0xpuz", "coin_id": "0xid"}
        result = _normalize_sage_coin_records([coin])
        self.assertEqual(len(result), 1)
        rec = result[0]
        self.assertEqual(rec["coin"]["amount"], 1000)
        self.assertEqual(rec["coin"]["parent_coin_info"], "0xpar")
        self.assertEqual(rec["coin"]["puzzle_hash"], "0xpuz")
        self.assertEqual(rec["coin_id"], "0xid")
        self.assertEqual(rec["spent_block_index"], 0)

    def test_min_filter_excludes_small(self):
        coins = [{"amount": "50"}, {"amount": "150"}]
        result = _normalize_sage_coin_records(coins, min_amount_mojos=100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["coin"]["amount"], 150)

    def test_max_filter_excludes_large(self):
        coins = [{"amount": "50"}, {"amount": "150"}]
        result = _normalize_sage_coin_records(coins, max_amount_mojos=100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["coin"]["amount"], 50)

    def test_alt_amount_field_amt(self):
        result = _normalize_sage_coin_records([{"amt": "777"}])
        self.assertEqual(result[0]["coin"]["amount"], 777)

    def test_alt_parent_field(self):
        result = _normalize_sage_coin_records([{"amount": "1", "parentCoin": "0xp"}])
        self.assertEqual(result[0]["coin"]["parent_coin_info"], "0xp")

    def test_alt_coin_id_field(self):
        result = _normalize_sage_coin_records([{"amount": "1", "name": "0xn"}])
        self.assertEqual(result[0]["coin_id"], "0xn")

    def test_empty_input(self):
        self.assertEqual(_normalize_sage_coin_records([]), [])


# ---------------------------------------------------------------------------
# is_offer_time_expired
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestIsOfferTimeExpired(unittest.TestCase):
    def test_no_max_time_returns_false(self):
        self.assertFalse(is_offer_time_expired({}))

    def test_max_time_zero_returns_false(self):
        self.assertFalse(is_offer_time_expired({"max_time": 0}))

    def test_future_valid_times_not_expired(self):
        future = int(time.time()) + 3600
        offer = {"valid_times": {"max_time": future}}
        self.assertFalse(is_offer_time_expired(offer))

    def test_past_valid_times_expired(self):
        past = int(time.time()) - 3600
        offer = {"valid_times": {"max_time": past}}
        self.assertTrue(is_offer_time_expired(offer))

    def test_top_level_max_time_past_expired(self):
        past = int(time.time()) - 3600
        offer = {"max_time": past}
        self.assertTrue(is_offer_time_expired(offer))

    def test_future_top_level_max_time_not_expired(self):
        future = int(time.time()) + 3600
        offer = {"max_time": future}
        self.assertFalse(is_offer_time_expired(offer))


# ---------------------------------------------------------------------------
# get_offer_expiry_info
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestGetOfferExpiryInfo(unittest.TestCase):
    def test_no_max_time_returns_inf(self):
        info = get_offer_expiry_info({})
        self.assertEqual(info["max_time"], 0)
        self.assertFalse(info["expired"])
        self.assertTrue(math.isinf(info["seconds_remaining"]))

    def test_future_offer_not_expired(self):
        future = int(time.time()) + 3600
        info = get_offer_expiry_info({"max_time": future})
        self.assertFalse(info["expired"])
        self.assertGreater(info["seconds_remaining"], 0)

    def test_past_offer_is_expired(self):
        past = int(time.time()) - 3600
        info = get_offer_expiry_info({"max_time": past})
        self.assertTrue(info["expired"])
        self.assertLess(info["seconds_remaining"], 0)

    def test_max_time_in_result(self):
        ts = int(time.time()) + 100
        info = get_offer_expiry_info({"max_time": ts})
        self.assertEqual(info["max_time"], ts)


# ---------------------------------------------------------------------------
# cat_to_mojos
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestCatToMojos(unittest.TestCase):
    def test_standard_3_decimals(self):
        self.assertEqual(cat_to_mojos(Decimal("1.5"), 3), 1500)

    def test_truncates_not_rounds(self):
        self.assertEqual(cat_to_mojos(Decimal("1.9999"), 3), 1999)

    def test_zero_decimals(self):
        self.assertEqual(cat_to_mojos(Decimal("5"), 0), 5)

    def test_small_amount_truncated_to_zero(self):
        self.assertEqual(cat_to_mojos(Decimal("0.0001"), 3), 0)


# ---------------------------------------------------------------------------
# xch_to_mojos
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestXchToMojos(unittest.TestCase):
    def test_one_xch(self):
        self.assertEqual(xch_to_mojos(Decimal("1")), 1_000_000_000_000)

    def test_sub_mojo_truncated_to_zero(self):
        self.assertEqual(xch_to_mojos(Decimal("0.0000000000001")), 0)

    def test_zero(self):
        self.assertEqual(xch_to_mojos(Decimal("0")), 0)

    def test_truncation_not_rounding(self):
        # 0.9999999999999 XCH = 999999999999.9 mojos → floor = 999999999999
        self.assertEqual(xch_to_mojos(Decimal("0.9999999999999")), 999_999_999_999)


# ---------------------------------------------------------------------------
# _is_open_status
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestIsOpenStatus(unittest.TestCase):
    def setUp(self):
        # Clear per-function unknown-status cache so tests are isolated
        if hasattr(_is_open_status, "_unknown_logged"):
            _is_open_status._unknown_logged.clear()

    def test_none_returns_false(self):
        self.assertFalse(_is_open_status(None))

    def test_int_0_pending_accept_is_open(self):
        self.assertTrue(_is_open_status(0))

    def test_int_1_pending_confirm_is_open(self):
        self.assertTrue(_is_open_status(1))

    def test_int_2_pending_cancel_is_closed(self):
        self.assertFalse(_is_open_status(2))

    def test_int_3_cancelled_is_closed(self):
        self.assertFalse(_is_open_status(3))

    def test_string_open_statuses(self):
        for s in ("PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING", "open", "active"):
            with self.subTest(status=s):
                self.assertTrue(_is_open_status(s))

    def test_string_closed_statuses(self):
        for s in ("CANCELLED", "CANCELED", "CONFIRMED", "FAILED", "EXPIRED"):
            with self.subTest(status=s):
                self.assertFalse(_is_open_status(s))

    def test_unknown_string_returns_false(self):
        self.assertFalse(_is_open_status("TOTALLY_UNKNOWN_XYZ"))

    def test_expired_offer_record_forces_false(self):
        past = int(time.time()) - 3600
        offer = {"valid_times": {"max_time": past}}
        self.assertFalse(_is_open_status(0, offer_record=offer))

    def test_future_offer_record_uses_status(self):
        future = int(time.time()) + 3600
        offer = {"valid_times": {"max_time": future}}
        self.assertTrue(_is_open_status(0, offer_record=offer))


# ---------------------------------------------------------------------------
# classify_offers_from_list
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestClassifyOffersFromList(unittest.TestCase):
    def setUp(self):
        # Reset first-call logging flag so each test starts clean
        if hasattr(classify_offers_from_list, "_logged"):
            del classify_offers_from_list._logged

    def _open_buy(self):
        return {
            "status": 0,
            "summary": {
                "offered": {"xch": 1000},
                "requested": {_ASSET: 500},
            },
        }

    def _open_sell(self):
        return {
            "status": 0,
            "summary": {
                "offered": {_ASSET: 500},
                "requested": {"xch": 1000},
            },
        }

    def _closed_buy(self):
        return {
            "status": 3,
            "summary": {
                "offered": {"xch": 1000},
                "requested": {_ASSET: 500},
            },
        }

    def test_empty_list(self):
        buy, sell, closed = classify_offers_from_list([], _ASSET)
        self.assertEqual(buy, [])
        self.assertEqual(sell, [])
        self.assertEqual(closed, [])

    def test_non_dict_items_skipped(self):
        buy, sell, closed = classify_offers_from_list(["bad", None, 42], _ASSET)
        self.assertEqual(buy, [])
        self.assertEqual(sell, [])
        self.assertEqual(closed, [])

    def test_open_buy_classified(self):
        buy, sell, closed = classify_offers_from_list([self._open_buy()], _ASSET)
        self.assertEqual(len(buy), 1)
        self.assertEqual(sell, [])
        self.assertEqual(closed, [])

    def test_open_sell_classified(self):
        buy, sell, closed = classify_offers_from_list([self._open_sell()], _ASSET)
        self.assertEqual(buy, [])
        self.assertEqual(len(sell), 1)
        self.assertEqual(closed, [])

    def test_closed_offer_classified(self):
        buy, sell, closed = classify_offers_from_list([self._closed_buy()], _ASSET)
        self.assertEqual(buy, [])
        self.assertEqual(sell, [])
        self.assertEqual(len(closed), 1)

    def test_mixed_list(self):
        offers = [self._open_buy(), self._open_sell(), self._closed_buy()]
        buy, sell, closed = classify_offers_from_list(offers, _ASSET)
        self.assertEqual(len(buy), 1)
        self.assertEqual(len(sell), 1)
        self.assertEqual(len(closed), 1)

    def test_wrong_asset_open_offer_skipped(self):
        offer = {
            "status": 0,
            "summary": {
                "offered": {"xch": 1000},
                "requested": {"other_asset": 500},
            },
        }
        buy, sell, closed = classify_offers_from_list([offer], _ASSET)
        self.assertEqual(buy, [])
        self.assertEqual(sell, [])
        self.assertEqual(closed, [])

    def test_wrong_pair_closed_offer_not_in_closed(self):
        offer = {
            "status": 3,
            "summary": {
                "offered": {"xch": 1000},
                "requested": {"other_asset": 500},
            },
        }
        buy, sell, closed = classify_offers_from_list([offer], _ASSET)
        self.assertEqual(closed, [])


# ---------------------------------------------------------------------------
# _normalize_offer_lock_id
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestNormalizeOfferLockId(unittest.TestCase):
    def test_non_string_returns_none(self):
        self.assertIsNone(_normalize_offer_lock_id(None))
        self.assertIsNone(_normalize_offer_lock_id(42))
        self.assertIsNone(_normalize_offer_lock_id(["abc"]))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_normalize_offer_lock_id(""))
        self.assertIsNone(_normalize_offer_lock_id("   "))

    def test_strips_0x_prefix(self):
        self.assertEqual(_normalize_offer_lock_id("0xABCDEF"), "abcdef")

    def test_lowercases_no_prefix(self):
        self.assertEqual(_normalize_offer_lock_id("ABCDEF"), "abcdef")

    def test_strips_whitespace_and_0x(self):
        self.assertEqual(_normalize_offer_lock_id("  0xDeAd  "), "dead")

    def test_already_normalized(self):
        self.assertEqual(_normalize_offer_lock_id("deadbeef"), "deadbeef")


if __name__ == "__main__":
    unittest.main()
