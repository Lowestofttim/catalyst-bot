"""Splash P2P offer-broadcasting routes.

Eleven routes covering the Splash binary lifecycle (setup, download,
start), inbound offer webhook (rate-limited, token-exempt), stats and
toggle. Shared state (`bot`, `events`) is read from `api_server` via
attribute access so reassignments (`create_bot()`) are picked up.
"""

from __future__ import annotations

import hashlib
import sys
import time

from flask import Blueprint, current_app, jsonify, request

import api_server
from config import cfg
from database import log_event


bp = Blueprint("splash", __name__)


def _api_server():
    """Return the currently loaded api_server module for reload-safe routes."""
    try:
        owner = current_app.config.get("_CATALYST_API_SERVER_MODULE")
        return owner or sys.modules.get("api_server", api_server)
    except RuntimeError:
        return sys.modules.get("api_server", api_server)


@bp.route("/api/splash/stats")
def api_splash_stats():
    """Get Splash P2P broadcasting statistics."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    stats = bot.splash_manager.get_stats()
    health = bot.splash_manager.check_health()
    stats["health"] = health
    try:
        stats["receive"] = bot.get_splash_receive_stats()
    except Exception:
        pass
    return jsonify(stats)


@bp.route("/api/splash/receive", methods=["GET", "POST"])
def api_splash_receive():
    """Get or update inbound Splash listening state."""
    bot = api_server.bot
    cfg = api_server.cfg
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    if request.method == "GET":
        return jsonify(api_server._serialize_dict(bot.get_splash_receive_stats()))

    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    cfg.update("SPLASH_RECEIVE_ENABLED", "true" if enabled else "false")

    node_action = "unchanged"
    try:
        node_running = bool(bot.splash_node.is_running())
    except Exception:
        node_running = False

    try:
        if node_running:
            bot.splash_node.stop()
            time.sleep(1)
            if enabled or getattr(cfg, "SPLASH_ENABLED", False):
                restarted = bot.splash_node.start()
                node_action = "restarted" if restarted else "restart_failed"
            else:
                node_action = "stopped"
        elif enabled or getattr(cfg, "SPLASH_ENABLED", False):
            started = bot.splash_node.start()
            node_action = "started" if started else "start_failed"
    except Exception as e:
        node_action = f"error:{e}"

    log_event(
        "info",
        "splash_receive_toggled",
        f"Splash listening {'enabled' if enabled else 'disabled'} ({node_action})"
    )

    payload = bot.get_splash_receive_stats()
    api_server.events.emit("splash_incoming", payload)
    api_server.events.emit("config_changed", {
        "key": "SPLASH_RECEIVE_ENABLED",
        "value": enabled,
        "source": "splash_receive_toggle",
    })

    return jsonify({
        "success": True,
        "enabled": enabled,
        "node_action": node_action,
        "stats": api_server._serialize_dict(payload),
    })


@bp.route("/api/splash/node")
def api_splash_node():
    """Get Splash P2P node status (binary process health)."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.splash_node.get_status())


@bp.route("/api/splash/node/start", methods=["POST"])
def api_splash_node_start():
    """Start the Splash P2P node process (used by startup gate)."""
    bot = api_server.bot
    cfg = api_server.cfg
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        if not getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
            cfg.update("SPLASH_RECEIVE_ENABLED", "true")
            log_event(
                "info",
                "splash_receive_startup_default",
                "Splash incoming listener enabled by default for node startup",
            )
        started = bot.splash_node.start()
        status = bot.splash_node.get_status()
        return jsonify({
            "success": started,
            "message": "Splash node started" if started else "Failed to start Splash node",
            "status": status
        })
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/splash/node/output")
def api_splash_node_output():
    """Get recent output lines from the Splash node process."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    lines = int(request.args.get("lines", 20))
    return jsonify({"output": bot.splash_node.get_recent_output(lines)})


@bp.route("/api/splash/setup/check")
def api_splash_setup_check():
    """Check if Splash binary is installed and get platform info."""
    try:
        from splash_setup import check_installed
        return jsonify(check_installed())
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/splash/setup/download", methods=["POST"])
def api_splash_setup_download():
    """Start downloading the Splash binary (non-blocking)."""
    try:
        from splash_setup import start_background_download
        result = start_background_download()
        return jsonify(result)
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/splash/setup/progress")
def api_splash_setup_progress():
    """Get download progress (poll this during download)."""
    try:
        from splash_setup import get_download_status
        return jsonify(get_download_status())
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/splash/setup/release")
def api_splash_setup_release():
    """Get latest Splash release info from GitHub."""
    try:
        from splash_setup import get_latest_release, detect_platform
        release = get_latest_release()
        platform_info = detect_platform()
        return jsonify({
            "release": release,
            "platform": platform_info,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/splash/incoming", methods=["POST"])
def api_splash_incoming():
    """Webhook for receiving offers from the Splash P2P network.

    Splash binary can be configured to POST incoming offers here.
    We store them in the database for potential future sniper use.

    Loopback-only and token-exempt, but rate-limited to prevent a
    pathological flood from amplifying into unbounded DB writes.
    """
    server = _api_server()
    cfg = server.cfg
    if not getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
        return jsonify({"error": "Splash receive disabled"}), 403

    origin = request.headers.get("Origin", "").strip()
    if origin and not server._is_loopback_origin(origin):
        return jsonify({"error": "loopback_origin_required"}), 403

    if not request.is_json:
        return jsonify({"error": "JSON body required"}), 415

    # Dedicated rate limiter (defined in api_server) — 200/sec is generous
    # for a real local Splash binary but stops abuse.
    if server._splash_incoming_rate_limited():
        return jsonify({"error": "rate_limited"}), 429

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400
    offer_bech32 = data.get("offer", "")

    if not offer_bech32 or not isinstance(offer_bech32, str):
        return jsonify({"error": "Missing 'offer' field"}), 400

    # Hard cap offer string length — real Chia offers are a few KB, not MB.
    if len(offer_bech32) > 32768:
        return jsonify({"error": "Offer too large"}), 413

    if not offer_bech32.lower().startswith("offer1"):
        return jsonify({"error": "Invalid offer format"}), 400

    try:
        fp = hashlib.sha256(offer_bech32.strip().encode("utf-8")).hexdigest()
        source_ip = request.remote_addr

        from database import record_splash_incoming
        was_new = record_splash_incoming(offer_bech32, fp, source_ip=source_ip)

        if was_new:
            log_event("debug", "splash_received",
                      f"Received new offer from Splash (fp: {fp[:16]}...)")
            bot = server.bot
            if bot:
                try:
                    server.events.emit("splash_incoming", bot.get_splash_receive_stats())
                except Exception:
                    pass

        return jsonify({"ok": True, "new": was_new})
    except Exception as e:
        return server._api_error(e, request.path)


@bp.route("/api/splash/incoming/list")
def api_splash_incoming_list():
    """List recent offers received from Splash network."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        status_filter = request.args.get("status")
        limit = int(request.args.get("limit", 50))
        from database import get_splash_incoming_offers
        offers = get_splash_incoming_offers(status=status_filter, limit=limit)
        return jsonify({"offers": offers, "count": len(offers)})
    except Exception as e:
        return api_server._api_error(e, request.path)
