"""Tests for event_taxonomy.py — canonical event categorization."""

import unittest

from event_taxonomy import (
    EventCategory,
    categorize_event,
    get_category_map,
)


class TestEventTaxonomy(unittest.TestCase):

    def test_lifecycle_events(self):
        self.assertEqual(categorize_event("bot_starting"), EventCategory.LIFECYCLE)
        self.assertEqual(categorize_event("startup_sync_done"), EventCategory.LIFECYCLE)
        self.assertEqual(categorize_event("cycle_complete"), EventCategory.LIFECYCLE)
        self.assertEqual(categorize_event("startup_spares_low"), EventCategory.LIFECYCLE)
        self.assertEqual(categorize_event("startup_coins_unavailable"), EventCategory.LIFECYCLE)

    def test_offer_events(self):
        self.assertEqual(categorize_event("offer_created"), EventCategory.OFFER)
        self.assertEqual(categorize_event("fills_detected"), EventCategory.OFFER)
        self.assertEqual(categorize_event("requoting"), EventCategory.OFFER)

    def test_pricing_events(self):
        self.assertEqual(categorize_event("price_found"), EventCategory.PRICING)
        self.assertEqual(categorize_event("no_price"), EventCategory.PRICING)
        self.assertEqual(categorize_event("mempool_price_confirmed"), EventCategory.PRICING)
        self.assertEqual(categorize_event("mempool_swap_detected"), EventCategory.PRICING)
        self.assertEqual(categorize_event("mempool_preconfirm_cancel_below_trigger"), EventCategory.PRICING)

    def test_tibet_protection_offer_events(self):
        self.assertEqual(categorize_event("defensive_cancel_start"), EventCategory.OFFER)
        self.assertEqual(categorize_event("mempool_defensive_cancel_done"), EventCategory.OFFER)
        self.assertEqual(categorize_event("mempool_preconfirm_defensive_cancel_done"), EventCategory.OFFER)
        self.assertEqual(categorize_event("mempool_preconfirm_cancel_deferred_pending_cancel_settle"), EventCategory.OFFER)
        self.assertEqual(categorize_event("emergency_requote_waiting_for_cancel_settle"), EventCategory.OFFER)
        self.assertEqual(categorize_event("pending_cancel_settle_retry_queued"), EventCategory.OFFER)

    def test_wallet_events(self):
        self.assertEqual(categorize_event("wallet_sync"), EventCategory.WALLET)
        self.assertEqual(categorize_event("chia_unhealthy"), EventCategory.WALLET)

    def test_exchange_events(self):
        self.assertEqual(categorize_event("dexie_flush_result"), EventCategory.EXCHANGE)
        self.assertEqual(categorize_event("splash_repost_done"), EventCategory.EXCHANGE)

    def test_risk_events(self):
        self.assertEqual(categorize_event("circuit_breaker"), EventCategory.RISK)
        self.assertEqual(categorize_event("recovery_mode_enter"), EventCategory.RISK)

    def test_system_events(self):
        self.assertEqual(categorize_event("db_migration"), EventCategory.SYSTEM)
        self.assertEqual(categorize_event("config_error"), EventCategory.SYSTEM)

    def test_coin_events(self):
        self.assertEqual(categorize_event("coin_prep_started"), EventCategory.COIN)
        self.assertEqual(categorize_event("topup_trigger"), EventCategory.COIN)

    def test_unknown_event_defaults_to_system(self):
        self.assertEqual(categorize_event("totally_unknown_event"), EventCategory.SYSTEM)

    def test_new_preflight_events_mapped(self):
        self.assertEqual(categorize_event("preflight_run"), EventCategory.LIFECYCLE)
        self.assertEqual(categorize_event("preflight_blocked"), EventCategory.LIFECYCLE)

    def test_reservation_events_mapped(self):
        self.assertEqual(categorize_event("reservation_acquired"), EventCategory.COIN)
        self.assertEqual(categorize_event("reservation_released"), EventCategory.COIN)
        self.assertEqual(categorize_event("reservation_expired"), EventCategory.COIN)

    def test_offer_lifecycle_event_mapped(self):
        self.assertEqual(categorize_event("offer_lifecycle_transition"), EventCategory.OFFER)

    def test_get_category_map_returns_dict(self):
        m = get_category_map()
        self.assertIsInstance(m, dict)
        self.assertGreater(len(m), 50)
        self.assertIn("bot_starting", m)

    def test_categories_are_strings(self):
        """Categories should be usable as plain strings."""
        self.assertEqual(str(EventCategory.LIFECYCLE), "lifecycle")
        self.assertEqual(str(EventCategory.OFFER), "offer")


if __name__ == "__main__":
    unittest.main()
