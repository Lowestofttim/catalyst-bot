"""Slice 02-11 — offer_manager.py unit tests.

No network/wallet calls. Tests module-level conversion helpers, static methods
(_slot_size_variation, _size_key, _coin_designation_priority), slot suspension
lifecycle, bot-cancel tracking, detect_expiring_offers, _classify_tier,
should_requote, and _allocate_unique_requested_mojos.
"""

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

try:
    import offer_manager as _om_mod
    from offer_manager import (
        OfferManager,
        xch_to_mojos, mojos_to_xch,
        cat_to_mojos, mojos_to_cat,
        CANCEL_PENDING_METHODS,
    )
    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_FAKE_CFG = SimpleNamespace(
    TIER_ENABLED=True,
    BUY_INNER_TIER_COUNT=2,
    BUY_MID_TIER_COUNT=3,
    BUY_OUTER_TIER_COUNT=2,
    BUY_EXTREME_TIER_COUNT=0,
    SELL_INNER_TIER_COUNT=2,
    SELL_MID_TIER_COUNT=3,
    SELL_OUTER_TIER_COUNT=2,
    SELL_EXTREME_TIER_COUNT=0,
    AUTO_REQUOTE=True,
    REQUOTE_COOLDOWN_SECS=30,
    REQUOTE_DRIFT_INNER=Decimal("0.003"),
    REQUOTE_DRIFT_MID=Decimal("0.008"),
    REQUOTE_DRIFT_FULL=Decimal("0.02"),
    REQUOTE_DRIFT_EMERGENCY=Decimal("0.05"),
    CAT_ASSET_ID="0xdeadbeef",
    WALLET_ID_XCH=1,
    MAX_POSITION_XCH=Decimal("2.9"),
    DEFAULT_TRADE_XCH=Decimal("0.03"),
    OFFER_REFRESH_BEFORE=1800,
    LADDER_CREATE_PARALLELISM=5,
    get_requote_fraction=lambda: Decimal("0.01"),
)


@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class _OM(unittest.TestCase):
    def setUp(self):
        self._cfg_patcher = patch.object(_om_mod, "cfg", _FAKE_CFG)
        self._cfg_patcher.start()
        self._log_patcher = patch.object(_om_mod, "log_event")
        self.log_event = self._log_patcher.start()
        self._manager = OfferManager()

    def tearDown(self):
        self._cfg_patcher.stop()
        self._log_patcher.stop()


# ===========================================================================
# Conversion helpers (module-level pure functions)
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestConversionHelpers(unittest.TestCase):
    def test_xch_to_mojos_one_xch(self):
        self.assertEqual(xch_to_mojos(Decimal("1")), 1_000_000_000_000)

    def test_xch_to_mojos_fractional(self):
        self.assertEqual(xch_to_mojos(Decimal("0.001")), 1_000_000_000)

    def test_xch_to_mojos_truncates_not_rounds(self):
        # 0.0000000000001 XCH = 0.1 mojos — truncated to 0
        self.assertEqual(xch_to_mojos(Decimal("0.0000000000001")), 0)

    def test_mojos_to_xch_roundtrip(self):
        mojos = 1_234_567_890_123
        self.assertEqual(xch_to_mojos(mojos_to_xch(mojos)), mojos)

    def test_cat_to_mojos_3_decimals(self):
        # 1 token with 3 decimal places = 1000 mojos
        self.assertEqual(cat_to_mojos(Decimal("1"), 3), 1000)

    def test_cat_to_mojos_0_decimals(self):
        self.assertEqual(cat_to_mojos(Decimal("5"), 0), 5)

    def test_cat_to_mojos_truncates(self):
        # 1.9999 with 3 decimals = 1999 mojos (truncated)
        self.assertEqual(cat_to_mojos(Decimal("1.9999"), 3), 1999)

    def test_mojos_to_cat_roundtrip(self):
        mojos = 12345
        decimals = 3
        self.assertEqual(cat_to_mojos(mojos_to_cat(mojos, decimals), decimals), mojos)

    def test_mojos_to_xch_zero(self):
        self.assertEqual(mojos_to_xch(0), Decimal("0"))

    def test_cat_to_mojos_large_decimals(self):
        # CAT with 12 decimals (same as XCH)
        self.assertEqual(cat_to_mojos(Decimal("1"), 12), 1_000_000_000_000)


