from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT_GUI = ROOT / "bot_gui.html"


def test_dashboard_explains_side_aware_toxicity_guard():
    html = BOT_GUI.read_text(encoding="utf-8")

    assert "side-aware adverse-selection score" in html
    assert "order-flow toxicity" in html
    assert "VPIN-style flow imbalance" in html
    assert "Buy and sell are scored separately" in html


def test_dashboard_explains_toxicity_clear_condition_and_cooldown():
    html = BOT_GUI.read_text(encoding="utf-8")

    assert "toxicity_clear_condition" in html
    assert "toxicity_cooldown_secs_if_clear" in html
    assert "Clears when:" in html
    assert "Expected cool-down once clear:" in html
