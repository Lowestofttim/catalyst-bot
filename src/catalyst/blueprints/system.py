"""Deprecated console + wallet backend switch routes.

Four routes:
  * `/api/console/{status,toggle}` — legacy no-ops kept stable-shaped so
    stale clients don't error out. The external console was removed
    2026-04-06; the in-app Logs view replaced it.
  * `/api/wallets/{detect,switch}` — probe Chia/Sage wallets and persist
    a wallet-type preference to `.env` for the next restart.
"""

from __future__ import annotations

import sys

from flask import Blueprint, jsonify, request

import api_server
from database import log_event


bp = Blueprint("system", __name__)


@bp.route("/api/console/status")
def api_console_status():
    """Legacy — the external console popup was removed 2026-04-06.

    The GUI now uses the in-app Logs view (sidebar → Logs) instead of
    toggling a separate console window. Kept as a stable-shaped no-op
    so any stale clients don't error out.
    """
    return jsonify({
        "main_visible": False,
        "coin_prep_visible": False,
        "coin_prep_running": api_server._coin_prep_state.get("running", False),
        "platform": sys.platform,
        "deprecated": True,
    })


@bp.route("/api/console/toggle", methods=["POST"])
def api_console_toggle():
    """Legacy — external console popup was eliminated to remove the
    'closing the console kills the bot' footgun. Clients should use
    the in-app Logs view instead."""
    return jsonify({
        "success": False,
        "deprecated": True,
        "error": "The external console has been removed — use the in-app Logs view (sidebar → Logs)",
    })


@bp.route("/api/wallets/detect")
def api_wallets_detect():
    """Probe both Chia and Sage wallets using their own RPC modules.

    Uses the actual wallet modules (wallet_chia.py and wallet_sage.py)
    which already have all the connection logic, certs, and retry handling.
    """
    detected = []

    try:
        from wallet_chia import rpc as chia_rpc
        result = chia_rpc("get_sync_status", {}, timeout=3)
        if result and result.get("success"):
            detected.append({
                "type": "chia",
                "label": "Chia Wallet",
                "icon": "🌿",
                "port": 9256,
                "reachable": True,
                "synced": result.get("synced", False),
                "syncing": result.get("syncing", False),
            })
    except Exception:
        pass

    # --- Sage wallet detection disabled for now ---
    # Sage RPC requires specific SSL certs that aren't easily auto-detected.

    current = api_server.get_wallet_type()
    return jsonify({
        "success": True,
        "current": current,
        "detected": detected,
    })


@bp.route("/api/wallets/switch", methods=["POST"])
def api_wallets_switch():
    """Switch the active wallet backend (requires restart to take effect)."""
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    new_type = data.get("wallet_type", "").strip().lower()
    if new_type not in ("chia", "sage"):
        return jsonify({"success": False, "error": "Invalid wallet type. Use 'chia' or 'sage'."})

    try:
        # WALLET_TYPE is intentionally excluded from _UPDATABLE_KEYS because hot-reloading it
        # mid-run would break all wallet operations. This endpoint only persists it for the
        # next restart, so we write to .env directly without triggering a live reload.
        from dotenv import set_key as _set_key
        from config import _ENV_PATH
        _set_key(_ENV_PATH, "WALLET_TYPE", new_type)
        log_event("info", "wallet_switch", f"Wallet switched to {new_type} — restart required")
        return jsonify({
            "success": True,
            "wallet_type": new_type,
            "message": f"Switched to {new_type}. Please restart the bot for the change to take effect.",
            "restart_required": True,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)
