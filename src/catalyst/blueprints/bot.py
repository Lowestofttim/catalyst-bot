"""Bot lifecycle + status/state/events + diagnostics routes.

The high-traffic surface of the API: bot start/stop/shutdown, bot
state snapshot, the all-encompassing /api/status (3000+ lines),
the /api/events Server-Sent-Events stream, and the request-level
diagnostics counters.

These routes reach into many api_server helpers and the bot event
bus (api_server.events). The SSE route manages its own subscriber
queue attached to api_server.events.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict

from flask import Blueprint, Response, current_app, jsonify, request

import api_server
from config import cfg
from database import log_event, backup_database, get_stats
from super_log import slog
# Shared helper defined in the offers blueprint — used by /api/status.
from blueprints.offers import _build_fill_history_for_gui

try:
    from api_call_tracker import record as _record_api_call
except Exception:
    def _record_api_call(*args, **kwargs):
        return None


bp = Blueprint("bot", __name__)


def _api_server():
    """Return the currently loaded api_server module.

    Flask keeps route callables registered even if tests reload api_server;
    resolving the module lazily avoids reading stale bot/events state.
    """
    try:
        owner = current_app.config.get("_CATALYST_API_SERVER_MODULE")
        return owner or sys.modules.get("api_server", api_server)
    except RuntimeError:
        return sys.modules.get("api_server", api_server)


@bp.route("/api/events")
def api_events():
    """SSE endpoint — GUI connects here for real-time updates.

    Events are pushed as:
        data: {"type": "price_update", "data": {...}, "ts": 1234567890}

    The GUI listens with EventSource('/api/events') in JavaScript.
    """
    bot = api_server.bot
    def stream():
        q = api_server.events.subscribe()
        try:
            # Send initial state immediately
            if bot:
                initial = api_server._serialize_dict(bot.get_state())
                yield f"data: {json.dumps({'type': 'state', 'data': initial})}\n\n"

            while True:
                try:
                    msg = q.get(timeout=30)
                    # Serialize Decimals
                    serialized = api_server._serialize_dict(msg) if isinstance(msg, dict) else msg
                    yield f"data: {json.dumps(serialized, default=str)}\n\n"
                except queue.Empty:
                    # Send keepalive every 30 seconds
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            # Always unsubscribe — handles both clean disconnect (GeneratorExit)
            # and abrupt disconnect (WSGI server closes the generator).
            api_server.events.unsubscribe(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})

@bp.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    """Start the bot loop with pre-start validation (V1 parity).

    Checks wallet sync status, CAT config, and basic sanity
    before allowing the bot to start. V1 had validate_start().
    """
    server = _api_server()
    bot = server.bot
    cfg = server.cfg
    slog("GUI_ACTION", ">>> BUTTON: Start Bot")
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    if bot.is_running():
        return jsonify({"success": True, "status": "already_running"})

    # ---- Pre-start validation (V1 parity) ----
    warnings = []
    errors = []
    needs_coin_prep = False
    coin_prep_error = None
    tier_size_drift = []

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

    signing_block_reason = server._get_sage_signing_block_reason()
    if signing_block_reason:
        errors.append(signing_block_reason)

    # Check spread is sensible
    if cfg.SPREAD_BPS <= 0:
        errors.append("SPREAD_BPS is 0 or negative — bot would create bad offers")

    # G5b: warn when both sides are at zero offers
    _buy_slots = getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 0) or 0
    _sell_slots = getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 0) or 0
    if _buy_slots == 0 and _sell_slots == 0:
        warnings.append(
            "MAX_ACTIVE_BUY and MAX_ACTIVE_SELL are both 0 — "
            "bot will loop but create no offers"
        )

    # Check hard price limits. Only warn when BOTH are missing AND the
    # dynamic band isn't doing equivalent duty: DYNAMIC_LIMIT_PCT > 0 gives
    # us a percentage-based circuit breaker that already rejects extreme
    # oracle errors, so telling the user "circuit breakers disabled" when
    # the dynamic guard is active is misleading.
    hard_min = getattr(cfg, "HARD_MIN_PRICE_XCH", Decimal("0"))
    hard_max = getattr(cfg, "HARD_MAX_PRICE_XCH", Decimal("0"))
    dynamic_limit = getattr(cfg, "DYNAMIC_LIMIT_PCT", Decimal("0"))
    if (hard_min <= 0 or hard_max <= 0) and dynamic_limit <= 0:
        warnings.append(
            "No price circuit breakers configured — "
            "set HARD_MIN_PRICE_XCH/HARD_MAX_PRICE_XCH or DYNAMIC_LIMIT_PCT"
        )

    # Tier-size drift gate: refuse to start if the on-disk coin
    # designations don't match the current Smart Settings tier sizes.
    # Without this, the first cycle posts offers from coins that won't
    # fit any tier cleanly (the SBX→MZ residue case from yesterday) and
    # the wallet can void them all simultaneously when an in-flight
    # split TX confirms. Coin prep's reclassify pass should have caught
    # this — if drift survives that, something's wrong and the bot
    # shouldn't trade until it's fixed.
    try:
        from coin_manager import check_tier_size_drift_standalone
        _drift = check_tier_size_drift_standalone(
            low_ratio=0.50, high_ratio=2.00, min_sample=2
        ) or []
        if _drift:
            tier_size_drift = _drift
            needs_coin_prep = True
            _summary = ", ".join(
                f"{f['side']}/{f['tier']}={f['ratio']}× (n={f['coin_count']})"
                for f in _drift
            )
            errors.append(
                "Coin tier sizes don't match Smart Settings — "
                "re-run Coin Prep before starting. Drift: " + _summary
            )
            coin_prep_error = errors[-1]
    except Exception as _drift_err:
        warnings.append(f"Tier-drift gate skipped: {_drift_err}")

    # Block start on critical errors
    if errors:
        payload = {"success": False, "status": "error", "errors": errors, "warnings": warnings}
        if needs_coin_prep:
            payload.update({
                "needs_coin_prep": True,
                "reason": "tier_size_drift",
                "error": coin_prep_error or errors[-1],
                "message": coin_prep_error or errors[-1],
                "tier_size_drift": tier_size_drift,
            })
        return jsonify(payload), 400

    server._reset_runtime_session_stats()

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
    server._fresh_start_clear()
    server.events.emit("bot_control", {"action": "started"})
    result = {"success": True, "status": "started"}
    if warnings:
        result["warnings"] = warnings
    return jsonify(result)

@bp.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    """Stop the bot loop."""
    server = _api_server()
    bot = server.bot
    slog("GUI_ACTION", ">>> BUTTON: Stop Bot")
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    bot.stop()
    server.events.emit("bot_control", {"action": "stopped"})
    return jsonify({"status": "stopped"})

@bp.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Full shutdown — stop bot, cancel offers, kill server.

    Called by the GUI 'Shutdown' button or when the user wants
    to cleanly exit everything.
    """
    bot = api_server.bot
    try:
        cancel_first = bool((request.get_json(silent=True) or {}).get("cancel_offers", False))
    except Exception:
        cancel_first = False

    def _do_shutdown():
        """Run shutdown sequence in background thread so the HTTP response returns first."""
        time.sleep(0.5)  # Let the response reach the browser

        print("\n🛑 SHUTDOWN sequence starting...", flush=True)

        # 0. Kill coin prep subprocess if it's still running
        try:
            if api_server._coin_prep_proc is not None and api_server._coin_prep_proc.poll() is None:
                prep_pid = api_server._coin_prep_proc.pid
                print(f"   Stopping coin prep worker (PID: {prep_pid})...", flush=True)
                api_server._coin_prep_proc.terminate()
                try:
                    api_server._coin_prep_proc.wait(timeout=5)
                except Exception:
                    api_server._coin_prep_proc.kill()
                    api_server._coin_prep_proc.wait(timeout=3)
                print("   ✅ Coin prep worker stopped", flush=True)
                api_server._coin_prep_proc = None
                api_server._coin_prep_state["running"] = False
                api_server._coin_prep_state["error"] = "Stopped by shutdown"
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

            # Cancel TXs submitted to mempool take a few seconds to leave the
            # wallet's open-offer view. cancel_all keeps DB status as "open"
            # for those (so a racing fill isn't misclassified) — but if we
            # exit immediately the DB stays stale, and the next startup
            # shows offers that no longer exist on-chain. Poll briefly until
            # the wallet confirms they're gone, then write through to the DB
            # so the next session boots with a clean book.
            try:
                import time as _t
                from database import get_open_offers, update_offer_status
                submitted_tids = [
                    tid for tid, r in (result or {}).items()
                    if r and r.get("success")
                ]
                deadline = _t.time() + 30
                final_open: list = []
                while _t.time() < deadline:
                    open_buys, open_sells, _ = bot.offer_manager.sync_from_wallet()
                    final_open = open_buys + open_sells
                    if not final_open:
                        break
                    _t.sleep(2)

                # Anything in our submitted set that's no longer in the wallet
                # has cleared on-chain — mark it cancelled in the DB.
                still_open_tids = {o.get("trade_id") for o in final_open}
                cleared = [tid for tid in submitted_tids if tid not in still_open_tids]
                for tid in cleared:
                    try:
                        update_offer_status(tid, "cancelled")
                    except Exception:
                        pass
                if cleared:
                    print(f"   ✅ {len(cleared)} cancellation(s) confirmed; "
                          f"DB marked cancelled", flush=True)
                if final_open:
                    print(f"   ⚠️ {len(final_open)} offer(s) still pending after "
                          f"30s — next startup reconcile will catch them",
                          flush=True)
            except Exception as e:
                print(f"   ⚠️ Cancel settle wait failed: {e}", flush=True)

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

