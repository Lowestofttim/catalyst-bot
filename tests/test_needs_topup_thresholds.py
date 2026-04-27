"""Tests for needs_topup() threshold computation.

Validates that the TIER_TRIGGER_PCT_* percentages from .env produce exactly
the intended trigger points for both reversed and non-reversed buy ladders.

Config under test (.env values):
    TIER_TRIGGER_PCT_INNER   = 40   → inner fires when 3 spares remain (10-pool)
    TIER_TRIGGER_PCT_MID     = 60   → mid fires when 2 spares remain  (5-pool)
    TIER_TRIGGER_PCT_OUTER   = 25   → outer fires when 0 spares remain (3-pool)
    TIER_TRIGGER_PCT_EXTREME = 15   → extreme fires when 0 spares remain (2-pool)

Spare pools (slot-position space, from Smart Settings moderate fill rate):
    inner=10, mid=5, outer=3, extreme=2

Non-reversed (BUY_LADDER_REVERSED=False):
    coin-size == slot-position, so inner COIN uses inner POSITION trigger (40%).

Reversed (BUY_LADDER_REVERSED=True):
    inner COIN serves EXTREME position (rare fill, 15% trigger)
    extreme COIN serves INNER position (high fill, 40% trigger)
    Spare pool in coin-size space after flip: inner=2, mid=3, outer=5, extreme=10
"""

import importlib
import sys
import time
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


def _build_fake_config(reversed_ladder: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        TIER_ENABLED=True,
        BUY_LADDER_REVERSED=reversed_ladder,

        # Slot counts per tier (both sides symmetric for these tests)
        BUY_INNER_TIER_COUNT=3,
        BUY_MID_TIER_COUNT=4,
        BUY_OUTER_TIER_COUNT=3,
        BUY_EXTREME_TIER_COUNT=2,
        SELL_INNER_TIER_COUNT=3,
        SELL_MID_TIER_COUNT=4,
        SELL_OUTER_TIER_COUNT=3,
        SELL_EXTREME_TIER_COUNT=2,
        # Legacy fallback keys
        INNER_TIER_COUNT=3,
        MID_TIER_COUNT=4,
        OUTER_TIER_COUNT=3,
        EXTREME_TIER_COUNT=2,

        # Spare counts in slot-POSITION space.
        # flip_position_tiers_to_coin_size_tiers() flips these for reversed buy.
        BUY_INNER_TIER_SPARE_COUNT=10,
        BUY_MID_TIER_SPARE_COUNT=5,
        BUY_OUTER_TIER_SPARE_COUNT=3,
        BUY_EXTREME_TIER_SPARE_COUNT=2,
        SELL_INNER_TIER_SPARE_COUNT=10,
        SELL_MID_TIER_SPARE_COUNT=5,
        SELL_OUTER_TIER_SPARE_COUNT=3,
        SELL_EXTREME_TIER_SPARE_COUNT=2,
        INNER_TIER_SPARE_COUNT=10,
        MID_TIER_SPARE_COUNT=5,
        OUTER_TIER_SPARE_COUNT=3,
        EXTREME_TIER_SPARE_COUNT=2,

        # Trigger percentages — exactly the values written to .env
        TIER_TRIGGER_PCT_INNER=40,
        TIER_TRIGGER_PCT_MID=60,
        TIER_TRIGGER_PCT_OUTER=25,
        TIER_TRIGGER_PCT_EXTREME=15,
        TIER_TRIGGER_PCT_SNIPER=40,
        TIER_TRIGGER_PCT_FEES=30,
        # Disable pace scaling so pace_scale is always 1.0 (deterministic tests)
        TIER_TRIGGER_PACE_SCALE=False,

        # Offer sizes
        INNER_SIZE_XCH=Decimal("3.5"),
        MID_SIZE_XCH=Decimal("1.75"),
        OUTER_SIZE_XCH=Decimal("0.875"),
        EXTREME_SIZE_XCH=Decimal("0.35"),
        BUY_INNER_SIZE_XCH=Decimal("3.5"),
        BUY_MID_SIZE_XCH=Decimal("1.75"),
        BUY_OUTER_SIZE_XCH=Decimal("0.875"),
        BUY_EXTREME_SIZE_XCH=Decimal("0.35"),
        SELL_INNER_SIZE_XCH=Decimal("3.5"),
        SELL_MID_SIZE_XCH=Decimal("1.75"),
        SELL_OUTER_SIZE_XCH=Decimal("0.875"),
        SELL_EXTREME_SIZE_XCH=Decimal("0.35"),

        COIN_PREP_MULTIPLIER=Decimal("2.0"),
        MAX_ACTIVE_BUY_OFFERS=12,
        MAX_ACTIVE_SELL_OFFERS=12,
        ENABLE_BUY=True,
        ENABLE_SELL=True,
        ENABLE_COIN_PREP=True,

        # Disable optional pools so tests focus purely on tier thresholds
        SNIPER_ENABLED=False,
        SNIPER_PREP_COUNT=0,
        SNIPER_SIZE_XCH="0",

        WALLET_FINGERPRINT="",
        WALLET_ID_XCH=1,
        CAT_WALLET_ID=2,
        CAT_DECIMALS=3,
        XCH_RESERVE=Decimal("0"),
        XCH_TARGET_COINS=50,
        CAT_TARGET_COINS=50,
    )


