"""Offers, fills, P&L, and reset routes.

Covers: active-offer listing, cancel flows (single, all, orphan cleanup),
fill history (raw, classified, arb-wallets, market-fill-intel), P&L
summary + reset, and the full/offer-history reset buttons.

Shared cancel-all progress state lives on the api_server module
(api_server._cancel_all_state) so the shutdown path and other modules
can still inspect it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event, get_stats
from super_log import slog


bp = Blueprint("offers", __name__)


def _build_fill_history_for_gui(asset_id: str, limit: int = 20) -> list:
    """Return DB-backed fill history in the shape the Offers history tab expects."""
    if not asset_id:
        return []

    history_by_trade_id = {}
    since_cutoff = api_server._get_run_history_cutoff()
    try:
        from database import get_fills
        fills = get_fills(
            cat_asset_id=asset_id,
            since=since_cutoff,
            limit=max(limit * 3, 60),
        )
    except Exception:
        fills = []

    cat_name = api_server._active_cat.get("name") or getattr(cfg, "CAT_NAME", "") or "CAT"

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
            "age": api_server._history_age_label(filled_at),
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

@bp.route("/api/offers")
def api_offers():
    """Get current open offers."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    open_buys, open_sells, _ = bot.offer_manager.sync_from_wallet()

    return jsonify({
        "buys": api_server._serialize_offers(open_buys),
        "sells": api_server._serialize_offers(open_sells),
        "buy_count": len(open_buys),
        "sell_count": len(open_sells),
    })

@bp.route("/api/offers/cancel_all/status")
def api_cancel_all_status():
    """Return the live cancel-all progress state for the GUI."""
    bot = api_server.bot
    return jsonify({"success": True, **_get_cancel_all_state()})

@bp.route("/api/offers/open_count")
def api_open_offer_count():
    """Return the number of still-active offers in the wallet.

    Used by the shutdown flow to verify cancels actually confirmed
    on-chain before proceeding with app exit.
    """
    bot = api_server.bot
    try:
        from database import get_open_offers
        open_offers = get_open_offers()
        return jsonify({"success": True, "open_count": len(open_offers)})
    except Exception as e:
        return jsonify({"success": False, "open_count": -1, "error": str(e)})

@bp.route("/api/offers/cancel_all", methods=["POST"])
def api_cancel_all():
    """Cancel all open offers when the bot is not actively managing the book."""
    bot = api_server.bot
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
            api_server.events.emit("offers_cancelled", {"count": cancelled, "reason": "manual_cancel_all"})
            # Reset gap closer state if active (cancel_all includes gap-closer offers)
            if bot.boost_manager._boost_active:
                bot.boost_manager._boost_active = False
                bot.boost_manager._active_boost_ids.clear()
                bot.boost_manager._boost_mid_price = Decimal("0")
                bot.boost_manager._gap_spread_bps = 0
                bot.boost_manager._convergence_factor = Decimal("1.0")
                api_server.events.emit("boost", {"active": False})
        except Exception as e:
            _set_cancel_all_state(
                running=False,
                complete=False,
                error=str(e),
                phase="error",
                finished_at=datetime.now(timezone.utc).isoformat(),
                message=f"Cancel all failed: {e}",
            )
            return api_server._api_error(e, request.path)
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
                    api_server.events.emit("offers_cancelled", {"count": _w_cancelled, "reason": "manual_cancel_all"})
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
                                api_server.events.emit("boost", {"active": False})
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
            return api_server._api_error(e, request.path)

    return jsonify({
        "success": True,
        "cancelled": cancelled,
        "failed": failed,
    })

@bp.route("/api/offers/cleanup_orphans", methods=["POST"])
def api_cleanup_orphans():
    """Find and cancel wallet offers not tracked by the bot.

    These are "ghost" offers — the bot tried to cancel them but the
    on-chain cancel failed. They're still live on Dexie but the bot
    doesn't know about them. This endpoint finds and cancels them.
    """
    bot = api_server.bot
    slog("GUI_ACTION", ">>> BUTTON: Cleanup Orphaned Offers")

    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        result = bot.cleanup_orphaned_offers()
        return jsonify({"success": True, **result})
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/offers/cancel", methods=["POST"])
def api_cancel_offer():
    """Cancel a specific offer.

    Body: {"trade_id": "0x..."}
    """
    bot = api_server.bot
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
        return api_server._api_error(e, request.path)
    # cancel_offers returns a dict; surface any storm-protection refusal
    if isinstance(result, dict) and result.get("error"):
        return jsonify({"success": False, "trade_id": trade_id, **result}), 400
    return jsonify({"success": True, "status": "cancelled", "trade_id": trade_id})