# ===========================================================================
# CANCEL_PENDING_METHODS frozenset
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestCancelPendingMethods(unittest.TestCase):
    def test_submitted_pending_in_set(self):
        self.assertIn("submitted_pending_confirm", CANCEL_PENDING_METHODS)

    def test_already_in_mempool_in_set(self):
        self.assertIn("already_in_mempool", CANCEL_PENDING_METHODS)

    def test_confirmed_not_in_set(self):
        self.assertNotIn("confirmed", CANCEL_PENDING_METHODS)


# ===========================================================================
# _slot_size_variation (static, pure)
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestSlotSizeVariation(unittest.TestCase):
    def test_slot_zero_positive_variation(self):
        v = OfferManager._slot_size_variation(0)
        self.assertGreater(v, 0)

    def test_increasing_slots_increasing_variation(self):
        v0 = OfferManager._slot_size_variation(0)
        v5 = OfferManager._slot_size_variation(5)
        self.assertGreaterEqual(v5, v0)

    def test_never_exceeds_max(self):
        for slot in range(200):
            v = OfferManager._slot_size_variation(slot)
            self.assertLessEqual(v, Decimal("0.001"))

    def test_negative_slot_clamped(self):
        # Negative slot treated as 0
        self.assertEqual(
            OfferManager._slot_size_variation(-5),
            OfferManager._slot_size_variation(0),
        )

    def test_large_unique_count_smaller_step(self):
        v_small = OfferManager._slot_size_variation(1, expected_unique_count=10)
        v_large = OfferManager._slot_size_variation(1, expected_unique_count=1000)
        self.assertGreaterEqual(v_small, v_large)


# ===========================================================================
# _size_key (static, pure)
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestSizeKey(unittest.TestCase):
    def test_normalises_to_8_decimal_places(self):
        key = OfferManager._size_key(Decimal("1.23456789012"))
        self.assertEqual(key, Decimal("1.23456789"))

    def test_float_input_normalised(self):
        key = OfferManager._size_key(Decimal("0.1"))
        self.assertEqual(str(key), "0.10000000")


# ===========================================================================
# _coin_designation_priority (static, pure)
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestCoinDesignationPriority(unittest.TestCase):
    def test_tier_spare_preferred_match_is_zero(self):
        p = OfferManager._coin_designation_priority("tier_spare", "inner", "inner")
        self.assertEqual(p, 0)

    def test_tier_active_preferred_match_is_one(self):
        p = OfferManager._coin_designation_priority("tier_active", "inner", "inner")
        self.assertEqual(p, 1)

    def test_tier_spare_no_preferred(self):
        p = OfferManager._coin_designation_priority("tier_spare", "inner")
        self.assertEqual(p, 0)

    def test_dust_no_preferred(self):
        p = OfferManager._coin_designation_priority("dust", "none")
        self.assertEqual(p, 2)

    def test_unknown_is_highest_priority_number(self):
        no_pref = OfferManager._coin_designation_priority("unknown_desig", "none")
        self.assertEqual(no_pref, 3)

    def test_unknown_with_pref_is_5(self):
        p = OfferManager._coin_designation_priority("unknown", "none", "inner")
        self.assertEqual(p, 5)


# ===========================================================================
# Slot suspension lifecycle
# ===========================================================================

