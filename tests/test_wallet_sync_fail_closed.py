import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


class _FakeCfg:
    CAT_ASSET_ID = "test-cat"
    OFFER_EXPIRY_SECS = 86400
    OFFER_STAGGER_SECS = 10
    DRY_RUN = False
    MAX_ACTIVE_BUY_OFFERS = 40
    MAX_ACTIVE_SELL_OFFERS = 40
    COIN_IDS_ENABLED = True
    DEXIE_AUTO_POST = False
    ENABLE_BUY = True
    ENABLE_SELL = True
    SPACESCAN_ENABLED = False
    WALLET_ADDRESS = "xch1test"


class WalletSyncFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.logged = []

        fake_config = types.ModuleType("config")
        fake_config.cfg = _FakeCfg()
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.add_offer = lambda *args, **kwargs: None
        fake_database.update_offer_status = lambda *args, **kwargs: True
        fake_database.update_offer_lifecycle_state = lambda *args, **kwargs: None
        fake_database.transition_offer = lambda *args, **kwargs: None
        fake_database.mark_cancel_attempted = lambda *args, **kwargs: None
        fake_database.update_offer_coin_id = lambda *args, **kwargs: None
        fake_database.update_offer_bech32 = lambda *args, **kwargs: None
        fake_database.get_open_offers = lambda *args, **kwargs: []
        fake_database.get_offer = lambda *args, **kwargs: None
        fake_database.record_fill = lambda *args, **kwargs: 1
        fake_database.get_unmatched_fills = lambda *args, **kwargs: []
        fake_database.match_round_trip = lambda *args, **kwargs: None
        fake_database.lock_coin = lambda *args, **kwargs: None
        fake_database.log_event = self._log_event
        sys.modules["database"] = fake_database

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.create_offer = lambda *args, **kwargs: {"success": True}
        fake_wallet.cancel_offer = lambda *args, **kwargs: {"success": True}
        fake_wallet.cancel_offers_batch = lambda *args, **kwargs: {}
        fake_wallet.get_all_offers = lambda *args, **kwargs: []
        fake_wallet.classify_offers_from_list = lambda *args, **kwargs: ([], [], [])
        fake_wallet.is_offer_time_expired = lambda *args, **kwargs: False
        fake_wallet.get_offer_expiry_info = lambda *args, **kwargs: {}
        fake_wallet.get_offer_bech32 = lambda *args, **kwargs: ""
        fake_wallet.cleanup_expired_offers = lambda *args, **kwargs: 0
        fake_wallet.get_exact_spendable_coins_rpc = lambda *args, **kwargs: {
            "success": True,
            "confirmed_records": [],
        }
        fake_wallet.get_wallet_type = lambda: "sage"
        fake_wallet.get_owned_coins_detailed = lambda *args, **kwargs: None
        fake_wallet.WALLET_ID_XCH = 1
        sys.modules["wallet"] = fake_wallet

        fake_coin_manager = types.ModuleType("coin_manager")
        fake_coin_manager._coin_id_from_record = lambda rec: rec.get("coin_id")
        sys.modules["coin_manager"] = fake_coin_manager

        fake_wallet_sage = types.ModuleType("wallet_sage")
        fake_wallet_sage.rpc = lambda *args, **kwargs: None
        sys.modules["wallet_sage"] = fake_wallet_sage

        fake_spacescan = types.ModuleType("spacescan")
        fake_spacescan.verify_fill = lambda *args, **kwargs: None
        sys.modules["spacescan"] = fake_spacescan

        fake_dexie_manager = types.ModuleType("dexie_manager")
        fake_dexie_manager.get_offer_detail = lambda *args, **kwargs: None
        sys.modules["dexie_manager"] = fake_dexie_manager

        sys.modules.pop("offer_manager", None)
        sys.modules.pop("fill_tracker", None)
        self.offer_manager = importlib.import_module("offer_manager")
        self.fill_tracker = importlib.import_module("fill_tracker")

    def tearDown(self):
        for name in [
            "offer_manager",
            "fill_tracker",
            "dexie_manager",
            "spacescan",
            "wallet_sage",
            "coin_manager",
            "wallet",
            "database",
            "config",
        ]:
            sys.modules.pop(name, None)

    def _log_event(self, severity, event_type, message, data=None):
        self.logged.append((severity, event_type, message, data))

    def test_sync_from_wallet_uses_cached_book_after_rpc_failure(self):
        manager = self.offer_manager.OfferManager()

        def fake_get_all_offers(*args, **kwargs):
            if not hasattr(fake_get_all_offers, "_calls"):
                fake_get_all_offers._calls = 0
            fake_get_all_offers._calls += 1
            if fake_get_all_offers._calls == 1:
                fake_get_all_offers._last_error = ""
                return [{"trade_id": "seed"}]
            fake_get_all_offers._last_error = "The read operation timed out"
            return None

        fake_get_all_offers._last_error = ""

        with (
            patch.object(self.offer_manager, "get_all_offers", new=fake_get_all_offers),
            patch.object(
                self.offer_manager,
                "classify_offers_from_list",
                return_value=(
                    [{"trade_id": "buy-live"}],
                    [{"trade_id": "sell-live"}],
                    [],
                ),
            ),
        ):
            fresh_buy, fresh_sell, _ = manager.sync_from_wallet()
            stale_buy, stale_sell, _ = manager.sync_from_wallet()

        self.assertEqual([o["trade_id"] for o in fresh_buy], ["buy-live"])
        self.assertEqual([o["trade_id"] for o in stale_buy], ["buy-live"])
        self.assertEqual([o["trade_id"] for o in stale_sell], ["sell-live"])

        meta = manager.get_wallet_sync_meta()
        self.assertFalse(meta["fresh"])
        self.assertTrue(meta["using_cache"])
        self.assertEqual(meta["consecutive_failures"], 1)
        self.assertIn("timed out", meta["last_error"])
        self.assertTrue(any(evt == "wallet_sync_cache" for _, evt, _, _ in self.logged))

    def test_expected_empty_wallet_book_after_cancel_all_bypasses_cache(self):
        manager = self.offer_manager.OfferManager()

        def fake_get_all_offers(*args, **kwargs):
            if not hasattr(fake_get_all_offers, "_calls"):
                fake_get_all_offers._calls = 0
            fake_get_all_offers._calls += 1
            if fake_get_all_offers._calls == 1:
                return [{"trade_id": "seed"}]
            return []

        def fake_classify(all_offers, _asset_id):
            if all_offers:
                return (
                    [{"trade_id": f"buy-live-{i}"} for i in range(3)],
                    [{"trade_id": f"sell-live-{i}"} for i in range(3)],
                    [],
                )
            return ([], [], [])

        with (
            patch.object(self.offer_manager, "get_all_offers", new=fake_get_all_offers),
            patch.object(
                self.offer_manager,
                "classify_offers_from_list",
                side_effect=fake_classify,
            ),
        ):
            fresh_buy, fresh_sell, _ = manager.sync_from_wallet()
            manager.expect_empty_wallet_offer_book("manual_cancel_all")
            empty_buy, empty_sell, _ = manager.sync_from_wallet()

        self.assertEqual(len(fresh_buy), 3)
        self.assertEqual(len(fresh_sell), 3)
        self.assertEqual(empty_buy, [])
        self.assertEqual(empty_sell, [])

        meta = manager.get_wallet_sync_meta()
        self.assertTrue(meta["fresh"])
        self.assertFalse(meta["using_cache"])
        self.assertEqual(meta["cache_size"], 0)
        self.assertEqual(meta.get("expected_empty_reason"), "manual_cancel_all")

    def test_worker_cancelled_ids_file_bypasses_empty_cache_guard(self):
        manager = self.offer_manager.OfferManager()
        cancelled_ids = {
            *(f"buy-live-{i}" for i in range(3)),
            *(f"sell-live-{i}" for i in range(3)),
        }

        with tempfile.TemporaryDirectory() as tmp:
            cancelled_path = os.path.join(tmp, "worker_cancelled_ids.json")
            with open(cancelled_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "cancelled_ids": sorted(cancelled_ids),
                        "timestamp": time.time(),
                    },
                    f,
                )

            fake_user_paths = types.ModuleType("user_paths")
            fake_user_paths.worker_cancelled_ids_file = lambda: cancelled_path

            def fake_get_all_offers(*args, **kwargs):
                if not hasattr(fake_get_all_offers, "_calls"):
                    fake_get_all_offers._calls = 0
                fake_get_all_offers._calls += 1
                if fake_get_all_offers._calls == 1:
                    return [{"trade_id": "seed"}]
                return []

            def fake_classify(all_offers, _asset_id):
                if all_offers:
                    return (
                        [{"trade_id": f"buy-live-{i}"} for i in range(3)],
                        [{"trade_id": f"sell-live-{i}"} for i in range(3)],
                        [],
                    )
                return ([], [], [])

            with (
                patch.dict(sys.modules, {"user_paths": fake_user_paths}),
                patch.object(
                    self.offer_manager, "get_all_offers", new=fake_get_all_offers
                ),
                patch.object(
                    self.offer_manager,
                    "classify_offers_from_list",
                    side_effect=fake_classify,
                ),
            ):
                fresh_buy, fresh_sell, _ = manager.sync_from_wallet()
                empty_buy, empty_sell, _ = manager.sync_from_wallet()

        self.assertEqual(len(fresh_buy), 3)
        self.assertEqual(len(fresh_sell), 3)
        self.assertEqual(empty_buy, [])
        self.assertEqual(empty_sell, [])

        meta = manager.get_wallet_sync_meta()
        self.assertTrue(meta["fresh"])
        self.assertFalse(meta["using_cache"])
        self.assertEqual(meta["cache_size"], 0)
        self.assertEqual(meta.get("expected_empty_reason"), "coin_prep_cancel_all")

    def test_mass_disappearance_is_blocked_while_wallet_sync_is_stale(self):
        class _FakeOfferManager:
            @staticmethod
            def get_wallet_sync_meta():
                return {"fresh": False, "using_cache": True}

            @staticmethod
            def is_bot_cancelled(_trade_id):
                return False

        tracker = self.fill_tracker.FillTracker(_FakeOfferManager())
        tracker._previous_ids["buy"] = {"buy-a", "buy-b"}
        tracker._previous_ids["sell"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["buy_fills"], [])
        self.assertEqual(result["sell_fills"], [])
        self.assertEqual(tracker._mass_disappearance_count, 0)
        self.assertEqual(tracker._previous_ids["buy"], {"buy-a", "buy-b"})
        self.assertTrue(
            any(evt == "mass_disappearance_blocked" for _, evt, _, _ in self.logged)
        )


if __name__ == "__main__":
    unittest.main()