class NeedsTopupThresholdTests(unittest.TestCase):
    """Parametric threshold tests — one class handles both ladder modes.

    Sub-classes set `REVERSED` to control BUY_LADDER_REVERSED.
    The base class supplies all helper methods and assertion utilities.
    """

    REVERSED: bool = False  # overridden in sub-classes

    def setUp(self):
        self._ns = _build_fake_config(self.REVERSED)

        fake_config = types.ModuleType("config")
        fake_config.cfg = self._ns
        sys.modules["config"] = fake_config

        fake_db = types.ModuleType("database")
        fake_db.log_event = lambda *a, **kw: None
        fake_db.get_tier_spare_counts = lambda *a, **kw: {}
        fake_db.get_current_pace = lambda: "normal"
        fake_db.add_offer = lambda *a, **kw: None
        fake_db.update_offer_status = lambda *a, **kw: None
        fake_db.get_open_offers = lambda *a, **kw: []
        fake_db.get_offer = lambda *a, **kw: None
        fake_db.lock_coin = lambda *a, **kw: None
        sys.modules["database"] = fake_db

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.create_offer = lambda *a, **kw: {"success": True}
        fake_wallet.cancel_offer = lambda *a, **kw: {"success": True}
        fake_wallet.get_all_offers = lambda *a, **kw: []
        fake_wallet.get_exact_spendable_coins_rpc = lambda *a, **kw: {"success": True, "records": []}
        fake_wallet.get_wallet_type = lambda: "sage"
        fake_wallet.get_owned_coins_detailed = lambda *a, **kw: {}
        fake_wallet.WALLET_ID_XCH = 1
        fake_wallet.get_all_coins_for_wallet = lambda *a, **kw: []
        fake_wallet.get_wallet_balance = lambda *a, **kw: {"wallet_balance": {"spendable_balance": 0}}
        fake_wallet.get_next_address = lambda *a, **kw: {"success": True, "address": "xch1test"}
        fake_wallet.send_transaction = lambda *a, **kw: {"success": True}
        fake_wallet.split_coins_rpc = lambda *a, **kw: {"success": True}
        fake_wallet.get_owned_coins = lambda *a, **kw: {}
        sys.modules["wallet"] = fake_wallet

        fake_tx_fees = types.ModuleType("tx_fees")
        fake_tx_fees.fee_pool_enabled = lambda: False
        fake_tx_fees.get_effective_transaction_fee_mojos = lambda: 0
        fake_tx_fees.get_fee_coin_size_mojos = lambda: 0
        fake_tx_fees.get_fee_coin_size_xch = lambda: Decimal("0")
        fake_tx_fees.get_fee_pool_count = lambda: 0
        fake_tx_fees.get_fee_tier_name = lambda: "fees"
        sys.modules["tx_fees"] = fake_tx_fees

        fake_wsub = types.ModuleType("win_subprocess")
        fake_wsub.hidden_subprocess_kwargs = lambda: {}
        sys.modules["win_subprocess"] = fake_wsub

        sys.modules.pop("coin_manager", None)
        self.cm = importlib.import_module("coin_manager")

    def tearDown(self):
        for name in ["coin_manager", "wallet", "database", "config",
                     "tx_fees", "win_subprocess"]:
            sys.modules.pop(name, None)

    # ── helper ──────────────────────────────────────────────────────────────

    def _manager(self, xch_overrides=None, cat_overrides=None):
        """Build a CoinManager with controlled spare state.

        Default spares are 100 for every tier (well above all thresholds).
        Pass xch_overrides / cat_overrides to lower specific tiers.
        """
        with patch.object(self.cm.CoinManager, "_resolve_fingerprint",
                          return_value="123456789"):
            mgr = self.cm.CoinManager()

        safe = {"inner": 100, "mid": 100, "outer": 100,
                "extreme": 100, "sniper": 0, "fees": 0}
        xch = {**safe, **(xch_overrides or {})}
        cat = {**safe, **(cat_overrides or {})}
        mgr._tier_spares = {"xch": xch, "cat": cat}
        mgr._last_topup_time = 0      # past the 600 s cooldown
        mgr._no_coins_backoff = False
        return mgr

    def _assert_fires(self, tier, count, msg=None):
        """Assert that needs_topup() returns True when a given XCH tier is low."""
        mgr = self._manager(xch_overrides={tier: count})
        self.assertTrue(
            mgr.needs_topup(),
            msg or f"Expected topup to fire: xch[{tier}]={count}")

    def _assert_silent(self, tier, count, msg=None):
        """Assert that needs_topup() returns False for the emergency path.

        Blocks the drip path (sets _last_drip_time to now) so the test
        focuses purely on the emergency threshold logic.
        """
        mgr = self._manager(xch_overrides={tier: count})
        mgr._last_drip_time = time.time()  # suppress drip path for this check
        self.assertFalse(
            mgr.needs_topup(),
            msg or f"Expected no topup: xch[{tier}]={count}")

    def test_no_trip_all_above_threshold(self):
        """All tiers at 100 spares — no topup."""
        self.assertFalse(self._manager().needs_topup())

    def test_cooldown_blocks_low_inner(self):
        """Recent topup+drip suppresses trigger even when inner=0."""
        mgr = self._manager(xch_overrides={"inner": 0})
        mgr._last_topup_time = time.time()
        mgr._last_drip_time = time.time()  # block drip path too
        self.assertFalse(mgr.needs_topup(), "cooldown should block topup")

    def test_drip_ready_does_not_bypass_emergency_cooldown(self):
        """A ready drip timer must not re-run emergency topup while it cools down."""
        mgr = self._manager(xch_overrides={"inner": 3})
        mgr._last_topup_time = time.time()  # emergency path on cooldown
        mgr._last_drip_time = 0             # drip path ready
        self._ns.TIER_DRIP_PCT = 5          # keep this focused on emergency gating
        self.assertFalse(
            mgr.needs_topup(),
            "drip readiness should not bypass the emergency topup cooldown",
        )


