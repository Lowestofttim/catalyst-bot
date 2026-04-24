"""Coin prep + coin inventory + logs + DB backup + fills export.

Heavy coin-prep subprocess orchestration (status, verify, trigger,
reset) plus wallet coin read/topup/prep routes and the log surface
the GUI uses (live log feed, clear, download, debug bundle zip,
subprocess log receiver).

Shared state lives on the api_server module:
  * api_server._coin_prep_state - dict mutated in place
  * api_server._coin_prep_proc  - subprocess.Popen handle; blueprint
    assigns via attribute access instead of `global`
  * api_server._logs_cleared_at - GUI log-panel clear timestamp
"""

from __future__ import annotations

import glob
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, Response, jsonify, request, send_file

import api_server
from config import cfg
from database import log_event, get_stats, backup_database


# Package directory — the parent of blueprints/ (i.e. src/catalyst/).
# Coin-prep sidecars (worker script, subprocess cwd, status/output files)
# live alongside the package modules. dirname(__file__) is blueprints/;
# one more dirname() gets us the package root.
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


bp = Blueprint("coin_prep", __name__)


@bp.route("/api/coins")
def api_coins():
    """Get coin status.

    F62 (2026-04-09): refresh inventory on-demand so the dashboard
    reflects the current wallet state even when the bot isn't running.
    Without this, the in-memory inventory dict stays at whatever the
    last loop tick captured — typically all-zero on a fresh session,
    or stale post-coin-prep until the user starts the bot. The refresh
    is guarded against running during coin prep / topup so it doesn't
    race with the worker.
    """
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # On-demand refresh when the bot isn't running (so the dashboard
    # shows accurate numbers after coin prep finishes). When the bot IS
    # running, its loop refreshes every tick, so skip the extra RPC.
    #
    # Also reap the coin_prep subprocess here — only the bot loop normally
    # calls check_coin_prep_status(), so a manual prep while the bot is
    # stopped leaves ``_prep_running`` pinned True until the next bot
    # start. That blocks the on-demand refresh below and any second prep
    # attempt. Reaping it here lets the dashboard recover without a
    # bot restart.
    try:
        if not bot.is_running():
            bot.coin_manager.check_coin_prep_status()
            bot.coin_manager.update_coin_counts()
    except Exception as _refresh_err:
        # Don't fail the endpoint if the refresh glitches; the cached
        # status is still better than a 500.
        log_event("debug", "api_coins_refresh_failed",
                  f"On-demand coin refresh failed: {_refresh_err}")

    return jsonify(bot.coin_manager.get_status())

