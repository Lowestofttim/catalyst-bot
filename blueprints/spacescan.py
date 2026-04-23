"""Spacescan API key management + reservation diagnostics.

Three routes:
  * `/api/spacescan/status` — report configured/enabled/tier and live stats.
  * `/api/spacescan/setup` — validate and persist the Pro API key (or
    clear it), stored in user_secrets (NOT .env).
  * `/api/reservations` — list active capacity reservations (diagnostics).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event


bp = Blueprint("spacescan", __name__)


@bp.route("/api/spacescan/status")
def api_spacescan_status():
    """Check current Spacescan configuration and tier.

    Returns whether an API key is configured, the detected tier,
    and current usage stats.  Used by the first-run setup modal.
    """
    has_key = bool(getattr(cfg, "SPACESCAN_API_KEY", ""))
    enabled = getattr(cfg, "SPACESCAN_ENABLED", True)

    result = {
        "configured": has_key,
        "enabled": enabled,
        "tier": "pro" if has_key else "free",
    }
    result["advice"] = api_server._get_spacescan_plan_advice()

    try:
        from spacescan import get_api_stats
        result["stats"] = get_api_stats()
    except ImportError:
        result["stats"] = None

    return jsonify(result)


@bp.route("/api/spacescan/setup", methods=["POST"])
def api_spacescan_setup():
    """Save or clear the Spacescan API key.

    POST {"api_key": "xxx"}  → saves key, enables Pro tier
    POST {"api_key": ""}     → clears key, falls back to Free tier
    POST {"skip": true}      → marks setup as seen, stays on Free tier
    """
    cfg = api_server.cfg
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400

    if data.get("skip"):
        cfg.update("SPACESCAN_ENABLED", "true")
        log_event("info", "spacescan_setup", "User chose Free tier (no API key)")
        return jsonify({"success": True, "tier": "free", "message": "Free tier active"})

    api_key = data.get("api_key", "").strip()

    if api_key:
        # Validate the key by making a test call.
        # Uses the well-known Chia null address so we never disclose any
        # real user address to Spacescan during key verification.
        _NULL_XCH_ADDRESS = "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqs0wd5zg"
        try:
            import requests as _req
            test_resp = _req.get(
                f"https://pro-api.spacescan.io/address/xch-balance/{_NULL_XCH_ADDRESS}",
                headers={"Accept": "application/json", "x-api-key": api_key},
                timeout=10,
            )
            if test_resp.status_code == 403:
                return jsonify({"success": False, "error": "Invalid API key — Spacescan rejected it (403)"}), 400
            if test_resp.status_code == 429:
                return jsonify({"success": False, "error": "Rate limited — try again in 60 seconds"}), 429
            if test_resp.status_code >= 500:
                return jsonify({"success": False, "error": f"Spacescan server error ({test_resp.status_code}) — try again shortly"}), 502
            # 200 or 400 both mean the key passed authentication (400 = key accepted
            # but the null-address probe returned "not found" — that is fine).
        except Exception as e:
            return jsonify({"success": False, "error": f"Could not reach Spacescan: {e}"}), 502

        # Key is valid — persist in user-local secrets (NOT .env) and apply in-memory.
        import user_secrets as _user_secrets
        _user_secrets.set_secret("SPACESCAN_API_KEY", api_key)
        cfg.SPACESCAN_API_KEY = api_key
        cfg.update("SPACESCAN_ENABLED", "true")
        log_event("info", "spacescan_setup", "Pro API key configured and validated")
        return jsonify({"success": True, "tier": "pro", "message": "Pro API key saved and verified"})
    else:
        import user_secrets as _user_secrets
        _user_secrets.set_secret("SPACESCAN_API_KEY", "")
        cfg.SPACESCAN_API_KEY = ""
        cfg.update("SPACESCAN_ENABLED", "true")
        log_event("info", "spacescan_setup", "API key cleared — using Free tier")
        return jsonify({"success": True, "tier": "free", "message": "Switched to Free tier"})


@bp.route("/api/reservations")
def api_reservations():
    """List active capacity reservations (diagnostics)."""
    try:
        from reservation_manager import ReservationManager
        rm = ReservationManager()
        return jsonify({
            "totals": rm.get_reserved_totals(),
            "active": rm.list_active(),
        })
    except Exception as e:
        return api_server._api_error(e, request.path)