@bp.route("/api/fills")
def api_fills():
    """Get recent fill history."""
    bot = api_server.bot
    cfg = api_server.cfg
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    limit = request.args.get("limit", 20, type=int)
    from database import get_fills
    fills = get_fills(
        cat_asset_id=cfg.CAT_ASSET_ID,
        limit=limit,
        since=api_server._get_run_history_cutoff(),
    )
    return jsonify({"fills": api_server._serialize_list(fills)})

@bp.route("/api/fills/classified")
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
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        from database import get_connection
        from fill_classifier import FillType

        classification_filter = request.args.get("type") or None
        side_filter           = request.args.get("side") or None
        limit                 = min(request.args.get("limit", 50, type=int), 200)
        offset                = request.args.get("offset", 0, type=int)
        since                 = request.args.get("since") or api_server._get_run_history_cutoff() or None

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
            "fills":           api_server._serialize_list(fills),
            "total":           total,
            "limit":           limit,
            "offset":          offset,
            "summary":         summary,
            "sweep_pending":   sweep_pending,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/fills/arb-wallets")
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
    bot = api_server.bot
    cfg = api_server.cfg
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
            ph = str(row["taker_puzzle_hash"]).lower().removeprefix("0x")
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
        return api_server._api_error(e, request.path)

@bp.route("/api/market/fill-intel")
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
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        from database import get_connection

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
                    from datetime import datetime, timedelta
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
        return api_server._api_error(e, request.path)

