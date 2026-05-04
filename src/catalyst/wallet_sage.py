"""Sage light-wallet RPC client with drop-in Chia-wallet API compatibility

Talks to the Sage light wallet (default ``https://127.0.0.1:9257``), a Rust-based
Chia wallet that connects directly to network peers without a full node. Exposes
the same function signatures as ``wallet_chia.py`` so it is both swappable
through ``wallet.py`` and directly imported by many modules including
``bot_loop``, ``coin_manager``, ``offer_manager``, ``api_server``, ``sage_node``,
and the coin-prep worker.

Key responsibilities:
    - Offer create / list / inspect / cancel with Sage's offered/requested asset format
    - XCH and CAT coin queries keyed by asset_id (no wallet_id abstraction)
    - Native ``split_xch`` / ``split_cat`` endpoints plus transfer helpers
    - Mutual-TLS client cert auto-generated on first run, reused per thread via
      ``http.client.HTTPSConnection`` for low-latency polling

Amounts are passed and returned as strings and converted to integer mojos
internally. A workaround in ``get_spendable_coins_with_owned_fallback``
compensates for Sage's ``filter_mode="selectable"`` hiding coins locked on both
sides of an active offer. ``cancel_offer`` treats HTTP 404 as success, since the
offer is already gone from the wallet's view.
"""

import os
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import hashlib
from typing import List, Dict, Optional, Tuple, Any
from decimal import Decimal, ROUND_DOWN
from tx_fees import get_effective_transaction_fee_mojos

# Silence warnings for localhost self-signed cert
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from user_paths import env_file as _env_file, data_dir as _data_dir
    load_dotenv(_env_file(), override=False)
except Exception:
    _data_dir = None
    load_dotenv()


def _console(msg: str) -> None:
    """Print to console, replacing unencodable chars on cp1252 Windows terminals."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)

# Sage defaults to port 9257 — uses HTTPS with self-signed cert
# IMPORTANT: use 127.0.0.1 (not localhost) to match Sage's actual bind address
WALLET_URL = os.getenv("SAGE_RPC_URL", "https://127.0.0.1:9257").rstrip("/")
CERT_PATH = os.getenv("SAGE_CERT_PATH", "")
KEY_PATH = os.getenv("SAGE_KEY_PATH", "")

# --- Auto-detect or generate client certificates ---
# Sage RPC requires mutual TLS. Clients must present a self-signed cert.
# Priority: SAGE_CERT_PATH env → auto-generated cert in bot directory.

def _generate_self_signed_cert(cert_path, key_path):
    """Generate a self-signed certificate for Sage RPC client auth.

    Uses Python's built-in ssl module if available, otherwise falls back
    to creating a minimal self-signed cert via subprocess (openssl).
    """
    try:
        # Try using cryptography library (may already be installed for Chia)
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "sage-bot-client"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CATalyst Bot"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256())
        )

        os.makedirs(os.path.dirname(cert_path), exist_ok=True)
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        # Restrict private key to owner-only access
        try:
            import stat
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError:
            pass  # Windows may not support POSIX permissions fully
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        print("✅ [Sage] Generated self-signed client cert")
        return True
    except ImportError:
        print("⚠️  [Sage] 'cryptography' package not installed — cannot generate client cert")
        print("   Install it with: pip install cryptography --break-system-packages")
        return False
    except Exception as e:
        print(f"⚠️  [Sage] Failed to generate client cert: {e}")
        return False


def _auto_client_cert_paths() -> Tuple[str, str]:
    try:
        _cert_base = _data_dir() if _data_dir else os.path.dirname(os.path.abspath(__file__))
    except Exception:
        _cert_base = os.path.dirname(os.path.abspath(__file__))
    return (
        os.path.join(_cert_base, "sage_client_ssl", "client.crt"),
        os.path.join(_cert_base, "sage_client_ssl", "client.key"),
    )


def _resolve_client_cert_paths(cert_path: str, key_path: str) -> Tuple[str, str]:
    if cert_path and key_path:
        return cert_path, key_path

    # Auto-generate a client cert in the user data directory so packaged
    # installs under Program Files never need to write beside the executable.
    _auto_cert, _auto_key = _auto_client_cert_paths()

    if os.path.exists(_auto_cert) and os.path.exists(_auto_key):
        return _auto_cert, _auto_key
    if _generate_self_signed_cert(_auto_cert, _auto_key):
        return _auto_cert, _auto_key
    return cert_path, key_path


CERT_PATH, KEY_PATH = _resolve_client_cert_paths(CERT_PATH, KEY_PATH)

# Sage doesn't use wallet IDs — but we keep the constant for API compatibility.
# Other modules import WALLET_ID_XCH for offer_dict keys.
# For Sage, offers use asset_id strings instead of wallet IDs, but the
# calling code passes this constant as a dict key. The create_offer()
# function below translates wallet_id keys into Sage's make_offer format.
WALLET_ID_XCH = int(os.getenv("CHIA_WALLET_ID_XCH", "1"))

# CAT asset ID — Sage needs this to query CAT coins (instead of wallet_id)
_CAT_ASSET_ID = os.getenv("CAT_ASSET_ID", "")

# Mapping from synthetic wallet_id → asset_id for multi-CAT discovery.
# Populated by get_wallets() when Sage's get_cats endpoint returns results.
# Allows get_wallet_balance() etc. to resolve any CAT, not just the configured one.
_wallet_id_to_asset_id: dict = {}

HEADERS = {"Content-Type": "application/json"}

WALLET_DEBUG = os.getenv("WALLET_DEBUG", "false").lower() == "true"

# Sage has no full node — these stubs exist for adapter compatibility.
# Modules that check FULL_NODE_URL will get None and skip full-node-only logic.
FULL_NODE_URL = None
FULL_NODE_CERT = None
FULL_NODE_KEY = None

# requests.Session stub — Sage uses raw http.client, not requests.
# Some modules reference wallet.session for timeout config.
session = None

_TLS_VERIFY = False

# --- Direct HTTPS connection (bypasses requests/urllib3 SSL quirks) ---
import ssl
import http.client
import json as _json

def _parse_sage_rpc_url(url: str) -> Tuple[str, int]:
    _url_body = (url or "https://127.0.0.1:9257").replace("https://", "").replace("http://", "").rstrip("/")
    if ":" in _url_body:
        host, _port_str = _url_body.split(":", 1)
        port = int(_port_str)
    else:
        host = _url_body
        port = 9257

    # IPv4/IPv6 dual-stack gotcha on Windows: Sage binds to 127.0.0.1 only,
    # but Python's getaddrinfo("localhost", ...) on modern Windows returns
    # the IPv6 [::1] address FIRST. Normalising local aliases avoids stalls.
    if host.lower() in ("localhost", "localhost.localdomain"):
        host = "127.0.0.1"
    return host, port


# Parse host/port once at module level; reload_connection_settings refreshes it
# if the setup flow writes a new SAGE_RPC_URL or cert path.
_SAGE_HOST, _SAGE_PORT = _parse_sage_rpc_url(WALLET_URL)


class SageMempoolConflict(Exception):
    """Raised when Sage reports a MEMPOOL_CONFLICT — two transactions tried to spend the same coin."""
    pass


class SageUnknownUnspent(Exception):
    """Raised when Sage reports UNKNOWN_UNSPENT — the coin doesn't exist in the UTXO set.

    Common causes:
    - Coin hasn't confirmed on-chain yet (pool coin created but not spendable)
    - Coin was already spent by another transaction
    - Stale coin ID from a previous state
    """
    pass


class SageAlreadyIncluding(Exception):
    """Raised when Sage reports ALREADY_INCLUDING_TRANSACTION — the cancel TX
    is already in the mempool from a previous submit. This is effectively a
    success: the cancel is in flight and just needs blocks to confirm.
    """
    pass


def _sage_tx_error_level(kind: str, endpoint: str) -> str:
    """Classify Sage transaction errors for operator-facing logs."""
    kind_norm = str(kind or "").upper()
    endpoint_norm = str(endpoint or "").lower()
    if kind_norm in {"MEMPOOL_CONFLICT", "ALREADY_INCLUDING"} and "cancel" in endpoint_norm:
        return "info"
    return "warning"


# ---- Sage RPC result validation ----

def _rpc_succeeded(result) -> bool:
    """Check if an RPC result represents a successful response.

    rpc() returns structured error dicts on transport/HTTP errors
    (e.g., {"success": False, "error": "Sage HTTP 500: ..."}),
    or None on unexpected exceptions. This function distinguishes
    genuine success from those failure modes.
    """
    if not isinstance(result, dict):
        return False
    if result.get("success") is False:
        return False
    if result.get("error") or result.get("error_message"):
        return False
    status = str(result.get("status") or "").strip().lower()
    if status in ("error", "failed", "failure"):
        return False
    return True


# ---- Sage initialization state ----
# Sage requires an explicit `initialize` RPC before wallet reads work.
# sage_login() calls sage_initialize(), but GUI/API read paths can race
# ahead before login finishes. This guard prevents those early reads from
# hitting un-initialized Sage and returning garbage/errors.
import threading as _thr
import time as _time
import socket as _sock
_init_lock = _thr.Lock()
_init_ok = False
_init_last_attempt = 0.0
# Cooldown MUST exceed the RPC timeout below, otherwise callers that were
# queued on _init_lock during a failing attempt will immediately retry the
# moment the lock releases — you get an N-caller × 8-second pile-up on
# startup when Sage is down.
_INIT_RETRY_SECS = 45.0
# Quick reachability pre-check timeout. If Sage's RPC port isn't even
# accepting connections, we want to know that in milliseconds so the
# startup path isn't held hostage waiting for HTTP/TLS timeouts.
_INIT_REACHABILITY_TIMEOUT = 0.5
# RPC timeout for the initialize call itself. 8s is enough for a healthy
# Sage to respond; if Sage is reachable but sluggish we don't want to
# block forever.
_INIT_RPC_TIMEOUT = 8


def _sage_rpc_port_reachable() -> bool:
    """Fast TCP reachability probe for the Sage RPC port.

    Returns True if something is listening, False otherwise. Never raises.
    Uses a sub-second timeout so startup paths aren't held hostage to
    HTTP/TLS timeouts when Sage isn't running.
    """
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        s.settimeout(_INIT_REACHABILITY_TIMEOUT)
        s.connect((_SAGE_HOST, _SAGE_PORT))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def ensure_initialized(force_retry: bool = False) -> bool:
    """Ensure Sage wallet manager has been initialized.

    Returns True if already initialized or initialization succeeds now.
    Uses a retry cooldown (_INIT_RETRY_SECS) instead of a permanent
    failure latch — transient errors don't permanently wedge the bot.

    Thread-safe: the actual RPC call happens inside _init_lock to prevent
    concurrent init attempts.

    Fast-fails when the Sage RPC port is not listening, so startup paths
    don't pay the full HTTP/TLS timeout on every caller when Sage is down.
    """
    global _init_ok, _init_last_attempt
    with _init_lock:
        if _init_ok:
            return True

        now = _time.time()
        if not force_retry and _init_last_attempt and (now - _init_last_attempt) < _INIT_RETRY_SECS:
            return False

        # Fast reachability pre-check — if Sage isn't listening at all,
        # record the attempt and bail immediately without paying the
        # multi-second HTTP/TLS timeout. This is the critical fix for
        # startup: with Sage down, the caller returns in <1s instead of
        # ~8s, and the cooldown still prevents retries for 45s.
        if not _sage_rpc_port_reachable():
            _init_last_attempt = _time.time()
            _console(f"  [Sage] RPC port {_SAGE_HOST}:{_SAGE_PORT} unreachable — skipping initialize")
            return False

        # Attempt initialization under the lock.
        # Some Sage versions don't have /initialize (returns 404) — that's OK,
        # it means the wallet doesn't require explicit init.
        try:
            result = rpc("initialize", {}, timeout=_INIT_RPC_TIMEOUT)
            if _rpc_succeeded(result):
                _console("  [Sage] initialize OK")
            elif isinstance(result, dict) and "404" in str(result.get("error", "")):
                _console("  [Sage] initialize endpoint not found (Sage version doesn't require it) — OK")
            else:
                _console(f"  [Sage] INIT FAILED: initialize returned {result!r}")
                # Record attempt time at the END of a failed attempt so
                # the cooldown starts from now, not from when we acquired
                # the lock. This prevents waiting-caller pile-ups.
                _init_last_attempt = _time.time()
                return False
            _init_ok = True
            return True
        except Exception as e:
            _console(f"  [Sage] INIT FAILED: initialize error: {e}")
            _init_last_attempt = _time.time()
            return False


# Thread-local connection cache — reuses TLS connections within a thread.
# Eliminates ~100-200ms TLS handshake overhead per RPC call.
_conn_local = _thr.local()


def reload_connection_settings() -> None:
    """Reload Sage RPC/cert paths after the GUI writes first-run settings."""
    global WALLET_URL, CERT_PATH, KEY_PATH, _SAGE_HOST, _SAGE_PORT
    global _init_ok, _init_last_attempt

    try:
        load_dotenv(_env_file(), override=False)
    except Exception:
        load_dotenv(override=True)

    WALLET_URL = os.getenv("SAGE_RPC_URL", "https://127.0.0.1:9257").rstrip("/")
    cert_path = os.getenv("SAGE_CERT_PATH", "")
    key_path = os.getenv("SAGE_KEY_PATH", "")
    CERT_PATH, KEY_PATH = _resolve_client_cert_paths(cert_path, key_path)
    _SAGE_HOST, _SAGE_PORT = _parse_sage_rpc_url(WALLET_URL)
    _conn_local.conn = None
    _init_ok = False
    _init_last_attempt = 0.0


def _get_sage_connection(timeout: int = 10):
    """Get or create a thread-local HTTPS connection to Sage.

    Reuses the existing connection if alive, creates a new one otherwise.
    """
    conn = getattr(_conn_local, 'conn', None)
    if conn is not None:
        # Check if the existing connection is still usable
        try:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                conn.timeout = timeout
                return conn
        except Exception:
            pass

    # Build SSL context
    ctx = ssl._create_unverified_context()
    if CERT_PATH and KEY_PATH:
        ctx.load_cert_chain(CERT_PATH, KEY_PATH)

    conn = http.client.HTTPSConnection(
        _SAGE_HOST, _SAGE_PORT, timeout=timeout, context=ctx
    )
    _conn_local.conn = conn
    return conn


def _sage_post(path: str, payload: dict, timeout: int = 10):
    """Low-level HTTPS POST to Sage, bypassing requests library entirely.

    Uses http.client + ssl for complete control over TLS negotiation.
    Presents our self-signed client cert for mutual TLS authentication.
    Reuses thread-local connections for performance.
    """
    body = _json.dumps(payload).encode("utf-8")
    headers_dict = {"Content-Type": "application/json", "Connection": "keep-alive"}

    conn = _get_sage_connection(timeout)
    try:
        conn.request("POST", "/" + path.lstrip("/"), body=body, headers=headers_dict)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
    except Exception:
        # Connection was stale — recreate and retry once
        _conn_local.conn = None
        ctx = ssl._create_unverified_context()
        if CERT_PATH and KEY_PATH:
            ctx.load_cert_chain(CERT_PATH, KEY_PATH)
        conn = http.client.HTTPSConnection(
            _SAGE_HOST, _SAGE_PORT, timeout=timeout, context=ctx
        )
        _conn_local.conn = conn
        conn.request("POST", "/" + path.lstrip("/"), body=body, headers=headers_dict)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")

    def _log_sage_error(kind: str, snippet: str):
        """Always-on structured logger for sage transaction errors so we
        can grep / display them in the GUI. Fail-open."""
        try:
            from database import log_event as _le
            # Capture which thread raised this so we can correlate with
            # boost_step/cancel/create activity in the same window.
            import threading as _t
            level = _sage_tx_error_level(kind, path)
            expected_cancel_settlement = level == "info"
            if expected_cancel_settlement:
                message = (
                    f"Sage {kind} on {path}: cancel appears in flight; "
                    "the bot will verify before marking it cancelled"
                )
            else:
                message = f"⚠️ Sage {kind} on {path}: {snippet[:200]}"
            _le(level, f"sage_{kind.lower()}",
                message,
                data={"endpoint": path, "kind": kind, "snippet": snippet[:500],
                      "expected_cancel_settlement": expected_cancel_settlement,
                      "thread": _t.current_thread().name})
        except Exception:
            pass

    if resp.status == 200:
        parsed = _json.loads(data)
        # Check for transaction errors in the response body
        # Sage may return 200 but include error details in the JSON
        if isinstance(parsed, dict):
            error_msg = parsed.get("error") or parsed.get("error_message") or ""
            status = parsed.get("status") or ""
            combined = str(error_msg) + str(status)
            if "MEMPOOL_CONFLICT" in combined:
                _log_sage_error("MEMPOOL_CONFLICT", combined)
                raise SageMempoolConflict(
                    f"MEMPOOL_CONFLICT on {path}: another transaction already spent one of these coins")
            if "UNKNOWN_UNSPENT" in combined:
                _log_sage_error("UNKNOWN_UNSPENT", combined)
                raise SageUnknownUnspent(
                    f"UNKNOWN_UNSPENT on {path}: coin not in UTXO set (not confirmed or already spent)")
            if "ALREADY_INCLUDING_TRANSACTION" in combined:
                _log_sage_error("ALREADY_INCLUDING", combined)
                raise SageAlreadyIncluding(
                    f"ALREADY_INCLUDING_TRANSACTION on {path}: cancel TX already in mempool")
        return parsed
    else:
        # Check for specific error types in non-200 responses
        if "MEMPOOL_CONFLICT" in data:
            _log_sage_error("MEMPOOL_CONFLICT", data)
            raise SageMempoolConflict(
                f"MEMPOOL_CONFLICT on {path}: {data[:200]}")
        if "UNKNOWN_UNSPENT" in data:
            _log_sage_error("UNKNOWN_UNSPENT", data)
            raise SageUnknownUnspent(
                f"UNKNOWN_UNSPENT on {path}: {data[:200]}")
        if "ALREADY_INCLUDING_TRANSACTION" in data:
            _log_sage_error("ALREADY_INCLUDING", data)
            raise SageAlreadyIncluding(
                f"ALREADY_INCLUDING_TRANSACTION on {path}: {data[:200]}")
        raise ConnectionError(f"Sage HTTP {resp.status}: {data[:300]}")

# Quiet mode: suppress RPC error logging
_quiet_mode = False


def set_quiet_mode(quiet: bool):
    """Enable/disable RPC error suppression."""
    global _quiet_mode
    _quiet_mode = quiet


def rpc(endpoint: str, payload: dict, timeout: int = 10):
    """Make RPC call to Sage wallet on port 9257.

    Uses direct http.client + ssl (bypasses requests/urllib3 SSL issues).
    Client certs loaded from: SAGE_CERT_PATH → CHIA_WALLET_CERT → ~/.chia defaults.

    Returns:
        dict on success, None on error.
        For MEMPOOL_CONFLICT, returns {"error": "MEMPOOL_CONFLICT", ...} so callers
        can detect it without catching exceptions.
    """
    # Guard: never send get_coins with empty string asset_id (fresh install, no token)
    if endpoint == "get_coins":
        aid = payload.get("asset_id")
        if aid is not None and (not aid or not str(aid).strip()):
            return None

    start = time.time()

    try:
        result = _sage_post(endpoint, payload, timeout=timeout)

        if WALLET_DEBUG:
            elapsed = time.time() - start
            if elapsed > 1.0:
                print(f"⏱️  [Sage] {endpoint} took {elapsed:.2f}s")

        return result
    except SageMempoolConflict as e:
        elapsed = time.time() - start
        _conflict_level = _sage_tx_error_level("MEMPOOL_CONFLICT", endpoint)
        if _conflict_level == "info":
            print(f"   [Sage] cancel already in flight on {endpoint} "
                  f"(after {elapsed:.2f}s); will verify")
        else:
            print(f"⚠️  [Sage] MEMPOOL_CONFLICT on {endpoint} (after {elapsed:.2f}s): {e}")
        # Structured event so we can query/display these in the GUI and
        # diagnose root cause. Includes payload key summary so we know
        # which coin/offer triggered it (without dumping full payloads).
        try:
            from database import log_event as _le
            _payload_summary = {
                k: (str(v)[:80] if not isinstance(v, (int, float, bool)) else v)
                for k, v in (payload or {}).items() if k != "puzzle_reveal"
            }
            if _conflict_level == "info":
                message = (
                    f"Sage MEMPOOL_CONFLICT on {endpoint} after {elapsed:.2f}s: "
                    "cancel appears in flight; bot will verify"
                )
            else:
                message = (
                    f"⚠️ Sage MEMPOOL_CONFLICT on {endpoint} after {elapsed:.2f}s: "
                    f"{str(e)[:200]}"
                )
            _le(_conflict_level, "sage_mempool_conflict",
                message,
                data={"endpoint": endpoint, "elapsed_secs": round(elapsed, 2),
                      "error_message": str(e)[:500],
                      "expected_cancel_settlement": _conflict_level == "info",
                      "payload_summary": _payload_summary})
        except Exception:
            pass  # additive — never block on logging failure
        # Return a structured error so callers can detect this specific failure
        return {"error": "MEMPOOL_CONFLICT", "success": False,
                "message": str(e), "endpoint": endpoint}
    except SageUnknownUnspent as e:
        elapsed = time.time() - start
        print(f"⚠️  [Sage] UNKNOWN_UNSPENT on {endpoint} (after {elapsed:.2f}s): {e}")
        # Return structured error — coin not confirmed on-chain yet or already spent
        return {"error": "UNKNOWN_UNSPENT", "success": False,
                "message": str(e), "endpoint": endpoint}
    except ConnectionError as e:
        elapsed = time.time() - start
        err_str = str(e)
        # Suppress the noisy error print for the one expected 404 case:
        # `initialize` returns 404 on Sage versions that don't require an
        # explicit init RPC. ensure_initialized() handles this dict and
        # treats it as success — there's no need to scare the operator
        # with a ❌ line in the log every startup.
        _is_expected_init_404 = (endpoint == "initialize" and "404" in err_str)
        if not _quiet_mode and not _is_expected_init_404:
            print(f"❌ Sage RPC error calling {endpoint} (after {elapsed:.2f}s): {e}")
        # Return structured error so callers can see the actual message
        # (previously returned None which became "Unknown error" in logs)
        return {"error": err_str[:300], "success": False, "endpoint": endpoint}
    except Exception as e:
        elapsed = time.time() - start
        if not _quiet_mode:
            print(f"❌ Sage RPC error calling {endpoint} (after {elapsed:.2f}s): {e}")
        return None


def full_node_rpc(endpoint: str, payload: dict, timeout: int = 5):
    """Sage is a light wallet — no full node RPC available.

    Returns None always. Modules that check full node health will
    see 'not available' which is expected for Sage.
    """
    return None


# ==================== KEY / FINGERPRINT MANAGEMENT ====================

def get_sage_keys() -> list:
    """Get all available keys/fingerprints from Sage wallet.

    Returns:
        List of dicts: [{name, fingerprint, public_key, kind, has_secrets, network_id}, ...]
        Empty list on error.
    """
    try:
        result = rpc("get_keys", {}, timeout=10)
        if result and isinstance(result, dict):
            return result.get("keys", [])
        elif result and isinstance(result, list):
            return result
    except Exception as e:
        print(f"  [Sage] get_sage_keys error: {e}")
    return []


def get_current_key() -> dict:
    """Get the currently active (logged-in) key.

    Returns:
        Dict with key info {name, fingerprint, ...} or None if not logged in.
    """
    if not ensure_initialized():
        return None
    try:
        result = rpc("get_key", {}, timeout=5)
        if result and isinstance(result, dict):
            key = result.get("key")
            if key and isinstance(key, dict):
                return key
    except Exception as e:
        print(f"  [Sage] get_current_key error: {e}")
    return None


def _require_signing_capability() -> bool:
    """Check that the active wallet has signing capability (has secrets).

    Watch-only wallets can query balances and offers but must NOT sign
    transactions, create offers, or cancel offers. This guard should be
    called at the top of any signing/submission path.

    Returns True if signing is allowed, False if watch-only.
    """
    try:
        key = get_current_key()
        if key and isinstance(key, dict):
            has_secrets = key.get("has_secrets", False)  # Default False — watch-only wallets must be blocked from signing by default
            if not has_secrets:
                print("  [Sage] BLOCKED: wallet is watch-only (no secrets) — cannot sign", flush=True)
                return False
            return True
        print("  [Sage] BLOCKED: active key unavailable — refusing signing operation", flush=True)
        return False
    except Exception as e:
        print(f"  [Sage] BLOCKED: signing capability check failed: {e}", flush=True)
        return False


def sage_initialize() -> bool:
    """Explicit Sage wallet manager initialization — required before wallet ops.

    Delegates to ensure_initialized() with force_retry=True so that
    an explicit init request always retries even within the cooldown window.
    """
    return ensure_initialized(force_retry=True)


def sage_login(fingerprint: int, force_resync: bool = False) -> bool:
    """Log in to a specific fingerprint via readiness check + resync + login.

    Sage requires:
    0. Readiness check — verify Sage RPC is responding before attempting login
    1. initialize — explicit wallet manager initialization
    2. resync(fingerprint) — loads the wallet data for that key (only if force_resync=True)
    3. login(fingerprint) — activates the key as the current session

    Args:
        fingerprint: Integer fingerprint to connect to.
        force_resync: If True, run resync before login. Default False.

    Returns:
        True if login succeeded and get_key confirms the right fingerprint.
    """
    fingerprint = int(fingerprint)
    print(f"  [Sage] Logging in to fingerprint {fingerprint}...")

    # Step 0: verify Sage is reachable before attempting login.
    # This distinguishes "Sage not running" from "login failed" errors.
    try:
        version_result = rpc("get_version", {}, timeout=5)
        if not _rpc_succeeded(version_result) or not (isinstance(version_result, dict) and version_result.get("version")):
            _console(f"  [Sage] INIT FAILED: Cannot reach Sage RPC: {version_result}")
            return False
        _console(f"  [Sage] Sage v{version_result['version']} is reachable -- proceeding with login")
    except Exception as e:
        _console(f"  [Sage] INIT FAILED: Cannot reach Sage RPC: {e}")
        return False

    # Step 1: explicit initialize
    if not sage_initialize():
        return False

    time.sleep(1)

    # Step 2: resync — loads wallet data (only when forced)
    if force_resync:
        result = rpc("resync", {"fingerprint": fingerprint}, timeout=30)
        if not _rpc_succeeded(result):
            _console(f"  [Sage] resync failed: {result}")
            return False
        _console("  [Sage] resync OK")

        time.sleep(1)

    # Step 3: login — activates the key
    result = rpc("login", {"fingerprint": fingerprint}, timeout=30)
    if not _rpc_succeeded(result):
        _console(f"  [Sage] login failed: {result}")
        return False
    _console("  [Sage] login OK")

    time.sleep(1)

    # Step 3: verify
    key = get_current_key()
    if key and key.get("fingerprint") == fingerprint:
        print(f"  [Sage] Confirmed: logged in as '{key.get('name', '?')}' ({fingerprint})")
        return True
    elif key:
        actual_fp = key.get('fingerprint')
        print(f"  [Sage] ERROR: fingerprint mismatch after login attempt — "
              f"wanted {fingerprint}, got {actual_fp}. Refusing to start.", flush=True)
        try:
            from super_log import log_event
            log_event("error", "wallet_fingerprint_mismatch",
                      f"Sage fingerprint mismatch: wanted {fingerprint}, got {actual_fp}. "
                      f"Bot will not trade from the wrong wallet.")
        except Exception:
            pass
        return False
    else:
        print("  [Sage] Login appeared to succeed but get_key returned null")
        return False


# ==================== HEALTH MONITORING ====================

def get_wallet_sync_status() -> dict:
    """Check Sage wallet sync status via RPC.

    Sage uses get_sync_status which returns:
      { "synced_coins": int, "total_coins": int, "unit": { ... }, ... }

    Note: older Sage versions returned a boolean "synced" field; current
    versions (0.12.x+) omit it and only report synced_coins/total_coins.
    We infer sync state from these counts when no explicit boolean is present.
    """
    if not ensure_initialized():
        return {"reachable": False, "synced": False, "syncing": False, "sync_state": "offline"}
    try:
        result = rpc("get_sync_status", {}, timeout=5)
        if _rpc_succeeded(result):
            raw_synced = result.get("synced")
            synced_coins = result.get("synced_coins", 0) or 0
            total_coins = result.get("total_coins", 0) or 0

            if raw_synced is True:
                sync_state = "synced"
                synced = True
                syncing = False
            elif raw_synced is False:
                sync_state = "not_synced"
                synced = False
                syncing = True
            elif total_coins > 0 and synced_coins >= total_coins:
                # Current Sage versions omit the boolean field and use
                # synced_coins == total_coins to indicate a fully synced wallet.
                sync_state = "synced"
                synced = True
                syncing = False
            elif total_coins > 0 and synced_coins < total_coins:
                sync_state = "not_synced"
                synced = False
                syncing = True
            else:
                # total_coins == 0: wallet may still be loading, stay unknown.
                sync_state, synced, syncing = "unknown", False, False

            return {
                "reachable": True,
                "synced": synced,
                "syncing": syncing,
                "sync_state": sync_state,
            }
        # rpc() returned something but _rpc_succeeded() rejected it as a
        # structured failure (HTTP non-200, error key, malformed payload).
        # Treat this as "wallet RPC is not actually healthy" so the health
        # monitor does not show a green tray while the RPC is silently
        # failing. Previously we returned reachable=True which, combined
        # with the bot_loop Sage service-ok shortcut, let operators miss
        # wallet outages entirely.
        return {"reachable": False, "synced": False, "syncing": False,
                "sync_state": "rpc_failed"}
    except Exception:
        return {"reachable": False, "synced": False, "syncing": False, "sync_state": "offline"}


def get_full_node_sync_status() -> dict:
    """Sage has no full node — return a neutral status.

    This lets the health monitor work without errors. The overall
    health check will report 'no_full_node' status.
    """
    return {
        "reachable": False,
        "synced": False,
        "syncing": False,
        "peak_height": 0,
        "note": "Sage is a light wallet — no full node"
    }


def get_chia_health() -> dict:
    """Combined health check — adapted for Sage (no full node).

    Sage is a light wallet — it doesn't have a full node sync state.
    If the wallet RPC is reachable and responding, we treat service health as
    healthy even when sync state is unknown. Startup/readiness logic remains
    conservative by using the explicit sync_state field.
    """
    wallet = get_wallet_sync_status()
    node = get_full_node_sync_status()

    # Peer count: Sage can respond to RPC but have zero working peer
    # connections, which means transactions are accepted locally but
    # never broadcast.  Detect this so the GUI can warn the user.
    try:
        _peers = get_peer_connections()
        peer_count = len(_peers) if isinstance(_peers, list) else 0
    except Exception:
        peer_count = -1  # unknown

    if not wallet["reachable"]:
        status, healthy = "unreachable", False
    elif wallet.get("reachable") and peer_count == 0:
        status, healthy = "no_peers", False
    elif wallet.get("sync_state") == "synced":
        status, healthy = "healthy", True
    elif wallet.get("sync_state") == "not_synced":
        status, healthy = "wallet_not_synced", False
    else:
        # Covers "unknown" and any other unexpected sync_state values.
        # Only explicit synced=True from Sage should be treated as healthy.
        status, healthy = "wallet_sync_unknown", False

    return {
        "status": status,
        "wallet": wallet,
        "node": node,
        "healthy": healthy,
        "peer_count": peer_count,
        "timestamp": time.time(),
        "wallet_type": "sage",
    }


# ============================================================================
# COIN MANAGEMENT
# ============================================================================

def _get_cat_asset_id() -> Optional[str]:
    """Get the active CAT asset ID. Always returns the most-recent value.

    Callers: use notify_cat_asset_id_changed() when the active CAT changes
    rather than relying on os.getenv(), because os.environ is only updated
    when cfg.update() flushes to disk and load_dotenv() is re-run.
    """
    global _CAT_ASSET_ID
    return _CAT_ASSET_ID or None


def notify_cat_asset_id_changed(asset_id: str) -> None:
    """Called by api_server when the user selects a new trading pair.

    Keeps _CAT_ASSET_ID in sync so _resolve_asset_id() cross-checks work
    correctly without reading stale values from os.environ.
    Also clears any per-wallet-id mismatch-warned flags so the first access
    after a switch gets a clean cross-check.
    """
    global _CAT_ASSET_ID
    new_id = (asset_id or "").strip().lower().replace("0x", "")
    old_id = (_CAT_ASSET_ID or "").strip().lower().replace("0x", "")
    if new_id != old_id:
        _CAT_ASSET_ID = (asset_id or "").strip() or None
        # Clear stale mismatch-warned flags so the next resolve is a fresh check
        for attr in [a for a in dir(_resolve_asset_id) if a.startswith("_mismatch_warned_")]:
            try:
                delattr(_resolve_asset_id, attr)
            except AttributeError:
                pass
        print(f"[Sage] Active CAT updated: {new_id[:16] if new_id else 'none'}", flush=True)


def _is_cat_wallet(wallet_id: int) -> bool:
    """Determine if a wallet_id refers to a CAT wallet.

    In the Chia wallet, wallet_id=1 is always XCH, and CAT wallets
    have higher IDs. We use this heuristic for the Sage adapter since
    Sage doesn't use wallet IDs at all.
    """
    return wallet_id != WALLET_ID_XCH


def _get_coin_query_asset_id(wallet_id: int) -> Tuple[bool, Optional[str]]:
    """Resolve the Sage asset_id for a wallet_id."""
    if _is_cat_wallet(wallet_id):
        asset_id = _resolve_asset_id(wallet_id)
        if not asset_id or not asset_id.strip():
            return False, None
        return True, asset_id
    return True, None


def _extract_sage_coin_list(result: Optional[Dict]) -> List[Dict[str, Any]]:
    """Extract Sage coin dicts from a get_coins-style response."""
    if not result or not isinstance(result, dict):
        return []

    found = result.get("coins") or result.get("records") or result.get("data") or []
    if not found:
        for value in result.values():
            if isinstance(value, list) and len(value) > 0:
                found = value
                break

    return [coin for coin in found if isinstance(coin, dict)]


def _normalize_sage_coin_records(coins: List[Dict[str, Any]],
                                 min_amount_mojos: int = 0,
                                 max_amount_mojos: Optional[int] = None) -> List[Dict[str, Any]]:
    """Convert raw Sage coin dicts into the Chia-compatible record shape."""
    records = []
    for coin in coins:
        raw_amount = (coin.get("amount") or coin.get("amt")
                      or coin.get("value") or "0")
        amount = int(raw_amount)

        if min_amount_mojos > 0 and amount < min_amount_mojos:
            continue
        if max_amount_mojos is not None and amount > max_amount_mojos:
            continue

        parent = (coin.get("parent_coin_info") or coin.get("parent_coin")
                  or coin.get("parentCoin") or coin.get("parent") or "")
        puzzle = (coin.get("puzzle_hash") or coin.get("puzzleHash")
                  or coin.get("puzzle") or "")
        coin_id = (coin.get("coin_id") or coin.get("id")
                   or coin.get("coinId") or coin.get("name") or "")

        records.append({
            "coin": {
                "parent_coin_info": parent,
                "puzzle_hash": puzzle,
                "amount": amount,
            },
            "spent_block_index": 0,
            "coin_id": coin_id,
        })

    return records


def _query_coin_records(wallet_id: int, filter_mode: str,
                        min_amount_mojos: int = 0,
                        max_amount_mojos: Optional[int] = None) -> Optional[Dict]:
    """Query Sage get_coins for one filter mode and normalize the records."""
    supported, asset_id = _get_coin_query_asset_id(wallet_id)
    if not supported:
        return None

    result = rpc("get_coins", {
        "asset_id": asset_id,
        "offset": 0,
        "limit": 500,
        "sort_mode": "amount",
        "filter_mode": filter_mode,
        "ascending": False,
    }, timeout=15)

    if not result or not isinstance(result, dict):
        return None

    coins = _extract_sage_coin_list(result)
    if not coins:
        result_keys = [k for k in result.keys() if k not in ("success", "error")]
        if result_keys:
            total = result.get("total")
            total_suffix = f", total={total}" if total is not None else ""
            print(f"⚠️  [Sage] get_coins({filter_mode}) returned 0 coins "
                  f"(keys: {result_keys}{total_suffix})", flush=True)

    if WALLET_DEBUG and coins:
        print(f"   🔍 [Sage] First {filter_mode} coin keys: "
              f"{list(coins[0].keys()) if isinstance(coins[0], dict) else 'not a dict'}")

    records = _normalize_sage_coin_records(
        coins,
        min_amount_mojos=min_amount_mojos,
        max_amount_mojos=max_amount_mojos,
    )
    return {
        "success": True,
        "records": records,
        "confirmed_records": records,
    }


def get_spendable_coins(wallet_id: int, min_amount_mojos: int = 0,
                        max_amount_mojos: int = None) -> Optional[Dict]:
    """Query Sage's strict selectable view within a size range."""
    return _query_coin_records(
        wallet_id,
        "selectable",
        min_amount_mojos=min_amount_mojos,
        max_amount_mojos=max_amount_mojos,
    )


