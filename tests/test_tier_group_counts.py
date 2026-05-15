import importlib
import sys
import types
import unittest
from decimal import Decimal


class TierGroupCountTests(unittest.TestCase):
    def setUp(self):
        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            TIER_ENABLED=True,
            # Legacy keys (kept for backward compat — new code reads BUY_/SELL_ variants)
            INNER_TIER_COUNT=1,
            MID_TIER_COUNT=2,
            OUTER_TIER_COUNT=1,
            EXTREME_TIER_COUNT=0,
            INNER_TIER_SPARE_COUNT=0,
            MID_TIER_SPARE_COUNT=0,
            OUTER_TIER_SPARE_COUNT=0,
            EXTREME_TIER_SPARE_COUNT=0,
            # Per-side tier counts (current config style — required by get_tier_distribution)
            BUY_INNER_TIER_COUNT=1,
            BUY_MID_TIER_COUNT=2,
            BUY_OUTER_TIER_COUNT=1,
            BUY_EXTREME_TIER_COUNT=0,
            SELL_INNER_TIER_COUNT=1,
            SELL_MID_TIER_COUNT=2,
            SELL_OUTER_TIER_COUNT=1,
            SELL_EXTREME_TIER_COUNT=0,
            # Per-side spare counts
            BUY_INNER_TIER_SPARE_COUNT=0,
            BUY_MID_TIER_SPARE_COUNT=0,
            BUY_OUTER_TIER_SPARE_COUNT=0,
            BUY_EXTREME_TIER_SPARE_COUNT=0,
            SELL_INNER_TIER_SPARE_COUNT=0,
            SELL_MID_TIER_SPARE_COUNT=0,
            SELL_OUTER_TIER_SPARE_COUNT=0,
            SELL_EXTREME_TIER_SPARE_COUNT=0,
            # Offer sizes
            INNER_SIZE_XCH=Decimal("1.0"),
            MID_SIZE_XCH=Decimal("0.5"),
            OUTER_SIZE_XCH=Decimal("0.25"),
            EXTREME_SIZE_XCH=Decimal("0.1"),
            BUY_INNER_SIZE_XCH=Decimal("1.0"),
            BUY_MID_SIZE_XCH=Decimal("0.5"),
            BUY_OUTER_SIZE_XCH=Decimal("0.25"),
            BUY_EXTREME_SIZE_XCH=Decimal("0.1"),
            SELL_INNER_SIZE_XCH=Decimal("1.0"),
            SELL_MID_SIZE_XCH=Decimal("0.5"),
            SELL_OUTER_SIZE_XCH=Decimal("0.25"),
            SELL_EXTREME_SIZE_XCH=Decimal("0.1"),
            BUY_LADDER_REVERSED=False,
            WALLET_ID_XCH=1,
        )
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.log_event = lambda *args, **kwargs: None
        fake_database.add_offer = lambda *args, **kwargs: None
        fake_database.update_offer_status = lambda *args, **kwargs: None
        fake_database.update_offer_lifecycle_state = lambda *args, **kwargs: None
        fake_database.transition_offer = lambda *args, **kwargs: None
        fake_database.mark_cancel_attempted = lambda *args, **kwargs: None
        fake_database.update_offer_coin_id = lambda *args, **kwargs: None
        fake_database.update_offer_bech32 = lambda *args, **kwargs: None
        fake_database.get_open_offers = lambda *args, **kwargs: []
        fake_database.get_offer = lambda *args, **kwargs: None
        fake_database.lock_coin = lambda *args, **kwargs: None
        sys.modules["database"] = fake_database

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.create_offer = lambda *args, **kwargs: {"success": True}
        fake_wallet.cancel_offer = lambda *args, **kwargs: {"success": True}
        fake_wallet.cancel_offers_batch = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_all_offers = lambda *args, **kwargs: []
        fake_wallet.classify_offers_from_list = lambda *args, **kwargs: {}
        fake_wallet.is_offer_time_expired = lambda *args, **kwargs: False
        fake_wallet.get_offer_expiry_info = lambda *args, **kwargs: {}
        fake_wallet.get_offer_bech32 = lambda *args, **kwargs: ""
        fake_wallet.cleanup_expired_offers = lambda *args, **kwargs: None
        fake_wallet.get_exact_spendable_coins_rpc = lambda *args, **kwargs: {
            "success": True,
            "records": [],
        }
        fake_wallet.get_wallet_type = lambda: "sage"
        fake_wallet.get_owned_coins_detailed = lambda *args, **kwargs: {}
        fake_wallet.WALLET_ID_XCH = 1
        fake_wallet.get_all_coins_for_wallet = lambda *args, **kwargs: []
        fake_wallet.get_wallet_balance = lambda *args, **kwargs: {
            "wallet_balance": {"spendable_balance": 0}
        }
        fake_wallet.get_next_address = lambda *args, **kwargs: {
            "success": True,
            "address": "xch1test",
        }
        fake_wallet.send_transaction = lambda *args, **kwargs: {"success": True}
        fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_owned_coins = lambda *args, **kwargs: {}
        sys.modules["wallet"] = fake_wallet

        fake_tx_fees = types.ModuleType("tx_fees")
        fake_tx_fees.fee_pool_enabled = lambda: False
        fake_tx_fees.get_effective_transaction_fee_mojos = lambda: 0
        fake_tx_fees.get_fee_coin_size_mojos = lambda: 0
        fake_tx_fees.get_fee_coin_size_xch = lambda: Decimal("0")
        fake_tx_fees.get_fee_pool_count = lambda: 0
        fake_tx_fees.get_fee_tier_name = lambda: "fees"
        sys.modules["tx_fees"] = fake_tx_fees

        fake_win_subprocess = types.ModuleType("win_subprocess")
        fake_win_subprocess.hidden_subprocess_kwargs = lambda: {}
        sys.modules["win_subprocess"] = fake_win_subprocess

        for module_name in ["coin_manager", "offer_manager"]:
            sys.modules.pop(module_name, None)

        self.coin_manager = importlib.import_module("coin_manager")
        self.offer_manager = importlib.import_module("offer_manager")

    def tearDown(self):
        for name in [
            "coin_manager",
            "offer_manager",
            "wallet",
            "database",
            "config",
            "tx_fees",
            "win_subprocess",
        ]:
            sys.modules.pop(name, None)

    def test_coin_manager_distribution_uses_configured_template(self):
        dist = self.coin_manager.get_tier_distribution(6)
        self.assertEqual(
            dist,
            {"inner": 1, "mid": 2, "outer": 1, "extreme": 2},
        )

    def test_coin_manager_distribution_truncates_outer_tiers_for_smaller_ladder(self):
        dist = self.coin_manager.get_tier_distribution(3)
        self.assertEqual(
            dist,
            {"inner": 1, "mid": 2, "outer": 0, "extreme": 0},
        )

    def test_offer_manager_classifies_slots_from_configured_template(self):
        manager = self.offer_manager.OfferManager()
        tiers = [manager._classify_tier(slot, 6) for slot in range(6)]
        self.assertEqual(
            tiers,
            ["inner", "mid", "mid", "outer", "extreme", "extreme"],
        )

    def test_weighted_prep_counts_preserve_total_multiplier_budget(self):
        counts = self.coin_manager.get_weighted_tier_prep_counts(6, 3.0)
        self.assertEqual(sum(counts.values()), 24)

    def test_weighted_prep_counts_clamps_multiplier_floor_to_1(self):
        # _clamp_coin_prep_multiplier floors at 1.0 so 0.5 → 1.0 spare layer.
        # With 6 base slots and multiplier clamped to 1.0:
        #   spare_budget = round(6 * 1.0) = 6 → total = 12
        counts = self.coin_manager.get_weighted_tier_prep_counts(6, 0.5)
        self.assertEqual(sum(counts.values()), 12)

    def test_weighted_prep_counts_bias_spares_toward_larger_tiers(self):
        dist = self.coin_manager.get_tier_distribution(6)
        counts = self.coin_manager.get_weighted_tier_prep_counts(6, 3.0)
        spares = {tier: counts[tier] - dist[tier] for tier in dist}

        self.assertGreater(spares["inner"], spares["outer"])
        self.assertGreater(spares["mid"], spares["extreme"])
        self.assertGreaterEqual(counts["inner"], dist["inner"])
        self.assertGreaterEqual(counts["extreme"], dist["extreme"])

    def test_recommended_spare_counts_return_spare_portion_only(self):
        dist = self.coin_manager.get_tier_distribution(6)
        recommended = self.coin_manager.get_recommended_tier_spare_counts(6, 3.0)
        prepared = self.coin_manager.get_weighted_tier_prep_counts(
            6, 3.0, spare_counts={}
        )

        self.assertEqual(
            sum(recommended.values()),
            sum(prepared.values()) - sum(dist.values()),
        )
        self.assertGreater(recommended["inner"], recommended["extreme"])

    def test_explicit_spare_counts_override_weighted_recommendation(self):
        counts = self.coin_manager.get_weighted_tier_prep_counts(
            6,
            3.0,
            spare_counts={"inner": 3, "mid": 2, "outer": 1, "extreme": 0},
        )
        self.assertEqual(
            counts,
            {"inner": 4, "mid": 4, "outer": 2, "extreme": 2},
        )


if __name__ == "__main__":
    unittest.main()
