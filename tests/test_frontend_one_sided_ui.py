import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bot_gui.html"


def _html() -> str:
    return GUI.read_text(encoding="utf-8")


def _css_block(html: str, selector: str) -> str:
    match = re.search(
        re.escape(selector) + r"\s*\{(?P<body>[^}]*)\}",
        html,
        flags=re.IGNORECASE,
    )
    return match.group("body") if match else ""


def test_single_sided_mode_parks_inactive_sections_instead_of_hiding_them():
    html = _html()

    assert (
        "display"
        not in _css_block(
            html, "body.liquidity-mode-buy-only .mode-hide-on-buy-only"
        ).lower()
    )
    assert (
        "display"
        not in _css_block(
            html, "body.liquidity-mode-sell-only .mode-hide-on-sell-only"
        ).lower()
    )
    assert "Two-sided only" in html


def test_offer_tab_has_visible_parked_side_state_hooks():
    html = _html()

    for required in (
        'id="offersModeBanner"',
        'id="buyOffersSection"',
        'id="sellOffersSection"',
        'id="buyOffersModeBadge"',
        'id="sellOffersModeBadge"',
        "function applyOneSidedUiState",
    ):
        assert required in html

    orderbook_depth = html.index("Orderbook Depth")
    assert html.index('id="intelBuyDepthPanel"') > orderbook_depth
    assert html.index('id="intelSellDepthPanel"') > orderbook_depth


def test_save_and_coin_prep_paths_sanitize_inactive_side_from_liquidity_mode():
    html = _html()

    assert "function normalizeConfigForLiquidityMode" in html
    assert "normalizeConfigForLiquidityMode(config)" in html
    assert (
        "params.set('liquidity_mode'" in html or 'params.set("liquidity_mode"' in html
    )
    assert "liquidityMode:" in html
    assert re.search(
        r"function\s+buildCoinPrepPlan\s*\(\s*\{[\s\S]{0,500}liquidityMode",
        html,
    )
    assert "All active-side tier counts are zero" in html
    assert "f.active === false" in html


def test_status_update_hydrates_liquidity_mode_picker_and_body_class():
    html = _html()

    update_ui = re.search(
        r"function\s+updateUI\s*\(\s*data\s*\)\s*\{(?P<body>[\s\S]{0,1800})",
        html,
    )
    assert update_ui, "updateUI() not found"
    body = update_ui.group("body")

    assert "setLiquidityMode(data.liquidity.mode" in body
    assert "applyLiquidityModeToBody" in html


def test_data_reset_buttons_disable_while_bot_is_running():
    html = _html()

    assert "function updateDataResetButtonState" in html
    assert "updateDataResetButtonState(isRunning)" in html
    for button_id in ("btnResetPnl", "btnResetOfferHistory", "btnResetFull"):
        assert button_id in html


def test_pnl_tab_defines_realized_unrealized_and_total_pnl_terms():
    html = _html()

    pnl_start = html.index('id="v4View-pnl"')
    pnl_end = html.index('id="v4View-intel"', pnl_start)
    pnl_html = html[pnl_start:pnl_end]

    assert "Realized PnL" in pnl_html
    assert "profit/loss from completed round trips" in pnl_html
    assert "Unrealized PnL" in pnl_html
    assert "profit/loss on inventory you still hold" in pnl_html
    assert "Total PnL" in pnl_html
    assert "Realized PnL + Unrealized PnL" in pnl_html
    assert "XCH realized" in html
    assert "XCH realised" not in html


def test_pnl_terms_panel_exposes_live_realized_unrealized_and_total_values():
    html = _html()

    pnl_start = html.index('id="v4View-pnl"')
    pnl_end = html.index('id="v4View-intel"', pnl_start)
    pnl_html = html[pnl_start:pnl_end]

    for element_id in (
        'id="pnlRealizedMetric"',
        'id="pnlUnrealizedMetric"',
        'id="pnlTotalMetric"',
    ):
        assert element_id in pnl_html

    assert "function derivePnlBreakdown" in html
    assert "function updatePnlMetricCard" in html
    assert "unrealised_pnl_xch" in html
    assert "total_pnl_xch" in html


def test_pnl_breakdown_does_not_treat_inventory_notional_as_unrealized_pnl():
    html = _html()

    derive = re.search(
        r"function\s+derivePnlBreakdown\s*\([^)]*\)\s*\{(?P<body>[\s\S]*?)\n\s*\}\n\s*function\s+updatePnlMetricCard",
        html,
    )
    assert derive, "derivePnlBreakdown() not found"
    assert "netPositionCat * mid" not in derive.group("body")
    assert "hasExplicitUnrealizedPnl" in derive.group("body")


def test_dashboard_performance_payload_cannot_overwrite_missing_pnl_breakdown():
    html = _html()

    apply = re.search(
        r"function\s+applyPnlSummary\s*\([^)]*\)\s*\{(?P<body>[\s\S]*?)\n\s*\}\n\s*function\s+fetchPnLData",
        html,
    )
    assert apply, "applyPnlSummary() not found"
    body = apply.group("body")
    assert "hasFullPnlBreakdown" in body
    assert re.search(
        r"if\s*\(\s*hasFullPnlBreakdown\s*\)\s*\{[\s\S]{0,1200}pnlUnrealizedMetric",
        body,
    )
    assert re.search(
        r"if\s*\(\s*hasFullPnlBreakdown\s*\)\s*\{[\s\S]{0,1600}pnlTotalMetric",
        body,
    )


def test_dashboard_price_limits_use_guard_price_formatter():
    html = _html()

    update = re.search(
        r"function\s+updateCommandCentre\s*\([^)]*\)\s*\{(?P<body>[\s\S]*?)\n\s*\}\n\s*function\s+formatTierGroupLabel",
        html,
    )
    assert update, "updateCommandCentre() not found"
    body = update.group("body")
    assert "formatPriceGuardInput(sa.hard_min_price)" in body
    assert "formatPriceGuardInput(sa.hard_max_price)" in body
    assert "sa.hard_min_price + ' - ' + sa.hard_max_price" not in body