def get_spendable_coins_with_owned_fallback(wallet_id: int, min_amount_mojos: int = 0,
                                            max_amount_mojos: int = None) -> Optional[Dict]:
    """Compatibility helper that merges owned-only coins into the selectable set."""
    if _is_cat_wallet(wallet_id):
        asset_id = _resolve_asset_id(wallet_id)
        if not asset_id or not asset_id.strip():
            # No token configured yet (fresh install) — skip silently
            return None
    else:
        asset_id = None  # XCH = null asset_id

    # ── SAGE BUG WORKAROUND ──────────────────────────────────────────
    # Sage's filter_mode="selectable" hides coins on BOTH sides of an
    # offer — not just the offered side.  GitHub issue submitted to
    # xch-dev/sage.  Workaround: fetch "owned" (all held coins) AND
    # "selectable" (conservative set).  Return selectable first (known
    # free), then append any owned-only coins that the bug wrongly hid.
    # If a genuinely locked coin slips through, the wallet will reject
    # the offer at creation time — safe fallback.
    # ────────────────────────────────────────────────────────────────

    # 1) Fetch ALL held coins (free + offer-locked, excludes spent)
    owned_result = rpc("get_coins", {
        "asset_id": asset_id,
        "offset": 0,
        "limit": 500,
        "sort_mode": "amount",
        "filter_mode": "owned",
        "ascending": False,
    }, timeout=15)

    # 2) Fetch selectable coins (Sage's conservative set — may under-report)
    selectable_result = rpc("get_coins", {
        "asset_id": asset_id,
        "offset": 0,
        "limit": 500,
        "sort_mode": "amount",
        "filter_mode": "selectable",
        "ascending": False,
    }, timeout=15)

    if not owned_result and not selectable_result:
        return None

    # ── DIAGNOSTIC: log raw coin counts from Sage ──────────────────
    # Helps debug cases where Sage returns fewer coins than expected
    # (e.g., auto-combine merging split coins back into one).
    wtype = "CAT" if asset_id else "XCH"
    _owned_raw = []
    _sel_raw = []
    if owned_result and isinstance(owned_result, dict):
        _owned_raw = owned_result.get("coins") or owned_result.get("records") or owned_result.get("data") or []
    if selectable_result and isinstance(selectable_result, dict):
        _sel_raw = selectable_result.get("coins") or selectable_result.get("records") or selectable_result.get("data") or []
    if len(_owned_raw) <= 3 or len(_sel_raw) <= 3:
        # Only log when suspiciously few coins — helps catch auto-combine
        print(f"🔍 [Sage] get_coins({wtype}) raw: "
              f"owned={len(_owned_raw)} coins, selectable={len(_sel_raw)} coins",
              flush=True)
        if _owned_raw and isinstance(_owned_raw[0], dict):
            for i, c in enumerate(_owned_raw[:3]):
                amt = c.get("amount", "?")
                cid = (c.get("coin_id") or c.get("id") or c.get("coinId")
                       or c.get("name") or "?")
                print(f"   owned[{i}]: id={str(cid)[:20]}... amount={amt}",
                      flush=True)

    # Extract coin lists from both responses
    def _extract_coins(result):
        if not result or not isinstance(result, dict):
            return []
        found = result.get("coins") or result.get("records") or result.get("data") or []
        if not found:
            for k in result.keys():
                v = result.get(k)
                if isinstance(v, list) and len(v) > 0:
                    found = v
                    break
        return found

    owned_coins = _extract_coins(owned_result)
    selectable_coins = _extract_coins(selectable_result)

    # Build set of selectable coin IDs for priority ordering
    selectable_ids = set()
    for c in selectable_coins:
        if isinstance(c, dict):
            cid = (c.get("coin_id") or c.get("id") or c.get("coinId")
                   or c.get("name") or "")
            if cid:
                selectable_ids.add(cid.lower().replace("0x", ""))

    # Merge: selectable coins first, then owned-only coins.
    # NOTE: owned-only does not prove spendability. This compatibility
    # path exists because some Sage builds have reported fewer selectable
    # coins than expected in practice, but the difference may also reflect
    # offer locks or other non-selectable states.
    coins = list(selectable_coins)  # start with known-free
    hidden_count = 0
    for c in owned_coins:
        if not isinstance(c, dict):
            continue
        cid = (c.get("coin_id") or c.get("id") or c.get("coinId")
               or c.get("name") or "")
        if cid and cid.lower().replace("0x", "") not in selectable_ids:
            coins.append(c)
            hidden_count += 1

    if hidden_count > 0:
        wtype = "CAT" if asset_id else "XCH"
        # Only log when the hidden count changes — prevents 200+ duplicate messages
        _cache_key = f"_hidden_{wtype}"
        _prev = getattr(get_spendable_coins_with_owned_fallback, _cache_key, -1)
        if hidden_count != _prev:
            setattr(get_spendable_coins_with_owned_fallback, _cache_key, hidden_count)
            print(f"🔧 [Sage workaround] {hidden_count} {wtype} owned-only coins "
                  f"were added back from the owned set "
                  f"(total: {len(coins)}, selectable: {len(selectable_coins)}, "
                  f"owned: {len(owned_coins)})", flush=True)

    if not coins and owned_result and isinstance(owned_result, dict):
        result_keys = [k for k in owned_result.keys() if k not in ('success', 'error')]
        if result_keys:
            print(f"⚠️  [Sage] get_coins response keys: {result_keys} "
                  f"(none matched coins/records/data)", flush=True)

    if WALLET_DEBUG and coins:
        print(f"   🔍 [Sage] First coin keys: {list(coins[0].keys()) if isinstance(coins[0], dict) else 'not a dict'}")

    # Convert Sage coin format to Chia-compatible records.
    # Defensive: try multiple possible field names since Sage RPC
    # format may vary between versions.
    records = []
    for coin in coins:
        if not isinstance(coin, dict):
            continue

        # Amount — try multiple field names, handle string or int
        raw_amount = (coin.get("amount") or coin.get("amt")
                      or coin.get("value") or "0")
        amount = int(raw_amount)

        # Apply filters
        if min_amount_mojos > 0 and amount < min_amount_mojos:
            continue
        if max_amount_mojos is not None and amount > max_amount_mojos:
            continue

        # Parent coin info — try multiple field names
        parent = (coin.get("parent_coin_info") or coin.get("parent_coin")
                  or coin.get("parentCoin") or coin.get("parent") or "")

        # Puzzle hash — try multiple field names
        puzzle = (coin.get("puzzle_hash") or coin.get("puzzleHash")
                  or coin.get("puzzle") or "")

        # Coin ID — try multiple field names
        coin_id = (coin.get("coin_id") or coin.get("id")
                   or coin.get("coinId") or coin.get("name") or "")

        records.append({
            "coin": {
                "parent_coin_info": parent,
                "puzzle_hash": puzzle,
                "amount": amount,
            },
            "spent_block_index": 0,  # Sage only returns spendable coins
            "coin_id": coin_id,
        })

    return {
        "success": True,
        "records": records,
        "confirmed_records": records,  # Alias used by some callers
    }


