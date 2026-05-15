from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bot_gui.html"
RISK_MANAGER = ROOT / "src" / "catalyst" / "risk_manager.py"
APP_BRIDGE = ROOT / "src" / "catalyst" / "app_bridge.py"


def test_dashboard_sse_keeps_advisor_performance_state_fresh():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "_lcDashboardData.performance.loop_count = data.loop_count;" in html
    assert "_lcDashboardData.performance.uptime_secs = data.uptime_secs;" in html
    assert "_lcDashboardData.performance.open_buys = data.open_buys;" in html
    assert "_lcDashboardData.performance.open_sells = data.open_sells;" in html
    assert "_lcDashboardData.performance.open_offers = data.open_buys + data.open_sells;" in html


def test_recommendations_clear_stale_rotator_cache_when_empty():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert """if (active.length === 0) {
                _alertActiveCache = [];
                _alertRotatorIdx = 0;
                if (_alertRotatorTimer) {
                    clearInterval(_alertRotatorTimer);
                    _alertRotatorTimer = null;
                }""" in html


def test_recommendation_action_row_wraps_inside_guidance_card():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert ".alert-item .alert-content { flex: 1; min-width: 0;" in html
    assert ".alert-item .alert-msg { color: var(--text-secondary); font-size: 10px; overflow-wrap: anywhere;" in html
    assert ".alert-actions-row" in html
    assert "flex-wrap: wrap" in html
    assert "max-width: 100%" in html
    assert '<div class="alert-actions-row">' in html


def test_update_badge_is_compact_sidebar_control():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert 'id="v4UpdateBadge" class="v4-update-badge"' in html
    assert ".v4-update-badge" in html
    assert "width: 44px" in html
    assert "white-space: normal" in html
    assert "v4-update-badge-version" in html


def test_data_reset_success_refreshes_visible_stats():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "async function refreshAfterDataReset({ clearPnlCharts = true } = {})" in html
    assert "await refreshAfterDataReset();" in html
    assert "await refreshAfterDataReset({ clearPnlCharts });" in html
    assert "fetchDashboard()" in html
    assert "fetchPnLData()" in html
    assert "_v4LastPnlSignature = ''" in html
    assert "v4RenderPnlChart()" in html
    assert "v4RenderInventoryChart()" in html
    assert "updateDashboard()" not in html


def test_offer_history_reset_preserves_pnl_chart_history():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "async function _runReset(endpoint, label, successMsgBuilder, { clearPnlCharts = true } = {})" in html
    assert "await _runReset('reset/offer-history', 'Clear offer history'," in html
    assert "{ clearPnlCharts: false });" in html


def test_desktop_bridge_covers_reset_routes_used_by_data_buttons():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    bridge = APP_BRIDGE.read_text(encoding="utf-8", errors="replace")

    for route, method in (
        ("pnl/reset-preview", "get_pnl_reset_preview"),
        ("pnl/reset", "reset_pnl"),
        ("reset/offer-history", "reset_offer_history"),
        ("reset/full", "reset_full"),
    ):
        assert f"clean === '{route}'" in html
        assert f"return '{method}'" in html
        assert f"def {method}" in bridge


def test_diagnostics_are_distributed_across_existing_workflow_tabs():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    required_ids = [
        "offersDiagnosticsGrid",
        "diagBuyRequotePressure",
        "diagSellRequotePressure",
        "diagPendingCancels",
        "diagRequoteBatch",
        "intelSourceCoverage",
        "intelArbGapTrend",
        "pnlFlowDiagnostics",
        "pnlPendingVerification",
    ]

    for element_id in required_ids:
        assert f'id="{element_id}"' in html


def test_market_price_history_has_range_controls_without_adding_new_main_tab():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert 'data-price-range="24"' in html
    assert 'data-price-range="0.333333"' in html
    assert 'id="v4View-analysis"' not in html


def test_market_price_history_treats_sql_timestamps_as_utc():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "function _v4ParsePriceHistoryTimestamp" in html
    assert "normalized + 'Z'" in html
    assert "const t = _v4ParsePriceHistoryTimestamp(point.timestamp);" in html


def test_dry_run_is_not_user_facing_setting():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert 'id="configDryRun"' not in html
    assert 'id="ccDryRun"' not in html
    assert "Dry Run Mode" not in html
    assert "dry_run:" not in html


