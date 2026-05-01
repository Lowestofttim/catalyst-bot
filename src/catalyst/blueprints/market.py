"""Market / pricing / Dexie / Coinset / debug routes.

Covers the market-intel surface (Dexie stats, orderbook, slippage, DBX
eligibility, TibetSwap AMM, price feeds) and the debug endpoints used by
operators to sanity-check pricing, coin prep, and Sage offer creation.

`_fetch_dbx_pair_status` lives here since only market routes use it.
`_fetch_price_standalone` and `_fetch_dexie_orderbook_standalone` stay in
api_server because smart-defaults also uses them; blueprint routes reach
them via `api_server.xxx`.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event

try:
    from api_call_tracker import record as _record_api_call
except Exception:
    def _record_api_call(*args, **kwargs):
        return None


bp = Blueprint("market", __name__)


def _fetch_dbx_pair_status(asset_id: str, ticker_id: str) -> dict:
    """Fetch Dexie pair-level rewards status from /v1/incentives.

    The previous implementation looked for an ``incentives`` field on the
    ticker response that doesn't exist there, so it always returned None.
    The authoritative source is /v1/incentives, which dexie_incentives.py
    wraps with a 5-minute cache.

    Returns the projected per-direction shape used by the GUI Market Intel
    panel and Smart Settings.
    """
    result: dict = {
        "pair_incentivized": None,
        "pair_source": "",
        "buy": None,
        "sell": None,
    }
    if not asset_id:
        return result

    try:
        from dexie_incentives import fetch_incentives, get_pair_incentives
        bulk = fetch_incentives()
        # If the upstream call genuinely failed (no incentives at all and
        # success=False) we keep pair_incentivized as None so the GUI can
        # render "unavailable" instead of falsely claiming "not incentivized".
        if not bulk.get("success") and not bulk.get("incentives"):
            result["pair_source"] = "unavailable"
            return result
        pair = get_pair_incentives(asset_id)
        result["pair_incentivized"] = bool(pair.get("incentivized"))
        result["pair_source"] = "dexie_incentives_api"
        result["buy"] = pair.get("buy")
        result["sell"] = pair.get("sell")
    except Exception:
        result["pair_source"] = "unavailable"
    return result


@bp.route("/api/dexie/stats")
def api_dexie_stats():
    """Get Dexie posting statistics."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500
    return jsonify(bot.dexie_manager.get_stats())