def count_suitable_coins(wallet_id: int, target_amount_mojos: int,
                         tolerance: float = 0.25, is_cat: bool = False,
                         decimals: int = 3) -> int:
    """Count how many coins are suitable for a specific offer size."""
    min_mojos = int(target_amount_mojos * (1 - tolerance))
    max_mojos = int(target_amount_mojos * (1 + tolerance))

    result = get_spendable_coins(wallet_id, min_mojos, max_mojos)

    if not result or not result.get("success"):
        return 0

    records = result.get("records", [])
    return len(records)


def get_selectable_coins_only(wallet_id: int) -> Optional[Dict]:
    """Get ONLY selectable (on-chain confirmed) coins — NO workaround.

    This bypasses the Sage selectable bug workaround that merges owned+selectable.
    Use this when you need to know what's actually confirmed on the blockchain,
    e.g., during coin prep confirmation polling where we must NOT proceed
    until coins are truly on-chain.

    Returns same format as get_spendable_coins() for compatibility.
    """
    if _is_cat_wallet(wallet_id):
        asset_id = _resolve_asset_id(wallet_id)
        if not asset_id:
            return None
    else:
        asset_id = None

    result = rpc("get_coins", {
        "asset_id": asset_id,
        "offset": 0,
        "limit": 500,
        "sort_mode": "amount",
        "filter_mode": "selectable",
        "ascending": False,
    }, timeout=15)

    if not result or not isinstance(result, dict):
        return None

    # Extract coins from response
    found = result.get("coins") or result.get("records") or result.get("data") or []
    if not found:
        for k in result.keys():
            v = result.get(k)
            if isinstance(v, list) and len(v) > 0:
                found = v
                break

    # Convert to Chia-compatible format
    records = []
    for coin in found:
        if not isinstance(coin, dict):
            continue
        raw_amount = (coin.get("amount") or coin.get("amt")
                      or coin.get("value") or "0")
        amount = int(raw_amount)
        parent = (coin.get("parent_coin_info") or coin.get("parent_coin")
                  or coin.get("parentCoin") or coin.get("parent") or "")
        puzzle = (coin.get("puzzle_hash") or coin.get("puzzleHash")
                  or coin.get("puzzle") or "")
        coin_id = (coin.get("coin_id") or coin.get("id")
                   or coin.get("coinId") or coin.get("name") or "")
        records.append({
            "coin": {
                "parent_coin_info": parent,
                "puzzle_hash": puzzle,
                "amount": amount,
            },
            "spent_block_index": 0,
            "coin_id": coin_id,
        })

    return {
        "success": True,
        "records": records,
        "confirmed_records": records,
    }


def get_spendable_coin_count(wallet_id: int) -> int:
    """Get the count of truly spendable coins using Sage's native endpoint.

    This calls Sage's get_spendable_coin_count endpoint which returns ONLY
    coins that are confirmed on-chain and not locked by offers or pending
    transactions. It should match the strict selectable view returned by
    get_spendable_coins() / get_selectable_coins_only().

    Use this during coin prep confirmation polling to know when coins
    are actually on-chain.

    Sage API: get_spendable_coin_count { asset_id: Option<String> }
    Returns: { count: u32 }
    """
    ensure_initialized()

    if _is_cat_wallet(wallet_id):
        asset_id = _resolve_asset_id(wallet_id)
        if not asset_id:
            return 0
    else:
        asset_id = None  # XCH = null asset_id

    result = rpc("get_spendable_coin_count", {
        "asset_id": asset_id,
    }, timeout=10)

    if _rpc_succeeded(result):
        count = result.get("count", 0)
        if isinstance(count, int):
            return count
        # Try parsing from string
        try:
            return int(count)
        except (ValueError, TypeError):
            pass
    # RPC failed or returned error — return -1 so callers can distinguish
    # "zero coins" from "could not reach wallet"
    error_detail = ""
    if isinstance(result, dict):
        error_detail = result.get("error", "")
    print(f"  [Sage] get_spendable_coin_count failed for wallet {wallet_id}: {error_detail or 'no response'}")
    return -1


def get_pending_transactions() -> Optional[list]:
    """Get all pending (unconfirmed) transactions from Sage.

    Returns a list of PendingTransactionRecord dicts, each with:
      - transaction_id: str
      - fee: str (amount in mojos)
      - submitted_at: int or None (epoch seconds)

    When this returns an empty list, all transactions are confirmed.
    Use this to wait for a send/split to actually hit the blockchain
    before proceeding to the next operation.

    Sage API: get_pending_transactions {}
    Returns: { pending_transactions: [...] }
    """
    ensure_initialized()

    result = rpc("get_pending_transactions", {}, timeout=10)

    if _rpc_succeeded(result):
        # Try multiple possible response keys
        pending = (result.get("pending_transactions")
                   or result.get("transactions")
                   or result.get("data")
                   or [])
        if isinstance(pending, list):
            return pending
    # RPC failed or returned error — return None so callers can distinguish
    # "no pending transactions" from "could not reach wallet"
    error_detail = ""
    if isinstance(result, dict):
        error_detail = result.get("error", "")
    print(f"  [Sage] get_pending_transactions failed: {error_detail or 'no response'}")
    return None


def _are_coins_spendable_rpc(coin_ids: list) -> Optional[bool]:
    """Call Sage's exact spendability endpoint for a set of coin ids."""
    if not coin_ids:
        return True

    normalized = []
    for cid in coin_ids:
        if isinstance(cid, str):
            clean = cid.lower()
            if clean.startswith("0x"):
                clean = clean[2:]
            normalized.append(clean)

    result = rpc("get_are_coins_spendable", {
        "coin_ids": normalized,
    }, timeout=10)

    if result is None:
        return None

    return result.get("spendable", False) is True


def get_spendable_coins_rpc(wallet_id: int) -> Optional[Dict]:
    """Get the strict selectable-only spendable coins for a wallet."""
    return get_spendable_coins(wallet_id, min_amount_mojos=0)


def split_coins_rpc(wallet_id: int, target_coin_id: str, num_coins: int,
                    amount_per_coin: int, fee_mojos: int = 0,
                    is_cat: bool = False) -> Optional[Dict]:
    """Split a coin into multiple smaller coins using Sage's native /split endpoint.

    IMPORTANT: Sage uses a single generic /split endpoint (not split_xch/split_cat).
    It works for both XCH and CAT coins — Sage determines the type from the coin IDs.

    NOTE: Sage /split divides the parent coin evenly by output_count.
    The amount_per_coin parameter is NOT sent to Sage — it is used only
    by the caller for balance validation before calling this function.
    The actual output size = parent_coin_amount / output_count.

    Source: sage-api/src/requests/transactions.rs (struct Split)
    Parameters:
      - coin_ids: Vec<String>   — which coin(s) to split
      - output_count: u32       — how many output coins to create
      - fee: Amount (string)    — transaction fee in mojos
      - auto_submit: bool       — whether to submit immediately (default false)
    """
    if not _require_signing_capability():
        return None

    # Sage expects coin_ids as an array, strip 0x prefix
    bare_coin_id = target_coin_id.replace("0x", "") if target_coin_id else ""
    coin_ids = [bare_coin_id]

    payload = {
        "coin_ids": coin_ids,
        "output_count": num_coins,
        "fee": str(int(fee_mojos)),
        "auto_submit": True,
    }

    print(f"   [Sage] Splitting coin {bare_coin_id[:16]}... into {num_coins} outputs via /split")
    result = rpc("split", payload, timeout=60)
    if WALLET_DEBUG:
        print(f"  [Sage] split result: {result}")
    return result


def create_transaction_rpc(selected_coin_ids: list, actions: list,
                            auto_submit: bool = True) -> Optional[Dict]:
    """Sage's flexible transaction builder with forced coin selection.

    /create_transaction supports:
    - selected_coin_ids: force specific input coins to be spent
    - actions: arbitrary custom-sized output amounts

    This is the correct approach for topup splits — /send_xch silently
    ignores coin_ids hints, but /create_transaction honors selected_coin_ids.

    Source: sage-api/src/requests/action_system.rs (struct CreateTransaction)
    Parameters:
      - selected_coin_ids: Vec<String>  — pre-selected input coins (bare hex, no 0x)
      - actions: Vec<Action>            — send/fee actions for outputs
      - auto_submit: bool               — whether to submit immediately

    Action types (serde snake_case tagged):
      Send XCH:
        {"type": "send", "id": {"type": "xch"}, "address": "xch1...",
         "amount": "<mojos>", "memos": []}
      Send CAT:
        {"type": "send", "id": {"type": "existing", "asset_id": "0x..."},
         "address": "xch1...", "amount": "<mojos>", "memos": []}
      Fee:
        {"type": "fee", "amount": "<mojos>"}
    """
    if not _require_signing_capability():
        return None

    # Strip 0x prefixes from coin IDs
    bare_ids = [cid.replace("0x", "") for cid in (selected_coin_ids or [])]

    payload = {
        "selected_coin_ids": bare_ids,
        "actions": actions,
        "auto_submit": auto_submit,
    }

    print(f"   [Sage] create_transaction: {len(bare_ids)} selected coins, "
          f"{len(actions)} actions via /create_transaction")
    result = rpc("create_transaction", payload, timeout=60)
    if WALLET_DEBUG:
        print(f"  [Sage] create_transaction result: {result}")
    if auto_submit:
        return _submit_coin_spends_if_needed(result, "create_transaction")
    return result


def _response_transaction_id(result: Optional[Dict]) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    single = result.get("transaction_id") or result.get("tx_id")
    if single:
        return str(single)
    tx_ids = result.get("transaction_ids")
    if isinstance(tx_ids, list) and tx_ids:
        return str(tx_ids[0])
    nested = result.get("transaction") or result.get("tx")
    if isinstance(nested, dict):
        nested_single = nested.get("transaction_id") or nested.get("tx_id")
        if nested_single:
            return str(nested_single)
        nested_ids = nested.get("transaction_ids")
        if isinstance(nested_ids, list) and nested_ids:
            return str(nested_ids[0])
    return None


def _submit_coin_spends_if_needed(result: Optional[Dict], context: str) -> Optional[Dict]:
    """Sign and submit Sage responses that only contain unsigned coin_spends."""
    if not isinstance(result, dict):
        return result
    if result.get("error") or result.get("success") is False:
        return result
    if _response_transaction_id(result):
        return result

    coin_spends = result.get("coin_spends")
    if not coin_spends:
        return result

    try:
        sign_resp = _sage_post("sign_coin_spends", {
            "coin_spends": coin_spends,
            "auto_submit": False,
            "partial": False,
        }, timeout=30)
    except Exception as exc:
        return {
            "success": False,
            "error": f"{context} sign_coin_spends failed: {exc}",
        }

    if not isinstance(sign_resp, dict):
        return {
            "success": False,
            "error": f"{context} sign_coin_spends returned non-dict response",
        }

    spend_bundle = sign_resp.get("spend_bundle")
    if not spend_bundle or not spend_bundle.get("aggregated_signature"):
        return {
            "success": False,
            "error": f"{context} sign_coin_spends returned no signed spend bundle",
        }

    try:
        submit_resp = _sage_post("submit_transaction", {
            "spend_bundle": spend_bundle,
        }, timeout=30)
    except Exception as exc:
        return {
            "success": False,
            "error": f"{context} submit_transaction failed: {exc}",
        }

    if not isinstance(submit_resp, dict):
        return {
            "success": False,
            "error": f"{context} submit_transaction returned non-dict response",
        }

    submit_error = submit_resp.get("error") or submit_resp.get("reason")
    submit_status = str(submit_resp.get("status", "") or "").lower()
    if (
        submit_error
        or submit_status in ("failed", "error", "rejected")
        or submit_resp.get("success") is False
    ):
        return {
            "success": False,
            "error": f"{context} submit_transaction rejected: "
                     f"{submit_error or submit_status or 'success=false'}",
            "submit_response": submit_resp,
        }

    out = dict(result)
    out["success"] = True
    out["submitted"] = True
    out["submit_response"] = submit_resp
    tx_id = _response_transaction_id(submit_resp)
    if tx_id:
        out["transaction_id"] = tx_id
    else:
        try:
            pending_count = len(get_pending_transactions() or [])
        except Exception:
            pending_count = None
        if pending_count == 0:
            return {
                "success": False,
                "error": (
                    f"{context} submit_transaction returned no transaction id "
                    "and Sage reports no pending transaction"
                ),
                "submit_response": submit_resp,
            }
    return out


def sage_topup_split(source_coin_id: str, num_coins: int, trading_size_mojos: int,
                     own_address: str, fee_mojos: int = 0,
                     is_cat: bool = False,
                     fee_coin_id: Optional[str] = None) -> Optional[Dict]:
    """One-step topup split using Sage's /create_transaction endpoint.

    Spends the designated topup pool coin (source_coin_id) and creates
    num_coins output coins of exactly trading_size_mojos each in a single
    atomic transaction.  Change returns to the wallet automatically.
    For fee-paid CAT topups, callers may also pass a dedicated XCH fee coin
    so Sage does not auto-select a fee input.

    Using /create_transaction instead of /send_xch because /send_xch does NOT
    honour the coin_ids hint — Sage's SendXch struct has no such field, so the
    param is silently dropped and Sage picks coins freely (consuming tier_spare
    coins from other tiers rather than the intended topup pool coin).

    /create_transaction's selected_coin_ids IS honoured by Sage (pre-selects
    the coins before running normal selection logic).
    """
    if is_cat:
        asset_id = _get_cat_asset_id()
        if not asset_id:
            print("  [Sage] CAT_ASSET_ID not configured — cannot sage_topup_split CAT")
            return None
        send_id = {"type": "existing", "asset_id": asset_id}
    else:
        send_id = {"type": "xch"}

    actions: list = []

    # N send-to-self actions, each of exactly trading_size_mojos
    for _ in range(num_coins):
        actions.append({
            "type": "send",
            "id": send_id,
            "address": own_address,
            "amount": str(int(trading_size_mojos)),
            "memos": [],
        })

    # Explicit fee action (if any)
    if fee_mojos > 0:
        actions.append({
            "type": "fee",
            "amount": str(int(fee_mojos)),
        })

    selected_coin_ids = [source_coin_id]
    if fee_mojos > 0 and fee_coin_id:
        selected_coin_ids.append(fee_coin_id)

    return create_transaction_rpc(
        selected_coin_ids=selected_coin_ids,
        actions=actions,
        auto_submit=True,
    )


def combine_coins(coin_ids: list, fee_mojos: int = 0) -> Optional[Dict]:
    """Combine multiple coins into one using Sage's native /combine endpoint.

    IMPORTANT: Sage uses a single generic /combine endpoint (not combine_xch/combine_cat).
    It works for both XCH and CAT coins — Sage determines the type from the coin IDs.

    Source: sage-api/src/requests/transactions.rs (struct Combine)
    Parameters:
      - coin_ids: Vec<String>   — which coins to combine
      - fee: Amount (string)    — transaction fee in mojos

    Returns None if wallet is watch-only (no signing capability).
      - auto_submit: bool       — whether to submit immediately (default false)
    """
    if not _require_signing_capability():
        return None

    # Strip 0x prefixes
    bare_ids = [cid.replace("0x", "") for cid in coin_ids]

    payload = {
        "coin_ids": bare_ids,
        "fee": str(int(fee_mojos)),
        "auto_submit": True,
    }

    print(f"   [Sage] Combining {len(bare_ids)} coins via /combine")
    result = rpc("combine", payload, timeout=120)
    if WALLET_DEBUG:
        print(f"  [Sage] combine result: {result}")
    return _submit_coin_spends_if_needed(result, "combine")


