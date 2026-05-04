"""Smart-Defaults recommendation engine route.

Single heavy route that analyses live market + wallet state and
returns recommended configuration values. Helpers for standalone
price/orderbook fetches live here since smart-defaults is their
only non-blueprint caller (the /api/price and /api/price/tibet
routes in blueprints/market.py call them via api_server.*).
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event

try:
    from api_call_tracker import record as _record_api_call
except Exception:
    def _record_api_call(*args, **kwargs):
        return None


bp = Blueprint("smart_defaults", __name__)


def _smart_tibet_shock_trigger_pct(min_edge_bps) -> float:
    """Derive the explicit TibetSwap shock cancel trigger for Smart Settings.

    Runtime auto mode uses half of MIN_EDGE_BPS with a 0.50% floor. Smart
    Settings writes the concrete value it just derived so the setup form shows
    the actual defensive-cancel threshold instead of leaving a silent 0/auto.
    """
    try:
        edge_bps = Decimal(str(min_edge_bps or "0"))
    except (InvalidOperation, ValueError, TypeError):
        edge_bps = Decimal("0")
    trigger = max(Decimal("0.50"), edge_bps / Decimal("200"))
    return float(trigger.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _positive_int_or_none(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int_or_none(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _smart_float(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    try:
        import math
        if not math.isfinite(parsed):
            return default
    except Exception:
        pass
    return parsed


def _prep_count_from_budget(pool_target_xch: float, coin_size_xch: float, cap: int, minimum: int) -> int:
    """Return a prep count that fits the budget without forcing big-wallet floors."""
    pool_target_xch = max(0.0, _smart_float(pool_target_xch))
    coin_size_xch = max(0.0001, _smart_float(coin_size_xch, 0.0001))
    cap = max(0, int(cap or 0))
    minimum = max(0, int(minimum or 0))
    if pool_target_xch <= 0 or cap <= 0:
        return 0
    supported = int(pool_target_xch / coin_size_xch)
    if supported <= 0:
        return 0
    count = min(cap, supported)
    return count if count < minimum else max(minimum, count)


def _smart_fee_prep_count(avail_xch: float, fee_coin_size_xch: float, fee_pct: float = 0.03) -> int:
    """Scale fee prep coins down for small wallets.

    The old hard 50-coin cap was fine for large wallets but noisy on a
    single-digit XCH test wallet. This keeps the same large-wallet behavior
    while avoiding dozens of tiny fee coins when capital is intentionally low.
    """
    avail_xch = max(0.0, _smart_float(avail_xch))
    if avail_xch <= 0:
        return 0
    if avail_xch < 3:
        cap, minimum = 10, 2
    elif avail_xch < 10:
        cap, minimum = 20, 2
    elif avail_xch < 25:
        cap, minimum = 30, 5
    else:
        cap, minimum = 50, 5
    return _prep_count_from_budget(avail_xch * max(0.0, _smart_float(fee_pct)), fee_coin_size_xch, cap, minimum)


def _smart_sniper_pool_pct(avail_xch: float, fills_per_day: float) -> float:
    fills_per_day = _smart_float(fills_per_day)
    if fills_per_day > 10:
        base = 0.07
    elif fills_per_day > 3:
        base = 0.06
    else:
        base = 0.04

    avail_xch = max(0.0, _smart_float(avail_xch))
    if avail_xch < 3:
        return min(base, 0.02)
    if avail_xch < 10:
        return min(base, 0.03)
    if avail_xch < 25:
        return min(base, 0.04)
    return base


def _smart_sniper_prep_plan(avail_xch: float, fills_per_day: float, sniper_size_xch: float = 0.01) -> dict:
    """Return wallet-scaled sniper pool sizing."""
    avail_xch = max(0.0, _smart_float(avail_xch))
    sniper_size_xch = max(0.0001, _smart_float(sniper_size_xch, 0.01))
    pct = _smart_sniper_pool_pct(avail_xch, fills_per_day)
    target_xch = avail_xch * pct

    fills_per_day = _smart_float(fills_per_day)
    if fills_per_day > 10:
        activity_cap = 30
    elif fills_per_day > 3:
        activity_cap = 25
    else:
        activity_cap = 20

    if avail_xch < 3:
        cap, minimum = min(activity_cap, 6), 2
    elif avail_xch < 10:
        cap, minimum = min(activity_cap, 12), 2
    elif avail_xch < 25:
        cap, minimum = min(activity_cap, 18), 5
    else:
        cap, minimum = activity_cap, 5

    count = _prep_count_from_budget(target_xch, sniper_size_xch, cap, minimum)
    return {
        "pct": pct,
        "target_xch": target_xch,
        "count": count,
        "pool_xch": round(sniper_size_xch * count, 4),
    }


def _smart_position_floor(xch_total: float, trade_size_xch: float) -> float:
    """Small-wallet-aware minimum for MAX_POSITION_XCH."""
    xch_total = max(0.0, _smart_float(xch_total))
    trade_size_xch = max(0.0, _smart_float(trade_size_xch))
    if trade_size_xch > 0:
        wallet_cap = xch_total * 0.50 if xch_total > 0 else 5.0
        return round(max(0.1, min(5.0, trade_size_xch * 5, wallet_cap)), 1)

    if xch_total <= 0:
        return 5.0
    return round(max(0.1, min(5.0, xch_total * 0.10)), 1)


def _smart_initial_max_position(
    xch_total: float,
    trade_size_xch: float,
    risk_level: str,
    position_mult: float = 1.0,
) -> float:
    """Derive MAX_POSITION_XCH before the final ladder-consistency clamp."""
    xch_total = max(0.0, _smart_float(xch_total))
    if xch_total <= 0:
        return 5.0

    risk = str(risk_level or "moderate").lower().strip()
    if risk == "healthy":
        max_position = round(xch_total * 0.40, 1)
    elif risk == "moderate":
        max_position = round(xch_total * 0.30, 1)
    elif risk == "thin":
        max_position = round(xch_total * 0.20, 1)
    else:
        max_position = round(xch_total * 0.15, 1)

    floor = _smart_position_floor(xch_total, trade_size_xch)
    max_position = max(max_position, floor)
    if _smart_float(position_mult, 1.0) != 1.0:
        max_position = max(floor, round(max_position * _smart_float(position_mult, 1.0), 1))
    return max_position


def _smart_trade_vwap(trades: dict) -> float:
    trade_list = (trades.get("trades") or []) if isinstance(trades, dict) else []
    sum_pv = sum(
        t.get("price", 0) * t.get("xch_amount", 0)
        for t in trade_list
        if t.get("price", 0) > 0 and t.get("xch_amount", 0) > 0
    )
    sum_v = sum(
        t.get("xch_amount", 0)
        for t in trade_list
        if t.get("price", 0) > 0 and t.get("xch_amount", 0) > 0
    )
    return (sum_pv / sum_v) if sum_v > 0 else 0


def _resolve_smart_mid_price(
    ticker: dict,
    tibet: dict,
    spacescan: dict,
    trades: dict,
    orderbook: dict,
    messages: list | None = None,
) -> dict:
    """Resolve the best CAT-specific mid price Smart Settings can use.

    Prefer executable live markets. Fall back to Dexie orderbook and then
    trade VWAP so CATs without a ticker row or Tibet pool still get a
    conservative, token-specific plan when enough asset-id data exists.
    """
    messages = messages if messages is not None else []
    dexie_price = ticker.get("price", 0) if isinstance(ticker, dict) else 0
    tibet_price = tibet.get("price", 0) if isinstance(tibet, dict) and tibet.get("has_data") else 0
    spacescan_price = (
        spacescan.get("price_xch", 0)
        if isinstance(spacescan, dict) and spacescan.get("has_data")
        else 0
    )
    mid_price = 0
    arb_gap_bps = 0
    spacescan_gap_bps = 0
    price_source = ""

    has_both_prices = dexie_price > 0 and tibet_price > 0
    if has_both_prices:
        mid_price = (dexie_price + tibet_price) / 2
        arb_gap_bps = abs(dexie_price - tibet_price) / mid_price * 10000
        price_source = "dexie_tibet"
        messages.append(f"Price: {mid_price:.8f} (Dexie + Tibet)")
        if arb_gap_bps > 50:
            messages.append(f"Arb gap: {api_server._bps_to_pct(arb_gap_bps)}")
    elif dexie_price > 0:
        mid_price = dexie_price
        price_source = "dexie_ticker"
        messages.append(f"Price: {mid_price:.8f} (Dexie only)")
    elif tibet_price > 0:
        mid_price = tibet_price
        price_source = "tibet_pool"
        messages.append(f"Price: {mid_price:.8f} (Tibet only)")
    else:
        best_bid = orderbook.get("best_bid", 0) if isinstance(orderbook, dict) else 0
        best_ask = orderbook.get("best_ask", 0) if isinstance(orderbook, dict) else 0
        if best_bid > 0 and best_ask > 0:
            mid_price = (best_bid + best_ask) / 2
            price_source = "dexie_orderbook"
            messages.append(f"Price: {mid_price:.8f} (Dexie orderbook)")
        elif best_ask > 0:
            mid_price = best_ask
            price_source = "dexie_orderbook_ask"
            messages.append(f"Price: {mid_price:.8f} (Dexie orderbook ask)")
        elif best_bid > 0:
            mid_price = best_bid
            price_source = "dexie_orderbook_bid"
            messages.append(f"Price: {mid_price:.8f} (Dexie orderbook bid)")
        else:
            vwap_price = _smart_trade_vwap(trades)
            if vwap_price > 0:
                mid_price = vwap_price
                price_source = "dexie_trade_vwap"
                messages.append(f"Price: {mid_price:.8f} (Dexie trade VWAP)")

    if price_source.startswith("dexie_") and dexie_price <= 0 and mid_price > 0:
        dexie_price = mid_price

    if spacescan_price > 0 and mid_price > 0:
        spacescan_gap_bps = abs(spacescan_price - mid_price) / mid_price * 10000

    return {
        "dexie_price": dexie_price,
        "tibet_price": tibet_price,
        "mid_price": mid_price,
        "arb_gap_bps": arb_gap_bps,
        "spacescan_gap_bps": spacescan_gap_bps,
        "has_both_prices": has_both_prices,
        "price_source": price_source,
        "vwap_price": _smart_trade_vwap(trades),
    }


def _smart_dbx_defaults(asset_id: str) -> dict:
    """Pull DBX-related defaults from Dexie's live incentives feed.

    Returns the keys Smart Settings emits for the Market Intelligence
    section, populated from /v1/incentives when available, falling back
    to safe values when the API is unreachable. The tightest applicable
    spread cap is used so the bot stays inside both sides' qualifying
    range when only one side is set lower.
    """
    out: dict = {
        "dbx_max_spread_bps": 500,   # 5% — sane fallback if API is down
        "pair_incentivized": None,
        "dbx_buy_incentive": None,
        "dbx_sell_incentive": None,
    }
    if not asset_id:
        return out
    try:
        from dexie_incentives import fetch_incentives, get_pair_incentives
        bulk = fetch_incentives()
        if not bulk.get("success") and not bulk.get("incentives"):
            return out  # API unreachable — keep pair_incentivized=None
        pair = get_pair_incentives(asset_id)
        out["pair_incentivized"] = bool(pair.get("incentivized"))
        out["dbx_buy_incentive"] = pair.get("buy")
        out["dbx_sell_incentive"] = pair.get("sell")
        if pair.get("incentivized"):
            caps = [int(s.get("max_spread_bps") or 0)
                    for s in (pair.get("buy"), pair.get("sell"))
                    if s and (s.get("max_spread_bps") or 0) > 0]
            if caps:
                out["dbx_max_spread_bps"] = min(caps)
    except Exception:
        pass
    return out


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
        _record_api_call("tibetswap", "/pairs")
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
            print("[PRICE_STANDALONE] CAT not found on TibetSwap, trying Dexie...")
    except Exception as e:
        print(f"[PRICE_STANDALONE] TibetSwap failed ({e}), trying Dexie...")

    # --- Fallback to Dexie ---
    if not price:
        try:
            ticker_id = api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "") or ""
            # Dexie ticker format is "{CAT}_XCH" e.g. "SBX_XCH" (V1 confirmed)
            if ticker_id and "_" not in ticker_id:
                ticker_id = f"{ticker_id}_XCH"
            dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")

            # Method 1: Try ticker endpoint if we have a ticker_id
            if ticker_id:
                _record_api_call("dexie", "/v2/prices/tickers")
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
                _record_api_call("dexie", "/v1/offers")
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

    return jsonify(api_server._decimal_safe({
        "success": True,
        "mid": price,
        "tibet_price": price if source == "tibetswap" else None,
        "dexie_price": price if source and source.startswith("dexie") else None,
        "tibet_enabled": source == "tibetswap",
        "source": source,
        "liquidity": {},
    }))

def _fetch_dexie_orderbook_standalone(asset_id: str) -> dict:
    """Fetch Dexie orderbook and calculate competitor spread/depth.

    Standalone version (no bot/market_intel needed) for Smart Defaults.

    F77 (2026-04-17): returned dict now distinguishes three cases
    previously conflated:
      - ``api_ok=True, has_data=True``  — API worked, competitors present
      - ``api_ok=True, has_data=False`` — API worked, no competitors
      - ``api_ok=False, error=...``     — API call failed; caller can warn
    Previously both of the latter two returned identical zeroed results,
    so the smart-defaults algorithm couldn't tell "we're alone on the
    book" from "Dexie returned a 500".
    """
    import requests as _req
    result = {
        "best_bid": 0, "best_ask": 0, "competitor_spread_bps": 0,
        "buy_depth_xch": 0, "sell_depth_xch": 0,
        "num_buy_offers": 0, "num_sell_offers": 0,
        "has_data": False,
        "api_ok": False,
        "error": "",
    }
    if not asset_id:
        result["error"] = "no asset_id"
        return result

    dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
    our_tag = getattr(cfg, "DEXIE_BOT_TAG", "")

    try:
        # Sell side: CAT offered for XCH (ascending = cheapest first = best ask)
        # NOTE: Dexie API uses "offered_asset_id" / "requested_asset_id" params
        _record_api_call("dexie", "/v1/offers")
        sell_resp = _req.get(f"{dexie_base}/v1/offers", params={
            "offered_asset_id": asset_id,
            "status": 0, "page_size": 20, "sort": "price_asc"
        }, timeout=8)
        sell_ok = sell_resp.status_code == 200
        sell_offers = sell_resp.json().get("offers", []) if sell_ok else []

        # Buy side: XCH offered for CAT (descending = highest first = best bid)
        _record_api_call("dexie", "/v1/offers")
        buy_resp = _req.get(f"{dexie_base}/v1/offers", params={
            "requested_asset_id": asset_id,
            "status": 0, "page_size": 20, "sort": "price_desc"
        }, timeout=8)
        buy_ok = buy_resp.status_code == 200
        buy_offers = buy_resp.json().get("offers", []) if buy_ok else []

        # F77: if EITHER leg of the orderbook failed, mark the call as
        # not-OK — we don't have a reliable snapshot of the competitor
        # book. Caller is expected to check `api_ok` before consuming
        # `best_bid` / `best_ask` / competitor metrics.
        if not sell_ok or not buy_ok:
            result["error"] = (
                f"sell HTTP {sell_resp.status_code}, buy HTTP {buy_resp.status_code}"
            )
            return result
        result["api_ok"] = True

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

        # has_data means "API succeeded AND competitors were found".
        # api_ok alone distinguishes "no competitors" from "API broken".
        result["has_data"] = (
            result["api_ok"]
            and (result["num_buy_offers"] > 0 or result["num_sell_offers"] > 0)
        )
        _state_tag = (
            "ok" if result["api_ok"] and (result["num_buy_offers"] or result["num_sell_offers"])
            else ("empty-book" if result["api_ok"] else "api-failed")
        )
        print(f"[SMART_DEFAULTS] Orderbook [{_state_tag}]: "
              f"bid={result['best_bid']:.8f}, ask={result['best_ask']:.8f}, "
              f"spread={api_server._bps_to_pct(result['competitor_spread_bps'])}, "
              f"buys={result['num_buy_offers']}, sells={result['num_sell_offers']}"
              + (f" — {result['error']}" if result['error'] else ""))
        return result
    except Exception as e:
        result["error"] = f"exception: {e}"
        print(f"[SMART_DEFAULTS] Orderbook fetch failed: {e}")
        return result

@bp.route("/api/smart-defaults")
def api_smart_defaults():
    """Calculate ALL smart default settings from live market data.

    Gathers wallet balances, prices from both exchanges, pool depth,
    competitor orderbook, and volatility history — then calculates
    every setting from real data. Works even when bot is stopped.

    ``liquidity_mode`` (query arg, default "two_sided") scopes the plan
    to one side of the book. In single-sided modes the disabled side's
    size / count / spare fields are returned as None / 0 so the save
    layer writes a clean config without stale SELL_* or BUY_* residue.
    """
    bot = api_server.bot
    try:
        xch_res = request.args.get("xch_reserve", 0)
        cat_res = request.args.get("cat_reserve", 0)
        risk_profile = request.args.get("risk_profile", "balanced")
        liquidity_mode = (request.args.get("liquidity_mode", "two_sided") or "two_sided").strip().lower()
        if liquidity_mode not in ("two_sided", "buy_only", "sell_only"):
            liquidity_mode = "two_sided"
        dbx_cap = (request.args.get("dbx_cap", "false") or "").strip().lower() in ("1", "true", "yes")
        asset_id = (request.args.get("asset_id", "") or "").strip()
        cat_ticker_id = (request.args.get("cat_ticker_id", "") or "").strip()
        cat_name = (request.args.get("cat_name", "") or "").strip()
        cat_wallet_id = _positive_int_or_none(request.args.get("cat_wallet_id"))
        cat_decimals = _nonnegative_int_or_none(request.args.get("cat_decimals"))
        return api_server._calculate_smart_defaults(
            xch_reserve=xch_res,
            cat_reserve=cat_res,
            risk_profile=risk_profile,
            liquidity_mode=liquidity_mode,
            dbx_cap=dbx_cap,
            asset_id=asset_id or None,
            cat_wallet_id=cat_wallet_id,
            cat_decimals=cat_decimals,
            cat_ticker_id=cat_ticker_id or None,
            cat_name=cat_name or None,
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[SMART_DEFAULTS] ERROR: {e}\n{tb}")
        log_event("error", "smart_defaults", f"Smart Settings failed: {e}")
        return jsonify({"error": "Smart Settings calculation failed", "code": "SERVER_ERROR"}), 500

def _calculate_smart_defaults(
    xch_reserve=0.0,
    cat_reserve=0.0,
    risk_profile="balanced",
    liquidity_mode="two_sided",
    dbx_cap=False,
    asset_id=None,
    cat_wallet_id=None,
    cat_decimals=None,
    cat_ticker_id=None,
    cat_name=None,
):
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
    #
    # F77 (2026-04-17): capital/sizing multipliers no longer pinned at 1.0 for
    # all three profiles. Conservative now deploys LESS capital into the
    # trading ladder (keeping a larger cushion), and uses FEWER slots (so
    # each slot is thicker and less spread out). Balanced/Aggressive keep
    # current behaviour as the reference (full capital, full slot count).
    #
    # Profiles affect:
    #  - Capital deployed (conservative shrinks the trading-XCH budget)
    #  - Number of offers (conservative runs a shorter ladder)
    #  - Spread width (conservative earns more per fill)
    #  - Requote speed (conservative lets offers ride longer)
    #  - Position-cap sensitivity (conservative trips sooner)
    #  - Inventory rebalancing (conservative rebalances gentler)
    #  - Safety buffers (conservative keeps more spares)
    _RISK_PROFILES = {
        "conservative": {
            # ── Capital / sizing ──
            # F77: actually deploy less capital — "conservative" was cosmetic
            # before (same capital as balanced, only spread differed). 0.85
            # means 85% of the trading-XCH budget is committed; the other
            # 15% stays as extra buffer on top of the backend's normal
            # headroom. 0.80 offer count means fewer slots (7 vs 10 at
            # default), each slot thicker and less spread out.
            "capital_mult":       0.85,
            "max_offers_mult":    0.80,
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
            # Baseline — everything at 1.0 (full wallet into trading ladder)
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
            # ── Capital / sizing (same as balanced — already at max) ──
            # capital can't exceed 100% and adding slots beyond the balanced
            # budget just makes each slot thinner → worse fill economics.
            # "Aggressive" differentiates through tighter spreads, faster
            # requote, looser inventory limits, not more capital.
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

    active_cat = getattr(api_server, "_active_cat", {}) or {}
    asset_id = (str(asset_id).strip() if asset_id else "") or active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    decimals = cat_decimals if cat_decimals is not None else active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
    ticker_id = (str(cat_ticker_id).strip() if cat_ticker_id else "") or active_cat.get("ticker_id") or (cfg.CAT_TICKER_ID if hasattr(cfg, "CAT_TICKER_ID") else "")
    cat_wid = cat_wallet_id or active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", 2)
    cat_name = (str(cat_name).strip() if cat_name else "") or active_cat.get("name") or getattr(cfg, "CAT_NAME", "CAT")
    if ticker_id and "_" not in ticker_id:
        ticker_id = f"{ticker_id}_XCH"

    if not asset_id:
        return jsonify({"error": "No trading pair selected"})

    print("\n[SMART_DEFAULTS v2] === Gathering 30 days of market data ===")
    log_event("info", "smart_defaults",
              f"Smart Settings: gathering 30 days of market data for {cat_name}")
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
            _raw_total = api_server._safe_float(wb.get("unconfirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = api_server._safe_float(wb.get("confirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = api_server._safe_float(wb.get("spendable_balance", 0))
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
            _raw_total = api_server._safe_float(wb.get("unconfirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = api_server._safe_float(wb.get("confirmed_wallet_balance", 0))
            if _raw_total <= 0:
                _raw_total = api_server._safe_float(wb.get("spendable_balance", 0))
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

    # Fetch before price resolution: some CATs have no Dexie ticker or Tibet
    # pool yet, but do have live asset-id orderbook offers we can price from.
    orderbook = _fetch_dexie_orderbook_standalone(asset_id)
    if orderbook["has_data"]:
        messages.append(f"Competitors: {orderbook['num_buy_offers']}B/{orderbook['num_sell_offers']}S")

    # ---- 3. Prices (from collected data + orderbook/trade fallbacks) ----
    price_info = _resolve_smart_mid_price(ticker, tibet, spacescan, trades, orderbook, messages)
    dexie_price = price_info["dexie_price"]
    tibet_price = price_info["tibet_price"]
    mid_price = price_info["mid_price"]
    arb_gap_bps = price_info["arb_gap_bps"]
    spacescan_gap_bps = price_info["spacescan_gap_bps"]
    has_both_prices = price_info["has_both_prices"]
    price_source = price_info["price_source"]
    vwap_price = price_info["vwap_price"]
    if not mid_price:
        return jsonify({"error": "No price available from Dexie or TibetSwap"})

    # ---- 5. Read user inputs (trade size, max offers) ----
    from flask import request as flask_request
    trade_size = api_server._safe_float(flask_request.args.get("trade_size", 0))

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
        messages.append(f"Pool: {pool_xch:.1f} XCH, slippage: {api_server._bps_to_pct(real_slippage_bps)} for 0.01 XCH")
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
            f"Spacescan price differs from executable markets by {api_server._bps_to_pct(spacescan_gap_bps)} — staying conservative"
        )
    elif spacescan_gap_bps > 1000:
        sanity_adj = 25
        messages.append(
            f"Spacescan price is {api_server._bps_to_pct(spacescan_gap_bps)} away from live venues — sanity buffer added"
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
          f"{pool_trend_adj} pool_trend = {api_server._bps_to_pct(base_spread_bps)}"
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

    print(f"[SMART_DEFAULTS v2] Requote: {api_server._bps_to_pct(requote_bps)} "
          f"(slippage={api_server._bps_to_pct(real_slippage_bps)}, pool={pool_xch:.0f} XCH)")

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

    max_position = _smart_initial_max_position(
        xch_spendable if has_wallet else 0,
        trade_size,
        risk_level,
        _rp["position_mult"],
    )

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
    _band_note = " (using 90d range — quiet phase)" if (quiet_phase and range_90d_pct > range_30d_pct) else ""
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
    tibet_shock_cancel_trigger_pct = _smart_tibet_shock_trigger_pct(inner_edge_bps)
    messages.append(
        f"Tibet shock cancel: {tibet_shock_cancel_trigger_pct:.2f}% "
        f"(half of inner edge)"
    )

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

    # Safety floor: rails must straddle the price the bot actually trades
    # against, which is what the Save validator checks (bot_state.pricing.mid).
    # bot.py prefers TibetSwap's pool ratio and only falls back to Dexie when
    # Tibet is unavailable. Anchoring on the (Dexie + Tibet)/2 blend used
    # elsewhere here breaks for illiquid CATs: a stale Dexie last_price drags
    # the blend well below the live Tibet price, so max_mid lands beneath the
    # current market and the GUI refuses to save the result of Smart Settings.
    live_price = tibet_price if tibet_price > 0 else dexie_price
    if live_price > 0:
        min_max_mid = live_price * 1.15
        if max_mid < min_max_mid:
            messages.append(f"Price rail ceiling raised to 15% above live price "
                            f"({live_price:.8f} → {min_max_mid:.8f}) — market is above 30d high")
            max_mid = min_max_mid
        if min_mid > live_price * 0.85:
            min_mid = live_price * 0.85

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
    # F77 (2026-04-17): fold the orderbook API status into the quality
    # label. The orderbook is fetched in this function (not inside
    # market_data_collector), so _assess_data_quality doesn't see it —
    # we splice its status in here. If the API failed, add it to the
    # existing "(partial: ...)" caveat; otherwise leave the label alone.
    if not orderbook.get("api_ok", True):
        if "(partial:" in quality_label:
            # Merge with existing caveat
            quality_label = quality_label.replace(
                "(partial:", "(partial: dexie_orderbook,"
            )
        else:
            quality_label = f"{quality_label} (partial: dexie_orderbook)"
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
    # Fee coin size must comfortably exceed the fee so change can recycle.
    #
    # F82 (2026-04-20): target-seconds is now risk-profile driven (was
    # hardcoded 120s). Conservative waits longer (cheaper fees, tolerates
    # slower confirms), aggressive pays for speed (higher fees, rapid
    # refresh). Also fetch a one-tier-faster quote to catch congestion
    # spikes — ``tx_fees`` takes the max of the two so the fee coin size
    # covers a sudden mempool surge.
    if _risk_profile_name == "conservative":
        _fee_target_secs = 300
    elif _risk_profile_name == "aggressive":
        _fee_target_secs = 60
    else:
        _fee_target_secs = 120
    _fee_spike_secs = max(30, int(_fee_target_secs / 2))
    try:
        from tx_fees import get_suggested_transaction_fee
        # Typical CAT spend ~35M cost units.
        _fee_est = get_suggested_transaction_fee(target_seconds=_fee_target_secs, cost=35_000_000)
        _fee_est_60 = get_suggested_transaction_fee(target_seconds=_fee_spike_secs, cost=35_000_000)
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
                f"Fee (Coinset, {_fee_target_secs}s target / {_fee_spike_secs}s peak, "
                f"{_risk_profile_name}): {_peak_fee_xch:.8f} XCH "
                f"→ coin size {_fee_coin_size:.4f} XCH"
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
    _fee_prep_count   = _smart_fee_prep_count(_avail_xch, _fee_coin_size, _FEE_PCT)
    _fee_pool_xch     = _fee_coin_size * _fee_prep_count

    _SNIPER_PCT = _smart_sniper_pool_pct(_avail_xch, fills_per_day)
    _sniper_pool_raw   = _avail_xch * _SNIPER_PCT
    # Sniper offers are expendable probes — keep them at Dexie's minimum
    # displayable size (0.01 XCH) so they show up on the book without wasting
    # capital. The pool carries many cheap coins rather than fewer large ones.

    # Prep count: more fills = faster sniper coin burn = need more ready.
    # Cap scales with the sniper pool so we never prep more than the pool
    # can fund at the fixed minimum size.
    _sniper_plan = _smart_sniper_prep_plan(_avail_xch, fills_per_day, _smart_sniper_size)
    _smart_sniper_prep = int(_sniper_plan.get("count") or 0)
    _sniper_pool_xch   = float(_sniper_plan.get("pool_xch") or 0.0)

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
    # F82 (2026-04-20): Smart Settings always recommends reverse-buy ON (see
    # ``"buy_ladder_reversed": True`` in the response ~line 9611, set by F78).
    # Force the internal computation to match so the returned sizes are
    # position-indexed under reverse=True and consistent with the returned
    # flag. Previously this read cfg.BUY_LADDER_REVERSED, so when cfg was
    # already True the sizes were computed reversed but the frontend did an
    # extra swap on top, flipping them to the wrong orientation. Sell-only
    # mode has no buy ladder so the flag is forced False there at line ~9798.
    _buy_ladder_reversed = (liquidity_mode != "sell_only")
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

        # ── POST-SHOCK XCH BUDGET RECONCILIATION ───────────────────────────
        # The price-shock spare floor above may have raised spare counts AFTER
        # the capital-plan solver ran, so the solver's base_size was computed
        # with fewer spares than are now recommended. Re-check that
        # (active + new_spares) × base_size × headroom ≤ _trading_xch and
        # scale base_size down proportionally if not.
        _post_shock_spare_ovhd = (
            _spare_inner   * _size_mults[0] +
            _spare_mid     * _size_mults[1] +
            _spare_outer   * _size_mults[2] +
            _spare_extreme * _size_mults[3]
        )
        _post_shock_total_xch = (
            (_n_final * _TIER_CAPITAL_FACTOR + _post_shock_spare_ovhd)
            * _CP_HEADROOM_MULT * _base_size
        )
        if _post_shock_total_xch > _trading_xch > 0:
            _rescale = _trading_xch / _post_shock_total_xch
            _base_size     = max(_MIN_OFFER_XCH, round(_base_size * _rescale, 4))
            _smart_trade_size = _base_size
            _smart_inner   = max(_MIN_OFFER_XCH, round(_base_size * _size_mults[0], 4))
            _smart_mid     = max(_MIN_OFFER_XCH, round(_base_size * _size_mults[1], 4))
            _smart_outer   = (max(_MIN_OFFER_XCH, round(_base_size * _size_mults[2], 4))
                              if _max_tiers >= 3 else 0.0)
            _smart_extreme = (max(_MIN_OFFER_XCH, round(_base_size * _size_mults[3], 4))
                              if _max_tiers == 4 else 0.0)
            print(f"[SMART_DEFAULTS] Post-shock XCH rescale ×{_rescale:.4f}: "
                  f"base {_base_size:.4f} XCH, inner {_smart_inner:.4f} XCH")

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
            # tiers. Carve those holders OUT of the CAT budget here so the
            # trading-tier clamp leaves room for them.
            #
            # F77 (2026-04-17): removed the separate `* 0.85` safety factor.
            # It was double-counting the topup reservation (which is
            # already subtracted below as `_topup_cat_prep`), giving CAT
            # side only ~70% of balance for trading while XCH side uses
            # ~88%. The 15-25% topup-buffer is the explicit slack; no
            # additional hidden factor is needed. Result: CAT-side ladder
            # now deploys the same fraction of balance as the XCH side,
            # which matches user intent ("my CAT should be fully used").
            _sniper_cat_prep = (
                round((_smart_sniper_size / mid_price) * _cp_hm) * _smart_sniper_prep
                if mid_price > 0 else 0
            )
            _topup_cat_prep = round(_avail_cat * _TOPUP_BUFFER_PCT)
            # Match the XCH-side's implicit 2% safety margin (see
            # _safe_tier_budget = _tier_warning_budget * 0.98 below) so
            # both sides leave identical rounding-noise buffer.
            _cat_budget = max(
                0.0,
                _avail_cat * 0.98 - _sniper_cat_prep - _topup_cat_prep
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
                    f"Spread raised to {api_server._bps_to_pct(_capital_spread_floor)} "
                    f"(micro wallet — {_trading_xch:.4f} XCH trading capital, "
                    f"fees must be covered by spread)"
                )
        elif _trading_xch < 0.2:
            # Small: < 0.2 XCH trading capital — fees are a high % of each fill
            _capital_spread_floor = 600  # 6% minimum
            if base_spread_bps < _capital_spread_floor:
                base_spread_bps = _capital_spread_floor
                messages.append(
                    f"Spread raised to {api_server._bps_to_pct(_capital_spread_floor)} "
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

    # ═══ F66 FINAL BUY-SIDE XCH VERIFICATION ══════════════════════════════
    # Belt-and-suspenders check mirroring F65 (which does the same for CAT
    # sell side).  F62 computes _smart_buy_* from _orig_xch_budget, but
    # _launcher_buy_budget may be smaller because the 2× largest-tier topup
    # guard (line 8501) can increase _topup_buffer_xch AFTER _orig_xch_budget
    # was captured.  This guard recomputes the buy-side coin-prep total with
    # the F62 sizes and scales them down if they exceed the available budget.
    if _smart_buy_inner > 0 and _avail_xch > 0:
        _f66_hm = 1.0 + (coin_prep_headroom_pct / 100.0)
        # Build (count, size) pairs in SLOT-indexed order.
        # Under reverse-buy the slot-indexed buy counts are position counts;
        # the corresponding sizes are already position-semantic too
        # (_smart_buy_inner = smallest size, used by position inner = many offers).
        _f66_tiers = [
            (_buy_n_inner   + _buy_spare_inner,   _smart_buy_inner),
            (_buy_n_mid     + _buy_spare_mid,     _smart_buy_mid),
        ]
        if _max_tiers >= 3 and _smart_buy_outer > 0:
            _f66_tiers.append((_buy_n_outer + _buy_spare_outer, _smart_buy_outer))
        if _max_tiers == 4 and _smart_buy_extreme > 0:
            _f66_tiers.append((_buy_n_extreme + _buy_spare_extreme, _smart_buy_extreme))
        _f66_tier_xch = sum(_cnt * _sx * _f66_hm for _cnt, _sx in _f66_tiers)
        # Budget: avail minus fixed carve-outs (fee pool, sniper pool, topup buffer).
        # Use _topup_buffer_xch (the final bumped value) not _topup_buffer_reserve.
        _f66_budget = max(0.0,
            (_avail_xch - _fee_pool_xch - _sniper_pool_xch - _topup_buffer_xch) * 0.98)
        if _f66_tier_xch > _f66_budget > 0:
            _f66_scale = _f66_budget / _f66_tier_xch
            _f66_old_inner = _smart_buy_inner
            _smart_buy_inner   = max(_MIN_OFFER_XCH, round(_smart_buy_inner   * _f66_scale, 4))
            _smart_buy_mid     = max(_MIN_OFFER_XCH, round(_smart_buy_mid     * _f66_scale, 4))
            _smart_buy_outer   = (max(_MIN_OFFER_XCH, round(_smart_buy_outer  * _f66_scale, 4))
                                  if _smart_buy_outer > 0 else 0.0)
            _smart_buy_extreme = (max(_MIN_OFFER_XCH, round(_smart_buy_extreme * _f66_scale, 4))
                                  if _smart_buy_extreme > 0 else 0.0)
            # Keep shared sizes in sync (pre-F62 callers read these).
            _smart_inner   = _smart_buy_inner
            _smart_mid     = _smart_buy_mid
            _smart_outer   = _smart_buy_outer
            _smart_extreme = _smart_buy_extreme
            _smart_trade_size = _smart_buy_mid  # mid is the reference "base"
            messages.append(
                f"F66 buy-side XCH safety clamp: "
                f"inner {_f66_old_inner:.4f} → {_smart_buy_inner:.4f} "
                f"({_f66_scale*100:.1f}% scale, tier prep "
                f"{_f66_tier_xch:.2f} → {_f66_budget:.2f} XCH budget)"
            )
            print(
                f"[SMART_DEFAULTS] F66 XCH safety clamp: "
                f"inner {_f66_old_inner:.4f} → {_smart_buy_inner:.4f}, "
                f"tier_xch {_f66_tier_xch:.2f} → budget {_f66_budget:.2f}"
            )
    # ═══ END F66 FINAL BUY-SIDE XCH VERIFICATION ══════════════════════════

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

    # Diagnostic dump — printed on every smart-defaults call so any
    # future coin-prep overshoot can be traced from the server log.
    print(
        f"[SMART_DEFAULTS] Capital summary: "
        f"spendable={xch_spendable:.4f} avail={_avail_xch:.4f} "
        f"fee={_fee_pool_xch:.4f} sniper={_sniper_pool_xch:.4f} "
        f"topup={_topup_buffer_xch:.4f} trading={_trading_xch:.4f} | "
        f"fills/day={fills_per_day:.2f} regime={regime} n_final={_n_final} "
        f"size_mults={tuple(round(m,3) for m in _size_mults)} | "
        f"spares={_spare_inner}/{_spare_mid}/{_spare_outer}/{_spare_extreme} | "
        f"buy_sizes(F62)={_smart_buy_inner:.4f}/{_smart_buy_mid:.4f}/"
        f"{_smart_buy_outer:.4f}/{_smart_buy_extreme:.4f} | "
        f"sell_sizes={_smart_sell_inner:.4f}/{_smart_sell_mid:.4f}/"
        f"{_smart_sell_outer:.4f}/{_smart_sell_extreme:.4f} | "
        f"orig_xch_budget={_orig_xch_budget:.4f} "
        f"reversed={_buy_ladder_reversed}"
    )

    # ═══ Build response ═══
    # F78 (2026-04-18): *_bps fields now return integer basis points
    # matching the field name and the env units. Previously they returned
    # values divided by 100 (i.e. percent) which forced every consumer to
    # know about the inversion. The GUI's read path now does the /100 for
    # display only; the save path still × 100 (unchanged) — which together
    # round-trips correctly. Direct API callers can apply the response
    # straight to /api/config without conversion now.
    #
    # Sniper auto-enable: when Smart Settings has allocated a real sniper
    # pool in two-sided mode (pool budget was already carved before
    # trading_xch so totals fit), turn the feature on. See the matching
    # "Bot Operations" block below for the rationale.
    _sniper_auto = (
        liquidity_mode == "two_sided"
        and float(_smart_sniper_size or 0) > 0
        and int(_smart_sniper_prep or 0) > 0
    )

    # ═══ DBX cap clamp (opt-in via the pre-prompt) ════════════════════════
    # When the user picks "Maximize DBX" before Smart Settings runs, clamp
    # the spread + requote so every tier of the resulting ladder lands
    # inside Dexie's incentive cap and stays reward-eligible. The cap is
    # the tighter of the two sides' caps (already computed in
    # _smart_dbx_defaults). Only applies if the calculated spread is wider
    # than the cap — otherwise the result already qualifies and we leave
    # everything alone.
    _dbx_cap_meta = _smart_dbx_defaults(asset_id) if dbx_cap else None
    if dbx_cap and _dbx_cap_meta and _dbx_cap_meta.get("pair_incentivized"):
        _cap_bps = int(_dbx_cap_meta.get("dbx_max_spread_bps") or 0)
        if _cap_bps > 0:
            _orig_base = base_spread_bps
            if base_spread_bps > _cap_bps:
                base_spread_bps = _cap_bps
            if max_spread_bps > _cap_bps:
                max_spread_bps = _cap_bps
            if min_spread_bps > _cap_bps:
                min_spread_bps = _cap_bps
            # Requote scales with the (now tighter) spread so requotes
            # don't slingshot offers outside the cap. Floor at 25 bps so
            # we don't end up requoting on every micro-move.
            if _orig_base > _cap_bps and _orig_base > 0:
                _scale = Decimal(str(_cap_bps)) / Decimal(str(_orig_base))
                requote_bps = max(Decimal("25"), Decimal(str(requote_bps)) * _scale)
            messages.append(
                f"DBX cap applied: spread tightened to {_cap_bps/100:.1f}% "
                f"(from {_orig_base/100:.1f}%) so all tiers stay reward-eligible"
            )

    result = {
        # Smart Pricing
        "dynamic_spread_enabled": has_both_prices,
        "base_spread_bps": int(round(base_spread_bps)),
        "volatility_window_hours": volatility_window,
        "min_edge_bps": int(round(inner_edge_bps)),  # env key is MIN_EDGE_BPS
        "min_spread_bps": int(round(min_spread_bps)),
        "max_spread_bps": int(round(max_spread_bps)),
        "inventory_enabled": True,
        "skew_intensity": skew_intensity,
        "max_position_xch": max_position,
        "spread_bps": int(round(base_spread_bps)),
        "loop_seconds": loop_seconds,

        # Auto-Requote
        "auto_requote": True,
        "requote_bps": int(round(requote_bps)),
        # F82 (2026-04-20): requote cooldown derived from fill frequency
        # + volatility + risk profile (was hardcoded 60s).
        # Base anchors on fill rate — busy markets need faster refresh
        # (30s), quiet markets avoid churn (90s). Volatile regimes widen
        # 25% so offers ride through noise. spread_step_mult applies the
        # risk bias (conservative +20% cooldown = less churn, aggressive
        # -15% = chase price). Clamped 20–180s.
        "requote_cooldown": max(20, min(180, int(round(
            (30 if fills_per_day > 10 else
             45 if fills_per_day > 3 else
             60 if fills_per_day > 1 else
             90)
            * (1.25 if regime in ("extreme", "volatile") else 1.0)
            * _rp["spread_step_mult"]
        )))),
        "requote_batch_size": requote_batch_size,

        # Safety & Limits (reserves intentionally excluded — user's choice)
        "max_mid_move": round(max_mid_move, 1),
        "dynamic_limit_pct": dynamic_limit_pct,
        "max_step_change_pct": max_step_change_pct,
        "tibet_shock_cancel_trigger_pct": tibet_shock_cancel_trigger_pct,
        "arb_alert_threshold_bps": int(round(arb_alert_threshold_bps)),  # env key is ARB_ALERT_THRESHOLD_BPS
        "min_mid": min_mid,
        "max_mid": max_mid,

        # Market Intelligence — pulled from Dexie's live /v1/incentives feed
        # so the spread cap and pair_incentivized flag reflect what Dexie
        # actually publishes today, not the (broken) ticker-field check or a
        # hard-coded 5%.
        "competitor_aware_enabled": competitor_enabled,
        **_smart_dbx_defaults(asset_id),

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
        # F82 (2026-04-20): target confirmation window drives the runtime
        # fee estimator in ``tx_fees.py``. Risk-profile scaled:
        # conservative=300s (cheapest), balanced=120s, aggressive=60s
        # (fastest).
        "transaction_fee_target_secs": _fee_target_secs,

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
        # Reversed (True) is the recommended default. Under reverse-buy the
        # buy ladder is asymmetric to the sell ladder:
        #   * Sell side (unchanged):  inner = LARGE, extreme = small
        #     (big sells nearest mid where buyers cluster)
        #   * Buy side (reversed):    inner = small, extreme = LARGE
        #     (small commits near mid, big commits only trigger on a deep drop)
        # So toggle ON = reverse-on = "buy: small near price → large far",
        # matching the tooltip on the GUI checkbox.
        #
        # The concrete Smart Defaults code lives around line 8645:
        #   _smart_buy_inner   = base × _size_mults[3]  # smallest mult → smallest size
        #   _smart_buy_extreme = base × _size_mults[0]  # biggest mult  → biggest size
        #
        # F78 (fix history): this field was hardcoded to False here, which
        # overrode the recommendation every time Smart Settings ran. Now
        # returns True so the recommended layout actually gets applied.
        # User can still override via the GUI toggle after Smart Settings.
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
        # Smart Settings sizes a sniper pool (_smart_sniper_size /
        # _smart_sniper_prep are carved BEFORE _trading_xch, so the pool
        # always fits within the wallet). Previously this field was
        # `getattr(cfg, "SNIPER_ENABLED", True)` — a pass-through from the
        # current config. If the user had ever toggled sniper off, Smart
        # Settings kept emitting sniper_enabled=False even while still
        # allocating the pool sizing, so the GUI preview hid the row and
        # coin prep never built the tier. Auto-enable whenever there's a
        # real pool to prepare (two_sided mode, size>0, count>0); single-
        # sided modes leave it off (sniper needs both sides). Users can
        # still uncheck the box after Smart Settings runs if they want it
        # off — a subsequent Smart Settings click will re-enable it, but
        # that's the point of Smart Settings. `_sniper_auto` is computed
        # just above the `result = {…}` dict literal (see a few lines up).
        "sniper_enabled": bool(_sniper_auto),
        "sniper_size_xch": _smart_sniper_size,
        "sniper_prep_count": _smart_sniper_prep,
        # F82 (2026-04-20): derive re-arm thresholds from the same market
        # signals that drive the main requote step — stops Smart Settings
        # passing through a bad .env value (e.g. 0%, which makes sniper
        # re-arm on every price tick → review-settings warning & thrash).
        #
        # Price-move trigger: how far the mid must move before the previous
        # probe's pricing is stale. Anchor at 40% of base_spread — smaller
        # than the main requote (60%) because sniper probes are
        # expendable and benefit from faster refresh.
        #
        # Gap-move trigger: how far the dex/tibet arb gap must shift
        # before the edge is worth re-probing. Anchor at 50% of
        # arb_alert_threshold so re-arm fires at roughly half the
        # alert-worthy gap.
        #
        # Volatility widen + spread_step_mult are reused from the requote
        # logic so conservative/balanced/aggressive behave consistently
        # across all three step-size knobs. Clamped to 50–500 bps
        # (0.5%–5%): floor stops the 0% bug, ceiling keeps probes
        # responsive on thin-liquidity tokens.
        "sniper_rearm_price_move_bps": int(round(max(50, min(500,
            base_spread_bps * 0.40
            * (1.15 if regime in ("extreme", "volatile") else 1.0)
            * _rp["spread_step_mult"]
        )))),
        "sniper_rearm_gap_move_bps": int(round(max(50, min(500,
            arb_alert_threshold_bps * 0.50
            * (1.15 if regime in ("extreme", "volatile") else 1.0)
            * _rp["spread_step_mult"]
        )))),
        "splash_enabled": cfg.SPLASH_ENABLED,
        "enable_coin_prep": cfg.ENABLE_COIN_PREP,
        "enable_runtime_coin_health": cfg.ENABLE_RUNTIME_COIN_HEALTH,

        # F78: expose the risk-profile multipliers that shaped this
        # response. Lets the operator see exactly what `conservative` /
        # `balanced` / `aggressive` actually changed vs each other,
        # rather than guessing from output diffs. Filled below the result
        # construction so it captures any mid-flight overrides.
        "_risk_profile_meta": {
            "name": _risk_profile_name,
            "multipliers": dict(_rp),
        },

        # V2 Metadata for toast + GUI
        "_data_sources": {
            "version": 2,
            "asset_id": asset_id,
            "cat_wallet_id": cat_wid,
            "cat_decimals": decimals,
            "cat_ticker_id": ticker_id,
            "cat_name": cat_name,
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
            # F76: return full precision (was rounding to 2dp, which caused a
            # systematic over-report — 110.6773 → 110.68 — that bled into
            # the GUI's F66 residual-filler and tripped its own preflight).
            # Truncate rather than round so we NEVER over-report the balance.
            "xch_balance": int(float(xch_spendable) * 10000) / 10000,
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
    print(f"[SMART_DEFAULTS v2] === Done! Spread: {api_server._bps_to_pct(base_spread_bps)}, "
          f"Requote: {api_server._bps_to_pct(requote_bps)}, "
          f"Quality: {quality_score}% ===\n")
    log_event("success", "smart_defaults",
              f"Smart Settings: Spread {api_server._bps_to_pct(base_spread_bps)}, "
              f"Requote {api_server._bps_to_pct(requote_bps)}, "
              f"Quality {quality_score}% ({quality_label})",
              {"version": 2, "base_spread_bps": base_spread_bps, "requote_bps": requote_bps,
               "quality_score": quality_score,
               "regime": regime, "fills_per_day": fills_per_day,
               "mid_price": mid_price, "arb_gap_bps": round(arb_gap_bps, 1)})

    # ── LIQUIDITY MODE POST-PROCESS ──────────────────────────────────────
    # The capital-plan solver always computes a two-sided plan. When the
    # caller pinned a single side via `liquidity_mode`, scrub the
    # disabled side's fields so the save layer writes a clean config
    # without stale SELL_* or BUY_* residue.
    #
    # Buy-only: zero MAX_ACTIVE_SELL, null out sell_*_size_xch and all
    # sell-side tier counts / spares. Keep buy_* intact. Sniper off
    # (arb needs both sides). Reverse-buy respected.
    #
    # Sell-only: mirror — zero buy side, keep sell. Sniper off.
    # Reverse-buy flag becomes a no-op (no buy ladder to reverse).
    result["liquidity_mode"] = liquidity_mode
    if liquidity_mode == "buy_only":
        _zero_fields = [
            "max_active_sell",
            "sell_inner_size_xch", "sell_mid_size_xch",
            "sell_outer_size_xch", "sell_extreme_size_xch",
            "sell_inner_tier_count", "sell_mid_tier_count",
            "sell_outer_tier_count", "sell_extreme_tier_count",
            "sell_inner_tier_spare_count", "sell_mid_tier_spare_count",
            "sell_outer_tier_spare_count", "sell_extreme_tier_spare_count",
            # Topup and reserve CAT are the token side — in buy-only
            # we DO still keep CAT reserve (it protects any existing
            # CAT balance from being treated as spendable) but the
            # bot-side topup CAT pool is meaningless.
            "topup_pool_cat",
        ]
        for k in _zero_fields:
            result[k] = 0 if k in ("max_active_sell", "topup_pool_cat") else None
        # Ensure max_position_xch covers the full buy ladder so the position
        # guard (offer_manager: hard limit = max_position * 1.1) never blocks
        # initial ladder creation. Use /0.9 for headroom; round up to 1 dp.
        _buy_ladder_xch = sum(
            (result.get(f"buy_{_t}_size_xch") or 0) * (result.get(f"buy_{_t}_tier_count") or 0)
            for _t in ("inner", "mid", "outer", "extreme")
        )
        if _buy_ladder_xch > 0:
            import math as _math_mp
            _min_pos = round(_math_mp.ceil(_buy_ladder_xch / 0.9 * 10) / 10, 1)
            if (result.get("max_position_xch") or 0) < _min_pos:
                result["max_position_xch"] = _min_pos
        # Sniper arb needs both sides
        result["sniper_enabled"] = False
        result["sniper_prep_count"] = 0
        result["messages"] = (result.get("messages") or []) + [
            "Buy-only mode: sell ladder disabled, sniper off."
        ]
        print("[SMART_DEFAULTS] liquidity_mode=buy_only — zeroed sell-side fields")
    elif liquidity_mode == "sell_only":
        _zero_fields = [
            "max_active_buy",
            "buy_inner_size_xch", "buy_mid_size_xch",
            "buy_outer_size_xch", "buy_extreme_size_xch",
            "buy_inner_tier_count", "buy_mid_tier_count",
            "buy_outer_tier_count", "buy_extreme_tier_count",
            "buy_inner_tier_spare_count", "buy_mid_tier_spare_count",
            "buy_outer_tier_spare_count", "buy_extreme_tier_spare_count",
            "topup_pool_xch",
        ]
        for k in _zero_fields:
            result[k] = 0 if k in ("max_active_buy", "topup_pool_xch") else None
        # Reverse-buy is a buy-ladder concept — force off in sell-only.
        result["buy_ladder_reversed"] = False
        # Ensure max_position_xch covers the full sell ladder so the position
        # guard (offer_manager: hard limit = max_position * 1.1) never blocks
        # initial ladder creation. Use /0.9 for headroom; round up to 1 dp.
        _sell_ladder_xch = sum(
            (result.get(f"sell_{_t}_size_xch") or 0) * (result.get(f"sell_{_t}_tier_count") or 0)
            for _t in ("inner", "mid", "outer", "extreme")
        )
        if _sell_ladder_xch > 0:
            import math as _math_mp
            _min_pos = round(_math_mp.ceil(_sell_ladder_xch / 0.9 * 10) / 10, 1)
            if (result.get("max_position_xch") or 0) < _min_pos:
                result["max_position_xch"] = _min_pos
        # Sniper arb needs both sides
        result["sniper_enabled"] = False
        result["sniper_prep_count"] = 0
        result["messages"] = (result.get("messages") or []) + [
            "Sell-only mode: buy ladder disabled, sniper off."
        ]
        print("[SMART_DEFAULTS] liquidity_mode=sell_only — zeroed buy-side fields")

    # ── UNIVERSAL MAX_POSITION_XCH CONSISTENCY CLAMP ──────────────────────
    # The position hard guard in offer_manager.create_ladder() refuses to
    # create a ladder whose projected XCH exposure would exceed
    # MAX_POSITION_XCH × 1.1. Smart Defaults derives MAX_POSITION_XCH from
    # wallet-size percentages (risk_level × position_mult) while the ladder
    # is sized independently from the trading budget — those two can
    # disagree, and when they do the bot blocks its own first cycle with
    # `position_hard_guard_blocked`.
    #
    # Raise MAX_POSITION_XCH to cover the WORST CASE of:
    #   1. buy-side tier-summed ladder value   (all buys fill → long)
    #   2. sell-side tier-summed ladder value  (all sells fill → short)
    #   3. default_trade_xch × max_active_buy  (guard's current naive proxy)
    #   4. default_trade_xch × max_active_sell
    # Dividing by 0.9 gives the 1.1× hard limit ~22% headroom over the
    # ladder worst-case, so ordinary operation stays well clear of the
    # guard. The existing buy_only/sell_only post-process branches apply a
    # similar clamp for single-sided modes; this catches the default
    # both-sides path which previously had no such check.
    try:
        import math as _math_mp
        _buy_ladder_xch = 0.0
        _sell_ladder_xch = 0.0
        for _t in ("inner", "mid", "outer", "extreme"):
            _bs = result.get(f"buy_{_t}_size_xch") or 0
            _bc = result.get(f"buy_{_t}_tier_count") or 0
            _buy_ladder_xch += float(_bs) * float(_bc)
            _ss = result.get(f"sell_{_t}_size_xch") or 0
            _sc = result.get(f"sell_{_t}_tier_count") or 0
            _sell_ladder_xch += float(_ss) * float(_sc)
        _dts = float(result.get("default_trade_xch") or 0)
        _mab = float(result.get("max_active_buy") or 0)
        _mas = float(result.get("max_active_sell") or 0)
        _worst = max(_buy_ladder_xch, _sell_ladder_xch, _dts * _mab, _dts * _mas)
        if _worst > 0:
            _min_pos = round(_math_mp.ceil(_worst / 0.9 * 10) / 10, 1)
            _cur = float(result.get("max_position_xch") or 0)
            if _cur < _min_pos:
                result["max_position_xch"] = _min_pos
                result.setdefault("messages", []).append(
                    f"MAX_POSITION_XCH raised {_cur} → {_min_pos} XCH so the "
                    f"position guard's 1.1× hard limit covers the full "
                    f"ladder (worst-case fill = {_worst:.2f} XCH)."
                )
                print(f"[SMART_DEFAULTS] Max position consistency clamp: "
                      f"{_cur} → {_min_pos} XCH "
                      f"(worst-case ladder {_worst:.2f} XCH, "
                      f"buy={_buy_ladder_xch:.2f} sell={_sell_ladder_xch:.2f} "
                      f"guard_buy={_dts * _mab:.2f} guard_sell={_dts * _mas:.2f})")
    except Exception as _mp_err:
        print(f"[SMART_DEFAULTS] Max position consistency clamp skipped: {_mp_err}")

    return jsonify(result)
