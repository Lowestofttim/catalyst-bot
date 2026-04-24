"""Flask HTTP + SSE server backing the dashboard and the in-process bridge

Thin translation layer between HTTP and the trading modules. Routes delegate
straight into `bot_loop`, `offer_manager`, `coin_manager`, `wallet`, and the
other domain modules; this file owns request validation, response shaping, and
the real-time event stream. Consumed both by `bot_gui.html` over `fetch` and
by `app_bridge.py` via `test_request_context()`.

Key responsibilities:
    - Expose REST routes for bot control, config, offers, fills, coins,
      wallet/Sage lifecycle, Splash, Dexie, Spacescan, and diagnostics
    - Stream live updates to the GUI over Server-Sent Events at `/api/events`
    - Gate state-changing routes behind a loopback-origin check plus a
      per-run `X-Bot-Local-Token`
    - Install superlog hooks at startup and bind strictly to `127.0.0.1:5000`

The server is never exposed beyond loopback. Any change that relaxes the
origin or token checks must preserve that invariant.
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

# When run as the entry point (`python api_server.py`), Python loads this file
# as the `__main__` module — `sys.modules` has no `api_server` key. Any
# blueprint that does `import api_server` later in this file would then trigger
# a second load of this file under the `api_server` name, re-running every
# side effect and crashing mid-blueprint-import with a circular-import error.
# Aliasing `sys.modules['api_server']` to the running `__main__` module makes
# subsequent `import api_server` calls return the already-initialized object.
if __name__ == "__main__":
    sys.modules.setdefault("api_server", sys.modules[__name__])
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, Optional
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
# resolves to the _internal/ subdirectory, NOT the bundle root where
# data files (HTML, images) are placed. sys._MEIPASS always points to
# the bundle root, so we use it when available.
#
# In dev mode this file lives at src/catalyst/api_server.py, so the repo
# root (where bot_gui.html sits) is two dirname() hops up from here.
# ---------------------------------------------------------------------------
_APP_ROOT = getattr(sys, '_MEIPASS', None)
if _APP_ROOT is None:
    # Dev mode: this module lives at src/catalyst/api_server.py, and
    # bot_gui.html sits at the repo root (three dirname() hops up).
    _APP_ROOT = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    ))
    # Legacy fallback: if bot_gui.html isn't at that computed root
    # (e.g. a flat install for ad-hoc testing), look alongside this
    # module instead so we don't regress the pre-src-layout behaviour.
    if not os.path.isfile(os.path.join(_APP_ROOT, "bot_gui.html")):
        _APP_ROOT = os.path.dirname(os.path.abspath(__file__))
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
    """Return the packaged app version from _version.py (single source of truth)."""
    try:
        from _version import __version__
        return __version__
    except Exception:
        return "unknown"


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
        "cache_age_secs": None,
        "stale": False,
    }
    if not asset_id:
        return context

    try:
        from database import get_market_analysis_cache, get_market_analysis_cache_age_secs
        spacescan = get_market_analysis_cache(asset_id, "spacescan") or {}
        analysis = get_market_analysis_cache(asset_id, "full_analysis") or {}
        # Advisor tips that depend on Spacescan should degrade gracefully when
        # the cache is old. 12h is well inside the 24h TTL but clearly past
        # "fresh" — beyond this threshold we tag the context stale so the
        # front-end can suppress or annotate the dependent advisories.
        _age = get_market_analysis_cache_age_secs(asset_id, "spacescan")
        if _age is not None:
            context["cache_age_secs"] = int(_age)
            context["stale"] = bool(_age > 12 * 3600)
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
# Suppress the "This is a development server" startup warning.
# Flask's built-in server is intentional here (single-user desktop app),
# so the warning adds no value and clutters the console.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


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


# _build_fill_history_for_gui moved to blueprint



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
              action: str = None, action_label: str = None,
              action_value: str = None):
        """Convenience: set a persistent alert and emit it.

        ``action_value`` is an opaque string passed to the action handler
        in the frontend (e.g. a comma-separated list of trade_ids). The
        default is ``None``; set it when the action needs a payload.
        """
        if hasattr(self, '_alert_store'):
            self._alert_store.set_alert(alert_id, severity, title, message,
                                        action, action_label, action_value)

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
                  action: str = None, action_label: str = None,
                  action_value: str = None):
        """Create or update an alert. Severity: 'error', 'warning', 'info', 'success'.

        ``action_value`` is an opaque payload passed to the action handler
        (e.g. a comma-separated list of trade_ids). Optional.
        """
        with self._lock:
            self._alerts[alert_id] = {
                "id": alert_id,
                "severity": severity,
                "title": title,
                "message": message,
                "action": action,  # optional action ID handled client-side
                "action_label": action_label,  # button text
                "action_value": action_value,  # optional payload for the action
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


def _get_live_mid_price_str() -> Optional[str]:
    """Return the bot's current weighted mid as a decimal string, or None.

    Used to seed the coin-prep subprocess with the same price the bot trades
    against, so CAT-coin sizes align with live ladder sizes. Tries the cached
    last_price first, then a fresh fetch via get_price() if the cache is empty
    or stale (common when prep is triggered before the bot loop has started
    and no cycle has populated the cache yet).

    Returns None only when both paths fail; the worker then falls back to
    Dexie's last_price ticker, which may lag on thin markets.
    """
    try:
        pe = getattr(bot, "price_engine", None) if "bot" in globals() else None
        if pe is None:
            return None
        p = pe.get_last_price()
        if p is None or Decimal(str(p)) <= 0:
            # Cache miss — force a fresh fetch of the weighted mid so prep
            # and the bot agree on price even on first run.
            try:
                fresh = pe.get_price()
                if isinstance(fresh, dict):
                    p = fresh.get("mid_price") or fresh.get("mid") or fresh.get("price")
                else:
                    p = fresh
            except Exception:
                p = None
        if p is None:
            return None
        p_dec = Decimal(str(p))
        if p_dec <= 0:
            return None
        return format(p_dec, "f")
    except Exception:
        return None


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
    # F74: shape-fix recovery orchestrator. Attached after the event
    # bus is wired so flows can emit SSE progress events.
    try:
        from shape_fix_orchestrator import ShapeFixOrchestrator
        bot.shape_fix_orchestrator = ShapeFixOrchestrator(bot, events)
    except Exception as _sf_err:
        # Non-fatal — dashboard simply won't have the modal experience
        print(f"  [SHAPE-FIX] ⚠️  Could not init orchestrator: {_sf_err}", flush=True)
        bot.shape_fix_orchestrator = None
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
        "sage_rpc_advanced.png",
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
                             preserve_history: bool = False,
                             reason: str = "fresh_start") -> Dict:
    """Reset session-facing bot state.

    Two modes controlled by ``preserve_history``:

    * ``preserve_history=False`` (default / legacy / "Start Fresh"):
        Clears fills, round-trips, position baseline, and runtime stats
        in addition to anything the caller opted into via the other flags.
        Equivalent to "wipe everything and start over" — used when the
        operator explicitly picks Start Fresh or when switching CATs.

    * ``preserve_history=True`` (coin-prep re-run):
        Keeps the fills / round-trips tables and the position baseline.
        Coin prep can still opt into ``clear_coins`` and
        ``cancel_open_offers`` because those records refer to coin IDs
        that are about to be destroyed by the re-split — but the user's
        trading history survives the re-prep. This is the 2026-04-19
        default for the Prepare Coins flow; users who actually want a
        full wipe can pick the explicit Start Fresh button.
    """
    global _run_history_cutoff, _session_start_time

    from database import _sqlite_ts
    reset_at = _sqlite_ts(datetime.now(timezone.utc))
    summary = {
        "reset_at": reset_at,
        "preserve_history": bool(preserve_history),
        "fills_cleared": 0,
        "round_trips_cleared": 0,
        "coins_cleared": 0,
        "open_offers_cancelled": 0,
        "price_history_cleared": False,
        "inventory_cleared": False,
    }

    conn = get_connection()
    try:
        has_round_trips = bool(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='round_trips'"
        ).fetchone())

        if not preserve_history:
            # Only count rows we're actually going to delete.
            summary["fills_cleared"] = int(
                (conn.execute("SELECT COUNT(*) as cnt FROM fills").fetchone()["cnt"]) or 0
            )
            if has_round_trips:
                summary["round_trips_cleared"] = int(
                    (conn.execute("SELECT COUNT(*) as cnt FROM round_trips").fetchone()["cnt"]) or 0
                )

        if clear_coins:
            summary["coins_cleared"] = int(
                (conn.execute("SELECT COUNT(*) as cnt FROM coins").fetchone()["cnt"]) or 0
            )

        if not preserve_history:
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

    if not preserve_history:
        # Advance the run-history cutoff so dashboard queries (/api/logs,
        # offer history, etc.) stop surfacing pre-reset entries. Under
        # preserve-history mode we keep the existing cutoff so the user's
        # own history stays visible after a coin-prep re-run.
        _run_history_cutoff = reset_at
        cfg.RUN_HISTORY_CUTOFF = reset_at
        _session_start_time = reset_at

    if not preserve_history and bot and getattr(bot, "risk_manager", None):
        # Only zero the position baseline on a full reset. A coin-prep
        # re-run doesn't change the on-chain position, so the accumulated
        # net_position_cat must remain intact (otherwise the next cycle
        # believes it's starting from zero and will happily rebuild
        # exposure past MAX_POSITION_XCH).
        try:
            bot.risk_manager.reset_position()
        except Exception:
            pass

    if not preserve_history:
        stats_reset = _reset_runtime_session_stats()
        summary.update(stats_reset)
    else:
        # Always drain Splash incoming (those offers reference the old
        # coin IDs) but DON'T reset market_intel / splash session stats
        # under preserve_history.
        try:
            from database import clear_splash_incoming
            summary["splash_incoming_cleared"] = int(clear_splash_incoming() or 0)
        except Exception:
            summary["splash_incoming_cleared"] = 0

    if reason:
        if preserve_history:
            details = (
                f"Coin-prep re-run at {reset_at}: preserved fills / round-trips / "
                f"position baseline, cleared {summary['coins_cleared']} coin rows"
            )
            if cancel_open_offers:
                details += f", cancelled {summary['open_offers_cancelled']} open offers"
        else:
            details = (
                f"Fresh run reset at {reset_at}: cleared {summary['fills_cleared']} fills, "
                f"{summary['round_trips_cleared']} round-trips, "
                f"{summary.get('splash_incoming_cleared', 0)} Splash incoming offers"
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
                "User-Agent": f"CATalyst/{current}",
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
                "User-Agent": "CATalyst/sage-version-check",
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

# api_events moved to blueprint



# ---------------------------------------------------------------------------
# Bot Control Routes
# ---------------------------------------------------------------------------

# api_bot_start moved to blueprint



# api_bot_stop moved to blueprint



# api_shutdown moved to blueprint



# api_bot_state moved to blueprint



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


# api_status moved to blueprint



def _build_liquidity_status_block(raw_status: dict) -> dict:
    """Build the ``liquidity`` payload for /api/status.

    Returns::

        {
          "mode": "two_sided" | "buy_only" | "sell_only",
          "active_side": "both" | "buy" | "sell",
          "parked": bool,
          "parked_reason": str | None,      # short code for the banner
          "parked_message": str | None,     # user-visible detail
        }

    Parked = the active side can't fund another offer. In buy_only that's
    "XCH balance below the smallest buy tier size"; in sell_only that's
    "CAT balance below the smallest sell tier size". Two-sided never
    parks (the bot's existing inventory logic handles exhaustion
    differently).
    """
    block = {
        "mode": (getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided").lower(),
        "active_side": "both",
        "parked": False,
        "parked_reason": None,
        "parked_message": None,
    }
    if block["mode"] not in ("two_sided", "buy_only", "sell_only"):
        block["mode"] = "two_sided"
    try:
        block["active_side"] = cfg.active_side()
    except Exception:
        pass
    if block["mode"] == "two_sided":
        return block

    # Compute parked-state for the single-sided modes. We use "smallest
    # prep tier size" as the floor — if the wallet can't cover even one
    # offer at the smallest tier (with a 10% headroom margin) there's
    # nothing useful to do and the bot is effectively parked.
    try:
        _bal = raw_status.get("balances") or {}
        if block["mode"] == "buy_only":
            xch_avail = float(_bal.get("xch", {}).get("spendable") or 0)
            # Smallest buy-side offer size — prefer per-side fields,
            # fall back to shared legacy. Under reverse-buy the smallest
            # position size is still the inner POSITION (not bucket).
            try:
                from config import get_buy_tier_size_xch
                _sizes = [float(get_buy_tier_size_xch(t) or 0) for t in ("inner", "mid", "outer", "extreme")]
                _sizes = [s for s in _sizes if s > 0]
                floor = min(_sizes) if _sizes else float(getattr(cfg, "DEFAULT_TRADE_XCH", 0.01) or 0.01)
            except Exception:
                floor = float(getattr(cfg, "DEFAULT_TRADE_XCH", 0.01) or 0.01)
            reserve = float(getattr(cfg, "XCH_RESERVE", 0) or 0)
            usable = max(0.0, xch_avail - reserve)
            if floor > 0 and usable < floor * 1.02:
                block["parked"] = True
                block["parked_reason"] = "xch_exhausted"
                block["parked_message"] = (
                    f"Accumulation parked: {usable:.4f} XCH available "
                    f"(below smallest buy tier {floor:.4f} XCH). "
                    f"Add XCH to resume, or switch to Two-Sided to recycle "
                    f"the CAT you've accumulated."
                )
        elif block["mode"] == "sell_only":
            cat_avail = float(_bal.get("cat", {}).get("spendable") or 0)
            try:
                from config import get_sell_tier_size_xch
                mid = None
                try:
                    pricing = raw_status.get("pricing") or {}
                    if pricing.get("mid"):
                        mid = float(pricing.get("mid") or 0)
                except Exception:
                    mid = None
                _xch_sizes = [float(get_sell_tier_size_xch(t) or 0) for t in ("inner", "mid", "outer", "extreme")]
                _xch_sizes = [s for s in _xch_sizes if s > 0]
                xch_floor = min(_xch_sizes) if _xch_sizes else 0.0
                cat_floor = (xch_floor / mid) if (mid and mid > 0 and xch_floor > 0) else 0.0
            except Exception:
                cat_floor = 0.0
            reserve = float(getattr(cfg, "CAT_RESERVE", 0) or 0)
            usable = max(0.0, cat_avail - reserve)
            if cat_floor > 0 and usable < cat_floor * 1.02:
                block["parked"] = True
                block["parked_reason"] = "cat_exhausted"
                cat_name = getattr(cfg, "CAT_NAME", None) or "tokens"
                block["parked_message"] = (
                    f"Distribution parked: {usable:,.0f} {cat_name} available "
                    f"(below smallest sell tier {cat_floor:,.0f}). "
                    f"Top up {cat_name} to resume, or switch to Two-Sided "
                    f"to buy more back."
                )
    except Exception:
        # Never let a parked-state computation break /api/status
        pass
    return block


def _safe_float(val) -> float:
    """Safely convert a value to float (handles Decimal, str, None)."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# api_runtime_diagnostics moved to blueprint



