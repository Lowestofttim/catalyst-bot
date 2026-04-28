import importlib
import os
import sys
import types
import unittest


_MODS_TO_RESTORE = ("coin_prep_worker", "wallet_sage", "database", "wallet", "dotenv")


class CoinPrepConfirmedViewTests(unittest.TestCase):
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
        self._saved_modules = {
            name: sys.modules.get(name) for name in _MODS_TO_RESTORE
        }
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
        fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_transaction = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "records": [
                {
                    "coin": {
                        "parent_coin_info": "cc" * 32,
                        "puzzle_hash": "dd" * 32,
                        "amount": 123,
                    },
                    "coin_id": "0x" + "22" * 32,
                }
            ],
        }
        sys.modules["wallet"] = fake_wallet

        fake_database = types.ModuleType("database")
        fake_database.init_database = lambda: None
        fake_database.upsert_coin = lambda *args, **kwargs: True
        fake_database.set_coin_designation = lambda *args, **kwargs: True
        fake_database.designate_reserve = lambda *args, **kwargs: True
        fake_database.get_reserve_coins = lambda *args, **kwargs: []
        fake_database.mark_coins_gone = lambda *args, **kwargs: True
        sys.modules["database"] = fake_database

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.get_current_key = lambda: {"fingerprint": "123"}
        fake_wallet_sage.get_spendable_coin_count = lambda wallet_id: 17 if wallet_id == 1 else 19
        fake_wallet_sage.get_selectable_coins_only = lambda wallet_id: {
            "success": True,
            "records": [
                {
                    "coin": {
                        "parent_coin_info": "cc" * 32,
                        "puzzle_hash": "dd" * 32,
                        "amount": 123,
                    },
                    "coin_id": "0x" + "22" * 32,
                }
            ],
        }
        fake_wallet_sage.get_wallet_balance = lambda wallet_id: {
            "success": True,
            "wallet_balance": {"spendable_balance": 0},
        }
        sys.modules["wallet_sage"] = fake_wallet_sage

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: True
        fake_dotenv.set_key = lambda *args, **kwargs: True
        sys.modules["dotenv"] = fake_dotenv

        sys.modules.pop("coin_prep_worker", None)
        self.coin_prep_worker = importlib.import_module("coin_prep_worker")
        self.worker = self.coin_prep_worker.CoinPrepWorker()

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

    def test_get_confirmed_coin_count_prefers_sage_count_endpoint(self):
        self.assertEqual(self.worker.get_confirmed_coin_count(1), 17)
        self.assertEqual(self.worker.get_confirmed_coin_count(2), 19)

    def test_get_coin_count_prefers_sage_count_endpoint(self):
        self.assertEqual(self.worker.get_coin_count(1), 17)
        self.assertEqual(self.worker.get_coin_count(2), 19)

    def test_get_coins_via_rpc_can_use_strict_selectable_view(self):
        strict_coins = self.worker._get_coins_via_rpc(1, "strict-test", selectable_only=True)
        default_coins = self.worker._get_coins_via_rpc(1, "default-test")

        self.assertEqual(len(strict_coins), 1)
        self.assertEqual(strict_coins[0]["amount"], 123)
        self.assertTrue(strict_coins[0]["coin_id"].startswith("0x22"))

        self.assertEqual(len(default_coins), 1)
        self.assertEqual(default_coins[0]["amount"], 123)
        self.assertTrue(default_coins[0]["coin_id"].startswith("0x22"))

    def test_strict_selectable_helper_uses_selectable_view_not_merged_view(self):
        selectable_id = "0x" + "22" * 32
        merged_only_id = "0x" + "11" * 32

        self.assertTrue(
            self.worker._are_coin_ids_selectable(1, [selectable_id], "strict-helper")
        )
        self.assertFalse(
            self.worker._are_coin_ids_selectable(1, [merged_only_id], "strict-helper")
        )

    def test_status_targets_track_prepared_coins_not_reserve_bonus(self):
        self.assertEqual(self.worker.status.xch_coins_target, self.worker.xch_target_coins)
        self.assertEqual(self.worker.status.cat_coins_target, self.worker.cat_target_coins)
        self.assertEqual(self.worker.xch_expected_total_coins, self.worker.xch_target_coins + 1)
        self.assertEqual(self.worker.cat_expected_total_coins, self.worker.cat_target_coins + 1)

    def test_blank_template_env_values_use_worker_defaults(self):
        self.assertEqual(self.worker.xch_wallet_id, 1)
        self.assertEqual(self.worker.cat_wallet_id, 2)
        self.assertEqual(self.worker.xch_target_coins, 50)
        self.assertEqual(self.worker.cat_target_coins, 50)
        self.assertEqual(self.worker.cat_decimals, 3)

    def test_preselected_pool_helper_falls_back_to_same_amount_coin_when_exact_id_not_found(self):
        # When the exact coin ID is not selectable AND a selectable coin with the
        # same amount exists, the worker should fall back to that coin.
        # This handles stale wallet data where the pool coin map was built before
        # the split confirmed.
        fallback_coin = {
            "coin_id": "0x" + "44" * 32,
            "id": "0x" + "44" * 32,
            "amount": 220,
            "amount_mojos": 220,
        }

        original_get = self.worker._get_coins_via_rpc
        original_selectable = self.worker._are_coin_ids_selectable
        try:
            def fake_get(wallet_id, name, selectable_only=False):
                if selectable_only:
                    return [fallback_coin]
                return []

            self.worker._get_coins_via_rpc = fake_get
            self.worker._are_coin_ids_selectable = lambda *args, **kwargs: False

            resolved = self.worker._wait_for_preselected_pool_coin(
                wallet_id=1,
                pool_coin={"coin_id": "0x" + "33" * 32, "amount": 220, "amount_mojos": 220},
                side_label="XCH",
                tier_name="fees",
                timeout_s=1,
                poll_interval_s=1,
            )
        finally:
            self.worker._get_coins_via_rpc = original_get
            self.worker._are_coin_ids_selectable = original_selectable

        # Amount-fallback: returns the selectable coin with matching amount
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.get("coin_id"), fallback_coin["coin_id"])

    def test_preselected_pool_helper_can_resolve_exact_id_from_selectable_check(self):
        expected_coin_id = "0x" + "33" * 32

        original_get = self.worker._get_coins_via_rpc
        original_selectable = self.worker._are_coin_ids_selectable
        try:
            self.worker._get_coins_via_rpc = lambda *args, **kwargs: []

            def fake_selectable(wallet_id, coin_ids, label):
                normalized = [cid.replace("0x", "").lower() for cid in coin_ids]
                return normalized == [expected_coin_id.replace("0x", "").lower()]

            self.worker._are_coin_ids_selectable = fake_selectable

            resolved = self.worker._wait_for_preselected_pool_coin(
                wallet_id=1,
                pool_coin={"coin_id": expected_coin_id, "amount": 220, "amount_mojos": 220},
                side_label="XCH",
                tier_name="fees",
                timeout_s=1,
                poll_interval_s=1,
            )
        finally:
            self.worker._get_coins_via_rpc = original_get
            self.worker._are_coin_ids_selectable = original_selectable

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["coin_id"].replace("0x", "").lower(), ("33" * 32))
        self.assertEqual(resolved["amount"], 220)

    def test_extract_sage_transaction_ids_handles_both_plural_and_single_fields(self):
        tx_ids = self.coin_prep_worker.CoinPrepWorker._extract_sage_transaction_ids({
            "transaction_ids": ["aa" * 32],
            "transaction_id": "0x" + "bb" * 32,
            "transaction": {"transaction_id": "cc" * 32},
        })

        self.assertEqual(tx_ids, [
            "0x" + "aa" * 32,
            "0x" + "bb" * 32,
            "0x" + "cc" * 32,
        ])

    def test_transaction_confirmation_state_marks_single_tx_confirmed(self):
        original_get_transaction = self.coin_prep_worker.get_transaction
        try:
            self.coin_prep_worker.get_transaction = lambda tx_id: {
                "confirmed": True,
                "confirmed_at_height": 12345,
            }
            state = self.worker._get_transaction_confirmation_state(["0x" + "aa" * 32])
        finally:
            self.coin_prep_worker.get_transaction = original_get_transaction

        self.assertTrue(state["confirmed"])
        self.assertEqual(state["confirmed_count"], 1)
        self.assertEqual(state["height"], 12345)


if __name__ == "__main__":
    unittest.main()