@bp.route("/api/bot/state")
def api_bot_state():
    """Get full bot state (for GUI polling fallback)."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    state = bot.get_state()

    # When the bot object exists but trading is stopped, coin_manager/risk state can
    # still reflect a cold in-memory snapshot from startup. Backfill from the same
    # safe RPC health/count helpers used by other read-only endpoints so the GUI
    # does not show a misleading all-zero stopped state.
    if not state.get("running", False):
        try:
            state["chia_health"] = api_server._get_health_snapshot()
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
                    cat_decimals = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
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
                    cat_wallet_id = api_server._active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", 2)
                    cat_free = int(get_spendable_coin_count(cat_wallet_id) or 0)

                    coins["xch_coins"] = xch_free
                    coins["cat_coins"] = cat_free
                    coins["xch_total_coins"] = xch_free + int(coins.get("xch_locked_coins", 0) or 0)
                    coins["cat_total_coins"] = cat_free + int(coins.get("cat_locked_coins", 0) or 0)

                state["coins"] = coins
        except Exception:
            pass

    return jsonify(api_server._serialize_dict(state))

@bp.route("/api/status")
def api_status():
    """Main GUI polling endpoint — assembles full state in the format the GUI expects.

    Returns a nested dict with: running, stats, balances, pricing, offers, logs,
    chia_health, wallet_type, current_cat. This is polled every 5 seconds.
    """
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        from database import get_recent_events, get_open_offers

        # If bot hasn't been created yet, return minimal static state.
        # DO NOT make live network calls during polling — /api/status is called
        # every 5 seconds and side effects here cause wallet RPC contention.
        # The /api/dashboard endpoint provides fresh data on page load.
        if not bot:
            xch_bal = {"spendable": 0, "total": 0}
            cat_bal = {"spendable": 0, "total": 0}

            # Pre-start pricing cache. Without this, every /api/status poll
            # (every 5 s) fires a live TibetSwap and Dexie fetch AND writes
            # a price_lookup / price_found log row — opening the dashboard
            # before the bot started generated ~720 log rows per hour and
            # put pointless load on the oracles. Cache the lookup result
            # for 60 s so log entries and HTTP calls drop to 1 per minute.
            global _prebot_price_cache  # noqa: PLW0603
            if "_prebot_price_cache" not in globals():
                _prebot_price_cache = {"fetched_at": 0.0, "pricing": None,
                                        "asset_id": ""}

            pricing = {"bid": 0, "mid": 0, "ask": 0}
            asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
            cat_dec = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)

            _cache_ttl = 60.0
            _now_ts = time.time()
            _use_cache = (
                _prebot_price_cache.get("pricing") is not None
                and _prebot_price_cache.get("asset_id") == asset_id
                and (_now_ts - _prebot_price_cache.get("fetched_at", 0.0)) < _cache_ttl
            )
            if _use_cache:
                pricing = dict(_prebot_price_cache["pricing"])
                # Skip all HTTP fetches and logging below for this poll —
                # the cache is fresh enough for a pre-bot dashboard.
                asset_id = ""

            if asset_id:
                print(f"[STATUS] Pricing lookup: asset_id={asset_id!r}, decimals={cat_dec}", flush=True)
                log_event("info", "price_lookup", f"Looking up price for {api_server._active_cat.get('name', 'unknown')}")
            if asset_id:
                import requests as _req
                mid = 0

                # --- Try TibetSwap ---
                try:
                    _record_api_call("tibetswap", "/pairs")
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
                        ticker_id = api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "") or ""
                        # Dexie ticker format is "{CAT}_XCH" e.g. "SBX_XCH" (V1 confirmed)
                        if ticker_id and "_" not in ticker_id:
                            ticker_id = f"{ticker_id}_XCH"
                        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
                        if ticker_id:
                            _record_api_call("dexie", "/v2/prices/tickers")
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

            # Cache the freshly-fetched result so the next ~60 s of polls
            # serves this response without refetching. We stash the
            # upstream asset_id to invalidate the cache when the operator
            # switches CATs. If pricing is empty (no price found) we also
            # cache that so a dead pair doesn't get retried every 5 s.
            if not _use_cache:
                asset_id_current = (
                    api_server._active_cat.get("asset_id")
                    or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
                    or ""
                )
                try:
                    _prebot_price_cache["pricing"] = dict(pricing)
                    _prebot_price_cache["asset_id"] = asset_id_current
                    _prebot_price_cache["fetched_at"] = _now_ts
                except Exception:
                    pass

            # Fetch open offers from wallet RPC — uses the same normalize path
            # as the bot (get_all_offers → classify_offers_from_list) so prices
            # and amounts are properly extracted before Start Bot.
            offers_buy_pre = []
            offers_sell_pre = []
            try:
                from wallet import get_all_offers, classify_offers_from_list
                asset_id_for_offers = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
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
                cat_wid_coins = api_server._active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
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

            cat_name = api_server._active_cat.get("name") or (cfg.CAT_NAME if hasattr(cfg, "CAT_NAME") else "")
            return jsonify({
                "running": False,
                "stats": {"loop_count": 0, "uptime_seconds": 0, "last_loop_time": 0,
                           "total_fills": 0, "errors": 0},
                "balances": {"xch": xch_bal, "cat": cat_bal},
                "pricing": api_server._decimal_safe(pricing),
                "offers": {
                    "buy": offers_buy_pre,
                    "sell": offers_sell_pre,
                    "history": _build_fill_history_for_gui(asset_id, limit=20),
                },
                "coin_tracking": coin_tracking_pre,
                "logs": [],
                "chia_health": api_server._get_health_snapshot(),
                "wallet_type": api_server.get_wallet_type(),
                "current_cat": {
                    "name": cat_name,
                    "asset_id": asset_id,
                    "wallet_id": api_server._active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", None),
                    "decimals": api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3),
                    "ticker_id": api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", None),
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
        xch_bal = coins_data.get("xch_balance") or {}
        cat_bal = coins_data.get("cat_balance") or {}
        balances_out = {
            "xch": {
                "spendable": api_server._safe_float(xch_bal.get("spendable") or xch_bal.get("free", 0)),
                "total": api_server._safe_float(xch_bal.get("total", 0)),
            },
            "cat": {
                "spendable": api_server._safe_float(cat_bal.get("spendable") or cat_bal.get("free", 0)),
                "total": api_server._safe_float(cat_bal.get("total", 0)),
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
                    confirmed = api_server._safe_float(wb.get("confirmed_wallet_balance", 0))
                    spendable = api_server._safe_float(wb.get("spendable_balance", 0))
                    balances_out["xch"]["total"] = confirmed / 1e12
                    balances_out["xch"]["spendable"] = spendable / 1e12
            except Exception:
                pass

        if balances_out["cat"]["total"] == 0:
            try:
                from wallet import get_wallet_balance
                # Use actively selected CAT wallet_id, fall back to config
                cat_wallet_id = api_server._active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
                cat_result = get_wallet_balance(cat_wallet_id)
                if cat_result and cat_result.get("success"):
                    wb = cat_result.get("wallet_balance") or {}
                    cat_decimals = api_server._active_cat.get("decimals") or getattr(cfg, 'CAT_DECIMALS', 3)
                    confirmed = api_server._safe_float(wb.get("confirmed_wallet_balance", 0))
                    spendable = api_server._safe_float(wb.get("spendable_balance", 0))
                    balances_out["cat"]["total"] = confirmed / (10 ** cat_decimals)
                    balances_out["cat"]["spendable"] = spendable / (10 ** cat_decimals)
            except Exception:
                pass

        # --- Pricing ---
        price_info = bot.get_price_info() if hasattr(bot, "get_price_info") else {}
        mid = api_server._safe_float(raw.get("mid_price", 0))
        bid = api_server._safe_float(price_info.get("last_quoted_buy", 0))
        ask = api_server._safe_float(price_info.get("last_quoted_sell", 0))

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
                asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
                cat_dec = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
                if asset_id:
                    _record_api_call("tibetswap", "/pairs")
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
                        _buy_bps = api_server._safe_float(health.get("buy_spread_bps", 0))
                        _sell_bps = api_server._safe_float(health.get("sell_spread_bps", 0))
                        if _buy_bps > 0 and _sell_bps > 0:
                            bid = mid * (1 - _buy_bps / 10000)
                            ask = mid * (1 + _sell_bps / 10000)
                            _got_spread = True
            except Exception:
                pass

            # Fallback: if risk manager didn't provide spread, use config
            if not _got_spread:
                _base_bps = api_server._safe_float(
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
                asset_id_for_classify = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
                cat_decimals = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
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
                            "created_at": tr.get("created_at_time") or api_server._sage_ts_to_iso(tr.get("creation_timestamp")),
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
                        print("  [DEXIE] ⚠️ NO offers have dexie_id in DB — "
                              "Dexie posting may have failed in previous sessions", flush=True)
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
        cat_name = api_server._active_cat.get("name") or getattr(cfg, "CAT_NAME", "CAT")
        cat_dec = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
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
            api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", ""),
            limit=50,
        )
        if not history_out:
            # F46 (2026-04-09): the fallback path returns raw fill rows
            # which lack the `status`/`price` keys the GUI expects.
            # Normalise the shape here so updateHistory() doesn't throw.
            raw_recent = api_server._serialize_list(fills_data.get("recent") or [])
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
                    norm["cat_name"] = api_server._active_cat.get("name") or getattr(cfg, "CAT_NAME", "") or "CAT"
                history_out.append(norm)
        offers_out = {
            "buy": api_server._serialize_list(enriched_buy),
            "sell": api_server._serialize_list(enriched_sell),
            "history": history_out,
        }

        # --- Logs (latest 100 events, filtered to current session) ---
        try:
            from database import get_events_since, get_recent_events
            cutoff = api_server._session_start_time
            if api_server._logs_cleared_at and (not cutoff or api_server._logs_cleared_at > cutoff):
                cutoff = api_server._logs_cleared_at
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
                "cat_locked_amount": f"{db_coin_summary.get('cat_locked_mojos', 0) / (10 ** ((api_server._active_cat.get('decimals') or getattr(cfg, 'CAT_DECIMALS', 3)))):.2f}",
                "xch_topup_pool_amount": inv.get("xch_reserve_total", "0"),
                "cat_topup_pool_amount": inv.get("cat_reserve_total", "0"),
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
                "xch_topup_pool_amount": inv.get("xch_reserve_total", "0"),
                "cat_topup_pool_amount": inv.get("cat_reserve_total", "0"),
            }

        # If coin tracking is all zeros (bot hasn't run), query Sage directly.
        # Valid Sage filter_mode values: all, selectable, owned, spent, clawback
        # "selectable" = free/spendable coins, "owned" = free + offer-locked
        # Locked = owned - selectable
        if coin_tracking["xch_free"] == 0 and coin_tracking["xch_total"] == 0:
            try:
                from wallet import rpc as wallet_rpc
                cat_asset_id = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")

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

                cat_dec = api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)

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
                    print("[STATUS] Coin tracking (Sage RPC):", flush=True)
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
                risk_out = api_server._serialize_dict(dict(inv))
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
            "chia_health": api_server._get_health_snapshot() if not raw.get("running", False) else (raw.get("chia_health") or {}),
            "wallet_type": raw.get("wallet_type", "sage"),
            "current_cat": {
                "name": api_server._active_cat.get("name") or (cfg.CAT_NAME if hasattr(cfg, "CAT_NAME") else ""),
                "asset_id": api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""),
                "wallet_id": api_server._active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", None),
                "decimals": api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3),
                "ticker_id": api_server._active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", None),
            },
            # Liquidity mode block — used by the GUI to show the mode badge
            # and, in single-sided modes, a "fuel parked" banner when the
            # active side can no longer fund a single offer at the smallest
            # tier size. Two-sided mode reports parked=False.
            "liquidity": api_server._build_liquidity_status_block(raw),
        }

        return jsonify(api_server._serialize_dict(result))
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/diagnostics/runtime")
def api_runtime_diagnostics():
    """Return the live runtime-monitor snapshot."""
    bot = api_server.bot
    if not bot:
        return jsonify({"enabled": False, "status": "idle", "recent_actions": [], "recent_findings": []})
    try:
        raw = bot.get_state() or {}
        return jsonify(api_server._serialize_dict(raw.get("diagnostics") or {}))
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/diagnostics/api-stats")
def api_diagnostics_api_stats():
    """F45 (2026-04-08): unified usage stats for all 3 external APIs.

    Returns counters for Spacescan (paid call budget), Coinset (hit
    rate vs wallet RPC fallback) and Dexie (post queue, v3 cache),
    plus the live circuit-breaker / rate-limit cooldown status for
    each one. Drives the diagnostics panel so the operator can see
    at a glance which API is doing the work and how much budget is
    left before the bot has to fall back.
    """
    bot = api_server.bot
    payload: Dict[str, Any] = {
        "spacescan": {"available": False},
        "coinset":   {"available": False},
        "dexie":     {"available": False},
    }

    # Pull the centralized tracker once. Lets us merge "direct" calls
    # (calls bypassing per-service managers — e.g. Smart Settings hitting
    # api.dexie.space directly) into each service panel and surface
    # CoinGecko + GitHub which have no other counter source.
    try:
        from api_call_tracker import (
            get_count as _tracker_get_count,
            get_endpoint_breakdown as _tracker_endpoints,
            get_last_call_ago_secs as _tracker_last_ago,
        )
    except Exception:
        _tracker_get_count = lambda _s: 0
        _tracker_endpoints = lambda _s: {}
        _tracker_last_ago = lambda _s: None

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

    # Merge any spacescan calls recorded via the centralized tracker
    # (currently market_data_collector routes through spacescan.record_external_call,
    # so this is mostly informational, but still keep parity for future sites).
    if payload["spacescan"].get("available"):
        _tracked = int(_tracker_get_count("spacescan"))
        if _tracked:
            payload["spacescan"]["direct_calls"] = _tracked
            payload["spacescan"]["direct_calls_by_endpoint"] = _tracker_endpoints("spacescan")

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

    # Merge "direct" Coinset calls (tx_fees fee-estimate, anything not
    # going through coinset_client). These come from the centralized
    # tracker so the operator sees a complete total.
    if payload["coinset"].get("available"):
        _direct = int(_tracker_get_count("coinset"))
        if _direct:
            payload["coinset"]["direct_calls"] = _direct
            payload["coinset"]["direct_calls_by_endpoint"] = _tracker_endpoints("coinset")
            payload["coinset"]["api_calls_total"] = (
                payload["coinset"].get("api_calls_total", 0) + _direct
            )

    # Add mempool watcher's Coinset API call count (separate HTTP client)
    try:
        import mempool_watcher as _mw
        _watcher = getattr(_mw, "_watcher_instance", None)
        if _watcher:
            _mw_coinset = getattr(_watcher, "_coinset_api_calls", 0)
            _mw_tibet = getattr(_watcher, "_tibet_api_calls", 0)
            _mw_hits = int(getattr(_watcher, "_fill_warn_hits", 0) or 0)
            _mw_misses = int(getattr(_watcher, "_fill_warn_misses", 0) or 0)
            _mw_total = _mw_hits + _mw_misses
            # Add to coinset total
            if payload["coinset"].get("available"):
                payload["coinset"]["mempool_watcher_calls"] = _mw_coinset
                payload["coinset"]["api_calls_total"] = (
                    payload["coinset"].get("api_calls_total", 0) + _mw_coinset
                )
                payload["coinset"]["fill_warn_hits"] = _mw_hits
                payload["coinset"]["fill_warn_misses"] = _mw_misses
                payload["coinset"]["fill_warn_hit_rate_pct"] = (
                    round(100.0 * _mw_hits / _mw_total, 2) if _mw_total else None
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

    # Merge "direct" Dexie calls (Smart Settings, market intel, deposit
    # advisor, fill verification, doctor, sage_node, etc.) — these
    # bypass dexie_manager so they're invisible to the manager's stats.
    if payload["dexie"].get("available"):
        _direct = int(_tracker_get_count("dexie"))
        if _direct:
            payload["dexie"]["direct_calls"] = _direct
            payload["dexie"]["direct_calls_by_endpoint"] = _tracker_endpoints("dexie")

    # --- Splash (P2P offer broadcast) ---------------------------------
    # Splash has its own /api/splash/stats endpoint, but callers of the
    # unified diagnostics modal should see it in one place alongside the
    # other external APIs. Mirror the field shape used by Dexie so the
    # frontend can render the same "posts / failed / queue" panel.
    try:
        if bot is not None and getattr(bot, "splash_manager", None):
            sp_stats = bot.splash_manager.get_stats() or {}
            sp_health = {}
            try:
                sp_health = bot.splash_manager.check_health() or {}
            except Exception:
                sp_health = {}
            sp_receive = {}
            try:
                sp_receive = bot.get_splash_receive_stats() or {}
            except Exception:
                sp_receive = {}
            payload["splash"] = {
                "available": True,
                "total_posted": int(sp_stats.get("total_posted", 0) or 0),
                "total_failed": int(sp_stats.get("total_failed", 0) or 0),
                "total_skipped": int(sp_stats.get("total_skipped", 0) or 0),
                "queue_size": int(sp_stats.get("queue_size", 0) or 0),
                "fingerprints_cached": int(sp_stats.get("fingerprints_cached", 0) or 0),
                "healthy": bool(sp_stats.get("healthy", True)),
                "consecutive_failures": int(sp_stats.get("consecutive_failures", 0) or 0),
                "health": sp_health,
                "receive": sp_receive,
            }
        else:
            payload["splash"] = {"available": False}
    except Exception as e:
        payload["splash"] = {"available": False, "error": str(e)}

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

    # Merge "direct" TibetSwap calls (token discovery, /pairs lookups
    # from cat_resolver, smart_defaults, market intel, etc.) — these
    # bypass amm_monitor so they're invisible to its stats. Always
    # surface the counter; if amm_monitor isn't running we still want
    # the direct counts to show up.
    _tibet_direct = int(_tracker_get_count("tibetswap"))
    if _tibet_direct:
        if not isinstance(payload.get("tibetswap"), dict):
            payload["tibetswap"] = {"available": False}
        # Even when amm_monitor is offline (available=False), expose
        # the direct calls so the modal can render *something*.
        payload["tibetswap"]["direct_calls"] = _tibet_direct
        payload["tibetswap"]["direct_calls_by_endpoint"] = _tracker_endpoints("tibetswap")
        # Promote to "available" if at least one direct call landed —
        # the panel becomes meaningful even without the AMM monitor.
        if not payload["tibetswap"].get("available"):
            payload["tibetswap"]["available_via_direct"] = True

    # --- CoinGecko (XCH/USD price) ------------------------------------
    # Used by Smart Settings to display USD-denominated values and to
    # detect systemic XCH moves. No paid tier; cached 5 minutes.
    _cg_calls = int(_tracker_get_count("coingecko"))
    _cg_last_ago = _tracker_last_ago("coingecko")
    payload["coingecko"] = {
        "available": _cg_calls > 0,
        "calls": _cg_calls,
        "by_endpoint": _tracker_endpoints("coingecko"),
        "last_call_ago_secs": _cg_last_ago,
    }

    # --- GitHub (release polls) ---------------------------------------
    # Sage version check (api_server) + Splash binary download
    # (splash_setup). Both are cached 6h+ so volume is low — we surface
    # them so the operator can confirm the update path is alive.
    _gh_calls = int(_tracker_get_count("github"))
    _gh_last_ago = _tracker_last_ago("github")
    payload["github"] = {
        "available": _gh_calls > 0,
        "calls": _gh_calls,
        "by_endpoint": _tracker_endpoints("github"),
        "last_call_ago_secs": _gh_last_ago,
    }

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

@bp.route("/api/bot/price")
def api_bot_price():
    """Get current price info."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    return jsonify(api_server._serialize_dict(bot.get_price_info()))
