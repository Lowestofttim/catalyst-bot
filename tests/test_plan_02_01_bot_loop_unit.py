"""Slice 02-01 — bot_loop.py unit tests: cycle gates, timer logic, pure helpers.

Covers functions not previously tested:
  _bps_to_pct, _get_live_offer_edges, _extract_open_offer_ids,
  _probe_hold_seconds_remaining, _probe_has_matured,
  _probe_cleanup_seconds_remaining, _confirmed_probe_slot_offsets,
  _apply_probe_retry_backoff, _get_sniper_launch_reason,
  _get_probe_price_boundary
"""

import sys
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Minimal module stubs — must be installed BEFORE importing bot_loop
# ---------------------------------------------------------------------------


class _FakeCfg:
    DRY_RUN = False
    ENABLE_BUY = True
    ENABLE_SELL = True
    AUTO_REQUOTE = True
    DEXIE_AUTO_POST = True
    SPLASH_ENABLED = True
    COIN_IDS_ENABLED = True
    CAT_ASSET_ID = "abc123cat"
    CAT_DECIMALS = 3
    CAT_TICKER_ID = "TST"
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
    SNIPER_REARM_PRICE_MOVE_BPS = "200"
    SNIPER_REARM_GAP_MOVE_BPS = "100"


_fake_cfg_instance = _FakeCfg()

_SAVED = {}
_STUB_NAMES = [
    "config",
    "requests",
    "database",
    "price_engine",
    "offer_manager",
    "fill_tracker",
    "dexie_manager",
    "splash_manager",
    "splash_node",
    "coinset_client",
    "coin_manager",
    "risk_manager",
    "sniper",
    "boost_manager",
    "market_intel",
    "wallet",
    "amm_monitor",
    "runtime_monitor",
    "splash_receive",
    "bot_loop",
]
for _n in _STUB_NAMES:
    _SAVED[_n] = sys.modules.get(_n)


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _cls_mod(name, cls_name, cls):
    m = types.ModuleType(name)
    setattr(m, cls_name, cls)
    sys.modules[name] = m


_fake_cfg_mod = _mod("config", cfg=_fake_cfg_instance)
_mod("requests", Session=object)
_mod(
    "database",
    init_database=lambda: None,
    log_event=lambda *a, **kw: None,
    get_events_since=lambda *a, **kw: [],
    get_open_offers=lambda *a, **kw: [],
    get_stats=lambda: {},
    get_offer=lambda *a, **kw: None,
    get_offers_by_trade_ids=lambda *a, **kw: [],
    update_offer_status=lambda *a, **kw: True,
    backfill_verified_fills_from_offers=lambda *a, **kw: 0,
)
_mod(
    "wallet",
    get_all_offers=lambda *a, **kw: [],
    classify_offers_from_list=lambda *a, **kw: ([], [], []),
    get_chia_health=lambda *a, **kw: {},
    cancel_offer=lambda *a, **kw: {},
    get_wallet_type=lambda: "sage",
)
_mod("splash_receive", classify_offer_for_asset=lambda *a, **kw: {})


class _PE:
    def get_price(self, *a, **kw):
        return {"mid_price": Decimal("1.10")}


class _OM:
    def __init__(self):
        pass

    def get_recently_created_count(self, side):
        return 0

    def get_replenishment_slots(self, side, total, **kw):
        return list(range(total))

    def create_ladder(self, *a, **kw):
        return []

    def should_requote(self, *a, **kw):
        return False

    def should_requote_graduated(self, *a, **kw):
        return None

    def requote_side(self, *a, **kw):
        return {"offers": [], "fully_replaced": True}

    def get_suspended_slot_count(self, side):
        return 0

    def unsuspend_slots_if_coins_available(self, side):
        pass

    def clean_visible_recently_created(self, *a, **kw):
        pass


class _FT:
    def __init__(self, offer_manager=None):
        pass

    def should_protect_side(self, side):
        return False


class _DM:
    def __init__(self):
        pass

    def queue_post(self, *a, **kw):
        pass

    def flush_queue(self):
        return {"posted": 0, "failed": 0, "skipped": 0}


class _SM:
    def __init__(self):
        pass

    def queue_post(self, *a, **kw):
        pass

    def flush_queue(self):
        return {"posted": 0, "failed": 0, "skipped": 0}


class _SN:
    pass


class _CC:
    pass


class _CM:
    def __init__(self):
        self._price_engine = None
        self.fee_pool = None

    def is_busy(self):
        return False

    def snapshot_coins(self, reason):
        pass

    def get_status(self):
        return {"inventory": {}}


class _RM:
    def __init__(self, price_engine=None, market_intel=None):
        self._boost_manager = None

    def should_enable_side(self, side, mid):
        return True

    def get_adjusted_spread(self, side):
        return Decimal("0.08")

    def update_arb_gap(self, gap):
        pass


class _Sniper:
    def __init__(self, **kw):
        self._active_snipe_ids = []
        self._active_snipe_sides = {}

    def prune_active_snipes(self, ids):
        pass