def get_transaction(transaction_id: str, timeout: int = 10) -> Optional[Dict]:
    """Get transaction status by ID.

    Sage may not support get_transaction directly. Returns None
    to signal that coin-count polling should be used as fallback.
    """
    result = rpc("get_transaction", {
        "transaction_id": transaction_id,
    }, timeout=timeout)

    if result and result.get("success"):
        return result.get("transaction") or result.get("transaction_record") or result
    return None


def split_coins_bulk(wallet_id: int, num_coins: int, coin_size_mojos: int,
                     fee_mojos: int = 0, reserve_multiplier: float = 2.0,
                     is_cat: bool = False, cat_decimals: int = 3) -> Optional[Dict]:
    """Split wallet balance using Sage's native split endpoints.

    Strategy matches wallet_chia.py:
    1. Find largest spendable coin
    2. Use Sage's split_xch or split_cat
    3. Remainder stays as reserve
    """
    print(f"💰 [Sage] Smart coin splitting for wallet {wallet_id}...")

    # Get spendable coins
    coins_result = get_spendable_coins_rpc(wallet_id)

    if not coins_result or not coins_result.get("success"):
        return {"success": False, "error": "Failed to get spendable coins"}

    coin_records = coins_result.get("confirmed_records", [])

    if not coin_records:
        return {"success": False, "error": "No spendable coins found"}

    # Filter to unspent only
    unspent_records = [r for r in coin_records if r.get("spent_block_index", 0) == 0]

    if not unspent_records:
        return {"success": False, "error": "All coins pending spend"}

    print(f"   📊 Found {len(unspent_records)} unspent coins")

    # Find largest coin
    largest_coin = max(unspent_records, key=lambda c: c["coin"]["amount"])
    coin_amount = largest_coin["coin"]["amount"]

    # Get coin ID — Sage stores it directly in the record
    coin_id = largest_coin.get("coin_id", "")

    # If no coin_id in record, calculate it (same as Chia method)
    if not coin_id:
        parent = largest_coin["coin"]["parent_coin_info"]
        puzzle = largest_coin["coin"]["puzzle_hash"]
        amount = largest_coin["coin"]["amount"]

        if not parent.startswith("0x"):
            parent = "0x" + parent
        if not puzzle.startswith("0x"):
            puzzle = "0x" + puzzle

        parent_bytes = bytes.fromhex(parent.replace("0x", ""))
        puzzle_bytes = bytes.fromhex(puzzle.replace("0x", ""))
        # Chia uses variable-length signed encoding, NOT fixed 8 bytes.
        # Using fixed 8 bytes produces the WRONG coin_id for small amounts.
        if amount > 0:
            byte_count = (amount.bit_length() + 8) >> 3
            amount_bytes = amount.to_bytes(byte_count, 'big', signed=True)
        else:
            amount_bytes = b'\x00'
        coin_id = "0x" + hashlib.sha256(parent_bytes + puzzle_bytes + amount_bytes).hexdigest()

    if is_cat:
        token_amount = coin_size_mojos
        coin_scale = 10 ** cat_decimals
        coin_amount_tokens = coin_amount / coin_scale
        needed = num_coins * token_amount

        print(f"   📊 Largest CAT coin: {coin_amount_tokens:.2f} tokens")
        print(f"   🎯 Target: {num_coins} coins × {token_amount:.2f} tokens")

        if coin_amount_tokens < needed:
            return {
                "success": False,
                "error": f"Insufficient balance: have {coin_amount_tokens:.2f}, need {needed:.2f}"
            }
    else:
        coin_amount_xch = coin_amount / 1e12
        total_needed_mojos = num_coins * coin_size_mojos
        total_needed_xch = total_needed_mojos / 1e12

        print(f"   📊 Largest XCH coin: {coin_amount_xch:.4f} XCH ({coin_amount} mojos)")
        print(f"   🎯 Target: {num_coins} coins × {coin_size_mojos / 1e12:.4f} XCH")

        if coin_amount < total_needed_mojos:
            return {
                "success": False,
                "error": f"Insufficient balance: have {coin_amount_xch:.4f} XCH, need {total_needed_xch:.4f} XCH"
            }

    print(f"   🎲 [Sage] Splitting coin {coin_id[:18]}...")

    result = split_coins_rpc(
        wallet_id=wallet_id,
        target_coin_id=coin_id,
        num_coins=num_coins,
        amount_per_coin=int(coin_size_mojos) if not is_cat else int(coin_size_mojos * (10 ** cat_decimals)),
        fee_mojos=fee_mojos,
        is_cat=is_cat
    )

    if result and result.get("success"):
        print("   ✅ [Sage] Split transaction submitted!")
        return {
            "success": True,
            "coins_created": num_coins,
            "transaction_id": result.get("transaction_id"),
        }
    else:
        error = result.get("error", "Unknown error") if result else "RPC call failed"
        print(f"   ❌ [Sage] split failed: {error}")
        return {"success": False, "error": error}


def wait_for_coin_confirmations(wallet_id: int, target_coin_size_mojos: int,
                                 target_count: int, tolerance: float = 0.25,
                                 max_wait_seconds: int = 300,
                                 poll_interval: int = 10,
                                 progress_callback=None) -> bool:
    """Wait for coins to confirm after splitting."""
    start_time = time.time()

    while (time.time() - start_time) < max_wait_seconds:
        confirmed = count_suitable_coins(wallet_id, target_coin_size_mojos, tolerance)

        if progress_callback:
            progress_callback(confirmed, target_count)

        if confirmed >= target_count:
            return True

        time.sleep(poll_interval)

    return False


# ============================================================================
# BALANCE & ADDRESS
# ============================================================================

def get_wallet_balance(wallet_id: int):
    """Get wallet balance — translated for Sage.

    Sage doesn't have a direct get_wallet_balance endpoint.
    We use get_sync_status for XCH (has balance + selectable_balance),
    and get_coins for CATs (two calls: one for all coins, one for selectable).

    Returns confirmed_wallet_balance = total (including locked in offers),
    spendable_balance = only free/selectable coins.
    """
    if _is_cat_wallet(wallet_id):
        asset_id = _resolve_asset_id(wallet_id)
        if not asset_id:
            return None

        # CAT balance: "selectable" = spendable, "owned" = total (free + locked)
        # Valid Sage filter_mode values: all, selectable, owned, spent, clawback
        # "all" includes spent coins and hits 500-limit. "owned" = currently held only.
        # IMPORTANT: reject structured error results — don't silently turn them into zero balance.
        sel_result = rpc("get_coins", {
            "asset_id": asset_id,
            "offset": 0, "limit": 500,
            "filter_mode": "selectable",
        }, timeout=10)
        if not _rpc_succeeded(sel_result):
            return {"success": False, "error": f"CAT selectable balance query failed for wallet {wallet_id}: {sel_result}"}
        sel_coins = sel_result.get("coins") or sel_result.get("records") or sel_result.get("data") or []
        spendable = sum(int(c.get("amount", "0")) for c in sel_coins)

        owned_result = rpc("get_coins", {
            "asset_id": asset_id,
            "offset": 0, "limit": 500,
            "filter_mode": "owned",
        }, timeout=10)
        if not _rpc_succeeded(owned_result):
            return {"success": False, "error": f"CAT owned balance query failed for wallet {wallet_id}: {owned_result}"}
        owned_coins = owned_result.get("coins") or owned_result.get("records") or owned_result.get("data") or []
        total = sum(int(c.get("amount", "0")) for c in owned_coins)

        if not hasattr(get_wallet_balance, '_cat_diag_logged'):
            get_wallet_balance._cat_diag_logged = True
            print(f"  [Sage] CAT balance for {asset_id[:12]}...: "
                  f"selectable={spendable} ({len(sel_coins)} coins), "
                  f"owned={total} ({len(owned_coins)} coins)", flush=True)

        if total < spendable:
            total = spendable

        return {
            "success": True,
            "wallet_balance": {
                "confirmed_wallet_balance": total,
                "spendable_balance": spendable,
                "unconfirmed_wallet_balance": total,
                "pending_coin_removal_count": 0,
                "wallet_id": wallet_id,
            }
        }
    else:
        # XCH balance: Sage's get_sync_status only has selectable_balance (no total).
        # Valid Sage filter_mode values: all, selectable, owned, spent, clawback
        # "owned" = currently held coins (free + offer-locked, not spent)
        # "selectable" = spendable only (free, not locked in offers)
        # Total = owned sum, Spendable = selectable sum (or selectable_balance from sync)

        # Step 1: get selectable (spendable) from get_sync_status (fastest)
        spendable = 0
        sync = rpc("get_sync_status", {}, timeout=5)
        if _rpc_succeeded(sync):
            sel_val = sync.get("selectable_balance")
            if sel_val is not None:
                spendable = int(sel_val)

        # Step 2: get ALL owned XCH coins (free + offer-locked) for total
        owned_result = rpc("get_coins", {
            "asset_id": None,
            "offset": 0, "limit": 500,
            "filter_mode": "owned",
        }, timeout=10)
        if not _rpc_succeeded(owned_result):
            return {"success": False, "error": f"XCH owned balance query failed: {owned_result}"}
        owned_coins = owned_result.get("coins") or owned_result.get("records") or owned_result.get("data") or []
        total = sum(int(c.get("amount", "0")) for c in owned_coins)

        if not hasattr(get_wallet_balance, '_xch_diag_logged'):
            get_wallet_balance._xch_diag_logged = True
            print(f"  [Sage] XCH balance: selectable_balance={spendable/1e12:.4f}, "
                  f"owned={total/1e12:.4f} ({len(owned_coins)} coins), "
                  f"locked_est={(total - spendable)/1e12:.4f}", flush=True)

        # V5 FIX: Removed the "inflated" warning that fired 1,776+ times.
        # With filter_mode="owned", total CORRECTLY includes offer-locked coins.
        # When you have 100 offers, owned (84.7) >> spendable (1.2) is normal.
        # The old check replaced total with spendable, losing locked coin data.
        # Now we keep total as-is (it's the true confirmed_wallet_balance).
        # Only log a one-time diagnostic if the ratio is extreme.
        if total > spendable * 50 and spendable > 0:
            if not hasattr(get_wallet_balance, '_xch_inflated_warned'):
                get_wallet_balance._xch_inflated_warned = True
                print(f"  [Sage] XCH owned/spendable ratio high: "
                      f"owned={total/1e12:.1f}, spendable={spendable/1e12:.1f} "
                      f"(normal when most coins are locked by offers)", flush=True)

        # Ensure total >= spendable
        if total < spendable:
            total = spendable

        # A genuinely empty wallet is a valid state — return the zero
        # balance instead of None so callers can distinguish "empty" from
        # "RPC failed".  RPC failures already return None earlier in this
        # function (before we computed `total`/`spendable`).

        return {
            "success": True,
            "wallet_balance": {
                "confirmed_wallet_balance": total,
                "spendable_balance": spendable,
                "unconfirmed_wallet_balance": total,
                "pending_coin_removal_count": 0,
                "wallet_id": wallet_id,
            }
        }


def get_balances_parallel(wallet_ids: list = None):
    """Fetch multiple wallet balances in parallel."""
    if wallet_ids is None:
        wallet_ids = [WALLET_ID_XCH]

    results = {}
    with ThreadPoolExecutor(max_workers=len(wallet_ids)) as executor:
        future_to_id = {
            executor.submit(get_wallet_balance, wid): wid
            for wid in wallet_ids
        }
        for future in as_completed(future_to_id):
            wallet_id = future_to_id[future]
            try:
                results[wallet_id] = future.result()
            except Exception as e:
                print(f"❌ [Sage] Failed to get balance for wallet {wallet_id}: {e}")
                results[wallet_id] = None
    return results


def _resolve_asset_id(wallet_id: int) -> Optional[str]:
    """Resolve a wallet_id to a CAT asset_id using the discovery mapping.

    Falls back to the configured CAT_ASSET_ID from .env if the mapping
    hasn't been populated yet (get_wallets() not called).
    """
    global _wallet_id_to_asset_id
    if wallet_id in _wallet_id_to_asset_id:
        resolved = _wallet_id_to_asset_id[wallet_id]
        # Cross-check: warn if resolved asset doesn't match configured
        configured = _get_cat_asset_id()
        if configured:
            r_norm = (resolved or "").lower().replace("0x", "")
            c_norm = configured.lower().replace("0x", "")
            if r_norm != c_norm:
                if not hasattr(_resolve_asset_id, f'_mismatch_warned_{wallet_id}'):
                    setattr(_resolve_asset_id, f'_mismatch_warned_{wallet_id}', True)
                    print(f"⚠️  [Sage] _resolve_asset_id({wallet_id}): "
                          f"mapped={resolved[:16]}... vs configured={configured[:16]}... "
                          f"— MISMATCH! Full mapping: {_wallet_id_to_asset_id}",
                          flush=True)
        return resolved
    # Fallback: use configured asset_id (single-CAT mode)
    return _get_cat_asset_id()


def get_wallets():
    """Discover ALL CAT tokens in the Sage wallet via get_cats RPC.

    Sage's get_cats endpoint returns every CAT the wallet holds, with
    asset_id, name, ticker, balance, etc. We convert these into
    synthetic Chia-format wallet entries and populate the
    _wallet_id_to_asset_id mapping so other functions (get_wallet_balance,
    get_spendable_coins, etc.) can work with any CAT.

    Synthetic wallet_id assignment:
      - XCH is always WALLET_ID_XCH (typically 1)
      - The configured CAT (from .env) gets CAT_WALLET_ID (typically 5)
      - Other discovered CATs get IDs starting from 100, incrementing
    """
    if not ensure_initialized():
        return {"success": False, "wallets": None, "error": "Sage not initialized"}

    global _wallet_id_to_asset_id

    wallets = [
        {"id": WALLET_ID_XCH, "name": "Chia Wallet", "type": 0},
    ]

    # Try Sage's get_cats RPC to discover all CATs in the wallet
    try:
        result = rpc("get_cats", {}, timeout=10)
    except Exception as e:
        _console(f"  [Sage] get_cats RPC failed: {e} -- falling back to .env config")
        result = None

    if result and isinstance(result, dict):
        cats_list = result.get("cats") or result.get("data") or []

        if cats_list:
            configured_asset_id = _get_cat_asset_id()

            # ── DYNAMIC WALLET_ID ASSIGNMENT ─────────────────────────
            # Sage doesn't have wallet IDs — we assign them dynamically
            # based on the configured CAT_ASSET_ID, NOT from .env.
            #
            # The configured CAT (matching CAT_ASSET_ID) ALWAYS gets
            # CONFIGURED_CAT_WID (2). All other CATs get IDs starting
            # at 1000+. This eliminates collision bugs entirely.
            #
            # We then update cfg.CAT_WALLET_ID to match, so all modules
            # use the correct ID regardless of what .env says.
            # ─────────────────────────────────────────────────────────
            CONFIGURED_CAT_WID = 2   # Fixed ID for the active trading CAT
            next_synthetic_id = 1000  # Other CATs — far away, no collision risk

            new_mapping = {}

            # Normalize configured asset_id for comparison
            # Sage may return asset_ids with/without 0x prefix and varying case
            configured_normalized = (configured_asset_id or "").lower().replace("0x", "").strip()
            found_configured = False

            for cat in cats_list:
                asset_id = cat.get("asset_id", "")
                if not asset_id:
                    continue

                name = cat.get("name") or cat.get("ticker") or f"CAT-{asset_id[:8]}"
                ticker = cat.get("ticker") or ""

                # Match by asset_id — the ONLY reliable identifier for Sage
                cat_normalized = asset_id.lower().replace("0x", "").strip()
                if configured_normalized and cat_normalized == configured_normalized:
                    wid = CONFIGURED_CAT_WID
                    found_configured = True
                else:
                    wid = next_synthetic_id
                    next_synthetic_id += 1

                new_mapping[wid] = asset_id

                display_name = f"{name} ({ticker})" if ticker else f"{name} (CAT)"
                wallets.append({
                    "id": wid,
                    "name": display_name,
                    "type": 6,
                    "data": asset_id,
                })

            # Update the global mapping
            _wallet_id_to_asset_id = new_mapping

            # ── CRITICAL: Update cfg.CAT_WALLET_ID dynamically ──────
            # This ensures ALL modules use the correct wallet_id for
            # the configured CAT, regardless of what .env says.
            # This is the proper fix: asset_id drives wallet_id, not
            # a static number in .env that can go stale or collide.
            try:
                from config import cfg as _cfg_instance
                if _cfg_instance and found_configured:
                    old_wid = getattr(_cfg_instance, 'CAT_WALLET_ID', '?')
                    _cfg_instance.CAT_WALLET_ID = CONFIGURED_CAT_WID
                    if old_wid != CONFIGURED_CAT_WID:
                        _console(f"  [Sage] CAT_WALLET_ID updated: {old_wid} -> "
                                 f"{CONFIGURED_CAT_WID} (dynamic from asset_id match)")
            except Exception as e:
                print(f"  [Sage] Could not update cfg.CAT_WALLET_ID: {e}",
                      flush=True)

            if not hasattr(get_wallets, '_discovery_logged'):
                get_wallets._discovery_logged = True
                print(f"  [Sage] Discovered {len(cats_list)} CAT(s) via get_cats RPC: "
                      f"{[c.get('ticker') or c.get('name') or c.get('asset_id', '')[:8] for c in cats_list]}",
                      flush=True)
                # Log the full mapping for debugging wallet_id → asset_id issues
                for wid, aid in new_mapping.items():
                    tag = "TRADING" if wid == CONFIGURED_CAT_WID else "other"
                    _console(f"  [Sage]   wallet_id {wid} -> {aid[:20]}... ({tag})")
                if not found_configured and configured_asset_id:
                    print(f"  🚫 [Sage] CONFIGURED CAT NOT FOUND in wallet! "
                          f"Configured asset: {str(configured_asset_id)[:20]}... "
                          f"Sage has: {[c.get('asset_id','')[:20] for c in cats_list]}",
                          flush=True)

            return {"success": True, "wallets": wallets}

    # Fallback: get_cats didn't work — use single CAT from .env config
    asset_id = _get_cat_asset_id()
    cat_wallet_id = 2  # Same fixed ID as dynamic path
    cat_name = os.getenv("CAT_NAME", "MZ")

    if asset_id:
        _wallet_id_to_asset_id = {cat_wallet_id: asset_id}
        wallets.append({
            "id": cat_wallet_id,
            "name": f"{cat_name} (CAT)",
            "type": 6,
            "data": asset_id,
        })

    return {"success": True, "wallets": wallets}


def get_next_address(wallet_id: int = None, new_address: bool = True):
    """Get next receive address from Sage."""
    ensure_initialized()

    if wallet_id is None:
        wallet_id = WALLET_ID_XCH
    # Sage doesn't have a dedicated get_address endpoint.
    # The receive_address comes from get_sync_status.
    result = rpc("get_sync_status", {}, timeout=5)
    if _rpc_succeeded(result):
        address = result.get("receive_address", "")
        if address:
            return {"success": True, "address": address}
    error_detail = ""
    if isinstance(result, dict):
        error_detail = result.get("error", "")
    print(f"  [Sage] get_next_address failed: {error_detail or 'no response'}")
    return {"success": False, "error": f"RPC failed: {error_detail or 'no response'}"}


