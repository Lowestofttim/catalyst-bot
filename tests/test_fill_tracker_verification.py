import importlib
import sys
import types
import unittest


class _FakeCfg:
    SPACESCAN_ENABLED = True
    WALLET_ADDRESS = "xch1ourwalletaddress"


class FillTrackerVerificationTests(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self.recorded = []
        self.status_updates = []
        self.db_offer = {"trade_id": "", "coin_id": "0xcoin123"}

        fake_config = types.ModuleType("config")
        fake_config.cfg = _FakeCfg()
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.record_fill = self._record_fill
        fake_database.get_unmatched_fills = lambda *args, **kwargs: []
        fake_database.match_round_trip = lambda *args, **kwargs: None
        fake_database.get_open_offers = lambda *args, **kwargs: []
        fake_database.get_offer = lambda trade_id: {
            **self.db_offer,
            "trade_id": trade_id,
        }
        fake_database.update_offer_status = self._update_offer_status
        fake_database.update_offer_lifecycle_state = lambda *args, **kwargs: None
        fake_database.transition_offer = lambda *args, **kwargs: None
        fake_database.mark_cancel_attempted = lambda *args, **kwargs: None
        fake_database.log_event = self._log_event
        sys.modules["database"] = fake_database

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_wallet_type = lambda: "sage"
        sys.modules["wallet"] = fake_wallet

        self.fake_wallet_sage = types.ModuleType("wallet_sage")
        self.fake_wallet_sage.rpc = lambda *args, **kwargs: None
        sys.modules["wallet_sage"] = self.fake_wallet_sage

        self.fake_dexie_manager = types.ModuleType("dexie_manager")
        self.fake_dexie_manager.get_offer_detail = lambda dexie_id: None
        sys.modules["dexie_manager"] = self.fake_dexie_manager

        self.fake_spacescan = types.ModuleType("spacescan")
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: None
        sys.modules["spacescan"] = self.fake_spacescan

        sys.modules.pop("fill_tracker", None)
        self.fill_tracker = importlib.import_module("fill_tracker")

    def tearDown(self):
        for name in ["fill_tracker", "spacescan", "wallet_sage", "wallet",
                     "database", "config", "dexie_manager"]:
            sys.modules.pop(name, None)

    def _log_event(self, severity, event_type, message, data=None):
        self.logged.append((severity, event_type, message, data))

    def _record_fill(self, *args, **kwargs):
        self.recorded.append((args, kwargs))
        return 123

    def _update_offer_status(self, trade_id, status):
        self.status_updates.append((trade_id, status))
        return True

    def test_unverified_spacescan_result_does_not_record_fill(self):
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-unverified"
        tracker._previous_ids["buy"] = {trade_id}
        tracker._previous_ids["sell"] = set()

        details_cache = {
            trade_id: {
                "price": 0,
                "size_xch": 0,
                "size_cat": 0,
                "tier": "extreme",
            }
        }

        result = tracker.detect_fills(set(), set(), details_cache)

        self.assertEqual(result["buy_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertIn((trade_id, "cancelled"), self.status_updates)
        self.assertTrue(any(evt == "fill_unverified" for _, evt, _, _ in self.logged))
        self.assertTrue(any(evt == "offer_closed_unverified" for _, evt, _, _ in self.logged))
        fill_msgs = [msg for _, evt, msg, _ in self.logged if evt == "fill_unverified"]
        self.assertTrue(any("inconclusive" in msg.lower() for msg in fill_msgs))
        self.assertFalse(any("spacescan unavailable" in msg.lower() for msg in fill_msgs))

    def test_verified_spacescan_result_records_fill(self):
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: True
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-verified"
        tracker._previous_ids["sell"] = {trade_id}
        tracker._previous_ids["buy"] = set()

        fill_detail = {
            "trade_id": trade_id,
            "side": "sell",
            "price": "0.1",
        }
        tracker._record_fill = lambda trade_id, side, details_cache: fill_detail

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["sell_fills"], [fill_detail])
        self.assertTrue(any(evt == "fill_verified" for _, evt, _, _ in self.logged))

    def test_spacescan_disabled_does_not_record_fill(self):
        sys.modules["config"].cfg.SPACESCAN_ENABLED = False
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: True
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-disabled"
        tracker._previous_ids["buy"] = {trade_id}
        tracker._previous_ids["sell"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["buy_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertTrue(any(evt == "spacescan_disabled" for _, evt, _, _ in self.logged))

    def test_wallet_cancelled_status_blocks_fill_recording(self):
        self.fake_wallet_sage.rpc = lambda *args, **kwargs: {"status": "CANCELLED"}
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: True
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-cancelled"
        tracker._previous_ids["sell"] = {trade_id}
        tracker._previous_ids["buy"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["sell_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertIn((trade_id, "cancelled"), self.status_updates)
        self.assertTrue(any(evt == "fill_wallet_closed_nonfill" for _, evt, _, _ in self.logged))
        self.assertTrue(any(evt == "offer_closed_nonfill" for _, evt, _, _ in self.logged))

    def test_dexie_still_open_blocks_fill_recording(self):
        self.db_offer = {"coin_id": "0xcoin123", "dexie_id": "dexie-open"}
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: True
        self.fake_dexie_manager.get_offer_detail = lambda dexie_id: {
            "status": 0,
            "trade_id": "0xtrade-open",
            "involved_coins": ["0xcoin123"],
        }
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-open"
        tracker._previous_ids["buy"] = {trade_id}
        tracker._previous_ids["sell"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["buy_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertTrue(any(evt == "fill_dexie_still_open" for _, evt, _, _ in self.logged))

    def test_dexie_trade_mismatch_blocks_fill_recording(self):
        self.db_offer = {"coin_id": "0xcoin123", "dexie_id": "dexie-mismatch"}
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: True
        self.fake_dexie_manager.get_offer_detail = lambda dexie_id: {
            "status": 3,
            "trade_id": "0xsomeoneelse",
            "involved_coins": ["0xcoin123"],
        }
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-mismatch"
        tracker._previous_ids["sell"] = {trade_id}
        tracker._previous_ids["buy"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["sell_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertTrue(any(evt == "fill_dexie_trade_mismatch" for _, evt, _, _ in self.logged))


if __name__ == "__main__":
    unittest.main()
