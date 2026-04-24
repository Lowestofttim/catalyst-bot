"""Health, doctor, runtime, self-test, config validation and export routes.

Seven read-only routes that report on system health and config state.
Pure diagnostic surface; no mutations beyond the optional auto-repair
inside `run_runtime_checks` which is gated behind a query param.
"""

from __future__ import annotations

import time

from flask import Blueprint, Response, jsonify, request

import api_server
from config import cfg
from database import log_event


bp = Blueprint("diagnostics", __name__)


@bp.route("/api/health")
def api_health():
    """Health check endpoint — does a LIVE wallet check even when bot is stopped."""
    # Don't touch Sage RPC before the user has accepted the disclaimer.
    import chia_node
    if not chia_node.is_startup_authorised():
        return jsonify({
            "status": "ok",
            "version": api_server.get_app_version(),
            "wallet_type": api_server.get_wallet_type(),
            "bot_running": False,
            "sse_clients": api_server.events.subscriber_count,
            "timestamp": int(time.time()),
            "chia_health": {"status": "not_started", "healthy": False, "consecutive_failures": 0},
        })

    health_data = {}
    try:
        from wallet import get_chia_health
        raw_health = get_chia_health()
        # Flatten for GUI compatibility (sync indicator expects top-level fields)
        wallet_info = raw_health.get("wallet") or {}
        node_info = raw_health.get("node") or {}
        health_data = {
            "status": raw_health.get("status", "unknown"),
            "healthy": raw_health.get("healthy", False),
            "wallet_reachable": wallet_info.get("reachable", False),
            "wallet_synced": wallet_info.get("synced", False),
            "wallet_syncing": wallet_info.get("syncing", False),
            "wallet_sync_state": wallet_info.get("sync_state", "unknown"),
            "node_reachable": node_info.get("reachable", False),
            "node_synced": node_info.get("synced", False),
            "peer_count": raw_health.get("peer_count", -1),
            "consecutive_failures": 0,
        }
    except Exception as e:
        health_data = {"status": "unreachable", "error": str(e), "consecutive_failures": 0}

    bot = api_server.bot
    return jsonify({
        "status": "ok",
        "version": api_server.get_app_version(),
        "wallet_type": api_server.get_wallet_type(),
        "bot_running": bot.is_running() if bot else False,
        "sse_clients": api_server.events.subscriber_count,
        "timestamp": int(time.time()),
        "chia_health": health_data,
    })


@bp.route("/api/doctor")
def api_doctor():
    """Run preflight checks and return a structured readiness report."""
    try:
        from doctor import run_preflight
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        report = run_preflight(force=force)
        return jsonify(report.to_dict())
    except Exception as e:
        log_event("error", "api_error", f"Preflight check failed: {e}", {"endpoint": request.path})
        return jsonify({"can_start": False, "summary": "Preflight check failed — see debug log",
                        "checks": []}), 500


@bp.route("/api/health/runtime")
def api_health_runtime():
    """Run runtime health checks (read-only by default).

    Sister endpoint to /api/doctor — that one runs preflight checks (can
    the bot start?), this one runs runtime checks (is the running bot
    still in sync with reality?). Cross-checks DB vs Dexie/Sage/Spacescan.

    Query params:
        repair=true   — also execute auto-repair actions (default: read-only)
        force=true    — bypass the 60s cache and re-run now
    """
    try:
        from bot_health import run_runtime_checks
        auto_repair = request.args.get("repair", "").lower() in ("1", "true", "yes")
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        report = run_runtime_checks(auto_repair=auto_repair, force=force)
        return jsonify(report.to_dict())
    except Exception as e:
        log_event("error", "api_error", f"Runtime health check failed: {e}",
                  {"endpoint": request.path})
        return jsonify({"healthy": False,
                        "summary": "Runtime health check failed — see debug log",
                        "checks": []}), 500