class _BM:
    def __init__(self, **kw):
        pass

    def prune_active_boosts(self, ids):
        pass


class _MI:
    def __init__(self, price_engine=None):
        self.orderbook = {}

    def refresh_orderbook(self, force=False):
        return dict(self.orderbook)


class _AMM:
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


class _RM2:
    def __init__(self, loop):
        pass

    def get_state(self):
        return {}


_cls_mod("price_engine", "PriceEngine", _PE)
_cls_mod("offer_manager", "OfferManager", _OM)
_cls_mod("fill_tracker", "FillTracker", _FT)
_cls_mod("dexie_manager", "DexieManager", _DM)
sys.modules["dexie_manager"].get_offer_detail = lambda *a, **kw: {"status": 0}
_cls_mod("splash_manager", "SplashManager", _SM)
_cls_mod("splash_node", "SplashNode", _SN)
_cls_mod("coinset_client", "CoinsetClient", _CC)
_cls_mod("coin_manager", "CoinManager", _CM)
_cls_mod("risk_manager", "RiskManager", _RM)
_cls_mod("sniper", "Sniper", _Sniper)
_cls_mod("boost_manager", "BoostManager", _BM)
_cls_mod("market_intel", "MarketIntel", _MI)
_cls_mod("amm_monitor", "AMMMonitor", _AMM)
_cls_mod("runtime_monitor", "RuntimeMonitor", _RM2)

sys.modules.pop("bot_loop", None)
import bot_loop  # noqa: E402

# Restore original modules so later test files see the real stacks
for _n, _orig in _SAVED.items():
    if _orig is None:
        sys.modules.pop(_n, None)
    else:
        sys.modules[_n] = _orig


# ---------------------------------------------------------------------------
# Helper: minimal BotLoop with cfg patched
# ---------------------------------------------------------------------------


def _make_loop():
    loop = bot_loop.BotLoop()
    loop._running = False
    return loop


class _PatchedCfg(unittest.TestCase):
    def setUp(self):
        self._p = patch.object(bot_loop, "cfg", _fake_cfg_instance)
        self._p.start()

    def tearDown(self):
        self._p.stop()


# ===========================================================================
# Tests
# ===========================================================================


class TestBpsToPct(_PatchedCfg):
    """_bps_to_pct — pure conversion utility."""

    def test_small_value_two_decimal_places(self):
        self.assertEqual(bot_loop._bps_to_pct(50), "0.50%")

    def test_large_value_one_decimal_place(self):
        self.assertEqual(bot_loop._bps_to_pct(200), "2.0%")

    def test_zero(self):
        self.assertEqual(bot_loop._bps_to_pct(0), "0.00%")

    def test_string_input(self):
        self.assertEqual(bot_loop._bps_to_pct("100"), "1.0%")

    def test_decimal_input(self):
        result = bot_loop._bps_to_pct(Decimal("75"))
        self.assertIn("%", result)

    def test_invalid_input_returns_string(self):
        result = bot_loop._bps_to_pct("not_a_number")
        self.assertIsInstance(result, str)


class TestPositionSanityDepositDetection(_PatchedCfg):
    def test_delta_matching_recent_unknown_cat_deposit_is_recognized(self):
        class _Conn:
            def execute(self, *_args, **_kwargs):
                return self

            def fetchall(self):
                return [
                    {
                        "coin_id": "0x65119c25b5bc049c2496a5791349c552269ff51483ada6bcb2bc68ae51ed08be",
                        "amount_mojos": 193_886_291,
                        "designation": "unknown",
                        "first_seen": "2026-05-02 14:40:07",
                    }
                ]

        fake_db = types.ModuleType("database")
        fake_db.get_connection = lambda: _Conn()
        fake_db.get_setting = lambda *_args, **_kwargs: ""

        with patch.dict(sys.modules, {"database": fake_db}):
            match = bot_loop._find_cat_deposit_for_position_delta(
                Decimal("193886.291"),
                Decimal("1000"),
                0,
            )

        self.assertIsNotNone(match)
        self.assertEqual(match["amount_mojos"], 193_886_291)

    def test_delta_matching_old_cat_deposit_before_baseline_is_ignored(self):
        class _Conn:
            def execute(self, *_args, **_kwargs):
                return self

            def fetchall(self):
                return [
                    {
                        "coin_id": "0xolddeposit",
                        "amount_mojos": 193_886_291,
                        "designation": "unknown",
                        "first_seen": "2026-05-02 14:40:07",
                    }
                ]

        fake_db = types.ModuleType("database")
        fake_db.get_connection = lambda: _Conn()
        fake_db.get_setting = lambda *_args, **_kwargs: ""
        baseline_at = datetime(2026, 5, 2, 14, 45, 0, tzinfo=timezone.utc).timestamp()

        with patch.dict(sys.modules, {"database": fake_db}):
            match = bot_loop._find_cat_deposit_for_position_delta(
                Decimal("193886.291"),
                Decimal("1000"),
                baseline_at,
            )

        self.assertIsNone(match)


