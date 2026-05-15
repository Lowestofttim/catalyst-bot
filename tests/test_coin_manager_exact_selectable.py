import importlib
import os
import sys
import types
import unittest


class CoinManagerExactSelectableTests(unittest.TestCase):
    def setUp(self):
        self._saved_wallet_type = os.environ.get("WALLET_TYPE")
        os.environ["WALLET_TYPE"] = "chia"

        # Save originals so tearDown can restore them — popping without
        # restoring leaves sys.modules dirty for subsequent test files
        # (they re-import fresh, but coin_manager's import-time bindings
        # to wallet.* functions can break if the wallet stub survives).
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in ("coin_manager", "wallet", "wallet_sage", "database", "config")
        }

        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            COINSET_ENABLED=False,
            WALLET_ID_XCH=1,
            CAT_WALLET_ID=2,
            TIER_ENABLED=False,
            CAT_DECIMALS=3,
        )
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.log_event = lambda *args, **kwargs: None
        sys.modules["database"] = fake_database

        merged_coin_id = "0x" + "11" * 32
        selectable_coin_id = "0x" + "22" * 32

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "records": [
                {
                    "coin": {
                        "parent_coin_info": "aa" * 32,
                        "puzzle_hash": "bb" * 32,
                        "amount": 111,
                    },
                    "coin_id": merged_coin_id,
                }
            ],
        }
        fake_wallet.get_exact_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "records": [
                {
                    "coin": {
                        "parent_coin_info": "cc" * 32,
                        "puzzle_hash": "dd" * 32,
                        "amount": 222,
                    },
                    "coin_id": selectable_coin_id,
                }
            ],
        }
        fake_wallet.get_all_coins_for_wallet = lambda *args, **kwargs: []
        fake_wallet.get_wallet_balance = lambda *args, **kwargs: {
            "wallet_balance": {"spendable_balance": 0}
        }
        fake_wallet.get_next_address = lambda *args, **kwargs: {
            "success": True,
            "address": "xch1test",
        }
        fake_wallet.send_transaction = lambda *args, **kwargs: {"success": True}
        fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_wallet_type = lambda: "chia"
        fake_wallet.WALLET_ID_XCH = 1
        fake_wallet.get_owned_coins = lambda *args, **kwargs: {}
        fake_wallet.get_owned_coins_detailed = lambda *a, **kw: None
        fake_wallet.rpc = lambda *args, **kwargs: {"fingerprint": "123"}
        sys.modules["wallet"] = fake_wallet

        sys.modules.pop("coin_manager", None)
        self.coin_manager = importlib.import_module("coin_manager")
        self.manager = self.coin_manager.CoinManager()
        self.selectable_coin_id = selectable_coin_id

    def tearDown(self):
        for name, saved in self._saved_modules.items():
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

        if self._saved_wallet_type is None:
            os.environ.pop("WALLET_TYPE", None)
        else:
            os.environ["WALLET_TYPE"] = self._saved_wallet_type

    def test_get_coins_fast_uses_exact_selectable_wallet_view(self):
        result = self.manager._get_coins_fast(1)
        records = self.coin_manager._extract_coin_records(result)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["coin"]["amount"], 222)
        self.assertEqual(records[0]["coin_id"], self.selectable_coin_id)

    def test_snapshot_coin_ids_uses_exact_selectable_wallet_view(self):
        snapshot = self.manager._snapshot_coin_ids(1, "test")
        self.assertEqual(snapshot, {self.selectable_coin_id: 222})


if __name__ == "__main__":
    unittest.main()