# api_diagnostics_api_stats moved to blueprint



def _sage_ts_to_iso(ts) -> str:
    """Convert a Sage creation_timestamp (unix epoch) to ISO format string."""
    if not ts:
        return ""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ""


# api_bot_price moved to blueprint



# ---------------------------------------------------------------------------
# Config Routes
# ---------------------------------------------------------------------------

# api_config_get moved to blueprint



# api_fees_status moved to blueprint



# _apply_sage_change_address_setting moved to blueprint



# api_config_update moved to blueprint



# api_config_reload moved to blueprint



# api_config_apply moved to blueprint



# api_config_live moved to blueprint



# ---------------------------------------------------------------------------
# Offer Routes
# ---------------------------------------------------------------------------

# api_offers moved to blueprint



# api_cancel_all_status moved to blueprint



# api_open_offer_count moved to blueprint



# api_cancel_all moved to blueprint



# api_cleanup_orphans moved to blueprint



# api_cancel_offer moved to blueprint



# Boost routes moved to blueprints/boost.py

# ---------------------------------------------------------------------------
# Fill & PnL Routes
# ---------------------------------------------------------------------------

# api_fills moved to blueprint



# api_fills_classified moved to blueprint



# api_fills_arb_wallets moved to blueprint



# api_market_fill_intel moved to blueprint



