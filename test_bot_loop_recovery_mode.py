import sys
import types
import unittest
from decimal import Decimal


class _FakeCfg:
    DRY_RUN = False
    ENABLE_BUY = True
    ENABLE_SELL = True
    AUTO_REQUOTE = True
    DEXIE_AUTO_POST = False
    SPLASH_ENABLED = False
    COIN_IDS_ENABLED = True
    CAT_ASSET_ID = "test-cat"
    CAT_DECIMALS = 3
    CAT_TICKER_ID = "MZ"
    MAX_ACTIVE_BUY_OFFERS = 40
    MAX_ACTIVE_SELL_OFFERS = 40
    MIN_EDGE_BPS = Decimal("300")
    ARB_ALERT_THRESHOLD_BPS = Decimal("200")
    SNIPER_CONFIRM_SECS = 30
    SNIPER_LINGER_SECS = 600
    SNIPER_POLL_SECS = 5
    SNIPER_TOP_BOOK_BPS = Decimal("1")
    SNIPER_RETRY_BACKOFF_BPS = Decimal("50")
    SNIPER_MAIN_BOOK_GUARD_BPS = Decimal("1")
    SNIPER_TOP_BOOK_BPS = Decimal("1")
    SNIPER_ENABLED = True
    SNIPER_MIN_GAP_BPS = Decimal("50")
    LOOP_SECONDS = 60
    WALLET_ID_XCH = 1
    CAT_WALLET_ID = 2


fake_config = types.ModuleType("config")
fake_config.cfg = _FakeCfg()
_ORIGINAL_MODULES = {
    name: sys.modules.get(name)
    for name in [
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
        "runtime_monitor",
        "wallet",
        "splash_receive",
        "amm_monitor",
        "bot_loop",
    ]
}
sys.modules["config"] = fake_config

fake_requests = types.ModuleType("requests")
fake_requests.Session = object
sys.modules["requests"] = fake_requests

fake_database = types.ModuleType("database")
fake_database.init_database = lambda: None
fake_database.log_event = lambda *args, **kwargs: None
fake_database.get_stats = lambda *args, **kwargs: {}
fake_database.get_offer = lambda *args, **kwargs: None
fake_database.update_offer_status = lambda *args, **kwargs: True
fake_database.update_offer_lifecycle_state = lambda *args, **kwargs: None
fake_database.transition_offer = lambda *args, **kwargs: None
fake_database.backfill_verified_fills_from_offers = lambda *args, **kwargs: 0
sys.modules["database"] = fake_database


class _DummyPriceEngine:
    def get_price(self, *args, **kwargs):
        return {"mid_price": Decimal("1.00")}


class _DummyOfferManager:
    def __init__(self):
        self.create_calls = []
        self.requote_calls = []
        self._recently_created = {}
        self._pending_cancel_retries = {}
        self._bot_cancelled_ids = set()

    def get_recently_created_count(self, side):
        del side
        return 0

    def get_replenishment_slots(self, side, total_slots, cat_asset_id=None, live_offer_ids=None):
        del side, cat_asset_id, live_offer_ids
        return list(range(total_slots))

    def create_ladder(self, mid_price, side, **kwargs):
        self.create_calls.append((side, mid_price, kwargs))
        return []

    def should_requote(self, side, current_price, last_quoted_price):
        del side, current_price, last_quoted_price
        return True

    def requote_side(self, side, current_price, **kwargs):
        self.requote_calls.append((side, current_price, kwargs))
        return []

    def sync_from_wallet(self):
        return [], [], []

    def get_wallet_sync_meta(self):
        return {"fresh": True, "using_cache": False}

    def prune_caches(self, active_ids):
        del active_ids

    def clean_visible_recently_created(self, ids):
        del ids

    def cancel_offers(self, trade_ids, reason="manual"):
        del reason
        return {tid: {"success": True} for tid in trade_ids}

    def is_bot_cancelled(self, trade_id):
        del trade_id
        return False


class _DummyFillTracker:
    def __init__(self, offer_manager=None):
        self.offer_manager = offer_manager

    def should_protect_side(self, side):
        del side
        return False

    def get_fill_counts(self):
        return {"buy": 0, "sell": 0}


class _DummyDexieManager:
    def queue_post(self, *args, **kwargs):
        pass

    def get_stats(self):
        return {}

    def prune_mappings(self, active_ids):
        del active_ids


class _DummySplashManager:
    def __init__(self):
        self._queue = []

    def queue_post(self, *args, **kwargs):
        pass

    def get_stats(self):
        return {}


class _DummySplashNode:
    def get_status(self):
        return {}


class _DummyCoinsetClient:
    def get_stats(self):
        return {}


class _DummyCoinManager:
    def __init__(self):
        self._price_engine = None
        self._reserve_ids_xch = set()
        self._reserve_ids_cat = set()
        self._tier_spares = {}
        self._xch_inventory = {"reserve": []}

    def is_busy(self):
        return False

    def snapshot_coins(self, reason):
        del reason

    def get_status(self):
        return {"inventory": {}}

    def get_trading_pace(self):
        return "normal"


class _DummyRiskManager:
    def __init__(self, price_engine=None, market_intel=None):
        del price_engine, market_intel
        self._boost_manager = None

    def should_enable_side(self, side, mid_price):
        del side, mid_price
        return True

    def get_adjusted_spread(self, side):
        del side
        return Decimal("0.08")

    def get_inventory_state(self):
        return {}


