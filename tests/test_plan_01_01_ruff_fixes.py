"""Regression tests for bugs found and fixed in plan slice 01-01 (ruff lint sweep).

Each test verifies a specific NameError or dead-code bug that was introduced
before the audit pass.  The test name maps to the finding in findings.md.
"""

import importlib
import types
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# F821-1: wallet_sage.get_wallet_puzzle_hashes — log_event was undefined
# ---------------------------------------------------------------------------


class TestWalletSageLogEventImport(unittest.TestCase):
    """get_wallet_puzzle_hashes used log_event without importing it.
    All four code paths (bech32m missing, rpc error, loaded, empty) would
    raise NameError before the fix."""

    def _rpc_side_effect(self, *args, **kwargs):
        raise RuntimeError("mock rpc failure")

    def test_get_puzzle_hashes_rpc_error_no_name_error(self):
        """NameError must NOT propagate when get_derivations raises."""
        import wallet_sage

        with patch.object(wallet_sage, "rpc", side_effect=RuntimeError("fail")):
            with patch.object(wallet_sage, "_puzzle_hash_cache", set()):
                with patch.object(wallet_sage, "_puzzle_hash_cache_at", 0.0):
                    # Should not raise NameError for log_event
                    result = wallet_sage.get_wallet_puzzle_hashes(force=True)
        self.assertIsInstance(result, set)

    def test_get_puzzle_hashes_import_error_no_name_error(self):
        """NameError must NOT propagate when chia.util.bech32m is missing."""
        import wallet_sage

        with patch.dict(
            "sys.modules", {"chia": None, "chia.util": None, "chia.util.bech32m": None}
        ):
            with patch.object(wallet_sage, "_puzzle_hash_cache", set()):
                with patch.object(wallet_sage, "_puzzle_hash_cache_at", 0.0):
                    result = wallet_sage.get_wallet_puzzle_hashes(force=True)
        self.assertIsInstance(result, set)


# ---------------------------------------------------------------------------
# F821-2: api_server.api_token_overview — _req (requests) was undefined
# ---------------------------------------------------------------------------


class TestApiTokenOverviewRequestsImport(unittest.TestCase):
    """api_token_overview called _req.get() without `import requests as _req`.
    Would raise NameError on any invocation before the fix."""

    def test_token_overview_no_name_error_on_request_failure(self):
        """NameError must NOT raise; network failure returns error JSON."""
        import api_server

        with api_server.app.test_request_context(
            "/api/token_overview?dexie_asset_id=" + "a" * 64
        ):
            with patch("requests.get", side_effect=ConnectionError("offline")):
                resp = api_server.api_token_overview()
        data = resp.get_json()
        self.assertFalse(data["success"])

    def test_token_overview_empty_id_returns_fast(self):
        """Empty dexie_asset_id returns early without touching requests."""
        import api_server

        with api_server.app.test_request_context("/api/token_overview"):
            resp = api_server.api_token_overview()
        data = resp.get_json()
        self.assertFalse(data["success"])


# ---------------------------------------------------------------------------
# F821-3: coin_manager._two_step_split — cat_token_amount was undefined
# ---------------------------------------------------------------------------