# api_offers_diagnostic moved to blueprint



# api_purge_fills moved to blueprint



# api_pnl_reset_preview moved to blueprint



# api_pnl_reset moved to blueprint



# api_reset_offer_history moved to blueprint



# api_reset_full moved to blueprint



# Sentinel context manager for the sniper lock fallback above.
class _SNIPE_LOCK_NOOP_CLS:
    def __enter__(self): return self
    def __exit__(self, *a): return False
_SNIPE_LOCK_NOOP = _SNIPE_LOCK_NOOP_CLS()


# api_deposit_advisory_allocate moved to blueprint



# Session routes moved to blueprints/session.py


# api_pnl moved to blueprint



# ---------------------------------------------------------------------------
# Dashboard Command Centre (aggregated endpoint for the top panel)
# ---------------------------------------------------------------------------

# api_dashboard moved to blueprint



# api_stats moved to blueprint



# ---------------------------------------------------------------------------
# Inventory & Risk Routes
# ---------------------------------------------------------------------------

# api_inventory moved to blueprint



# api_risk_spreads moved to blueprint



# ---------------------------------------------------------------------------
# Coin Routes
# ---------------------------------------------------------------------------

# api_coins moved to blueprint



# api_coin_topup moved to blueprint



# api_coin_prep moved to blueprint