@bp.route("/api/offers/diagnostic")
def api_offers_diagnostic():
    """Compare the live wallet book to the DB book and summarize coin safety."""
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        from database import get_connection
        conn = get_connection()
        asset_id = api_server._active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")

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

        return jsonify(api_server._serialize_dict({
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
        return api_server._api_error(e, request.path)

@bp.route("/api/fills/purge", methods=["POST"])
def api_purge_fills():
    """Purge all fill records to reset inventory position.

    Use when false fills have corrupted the position calculation
    (e.g. circuit breaker tripping on fake data from testing).
    Clears fills table + round_trips table + resets risk manager state.
    """
    bot = api_server.bot
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
        return api_server._api_error(e, request.path)

@bp.route("/api/pnl/reset-preview", methods=["GET"])
def api_pnl_reset_preview():
    """Peek at what a Reset Stats / Start Fresh action would clear.

    Used by the pre-prep confirm modal and the PnL Reset button to decide
    whether to SHOW the confirm (there's data to lose) or skip straight to
    the destructive action (nothing to preserve anyway). Returns zero
    counts instead of erroring if the tables don't exist yet.
    """
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        from database import get_connection
        conn = get_connection()
        fills = 0
        round_trips = 0
        try:
            fills = int((conn.execute(
                "SELECT COUNT(*) as cnt FROM fills").fetchone()["cnt"]) or 0)
        except Exception:
            fills = 0
        try:
            if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='round_trips'"
            ).fetchone():
                round_trips = int((conn.execute(
                    "SELECT COUNT(*) as cnt FROM round_trips").fetchone()["cnt"]) or 0)
        except Exception:
            round_trips = 0

        # Count terminal-state offer rows so the pre-prep modal can decide
        # whether to offer the "clear offer history" checkbox.
        offer_history_rows = 0
        try:
            offer_history_rows = int((conn.execute(
                "SELECT COUNT(*) AS cnt FROM offers "
                "WHERE status IN ('cancelled', 'filled', 'expired') "
                "   OR lifecycle_state IN ('cancelled', 'filled', 'expired', "
                "                          'phantom_rejected', 'user_cancelled')"
            ).fetchone()["cnt"]) or 0)
        except Exception:
            offer_history_rows = 0

        realised_pnl_xch = Decimal("0")
        net_position_cat = Decimal("0")
        try:
            # Same data sources as /api/pnl — get_stats aggregates fills
            # to realised_pnl, risk_manager.get_inventory_state reports
            # the live net position.
            stats = api_server.get_stats(cfg.CAT_ASSET_ID, since=api_server._get_run_history_cutoff())
            realised_pnl_xch = Decimal(str(stats.get("realised_pnl_xch", 0) or 0))
        except Exception:
            realised_pnl_xch = Decimal("0")
        try:
            if bot and getattr(bot, "risk_manager", None):
                inv = bot.risk_manager.get_inventory_state() or {}
                net_position_cat = Decimal(str(inv.get("net_position_cat", 0) or 0))
        except Exception:
            net_position_cat = Decimal("0")

        has_pnl_data = (fills > 0 or round_trips > 0
                        or realised_pnl_xch != 0 or net_position_cat != 0)
        has_data = bool(has_pnl_data or offer_history_rows > 0)
        return jsonify({
            "success": True,
            "has_data": has_data,
            "has_pnl_data": bool(has_pnl_data),
            "fills": fills,
            "round_trips": round_trips,
            "realised_pnl_xch": str(realised_pnl_xch),
            "net_position_cat": str(net_position_cat),
            "offer_history_rows": offer_history_rows,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/pnl/reset", methods=["POST"])
def api_pnl_reset():
    """Explicit user-initiated full reset of trading stats.

    Equivalent to the "Start Fresh" path without touching coin prep — used
    by the Reset Stats button on the PnL tab. Requires a confirmation
    token in the request body so a stray curl / mis-clicked fetch can't
    wipe history. The token is just a correctness gate, not security —
    the whole API is bound to 127.0.0.1.

    Clears: fills, round_trips, price_history, inventory_snapshots,
            position baseline, runtime session stats.
    Preserves: coins, open offers, prepped pool — nothing on-chain is
               touched.
    """
    bot = api_server.bot
    slog("GUI_ACTION", ">>> BUTTON: Reset Trading Stats")
    try:
        payload = request.get_json(silent=True) or {}
        if (payload.get("confirm") or "").strip().upper() != "RESET":
            return jsonify({
                "success": False,
                "error": "confirmation_required",
                "message": "Send {confirm: 'RESET'} to confirm the wipe.",
            }), 400

        summary = api_server._reset_fresh_run_session(
            clear_coins=False,
            clear_price_history=True,
            clear_inventory=True,
            cancel_open_offers=False,
            preserve_history=False,
            reason="pnl_reset_stats",
        )
        return jsonify({
            "success": True,
            "message": (f"Cleared {summary.get('fills_cleared', 0)} fills "
                        f"and {summary.get('round_trips_cleared', 0)} round-trips. "
                        f"Position baseline reset to zero."),
            **api_server._serialize_dict(summary),
        })
    except Exception as e:
        log_event("warning", "pnl_reset_failed",
                  f"Explicit PnL reset failed: {e}")
        return api_server._api_error(e, request.path)

@bp.route("/api/reset/offer-history", methods=["POST"])
def api_reset_offer_history():
    """Delete terminal-state offer rows (cancelled / filled / expired /
    phantom_rejected) from the DB. Keeps open offers intact.

    Use case: user wants a clean offer-history view without touching
    P&L counters or coin state. After a long run the `offers` table can
    accumulate thousands of cancelled rows (the 2026-04-21 repair found
    1,880 on one DB) which bloats diagnostic queries and the GUI.

    Gated on bot-not-running to avoid racing with live cancel/expiry
    writes. Requires ``{confirm: 'RESET'}`` body token.
    """
    bot = api_server.bot
    slog("GUI_ACTION", ">>> BUTTON: Clear Offer History")
    try:
        if bot and bot.is_running():
            return jsonify({
                "success": False,
                "error": "bot_running",
                "message": "Stop the bot before clearing offer history.",
            }), 409

        payload = request.get_json(silent=True) or {}
        if (payload.get("confirm") or "").strip().upper() != "RESET":
            return jsonify({
                "success": False,
                "error": "confirmation_required",
                "message": "Send {confirm: 'RESET'} to confirm the wipe.",
            }), 400

        conn = get_connection()
        try:
            # Count before delete for the summary.
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM offers "
                "WHERE status IN ('cancelled', 'filled', 'expired') "
                "   OR lifecycle_state IN ('cancelled', 'filled', 'expired', "
                "                          'phantom_rejected', 'user_cancelled')"
            ).fetchone()
            n_before = int((before["n"] if before else 0) or 0)

            cur = conn.execute(
                "DELETE FROM offers "
                "WHERE status IN ('cancelled', 'filled', 'expired') "
                "   OR lifecycle_state IN ('cancelled', 'filled', 'expired', "
                "                          'phantom_rejected', 'user_cancelled')"
            )
            deleted = int(cur.rowcount or 0)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

        log_event("info", "offer_history_cleared",
                  f"Cleared {deleted} terminal-state offer rows "
                  f"(was {n_before} matching)")
        return jsonify({
            "success": True,
            "message": f"Cleared {deleted} closed/cancelled/expired offer rows.",
            "deleted": deleted,
        })
    except Exception as e:
        log_event("warning", "offer_history_clear_failed",
                  f"Clear offer history failed: {e}")
        return api_server._api_error(e, request.path)

@bp.route("/api/reset/full", methods=["POST"])
def api_reset_full():
    """Full reset: P&L counters + offer history + runtime stat counters.

    Wipes everything a user would want gone for a genuinely fresh start
    — but stops short of touching coin designations, wallet state, or
    settings. Chains the existing ``/api/pnl/reset`` semantics with
    offer-history deletion and in-memory counter resets on
    risk_manager / sniper / fill_tracker.

    Gated on bot-not-running. Requires ``{confirm: 'RESET'}``.
    """
    bot = api_server.bot
    slog("GUI_ACTION", ">>> BUTTON: Full Reset")
    try:
        if bot and bot.is_running():
            return jsonify({
                "success": False,
                "error": "bot_running",
                "message": "Stop the bot before running a full reset.",
            }), 409

        payload = request.get_json(silent=True) or {}
        if (payload.get("confirm") or "").strip().upper() != "RESET":
            return jsonify({
                "success": False,
                "error": "confirmation_required",
                "message": "Send {confirm: 'RESET'} to confirm the wipe.",
            }), 400

        # Step 1: PnL reset (fills + round_trips + price_history + inventory).
        summary = api_server._reset_fresh_run_session(
            clear_coins=False,
            clear_price_history=True,
            clear_inventory=True,
            cancel_open_offers=False,
            preserve_history=False,
            reason="full_reset",
        )

        # Step 2: delete terminal-state offer rows.
        conn = get_connection()
        offers_deleted = 0
        try:
            cur = conn.execute(
                "DELETE FROM offers "
                "WHERE status IN ('cancelled', 'filled', 'expired') "
                "   OR lifecycle_state IN ('cancelled', 'filled', 'expired', "
                "                          'phantom_rejected', 'user_cancelled')"
            )
            offers_deleted = int(cur.rowcount or 0)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        # Step 3: reset in-memory counters on bot components. The bot is
        # not running at this point (gate above), so we reset whatever
        # instances exist. All of these are best-effort — a missing
        # component is not a failure condition.
        counters_reset = []
        try:
            if bot is not None:
                _rm = getattr(bot, "risk_manager", None)
                if _rm is not None and hasattr(_rm, "reset_position"):
                    _rm.reset_position()
                    counters_reset.append("risk_manager.position")
                _sn = getattr(bot, "sniper", None)
                if _sn is not None:
                    try:
                        with getattr(_sn, "_snipe_lock", api_server._SNIPE_LOCK_NOOP):
                            _sn._total_snipes = 0
                            _sn._total_skipped = 0
                            if hasattr(_sn, "_snipe_history"):
                                _sn._snipe_history.clear()
                            if hasattr(_sn, "_active_snipe_ids"):
                                _sn._active_snipe_ids.clear()
                            _sn._last_snipe_time = 0
                        counters_reset.append("sniper.counters")
                    except Exception:
                        pass
                _ft = getattr(bot, "fill_tracker", None)
                if _ft is not None:
                    try:
                        if hasattr(_ft, "_mass_disappearance_count"):
                            _ft._mass_disappearance_count = 0
                        if hasattr(_ft, "_mass_disappearance_first_at"):
                            _ft._mass_disappearance_first_at = None
                        counters_reset.append("fill_tracker.counters")
                    except Exception:
                        pass
                # Watchdog persistence streaks — wipe so a fresh start
                # doesn't inherit stale tier-drift alerts.
                try:
                    if hasattr(bot, "_watchdog_violation_streaks"):
                        bot._watchdog_violation_streaks.clear()
                        counters_reset.append("watchdog.streaks")
                except Exception:
                    pass
        except Exception as _c_err:
            log_event("debug", "full_reset_counters_partial",
                      f"Some in-memory counter resets failed (non-fatal): {_c_err}")

        log_event("info", "full_reset_done",
                  f"Full reset: fills={summary.get('fills_cleared', 0)}, "
                  f"round_trips={summary.get('round_trips_cleared', 0)}, "
                  f"offers_deleted={offers_deleted}, "
                  f"counters_reset={','.join(counters_reset) or 'none'}")
        return jsonify({
            "success": True,
            "message": (f"Cleared {summary.get('fills_cleared', 0)} fills, "
                        f"{offers_deleted} offer rows, and reset "
                        f"{len(counters_reset)} in-memory counters."),
            "offers_deleted": offers_deleted,
            "counters_reset": counters_reset,
            **api_server._serialize_dict(summary),
        })
    except Exception as e:
        log_event("warning", "full_reset_failed",
                  f"Full reset failed: {e}")
        return api_server._api_error(e, request.path)

@bp.route("/api/pnl")
def api_pnl():
    """Get PnL summary with realised, unrealised, and round-trip details."""
    bot = api_server.bot
    cfg = api_server.cfg
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    try:
        stats = api_server.get_stats(cfg.CAT_ASSET_ID, since=api_server._get_run_history_cutoff())
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
            "pending_verification_count": api_server._get_session_pending_verification_count(),
            "avg_spread_capture": stats.get("avg_spread_capture", "0"),
            "net_position_cat": inventory.get("net_position_cat", "0"),
            "circuit_breaker_active": inventory.get("circuit_breaker_active", False),
            "sniper": sniper_stats,
            # Extended statistics
            "unmatched_buy_fills": stats.get("unmatched_buy_fills", 0),
            "unmatched_sell_fills": stats.get("unmatched_sell_fills", 0),
            "volume_xch": stats.get("volume_xch", "0"),
            "volume_cat": stats.get("volume_cat", "0"),
            # Per-side gross volumes (new) — what the user actually traded:
            # buy_volume_xch = XCH we paid out to buy CAT
            # buy_volume_cat = CAT we received from those buys
            # sell_volume_xch = XCH we received from selling CAT
            # sell_volume_cat = CAT we delivered on those sells
            # net_xch_flow = sell_volume_xch - buy_volume_xch (gross XCH gain/loss)
            # net_cat_flow = buy_volume_cat - sell_volume_cat (inventory delta)
            "buy_volume_xch": stats.get("buy_volume_xch", "0"),
            "buy_volume_cat": stats.get("buy_volume_cat", "0"),
            "sell_volume_xch": stats.get("sell_volume_xch", "0"),
            "sell_volume_cat": stats.get("sell_volume_cat", "0"),
            "net_xch_flow": stats.get("net_xch_flow", "0"),
            "net_cat_flow": stats.get("net_cat_flow", "0"),
            "avg_fill_size_xch": stats.get("avg_fill_size_xch", "0"),
            "avg_round_trip_secs": stats.get("avg_round_trip_secs", 0),
            "avg_pnl_per_trip_xch": stats.get("avg_pnl_per_trip_xch", "0"),
        }

        return jsonify(api_server._serialize_dict(pnl_data))
    except Exception as e:
        return api_server._api_error(e, request.path)

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

def _set_cancel_all_state(**updates):
    with api_server._cancel_all_state_lock:
        api_server._cancel_all_state.update(updates)
        api_server._cancel_all_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return dict(api_server._cancel_all_state)

def _reset_cancel_all_state(**updates):
    with api_server._cancel_all_state_lock:
        api_server._cancel_all_state.clear()
        api_server._cancel_all_state.update(_new_cancel_all_state())
        api_server._cancel_all_state.update(updates)
        api_server._cancel_all_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return dict(api_server._cancel_all_state)

def _get_cancel_all_state():
    with api_server._cancel_all_state_lock:
        return dict(api_server._cancel_all_state)