def _get_active_address_prefix() -> Optional[str]:
    """Infer the active network prefix from the wallet's current receive address."""
    try:
        addr_result = get_next_address(new_address=False)
        if addr_result and addr_result.get("success"):
            address = str(addr_result.get("address", "")).strip()
            if address.startswith("txch1"):
                return "txch1"
            if address.startswith("xch1"):
                return "xch1"
    except Exception:
        pass
    return None


def _validate_address_for_active_network(address: str, *, context: str) -> Optional[str]:
    """Validate address shape and, where possible, ensure it matches the active network."""
    if not address or not isinstance(address, str):
        print(f"  [Sage] {context}: invalid address (empty or not string)")
        return None

    address = address.strip()
    if not address.startswith("xch1") and not address.startswith("txch1"):
        print(f"  [Sage] {context}: address must start with xch1 or txch1, got: {address[:10]}...")
        return None
    if len(address) < 60:
        print(f"  [Sage] {context}: address too short ({len(address)} chars)")
        return None

    expected_prefix = _get_active_address_prefix()
    if expected_prefix and not address.startswith(expected_prefix):
        print(f"  [Sage] {context}: address prefix does not match active wallet network "
              f"({expected_prefix}), got {address[:4]}...", flush=True)
        return None

    return address


def sign_message_by_address(address: str, message: str) -> dict:
    """Sign a message with the key associated with ``address`` via Sage RPC.

    Used by the Dexie liquidity-rewards claim flow: each claim signs the
    message ``"Claim dexie liquidity rewards for offer <id> (<ts>)"`` with
    the offer's maker address, proving wallet ownership without spending
    any coins.

    Returns ``{"success": True, "public_key": str, "signature": str}`` on
    success, or ``{"success": False, "error": str}`` on failure. Signing
    is blocked on watch-only wallets via ``_require_signing_capability``.
    """
    if not _require_signing_capability():
        return {"success": False, "error": "wallet_watch_only"}

    addr = _validate_address_for_active_network(address, context="sign_message_by_address")
    if not addr:
        return {"success": False, "error": "invalid_address"}
    if not isinstance(message, str) or not message:
        return {"success": False, "error": "invalid_message"}

    try:
        result = rpc("sign_message_by_address", {
            "address": addr,
            "message": message,
        }, timeout=15)
    except Exception as e:
        return {"success": False, "error": f"rpc_exception: {e}"}

    if not _rpc_succeeded(result):
        err = (result or {}).get("error", "rpc_failed") if isinstance(result, dict) else "rpc_failed"
        # Sage's standalone RPC server (sage-rpc) does not register
        # sign_message_by_address — only the Tauri desktop app exposes it
        # via WalletConnect. Surface a specific error so the GUI can show
        # a useful message instead of a generic 404 / rpc_failed.
        err_str = str(err).lower()
        if "404" in err_str or "not found" in err_str or "unknown" in err_str:
            return {"success": False, "error": "signing_not_supported_by_sage_rpc"}
        return {"success": False, "error": str(err)}

    sig = result.get("signature") or ""
    pk = result.get("public_key") or ""
    if not sig or not pk:
        return {"success": False, "error": "missing_signature_or_public_key"}
    return {"success": True, "public_key": pk, "signature": sig}


def set_change_address(change_address: str, fingerprint: int = None) -> dict:
    """Set Sage's persistent change address for the active fingerprint."""
    address = _validate_address_for_active_network(
        change_address, context="set_change_address"
    )
    if not address:
        return {"success": False, "error": "invalid_change_address"}

    try:
        if fingerprint is None:
            key = get_current_key()
            if not key or not key.get("fingerprint"):
                return {"success": False, "error": "no_active_fingerprint"}
            fingerprint = int(key["fingerprint"])
        else:
            fingerprint = int(fingerprint)

        result = rpc("set_change_address", {
            "fingerprint": fingerprint,
            "change_address": address,
        }, timeout=10)
        if result is None:
            return {"success": False, "error": "rpc_failed"}

        return {"success": True, "fingerprint": fingerprint, "address": address}
    except Exception as e:
        print(f"  [Sage] set_change_address error: {e}", flush=True)
        return {"success": False, "error": str(e)}


def send_transaction(wallet_id: int, amount_mojos: int, address: str, fee_mojos: int = 0,
                     source_coin_ids: list = None):
    """Send XCH or CAT transaction via Sage.

    Args:
        wallet_id: Wallet ID (1 = XCH, others = CAT)
        amount_mojos: Amount to send in mojos
        address: Destination address
        fee_mojos: Transaction fee in mojos
        source_coin_ids: Optional list of specific coin IDs to spend from.
            CRITICAL for sequential sends — prevents Sage from consuming
            previously-created coins that we need to keep (e.g. tier pool coins).
            Coin IDs should be bare hex (no 0x prefix).
    """
    if not _require_signing_capability():
        return None

    address = _validate_address_for_active_network(address, context="send_transaction")
    if not address:
        return None

    if _is_cat_wallet(wallet_id):
        asset_id = _resolve_asset_id(wallet_id)
        payload = {
            "asset_id": asset_id,
            "address": str(address),
            "amount": str(int(amount_mojos)),
            "fee": str(int(fee_mojos)),
            "auto_submit": True,  # CRITICAL: without this, Sage plans but never sends
        }
        if source_coin_ids:
            # Strip 0x prefix and pass specific coins to spend
            payload["coin_ids"] = [cid.replace("0x", "") for cid in source_coin_ids]
        return rpc("send_cat", payload, timeout=30)
    else:
        payload = {
            "address": str(address),
            "amount": str(int(amount_mojos)),
            "fee": str(int(fee_mojos)),
            "auto_submit": True,  # CRITICAL: without this, Sage plans but never sends
        }
        if source_coin_ids:
            payload["coin_ids"] = [cid.replace("0x", "") for cid in source_coin_ids]
        return rpc("send_xch", payload, timeout=30)


def send_transaction_multi(payments: list, fee_mojos: int = 0):
    """Send multiple payments — Sage uses multi_send."""
    if not _require_signing_capability():
        return None
    # Convert payments to Sage's bulk format
    sage_payments = []
    for p in payments:
        address = _validate_address_for_active_network(
            p.get("address", p.get("puzzle_hash", "")),
            context="send_transaction_multi"
        )
        if not address:
            return None
        sage_payments.append({
            "address": address,
            "amount": str(p.get("amount", 0)),
        })

    payload = {
        "payments": sage_payments,
        "fee": str(int(fee_mojos)),
        "auto_submit": True,  # CRITICAL: without this, Sage plans but never sends
    }
    return rpc("multi_send", payload, timeout=30)


def send_cat_multi(payments: list, fee_mojos: int = 0):
    """Send CAT to multiple addresses in ONE transaction via Sage's multi_send.

    Uses the SAME multi_send endpoint as XCH, but with asset_id added to each
    payment item. Confirmed working via test_bulk_cat.py (Test 3).
    Creates all output coins atomically — no sequential coin consumption risk.

    Args:
        payments: List of {"address": "xch1...", "amount": <mojos_int>}
        fee_mojos: Transaction fee in mojos
    """
    if not _require_signing_capability():
        return None

    asset_id = _get_cat_asset_id()
    if not asset_id:
        print("  [Sage] CAT_ASSET_ID not configured — cannot multi send CAT")
        return None

    sage_payments = []
    for p in payments:
        address = _validate_address_for_active_network(
            p.get("address", p.get("puzzle_hash", "")),
            context="send_cat_multi"
        )
        if not address:
            return None
        sage_payments.append({
            "address": address,
            "amount": str(p.get("amount", 0)),
            "asset_id": asset_id,  # Per-payment asset_id makes multi_send work for CATs
        })

    payload = {
        "payments": sage_payments,
        "fee": str(int(fee_mojos)),
        "auto_submit": True,  # CRITICAL: without this, Sage plans but never sends
    }
    return rpc("multi_send", payload, timeout=30)


# ============================================================================
# OFFER MANAGEMENT
# ============================================================================

def create_offer(offer_dict: dict, validate_only: bool = False, max_time: int = None,
                  _reuse_puzhash: bool = True,
                  min_coin_amount: int = None, max_coin_amount: int = None,
                  coin_ids: list = None, fee_mojos: int = 0):
    """Create an offer via Sage's make_offer endpoint.

    Sage's make_offer uses offered_assets / requested_assets arrays:
      {
        "offered_assets": [{"asset_id": null, "amount": "50000000000"}],
        "requested_assets": [{"asset_id": "b8edcc...", "amount": "1000"}],
        "fee": "0",
        "expires_at_second": 1709500000,  (optional)
        "auto_import": true
      }

    The offer_dict from our bot uses wallet_id keys with signed amounts:
      {1: -50000000000, 5: 1000}  (negative = offering, positive = requesting)
    We translate: wallet_id 1 → asset_id null (XCH), others → CAT asset_id.
    """
    # ── GUARDRAIL: watch-only wallets cannot create offers ──────
    if not _require_signing_capability():
        return {"success": False, "error": "Watch-only wallet cannot sign offers"}

    # ── GUARDRAIL: validate_only is not supported by Sage ──────
    # The parameter exists for Chia wallet compatibility but Sage's make_offer
    # always creates and submits. Reject explicitly rather than silently ignoring.
    if validate_only:
        print("  [Sage] create_offer: validate_only=True is not supported by Sage adapter")
        return {"success": False, "error": "validate_only not supported by Sage — offers are always submitted"}

    offered_assets = []
    requested_assets = []

    # ── GUARDRAIL: request-only offers require a fee ──────
    # Sage requires a fee for request-only offers (no offered assets).
    # Block them early since we hardcode fee="0".
    has_offered = any(int(v) < 0 for v in offer_dict.values())
    if not has_offered:
        print("  [Sage] create_offer: request-only offers require a fee (currently hardcoded to 0)")
        return {"success": False, "error": "Request-only offers require a fee — not supported"}

    # ── GUARDRAIL: Verify CAT asset_id matches configured token ──────
    # Prevents creating offers in the wrong CAT if the wallet_id → asset_id
    # mapping is stale or was incorrectly populated by get_wallets().
    # This is a hard safety check — the bot must NEVER trade the wrong token.
    configured_cat = _get_cat_asset_id()
    configured_cat_normalized = (configured_cat or "").lower().replace("0x", "").strip()

    for key, amount in offer_dict.items():
        key_int = int(key)
        amount_int = int(amount)

        # Determine asset_id: null for XCH, resolved from mapping for CATs
        if key_int == WALLET_ID_XCH:
            asset_id = None  # XCH has no asset_id in Sage
        else:
            asset_id = _resolve_asset_id(key_int)
            if not asset_id:
                print(f"❌ [Sage] No asset_id for wallet {key_int} — cannot create offer")
                return {"success": False, "error": f"No asset_id for wallet {key_int} — cannot create offer"}

            # CRITICAL SAFETY CHECK: resolved asset_id MUST match configured CAT
            resolved_normalized = asset_id.lower().replace("0x", "").strip()
            if configured_cat_normalized and resolved_normalized != configured_cat_normalized:
                print(f"🚫 [Sage] SAFETY BLOCK: wallet_id {key_int} resolved to "
                      f"asset_id {asset_id[:16]}... but configured CAT is "
                      f"{configured_cat[:16]}... — REFUSING to create offer "
                      f"in wrong token!", flush=True)
                return {"success": False, "error": f"SAFETY BLOCK: wallet {key_int} resolves to wrong token asset_id"}

        if amount_int < 0:
            # Negative = we are offering this asset
            offered_assets.append({
                "asset_id": asset_id,
                "amount": str(abs(amount_int)),
            })
        elif amount_int > 0:
            # Positive = we are requesting this asset
            requested_assets.append({
                "asset_id": asset_id,
                "amount": str(amount_int),
            })

    payload = {
        "offered_assets": offered_assets,
        "requested_assets": requested_assets,
        "fee": str(int(fee_mojos)) if fee_mojos else "0",
        "auto_import": True,
    }

    # Only set expiry if max_time is a positive timestamp.
    # CRITICAL: Sage interprets expires_at_second=0 as "expired at Unix epoch 0"
    # (January 1 1970), NOT "no expiry". The Chia official wallet treats max_time=0
    # as "no expiry", but Sage is literal. So we must omit the field entirely
    # when the intent is "never expires".
    if max_time is not None and int(max_time) > 0:
        payload["expires_at_second"] = int(max_time)

    # Pass specific coin IDs if provided (requires Sage PR #761 / coin_ids feature)
    # IMPORTANT: Strip 0x prefix — Sage expects bare hex (like split/combine endpoints)
    if coin_ids:
        bare_ids = [cid.replace("0x", "") for cid in coin_ids]
        payload["coin_ids"] = bare_ids
        if WALLET_DEBUG:
            print(f"  [Sage] Using specific coin_ids: {bare_ids}")

    if WALLET_DEBUG:
        print(f"  [Sage] make_offer payload: offered={offered_assets}, requested={requested_assets}")

    result = rpc("make_offer", payload, timeout=15)

    if result and isinstance(result, dict):
        # ALWAYS log response keys so we can debug format issues
        # (This was the cause of the "offers created but not tracked" bug)
        result_keys = list(result.keys())
        print(f"  [Sage] make_offer response keys: {result_keys}", flush=True)

        # Normalize response to match Chia format expected by offer_manager.
        # Chia returns: {"success": true, "offer": "offer1...", "trade_record": {"trade_id": "..."}}
        # Sage returns: varies by version — try multiple possible key names.
        # We need to ensure both "trade_id" and "trade_record" exist for compatibility.

        # --- Extract offer_id: try multiple possible key names ---
        offer_id = (result.get("offer_id")
                    or result.get("id")
                    or result.get("trade_id")
                    or result.get("offerId")
                    or "")

        # If still empty, check nested structures
        if not offer_id:
            # Check trade_record.trade_id (Chia format)
            tr = result.get("trade_record")
            if isinstance(tr, dict):
                offer_id = tr.get("trade_id") or tr.get("offer_id") or ""
            # Check offer object if it's a dict (some Sage versions nest it)
            offer_obj = result.get("offer")
            if isinstance(offer_obj, dict):
                offer_id = offer_obj.get("id") or offer_obj.get("offer_id") or ""

        if offer_id:
            result["trade_id"] = offer_id
            result["trade_record"] = result.get("trade_record", {})
            if not isinstance(result["trade_record"], dict):
                result["trade_record"] = {}
            result["trade_record"]["trade_id"] = offer_id
            print(f"  [Sage] ✅ trade_id extracted: {offer_id[:16]}...", flush=True)
        else:
            # CRITICAL: offer was created but we can't track it!
            print("  ⚠️  [Sage] make_offer succeeded but NO offer_id found!", flush=True)
            print(f"  ⚠️  [Sage] Response (first 500 chars): {str(result)[:500]}", flush=True)

        # Ensure success flag is set — but only if no error field is present.
        # A response like {"offer": "...", "error": "wallet locked"} should NOT
        # be normalized to success=True.
        has_error = bool(result.get("error"))
        if not has_error:
            if "offer" in result and "success" not in result:
                result["success"] = True
            # Also set success if we have an offer_id (offer was clearly created)
            if offer_id and "success" not in result:
                result["success"] = True

        return result

    # Log if we got None/empty back
    print(f"  ❌ [Sage] make_offer returned: {result}", flush=True)
    if result is None:
        return {"success": False, "error": "make_offer RPC returned None (network or Sage error)"}
    return result


def cancel_offer(trade_id: str, secure: bool = True, timeout: int = 60,
                 fee_mojos: Optional[int] = None):
    """Cancel an offer via Sage.

    Sage's cancel_offer takes offer_id and optional fee.
    The 'secure' flag maps to whether we pay a fee for on-chain cancel.

    NOTE: Sage's CancelOffer struct does NOT accept coin_ids — fee coin
    is always auto-selected.  Fee contention between cancels is handled
    by using bulk cancel (single tx) and sequencing cancels before creates
    in the bot loop.  Creates DO get dedicated fee coins via make_offer's
    coin_ids parameter.

    Returns dict with 'success' key, or error dict on failure.
    """
    if not _require_signing_capability():
        return {"success": False, "error": "Watch-only wallet cannot cancel offers"}

    resolved_fee = 0 if not secure else (
        max(0, int(fee_mojos))
        if fee_mojos is not None
        else get_effective_transaction_fee_mojos()
    )

    payload = {
        "offer_id": trade_id,  # Sage uses offer_id, not trade_id
        "fee": str(int(resolved_fee)),  # REQUIRED — Sage 422s without fee field
        "auto_submit": True,   # Submit the cancel transaction immediately
    }

    # Use _sage_post directly so we can see the actual HTTP status + body
    # on failure (rpc() swallows the error details)
    try:
        result = _sage_post("cancel_offer", payload, timeout=timeout)

        if WALLET_DEBUG:
            print(f"   [Sage] cancel_offer {trade_id[:16]}... → {str(result)[:200]}")

        if result is None:
            return {"success": False, "error": f"Cancel RPC returned None for {trade_id[:16]}..."}

        # Sage may not include 'success' key — add it if missing, but only
        # when there are no failure indicators (error key or failed status)
        if isinstance(result, dict) and "success" not in result:
            if ("error" in result or "reason" in result
                    or result.get("status") in ("failed", "error", "rejected")):
                result["success"] = False
            else:
                result["success"] = True

        return result

    except SageAlreadyIncluding as e:
        print(f"   [Sage] cancel_offer {trade_id[:16]}... already in mempool")
        return {"success": True, "method": "already_in_mempool",
                "note": f"Sage cancel already in mempool: {str(e)[:160]}"}

    except SageMempoolConflict as e:
        print(f"   [Sage] cancel_offer {trade_id[:16]}... cancel conflict in flight")
        return {"success": True, "method": "mempool_conflict_inflight",
                "note": f"Sage cancel conflict appears in-flight: {str(e)[:160]}"}

    except ConnectionError as e:
        err_str = str(e)
        # Sage may return non-200 but still process the cancel.
        # Log the actual status for debugging, but DON'T assume failure yet —
        # the confirmation poll in cancel_offers_batch will verify.
        print(f"   [Sage] cancel_offer {trade_id[:16]}... HTTP error: {err_str[:200]}")

        # Sage returns 404 "Missing offer" when the offer is no longer in
        # the wallet. That can mean CANCELLED, FILLED, or EXPIRED — we do
        # not yet know which. Earlier versions returned success=True here,
        # which caused the bot to confidently write `status=cancelled` in
        # the DB even when the offer had actually filled. We now return a
        # distinct "already_gone_ambiguous" method so the caller leaves
        # DB status open and lets fill_tracker / bot_health decide the
        # final state from on-chain evidence.
        if "404" in err_str or "Missing offer" in err_str or "not found" in err_str.lower():
            print(f"   [Sage] cancel_offer {trade_id[:16]}... → offer already gone (404), deferring to on-chain verification")
            return {"success": True, "already_gone": True,
                    "method": "already_gone_ambiguous",
                    "note": "Sage 404 — offer gone, fill_tracker / bot_health will verify"}

        # HTTP 500/202 are NOT success — don't promote them.
        # The retry mechanism in cancel_offers will handle re-attempts.
        # Previously these were treated as success which masked real failures.
        if "HTTP 500" in err_str or "HTTP 202" in err_str:
            print(f"   [Sage] cancel_offer {trade_id[:16]}... got {err_str[:50]} — not treating as success")
            return {"success": False, "uncertain": True,
                    "error": f"Sage returned non-200: {err_str[:100]}"}

        return {"success": False, "error": err_str[:200]}

    except Exception as e:
        err_str = str(e)
        # V5 FIX: Also catch 404/Missing offer from non-ConnectionError exceptions
        if "404" in err_str or "Missing offer" in err_str or "not found" in err_str.lower():
            print(f"   [Sage] cancel_offer {trade_id[:16]}... → offer already gone (404), treating as success")
            return {"success": True, "already_gone": True,
                    "note": "Sage 404 — offer already gone"}
        if not _quiet_mode:
            print(f"   [Sage] cancel_offer {trade_id[:16]}... error: {e}")
        return {"success": False, "error": err_str[:200]}