# ---------------------------------------------------------------------------
# Dexie Routes
# ---------------------------------------------------------------------------

# api_dexie_stats moved to blueprint



# api_dexie_repost moved to blueprint



# ---------------------------------------------------------------------------
# Market Intelligence Routes (NEW — ecosystem upgrades)
# ---------------------------------------------------------------------------

# _fetch_dbx_pair_status moved to blueprint



# api_market_intel moved to blueprint



# api_market_orderbook moved to blueprint



# api_market_slippage moved to blueprint



# api_market_dbx moved to blueprint



# Alert/watchdog routes moved to blueprints/watchdog.py

# Splash P2P routes moved to blueprints/splash.py (registered at bottom of file)

# ---------------------------------------------------------------------------
# V3: Coinset API Routes
# ---------------------------------------------------------------------------

# api_coinset_stats moved to blueprint



# ---------------------------------------------------------------------------
# Price Routes
# ---------------------------------------------------------------------------

# api_price moved to blueprint



# api_market_summary moved to blueprint



# api_tibet_price moved to blueprint



# api_amm_price moved to blueprint



# api_debug_coinprep moved to blueprint



# api_debug_pricing moved to blueprint



# api_debug_tibet_test moved to blueprint



# api_debug_sage_single_offer_test moved to blueprint