class TestBotLoopWiring(_PatchedCfg):
    """BotLoop wiring used by dashboard market health."""

    def test_risk_manager_gets_live_bot_reference(self):
        loop = _make_loop()

        self.assertIs(loop.risk_manager._bot_ref, loop)

    def test_sage_wires_coinset_for_chain_truth_without_inventory_init(self):
        loop = _make_loop()

        class Client:
            def __init__(self):
                self.initialized = False

            def initialize_puzzle_hashes(self):
                self.initialized = True
                return True

        client = Client()
        loop.coinset_client = client
        loop.coin_manager = types.SimpleNamespace()

        with (
            patch.object(bot_loop.cfg, "COINSET_ENABLED", True, create=True),
            patch.object(bot_loop, "log_event") as log_event,
        ):
            loop._initialize_coinset_for_wallet_type("sage")

        self.assertIs(loop.coin_manager._coinset_client, client)
        self.assertFalse(client.initialized)
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("coinset_ready", event_types)

    def test_record_live_offer_edges_updates_cache_and_gui_state(self):
        loop = _make_loop()
        edges = {"our_best_bid": "1.00", "our_best_ask": "1.05"}

        loop._record_live_offer_edges(edges)

        self.assertEqual(loop._last_live_offer_edges, edges)
        self.assertEqual(loop._bot_state["our_best_bid"], "1.00")
        self.assertEqual(loop._bot_state["our_best_ask"], "1.05")

    def test_toxicity_cancel_disabled_leaves_live_offers_on_book(self):
        loop = _make_loop()
        calls = []

        class Snapshot:
            score = 95
            level = "extreme"
            suggested_action = "Throttle new buy offers"
            throttled_sides = ("buy",)

            def is_side_throttled(self, side, now):
                return side == "buy"

            def to_dict(self):
                return {
                    "score": self.score,
                    "level": self.level,
                    "throttled_sides": list(self.throttled_sides),
                }

        def cancel_offers(trade_ids, **kwargs):
            calls.append((tuple(trade_ids), kwargs))
            return {tid: {"success": True} for tid in trade_ids}

        loop.offer_manager = types.SimpleNamespace(cancel_offers=cancel_offers)

        with (
            patch.object(bot_loop.cfg, "TOXICITY_CANCEL_ENABLED", False, create=True),
            patch.object(
                bot_loop.cfg,
                "MARKET_TOXICITY_CANCEL_LIVE_OFFERS",
                True,
                create=True,
            ),
            patch.object(bot_loop, "log_event"),
            patch.object(bot_loop.time, "time", return_value=1000),
        ):
            cancelled = loop._cancel_toxicity_throttled_offers(
                Snapshot(),
                current_buy_ids={"buy-1", "buy-2"},
                current_sell_ids={"sell-1"},
            )

        self.assertEqual(cancelled["buy"], set())
        self.assertEqual(cancelled["sell"], set())
        self.assertEqual(calls, [])

    def test_toxicity_cancel_enabled_cancels_live_offers_for_throttled_side(self):
        loop = _make_loop()
        calls = []

        class Snapshot:
            score = 95
            level = "extreme"
            suggested_action = "Throttle new buy offers"
            throttled_sides = ("buy",)

            def is_side_throttled(self, side, now):
                return side == "buy"

            def to_dict(self):
                return {
                    "score": self.score,
                    "level": self.level,
                    "throttled_sides": list(self.throttled_sides),
                }

        def cancel_offers(trade_ids, **kwargs):
            calls.append((tuple(trade_ids), kwargs))
            return {tid: {"success": True} for tid in trade_ids}

        loop.offer_manager = types.SimpleNamespace(cancel_offers=cancel_offers)
        loop._pending_cancel_wallet_ids_by_side = {
            "buy": {"pending-buy"},
            "sell": set(),
        }

        with (
            patch.object(
                bot_loop.cfg,
                "TOXICITY_CANCEL_ENABLED",
                True,
                create=True,
            ),
            patch.object(bot_loop, "log_event"),
            patch.object(bot_loop.time, "time", return_value=1000),
        ):
            cancelled = loop._cancel_toxicity_throttled_offers(
                Snapshot(),
                current_buy_ids={"buy-1", "buy-2", "pending-buy"},
                current_sell_ids={"sell-1"},
            )

        self.assertEqual(cancelled["buy"], {"buy-1", "buy-2"})
        self.assertEqual(cancelled["sell"], set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0][0]), {"buy-1", "buy-2"})
        self.assertEqual(calls[0][1]["reason"], "market_toxicity_guard")
        self.assertTrue(calls[0][1]["skip_confirmation"])
        self.assertTrue(calls[0][1]["force_storm"])