def is_offer_time_expired(offer: dict) -> bool:
    """Check if an offer has expired based on its max_time field.

    This is pure logic — no RPC needed. Works the same for both
    Chia and Sage since it operates on the offer dict.
    """
    # Check Chia-style valid_times
    valid_times = offer.get("valid_times") or {}
    max_time = valid_times.get("max_time", 0)

    # Also check Sage-style top-level max_time
    if not max_time:
        max_time = offer.get("max_time", 0)

    if max_time and max_time > 0:
        return int(time.time()) > max_time
    return False


def get_offer_expiry_info(offer: dict) -> dict:
    """Get expiry timing info for an offer.

    Pure logic — same as Chia wallet version.
    """
    valid_times = offer.get("valid_times") or {}
    max_time = valid_times.get("max_time", 0)

    # Also check Sage-style
    if not max_time:
        max_time = offer.get("max_time", 0)

    now = int(time.time())

    if not max_time or max_time <= 0:
        return {"max_time": 0, "expired": False, "seconds_remaining": float('inf')}

    return {
        "max_time": max_time,
        "expired": now > max_time,
        "seconds_remaining": max_time - now,
    }


def cleanup_expired_offers(log_fn=None) -> int:
    """Cancel any offers whose max_time has passed.

    Same logic as Chia version — works on the offer dicts.
    """
    def _log(level, msg):
        if log_fn:
            log_fn(level, msg)

    offers = get_all_offers(include_completed=False, start=0, end=200)
    if not offers:
        return 0

    now = int(time.time())
    cancelled = 0
    expired_found = 0

    for offer in offers:
        if not isinstance(offer, dict):
            continue

        if is_offer_time_expired(offer):
            expired_found += 1
            trade_id = offer.get("trade_id", "")
            valid_times = offer.get("valid_times") or {}
            max_time = valid_times.get("max_time", 0) or offer.get("max_time", 0)
            expired_ago = now - max_time if max_time else 0
            trade_id_short = str(trade_id)[:16]

            _log("info", f"  Cancelling expired offer {trade_id_short}... "
                         f"(expired {expired_ago}s / {expired_ago // 60}m ago)")

            result = cancel_offer(str(trade_id), secure=False)
            if result and result.get("success"):
                cancelled += 1
            else:
                _log("warning", f"  Failed to cancel {trade_id_short}: {result}")

            time.sleep(0.3)

    if expired_found > 0:
        _log("success" if cancelled > 0 else "warning",
             f"Expired offer cleanup: found {expired_found}, "
             f"cancelled {cancelled}")

    return cancelled


def get_all_offers(include_completed: bool = True, start: int = 0, end: int = 50):
    """Get all offers from Sage.

    Sage uses 'get_offers' endpoint (not 'get_all_offers').
    Response format may differ — we normalize to Chia's format.
    """
    payload = {
        "include_completed": include_completed,
        "start": start,
        "end": end,
    }
    res = rpc("get_offers", payload, timeout=8)

    # IMPORTANT: Sage RPC wrappers may return a structured error dict on timeout/
    # transport failure. That must not be interpreted as "zero open offers",
    # or the bot can falsely conclude the entire book vanished and start
    # rebuilding on top of still-live offers.
    get_all_offers._last_error = ""
    if not res:
        get_all_offers._last_error = "get_offers returned None/empty"
        print("  [Sage] get_offers returned None/empty!", flush=True)
        return None
    if isinstance(res, dict) and res.get("success") is False and res.get("error"):
        get_all_offers._last_error = str(res.get("error") or "wallet get_offers failed")
        print(f"  ⚠️  [Sage] get_offers failed: {get_all_offers._last_error}", flush=True)
        return None

    # Handle Sage's response format
    # Sage may return {"offers": [...]} or {"trades": [...]}
    offers_list = res.get("offers")
    if offers_list is None:
        offers_list = res.get("trades")
    if offers_list is None:
        offers_list = res.get("trade_records")
    if offers_list is None:
        offers_list = []

    # Log format details on first call AND on first call that has offers
    # (first call may return 0 offers before any are created)
    _call_count = getattr(get_all_offers, '_call_count', 0) + 1
    get_all_offers._call_count = _call_count
    _should_log = (not hasattr(get_all_offers, '_format_logged') or
                   (not hasattr(get_all_offers, '_offers_logged') and len(offers_list) > 0))
    if _should_log:
        get_all_offers._format_logged = True
        if len(offers_list) > 0:
            get_all_offers._offers_logged = True
        print(f"  [Sage] get_offers response keys: {list(res.keys())} "
              f"(call #{_call_count}, include_completed={include_completed})", flush=True)
        print(f"  [Sage] get_offers found {len(offers_list)} raw offers", flush=True)
        if offers_list and isinstance(offers_list[0], dict):
            first = offers_list[0]
            print(f"  [Sage] First offer keys: {list(first.keys())}", flush=True)
            print(f"  [Sage] First offer status: {repr(first.get('status'))}, "
                  f"trade_id/offer_id: {(first.get('trade_id') or first.get('offer_id', '?'))[:16]}...", flush=True)
            # Log status distribution across all offers
            status_counts = {}
            for o in offers_list[:200]:
                s = repr(o.get('status', 'MISSING'))
                status_counts[s] = status_counts.get(s, 0) + 1
            print(f"  [Sage] Status distribution: {status_counts}", flush=True)
            raw_summary = first.get('summary')
            if raw_summary and isinstance(raw_summary, dict):
                print(f"  [Sage] First offer summary keys: {list(raw_summary.keys())}", flush=True)
                import json
                print(f"  [Sage] First offer summary: {json.dumps(raw_summary, default=str)[:300]}", flush=True)
            else:
                print(f"  [Sage] First offer summary: {raw_summary}", flush=True)

    if not isinstance(offers_list, list):
        return []

    # Normalize each offer to ensure Chia-compatible fields exist
    normalized = []
    for offer in offers_list:
        if not isinstance(offer, dict):
            continue

        # --- Sage → Chia field mapping ---

        # 1. offer_id → trade_id (Sage uses offer_id, Chia uses trade_id)
        if "trade_id" not in offer and "offer_id" in offer:
            offer["trade_id"] = offer["offer_id"]

        # 2. Ensure valid_times exists (Sage may use different key)
        # Sage uses "expires_at_second" instead of Chia's "max_time"
        if "valid_times" not in offer:
            max_t = (offer.get("expires_at_second", 0) or
                     offer.get("max_time", 0) or 0)
            if max_t and int(max_t) > 0:
                offer["valid_times"] = {"max_time": int(max_t)}
            else:
                offer["valid_times"] = {}

        # 3. Normalize summary: Sage uses maker/taker arrays with nested
        #    asset objects; Chia uses offered/requested dicts keyed by
        #    "xch" or asset_id with integer amounts.
        summary = offer.get("summary")
        if summary and isinstance(summary, dict):
            if "maker" in summary or "taker" in summary:
                offer["summary"] = _normalize_sage_summary(summary)
            elif "offered" not in summary and "requested" not in summary:
                offer["summary"] = _build_offer_summary(offer)
        elif not summary:
            offer["summary"] = _build_offer_summary(offer)

        normalized.append(offer)

    # Log first normalized offer's summary once to verify conversion worked
    if not hasattr(get_all_offers, '_norm_logged'):
        get_all_offers._norm_logged = True
        if normalized and isinstance(normalized[0], dict):
            ns = normalized[0].get("summary", {})
            print(f"  [Sage] After normalization — first offer summary: "
                  f"offered={list(ns.get('offered', {}).keys())} "
                  f"requested={list(ns.get('requested', {}).keys())}", flush=True)

    # CLIENT-SIDE SAFETY FILTER: Sage may ignore include_completed=False
    # and return all offers (expired, cancelled, etc.) regardless.
    # If that happens and the count keeps growing, open offers could get
    # pushed out of the end=500 window — the exact truncation bug from V1.
    # Fix: filter completed offers ourselves when include_completed=False.
    #
    # IMPORTANT: PENDING_CANCEL (int 2) is included here because it is
    # STILL fillable — the cancel TX is in the mempool but a counterparty
    # can still take the offer until that TX confirms on-chain. The
    # cancel_offers_batch success check uses _is_still_fillable() to
    # count remaining take-able offers; if we stripped PENDING_CANCEL
    # upstream, that counter would fire "open_remaining == 0" prematurely
    # and declare a batch cancel successful while fills could still land,
    # letting adverse fills stack into a fast move.
    if not include_completed:
        before_count = len(normalized)
        FILLABLE_STATUS_STRINGS = {"PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING",
                                   "PENDING_CANCEL", "IN_PROGRESS", "OPEN", "ACTIVE"}
        filtered = []
        for offer in normalized:
            status_val = offer.get("status")
            if status_val is None:
                filtered.append(offer)  # keep unknowns for classification to handle
                continue
            if isinstance(status_val, int):
                if status_val <= 2:  # PENDING_ACCEPT, PENDING_CONFIRM, PENDING_CANCEL
                    filtered.append(offer)
            else:
                if str(status_val).upper() in FILLABLE_STATUS_STRINGS:
                    filtered.append(offer)

        if before_count != len(filtered):
            if not hasattr(get_all_offers, '_filter_logged'):
                get_all_offers._filter_logged = True
                print(f"  [Sage] Client-side filter: {before_count} raw → "
                      f"{len(filtered)} fillable (Sage ignored include_completed=False)",
                      flush=True)
        normalized = filtered

    return normalized


def _normalize_sage_summary(sage_summary: dict) -> dict:
    """Convert Sage's maker/taker summary format to Chia's offered/requested.

    Sage format:
        {"maker": [{"asset": {"asset_id": "abc..." or null}, "amount": 1000}],
         "taker": [{"asset": {"asset_id": null}, "amount": 500}],
         "fee": 0}

    Chia format:
        {"offered": {"xch": 500, "abc...": 1000},
         "requested": {"xch": 500, "abc...": 1000}}

    In Sage: maker = what we're offering, taker = what we want back.
    """
    offered = {}
    requested = {}

    for item in sage_summary.get("maker", []):
        asset_info = item.get("asset", {})
        asset_id = asset_info.get("asset_id")
        amount = item.get("amount", 0)
        key = "xch" if asset_id is None else asset_id
        offered[key] = amount

    for item in sage_summary.get("taker", []):
        asset_info = item.get("asset", {})
        asset_id = asset_info.get("asset_id")
        amount = item.get("amount", 0)
        key = "xch" if asset_id is None else asset_id
        requested[key] = amount

    return {"offered": offered, "requested": requested}


def _build_offer_summary(offer: dict) -> dict:
    """Build a Chia-compatible offer summary from Sage's offer format.

    Sage may structure offer data differently. This function creates
    the expected {"offered": {"xch": amount, asset_id: amount}, ...}
    format that classify_offers_from_list() expects.
    """
    summary = {"offered": {}, "requested": {}}

    # Try to extract from Sage's format — try multiple possible field names
    offered = (offer.get("offered_assets") or offer.get("offered")
               or offer.get("offer_assets") or offer.get("offering") or {})
    requested = (offer.get("requested_assets") or offer.get("requested")
                 or offer.get("request_assets") or offer.get("requesting") or {})

    # If Sage uses a nested "summary" or "offer" structure, try that too
    if not offered and not requested:
        inner = offer.get("offer") or offer.get("trade") or {}
        if isinstance(inner, dict):
            offered = inner.get("offered") or inner.get("offered_assets") or {}
            requested = inner.get("requested") or inner.get("requested_assets") or {}

    # Log first unknown format for debugging
    if not offered and not requested and WALLET_DEBUG:
        print(f"   ⚠️  [Sage] Could not parse offer summary. Keys: {list(offer.keys())}")

    # Normalize keys to lowercase. Sage may use full asset IDs as keys,
    # while Chia's classify_offers_from_list expects lowercase "xch".
    for key, value in offered.items():
        amount = int(value) if isinstance(value, str) else value
        normalized_key = key.lower() if len(key) < 10 else key  # Keep asset_ids as-is
        summary["offered"][normalized_key] = amount

    for key, value in requested.items():
        amount = int(value) if isinstance(value, str) else value
        normalized_key = key.lower() if len(key) < 10 else key
        summary["requested"][normalized_key] = amount

    return summary


def get_offer_bech32(trade_id: str) -> str:
    """Get the bech32 offer string for a specific trade_id.

    Sage's get_offer endpoint should return the full offer data.
    """
    # Sage uses offer_id, not trade_id
    res = rpc("get_offer", {
        "offer_id": trade_id,
        "file_contents": True
    }, timeout=10)

    if not res:
        return None

    # Check standard locations for the offer string
    offer_str = res.get("offer")
    if offer_str and isinstance(offer_str, str) and offer_str.startswith("offer1"):
        return offer_str

    trade_record = res.get("trade_record") or {}
    offer_str = trade_record.get("offer")
    if offer_str and isinstance(offer_str, str) and offer_str.startswith("offer1"):
        return offer_str

    return None


def _is_still_fillable(status_val, offer_record=None) -> bool:
    """Return True if the offer can still be taken by a counterparty.

    Narrower than _is_open_status: a PENDING_CANCEL offer has its cancel
    TX in the mempool but the chain hasn't confirmed yet, so it IS still
    fillable until the cancel lands. Use this variant when deciding
    whether a cancel batch has actually removed the offer from the
    take-able book (cancel_offers_batch success detection).
    """
    if offer_record and is_offer_time_expired(offer_record):
        return False
    if status_val is None:
        return False
    if isinstance(status_val, int):
        # 0=PENDING_ACCEPT, 1=PENDING_CONFIRM, 2=PENDING_CANCEL all
        # remain fillable — the offer is still on chain/mempool until
        # the cancel TX confirms.
        return status_val <= 2
    status = str(status_val).upper()
    FILLABLE = {"PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING",
                "PENDING_CANCEL", "IN_PROGRESS", "OPEN", "ACTIVE"}
    return status in FILLABLE


def _is_open_status(status_val, offer_record=None) -> bool:
    """Determine if an offer status represents an open/active offer.

    Chia TradeStatus integer enum:
        0 = PENDING_ACCEPT  (open)
        1 = PENDING_CONFIRM (open)
        2 = PENDING_CANCEL  (transitioning — treat as closed)
        3 = CANCELLED       (closed)
        4 = CONFIRMED       (closed)
        5 = FAILED          (closed)
    """
    if offer_record and is_offer_time_expired(offer_record):
        return False

    if status_val is None:
        return False
    if isinstance(status_val, int):
        # Only 0 (PENDING_ACCEPT) and 1 (PENDING_CONFIRM) are truly open
        return status_val <= 1

    status = str(status_val).upper()
    OPEN_STATUSES = {"PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING", "IN_PROGRESS", "OPEN", "ACTIVE"}
    CLOSED_STATUSES = {"PENDING_CANCEL", "CANCELLED", "CANCELED", "CONFIRMED", "FAILED",
                       "EXPIRED", "COMPLETED", "SUCCESS"}

    if status in CLOSED_STATUSES:
        return False
    if status in OPEN_STATUSES:
        return True

    # Unknown status — log it once so we can add it to the right set
    if not hasattr(_is_open_status, '_unknown_logged'):
        _is_open_status._unknown_logged = set()
    if status not in _is_open_status._unknown_logged:
        _is_open_status._unknown_logged.add(status)
        print(f"  ⚠️  [Sage] Unknown offer status: {repr(status_val)} "
              f"(uppercased: {status}) — treating as CLOSED. "
              f"Add to OPEN_STATUSES or CLOSED_STATUSES in _is_open_status().",
              flush=True)
    return False


def classify_offers_from_list(offers_list: list, asset_id_mz: str):
    """Classify offers from a pre-fetched list.

    Same logic as Chia version — operates on normalized offer dicts.
    """
    open_buy = []
    open_sell = []
    closed_offers = []

    _first_classify = not hasattr(classify_offers_from_list, '_logged')
    if _first_classify:
        classify_offers_from_list._logged = True
        print(f"  [classify] Starting classification of {len(offers_list)} offers for asset {asset_id_mz[:12]}...", flush=True)
    skipped_status = 0
    skipped_pair = 0
    for i, tr in enumerate(offers_list):
        if not isinstance(tr, dict):
            continue

        status_val = tr.get("status")
        summary = tr.get("summary") or {}
        offered = summary.get("offered") or {}
        requested = summary.get("requested") or {}

        is_open = _is_open_status(status_val, offer_record=tr)

        is_buy = "xch" in offered and asset_id_mz in requested
        is_sell = asset_id_mz in offered and "xch" in requested

        # Debug: log first few offers on first call only
        if _first_classify and i < 3:
            print(f"  [classify] offer #{i}: status={status_val} is_open={is_open} "
                  f"offered_keys={list(offered.keys())[:3]} requested_keys={list(requested.keys())[:3]} "
                  f"is_buy={is_buy} is_sell={is_sell}", flush=True)

        if is_open:
            if is_buy:
                open_buy.append(tr)
            elif is_sell:
                open_sell.append(tr)
            else:
                skipped_pair += 1
        else:
            if is_buy or is_sell:
                closed_offers.append(tr)
            else:
                skipped_status += 1

    if _first_classify:
        print(f"  [classify] Result: {len(open_buy)} buys, {len(open_sell)} sells, "
              f"{len(closed_offers)} closed, {skipped_status} wrong status, {skipped_pair} wrong pair",
              flush=True)
    return open_buy, open_sell, closed_offers


def classify_open_offers_for_pair(asset_id_mz: str):
    """LEGACY: Keep for backwards compatibility."""
    offers_list = get_all_offers(include_completed=True)
    if offers_list is None:
        print("⚠️  [Sage] Could not fetch offers from Sage RPC.")
        return [], []

    open_buy, open_sell, _ = classify_offers_from_list(offers_list, asset_id_mz)
    return open_buy, open_sell


def _normalize_offer_lock_id(offer_id: Any) -> Optional[str]:
    """Normalize offer/trade ids for lock matching.

    Sage coin records may report offer_id/offer_hash with or without a 0x
    prefix, while the app may track trade ids in either form.
    """
    if not isinstance(offer_id, str):
        return None
    normalized = offer_id.strip().lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    return normalized or None


def _get_still_locked_trade_ids(trade_ids: set, owned_coin_map: Optional[Dict]) -> set:
    """Return trade ids that still have owned coins locked by offer_id."""
    if not trade_ids or not owned_coin_map:
        return set()
    locked_offer_ids = set()
    for info in owned_coin_map.values():
        if not isinstance(info, dict):
            continue
        normalized = _normalize_offer_lock_id(info.get("offer_id"))
        if normalized:
            locked_offer_ids.add(normalized)
    still_locked = set()
    for trade_id in trade_ids:
        normalized = _normalize_offer_lock_id(trade_id)
        if normalized and normalized in locked_offer_ids:
            still_locked.add(trade_id)
    return still_locked


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        return max(minimum, min(int(raw), maximum))
    except Exception:
        return default


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        return max(minimum, min(float(raw), maximum))
    except Exception:
        return default


def _chunked(items: list, size: int) -> list:
    size = max(1, int(size or 1))
    return [items[i:i + size] for i in range(0, len(items), size)]


def _sage_bulk_cancel_batch_size() -> int:
    return _bounded_env_int("SAGE_BULK_CANCEL_BATCH_SIZE", 25, 1, 100)


def _sage_bulk_cancel_batch_pause_secs() -> float:
    return _bounded_env_float("SAGE_BULK_CANCEL_BATCH_PAUSE_SECS", 0.5, 0.0, 10.0)


