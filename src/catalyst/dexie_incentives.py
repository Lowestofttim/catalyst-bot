"""Dexie liquidity-incentive program client.

Wraps Dexie's `/v1/incentives` endpoint, which is the authoritative source
for per-pair reward parameters. The previous code in `blueprints/market.py`
checked the ticker endpoint for an `incentives` field that does not exist
there, so `pair_incentivized` was permanently `None` for every CAT.

Each incentivized pair returns two entries (one per direction):

    - offered=XCH, requested=CAT  → "buy CAT" side (we sell XCH for CAT)
    - offered=CAT, requested=XCH  → "sell CAT" side (we sell CAT for XCH)

Each entry carries:
    - range: min/max offer size in the offered asset
    - maxSpread: max distance from market price for an offer to qualify
    - rewardRate: token + amount the pool distributes (e.g. 100 DBX/day)
    - withinSpread: total qualifying liquidity from all market makers
    - estimatedAPR: Dexie's own APR estimate for the pool
    - marketPrice: Dexie's reference market price for the pair

A small in-memory cache (5 min TTL) keeps load off Dexie's endpoint.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import requests

try:
    from config import cfg
except Exception:
    cfg = None  # type: ignore


_DEFAULT_BASE = "https://api.dexie.space"
_INCENTIVES_PATH = "/v1/incentives"
_CACHE_TTL_SECS = 300

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"fetched_at": 0.0, "data": None}


def _api_base() -> str:
    base = getattr(cfg, "DEXIE_API_BASE", _DEFAULT_BASE) if cfg else _DEFAULT_BASE
    return (base or _DEFAULT_BASE).rstrip("/")


def _normalize_asset_id(asset_id: str) -> str:
    if not asset_id:
        return ""
    a = asset_id.strip().lower()
    if a.startswith("0x"):
        a = a[2:]
    return a


def fetch_incentives(force: bool = False) -> Dict[str, Any]:
    """Return parsed `/v1/incentives` payload, cached for 5 minutes.

    Returns ``{"success": bool, "incentives": [...], "fetched_at": float}``.
    On network error the cache (even if stale) is returned with ``success=False``.
    """
    now = time.time()
    with _cache_lock:
        cached = _cache.get("data")
        cached_at = _cache.get("fetched_at", 0.0)
        if not force and cached and (now - cached_at) < _CACHE_TTL_SECS:
            return cached

    try:
        resp = requests.get(f"{_api_base()}{_INCENTIVES_PATH}", timeout=10)
        if resp.status_code != 200:
            with _cache_lock:
                if _cache.get("data"):
                    return _cache["data"]
            return {
                "success": False,
                "incentives": [],
                "fetched_at": now,
                "error": f"HTTP {resp.status_code}",
            }
        body = resp.json() or {}
        incentives = body.get("incentives") or []
        result = {
            "success": bool(body.get("success", True)),
            "incentives": incentives,
            "fetched_at": now,
        }
        with _cache_lock:
            _cache["data"] = result
            _cache["fetched_at"] = now
        return result
    except Exception as e:
        with _cache_lock:
            if _cache.get("data"):
                return _cache["data"]
        return {"success": False, "incentives": [], "fetched_at": now, "error": str(e)}


def _entry_direction(entry: Dict[str, Any]) -> str:
    """Identify whether an incentive entry is the buy-CAT or sell-CAT side.

    Returns "buy" if the entry rewards offering XCH for the CAT (we acquire CAT),
    "sell" if it rewards offering CAT for XCH (we sell CAT). Empty string if
    the entry is for a non-XCH pair we don't care about here.
    """
    offered = (entry.get("offered") or {}).get("id", "").lower()
    requested = (entry.get("requested") or {}).get("id", "").lower()
    if offered == "xch":
        return "buy"
    if requested == "xch":
        return "sell"
    return ""


def _serialize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Project a raw incentive entry into a stable, GUI-friendly shape."""
    rng = entry.get("range") or {}
    rate = entry.get("rewardRate") or {}
    return {
        "range_min": float(rng.get("min", 0) or 0),
        "range_max": float(rng.get("max", 0) or 0),
        "range_unit": (rng.get("code") or "").upper(),
        "max_spread_pct": float(entry.get("maxSpread", 0) or 0),
        "max_spread_bps": int(round(float(entry.get("maxSpread", 0) or 0) * 10000)),
        "reward_token": (rate.get("code") or "").upper(),
        "reward_token_id": rate.get("id", ""),
        "reward_amount_per_day": float(rate.get("amount", 0) or 0),
        "within_spread_liquidity": float(entry.get("withinSpread", 0) or 0),
        "estimated_apr": float(entry.get("estimatedAPR", 0) or 0),
        "market_price": float(entry.get("marketPrice", 0) or 0),
    }


def get_pair_incentives(asset_id: str, *, force: bool = False) -> Dict[str, Any]:
    """Return per-direction incentive parameters for a CAT/XCH pair.

    Returns:
        {
            "incentivized": bool,
            "buy": {...} or None,   # incentive for offers buying the CAT
            "sell": {...} or None,  # incentive for offers selling the CAT
            "fetched_at": float,
            "stale": bool,
        }
    """
    norm = _normalize_asset_id(asset_id)
    payload = fetch_incentives(force=force)
    out: Dict[str, Any] = {
        "incentivized": False,
        "buy": None,
        "sell": None,
        "fetched_at": payload.get("fetched_at", 0.0),
        "stale": not payload.get("success", False),
    }
    if not norm:
        return out

    for entry in payload.get("incentives", []) or []:
        offered_id = _normalize_asset_id((entry.get("offered") or {}).get("id", ""))
        requested_id = _normalize_asset_id((entry.get("requested") or {}).get("id", ""))
        if norm not in (offered_id, requested_id):
            continue
        direction = _entry_direction(entry)
        if direction not in ("buy", "sell"):
            continue
        out[direction] = _serialize_entry(entry)
        out["incentivized"] = True
    return out


def is_pair_incentivized(asset_id: str) -> Optional[bool]:
    """Three-state helper: True/False if known, None if Dexie unreachable."""
    payload = fetch_incentives()
    if not payload.get("success") and not payload.get("incentives"):
        return None
    return get_pair_incentives(asset_id).get("incentivized", False)


def clear_cache() -> None:
    """Drop cached incentive data (used by tests)."""
    with _cache_lock:
        _cache["data"] = None
        _cache["fetched_at"] = 0.0
