"""Multi-source market data aggregation and analysis for Smart Defaults

Collects raw market data from Dexie trade history and ticker, TibetSwap
pool and quote endpoints, Spacescan token info, and internal DB metrics,
then derives analysis covering volatility, liquidity, token health, and
bot performance. The two top-level entry points are
`collect_all_market_data()` and `analyze_market_data()`; results feed
the Smart Defaults recommendation layer that tunes bot configuration.

Key responsibilities:
    - Fan out to Dexie, TibetSwap, Spacescan, and the internal DB
    - Normalize raw data for the analysis stage
    - Compute volatility, liquidity, health, and performance metrics
    - Cache results via `get_market_analysis_cache` /
      `set_market_analysis_cache` so repeated calls don't hammer APIs
"""

import time
import math
import threading
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

from config import cfg
from database import (
    get_connection, get_market_analysis_cache, set_market_analysis_cache,
    get_pool_snapshots, record_pool_snapshot
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEXIE_BASE = "https://api.dexie.space"
TIBET_BASE = "https://api.v2.tibetswap.io"
# F78 (2026-04-17): additional data sources for richer Smart Settings.
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINSET_BASE_DEFAULT = "https://api.coinset.org"

# Cache TTLs (minutes)
CACHE_TTL_TRADES = 60       # Dexie trade history — changes slowly
CACHE_TTL_TICKER = 5        # Dexie ticker — changes frequently
CACHE_TTL_SPACESCAN = 1440  # Spacescan token info — 24 hours (rate limited)
CACHE_TTL_TIBET = 30        # TibetSwap pool — matches price_engine cache
CACHE_TTL_ANALYSIS = 30     # Full analysis result
# F78 (2026-04-17):
CACHE_TTL_XCH_USD = 5       # XCH/USD fiat price — moves moderately
CACHE_TTL_BLOCKCHAIN = 1    # Chia blockchain state — block-cadence sensitive
CACHE_TTL_TRENDING = 10     # Dexie trending pairs — discovery use, not critical

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})

# Spacescan token-health calls are slow-changing Smart Defaults data, not the
# critical fill-verification path. Pace them separately so cache refreshes do
# not hammer the free tier and produce repeated 429 warning noise.
_SPACESCAN_SMART_PRO_INTERVAL = 2.0
_SPACESCAN_SMART_FREE_INTERVAL = 12.0
_SPACESCAN_SMART_429_COOLDOWN = 60.0
_SPACESCAN_SMART_WARN_DEDUP = 300.0
_spacescan_smart_lock = threading.Lock()
_spacescan_smart_last_call_at: Dict[str, float] = {}
_spacescan_smart_cooldown_until: Dict[str, float] = {}
_spacescan_smart_last_warned: Dict[str, float] = {}


# ===========================================================================
# PHASE 1: DATA COLLECTION
# ===========================================================================

def collect_all_market_data(asset_id: str, ticker_id: str,
                            decimals: int = 3,
                            progress_callback=None) -> Dict:
    """Gather all market data from external APIs + internal DB.

    This is the main entry point for Smart Defaults v2 data gathering.
    Each source is fetched independently — if one fails, the others
    still work. Results are cached to avoid hammering APIs.

    Args:
        asset_id: CAT asset ID (hex string)
        ticker_id: Dexie ticker ID (e.g. "SBX_XCH")
        decimals: CAT decimal places
        progress_callback: Optional fn(step, total, message) for GUI progress

    Returns dict with keys:
        dexie_trades, dexie_ticker, tibet_pool, tibet_quote,
        spacescan, internal_db, _metadata
    """
    def _progress(step, msg):
        if progress_callback:
            progress_callback(step, 6, msg)
        print(f"[MARKET_DATA] ({step}/6) {msg}")

    result = {
        "dexie_trades": None,
        "dexie_ticker": None,
        "tibet_pool": None,
        "tibet_quote": None,
        "spacescan": None,
        "internal_db": None,
        # F78 (2026-04-17): XCH/USD, blockchain state, Dexie trending
        "xch_usd": None,
        "blockchain_state": None,
        "dexie_trending": None,
        "_metadata": {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "sources_ok": [],
            "sources_failed": [],
            "cache_hits": [],
        }
    }

    meta = result["_metadata"]

    # ---- 1. Dexie Trade History (30 days) ----
    _progress(1, "Fetching Dexie trade history...")
    try:
        cached = get_market_analysis_cache(asset_id, "dexie_trades")
        if cached:
            result["dexie_trades"] = cached
            meta["cache_hits"].append("dexie_trades")
        else:
            trades = _fetch_dexie_trade_history(asset_id, days=30)
            if trades:
                result["dexie_trades"] = trades
                set_market_analysis_cache(asset_id, "dexie_trades", trades, CACHE_TTL_TRADES)
        if result["dexie_trades"]:
            meta["sources_ok"].append("dexie_trades")
        else:
            meta["sources_failed"].append("dexie_trades")
    except Exception as e:
        print(f"[MARKET_DATA] Dexie trades failed: {e}")
        meta["sources_failed"].append("dexie_trades")

    # ---- 2. Dexie Ticker (30d/90d ranges + current price) ----
    _progress(2, "Fetching Dexie ticker data...")
    try:
        cached = get_market_analysis_cache(asset_id, "dexie_ticker")
        if cached:
            result["dexie_ticker"] = cached
            meta["cache_hits"].append("dexie_ticker")
        else:
            ticker = _fetch_dexie_ticker_extended(ticker_id)
            if ticker and ticker.get("has_data"):
                result["dexie_ticker"] = ticker
                set_market_analysis_cache(asset_id, "dexie_ticker", ticker, CACHE_TTL_TICKER)
        if result["dexie_ticker"]:
            meta["sources_ok"].append("dexie_ticker")
        else:
            meta["sources_failed"].append("dexie_ticker")
    except Exception as e:
        print(f"[MARKET_DATA] Dexie ticker failed: {e}")
        meta["sources_failed"].append("dexie_ticker")

    # ---- 3. TibetSwap Pool + Quote ----
    _progress(3, "Fetching TibetSwap pool data...")
    try:
        cached = get_market_analysis_cache(asset_id, "tibet_pool")
        if cached:
            result["tibet_pool"] = cached
            meta["cache_hits"].append("tibet_pool")
        else:
            pool = _fetch_tibet_pool(asset_id, decimals)
            if pool and pool.get("has_data"):
                result["tibet_pool"] = pool
                set_market_analysis_cache(asset_id, "tibet_pool", pool, CACHE_TTL_TIBET)
                # Store snapshot for historical tracking
                record_pool_snapshot(
                    asset_id,
                    pool["xch_reserve"],
                    pool["cat_reserve"],
                    pool["price"]
                )
        if result["tibet_pool"]:
            meta["sources_ok"].append("tibet_pool")
            # Also get a quote for slippage estimation
            pair_id = result["tibet_pool"].get("pair_id", "")
            if pair_id:
                quote = _fetch_tibet_quote(pair_id, amount_mojos=10000000000)  # 0.01 XCH
                if quote:
                    result["tibet_quote"] = quote
                    meta["sources_ok"].append("tibet_quote")
        else:
            meta["sources_failed"].append("tibet_pool")
    except Exception as e:
        print(f"[MARKET_DATA] Tibet failed: {e}")
        meta["sources_failed"].append("tibet_pool")

    # ---- 4. Spacescan Token Analytics ----
    _progress(4, "Fetching Spacescan token data...")
    try:
        cached = get_market_analysis_cache(asset_id, "spacescan")
        if cached:
            result["spacescan"] = cached
            meta["cache_hits"].append("spacescan")
        else:
            spacescan = _fetch_spacescan_data(asset_id)
            if spacescan and spacescan.get("has_data"):
                # Merge with prior cache: if the new fetch is partial
                # (holders/activity sub-call failed), preserve the
                # previously-good values rather than overwriting them with 0.
                # Without this every Spacescan 429 silently bricks the
                # holder count + activity until the next successful full
                # fetch, which can be hours away on the free tier.
                spacescan = _merge_partial_spacescan(spacescan, asset_id)
                result["spacescan"] = spacescan
                _partial = (
                    int(spacescan.get("holder_count", 0) or 0) <= 0
                    or bool(spacescan.get("activity_fetch_failed"))
                )
                _ttl = 30 if _partial else CACHE_TTL_SPACESCAN  # 30 min vs 24 hr
                set_market_analysis_cache(asset_id, "spacescan", spacescan, _ttl)
        if result["spacescan"]:
            meta["sources_ok"].append("spacescan")
        else:
            meta["sources_failed"].append("spacescan")
    except Exception as e:
        print(f"[MARKET_DATA] Spacescan failed: {e}")
        meta["sources_failed"].append("spacescan")

    # ---- 5. Internal Database History ----
    _progress(5, "Querying internal database...")
    try:
        db_data = _fetch_internal_db_history(asset_id)
        result["internal_db"] = db_data
        if db_data and (db_data.get("price_count", 0) > 0 or db_data.get("fill_count", 0) > 0):
            meta["sources_ok"].append("internal_db")
        else:
            # Empty DB is expected on first run — not a failure
            meta["sources_ok"].append("internal_db_empty")
    except Exception as e:
        print(f"[MARKET_DATA] Internal DB failed: {e}")
        meta["sources_failed"].append("internal_db")

    # ---- 6. F78: XCH/USD price (CoinGecko) ----
    try:
        cached = get_market_analysis_cache("_global_", "xch_usd")
        if cached:
            result["xch_usd"] = cached
            meta["cache_hits"].append("xch_usd")
        else:
            xch_usd = _fetch_xch_usd_price()
            if xch_usd:
                result["xch_usd"] = xch_usd
                set_market_analysis_cache("_global_", "xch_usd", xch_usd, CACHE_TTL_XCH_USD)
        if result["xch_usd"]:
            meta["sources_ok"].append("xch_usd")
        else:
            # Fiat price is supplementary — missing it is non-fatal, don't
            # list as failure (wouldn't ding data-quality score either).
            meta["sources_ok"].append("xch_usd_empty")
    except Exception as e:
        print(f"[MARKET_DATA] XCH/USD fetch failed: {e}")
        meta["sources_ok"].append("xch_usd_empty")

    # ---- 7. F78: Coinset blockchain state ----
    try:
        cached = get_market_analysis_cache("_global_", "blockchain_state")
        if cached:
            result["blockchain_state"] = cached
            meta["cache_hits"].append("blockchain_state")
        else:
            bc = _fetch_coinset_blockchain_state()
            if bc:
                result["blockchain_state"] = bc
                set_market_analysis_cache("_global_", "blockchain_state", bc, CACHE_TTL_BLOCKCHAIN)
        if result["blockchain_state"]:
            meta["sources_ok"].append("blockchain_state")
        else:
            meta["sources_ok"].append("blockchain_state_empty")
    except Exception as e:
        print(f"[MARKET_DATA] Blockchain state fetch failed: {e}")
        meta["sources_ok"].append("blockchain_state_empty")

    # ---- 8. F78: Dexie trending pairs (market-wide context) ----
    try:
        cached = get_market_analysis_cache("_global_", "dexie_trending")
        if cached:
            result["dexie_trending"] = cached
            meta["cache_hits"].append("dexie_trending")
        else:
            trending = _fetch_dexie_trending_pairs(limit=20)
            if trending:
                result["dexie_trending"] = trending
                set_market_analysis_cache("_global_", "dexie_trending", trending, CACHE_TTL_TRENDING)
        if result["dexie_trending"]:
            meta["sources_ok"].append("dexie_trending")
        else:
            meta["sources_ok"].append("dexie_trending_empty")
    except Exception as e:
        print(f"[MARKET_DATA] Dexie trending fetch failed: {e}")
        meta["sources_ok"].append("dexie_trending_empty")

    _progress(6, "Data collection complete")

    total_ok = len([s for s in meta["sources_ok"] if not s.endswith("_empty")])
    total_sources = total_ok + len(meta["sources_failed"])
    print(f"[MARKET_DATA] Done: {total_ok}/{total_sources} sources OK, "
          f"{len(meta['cache_hits'])} cache hits")

    return result


