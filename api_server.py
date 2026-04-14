"""
V2 API Server — Thin Flask Layer

This is the web server: HTTP routes + Server-Sent Events for the GUI.
All business logic lives in the modules (bot_loop, offer_manager, etc.).
This file just translates HTTP requests into module calls.

SSE (Server-Sent Events) provides real-time push updates to the GUI
without needing Flask-SocketIO or any extra dependencies.

Down from 7,500 lines in V1 to ~600 lines.

Usage:
    python api_server.py
    # Serves on http://localhost:5000
"""

import os
import sys
import io
import json
import time
import signal
import queue
import logging
import threading
import secrets
import webbrowser
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import urlparse, quote

# ---------------------------------------------------------------------------
# Fix Windows cp1252 terminal encoding so emoji in log messages don't crash.
# ---------------------------------------------------------------------------
# Use reconfigure() instead of detach+wrap: reconfigure changes encoding
# in-place without detaching the underlying buffer.  This is critical when
# running under pytest --capture=sys: sys.stdout is a CaptureIO wrapping a
# BytesIO, and calling detach() on it would rip the BytesIO away, causing
# pytest's getvalue() to fail with "assert isinstance(self.buffer, BytesIO)".
if sys.platform == "win32":
    for _attr in ("stdout", "__stdout__", "stderr", "__stderr__"):
        _st = getattr(sys, _attr, None)
        if _st is not None and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif _st is not None and hasattr(_st, "buffer"):
            # Fallback for streams that don't support reconfigure — only safe
            # to detach when the buffer is a real file (not BytesIO capture).
            try:
                if not isinstance(_st.buffer, io.BytesIO):
                    _buf = _st.detach()
                    _wrapped = io.TextIOWrapper(
                        _buf, encoding="utf-8", errors="replace",
                        line_buffering=True,
                    )
                    setattr(sys, _attr, _wrapped)
            except Exception:
                pass
from flask import Flask, jsonify, request, send_from_directory, send_file, Response

# ---- Super Log: capture EVERYTHING to terminal + file ----
from super_log import init_super_log, slog, intercept_log_event
init_super_log()
slog("STARTUP", "=== API SERVER STARTING ===")

from config import cfg
from database import (
    init_database,
    log_event,
    get_stats,
    backup_database,
    get_connection,
    get_live_tier_group_counts,
)
from tx_fees import get_fee_settings_snapshot

# ---------------------------------------------------------------------------
# Bundle-aware path resolution.
#
# In a PyInstaller onedir bundle, __file__ for non-entry-point modules
# resolves to the _internal/ subdirectory, NOT the bundle root where data
# files (HTML, images) are placed.  sys._MEIPASS always points to the
# bundle root, so we use it when available.
# ---------------------------------------------------------------------------
_APP_ROOT = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
_APP_VERSION_CACHE = None
_SPACESCAN_PUBLIC_PLANS = {
    "free": {
        "label": "Free",
        "requests_per_minute": 5,
        "requests_per_month": 1000,
    },
    "hobbyist": {
        "label": "Hobbyist",
        "requests_per_minute": 10,
        "requests_per_month": 10000,
    },
    "builder": {
        "label": "Builder",
        "requests_per_minute": 20,
        "requests_per_month": 40000,
    },
    "startup": {
        "label": "Startup",
        "requests_per_minute": 60,
        "requests_per_month": 100000,
    },
}

# Intercept log_event so ALL events appear in super_log too
intercept_log_event()

from bot_loop import BotLoop
from wallet import get_wallet_type

# ---- Super Log: hook ALL module methods for complete visibility ----
try:
    from super_log_hooks import install_all_hooks
    install_all_hooks()
except Exception as e:
    slog("STARTUP", f"Failed to install hooks: {e}")


# ---------------------------------------------------------------------------
# Suppress noisy Flask/Werkzeug request logs for polling endpoints
# ---------------------------------------------------------------------------
# These endpoints are hit every 1-5 seconds by the GUI and flood the terminal
# with useless lines. We filter them so only "interesting" requests show up.

_QUIET_ENDPOINTS = {
    "/api/status",
    "/api/bot/state",
    "/api/health",
    "/api/coin-prep/status",
    "/api/offers/cancel_all/status",
    "/api/splash/incoming",
    "/api/events",
    "/api/sage/startup-status",
    "/api/console/status",
}

# Loopback-only machine producers may post here without the GUI token.
# Keep this list extremely small and only for routes that are not user-driven.
_TOKEN_EXEMPT_WRITE_ROUTES = {
    "/api/splash/incoming",
}

# Generic control-plane throttling is too aggressive for local webhook bursts.
# Those machine routes stay loopback-only and must implement their own validation.
_RATE_LIMIT_EXEMPT_WRITE_ROUTES = {
    "/api/splash/incoming",
    "/api/log",              # GUI flushes buffered log entries in bursts
}

# Dedicated limiter for /api/splash/incoming so an unbounded webhook flood
# cannot amplify into runaway DB writes. 200/sec per process is still
# generous for a local webhook but prevents a pathological flood.
_SPLASH_RATE_LIMIT = {"window_s": 1.0, "max": 200, "hits": [], "lock": threading.Lock()}
def _splash_incoming_rate_limited() -> bool:
    import time as _t
    now = _t.time()
    with _SPLASH_RATE_LIMIT["lock"]:
        hits = _SPLASH_RATE_LIMIT["hits"]
        cutoff = now - _SPLASH_RATE_LIMIT["window_s"]
        # Drop expired entries
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= _SPLASH_RATE_LIMIT["max"]:
            return True
        hits.append(now)
        return False

# ---------------------------------------------------------------------------
# Simple per-endpoint rate limiter for state-changing operations
#
# Thread-safe within a single process (threading.Lock). This does NOT protect
# across multiple worker processes, but Flask runs single-process in this app
# (embedded in desktop_app.py or standalone). If ever deployed multi-worker
# (gunicorn -w N), replace with a shared store (Redis, SQLite, etc.).
# ---------------------------------------------------------------------------
_rate_limit_log: dict = {}  # {endpoint: [timestamp, ...]}
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_WINDOW = 10     # seconds
_RATE_LIMIT_MAX = 20        # max requests per window

def _is_rate_limited(endpoint: str) -> bool:
    """Check if an endpoint is being called too frequently."""
    import time as _rl_time
    now = _rl_time.time()
    cutoff = now - _RATE_LIMIT_WINDOW
    with _rate_limit_lock:
        hits = _rate_limit_log.get(endpoint, [])
        hits = [t for t in hits if t > cutoff]
        hits.append(now)
        _rate_limit_log[endpoint] = hits
        return len(hits) > _RATE_LIMIT_MAX

_dbx_pair_cache = {}
_LOCAL_API_TOKEN_HEADER = "X-Bot-Local-Token"
_LOCAL_API_QUERY_PARAM = "_local_token"
_LOCAL_API_TOKEN = os.environ.get("BOT_LOCAL_WRITE_TOKEN") or secrets.token_urlsafe(32)
os.environ["BOT_LOCAL_WRITE_TOKEN"] = _LOCAL_API_TOKEN

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

# Substrings that flag a config key as sensitive — these values are never
# logged or returned in API error messages.
_SENSITIVE_KEY_FRAGMENTS = {
    "key", "cert", "password", "secret", "token", "mnemonic", "seed",
    "fingerprint", "private",
}


def _is_sensitive_key(key: str) -> bool:
    """Return True if the config key name suggests a sensitive value."""
    k = str(key).lower()
    return any(frag in k for frag in _SENSITIVE_KEY_FRAGMENTS)


def _sanitize_config_dict(d: object) -> object:
    """Recursively redact values whose keys look sensitive.

    Used before any dict reaches a log line or API response to prevent
    accidental credential exposure.
    """
    if isinstance(d, dict):
        return {
            k: "***" if _is_sensitive_key(k) else _sanitize_config_dict(v)
            for k, v in d.items()
        }
    if isinstance(d, (list, tuple)):
        return [_sanitize_config_dict(x) for x in d]
    return d


def _decimal_safe(obj):
    """Recursively convert Decimal values to float for JSON serialization.

    Decimal arithmetic is used for price calculations to avoid float rounding,
    but JSON (and JS) only support float — convert at the serialization boundary.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_decimal_safe(x) for x in obj]
    return obj


def _api_error(e: Exception, endpoint: str = "", status: int = 500):
    """Return a safe JSON error response that does NOT expose internal details.

    The real exception is written to the database event log so it is still
    visible in the debug log download, but clients only see a generic message.
    """
    try:
        log_event("error", "api_error",
                  f"Unhandled exception on {endpoint or 'unknown'}: {e}",
                  {"endpoint": endpoint})
    except Exception:
        pass
    return jsonify({"error": "Internal server error", "code": "SERVER_ERROR"}), status


# ---------------------------------------------------------------------------
# Startup security checks
# ---------------------------------------------------------------------------

def _check_env_file_permissions():
    """Warn if the .env file is readable by group or others.

    POSIX permission bits only have meaningful semantics on Unix-like
    platforms. On Windows, ``os.stat()`` happily returns an ``st_mode``
    value with group/other bits set (NTFS typically reports 0o666), so
    the naive mask check fires a false-positive on every startup — we
    saw it spamming the logs tab. Skip the check entirely on Windows
    where NTFS ACLs are the actual access-control layer.
    """
    if sys.platform == "win32":
        return
    import stat as _stat
    try:
        from user_paths import env_file as _env_file
        env_path = _env_file()
    except Exception:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        mode = os.stat(env_path).st_mode
        if mode & (_stat.S_IRGRP | _stat.S_IWGRP | _stat.S_IROTH | _stat.S_IWOTH):
            print(
                "[SECURITY] WARNING: .env file is readable/writable by group or others. "
                "Run: chmod 600 .env"
            )
            try:
                log_event("warning", "security",
                          ".env file has insecure permissions (readable by group/others)")
            except Exception:
                pass
    except OSError:
        pass


_check_env_file_permissions()
_LIVE_REQUOTE_ONLY_KEYS = {
    "SPREAD_BPS",
    "BASE_SPREAD_BPS",
    "MIN_EDGE_BPS",
    "MIN_SPREAD_BPS",
    "MAX_SPREAD_BPS",
    "VOLATILITY_WINDOW_HOURS",
    "SKEW_INTENSITY",
    "MAX_POSITION_XCH",
    "DYNAMIC_SPREAD_ENABLED",
    "INVENTORY_ENABLED",
    "COMPETITOR_AWARE_ENABLED",
    "DBX_MAX_SPREAD_BPS",
}


def get_app_version() -> str:
    """Return the packaged app version from the local project metadata."""
    global _APP_VERSION_CACHE
    if _APP_VERSION_CACHE:
        return _APP_VERSION_CACHE

    version_files = (
        os.path.join(_APP_ROOT, "src-tauri", "tauri.conf.json"),
        os.path.join(_APP_ROOT, "package.json"),
    )
    for path in version_files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            version = str(data.get("version", "")).strip()
            if version:
                _APP_VERSION_CACHE = version
                return _APP_VERSION_CACHE
        except Exception:
            continue

    _APP_VERSION_CACHE = "unknown"
    return _APP_VERSION_CACHE


def _get_spacescan_plan_advice() -> Dict[str, object]:
    """Estimate sensible Spacescan plan guidance for this bot profile."""
    loop_seconds = max(15, int(getattr(cfg, "LOOP_SECONDS", 90) or 90))
    balance_every_loops = max(1, int(getattr(cfg, "SPACESCAN_BALANCE_CHECK_EVERY_N", 10) or 10))

    loops_per_day = 86400 / float(loop_seconds)
    balance_checks_per_day = loops_per_day / float(balance_every_loops)

    # Paid mode performs both XCH and CAT balance checks on each scheduled pass.
    balance_calls_month = int(round(balance_checks_per_day * 2 * 30))
    token_context_calls_month = 120  # ~4 calls/day from cached token context refreshes
    baseline_paid_monthly = balance_calls_month + token_context_calls_month

    if baseline_paid_monthly <= _SPACESCAN_PUBLIC_PLANS["hobbyist"]["requests_per_month"]:
        minimum_paid_tier = "hobbyist"
    elif baseline_paid_monthly <= _SPACESCAN_PUBLIC_PLANS["builder"]["requests_per_month"]:
        minimum_paid_tier = "builder"
    else:
        minimum_paid_tier = "startup"

    if baseline_paid_monthly <= 4000:
        recommended_paid_tier = "hobbyist"
    elif baseline_paid_monthly <= 30000:
        recommended_paid_tier = "builder"
    else:
        recommended_paid_tier = "startup"

    if recommended_paid_tier == "startup":
        message = (
            f"At a {loop_seconds}s loop, this bot would use about "
            f"{baseline_paid_monthly:,} paid-plan calls/month before fills. "
            "Startup is the safer fit for this profile."
        )
    elif minimum_paid_tier == "hobbyist" and recommended_paid_tier == "builder":
        message = (
            f"At a {loop_seconds}s loop, this bot would use about "
            f"{baseline_paid_monthly:,} paid-plan calls/month before fills. "
            "Hobbyist can work, but Builder gives safer 24/7 headroom for restarts, "
            "fills, and on-chain sanity checks."
        )
    else:
        message = (
            f"At a {loop_seconds}s loop, this bot would use about "
            f"{baseline_paid_monthly:,} paid-plan calls/month before fills. "
            f"{_SPACESCAN_PUBLIC_PLANS[recommended_paid_tier]['label']} is the sensible fit."
        )

    return {
        "loop_seconds": loop_seconds,
        "balance_every_loops": balance_every_loops,
        "balance_calls_month": balance_calls_month,
        "token_context_calls_month": token_context_calls_month,
        "baseline_paid_monthly": baseline_paid_monthly,
        "fill_verify_call_cost": 1,
        "topup_cross_check_call_cost": 1,
        "minimum_paid_tier": minimum_paid_tier,
        "recommended_paid_tier": recommended_paid_tier,
        "message": message,
        "plans": _SPACESCAN_PUBLIC_PLANS,
    }


def _get_spacescan_market_context(asset_id: str = "", ticker_id: str = "",
                                  decimals: int = 3, *,
                                  executable_mid_price: float = 0.0) -> dict:
    """Return cached Spacescan-assisted token context for live UI decisions.

    This is deliberately *not* a live pricing feed. Dexie + Tibet remain the
    executable market sources. Spacescan contributes token health, activity,
    supply, and explorer-price sanity checks.
    """
    context = {
        "enabled": bool(getattr(cfg, "SPACESCAN_ENABLED", True)),
        "has_data": False,
        "holder_count": 0,
        "activity_count": 0,
        "activity_level": "unknown",
        "risk_level": "unknown",
        "confidence": "low",
        "price_xch": 0.0,
        "price_usd": 0.0,
        "circulating_supply": 0.0,
        "total_supply": 0.0,
        "price_gap_bps": 0.0,
        "regime_hint": "unknown",
        "message": "Spacescan token context not loaded",
    }
    if not asset_id:
        return context

    try:
        from database import get_market_analysis_cache
        spacescan = get_market_analysis_cache(asset_id, "spacescan") or {}
        analysis = get_market_analysis_cache(asset_id, "full_analysis") or {}
        if not spacescan:
            # Spacescan raw data not cached at all — return empty context rather than
            # triggering a full background data collection here. Smart Defaults
            # populates this cache when explicitly run.
            return context
        # full_analysis may have expired (30min TTL) while spacescan data (24hr TTL)
        # is still valid. Use spacescan raw data directly, fall back to analysis
        # for derived fields (activity_level, risk_level) when available.
        health = (analysis.get("token_health") or {}) if isinstance(analysis, dict) else {}

        context["has_data"] = bool(spacescan.get("has_data"))
        context["token_preview_url"] = str(spacescan.get("token_preview_url", "") or "")
        context["holder_count"] = int(spacescan.get("holder_count", 0) or 0)
        context["activity_count"] = int(spacescan.get("activity_count", 0) or 0)
        # Derive activity_level and risk_level from raw spacescan data when
        # full_analysis has expired but spacescan cache is still valid.
        if health:
            context["activity_level"] = str(health.get("activity_level", "unknown") or "unknown")
            context["risk_level"] = str(health.get("risk_level", "unknown") or "unknown")
            context["confidence"] = str(health.get("confidence", "low") or "low")
        else:
            # Derive from raw spacescan data inline (same logic as _analyze_token_health)
            hc = context["holder_count"]
            ac = int(spacescan.get("activity_count", 0) or 0)
            if hc >= 200:
                context["risk_level"] = "healthy"
            elif hc >= 50:
                context["risk_level"] = "moderate"
            elif hc > 0:
                context["risk_level"] = "thin"
            else:
                context["risk_level"] = "unknown"
            if ac >= 500:
                context["activity_level"] = "active"
            elif ac >= 100:
                context["activity_level"] = "moderate"
            elif ac > 0:
                context["activity_level"] = "quiet"
            elif hc >= 500:
                # Activity endpoint returned 0 but token has many holders —
                # likely a Spacescan data gap, not genuinely zero activity.
                # Infer from holder count as a proxy.
                context["activity_level"] = "active"
            elif hc >= 100:
                context["activity_level"] = "moderate"
            elif hc > 0:
                context["activity_level"] = "quiet"
            else:
                context["activity_level"] = "unknown"
            context["confidence"] = "medium" if hc > 0 else "low"
        context["price_xch"] = float(spacescan.get("price_xch", 0) or 0)
        context["price_usd"] = float(spacescan.get("price_usd", 0) or 0)
        context["circulating_supply"] = float(spacescan.get("circulating_supply", 0) or 0)
        context["total_supply"] = float(spacescan.get("total_supply", 0) or 0)

        mid = float(executable_mid_price or 0)
        explorer_px = context["price_xch"]
        if mid > 0 and explorer_px > 0:
            context["price_gap_bps"] = round(abs(explorer_px - mid) / mid * 10000, 2)

        risk = context["risk_level"].lower()
        activity = context["activity_level"].lower()
        if risk in {"risky", "thin"} and activity in {"dormant", "quiet"}:
            context["regime_hint"] = "fragile"
        elif risk == "healthy" and activity in {"active", "moderate"}:
            context["regime_hint"] = "established"
        elif activity in {"dormant", "quiet"}:
            context["regime_hint"] = "quiet"
        elif risk in {"risky", "thin"}:
            context["regime_hint"] = "thin"
        else:
            context["regime_hint"] = "balanced"

        holders = context["holder_count"]
        activities = context["activity_count"]
        msg = f"{holders} holders, {activity} activity, {risk} risk"
        if context["price_gap_bps"] > 0:
            msg += f", explorer gap {context['price_gap_bps'] / 100:.1f}%"
        context["message"] = msg
    except Exception as e:
        context["message"] = f"Spacescan context unavailable: {e}"

    return context


def _get_live_requote_notice(changed_keys):
    """Explain when a config change only affects future quotes.

    Quote-affecting risk/spread controls should never force a live migration
    from the GUI. Existing offers stay live; the new values are picked up by
    future requotes and newly-created offers.
    """
    try:
        if not bot or not bot.is_running():
            return None
    except Exception:
        return None

    keys = sorted({str(k) for k in (changed_keys or []) if str(k) in _LIVE_REQUOTE_ONLY_KEYS})
    if not keys:
        return None

    return {
        "keys": keys,
        "apply_mode": "next_requote",
        "warning": (
            "Saved without live offer migration — existing offers stay live and "
            "the change will take effect on future requotes and new offers."
        ),
    }


def _is_loopback_addr(addr: str) -> bool:
    addr = str(addr or "").strip().lower()
    if addr in {"localhost"}:
        return True
    try:
        import ipaddress
        # Handles 127.0.0.0/8, ::1, ::ffff:127.x.x.x and all other loopback forms
        return ipaddress.ip_address(addr).is_loopback
    except (ValueError, AttributeError):
        return False


def _has_valid_local_token() -> bool:
    supplied = (
        request.headers.get(_LOCAL_API_TOKEN_HEADER, "")
        or request.args.get(_LOCAL_API_QUERY_PARAM, "")
    )
    supplied = str(supplied or "")
    return bool(supplied) and secrets.compare_digest(supplied, _LOCAL_API_TOKEN)


def _get_sage_signing_block_reason():
    """Return a message when the active Sage key is present but cannot sign."""
    try:
        if get_wallet_type() != "sage":
            return None
    except Exception:
        return None

    try:
        from wallet_sage import get_current_key
        key = get_current_key() or {}
        if not key.get("has_secrets", False):
            fp = key.get("fingerprint")
            msg = "Active Sage wallet is watch-only and cannot sign offers"
            if fp:
                msg += f" (fingerprint {fp})"
            return msg
    except Exception:
        return None

    return None


def _serve_bootstrapped_html(filename: str):
    """Serve HTML with the local runtime token injected for same-machine use."""
    gui_dir = _APP_ROOT
    path = os.path.join(gui_dir, filename)
    with open(path, "r", encoding="utf-8") as f:
        html_doc = f.read()

    bootstrap = (
        "<script>"
        f"window.__BOT_LOCAL_TOKEN={json.dumps(_LOCAL_API_TOKEN)};"
        f"window.__BOT_LOCAL_TOKEN_HEADER={json.dumps(_LOCAL_API_TOKEN_HEADER)};"
        "</script>"
    )
    if "</head>" in html_doc:
        html_doc = html_doc.replace("</head>", bootstrap + "\n</head>", 1)
    else:
        html_doc = bootstrap + html_doc
    return Response(html_doc, mimetype="text/html")


class _QuietRequestFilter(logging.Filter):
    """Filter out repetitive polling requests from Werkzeug's access log."""
    def filter(self, record):
        msg = record.getMessage()
        # Werkzeug log format: '127.0.0.1 - - [date] "GET /api/status HTTP/1.1" 200 -'
        for endpoint in _QUIET_ENDPOINTS:
            if endpoint in msg:
                return False  # Suppress this log line
        return True  # Show everything else


# Apply the filter to Werkzeug's logger
logging.getLogger("werkzeug").addFilter(_QuietRequestFilter())


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _bps_to_pct(val):
    """Convert a BPS value to a formatted % string."""
    try:
        n = float(val) / 100
        if n < 1:
            return f"{n:.2f}%"
        return f"{n:.1f}%"
    except (ValueError, TypeError):
        return str(val)


def _history_age_label(timestamp_value: str) -> str:
    """Convert an ISO timestamp into the short relative label used by the GUI."""
    age = "Recently"
    try:
        if timestamp_value:
            dt = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
            age_secs = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
            if age_secs < 60:
                age = f"{int(age_secs)}s ago"
            elif age_secs < 3600:
                age = f"{int(age_secs / 60)}m ago"
            elif age_secs < 86400:
                age = f"{age_secs / 3600:.1f}h ago"
            else:
                age = f"{age_secs / 86400:.1f}d ago"
    except Exception:
        age = "Recently"
    return age


def _build_fill_history_for_gui(asset_id: str, limit: int = 20) -> list:
    """Return DB-backed fill history in the shape the Offers history tab expects."""
    if not asset_id:
        return []

    history_by_trade_id = {}
    since_cutoff = _get_run_history_cutoff()
    try:
        from database import get_fills
        fills = get_fills(
            cat_asset_id=asset_id,
            since=since_cutoff,
            limit=max(limit * 3, 60),
        )
    except Exception:
        fills = []

    cat_name = _active_cat.get("name") or getattr(cfg, "CAT_NAME", "") or "CAT"

    def _add_history_row(row: dict):
        trade_id = str(row.get("trade_id") or "").strip()
        if not trade_id:
            return

        dexie_id = str(row.get("dexie_id") or "").strip()
        dexie_link = f"https://dexie.space/offers/{dexie_id}" if dexie_id else ""

        if trade_id in history_by_trade_id:
            # Already have this trade — just patch in the Dexie link if the
            # fills table didn't have it (dexie_id lives on the offers row,
            # not the fills row, so the first pass often leaves it blank).
            if dexie_link and not history_by_trade_id[trade_id].get("dexie_link"):
                history_by_trade_id[trade_id]["dexie_link"] = dexie_link
            return

        filled_at = (
            row.get("filled_at")
            or row.get("timestamp")
            or row.get("created_at")
            or ""
        )
        history_by_trade_id[trade_id] = {
            "trade_id": trade_id,
            "full_id": trade_id,
            "side": row.get("side", ""),
            "status": "FILLED",
            "price": str(row.get("price_xch", row.get("price", ""))),
            "size_xch": str(row.get("size_xch", "")),
            "size_cat": str(row.get("size_cat", "")),
            "tier": row.get("tier", "unknown"),
            "coin_id": row.get("coin_id", ""),
            "cat_name": cat_name,
            "age": _history_age_label(filled_at),
            "filled_at": filled_at,
            "dexie_link": dexie_link,
            "_sort_key": str(filled_at),
        }

    for row in fills:
        _add_history_row(row)

    try:
        conn = get_connection()
        filled_offer_rows = conn.execute(
            """SELECT trade_id, side, price_xch, size_xch, size_cat, tier,
                      coin_id, filled_at, created_at, dexie_id
               FROM offers
               WHERE status='filled' AND cat_asset_id=?
                 AND (? IS NULL OR COALESCE(filled_at, created_at) >= ?)
               ORDER BY COALESCE(filled_at, created_at) DESC
               LIMIT ?""",
            (asset_id, since_cutoff, since_cutoff, max(limit * 4, 100)),
        ).fetchall()
        for row in filled_offer_rows:
            _add_history_row(dict(row))
    except Exception:
        pass

    history = sorted(
        history_by_trade_id.values(),
        key=lambda item: item.get("_sort_key", ""),
        reverse=True,
    )
    for item in history:
        item.pop("_sort_key", None)
    return history[:limit]


def _get_live_local_offer_edges(asset_id: str) -> dict:
    """Get our current best live bid/ask from wallet-open offers.

    Uses wallet-open trade IDs when possible so stale DB rows do not distort the
    Market Intel "best live" display. Falls back to DB-open rows only if wallet
    sync is unavailable.
    """
    result = {
        "our_best_bid": Decimal("0"),
        "our_best_ask": Decimal("0"),
        "our_open_buys": 0,
        "our_open_sells": 0,
        "source": "db_open_offers",
    }
    if not asset_id:
        return result

    trade_ids = None
    if bot and getattr(bot, "offer_manager", None):
        try:
            wallet_open_buys, wallet_open_sells, _ = bot.offer_manager.sync_from_wallet()
            trade_ids = [
                o.get("trade_id", "")
                for o in (wallet_open_buys + wallet_open_sells)
                if o.get("trade_id")
            ]
            result["our_open_buys"] = len(wallet_open_buys)
            result["our_open_sells"] = len(wallet_open_sells)
            result["source"] = "wallet_sync"
        except Exception:
            trade_ids = None

    conn = get_connection()
    params = [asset_id]
    query = (
        "SELECT side, MIN(CAST(price_xch AS REAL)) AS min_price, "
        "MAX(CAST(price_xch AS REAL)) AS max_price, COUNT(*) AS cnt "
        "FROM offers WHERE status='open' AND cat_asset_id=?"
    )
    if trade_ids is not None:
        if not trade_ids:
            return result
        placeholders = ",".join("?" for _ in trade_ids)
        query += f" AND trade_id IN ({placeholders})"
        params.extend(trade_ids)
    query += " GROUP BY side"

    rows = conn.execute(query, params).fetchall()
    for row in rows:
        side = row["side"]
        if side == "buy":
            result["our_best_bid"] = Decimal(str(row["max_price"] or 0))
            if trade_ids is None:
                result["our_open_buys"] = int(row["cnt"] or 0)
        elif side == "sell":
            result["our_best_ask"] = Decimal(str(row["min_price"] or 0))
            if trade_ids is None:
                result["our_open_sells"] = int(row["cnt"] or 0)
    return result


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)

# The bot loop instance (created at startup)
bot: BotLoop = None

# Active CAT selection — updated when user picks a CAT from the dropdown.
# Stores wallet_id, asset_id, name, decimals so /api/status can fetch
# the correct balance regardless of what's in .env.
# Initialize from .env so pricing works immediately on startup (before user selects a CAT)
_active_cat = {
    "wallet_id": getattr(cfg, "CAT_WALLET_ID", None),
    "asset_id": getattr(cfg, "CAT_ASSET_ID", None) or None,
    "name": getattr(cfg, "CAT_NAME", None) or None,
    "decimals": getattr(cfg, "CAT_DECIMALS", None),
    "ticker_id": getattr(cfg, "CAT_TICKER_ID", None) or None,
}
# Lock for multi-key mutations of _active_cat so readers never see a
# half-updated pair (e.g. asset_id from the new CAT but decimals from the old).
_active_cat_lock = threading.Lock()
# Auto-fix: Dexie ticker format is "{CAT}_XCH" e.g. "SBX_XCH" (V1 confirmed)
if _active_cat["ticker_id"] and "_" not in _active_cat["ticker_id"]:
    _active_cat["ticker_id"] = f"{_active_cat['ticker_id']}_XCH"
    cfg.update("CAT_TICKER_ID", _active_cat["ticker_id"])
print(f"[STARTUP] _active_cat initialized from .env: {_active_cat}")

# Auto-resolve CAT metadata (TIBET_PAIR_ID, CAT_TICKER_ID, CAT_NAME) at startup.
# Runs in a background thread so it doesn't block Flask startup.
# Clears TIBET_PAIR_ID first — it may belong to a previous token if the user
# switched CATs via the GUI in a prior session and then restarted. The resolver
# will fill in the correct pair for the current CAT_ASSET_ID.
def _background_cat_resolve():
    try:
        from cat_resolver import resolve_and_apply as _resolve_cat
        # Clear stale TIBET_PAIR_ID before resolving — ensures we always get
        # the pair for the currently configured CAT, not a leftover from the last session.
        cfg.update("TIBET_PAIR_ID", "")
        meta = _resolve_cat(cfg)
        if meta:
            # Keep _active_cat in sync with any newly resolved fields
            with _active_cat_lock:
                if meta.get("ticker_id") and not _active_cat.get("ticker_id"):
                    _active_cat["ticker_id"] = meta["ticker_id"]
                if meta.get("name") and (not _active_cat.get("name") or _active_cat.get("name") == "MZ"):
                    _active_cat["name"] = meta["name"]
            print(f"[STARTUP] CAT metadata resolved: pair_id={str(meta.get('pair_id') or '')[:20]}... "
                  f"ticker={meta.get('ticker_id')} name={meta.get('name')}")
    except Exception as e:
        print(f"[STARTUP] CAT metadata resolve failed (non-critical): {e}")

import threading as _threading
_threading.Thread(target=_background_cat_resolve, daemon=True, name="cat-resolver").start()

# Track when the GUI log panel was last cleared.
# Events older than this timestamp are hidden from the GUI but still
# available via the debug log download (preserves full history).
# Loaded from database on startup so it survives restarts.
_logs_cleared_at = None
_session_start_time = None  # Set at app startup — logs older than this are hidden
_run_history_cutoff = None  # Set when the user explicitly starts a fresh run
if not hasattr(cfg, "RUN_HISTORY_CUTOFF"):
    cfg.RUN_HISTORY_CUTOFF = None

# Persists the user's "Start Fresh" choice across process restarts so the
# resume modal doesn't reappear.  Uses a flag file rather than memory so
# it survives the app being fully closed and reopened.
import os as _os
_FRESH_START_FLAG = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".fresh_start_chosen")

def _fresh_start_is_set() -> bool:
    return _os.path.exists(_FRESH_START_FLAG)

def _fresh_start_set():
    try:
        open(_FRESH_START_FLAG, "w").close()
    except Exception:
        pass

def _fresh_start_clear():
    try:
        if _os.path.exists(_FRESH_START_FLAG):
            _os.remove(_FRESH_START_FLAG)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Event Bus (for SSE push to GUI)
# ---------------------------------------------------------------------------

class EventBus:
    """Simple event bus for Server-Sent Events (SSE).

    Modules call emit() to push events. Connected GUI clients
    receive them instantly via the /api/events SSE endpoint.
    """

    def __init__(self):
        self._subscribers: list = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Create a new subscriber queue."""
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """Remove a subscriber."""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, event_type: str, data: dict):
        """Push an event to all subscribers."""
        msg = {"type": event_type, "data": data, "ts": time.time()}
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    def alert(self, alert_id: str, severity: str, title: str, message: str,
              action: str = None, action_label: str = None):
        """Convenience: set a persistent alert and emit it."""
        if hasattr(self, '_alert_store'):
            self._alert_store.set_alert(alert_id, severity, title, message, action, action_label)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class AlertStore:
    """Persistent alerts that require user acknowledgment.

    Unlike the activity feed (rolling, ephemeral), alerts persist until
    the user dismisses them. Used for important state changes the user
    needs to know about: pricing strategy, position limits, side disabled, etc.
    """

    def __init__(self):
        self._alerts: Dict[str, dict] = {}  # keyed by alert_id
        self._lock = threading.Lock()

    def set_alert(self, alert_id: str, severity: str, title: str, message: str,
                  action: str = None, action_label: str = None):
        """Create or update an alert. Severity: 'error', 'warning', 'info', 'success'."""
        with self._lock:
            self._alerts[alert_id] = {
                "id": alert_id,
                "severity": severity,
                "title": title,
                "message": message,
                "action": action,  # optional action ID handled client-side
                "action_label": action_label,  # button text
                "created_at": time.time(),
                "dismissed": False
            }
        # Push to GUI via SSE
        events.emit("alert", self._alerts[alert_id])

    def dismiss(self, alert_id: str):
        """Mark an alert as dismissed."""
        with self._lock:
            if alert_id in self._alerts:
                self._alerts[alert_id]["dismissed"] = True

    def clear(self, alert_id: str):
        """Remove an alert entirely (e.g. condition resolved)."""
        with self._lock:
            self._alerts.pop(alert_id, None)
        events.emit("alert_cleared", {"id": alert_id})

    def get_active(self) -> list:
        """Get all non-dismissed alerts."""
        with self._lock:
            return [a for a in self._alerts.values() if not a["dismissed"]]


events = EventBus()
alerts = AlertStore()
# Wire alerts to event bus for accessing from bot_loop via events._alert_store
events._alert_store = alerts

# Hook log_event() to push to the live console via SSE
try:
    from database import set_log_sse_callback
    set_log_sse_callback(events.emit)
    print("  [SSE] log_event → SSE callback registered ✓", flush=True)
except Exception as e:
    print(f"  [SSE] ⚠️ Failed to register log_event callback: {e}", flush=True)


def create_bot() -> BotLoop:
    """Create and return the bot loop instance."""
    global bot
    bot = BotLoop()
    # Wire up event bus to bot loop for push updates
    bot._event_bus = events
    # Inject spacescan getter so SSE dashboard_update events include spacescan metrics.
    # This avoids a circular import: api_server → bot_loop is the import direction,
    # so we inject the callable after construction instead.
    bot._spacescan_context_getter = _get_spacescan_market_context
    bot.runtime_monitor.start()
    return bot


# ---------------------------------------------------------------------------
# GUI Route
# ---------------------------------------------------------------------------

@app.after_request
def add_no_cache_headers(response):
    """Prevent browser from caching HTML and API responses.

    This fixes the 'stuck GUI after restart' problem — without these headers
    the browser serves a stale cached page that can't connect to the new server.
    """
    if response.content_type and ("text/html" in response.content_type
                                   or "application/json" in response.content_type):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # CORS — restrict to loopback origin only (prevents any webpage from reading API)
    response.headers["Access-Control-Allow-Origin"] = "http://127.0.0.1:5000"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Bot-Local-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    if response.content_type and "text/html" in response.content_type:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://icons.dexie.space https://*.spacescan.io https://cdn.spacescan.io https://assets.spacescan.io; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    return response


@app.before_request
def enforce_local_runtime_guard():
    """Keep the control plane loopback-only and require a per-run token for writes."""
    path = request.path or ""

    if path.startswith("/api/debug/"):
        return jsonify({"error": "debug_routes_disabled"}), 404

    protected_pages = {"/", "/console", "/api/events"}
    if path.startswith("/api/") or path in protected_pages:
        if not _is_loopback_addr(request.remote_addr):
            if path.startswith("/api/"):
                return jsonify({"error": "loopback_only"}), 403
            return Response("Loopback only", status=403, mimetype="text/plain")

    if path == "/api/events" and not _has_valid_local_token():
        return Response("Unauthorized", status=401, mimetype="text/plain")

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/api/"):
        requires_token = path not in _TOKEN_EXEMPT_WRITE_ROUTES
        if requires_token and not _has_valid_local_token():
            return jsonify({"error": "unauthorized"}), 401
        if path not in _RATE_LIMIT_EXEMPT_WRITE_ROUTES and _is_rate_limited(path):
            return jsonify({"error": "rate_limited", "message": "Too many requests"}), 429


@app.route("/")
def serve_gui():
    """Serve the bot GUI HTML file."""
    return _serve_bootstrapped_html("bot_gui.html")


@app.route("/console")
def serve_console():
    """Serve the live console popup window."""
    return _serve_bootstrapped_html("bot_console.html")


@app.route("/brand/<path:filename>")
@app.route("/assets/<path:filename>")
def serve_brand_asset(filename: str):
    """Serve brand assets used by the GUI from the assets/ folder."""
    gui_dir = _APP_ROOT
    assets_dir = os.path.join(gui_dir, "assets")
    allowed = {
        "bot_icon_new.png",
        "favicon.ico",
        "sage_logo_official.png",
        "dexie_logo_official.png",
        "dexie_logo_official.ico",
        "tibetswap_logo_official.png",
        "MonkeyZoo_Logo.png",
        "monkeyzoo-logo-1.gif",
        "spacescan-logo-192.webp",
    }
    if filename not in allowed:
        return Response("Not Found", status=404, mimetype="text/plain")
    # Try assets/ folder first, fall back to app root for backward compat
    if os.path.isfile(os.path.join(assets_dir, filename)):
        return send_from_directory(assets_dir, filename)
    return send_from_directory(gui_dir, filename)


def _get_session_pending_verification_count() -> int:
    """Count unverified closures in the current bot session."""
    if not bot or not getattr(bot, "_start_time", 0):
        return 0
    try:
        since_iso = datetime.fromtimestamp(bot._start_time, timezone.utc).isoformat()
        row = get_connection().execute(
            """SELECT COUNT(*) as cnt
               FROM events
               WHERE event_type='offer_closed_unverified'
                 AND timestamp >= ?""",
            (since_iso,)
        ).fetchone()
        return int((row["cnt"] if row else 0) or 0)
    except Exception:
        return 0


def _get_run_history_cutoff() -> str:
    """Return the current fresh-run history cutoff, if one exists."""
    return _run_history_cutoff or getattr(cfg, "RUN_HISTORY_CUTOFF", None)


def _restore_run_history_cutoff_from_events() -> str:
    """Restore the latest fresh-run cutoff from persisted events.

    Fresh-run resets are logged into the events table, so we can recover the
    most recent cutoff after an app restart and keep history/PnL scoped to the
    current run instead of reverting to lifetime stats.
    """
    global _run_history_cutoff
    try:
        row = get_connection().execute(
            """SELECT timestamp
               FROM events
               WHERE event_type IN ('session_fresh_start', 'fresh_start_cleanup')
               ORDER BY id DESC
               LIMIT 1"""
        ).fetchone()
        cutoff = str((row["timestamp"] if row else "") or "").strip()
        _run_history_cutoff = cutoff or None
        cfg.RUN_HISTORY_CUTOFF = _run_history_cutoff
        return _run_history_cutoff
    except Exception:
        return None


def _reset_runtime_session_stats() -> Dict:
    """Reset in-memory per-run stats for a new bot/session start."""
    reset_summary = {
        "market_intel_reset": False,
        "splash_reset": False,
        "splash_incoming_cleared": 0,
    }

    try:
        from database import clear_splash_incoming
        reset_summary["splash_incoming_cleared"] = int(clear_splash_incoming() or 0)
    except Exception:
        reset_summary["splash_incoming_cleared"] = 0

    if not bot:
        return reset_summary

    try:
        if getattr(bot, "market_intel", None):
            bot.market_intel.reset_session_stats()
            reset_summary["market_intel_reset"] = True
    except Exception:
        reset_summary["market_intel_reset"] = False

    try:
        if getattr(bot, "splash_manager", None):
            bot.splash_manager.reset_session_stats()
            reset_summary["splash_reset"] = True
    except Exception:
        reset_summary["splash_reset"] = False

    try:
        events.emit("splash_incoming", bot.get_splash_receive_stats())
    except Exception:
        pass

    return reset_summary


def _reset_fresh_run_session(clear_coins: bool = False,
                             clear_price_history: bool = False,
                             clear_inventory: bool = False,
                             cancel_open_offers: bool = False,
                             reason: str = "fresh_start") -> Dict:
    """Reset session-facing bot state for a brand new run.

    This intentionally clears fill/PnL state immediately when the operator
    chooses Start Fresh, so the Offers history and PnL panels do not carry the
    previous run forward while the user prepares a new setup.
    """
    global _run_history_cutoff, _session_start_time

    from database import _sqlite_ts
    reset_at = _sqlite_ts(datetime.now(timezone.utc))
    summary = {
        "reset_at": reset_at,
        "fills_cleared": 0,
        "round_trips_cleared": 0,
        "coins_cleared": 0,
        "open_offers_cancelled": 0,
        "price_history_cleared": False,
        "inventory_cleared": False,
    }

    conn = get_connection()
    try:
        summary["fills_cleared"] = int(
            (conn.execute("SELECT COUNT(*) as cnt FROM fills").fetchone()["cnt"]) or 0
        )

        has_round_trips = bool(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='round_trips'"
        ).fetchone())
        if has_round_trips:
            summary["round_trips_cleared"] = int(
                (conn.execute("SELECT COUNT(*) as cnt FROM round_trips").fetchone()["cnt"]) or 0
            )

        if clear_coins:
            summary["coins_cleared"] = int(
                (conn.execute("SELECT COUNT(*) as cnt FROM coins").fetchone()["cnt"]) or 0
            )

        conn.execute("DELETE FROM fills")
        if has_round_trips:
            conn.execute("DELETE FROM round_trips")
        if clear_coins:
            conn.execute("DELETE FROM coins")
        if cancel_open_offers:
            cursor = conn.execute("UPDATE offers SET status='cancelled' WHERE status='open'")
            summary["open_offers_cancelled"] = int(cursor.rowcount or 0)
        if clear_price_history:
            try:
                conn.execute("DELETE FROM price_history")
                summary["price_history_cleared"] = True
            except Exception:
                summary["price_history_cleared"] = False
        if clear_inventory:
            try:
                conn.execute("DELETE FROM inventory_snapshots")
                summary["inventory_cleared"] = True
            except Exception:
                summary["inventory_cleared"] = False
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    _run_history_cutoff = reset_at
    cfg.RUN_HISTORY_CUTOFF = reset_at
    # Also advance the session-start cutoff so /api/logs and the dashboard
    # logs section only show events from THIS fresh run, not the original
    # server startup.
    _session_start_time = reset_at

    if bot and getattr(bot, "risk_manager", None):
        try:
            bot.risk_manager.reset_position()
        except Exception:
            pass

    stats_reset = _reset_runtime_session_stats()
    summary.update(stats_reset)

    if reason:
        details = (
            f"Fresh run reset at {reset_at}: cleared {summary['fills_cleared']} fills, "
            f"{summary['round_trips_cleared']} round-trips, "
            f"{summary['splash_incoming_cleared']} Splash incoming offers"
        )
        if clear_coins:
            details += f", {summary['coins_cleared']} coins"
        if cancel_open_offers:
            details += f", cancelled {summary['open_offers_cancelled']} open offers"
        log_event("info", reason, details)

    return summary


@app.route("/favicon.ico")
def favicon():
    gui_dir = _APP_ROOT
    assets_dir = os.path.join(gui_dir, "assets")
    # Try assets/ first, then app root for backward compat
    for d in (assets_dir, gui_dir):
        if os.path.isfile(os.path.join(d, "favicon.ico")):
            return send_from_directory(d, "favicon.ico")
        if os.path.isfile(os.path.join(d, "bot_icon_new.ico")):
            return send_from_directory(d, "bot_icon_new.ico")
    return Response(status=404)


def _is_allowed_external_url(raw_url: str) -> bool:
    """Allow only absolute http/https URLs for desktop external-link opens."""
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _launch_external_url(raw_url: str) -> bool:
    """Best-effort launch in the OS default browser without touching bot state."""
    url = str(raw_url or "").strip()
    if not _is_allowed_external_url(url):
        return False
    try:
        return webbrowser.open(url, new=2)
    except Exception:
        return False


@app.route("/api/open-external", methods=["POST"])
def api_open_external():
    """Open a vetted external URL in the user's default browser.

    POST-only to prevent CSRF via cross-origin GET from any webpage.
    Requires the per-run local token (enforced by before_request).
    """
    if not _is_loopback_addr(request.remote_addr):
        return jsonify({"success": False, "error": "loopback_only"}), 403

    payload = request.get_json(silent=True)
    raw_url = (
        (payload or {}).get("url")
        if isinstance(payload, dict)
        else None
    )
    url = str(raw_url or "").strip()

    if not _is_allowed_external_url(url):
        if request.method == "GET":
            return Response("Only absolute http/https URLs are allowed", status=400, mimetype="text/plain")
        return jsonify({"success": False, "error": "Only absolute http/https URLs are allowed"}), 400

    if not _launch_external_url(url):
        if request.method == "GET":
            return Response("Could not open URL in the default browser", status=500, mimetype="text/plain")
        return jsonify({"success": False, "error": "Could not open URL in the default browser"}), 500

    if request.method == "GET":
        return Response("Opened external link", mimetype="text/plain")
    return jsonify({"success": True, "url": url})


@app.route("/api/open-data-folder", methods=["POST"])
def api_open_data_folder():
    """Reveal the per-user data directory in the OS file manager.

    Useful for support: users can click this button and then attach
    crash.log / bot.db / bot_superlog_*.log to a bug report.
    Loopback only and POST-only to prevent CSRF.
    """
    if not _is_loopback_addr(request.remote_addr):
        return jsonify({"success": False, "error": "loopback_only"}), 403

    try:
        from user_paths import data_dir as _dd
        folder = _dd()
    except Exception as e:
        return jsonify({"success": False, "error": f"data dir unavailable: {e}"}), 500

    if not os.path.isdir(folder):
        return jsonify({"success": False, "error": f"data dir does not exist: {folder}"}), 500

    try:
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess as _sp
            _sp.Popen(["open", folder])
        else:
            import subprocess as _sp
            _sp.Popen(["xdg-open", folder])
    except Exception as e:
        return jsonify({"success": False, "error": f"could not open folder: {e}"}), 500

    return jsonify({"success": True, "folder": folder})


@app.route("/api/crash-log", methods=["GET"])
def api_crash_log():
    """Return the most recent crash.log contents (truncated) so the GUI
    can show users why the app failed last time, with a button to copy
    or email it to support.

    Loopback only. Returns a bounded amount of text (256 KiB) and never
    follows symlinks.
    """
    if not _is_loopback_addr(request.remote_addr):
        return jsonify({"success": False, "error": "loopback_only"}), 403

    try:
        from user_paths import crash_log_file, data_dir as _dd
        path = crash_log_file()
        data_folder = _dd()
    except Exception as e:
        return jsonify({"success": False, "error": f"data dir unavailable: {e}"}), 500

    if not os.path.isfile(path):
        return jsonify({
            "success": True,
            "exists": False,
            "path": path,
            "folder": data_folder,
            "content": "",
            "size": 0,
        })

    try:
        st = os.stat(path)
    except OSError as e:
        return jsonify({"success": False, "error": f"stat failed: {e}"}), 500

    MAX_BYTES = 256 * 1024  # 256 KiB cap — plenty for a traceback
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            if st.st_size > MAX_BYTES:
                fh.seek(st.st_size - MAX_BYTES)
                content = "[... truncated — older content omitted ...]\n" + fh.read()
            else:
                content = fh.read()
    except OSError as e:
        return jsonify({"success": False, "error": f"read failed: {e}"}), 500

    return jsonify({
        "success": True,
        "exists": True,
        "path": path,
        "folder": data_folder,
        "content": content,
        "size": st.st_size,
        "mtime": st.st_mtime,
    })


# ---------------------------------------------------------------------------
# Version check against GitHub releases
# ---------------------------------------------------------------------------
#
# The release API URL is configurable via the RELEASES_API_URL env var so
# it can be changed without a redeploy, or disabled entirely (empty string).
# It must return a GitHub-style JSON object with "tag_name" and "html_url".
# We cache the result for 6 hours to avoid hammering GitHub's unauthenticated
# rate limit (60 req/hr per IP).

_UPDATE_CHECK_CACHE: dict = {"at": 0.0, "data": None}
_UPDATE_CHECK_TTL = 6 * 3600  # 6 hours


def _parse_semver(tag: str):
    """Parse 'v1.2.3' / '1.2.3' into a (major, minor, patch) int tuple.

    Returns None if the tag doesn't look like a semver. Extra pre-release /
    build metadata after the patch number is ignored for comparison purposes.
    """
    if not tag:
        return None
    s = str(tag).strip().lstrip("vV")
    head = s.split("-", 1)[0].split("+", 1)[0]
    parts = head.split(".")
    if len(parts) < 1:
        return None
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


@app.route("/api/check-update", methods=["GET"])
def api_check_update():
    """Check whether a newer release is published on GitHub.

    Returns:
        {
            "success": True,
            "enabled": True/False,
            "current": "4.0.0",
            "latest": "4.1.0" | None,
            "update_available": True/False,
            "url": "https://github.com/.../releases/tag/v4.1.0" | None,
            "checked_at": <unix ts>,
        }

    Loopback only. Silently caches for 6 hours. Never blocks the GUI —
    network failures return `update_available: False` so the banner
    stays hidden.
    """
    if not _is_loopback_addr(request.remote_addr):
        return jsonify({"success": False, "error": "loopback_only"}), 403

    current = get_app_version()
    # Read the releases URL from the environment. It's loaded from .env
    # via load_dotenv() in config.py, so any change + save is picked up
    # on the next process start. Leaving this unset disables the check.
    releases_url = str(os.environ.get("RELEASES_API_URL", "") or "").strip()

    if not releases_url:
        # Update checking is disabled entirely.
        return jsonify({
            "success": True,
            "enabled": False,
            "current": current,
            "latest": None,
            "update_available": False,
            "url": None,
            "checked_at": time.time(),
        })

    now = time.time()
    cached = _UPDATE_CHECK_CACHE.get("data")
    cached_at = float(_UPDATE_CHECK_CACHE.get("at") or 0)
    if cached and (now - cached_at) < _UPDATE_CHECK_TTL:
        return jsonify(cached)

    latest_tag = None
    release_url = None
    try:
        import requests as _req
        r = _req.get(
            releases_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"ChiaMarketMaker/{current}",
            },
            timeout=6,
        )
        if r.status_code == 200:
            j = r.json()
            latest_tag = str(j.get("tag_name") or "").strip() or None
            release_url = str(j.get("html_url") or "").strip() or None
    except Exception as e:
        log_event("info", "update_check_failed", f"{e}")

    cur_sv = _parse_semver(current)
    lat_sv = _parse_semver(latest_tag) if latest_tag else None
    update_available = bool(cur_sv and lat_sv and lat_sv > cur_sv)

    result = {
        "success": True,
        "enabled": True,
        "current": current,
        "latest": latest_tag,
        "update_available": update_available,
        "url": release_url,
        "checked_at": now,
    }
    _UPDATE_CHECK_CACHE["at"] = now
    _UPDATE_CHECK_CACHE["data"] = result
    return jsonify(result)


# ---------------------------------------------------------------------------
# Sage wallet release check (backend proxy for the startup version card)
# ---------------------------------------------------------------------------
#
# The startup flow used to hit api.github.com directly from JavaScript to
# see if the installed Sage is out of date. That works inside WebView2 on
# Windows but fails with "Failed to fetch" in a plain browser because of
# CORS on local-origin requests. Proxying through Flask removes the CORS
# surface entirely, lets the same HTML run in dev mode without errors,
# and gives us one place to cache + rate-limit the call.

_SAGE_RELEASE_CACHE: dict = {"at": 0.0, "data": None}
_SAGE_RELEASE_TTL = 6 * 3600  # 6 hours — GitHub's unauth rate limit is 60/hr


@app.route("/api/sage/latest-release", methods=["GET"])
def api_sage_latest_release():
    """Return the latest Sage release tag from GitHub, cached 6 hours.

    Response:
        {"success": True, "tag": "0.12.10", "url": "https://..."}
        {"success": False, "error": "..."} on failure (non-fatal for GUI)

    Loopback only. Never raises to the caller — network failures return
    success=False so the startup flow can skip the update card quietly.
    """
    if not _is_loopback_addr(request.remote_addr):
        return jsonify({"success": False, "error": "loopback_only"}), 403

    now = time.time()
    cached = _SAGE_RELEASE_CACHE.get("data")
    cached_at = float(_SAGE_RELEASE_CACHE.get("at") or 0)
    if cached and (now - cached_at) < _SAGE_RELEASE_TTL:
        return jsonify(cached)

    try:
        import requests as _req
        r = _req.get(
            "https://api.github.com/repos/xch-dev/sage/releases/latest",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ChiaMarketMaker/sage-version-check",
            },
            timeout=6,
        )
        if r.status_code != 200:
            result = {"success": False, "error": f"github_http_{r.status_code}"}
        else:
            j = r.json()
            tag = str(j.get("tag_name") or "").lstrip("vV").strip()
            url = str(j.get("html_url") or "").strip() or None
            if not tag:
                result = {"success": False, "error": "no_tag_in_response"}
            else:
                result = {"success": True, "tag": tag, "url": url}
    except Exception as e:
        # Network error, timeout, DNS, etc. — non-fatal
        result = {"success": False, "error": f"fetch_failed: {e}"}

    _SAGE_RELEASE_CACHE["at"] = now
    _SAGE_RELEASE_CACHE["data"] = result
    return jsonify(result)


# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) — Real-time push to GUI
# ---------------------------------------------------------------------------

@app.route("/api/events")
def api_events():
    """SSE endpoint — GUI connects here for real-time updates.

    Events are pushed as:
        data: {"type": "price_update", "data": {...}, "ts": 1234567890}

    The GUI listens with EventSource('/api/events') in JavaScript.
    """
    def stream():
        q = events.subscribe()
        try:
            # Send initial state immediately
            if bot:
                initial = _serialize_dict(bot.get_state())
                yield f"data: {json.dumps({'type': 'state', 'data': initial})}\n\n"

            while True:
                try:
                    msg = q.get(timeout=30)
                    # Serialize Decimals
                    serialized = _serialize_dict(msg) if isinstance(msg, dict) else msg
                    yield f"data: {json.dumps(serialized, default=str)}\n\n"
                except queue.Empty:
                    # Send keepalive every 30 seconds
                    yield f": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            # Always unsubscribe — handles both clean disconnect (GeneratorExit)
            # and abrupt disconnect (WSGI server closes the generator).
            events.unsubscribe(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Bot Control Routes
# ---------------------------------------------------------------------------

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    """Start the bot loop with pre-start validation (V1 parity).

    Checks wallet sync status, CAT config, and basic sanity
    before allowing the bot to start. V1 had validate_start().
    """
    slog("GUI_ACTION", ">>> BUTTON: Start Bot")
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    if bot.is_running():
        return jsonify({"success": True, "status": "already_running"})

    # ---- Pre-start validation (V1 parity) ----
    warnings = []
    errors = []

    # Check CAT_ASSET_ID is configured
    if not cfg.CAT_ASSET_ID or cfg.CAT_ASSET_ID == "":
        errors.append("CAT_ASSET_ID is not set in .env — bot cannot trade")

    # Check wallet connectivity (non-blocking, best effort)
    try:
        from wallet import get_wallet_sync_status
        sync = get_wallet_sync_status()
        if sync:
            sync_state = str(sync.get("sync_state") or "").strip().lower()
            if not sync.get("reachable", False):
                warnings.append("Could not reach wallet RPC — check if Sage/Chia is running")
            elif sync_state == "not_synced":
                warnings.append("Wallet is not fully synced — offers may fail")
        else:
            warnings.append("Could not reach wallet RPC — check if Sage/Chia is running")
    except Exception as e:
        warnings.append(f"Wallet check failed: {str(e)[:100]}")

    signing_block_reason = _get_sage_signing_block_reason()
    if signing_block_reason:
        errors.append(signing_block_reason)

    # Check spread is sensible
    if cfg.SPREAD_BPS <= 0:
        errors.append("SPREAD_BPS is 0 or negative — bot would create bad offers")

    # Check hard price limits are set
    hard_min = getattr(cfg, "HARD_MIN_PRICE_XCH", Decimal("0"))
    hard_max = getattr(cfg, "HARD_MAX_PRICE_XCH", Decimal("0"))
    if hard_min <= 0 or hard_max <= 0:
        warnings.append("HARD_MIN_PRICE_XCH or HARD_MAX_PRICE_XCH not set — circuit breakers disabled")

    # Block start on critical errors
    if errors:
        return jsonify({"status": "error", "errors": errors, "warnings": warnings}), 400

    _reset_runtime_session_stats()

    # Start with warnings
    started = bot.start()
    if not started:
        state = {}
        try:
            state = bot.get_state() or {}
        except Exception:
            state = {}
        message = "Bot start was blocked before trading could begin"
        if str(state.get("status") or "").strip().lower() == "blocked":
            message = "Bot start blocked - active wallet cannot sign or preflight did not pass"
        return jsonify({
            "status": "error",
            "errors": [message],
            "warnings": warnings,
            "bot_status": state.get("status") or "blocked",
        }), 400
    # Clear the fresh-start flag now that a real run has begun.
    # This ensures the resume modal shows correctly on the NEXT restart —
    # the flag was only meant to suppress the modal within a single session
    # (so a hot-reload after choosing "Start Fresh" doesn't re-show it).
    _fresh_start_clear()
    events.emit("bot_control", {"action": "started"})
    result = {"success": True, "status": "started"}
    if warnings:
        result["warnings"] = warnings
    return jsonify(result)


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    """Stop the bot loop."""
    slog("GUI_ACTION", ">>> BUTTON: Stop Bot")
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    bot.stop()
    events.emit("bot_control", {"action": "stopped"})
    return jsonify({"status": "stopped"})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Full shutdown — stop bot, cancel offers, kill server.

    Called by the GUI 'Shutdown' button or when the user wants
    to cleanly exit everything.
    """
    try:
        cancel_first = bool((request.get_json(silent=True) or {}).get("cancel_offers", False))
    except Exception:
        cancel_first = False

    def _do_shutdown():
        """Run shutdown sequence in background thread so the HTTP response returns first."""
        time.sleep(0.5)  # Let the response reach the browser

        print("\n🛑 SHUTDOWN sequence starting...", flush=True)

        # 0. Kill coin prep subprocess if it's still running
        global _coin_prep_proc
        try:
            if _coin_prep_proc is not None and _coin_prep_proc.poll() is None:
                prep_pid = _coin_prep_proc.pid
                print(f"   Stopping coin prep worker (PID: {prep_pid})...", flush=True)
                _coin_prep_proc.terminate()
                try:
                    _coin_prep_proc.wait(timeout=5)
                except Exception:
                    _coin_prep_proc.kill()
                    _coin_prep_proc.wait(timeout=3)
                print(f"   ✅ Coin prep worker stopped", flush=True)
                _coin_prep_proc = None
                _coin_prep_state["running"] = False
                _coin_prep_state["error"] = "Stopped by shutdown"
                # Ungate bot loop in case it was gated by coin prep
                if bot and hasattr(bot, 'coin_manager'):
                    bot.coin_manager._prep_running = False
        except Exception as e:
            print(f"   ⚠️ Coin prep cleanup: {e}", flush=True)

        # 1. Stop the bot loop
        if bot and bot.is_running():
            print("   Stopping bot loop...", flush=True)
            bot.stop()
            print("   ✅ Bot loop stopped", flush=True)

        # 2. Cancel all offers if requested
        if cancel_first and bot and bot.offer_manager:
            print("   Cancelling all offers...", flush=True)
            try:
                result = bot.offer_manager.cancel_all()
                cancelled = sum(1 for r in result.values() if r and r.get("success"))
                print(f"   ✅ Cancelled {cancelled} offers", flush=True)
            except Exception as e:
                print(f"   ⚠️ Cancel failed: {e}", flush=True)

        # 3. Stop Splash node (in case bot.stop() didn't cover it)
        try:
            if bot and hasattr(bot, 'splash_node') and bot.splash_node.is_running():
                bot.splash_node.stop()
                print("   ✅ Splash node stopped", flush=True)
        except Exception:
            pass

        try:
            if bot and hasattr(bot, "runtime_monitor"):
                bot.runtime_monitor.stop()
        except Exception:
            pass

        # 4. Database backup
        try:
            backup_database()
            print("   ✅ Database backed up", flush=True)
        except Exception:
            pass

        # 4b. Checkpoint the SQLite WAL before calling os._exit(). Without
        # this, recent writes sit in the -wal file; a hard exit can lose them.
        try:
            from database import get_connection as _get_conn
            _conn = _get_conn()
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            try:
                _conn.commit()
            except Exception:
                pass
            print("   ✅ WAL checkpointed", flush=True)
        except Exception as _wal_err:
            print(f"   ⚠️ WAL checkpoint failed: {_wal_err}", flush=True)

        print("   Shutting down server...", flush=True)
        log_event("info", "server_shutdown", "Server shutting down via GUI")

        # 5. Kill the process
        os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify({"success": True, "message": "Shutting down..."})


@app.route("/api/bot/state")
def api_bot_state():
    """Get full bot state (for GUI polling fallback)."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    state = bot.get_state()

    # When the bot object exists but trading is stopped, coin_manager/risk state can
    # still reflect a cold in-memory snapshot from startup. Backfill from the same
    # safe RPC health/count helpers used by other read-only endpoints so the GUI
    # does not show a misleading all-zero stopped state.
    if not state.get("running", False):
        try:
            state["chia_health"] = _get_health_snapshot()
        except Exception:
            pass

        try:
            coins = dict(state.get("coins") or {})
            if int(coins.get("xch_coins", 0) or 0) == 0 and int(coins.get("xch_total_coins", 0) or 0) == 0:
                from database import get_coin_summary

                db_coin_summary = get_coin_summary() or {}
                if db_coin_summary:
                    inventory = dict(coins.get("inventory") or {})
                    coins["xch_coins"] = int(db_coin_summary.get("xch_free_count", 0) or 0)
                    coins["cat_coins"] = int(db_coin_summary.get("cat_free_count", 0) or 0)
                    coins["xch_locked_coins"] = int(db_coin_summary.get("xch_locked_count", 0) or 0)
                    coins["cat_locked_coins"] = int(db_coin_summary.get("cat_locked_count", 0) or 0)
                    coins["xch_total_coins"] = int(db_coin_summary.get("xch_total", 0) or 0)
                    coins["cat_total_coins"] = int(db_coin_summary.get("cat_total", 0) or 0)
                    inventory["xch_locked_amount"] = f"{int(db_coin_summary.get('xch_locked_mojos', 0) or 0) / 1e12:.4f}"
                    cat_decimals = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
                    inventory["cat_locked_amount"] = (
                        f"{int(db_coin_summary.get('cat_locked_mojos', 0) or 0) / (10 ** cat_decimals):.2f}"
                    )
                    inventory["xch_locked_coins"] = coins["xch_locked_coins"]
                    inventory["cat_locked_coins"] = coins["cat_locked_coins"]
                    inventory["xch_total_coins"] = coins["xch_total_coins"]
                    inventory["cat_total_coins"] = coins["cat_total_coins"]
                    coins["inventory"] = inventory

                if int(coins.get("xch_coins", 0) or 0) == 0 and int(coins.get("xch_total_coins", 0) or 0) == 0:
                    from wallet import get_spendable_coin_count, WALLET_ID_XCH

                    xch_free = int(get_spendable_coin_count(WALLET_ID_XCH) or 0)
                    cat_wallet_id = _active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", 2)
                    cat_free = int(get_spendable_coin_count(cat_wallet_id) or 0)

                    coins["xch_coins"] = xch_free
                    coins["cat_coins"] = cat_free
                    coins["xch_total_coins"] = xch_free + int(coins.get("xch_locked_coins", 0) or 0)
                    coins["cat_total_coins"] = cat_free + int(coins.get("cat_locked_coins", 0) or 0)

                state["coins"] = coins
        except Exception:
            pass

    return jsonify(_serialize_dict(state))


def _get_health_snapshot() -> dict:
    """Quick health check for /api/status when bot hasn't started yet."""
    import chia_node
    if not chia_node.is_startup_authorised():
        return {"status": "not_started", "consecutive_failures": 0}
    try:
        from wallet import get_chia_health
        h = get_chia_health()
        wallet = h.get("wallet", {}) or {}
        node = h.get("node", {}) or {}
        return {
            "status": h.get("status", "unknown"),
            "wallet_reachable": wallet.get("reachable", False),
            "wallet_synced": wallet.get("synced", False),
            "wallet_syncing": wallet.get("syncing", False),
            "wallet_sync_state": wallet.get("sync_state", "unknown"),
            "node_reachable": node.get("reachable", False),
            "node_synced": node.get("synced", False),
            "consecutive_failures": 0,
            "last_check": time.time(),
        }
    except Exception:
        return {"status": "unknown", "consecutive_failures": 0}


@app.route("/api/status")
def api_status():
    """Main GUI polling endpoint — assembles full state in the format the GUI expects.

    Returns a nested dict with: running, stats, balances, pricing, offers, logs,
    chia_health, wallet_type, current_cat. This is polled every 5 seconds.
    """
    try:
        from database import get_recent_events, get_open_offers

        # If bot hasn't been created yet, return minimal static state.
        # DO NOT make live network calls during polling — /api/status is called
        # every 5 seconds and side effects here cause wallet RPC contention.
        # The /api/dashboard endpoint provides fresh data on page load.
        if not bot:
            xch_bal = {"spendable": 0, "total": 0}
            cat_bal = {"spendable": 0, "total": 0}

            # Note: pricing/offer fetches below run pre-bot for GUI display.
            # TODO: Move to /api/dashboard and cache; /api/status should be read-only.
            pricing = {"bid": 0, "mid": 0, "ask": 0}
            asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
            cat_dec = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
            print(f"[STATUS] Pricing lookup: asset_id={asset_id!r}, decimals={cat_dec}", flush=True)
            log_event("info", "price_lookup", f"Looking up price for {_active_cat.get('name', 'unknown')}")
            if asset_id:
                import requests as _req
                mid = 0

                # --- Try TibetSwap ---
                try:
                    resp = _req.get("https://api.v2.tibetswap.io/pairs",
                                    params={"skip": 0, "limit": 200}, timeout=8)
                    if resp.status_code == 200:
                        norm_id = asset_id.lower().strip().replace("0x", "")
                        for p in resp.json():
                            p_id = str(p.get("asset_id", "")).lower().strip().replace("0x", "")
                            if p_id == norm_id:
                                xr = Decimal(str(p.get("xch_reserve", 0))) / Decimal("1000000000000")
                                tr = Decimal(str(p.get("token_reserve", 0))) / (Decimal(10) ** int(cat_dec))
                                if tr > 0:
                                    mid = xr / tr
                                    pricing = {"bid": mid, "mid": mid, "ask": mid,
                                               "tibet_price": mid, "tibet_enabled": True,
                                               "source": "tibetswap",
                                               "liquidity": {"xch_reserve": str(xr), "token_reserve": str(tr)}}
                                    print(f"[STATUS] TibetSwap price: {mid}", flush=True)
                                    log_event("success", "price_found", f"TibetSwap price: {mid:.8f} XCH")
                                break
                except Exception as e:
                    print(f"[STATUS] TibetSwap failed: {e}")
                    log_event("warning", "price_lookup", f"TibetSwap failed: {e}")

                # --- Fallback to Dexie if TibetSwap had no match ---
                if mid == 0:
                    print("[STATUS] No TibetSwap price, trying Dexie...", flush=True)
                    log_event("info", "price_lookup", "No TibetSwap price, trying Dexie fallback")
                    try:
                        ticker_id = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "") or ""
                        # Dexie ticker format is "{CAT}_XCH" e.g. "SBX_XCH" (V1 confirmed)
                        if ticker_id and "_" not in ticker_id:
                            ticker_id = f"{ticker_id}_XCH"
                        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
                        if ticker_id:
                            resp = _req.get(f"{dexie_base}/v2/prices/tickers",
                                            params={"ticker_id": ticker_id}, timeout=8)
                            if resp.status_code == 200:
                                tickers = resp.json().get("tickers", [])
                                if tickers:
                                    for field in ["current_avg_price", "last_price", "price"]:
                                        val = tickers[0].get(field)
                                        if val and str(val) != "0":
                                            mid = Decimal(str(val))
                                            pricing = {"bid": mid, "mid": mid, "ask": mid,
                                                       "dexie_price": mid, "tibet_enabled": False,
                                                       "source": "dexie"}
                                            print(f"[STATUS] Dexie ticker price: {mid}")
                                            log_event("success", "price_found", f"Dexie ticker price: {mid:.8f} XCH")
                                            break
                        # If no ticker_id or no result, try orderbook
                        if mid == 0:
                            resp = _req.get(f"{dexie_base}/v1/offers",
                                            params={"offered": asset_id, "requested": "xch",
                                                     "status": 0, "page_size": 1, "sort": "price_asc"},
                                            timeout=8)
                            if resp.status_code == 200:
                                offers = resp.json().get("offers", [])
                                if offers:
                                    best_ask = Decimal(str(offers[0].get("price", 0)))
                                    if best_ask > 0:
                                        mid = best_ask
                                        pricing = {"bid": mid, "mid": mid, "ask": mid,
                                                   "dexie_price": mid, "tibet_enabled": False,
                                                   "source": "dexie_orderbook"}
                                        print(f"[STATUS] Dexie orderbook price: {mid}")
                                        log_event("success", "price_found", f"Dexie orderbook price: {mid:.8f} XCH")
                    except Exception as e:
                        print(f"[STATUS] Dexie fallback failed: {e}")
                        log_event("warning", "price_lookup", f"Dexie fallback failed: {e}")

                if mid == 0:
                    print("[STATUS] No price from any source")
                    log_event("error", "price_lookup", "No price available from any source")
            else:
                print("[STATUS] No asset_id available for pricing", flush=True)
                log_event("warning", "price_lookup", "No asset_id configured — cannot fetch price")

            # Compute actual bid/ask from mid using configured spread
            if pricing.get("mid", 0) > 0 and pricing.get("bid") == pricing.get("mid"):
                _spread_bps = Decimal(str(getattr(cfg, "BASE_SPREAD_BPS", 0) or getattr(cfg, "SPREAD_BPS", 200) or 200))
                _spread_frac = _spread_bps / Decimal("10000")
                pricing["bid"] = pricing["mid"] * (1 - _spread_frac / 2)
                pricing["ask"] = pricing["mid"] * (1 + _spread_frac / 2)

            # Fetch open offers from wallet RPC — uses the same normalize path
            # as the bot (get_all_offers → classify_offers_from_list) so prices
            # and amounts are properly extracted before Start Bot.
            offers_buy_pre = []
            offers_sell_pre = []
            try:
                from wallet import get_all_offers, classify_offers_from_list
                asset_id_for_offers = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
                pre_offers = get_all_offers(include_completed=False, start=0, end=500)
                if pre_offers and isinstance(pre_offers, list) and asset_id_for_offers:
                    open_buys, open_sells, _ = classify_offers_from_list(
                        pre_offers, asset_id_for_offers)

                    # Load DB offers once for Dexie link / tier / coin_id lookup
                    db_map = {}
                    try:
                        for dbo in get_open_offers():
                            db_map[dbo.get("trade_id", "")] = dbo
                    except Exception:
                        pass

                    # Extract price/size from normalized summary for each offer
                    for o in open_buys:
                        summary = o.get("summary") or {}
                        offered = summary.get("offered", {})
                        requested = summary.get("requested", {})
                        xch_mojos = Decimal(str(offered.get("xch", 0)))
                        cat_mojos = Decimal(str(requested.get(asset_id_for_offers, 0)))
                        xch_amount = xch_mojos / Decimal("1000000000000")
                        cat_amount = cat_mojos / (Decimal(10) ** cat_dec) if cat_mojos else Decimal(0)
                        price = xch_amount / cat_amount if cat_amount > 0 else Decimal(0)
                        tid = o.get("trade_id", "")
                        db_offer = db_map.get(tid, {})
                        offers_buy_pre.append({
                            "trade_id": tid,
                            "side": "buy",
                            "price_xch": f"{price:.10f}",
                            "size_xch": f"{xch_amount:.4f}",
                            "size_cat": f"{cat_amount:.3f}",
                            "status": "open",
                            "tier": db_offer.get("tier", ""),
                            "dexie_id": db_offer.get("dexie_id", ""),
                            "coin_id": db_offer.get("coin_id", ""),
                            "created_at": o.get("creation_timestamp", ""),
                        })

                    for o in open_sells:
                        summary = o.get("summary") or {}
                        offered = summary.get("offered", {})
                        requested = summary.get("requested", {})
                        cat_mojos = Decimal(str(offered.get(asset_id_for_offers, 0)))
                        xch_mojos = Decimal(str(requested.get("xch", 0)))
                        xch_amount = xch_mojos / Decimal("1000000000000")
                        cat_amount = cat_mojos / (Decimal(10) ** cat_dec) if cat_mojos else Decimal(0)
                        price = xch_amount / cat_amount if cat_amount > 0 else Decimal(0)
                        tid = o.get("trade_id", "")
                        db_offer = db_map.get(tid, {})
                        offers_sell_pre.append({
                            "trade_id": tid,
                            "side": "sell",
                            "price_xch": f"{price:.10f}",
                            "size_xch": f"{xch_amount:.4f}",
                            "size_cat": f"{cat_amount:.3f}",
                            "status": "open",
                            "tier": db_offer.get("tier", ""),
                            "dexie_id": db_offer.get("dexie_id", ""),
                            "coin_id": db_offer.get("coin_id", ""),
                            "created_at": o.get("creation_timestamp", ""),
                        })

                    print(f"[STATUS] Pre-bot offers: {len(offers_buy_pre)} buys, "
                          f"{len(offers_sell_pre)} sells", flush=True)
            except Exception as e:
                import traceback
                print(f"[STATUS] Pre-bot offer fetch error: {e}", flush=True)
                traceback.print_exc()

            # Build coin tracking for pre-bot display (matches running format)
            xch_free = 0
            cat_free = 0
            try:
                from wallet import get_spendable_coin_count, WALLET_ID_XCH
                xch_free = int(get_spendable_coin_count(WALLET_ID_XCH) or 0)
                cat_wid_coins = _active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
                cat_free = int(get_spendable_coin_count(cat_wid_coins) or 0)
            except Exception:
                pass
            xch_locked = len(offers_buy_pre)
            cat_locked = len(offers_sell_pre)
            # Calculate locked amounts from offer sizes
            xch_locked_amt = sum(float(o.get("size_xch", 0)) for o in offers_buy_pre)
            cat_locked_amt = sum(float(o.get("size_cat", 0)) for o in offers_sell_pre)
            coin_tracking_pre = {
                "xch_free": xch_free,
                "xch_locked": xch_locked,
                "xch_total": xch_free + xch_locked,
                "cat_free": cat_free,
                "cat_locked": cat_locked,
                "cat_total": cat_free + cat_locked,
                "xch_locked_amount": f"{xch_locked_amt:.4f}",
                "cat_locked_amount": f"{cat_locked_amt:.0f}",
            }

            cat_name = _active_cat.get("name") or (cfg.CAT_NAME if hasattr(cfg, "CAT_NAME") else "")
            return jsonify({
                "running": False,
                "stats": {"loop_count": 0, "uptime_seconds": 0, "last_loop_time": 0,
                           "total_fills": 0, "errors": 0},
                "balances": {"xch": xch_bal, "cat": cat_bal},
                "pricing": _decimal_safe(pricing),
                "offers": {
                    "buy": offers_buy_pre,
                    "sell": offers_sell_pre,
                    "history": _build_fill_history_for_gui(asset_id, limit=20),
                },
                "coin_tracking": coin_tracking_pre,
                "logs": [],
                "chia_health": _get_health_snapshot(),
                "wallet_type": get_wallet_type(),
                "current_cat": {
                    "name": cat_name,
                    "asset_id": asset_id,
                    "wallet_id": _active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", None),
                    "decimals": _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3),
                    "ticker_id": _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", None),
                },
            })

        # Get raw state from bot
        raw = bot.get_state()

        # --- Stats ---
        db_stats = raw.get("stats") or {}
        # Compute uptime from bot's start_time (db_stats doesn't track this)
        import time as _time
        _uptime = int(_time.time() - bot._start_time) if bot._start_time else 0
        stats_out = {
            "loop_count": raw.get("loop_count", 0),
            "uptime_seconds": _uptime,
            "last_loop_time": raw.get("loop_duration") or raw.get("last_loop_time", 0),
            "total_fills": db_stats.get("total_fills", 0),
            "errors": db_stats.get("errors", 0),
        }

        # --- Balances ---
        coins_data = raw.get("coins") or {}
        risk_data = raw.get("risk") or {}
        xch_bal = coins_data.get("xch_balance") or {}
        cat_bal = coins_data.get("cat_balance") or {}
        balances_out = {
            "xch": {
                "spendable": _safe_float(xch_bal.get("spendable") or xch_bal.get("free", 0)),
                "total": _safe_float(xch_bal.get("total", 0)),
            },
            "cat": {
                "spendable": _safe_float(cat_bal.get("spendable") or cat_bal.get("free", 0)),
                "total": _safe_float(cat_bal.get("total", 0)),
            },
        }

        # If balances are all zero (bot hasn't run yet), try direct wallet RPC
        if balances_out["xch"]["total"] == 0:
            try:
                from wallet import get_wallet_balance, WALLET_ID_XCH
                xch_result = get_wallet_balance(WALLET_ID_XCH)
                if xch_result and xch_result.get("success"):
                    wb = xch_result.get("wallet_balance") or {}
                    # Chia returns mojos — convert to XCH (1 XCH = 1e12 mojos)
                    confirmed = _safe_float(wb.get("confirmed_wallet_balance", 0))
                    spendable = _safe_float(wb.get("spendable_balance", 0))
                    balances_out["xch"]["total"] = confirmed / 1e12
                    balances_out["xch"]["spendable"] = spendable / 1e12
            except Exception:
                pass

        if balances_out["cat"]["total"] == 0:
            try:
                from wallet import get_wallet_balance
                # Use actively selected CAT wallet_id, fall back to config
                cat_wallet_id = _active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
                cat_result = get_wallet_balance(cat_wallet_id)
                if cat_result and cat_result.get("success"):
                    wb = cat_result.get("wallet_balance") or {}
                    cat_decimals = _active_cat.get("decimals") or getattr(cfg, 'CAT_DECIMALS', 3)
                    confirmed = _safe_float(wb.get("confirmed_wallet_balance", 0))
                    spendable = _safe_float(wb.get("spendable_balance", 0))
                    balances_out["cat"]["total"] = confirmed / (10 ** cat_decimals)
                    balances_out["cat"]["spendable"] = spendable / (10 ** cat_decimals)
            except Exception:
                pass

        # --- Pricing ---
        price_info = bot.get_price_info() if hasattr(bot, "get_price_info") else {}
        mid = _safe_float(raw.get("mid_price", 0))
        bid = _safe_float(price_info.get("last_quoted_buy", 0))
        ask = _safe_float(price_info.get("last_quoted_sell", 0))

        # If bot exists but hasn't run a loop yet, mid_price will be 0.
        # NOTE: We intentionally do NOT call price_engine.get_price() here.
        # get_price() writes to price_history (DB write), and GUI polls every
        # few seconds from Flask threads. Those writes cause cascading DB lock
        # contention with the bot loop's startup batch cancel.
        # Instead, use cached price from last bot loop, or show 0 until first loop.
        if mid == 0 and hasattr(bot, "price_engine") and bot.price_engine:
            try:
                # Use cached price if available (read-only, no DB write)
                cached = getattr(bot.price_engine, "_last_price_result", None)
                if cached and cached.get("mid_price"):
                    mid = float(cached["mid_price"])
            except Exception:
                pass

        # Last resort: if still no price (bot created but loop hasn't run yet),
        # do a lightweight TibetSwap fetch. This is read-only — no DB writes.
        # Without this, the settings/coin-prep page can't calculate sell amounts.
        if mid == 0:
            try:
                import requests as _req
                asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
                cat_dec = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
                if asset_id:
                    resp = _req.get("https://api.v2.tibetswap.io/pairs",
                                    params={"skip": 0, "limit": 200}, timeout=8)
                    if resp.status_code == 200:
                        norm_id = asset_id.lower().strip().replace("0x", "")
                        for p in resp.json():
                            p_id = str(p.get("asset_id", "")).lower().strip().replace("0x", "")
                            if p_id == norm_id:
                                xr = float(p.get("xch_reserve", 0)) / 1e12
                                tr = float(p.get("token_reserve", 0)) / (10 ** int(cat_dec))
                                if tr > 0:
                                    mid = xr / tr
                                    print(f"[STATUS] TibetSwap fallback price: {mid:.8f}", flush=True)
                                break
            except Exception as e:
                print(f"[STATUS] TibetSwap fallback failed: {e}", flush=True)

        # Compute bid/ask from mid using the EFFECTIVE spread.
        # last_quoted_buy/sell both store mid_price (not actual bid/ask),
        # so we always need to derive bid/ask from the spread.
        if mid > 0:
            _got_spread = False
            # Try to get the effective spread from the risk manager (dynamic spread)
            try:
                if hasattr(bot, "risk_manager") and bot.risk_manager:
                    health = bot.risk_manager.get_market_health()
                    if health:
                        _buy_bps = _safe_float(health.get("buy_spread_bps", 0))
                        _sell_bps = _safe_float(health.get("sell_spread_bps", 0))
                        if _buy_bps > 0 and _sell_bps > 0:
                            bid = mid * (1 - _buy_bps / 10000)
                            ask = mid * (1 + _sell_bps / 10000)
                            _got_spread = True
            except Exception:
                pass

            # Fallback: if risk manager didn't provide spread, use config
            if not _got_spread:
                _base_bps = _safe_float(
                    getattr(cfg, "BASE_SPREAD_BPS", 0)
                    or getattr(cfg, "SPREAD_BPS", 200)
                    or 200
                )
                spread_frac = _base_bps / 10000
                bid = mid * (1 - spread_frac / 2)
                ask = mid * (1 + spread_frac / 2)

        pricing_out = {"bid": bid, "mid": mid, "ask": ask}

        # --- Offers ---
        is_running = raw.get("running", False)
        if is_running:
            # Bot running — use database records (kept in sync by bot loop)
            try:
                cat_id = cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""
                offers_buy = get_open_offers(side="buy", cat_asset_id=cat_id)
                offers_sell = get_open_offers(side="sell", cat_asset_id=cat_id)
            except Exception:
                offers_buy = []
                offers_sell = []
        else:
            # Bot stopped — fetch from wallet RPC and classify properly
            # to get real prices, sizes, and side detection
            offers_buy = []
            offers_sell = []
            try:
                from wallet import get_all_offers, classify_offers_from_list
                asset_id_for_classify = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
                cat_decimals = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
                all_offers = get_all_offers(end=200)
                if all_offers and isinstance(all_offers, list) and asset_id_for_classify:
                    buys_raw, sells_raw, _ = classify_offers_from_list(all_offers, asset_id_for_classify)

                    def _extract_offer_data(tr, side):
                        """Extract price/size from a classified offer's normalized summary."""
                        summary = tr.get("summary") or {}
                        offered = summary.get("offered") or {}
                        requested = summary.get("requested") or {}
                        tid = tr.get("trade_id", "")

                        xch_mojos = 0
                        cat_mojos = 0
                        if side == "buy":
                            # Buying CAT: offering XCH, requesting CAT
                            xch_mojos = offered.get("xch", 0)
                            cat_mojos = requested.get(asset_id_for_classify, 0)
                        else:
                            # Selling CAT: offering CAT, requesting XCH
                            cat_mojos = offered.get(asset_id_for_classify, 0)
                            xch_mojos = requested.get("xch", 0)

                        # Convert mojos to display units
                        xch_val = abs(float(xch_mojos)) / 1e12
                        cat_val = abs(float(cat_mojos)) / (10 ** cat_decimals)

                        # Calculate price (XCH per CAT)
                        price = xch_val / cat_val if cat_val > 0 else 0

                        return {
                            "trade_id": tid,
                            "side": side,
                            "price_xch": str(price),
                            "size_xch": str(xch_val),
                            "size_cat": str(cat_val),
                            "status": "open",
                            "created_at": tr.get("created_at_time") or _sage_ts_to_iso(tr.get("creation_timestamp")),
                        }

                    for tr in buys_raw:
                        offers_buy.append(_extract_offer_data(tr, "buy"))
                    for tr in sells_raw:
                        offers_sell.append(_extract_offer_data(tr, "sell"))

            except Exception as e:
                import traceback
                print(f"[STATUS] Wallet offer fetch (bot stopped): {e}", flush=True)
                traceback.print_exc()

        # Enrich wallet-sourced offers with Dexie links from bot's dexie_manager
        # and/or database records (prices, sizes, tier, expiry)
        if not is_running and (offers_buy or offers_sell):
            # Source 1: Bot's in-memory dexie_manager (survives within same process)
            dexie_mgr = getattr(bot, 'dexie_manager', None) if bot else None
            if dexie_mgr:
                for o in offers_buy + offers_sell:
                    tid = o.get("trade_id", "")
                    if tid and not o.get("dexie_id"):
                        dexie_id = dexie_mgr.get_dexie_id(tid)
                        if dexie_id:
                            o["dexie_id"] = dexie_id
                            o["dexie_posted"] = True

            # Source 2: Database offers table (has dexie_id, tier, expiry)
            try:
                from database import get_open_offers as db_get_open_offers
                db_offers = db_get_open_offers()
                db_map = {o["trade_id"]: o for o in db_offers if o.get("trade_id")}
                # One-shot diagnostic — check how many DB offers have dexie_id
                if not hasattr(api_status, '_dexie_diag_done'):
                    api_status._dexie_diag_done = True
                    has_dexie = sum(1 for o in db_offers if o.get("dexie_id"))
                    print(f"  [DEXIE] DB has {len(db_offers)} open offers, "
                          f"{has_dexie} have dexie_id", flush=True)
                    if db_offers and not has_dexie:
                        print(f"  [DEXIE] ⚠️ NO offers have dexie_id in DB — "
                              f"Dexie posting may have failed in previous sessions", flush=True)
                for o in offers_buy + offers_sell:
                    tid = o.get("trade_id", "")
                    if tid and tid in db_map:
                        db_o = db_map[tid]
                        # Copy Dexie info if not already set
                        if not o.get("dexie_id") and db_o.get("dexie_id"):
                            o["dexie_id"] = db_o["dexie_id"]
                        if db_o.get("dexie_posted"):
                            o["dexie_posted"] = True
                        # Copy price/size if wallet didn't provide them
                        if o.get("price_xch") in ("0", 0, None, ""):
                            o["price_xch"] = db_o.get("price_xch", o["price_xch"])
                        if o.get("size_xch") in ("0", 0, None, ""):
                            o["size_xch"] = db_o.get("size_xch", o["size_xch"])
                        if o.get("size_cat") in ("0", 0, None, ""):
                            o["size_cat"] = db_o.get("size_cat", o["size_cat"])
                        # Copy tier and expiry info if available
                        if db_o.get("tier"):
                            o["tier"] = db_o["tier"]
                        if db_o.get("expires_at"):
                            o["expires_at"] = db_o["expires_at"]
                        if db_o.get("created_at") and not o.get("created_at"):
                            o["created_at"] = db_o["created_at"]
            except Exception as e:
                print(f"[STATUS] DB offer enrichment failed: {e}", flush=True)

        # Enrich offers with Dexie links and GUI-friendly fields
        cat_name = _active_cat.get("name") or getattr(cfg, "CAT_NAME", "CAT")
        cat_dec = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
        mid = pricing_out.get("mid", 0)

        def _enrich_offer(offer_dict):
            """Add GUI-friendly fields to a database offer record."""
            o = dict(offer_dict)  # Don't mutate original

            # Dexie link (V1 parity)
            # Source 1: Database record (already in offer dict from get_open_offers)
            dexie_id = o.get("dexie_id")
            # Source 2: In-memory dexie_manager (catches freshly posted offers
            # before next DB read, and covers startup where DB might lag)
            if not dexie_id and is_running:
                dexie_mgr = getattr(bot, 'dexie_manager', None) if bot else None
                if dexie_mgr:
                    tid = o.get("trade_id", "")
                    if tid:
                        dexie_id = dexie_mgr.get_dexie_id(tid)
                        if dexie_id:
                            o["dexie_id"] = dexie_id
            if dexie_id:
                o["dexie_link"] = f"https://dexie.space/offers/{dexie_id}"
                o["dexie"] = "✅ Dexie"
            elif o.get("dexie_posted"):
                o["dexie"] = "✅ Dexie"
            elif not is_running:
                o["dexie"] = "⏳ Start bot to post"
            else:
                o["dexie"] = "📍 Local"

            # Short ID for display
            tid = o.get("trade_id", "")
            o["id"] = (tid[:16] + "...") if len(tid) > 16 else tid
            o["full_id"] = tid

            # Sizes for display
            try:
                size_xch = float(o.get("size_xch", 0))
                size_cat = float(o.get("size_cat", 0))
                price = float(o.get("price_xch", 0))
                o["size_xch"] = f"{size_xch:.4f}"
                o["size_cat"] = f"{size_cat:,.{cat_dec}f}"
                o["price"] = f"{price:.10f}" if price else "N/A"
            except (ValueError, TypeError):
                pass

            coin_id = str(o.get("coin_id") or "")
            o["coin_id_short"] = (
                (coin_id[:18] + "...")
                if coin_id and len(coin_id) > 18
                else (coin_id or "N/A")
            )

            # Age
            created = o.get("created_at", "")
            if created:
                try:
                    ct = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_secs = (datetime.now(timezone.utc) - ct).total_seconds()
                    if age_secs < 60:
                        o["age"] = f"{int(age_secs)}s"
                    elif age_secs < 3600:
                        o["age"] = f"{int(age_secs / 60)}m"
                    else:
                        o["age"] = f"{age_secs / 3600:.1f}h"
                    o["created_datetime"] = ct.strftime("%Y-%m-%d %H:%M:%S UTC")
                except Exception:
                    o["age"] = ""
                    o["created_datetime"] = created
            else:
                o["age"] = ""

            # Spread from mid
            try:
                price_f = float(o.get("price_xch", 0))
                mid_f = float(mid)
                if mid_f > 0 and price_f > 0:
                    spread_pct = ((price_f - mid_f) / mid_f) * 100
                    o["spread_pct"] = f"{spread_pct:+.2f}%"
                    o["mid_price"] = f"{mid_f:.10f}"
                else:
                    o["spread_pct"] = "N/A"
                    o["mid_price"] = "N/A"
            except (ValueError, TypeError):
                o["spread_pct"] = "N/A"
                o["mid_price"] = "N/A"

            # Status description
            status = o.get("status", "open")
            if status == "open":
                o["status"] = "PENDING_ACCEPT"
                o["status_description"] = "Offer is active and waiting for a taker"

            o["cat_name"] = cat_name

            return o

        enriched_buy = [_enrich_offer(o) for o in offers_buy]
        enriched_sell = [_enrich_offer(o) for o in offers_sell]

        fills_data = raw.get("fills") or {}
        history_out = _build_fill_history_for_gui(
            _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", ""),
            limit=50,
        )
        if not history_out:
            # F46 (2026-04-09): the fallback path returns raw fill rows
            # which lack the `status`/`price` keys the GUI expects.
            # Normalise the shape here so updateHistory() doesn't throw.
            raw_recent = _serialize_list(fills_data.get("recent") or [])
            history_out = []
            for it in raw_recent:
                if not isinstance(it, dict):
                    history_out.append(it)
                    continue
                norm = dict(it)
                if "status" not in norm or not norm.get("status"):
                    vs = str(norm.get("verification_status") or "").lower()
                    norm["status"] = "FILLED" if vs in ("verified", "confirmed") else (vs.upper() or "FILLED")
                if "price" not in norm and "price_xch" in norm:
                    norm["price"] = norm["price_xch"]
                if "cat_name" not in norm:
                    norm["cat_name"] = _active_cat.get("name") or getattr(cfg, "CAT_NAME", "") or "CAT"
                history_out.append(norm)
        offers_out = {
            "buy": _serialize_list(enriched_buy),
            "sell": _serialize_list(enriched_sell),
            "history": history_out,
        }

        # --- Logs (latest 100 events, filtered to current session) ---
        try:
            from database import get_events_since, get_recent_events
            cutoff = _session_start_time
            if _logs_cleared_at and (not cutoff or _logs_cleared_at > cutoff):
                cutoff = _logs_cleared_at
            if cutoff:
                events_list = get_events_since(cutoff, limit=100)
            else:
                events_list = get_recent_events(limit=100)
            # Map database field names to what GUI expects
            logs_out = []
            for ev in events_list:
                logs_out.append({
                    "timestamp": ev.get("timestamp", ""),
                    "full_ts": ev.get("timestamp", ""),
                    "level": ev.get("severity", "info"),
                    "source": ev.get("event_type", ""),
                    "message": ev.get("message", ""),
                })
            # One-shot diagnostic — log first time we return events
            if not hasattr(api_status, '_logs_diag_done'):
                api_status._logs_diag_done = True
                print(f"  [LOGS] Session cutoff: {cutoff}", flush=True)
                print(f"  [LOGS] Events returned: {len(logs_out)}", flush=True)
                if logs_out:
                    print(f"  [LOGS] First: {logs_out[0].get('message', '')[:80]}", flush=True)
        except Exception as e:
            logs_out = []
            print(f"  [LOGS] ⚠️ Log query failed: {e}", flush=True)

        # --- Coin tracking (free vs locked) ---
        coin_tracking = {}
        inv = coins_data.get("inventory") or {}
        try:
            from database import get_coin_summary
            db_coin_summary = get_coin_summary()
        except Exception:
            db_coin_summary = {}

        if db_coin_summary:
            _xch_free_db = db_coin_summary.get("xch_free_count", 0)
            _xch_locked_db = db_coin_summary.get("xch_locked_count", 0)
            _cat_free_db = db_coin_summary.get("cat_free_count", 0)
            _cat_locked_db = db_coin_summary.get("cat_locked_count", 0)
            coin_tracking = {
                "xch_spendable": _xch_free_db + _xch_locked_db,
                "xch_free": _xch_free_db,
                "xch_locked": _xch_locked_db,
                "xch_total": db_coin_summary.get("xch_total", 0),
                "cat_spendable": _cat_free_db + _cat_locked_db,
                "cat_free": _cat_free_db,
                "cat_locked": _cat_locked_db,
                "cat_total": db_coin_summary.get("cat_total", 0),
                "xch_locked_amount": f"{db_coin_summary.get('xch_locked_mojos', 0) / 1e12:.4f}",
                "cat_locked_amount": f"{db_coin_summary.get('cat_locked_mojos', 0) / (10 ** ((_active_cat.get('decimals') or getattr(cfg, 'CAT_DECIMALS', 3)))):.2f}",
            }
        else:
            _xch_coins = coins_data.get("xch_coins", 0)
            _xch_locked_c = coins_data.get("xch_locked_coins", 0)
            _cat_coins = coins_data.get("cat_coins", 0)
            _cat_locked_c = coins_data.get("cat_locked_coins", 0)
            coin_tracking = {
                "xch_spendable": _xch_coins + _xch_locked_c,
                "xch_free": _xch_coins,
                "xch_locked": _xch_locked_c,
                "xch_total": coins_data.get("xch_total_coins", 0),
                "cat_spendable": _cat_coins + _cat_locked_c,
                "cat_free": _cat_coins,
                "cat_locked": _cat_locked_c,
                "cat_total": coins_data.get("cat_total_coins", 0),
                "xch_locked_amount": inv.get("xch_locked_amount", "0"),
                "cat_locked_amount": inv.get("cat_locked_amount", "0"),
            }

        # If coin tracking is all zeros (bot hasn't run), query Sage directly.
        # Valid Sage filter_mode values: all, selectable, owned, spent, clawback
        # "selectable" = free/spendable coins, "owned" = free + offer-locked
        # Locked = owned - selectable
        if coin_tracking["xch_free"] == 0 and coin_tracking["xch_total"] == 0:
            try:
                from wallet import rpc as wallet_rpc
                cat_asset_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")

                def _count_coins(asset_id, filter_mode):
                    """Query Sage get_coins and return (count, total_mojos)."""
                    result = wallet_rpc("get_coins", {
                        "asset_id": asset_id,
                        "offset": 0, "limit": 500,
                        "filter_mode": filter_mode,
                    }, timeout=10)
                    if not result:
                        return 0, 0
                    coins = (result.get("coins") or result.get("records")
                             or result.get("data") or [])
                    total_mojos = sum(int(c.get("amount", "0")) for c in coins)
                    return len(coins), total_mojos

                # XCH coins: selectable (free) from Sage RPC
                xch_free, xch_free_mojos = _count_coins(None, "selectable")

                # CAT coins: selectable (free) from Sage RPC
                cat_free, cat_free_mojos = _count_coins(cat_asset_id, "selectable")

                cat_dec = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)

                # Locked counts from OFFERS, not from owned-selectable.
                # Each buy offer locks 1 XCH coin; each sell offer locks 1 CAT coin.
                # The old "owned - selectable" formula double-counted because
                # Sage marks coins on both sides of an offer as non-selectable.
                xch_locked = len(offers_buy)
                cat_locked = len(offers_sell)
                xch_locked_mojos = int(sum(
                    float(o.get("size_xch", 0)) * 1e12 for o in offers_buy
                )) if offers_buy else 0
                cat_locked_mojos = int(sum(
                    float(o.get("size_cat", 0)) * (10 ** cat_dec) for o in offers_sell
                )) if offers_sell else 0

                # "spendable" = raw wallet selectable coin count
                # "free" = truly available (spendable minus coins locked by active offers)
                # "total" = spendable + locked (full wallet coin count)
                xch_truly_free = max(0, xch_free - xch_locked)
                cat_truly_free = max(0, cat_free - cat_locked)
                coin_tracking["xch_spendable"] = xch_free
                coin_tracking["xch_free"] = xch_truly_free
                coin_tracking["xch_locked"] = xch_locked
                coin_tracking["xch_total"] = xch_free + xch_locked
                coin_tracking["cat_spendable"] = cat_free
                coin_tracking["cat_free"] = cat_truly_free
                coin_tracking["cat_locked"] = cat_locked
                coin_tracking["cat_total"] = cat_free + cat_locked
                coin_tracking["xch_locked_amount"] = f"{xch_locked_mojos / 1e12:.4f}"
                coin_tracking["cat_locked_amount"] = f"{cat_locked_mojos / (10 ** cat_dec):.2f}"

                if not hasattr(api_status, '_coin_diag_logged'):
                    api_status._coin_diag_logged = True
                    print(f"[STATUS] Coin tracking (Sage RPC):", flush=True)
                    print(f"  XCH: {xch_free} selectable, {xch_locked} locked "
                          f"({len(offers_buy)} buy offers)", flush=True)
                    print(f"  CAT: {cat_free} selectable, {cat_locked} locked "
                          f"({len(offers_sell)} sell offers)", flush=True)

            except Exception as e:
                import traceback
                print(f"[STATUS] Coin tracking RPC failed: {e}", flush=True)
                traceback.print_exc()

        # --- Spread BPS for Close the Gap modal ---
        spread_bps_val = "0"
        if hasattr(bot, '_bot_state') and bot._bot_state.get("spread_bps"):
            spread_bps_val = bot._bot_state["spread_bps"]
        elif hasattr(bot, 'risk_manager') and bot.risk_manager:
            try:
                bs = bot.risk_manager.get_adjusted_spread("buy")
                ss = bot.risk_manager.get_adjusted_spread("sell")
                spread_bps_val = str(int((bs + ss) / 2 * Decimal("10000")))
            except Exception:
                pass

        # --- Arb gap for Close the Gap modal ---
        arb_gap_val = "0"
        if hasattr(bot, '_bot_state') and bot._bot_state.get("arb_gap_bps"):
            arb_gap_val = bot._bot_state["arb_gap_bps"]

        # --- Risk manager state ---
        # F48 (2026-04-09): previously /api/status had no 'risk' key at all,
        # so monitoring scripts that queried for net_position_cat always got
        # None. Expose the inventory state dict directly so the dashboard
        # and external monitors can see the bot's own position estimate.
        risk_out: Dict[str, Any] = {}
        try:
            if hasattr(bot, "risk_manager") and bot.risk_manager:
                inv = bot.risk_manager.get_inventory_state() or {}
                # get_inventory_state() already serializes Decimals to strings,
                # but wrap in _serialize_dict for defence-in-depth.
                risk_out = _serialize_dict(dict(inv))
        except Exception as _risk_err:
            risk_out = {"error": f"risk_state_unavailable: {_risk_err}"}

        # --- Assemble response ---
        result = {
            "running": raw.get("running", False),
            "stats": stats_out,
            "balances": balances_out,
            "pricing": pricing_out,
            "offers": offers_out,
            "logs": logs_out,
            "coin_tracking": coin_tracking,
            "spread_bps": spread_bps_val,
            "arb_gap_bps": arb_gap_val,
            "risk": risk_out,
            "sniper": raw.get("sniper") or {},
            "diagnostics": raw.get("diagnostics") or {},
            "chia_health": _get_health_snapshot() if not raw.get("running", False) else (raw.get("chia_health") or {}),
            "wallet_type": raw.get("wallet_type", "sage"),
            "current_cat": {
                "name": _active_cat.get("name") or (cfg.CAT_NAME if hasattr(cfg, "CAT_NAME") else ""),
                "asset_id": _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""),
                "wallet_id": _active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", None),
                "decimals": _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3),
                "ticker_id": _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", None),
            },
        }

        return jsonify(_serialize_dict(result))
    except Exception as e:
        return _api_error(e, request.path)


def _safe_float(val) -> float:
    """Safely convert a value to float (handles Decimal, str, None)."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


@app.route("/api/diagnostics/runtime")
def api_runtime_diagnostics():
    """Return the live runtime-monitor snapshot."""
    if not bot:
        return jsonify({"enabled": False, "status": "idle", "recent_actions": [], "recent_findings": []})
    try:
        raw = bot.get_state() or {}
        return jsonify(_serialize_dict(raw.get("diagnostics") or {}))
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/diagnostics/api-stats")
def api_diagnostics_api_stats():
    """F45 (2026-04-08): unified usage stats for all 3 external APIs.

    Returns counters for Spacescan (paid call budget), Coinset (hit
    rate vs wallet RPC fallback) and Dexie (post queue, v3 cache),
    plus the live circuit-breaker / rate-limit cooldown status for
    each one. Drives the diagnostics panel so the operator can see
    at a glance which API is doing the work and how much budget is
    left before the bot has to fall back.
    """
    payload: Dict[str, Any] = {
        "spacescan": {"available": False},
        "coinset":   {"available": False},
        "dexie":     {"available": False},
    }

    # --- Spacescan ----------------------------------------------------
    try:
        import spacescan as _ss
        stats = _ss.get_api_stats() or {}
        payload["spacescan"] = {
            "available": True,
            "tier": stats.get("tier", "unknown"),
            "calls_this_session": int(stats.get("calls_this_session", 0) or 0),
            "calls_today": int(stats.get("calls_today", 0) or 0),
            "daily_budget": stats.get("daily_budget", "unknown"),
            "session_uptime_hours": float(stats.get("session_uptime_hours", 0) or 0),
            "call_interval_secs": float(stats.get("call_interval_secs", 0) or 0),
            "rate_limited_until": getattr(_ss, "_rate_limited_until", 0.0),
        }
        # Compute remaining budget when daily_budget is numeric
        try:
            db = stats.get("daily_budget")
            if isinstance(db, (int, float)) and db > 0:
                payload["spacescan"]["budget_remaining"] = max(
                    0, int(db) - int(stats.get("calls_today", 0) or 0)
                )
        except Exception:
            pass
    except Exception as e:
        payload["spacescan"]["error"] = str(e)

    # --- Coinset ------------------------------------------------------
    try:
        if bot is not None and getattr(bot, "coinset_client", None):
            cstats = bot.coinset_client.get_stats() or {}
            payload["coinset"] = {
                "available": True,
                # F53 (2026-04-09): mode tells the operator which code path
                # the client is using. "sage_compat" is the expected state
                # for Sage wallets — the puzzle-hash cache is intentionally
                # skipped, but the individual coin / block / hint APIs are
                # still heavily used via api_calls_total below.
                "mode": str(cstats.get("mode", "unknown")),
                "initialized": bool(cstats.get("initialized", False)),
                "puzzle_hashes_cached": int(cstats.get("puzzle_hashes_cached", 0) or 0),
                # Legacy counter (only fires when puzzle-hash cache is active)
                "total_queries": int(cstats.get("total_queries", 0) or 0),
                "coinset_hits": int(cstats.get("coinset_hits", 0) or 0),
                "coinset_misses": int(cstats.get("coinset_misses", 0) or 0),
                "fallback_count": int(cstats.get("fallback_count", 0) or 0),
                "hit_rate_pct": float(cstats.get("hit_rate_pct", 0) or 0),
                # F53 counters — fire on every HTTP request regardless of mode
                "api_calls_total": int(cstats.get("api_calls_total", 0) or 0),
                "api_calls_by_method": dict(cstats.get("api_calls_by_method", {}) or {}),
                "api_errors_total": int(cstats.get("api_errors_total", 0) or 0),
                "last_coinset_time_ms": float(cstats.get("last_coinset_time_ms", 0) or 0),
                "healthy": bool(cstats.get("healthy", False)),
                "consecutive_failures": int(cstats.get("consecutive_failures", 0) or 0),
                "rate_limited_until": float(getattr(bot.coinset_client, "_rate_limited_until", 0.0) or 0),
            }
    except Exception as e:
        payload["coinset"]["error"] = str(e)

    # Add mempool watcher's Coinset API call count (separate HTTP client)
    try:
        import mempool_watcher as _mw
        _watcher = getattr(_mw, "_watcher_instance", None)
        if _watcher:
            _mw_coinset = getattr(_watcher, "_coinset_api_calls", 0)
            _mw_tibet = getattr(_watcher, "_tibet_api_calls", 0)
            # Add to coinset total
            if payload["coinset"].get("available"):
                payload["coinset"]["mempool_watcher_calls"] = _mw_coinset
                payload["coinset"]["api_calls_total"] = (
                    payload["coinset"].get("api_calls_total", 0) + _mw_coinset
                )
            # Add to tibetswap later (after tibetswap section is built)
    except Exception:
        pass

    # --- Dexie --------------------------------------------------------
    try:
        if bot is not None and getattr(bot, "dexie_manager", None):
            dstats = bot.dexie_manager.get_stats() or {}
            payload["dexie"] = {
                "available": True,
                # Legacy keys kept for backward compat
                "total_posted": int(dstats.get("total_posted", 0) or 0),
                "total_failed": int(dstats.get("total_failed", 0) or 0),
                "total_skipped": int(dstats.get("total_skipped", 0) or 0),
                "queue_size": int(dstats.get("queue_size", 0) or 0),
                "tracked_mappings": int(dstats.get("tracked_mappings", 0) or 0),
                "fingerprints_cached": int(dstats.get("fingerprints_cached", 0) or 0),
                # F53 (2026-04-09): session-scoped counters with clearer
                # semantics. session_posted = posts since bot process
                # started; known_mappings = DB-hydrated + session posts;
                # hydrated_from_db = true when DB had mappings we loaded
                # at startup (explains the common "0 posted, N known"
                # pattern right after a restart).
                "session_posted": int(dstats.get("session_posted", 0) or 0),
                "session_failed": int(dstats.get("session_failed", 0) or 0),
                "session_skipped": int(dstats.get("session_skipped", 0) or 0),
                "known_mappings": int(dstats.get("known_mappings", 0) or 0),
                "hydrated_from_db": bool(dstats.get("hydrated_from_db", False)),
                "rate_limited_until": float(getattr(bot.dexie_manager, "_rate_limited_until", 0.0) or 0),
                "v3_trades_cached_pairs": len(getattr(bot.dexie_manager, "_v3_trades_cache", {}) or {}),
                "v3_pairs_cached": bool(getattr(bot.dexie_manager, "_v3_pairs_cache", None)),
            }
    except Exception as e:
        payload["dexie"]["error"] = str(e)

    # --- TibetSwap / AMM Monitor --------------------------------------
    try:
        if bot is not None and getattr(bot, "amm_monitor", None):
            amm_stats = bot.amm_monitor.get_stats() or {}
            # Also grab price engine stats
            _pe = getattr(bot, "price_engine", None)
            _tibet_cache_age = None
            _pe_tibet_fetches = 0
            _pe_dexie_fetches = 0
            if _pe:
                with getattr(_pe, "_price_lock", type("_", (), {"__enter__": lambda s: s, "__exit__": lambda *a: None})()):
                    _last_tibet_ts = getattr(_pe, "_last_tibet_price_time", 0) or 0
                    if _last_tibet_ts > 0:
                        _tibet_cache_age = round(time.time() - _last_tibet_ts, 1)
                _pe_tibet_fetches = getattr(_pe, "_tibet_price_fetches", 0)
                _pe_dexie_fetches = getattr(_pe, "_dexie_price_fetches", 0)
            # Orderbook refresh count from bot loop
            _ob_refreshes = 0
            try:
                _ob_refreshes = int(bot._bot_state.get("orderbook_refreshes", 0) or 0)
            except Exception:
                pass
            payload["tibetswap"] = {
                "available": bool(amm_stats.get("available", False)),
                "amm_price": amm_stats.get("amm_price"),
                "drift_bps": amm_stats.get("drift_bps"),
                "arb_pressure": amm_stats.get("arb_pressure", 0),
                "arb_pressure_label": amm_stats.get("arb_pressure_label", "unknown"),
                "total_polls": int(amm_stats.get("total_polls", 0) or 0),
                "failed_polls": int(amm_stats.get("failed_polls", 0) or 0),
                "consecutive_failures": int(amm_stats.get("consecutive_failures", 0) or 0),
                "last_success_ago_secs": amm_stats.get("last_success_ago_secs"),
                "price_cache_age_secs": _tibet_cache_age,
                "pair_id": amm_stats.get("pair_id", ""),
                "price_fetches": _pe_tibet_fetches,
            }
            # Add mempool watcher's Tibet API calls
            try:
                import mempool_watcher as _mw2
                _watcher2 = getattr(_mw2, "_watcher_instance", None)
                if _watcher2:
                    _mw_tibet2 = getattr(_watcher2, "_tibet_api_calls", 0)
                    payload["tibetswap"]["mempool_watcher_calls"] = _mw_tibet2
                    payload["tibetswap"]["price_fetches"] = (
                        _pe_tibet_fetches + _mw_tibet2
                    )
            except Exception:
                pass
            # Add Dexie read counters to the Dexie section
            if payload["dexie"].get("available"):
                payload["dexie"]["price_fetches"] = _pe_dexie_fetches
                payload["dexie"]["orderbook_refreshes"] = _ob_refreshes
            # Dynamic buffer stats if available
            dyn = amm_stats.get("dynamic_buffer", {})
            if dyn:
                payload["tibetswap"]["sweep_count_in_window"] = dyn.get("sweep_count_in_window", 0)
                payload["tibetswap"]["buffer_widened"] = dyn.get("current_buffer_bps") is not None
        else:
            payload["tibetswap"] = {"available": False}
    except Exception as e:
        payload["tibetswap"] = {"available": False, "error": str(e)}

    # F53 (2026-04-09): human-readable timestamp without microseconds.
    # Previously this returned a full ISO 8601 with microseconds + offset
    # (e.g. "2026-04-09T10:30:07.842809+00:00") which the operator found
    # confusing. Now we return a clean "YYYY-MM-DD HH:MM:SS UTC" string
    # plus the raw ISO as a secondary field for programmatic use.
    try:
        _now = datetime.now(timezone.utc)
        payload["generated_at"] = _now.strftime("%Y-%m-%d %H:%M:%S UTC")
        payload["generated_at_iso"] = _now.isoformat(timespec="seconds")
    except Exception:
        payload["generated_at"] = None
        payload["generated_at_iso"] = None
    return jsonify(payload)


def _sage_ts_to_iso(ts) -> str:
    """Convert a Sage creation_timestamp (unix epoch) to ISO format string."""
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ""


@app.route("/api/bot/price")
def api_bot_price():
    """Get current price info."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(_serialize_dict(bot.get_price_info()))


# ---------------------------------------------------------------------------
# Config Routes
# ---------------------------------------------------------------------------

@app.route("/api/config")
def api_config_get():
    """Get all configuration (excludes secrets)."""
    return jsonify(cfg.to_dict())


@app.route("/api/fees/status")
def api_fees_status():
    """Get fee settings plus the current effective/suggested fee snapshot."""
    return jsonify({"success": True, **get_fee_settings_snapshot()})


def _apply_sage_change_address_setting() -> dict:
    """Apply the opt-in Sage change-address setting immediately when possible."""
    try:
        from wallet import get_wallet_type, get_next_address
        if get_wallet_type() != "sage":
            return {"attempted": False, "success": False, "error": "wallet_not_sage"}
        if not getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False):
            return {"attempted": False, "success": False, "error": "setting_disabled"}

        addr_result = get_next_address(new_address=False)
        if not addr_result or not addr_result.get("success") or not addr_result.get("address"):
            return {"attempted": True, "success": False, "error": "wallet_address_unavailable"}

        cfg.WALLET_ADDRESS = addr_result["address"]
        from wallet_sage import set_change_address as _sage_set_change_address
        result = _sage_set_change_address(cfg.WALLET_ADDRESS)
        if result and result.get("success"):
            log_event("success", "sage_change_address_set",
                      f"Sage change address set to {cfg.WALLET_ADDRESS[:20]}... "
                      f"for fingerprint {result.get('fingerprint')}")
            return {"attempted": True, **result}

        error = (result or {}).get("error", "unknown_error")
        log_event("warning", "sage_change_address_failed",
                  f"Could not set Sage change address via API: {error}")
        return {"attempted": True, "success": False, "error": error}
    except Exception as e:
        log_event("warning", "sage_change_address_failed",
                  f"Error applying Sage change address via API: {e}")
        return {"attempted": True, "success": False, "error": "Change address failed"}


@app.route("/api/config", methods=["POST"])
def api_config_update():
    """Update configuration settings.

    Accepts two formats:
      Single:  {"key": "SPREAD_BPS", "value": "800"}
      Bulk:    {"spread_bps": 800, "loop_seconds": 90, ...}
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400

    # Block security-sensitive settings from API modification.
    # These can only be changed by editing .env directly.
    blocked = {
        # Secrets & credentials
        "CHIA_WALLET_CERT", "CHIA_WALLET_KEY", "WALLET_FINGERPRINT",
        "SPACESCAN_API_KEY", "SAGE_CERT_PATH", "SAGE_KEY_PATH",
        # Endpoint URLs — an attacker could redirect wallet/API calls
        "CHIA_WALLET_RPC_URL", "CHIA_FULL_NODE_RPC_URL", "SAGE_RPC_URL",
        "DEXIE_API_BASE", "TIBET_API_BASE",
        "SPLASH_SUBMIT_URL", "COINSET_API_URL",
        "SPACESCAN_PRO_URL", "SPACESCAN_FREE_URL",
        # Wallet type — changing mid-run would break everything
        "WALLET_TYPE",
        # CAT identity — changing mid-run creates wrong offers
        "CAT_ASSET_ID",
    }

    # Detect format: single key-value pair or bulk config object
    if "key" in data and "value" in data:
        # --- Single key-value format ---
        key = data["key"]
        value = data["value"]
        if key in blocked:
            return jsonify({"success": False, "error": f"Cannot modify {key} via API"}), 403
        ok = cfg.update(key, str(value), source="api_settings_save")
        if ok:
            extra = None
            if key == "SAGE_SET_CHANGE_ADDRESS" and str(value).strip().lower() in ("true", "1", "yes", "on"):
                extra = _apply_sage_change_address_setting()
            safe_value = "***" if _is_sensitive_key(key) else value
            log_event("info", "config_changed", f"Config updated: {key} = {safe_value}")
            response = {"success": True, "status": "updated", "key": key,
                        "change_address_result": extra}
            event_payload = {"key": key, "value": safe_value}
            notice = _get_live_requote_notice([key])
            if notice:
                response["apply_mode"] = notice["apply_mode"]
                response["warning"] = notice["warning"]
                event_payload["apply_mode"] = notice["apply_mode"]
                event_payload["warning"] = notice["warning"]
            events.emit("config_changed", event_payload)
            return jsonify(response)
        return jsonify({"success": False, "error": f"Failed to update {key}"}), 500
    else:
        # --- Bulk format: GUI sends {spread_bps: 800, loop_seconds: 90, ...} ---
        # Map lowercase GUI keys → uppercase .env keys
        key_map = {
            "spread_bps": "SPREAD_BPS",
            "loop_seconds": "LOOP_SECONDS",
            "default_trade_xch": "DEFAULT_TRADE_XCH",
            "max_active_buy": "MAX_ACTIVE_BUY",
            "max_active_sell": "MAX_ACTIVE_SELL",
            "auto_requote": "AUTO_REQUOTE",
            "requote_bps": "REQUOTE_BPS",
            "requote_cooldown": "REQUOTE_COOLDOWN_SECS",
            "requote_batch_size": "REQUOTE_BATCH_SIZE",
            "xch_reserve": "XCH_RESERVE",
            "cat_reserve": "CAT_RESERVE",
            # F49 (2026-04-09): topup pool (two-tier reserve system)
            "topup_pool_pct": "TOPUP_POOL_PCT",
            "topup_pool_xch": "TOPUP_POOL_XCH",
            "topup_pool_cat": "TOPUP_POOL_CAT",
            # max_mid_move_bps removed 2026-04-08 — config key was never
            # consumed by trading code. Use DYNAMIC_LIMIT_PCT and
            # MAX_STEP_CHANGE_FRACTION for the equivalent guards.
            "dynamic_limit_pct": "DYNAMIC_LIMIT_PCT",
            "max_step_change_fraction": "MAX_STEP_CHANGE_FRACTION",
            "min_mid": "HARD_MIN_PRICE_XCH",
            "max_mid": "HARD_MAX_PRICE_XCH",
            "price_strategy": "PRICE_STRATEGY",
            "arb_threshold_bps": "ARB_ALERT_THRESHOLD_BPS",
            "offer_expiry_minutes": "OFFER_EXPIRY_SECS",
            # V2: Dynamic Spreads
            "dynamic_spread_enabled": "DYNAMIC_SPREAD_ENABLED",
            "base_spread_bps": "BASE_SPREAD_BPS",
            "min_edge_bps": "MIN_EDGE_BPS",
            "min_spread_bps": "MIN_SPREAD_BPS",
            "max_spread_bps": "MAX_SPREAD_BPS",
            "volatility_window_hours": "VOLATILITY_WINDOW_HOURS",
            # V2: Inventory Management
            "inventory_enabled": "INVENTORY_ENABLED",
            "skew_intensity": "SKEW_INTENSITY",
            "max_position_xch": "MAX_POSITION_XCH",
            # V2: Tiered Orders
            "tier_enabled": "TIER_ENABLED",
            "buy_ladder_reversed": "BUY_LADDER_REVERSED",
            "inner_size_xch": "INNER_SIZE_XCH",
            "mid_size_xch": "MID_SIZE_XCH",
            "outer_size_xch": "OUTER_SIZE_XCH",
            "extreme_size_xch": "EXTREME_SIZE_XCH",
            # F62 (2026-04-09): per-side tier sizes. Independent buy/sell
            # offer sizing so each side can fully use its own balance.
            "buy_inner_size_xch":   "BUY_INNER_SIZE_XCH",
            "buy_mid_size_xch":     "BUY_MID_SIZE_XCH",
            "buy_outer_size_xch":   "BUY_OUTER_SIZE_XCH",
            "buy_extreme_size_xch": "BUY_EXTREME_SIZE_XCH",
            "sell_inner_size_xch":   "SELL_INNER_SIZE_XCH",
            "sell_mid_size_xch":     "SELL_MID_SIZE_XCH",
            "sell_outer_size_xch":   "SELL_OUTER_SIZE_XCH",
            "sell_extreme_size_xch": "SELL_EXTREME_SIZE_XCH",
            "inner_tier_count": "INNER_TIER_COUNT",
            "mid_tier_count": "MID_TIER_COUNT",
            "outer_tier_count": "OUTER_TIER_COUNT",
            "extreme_tier_count": "EXTREME_TIER_COUNT",
            "buy_inner_tier_count": "BUY_INNER_TIER_COUNT",
            "buy_mid_tier_count": "BUY_MID_TIER_COUNT",
            "buy_outer_tier_count": "BUY_OUTER_TIER_COUNT",
            "buy_extreme_tier_count": "BUY_EXTREME_TIER_COUNT",
            "sell_inner_tier_count": "SELL_INNER_TIER_COUNT",
            "sell_mid_tier_count": "SELL_MID_TIER_COUNT",
            "sell_outer_tier_count": "SELL_OUTER_TIER_COUNT",
            "sell_extreme_tier_count": "SELL_EXTREME_TIER_COUNT",
            "inner_tier_spare_count": "INNER_TIER_SPARE_COUNT",
            "mid_tier_spare_count": "MID_TIER_SPARE_COUNT",
            "outer_tier_spare_count": "OUTER_TIER_SPARE_COUNT",
            "extreme_tier_spare_count": "EXTREME_TIER_SPARE_COUNT",
            "buy_inner_tier_spare_count": "BUY_INNER_TIER_SPARE_COUNT",
            "buy_mid_tier_spare_count": "BUY_MID_TIER_SPARE_COUNT",
            "buy_outer_tier_spare_count": "BUY_OUTER_TIER_SPARE_COUNT",
            "buy_extreme_tier_spare_count": "BUY_EXTREME_TIER_SPARE_COUNT",
            "sell_inner_tier_spare_count": "SELL_INNER_TIER_SPARE_COUNT",
            "sell_mid_tier_spare_count": "SELL_MID_TIER_SPARE_COUNT",
            "sell_outer_tier_spare_count": "SELL_OUTER_TIER_SPARE_COUNT",
            "sell_extreme_tier_spare_count": "SELL_EXTREME_TIER_SPARE_COUNT",
            # V2: Market Intelligence
            "competitor_aware_enabled": "COMPETITOR_AWARE_ENABLED",
            "dbx_max_spread_bps": "DBX_MAX_SPREAD_BPS",
            # V2: Coin Prep
            "coin_prep_multiplier": "COIN_PREP_MULTIPLIER",
            "coin_prep_headroom_pct": "COIN_PREP_HEADROOM_PCT",
            "transaction_fee_mode": "TRANSACTION_FEE_MODE",
            "transaction_fee_xch": "TRANSACTION_FEE_XCH",
            "transaction_fee_target_secs": "TRANSACTION_FEE_TARGET_SECS",
            "transaction_fee_estimate_cost": "TRANSACTION_FEE_ESTIMATE_COST",
            "fee_prep_count": "FEE_PREP_COUNT",
            "fee_coin_size_xch": "FEE_COIN_SIZE_XCH",
            # V2: Bot Operations
            "sniper_enabled": "SNIPER_ENABLED",
            "sniper_size_xch": "SNIPER_SIZE_XCH",
            "sniper_prep_count": "SNIPER_PREP_COUNT",
            "sniper_rearm_price_move_bps": "SNIPER_REARM_PRICE_MOVE_BPS",
            "sniper_rearm_gap_move_bps": "SNIPER_REARM_GAP_MOVE_BPS",
            "splash_enabled": "SPLASH_ENABLED",
            "enable_coin_prep": "ENABLE_COIN_PREP",
            "enable_runtime_coin_health": "ENABLE_RUNTIME_COIN_HEALTH",
            "sage_set_change_address": "SAGE_SET_CHANGE_ADDRESS",
        }
        updated = []
        errors = []
        for gui_key, value in data.items():
            env_key = key_map.get(gui_key, gui_key.upper())
            if env_key in blocked:
                continue
            ok = cfg.update(env_key, str(value), source="api_settings_save")
            if ok:
                updated.append(env_key)
            else:
                errors.append(env_key)

        response = {
            "success": len(errors) == 0,
            "status": "updated",
            "updated": updated,
            "errors": errors,
            "change_address_result": None,
        }

        if updated:
            # LEGACY KEY CLEARING: When HARD_MAX_PRICE_XCH or HARD_MIN_PRICE_XCH
            # are written, also clear the legacy MAX_MID / MIN_MID keys.
            # These old keys exist in .env files from pre-V2 configs. The config
            # fallback chain reads them as a fallback when the HARD_* values are
            # empty, silently overriding the new settings. Clearing them here
            # removes the ambiguity permanently — HARD_* is the single source.
            legacy_cleared = []
            if "HARD_MAX_PRICE_XCH" in updated:
                if cfg.update("MAX_MID", ""):
                    legacy_cleared.append("MAX_MID")
            if "HARD_MIN_PRICE_XCH" in updated:
                if cfg.update("MIN_MID", ""):
                    legacy_cleared.append("MIN_MID")
            if legacy_cleared:
                log_event("info", "legacy_keys_cleared",
                          f"Cleared legacy price rail keys: {', '.join(legacy_cleared)} "
                          f"(superseded by HARD_MAX/MIN_PRICE_XCH)")
                updated.extend(legacy_cleared)

            log_event("info", "config_changed", f"Bulk config updated: {', '.join(updated)}")
            event_payload = {"keys": updated}
            notice = _get_live_requote_notice(updated)
            if notice:
                response["apply_mode"] = notice["apply_mode"]
                response["warning"] = notice["warning"]
                event_payload["apply_mode"] = notice["apply_mode"]
                event_payload["warning"] = notice["warning"]
            events.emit("config_changed", event_payload)

        extra = None
        if ("SAGE_SET_CHANGE_ADDRESS" in updated and
                str(getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False)).lower() == "true"):
            extra = _apply_sage_change_address_setting()

        response["change_address_result"] = extra
        return jsonify(response)


@app.route("/api/config/reload", methods=["POST"])
def api_config_reload():
    """Reload config from .env file."""
    cfg.reload()
    events.emit("config_changed", {"action": "full_reload"})
    return jsonify({"status": "reloaded"})


@app.route("/api/config/apply", methods=["POST"])
def api_config_apply():
    """Apply config changes gracefully while bot is running (V1 parity).

    Instead of stop→change→restart (which causes a gap in market presence),
    this keeps the 2 tightest offers per side alive, cancels outer offers
    to free coins, reloads config, and lets the normal bot cycle rebuild.
    """
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    if not bot.is_running():
        # Not running — just reload config
        cfg.reload()
        return jsonify({"status": "reloaded", "message": "Bot not running — config reloaded directly"})

    result = bot.graceful_config_change()
    events.emit("config_changed", {"action": "graceful_apply", "result": result})
    return jsonify(result)


@app.route("/api/config/live", methods=["POST"])
def api_config_live():
    """Live control endpoint — update a single config key and optionally
    trigger a graceful apply.  Used by the Live Controls bar in the GUI.

    Body: {"key": "BASE_SPREAD_BPS", "value": "600", "graceful": true}
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    if "key" not in data or "value" not in data:
        return jsonify({"success": False, "error": "Missing key/value"}), 400

    key = data["key"]
    value = str(data["value"])
    graceful = data.get("graceful", False)

    # Block unsafe keys from live controls — must match api_config_update blocklist
    blocked = {
        # Secrets & credentials
        "CHIA_WALLET_CERT", "CHIA_WALLET_KEY", "WALLET_FINGERPRINT",
        "SPACESCAN_API_KEY", "SAGE_CERT_PATH", "SAGE_KEY_PATH",
        # Endpoint URLs — an attacker could redirect wallet/API calls
        "CHIA_WALLET_RPC_URL", "CHIA_FULL_NODE_RPC_URL", "SAGE_RPC_URL",
        "DEXIE_API_BASE", "TIBET_API_BASE",
        "SPLASH_SUBMIT_URL", "COINSET_API_URL",
        "SPACESCAN_PRO_URL", "SPACESCAN_FREE_URL",
        # Wallet type — changing mid-run would break everything
        "WALLET_TYPE",
        # CAT identity — changing mid-run creates wrong offers
        "CAT_ASSET_ID",
    }
    if key in blocked:
        return jsonify({"success": False, "error": f"Cannot modify {key} via live controls"}), 403

    # Apply the config change
    # F26 (2026-04-08): pass source so config_history audit shows where the change came from
    ok = cfg.update(key, value, source="gui_live_control")
    if not ok:
        return jsonify({"success": False, "error": f"Failed to update {key}"}), 500

    log_event("info", "config_live", f"Live control: {key} = {value}")
    response = {"success": True, "key": key}

    # Warn when bot is in recovery mode — config changes may be deferred
    if bot:
        try:
            bot_status = (bot.get_state() or {}).get("status", "")
            if bot_status == "recovering":
                response["recovery_warning"] = (
                    "Bot is currently in recovery mode \u2014 config changes may not "
                    "take effect until recovery completes."
                )
        except Exception:
            pass
    event_payload = {"key": key, "value": value, "source": "live_controls"}

    if bot and bot.is_running() and key in _LIVE_REQUOTE_ONLY_KEYS:
        warning = (
            "Saved without live offer migration — existing offers stay live and "
            "the change will take effect on future requotes and new offers."
        )
        response["apply_mode"] = "next_requote"
        response["warning"] = warning
        event_payload["apply_mode"] = "next_requote"
        event_payload["warning"] = warning

    events.emit("config_changed", event_payload)

    if graceful and key in _LIVE_REQUOTE_ONLY_KEYS:
        response["graceful"] = {
            "status": "skipped",
            "message": "Live migration is disabled for this control; existing offers were left in place.",
        }
        return jsonify(response)

    # Optionally trigger graceful apply for spread/sizing changes
    if graceful and bot and bot.is_running():
        try:
            result = bot.graceful_config_change()
            events.emit("config_changed", {"action": "graceful_apply", "result": result})
            response["graceful"] = result
            return jsonify(response)
        except Exception as e:
            log_event("error", "api_error", f"Config apply graceful error: {e}")
            response["graceful_error"] = "Apply failed — see debug log"
            return jsonify(response)

    return jsonify(response)


# ---------------------------------------------------------------------------
# Offer Routes
# ---------------------------------------------------------------------------

@app.route("/api/offers")
def api_offers():
    """Get current open offers."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    open_buys, open_sells, _ = bot.offer_manager.sync_from_wallet()

    return jsonify({
        "buys": _serialize_offers(open_buys),
        "sells": _serialize_offers(open_sells),
        "buy_count": len(open_buys),
        "sell_count": len(open_sells),
    })


@app.route("/api/offers/cancel_all/status")
def api_cancel_all_status():
    """Return the live cancel-all progress state for the GUI."""
    return jsonify({"success": True, **_get_cancel_all_state()})


@app.route("/api/offers/open_count")
def api_open_offer_count():
    """Return the number of still-active offers in the wallet.

    Used by the shutdown flow to verify cancels actually confirmed
    on-chain before proceeding with app exit.
    """
    try:
        from database import get_open_offers
        open_offers = get_open_offers()
        return jsonify({"success": True, "open_count": len(open_offers)})
    except Exception as e:
        return jsonify({"success": False, "open_count": -1, "error": str(e)})


@app.route("/api/offers/cancel_all", methods=["POST"])
def api_cancel_all():
    """Cancel all open offers when the bot is not actively managing the book."""
    slog("GUI_ACTION", ">>> BUTTON: Cancel All Offers")
    cancelled = 0
    failed = 0

    if bot and bot.is_running():
        msg = ("Stop the bot before cancelling all offers. "
               "A live cancel can race with automatic requotes and recreate the book.")
        log_event("warning", "cancel_all_blocked_live", msg)
        return jsonify({
            "success": False,
            "error": msg,
            "requires_stop": True,
        }), 409

    state = _get_cancel_all_state()
    if state.get("running"):
        return jsonify({
            "success": False,
            "error": "Cancel all is already in progress.",
        }), 409

    _reset_cancel_all_state(
        running=True,
        complete=False,
        error=None,
        phase="starting",
        message="Preparing cancel-all request...",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
    )

    if bot and bot.is_running() and bot.offer_manager:
        # Bot is live — use offer manager (handles database updates + fill tracking)
        try:
            def on_progress(payload):
                _set_cancel_all_state(**payload)

            result = bot.offer_manager.cancel_all(progress_callback=on_progress)
            for tid, res in result.items():
                if res and res.get("success"):
                    cancelled += 1
                else:
                    failed += 1
            _set_cancel_all_state(
                running=False,
                complete=True,
                error=None,
                phase="complete",
                total=cancelled + failed,
                cancelled=cancelled,
                failed=failed,
                finished_at=datetime.now(timezone.utc).isoformat(),
                message=f"Cancel all complete: {cancelled} succeeded, {failed} failed.",
            )
            events.emit("offers_cancelled", {"count": cancelled, "reason": "manual_cancel_all"})
            # Reset gap closer state if active (cancel_all includes gap-closer offers)
            if bot.boost_manager._boost_active:
                bot.boost_manager._boost_active = False
                bot.boost_manager._active_boost_ids.clear()
                bot.boost_manager._boost_mid_price = Decimal("0")
                bot.boost_manager._gap_spread_bps = 0
                bot.boost_manager._convergence_factor = Decimal("1.0")
                events.emit("boost", {"active": False})
        except Exception as e:
            _set_cancel_all_state(
                running=False,
                complete=False,
                error=str(e),
                phase="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                message=f"Cancel all failed: {e}",
            )
            return _api_error(e, request.path)
    else:
        # Bot stopped or not started — cancel directly via wallet RPC.
        # Always use the wallet as source of truth, not the database,
        # because requoting or failed cancels can leave orphaned offers
        # that exist in the wallet but aren't tracked in the DB.
        #
        # Run in a BACKGROUND THREAD so the HTTP response returns instantly
        # and the GUI can poll /api/offers/cancel_all/status for live progress
        # instead of hanging for 2-3 minutes with no feedback.
        try:
            from wallet import get_all_offers, cancel_offers_batch, is_offer_time_expired
            all_offers = get_all_offers(include_completed=False, end=500)
            if not all_offers:
                _set_cancel_all_state(
                    running=False,
                    complete=True,
                    error=None,
                    phase="complete",
                    message="No offers found to cancel.",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return jsonify({"success": True, "cancelled": 0, "message": "No offers found"})

            # Filter to open offers only.
            # Accept both Chia statuses (PENDING_ACCEPT / 4) and
            # Sage statuses (ACTIVE / OPEN / PENDING).
            # Sage may return integer status (0/1 = open) or string.
            OPEN_STATUSES = {"PENDING_ACCEPT", "4", "ACTIVE", "OPEN",
                             "PENDING", "PENDING_CONFIRM", "IN_PROGRESS",
                             "0", "1"}
            open_ids = []
            for o in (all_offers if isinstance(all_offers, list) else []):
                if not isinstance(o, dict):
                    continue
                raw_status = o.get("status", "")
                status = str(raw_status).upper() if raw_status is not None else ""
                # Integer status: 0 or 1 = open in Sage
                is_open = (status in OPEN_STATUSES
                           or (isinstance(raw_status, int) and raw_status <= 1))
                if is_open:
                    if not is_offer_time_expired(o):
                        tid = o.get("trade_id", "") or o.get("offer_id", "")
                        if tid:
                            open_ids.append(tid)

            if not open_ids:
                _set_cancel_all_state(
                    running=False,
                    complete=True,
                    error=None,
                    phase="complete",
                    message="No active offers found to cancel.",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                return jsonify({"success": True, "cancelled": 0, "message": "No active offers found"})

            # Set initial progress state — frontend polls this immediately.
            _set_cancel_all_state(
                running=True,
                complete=False,
                error=None,
                phase="running",
                total=len(open_ids),
                batch_size=len(open_ids),
                total_batches=1,
                current_batch=1,
                cancelled=0,
                failed=0,
                message=f"Cancelling {len(open_ids)} offers directly from the wallet...",
            )
            log_event("info", "cancel_all_direct",
                      f"Cancelling {len(open_ids)} offers directly via wallet "
                      f"(bot stopped, bypassing DB)")

            # ---- Background worker ----
            _cancel_open_ids = list(open_ids)  # snapshot

            def _cancel_all_worker():
                _w_cancelled = 0
                _w_failed = 0
                try:
                    _results = cancel_offers_batch(_cancel_open_ids, secure=True)
                    _cancelled_ids = []
                    for _tid, _res in _results.items():
                        if _res and _res.get("success"):
                            _w_cancelled += 1
                            _cancelled_ids.append(_tid)
                        else:
                            _w_failed += 1
                    # Sync DB: mark cancelled offers so they don't reappear
                    if _cancelled_ids:
                        try:
                            conn = get_connection()
                            for _tid in _cancelled_ids:
                                conn.execute(
                                    "UPDATE offers SET status='cancelled' "
                                    "WHERE trade_id=? AND status='open'",
                                    (_tid,),
                                )
                            conn.commit()
                        except Exception:
                            pass  # DB sync is best-effort
                    _set_cancel_all_state(
                        running=False,
                        complete=True,
                        error=None,
                        phase="complete",
                        total=len(_cancel_open_ids),
                        batch_size=len(_cancel_open_ids),
                        total_batches=1,
                        current_batch=1,
                        batch_cancelled=_w_cancelled,
                        batch_failed=_w_failed,
                        cancelled=_w_cancelled,
                        failed=_w_failed,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        message=f"Cancel all complete: {_w_cancelled} succeeded, {_w_failed} failed.",
                    )
                    events.emit("offers_cancelled", {"count": _w_cancelled, "reason": "manual_cancel_all"})
                    log_event("info", "cancel_all_complete",
                              f"Cancel all finished: {_w_cancelled} succeeded, {_w_failed} failed")
                    # Reset gap closer state if active
                    if bot and getattr(bot, "boost_manager", None):
                        try:
                            if bot.boost_manager._boost_active:
                                bot.boost_manager._boost_active = False
                                bot.boost_manager._active_boost_ids.clear()
                                bot.boost_manager._boost_mid_price = Decimal("0")
                                bot.boost_manager._gap_spread_bps = 0
                                bot.boost_manager._convergence_factor = Decimal("1.0")
                                events.emit("boost", {"active": False})
                        except Exception:
                            pass
                except Exception as _e:
                    _set_cancel_all_state(
                        running=False,
                        complete=False,
                        error=str(_e),
                        phase="error",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        message=f"Cancel all failed: {_e}",
                    )
                    log_event("error", "cancel_all_error",
                              f"Cancel all background worker failed: {_e}")

            _t = threading.Thread(target=_cancel_all_worker, name="cancel-all-bg",
                                  daemon=True)
            _t.start()

            # Return immediately — frontend polls /api/offers/cancel_all/status
            return jsonify({
                "success": True,
                "async": True,
                "total": len(open_ids),
                "message": f"Cancelling {len(open_ids)} offers in background...",
            })

        except Exception as e:
            _set_cancel_all_state(
                running=False,
                complete=False,
                error=str(e),
                phase="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                message=f"Cancel all failed: {e}",
            )
            return _api_error(e, request.path)

    return jsonify({
        "success": True,
        "cancelled": cancelled,
        "failed": failed,
    })


@app.route("/api/offers/cleanup_orphans", methods=["POST"])
def api_cleanup_orphans():
    """Find and cancel wallet offers not tracked by the bot.

    These are "ghost" offers — the bot tried to cancel them but the
    on-chain cancel failed. They're still live on Dexie but the bot
    doesn't know about them. This endpoint finds and cancels them.
    """
    slog("GUI_ACTION", ">>> BUTTON: Cleanup Orphaned Offers")

    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        result = bot.cleanup_orphaned_offers()
        return jsonify({"success": True, **result})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/offers/cancel", methods=["POST"])
def api_cancel_offer():
    """Cancel a specific offer.

    Body: {"trade_id": "0x..."}
    """
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Guard: warn if bot is actively creating/requoting — the cancel can
    # race with the wallet. Still allow it (user may need to emergency-cancel)
    # but surface the risk so callers can back off if appropriate.
    if bot.is_running() and bot.coin_manager.is_busy():
        log_event("warning", "cancel_while_busy",
                  "Manual cancel issued while coin operations are in progress")

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400
    trade_id = data.get("trade_id", "")
    if not trade_id or not isinstance(trade_id, str):
        return jsonify({"error": "Missing trade_id"}), 400

    try:
        result = bot.offer_manager.cancel_offers([trade_id], reason="manual_api")
    except Exception as e:
        return _api_error(e, request.path)
    # cancel_offers returns a dict; surface any storm-protection refusal
    if isinstance(result, dict) and result.get("error"):
        return jsonify({"success": False, "trade_id": trade_id, **result}), 400
    return jsonify({"success": True, "status": "cancelled", "trade_id": trade_id})


# ---------------------------------------------------------------------------
# Close the Gap Routes (adaptive Dexie ranking improvement)
# ---------------------------------------------------------------------------

@app.route("/api/boost/activate", methods=["POST"])
def api_boost_activate():
    """Activate Close the Gap — adaptive spread probing for ranking."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Guard: runtime_monitor must have a fresh Dexie orderbook snapshot before
    # we can compute a meaningful starting spread. If pressed immediately after
    # startup the market data is empty and we'd fall back to the configured
    # spread, placing probes far behind the real best prices.
    try:
        rm_check = bot.runtime_monitor.get_state() if hasattr(bot, "runtime_monitor") else {}
        market_check = rm_check.get("market", {})
        orderbook_age = float(market_check.get("orderbook_age_secs", 9999) or 9999)
        orderbook_refreshes = int(market_check.get("orderbook_refreshes", 0) or 0)
        if orderbook_refreshes == 0 or orderbook_age > 120:
            return jsonify({
                "error": "Market data not ready yet — please wait a few seconds and try again.",
                "orderbook_refreshes": orderbook_refreshes,
                "orderbook_age_secs": orderbook_age,
            }), 503
    except Exception:
        pass  # If monitor check fails, proceed anyway

    mid_price = bot._current_mid_price
    # Get current arb gap from TibetSwap
    arb_gap = Decimal("0")
    try:
        arb_gap = Decimal(str(bot._bot_state.get("arb_gap_bps", 0)))
    except Exception:
        pass

    # Compute the starting spread from the best prices currently visible on Dexie.
    # This includes both our own offers and competitor offers, so the sniper probe
    # starts just inside the TIGHTEST existing offer on each side — immediately
    # competitive rather than behind the current best prices.
    main_spread_bps = 0
    start_pct_override_dexie = None  # May be overridden below
    try:
        mid_f = float(mid_price) if mid_price and float(mid_price) > 0 else 0
        if mid_f > 0:
            # Collect best prices from runtime_monitor's cached market state.
            # bot._bot_state does NOT contain diagnostics — that's assembled by
            # runtime_monitor on demand. We read it directly from the monitor cache.
            rm_state = {}
            try:
                rm_state = bot.runtime_monitor.get_state() if hasattr(bot, "runtime_monitor") else {}
            except Exception:
                pass
            market_diag = rm_state.get("market", {})
            our_bid  = float(market_diag.get("our_best_bid",  0) or 0)
            our_ask  = float(market_diag.get("our_best_ask",  0) or 0)
            comp_bid = float(market_diag.get("best_competitor_bid", 0) or 0)
            comp_ask = float(market_diag.get("best_competitor_ask", 0) or 0)

            # Best overall Dexie bid/ask (highest buy, lowest sell)
            candidates_bid = [p for p in [our_bid, comp_bid] if p > 0]
            candidates_ask = [p for p in [our_ask, comp_ask] if p > 0 and p > mid_f]
            best_dexie_bid = max(candidates_bid) if candidates_bid else 0
            best_dexie_ask = min(candidates_ask) if candidates_ask else 0

            if best_dexie_bid > 0 and best_dexie_ask > 0 and best_dexie_ask > best_dexie_bid:
                # Half-spread from mid on each side
                buy_half_bps  = int(((mid_f - best_dexie_bid) / mid_f) * 10000)
                sell_half_bps = int(((best_dexie_ask - mid_f) / mid_f) * 10000)
                # Use the TIGHTER half doubled — probe enters inside the tightest side
                tighter_half  = min(buy_half_bps, sell_half_bps)
                if tighter_half > 0:
                    # 95% of the tighter side = just inside the current best price
                    main_spread_bps = max(1, int(tighter_half * 2 * 0.95))
                    start_pct_override_dexie = 100  # Use exact value, no further reduction

        # Fallback: use our own ladder innermost spread if Dexie data unavailable
        if main_spread_bps == 0:
            from database import get_open_offers
            open_offers = get_open_offers() or []
            ladder_offers = [o for o in open_offers
                             if o.get("tier") != "boost" and float(o.get("price", 0)) > 0]
            buys  = [float(o["price"]) for o in ladder_offers if o.get("side") == "buy"]
            sells = [float(o["price"]) for o in ladder_offers if o.get("side") == "sell"]
            if buys and sells:
                innermost_bps = int(((min(sells) - max(buys)) / mid_f) * 10000)
                if innermost_bps > 0:
                    main_spread_bps = innermost_bps

        # Last resort: risk-manager configured spread
        if main_spread_bps == 0 and bot.risk_manager:
            buy_spread  = bot.risk_manager.get_adjusted_spread("buy")  * Decimal("10000")
            sell_spread = bot.risk_manager.get_adjusted_spread("sell") * Decimal("10000")
            main_spread_bps = int((buy_spread + sell_spread) / 2)
    except Exception:
        pass

    # Read optional custom settings from request body
    size_xch_override = None
    start_pct_override = None
    try:
        data = request.get_json(silent=True) or {}
        if "size_xch" in data:
            size_xch_override = Decimal(str(data["size_xch"]))
        if "start_pct" in data:
            start_pct_override = int(data["start_pct"])
    except (ValueError, TypeError):
        pass

    # If Dexie-derived spread was computed above and the user didn't supply
    # an explicit start_pct, apply start_pct=100 so the exact Dexie-based
    # spread is used without further reduction.
    if start_pct_override is None and start_pct_override_dexie is not None:
        start_pct_override = start_pct_override_dexie

    # Run activation in background thread so GUI doesn't block
    # while waiting for wallet RPC (which can be slow if topup is running)
    import threading

    # Calculate expected spread for immediate GUI feedback
    start_pct = start_pct_override or getattr(cfg, "GAP_CLOSE_START_PCT", 75)
    expected_spread = max(1, int(main_spread_bps * start_pct / 100)) if main_spread_bps > 0 else getattr(cfg, "BOOST_SPREAD_BPS", 200)
    buffer = getattr(cfg, "GAP_CLOSE_SAFETY_BUFFER_BPS", 20)
    expected_floor = max(1, int(arb_gap) + buffer)
    expected_spread = max(expected_spread, expected_floor)

    def _activate_bg():
        try:
            result = bot.boost_manager.activate(
                mid_price, arb_gap_bps=arb_gap,
                main_spread_bps=main_spread_bps,
                size_xch_override=size_xch_override,
                start_pct_override=start_pct_override,
            )
            if result.get("success"):
                events.emit("boost", bot.boost_manager.get_state())
            elif result.get("error"):
                log_event("error", "gap_closer_activate_failed", result["error"])
        except Exception as e:
            log_event("error", "gap_closer_activate_error", f"Activation failed: {e}")

    t = threading.Thread(target=_activate_bg, daemon=True)
    t.start()

    # Return immediately with expected values so GUI updates fast
    return jsonify({
        "success": True,
        "spread_bps": expected_spread,
        "arb_floor_bps": expected_floor,
        "created": 0,  # Actual creation happens in background
        "async": True,
        "warnings": [],
    })


@app.route("/api/boost/deactivate", methods=["POST"])
def api_boost_deactivate():
    """Deactivate Close the Gap — cancel all gap-closer offers."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    result = bot.boost_manager.deactivate()
    events.emit("boost", {"active": False})
    return jsonify(result)


@app.route("/api/boost/state")
def api_boost_state():
    """Get current boost state."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.boost_manager.get_state())


# ---------------------------------------------------------------------------
# Fill & PnL Routes
# ---------------------------------------------------------------------------

@app.route("/api/fills")
def api_fills():
    """Get recent fill history."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    limit = request.args.get("limit", 20, type=int)
    from database import get_fills
    fills = get_fills(
        cat_asset_id=cfg.CAT_ASSET_ID,
        limit=limit,
        since=_get_run_history_cutoff(),
    )
    return jsonify({"fills": _serialize_list(fills)})


@app.route("/api/fills/classified")
def api_fills_classified():
    """Get fills with classification metadata.

    Query params:
        type      — filter by classification: retail | arb_sweep_buy |
                    arb_sweep_sell | dexie_combined | unknown | arb (any arb type)
        side      — buy | sell
        limit     — max rows (default 50, max 200)
        offset    — pagination offset (default 0)
        since     — ISO timestamp lower bound
    """
    try:
        from database import get_connection
        from fill_classifier import FillType

        classification_filter = request.args.get("type") or None
        side_filter           = request.args.get("side") or None
        limit                 = min(request.args.get("limit", 50, type=int), 200)
        offset                = request.args.get("offset", 0, type=int)
        since                 = request.args.get("since") or _get_run_history_cutoff() or None

        conn = get_connection()
        cat_asset_id = cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""

        params = [cat_asset_id]
        where  = ["cat_asset_id = ?",
                  "COALESCE(verification_status, 'legacy') = 'verified'"]

        if classification_filter:
            if classification_filter == "arb":
                # Any arb-flavoured classification
                where.append(
                    "fill_classification IN ('arb_sweep_buy','arb_sweep_sell','dexie_combined')"
                )
            else:
                where.append("COALESCE(fill_classification,'unknown') = ?")
                params.append(classification_filter)

        if side_filter in ("buy", "sell"):
            where.append("side = ?")
            params.append(side_filter)

        if since:
            where.append("filled_at >= ?")
            params.append(since)

        where_clause = " AND ".join(where)

        # Total count for pagination metadata
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM fills WHERE {where_clause}", params
        ).fetchone()
        total = count_row[0] if count_row else 0

        rows = conn.execute(
            f"""SELECT fill_id, trade_id, side, price_xch, size_xch, size_cat,
                       tier, filled_at, fill_classification, taker_puzzle_hash,
                       spent_block_index, sweep_group_id, round_trip_id
                FROM fills
                WHERE {where_clause}
                ORDER BY filled_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

        fills = [dict(r) for r in rows]

        # Build summary counts
        summary: dict = {
            FillType.RETAIL:         0,
            FillType.ARB_SWEEP_BUY:  0,
            FillType.ARB_SWEEP_SELL: 0,
            FillType.DEXIE_COMBINED: 0,
            FillType.UNKNOWN:        0,
        }
        for f in fills:
            cls = f.get("fill_classification") or FillType.UNKNOWN
            if cls in summary:
                summary[cls] += 1
            else:
                summary[FillType.UNKNOWN] += 1

        # Attach sweep coordinator live state
        sweep_pending: dict = {}
        try:
            from sweep_coordinator import get_coordinator as _sc
            sweep_pending = _sc().get_pending_summary()
        except Exception:
            pass

        return jsonify({
            "fills":           _serialize_list(fills),
            "total":           total,
            "limit":           limit,
            "offset":          offset,
            "summary":         summary,
            "sweep_pending":   sweep_pending,
        })
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/fills/arb-wallets")
def api_fills_arb_wallets():
    """Auto-discover candidate arb puzzle hashes from fill history.

    Ranks puzzle hashes by how often they appear across distinct sweep groups.
    A hash that appears in many sweep groups is a strong arb-bot candidate.

    Response fields per candidate:
        puzzle_hash       — the raw hash (without 0x prefix)
        fill_count        — total fills where this hash is the taker
        sweep_group_count — distinct sweep_group_ids this hash appears in
        arb_confidence    — "high" / "medium" / "low"
        already_known     — true if already in KNOWN_ARB_PUZZLE_HASHES
        sides             — list of distinct sides swept ("buy", "sell")

    Usage: copy high-confidence puzzle_hash values into KNOWN_ARB_PUZZLE_HASHES
    in your .env file (comma-separated).
    """
    try:
        from database import get_connection

        conn = get_connection()
        cat_asset_id = cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""

        # Fetch all fills that have a taker_puzzle_hash recorded
        rows = conn.execute(
            """SELECT taker_puzzle_hash, fill_classification, sweep_group_id,
                      side, filled_at
               FROM fills
               WHERE taker_puzzle_hash IS NOT NULL
                 AND taker_puzzle_hash != ''
                 AND cat_asset_id = ?
                 AND COALESCE(verification_status, 'legacy') = 'verified'
               ORDER BY filled_at DESC""",
            (cat_asset_id,),
        ).fetchall()

        if not rows:
            return jsonify({
                "candidates": [],
                "total_fills_with_taker_hash": 0,
                "message": "No fills with taker_puzzle_hash recorded yet. "
                           "Hashes are captured as new fills occur.",
            })

        # Aggregate per puzzle hash
        from collections import defaultdict
        stats: dict = defaultdict(lambda: {
            "fill_count": 0,
            "sweep_groups": set(),
            "sides": set(),
            "classifications": set(),
            "latest_fill": None,
        })

        for row in rows:
            ph = str(row["taker_puzzle_hash"]).lower().lstrip("0x")
            s = stats[ph]
            s["fill_count"] += 1
            if row["sweep_group_id"]:
                s["sweep_groups"].add(row["sweep_group_id"])
            if row["side"]:
                s["sides"].add(row["side"])
            if row["fill_classification"]:
                s["classifications"].add(row["fill_classification"])
            if s["latest_fill"] is None:
                s["latest_fill"] = row["filled_at"]

        # Load known hashes for the "already_known" flag
        known_hashes = set(getattr(cfg, "KNOWN_ARB_PUZZLE_HASHES", []) or [])

        # Build ranked candidate list
        candidates = []
        for ph, s in stats.items():
            sweep_count = len(s["sweep_groups"])
            fill_count  = s["fill_count"]

            # Confidence heuristic:
            #   high   → appears in 3+ distinct sweep groups (definitely systematic)
            #   medium → appears in 2 sweep groups OR 3+ fills without sweep data
            #   low    → single fill, no sweep correlation
            if sweep_count >= 3:
                confidence = "high"
            elif sweep_count >= 2 or fill_count >= 3:
                confidence = "medium"
            else:
                confidence = "low"

            candidates.append({
                "puzzle_hash":       ph,
                "fill_count":        fill_count,
                "sweep_group_count": sweep_count,
                "arb_confidence":    confidence,
                "already_known":     ph in known_hashes,
                "sides":             sorted(s["sides"]),
                "classifications":   sorted(s["classifications"]),
                "latest_fill":       s["latest_fill"],
            })

        # Sort: already-known first (so you can see what's configured),
        # then by sweep_group_count desc, then fill_count desc
        candidates.sort(key=lambda c: (
            not c["already_known"],
            -c["sweep_group_count"],
            -c["fill_count"],
        ))

        # Summarise which hashes look like strong candidates not yet configured
        unconfigured_high = [
            c["puzzle_hash"] for c in candidates
            if c["arb_confidence"] == "high" and not c["already_known"]
        ]

        return jsonify({
            "candidates":               candidates,
            "total_fills_with_taker_hash": len(rows),
            "total_unique_hashes":      len(candidates),
            "unconfigured_high_confidence": unconfigured_high,
            "known_hashes_configured":  sorted(known_hashes),
            "tip": (
                "Add high-confidence puzzle_hash values to KNOWN_ARB_PUZZLE_HASHES "
                "in your .env (comma-separated) to enable ARB_SWEEP_BUY/SELL classification."
                if unconfigured_high else
                "No unconfigured high-confidence candidates found yet. "
                "More fills needed or all known hashes are already configured."
            ),
        })
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/market/fill-intel")
def api_market_fill_intel():
    """Fill intelligence summary — classification breakdown, spread correlation,
    and best trading windows.

    Query parameters:
        days   — look-back window in days (default 7, max 90)
        tz_offset_hours — client UTC offset for "hour of day" bucketing (default 0)

    Response:
        classification_breakdown — counts and % per fill type
        arb_rate_pct             — % of fills that are arb (vs retail)
        sweep_stats              — total sweeps, avg fills/sweep, max fills/sweep
        hourly_buckets           — list of {hour, fill_count, arb_count, retail_count}
                                   (UTC unless tz_offset_hours provided)
        spread_correlation       — placeholder (requires spread-at-fill data,
                                   not yet stored; returns null with explanation)
        data_window_days         — actual days of data returned
        fill_count               — total fills in window
    """
    try:
        from database import get_connection
        import math

        days = min(int(request.args.get("days", 7)), 90)
        tz_offset = float(request.args.get("tz_offset_hours", 0))

        conn = get_connection()
        cat_asset_id = cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""

        # ── Fetch fills within window ──────────────────────────────────────────
        rows = conn.execute(
            """SELECT fill_classification, sweep_group_id, side, filled_at
               FROM fills
               WHERE cat_asset_id = ?
                 AND COALESCE(verification_status, 'legacy') = 'verified'
                 AND filled_at >= datetime('now', ? || ' days')
               ORDER BY filled_at ASC""",
            (cat_asset_id, f"-{days}"),
        ).fetchall()

        total = len(rows)

        if total == 0:
            return jsonify({
                "classification_breakdown": {},
                "arb_rate_pct":    0.0,
                "sweep_stats":     {"total_sweeps": 0, "avg_fills_per_sweep": 0.0, "max_fills_per_sweep": 0},
                "hourly_buckets":  [],
                "spread_correlation": None,
                "spread_correlation_note": "No fills recorded in this window.",
                "data_window_days": days,
                "fill_count": 0,
            })

        # ── Classification breakdown ────────────────────────────────────────────
        from collections import defaultdict, Counter

        cls_counts: Counter = Counter()
        sweep_groups: dict = defaultdict(int)  # group_id → fill count
        hourly: dict = defaultdict(lambda: {"fill_count": 0, "arb_count": 0, "retail_count": 0})

        ARB_TYPES = {"arb_sweep_buy", "arb_sweep_sell", "dexie_combined"}

        for row in rows:
            cls = row["fill_classification"] or "unknown"
            cls_counts[cls] += 1

            if row["sweep_group_id"]:
                sweep_groups[row["sweep_group_id"]] += 1

            # Bucket by hour-of-day with optional tz shift
            if row["filled_at"]:
                try:
                    from datetime import datetime, timezone, timedelta
                    dt_utc = datetime.fromisoformat(str(row["filled_at"]).replace("Z", "+00:00"))
                    dt_local = dt_utc + timedelta(hours=tz_offset)
                    hour_key = dt_local.hour
                    bucket = hourly[hour_key]
                    bucket["fill_count"] += 1
                    if cls in ARB_TYPES:
                        bucket["arb_count"] += 1
                    elif cls == "retail":
                        bucket["retail_count"] += 1
                except Exception:
                    pass

        # ── Build breakdown with percentages ───────────────────────────────────
        breakdown = {}
        for cls_name, count in sorted(cls_counts.items(), key=lambda x: -x[1]):
            breakdown[cls_name] = {
                "count":   count,
                "pct":     round(count / total * 100, 1) if total else 0.0,
            }

        arb_count = sum(cls_counts.get(t, 0) for t in ARB_TYPES)
        arb_rate  = round(arb_count / total * 100, 1) if total else 0.0

        # ── Sweep stats ─────────────────────────────────────────────────────────
        sweep_fill_counts = list(sweep_groups.values())
        total_sweeps      = len(sweep_fill_counts)
        avg_fills         = round(sum(sweep_fill_counts) / total_sweeps, 2) if total_sweeps else 0.0
        max_fills         = max(sweep_fill_counts, default=0)

        # ── Hourly buckets (all 24 hours, zero-filled) ─────────────────────────
        hourly_buckets = []
        for h in range(24):
            b = hourly.get(h, {"fill_count": 0, "arb_count": 0, "retail_count": 0})
            hourly_buckets.append({
                "hour":         h,
                "fill_count":   b["fill_count"],
                "arb_count":    b["arb_count"],
                "retail_count": b["retail_count"],
                "arb_pct":      round(b["arb_count"] / b["fill_count"] * 100, 1)
                                if b["fill_count"] else 0.0,
            })

        return jsonify({
            "classification_breakdown": breakdown,
            "arb_rate_pct":    arb_rate,
            "sweep_stats": {
                "total_sweeps":        total_sweeps,
                "avg_fills_per_sweep": avg_fills,
                "max_fills_per_sweep": max_fills,
            },
            "hourly_buckets":  hourly_buckets,
            "spread_correlation": None,
            "spread_correlation_note": (
                "Spread-at-fill is not yet stored in the fills table. "
                "This field will be populated in a future schema migration."
            ),
            "data_window_days": days,
            "fill_count": total,
        })
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/offers/diagnostic")
def api_offers_diagnostic():
    """Compare the live wallet book to the DB book and summarize coin safety."""
    try:
        from database import get_connection
        conn = get_connection()
        asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")

        db_rows = conn.execute(
            """SELECT o.trade_id, o.side, o.tier, o.price_xch, o.size_xch, o.size_cat,
                      o.coin_id, o.dexie_id, o.dexie_posted, o.created_at,
                      c.designation, c.assigned_tier
               FROM offers o
               LEFT JOIN coins c ON c.coin_id = o.coin_id
               WHERE o.status='open' AND o.cat_asset_id=?
               ORDER BY o.side,
                        CASE o.tier
                            WHEN 'inner' THEN 1
                            WHEN 'mid' THEN 2
                            WHEN 'outer' THEN 3
                            WHEN 'extreme' THEN 4
                            WHEN 'sniper' THEN 5
                            ELSE 9
                        END,
                        CAST(o.price_xch AS REAL)""",
            (asset_id,)
        ).fetchall()
        db_open = [dict(row) for row in db_rows]
        db_ids = {row["trade_id"] for row in db_open if row.get("trade_id")}

        duplicate_rows = conn.execute(
            """SELECT coin_id, COUNT(*) as cnt,
                      GROUP_CONCAT(SUBSTR(trade_id, 1, 16)) as trade_samples
               FROM offers
               WHERE status='open' AND cat_asset_id=? AND coin_id IS NOT NULL AND coin_id != ''
               GROUP BY coin_id
               HAVING COUNT(*) > 1
               ORDER BY cnt DESC, coin_id""",
            (asset_id,)
        ).fetchall()
        duplicate_coin_ids = [dict(row) for row in duplicate_rows]

        reserve_rows = conn.execute(
            """SELECT o.trade_id, o.side, o.tier, o.coin_id, c.designation, c.assigned_tier
               FROM offers o
               JOIN coins c ON c.coin_id = o.coin_id
               WHERE o.status='open' AND o.cat_asset_id=? AND c.designation='reserve'
               ORDER BY o.side, o.tier, CAST(o.price_xch AS REAL)""",
            (asset_id,)
        ).fetchall()
        reserve_backed = [dict(row) for row in reserve_rows]

        summary_rows = conn.execute(
            """SELECT side, tier, COUNT(*) as offers, COUNT(DISTINCT coin_id) as unique_coins
               FROM offers
               WHERE status='open' AND cat_asset_id=?
               GROUP BY side, tier
               ORDER BY side, tier""",
            (asset_id,)
        ).fetchall()
        db_summary = [dict(row) for row in summary_rows]

        wallet_error = None
        wallet_open_buys = []
        wallet_open_sells = []
        try:
            if bot and getattr(bot, "offer_manager", None):
                wallet_open_buys, wallet_open_sells, _ = bot.offer_manager.sync_from_wallet()
            else:
                from wallet import get_all_offers, classify_offers_from_list
                wallet_offers = get_all_offers(include_completed=False, start=0, end=500)
                if wallet_offers is None:
                    raise RuntimeError("wallet_offer_query_failed")
                wallet_open_buys, wallet_open_sells, _ = classify_offers_from_list(wallet_offers, asset_id)
        except Exception as e:
            wallet_error = str(e)

        wallet_ids = {
            o.get("trade_id", "") for o in (wallet_open_buys + wallet_open_sells)
            if o.get("trade_id")
        }
        stale_in_db = sorted(db_ids - wallet_ids)
        wallet_only = sorted(wallet_ids - db_ids)

        likely_stale_dexie_rows = (
            wallet_error is None and
            len(duplicate_coin_ids) == 0 and
            len(reserve_backed) == 0 and
            len(stale_in_db) == 0 and
            len(wallet_only) == 0
        )

        if likely_stale_dexie_rows:
            diagnosis = ("Wallet and DB agree on the open book, and each live offer has a "
                         "unique non-reserve coin. Greyed Dexie rows are likely stale invalid "
                         "offers from earlier runs or Dexie cache lag.")
        else:
            diagnosis = ("Wallet/DB mismatch or coin-safety issue detected. Inspect the "
                         "differences below before assuming Dexie is just stale.")

        return jsonify(_serialize_dict({
            "success": True,
            "diagnosis": diagnosis,
            "likely_stale_dexie_rows": likely_stale_dexie_rows,
            "wallet_error": wallet_error,
            "wallet_open_buys": len(wallet_open_buys),
            "wallet_open_sells": len(wallet_open_sells),
            "db_open_buys": sum(1 for row in db_open if row.get("side") == "buy"),
            "db_open_sells": sum(1 for row in db_open if row.get("side") == "sell"),
            "duplicate_coin_ids": duplicate_coin_ids,
            "reserve_backed_offers": reserve_backed,
            "stale_in_db": stale_in_db,
            "wallet_only": wallet_only,
            "summary": db_summary,
            "open_offers": db_open,
        }))
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/fills/purge", methods=["POST"])
def api_purge_fills():
    """Purge all fill records to reset inventory position.

    Use when false fills have corrupted the position calculation
    (e.g. circuit breaker tripping on fake data from testing).
    Clears fills table + round_trips table + resets risk manager state.
    """
    slog("GUI_ACTION", ">>> BUTTON: Purge Fill Records")

    try:
        from database import get_connection, log_event
        conn = get_connection()

        # Count before purge
        fill_count = conn.execute("SELECT COUNT(*) as cnt FROM fills").fetchone()["cnt"]
        rt_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM round_trips"
        ).fetchone()["cnt"] if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='round_trips'"
        ).fetchone() else 0

        # Purge fills
        conn.execute("DELETE FROM fills")
        conn.commit()

        # Purge round_trips if table exists
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='round_trips'"
        ).fetchone():
            conn.execute("DELETE FROM round_trips")
            conn.commit()

        log_event("info", "fills_purged",
                  f"Purged {fill_count} fills and {rt_count} round-trips "
                  f"(inventory position reset to 0)")

        # Reset risk manager state if bot is running
        if bot and bot.risk_manager:
            bot.risk_manager.reset_position()

        return jsonify({
            "success": True,
            "fills_purged": fill_count,
            "round_trips_purged": rt_count,
            "message": f"Purged {fill_count} fills — position reset to 0"
        })
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/session/fresh-start", methods=["POST"])
def api_session_fresh_start():
    """Begin a brand new run without carrying forward old session state."""
    try:
        payload = _reset_fresh_run_session(
            clear_coins=False,
            clear_price_history=False,
            clear_inventory=False,
            cancel_open_offers=False,
            reason="session_fresh_start",
        )
        # Persist the choice so check-resume returns can_resume=False on the
        # next page load, even though the old live offers are still in Sage.
        # Cleared automatically when the bot starts a new run.
        _fresh_start_set()
        return jsonify({
            "success": True,
            "message": "Fresh run session started",
            **_serialize_dict(payload),
        })
    except Exception as e:
        log_event("warning", "session_fresh_start_failed",
                  f"Failed to reset fresh run session: {e}")
        return _api_error(e, request.path)


@app.route("/api/session/resume-chosen", methods=["POST"])
def api_session_resume_chosen():
    """User explicitly chose 'Load Previous Session' — clear the fresh-start flag."""
    _fresh_start_clear()
    return jsonify({"success": True})


@app.route("/api/pnl")
def api_pnl():
    """Get PnL summary with realised, unrealised, and round-trip details."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        stats = get_stats(cfg.CAT_ASSET_ID, since=_get_run_history_cutoff())
        inventory = bot.risk_manager.get_inventory_state()
        sniper_stats = bot.sniper.get_stats() if getattr(bot, "sniper", None) else {}

        pnl_data = {
            "realised_pnl_xch": stats.get("realised_pnl_xch", "0"),
            "total_fills": stats.get("total_fills", 0),
            "buy_fills": stats.get("buy_fills", 0),
            "sell_fills": stats.get("sell_fills", 0),
            "round_trips": stats.get("round_trips", 0),
            "win_rate": stats.get("win_rate", 0),
            "fill_rate_per_hour": stats.get("fill_rate_per_hour", 0),
            "pending_verification_count": _get_session_pending_verification_count(),
            "avg_spread_capture": stats.get("avg_spread_capture", "0"),
            "net_position_cat": inventory.get("net_position_cat", "0"),
            "circuit_breaker_active": inventory.get("circuit_breaker_active", False),
            "sniper": sniper_stats,
            # Extended statistics
            "unmatched_buy_fills": stats.get("unmatched_buy_fills", 0),
            "unmatched_sell_fills": stats.get("unmatched_sell_fills", 0),
            "volume_xch": stats.get("volume_xch", "0"),
            "volume_cat": stats.get("volume_cat", "0"),
            "avg_fill_size_xch": stats.get("avg_fill_size_xch", "0"),
            "avg_round_trip_secs": stats.get("avg_round_trip_secs", 0),
            "avg_pnl_per_trip_xch": stats.get("avg_pnl_per_trip_xch", "0"),
        }

        return jsonify(_serialize_dict(pnl_data))
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Dashboard Command Centre (aggregated endpoint for the top panel)
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
def api_dashboard():
    """Aggregated endpoint for the Command Centre panel.

    Returns all settings, market health, wallet balances, coin counts,
    and performance stats in one call. Designed to be called once on
    page load, then kept live via SSE dashboard_update events.
    """
    try:
        from database import get_stats, get_coin_summary, get_open_offers

        # --- Active Settings ---
        settings = {
            "trading": {
                "dry_run": cfg.DRY_RUN,
                "enable_buy": cfg.ENABLE_BUY,
                "enable_sell": cfg.ENABLE_SELL,
                "loop_seconds": cfg.LOOP_SECONDS,
                "max_active_buy": cfg.MAX_ACTIVE_BUY_OFFERS,
                "max_active_sell": cfg.MAX_ACTIVE_SELL_OFFERS,
                "offer_expiry_mins": cfg.OFFER_EXPIRY_SECS // 60,
                "auto_requote": cfg.AUTO_REQUOTE,
                "requote_bps": str(cfg.REQUOTE_BPS),
            },
            "spreads": {
                "mode": "dynamic" if cfg.DYNAMIC_SPREAD_ENABLED else "fixed",
                "spread_bps": str(cfg.SPREAD_BPS),
                "base_spread_bps": str(cfg.BASE_SPREAD_BPS),
                "min_spread_bps": str(cfg.MIN_SPREAD_BPS),
                "max_spread_bps": str(cfg.MAX_SPREAD_BPS),
                "min_edge_bps": str(cfg.MIN_EDGE_BPS),
                "dynamic_enabled": cfg.DYNAMIC_SPREAD_ENABLED,
            },
            "inventory": {
                "enabled": cfg.INVENTORY_ENABLED,
                "skew_intensity": str(cfg.SKEW_INTENSITY),
                "max_position_xch": str(cfg.MAX_POSITION_XCH),
            },
            "tiers": {
                "enabled": cfg.TIER_ENABLED,
                "inner_xch": str(cfg.INNER_SIZE_XCH),
                "mid_xch": str(cfg.MID_SIZE_XCH),
                "outer_xch": str(cfg.OUTER_SIZE_XCH),
                "extreme_xch": str(cfg.EXTREME_SIZE_XCH),
            },
            "safety": {
                "xch_reserve": str(cfg.XCH_RESERVE),
                "cat_reserve": str(cfg.CAT_RESERVE),
                "hard_min_price": str(cfg.HARD_MIN_PRICE_XCH),
                "hard_max_price": str(cfg.HARD_MAX_PRICE_XCH),
                "dynamic_limit_pct": str(cfg.DYNAMIC_LIMIT_PCT),
            },
            "features": {
                "sniper": getattr(cfg, "SNIPER_ENABLED", True),
                "competitor_aware": cfg.COMPETITOR_AWARE_ENABLED,
                "splash": cfg.SPLASH_ENABLED,
                "auto_requote": cfg.AUTO_REQUOTE,
                "coin_prep": cfg.ENABLE_COIN_PREP,
                "runtime_coin_health": cfg.ENABLE_RUNTIME_COIN_HEALTH,
                "dynamic_spread": cfg.DYNAMIC_SPREAD_ENABLED,
                "inventory_mgmt": cfg.INVENTORY_ENABLED,
                "tiered_orders": cfg.TIER_ENABLED,
            },
        }

        settings["safety"]["circuit_breaker_active"] = False
        settings["safety"]["circuit_breaker_reason"] = ""
        if bot and getattr(bot, "risk_manager", None):
            try:
                inventory_state = bot.risk_manager.get_inventory_state()
                settings["safety"]["circuit_breaker_active"] = bool(
                    inventory_state.get("circuit_breaker_active", False)
                )
                settings["safety"]["circuit_breaker_reason"] = str(
                    inventory_state.get("circuit_breaker_reason", "") or ""
                )
            except Exception:
                pass

        # --- Market Health ---
        market_health = {"status": "green", "message": "Waiting for first cycle", "conditions": [], "metrics": {}}
        if bot and bot.risk_manager:
            try:
                _lc = getattr(bot, "_loop_count", 0) or 0
                market_health = bot.risk_manager.get_market_health(loop_count=_lc)
            except Exception as e:
                market_health["message"] = f"Health check error: {e}"
        if bot:
            try:
                metrics = market_health.setdefault("metrics", {})
                live_state = getattr(bot, "_bot_state", {}) or {}
                live_arb_gap = live_state.get("arb_gap_bps")
                if live_arb_gap not in (None, ""):
                    metrics["arb_gap_bps"] = str(live_arb_gap)

                if getattr(bot, "market_intel", None):
                    summary = bot.market_intel.get_market_summary() or {}
                    refreshes = int(summary.get("orderbook_refreshes", 0) or 0)
                    metrics["market_intel_refreshes"] = refreshes
                    metrics["market_intel_state"] = "ready" if refreshes > 0 else "searching"
                    metrics["market_intel_age_secs"] = (
                        summary.get("orderbook_age_secs") if refreshes > 0 else None
                    )
                    comp_buys = int(summary.get("num_competitor_buys", 0) or 0)
                    comp_sells = int(summary.get("num_competitor_sells", 0) or 0)
                    comp_total = comp_buys + comp_sells
                    metrics["competitor_count"] = comp_total
                    if comp_buys > 0 and comp_sells > 0:
                        metrics["competitor_sides"] = "both"
                    elif comp_buys > 0:
                        metrics["competitor_sides"] = "buy only"
                    elif comp_sells > 0:
                        metrics["competitor_sides"] = "sell only"
                    else:
                        metrics["competitor_sides"] = "none"
                    if summary.get("competitor_spread_bps") is not None:
                        metrics["market_spread_bps"] = str(summary.get("competitor_spread_bps", "0"))
                    if summary.get("overall_spread_bps") is not None:
                        metrics["overall_spread_bps"] = str(summary.get("overall_spread_bps", "0"))
            except Exception:
                pass

        active_asset_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        active_ticker_id = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
        active_decimals = int(_active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3) or 3)
        executable_mid = Decimal("0")
        try:
            if bot and getattr(bot, "price_engine", None):
                _lp = bot.price_engine.get_last_price()
                executable_mid = Decimal(str(_lp)) if _lp else Decimal("0")
        except Exception:
            executable_mid = Decimal("0")

        spacescan_context = _get_spacescan_market_context(
            asset_id=active_asset_id,
            ticker_id=active_ticker_id,
            decimals=active_decimals,
            executable_mid_price=executable_mid,
        )
        try:
            metrics = market_health.setdefault("metrics", {})
            metrics["spacescan_enabled"] = spacescan_context.get("enabled", False)
            metrics["spacescan_has_data"] = spacescan_context.get("has_data", False)
            metrics["spacescan_holder_count"] = spacescan_context.get("holder_count", 0)
            metrics["spacescan_activity_level"] = spacescan_context.get("activity_level", "unknown")
            metrics["spacescan_risk_level"] = spacescan_context.get("risk_level", "unknown")
            metrics["spacescan_price_gap_bps"] = str(spacescan_context.get("price_gap_bps", 0))
        except Exception:
            pass

        # --- Runtime metrics: trading pace, probe state, loop timing ---
        try:
            metrics = market_health.setdefault("metrics", {})
            metrics["loop_seconds"] = int(getattr(cfg, "LOOP_SECONDS", 60))
            if bot:
                probe = getattr(bot, "_probe_state", {}) or {}
                probe_active = bool(probe.get("active", False))
                confirmed = probe.get("confirmed_price")
                probe_confirmed = bool(
                    confirmed not in (None, 0)
                    and str(confirmed) not in ("0", "0.0", "None")
                )
                metrics["probe_active"] = probe_active
                metrics["probe_confirmed"] = probe_confirmed
                if probe_confirmed:
                    metrics["probe_status"] = "confirmed"
                elif probe_active:
                    metrics["probe_status"] = "searching"
                else:
                    metrics["probe_status"] = "idle"
                if getattr(bot, "coin_manager", None):
                    try:
                        metrics["trading_pace"] = bot.coin_manager.get_trading_pace()
                    except Exception:
                        metrics["trading_pace"] = "unknown"
                if bot.risk_manager:
                    try:
                        metrics["circuit_breaker_blocked_side"] = (
                            bot.risk_manager.get_circuit_breaker_blocked_side()
                        )
                    except Exception:
                        metrics["circuit_breaker_blocked_side"] = ""
        except Exception:
            pass

        # --- Wallet & Coins ---
        wallet = {"xch_spendable": 0, "xch_total": 0, "cat_spendable": 0, "cat_total": 0}
        coins = {
            "xch_free": 0, "xch_locked": 0, "xch_total": 0,
            "cat_free": 0, "cat_locked": 0, "cat_total": 0,
            "tier_counts": {"enabled": False, "xch": {}, "cat": {}},
        }

        # Fetch wallet balances directly from RPC (works whether bot is running or not)
        try:
            from wallet import get_wallet_balance, WALLET_ID_XCH
            xr = get_wallet_balance(WALLET_ID_XCH)
            if xr and xr.get("success"):
                wb = xr.get("wallet_balance") or {}
                wallet["xch_total"] = str(Decimal(str(wb.get("confirmed_wallet_balance", 0))) / Decimal("1000000000000"))
                wallet["xch_spendable"] = str(Decimal(str(wb.get("spendable_balance", 0))) / Decimal("1000000000000"))
            cat_wid = _active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
            cat_dec = _active_cat.get("decimals") or getattr(cfg, 'CAT_DECIMALS', 3)
            cr = get_wallet_balance(cat_wid)
            if cr and cr.get("success"):
                wb = cr.get("wallet_balance") or {}
                _cat_divisor = Decimal(10) ** int(cat_dec)
                wallet["cat_total"] = str(Decimal(str(wb.get("confirmed_wallet_balance", 0))) / _cat_divisor)
                wallet["cat_spendable"] = str(Decimal(str(wb.get("spendable_balance", 0))) / _cat_divisor)
        except Exception as e:
            print(f"[DASHBOARD] Wallet balance fetch error: {e}", flush=True)

        try:
            db_coin_summary = get_coin_summary()
            coins["xch_free"] = db_coin_summary.get("xch_free_count", 0)
            coins["xch_locked"] = db_coin_summary.get("xch_locked_count", 0)
            coins["xch_total"] = db_coin_summary.get("xch_total", 0)
            coins["cat_free"] = db_coin_summary.get("cat_free_count", 0)
            coins["cat_locked"] = db_coin_summary.get("cat_locked_count", 0)
            coins["cat_total"] = db_coin_summary.get("cat_total", 0)
            if getattr(cfg, "TIER_ENABLED", False):
                tier_counts = get_live_tier_group_counts()
                tier_counts["enabled"] = True
                coins["tier_counts"] = tier_counts
        except Exception:
            pass

        # Sage RPC fallback: if bot isn't running (or coin_manager returned zeros),
        # query Sage directly so the dashboard always shows real coin counts.
        if coins["xch_free"] == 0 and coins["xch_total"] == 0:
            try:
                from wallet import rpc as wallet_rpc
                cat_asset_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")

                def _dash_count_coins(asset_id, filter_mode):
                    """Query Sage get_coins and return (count, total_mojos)."""
                    result = wallet_rpc("get_coins", {
                        "asset_id": asset_id,
                        "offset": 0, "limit": 500,
                        "filter_mode": filter_mode,
                    }, timeout=10)
                    if not result:
                        return 0, 0
                    coin_list = (result.get("coins") or result.get("records")
                                 or result.get("data") or [])
                    total_mojos = sum(int(c.get("amount", "0")) for c in coin_list)
                    return len(coin_list), total_mojos

                xch_free, _ = _dash_count_coins(None, "selectable")
                cat_free, _ = _dash_count_coins(cat_asset_id, "selectable") if cat_asset_id else (0, 0)

                coins["xch_free"] = xch_free
                coins["xch_total"] = xch_free
                coins["cat_free"] = cat_free
                coins["cat_total"] = cat_free
            except Exception as e:
                print(f"[DASHBOARD] Sage coin count fallback error: {e}", flush=True)

        # --- Performance Stats ---
        performance = {}
        try:
            stats = get_stats(cfg.CAT_ASSET_ID, since=_get_run_history_cutoff())
            performance = _serialize_dict(stats)
            performance["pending_verification_count"] = _get_session_pending_verification_count()
        except Exception:
            pass

        # Add uptime from bot loop
        if bot:
            try:
                active_cat_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
                live_open_buys = len(get_open_offers(side="buy", cat_asset_id=active_cat_id))
                live_open_sells = len(get_open_offers(side="sell", cat_asset_id=active_cat_id))
                performance["open_buys"] = live_open_buys
                performance["open_sells"] = live_open_sells
                performance["open_offers"] = live_open_buys + live_open_sells
                performance["loop_count"] = bot._loop_count
                performance["uptime_secs"] = int(time.time() - bot._start_time) if getattr(bot, '_start_time', 0) else 0
            except Exception:
                pass

        sniper_stats = {}
        if bot and getattr(bot, "sniper", None):
            try:
                sniper_stats = _serialize_dict(bot.sniper.get_stats())
            except Exception:
                sniper_stats = {}

        # --- External Links ---
        asset_id = (_active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "") or "").strip()
        ticker_id = (_active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "") or "").strip().upper()
        if ticker_id and "_" not in ticker_id:
            ticker_id = f"{ticker_id}_XCH"
        dexie_orderbook = ""
        if ticker_id:
            parts = [p for p in ticker_id.split("_") if p]
            if len(parts) >= 2:
                dexie_orderbook = f"https://dexie.space/offers/{quote(parts[0])}/{quote(parts[1])}"
        elif asset_id:
            dexie_orderbook = f"https://dexie.space/offers/{quote(asset_id)}/XCH"

        links = {
            "dexie_orderbook": dexie_orderbook,
            "tibetswap_pool": f"https://v2.tibetswap.io/pair/{quote(getattr(cfg, 'TIBET_PAIR_ID', '') or '')}" if getattr(cfg, 'TIBET_PAIR_ID', '') else (f"https://v2.tibetswap.io/?asset_id={quote(asset_id)}" if asset_id else "https://v2.tibetswap.io"),
            "spacescan_token": f"https://www.spacescan.io/cat2/{quote(asset_id)}" if asset_id else "",
        }

        return jsonify(_serialize_dict({
            "settings": settings,
            "market_health": market_health,
            "wallet": wallet,
            "coins": coins,
            "performance": performance,
            "sniper": sniper_stats,
            "spacescan_context": spacescan_context,
            "links": links,
            "cat_name": cfg.CAT_NAME if hasattr(cfg, 'CAT_NAME') else "CAT",
            "current_cat": _active_cat,
            "wallet_type": "sage",
        }))
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/stats")
def api_stats():
    """Get trading statistics."""
    try:
        stats = get_stats(cfg.CAT_ASSET_ID, since=_get_run_history_cutoff())
        return jsonify(stats)
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Inventory & Risk Routes
# ---------------------------------------------------------------------------

@app.route("/api/inventory")
def api_inventory():
    """Get current inventory state."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.risk_manager.get_inventory_state())


@app.route("/api/risk/spreads")
def api_risk_spreads():
    """Get current adjusted spreads for each side."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    buy_spread = bot.risk_manager.get_adjusted_spread("buy")
    sell_spread = bot.risk_manager.get_adjusted_spread("sell")

    return jsonify({
        "buy_spread_bps": str(buy_spread * Decimal("10000")),
        "sell_spread_bps": str(sell_spread * Decimal("10000")),
        "buy_spread_pct": str(buy_spread * Decimal("100")),
        "sell_spread_pct": str(sell_spread * Decimal("100")),
        "dynamic_enabled": cfg.DYNAMIC_SPREAD_ENABLED,
        "inventory_enabled": cfg.INVENTORY_ENABLED,
    })


# ---------------------------------------------------------------------------
# Coin Routes
# ---------------------------------------------------------------------------

@app.route("/api/coins")
def api_coins():
    """Get coin status.

    F62 (2026-04-09): refresh inventory on-demand so the dashboard
    reflects the current wallet state even when the bot isn't running.
    Without this, the in-memory inventory dict stays at whatever the
    last loop tick captured — typically all-zero on a fresh session,
    or stale post-coin-prep until the user starts the bot. The refresh
    is guarded against running during coin prep / topup so it doesn't
    race with the worker.
    """
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # On-demand refresh when the bot isn't running (so the dashboard
    # shows accurate numbers after coin prep finishes). When the bot IS
    # running, its loop refreshes every tick, so skip the extra RPC.
    try:
        if not bot.is_running():
            bot.coin_manager.update_coin_counts()
    except Exception as _refresh_err:
        # Don't fail the endpoint if the refresh glitches; the cached
        # status is still better than a 500.
        log_event("debug", "api_coins_refresh_failed",
                  f"On-demand coin refresh failed: {_refresh_err}")

    return jsonify(bot.coin_manager.get_status())


@app.route("/api/coins/topup", methods=["POST"])
def api_coin_topup():
    """Manually trigger coin topup."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Block if bot is live — topup splits coins and races with offer creation
    if bot.is_running():
        return jsonify({
            "error": "Stop the bot before manual top-up. "
                     "The bot handles top-up automatically while running.",
            "requires_stop": True,
        }), 409

    open_buys = bot.offer_manager.get_open_offer_count("buy")
    open_sells = bot.offer_manager.get_open_offer_count("sell")

    started = bot.coin_manager.start_topup(open_buys, open_sells)
    return jsonify({"status": "started" if started else "already_running"})


@app.route("/api/coins/prep", methods=["POST"])
def api_coin_prep():
    """Manually trigger full coin prep."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Block if bot is live — coin prep splits/combines and races with offer creation
    if bot.is_running():
        return jsonify({
            "error": "Stop the bot before manual coin prep. "
                     "The bot handles coin prep automatically while running.",
            "requires_stop": True,
        }), 409

    started = bot.coin_manager.start_coin_prep()
    return jsonify({"status": "started" if started else "already_running"})


# ---------------------------------------------------------------------------
# Dexie Routes
# ---------------------------------------------------------------------------

@app.route("/api/dexie/stats")
def api_dexie_stats():
    """Get Dexie posting statistics."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.dexie_manager.get_stats())


@app.route("/api/dexie/repost", methods=["POST"])
def api_dexie_repost():
    """Repost all active offers to Dexie."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    open_buys, open_sells, _ = bot.offer_manager.sync_from_wallet()
    all_offers = open_buys + open_sells
    bot.dexie_manager.repost_active_offers(all_offers)
    return jsonify({"status": "queued", "count": len(all_offers)})


# ---------------------------------------------------------------------------
# Market Intelligence Routes (NEW — ecosystem upgrades)
# ---------------------------------------------------------------------------

def _fetch_dbx_pair_status(asset_id: str, ticker_id: str) -> dict:
    """Fetch Dexie pair-level rewards status with a short TTL cache."""
    cache_key = ((asset_id or "").lower(), (ticker_id or "").upper())
    now = time.time()
    cached = _dbx_pair_cache.get(cache_key)
    if cached and (now - cached.get("ts", 0)) < 300:
        return dict(cached.get("data") or {})

    result = {
        "pair_incentivized": None,
        "pair_source": "",
    }
    if not asset_id and not ticker_id:
        return result

    try:
        import requests as _req

        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space").rstrip("/")
        tid = ticker_id or ""
        if tid and "_" not in tid:
            tid = f"{tid}_XCH"

        if tid:
            resp = _req.get(
                f"{dexie_base}/v2/prices/tickers",
                params={"ticker_id": tid},
                timeout=8,
            )
            if resp.status_code == 200:
                tickers = resp.json().get("tickers", [])
                if tickers:
                    ticker = tickers[0]
                    if "incentives" in ticker:
                        result["pair_incentivized"] = bool(ticker.get("incentives"))
                        result["pair_source"] = "dexie_ticker"
    except Exception:
        pass

    _dbx_pair_cache[cache_key] = {"ts": now, "data": dict(result)}
    return result


@app.route("/api/market/intel")
def api_market_intel():
    """Get full market intelligence summary.

    Includes competitor analysis, orderbook depth, and DBX eligibility.
    """
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        bot.market_intel.refresh_orderbook(force=True)
    except Exception:
        pass

    summary = bot.market_intel.get_market_summary()
    asset_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
    ticker_id = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
    try:
        from decimal import Decimal
        buy_spread = bot.risk_manager.get_adjusted_spread("buy")
        sell_spread = bot.risk_manager.get_adjusted_spread("sell")
        avg_spread_bps = ((buy_spread + sell_spread) / 2) * Decimal("10000")
        mid_price = bot.price_engine.get_last_price() or Decimal("0")
        live_dbx = bot.market_intel.check_dbx_eligibility(avg_spread_bps, mid_price)
    except Exception:
        live_dbx = {}
        mid_price = Decimal("0")

    try:
        local_book = _get_live_local_offer_edges(asset_id)
        our_best_bid = local_book.get("our_best_bid", Decimal("0"))
        our_best_ask = local_book.get("our_best_ask", Decimal("0"))
        summary["our_best_bid"] = str(our_best_bid)
        summary["our_best_ask"] = str(our_best_ask)
        summary["our_open_buys"] = int(local_book.get("our_open_buys", 0) or 0)
        summary["our_open_sells"] = int(local_book.get("our_open_sells", 0) or 0)
        summary["live_book_source"] = local_book.get("source", "")

        ext_best_bid = Decimal(str(summary.get("overall_best_bid") or summary.get("best_bid") or 0))
        ext_best_ask = Decimal(str(summary.get("overall_best_ask") or summary.get("best_ask") or 0))
        overall_best_bid = max(ext_best_bid, our_best_bid)
        bid_candidates = [v for v in (ext_best_ask, our_best_ask) if v > 0]
        overall_best_ask = min(bid_candidates) if bid_candidates else Decimal("0")
        summary["overall_best_bid"] = str(overall_best_bid)
        summary["overall_best_ask"] = str(overall_best_ask)
        if overall_best_bid > 0 and overall_best_ask > 0 and overall_best_bid < overall_best_ask:
            overall_mid = (overall_best_bid + overall_best_ask) / 2
            summary["overall_spread_bps"] = str(
                ((overall_best_ask - overall_best_bid) / overall_mid * Decimal("10000"))
                if overall_mid > 0 else Decimal("0")
            )
        elif overall_best_bid > 0 and overall_best_ask > 0:
            summary["overall_spread_bps"] = "0"
    except Exception:
        pass

    dbx = dict(summary.get("dbx") or {})
    if dbx or live_dbx:
        if live_dbx:
            dbx["eligible"] = bool(live_dbx.get("eligible_offers", 0))
            dbx["eligible_offers"] = live_dbx.get("eligible_offers", 0)
            dbx["max_spread_bps"] = str(
                live_dbx.get("max_eligible_spread", dbx.get("max_spread_bps", "0"))
            )
            dbx["estimated_rate"] = str(
                live_dbx.get("estimated_dbx_rate", dbx.get("estimated_rate", "0"))
            )
        dbx["spread_eligible"] = bool(dbx.get("eligible"))
        dbx.update(_fetch_dbx_pair_status(asset_id, ticker_id))
        summary["dbx"] = dbx

    try:
        splash = bot.splash_manager.get_stats()
        splash["health"] = bot.splash_manager.check_health()
        summary["splash"] = splash
    except Exception:
        pass

    try:
        summary["splash_node"] = bot.splash_node.get_status()
    except Exception:
        pass

    try:
        summary["splash_receive"] = bot.get_splash_receive_stats()
    except Exception:
        pass

    try:
        summary["spacescan"] = _get_spacescan_market_context(
            asset_id=asset_id,
            ticker_id=ticker_id,
            decimals=int(_active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3) or 3),
            executable_mid_price=float(mid_price or 0),
        )
    except Exception:
        pass

    return jsonify(_serialize_dict(summary))


@app.route("/api/market/orderbook")
def api_market_orderbook():
    """Force refresh and return orderbook data."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    data = bot.market_intel.refresh_orderbook(force=True)
    return jsonify(_serialize_dict(data))


@app.route("/api/market/slippage")
def api_market_slippage():
    """Get TibetSwap slippage estimate for a given trade size.

    Query params: amount (XCH), side (buy/sell)
    """
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    amount = request.args.get("amount", "0.01")
    side = request.args.get("side", "buy")

    try:
        from decimal import Decimal
        quote = bot.price_engine.get_tibet_quote(
            amount_xch=Decimal(amount),
            side=side
        )
        if quote:
            return jsonify(quote)
        return jsonify({"error": "Could not get quote"}), 404
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/market/dbx")
def api_market_dbx():
    """Get DBX rewards eligibility status."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        from decimal import Decimal
        buy_spread = bot.risk_manager.get_adjusted_spread("buy")
        sell_spread = bot.risk_manager.get_adjusted_spread("sell")
        avg_spread_bps = ((buy_spread + sell_spread) / 2) * Decimal("10000")
        mid_price = bot.price_engine.get_last_price() or Decimal("0")

        dbx = bot.market_intel.check_dbx_eligibility(avg_spread_bps, mid_price)
        asset_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        ticker_id = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
        dbx["spread_eligible"] = bool(dbx.get("eligible_offers", 0))
        dbx.update(_fetch_dbx_pair_status(asset_id, ticker_id))
        return jsonify(_serialize_dict(dbx))
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Alert Management Routes
# ---------------------------------------------------------------------------

@app.route("/api/alerts")
def api_alerts():
    """Get all active (non-dismissed) alerts."""
    return jsonify({"alerts": alerts.get_active()})


@app.route("/api/alerts/dismiss", methods=["POST"])
def api_dismiss_alert():
    """Dismiss an alert by ID."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    alert_id = data.get("id", "")
    if alert_id:
        alerts.dismiss(alert_id)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "No alert ID provided"}), 400


# ---------------------------------------------------------------------------
# V3: Splash Network Routes
# ---------------------------------------------------------------------------

@app.route("/api/splash/stats")
def api_splash_stats():
    """Get Splash P2P broadcasting statistics."""
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


@app.route("/api/splash/receive", methods=["GET", "POST"])
def api_splash_receive():
    """Get or update inbound Splash listening state."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    if request.method == "GET":
        return jsonify(_serialize_dict(bot.get_splash_receive_stats()))

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
    events.emit("splash_incoming", payload)
    events.emit("config_changed", {
        "key": "SPLASH_RECEIVE_ENABLED",
        "value": enabled,
        "source": "splash_receive_toggle",
    })

    return jsonify({
        "success": True,
        "enabled": enabled,
        "node_action": node_action,
        "stats": _serialize_dict(payload),
    })


@app.route("/api/splash/node")
def api_splash_node():
    """Get Splash P2P node status (binary process health)."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.splash_node.get_status())


@app.route("/api/splash/node/start", methods=["POST"])
def api_splash_node_start():
    """Start the Splash P2P node process (used by startup gate)."""
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
        return _api_error(e, request.path)


@app.route("/api/splash/node/output")
def api_splash_node_output():
    """Get recent output lines from the Splash node process."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    lines = int(request.args.get("lines", 20))
    return jsonify({"output": bot.splash_node.get_recent_output(lines)})


@app.route("/api/splash/setup/check")
def api_splash_setup_check():
    """Check if Splash binary is installed and get platform info."""
    try:
        from splash_setup import check_installed
        return jsonify(check_installed())
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/splash/setup/download", methods=["POST"])
def api_splash_setup_download():
    """Start downloading the Splash binary (non-blocking)."""
    try:
        from splash_setup import start_background_download
        result = start_background_download()
        return jsonify(result)
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/splash/setup/progress")
def api_splash_setup_progress():
    """Get download progress (poll this during download)."""
    try:
        from splash_setup import get_download_status
        return jsonify(get_download_status())
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/splash/setup/release")
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
        return _api_error(e, request.path)


@app.route("/api/splash/incoming", methods=["POST"])
def api_splash_incoming():
    """Webhook for receiving offers from the Splash P2P network.

    Splash binary can be configured to POST incoming offers here.
    We store them in the database for potential future sniper use.

    Loopback-only and token-exempt, but rate-limited to prevent a
    pathological flood from amplifying into unbounded DB writes.
    """
    if not getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
        return jsonify({"error": "Splash receive disabled"}), 403

    # Dedicated rate limiter (defined in this module) — 200/sec is generous
    # for a real local Splash binary but stops abuse.
    if _splash_incoming_rate_limited():
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
        import hashlib
        fp = hashlib.sha256(offer_bech32.strip().encode("utf-8")).hexdigest()
        source_ip = request.remote_addr

        from database import record_splash_incoming
        was_new = record_splash_incoming(offer_bech32, fp, source_ip=source_ip)

        if was_new:
            log_event("debug", "splash_received",
                      f"Received new offer from Splash (fp: {fp[:16]}...)")
            if bot:
                try:
                    events.emit("splash_incoming", bot.get_splash_receive_stats())
                except Exception:
                    pass

        return jsonify({"ok": True, "new": was_new})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/splash/incoming/list")
def api_splash_incoming_list():
    """List recent offers received from Splash network."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        status_filter = request.args.get("status")
        limit = int(request.args.get("limit", 50))
        from database import get_splash_incoming_offers
        offers = get_splash_incoming_offers(status=status_filter, limit=limit)
        return jsonify({"offers": offers, "count": len(offers)})
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# V3: Coinset API Routes
# ---------------------------------------------------------------------------

@app.route("/api/coinset/stats")
def api_coinset_stats():
    """Get Coinset API query statistics."""
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    stats = bot.coinset_client.get_stats()
    health = bot.coinset_client.check_health()
    stats["health"] = health
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Price Routes
# ---------------------------------------------------------------------------

@app.route("/api/price")
def api_price():
    """Get current price from all sources."""
    # Use active CAT selection if available
    asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    decimals = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
    ticker = _active_cat.get("ticker_id") or (cfg.CAT_TICKER_ID if hasattr(cfg, "CAT_TICKER_ID") else "")

    if bot:
        price_data = bot.price_engine.get_price(asset_id, decimals, ticker)
        result = _serialize_dict(price_data)
        # GUI expects "mid" key — price_engine returns "mid_price"
        if "mid" not in result and "mid_price" in result:
            result["mid"] = result["mid_price"]
        # Ensure "success" key exists for GUI fallback check
        if "mid" not in result:
            result["mid"] = 0
        result["success"] = float(result.get("mid", 0) or 0) > 0
        return jsonify(result)

    # Bot not running — do a lightweight price lookup (TibetSwap + Dexie fallback)
    return _fetch_price_standalone(asset_id, decimals)


@app.route("/api/market/summary")
def api_market_summary():
    """Lightweight market overview for the dashboard.

    Returns best bid/ask from Dexie orderbook, 24h volume, TibetSwap pool
    depth, and price sources — all in one call. Works whether the bot is
    running or not.
    """
    import requests as _req

    asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    ticker_id = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
    decimals = int(_active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3))

    result = {
        "best_bid": 0, "best_ask": 0,
        "dexie_price": 0, "tibet_price": 0, "mid_price": 0,
        "volume_24h": 0, "pool_xch": 0, "pool_cat": 0,
        "dexie_depth_xch": 0,
        "arb_gap_bps": 0, "has_data": False,
    }

    if not asset_id:
        return jsonify(result)

    dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")

    # --- Dexie ticker (24h volume + last price + native bid/ask) ---
    try:
        if ticker_id:
            tid = ticker_id if "_" in ticker_id else f"{ticker_id}_XCH"
            resp = _req.get(f"{dexie_base}/v2/prices/tickers",
                            params={"ticker_id": tid}, timeout=8)
            if resp.status_code == 200:
                tickers = resp.json().get("tickers", [])
                if tickers:
                    t = tickers[0]
                    result["dexie_price"] = float(t.get("current_avg_price", 0) or 0)
                    # target_volume = XCH volume for TICKER_XCH pairs
                    result["volume_24h"] = float(t.get("target_volume", 0) or 0)
                    # v2 ticker provides native bid/ask (already in XCH/CAT)
                    _ticker_bid = float(t.get("bid", 0) or 0)
                    _ticker_ask = float(t.get("ask", 0) or 0)
                    if _ticker_bid > 0:
                        result["best_bid"] = _ticker_bid
                    if _ticker_ask > 0:
                        result["best_ask"] = _ticker_ask
    except Exception:
        pass

    # --- Dexie orderbook best bid & ask (compute from actual amounts) ---
    # The Dexie v1 'price' field is unreliable (inverted for buy offers).
    # Instead, parse offered/requested arrays and compute XCH/CAT directly,
    # the same proven approach used in market_intel.py.
    def _extract_xch_per_cat(offer, cat_id):
        """Extract XCH/CAT price from a Dexie v1 offer's amounts."""
        xch_amt = 0.0
        cat_amt = 0.0
        for asset in offer.get("offered", []) + offer.get("requested", []):
            code = str(asset.get("code", "")).upper()
            aid = str(asset.get("id", "")).lower().replace("0x", "")
            amt = float(asset.get("amount", 0) or 0)
            if code == "XCH" or aid == "" or aid == "xch":
                xch_amt = amt
            elif aid == cat_id.lower().replace("0x", ""):
                cat_amt = amt
        if xch_amt > 0 and cat_amt > 0:
            return xch_amt / cat_amt
        return 0.0

    try:
        # Best ask — lowest sell (someone selling CAT for XCH)
        # Sort by price_asc to get cheapest CAT first
        resp = _req.get(f"{dexie_base}/v1/offers",
                        params={"offered": asset_id, "requested": "xch",
                                "status": 0, "page_size": 3, "sort": "price_asc"},
                        timeout=8)
        if resp.status_code == 200:
            for offer in resp.json().get("offers", []):
                p = _extract_xch_per_cat(offer, asset_id)
                if p > 0:
                    result["best_ask"] = p
                    break

        # Best bid — highest buy (someone buying CAT with XCH)
        # For buy offers, Dexie price = CAT/XCH (inverted).
        # price_asc = lowest CAT/XCH = highest XCH/CAT = best bid
        resp = _req.get(f"{dexie_base}/v1/offers",
                        params={"offered": "xch", "requested": asset_id,
                                "status": 0, "page_size": 3, "sort": "price_asc"},
                        timeout=8)
        if resp.status_code == 200:
            for offer in resp.json().get("offers", []):
                p = _extract_xch_per_cat(offer, asset_id)
                if p > 0:
                    result["best_bid"] = p
                    break
    except Exception:
        pass

    # --- Dexie orderbook total depth (XCH on both sides) ---
    try:
        dexie_total_xch = 0.0
        # Sell side depth (CAT offered → XCH requested)
        resp = _req.get(f"{dexie_base}/v1/offers",
                        params={"offered": asset_id, "requested": "xch",
                                "status": 0, "page_size": 50},
                        timeout=8)
        if resp.status_code == 200:
            for offer in resp.json().get("offers", []):
                for asset in offer.get("requested", []):
                    if str(asset.get("code", "")).upper() == "XCH":
                        dexie_total_xch += float(asset.get("amount", 0) or 0)

        # Buy side depth (XCH offered → CAT requested)
        resp = _req.get(f"{dexie_base}/v1/offers",
                        params={"offered": "xch", "requested": asset_id,
                                "status": 0, "page_size": 50},
                        timeout=8)
        if resp.status_code == 200:
            for offer in resp.json().get("offers", []):
                for asset in offer.get("offered", []):
                    if str(asset.get("code", "")).upper() == "XCH":
                        dexie_total_xch += float(asset.get("amount", 0) or 0)

        result["dexie_depth_xch"] = round(dexie_total_xch, 2)
    except Exception:
        pass

    # --- TibetSwap pool ---
    try:
        resp = _req.get("https://api.v2.tibetswap.io/pairs",
                        params={"skip": 0, "limit": 200}, timeout=8)
        if resp.status_code == 200:
            norm_id = asset_id.lower().strip().replace("0x", "")
            for p in resp.json():
                p_id = str(p.get("asset_id", "")).lower().strip().replace("0x", "")
                if p_id == norm_id:
                    xr = float(p.get("xch_reserve", 0)) / 1e12
                    tr = float(p.get("token_reserve", 0)) / (10 ** decimals)
                    if tr > 0:
                        result["tibet_price"] = xr / tr
                        result["pool_xch"] = round(xr, 2)
                        result["pool_cat"] = round(tr, 0)
                    break
    except Exception:
        pass

    # --- Compute mid price and arb gap ---
    # Use live orderbook mid (best bid + best ask / 2) for arb gap, not the
    # 24h-average dexie_price from the ticker endpoint which is stale.
    bb = result["best_bid"]
    ba = result["best_ask"]
    dexie_live_mid = (bb + ba) / 2 if bb > 0 and ba > 0 else result["dexie_price"]
    dp = result["dexie_price"]
    tp = result["tibet_price"]
    if dexie_live_mid > 0 and tp > 0:
        result["mid_price"] = (dexie_live_mid + tp) / 2
        result["arb_gap_bps"] = round(abs(dexie_live_mid - tp) / dexie_live_mid * 10000, 1)
    elif dp > 0 and tp > 0:
        result["mid_price"] = (dp + tp) / 2
        result["arb_gap_bps"] = round(abs(dp - tp) / dp * 10000, 1)
    elif dp > 0:
        result["mid_price"] = dp
    elif tp > 0:
        result["mid_price"] = tp

    result["has_data"] = result["mid_price"] > 0
    return jsonify(result)


@app.route("/api/price/tibet")
def api_tibet_price():
    """Get TibetSwap pool info."""
    asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    decimals = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)

    if bot:
        pool = bot.price_engine.get_tibet_pool_info(asset_id)
        return jsonify(_serialize_dict(pool))

    # Bot not running — fetch directly
    return _fetch_price_standalone(asset_id, decimals)


@app.route("/api/amm/price")
def api_amm_price():
    """Get live TibetSwap AMM state from the AMMMonitor background poller.

    Returns the latest cached AMM reserve data, price, drift from last quoted
    mid, and monitor health stats. Used by the GUI to display a live AMM price
    row alongside the market price card.

    Response fields:
      available       bool   — True if at least one successful poll
      amm_price       str    — XCH per token (Decimal string)
      xch_reserve     str    — XCH in pool
      token_reserve   str    — Tokens in pool
      fetched_at      float  — Unix timestamp of last successful poll
      drift_bps       str    — Drift from last quoted mid in bps (or null)
      pair_id         str    — TibetSwap pair ID
      total_polls     int    — Lifetime poll count
      failed_polls    int    — Lifetime failure count
      last_success_ago_secs float  — Seconds since last good poll
    """
    if not bot or not hasattr(bot, "amm_monitor"):
        return jsonify({"available": False, "error": "AMM monitor not running"})

    try:
        state = bot.amm_monitor.get_amm_state()
        stats = bot.amm_monitor.get_stats()
        drift = bot.amm_monitor.get_drift_bps()

        result = {
            "available": bool(state and state.get("available")),
            "amm_price": str(state["amm_price"]) if state and state.get("amm_price") else None,
            "xch_reserve": str(state["xch_reserve"]) if state and state.get("xch_reserve") else None,
            "token_reserve": str(state["token_reserve"]) if state and state.get("token_reserve") else None,
            "fetched_at": state.get("fetched_at", 0) if state else 0,
            "drift_bps": str(drift.quantize(__import__("decimal").Decimal("0.1"))) if drift is not None else None,
            "pair_id": stats.get("pair_id", ""),
            "total_polls": stats.get("total_polls", 0),
            "failed_polls": stats.get("failed_polls", 0),
            "consecutive_failures": stats.get("consecutive_failures", 0),
            "last_success_ago_secs": stats.get("last_success_ago_secs"),
            "poll_interval_secs": getattr(cfg, "AMM_POLL_INTERVAL_SECS", 30),
            "drift_threshold_bps": str(getattr(cfg, "AMM_DRIFT_REQUOTE_BPS", "40")),
            "buffer_enabled": getattr(cfg, "ENABLE_AMM_BUFFER", False),
            "buffer_bps": str(getattr(cfg, "AMM_BUFFER_BPS", "30")),
            # Tier 3: arb pressure + dynamic buffer
            "arb_pressure":        stats.get("arb_pressure"),
            "arb_pressure_label":  stats.get("arb_pressure_label"),
            "dynamic_buffer":      stats.get("dynamic_buffer", {}),
            "sweep_protection":    {
                side: round(max(0, expiry - __import__("time").time()), 1)
                for side, expiry in getattr(bot, "_sweep_protection", {}).items()
                if expiry > __import__("time").time()
            },
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})


@app.route("/api/debug/coinprep")
def api_debug_coinprep():
    """Debug: shows coin prep worker status and any error output.
    Open http://localhost:5000/api/debug/coinprep in your browser.
    """
    result = {"_coin_prep_state": _coin_prep_state}

    # Read worker status file
    base_dir = os.path.dirname(os.path.abspath(__file__))
    status_file = os.path.join(base_dir, "coin_prep_status.json")
    if os.path.exists(status_file):
        try:
            with open(status_file, "r") as f:
                result["worker_status_file"] = json.load(f)
        except Exception as e:
            result["worker_status_file_error"] = str(e)
    else:
        result["worker_status_file"] = "NOT FOUND"

    # Read worker output log (stdout + stderr merged)
    log_file = os.path.join(base_dir, "coin_prep_output.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_content = f.read()
            result["worker_output_log"] = log_content[-2000:]  # Last 2000 chars
        except Exception as e:
            result["worker_output_log_error"] = str(e)
    else:
        result["worker_output_log"] = "NOT FOUND"

    # Check subprocess status via coin_manager
    if bot:
        try:
            result["coin_manager_status"] = bot.coin_manager.check_coin_prep_status()
        except Exception as e:
            result["coin_manager_error"] = str(e)

    # Check recent log events for coin prep errors
    try:
        from database import get_recent_events
        events = get_recent_events(limit=20)
        prep_events = [e for e in events if "coin_prep" in str(e.get("event_type", ""))]
        result["recent_coin_prep_events"] = prep_events[:10]
    except Exception:
        pass

    return jsonify(result)


@app.route("/api/debug/pricing")
def api_debug_pricing():
    """Debug: shows exactly what pricing the GUI sees.
    Open http://localhost:5000/api/debug/pricing in your browser.
    """
    import requests as _req
    result = {"_active_cat": {k: str(v)[:50] if v else None for k, v in _active_cat.items()}}
    result["bot_exists"] = bot is not None

    asset_id = _active_cat.get("asset_id") or ""
    cat_dec = _active_cat.get("decimals") or 3
    ticker_id = _active_cat.get("ticker_id") or ""
    result["asset_id"] = asset_id
    result["ticker_id"] = ticker_id

    # Test 1: What does /api/status return for pricing?
    try:
        resp = _req.get("http://127.0.0.1:5000/api/status", timeout=15)
        status_data = resp.json()
        result["status_pricing"] = status_data.get("pricing", "MISSING")
        result["status_current_cat"] = status_data.get("current_cat", "MISSING")
    except Exception as e:
        result["status_error"] = str(e)

    # Test 2: What does /api/price return?
    try:
        resp = _req.get("http://127.0.0.1:5000/api/price", timeout=15)
        result["price_response"] = resp.json()
    except Exception as e:
        result["price_error"] = str(e)

    # Test 3: Direct TibetSwap test
    try:
        resp = _req.get("https://api.v2.tibetswap.io/pairs",
                        params={"skip": 0, "limit": 200}, timeout=10)
        pairs = resp.json() if resp.status_code == 200 else []
        result["tibet_total_pairs"] = len(pairs)
        if asset_id:
            norm = asset_id.lower().strip().replace("0x", "")
            for p in pairs:
                pid = str(p.get("asset_id", "")).lower().strip().replace("0x", "")
                if pid == norm:
                    xr = float(p.get("xch_reserve", 0)) / 1e12
                    tr = float(p.get("token_reserve", 0)) / (10 ** int(cat_dec))
                    result["tibet_match"] = {
                        "name": p.get("short_name", "?"),
                        "price": xr / tr if tr > 0 else 0,
                        "xch_reserve": xr,
                        "token_reserve": tr,
                    }
                    break
            else:
                result["tibet_match"] = "NOT FOUND"
    except Exception as e:
        result["tibet_error"] = str(e)

    return jsonify(result)


@app.route("/api/debug/tibet-test")
def api_debug_tibet_test():
    """Debug endpoint: test TibetSwap API connectivity directly.

    Open http://localhost:5000/api/debug/tibet-test in your browser to check.
    """
    result = {"test": "TibetSwap API connectivity"}
    asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    result["asset_id_used"] = asset_id
    result["_active_cat"] = {k: str(v)[:30] if v else None for k, v in _active_cat.items()}

    try:
        import requests as _req
        resp = _req.get("https://api.v2.tibetswap.io/pairs",
                        params={"skip": 0, "limit": 200}, timeout=10)
        result["tibet_status"] = resp.status_code
        if resp.status_code == 200:
            pairs = resp.json()
            result["total_pairs"] = len(pairs)
            # Show first 3 pair names for sanity
            result["sample_pairs"] = [
                {"name": p.get("short_name", p.get("name", "?")), "asset_id": str(p.get("asset_id", ""))[:20] + "..."}
                for p in pairs[:3]
            ]
            # Try to find our CAT
            if asset_id:
                norm = asset_id.lower().strip().replace("0x", "")
                for p in pairs:
                    pid = str(p.get("asset_id", "")).lower().strip().replace("0x", "")
                    if pid == norm:
                        xr = float(p.get("xch_reserve", 0)) / 1e12
                        dec = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
                        tr = float(p.get("token_reserve", 0)) / (10 ** int(dec))
                        result["matched_pair"] = {
                            "name": p.get("short_name", p.get("name")),
                            "xch_reserve": xr,
                            "token_reserve": tr,
                            "price": xr / tr if tr > 0 else 0,
                        }
                        break
                else:
                    result["matched_pair"] = None
                    result["error"] = f"No pair found matching asset_id {norm[:20]}..."
        else:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        result["error"] = str(e)
        result["tibet_status"] = "FAILED"

    return jsonify(result)


@app.route("/api/debug/sage-single-offer-test", methods=["POST"])
def api_debug_sage_single_offer_test():
    """Create one selected-coin XCH offer and one CAT offer, inspect, cancel."""
    try:
        from wallet import (
            get_wallet_type,
            create_offer,
            cancel_offer,
            get_owned_coins_detailed,
        )
        if get_wallet_type() != "sage":
            return jsonify({"ok": False, "error": "sage_only_debug_route"}), 400

        import sqlite3
        from database import DB_PATH

        def _pick_smallest_spare(wallet_type: str):
            conn = sqlite3.connect(DB_PATH, timeout=10)
            try:
                row = conn.execute(
                    """
                    select coin_id, amount_mojos, assigned_tier
                    from coins
                    where status='free'
                      and designation='tier_spare'
                      and wallet_type=?
                    order by amount_mojos asc, coin_id asc
                    limit 1
                    """,
                    (wallet_type,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "coin_id": row[0],
                    "amount_mojos": int(row[1]),
                    "assigned_tier": row[2],
                }
            finally:
                conn.close()

        def _extract_trade_id(result: dict) -> str:
            if not isinstance(result, dict):
                return ""
            trade_id = result.get("trade_id") or result.get("offer_id") or ""
            if not trade_id:
                tr = result.get("trade_record") or {}
                if isinstance(tr, dict):
                    trade_id = tr.get("trade_id") or tr.get("offer_id") or ""
            if not trade_id:
                offer_obj = result.get("offer") or {}
                if isinstance(offer_obj, dict):
                    trade_id = offer_obj.get("id") or offer_obj.get("offer_id") or ""
            return str(trade_id or "")

        def _run_case(name: str, wallet_id: int, offer_dict: dict, selected_coin_id: str):
            result = {
                "name": name,
                "selected_coin_id": selected_coin_id,
                "offer_dict": offer_dict,
            }
            create_res = create_offer(
                offer_dict,
                validate_only=False,
                max_time=int(time.time()) + 300,
                coin_ids=[selected_coin_id],
            )
            result["create_result"] = create_res

            trade_id = _extract_trade_id(create_res or {})
            result["trade_id"] = trade_id
            if not trade_id:
                return result

            time.sleep(2)
            owned = get_owned_coins_detailed(wallet_id) or {}
            locked_inputs = []
            for coin_id, info in owned.items():
                offer_id = str(info.get("offer_id") or "").lower()
                if offer_id == trade_id.lower():
                    locked_inputs.append({
                        "coin_id": coin_id,
                        "amount": int(info.get("amount") or 0),
                    })
            result["locked_inputs"] = locked_inputs

            cancel_res = cancel_offer(trade_id, secure=False, timeout=30)
            result["cancel_result"] = cancel_res
            return result

        xch_coin = _pick_smallest_spare("xch")
        cat_coin = _pick_smallest_spare("cat")
        if not xch_coin or not cat_coin:
            return jsonify({
                "ok": False,
                "error": "no_free_spare_coin",
                "xch_coin": xch_coin,
                "cat_coin": cat_coin,
            }), 409

        xch_case = _run_case(
            name="xch_selected_manual",
            wallet_id=int(cfg.WALLET_ID_XCH),
            offer_dict={
                str(int(cfg.WALLET_ID_XCH)): -1_000_000_000,
                str(int(cfg.CAT_WALLET_ID)): 8_000,
            },
            selected_coin_id=xch_coin["coin_id"],
        )

        time.sleep(2)

        cat_case = _run_case(
            name="cat_selected_manual",
            wallet_id=int(cfg.CAT_WALLET_ID),
            offer_dict={
                str(int(cfg.CAT_WALLET_ID)): -8_000,
                str(int(cfg.WALLET_ID_XCH)): 1_000_000_000,
            },
            selected_coin_id=cat_coin["coin_id"],
        )

        payload = {
            "ok": True,
            "xch_coin": xch_coin,
            "cat_coin": cat_coin,
            "results": [xch_case, cat_case],
        }
        log_event("info", "sage_single_offer_test", json.dumps(payload, default=str)[:1500])
        return jsonify(payload)
    except Exception as e:
        return _api_error(e, request.path)


def _fetch_price_standalone(asset_id, decimals):
    """Lightweight price fetch when bot isn't running.

    Tries TibetSwap first (AMM pool price), then falls back to Dexie (order book price).
    Many CATs are only on Dexie and not on TibetSwap, so both sources matter.
    """
    print(f"[PRICE_STANDALONE] Called with asset_id={asset_id!r}, decimals={decimals}")
    if not asset_id:
        print("[PRICE_STANDALONE] No asset_id — returning error")
        return jsonify({"success": False, "error": "No CAT selected"})

    import requests as _req
    price = None
    source = None

    # --- Try TibetSwap first ---
    try:
        resp = _req.get("https://api.v2.tibetswap.io/pairs",
                        params={"skip": 0, "limit": 200}, timeout=8)
        pairs = resp.json() if resp.status_code == 200 else []
        print(f"[PRICE_STANDALONE] TibetSwap API: status={resp.status_code}, pairs={len(pairs)}")

        normalized = asset_id.lower().strip()
        if normalized.startswith("0x"):
            normalized = normalized[2:]

        for p in pairs:
            p_id = str(p.get("asset_id", "")).lower().strip()
            if p_id.startswith("0x"):
                p_id = p_id[2:]
            if p_id == normalized:
                xch_reserve = Decimal(str(p.get("xch_reserve", 0)))
                token_reserve = Decimal(str(p.get("token_reserve", 0)))
                if token_reserve > 0 and xch_reserve > 0:
                    xch_amount = xch_reserve / Decimal("1000000000000")
                    token_amount = token_reserve / (Decimal(10) ** int(decimals))
                    price = xch_amount / token_amount
                    source = "tibetswap"
                    print(f"[PRICE_STANDALONE] TibetSwap match! price={price}")
                break

        if not price:
            print(f"[PRICE_STANDALONE] CAT not found on TibetSwap, trying Dexie...")
    except Exception as e:
        print(f"[PRICE_STANDALONE] TibetSwap failed ({e}), trying Dexie...")

    # --- Fallback to Dexie ---
    if not price:
        try:
            ticker_id = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "") or ""
            # Dexie ticker format is "{CAT}_XCH" e.g. "SBX_XCH" (V1 confirmed)
            if ticker_id and "_" not in ticker_id:
                ticker_id = f"{ticker_id}_XCH"
            dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")

            # Method 1: Try ticker endpoint if we have a ticker_id
            if ticker_id:
                resp = _req.get(f"{dexie_base}/v2/prices/tickers",
                                params={"ticker_id": ticker_id}, timeout=8)
                if resp.status_code == 200:
                    tickers = resp.json().get("tickers", [])
                    if tickers:
                        tk = tickers[0]
                        # Prefer bid/ask midpoint (real market) over last_price (can be outlier)
                        tk_bid = Decimal(str(tk.get("bid") or tk.get("best_bid") or 0))
                        tk_ask = Decimal(str(tk.get("ask") or tk.get("best_ask") or 0))
                        if tk_bid > 0 and tk_ask > 0:
                            price = (tk_bid + tk_ask) / 2
                            source = "dexie_bid_ask"
                            print(f"[PRICE_STANDALONE] Dexie bid/ask mid={price:.10f} "
                                  f"(bid={tk_bid}, ask={tk_ask})")
                        else:
                            for field in ["current_avg_price", "last_price", "price"]:
                                val = tk.get(field)
                                if val and str(val) != "0":
                                    price = Decimal(str(val))
                                    source = "dexie_ticker"
                                    print(f"[PRICE_STANDALONE] Dexie ticker match! {field}={price}")
                                    break

            # Method 2: Try Dexie offers endpoint for best bid/ask
            if not price:
                resp = _req.get(f"{dexie_base}/v1/offers",
                                params={"offered": asset_id, "requested": "xch",
                                         "status": 0, "page_size": 1, "sort": "price_asc"},
                                timeout=8)
                if resp.status_code == 200:
                    offers = resp.json().get("offers", [])
                    if offers:
                        best_ask = Decimal(str(offers[0].get("price", 0)))
                        if best_ask > 0:
                            price = best_ask
                            source = "dexie_orderbook"
                            print(f"[PRICE_STANDALONE] Dexie orderbook price={price}")
        except Exception as e:
            print(f"[PRICE_STANDALONE] Dexie fetch also failed: {e}")

    if not price:
        return jsonify({"success": False, "error": "No price available from TibetSwap or Dexie"})

    return jsonify(_decimal_safe({
        "success": True,
        "mid": price,
        "tibet_price": price if source == "tibetswap" else None,
        "dexie_price": price if source and source.startswith("dexie") else None,
        "tibet_enabled": source == "tibetswap",
        "source": source,
        "liquidity": {},
    }))


# ---------------------------------------------------------------------------
# Smart Defaults — Live Market Data Analysis
# ---------------------------------------------------------------------------

def _fetch_dexie_orderbook_standalone(asset_id: str) -> dict:
    """Fetch Dexie orderbook and calculate competitor spread/depth.

    Standalone version (no bot/market_intel needed) for Smart Defaults.
    """
    import requests as _req
    result = {
        "best_bid": 0, "best_ask": 0, "competitor_spread_bps": 0,
        "buy_depth_xch": 0, "sell_depth_xch": 0,
        "num_buy_offers": 0, "num_sell_offers": 0,
        "has_data": False,
    }
    if not asset_id:
        return result

    dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
    our_tag = getattr(cfg, "DEXIE_BOT_TAG", "")

    try:
        # Sell side: CAT offered for XCH (ascending = cheapest first = best ask)
        # NOTE: Dexie API uses "offered_asset_id" / "requested_asset_id" params
        sell_resp = _req.get(f"{dexie_base}/v1/offers", params={
            "offered_asset_id": asset_id,
            "status": 0, "page_size": 20, "sort": "price_asc"
        }, timeout=8)
        sell_offers = sell_resp.json().get("offers", []) if sell_resp.status_code == 200 else []

        # Buy side: XCH offered for CAT (descending = highest first = best bid)
        buy_resp = _req.get(f"{dexie_base}/v1/offers", params={
            "requested_asset_id": asset_id,
            "status": 0, "page_size": 20, "sort": "price_desc"
        }, timeout=8)
        buy_offers = buy_resp.json().get("offers", []) if buy_resp.status_code == 200 else []

        # Filter out our own offers (by tag)
        def is_ours(offer):
            tags = offer.get("tags", [])
            return our_tag and our_tag in tags

        # Parse sell side (extract prices)
        for offer in sell_offers:
            if is_ours(offer):
                continue
            offered = offer.get("offered", [])
            requested = offer.get("requested", [])
            cat_amount = 0
            xch_amount = 0
            for item in offered:
                if str(item.get("id", "")).lower().replace("0x", "") == asset_id.lower().replace("0x", ""):
                    cat_amount = float(item.get("amount", 0))
            for item in requested:
                code = str(item.get("code", "")).upper()
                if code == "XCH" or str(item.get("id", "")).lower() == "xch":
                    xch_amount = float(item.get("amount", 0))
            if cat_amount > 0 and xch_amount > 0:
                price = xch_amount / cat_amount
                if result["best_ask"] == 0 or price < result["best_ask"]:
                    result["best_ask"] = price
                result["sell_depth_xch"] += xch_amount
                result["num_sell_offers"] += 1

        # Parse buy side
        for offer in buy_offers:
            if is_ours(offer):
                continue
            offered = offer.get("offered", [])
            requested = offer.get("requested", [])
            xch_amount = 0
            cat_amount = 0
            for item in offered:
                code = str(item.get("code", "")).upper()
                if code == "XCH" or str(item.get("id", "")).lower() == "xch":
                    xch_amount = float(item.get("amount", 0))
            for item in requested:
                if str(item.get("id", "")).lower().replace("0x", "") == asset_id.lower().replace("0x", ""):
                    cat_amount = float(item.get("amount", 0))
            if cat_amount > 0 and xch_amount > 0:
                price = xch_amount / cat_amount
                if price > result["best_bid"]:
                    result["best_bid"] = price
                result["buy_depth_xch"] += xch_amount
                result["num_buy_offers"] += 1

        # Sanity check: if bid > ask (inverted), the data is garbage — reset
        if result["best_bid"] > 0 and result["best_ask"] > 0:
            if result["best_bid"] >= result["best_ask"]:
                print(f"[SMART_DEFAULTS] Orderbook inverted (bid {result['best_bid']:.10f} "
                      f">= ask {result['best_ask']:.10f}) — discarding")
                result["best_bid"] = 0
                result["best_ask"] = 0

        # Calculate competitor spread
        if result["best_bid"] > 0 and result["best_ask"] > 0:
            mid = (result["best_bid"] + result["best_ask"]) / 2
            if mid > 0:
                result["competitor_spread_bps"] = (result["best_ask"] - result["best_bid"]) / mid * 10000

        result["has_data"] = result["num_buy_offers"] > 0 or result["num_sell_offers"] > 0
        print(f"[SMART_DEFAULTS] Orderbook: bid={result['best_bid']:.8f}, "
              f"ask={result['best_ask']:.8f}, spread={_bps_to_pct(result['competitor_spread_bps'])}, "
              f"buys={result['num_buy_offers']}, sells={result['num_sell_offers']}")
        return result
    except Exception as e:
        print(f"[SMART_DEFAULTS] Orderbook fetch failed: {e}")
        return result


@app.route("/api/smart-defaults")
def api_smart_defaults():
    """Calculate ALL smart default settings from live market data.

    Gathers wallet balances, prices from both exchanges, pool depth,
    competitor orderbook, and volatility history — then calculates
    every setting from real data. Works even when bot is stopped.
    """
    try:
        xch_res = request.args.get("xch_reserve", 0)
        cat_res = request.args.get("cat_reserve", 0)
        risk_profile = request.args.get("risk_profile", "balanced")
        return _calculate_smart_defaults(xch_reserve=xch_res, cat_reserve=cat_res, risk_profile=risk_profile)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[SMART_DEFAULTS] ERROR: {e}\n{tb}")
        log_event("error", "smart_defaults", f"Smart Settings failed: {e}")
        return jsonify({"error": "Smart Settings calculation failed", "code": "SERVER_ERROR"}), 500


def _calculate_smart_defaults(xch_reserve=0.0, cat_reserve=0.0, risk_profile="balanced"):
    """Smart Defaults v2 — data-driven settings from 30 days of market data.

    Replaces v1's snapshot-only approach with deep analysis:
    - 30 days of Dexie trade history (fill rate, volume, trends)
    - 30d/90d ticker ranges (real volatility, not just 24h)
    - TibetSwap pool depth + quote-based slippage
    - Spacescan token health (holders, activity, supply)
    - Bot's own performance history (if available)

    Falls back gracefully to v1-style calculations if any source fails.
    """
    # ── RISK PROFILE ──────────────────────────────────────────────────────────
    # Multipliers applied to Smart Settings outputs. Balanced = no change.
    # Risk profiles only change HOW the bot reacts to market conditions.
    # Total capital deployed and number of offers are IDENTICAL across all
    # three profiles — only spread behaviour, requote speed, position
    # tolerance, inventory rebalancing, and safety buffers differ.
    #
    # conservative: wider spreads, slower requoting, tighter inventory
    #               tolerance, gentler rebalancing, bigger safety buffers
    # balanced:     baseline reactivity
    # aggressive:   tighter spreads, faster requoting, looser inventory
    #               tolerance, harder rebalancing, same safety buffers
    _RISK_PROFILES = {
        "conservative": {
            # ── Capital / sizing (IDENTITY — all 1.0) ──
            "capital_mult":       1.0,
            "max_offers_mult":    1.0,
            "inner_tier_mult":    1.0,
            "tier_size_mult":     1.0,
            # ── Spread behaviour ──
            "spread_bps_mult":    1.10,  # wider base spread (earn more per fill)
            "spread_step_mult":   1.20,  # wider requote threshold (let offers ride)
            # ── Risk / inventory ──
            "position_mult":      0.75,  # tighter position CB (trip sooner)
            "skew_mult":          0.75,  # gentler inventory rebalancing
            # ── Safety buffers ──
            "spare_adj":         +1,     # +1 spare per active tier
            "coin_prep_adj":     +0.5,   # more coin-prep buffer (floor enforced at 2.0 max)
        },
        "balanced": {
            # Baseline — everything at 1.0
            "capital_mult":       1.0,
            "max_offers_mult":    1.0,
            "inner_tier_mult":    1.0,
            "tier_size_mult":     1.0,
            "spread_bps_mult":    1.0,
            "spread_step_mult":   1.0,
            "position_mult":      1.0,
            "skew_mult":          1.0,
            "spare_adj":          0,
            "coin_prep_adj":      0.0,
        },
        "aggressive": {
            # ── Capital / sizing (IDENTITY — all 1.0) ──
            "capital_mult":       1.0,
            "max_offers_mult":    1.0,
            "inner_tier_mult":    1.0,
            "tier_size_mult":     1.0,
            # ── Spread behaviour ──
            "spread_bps_mult":    0.92,  # tighter base spread (more competitive)
            "spread_step_mult":   0.85,  # tighter requote threshold (chase price)
            # ── Risk / inventory ──
            "position_mult":      1.25,  # looser position CB (allow larger swings)
            "skew_mult":          1.25,  # harder inventory rebalancing
            # ── Safety buffers ──
            "spare_adj":          0,     # baseline spares
            "coin_prep_adj":      0.0,   # baseline coin-prep buffer
        },
    }
    _rp = _RISK_PROFILES.get(str(risk_profile).lower().strip(), _RISK_PROFILES["balanced"])
    _risk_profile_name = str(risk_profile).lower().strip()
    if _risk_profile_name not in _RISK_PROFILES:
        _risk_profile_name = "balanced"
    print(f"[SMART_DEFAULTS v2] Risk profile: {_risk_profile_name}")
    # ─────────────────────────────────────────────────────────────────────────

    from decimal import Decimal
    from market_data_collector import collect_all_market_data, analyze_market_data

    asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    decimals = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
    ticker_id = _active_cat.get("ticker_id") or (cfg.CAT_TICKER_ID if hasattr(cfg, "CAT_TICKER_ID") else "")
    cat_wid = _active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", 2)

    if not asset_id:
        return jsonify({"error": "No trading pair selected"})

    print(f"\n[SMART_DEFAULTS v2] === Gathering 30 days of market data ===")
    log_event("info", "smart_defaults", "Smart Settings: gathering 30 days of market data")
    messages = []

    # ---- 1. Wallet balances (same as v1 — always needed) ----
    # F62 (2026-04-09): use UNCONFIRMED (projected post-pending) balance.
    #
    # History:
    #  • V1 used `spendable_balance` which excluded coins locked in active
    #    offers. Running Smart Settings while trading sized the ladder for
    #    half the wallet.
    #  • F62 first moved to `confirmed_wallet_balance` (full post-pending
    #    balance) so the ladder sizes against the whole wallet.
    #  • But confirmed_wallet_balance DROPS temporarily when a self-tx is
    #    pending (e.g. during a coin-prep combine). If the user reruns
    #    Smart Settings while a combine is in-flight, confirmed has spent
    #    the old coins but the new output isn't confirmed yet → a 15-20%
    #    temporary dip → Smart Settings computes smaller tier sizes →
    #    persisted to .env → next coin prep run creates a crippled ladder.
    #
    # Using `unconfirmed_wallet_balance` (confirmed + pending_change) is
    # stable during self-transactions: the combine tx pays 80 XCH back to
    # the wallet as pending change, so unconfirmed stays ~81 XCH throughout
    # even when confirmed drops to 66. That's the right number for sizing.
    #
    # The variable name stays `xch_spendable` / `cat_spendable` to avoid
    # a sprawling rename, but it now holds the projected post-pending total.
    xch_spendable = 0
    cat_spendable = 0
    has_wallet = False
    _pending_tx_count = 0
    try:
        from wallet import get_wallet_balance, WALLET_ID_XCH
        xr = get_wallet_balance(WALLET_ID_XCH)
        if xr and xr.get("success"):
            wb = xr.get("wallet_balance") or {}
            # Prefer unconfirmed (confirmed + pending); fall back to
            # confirmed, then spendable, for wallet backends that don't
            # report all three fields.
            _raw_total = _safe_float(wb.get("unconfirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = _safe_float(wb.get("confirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = _safe_float(wb.get("spendable_balance", 0))
            xch_spendable = _raw_total / 1e12
            # F62 (2026-04-09): track pending tx count so we can WARN
            # the user (or the GUI) if Smart Settings is running during
            # in-flight wallet operations. Both confirmed and unconfirmed
            # are subject to transient inconsistency mid-tx.
            _pending_tx_count += int(wb.get("pending_coin_removal_count", 0) or 0)
            has_wallet = True
        cr = get_wallet_balance(cat_wid)
        if cr and cr.get("success"):
            wb = cr.get("wallet_balance") or {}
            _raw_total = _safe_float(wb.get("unconfirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = _safe_float(wb.get("confirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = _safe_float(wb.get("spendable_balance", 0))
            cat_spendable = _raw_total / (10 ** decimals)
            _pending_tx_count += int(wb.get("pending_coin_removal_count", 0) or 0)
        if has_wallet:
            messages.append(f"Wallet: {xch_spendable:.2f} XCH (total)")
            print(f"[SMART_DEFAULTS v2] Wallet (total): {xch_spendable:.4f} XCH, {cat_spendable:.0f} CAT")
            # F62 (2026-04-09): warn loudly if there are in-flight wallet
            # transactions. Even using `unconfirmed_wallet_balance`, the
            # balance can still be briefly inconsistent between submission
            # and inclusion. Running Smart Settings during a pending
            # combine/split is the classic cause of "inflated" tier sizes
            # that then fail at coin prep time.
            if _pending_tx_count > 0:
                warn_msg = (
                    f"WARNING: {_pending_tx_count} pending wallet tx(s) in flight. "
                    f"Smart Settings results may be off — wait ~30s for the wallet "
                    f"to settle and re-run, or coin prep may fail."
                )
                messages.append(warn_msg)
                print(f"[SMART_DEFAULTS v2] {warn_msg}")
                log_event("info", "smart_defaults_pending_tx",
                          f"Smart Settings ran during {_pending_tx_count} pending tx(s); "
                          f"recommend waiting for wallet to settle.")
    except Exception as e:
        print(f"[SMART_DEFAULTS v2] Wallet fetch failed: {e}")
        messages.append("Wallet: not available")

    # ---- 2. V2: Collect all market data (keep slow-changing Spacescan cache) ----
    try:
        from database import clear_market_analysis_cache
        clear_market_analysis_cache(asset_id, keep_analysis_types=("spacescan",))
        print("[SMART_DEFAULTS v2] Cleared market analysis cache for fresh data (kept Spacescan cache)")
    except Exception:
        pass  # clear_market_analysis_cache may not exist yet — that's fine
    raw = collect_all_market_data(asset_id, ticker_id, decimals)
    analysis = analyze_market_data(raw, asset_id)

    # Extract key data for calculations
    ticker = raw.get("dexie_ticker") or {}
    trades = raw.get("dexie_trades") or {}
    tibet = raw.get("tibet_pool") or {}
    tibet_quote = raw.get("tibet_quote") or {}
    spacescan = raw.get("spacescan") or {}
    db_hist = raw.get("internal_db") or {}

    vol = analysis.get("volatility", {})
    liq = analysis.get("liquidity", {})
    health = analysis.get("token_health", {})
    bot_perf = analysis.get("bot_performance", {})
    quality = analysis.get("data_quality", {})
    risk_level = health.get("risk_level", "moderate")

    # ---- 3. Prices (from collected data) ----
    dexie_price = ticker.get("price", 0)
    tibet_price = tibet.get("price", 0) if tibet.get("has_data") else 0
    spacescan_price = spacescan.get("price_xch", 0) if spacescan.get("has_data") else 0
    mid_price = 0
    arb_gap_bps = 0
    spacescan_gap_bps = 0

    has_both_prices = dexie_price > 0 and tibet_price > 0
    if has_both_prices:
        mid_price = (dexie_price + tibet_price) / 2
        arb_gap_bps = abs(dexie_price - tibet_price) / mid_price * 10000
        messages.append(f"Price: {mid_price:.8f} (Dexie + Tibet)")
        if arb_gap_bps > 50:
            messages.append(f"Arb gap: {_bps_to_pct(arb_gap_bps)}")
    elif dexie_price > 0:
        mid_price = dexie_price
        messages.append(f"Price: {mid_price:.8f} (Dexie only)")
    elif tibet_price > 0:
        mid_price = tibet_price
        messages.append(f"Price: {mid_price:.8f} (Tibet only)")
    else:
        return jsonify({"error": "No price available from Dexie or TibetSwap"})

    if spacescan_price > 0 and mid_price > 0:
        spacescan_gap_bps = abs(spacescan_price - mid_price) / mid_price * 10000

    # ---- 4. Competitor orderbook (still fetch live — changes fast) ----
    orderbook = _fetch_dexie_orderbook_standalone(asset_id)
    if orderbook["has_data"]:
        messages.append(f"Competitors: {orderbook['num_buy_offers']}B/{orderbook['num_sell_offers']}S")

    # ---- 5. Read user inputs (trade size, max offers) ----
    from flask import request as flask_request
    trade_size = _safe_float(flask_request.args.get("trade_size", 0))
    max_buy = int(_safe_float(flask_request.args.get("max_buy", 0)))
    max_sell = int(_safe_float(flask_request.args.get("max_sell", 0)))

    # ══════════════════════════════════════════════════════════════
    # V2 CALCULATION PHASE — data-driven from 30 days of history
    # ══════════════════════════════════════════════════════════════

    print(f"[SMART_DEFAULTS v2] === Calculating settings (data quality: {quality.get('quality', '?')}) ===")

    # ═══ V2: BASE SPREAD (from fill rate + volume) ═══
    # The plan's logic: fill rate determines base, then adjust
    fills_per_day = liq.get("fills_per_day", 0)
    daily_volume = liq.get("daily_volume_xch", 0)

    # Fallback: if no individual trade records but ticker has 30d volume,
    # estimate fill rate from aggregated ticker data
    ticker_volume_30d = ticker.get("volume_30d", 0)
    if fills_per_day == 0 and daily_volume == 0 and ticker_volume_30d > 0:
        daily_volume = ticker_volume_30d / 30.0
        # Estimate fills/day from volume and typical trade size
        avg_trade_est = trade_size if trade_size > 0 else 0.1
        fills_per_day = daily_volume / avg_trade_est if avg_trade_est > 0 else 0
        print(f"[SMART_DEFAULTS v2] Using ticker 30d volume fallback: "
              f"{ticker_volume_30d:.2f} XCH total → {daily_volume:.2f}/day, ~{fills_per_day:.1f} fills/day")

    if fills_per_day > 10 and daily_volume > 5:
        # Active market — tight spreads work
        spread_base = 300  # 3%
        messages.append(f"Active market: {fills_per_day:.0f} fills/day, {daily_volume:.1f} XCH/day → tighter spread")
    elif fills_per_day > 3 and daily_volume > 1:
        # Moderate market
        spread_base = 500  # 5%
        messages.append(f"Moderate market: ~{fills_per_day:.1f} fills/day, {daily_volume:.1f} XCH/day → balanced spread")
    elif fills_per_day > 0.5 or daily_volume > 0.1:
        # Quiet market — profit per trade matters more
        spread_base = 700  # 7%
        messages.append(f"Quiet market: ~{fills_per_day:.1f} fills/day, {daily_volume:.2f} XCH/day → wider spread")
    elif ticker_volume_30d > 0:
        # Very low volume but ticker shows SOME activity
        spread_base = 700  # 7%
        messages.append(f"Low volume: {ticker_volume_30d:.2f} XCH in 30 days → wider spread")
    else:
        # Genuinely no trade data anywhere
        spread_base = 500
        messages.append("No trade data available — using moderate spread")

    # V2: Volatility adjustment (from 30-day analysis, not just 24h)
    regime = vol.get("regime", "normal")
    quiet_phase = vol.get("quiet_phase", False)
    range_90d_pct = vol.get("range_90d_pct", 0)

    # ─── VWAP from trade history ───
    # Weighted average price by XCH volume — better price anchor than simple average.
    vwap_price = 0
    trade_list = (trades.get("trades") or []) if isinstance(trades, dict) else []
    if len(trade_list) >= 3:
        _sum_pv = sum(t.get("price", 0) * t.get("xch_amount", 0) for t in trade_list if t.get("price", 0) > 0 and t.get("xch_amount", 0) > 0)
        _sum_v  = sum(t.get("xch_amount", 0) for t in trade_list if t.get("price", 0) > 0 and t.get("xch_amount", 0) > 0)
        if _sum_v > 0:
            vwap_price = _sum_pv / _sum_v
            print(f"[SMART_DEFAULTS v2] VWAP (30d): {vwap_price:.8f} XCH "
                  f"(vs Dexie {dexie_price:.8f}, Tibet {tibet_price:.8f})")
            messages.append(f"VWAP (30d): {vwap_price:.8f}")
    # Use VWAP as mid_price when it diverges from current price by <10%
    # (avoids anchoring to a stale or thin snapshot price)
    if vwap_price > 0 and mid_price > 0 and abs(vwap_price - mid_price) / mid_price < 0.10:
        mid_price = (mid_price + vwap_price) / 2   # Blend: 50% current, 50% VWAP
        messages.append("Mid blended with VWAP")

    if regime == "extreme":
        vol_adj = 200      # +2%
    elif regime == "volatile":
        vol_adj = 100      # +1%
    elif regime == "quiet":
        vol_adj = -50      # −0.5%
    else:
        vol_adj = 0        # normal — no adjustment

    # V2: Pool depth adjustment (use real quote slippage if available)
    pool_adj = 0
    pool_xch = tibet.get("xch_reserve", 0) if tibet.get("has_data") else 0
    real_slippage_bps = 0
    if tibet_quote and tibet_quote.get("price_impact", 0) > 0:
        # Real slippage from TibetSwap quote — much better than formula!
        real_slippage_bps = abs(tibet_quote["price_impact"]) * 10000
        if real_slippage_bps > 500:
            pool_adj = 100    # Very thin pool: +1%
        elif real_slippage_bps > 200:
            pool_adj = 50     # Thin pool: +0.5%
        messages.append(f"Pool: {pool_xch:.1f} XCH, slippage: {_bps_to_pct(real_slippage_bps)} for 0.01 XCH")
    elif pool_xch > 0:
        # Fallback: estimate from pool depth
        if pool_xch < 50:
            pool_adj = 100
        elif pool_xch < 200:
            pool_adj = 50
        messages.append(f"Pool: {pool_xch:.1f} XCH")

    # V2: Competition adjustment
    comp_adj = 0
    comp_spread = orderbook.get("competitor_spread_bps", 0)
    if comp_spread > 0:
        if comp_spread < spread_base * 0.8:
            comp_adj = -50    # Competitors tighter — narrow a bit
        elif comp_spread > spread_base * 1.5:
            comp_adj = 50     # Competitors wider — widen a bit

    # V2: Spacescan token-health adjustment (context, not live pricing)
    health_adj = 0
    activity_level = health.get("activity_level", "unknown")
    if risk_level == "risky":
        health_adj += 100
    elif risk_level == "thin":
        health_adj += 50

    if activity_level == "dormant":
        health_adj += 75
    elif activity_level == "quiet":
        health_adj += 50
    elif activity_level == "active" and risk_level == "healthy":
        health_adj -= 25

    # V2: Explorer price sanity check
    sanity_adj = 0
    if spacescan_gap_bps > 2500:
        sanity_adj = 75
        messages.append(
            f"Spacescan price differs from executable markets by {_bps_to_pct(spacescan_gap_bps)} — staying conservative"
        )
    elif spacescan_gap_bps > 1000:
        sanity_adj = 25
        messages.append(
            f"Spacescan price is {_bps_to_pct(spacescan_gap_bps)} away from live venues — sanity buffer added"
        )

    # V2: Arb buffer (same as v1 — still valid)
    arb_buffer = min(100, int(arb_gap_bps * 0.1)) if arb_gap_bps > 100 else 0

    # V2: Quiet-phase buffer — token is in a temporary lull; widen spread to
    # survive the (likely inevitable) return to normal volatility.
    quiet_phase_adj = 0
    if quiet_phase:
        quiet_phase_adj = 150   # +1.5% — absorbs the snap-back move
        messages.append(
            f"Quiet phase detected (90d range {range_90d_pct:.1f}% >> 30d) "
            f"— spread widened for snap-back protection"
        )

    # V2: Pool-trend buffer — if the AMM pool is shrinking, slippage will worsen
    # over time and spreads need to compensate.
    pool_trend_adj = 0
    pool_trend = db_hist.get("pool_trend", "unknown")
    if pool_trend == "shrinking":
        pool_trend_adj = 75    # +0.75% — compensate for worsening slippage
        messages.append("Pool trend: shrinking — spread widened for slippage buffer")
    elif pool_trend == "growing":
        pool_trend_adj = -25   # −0.25% — growing pool = better execution
        messages.append("Pool trend: growing — slight spread tightening")

    # ═══ FINAL BASE SPREAD ═══
    base_spread_bps = (spread_base + vol_adj + pool_adj + comp_adj + health_adj
                       + sanity_adj + arb_buffer + quiet_phase_adj + pool_trend_adj)
    base_spread_bps = max(250, min(1000, base_spread_bps))  # 2.5% floor, 10% ceiling

    # ── RISK PROFILE: base spread ──────────────────────────────────────────────
    # Conservative widens the spread (earn more per fill, fill less often).
    # Aggressive tightens it (more competitive, more fills, smaller margin).
    # Applied before inner_edge / requote so all derived values stay consistent.
    if _rp["spread_bps_mult"] != 1.0:
        base_spread_bps = max(250, min(1000, round(base_spread_bps * _rp["spread_bps_mult"])))

    print(f"[SMART_DEFAULTS v2] Spread: {spread_base} base + {vol_adj} vol({regime}) + "
          f"{pool_adj} pool + {comp_adj} comp + {health_adj} health + "
          f"{sanity_adj} sanity + {arb_buffer} arb + {quiet_phase_adj} quiet + "
          f"{pool_trend_adj} pool_trend = {_bps_to_pct(base_spread_bps)}"
          + (f" [×{_rp['spread_bps_mult']} {_risk_profile_name}]" if _rp["spread_bps_mult"] != 1.0 else ""))

    # ═══ INNER EDGE ═══
    inner_edge_bps = max(100, int(base_spread_bps * 0.4))

    # ═══ MIN/MAX SPREAD ═══
    # Keep Smart Defaults internally consistent with the runtime ladder rule:
    # the outer spread must stay at least 1.5x wider than the inner edge.
    required_outer_bps = (inner_edge_bps * 3 + 1) // 2  # ceil(inner_edge_bps * 1.5)
    min_spread_bps = max(200, int(base_spread_bps * 0.6), required_outer_bps)
    max_spread_bps = max(min_spread_bps * 2, min(int(base_spread_bps * 2), 1500))

    # ═══ VOLATILITY WINDOW ═══
    # V2: Set based on actual data depth and volatility regime
    if regime == "extreme" or regime == "volatile":
        volatility_window = 4    # Short window — respond fast to volatile markets
    elif db_hist.get("price_count", 0) > 100:
        volatility_window = 24   # Deep history — look at a full day
    elif db_hist.get("price_count", 0) > 20:
        volatility_window = 8
    else:
        volatility_window = 4    # New bot — keep it responsive

    # ═══ REQUOTE ═══
    # V2: Use real TibetSwap slippage instead of formula
    if real_slippage_bps > 0:
        # Set requote above the noise caused by typical AMM trades
        typical_impact_bps = real_slippage_bps * 100  # Scale: 0.01 XCH quote → full trade
        if trade_size > 0 and pool_xch > 0:
            # Better estimate: scale by our actual trade size vs pool
            trade_ratio = trade_size / pool_xch
            typical_impact_bps = trade_ratio * 10000  # Direct estimate
    elif pool_xch > 0:
        typical_impact_bps = 500 * (1.0 + max(0, (100 - pool_xch) / 100) * 0.5)
    else:
        typical_impact_bps = 500

    # Base: 60% of the full spread.
    # An offer placed at ±(spread/2) from mid should survive until mid has moved
    # well past the offer price — i.e., well past half_spread from the last quote.
    # At 60% of spread, the offer is still ~10% inside the spread when we cancel,
    # meaning it had a real chance to fill and we're not being trigger-happy.
    # Simulation finding: 40% threshold caused 75-95% of fills to be missed.
    spread_based = base_spread_bps * 0.60

    # Also consider raw market-impact noise (scaled to trade size vs pool)
    requote_bps = max(spread_based, typical_impact_bps * 2.0)

    # Volatile / extreme: widen further — price oscillates, let offers ride
    # through the noise rather than churning cancels on every wave
    if regime in ("extreme", "volatile"):
        requote_bps *= 1.15

    # Clamp to spread-relative bounds (55%–80% of full spread)
    # 55% lower: never cancel before the offer could realistically fill
    # 80% upper: don't leave clearly stale offers (offer is past fair value)
    min_requote = base_spread_bps * 0.55
    max_requote = base_spread_bps * 0.80
    requote_bps = max(min_requote, min(max_requote, requote_bps))

    # Absolute floor regardless of spread size
    requote_bps = max(150, requote_bps)

    # ── RISK PROFILE: spread step ──────────────────────────────────────────
    # Conservative widens requote (let offers ride longer, less churn).
    # Aggressive narrows it (cancel sooner, stay tighter to mid).
    # Re-apply absolute floor after adjustment.
    if _rp["spread_step_mult"] != 1.0:
        requote_bps = max(150, requote_bps * _rp["spread_step_mult"])

    print(f"[SMART_DEFAULTS v2] Requote: {_bps_to_pct(requote_bps)} "
          f"(slippage={_bps_to_pct(real_slippage_bps)}, pool={pool_xch:.0f} XCH)")

    # ═══ RESERVES ═══
    # Smart Defaults does NOT touch reserves — that's the user's choice.
    # We still calculate available amounts using the user's current reserve setting.

    # ═══ V2: MAX POSITION (from token health) ═══
    # Max position = how much inventory imbalance is tolerated before the
    # circuit breaker disables one side.  Must be large enough that normal
    # fill clustering doesn't constantly trip the breaker.
    # Floor: at least 5× trade size so a small run of fills doesn't halt.
    risk_level = health.get("risk_level", "moderate")
    if has_wallet and xch_spendable > 0:
        if risk_level == "healthy":
            max_position = round(xch_spendable * 0.40, 1)   # 40% for healthy tokens
        elif risk_level == "moderate":
            max_position = round(xch_spendable * 0.30, 1)   # 30% for moderate
        elif risk_level == "thin":
            max_position = round(xch_spendable * 0.20, 1)   # 20% for thin
        else:
            max_position = round(xch_spendable * 0.15, 1)   # 15% for risky
        # Floor: at least 5× trade size so fills don't trip breaker too fast
        min_position = round(trade_size * 5, 1) if trade_size > 0 else 5.0
        max_position = max(max_position, min_position)
    else:
        max_position = 5.0

    # ── RISK PROFILE: max position ─────────────────────────────────────────────
    # Conservative trips circuit breaker sooner (less inventory risk exposure).
    # Aggressive allows bigger inventory swings before halting one side.
    # Floor kept at min_position so a single fill doesn't immediately trip it.
    if _rp["position_mult"] != 1.0:
        _min_pos = round(trade_size * 5, 1) if trade_size > 0 else 5.0
        max_position = max(_min_pos, round(max_position * _rp["position_mult"], 1))

    # ═══ V2: SKEW INTENSITY (from price trend) ═══
    price_trend = trades.get("price_trend_pct", 0) if trades else 0
    if abs(price_trend) > 10:
        skew_intensity = 0.5     # Trending → aggressive rebalancing
    elif liq.get("level") == "very_low":
        skew_intensity = 0.2     # Low volume → gentle
    else:
        skew_intensity = 0.3     # Moderate default

    # ── RISK PROFILE: skew intensity ───────────────────────────────────────────
    # Conservative: gentler skew — let inventory drift rather than forcing rebalance.
    # Aggressive: snap back to neutral faster to stay balanced.
    # Clamped 0.1–0.8 so we never fully disable or max-out the skew.
    if _rp["skew_mult"] != 1.0:
        skew_intensity = max(0.1, min(0.8, round(skew_intensity * _rp["skew_mult"], 2)))

    # ═══ EMERGENCY BRAKE ═══
    # V2: Use 30-day max single-day move for better calibration
    max_move = vol.get("max_single_move_pct", 0)
    if max_move > 0:
        max_mid_move = max(2.0, min(20.0, max_move * 2))  # 2x worst day
    elif vol.get("range_30d_pct", 0) > 0:
        max_mid_move = max(2.0, min(20.0, vol["range_30d_pct"] / 3))
    else:
        max_mid_move = 5.0

    # ═══ DYNAMIC BAND (DYNAMIC_LIMIT_PCT) ═══
    # How wide the ±% band around the EMA reference should be.
    # Must comfortably contain the token's real swing range — a band that's
    # too tight causes false rejects on legitimate volatile moves.
    # For quiet-phase tokens, use the 90d range (their true volatility profile)
    # instead of the misleadingly calm 30d window.
    range_30d_pct = vol.get("range_30d_pct", 0)
    band_basis = range_90d_pct if (quiet_phase and range_90d_pct > range_30d_pct) else range_30d_pct
    if regime == "extreme":
        dynamic_limit_pct = max(100, round(band_basis * 1.5 / 5) * 5)   # ≥100%, rounded to 5
    elif regime == "volatile":
        dynamic_limit_pct = max(60,  round(band_basis * 1.5 / 5) * 5)   # ≥60%
    elif regime == "quiet":
        dynamic_limit_pct = max(20,  round(band_basis * 1.5 / 5) * 5)   # ≥20%
    else:
        dynamic_limit_pct = max(40,  round(band_basis * 1.5 / 5) * 5)   # ≥40% normal
    # Pool-depth correction: thin AMM pools amplify price shocks because even a
    # modest buy moves the quoted price significantly.  Widen the band so a
    # sudden pool-driven price tick doesn't falsely reject a valid price feed.
    _pool_band_bump = 0
    if pool_xch > 0 and pool_xch < 200:
        # Linear bump: 0 XCH pool → +50%, 100 XCH → +25%, 200 XCH → 0%
        _pool_band_bump = max(0, round((200 - pool_xch) / 4 / 5) * 5)
        dynamic_limit_pct = min(200, dynamic_limit_pct + _pool_band_bump)

    dynamic_limit_pct = min(dynamic_limit_pct, 200)   # Hard ceiling 200%
    if dynamic_limit_pct == 0:
        dynamic_limit_pct = 50   # Fallback if no data
    _band_note = f" (using 90d range — quiet phase)" if (quiet_phase and range_90d_pct > range_30d_pct) else ""
    if _pool_band_bump:
        _band_note += f" (+{_pool_band_bump}% thin-pool shock buffer)"
    messages.append(f"Dynamic band: ±{dynamic_limit_pct}% ({regime} regime){_band_note}")

    # ═══ STEP-CHANGE GUARD (MAX_STEP_CHANGE_FRACTION) ═══
    # Rejects a price fetch that moved more than N% from the previous reading.
    # Purpose: catch API glitches, not legitimate market moves.
    # Set to 2× the worst observed single day — generous enough that real
    # volatility doesn't falsely trip it, tight enough to catch bad data.
    # Disabled (0) for extreme tokens where any move is plausible.
    if regime == "extreme":
        max_step_change_pct = 0   # Disable — too risky to reject real moves
        messages.append("Step-change guard: disabled (extreme volatility)")
    elif max_move > 0:
        raw_step = max_move * 2.0   # 2× worst single-day move observed
        max_step_change_pct = max(15, min(40, round(raw_step / 5) * 5))
        messages.append(f"Step-change guard: {max_step_change_pct}% (2× {max_move:.0f}% worst day)")
    elif range_30d_pct > 0:
        max_step_change_pct = max(15, min(40, round(range_30d_pct / 5) * 5))
        messages.append(f"Step-change guard: {max_step_change_pct}% (from 30d range)")
    else:
        max_step_change_pct = 0   # No data — leave disabled

    # ═══ ARB ALERT THRESHOLD ═══
    # The Dexie-vs-Tibet gap that triggers an emergency mid-cycle requote.
    # Volatile tokens naturally have wider gaps so the threshold needs raising
    # to avoid constant false-trigger emergency requotes.
    if regime == "extreme":
        arb_alert_threshold_bps = 500
    elif regime == "volatile":
        arb_alert_threshold_bps = 350
    elif regime == "quiet":
        arb_alert_threshold_bps = 100
    else:
        arb_alert_threshold_bps = 200
    # Also factor in the live arb gap — if the gap is normally wide, set above it
    if arb_gap_bps > arb_alert_threshold_bps * 0.8:
        arb_alert_threshold_bps = max(arb_alert_threshold_bps, int(arb_gap_bps * 1.5))
    arb_alert_threshold_bps = min(arb_alert_threshold_bps, 1000)

    # ═══ LOOP SECONDS (volatility + fill-rate aware) ═══
    # Primary driver: volatility regime (price shock response speed).
    # Secondary: fill rate — high-fill markets need fast fill detection
    # regardless of volatility, because spare coins deplete quickly.
    if regime == "extreme":
        loop_seconds = 30
    elif regime == "volatile":
        loop_seconds = 45
    elif regime == "quiet":
        loop_seconds = 90
    else:
        loop_seconds = 60
    # Fill-rate override: can only tighten the loop, never loosen it.
    # A busy market burning through spare coins faster than the loop detects
    # fills will eventually run dry mid-ladder.
    if fills_per_day > 10 and loop_seconds > 30:
        loop_seconds = 30   # Very active: match extreme-volatility speed
    elif fills_per_day > 5 and loop_seconds > 45:
        loop_seconds = 45   # Active: match volatile speed

    # ═══ REQUOTE BATCH SIZE ═══
    # How many offers to cancel/recreate per requote pass.
    # Volatile tokens need smaller batches — individual offers matter more
    # and wallet contention is higher when things move fast.
    if regime in ("extreme", "volatile"):
        requote_batch_size = 3
    else:
        requote_batch_size = 5

    # ═══ PRICE RAILS ═══
    # V2: Use 30-day range with buffer instead of arbitrary ±50%
    high_30d = ticker.get("high_30d", 0)
    low_30d = ticker.get("low_30d", 0)
    if high_30d > 0 and low_30d > 0:
        range_30d = high_30d - low_30d
        min_mid = max(0, low_30d - range_30d * 0.5)   # 50% below 30d low
        max_mid = high_30d + range_30d * 0.5            # 50% above 30d high
        messages.append(f"Price rails from 30d range: {low_30d:.8f} – {high_30d:.8f}")
    else:
        min_mid = mid_price * 0.5 if mid_price > 0 else 0
        max_mid = mid_price * 1.5 if mid_price > 0 else 0

    # Safety floor: max_mid must always be at least 15% above current price.
    # Without this, a bull run pushes the price above the 30d high and the
    # ceiling is breached the moment Smart Settings saves — blocking every cycle.
    if mid_price > 0:
        min_max_mid = mid_price * 1.15
        if max_mid < min_max_mid:
            messages.append(f"Price rail ceiling raised to 15% above current price "
                            f"({mid_price:.8f} → {min_max_mid:.8f}) — market is above 30d high")
            max_mid = min_max_mid
    # Safety floor: min_mid must always be at least 15% below current price.
    if mid_price > 0 and min_mid > mid_price * 0.85:
        min_mid = mid_price * 0.85

    # ═══ COMPETITOR AWARENESS ═══
    competitor_enabled = True

    # ═══ COIN PREP HEADROOM ═══
    # Extra size added to each prepared coin so the bot has room for price drift
    # between when a coin is prepped and when it's used. Volatile tokens need
    # wider headroom — their price can move more between prep and use.
    if regime == "extreme":
        coin_prep_headroom_pct = 15
    elif regime == "volatile":
        coin_prep_headroom_pct = 12
    elif regime == "quiet":
        coin_prep_headroom_pct = 7
    else:
        coin_prep_headroom_pct = 10
    # Shallow pool adds price uncertainty → extra 3%
    if 0 < pool_xch < 100:
        coin_prep_headroom_pct = min(20, coin_prep_headroom_pct + 3)

    # ═══ TIER SPARE COUNTS (F62) ═══
    # How many backup prepared coins to keep per tier.
    # Fill rate drives the absolute counts; position-inner always gets the
    # biggest buffer because inner offers sit closest to mid and fill first.
    #
    # Under reverse-buy (the default), these counts flow to:
    #   _sell_spare_inner  = _spare_inner   → CAT inner size (position inner on sell side)
    #   _buy_spare_extreme = _spare_inner   → XCH extreme size (position inner on buy side)
    # So bumping `_spare_inner` adds spares to the most-active tier on BOTH sides.
    #
    # Ratios are monotonic inner > mid > outer > extreme, matching the fill
    # frequency gradient. Absolute values are ~2× the pre-F62 defaults so a
    # fresh-deploy ladder doesn't immediately trip the coin-health alarm.
    if fills_per_day > 10:
        _spare_inner   = 15  # Very active: fills arrive faster than coin prep
        _spare_mid     = 8
        _spare_outer   = 4
        _spare_extreme = 2
    elif fills_per_day > 3:
        _spare_inner   = 10  # Active: large cluster buffer, heavy inner bias
        _spare_mid     = 5
        _spare_outer   = 3
        _spare_extreme = 2
    elif fills_per_day > 0.5:
        _spare_inner   = 7   # Moderate: meaningful buffer, inner-weighted
        _spare_mid     = 4
        _spare_outer   = 2
        _spare_extreme = 1
    else:
        # Quiet / no-data: still keep a solid inner buffer so back-to-back
        # fills never leave the most-active tier empty while coin prep runs.
        _spare_inner   = 5
        _spare_mid     = 3
        _spare_outer   = 1
        _spare_extreme = 1

    # ── RISK PROFILE: spare coin adjustment ────────────────────────────────────
    # Conservative adds +1 spare to each active tier (more TX safety buffer
    # so a sudden fill cluster doesn't leave the ladder exposed while coin prep runs).
    # Aggressive keeps standard counts (more capital deployed in offers instead).
    if _rp["spare_adj"] != 0:
        _spare_inner = max(0, _spare_inner + _rp["spare_adj"])
        if _spare_mid   > 0: _spare_mid   = max(0, _spare_mid   + _rp["spare_adj"])
        if _spare_outer > 0: _spare_outer = max(0, _spare_outer + _rp["spare_adj"])
        if _spare_extreme > 0: _spare_extreme = max(0, _spare_extreme + _rp["spare_adj"])

    # ═══ F63 (2026-04-10): PRICE SHOCK SPARE FLOOR ═══
    # During a full-ladder requote (price move), the bot needs enough spare
    # coins to create the first wave of replacements BEFORE any old offers
    # are cancelled. Floor: 50% of each tier's live count.
    import math as _math_spare
    # Note: _target_n is set later after capital plan. These spares are
    # computed from the market-activity _market_n estimate which drives
    # tier count decisions. The actual tier counts (_smart_n_*) are set
    # after the capital plan, so we apply the floor AGAIN after they're
    # finalized — see the second F63 block below the capital plan.
    # For now, just mark that the shock floor should be applied.
    _apply_shock_floor = True

    # ═══ COIN PREP MULTIPLIER ═══
    # Calculated later, after the capital plan — needs _smart_trade_size and _smart_max_buy.
    coin_prep_multiplier = 1.0   # Placeholder; overwritten below after capital plan.

    # ═══ V2 Data Quality Messages ═══
    quality_score = quality.get("score", 0)
    quality_label = quality.get("quality", "unknown")
    messages.append(f"Data quality: {quality_score}% ({quality_label})")

    # Volatility info
    if vol.get("confidence") == "high":
        messages.append(f"Volatility: {vol['regime']} ({vol.get('std_dev_pct', 0):.1f}% daily std dev)")
    elif vol.get("range_30d_pct", 0) > 0:
        messages.append(f"30d range: {vol['range_30d_pct']:.1f}% ({vol.get('regime', 'normal')})")

    # Trade/volume info — show individual trades OR ticker volume
    if trades and trades.get("total_count", 0) > 0:
        messages.append(f"30d trades: {trades['total_count']} ({trades.get('volume_trend', '?')} volume)")
    elif ticker_volume_30d > 0:
        messages.append(f"30d volume: {ticker_volume_30d:.2f} XCH (from ticker)")

    # Token health
    if health.get("holder_count", 0) > 0:
        messages.append(
            f"Token: {health['holder_count']} holders, {risk_level} risk, {activity_level} activity"
        )

    # Bot's own history
    if bot_perf.get("has_history"):
        messages.append(f"Bot history: {db_hist.get('fill_count', 0)} fills")
    else:
        messages.append("Bot: first run — will improve with trading history")

    # ═══ Fee estimation (Coinset) ═══
    # Use Coinset to estimate realistic fee coin sizes rather than hard-coded defaults.
    # Target 120s confirmation; also fetch 60s to catch congestion spikes.
    # Fee coin size must comfortably exceed the fee so change can recycle.
    try:
        from tx_fees import get_suggested_transaction_fee
        # Typical CAT spend ~35M cost units; target 120s for normal market-making pace
        _fee_est = get_suggested_transaction_fee(target_seconds=120, cost=35_000_000)
        _fee_est_60 = get_suggested_transaction_fee(target_seconds=60, cost=35_000_000)
        if _fee_est.get("available"):
            _fee_mojos = int(_fee_est.get("fee_mojos", 0) or 0)
            _fee_mojos_60 = int(_fee_est_60.get("fee_mojos", 0) or 0)
            # Use the higher of 60s/120s for headroom — covers congestion spikes
            _peak_fee_mojos = max(_fee_mojos, _fee_mojos_60)
            _peak_fee_xch = _peak_fee_mojos / 1e12
            # Fee coin must be at least 20x the peak fee so it can recycle ~10 times
            # before a top-up is needed. Hard minimum 0.001 XCH.
            _fee_coin_raw = max(0.001, _peak_fee_xch * 20)
            # Round up to 3 significant figures for cleanliness
            import math as _math
            if _fee_coin_raw >= 0.001:
                _magnitude = 10 ** (_math.floor(_math.log10(_fee_coin_raw)) - 2)
                _fee_coin_size = round(_math.ceil(_fee_coin_raw / _magnitude) * _magnitude, 6)
            else:
                _fee_coin_size = 0.001
            _smart_fee_xch = round(_peak_fee_xch, 10) if _peak_fee_xch > 0 else float(
                getattr(cfg, "TRANSACTION_FEE_XCH", Decimal("0.000001"))
            )
            messages.append(
                f"Fee (Coinset, 60s peak): {_peak_fee_xch:.8f} XCH → coin size {_fee_coin_size:.4f} XCH"
            )
        else:
            # Coinset unavailable — preserve existing values
            _fee_coin_size = float(getattr(cfg, "FEE_COIN_SIZE_XCH", Decimal("0.001")))
            _smart_fee_xch = float(getattr(cfg, "TRANSACTION_FEE_XCH", Decimal("0.000001")))
    except Exception:
        _fee_coin_size = float(getattr(cfg, "FEE_COIN_SIZE_XCH", Decimal("0.001")))
        _smart_fee_xch = float(getattr(cfg, "TRANSACTION_FEE_XCH", Decimal("0.000001")))

    # ═══ Capital Allocation — reserve-first, scales from 1 XCH to thousands ═══
    # Reserve-first: everything sized from what the user is willing to risk.
    # xch_reserve / cat_reserve arrive as ABSOLUTE amounts from the frontend
    # (XCH and tokens respectively — the reserve input fields hold absolute values,
    # not percentages).  Cap at spendable so a large reserve never goes negative.
    _xch_reserve = min(xch_spendable, max(0.0, float(xch_reserve or 0)))
    _cat_reserve = min(cat_spendable, max(0.0, float(cat_reserve or 0)))
    _avail_xch = max(0.0, xch_spendable - _xch_reserve)
    _avail_cat = max(0.0, cat_spendable - _cat_reserve)

    # Practical minimum: Dexie offers below this aren't worth a taker's fee
    _MIN_OFFER_XCH = 0.005

    # ── Percentage-based pool allocation ──
    # Fee pool:    3% of available → buy fee coins at Coinset-estimated size
    # Sniper pool: 4% of available → split into prep coins
    # Trading:     remaining ~93%
    _FEE_PCT    = 0.03
    # Sniper pool scales with fill rate: busier markets need more sniper coins
    # for rapid rearm after each probe fills.
    if fills_per_day > 10:
        _SNIPER_PCT = 0.07   # Very active: 7% — frequent probes, fast rearm
    elif fills_per_day > 3:
        _SNIPER_PCT = 0.06   # Active: 6%
    else:
        _SNIPER_PCT = 0.04   # Normal/quiet: 4%

    _SNIPER_MIN_SIZE_XCH = 0.01
    _smart_sniper_size = _SNIPER_MIN_SIZE_XCH

    # ── Fee < Sniper enforcement ──
    # Sage auto-picks the smallest available coin for fees.  Fee coins MUST
    # be smaller than sniper coins so Sage always grabs the right pool.
    # If Coinset-estimated fee size is ≥ sniper size, clamp it to half the
    # sniper size — still large enough for ~10 reuses but clearly smaller.
    if _fee_coin_size >= _SNIPER_MIN_SIZE_XCH:
        _fee_coin_size = round(_SNIPER_MIN_SIZE_XCH / 2, 6)  # 0.005 XCH
        messages.append(
            f"Fee coin size clamped to {_fee_coin_size} XCH "
            f"(must be < sniper min {_SNIPER_MIN_SIZE_XCH} XCH)"
        )

    _fee_pool_target  = _avail_xch * _FEE_PCT
    _fee_prep_count   = max(5, min(50, int(_fee_pool_target / max(0.0001, _fee_coin_size))))
    _fee_pool_xch     = _fee_coin_size * _fee_prep_count

    _sniper_pool_raw   = _avail_xch * _SNIPER_PCT
    # Sniper offers are expendable probes — keep them at Dexie's minimum
    # displayable size (0.01 XCH) so they show up on the book without wasting
    # capital. The pool carries many cheap coins rather than fewer large ones.

    # Prep count: more fills = faster sniper coin burn = need more ready.
    # Cap scales with the sniper pool so we never prep more than the pool
    # can fund at the fixed minimum size.
    if fills_per_day > 10:
        _sniper_max_prep = 30
    elif fills_per_day > 3:
        _sniper_max_prep = 25
    else:
        _sniper_max_prep = 20
    _pool_max_prep = int(_sniper_pool_raw / max(0.0001, _smart_sniper_size))
    _smart_sniper_prep = max(5, min(_sniper_max_prep, _pool_max_prep))
    _sniper_pool_xch   = round(_smart_sniper_size * _smart_sniper_prep, 4)

    # ── Bottleneck-driven capital allocation ─────────────────────────────────
    # The bot is symmetric: every buy needs XCH, every sell needs CAT.
    # Whichever side has less spending power (in XCH-equivalent terms) is
    # the bottleneck — use ALL of the smaller side (after fees/sniper pools
    # are carved off), carve 10% for a topup buffer, and the remaining 90%
    # becomes the trading budget.
    #
    # Tim's mental model:
    #   1. Subtract the user's reserve (do not touch).
    #   2. Bottleneck = min(post-pools XCH, avail CAT × mid_price).
    #   3. Carve 10% off the bottleneck for the topup buffer (large unbroken
    #      coins the topup worker splits when a tier runs short).
    #   4. Remaining 90% = trading budget per side (symmetric).
    #
    # NOTE: do NOT pre-shrink the CAT side by a "prep overhead" factor here.
    # The capital-plan solver below (_solve_base_cat) already accounts for
    # headroom + spare overhead when it sizes the ladder, and the CAT
    # feasibility clamp at the end scales tier sizes down if coin prep
    # overshoots. Applying overhead HERE just flips the bottleneck the wrong
    # way (e.g. treating 113 XCH-worth of CAT as 71) and leaves huge amounts
    # of capital stranded in the topup buffer.
    _post_pools_xch = max(0.0, _avail_xch - _fee_pool_xch - _sniper_pool_xch)
    if mid_price and mid_price > 0 and _avail_cat > 0:
        _cat_xch_equiv = round(_avail_cat * mid_price, 4)
        _bottleneck_xch = min(_post_pools_xch, _cat_xch_equiv)
        _cat_limited_trading = (_cat_xch_equiv < _post_pools_xch)
    else:
        _cat_xch_equiv = None
        _bottleneck_xch = _post_pools_xch
        _cat_limited_trading = False
    # Kept for downstream message formatting — same value as bottleneck when
    # CAT is binding, else matches the raw XCH equivalent.
    _cat_xch_capacity = _cat_xch_equiv

    # F62 (2026-04-09): topup buffer percentage computed fresh from market
    # activity, NOT read from cfg. Smart Settings is supposed to recompute
    # every field from scratch — the topup pool should behave like every
    # other slider on the page. Reading `cfg.TOPUP_POOL_PCT` was letting
    # a stale 0.10 from an earlier Smart Settings run silently override
    # the new recommendation.
    #
    # Scale with fill rate: busier markets burn spare coins faster, so a
    # bigger reserve buys more autonomous runtime between refreshes.
    # Quiet markets can run leaner and put more capital into the trading
    # ladder. The band is 15–25% so the trading capacity always sits
    # comfortably above 75% of avail.
    if fills_per_day > 10:
        _TOPUP_BUFFER_PCT = 0.25   # Very active: 25% — fast cluster burn
    elif fills_per_day > 3:
        _TOPUP_BUFFER_PCT = 0.20   # Active: 20% — ~2 days of autonomous runtime
    elif fills_per_day > 0.5:
        _TOPUP_BUFFER_PCT = 0.15   # Moderate: 15% — standard buffer
    else:
        _TOPUP_BUFFER_PCT = 0.15   # Quiet: 15% — minimum healthy buffer
    _topup_buffer_reserve = round(_bottleneck_xch * _TOPUP_BUFFER_PCT, 4)
    if _topup_buffer_reserve > _bottleneck_xch:
        _topup_buffer_reserve = round(_bottleneck_xch, 4)

    # F55 (2026-04-09): Initialise the FINAL topup buffer value upfront so
    # the API response always has a real number even when the capital plan
    # branch below didn't run (insufficient capital). It's recomputed inside
    # the main capital plan as offers are sized, then bumped by the 2× largest
    # tier guard.
    _topup_buffer_xch = _topup_buffer_reserve

    # Remaining 90% becomes the trading budget — same value on both sides
    # so the ladder is perfectly symmetric and coin prep fits on both sides.
    _trading_xch = max(0.0, round(_bottleneck_xch - _topup_buffer_reserve, 4))

    # F62 (2026-04-09): save independent per-side budgets so the asymmetric
    # sizing block at the end of the capital plan can solve each side from
    # its own pool. The main plan still runs with the symmetric `_trading_xch`
    # (which uses the smaller of the two budgets) for backward compat with
    # the existing clamps — those output a SELL-side-safe base_size. F62
    # then overrides the BUY side with its own max, independently.
    _orig_xch_budget = max(0.0, round(_post_pools_xch - _topup_buffer_reserve, 4))
    if _cat_xch_equiv is not None and _cat_xch_equiv > 0:
        _cat_topup_reserve_xch_equiv = round(_cat_xch_equiv * _TOPUP_BUFFER_PCT, 4)
        _orig_cat_budget_xch = max(0.0, round(_cat_xch_equiv - _cat_topup_reserve_xch_equiv, 4))
    else:
        _cat_topup_reserve_xch_equiv = 0.0
        _orig_cat_budget_xch = 0.0

    # ── Market activity drives offer count ──
    # Capital determines SIZES; market activity determines how many offers to maintain.
    # 3× wider distribution: the same total XCH per tier is spread across 3× more
    # price slots, so each individual offer is ~1/3 the size but populates more
    # of the book. Total XCH deployed per tier is unchanged.
    if fills_per_day > 10:
        _market_n = 60
    elif fills_per_day > 3:
        _market_n = 45
    elif fills_per_day > 0.5:
        _market_n = 36
    else:
        _market_n = 24

    # Hard cap: never more offers than the minimum floor can support
    # (uses 2.5 as an approximate capital factor before tier selection)
    _max_possible_n = max(2, int(_trading_xch / (_MIN_OFFER_XCH * 2.5)))
    _target_n = min(_market_n, _max_possible_n)

    # ── Pool impact cap ──
    # If the user's capital is large vs the pool, takers face high slippage on outer
    # offers — cap depth so the ladder stays effective.
    # Caps are also scaled 3× to match the wider distribution goal.
    _pool_note = ""
    if pool_xch > 0 and _trading_xch > 0:
        _pool_ratio = _trading_xch / pool_xch
        if _pool_ratio > 0.5:
            _target_n  = max(2, min(_target_n, 24))
            _pool_note = "pool-dominated"
        elif _pool_ratio > 0.2:
            _target_n  = max(2, min(_target_n, 36))
            _pool_note = "pool-aware"

    # ── RISK PROFILE: capital deployment ──────────────────────────────────────
    # Scales the trading XCH pool, not the offer count.  This keeps the same
    # number of offers (market-activity-driven) but makes each offer
    # proportionally smaller/larger.  Scaling _target_n would produce the
    # opposite of what's wanted: fewer offers ÷ same XCH = BIGGER per-offer
    # size, which is wrong for a conservative profile.
    if _rp["capital_mult"] != 1.0:
        _trading_xch = max(0.0, round(_trading_xch * _rp["capital_mult"], 4))

    # NOTE: CAT-limited bottleneck handling now lives in the bottleneck-driven
    # capital allocation block above (uses _PREP_OVERHEAD to account for
    # headroom + spare coin overhead). Do NOT re-clamp _trading_xch here — that
    # would override the overhead-adjusted capacity with the raw CAT XCH
    # equivalent and re-introduce the over-allocation bug.

    # Trading pct computed here so it reflects the definitive value (post-CAT-limit).
    _trading_pct = round(_trading_xch / _avail_xch * 100, 1) if _avail_xch > 0 else 0.0

    # ── Market regime → tier size multipliers ──
    # Sizes are relative to base_size (mid tier = 1×).
    # Inner is the offer closest to mid — fills most often.  It should be
    # modestly larger than mid (to capture more spread per fill) but NOT
    # so large that each fill locks up disproportionate capital.
    # Outer/extreme are sized up relative to previous values because in
    # volatile markets those tiers do fill, and larger outer offers catch
    # bigger moves efficiently.  Capital is redistributed from inner → outer.
    if regime in ("volatile", "extreme"):
        _size_mults = (1.2, 1.0, 0.75, 0.40)
        _tier_style = "spread"           # price reaches outer tiers regularly
    elif fills_per_day > 10:
        _size_mults = (1.5, 1.0, 0.65, 0.30)
        _tier_style = "balanced"         # active market, all tiers fill occasionally
    elif fills_per_day > 1:
        _size_mults = (1.8, 1.0, 0.55, 0.25)
        _tier_style = "standard"         # moderate, inner still largest
    else:
        _size_mults = (2.5, 1.0, 0.40, 0.15)
        _tier_style = "concentrated"     # quiet, put capital where fills happen

    # Shallow pool: large outer orders face slippage takers won't accept
    if 0 < pool_xch < 100:
        _sm = list(_size_mults)
        _sm[2] = round(_sm[2] * 0.7, 3)
        _sm[3] = round(_sm[3] * 0.4, 3)
        _size_mults = tuple(_sm)
        if not _pool_note:
            _pool_note = "shallow-pool"

    # ── Market regime → count distribution ──
    # What fraction of total offers goes to each tier.
    if regime in ("volatile", "extreme"):
        _count_dist = (0.30, 0.30, 0.25, 0.15)
    elif fills_per_day > 10:
        _count_dist = (0.35, 0.30, 0.22, 0.13)
    elif fills_per_day > 1:
        _count_dist = (0.42, 0.30, 0.20, 0.08)
    else:
        _count_dist = (0.52, 0.33, 0.12, 0.03)

    # ── Auto-disable tiers whose offer size would be below the practical floor ──
    # Estimate base size using current 4-tier factor, then check each tier.
    _TIER_AVG_EST = sum(d * m for d, m in zip(_count_dist, _size_mults))
    _base_est     = _trading_xch / max(1, _target_n * _TIER_AVG_EST * 2.0)
    _max_tiers = 4
    if _base_est * _size_mults[3] < _MIN_OFFER_XCH:
        _max_tiers = 3
    if _max_tiers >= 3 and _base_est * _size_mults[2] < _MIN_OFFER_XCH:
        _max_tiers = 2

    # Zero out disabled tiers
    _size_mults = (
        _size_mults[0],
        _size_mults[1],
        _size_mults[2] if _max_tiers >= 3 else 0.0,
        _size_mults[3] if _max_tiers == 4 else 0.0,
    )
    # Redistribute count weight from disabled tiers into inner
    _cd = list(_count_dist)
    if _max_tiers < 4:
        _cd[0] += _cd[3]; _cd[3] = 0.0
    if _max_tiers < 3:
        _cd[0] += _cd[2]; _cd[2] = 0.0
    _total_cd = sum(_cd) or 1.0
    _count_dist = tuple(c / _total_cd for c in _cd)

    # Final capital factor with confirmed tiers.
    # NOTE (2026-04-07): removed legacy × 2.0 multiplier.  The × 2 assumed the
    # trading budget was shared between buy and sell sides, but buy offers lock
    # XCH and sell offers lock CAT — each side drains its own wallet, so the
    # full _trading_xch belongs to the buy side (and full _avail_cat to sell).
    # Previously ~50% of the XCH budget went unused on the XCH side.
    _TIER_AVG = sum(d * m for d, m in zip(_count_dist, _size_mults))
    _TIER_CAPITAL_FACTOR = round(_TIER_AVG, 4)

    # Reverse-buy effective factor.  When BUY_LADDER_REVERSED is on the GUI
    # swaps the buy-side count distribution (inner↔extreme, mid↔outer) so the
    # densest counts move to the smallest-size positions.  Effective buy
    # weighting therefore uses count_dist applied in reverse against size_mults.
    # In normal mode this collapses to the same value as _TIER_AVG.
    _buy_ladder_reversed = bool(getattr(cfg, "BUY_LADDER_REVERSED", False))
    _BUY_TIER_AVG = (
        sum(_count_dist[3 - i] * _size_mults[i] for i in range(4))
        if _buy_ladder_reversed else _TIER_AVG
    )
    _BUY_TIER_FACTOR = round(_BUY_TIER_AVG, 4)

    # Spare overhead (in base_size units): prepared spare coins live outside
    # active offers but still consume capital.  Include them in the divisor so
    # base_size × (active + spares + headroom) ≈ side budget exactly.
    # Sell-side spares stay in size order; buy-side spares are display-swapped
    # under reverse buy so the heaviest spare lands on the smallest size.
    _SPARE_OVERHEAD = (
        _spare_inner   * _size_mults[0] +
        _spare_mid     * _size_mults[1] +
        _spare_outer   * _size_mults[2] +
        _spare_extreme * _size_mults[3]
    )
    _BUY_SPARE_OVERHEAD = (
        sum((
            _spare_inner, _spare_mid, _spare_outer, _spare_extreme
        )[3 - i] * _size_mults[i] for i in range(4))
        if _buy_ladder_reversed else _SPARE_OVERHEAD
    )
    _CP_HEADROOM_MULT = 1.0 + (coin_prep_headroom_pct / 100.0)

    # ── Defaults ──
    _smart_max_buy   = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS",  5) or 5)
    _smart_max_sell  = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 5) or 5)
    _smart_n_inner   = 0
    _smart_n_mid     = 0
    _smart_n_outer   = 0
    _smart_n_extreme = 0
    _smart_inner     = 0.0
    _smart_mid       = 0.0
    _smart_outer     = 0.0
    _smart_extreme   = 0.0
    _smart_trade_size = 0.0
    _capital_plan    = {}
    _n_sell_cap      = 0   # F64: CAT-backed sell capacity (set inside capital plan)

    if _avail_xch > 0 and _trading_xch >= (_MIN_OFFER_XCH * 2) and _target_n > 0:
        # Derive base size from trading capital — includes active + spares + headroom.
        # Two budgets:
        #   buy:  _trading_xch ≥ (n*_BUY_TIER_FACTOR  + _BUY_SPARE_OVERHEAD) * HEADROOM * base
        #   sell: _avail_cat   ≥ (n*_TIER_CAPITAL_FACTOR + _SPARE_OVERHEAD) * HEADROOM * (base/mid_price)
        # In normal mode buy/sell collapse to the same divisor.  In reverse-buy
        # mode the buy-side count distribution is flipped so its weighted sum
        # is much smaller — base can grow until the sell-side CAT budget binds.
        def _solve_base_xch(n):
            _den = max(1e-9, (n * _BUY_TIER_FACTOR + _BUY_SPARE_OVERHEAD) * _CP_HEADROOM_MULT)
            return _trading_xch / _den

        def _solve_base_cat(n):
            if not (mid_price and mid_price > 0 and _avail_cat > 0):
                return float("inf")
            _den = max(1e-9, (n * _TIER_CAPITAL_FACTOR + _SPARE_OVERHEAD) * _CP_HEADROOM_MULT)
            return (_avail_cat * mid_price) / _den

        def _solve_base(n):
            return min(_solve_base_xch(n), _solve_base_cat(n))

        _base_size = _solve_base(_target_n)

        # Enforce minimum floor — reduce n if needed.
        if _base_size < _MIN_OFFER_XCH:
            # Binding side: whichever solver gives the smaller base.
            _xch_units = _trading_xch / (_MIN_OFFER_XCH * _CP_HEADROOM_MULT)
            _n_xch     = int((_xch_units - _BUY_SPARE_OVERHEAD) / max(1e-9, _BUY_TIER_FACTOR))
            if mid_price and mid_price > 0 and _avail_cat > 0:
                _cat_units = (_avail_cat * mid_price) / (_MIN_OFFER_XCH * _CP_HEADROOM_MULT)
                _n_cat     = int((_cat_units - _SPARE_OVERHEAD) / max(1e-9, _TIER_CAPITAL_FACTOR))
                _target_n  = max(2, min(_n_xch, _n_cat))
            else:
                _target_n  = max(2, _n_xch)
            _base_size = _solve_base(_target_n)

        # XCH-backed buy capacity at the trial base size.
        _n_buy = _target_n
        if _base_size > 0:
            _xch_units_avail = _trading_xch / _base_size
            _n_buy = max(0, int(
                (_xch_units_avail / _CP_HEADROOM_MULT - _BUY_SPARE_OVERHEAD)
                / max(1e-9, _BUY_TIER_FACTOR)
            ))
            _n_buy = max(1, min(_target_n, _n_buy))

        # CAT-backed sell capacity at the trial base size.
        _n_sell = _target_n
        if mid_price and mid_price > 0 and _avail_cat > 0 and _base_size > 0:
            _cat_base = _base_size / mid_price
            if _cat_base > 0:
                _cat_units_avail = _avail_cat / _cat_base
                _n_sell = max(0, int(
                    (_cat_units_avail / _CP_HEADROOM_MULT - _SPARE_OVERHEAD)
                    / max(1e-9, _TIER_CAPITAL_FACTOR)
                ))
        elif _avail_cat <= 0:
            _n_sell = 0

        # Symmetric — same depth on both sides
        _n_final = max(1, min(_n_buy, _n_sell)) if _n_sell > 0 else max(1, _n_buy)

        # Recalculate base size with agreed n (uses the binding constraint).
        _base_size = max(_MIN_OFFER_XCH, round(_solve_base(_n_final), 4))

        # Re-check CAT capacity against the definitive final base_size.
        if mid_price and mid_price > 0 and _base_size > 0:
            _cat_per_offer_final = _base_size / mid_price
            if _cat_per_offer_final > 0:
                _cat_units_avail = _avail_cat / _cat_per_offer_final
                _n_sell = max(0, int(
                    (_cat_units_avail / _CP_HEADROOM_MULT - _SPARE_OVERHEAD)
                    / max(1e-9, _TIER_CAPITAL_FACTOR)
                ))
        _n_sell_cap = _n_sell  # definitive CAT-backed sell capacity

        # Distribute offers across tiers; mid absorbs rounding remainder
        _n_inner   = max(1, round(_n_final * _count_dist[0]))
        _n_outer   = (max(0, round(_n_final * _count_dist[2]))
                      if _n_final >= 4 and _max_tiers >= 3 else 0)
        _n_extreme = (max(0, round(_n_final * _count_dist[3]))
                      if _n_final >= 5 and _max_tiers == 4 else 0)
        _n_mid     = max(1, _n_final - _n_inner - _n_outer - _n_extreme)

        _smart_trade_size = _base_size
        _smart_max_buy    = _n_final
        _smart_max_sell   = _n_final
        _smart_n_inner    = _n_inner
        _smart_n_mid      = _n_mid
        _smart_n_outer    = _n_outer
        _smart_n_extreme  = _n_extreme
        _smart_inner   = round(_base_size * _size_mults[0], 4)
        _smart_mid     = round(_base_size * _size_mults[1], 4)
        _smart_outer   = round(_base_size * _size_mults[2], 4) if _max_tiers >= 3 else 0.0
        _smart_extreme = round(_base_size * _size_mults[3], 4) if _max_tiers == 4 else 0.0

        # ── RISK PROFILE: offer count + tier sizes ─────────────────────────
        # max_offers_mult controls how many offers the bot maintains.
        # It is applied to both the capital-plan count (_n_final base) AND the
        # config value so coin prep, spares, and active-offer limits are aligned.
        # capital_mult already shrank _trading_xch → smaller per-offer sizes.
        # Combining both: conservative = fewer AND smaller offers.
        if _rp["max_offers_mult"] != 1.0:
            _adj_n          = max(1, round(_n_final * _rp["max_offers_mult"]))
            _smart_max_buy  = _adj_n
            _smart_max_sell = _adj_n
            # Re-derive tier counts from the adjusted total
            _smart_n_inner  = max(1, round(_adj_n * _count_dist[0]))
            _smart_n_outer  = (max(0, round(_adj_n * _count_dist[2]))
                               if _adj_n >= 4 and _max_tiers >= 3 else 0)
            _smart_n_extreme = (max(0, round(_adj_n * _count_dist[3]))
                                if _adj_n >= 5 and _max_tiers == 4 else 0)
            _smart_n_mid    = max(1, _adj_n - _smart_n_inner - _smart_n_outer - _smart_n_extreme)
        if _rp["inner_tier_mult"] != 1.0:
            # Inner has its own mult — most-filled tier, most impact on capital use.
            # Cap at what the remaining trading XCH can actually fund for inner slots.
            _inner_cap = round(_trading_xch / max(1, _smart_n_inner), 4)
            _smart_inner = min(_inner_cap, round(_smart_inner * _rp["inner_tier_mult"], 4))
        if _rp["tier_size_mult"] != 1.0:
            # Mid/outer/extreme scaled together — inner already handled above.
            # Floor at _MIN_OFFER_XCH so we don't create unplaceable offers.
            _tsm = _rp["tier_size_mult"]
            _smart_mid     = max(_MIN_OFFER_XCH, round(_smart_mid     * _tsm, 4)) if _smart_mid     > 0 else 0.0
            _smart_outer   = max(_MIN_OFFER_XCH, round(_smart_outer   * _tsm, 4)) if _smart_outer   > 0 else 0.0
            _smart_extreme = max(_MIN_OFFER_XCH, round(_smart_extreme * _tsm, 4)) if _smart_extreme > 0 else 0.0

        # F63 (2026-04-10): apply price shock spare floor now that tier
        # counts are finalized. Each tier gets at least ceil(live×0.5) spares
        # so the rolling wave requote can start its first batch immediately.
        if _apply_shock_floor:
            _pre_shock = (_spare_inner, _spare_mid, _spare_outer, _spare_extreme)
            _spare_inner   = max(_spare_inner,   _math_spare.ceil(_smart_n_inner   * 0.5)) if _smart_n_inner   > 0 else _spare_inner
            _spare_mid     = max(_spare_mid,     _math_spare.ceil(_smart_n_mid     * 0.5)) if _smart_n_mid     > 0 else _spare_mid
            _spare_outer   = max(_spare_outer,   _math_spare.ceil(_smart_n_outer   * 0.5)) if _smart_n_outer   > 0 else _spare_outer
            _spare_extreme = max(_spare_extreme, _math_spare.ceil(_smart_n_extreme * 0.5)) if _smart_n_extreme > 0 else _spare_extreme
            _post_shock = (_spare_inner, _spare_mid, _spare_outer, _spare_extreme)
            if _pre_shock != _post_shock:
                print(f"[SMART_DEFAULTS] Price shock spare floor applied: "
                      f"inner {_pre_shock[0]}->{_post_shock[0]}, "
                      f"mid {_pre_shock[1]}->{_post_shock[1]}, "
                      f"outer {_pre_shock[2]}->{_post_shock[2]}, "
                      f"extreme {_pre_shock[3]}->{_post_shock[3]}")

        # Hard cap _smart_max_sell to what the available CAT can actually fund.
        # Risk-profile adjustments (max_offers_mult) may have pushed _smart_max_sell
        # above _n_sell_cap; clamp it back down.  _n_sell_cap was computed against
        # the final base_size so this is the definitive, accurate limit.
        if _smart_max_sell > _n_sell_cap:
            _smart_max_sell = _n_sell_cap

        # ── PRICE SHOCK SPARE BUFFER (Fix G, 2026-04-10) ──────────────────
        # During a price shock the entire one-side ladder gets requoted.
        # The rolling wave needs at least 50% of each tier's live count
        # available as spare coins so replacement offers can start creating
        # immediately without waiting for coin prep.  Take the MAX of
        # (fill-rate spares, price-shock spares) for each tier.
        import math as _math_g
        _shock_inner   = _math_g.ceil(_smart_n_inner   * 0.5)
        _shock_mid     = _math_g.ceil(_smart_n_mid     * 0.5)
        _shock_outer   = _math_g.ceil(_smart_n_outer   * 0.5)
        _shock_extreme = _math_g.ceil(_smart_n_extreme * 0.5)
        if _shock_inner > _spare_inner:
            log_event("info", "smart_spare_shock_buffer",
                      f"Spare inner raised from {_spare_inner} to "
                      f"{_shock_inner} for price shock resilience")
            _spare_inner = _shock_inner
        if _shock_mid > _spare_mid:
            log_event("info", "smart_spare_shock_buffer",
                      f"Spare mid raised from {_spare_mid} to "
                      f"{_shock_mid} for price shock resilience")
            _spare_mid = _shock_mid
        if _shock_outer > _spare_outer and _smart_n_outer > 0:
            log_event("info", "smart_spare_shock_buffer",
                      f"Spare outer raised from {_spare_outer} to "
                      f"{_shock_outer} for price shock resilience")
            _spare_outer = _shock_outer
        if _shock_extreme > _spare_extreme and _smart_n_extreme > 0:
            log_event("info", "smart_spare_shock_buffer",
                      f"Spare extreme raised from {_spare_extreme} to "
                      f"{_shock_extreme} for price shock resilience")
            _spare_extreme = _shock_extreme

        # ── COIN-PREP CAT FEASIBILITY CLAMP ────────────────────────────────
        # The capital-plan solver may have been run when the token balance was
        # higher (or the price was higher), so the recommended tier sizes can
        # exceed what the current CAT balance can actually support for coin
        # prep.  Replicate the JS formula exactly so the settings we emit are
        # guaranteed to pass the coin-prep feasibility check in the frontend:
        #
        #   totalCatForCoinPrep = sum_tier:
        #       (n_live + n_spare) × round(tier_size_xch / mid_price × headroom_mult)
        #
        # If the total exceeds avail_cat, scale ALL tier sizes down by the
        # ratio (avail_cat / total), then re-floor at _MIN_OFFER_XCH.
        if mid_price and mid_price > 0 and _avail_cat > 0:
            _cp_hm = 1.0 + (coin_prep_headroom_pct / 100.0)
            _tier_live_spare_size = [
                (_smart_n_inner,   _spare_inner,   _smart_inner),
                (_smart_n_mid,     _spare_mid,     _smart_mid),
            ]
            if _max_tiers >= 3 and _smart_outer > 0:
                _tier_live_spare_size.append((_smart_n_outer, _spare_outer, _smart_outer))
            if _max_tiers >= 4 and _smart_extreme > 0:
                _tier_live_spare_size.append((_smart_n_extreme, _spare_extreme, _smart_extreme))
            # Use the actual configured spares the frontend will build with.
            # PREVIOUSLY this also took max() against (live × 3) as a "what if
            # the user is on the recommended 2:1 spare template" defensive
            # check, but that doubled the CAT requirement and triggered a
            # ~50% scale-down even when the user had a much smaller custom
            # spare template (e.g. 11 spares for 24 live). The recommended
            # spare snap already lives in the frontend Recommended button —
            # if the user clicks it the spare counts arrive here updated, so
            # the defensive max is never needed.
            _total_cat_prep = sum(
                (_nl + _ns) * round((_sx / mid_price) * _cp_hm)
                for _nl, _ns, _sx in _tier_live_spare_size
            )
            # F55 (2026-04-09): the frontend's coin-prep total includes
            # sniper CAT and the topup-pool CAT alongside the trading
            # tiers. If we let trading tiers eat 85% of avail_cat, and
            # then sniper + topup take their share on top, the frontend
            # sum can exceed avail_cat and fire a critical "Coin prep
            # impossible" warning even though Smart Settings just signed
            # off. Carve those holders OUT of the budget here so the
            # trading-tier clamp leaves room for them.
            _sniper_cat_prep = (
                round((_smart_sniper_size / mid_price) * _cp_hm) * _smart_sniper_prep
                if mid_price > 0 else 0
            )
            _topup_cat_prep = round(_avail_cat * _TOPUP_BUFFER_PCT)
            _cat_budget = max(
                0.0,
                _avail_cat * 0.85 - _sniper_cat_prep - _topup_cat_prep
            )
            if _total_cat_prep > _cat_budget:
                _cat_scale = _cat_budget / _total_cat_prep   # < 1.0
                _pre_scale_inner = _smart_inner
                _smart_inner   = max(_MIN_OFFER_XCH, round(_smart_inner   * _cat_scale, 4))
                _smart_mid     = max(_MIN_OFFER_XCH, round(_smart_mid     * _cat_scale, 4))
                _smart_outer   = (max(_MIN_OFFER_XCH, round(_smart_outer   * _cat_scale, 4))
                                  if _smart_outer   > 0 else 0.0)
                _smart_extreme = (max(_MIN_OFFER_XCH, round(_smart_extreme * _cat_scale, 4))
                                  if _smart_extreme > 0 else 0.0)
                # _base_size is the reference "mid" size from which all tiers derive.
                # Scale it by the same ratio so the multiplier structure is preserved.
                _base_size = round(_base_size * _cat_scale, 4)
                _smart_trade_size = _base_size
                messages.append(
                    f"Sell offer sizes scaled down {(1-_cat_scale)*100:.0f}% "
                    f"({_pre_scale_inner:.4f} → {_smart_inner:.4f} XCH inner) "
                    f"so the {_smart_max_sell} sell offers fit your CAT balance. "
                    f"Coin prep would have needed ~{_total_cat_prep:,.0f} tokens, "
                    f"budget is {_cat_budget:,.0f} (85% of {_avail_cat:,.0f} available)."
                )
                print(f"[SMART_DEFAULTS] CAT prep clamp triggered: scale={_cat_scale:.3f}, "
                      f"was {_total_cat_prep:,.0f} tokens, budget {_cat_budget:,.0f} "
                      f"(85% of {_avail_cat:,.0f} avail)")
        # ── END COIN-PREP CAT FEASIBILITY CLAMP ────────────────────────────

        _cat_limited = bool(_n_sell_cap < _n_buy and mid_price and mid_price > 0)
        _strategy = (
            f"{_tier_style} {_max_tiers}-tier ladder · "
            + (f"{_smart_max_buy}B/{_smart_max_sell}S offers"
               if _cat_limited else f"{_n_final} offers/side")
            + f" · {_trading_xch:.2f} XCH trading ({_trading_pct:.0f}%)"
            + (f" · {_pool_note}" if _pool_note else "")
        )
        _tier_label_full = (
            f"{_tier_style} · {_max_tiers} tiers" + (f" · {_pool_note}" if _pool_note else "")
        )
        # Topup buffer: XCH NOT deployed into active offers.
        # This is the "topup pool" — large unbroken coins the topup worker
        # splits when a tier runs short.  We ALREADY carved out
        # _topup_buffer_reserve before computing _trading_xch, so the unused
        # XCH here equals (avail − fees − sniper − trading), which is the
        # reserved buffer plus any rounding crumbs.
        _topup_buffer_xch = max(0.0, round(
            _avail_xch - _fee_pool_xch - _sniper_pool_xch - _trading_xch, 4))
        _largest_tier_xch = max(
            _smart_inner if _smart_inner > 0 else 0.0,
            _smart_mid   if _smart_mid   > 0 else 0.0,
            _smart_outer if _smart_outer > 0 else 0.0,
            _smart_extreme if _smart_extreme > 0 else 0.0,
            float(_MIN_OFFER_XCH),
        )
        # Aim for ≥2× the largest tier so the topup worker can split a full
        # replacement coin AND still have something to feed into the next
        # split.  If the 10% reservation isn't enough, top it up by reducing
        # _trading_xch (we already know base sizes — we just shrink the
        # pool, not the per-offer sizes, since the formula is fixed).
        _target_buffer = round(max(_topup_buffer_xch, _largest_tier_xch * 2), 4)
        # Don't let the buffer eat the entire trading budget — cap at 25% of
        # post-pools XCH so we always keep most capital working.
        _max_buffer_allowed = round(_post_pools_xch * 0.25, 4)
        if _target_buffer > _max_buffer_allowed:
            _target_buffer = _max_buffer_allowed
        if _target_buffer > _topup_buffer_xch:
            _extra_needed = round(_target_buffer - _topup_buffer_xch, 4)
            if _extra_needed > 0 and _trading_xch > _extra_needed:
                _trading_xch = round(_trading_xch - _extra_needed, 4)
                _topup_buffer_xch = round(_topup_buffer_xch + _extra_needed, 4)
                _trading_pct = round(_trading_xch / _avail_xch * 100, 1) if _avail_xch > 0 else 0.0
        _topup_buffer_adequate = _topup_buffer_xch >= _largest_tier_xch * 2
        if _topup_buffer_adequate:
            messages.append(
                f"Topup buffer: {_topup_buffer_xch:.2f} XCH retained for reserve coin splits "
                f"(≥2× {_largest_tier_xch:.4f} largest tier ✓)"
            )
        else:
            messages.append(
                f"Topup buffer: {_topup_buffer_xch:.2f} XCH — low for reserve splits "
                f"(needs ≥{_largest_tier_xch * 2:.2f} XCH to reliably replenish "
                f"the largest tier). Consider reducing offer counts or tier sizes."
            )

        _capital_plan = {
            "total_xch":             round(xch_spendable, 4),
            "xch_reserve":           _xch_reserve,
            "cat_reserve":           _cat_reserve,
            "available_xch":         round(_avail_xch, 4),
            "available_cat":         round(_avail_cat, 2),
            "fee_pool_xch":          round(_fee_pool_xch, 4),
            "fee_pct":               round(_FEE_PCT * 100, 1),
            "sniper_pool_xch":       round(_sniper_pool_xch, 4),
            "sniper_pct":            round(_SNIPER_PCT * 100, 1),
            "trading_xch":           round(_trading_xch, 4),
            "trading_pct":           _trading_pct,
            "topup_buffer_xch":      _topup_buffer_xch,
            "topup_buffer_adequate": _topup_buffer_adequate,
            "largest_tier_xch":      round(_largest_tier_xch, 4),
            "n_final":               _n_final,
            "base_size":             _base_size,
            "max_tiers":             _max_tiers,
            "tier_label":            _tier_label_full,
            "strategy":              _strategy,
            "n_sell_limited_by_cat": _cat_limited,
        }
        messages.append(f"Strategy: {_strategy}")
        _tier_msg = (
            f"Tiers: inner {_n_inner}×{_smart_inner:.4f}"
            f" / mid {_n_mid}×{_smart_mid:.4f}"
        )
        if _n_outer > 0:
            _tier_msg += f" / outer {_n_outer}×{_smart_outer:.4f}"
        if _n_extreme > 0:
            _tier_msg += f" / extreme {_n_extreme}×{_smart_extreme:.4f}"
        messages.append(_tier_msg + " XCH")
        if _cat_limited_trading and _cat_xch_equiv is not None:
            # F55 (2026-04-09): drop the duplicate "X XCH stays in topup buffer"
            # number — it was computed differently from `_topup_buffer_xch`
            # above and produced a contradictory second figure in the same
            # log. The single source of truth is the "Topup buffer:" message
            # at line 7370 (uses _topup_buffer_xch — the final adjusted value).
            messages.append(
                f"CAT balance ({_avail_cat:.0f} tokens ≈ {_cat_xch_equiv:.2f} XCH) is smaller than XCH "
                f"trading budget — offer sizes matched to CAT value. "
                f"Unused XCH is held in the topup buffer above."
            )
    else:
        _capital_plan = {
            "total_xch":     round(xch_spendable, 4),
            "xch_reserve":   _xch_reserve,
            "cat_reserve":   _cat_reserve,
            "available_xch": round(_avail_xch, 4),
            "available_cat": round(_avail_cat, 2),
            "insufficient":  True,
        }
        if _avail_xch > 0:
            messages.append(
                f"Capital: {_avail_xch:.4f} XCH after reserve — "
                f"need at least {_MIN_OFFER_XCH * 2:.3f} XCH trading capital"
            )
        else:
            messages.append("Capital: no XCH available after reserve")

    # ═══ COIN PREP MULTIPLIER — recalculated from capital plan ═══
    # Now we have the capital plan values (_smart_trade_size, _smart_max_buy/sell,
    # _avail_xch, _avail_cat) so we can compute a meaningful multiplier.
    # The multiplier = how many times over the live ladder we can afford to pre-prep.
    # e.g. multiplier=1.0 means you have exactly enough capital to cover one live
    # ladder's worth of prepared coins; 2.0 means two layers; 0.5 means only half.
    coin_prep_multiplier = 1.0
    if _smart_trade_size > 0 and _smart_max_buy > 0 and _avail_xch > 0:
        # Compute tier-weighted XCH needed (not just flat trade_size × max_buy).
        # Inner coins are 2× trade size, mid=1×, outer=0.5×, extreme=0.2×.
        # Without tier weighting, the formula massively under-estimates and
        # produces a multiplier that is far higher than the wallet can sustain.
        try:
            from coin_manager import get_tier_distribution as _gtd_sd
            _sd_dist = _gtd_sd(_smart_max_buy)
            # Use the freshly-calculated smart sizes, NOT stale cfg values.
            # cfg.INNER_SIZE_XCH etc. still hold whatever was in .env before
            # Smart Defaults ran — reading them here produced multipliers
            # calculated against the OLD (larger) offer sizes, not the new ones.
            _tier_smart_sizes = {
                "inner":   _smart_inner   if _smart_inner   > 0 else _smart_trade_size * 2.0,
                "mid":     _smart_mid     if _smart_mid     > 0 else _smart_trade_size * 1.0,
                "outer":   _smart_outer   if _smart_outer   > 0 else _smart_trade_size * 0.5,
                "extreme": _smart_extreme if _smart_extreme > 0 else _smart_trade_size * 0.2,
            }
            _cp_xch_needed = 0.0
            for _st, _sc in _sd_dist.items():
                _st_size = _tier_smart_sizes.get(_st, _smart_trade_size * 0.2)
                _cp_xch_needed += _st_size * _sc * 2  # × 2 for both sides (buy + sell)
            _cp_xch_needed *= (1 + coin_prep_headroom_pct / 100.0)
            if _cp_xch_needed <= 0:
                _cp_xch_needed = _smart_trade_size * _smart_max_buy
        except Exception:
            _cp_xch_needed = _smart_trade_size * _smart_max_buy

        _cp_cat_needed = 0.0
        if _smart_trade_size > 0 and _smart_max_sell > 0 and mid_price > 0:
            _cp_headroom_mult = 1 + (coin_prep_headroom_pct / 100.0)
            _cp_cat_needed = (_smart_trade_size / mid_price) * _cp_headroom_mult * _smart_max_sell

        _cp_xch_mult = min(3.0, _avail_xch / _cp_xch_needed)
        _cp_cat_mult = 3.0
        if _cp_cat_needed > 0 and _avail_cat > 0:
            _cp_cat_mult = min(3.0, _avail_cat / _cp_cat_needed)

        _cp_raw = min(_cp_xch_mult, _cp_cat_mult)
        # Round to nearest 0.5, floor at 1.0 (never under-prep spares),
        # cap at 2.5 (beyond this the prep time exceeds practical benefit).
        coin_prep_multiplier = max(1.0, min(2.5, int(_cp_raw * 2) / 2.0))
        # ── RISK PROFILE: coin prep adjustment (additive, then re-round) ──
        if _rp["coin_prep_adj"] != 0.0:
            coin_prep_multiplier = max(1.0, min(2.5,
                round((coin_prep_multiplier + _rp["coin_prep_adj"]) * 2) / 2.0
            ))
        print(f"[SMART_DEFAULTS v2] Coin prep multiplier: {coin_prep_multiplier} "
              f"(xch_mult={_cp_xch_mult:.2f}, cat_mult={_cp_cat_mult:.2f}, "
              f"tier_xch_needed={_cp_xch_needed:.2f}, profile={_risk_profile_name})")
    else:
        # No capital plan (insufficient funds) — use floor minimum
        coin_prep_multiplier = 1.0

    # ── Micro-wallet spread floor ──
    # For small trading capital, each blockchain tx fee is a significant % of each
    # fill. Widen the spread floor to ensure fees are always covered.
    # Thresholds are percentage-based — they scale naturally with any wallet size.
    if _avail_xch > 0 and _trading_xch > 0:
        if _trading_xch < 0.05:
            # Micro: < 0.05 XCH trading capital — fees eat virtually any fill
            _capital_spread_floor = 800  # 8% minimum
            if base_spread_bps < _capital_spread_floor:
                base_spread_bps = _capital_spread_floor
                messages.append(
                    f"Spread raised to {_bps_to_pct(_capital_spread_floor)} "
                    f"(micro wallet — {_trading_xch:.4f} XCH trading capital, "
                    f"fees must be covered by spread)"
                )
        elif _trading_xch < 0.2:
            # Small: < 0.2 XCH trading capital — fees are a high % of each fill
            _capital_spread_floor = 600  # 6% minimum
            if base_spread_bps < _capital_spread_floor:
                base_spread_bps = _capital_spread_floor
                messages.append(
                    f"Spread raised to {_bps_to_pct(_capital_spread_floor)} "
                    f"(small wallet — {_trading_xch:.4f} XCH trading capital)"
                )

    # ═══ BUY-SIDE REVERSAL (reverse-buy ladder only) ═════════════════════
    # Smart Settings computes _smart_n_*/_spare_* in SIZE-indexed semantics:
    # the largest count goes on the largest coin size (size_inner). That is
    # correct for the SELL side (slot inner = inner SIZE = most-active slot
    # = should have the most offers). The buy side uses the SAME position-
    # indexed count distribution as sell (inner=most, extreme=fewest)
    # regardless of BUY_LADDER_REVERSED. The reversal is in the SIZES
    # (inner position gets smallest size under reverse-buy), not the counts.
    # The launcher's _flip_tiers handles the position→size mapping for
    # coin prep, so putting the highest count at position inner (smallest
    # size) gives the correct capital allocation: many small coins, few
    # large coins.
    _buy_n_inner   = _smart_n_inner
    _buy_n_mid     = _smart_n_mid
    _buy_n_outer   = _smart_n_outer
    _buy_n_extreme = _smart_n_extreme
    _buy_spare_inner   = _spare_inner
    _buy_spare_mid     = _spare_mid
    _buy_spare_outer   = _spare_outer
    _buy_spare_extreme = _spare_extreme

    # ═══ HARD FEASIBILITY CHECK (mirror the launcher's pool formula) ═════
    # Compute the exact XCH pool the launcher will request, accounting for
    # the frontend swap + launcher flip + coin-size sizing. If the proposed
    # ladder doesn't fit the wallet's actual buy budget, scale ALL tier
    # sizes down so the launcher never has to invoke its emergency
    # auto-scaler (which silently drops tiers and confuses the user).
    #
    # The launcher's effective formula (after frontend swap → env → _flip_tiers)
    # collapses to: pool = sum_i((live[size_i] + spare[size_i]) × size_xch[i]) × headroom
    # where size_xch[i] = base_size × size_mults[i] = _smart_inner/_smart_mid/etc.
    if _smart_inner > 0:
        # Position counts as the env will hold them (no frontend swap —
        # values go directly from API response → form inputs → .env).
        _env_buy_inner   = _buy_n_inner   + _buy_spare_inner
        _env_buy_mid     = _buy_n_mid     + _buy_spare_mid
        _env_buy_outer   = _buy_n_outer   + _buy_spare_outer
        _env_buy_extreme = _buy_n_extreme + _buy_spare_extreme
        # Launcher flip: position → coin SIZE
        if _buy_ladder_reversed:
            _size_inner_total   = _env_buy_extreme   # slot extreme uses inner size
            _size_mid_total     = _env_buy_outer
            _size_outer_total   = _env_buy_mid
            _size_extreme_total = _env_buy_inner
        else:
            _size_inner_total   = _env_buy_inner
            _size_mid_total     = _env_buy_mid
            _size_outer_total   = _env_buy_outer
            _size_extreme_total = _env_buy_extreme
        _buy_pool_xch = (
            _size_inner_total   * _smart_inner
            + _size_mid_total     * _smart_mid
            + _size_outer_total   * _smart_outer
            + _size_extreme_total * _smart_extreme
        ) * _CP_HEADROOM_MULT
        # Budget the launcher will see: avail XCH minus the carve-outs
        # (fee pool + sniper pool + topup buffer).
        # F55 (2026-04-09): the topup buffer must be excluded from the
        # launcher budget — it is XCH HELD as unsplit reserve coins, not
        # capital available for trading-tier coin prep. Previously the
        # 10% topup buffer wasn't subtracted, so the launcher tried to
        # prep tier coins worth 95% of post_pools_xch on top of holding
        # the 10% topup buffer = 105% of post_pools_xch total. The
        # frontend's coin-prep preview correctly summed the parts and
        # threw a "Coin prep impossible" critical warning even though
        # Smart Settings claimed the plan fit.
        _launcher_buy_budget = max(0.0,
            _avail_xch - _fee_pool_xch - _sniper_pool_xch - _topup_buffer_xch)
        # F57 (2026-04-09): reduced from 5% to 2% safety margin so Smart
        # Settings can deploy more of the wallet's actual capacity. The 5%
        # margin was over-conservative — `_avail_xch` already excludes the
        # user's reserve and the topup buffer, both of which absorb any
        # transient locked-coin / rounding noise. The 2% margin is enough
        # to handle wallet RPC quantization at the mojo level.
        _launcher_buy_budget *= 0.98
        if _buy_pool_xch > _launcher_buy_budget and _buy_pool_xch > 0:
            _buy_scale = _launcher_buy_budget / _buy_pool_xch
            _pre_inner = _smart_inner
            _smart_inner   = max(_MIN_OFFER_XCH, round(_smart_inner   * _buy_scale, 4))
            _smart_mid     = max(_MIN_OFFER_XCH, round(_smart_mid     * _buy_scale, 4))
            _smart_outer   = (max(_MIN_OFFER_XCH, round(_smart_outer   * _buy_scale, 4))
                              if _smart_outer   > 0 else 0.0)
            _smart_extreme = (max(_MIN_OFFER_XCH, round(_smart_extreme * _buy_scale, 4))
                              if _smart_extreme > 0 else 0.0)
            _smart_trade_size = round(_smart_trade_size * _buy_scale, 4)
            messages.append(
                f"Buy ladder sizes scaled down {(1-_buy_scale)*100:.0f}% "
                f"({_pre_inner:.4f} → {_smart_inner:.4f} XCH inner) "
                f"so coin prep + topup buffer fits the wallet. "
                f"Unscaled buy pool would have been {_buy_pool_xch:.2f} XCH; "
                f"budget after fees, sniper and topup buffer is {_launcher_buy_budget:.2f} XCH."
            )
            print(f"[SMART_DEFAULTS] Buy pool clamp triggered: scale={_buy_scale:.3f}, "
                  f"was {_buy_pool_xch:.2f} XCH, budget {_launcher_buy_budget:.2f} XCH "
                  f"(reverse-buy={_buy_ladder_reversed})")

    # ═══ TIER-AWARE "TIGHT ALLOCATION" GUARD (F57 2026-04-09) ════════════
    # The GUI's checkReserveWarnings() now (post-F57) computes the SUM of
    # tier_count × tier_size across all four tiers — the true XCH the buy
    # ladder will lock — and warns if `tier_sum + reserve > 0.9 × balance`.
    #
    # Smart Settings has to keep the same total below that bar, otherwise
    # the warning fires the moment the GUI form re-validates after Smart
    # Settings populates it. Anchor against the ACTUAL ladder sum (not the
    # base × max_buy flat overcount that the previous F56 used) so we can
    # use the wallet's full capacity instead of leaving 30%+ idle.
    #
    # Reserve-percentage handling:
    #   • 0%   → 90%   proportional clamp using the actual tier sum;
    #                  trade size always stays positive
    #   • ≥ 90%        math is impossible (any offer trips the warning) →
    #                  drop buy offers entirely so the warning is correctly
    #                  suppressed (only fires when max_buy > 0)
    if (xch_spendable > 0 and _smart_max_buy > 0
            and "_smart_trade_size" in dir() and _smart_trade_size > 0):
        _tier_warning_budget = max(0.0, 0.9 * xch_spendable - _xch_reserve)
        # 2% safety margin — leaves room for rounding noise after the GUI
        # populates the form. The previous 12% margin was over-conservative
        # because it assumed the GUI used the flat overcount; with F57 the
        # GUI uses the same actual sum we compute here, so we can target
        # much closer to the true threshold.
        _safe_tier_budget = _tier_warning_budget * 0.98

        # F57c (2026-04-09): compute the BUY-side live ladder XCH.
        #
        # The clamp exists to keep the GUI "Tight allocation" warning quiet.
        # That warning checks buy_ladder + xch_reserve ≤ 0.9 × balance — so
        # we need the BUY-side sum here, not the sell-side sum.
        #
        # Under reverse-buy, the buy ladder is NOT symmetric with the sell
        # ladder in XCH terms — the buy side uses MANY small coins at the
        # most-active positions (position inner/mid) and FEW large coins
        # at the least-active positions (position extreme). That means the
        # total XCH is LESS than the sell side's CAT-equivalent.
        #
        # Correct pairing under reverse-buy:
        #   position inner (count = 42%)  uses SIZE extreme  (smallest coin)
        #   position mid   (count = 30%)  uses SIZE outer
        #   position outer (count = 20%)  uses SIZE mid
        #   position extreme (count = 8%) uses SIZE inner    (largest coin)
        #
        # Without reverse-buy the pairing is the identity (position inner
        # uses size inner, etc.) so the two branches give the same sum.
        #
        # Previous versions of this clamp used the sell-side sum
        # (_smart_n_inner × _smart_inner + ...) as if it were the buy sum.
        # That made it clamp at a number roughly 2× larger than the real
        # buy capacity on typical tokens, which starved the ladder of ~15
        # XCH of avail capital under reverse-buy.
        if _buy_ladder_reversed:
            _ladder_sum = (
                _smart_n_inner   * _smart_extreme +  # pos inner × size extreme
                _smart_n_mid     * _smart_outer   +  # pos mid   × size outer
                _smart_n_outer   * _smart_mid     +  # pos outer × size mid
                _smart_n_extreme * _smart_inner      # pos extreme × size inner
            )
        else:
            _ladder_sum = (
                _smart_n_inner   * _smart_inner   +
                _smart_n_mid     * _smart_mid     +
                _smart_n_outer   * _smart_outer   +
                _smart_n_extreme * _smart_extreme
            )

        # Practical floor: a buy offer below this XCH is below Dexie's display
        # threshold and not worth a taker's fee.
        _PRACTICAL_MIN_BASE = max(_MIN_OFFER_XCH * 2, 0.01)

        if _tier_warning_budget <= 0:
            # Reserve is at or above 90% of the wallet — no positive trade
            # size lets the warning stay quiet. Drop buy offers entirely so
            # the warning correctly does not fire (it only checks max_buy > 0).
            _pre_max_buy = _smart_max_buy
            _smart_max_buy = 0
            messages.append(
                f"Reserve ({_xch_reserve:.2f} XCH) is ≥ 90% of total wallet. "
                f"Buy offers dropped to 0 — the wallet has no headroom for "
                f"buy-side allocation. Lower the reserve to enable buying."
            )
            print(f"[SMART_DEFAULTS] Reserve ≥ 90% of wallet: dropped buy offers "
                  f"({_pre_max_buy} → 0). reserve={_xch_reserve:.2f}, "
                  f"xch_spendable={xch_spendable:.2f}")
        elif _ladder_sum > _safe_tier_budget and _safe_tier_budget > 0 and _ladder_sum > 0:
            _tier_scale = _safe_tier_budget / _ladder_sum
            _pre_tier_inner = _smart_inner if _smart_inner > 0 else _smart_trade_size
            _new_trade_size = round(_smart_trade_size * _tier_scale, 4)

            # If the proposed trade size would push the inner tier below the
            # practical floor, reduce max_buy instead so the remaining offers
            # can stay at a usable size.
            if _new_trade_size < _PRACTICAL_MIN_BASE and _PRACTICAL_MIN_BASE > 0:
                _new_max_buy = max(1, int(_safe_tier_budget / _PRACTICAL_MIN_BASE))
                _pre_max_buy = _smart_max_buy
                _smart_max_buy = _new_max_buy
                _smart_trade_size = round(_PRACTICAL_MIN_BASE, 4)
                if _smart_inner   > 0: _smart_inner   = round(_smart_trade_size * 1.5, 4)
                if _smart_mid     > 0: _smart_mid     = round(_smart_trade_size * 1.0, 4)
                if _smart_outer   > 0: _smart_outer   = round(_smart_trade_size * 0.5, 4)
                if _smart_extreme > 0: _smart_extreme = round(_smart_trade_size * 0.2, 4)
                messages.append(
                    f"High reserve ({_xch_reserve:.2f} XCH) — buy offers reduced "
                    f"from {_pre_max_buy} to {_smart_max_buy} so each remaining "
                    f"offer stays at a practical {_smart_trade_size:.4f} XCH."
                )
                print(f"[SMART_DEFAULTS] Tier-sum clamp (max_buy reduction): "
                      f"max_buy {_pre_max_buy}→{_smart_max_buy}, "
                      f"trade_size→{_smart_trade_size:.4f} XCH, "
                      f"safe_budget={_safe_tier_budget:.2f} XCH")
            else:
                _smart_trade_size = _new_trade_size
                if _smart_inner   > 0: _smart_inner   = max(_MIN_OFFER_XCH, round(_smart_inner   * _tier_scale, 4))
                if _smart_mid     > 0: _smart_mid     = max(_MIN_OFFER_XCH, round(_smart_mid     * _tier_scale, 4))
                if _smart_outer   > 0: _smart_outer   = max(_MIN_OFFER_XCH, round(_smart_outer   * _tier_scale, 4))
                if _smart_extreme > 0: _smart_extreme = max(_MIN_OFFER_XCH, round(_smart_extreme * _tier_scale, 4))
                messages.append(
                    f"Tier sizes scaled {(1-_tier_scale)*100:.0f}% "
                    f"({_pre_tier_inner:.4f} → {_smart_inner:.4f} XCH inner) "
                    f"so the buy ladder + reserve stays under 90% of wallet."
                )
                print(f"[SMART_DEFAULTS] Tier-sum clamp: scale={_tier_scale:.3f}, "
                      f"ladder_sum={_ladder_sum:.2f} XCH, safe_budget={_safe_tier_budget:.2f} XCH "
                      f"(0.98 × ({0.9*xch_spendable:.2f} − {_xch_reserve:.2f}))")

            # Recompute trading_pct for the response to reflect the trim.
            # trading_xch = the ACTUAL buy-side ladder sum (reverse-buy aware).
            try:
                if _buy_ladder_reversed:
                    _trading_xch = round(
                        _smart_n_inner   * _smart_extreme +
                        _smart_n_mid     * _smart_outer   +
                        _smart_n_outer   * _smart_mid     +
                        _smart_n_extreme * _smart_inner,
                        4)
                else:
                    _trading_xch = round(
                        _smart_n_inner   * _smart_inner   +
                        _smart_n_mid     * _smart_mid     +
                        _smart_n_outer   * _smart_outer   +
                        _smart_n_extreme * _smart_extreme,
                        4)
                _trading_pct = round(_trading_xch / _avail_xch * 100, 1) if _avail_xch > 0 else 0.0
                if "_capital_plan" in dir() and isinstance(_capital_plan, dict):
                    _capital_plan["trading_xch"] = _trading_xch
                    _capital_plan["trading_pct"] = _trading_pct
            except Exception:
                pass
        else:
            # Already inside the budget — just refresh trading_xch in the
            # capital plan to reflect the actual ladder sum (more honest
            # number than the old "_trading_xch as base × max_buy estimate").
            try:
                _trading_xch = round(_ladder_sum, 4)
                _trading_pct = round(_trading_xch / _avail_xch * 100, 1) if _avail_xch > 0 else 0.0
                if "_capital_plan" in dir() and isinstance(_capital_plan, dict):
                    _capital_plan["trading_xch"] = _trading_xch
                    _capital_plan["trading_pct"] = _trading_pct
            except Exception:
                pass

    # ═══ F62 (2026-04-09): PER-SIDE TIER SIZES ════════════════════════════
    # Up to this point `_smart_inner` / `_smart_mid` / `_smart_outer` /
    # `_smart_extreme` have been symmetric values shared between buy and
    # sell ladders (the CAT clamp, tight guard etc. all treat them as one
    # set). Under reverse-buy with a CAT-binding clamp, that symmetric
    # sizing shrinks the buy-side to half its actual capacity and leaves
    # huge amounts of XCH idle in the wallet.
    #
    # F62 runs AFTER all existing clamps complete. It:
    #   1. Treats the existing _smart_* values as the SELL-side output
    #      (which is correct — the CAT clamp sized them for the CAT budget)
    #   2. Computes an INDEPENDENT buy base_size from the saved
    #      _orig_xch_budget so the BUY side fully consumes its own balance
    #   3. Emits position-semantic BUY_*_SIZE_XCH and SELL_*_SIZE_XCH fields
    #      alongside the legacy shared fields (kept in sync with SELL for
    #      backward compat with anything that hasn't been migrated to the
    #      per-side helpers yet)
    #
    # Guards: the HARD FEASIBILITY CHECK and TIGHT ALLOCATION GUARD above
    # both check the buy pool against _smart_inner (sell values). Under
    # F62, _smart_buy_* may be LARGER than _smart_inner, so the guard
    # above was checking a smaller number than the actual buy pool. That
    # means the guard is LENIENT under F62. We must re-verify here with
    # the actual buy sizes before accepting the solution.
    _smart_buy_inner = _smart_inner
    _smart_buy_mid = _smart_mid
    _smart_buy_outer = _smart_outer
    _smart_buy_extreme = _smart_extreme
    _smart_sell_inner = _smart_inner
    _smart_sell_mid = _smart_mid
    _smart_sell_outer = _smart_outer
    _smart_sell_extreme = _smart_extreme
    if (_orig_xch_budget > 0 and _n_final > 0 and _smart_inner > 0
            and _avail_xch > 0):
        # Solve for the biggest base that fits the XCH budget. The existing
        # `_solve_base_xch` uses `count_dist`-based `_BUY_TIER_FACTOR`
        # which is derived from the fractional target distribution
        # (0.42/0.30/0.20/0.08). After rounding to integer counts
        # (15/11/7/3), the ACTUAL ladder uses a slightly different tier
        # factor (0.6167 vs 0.614 for the typical n=36 case). That 0.4%
        # gap propagates to a ~0.17 XCH overshoot vs budget. Solving from
        # the ACTUAL integer counts eliminates the rounding gap and
        # guarantees the prep total stays at or below `_orig_xch_budget`.
        if _buy_ladder_reversed:
            # Reverse-buy: position inner uses smallest mult, etc.
            _buy_live_coeff = (
                _smart_n_inner   * _size_mults[3] +
                _smart_n_mid     * _size_mults[2] +
                _smart_n_outer   * _size_mults[1] +
                _smart_n_extreme * _size_mults[0]
            )
            # Buy spare overhead uses position-semantic counts too
            _buy_spare_coeff = (
                _spare_inner   * _size_mults[3] +
                _spare_mid     * _size_mults[2] +
                _spare_outer   * _size_mults[1] +
                _spare_extreme * _size_mults[0]
            )
        else:
            _buy_live_coeff = (
                _smart_n_inner   * _size_mults[0] +
                _smart_n_mid     * _size_mults[1] +
                _smart_n_outer   * _size_mults[2] +
                _smart_n_extreme * _size_mults[3]
            )
            _buy_spare_coeff = (
                _spare_inner   * _size_mults[0] +
                _spare_mid     * _size_mults[1] +
                _spare_outer   * _size_mults[2] +
                _spare_extreme * _size_mults[3]
            )
        _buy_denom = max(1e-9, (_buy_live_coeff + _buy_spare_coeff) * _CP_HEADROOM_MULT)
        # Apply a tiny safety margin (0.5%) to absorb downstream rounding
        # of individual tier sizes — base × mult gets rounded to 4 dp per
        # tier, and accumulating those errors can still push the total
        # ~0.02 XCH above the theoretical max otherwise.
        _buy_base_max = (_orig_xch_budget * 0.995) / _buy_denom
        _buy_base_max = max(_MIN_OFFER_XCH, _buy_base_max)

        # Position-semantic sizes: under reverse-buy, buy position inner
        # (tightest, closest to mid) uses the SMALLEST multiplier (0.25×),
        # buy position extreme (widest) uses the LARGEST (1.8×). Without
        # reverse-buy it matches the sell layout.
        if _buy_ladder_reversed:
            _smart_buy_inner   = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[3], 4))
            _smart_buy_mid     = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[2], 4))
            _smart_buy_outer   = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[1], 4))
            _smart_buy_extreme = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[0], 4))
        else:
            _smart_buy_inner   = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[0], 4))
            _smart_buy_mid     = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[1], 4))
            _smart_buy_outer   = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[2], 4))
            _smart_buy_extreme = max(_MIN_OFFER_XCH, round(_buy_base_max * _size_mults[3], 4))

        # Sell side: existing _smart_* values (already CAT-clamped correctly)
        _smart_sell_inner   = _smart_inner
        _smart_sell_mid     = _smart_mid
        _smart_sell_outer   = _smart_outer
        _smart_sell_extreme = _smart_extreme

        # Recompute the actual live buy-ladder XCH so trading_xch /
        # trading_pct reflect the TRUE deployment (not the old sell-side
        # ladder sum).
        if _buy_ladder_reversed:
            _buy_live_xch = (
                _smart_n_inner   * _smart_buy_inner   +
                _smart_n_mid     * _smart_buy_mid     +
                _smart_n_outer   * _smart_buy_outer   +
                _smart_n_extreme * _smart_buy_extreme
            )
        else:
            _buy_live_xch = (
                _smart_n_inner   * _smart_buy_inner   +
                _smart_n_mid     * _smart_buy_mid     +
                _smart_n_outer   * _smart_buy_outer   +
                _smart_n_extreme * _smart_buy_extreme
            )
        _trading_xch = round(_buy_live_xch, 4)
        _trading_pct = round(_trading_xch / _avail_xch * 100, 1) if _avail_xch > 0 else 0.0
        if "_capital_plan" in dir() and isinstance(_capital_plan, dict):
            _capital_plan["trading_xch"] = _trading_xch
            _capital_plan["trading_pct"] = _trading_pct
            _capital_plan["buy_base_size"] = round(_buy_base_max, 4)
            _capital_plan["sell_base_size"] = round(_base_size, 4)
        messages.append(
            f"F62 asymmetric sizing: buy ladder = "
            f"{_buy_live_xch:.2f} XCH live ({_trading_pct:.0f}% of avail), "
            f"buy base {_buy_base_max:.4f} vs sell base {_base_size:.4f}"
        )
    # ═══ END PER-SIDE TIER SIZES ══════════════════════════════════════════

    # ═══ F64 (2026-04-12): SELL-SIDE INDEPENDENT SIZING ═════════════════
    # Mirror of F62 (which gives the buy side independent *sizes* from the
    # XCH budget).  F64 handles the reverse: when the CAT balance can fund
    # larger sell offers than the XCH-constrained symmetric base_size,
    # compute independent sell tier sizes from the CAT-side budget so the
    # sell ladder deploys the full CAT capacity.
    #
    # Without F64, sell offer sizes are locked to `_base_size` which is
    # min(XCH, CAT) constrained.  When XCH is the bottleneck, sell offers
    # are artificially small and excess CAT sits idle in the wallet.
    #
    # Approach:
    #   1. Compute the sell-side CAT budget in XCH-equiv (same carve-outs
    #      as the coin-prep feasibility clamp: 85% minus sniper & topup)
    #   2. Derive the largest sell base_size that fits this budget
    #   3. If sell_base > symmetric base (>5% larger), compute independent
    #      sell tier sizes from the sell base
    #   4. Optionally expand sell count if CAT still has excess capacity
    # ──────────────────────────────────────────────────────────────────────
    _sell_n_inner   = _smart_n_inner
    _sell_n_mid     = _smart_n_mid
    _sell_n_outer   = _smart_n_outer
    _sell_n_extreme = _smart_n_extreme
    _sell_spare_inner   = _spare_inner
    _sell_spare_mid     = _spare_mid
    _sell_spare_outer   = _spare_outer
    _sell_spare_extreme = _spare_extreme

    if (_avail_cat > 0 and mid_price and mid_price > 0
            and _smart_sell_inner > 0 and _n_final > 0):
        # ── Step 1: Compute sell-side CAT budget in XCH-equiv ──
        # Mirror the XCH approach: avail − carve-outs.
        # XCH side uses _orig_xch_budget = _post_pools_xch − topup.
        # CAT side:     _f64 budget  = avail_cat × 0.98 − sniper − topup.
        # The 2% margin absorbs price drift between Smart Settings
        # computation and the frontend coin-prep preview (which uses
        # the LIVE mid_price — even a ~1.5% drop increases per-coin
        # token amounts enough to overshoot the balance).
        _cp_hm_f64 = 1.0 + (coin_prep_headroom_pct / 100.0)
        _sniper_cat_tokens = (
            round((_smart_sniper_size / mid_price) * _cp_hm_f64)
            * _smart_sniper_prep
            if _smart_sniper_size > 0 else 0
        )
        _topup_cat_tokens = round(_avail_cat * _TOPUP_BUFFER_PCT)
        _f64_cat_budget_tokens = max(
            0.0,
            _avail_cat * 0.98 - _sniper_cat_tokens - _topup_cat_tokens
        )
        _f64_sell_budget_xch = _f64_cat_budget_tokens * mid_price

        if _f64_sell_budget_xch > 0:
            # ── Step 2: Derive the largest sell base that fits ──
            # Sell side is not reversed — position inner = size inner.
            _sell_live_coeff = (
                _smart_n_inner   * _size_mults[0] +
                _smart_n_mid     * _size_mults[1] +
                _smart_n_outer   * _size_mults[2] +
                _smart_n_extreme * _size_mults[3]
            )
            _sell_spare_coeff = (
                _spare_inner   * _size_mults[0] +
                _spare_mid     * _size_mults[1] +
                _spare_outer   * _size_mults[2] +
                _spare_extreme * _size_mults[3]
            )
            _sell_denom = max(1e-9,
                (_sell_live_coeff + _sell_spare_coeff) * _cp_hm_f64
            )
            # 0.5% safety margin (same as F62) absorbs per-tier rounding.
            _sell_base_max = (_f64_sell_budget_xch * 0.995) / _sell_denom
            _sell_base_max = max(_MIN_OFFER_XCH, _sell_base_max)

            # ── Step 3: Apply if meaningfully larger (>5%) ──
            if _sell_base_max > _base_size * 1.05:
                _f64_old_sell_inner = _smart_sell_inner

                # Helper: compute tier sizes from a base, then verify the
                # ACTUAL total in tokens using the same per-tier integer
                # rounding the frontend uses (round(xch / mid_price × hm)).
                # Returns (sizes_dict, total_cat_tokens).
                def _f64_size_and_verify(base):
                    si = max(_MIN_OFFER_XCH, round(base * _size_mults[0], 4))
                    sm = max(_MIN_OFFER_XCH, round(base * _size_mults[1], 4))
                    so = (max(_MIN_OFFER_XCH, round(base * _size_mults[2], 4))
                          if _max_tiers >= 3 and _smart_sell_outer > 0
                          else _smart_sell_outer)
                    se = (max(_MIN_OFFER_XCH, round(base * _size_mults[3], 4))
                          if _max_tiers == 4 and _smart_sell_extreme > 0
                          else _smart_sell_extreme)
                    # Same formula the frontend uses in buildCoinPrepPlan:
                    # (live + spare) × round(tier_xch / mid_price × headroom)
                    _tls = [
                        (_smart_n_inner + _spare_inner, si),
                        (_smart_n_mid + _spare_mid, sm),
                    ]
                    if _max_tiers >= 3 and so > 0:
                        _tls.append((_smart_n_outer + _spare_outer, so))
                    if _max_tiers == 4 and se > 0:
                        _tls.append((_smart_n_extreme + _spare_extreme, se))
                    total = sum(
                        cnt * round((sx / mid_price) * _cp_hm_f64)
                        for cnt, sx in _tls
                    )
                    return (si, sm, so, se), total

                _f64_sizes, _f64_total_cat = _f64_size_and_verify(
                    _sell_base_max)

                # If the integer-rounded total overshoots the token budget,
                # binary-search for the largest base that fits.
                if _f64_total_cat > _f64_cat_budget_tokens:
                    _lo = _base_size        # known-safe (symmetric)
                    _hi = _sell_base_max     # known-over
                    for _ in range(30):      # converges in <20 iterations
                        _mid_b = (_lo + _hi) / 2.0
                        _, _mid_total = _f64_size_and_verify(_mid_b)
                        if _mid_total <= _f64_cat_budget_tokens:
                            _lo = _mid_b
                        else:
                            _hi = _mid_b
                    _f64_sizes, _f64_total_cat = _f64_size_and_verify(_lo)
                    _sell_base_max = _lo

                # Only apply if still meaningfully larger after the clamp
                if _sell_base_max > _base_size * 1.05:
                    _smart_sell_inner, _smart_sell_mid = _f64_sizes[0], _f64_sizes[1]
                    _smart_sell_outer, _smart_sell_extreme = _f64_sizes[2], _f64_sizes[3]

                    # Also update legacy shared fields so pre-F62 callers see
                    # the sell-side values (shared = sell, as before).
                    _smart_inner   = _smart_sell_inner
                    _smart_mid     = _smart_sell_mid
                    _smart_outer   = _smart_sell_outer
                    _smart_extreme = _smart_sell_extreme

                    # Sell live CAT deployment for reporting
                    _sell_live_xch = (
                        _smart_n_inner   * _smart_sell_inner +
                        _smart_n_mid     * _smart_sell_mid +
                        _smart_n_outer   * _smart_sell_outer +
                        _smart_n_extreme * _smart_sell_extreme
                    )
                    _sell_cat_deployed = round(_sell_live_xch / mid_price, 0)
                    _sell_cat_pct = round(
                        _sell_cat_deployed / _avail_cat * 100, 1
                    ) if _avail_cat > 0 else 0.0

                    if "_capital_plan" in dir() and isinstance(_capital_plan, dict):
                        _capital_plan["sell_base_size"] = round(_sell_base_max, 4)
                        _capital_plan["sell_budget_xch"] = round(
                            _f64_sell_budget_xch, 4)
                    messages.append(
                        f"F64 sell sizing: sell base {_sell_base_max:.4f} XCH "
                        f"(vs symmetric {_base_size:.4f}) — "
                        f"inner {_f64_old_sell_inner:.4f} → "
                        f"{_smart_sell_inner:.4f} XCH, "
                        f"~{_f64_total_cat:,.0f}/{_f64_cat_budget_tokens:,.0f} "
                        f"CAT ({_sell_cat_pct:.0f}% of balance)"
                    )
                    print(
                        f"[SMART_DEFAULTS] F64 sell sizing: "
                        f"sell base {_sell_base_max:.4f} vs sym {_base_size:.4f}, "
                        f"inner {_f64_old_sell_inner:.4f} → "
                        f"{_smart_sell_inner:.4f}, "
                        f"~{_f64_total_cat:,.0f}/{_f64_cat_budget_tokens:,.0f} "
                        f"CAT ({_sell_cat_pct:.0f}%)"
                    )

        # ── Step 4: Count expansion (if CAT still has excess capacity) ──
        # After sizing up, check if the CAT can also support more sell
        # offers (e.g. _n_sell_cap > _smart_max_sell at the new sizes).
        # This handles the edge case where XCH and CAT have similar
        # per-offer capacity but the CAT can fund more total offers.
        if (_n_sell_cap > _smart_max_sell and _smart_sell_inner > 0):
            import math as _math_f64
            _f64_expand_target = min(_n_sell_cap, _target_n)
            if _f64_expand_target > _smart_max_sell:
                _f64_old_sell_count = _smart_max_sell

                def _f64_distribute(n):
                    """Distribute n across tiers; return (counts, spares, cat)."""
                    ni = max(1, round(n * _count_dist[0]))
                    no = (max(0, round(n * _count_dist[2]))
                          if n >= 4 and _max_tiers >= 3 else 0)
                    ne = (max(0, round(n * _count_dist[3]))
                          if n >= 5 and _max_tiers == 4 else 0)
                    nm = max(1, n - ni - no - ne)
                    si = max(_spare_inner,   _math_f64.ceil(ni * 0.5))
                    sm = max(_spare_mid,     _math_f64.ceil(nm * 0.5))
                    so = (max(_spare_outer,  _math_f64.ceil(no * 0.5))
                          if no > 0 else _spare_outer)
                    se = (max(_spare_extreme, _math_f64.ceil(ne * 0.5))
                          if ne > 0 else _spare_extreme)
                    _tiers = [
                        (ni, si, _smart_sell_inner),
                        (nm, sm, _smart_sell_mid),
                    ]
                    if _max_tiers >= 3 and _smart_sell_outer > 0:
                        _tiers.append((no, so, _smart_sell_outer))
                    if _max_tiers == 4 and _smart_sell_extreme > 0:
                        _tiers.append((ne, se, _smart_sell_extreme))
                    cat = sum(
                        (_nl + _ns) * round((_sx / mid_price) * _cp_hm_f64)
                        for _nl, _ns, _sx in _tiers
                    )
                    return (ni, nm, no, ne), (si, sm, so, se), cat

                _f64c, _f64s, _f64_cat = _f64_distribute(_f64_expand_target)
                if _f64_cat <= _f64_cat_budget_tokens:
                    _f64_expanded = _f64_expand_target
                else:
                    # Scale down to fit, then fine-tune upward
                    _f64_scale = _f64_cat_budget_tokens / max(1, _f64_cat)
                    _f64_expanded = max(
                        _smart_max_sell, int(_f64_expand_target * _f64_scale)
                    )
                    while _f64_expanded < _f64_expand_target:
                        _, _, _tc = _f64_distribute(_f64_expanded + 1)
                        if _tc <= _f64_cat_budget_tokens:
                            _f64_expanded += 1
                        else:
                            break
                    _f64c, _f64s, _f64_cat = _f64_distribute(_f64_expanded)

                if _f64_expanded > _f64_old_sell_count:
                    _smart_max_sell     = _f64_expanded
                    _sell_n_inner       = _f64c[0]
                    _sell_n_mid         = _f64c[1]
                    _sell_n_outer       = _f64c[2]
                    _sell_n_extreme     = _f64c[3]
                    _sell_spare_inner   = _f64s[0]
                    _sell_spare_mid     = _f64s[1]
                    _sell_spare_outer   = _f64s[2]
                    _sell_spare_extreme = _f64s[3]
                    messages.append(
                        f"F64 sell count: {_f64_old_sell_count} → "
                        f"{_f64_expanded} sell offers "
                        f"({_f64_cat:,.0f}/{_f64_cat_budget_tokens:,.0f} "
                        f"tokens)"
                    )
                    print(
                        f"[SMART_DEFAULTS] F64 sell count: "
                        f"{_f64_old_sell_count} → {_f64_expanded} "
                        f"(CAT: {_f64_cat:,.0f}/{_f64_cat_budget_tokens:,.0f})"
                    )

        # ── Update strategy if asymmetric ──
        if _smart_max_buy != _smart_max_sell or _smart_sell_inner != _smart_buy_inner:
            _strategy = (
                f"{_tier_style} {_max_tiers}-tier ladder · "
                f"{_smart_max_buy}B/{_smart_max_sell}S offers"
                f" · {_trading_xch:.2f} XCH trading ({_trading_pct:.0f}%)"
                + (f" · {_pool_note}" if _pool_note else "")
            )
            if "_capital_plan" in dir() and isinstance(_capital_plan, dict):
                _capital_plan["strategy"] = _strategy
    # ═══ END SELL-SIDE INDEPENDENT SIZING ═════════════════════════════════

    # ═══ F65 FINAL SELL-SIDE CAT VERIFICATION ═════════════════════════════
    # Belt-and-suspenders check: compute the EXACT coin-prep total using
    # the same formula the frontend uses (tiers + sniper + topup), and
    # scale sell sizes down if the total exceeds _avail_cat.
    # This catches any overshoot regardless of origin: F64 budget drift,
    # rounding accumulation, mid_price movement, or future code changes.
    if (_avail_cat > 0 and mid_price and mid_price > 0
            and _smart_sell_inner > 0):
        _f65_hm = 1.0 + (coin_prep_headroom_pct / 100.0)
        _f65_tiers = [
            (_sell_n_inner   + _sell_spare_inner,   _smart_sell_inner),
            (_sell_n_mid     + _sell_spare_mid,     _smart_sell_mid),
        ]
        if _max_tiers >= 3 and _smart_sell_outer > 0:
            _f65_tiers.append(
                (_sell_n_outer + _sell_spare_outer, _smart_sell_outer))
        if _max_tiers == 4 and _smart_sell_extreme > 0:
            _f65_tiers.append(
                (_sell_n_extreme + _sell_spare_extreme, _smart_sell_extreme))
        _f65_tier_cat = sum(
            _cnt * round((_sx / mid_price) * _f65_hm)
            for _cnt, _sx in _f65_tiers
        )
        _f65_sniper_cat = (
            round((_smart_sniper_size / mid_price) * _f65_hm)
            * _smart_sniper_prep
            if _smart_sniper_size > 0 else 0
        )
        _f65_topup_cat = round(_avail_cat * _TOPUP_BUFFER_PCT)
        _f65_total_cat = _f65_tier_cat + _f65_sniper_cat + _f65_topup_cat

        if _f65_total_cat > _avail_cat:
            # Overshoot!  Scale sell sizes down so the total fits.
            # Only tier sizes are adjustable — sniper and topup are fixed.
            _f65_tier_budget = max(1.0, _avail_cat - _f65_sniper_cat - _f65_topup_cat)
            _f65_scale = _f65_tier_budget / max(1.0, _f65_tier_cat)
            _f65_old_inner = _smart_sell_inner
            _smart_sell_inner   = max(_MIN_OFFER_XCH, round(_smart_sell_inner   * _f65_scale, 4))
            _smart_sell_mid     = max(_MIN_OFFER_XCH, round(_smart_sell_mid     * _f65_scale, 4))
            _smart_sell_outer   = (max(_MIN_OFFER_XCH, round(_smart_sell_outer  * _f65_scale, 4))
                                   if _smart_sell_outer > 0 else 0.0)
            _smart_sell_extreme = (max(_MIN_OFFER_XCH, round(_smart_sell_extreme * _f65_scale, 4))
                                   if _smart_sell_extreme > 0 else 0.0)
            # Keep shared sizes in sync (pre-F62 callers read these)
            _smart_inner   = _smart_sell_inner
            _smart_mid     = _smart_sell_mid
            _smart_outer   = _smart_sell_outer
            _smart_extreme = _smart_sell_extreme

            # Verify the scaled sizes actually fit now
            _f65_tiers2 = [
                (_sell_n_inner + _sell_spare_inner, _smart_sell_inner),
                (_sell_n_mid   + _sell_spare_mid,   _smart_sell_mid),
            ]
            if _max_tiers >= 3 and _smart_sell_outer > 0:
                _f65_tiers2.append(
                    (_sell_n_outer + _sell_spare_outer, _smart_sell_outer))
            if _max_tiers == 4 and _smart_sell_extreme > 0:
                _f65_tiers2.append(
                    (_sell_n_extreme + _sell_spare_extreme, _smart_sell_extreme))
            _f65_new_tier = sum(
                _c * round((_s / mid_price) * _f65_hm)
                for _c, _s in _f65_tiers2
            )
            _f65_new_total = _f65_new_tier + _f65_sniper_cat + _f65_topup_cat
            messages.append(
                f"F65 sell-side CAT safety clamp: "
                f"inner {_f65_old_inner:.4f} → {_smart_sell_inner:.4f} "
                f"({_f65_scale*100:.1f}% scale), "
                f"~{_f65_new_total:,.0f}/{_avail_cat:,.0f} CAT "
                f"(was {_f65_total_cat:,.0f} — overshoot of "
                f"{_f65_total_cat - _avail_cat:,.0f})"
            )
            print(
                f"[SMART_DEFAULTS] F65 CAT safety clamp: "
                f"inner {_f65_old_inner:.4f} → {_smart_sell_inner:.4f}, "
                f"total {_f65_total_cat:,.0f} → {_f65_new_total:,.0f} "
                f"(budget {_avail_cat:,.0f})"
            )
    # ═══ END F65 FINAL SELL-SIDE CAT VERIFICATION ═════════════════════════

    # ═══ Build response ═══
    result = {
        # Smart Pricing
        "dynamic_spread_enabled": has_both_prices,
        "base_spread_bps": round(base_spread_bps / 100, 1),
        "volatility_window_hours": volatility_window,
        "inner_edge_bps": round(inner_edge_bps / 100, 1),
        "min_spread_bps": round(min_spread_bps / 100, 1),
        "max_spread_bps": round(max_spread_bps / 100, 1),
        "inventory_enabled": True,
        "skew_intensity": skew_intensity,
        "max_position_xch": max_position,
        "spread_bps": round(base_spread_bps / 100, 1),
        "loop_seconds": loop_seconds,

        # Auto-Requote
        "auto_requote": True,
        "requote_bps": round(requote_bps / 100, 1),
        "requote_cooldown": 60,
        "requote_batch_size": requote_batch_size,

        # Safety & Limits (reserves intentionally excluded — user's choice)
        "max_mid_move": round(max_mid_move, 1),
        "dynamic_limit_pct": dynamic_limit_pct,
        "max_step_change_pct": max_step_change_pct,
        "arb_threshold_bps": round(arb_alert_threshold_bps / 100, 1),
        "min_mid": min_mid,
        "max_mid": max_mid,

        # Market Intelligence
        "competitor_aware_enabled": competitor_enabled,
        "dbx_max_spread_bps": 5.0,
        "pair_incentivized": bool(ticker.get("incentives")) if ticker else None,

        # Coin Prep (all market-derived)
        "coin_prep_multiplier": coin_prep_multiplier,
        "coin_prep_headroom_pct": coin_prep_headroom_pct,
        "inner_tier_spare_count": _spare_inner,
        "mid_tier_spare_count":   _spare_mid,
        "outer_tier_spare_count": _spare_outer,
        "extreme_tier_spare_count": _spare_extreme,
        # Per-side spares (V4): buy side uses the reversed values when
        # BUY_LADDER_REVERSED is on so the frontend swap → launcher flip chain
        # places the densest spare pool at the smallest coin SIZE (which under
        # reverse-buy lives at the most-active slot inner). Sell side keeps
        # the standard size-indexed spares (largest spare on largest size, since
        # sell slot inner = inner SIZE).
        "buy_inner_tier_spare_count":   _buy_spare_inner,
        "buy_mid_tier_spare_count":     _buy_spare_mid,
        "buy_outer_tier_spare_count":   _buy_spare_outer,
        "buy_extreme_tier_spare_count": _buy_spare_extreme,
        "sell_inner_tier_spare_count": _sell_spare_inner,
        "sell_mid_tier_spare_count":   _sell_spare_mid,
        "sell_outer_tier_spare_count": _sell_spare_outer,
        "sell_extreme_tier_spare_count": _sell_spare_extreme,
        # F65 (2026-04-12): snapshot the mid_price used by Smart Settings
        # so the frontend coin-prep preview uses the SAME price for
        # per-coin token calculations — not the live price which may
        # have drifted since Smart Settings ran, causing false
        # overshoot warnings.
        "smart_mid_price": mid_price if mid_price and mid_price > 0 else None,

        # Transaction Fees (Coinset-estimated or existing values)
        "transaction_fee_mode": "auto",
        "transaction_fee_xch": _smart_fee_xch,
        "fee_coin_size_xch": _fee_coin_size,
        "fee_prep_count": _fee_prep_count,

        # F49 (2026-04-09): two-tier reserve — topup pool allocation
        # F55 (2026-04-09): use the FINAL adjusted `_topup_buffer_xch`
        # rather than the raw 10%-of-bottleneck value. The two diverged
        # whenever CAT was the bottleneck or the 2× largest-tier floor
        # bumped the buffer up — leaving the GUI form input showing
        # one number while the log message reported a different one.
        #
        # `xch_reserve` / `cat_reserve` above are the user's untouchable
        # hard floor (set in step 1 of settings). The topup pool is the
        # working allocation Smart Settings carves out of the remaining
        # balance for the coin-splitting worker to consume. After all
        # adjustments it equals (avail − fees − sniper − trading), which
        # captures both the original 10% slice AND any XCH stranded by
        # a CAT-side bottleneck.
        #
        # The frontend writes these to .env on save; `cfg.update()`
        # clears the session spend counter whenever either value
        # changes, so a fresh Smart Settings run always gets a fresh
        # budget.
        "topup_pool_pct": _TOPUP_BUFFER_PCT,
        "topup_pool_xch": round(_topup_buffer_xch, 4) if _topup_buffer_xch > 0 else 0,
        # CAT side: 10% of the user's available CAT (per-side computation,
        # matches the user's "10% of remaining" design intent). Capped at
        # avail so it never exceeds what they actually have.
        "topup_pool_cat": (
            round(min(_avail_cat, _avail_cat * _TOPUP_BUFFER_PCT), 3)
            if _avail_cat > 0
            else 0
        ),

        # Ladder Strategy
        # Reverse buy ladder is always recommended — it limits XCH exposure by placing
        # small offers closest to price. A large offer only fills on a genuine deep drop.
        "buy_ladder_reversed": True,

        # Offer Sizing (capital-derived — requires reserve params from frontend step 1)
        "max_active_buy": _smart_max_buy,
        "max_active_sell": _smart_max_sell,
        "default_trade_xch": round(_smart_trade_size, 4) if _smart_trade_size > 0 else None,
        # Legacy single-shared size fields (kept in sync with the SELL
        # side so pre-F62 callers continue to work).
        "inner_size_xch": _smart_inner if _smart_inner > 0 else None,
        "mid_size_xch": _smart_mid if _smart_mid > 0 else None,
        "outer_size_xch": _smart_outer if _smart_outer > 0 else None,
        "extreme_size_xch": _smart_extreme if _smart_extreme > 0 else None,
        # F62 (2026-04-09): per-side tier sizes. Position-semantic on both
        # sides — BUY_INNER_SIZE_XCH is what position inner buys spend,
        # SELL_INNER_SIZE_XCH is what position inner sells spend. Under
        # reverse-buy, the BUY values are naturally smaller at position
        # inner (tight side) and larger at position extreme (wide side)
        # because Smart Settings computes them directly from the XCH
        # budget without any shared solver constraint.
        "buy_inner_size_xch":   _smart_buy_inner   if _smart_buy_inner   > 0 else None,
        "buy_mid_size_xch":     _smart_buy_mid     if _smart_buy_mid     > 0 else None,
        "buy_outer_size_xch":   _smart_buy_outer   if _smart_buy_outer   > 0 else None,
        "buy_extreme_size_xch": _smart_buy_extreme if _smart_buy_extreme > 0 else None,
        "sell_inner_size_xch":   _smart_sell_inner   if _smart_sell_inner   > 0 else None,
        "sell_mid_size_xch":     _smart_sell_mid     if _smart_sell_mid     > 0 else None,
        "sell_outer_size_xch":   _smart_sell_outer   if _smart_sell_outer   > 0 else None,
        "sell_extreme_size_xch": _smart_sell_extreme if _smart_sell_extreme > 0 else None,
        "inner_tier_count": _smart_n_inner if _smart_n_inner > 0 else None,
        "mid_tier_count": _smart_n_mid if _smart_n_mid > 0 else None,
        "outer_tier_count": _smart_n_outer if _smart_n_outer >= 0 else None,
        "extreme_tier_count": _smart_n_extreme if _smart_n_extreme >= 0 else None,
        # Per-side live counts (V4): buy side uses the reversed values when
        # BUY_LADDER_REVERSED is on so the densest count lands at the smallest
        # coin SIZE (slot inner under reverse-buy). The frontend then performs
        # its inner↔extreme swap to convert these size-indexed values into
        # position-indexed BUY_*_TIER_COUNT inputs.
        "buy_inner_tier_count":   _buy_n_inner   if _buy_n_inner   > 0 else None,
        "buy_mid_tier_count":     _buy_n_mid     if _buy_n_mid     > 0 else None,
        "buy_outer_tier_count":   _buy_n_outer   if _buy_n_outer   >= 0 else None,
        "buy_extreme_tier_count": _buy_n_extreme if _buy_n_extreme >= 0 else None,
        "sell_inner_tier_count": _sell_n_inner if _sell_n_inner > 0 else None,
        "sell_mid_tier_count": _sell_n_mid if _sell_n_mid > 0 else None,
        "sell_outer_tier_count": _sell_n_outer if _sell_n_outer >= 0 else None,
        "sell_extreme_tier_count": _sell_n_extreme if _sell_n_extreme >= 0 else None,
        "_capital_plan": _capital_plan,

        # Bot Operations
        "sniper_enabled": getattr(cfg, "SNIPER_ENABLED", True),
        "sniper_size_xch": _smart_sniper_size,
        "sniper_prep_count": _smart_sniper_prep,
        "sniper_rearm_price_move_bps": getattr(cfg, "SNIPER_REARM_PRICE_MOVE_BPS", 100),
        "sniper_rearm_gap_move_bps": getattr(cfg, "SNIPER_REARM_GAP_MOVE_BPS", 100),
        "splash_enabled": cfg.SPLASH_ENABLED,
        "enable_coin_prep": cfg.ENABLE_COIN_PREP,
        "enable_runtime_coin_health": cfg.ENABLE_RUNTIME_COIN_HEALTH,

        # V2 Metadata for toast + GUI
        "_data_sources": {
            "version": 2,
            "risk_profile": _risk_profile_name,
            "has_wallet_balance": has_wallet,
            "has_both_prices": has_both_prices,
            "has_trade_history": bool(trades),
            "has_competitor_data": orderbook["has_data"],
            "has_tibet_pool": tibet.get("has_data", False),
            "has_tibet_quote": bool(tibet_quote),
            "has_spacescan": spacescan.get("has_data", False),
            "has_bot_history": bot_perf.get("has_history", False),
            "mid_price": mid_price,
            "arb_gap_bps": round(arb_gap_bps, 1),
            "pool_depth_xch": pool_xch,
            "competitor_spread_bps": round(orderbook.get("competitor_spread_bps", 0), 0),
            "xch_balance": round(xch_spendable, 2),
            "data_quality_score": quality_score,
            "data_quality_label": quality_label,
            "volatility_regime": regime,
            "liquidity_level": liq.get("level", "unknown"),
            "fills_per_day": fills_per_day,
            "volume_trend": trades.get("volume_trend", "unknown") if trades else "unknown",
            "risk_level": risk_level,
            "messages": messages,
        },
        # V2: Full analysis available for GUI expansion
        "_analysis": {
            "volatility": vol,
            "liquidity": liq,
            "token_health": health,
            "bot_performance": bot_perf,
            "data_quality": quality,
        },
    }

    print(f"[SMART_DEFAULTS v2] Offers: buy={_smart_max_buy}, sell={_smart_max_sell} | "
          f"Tiers: inner={_smart_inner}, mid={_smart_mid}, outer={_smart_outer}, extreme={_smart_extreme} | "
          + (f"Sell tiers: {_sell_n_inner}/{_sell_n_mid}/{_sell_n_outer}/{_sell_n_extreme} | "
             if _sell_n_inner != _smart_n_inner else "")
          + f"Spares: inner={_spare_inner}, mid={_spare_mid}, outer={_spare_outer}, extreme={_spare_extreme} | "
          f"Position: {max_position} XCH | Skew: {skew_intensity}")
    print(f"[SMART_DEFAULTS v2] === Done! Spread: {_bps_to_pct(base_spread_bps)}, "
          f"Requote: {_bps_to_pct(requote_bps)}, "
          f"Quality: {quality_score}% ===\n")
    log_event("success", "smart_defaults",
              f"Smart Settings: Spread {_bps_to_pct(base_spread_bps)}, "
              f"Requote {_bps_to_pct(requote_bps)}, "
              f"Quality {quality_score}% ({quality_label})",
              {"version": 2, "base_spread_bps": base_spread_bps, "requote_bps": requote_bps,
               "quality_score": quality_score,
               "regime": regime, "fills_per_day": fills_per_day,
               "mid_price": mid_price, "arb_gap_bps": round(arb_gap_bps, 1)})

    return jsonify(result)


# ---------------------------------------------------------------------------
# Database Routes
# ---------------------------------------------------------------------------

@app.route("/api/db/backup", methods=["POST"])
def api_db_backup():
    """Create a database backup."""
    try:
        path = backup_database()
        # Return only the filename, not the full filesystem path, to
        # avoid leaking the user's directory structure to the GUI.
        filename = os.path.basename(path) if path else ""
        return jsonify({"status": "backed_up", "filename": filename})
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Log Route (for GUI log panel)
# ---------------------------------------------------------------------------

@app.route("/api/logs")
def api_logs():
    """Get recent log events — only from current session.

    Uses whichever is more recent: session start time or user's manual
    clear point. This prevents old sessions' noise flooding the console.
    """
    limit = request.args.get("limit", 50, type=int)
    category = request.args.get("category") or None  # e.g. offer/pricing/risk
    try:
        from database import get_events_since, get_recent_events
        # Pick the most recent cutoff — session start vs user clear
        cutoff = _session_start_time
        if _logs_cleared_at and (not cutoff or _logs_cleared_at > cutoff):
            cutoff = _logs_cleared_at
        if cutoff:
            events_list = get_events_since(cutoff, limit=limit, category=category)
        else:
            events_list = get_recent_events(limit=limit, category=category)
        return jsonify({"logs": _serialize_list(events_list)})
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Wallet & CAT Discovery Routes (GUI startup needs these)
# ---------------------------------------------------------------------------

@app.route("/api/fingerprint")
def api_fingerprint():
    """Get wallet fingerprint — prefer the live wallet session over saved config."""
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
        return _api_error(e, request.path)


def _normalize_asset_id(asset_id: str) -> str:
    """Normalize asset ID for matching — remove 0x, lowercase, strip trailing 00 pairs.
    Matches V1's normalize_asset_id() exactly.
    """
    if not asset_id:
        return ""
    cleaned = asset_id.lower().replace("0x", "")
    while cleaned.endswith("00") and len(cleaned) > 60:
        cleaned = cleaned[:-2]
    return cleaned


def _get_dexie_pairs() -> list:
    """Fetch all trading pairs from Dexie API.
    Matches V1's get_dexie_pairs() exactly:
      GET /v2/prices/tickers (no params) → all tickers → filter _XCH pairs.
    """
    try:
        import requests as _req
        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
        url = f"{dexie_base}/v2/prices/tickers"
        response = _req.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        tickers = data.get("tickers", [])

        pairs = []
        for ticker in tickers:
            ticker_id = ticker.get("ticker_id", "")
            if "_XCH" in ticker_id and ticker_id != "XCH_USDT":
                base_name = ticker_id.replace("_XCH", "")
                pairs.append({
                    "ticker_id": ticker_id,
                    "name": ticker.get("base_name", base_name),
                    "asset_id": ticker.get("base_id", ""),
                    "price": float(ticker.get("current_avg_price", 0) or 0),
                    "volume_24h": float(ticker.get("target_volume", 0) or 0),
                    # Extra market stats — surfaced in token snapshot on pair select
                    "vol_7d_xch":   float(ticker.get("target_volume_7d",  0) or 0),
                    "vol_30d_xch":  float(ticker.get("target_volume_30d", 0) or 0),
                    "price_high_7d": float(ticker.get("high_7d",  0) or 0),
                    "price_low_7d":  float(ticker.get("low_7d",   0) or 0),
                })

        pairs.sort(key=lambda x: x["volume_24h"], reverse=True)
        print(f"[CATS] Fetched {len(pairs)} Dexie pairs")
        return pairs
    except Exception as e:
        print(f"[CATS] Failed to fetch Dexie pairs: {e}")
        return []


@app.route("/api/token_overview")
def api_token_overview():
    """Return description + website for a token from Dexie v1/assets.
    Called once when the user selects a trading pair on the dashboard.
    Searches up to the first 3 pages (300 assets) to find a match.
    ?dexie_asset_id=<64-char-id>
    """
    dexie_asset_id = (request.args.get("dexie_asset_id") or "").strip().lower()
    if not dexie_asset_id:
        return jsonify({"success": False, "description": "", "website": ""})
    try:
        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
        for page in range(1, 4):
            resp = _req.get(
                f"{dexie_base}/v1/assets",
                params={"page": page, "page_size": 100},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            assets = data.get("assets", [])
            for asset in assets:
                if asset.get("id", "").lower() == dexie_asset_id:
                    return jsonify({
                        "success": True,
                        "description": asset.get("description", ""),
                        "website": asset.get("website", ""),
                    })
            # If fewer assets than page_size returned, no point fetching more
            if len(assets) < 100:
                break
        return jsonify({"success": True, "description": "", "website": ""})
    except Exception as e:
        print(f"[TOKEN_OVERVIEW] Failed for {dexie_asset_id[:12]}: {e}")
        return jsonify({"success": False, "description": "", "website": "", "error": str(e)})


@app.route("/api/dexie/v3-pairs")
def api_dexie_v3_pairs():
    """F38 (2026-04-08): expose Dexie v3 pairs (with summary stats) to the GUI.

    Used by the pair selector to show real volume / last-price / activity
    instead of just "this CAT exists in your wallet".
    """
    global bot
    if not bot or not getattr(bot, "dexie_manager", None):
        return jsonify({"pairs": [], "error": "bot not initialised"}), 503
    try:
        pairs = bot.dexie_manager.fetch_v3_pairs() or []
        return jsonify({"pairs": pairs, "count": len(pairs)})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/cats")
def api_cats():
    """Discover CAT tokens by matching wallet CATs against Dexie pairs.

    Matches V1's get_available_cats() approach:
    1. Fetch wallet CATs (type 6)
    2. Fetch ALL Dexie trading pairs
    3. Match by asset_id → get real ticker_id from Dexie (e.g. "SBX_XCH")
    4. Return matched CATs as "ready", unmatched wallet CATs separately
    5. F38 (2026-04-08): also enrich each matched CAT with v3 pair stats
       (last_price, base_volume_24h, target_volume_24h) when available
       so the pair selector can sort/filter by trading activity.
    """
    cats = []
    try:
        # Step 1: Get CATs from wallet
        from wallet import get_wallets
        result = get_wallets()
        wallet_cats = {}  # asset_id -> {wallet_id, name, asset_id}

        if result and result.get("success"):
            wallets = result.get("wallets") or []
            for w in wallets:
                wtype = w.get("type", 0)
                if wtype == 6 or str(wtype) == "6" or str(wtype).upper() == "CAT":
                    wallet_id = w.get("id", 0)
                    name = w.get("name", "Unknown CAT")
                    asset_id = w.get("data", "") or w.get("asset_id", "")
                    if isinstance(asset_id, str) and len(asset_id) > 64:
                        asset_id = asset_id[:64]
                    if asset_id:
                        wallet_cats[asset_id] = {
                            "wallet_id": wallet_id,
                            "name": name,
                            "asset_id": asset_id,
                        }
                        print(f"[CATS] Found wallet CAT: {name} (Wallet {wallet_id}, Asset: {asset_id[:16]}...)")

        print(f"[CATS] Total wallet CATs found: {len(wallet_cats)}")
        log_event("info", "cat_discovery", f"Found {len(wallet_cats)} CAT tokens in wallet")

        # Step 2: Fetch ALL Dexie trading pairs
        dexie_pairs = _get_dexie_pairs()
        print(f"[CATS] Found {len(dexie_pairs)} Dexie pairs")

        # F38 (2026-04-08): also fetch v3 pairs cache so we can enrich
        # each matched CAT with last_price + 24h volume from the live
        # v3 surface. Falls back gracefully if v3 is unavailable.
        v3_by_ticker: Dict[str, Dict] = {}
        try:
            global bot
            if bot and getattr(bot, "dexie_manager", None):
                v3_pairs = bot.dexie_manager.fetch_v3_pairs() or []
                for vp in v3_pairs:
                    try:
                        tid = str(vp.get("ticker_id") or vp.get("ticker") or "").upper()
                        if tid:
                            v3_by_ticker[tid] = vp
                    except Exception:
                        continue
                if v3_by_ticker:
                    print(f"[CATS] Enriched with {len(v3_by_ticker)} v3 pair stats")
        except Exception as _v3_err:
            print(f"[CATS] v3 enrichment skipped: {_v3_err}")

        # Step 3: Match wallet CATs against Dexie pairs by asset_id
        matched_count = 0
        for pair in dexie_pairs:
            raw_asset_id = pair.get("asset_id", "")
            normalized_dexie = _normalize_asset_id(raw_asset_id)

            wallet_info = None
            for wallet_asset, wallet_data in wallet_cats.items():
                normalized_wallet = _normalize_asset_id(wallet_asset)
                if (wallet_asset.lower() == raw_asset_id.lower() or
                        normalized_wallet == normalized_dexie):
                    wallet_info = wallet_data
                    break

            if wallet_info:
                matched_count += 1
                print(f"[CATS] Matched: {pair['name']} ({pair['ticker_id']}) in wallet {wallet_info['wallet_id']}")
                # Use the Dexie base_id (full 64-char) for the icon URL.
                # Sage wallet returns a 32-char truncated ID which icons.dexie.space
                # does not recognise (returns a grey 404 placeholder).
                # IMPORTANT: icons.dexie.space serves .webp ONLY — .png returns
                # a 1140-byte grey placeholder (404).
                dexie_asset_id = pair.get("asset_id", "") or wallet_info["asset_id"]
                icon_url = (f"https://icons.dexie.space/{dexie_asset_id}.webp"
                            if dexie_asset_id else "")
                # F38: pull v3 stats if available (last_price, 24h volumes)
                _v3 = v3_by_ticker.get(pair["ticker_id"].upper(), {})
                cats.append({
                    "asset_id": wallet_info["asset_id"],
                    "dexie_asset_id": dexie_asset_id,
                    "icon_url": icon_url,
                    "name": pair["name"],
                    "ticker": pair["ticker_id"].replace("_XCH", ""),
                    "ticker_id": pair["ticker_id"],  # Real Dexie ticker e.g. "SBX_XCH"
                    "wallet_id": wallet_info["wallet_id"],
                    "decimals": 3,
                    "category": "ready",
                    "volume_24h":    pair.get("volume_24h", 0),
                    "price":         pair.get("price", 0),
                    "vol_7d_xch":    pair.get("vol_7d_xch", 0),
                    "vol_30d_xch":   pair.get("vol_30d_xch", 0),
                    "price_high_7d": pair.get("price_high_7d", 0),
                    "price_low_7d":  pair.get("price_low_7d", 0),
                    # F38: v3 enrichment fields (None when not available)
                    "v3_last_price":      _v3.get("last_price"),
                    "v3_base_volume":     _v3.get("base_volume"),
                    "v3_target_volume":   _v3.get("target_volume"),
                    "v3_high":            _v3.get("high"),
                    "v3_low":             _v3.get("low"),
                    "v3_bid":             _v3.get("bid"),
                    "v3_ask":             _v3.get("ask"),
                })

        print(f"[CATS] Matched {matched_count} wallet CATs with Dexie pairs")
        log_event("success", "cat_discovery",
                  f"Matched {matched_count} wallet CATs with Dexie trading pairs")

        # Step 4: Add wallet CATs not on Dexie (unmatched)
        matched_assets = {c["asset_id"].lower() for c in cats}
        for asset_id, wdata in wallet_cats.items():
            if asset_id.lower() not in matched_assets:
                ticker = wdata["name"].split(" ")[0] if wdata["name"] else asset_id[:8]
                cats.append({
                    "asset_id": asset_id,
                    "name": wdata["name"],
                    "ticker": ticker,
                    "ticker_id": f"{ticker}_XCH",
                    "wallet_id": wdata["wallet_id"],
                    "decimals": 3,
                    "category": "wallet_only",
                    "volume_24h": 0,
                })

    except Exception as e:
        print(f"[CATS] Error in CAT discovery: {e}")
        import traceback
        traceback.print_exc()

    # Fallback: if everything failed, use configured CAT from .env
    if not cats:
        cat_id = cfg.CAT_ASSET_ID
        if cat_id:
            cat_name = getattr(cfg, 'CAT_NAME', 'CAT')
            cat_ticker = getattr(cfg, 'CAT_TICKER_ID', cat_id[:8])
            cats.append({
                "asset_id": cat_id,
                "name": cat_name,
                "ticker": cat_ticker,
                "ticker_id": cat_ticker,
                "wallet_id": getattr(cfg, 'CAT_WALLET_ID', 2),
                "decimals": getattr(cfg, 'CAT_DECIMALS', 3),
                "category": "ready",
                "volume_24h": 0,
            })

    return jsonify({"success": True, "cats": cats})


@app.route("/api/cat/select", methods=["POST"])
def api_cat_select():
    """Select active CAT token — stores wallet_id so balance lookups work."""
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    asset_id = data.get("asset_id", "")
    wallet_id = data.get("wallet_id")
    name = data.get("name", "")
    decimals = data.get("decimals", 3)
    ticker_id = data.get("ticker_id", "")

    # Validate asset_id format (64 lowercase hex chars) BEFORE writing to
    # .env. Blocks a malformed or arbitrarily-long string from corrupting
    # the configuration.
    if asset_id:
        asset_id = str(asset_id).strip()
        if len(asset_id) != 64 or not all(c in '0123456789abcdefABCDEF' for c in asset_id):
            return jsonify({
                "success": False,
                "error": "CAT asset_id must be exactly 64 hex characters",
            }), 400
        asset_id = asset_id.lower()
    # Defensive caps on other user-controlled strings
    if name and len(str(name)) > 128:
        return jsonify({"success": False, "error": "CAT name too long"}), 400
    if ticker_id and len(str(ticker_id)) > 64:
        return jsonify({"success": False, "error": "Ticker ID too long"}), 400
    if decimals is not None:
        try:
            decimals = int(decimals)
            if decimals < 0 or decimals > 18:
                return jsonify({"success": False, "error": "Invalid decimals"}), 400
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "Invalid decimals"}), 400

    # Safety: never change the trading pair while the bot is running.
    # Switching mid-run would cause offers to be created for the wrong token
    # until the next loop cycle (and even then, stale offers stay on the book).
    try:
        if bot is not None and bot.is_running():
            return jsonify({
                "success": False,
                "error": "Stop the bot before changing the trading pair. "
                         "Switching CAT mid-run would cause offers for the wrong token."
            }), 409
    except Exception:
        pass

    with _active_cat_lock:
        _active_cat["asset_id"] = asset_id
        _active_cat["name"] = name
        _active_cat["decimals"] = int(decimals) if decimals else 3
        _active_cat["ticker_id"] = ticker_id
        if wallet_id is not None:
            _active_cat["wallet_id"] = int(wallet_id)

    # Persist to .env so it survives restarts
    # NOTE: CAT_WALLET_ID is NOT saved — it's assigned dynamically by
    # get_wallets() based on CAT_ASSET_ID. Saving a static wallet_id
    # caused wrong-token trading when the mapping changed between sessions.
    if asset_id:
        cfg.update("CAT_ASSET_ID", asset_id)
    if name:
        cfg.update("CAT_NAME", name)
    if decimals:
        cfg.update("CAT_DECIMALS", str(int(decimals)))
    if ticker_id:
        cfg.update("CAT_TICKER_ID", ticker_id)

    # Reset risk manager so stale inventory/CB state from the previous CAT
    # doesn't leak into the advisor or circuit breaker logic for the new one.
    if bot is not None:
        try:
            if hasattr(bot, "risk_manager") and bot.risk_manager:
                bot.risk_manager.reset_session()
                log_event("info", "cat_switch_risk_reset",
                          f"Risk manager reset for CAT change to {name}")
        except Exception as e:
            log_event("warning", "cat_switch_risk_reset_failed",
                      f"Could not reset risk manager on CAT switch: {e}")

    # Auto-resolve TIBET_PAIR_ID for the newly selected CAT.
    # Runs in background so the select response returns immediately.
    # Clears the resolver cache first so we get fresh data for the new asset.
    if asset_id:
        def _resolve_new_cat_tibet():
            try:
                import cat_resolver as _cr
                # Force refresh — asset just changed.
                # Clear TIBET_PAIR_ID first so _apply_to_cfg fills in the
                # correct value for the new token (not the previous token's pair).
                _cr._cache = None
                _cr._last_resolve_at = 0
                cfg.update("TIBET_PAIR_ID", "")
                meta = _cr.resolve_and_apply(cfg)
                if meta.get("pair_id"):
                    log_event("info", "cat_tibet_pair_resolved",
                              f"TIBET_PAIR_ID auto-resolved for {name}: "
                              f"{meta['pair_id'][:20]}...")
                    print(f"[CAT SELECT] TIBET_PAIR_ID resolved: {meta['pair_id'][:20]}...")
                else:
                    log_event("info", "cat_tibet_pair_not_found",
                              f"CAT {name} ({asset_id[:12]}...) has no TibetSwap pair — "
                              f"AMM monitoring disabled for this token")
            except Exception as e:
                log_event("warning", "cat_tibet_resolve_error",
                          f"TIBET_PAIR_ID auto-resolve failed after CAT select: {e}")
        import threading as _t
        _t.Thread(target=_resolve_new_cat_tibet, daemon=True,
                  name="cat-tibet-resolve").start()

    # Notify the Sage wallet adapter so _get_cat_asset_id() returns the new
    # asset ID immediately — without waiting for .env to be re-read.
    try:
        from wallet_sage import notify_cat_asset_id_changed
        notify_cat_asset_id_changed(asset_id)
    except Exception:
        pass  # Chia wallet mode — no-op

    print(f"🔄 CAT selected: {name} (wallet_id={wallet_id}, asset={asset_id[:12]}...)")
    log_event("info", "cat_selected", f"Trading pair selected: {name} (wallet {wallet_id})")
    return jsonify({"success": True, "asset_id": asset_id, "wallet_id": wallet_id})


@app.route("/api/cat/refresh", methods=["POST"])
def api_cat_refresh():
    """Refresh CAT token list (re-read from config)."""
    cfg.reload()
    return jsonify({"success": True})


@app.route("/api/balances/refresh", methods=["POST"])
def api_balances_refresh():
    """Force refresh wallet balances and return them."""
    try:
        # Fetch fresh balances from wallet
        xch_bal = {"spendable": 0, "total": 0}
        cat_bal = {"spendable": 0, "total": 0}
        try:
            from wallet import get_wallet_balance, WALLET_ID_XCH
            xr = get_wallet_balance(WALLET_ID_XCH)
            if xr and xr.get("success"):
                wb = xr.get("wallet_balance") or {}
                xch_bal["total"] = _safe_float(wb.get("confirmed_wallet_balance", 0)) / 1e12
                xch_bal["spendable"] = _safe_float(wb.get("spendable_balance", 0)) / 1e12
            cat_wid = _active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
            cat_dec = _active_cat.get("decimals") or getattr(cfg, 'CAT_DECIMALS', 3)
            cr = get_wallet_balance(cat_wid)
            if cr and cr.get("success"):
                wb = cr.get("wallet_balance") or {}
                cat_bal["total"] = _safe_float(wb.get("confirmed_wallet_balance", 0)) / (10 ** cat_dec)
                cat_bal["spendable"] = _safe_float(wb.get("spendable_balance", 0)) / (10 ** cat_dec)
        except Exception:
            pass
        return jsonify({
            "success": True,
            "balances": {
                "xch": xch_bal,
                "cat": cat_bal,
            }
        })
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/settings/defaults")
def api_settings_defaults():
    """Get default settings (current config as defaults for GUI)."""
    d = _serialize_dict(cfg.to_dict())
    d["success"] = True
    return jsonify(d)


@app.route("/api/settings/validate", methods=["POST"])
def api_settings_validate():
    """Validate config settings before saving."""
    data = request.get_json(silent=True)

    if not isinstance(data, dict):

        return jsonify({"success": False, "error": "Invalid request body"}), 400
    errors = []
    warnings = []

    def _get_first(*keys):
        for key in keys:
            if key in data:
                return data.get(key)
        return None

    def _decimal_value(*keys):
        raw = _get_first(*keys)
        if raw in (None, ""):
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None

    def _bool_value(*keys):
        raw = _get_first(*keys)
        if raw is None:
            return None
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    # Basic validation
    if "SPREAD_BPS" in data:
        try:
            spread = float(data["SPREAD_BPS"])
            if spread < 10:
                warnings.append("Spread below 0.1% is very tight — high risk of adverse selection")
            if spread > 2000:
                warnings.append("Spread above 20% — offers unlikely to fill")
        except ValueError:
            errors.append("SPREAD_BPS must be a number")

    if "NUM_OFFERS" in data:
        try:
            n = int(data["NUM_OFFERS"])
            if n < 1:
                errors.append("NUM_OFFERS must be at least 1")
            if n > 50:
                warnings.append("More than 50 offers per side requires many coins")
        except ValueError:
            errors.append("NUM_OFFERS must be an integer")

    fee_mode = str(data.get("transaction_fee_mode", "auto") or "auto").strip().lower()
    if fee_mode not in {"auto", "manual"}:
        errors.append("Transaction fee mode must be auto or manual")

    try:
        fee_xch = Decimal(str(data.get("transaction_fee_xch", "0") or "0"))
        if fee_xch < 0:
            errors.append("Transaction fee must be zero or greater")
    except Exception:
        errors.append("Transaction fee must be a valid XCH amount")
        fee_xch = Decimal("0")

    try:
        fee_count = int(data.get("fee_prep_count", 0) or 0)
        if fee_count < 0:
            errors.append("Fee prep count must be zero or greater")
    except Exception:
        errors.append("Fee prep count must be an integer")
        fee_count = 0

    try:
        fee_coin_size = Decimal(str(data.get("fee_coin_size_xch", "0") or "0"))
        if fee_coin_size < 0:
            errors.append("Fee coin size must be zero or greater")
    except Exception:
        errors.append("Fee coin size must be a valid XCH amount")
        fee_coin_size = Decimal("0")

    if fee_count > 0 and fee_coin_size <= 0:
        errors.append("Fee coin size must be greater than zero when fee prep count is enabled")

    if fee_mode == "manual" and fee_xch > 0 and fee_coin_size > 0 and fee_coin_size <= fee_xch:
        warnings.append("Fee coin size should usually be larger than the manual fee so change can recycle into the fee pool")

    # Dynamic spread validation
    base_spread = _decimal_value("base_spread_bps", "BASE_SPREAD_BPS")
    min_edge = _decimal_value("min_edge_bps", "MIN_EDGE_BPS")
    min_spread = _decimal_value("min_spread_bps", "MIN_SPREAD_BPS")
    max_spread = _decimal_value("max_spread_bps", "MAX_SPREAD_BPS")
    vol_window = _decimal_value("volatility_window_hours", "VOLATILITY_WINDOW_HOURS")
    skew_intensity = _decimal_value("skew_intensity", "SKEW_INTENSITY")
    max_position = _decimal_value("max_position_xch", "MAX_POSITION_XCH")
    default_trade_xch = _decimal_value("default_trade_xch", "DEFAULT_TRADE_XCH")
    sniper_rearm_price_move = _decimal_value("sniper_rearm_price_move_bps", "SNIPER_REARM_PRICE_MOVE_BPS")
    sniper_rearm_gap_move = _decimal_value("sniper_rearm_gap_move_bps", "SNIPER_REARM_GAP_MOVE_BPS")
    dynamic_enabled = _bool_value("dynamic_spread_enabled", "DYNAMIC_SPREAD_ENABLED")
    inventory_enabled = _bool_value("inventory_enabled", "INVENTORY_ENABLED")
    competitor_enabled = _bool_value("competitor_aware_enabled", "COMPETITOR_AWARE_ENABLED")

    if base_spread is not None:
        if base_spread <= 0:
            errors.append("Base spread must be greater than zero")
        elif base_spread < Decimal("200"):
            warnings.append("Base spread below 2% is very aggressive for live market making")
        elif base_spread > Decimal("1500"):
            warnings.append("Base spread above 15% is very wide and can stall fills")

    if min_edge is not None and min_edge < 0:
        errors.append("Inner edge must be zero or greater")

    if min_spread is not None:
        if min_spread <= 0:
            errors.append("Min spread must be greater than zero")

    if max_spread is not None:
        if max_spread <= 0:
            errors.append("Max spread must be greater than zero")

    if min_spread is not None and max_spread is not None and max_spread < min_spread:
        errors.append("Max spread must be greater than or equal to min spread")

    if min_edge is not None:
        required_outer = min_edge * Decimal("1.5")
        if max_spread is not None and max_spread < required_outer:
            errors.append("Max spread must be at least 1.5× the inner edge")
        if min_spread is not None and min_spread < required_outer:
            warnings.append("Min spread is below the ladder safety floor and will be clamped up at runtime")
        if base_spread is not None and base_spread < required_outer:
            warnings.append("Base spread is below the ladder safety floor and will be clamped up at runtime")

    if base_spread is not None and min_spread is not None and base_spread < min_spread:
        warnings.append("Base spread is below min spread and will be clamped up at runtime")
    if base_spread is not None and max_spread is not None and base_spread > max_spread:
        warnings.append("Base spread is above max spread and will be clamped down at runtime")

    if vol_window is not None:
        if vol_window <= 0:
            errors.append("Volatility window must be greater than zero")
        elif vol_window < Decimal("1"):
            warnings.append("Volatility window below 1 hour will make spreads very reactive")
        elif vol_window > Decimal("24"):
            warnings.append("Volatility window above 24 hours will make spreads slow to adapt")

    if skew_intensity is not None:
        if skew_intensity < 0:
            errors.append("Skew intensity must be zero or greater")
        elif skew_intensity > 1:
            errors.append("Skew intensity must be 1.0 or lower")
        elif skew_intensity > Decimal("0.7"):
            warnings.append("Skew intensity above 0.7 is aggressive and can swing buy/sell spreads sharply")

    if max_position is not None:
        if max_position < 0:
            errors.append("Max position must be zero or greater")
        elif max_position == 0:
            warnings.append("Max position set to 0 disables position-limit protection")
            if inventory_enabled:
                warnings.append("Inventory management is enabled, but max position 0 effectively disables skew and side protection")
        elif default_trade_xch is not None and default_trade_xch > 0 and max_position < default_trade_xch:
            warnings.append("Max position is smaller than one normal trade size, so inventory protection may trip very quickly")
        elif bot and getattr(bot, "risk_manager", None):
            try:
                current_mid = getattr(bot, "_current_mid_price", Decimal("0")) or Decimal("0")
                if current_mid <= 0 and getattr(bot, "price_engine", None):
                    current_mid = Decimal(str(bot.price_engine.get_last_price() or 0))
                current_pos_cat = Decimal(
                    str(bot.risk_manager.get_inventory_state().get("net_position_cat", "0"))
                )
                current_pos_xch = abs(current_pos_cat * current_mid) if current_mid > 0 else Decimal("0")
                if current_pos_xch > 0:
                    if current_pos_xch > max_position:
                        warnings.append(
                            f"Current position is already {current_pos_xch:.2f} XCH, above the new max position"
                        )
                    elif current_pos_xch >= max_position * Decimal("0.8"):
                        warnings.append(
                            f"Current position is {current_pos_xch:.2f} XCH, close to the new max position"
                        )
            except Exception:
                pass

    if sniper_rearm_price_move is not None:
        if sniper_rearm_price_move < 0:
            errors.append("Sniper re-arm price move must be zero or greater")
        elif sniper_rearm_price_move == 0:
            warnings.append("Sniper re-arm price move of 0% makes sniper re-arm on every qualifying gap")
        elif sniper_rearm_price_move < Decimal("25"):
            warnings.append("Sniper re-arm price move below 0.25% may create frequent tiny probes")

    if sniper_rearm_gap_move is not None:
        if sniper_rearm_gap_move < 0:
            errors.append("Sniper re-arm arb gap move must be zero or greater")
        elif sniper_rearm_gap_move == 0:
            warnings.append("Sniper re-arm arb gap move of 0% makes sniper re-arm on every qualifying gap")
        elif sniper_rearm_gap_move < Decimal("25"):
            warnings.append("Sniper re-arm arb gap move below 0.25% may create frequent tiny probes")

    if dynamic_enabled is False and (inventory_enabled or competitor_enabled):
        warnings.append(
            "Dynamic spreads off only disables volatility, fill-rate, arb-gap, and pool-depth scaling; "
            "inventory skew and competitor nudges still apply if those features stay enabled"
        )

    return jsonify({
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    })


# ---------------------------------------------------------------------------
# Check Resume (GUI startup)
# ---------------------------------------------------------------------------

def _resume_last_active_label(offers: list) -> str:
    """Return a human-readable 'last active' string from the most recent offer timestamp."""
    from datetime import datetime, timezone
    best = None
    for o in offers:
        ts = o.get("creation_timestamp") or o.get("created_at") or ""
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if best is None or dt > best:
                best = dt
        except Exception:
            pass
    if best is None:
        return "Previous session"
    now = datetime.now(timezone.utc)
    diff = now - best
    minutes = int(diff.total_seconds() // 60)
    if minutes < 2:
        return "Active just now"
    if minutes < 60:
        return f"Last active {minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"Last active {hours}h ago"
    days = hours // 24
    return f"Last active {days}d ago"


@app.route("/api/check-resume")
def api_check_resume():
    """Check if there are existing offers from a previous session.

    Returns can_resume + offer details so the GUI can show a resume modal.
    """
    # If the bot is already running (page refresh mid-session), nothing to resume.
    if bot and getattr(bot, "_loop_count", 0) > 0:
        return jsonify({"can_resume": False, "has_session": True,
                        "buy_count": 0, "sell_count": 0, "reason": "bot_already_running"})
    # If the user already chose "Start Fresh" this process lifetime, don't
    # re-show the resume modal on subsequent page loads (e.g. hot-reload in
    # --dev mode).  The flag is cleared when the bot actually starts.
    if _fresh_start_is_set():
        return jsonify({"can_resume": False, "has_session": False,
                        "buy_count": 0, "sell_count": 0, "reason": "fresh_start_chosen"})
    try:
        from wallet import get_all_offers, classify_offers_from_list
        asset_id = _active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
        offers = get_all_offers(include_completed=False, start=0, end=200)
        if not offers:
            return jsonify({"can_resume": False, "has_session": False,
                            "buy_count": 0, "sell_count": 0, "reason": "no offers"})

        open_buy, open_sell, _ = classify_offers_from_list(offers, asset_id)
        total = len(open_buy) + len(open_sell)
        can_resume = total > 0

        # Build saved settings summary from current config
        saved = {}
        if hasattr(cfg, "DEFAULT_TRADE_XCH"):
            saved["trade_xch"] = str(cfg.DEFAULT_TRADE_XCH)
        if hasattr(cfg, "MAX_ACTIVE_BUY"):
            saved["max_buy"] = int(cfg.MAX_ACTIVE_BUY)
        if hasattr(cfg, "MAX_ACTIVE_SELL"):
            saved["max_sell"] = int(cfg.MAX_ACTIVE_SELL)
        if hasattr(cfg, "SPREAD_BPS"):
            saved["spread_bps"] = float(cfg.SPREAD_BPS)
        saved["cat_name"] = _active_cat.get("name") or getattr(cfg, "CAT_NAME", "CAT")
        saved["cat_asset_id"] = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        saved["cat_wallet_id"] = _active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", None)
        saved["cat_decimals"] = _active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
        saved["cat_ticker_id"] = _active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")

        # ---- Detect gap closer activity ----
        # Two strategies: (1) check DB for open boost offers, (2) check events
        # for recent gap closer activity (more reliable since step-cancels
        # mark old offers as 'cancelled' even though gap closer is still active)
        gap_closer_info = {"active": False, "count": 0}
        try:
            from database import get_connection
            import json as _json
            db = get_connection()

            # Strategy 1: Check DB for open boost offers
            boost_count = 0
            try:
                from database import get_open_offers
                boost_offers = get_open_offers(cat_asset_id=asset_id)
                boost_count = sum(1 for o in boost_offers if o.get("tier") == "boost")
                print(f"[RESUME] DB open boost offers: {boost_count}", flush=True)
            except Exception:
                pass

            # Strategy 2: Check for recent gap closer events (last 2 hours)
            # This is more reliable — if there's a recent activation/step event
            # without a deactivation after it, the gap closer was active
            gc_event = None
            gc_deactivated = False
            try:
                # Check for most recent gap closer event
                row = db.execute(
                    "SELECT event_type, data, timestamp FROM events "
                    "WHERE event_type IN ('gap_closer_step', 'gap_closer_arbed', "
                    "  'gap_closer_activated', 'gap_closer_deactivated') "
                    "ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row:
                    evt_type = row[0]
                    evt_data_str = row[1]
                    evt_ts = row[2]
                    print(f"[RESUME] Latest gap closer event: {evt_type} at {evt_ts}", flush=True)

                    if evt_type == "gap_closer_deactivated":
                        gc_deactivated = True
                    else:
                        # Active event found — check if it's recent (within 2 hours)
                        from datetime import datetime, timezone, timedelta
                        try:
                            evt_time = datetime.fromisoformat(evt_ts.replace("Z", "+00:00"))
                            age = datetime.now(timezone.utc) - evt_time
                            if age < timedelta(hours=2):
                                gc_event = evt_data_str
                                print(f"[RESUME] Gap closer was active ({age.seconds//60}min ago)", flush=True)
                            else:
                                print(f"[RESUME] Gap closer event too old ({age})", flush=True)
                        except Exception:
                            gc_event = evt_data_str  # Can't parse time, assume recent
            except Exception as e:
                print(f"[RESUME] Event check error: {e}", flush=True)

            # Determine gap closer state
            if boost_count > 0 or (gc_event and not gc_deactivated):
                gap_closer_info["active"] = True
                gap_closer_info["count"] = max(boost_count, 2)  # At least 2 (buy+sell pair)

                # Extract spread data from event
                if gc_event:
                    try:
                        evt_data = _json.loads(gc_event) if isinstance(gc_event, str) else gc_event
                        if evt_data and isinstance(evt_data, dict):
                            if evt_data.get("spread_bps"):
                                gap_closer_info["last_spread_bps"] = int(evt_data["spread_bps"])
                            if evt_data.get("arb_floor_bps"):
                                gap_closer_info["arb_floor_bps"] = int(evt_data["arb_floor_bps"])
                            if evt_data.get("steps_taken"):
                                gap_closer_info["steps_taken"] = int(evt_data["steps_taken"])
                    except Exception:
                        pass

                print(f"[RESUME] Gap closer info: {gap_closer_info}", flush=True)
        except Exception as e:
            print(f"[RESUME] Gap closer detection error: {e}", flush=True)

        return jsonify({
            "can_resume": can_resume,
            "has_session": can_resume,
            "buy_count": len(open_buy),
            "sell_count": len(open_sell),
            "offer_count": total,
            "saved_settings": saved,
            "active_cat": {
                "asset_id": saved.get("cat_asset_id") or "",
                "wallet_id": saved.get("cat_wallet_id"),
                "decimals": saved.get("cat_decimals"),
                "ticker_id": saved.get("cat_ticker_id") or "",
                "name": saved.get("cat_name") or "CAT",
            },
            "gap_closer": gap_closer_info,
            "last_active": _resume_last_active_label(open_buy + open_sell),
        })
    except Exception as e:
        log_event("error", "api_error", f"Resume session check failed: {e}", {"endpoint": request.path})
        return jsonify({"can_resume": False, "has_session": False,
                        "error": "Internal server error", "code": "SERVER_ERROR"})


# ---------------------------------------------------------------------------
# Coin Prep Routes (GUI coin preparation flow)
# ---------------------------------------------------------------------------

_coin_prep_state = {
    "running": False,
    "complete": False,
    "error": None,
    "started_at": None,
    "xch_coins": 0,
    "cat_coins": 0,
    "xch_needed": 0,
    "cat_needed": 0,
}
_coin_prep_proc = None  # Global ref to subprocess — used to kill old worker on re-trigger


def _new_cancel_all_state():
    return {
        "running": False,
        "complete": False,
        "error": None,
        "phase": "idle",
        "message": "",
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
        "total": 0,
        "batch_size": 0,
        "total_batches": 0,
        "current_batch": 0,
        "batch_cancelled": 0,
        "batch_failed": 0,
        "cancelled": 0,
        "failed": 0,
    }


_cancel_all_state = _new_cancel_all_state()
_cancel_all_state_lock = threading.Lock()


def _set_cancel_all_state(**updates):
    with _cancel_all_state_lock:
        _cancel_all_state.update(updates)
        _cancel_all_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return dict(_cancel_all_state)


def _reset_cancel_all_state(**updates):
    with _cancel_all_state_lock:
        _cancel_all_state.clear()
        _cancel_all_state.update(_new_cancel_all_state())
        _cancel_all_state.update(updates)
        _cancel_all_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return dict(_cancel_all_state)


def _get_cancel_all_state():
    with _cancel_all_state_lock:
        return dict(_cancel_all_state)


@app.route("/api/log", methods=["POST"])
def api_log_event():
    """Receive log messages from subprocesses (e.g. coin prep worker).

    The coin prep worker runs in a separate process and can't access the SSE
    event bus directly. It POSTs log messages here, and we push them to the
    live console via SSE + write to the database.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        severity = payload.get("severity", "info")
        event_type = payload.get("event_type", "coin_prep")
        message = payload.get("message", "")

        if not message:
            return jsonify({"success": False, "error": "No message"}), 400

        # Write to DB + push to SSE (log_event does both now)
        from database import log_event
        log_event(severity, event_type, message)

        # Emit a coin_change SSE event when coin prep hits key milestones
        # so the Chia dashboard can auto-refresh Coins/Balances/Wallet Status
        if event_type == "coin_prep":
            coin_keywords = ["confirmed", "split", "consolidat", "pool",
                             "coins)", "coin)", "COMPLETE", "verified"]
            if any(kw.lower() in message.lower() for kw in coin_keywords):
                events.emit("coin_change", {
                    "source": "coin_prep",
                    "message": message[:200],
                })

        return jsonify({"success": True})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/coin-prep/status")
def api_coin_prep_status():
    """Get coin preparation status.

    Reads live progress from the worker's coin_prep_status.json file
    (the subprocess writes phase/progress/message there).
    Falls back to the in-memory _coin_prep_state for basic running/complete flags.
    """
    try:
        result = {"success": True, **_coin_prep_state}

        def _refresh_finished_prep_coin_counts(payload: dict):
            """Backfill current coin counts after prep stops.

            The worker status file records in-run snapshots. On error or completion
            those counts can lag the final stopped-state inventory badly, so prefer
            the same read-only DB/RPC sources used by the other API endpoints.
            """
            if payload.get("running"):
                return

            is_complete = bool(payload.get("complete")) or str(payload.get("phase") or "") == "complete"

            try:
                from database import get_coin_summary

                summary = get_coin_summary() or {}
            except Exception:
                summary = {}

            if summary:
                xch_free = int(summary.get("xch_free_count", 0) or 0)
                cat_free = int(summary.get("cat_free_count", 0) or 0)
                payload["xch_free_coins"] = xch_free
                payload["cat_free_coins"] = cat_free
                if is_complete:
                    payload["xch_coins"] = int(summary.get("xch_total", xch_free) or 0)
                    payload["cat_coins"] = int(summary.get("cat_total", cat_free) or 0)
                else:
                    payload["xch_coins"] = xch_free
                    payload["cat_coins"] = cat_free
                return

            if bot and getattr(bot, "coin_manager", None):
                try:
                    xch, cat = bot.coin_manager.get_coin_health()
                    payload["xch_free_coins"] = int(xch or 0)
                    payload["cat_free_coins"] = int(cat or 0)
                    payload["xch_coins"] = int(xch or 0)
                    payload["cat_coins"] = int(cat or 0)
                    return
                except Exception:
                    pass

            try:
                from wallet import get_spendable_coin_count, WALLET_ID_XCH

                payload["xch_coins"] = int(get_spendable_coin_count(WALLET_ID_XCH) or 0)
                payload["xch_free_coins"] = payload["xch_coins"]
                cat_wallet_id = getattr(cfg, "CAT_WALLET_ID", None) or getattr(bot, "cat_wallet_id", None)
                if cat_wallet_id:
                    payload["cat_coins"] = int(get_spendable_coin_count(int(cat_wallet_id)) or 0)
                    payload["cat_free_coins"] = payload["cat_coins"]
            except Exception:
                pass

        # Read live progress from the worker's status file (V1 parity)
        status_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "coin_prep_status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file, "r") as f:
                    worker_status = json.load(f)

                # Check if this status file belongs to the CURRENT run.
                # If it has a different run_id (or none), it's stale from
                # a previous run — ignore completion/error from it.
                current_run_id = _coin_prep_state.get("run_id")
                file_run_id = worker_status.get("run_id")
                is_current_run = (
                    current_run_id  # There IS an active run
                    and (
                        not file_run_id  # Old-format file — trust if running
                        or file_run_id == current_run_id  # Same run
                    )
                )

                # Only overlay the worker status when it belongs to the
                # current run, or when there is no active/newer run in memory.
                if is_current_run:
                    result["phase"] = worker_status.get("phase", result.get("phase", "idle"))
                    result["progress"] = worker_status.get("progress", 0)
                    result["message"] = worker_status.get("message", "")
                    result["xch_coins"] = worker_status.get("xch_coins_current", 0)
                    result["cat_coins"] = worker_status.get("cat_coins_current", 0)
                    result["xch_target"] = worker_status.get("xch_coins_target", 0)
                    result["cat_target"] = worker_status.get("cat_coins_target", 0)
                    w_error = worker_status.get("error")
                    if w_error:
                        result["error"] = w_error

                    # Detect completion/error from worker status — but ONLY
                    # if the status file belongs to the current run.
                    if worker_status.get("phase") == "complete":
                        result["complete"] = True
                        result["running"] = False
                        _coin_prep_state["complete"] = True
                        _coin_prep_state["running"] = False
                    elif worker_status.get("phase") == "error":
                        result["running"] = False
                        _coin_prep_state["running"] = False
                        _coin_prep_state["error"] = w_error
                else:
                    # Stale status file from previous run.
                    if _coin_prep_state["running"]:
                        # New run just started — ignore stale file
                        result["phase"] = "idle"
                        result["progress"] = 0
                        result["message"] = "Starting coin preparation..."
                        result["complete"] = False
                    elif worker_status.get("phase") == "complete":
                        # Previous run completed — verify the wallet still
                        # has coins of the RIGHT SIZES before claiming
                        # "already done".  Uses coin_prep_last.json (which
                        # stores the tier sizes/counts from the last
                        # successful prep) + wallet RPC to do disjoint
                        # size-matching with 5 % tolerance — same logic
                        # the /api/coin-prep/verify endpoint uses.
                        _prev_ok = False
                        _matched_xch = 0
                        _matched_cat = 0
                        _target_xch = 0
                        _target_cat = 0
                        try:
                            _prep_path = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                "coin_prep_last.json",
                            )
                            if os.path.exists(_prep_path):
                                with open(_prep_path, "r") as _pf:
                                    _last = json.load(_pf)

                                from wallet import get_spendable_coins_rpc, WALLET_ID_XCH
                                from config import cfg as _cfg

                                _cat_wid = int(
                                    _active_cat.get("wallet_id")
                                    or getattr(_cfg, "CAT_WALLET_ID", 2)
                                    or 2
                                )
                                _cat_dec = int(
                                    _active_cat.get("decimals")
                                    or getattr(_cfg, "CAT_DECIMALS", 3)
                                )
                                _tol = 0.05

                                # Fetch spendable coins from wallet
                                _xr = get_spendable_coins_rpc(WALLET_ID_XCH)
                                _cr = get_spendable_coins_rpc(_cat_wid)
                                _xch_coins = [
                                    r.get("coin", {}).get("amount", 0)
                                    for r in (_xr or {}).get("records", [])
                                    if r.get("coin", {}).get("amount", 0) > 0
                                ] if _xr and _xr.get("success") else []
                                _cat_coins = [
                                    r.get("coin", {}).get("amount", 0)
                                    for r in (_cr or {}).get("records", [])
                                    if r.get("coin", {}).get("amount", 0) > 0
                                ] if _cr and _cr.get("success") else []

                                def _alloc_match(coins_list, requests, tol):
                                    """Allocate coins disjointly to tiers."""
                                    remaining = list(coins_list)
                                    allocated = {}
                                    reqs = list(enumerate(requests))
                                    reqs.sort(key=lambda x: (-x[1][1], x[0]))
                                    for _, (tier, target_m, needed) in reqs:
                                        if target_m <= 0 or needed <= 0:
                                            allocated[tier] = 0
                                            continue
                                        lo = int(target_m * (1 - tol))
                                        hi = int(target_m * (1 + tol))
                                        hits = [i for i, a in enumerate(remaining) if lo <= a <= hi]
                                        take = min(needed, len(hits))
                                        allocated[tier] = take
                                        for i in reversed(hits[:take]):
                                            remaining.pop(i)
                                    return allocated

                                _all_ok = True
                                if _last.get("tier_enabled"):
                                    _tsxch = _last.get("tier_sizes_xch", {})
                                    _tscat = _last.get("tier_sizes_cat", {})
                                    _tc = _last.get("tier_counts", {})
                                    _xreqs = []
                                    _creqs = []
                                    for _t, _cnt in _tc.items():
                                        _cnt = int(_cnt or 0)
                                        _xsz = float(_tsxch.get(_t, 0))
                                        _csz = float(_tscat.get(_t, 0))
                                        _target_xch += _cnt
                                        if _csz > 0:
                                            _target_cat += _cnt
                                        if _xsz > 0 and _cnt > 0:
                                            _xreqs.append((_t, int(_xsz * 1e12), _cnt))
                                        if _csz > 0 and _cnt > 0:
                                            _creqs.append((_t, int(_csz * (10 ** _cat_dec)), _cnt))
                                    _xa = _alloc_match(_xch_coins, _xreqs, _tol)
                                    _ca = _alloc_match(_cat_coins, _creqs, _tol)
                                    for _t, _cnt in _tc.items():
                                        _cnt = int(_cnt or 0)
                                        if _cnt <= 0:
                                            continue
                                        _xsz = float(_tsxch.get(_t, 0))
                                        _csz = float(_tscat.get(_t, 0))
                                        if _xsz > 0 and _xa.get(_t, 0) < _cnt:
                                            _all_ok = False
                                        if _csz > 0 and _ca.get(_t, 0) < _cnt:
                                            _all_ok = False
                                    _matched_xch = sum(_xa.values())
                                    _matched_cat = sum(_ca.values())
                                else:
                                    # Flat mode
                                    _xsz = float(_last.get("xch_coin_size") or _last.get("prepared_trade_size_xch") or 0)
                                    _csz = float(_last.get("cat_coin_size") or 0)
                                    _xt = int(_last.get("xch_target") or 0)
                                    _ct = int(_last.get("cat_target") or 0)
                                    _target_xch = _xt
                                    _target_cat = _ct
                                    if _xsz > 0 and _xt > 0:
                                        _xm = int(_xsz * 1e12)
                                        _lo = int(_xm * (1 - _tol))
                                        _hi = int(_xm * (1 + _tol))
                                        _matched_xch = sum(1 for c in _xch_coins if _lo <= c <= _hi)
                                        if _matched_xch < _xt:
                                            _all_ok = False
                                    if _csz > 0 and _ct > 0:
                                        _cm = int(_csz * (10 ** _cat_dec))
                                        _lo = int(_cm * (1 - _tol))
                                        _hi = int(_cm * (1 + _tol))
                                        _matched_cat = sum(1 for c in _cat_coins if _lo <= c <= _hi)
                                        if _matched_cat < _ct:
                                            _all_ok = False

                                _prev_ok = _all_ok and (_target_xch > 0 or _target_cat > 0)
                        except Exception:
                            _prev_ok = False

                        if _prev_ok:
                            result["phase"] = "complete"
                            result["complete"] = True
                            result["xch_coins"] = _matched_xch
                            result["cat_coins"] = _matched_cat
                            result["xch_target"] = _target_xch
                            result["cat_target"] = _target_cat
                            result["previously_complete"] = True
                        # else: stale file + wallet doesn't have right
                        # coin sizes → ignore, result stays idle
            except (json.JSONDecodeError, IOError):
                pass  # File being written — skip this poll

        # Also check if the subprocess is still alive (via coin_manager)
        if _coin_prep_state["running"] and bot:
            prep_status = bot.coin_manager.check_coin_prep_status()
            if not prep_status.get("running") and not result.get("phase") == "complete":
                # Subprocess exited but we didn't see "complete" in status file
                exit_code = prep_status.get("exit_code")
                if exit_code is not None and exit_code != 0:
                    result["phase"] = "error"
                    result["error"] = f"Worker exited with code {exit_code}"
                    _coin_prep_state["running"] = False
                    _coin_prep_state["error"] = result["error"]

        _refresh_finished_prep_coin_counts(result)

        # Optionally refresh live coin counts (when not actively prepping)
        refresh = request.args.get("refresh", "false").lower() == "true"
        if refresh and bot and not _coin_prep_state["running"]:
            try:
                xch, cat = bot.coin_manager.get_coin_health()
                result["xch_coins"] = xch
                result["cat_coins"] = cat
            except Exception:
                pass

        # Include last successful prep settings (for smart skip detection)
        prep_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "coin_prep_last.json")
        if os.path.exists(prep_json_path):
            try:
                with open(prep_json_path, "r") as f:
                    result["last_prep_settings"] = json.load(f)
            except (json.JSONDecodeError, IOError):
                result["last_prep_settings"] = None
        else:
            result["last_prep_settings"] = None

        # Include the recent coin prep transcript for the inline console.
        # We prefer DB-backed events because that captures both structured
        # coin_prep logs and raw worker stdout mirrored via /api/log.
        try:
            from database import get_events_since, get_recent_events

            prep_cutoff = (
                _coin_prep_state.get("started_at")
                or _session_start_time
            )
            if prep_cutoff:
                recent_events = get_events_since(prep_cutoff, limit=600)
            else:
                recent_events = get_recent_events(limit=600)

            prep_events = [
                evt for evt in reversed(recent_events)
                if str(evt.get("event_type", "")).startswith("coin_prep")
            ]

            result["log_lines"] = [
                f"{str(evt.get('timestamp', ''))[11:19]} "
                f"[{str(evt.get('severity', 'info')).upper()}] "
                f"{evt.get('message', '')}"
                for evt in prep_events[-400:]
            ]
        except Exception:
            result["log_lines"] = []

        return jsonify(result)
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/coin-prep/verify")
def api_coin_prep_verify():
    """Verify if the wallet already has the right coins for the requested prep.

    Fetches spendable coins from the wallet and groups them by amount,
    checking if enough coins of each tier size already exist.

    Query params (tier mode):
      tier_enabled=true
      inner_xch=1.4&mid_xch=0.7&outer_xch=0.35&extreme_xch=0.14
      inner_cat=...&mid_cat=...&outer_cat=...&extreme_cat=...
      inner_count=6&mid_count=18&outer_count=18&extreme_count=18

    Query params (flat mode):
      tier_enabled=false
      trade_size=0.7&prepared_xch_size=0.77&prepared_cat_size=7654&max_buy=25&max_sell=25
    """
    try:
        from wallet import get_spendable_coins_rpc, get_wallet_balance, WALLET_ID_XCH
        from config import cfg

        cat_wallet_id = int(_active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", 2) or 2)
        tier_enabled = request.args.get("tier_enabled", "false").lower() == "true"
        tolerance = 0.05  # 5% tolerance for matching coin sizes

        # Fetch wallet balances for sufficiency check
        # Uses CONFIRMED (total) balance, NOT spendable, because coin prep's
        # first step is to cancel all existing offers — so locked coins WILL
        # become available during prep.
        xch_bal_result = get_wallet_balance(WALLET_ID_XCH)
        cat_bal_result = get_wallet_balance(cat_wallet_id)
        xch_balance_mojos = 0
        cat_balance_mojos = 0
        if xch_bal_result and isinstance(xch_bal_result, dict):
            wb = xch_bal_result.get("wallet_balance") or xch_bal_result
            xch_balance_mojos = wb.get("confirmed_wallet_balance", 0) or wb.get("spendable_balance", 0)
            if isinstance(xch_balance_mojos, str):
                xch_balance_mojos = int(xch_balance_mojos)
        if cat_bal_result and isinstance(cat_bal_result, dict):
            wb = cat_bal_result.get("wallet_balance") or cat_bal_result
            cat_balance_mojos = wb.get("confirmed_wallet_balance", 0) or wb.get("spendable_balance", 0)
            if isinstance(cat_balance_mojos, str):
                cat_balance_mojos = int(cat_balance_mojos)

        # Fetch all spendable coins
        xch_result = get_spendable_coins_rpc(WALLET_ID_XCH)
        cat_result = get_spendable_coins_rpc(cat_wallet_id)

        xch_coins = []
        if xch_result and xch_result.get("success"):
            for r in xch_result.get("records", []):
                amt = r.get("coin", {}).get("amount", 0)
                if amt > 0:
                    xch_coins.append(amt)

        cat_coins = []
        if cat_result and cat_result.get("success"):
            for r in cat_result.get("records", []):
                amt = r.get("coin", {}).get("amount", 0)
                if amt > 0:
                    cat_coins.append(amt)

        cat_decimals = int(_active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3))

        def count_matching(coins_list, target_mojos, tol):
            """Count coins within tolerance of target size."""
            low = int(target_mojos * (1 - tol))
            high = int(target_mojos * (1 + tol))
            return sum(1 for c in coins_list if low <= c <= high)

        def allocate_matching_counts(coins_list, requests, tol):
            """Allocate matching coins disjointly across tiers.

            This avoids double-counting when multiple tiers intentionally share
            the same exact size, like XCH sniper + fees.
            Returns the number of coins allocatable to each tier.
            """
            remaining = list(coins_list)
            allocated = {}
            indexed_requests = list(enumerate(requests))
            indexed_requests.sort(key=lambda item: (-item[1][1], item[0]))

            for _, (tier, target_mojos, needed) in indexed_requests:
                if target_mojos <= 0 or needed <= 0:
                    allocated[tier] = 0
                    continue
                low = int(target_mojos * (1 - tol))
                high = int(target_mojos * (1 + tol))
                matched_positions = [idx for idx, amt in enumerate(remaining) if low <= amt <= high]
                consume = min(needed, len(matched_positions))
                allocated[tier] = consume
                for idx in reversed(matched_positions[:consume]):
                    remaining.pop(idx)

            return allocated

        if tier_enabled:
            tiers = [
                tier for tier in ["inner", "mid", "outer", "extreme", "sniper", "fees"]
                if any(request.args.get(f"{tier}_{suffix}") is not None for suffix in ("xch", "cat", "count"))
            ]
            if not tiers:
                tiers = ["inner", "mid", "outer", "extreme"]
            result_tiers = {}
            all_sufficient = True
            tier_specs = {}
            xch_requests = []
            cat_requests = []

            for tier in tiers:
                xch_size = float(request.args.get(f"{tier}_xch", "0"))
                cat_size = float(request.args.get(f"{tier}_cat", "0"))
                needed = int(request.args.get(f"{tier}_count", "0"))
                is_xch_only_tier = tier == "fees" or cat_size <= 0

                xch_mojos = int(xch_size * 1e12)
                cat_mojos = int(cat_size * (10 ** cat_decimals))
                tier_specs[tier] = {
                    "xch_size": xch_size,
                    "cat_size": cat_size,
                    "needed": needed,
                    "xch_mojos": xch_mojos,
                    "cat_mojos": cat_mojos,
                    "xch_only": is_xch_only_tier,
                }
                if xch_mojos > 0 and needed > 0:
                    xch_requests.append((tier, xch_mojos, needed))
                if not is_xch_only_tier and cat_mojos > 0 and needed > 0:
                    cat_requests.append((tier, cat_mojos, needed))

            xch_allocated = allocate_matching_counts(xch_coins, xch_requests, tolerance)
            cat_allocated = allocate_matching_counts(cat_coins, cat_requests, tolerance)

            for tier in tiers:
                spec = tier_specs[tier]
                needed = spec["needed"]
                xch_have = xch_allocated.get(tier, 0) if spec["xch_mojos"] > 0 else 0
                cat_have = cat_allocated.get(tier, 0) if spec["cat_mojos"] > 0 else 0
                sufficient = (
                    xch_have >= needed and (True if spec["xch_only"] else cat_have >= needed)
                ) if needed > 0 else True
                if not sufficient:
                    all_sufficient = False

                result_tiers[tier] = {
                    "xch_size": spec["xch_size"],
                    "cat_size": spec["cat_size"],
                    "needed": needed,
                    "xch_have": xch_have,
                    "cat_have": cat_have,
                    "xch_only": spec["xch_only"],
                    "sufficient": sufficient,
                }

            # --- Balance sufficiency check ---
            # Calculate total XCH and CAT needed if coin prep were to run
            total_xch_needed_mojos = 0
            total_cat_needed_mojos = 0
            for tier in tiers:
                xch_size = float(request.args.get(f"{tier}_xch", "0"))
                cat_size = float(request.args.get(f"{tier}_cat", "0"))
                needed = int(request.args.get(f"{tier}_count", "0"))
                total_xch_needed_mojos += int(xch_size * 1e12) * needed
                if tier != "fees" and cat_size > 0:
                    total_cat_needed_mojos += int(cat_size * (10 ** cat_decimals)) * needed

            xch_balance_sufficient = xch_balance_mojos >= total_xch_needed_mojos
            cat_balance_sufficient = cat_balance_mojos >= total_cat_needed_mojos

            balance_warnings = []
            if not xch_balance_sufficient and total_xch_needed_mojos > 0:
                xch_need = total_xch_needed_mojos / 1e12
                xch_have = xch_balance_mojos / 1e12
                balance_warnings.append(
                    f"XCH balance too low: need {xch_need:.3f} XCH but only have {xch_have:.3f} XCH"
                )
            if not cat_balance_sufficient and total_cat_needed_mojos > 0:
                cat_unit = 10 ** cat_decimals
                cat_need = total_cat_needed_mojos / cat_unit
                cat_have = cat_balance_mojos / cat_unit
                balance_warnings.append(
                    f"CAT balance too low: need {cat_need:,.0f} CAT but only have {cat_have:,.0f} CAT"
                )

            return jsonify({
                "success": True,
                "tier_enabled": True,
                "tiers": result_tiers,
                "all_sufficient": all_sufficient,
                "xch_total": len(xch_coins),
                "cat_total": len(cat_coins),
                "xch_balance_mojos": xch_balance_mojos,
                "cat_balance_mojos": cat_balance_mojos,
                "xch_needed_mojos": total_xch_needed_mojos,
                "cat_needed_mojos": total_cat_needed_mojos,
                "balance_sufficient": xch_balance_sufficient and cat_balance_sufficient,
                "balance_warnings": balance_warnings,
            })
        else:
            # Flat mode
            trade_size = float(request.args.get("trade_size", "0"))
            prepared_xch_size = float(request.args.get("prepared_xch_size", str(trade_size or 0)))
            prepared_cat_size = float(request.args.get("prepared_cat_size", "0"))
            max_buy = int(request.args.get("max_buy", "0"))
            max_sell = int(request.args.get("max_sell", "0"))
            if prepared_cat_size <= 0:
                prepared_cat_size = trade_size

            xch_mojos = int(prepared_xch_size * 1e12)
            cat_mojos = int(prepared_cat_size * (10 ** cat_decimals))

            xch_right_size = count_matching(xch_coins, xch_mojos, tolerance)
            cat_right_size = count_matching(cat_coins, cat_mojos, tolerance)

            # --- Balance sufficiency check (flat mode) ---
            total_xch_needed_mojos = xch_mojos * max_buy
            total_cat_needed_mojos = cat_mojos * max_sell

            xch_balance_sufficient = xch_balance_mojos >= total_xch_needed_mojos
            cat_balance_sufficient = cat_balance_mojos >= total_cat_needed_mojos

            balance_warnings = []
            if not xch_balance_sufficient and total_xch_needed_mojos > 0:
                xch_need = total_xch_needed_mojos / 1e12
                xch_have = xch_balance_mojos / 1e12
                balance_warnings.append(
                    f"XCH balance too low: need {xch_need:.3f} XCH but only have {xch_have:.3f} XCH"
                )
            if not cat_balance_sufficient and total_cat_needed_mojos > 0:
                cat_unit = 10 ** cat_decimals
                cat_need = total_cat_needed_mojos / cat_unit
                cat_have = cat_balance_mojos / cat_unit
                balance_warnings.append(
                    f"CAT balance too low: need {cat_need:,.0f} CAT but only have {cat_have:,.0f} CAT"
                )

            return jsonify({
                "success": True,
                "tier_enabled": False,
                "xch_coins_right_size": xch_right_size,
                "cat_coins_right_size": cat_right_size,
                "xch_needed": max_buy,
                "cat_needed": max_sell,
                "all_sufficient": (xch_right_size >= max_buy and cat_right_size >= max_sell),
                "xch_total": len(xch_coins),
                "cat_total": len(cat_coins),
                "xch_balance_mojos": xch_balance_mojos,
                "cat_balance_mojos": cat_balance_mojos,
                "xch_needed_mojos": total_xch_needed_mojos,
                "cat_needed_mojos": total_cat_needed_mojos,
                "balance_sufficient": xch_balance_sufficient and cat_balance_sufficient,
                "balance_warnings": balance_warnings,
            })

    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/coin-prep/trigger", methods=["POST"])
def api_coin_prep_trigger():
    """Trigger coin preparation.

    Launches the coin_prep_worker subprocess via coin_manager.
    The worker writes its progress to coin_prep_status.json.
    The /api/coin-prep/status endpoint reads that file for live progress.
    This thread monitors the subprocess and updates running/complete flags.
    """
    try:
        global _coin_prep_proc

        # Read coin_multiplier from request body NOW, while we're still
        # inside the Flask request context. The do_prep() thread runs AFTER
        # the HTTP response is sent, so request.get_json() won't work there.
        try:
            _prep_req_data = request.get_json(silent=True) or {}
            _prep_coin_multiplier = float(_prep_req_data.get("coin_multiplier", 1))
            _prep_coin_multiplier = max(0.5, min(3.0, _prep_coin_multiplier))
        except Exception:
            _prep_coin_multiplier = 1.0
        log_event("info", "coin_prep_multiplier",
                  f"Coin prep multiplier from GUI: {_prep_coin_multiplier}×")

        # If a previous worker is still running, kill it first.
        # Two workers operating on the same wallet simultaneously causes
        # coin conflicts, failed splits, and wallet sync chaos.
        if _coin_prep_proc is not None and _coin_prep_proc.poll() is None:
            old_pid = _coin_prep_proc.pid
            log_event("info", "coin_prep_kill",
                      f"Killing previous coin prep worker (PID: {old_pid}) before starting new run")
            try:
                _coin_prep_proc.terminate()
                # Give it 3 seconds to exit gracefully, then force kill
                try:
                    _coin_prep_proc.wait(timeout=3)
                except Exception:
                    _coin_prep_proc.kill()
                    _coin_prep_proc.wait(timeout=2)
                log_event("info", "coin_prep_killed",
                          f"Previous worker (PID: {old_pid}) terminated")
            except Exception as kill_err:
                log_event("warning", "coin_prep_kill_failed",
                          f"Could not kill PID {old_pid}: {kill_err}")
            _coin_prep_proc = None

        # Also kill any worker launched via coin_manager (bot loop path)
        if bot and hasattr(bot, 'coin_manager') and bot.coin_manager._prep_process:
            cm_proc = bot.coin_manager._prep_process
            if cm_proc.poll() is None:
                cm_pid = cm_proc.pid
                log_event("info", "coin_prep_kill",
                          f"Killing coin_manager worker (PID: {cm_pid}) before starting new run")
                try:
                    cm_proc.terminate()
                    try:
                        cm_proc.wait(timeout=3)
                    except Exception:
                        cm_proc.kill()
                        cm_proc.wait(timeout=2)
                except Exception:
                    pass
                bot.coin_manager._prep_process = None
                bot.coin_manager._prep_running = False

        # ---- FRESH START: Clear old session data ----
        # Coin prep means a full reset — cancel all, re-split, start fresh.
        # Old fills, offers, and coin records are stale and would cause the
        # bot to inherit wrong position limits and phantom fill history.
        try:
            _reset_fresh_run_session(
                clear_coins=True,
                clear_price_history=True,
                clear_inventory=True,
                cancel_open_offers=True,
                reason="fresh_start_cleanup",
            )
        except Exception as _clean_err:
            log_event("warning", "fresh_start_cleanup_failed",
                      f"DB cleanup before coin prep failed: {_clean_err}")

        # Balance gate removed — the /api/coin-prep/verify endpoint already checks
        # balance accurately before the confirm button is shown, and uses the same
        # coin plan formula as the GUI. The old formula here (c * 2 * mult) was
        # overcalculating required XCH and blocking valid runs at higher multipliers.

        # Generate a unique run ID so we can distinguish old completions from new runs
        import uuid as _uuid
        run_id = str(_uuid.uuid4())[:8]

        _coin_prep_state["running"] = True
        _coin_prep_state["complete"] = False
        _coin_prep_state["error"] = None
        _coin_prep_state["phase"] = "idle"
        _coin_prep_state["run_id"] = run_id
        _coin_prep_state["started_at"] = datetime.now(timezone.utc).isoformat()

        # CRITICAL: Stop the bot loop entirely during coin prep.
        # Just setting _prep_running is NOT enough — the bot loop's
        # requote step also creates offers, and any running cycle
        # may already be mid-execution. The only safe approach is
        # to fully stop the bot. User must press "Start Bot" after
        # coin prep completes.
        if bot and bot.is_running():
            bot.stop()
            log_event("info", "coin_prep_bot_stopped",
                      "Bot loop STOPPED for coin prep — press Start Bot after prep completes")
            events.emit("bot_control", {"action": "stopped",
                                        "reason": "coin_prep"})

        # Also set the flag as a safety belt
        if bot and hasattr(bot, 'coin_manager'):
            bot.coin_manager._prep_running = True
            log_event("info", "coin_prep_gate",
                      "Coin manager marked busy for coin prep")

        # Write a fresh "starting" status file immediately.
        # This prevents the GUI from reading stale COMPLETE status
        # from a previous run during the gap before the subprocess starts.
        status_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "coin_prep_status.json")
        try:
            fresh_status = {
                "phase": "idle",
                "progress": 0.0,
                "message": "Starting coin preparation...",
                "xch_coins_current": 0,
                "cat_coins_current": 0,
                "xch_coins_target": 0,
                "cat_coins_target": 0,
                "error": None,
                "timestamp": time.time(),
                "run_id": run_id
            }
            with open(status_file, "w") as f:
                json.dump(fresh_status, f, indent=2)
        except Exception:
            # If we can't write, at least try to delete the old one
            try:
                if os.path.exists(status_file):
                    os.remove(status_file)
            except Exception:
                pass

        def do_prep():
            global _coin_prep_proc
            prep_succeeded = False
            try:
                # Launch worker without a visible console window.
                # We rely on the DB/superlog/log file for debugging instead of
                # popping a Windows terminal in front of the GUI.
                import subprocess as _sp
                worker_dir = os.path.dirname(os.path.abspath(__file__))
                worker_path = os.path.join(worker_dir, "coin_prep_worker.py")

                if not os.path.exists(worker_path):
                    _coin_prep_state["error"] = "coin_prep_worker.py not found"
                    _coin_prep_state["running"] = False
                    return

                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"

                # Build CLI args from LIVE config so the worker uses the
                # actual GUI settings, not stale .env values.
                # Double up: buy+sell per side for spares (requotes, sniping)
                max_buy = getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25)
                max_sell = getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25)
                trade_xch = str(getattr(cfg, "DEFAULT_TRADE_XCH", "0.5"))

                if getattr(cfg, "TIER_ENABLED", False):
                    # Tier-aware coin prep with PER-SIDE counts. Buy ladder is XCH-funded
                    # (BUY_*_TIER_COUNT + spares); sell ladder is CAT-funded (SELL_*_TIER_COUNT
                    # + spares). The worker uses these independently, so asymmetric ladders
                    # (e.g. 3 buy inner + 10 sell inner) prep the right number of coins on
                    # each side instead of forcing both sides to the larger value.
                    #
                    # F62 (2026-04-09): tier SIZES are also per-side now. XCH
                    # coins use buy sizes; CAT coins use sell sizes. When the
                    # per-side fields aren't set the helpers fall back to the
                    # legacy shared sizes with reverse-buy flipping.
                    #
                    # F62b (2026-04-09): the worker's tier_counts come from
                    # `_flip_tiers(buy_position_counts, side="buy")` below,
                    # which are SIZE-INDEXED (under reverse-buy, buy position
                    # inner → size extreme slot). So the sizes dict we hand
                    # the worker must ALSO be size-indexed, otherwise the
                    # count × size product multiplies mismatched pairs and
                    # blows up the pool by 2x. Apply the reverse-buy flip to
                    # the size dict so it's consistent with the counts.
                    from config import get_buy_tier_size_xch, get_sell_tier_size_xch
                    # Launcher is in a separate function from Smart Settings,
                    # so `_buy_ladder_reversed` isn't in scope — read directly
                    # from config here.
                    _buy_ladder_reversed = bool(getattr(cfg, "BUY_LADDER_REVERSED", False))
                    # Position-semantic buy sizes (from per-side helpers):
                    _buy_inner_pos = Decimal(str(get_buy_tier_size_xch("inner")   or getattr(cfg, "INNER_SIZE_XCH", Decimal("1.0"))))
                    _buy_mid_pos   = Decimal(str(get_buy_tier_size_xch("mid")     or getattr(cfg, "MID_SIZE_XCH", Decimal("0.5"))))
                    _buy_outer_pos = Decimal(str(get_buy_tier_size_xch("outer")   or getattr(cfg, "OUTER_SIZE_XCH", Decimal("0.25"))))
                    _buy_extr_pos  = Decimal(str(get_buy_tier_size_xch("extreme") or getattr(cfg, "EXTREME_SIZE_XCH", Decimal("0.1"))))
                    if _buy_ladder_reversed:
                        # Under reverse-buy, SIZE inner (biggest XCH coin) is
                        # used by POSITION extreme, and SIZE extreme (smallest)
                        # is used by POSITION inner. Flip to match the
                        # size-indexed counts.
                        _buy_tier_sizes = {
                            "inner":   _buy_extr_pos,  # size inner slot = pos extreme size (biggest)
                            "mid":     _buy_outer_pos,
                            "outer":   _buy_mid_pos,
                            "extreme": _buy_inner_pos, # size extreme slot = pos inner size (smallest)
                        }
                    else:
                        _buy_tier_sizes = {
                            "inner":   _buy_inner_pos,
                            "mid":     _buy_mid_pos,
                            "outer":   _buy_outer_pos,
                            "extreme": _buy_extr_pos,
                        }
                    # Sell side is never flipped — sell positions always map
                    # to their same-named size tier.
                    _sell_tier_sizes = {
                        "inner":   Decimal(str(get_sell_tier_size_xch("inner")   or getattr(cfg, "INNER_SIZE_XCH", Decimal("1.0")))),
                        "mid":     Decimal(str(get_sell_tier_size_xch("mid")     or getattr(cfg, "MID_SIZE_XCH", Decimal("0.5")))),
                        "outer":   Decimal(str(get_sell_tier_size_xch("outer")   or getattr(cfg, "OUTER_SIZE_XCH", Decimal("0.25")))),
                        "extreme": Decimal(str(get_sell_tier_size_xch("extreme") or getattr(cfg, "EXTREME_SIZE_XCH", Decimal("0.1")))),
                    }
                    # Kept for backward compat with code below that reads
                    # `tier_sizes` as a single dict (it'll be the max of both
                    # sides, used only for the worker's legacy --tier-sizes
                    # arg). The per-side values also flow via new CLI args.
                    tier_sizes = {
                        k: max(_buy_tier_sizes.get(k, Decimal("0")),
                               _sell_tier_sizes.get(k, Decimal("0")))
                        for k in ("inner", "mid", "outer", "extreme")
                    }

                    def _tier_count(prefix, tier):
                        live = int(getattr(cfg, f"{prefix}_{tier.upper()}_TIER_COUNT", 0) or 0)
                        spare = int(getattr(cfg, f"{prefix}_{tier.upper()}_TIER_SPARE_COUNT", 0) or 0)
                        return max(0, live + spare)

                    # ── Slot-position counts as configured by the user ──────
                    # These describe how many BUY/SELL offers sit at each
                    # ladder POSITION (inner=closest to mid, extreme=furthest).
                    buy_position_counts = {
                        "inner":   _tier_count("BUY", "inner"),
                        "mid":     _tier_count("BUY", "mid"),
                        "outer":   _tier_count("BUY", "outer"),
                        "extreme": _tier_count("BUY", "extreme"),
                    }
                    sell_position_counts = {
                        "inner":   _tier_count("SELL", "inner"),
                        "mid":     _tier_count("SELL", "mid"),
                        "outer":   _tier_count("SELL", "outer"),
                        "extreme": _tier_count("SELL", "extreme"),
                    }

                    # ── Translate slot positions → coin SIZE counts ─────────
                    # The coin prep allocates coins by SIZE, not by position.
                    # When BUY_LADDER_REVERSED is on, a buy slot at the
                    # "extreme" position uses an INNER-sized coin, etc. The
                    # flip helper applies that mapping (no-op when reversal
                    # is off, and always a no-op for the sell side). This
                    # makes the live ladder settings the SINGLE SOURCE OF
                    # TRUTH for both prep and offer creation.
                    from coin_manager import flip_position_tiers_to_coin_size_tiers as _flip_tiers
                    xch_tier_counts = _flip_tiers(buy_position_counts, side="buy")
                    cat_tier_counts = _flip_tiers(sell_position_counts, side="sell")

                    # Sniper needs BOTH sides: buy snipers lock XCH coins, sell snipers
                    # lock CAT coins. preferred_tier="sniper" strict on both sides, so a
                    # missing CAT sniper pool silently kills sell-side probes and leaves
                    # the ladder anchored to one-sided probe data only. Fees are XCH-only.
                    sniper_count = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
                    sniper_size = Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", "0") or "0"))
                    if getattr(cfg, "SNIPER_ENABLED", False) and sniper_count > 0 and sniper_size > 0:
                        xch_tier_counts["sniper"] = sniper_count
                        cat_tier_counts["sniper"] = sniper_count
                        tier_sizes["sniper"] = sniper_size

                    fee_status = get_fee_settings_snapshot()
                    fee_count = int(fee_status.get("fee_prep_count", 0) or 0)
                    fee_size = Decimal(str(fee_status.get("fee_coin_size_xch", "0") or "0"))
                    if fee_status.get("fee_pool_enabled") and fee_count > 0 and fee_size > 0:
                        xch_tier_counts["fees"] = fee_count
                        tier_sizes["fees"] = fee_size

                    # Drop zero entries so the worker log stays clean
                    xch_tier_counts = {k: v for k, v in xch_tier_counts.items() if v > 0}
                    cat_tier_counts = {k: v for k, v in cat_tier_counts.items() if v > 0}

                    xch_total_coins = sum(xch_tier_counts.values())
                    cat_total_coins = sum(cat_tier_counts.values())
                    total_coins = xch_total_coins + cat_total_coins

                    tier_sizes_str = ",".join(f"{tier}={size}" for tier, size in tier_sizes.items())
                    xch_counts_str = ",".join(f"{k}={v}" for k, v in xch_tier_counts.items())
                    cat_counts_str = ",".join(f"{k}={v}" for k, v in cat_tier_counts.items())
                    # F62 (2026-04-09): also build per-side size strings.
                    # Sniper/fees stay in the combined `tier_sizes` dict;
                    # only the four trading tiers differ between buy and sell.
                    _buy_sizes_for_cli = dict(_buy_tier_sizes)
                    _sell_sizes_for_cli = dict(_sell_tier_sizes)
                    # Add sniper/fees from the combined dict (same on both sides)
                    if "sniper" in tier_sizes:
                        _buy_sizes_for_cli["sniper"] = tier_sizes["sniper"]
                        _sell_sizes_for_cli["sniper"] = tier_sizes["sniper"]
                    if "fees" in tier_sizes:
                        _buy_sizes_for_cli["fees"] = tier_sizes["fees"]
                        # fees is XCH-only, don't add to sell
                    buy_sizes_str  = ",".join(f"{t}={s}" for t, s in _buy_sizes_for_cli.items())
                    sell_sizes_str = ",".join(f"{t}={s}" for t, s in _sell_sizes_for_cli.items())

                    cmd = [
                        "python", worker_path,
                        "--xch-target", str(xch_total_coins),
                        "--cat-target", str(cat_total_coins),
                        "--tier-sizes", tier_sizes_str,       # legacy shared (kept for back-compat)
                        "--buy-tier-sizes", buy_sizes_str,    # F62: XCH coin sizes (for buy offers)
                        "--sell-tier-sizes", sell_sizes_str,  # F62: CAT coin sizes (for sell offers, in XCH equiv)
                        "--tier-counts-xch", xch_counts_str,
                        "--tier-counts-cat", cat_counts_str,
                        "--prep-headroom-pct", str(getattr(cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"))),
                        "--run-id", run_id,
                    ]
                    log_event("info", "coin_prep_config",
                              f"GUI tier coin prep (per-side): "
                              f"XCH={xch_total_coins} {xch_counts_str} | "
                              f"CAT={cat_total_coins} {cat_counts_str} "
                              f"(+{getattr(cfg, 'COIN_PREP_HEADROOM_PCT', Decimal('10'))}% headroom)")
                else:
                    # Uniform coin prep — uses _prep_coin_multiplier from request context
                    coin_multiplier = _prep_coin_multiplier
                    total_coins = int((max_buy + max_sell) * coin_multiplier)
                    cmd = [
                        "python", worker_path,
                        "--xch-target", str(total_coins),
                        "--xch-size", trade_xch,
                        "--cat-target", str(total_coins),
                        "--prep-headroom-pct", str(getattr(cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"))),
                        "--run-id", run_id,
                    ]
                    log_event("info", "coin_prep_config",
                              f"GUI coin prep: {total_coins} coins "
                              f"({max_buy}+{max_sell} × {coin_multiplier}), "
                              f"XCH size {trade_xch} (+{getattr(cfg, 'COIN_PREP_HEADROOM_PCT', Decimal('10'))}% headroom)")

                log_path = os.path.join(worker_dir, "coin_prep_output.log")
                log_file = open(log_path, "w", encoding="utf-8")
                popen_kwargs = {
                    "stdout": log_file,
                    "stderr": _sp.STDOUT,
                    "stdin": _sp.DEVNULL,
                    "cwd": worker_dir,
                    "env": env,
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = getattr(_sp, "CREATE_NO_WINDOW", 0)
                proc = _sp.Popen(cmd, **popen_kwargs)

                # Store process object globally (for killing on re-trigger)
                # and PID for lifecycle management
                _coin_prep_proc = proc
                _coin_prep_state["pid"] = proc.pid
                _console_state["coin_prep_visible"] = False

                log_event("info", "coin_prep_started",
                          f"Coin prep worker started (PID: {proc.pid})")

                # Monitor until it finishes
                while proc.poll() is None:
                    time.sleep(2)

                exit_code = proc.returncode

                if exit_code == 0:
                    _coin_prep_state["complete"] = True
                    _coin_prep_state["error"] = None
                    _coin_prep_state["phase"] = "complete"
                    prep_succeeded = True
                    log_event("info", "coin_prep_complete", "Coin prep finished successfully")
                else:
                    _coin_prep_state["complete"] = False
                    _coin_prep_state["phase"] = "error"
                    error_msg = f"Worker exited with code {exit_code}"
                    # Try to read log file for error context (non-Windows)
                    log_path = os.path.join(worker_dir, "coin_prep_output.log")
                    if os.path.exists(log_path):
                        try:
                            with open(log_path, "r", encoding="utf-8") as f:
                                output = f.read()
                            if output:
                                error_msg += f"\nLast output: ...{output[-500:]}"
                        except Exception:
                            pass
                    log_event("error", "coin_prep_failed", error_msg[:1000])
                    _coin_prep_state["error"] = error_msg

            except Exception as e:
                _coin_prep_state["complete"] = False
                _coin_prep_state["phase"] = "error"
                _coin_prep_state["error"] = str(e)
                log_event("error", "coin_prep_exception", str(e))
            finally:
                # Ensure the subprocess is terminated if it's still running.
                # Without this, an exception in the monitor loop (e.g. log_event
                # or state update throws) can orphan the child process.
                try:
                    if 'proc' in locals() and proc and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                except Exception:
                    pass
                try:
                    if 'log_file' in locals() and log_file:
                        log_file.close()
                except Exception:
                    pass
                _coin_prep_state["running"] = False
                _coin_prep_proc = None  # Clear global ref — worker is done
                # CRITICAL: Ungate the bot loop so it can resume offer creation
                if bot and hasattr(bot, 'coin_manager'):
                    bot.coin_manager._prep_running = False
                    if prep_succeeded:
                        log_event("info", "coin_prep_ungate",
                                  "Coin prep complete — press Start Bot to begin trading")
                    else:
                        log_event("warning", "coin_prep_ungate_error",
                                  "Coin prep ended with an error — review details before retrying")

        threading.Thread(target=do_prep, daemon=True).start()
        return jsonify({"success": True, "message": "Coin prep started"})
    except Exception as e:
        _coin_prep_state["running"] = False
        # Also ungate on early failure
        if bot and hasattr(bot, 'coin_manager'):
            bot.coin_manager._prep_running = False
        try:
            log_event("error", "coin_prep_trigger_failed", str(e))
        except Exception:
            pass
        return _api_error(e, request.path)


@app.route("/api/coin-prep/reset", methods=["POST"])
def api_coin_prep_reset():
    """Reset coin prep state."""
    _coin_prep_state["running"] = False
    _coin_prep_state["complete"] = False
    _coin_prep_state["started_at"] = None
    # Ungate bot loop if it was gated
    if bot and hasattr(bot, 'coin_manager'):
        bot.coin_manager._prep_running = False
    _coin_prep_state["error"] = None
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Console & System Routes
# ---------------------------------------------------------------------------

_console_state = {"main_visible": False, "coin_prep_visible": False}


@app.route("/api/console/status")
def api_console_status():
    """Legacy — the external console popup was removed 2026-04-06.

    The GUI now uses the in-app Logs view (sidebar → Logs) instead of
    toggling a separate console window. Kept as a stable-shaped no-op
    so any stale clients don't error out.
    """
    return jsonify({
        "main_visible": False,
        "coin_prep_visible": False,
        "coin_prep_running": _coin_prep_state.get("running", False),
        "platform": sys.platform,
        "deprecated": True,
    })


@app.route("/api/console/toggle", methods=["POST"])
def api_console_toggle():
    """Legacy — external console popup was eliminated to remove the
    'closing the console kills the bot' footgun. Clients should use
    the in-app Logs view instead."""
    return jsonify({
        "success": False,
        "deprecated": True,
        "error": "The external console has been removed — use the in-app Logs view (sidebar → Logs)",
    })



# ---------------------------------------------------------------------------
# Wallet Detection & Switching
# ---------------------------------------------------------------------------

@app.route("/api/wallets/detect")
def api_wallets_detect():
    """Probe both Chia and Sage wallets using their own RPC modules.

    Uses the actual wallet modules (wallet_chia.py and wallet_sage.py)
    which already have all the connection logic, certs, and retry handling.
    """
    detected = []

    # --- Probe Chia wallet using wallet_chia module ---
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
    # Re-enable once Sage publishes docs on cert setup for third-party clients.

    current = get_wallet_type()
    return jsonify({
        "success": True,
        "current": current,
        "detected": detected,
    })


@app.route("/api/wallets/switch", methods=["POST"])
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
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Data Export Routes
# ---------------------------------------------------------------------------

@app.route("/api/fills/export")
def api_fills_export():
    """Export fill history as CSV."""
    try:
        asset_id = _active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        if not asset_id:
            return jsonify({"success": False, "error": "No active CAT selected"}), 400

        history = _build_fill_history_for_gui(asset_id, limit=1000)
        if not history:
            return jsonify({"success": False, "error": "No fills to export"}), 404

        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "filled_at",
            "side",
            "price_xch",
            "size_xch",
            "size_cat",
            "tier",
            "trade_id",
            "coin_id",
        ])
        for f in history:
            writer.writerow([
                f.get("filled_at", ""),
                f.get("side", ""),
                str(f.get("price", "")),
                str(f.get("size_xch", "")),
                str(f.get("size_cat", "")),
                str(f.get("tier", "")),
                f.get("trade_id", ""),
                f.get("coin_id", ""),
            ])
        csv_data = output.getvalue()
        return Response(csv_data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=fills_export.csv"})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    """Clear the GUI log panel (hides older events, keeps them in DB for debug download)."""
    global _logs_cleared_at
    from datetime import datetime, timezone
    _logs_cleared_at = datetime.now(timezone.utc).isoformat()
    # Persist to database so it survives restarts
    try:
        from database import set_setting
        set_setting("logs_cleared_at", _logs_cleared_at)
    except Exception:
        pass
    return jsonify({"success": True, "message": "Log panel cleared"})


@app.route("/api/logs/download")
def api_logs_download():
    """Download a richer debug bundle with recent events and runtime state."""
    try:
        import glob
        import io
        import zipfile
        from database import get_recent_events
        from super_log import get_archive_summary, get_log_path, get_log_stats

        def _read_text_tail(path: str, max_bytes: int = 400_000) -> str:
            if not path or not os.path.exists(path):
                return ""
            with open(path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                if size > max_bytes:
                    fh.seek(-max_bytes, os.SEEK_END)
                else:
                    fh.seek(0)
                return fh.read().decode("utf-8", errors="replace")

        def _json_safe(value):
            if isinstance(value, dict):
                return _serialize_dict(value)
            if isinstance(value, list):
                return _serialize_list(value)
            return value

        events_list = get_recent_events(limit=2000)
        lines = []
        for ev in events_list:
            ts = ev.get("timestamp", "")
            level = ev.get("severity", "")
            source = ev.get("event_type", "")
            msg = ev.get("message", "")
            lines.append(f"[{ts}] [{level}] [{source}] {msg}")

        event_counts = {}
        for ev in events_list:
            key = str(ev.get("event_type", "") or "unknown")
            event_counts[key] = event_counts.get(key, 0) + 1

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": get_app_version(),
            "wallet_type": get_wallet_type(),
            "bot_running": bool(bot.is_running()) if bot else False,
            "current_cat": _active_cat,
            "session_start_time": _session_start_time,
            "logs_cleared_at": _logs_cleared_at,
            "event_count": len(events_list),
        }

        snapshots = {
            "health": _get_health_snapshot(),
            "event_type_counts": event_counts,
            "superlog_stats": get_log_stats(),
            "superlog_archive": get_archive_summary(5),
        }

        if bot:
            runtime_snapshot = {
                "running": bool(bot.is_running()),
                "loop_count": int(getattr(bot, "_loop_count", 0) or 0),
                "uptime_secs": int(time.time() - getattr(bot, "_start_time", 0))
                if getattr(bot, "_start_time", 0) else 0,
                "recovery": dict(getattr(bot, "_recovery_state", {}) or {}),
                "probe_state": dict(getattr(bot, "_probe_state", {}) or {}),
            }
            try:
                runtime_snapshot["price_info"] = bot.get_price_info()
            except Exception as e:
                runtime_snapshot["price_info_error"] = str(e)
            snapshots["runtime"] = _serialize_dict(runtime_snapshot)

            try:
                stats = get_stats(cfg.CAT_ASSET_ID, since=_get_run_history_cutoff())
                snapshots["pnl"] = _serialize_dict({
                    **stats,
                    "pending_verification_count": _get_session_pending_verification_count(),
                    "sniper": bot.sniper.get_stats() if getattr(bot, "sniper", None) else {},
                })
            except Exception as e:
                snapshots["pnl"] = {"error": str(e)}

            try:
                snapshots["market_intel"] = _serialize_dict(bot.market_intel.get_market_summary() or {})
            except Exception as e:
                snapshots["market_intel"] = {"error": str(e)}

            try:
                snapshots["runtime_monitor"] = _serialize_dict(bot.runtime_monitor.get_state() or {})
            except Exception as e:
                snapshots["runtime_monitor"] = {"error": str(e)}

            splash_snapshot = {}
            try:
                splash_snapshot["broadcast"] = _serialize_dict(bot.splash_manager.get_stats() or {})
            except Exception as e:
                splash_snapshot["broadcast"] = {"error": str(e)}
            try:
                splash_snapshot["node"] = _serialize_dict(bot.splash_node.get_status() or {})
            except Exception as e:
                splash_snapshot["node"] = {"error": str(e)}
            try:
                splash_snapshot["receive"] = _serialize_dict(bot.get_splash_receive_stats() or {})
            except Exception as e:
                splash_snapshot["receive"] = {"error": str(e)}
            snapshots["splash"] = splash_snapshot

        log_texts = {}
        superlog_path = get_log_path()
        if superlog_path:
            log_texts["logs/current_superlog_tail.log"] = _read_text_tail(superlog_path)

        tauri_stdout = os.path.join(_APP_ROOT, "tauri_backend_stdout.log")
        if os.path.exists(tauri_stdout):
            log_texts["logs/tauri_backend_stdout_tail.log"] = _read_text_tail(tauri_stdout)

        # Look for superlog files in the user data dir first (the
        # canonical location), then fall back to the install dir for
        # pre-migration dev installs.
        try:
            from user_paths import log_dir as _user_log_dir
            _log_dirs = [_user_log_dir(), _APP_ROOT]
        except Exception:
            _log_dirs = [_APP_ROOT]
        run_logs = []
        for _ld in _log_dirs:
            run_logs.extend(glob.glob(os.path.join(_ld, "bot_superlog_*.log")))
        if run_logs:
            latest_run_log = max(run_logs, key=os.path.getmtime)
            log_texts["logs/latest_run_superlog_tail.log"] = _read_text_tail(latest_run_log)
            manifest["latest_run_log"] = os.path.basename(latest_run_log)

        bundle_name = "bot_debug_bundle_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".zip"
        readme = "\n".join([
            "CATalyst debug bundle",
            "",
            "Included:",
            "- manifest.json: bundle metadata",
            "- recent_events.json / recent_events.txt: latest database events",
            "- snapshots/*.json: health, runtime, market, pnl, splash, and monitor state",
            "- logs/*.log: tails of the current superlog and nearby runtime logs",
            "",
            "This bundle is designed for troubleshooting a run without requiring direct DB access.",
        ])

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", readme)
            zf.writestr("manifest.json", json.dumps(_json_safe(manifest), indent=2))
            zf.writestr("recent_events.json", json.dumps(_json_safe(events_list), indent=2))
            zf.writestr("recent_events.txt", "\n".join(lines))
            for name, payload in snapshots.items():
                zf.writestr(f"snapshots/{name}.json", json.dumps(_json_safe(payload), indent=2))
            for path, text in log_texts.items():
                if text:
                    zf.writestr(path, text)

        buffer.seek(0)
        return Response(
            buffer.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename={bundle_name}"},
        )
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/superlog/stats")
def api_superlog_stats():
    """Get superlog statistics — file size, level, error dump count."""
    try:
        from super_log import get_log_stats
        return jsonify(get_log_stats())
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/superlog/level", methods=["POST"])
def api_superlog_level():
    """Change superlog file/terminal level at runtime.

    POST {"file_level": "trace"} to enable verbose logging for debugging.
    POST {"file_level": "info"} to go back to quiet mode.
    """
    try:
        data = request.get_json(force=True) or {}
        from super_log import set_file_level, set_terminal_level, get_log_stats
        if "file_level" in data:
            set_file_level(data["file_level"])
        if "terminal_level" in data:
            set_terminal_level(data["terminal_level"])
        return jsonify({"ok": True, **get_log_stats()})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/superlog/archive")
def api_superlog_archive():
    """Get archived digests from past log sessions.

    Shows error history, fill counts, and cycle stats from rotated logs.
    Useful for seeing what happened days/weeks ago without keeping full logs.
    Query param: ?last=20 (default 10)
    """
    try:
        from super_log import get_archive_summary
        last_n = request.args.get("last", 10, type=int)
        return jsonify(get_archive_summary(last_n=min(last_n, 100)))
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/superlog/download")
def api_superlog_download():
    """Download the current superlog file directly."""
    try:
        from super_log import get_log_path
        log_path = get_log_path()
        if log_path and os.path.exists(log_path):
            return send_file(log_path, mimetype="text/plain",
                             as_attachment=True,
                             download_name=os.path.basename(log_path))
        return jsonify({"error": "No superlog file found"}), 404
    except Exception as e:
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

@app.route("/api/health")
def api_health():
    """Health check endpoint — does a LIVE wallet check even when bot is stopped."""
    # Don't touch Sage RPC before the user has accepted the disclaimer.
    import chia_node
    if not chia_node.is_startup_authorised():
        return jsonify({
            "status": "ok",
            "version": get_app_version(),
            "wallet_type": get_wallet_type(),
            "bot_running": False,
            "sse_clients": events.subscriber_count,
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

    return jsonify({
        "status": "ok",
        "version": get_app_version(),
        "wallet_type": get_wallet_type(),
        "bot_running": bot.is_running() if bot else False,
        "sse_clients": events.subscriber_count,
        "timestamp": int(time.time()),
        "chia_health": health_data,
    })


# ---------------------------------------------------------------------------
# Doctor / Preflight
# ---------------------------------------------------------------------------

@app.route("/api/doctor")
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


@app.route("/api/config/history")
def api_config_history():
    """F26 (2026-04-08): expose the config change audit trail.

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
        return _api_error(e, request.path)


@app.route("/api/self-test")
def api_self_test():
    """F18 (2026-04-08): expose the startup self-test results to the GUI.

    Returns the self-test results captured at the last bot startup, OR
    runs a fresh self-test if force=1 is passed. The GUI can show the
    user what services are down and what features will be missing.
    """
    try:
        global bot
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
        return _api_error(e, request.path)


@app.route("/api/config/validate")
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
        return _api_error(e, request.path)


@app.route("/api/config/export-env")
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
        from flask import Response
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=chia_bot_settings.env"},
        )
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/reservations")
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
        return _api_error(e, request.path)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_offers(offers: list) -> list:
    """Convert offer list to JSON-safe format."""
    result = []
    for o in offers:
        item = {}
        for k, v in o.items():
            if isinstance(v, Decimal):
                item[k] = str(v)
            else:
                item[k] = v
        result.append(item)
    return result


def _serialize_list(items: list) -> list:
    """Convert a list of dicts to JSON-safe format."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(_serialize_dict(item))
        else:
            result.append(item)
    return result


def _serialize_dict(d: dict) -> dict:
    """Convert a dict to JSON-safe format (Decimal → str)."""
    if d is None:
        return {}
    result = {}
    for k, v in d.items():
        if isinstance(v, Decimal):
            result[k] = str(v)
        elif isinstance(v, dict):
            result[k] = _serialize_dict(v)
        elif isinstance(v, list):
            result[k] = _serialize_list(v)
        else:
            result[k] = v
    return result



@app.route("/api/wallet/sage-running", methods=["GET"])
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
        return _api_error(e, request.path)


@app.route("/api/wallet/begin-startup", methods=["POST"])
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
        return _api_error(e, request.path)


@app.route("/api/sage/startup-status")
def api_chia_startup_status():
    """Get current Chia startup phase for the main GUI to display."""
    try:
        import chia_node
        return jsonify(chia_node.get_startup_status())
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/sage/fingerprints")
def api_chia_fingerprints():
    """List available wallet fingerprints for the startup selection screen."""
    try:
        import chia_node
        fps = chia_node.get_available_fingerprints()
        return jsonify({"success": True, "fingerprints": fps})
    except Exception as e:
        return _api_error(e, request.path)


@app.route("/api/sage/start-with-fingerprint", methods=["POST"])
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
        return _api_error(e, request.path)


@app.route("/api/sage/setup-certs", methods=["POST"])
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
            # Auto-detect
            detected = chia_node._detect_sage_cert_path()
            if detected:
                cert_path = detected
                # Key is always a sibling file
                key_path = detected.replace("wallet.crt", "wallet.key")
            else:
                return jsonify({
                    "success": False,
                    "error": "Could not auto-detect Sage certificates. "
                             "Please provide the path manually.",
                }), 404

        # ── Safety: only accept paths inside a known Sage data directory ──
        # This prevents a local attacker (or compromised .env) from pointing
        # the bot at an arbitrary TLS cert elsewhere on the filesystem.
        def _is_inside_allowed_sage_dir(path: str) -> bool:
            try:
                real = os.path.realpath(path)
            except Exception:
                return False
            allowed_roots = []
            # Windows: %APPDATA%\com.rigidnetwork.sage\ssl\
            if sys.platform == "win32":
                appdata = os.environ.get("APPDATA")
                if appdata:
                    allowed_roots.append(os.path.realpath(
                        os.path.join(appdata, "com.rigidnetwork.sage")
                    ))
            # macOS: ~/Library/Application Support/com.rigidnetwork.sage/
            elif sys.platform == "darwin":
                allowed_roots.append(os.path.realpath(
                    os.path.expanduser("~/Library/Application Support/com.rigidnetwork.sage")
                ))
            # Linux: ~/.local/share/com.rigidnetwork.sage/
            else:
                allowed_roots.append(os.path.realpath(
                    os.path.expanduser("~/.local/share/com.rigidnetwork.sage")
                ))
            # Also allow paths inside the bot's own directory (for bundled certs)
            allowed_roots.append(os.path.realpath(os.path.dirname(os.path.abspath(__file__))))
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

        # Write to .env and update live environment
        os.environ["SAGE_CERT_PATH"] = cert_path
        os.environ["SAGE_KEY_PATH"] = key_path
        try:
            try:
                from user_paths import env_file as _env_file
                env_path = _env_file()
            except Exception:
                env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            lines = []
            if os.path.isfile(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            # Update or append each key
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
        return _api_error(e, request.path)


# ============================================================
# Spacescan API Setup
# ============================================================

@app.route("/api/spacescan/status")
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
    result["advice"] = _get_spacescan_plan_advice()

    # Try to get live stats from spacescan module
    try:
        from spacescan import get_api_stats
        result["stats"] = get_api_stats()
    except ImportError:
        result["stats"] = None

    return jsonify(result)


@app.route("/api/spacescan/setup", methods=["POST"])
def api_spacescan_setup():
    """Save or clear the Spacescan API key.

    POST {"api_key": "xxx"}  → saves key, enables Pro tier
    POST {"api_key": ""}     → clears key, falls back to Free tier
    POST {"skip": true}      → marks setup as seen, stays on Free tier
    """
    data = request.get_json(silent=True)

    if not isinstance(data, dict):

        return jsonify({"success": False, "error": "Invalid request body"}), 400

    # "Skip" — user chose Free tier knowingly
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
            if test_resp.status_code != 200:
                return jsonify({"success": False, "error": f"Spacescan returned HTTP {test_resp.status_code}"}), 400
        except Exception as e:
            return jsonify({"success": False, "error": f"Could not reach Spacescan: {e}"}), 502

        # Key is valid — persist in user-local secrets (NOT .env) and apply in-memory.
        # user_secrets stores the key in %APPDATA%\ChiaMarketMaker\user_secrets.json
        # so it survives restarts on this machine but cannot travel to another PC.
        import user_secrets as _user_secrets
        _user_secrets.set_secret("SPACESCAN_API_KEY", api_key)
        cfg.SPACESCAN_API_KEY = api_key  # apply in-memory without writing to .env
        cfg.update("SPACESCAN_ENABLED", "true")
        log_event("info", "spacescan_setup", "Pro API key configured and validated")
        return jsonify({"success": True, "tier": "pro", "message": "Pro API key saved and verified"})
    else:
        # Clear key — remove from user secrets and fall back to free
        import user_secrets as _user_secrets
        _user_secrets.set_secret("SPACESCAN_API_KEY", "")
        cfg.SPACESCAN_API_KEY = ""  # clear in-memory
        cfg.update("SPACESCAN_ENABLED", "true")
        log_event("info", "spacescan_setup", "API key cleared — using Free tier")
        return jsonify({"success": True, "tier": "free", "message": "Switched to Free tier"})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _graceful_shutdown(signum, _frame):
    """Handle Ctrl+C or terminal close — stop bot cleanly before exit."""
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    print(f"\n🛑 Received {sig_name} — shutting down gracefully...", flush=True)

    if bot and bot.is_running():
        print("   Stopping bot loop...", flush=True)
        bot.stop()
        print("   ✅ Bot loop stopped", flush=True)

    try:
        backup_database()
        print("   ✅ Database backed up", flush=True)
    except Exception:
        pass

    # Stop Splash node (in case bot.stop() didn't cover it)
    try:
        if bot and hasattr(bot, 'splash_node') and bot.splash_node.is_running():
            bot.splash_node.stop()
            print("   ✅ Splash node stopped", flush=True)
    except Exception:
        pass

    try:
        if bot and hasattr(bot, "runtime_monitor"):
            bot.runtime_monitor.stop()
    except Exception:
        pass

    # Stop Chia services
    print("   Stopping Chia services...", flush=True)
    try:
        import chia_node
        result = chia_node.stop_chia("all")
        if result.get("success"):
            print("   ✅ Chia services stopped", flush=True)
        else:
            print(f"   ⚠️ Chia stop: {result.get('error', 'unknown')}", flush=True)
    except Exception as e:
        print(f"   ⚠️ Could not stop Chia: {e}", flush=True)

    print("   Goodbye!", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    print("=" * 60)
    print("  Chia CAT Market Maker V2 — 'The Smart One'")
    print("=" * 60)

    # --- Check for stale instance already running on port 5000 ---
    import socket as _socket
    _port = 5000
    _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _sock.settimeout(1)
        _sock.connect(("127.0.0.1", _port))
        _sock.close()
        # Port is in use — another instance is running
        print(f"\n  ⚠️  Port {_port} is already in use!")
        print(f"  Another bot instance appears to be running.")
        print(f"  Please close the other instance first (Ctrl+C in its terminal),")
        print(f"  or kill it via Task Manager (look for 'python api_server.py').")
        print(f"\n  Exiting to avoid running multiple instances.\n")
        sys.exit(1)
    except (ConnectionRefusedError, OSError, _socket.timeout):
        pass  # Port is free — good to go
    finally:
        try:
            _sock.close()
        except Exception:
            pass

    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGINT, _graceful_shutdown)   # Ctrl+C
    signal.signal(signal.SIGTERM, _graceful_shutdown)   # kill / task manager
    # SIGBREAK is Windows-only (terminal close / Ctrl+Break)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _graceful_shutdown)

    # Initialise
    init_database()

    # Record session start time — console only shows events from THIS session
    _session_start_time = datetime.now(timezone.utc).isoformat()

    # Restore "logs cleared at" from database so Clear survives restarts
    try:
        from database import get_setting
        saved = get_setting("logs_cleared_at")
        if saved:
            _logs_cleared_at = saved
            print(f"  [Logs] Restored clear-point: {saved}")
    except Exception:
        pass

    # Restore the latest fresh-run cutoff so PnL/history stay scoped to the
    # current run even after an app restart.
    try:
        restored_cutoff = _restore_run_history_cutoff_from_events()
        if restored_cutoff:
            print(f"  [Fresh Run] Restored history cutoff: {restored_cutoff}")
    except Exception:
        pass

    # Fresh app startups should not inherit old Splash receive counters.
    try:
        from database import clear_splash_incoming
        clear_splash_incoming()
    except Exception:
        pass

    create_bot()

    # Load user-local secrets (e.g. Spacescan API key) into cfg in-memory.
    # These are stored in %APPDATA%\ChiaMarketMaker\ and are never written to .env.
    try:
        import user_secrets as _user_secrets
        _user_secrets.apply_to_config(cfg)
        if cfg.SPACESCAN_API_KEY:
            print("  [Secrets] Spacescan API key loaded from user secrets.", flush=True)
    except Exception as _e:
        print(f"  [Secrets] Could not load user secrets: {_e}", flush=True)

    # Wallet preload is NOT auto-started here.
    # It is triggered explicitly by the GUI after the user accepts the risk
    # disclosure, via POST /api/wallet/begin-startup.  This ensures no wallet
    # RPC calls are made before the user has acknowledged the disclaimer.

    log_event("info", "server_started", "API server starting on port 5000")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True
    )

