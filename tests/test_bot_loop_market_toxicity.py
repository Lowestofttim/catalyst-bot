from decimal import Decimal
from types import SimpleNamespace

import bot_loop
from market_toxicity import ToxicitySnapshot


class _FakeGuard:
    def __init__(self):
        self.last_context = None
        self.snapshot = ToxicitySnapshot(score=42, buy_score=42, level="mild")
        self.reset_called = False

    def update(self, context):
        self.last_context = context
        return self.snapshot

    def reset(self):
        self.reset_called = True
        self.snapshot = ToxicitySnapshot()


class _FakeRiskManager:
    def __init__(self):
        self.snapshot = None
        self._net_position_cat = Decimal("12")
        self.reset_session_called = False

    def set_market_toxicity(self, snapshot):
        self.snapshot = snapshot

    def reset_session(self):
        self.reset_session_called = True


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStartupEvent:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


class _FakeMarketIntel:
    def get_market_summary(self):
        return {
            "thin_side": "buy",
            "buy_depth_xch": "0.2",
            "sell_depth_xch": "3.0",
            "orderbook_refreshes": 3,
            "orderbook_age_secs": 5,
        }

    def get_orderbook_snapshot(self):
        return {
            "buy_count": 5,
            "sell_count": 7,
            "buy_truncated": False,
            "sell_truncated": False,
        }


class _FakeCoinManager:
    def __init__(self):
        self._xch_inventory = {"trading": [{"amount": 500_000_000_000}]}
        self._cat_inventory = {"trading": [{"amount": 100_000}]}

    def get_status(self):
        return {
            "inventory": {
                "xch_locked_amount_raw": 200_000_000_000,
                "cat_locked_amount_raw": 50_000,
            }
        }

    def get_inventory_summary(self):
        return {
            "xch_locked_amount_raw": 200_000_000_000,
            "cat_locked_amount_raw": 50_000,
        }


def _loop():
    loop = object.__new__(bot_loop.BotLoop)
    loop.market_toxicity_guard = _FakeGuard()
    loop.risk_manager = _FakeRiskManager()
    loop.market_intel = _FakeMarketIntel()
    loop.coin_manager = _FakeCoinManager()
    loop.offer_manager = SimpleNamespace(_recently_created={"fill-buy": 990.0})
    loop._recent_sweep_events = [{"side": "buy", "fill_count": 2}]
    loop._bot_state = {}
    loop._state_lock = SimpleNamespace(
        __enter__=lambda self: self,
        __exit__=lambda self, exc_type, exc, tb: False,
    )
    loop._set_state = lambda **updates: loop._bot_state.update(updates)
    return loop


def test_update_market_toxicity_builds_context_and_updates_risk_manager(monkeypatch):
    loop = _loop()
    monkeypatch.setattr(bot_loop.time, "time", lambda: 1000.0)

    loop._update_market_toxicity(
        price_data={"dexie_price": "0.0101", "tibet_price": "0.0099"},
        mid_price=Decimal("0.01"),
        arb_gap=Decimal("150"),
        open_buys=[
            {
                "trade_id": "open-buy",
                "summary": {"offered": {"xch": 200_000_000_000}, "requested": {}},
            }
        ],
        open_sells=[],
        buy_fills=[
            {"trade_id": "fill-buy", "side": "buy", "size_xch": Decimal("0.05")}
        ],
        sell_fills=[],
    )

    ctx = loop.market_toxicity_guard.last_context
    assert ctx.recent_fills[0]["side"] == "buy"
    assert ctx.recent_fills[0]["age_secs"] == 10
    assert ctx.open_offers[0]["side"] == "buy"
    assert ctx.open_offers[0]["size_xch"] == Decimal("0.2")
    assert ctx.market_intel["thin_side"] == "buy"
    assert ctx.inventory_state["xch_spendable"] == Decimal("0.5")
    assert loop.risk_manager.snapshot is loop.market_toxicity_guard.snapshot
    assert loop._bot_state["market_toxicity"]["score"] == 42


def test_update_market_toxicity_failure_keeps_loop_alive(monkeypatch):
    loop = _loop()
    loop.market_toxicity_guard.update = lambda context: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    events = []
    monkeypatch.setattr(
        bot_loop, "log_event", lambda *args, **kwargs: events.append(args)
    )

    loop._update_market_toxicity(
        price_data={},
        mid_price=Decimal("0.01"),
        arb_gap=Decimal("0"),
        open_buys=[],
        open_sells=[],
        buy_fills=[],
        sell_fills=[],
    )

    assert any(evt[1] == "toxicity_guard_error" for evt in events)


def test_toxicity_inventory_state_logs_position_parse_failure(monkeypatch):
    loop = _loop()
    loop.risk_manager._net_position_cat = "not-a-decimal"
    events = []
    monkeypatch.setattr(
        bot_loop, "log_event", lambda *args, **kwargs: events.append(args)
    )

    state = loop._build_toxicity_inventory_state(Decimal("0.01"))

    assert state["position_xch"] == Decimal("0")
    assert state["position_pct"] == Decimal("0")
    assert state["pressure_side"] == ""
    assert any(evt[1] == "toxicity_inventory_state_failed" for evt in events)


def test_reset_runtime_state_clears_toxicity_sweep_memory(monkeypatch):
    from dynamic_amm_buffer import get_state, record_sweep, reset_buffer
    from sweep_coordinator import get_coordinator, reset_coordinator

    reset_buffer()
    record_sweep(fill_count=3)
    assert get_state()["sweep_count_in_window"] == 1

    reset_coordinator()
    old_coordinator = get_coordinator()
    old_coordinator.process_fill(
        1,
        SimpleNamespace(
            trade_id="old",
            classification="unknown",
            spent_block_index=99,
            taker_puzzle_hash=None,
            side="sell",
        ),
    )
    assert old_coordinator.get_pending_summary()["pending_fill_count"] == 1

    loop = object.__new__(bot_loop.BotLoop)
    loop._probe_lock = _NoopLock()
    loop.market_toxicity_guard = _FakeGuard()
    loop.risk_manager = _FakeRiskManager()
    loop._startup_complete = _FakeStartupEvent()
    loop._startup_coin_recheck_done = True
    loop._startup_repost_done = True
    loop._sweep_protection = {"sell": 123.0}
    loop._recent_sweep_events = [{"side": "sell", "fill_count": 4}]
    loop._last_toxicity_live_cancel = {
        "buy": {"at": 10.0, "signature": "old-buy"},
        "sell": {"at": 20.0, "signature": "old-sell"},
    }
    monkeypatch.setattr(bot_loop, "log_event", lambda *args, **kwargs: None)

    loop._reset_runtime_state()

    assert loop._sweep_protection == {}
    assert loop._recent_sweep_events == []
    assert loop._last_toxicity_live_cancel == {
        "buy": {"at": 0.0, "signature": ""},
        "sell": {"at": 0.0, "signature": ""},
    }
    assert loop.market_toxicity_guard.reset_called is True
    assert loop.risk_manager.reset_session_called is True
    assert loop._startup_complete.cleared is True
    assert get_state()["sweep_count_in_window"] == 0
    assert get_coordinator() is not old_coordinator
    assert get_coordinator().get_pending_summary()["pending_fill_count"] == 0
