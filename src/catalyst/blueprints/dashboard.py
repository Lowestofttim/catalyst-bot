"""Dashboard, stats, inventory, and risk-spread summary routes.

Read-only aggregator routes that compose information from multiple
bot subsystems for the GUI dashboard view. Depend heavily on helpers
still living in api_server (_get_health_snapshot, _get_live_mid_price_str,
_get_live_local_offer_edges, _get_spacescan_market_context, etc.).
"""

from __future__ import annotations

import time
from decimal import Decimal
from urllib.parse import quote

from flask import Blueprint, jsonify, request

import api_server
from database import get_stats


bp = Blueprint("dashboard", __name__)


def _get_cfg_int(cfg, name: str, fallback: str | None = None) -> int:
    value = getattr(cfg, name, None)
    if value is None and fallback:
        value = getattr(cfg, fallback, 0)
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _build_coin_recommendations(cfg, coins: dict, is_running: bool) -> list[dict]:
    """Build user-facing coin recommendations for the dashboard.

    These are runtime nudges, not startup prep instructions. Full Coin Prep
    cancels/rebuilds offers, so live recommendations should prefer adding or
    allocating top-up fuel unless the bot explicitly has to stop and rebuild.
    """
    if not is_running or not getattr(cfg, "TIER_ENABLED", False):
        return []
    if not getattr(cfg, "ENABLE_SELL", True):
        return []

    tier_counts = (coins or {}).get("tier_counts") or {}
    if not tier_counts.get("enabled", False):
        return []

    cat_counts = tier_counts.get("cat") or {}
    cat_reserve_count = int(cat_counts.get("reserve", 0) or 0)
    cat_dust_count = int(cat_counts.get("dust", 0) or 0)
    if cat_reserve_count > 0 or cat_dust_count >= 2:
        return []

    targets = {
        "inner": _get_cfg_int(
            cfg, "SELL_INNER_TIER_SPARE_COUNT", "INNER_TIER_SPARE_COUNT"
        ),
        "mid": _get_cfg_int(cfg, "SELL_MID_TIER_SPARE_COUNT", "MID_TIER_SPARE_COUNT"),
        "outer": _get_cfg_int(
            cfg, "SELL_OUTER_TIER_SPARE_COUNT", "OUTER_TIER_SPARE_COUNT"
        ),
        "extreme": _get_cfg_int(
            cfg, "SELL_EXTREME_TIER_SPARE_COUNT", "EXTREME_TIER_SPARE_COUNT"
        ),
    }
    try:
        sniper_enabled = (
            bool(getattr(cfg, "SNIPER_ENABLED", False))
            and int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0) > 0
            and Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", "0") or "0")) > 0
        )
    except Exception:
        sniper_enabled = False
    if sniper_enabled:
        targets["sniper"] = _get_cfg_int(cfg, "SNIPER_PREP_COUNT")

    short = []
    labels = {
        "inner": "inner",
        "mid": "mid",
        "outer": "outer",
        "extreme": "extreme",
        "sniper": "sniper",
    }
    for tier, target in targets.items():
        if target <= 0:
            continue
        have = int(cat_counts.get(tier, 0) or 0)
        if have < target:
            short.append(f"{labels.get(tier, tier)} {have}/{target}")

    if not short:
        return []

    return [
        {
            "id": "cat_topup_pool_empty",
            "severity": "warning",
            "title": "CAT top-up pool empty",
            "message": (
                "CAT spare coins are below target ("
                + ", ".join(short[:4])
                + "). The bot has no CAT top-up pool to split from. "
                "Add CAT funds or allocate an incoming CAT coin to the top-up pool "
                "so sell-side spares can be rebuilt while the bot keeps running."
            ),
            "action": "reviewTopupPool",
            "action_label": "Review top-up pool",
            "action_value": "cat",
        }
    ]


def _positive_decimal_string(value) -> str:
    try:
        dec = Decimal(str(value))
    except Exception:
        return ""
    if dec <= 0:
        return ""
    return str(dec)


def _build_fiat_price_summary(spacescan_context: dict) -> dict:
    """Build the dashboard's compact fiat price summary."""
    xch_price = {}
    try:
        from market_data_collector import get_cached_xch_usd_price

        xch_price = get_cached_xch_usd_price() or {}
    except Exception:
        xch_price = {}

    xch_usd = _positive_decimal_string(xch_price.get("xch_usd"))
    cat_usd = _positive_decimal_string((spacescan_context or {}).get("price_usd"))
    cat_xch = _positive_decimal_string((spacescan_context or {}).get("price_xch"))

    return {
        "xch_usd_price": xch_usd,
        "xch_usd_source": str(xch_price.get("source") or "") if xch_usd else "",
        "xch_usd_last_updated_at": int(xch_price.get("last_updated_at") or 0)
        if xch_usd
        else 0,
        "cat_usd_price": cat_usd,
        "cat_price_xch": cat_xch,
        "cat_usd_source": "spacescan" if cat_usd else "",
    }


