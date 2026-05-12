import itertools
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch, call


class _FakeCfg:
    OFFER_EXPIRY_SECS = 86400
    OFFER_STAGGER_SECS = 10
    DRY_RUN = False
    MAX_ACTIVE_BUY_OFFERS = 25
    MAX_ACTIVE_SELL_OFFERS = 25
    DEFAULT_TRADE_XCH = Decimal("0.01")
    CAT_ASSET_ID = "test-cat"
    CAT_DECIMALS = 3
    CAT_WALLET_ID = 2
    WALLET_ID_XCH = 1
    TIER_ENABLED = False
    LADDER_CREATE_PARALLELISM = 5
    LADDER_CREATE_DELAY_MS = 0
    MIN_EDGE_BPS = Decimal("300")
    SNIPER_EXPIRY_SECS = 1800
    SNIPER_SIZE_XCH = Decimal("0.001")
    SNIPER_PREP_COUNT = 20
    COIN_IDS_ENABLED = True
    DEXIE_AUTO_POST = False
    ENABLE_BUY = True
    ENABLE_SELL = True
    MIN_TRADE_XCH = Decimal("0.01")
    MAX_TRADE_XCH = Decimal("5")
    SNIPER_COOLDOWN_SECS = 0
    INVENTORY_ENABLED = False
    COIN_PREP_HEADROOM_PCT = Decimal("0")
    BUY_LADDER_REVERSED = False

    @staticmethod
    def get_spread_fraction():
        return Decimal("0.08")


_ORIG_MODULES = {
    name: sys.modules.get(name)
    for name in ("config", "database", "wallet", "coin_manager", "tx_fees",
                 "win_subprocess", "offer_manager")
}

fake_config = types.ModuleType("config")
fake_config.cfg = _FakeCfg()
sys.modules["config"] = fake_config

fake_database = types.ModuleType("database")
fake_database.add_offer = lambda *args, **kwargs: None
fake_database.update_offer_status = lambda *args, **kwargs: None
fake_database.update_offer_coin_id = lambda *args, **kwargs: None
fake_database.update_offer_bech32 = lambda *args, **kwargs: None
fake_database.get_open_offers = lambda *args, **kwargs: []
fake_database.get_offer = lambda *args, **kwargs: None
fake_database.get_free_coins = lambda *args, **kwargs: []
fake_database.get_reserve_coins = lambda *args, **kwargs: []
fake_database.log_event = lambda *args, **kwargs: None
fake_database.lock_coin = lambda *args, **kwargs: None
fake_database.update_offer_lifecycle_state = lambda *args, **kwargs: None
fake_database.transition_offer = lambda *args, **kwargs: None
fake_database.mark_cancel_attempted = lambda *args, **kwargs: None
sys.modules["database"] = fake_database

fake_wallet = types.ModuleType("wallet")
fake_wallet.create_offer = lambda *args, **kwargs: None
fake_wallet.cancel_offer = lambda *args, **kwargs: {}
fake_wallet.cancel_offers_batch = lambda *args, **kwargs: {}
fake_wallet.get_all_offers = lambda *args, **kwargs: []
fake_wallet.classify_offers_from_list = lambda *args, **kwargs: ([], [], [])
fake_wallet.is_offer_time_expired = lambda *args, **kwargs: False
fake_wallet.get_offer_expiry_info = lambda *args, **kwargs: {}
fake_wallet.get_offer_bech32 = lambda *args, **kwargs: ""
fake_wallet.cleanup_expired_offers = lambda *args, **kwargs: 0
fake_wallet.get_spendable_coins_rpc = lambda *args, **kwargs: {"success": True, "confirmed_records": []}
fake_wallet.get_exact_spendable_coins_rpc = fake_wallet.get_spendable_coins_rpc
fake_wallet.get_owned_coins_detailed = lambda *args, **kwargs: None
fake_wallet.get_wallet_type = lambda: "sage"
fake_wallet.WALLET_ID_XCH = 1
sys.modules["wallet"] = fake_wallet

fake_coin_manager = types.ModuleType("coin_manager")
fake_coin_manager._coin_id_from_record = lambda rec: rec.get("coin_id")
sys.modules["coin_manager"] = fake_coin_manager

fake_tx_fees = types.ModuleType("tx_fees")
fake_tx_fees.fee_pool_enabled = lambda: False
fake_tx_fees.get_effective_transaction_fee_mojos = lambda: 0
fake_tx_fees.get_fee_coin_size_mojos = lambda: 0
fake_tx_fees.get_fee_coin_size_xch = lambda: _FakeCfg.__dict__.get("DEFAULT_TRADE_XCH", 0)
fake_tx_fees.get_fee_pool_count = lambda: 0
fake_tx_fees.get_fee_tier_name = lambda: "fees"
sys.modules["tx_fees"] = fake_tx_fees

fake_win_subprocess = types.ModuleType("win_subprocess")
fake_win_subprocess.hidden_subprocess_kwargs = lambda: {}
sys.modules["win_subprocess"] = fake_win_subprocess

# Pop offer_manager so it re-imports with our fakes rather than the cached
# version loaded by test_api_local_guard (which uses real config/wallet).
sys.modules.pop("offer_manager", None)
import offer_manager

# Restore originals after import so we don't pollute subsequent test files.
for _name, _mod in _ORIG_MODULES.items():
    if _mod is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _mod