# _fetch_price_standalone moved to blueprint



# ---------------------------------------------------------------------------
# Smart Defaults — Live Market Data Analysis
# ---------------------------------------------------------------------------

# _fetch_dexie_orderbook_standalone moved to blueprint



# api_smart_defaults moved to blueprint



# _calculate_smart_defaults moved to blueprint



# ---------------------------------------------------------------------------
# Database Routes
# ---------------------------------------------------------------------------

# api_db_backup moved to blueprint



# ---------------------------------------------------------------------------
# Log Route (for GUI log panel)
# ---------------------------------------------------------------------------

# api_logs moved to blueprint



# ---------------------------------------------------------------------------
# Wallet & CAT Discovery Routes (GUI startup needs these)
# ---------------------------------------------------------------------------

# api_fingerprint moved to blueprint



# _normalize_asset_id moved to blueprint



# _get_dexie_pairs moved to blueprint



# api_token_overview moved to blueprint



# api_dexie_v3_pairs moved to blueprint



# api_cats moved to blueprint



# api_cat_select moved to blueprint



# api_cat_refresh moved to blueprint



# api_balances_refresh moved to blueprint



# api_full_node_status moved to blueprint



# api_settings_defaults moved to blueprint



# api_settings_validate moved to blueprint



# ---------------------------------------------------------------------------
# Check Resume (GUI startup)
# ---------------------------------------------------------------------------

# _resume_last_active_label moved to blueprint



# api_check_resume moved to blueprint



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


# The cancel-all state-factory/mutator helpers live in blueprints/offers.py,
# but the SHARED dict + lock must be initialized here at module load time so
# other modules (shutdown path, GUI fetch) can read it before the blueprints
# are registered below.
_cancel_all_state = {
    "running": False, "complete": False, "error": None,
    "phase": "idle", "message": "",
    "started_at": None, "finished_at": None, "updated_at": None,
    "total": 0, "batch_size": 0, "total_batches": 0, "current_batch": 0,
    "batch_cancelled": 0, "batch_failed": 0,
    "cancelled": 0, "failed": 0,
}
_cancel_all_state_lock = threading.Lock()


# _set_cancel_all_state moved to blueprint



# _reset_cancel_all_state moved to blueprint



# _get_cancel_all_state moved to blueprint



# api_log_event moved to blueprint



# api_coin_prep_status moved to blueprint



# api_coin_prep_verify moved to blueprint



# api_coin_prep_trigger moved to blueprint



# api_coin_prep_reset moved to blueprint