def test_market_diagnostics_uses_live_amm_and_summary_sources():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "_lastAmmPriceData" in html
    assert "_lastMarketSummary" in html
    assert "summaryTibetXch" in html


def test_close_gap_recommendation_has_confidence_gate():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "_SA_CLOSE_GAP_PROMOTE_BPS = 200" in html
    assert "_SA_CLOSE_GAP_CONFIRM_UPDATES = 3" in html
    assert "saCloseGapSignalReady(" in html
    assert "closeGapCandidate" in html
    assert "if (closeGapReady)" in html


def test_min_spread_clamp_copy_is_diagnostic_not_directive():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    text = RISK_MANAGER.read_text(encoding="utf-8", errors="replace")

    assert "raise MIN_SPREAD_BPS" not in text
    assert "configured minimum clamp" in text
    assert "normalizeMarketConditionText" in html
    assert "raise MIN_SPREAD_BPS" not in html


def test_max_spread_clamp_recommendation_opens_running_settings():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert 'id="settings-section-smart-pricing"' in html
    assert "reviewMaxSpread" in html
    assert "Review Max Spread" in html
    assert "actionType: 'reviewMaxSpread'" in html
    assert "Settings > Setup > Smart Pricing" in html
    assert "settingsSwitchSubview('setup')" in html
    assert "configMaxSpreadBps" in html
    assert "future requotes and new offers" in html


def test_live_controls_points_max_spread_users_to_setup():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "Max Spread caps live in Setup > Smart Pricing" in html


def test_spread_tighten_recommendations_pause_at_max_spread_clamp():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "const maxSpreadClampActive" in html
    assert "if (!_gapCloserActive && !maxSpreadClampActive" in html
    assert "fillsHr === 0 && !maxSpreadClampActive" in html


def test_inventory_drift_advisor_uses_backend_position_percent():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "const positionLoadPct" in html
    assert "positionLoadPct > 70" in html
    assert "Math.abs(netPos) > maxPos * 0.7" not in html


def test_advisor_fill_rate_reads_dashboard_field_name():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "perf.fill_rate_per_hour ?? perf.fills_per_hour" in html


def test_running_settings_restart_warning_is_setup_only():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "function updateSettingsRestartBannerVisibility()" in html
    assert "const setupActive = !!(setupView && setupView.classList.contains('is-active'));" in html
    assert "isRunning && setupActive" in html
    assert "try { updateSettingsRestartBannerVisibility(); } catch (_) {}" in html
    assert "banner.classList.toggle('is-visible', isRunning);" not in html


def test_recovery_guidance_collapses_expected_ladder_noise():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "function isRecoveryGuidanceActive()" in html
    assert "ladderStillBuilding && !isRecoveryGuidanceActive()" in html
    assert "function isRecoveryExpectedOfferCountDiagnostic(alert)" in html
    assert "if (isRecoveryExpectedOfferCountDiagnostic(a)) return false;" in html


def test_splash_incoming_hint_explains_sparse_relevant_gossip():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "gossip sparse" in html
    assert "no relevant offers seen" in html
    assert "Connected" in html


def test_market_health_copy_distinguishes_recovery_from_market_health():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert "Market healthy — bot rebuilding ladder" in html


def test_logs_tab_has_run_doctor_button_wired_to_existing_modal():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert 'id="logsRunDoctorBtn"' in html
    assert 'onclick="runDoctorFromLogs(this)"' in html
    assert "async function runDoctorFromLogs" in html
    assert "await showDoctorReport();" in html
    assert "const resp = await apiFetch('/api/doctor?force=true');" in html


def test_dashboard_has_active_toxicity_guard_notice():
    html = GUI.read_text(encoding="utf-8", errors="replace")

    assert 'id="ccToxicityAction"' in html
    assert 'id="ccToxicityActionScore"' in html
    assert "function updateToxicityAction" in html
    assert "toxicity_buy_spread_multiplier" in html
    assert "toxicity_throttle_until" in html
    assert "openToxicityGuardSettings" in html
    assert 'data-toxicity-action="settings"' in html
    assert 'data-toxicity-action="smart-settings"' in html
    assert "Adverse Selection Guard active" in html
