"""Close the Gap / maker boost routes.

Three routes that control the sniper-style adaptive probe that tightens
spreads to improve Dexie ranking. Activation is async (background thread)
so the GUI doesn't block on wallet RPC.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event


bp = Blueprint("boost", __name__)


@bp.route("/api/boost/activate", methods=["POST"])
def api_boost_activate():
    """Activate Close the Gap — adaptive spread probing for ranking."""
    bot = api_server.bot
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
    start_pct_override_dexie = None
    try:
        mid_f = float(mid_price) if mid_price and float(mid_price) > 0 else 0
        if mid_f > 0:
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

            candidates_bid = [p for p in [our_bid, comp_bid] if p > 0]
            candidates_ask = [p for p in [our_ask, comp_ask] if p > 0 and p > mid_f]
            best_dexie_bid = max(candidates_bid) if candidates_bid else 0
            best_dexie_ask = min(candidates_ask) if candidates_ask else 0

            if best_dexie_bid > 0 and best_dexie_ask > 0 and best_dexie_ask > best_dexie_bid:
                buy_half_bps  = int(((mid_f - best_dexie_bid) / mid_f) * 10000)
                sell_half_bps = int(((best_dexie_ask - mid_f) / mid_f) * 10000)
                tighter_half  = min(buy_half_bps, sell_half_bps)
                if tighter_half > 0:
                    main_spread_bps = max(1, int(tighter_half * 2 * 0.95))
                    start_pct_override_dexie = 100

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

        if main_spread_bps == 0 and bot.risk_manager:
            buy_spread  = bot.risk_manager.get_adjusted_spread("buy")  * Decimal("10000")
            sell_spread = bot.risk_manager.get_adjusted_spread("sell") * Decimal("10000")
            main_spread_bps = int((buy_spread + sell_spread) / 2)
    except Exception:
        pass

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

    if start_pct_override is None and start_pct_override_dexie is not None:
        start_pct_override = start_pct_override_dexie

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
                api_server.events.emit("boost", bot.boost_manager.get_state())
            elif result.get("error"):
                log_event("error", "gap_closer_activate_failed", result["error"])
        except Exception as e:
            log_event("error", "gap_closer_activate_error", f"Activation failed: {e}")

    t = threading.Thread(target=_activate_bg, daemon=True)
    t.start()

    return jsonify({
        "success": True,
        "spread_bps": expected_spread,
        "arb_floor_bps": expected_floor,
        "created": 0,
        "async": True,
        "warnings": [],
    })


@bp.route("/api/boost/deactivate", methods=["POST"])
def api_boost_deactivate():
    """Deactivate Close the Gap — cancel all gap-closer offers."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    result = bot.boost_manager.deactivate()
    api_server.events.emit("boost", {"active": False})
    return jsonify(result)


@bp.route("/api/boost/state")
def api_boost_state():
    """Get current boost state."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.boost_manager.get_state())