# Console + wallet detect/switch routes moved to blueprints/system.py

# ---------------------------------------------------------------------------
# Data Export Routes
# ---------------------------------------------------------------------------

# api_fills_export moved to blueprint



# api_logs_clear moved to blueprint



# api_logs_download moved to blueprint



# SuperLog routes moved to blueprints/superlog.py



# Health, doctor, self-test, config-validate/history/export routes moved to
# blueprints/diagnostics.py (registered at bottom of file)


# Reservations route moved to blueprints/spacescan.py


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



# api_wallet_sage_running moved to blueprint



# api_wallet_retry_sage_connect moved to blueprint



# api_wallet_begin_startup moved to blueprint



# api_chia_startup_status moved to blueprint



# api_chia_fingerprints moved to blueprint



# api_chia_start_with_fingerprint moved to blueprint



# api_sage_setup_certs moved to blueprint



# Spacescan routes moved to blueprints/spacescan.py


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


# ---------------------------------------------------------------------------
# Blueprint registration
# ---------------------------------------------------------------------------
# Flask Blueprints let us split the route file without breaking callers that
# do `api_server.api_xxx(...)` directly (app_bridge.py and tests). Each
# blueprint imports this module and accesses shared state via attribute
# access (e.g. `api_server.bot`) so reassignments are picked up. We re-export
# the route function names below so `api_server.api_splash_stats` still
# resolves after the move.
from blueprints.splash import (
    bp as _splash_bp,
    api_splash_stats,
    api_splash_receive,
    api_splash_node,
    api_splash_node_start,
    api_splash_node_output,
    api_splash_setup_check,
    api_splash_setup_download,
    api_splash_setup_progress,
    api_splash_setup_release,
    api_splash_incoming,
    api_splash_incoming_list,
)
from blueprints.diagnostics import (
    bp as _diagnostics_bp,
    api_health,
    api_doctor,
    api_health_runtime,
    api_config_history,
    api_self_test,
    api_config_validate,
    api_config_export_env,
)
from blueprints.superlog import (
    bp as _superlog_bp,
    api_superlog_stats,
    api_superlog_level,
    api_superlog_archive,
    api_superlog_download,
)
from blueprints.watchdog import (
    bp as _watchdog_bp,
    api_alerts,
    api_dismiss_alert,
    api_watchdog_cancel_mismatched_offers,
    api_watchdog_shape_fix_status,
    api_watchdog_shape_fix_abort,
)
from blueprints.boost import (
    bp as _boost_bp,
    api_boost_activate,
    api_boost_deactivate,
    api_boost_state,
)
from blueprints.session import (
    bp as _session_bp,
    api_session_fresh_start,
    api_session_resume_chosen,
)
from blueprints.system import (
    bp as _system_bp,
    api_console_status,
    api_console_toggle,
    api_wallets_detect,
    api_wallets_switch,
)
from blueprints.spacescan import (
    bp as _spacescan_bp,
    api_spacescan_status,
    api_spacescan_setup,
    api_reservations,
)

from blueprints.market import (
    bp as _market_bp,
    api_dexie_stats, api_dexie_repost, api_market_intel,
    api_market_orderbook, api_market_slippage, api_market_dbx,
    api_coinset_stats, api_price, api_market_summary,
    api_tibet_price, api_amm_price,
    api_debug_coinprep, api_debug_pricing, api_debug_tibet_test,
    api_debug_sage_single_offer_test,
)
from blueprints.sage import (
    bp as _sage_bp,
    api_fingerprint, api_full_node_status,
    api_wallet_sage_running, api_wallet_retry_sage_connect,
    api_wallet_begin_startup, api_chia_startup_status,
    api_chia_fingerprints, api_chia_start_with_fingerprint,
    api_sage_setup_certs,
)
from blueprints.cat import (
    bp as _cat_bp,
    api_deposit_advisory_allocate, api_token_overview,
    api_dexie_v3_pairs, api_cats, api_cat_select,
    api_cat_refresh, api_balances_refresh,
)
from blueprints.config_bp import (
    bp as _config_bp,
    api_config_get, api_fees_status, api_config_update,
    api_config_reload, api_config_apply, api_config_live,
    api_settings_defaults, api_settings_validate, api_check_resume,
)
from blueprints.coin_prep import (
    bp as _coin_prep_bp,
    api_coins, api_coin_topup, api_coin_prep,
    api_db_backup, api_logs, api_log_event,
    api_coin_prep_status, api_coin_prep_verify, api_coin_prep_trigger,
    api_coin_prep_reset, api_fills_export,
    api_logs_clear, api_logs_download,
)