@bp.route("/api/dexie/repost", methods=["POST"])
def api_dexie_repost():
    """Repost all active offers to Dexie."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500
    open_buys, open_sells, _ = bot.offer_manager.sync_from_wallet()
    all_offers = open_buys + open_sells
    bot.dexie_manager.repost_active_offers(all_offers)
    return jsonify({"status": "queued", "count": len(all_offers)})


@bp.route("/api/market/intel")
def api_market_intel():
    """Get full market intelligence summary.

    Includes competitor analysis, orderbook depth, and DBX eligibility.
    """
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        bot.market_intel.refresh_orderbook(force=True)
    except Exception:
        pass

    summary = bot.market_intel.get_market_summary()
    asset_id = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
    ticker_id = api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
    try:
        buy_spread = bot.risk_manager.get_adjusted_spread("buy")
        sell_spread = bot.risk_manager.get_adjusted_spread("sell")
        avg_spread_bps = ((buy_spread + sell_spread) / 2) * Decimal("10000")
        mid_price = bot.price_engine.get_last_price() or Decimal("0")
        live_dbx = bot.market_intel.check_dbx_eligibility(avg_spread_bps, mid_price)
    except Exception:
        live_dbx = {}
        mid_price = Decimal("0")

    try:
        local_book = api_server._get_live_local_offer_edges(asset_id)
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
            dbx["eligible_buy"] = bool(live_dbx.get("eligible_buy", False))
            dbx["eligible_sell"] = bool(live_dbx.get("eligible_sell", False))
            dbx["max_spread_bps"] = str(
                live_dbx.get("max_eligible_spread", dbx.get("max_spread_bps", "0"))
            )
            dbx["estimated_apr"] = str(
                live_dbx.get("estimated_dbx_rate", dbx.get("estimated_apr", "0"))
            )
            dbx["buy_incentive"] = live_dbx.get("buy_incentive")
            dbx["sell_incentive"] = live_dbx.get("sell_incentive")
            dbx["pair_incentivized"] = live_dbx.get("pair_incentivized")
        dbx["spread_eligible"] = bool(dbx.get("eligible"))
        # Refresh pair_incentivized + per-direction details from the live
        # /v1/incentives cache. check_dbx_eligibility is TTL-gated (5 min)
        # so this fills in current data even when the eligibility check
        # itself was skipped this cycle.
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
        summary["spacescan"] = api_server._get_spacescan_market_context(
            asset_id=asset_id,
            ticker_id=ticker_id,
            decimals=int(api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3) or 3),
            executable_mid_price=float(mid_price or 0),
        )
    except Exception:
        pass

    return jsonify(api_server._serialize_dict(summary))


@bp.route("/api/dbx/info")
def api_dbx_info():
    """Lightweight per-pair Dexie-incentive lookup.

    Query: ?asset_id=<hex>  (defaults to the active CAT)

    Returns just the projected incentive blob for the requested pair —
    no orderbook refresh, no market-intel computation. The Smart
    Settings DBX-cap pre-prompt calls this so the modal appears
    instantly instead of waiting on a forced /api/market/intel refresh.
    """
    asset_id = (request.args.get("asset_id") or "").strip()
    if not asset_id:
        asset_id = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
    out = {
        "asset_id": asset_id,
        "pair_incentivized": None,
        "max_spread_bps": 0,
        "estimated_apr": 0.0,
        "reward_token": "",
        "buy": None,
        "sell": None,
        # Whether new offer posts carry the claim_rewards flag — drives
        # the GUI's Pending Rewards panel UX (auto-claim ON → hide manual
        # claim button, show "auto-paid by Dexie" copy).
        "auto_claim_enabled": bool(getattr(cfg, "DEXIE_AUTO_CLAIM_REWARDS", True)),
    }
    if not asset_id:
        return jsonify(out)
    try:
        from dexie_incentives import fetch_incentives, get_pair_incentives
        bulk = fetch_incentives()
        if not bulk.get("success") and not bulk.get("incentives"):
            return jsonify(out)  # API unreachable — pair_incentivized stays None
        pair = get_pair_incentives(asset_id)
        out["pair_incentivized"] = bool(pair.get("incentivized"))
        out["buy"] = pair.get("buy")
        out["sell"] = pair.get("sell")
        sides = [s for s in (out["buy"], out["sell"]) if s]
        caps = [int(s.get("max_spread_bps") or 0) for s in sides if (s.get("max_spread_bps") or 0) > 0]
        if caps:
            out["max_spread_bps"] = min(caps)
        aprs = [float(s.get("estimated_apr") or 0) for s in sides if (s.get("estimated_apr") or 0) > 0]
        if aprs:
            out["estimated_apr"] = max(aprs)
        for s in sides:
            tok = (s.get("reward_token") or "").strip()
            if tok:
                out["reward_token"] = tok
                break
    except Exception:
        pass
    return jsonify(out)


@bp.route("/api/dbx/pending")
def api_dbx_pending():
    """List the user's offers that currently have claimable Dexie rewards."""
    try:
        from dexie_claims import list_pending_rewards
        result = list_pending_rewards() or {}
        # Include the auto-claim toggle so the GUI panel can render the
        # right messaging in a single round-trip (auto-claim ON →
        # informational only; auto-claim OFF → show Claim button).
        result["auto_claim_enabled"] = bool(getattr(cfg, "DEXIE_AUTO_CLAIM_REWARDS", True))
        return jsonify(result)
    except Exception as e:
        log_event("error", "dbx_claim", f"pending lookup failed: {e}")
        return jsonify({"success": False, "error": str(e), "offers": [], "totals": {}})