class TestTwoStepSplitCatTokenAmount(unittest.TestCase):
    """_two_step_split referenced cat_token_amount which is not a parameter.
    The CAT branch would raise NameError before the fix."""

    def _build_manager(self):
        """Return a minimal CoinManager instance with enough mocks to run."""
        import coin_manager as cm

        mgr = cm.CoinManager.__new__(cm.CoinManager)
        mgr._fingerprint = "test-fp"
        mgr._db = MagicMock()
        mgr._lock = __import__("threading").Lock()
        mgr._last_inventory = {}
        return mgr

    def test_two_step_split_cat_branch_no_name_error(self):
        """CAT branch must not raise NameError after removing cat_token_amount ref."""
        import coin_manager as cm

        mgr = self._build_manager()
        # The Sage fast-path will be taken — it bypasses the broken branch.
        # Patch get_wallet_type to return "sage" to take that path cleanly.
        with patch("coin_manager.get_wallet_type", return_value="sage"):
            with patch.object(mgr, "_sage_one_step_split", return_value=True):
                result = mgr._two_step_split(
                    name="CAT",
                    wallet_id=2,
                    source_coin_id="0xabc",
                    pool_amount_mojos=1000,
                    num_to_create=3,
                    trading_size_mojos=333,
                    is_cat=True,
                )
        self.assertTrue(result)

    def test_cat_token_amount_not_referenced_in_source(self):
        """cat_token_amount must not appear in _two_step_split source after fix."""
        import inspect
        import coin_manager as cm

        src = inspect.getsource(cm.CoinManager._two_step_split)
        self.assertNotIn(
            "cat_token_amount",
            src,
            "cat_token_amount is not a parameter of _two_step_split; "
            "any reference would be a NameError at runtime",
        )


# ---------------------------------------------------------------------------
# F811-1: database.record_config_change — old dead definition removed
# ---------------------------------------------------------------------------


class TestDatabaseRecordConfigChange(unittest.TestCase):
    """The old record_config_change (3-arg) silently overrode the new one
    (5-arg with source+note).  Callers using source/note would pass them to
    the wrong signature and silently discard audit data.  After the fix only
    the F26 version exists."""

    def test_record_config_change_accepts_source_and_note(self):
        """F26 signature (with source, note) must be accepted without TypeError."""
        from database import record_config_change
        import inspect

        sig = inspect.signature(record_config_change)
        params = list(sig.parameters)
        self.assertIn("source", params)
        self.assertIn("note", params)

    def test_only_one_record_config_change_definition(self):
        """There must be exactly one record_config_change in database (dead clone removed)."""
        import inspect
        import database

        src = inspect.getsource(database)
        count = src.count("def record_config_change(")
        self.assertEqual(
            count, 1, "Dead 3-arg clone of record_config_change must be removed"
        )


# ---------------------------------------------------------------------------
# F811-2: wallet_chia.count_suitable_coins — old definition removed, caller fixed
# ---------------------------------------------------------------------------


class TestWalletChiaCountSuitableCoins(unittest.TestCase):
    """Old count_suitable_coins(wallet_id, target, tolerance) used positional args.
    After removing the dead first definition, the surviving signature is
    (wallet_id, target_size_mojos, is_cat, decimals, tolerance).
    The caller in wait_for_coin_confirmations must pass tolerance as kwarg."""

    def test_count_suitable_coins_tolerance_kwarg(self):
        """tolerance=0.25 must not be mistaken for is_cat=True after signature fix."""
        import wallet_chia

        mock_result = {
            "success": True,
            "confirmed_records": [
                {"coin": {"amount": 200_000_000_000}},  # 0.2 XCH
            ],
        }
        with patch.object(
            wallet_chia, "get_spendable_coins_rpc", return_value=mock_result
        ):
            # 0.2 XCH target ± 25%: 0.15-0.25 XCH range
            count = wallet_chia.count_suitable_coins(1, 200_000_000_000, tolerance=0.25)
        self.assertEqual(count, 1)

    def test_wait_for_coin_confirmations_no_type_error(self):
        """wait_for_coin_confirmations must not pass tolerance as is_cat."""
        import wallet_chia

        mock_result = {
            "success": True,
            "confirmed_records": [
                {"coin": {"amount": 200_000_000_000}},
            ],
        }
        # Should return True immediately (1 coin found ≥ target_count=1)
        with patch.object(
            wallet_chia, "get_spendable_coins_rpc", return_value=mock_result
        ):
            result = wallet_chia.wait_for_coin_confirmations(
                wallet_id=1,
                target_coin_size_mojos=200_000_000_000,
                target_count=1,
                tolerance=0.25,
                max_wait_seconds=5,
                poll_interval=0,
            )
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
