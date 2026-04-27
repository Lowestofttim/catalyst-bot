import importlib
import sys
import types
import unittest


class _FakeCfg:
    SPACESCAN_ENABLED = True
    WALLET_ADDRESS = "xch1ourwalletaddress"


class _FakeOfferManager:
    def __init__(self, cancelled):
        self.cancelled = set(cancelled)

    def is_bot_cancelled(self, trade_id):
        return trade_id in self.cancelled


_MODS_TO_RESTORE = ("fill_tracker", "spacescan", "wallet_sage", "wallet",
                    "database", "config", "dexie_manager")


class FillTrackerVerificationTests(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name) for name in _MODS_TO_RESTORE
        }
        self.logged = []
        self.recorded = []
        self.status_updates = []
        self.lifecycle_updates = []
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
        fake_database.update_offer_lifecycle_state = self._update_lifecycle_state
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
        for name, saved in self._saved_modules.items():
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

    def _log_event(self, severity, event_type, message, data=None):
        self.logged.append((severity, event_type, message, data))

    def _record_fill(self, *args, **kwargs):
        self.recorded.append((args, kwargs))
        return 123

    def _update_offer_status(self, trade_id, status):
        self.status_updates.append((trade_id, status))
        return True

    def _update_lifecycle_state(self, trade_id, lifecycle_state):
        self.lifecycle_updates.append((trade_id, lifecycle_state))
        return True

    def test_unverified_spacescan_result_parks_for_retry(self):
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

        # New behaviour: an inconclusive Spacescan result does NOT immediately
        # retire the offer as cancelled (that path silently erased real fills
        # during Spacescan outages). Instead the trade is parked in
        # _pending_reverify and retried on subsequent cycles; only after the
        # retry budget is exhausted does it get a terminal status — and even
        # then with an operator-visible error log.
        self.assertEqual(result["buy_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertNotIn((trade_id, "cancelled"), self.status_updates)
        self.assertIn(trade_id, tracker._pending_reverify)
        self.assertTrue(any(evt == "fill_verify_pending"
                            for _, evt, _, _ in self.logged))

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

    def test_wallet_pending_cancel_stays_open_for_reconcile(self):
        self.fake_wallet_sage.rpc = lambda *args, **kwargs: {"status": "PENDING_CANCEL"}
        spacescan_calls = []

        def _spacescan_should_not_run(*args, **kwargs):
            spacescan_calls.append((args, kwargs))
            self.fail("Pending-cancel offers are still fillable; leave them for cancel reconcile")

        self.fake_spacescan.verify_fill = _spacescan_should_not_run
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-pending-cancel"
        tracker._previous_ids["sell"] = {trade_id}
        tracker._previous_ids["buy"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["sell_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.status_updates, [])
        self.assertEqual(self.lifecycle_updates, [])
        self.assertEqual(spacescan_calls, [])
        self.assertTrue(any(evt == "fill_wallet_still_open" for _, evt, _, _ in self.logged))

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
        self.assertEqual(self.status_updates, [])
        self.assertEqual(self.lifecycle_updates, [])
        self.assertTrue(any(evt == "fill_dexie_still_open" for _, evt, _, _ in self.logged))

    def test_dexie_trade_mismatch_defers_to_spacescan(self):
        # New policy: Spacescan is the golden gate. A Dexie trade_id mismatch
        # (likely stale Dexie data) must not veto a Spacescan-confirmed fill;
        # it merely logs a defer message and lets Spacescan decide.
        self.db_offer = {"coin_id": "0xcoin123", "dexie_id": "dexie-mismatch"}
        self.fake_spacescan.verify_fill = lambda coin_id, our_address: True
        self.fake_dexie_manager.get_offer_detail = lambda dexie_id: {
            "status": 3,
            "trade_id": "0xsomeoneelse",
            "involved_coins": ["0xcoin123"],
        }
        tracker = self.fill_tracker.FillTracker()
        trade_id = "trade-mismatch"
        fill_detail = {"trade_id": trade_id, "side": "sell", "price": "0.1"}
        tracker._record_fill = lambda trade_id, side, details_cache: fill_detail
        tracker._previous_ids["sell"] = {trade_id}
        tracker._previous_ids["buy"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["sell_fills"], [fill_detail])
        self.assertTrue(any(evt == "fill_dexie_trade_mismatch_defer"
                            for _, evt, _, _ in self.logged))
        self.assertTrue(any(evt == "fill_verified"
                            for _, evt, _, _ in self.logged))

    def test_bot_cancelled_dexie_cancel_skips_spacescan(self):
        self.db_offer = {
            "coin_id": "0xcoin123",
            "dexie_id": "dexie-cancelled",
        }
        trade_id = "trade-dexie-cancelled"
        spacescan_calls = []

        def _spacescan_should_not_run(*args, **kwargs):
            spacescan_calls.append((args, kwargs))
            self.fail("Spacescan should not run after Dexie confirms cancel")

        self.fake_spacescan.verify_fill = _spacescan_should_not_run
        self.fake_dexie_manager.get_offer_detail = lambda *args, **kwargs: {
            "status": 3,
            "trade_id": trade_id,
            "involved_coins": ["0xcoin123"],
        }

        tracker = self.fill_tracker.FillTracker(
            offer_manager=_FakeOfferManager({trade_id})
        )
        tracker._previous_ids["buy"] = {trade_id}
        tracker._previous_ids["sell"] = set()

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["buy_fills"], [])
        self.assertEqual(self.recorded, [])
        self.assertEqual(spacescan_calls, [])
        self.assertIn((trade_id, "cancelled"), self.status_updates)
        self.assertTrue(any(evt == "offer_closed_nonfill"
                            for _, evt, _, _ in self.logged))

    def test_bot_cancelled_dexie_fill_records_without_spacescan(self):
        self.db_offer = {
            "coin_id": "0xcoin123",
            "dexie_id": "dexie-filled",
        }
        trade_id = "trade-dexie-filled"
        spacescan_calls = []

        def _spacescan_should_not_run(*args, **kwargs):
            spacescan_calls.append((args, kwargs))
            self.fail("Spacescan should not run after Dexie confirms fill")

        self.fake_spacescan.verify_fill = _spacescan_should_not_run
        self.fake_dexie_manager.get_offer_detail = lambda *args, **kwargs: {
            "status": 4,
            "trade_id": trade_id,
            "involved_coins": ["0xcoin123"],
        }
        fill_detail = {"trade_id": trade_id, "side": "sell", "price": "0.1"}

        tracker = self.fill_tracker.FillTracker(
            offer_manager=_FakeOfferManager({trade_id})
        )
        tracker._record_fill = lambda trade_id, side, details_cache: fill_detail
        tracker._previous_ids["buy"] = set()
        tracker._previous_ids["sell"] = {trade_id}

        result = tracker.detect_fills(set(), set(), {})

        self.assertEqual(result["sell_fills"], [fill_detail])
        self.assertEqual(spacescan_calls, [])
        self.assertTrue(any(evt == "fill_beat_cancel_dexie"
                            for _, evt, _, _ in self.logged))


if __name__ == "__main__":
    unittest.main()