def _is_no_spendable_coin_error(result: Optional[Dict]) -> bool:
    if not isinstance(result, dict):
        return False
    error = str(result.get("error") or result.get("reason") or "").lower()
    return "no spendable coins" in error or "coin selection error" in error


def _cancel_offers_bulk_proper(offer_ids: list, fee_mojos: int = 0) -> bool:
    """Cancel multiple offers using the same 3-step path the Sage GUI uses.

    The Sage 'Cancel All Active' button does NOT use auto_submit=True.  It uses:
      1. cancel_offers(auto_submit=False)  → unsigned coin_spends returned
      2. sign_coin_spends(coin_spends)     → aggregated_signature produced
      3. submit_transaction(spend_bundle)  → broadcast to peers

    Using auto_submit=True in the HTTP RPC path signs server-side via a different
    code path that silently produces an invalid/incomplete signature, causing the
    transaction to fail on-chain.  The explicit sign+submit path always works.

    Returns True if all three steps succeeded, False otherwise.
    """
    num = len(offer_ids)
    print(f"   [Bulk] Step 1: cancel_offers(auto_submit=False, fee=0, n={num})...")
    try:
        cancel_resp = _sage_post("cancel_offers", {
            "offer_ids": offer_ids,
            "fee": fee_mojos,        # integer, not string — matches Tauri path
            "auto_submit": False,    # CRITICAL: get unsigned coin_spends back
        }, timeout=max(30, num * 2))
    except Exception as e:
        print(f"   [Bulk] cancel_offers failed: {e}")
        return False

    if not cancel_resp or not isinstance(cancel_resp, dict):
        print(f"   [Bulk] cancel_offers returned unexpected: {str(cancel_resp)[:200]}")
        return False

    coin_spends = cancel_resp.get("coin_spends")
    if not coin_spends:
        print(f"   [Bulk] cancel_offers response has no coin_spends: {str(cancel_resp)[:300]}")
        return False

    print(f"   [Bulk] Got {len(coin_spends)} coin_spends.  Step 2: sign_coin_spends...")
    try:
        sign_timeout = max(30, min(180, len(coin_spends) * 2))
        sign_resp = _sage_post("sign_coin_spends", {
            "coin_spends": coin_spends,
            "auto_submit": False,
            "partial": False,
        }, timeout=sign_timeout)
    except Exception as e:
        print(f"   [Bulk] sign_coin_spends failed: {e}")
        return False

    if not sign_resp or not isinstance(sign_resp, dict):
        print(f"   [Bulk] sign_coin_spends returned unexpected: {str(sign_resp)[:200]}")
        return False

    spend_bundle = sign_resp.get("spend_bundle")
    if not spend_bundle or not spend_bundle.get("aggregated_signature"):
        print(f"   [Bulk] sign_coin_spends response missing spend_bundle/sig: "
              f"{str(sign_resp)[:300]}")
        return False

    sig = spend_bundle.get("aggregated_signature", "")[:20]
    print(f"   [Bulk] Signed OK (sig={sig}...).  Step 3: submit_transaction...")
    try:
        submit_resp = _sage_post("submit_transaction", {
            "spend_bundle": spend_bundle,
        }, timeout=30)
    except Exception as e:
        print(f"   [Bulk] submit_transaction failed: {e}")
        return False

    print(f"   [Bulk] submit_transaction returned: {str(submit_resp)[:200]}")
    # HTTP 200 alone is not enough — Sage sometimes returns a 200 with
    # success:false / a populated error field when the signed bundle is
    # rejected (bad aggregate signature, already-spent inputs, etc.).
    # Previously we trusted the 200 status and every caller thought the
    # bulk cancel was in the mempool even though it was rejected, so the
    # sequential fallback never ran and the book stayed live under fake
    # "pending cancel" rows. Validate the JSON body before claiming
    # success.
    if not isinstance(submit_resp, dict):
        print("   [Bulk] submit_transaction response not a dict — falling back")
        return False
    _sub_err = submit_resp.get("error") or submit_resp.get("reason")
    _sub_status = str(submit_resp.get("status", "") or "").lower()
    if _sub_err or _sub_status in ("failed", "error", "rejected"):
        print(f"   [Bulk] submit_transaction rejected payload "
              f"(error={_sub_err!r}, status={_sub_status!r}) — falling back")
        return False
    if "success" in submit_resp and submit_resp.get("success") is False:
        print("   [Bulk] submit_transaction success=false — falling back")
        return False
    return True


def cancel_offers_batch(trade_ids: list, secure: bool = True, max_workers: int = 3,
                        fee_mojos: Optional[int] = None,
                        skip_confirmation: bool = False):
    """Cancel multiple offers via Sage, then wait for coins to return.

    Uses the same 3-step path the Sage GUI uses for bulk (>=2 offers):
      cancel_offers(auto_submit=False) → sign_coin_spends → submit_transaction
    Falls back to sequential individual cancel_offer calls if bulk fails.

    Simple model:
      1. Snapshot spendable coin count before cancel.
      2. Send bulk cancel_offers RPC (single transaction, fee=0).
      3. Poll spendable count until coins come back (count increases)
         or timeout. Coins will have new IDs but same values — coin
         manager handles ID tracking naturally.
      4. If bulk RPC fails, fall back to sequential cancels.

    IMPORTANT: The Sage cancel_offers endpoint applies the fee parameter
    independently per offer in a loop, then merges all CoinSpend objects
    into ONE spend bundle.  Passing fee > 0 causes Sage to select a fee
    coin for EACH offer separately; when merged the duplicate/conflicting
    fee coin spends cause BAD_AGGREGATE_SIGNATURE on-chain.  The fix is to
    always send fee=0 for the bulk RPC — the offer escrow coins are still
    spent and the cancel is confirmed on-chain, just without a priority fee.
    Sequential single-offer cancels (the fallback) each submit their own
    independent TX and are not affected by this bug.

    Returns dict of {trade_id: {"success": bool, ...}}.
    """
    results = {}
    if not trade_ids:
        return results

    resolved_fee = 0 if not secure else (
        max(0, int(fee_mojos))
        if fee_mojos is not None
        else get_effective_transaction_fee_mojos()
    )

    num_offers = len(trade_ids)

    # ── Wallet IDs for coin count checks ──
    _wallet_ids: set = {1}
    try:
        from config import cfg as _cfg_ref
        _wallet_ids.add(int(getattr(_cfg_ref, "CAT_WALLET_ID", 1)))
    except Exception:
        pass

    def _total_spendable():
        """Total spendable coins across XCH + CAT wallets, or None on RPC failure."""
        total = 0
        for _wid in _wallet_ids:
            try:
                count = get_spendable_coin_count(wallet_id=_wid)
                if count is None or count < 0:
                    return None
                total += count
            except Exception:
                return None
        return total

    target_trade_ids = set(trade_ids)

    def _pending_count():
        """Return pending transaction count, or None if Sage cannot answer."""
        try:
            pending = get_pending_transactions()
            if isinstance(pending, list):
                return len(pending)
        except Exception:
            pass
        return None

    def _locked_trade_ids():
        """Return trade ids still locking wallet coins, or None on RPC failure."""
        locked = set()
        for _wid in _wallet_ids:
            try:
                owned = get_owned_coins_detailed(_wid)
            except Exception:
                return None
            if owned is None:
                return None
            locked.update(_get_still_locked_trade_ids(target_trade_ids, owned))
        return locked

    # ── 1. Pre-cancel snapshot ──
    pre_coins = _total_spendable()
    if pre_coins is not None:
        print(f"   📸 [Sage] Pre-cancel: {pre_coins} spendable coins")
    else:
        print("   ⚠️ [Sage] Could not snapshot pre-cancel coins")

    # ── 2. Bulk cancel (GUI-identical 3-step path) with sequential fallback ──
    #
    # The Sage GUI's "Cancel All Active" button does:
    #   cancel_offers(auto_submit=False) → sign_coin_spends → submit_transaction
    # NOT auto_submit=True (which signs server-side via a different code path
    # that silently produces an invalid signature → transaction rejected on-chain).
    # _cancel_offers_bulk_proper() replicates the exact GUI path.
    cancel_submitted = False

    def _mark_bulk_submitted(batch_ids: list) -> None:
        for tid in batch_ids:
            results[tid] = {"success": True,
                            "method": "submitted_pending_confirm",
                            "submission_path": "bulk_3step"}

    def _cancel_sequential(batch_ids: list, batch_label: str = "") -> bool:
        nonlocal cancel_submitted
        delay = 0.3
        label = f" {batch_label}" if batch_label else ""
        print(f"📋 [Sage] Cancelling {len(batch_ids)} offers sequentially{label} ({delay}s delay)...")
        any_submitted = False
        for i, tid in enumerate(batch_ids):
            try:
                result = cancel_offer(tid, secure, timeout=15, fee_mojos=resolved_fee)
                if (
                    resolved_fee > 0
                    and result
                    and not result.get("success")
                    and _is_no_spendable_coin_error(result)
                ):
                    print(f"   ⚠️ [Sage] No fee coin for {tid[:16]}...; retrying cancel with fee=0")
                    result = cancel_offer(tid, secure, timeout=15, fee_mojos=0)
                results[tid] = result or {"success": False, "error": "RPC returned None"}
                if result and result.get("success"):
                    cancel_submitted = True
                    any_submitted = True
                    if (i + 1) % 10 == 0 or (i + 1) == len(batch_ids):
                        print(f"   ✅ [Sage] Cancelled {i + 1}/{len(batch_ids)}")
                else:
                    error = (result or {}).get("error", "unknown")
                    print(f"   ❌ [Sage] Failed {tid[:16]}...: {error}")
                if i < len(batch_ids) - 1:
                    time.sleep(delay)
            except (SageAlreadyIncluding, SageMempoolConflict):
                print(f"   ✅ [Sage] {tid[:16]}... already in mempool")
                results[tid] = {"success": True, "method": "already_in_mempool"}
                cancel_submitted = True
                any_submitted = True
            except Exception as e:
                print(f"   ❌ [Sage] Failed {tid[:16]}...: {e}")
                results[tid] = {"success": False, "error": str(e)}
        return any_submitted

    bulk_batch_size = _sage_bulk_cancel_batch_size()
    bulk_pause = _sage_bulk_cancel_batch_pause_secs()
    batches = _chunked(trade_ids, bulk_batch_size)

    if num_offers >= 2 and len(batches) > 1:
        print(f"📋 [Sage] Splitting bulk cancel of {num_offers} offers "
              f"into {len(batches)} batches of up to {bulk_batch_size}")

    for batch_index, batch_ids in enumerate(batches, start=1):
        batch_label = f"(batch {batch_index}/{len(batches)})"
        batch_submitted = False

        if len(batch_ids) >= 2:
            print(f"📋 [Sage] Bulk cancel {batch_label}: "
                  f"{len(batch_ids)} offers (GUI 3-step path)...")
            try:
                bulk_ok = _cancel_offers_bulk_proper(batch_ids, fee_mojos=0)
                if bulk_ok:
                    print(f"   ✅ [Sage] Bulk cancel {batch_label} submitted successfully")
                    cancel_submitted = True
                    batch_submitted = True
                    _mark_bulk_submitted(batch_ids)
                else:
                    print(f"   ⚠️ [Sage] Bulk cancel {batch_label} failed — falling back to sequential")
            except Exception as e:
                print(f"   ⚠️ [Sage] Bulk cancel {batch_label} error: {e} — falling back to sequential")

        if not batch_submitted:
            _cancel_sequential(batch_ids, batch_label if len(batches) > 1 else "")

        if bulk_pause > 0 and batch_index < len(batches):
            time.sleep(bulk_pause)

    if not cancel_submitted:
        print("   ❌ [Sage] No cancel RPCs succeeded — aborting")
        return results

    # ── 3. Skip confirmation if requested (requote fire-and-forget) ──
    if skip_confirmation:
        print("   📨 [Sage] Skipping confirmation (fire-and-forget mode)")
        return results

    # ── 4. Wait for coins to return ──
    # When cancels confirm on-chain the locked coins are released back as
    # new spendable coins (new IDs, same values). We just poll the total
    # spendable count and wait for it to increase — that's the definitive
    # signal that the cancel TX landed in a block.
    try:
        from config import cfg as _cfg
        poll_interval = max(3, min(int(getattr(_cfg, "CANCEL_POLL_INTERVAL_SECS", 10) or 10), 30))
        max_wait = max(30, min(int(getattr(_cfg, "CANCEL_MAX_WAIT_SECS", 120) or 120), 600))
    except Exception:
        poll_interval, max_wait = 10, 120

    print(f"🔄 [Sage] Waiting for coins to return (poll every {poll_interval}s, "
          f"max {max_wait}s)...")

    start_time = time.time()
    confirmed = False

    while (time.time() - start_time) < max_wait:
        time.sleep(poll_interval)
        elapsed = int(time.time() - start_time)
        try:
            current_coins = _total_spendable()
            if current_coins is None:
                print(f"   🔄 [{elapsed}s] spendable=? (RPC error, retrying)")
                continue

            delta = (current_coins - pre_coins) if pre_coins is not None else 0
            # Also check how many open offers remain
            open_remaining = 0
            try:
                open_offers = get_all_offers(include_completed=False, end=500)
                if open_offers and isinstance(open_offers, list):
                    for o in open_offers:
                        tid = o.get("trade_id", "") or o.get("offer_id", "")
                        if tid in target_trade_ids:
                            raw_status = o.get("status")
                            # Count fillable offers (includes PENDING_CANCEL
                            # — the cancel TX is in the mempool but the
                            # counterparty can still take the offer until
                            # it confirms). Previously we used
                            # _is_open_status which counted PENDING_CANCEL
                            # as closed, so the batch could declare
                            # success while offers were still accepting
                            # fills in a flash-move window. Under
                            # congestion that let adverse fills stack
                            # into the move.
                            if _is_still_fillable(raw_status, o):
                                open_remaining += 1
            except Exception:
                open_remaining = -1  # unknown

            pending_count = _pending_count()
            still_locked = _locked_trade_ids()
            coins_returned = pre_coins is not None and delta >= num_offers
            locks_clear = still_locked is not None and not still_locked
            pending_clear = pending_count == 0

            print(f"   🔄 [{elapsed}s] spendable={current_coins} "
                  f"(delta=+{delta}), open_remaining={open_remaining}")

            # Success: no more fillable offers from our batch (offers
            # off-book and cancels confirmed on-chain — PENDING_CANCEL
            # rows are NOT counted as success because a fill can still
            # beat an in-mempool cancel).
            if open_remaining == 0 and (coins_returned or (locks_clear and pending_clear)):
                print(f"   ✅ [Sage] All offers cancelled — coins returned "
                      f"(spendable={current_coins}, delta=+{delta})")
                confirmed = True
                for tid in trade_ids:
                    entry = results.get(tid, {})
                    entry["success"] = True
                    entry["method"] = "confirmed_by_unlock"
                    results[tid] = entry
                break

            if open_remaining == 0:
                print("   [Sage] Offers are off-book, waiting for cancel "
                      "settlement before releasing coins to coin prep")

            # Secondary: coin count jumped significantly even if status is lagging
            if coins_returned and open_remaining <= 0:
                print(f"   ✅ [Sage] Coin count confirms cancels "
                      f"(+{delta} coins, expected ~{num_offers})")
                confirmed = True
                for tid in trade_ids:
                    results[tid] = {"success": True, "method": "confirmed_by_coin_delta"}
                break

        except Exception as e:
            print(f"   ⚠️ [{elapsed}s] Poll error: {e}")

    # ── 5. Final result ──
    elapsed = int(time.time() - start_time)
    if not confirmed:
        # Sequential-phase entries already in `results` look success=True
        # even though the cancel TX was only SUBMITTED, never verified on
        # chain. If we leave them alone here, offer_manager sees method=""
        # and writes status='cancelled' in the DB — corrupting state when
        # the TX is actually still pending or has been displaced.
        # Demote every unconfirmed success to submitted_pending_confirm
        # so the CANCEL_PENDING_METHODS guard keeps DB status open until
        # bot_health / fill_tracker observes the real on-chain outcome.
        # Duplicated (not imported) to avoid a circular import with
        # offer_manager. Keep in sync with offer_manager.CANCEL_PENDING_METHODS.
        PENDING_METHODS = frozenset({
            "submitted_pending_confirm",
            "already_in_mempool",
            "mempool_conflict_inflight",
            "already_gone_ambiguous",
        })
        try:
            final_coins = _total_spendable()
            final_delta = (final_coins - pre_coins) if (final_coins and pre_coins) else 0
            print(f"   ⏱️ [Sage] Timeout after {elapsed}s — spendable={final_coins}, "
                  f"delta=+{final_delta}")
            demoted = 0
            for tid in trade_ids:
                existing = results.get(tid)
                if existing is None:
                    # Never made it into the sequential-phase dict: mark as
                    # submitted_pending_confirm so DB stays open.
                    results[tid] = {"success": True,
                                    "method": "submitted_pending_confirm",
                                    "note": f"Cancel submitted, awaiting on-chain confirm "
                                            f"(timed out after {elapsed}s)"}
                    demoted += 1
                    continue
                if not existing.get("success"):
                    # Real failure from the sequential phase — leave alone
                    # so the retry queue can re-attempt.
                    continue
                method = str(existing.get("method") or "")
                if method in PENDING_METHODS:
                    # Already flagged pending; nothing to do.
                    continue
                # An unconfirmed "success" with no pending tag. Demote it
                # so downstream consumers do not mistake it for a verified
                # on-chain cancel.
                demoted_entry = dict(existing)
                demoted_entry["method"] = "submitted_pending_confirm"
                demoted_entry.setdefault(
                    "previous_method", method or "unspecified"
                )
                demoted_entry["note"] = (
                    f"Cancel submitted but not confirmed within {elapsed}s "
                    f"— leaving DB open for verifier to settle."
                )
                results[tid] = demoted_entry
                demoted += 1
            if demoted:
                print(f"   ⏱️ [Sage] Demoted {demoted} unconfirmed cancels to "
                      f"submitted_pending_confirm")
        except Exception as _final_err:
            print(f"   ⚠️ [Sage] Timeout post-processing failed: {_final_err}")
            for tid in trade_ids:
                if tid not in results:
                    results[tid] = {"success": False, "error": f"Timed out after {elapsed}s"}
    else:
        print(f"   ✅ [Sage] Cancel batch complete in {elapsed}s")

    return results


# ============================================================================
# DASHBOARD / NODE QUERIES (Sage stubs — light wallet has no full node)
# ============================================================================

def get_blockchain_state_full():
    """Get blockchain state — Sage has no full node, so we use Coinset API
    if available, otherwise return a minimal status from get_sync_status.
    """
    # Try Coinset API first (if V3 coinset_client is available)
    try:
        from coinset_client import CoinsetClient
        client = CoinsetClient()
        state = client.get_blockchain_state()
        if state:
            return state
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: get basic info from Sage's own sync status
    try:
        result = rpc("get_sync_status", {}, timeout=5)
        if result and isinstance(result, dict):
            return {
                "success": True,
                "peak_height": result.get("peak_height", 0),
                "peak_timestamp": 0,  # Sage sync status doesn't include this
                "difficulty": 0,  # N/A for light wallet
                "space_bytes": 0,  # N/A for light wallet
                "mempool_size": 0,  # N/A for light wallet
                "synced": result.get("synced", False),
                "syncing": not result.get("synced", False),
                "sync_tip_height": result.get("target_height", 0),
                "sync_progress_height": result.get("peak_height", 0),
            }
    except Exception:
        pass

    return None