# ────────────────────────────────────────────────────────────────────────────
# Non-reversed ladder (BUY_LADDER_REVERSED=False)
#
# coin-size tier == slot-position tier for XCH side.
#
# Computed thresholds:
#   inner:   spare_target=10, 40% → threshold=4  → fires when ≤3
#   mid:     spare_target=5,  60% → threshold=3  → fires when ≤2
#   outer:   spare_target=3,  25% → threshold=1  → fires when  0
#   extreme: spare_target=2,  15% → threshold=1  → fires when  0
# ────────────────────────────────────────────────────────────────────────────

class TestNonReversed(NeedsTopupThresholdTests):
    REVERSED = False

    # inner: threshold = max(1, round(10 * 0.40)) = 4
    def test_inner_fires_at_3_spares(self):
        self._assert_fires("inner", 3, "inner=3 < threshold(4): should fire")

    def test_inner_silent_at_4_spares(self):
        self._assert_silent("inner", 4, "inner=4 == threshold(4): should NOT fire")

    def test_inner_silent_at_10_spares(self):
        self._assert_silent("inner", 10, "inner=10 (full pool): should NOT fire")

    # mid: threshold = max(1, round(5 * 0.60)) = 3
    def test_mid_fires_at_2_spares(self):
        self._assert_fires("mid", 2, "mid=2 < threshold(3): should fire")

    def test_mid_silent_at_3_spares(self):
        self._assert_silent("mid", 3, "mid=3 == threshold(3): should NOT fire")

    # outer: threshold = max(1, round(3 * 0.25)) = max(1, 1) = 1
    def test_outer_fires_at_0_spares(self):
        self._assert_fires("outer", 0, "outer=0 < threshold(1): should fire")

    def test_outer_silent_at_1_spare(self):
        self._assert_silent("outer", 1, "outer=1 == threshold(1): should NOT fire")

    # extreme: threshold = max(1, round(2 * 0.15)) = max(1, 0) = 1
    def test_extreme_fires_at_0_spares(self):
        self._assert_fires("extreme", 0, "extreme=0 < threshold(1): should fire")

    def test_extreme_silent_at_1_spare(self):
        self._assert_silent("extreme", 1, "extreme=1 == threshold(1): should NOT fire")


