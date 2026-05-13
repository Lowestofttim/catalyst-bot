"""Sage wallet lifecycle + full-node status + fingerprint routes.

Nine routes covering: wallet-type/fingerprint queries, the Sage preload
startup sequence (probe, retry, begin-startup, fingerprints, start-with-
fingerprint), cert-path auto-detection, and full-node RPC status.
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event


bp = Blueprint("sage", __name__)

_STARTUP_MESSAGES = {
    "idle": "Startup thread not running",
    "starting": "Wallet services are starting...",
    "launching": "Launching wallet application...",
    "rpc_disabled": "Sage is open but RPC is not enabled",
    "waiting_certs": "Sage needs certificate configuration",
    "waiting_fingerprint": "Wallet connected - select a wallet",
    "version_blocked": "Sage version is not supported",
    "ready": "Wallet is healthy",
    "syncing": "Wallet is syncing...",
    "error": "Wallet startup status unavailable",
}


def _safe_startup_phase(status):
    if not isinstance(status, dict):
        return "error"
    if status.get("error"):
        return "error"
    text = str(status.get("phase") or "").strip().lower()
    if text == "idle":
        return "idle"
    if text == "launching":
        return "launching"
    if text == "rpc_disabled":
        return "rpc_disabled"
    if text == "waiting_certs":
        return "waiting_certs"
    if text == "waiting_fingerprint":
        return "waiting_fingerprint"
    if text == "version_blocked":
        return "version_blocked"
    if text == "ready":
        return "ready"
    if text == "syncing":
        return "syncing"
    if text == "error":
        return "error"
    return "starting"


def _safe_node_status(status):
    if not isinstance(status, dict):
        return "unknown"
    text = str(status.get("node_status") or "").strip().lower()
    if text == "checking":
        return "checking"
    if text == "healthy":
        return "healthy"
    if text == "syncing":
        return "syncing"
    if text == "node_not_synced":
        return "node_not_synced"
    if text == "unreachable":
        return "unreachable"
    return "unknown"


def _safe_wallet_type(status):
    if not isinstance(status, dict):
        return "sage"
    text = str(status.get("wallet_type") or "").strip().lower()
    if text == "chia":
        return "chia"
    return "sage"


def _safe_digit_text(value):
    text = str(value or "").strip()
    return text if text.isdigit() else ""


def _safe_int(value, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _safe_version_text(value, default=""):
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return default
    allowed = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-+_")
    return text if all(ch in allowed for ch in text) else default


def _safe_wallet_service_result(result):
    if isinstance(result, dict) and result.get("success"):
        return {"success": True, "message": "Wallet services start requested"}

    log_event(
        "warning",
        "wallet_service_start_failed",
        "Wallet service start request failed",
    )
    return {"success": False, "error": "Could not start wallet services"}


def _safe_wallet_start_result(result):
    if isinstance(result, dict) and result.get("success"):
        return {"success": True, "message": "Wallet start requested"}

    payload = {"success": False, "error": "Could not start selected wallet"}
    if isinstance(result, dict) and result.get("unsupported_version"):
        installed = _safe_version_text(result.get("sage_version"), "unknown")
        minimum = _safe_version_text(
            result.get("sage_min_required_version"),
            str(getattr(api_server, "MIN_SUPPORTED_SAGE_VERSION", "0.12.10")),
        )
        payload.update({
            "unsupported_version": True,
            "error": "Sage version is not supported",
            "sage_version": installed,
            "sage_min_required_version": minimum,
        })
    else:
        log_event(
            "warning",
            "wallet_fingerprint_start_failed",
            "Wallet fingerprint start request failed",
        )
    return payload



@bp.route("/api/fingerprint")
def api_fingerprint():
    """Get wallet fingerprint — prefer the live wallet session over saved config."""
    cfg = api_server.cfg
    try:
        # Don't touch Sage RPC before the user has accepted the disclaimer.
        import chia_node
        if not chia_node.is_startup_authorised():
            return jsonify({"fingerprint": "", "source": "not_started"})

        fp = None

        # 1. Prefer the live wallet session.
        try:
            from wallet import get_wallet_type
            wtype = get_wallet_type()
            if wtype == "sage":
                from wallet_sage import get_current_key
                key = get_current_key()
                if key and key.get("fingerprint"):
                    fp = str(key["fingerprint"])
            else:
                from wallet import rpc
                result = rpc("get_logged_in_fingerprint", {}, timeout=5)
                if result and result.get("success") and result.get("fingerprint"):
                    fp = str(result.get("fingerprint"))
        except Exception:
            pass

        # 2. Fall back to configured values when live detection is unavailable.
        if not fp:
            fp = cfg.WALLET_FINGERPRINT if hasattr(cfg, 'WALLET_FINGERPRINT') and cfg.WALLET_FINGERPRINT else None
        if not fp:
            fp = os.getenv("WALLET_FINGERPRINT", "")

        fp = _safe_digit_text(fp)
        return jsonify({"success": bool(fp), "fingerprint": fp or "Not detected"})
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/full-node/status", methods=["GET"])
def api_full_node_status():
    """Report the mempool watcher's current source and session counters.

    Used by the dashboard to show whether the bot is polling a local Chia
    full node or falling back to Coinset. Returns the configured URL
    (credentials paths are NOT returned) and call counts for both sources
    so operators can see a clean handover after enabling the full-node
    option.
    """
    status = {
        "success": True,
        "full_node_enabled": bool(getattr(cfg, "FULL_NODE_ENABLED", False)),
        "full_node_url": str(getattr(cfg, "FULL_NODE_RPC_URL", "") or ""),
        "full_node_cert_configured": bool(getattr(cfg, "FULL_NODE_CERT_PATH", "")),
        "full_node_key_configured": bool(getattr(cfg, "FULL_NODE_KEY_PATH", "")),
        "full_node_timeout": int(getattr(cfg, "FULL_NODE_TIMEOUT", 5) or 5),
        "active_source": "coinset",
        "full_node_calls": 0,
        "coinset_calls": 0,
        "fill_warn_hits": 0,
        "fill_warn_misses": 0,
    }
    try:
        import mempool_watcher as _mw
        w = getattr(_mw, "_watcher_instance", None)
        if w is not None:
            status["active_source"] = (
                "full_node" if getattr(w, "_full_node_active", False) else "coinset"
            )
            status["full_node_calls"] = int(
                getattr(w, "_full_node_api_calls", 0) or 0
            )
            status["coinset_calls"] = int(
                getattr(w, "_coinset_api_calls", 0) or 0
            )
            status["fill_warn_hits"] = int(getattr(w, "_fill_warn_hits", 0) or 0)
            status["fill_warn_misses"] = int(
                getattr(w, "_fill_warn_misses", 0) or 0
            )
    except Exception as _err:
        status["watcher_error"] = str(_err)
    return jsonify(status)


@bp.route("/api/wallet/sage-running", methods=["GET"])
def api_wallet_sage_running():
    """Quick non-intrusive check: is Sage RPC reachable right now?

    Does not start anything — just probes the port. Used by the GUI to decide
    whether to show 'Launch Sage for me' or 'Connect to Sage'.
    """
    try:
        import sage_node
        authenticated = sage_node._is_sage_rpc_available()
        port_listening = authenticated or sage_node._is_sage_rpc_port_listening()
        return jsonify({
            "running": bool(port_listening),
            "rpc_authenticated": bool(authenticated),
            "rpc_port_listening": bool(port_listening),
        })
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/wallet/retry-sage-connect", methods=["POST"])
def api_wallet_retry_sage_connect():
    """Reset and restart the Sage preload after user enables RPC."""
    try:
        import sage_node
        sage_node.reset_preload()
        sage_node.start_preload()
        return jsonify({"started": True})
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/daemon/start", methods=["POST"])
def api_sage_daemon_start():
    """Legacy GUI alias for starting wallet services.

    Sage itself has no Chia daemon, but old frontend code still calls this
    endpoint before logging in. Route it through sage_node.start_chia(), which
    already returns a safe no-op success for Sage and starts Chia services when
    WALLET_TYPE=chia.
    """
    try:
        data = request.get_json(silent=True) or {}
        services = str(data.get("services", "all") or "all").lower().strip()
        import sage_node
        result = sage_node.start_chia(services)
        return jsonify(_safe_wallet_service_result(result))
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/wallet/begin-startup", methods=["POST"])
def api_wallet_begin_startup():
    """Trigger wallet preload after the user has chosen how to connect.

    Accepts optional JSON body: {"auto_launch": bool}
      auto_launch=true  (default) — bot may launch Sage exe if not running
      auto_launch=false           — user will open Sage; bot only waits/connects

    Safe to call multiple times — start_preload() is a no-op if already running.
    """
    try:
        data = request.get_json(silent=True) or {}
        auto_launch = data.get("auto_launch", True)
        import chia_node
        chia_node.set_auto_launch(bool(auto_launch))
        chia_node.start_preload()
        return jsonify({"started": True})
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/startup-status")
def api_chia_startup_status():
    """Get current Chia startup phase for the main GUI to display."""
    try:
        import chia_node
        import sage_node
        status = chia_node.get_startup_status()
        phase = _safe_startup_phase(status)
        wallet_type = _safe_wallet_type(status)
        payload = {
            "phase": phase,
            "message": _STARTUP_MESSAGES[phase],
            "fingerprint": _safe_digit_text(
                getattr(sage_node, "_selected_fingerprint", "")
            ),
            "node_status": _safe_node_status(status),
            "preload_running": bool(getattr(sage_node, "_preload_running", False)),
            "wallet_type": wallet_type,
        }

        if phase == "syncing":
            cached_status = getattr(sage_node, "_node_status_cache", {}) or {}
            payload["sync_progress"] = _safe_int(
                cached_status.get("sync_progress_height")
            )
            payload["sync_tip"] = _safe_int(cached_status.get("sync_tip_height"))

        if wallet_type == "sage" and phase not in ("idle", "waiting_certs"):
            try:
                version_gate = sage_node.get_sage_version_requirement()
                minimum = _safe_version_text(
                    version_gate.get("minimum_required_version")
                )
                if minimum:
                    payload["sage_min_required_version"] = minimum
                installed = _safe_version_text(version_gate.get("installed_version"))
                if installed and installed != "unknown":
                    payload["sage_version"] = installed
                if version_gate.get("supported") is False:
                    payload["sage_version_supported"] = False
                    payload["sage_version_requirement_message"] = (
                        "Sage version is not supported"
                    )
                elif version_gate.get("supported") is True:
                    payload["sage_version_supported"] = True
            except Exception:
                payload["sage_version_supported"] = None
                payload["sage_version_requirement_message"] = (
                    "Unable to determine Sage version support"
                )

        return jsonify(payload)
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/fingerprints")
def api_chia_fingerprints():
    """List available wallet fingerprints for the startup selection screen."""
    try:
        import chia_node
        fps = chia_node.get_available_fingerprints()
        return jsonify({"success": True, "fingerprints": fps})
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/start-with-fingerprint", methods=["POST"])
def api_chia_start_with_fingerprint():
    """Start Chia with a user-selected fingerprint."""
    try:
        import chia_node
        data = request.get_json(silent=True)

        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid request body"}), 400
        fingerprint = str(data.get("fingerprint", "")).strip()
        if not fingerprint or not fingerprint.isdigit():
            return jsonify({"success": False, "error": "Invalid fingerprint"}), 400

        result = chia_node.trigger_start(fingerprint)
        return jsonify(_safe_wallet_start_result(result))
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/fingerprint", methods=["POST"])
def api_sage_set_fingerprint():
    """Persist and start the user-selected Sage wallet fingerprint."""
    try:
        import chia_node
        data = request.get_json(silent=True)

        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid request body"}), 400

        fingerprint = str(data.get("fingerprint", "")).strip()
        if not fingerprint or not fingerprint.isdigit():
            return jsonify({"success": False, "error": "Invalid fingerprint"}), 400

        bot = api_server.bot
        if bot and bot.is_running():
            return jsonify({
                "success": False,
                "error": "Stop the bot before changing wallet fingerprint",
            }), 409

        available_values = set()
        for item in chia_node.get_available_fingerprints() or []:
            value = item.get("fingerprint") if isinstance(item, dict) else item
            value = str(value or "").strip()
            if value.isdigit():
                available_values.add(value)
        if fingerprint not in available_values:
            return jsonify({
                "success": False,
                "fingerprint": fingerprint,
                "error": "Selected Sage fingerprint is not available",
            }), 400

        result = chia_node.trigger_start(fingerprint)
        if not result.get("success"):
            safe_result = _safe_wallet_start_result(result)
            return jsonify({
                "success": False,
                "fingerprint": fingerprint,
                **safe_result,
            }), 400

        ok = api_server.cfg.update(
            "SAGE_FINGERPRINT",
            fingerprint,
            source="sage_wallet_settings",
            note="User selected Sage wallet fingerprint",
        )
        if not ok:
            return jsonify({
                "success": False,
                "error": "Could not save Sage fingerprint",
            }), 500

        os.environ["SAGE_FINGERPRINT"] = fingerprint

        return jsonify({
            "success": True,
            "fingerprint": fingerprint,
            "message": "Sage fingerprint saved",
        })
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/cert-candidates")
def api_sage_cert_candidates():
    """Return likely Sage wallet.crt paths for UI pre-fill."""
    try:
        import sage_node
        data_dir = str(request.args.get("data_dir", "") or "").strip()
        extra_dirs = [data_dir] if data_dir else None
        candidates = sage_node.get_sage_cert_candidates(extra_dirs)
        detected = sage_node.detect_sage_cert_path(extra_dirs)
        suggested = detected or (candidates[0] if candidates else "")
        return jsonify({
            "success": True,
            "candidates": candidates,
            "suggested_cert_path": suggested,
            "detected_cert_path": detected or "",
        })
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/sage/setup-certs", methods=["POST"])
def api_sage_setup_certs():
    """Auto-detect or set Sage certificate paths.

    POST with {"cert_path": "...", "key_path": "..."} to set manually,
    {"data_dir": "..."} to search a custom Sage data root, or {} to
    auto-detect from common Sage locations.
    """
    try:
        import sage_node
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid request body"}), 400

        cert_path = data.get("cert_path", "").strip()
        key_path = data.get("key_path", "").strip()
        data_dir = data.get("data_dir", "").strip()
        extra_dirs = [data_dir] if data_dir else None

        if not cert_path:
            detected = sage_node.detect_sage_cert_path(extra_dirs)
            if detected:
                cert_path = detected
                key_path = os.path.join(os.path.dirname(detected), "wallet.key")
            else:
                return jsonify({
                    "success": False,
                    "error": "Could not auto-detect Sage certificates. "
                             "Use Browse in the desktop app, or Paste the "
                             "full path to Sage's ssl\\wallet.crt.",
                }), 404

        ok, reason, cert_path, key_path = sage_node.validate_sage_cert_pair(cert_path, key_path)
        if not ok:
            log_event("warning", "sage_cert_path_rejected",
                      f"Rejected Sage certificate selection: {reason}")
            return jsonify({"success": False, "error": reason}), 400

        sage_data_dir = os.path.dirname(os.path.dirname(cert_path))

        os.environ["SAGE_CERT_PATH"] = cert_path
        os.environ["SAGE_KEY_PATH"] = key_path
        os.environ["SAGE_DATA_DIR"] = sage_data_dir
        try:
            try:
                from user_paths import env_file as _env_file
                env_path = _env_file()
            except Exception:
                env_path = os.path.join(os.path.dirname(os.path.abspath(api_server.__file__)), ".env")
            lines = []
            if os.path.isfile(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            for key, val in [
                ("SAGE_CERT_PATH", cert_path),
                ("SAGE_KEY_PATH", key_path),
                ("SAGE_DATA_DIR", sage_data_dir),
            ]:
                found = False
                for i, line in enumerate(lines):
                    if line.strip().startswith(f"{key}="):
                        lines[i] = f"{key}={val}\n"
                        found = True
                        break
                if not found:
                    lines.append(f"{key}={val}\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as env_err:
            print(f"[Sage] Warning: could not update .env: {env_err}")

        try:
            cfg.reload()
        except Exception:
            pass
        try:
            import wallet_sage
            wallet_sage.reload_connection_settings()
        except Exception as reload_err:
            print(f"[Sage] Warning: could not refresh wallet_sage config: {reload_err}")

        return jsonify({
            "success": True,
            "message": "Certificate paths saved",
            "cert_path": cert_path,
            "key_path": key_path,
            "data_dir": sage_data_dir,
        })
    except Exception:
        return api_server._api_exception(request.path)