class OfferManagerCoinIdTests(unittest.TestCase):
    def test_get_ladder_parallelism_uses_default_workers(self):
        manager = offer_manager.OfferManager()

        with patch.object(offer_manager, "get_wallet_type", return_value="sage"):
            self.assertEqual(manager._get_ladder_parallelism(True), 5)
            self.assertEqual(manager._get_ladder_parallelism(False), 1)

        with patch.object(offer_manager, "get_wallet_type", return_value="chia"):
            self.assertEqual(manager._get_ladder_parallelism(True), 5)

    def test_get_ladder_parallelism_honors_config_override(self):
        manager = offer_manager.OfferManager()

        with patch.object(offer_manager.cfg, "LADDER_CREATE_PARALLELISM", 1):
            self.assertEqual(manager._get_ladder_parallelism(True), 1)
            self.assertEqual(manager._get_ladder_parallelism(False), 1)

    def test_cancel_offers_logs_only_confirmed_cancels(self):
        manager = offer_manager.OfferManager()
        events = []

        with patch.object(offer_manager, "cancel_offers_batch", return_value={
            "trade-ok": {"success": True},
            "trade-fail": {"success": False},
        }), patch.object(offer_manager, "update_offer_status") as mock_update, \
                patch.object(offer_manager, "log_event",
                             side_effect=lambda level, event_type, message: events.append(
                                 (level, event_type, message))):
            result = manager.cancel_offers(["trade-ok", "trade-fail"], reason="test")

        self.assertTrue(result["trade-ok"]["success"])
        mock_update.assert_called_once_with("trade-ok", "cancelled")
        event_types = [event_type for _, event_type, _ in events]
        self.assertIn("cancel_result", event_types)
        self.assertIn("offers_cancelled", event_types)
        self.assertIn("offers_cancel_pending", event_types)
        cancelled_msgs = [msg for _, event_type, msg in events if event_type == "offers_cancelled"]
        self.assertEqual(cancelled_msgs, ["Cancelled 1 offers (reason: test)"])

    def test_requote_side_cancels_most_at_risk_first(self):
        """Single-pass requote cancels the most-at-risk old offers first."""
        manager = offer_manager.OfferManager()
        cancel_batches = []
        open_offers = [
            {
                "trade_id": "extreme-trade",
                "tier": "extreme",
                "price_xch": "0.1110",
                "created_at": "2026-03-29T00:00:04+00:00",
            },
            {
                "trade_id": "outer-trade",
                "tier": "outer",
                "price_xch": "0.1140",
                "created_at": "2026-03-29T00:00:03+00:00",
            },
            {
                "trade_id": "mid-trade",
                "tier": "mid",
                "price_xch": "0.1170",
                "created_at": "2026-03-29T00:00:02+00:00",
            },
            {
                "trade_id": "inner-trade",
                "tier": "inner",
                "price_xch": "0.1200",
                "created_at": "2026-03-29T00:00:01+00:00",
            },
        ]

        def fake_create_ladder(mid_price, side, num_offers=None, **kwargs):
            count = int(num_offers or 0)
            return [{"trade_id": f"new-{i}"} for i in range(count)]

        def fake_cancel_offers(trade_ids, reason="requote", skip_confirmation=False,
                               force_storm=False):
            del reason, skip_confirmation, force_storm
            cancel_batches.append(list(trade_ids))
            return {tid: {"success": True} for tid in trade_ids}

        # Two spare trading coins (DB-first path in requote_side).
        # Must have designation in {"tier_spare","tier_active"} and assigned_tier
        # NOT in {"none","sniper","reserve","fee"} to be counted as spares.
        _two_spare_coins = [
            {"designation": "tier_spare", "assigned_tier": "inner"},
            {"designation": "tier_spare", "assigned_tier": "mid"},
        ]

        with patch.object(offer_manager, "get_open_offers", return_value=open_offers), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"coin_records": [{}, {}]}), \
                patch("database.get_free_coins", return_value=_two_spare_coins), \
                patch.object(manager, "create_ladder", side_effect=fake_create_ladder), \
                patch.object(manager, "cancel_offers", side_effect=fake_cancel_offers), \
                patch.object(offer_manager, "log_event"):
            result = manager.requote_side("buy", Decimal("0.1200"))

        # Single pass: 2 spares → create 2, cancel 2 most-at-risk
        self.assertEqual(result["replaced_count"], 2)
        self.assertEqual(result["target_count"], 4)
        self.assertFalse(result["fully_replaced"])
        self.assertEqual(len(result["offers"]), 2)
        # Only one cancel batch (single pass, no rolling waves)
        self.assertEqual(len(cancel_batches), 1)
        # Most-at-risk first: inner (closest to mid), then mid
        self.assertEqual(cancel_batches[0], ["inner-trade", "mid-trade"])

    def test_requote_side_large_partial_retires_stale_survivor(self):
        """Larger partial requotes blend stale/far survivors into cancel targets."""
        manager = offer_manager.OfferManager()
        cancel_batches = []
        force_flags = []
        open_offers = []
        for idx, price in enumerate([
            "0.1199", "0.1190", "0.1180", "0.1170",
            "0.1160", "0.1150", "0.1140", "0.1130",
        ], start=1):
            open_offers.append({
                "trade_id": f"risk-{idx}",
                "tier": "inner" if idx <= 2 else "mid",
                "price_xch": price,
                "created_at": f"2026-05-01T00:00:{idx:02d}+00:00",
            })
        open_offers.extend([
            {
                "trade_id": "stale-old-far",
                "tier": "extreme",
                "price_xch": "0.1000",
                "created_at": "2026-04-30T22:01:23+00:00",
            },
            {
                "trade_id": "stale-new-far",
                "tier": "extreme",
                "price_xch": "0.0990",
                "created_at": "2026-05-01T00:05:00+00:00",
            },
        ])

        def fake_create_ladder(mid_price, side, num_offers=None, **kwargs):
            count = int(num_offers or 0)
            return [{"trade_id": f"new-{i}"} for i in range(count)]

        def fake_cancel_offers(trade_ids, reason="requote", skip_confirmation=False,
                               force_storm=False):
            del reason, skip_confirmation
            cancel_batches.append(list(trade_ids))
            force_flags.append(force_storm)
            return {tid: {"success": True} for tid in trade_ids}

        _eight_spare_coins = [
            {"designation": "tier_spare", "assigned_tier": "inner"}
            for _ in range(8)
        ]

        with patch.object(offer_manager, "get_open_offers", return_value=open_offers), \
                patch("database.get_free_coins", return_value=_eight_spare_coins), \
                patch.object(manager, "create_ladder", side_effect=fake_create_ladder), \
                patch.object(manager, "cancel_offers", side_effect=fake_cancel_offers), \
                patch.object(offer_manager, "log_event"):
            result = manager.requote_side("buy", Decimal("0.1200"))

        self.assertEqual(result["replaced_count"], 8)
        self.assertEqual(result["target_count"], 10)
        self.assertFalse(result["fully_replaced"])
        self.assertEqual(len(cancel_batches), 1)
        self.assertEqual(cancel_batches[0][:6], [
            "risk-1", "risk-2", "risk-3", "risk-4", "risk-5", "risk-6",
        ])
        self.assertIn("stale-old-far", cancel_batches[0][6:])
        self.assertNotIn("risk-7", cancel_batches[0])
        self.assertEqual(force_flags, [False])

    def test_requote_side_can_force_intentional_cancel_storm(self):
        manager = offer_manager.OfferManager()
        force_flags = []
        open_offers = [
            {
                "trade_id": f"old-{idx}",
                "tier": "inner",
                "price_xch": "0.1200",
                "created_at": f"2026-05-01T00:00:{idx:02d}+00:00",
            }
            for idx in range(1, 7)
        ]

        def fake_create_ladder(mid_price, side, num_offers=None, **kwargs):
            del mid_price, side, kwargs
            count = int(num_offers or 0)
            return [{"trade_id": f"new-{i}"} for i in range(count)]

        def fake_cancel_offers(trade_ids, reason="requote", skip_confirmation=False,
                               force_storm=False):
            del reason, skip_confirmation
            force_flags.append(force_storm)
            return {tid: {"success": True} for tid in trade_ids}

        _six_spare_coins = [
            {"designation": "tier_spare", "assigned_tier": "inner"}
            for _ in range(6)
        ]

        with patch.object(offer_manager, "get_open_offers", return_value=open_offers), \
                patch("database.get_free_coins", return_value=_six_spare_coins), \
                patch.object(manager, "create_ladder", side_effect=fake_create_ladder), \
                patch.object(manager, "cancel_offers", side_effect=fake_cancel_offers), \
                patch.object(offer_manager, "log_event"):
            result = manager.requote_side(
                "buy",
                Decimal("0.1200"),
                force_cancel_storm=True,
            )

        self.assertEqual(result["replaced_count"], 6)
        self.assertEqual(force_flags, [True])

    def test_requote_side_does_not_create_when_cancel_is_pending(self):
        manager = offer_manager.OfferManager()
        open_offers = [{
            "trade_id": "old-sell",
            "tier": "inner",
            "price_xch": "0.1200",
            "created_at": "2026-03-29T00:00:01+00:00",
        }]
        calls = []

        def fake_create_ladder(*args, **kwargs):
            calls.append("create")
            return [{"trade_id": "new-sell"}]

        def fake_cancel_offers(trade_ids, reason="requote", skip_confirmation=False,
                               force_storm=False):
            del trade_ids, reason, skip_confirmation, force_storm
            calls.append("cancel")
            return {
                "old-sell": {
                    "success": True,
                    "method": "submitted_pending_confirm",
                }
            }

        _one_spare_coin = [{"designation": "tier_spare", "assigned_tier": "inner"}]

        with patch.object(offer_manager, "get_open_offers", return_value=open_offers), \
                patch("database.get_free_coins", return_value=_one_spare_coin), \
                patch.object(manager, "create_ladder", side_effect=fake_create_ladder), \
                patch.object(manager, "cancel_offers", side_effect=fake_cancel_offers), \
                patch.object(offer_manager, "log_event"):
            result = manager.requote_side("sell", Decimal("0.1200"))

        self.assertEqual(calls, ["cancel"])
        self.assertEqual(result["offers"], [])
        self.assertEqual(result["replaced_count"], 0)
        self.assertEqual(result["pending_cancel_count"], 1)
        self.assertEqual(result["failed_cancel_count"], 0)
        self.assertFalse(result["fully_replaced"])

    def test_retry_failed_cancels_exhaustion_does_not_mark_cancelled(self):
        manager = offer_manager.OfferManager()
        manager._pending_cancel_retries = {
            "trade-stuck": {
                "attempts": manager._max_cancel_retries,
                "first_failed": 0,
            }
        }
        manager._bot_cancelled_ids.add("trade-stuck")

        with patch.object(offer_manager, "update_offer_status") as mock_update:
            retried = manager.retry_failed_cancels()

        self.assertEqual(retried, 0)
        mock_update.assert_not_called()
        self.assertNotIn("trade-stuck", manager._bot_cancelled_ids)
        self.assertNotIn("trade-stuck", manager._pending_cancel_retries)

    def test_create_offer_with_retry_uses_selected_coin_id_directly(self):
        manager = offer_manager.OfferManager()
        selected_coin_id = "0xabc123"
        seen = {}

        def fake_create_offer(offer_dict, validate_only=False, max_time=None,
                              min_coin_amount=None, max_coin_amount=None,
                              coin_ids=None):
            seen["coin_ids"] = coin_ids
            return {
                "success": True,
                "offer": "offer1selectedcoin",
                "trade_id": "trade-selected",
                "trade_record": {"trade_id": "trade-selected"},
            }

        with patch.object(offer_manager, "create_offer", side_effect=fake_create_offer), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=AssertionError("should not re-select coin")):
            result = manager.create_offer_with_retry(
                {"1": -1000, "2": 2000},
                max_retries=0,
                coin_ids_enabled=True,
                selected_coin_id=selected_coin_id,
            )

        self.assertTrue(result["success"])
        self.assertEqual(seen["coin_ids"], [selected_coin_id])
        self.assertEqual(result["locked_coin_id"], selected_coin_id)

    def test_create_ladder_passes_preselected_coin_ids_to_workers(self):
        manager = offer_manager.OfferManager()
        preselected = ["0xcoin1", "0xcoin2", "0xcoin3"]
        seen = []
        counter = itertools.count(1)

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            idx = next(counter)
            seen.append(selected_coin_id)
            return {
                "success": True,
                "trade_id": f"trade-{idx}",
                "trade_record": {"trade_id": f"trade-{idx}"},
                "locked_coin_id": selected_coin_id,
                "offer": f"offer1{idx}",
            }

        with patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                          side_effect=preselected), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=3,
                coin_ids_enabled=True,
            )

        self.assertCountEqual(seen, preselected)
        self.assertEqual([offer["coin_id"] for offer in created], preselected)

    def test_create_ladder_caches_offer_bech32_for_splash_repost(self):
        manager = offer_manager.OfferManager()

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            return {
                "success": True,
                "trade_id": "trade-cache",
                "trade_record": {"trade_id": "trade-cache"},
                "locked_coin_id": selected_coin_id,
                "offer": "offer1cachedbech32",
            }

        with patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                          return_value="0xcoin-cache"), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "update_offer_bech32", create=True) as update_bech32, \
                patch.object(offer_manager, "log_event"), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                coin_ids_enabled=True,
            )

        self.assertEqual(created[0]["offer_bech32"], "offer1cachedbech32")
        update_bech32.assert_called_once_with("trade-cache", "offer1cachedbech32")

    def test_create_ladder_tier_mode_skips_when_no_preselected_coin(self):
        manager = offer_manager.OfferManager()

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                return Decimal("1.0")

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "COIN_PREP_HEADROOM_PCT", Decimal("5")), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             return_value=None), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             side_effect=AssertionError("Sage fallback should not run")), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": []}), \
                patch.object(manager, "record_slot_coin_failure") as record_failure:
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(created, [])
        record_failure.assert_called_once()

    def test_select_coin_for_offer_avoids_reserve_and_prefers_matching_tier(self):
        manager = offer_manager.OfferManager()
        records = [
            {"coin_id": "0xreserve", "coin": {"amount": 6000}},
            {"coin_id": "0xouter", "coin": {"amount": 2200}},
            {"coin_id": "0xinner", "coin": {"amount": 2100}},
        ]
        db_free = [
            {"coin_id": "0xreserve", "designation": "reserve", "assigned_tier": "none", "amount_mojos": 6000},
            {"coin_id": "0xouter", "designation": "tier_spare", "assigned_tier": "outer", "amount_mojos": 2200},
            {"coin_id": "0xinner", "designation": "tier_spare", "assigned_tier": "inner", "amount_mojos": 2100},
        ]

        with patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                          return_value={"success": True, "confirmed_records": records}), \
                patch("database.get_free_coins", return_value=db_free), \
                patch("database.get_reserve_coins", return_value=[db_free[0]]):
            coin_id = manager._select_coin_for_offer(
                wallet_id=1,
                amount_mojos=2000,
                preferred_tier="inner",
            )

        self.assertEqual(coin_id, "0xinner")

    def test_select_coin_for_offer_strict_tier_does_not_fallback(self):
        manager = offer_manager.OfferManager()
        records = [
            {"coin_id": "0xmid", "coin": {"amount": 2100}},
            {"coin_id": "0xouter", "coin": {"amount": 2200}},
        ]
        db_free = [
            {"coin_id": "0xmid", "designation": "tier_spare", "assigned_tier": "mid", "amount_mojos": 2100},
            {"coin_id": "0xouter", "designation": "tier_spare", "assigned_tier": "outer", "amount_mojos": 2200},
        ]

        with patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                          return_value={"success": True, "confirmed_records": records}), \
                patch("database.get_free_coins", return_value=db_free), \
                patch("database.get_reserve_coins", return_value=[]):
            coin_id = manager._select_coin_for_offer(
                wallet_id=1,
                amount_mojos=2000,
                preferred_tier="sniper",
                strict_preferred_tier=True,
            )

        self.assertIsNone(coin_id)

    def test_select_coin_for_offer_allows_moderate_same_tier_oversize(self):
        manager = offer_manager.OfferManager()
        records = [
            {"coin_id": "0xextreme", "coin": {"amount": 1550}},
        ]
        db_free = [
            {
                "coin_id": "0xextreme",
                "designation": "tier_spare",
                "assigned_tier": "extreme",
                "amount_mojos": 1550,
            },
        ]

        with patch.object(offer_manager.cfg, "COIN_OVERSIZE_FALLBACK_RATIO", Decimal("2.0"), create=True), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": records}), \
                patch("database.get_free_coins", return_value=db_free), \
                patch("database.get_reserve_coins", return_value=[]), \
                patch.object(offer_manager, "log_event"):
            coin_id = manager._select_coin_for_offer(
                wallet_id=2,
                amount_mojos=1000,
                preferred_tier="extreme",
                strict_preferred_tier=True,
                max_amount_mojos=1500,
            )

        self.assertEqual(coin_id, "0xextreme")

    def test_select_coin_for_offer_rejects_wild_same_tier_oversize(self):
        manager = offer_manager.OfferManager()
        records = [
            {"coin_id": "0xextreme", "coin": {"amount": 5000}},
        ]
        db_free = [
            {
                "coin_id": "0xextreme",
                "designation": "tier_spare",
                "assigned_tier": "extreme",
                "amount_mojos": 5000,
            },
        ]

        with patch.object(offer_manager.cfg, "COIN_OVERSIZE_FALLBACK_RATIO", Decimal("2.0"), create=True), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": records}), \
                patch("database.get_free_coins", return_value=db_free), \
                patch("database.get_reserve_coins", return_value=[]):
            coin_id = manager._select_coin_for_offer(
                wallet_id=2,
                amount_mojos=1000,
                preferred_tier="extreme",
                strict_preferred_tier=True,
                max_amount_mojos=1500,
            )

        self.assertIsNone(coin_id)

    def test_create_ladder_tier_mode_keeps_exact_prepped_buy_size(self):
        manager = offer_manager.OfferManager()
        captured = []
        rpc_records = [
            {"coin_id": "0xinnercoin", "coin": {"amount": offer_manager.xch_to_mojos(Decimal("2.2"))}},
        ]

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                sizes = {
                    "inner": Decimal("2.2"),
                    "mid": Decimal("1.1"),
                    "outer": Decimal("0.55"),
                    "extreme": Decimal("0.22"),
                }
                return sizes[tier]

        def fake_select(wallet_id, amount_mojos, used_coins=None,
                         preferred_tier=None, strict_preferred_tier=False,
                         spendable_records=None, max_amount_mojos=None, **kwargs):
            captured.append({
                "wallet_id": wallet_id,
                "amount_mojos": amount_mojos,
                "preferred_tier": preferred_tier,
            })
            return "0xinnercoin"

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            return {
                "success": True,
                "trade_id": "trade-inner",
                "trade_record": {"trade_id": "trade-inner"},
                "locked_coin_id": selected_coin_id,
                "offer": "offer-inner",
            }

        # Explicitly disable BUY_LADDER_REVERSED so we test the non-reversed
        # path deterministically. The coin_size_tier_for_slot_position function
        # reads cfg from coin_manager, so we must patch it there too.
        import coin_manager as _cm_mod
        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "BUY_LADDER_REVERSED", False), \
                patch.object(_cm_mod.cfg, "BUY_LADDER_REVERSED", False), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=fake_select), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": rpc_records}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(len(captured), 1)
        # BUY_LADDER_REVERSED=False: position inner stays as coin tier inner
        self.assertEqual(captured[0]["preferred_tier"], "inner")
        self.assertEqual(captured[0]["amount_mojos"], offer_manager.xch_to_mojos(Decimal("2.2")))
        self.assertEqual(created[0]["size_xch"], Decimal("2.2"))

    def test_create_ladder_tier_mode_ignores_live_size_collision_for_exact_buy_spend(self):
        manager = offer_manager.OfferManager()

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                return {
                    "inner": Decimal("2.2"),
                    "mid": Decimal("1.1"),
                    "outer": Decimal("0.55"),
                    "extreme": Decimal("0.22"),
                }[tier]

        counter = itertools.count(1)

        def fake_select(wallet_id, amount_mojos, used_coins=None,
                        preferred_tier=None, strict_preferred_tier=False,
                        spendable_records=None, max_amount_mojos=None, **kwargs):
            return f"0xcoin{next(counter)}"

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            return {
                "success": True,
                "trade_id": f"trade-{selected_coin_id}",
                "trade_record": {"trade_id": f"trade-{selected_coin_id}"},
                "locked_coin_id": selected_coin_id,
                "offer": f"offer-{selected_coin_id}",
            }

        existing = [{
            "trade_id": "existing-inner",
            "side": "buy",
            "tier": "inner",
            "size_xch": "2.19999999",
        }]

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager, "get_open_offers", return_value=existing), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=fake_select), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": [
                                 {"coin_id": "0xcoin1", "coin": {"amount": offer_manager.xch_to_mojos(Decimal("2.2"))}}
                             ]}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["size_xch"], Decimal("2.2"))

    def test_create_ladder_tier_mode_matches_exact_sell_coin_amount(self):
        manager = offer_manager.OfferManager()

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                return {
                    "inner": Decimal("2.0"),
                    "mid": Decimal("1.0"),
                    "outer": Decimal("0.5"),
                    "extreme": Decimal("0.2"),
                }[tier]

        selected_cat_mojos = offer_manager.cat_to_mojos(Decimal("19897"), 3)

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            return {
                "success": True,
                "trade_id": "trade-sell-inner",
                "trade_record": {"trade_id": "trade-sell-inner"},
                "locked_coin_id": selected_coin_id,
                "offer": "offer-sell-inner",
            }

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             return_value="0xsellcoin"), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": [
                                 {"coin_id": "0xsellcoin", "coin": {"amount": selected_cat_mojos}}
                             ]}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.000120622202"),
                side="sell",
                num_offers=1,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["size_cat"], Decimal("19897"))
        self.assertEqual(
            created[0]["size_xch"],
            offer_manager.mojos_to_xch(
                offer_manager.xch_to_mojos(Decimal("19897") * created[0]["price"])
            ),
        )

    def test_create_ladder_tier_mode_nudges_buy_requested_amount_when_live_collision_exists(self):
        manager = offer_manager.OfferManager()
        selected_xch_mojos = offer_manager.xch_to_mojos(Decimal("2.2"))

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                return {
                    "inner": Decimal("2.2"),
                    "mid": Decimal("1.1"),
                    "outer": Decimal("0.55"),
                    "extreme": Decimal("0.22"),
                }[tier]

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            return {
                "success": True,
                "trade_id": "trade-buy-collision",
                "trade_record": {"trade_id": "trade-buy-collision"},
                "locked_coin_id": selected_coin_id,
                "offer": "offer-buy-collision",
            }

        half_spread = offer_manager.cfg.get_spread_fraction() / Decimal("2")
        price = manager._get_ladder_price(0, "buy", Decimal("0.001"), half_spread, 50)
        base_requested_cat_mojos = offer_manager.cat_to_mojos(Decimal("2.2") / price, 3)
        existing = [{
            "trade_id": "existing-buy",
            "side": "buy",
            "tier": "inner",
            "size_cat": str(offer_manager.mojos_to_cat(base_requested_cat_mojos, 3)),
        }]

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager, "get_open_offers", return_value=existing), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             return_value="0xbuycoin"), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": [
                                 {"coin_id": "0xbuycoin", "coin": {"amount": selected_xch_mojos}}
                             ]}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["size_xch"], Decimal("2.2"))
        self.assertEqual(
            created[0]["size_cat"],
            offer_manager.mojos_to_cat(base_requested_cat_mojos + 1, 3),
        )

    def test_create_ladder_tier_mode_nudges_sell_requested_xch_within_same_tier_batch(self):
        manager = offer_manager.OfferManager()
        selected_cat_mojos = offer_manager.cat_to_mojos(Decimal("18239"), 3)
        counter = itertools.count(1)

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                return {
                    "inner": Decimal("2.0"),
                    "mid": Decimal("1.0"),
                    "outer": Decimal("0.5"),
                    "extreme": Decimal("0.2"),
                }[tier]

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            idx = next(counter)
            return {
                "success": True,
                "trade_id": f"trade-sell-{idx}",
                "trade_record": {"trade_id": f"trade-sell-{idx}"},
                "locked_coin_id": selected_coin_id,
                "offer": f"offer-sell-{idx}",
            }

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10")), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=["0xsellcoin1", "0xsellcoin2"]), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": [
                                 {"coin_id": "0xsellcoin1", "coin": {"amount": selected_cat_mojos}},
                                 {"coin_id": "0xsellcoin2", "coin": {"amount": selected_cat_mojos}},
                             ]}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.000120622202"),
                side="sell",
                num_offers=2,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(len(created), 2)
        self.assertEqual(created[0]["size_xch"], Decimal("2.0"))
        self.assertEqual(
            created[1]["size_xch"],
            offer_manager.mojos_to_xch(offer_manager.xch_to_mojos(Decimal("2.0")) + 2),
        )

    def test_create_ladder_tier_mode_with_headroom_keeps_live_buy_size(self):
        manager = offer_manager.OfferManager()
        captured = []
        selected_amount = offer_manager.xch_to_mojos(Decimal("2.2"))

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                return {
                    "inner": Decimal("2.0"),
                    "mid": Decimal("1.0"),
                    "outer": Decimal("0.5"),
                    "extreme": Decimal("0.2"),
                }[tier]

        def fake_select(wallet_id, amount_mojos, used_coins=None,
                         preferred_tier=None, strict_preferred_tier=False,
                         spendable_records=None, max_amount_mojos=None, **kwargs):
            captured.append({
                "wallet_id": wallet_id,
                "amount_mojos": amount_mojos,
                "preferred_tier": preferred_tier,
            })
            return "0xheadroomcoin"

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            return {
                "success": True,
                "trade_id": "trade-headroom-buy",
                "trade_record": {"trade_id": "trade-headroom-buy"},
                "locked_coin_id": selected_coin_id,
                "offer": "offer-headroom-buy",
            }

        import coin_manager as _cm_mod2
        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10")), \
                patch.object(offer_manager.cfg, "BUY_LADDER_REVERSED", False), \
                patch.object(_cm_mod2.cfg, "BUY_LADDER_REVERSED", False), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=fake_select), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": [
                                 {"coin_id": "0xheadroomcoin", "coin": {"amount": selected_amount}}
                             ]}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                total_slots=50,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["preferred_tier"], "inner")
        self.assertEqual(captured[0]["amount_mojos"], offer_manager.xch_to_mojos(Decimal("2.0")))
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["size_xch"], Decimal("2.0"))

    def test_create_ladder_prefetches_spendable_snapshot_once_per_side(self):
        manager = offer_manager.OfferManager()
        rpc_records = [
            {"coin_id": "0xcoin1", "coin": {"amount": 20000000000}},
            {"coin_id": "0xcoin2", "coin": {"amount": 21000000000}},
            {"coin_id": "0xcoin3", "coin": {"amount": 22000000000}},
        ]
        db_free = [
            {"coin_id": "0xcoin1", "designation": "tier_spare", "assigned_tier": "mid", "amount_mojos": 20000000000},
            {"coin_id": "0xcoin2", "designation": "tier_spare", "assigned_tier": "mid", "amount_mojos": 21000000000},
            {"coin_id": "0xcoin3", "designation": "tier_spare", "assigned_tier": "mid", "amount_mojos": 22000000000},
        ]
        counter = itertools.count(1)

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            idx = next(counter)
            return {
                "success": True,
                "trade_id": f"trade-{idx}",
                "trade_record": {"trade_id": f"trade-{idx}"},
                "locked_coin_id": selected_coin_id,
                "offer": f"offer-{idx}",
            }

        with patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                          return_value={"success": True, "confirmed_records": rpc_records}) as mock_rpc, \
                patch("database.get_free_coins", return_value=db_free), \
                patch("database.get_reserve_coins", return_value=[]), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=3,
                coin_ids_enabled=True,
            )

        self.assertEqual(mock_rpc.call_count, 1)
        self.assertEqual(len(created), 3)
        self.assertEqual(sorted(o["coin_id"] for o in created), ["0xcoin1", "0xcoin2", "0xcoin3"])

    def test_create_ladder_accepts_offer_and_records_multi_input_overlap(self):
        manager = offer_manager.OfferManager()

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None):
            return {
                "success": True,
                "trade_id": "trade-overlap",
                "trade_record": {"trade_id": "trade-overlap"},
                "locked_coin_id": selected_coin_id,
                "offer": "offer-overlap",
            }

        overlap_map = {
            "0xcoin-mid": {"amount": 1000000000000, "offer_id": "trade-overlap"},
            "0xcoin-extra": {"amount": 200000000000, "offer_id": "trade-overlap"},
        }

        with patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                          return_value="0xcoin-mid"), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_owned_coins_detailed",
                             return_value=overlap_map), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": [
                                 {"coin_id": "0xcoin-mid", "coin": {"amount": 1000000000000}}
                             ]}), \
                patch.object(offer_manager, "add_offer") as mock_add_offer, \
                patch.object(offer_manager, "lock_coin") as mock_lock_coin, \
                patch.object(offer_manager, "log_event") as mock_log_event, \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"):
            created = manager.create_ladder(
                mid_price=Decimal("0.001"),
                side="buy",
                num_offers=1,
                coin_ids_enabled=True,
            )

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["coin_id"], "0xcoin-mid")
        self.assertEqual(created[0]["locked_coin_ids"], ["0xcoin-extra", "0xcoin-mid"])
        mock_add_offer.assert_called_once()
        self.assertEqual(
            mock_lock_coin.call_args_list,
            [call("0xcoin-extra", "trade-overlap"), call("0xcoin-mid", "trade-overlap")],
        )
        self.assertTrue(any(args[1] == "coin_ids_overlap_observed" for args, _ in mock_log_event.call_args_list))

    def test_get_replenishment_slots_heals_tier_shortage_instead_of_mini_ladder(self):
        # Scenario: only the extreme tier has a shortage (3 slots short).
        # With the current tier config (INNER=10, MID=7, OUTER=5, EXTREME=28
        # for a 50-slot ladder), extreme spans slots 22–49.  The function
        # fills from the INNERMOST end of the shortage, so the 3 missing
        # extreme slots should be positions [22, 23, 24] — NOT a mini-ladder
        # that would blindly add offers at positions 47–49.
        manager = offer_manager.OfferManager()
        existing = (
            [{"tier": "inner"}] * 10    # exactly at target — no inner shortage
            + [{"tier": "mid"}] * 7     # exactly at target — no mid shortage
            + [{"tier": "outer"}] * 5   # exactly at target — no outer shortage
            + [{"tier": "extreme"}] * 25  # 3 short of 28-slot extreme target
        )

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "BUY_INNER_TIER_COUNT", 10, create=True), \
                patch.object(offer_manager.cfg, "BUY_MID_TIER_COUNT", 7, create=True), \
                patch.object(offer_manager.cfg, "BUY_OUTER_TIER_COUNT", 5, create=True), \
                patch.object(offer_manager.cfg, "BUY_EXTREME_TIER_COUNT", 0, create=True), \
                patch.object(offer_manager, "get_open_offers", return_value=existing):
            slots = manager.get_replenishment_slots(
                side="buy",
                total_slots=50,
                cat_asset_id="test-cat",
            )

        # Only extreme is short (22–49 with 25 existing → 3 slots needed);
        # fill from innermost extreme positions [22, 23, 24].
        self.assertEqual(slots, [22, 23, 24])

    def test_create_ladder_can_bypass_stale_refill_interpolation(self):
        manager = offer_manager.OfferManager()
        selected = itertools.count(1)

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                del tier, side
                return Decimal("1.0")

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            del self, offer_dict, max_retries, expiry_offset, expiry_secs
            del used_coins, coin_ids_enabled, preferred_tier, strict_preferred_tier
            return {
                "success": True,
                "trade_id": f"trade-{selected_coin_id}",
                "trade_record": {"trade_id": f"trade-{selected_coin_id}"},
                "locked_coin_id": selected_coin_id,
                "offer": f"offer-{selected_coin_id}",
            }

        stale_extreme_offers = [
            {"tier": "extreme", "price_xch": "0.0002030566", "size_xch": "1.0"},
            {"tier": "extreme", "price_xch": "0.0002039827", "size_xch": "1.0"},
        ]

        def fake_select(*args, **kwargs):
            del args, kwargs
            return f"0xcoin{next(selected)}"

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "SELL_INNER_TIER_COUNT", 7, create=True), \
                patch.object(offer_manager.cfg, "SELL_MID_TIER_COUNT", 7, create=True), \
                patch.object(offer_manager.cfg, "SELL_OUTER_TIER_COUNT", 6, create=True), \
                patch.object(offer_manager.cfg, "SELL_EXTREME_TIER_COUNT", 4, create=True), \
                patch.object(offer_manager.cfg, "MAX_SPREAD_BPS", Decimal("1350"), create=True), \
                patch.object(offer_manager.cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"), create=True), \
                patch.object(offer_manager, "get_open_offers", return_value=stale_extreme_offers), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=fake_select), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": []}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.0001762177694059978342543424548"),
                side="sell",
                num_offers=2,
                spread_fraction=Decimal("0.15"),
                total_slots=24,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
                slot_sequence=[22, 23],
                interpolate_refill_prices=False,
            )

        self.assertEqual(len(created), 2)
        self.assertEqual([offer["tier"] for offer in created], ["extreme", "extreme"])

    def test_create_ladder_falls_back_when_refill_interpolation_rejects_slot(self):
        manager = offer_manager.OfferManager()
        selected = itertools.count(1)

        class _FakeRiskManager:
            @staticmethod
            def get_tier_size(tier, side=None):
                del tier, side
                return Decimal("1.0")

        def fake_create_offer_with_retry(self, offer_dict, max_retries=2,
                                         expiry_offset=0, expiry_secs=None,
                                         used_coins=None, coin_ids_enabled=False,
                                         selected_coin_id=None, preferred_tier=None,
                                         strict_preferred_tier=False):
            del self, offer_dict, max_retries, expiry_offset, expiry_secs
            del used_coins, coin_ids_enabled, preferred_tier, strict_preferred_tier
            return {
                "success": True,
                "trade_id": f"trade-{selected_coin_id}",
                "trade_record": {"trade_id": f"trade-{selected_coin_id}"},
                "locked_coin_id": selected_coin_id,
                "offer": f"offer-{selected_coin_id}",
            }

        stale_extreme_offers = [
            {"tier": "extreme", "price_xch": "0.0002030566", "size_xch": "1.0"},
            {"tier": "extreme", "price_xch": "0.0002039827", "size_xch": "1.0"},
        ]

        def fake_select(*args, **kwargs):
            del args, kwargs
            return f"0xcoin{next(selected)}"

        with patch.object(offer_manager.cfg, "TIER_ENABLED", True), \
                patch.object(offer_manager.cfg, "SELL_INNER_TIER_COUNT", 7, create=True), \
                patch.object(offer_manager.cfg, "SELL_MID_TIER_COUNT", 7, create=True), \
                patch.object(offer_manager.cfg, "SELL_OUTER_TIER_COUNT", 6, create=True), \
                patch.object(offer_manager.cfg, "SELL_EXTREME_TIER_COUNT", 4, create=True), \
                patch.object(offer_manager.cfg, "MAX_SPREAD_BPS", Decimal("1350"), create=True), \
                patch.object(offer_manager.cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"), create=True), \
                patch.object(offer_manager, "get_open_offers", return_value=stale_extreme_offers), \
                patch.object(offer_manager.OfferManager, "_select_coin_for_offer",
                             side_effect=fake_select), \
                patch.object(offer_manager.OfferManager, "create_offer_with_retry",
                             new=fake_create_offer_with_retry), \
                patch.object(offer_manager, "get_exact_spendable_coins_rpc",
                             return_value={"success": True, "confirmed_records": []}), \
                patch.object(offer_manager, "add_offer"), \
                patch.object(offer_manager, "log_event"), \
                patch.object(offer_manager, "get_offer_bech32", return_value=""), \
                patch("builtins.print"), \
                patch("database.lock_coin"):
            created = manager.create_ladder(
                mid_price=Decimal("0.0001762177694059978342543424548"),
                side="sell",
                num_offers=2,
                spread_fraction=Decimal("0.15"),
                total_slots=24,
                coin_ids_enabled=True,
                risk_manager=_FakeRiskManager(),
                slot_sequence=[22, 23],
            )

        self.assertEqual(len(created), 2)
        self.assertEqual([offer["tier"] for offer in created], ["extreme", "extreme"])

    def test_slot_size_variation_supports_thousands_of_unique_steps(self):
        self.assertEqual(
            offer_manager.OfferManager._slot_size_variation(0, expected_unique_count=5),
            Decimal("0.00001000"),
        )
        self.assertEqual(
            offer_manager.OfferManager._slot_size_variation(99, expected_unique_count=100),
            Decimal("0.00100000"),
        )
        self.assertEqual(
            offer_manager.OfferManager._slot_size_variation(999, expected_unique_count=1000),
            Decimal("0.00100000"),
        )
        self.assertEqual(
            offer_manager.OfferManager._slot_size_variation(250000, expected_unique_count=250000),
            Decimal("0.001"),
        )