@bp.route("/api/coins/topup", methods=["POST"])
def api_coin_topup():
    """Manually trigger coin topup."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Block if bot is live — topup splits coins and races with offer creation
    if bot.is_running():
        return jsonify({
            "error": "Stop the bot before manual top-up. "
                     "The bot handles top-up automatically while running.",
            "requires_stop": True,
        }), 409

    open_buys = bot.offer_manager.get_open_offer_count("buy")
    open_sells = bot.offer_manager.get_open_offer_count("sell")

    started = bot.coin_manager.start_topup(open_buys, open_sells)
    return jsonify({"status": "started" if started else "already_running"})

@bp.route("/api/coins/prep", methods=["POST"])
def api_coin_prep():
    """Manually trigger full coin prep."""
    bot = api_server.bot
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    # Block if bot is live — coin prep splits/combines and races with offer creation
    if bot.is_running():
        return jsonify({
            "error": "Stop the bot before manual coin prep. "
                     "The bot handles coin prep automatically while running.",
            "requires_stop": True,
        }), 409

    started = bot.coin_manager.start_coin_prep()
    return jsonify({"status": "started" if started else "already_running"})

@bp.route("/api/db/backup", methods=["POST"])
def api_db_backup():
    """Create a database backup."""
    bot = api_server.bot
    try:
        path = backup_database()
        # Return only the filename, not the full filesystem path, to
        # avoid leaking the user's directory structure to the GUI.
        filename = os.path.basename(path) if path else ""
        return jsonify({"status": "backed_up", "filename": filename})
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/logs")
def api_logs():
    """Get recent log events — only from current session.

    Uses whichever is more recent: session start time or user's manual
    clear point. This prevents old sessions' noise flooding the console.
    """
    bot = api_server.bot
    limit = request.args.get("limit", 50, type=int)
    category = request.args.get("category") or None  # e.g. offer/pricing/risk
    try:
        from database import get_events_since, get_recent_events
        # Pick the most recent cutoff — session start vs user clear
        cutoff = api_server._session_start_time
        if api_server._logs_cleared_at and (not cutoff or api_server._logs_cleared_at > cutoff):
            cutoff = api_server._logs_cleared_at
        if cutoff:
            events_list = get_events_since(cutoff, limit=limit, category=category)
        else:
            events_list = get_recent_events(limit=limit, category=category)
        return jsonify({"logs": api_server._serialize_list(events_list)})
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/log", methods=["POST"])
def api_log_event():
    """Receive log messages from subprocesses (e.g. coin prep worker).

    The coin prep worker runs in a separate process and can't access the SSE
    event bus directly. It POSTs log messages here, and we push them to the
    live console via SSE + write to the database.
    """
    bot = api_server.bot
    try:
        payload = request.get_json(force=True, silent=True) or {}
        severity = payload.get("severity", "info")
        event_type = payload.get("event_type", "coin_prep")
        message = payload.get("message", "")

        if not message:
            return jsonify({"success": False, "error": "No message"}), 400

        # Write to DB + push to SSE (log_event does both now)
        from database import log_event
        log_event(severity, event_type, message)

        # Emit a coin_change SSE event when coin prep hits key milestones
        # so the Chia dashboard can auto-refresh Coins/Balances/Wallet Status
        if event_type == "coin_prep":
            coin_keywords = ["confirmed", "split", "consolidat", "pool",
                             "coins)", "coin)", "COMPLETE", "verified"]
            if any(kw.lower() in message.lower() for kw in coin_keywords):
                api_server.events.emit("coin_change", {
                    "source": "coin_prep",
                    "message": message[:200],
                })

        return jsonify({"success": True})
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/coin-prep/status")
def api_coin_prep_status():
    """Get coin preparation status.

    Reads live progress from the worker's coin_prep_status.json file
    (the subprocess writes phase/progress/message there).
    Falls back to the in-memory api_server._coin_prep_state for basic running/complete flags.
    """
    bot = api_server.bot
    try:
        result = {"success": True, **api_server._coin_prep_state}

        def _refresh_finished_prep_coin_counts(payload: dict):
            """Backfill current coin counts after prep stops.

            The worker status file records in-run snapshots. On error or completion
            those counts can lag the final stopped-state inventory badly, so prefer
            the same read-only DB/RPC sources used by the other API endpoints.
            """
            if payload.get("running"):
                return

            is_complete = bool(payload.get("complete")) or str(payload.get("phase") or "") == "complete"

            try:
                from database import get_coin_summary

                summary = get_coin_summary() or {}
            except Exception:
                summary = {}

            if summary:
                xch_free = int(summary.get("xch_free_count", 0) or 0)
                cat_free = int(summary.get("cat_free_count", 0) or 0)
                payload["xch_free_coins"] = xch_free
                payload["cat_free_coins"] = cat_free
                if is_complete:
                    payload["xch_coins"] = int(summary.get("xch_total", xch_free) or 0)
                    payload["cat_coins"] = int(summary.get("cat_total", cat_free) or 0)
                else:
                    payload["xch_coins"] = xch_free
                    payload["cat_coins"] = cat_free
                return

            if bot and getattr(bot, "coin_manager", None):
                try:
                    xch, cat = bot.coin_manager.get_coin_health()
                    payload["xch_free_coins"] = int(xch or 0)
                    payload["cat_free_coins"] = int(cat or 0)
                    payload["xch_coins"] = int(xch or 0)
                    payload["cat_coins"] = int(cat or 0)
                    return
                except Exception:
                    pass

            try:
                from wallet import get_spendable_coin_count, WALLET_ID_XCH

                payload["xch_coins"] = int(get_spendable_coin_count(WALLET_ID_XCH) or 0)
                payload["xch_free_coins"] = payload["xch_coins"]
                cat_wallet_id = getattr(cfg, "CAT_WALLET_ID", None) or getattr(bot, "cat_wallet_id", None)
                if cat_wallet_id:
                    payload["cat_coins"] = int(get_spendable_coin_count(int(cat_wallet_id)) or 0)
                    payload["cat_free_coins"] = payload["cat_coins"]
            except Exception:
                pass

        # Read live progress from the worker's status file (V1 parity)
        status_file = os.path.join(_PACKAGE_DIR, "coin_prep_status.json")
        if os.path.exists(status_file):
            try:
                with open(status_file, "r") as f:
                    worker_status = json.load(f)

                # Check if this status file belongs to the CURRENT run.
                # If it has a different run_id (or none), it's stale from
                # a previous run — ignore completion/error from it.
                current_run_id = api_server._coin_prep_state.get("run_id")
                file_run_id = worker_status.get("run_id")
                is_current_run = (
                    current_run_id  # There IS an active run
                    and (
                        not file_run_id  # Old-format file — trust if running
                        or file_run_id == current_run_id  # Same run
                    )
                )

                # Only overlay the worker status when it belongs to the
                # current run, or when there is no active/newer run in memory.
                if is_current_run:
                    result["phase"] = worker_status.get("phase", result.get("phase", "idle"))
                    result["progress"] = worker_status.get("progress", 0)
                    result["message"] = worker_status.get("message", "")
                    result["xch_coins"] = worker_status.get("xch_coins_current", 0)
                    result["cat_coins"] = worker_status.get("cat_coins_current", 0)
                    result["xch_target"] = worker_status.get("xch_coins_target", 0)
                    result["cat_target"] = worker_status.get("cat_coins_target", 0)
                    w_error = worker_status.get("error")
                    if w_error:
                        result["error"] = w_error

                    # Detect completion/error from worker status — but ONLY
                    # if the status file belongs to the current run.
                    if worker_status.get("phase") == "complete":
                        result["complete"] = True
                        result["running"] = False
                        api_server._coin_prep_state["complete"] = True
                        api_server._coin_prep_state["running"] = False
                    elif worker_status.get("phase") == "error":
                        result["running"] = False
                        api_server._coin_prep_state["running"] = False
                        api_server._coin_prep_state["error"] = w_error
                else:
                    # Stale status file from previous run.
                    if api_server._coin_prep_state["running"]:
                        # New run just started — ignore stale file
                        result["phase"] = "idle"
                        result["progress"] = 0
                        result["message"] = "Starting coin preparation..."
                        result["complete"] = False
                    elif worker_status.get("phase") == "complete":
                        # Previous run completed — verify the wallet still
                        # has coins of the RIGHT SIZES before claiming
                        # "already done".  Uses coin_prep_last.json (which
                        # stores the tier sizes/counts from the last
                        # successful prep) + wallet RPC to do disjoint
                        # size-matching with 5 % tolerance — same logic
                        # the /api/coin-prep/verify endpoint uses.
                        _prev_ok = False
                        _matched_xch = 0
                        _matched_cat = 0
                        _target_xch = 0
                        _target_cat = 0
                        try:
                            _prep_path = os.path.join(
                                _PACKAGE_DIR, "coin_prep_last.json",
                            )
                            if os.path.exists(_prep_path):
                                with open(_prep_path, "r") as _pf:
                                    _last = json.load(_pf)

                                from wallet import get_spendable_coins_rpc, WALLET_ID_XCH
                                from config import cfg as _cfg

                                _cat_wid = int(
                                    api_server._active_cat.get("wallet_id")
                                    or getattr(_cfg, "CAT_WALLET_ID", 2)
                                    or 2
                                )
                                _cat_dec = int(
                                    api_server._active_cat.get("decimals")
                                    or getattr(_cfg, "CAT_DECIMALS", 3)
                                )
                                _tol = 0.05

                                # Fetch spendable coins from wallet
                                _xr = get_spendable_coins_rpc(WALLET_ID_XCH)
                                _cr = get_spendable_coins_rpc(_cat_wid)
                                _xch_coins = [
                                    r.get("coin", {}).get("amount", 0)
                                    for r in (_xr or {}).get("records", [])
                                    if r.get("coin", {}).get("amount", 0) > 0
                                ] if _xr and _xr.get("success") else []
                                _cat_coins = [
                                    r.get("coin", {}).get("amount", 0)
                                    for r in (_cr or {}).get("records", [])
                                    if r.get("coin", {}).get("amount", 0) > 0
                                ] if _cr and _cr.get("success") else []

                                def _alloc_match(coins_list, requests, tol):
                                    """Allocate coins disjointly to tiers."""
                                    remaining = list(coins_list)
                                    allocated = {}
                                    reqs = list(enumerate(requests))
                                    reqs.sort(key=lambda x: (-x[1][1], x[0]))
                                    for _, (tier, target_m, needed) in reqs:
                                        if target_m <= 0 or needed <= 0:
                                            allocated[tier] = 0
                                            continue
                                        lo = int(target_m * (1 - tol))
                                        hi = int(target_m * (1 + tol))
                                        hits = [i for i, a in enumerate(remaining) if lo <= a <= hi]
                                        take = min(needed, len(hits))
                                        allocated[tier] = take
                                        for i in reversed(hits[:take]):
                                            remaining.pop(i)
                                    return allocated

                                _all_ok = True
                                if _last.get("tier_enabled"):
                                    _tsxch = _last.get("tier_sizes_xch", {})
                                    _tscat = _last.get("tier_sizes_cat", {})
                                    _tc = _last.get("tier_counts", {})
                                    _xreqs = []
                                    _creqs = []
                                    for _t, _cnt in _tc.items():
                                        _cnt = int(_cnt or 0)
                                        _xsz = float(_tsxch.get(_t, 0))
                                        _csz = float(_tscat.get(_t, 0))
                                        _target_xch += _cnt
                                        if _csz > 0:
                                            _target_cat += _cnt
                                        if _xsz > 0 and _cnt > 0:
                                            _xreqs.append((_t, int(_xsz * 1e12), _cnt))
                                        if _csz > 0 and _cnt > 0:
                                            _creqs.append((_t, int(_csz * (10 ** _cat_dec)), _cnt))
                                    _xa = _alloc_match(_xch_coins, _xreqs, _tol)
                                    _ca = _alloc_match(_cat_coins, _creqs, _tol)
                                    for _t, _cnt in _tc.items():
                                        _cnt = int(_cnt or 0)
                                        if _cnt <= 0:
                                            continue
                                        _xsz = float(_tsxch.get(_t, 0))
                                        _csz = float(_tscat.get(_t, 0))
                                        if _xsz > 0 and _xa.get(_t, 0) < _cnt:
                                            _all_ok = False
                                        if _csz > 0 and _ca.get(_t, 0) < _cnt:
                                            _all_ok = False
                                    _matched_xch = sum(_xa.values())
                                    _matched_cat = sum(_ca.values())
                                else:
                                    # Flat mode
                                    _xsz = float(_last.get("xch_coin_size") or _last.get("prepared_trade_size_xch") or 0)
                                    _csz = float(_last.get("cat_coin_size") or 0)
                                    _xt = int(_last.get("xch_target") or 0)
                                    _ct = int(_last.get("cat_target") or 0)
                                    _target_xch = _xt
                                    _target_cat = _ct
                                    if _xsz > 0 and _xt > 0:
                                        _xm = int(_xsz * 1e12)
                                        _lo = int(_xm * (1 - _tol))
                                        _hi = int(_xm * (1 + _tol))
                                        _matched_xch = sum(1 for c in _xch_coins if _lo <= c <= _hi)
                                        if _matched_xch < _xt:
                                            _all_ok = False
                                    if _csz > 0 and _ct > 0:
                                        _cm = int(_csz * (10 ** _cat_dec))
                                        _lo = int(_cm * (1 - _tol))
                                        _hi = int(_cm * (1 + _tol))
                                        _matched_cat = sum(1 for c in _cat_coins if _lo <= c <= _hi)
                                        if _matched_cat < _ct:
                                            _all_ok = False

                                _prev_ok = _all_ok and (_target_xch > 0 or _target_cat > 0)
                        except Exception:
                            _prev_ok = False

                        if _prev_ok:
                            result["phase"] = "complete"
                            result["complete"] = True
                            result["xch_coins"] = _matched_xch
                            result["cat_coins"] = _matched_cat
                            result["xch_target"] = _target_xch
                            result["cat_target"] = _target_cat
                            result["previously_complete"] = True
                        # else: stale file + wallet doesn't have right
                        # coin sizes → ignore, result stays idle
            except (json.JSONDecodeError, IOError):
                pass  # File being written — skip this poll

        # Also check if the subprocess is still alive (via coin_manager)
        if api_server._coin_prep_state["running"] and bot:
            prep_status = bot.coin_manager.check_coin_prep_status()
            if not prep_status.get("running") and not result.get("phase") == "complete":
                # Subprocess exited but we didn't see "complete" in status file
                exit_code = prep_status.get("exit_code")
                if exit_code is not None and exit_code != 0:
                    result["phase"] = "error"
                    result["error"] = f"Worker exited with code {exit_code}"
                    api_server._coin_prep_state["running"] = False
                    api_server._coin_prep_state["error"] = result["error"]

        _refresh_finished_prep_coin_counts(result)

        # Optionally refresh live coin counts (when not actively prepping)
        refresh = request.args.get("refresh", "false").lower() == "true"
        if refresh and bot and not api_server._coin_prep_state["running"]:
            try:
                xch, cat = bot.coin_manager.get_coin_health()
                result["xch_coins"] = xch
                result["cat_coins"] = cat
            except Exception:
                pass

        # Include last successful prep settings (for smart skip detection)
        prep_json_path = os.path.join(_PACKAGE_DIR, "coin_prep_last.json")
        if os.path.exists(prep_json_path):
            try:
                with open(prep_json_path, "r") as f:
                    result["last_prep_settings"] = json.load(f)
            except (json.JSONDecodeError, IOError):
                result["last_prep_settings"] = None
        else:
            result["last_prep_settings"] = None

        # Include the recent coin prep transcript for the inline console.
        # We prefer DB-backed events because that captures both structured
        # coin_prep logs and raw worker stdout mirrored via /api/log.
        try:
            from database import get_events_since, get_recent_events

            prep_cutoff = (
                api_server._coin_prep_state.get("started_at")
                or api_server._session_start_time
            )
            if prep_cutoff:
                recent_events = get_events_since(prep_cutoff, limit=600)
            else:
                recent_events = get_recent_events(limit=600)

            prep_events = [
                evt for evt in reversed(recent_events)
                if str(evt.get("event_type", "")).startswith("coin_prep")
            ]

            result["log_lines"] = [
                f"{str(evt.get('timestamp', ''))[11:19]} "
                f"[{str(evt.get('severity', 'info')).upper()}] "
                f"{evt.get('message', '')}"
                for evt in prep_events[-400:]
            ]
        except Exception:
            result["log_lines"] = []

        return jsonify(result)
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/coin-prep/verify")
def api_coin_prep_verify():
    """Verify if the wallet already has the right coins for the requested prep.

    Fetches spendable coins from the wallet and groups them by amount,
    checking if enough coins of each tier size already exist.

    Query params (tier mode):
      tier_enabled=true
      inner_xch=1.4&mid_xch=0.7&outer_xch=0.35&extreme_xch=0.14
      inner_cat=...&mid_cat=...&outer_cat=...&extreme_cat=...
      inner_count=6&mid_count=18&outer_count=18&extreme_count=18

    Query params (flat mode):
      tier_enabled=false
      trade_size=0.7&prepared_xch_size=0.77&prepared_cat_size=7654&max_buy=25&max_sell=25
    """
    bot = api_server.bot
    try:
        from wallet import get_spendable_coins_rpc, get_wallet_balance, WALLET_ID_XCH
        from config import cfg

        cat_wallet_id = int(api_server._active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", 2) or 2)
        tier_enabled = request.args.get("tier_enabled", "false").lower() == "true"
        tolerance = 0.05  # 5% tolerance for matching coin sizes

        # Fetch wallet balances for sufficiency check
        # Uses CONFIRMED (total) balance, NOT spendable, because coin prep's
        # first step is to cancel all existing offers — so locked coins WILL
        # become available during prep.
        xch_bal_result = get_wallet_balance(WALLET_ID_XCH)
        cat_bal_result = get_wallet_balance(cat_wallet_id)
        xch_balance_mojos = 0
        cat_balance_mojos = 0
        if xch_bal_result and isinstance(xch_bal_result, dict):
            wb = xch_bal_result.get("wallet_balance") or xch_bal_result
            xch_balance_mojos = wb.get("confirmed_wallet_balance", 0) or wb.get("spendable_balance", 0)
            if isinstance(xch_balance_mojos, str):
                xch_balance_mojos = int(xch_balance_mojos)
        if cat_bal_result and isinstance(cat_bal_result, dict):
            wb = cat_bal_result.get("wallet_balance") or cat_bal_result
            cat_balance_mojos = wb.get("confirmed_wallet_balance", 0) or wb.get("spendable_balance", 0)
            if isinstance(cat_balance_mojos, str):
                cat_balance_mojos = int(cat_balance_mojos)

        # Fetch all spendable coins
        xch_result = get_spendable_coins_rpc(WALLET_ID_XCH)
        cat_result = get_spendable_coins_rpc(cat_wallet_id)

        xch_coins = []
        if xch_result and xch_result.get("success"):
            for r in xch_result.get("records", []):
                amt = r.get("coin", {}).get("amount", 0)
                if amt > 0:
                    xch_coins.append(amt)

        cat_coins = []
        if cat_result and cat_result.get("success"):
            for r in cat_result.get("records", []):
                amt = r.get("coin", {}).get("amount", 0)
                if amt > 0:
                    cat_coins.append(amt)

        cat_decimals = int(api_server._active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3))

        def count_matching(coins_list, target_mojos, tol):
            """Count coins within tolerance of target size."""
            low = int(target_mojos * (1 - tol))
            high = int(target_mojos * (1 + tol))
            return sum(1 for c in coins_list if low <= c <= high)

        def allocate_matching_counts(coins_list, requests, tol):
            """Allocate matching coins disjointly across tiers.

            This avoids double-counting when multiple tiers intentionally share
            the same exact size, like XCH sniper + fees.
            Returns the number of coins allocatable to each tier.
            """
            remaining = list(coins_list)
            allocated = {}
            indexed_requests = list(enumerate(requests))
            indexed_requests.sort(key=lambda item: (-item[1][1], item[0]))

            for _, (tier, target_mojos, needed) in indexed_requests:
                if target_mojos <= 0 or needed <= 0:
                    allocated[tier] = 0
                    continue
                low = int(target_mojos * (1 - tol))
                high = int(target_mojos * (1 + tol))
                matched_positions = [idx for idx, amt in enumerate(remaining) if low <= amt <= high]
                consume = min(needed, len(matched_positions))
                allocated[tier] = consume
                for idx in reversed(matched_positions[:consume]):
                    remaining.pop(idx)

            return allocated

        if tier_enabled:
            tiers = [
                tier for tier in ["inner", "mid", "outer", "extreme", "sniper", "fees"]
                if any(request.args.get(f"{tier}_{suffix}") is not None for suffix in ("xch", "cat", "count"))
            ]
            if not tiers:
                tiers = ["inner", "mid", "outer", "extreme"]
            result_tiers = {}
            all_sufficient = True
            tier_specs = {}
            xch_requests = []
            cat_requests = []

            for tier in tiers:
                xch_size = float(request.args.get(f"{tier}_xch", "0"))
                cat_size = float(request.args.get(f"{tier}_cat", "0"))
                needed = int(request.args.get(f"{tier}_count", "0"))
                is_xch_only_tier = tier == "fees" or cat_size <= 0

                xch_mojos = int(xch_size * 1e12)
                cat_mojos = int(cat_size * (10 ** cat_decimals))
                tier_specs[tier] = {
                    "xch_size": xch_size,
                    "cat_size": cat_size,
                    "needed": needed,
                    "xch_mojos": xch_mojos,
                    "cat_mojos": cat_mojos,
                    "xch_only": is_xch_only_tier,
                }
                if xch_mojos > 0 and needed > 0:
                    xch_requests.append((tier, xch_mojos, needed))
                if not is_xch_only_tier and cat_mojos > 0 and needed > 0:
                    cat_requests.append((tier, cat_mojos, needed))

            xch_allocated = allocate_matching_counts(xch_coins, xch_requests, tolerance)
            cat_allocated = allocate_matching_counts(cat_coins, cat_requests, tolerance)

            for tier in tiers:
                spec = tier_specs[tier]
                needed = spec["needed"]
                xch_have = xch_allocated.get(tier, 0) if spec["xch_mojos"] > 0 else 0
                cat_have = cat_allocated.get(tier, 0) if spec["cat_mojos"] > 0 else 0
                sufficient = (
                    xch_have >= needed and (True if spec["xch_only"] else cat_have >= needed)
                ) if needed > 0 else True
                if not sufficient:
                    all_sufficient = False

                result_tiers[tier] = {
                    "xch_size": spec["xch_size"],
                    "cat_size": spec["cat_size"],
                    "needed": needed,
                    "xch_have": xch_have,
                    "cat_have": cat_have,
                    "xch_only": spec["xch_only"],
                    "sufficient": sufficient,
                }

            # --- Balance sufficiency check ---
            # Calculate total XCH and CAT needed if coin prep were to run
            total_xch_needed_mojos = 0
            total_cat_needed_mojos = 0
            for tier in tiers:
                xch_size = float(request.args.get(f"{tier}_xch", "0"))
                cat_size = float(request.args.get(f"{tier}_cat", "0"))
                needed = int(request.args.get(f"{tier}_count", "0"))
                total_xch_needed_mojos += int(xch_size * 1e12) * needed
                if tier != "fees" and cat_size > 0:
                    total_cat_needed_mojos += int(cat_size * (10 ** cat_decimals)) * needed

            xch_balance_sufficient = xch_balance_mojos >= total_xch_needed_mojos
            cat_balance_sufficient = cat_balance_mojos >= total_cat_needed_mojos

            balance_warnings = []
            if not xch_balance_sufficient and total_xch_needed_mojos > 0:
                xch_need = total_xch_needed_mojos / 1e12
                xch_have = xch_balance_mojos / 1e12
                balance_warnings.append(
                    f"XCH balance too low: need {xch_need:.3f} XCH but only have {xch_have:.3f} XCH"
                )
            if not cat_balance_sufficient and total_cat_needed_mojos > 0:
                cat_unit = 10 ** cat_decimals
                cat_need = total_cat_needed_mojos / cat_unit
                cat_have = cat_balance_mojos / cat_unit
                balance_warnings.append(
                    f"CAT balance too low: need {cat_need:,.0f} CAT but only have {cat_have:,.0f} CAT"
                )

            return jsonify({
                "success": True,
                "tier_enabled": True,
                "tiers": result_tiers,
                "all_sufficient": all_sufficient,
                "xch_total": len(xch_coins),
                "cat_total": len(cat_coins),
                "xch_balance_mojos": xch_balance_mojos,
                "cat_balance_mojos": cat_balance_mojos,
                "xch_needed_mojos": total_xch_needed_mojos,
                "cat_needed_mojos": total_cat_needed_mojos,
                "balance_sufficient": xch_balance_sufficient and cat_balance_sufficient,
                "balance_warnings": balance_warnings,
            })
        else:
            # Flat mode
            trade_size = float(request.args.get("trade_size", "0"))
            prepared_xch_size = float(request.args.get("prepared_xch_size", str(trade_size or 0)))
            prepared_cat_size = float(request.args.get("prepared_cat_size", "0"))
            max_buy = int(request.args.get("max_buy", "0"))
            max_sell = int(request.args.get("max_sell", "0"))
            if prepared_cat_size <= 0:
                prepared_cat_size = trade_size

            xch_mojos = int(prepared_xch_size * 1e12)
            cat_mojos = int(prepared_cat_size * (10 ** cat_decimals))

            xch_right_size = count_matching(xch_coins, xch_mojos, tolerance)
            cat_right_size = count_matching(cat_coins, cat_mojos, tolerance)

            # --- Balance sufficiency check (flat mode) ---
            total_xch_needed_mojos = xch_mojos * max_buy
            total_cat_needed_mojos = cat_mojos * max_sell

            xch_balance_sufficient = xch_balance_mojos >= total_xch_needed_mojos
            cat_balance_sufficient = cat_balance_mojos >= total_cat_needed_mojos

            balance_warnings = []
            if not xch_balance_sufficient and total_xch_needed_mojos > 0:
                xch_need = total_xch_needed_mojos / 1e12
                xch_have = xch_balance_mojos / 1e12
                balance_warnings.append(
                    f"XCH balance too low: need {xch_need:.3f} XCH but only have {xch_have:.3f} XCH"
                )
            if not cat_balance_sufficient and total_cat_needed_mojos > 0:
                cat_unit = 10 ** cat_decimals
                cat_need = total_cat_needed_mojos / cat_unit
                cat_have = cat_balance_mojos / cat_unit
                balance_warnings.append(
                    f"CAT balance too low: need {cat_need:,.0f} CAT but only have {cat_have:,.0f} CAT"
                )

            return jsonify({
                "success": True,
                "tier_enabled": False,
                "xch_coins_right_size": xch_right_size,
                "cat_coins_right_size": cat_right_size,
                "xch_needed": max_buy,
                "cat_needed": max_sell,
                "all_sufficient": (xch_right_size >= max_buy and cat_right_size >= max_sell),
                "xch_total": len(xch_coins),
                "cat_total": len(cat_coins),
                "xch_balance_mojos": xch_balance_mojos,
                "cat_balance_mojos": cat_balance_mojos,
                "xch_needed_mojos": total_xch_needed_mojos,
                "cat_needed_mojos": total_cat_needed_mojos,
                "balance_sufficient": xch_balance_sufficient and cat_balance_sufficient,
                "balance_warnings": balance_warnings,
            })

    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/coin-prep/trigger", methods=["POST"])
def api_coin_prep_trigger():
    """Trigger coin preparation.

    Launches the coin_prep_worker subprocess via coin_manager.
    The worker writes its progress to coin_prep_status.json.
    The /api/coin-prep/status endpoint reads that file for live progress.
    This thread monitors the subprocess and updates running/complete flags.
    """
    bot = api_server.bot
    try:

        # Read coin_multiplier and full_reset flag from request body NOW,
        # while we're still inside the Flask request context. The do_prep()
        # thread runs AFTER the HTTP response is sent, so request.get_json()
        # won't work there.
        try:
            _prep_req_data = request.get_json(silent=True) or {}
            _prep_coin_multiplier = float(_prep_req_data.get("coin_multiplier", 1))
            _prep_coin_multiplier = max(0.5, min(3.0, _prep_coin_multiplier))
        except Exception:
            _prep_req_data = {}
            _prep_coin_multiplier = 1.0
        # Historical flag: full_reset=True means "Start Fresh" — wipes fills /
        # round-trips / position baseline alongside the coin-shape reset.
        # Default False (2026-04-19) so a routine re-prep keeps the user's
        # trading history. 2026-04-21: superseded by the granular flags
        # below (reset_pnl / reset_offer_history / reset_counters) driven by
        # the pre-prep choice modal. full_reset is still honoured as an
        # alias for reset_pnl so older clients keep working.
        _prep_full_reset = bool(_prep_req_data.get("full_reset", False))
        _prep_reset_pnl = bool(_prep_req_data.get("reset_pnl", _prep_full_reset))
        _prep_reset_offers = bool(_prep_req_data.get("reset_offer_history", False))
        _prep_reset_counters = bool(_prep_req_data.get("reset_counters", False))
        log_event("info", "coin_prep_multiplier",
                  f"Coin prep multiplier from GUI: {_prep_coin_multiplier}× "
                  f"(reset_pnl={_prep_reset_pnl}, "
                  f"reset_offers={_prep_reset_offers}, "
                  f"reset_counters={_prep_reset_counters})")

        # If a previous worker is still running, kill it first.
        # Two workers operating on the same wallet simultaneously causes
        # coin conflicts, failed splits, and wallet sync chaos.
        if api_server._coin_prep_proc is not None and api_server._coin_prep_proc.poll() is None:
            old_pid = api_server._coin_prep_proc.pid
            log_event("info", "coin_prep_kill",
                      f"Killing previous coin prep worker (PID: {old_pid}) before starting new run")
            try:
                api_server._coin_prep_proc.terminate()
                # Give it 3 seconds to exit gracefully, then force kill
                try:
                    api_server._coin_prep_proc.wait(timeout=3)
                except Exception:
                    api_server._coin_prep_proc.kill()
                    api_server._coin_prep_proc.wait(timeout=2)
                log_event("info", "coin_prep_killed",
                          f"Previous worker (PID: {old_pid}) terminated")
            except Exception as kill_err:
                log_event("warning", "coin_prep_kill_failed",
                          f"Could not kill PID {old_pid}: {kill_err}")
            api_server._coin_prep_proc = None

        # Also kill any worker launched via coin_manager (bot loop path)
        if bot and hasattr(bot, 'coin_manager') and bot.coin_manager._prep_process:
            cm_proc = bot.coin_manager._prep_process
            if cm_proc.poll() is None:
                cm_pid = cm_proc.pid
                log_event("info", "coin_prep_kill",
                          f"Killing coin_manager worker (PID: {cm_pid}) before starting new run")
                try:
                    cm_proc.terminate()
                    try:
                        cm_proc.wait(timeout=3)
                    except Exception:
                        cm_proc.kill()
                        cm_proc.wait(timeout=2)
                except Exception:
                    pass
                bot.coin_manager._prep_process = None
                bot.coin_manager._prep_running = False

        # ---- Clear session data before the coin_prep_worker runs ----
        # Under the default preserve_history path we only wipe state that
        # directly refers to the coin IDs / offers about to be replaced:
        # coin rows, inventory snapshots, cancelled offers. Fills, round
        # trips, position baseline, and market-intel stats all survive so
        # a routine re-prep doesn't destroy the user's trading record.
        #
        # Under full_reset=True the call mirrors the pre-2026-04-19
        # behaviour — fills and round-trips are deleted too. That path is
        # opt-in, triggered from the GUI's "Start Fresh" button in the
        # pre-prep confirm modal or the PnL tab's Reset Stats action.
        try:
            api_server._reset_fresh_run_session(
                clear_coins=True,
                clear_price_history=_prep_reset_pnl,
                clear_inventory=True,
                cancel_open_offers=True,
                preserve_history=(not _prep_reset_pnl),
                reason=("fresh_start_cleanup" if _prep_reset_pnl
                        else "coin_prep_reprep_cleanup"),
            )
        except Exception as _clean_err:
            log_event("warning", "fresh_start_cleanup_failed",
                      f"DB cleanup before coin prep failed: {_clean_err}")

        # Optional: delete terminal-state offer rows. Same SQL as the
        # standalone /api/reset/offer-history endpoint — live offers are
        # already handled by the cancel_open_offers path above, so this
        # only touches cancelled/filled/expired/phantom rows that would
        # otherwise bloat the history view.
        if _prep_reset_offers:
            try:
                conn = get_connection()
                cur = conn.execute(
                    "DELETE FROM offers "
                    "WHERE status IN ('cancelled', 'filled', 'expired') "
                    "   OR lifecycle_state IN ('cancelled', 'filled', 'expired', "
                    "                          'phantom_rejected', 'user_cancelled')"
                )
                deleted = int(cur.rowcount or 0)
                conn.commit()
                log_event("info", "coin_prep_offer_history_cleared",
                          f"Pre-prep: cleared {deleted} terminal-state offer rows")
            except Exception as _hist_err:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log_event("warning", "coin_prep_offer_history_failed",
                          f"Pre-prep offer-history clear failed: {_hist_err}")

        # Optional: reset in-memory runtime counters (sniper / fill-tracker /
        # watchdog streaks / risk-manager position). Mirrors the counters
        # step of /api/reset/full. Best-effort — missing attrs aren't fatal.
        if _prep_reset_counters:
            _counters_reset = []
            try:
                if bot is not None:
                    _rm = getattr(bot, "risk_manager", None)
                    if _rm is not None and hasattr(_rm, "reset_position"):
                        _rm.reset_position()
                        _counters_reset.append("risk_manager.position")
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
                            _counters_reset.append("sniper.counters")
                        except Exception:
                            pass
                    _ft = getattr(bot, "fill_tracker", None)
                    if _ft is not None:
                        try:
                            if hasattr(_ft, "_mass_disappearance_count"):
                                _ft._mass_disappearance_count = 0
                            if hasattr(_ft, "_mass_disappearance_first_at"):
                                _ft._mass_disappearance_first_at = None
                            _counters_reset.append("fill_tracker.counters")
                        except Exception:
                            pass
                    try:
                        if hasattr(bot, "_watchdog_violation_streaks"):
                            bot._watchdog_violation_streaks.clear()
                            _counters_reset.append("watchdog.streaks")
                    except Exception:
                        pass
                log_event("info", "coin_prep_counters_reset",
                          f"Pre-prep counter resets: "
                          f"{','.join(_counters_reset) or 'none'}")
            except Exception as _c_err:
                log_event("warning", "coin_prep_counters_failed",
                          f"Pre-prep counter reset partial: {_c_err}")

        # Balance gate removed — the /api/coin-prep/verify endpoint already checks
        # balance accurately before the confirm button is shown, and uses the same
        # coin plan formula as the GUI. The old formula here (c * 2 * mult) was
        # overcalculating required XCH and blocking valid runs at higher multipliers.

        # Generate a unique run ID so we can distinguish old completions from new runs
        import uuid as _uuid
        run_id = str(_uuid.uuid4())[:8]

        api_server._coin_prep_state["running"] = True
        api_server._coin_prep_state["complete"] = False
        api_server._coin_prep_state["error"] = None
        api_server._coin_prep_state["phase"] = "idle"
        api_server._coin_prep_state["run_id"] = run_id
        api_server._coin_prep_state["started_at"] = datetime.now(timezone.utc).isoformat()

        # CRITICAL: Stop the bot loop entirely during coin prep.
        # Just setting _prep_running is NOT enough — the bot loop's
        # requote step also creates offers, and any running cycle
        # may already be mid-execution. The only safe approach is
        # to fully stop the bot. User must press "Start Bot" after
        # coin prep completes.
        if bot and bot.is_running():
            bot.stop()
            log_event("info", "coin_prep_bot_stopped",
                      "Bot loop STOPPED for coin prep — press Start Bot after prep completes")
            api_server.events.emit("bot_control", {"action": "stopped",
                                        "reason": "coin_prep"})

        # Also set the flag as a safety belt
        if bot and hasattr(bot, 'coin_manager'):
            bot.coin_manager._prep_running = True
            log_event("info", "coin_prep_gate",
                      "Coin manager marked busy for coin prep")

        # Write a fresh "starting" status file immediately.
        # This prevents the GUI from reading stale COMPLETE status
        # from a previous run during the gap before the subprocess starts.
        status_file = os.path.join(_PACKAGE_DIR, "coin_prep_status.json")
        try:
            fresh_status = {
                "phase": "idle",
                "progress": 0.0,
                "message": "Starting coin preparation...",
                "xch_coins_current": 0,
                "cat_coins_current": 0,
                "xch_coins_target": 0,
                "cat_coins_target": 0,
                "error": None,
                "timestamp": time.time(),
                "run_id": run_id
            }
            with open(status_file, "w") as f:
                json.dump(fresh_status, f, indent=2)
        except Exception:
            # If we can't write, at least try to delete the old one
            try:
                if os.path.exists(status_file):
                    os.remove(status_file)
            except Exception:
                pass

        def do_prep():
            prep_succeeded = False
            try:
                # Launch worker without a visible console window.
                # We rely on the DB/superlog/log file for debugging instead of
                # popping a Windows terminal in front of the GUI.
                import subprocess as _sp
                worker_dir = _PACKAGE_DIR
                worker_path = os.path.join(worker_dir, "coin_prep_worker.py")

                if not os.path.exists(worker_path):
                    api_server._coin_prep_state["error"] = "coin_prep_worker.py not found"
                    api_server._coin_prep_state["running"] = False
                    return

                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"

                # Build CLI args from LIVE config so the worker uses the
                # actual GUI settings, not stale .env values.
                # Double up: buy+sell per side for spares (requotes, sniping)
                max_buy = getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25)
                max_sell = getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25)
                trade_xch = str(getattr(cfg, "DEFAULT_TRADE_XCH", "0.5"))

                if getattr(cfg, "TIER_ENABLED", False):
                    # Tier-aware coin prep with PER-SIDE counts. Buy ladder is XCH-funded
                    # (BUY_*_TIER_COUNT + spares); sell ladder is CAT-funded (SELL_*_TIER_COUNT
                    # + spares). The worker uses these independently, so asymmetric ladders
                    # (e.g. 3 buy inner + 10 sell inner) prep the right number of coins on
                    # each side instead of forcing both sides to the larger value.
                    #
                    # F62 (2026-04-09): tier SIZES are also per-side now. XCH
                    # coins use buy sizes; CAT coins use sell sizes. When the
                    # per-side fields aren't set the helpers fall back to the
                    # legacy shared sizes with reverse-buy flipping.
                    #
                    # F62b (2026-04-09): the worker's tier_counts come from
                    # `_flip_tiers(buy_position_counts, side="buy")` below,
                    # which are SIZE-INDEXED (under reverse-buy, buy position
                    # inner → size extreme slot). So the sizes dict we hand
                    # the worker must ALSO be size-indexed, otherwise the
                    # count × size product multiplies mismatched pairs and
                    # blows up the pool by 2x. Apply the reverse-buy flip to
                    # the size dict so it's consistent with the counts.
                    from config import get_buy_tier_size_xch, get_sell_tier_size_xch
                    # Launcher is in a separate function from Smart Settings,
                    # so `_buy_ladder_reversed` isn't in scope — read directly
                    # from config here.
                    _buy_ladder_reversed = bool(getattr(cfg, "BUY_LADDER_REVERSED", False))
                    # Position-semantic buy sizes (from per-side helpers):
                    _buy_inner_pos = Decimal(str(get_buy_tier_size_xch("inner")   or getattr(cfg, "INNER_SIZE_XCH", Decimal("1.0"))))
                    _buy_mid_pos   = Decimal(str(get_buy_tier_size_xch("mid")     or getattr(cfg, "MID_SIZE_XCH", Decimal("0.5"))))
                    _buy_outer_pos = Decimal(str(get_buy_tier_size_xch("outer")   or getattr(cfg, "OUTER_SIZE_XCH", Decimal("0.25"))))
                    _buy_extr_pos  = Decimal(str(get_buy_tier_size_xch("extreme") or getattr(cfg, "EXTREME_SIZE_XCH", Decimal("0.1"))))
                    if _buy_ladder_reversed:
                        # Under reverse-buy, SIZE inner (biggest XCH coin) is
                        # used by POSITION extreme, and SIZE extreme (smallest)
                        # is used by POSITION inner. Flip to match the
                        # size-indexed counts.
                        _buy_tier_sizes = {
                            "inner":   _buy_extr_pos,  # size inner slot = pos extreme size (biggest)
                            "mid":     _buy_outer_pos,
                            "outer":   _buy_mid_pos,
                            "extreme": _buy_inner_pos, # size extreme slot = pos inner size (smallest)
                        }
                    else:
                        _buy_tier_sizes = {
                            "inner":   _buy_inner_pos,
                            "mid":     _buy_mid_pos,
                            "outer":   _buy_outer_pos,
                            "extreme": _buy_extr_pos,
                        }
                    # Sell side is never flipped — sell positions always map
                    # to their same-named size tier.
                    _sell_tier_sizes = {
                        "inner":   Decimal(str(get_sell_tier_size_xch("inner")   or getattr(cfg, "INNER_SIZE_XCH", Decimal("1.0")))),
                        "mid":     Decimal(str(get_sell_tier_size_xch("mid")     or getattr(cfg, "MID_SIZE_XCH", Decimal("0.5")))),
                        "outer":   Decimal(str(get_sell_tier_size_xch("outer")   or getattr(cfg, "OUTER_SIZE_XCH", Decimal("0.25")))),
                        "extreme": Decimal(str(get_sell_tier_size_xch("extreme") or getattr(cfg, "EXTREME_SIZE_XCH", Decimal("0.1")))),
                    }
                    # Kept for backward compat with code below that reads
                    # `tier_sizes` as a single dict (it'll be the max of both
                    # sides, used only for the worker's legacy --tier-sizes
                    # arg). The per-side values also flow via new CLI args.
                    tier_sizes = {
                        k: max(_buy_tier_sizes.get(k, Decimal("0")),
                               _sell_tier_sizes.get(k, Decimal("0")))
                        for k in ("inner", "mid", "outer", "extreme")
                    }

                    def _tier_count(prefix, tier):
                        live = int(getattr(cfg, f"{prefix}_{tier.upper()}_TIER_COUNT", 0) or 0)
                        spare = int(getattr(cfg, f"{prefix}_{tier.upper()}_TIER_SPARE_COUNT", 0) or 0)
                        return max(0, live + spare)

                    # ── Slot-position counts as configured by the user ──────
                    # These describe how many BUY/SELL offers sit at each
                    # ladder POSITION (inner=closest to mid, extreme=furthest).
                    buy_position_counts = {
                        "inner":   _tier_count("BUY", "inner"),
                        "mid":     _tier_count("BUY", "mid"),
                        "outer":   _tier_count("BUY", "outer"),
                        "extreme": _tier_count("BUY", "extreme"),
                    }
                    sell_position_counts = {
                        "inner":   _tier_count("SELL", "inner"),
                        "mid":     _tier_count("SELL", "mid"),
                        "outer":   _tier_count("SELL", "outer"),
                        "extreme": _tier_count("SELL", "extreme"),
                    }

                    # ── Translate slot positions → coin SIZE counts ─────────
                    # The coin prep allocates coins by SIZE, not by position.
                    # When BUY_LADDER_REVERSED is on, a buy slot at the
                    # "extreme" position uses an INNER-sized coin, etc. The
                    # flip helper applies that mapping (no-op when reversal
                    # is off, and always a no-op for the sell side). This
                    # makes the live ladder settings the SINGLE SOURCE OF
                    # TRUTH for both prep and offer creation.
                    from coin_manager import flip_position_tiers_to_coin_size_tiers as _flip_tiers
                    xch_tier_counts = _flip_tiers(buy_position_counts, side="buy")
                    cat_tier_counts = _flip_tiers(sell_position_counts, side="sell")

                    # Sniper needs BOTH sides: buy snipers lock XCH coins, sell snipers
                    # lock CAT coins. preferred_tier="sniper" strict on both sides, so a
                    # missing CAT sniper pool silently kills sell-side probes and leaves
                    # the ladder anchored to one-sided probe data only. Fees are XCH-only.
                    sniper_count = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
                    sniper_size = Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", "0") or "0"))
                    if getattr(cfg, "SNIPER_ENABLED", False) and sniper_count > 0 and sniper_size > 0:
                        xch_tier_counts["sniper"] = sniper_count
                        cat_tier_counts["sniper"] = sniper_count
                        tier_sizes["sniper"] = sniper_size

                    fee_status = api_server.get_fee_settings_snapshot()
                    fee_count = int(fee_status.get("fee_prep_count", 0) or 0)
                    fee_size = Decimal(str(fee_status.get("fee_coin_size_xch", "0") or "0"))
                    if fee_status.get("fee_pool_enabled") and fee_count > 0 and fee_size > 0:
                        xch_tier_counts["fees"] = fee_count
                        tier_sizes["fees"] = fee_size

                    # Drop zero entries so the worker log stays clean
                    xch_tier_counts = {k: v for k, v in xch_tier_counts.items() if v > 0}
                    cat_tier_counts = {k: v for k, v in cat_tier_counts.items() if v > 0}

                    xch_total_coins = sum(xch_tier_counts.values())
                    cat_total_coins = sum(cat_tier_counts.values())
                    total_coins = xch_total_coins + cat_total_coins

                    tier_sizes_str = ",".join(f"{tier}={size}" for tier, size in tier_sizes.items())
                    xch_counts_str = ",".join(f"{k}={v}" for k, v in xch_tier_counts.items())
                    cat_counts_str = ",".join(f"{k}={v}" for k, v in cat_tier_counts.items())
                    # F62 (2026-04-09): also build per-side size strings.
                    # Sniper/fees stay in the combined `tier_sizes` dict;
                    # only the four trading tiers differ between buy and sell.
                    _buy_sizes_for_cli = dict(_buy_tier_sizes)
                    _sell_sizes_for_cli = dict(_sell_tier_sizes)
                    # Add sniper/fees from the combined dict (same on both sides)
                    if "sniper" in tier_sizes:
                        _buy_sizes_for_cli["sniper"] = tier_sizes["sniper"]
                        _sell_sizes_for_cli["sniper"] = tier_sizes["sniper"]
                    if "fees" in tier_sizes:
                        _buy_sizes_for_cli["fees"] = tier_sizes["fees"]
                        # fees is XCH-only, don't add to sell
                    buy_sizes_str  = ",".join(f"{t}={s}" for t, s in _buy_sizes_for_cli.items())
                    sell_sizes_str = ",".join(f"{t}={s}" for t, s in _sell_sizes_for_cli.items())

                    # Pass the live weighted mid (Tibet+Dexie) so prep sizes
                    # CAT coins against the same price the bot uses for live
                    # offers. Without this, prep defaults to Dexie's last_price,
                    # which can lag by 40%+ on thin markets and undersize the
                    # CAT sniper pool (sniper sell creation then fails).
                    _live_price_arg = api_server._get_live_mid_price_str()
                    cmd = [
                        "python", worker_path,
                        "--xch-target", str(xch_total_coins),
                        "--cat-target", str(cat_total_coins),
                        "--tier-sizes", tier_sizes_str,       # legacy shared (kept for back-compat)
                        "--buy-tier-sizes", buy_sizes_str,    # F62: XCH coin sizes (for buy offers)
                        "--sell-tier-sizes", sell_sizes_str,  # F62: CAT coin sizes (for sell offers, in XCH equiv)
                        "--tier-counts-xch", xch_counts_str,
                        "--tier-counts-cat", cat_counts_str,
                        "--prep-headroom-pct", str(getattr(cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"))),
                        "--run-id", run_id,
                    ]
                    if _live_price_arg:
                        cmd += ["--live-price", _live_price_arg]
                    log_event("info", "coin_prep_config",
                              f"GUI tier coin prep (per-side): "
                              f"XCH={xch_total_coins} {xch_counts_str} | "
                              f"CAT={cat_total_coins} {cat_counts_str} "
                              f"(+{getattr(cfg, 'COIN_PREP_HEADROOM_PCT', Decimal('10'))}% headroom) "
                              f"live_price={_live_price_arg or 'unavailable→Dexie fallback'}")
                else:
                    # Uniform coin prep — uses _prep_coin_multiplier from request context
                    coin_multiplier = _prep_coin_multiplier
                    total_coins = int((max_buy + max_sell) * coin_multiplier)
                    _live_price_arg = api_server._get_live_mid_price_str()
                    cmd = [
                        "python", worker_path,
                        "--xch-target", str(total_coins),
                        "--xch-size", trade_xch,
                        "--cat-target", str(total_coins),
                        "--prep-headroom-pct", str(getattr(cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"))),
                        "--run-id", run_id,
                    ]
                    if _live_price_arg:
                        cmd += ["--live-price", _live_price_arg]
                    log_event("info", "coin_prep_config",
                              f"GUI coin prep: {total_coins} coins "
                              f"({max_buy}+{max_sell} × {coin_multiplier}), "
                              f"XCH size {trade_xch} (+{getattr(cfg, 'COIN_PREP_HEADROOM_PCT', Decimal('10'))}% headroom)")

                log_path = os.path.join(worker_dir, "coin_prep_output.log")
                log_file = open(log_path, "w", encoding="utf-8")
                popen_kwargs = {
                    "stdout": log_file,
                    "stderr": _sp.STDOUT,
                    "stdin": _sp.DEVNULL,
                    "cwd": worker_dir,
                    "env": env,
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = getattr(_sp, "CREATE_NO_WINDOW", 0)
                proc = _sp.Popen(cmd, **popen_kwargs)

                # Store process object globally (for killing on re-trigger)
                # and PID for lifecycle management
                api_server._coin_prep_proc = proc
                api_server._coin_prep_state["pid"] = proc.pid

                log_event("info", "coin_prep_started",
                          f"Coin prep worker started (PID: {proc.pid})")

                # Monitor until it finishes
                while proc.poll() is None:
                    time.sleep(2)

                exit_code = proc.returncode

                if exit_code == 0:
                    api_server._coin_prep_state["complete"] = True
                    api_server._coin_prep_state["error"] = None
                    api_server._coin_prep_state["phase"] = "complete"
                    prep_succeeded = True
                    log_event("info", "coin_prep_complete", "Coin prep finished successfully")
                    # F82 (2026-04-20): push a coin_update SSE event so the
                    # Command Centre tier-group card renders the fresh coin
                    # inventory immediately. Without this the GUI only
                    # shows live counts when the bot is running — the user
                    # sees blank tier groups for the entire "coins ready,
                    # bot not yet started" window. The worker writes the
                    # designated/assigned_tier rows to the DB before exit,
                    # so querying here reflects the final prep state.
                    try:
                        from database import (
                            get_coin_summary as _gcs,
                            get_live_tier_group_counts as _gltgc,
                        )
                        _summary = _gcs() or {}
                        _tier_counts = _gltgc()
                        _tier_counts["enabled"] = bool(
                            getattr(cfg, "TIER_ENABLED", False)
                        )
                        _xch_locked_mojos = int(_summary.get("xch_locked_mojos", 0) or 0)
                        _cat_locked_mojos = int(_summary.get("cat_locked_mojos", 0) or 0)
                        _cat_dec = int(
                            api_server._active_cat.get("decimals")
                            or getattr(cfg, "CAT_DECIMALS", 3)
                            or 3
                        )
                        api_server.events.emit("coin_update", {
                            "reason": "coin_prep_complete",
                            "xch_free": int(_summary.get("xch_free_count", 0) or 0),
                            "xch_locked": int(_summary.get("xch_locked_count", 0) or 0),
                            "xch_total": int(_summary.get("xch_total", 0) or 0),
                            "cat_free": int(_summary.get("cat_free_count", 0) or 0),
                            "cat_locked": int(_summary.get("cat_locked_count", 0) or 0),
                            "cat_total": int(_summary.get("cat_total", 0) or 0),
                            "xch_locked_amount": (
                                f"{_xch_locked_mojos / 1e12:.4f}"
                                if _xch_locked_mojos > 0 else "0"
                            ),
                            "cat_locked_amount": (
                                f"{_cat_locked_mojos / (10 ** _cat_dec):.{_cat_dec}f}"
                                if _cat_locked_mojos > 0 else "0"
                            ),
                            "tier_counts": _tier_counts,
                        })
                    except Exception as _e:
                        log_event("debug", "coin_prep_emit_failed",
                                  f"Post-prep coin_update emit failed (non-critical): {_e}")
                else:
                    api_server._coin_prep_state["complete"] = False
                    api_server._coin_prep_state["phase"] = "error"
                    error_msg = f"Worker exited with code {exit_code}"
                    # Try to read log file for error context (non-Windows)
                    log_path = os.path.join(worker_dir, "coin_prep_output.log")
                    if os.path.exists(log_path):
                        try:
                            with open(log_path, "r", encoding="utf-8") as f:
                                output = f.read()
                            if output:
                                error_msg += f"\nLast output: ...{output[-500:]}"
                        except Exception:
                            pass
                    log_event("error", "coin_prep_failed", error_msg[:1000])
                    api_server._coin_prep_state["error"] = error_msg

            except Exception as e:
                api_server._coin_prep_state["complete"] = False
                api_server._coin_prep_state["phase"] = "error"
                api_server._coin_prep_state["error"] = str(e)
                log_event("error", "coin_prep_exception", str(e))
            finally:
                # Ensure the subprocess is terminated if it's still running.
                # Without this, an exception in the monitor loop (e.g. log_event
                # or state update throws) can orphan the child process.
                try:
                    if 'proc' in locals() and proc and proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                except Exception:
                    pass
                try:
                    if 'log_file' in locals() and log_file:
                        log_file.close()
                except Exception:
                    pass
                api_server._coin_prep_state["running"] = False
                api_server._coin_prep_proc = None  # Clear global ref — worker is done
                # CRITICAL: Ungate the bot loop so it can resume offer creation
                if bot and hasattr(bot, 'coin_manager'):
                    bot.coin_manager._prep_running = False
                    if prep_succeeded:
                        log_event("info", "coin_prep_ungate",
                                  "Coin prep complete — press Start Bot to begin trading")
                    else:
                        log_event("warning", "coin_prep_ungate_error",
                                  "Coin prep ended with an error — review details before retrying")

        threading.Thread(target=do_prep, daemon=True).start()
        return jsonify({"success": True, "message": "Coin prep started"})
    except Exception as e:
        api_server._coin_prep_state["running"] = False
        # Also ungate on early failure
        if bot and hasattr(bot, 'coin_manager'):
            bot.coin_manager._prep_running = False
        try:
            log_event("error", "coin_prep_trigger_failed", str(e))
        except Exception:
            pass
        return api_server._api_error(e, request.path)

@bp.route("/api/coin-prep/reset", methods=["POST"])
def api_coin_prep_reset():
    """Reset coin prep state."""
    bot = api_server.bot
    api_server._coin_prep_state["running"] = False
    api_server._coin_prep_state["complete"] = False
    api_server._coin_prep_state["started_at"] = None
    # Ungate bot loop if it was gated
    if bot and hasattr(bot, 'coin_manager'):
        bot.coin_manager._prep_running = False
    api_server._coin_prep_state["error"] = None
    return jsonify({"success": True})

@bp.route("/api/fills/export")
def api_fills_export():
    """Export fill history as CSV."""
    bot = api_server.bot
    try:
        asset_id = api_server._active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        if not asset_id:
            return jsonify({"success": False, "error": "No active CAT selected"}), 400

        history = api_server._build_fill_history_for_gui(asset_id, limit=1000)
        if not history:
            return jsonify({"success": False, "error": "No fills to export"}), 404

        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "filled_at",
            "side",
            "price_xch",
            "size_xch",
            "size_cat",
            "tier",
            "trade_id",
            "coin_id",
        ])
        for f in history:
            writer.writerow([
                f.get("filled_at", ""),
                f.get("side", ""),
                str(f.get("price", "")),
                str(f.get("size_xch", "")),
                str(f.get("size_cat", "")),
                str(f.get("tier", "")),
                f.get("trade_id", ""),
                f.get("coin_id", ""),
            ])
        csv_data = output.getvalue()
        return Response(csv_data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=fills_export.csv"})
    except Exception as e:
        return api_server._api_error(e, request.path)

@bp.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    """Clear the GUI log panel (hides older events, keeps them in DB for debug download)."""
    bot = api_server.bot
    from datetime import datetime, timezone
    api_server._logs_cleared_at = datetime.now(timezone.utc).isoformat()
    # Persist to database so it survives restarts
    try:
        from database import set_setting
        set_setting("logs_cleared_at", api_server._logs_cleared_at)
    except Exception:
        pass
    return jsonify({"success": True, "message": "Log panel cleared"})

@bp.route("/api/logs/download")
def api_logs_download():
    """Download a richer debug bundle with recent events and runtime state."""
    bot = api_server.bot
    cfg = api_server.cfg
    try:
        import glob
        import io
        import zipfile
        from database import get_recent_events
        from super_log import get_archive_summary, get_log_path, get_log_stats

        def _read_text_tail(path: str, max_bytes: int = 400_000) -> str:
            if not path or not os.path.exists(path):
                return ""
            with open(path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                if size > max_bytes:
                    fh.seek(-max_bytes, os.SEEK_END)
                else:
                    fh.seek(0)
                return fh.read().decode("utf-8", errors="replace")

        def _json_safe(value):
            if isinstance(value, dict):
                return api_server._serialize_dict(value)
            if isinstance(value, list):
                return api_server._serialize_list(value)
            return value

        events_list = get_recent_events(limit=2000)
        lines = []
        for ev in events_list:
            ts = ev.get("timestamp", "")
            level = ev.get("severity", "")
            source = ev.get("event_type", "")
            msg = ev.get("message", "")
            lines.append(f"[{ts}] [{level}] [{source}] {msg}")

        event_counts = {}
        for ev in events_list:
            key = str(ev.get("event_type", "") or "unknown")
            event_counts[key] = event_counts.get(key, 0) + 1

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": api_server.get_app_version(),
            "wallet_type": api_server.get_wallet_type(),
            "bot_running": bool(bot.is_running()) if bot else False,
            "current_cat": api_server._active_cat,
            "session_start_time": api_server._session_start_time,
            "logs_cleared_at": api_server._logs_cleared_at,
            "event_count": len(events_list),
        }

        snapshots = {
            "health": api_server._get_health_snapshot(),
            "event_type_counts": event_counts,
            "superlog_stats": get_log_stats(),
            "superlog_archive": get_archive_summary(5),
        }

        if bot:
            runtime_snapshot = {
                "running": bool(bot.is_running()),
                "loop_count": int(getattr(bot, "_loop_count", 0) or 0),
                "uptime_secs": int(time.time() - getattr(bot, "_start_time", 0))
                if getattr(bot, "_start_time", 0) else 0,
                "recovery": dict(getattr(bot, "_recovery_state", {}) or {}),
                "probe_state": dict(getattr(bot, "_probe_state", {}) or {}),
            }
            try:
                runtime_snapshot["price_info"] = bot.get_price_info()
            except Exception as e:
                runtime_snapshot["price_info_error"] = str(e)
            snapshots["runtime"] = api_server._serialize_dict(runtime_snapshot)

            try:
                stats = get_stats(cfg.CAT_ASSET_ID, since=api_server._get_run_history_cutoff())
                snapshots["pnl"] = api_server._serialize_dict({
                    **stats,
                    "pending_verification_count": api_server._get_session_pending_verification_count(),
                    "sniper": bot.sniper.get_stats() if getattr(bot, "sniper", None) else {},
                })
            except Exception as e:
                snapshots["pnl"] = {"error": str(e)}

            try:
                snapshots["market_intel"] = api_server._serialize_dict(bot.market_intel.get_market_summary() or {})
            except Exception as e:
                snapshots["market_intel"] = {"error": str(e)}

            try:
                snapshots["runtime_monitor"] = api_server._serialize_dict(bot.runtime_monitor.get_state() or {})
            except Exception as e:
                snapshots["runtime_monitor"] = {"error": str(e)}

            splash_snapshot = {}
            try:
                splash_snapshot["broadcast"] = api_server._serialize_dict(bot.splash_manager.get_stats() or {})
            except Exception as e:
                splash_snapshot["broadcast"] = {"error": str(e)}
            try:
                splash_snapshot["node"] = api_server._serialize_dict(bot.splash_node.get_status() or {})
            except Exception as e:
                splash_snapshot["node"] = {"error": str(e)}
            try:
                splash_snapshot["receive"] = api_server._serialize_dict(bot.get_splash_receive_stats() or {})
            except Exception as e:
                splash_snapshot["receive"] = {"error": str(e)}
            snapshots["splash"] = splash_snapshot

        log_texts = {}
        superlog_path = get_log_path()
        if superlog_path:
            log_texts["logs/current_superlog_tail.log"] = _read_text_tail(superlog_path)

        tauri_stdout = os.path.join(api_server._APP_ROOT, "tauri_backend_stdout.log")
        if os.path.exists(tauri_stdout):
            log_texts["logs/tauri_backend_stdout_tail.log"] = _read_text_tail(tauri_stdout)

        # Look for superlog files in the user data dir first (the
        # canonical location), then fall back to the install dir for
        # pre-migration dev installs.
        try:
            from user_paths import log_dir as _user_log_dir
            _log_dirs = [_user_log_dir(), api_server._APP_ROOT]
        except Exception:
            _log_dirs = [api_server._APP_ROOT]
        run_logs = []
        for _ld in _log_dirs:
            run_logs.extend(glob.glob(os.path.join(_ld, "bot_superlog_*.log")))
        if run_logs:
            latest_run_log = max(run_logs, key=os.path.getmtime)
            log_texts["logs/latest_run_superlog_tail.log"] = _read_text_tail(latest_run_log)
            manifest["latest_run_log"] = os.path.basename(latest_run_log)

        bundle_name = "bot_debug_bundle_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".zip"
        readme = "\n".join([
            "CATalyst debug bundle",
            "",
            "Included:",
            "- manifest.json: bundle metadata",
            "- recent_events.json / recent_events.txt: latest database events",
            "- snapshots/*.json: health, runtime, market, pnl, splash, and monitor state",
            "- logs/*.log: tails of the current superlog and nearby runtime logs",
            "",
            "This bundle is designed for troubleshooting a run without requiring direct DB access.",
        ])

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", readme)
            zf.writestr("manifest.json", json.dumps(_json_safe(manifest), indent=2))
            zf.writestr("recent_events.json", json.dumps(_json_safe(events_list), indent=2))
            zf.writestr("recent_events.txt", "\n".join(lines))
            for name, payload in snapshots.items():
                zf.writestr(f"snapshots/{name}.json", json.dumps(_json_safe(payload), indent=2))
            for path, text in log_texts.items():
                if text:
                    zf.writestr(path, text)

        buffer.seek(0)
        return Response(
            buffer.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename={bundle_name}"},
        )
    except Exception as e:
        return api_server._api_error(e, request.path)
