"""Central Sage wallet runtime and startup layer for the dashboard backend

Sits between `api_server` and `wallet_sage`, adding lifecycle and
dashboard-specific helpers on top of raw wallet RPC. Handles fingerprint
discovery and login orchestration, daemon and node status polling, coin
display (including lock detection), and transaction history formatting.
Also enforces a minimum supported Sage version via
`MIN_SUPPORTED_SAGE_VERSION`.

Key responsibilities:
    - Wallet/daemon startup, login, and fingerprint selection
    - Dashboard aggregation: balance, coins, history, node status
    - Background preload of node status with short-TTL cache
    - Version gating against MIN_SUPPORTED_SAGE_VERSION

Data formatting here is GUI-oriented; pure RPC primitives live in
wallet_sage.py.
"""

import os
import re
import time
import subprocess
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from database import log_event
from win_subprocess import hidden_subprocess_kwargs


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_daemon_lock = threading.Lock()
_last_daemon_status: Dict = {}
_last_daemon_check: float = 0
_DAEMON_CACHE_SECONDS = 10

# Background preload cache for node status
_node_status_cache: Dict = {}
_node_status_cache_time: float = 0
_NODE_STATUS_CACHE_MAX_AGE = 15  # seconds — serve cached data if fresh enough
_preload_thread: Optional[threading.Thread] = None
_preload_running = False
MIN_SUPPORTED_SAGE_VERSION = "0.12.9"

# Fingerprint selection state
_selected_fingerprint: Optional[str] = None
_start_triggered = threading.Event()  # set when user selects a fingerprint

# User launch preference — set before start_preload() is called.
# True  = bot may auto-launch Sage if not running (default for new installs).
# False = user will open Sage themselves; bot should not launch the exe.
_auto_launch_sage: bool = True

# Gate flag — set to True when the user has accepted the disclaimer and
# explicitly triggered wallet startup via POST /api/wallet/begin-startup.
# Prevents pre-disclaimer Sage RPC noise from health/fingerprint endpoints.
_startup_authorised: bool = False


def set_auto_launch(value: bool) -> None:
    """Set whether the preload loop may auto-launch the Sage exe."""
    global _auto_launch_sage
    _auto_launch_sage = value


def is_startup_authorised() -> bool:
    """Return True once the user has accepted the disclaimer and triggered begin-startup."""
    return _startup_authorised


def _get_live_sage_fingerprint() -> Optional[str]:
    """Return the currently logged-in Sage fingerprint, if any."""
    try:
        from wallet_sage import get_current_key
        key = get_current_key()
        if key and key.get("fingerprint") is not None:
            return str(key.get("fingerprint"))
    except Exception:
        pass
    return None


def _parse_sage_version(version: str) -> Optional[Tuple[int, int, int]]:
    """Parse a Sage semver-like version into a comparable tuple."""
    version_text = str(version or "").strip()
    if not version_text or version_text.lower() == "unknown":
        return None

    version_text = version_text.lstrip("vV")
    parts = version_text.split(".")
    parsed: List[int] = []
    for idx in range(3):
        token = parts[idx] if idx < len(parts) else "0"
        match = re.match(r"(\d+)", token)
        if not match:
            return None
        parsed.append(int(match.group(1)))
    return tuple(parsed)


def compare_sage_versions(version_a: str, version_b: str) -> int:
    """Compare two Sage version strings."""
    parsed_a = _parse_sage_version(version_a)
    parsed_b = _parse_sage_version(version_b)
    if parsed_a is None or parsed_b is None:
        return 0
    if parsed_a < parsed_b:
        return -1
    if parsed_a > parsed_b:
        return 1
    return 0


def _load_current_sage_version() -> str:
    """Read the current Sage version from the wallet RPC."""
    try:
        from wallet_sage import get_sage_version
        return get_sage_version() or "unknown"
    except Exception:
        return "unknown"


def get_sage_version_requirement() -> Dict:
    """Return the installed Sage version and whether it meets the minimum."""
    installed_version = _load_current_sage_version()
    requirement = {
        "installed_version": installed_version if installed_version else "unknown",
        "minimum_required_version": MIN_SUPPORTED_SAGE_VERSION,
        "supported": False,
        "reason": "",
    }

    if compare_sage_versions(requirement["installed_version"], MIN_SUPPORTED_SAGE_VERSION) >= 0:
        requirement["supported"] = True
        return requirement

    if requirement["installed_version"] == "unknown":
        requirement["reason"] = (
            f"Could not verify Sage version. This bot requires Sage v{MIN_SUPPORTED_SAGE_VERSION} "
            "or later because that build includes the coin selection fixes."
        )
    else:
        requirement["reason"] = (
            f"Sage v{requirement['installed_version']} is too old. This bot requires "
            f"Sage v{MIN_SUPPORTED_SAGE_VERSION} or later because earlier versions do not "
            "include the coin selection fixes."
        )
    return requirement


# ---------------------------------------------------------------------------
# Fingerprint Management
# ---------------------------------------------------------------------------