class CancelPendingMempoolTests(unittest.TestCase):
    """Regression coverage: cancel TX that Sage accepted into the mempool
    but didn't confirm on-chain must NOT flip the DB to 'cancelled' —
    the offer is still live and can still fill. Previously the bot did
    flip the DB, producing ghost offers during mempool congestion."""

    def test_pending_methods_constant_covers_all_unconfirmed_sage_paths(self):
        self.assertIn("submitted_pending_confirm",
                      offer_manager.CANCEL_PENDING_METHODS)
        self.assertIn("already_in_mempool",
                      offer_manager.CANCEL_PENDING_METHODS)
        self.assertIn("mempool_conflict_inflight",
                      offer_manager.CANCEL_PENDING_METHODS)

    def test_cancel_offers_skips_db_update_for_pending_methods(self):
        manager = offer_manager.OfferManager()

        # Mock the DB open-offers lookup to return one offer we want to cancel.
        with patch.object(offer_manager, "get_open_offers",
                          return_value=[{"trade_id": "tid-pending",
                                         "coin_id": "0xc1", "side": "buy"}]), \
             patch.object(offer_manager, "cancel_offers_batch",
                          return_value={"tid-pending": {
                              "success": True,
                              "method": "submitted_pending_confirm",
                              "note": "Cancel submitted, awaiting on-chain confirm",
                          }}), \
             patch.object(offer_manager, "update_offer_status") as mock_update, \
             patch.object(offer_manager, "transition_offer"), \
             patch.object(offer_manager, "cleanup_expired_offers", return_value=0):
            manager.cancel_offers(["tid-pending"], reason="test")

        # The DB must NOT be flipped to cancelled — the offer is still live.
        cancelled_calls = [c for c in mock_update.call_args_list
                           if len(c.args) >= 2 and c.args[1] == "cancelled"]
        self.assertEqual(cancelled_calls, [],
                         "DB must not be marked cancelled while cancel TX is "
                         "still pending in the mempool")

    def test_cancel_offers_flips_db_for_confirmed_methods(self):
        """Sanity check: the happy path (truly confirmed) DOES flip the DB."""
        manager = offer_manager.OfferManager()

        with patch.object(offer_manager, "get_open_offers",
                          return_value=[{"trade_id": "tid-ok",
                                         "coin_id": "0xc2", "side": "buy"}]), \
             patch.object(offer_manager, "cancel_offers_batch",
                          return_value={"tid-ok": {
                              "success": True,
                              "method": "confirmed_by_unlock",
                          }}), \
             patch.object(offer_manager, "update_offer_status") as mock_update, \
             patch.object(offer_manager, "transition_offer"), \
             patch.object(offer_manager, "cleanup_expired_offers", return_value=0):
            manager.cancel_offers(["tid-ok"], reason="test")

        cancelled_calls = [c for c in mock_update.call_args_list
                           if len(c.args) >= 2 and c.args[1] == "cancelled"]
        self.assertEqual(len(cancelled_calls), 1,
                         "DB must be flipped to cancelled when Sage confirms "
                         "the cancel via coin unlock")

    def test_cancel_offers_purges_public_post_queues(self):
        manager = offer_manager.OfferManager()
        purged = {"dexie": [], "splash": []}

        class QueueStub:
            def __init__(self, name):
                self.name = name

            def purge_trade_ids(self, trade_ids):
                purged[self.name].append(list(trade_ids))

        manager.dexie_manager = QueueStub("dexie")
        manager.splash_manager = QueueStub("splash")

        with patch.object(offer_manager, "get_open_offers",
                          return_value=[{"trade_id": "tid-ok",
                                         "coin_id": "0xc2", "side": "buy"}]), \
             patch.object(offer_manager, "cancel_offers_batch",
                          return_value={"tid-ok": {
                              "success": True,
                              "method": "confirmed_by_unlock",
                          }}), \
             patch.object(offer_manager, "update_offer_status"), \
             patch.object(offer_manager, "transition_offer"), \
             patch.object(offer_manager, "cleanup_expired_offers", return_value=0):
            manager.cancel_offers(["tid-ok"], reason="test")

        self.assertEqual(purged["dexie"], [["tid-ok"]])
        self.assertEqual(purged["splash"], [["tid-ok"]])


if __name__ == "__main__":
    unittest.main()
