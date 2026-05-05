from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "bot_gui.html"


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_tibet_price_shift_events_are_visible_in_system_logs():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    icon_map = _section(html, "const SYSTEM_LOG_EVENT_ICONS = {", "};")

    required_events = [
        "mempool_swap_detected",
        "mempool_imminent_wake",
        "mempool_price_confirmed",
        "mempool_price_move",
        "defensive_cancel_start",
        "mempool_defensive_cancel_done",
        "mempool_defensive_cancel_deferred_pending_cancel_settle",
        "mempool_preconfirm_defensive_cancel_done",
        "mempool_preconfirm_cancel_below_trigger",
        "mempool_preconfirm_cancel_deferred_pending_cancel_settle",
        "amm_drift_detected",
        "amm_drift_requote_triggered",
        "tibet_swap_detected",
    ]

    for event_type in required_events:
        assert f"{event_type}:" in icon_map


def test_tibet_price_shift_events_have_activity_copy():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    activity_translator = _section(html, "function laTranslate(", "function laHandleEvent(")

    required_events = [
        "mempool_swap_detected",
        "mempool_imminent_wake",
        "mempool_price_confirmed",
        "mempool_price_move",
        "mempool_defensive_cancel_done",
        "mempool_defensive_cancel_deferred_pending_cancel_settle",
        "mempool_preconfirm_defensive_cancel_done",
        "mempool_preconfirm_cancel_below_trigger",
        "mempool_preconfirm_cancel_deferred_pending_cancel_settle",
        "amm_drift_requote_triggered",
    ]

    for event_type in required_events:
        assert event_type in activity_translator

    assert "Tibet swap spotted early" in activity_translator
    assert "Tibet price shift confirmed" in activity_translator


def test_controlled_recovery_activity_events_are_info_not_warning():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    activity_translator = _section(html, "function laTranslate(", "function laHandleEvent(")

    assert "eventType === 'recovery_mode_enter'" in activity_translator
    recovery_section = activity_translator.split(
        "eventType === 'recovery_mode_enter'", 1
    )[1].split("eventType === 'recovery_mode_exit'", 1)[0]
    assert "Recovery mode" in recovery_section
    assert "cls: 'la-info'" in recovery_section

    assert "eventType === 'pending_cancel_settle_retry_queued'" in activity_translator
    retry_section = activity_translator.split(
        "eventType === 'pending_cancel_settle_retry_queued'", 1
    )[1].split("if (eventType === 'cancel_retries')", 1)[0]
    assert "Cancel settlement retry queued" in retry_section
    assert "cls: 'la-info'" in retry_section


def test_coin_topup_events_are_visible_in_system_logs():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    icon_map = _section(html, "const SYSTEM_LOG_EVENT_ICONS = {", "};")
    filter_fn = _section(
        html,
        "function shouldShowSystemLogEvent(severity, eventType) {",
        "function formatSystemLogMessage(eventType, message) {",
    )

    required_events = [
        "topup_started",
        "topup_trigger",
        "topup_tiers_adequate",
        "topup_inventory",
        "topup_deferred_offer_rebuild_priority",
        "tier_size_drift",
        "tier_size_drift_topup_started",
        "tier_size_drift_topup_deferred",
        "tier_size_drift_waiting_for_source",
    ]

    for event_type in required_events:
        assert f"{event_type}:" in icon_map

    assert "evt.startsWith('topup_')" in filter_fn
    assert "evt.startsWith('drip_')" in filter_fn
    assert "evt.startsWith('tier_size_drift')" in filter_fn


def test_tier_group_panel_explains_position_order_and_topup_pool():
    html = GUI.read_text(encoding="utf-8", errors="replace")
    tier_group_fn = _section(
        html,
        "function renderCoinTierGroups(tierData, poolAmounts) {",
        "function getCommandCentreDashboardState()",
    )

    assert "XCH (buy pos)" in tier_group_fn
    assert "buy-position order" in tier_group_fn
    assert "Unallocated top-up fuel, separate from hard reserve" in tier_group_fn
    assert "A low amount is fine when tier spares are stocked" in tier_group_fn
