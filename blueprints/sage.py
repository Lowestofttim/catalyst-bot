"""Sage wallet lifecycle + full-node status + fingerprint routes.

Nine routes covering: wallet-type/fingerprint queries, the Sage preload
startup sequence (probe, retry, begin-startup, fingerprints, start-with-
fingerprint), cert-path auto-detection, and full-node RPC status.
"""

from __future__ import annotations

import os
import sys

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event


bp = Blueprint("sage", __name__)


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

        return jsonify({"success": bool(fp), "fingerprint": fp or "Not detected"})
    except Exception as e:
        return api_server._api_error(e, request.path)


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
        running = sage_node._is_sage_rpc_available()
        return jsonify({"running": running})
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/wallet/retry-sage-connect", methods=["POST"])
def api_wallet_retry_sage_connect():
    """Reset and restart the Sage preload after user enables RPC."""
    try:
        import sage_node
        sage_node.reset_preload()
        sage_node.start_preload()
        return jsonify({"started": True})
    except Exception as e:
        return api_server._api_error(e, request.path)


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
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/sage/startup-status")
def api_chia_startup_status():
    """Get current Chia startup phase for the main GUI to display."""
    try:
        import chia_node
        return jsonify(chia_node.get_startup_status())
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/sage/fingerprints")
def api_chia_fingerprints():
    """List available wallet fingerprints for the startup selection screen."""
    try:
        import chia_node
        fps = chia_node.get_available_fingerprints()
        return jsonify({"success": True, "fingerprints": fps})
    except Exception as e:
        return api_server._api_error(e, request.path)


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
        return jsonify(result)
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/sage/setup-certs", methods=["POST"])
def api_sage_setup_certs():
    """Auto-detect or set Sage certificate paths.

    POST with {"cert_path": "...", "key_path": "..."} to set manually,
    or POST with {} to auto-detect from common Sage install locations.
    """
    try:
        import chia_node
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid request body"}), 400

        cert_path = data.get("cert_path", "").strip()
        key_path = data.get("key_path", "").strip()

        if not cert_path:
            detected = chia_node._detect_sage_cert_path()
            if detected:
                cert_path = detected
                key_path = detected.replace("wallet.crt", "wallet.key")
            else:
                return jsonify({
                    "success": False,
                    "error": "Could not auto-detect Sage certificates. "
                             "Please provide the path manually.",
                }), 404

        # Safety: only accept paths inside a known Sage data directory.
        # This prevents a local attacker (or compromised .env) from pointing
        # the bot at an arbitrary TLS cert elsewhere on the filesystem.
        def _is_inside_allowed_sage_dir(path: str) -> bool:
            try:
                real = os.path.realpath(path)
            except Exception:
                return False
            allowed_roots = []
            if sys.platform == "win32":
                appdata = os.environ.get("APPDATA")
                if appdata:
                    allowed_roots.append(os.path.realpath(
                        os.path.join(appdata, "com.rigidnetwork.sage")
                    ))
            elif sys.platform == "darwin":
                allowed_roots.append(os.path.realpath(
                    os.path.expanduser("~/Library/Application Support/com.rigidnetwork.sage")
                ))
            else:
                allowed_roots.append(os.path.realpath(
                    os.path.expanduser("~/.local/share/com.rigidnetwork.sage")
                ))
            # Also allow paths inside the bot's own directory (for bundled certs)
            allowed_roots.append(os.path.realpath(os.path.dirname(os.path.abspath(api_server.__file__))))
            for root in allowed_roots:
                if real == root or real.startswith(root + os.sep):
                    return True
            return False

        if not _is_inside_allowed_sage_dir(cert_path):
            log_event("warning", "sage_cert_path_rejected",
                      f"Rejected cert_path outside allowed Sage data dir: {cert_path}")
            return jsonify({
                "success": False,
                "error": "Cert path must be inside the Sage wallet data directory. "
                         "Leave the field blank to auto-detect.",
            }), 400

        if not os.path.isfile(cert_path):
            log_event("warning", "sage_cert_missing", f"Cert not found: {cert_path}")
            return jsonify({"success": False, "error": "Certificate file not found at the specified path"}), 400
        if not key_path:
            key_path = cert_path.replace(".crt", ".key")
        if not _is_inside_allowed_sage_dir(key_path):
            log_event("warning", "sage_key_path_rejected",
                      f"Rejected key_path outside allowed Sage data dir: {key_path}")
            return jsonify({
                "success": False,
                "error": "Key path must be inside the Sage wallet data directory.",
            }), 400
        if not os.path.isfile(key_path):
            log_event("warning", "sage_key_missing", f"Key not found: {key_path}")
            return jsonify({"success": False, "error": "Key file not found at the expected path"}), 400

        os.environ["SAGE_CERT_PATH"] = cert_path
        os.environ["SAGE_KEY_PATH"] = key_path
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
            for key, val in [("SAGE_CERT_PATH", cert_path), ("SAGE_KEY_PATH", key_path)]:
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

        return jsonify({
            "success": True,
            "message": "Certificate paths saved to .env",
        })
    except Exception as e:
        return api_server._api_error(e, request.path)