# ---------------------------------------------------------------------------
# 1. Dexie Trade History Fetcher (paginated, 30 days)
# ---------------------------------------------------------------------------

def _fetch_dexie_trade_history(asset_id: str, days: int = 30) -> Optional[Dict]:
    """Fetch completed trades from Dexie for the last N days.

    Tries multiple API approaches (Dexie's API has changed over time):
      1. /v1/offers with status=4 (completed/taken) — single status value
      2. /v1/offers with status=4 for each direction separately
    Fetches both buy and sell sides, paginated.

    Returns dict with:
        trades: list of trade records
        total_count: total trades found
        daily_volume_xch: average daily volume
        avg_trade_size_xch: median trade size
        fills_per_day: average fills per day
        volume_trend: 'growing', 'stable', or 'declining'
        price_trend_pct: 30-day price change as percentage
    """
    if not asset_id:
        return None

    all_trades = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    # Fetch both directions: XCH→CAT (buys) and CAT→XCH (sells)
    for direction in ["buy", "sell"]:
        page = 1
        max_pages = 50  # Safety limit

        while page <= max_pages:
            try:
                if direction == "buy":
                    # Buys: XCH offered for CAT
                    params = {
                        "offered": "xch",
                        "requested": asset_id,
                        "status": 4,
                        "page_size": 100,
                        "page": page,
                        "sort": "date_completed",
                        "order": "desc",
                    }
                else:
                    # Sells: CAT offered for XCH
                    params = {
                        "offered": asset_id,
                        "requested": "xch",
                        "status": 4,
                        "page_size": 100,
                        "page": page,
                        "sort": "date_completed",
                        "order": "desc",
                    }

                resp = _session.get(f"{DEXIE_BASE}/v1/offers", params=params, timeout=15)
                if resp.status_code != 200:
                    print(f"[MARKET_DATA] Dexie trades ({direction}) HTTP {resp.status_code}")
                    break

                data = resp.json()
                offers = data.get("offers", [])
                if not offers:
                    break

                # Filter to our time window and extract trade data
                page_had_valid = False
                for offer in offers:
                    completed = offer.get("date_completed", "")
                    if not completed:
                        continue

                    # Check if within our time window
                    if completed < cutoff_str:
                        # Past our window — stop paginating this direction
                        page_had_valid = False
                        break

                    page_had_valid = True

                    # Extract amounts from offered/requested items.
                    # Dexie returns amounts in human-readable decimal units already
                    # (e.g. 2.0 XCH, 102799.183 BEPE) — no mojo conversion needed.
                    offered = offer.get("offered", [])
                    requested = offer.get("requested", [])

                    xch_amount = 0
                    cat_amount = 0
                    for item in offered + requested:
                        code = str(item.get("code", "")).upper()
                        if code == "XCH" or str(item.get("id", "")).lower() == "xch":
                            xch_amount = _safe_float(item.get("amount", 0))
                        else:
                            cat_amount = _safe_float(item.get("amount", 0))

                    # Compute price as XCH/CAT from parsed amounts.
                    # NOTE: Dexie's offer "price" field is CAT/XCH (inverted) —
                    # using it directly would produce VWAP ~25000 instead of ~0.00002.
                    price = xch_amount / cat_amount if cat_amount > 0 and xch_amount > 0 else 0

                    all_trades.append({
                        "date": completed,
                        "direction": direction,
                        "price": price,
                        "xch_amount": xch_amount,
                        "cat_amount": cat_amount,
                    })

                if not page_had_valid:
                    break

                # Check pagination
                total = data.get("total", 0)
                if page * 100 >= total:
                    break

                page += 1
                time.sleep(0.3)  # Rate-limit friendly

            except Exception as e:
                print(f"[MARKET_DATA] Dexie trades page {page} ({direction}) failed: {e}")
                break

    # If status=4 found nothing, try offered_or_requested approach as fallback
    if not all_trades:
        print("[MARKET_DATA] No trades from status=4, trying offered_or_requested fallback...")
        try:
            params = {
                "offered_or_requested": asset_id,
                "status": 4,
                "page_size": 100,
                "sort": "date_completed",
                "order": "desc",
            }
            resp = _session.get(f"{DEXIE_BASE}/v1/offers", params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for offer in data.get("offers", []):
                    completed = offer.get("date_completed", "")
                    if not completed or completed < cutoff_str:
                        continue
                    offered_items = offer.get("offered", [])
                    requested_items = offer.get("requested", [])
                    xch_amount = 0
                    cat_amount = 0
                    direction = "buy"
                    for item in offered_items + requested_items:
                        code = str(item.get("code", "")).upper()
                        if code == "XCH" or str(item.get("id", "")).lower() == "xch":
                            xch_amount = _safe_float(item.get("amount", 0))
                        else:
                            cat_amount = _safe_float(item.get("amount", 0))
                    # Determine direction from which side had XCH
                    for item in offered_items:
                        code = str(item.get("code", "")).upper()
                        if code == "XCH" or str(item.get("id", "")).lower() == "xch":
                            direction = "buy"
                            break
                        else:
                            direction = "sell"
                    # Compute XCH/CAT price from amounts (Dexie "price" field is CAT/XCH inverted)
                    price = xch_amount / cat_amount if cat_amount > 0 and xch_amount > 0 else 0
                    all_trades.append({
                        "date": completed,
                        "direction": direction,
                        "price": price,
                        "xch_amount": xch_amount,
                        "cat_amount": cat_amount,
                    })
                if all_trades:
                    print(f"[MARKET_DATA] Fallback found {len(all_trades)} trades")
        except Exception as e:
            print(f"[MARKET_DATA] Dexie fallback failed: {e}")

    if not all_trades:
        print(f"[MARKET_DATA] No completed trades found on Dexie in last {days} days")
        return None

    # Sort by date (newest first)
    all_trades.sort(key=lambda t: t["date"], reverse=True)

    # --- Outlier filtering: remove anomalous spike trades before statistics ---
    # A single off-market trade (e.g. someone accidentally paying 0.9648 XCH/MZ
    # instead of 0.000115) would dominate VWAP and trigger false extreme-volatility
    # flags. Filter any trade whose price is more than 10x or less than 1/10th
    # of the median — these are data anomalies, not real market volatility.
    raw_prices = [t["price"] for t in all_trades if t["price"] > 0]
    if len(raw_prices) >= 5:
        sorted_prices = sorted(raw_prices)
        median_price = sorted_prices[len(sorted_prices) // 2]
        if median_price > 0:
            outlier_lo = median_price / 10
            outlier_hi = median_price * 10
            filtered = [t for t in all_trades if t["price"] <= 0 or outlier_lo <= t["price"] <= outlier_hi]
            n_removed = len(all_trades) - len(filtered)
            if n_removed > 0:
                print(f"[MARKET_DATA] Outlier filter: removed {n_removed} trade(s) "
                      f"outside [{outlier_lo:.8f}, {outlier_hi:.8f}] "
                      f"(median={median_price:.8f})")
                all_trades = filtered

    # --- Calculate summary statistics ---
    xch_amounts = [t["xch_amount"] for t in all_trades if t["xch_amount"] > 0]
    prices = [t["price"] for t in all_trades if t["price"] > 0]

    total_volume = sum(xch_amounts)
    avg_trade_size = sorted(xch_amounts)[len(xch_amounts) // 2] if xch_amounts else 0  # median
    fills_per_day = len(all_trades) / max(1, days)
    daily_volume = total_volume / max(1, days)

    # --- Volume trend: compare first half vs second half ---
    mid_idx = len(all_trades) // 2
    if mid_idx > 5:
        recent_vol = sum(t["xch_amount"] for t in all_trades[:mid_idx])
        older_vol = sum(t["xch_amount"] for t in all_trades[mid_idx:])
        if older_vol > 0:
            vol_ratio = recent_vol / older_vol
            if vol_ratio > 1.3:
                volume_trend = "growing"
            elif vol_ratio < 0.7:
                volume_trend = "declining"
            else:
                volume_trend = "stable"
        else:
            volume_trend = "growing"
    else:
        volume_trend = "insufficient_data"

    # --- Price trend: compare oldest 10% average vs newest 10% ---
    price_trend_pct = 0
    if len(prices) >= 10:
        slice_size = max(3, len(prices) // 10)
        recent_avg = sum(prices[:slice_size]) / slice_size
        oldest_avg = sum(prices[-slice_size:]) / slice_size
        if oldest_avg > 0:
            price_trend_pct = ((recent_avg - oldest_avg) / oldest_avg) * 100

    return {
        "trades": all_trades[:200],  # Cap stored trades to prevent huge cache entries
        "total_count": len(all_trades),
        "total_volume_xch": round(total_volume, 4),
        "daily_volume_xch": round(daily_volume, 4),
        "avg_trade_size_xch": round(avg_trade_size, 4),
        "fills_per_day": round(fills_per_day, 2),
        "volume_trend": volume_trend,
        "price_trend_pct": round(price_trend_pct, 2),
        "days_covered": days,
    }


# ---------------------------------------------------------------------------
# 2. Dexie Ticker Extended (30d/90d ranges)
# ---------------------------------------------------------------------------

def _fetch_dexie_ticker_extended(ticker_id: str) -> Optional[Dict]:
    """Fetch full Dexie ticker with 30d/90d/1y range data.

    The ticker endpoint returns much more than just current price —
    it has 30d/90d high/low/volume fields that are perfect for
    calibrating spreads and safety rails.
    """
    if not ticker_id:
        return None

    if "_" not in ticker_id:
        ticker_id = f"{ticker_id}_XCH"

    try:
        resp = _session.get(
            f"{DEXIE_BASE}/v2/prices/tickers",
            params={"ticker_id": ticker_id},
            timeout=10
        )
        if resp.status_code != 200:
            return None

        tickers = resp.json().get("tickers", [])
        if not tickers:
            return None

        t = tickers[0]

        # Extract all available fields
        result = {
            "has_data": True,
            # Current price
            "price": _safe_float(t.get("last_price", 0)),
            "bid": _safe_float(t.get("bid", t.get("best_bid", 0))),
            "ask": _safe_float(t.get("ask", t.get("best_ask", 0))),
            # 24h data (may be empty for low-frequency tokens)
            "high_24h": _safe_float(t.get("high", t.get("high_24h", 0))),
            "low_24h": _safe_float(t.get("low", t.get("low_24h", 0))),
            "volume_24h": _safe_float(t.get("base_volume", 0)),
            # 30d data (confirmed working for MZ)
            "high_30d": _safe_float(t.get("high_30d", 0)),
            "low_30d": _safe_float(t.get("low_30d", 0)),
            "volume_30d": _safe_float(t.get("base_volume_30d", 0)),
            "price_30d": _safe_float(t.get("price_30d", 0)),
            # 90d data
            "high_90d": _safe_float(t.get("high_90d", 0)),
            "low_90d": _safe_float(t.get("low_90d", 0)),
            "price_90d": _safe_float(t.get("price_90d", 0)),
            # 1y data
            "high_1y": _safe_float(t.get("high_1y", 0)),
            "low_1y": _safe_float(t.get("low_1y", 0)),
        }

        # Better price: prefer bid/ask midpoint
        if result["bid"] > 0 and result["ask"] > 0:
            result["price"] = (result["bid"] + result["ask"]) / 2

        return result

    except Exception as e:
        print(f"[MARKET_DATA] Dexie ticker failed: {e}")
        return None


# ---------------------------------------------------------------------------
# 3. TibetSwap Pool + Quote
# ---------------------------------------------------------------------------

def _fetch_tibet_pool(asset_id: str, decimals: int = 3) -> Optional[Dict]:
    """Fetch TibetSwap pool reserves and pair info.

    Uses the same pair-matching logic as price_engine.py (confirmed working).
    """
    if not asset_id:
        return None

    try:
        resp = _session.get(
            f"{TIBET_BASE}/pairs",
            params={"skip": 0, "limit": 200},
            timeout=10
        )
        if resp.status_code != 200:
            return None

        pairs = resp.json()
        if not isinstance(pairs, list):
            return None

        # Normalize and match asset ID (same logic as price_engine._find_tibet_pair)
        normalized = asset_id.lower().strip()
        if normalized.startswith("0x"):
            normalized = normalized[2:]

        for pair in pairs:
            pair_asset = str(pair.get("asset_id", "")).lower().strip()
            if pair_asset.startswith("0x"):
                pair_asset = pair_asset[2:]

            # Exact match only — never strip trailing zeros from hex
            # asset IDs (distinct CATs can differ only in trailing hex
            # digits, and rstrip("0") would collide them).
            if pair_asset == normalized:
                xch_raw = _safe_float(pair.get("xch_reserve", 0))
                tok_raw = _safe_float(pair.get("token_reserve", 0))

                if xch_raw > 0 and tok_raw > 0:
                    xch_reserve = xch_raw / 1e12
                    cat_reserve = tok_raw / (10 ** decimals)
                    price = xch_reserve / cat_reserve if cat_reserve > 0 else 0

                    return {
                        "has_data": True,
                        "pair_id": pair.get("pair_id", ""),
                        "xch_reserve": round(xch_reserve, 6),
                        "cat_reserve": round(cat_reserve, 2),
                        "price": price,
                        "liquidity": _safe_float(pair.get("liquidity", 0)),
                        "asset_name": pair.get("asset_short_name", ""),
                    }

        return None

    except Exception as e:
        print(f"[MARKET_DATA] Tibet pool failed: {e}")
        return None


def _fetch_tibet_quote(pair_id: str, amount_mojos: int = 10000000000) -> Optional[Dict]:
    """Get a swap quote from TibetSwap for slippage estimation.

    Endpoint: GET /quote/{pair_id}?amount_in=X&xch_is_input=true
    Confirmed working in test_api_data_sources.py.
    """
    if not pair_id:
        return None

    try:
        resp = _session.get(
            f"{TIBET_BASE}/quote/{pair_id}",
            params={
                "amount_in": str(amount_mojos),
                "xch_is_input": "true"
            },
            timeout=8
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        return {
            "amount_in": _safe_float(data.get("amount_in", 0)),
            "amount_out": _safe_float(data.get("amount_out", 0)),
            "price_impact": _safe_float(data.get("price_impact", 0)),
            "fee": _safe_float(data.get("fee", 0)),
        }

    except Exception as e:
        print(f"[MARKET_DATA] Tibet quote failed: {e}")
        return None


# ---------------------------------------------------------------------------
# F78 (2026-04-17) — additional data sources
# ---------------------------------------------------------------------------

def _fetch_xch_usd_price() -> Optional[Dict]:
    """Fetch XCH/USD fiat price from CoinGecko.

    Free, no API key required. Used by Smart Settings to:
      - Display settings + ladder values in USD alongside XCH
      - Detect systemic XCH moves (e.g. XCH +15% today → widen spreads)
        separately from token-specific volatility
    Returns None on any failure — caller treats as "USD data unavailable".
    """
    try:
        try:
            from api_call_tracker import record as _record_api_call
            _record_api_call("coingecko", "/simple/price")
        except Exception:
            pass
        resp = _session.get(
            f"{COINGECKO_BASE}/simple/price",
            params={
                "ids": "chia",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        chia = data.get("chia") or {}
        if not chia.get("usd"):
            return None
        return {
            "has_data": True,
            "xch_usd": _safe_float(chia.get("usd", 0)),
            "xch_usd_24h_change_pct": _safe_float(chia.get("usd_24h_change", 0)),
            "last_updated_at": int(chia.get("last_updated_at", 0) or 0),
        }
    except Exception as e:
        print(f"[MARKET_DATA] CoinGecko XCH/USD fetch failed: {e}")
        return None


def _fetch_coinset_blockchain_state() -> Optional[Dict]:
    """Fetch current Chia blockchain state from Coinset.

    Returns peak height, network difficulty, sync status, mempool
    volume. Used by Smart Settings to refine fee timing and detect
    network congestion (high mempool → widen fee estimates).
    """
    try:
        base = str(
            getattr(cfg, "COINSET_API_URL", COINSET_BASE_DEFAULT)
            or COINSET_BASE_DEFAULT
        ).rstrip("/")
        resp = _session.post(
            f"{base}/get_blockchain_state",
            json={},
            headers={"content-type": "application/json"},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("success"):
            return None
        state = data.get("blockchain_state") or {}
        if not state:
            return None
        peak = state.get("peak") or {}
        # mempool_size / mempool_cost are sibling fields on the response;
        # Coinset mirrors the full-node shape.
        return {
            "has_data": True,
            "peak_height": int(peak.get("height", 0) or 0),
            "peak_timestamp": int(peak.get("timestamp", 0) or 0),
            "sync_progress_pct": _safe_float(state.get("sync", {}).get("sync_progress_height", 0)),
            "synced": bool(state.get("sync", {}).get("synced", False)),
            "mempool_size": int(data.get("mempool_size", 0) or 0),
            "mempool_cost": int(data.get("mempool_cost", 0) or 0),
            "mempool_min_fees": int(data.get("mempool_min_fees", 0) or 0),
            "space_network": _safe_float(state.get("space", 0)),
            "difficulty": int(state.get("difficulty", 0) or 0),
        }
    except Exception as e:
        print(f"[MARKET_DATA] Coinset blockchain_state fetch failed: {e}")
        return None


def _fetch_dexie_trending_pairs(limit: int = 20) -> Optional[Dict]:
    """Fetch Dexie's top pairs by 24h volume.

    Feeds the Smart Settings "market context" layer — is the bot's pair
    moving in line with the broader market? Currently informational; a
    future pass could correlate the bot's CAT vs top-10 for systemic-vs-
    idiosyncratic vol decomposition.
    """
    try:
        resp = _session.get(
            f"{DEXIE_BASE}/v1/pairs",
            params={"sort": "volume_24h", "dir": "desc", "page_size": int(limit)},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs") or data.get("data") or []
        if not isinstance(pairs, list) or not pairs:
            return None
        # Keep only the fields Smart Settings actually uses
        trimmed = []
        for p in pairs[:limit]:
            if not isinstance(p, dict):
                continue
            trimmed.append({
                "ticker": str(p.get("ticker_id") or p.get("id") or ""),
                "name": str(p.get("name") or ""),
                "volume_24h": _safe_float(p.get("volume_24h", 0)),
                "price": _safe_float(p.get("price", 0)),
                "price_change_24h_pct": _safe_float(p.get("price_change_24h", 0)),
            })
        return {
            "has_data": bool(trimmed),
            "pairs": trimmed,
            "total_volume_24h": sum(p["volume_24h"] for p in trimmed),
        }
    except Exception as e:
        print(f"[MARKET_DATA] Dexie trending pairs fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# 4. Spacescan Token Analytics
# ---------------------------------------------------------------------------

def _spacescan_smart_headers(api_key: str = "") -> Dict[str, str]:
    """Headers that keep Spacescan token endpoints on the documented v1/xch lane."""
    headers = {
        "Accept": "application/json",
        "version": "v1",
        "network": "xch",
    }
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _spacescan_smart_timeout() -> tuple[int, int]:
    """Smart Defaults can wait longer on token analytics than live bot loops can."""
    read_timeout = max(int(getattr(cfg, "SPACESCAN_TIMEOUT", 10)), 20)
    return (5, read_timeout)


def _spacescan_smart_key(base_url: str, headers: Optional[Dict[str, str]]) -> tuple[str, bool]:
    """Return a cooldown bucket key and whether the request uses a Pro key."""
    has_key = bool((headers or {}).get("x-api-key"))
    tier = "pro" if has_key else "free"
    return f"{tier}:{base_url.rstrip('/')}", has_key


def _spacescan_smart_before_request(key: str, is_pro: bool) -> Optional[str]:
    """Return an error string if a Spacescan request should be skipped."""
    now = time.time()
    interval = _SPACESCAN_SMART_PRO_INTERVAL if is_pro else _SPACESCAN_SMART_FREE_INTERVAL

    with _spacescan_smart_lock:
        cooldown_until = _spacescan_smart_cooldown_until.get(key, 0.0)
        if now < cooldown_until:
            remaining = int(max(1, cooldown_until - now))
            return f"HTTP 429 cooldown active ({remaining}s)"

        last_call_at = _spacescan_smart_last_call_at.get(key, 0.0)
        wait_for = max(0.0, (last_call_at + interval) - now)
        scheduled_at = now + wait_for
        _spacescan_smart_last_call_at[key] = scheduled_at

    if wait_for > 0:
        time.sleep(wait_for)
    return None


def _spacescan_smart_set_cooldown(key: str) -> None:
    with _spacescan_smart_lock:
        _spacescan_smart_cooldown_until[key] = time.time() + _SPACESCAN_SMART_429_COOLDOWN


def _spacescan_smart_report_once(key: str, message: str) -> None:
    now = time.time()
    with _spacescan_smart_lock:
        last = _spacescan_smart_last_warned.get(key, 0.0)
        if now - last < _SPACESCAN_SMART_WARN_DEDUP:
            return
        _spacescan_smart_last_warned[key] = now
    print(message)


def _spacescan_smart_get(base_url: str, endpoint: str, *,
                         params: Optional[Dict[str, Any]] = None,
                         headers: Optional[Dict[str, str]] = None,
                         retries: int = 1) -> tuple[Optional[Dict], Optional[str]]:
    """Request a Spacescan endpoint with light retry/backoff for transient slowness."""
    url = f"{base_url.rstrip('/')}{endpoint}"
    timeout = _spacescan_smart_timeout()
    last_error = "unknown error"
    cooldown_key, is_pro = _spacescan_smart_key(base_url, headers)

    for attempt in range(retries + 1):
        cooldown_error = _spacescan_smart_before_request(cooldown_key, is_pro)
        if cooldown_error:
            return None, cooldown_error

        try:
            resp = _session.get(url, params=params, headers=headers, timeout=timeout)

            if resp.status_code == 429:
                last_error = "HTTP 429"
                _spacescan_smart_set_cooldown(cooldown_key)
                return None, last_error
            if resp.status_code in {500, 502, 503, 504}:
                last_error = f"HTTP {resp.status_code}"
            else:
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("status") not in (None, "success"):
                    last_error = f"status={data.get('status')}"
                else:
                    # Count this call in the central Spacescan stats
                    try:
                        import spacescan as _ss
                        _ss.record_external_call()
                    except Exception:
                        pass
                    return data, None

        except requests.exceptions.Timeout:
            last_error = f"read timeout after {timeout[1]}s"
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        except ValueError as e:
            last_error = f"invalid JSON: {e}"

        if attempt < retries:
            time.sleep(1 + attempt)

    return None, last_error


def _spacescan_count_from_payload(payload: Any, *,
                                  count_keys: Optional[List[str]] = None,
                                  list_keys: Optional[List[str]] = None) -> int:
    """Best-effort count extraction across the response shapes Spacescan returns."""
    count_keys = count_keys or ["count", "total", "total_count"]
    list_keys = list_keys or ["data", "holders", "activities", "items", "results"]
    best_list_len = 0

    def _coerce_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except Exception:
                return None
        return None

    def _scan(node: Any) -> Optional[int]:
        nonlocal best_list_len
        if isinstance(node, list):
            best_list_len = max(best_list_len, len(node))
            return None
        if not isinstance(node, dict):
            return None

        for key in count_keys:
            value = _coerce_int(node.get(key))
            if value is not None:
                return value

        for key in list_keys:
            value = node.get(key)
            if isinstance(value, list):
                best_list_len = max(best_list_len, len(value))

        for key in ("data", "meta", "pagination", "page", "result"):
            nested = node.get(key)
            value = _scan(nested)
            if value is not None:
                return value
        return None

    explicit = _scan(payload)
    if explicit is not None:
        return explicit
    return best_list_len


def _merge_partial_spacescan(new_payload: Dict, asset_id: str) -> Dict:
    """Merge a freshly-fetched Spacescan payload with the prior cached one,
    preserving good fields when the new fetch is partial.

    The Spacescan free tier exposes holder/activity data via separate
    endpoints (`/holders`, `/activities`). When those sub-calls hit a 429
    or timeout, the parent fetcher returns has_data=True with
    holder_count=0, activity_count=0, and activity_fetch_failed=True.
    Without this merge the cache would overwrite a previously-good 3,393
    holders with 0 every time a sub-call hiccupped — effectively bricking
    the dashboard's holder count whenever Spacescan rate-limited us.

    Strategy: if the new fetch is partial AND we have a non-zero value
    cached, keep the cached value for that field. Other fields (price,
    supply, name) always come from the fresh payload since they're cheap
    to refetch and rarely change.
    """
    if not new_payload or not isinstance(new_payload, dict):
        return new_payload or {}

    new_holder = int(new_payload.get("holder_count", 0) or 0)
    new_activity = int(new_payload.get("activity_count", 0) or 0)
    activity_failed = bool(new_payload.get("activity_fetch_failed"))

    holder_partial = (new_holder <= 0)
    activity_partial = (new_activity <= 0) or activity_failed
    if not holder_partial and not activity_partial:
        return new_payload  # full fetch — no merge needed

    try:
        prior = get_market_analysis_cache(asset_id, "spacescan") or {}
    except Exception:
        return new_payload

    merged = dict(new_payload)
    if holder_partial:
        prior_holder = int(prior.get("holder_count", 0) or 0)
        if prior_holder > 0:
            merged["holder_count"] = prior_holder
            merged["holder_count_from_prior_cache"] = True
    if activity_partial:
        prior_activity = int(prior.get("activity_count", 0) or 0)
        if prior_activity > 0:
            merged["activity_count"] = prior_activity
            merged["activity_count_from_prior_cache"] = True
    return merged


def refresh_spacescan_cache(asset_id: str) -> Optional[Dict]:
    """Re-fetch Spacescan token data and update the cache.

    Standalone helper for the periodic cache-staleness self-heal driven
    by `bot_health.check_spacescan_cache_stale`. Returns the fresh payload
    on success, or None if the fetch failed / returned no data.

    Respects the same partial-failure TTL logic as the full market-data
    collector: when activity or holders silently returned empty, we cache
    with a 30-min TTL so the next refresh retries promptly rather than
    baking the hiccup into the full 24-hour window.
    """
    if not asset_id:
        return None
    try:
        payload = _fetch_spacescan_data(asset_id)
    except Exception as e:
        print(f"[MARKET_DATA] Spacescan refresh failed: {e}")
        return None
    if not payload or not payload.get("has_data"):
        return None
    try:
        # Merge with prior cache before persisting — see _merge_partial_spacescan.
        payload = _merge_partial_spacescan(payload, asset_id)
        _partial = (
            int(payload.get("holder_count", 0) or 0) <= 0
            or bool(payload.get("activity_fetch_failed"))
        )
        _ttl = 30 if _partial else CACHE_TTL_SPACESCAN
        set_market_analysis_cache(asset_id, "spacescan", payload, _ttl)
    except Exception as e:
        print(f"[MARKET_DATA] Spacescan cache write failed: {e}")
        # Still return the fresh payload — caller may use it even if the
        # persist failed. Next refresh will retry the write naturally.
    return payload


def _fetch_spacescan_data(asset_id: str) -> Optional[Dict]:
    """Fetch token info from Spacescan (free tier + Pro API).

    Endpoints confirmed from docs.spacescan.io:
      - /token/info/{id}?include_price=true&include_supply=true (free)
      - /token/holders/{id} (Pro recommended)
      - /token/activity?asset_id=X (Pro recommended)

    Uses Pro API with x-api-key header when key is available.
    """
    if not asset_id:
        return None

    result = {
        "has_data": False,
        "name": "",
        "symbol": "",
        "precision": 0,
        "price_usd": 0,
        "price_xch": 0,
        "total_supply": 0,
        "circulating_supply": 0,
        "holder_count": 0,
        "activity_count": 0,
    }

    api_key = (cfg.SPACESCAN_API_KEY if hasattr(cfg, "SPACESCAN_API_KEY") else "").strip()
    free_url = getattr(cfg, "SPACESCAN_FREE_URL", "https://api.spacescan.io")
    pro_url = getattr(cfg, "SPACESCAN_PRO_URL", "https://pro-api.spacescan.io")
    pro_headers = _spacescan_smart_headers(api_key)
    free_headers = _spacescan_smart_headers()

    # ---- Token Info (prefer Pro when available, then free fallback) ----
    info_errors: List[str] = []
    info_attempts = []
    if api_key:
        info_attempts.append((pro_url, pro_headers, "pro"))
    info_attempts.append((free_url, free_headers, "free"))

    for idx, (base_url, headers, label) in enumerate(info_attempts):
        data, err = _spacescan_smart_get(
            base_url,
            f"/token/info/{asset_id}",
            params={"include_price": "true", "include_supply": "true"},
            headers=headers,
            retries=1,
        )
        if data:
            info = data.get("info", {})
            price = data.get("price", {})
            supply = data.get("supply", {})

            result["name"] = info.get("name", "")
            result["symbol"] = info.get("symbol", "")
            result["precision"] = info.get("precision", 0)
            result["price_usd"] = _safe_float(price.get("usd", 0))
            result["price_xch"] = _safe_float(price.get("xch", 0))
            result["total_supply"] = _safe_float(supply.get("total_supply", 0))
            result["circulating_supply"] = _safe_float(supply.get("circulating_supply", 0))
            # Token icon — Spacescan serves higher-res than Dexie CDN (22KB vs 5KB).
            # preview_url is the full CDN URL; image_url is just the filename.
            result["token_preview_url"] = (
                info.get("preview_url") or data.get("preview_url") or
                info.get("image_url") or data.get("image_url") or ""
            )
            result["has_data"] = True
            break
        if err:
            info_errors.append(f"{label}: {err}")
        if idx < len(info_attempts) - 1:
            time.sleep(1)

    if not result["has_data"] and info_errors:
        joined = " | ".join(info_errors)
        _spacescan_smart_report_once(
            f"info:{joined}",
            f"[MARKET_DATA] Spacescan info failed: {joined}",
        )

    # ---- Holders ----
    # The /token/holders endpoint returns { "tokens": [...], "count": N, "total_count": REAL_TOTAL }
    # We only need total_count — use count=1 to minimise response size.
    # Pro key preferred (higher rate limits), free tier works too.
    time.sleep(1)
    holder_attempts = []
    if api_key:
        holder_attempts.append((pro_url, pro_headers, "pro"))
    holder_attempts.append((free_url, free_headers, "free"))

    for h_idx, (h_base, h_headers, h_label) in enumerate(holder_attempts):
        data, err = _spacescan_smart_get(
            h_base,
            f"/token/holders/{asset_id}",
            params={"count": 1},  # Minimal — we only need total_count
            headers=h_headers,
            retries=1,
        )
        if data:
            # Prefer total_count (real holder total) over count (page size) or list length
            total_count = data.get("total_count") or data.get("total_holders") or data.get("holder_count")
            if total_count is not None:
                try:
                    result["holder_count"] = int(total_count)
                except (TypeError, ValueError):
                    pass
            if result["holder_count"] == 0:
                # Fallback: count the returned list if no total field
                result["holder_count"] = _spacescan_count_from_payload(
                    data,
                    count_keys=["total_count", "total_holders", "holder_count"],
                    list_keys=["tokens", "data", "holders", "items", "results"],
                )
            if result["holder_count"] > 0:
                break  # Got a good answer — don't retry with free tier
        elif err:
            _spacescan_smart_report_once(
                f"holders:{h_label}:{err}",
                f"[MARKET_DATA] Spacescan holders ({h_label}) failed: {err}",
            )
        if h_idx < len(holder_attempts) - 1:
            time.sleep(1)

    # ---- Activity ----
    # F39 (2026-04-08): fixed indentation — was previously nested INSIDE
    # the holders loop, so it ran once per holder attempt and only ever
    # used the pro URL (ignoring the free fallback).
    #
    # F77 (2026-04-17): reordered attempts and tightened retry:
    #   1. pro-legacy — `/token/activity?asset_id=X` — proven working
    #   2. free       — `/token/activities/X` — community endpoint
    #   3. pro-plural — `/token/activities/X` — pro endpoint, often 404s
    #                   for many tokens (observed on MZ); keep as last
    #                   resort so the preceding tiers get first crack.
    # Retries bumped from 1→2 (3 attempts per endpoint) and we
    # interject a 3s sleep between endpoints when the previous attempt
    # hit HTTP 429 — gives the free rate-limit a chance to clear before
    # we waste another attempt on it.
    time.sleep(1)
    activity_errors: List[str] = []
    activity_attempts: List[tuple] = []
    if api_key:
        activity_attempts.append(
            (pro_url, "/token/activity",
             {"asset_id": asset_id, "type": "transfer", "count": 100},
             pro_headers, "pro-legacy")
        )
    activity_attempts.append(
        (free_url, f"/token/activities/{asset_id}",
         {"count": 100}, free_headers, "free")
    )
    if api_key:
        activity_attempts.append(
            (pro_url, f"/token/activities/{asset_id}",
             {"count": 100}, pro_headers, "pro-plural")
        )
    last_was_rate_limited = False
    for base_url, endpoint, params, headers, label in activity_attempts:
        if last_was_rate_limited:
            # Rate-limit cooldown — 3s is empirically enough for the
            # Spacescan free tier's sliding window to reset.
            time.sleep(3)
        data, err = _spacescan_smart_get(
            base_url, endpoint, params=params, headers=headers, retries=2,
        )
        last_was_rate_limited = bool(err and "429" in err)
        if data:
            result["activity_count"] = _spacescan_count_from_payload(
                data,
                count_keys=["activity_count", "activities", "count", "total", "total_count"],
                list_keys=["data", "activities", "items", "results"],
            )
            if result["activity_count"] > 0:
                break
        if err:
            activity_errors.append(f"{label}: {err}")

    if result["activity_count"] == 0 and activity_errors:
        joined = " | ".join(activity_errors)
        _spacescan_smart_report_once(
            f"activity:{joined}",
            f"[MARKET_DATA] Spacescan activity failed: {joined}",
        )
        # F77: surface the failure to the data-quality layer. Previously
        # the score ignored activity altogether; now set a flag so the
        # quality label can note "token health partial".
        result["activity_fetch_failed"] = True

    # F40 (2026-04-08): supply data lives inside /token/info/{id}'s
    # `supply.{total_supply, circulating_supply}` block — verified live
    # against api.spacescan.io for MZ. The dedicated /cat/total-supply
    # and /cat/circulating-supply endpoints DO NOT EXIST on Spacescan
    # (both 404 for any CAT). The /token/info parser at the top of this
    # function already pulls both fields out via supply.get("..."), so
    # no fallback is needed. If the values are still 0 here it means
    # /token/info itself failed for this CAT.

    return result


# ---------------------------------------------------------------------------
# 5. Internal Database History
# ---------------------------------------------------------------------------

def _fetch_internal_db_history(asset_id: str) -> Dict:
    """Query our own database for the bot's historical performance.

    This is the best data source because it's YOUR actual results.
    Returns zeros/empty if bot hasn't run yet (expected on first setup).
    """
    result = {
        "price_count": 0,
        "fill_count": 0,
        "buy_fills": 0,
        "sell_fills": 0,
        "total_fill_volume_xch": 0,
        "avg_fill_size_xch": 0,
        "inventory_snapshots": 0,
        "latest_net_position": 0,
        "pool_snapshots": 0,
        "pool_trend": "unknown",
        # F78 (2026-04-17): bot's own-fill volatility — rolling std-dev
        # of our actual fill prices. Uses ground-truth data we already
        # store; previously ignored despite being the best volatility
        # signal we have (Dexie trades are noisier aggregates).
        "own_fill_stddev_pct": 0.0,      # std-dev of log-returns ×100
        "own_fill_range_pct": 0.0,        # (max-min)/mean ×100 of recent fills
        "own_fill_samples": 0,            # number of fills used in the calc
    }

    conn = get_connection()

    try:
        # Price history count (30 days)
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM price_history "
            "WHERE cat_asset_id = ? AND timestamp >= datetime('now', '-30 days')",
            (asset_id,)
        ).fetchone()
        result["price_count"] = row["cnt"] if row else 0

        # Fill history (30 days)
        row = conn.execute(
            "SELECT COUNT(*) as cnt, "
            "SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) as buys, "
            "SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) as sells, "
            "SUM(CAST(size_xch AS REAL)) as total_vol, "
            "AVG(CAST(size_xch AS REAL)) as avg_size "
            "FROM fills WHERE cat_asset_id = ? AND filled_at >= datetime('now', '-30 days')",
            (asset_id,)
        ).fetchone()
        if row and row["cnt"]:
            result["fill_count"] = row["cnt"]
            result["buy_fills"] = row["buys"] or 0
            result["sell_fills"] = row["sells"] or 0
            result["total_fill_volume_xch"] = round(_safe_float(row["total_vol"]), 4)
            result["avg_fill_size_xch"] = round(_safe_float(row["avg_size"]), 4)

        # Inventory snapshots
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM inventory "
            "WHERE cat_asset_id = ? AND timestamp >= datetime('now', '-30 days')",
            (asset_id,)
        ).fetchone()
        result["inventory_snapshots"] = row["cnt"] if row else 0

        # Latest net position
        row = conn.execute(
            "SELECT net_position FROM inventory "
            "WHERE cat_asset_id = ? ORDER BY timestamp DESC LIMIT 1",
            (asset_id,)
        ).fetchone()
        if row:
            result["latest_net_position"] = _safe_float(row["net_position"])

        # Pool snapshots (our own history)
        snapshots = get_pool_snapshots(asset_id, hours=720)
        result["pool_snapshots"] = len(snapshots)
        if len(snapshots) >= 10:
            # Pool trend: compare recent vs older reserves
            mid = len(snapshots) // 2
            recent_avg = sum(s["xch_reserve"] for s in snapshots[:mid]) / mid
            older_avg = sum(s["xch_reserve"] for s in snapshots[mid:]) / (len(snapshots) - mid)
            if older_avg > 0:
                ratio = recent_avg / older_avg
                if ratio > 1.1:
                    result["pool_trend"] = "growing"
                elif ratio < 0.9:
                    result["pool_trend"] = "shrinking"
                else:
                    result["pool_trend"] = "stable"

        # F78 (2026-04-17): bot's own-fill volatility.
        # Use log-returns of consecutive fill prices — standard vol metric
        # that handles price scale invariantly. Annualised conversion is
        # skipped; we emit the raw sigma so downstream can pick a window.
        try:
            rows = conn.execute(
                "SELECT CAST(price_xch AS REAL) AS price, filled_at "
                "FROM fills WHERE cat_asset_id = ? "
                "AND filled_at >= datetime('now', '-30 days') "
                "AND price_xch IS NOT NULL "
                "ORDER BY filled_at ASC",
                (asset_id,)
            ).fetchall()
            prices = [r["price"] for r in rows if r["price"] and r["price"] > 0]
            n = len(prices)
            if n >= 3:
                # Log-return std-dev (expressed as percentage)
                import math as _math
                returns = [
                    _math.log(prices[i] / prices[i - 1])
                    for i in range(1, n)
                    if prices[i - 1] > 0 and prices[i] > 0
                ]
                if returns:
                    mean_ret = sum(returns) / len(returns)
                    variance = sum(
                        (r - mean_ret) ** 2 for r in returns
                    ) / max(1, len(returns) - 1)
                    sigma = variance ** 0.5
                    result["own_fill_stddev_pct"] = round(sigma * 100, 4)
                # Range-based measure (robust to small samples)
                p_min = min(prices)
                p_max = max(prices)
                p_mean = sum(prices) / n
                if p_mean > 0:
                    result["own_fill_range_pct"] = round(
                        (p_max - p_min) / p_mean * 100, 2
                    )
                result["own_fill_samples"] = n
        except Exception as _own_vol_err:
            # Non-fatal — leave the defaults (0.0) in place
            print(f"[MARKET_DATA] Own-fill vol calc failed: {_own_vol_err}")

    except Exception as e:
        print(f"[MARKET_DATA] Internal DB query failed: {e}")

    return result


# ===========================================================================
# PHASE 2: ANALYSIS ENGINE
# ===========================================================================

def analyze_market_data(raw: Dict, asset_id: str) -> Dict:
    """Analyze collected market data and produce actionable insights.

    Takes the raw data from collect_all_market_data() and calculates:
      - Volatility analysis (regime, 30-day range, max drawdown)
      - Liquidity analysis (volume, fill rate, depth)
      - Token health (holder count, activity, supply context)
      - Competition analysis (orderbook state)
      - Bot performance (if internal DB has history)

    Each section includes a confidence level (high/medium/low) based on
    how much data was available.

    Args:
        raw: Output from collect_all_market_data()
        asset_id: CAT asset ID

    Returns dict with analysis sections + overall data_quality score.
    """
    # Check cache first
    cached = get_market_analysis_cache(asset_id, "full_analysis")
    if cached:
        return cached

    analysis = {
        "volatility": _analyze_volatility(raw),
        "liquidity": _analyze_liquidity(raw),
        "token_health": _analyze_token_health(raw),
        "bot_performance": _analyze_bot_performance(raw),
        "data_quality": _assess_data_quality(raw),
    }

    # Cache the full analysis
    set_market_analysis_cache(asset_id, "full_analysis", analysis, CACHE_TTL_ANALYSIS)

    return analysis


def _analyze_volatility(raw: Dict) -> Dict:
    """Calculate volatility metrics from ticker + trade data.

    Uses 30-day high/low from Dexie ticker (confirmed available) plus
    trade price history for more granular volatility.

    Returns:
        regime: 'quiet', 'normal', 'volatile', 'extreme'
        range_30d_pct: 30-day price range as % of mid
        max_single_move_pct: largest single-day price change
        std_dev_pct: standard deviation of daily returns
        confidence: 'high', 'medium', 'low'
    """
    result = {
        "regime": "normal",
        "range_30d_pct": 0,
        "range_90d_pct": 0,
        "range_1y_pct": 0,
        "max_single_move_pct": 0,
        "std_dev_pct": 0,
        "confidence": "low",
        "explanation": "No volatility data available",
        "quiet_phase": False,  # True if 30d is calm but 90d/1y shows historical volatility
    }

    ticker = raw.get("dexie_ticker") or {}
    trades = raw.get("dexie_trades") or {}

    # --- 30-day range from ticker ---
    high_30d = ticker.get("high_30d", 0)
    low_30d = ticker.get("low_30d", 0)
    current_price = ticker.get("price", 0)

    if high_30d > 0 and low_30d > 0 and current_price > 0:
        mid_30d = (high_30d + low_30d) / 2
        range_pct = (high_30d - low_30d) / mid_30d * 100
        result["range_30d_pct"] = round(range_pct, 2)
        result["confidence"] = "medium"
        result["explanation"] = f"30-day range: {range_pct:.1f}% (high {high_30d:.8f}, low {low_30d:.8f})"

    # --- 90-day and 1-year context ---
    # These give a longer-term view of how volatile this token really is.
    # A token in a calm 30d patch can still be historically very volatile.
    high_90d = ticker.get("high_90d", 0)
    low_90d = ticker.get("low_90d", 0)
    high_1y = ticker.get("high_1y", 0)
    low_1y = ticker.get("low_1y", 0)

    if high_90d > 0 and low_90d > 0 and current_price > 0:
        mid_90d = (high_90d + low_90d) / 2
        range_90d_pct = (high_90d - low_90d) / mid_90d * 100
        result["range_90d_pct"] = round(range_90d_pct, 2)

    if high_1y > 0 and low_1y > 0 and current_price > 0:
        mid_1y = (high_1y + low_1y) / 2
        range_1y_pct = (high_1y - low_1y) / mid_1y * 100
        result["range_1y_pct"] = round(range_1y_pct, 2)

    # Detect "quiet phase of a volatile token":
    # If the 30d range is much smaller than the 90d range the token is in a
    # temporary lull — this is actually MORE dangerous, not safer, because a
    # return to normal volatility would be a sudden large move.
    if result["range_30d_pct"] > 0 and result["range_90d_pct"] > 0:
        if result["range_90d_pct"] > result["range_30d_pct"] * 2.5:
            result["quiet_phase"] = True

    # --- Daily returns from trade history ---
    trade_list = trades.get("trades", [])
    if len(trade_list) >= 10:
        # Group trades by day and calculate daily average prices
        daily_prices = {}
        for t in trade_list:
            if t.get("price", 0) > 0:
                day = t["date"][:10]  # YYYY-MM-DD
                if day not in daily_prices:
                    daily_prices[day] = []
                daily_prices[day].append(t["price"])

        # Calculate daily averages
        sorted_days = sorted(daily_prices.keys())
        daily_avgs = [sum(daily_prices[d]) / len(daily_prices[d]) for d in sorted_days]

        if len(daily_avgs) >= 3:
            # Daily returns
            returns = []
            for i in range(1, len(daily_avgs)):
                if daily_avgs[i - 1] > 0:
                    ret = (daily_avgs[i] - daily_avgs[i - 1]) / daily_avgs[i - 1]
                    returns.append(ret)

            if returns:
                # Standard deviation of returns
                mean_ret = sum(returns) / len(returns)
                variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
                std_dev = math.sqrt(variance)
                result["std_dev_pct"] = round(std_dev * 100, 2)

                # Max single-day move
                max_move = max(abs(r) for r in returns)
                result["max_single_move_pct"] = round(max_move * 100, 2)

                result["confidence"] = "high"
                result["explanation"] = (
                    f"30-day vol: std_dev={std_dev*100:.1f}%, "
                    f"range={result['range_30d_pct']:.1f}%, "
                    f"max_move={max_move*100:.1f}% "
                    f"(from {len(daily_avgs)} days of trades)"
                )

    # --- F78 (2026-04-17): own-fill vol cross-check ---
    # Our own fills are ground truth (no Dexie aggregation noise). If we
    # have enough of them, they either CORROBORATE the Dexie-derived vol
    # (good) or DIVERGE (means Dexie trade data is stale/thin). When
    # divergent, prefer own-fill vol as the primary signal.
    own_vol = float((raw.get("internal_db") or {}).get("own_fill_stddev_pct", 0) or 0)
    own_samples = int((raw.get("internal_db") or {}).get("own_fill_samples", 0) or 0)
    if own_samples >= 10 and own_vol > 0:
        result["own_fill_stddev_pct"] = own_vol
        result["own_fill_samples"] = own_samples
        current_std = result["std_dev_pct"]
        # Divergence > 40% in either direction → trust our own fills
        if current_std > 0:
            ratio = own_vol / current_std
            if ratio > 1.4 or ratio < 0.71:
                result["std_dev_pct"] = own_vol
                result["confidence"] = "high"
                result["explanation"] = (
                    f"Own-fill vol {own_vol:.1f}% used as primary "
                    f"(diverges from Dexie {current_std:.1f}%, n={own_samples})"
                )
        else:
            # No Dexie signal — own fills are our only source
            result["std_dev_pct"] = own_vol
            result["confidence"] = "medium"
            result["explanation"] = (
                f"Own-fill vol {own_vol:.1f}% (Dexie trades unavailable, n={own_samples})"
            )

    # --- Determine regime ---
    # Use the best available metric
    vol_metric = result["std_dev_pct"] or (result["range_30d_pct"] / 4)  # Range/4 ≈ rough std dev

    if vol_metric > 15:
        result["regime"] = "extreme"
    elif vol_metric > 8:
        result["regime"] = "volatile"
    elif vol_metric > 3:
        result["regime"] = "normal"
    else:
        result["regime"] = "quiet"

    # --- Quiet-phase bump ---
    # If the token is in a lull (30d calm, but 90d shows it's normally volatile),
    # treat it as one level more risky.  A return to historical norms would be a
    # sudden large move and we should not be lulled by the recent quiet period.
    if result["quiet_phase"] and result["regime"] in ("quiet", "normal"):
        _regime_bump = {"quiet": "normal", "normal": "volatile"}
        result["regime"] = _regime_bump[result["regime"]]
        result["explanation"] += (
            f" [quiet phase — 90d range {result['range_90d_pct']:.1f}% >> "
            f"30d {result['range_30d_pct']:.1f}%, regime bumped up]"
        )

    return result


def _analyze_liquidity(raw: Dict) -> Dict:
    """Assess market liquidity from volume, depth, and fill rate.

    Returns:
        level: 'high', 'moderate', 'low', 'very_low'
        daily_volume_xch: average daily volume
        fills_per_day: average fills per day
        avg_trade_size_xch: median trade size
        pool_depth_xch: TibetSwap pool XCH reserve
        pool_share_pct: our typical trade as % of pool
        volume_trend: 'growing', 'stable', 'declining'
        confidence: 'high', 'medium', 'low'
    """
    result = {
        "level": "low",
        "daily_volume_xch": 0,
        "fills_per_day": 0,
        "avg_trade_size_xch": 0,
        "pool_depth_xch": 0,
        "pool_share_pct": 0,
        "volume_trend": "unknown",
        "confidence": "low",
        "explanation": "No liquidity data available",
    }

    trades = raw.get("dexie_trades") or {}
    tibet = raw.get("tibet_pool") or {}

    # From Dexie trade history
    if trades:
        result["daily_volume_xch"] = trades.get("daily_volume_xch", 0)
        result["fills_per_day"] = trades.get("fills_per_day", 0)
        result["avg_trade_size_xch"] = trades.get("avg_trade_size_xch", 0)
        result["volume_trend"] = trades.get("volume_trend", "unknown")
        result["confidence"] = "high" if trades.get("total_count", 0) > 50 else "medium"

    # From TibetSwap pool
    if tibet and tibet.get("has_data"):
        result["pool_depth_xch"] = tibet.get("xch_reserve", 0)
        # Calculate our trade's share of the pool
        trade_size = float(cfg.DEFAULT_TRADE_XCH)
        if result["pool_depth_xch"] > 0 and trade_size > 0:
            result["pool_share_pct"] = round((trade_size / result["pool_depth_xch"]) * 100, 2)

    # --- Determine liquidity level ---
    daily_vol = result["daily_volume_xch"]
    fills = result["fills_per_day"]

    if daily_vol > 10 and fills > 10:
        result["level"] = "high"
    elif daily_vol > 1 and fills > 3:
        result["level"] = "moderate"
    elif daily_vol > 0.1 or fills > 0.5:
        result["level"] = "low"
    else:
        result["level"] = "very_low"

    result["explanation"] = (
        f"Volume: {daily_vol:.2f} XCH/day, "
        f"{fills:.1f} fills/day, "
        f"avg size: {result['avg_trade_size_xch']:.4f} XCH, "
        f"pool: {result['pool_depth_xch']:.1f} XCH"
    )

    return result


def _analyze_token_health(raw: Dict) -> Dict:
    """Assess token health from Spacescan data.

    Returns:
        risk_level: 'healthy', 'moderate', 'thin', 'risky'
        holder_count: number of holders
        circulating_supply: tokens in circulation
        activity_level: 'active', 'moderate', 'quiet', 'dormant'
        confidence: 'high', 'medium', 'low'
    """
    result = {
        "risk_level": "moderate",
        "holder_count": 0,
        "circulating_supply": 0,
        "activity_level": "unknown",
        "confidence": "low",
        "explanation": "No Spacescan data available — using cautious defaults",
    }

    spacescan = raw.get("spacescan") or {}
    if not spacescan.get("has_data"):
        return result

    holders = spacescan.get("holder_count", 0)
    activities = spacescan.get("activity_count", 0)
    circ = spacescan.get("circulating_supply", 0)

    result["holder_count"] = holders
    result["circulating_supply"] = circ
    result["confidence"] = "medium"

    # Risk level based on holder count
    if holders > 500:
        result["risk_level"] = "healthy"
    elif holders > 100:
        result["risk_level"] = "moderate"
    elif holders > 20:
        result["risk_level"] = "thin"
    else:
        result["risk_level"] = "risky"

    # Activity level
    if activities > 50:
        result["activity_level"] = "active"
    elif activities > 10:
        result["activity_level"] = "moderate"
    elif activities > 0:
        result["activity_level"] = "quiet"
    else:
        result["activity_level"] = "dormant"

    result["explanation"] = (
        f"Holders: {holders}, "
        f"activity: {result['activity_level']}, "
        f"supply: {circ:,.0f}, "
        f"risk: {result['risk_level']}"
    )

    return result


def _analyze_bot_performance(raw: Dict) -> Dict:
    """Analyze the bot's own trading performance from internal DB.

    Only useful after the bot has been running. Returns empty/zero
    metrics for first-time setup (expected).
    """
    result = {
        "has_history": False,
        "fill_rate_per_day": 0,
        "avg_fill_size_xch": 0,
        "inventory_drift": "neutral",
        "confidence": "low",
        "explanation": "No bot history yet — will improve after first trading session",
    }

    db = raw.get("internal_db") or {}
    if not db or db.get("fill_count", 0) == 0:
        return result

    result["has_history"] = True
    result["fill_rate_per_day"] = round(db["fill_count"] / 30, 2)  # Approximate
    result["avg_fill_size_xch"] = db.get("avg_fill_size_xch", 0)
    result["confidence"] = "high" if db["fill_count"] > 20 else "medium"

    # Inventory drift
    net_pos = db.get("latest_net_position", 0)
    if net_pos > 0:
        result["inventory_drift"] = "long_cat"
    elif net_pos < 0:
        result["inventory_drift"] = "short_cat"
    else:
        result["inventory_drift"] = "neutral"

    result["explanation"] = (
        f"Bot fills: {db['fill_count']} ({db['buy_fills']}B/{db['sell_fills']}S), "
        f"avg size: {result['avg_fill_size_xch']:.4f} XCH, "
        f"drift: {result['inventory_drift']}"
    )

    return result


def _assess_data_quality(raw: Dict) -> Dict:
    """Score the overall data quality for Smart Defaults decisions.

    Returns a percentage score and per-source breakdown.
    Weights reflect actual impact on settings quality:
    - Dexie ticker (30d data) is the MOST important — gives price, volume, range
    - TibetSwap pool gives real-time depth and slippage
    - Dexie individual trades give fill rate detail (nice to have)
    - Spacescan gives holder/health context
    - Internal DB only matters after bot has run
    """
    sources = {
        "dexie_ticker": {"weight": 30, "available": False, "confidence": "low"},
        "tibet_pool": {"weight": 25, "available": False, "confidence": "low"},
        "dexie_trades": {"weight": 20, "available": False, "confidence": "low"},
        "spacescan": {"weight": 15, "available": False, "confidence": "low"},
        "internal_db": {"weight": 10, "available": False, "confidence": "low"},
    }

    ticker = raw.get("dexie_ticker") or {}
    if ticker.get("has_data"):
        sources["dexie_ticker"]["available"] = True
        # Extra credit if ticker has 30d range data (much richer than just price)
        has_30d = ticker.get("high_30d", 0) > 0 and ticker.get("low_30d", 0) > 0
        has_volume = ticker.get("volume_30d", 0) > 0
        if has_30d and has_volume:
            sources["dexie_ticker"]["confidence"] = "high"
        elif has_30d or has_volume:
            sources["dexie_ticker"]["confidence"] = "high"
        else:
            sources["dexie_ticker"]["confidence"] = "medium"

    if raw.get("dexie_trades"):
        sources["dexie_trades"]["available"] = True
        count = raw["dexie_trades"].get("total_count", 0)
        sources["dexie_trades"]["confidence"] = "high" if count > 50 else "medium" if count > 10 else "low"

    if raw.get("tibet_pool") and raw["tibet_pool"].get("has_data"):
        sources["tibet_pool"]["available"] = True
        sources["tibet_pool"]["confidence"] = "high"

    if raw.get("spacescan") and raw["spacescan"].get("has_data"):
        sources["spacescan"]["available"] = True
        holders = raw["spacescan"].get("holder_count", 0)
        sources["spacescan"]["confidence"] = "high" if holders > 0 else "medium"

    # F77 (2026-04-17): record partial-fetch failures separately so the
    # label can warn the user even when the weighted score stays high.
    # Spacescan activity 404/429 and Dexie orderbook API errors would
    # otherwise be silently masked by a "100% excellent" score.
    partial_failures: List[str] = []
    spacescan_raw = raw.get("spacescan") or {}
    if spacescan_raw.get("activity_fetch_failed"):
        partial_failures.append("spacescan_activity")
    orderbook_raw = raw.get("dexie_orderbook") or {}
    if orderbook_raw and not orderbook_raw.get("api_ok", True):
        partial_failures.append("dexie_orderbook")

    db = raw.get("internal_db") or {}
    if db.get("fill_count", 0) > 0 or db.get("price_count", 0) > 0:
        sources["internal_db"]["available"] = True
        sources["internal_db"]["confidence"] = "high" if db.get("fill_count", 0) > 20 else "medium"

    # Calculate weighted score
    total_weight = sum(s["weight"] for s in sources.values())
    available_weight = sum(s["weight"] for s in sources.values() if s["available"])
    score = round((available_weight / total_weight) * 100) if total_weight > 0 else 0

    # Overall quality label — with F77 "limited" caveat when something
    # partially failed even if the weighted score remains high.
    if score >= 80:
        quality = "excellent"
    elif score >= 60:
        quality = "good"
    elif score >= 40:
        quality = "fair"
    else:
        quality = "limited"
    if partial_failures:
        # Append a caveat that won't break existing equality checks
        # ("excellent" → "excellent (partial data)"). UI can display
        # this verbatim.
        quality = f"{quality} (partial: {', '.join(partial_failures)})"

    # F77: use startswith() so the "(partial: ...)" caveat added above
    # doesn't knock the recommendation into "Limited data" when the core
    # score is still high.
    _base_label = quality.split(" ", 1)[0]  # "excellent (partial...)" → "excellent"
    return {
        "score": score,
        "quality": quality,
        "sources": sources,
        "partial_failures": partial_failures,
        "recommendation": (
            "All key data available — high confidence in calculated settings"
            if _base_label == "excellent"
            else "Good data coverage — settings should be reliable"
            if _base_label == "good"
            else "Some data missing — settings will use conservative fallbacks where needed"
            if _base_label == "fair"
            else "Limited data — settings will be conservative. Run the bot to build history."
        ),
    }


# ===========================================================================
# Utility
# ===========================================================================

def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert any value to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