class TestTierSizeDriftTopup(_PatchedCfg):
    def test_tier_size_drift_uses_proactive_topup_threshold(self):
        loop = _make_loop()
        calls = []
        alerts = []

        class DriftCoinManager:
            def check_tier_size_drift(self):
                return [
                    {"side": "cat", "tier": "inner", "ratio": 0.98, "coin_count": 2}
                ]

            def start_topup(self, active_buy, active_sell, is_drip=None):
                calls.append((active_buy, active_sell, is_drip))
                return True

        fake_coin_manager = types.ModuleType("coin_manager")
        fake_coin_manager.reclassify_tier_spare_coins = lambda: None
        fake_database = types.ModuleType("database")
        fake_database.get_open_offers = lambda cat_asset_id=None: [
            {"side": "buy"},
            {"side": "sell"},
            {"side": "sell"},
        ]

        loop.coin_manager = DriftCoinManager()
        loop._last_tier_drift_topup_time = 0
        loop._emit_alert = lambda *a, **kw: alerts.append(a)
        loop._clear_alert = lambda *a, **kw: None

        with (
            patch.dict(
                sys.modules,
                {
                    "coin_manager": fake_coin_manager,
                    "database": fake_database,
                },
            ),
            patch.object(bot_loop, "log_event"),
        ):
            loop._check_tier_size_drift()

        self.assertEqual(calls, [(1, 2, True)])
        self.assertTrue(alerts)
        self.assertIn("Live topup has been queued", alerts[-1][3])
        self.assertNotIn("Live topup has started to reshape", alerts[-1][3])

    def test_tier_size_drift_waits_when_only_side_has_no_split_source(self):
        loop = _make_loop()
        calls = []

        class DriftCoinManager:
            def check_tier_size_drift(self):
                return [
                    {
                        "side": "cat",
                        "tier": "inner",
                        "ratio": 0.89,
                        "coin_count": 8,
                        "live_size_mojos": 1000,
                    }
                ]

            def _optional_topup_source_available(
                self, wallet_type, target_size_mojos=0
            ):
                return False

            def start_topup(self, active_buy, active_sell, is_drip=None):
                calls.append((active_buy, active_sell, is_drip))
                return True

        fake_coin_manager = types.ModuleType("coin_manager")
        fake_coin_manager.reclassify_tier_spare_coins = lambda: None
        fake_database = types.ModuleType("database")
        fake_database.get_open_offers = lambda cat_asset_id=None: [
            {"side": "buy"},
            {"side": "sell"},
        ]

        loop.coin_manager = DriftCoinManager()
        loop._emit_alert = lambda *a, **kw: None
        loop._clear_alert = lambda *a, **kw: None

        with (
            patch.dict(
                sys.modules,
                {
                    "coin_manager": fake_coin_manager,
                    "database": fake_database,
                },
            ),
            patch.object(bot_loop, "log_event") as log_event,
        ):
            loop._check_tier_size_drift()

        events = [call.args[1] for call in log_event.call_args_list]
        self.assertEqual(calls, [])
        self.assertIn("tier_size_drift_waiting_for_source", events)
        self.assertNotIn("tier_size_drift_topup_started", events)

    def test_sage_cleanup_skip_summary_is_user_friendly(self):
        msg = bot_loop._format_sage_cleanup_skip_summary(
            total=655,
            new=655,
            repeated=0,
        )

        self.assertIn("historical Sage offer records", msg)
        self.assertIn("left them visible in Sage", msg)
        self.assertNotIn("anomal", msg.lower())
        self.assertNotIn("review", msg.lower())


