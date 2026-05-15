"""Centralized counter for outbound HTTP calls that bypass the per-service
managers (``dexie_manager``, ``coinset_client``, ``amm_monitor``, etc.).

The managers track calls they own. This module tracks the rest:

* Direct ``GET`` from Flask blueprints (Dexie tickers/offers, Tibet pairs)
  on Smart Settings refresh, intel-tab refresh, deposit advisor, etc.
* Direct helper calls (``tx_fees._coinset_fee_estimate``, single-offer
  fetches in ``bot_health``, token-info lookups in ``coin_manager``)
* Side fetches (CoinGecko XCH/USD, GitHub release polls)
* Init-time discovery (``cat_resolver``, ``doctor``)

Without this counter the diagnostics modal under-reports real traffic by
30+ requests a day. Every gap site calls :func:`record` once per HTTP
request issued; the diagnostics endpoint merges these counters into the
existing service panels so the operator sees the full picture in one
place.

Service names are stable strings — keep them short and lower-case so the
diagnostics endpoint can key off them directly:

  ``dexie``, ``tibetswap``, ``coinset``, ``coingecko``, ``github``,
  ``spacescan``

Thread-safety: a single ``RLock`` guards all mutation. The module is
designed to be cheap on the hot path — one dict lookup + one increment
under the lock — so call sites can use it without worrying about
contention.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

_lock = threading.RLock()
_counts: Dict[str, int] = {}
_endpoint_counts: Dict[str, Dict[str, int]] = {}
_last_call_ts: Dict[str, float] = {}
_session_started_at: float = time.time()


def record(service: str, endpoint: Optional[str] = None, count: int = 1) -> None:
    """Record one or more outbound HTTP calls to ``service``.

    Args:
        service: Stable lowercase service identifier
            (``"dexie"`` / ``"tibetswap"`` / ``"coinset"`` / ``"coingecko"``
            / ``"github"`` / ``"spacescan"``).
        endpoint: Optional path-level breakdown (``"v1/offers"``,
            ``"/pairs"``). Stored in a separate per-service map so the
            diagnostics modal can show "calls by endpoint" without
            inflating the total.
        count: Number of calls to record (defaults to 1). Useful when a
            wrapper batches retries — pass the actual count rather than
            looping calls to :func:`record`.

    Silently ignores invalid input so call sites never have to wrap this
    in a try/except. Tracking failures must never break trading.
    """
    if not service or count <= 0:
        return
    try:
        svc = str(service).strip().lower()
        if not svc:
            return
        with _lock:
            _counts[svc] = _counts.get(svc, 0) + int(count)
            if endpoint:
                ep_map = _endpoint_counts.setdefault(svc, {})
                ep_key = str(endpoint).strip()
                if ep_key:
                    ep_map[ep_key] = ep_map.get(ep_key, 0) + int(count)
            _last_call_ts[svc] = time.time()
    except Exception:
        # Tracking must never raise into the caller's hot path.
        pass


def get_count(service: str) -> int:
    """Return the cumulative call count for ``service`` since process
    start. 0 when the service has never been recorded."""
    if not service:
        return 0
    with _lock:
        return int(_counts.get(str(service).strip().lower(), 0))


def get_endpoint_breakdown(service: str) -> Dict[str, int]:
    """Return a copy of the per-endpoint counts for ``service``. Empty
    dict when no endpoint detail was recorded."""
    if not service:
        return {}
    with _lock:
        return dict(_endpoint_counts.get(str(service).strip().lower(), {}))


def get_last_call_ago_secs(service: str) -> Optional[float]:
    """Return seconds since the most recent call to ``service`` (or
    None if it's never been called)."""
    if not service:
        return None
    with _lock:
        ts = _last_call_ts.get(str(service).strip().lower())
    if ts is None:
        return None
    return max(0.0, time.time() - ts)


def get_all_stats() -> Dict[str, object]:
    """Return a snapshot of all counters. The diagnostics endpoint
    consumes this to merge into the per-service payloads."""
    now = time.time()
    with _lock:
        last = dict(_last_call_ts)
        return {
            "session_uptime_secs": round(now - _session_started_at, 1),
            "total_by_service": dict(_counts),
            "by_endpoint": {s: dict(eps) for s, eps in _endpoint_counts.items()},
            "last_call_ts": last,
            "last_call_ago_secs": {s: round(now - ts, 1) for s, ts in last.items()},
        }


def reset() -> None:
    """Clear all counters. Used by tests; not intended for runtime."""
    global _session_started_at
    with _lock:
        _counts.clear()
        _endpoint_counts.clear()
        _last_call_ts.clear()
        _session_started_at = time.time()