# ────────────────────────────────────────────────────────────────────────────
# Reversed ladder (BUY_LADDER_REVERSED=True)
#
# Coin-size ↔ slot-position are swapped for XCH/buy side.
# inner COIN  → extreme POSITION (rare fill)    → 15% trigger
# mid COIN    → outer  POSITION  (low fill)     → 25% trigger
# outer COIN  → mid    POSITION  (medium fill)  → 60% trigger
# extreme COIN→ inner  POSITION  (high fill)    → 40% trigger
#
# Spare pools in COIN-SIZE space after the flip
# (position space inner=10,mid=5,outer=3,extreme=2):
#   inner coin:   spare_target=2   (was extreme-pos spare)
#   mid coin:     spare_target=3   (was outer-pos spare)
#   outer coin:   spare_target=5   (was mid-pos spare)
#   extreme coin: spare_target=10  (was inner-pos spare)
#
# Thresholds:
#   inner coin:   max(1, round(2  * 0.15)) = max(1, 0) = 1  → fires at 0
#   mid coin:     max(1, round(3  * 0.25)) = max(1, 1) = 1  → fires at 0
#   outer coin:   max(1, round(5  * 0.60)) = 3              → fires at ≤2
#   extreme coin: max(1, round(10 * 0.40)) = 4              → fires at ≤3
# ────────────────────────────────────────────────────────────────────────────