@bp.route("/api/dashboard")
def api_dashboard():
    """Aggregated endpoint for the Command Centre panel.

    Returns all settings, market health, wallet balances, coin counts,
    and performance stats in one call. Designed to be called once on
    page load, then kept live via SSE dashboard_update events.
    """
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        from database import (
            get_stats,
            get_coin_summary,
            get_open_offers,
            get_live_tier_group_counts,
        )

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

        active_asset_id = api_server._active_cat.get("asset_id") or getattr(
            cfg, "CAT_ASSET_ID", ""
        )
        active_ticker_id = api_server._active_cat.get("ticker_id") or getattr(
            cfg, "CAT_TICKER_ID", ""
        )
        active_decimals = int(
            api_server._active_cat.get("decimals")
            or getattr(cfg, "CAT_DECIMALS", 3)
            or 3
        )

        # --- Market Health ---
        market_health = {
            "status": "green",
            "message": "Waiting for first cycle",
            "conditions": [],
            "metrics": {},
        }
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
                    metrics["market_intel_state"] = (
                        "ready" if refreshes > 0 else "searching"
                    )
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
                        metrics["market_spread_bps"] = str(
                            summary.get("competitor_spread_bps", "0")
                        )
                    if summary.get("overall_spread_bps") is not None:
                        metrics["overall_spread_bps"] = str(
                            summary.get("overall_spread_bps", "0")
                        )
            except Exception:
                pass
            try:
                live_edges = {}
                cached_edges = getattr(bot, "_last_live_offer_edges", {}) or {}
                if cached_edges:
                    live_edges.update(cached_edges)

                def _edge_decimal(source: dict, key: str) -> Decimal:
                    try:
                        return Decimal(str(source.get(key, 0) or 0))
                    except Exception:
                        return Decimal("0")

                best_bid = _edge_decimal(live_edges, "our_best_bid")
                best_ask = _edge_decimal(live_edges, "our_best_ask")
                if active_asset_id and (best_bid <= 0 or best_ask <= 0):
                    local_edges = (
                        api_server._get_live_local_offer_edges(active_asset_id) or {}
                    )
                    live_edges.update(local_edges)
                    best_bid = _edge_decimal(live_edges, "our_best_bid")
                    best_ask = _edge_decimal(live_edges, "our_best_ask")

                if best_bid > 0:
                    metrics["our_best_bid"] = str(best_bid)
                if best_ask > 0:
                    metrics["our_best_ask"] = str(best_ask)
                if live_edges.get("our_open_buys") is not None:
                    metrics["our_open_buys"] = int(live_edges.get("our_open_buys") or 0)
                if live_edges.get("our_open_sells") is not None:
                    metrics["our_open_sells"] = int(
                        live_edges.get("our_open_sells") or 0
                    )

                live_state = getattr(bot, "_bot_state", {}) or {}
                try:
                    mid = Decimal(
                        str(
                            live_state.get("mid_price")
                            or getattr(bot, "_current_mid_price", None)
                            or api_server._get_live_mid_price_str()
                            or 0
                        )
                    )
                except Exception:
                    mid = Decimal("0")
                if mid <= 0 and best_bid > 0 and best_ask > best_bid:
                    mid = (best_bid + best_ask) / Decimal("2")
                if best_bid > 0 and best_ask > best_bid and mid > 0:
                    metrics["your_spread_bps"] = str(
                        (best_ask - best_bid) / mid * Decimal("10000")
                    )
            except Exception:
                pass

        executable_mid = Decimal("0")
        try:
            if bot and getattr(bot, "price_engine", None):
                _lp = bot.price_engine.get_last_price()
                executable_mid = Decimal(str(_lp)) if _lp else Decimal("0")
        except Exception:
            executable_mid = Decimal("0")

        spacescan_context = api_server._get_spacescan_market_context(
            asset_id=active_asset_id,
            ticker_id=active_ticker_id,
            decimals=active_decimals,
            executable_mid_price=executable_mid,
        )
        fiat_prices = _build_fiat_price_summary(spacescan_context)
        try:
            metrics = market_health.setdefault("metrics", {})
            metrics["spacescan_enabled"] = spacescan_context.get("enabled", False)
            metrics["spacescan_tier"] = spacescan_context.get("tier", "free")
            metrics["spacescan_has_data"] = spacescan_context.get("has_data", False)
            metrics["spacescan_holder_count"] = spacescan_context.get("holder_count", 0)
            metrics["spacescan_activity_level"] = spacescan_context.get(
                "activity_level", "unknown"
            )
            metrics["spacescan_risk_level"] = spacescan_context.get(
                "risk_level", "unknown"
            )
            metrics["spacescan_price_gap_bps"] = str(
                spacescan_context.get("price_gap_bps", 0)
            )
            # Partial-fetch flags so the GUI can show "rate-limited" rather
            # than rendering 0/unknown as if the data were genuinely absent.
            metrics["spacescan_activity_fetch_failed"] = bool(
                spacescan_context.get("activity_fetch_failed", False)
            )
            metrics["spacescan_holder_from_prior_cache"] = bool(
                spacescan_context.get("holder_count_from_prior_cache", False)
            )
            metrics["spacescan_activity_from_prior_cache"] = bool(
                spacescan_context.get("activity_count_from_prior_cache", False)
            )
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
        wallet = {
            "xch_spendable": 0,
            "xch_total": 0,
            "cat_spendable": 0,
            "cat_total": 0,
        }
        # tier_counts is intentionally OMITTED here. We only populate it
        # below when TIER_ENABLED is true AND we successfully read the
        # coin summary. Sending a placeholder {"enabled": False, ...}
        # caused the GUI's spare-tier panel to flicker on/off whenever a
        # transient DB read raised: the frontend treats enabled=false as
        # "hide", but the user's tier mode is actually still on. Leaving
        # the key absent lets the frontend keep its last known render.
        coins = {
            "xch_free": 0,
            "xch_locked": 0,
            "xch_total": 0,
            "cat_free": 0,
            "cat_locked": 0,
            "cat_total": 0,
        }

        # Fetch wallet balances directly from RPC (works whether bot is running or not)
        try:
            from wallet import get_wallet_balance, WALLET_ID_XCH

            xr = get_wallet_balance(WALLET_ID_XCH)
            if xr and xr.get("success"):
                wb = xr.get("wallet_balance") or {}
                wallet["xch_total"] = str(
                    Decimal(str(wb.get("confirmed_wallet_balance", 0)))
                    / Decimal("1000000000000")
                )
                wallet["xch_spendable"] = str(
                    Decimal(str(wb.get("spendable_balance", 0)))
                    / Decimal("1000000000000")
                )
            cat_wid = api_server._active_cat.get("wallet_id") or getattr(
                cfg, "CAT_WALLET_ID", 2
            )
            cat_dec = api_server._active_cat.get("decimals") or getattr(
                cfg, "CAT_DECIMALS", 3
            )
            cr = get_wallet_balance(cat_wid)
            if cr and cr.get("success"):
                wb = cr.get("wallet_balance") or {}
                _cat_divisor = Decimal(10) ** int(cat_dec)
                wallet["cat_total"] = str(
                    Decimal(str(wb.get("confirmed_wallet_balance", 0))) / _cat_divisor
                )
                wallet["cat_spendable"] = str(
                    Decimal(str(wb.get("spendable_balance", 0))) / _cat_divisor
                )
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
            try:
                is_running = bool(bot and bot.is_running())
            except Exception:
                is_running = False
            coins["recommendations"] = _build_coin_recommendations(
                cfg,
                coins,
                is_running=is_running,
            )
        except Exception:
            pass

        # Sage RPC fallback: if bot isn't running (or coin_manager returned zeros),
        # query Sage directly so the dashboard always shows real coin counts.
        if coins["xch_free"] == 0 and coins["xch_total"] == 0:
            try:
                from wallet import rpc as wallet_rpc

                cat_asset_id = api_server._active_cat.get("asset_id") or getattr(
                    cfg, "CAT_ASSET_ID", ""
                )

                def _dash_count_coins(asset_id, filter_mode):
                    """Query Sage get_coins and return (count, total_mojos)."""
                    result = wallet_rpc(
                        "get_coins",
                        {
                            "asset_id": asset_id,
                            "offset": 0,
                            "limit": 500,
                            "filter_mode": filter_mode,
                        },
                        timeout=10,
                    )
                    if not result:
                        return 0, 0
                    coin_list = (
                        result.get("coins")
                        or result.get("records")
                        or result.get("data")
                        or []
                    )
                    total_mojos = sum(int(c.get("amount", "0")) for c in coin_list)
                    return len(coin_list), total_mojos

                xch_free, _ = _dash_count_coins(None, "selectable")
                cat_free, _ = (
                    _dash_count_coins(cat_asset_id, "selectable")
                    if cat_asset_id
                    else (0, 0)
                )

                coins["xch_free"] = xch_free
                coins["xch_total"] = xch_free
                coins["cat_free"] = cat_free
                coins["cat_total"] = cat_free
            except Exception as e:
                print(f"[DASHBOARD] Sage coin count fallback error: {e}", flush=True)

        # --- Performance Stats ---
        performance = {}
        try:
            stats = get_stats(
                cfg.CAT_ASSET_ID, since=api_server._get_run_history_cutoff()
            )
            performance = api_server._serialize_dict(stats)
            performance["pending_verification_count"] = (
                api_server._get_session_pending_verification_count()
            )
        except Exception:
            pass

        # Add uptime from bot loop
        if bot:
            try:
                active_cat_id = api_server._active_cat.get("asset_id") or getattr(
                    cfg, "CAT_ASSET_ID", ""
                )
                live_open_buys = len(
                    get_open_offers(side="buy", cat_asset_id=active_cat_id)
                )
                live_open_sells = len(
                    get_open_offers(side="sell", cat_asset_id=active_cat_id)
                )
                performance["open_buys"] = live_open_buys
                performance["open_sells"] = live_open_sells
                performance["open_offers"] = live_open_buys + live_open_sells
                performance["loop_count"] = bot._loop_count
                performance["uptime_secs"] = (
                    int(time.time() - bot._start_time)
                    if getattr(bot, "_start_time", 0)
                    else 0
                )
            except Exception:
                pass

        sniper_stats = {}
        if bot and getattr(bot, "sniper", None):
            try:
                sniper_stats = api_server._serialize_dict(bot.sniper.get_stats())
            except Exception:
                sniper_stats = {}

        # Boost stats — surfaced alongside sniper since both share the
        # sniper coin pool and the GUI's "Sniper Stats" panel covers both.
        boost_stats = {}
        if bot and getattr(bot, "boost_manager", None):
            try:
                boost_stats = api_server._serialize_dict(
                    bot.boost_manager.get_stats_summary()
                )
            except Exception:
                boost_stats = {}

        # --- External Links ---
        asset_id = (
            api_server._active_cat.get("asset_id")
            or getattr(cfg, "CAT_ASSET_ID", "")
            or ""
        ).strip()
        ticker_id = (
            (
                api_server._active_cat.get("ticker_id")
                or getattr(cfg, "CAT_TICKER_ID", "")
                or ""
            )
            .strip()
            .upper()
        )
        if ticker_id and "_" not in ticker_id:
            ticker_id = f"{ticker_id}_XCH"
        dexie_orderbook = ""
        if ticker_id:
            parts = [p for p in ticker_id.split("_") if p]
            if len(parts) >= 2:
                dexie_orderbook = (
                    f"https://dexie.space/offers/{quote(parts[0])}/{quote(parts[1])}"
                )
        elif asset_id:
            dexie_orderbook = f"https://dexie.space/offers/{quote(asset_id)}/XCH"

        links = {
            "dexie_orderbook": dexie_orderbook,
            "tibetswap_pool": f"https://v2.tibetswap.io/pair/{quote(getattr(cfg, 'TIBET_PAIR_ID', '') or '')}"
            if getattr(cfg, "TIBET_PAIR_ID", "")
            else (
                f"https://v2.tibetswap.io/?asset_id={quote(asset_id)}"
                if asset_id
                else "https://v2.tibetswap.io"
            ),
            "spacescan_token": f"https://www.spacescan.io/cat2/{quote(asset_id)}"
            if asset_id
            else "",
        }

        return jsonify(
            api_server._serialize_dict(
                {
                    "settings": settings,
                    "market_health": market_health,
                    "wallet": wallet,
                    "coins": coins,
                    "performance": performance,
                    "sniper": sniper_stats,
                    "boost": boost_stats,
                    "spacescan_context": spacescan_context,
                    "fiat_prices": fiat_prices,
                    "links": links,
                    "cat_name": cfg.CAT_NAME if hasattr(cfg, "CAT_NAME") else "CAT",
                    "current_cat": api_server._active_cat,
                    "wallet_type": "sage",
                }
            )
        )
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/stats")
def api_stats():
    """Get trading statistics."""
    cfg = api_server.cfg
    try:
        stats = get_stats(cfg.CAT_ASSET_ID, since=api_server._get_run_history_cutoff())
        return jsonify(stats)
    except Exception:
        return api_server._api_exception(request.path)


@bp.route("/api/inventory")
def api_inventory():
    """Get current inventory state."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(bot.risk_manager.get_inventory_state())


@bp.route("/api/risk/spreads")
def api_risk_spreads():
    """Get current adjusted spreads for each side."""
    bot = api_server.bot
    cfg = api_server.cfg
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    buy_spread = bot.risk_manager.get_adjusted_spread("buy")
    sell_spread = bot.risk_manager.get_adjusted_spread("sell")

    return jsonify(
        {
            "buy_spread_bps": str(buy_spread * Decimal("10000")),
            "sell_spread_bps": str(sell_spread * Decimal("10000")),
            "buy_spread_pct": str(buy_spread * Decimal("100")),
            "sell_spread_pct": str(sell_spread * Decimal("100")),
            "dynamic_enabled": cfg.DYNAMIC_SPREAD_ENABLED,
            "inventory_enabled": cfg.INVENTORY_ENABLED,
        }
    )
