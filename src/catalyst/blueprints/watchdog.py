"""Alert management and watchdog cancel-recovery routes.

Five routes:
  * `/api/alerts` and `/api/alerts/dismiss` — the in-memory alert bus
    (backed by `api_server.alerts`).
  * `/api/watchdog/cancel-mismatched-offers` — delegates to the bot's
    ShapeFixOrchestrator so the GUI gets progressive SSE updates.
  * `/api/watchdog/shape-fix-status` and `/api/watchdog/shape-fix-abort`
    — inspect and cancel the running shape-fix flow.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import api_server
from database import log_event


bp = Blueprint("watchdog", __name__)


@bp.route("/api/alerts")
def api_alerts():
    """Get all active (non-dismissed) alerts."""
    return jsonify({"alerts": api_server.alerts.get_active()})


@bp.route("/api/alerts/dismiss", methods=["POST"])
def api_dismiss_alert():
    """Dismiss an alert by ID."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    alert_id = data.get("id", "")
    if alert_id:
        api_server.alerts.dismiss(alert_id)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "No alert ID provided"}), 400


@bp.route("/api/watchdog/cancel-mismatched-offers", methods=["POST"])
def api_watchdog_cancel_mismatched_offers():
    """Delegate watchdog-flagged cancels to the ShapeFixOrchestrator so the
    UI gets progressive status updates.

    Body: ``{"trade_ids": [...], "alert_id": "...optional...", "side": "buy"|"sell"}``

    Returns 202 Accepted with ``{"success": True, "flow_id": "..."}``
    immediately — the actual cancel + wait + rebuild run on a dedicated
    thread. The frontend subscribes to ``shape_fix_progress`` SSE events
    (keyed by ``flow_id``) to follow the flow.

    If the orchestrator is busy with another flow (one side at a time
    per user requirement), returns 409 with an explanatory error.
    """
    bot = api_server.bot
    if not bot:
        return jsonify({"success": False, "error": "Bot not initialised"}), 500

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400

    raw_ids = data.get("trade_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
    trade_ids = [str(t) for t in raw_ids if t]
    if not trade_ids:
        return jsonify({"success": False, "error": "No trade_ids provided"}), 400

    seen = set()
    unique_tids: list = []
    for t in trade_ids:
        if t not in seen:
            seen.add(t)
            unique_tids.append(t)

    alert_id = str(data.get("alert_id") or "").strip()

    # Infer side from the alert_id when caller didn't pass it explicitly.
    # Alert IDs follow `watchdog_<code>_<side>` convention.
    side = str(data.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        if alert_id.endswith("_buy"):
            side = "buy"
        elif alert_id.endswith("_sell"):
            side = "sell"
        else:
            side = "sell"   # Default — most shape violations seen on sell

    orchestrator = getattr(bot, "shape_fix_orchestrator", None)
    if orchestrator is None:
        # Fallback — orchestrator failed to init. Fall back to the
        # older synchronous path so the button still does something.
        log_event("warning", "watchdog_cancel_fallback_sync",
                  "Orchestrator unavailable — falling back to sync cancel")
        try:
            result = bot.offer_manager.cancel_offers(
                unique_tids, reason="watchdog_shape_fix")
        except Exception as e:
            log_event("error", "watchdog_cancel_failed",
                      f"Watchdog-triggered cancel failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        cancelled = [tid for tid, r in (result or {}).items()
                     if isinstance(r, dict) and r.get("success")]
        failed = [tid for tid in unique_tids if tid not in cancelled]
        if alert_id:
            try:
                api_server.alerts.clear(alert_id)
            except Exception:
                pass
        return jsonify({
            "success": True,
            "fallback": "sync",
            "cancelled_count": len(cancelled),
            "failed_count": len(failed),
        })

    # Happy path — delegate to the orchestrator.
    outcome = orchestrator.start_flow(
        side=side, trade_ids=unique_tids, alert_id=alert_id)
    if not outcome.get("accepted"):
        return jsonify({
            "success": False,
            "error": outcome.get("error") or "Orchestrator rejected flow",
        }), 409

    log_event("info", "shape_fix_flow_started",
              f"Shape-fix flow started for {side} side "
              f"({len(unique_tids)} offers)",
              data={
                  "flow_id": outcome["flow_id"],
                  "side": side,
                  "trade_id_count": len(unique_tids),
                  "alert_id": alert_id,
              })

    return jsonify({
        "success": True,
        "flow_id": outcome["flow_id"],
        "side": side,
        "total_requested": len(unique_tids),
    }), 202


@bp.route("/api/watchdog/shape-fix-status")
def api_watchdog_shape_fix_status():
    """Snapshot of any in-flight shape-fix recovery flow.

    Returns ``{"active": False}`` when idle, or the current
    :class:`FlowState` rendered as a dict when a flow is running.
    """
    bot = api_server.bot
    if not bot:
        return jsonify({"active": False, "error": "Bot not initialised"}), 200
    orch = getattr(bot, "shape_fix_orchestrator", None)
    if orch is None:
        return jsonify({"active": False, "error": "Orchestrator unavailable"}), 200
    flow = orch.current_flow()
    if flow is None:
        return jsonify({"active": False}), 200
    return jsonify({"active": True, "flow": flow.to_dict()})


@bp.route("/api/watchdog/shape-fix-abort", methods=["POST"])
def api_watchdog_shape_fix_abort():
    """Request abort of the running shape-fix flow (if any).

    Body: ``{"side": "buy"|"sell"}`` (optional — defaults to the only
    running side).
    """
    bot = api_server.bot
    if not bot:
        return jsonify({"success": False, "error": "Bot not initialised"}), 500
    orch = getattr(bot, "shape_fix_orchestrator", None)
    if orch is None:
        return jsonify({"success": False, "error": "Orchestrator unavailable"}), 404
    data = request.get_json(silent=True) or {}
    side = str(data.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        flow = orch.current_flow()
        if flow is None:
            return jsonify({"success": False, "error": "No flow running"}), 404
        side = flow.side
    ok = orch.abort_flow(side)
    return jsonify({"success": ok, "side": side})
