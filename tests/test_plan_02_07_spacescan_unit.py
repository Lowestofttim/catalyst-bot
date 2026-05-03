"""Slice 02-07 — spacescan.py unit tests.

No network calls. Tests pure helpers (tier detection, rate/budget gate,
headers, stats), is_coin_spent via mocked _spacescan_get, verify_fill
decision tree via mocked is_coin_spent, get_xch_balance / get_token_balance
via mocked _spacescan_get, and is_known_wallet_address.
"""

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

try:
    import spacescan as _ss_mod
    from spacescan import (
        is_pro_tier, _get_call_interval, _check_daily_budget,
        get_api_stats, record_external_call, should_check_balance,
        is_known_wallet_address, is_coin_spent, verify_fill,
        get_xch_balance, get_token_balance,
    )
    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_PRO_CFG = SimpleNamespace(
    SPACESCAN_API_KEY="test-pro-key",
    SPACESCAN_PRO_URL="https://pro-api.spacescan.io",
    SPACESCAN_FREE_URL="https://api.spacescan.io",
    SPACESCAN_TIMEOUT=10,
    SPACESCAN_ENABLED=True,
    SPACESCAN_BALANCE_THRESHOLD_XCH=Decimal("0.1"),
    WALLET_ADDRESS="xch1testaddress123",
    WALLET_TYPE="sage",
    SAGE_SET_CHANGE_ADDRESS=True,
)

_FREE_CFG = SimpleNamespace(
    SPACESCAN_API_KEY="",
    SPACESCAN_FREE_URL="https://api.spacescan.io",
    SPACESCAN_TIMEOUT=10,
    SPACESCAN_ENABLED=True,
    WALLET_ADDRESS="",
    WALLET_TYPE="sage",
    SAGE_SET_CHANGE_ADDRESS=True,
)


@unittest.skipIf(_SKIP is not None, f"spacescan unavailable: {_SKIP}")
class _SS(unittest.TestCase):
    def setUp(self):
        self._cfg_patcher = patch.object(_ss_mod, "cfg", _PRO_CFG)
        self._cfg_patcher.start()
        self._log_patcher = patch.object(_ss_mod, "log_event")
        self._log_patcher.start()
        # Save and reset all module-level state
        self._orig = {
            "last_call": _ss_mod._last_call_time,
            "rate_until": _ss_mod._rate_limited_until,
            "calls_session": _ss_mod._calls_this_session,
            "calls_today": _ss_mod._calls_today,
            "calls_by_endpoint": dict(getattr(_ss_mod, "_calls_by_endpoint", {})),
            "today_date": _ss_mod._today_date,
            "cache": set(_ss_mod._known_wallet_addresses_cache),
            "cache_at": _ss_mod._known_wallet_addresses_cache_at,
        }
        _ss_mod._last_call_time = 0.0
        _ss_mod._rate_limited_until = 0.0
        _ss_mod._calls_this_session = 0
        _ss_mod._calls_today = 0
        _ss_mod._calls_by_endpoint = {}
        _ss_mod._today_date = ""
        _ss_mod._known_wallet_addresses_cache = set()
        _ss_mod._known_wallet_addresses_cache_at = 0.0

    def tearDown(self):
        self._cfg_patcher.stop()
        self._log_patcher.stop()
        _ss_mod._last_call_time = self._orig["last_call"]
        _ss_mod._rate_limited_until = self._orig["rate_until"]
        _ss_mod._calls_this_session = self._orig["calls_session"]
        _ss_mod._calls_today = self._orig["calls_today"]
        _ss_mod._calls_by_endpoint = self._orig["calls_by_endpoint"]
        _ss_mod._today_date = self._orig["today_date"]
        _ss_mod._known_wallet_addresses_cache = self._orig["cache"]
        _ss_mod._known_wallet_addresses_cache_at = self._orig["cache_at"]


# ===========================================================================
# Tier detection and rate helpers
# ===========================================================================

