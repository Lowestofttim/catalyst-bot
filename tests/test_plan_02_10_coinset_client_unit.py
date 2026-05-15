"""Slice 02-10 — coinset_client.py unit tests.

No network calls. Tests static helpers (_extract_puzzle_hashes,
_format_as_wallet_response), stats methods (_record_api_call, get_stats),
guard-clause paths (disabled, rate limited, empty hint/coin), and the
verify_coin_spent_on_chain decision tree via mocked get_coin_by_name.
"""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

try:
    import coinset_client as _cc_mod
    from coinset_client import CoinsetClient

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_FAKE_CFG = SimpleNamespace(
    COINSET_ENABLED=True,
    COINSET_API_URL="https://api.coinset.org",
    COINSET_TIMEOUT=5,
    COINSET_FALLBACK_WALLET=True,
    WALLET_ID_XCH=1,
    CAT_WALLET_ID=2,
    WALLET_TYPE="sage",
)

_FAKE_CFG_DISABLED = SimpleNamespace(
    COINSET_ENABLED=False,
    WALLET_TYPE="sage",
)


@unittest.skipIf(_SKIP is not None, f"coinset_client unavailable: {_SKIP}")
class _CC(unittest.TestCase):
    def setUp(self):
        self._cfg_patcher = patch.object(_cc_mod, "cfg", _FAKE_CFG)
        self._cfg_patcher.start()
        self._config_patcher = patch("config.cfg", _FAKE_CFG)
        self._config_patcher.start()
        self._log_patcher = patch.object(_cc_mod, "log_event")
        self._log_patcher.start()
        self._client = CoinsetClient()

    def tearDown(self):
        self._cfg_patcher.stop()
        self._config_patcher.stop()
        self._log_patcher.stop()


# ===========================================================================
# Static helper: _extract_puzzle_hashes
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coinset_client unavailable: {_SKIP}")
class TestExtractPuzzleHashes(unittest.TestCase):
    def test_none_returns_empty(self):
        result = CoinsetClient._extract_puzzle_hashes(None)
        self.assertEqual(len(result), 0)

    def test_non_dict_returns_empty(self):
        result = CoinsetClient._extract_puzzle_hashes("bad")
        self.assertEqual(len(result), 0)

    def test_coin_records_format(self):
        rpc = {
            "coin_records": [
                {"coin": {"puzzle_hash": "0xabc"}},
                {"coin": {"puzzle_hash": "0xdef"}},
            ]
        }
        result = CoinsetClient._extract_puzzle_hashes(rpc)
        self.assertIn("0xabc", result)
        self.assertIn("0xdef", result)

    def test_confirmed_records_fallback(self):
        rpc = {"confirmed_records": [{"puzzle_hash": "0x123"}]}
        result = CoinsetClient._extract_puzzle_hashes(rpc)
        self.assertIn("0x123", result)

    def test_deduplication(self):
        rpc = {
            "coin_records": [
                {"coin": {"puzzle_hash": "0xaaa"}},
                {"coin": {"puzzle_hash": "0xaaa"}},
            ]
        }
        result = CoinsetClient._extract_puzzle_hashes(rpc)
        self.assertEqual(len(result), 1)

    def test_missing_puzzle_hash_skipped(self):
        rpc = {"coin_records": [{"coin": {}}]}
        result = CoinsetClient._extract_puzzle_hashes(rpc)
        self.assertEqual(len(result), 0)


# ===========================================================================
# Static helper: _format_as_wallet_response
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"coinset_client unavailable: {_SKIP}")
class TestFormatAsWalletResponse(unittest.TestCase):
    def test_spent_coins_filtered_out(self):
        records = [
            {"spent": False, "coin": {"amount": 1000}},
            {"spent": True, "coin": {"amount": 2000}},
        ]
        result = CoinsetClient._format_as_wallet_response(records)
        self.assertEqual(len(result["coin_records"]), 1)

    def test_has_source_key(self):
        result = CoinsetClient._format_as_wallet_response([])
        self.assertEqual(result["_source"], "coinset")

    def test_success_true(self):
        result = CoinsetClient._format_as_wallet_response([])
        self.assertTrue(result["success"])

    def test_empty_list_valid(self):
        result = CoinsetClient._format_as_wallet_response([])
        self.assertEqual(result["coin_records"], [])


# ===========================================================================
# Stats and counter helpers
# ===========================================================================


class TestStatsAndCounters(_CC):
    def test_initial_stats_all_zero(self):
        stats = self._client.get_stats()
        self.assertEqual(stats["api_calls_total"], 0)
        self.assertEqual(stats["coinset_hits"], 0)
        self.assertEqual(stats["api_errors_total"], 0)

    def test_record_api_call_increments_total(self):
        self._client._record_api_call("test_method")
        stats = self._client.get_stats()
        self.assertEqual(stats["api_calls_total"], 1)

    def test_record_api_call_increments_by_method(self):
        self._client._record_api_call("test_method")
        self._client._record_api_call("test_method")
        stats = self._client.get_stats()
        self.assertEqual(stats["api_calls_by_method"].get("test_method"), 2)

    def test_record_api_error_increments_counter(self):
        self._client._record_api_error()
        stats = self._client.get_stats()
        self.assertEqual(stats["api_errors_total"], 1)

    def test_get_stats_mode_sage(self):
        # _FAKE_CFG has WALLET_TYPE="sage" → mode="sage_compat"
        stats = self._client.get_stats()
        self.assertEqual(stats["mode"], "sage_compat")

    def test_get_stats_mode_chia_initialized(self):
        chia_cfg = SimpleNamespace(**{**_FAKE_CFG.__dict__, "WALLET_TYPE": "chia"})
        with patch("config.cfg", chia_cfg):
            self._client._initialized = True
            stats = self._client.get_stats()
        self.assertEqual(stats["mode"], "initialized")

    def test_get_stats_mode_chia_pending(self):
        chia_cfg = SimpleNamespace(**{**_FAKE_CFG.__dict__, "WALLET_TYPE": "chia"})
        with patch("config.cfg", chia_cfg):
            self._client._initialized = False
            stats = self._client.get_stats()
        self.assertEqual(stats["mode"], "pending_init")

    def test_hit_rate_zero_queries(self):
        stats = self._client.get_stats()
        # 0/max(0,1) → 0.0%
        self.assertEqual(stats["hit_rate_pct"], 0.0)

    def test_puzzle_hashes_cached_count(self):
        self._client._puzzle_hashes = {1: {"0xaaa", "0xbbb"}, 2: {"0xccc"}}
        stats = self._client.get_stats()
        self.assertEqual(stats["puzzle_hashes_cached"], 3)


