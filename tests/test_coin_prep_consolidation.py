import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch
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
            "CAT_ASSET_ID": os.environ.get("CAT_ASSET_ID"),
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

    def test_sage_consolidation_uses_send_to_self_and_subtracts_xch_fee(self):
        calls = {"send": []}
        counts = iter([3, 0, 1])

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_spendable_coin_count = lambda wallet_id: 3
        fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
            "address": "xch1self",
        }
        fake_wallet_sage.get_wallet_balance = lambda wallet_id: {
            "success": True,
            "wallet_balance": {"spendable_balance": 1000},
        }
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": [
                {"coin_id": "0x" + "11" * 32, "spent_block_index": 0, "amount": 400},
                {"coin_id": "0x" + "22" * 32, "spent_block_index": 0, "coin": {"amount": 600}},
            ],
        }

        def send_transaction(wallet_id, amount_mojos, address, fee_mojos=0, source_coin_ids=None):
            calls["send"].append({
                "wallet_id": wallet_id,
                "amount_mojos": amount_mojos,
                "address": address,
                "fee_mojos": fee_mojos,
                "source_coin_ids": source_coin_ids,
            })
            return {"success": True, "submitted": True}

        fake_wallet_sage.send_transaction = send_transaction
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.get_coin_count = lambda wallet_id: next(counts, 1)
        worker._tx_fee_mojos = lambda: 10

        with patch.object(self.coin_prep_worker.time, "sleep", return_value=None):
            self.assertTrue(worker._consolidate_wallet_sage(1, "XCH"))

        self.assertEqual(calls["send"], [{
            "wallet_id": 1,
            "amount_mojos": 990,
            "address": "xch1self",
            "fee_mojos": 10,
            "source_coin_ids": ["11" * 32, "22" * 32],
        }])

    def test_sage_cat_consolidation_sends_full_cat_balance_to_self(self):
        calls = {"send": []}
        counts = iter([2, 0, 1])

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_spendable_coin_count = lambda wallet_id: 2
        fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
            "address": "xch1self",
        }
        fake_wallet_sage.get_wallet_balance = lambda wallet_id: {
            "success": True,
            "wallet_balance": {"spendable_balance": 1000},
        }
        def get_spendable_coins_rpc(wallet_id):
            if wallet_id == 1:
                return {
                    "success": True,
                    "confirmed_records": [
                        {"coin_id": "0x" + "aa" * 32, "spent_block_index": 0, "amount": 25},
                        {"coin_id": "0x" + "bb" * 32, "spent_block_index": 0, "amount": 100},
                    ],
                }
            return {
                "success": True,
                "confirmed_records": [
                    {"coin_id": "0x" + "11" * 32, "spent_block_index": 0, "amount": 400},
                    {"coin_id": "0x" + "22" * 32, "spent_block_index": 0, "coin": {"amount": 600}},
                ],
            }
        fake_wallet_sage.get_spendable_coins_rpc = get_spendable_coins_rpc

        def send_transaction(wallet_id, amount_mojos, address, fee_mojos=0, source_coin_ids=None):
            calls["send"].append({
                "wallet_id": wallet_id,
                "amount_mojos": amount_mojos,
                "address": address,
                "fee_mojos": fee_mojos,
                "source_coin_ids": source_coin_ids,
            })
            return {"success": True, "submitted": True}

        fake_wallet_sage.send_transaction = send_transaction
        sys.modules["wallet_sage"] = fake_wallet_sage
        os.environ["CAT_ASSET_ID"] = "abc123"

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.cat_wallet_id = 2
        worker.get_coin_count = lambda wallet_id: next(counts, 1)
        worker._tx_fee_mojos = lambda: 10

        with patch.object(self.coin_prep_worker.time, "sleep", return_value=None):
            self.assertTrue(worker._consolidate_wallet_sage(2, "CAT"))

        self.assertEqual(calls["send"], [{
            "wallet_id": 2,
            "amount_mojos": 1000,
            "address": "xch1self",
            "fee_mojos": 10,
            "source_coin_ids": ["11" * 32, "22" * 32],
        }])

    def test_sage_large_xch_consolidation_uses_priority_fee(self):
        calls = {"send": []}
        counts = iter([20, 0, 1])

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_spendable_coin_count = lambda wallet_id: 20
        fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
            "address": "xch1self",
        }
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": [
                {"coin_id": "0x" + f"{i:064x}", "spent_block_index": 0, "amount": 100}
                for i in range(1, 21)
            ],
        }

        def send_transaction(wallet_id, amount_mojos, address, fee_mojos=0, source_coin_ids=None):
            calls["send"].append({
                "wallet_id": wallet_id,
                "amount_mojos": amount_mojos,
                "address": address,
                "fee_mojos": fee_mojos,
                "source_coin_ids": source_coin_ids,
            })
            return {"success": True, "submitted": True}

        fake_wallet_sage.send_transaction = send_transaction
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.get_coin_count = lambda wallet_id: next(counts, 1)
        worker._tx_fee_mojos = lambda: 10

        with patch.object(self.coin_prep_worker.time, "sleep", return_value=None):
            self.assertTrue(worker._consolidate_wallet_sage(1, "XCH"))

        self.assertEqual(calls["send"][0]["amount_mojos"], 1980)
        self.assertEqual(calls["send"][0]["fee_mojos"], 20)

    def test_sage_large_xch_consolidation_uses_one_balance_self_send(self):
        records = [
            {"coin_id": "0x" + f"{i:064x}", "spent_block_index": 0, "amount": 100}
            for i in range(1, 46)
        ]
        calls = []
        counts = iter([45, 0, 1])

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
            "address": "xch1self",
        }
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": list(records),
        }

        def send_transaction(wallet_id, amount_mojos, address, fee_mojos=0, source_coin_ids=None):
            calls.append({
                "wallet_id": wallet_id,
                "amount_mojos": amount_mojos,
                "address": address,
                "fee_mojos": fee_mojos,
                "source_coin_ids": list(source_coin_ids or []),
            })
            return {"success": True, "submitted": True}

        fake_wallet_sage.send_transaction = send_transaction
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.get_coin_count = lambda wallet_id: next(counts, 1)
        worker._tx_fee_mojos = lambda: 10

        with patch.object(self.coin_prep_worker.time, "sleep", return_value=None):
            self.assertTrue(worker._consolidate_wallet_sage(1, "XCH"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["amount_mojos"], 4500 - 20)
        self.assertEqual(calls[0]["fee_mojos"], 20)
        self.assertEqual(len(calls[0]["source_coin_ids"]), 45)

    def test_sage_consolidation_rejects_transient_pending_lock_that_restores_old_count(self):
        calls = []
        counts = iter([3, 0, 3, 3, 3])

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
            "address": "xch1self",
        }
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": [
                {"coin_id": "0x" + f"{i:064x}", "spent_block_index": 0, "amount": 100}
                for i in range(1, 4)
            ],
        }

        def send_transaction(wallet_id, amount_mojos, address, fee_mojos=0, source_coin_ids=None):
            calls.append(source_coin_ids)
            return {"success": True, "submitted": True}

        fake_wallet_sage.send_transaction = send_transaction
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.get_coin_count = lambda wallet_id: next(counts, 3)
        worker._tx_fee_mojos = lambda: 0

        with patch.object(self.coin_prep_worker.time, "sleep", return_value=None):
            self.assertFalse(worker._consolidate_wallet_sage(1, "XCH"))

        self.assertEqual(len(calls), 1)

    def test_sage_consolidation_recovers_by_resync_when_wallet_view_is_stale(self):
        calls = {"send": 0, "resync": 0}
        counts = iter([3, 0, 3, 3, 3, 3, 3, 3, 1])

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
            "address": "xch1self",
        }
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": [
                {"coin_id": "0x" + f"{i:064x}", "spent_block_index": 0, "amount": 100}
                for i in range(1, 4)
            ],
        }

        def send_transaction(wallet_id, amount_mojos, address, fee_mojos=0, source_coin_ids=None):
            calls["send"] += 1
            return {"success": True, "submitted": True}

        def sage_login(fingerprint, force_resync=False):
            calls["resync"] += 1
            self.assertEqual(fingerprint, 123)
            self.assertTrue(force_resync)
            return True

        fake_wallet_sage.send_transaction = send_transaction
        fake_wallet_sage.sage_login = sage_login
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker.get_coin_count = lambda wallet_id: next(counts, 1)
        worker._tx_fee_mojos = lambda: 0

        with patch.object(self.coin_prep_worker.time, "sleep", return_value=None):
            self.assertTrue(worker._consolidate_wallet_sage(1, "XCH"))

        self.assertEqual(calls, {"send": 1, "resync": 1})

    def test_worker_aborts_when_consolidation_never_verifies(self):
        source = (Path(__file__).resolve().parent.parent / "src" / "catalyst" / "coin_prep_worker.py").read_text(encoding="utf-8")

        self.assertIn("Consolidation did not complete", source)
        self.assertNotIn("Continuing anyway - transactions may still be pending", source)

    def test_sage_combine_zero_spendable_coins_is_not_success(self):
        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "confirmed_records": [],
        }
        fake_wallet_sage.combine_coins = lambda *args, **kwargs: {
            "success": True,
            "submitted": True,
        }
        sys.modules["wallet_sage"] = fake_wallet_sage

        worker = self.coin_prep_worker.CoinPrepWorker()
        worker._consolidate_wallet_sage_fallback = lambda wallet_id, name: False

        self.assertFalse(worker._consolidate_wallet_sage_combine(1, "XCH"))


if __name__ == "__main__":
    unittest.main()