def get_available_fingerprints() -> List[Dict]:
    """Get all available wallet fingerprints.

    Chia: parses `chia keys show` CLI output.
    Sage: calls get_sage_keys() RPC — returns real wallet names.

    Returns:
        List of dicts: [{"fingerprint": "1234567890", "index": 1, "label": "..."}, ...]
    """
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()

    if wallet_type == "sage":
        return _get_sage_fingerprints()

    # --- Chia path: parse CLI output ---
    fingerprints = []
    try:
        result = subprocess.run(
            ["chia", "keys", "show"],
            capture_output=True, text=True, timeout=15,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode == 0:
            index = 0
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.lower().startswith("fingerprint:"):
                    fp = line.split(":", 1)[1].strip()
                    if fp.isdigit():
                        index += 1
                        fingerprints.append({
                            "fingerprint": fp,
                            "index": index,
                            "label": f"Wallet {index}",
                        })
            print(f"[Chia] Found {len(fingerprints)} fingerprints", flush=True)
        else:
            print(f"[Chia] chia keys show failed: {result.stderr[:200]}", flush=True)
    except FileNotFoundError:
        print("[Chia] chia CLI not found — check PATH", flush=True)
    except subprocess.TimeoutExpired:
        print("[Chia] chia keys show timed out", flush=True)
    except Exception as e:
        print(f"[Chia] Error listing fingerprints: {e}", flush=True)

    return fingerprints


def _get_sage_fingerprints() -> List[Dict]:
    """Get fingerprints from Sage RPC — returns real wallet names."""
    try:
        from wallet_sage import get_sage_keys
        keys = get_sage_keys()
        fingerprints = []
        for i, key in enumerate(keys):
            name = key.get("name", f"Wallet {i + 1}")
            fp = key.get("fingerprint", 0)
            fingerprints.append({
                "fingerprint": str(fp),
                "index": i + 1,
                "label": name,
            })
        print(f"[Sage] Found {len(fingerprints)} fingerprints", flush=True)
        return fingerprints
    except Exception as e:
        print(f"[Sage] Error listing fingerprints: {e}", flush=True)
        return []


def trigger_start(fingerprint: str) -> Dict:
    """Called when user selects a fingerprint in the GUI startup modal.

    Chia: starts daemon + logs in via RPC.
    Sage: calls resync + login via RPC (no daemon to start).
    """
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    tag = "[Sage]" if wallet_type == "sage" else "[Chia]"

    if wallet_type == "sage":
        version_gate = get_sage_version_requirement()
        if not version_gate.get("supported"):
            error = version_gate.get("reason") or (
                f"Sage v{version_gate.get('minimum_required_version', MIN_SUPPORTED_SAGE_VERSION)} or later is required."
            )
            log_event("warning", "sage_startup", error)
            print(f"{tag} {error}", flush=True)
            return {
                "success": False,
                "error": error,
                "unsupported_version": True,
                "sage_version": version_gate.get("installed_version", "unknown"),
                "sage_min_required_version": version_gate.get(
                    "minimum_required_version", MIN_SUPPORTED_SAGE_VERSION
                ),
            }

    global _selected_fingerprint
    _selected_fingerprint = fingerprint
    _start_triggered.set()

    log_event("info", f"{wallet_type}_startup",
              f"Fingerprint {fingerprint} selected — starting...")
    print(f"{tag} Fingerprint {fingerprint} selected — starting immediately",
          flush=True)

    def _do_start():
        global _sage_startup_phase
        if wallet_type == "sage":
            # Skip if preload thread already completed login.
            # _log_in_fingerprint also checks inside its lock, so even if this
            # check passes, the lock-guarded check prevents a true double login.
            if _sage_startup_phase == "ready":
                active_fp = _get_live_sage_fingerprint()
                if active_fp == str(fingerprint):
                    print(f"{tag} Already logged in to fingerprint {fingerprint} — skipping duplicate login", flush=True)
                    return
                print(f"{tag} Switching Sage session from fingerprint {active_fp or 'unknown'} to {fingerprint}", flush=True)
            with _phase_lock:
                _sage_startup_phase = "starting"
            print(f"{tag} Logging in to fingerprint {fingerprint}...", flush=True)
            success = _log_in_fingerprint(fingerprint)
            if success:
                # _log_in_fingerprint sets "ready" inside its lock on success.
                # Re-enable RPC error logging
                try:
                    from wallet_sage import set_quiet_mode
                    set_quiet_mode(False)
                except Exception:
                    pass
                # Update node status cache so dashboard works
                try:
                    result = get_node_status(bypass_cache=True)
                    global _node_status_cache, _node_status_cache_time
                    with _cache_lock:
                        _node_status_cache = result
                        _node_status_cache_time = time.time()
                except Exception:
                    pass
                # Don't log "fully started" here — _log_in_fingerprint already
                # logged the login success, and the preload thread logs "fully started".
                # Logging it again here was causing the duplicate message.
                print(f"{tag} Startup complete", flush=True)
            else:
                with _phase_lock:
                    _sage_startup_phase = "error"
            return
        else:
            # Chia: start daemon if needed, then login
            with _cache_lock:
                cached = _node_status_cache
            already_healthy = (cached and cached.get("status") == "healthy")

            if already_healthy:
                print("[Chia] Already healthy — skipping 'chia start all'", flush=True)
                log_event("info", "chia_startup",
                          "Chia already running and healthy — skipping start command")
            else:
                print("[Chia] Running 'chia start all'...", flush=True)
                result = start_chia("all")
                if result.get("success"):
                    log_event("info", "chia_startup",
                              "Chia start command sent — waiting for services...")
                    print(f"[Chia] Start OK: {result.get('output', '')[:120]}", flush=True)
                else:
                    err = result.get("error", "unknown")
                    log_event("warning", "chia_startup", f"Start may have failed: {err}")
                    print(f"[Chia] Start failed: {err}", flush=True)

            log_event("info", "chia_startup",
                      f"Waiting for wallet to respond, then logging in "
                      f"fingerprint {fingerprint}...")
            _log_in_fingerprint(fingerprint)

    threading.Thread(target=_do_start, daemon=True, name="wallet-quick-start").start()

    return {"success": True, "message": f"Starting wallet with fingerprint {fingerprint}"}


def _log_in_fingerprint(fingerprint: str) -> bool:
    """Log in to a specific fingerprint via wallet RPC.

    Chia: calls log_in RPC (waits up to 60s for wallet to respond).
    Sage: calls sage_login() which does resync + login + verify.

    Thread-safe: uses _login_lock so only one thread can login at a time.
    For Sage, if _sage_startup_phase is already "ready", returns True
    immediately (another thread already completed login).
    """
    global _sage_startup_phase
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()

    if wallet_type == "sage":
        # Acquire lock — only one thread can login at a time
        with _login_lock:
            # Double-check after acquiring lock — another thread may have
            # completed login while we were waiting for the lock
            if _sage_startup_phase == "ready":
                active_fp = _get_live_sage_fingerprint()
                if active_fp == str(fingerprint):
                    print(f"[Sage] Login already complete for fingerprint {fingerprint} (skipping duplicate)", flush=True)
                    return True
                print(f"[Sage] Active fingerprint is {active_fp or 'unknown'} — re-login required for {fingerprint}", flush=True)
                with _phase_lock:
                    _sage_startup_phase = "starting"

            # Sage: use the resync → login → verify sequence
            try:
                from wallet_sage import sage_login
                success = sage_login(int(fingerprint))
                if success:
                    with _phase_lock:
                        _sage_startup_phase = "ready"  # Set inside lock before anyone else can check
                    log_event("success", "sage_startup",
                              f"Logged in to fingerprint {fingerprint}")
                    print(f"[Sage] Logged in to fingerprint {fingerprint}", flush=True)
                else:
                    log_event("warning", "sage_startup",
                              f"Could not log in to fingerprint {fingerprint}")
                    print(f"[Sage] Login failed for fingerprint {fingerprint}", flush=True)
                return success
            except Exception as e:
                log_event("warning", "sage_startup",
                          f"Sage login error: {e}")
                print(f"[Sage] Login error: {e}", flush=True)
                return False

    # --- Chia path: poll log_in RPC ---
    from wallet import rpc

    for attempt in range(12):
        try:
            result = rpc("log_in", {"fingerprint": int(fingerprint)}, timeout=5)
            if result and result.get("success"):
                log_event("success", "chia_startup",
                          f"Logged in to fingerprint {fingerprint}")
                print(f"[Chia] Logged in to fingerprint {fingerprint}", flush=True)
                return True
            elif result:
                print(f"[Chia] log_in attempt {attempt+1}: {result}", flush=True)
        except Exception as e:
            print(f"[Chia] log_in attempt {attempt+1} failed: {e}", flush=True)
        time.sleep(5)

    log_event("warning", "chia_startup",
              f"Could not log in to fingerprint {fingerprint} after 60s")
    return False


def _resolve_startup_fingerprint() -> Optional[str]:
    """Determine whether startup may use a fingerprint without prompting.

    Sage behaviour:
    1. Saved SAGE_FINGERPRINT/WALLET_FINGERPRINT values are ignored on fresh startup.
    2. None makes the GUI startup modal show the fingerprint picker.

    The user should always choose which wallet to use via the GUI picker on
    startup; a saved setting is not consent for a new process.
    """
    from dotenv import load_dotenv
    load_dotenv()

    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    tag = "[Sage]" if wallet_type == "sage" else "[Chia]"

    if wallet_type == "sage":
        saved = [
            env_key
            for env_key in ("SAGE_FINGERPRINT", "WALLET_FINGERPRINT")
            if os.getenv(env_key, "").strip()
        ]
        if saved:
            print(
                f"{tag} Saved fingerprint found ({', '.join(saved)}) - waiting for GUI selection",
                flush=True,
            )
        else:
            print(f"{tag} No fingerprint in .env - waiting for GUI selection",
                  flush=True)
        return None

    # Optional .env override for legacy Chia startup.
    for env_key in ("SAGE_FINGERPRINT", "WALLET_FINGERPRINT"):
        fp = os.getenv(env_key, "").strip()
        if fp and fp.isdigit():
            print(f"{tag} Using {env_key} from .env: {fp}", flush=True)
            return fp

    print(f"{tag} No fingerprint in .env - waiting for GUI selection",
          flush=True)
    return None


def start_preload():
    """Start background thread that auto-starts Chia and monitors node status.

    Called by api_server on startup. This thread:
    1. Checks if Chia is already running → if yes, proceed to monitoring
    2. If not running → auto-starts Chia using .env fingerprint or first available key
    3. If no fingerprint found → waits for user to select in the dashboard
    4. Polls until healthy, caching results for instant dashboard loads

    The goal is that by the time the user clicks "Chia Dashboard", everything
    is already connected and data loads instantly.

    All progress is logged to the bot console via log_event().
    """
    global _preload_thread, _preload_running, _startup_authorised

    _startup_authorised = True  # user has accepted disclaimer and triggered startup

    with _preload_start_lock:
        if _preload_running:
            return  # already running
        _preload_running = True

    # Set Sage phase immediately so GUI knows we're connecting
    # (before the thread's initial sleep)
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    if wallet_type == "sage":
        global _sage_startup_phase
        with _phase_lock:
            _sage_startup_phase = "connecting"

    def _preload_loop():
        global _node_status_cache, _node_status_cache_time, _preload_running
        global _selected_fingerprint

        _chia_started = False
        _login_done = False
        _startup_logged = False
        _healthy_logged = False

        # Small delay so the GUI/console has time to connect SSE
        time.sleep(3)

        # Sage light wallet: full startup state machine
        wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
        if wallet_type == "sage":
            global _sage_startup_phase
            with _phase_lock:
                _sage_startup_phase = "connecting"
            log_event("info", "sage_startup", "Starting Sage wallet startup sequence...")
            print("[Sage] Starting startup sequence...", flush=True)

            # Suppress noisy RPC errors from other threads while starting
            try:
                from wallet_sage import set_quiet_mode
                set_quiet_mode(True)
            except Exception:
                pass

            # --- Step 1: Quick check if Sage is already running ---
            with _phase_lock:
                _sage_startup_phase = "connecting"
            sage_running = False
            for attempt in range(2):  # 2 quick checks (6 seconds)
                if _is_sage_rpc_available():
                    sage_running = True
                    print("[Sage] RPC is responding", flush=True)
                    break
                if _is_sage_rpc_port_listening():
                    sage_running = True
                    print("[Sage] RPC port is listening - checking certificates", flush=True)
                    break
                if attempt == 0:
                    print("[Sage] Checking if Sage is running...", flush=True)
                time.sleep(3)

            # --- Step 1b: Sage process running but RPC disabled ---
            # Distinguish "Sage not open" from "Sage open, RPC off" so we can
            # show actionable instructions rather than a generic wait spinner.
            if not sage_running and _is_sage_process_running():
                with _phase_lock:
                    _sage_startup_phase = "rpc_disabled"
                log_event("warning", "sage_startup",
                          "Sage is running but RPC is not enabled — "
                          "user needs to enable it in Sage Settings → Advanced")
                print("[Sage] Sage open but RPC disabled — waiting for user to enable it",
                      flush=True)
                while _preload_running and not sage_running:
                    if _is_sage_rpc_available():
                        sage_running = True
                        with _phase_lock:
                            _sage_startup_phase = "connecting"
                        log_event("success", "sage_startup",
                                  "Sage RPC became available — resuming startup")
                        print("[Sage] RPC enabled — resuming", flush=True)
                        break
                    if _is_sage_rpc_port_listening():
                        sage_running = True
                        with _phase_lock:
                            _sage_startup_phase = "connecting"
                        log_event("info", "sage_startup",
                                  "Sage RPC port became available - checking certificate access")
                        print("[Sage] RPC port enabled - checking certificates", flush=True)
                        break
                    time.sleep(5)
                if not sage_running or not _preload_running:
                    if _preload_running:
                        with _phase_lock:
                            _sage_startup_phase = "error"
                    return
                # Sage is now running with RPC — skip the launch block below

            # --- Step 2: Launch exe if not running ---
            if not sage_running:
                with _phase_lock:
                    _sage_startup_phase = "launching"
                exe_path = os.getenv("SAGE_EXE_PATH", "").strip()
                if not exe_path:
                    exe_path = _detect_sage_exe_path()

                # Only auto-launch if the user opted in via the GUI prompt.
                # If _auto_launch_sage is False the user said they'll open Sage
                # themselves — fall through to the manual-start polling loop.
                if exe_path and not _auto_launch_sage:
                    log_event("info", "sage_startup",
                              "User chose to open Sage manually — waiting for RPC")
                    print("[Sage] User will open Sage manually — waiting...", flush=True)
                    exe_path = None  # skip launch, fall to manual-wait below

                if exe_path:
                    log_event("info", "sage_startup",
                              f"Launching Sage: {os.path.basename(exe_path)}")
                    print(f"[Sage] Launching {exe_path}...", flush=True)

                    if _launch_sage_exe(exe_path):
                        with _phase_lock:
                            _sage_startup_phase = "connecting"  # Sage launched — waiting for RPC
                        # Wait for RPC to become available after launch.
                        # Give Sage ~10s to start the RPC server before deciding
                        # it's disabled. After that, if the process is running but
                        # RPC still isn't responding, show the user instructions
                        # instead of silently waiting for 3 minutes then failing.
                        print("[Sage] Waiting for RPC to come online...", flush=True)
                        _RPC_GRACE_ATTEMPTS = 2   # 10s — enough for Sage to start
                        for attempt in range(36):  # 3 minutes total
                            if _is_sage_rpc_available():
                                sage_running = True
                                print("[Sage] RPC came online after launch", flush=True)
                                log_event("success", "sage_startup",
                                          "Sage launched and RPC available")
                                break
                            if _is_sage_rpc_port_listening():
                                sage_running = True
                                print("[Sage] RPC port came online after launch - checking certificates",
                                      flush=True)
                                log_event("info", "sage_startup",
                                          "Sage RPC port is listening - checking certificate access")
                                break
                            if attempt >= _RPC_GRACE_ATTEMPTS and _is_sage_process_running():
                                # Sage is open but RPC never came up — likely disabled
                                with _phase_lock:
                                    _sage_startup_phase = "rpc_disabled"
                                log_event("warning", "sage_startup",
                                          "Sage launched but RPC did not start — "
                                          "user needs to enable RPC in Settings → Advanced")
                                print("[Sage] Sage open but RPC disabled — "
                                      "showing instructions", flush=True)
                                while _preload_running and not sage_running:
                                    if _is_sage_rpc_available():
                                        sage_running = True
                                        with _phase_lock:
                                            _sage_startup_phase = "connecting"
                                        log_event("success", "sage_startup",
                                                  "Sage RPC became available — resuming")
                                        print("[Sage] RPC enabled — resuming", flush=True)
                                        break
                                    if _is_sage_rpc_port_listening():
                                        sage_running = True
                                        with _phase_lock:
                                            _sage_startup_phase = "connecting"
                                        log_event("info", "sage_startup",
                                                  "Sage RPC port became available - checking certificate access")
                                        print("[Sage] RPC port enabled - checking certificates", flush=True)
                                        break
                                    time.sleep(5)
                                break  # exit the 36-attempt loop either way
                            time.sleep(5)

                        if not sage_running:
                            with _phase_lock:
                                _sage_startup_phase = "error"
                            log_event("error", "sage_startup",
                                      "Sage RPC did not respond after launching exe")
                            print("[Sage] RPC did not come online after 3 min",
                                  flush=True)
                else:
                    log_event("warning", "sage_startup",
                              "Cannot find sage-tauri.exe — please start Sage manually "
                              "and return to the CATalyst window")
                    print("[Sage] sage-tauri.exe not found — waiting for manual start",
                          flush=True)

                    # Wait for user to start Sage manually
                    while _preload_running and not sage_running:
                        if _is_sage_rpc_available():
                            sage_running = True
                            log_event("success", "sage_startup",
                                      "Sage detected — RPC is now available")
                            print("[Sage] RPC detected!", flush=True)
                            break
                        if _is_sage_rpc_port_listening():
                            sage_running = True
                            log_event("info", "sage_startup",
                                      "Sage detected - RPC port is listening, checking certificates")
                            print("[Sage] RPC port detected!", flush=True)
                            break
                        time.sleep(5)

            if not sage_running or not _preload_running:
                if _preload_running:
                    with _phase_lock:
                        _sage_startup_phase = "error"
                return

            # --- Step 3: Check certificate configuration ---
            cert_path = os.getenv("SAGE_CERT_PATH", "").strip()
            key_path = os.getenv("SAGE_KEY_PATH", "").strip()
            if cert_path:
                ok, reason, cert_real, key_real = validate_sage_cert_pair(cert_path, key_path)
                if ok:
                    cert_path = cert_real
                    key_path = key_real
                    _set_sage_cert_env_and_reload(cert_real, key_real)
                else:
                    log_event("warning", "sage_startup",
                              f"Configured Sage certificates are not usable: {reason}")
                    cert_path = ""
            if not cert_path:
                detected = detect_sage_cert_path()
                if detected:
                    cert_path = detected
                    key_path = os.path.join(os.path.dirname(detected), "wallet.key")
                    _set_sage_cert_env_and_reload(detected, key_path)
                    # Auto-detected — note for user but don't need to ask
                    log_event("info", "sage_startup",
                              f"Auto-detected cert: {cert_path}")

            if not cert_path:
                with _phase_lock:
                    _sage_startup_phase = "waiting_certs"
                log_event("warning", "sage_startup",
                          "Sage certificates not configured — "
                          "waiting for setup in the CATalyst window")
                print("[Sage] Certs not configured — waiting for setup", flush=True)
                # Wait for the user to configure certs through the frontend.
                while _preload_running:
                    from dotenv import load_dotenv
                    try:
                        from user_paths import env_file as _env_file
                        load_dotenv(_env_file(), override=True)
                    except Exception:
                        load_dotenv(override=True)
                    cert_path = os.getenv("SAGE_CERT_PATH", "").strip()
                    key_path = os.getenv("SAGE_KEY_PATH", "").strip()
                    ok, reason, cert_real, key_real = validate_sage_cert_pair(cert_path, key_path)
                    if ok:
                        _set_sage_cert_env_and_reload(cert_real, key_real)
                        log_event("success", "sage_startup",
                                  f"Certificate configured: {cert_real}")
                        break
                    if cert_path:
                        log_event("warning", "sage_startup",
                                  f"Waiting for usable Sage certificate: {reason}")
                    time.sleep(5)

            if not _is_sage_rpc_available():
                with _phase_lock:
                    _sage_startup_phase = "waiting_certs"
                log_event("warning", "sage_startup",
                          "Sage RPC port is listening but CATalyst could not authenticate - "
                          "checking Sage certificate paths")
                print("[Sage] RPC port is listening but authenticated RPC failed - checking certs",
                      flush=True)
                while _preload_running:
                    detected = detect_sage_cert_path()
                    if detected:
                        key_path = os.path.join(os.path.dirname(detected), "wallet.key")
                        _set_sage_cert_env_and_reload(detected, key_path)
                    if _is_sage_rpc_available():
                        with _phase_lock:
                            _sage_startup_phase = "connecting"
                        log_event("success", "sage_startup",
                                  "Sage RPC authenticated after refreshing certificate paths")
                        print("[Sage] Authenticated RPC available after cert refresh", flush=True)
                        break
                    time.sleep(5)
                if not _preload_running:
                    return

            version_gate = get_sage_version_requirement()
            if not version_gate.get("supported"):
                with _phase_lock:
                    _sage_startup_phase = "version_blocked"
                reason = version_gate.get("reason") or (
                    f"Sage v{MIN_SUPPORTED_SAGE_VERSION} or later is required."
                )
                log_event("warning", "sage_startup", reason)
                print(f"[Sage] {reason}", flush=True)
                while _preload_running:
                    time.sleep(5)
                    version_gate = get_sage_version_requirement()
                    if version_gate.get("supported"):
                        log_event(
                            "success",
                            "sage_startup",
                            f"Sage v{version_gate['installed_version']} meets the minimum supported version "
                            f"{version_gate['minimum_required_version']} - resuming startup",
                        )
                        print(
                            f"[Sage] Sage v{version_gate['installed_version']} meets the minimum supported version - resuming startup",
                            flush=True,
                        )
                        break
                if not _preload_running:
                    return

            # --- Step 4: Resolve fingerprint ---
            fp = _resolve_startup_fingerprint()

            if fp:
                # Auto-login with fingerprint from .env
                with _phase_lock:
                    _sage_startup_phase = "starting"
                _selected_fingerprint = fp
                log_event("info", "sage_startup",
                          f"Auto-logging in to fingerprint {fp}...")
                success = _log_in_fingerprint(fp)
                if success:
                    with _phase_lock:
                        _sage_startup_phase = "ready"
                else:
                    log_event("warning", "sage_startup",
                              f"Auto-login failed for fingerprint {fp}")
                    with _phase_lock:
                        _sage_startup_phase = "waiting_fingerprint"
                    fp = None  # Fall through to picker

            login_handled_by_trigger = False

            if not fp:
                # Wait for user to select fingerprint in GUI
                with _phase_lock:
                    _sage_startup_phase = "waiting_fingerprint"
                log_event("info", "sage_startup",
                          "Waiting for wallet selection in GUI...")
                print("[Sage] No fingerprint configured — waiting for GUI selection",
                      flush=True)

                _start_triggered.wait(timeout=600)  # 10 min

                # trigger_start() already handles login and sets
                # _sage_startup_phase = "ready". Just wait for it.
                login_handled_by_trigger = False
                if _selected_fingerprint and _sage_startup_phase != "ready":
                    # Only login here if trigger_start didn't already do it
                    with _phase_lock:
                        _sage_startup_phase = "starting"
                    log_event("info", "sage_startup",
                              f"Logging in to selected fingerprint "
                              f"{_selected_fingerprint}...")
                    success = _log_in_fingerprint(_selected_fingerprint)
                    if success:
                        with _phase_lock:
                            _sage_startup_phase = "ready"
                    else:
                        with _phase_lock:
                            _sage_startup_phase = "error"
                        log_event("error", "sage_startup", "Login failed")
                        return
                elif _sage_startup_phase == "ready":
                    # trigger_start() already logged success + updated cache
                    login_handled_by_trigger = True
                    print("[Sage] Login already handled by trigger_start",
                          flush=True)

            # --- Step 5: Update cache and monitor ---
            # Only log success + update cache if THIS thread did the login
            # (trigger_start's _do_start already does this, so skip to avoid duplicates)
            if _sage_startup_phase == "ready" and not login_handled_by_trigger:
                log_event("success", "sage_startup",
                          "Sage wallet fully started and logged in")
                print("[Sage] Startup complete — monitoring...", flush=True)

                # Re-enable RPC error logging now that Sage is connected
                try:
                    from wallet_sage import set_quiet_mode
                    set_quiet_mode(False)
                except Exception:
                    pass

                # Update node status cache so dashboard works
                try:
                    result = get_node_status(bypass_cache=True)
                    with _cache_lock:
                        _node_status_cache = result
                        _node_status_cache_time = time.time()
                except Exception:
                    pass

            # Keep monitoring (like Chia's healthy loop)
            while _preload_running:
                try:
                    result = get_node_status(bypass_cache=True)
                    with _cache_lock:
                        _node_status_cache = result
                        _node_status_cache_time = time.time()
                except Exception as e:
                    print(f"[Sage] Monitor error: {e}", flush=True)
                time.sleep(15)

            return  # Exit Sage preload loop

        log_event("info", "chia_startup", "Checking Sage wallet status...")
        print("[Sage] Checking if Sage wallet is running...", flush=True)

        # Track whether we've done the initial check
        _initial_check_done = False
        _fp_resolve_done = False  # Only try resolving fingerprint once

        while _preload_running:
            try:
                # Skip expensive RPC calls if we're just waiting for
                # the user to pick a fingerprint (no point hammering
                # a wallet that isn't running yet)
                if _initial_check_done and not _selected_fingerprint and not _start_triggered.is_set():
                    # Still waiting for user — don't waste time on RPCs
                    status = "unreachable"
                    wallet_ok = False
                    node_ok = False
                else:
                    result = get_node_status(bypass_cache=True)
                    with _cache_lock:
                        _node_status_cache = result
                        _node_status_cache_time = time.time()
                    _initial_check_done = True

                    status = result.get("status", "unknown")
                    wallet_ok = result.get("wallet_reachable", False)
                    node_ok = result.get("node_reachable", False)

                if status == "healthy":
                    if not _healthy_logged:
                        peers = result.get("peer_count", 0)
                        height = result.get("peak_height", 0)
                        log_event("success", "chia_startup",
                                  f"Sage wallet healthy — height #{height:,}, "
                                  f"{peers} peers connected")
                        print(f"[Chia] Node healthy — height #{height:,}, {peers} peers",
                              flush=True)
                        _healthy_logged = True
                    time.sleep(15)

                elif status == "syncing":
                    progress = result.get("sync_progress_height", 0)
                    tip = result.get("sync_tip_height", 0)
                    if not _startup_logged:
                        log_event("info", "chia_startup",
                                  f"Sage wallet syncing... ({progress:,} / {tip:,})")
                        _startup_logged = True
                    time.sleep(10)

                elif status == "unreachable" and not _chia_started:
                    # Chia not running — check if we have a fingerprint

                    if not _selected_fingerprint and not _fp_resolve_done:
                        # Check .env for auto-start fingerprint (once only)
                        _selected_fingerprint = _resolve_startup_fingerprint()
                        _fp_resolve_done = True

                    if _selected_fingerprint and not _start_triggered.is_set():
                        # Have a fingerprint from .env — auto-start Chia
                        log_event("info", "chia_startup",
                                  f"Starting all Chia services (fingerprint: "
                                  f"{_selected_fingerprint})...")
                        print(f"[Chia] Auto-starting with fingerprint "
                              f"{_selected_fingerprint}...", flush=True)
                        start_result = start_chia("all")
                        _chia_started = True
                        if start_result.get("success"):
                            log_event("info", "chia_startup",
                                      "Chia start command sent — waiting for "
                                      "node to come online...")
                            # Log in once wallet is up
                            _log_in_fingerprint(_selected_fingerprint)
                        time.sleep(5)

                    elif _start_triggered.is_set():
                        # trigger_start() already launched Chia in its own
                        # thread — just mark as started and wait for it
                        _chia_started = True
                        print("[Chia] Startup triggered by GUI — monitoring...",
                              flush=True)
                        time.sleep(5)

                    else:
                        # No fingerprint — wait for user to select in the
                        # GUI startup modal
                        if not _startup_logged:
                            log_event("info", "chia_startup",
                                      "Waiting for wallet selection...")
                            print("[Chia] No fingerprint — waiting for GUI "
                                  "selection...", flush=True)
                            _startup_logged = True
                        # Wait for trigger (short timeout so we stay responsive)
                        _start_triggered.wait(timeout=5)
                        if _start_triggered.is_set():
                            continue  # Re-check immediately
                        # Still waiting — skip expensive get_node_status next loop
                        continue

                elif _chia_started and not _login_done and wallet_ok and _selected_fingerprint:
                    # Wallet is up — log in (unless trigger_start's thread
                    # is already handling this via _start_triggered)
                    if _start_triggered.is_set():
                        # trigger_start() handles login — just mark done
                        _login_done = True
                        print("[Chia] Login handled by startup thread", flush=True)
                    else:
                        log_event("info", "chia_startup",
                                  f"Wallet responding — logging in to fingerprint "
                                  f"{_selected_fingerprint}...")
                        success = _log_in_fingerprint(_selected_fingerprint)
                        _login_done = True
                        if not success:
                            log_event("warning", "chia_startup",
                                      "Login failed — wallet may use default fingerprint")
                    time.sleep(3)

                else:
                    # Partially up or waiting
                    if not _startup_logged:
                        parts = []
                        if wallet_ok:
                            parts.append("wallet OK")
                        if node_ok:
                            parts.append("node OK")
                        if not parts:
                            parts.append("waiting for services")
                        log_event("info", "chia_startup",
                                  f"Chia starting up — {', '.join(parts)}...")
                        _startup_logged = True
                    time.sleep(5)

            except Exception as e:
                import traceback
                print(f"[Chia] Preload error: {e}", flush=True)
                traceback.print_exc()
                time.sleep(5)

    _preload_thread = threading.Thread(target=_preload_loop, daemon=True,
                                       name="chia-startup")
    _preload_thread.start()


def stop_preload():
    """Stop the background preload thread."""
    global _preload_running
    _preload_running = False


def get_startup_status() -> Dict:
    """Get the current wallet startup state for the GUI to display.

    Returns a simple dict the main GUI can poll to show startup progress.
    Works for both wallet types — adds wallet_type so the GUI can adapt.
    """
    global _selected_fingerprint, _sage_startup_phase
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    with _cache_lock:
        cached = _node_status_cache
    status = cached.get("status", "unknown") if cached else "checking"
    wallet_label = "Sage wallet" if wallet_type == "sage" else "Chia wallet"

    # Adoption check (RECOVERY-ONLY): if a page reload mid-session lost the
    # cached _selected_fingerprint while the bot was already running,
    # re-adopt the live Sage fingerprint so the GUI doesn't strand the
    # operator on the picker while the loop is happily running.
    #
    # The right gate is `_sage_startup_phase == "ready"`:
    #  - Fresh launch (post-disclaimer) → phase is "waiting_fingerprint"
    #    even though _preload_running is True. We want the picker.
    #  - Mid-session reload → phase is "ready" because trigger_start
    #    completed login before the page reload. Cache is empty because
    #    the Python process restarted but the live Sage state survived.
    #    Adopt so the GUI recovers without re-prompting.
    #
    # _preload_running was the wrong signal: it flips True the moment
    # the user accepts the disclaimer, BEFORE they've had a chance to
    # pick — making cold-start always silently adopt, defeating the
    # explicit checkpoint the user wants.
    _is_mid_session_recovery = (_sage_startup_phase == "ready")
    if (wallet_type == "sage" and not _selected_fingerprint
            and _is_mid_session_recovery):
        try:
            live_fp = _get_live_sage_fingerprint()
            if live_fp:
                _selected_fingerprint = live_fp
                try:
                    _start_triggered.set()
                except Exception:
                    pass
                if _sage_startup_phase == "waiting_fingerprint":
                    with _phase_lock:
                        _sage_startup_phase = "ready"
                if not _startup_authorised or not _preload_running:
                    try:
                        start_preload()
                    except Exception:
                        pass
        except Exception:
            pass

    # For Sage: use _sage_startup_phase directly — it tracks the exact state
    # of the multi-step startup (connecting → launching → waiting_certs →
    # waiting_fingerprint → starting → ready).  This prevents the GUI from
    # jumping to fingerprint selection while the exe is still launching.
    if wallet_type == "sage" and _sage_startup_phase:
        sp = _sage_startup_phase
        if sp in ("connecting", ""):
            phase = "starting"
            message = "Connecting to Sage wallet..."
        elif sp == "launching":
            phase = "launching"
            message = "Launching Sage wallet application..."
        elif sp == "rpc_disabled":
            phase = "rpc_disabled"
            message = "Sage is open but RPC is not enabled"
        elif sp == "waiting_certs":
            phase = "waiting_certs"
            message = "Sage needs certificate configuration"
        elif sp == "waiting_fingerprint":
            phase = "waiting_fingerprint"
            message = "Sage connected — select a wallet"
        elif sp == "version_blocked":
            phase = "version_blocked"
            message = f"Sage v{MIN_SUPPORTED_SAGE_VERSION} or later is required"
        elif sp == "starting":
            phase = "starting"
            message = "Logging in to Sage wallet..."
        elif sp == "ready":
            phase = "ready"
            message = "Sage wallet is healthy"
            try:
                from wallet import get_wallet_sync_status, get_wallets

                wallet_sync = get_wallet_sync_status() or {}
                sync_state = wallet_sync.get("sync_state", "unknown")

                if not wallet_sync.get("reachable"):
                    phase = "starting"
                    message = "Connecting to Sage wallet..."
                elif sync_state == "not_synced" or wallet_sync.get("syncing"):
                    phase = "syncing"
                    message = "Sage wallet is syncing..."
                elif sync_state == "unknown":
                    wallets_result = get_wallets() or {}
                    if not wallets_result.get("success") or wallets_result.get("wallets") is None:
                        phase = "starting"
                        message = "Sage connected - loading wallet data..."
                    else:
                        message = "Sage wallet is connected"
                else:
                    message = "Sage wallet is healthy"
            except Exception:
                pass
        elif sp == "error":
            phase = "starting"
            message = "Sage startup encountered an error — retrying..."
        else:
            phase = "starting"
            message = f"Sage startup: {sp}"
    elif not _preload_running:
        phase = "idle"
        message = "Startup thread not running"
    elif status == "healthy":
        phase = "ready"
        message = f"{wallet_label} is healthy"
    elif status in ("syncing", "node_not_synced"):
        phase = "syncing"
        message = f"{wallet_label} is syncing..."
    elif status == "unreachable" and not _selected_fingerprint:
        phase = "waiting_fingerprint"
        message = "Waiting for wallet selection..."
    elif status == "unreachable":
        phase = "starting"
        message = f"Starting {wallet_label} services..."
    elif cached and cached.get("wallet_reachable"):
        if not _selected_fingerprint:
            phase = "waiting_fingerprint"
            message = f"{wallet_label} connected — select a wallet"
        else:
            phase = "starting"
            if wallet_type == "sage":
                message = "Wallet connected — checking Sage status..."
            else:
                message = "Wallet connected — waiting for full node..."
    elif not _selected_fingerprint:
        phase = "waiting_fingerprint"
        message = "Waiting for wallet selection..."
    else:
        phase = "starting"
        message = f"Connecting to {wallet_label}..."

    result = {
        "phase": phase,
        "message": message,
        "fingerprint": _selected_fingerprint or "",
        "node_status": status,
        "preload_running": _preload_running,
        "wallet_type": wallet_type,
    }

    # Include sync progress for GUI progress bar
    if cached and status in ("syncing", "node_not_synced"):
        result["sync_progress"] = cached.get("sync_progress_height", 0)
        result["sync_tip"] = cached.get("sync_tip_height", 0)

    # Include Sage version and minimum-version gate info for the startup UI
    if wallet_type == "sage" and phase not in ("idle", "waiting_certs"):
        try:
            version_gate = get_sage_version_requirement()
            result["sage_min_required_version"] = version_gate["minimum_required_version"]
            installed_version = version_gate.get("installed_version", "unknown")
            if installed_version != "unknown":
                result["sage_version"] = installed_version
            if phase == "version_blocked" or installed_version != "unknown":
                result["sage_version_supported"] = version_gate["supported"]
                if version_gate.get("reason"):
                    result["sage_version_requirement_message"] = version_gate["reason"]
                    if phase == "version_blocked":
                        result["message"] = version_gate["reason"]
        except Exception:
            result["sage_min_required_version"] = MIN_SUPPORTED_SAGE_VERSION

    return result


# Sage-specific startup phase (used by the preload loop, read by get_startup_status)
_sage_startup_phase = ""

# Lock to prevent two threads from calling _log_in_fingerprint simultaneously
_login_lock = threading.Lock()

# Lock to protect all writes to _sage_startup_phase
_phase_lock = threading.Lock()

# Lock to prevent two threads from racing through start_preload()'s check-and-set
_preload_start_lock = threading.Lock()

# Lock to protect reads and writes to _node_status_cache / _node_status_cache_time
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Sage Auto-Detection Helpers
# ---------------------------------------------------------------------------

def _detect_sage_exe_path() -> Optional[str]:
    """Auto-detect sage-tauri.exe on Windows.

    Searches common install locations. Returns path if found, None otherwise.
    """
    import platform

    if platform.system() != "Windows":
        return None

    search_paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\Sage\sage-tauri.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Sage\sage-tauri.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\sage\sage-tauri.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Sage\sage-tauri.exe"),
    ]

    for path in search_paths:
        if os.path.isfile(path):
            print(f"[Sage] Auto-detected exe: {path}", flush=True)
            return path

    print("[Sage] sage-tauri.exe not found in common paths", flush=True)
    return None