class TestSlotSuspension(_OM):
    def test_new_slot_not_suspended(self):
        self.assertFalse(self._manager.is_slot_suspended("buy", 0))

    def test_suspended_after_threshold_failures(self):
        threshold = self._manager._slot_suspend_threshold
        for _ in range(threshold):
            self._manager.record_slot_coin_failure("buy", 0)
        self.assertTrue(self._manager.is_slot_suspended("buy", 0))
        self.assertTrue(any(
            call.args[0] == "info" and call.args[1] == "slot_suspended"
            for call in self.log_event.call_args_list
        ))

    def test_below_threshold_not_suspended(self):
        threshold = self._manager._slot_suspend_threshold
        for _ in range(threshold - 1):
            self._manager.record_slot_coin_failure("buy", 0)
        self.assertFalse(self._manager.is_slot_suspended("buy", 0))

    def test_clear_slot_failure_unsuspends(self):
        threshold = self._manager._slot_suspend_threshold
        for _ in range(threshold):
            self._manager.record_slot_coin_failure("buy", 0)
        self._manager.clear_slot_failure("buy", 0)
        self.assertFalse(self._manager.is_slot_suspended("buy", 0))

    def test_get_suspended_slot_count(self):
        for _ in range(self._manager._slot_suspend_threshold):
            self._manager.record_slot_coin_failure("buy", 0)
            self._manager.record_slot_coin_failure("buy", 1)
        self.assertEqual(self._manager.get_suspended_slot_count("buy"), 2)

    def test_suspended_count_does_not_include_other_side(self):
        for _ in range(self._manager._slot_suspend_threshold):
            self._manager.record_slot_coin_failure("buy", 0)
        self.assertEqual(self._manager.get_suspended_slot_count("sell"), 0)

    def test_unsuspend_requires_coin_for_the_suspended_slot_tier(self):
        for _ in range(self._manager._slot_suspend_threshold):
            self._manager.record_slot_coin_failure("sell", 0)

        with patch("database.get_free_coins", return_value=[
            {"designation": "tier_spare", "assigned_tier": "mid"},
            {"designation": "tier_spare", "assigned_tier": "outer"},
        ]):
            self._manager.unsuspend_slots_if_coins_available("sell")

        self.assertTrue(self._manager.is_slot_suspended("sell", 0))


class TestPositionHardGuard(_OM):
    def test_blocked_side_records_pause_and_logs_warning(self):
        class Risk:
            _net_position_cat = Decimal("-18988")

            def get_tier_size(self, tier, side="sell"):
                del tier, side
                return Decimal("0.03")

        with patch.object(_om_mod, "get_open_offers", return_value=[]):
            result = self._manager.check_position_guard(
                side="sell",
                mid_price=Decimal("0.00012"),
                num=45,
                slot_start=0,
                total_slots=45,
                slot_sequence=None,
                risk_manager=Risk(),
                default_size=Decimal("0.03"),
                cat_asset_id="0xdeadbeef",
                log_block=True,
                record_pause=True,
            )

        self.assertTrue(result["blocked"])
        pause = self._manager.get_position_guard_pause("sell")
        self.assertEqual(pause["side"], "sell")
        self.assertEqual(pause["opposite_side"], "buy")
        self.assertTrue(any(
            call.args[0] == "warning" and call.args[1] == "position_hard_guard_blocked"
            for call in self.log_event.call_args_list
        ))

    def test_unsuspend_clears_slot_when_required_tier_coin_is_available(self):
        for _ in range(self._manager._slot_suspend_threshold):
            self._manager.record_slot_coin_failure("sell", 0)

        with patch("database.get_free_coins", return_value=[
            {"designation": "tier_spare", "assigned_tier": "inner"},
        ]):
            self._manager.unsuspend_slots_if_coins_available("sell")

        self.assertFalse(self._manager.is_slot_suspended("sell", 0))

    def test_replenishment_slots_skip_suspended_slots(self):
        for _ in range(self._manager._slot_suspend_threshold):
            self._manager.record_slot_coin_failure("sell", 0)
            self._manager.record_slot_coin_failure("sell", 1)

        with patch.object(_om_mod, "get_open_offers", return_value=[]):
            slots = self._manager.get_replenishment_slots("sell", 7)

        self.assertEqual(slots, [2, 3, 4, 5, 6])

    def test_clear_cycle_coins_empties_set(self):
        self._manager._cycle_used_coin_ids.add("0xcoin1")
        self._manager.clear_cycle_coins()
        self.assertEqual(len(self._manager._cycle_used_coin_ids), 0)

    def test_auto_clear_after_threshold_plus_20(self):
        # After threshold + 21 failures the slot is auto-cleared
        total = self._manager._slot_suspend_threshold + 21
        for _ in range(total):
            self._manager.record_slot_coin_failure("buy", 0)
        self.assertFalse(self._manager.is_slot_suspended("buy", 0))


