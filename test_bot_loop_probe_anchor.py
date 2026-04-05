import sys
import types
import unittest
import io
import contextlib
from decimal import Decimal
from unittest.mock import patch


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
fake_database.get_offer = lambda *args, **kwargs: None
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

    def get_recently_created_count(self, side):
        return 0

    def get_replenishment_slots(self, side, total_slots, cat_asset_id=None, live_offer_ids=None):
        return list(range(total_slots))

    def create_ladder(self, mid_price, side, **kwargs):
        self.create_calls.append((side, mid_price, kwargs))
        return []

    def should_requote(self, side, current_price, last_quoted_price):
        return True

    def requote_side(self, side, current_price, **kwargs):
        self.requote_calls.append((side, current_price, kwargs))
        return []


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
    def __init__(self, offer_manager=None, risk_manager=None, dexie_manager=None,
                 splash_manager=None):
        self._active_snipe_ids = []
        self._active_snipe_sides = {}


class _DummyBoostManager:
    def __init__(self, offer_manager=None, dexie_manager=None, risk_manager=None):
        pass


class _DummyAMMMonitor:
    def __init__(self, price_engine=None):
        pass

    def start(self): pass
    def stop(self): pass
    def is_available(self): return False
    def get_amm_price(self): return None
    def get_amm_state(self): return None
    def get_drift_bps(self): return None
    def get_stats(self): return {}
    def notify_quoted_price(self, buy=None, sell=None): pass
    def check_amm_buffer(self, price, side): return True


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

    def test_full_ladder_does_not_rearm_missing_lingering_probe(self):
        loop = bot_loop.BotLoop()
        loop._running = False
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "confirmed_at": 1000.0,
            "launched_at": 990.0,
            "buy_tid": "probe-buy",
            "sell_tid": "probe-sell",
        })

        def _fail_revalidate(*args, **kwargs):
            raise AssertionError("confirmed probe revalidation should not run for a full ladder")

        loop._revalidate_confirmed_probe_edges = _fail_revalidate

        loop._create_offers_if_needed(
            Decimal("1.10"),
            current_buy_count=fake_config.cfg.MAX_ACTIVE_BUY_OFFERS,
            current_sell_count=fake_config.cfg.MAX_ACTIVE_SELL_OFFERS,
            current_buy_ids={"b1", "b2", "b3", "b4"},
            current_sell_ids={"s1", "s2", "s3", "s4"},
            arb_gap=Decimal("1"),
        )

    def test_probe_hold_requires_min_age_before_confirmation(self):
        loop = bot_loop.BotLoop()

        loop._probe_state.update({
            "active": True,
            "launched_at": 1000.0,
        })

        self.assertFalse(loop._probe_has_matured(now_ts=1025.0))
        self.assertTrue(loop._probe_has_matured(now_ts=1030.0))
        self.assertAlmostEqual(
            loop._probe_hold_seconds_remaining(now_ts=1012.5),
            17.5,
        )

    def test_probe_cleanup_linger_uses_latest_probe_anchor(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update({
            "active": False,
            "confirmed_at": 1000.0,
            "launched_at": 980.0,
        })

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
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
        })

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

    def test_market_aware_probe_prices_target_live_top_of_book(self):
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
            Decimal("0.999") * Decimal("1.0001"),
        )
        self.assertEqual(
            prices["sell_price"],
            Decimal("1.001") / Decimal("1.0001"),
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
        loop._probe_state.update({
            "active": True,
            "buy_tid": "probe-buy",
            "sell_tid": "probe-sell",
            "launched_at": 1000.0,
        })

        with patch.object(
            loop,
            "_refresh_live_offer_ids_from_wallet",
            side_effect=[
                ({"probe-buy"}, {"probe-sell"}),
                ({"probe-buy"}, {"probe-sell"}),
            ],
        ), patch.object(
            loop,
            "_probe_hold_seconds_remaining",
            side_effect=[25.0, 0.0],
        ), patch("bot_loop.time.sleep") as sleep_mock:
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
        loop._probe_state.update({
            "active": True,
            "buy_tid": "probe-buy",
            "sell_tid": "probe-sell",
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
            "tibet_price": Decimal("1.10"),
            "launched_at": 1000.0,
        })

        with patch.object(
            loop,
            "_watch_active_probe_window",
            return_value=({"probe-buy"}, {"probe-sell"}),
        ), patch.object(
            loop,
            "_probe_hold_seconds_remaining",
            return_value=0.0,
        ):
            result = loop._process_active_probe(set(), set(), Decimal("500"))

        self.assertFalse(loop._probe_state["active"])
        self.assertEqual(loop._probe_state["confirmed_price"], Decimal("1.10"))
        self.assertEqual(result["buy_ids"], {"probe-buy"})
        self.assertEqual(result["sell_ids"], {"probe-sell"})

    def test_create_offers_if_needed_applies_probe_anchor_per_side(self):
        loop = bot_loop.BotLoop()
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
        })

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
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "confirmed_at": 1000.0,
            "launched_at": 1000.0,
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
        })

        with patch("bot_loop.time.time", return_value=1100.0):
            loop._create_offers_if_needed(Decimal("1.10"), 0, 0)

        buy_call = next(call for call in loop.offer_manager.create_calls if call[0] == "buy")
        sell_call = next(call for call in loop.offer_manager.create_calls if call[0] == "sell")

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
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
        })

        def _fake_create_ladder(mid_price, side, **kwargs):
            return [{
                "offer_bech32": f"offer1-{side}",
                "trade_id": f"trade-{side}",
            }]

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
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "buy_tid": "probe-buy",
            "sell_tid": "probe-sell",
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
        })

        loop._create_offers_if_needed(
            Decimal("1.10"),
            current_buy_count=1,
            current_sell_count=1,
            current_buy_ids={"probe-buy"},
            current_sell_ids={"probe-sell"},
        )

        buy_call = next(call for call in loop.offer_manager.create_calls if call[0] == "buy")
        sell_call = next(call for call in loop.offer_manager.create_calls if call[0] == "sell")
        self.assertEqual(buy_call[2]["num_offers"], 4)
        self.assertEqual(sell_call[2]["num_offers"], 4)

    def test_handle_requoting_updates_baseline_to_anchored_mid(self):
        loop = bot_loop.BotLoop()
        loop._loop_count = 5
        loop._probe_state.update({
            "active": False,
            "confirmed_price": Decimal("1.10"),
            "buy_price": Decimal("1.02"),
            "sell_price": Decimal("1.18"),
        })
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


if __name__ == "__main__":
    unittest.main()
