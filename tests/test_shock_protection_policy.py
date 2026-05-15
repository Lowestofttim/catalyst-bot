from decimal import Decimal
from types import SimpleNamespace

from shock_protection import evaluate_tibet_shock, tibet_shock_trigger_pct


def _cfg(**overrides):
    base = {
        "MIN_EDGE_BPS": Decimal("300"),
        "TIBET_SHOCK_CANCEL_TRIGGER_PCT": Decimal("0"),
        "TIBET_SHOCK_CANCEL_MID_PCT": Decimal("5"),
        "TIBET_SHOCK_CANCEL_OUTER_PCT": Decimal("10"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_auto_trigger_scales_from_inner_edge_with_half_percent_floor():
    assert tibet_shock_trigger_pct(_cfg(MIN_EDGE_BPS=Decimal("275"))) == Decimal(
        "1.375"
    )
    assert tibet_shock_trigger_pct(_cfg(MIN_EDGE_BPS=Decimal("50"))) == Decimal("0.50")


def test_manual_trigger_percentage_overrides_auto_threshold():
    cfg = _cfg(
        MIN_EDGE_BPS=Decimal("300"),
        TIBET_SHOCK_CANCEL_TRIGGER_PCT=Decimal("2.25"),
    )

    assert tibet_shock_trigger_pct(cfg) == Decimal("2.25")
    assert not evaluate_tibet_shock(Decimal("2.24"), "up", cfg).cancel
    assert evaluate_tibet_shock(Decimal("2.25"), "up", cfg).cancel


def test_shock_action_uses_at_risk_side_and_graduated_tiers():
    cfg = _cfg()

    small_up = evaluate_tibet_shock(Decimal("2"), "up", cfg)
    mid_down = evaluate_tibet_shock(Decimal("5"), "down", cfg)
    outer_unknown = evaluate_tibet_shock(Decimal("10"), "?", cfg)

    assert small_up.cancel
    assert small_up.sides == ("sell",)
    assert small_up.tiers == ("inner",)
    assert mid_down.sides == ("buy",)
    assert mid_down.tiers == ("inner", "mid")
    assert outer_unknown.sides == ("buy", "sell")
    assert outer_unknown.tiers == ("inner", "mid", "outer")