class TestTierHelpers(_SS):
    def test_is_pro_tier_true_when_api_key_set(self):
        self.assertTrue(is_pro_tier())

    def test_is_pro_tier_false_without_key(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            self.assertFalse(is_pro_tier())

    def test_get_call_interval_pro(self):
        self.assertEqual(_get_call_interval(), _ss_mod._PRO_CALL_INTERVAL)

    def test_get_call_interval_free(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            self.assertEqual(_get_call_interval(), _ss_mod._FREE_CALL_INTERVAL)

    def test_get_base_url_pro(self):
        url = _ss_mod._get_base_url()
        self.assertIn("pro-api", url)

    def test_get_base_url_free(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            url = _ss_mod._get_base_url()
            self.assertNotIn("pro-api", url)

    def test_get_headers_includes_api_key(self):
        headers = _ss_mod._get_headers()
        self.assertIn("x-api-key", headers)
        self.assertEqual(headers["x-api-key"], "test-pro-key")

    def test_get_headers_no_key_for_free(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            headers = _ss_mod._get_headers()
            self.assertNotIn("x-api-key", headers)

    def test_should_check_balance_true_on_pro(self):
        pro_non_sage = SimpleNamespace(**vars(_PRO_CFG))
        pro_non_sage.WALLET_TYPE = "chia"
        with patch.object(_ss_mod, "cfg", pro_non_sage):
            self.assertTrue(should_check_balance())

    def test_should_check_balance_false_on_sage_even_with_pro_key(self):
        self.assertFalse(should_check_balance())

    def test_should_check_balance_false_on_free(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            self.assertFalse(should_check_balance())


# ===========================================================================
# Daily budget gate
# ===========================================================================

class TestCheckDailyBudget(_SS):
    def test_pro_tier_always_true(self):
        # Pro ignores call count
        _ss_mod._calls_today = 999
        self.assertTrue(_check_daily_budget("balance"))

    def test_free_fill_verify_always_true(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            _ss_mod._calls_today = 999
            self.assertTrue(_check_daily_budget("fill_verify"))

    def test_free_balance_within_budget_true(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            _ss_mod._calls_today = 0
            self.assertTrue(_check_daily_budget("balance"))

    def test_free_balance_at_limit_false(self):
        import datetime
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            # Set today's date so the daily-reset branch is NOT triggered
            _ss_mod._today_date = datetime.date.today().isoformat()
            _ss_mod._calls_today = _ss_mod._FREE_DAILY_BUDGET - 10
            self.assertFalse(_check_daily_budget("balance"))

    def test_date_change_resets_calls_today(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            _ss_mod._calls_today = 99
            _ss_mod._today_date = "1970-01-01"  # old date → triggers reset
            # After reset, _calls_today becomes 0, so balance check → True
            result = _check_daily_budget("balance")
            self.assertTrue(result)
            self.assertEqual(_ss_mod._calls_today, 0)


# ===========================================================================
# API stats and call counting
# ===========================================================================

class TestApiStats(_SS):
    def test_get_api_stats_tier_paid(self):
        stats = get_api_stats()
        self.assertEqual(stats["tier"], "paid")

    def test_get_api_stats_tier_free(self):
        with patch.object(_ss_mod, "cfg", _FREE_CFG):
            stats = get_api_stats()
        self.assertEqual(stats["tier"], "free")

    def test_get_api_stats_has_required_keys(self):
        stats = get_api_stats()
        for key in ("tier", "calls_this_session", "calls_today",
                    "session_uptime_hours", "daily_budget", "call_interval_secs"):
            self.assertIn(key, stats)

    def test_record_external_call_increments_session_counter(self):
        record_external_call(3)
        self.assertEqual(_ss_mod._calls_this_session, 3)

    def test_record_external_call_increments_daily_counter(self):
        record_external_call(5)
        self.assertEqual(_ss_mod._calls_today, 5)

    def test_record_external_call_tracks_endpoint_breakdown(self):
        record_external_call(endpoint="/token/info/asset123")
        stats = get_api_stats()
        self.assertEqual(
            stats["calls_by_endpoint"],
            {"/token/info/{asset_id}": 1},
        )


# ===========================================================================
# is_known_wallet_address
# ===========================================================================

class TestIsKnownWalletAddress(_SS):
    def _inject_cache(self, addresses):
        _ss_mod._known_wallet_addresses_cache = set(addresses)
        _ss_mod._known_wallet_addresses_cache_at = time.time()

    def test_empty_address_returns_false(self):
        self.assertFalse(is_known_wallet_address(""))

    def test_none_address_returns_false(self):
        self.assertFalse(is_known_wallet_address(None))

    def test_known_address_returns_true(self):
        self._inject_cache(["xch1abc123"])
        self.assertTrue(is_known_wallet_address("xch1abc123"))

    def test_unknown_address_returns_false(self):
        self._inject_cache(["xch1abc123"])
        self.assertFalse(is_known_wallet_address("xch1def456"))

    def test_explicit_addresses_override(self):
        self._inject_cache([])
        self.assertTrue(is_known_wallet_address("xch1extra", explicit_addresses={"xch1extra"}))


# ===========================================================================
# is_coin_spent (via mocked _spacescan_get)
# ===========================================================================

class TestIsCoinSpent(_SS):
    def _mock_get(self, return_value):
        return patch.object(_ss_mod, "_spacescan_get", return_value=return_value)

    def test_disabled_returns_none(self):
        cfg_no = SimpleNamespace(**{**_PRO_CFG.__dict__, "SPACESCAN_ENABLED": False})
        with patch.object(_ss_mod, "cfg", cfg_no):
            result = is_coin_spent("abc123")
        self.assertIsNone(result)

    def test_api_failure_returns_none(self):
        with self._mock_get(None):
            result = is_coin_spent("abc123")
        self.assertIsNone(result)

    def test_empty_coin_data_returns_none(self):
        # amount_value=None, no sender/receiver → "not found"
        fake_data = {"coin": {"amount_value": None, "sender": {}, "receiver": {}}}
        with self._mock_get(fake_data):
            result = is_coin_spent("abc123")
        self.assertIsNone(result)

    def test_spent_coin_returns_true(self):
        fake_data = {
            "coin": {
                "amount_value": "1000",
                "spent_block": 4567890,
                "sender": {"address": "xch1sender"},
                "receiver": {"address": "xch1receiver"},
                "offer_info": [],
            },
            "coins": [],
        }
        with self._mock_get(fake_data):
            result = is_coin_spent("0xabc123")
        self.assertIsNotNone(result)
        self.assertTrue(result["spent"])

    def test_unspent_coin_returns_false(self):
        fake_data = {
            "coin": {
                "amount_value": "1000",
                "spent_block": None,
                "sender": {"address": "xch1sender"},
                "receiver": {"address": "xch1receiver"},
                "offer_info": [],
            },
            "coins": [],
        }
        with self._mock_get(fake_data):
            result = is_coin_spent("0xabc123")
        self.assertFalse(result["spent"])

    def test_0x_prefix_added_when_missing(self):
        # Capture the endpoint arg to verify 0x was prepended
        calls = []
        def capture(ep):
            calls.append(ep)
            return None
        with patch.object(_ss_mod, "_spacescan_get", side_effect=capture):
            is_coin_spent("abc123")
        self.assertTrue(calls[0].endswith("/0xabc123"))


# ===========================================================================
# verify_fill decision tree (via mocked is_coin_spent)
# ===========================================================================

class TestVerifyFill(_SS):
    OUR_ADDR = "xch1ourwallet"

    def _mock_ics(self, return_value):
        return patch.object(_ss_mod, "is_coin_spent", return_value=return_value)

    def test_api_error_returns_none(self):
        with self._mock_ics(None):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertIsNone(rv)

    def test_unspent_coin_returns_false(self):
        with self._mock_ics({"spent": False, "offer_info": [], "child_coins": [],
                             "receiver_address": "", "amount": ""}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertFalse(rv)

    def test_offer_info_status_4_returns_true(self):
        with self._mock_ics({"spent": True,
                             "offer_info": [{"offer_status": 4, "hash_base_58": "abc"}],
                             "child_coins": [], "receiver_address": ""}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertTrue(rv)

    def test_offer_info_status_3_returns_false(self):
        with self._mock_ics({"spent": True,
                             "offer_info": [{"offer_status": 3, "hash_base_58": "abc"}],
                             "child_coins": [], "receiver_address": ""}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertFalse(rv)

    def test_offer_info_completed_wins_over_cancelled(self):
        with self._mock_ics({"spent": True,
                             "offer_info": [{"offer_status": 3, "hash_base_58": "a"},
                                            {"offer_status": 4, "hash_base_58": "b"}],
                             "child_coins": [], "receiver_address": ""}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertTrue(rv)

    def test_child_coin_external_address_returns_true(self):
        _ss_mod._known_wallet_addresses_cache = {self.OUR_ADDR}
        _ss_mod._known_wallet_addresses_cache_at = time.time()
        with self._mock_ics({"spent": True, "offer_info": [],
                             "child_coins": [{"cointype": "child",
                                              "owner_address": "xch1taker"}],
                             "receiver_address": ""}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertTrue(rv)

    def test_child_coins_all_ours_returns_false(self):
        _ss_mod._known_wallet_addresses_cache = {self.OUR_ADDR}
        _ss_mod._known_wallet_addresses_cache_at = time.time()
        with self._mock_ics({"spent": True, "offer_info": [],
                             "child_coins": [{"cointype": "child",
                                              "owner_address": self.OUR_ADDR}],
                             "receiver_address": ""}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertFalse(rv)

    def test_child_coin_to_sibling_change_address_returns_false(self):
        _ss_mod._known_wallet_addresses_cache = {self.OUR_ADDR}
        _ss_mod._known_wallet_addresses_cache_at = time.time()
        with self._mock_ics({
            "spent": True,
            "offer_info": [],
            "child_coins": [
                {"cointype": "parent", "owner_address": self.OUR_ADDR},
                {"cointype": "siblings", "owner_address": "xch1changeaddress"},
                {"cointype": "child", "owner_address": "xch1changeaddress"},
            ],
            "receiver_address": "",
        }):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertFalse(rv)

    def test_child_coin_to_external_address_still_returns_true_with_siblings(self):
        _ss_mod._known_wallet_addresses_cache = {self.OUR_ADDR}
        _ss_mod._known_wallet_addresses_cache_at = time.time()
        with self._mock_ics({
            "spent": True,
            "offer_info": [],
            "child_coins": [
                {"cointype": "parent", "owner_address": self.OUR_ADDR},
                {"cointype": "siblings", "owner_address": "xch1changeaddress"},
                {"cointype": "child", "owner_address": "xch1takeraddress"},
            ],
            "receiver_address": "",
        }):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertTrue(rv)

    def test_receiver_is_ours_returns_false(self):
        _ss_mod._known_wallet_addresses_cache = {self.OUR_ADDR}
        _ss_mod._known_wallet_addresses_cache_at = time.time()
        with self._mock_ics({"spent": True, "offer_info": [], "child_coins": [],
                             "receiver_address": self.OUR_ADDR}):
            rv = verify_fill("coin1", self.OUR_ADDR)
        self.assertFalse(rv)


# ===========================================================================
# get_xch_balance and get_token_balance (via mocked _spacescan_get)
# ===========================================================================

class TestBalanceFunctions(_SS):
    def _mock_get(self, return_value):
        return patch.object(_ss_mod, "_spacescan_get", return_value=return_value)

    def test_get_xch_balance_success(self):
        with self._mock_get({"status": "success", "xch": "12.345"}):
            result = get_xch_balance("xch1abc")
        self.assertEqual(result, Decimal("12.345"))

    def test_get_xch_balance_api_failure_returns_none(self):
        with self._mock_get(None):
            result = get_xch_balance("xch1abc")
        self.assertIsNone(result)

    def test_get_xch_balance_parse_error_returns_none(self):
        with self._mock_get({"status": "success", "xch": "not-a-number"}):
            result = get_xch_balance("xch1abc")
        self.assertIsNone(result)

    def test_get_token_balance_with_asset_id(self):
        balances = [
            {"asset_id": "abc123", "balance": "500"},
            {"asset_id": "def456", "balance": "1000"},
        ]
        with self._mock_get({"data": balances}):
            result = get_token_balance("xch1abc", "abc123")
        self.assertEqual(result, Decimal("500"))

    def test_get_token_balance_asset_id_not_found_returns_zero(self):
        balances = [{"asset_id": "other", "balance": "1000"}]
        with self._mock_get({"data": balances}):
            result = get_token_balance("xch1abc", "abc123")
        self.assertEqual(result, Decimal("0"))

    def test_get_token_balance_no_asset_id_returns_first(self):
        balances = [{"asset_id": "abc", "balance": "999"}]
        with self._mock_get({"data": balances}):
            result = get_token_balance("xch1abc")
        self.assertEqual(result, Decimal("999"))

    def test_get_token_balance_empty_list_returns_zero(self):
        with self._mock_get({"data": []}):
            result = get_token_balance("xch1abc")
        self.assertEqual(result, Decimal("0"))

    def test_get_token_balance_api_failure_returns_none(self):
        with self._mock_get(None):
            result = get_token_balance("xch1abc", "abc123")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
