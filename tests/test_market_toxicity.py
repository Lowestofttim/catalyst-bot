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


def test_same_side_sweep_with_fast_fills_throttles_filled_side():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            recent_sweep_events=[{"side": "sell", "fill_count": 3}],
            recent_fills=[
                {"side": "sell", "age_secs": 8, "size_xch": "0.04"},
                {"side": "sell", "age_secs": 10, "size_xch": "0.05"},
                {"side": "sell", "age_secs": 12, "size_xch": "0.06"},
            ],
        )
    )

    assert snap.sell_score >= 75
    assert "sell" in snap.throttled_sides
    assert "same_block_sweep" in {r["key"] for r in snap.reasons}
    assert "fast_fill_cluster" in {r["key"] for r in snap.reasons}


def test_large_dexie_tibet_gap_widens_both_sides_without_throttle():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            tibet_price=Decimal("0.0100"),
            dexie_price=Decimal("0.0105"),
            arb_gap_bps=Decimal("500"),
        )
    )

    assert snap.score >= 30
    assert snap.level == "mild"
    assert snap.buy_spread_multiplier == Decimal("1.10")
    assert snap.sell_spread_multiplier == Decimal("1.10")
    assert snap.throttled_sides == []
    assert "dexie_tibet_dislocation" in {r["key"] for r in snap.reasons}


def test_extreme_dexie_tibet_gap_elevates_both_sides_without_throttle():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            tibet_price=Decimal("0.0100"),
            dexie_price=Decimal("0.0110"),
            arb_gap_bps=Decimal("1000"),
        )
    )

    assert snap.score >= 55
    assert snap.level == "elevated"
    assert snap.buy_spread_multiplier == Decimal("1.35")
    assert snap.sell_spread_multiplier == Decimal("1.35")
    assert snap.throttled_sides == []
    assert "dexie_tibet_dislocation" in {r["key"] for r in snap.reasons}


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


def test_public_depth_signal_does_not_stack_every_loop():
    guard = MarketToxicityGuard()

    first = guard.update(
        _ctx(
            now=1000.0,
            market_intel={
                "thin_side": "sell",
                "buy_depth_xch": "3.0",
                "sell_depth_xch": "0.2",
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            },
        )
    )

    later = first
    for i in range(1, 8):
        later = guard.update(
            _ctx(
                now=1000.0 + (45.0 * i),
                market_intel={
                    "thin_side": "sell",
                    "buy_depth_xch": "3.0",
                    "sell_depth_xch": "0.2",
                    "orderbook_refreshes": 3,
                    "orderbook_age_secs": 12,
                },
            )
        )

    assert later.sell_score == first.sell_score
    assert later.throttled_sides == []


def test_sweep_response_cools_when_only_thin_depth_remains():
    guard = MarketToxicityGuard()

    fills = [{"side": "sell", "age_secs": 8 + i, "size_xch": "0.05"} for i in range(14)]
    hot = guard.update(
        _ctx(
            now=1000.0,
            recent_sweep_events=[{"side": "sell", "fill_count": 14}],
            recent_fills=fills,
            market_intel={
                "thin_side": "sell",
                "buy_depth_xch": "3.0",
                "sell_depth_xch": "0.2",
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            },
        )
    )

    cooled = hot
    for i in range(1, 16):
        cooled = guard.update(
            _ctx(
                now=1000.0 + (45.0 * i),
                recent_sweep_events=[],
                recent_fills=[],
                market_intel={
                    "thin_side": "sell",
                    "buy_depth_xch": "3.0",
                    "sell_depth_xch": "0.2",
                    "orderbook_refreshes": 3,
                    "orderbook_age_secs": 12,
                },
            )
        )

    assert hot.sell_score == 100
    assert "sell" in hot.throttled_sides
    assert cooled.sell_score < 75
    assert "sell" not in cooled.throttled_sides
    assert cooled.level != "extreme"


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


def test_sweep_reason_uses_side_specific_fill_count_when_available():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            recent_sweep_events=[
                {
                    "side": "buy",
                    "fill_count": 14,
                    "side_fill_count": 8,
                    "sweep_group_id": "sweep_mixed",
                },
                {
                    "side": "sell",
                    "fill_count": 14,
                    "side_fill_count": 6,
                    "sweep_group_id": "sweep_mixed",
                },
            ],
        )
    )

    sweep_details = [
        r["detail"] for r in snap.reasons if r["key"] == "same_block_sweep"
    ]
    assert "8 buy fills grouped in the same sweep" in sweep_details
    assert "6 sell fills grouped in the same sweep" in sweep_details
    assert "14 buy fills grouped in the same sweep" not in sweep_details
    assert "14 sell fills grouped in the same sweep" not in sweep_details


def test_public_whale_depth_reasons_are_aggregated_per_side():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            mid_price=Decimal("0.010"),
            market_intel={
                "whale_orders": [
                    {
                        "side": "buy",
                        "price": "0.00995",
                        "xch_amount": "4.0",
                        "age_secs": 90,
                    },
                    {
                        "side": "buy",
                        "price": "0.00990",
                        "xch_amount": "3.0",
                        "age_secs": 120,
                    },
                    {
                        "side": "buy",
                        "price": "0.00985",
                        "xch_amount": "2.0",
                        "age_secs": 180,
                    },
                ],
                "buy_depth_xch": "18",
                "sell_depth_xch": "16",
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            },
        )
    )

    whale_reasons = [
        r
        for r in snap.reasons
        if r["key"] == "whale_public_offer" and r["side"] == "buy"
    ]
    assert len(whale_reasons) == 1
    assert whale_reasons[0]["score"] >= 60
    assert (
        "3 market-relative public buy offers visible on Dexie"
        in whale_reasons[0]["detail"]
    )
    assert "near live market" in whale_reasons[0]["detail"]


