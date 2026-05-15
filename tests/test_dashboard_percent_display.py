from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bot_gui.html"


def test_user_facing_rate_controls_and_diagnostics_are_percent_first():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "Arb Alert Threshold (%)" in html
    assert "Arb Alert Threshold (bps)" not in html
    assert "Dexie/Tibet gap, as a percentage" in html
    assert "basis points, that marks an arbitrage alert" not in html
    assert "100 bps = 1%" not in html
    assert "return n.toFixed(1) + ' bps';" not in html
    assert "return bps2pct(n);" in html
    assert "offset_bps" not in html


def test_arb_alert_threshold_round_trips_percent_in_settings_ui():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "const _pct2bps = v => Math.round((parseFloat(v) || 0) * 100);" in html
    assert (
        "document.getElementById('configArbThreshold').value = arb > 0 ? _bps2pct(arb) : '2.0';"
        in html
    )
    assert (
        "arb_threshold_bps: _pct2bps(document.getElementById('configArbThreshold')?.value || '2.0'),"
        in html
    )
    assert "_arbEl.value = _bps2pct(data.arb_alert_threshold_bps);" in html
    assert (
        "arb_threshold_bps: parseInt(document.getElementById('configArbThreshold')?.value || '200')"
        not in html
    )


def test_smart_settings_summary_converts_bps_values_before_rendering_percent():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "const spreadPct = bps2pct(data.base_spread_bps || 0);" in html
    assert "const requotePct = bps2pct(data.requote_bps || 0);" in html
    assert "Base <strong>${spreadPct}</strong>" in html
    assert "Requote at <strong>${requotePct}</strong>" in html
    assert (
        "document.getElementById('configDbxMaxSpreadBps').value = _bps2pct(data.dbx_max_spread_bps);"
        in html
    )
    assert "Base <strong>${spreadBps.toFixed(1)}%</strong>" not in html


def test_live_market_health_fallback_recomputes_inner_spread_from_edges():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "mid_price: data.mid_price," in html
    assert "const liveBid = parseFloat(metrics.our_best_bid || 0);" in html
    assert "const liveAsk = parseFloat(metrics.our_best_ask || 0);" in html
    assert "let liveMid = parseFloat" in html
    assert "if (!(liveMid > 0) && liveBid > 0 && liveAsk > liveBid)" in html
    assert "liveMid = (liveBid + liveAsk) / 2;" in html
    assert "metrics.your_spread_bps = ((liveAsk - liveBid) / liveMid) * 10000;" in html