# ===========================================================================
# Bot-cancel tracking
# ===========================================================================

class TestBotCancelTracking(_OM):
    def test_not_bot_cancelled_initially(self):
        self.assertFalse(self._manager.is_bot_cancelled("0xtrade123"))

    def test_is_bot_cancelled_after_adding(self):
        self._manager._bot_cancelled_ids.add("0xtrade123")
        self.assertTrue(self._manager.is_bot_cancelled("0xtrade123"))

    def test_is_bot_cancelled_non_destructive(self):
        self._manager._bot_cancelled_ids.add("0xtrade123")
        self._manager.is_bot_cancelled("0xtrade123")
        # Still in set after query
        self.assertIn("0xtrade123", self._manager._bot_cancelled_ids)

    def test_get_cached_details_unknown_returns_none(self):
        self.assertIsNone(self._manager.get_cached_details("unknown"))

    def test_get_cached_details_known_returns_dict(self):
        self._manager._offer_details_cache["tid1"] = {"price": Decimal("0.001")}
        self.assertEqual(self._manager.get_cached_details("tid1"), {"price": Decimal("0.001")})


# ===========================================================================
# detect_expiring_offers
# ===========================================================================

class TestDetectExpiringOffers(_OM):
    def _make_offer(self, trade_id: str, max_time: int):
        return {"trade_id": trade_id, "valid_times": {"max_time": max_time}}

    def test_no_offers_returns_empty(self):
        result = self._manager.detect_expiring_offers([])
        self.assertEqual(result, [])

    def test_offer_expiring_soon_included(self):
        soon = int(time.time()) + 600  # 10 min from now < 1800 default
        result = self._manager.detect_expiring_offers([self._make_offer("tid1", soon)])
        self.assertIn("tid1", result)

    def test_offer_far_away_excluded(self):
        far = int(time.time()) + 7200  # 2 hours > 1800 refresh window
        result = self._manager.detect_expiring_offers([self._make_offer("tid1", far)])
        self.assertEqual(result, [])

    def test_already_expired_excluded(self):
        past = int(time.time()) - 100
        result = self._manager.detect_expiring_offers([self._make_offer("tid1", past)])
        self.assertEqual(result, [])

    def test_no_valid_times_excluded(self):
        offer = {"trade_id": "tid1"}
        result = self._manager.detect_expiring_offers([offer])
        self.assertEqual(result, [])

    def test_max_time_zero_excluded(self):
        result = self._manager.detect_expiring_offers([self._make_offer("tid1", 0)])
        self.assertEqual(result, [])

    def test_custom_refresh_window_respected(self):
        soon = int(time.time()) + 200  # within 300s
        far = int(time.time()) + 400   # outside 300s
        offers = [self._make_offer("soon", soon), self._make_offer("far", far)]
        result = self._manager.detect_expiring_offers(offers, refresh_before_secs=300)
        self.assertIn("soon", result)
        self.assertNotIn("far", result)


# ===========================================================================
# _classify_tier
# ===========================================================================