class TestCoinTopupPriority(_PatchedCfg):
    def test_proactive_topup_waits_when_missing_offers_have_spares(self):
        loop = _make_loop()
        loop._loop_count = 11
        loop._current_mid_price = Decimal("1")
        loop._startup_coin_recheck_done = True
        calls = []

        class DripCoinManager:
            _topup_is_drip = True
            _reconcile_counter = 0

            def is_busy(self):
                return False

            def check_coin_prep_status(self):
                return {}

            def update_coin_counts(self):
                return None

            def needs_coin_prep(self, active_buy, active_sell):
                return False

            def needs_topup(self, active_buy, active_sell):
                self._topup_is_drip = True
                return True

            def start_topup(self, active_buy, active_sell, is_drip=None):
                calls.append((active_buy, active_sell, is_drip))

            def check_runtime_health(self, active_buy, active_sell):
                return False

        loop.coin_manager = DripCoinManager()
        loop._reclaim_oversized_locked_offers = lambda: False
        loop._get_expected_offer_targets = lambda mid_price: {"buy": 4, "sell": 4}
        loop._available_tier_spares_for_side = lambda side: 5

        with patch.object(bot_loop, "log_event") as log_event:
            loop._handle_coins(active_buy_count=4, active_sell_count=2)

        events = [call.args[1] for call in log_event.call_args_list]
        self.assertEqual(calls, [])
        self.assertIn("topup_deferred_offer_rebuild_priority", events)

    def test_spare_topup_waits_when_book_full_and_no_source(self):
        loop = _make_loop()
        loop._loop_count = 11
        loop._current_mid_price = Decimal("1")
        loop._startup_coin_recheck_done = True
        calls = []

        class SpareOnlyCoinManager:
            _topup_is_drip = False
            _topup_needed_wallet_types = {"cat"}
            _reconcile_counter = 0

            def is_busy(self):
                return False

            def check_coin_prep_status(self):
                return {}

            def update_coin_counts(self):
                return None

            def needs_coin_prep(self, active_buy, active_sell):
                return False

            def needs_topup(self, active_buy, active_sell):
                self._topup_is_drip = False
                return True

            def start_topup(self, active_buy, active_sell, is_drip=None):
                calls.append((active_buy, active_sell, is_drip))

            def check_runtime_health(self, active_buy, active_sell):
                return False

            def _optional_topup_source_available(
                self, wallet_type, target_size_mojos=0
            ):
                return False

        loop.coin_manager = SpareOnlyCoinManager()
        loop._reclaim_oversized_locked_offers = lambda: False
        loop._get_expected_offer_targets = lambda mid_price: {"buy": 4, "sell": 4}

        with patch.object(bot_loop, "log_event") as log_event:
            loop._handle_coins(active_buy_count=4, active_sell_count=4)

        events = [call.args[1] for call in log_event.call_args_list]
        self.assertEqual(calls, [])
        self.assertIn("topup_waiting_for_source", events)

    def test_spare_topup_runs_when_missing_offers_need_help(self):
        loop = _make_loop()
        loop._loop_count = 11
        loop._current_mid_price = Decimal("1")
        loop._startup_coin_recheck_done = True
        calls = []

        class CriticalCoinManager:
            _topup_is_drip = False
            _topup_needed_wallet_types = {"cat"}
            _reconcile_counter = 0

            def is_busy(self):
                return False

            def check_coin_prep_status(self):
                return {}

            def update_coin_counts(self):
                return None

            def needs_coin_prep(self, active_buy, active_sell):
                return False

            def needs_topup(self, active_buy, active_sell):
                self._topup_is_drip = False
                return True

            def start_topup(self, active_buy, active_sell, is_drip=None):
                calls.append((active_buy, active_sell, is_drip))

            def check_runtime_health(self, active_buy, active_sell):
                return False

            def _optional_topup_source_available(
                self, wallet_type, target_size_mojos=0
            ):
                return False

        loop.coin_manager = CriticalCoinManager()
        loop._reclaim_oversized_locked_offers = lambda: False
        loop._get_expected_offer_targets = lambda mid_price: {"buy": 4, "sell": 4}
        loop._available_tier_spares_for_side = lambda side: 0

        with patch.object(bot_loop, "log_event"):
            loop._handle_coins(active_buy_count=4, active_sell_count=3)

        self.assertEqual(calls, [(4, 3, None)])

    def test_spare_topup_defers_when_toxicity_sets_targets_to_zero(self):
        loop = _make_loop()
        loop._loop_count = 11
        loop._current_mid_price = Decimal("1")
        loop._startup_coin_recheck_done = True
        calls = []

        class ToxicityTopupCoinManager:
            _topup_is_drip = False
            _topup_needed_wallet_types = {"xch", "cat"}
            _reconcile_counter = 0

            def is_busy(self):
                return False

            def check_coin_prep_status(self):
                return {}

            def update_coin_counts(self):
                return None

            def needs_coin_prep(self, active_buy, active_sell):
                return False

            def needs_topup(self, active_buy, active_sell):
                return True

            def start_topup(self, active_buy, active_sell, is_drip=None):
                calls.append((active_buy, active_sell, is_drip))

            def check_runtime_health(self, active_buy, active_sell):
                return False

        loop.coin_manager = ToxicityTopupCoinManager()
        loop._reclaim_oversized_locked_offers = lambda: False
        loop._get_expected_offer_targets = lambda mid_price: {"buy": 0, "sell": 0}

        with patch.object(bot_loop, "log_event") as log_event:
            loop._handle_coins(active_buy_count=15, active_sell_count=0)

        events = [call.args[1] for call in log_event.call_args_list]
        self.assertEqual(calls, [])
        self.assertIn("topup_deferred_zero_offer_targets", events)


