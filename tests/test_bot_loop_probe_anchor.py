import sys
import types
import unittest
import io
import contextlib
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from reaction_strategy import RequoteSeverity


class _FakeCfg:
    DRY_RUN = False
    ENABLE_BUY = True
    ENABLE_SELL = True
    AUTO_REQUOTE = True
    DEXIE_AUTO_POST = True
    SPLASH_ENABLED = True
    COIN_IDS_ENABLED = True
    CAT_ASSET_ID = "test-cat"
    CAT_DECIMALS = 3
    CAT_TICKER_ID = "MZ"
    MAX_ACTIVE_BUY_OFFERS = 4
    MAX_ACTIVE_SELL_OFFERS = 4
    MIN_EDGE_BPS = Decimal("300")
    ARB_ALERT_THRESHOLD_BPS = Decimal("200")
    SNIPER_CONFIRM_SECS = 30
    SNIPER_LINGER_SECS = 600
    SNIPER_POLL_SECS = 5
    SNIPER_TOP_BOOK_BPS = Decimal("1")
    SNIPER_RETRY_BACKOFF_BPS = Decimal("50")
    SNIPER_MAIN_BOOK_GUARD_BPS = Decimal("1")
    ADAPTIVE_LADDER_TARGETS = True
    DB_ONLY_OFFER_CONFIRM_GRACE_SECS = 90
    DB_ONLY_OFFER_RETRY_BACKOFF_SECS = 300


fake_config = types.ModuleType("config")
fake_config.cfg = _FakeCfg()
_ORIGINAL_MODULES = {
    "config": sys.modules.get("config"),
    "requests": sys.modules.get("requests"),
    "database": sys.modules.get("database"),
    "price_engine": sys.modules.get("price_engine"),
    "offer_manager": sys.modules.get("offer_manager"),
    "fill_tracker": sys.modules.get("fill_tracker"),
    "dexie_manager": sys.modules.get("dexie_manager"),
    "splash_manager": sys.modules.get("splash_manager"),
    "splash_node": sys.modules.get("splash_node"),
    "coinset_client": sys.modules.get("coinset_client"),
    "coin_manager": sys.modules.get("coin_manager"),
    "risk_manager": sys.modules.get("risk_manager"),
    "sniper": sys.modules.get("sniper"),
    "boost_manager": sys.modules.get("boost_manager"),
    "market_intel": sys.modules.get("market_intel"),
    "wallet": sys.modules.get("wallet"),
    "amm_monitor": sys.modules.get("amm_monitor"),
    "runtime_monitor": sys.modules.get("runtime_monitor"),
    "splash_receive": sys.modules.get("splash_receive"),
    "bot_loop": sys.modules.get("bot_loop"),
}
sys.modules["config"] = fake_config

fake_requests = types.ModuleType("requests")
fake_requests.Session = object
sys.modules["requests"] = fake_requests

fake_database = types.ModuleType("database")
fake_database.init_database = lambda: None
fake_database.log_event = lambda *args, **kwargs: None
fake_database.get_events_since = lambda *args, **kwargs: []
fake_database.get_open_offers = lambda *args, **kwargs: []
fake_database.get_stats = lambda: {}
fake_database.get_offer = lambda tid, *args, **kwargs: {"dexie_id": f"dexie-{tid}"}
fake_database.update_offer_status = lambda *args, **kwargs: True
fake_database.backfill_verified_fills_from_offers = lambda *args, **kwargs: 0
sys.modules["database"] = fake_database


class _DummyPriceEngine:
    def get_price(self, *args, **kwargs):
        return {"mid_price": Decimal("1.10")}


class _DummyOfferManager:
    def __init__(self):
        self.create_calls = []
        self.requote_calls = []
        self._recently_created = {}
        self._offer_details_cache = {}

    def get_recently_created_count(self, side):
        return 0

    def get_replenishment_slots(
        self, side, total_slots, cat_asset_id=None, live_offer_ids=None
    ):
        return list(range(total_slots))

    def create_ladder(self, mid_price, side, **kwargs):
        self.create_calls.append((side, mid_price, kwargs))
        return []

    def should_requote(self, side, current_price, last_quoted_price):
        return True

    def should_requote_graduated(self, side, current_price, last_price):
        return RequoteSeverity.FULL

    def requote_side(self, side, current_price, **kwargs):
        self.requote_calls.append((side, current_price, kwargs))
        return {"offers": [], "fully_replaced": True}

    def get_suspended_slot_count(self, side):
        return 0

    def unsuspend_slots_if_coins_available(self, side):
        pass


class _DummyFillTracker:
    def __init__(self, offer_manager=None):
        self.offer_manager = offer_manager

    def should_protect_side(self, side):
        return False


class _DummyDexieManager:
    def __init__(self):
        self._queue = []
        self.direct_posts = []

    def queue_post(self, *args, **kwargs):
        pass

    def flush_queue(self):
        return {"posted": 0, "failed": 0, "skipped": 0}

    def _post_single(self, *args, **kwargs):
        self.direct_posts.append((args, kwargs))
        return {"success": True}


class _DummySplashManager:
    def __init__(self):
        self._queue = []
        self.direct_posts = []

    def queue_post(self, *args, **kwargs):
        pass

    def flush_queue(self):
        return {"posted": 0, "failed": 0, "skipped": 0}

    def _post_single(self, *args, **kwargs):
        self.direct_posts.append((args, kwargs))
        return {"success": True}


class _DummySplashNode:
    pass


class _DummyCoinsetClient:
    pass


class _DummyCoinManager:
    def __init__(self):
        self._price_engine = None
        self.fee_pool = None  # Required by bot_loop.py BotLoop.__init__

    def is_busy(self):
        return False

    def snapshot_coins(self, reason):
        pass

    def get_status(self):
        return {"inventory": {}}


class _DummyRiskManager:
    def __init__(self, price_engine=None, market_intel=None):
        self._boost_manager = None

    def should_enable_side(self, side, mid_price):
        return True

    def get_adjusted_spread(self, side):
        return Decimal("0.08")

    def update_arb_gap(self, arb_gap):
        pass


class _DummySniper:
    def __init__(
        self,
        offer_manager=None,
        risk_manager=None,
        dexie_manager=None,
        splash_manager=None,
    ):
        self._active_snipe_ids = []
        self._active_snipe_sides = {}


class _DummyBoostManager:
    def __init__(
        self,
        offer_manager=None,
        dexie_manager=None,
        risk_manager=None,
        splash_manager=None,
    ):
        del offer_manager, dexie_manager, risk_manager, splash_manager


class _DummyAMMMonitor:
    def __init__(self, price_engine=None):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def is_available(self):
        return False

    def get_amm_price(self):
        return None

    def get_amm_state(self):
        return None

    def get_drift_bps(self):
        return None

    def get_stats(self):
        return {}

    def notify_quoted_price(self, buy=None, sell=None):
        pass

    def check_amm_buffer(self, price, side):
        return True


class _DummyMarketIntel:
    def __init__(self, price_engine=None):
        self.orderbook = {}

    def refresh_orderbook(self, force=False):
        del force
        return dict(self.orderbook)


def _module_with_class(name, cls_name, cls):
    module = types.ModuleType(name)
    setattr(module, cls_name, cls)
    sys.modules[name] = module


_module_with_class("price_engine", "PriceEngine", _DummyPriceEngine)
_module_with_class("offer_manager", "OfferManager", _DummyOfferManager)
_module_with_class("fill_tracker", "FillTracker", _DummyFillTracker)
_module_with_class("dexie_manager", "DexieManager", _DummyDexieManager)
sys.modules["dexie_manager"].get_offer_detail = lambda *args, **kwargs: {"status": 0}
_module_with_class("splash_manager", "SplashManager", _DummySplashManager)
_module_with_class("splash_node", "SplashNode", _DummySplashNode)
_module_with_class("coinset_client", "CoinsetClient", _DummyCoinsetClient)
_module_with_class("coin_manager", "CoinManager", _DummyCoinManager)
_module_with_class("risk_manager", "RiskManager", _DummyRiskManager)
_module_with_class("sniper", "Sniper", _DummySniper)
_module_with_class("boost_manager", "BoostManager", _DummyBoostManager)
_module_with_class("market_intel", "MarketIntel", _DummyMarketIntel)
_module_with_class("amm_monitor", "AMMMonitor", _DummyAMMMonitor)