class TestClassifyTier(_OM):
    def test_tier_disabled_returns_mid(self):
        disabled_cfg = SimpleNamespace(**{**_FAKE_CFG.__dict__, "TIER_ENABLED": False})
        with patch.object(_om_mod, "cfg", disabled_cfg):
            result = self._manager._classify_tier(0, 10, "buy")
        self.assertEqual(result, "mid")

    def test_total_zero_returns_mid(self):
        result = self._manager._classify_tier(0, 0, "buy")
        self.assertEqual(result, "mid")

    def test_buy_inner_slots(self):
        # Buy cfg: inner=2, mid=3, outer=2 → slots 0,1 are inner
        result = self._manager._classify_tier(0, 7, "buy")
        self.assertEqual(result, "inner")
        result = self._manager._classify_tier(1, 7, "buy")
        self.assertEqual(result, "inner")

    def test_buy_mid_slots(self):
        # slots 2,3,4 are mid
        result = self._manager._classify_tier(2, 7, "buy")
        self.assertEqual(result, "mid")

    def test_buy_outer_slots(self):
        # slots 5,6 are outer
        result = self._manager._classify_tier(5, 7, "buy")
        self.assertEqual(result, "outer")

    def test_no_tier_counts_uses_ratio(self):
        no_tier_cfg = SimpleNamespace(
            **{**_FAKE_CFG.__dict__,
               "BUY_INNER_TIER_COUNT": 0, "BUY_MID_TIER_COUNT": 0,
               "BUY_OUTER_TIER_COUNT": 0, "BUY_EXTREME_TIER_COUNT": 0}
        )
        with patch.object(_om_mod, "cfg", no_tier_cfg):
            # ratio 0/10 = 0.0 < 0.1 → inner
            r = self._manager._classify_tier(0, 10, "buy")
        self.assertEqual(r, "inner")

    def test_ratio_mid_range(self):
        no_tier_cfg = SimpleNamespace(
            **{**_FAKE_CFG.__dict__,
               "BUY_INNER_TIER_COUNT": 0, "BUY_MID_TIER_COUNT": 0,
               "BUY_OUTER_TIER_COUNT": 0, "BUY_EXTREME_TIER_COUNT": 0}
        )
        with patch.object(_om_mod, "cfg", no_tier_cfg):
            # slot 3/10 = 0.3 → mid
            r = self._manager._classify_tier(3, 10, "buy")
        self.assertEqual(r, "mid")


# ===========================================================================
# should_requote
# ===========================================================================

class TestShouldRequote(_OM):
    def test_auto_requote_disabled_returns_false(self):
        no_rq_cfg = SimpleNamespace(**{**_FAKE_CFG.__dict__, "AUTO_REQUOTE": False})
        with patch.object(_om_mod, "cfg", no_rq_cfg):
            result = self._manager.should_requote(
                "buy", Decimal("100"), Decimal("90"))
        self.assertFalse(result)

    def test_within_cooldown_returns_false(self):
        self._manager._last_requote_time["buy"] = time.time()  # just now
        result = self._manager.should_requote("buy", Decimal("100"), Decimal("90"))
        self.assertFalse(result)

    def test_zero_last_price_returns_false(self):
        self._manager._last_requote_time["buy"] = 0.0
        result = self._manager.should_requote("buy", Decimal("100"), Decimal("0"))
        self.assertFalse(result)

    def test_small_drift_returns_false(self):
        # drift 0.5% < 1% threshold
        self._manager._last_requote_time["buy"] = 0.0
        result = self._manager.should_requote(
            "buy", Decimal("100.5"), Decimal("100"))
        self.assertFalse(result)

    def test_large_drift_returns_true(self):
        # drift 5% > 1% threshold
        self._manager._last_requote_time["buy"] = 0.0
        result = self._manager.should_requote(
            "buy", Decimal("105"), Decimal("100"))
        self.assertTrue(result)

    def test_sell_side_respects_cooldown_independently(self):
        self._manager._last_requote_time["buy"] = 0.0
        self._manager._last_requote_time["sell"] = time.time()
        buy_result = self._manager.should_requote("buy", Decimal("105"), Decimal("100"))
        sell_result = self._manager.should_requote("sell", Decimal("105"), Decimal("100"))
        self.assertTrue(buy_result)
        self.assertFalse(sell_result)


# ===========================================================================
# _allocate_unique_requested_mojos
# ===========================================================================

class TestAllocateUniqueRequestedMojos(_OM):
    def test_no_collision_returns_base(self):
        used = set()
        result = self._manager._allocate_unique_requested_mojos(1000, 0, used)
        self.assertEqual(result, 1000)
        self.assertIn(1000, used)

    def test_collision_returns_different_value(self):
        used = {1000}
        result = self._manager._allocate_unique_requested_mojos(1000, 0, used)
        self.assertNotEqual(result, 1000)

    def test_adds_result_to_used_set(self):
        used = set()
        result = self._manager._allocate_unique_requested_mojos(5000, 3, used)
        self.assertIn(result, used)

    def test_multiple_calls_produce_unique_values(self):
        used = set()
        values = [
            self._manager._allocate_unique_requested_mojos(1000, i, used)
            for i in range(5)
        ]
        self.assertEqual(len(values), len(set(values)))


if __name__ == "__main__":
    unittest.main()
