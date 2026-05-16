from decimal import Decimal

from market_toxicity import ToxicitySnapshot
from risk_manager import RiskManager


def _patch_spread_cfg(monkeypatch):
    monkeypatch.setattr("risk_manager.cfg.DYNAMIC_SPREAD_ENABLED", True, raising=False)
    monkeypatch.setattr("risk_manager.cfg.INVENTORY_ENABLED", False, raising=False)
    monkeypatch.setattr(
        "risk_manager.cfg.BASE_SPREAD_BPS", Decimal("800"), raising=False
    )
    monkeypatch.setattr(
        "risk_manager.cfg.MIN_SPREAD_BPS", Decimal("300"), raising=False
    )
    monkeypatch.setattr(
        "risk_manager.cfg.MAX_SPREAD_BPS", Decimal("3000"), raising=False
    )
    monkeypatch.setattr("risk_manager.cfg.MIN_EDGE_BPS", Decimal("200"), raising=False)
    monkeypatch.setattr(
        "risk_manager.cfg.COMPETITOR_AWARE_ENABLED", False, raising=False
    )
    monkeypatch.setattr("risk_manager.cfg.MARKET_TOXICITY_ENABLED", True, raising=False)


def test_toxicity_multiplier_widens_before_clamp(monkeypatch):
    _patch_spread_cfg(monkeypatch)
    rm = RiskManager()
    rm.set_market_toxicity(
        ToxicitySnapshot(
            score=82,
            buy_score=82,
            sell_score=12,
            level="high",
            buy_spread_multiplier=Decimal("1.75"),
            sell_spread_multiplier=Decimal("1.20"),
            throttled_sides=["buy"],
            throttle_until={"buy": 9999999999.0},
            reasons=[],
            suggested_action="Throttle buy",
        )
    )

    assert rm.get_adjusted_spread("buy") == Decimal("0.14")
    assert rm.get_adjusted_spread("sell") == Decimal("0.096")
    assert rm.should_enable_side("buy", Decimal("0.01")) is False
    assert rm.should_enable_side("sell", Decimal("0.01")) is True


def test_inventory_state_exposes_toxicity_snapshot(monkeypatch):
    _patch_spread_cfg(monkeypatch)
    rm = RiskManager()
    rm.set_market_toxicity(
        ToxicitySnapshot(
            score=61,
            buy_score=61,
            sell_score=0,
            level="elevated",
            buy_spread_multiplier=Decimal("1.35"),
            sell_spread_multiplier=Decimal("1.10"),
            reasons=[
                {"key": "fast_fills", "side": "buy", "score": 35, "detail": "fast"}
            ],
        )
    )

    state = rm.get_inventory_state()

    assert state["market_toxicity"]["score"] == 61
    assert state["market_toxicity"]["level"] == "elevated"
    assert state["market_toxicity"]["buy_spread_multiplier"] == "1.35"


def test_market_health_exposes_toxicity_operator_details(monkeypatch):
    _patch_spread_cfg(monkeypatch)
    rm = RiskManager()
    rm.set_market_toxicity(
        ToxicitySnapshot(
            score=82,
            buy_score=82,
            sell_score=12,
            level="high",
            buy_spread_multiplier=Decimal("1.75"),
            sell_spread_multiplier=Decimal("1.20"),
            throttled_sides=["buy"],
            throttle_until={"buy": 9999999999.0},
            reasons=[
                {
                    "key": "fast_fills",
                    "side": "buy",
                    "score": 35,
                    "detail": "buy fills landed close together",
                }
            ],
            suggested_action="Throttle new buy offers until toxicity cools.",
            clear_condition="BUY pressure disappears",
            cooldown_secs_if_clear=225,
        )
    )

    health = rm.get_market_health()
    metrics = health["metrics"]

    assert metrics["toxicity_buy_spread_multiplier"] == "1.75"
    assert metrics["toxicity_sell_spread_multiplier"] == "1.20"
    assert metrics["toxicity_throttle_until"] == {"buy": 9999999999.0}
    assert metrics["toxicity_clear_condition"] == "BUY pressure disappears"
    assert metrics["toxicity_cooldown_secs_if_clear"] == 225
    assert metrics["toxicity_enabled"] is True


def test_malformed_toxicity_dict_fails_open_and_logs(monkeypatch):
    _patch_spread_cfg(monkeypatch)
    rm = RiskManager()
    rm.set_market_toxicity(
        {
            "throttled_sides": ["buy"],
            "throttle_until": {"buy": "not-a-timestamp"},
        }
    )
    events = []
    monkeypatch.setattr(
        "risk_manager.log_event", lambda *args, **kwargs: events.append(args)
    )

    assert rm.should_enable_side("buy", Decimal("0.01")) is True
    assert any(evt[1] == "toxicity_throttle_parse_failed" for evt in events)


def test_toxicity_side_check_exception_fails_open_and_logs(monkeypatch):
    class BadSnapshot:
        def is_side_throttled(self, side, now):
            raise RuntimeError("bad toxicity snapshot")

    _patch_spread_cfg(monkeypatch)
    rm = RiskManager()
    rm.set_market_toxicity(BadSnapshot())
    events = []
    monkeypatch.setattr(
        "risk_manager.log_event", lambda *args, **kwargs: events.append(args)
    )

    assert rm.should_enable_side("buy", Decimal("0.01")) is True
    assert any(evt[1] == "toxicity_side_check_failed" for evt in events)


def test_market_health_logs_malformed_toxicity_conditions(monkeypatch):
    _patch_spread_cfg(monkeypatch)
    rm = RiskManager()
    rm.set_market_toxicity(
        {
            "score": 80,
            "level": "high",
            "throttled_sides": ["sell"],
            "reasons": ["malformed reason"],
        }
    )
    events = []
    monkeypatch.setattr(
        "risk_manager.log_event", lambda *args, **kwargs: events.append(args)
    )

    health = rm.get_market_health()

    assert health["status"] in {"green", "amber", "red"}
    assert any(evt[1] == "toxicity_health_eval_failed" for evt in events)