class TestReversed(NeedsTopupThresholdTests):
    REVERSED = True

    # extreme COIN → inner POSITION (40%); spare_target=10 → threshold=4
    def test_extreme_coin_fires_at_3_spares(self):
        self._assert_fires("extreme", 3,
            "extreme coin=3 (serves inner position), threshold=4: should fire")

    def test_extreme_coin_silent_at_4_spares(self):
        self._assert_silent("extreme", 4,
            "extreme coin=4 == threshold(4): should NOT fire")

    def test_extreme_coin_silent_at_10_spares(self):
        self._assert_silent("extreme", 10,
            "extreme coin=10 (full pool): should NOT fire")

    # outer COIN → mid POSITION (60%); spare_target=5 → threshold=3
    def test_outer_coin_fires_at_2_spares(self):
        self._assert_fires("outer", 2,
            "outer coin=2 (serves mid position), threshold=3: should fire")

    def test_outer_coin_silent_at_3_spares(self):
        self._assert_silent("outer", 3,
            "outer coin=3 == threshold(3): should NOT fire")

    # mid COIN → outer POSITION (25%); spare_target=3 → threshold=1
    def test_mid_coin_fires_at_0_spares(self):
        self._assert_fires("mid", 0,
            "mid coin=0 (serves outer position), threshold=1: should fire")

    def test_mid_coin_silent_at_1_spare(self):
        self._assert_silent("mid", 1,
            "mid coin=1 == threshold(1): should NOT fire")

    # inner COIN → extreme POSITION (15%); spare_target=2 → threshold=1
    def test_inner_coin_fires_at_0_spares(self):
        self._assert_fires("inner", 0,
            "inner coin=0 (serves extreme position), threshold=1: should fire")

    def test_inner_coin_silent_at_1_spare(self):
        self._assert_silent("inner", 1,
            "inner coin=1 == threshold(1): should NOT fire")

    def test_reversed_mapping_sanity(self):
        """Confirm extreme-coin threshold is higher than inner-coin threshold.

        With the reversed ladder the HIGHEST threshold (fires earliest) must
        be on the LARGEST-fill position — which with reversal uses EXTREME coins.
        Inner coins serve the rarest-fill position and should have the LOWEST
        threshold (only fires at 0).
        """
        # extreme coin: threshold=4 — needs 4+ spares to stay quiet
        self._assert_silent("extreme", 4)
        # inner coin: threshold=1 — stays quiet as long as ≥1 spare exists
        self._assert_silent("inner", 1)
        # extreme coin at 3 fires; inner coin at 3 does not
        mgr_extreme_low = self._manager(xch_overrides={"extreme": 3})
        mgr_inner_low   = self._manager(xch_overrides={"inner": 3})
        self.assertTrue(mgr_extreme_low.needs_topup(),
                        "extreme=3 should fire (serves busy inner position)")
        self.assertFalse(mgr_inner_low.needs_topup(),
                         "inner=3 should NOT fire (serves quiet extreme position)")

    def test_topup_worker_prioritizes_floor_nearest_coin_pool(self):
        """When multiple reversed-buy XCH pools are low, refill the pool that
        serves the nearest-floor slot first.

        Reversed buy maps the inner slot position onto the extreme coin-size
        pool, so the topup worker should try XCH-extreme before XCH-outer when
        both are below their action thresholds.
        """
        mgr = self._manager()

        xch_inv = {
            "reserve": [{"coin": {"amount": 100_000_000_000_000, "name": "reserve"}}],
            "inner": [{}] * 2,
            "mid": [{}] * 3,
            "outer": [{}] * 2,
            "extreme": [{}] * 3,
            "small": [],
        }
        cat_inv = {
            "reserve": [],
            "inner": [{}] * 20,
            "mid": [{}] * 20,
            "outer": [{}] * 20,
            "extreme": [{}] * 20,
            "small": [],
        }
        active_counts = {"inner": 2, "mid": 3, "outer": 5, "extreme": 10}
        prepared_counts = {"inner": 4, "mid": 6, "outer": 10, "extreme": 20}
        calls = []

        def fake_smart_topup(name, *_args, **_kwargs):
            calls.append(name)
            return True

        with patch.object(mgr, "_absorb_misfits_to_reserve", return_value=False), \
             patch.object(mgr, "_classify_coins_by_designation", side_effect=[xch_inv, cat_inv]), \
             patch.object(mgr, "_get_tier_sizes_mojos", return_value={
                 "inner": 3_500_000_000_000,
                 "mid": 1_750_000_000_000,
                 "outer": 875_000_000_000,
                 "extreme": 350_000_000_000,
             }), \
             patch.object(mgr, "_configured_tier_sizes_xch", return_value={
                 "inner": Decimal("3.5"),
                 "mid": Decimal("1.75"),
                 "outer": Decimal("0.875"),
                 "extreme": Decimal("0.35"),
             }), \
             patch.object(mgr, "get_trading_pace", return_value="normal"), \
             patch.object(mgr, "_smart_topup_wallet", side_effect=fake_smart_topup), \
             patch.object(mgr, "update_coin_counts"), \
             patch.object(mgr, "log_inventory"), \
             patch.object(self.cm, "_get_free_coins_rpc", return_value={
                 "confirmed_records": [{"coin": {"amount": 1, "name": "dummy"}}]
             }), \
             patch.object(self.cm, "get_tier_distribution", return_value=active_counts), \
             patch.object(self.cm, "get_weighted_tier_prep_counts", return_value=prepared_counts):
            mgr._topup_worker(active_buy=12, active_sell=12)

        self.assertEqual(calls[:1], ["XCH-extreme"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
