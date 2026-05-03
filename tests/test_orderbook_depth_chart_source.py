from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bot_gui.html"


def test_orderbook_depth_chart_uses_independent_side_price_lanes():
    html = GUI.read_text(encoding="utf-8")

    assert "const sideLaneGap" in html
    assert "const buySpan = priceSpan(buyLow, buyHigh)" in html
    assert "if (!buySpan) return buyLaneRight" in html
    assert "const sellSpan = priceSpan(sellLow, sellHigh)" in html
    assert "if (!sellSpan) return sellLaneLeft" in html
    assert "function xAtBuy(price)" in html
    assert "function xAtSell(price)" in html
    assert "drawStep(buyCum, buyFill, buyLine, xAtBuy)" in html
    assert "drawStep(sellCum, sellFill, sellLine, xAtSell)" in html
    assert "ctx.fillText(buyLow.toFixed(6), padL, h - padB + 8)" in html
    assert "ctx.fillText(sellHigh.toFixed(6), w - padR, h - padB + 8)" in html