def _split_env_paths(raw: str) -> List[str]:
    """Split an env path list while tolerating comma-separated overrides."""
    if not raw:
        return []
    pieces: List[str] = []
    for chunk in raw.split(os.pathsep):
        for part in chunk.split(","):
            part = part.strip().strip('"').strip("'")
            if part:
                pieces.append(part)
    return pieces


def _normalise_path(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip())))


def _dedupe_paths(paths: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for path in paths:
        if not path:
            continue
        try:
            normalised = _normalise_path(path)
            key = os.path.normcase(os.path.realpath(normalised))
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(normalised)
    return result


def _candidate_sage_data_dirs(extra_dirs: Optional[List[str]] = None) -> List[str]:
    """Return known Sage data roots, including portable/custom overrides."""
    import platform

    roots: List[str] = []
    if extra_dirs:
        roots.extend(extra_dirs)

    roots.extend(_split_env_paths(os.getenv("SAGE_DATA_DIR", "")))
    roots.extend(_split_env_paths(os.getenv("SAGE_HOME", "")))
    roots.extend(_split_env_paths(os.getenv("SAGE_ALLOWED_CERT_ROOTS", "")))

    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        userprofile = os.environ.get("USERPROFILE")
        if appdata:
            roots.append(os.path.join(appdata, "com.rigidnetwork.sage"))
        if localappdata:
            roots.append(os.path.join(localappdata, "com.rigidnetwork.sage"))
            roots.append(os.path.join(localappdata, "Sage"))
            roots.append(os.path.join(localappdata, "sage"))
        if userprofile:
            roots.append(os.path.join(userprofile, "AppData", "Roaming", "com.rigidnetwork.sage"))
            roots.append(os.path.join(userprofile, "AppData", "Local", "com.rigidnetwork.sage"))
    elif system == "Darwin":
        roots.append(os.path.expanduser("~/Library/Application Support/com.rigidnetwork.sage"))
        roots.append(os.path.expanduser("~/Library/Application Support/Sage"))
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_config:
            roots.append(os.path.join(xdg_config, "com.rigidnetwork.sage"))
        if xdg_data:
            roots.append(os.path.join(xdg_data, "com.rigidnetwork.sage"))
        roots.append(os.path.expanduser("~/.config/com.rigidnetwork.sage"))
        roots.append(os.path.expanduser("~/.local/share/com.rigidnetwork.sage"))

    return _dedupe_paths(roots)


def _candidate_sage_ssl_dirs(extra_dirs: Optional[List[str]] = None) -> List[str]:
    ssl_dirs: List[str] = []
    for data_dir in _candidate_sage_data_dirs(extra_dirs):
        if os.path.basename(os.path.normpath(data_dir)).lower() == "ssl":
            ssl_dirs.append(data_dir)
        else:
            ssl_dirs.append(os.path.join(data_dir, "ssl"))
    return _dedupe_paths(ssl_dirs)


def validate_sage_cert_pair(cert_path: str, key_path: str = "") -> Tuple[bool, str, str, str]:
    """Validate and normalise a Sage wallet TLS cert/key pair.

    Security posture: accept flexible data roots, but only the Sage-shaped
    pair: an `ssl` directory containing `wallet.crt` and sibling `wallet.key`.
    The API never reads or returns private key contents.
    """
    if not cert_path or not cert_path.strip():
        return False, "Choose Sage's wallet.crt file.", "", ""

    try:
        cert_real = os.path.realpath(_normalise_path(cert_path))
    except Exception:
        return False, "Certificate path is invalid.", "", ""

    if os.path.basename(cert_real).lower() != "wallet.crt":
        return False, "Choose Sage's wallet.crt file, not another certificate.", cert_real, ""

    cert_dir = os.path.dirname(cert_real)
    if not key_path or not key_path.strip():
        key_path = os.path.join(cert_dir, "wallet.key")
    try:
        key_real = os.path.realpath(_normalise_path(key_path))
    except Exception:
        return False, "Key path is invalid.", cert_real, ""

    if os.path.basename(key_real).lower() != "wallet.key":
        return False, "Sage's key file must be named wallet.key.", cert_real, key_real
    if os.path.normcase(os.path.dirname(key_real)) != os.path.normcase(cert_dir):
        return False, "wallet.crt and wallet.key must be in the same Sage ssl folder.", cert_real, key_real
    if os.path.basename(os.path.dirname(cert_real)).lower() != "ssl":
        return False, "wallet.crt and wallet.key must be inside Sage's ssl folder.", cert_real, key_real
    # User-selected desktop path is restricted to ssl/wallet.crt plus sibling key.
    # codeql[py/path-injection]
    if not os.path.isfile(cert_real):
        return False, "Certificate file not found at the specified path.", cert_real, key_real
    # Key path is derived from or validated against the same Sage ssl folder.
    # codeql[py/path-injection]
    if not os.path.isfile(key_real):
        return False, "Key file not found next to wallet.crt.", cert_real, key_real

    return True, "", cert_real, key_real


def _set_sage_cert_env_and_reload(cert_path: str, key_path: str) -> bool:
    """Apply Sage cert paths to this process and refresh the wallet RPC client."""
    ok, reason, cert_real, key_real = validate_sage_cert_pair(cert_path, key_path)
    if not ok:
        log_event("warning", "sage_cert_path_rejected",
                  f"Rejected Sage certificate selection: {reason}")
        return False

    os.environ["SAGE_CERT_PATH"] = cert_real
    os.environ["SAGE_KEY_PATH"] = key_real
    os.environ["SAGE_DATA_DIR"] = os.path.dirname(os.path.dirname(cert_real))

    try:
        import wallet_sage
        wallet_sage.reload_connection_settings()
    except Exception as reload_err:
        print(f"[Sage] Warning: could not refresh wallet_sage config: {reload_err}")
    return True


def detect_sage_cert_path(extra_data_dirs: Optional[List[str]] = None) -> Optional[str]:
    """Auto-detect Sage TLS certificate location.

    Searches known platform data directories plus SAGE_DATA_DIR /
    SAGE_ALLOWED_CERT_ROOTS. Returns wallet.crt if its sibling wallet.key
    is present.
    """

    for d in _candidate_sage_ssl_dirs(extra_data_dirs):
        cert = os.path.join(d, "wallet.crt")
        key = os.path.join(d, "wallet.key")
        ok, _, cert_real, _ = validate_sage_cert_pair(cert, key)
        if ok:
            print(f"[Sage] Auto-detected certs: {d}", flush=True)
            return cert_real

    print("[Sage] Certificates not found in common paths", flush=True)
    return None


def get_sage_cert_candidates(extra_data_dirs: Optional[List[str]] = None) -> List[str]:
    """Return likely Sage wallet.crt paths for GUI pre-fill/browse fallback.

    These are path suggestions only. Callers must still validate with
    validate_sage_cert_pair() before saving anything.
    """
    candidates: List[str] = []

    configured = os.getenv("SAGE_CERT_PATH", "").strip()
    if configured:
        try:
            configured_path = _normalise_path(configured)
            if os.path.basename(configured_path).lower() == "wallet.crt":
                candidates.append(configured_path)
        except Exception:
            pass

    for ssl_dir in _candidate_sage_ssl_dirs(extra_data_dirs):
        candidates.append(os.path.join(ssl_dir, "wallet.crt"))

    return _dedupe_paths(candidates)


def _detect_sage_cert_path() -> Optional[str]:
    """Backward-compatible private wrapper for older imports/tests."""
    return detect_sage_cert_path()


def _launch_sage_exe(exe_path: str) -> bool:
    """Launch sage-tauri.exe as a separate process.

    Uses subprocess.Popen with Windows CREATE_NEW_PROCESS_GROUP and
    CREATE_BREAKAWAY_FROM_JOB so Sage runs independently of CATalyst's
    console and kill-on-close process job.

    Args:
        exe_path: Full path to sage-tauri.exe

    Returns:
        True if launch succeeded.
    """
    import sys as _sys

    if not os.path.isfile(exe_path):
        print(f"[Sage] Exe not found: {exe_path}", flush=True)
        return False

    try:
        if _sys.platform == "win32":
            try:
                proc = subprocess.Popen(
                    [exe_path],
                    **hidden_subprocess_kwargs(
                        detached=True,
                        new_process_group=True,
                        breakaway_from_job=True,
                    ),
                )
            except OSError as breakaway_error:
                print(
                    "[Sage] Breakaway launch failed; retrying without job breakaway: "
                    f"{breakaway_error}",
                    flush=True,
                )
                log_event(
                    "warning",
                    "sage_startup_breakaway_failed",
                    "Sage launch could not break away from the app process job; "
                    "retrying without breakaway",
                )
                proc = subprocess.Popen(
                    [exe_path],
                    **hidden_subprocess_kwargs(detached=True, new_process_group=True),
                )
        else:
            proc = subprocess.Popen([exe_path])

        print(f"[Sage] Launched {os.path.basename(exe_path)} (PID {proc.pid})",
              flush=True)
        log_event("info", "sage_startup", f"Launched sage-tauri.exe (PID {proc.pid})")
        return True

    except Exception as e:
        print(f"[Sage] Failed to launch: {e}", flush=True)
        log_event("error", "sage_startup", f"Failed to launch sage-tauri.exe: {e}")
        return False


def _is_sage_process_running() -> bool:
    """Check if the Sage exe process is running regardless of RPC state.

    Used to distinguish "Sage not open" from "Sage open but RPC disabled".
    """
    import sys as _sys
    try:
        if _sys.platform == "win32":
            from win_subprocess import hidden_subprocess_kwargs as _hskw
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq sage-tauri.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
                **_hskw(),
            )
            return "sage-tauri.exe" in result.stdout
        else:
            result = subprocess.run(
                ["pgrep", "-fi", "sage"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return False


def reset_preload():
    """Reset preload thread state so start_preload() can be called again.

    Call this before re-triggering begin-startup (e.g. after the user enables
    Sage RPC and clicks Rescan in the startup wizard).
    """
    global _preload_running, _sage_startup_phase
    _preload_running = False
    with _phase_lock:
        _sage_startup_phase = "connecting"


def _is_sage_rpc_available() -> bool:
    """Quick check if Sage RPC is responding on port 9257.

    Must verify the response is a genuine success — not an error dict.
    wallet_sage.rpc() returns {"error": ..., "success": False} on
    ConnectionError (instead of None), so checking 'is not None' alone
    gives false positives when Sage isn't running.
    """
    try:
        from wallet_sage import rpc
        result = rpc("get_version", {}, timeout=3)
        if result is None:
            return False
        if isinstance(result, dict) and result.get("success") is False:
            return False  # Error dict — connection failed
        if isinstance(result, dict) and "error" in result:
            return False  # Error response
        return True
    except Exception:
        return False


def _is_sage_rpc_port_listening() -> bool:
    """Return True when Sage's RPC TCP port accepts connections.

    This is intentionally weaker than `_is_sage_rpc_available()`: it tells the
    startup wizard that the RPC server is on, even if the authenticated TLS call
    still fails because cert paths have not been applied yet.
    """
    try:
        from wallet_sage import _sage_rpc_port_reachable
        return bool(_sage_rpc_port_reachable())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Node Status (aggregated view for dashboard)
# ---------------------------------------------------------------------------

def get_node_status(bypass_cache: bool = False) -> Dict:
    """Get comprehensive node status for the dashboard Node Status tab.

    Aggregates: blockchain state, wallet sync, peer count, network info.
    Each RPC call is individually wrapped so one slow/failed call
    doesn't block the entire dashboard from loading.

    If the background preload has recent cached data (< 15s old),
    returns it instantly. This means the dashboard opens immediately.
    """
    global _node_status_cache, _node_status_cache_time

    # Serve from cache if fresh enough (and not a preload call itself)
    if not bypass_cache:
        with _cache_lock:
            cached_snapshot = _node_status_cache
            cache_time_snapshot = _node_status_cache_time
        if cached_snapshot:
            cache_age = time.time() - cache_time_snapshot
            if cache_age < _NODE_STATUS_CACHE_MAX_AGE:
                return cached_snapshot
    from wallet import (
        get_chia_health,
        get_blockchain_state_full,
        get_peer_connections,
    )

    # Run all three RPC calls in parallel — each can take up to 5s,
    # running them sequentially would mean 15s+ which times out the frontend.
    health = {"status": "unknown", "healthy": False, "wallet": {}, "node": {}}
    blockchain = {}
    peers = []

    def _fetch_health():
        return get_chia_health()

    def _fetch_blockchain():
        return get_blockchain_state_full() or {}

    def _fetch_peers():
        return get_peer_connections() or []

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            future_health = executor.submit(_fetch_health)
            future_blockchain = executor.submit(_fetch_blockchain)
            future_peers = executor.submit(_fetch_peers)

            try:
                health = future_health.result(timeout=8)
            except Exception:
                pass
            try:
                blockchain = future_blockchain.result(timeout=8)
            except Exception:
                pass
            try:
                peers = future_peers.result(timeout=8)
            except Exception:
                pass
    except Exception:
        pass  # ThreadPool itself failed — use defaults

    full_node_peers = [p for p in peers if p.get("type") == 1]

    # Format space estimate (convert bytes to human-readable)
    space_bytes = blockchain.get("space_bytes") or 0
    space_display = _format_bytes(space_bytes) if space_bytes else "Unknown"

    # Peak timestamp to human-readable
    peak_ts = blockchain.get("peak_timestamp") or 0
    peak_age = ""
    if peak_ts > 0:
        age_seconds = time.time() - peak_ts
        if age_seconds < 60:
            peak_age = f"{int(age_seconds)}s ago"
        elif age_seconds < 3600:
            peak_age = f"{int(age_seconds / 60)}m ago"
        else:
            peak_age = f"{int(age_seconds / 3600)}h ago"

    return {
        # Overall status
        "status": health.get("status", "unknown"),
        "healthy": health.get("healthy", False),

        # Wallet
        "wallet_synced": health.get("wallet", {}).get("synced", False),
        "wallet_syncing": health.get("wallet", {}).get("syncing", False),
        "wallet_sync_state": health.get("wallet", {}).get("sync_state", "unknown"),
        "wallet_reachable": health.get("wallet", {}).get("reachable", False),

        # Full node
        "node_synced": blockchain.get("synced", False),
        "node_syncing": blockchain.get("syncing", False),
        "node_reachable": health.get("node", {}).get("reachable", False),

        # Blockchain
        "peak_height": blockchain.get("peak_height", 0),
        "peak_timestamp": peak_ts,
        "peak_age": peak_age,
        "difficulty": blockchain.get("difficulty", 0),
        "network_space": space_display,
        "mempool_size": blockchain.get("mempool_size", 0),

        # Sync progress (when syncing)
        "sync_tip_height": blockchain.get("sync_tip_height", 0),
        "sync_progress_height": blockchain.get("sync_progress_height", 0),

        # Peers
        "peer_count": len(full_node_peers),
        "total_connections": len(peers),
        "peers": full_node_peers[:20],  # Cap at 20 for display

        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Wallet Status (aggregated view for dashboard Wallet Status tab)
# ---------------------------------------------------------------------------

def get_wallet_status() -> Dict:
    """Get aggregated wallet status for the dashboard Wallet Status tab.

    Parallelises multiple RPC calls and returns a single dict with:
    - Logged-in fingerprint
    - Wallet sync state
    - Wallet count (XCH + CATs)
    - XCH balance + coin breakdown
    - CAT wallet summaries
    """
    from wallet import (get_wallet_sync_status, get_wallets, get_wallet_balance,
                        get_spendable_coins_rpc, get_transaction_count)
    from wallet import rpc, WALLET_ID_XCH

    result = {
        "fingerprint": "",
        "sync_status": "unknown",
        "synced": False,
        "syncing": False,
        "wallet_sync_state": "unknown",
        "wallet_reachable": False,
        "xch_wallet_id": WALLET_ID_XCH,
        "xch_confirmed": 0,
        "xch_spendable": 0,
        "xch_pending": 0,
        "xch_coins_free": 0,
        "xch_coins_locked": 0,
        "xch_coins_total": 0,
        "xch_pending_tx": 0,
        "cat_wallets": [],
        "wallet_count_xch": 1,
        "wallet_count_cat": 0,
        "timestamp": time.time(),
    }

    # Parallel RPC calls for speed
    futures = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures["sync"] = executor.submit(get_wallet_sync_status)
        # Sage uses get_current_key(), Chia uses get_logged_in_fingerprint
        _wtype = os.getenv("WALLET_TYPE", "sage").lower().strip()
        if _wtype == "sage":
            from wallet_sage import get_current_key as _get_key
            futures["fingerprint"] = executor.submit(_get_key)
        else:
            futures["fingerprint"] = executor.submit(
                rpc, "get_logged_in_fingerprint", {}, 5)
        futures["wallets"] = executor.submit(get_wallets)
        futures["xch_balance"] = executor.submit(
            get_wallet_balance, WALLET_ID_XCH)
        futures["xch_coins"] = executor.submit(
            get_spendable_coins_rpc, WALLET_ID_XCH)

        # Collect results with individual timeouts
        for key, fut in futures.items():
            try:
                futures[key] = fut.result(timeout=8)
            except Exception as e:
                print(f"[WalletStatus] {key} failed: {e}", flush=True)
                futures[key] = None

    # --- Parse sync status ---
    sync = futures.get("sync")
    if sync and isinstance(sync, dict):
        result["wallet_reachable"] = sync.get("reachable", False)
        result["synced"] = sync.get("synced", False)
        result["syncing"] = sync.get("syncing", False)
        result["wallet_sync_state"] = sync.get("sync_state", "unknown")
        if result["wallet_sync_state"] == "synced" or sync.get("synced"):
            result["sync_status"] = "synced"
        elif result["wallet_sync_state"] == "not_synced" or sync.get("syncing"):
            result["sync_status"] = "syncing"
        elif result["wallet_sync_state"] == "unknown" and sync.get("reachable"):
            result["sync_status"] = "unknown"
        elif sync.get("reachable"):
            result["sync_status"] = "not_synced"
        else:
            result["sync_status"] = "offline"

    # --- Parse fingerprint ---
    fp_data = futures.get("fingerprint")
    if fp_data and isinstance(fp_data, dict):
        # Chia returns {"success": true, "fingerprint": ...}
        # Sage returns {"fingerprint": ..., "name": ...} from get_current_key()
        fp_val = fp_data.get("fingerprint")
        if fp_val:
            result["fingerprint"] = str(fp_val)

    # --- Parse wallets (count + CAT details) ---
    wallets_data = futures.get("wallets")
    cat_wallets_raw = []
    if wallets_data and isinstance(wallets_data, dict):
        for w in wallets_data.get("wallets", []):
            wtype = w.get("type", 0)
            if wtype == 6 or str(wtype) == "6":
                cat_wallets_raw.append(w)
        result["wallet_count_cat"] = len(cat_wallets_raw)

    # --- Parse XCH balance ---
    xch_bal = futures.get("xch_balance")
    if xch_bal and isinstance(xch_bal, dict):
        wb = xch_bal.get("wallet_balance", xch_bal)
        confirmed = wb.get("confirmed_wallet_balance", 0)
        spendable = wb.get("spendable_balance", 0)
        pending = wb.get("pending_total_balance", 0)
        result["xch_confirmed"] = confirmed / 1e12
        result["xch_spendable"] = spendable / 1e12
        result["xch_pending"] = pending / 1e12

    # --- Parse XCH coins ---
    # The RPC only returns spendable (free) coins. To get locked counts,
    # we query our own database for open buy offers (each locks one XCH coin).
    xch_coins = futures.get("xch_coins")
    if xch_coins and isinstance(xch_coins, dict) and xch_coins.get("success"):
        confirmed_records = xch_coins.get("confirmed_records", [])
        xch_free_count = len(confirmed_records)
        result["xch_coins_free"] = xch_free_count

        # Get locked count from database
        xch_locked_count = 0
        try:
            from database import get_open_offers, get_locked_coins
            from config import cfg
            # Method 1: count open buy offers (each locks one XCH coin)
            buy_offers = get_open_offers(side="buy", cat_asset_id=cfg.CAT_ASSET_ID)
            offers_locked = len(buy_offers)
            # Method 2: count locked XCH coins in coins table
            db_locked = len(get_locked_coins(wallet_type="xch"))
            # Use whichever is higher (handles transition period)
            xch_locked_count = max(offers_locked, db_locked)
        except Exception:
            pass
        result["xch_coins_locked"] = xch_locked_count
        result["xch_coins_total"] = xch_free_count + xch_locked_count

    # --- Fetch pending TX count ---
    try:
        result["xch_pending_tx"] = get_transaction_count(WALLET_ID_XCH)
    except Exception:
        pass

    # --- Fetch CAT wallet details (balance + coins per CAT) ---
    cat_lookup = _build_cat_name_lookup()
    cat_results = []
    for w in cat_wallets_raw:
        wid = w.get("id", 0)
        asset_id = (w.get("data", "") or w.get("asset_id", "")).strip()
        raw_name = w.get("name", f"CAT {wid}")
        name = _resolve_cat_name(raw_name, asset_id, wid, cat_lookup)
        decimals = 3  # CATs default to 3

        cat_info = {
            "wallet_id": wid,
            "name": name,
            "asset_id": asset_id[:16] + "..." if len(asset_id) > 16 else asset_id,
            "balance": 0,
            "coins": 0,
        }

        try:
            bal = get_wallet_balance(wid)
            if bal and isinstance(bal, dict):
                wb = bal.get("wallet_balance", bal)
                cat_info["balance"] = round(
                    wb.get("confirmed_wallet_balance", 0) / (10 ** decimals), 2)
        except Exception:
            pass

        try:
            coins = get_spendable_coins_rpc(wid)
            if coins and isinstance(coins, dict) and coins.get("success"):
                cat_info["coins"] = len(coins.get("confirmed_records", []))
        except Exception:
            pass

        cat_results.append(cat_info)

    result["cat_wallets"] = cat_results
    return result


# ---------------------------------------------------------------------------
# Balances (all wallets)
# ---------------------------------------------------------------------------

def get_all_balances() -> Dict:
    """Get balances for XCH + all CAT wallets.

    Returns a dict with wallet summaries for the dashboard Balances tab.
    Resolves friendly CAT names from: .env config > Dexie pairs > wallet name.
    """
    from wallet import get_wallet_balance, get_wallets, WALLET_ID_XCH

    wallets_result = get_wallets()
    balances = []

    # Build a name lookup from .env config (the active trading CAT)
    cat_names = _build_cat_name_lookup()

    # XCH balance
    xch_result = get_wallet_balance(WALLET_ID_XCH)
    if xch_result and xch_result.get("success"):
        wb = xch_result.get("wallet_balance", {})
        balances.append({
            "wallet_id": WALLET_ID_XCH,
            "name": "Chia (XCH)",
            "type": "xch",
            "confirmed": float(wb.get("confirmed_wallet_balance", 0)) / 1e12,
            "spendable": float(wb.get("spendable_balance", 0)) / 1e12,
            "pending_total": float(wb.get("pending_total_balance", 0)) / 1e12,
            "unconfirmed": float(wb.get("unconfirmed_wallet_balance", 0)) / 1e12,
            "unit": "XCH",
            "decimals": 12,
        })

    # CAT balances
    if wallets_result and wallets_result.get("success"):
        for w in wallets_result.get("wallets", []):
            wtype = w.get("type", 0)
            if wtype == 6 or str(wtype) == "6" or str(wtype).upper() == "CAT":
                wallet_id = w.get("id", 0)
                raw_name = w.get("name", "Unknown CAT")
                asset_id = w.get("data", "") or w.get("asset_id", "")
                decimals = 3  # CATs default to 3 decimals

                # Resolve friendly name
                name = _resolve_cat_name(raw_name, asset_id, wallet_id, cat_names)
                unit = name.split(" ")[0] if name else "CAT"

                cat_result = get_wallet_balance(wallet_id)
                if cat_result and cat_result.get("success"):
                    wb = cat_result.get("wallet_balance", {})
                    scale = 10 ** decimals
                    balances.append({
                        "wallet_id": wallet_id,
                        "name": name,
                        "type": "cat",
                        "asset_id": asset_id[:16] + "..." if len(asset_id) > 16 else asset_id,
                        "asset_id_full": asset_id,
                        "confirmed": float(wb.get("confirmed_wallet_balance", 0)) / scale,
                        "spendable": float(wb.get("spendable_balance", 0)) / scale,
                        "pending_total": float(wb.get("pending_total_balance", 0)) / scale,
                        "unconfirmed": float(wb.get("unconfirmed_wallet_balance", 0)) / scale,
                        "unit": unit,
                        "decimals": decimals,
                    })

    return {
        "balances": balances,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Coins (UTXO listing with lock detection)
# ---------------------------------------------------------------------------

def get_coins_for_display(wallet_id: int, is_cat: bool = False,
                          decimals: int = 3) -> Dict:
    """Get all coins for a wallet with status information.

    Shows which coins are free (spendable) vs locked in offers.
    Used by the Chia Dashboard Coins tab.

    The Chia RPC `get_spendable_coins` only returns FREE coins — there is
    no RPC that returns locked coins. So we use our own database:
      - Spendable coins from RPC → these are free
      - Open offers from DB (with coin_id) → these coins are locked
      - Coins table from DB → any additional locked coins
    """
    from wallet import get_spendable_coins_rpc

    # ---- Step 1: Get all spendable (free) coins from wallet RPC ----
    spendable_result = get_spendable_coins_rpc(wallet_id)
    if not spendable_result or not spendable_result.get("success"):
        return {"coins": [], "error": "Could not fetch coins"}

    coins = []
    scale = 1e12 if not is_cat else (10 ** decimals)
    seen_coin_ids = set()

    for record in spendable_result.get("confirmed_records", []):
        coin_data = record.get("coin", {})
        parent = coin_data.get("parent_coin_info", "")
        puzzle = coin_data.get("puzzle_hash", "")
        amount = coin_data.get("amount", 0)

        coin_id = _compute_coin_id(parent, puzzle, amount)
        normalised = coin_id.lower().replace("0x", "")

        coins.append({
            "coin_id": coin_id,
            "coin_id_short": coin_id[:12] + "..." + coin_id[-8:] if len(coin_id) > 24 else coin_id,
            "parent": parent[:16] + "..." if len(parent) > 16 else parent,
            "amount_mojos": amount,
            "amount_display": amount / scale,
            "status": "free",
            "confirmed_height": record.get("confirmed_block_index", 0),
            "timestamp": record.get("timestamp", 0),
        })
        seen_coin_ids.add(normalised)

    # ---- Step 2: Get locked coins from our database ----
    # Source A: offers table — open offers with a recorded coin_id
    # Source B: coins table — any coins marked as 'locked'
    locked_coin_entries = []
    try:
        from database import get_open_offers, get_locked_coins
        from config import cfg

        # Source A: open offers with coin_id
        wallet_type = "cat" if is_cat else "xch"
        side_filter = "sell" if is_cat else "buy"
        open_offers = get_open_offers(side=side_filter, cat_asset_id=cfg.CAT_ASSET_ID)

        for offer in open_offers:
            offer_coin_id = offer.get("coin_id")
            if not offer_coin_id:
                continue
            normalised = offer_coin_id.lower().replace("0x", "")
            if normalised in seen_coin_ids:
                continue  # Already shown as free — shouldn't happen but skip

            # Calculate amount from offer size
            if is_cat:
                try:
                    size_cat = float(offer.get("size_cat", 0))
                    amount_mojos = int(size_cat * (10 ** decimals))
                except Exception:
                    amount_mojos = 0
            else:
                try:
                    size_xch = float(offer.get("size_xch", 0))
                    amount_mojos = int(size_xch * 1e12)
                except Exception:
                    amount_mojos = 0

            locked_coin_entries.append({
                "coin_id": offer_coin_id,
                "coin_id_short": offer_coin_id[:12] + "..." + offer_coin_id[-8:] if len(offer_coin_id) > 24 else offer_coin_id,
                "parent": "",
                "amount_mojos": amount_mojos,
                "amount_display": amount_mojos / scale,
                "status": "locked",
                "confirmed_height": 0,
                "timestamp": 0,
            })
            seen_coin_ids.add(normalised)

        # Source B: coins table — locked coins not already captured
        db_locked = get_locked_coins(wallet_type=wallet_type)
        for db_coin in db_locked:
            cid = db_coin.get("coin_id", "")
            normalised = cid.lower().replace("0x", "")
            if normalised in seen_coin_ids:
                continue
            amount_mojos = db_coin.get("amount_mojos", 0)
            locked_coin_entries.append({
                "coin_id": cid,
                "coin_id_short": cid[:12] + "..." + cid[-8:] if len(cid) > 24 else cid,
                "parent": "",
                "amount_mojos": amount_mojos,
                "amount_display": amount_mojos / scale,
                "status": "locked",
                "confirmed_height": 0,
                "timestamp": 0,
            })
            seen_coin_ids.add(normalised)

    except Exception:
        pass  # Database not available — just show free coins

    coins.extend(locked_coin_entries)

    # ---- Step 3: Add pending additions (from unconfirmed transactions) ----
    for record in spendable_result.get("unconfirmed_additions", []):
        coin_data = record.get("coin", {})
        amount = coin_data.get("amount", 0)
        coins.append({
            "coin_id": "pending",
            "coin_id_short": "Pending...",
            "parent": "",
            "amount_mojos": amount,
            "amount_display": amount / scale,
            "status": "pending",
            "confirmed_height": 0,
            "timestamp": 0,
        })

    # Sort: locked first, then free, then pending. Within each: largest first.
    status_order = {"locked": 0, "free": 1, "pending": 2}
    coins.sort(key=lambda c: (status_order.get(c["status"], 3), -c["amount_mojos"]))

    # Summary
    free_coins = [c for c in coins if c["status"] == "free"]
    locked_coins = [c for c in coins if c["status"] == "locked"]
    pending_coins = [c for c in coins if c["status"] == "pending"]

    return {
        "coins": coins,
        "summary": {
            "total_coins": len(coins),
            "free_coins": len(free_coins),
            "locked_coins": len(locked_coins),
            "pending_coins": len(pending_coins),
            "total_amount": sum(c["amount_display"] for c in coins),
            "free_amount": sum(c["amount_display"] for c in free_coins),
            "locked_amount": sum(c["amount_display"] for c in locked_coins),
        },
        "wallet_id": wallet_id,
        "is_cat": is_cat,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Coin Operations (split via CLI — proven pattern from coin_prep_worker)
# ---------------------------------------------------------------------------

def split_coin(wallet_id: int, coin_id: str, num_pieces: int,
               is_cat: bool = False, decimals: int = 3) -> Dict:
    """Split a specific coin into multiple pieces.

    Chia: Uses `chia wallet coins split` CLI (proven reliable method).
    Sage: Uses wallet adapter's split_coins_rpc (native RPC split).

    Args:
        wallet_id: Wallet ID
        coin_id: The coin to split (hex string with or without 0x prefix)
        num_pieces: How many coins to create
        is_cat: Whether this is a CAT wallet
        decimals: CAT decimal places (ignored for XCH)

    Returns:
        Dict with success status and message
    """
    from wallet import get_spendable_coins_rpc

    # Validate
    if num_pieces < 2:
        return {"success": False, "error": "Must split into at least 2 pieces"}
    if num_pieces > 50:
        return {"success": False, "error": "Maximum 50 pieces per split"}

    # Sage: use native RPC split instead of CLI
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    if wallet_type == "sage":
        try:
            from wallet import split_coins_rpc
            log_event("info", "coin_split_manual",
                      f"[Sage] Splitting coin {coin_id[:16]}... into {num_pieces} pieces")
            result = split_coins_rpc(wallet_id, num_pieces, coin_id, is_cat=is_cat, fee_mojos=0)
            if result and (result.get("success") or isinstance(result, dict)):
                log_event("success", "coin_split_manual",
                          f"[Sage] Split submitted: {coin_id[:16]}... → {num_pieces} pieces")
                return {
                    "success": True,
                    "message": f"Split submitted via Sage RPC! Creating {num_pieces} coins.",
                    "output": str(result)[:500],
                }
            else:
                error = (result or {}).get("error", "Unknown error")
                return {"success": False, "error": f"Sage split failed: {error}"}
        except Exception as e:
            log_event("error", "coin_split_manual", f"[Sage] Split error: {e}")
            return {"success": False, "error": f"Sage split error: {e}"}

    # Get fingerprint from env
    from dotenv import load_dotenv
    load_dotenv()
    fingerprint = os.getenv("CHIA_FINGERPRINT", "")
    if not fingerprint:
        return {"success": False, "error": "Select a wallet fingerprint in the app before starting Chia"}

    # Normalize coin_id
    if not coin_id.startswith("0x"):
        coin_id = "0x" + coin_id

    # Get the coin's amount from the wallet to calculate split size
    # get_spendable_coins_rpc returns raw RPC dict with confirmed_records
    rpc_result = get_spendable_coins_rpc(wallet_id)
    target_coin = None
    amount_mojos = 0
    if rpc_result and rpc_result.get("success"):
        for record in rpc_result.get("confirmed_records", []):
            coin_data = record.get("coin", {})
            parent = coin_data.get("parent_coin_info", "")
            puzzle = coin_data.get("puzzle_hash", "")
            amt = coin_data.get("amount", 0)
            if parent and puzzle:
                computed_id = _compute_coin_id(parent, puzzle, amt)
                if computed_id.lower() == coin_id.lower() or \
                   computed_id.lower().replace("0x", "") == coin_id.lower().replace("0x", ""):
                    target_coin = record
                    amount_mojos = amt
                    break

    if not target_coin:
        return {"success": False, "error": f"Coin {coin_id[:16]}... not found or not spendable"}
    if amount_mojos <= 0:
        return {"success": False, "error": "Coin has zero or negative amount"}

    # Calculate amount per piece (even split, remainder stays as change)
    amount_per_piece = amount_mojos // num_pieces

    if is_cat:
        # CAT: amount in token units for CLI
        amount_str = str(Decimal(str(amount_per_piece)) / Decimal(10 ** decimals))
    else:
        # XCH: amount in XCH for CLI
        amount_str = str(Decimal(str(amount_per_piece)) / Decimal("1000000000000"))

    # Build CLI command
    cmd = [
        "chia", "wallet", "coins", "split",
        "-f", fingerprint,
        "-i", str(wallet_id),
        "-n", str(num_pieces),
        "-a", amount_str,
        "-t", coin_id,
        "-m", "0",
    ]

    log_event("info", "coin_split_manual",
              f"Splitting coin {coin_id[:16]}... into {num_pieces} × {amount_str} "
              f"({'CAT' if is_cat else 'XCH'})")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            **hidden_subprocess_kwargs(),
        )

        output = result.stdout + result.stderr
        if result.returncode == 0 and "transaction submitted" in output.lower():
            log_event("success", "coin_split_manual",
                      f"Split submitted: {coin_id[:16]}... → {num_pieces} pieces")
            return {
                "success": True,
                "message": f"Split submitted! Creating {num_pieces} coins of {amount_str} each.",
                "output": output[:500],
            }
        else:
            log_event("warning", "coin_split_manual",
                      f"Split may have failed: {output[:200]}")
            return {
                "success": False,
                "error": f"CLI returned: {output[:300]}",
            }

    except subprocess.TimeoutExpired:
        log_event("error", "coin_split_manual", "Split command timed out after 120s")
        return {"success": False, "error": "Command timed out after 120 seconds"}
    except Exception as e:
        log_event("error", "coin_split_manual", f"Split error: {e}")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Transaction History
# ---------------------------------------------------------------------------

def get_transaction_history(wallet_id: int, offset: int = 0,
                            limit: int = 50) -> Dict:
    """Get formatted transaction history for the dashboard.

    Returns transactions in a display-friendly format.
    """
    from wallet import get_transactions_list, get_transaction_count

    total = get_transaction_count(wallet_id)

    result = get_transactions_list(wallet_id, start=offset, end=offset + limit)
    if not result or not result.get("success"):
        return {"transactions": [], "total": 0, "error": "Could not fetch transactions"}

    # Determine scale based on wallet type
    is_xch = (wallet_id == 1)
    scale = 1e12 if is_xch else 1000  # XCH uses 1e12, CATs typically 1e3

    transactions = []
    for tx in result.get("transactions", []):
        amount = tx.get("amount", 0)
        fee = tx.get("fee_amount", 0)
        tx_type = tx.get("type", 0)
        confirmed = tx.get("confirmed", False)
        height = tx.get("confirmed_at_height", 0)
        created = tx.get("created_at_time", 0)

        # Determine direction
        # Type mapping: 0=incoming, 1=outgoing, 2=self, 3=incoming_trade, 4=outgoing_trade
        type_names = {0: "receive", 1: "send", 2: "self", 3: "trade_in", 4: "trade_out"}
        direction = type_names.get(tx_type, f"type_{tx_type}")

        # Format timestamp
        time_str = ""
        if created > 0:
            import datetime
            dt = datetime.datetime.fromtimestamp(created)
            time_str = dt.strftime("%Y-%m-%d %H:%M")

        additions = tx.get("additions", [])
        removals = tx.get("removals", [])

        transactions.append({
            "tx_name": tx.get("name", "")[:16] + "...",
            "tx_name_full": tx.get("name", ""),
            "type": direction,
            "amount": amount / scale,
            "amount_mojos": amount,
            "fee": fee / 1e12,  # Fees always in XCH
            "fee_mojos": fee,
            "confirmed": confirmed,
            "height": height,
            "timestamp": created,
            "time_display": time_str,
            "additions_count": len(additions),
            "removals_count": len(removals),
            "trade_id": tx.get("trade_id", ""),
        })

    return {
        "transactions": transactions,
        "total": total,
        "offset": offset,
        "limit": limit,
        "wallet_id": wallet_id,
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Daemon Control (via CLI — simple and reliable)
# ---------------------------------------------------------------------------

def get_daemon_status() -> Dict:
    """Check which Chia services are currently running.

    Uses `chia show -s` to detect running services.
    Caches result for 10 seconds to avoid hammering CLI.
    For Sage wallet: skips CLI check, just probes wallet RPC.
    """
    global _last_daemon_status, _last_daemon_check

    now = time.time()
    if now - _last_daemon_check < _DAEMON_CACHE_SECONDS and _last_daemon_status:
        return _last_daemon_status

    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()

    # Sage: no Chia daemon — just check if wallet RPC responds
    if wallet_type == "sage":
        status = {
            "daemon_running": True,  # Sage runs independently
            "wallet_running": False,
            "full_node_running": False,  # No full node with Sage
            "farmer_running": False,
            "harvester_running": False,
            "services": ["sage_wallet"],
            "raw_output": "Sage light wallet (no Chia daemon)",
            "timestamp": now,
        }
        try:
            from wallet import get_wallet_sync_status
            ws = get_wallet_sync_status()
            if ws.get("reachable"):
                status["wallet_running"] = True
        except Exception:
            pass
        _last_daemon_status = status
        _last_daemon_check = now
        return status

    with _daemon_lock:
        status = {
            "daemon_running": False,
            "wallet_running": False,
            "full_node_running": False,
            "farmer_running": False,
            "harvester_running": False,
            "services": [],
            "raw_output": "",
            "timestamp": now,
        }

        try:
            # Check if daemon is running by trying to get status
            result = subprocess.run(
                ["chia", "show", "-s"],
                capture_output=True, text=True, timeout=15,
                **hidden_subprocess_kwargs(),
            )
            output = result.stdout + result.stderr
            status["raw_output"] = output[:1000]

            # Parse output for service status
            output_lower = output.lower()

            if "current blockchain status" in output_lower or "full node" in output_lower:
                status["daemon_running"] = True
                status["full_node_running"] = True
                status["services"].append("full_node")

            if "wallet height" in output_lower or "wallet synced" in output_lower:
                status["wallet_running"] = True
                status["services"].append("wallet")

            if "farming status" in output_lower or "farmer" in output_lower:
                # Check with a separate command
                pass

            # If we got any output, daemon is running
            if result.returncode == 0 and output.strip():
                status["daemon_running"] = True

            # Also try wallet RPC to confirm wallet is actually responding
            try:
                from wallet import get_wallet_sync_status
                ws = get_wallet_sync_status()
                if ws.get("reachable"):
                    status["wallet_running"] = True
                    if "wallet" not in status["services"]:
                        status["services"].append("wallet")
            except Exception:
                pass

        except subprocess.TimeoutExpired:
            status["raw_output"] = "Command timed out — daemon may be unresponsive"
        except FileNotFoundError:
            status["raw_output"] = "chia CLI not found — check PATH"
            status["error"] = "Chia CLI not found"
        except Exception as e:
            status["raw_output"] = str(e)

        _last_daemon_status = status
        _last_daemon_check = now
        return status


def start_chia(services: str = "wallet") -> Dict:
    """Start Chia services via CLI.

    Args:
        services: What to start — "wallet", "farmer", or "all" (default)
                  "wallet" starts wallet service only
                  "farmer" starts farmer + harvester
                  "all" starts everything (daemon, wallet, full node, farmer, harvester)

    Returns:
        Dict with success status and CLI output
    """
    # Sage: no Chia daemon to start — Sage wallet runs independently
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    if wallet_type == "sage":
        return {
            "success": True,
            "message": "Sage wallet runs independently — no Chia daemon to start",
            "output": "Sage light wallet does not use the Chia daemon",
        }

    valid_services = {"wallet", "farmer", "all"}
    if services not in valid_services:
        return {"success": False, "error": f"Invalid service: {services}. Use: {valid_services}"}

    log_event("info", "daemon_start", f"Starting Chia services: {services}")

    try:
        result = subprocess.run(
            ["chia", "start", services],
            capture_output=True, text=True, timeout=30,
            **hidden_subprocess_kwargs(),
        )
        output = result.stdout + result.stderr

        # Invalidate daemon status cache
        global _last_daemon_check
        _last_daemon_check = 0

        if result.returncode == 0:
            log_event("success", "daemon_start", f"Chia {services} started: {output[:100]}")
            return {
                "success": True,
                "message": f"Chia {services} started",
                "output": output[:500],
            }
        else:
            log_event("warning", "daemon_start", f"Start returned code {result.returncode}: {output[:200]}")
            return {
                "success": False,
                "error": output[:300],
            }

    except subprocess.TimeoutExpired:
        log_event("error", "daemon_start", "Start command timed out")
        return {"success": False, "error": "Command timed out after 30 seconds"}
    except FileNotFoundError:
        return {"success": False, "error": "chia CLI not found — check your PATH"}
    except Exception as e:
        log_event("error", "daemon_start", f"Start error: {e}")
        return {"success": False, "error": str(e)}


def stop_chia(services: str = "all") -> Dict:
    """Stop Chia services via CLI.

    Args:
        services: What to stop — "all" (default), "wallet", "farmer"

    Returns:
        Dict with success status and CLI output
    """
    # Sage: no Chia daemon to stop
    wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
    if wallet_type == "sage":
        return {
            "success": True,
            "message": "Sage wallet runs independently — no Chia daemon to stop",
            "output": "Sage light wallet does not use the Chia daemon",
        }

    valid_services = {"all", "wallet", "farmer"}
    if services not in valid_services:
        return {"success": False, "error": f"Invalid service: {services}. Use: {valid_services}"}

    log_event("info", "daemon_stop", f"Stopping Chia services: {services}")

    try:
        # Use -d flag to also stop daemon when stopping all
        cmd = ["chia", "stop", services]
        if services == "all":
            cmd.append("-d")

        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30,
            **hidden_subprocess_kwargs(),
        )
        output = result.stdout + result.stderr

        # Invalidate daemon status cache
        global _last_daemon_check
        _last_daemon_check = 0

        if result.returncode == 0:
            log_event("success", "daemon_stop", f"Chia {services} stopped: {output[:100]}")
            return {
                "success": True,
                "message": f"Chia {services} stopped",
                "output": output[:500],
            }
        else:
            log_event("warning", "daemon_stop", f"Stop returned code {result.returncode}: {output[:200]}")
            return {
                "success": False,
                "error": output[:300],
            }

    except subprocess.TimeoutExpired:
        log_event("error", "daemon_stop", "Stop command timed out")
        return {"success": False, "error": "Command timed out after 30 seconds"}
    except FileNotFoundError:
        return {"success": False, "error": "chia CLI not found — check your PATH"}
    except Exception as e:
        log_event("error", "daemon_stop", f"Stop error: {e}")
        return {"success": False, "error": str(e)}


def start_sage(services: str = "wallet") -> Dict:
    """Start the Sage-facing wallet service path."""
    return start_chia(services)


def stop_sage(services: str = "all") -> Dict:
    """Stop the Sage-facing wallet service path."""
    return stop_chia(services)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_bytes(num_bytes: int) -> str:
    """Format byte count to human-readable (EiB for network space)."""
    if num_bytes <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"]
    idx = 0
    size = float(num_bytes)
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.2f} {units[idx]}"


def _compute_coin_id(parent_coin_info: str, puzzle_hash: str, amount: int) -> str:
    """Compute coin ID from its components: SHA256(parent + puzzle_hash + amount).

    Uses Chia's variable-length signed integer encoding for amount.
    """
    parent_bytes = bytes.fromhex(parent_coin_info.replace("0x", ""))
    puzzle_bytes = bytes.fromhex(puzzle_hash.replace("0x", ""))

    if amount == 0:
        amount_bytes = b""
    else:
        byte_count = (amount.bit_length() + 8) >> 3
        amount_bytes = amount.to_bytes(byte_count, byteorder="big", signed=True)

    coin_id_bytes = hashlib.sha256(parent_bytes + puzzle_bytes + amount_bytes).digest()
    return "0x" + coin_id_bytes.hex()


def _build_cat_name_lookup() -> Dict[str, str]:
    """Build a mapping of asset_id → friendly name from available sources.

    Sources (checked in priority order):
    1. .env config: CAT_ASSET_ID + CAT_NAME (the active trading CAT)
    2. Dexie pairs cache (if api_server has fetched them)
    3. Well-known Chia CATs (hardcoded fallback for common tokens)

    Returns:
        Dict mapping lowercase asset_id hex → display name
    """
    names: Dict[str, str] = {}

    # Well-known Chia CATs (common tokens users are likely to have)
    well_known = {
        "a628c1c2c6fcb74d53746157e438e108eab5c0bb3e5c80ff3b1684d  ": "SBX",      # Spacebucks
        "6d95dae356e32a71db5ddcb42224754a02524c615c5fc35f568c2af04774e589": "USDS",     # Stably USD
        "8ebf855de6eb146db5602f0456d2f0cbe750d57f821b6f91a8592ee9f1d4cf31": "DBX",      # dexie bucks
        "509deafe3cd8bbfbb9ccce1d930e3d7b57b40c964fa33379b18d628175eb7a8f": "CH21",     # Chia Holiday 2021
        "78ad32a8c9ea70f27d73e9306fc467bab2a6b15b30289791e37e6a9a6cf03884": "SBX",      # Spacebucks v2
        "ccda19944b4def44bb4bc25363bb54b7a3d0b627f05a3f1aec67cf69aee7dadb": "HOA",      # HOA
    }
    names.update(well_known)

    # .env config — the active trading CAT (highest priority)
    try:
        from dotenv import load_dotenv
        load_dotenv()
        env_asset_id = os.getenv("CAT_ASSET_ID", "").strip().lower()
        env_cat_name = os.getenv("CAT_NAME", "").strip()
        if env_asset_id and env_cat_name:
            names[env_asset_id] = env_cat_name
    except Exception:
        pass

    # Try to get Dexie pairs for broader name resolution
    try:
        import requests as _req
        try:
            from api_call_tracker import record as _t
            _t("dexie", "/v2/prices/tickers")
        except Exception:
            pass
        dexie_base = os.getenv("DEXIE_API_BASE", "https://api.dexie.space")
        url = f"{dexie_base}/v2/prices/tickers"
        response = _req.get(url, timeout=5)
        if response.status_code == 200:
            tickers = response.json().get("tickers", [])
            for ticker in tickers:
                ticker_id = ticker.get("ticker_id", "")
                if "_XCH" in ticker_id:
                    base_id = ticker.get("base_id", "").strip().lower()
                    base_name = ticker.get("base_name", "") or ticker_id.replace("_XCH", "")
                    if base_id and base_name:
                        # Don't overwrite .env config (already set above)
                        if base_id not in names:
                            names[base_id] = base_name
    except Exception:
        pass  # Dexie lookup is best-effort — don't break balances if it fails

    return names


def _resolve_cat_name(raw_name: str, asset_id: str, wallet_id: int,
                      cat_names: Dict[str, str]) -> str:
    """Resolve a friendly display name for a CAT token.

    The Chia wallet often stores CATs with names like their full tail hash
    or generic names like "CAT". This function tries to find a better name.

    Args:
        raw_name: The wallet's stored name (might be a hex hash)
        asset_id: The CAT's asset/tail ID
        wallet_id: The wallet ID number
        cat_names: Lookup dict from _build_cat_name_lookup()

    Returns:
        Best available display name for the CAT
    """
    # Check if we have this asset_id in our lookup
    asset_lower = asset_id.strip().lower()
    if asset_lower and asset_lower in cat_names:
        return cat_names[asset_lower]

    # Check if raw_name looks like a hash (64+ hex chars = not a real name)
    clean = raw_name.strip()
    if len(clean) >= 32 and all(c in "0123456789abcdefABCDEF" for c in clean):
        # It's a hash — abbreviate it instead of showing the full thing
        return f"CAT ({clean[:8]}...)"

    # Check for generic/unhelpful names
    if clean.lower() in ("cat", "unknown cat", "unknown", ""):
        if asset_lower:
            return f"CAT ({asset_lower[:8]}...)"
        return f"CAT (wallet {wallet_id})"

    # The wallet name looks reasonable — use it
    return clean
