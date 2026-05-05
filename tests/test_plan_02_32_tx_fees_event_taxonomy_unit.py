"""Slice 02-32 — tx_fees.py, event_taxonomy.py, notification_manager.py unit tests.

No HTTP calls, no OS notifications dispatched (patched). Covers
_decimal_or_zero, xch_to_mojos, mojos_to_xch, fee pool helpers,
EventCategory StrEnum, categorize_event, get_category_map, and
NotificationManager rate limiting + category control.
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

try:
    import tx_fees as _tf
    _SKIP_TF = None
except ModuleNotFoundError as exc:
    _tf = None
    _SKIP_TF = str(exc)

try:
    import event_taxonomy as _et
    _SKIP_ET = None
except ModuleNotFoundError as exc:
    _et = None
    _SKIP_ET = str(exc)

try:
    import notification_manager as _nm
    _SKIP_NM = None
except ModuleNotFoundError as exc:
    _nm = None
    _SKIP_NM = str(exc)


# ===========================================================================
# tx_fees — pure conversion functions
# ===========================================================================

@unittest.skipIf(_SKIP_TF is not None, f"tx_fees unavailable: {_SKIP_TF}")
class TestDecimalOrZero(unittest.TestCase):
    def test_valid_decimal_string(self):
        self.assertEqual(_tf._decimal_or_zero("1.5"), Decimal("1.5"))

    def test_integer_input(self):
        self.assertEqual(_tf._decimal_or_zero(2), Decimal("2"))

    def test_none_returns_zero(self):
        self.assertEqual(_tf._decimal_or_zero(None), Decimal("0"))

    def test_empty_string_returns_zero(self):
        self.assertEqual(_tf._decimal_or_zero(""), Decimal("0"))

    def test_invalid_string_returns_zero(self):
        self.assertEqual(_tf._decimal_or_zero("not_a_number"), Decimal("0"))

    def test_decimal_instance(self):
        self.assertEqual(_tf._decimal_or_zero(Decimal("3.14")), Decimal("3.14"))


@unittest.skipIf(_SKIP_TF is not None, f"tx_fees unavailable: {_SKIP_TF}")
class TestXchToMojos(unittest.TestCase):
    def test_one_xch_is_one_trillion(self):
        self.assertEqual(_tf.xch_to_mojos(1), 1_000_000_000_000)

    def test_half_xch(self):
        self.assertEqual(_tf.xch_to_mojos(Decimal("0.5")), 500_000_000_000)

    def test_zero_returns_zero(self):
        self.assertEqual(_tf.xch_to_mojos(0), 0)

    def test_negative_returns_zero(self):
        self.assertEqual(_tf.xch_to_mojos(-1), 0)

    def test_none_returns_zero(self):
        self.assertEqual(_tf.xch_to_mojos(None), 0)

    def test_fractional_rounds_up(self):
        # Tiny fraction — rounds up to 1 mojo
        result = _tf.xch_to_mojos(Decimal("0.0000000000001"))
        self.assertGreaterEqual(result, 1)


@unittest.skipIf(_SKIP_TF is not None, f"tx_fees unavailable: {_SKIP_TF}")
class TestMojosToXch(unittest.TestCase):
    def test_one_trillion_mojos_is_one_xch(self):
        self.assertEqual(_tf.mojos_to_xch(1_000_000_000_000), Decimal("1"))

    def test_zero_mojos_returns_zero(self):
        self.assertEqual(_tf.mojos_to_xch(0), Decimal("0"))

    def test_negative_returns_zero(self):
        self.assertEqual(_tf.mojos_to_xch(-1), Decimal("0"))

    def test_none_returns_zero(self):
        self.assertEqual(_tf.mojos_to_xch(None), Decimal("0"))

    def test_half_trillion_mojos(self):
        self.assertEqual(_tf.mojos_to_xch(500_000_000_000), Decimal("0.5"))


# ===========================================================================
# tx_fees — cfg-dependent helpers (patched cfg)
# ===========================================================================

@unittest.skipIf(_SKIP_TF is not None, f"tx_fees unavailable: {_SKIP_TF}")
class TestFeePoolHelpers(unittest.TestCase):
    def _cfg(self, **attrs):
        return SimpleNamespace(**attrs)

    def test_get_fee_pool_count_normal(self):
        with patch.object(_tf, "cfg", self._cfg(FEE_PREP_COUNT=5)):
            self.assertEqual(_tf.get_fee_pool_count(), 5)

    def test_get_fee_pool_count_clamps_negative(self):
        with patch.object(_tf, "cfg", self._cfg(FEE_PREP_COUNT=-3)):
            self.assertEqual(_tf.get_fee_pool_count(), 0)

    def test_get_fee_coin_size_xch(self):
        with patch.object(_tf, "cfg", self._cfg(FEE_COIN_SIZE_XCH=Decimal("0.005"))):
            self.assertEqual(_tf.get_fee_coin_size_xch(), Decimal("0.005"))

    def test_get_fee_coin_size_mojos(self):
        with patch.object(_tf, "cfg", self._cfg(FEE_COIN_SIZE_XCH=Decimal("0.001"))):
            self.assertEqual(_tf.get_fee_coin_size_mojos(), 1_000_000_000)

    def test_fee_pool_configured_when_count_and_size_set(self):
        with patch.object(_tf, "cfg", self._cfg(
            FEE_PREP_COUNT=5, FEE_COIN_SIZE_XCH=Decimal("0.005")
        )):
            self.assertTrue(_tf.fee_pool_configured())

    def test_fee_pool_not_configured_when_count_zero(self):
        with patch.object(_tf, "cfg", self._cfg(
            FEE_PREP_COUNT=0, FEE_COIN_SIZE_XCH=Decimal("0.005")
        )):
            self.assertFalse(_tf.fee_pool_configured())

    def test_fee_pool_not_configured_when_size_zero(self):
        with patch.object(_tf, "cfg", self._cfg(
            FEE_PREP_COUNT=5, FEE_COIN_SIZE_XCH=Decimal("0")
        )):
            self.assertFalse(_tf.fee_pool_configured())

    def test_get_fee_pool_plan_has_expected_keys(self):
        with patch.object(_tf, "cfg", self._cfg(
            FEE_PREP_COUNT=3, FEE_COIN_SIZE_XCH=Decimal("0.005"),
            TRANSACTION_FEE_MODE="manual", TRANSACTION_FEE_XCH=Decimal("0.001"),
            CHIA_WALLET_CERT="", CHIA_WALLET_KEY="", WALLET_TYPE="sage"
        )):
            plan = _tf.get_fee_pool_plan()
        for key in ("enabled", "configured", "tier_name", "count", "coin_size_mojos", "coin_size_xch"):
            self.assertIn(key, plan)


# ===========================================================================
# tx_fees — get_transaction_fee_mode
# ===========================================================================

@unittest.skipIf(_SKIP_TF is not None, f"tx_fees unavailable: {_SKIP_TF}")
class TestGetTransactionFeeMode(unittest.TestCase):
    def test_manual_mode(self):
        with patch.object(_tf, "cfg", SimpleNamespace(TRANSACTION_FEE_MODE="manual")):
            self.assertEqual(_tf.get_transaction_fee_mode(), "manual")

    def test_auto_mode(self):
        with patch.object(_tf, "cfg", SimpleNamespace(TRANSACTION_FEE_MODE="auto")):
            self.assertEqual(_tf.get_transaction_fee_mode(), "auto")

    def test_invalid_mode_defaults_to_auto(self):
        with patch.object(_tf, "cfg", SimpleNamespace(TRANSACTION_FEE_MODE="bogus")):
            self.assertEqual(_tf.get_transaction_fee_mode(), "auto")


# ===========================================================================
# event_taxonomy — EventCategory + categorize_event + get_category_map
# ===========================================================================

@unittest.skipIf(_SKIP_ET is not None, f"event_taxonomy unavailable: {_SKIP_ET}")
class TestEventCategory(unittest.TestCase):
    def test_all_expected_categories_present(self):
        categories = {c.value for c in _et.EventCategory}
        for expected in ("lifecycle", "offer", "pricing", "wallet",
                         "exchange", "risk", "system", "coin"):
            self.assertIn(expected, categories)

    def test_category_is_str(self):
        self.assertIsInstance(_et.EventCategory.LIFECYCLE, str)


@unittest.skipIf(_SKIP_ET is not None, f"event_taxonomy unavailable: {_SKIP_ET}")
class TestCategorizeEvent(unittest.TestCase):
    def test_known_event_returns_correct_category(self):
        # 'bot_started' should be LIFECYCLE
        result = _et.categorize_event("bot_started")
        self.assertEqual(result, _et.EventCategory.LIFECYCLE)

    def test_unknown_event_returns_system(self):
        result = _et.categorize_event("totally_unknown_event_xyz")
        self.assertEqual(result, _et.EventCategory.SYSTEM)

    def test_empty_string_returns_system(self):
        result = _et.categorize_event("")
        self.assertEqual(result, _et.EventCategory.SYSTEM)

    def test_offer_event_categorized_as_offer(self):
        result = _et.categorize_event("offer_created")
        self.assertEqual(result, _et.EventCategory.OFFER)

    def test_risk_event_categorized_as_risk(self):
        result = _et.categorize_event("circuit_breaker_tripped")
        self.assertEqual(result, _et.EventCategory.RISK)


@unittest.skipIf(_SKIP_ET is not None, f"event_taxonomy unavailable: {_SKIP_ET}")
class TestGetCategoryMap(unittest.TestCase):
    def test_returns_dict(self):
        m = _et.get_category_map()
        self.assertIsInstance(m, dict)

    def test_returns_copy_not_reference(self):
        m1 = _et.get_category_map()
        m2 = _et.get_category_map()
        m1["__test__"] = "modified"
        self.assertNotIn("__test__", m2)

    def test_map_has_entries(self):
        m = _et.get_category_map()
        self.assertGreater(len(m), 0)


# ===========================================================================
# notification_manager — rate limiting and category control (no plyer calls)
# ===========================================================================

@unittest.skipIf(_SKIP_NM is not None, f"notification_manager unavailable: {_SKIP_NM}")
class TestNotificationManagerRateLimit(unittest.TestCase):
    def _make_nm(self):
        with patch.object(_nm, "PLYER_AVAILABLE", True):
            mgr = _nm.NotificationManager()
        mgr._send = MagicMock()  # Suppress actual OS notifications
        return mgr

    def test_notify_returns_true_when_first_call(self):
        mgr = self._make_nm()
        result = mgr.notify("Title", "Message", category="fill")
        self.assertTrue(result)

    def test_notify_returns_false_within_cooldown(self):
        mgr = self._make_nm()
        mgr.notify("T", "M", category="fill")
        result = mgr.notify("T", "M", category="fill")
        self.assertFalse(result)

    def test_notify_returns_false_when_disabled(self):
        mgr = self._make_nm()
        mgr.enabled = False
        result = mgr.notify("T", "M", category="fill")
        self.assertFalse(result)

    def test_notify_returns_false_when_category_disabled(self):
        mgr = self._make_nm()
        mgr.set_category_enabled("fill", False)
        result = mgr.notify("T", "M", category="fill")
        self.assertFalse(result)

    def test_set_category_enabled_updates_state(self):
        mgr = self._make_nm()
        mgr.set_category_enabled("fill", False)
        cats = mgr.get_categories()
        self.assertFalse(cats["fill"]["enabled"])

    def test_get_categories_returns_all_default_categories(self):
        mgr = self._make_nm()
        cats = mgr.get_categories()
        for key in ("fill", "error", "circuit_breaker", "sniper", "coin_prep",
                    "price_alert", "info", "warning", "critical"):
            self.assertIn(key, cats)

    def test_get_categories_returns_copy(self):
        mgr = self._make_nm()
        c1 = mgr.get_categories()
        c1["fill"]["enabled"] = False
        c2 = mgr.get_categories()
        self.assertTrue(c2["fill"]["enabled"])  # original unchanged

    def test_different_categories_have_independent_rate_limits(self):
        mgr = self._make_nm()
        mgr.notify("T", "M", category="fill")
        result = mgr.notify("T", "M", category="error")
        self.assertTrue(result)

    def test_duplicate_warning_notification_is_suppressed_after_cooldown(self):
        mgr = self._make_nm()
        mgr._categories["warning"]["cooldown_secs"] = 0
        mgr._categories["warning"]["dedupe_secs"] = 3600

        self.assertTrue(mgr.notify("Bot Health Warning", "same message", category="warning"))
        self.assertFalse(mgr.notify("Bot Health Warning", "same message", category="warning"))
        self.assertTrue(mgr.notify("Bot Health Warning", "different message", category="warning"))

    def test_send_truncates_windows_balloon_text_limits(self):
        with patch.object(_nm, "PLYER_AVAILABLE", True):
            mgr = _nm.NotificationManager()

        with patch.object(_nm.plyer_notification, "notify") as notify:
            mgr._send("T" * 100, "M" * 300, timeout=10)

        kwargs = notify.call_args.kwargs
        self.assertLessEqual(len(kwargs["title"]), 64)
        self.assertLessEqual(len(kwargs["message"]), 240)


if __name__ == "__main__":
    unittest.main()