class TestGetLiveOfferEdges(_PatchedCfg):
    """BotLoop._get_live_offer_edges — static method, extracts best bid/ask."""

    def test_empty_lists_return_zero_strings(self):
        result = bot_loop.BotLoop._get_live_offer_edges([], [])
        self.assertEqual(result["our_best_bid"], "0")
        self.assertEqual(result["our_best_ask"], "0")

    def test_best_bid_is_max_buy_price(self):
        buys = [
            {"price_xch": "1.05"},
            {"price_xch": "1.10"},
            {"price_xch": "1.08"},
        ]
        result = bot_loop.BotLoop._get_live_offer_edges(buys, [])
        self.assertEqual(Decimal(result["our_best_bid"]), Decimal("1.10"))

    def test_best_ask_is_min_positive_sell_price(self):
        sells = [
            {"price_xch": "1.15"},
            {"price_xch": "1.20"},
            {"price_xch": "1.12"},
        ]
        result = bot_loop.BotLoop._get_live_offer_edges([], sells)
        self.assertEqual(Decimal(result["our_best_ask"]), Decimal("1.12"))

    def test_zero_sell_prices_excluded(self):
        sells = [{"price_xch": "0"}, {"price_xch": "1.20"}]
        result = bot_loop.BotLoop._get_live_offer_edges([], sells)
        self.assertEqual(Decimal(result["our_best_ask"]), Decimal("1.20"))

    def test_none_prices_excluded(self):
        buys = [{"price_xch": None}, {"price_xch": "1.05"}]
        result = bot_loop.BotLoop._get_live_offer_edges(buys, [])
        self.assertEqual(Decimal(result["our_best_bid"]), Decimal("1.05"))

    def test_missing_wallet_prices_are_enriched_from_db_trade_ids(self):
        buys = [
            {"trade_id": "buy-inner", "price_xch": None},
            {"trade_id": "buy-outer", "price_xch": "0.90"},
        ]
        sells = [
            {"trade_id": "sell-inner", "price_xch": ""},
            {"trade_id": "sell-outer", "price_xch": "1.30"},
        ]
        db_rows = [
            {"trade_id": "buy-inner", "side": "buy", "price_xch": "1.05"},
            {"trade_id": "sell-inner", "side": "sell", "price_xch": "1.10"},
        ]

        with patch.object(bot_loop, "get_offers_by_trade_ids", return_value=db_rows):
            result = bot_loop.BotLoop._get_live_offer_edges(buys, sells)

        self.assertEqual(Decimal(result["our_best_bid"]), Decimal("1.05"))
        self.assertEqual(Decimal(result["our_best_ask"]), Decimal("1.10"))

    def test_none_inputs_treated_as_empty(self):
        result = bot_loop.BotLoop._get_live_offer_edges(None, None)
        self.assertEqual(result["our_best_bid"], "0")
        self.assertEqual(result["our_best_ask"], "0")


class TestExtractOpenOfferIds(_PatchedCfg):
    """BotLoop._extract_open_offer_ids — classifies wallet offers into buy/sell sets."""

    def _make_offer(self, trade_id, offered_keys, requested_keys):
        return {
            "trade_id": trade_id,
            "summary": {
                "offered": {k: 1 for k in offered_keys},
                "requested": {k: 1 for k in requested_keys},
            },
        }

    def test_buy_offer_classified_correctly(self):
        loop = _make_loop()
        asset_id = _fake_cfg_instance.CAT_ASSET_ID
        offers = [self._make_offer("buy1", ["xch"], [asset_id])]
        buys, sells = loop._extract_open_offer_ids(offers)
        self.assertIn("buy1", buys)
        self.assertNotIn("buy1", sells)

    def test_sell_offer_classified_correctly(self):
        loop = _make_loop()
        asset_id = _fake_cfg_instance.CAT_ASSET_ID
        offers = [self._make_offer("sell1", [asset_id], ["xch"])]
        buys, sells = loop._extract_open_offer_ids(offers)
        self.assertIn("sell1", sells)
        self.assertNotIn("sell1", buys)

    def test_empty_list_returns_empty_sets(self):
        loop = _make_loop()
        buys, sells = loop._extract_open_offer_ids([])
        self.assertEqual(buys, set())
        self.assertEqual(sells, set())

    def test_none_input_returns_empty_sets(self):
        loop = _make_loop()
        buys, sells = loop._extract_open_offer_ids(None)
        self.assertEqual(buys, set())
        self.assertEqual(sells, set())

    def test_unrelated_offer_not_classified(self):
        loop = _make_loop()
        offers = [self._make_offer("other1", ["usdc"], ["eth"])]
        buys, sells = loop._extract_open_offer_ids(offers)
        self.assertNotIn("other1", buys)
        self.assertNotIn("other1", sells)

    def test_offer_without_trade_id_skipped(self):
        loop = _make_loop()
        asset_id = _fake_cfg_instance.CAT_ASSET_ID
        offer = {
            "trade_id": "",
            "summary": {"offered": {"xch": 1}, "requested": {asset_id: 1}},
        }
        buys, sells = loop._extract_open_offer_ids([offer])
        self.assertEqual(buys, set())


class TestRequoteFailureBackoff(_PatchedCfg):
    """Emergency/forced requote failure backoff helpers."""

    def test_set_backoff_clears_force_flag(self):
        loop = _make_loop()
        loop._force_requote["buy"] = True
        with patch.object(
            _fake_cfg_instance, "REQUOTE_FAILURE_BACKOFF_SECS", 12, create=True
        ):
            with patch.object(bot_loop.time, "time", return_value=100.0):
                loop._set_requote_failure_backoff("buy", "unit_test")
        self.assertFalse(loop._force_requote["buy"])
        with patch.object(bot_loop.time, "time", return_value=105.0):
            self.assertAlmostEqual(loop._requote_backoff_remaining("buy"), 7.0)

    def test_expired_backoff_returns_zero(self):
        loop = _make_loop()
        loop._requote_failure_backoff_until["sell"] = 100.0
        with patch.object(bot_loop.time, "time", return_value=101.0):
            self.assertEqual(loop._requote_backoff_remaining("sell"), 0.0)