class _DummyRuntimeMonitor:
    def __init__(self, loop):
        del loop

    def get_state(self):
        return {}


_module_with_class("runtime_monitor", "RuntimeMonitor", _DummyRuntimeMonitor)

fake_wallet = types.ModuleType("wallet")
fake_wallet.get_all_offers = lambda *args, **kwargs: []
fake_wallet.classify_offers_from_list = lambda *args, **kwargs: ([], [], [])
fake_wallet.get_chia_health = lambda *args, **kwargs: {}
fake_wallet.cancel_offer = lambda *args, **kwargs: {}
fake_wallet.get_wallet_type = lambda: "sage"
sys.modules["wallet"] = fake_wallet

fake_splash_receive = types.ModuleType("splash_receive")
fake_splash_receive.classify_offer_for_asset = lambda *args, **kwargs: {}
sys.modules["splash_receive"] = fake_splash_receive

# Pop any cached bot_loop so we re-import it with our fakes rather than getting
# the version already loaded by test_api_local_guard (which imports api_server
# and brings in the full real module stack via sys.modules).
sys.modules.pop("bot_loop", None)
import bot_loop

for _name, _module in _ORIGINAL_MODULES.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


class ProbeAnchorTests(unittest.TestCase):
    def setUp(self):
        # When run alongside other test files (e.g. test_bot_loop_recovery_mode)
        # bot_loop may already be imported with a different cfg object.  Patch
        # bot_loop.cfg so this test file's _FakeCfg values are always active,
        # regardless of import order.
        self._cfg_patcher = patch.object(bot_loop, "cfg", fake_config.cfg)
        self._cfg_patcher.start()

    def tearDown(self):
        self._cfg_patcher.stop()

    def test_watchdog_warning_noise_is_suppressed_between_milestones(self):
        loop = bot_loop.BotLoop()
        loop._watchdog_persistence_threshold = 5
        loop._watchdog_auto_heal_threshold = 10
        loop._watchdog_repeat_warning_interval = 12

        self.assertTrue(
            loop._should_log_watchdog_operator_warning(
                is_error=False,
                streak=5,
            )
        )
        self.assertFalse(
            loop._should_log_watchdog_operator_warning(
                is_error=False,
                streak=6,
            )
        )
        self.assertFalse(
            loop._should_log_watchdog_operator_warning(
                is_error=False,
                streak=9,
            )
        )
        self.assertTrue(
            loop._should_log_watchdog_operator_warning(
                is_error=False,
                streak=10,
            )
        )
        self.assertFalse(
            loop._should_log_watchdog_operator_warning(
                is_error=False,
                streak=11,
            )
        )
        self.assertTrue(
            loop._should_log_watchdog_operator_warning(
                is_error=False,
                streak=22,
            )
        )
        self.assertTrue(
            loop._should_log_watchdog_operator_warning(
                is_error=True,
                streak=6,
            )
        )

    def test_probe_offer_state_marks_dexie_pending_as_taken(self):
        loop = bot_loop.BotLoop()

        with (
            patch.object(bot_loop, "get_offer", return_value={"dexie_id": "dexie-buy"}),
            patch.object(bot_loop, "get_offer_detail", return_value={"status": 1}),
        ):
            state = loop._classify_probe_offer("buy", "probe-buy", {"probe-buy"})

        self.assertFalse(state["confirmable"])
        self.assertTrue(state["taken"])
        self.assertEqual(state["reason"], "dexie_pending")

    def test_confirmed_probe_revalidation_rearms_dexie_pending_edge(self):
        loop = bot_loop.BotLoop()
        loop._running = False
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "confirmed_at": 1000.0,
                "launched_at": 990.0,
                "buy_tid": "probe-buy",
                "sell_tid": "probe-sell",
            }
        )

        calls = []

        def _fake_process(
            current_buy_ids, current_sell_ids, arb_gap, force_refresh=False
        ):
            calls.append(
                (set(current_buy_ids), set(current_sell_ids), arb_gap, force_refresh)
            )
            return {
                "buy_ids": set(current_buy_ids),
                "sell_ids": set(current_sell_ids),
                "sniper_fired": False,
            }

        loop._process_active_probe = _fake_process

        def _fake_get_offer(tid):
            return {"dexie_id": f"dexie-{tid}"}

        def _fake_get_detail(dexie_id, **kwargs):
            del kwargs
            status = 1 if dexie_id == "dexie-probe-buy" else 0
            return {"status": status}

        with (
            patch.object(bot_loop, "get_offer", side_effect=_fake_get_offer),
            patch.object(bot_loop, "get_offer_detail", side_effect=_fake_get_detail),
            patch.object(bot_loop.time, "time", return_value=1010.0),
        ):
            buy_ids, sell_ids, rearmed = loop._revalidate_confirmed_probe_edges(
                {"probe-buy"},
                {"probe-sell"},
                Decimal("125"),
            )

        self.assertEqual(buy_ids, {"probe-buy"})
        self.assertEqual(sell_ids, {"probe-sell"})
        self.assertTrue(rearmed)
        self.assertTrue(loop._probe_state["active"])
        self.assertEqual(loop._probe_state["confirmed_at"], 0)
        self.assertEqual(len(calls), 1)

    def test_pending_probe_does_not_confirm_after_max_attempts(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._probe_state.update(
            {
                "active": True,
                "buy_tid": "probe-buy",
                "sell_tid": "probe-sell",
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
                "tibet_price": Decimal("1.10"),
                "attempt": 5,
                "max_attempts": 5,
                "launched_at": 1000.0,
            }
        )

        created = []

        def _try_snipe_single(side, price, arb_gap):
            created.append((side, price, arb_gap))
            return [{"trade_id": f"new-{side}"}]

        loop.sniper.try_snipe_single = _try_snipe_single
        loop.sniper._last_snipe_time = 0

        with (
            patch.object(
                loop,
                "_watch_active_probe_window",
                return_value=({"probe-buy"}, {"probe-sell"}),
            ),
            patch.object(
                loop,
                "_probe_hold_seconds_remaining",
                return_value=0.0,
            ),
            patch.object(
                bot_loop,
                "get_offer",
                return_value={"dexie_id": "dexie-probe"},
            ),
            patch.object(
                bot_loop,
                "get_offer_detail",
                return_value={"status": 1},
            ),
            patch.object(
                loop,
                "_get_market_aware_probe_prices",
                return_value={
                    "buy_price": Decimal("1.00"),
                    "sell_price": Decimal("1.20"),
                    "overall_best_bid": Decimal("0"),
                    "overall_best_ask": Decimal("0"),
                },
            ),
        ):
            result = loop._process_active_probe(set(), set(), Decimal("125"))

        self.assertTrue(loop._probe_state["active"])
        self.assertIsNone(loop._probe_state["confirmed_price"])
        self.assertEqual(loop._probe_state["attempt"], 6)
        self.assertEqual(result["sniper_fired"], True)
        self.assertEqual([side for side, _, _ in created], ["sell", "buy"])

    def test_full_ladder_does_not_rearm_missing_lingering_probe(self):
        loop = bot_loop.BotLoop()
        loop._running = False
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "confirmed_at": 1000.0,
                "launched_at": 990.0,
                "buy_tid": "probe-buy",
                "sell_tid": "probe-sell",
            }
        )

        def _fail_revalidate(*args, **kwargs):
            raise AssertionError(
                "confirmed probe revalidation should not run for a full ladder"
            )

        loop._revalidate_confirmed_probe_edges = _fail_revalidate

        loop._create_offers_if_needed(
            Decimal("1.10"),
            current_buy_count=fake_config.cfg.MAX_ACTIVE_BUY_OFFERS,
            current_sell_count=fake_config.cfg.MAX_ACTIVE_SELL_OFFERS,
            current_buy_ids={"b1", "b2", "b3", "b4"},
            current_sell_ids={"s1", "s2", "s3", "s4"},
            arb_gap=Decimal("1"),
        )

    def test_adaptive_target_caps_short_side_to_verified_live_count_without_spares(
        self,
    ):
        loop = bot_loop.BotLoop()
        loop.coin_manager._tier_spares = {
            "xch": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
            "cat": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
        }

        targets = loop._get_adaptive_offer_targets(
            Decimal("1.10"),
            current_buy_count=4,
            current_sell_count=2,
        )

        self.assertEqual(targets["buy"], 4)
        self.assertEqual(targets["sell"], 2)

    def test_adaptive_target_allows_regrowth_only_for_available_spares(self):
        loop = bot_loop.BotLoop()
        loop.coin_manager._tier_spares = {
            "xch": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
            "cat": {"inner": 2, "mid": 0, "outer": 0, "extreme": 0},
        }

        targets = loop._get_adaptive_offer_targets(
            Decimal("1.10"),
            current_buy_count=4,
            current_sell_count=2,
        )

        self.assertEqual(targets["sell"], 4)

    def test_confirmed_fills_clear_db_only_backoff_before_rebuild_targeting(self):
        loop = bot_loop.BotLoop()
        loop._adaptive_target_backoff_until["sell"] = 1300.0
        loop.coin_manager._tier_spares = {
            "xch": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
            "cat": {"inner": 2, "mid": 0, "outer": 0, "extreme": 0},
        }

        with patch.object(bot_loop.time, "time", return_value=1000.0):
            loop._clear_adaptive_target_backoff_for_confirmed_fills(
                buy_fills=[],
                sell_fills=[{"trade_id": "filled-sell"}],
            )
            targets = loop._get_adaptive_offer_targets(
                Decimal("1.10"),
                current_buy_count=4,
                current_sell_count=2,
            )

        self.assertEqual(loop._adaptive_target_backoff_until["sell"], 0.0)
        self.assertEqual(targets["sell"], 4)

    def test_wallet_missing_db_offers_are_retired_after_confirmation_grace(self):
        loop = bot_loop.BotLoop()
        now_ts = 1000.0
        old_created = datetime.fromtimestamp(now_ts - 180, timezone.utc).isoformat()
        fresh_created = datetime.fromtimestamp(now_ts - 20, timezone.utc).isoformat()
        loop.offer_manager._recently_created = {
            "old-sell": now_ts - 180,
            "fresh-sell": now_ts - 20,
        }
        loop.offer_manager._offer_details_cache = {
            "old-sell": {"side": "sell"},
            "fresh-sell": {"side": "sell"},
        }

        with (
            patch.object(
                bot_loop, "update_offer_status", return_value=True
            ) as update_mock,
            patch.object(bot_loop, "log_event") as log_event_mock,
        ):
            result = loop._retire_wallet_missing_db_offers(
                db_buy_offers=[],
                db_sell_offers=[
                    {
                        "trade_id": "old-sell",
                        "created_at": old_created,
                        "tier": "inner",
                    },
                    {
                        "trade_id": "fresh-sell",
                        "created_at": fresh_created,
                        "tier": "inner",
                    },
                    {
                        "trade_id": "visible-sell",
                        "created_at": old_created,
                        "tier": "inner",
                    },
                ],
                wallet_buy_ids=set(),
                wallet_sell_ids={"visible-sell"},
                wallet_sync_fresh=True,
                now_ts=now_ts,
            )

        update_mock.assert_called_once_with("old-sell", "not_submitted")
        self.assertEqual(result["sell"], {"old-sell"})
        self.assertEqual(result["buy"], set())
        self.assertNotIn("old-sell", loop.offer_manager._recently_created)
        self.assertIn("fresh-sell", loop.offer_manager._recently_created)
        self.assertGreater(loop._adaptive_target_backoff_until["sell"], now_ts)
        not_submitted_events = [
            call
            for call in log_event_mock.call_args_list
            if call.args[1] == "db_only_offer_not_submitted"
        ]
        self.assertEqual(len(not_submitted_events), 1)
        self.assertEqual(not_submitted_events[0].args[0], "info")

    def test_orphan_offer_cleanup_sets_adaptive_backoff_for_closed_side(self):
        loop = bot_loop.BotLoop()

        class _Conn:
            def execute(self, *args, **kwargs):
                return self

            def fetchall(self):
                return [{"trade_id": "dead-sell", "coin_id": "0xcoin", "side": "sell"}]

        updates = []

        def _update_offer_status(trade_id, status):
            updates.append((trade_id, status))
            return True

        with (
            patch.dict(sys.modules, {"database": fake_database, "wallet": fake_wallet}),
            patch.object(
                fake_database, "get_connection", return_value=_Conn(), create=True
            ),
            patch.object(fake_database, "lock_coin", return_value=True, create=True),
            patch.object(
                fake_database, "update_offer_status", side_effect=_update_offer_status
            ),
            patch.object(fake_wallet, "get_all_offers", return_value=[]),
            patch.object(bot_loop.time, "time", return_value=1000.0),
            patch.object(bot_loop, "log_event"),
        ):
            loop._repair_unlinked_offer_coins()

        self.assertEqual(updates, [("dead-sell", "cancelled")])
        self.assertGreaterEqual(loop._adaptive_target_backoff_until["sell"], 1300.0)
        self.assertEqual(loop._adaptive_target_backoff_until["buy"], 0.0)

    def test_daily_reconcile_recent_db_only_offers_are_visibility_info(self):
        loop = bot_loop.BotLoop()
        now_ts = 1000.0
        old_created = datetime.fromtimestamp(now_ts - 180, timezone.utc).isoformat()
        fresh_created = datetime.fromtimestamp(now_ts - 20, timezone.utc).isoformat()
        db_open = [
            {"trade_id": f"visible-{idx}", "created_at": old_created, "tier": "inner"}
            for idx in range(4)
        ] + [
            {"trade_id": f"fresh-{idx}", "created_at": fresh_created, "tier": "inner"}
            for idx in range(4)
        ]
        wallet_open = [
            {"trade_id": f"visible-{idx}", "status": "PENDING_ACCEPT"}
            for idx in range(4)
        ]

        with (
            patch.dict(sys.modules, {"database": fake_database}),
            patch.object(
                bot_loop, "backfill_verified_fills_from_offers", return_value=[]
            ),
            patch.object(fake_database, "get_open_offers", return_value=db_open),
            patch.object(bot_loop, "get_all_offers", return_value=wallet_open),
            patch.object(bot_loop.time, "time", return_value=now_ts),
            patch.object(bot_loop, "log_event") as log_event_mock,
        ):
            loop._maybe_run_daily_reconcile()

        event_keys = [
            (call.args[0], call.args[1]) for call in log_event_mock.call_args_list
        ]
        self.assertIn(("info", "daily_reconcile_count_pending_visibility"), event_keys)
        self.assertNotIn(("warning", "daily_reconcile_count_mismatch"), event_keys)

    def test_immediate_sweep_protection_pauses_filled_side_same_cycle(self):
        loop = bot_loop.BotLoop()
        loop._sweep_protection = {}

        buy_fills = [
            {"trade_id": "buy-1"},
            {"trade_id": "buy-2"},
            {"trade_id": "buy-3"},
        ]

        with (
            patch.object(fake_config.cfg, "SWEEP_MIN_FILLS", 3, create=True),
            patch.object(fake_config.cfg, "SWEEP_PROTECTION_SECS", 90, create=True),
            patch.object(bot_loop.time, "time", return_value=1000.0),
        ):
            protected = loop._apply_immediate_sweep_protection(buy_fills, [])

        self.assertEqual(protected, {"buy"})
        self.assertEqual(loop._sweep_protection["buy"], 1090.0)
        self.assertNotIn("sell", loop._sweep_protection)

    def test_probe_hold_requires_min_age_before_confirmation(self):
        loop = bot_loop.BotLoop()

        loop._probe_state.update(
            {
                "active": True,
                "launched_at": 1000.0,
            }
        )

        self.assertFalse(loop._probe_has_matured(now_ts=1025.0))
        self.assertTrue(loop._probe_has_matured(now_ts=1030.0))
        self.assertAlmostEqual(
            loop._probe_hold_seconds_remaining(now_ts=1012.5),
            17.5,
        )

    def test_probe_cleanup_linger_uses_latest_probe_anchor(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_at": 1000.0,
                "launched_at": 980.0,
            }
        )

        self.assertAlmostEqual(
            loop._probe_cleanup_seconds_remaining(now_ts=1200.0),
            400.0,
        )

        loop._probe_state["launched_at"] = 1500.0
        self.assertAlmostEqual(
            loop._probe_cleanup_seconds_remaining(now_ts=1600.0),
            500.0,
        )

    def test_get_probe_anchored_mid_uses_side_probe_edge(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
            }
        )

        buy_mid = loop._get_probe_anchored_mid("buy", Decimal("1.10"))
        sell_mid = loop._get_probe_anchored_mid("sell", Decimal("1.10"))

        self.assertEqual(
            buy_mid,
            Decimal("1.02") / Decimal("0.97"),
        )
        self.assertEqual(
            sell_mid,
            Decimal("1.18") / Decimal("1.03"),
        )

    def test_market_aware_probe_prices_clamp_crossed_book_to_amm_safe_edge(self):
        loop = bot_loop.BotLoop()
        loop.market_intel.orderbook = {
            "overall_best_bid": Decimal("0.999"),
            "overall_best_ask": Decimal("1.001"),
        }

        prices = loop._get_market_aware_probe_prices(
            Decimal("1.0"),
            Decimal("50"),
        )

        self.assertEqual(
            prices["buy_price"],
            Decimal("1.0") / Decimal("1.005"),
        )
        self.assertEqual(
            prices["sell_price"],
            Decimal("1.0") * Decimal("1.005"),
        )

    def test_market_aware_probe_prices_clamp_wallet_edges_to_amm_safe_edge(self):
        loop = bot_loop.BotLoop()
        loop.market_intel.orderbook = {
            "overall_best_bid": Decimal("0.990"),
            "overall_best_ask": Decimal("1.010"),
        }

        prices = loop._get_market_aware_probe_prices(
            Decimal("1.0"),
            Decimal("50"),
            offer_edges={
                "our_best_bid": "1.003",
                "our_best_ask": "0.997",
            },
        )

        self.assertEqual(
            prices["buy_price"],
            Decimal("1.0") / Decimal("1.005"),
        )
        self.assertEqual(
            prices["sell_price"],
            Decimal("1.0") * Decimal("1.005"),
        )

    def test_probe_retry_backoff_steps_away_from_previous_probe(self):
        loop = bot_loop.BotLoop()

        self.assertEqual(
            loop._apply_probe_retry_backoff(
                "buy",
                Decimal("1.2000"),
                Decimal("1.2000"),
            ),
            Decimal("1.2000") / Decimal("1.005"),
        )
        self.assertEqual(
            loop._apply_probe_retry_backoff(
                "sell",
                Decimal("1.1800"),
                Decimal("1.1800"),
            ),
            Decimal("1.1800") * Decimal("1.005"),
        )

    def test_watch_active_probe_window_uses_fast_polling(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._probe_state.update(
            {
                "active": True,
                "buy_tid": "probe-buy",
                "sell_tid": "probe-sell",
                "launched_at": 1000.0,
            }
        )

        with (
            patch.object(
                loop,
                "_refresh_live_offer_ids_from_wallet",
                side_effect=[
                    ({"probe-buy"}, {"probe-sell"}),
                    ({"probe-buy"}, {"probe-sell"}),
                ],
            ),
            patch.object(
                loop,
                "_probe_hold_seconds_remaining",
                side_effect=[25.0, 0.0],
            ),
            patch("bot_loop.time.sleep") as sleep_mock,
        ):
            buy_ids, sell_ids = loop._watch_active_probe_window(
                set(),
                set(),
                force_refresh=True,
            )

        self.assertEqual(buy_ids, {"probe-buy"})
        self.assertEqual(sell_ids, {"probe-sell"})
        sleep_mock.assert_called_once_with(5.0)

    def test_process_active_probe_confirms_after_fast_watch(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._probe_state.update(
            {
                "active": True,
                "buy_tid": "probe-buy",
                "sell_tid": "probe-sell",
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
                "tibet_price": Decimal("1.10"),
                "launched_at": 1000.0,
            }
        )

        with (
            patch.object(
                loop,
                "_watch_active_probe_window",
                return_value=({"probe-buy"}, {"probe-sell"}),
            ),
            patch.object(
                loop,
                "_probe_hold_seconds_remaining",
                return_value=0.0,
            ),
        ):
            result = loop._process_active_probe(set(), set(), Decimal("500"))

        self.assertFalse(loop._probe_state["active"])
        self.assertEqual(loop._probe_state["confirmed_price"], Decimal("1.10"))
        self.assertEqual(result["buy_ids"], {"probe-buy"})
        self.assertEqual(result["sell_ids"], {"probe-sell"})

    def test_create_offers_if_needed_applies_probe_anchor_per_side(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
            }
        )

        loop._create_offers_if_needed(Decimal("1.10"), 0, 0)

        create_calls = loop.offer_manager.create_calls
        self.assertEqual(len(create_calls), 2)

        buy_call = next(call for call in create_calls if call[0] == "buy")
        sell_call = next(call for call in create_calls if call[0] == "sell")

        self.assertEqual(
            buy_call[1],
            Decimal("1.02") / Decimal("0.97"),
        )
        self.assertEqual(
            sell_call[1],
            Decimal("1.18") / Decimal("1.03"),
        )

    def test_create_offers_if_needed_passes_probe_boundaries(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "confirmed_at": 1000.0,
                "launched_at": 1000.0,
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
            }
        )

        with patch("bot_loop.time.time", return_value=1100.0):
            loop._create_offers_if_needed(Decimal("1.10"), 0, 0)

        buy_call = next(
            call for call in loop.offer_manager.create_calls if call[0] == "buy"
        )
        sell_call = next(
            call for call in loop.offer_manager.create_calls if call[0] == "sell"
        )

        self.assertEqual(
            buy_call[2]["price_cap"],
            Decimal("1.02") / Decimal("1.0001"),
        )
        self.assertEqual(
            sell_call[2]["price_floor"],
            Decimal("1.18") * Decimal("1.0001"),
        )

    def test_create_offers_if_needed_keeps_deployed_probe_baseline(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
            }
        )

        def _fake_create_ladder(mid_price, side, **kwargs):
            return [
                {
                    "offer_bech32": f"offer1-{side}",
                    "trade_id": f"trade-{side}",
                }
            ]

        loop.offer_manager.create_ladder = _fake_create_ladder
        loop._create_offers_if_needed(Decimal("1.10"), 0, 0)

        self.assertEqual(
            loop._last_quoted_price["buy"],
            Decimal("1.02") / Decimal("0.97"),
        )
        self.assertEqual(
            loop._last_quoted_price["sell"],
            Decimal("1.18") / Decimal("1.03"),
        )

    def test_create_offers_if_needed_excludes_live_confirmed_probes_from_slots(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "buy_tid": "probe-buy",
                "sell_tid": "probe-sell",
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
            }
        )

        loop._create_offers_if_needed(
            Decimal("1.10"),
            current_buy_count=1,
            current_sell_count=1,
            current_buy_ids={"probe-buy"},
            current_sell_ids={"probe-sell"},
        )

        buy_call = next(
            call for call in loop.offer_manager.create_calls if call[0] == "buy"
        )
        sell_call = next(
            call for call in loop.offer_manager.create_calls if call[0] == "sell"
        )
        self.assertEqual(buy_call[2]["num_offers"], 4)
        self.assertEqual(sell_call[2]["num_offers"], 4)

    def test_create_offers_if_needed_waits_for_wallet_active_pending_cancels(self):
        loop = bot_loop.BotLoop()
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": set(),
            "sell": {"pending-sell"},
        }

        loop._create_offers_if_needed(
            Decimal("1.10"),
            current_buy_count=fake_config.cfg.MAX_ACTIVE_BUY_OFFERS,
            current_sell_count=2,
            current_buy_ids={"b1", "b2", "b3", "b4"},
            current_sell_ids={"s1", "s2"},
        )

        self.assertFalse(
            any(call[0] == "sell" for call in loop.offer_manager.create_calls),
            "sell creation must pause until pending cancels disappear from Sage",
        )

    def test_wallet_active_pending_cancel_watchdog_queues_stale_retry(self):
        loop = bot_loop.BotLoop()
        loop.offer_manager._pending_cancel_retries = {}
        loop._pending_cancel_settle_seen = {
            "stuck-sell": {
                "side": "sell",
                "first_seen": 1000.0,
                "last_retry": 0.0,
                "retries": 0,
            },
            "gone-sell": {
                "side": "sell",
                "first_seen": 1000.0,
                "last_retry": 0.0,
                "retries": 0,
            },
        }
        loop._pending_cancel_settle_retry_secs = 300.0

        with patch.object(bot_loop.time, "time", return_value=1401.0):
            loop._track_pending_cancel_settle_watchdog(
                {
                    "buy": set(),
                    "sell": {"stuck-sell"},
                }
            )

        self.assertIn("stuck-sell", loop.offer_manager._pending_cancel_retries)
        self.assertEqual(
            loop.offer_manager._pending_cancel_retries["stuck-sell"]["attempts"],
            0,
        )
        self.assertEqual(loop._pending_cancel_settle_seen["stuck-sell"]["retries"], 1)
        self.assertNotIn("gone-sell", loop._pending_cancel_settle_seen)

    def test_pending_cancel_settle_retry_window_comes_from_config(self):
        with (
            patch.object(
                fake_config.cfg, "PENDING_CANCEL_SETTLE_RETRY_SECS", 120, create=True
            ),
            patch.object(
                fake_config.cfg, "PENDING_CANCEL_SETTLE_MAX_RETRIES", 7, create=True
            ),
        ):
            loop = bot_loop.BotLoop()

        self.assertEqual(loop._pending_cancel_settle_retry_secs, 120.0)
        self.assertEqual(loop._pending_cancel_settle_max_retries, 7)

    def test_emergency_requote_pending_cancels_are_waiting_not_failure(self):
        loop = bot_loop.BotLoop()
        loop._last_quoted_price["buy"] = Decimal("1.30")
        loop._last_quoted_plain_mid["buy"] = Decimal("1.30")
        backoffs = []
        events = []

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        loop._set_requote_failure_backoff = lambda side, reason, data=None: (
            backoffs.append((side, reason, data or {}))
        )

        with patch.object(bot_loop, "log_event", side_effect=_log_event):
            result = loop._process_emergency_requote_result(
                "buy",
                {
                    "offers": [],
                    "fully_replaced": False,
                    "replaced_count": 0,
                    "original_target_count": 24,
                    "pending_cancel_count": 24,
                    "failed_cancel_count": 0,
                },
                Decimal("1.10"),
                Decimal("1.10"),
            )

        self.assertFalse(result["any_progress"])
        self.assertEqual(backoffs, [])
        self.assertEqual(loop._last_quoted_price["buy"], Decimal("1.30"))
        self.assertTrue(
            any(
                severity == "info"
                and event_type == "emergency_requote_waiting_for_cancel_settle"
                for severity, event_type, _, _ in events
            )
        )
        self.assertFalse(
            any(
                event_type == "emergency_requote_no_progress"
                for _, event_type, _, _ in events
            )
        )

    def test_wallet_active_pending_cancel_watchdog_aggregates_retry_log(self):
        loop = bot_loop.BotLoop()
        loop.offer_manager._pending_cancel_retries = {}
        loop._pending_cancel_settle_retry_secs = 300.0
        loop._pending_cancel_settle_seen = {
            "stuck-sell-1": {
                "side": "sell",
                "first_seen": 1000.0,
                "last_retry": 0.0,
                "retries": 0,
            },
            "stuck-sell-2": {
                "side": "sell",
                "first_seen": 1000.0,
                "last_retry": 0.0,
                "retries": 0,
            },
            "stuck-sell-3": {
                "side": "sell",
                "first_seen": 1000.0,
                "last_retry": 0.0,
                "retries": 0,
            },
        }
        events = []

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        with (
            patch.object(bot_loop.time, "time", return_value=1401.0),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
        ):
            loop._track_pending_cancel_settle_watchdog(
                {
                    "buy": set(),
                    "sell": {"stuck-sell-1", "stuck-sell-2", "stuck-sell-3"},
                }
            )

        retry_events = [
            event
            for event in events
            if event[1] == "pending_cancel_settle_retry_queued"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0][3]["side"], "sell")
        self.assertEqual(retry_events[0][3]["queued_count"], 3)
        self.assertIn("3 sell cancel", retry_events[0][2])
        self.assertEqual(len(loop.offer_manager._pending_cancel_retries), 3)

    def test_pending_cancel_deferral_notice_suppresses_unchanged_repeats(self):
        loop = bot_loop.BotLoop()
        events = []

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        with (
            patch.object(bot_loop, "log_event", side_effect=_log_event),
            patch.object(bot_loop.time, "time", return_value=1000.0),
        ):
            first = loop._log_pending_cancel_notice(
                "warning",
                "emergency_requote_deferred_pending_cancel_settle",
                "Emergency sell requote deferred - 20 pending cancels must settle",
                side="sell",
                pending_count=20,
            )
            repeat = loop._log_pending_cancel_notice(
                "warning",
                "emergency_requote_deferred_pending_cancel_settle",
                "Emergency sell requote deferred - 20 pending cancels must settle",
                side="sell",
                pending_count=20,
            )

        with (
            patch.object(bot_loop, "log_event", side_effect=_log_event),
            patch.object(bot_loop.time, "time", return_value=1010.0),
        ):
            changed = loop._log_pending_cancel_notice(
                "warning",
                "emergency_requote_deferred_pending_cancel_settle",
                "Emergency sell requote deferred - 19 pending cancels must settle",
                side="sell",
                pending_count=19,
            )

        self.assertTrue(first)
        self.assertFalse(repeat)
        self.assertTrue(changed)
        self.assertEqual(
            [
                event[3].get("pending_count")
                for event in events
                if event[1] == "emergency_requote_deferred_pending_cancel_settle"
            ],
            [20, 19],
        )

    def test_wallet_active_pending_cancel_watchdog_uses_db_cancel_age_for_new_ids(self):
        loop = bot_loop.BotLoop()
        loop.offer_manager._pending_cancel_retries = {}
        loop._pending_cancel_settle_retry_secs = 300.0

        with (
            patch.object(bot_loop.time, "time", return_value=1401.0),
            patch.object(
                bot_loop,
                "get_offer",
                return_value={
                    "cancel_last_attempt_at": "1970-01-01T00:16:40+00:00",
                },
            ),
        ):
            loop._track_pending_cancel_settle_watchdog(
                {
                    "buy": set(),
                    "sell": {"old-sell"},
                }
            )

        self.assertIn("old-sell", loop.offer_manager._pending_cancel_retries)
        self.assertEqual(
            loop._pending_cancel_settle_seen["old-sell"]["first_seen"], 1000.0
        )

    def test_dexie_terminal_pending_cancel_releases_sage_lag_after_two_hits(self):
        loop = bot_loop.BotLoop()
        loop._pending_cancel_dexie_terminal_confirmations = 2

        def _fake_get_offer(tid):
            return {"dexie_id": f"dexie-{tid}"}

        with (
            patch.object(bot_loop, "get_offer", side_effect=_fake_get_offer),
            patch.object(
                bot_loop,
                "get_offer_detail",
                return_value={"status": bot_loop.DEXIE_STATUS_CANCELLED},
            ),
            patch.object(bot_loop.time, "time", return_value=1000.0),
        ):
            first = loop._filter_pending_cancel_wallet_ids_by_dexie(
                {
                    "buy": set(),
                    "sell": {"safe-sell"},
                }
            )

        self.assertIn(
            "safe-sell",
            first["sell"],
            "one Dexie terminal sample should not release the Sage-lag guard yet",
        )

        with (
            patch.object(bot_loop, "get_offer", side_effect=_fake_get_offer),
            patch.object(
                bot_loop,
                "get_offer_detail",
                return_value={"status": bot_loop.DEXIE_STATUS_CANCELLED},
            ),
            patch.object(bot_loop.time, "time", return_value=1001.0),
        ):
            second = loop._filter_pending_cancel_wallet_ids_by_dexie(
                {
                    "buy": set(),
                    "sell": {"safe-sell"},
                }
            )

        self.assertNotIn(
            "safe-sell",
            second["sell"],
            "two Dexie terminal samples should unblock replacement creation",
        )

    def test_dexie_active_or_completed_pending_cancel_stays_blocking(self):
        for status in (
            bot_loop.DEXIE_STATUS_ACTIVE,
            bot_loop.DEXIE_STATUS_PENDING,
            bot_loop.DEXIE_STATUS_COMPLETED,
        ):
            loop = bot_loop.BotLoop()
            loop._pending_cancel_dexie_terminal_confirmations = 2

            with (
                patch.object(
                    bot_loop, "get_offer", return_value={"dexie_id": "dexie-risky"}
                ),
                patch.object(
                    bot_loop, "get_offer_detail", return_value={"status": status}
                ),
            ):
                first = loop._filter_pending_cancel_wallet_ids_by_dexie(
                    {
                        "buy": {"risky-buy"},
                        "sell": set(),
                    }
                )
                second = loop._filter_pending_cancel_wallet_ids_by_dexie(
                    {
                        "buy": {"risky-buy"},
                        "sell": set(),
                    }
                )

            self.assertIn("risky-buy", first["buy"])
            self.assertIn(
                "risky-buy",
                second["buy"],
                f"Dexie status {status} must keep the Sage-lag guard active",
            )

    def test_forced_requote_waits_for_wallet_active_pending_cancels(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6
        loop._last_quoted_price["sell"] = Decimal("1.00")
        loop._force_requote["sell"] = True
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": set(),
            "sell": {"pending-sell"},
        }

        with contextlib.redirect_stdout(io.StringIO()):
            loop._handle_requoting(Decimal("1.20"), set(), set())

        self.assertEqual(loop.offer_manager.requote_calls, [])
        self.assertTrue(
            loop._force_requote["sell"],
            "forced requote should retry after pending cancels settle",
        )

    def test_requote_skips_config_disabled_side(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6
        loop._last_quoted_price["sell"] = Decimal("1.00")
        loop._force_requote["sell"] = True

        with (
            patch.object(fake_config.cfg, "ENABLE_SELL", False),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            loop._handle_requoting(
                Decimal("1.20"),
                current_buy_ids=set(),
                current_sell_ids={"sell-live"},
            )

        self.assertEqual(loop.offer_manager.requote_calls, [])
        self.assertFalse(
            loop._force_requote["sell"],
            "disabled sell side should not keep a stale forced-requote flag",
        )

    def test_opposite_side_requote_waits_during_recent_tibet_shock_guard(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6
        loop._last_quoted_price["sell"] = Decimal("1.00")
        loop._last_tibet_shock = {
            "at": time.time(),
            "direction": "down",
            "pct": 9.68,
            "sides": ("buy",),
            "tiers": ("inner", "mid"),
        }

        with contextlib.redirect_stdout(io.StringIO()):
            loop._handle_requoting(
                Decimal("0.90"),
                current_buy_ids={"buy-live"},
                current_sell_ids={"sell-live"},
            )

        self.assertEqual(
            loop.offer_manager.requote_calls,
            [],
            "non-vulnerable sell requote should wait during a recent down-shock guard",
        )
        self.assertTrue(
            loop._force_requote["sell"],
            "deferred opposite-side requote should retry after the guard expires",
        )

    def test_mempool_price_move_refreshes_tibet_cache_and_marks_reprice(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        injected = []
        invalidated = []

        class _PriceEngine:
            def inject_tibet_reserves(self, **kwargs):
                injected.append(kwargs)
                return True

            def invalidate_tibet_cache(self):
                invalidated.append(True)

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "price_move",
                        "direction": "up",
                        "magnitude_pct": 7.684,
                        "timestamp": 123.0,
                        "delta_xch": 42,
                        "old_xch_reserve": 1000,
                        "old_tok_reserve": 5000,
                        "new_xch_reserve": 1100,
                        "new_tok_reserve": 4500,
                        "old_price_xch": 0.00017369,
                        "new_price_xch": 0.00018716,
                        "pair_id": "pair-1",
                    }
                ]

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop.price_engine = _PriceEngine()

        with (
            patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool),
            patch.object(bot_loop.time, "time", return_value=1000.0),
        ):
            loop._drain_mempool_signals(in_cycle=True)

        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0]["pair_id"], "pair-1")
        self.assertEqual(injected[0]["xch_reserve"], 1100)
        self.assertEqual(injected[0]["token_reserve"], 4500)
        self.assertFalse(invalidated)
        self.assertTrue(loop._mempool_price_refresh_needed)
        self.assertEqual(loop._watcher_data["last_change_ts"], 1000.0)
        self.assertEqual(loop._watcher_data["last_xch_reserve"], 1100)
        self.assertEqual(loop._watcher_data["last_token_reserve"], 4500)
        self.assertEqual(loop._watcher_data["last_confirmed_price_xch"], "0.00018716")

    def test_mempool_price_move_stores_raw_reserves_in_watcher_units(self):
        loop = bot_loop.BotLoop()
        loop._running = True

        class _PriceEngine:
            def inject_tibet_reserves(self, **kwargs):
                del kwargs
                return True

            def invalidate_tibet_cache(self):
                raise AssertionError(
                    "cache should not invalidate on successful injection"
                )

        loop.price_engine = _PriceEngine()
        with patch.object(bot_loop.time, "time", return_value=1000.0):
            loop._record_confirmed_price_move(
                {
                    "type": "price_move",
                    "direction": "down",
                    "magnitude_pct": 7.369,
                    "new_xch_reserve": 125660489718592,
                    "new_tok_reserve": 953680549,
                    "new_price_xch": "0.00013177",
                    "pair_id": "pair-raw",
                },
                in_cycle=False,
            )

        self.assertAlmostEqual(loop._watcher_data["last_xch_reserve"], 125.660489718592)
        self.assertAlmostEqual(loop._watcher_data["last_token_reserve"], 953680.549)
        self.assertEqual(loop._watcher_data["change_pct"], 7.369)

    def test_mempool_price_move_invalidates_cache_when_injection_misses(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        invalidated = []

        class _PriceEngine:
            def inject_tibet_reserves(self, **kwargs):
                del kwargs
                return False

            def invalidate_tibet_cache(self):
                invalidated.append(True)

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "price_move",
                        "direction": "down",
                        "magnitude_pct": 5.0,
                        "timestamp": 123.0,
                        "delta_xch": -42,
                        "new_xch_reserve": 900,
                        "new_tok_reserve": 5500,
                        "new_price_xch": 0.00016000,
                        "pair_id": "pair-2",
                    }
                ]

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop.price_engine = _PriceEngine()

        with patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertTrue(invalidated)
        self.assertFalse(loop._mempool_price_refresh_needed)

    def test_projected_imminent_swap_xch_inflow_cancels_sell_side(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        cancel_calls = []
        events = []

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "imminent_swap",
                        "direction": "up",
                        "magnitude_pct": 15.0,
                        "source": "mempool_projected_reserves",
                        "timestamp": 123.0,
                        "pool_coin_id": "pool-1",
                        "current_xch_reserve": 126_610_400_180_848,
                        "current_tok_reserve": 945_042_996,
                        "new_xch_reserve": 141_610_400_133_678,
                        "new_tok_reserve": 845_566_825,
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 20

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop._defensive_cancel_tiers = _cancel_tiers

        with (
            patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
        ):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0][1]["sides"], ("sell",))
        self.assertEqual(cancel_calls[0][1]["tiers"], ("inner", "mid", "outer"))
        self.assertTrue(
            any(
                event_type == "mempool_preconfirm_defensive_cancel_done"
                for _, event_type, _, _ in events
            )
        )
        self.assertEqual(loop._last_tibet_shock["sides"], ("sell",))

    def test_projected_imminent_swap_xch_outflow_cancels_buy_side(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        cancel_calls = []

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "imminent_swap",
                        "direction": "down",
                        "magnitude_pct": 7.0,
                        "source": "mempool_projected_reserves",
                        "timestamp": 123.0,
                        "pool_coin_id": "pool-1",
                        "current_xch_reserve": 141_610_400_133_678,
                        "current_tok_reserve": 845_566_825,
                        "new_xch_reserve": 135_844_800_284_611,
                        "new_tok_reserve": 881_707_825,
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 12

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop._defensive_cancel_tiers = _cancel_tiers

        with patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0][1]["sides"], ("buy",))
        self.assertEqual(cancel_calls[0][1]["tiers"], ("inner", "mid"))

    def test_unknown_imminent_swap_does_not_cancel_preconfirmation(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        cancel_calls = []

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "imminent_swap",
                        "direction": "unknown",
                        "magnitude_pct": 0.0,
                        "source": "mempool_detected",
                        "timestamp": 123.0,
                        "pool_coin_id": "pool-1",
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 1

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop._defensive_cancel_tiers = _cancel_tiers

        with patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertEqual(cancel_calls, [])

    def test_projected_imminent_swap_defers_while_pending_cancel_settles(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": set(),
            "sell": {"pending-sell"},
        }
        cancel_calls = []
        events = []

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "imminent_swap",
                        "direction": "up",
                        "magnitude_pct": 15.0,
                        "source": "mempool_projected_reserves",
                        "timestamp": 123.0,
                        "pool_coin_id": "pool-1",
                        "current_xch_reserve": 126_610_400_180_848,
                        "current_tok_reserve": 945_042_996,
                        "new_xch_reserve": 141_610_400_133_678,
                        "new_tok_reserve": 845_566_825,
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 20

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop._defensive_cancel_tiers = _cancel_tiers

        with (
            patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
        ):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertEqual(cancel_calls, [])
        self.assertTrue(loop._force_requote["sell"])
        self.assertTrue(
            any(
                event_type == "mempool_preconfirm_cancel_deferred_pending_cancel_settle"
                for _, event_type, _, _ in events
            )
        )

    def test_mempool_price_move_respects_configured_shock_trigger(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        original_trigger = getattr(
            fake_config.cfg, "TIBET_SHOCK_CANCEL_TRIGGER_PCT", None
        )
        fake_config.cfg.TIBET_SHOCK_CANCEL_TRIGGER_PCT = Decimal("2.5")
        cancel_calls = []

        class _PriceEngine:
            def inject_tibet_reserves(self, **kwargs):
                del kwargs
                return True

            def invalidate_tibet_cache(self):
                raise AssertionError(
                    "cache should not invalidate on successful injection"
                )

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "price_move",
                        "direction": "up",
                        "magnitude_pct": 2.0,
                        "timestamp": 123.0,
                        "delta_xch": 42,
                        "new_xch_reserve": 1100,
                        "new_tok_reserve": 4500,
                        "new_price_xch": 0.00018716,
                        "pair_id": "pair-1",
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 1

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop.price_engine = _PriceEngine()
        loop._defensive_cancel_tiers = _cancel_tiers

        try:
            with patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool):
                loop._drain_mempool_signals(in_cycle=False)
        finally:
            if original_trigger is None:
                delattr(fake_config.cfg, "TIBET_SHOCK_CANCEL_TRIGGER_PCT")
            else:
                fake_config.cfg.TIBET_SHOCK_CANCEL_TRIGGER_PCT = original_trigger

        self.assertEqual(cancel_calls, [])

    def test_mempool_defensive_cancel_skips_side_recently_repriced_within_edge(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        cancel_calls = []
        events = []
        loop._last_shock_requote = {
            "buy": {
                "at": 1000.0,
                "mid_price": "0.00015782",
                "source": "emergency_requote",
                "severity": "emergency",
            },
            "sell": {},
        }

        class _PriceEngine:
            def inject_tibet_reserves(self, **kwargs):
                del kwargs
                return True

            def invalidate_tibet_cache(self):
                raise AssertionError(
                    "cache should not invalidate on successful injection"
                )

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "price_move",
                        "direction": "down",
                        "magnitude_pct": 12.376,
                        "timestamp": 123.0,
                        "delta_xch": -42,
                        "new_xch_reserve": 900,
                        "new_tok_reserve": 5500,
                        "new_price_xch": "0.0001542795119200447629053942877",
                        "pair_id": "pair-2",
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 15

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop.price_engine = _PriceEngine()
        loop._defensive_cancel_tiers = _cancel_tiers

        with (
            patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool),
            patch.object(bot_loop.time, "time", return_value=1010.0),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
        ):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertEqual(cancel_calls, [])
        self.assertTrue(
            any(
                event_type == "mempool_defensive_cancel_suppressed_recent_requote"
                for _, event_type, _, _ in events
            )
        )
        self.assertEqual(loop._last_tibet_shock["sides"], ("buy",))

    def test_mempool_defensive_cancel_defers_while_pending_cancel_settles(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": {"pending-buy"},
            "sell": set(),
        }
        cancel_calls = []
        events = []

        class _PriceEngine:
            def inject_tibet_reserves(self, **kwargs):
                del kwargs
                return True

            def invalidate_tibet_cache(self):
                raise AssertionError(
                    "cache should not invalidate on successful injection"
                )

        class _Watcher:
            def get_pending_signals(self):
                return [
                    {
                        "type": "price_move",
                        "direction": "up",
                        "magnitude_pct": 9.0,
                        "timestamp": 123.0,
                        "delta_xch": 42,
                        "new_xch_reserve": 1100,
                        "new_tok_reserve": 4500,
                        "new_price_xch": "0.00018716",
                        "pair_id": "pair-1",
                    }
                ]

        def _cancel_tiers(*args, **kwargs):
            cancel_calls.append((args, kwargs))
            return 12

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        fake_mempool = types.SimpleNamespace(_watcher_instance=_Watcher())
        loop.price_engine = _PriceEngine()
        loop._defensive_cancel_tiers = _cancel_tiers

        with (
            patch.object(bot_loop, "_mempool_watcher_mod", fake_mempool),
            patch.object(bot_loop.time, "time", return_value=1010.0),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
        ):
            loop._drain_mempool_signals(in_cycle=False)

        self.assertEqual(cancel_calls, [])
        self.assertTrue(loop._force_requote["sell"])
        self.assertTrue(
            any(
                event_type == "mempool_defensive_cancel_deferred_pending_cancel_settle"
                for _, event_type, _, _ in events
            )
        )
        self.assertEqual(loop._last_tibet_shock["sides"], ("sell",))

    def test_defensive_cancel_tiers_bypasses_storm_guard_and_counts_successes(self):
        loop = bot_loop.BotLoop()
        cancel_calls = []
        open_sells = [{"trade_id": f"sell-{idx}", "tier": "inner"} for idx in range(21)]

        class _Canceller:
            def cancel_offers(
                self,
                trade_ids,
                reason="manual",
                force_storm=False,
                skip_confirmation=False,
            ):
                cancel_calls.append(
                    {
                        "trade_ids": list(trade_ids),
                        "reason": reason,
                        "force_storm": force_storm,
                        "skip_confirmation": skip_confirmation,
                    }
                )
                return {
                    trade_id: {
                        "success": trade_id != "sell-20",
                        "error": "wallet_busy" if trade_id == "sell-20" else "",
                    }
                    for trade_id in trade_ids
                }

        def _get_open_offers(side=None, cat_asset_id=None):
            del cat_asset_id
            return open_sells if side == "sell" else []

        loop.offer_manager = _Canceller()

        with (
            patch("database.get_open_offers", side_effect=_get_open_offers),
            patch("config.cfg", fake_config.cfg),
        ):
            cancelled = loop._defensive_cancel_tiers(
                tiers=("inner", "mid", "outer"),
                sides=("sell",),
                reason="mempool_price_move_11.01pct_up",
            )

        self.assertEqual(cancelled, 20)
        self.assertEqual(len(cancel_calls), 1)
        self.assertTrue(cancel_calls[0]["force_storm"])
        self.assertTrue(cancel_calls[0]["skip_confirmation"])
        self.assertEqual(
            cancel_calls[0]["trade_ids"], [f"sell-{idx}" for idx in range(21)]
        )

    def test_probe_rearm_defers_while_pending_cancel_settles(self):
        loop = bot_loop.BotLoop()
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": {"pending-buy"},
            "sell": {"pending-sell"},
        }
        events = []

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        with patch.object(bot_loop, "log_event", side_effect=_log_event):
            launch_reason = loop._defer_probe_launch_for_pending_cancel(
                "price_move (9.0% >= 2.0%)"
            )

        self.assertIsNone(launch_reason)
        self.assertTrue(
            any(
                event_type == "probe_launch_deferred_pending_cancel_settle"
                for _, event_type, _, _ in events
            )
        )

    def test_create_disabled_under_target_message_is_rate_limited(self):
        loop = bot_loop.BotLoop()
        events = []
        loop._requote_failure_backoff_until["buy"] = 1060.0

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        with (
            patch.object(bot_loop.time, "time", return_value=1000.0),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            for _ in range(2):
                loop._create_offers_if_needed(
                    Decimal("1.00"),
                    1,
                    fake_config.cfg.MAX_ACTIVE_SELL_OFFERS,
                    current_buy_ids={"buy-live"},
                    current_sell_ids={
                        f"sell-{idx}"
                        for idx in range(fake_config.cfg.MAX_ACTIVE_SELL_OFFERS)
                    },
                )

        disabled_events = [
            event for event in events if event[1] == "create_disabled_buy_under_target"
        ]
        self.assertEqual(len(disabled_events), 1)

    def test_pending_mempool_reprice_updates_cycle_mid(self):
        loop = bot_loop.BotLoop()
        loop._mempool_price_refresh_needed = True
        arb_updates = []

        class _PriceEngine:
            def get_price(self, *args, **kwargs):
                del args, kwargs
                return {
                    "mid_price": Decimal("1.25"),
                    "dexie_price": Decimal("1.10"),
                    "tibet_price": Decimal("1.30"),
                    "arb_gap_bps": Decimal("1818.18"),
                }

        class _RiskManager:
            def update_arb_gap(self, arb_gap):
                arb_updates.append(arb_gap)

        loop.price_engine = _PriceEngine()
        loop.risk_manager = _RiskManager()

        mid, arb_gap, fresh = loop._refresh_price_if_mempool_move_pending(
            Decimal("1.00"),
            Decimal("0"),
        )

        self.assertEqual(mid, Decimal("1.25"))
        self.assertEqual(arb_gap, Decimal("1818.18"))
        self.assertEqual(fresh["tibet_price"], Decimal("1.30"))
        self.assertFalse(loop._mempool_price_refresh_needed)
        self.assertEqual(loop._current_mid_price, Decimal("1.25"))
        self.assertEqual(loop._bot_state["mid_price"], "1.25")
        self.assertEqual(arb_updates, [Decimal("1818.18")])

    def test_mempool_reprice_replaces_cycle_price_data_for_probe_launch(self):
        loop = bot_loop.BotLoop()
        loop._mempool_price_refresh_needed = True

        class _PriceEngine:
            def get_price(self, *args, **kwargs):
                del args, kwargs
                return {
                    "mid_price": Decimal("1.25"),
                    "dexie_price": Decimal("1.10"),
                    "tibet_price": Decimal("1.30"),
                    "arb_gap_bps": Decimal("1818.18"),
                }

        class _RiskManager:
            def update_arb_gap(self, arb_gap):
                del arb_gap

        loop.price_engine = _PriceEngine()
        loop.risk_manager = _RiskManager()
        stale_price_data = {
            "mid_price": Decimal("1.00"),
            "dexie_price": Decimal("0.95"),
            "tibet_price": Decimal("0.90"),
            "arb_gap_bps": Decimal("0"),
        }

        price_data, mid, arb_gap = loop._refresh_cycle_price_after_mempool_move(
            stale_price_data,
            Decimal("1.00"),
            Decimal("0"),
        )

        self.assertEqual(mid, Decimal("1.25"))
        self.assertEqual(arb_gap, Decimal("1818.18"))
        self.assertEqual(price_data["tibet_price"], Decimal("1.30"))
        self.assertEqual(price_data["dexie_price"], Decimal("1.10"))

    def test_handle_requoting_updates_baseline_to_anchored_mid(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6  # Must be > 5 to bypass the startup grace period
        loop._probe_state.update(
            {
                "active": False,
                "confirmed_price": Decimal("1.10"),
                "buy_price": Decimal("1.02"),
                "sell_price": Decimal("1.18"),
            }
        )
        loop._last_quoted_price["buy"] = Decimal("1.30")

        with contextlib.redirect_stdout(io.StringIO()):
            loop._handle_requoting(Decimal("1.10"), set(), set())

        self.assertEqual(
            loop.offer_manager.requote_calls[0][1],
            Decimal("1.02") / Decimal("0.97"),
        )
        self.assertEqual(
            loop.offer_manager.requote_calls[0][2]["price_cap"],
            Decimal("1.02") / Decimal("1.0001"),
        )
        self.assertEqual(
            loop._last_quoted_price["buy"],
            Decimal("1.02") / Decimal("0.97"),
        )

    def test_full_requote_allows_intentional_cancel_storm_guard_bypass(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6
        loop._last_quoted_price["buy"] = Decimal("1.30")

        with contextlib.redirect_stdout(io.StringIO()):
            loop._handle_requoting(
                Decimal("1.10"),
                current_buy_ids={f"buy-{idx}" for idx in range(24)},
                current_sell_ids={f"sell-{idx}" for idx in range(5)},
            )

        buy_call = next(
            call for call in loop.offer_manager.requote_calls if call[0] == "buy"
        )
        self.assertTrue(
            buy_call[2].get("force_cancel_storm"),
            "FULL/EMERGENCY requotes are deliberate side replacements and must not be blocked as accidental storms",
        )

    def test_recent_shock_requote_prevents_same_price_requote_loop(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6
        loop._last_quoted_price["buy"] = Decimal("1.30")
        loop._last_quoted_plain_mid["buy"] = Decimal("1.30")
        loop._last_shock_requote = {
            "buy": {
                "at": 1000.0,
                "mid_price": "1.10",
                "source": "emergency_requote",
                "severity": "emergency",
                "new_count": 16,
                "replaced_count": 16,
            },
            "sell": {},
        }
        events = []

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        with (
            patch.object(bot_loop.time, "time", return_value=1010.0),
            patch.object(bot_loop, "log_event", side_effect=_log_event),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            loop._handle_requoting(
                Decimal("1.10"),
                current_buy_ids={f"buy-{idx}" for idx in range(16)},
                current_sell_ids={f"sell-{idx}" for idx in range(24)},
            )

        self.assertEqual(
            [call for call in loop.offer_manager.requote_calls if call[0] == "buy"],
            [],
            "same-side Step 9 requote should not cancel fresh shock-requoted buys",
        )
        self.assertEqual(loop._last_quoted_price["buy"], Decimal("1.10"))
        self.assertFalse(loop._force_requote["buy"])
        self.assertTrue(
            any(
                event_type == "requote_deferred_recent_shock_requote"
                for _, event_type, _, _ in events
            )
        )

    def test_step9_requote_waits_for_opposite_side_pending_cancel_settle(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 6
        loop._last_quoted_price["sell"] = Decimal("1.30")
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": {"pending-buy"},
            "sell": set(),
        }
        events = []

        def _log_event(severity, event_type, message, data=None):
            events.append((severity, event_type, message, data or {}))

        with (
            patch.object(bot_loop, "log_event", side_effect=_log_event),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            loop._handle_requoting(
                Decimal("1.10"),
                current_buy_ids={"buy-live"},
                current_sell_ids={f"sell-{idx}" for idx in range(24)},
            )

        self.assertEqual(
            [call for call in loop.offer_manager.requote_calls if call[0] == "sell"],
            [],
            "Step 9 should not start a second side replacement while any side has pending cancels",
        )
        self.assertTrue(loop._force_requote["sell"])
        self.assertTrue(
            any(
                event_type == "requote_deferred_global_pending_cancel_settle"
                for _, event_type, _, _ in events
            )
        )


if __name__ == "__main__":
    unittest.main()