@bp.route("/api/config/history")
def api_config_history():
    """Expose the config change audit trail.

    Query params:
        limit: max rows (default 50, max 500)
        key: filter to a specific config key
        since_hours: only return rows from the last N hours
    """
    try:
        from database import get_config_history
        limit = max(1, min(500, int(request.args.get("limit", 50) or 50)))
        key = request.args.get("key") or None
        since_hours = request.args.get("since_hours")
        since_hours_int = int(since_hours) if since_hours else None
        rows = get_config_history(limit=limit, key=key, since_hours=since_hours_int)
        return jsonify({"rows": rows, "count": len(rows)})
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/self-test")
def api_self_test():
    """Expose the startup self-test results to the GUI.

    Returns the self-test results captured at the last bot startup, OR
    runs a fresh self-test if force=1 is passed. The GUI can show the
    user what services are down and what features will be missing.
    """
    try:
        bot = api_server.bot
        force = request.args.get("force", "").lower() in ("1", "true", "yes")
        if force and bot:
            try:
                bot._run_startup_self_test()
            except Exception as e:
                return jsonify({"error": f"self-test failed: {e}"}), 500
        results = getattr(bot, "_startup_self_test_results", {}) if bot else {}
        all_ok = all(r.get("ok", False) for r in results.values()
                     if not r.get("skipped", False))
        return jsonify({
            "all_ok": all_ok,
            "results": results,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/config/validate")
def api_config_validate():
    """Validate current config and return issues.
    Uses the cached report from the last reload if available (fast path);
    falls back to a fresh run when the cache is absent."""
    try:
        cached = getattr(cfg, "_validation_report", None)
        if cached is not None:
            return jsonify(cached.to_dict())
        from config_validator import validate_config
        report = validate_config(cfg)
        return jsonify(report.to_dict())
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/config/export-env")
def api_config_export_env():
    """Export current config as a .env file download.

    Only exports keys in cfg._UPDATABLE_KEYS (same set the GUI can write).
    Sensitive wallet credentials, cert paths, and RPC URLs are excluded.
    """
    try:
        sections = [
            ("Trading Core", [
                "LOOP_SECONDS", "SPREAD_BPS", "DEFAULT_TRADE_XCH",
                "MAX_ACTIVE_BUY", "MAX_ACTIVE_SELL",
                "ENABLE_BUY", "ENABLE_SELL", "DRY_RUN",
            ]),
            ("Reserves", [
                "XCH_RESERVE", "CAT_RESERVE",
            ]),
            ("Auto-Requote", [
                "AUTO_REQUOTE", "REQUOTE_BPS", "REQUOTE_COOLDOWN_SECS",
                "REQUOTE_BATCH_SIZE",
            ]),
            ("Price Safety & Limits", [
                "HARD_MIN_PRICE_XCH", "HARD_MAX_PRICE_XCH",
                "DYNAMIC_LIMIT_PCT", "MAX_STEP_CHANGE_FRACTION",
            ]),
            ("Smart Pricing - Dynamic Spreads", [
                "DYNAMIC_SPREAD_ENABLED", "BASE_SPREAD_BPS",
                "MIN_EDGE_BPS", "MIN_SPREAD_BPS", "MAX_SPREAD_BPS",
                "VOLATILITY_WINDOW_HOURS",
            ]),
            ("Smart Pricing - Inventory Management", [
                "INVENTORY_ENABLED", "SKEW_INTENSITY", "MAX_POSITION_XCH",
            ]),
            ("Tiered Orders", [
                "TIER_ENABLED", "BUY_LADDER_REVERSED",
                "INNER_SIZE_XCH", "MID_SIZE_XCH",
                "OUTER_SIZE_XCH", "EXTREME_SIZE_XCH",
                "INNER_TIER_COUNT", "MID_TIER_COUNT",
                "OUTER_TIER_COUNT", "EXTREME_TIER_COUNT",
                "BUY_INNER_TIER_COUNT", "BUY_MID_TIER_COUNT",
                "BUY_OUTER_TIER_COUNT", "BUY_EXTREME_TIER_COUNT",
                "SELL_INNER_TIER_COUNT", "SELL_MID_TIER_COUNT",
                "SELL_OUTER_TIER_COUNT", "SELL_EXTREME_TIER_COUNT",
                "INNER_TIER_SPARE_COUNT", "MID_TIER_SPARE_COUNT",
                "OUTER_TIER_SPARE_COUNT", "EXTREME_TIER_SPARE_COUNT",
                "BUY_INNER_TIER_SPARE_COUNT", "BUY_MID_TIER_SPARE_COUNT",
                "BUY_OUTER_TIER_SPARE_COUNT", "BUY_EXTREME_TIER_SPARE_COUNT",
                "SELL_INNER_TIER_SPARE_COUNT", "SELL_MID_TIER_SPARE_COUNT",
                "SELL_OUTER_TIER_SPARE_COUNT", "SELL_EXTREME_TIER_SPARE_COUNT",
            ]),
            ("Market Intelligence", [
                "COMPETITOR_AWARE_ENABLED", "DBX_MAX_SPREAD_BPS",
            ]),
            ("Bot Operations", [
                "SNIPER_ENABLED", "SNIPER_SIZE_XCH", "SNIPER_PREP_COUNT",
                "SNIPER_REARM_PRICE_MOVE_BPS", "SNIPER_REARM_GAP_MOVE_BPS",
                "TRANSACTION_FEE_MODE", "TRANSACTION_FEE_XCH",
                "TRANSACTION_FEE_TARGET_SECS",
                "FEE_PREP_COUNT", "FEE_COIN_SIZE_XCH",
                "SPLASH_ENABLED", "ENABLE_COIN_PREP",
                "ENABLE_RUNTIME_COIN_HEALTH", "SAGE_SET_CHANGE_ADDRESS",
                "COIN_PREP_MULTIPLIER", "COIN_PREP_HEADROOM_PCT",
            ]),
            ("CAT Token", [
                "CAT_ASSET_ID", "CAT_TICKER_ID", "CAT_NAME", "CAT_DECIMALS",
            ]),
        ]

        lines = ["# CATalyst — exported settings", "# Generated by bot GUI export", ""]
        emitted = set()

        for section_name, keys in sections:
            section_lines = []
            for key in keys:
                if key in emitted:
                    continue
                val = getattr(cfg, key, None)
                if val is None:
                    continue
                section_lines.append(f"{key}={val}")
                emitted.add(key)
            if section_lines:
                lines.append(f"# --- {section_name} ---")
                lines.extend(section_lines)
                lines.append("")

        content = "\n".join(lines)
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=chia_bot_settings.env"},
        )
    except Exception as e:
        return api_server._api_error(e, request.path)