class TestProbeHoldTimer(_PatchedCfg):
    """_probe_hold_seconds_remaining and _probe_has_matured — confirm timer."""

    def test_newly_launched_probe_has_remaining_time(self):
        loop = _make_loop()
        now = 1000.0
        probe = {"launched_at": now}
        remaining = loop._probe_hold_seconds_remaining(probe, now_ts=now)
        self.assertAlmostEqual(remaining, 30.0, places=1)

    def test_aged_probe_returns_zero_remaining(self):
        loop = _make_loop()
        probe = {"launched_at": 1000.0}
        remaining = loop._probe_hold_seconds_remaining(probe, now_ts=1035.0)
        self.assertEqual(remaining, 0.0)

    def test_not_matured_when_recent(self):
        loop = _make_loop()
        probe = {"launched_at": 1000.0}
        self.assertFalse(loop._probe_has_matured(probe, now_ts=1010.0))

    def test_matured_when_old_enough(self):
        loop = _make_loop()
        probe = {"launched_at": 1000.0}
        self.assertTrue(loop._probe_has_matured(probe, now_ts=1031.0))

    def test_zero_confirm_secs_is_always_matured(self):
        loop = _make_loop()
        with patch.object(bot_loop, "cfg") as mock_cfg:
            mock_cfg.SNIPER_CONFIRM_SECS = 0
            mock_cfg.SNIPER_LINGER_SECS = 600
            probe = {"launched_at": 1000.0}
            self.assertTrue(loop._probe_has_matured(probe, now_ts=1000.5))


class TestProbeCleanupTimer(_PatchedCfg):
    """_probe_cleanup_seconds_remaining — linger countdown."""

    def test_confirmed_probe_within_linger_has_positive_remaining(self):
        loop = _make_loop()
        probe = {"confirmed_at": 1000.0, "launched_at": 990.0}
        remaining = loop._probe_cleanup_seconds_remaining(probe, now_ts=1100.0)
        # 600s linger, 100s elapsed → 500s remaining
        self.assertAlmostEqual(remaining, 500.0, delta=1.0)

    def test_expired_linger_returns_zero(self):
        loop = _make_loop()
        probe = {"confirmed_at": 1000.0, "launched_at": 990.0}
        remaining = loop._probe_cleanup_seconds_remaining(probe, now_ts=1700.0)
        self.assertEqual(remaining, 0.0)

    def test_no_anchor_returns_full_linger(self):
        loop = _make_loop()
        probe = {}
        remaining = loop._probe_cleanup_seconds_remaining(probe, now_ts=9999.0)
        self.assertEqual(remaining, 600.0)

    def test_zero_linger_returns_zero(self):
        loop = _make_loop()
        with patch.object(bot_loop, "cfg") as mock_cfg:
            mock_cfg.SNIPER_CONFIRM_SECS = 30
            mock_cfg.SNIPER_LINGER_SECS = 0
            probe = {"confirmed_at": 1000.0}
            self.assertEqual(
                loop._probe_cleanup_seconds_remaining(probe, now_ts=1001.0), 0.0
            )


class TestConfirmedProbeSlotOffsets(_PatchedCfg):
    """_confirmed_probe_slot_offsets — exclude lingering probe from slot count."""

    def test_active_probe_no_offsets(self):
        loop = _make_loop()
        loop._probe_state = {"active": True, "buy_tid": "b1", "sell_tid": "s1"}
        offsets = loop._confirmed_probe_slot_offsets({"b1"}, {"s1"})
        self.assertEqual(offsets, {"buy": 0, "sell": 0})

    def test_confirmed_probe_tid_in_ids_offsets_1(self):
        loop = _make_loop()
        loop._probe_state = {
            "active": False,
            "buy_tid": "probe-buy",
            "sell_tid": "probe-sell",
        }
        offsets = loop._confirmed_probe_slot_offsets(
            {"probe-buy", "b2"}, {"probe-sell", "s2"}
        )
        self.assertEqual(offsets["buy"], 1)
        self.assertEqual(offsets["sell"], 1)

    def test_confirmed_probe_tid_not_in_ids_no_offset(self):
        loop = _make_loop()
        loop._probe_state = {
            "active": False,
            "buy_tid": "probe-buy",
            "sell_tid": "probe-sell",
        }
        offsets = loop._confirmed_probe_slot_offsets({"b1", "b2"}, {"s1", "s2"})
        self.assertEqual(offsets, {"buy": 0, "sell": 0})

    def test_none_id_sets_skips_check(self):
        loop = _make_loop()
        loop._probe_state = {"active": False, "buy_tid": "probe-buy"}
        offsets = loop._confirmed_probe_slot_offsets(None, None)
        self.assertEqual(offsets, {"buy": 0, "sell": 0})


