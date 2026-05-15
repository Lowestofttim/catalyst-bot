"""Regression tests for slice 01-05 (type annotation audit).

Verifies the two real bugs found by mypy:
  1. wallet.py now exports get_spendable_coin_count (both branches)
  2. database.update_offer_status always returns bool (never falls off end)
"""

import inspect


class TestWalletExportsGetSpendableCoinCount:
    def test_wallet_has_get_spendable_coin_count(self):
        """wallet.py must export get_spendable_coin_count so api_server import works."""
        import wallet

        assert hasattr(wallet, "get_spendable_coin_count"), (
            "wallet module missing get_spendable_coin_count — api_server will ImportError"
        )

    def test_get_spendable_coin_count_callable(self):
        """The exported symbol must be callable."""
        import wallet

        assert callable(wallet.get_spendable_coin_count)

    def test_wallet_chia_has_get_spendable_coin_count(self):
        """wallet_chia.py must define get_spendable_coin_count for the Chia branch."""
        import wallet_chia

        assert hasattr(wallet_chia, "get_spendable_coin_count")
        assert callable(wallet_chia.get_spendable_coin_count)

    def test_wallet_sage_has_get_spendable_coin_count(self):
        """wallet_sage.py already has get_spendable_coin_count (sanity check)."""
        import wallet_sage

        assert hasattr(wallet_sage, "get_spendable_coin_count")

    def test_wallet_chia_count_returns_int_on_none_rpc(self):
        """wallet_chia.get_spendable_coin_count returns 0 (int) on RPC failure."""
        import wallet_chia
        from unittest.mock import patch

        with patch.object(wallet_chia, "get_spendable_coins_rpc", return_value=None):
            result = wallet_chia.get_spendable_coin_count(1)
        assert isinstance(result, int)
        assert result == 0

    def test_wallet_chia_count_returns_correct_count(self):
        """wallet_chia.get_spendable_coin_count counts confirmed_records."""
        import wallet_chia
        from unittest.mock import patch

        fake_rpc = {"confirmed_records": [{"coin": "a"}, {"coin": "b"}, {"coin": "c"}]}
        with patch.object(
            wallet_chia, "get_spendable_coins_rpc", return_value=fake_rpc
        ):
            result = wallet_chia.get_spendable_coin_count(1)
        assert result == 3


class TestDatabaseUpdateOfferStatusAlwaysReturns:
    def test_update_offer_status_has_trailing_return(self):
        """update_offer_status must not fall off the end of the for loop."""
        import database

        src = inspect.getsource(database.update_offer_status)
        # The function should have a return False after the for loop
        assert "return False  # all retries exhausted" in src

    def test_update_offer_status_returns_bool_on_missing_trade(self):
        """update_offer_status returns False gracefully for unknown trade_id."""
        from database import update_offer_status, init_database

        init_database()
        result = update_offer_status("nonexistent-trade-id-xyz", "cancelled")
        assert isinstance(result, bool)