from blueprints.offers import (
    bp as _offers_bp,
    api_offers, api_cancel_all_status, api_open_offer_count,
    api_cancel_all, api_cleanup_orphans, api_cancel_offer,
    api_fills, api_fills_classified, api_fills_arb_wallets,
    api_market_fill_intel, api_offers_diagnostic, api_purge_fills,
    api_pnl_reset_preview, api_pnl_reset,
    api_reset_offer_history, api_reset_full, api_pnl,
)
from blueprints.dashboard import (
    bp as _dashboard_bp,
    api_dashboard, api_stats, api_inventory, api_risk_spreads,
)
from blueprints.smart_defaults import (
    bp as _smart_defaults_bp,
    api_smart_defaults,
)
from blueprints.bot import (
    bp as _bot_bp,
    api_events, api_bot_start, api_bot_stop, api_shutdown,
    api_bot_state, api_status,
    api_runtime_diagnostics, api_diagnostics_api_stats, api_bot_price,
)

app.register_blueprint(_splash_bp)
app.register_blueprint(_diagnostics_bp)
app.register_blueprint(_superlog_bp)
app.register_blueprint(_watchdog_bp)
app.register_blueprint(_boost_bp)
app.register_blueprint(_session_bp)
app.register_blueprint(_system_bp)
app.register_blueprint(_spacescan_bp)
app.register_blueprint(_market_bp)
app.register_blueprint(_sage_bp)
app.register_blueprint(_cat_bp)
app.register_blueprint(_config_bp)
app.register_blueprint(_coin_prep_bp)
app.register_blueprint(_offers_bp)
app.register_blueprint(_dashboard_bp)
app.register_blueprint(_smart_defaults_bp)
app.register_blueprint(_bot_bp)


# Re-export helpers that moved into blueprint modules so tests doing
# `patch.object(api_server, "_xxx", ...)` keep working unchanged.
from blueprints.market import _fetch_dbx_pair_status  # noqa: E402
from blueprints.smart_defaults import (  # noqa: E402
    _calculate_smart_defaults,
    _fetch_price_standalone,
    _fetch_dexie_orderbook_standalone,
)
from blueprints.offers import _build_fill_history_for_gui  # noqa: E402


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
        print("  Another bot instance appears to be running.")
        print("  Please close the other instance first (Ctrl+C in its terminal),")
        print("  or kill it via Task Manager (look for 'python api_server.py').")
        print("\n  Exiting to avoid running multiple instances.\n")
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

    # One-shot migration: mark all currently-designated reserve coins as
    # already-advised. Earlier coin-prep runs designated these coins but
    # didn't register them with the deposit advisor, so bot_health kept
    # re-raising "New XCH/CAT deposit" alerts for coins that were not
    # actually new. Runs once per install, gated by a settings flag.
    try:
        from database import get_setting, set_setting, get_reserve_coins
        if not get_setting("deposit_advisory_startup_backfill_v1"):
            raw = get_setting("deposit_advisory_advised_coins", "") or ""
            advised = {s.strip() for s in raw.split(",") if s.strip()}
            added = 0
            for _wt in ("xch", "cat"):
                try:
                    for _rc in (get_reserve_coins(_wt) or []):
                        _cid = _rc.get("coin_id") or ""
                        if _cid and _cid not in advised:
                            advised.add(_cid)
                            added += 1
                except Exception:
                    pass
            if added:
                set_setting("deposit_advisory_advised_coins", ",".join(sorted(advised)))
                print(f"  [DepositAdvisory] Backfilled {added} existing reserve coin(s)")
            set_setting("deposit_advisory_startup_backfill_v1", "1")
            # Best-effort: clear any currently-live advisory alerts so the
            # UI updates immediately instead of waiting for the next cycle.
            try:
                store = getattr(events, "_alert_store", None)
                if store is not None:
                    for item in list(store.get_active()):
                        _id = str(item.get("id", ""))
                        if _id.startswith("deposit_advisory_"):
                            store.clear(_id)
            except Exception:
                pass
    except Exception as _e:
        print(f"  [DepositAdvisory] Backfill skipped: {_e}")

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
    # These are stored in %APPDATA%\Catalyst\ and are never written to .env.
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

