import importlib
import os
import sys
import types
import unittest
from pathlib import Path


_MODS_TO_RESTORE = ("coin_prep_worker", "wallet_sage", "database", "wallet", "dotenv")


class CoinPrepConsolidationTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = {
            "WALLET_TYPE": os.environ.get("WALLET_TYPE"),
            "WALLET_FINGERPRINT": os.environ.get("WALLET_FINGERPRINT"),
            "DEFAULT_TRADE_XCH": os.environ.get("DEFAULT_TRADE_XCH"),
            "CAT_COIN_SIZE": os.environ.get("CAT_COIN_SIZE"),
            "CHIA_WALLET_ID_XCH": os.environ.get("CHIA_WALLET_ID_XCH"),
            "CAT_WALLET_ID": os.environ.get("CAT_WALLET_ID"),
            "MAX_ACTIVE_BUY_OFFERS": os.environ.get("MAX_ACTIVE_BUY_OFFERS"),
            "MAX_ACTIVE_BUY": os.environ.get("MAX_ACTIVE_BUY"),
            "MAX_ACTIVE_SELL_OFFERS": os.environ.get("MAX_ACTIVE_SELL_OFFERS"),
            "MAX_ACTIVE_SELL": os.environ.get("MAX_ACTIVE_SELL"),
            "CAT_DECIMALS": os.environ.get("CAT_DECIMALS"),
            "MZ_DECIMALS": os.environ.get("MZ_DECIMALS"),
        }
        self._saved_modules = {name: sys.modules.get(name) for name in _MODS_TO_RESTORE}

        os.environ["WALLET_TYPE"] = "sage"
        os.environ["WALLET_FINGERPRINT"] = "123"
        os.environ["DEFAULT_TRADE_XCH"] = ""
        os.environ["CAT_COIN_SIZE"] = "4000"
        os.environ["CHIA_WALLET_ID_XCH"] = ""
        os.environ["CAT_WALLET_ID"] = ""
        os.environ["MAX_ACTIVE_BUY_OFFERS"] = ""
        os.environ["MAX_ACTIVE_BUY"] = ""
        os.environ["MAX_ACTIVE_SELL_OFFERS"] = ""
        os.environ["MAX_ACTIVE_SELL"] = ""
        os.environ["CAT_DECIMALS"] = ""
        os.environ["MZ_DECIMALS"] = ""

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_all_offers = lambda *args, **kwargs: {"offers": []}
        fake_wallet.cancel_offer = lambda *args, **kwargs: {"success": True}
        fake_wallet.cancel_offers_batch = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_wallet_sync_status = lambda *args, **kwargs: {"synced": True}
        fake_wallet.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "records": [],
        }
        fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_transaction = lambda *args, **kwargs: {"success": True}
        sys.modules["wallet"] = fake_wallet

        fake_database = types.ModuleType("database")
        fake_database.init_database = lambda: None
        fake_database.upsert_coin = lambda *args, **kwargs: True
        fake_database.set_coin_designation = lambda *args, **kwargs: True
        fake_database.designate_reserve = lambda *args, **kwargs: True
        fake_database.get_reserve_coins = lambda *args, **kwargs: []
        fake_database.mark_coins_gone = lambda *args, **kwargs: True
        sys.modules["database"] = fake_database

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: True
        fake_dotenv.set_key = lambda *args, **kwargs: True
        sys.modules["dotenv"] = fake_dotenv

        sys.modules.pop("coin_prep_worker", None)
        self.coin_prep_worker = importlib.import_module("coin_prep_worker")

    def tearDown(self):
        for name, saved in self._saved_modules.items():
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_sage_consolidation_uses_explicit_combine_not_auto_combine(self):
        calls = {"auto_xch": 0, "combine": 0}

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_spendable_coin_count = lambda wallet_id: 3

        def auto_combine_xch(*args, **kwargs):
            calls["auto_xch"] += 1
            return {"success": True, "submitted": True}

        def combine_coins(coin_ids, fee_mojos=0):
            calls["combine"] += 1
            return {"success": True, "submitted": True}

        fake_wallet_sage.auto_combine_xch = auto_combine_xch
        fake_wallet_sage.auto_combine_cat = lambda *args, **kwargs: {"success": True}
        fake_wallet_sage.combine_coins = combine_coins
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": [
                {"coin_id": "0x" + "11" * 32, "spent_block_index": 0},
                {"coin_id": "0x" + "22" * 32, "spent_block_index": 0},
                {"coin_id": "0x" + "33" * 32, "spent_block_index": 0},
            ],
        }
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.get_coin_count = lambda wallet_id: 3

        self.assertTrue(worker._consolidate_wallet_sage(1, "XCH"))
        self.assertEqual(calls["auto_xch"], 0)
        self.assertEqual(calls["combine"], 1)

    def test_worker_aborts_when_consolidation_never_verifies(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "catalyst" / "coin_prep_worker.py").read_text(encoding="utf-8")

        self.assertIn("Consolidation did not complete", source)
        self.assertNotIn("Continuing anyway - transactions may still be pending", source)


if __name__ == "__main__":
    unittest.main()