# ===========================================================================
# Guard clause paths (no network)
# ===========================================================================


class TestGuardClauses(_CC):
    def test_verify_coin_spent_disabled_returns_none(self):
        with patch.object(_cc_mod, "cfg", _FAKE_CFG_DISABLED):
            result = self._client.verify_coin_spent_on_chain("0xabc")
        self.assertIsNone(result)

    def test_verify_coin_spent_empty_coin_id_returns_none(self):
        result = self._client.verify_coin_spent_on_chain("")
        self.assertIsNone(result)

    def test_get_block_record_height_zero_returns_none(self):
        result = self._client.get_block_record_by_height(0)
        self.assertIsNone(result)

    def test_get_block_record_negative_height_returns_none(self):
        result = self._client.get_block_record_by_height(-1)
        self.assertIsNone(result)

    def test_get_coin_records_by_hint_empty_hint_returns_none(self):
        result = self._client.get_coin_records_by_hint("")
        self.assertIsNone(result)

    def test_rate_limited_returns_none(self):
        self._client._rate_limited_until = time.time() + 3600
        result = self._client.get_block_record_by_height(1234)
        self.assertIsNone(result)

    def test_coinset_disabled_get_coin_by_name_returns_none(self):
        with patch.object(_cc_mod, "cfg", _FAKE_CFG_DISABLED):
            result = self._client.get_coin_by_name("0xabc")
        self.assertIsNone(result)

    def test_get_additions_and_removals_empty_hash_returns_none(self):
        result = self._client.get_additions_and_removals("")
        self.assertIsNone(result)


# ===========================================================================
# verify_coin_spent_on_chain — decision tree (via mocked get_coin_by_name)
# ===========================================================================


class TestVerifyCoinSpentOnChain(_CC):
    def _mock_gcbn(self, return_value):
        return patch.object(self._client, "get_coin_by_name", return_value=return_value)

    def test_coin_not_found_returns_none(self):
        with self._mock_gcbn(None):
            result = self._client.verify_coin_spent_on_chain("0xabc")
        self.assertIsNone(result)

    def test_spent_block_index_positive_returns_true(self):
        with self._mock_gcbn({"spent_block_index": 4567890}):
            result = self._client.verify_coin_spent_on_chain("0xabc")
        self.assertTrue(result)

    def test_spent_block_index_zero_returns_false(self):
        with self._mock_gcbn({"spent_block_index": 0}):
            result = self._client.verify_coin_spent_on_chain("0xabc")
        self.assertFalse(result)

    def test_0x_prefix_stripped_and_readded(self):
        calls = []

        def capture_name(name):
            calls.append(name)
            return None

        with patch.object(self._client, "get_coin_by_name", side_effect=capture_name):
            self._client.verify_coin_spent_on_chain("0xABCDEF")
        # Should call get_coin_by_name("0x" + normalised)
        self.assertTrue(calls[0].startswith("0x"))
        self.assertEqual(calls[0], "0xabcdef")


# ===========================================================================
# get_spendable_coins — guard paths (via mocked internals)
# ===========================================================================


class TestGetSpendableCoins(_CC):
    def test_not_initialized_calls_fallback(self):
        self._client._initialized = False
        with patch.object(
            self._client, "_fallback_wallet_rpc", return_value=None
        ) as mock_fb:
            self._client.get_spendable_coins(1)
        mock_fb.assert_called_once()

    def test_missing_wallet_id_calls_fallback(self):
        self._client._initialized = True
        self._client._puzzle_hashes = {99: {"0xaaa"}}  # wallet 1 not present
        with patch.object(
            self._client, "_fallback_wallet_rpc", return_value=None
        ) as mock_fb:
            self._client.get_spendable_coins(1)
        mock_fb.assert_called_once()

    def test_coinset_success_returns_formatted_response(self):
        self._client._initialized = True
        self._client._puzzle_hashes = {1: {"0xaaa"}}
        fake_coins = [{"spent": False, "coin": {"amount": 1000}}]
        with patch.object(self._client, "_query_coinset", return_value=fake_coins):
            result = self._client.get_spendable_coins(1)
        self.assertIsNotNone(result)
        self.assertEqual(result["_source"], "coinset")
        self.assertEqual(self._client._coinset_hits, 1)

    def test_coinset_failure_falls_back(self):
        self._client._initialized = True
        self._client._puzzle_hashes = {1: {"0xaaa"}}
        with patch.object(
            self._client, "_query_coinset", side_effect=Exception("timeout")
        ):
            with patch.object(
                self._client, "_fallback_wallet_rpc", return_value={"fallback": True}
            ) as mock_fb:
                result = self._client.get_spendable_coins(1)
        mock_fb.assert_called_once()
        self.assertEqual(result, {"fallback": True})


if __name__ == "__main__":
    unittest.main()