class TestApplyProbeRetryBackoff(_PatchedCfg):
    """_apply_probe_retry_backoff — step probe price away from filled edge."""

    def test_buy_side_caps_candidate_below_previous(self):
        loop = _make_loop()
        # buy: candidate should be pushed down from previous (50bps)
        # previous=1.10 → stepped = 1.10 / 1.005 ≈ 1.0945
        result = loop._apply_probe_retry_backoff(
            "buy", Decimal("1.12"), Decimal("1.10")
        )
        self.assertLess(result, Decimal("1.10"))

    def test_sell_side_pushes_candidate_above_previous(self):
        loop = _make_loop()
        # sell: candidate should be pushed up from previous (50bps)
        # previous=1.10 → stepped = 1.10 * 1.005 = 1.1055
        result = loop._apply_probe_retry_backoff(
            "sell", Decimal("1.08"), Decimal("1.10")
        )
        self.assertGreater(result, Decimal("1.10"))

    def test_zero_candidate_returns_zero(self):
        loop = _make_loop()
        result = loop._apply_probe_retry_backoff("buy", Decimal("0"), Decimal("1.10"))
        self.assertEqual(result, Decimal("0"))

    def test_zero_previous_returns_candidate_unchanged(self):
        loop = _make_loop()
        result = loop._apply_probe_retry_backoff("buy", Decimal("1.05"), Decimal("0"))
        self.assertEqual(result, Decimal("1.05"))

    def test_buy_candidate_already_below_stepped_unchanged(self):
        loop = _make_loop()
        # candidate=1.08 already below stepped=1.0945, so min keeps it
        result = loop._apply_probe_retry_backoff(
            "buy", Decimal("1.08"), Decimal("1.10")
        )
        self.assertLessEqual(result, Decimal("1.10"))


class TestGetSniperLaunchReason(_PatchedCfg):
    """_get_sniper_launch_reason — rearm threshold decision."""

    def test_empty_book_always_launches(self):
        loop = _make_loop()
        reason = loop._get_sniper_launch_reason(Decimal("1.10"), Decimal("200"))
        self.assertEqual(reason, "startup_empty_book")

    def test_price_moved_enough_triggers_launch(self):
        loop = _make_loop()
        # last_mid=1.00, new_mid=1.03 → 300bps > threshold 200bps
        loop._probe_state = {
            "last_discovery_mid_price": "1.00",
            "last_discovery_arb_gap_bps": "500",
        }
        reason = loop._get_sniper_launch_reason(
            Decimal("1.03"),
            Decimal("500"),
            current_buy_ids={"b1"},
            current_sell_ids={"s1"},
        )
        self.assertIsNotNone(reason)
        self.assertIn("price_move", reason)

    def test_price_unchanged_no_launch(self):
        loop = _make_loop()
        # Same price, same gap — no rearm
        loop._probe_state = {
            "last_discovery_mid_price": "1.10",
            "last_discovery_arb_gap_bps": "300",
        }
        reason = loop._get_sniper_launch_reason(
            Decimal("1.10"),
            Decimal("300"),
            current_buy_ids={"b1"},
            current_sell_ids={"s1"},
        )
        self.assertIsNone(reason)

    def test_arb_gap_shift_triggers_launch(self):
        loop = _make_loop()
        # last_gap=100, new_gap=210 → shift=110 > threshold 100
        loop._probe_state = {
            "last_discovery_mid_price": "1.10",
            "last_discovery_arb_gap_bps": "100",
        }
        reason = loop._get_sniper_launch_reason(
            Decimal("1.10"),
            Decimal("210"),
            current_buy_ids={"b1"},
            current_sell_ids={"s1"},
        )
        self.assertIsNotNone(reason)
        self.assertIn("arb_gap_shift", reason)


class TestGetProbePriceBoundary(_PatchedCfg):
    """_get_probe_price_boundary — main-book guard calculation."""

    def test_active_probe_returns_none(self):
        loop = _make_loop()
        loop._probe_state = {"active": True, "buy_price": "1.10"}
        self.assertIsNone(loop._get_probe_price_boundary("buy"))

    def test_expired_linger_returns_none(self):
        loop = _make_loop()
        # linger expired: confirmed_at very old
        loop._probe_state = {"active": False, "confirmed_at": 1.0, "buy_price": "1.10"}
        self.assertIsNone(loop._get_probe_price_boundary("buy"))

    def test_buy_boundary_is_below_edge(self):
        loop = _make_loop()
        import time

        now = time.time()
        loop._probe_state = {
            "active": False,
            "confirmed_at": now - 10,
            "buy_price": "1.10",
            "sell_price": "1.15",
        }
        boundary = loop._get_probe_price_boundary("buy")
        self.assertIsNotNone(boundary)
        self.assertLess(boundary, Decimal("1.10"))

    def test_sell_boundary_is_above_edge(self):
        loop = _make_loop()
        import time

        now = time.time()
        loop._probe_state = {
            "active": False,
            "confirmed_at": now - 10,
            "buy_price": "1.10",
            "sell_price": "1.15",
        }
        boundary = loop._get_probe_price_boundary("sell")
        self.assertIsNotNone(boundary)
        self.assertGreater(boundary, Decimal("1.15"))

    def test_zero_edge_price_returns_none(self):
        loop = _make_loop()
        import time

        now = time.time()
        loop._probe_state = {
            "active": False,
            "confirmed_at": now - 10,
            "buy_price": "0",
        }
        self.assertIsNone(loop._get_probe_price_boundary("buy"))


if __name__ == "__main__":
    unittest.main()
