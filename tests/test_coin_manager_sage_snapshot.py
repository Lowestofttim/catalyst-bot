import importlib
import os
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


_MODS_TO_RESTORE = ("coin_manager", "wallet", "wallet_sage", "database", "config")


class CoinManagerSageSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._saved_wallet_type = os.environ.get("WALLET_TYPE")
        os.environ["WALLET_TYPE"] = "sage"
        self._saved_modules = {name: sys.modules.get(name) for name in _MODS_TO_RESTORE}
        self.calls = {"detailed": 0, "upserts": []}

        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            COINSET_ENABLED=False,
            WALLET_ID_XCH=1,
            CAT_WALLET_ID=2,
            TIER_ENABLED=False,
            CAT_DECIMALS=3,
            DEFAULT_TRADE_XCH=None,
            XCH_COIN_SIZE=Decimal("1"),
            CAT_COIN_SIZE=Decimal("1000"),
            COIN_PREP_HEADROOM_PCT=Decimal("10"),
            WALLET_FINGERPRINT="",
        )
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.log_event = lambda *args, **kwargs: None
        fake_database.upsert_coin = (
            lambda coin_id, wallet_type, amount, tier="unknown": self.calls[
                "upserts"
            ].append((coin_id, wallet_type, amount))
        )
        fake_database.get_free_coins = lambda wallet_type: []
        fake_database.mark_coins_gone = lambda coin_ids: 0
        fake_database.get_coin_summary = lambda: {}
        fake_database.get_open_offers = lambda cat_asset_id=None: []
        sys.modules["database"] = fake_database

        def _unexpected(*args, **kwargs):
            raise AssertionError(
                "legacy wallet coin path should not be called when detailed Sage snapshot is available"
            )

        def _owned_snapshot(wallet_id):
            self.calls["detailed"] += 1
            if wallet_id == 1:
                return {
                    "0x" + "11" * 32: {"amount": 111, "offer_id": None},
                    "0x" + "12" * 32: {"amount": 222, "offer_id": "offer-xch"},
                }
            return {
                "0x" + "21" * 32: {"amount": 333, "offer_id": None},
                "0x" + "22" * 32: {"amount": 444, "offer_id": "offer-cat"},
            }

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_exact_spendable_coins_rpc = _unexpected
        fake_wallet.get_all_coins_for_wallet = lambda *args, **kwargs: []
        fake_wallet.get_wallet_balance = lambda *args, **kwargs: {
            "wallet_balance": {"spendable_balance": 0}
        }
        fake_wallet.get_next_address = lambda *args, **kwargs: {
            "success": True,
            "address": "xch1testaddress0000000000000000000000000000000000000000000",
        }
        fake_wallet.send_transaction = lambda *args, **kwargs: {"success": True}
        fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_wallet_type = lambda: "sage"
        fake_wallet.WALLET_ID_XCH = 1
        fake_wallet.get_owned_coins = _unexpected
        fake_wallet.get_owned_coins_detailed = _owned_snapshot
        sys.modules["wallet"] = fake_wallet

        sys.modules.pop("coin_manager", None)
        self.coin_manager = importlib.import_module("coin_manager")
        with patch.object(
            self.coin_manager.CoinManager,
            "_resolve_fingerprint",
            return_value="123456789",
        ):
            self.manager = self.coin_manager.CoinManager()

    def tearDown(self):
        for name, saved in self._saved_modules.items():
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

        if self._saved_wallet_type is None:
            os.environ.pop("WALLET_TYPE", None)
        else:
            os.environ["WALLET_TYPE"] = self._saved_wallet_type

    def test_update_coin_counts_reuses_sage_owned_snapshot(self):
        xch_count, cat_count = self.manager.update_coin_counts()

        self.assertEqual((xch_count, cat_count), (1, 1))
        self.assertEqual(self.calls["detailed"], 2)
        self.assertEqual(
            {
                (coin_id, wallet_type, amount)
                for coin_id, wallet_type, amount in self.calls["upserts"]
            },
            {
                ("0x" + "11" * 32, "xch", 111),
                ("0x" + "21" * 32, "cat", 333),
            },
        )


if __name__ == "__main__":
    unittest.main()