@bp.route("/api/dbx/claim", methods=["POST"])
def api_dbx_claim():
    """Sign and submit claims for all pending Dexie rewards.

    Optional JSON body: ``{"target_address": "xch1..."}`` to redirect rewards
    to a different address. No XCH leaves the wallet — only a signed
    message is sent to Dexie.
    """
    payload = request.get_json(silent=True) or {}
    target = (payload.get("target_address") or "").strip() or None
    try:
        from dexie_claims import claim_all
        result = claim_all(target_address=target)
    except Exception as e:
        log_event("error", "dbx_claim", f"claim failed: {e}")
        return jsonify({"success": False, "error": str(e)})
    log_event(
        "success" if result.get("success") else "warning",
        "dbx_claim",
        f"claim attempt: submitted={result.get('claims_submitted', 0)} "
        f"success={result.get('success')}",
    )
    return jsonify(result)


@bp.route("/api/market/price-history")
def api_market_price_history():
    """Return persisted price samples for the active pair."""
    asset_id = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
    if not asset_id:
        return jsonify({"success": False, "error": "No active CAT", "points": []}), 400

    try:
        hours = float(request.args.get("hours", "0.333333") or "0.333333")
    except (TypeError, ValueError):
        hours = 0.333333
    hours = max(0.01, min(hours, 24.0))

    try:
        limit = int(request.args.get("limit", "3000") or "3000")
    except (TypeError, ValueError):
        limit = 3000
    limit = max(2, min(limit, 5000))

    try:
        from database import get_recent_prices
        rows = get_recent_prices(asset_id, hours=hours, limit=limit)
        points = [
            {
                "timestamp": row.get("timestamp"),
                "mid": row.get("combined_price"),
                "dexie": row.get("dexie_price"),
                "tibet": row.get("tibet_price"),
                "strategy": row.get("strategy_used"),
            }
            for row in rows
        ]
        return jsonify({
            "success": True,
            "asset_id": asset_id,
            "range_hours": hours,
            "points": points,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/market/orderbook")
def api_market_orderbook():
    """Force refresh and return orderbook data."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    data = bot.market_intel.refresh_orderbook(force=True)
    return jsonify(api_server._serialize_dict(data))


@bp.route("/api/market/slippage")
def api_market_slippage():
    """Get TibetSwap slippage estimate for a given trade size.

    Query params: amount (XCH), side (buy/sell)
    """
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    amount = request.args.get("amount", "0.01")
    side = request.args.get("side", "buy")

    try:
        quote = bot.price_engine.get_tibet_quote(
            amount_xch=Decimal(amount),
            side=side
        )
        if quote:
            return jsonify(quote)
        return jsonify({"error": "Could not get quote"}), 404
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/market/dbx")
def api_market_dbx():
    """Get DBX rewards eligibility status."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        buy_spread = bot.risk_manager.get_adjusted_spread("buy")
        sell_spread = bot.risk_manager.get_adjusted_spread("sell")
        avg_spread_bps = ((buy_spread + sell_spread) / 2) * Decimal("10000")
        mid_price = bot.price_engine.get_last_price() or Decimal("0")

        dbx = bot.market_intel.check_dbx_eligibility(avg_spread_bps, mid_price)
        asset_id = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        ticker_id = api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
        dbx["spread_eligible"] = bool(dbx.get("eligible_offers", 0))
        dbx.update(_fetch_dbx_pair_status(asset_id, ticker_id))
        return jsonify(api_server._serialize_dict(dbx))
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/coinset/stats")
def api_coinset_stats():
    """Get Coinset API query statistics."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    stats = bot.coinset_client.get_stats()
    health = bot.coinset_client.check_health()
    stats["health"] = health
    return jsonify(stats)


@bp.route("/api/price")
def api_price():
    """Get current price from all sources."""
    bot = api_server.bot
    cfg = api_server.cfg
    asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    decimals = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
    ticker = api_server._active_cat.get("ticker_id") or (cfg.CAT_TICKER_ID if hasattr(cfg, "CAT_TICKER_ID") else "")

    if bot:
        price_data = bot.price_engine.get_price(asset_id, decimals, ticker)
        result = api_server._serialize_dict(price_data)
        # GUI expects "mid" key — price_engine returns "mid_price"
        if "mid" not in result and "mid_price" in result:
            result["mid"] = result["mid_price"]
        # Ensure "success" key exists for GUI fallback check
        if "mid" not in result:
            result["mid"] = 0
        result["success"] = float(result.get("mid", 0) or 0) > 0
        return jsonify(result)

    # Bot not running — lightweight price lookup via api_server helper
    return api_server._fetch_price_standalone(asset_id, decimals)


@bp.route("/api/market/summary")
def api_market_summary():
    """Lightweight market overview for the dashboard.

    Returns best bid/ask from Dexie orderbook, 24h volume, TibetSwap pool
    depth, and price sources — all in one call. Works whether the bot is
    running or not.
    """
    cfg = api_server.cfg
    import requests as _req

    asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    ticker_id = api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")
    decimals = int(api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3))

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
            _record_api_call("dexie", "/v2/prices/tickers")
            resp = _req.get(f"{dexie_base}/v2/prices/tickers",
                            params={"ticker_id": tid}, timeout=8)
            if resp.status_code == 200:
                tickers = resp.json().get("tickers", [])
                if tickers:
                    t = tickers[0]
                    result["dexie_price"] = float(t.get("current_avg_price", 0) or 0)
                    result["volume_24h"] = float(t.get("target_volume", 0) or 0)
                    _ticker_bid = float(t.get("bid", 0) or 0)
                    _ticker_ask = float(t.get("ask", 0) or 0)
                    if _ticker_bid > 0:
                        result["best_bid"] = _ticker_bid
                    if _ticker_ask > 0:
                        result["best_ask"] = _ticker_ask
    except Exception:
        pass

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
        _record_api_call("dexie", "/v1/offers")
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

        _record_api_call("dexie", "/v1/offers")
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

    try:
        dexie_total_xch = 0.0
        _record_api_call("dexie", "/v1/offers")
        resp = _req.get(f"{dexie_base}/v1/offers",
                        params={"offered": asset_id, "requested": "xch",
                                "status": 0, "page_size": 50},
                        timeout=8)
        if resp.status_code == 200:
            for offer in resp.json().get("offers", []):
                for asset in offer.get("requested", []):
                    if str(asset.get("code", "")).upper() == "XCH":
                        dexie_total_xch += float(asset.get("amount", 0) or 0)

        _record_api_call("dexie", "/v1/offers")
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

    try:
        _record_api_call("tibetswap", "/pairs")
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


@bp.route("/api/price/tibet")
def api_tibet_price():
    """Get TibetSwap pool info."""
    bot = api_server.bot
    cfg = api_server.cfg
    asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    decimals = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)

    if bot:
        pool = bot.price_engine.get_tibet_pool_info(asset_id)
        return jsonify(api_server._serialize_dict(pool))

    return api_server._fetch_price_standalone(asset_id, decimals)


@bp.route("/api/amm/price")
def api_amm_price():
    """Get live TibetSwap AMM state from the AMMMonitor background poller."""
    bot = api_server.bot
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
            "drift_bps": str(drift.quantize(Decimal("0.1"))) if drift is not None else None,
            "pair_id": stats.get("pair_id", ""),
            "total_polls": stats.get("total_polls", 0),
            "failed_polls": stats.get("failed_polls", 0),
            "consecutive_failures": stats.get("consecutive_failures", 0),
            "last_success_ago_secs": stats.get("last_success_ago_secs"),
            "poll_interval_secs": getattr(cfg, "AMM_POLL_INTERVAL_SECS", 30),
            "drift_threshold_bps": str(getattr(cfg, "AMM_DRIFT_REQUOTE_BPS", "40")),
            "buffer_enabled": getattr(cfg, "ENABLE_AMM_BUFFER", False),
            "buffer_bps": str(getattr(cfg, "AMM_BUFFER_BPS", "30")),
            "arb_pressure":        stats.get("arb_pressure"),
            "arb_pressure_label":  stats.get("arb_pressure_label"),
            "dynamic_buffer":      stats.get("dynamic_buffer", {}),
            "sweep_protection":    {
                side: round(max(0, expiry - time.time()), 1)
                for side, expiry in getattr(bot, "_sweep_protection", {}).items()
                if expiry > time.time()
            },
        }
        return jsonify(result)
    except Exception as e:
        return jsonify({"available": False, "error": str(e)})


@bp.route("/api/debug/coinprep")
def api_debug_coinprep():
    """Debug: shows coin prep worker status and any error output."""
    bot = api_server.bot
    result = {"_coin_prep_state": api_server._coin_prep_state}

    base_dir = os.path.dirname(os.path.abspath(api_server.__file__))
    status_file = os.path.join(base_dir, "coin_prep_status.json")
    if os.path.exists(status_file):
        try:
            with open(status_file, "r") as f:
                result["worker_status_file"] = json.load(f)
        except Exception as e:
            result["worker_status_file_error"] = str(e)
    else:
        result["worker_status_file"] = "NOT FOUND"

    log_file = os.path.join(base_dir, "coin_prep_output.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_content = f.read()
            result["worker_output_log"] = log_content[-2000:]
        except Exception as e:
            result["worker_output_log_error"] = str(e)
    else:
        result["worker_output_log"] = "NOT FOUND"

    if bot:
        try:
            result["coin_manager_status"] = bot.coin_manager.check_coin_prep_status()
        except Exception as e:
            result["coin_manager_error"] = str(e)

    try:
        from database import get_recent_events
        events = get_recent_events(limit=20)
        prep_events = [e for e in events if "coin_prep" in str(e.get("event_type", ""))]
        result["recent_coin_prep_events"] = prep_events[:10]
    except Exception:
        pass

    return jsonify(result)


@bp.route("/api/debug/pricing")
def api_debug_pricing():
    """Debug: shows exactly what pricing the GUI sees."""
    import requests as _req
    bot = api_server.bot
    result = {"_active_cat": {k: str(v)[:50] if v else None for k, v in api_server._active_cat.items()}}
    result["bot_exists"] = bot is not None

    asset_id = api_server._active_cat.get("asset_id") or ""
    cat_dec = api_server._active_cat.get("decimals") or 3
    ticker_id = api_server._active_cat.get("ticker_id") or ""
    result["asset_id"] = asset_id
    result["ticker_id"] = ticker_id

    try:
        resp = _req.get("http://127.0.0.1:5000/api/status", timeout=15)
        status_data = resp.json()
        result["status_pricing"] = status_data.get("pricing", "MISSING")
        result["status_current_cat"] = status_data.get("current_cat", "MISSING")
    except Exception as e:
        result["status_error"] = str(e)

    try:
        resp = _req.get("http://127.0.0.1:5000/api/price", timeout=15)
        result["price_response"] = resp.json()
    except Exception as e:
        result["price_error"] = str(e)

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


@bp.route("/api/debug/tibet-test")
def api_debug_tibet_test():
    """Debug endpoint: test TibetSwap API connectivity directly."""
    cfg = api_server.cfg
    result = {"test": "TibetSwap API connectivity"}
    asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
    result["asset_id_used"] = asset_id
    result["_active_cat"] = {k: str(v)[:30] if v else None for k, v in api_server._active_cat.items()}

    try:
        import requests as _req
        resp = _req.get("https://api.v2.tibetswap.io/pairs",
                        params={"skip": 0, "limit": 200}, timeout=10)
        result["tibet_status"] = resp.status_code
        if resp.status_code == 200:
            pairs = resp.json()
            result["total_pairs"] = len(pairs)
            result["sample_pairs"] = [
                {"name": p.get("short_name", p.get("name", "?")), "asset_id": str(p.get("asset_id", ""))[:20] + "..."}
                for p in pairs[:3]
            ]
            if asset_id:
                norm = asset_id.lower().strip().replace("0x", "")
                for p in pairs:
                    pid = str(p.get("asset_id", "")).lower().strip().replace("0x", "")
                    if pid == norm:
                        xr = float(p.get("xch_reserve", 0)) / 1e12
                        dec = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
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


@bp.route("/api/debug/sage-single-offer-test", methods=["POST"])
def api_debug_sage_single_offer_test():
    """Create one selected-coin XCH offer and one CAT offer, inspect, cancel."""
    cfg = api_server.cfg
    try:
        from wallet import (
            get_wallet_type,
            create_offer,
            cancel_offer,
            get_owned_coins_detailed,
        )
        if get_wallet_type() != "sage":
            return jsonify({"ok": False, "error": "sage_only_debug_route"}), 400

        from database import get_smallest_free_tier_spare

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

        xch_coin = get_smallest_free_tier_spare("xch")
        cat_coin = get_smallest_free_tier_spare("cat")
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
        return api_server._api_error(e, request.path)
