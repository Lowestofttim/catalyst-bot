from decimal import Decimal

from market_toxicity import MarketToxicityGuard, ToxicityContext


def _ctx(**overrides):
    base = dict(
        now=1000.0,
        loop_count=7,
        mid_price=Decimal("0.01"),
        tibet_price=Decimal("0.01"),
        dexie_price=Decimal("0.01"),
        arb_gap_bps=Decimal("0"),
        open_offers=[],
        recent_fills=[],
        market_intel={},
        orderbook_snapshot={},
        inventory_state={},
        wallet_health={},
        recent_sweep_events=[],
        liquidity_mode="two_sided",
    )
    base.update(overrides)
    return ToxicityContext(**base)


def test_one_sided_mode_not_toxic_without_bad_flow():
    guard = MarketToxicityGuard()

    snap = guard.update(_ctx(liquidity_mode="buy_only"))

    assert snap.level == "normal"
    assert snap.score == 0
    assert snap.throttled_sides == []


def test_fast_same_side_fills_raise_side_score():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            recent_fills=[
                {"side": "sell", "age_secs": 12, "size_xch": "0.02"},
                {"side": "sell", "age_secs": 18, "size_xch": "0.03"},
            ]
        )
    )

    assert snap.sell_score >= 55
    assert "fast_fills" in {r["key"] for r in snap.reasons}


def test_small_balance_exposure_can_throttle():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            open_offers=[
                {"side": "buy", "size_xch": "0.35"},
                {"side": "buy", "size_xch": "0.30"},
            ],
            inventory_state={"xch_spendable": "1.0", "cat_spendable_xch": "0.2"},
            recent_fills=[{"side": "buy", "age_secs": 15, "size_xch": "0.05"}],
        )
    )

    assert snap.buy_score >= 75
    assert "buy" in snap.throttled_sides
    assert "small_balance_exposure" in {r["key"] for r in snap.reasons}


def test_public_market_thin_side_scores_matching_side():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            market_intel={
                "thin_side": "sell",
                "buy_depth_xch": "3.0",
                "sell_depth_xch": "0.2",
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            }
        )
    )

    assert snap.sell_score > snap.buy_score
    assert "thin_public_depth" in {r["key"] for r in snap.reasons}


def test_own_whale_orders_do_not_self_throttle():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            market_intel={
                "whale_orders": [
                    {"side": "buy", "xch_amount": "4.0", "is_ours": True},
                    {"side": "buy", "xch_amount": "3.0", "is_ours": True},
                    {"side": "buy", "xch_amount": "2.0", "is_ours": True},
                    {"side": "buy", "xch_amount": "1.5", "is_ours": True},
                    {"side": "buy", "xch_amount": "1.1", "is_ours": True},
                ],
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            }
        )
    )

    assert snap.buy_score == 0
    assert snap.throttled_sides == []
    assert "whale_public_offer" not in {r["key"] for r in snap.reasons}


def test_scores_decay_when_conditions_calm():
    guard = MarketToxicityGuard()
    hot = guard.update(
        _ctx(
            now=1000.0,
            recent_fills=[
                {"side": "buy", "age_secs": 10, "size_xch": "0.04"},
                {"side": "buy", "age_secs": 15, "size_xch": "0.04"},
            ],
        )
    )

    calm = guard.update(_ctx(now=1090.0, recent_fills=[]))

    assert calm.buy_score < hot.buy_score
    assert calm.score < hot.score


def test_disabled_guard_returns_normal(monkeypatch):
    monkeypatch.setattr("market_toxicity.cfg.MARKET_TOXICITY_ENABLED", False, raising=False)
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(recent_fills=[{"side": "buy", "age_secs": 5, "size_xch": "0.1"}])
    )

    assert snap.enabled is False
    assert snap.score == 0
    assert snap.buy_spread_multiplier == Decimal("1.0")