def get_peer_connections():
    """Get peer connections — Sage manages its own P2P peers.
    We can list them via get_peers, but the format differs from full node.
    Returns empty list if not available (dashboard shows 0 peers gracefully).
    """
    try:
        result = rpc("get_peers", {}, timeout=5)
        if result and isinstance(result, list):
            peers = []
            for p in result:
                peers.append({
                    "node_id": str(p.get("ip", ""))[:16],
                    "peer_host": p.get("ip", ""),
                    "peer_port": p.get("port", 0),
                    "type": 1,  # Treat all Sage peers as full_node type for display
                    "bytes_read": 0,
                    "bytes_written": 0,
                    "peak_height": p.get("peak_height", 0),
                    "creation_time": 0,
                })
            return peers
        elif result and isinstance(result, dict):
            # Might be wrapped in a dict with a "peers" key
            peer_list = result.get("peers", [])
            peers = []
            for p in peer_list:
                peers.append({
                    "node_id": str(p.get("ip", ""))[:16],
                    "peer_host": p.get("ip", ""),
                    "peer_port": p.get("port", 0),
                    "type": 1,
                    "bytes_read": 0,
                    "bytes_written": 0,
                    "peak_height": p.get("peak_height", 0),
                    "creation_time": 0,
                })
            return peers
    except Exception:
        pass
    return []


def get_transactions_list(wallet_id: int, start: int = 0, end: int = 50,
                          sort_key: str = "CONFIRMED_AT_HEIGHT",
                          reverse: bool = True):
    """Get transaction history — Sage uses get_transactions endpoint."""
    try:
        result = rpc("get_transactions", {
            "offset": start,
            "limit": min(end - start, 50),
            "ascending": not reverse,
        }, timeout=15)
        if result and isinstance(result, dict):
            txs = result.get("transactions", [])
            return {
                "success": True,
                "transactions": txs,
                "wallet_id": wallet_id,
            }
        elif result and isinstance(result, list):
            return {
                "success": True,
                "transactions": result,
                "wallet_id": wallet_id,
            }
    except Exception:
        pass
    return None


def get_transaction_count(wallet_id: int) -> int:
    """Get total transaction count — Sage uses get_transactions with limit 0."""
    try:
        result = rpc("get_transactions", {"offset": 0, "limit": 1, "ascending": False}, timeout=5)
        if result and isinstance(result, dict):
            return result.get("total", 0)
    except Exception:
        pass
    return 0


# REMOVED: duplicate get_pending_transactions() that shadowed the correct
# implementation at line ~833. The correct version uses Sage's documented
# get_pending_transactions endpoint. This duplicate used get_transactions
# with heuristic status filtering.


def get_all_coins_for_wallet(wallet_id: int):
    """Get ALL coins for a wallet including locked ones.
    Returns both spendable and pending coins for the dashboard.
    """
    try:
        if _is_cat_wallet(wallet_id):
            asset_id = _get_cat_asset_id()
            if not asset_id or not asset_id.strip():
                return {"success": True, "confirmed_coins": [], "pending_coins": []}
            result = rpc("get_coins", {
                "asset_id": asset_id,
                "offset": 0, "limit": 500,
                "sort_mode": "amount",
                "filter_mode": "all",
                "ascending": False,
            }, timeout=15)
        else:
            result = rpc("get_coins", {
                "asset_id": None,
                "offset": 0, "limit": 500,
                "sort_mode": "amount",
                "filter_mode": "all",
                "ascending": False,
            }, timeout=15)

        if result and isinstance(result, dict):
            coins = result.get("coins", [])
            # Wrap in Chia-compatible format
            confirmed = []
            for c in coins:
                confirmed.append({
                    "coin": c,
                    "confirmed_block_index": c.get("confirmed_block_index", 0),
                    "spent_block_index": c.get("spent_block_index", 0),
                    "coinbase": False,
                    "timestamp": c.get("timestamp", 0),
                })
            return {
                "confirmed": confirmed,
                "pending_additions": [],
                "pending_removals": [],
            }
        elif result and isinstance(result, list):
            confirmed = []
            for c in result:
                confirmed.append({
                    "coin": c,
                    "confirmed_block_index": 0,
                    "spent_block_index": 0,
                    "coinbase": False,
                    "timestamp": 0,
                })
            return {
                "confirmed": confirmed,
                "pending_additions": [],
                "pending_removals": [],
            }
    except Exception as e:
        print(f"❌ [Sage] get_all_coins_for_wallet failed: {e}")
    return None


def get_owned_coins(wallet_id: int) -> Optional[Dict]:
    """Get all currently held coins (free + offer-locked, not spent).

    Uses filter_mode="owned" which returns only coins we still hold.
    Unlike "all" which includes spent coins and mixes asset types,
    "owned" gives us exactly the coins that exist right now.

    Returns dict: {normalized_coin_id: amount_mojos}
    """
    if _is_cat_wallet(wallet_id):
        asset_id = _get_cat_asset_id()
        if not asset_id:
            return None
    else:
        asset_id = None

    result = rpc("get_coins", {
        "asset_id": asset_id,
        "offset": 0, "limit": 500,
        "filter_mode": "owned",
    }, timeout=15)

    if not result:
        return None

    coins = result.get("coins") or result.get("records") or result.get("data") or []
    # Return {coin_id: amount_mojos} with normalized IDs (0x prefix)
    coin_map = {}
    for c in coins:
        cid = c.get("coin_id", "")
        if cid:
            # Normalize: add 0x prefix if missing
            if not cid.startswith("0x"):
                cid = "0x" + cid.lower()
            else:
                cid = cid.lower()
            coin_map[cid] = int(c.get("amount", "0"))
    return coin_map


def get_owned_coins_detailed(wallet_id: int) -> Optional[Dict]:
    """Get all owned coins with full detail including offer_id.

    Same as get_owned_coins but returns richer data per coin, including
    the offer_id (offer_hash) which tells us exactly which offer locked
    a coin. This eliminates the need for unreliable amount-based matching.

    From Sage source (migrations/0002_options.sql):
      - owned_coins view = wallet_coins WHERE spent_height IS NULL
        AND mempool_item_hash IS NULL (includes offer-locked coins)
      - selectable_coins view = same + offer_hash IS NULL (excludes locked)

    The response CoinRecord includes: coin_id, amount, offer_id,
    transaction_id, created_height, spent_height, etc.

    Returns dict: {normalized_coin_id: {amount, offer_id, created_height, spent_height}}
    """
    if _is_cat_wallet(wallet_id):
        asset_id = _get_cat_asset_id()
        if not asset_id:
            return None
    else:
        asset_id = None

    # Paginate through ALL owned coins. Previously we hard-capped at
    # 500, which silently truncated after a long downtime or heavy coin
    # fragmentation — the reconcile path then treated the snapshot as
    # authoritative and rebuilt ladder depth/tiers from a partial view
    # of the wallet. Page through until Sage returns fewer than the page
    # size (natural end) or a safety cap is hit.
    page_size = 500
    max_pages = 40                # 20k coins ceiling
    coin_map: Dict[str, Dict] = {}
    for page in range(max_pages):
        offset = page * page_size
        result = rpc("get_coins", {
            "asset_id": asset_id,
            "offset": offset, "limit": page_size,
            "filter_mode": "owned",
        }, timeout=15)
        if not result:
            if page == 0:
                return None
            break
        coins = result.get("coins") or result.get("records") or result.get("data") or []
        if not coins:
            break
        for c in coins:
            cid = c.get("coin_id", "")
            if cid:
                if not cid.startswith("0x"):
                    cid = "0x" + cid.lower()
                else:
                    cid = cid.lower()
                # Extract offer_id — this is the offer_hash from Sage's DB
                # If set, this coin is locked by an offer
                offer_id = c.get("offer_id") or c.get("offer_hash") or None
                if offer_id and isinstance(offer_id, str):
                    offer_id = offer_id.lower()
                coin_map[cid] = {
                    "amount": int(c.get("amount", "0")),
                    "offer_id": offer_id,
                    "created_height": c.get("created_height"),
                    "spent_height": c.get("spent_height"),
                    "transaction_id": c.get("transaction_id"),
                }
        if len(coins) < page_size:
            break
    return coin_map


def get_coins_by_ids(coin_ids: list) -> Optional[Dict]:
    """Get detailed status for specific coins by their IDs.

    Sage endpoint: get_coins_by_ids
    Request: {coin_ids: ["0xabc...", "0xdef..."]}
    Response: {coins: [CoinRecord, ...]}

    Each CoinRecord includes: coin_id, amount, offer_id (if locked),
    spent_height (if spent), created_height, transaction_id.

    Returns dict: {normalized_coin_id: {amount, offer_id, spent_height, created_height}}
    Returns None on RPC failure.
    """
    if not coin_ids:
        return {}

    # Normalize IDs for the request — Sage expects hex strings
    normalized = []
    for cid in coin_ids:
        if isinstance(cid, str):
            # Remove 0x prefix if present — Sage might want bare hex
            clean = cid.lower()
            if clean.startswith("0x"):
                clean = clean[2:]
            normalized.append(clean)

    result = rpc("get_coins_by_ids", {
        "coin_ids": normalized,
    }, timeout=15)

    if not result:
        return None

    coins = result.get("coins") or result.get("records") or result.get("data") or []
    coin_map = {}
    for c in coins:
        cid = c.get("coin_id", "")
        if cid:
            if not cid.startswith("0x"):
                cid = "0x" + cid.lower()
            else:
                cid = cid.lower()
            offer_id = c.get("offer_id") or c.get("offer_hash") or None
            if offer_id and isinstance(offer_id, str):
                offer_id = offer_id.lower()
            coin_map[cid] = {
                "amount": int(c.get("amount", "0")),
                "offer_id": offer_id,
                "spent_height": c.get("spent_height"),
                "created_height": c.get("created_height"),
                "transaction_id": c.get("transaction_id"),
            }
    return coin_map


def are_coins_spendable(coin_ids: list) -> Optional[bool]:
    """Check if ALL given coins are currently spendable.

    Sage endpoint: get_are_coins_spendable
    Request: {coin_ids: ["0xabc...", "0xdef..."]}
    Response: {spendable: bool} — true only if ALL coins are spendable.

    Returns True/False, or None on RPC failure.
    """
    return _are_coins_spendable_rpc(coin_ids)


def get_selectable_coins_map(wallet_id: int) -> Optional[Dict]:
    """Get all selectable (spendable/free) coins as a map.

    Returns dict: {normalized_coin_id: amount_mojos}
    """
    if _is_cat_wallet(wallet_id):
        asset_id = _get_cat_asset_id()
        if not asset_id:
            return None
    else:
        asset_id = None

    result = rpc("get_coins", {
        "asset_id": asset_id,
        "offset": 0, "limit": 500,
        "filter_mode": "selectable",
    }, timeout=15)

    if not result:
        return None

    coins = result.get("coins") or result.get("records") or result.get("data") or []
    coin_map = {}
    for c in coins:
        cid = c.get("coin_id", "")
        if cid:
            if not cid.startswith("0x"):
                cid = "0x" + cid.lower()
            else:
                cid = cid.lower()
            coin_map[cid] = int(c.get("amount", "0"))
    return coin_map


# ============================================================================
# SAGE-SPECIFIC FEATURES (not available in Chia wallet)
# ============================================================================

def auto_combine_xch(fee_mojos: int = 0, max_coins: int = 500):
    """Auto-combine small XCH coins into larger ones.
    Sage's smart combining — picks the optimal coins automatically.
    Requires max_coins parameter (max number of coins to combine per call).
    """
    if not _require_signing_capability():
        return {"success": False, "error": "Watch-only wallet cannot auto-combine XCH coins"}
    payload = {
        "fee": str(int(fee_mojos)),
        "max_coins": max_coins,
        "auto_submit": True,  # CRITICAL: without this, Sage plans but never sends
    }
    result = rpc("auto_combine_xch", payload, timeout=120)
    if WALLET_DEBUG:
        print(f"  [Sage] auto_combine_xch result: {result}")
    return result


def auto_combine_cat(asset_id: str = None, fee_mojos: int = 0, max_coins: int = 500):
    """Auto-combine small CAT coins into larger ones.
    Sage picks the optimal coins for the given CAT asset.
    Requires max_coins parameter (max number of coins to combine per call).
    """
    if not _require_signing_capability():
        return {"success": False, "error": "Watch-only wallet cannot auto-combine CAT coins"}
    if asset_id is None:
        asset_id = _get_cat_asset_id()
    if not asset_id:
        print("❌ [Sage] No CAT_ASSET_ID — cannot auto-combine CATs")
        return None

    payload = {
        "asset_id": asset_id,
        "fee": str(int(fee_mojos)),
        "max_coins": max_coins,
        "auto_submit": True,  # CRITICAL: without this, Sage plans but never sends
    }
    result = rpc("auto_combine_cat", payload, timeout=120)
    if WALLET_DEBUG:
        print(f"  [Sage] auto_combine_cat result: {result}")
    return result


# ---------------------------------------------------------------------------
# F48 (2026-04-09): Wallet puzzle hash cache for self-spend detection.
#
# Used by fill_tracker to distinguish real fills (MZ added at one of our
# addresses) from cancels (offer coin returned to one of our addresses).
# Sage's get_derivations endpoint returns every derived address in our
# wallet's address book; we decode each bech32 to a puzzle hash and cache
# the set for fast O(1) membership checks.
# ---------------------------------------------------------------------------

_puzzle_hash_cache: set = set()
_puzzle_hash_cache_at: float = 0.0
_PUZZLE_HASH_CACHE_TTL_SECS: float = 600.0  # 10 minutes


def get_wallet_puzzle_hashes(force: bool = False, max_derivations: int = 5000) -> set:
    """F48 (2026-04-09): return the set of puzzle hashes owned by the
    current Sage wallet.

    Walks Sage's get_derivations endpoint (both unhardened and hardened
    keys) and decodes each bech32 address to a 32-byte puzzle hash via
    chia.util.bech32m. Results are cached for 10 minutes so repeated
    calls during fill verification are cheap.

    Returns a set of lowercase hex strings WITHOUT the '0x' prefix so
    caller code can do `ph in get_wallet_puzzle_hashes()` after also
    stripping its own prefix.

    Returns an empty set on any failure. Callers must handle that case
    (typically by falling back to the legacy verification path).
    """
    from database import log_event  # F821: not imported at module level in wallet_sage
    global _puzzle_hash_cache, _puzzle_hash_cache_at

    now = time.time()
    if (not force and _puzzle_hash_cache
            and (now - _puzzle_hash_cache_at) < _PUZZLE_HASH_CACHE_TTL_SECS):
        return _puzzle_hash_cache

    try:
        from chia.util.bech32m import decode_puzzle_hash
    except ImportError:
        log_event(
            "warning",
            "puzzle_hash_cache_no_bech32m",
            "chia.util.bech32m not available — wallet PH cache disabled. "
            "Install chia-blockchain to enable fill-vs-cancel disambiguation.",
        )
        return set()

    collected: set = set()
    page_size = 200

    # Walk both unhardened and hardened derivation trees
    for hardened in (False, True):
        offset = 0
        # Hard cap iterations as a safety net against pathological pagination
        for _ in range(max(1, max_derivations // page_size)):
            try:
                res = rpc(
                    "get_derivations",
                    {"offset": offset, "limit": page_size, "hardened": hardened},
                    timeout=10,
                )
            except Exception as exc:
                log_event(
                    "debug",
                    "puzzle_hash_cache_rpc_error",
                    f"get_derivations(hardened={hardened}, offset={offset}) failed: {exc}",
                )
                break

            if not isinstance(res, dict) or res.get("success") is False:
                break

            derivations = res.get("derivations") or []
            if not derivations:
                break

            for d in derivations:
                addr = str((d or {}).get("address", "")).strip()
                if not addr:
                    continue
                try:
                    ph_bytes = decode_puzzle_hash(addr)
                    collected.add(ph_bytes.hex().lower())
                except Exception:
                    # Unknown address format — skip, don't raise
                    continue

            if len(derivations) < page_size:
                break
            offset += page_size

    if collected:
        _puzzle_hash_cache = collected
        _puzzle_hash_cache_at = now
        log_event(
            "info",
            "puzzle_hash_cache_loaded",
            f"Loaded {len(collected)} wallet puzzle hashes from Sage "
            f"derivations (used for fill-vs-cancel disambiguation)",
        )
    else:
        log_event(
            "warning",
            "puzzle_hash_cache_empty",
            "get_derivations returned no usable addresses — fill verification "
            "will fall back to legacy paths.",
        )

    return collected


def delete_offer(offer_id: str) -> bool:
    """Delete an offer record from Sage's local database.

    This is a LOCAL operation only — it does NOT cancel the offer on-chain.
    Use this to clean up completed, cancelled, or expired offers that are
    cluttering Sage's offer list. The offer must already be in a terminal
    state (cancelled/completed/expired) before calling this.

    Args:
        offer_id: The offer/trade ID to delete (hex string, with or without 0x)

    Returns:
        True if successfully deleted (or already gone from Sage), False on error.
    """
    bare_id = offer_id.replace("0x", "")
    try:
        result = _sage_post("delete_offer", {"offer_id": bare_id}, timeout=10)
        if WALLET_DEBUG:
            print(f"   [Sage] delete_offer {bare_id[:16]}... → {result}")
        if result is None:
            # Sage returned no body — this typically means the offer is not in
            # Sage's local DB (already auto-cleaned or never tracked locally).
            # Since delete_offer is a local-only idempotent cleanup, "not found"
            # is effectively success — the offer is already gone.
            if WALLET_DEBUG:
                print(f"   [Sage] delete_offer {bare_id[:16]}... no response (offer already gone)")
            return True
        if not result.get("success"):
            err = result.get("error", "unknown error")
            if not _quiet_mode:
                print(f"   ⚠️ [Sage] delete_offer {bare_id[:16]}... returned failure: {err}")
            return False
        return True
    except Exception as e:
        if not _quiet_mode:
            print(f"   ⚠️ [Sage] delete_offer {bare_id[:16]}... failed: {e}")
        return False


def delete_offers_batch(offer_ids: list) -> dict:
    """Delete multiple offer records from Sage's local database.

    Iterates through the list and deletes each one. Returns summary.

    Args:
        offer_ids: List of offer/trade IDs to delete

    Returns:
        dict with 'deleted' and 'failed' counts
    """
    deleted = 0
    failed = 0
    for oid in offer_ids:
        if delete_offer(oid):
            deleted += 1
        else:
            failed += 1
        time.sleep(0.1)  # Small delay to not hammer Sage
    return {"deleted": deleted, "failed": failed}


# REMOVED: duplicate get_spendable_coin_count() that shadowed the correct
# implementation at line ~796. The correct version uses Sage's documented
# get_spendable_coin_count endpoint. This duplicate used get_coins with
# filter_mode=selectable approximation.


def get_sage_version() -> str:
    """Get Sage wallet version for health diagnostics."""
    try:
        result = rpc("get_version", {}, timeout=3)
        if result and isinstance(result, dict):
            return result.get("version", "unknown")
        elif result and isinstance(result, str):
            return result
    except Exception:
        pass
    return "unknown"


def view_offer(offer_bech32: str):
    """Parse an offer string without importing it.
    Returns the offer details (what's being offered/requested).
    Useful for inspecting incoming Splash/Dexie offers before taking them.
    """
    try:
        result = rpc("view_offer", {"offer": offer_bech32}, timeout=10)
        return result
    except Exception as e:
        print(f"❌ [Sage] view_offer failed: {e}")
        return None


# ============================================================================
# COIN MANAGEMENT HELPERS
# ============================================================================

def cat_to_mojos(amount: Decimal, decimals: int) -> int:
    """Convert CAT amount to mojos."""
    scale = Decimal(10) ** Decimal(decimals)
    return int((amount * scale).to_integral_value(ROUND_DOWN))


def xch_to_mojos(amount: Decimal) -> int:
    """Convert XCH amount to mojos (1 XCH = 1e12 mojos)."""
    return int((amount * Decimal("1000000000000")).to_integral_value(ROUND_DOWN))