def test_deep_market_public_whales_do_not_keep_guard_mild():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            mid_price=Decimal("0.010"),
            market_intel={
                "buy_depth_xch": "120",
                "sell_depth_xch": "110",
                "whale_orders": [
                    {"side": "buy", "price": "0.0090", "xch_amount": "2.0"},
                    {"side": "buy", "price": "0.0088", "xch_amount": "2.0"},
                    {"side": "buy", "price": "0.0086", "xch_amount": "2.0"},
                ],
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            },
            open_offers=[
                {"side": "buy", "size_xch": "1.2"},
                {"side": "sell", "size_xch": "1.2"},
            ],
        )
    )

    assert snap.buy_score == 0
    assert snap.score == 0
    assert "whale_public_offer" not in {r["key"] for r in snap.reasons}


def test_near_touch_fresh_market_relative_public_order_scores_with_clear_hint():
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(
            mid_price=Decimal("0.010"),
            market_intel={
                "buy_depth_xch": "20",
                "sell_depth_xch": "18",
                "whale_orders": [
                    {
                        "side": "buy",
                        "price": "0.00994",
                        "xch_amount": "5.0",
                        "age_secs": 120,
                    }
                ],
                "orderbook_refreshes": 3,
                "orderbook_age_secs": 12,
            },
            open_offers=[
                {"side": "buy", "size_xch": "1.0"},
                {"side": "sell", "size_xch": "1.0"},
            ],
        )
    )

    whale_reasons = [r for r in snap.reasons if r["key"] == "whale_public_offer"]
    assert snap.buy_score >= 30
    assert whale_reasons
    assert "near live market" in whale_reasons[0]["detail"]
    assert "25.0% of buy depth" in whale_reasons[0]["detail"]
    assert snap.clear_condition
    assert snap.cooldown_secs_if_clear > 0


def test_stale_public_order_contributes_less_than_fresh_near_touch_order():
    fresh_guard = MarketToxicityGuard()
    stale_guard = MarketToxicityGuard()

    base_intel = {
        "buy_depth_xch": "20",
        "sell_depth_xch": "18",
        "orderbook_refreshes": 3,
        "orderbook_age_secs": 12,
    }
    fresh = fresh_guard.update(
        _ctx(
            mid_price=Decimal("0.010"),
            market_intel={
                **base_intel,
                "whale_orders": [
                    {
                        "side": "buy",
                        "price": "0.00994",
                        "xch_amount": "5.0",
                        "age_secs": 120,
                    }
                ],
            },
        )
    )
    stale = stale_guard.update(
        _ctx(
            mid_price=Decimal("0.010"),
            market_intel={
                **base_intel,
                "whale_orders": [
                    {
                        "side": "buy",
                        "price": "0.00994",
                        "xch_amount": "5.0",
                        "age_secs": 7200,
                    }
                ],
            },
        )
    )

    assert stale.buy_score < fresh.buy_score
    assert stale.level == "normal"


def test_recent_taker_intent_amplifies_near_touch_public_order():
    base_guard = MarketToxicityGuard()
    intent_guard = MarketToxicityGuard()
    market_intel = {
        "buy_depth_xch": "20",
        "sell_depth_xch": "18",
        "whale_orders": [
            {
                "side": "buy",
                "price": "0.00994",
                "xch_amount": "4.0",
                "age_secs": 120,
            }
        ],
        "orderbook_refreshes": 3,
        "orderbook_age_secs": 12,
    }

    base = base_guard.update(
        _ctx(mid_price=Decimal("0.010"), market_intel=market_intel)
    )
    with_intent = intent_guard.update(
        _ctx(
            mid_price=Decimal("0.010"),
            market_intel=market_intel,
            recent_fills=[
                {
                    "side": "buy",
                    "age_secs": 240,
                    "price": "0.00995",
                    "size_xch": "1.0",
                }
            ],
        )
    )

    assert with_intent.buy_score > base.buy_score
    assert any(
        r["key"] == "whale_public_offer" and "recent buy taker pressure" in r["detail"]
        for r in with_intent.reasons
    )


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


def test_reset_clears_previous_toxicity_snapshot():
    guard = MarketToxicityGuard()
    hot = guard.update(
        _ctx(
            recent_sweep_events=[{"side": "sell", "fill_count": 3}],
            recent_fills=[
                {"side": "sell", "age_secs": 8, "size_xch": "0.04"},
                {"side": "sell", "age_secs": 10, "size_xch": "0.05"},
                {"side": "sell", "age_secs": 12, "size_xch": "0.06"},
            ],
        )
    )

    assert hot.score > 0

    guard.reset()
    snap = guard.get_snapshot()

    assert snap.score == 0
    assert snap.throttled_sides == []
    assert snap.reasons == []


def test_disabled_guard_returns_normal(monkeypatch):
    monkeypatch.setattr(
        "market_toxicity.cfg.MARKET_TOXICITY_ENABLED", False, raising=False
    )
    guard = MarketToxicityGuard()

    snap = guard.update(
        _ctx(recent_fills=[{"side": "buy", "age_secs": 5, "size_xch": "0.1"}])
    )

    assert snap.enabled is False
    assert snap.score == 0
    assert snap.buy_spread_multiplier == Decimal("1.0")