class _DummySniper:
    def __init__(self, offer_manager=None, risk_manager=None, dexie_manager=None,
                 splash_manager=None):
        del offer_manager, risk_manager, dexie_manager, splash_manager
        self._active_snipe_ids = []
        self._active_snipe_sides = {}

    def get_stats(self):
        return {}


class _DummyBoostManager:
    def __init__(self, offer_manager=None, dexie_manager=None, risk_manager=None):
        del offer_manager, dexie_manager, risk_manager
        self._boost_active = False


class _DummyMarketIntel:
    def __init__(self, price_engine=None):
        del price_engine
        self.orderbook = {}

    def get_stats(self):
        return {}

    def refresh_orderbook(self, force=False):
        del force
        return dict(self.orderbook)


class _DummyRuntimeMonitor:
    def __init__(self, loop):
        del loop

    def get_state(self):
        return {}


class _DummyAMMMonitor:
    def __init__(self, price_engine=None): pass
    def start(self): pass
    def stop(self): pass
    def is_available(self): return False
    def get_amm_price(self): return None
    def get_amm_state(self): return None
    def get_drift_bps(self): return None
    def get_stats(self): return {}
    def notify_quoted_price(self, buy=None, sell=None): pass
    def check_amm_buffer(self, price, side): return True


def _module_with_class(name, cls_name, cls):
    module = types.ModuleType(name)
    setattr(module, cls_name, cls)
    sys.modules[name] = module


_module_with_class("price_engine", "PriceEngine", _DummyPriceEngine)
_module_with_class("offer_manager", "OfferManager", _DummyOfferManager)
_module_with_class("fill_tracker", "FillTracker", _DummyFillTracker)
_module_with_class("dexie_manager", "DexieManager", _DummyDexieManager)
_module_with_class("splash_manager", "SplashManager", _DummySplashManager)
_module_with_class("splash_node", "SplashNode", _DummySplashNode)
_module_with_class("coinset_client", "CoinsetClient", _DummyCoinsetClient)
_module_with_class("coin_manager", "CoinManager", _DummyCoinManager)
_module_with_class("risk_manager", "RiskManager", _DummyRiskManager)
_module_with_class("sniper", "Sniper", _DummySniper)
_module_with_class("boost_manager", "BoostManager", _DummyBoostManager)
_module_with_class("market_intel", "MarketIntel", _DummyMarketIntel)
_module_with_class("runtime_monitor", "RuntimeMonitor", _DummyRuntimeMonitor)
_module_with_class("amm_monitor", "AMMMonitor", _DummyAMMMonitor)

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


class RecoveryModeTests(unittest.TestCase):
    def setUp(self):
        self.logged = []
        bot_loop.log_event = self._log_event

    def _log_event(self, severity, event_type, message, data=None):
        self.logged.append((severity, event_type, message, data))

    def test_recovery_mode_enters_after_persistent_under_target(self):
        loop = bot_loop.BotLoop()
        loop._running = True

        for _ in range(loop._recovery_under_target_cycles):
            loop._evaluate_recovery_mode(Decimal("1.0"), 27, 35)

        self.assertTrue(loop._recovery_state["active"])
        self.assertEqual(loop._bot_state["status"], "recovering")
        self.assertTrue(any(evt == "recovery_mode_enter" for _, evt, _, _ in self.logged))

    def test_recovery_mode_does_not_enter_on_stale_sync_without_deficit(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._wallet_sync_stale_cycle = True

        for _ in range(loop._recovery_wallet_stale_cycles + 1):
            loop._evaluate_recovery_mode(Decimal("1.0"), 40, 40)

        self.assertFalse(loop._recovery_state["active"])
        self.assertFalse(any(evt == "recovery_mode_enter" for _, evt, _, _ in self.logged))

    def test_recovery_mode_exits_after_healthy_cycles(self):
        loop = bot_loop.BotLoop()
        loop._running = True
        loop._enter_recovery_mode("book drift", 8, 5)

        loop._evaluate_recovery_mode(Decimal("1.0"), 40, 40)
        self.assertTrue(loop._recovery_state["active"])

        loop._evaluate_recovery_mode(Decimal("1.0"), 40, 40)
        self.assertFalse(loop._recovery_state["active"])
        self.assertEqual(loop._bot_state["status"], "running")
        self.assertTrue(any(evt == "recovery_mode_exit" for _, evt, _, _ in self.logged))

    def test_recovery_mode_skips_requotes(self):
        loop = bot_loop.BotLoop()
        loop._recovery_state["active"] = True

        loop._handle_requoting(Decimal("1.0"), set(), set())

        self.assertEqual(loop.offer_manager.requote_calls, [])
        self.assertTrue(any(evt == "requote_skip_recovery" for _, evt, _, _ in self.logged))

    def test_recovery_mode_marks_creation_stall_when_book_cannot_refill(self):
        loop = bot_loop.BotLoop()
        loop._recovery_state["active"] = True
        loop._wallet_sync_stale_cycle = False
        loop._create_offers_if_needed(
            Decimal("1.0"),
            0,
            0,
            current_buy_ids=set(),
            current_sell_ids=set(),
            arb_gap=Decimal("0"),
        )

        self.assertTrue(loop._recovery_state["cycle_create_stalled"])
        self.assertEqual(len(loop.offer_manager.create_calls), 2)


if __name__ == "__main__":
    unittest.main()
