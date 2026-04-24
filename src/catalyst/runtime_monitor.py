"""Passive sidecar watchdog that observes bot state and emits derived warnings

Polls structured events from the database, wallet/DB/Dexie alignment, and
slow-call samples parsed from the current superlog. Correlates these signals
into human-readable warnings and recovery hints surfaced over the existing
GUI log stream, so operators can see what's happening without a manual audit.

Key responsibilities:
    - Tail `database.log_event()` output for categorised anomalies
    - Scan superlog entries for slow-call / performance regressions
    - Cross-check wallet, DB, and Dexie views for inventory misalignment
    - Emit derived warning and recovery-hint events back into the log stream

This module is strictly read-only — it does not repair state. Corrective
actions live in `bot_health.py`, its active sister module.
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Deque, Dict, List, Optional

from config import cfg
from database import get_events_since, get_open_offers, log_event
from event_taxonomy import EventCategory, categorize_event


_SLOW_CALL_RE = re.compile(
    r"\[\s*[^\]]+\]\s+\[[^\]]+\]\s+\[[^\]]+\]\s+\[\s*(?P<level>[A-Z]+)\]\s+"
    r"\[(?P<category>[^\]]+)\]\s+<<<\s+(?P<method>[^\s]+)\s+::\s+time_ms=(?P<ms>[\d.]+)"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _price_gap_bps(reference: Decimal, ours: Decimal) -> str:
    if reference <= 0 or ours <= 0:
        return "0"
    mid = (reference + ours) / Decimal("2")
    if mid <= 0:
        return "0"
    gap = abs(reference - ours) / mid * Decimal("10000")
    return str(gap.quantize(Decimal("0.1")))


def _tier_template_counts() -> Dict[str, int]:
    return {
        "inner": int(getattr(cfg, "INNER_TIER_COUNT", 0) or 0),
        "mid": int(getattr(cfg, "MID_TIER_COUNT", 0) or 0),
        "outer": int(getattr(cfg, "OUTER_TIER_COUNT", 0) or 0),
        "extreme": int(getattr(cfg, "EXTREME_TIER_COUNT", 0) or 0),
    }


def _blank_tier_counts() -> Dict[str, int]:
    return {tier: 0 for tier in ("inner", "mid", "outer", "extreme")}


class RuntimeMonitor:
    def __init__(self, bot):
        self._bot = bot
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._interval_secs = max(10, int(getattr(cfg, "RUNTIME_MONITOR_POLL_SECS", 20) or 20))
        self._dexie_grace_secs = max(30, int(getattr(cfg, "RUNTIME_MONITOR_DEXIE_GRACE_SECS", 120) or 120))
        self._topup_warn_secs = max(120, int(getattr(cfg, "RUNTIME_MONITOR_TOPUP_WARN_SECS", 900) or 900))
        self._stale_warn_polls = max(1, int(getattr(cfg, "RUNTIME_MONITOR_STALE_POLLS", 2) or 2))
        self._enabled = bool(getattr(cfg, "RUNTIME_MONITOR_ENABLED", True))
        self._service_running = False
        self._wake_event = threading.Event()
        self._superlog_path = ""
        self._superlog_offset = 0
        self._superlog_bootstrap_done = False
        self._last_event_ts = ""
        self._bootstrapped_events = False
        self._recent_actions: Deque[Dict] = deque(maxlen=30)
        self._recent_findings: Deque[Dict] = deque(maxlen=20)
        self._slow_samples: Dict[str, Deque[float]] = {}
        self._slow_last_ms: Dict[str, float] = {}
        self._slow_active: set[str] = set()
        # Tracks when each condition was last logged so min_refire_secs can
        # suppress spurious re-notifications when a condition briefly clears
        # and re-triggers within a short window.
        self._condition_last_logged: Dict[str, float] = {}
        # Per-condition last signature, for conditions that pass one (e.g.
        # ladder shape drift). When the signature matches a recent firing,
        # suppress the alert — nothing changed that the user would act on.
        self._condition_last_signature: Dict[str, str] = {}
        self._conditions: Dict[str, bool] = {
            "wallet_sync_stale": False,
            "db_wallet_divergence": False,
            "dexie_visibility_gap": False,
            "ladder_shape_drift": False,
            "coin_headroom_low": False,
            "topup_lag": False,
            "slow_runtime": False,
        }
        self._streaks: Dict[str, int] = {
            "wallet_sync_stale": 0,
            "db_wallet_divergence": 0,
            "dexie_visibility_gap": 0,
            "ladder_shape_drift": 0,
            "coin_headroom_low": 0,
        }
        self._topup_started_at = 0.0
        self._topup_baseline: Dict[str, int] = {}
        self._last_post_activity_at = 0.0
        self._last_fill_activity_at = 0.0
        self._dexie_reconcile_grace_until = 0.0
        self._last_status = "idle"
        self._state: Dict = {}
        self._slow_thresholds = {
            "sync_from_wallet": {"threshold_ms": 2500.0, "hits": 3},
            "update_coin_counts": {"threshold_ms": 9000.0, "hits": 3},
            "reconcile_with_wallet": {"threshold_ms": 18000.0, "hits": 2},
            "_run_one_cycle": {"threshold_ms": 30000.0, "hits": 2},
            "get_tibet_pool_info": {"threshold_ms": 900.0, "hits": 3},
        }
        self._session_cutoff_ts = _utc_now_iso()

    def reset_session(self):
        with self._lock:
            self._recent_actions.clear()
            self._recent_findings.clear()
            self._slow_samples.clear()
            self._slow_last_ms.clear()
            self._slow_active.clear()
            for key in self._conditions:
                self._conditions[key] = False
            for key in self._streaks:
                self._streaks[key] = 0
            self._topup_started_at = 0.0
            self._topup_baseline = {}
            self._last_post_activity_at = 0.0
            self._last_fill_activity_at = 0.0
            self._dexie_reconcile_grace_until = 0.0
            self._superlog_path = ""
            self._superlog_offset = 0
            self._superlog_bootstrap_done = False
            self._last_event_ts = ""
            self._bootstrapped_events = False
            self._last_status = "idle"
            self._state = {}
            self._session_cutoff_ts = _utc_now_iso()

    def start(self):
        if not self._enabled:
            return
        self._service_running = True
        self._wake_event.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="bot-health-watch",
        )
        self._thread.start()

    def stop(self):
        self._service_running = False
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None

    def _set_state_quiet(self, status: str, note: str = ""):
        with self._lock:
            if self._state.get("status") == status and self._state.get("note") == note:
                return
            self._state = {
                "status": status,
                "note": note,
                "active_conditions": [],
                "updated_at": _utc_now_iso(),
                "poll_interval_secs": self._interval_secs,
                "enabled": self._enabled,
                "recent_actions": list(self._recent_actions),
                "recent_findings": list(self._recent_findings),
            }

    def get_state(self) -> Dict:
        with self._lock:
            state = dict(self._state)
            state["recent_actions"] = list(self._recent_actions)
            state["recent_findings"] = list(self._recent_findings)
            return state

    def _run(self):
        if not self._enabled:
            return
        log_event("info", "bot_health_watch_started",
                  f"Bot health watch started (polling every {self._interval_secs}s)")
        while self._service_running:
            if not self._bot._running:
                self._sync_alert([], "healthy")
                self._set_state_quiet("idle", "Waiting for bot start")
                self._wake_event.wait(timeout=1)
                self._wake_event.clear()
                continue
            if not self._bot._startup_complete.is_set():
                self._sync_alert([], "healthy")
                self._set_state_quiet("warming_up", "Bot is starting up")
                self._wake_event.wait(timeout=1)
                self._wake_event.clear()
                continue
            try:
                self._run_once()
            except Exception as e:
                log_event("warning", "bot_health_watch_error",
                          f"Bot health watch check failed: {str(e)[:180]}")
            for _ in range(self._interval_secs):
                if not self._service_running:
                    break
                self._wake_event.wait(timeout=1)
                self._wake_event.clear()
        log_event("info", "bot_health_watch_exit", "Bot health watch stopped")

    def _run_once(self):
        self._bootstrap_recent_events()
        self._ingest_new_events()
        self._ingest_superlog()

        snapshot = self._collect_snapshot()
        active_conditions = self._evaluate(snapshot)
        status = self._summarize_status(active_conditions)
        snapshot["status"] = status
        snapshot["active_conditions"] = active_conditions
        snapshot["updated_at"] = _utc_now_iso()
        snapshot["poll_interval_secs"] = self._interval_secs
        snapshot["enabled"] = self._enabled

        with self._lock:
            self._state = snapshot

        self._sync_alert(active_conditions, status)

    def _bootstrap_recent_events(self):
        if self._bootstrapped_events:
            return
        recent = list(reversed(get_events_since(self._session_cutoff_ts, limit=200)))
        for event in recent:
            self._handle_event(event)
            self._last_event_ts = event.get("timestamp", self._last_event_ts)
        if not self._last_event_ts:
            self._last_event_ts = self._session_cutoff_ts
        self._bootstrapped_events = True

    def _ingest_new_events(self):
        if not self._last_event_ts:
            self._last_event_ts = _utc_now_iso()
            return
        rows = list(reversed(get_events_since(self._last_event_ts, limit=300)))
        for row in rows:
            self._handle_event(row)
            self._last_event_ts = row.get("timestamp", self._last_event_ts)

    def _handle_event(self, event: Dict):
        event_type = str(event.get("event_type") or "")
        severity = str(event.get("severity") or "info")
        message = str(event.get("message") or "")
        timestamp = str(event.get("timestamp") or _utc_now_iso())

        # Resolve category — use stored column value if present, otherwise
        # compute it on the fly so older rows (before taxonomy migration) work.
        raw_cat = event.get("event_category") or ""
        category: str = raw_cat if raw_cat else str(categorize_event(event_type))

        interesting = (
            severity in ("warning", "error")
            # Explicit high-value event types (kept for precision):
            or event_type in {
                "wallet_sync",
                "wallet_sync_live_again",
                "offer_created",
                "offer_create_failed",
                "dexie_flush_result",
                "dexie_repost_done",
                "dexie_repost_background",
                "splash_flush_result",
                "splash_repost_background",
                "splash_repost_done",
                "coin_prep_started",
                "coin_prep_failed",
                "topup_trigger",
                "health_topup_trigger",
                "topup_timeout",
                "spacescan_fill_confirmed",
                "sage_fill_backfill",
                "mass_disappearance_guard",
                "mass_disappearance_accepted",
            }
            # Category-based catch-all — surfaces any new event types that
            # belong to important categories without needing explicit listing.
            or category in (EventCategory.RISK, EventCategory.OFFER,
                            EventCategory.LIFECYCLE)
        )
        if interesting:
            self._recent_actions.append({
                "timestamp": timestamp,
                "severity": severity,
                "event_type": event_type,
                "category": category,
                "message": message[:240],
            })

        # --- Activity tracking (category-aware) ---
        # Post activity: any EXCHANGE or OFFER event signals the ladder is live
        if category in (EventCategory.EXCHANGE, EventCategory.OFFER) or event_type in {
            "offer_created",
            "dexie_flush_result",
            "dexie_repost_done",
            "splash_flush_result",
            "splash_repost_done",
        }:
            self._last_post_activity_at = time.time()

        if event_type in {"dexie_repost_background", "connectivity_recovery"}:
            self._dexie_reconcile_grace_until = max(
                self._dexie_reconcile_grace_until,
                time.time() + max(180, self._dexie_grace_secs),
            )
        if event_type == "dexie_repost_done":
            self._dexie_reconcile_grace_until = 0.0

        # Fill activity: OFFER category fills + explicit fill event types
        if category == EventCategory.OFFER and "fill" in event_type:
            self._last_fill_activity_at = time.time()
        elif event_type in {"spacescan_fill_confirmed", "sage_fill_backfill"}:
            self._last_fill_activity_at = time.time()

        # Coin prep tracking (COIN category catches future prep event names)
        if category == EventCategory.COIN or event_type in {
            "topup_trigger", "health_topup_trigger", "coin_prep_started"
        }:
            if "started" in event_type or "trigger" in event_type:
                if self._topup_started_at <= 0:
                    free_counts = self._safe_free_coin_counts(
                        int(self._bot._bot_state.get("open_buys", 0) or 0),
                        int(self._bot._bot_state.get("open_sells", 0) or 0),
                    )
                    self._topup_baseline = {
                        "xch_free": free_counts.get("xch_free", 0),
                        "cat_free": free_counts.get("cat_free", 0),
                    }
                    self._topup_started_at = time.time()

        if event_type in {"coin_prep_failed", "topup_timeout"}:
            self._topup_started_at = 0.0
            self._topup_baseline = {}

    def _ingest_superlog(self):
        path = self._resolve_superlog_path()
        if not path:
            return

        try:
            size = os.path.getsize(path)
        except OSError:
            return

        if path != self._superlog_path:
            self._superlog_path = path
            self._superlog_offset = 0
            self._superlog_bootstrap_done = False

        if not self._superlog_bootstrap_done:
            self._superlog_offset = size
            self._superlog_bootstrap_done = True
            return

        if self._superlog_offset > size:
            self._superlog_offset = 0

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(self._superlog_offset)
                lines = handle.readlines()
                self._superlog_offset = handle.tell()
        except OSError:
            return

        now = time.time()
        for line in lines[-400:]:
            match = _SLOW_CALL_RE.search(line)
            if not match:
                continue
            method = match.group("method")
            ms = float(match.group("ms"))
            threshold = self._slow_thresholds.get(method)
            if not threshold:
                continue
            self._slow_last_ms[method] = ms
            if ms < threshold["threshold_ms"]:
                continue
            bucket = self._slow_samples.setdefault(method, deque(maxlen=10))
            bucket.append(now)

        for method, bucket in list(self._slow_samples.items()):
            while bucket and (now - bucket[0]) > 300:
                bucket.popleft()
            if not bucket:
                self._slow_samples.pop(method, None)

    def _resolve_superlog_path(self) -> str:
        try:
            from super_log import get_log_path

            path = str(get_log_path() or "").strip()
            if path and os.path.exists(path):
                return path
        except Exception:
            pass

        try:
            from user_paths import log_dir as _user_log_dir
            pattern = os.path.join(_user_log_dir(), "bot_superlog_*.log")
        except Exception:
            pattern = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_superlog_*.log")
        matches = sorted(glob.glob(pattern))
        return matches[-1] if matches else ""

    def _collect_snapshot(self) -> Dict:
        wallet_buy = int(self._bot._bot_state.get("open_buys", 0) or 0)
        wallet_sell = int(self._bot._bot_state.get("open_sells", 0) or 0)
        wallet_meta = {}
        try:
            wallet_meta = self._bot.offer_manager.get_wallet_sync_meta() or {}
        except Exception:
            wallet_meta = {}

        open_offers = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
        db_buy = sum(1 for offer in open_offers if offer.get("side") == "buy")
        db_sell = sum(1 for offer in open_offers if offer.get("side") == "sell")
        # Count offers where a cancel RPC has been sent but not yet confirmed.
        # These are still status='open' in the DB but lifecycle_state='cancel_requested'.
        # Surfacing this count helps the dashboard distinguish "cancellation in progress"
        # from confirmed cancelled offers.
        db_cancel_pending = sum(
            1 for offer in open_offers
            if offer.get("lifecycle_state") == "cancel_requested"
        )
        db_tier_counts = {
            "buy": _blank_tier_counts(),
            "sell": _blank_tier_counts(),
        }
        for offer in open_offers:
            side = str(offer.get("side") or "").lower()
            tier = str(offer.get("tier") or "mid").lower()
            if side in db_tier_counts and tier in db_tier_counts[side]:
                db_tier_counts[side][tier] += 1

        orderbook_summary = {}
        orderbook_snapshot = {}
        try:
            self._bot.market_intel.refresh_orderbook(force=False)
            orderbook_summary = self._bot.market_intel.get_market_summary() or {}
            orderbook_snapshot = self._bot.market_intel.get_orderbook_snapshot() or {}
        except Exception:
            orderbook_summary = {}
            orderbook_snapshot = {}

        our_best_bid = _coerce_decimal(orderbook_snapshot.get("our_best_bid"))
        our_best_ask = _coerce_decimal(orderbook_snapshot.get("our_best_ask"))
        competitor_bid = _coerce_decimal(orderbook_summary.get("best_bid"))
        competitor_ask = _coerce_decimal(orderbook_summary.get("best_ask"))

        coin_status = self._bot.coin_manager.get_status()
        free_counts = self._safe_free_coin_counts(wallet_buy, wallet_sell)

        # Gap closer awareness — cancel+create operations take 60-90s on-chain
        # and temporarily diverge DB/wallet counts and tier shapes.  Suppress
        # the warnings that would fire during normal gap closer operation.
        gap_closer_active = False
        try:
            gap_closer_active = bool(
                self._bot.boost_manager and self._bot.boost_manager._boost_active
            )
        except Exception:
            pass

        performance = {
            "latest_ms": {k: round(v, 1) for k, v in self._slow_last_ms.items()},
            "active_methods": [],
        }

        return {
            "market": {
                "gap_closer_active": gap_closer_active,
                "wallet_buy": wallet_buy,
                "wallet_sell": wallet_sell,
                "db_buy": db_buy,
                "db_sell": db_sell,
                "db_cancel_pending": db_cancel_pending,
                "dexie_our_buy": int(orderbook_snapshot.get("our_buy_count", 0) or 0),
                "dexie_our_sell": int(orderbook_snapshot.get("our_sell_count", 0) or 0),
                "dexie_total_buy": int(orderbook_snapshot.get("buy_count", 0) or 0),
                "dexie_total_sell": int(orderbook_snapshot.get("sell_count", 0) or 0),
                "dexie_page_size": int(orderbook_snapshot.get("page_size", 0) or 0),
                "dexie_buy_truncated": bool(orderbook_snapshot.get("buy_truncated", False)),
                "dexie_sell_truncated": bool(orderbook_snapshot.get("sell_truncated", False)),
                "best_competitor_bid": str(competitor_bid),
                "best_competitor_ask": str(competitor_ask),
                "our_best_bid": str(our_best_bid),
                "our_best_ask": str(our_best_ask),
                "our_bid_gap_bps": _price_gap_bps(competitor_bid, our_best_bid),
                "our_ask_gap_bps": _price_gap_bps(competitor_ask, our_best_ask),
                "orderbook_age_secs": orderbook_summary.get("orderbook_age_secs", 0),
                "orderbook_errors": orderbook_summary.get("orderbook_errors", 0),
                "orderbook_refreshes": int(orderbook_summary.get("orderbook_refreshes", 0) or 0),
                "wallet_sync_fresh": bool(wallet_meta.get("fresh", True)),
                "wallet_sync_using_cache": bool(wallet_meta.get("using_cache", False)),
                "wallet_sync_failures": int(wallet_meta.get("consecutive_failures", 0) or 0),
                "wallet_sync_error": str(wallet_meta.get("last_error") or ""),
                "dexie_queue_size": int(self._bot.dexie_manager.get_stats().get("queue_size", 0) or 0),
                "db_buy_tiers": dict(db_tier_counts["buy"]),
                "db_sell_tiers": dict(db_tier_counts["sell"]),
            },
            "coins": {
                "xch_spendable": int(free_counts.get("xch_spendable", 0) or 0),
                "cat_spendable": int(free_counts.get("cat_spendable", 0) or 0),
                "xch_free": int(free_counts.get("xch_free", 0) or 0),
                "cat_free": int(free_counts.get("cat_free", 0) or 0),
                "xch_locked": int(coin_status.get("xch_locked_coins", 0) or 0),
                "cat_locked": int(coin_status.get("cat_locked_coins", 0) or 0),
                "prep_running": bool(coin_status.get("prep_running")),
                "topup_running": bool(coin_status.get("topup_running")),
                "inventory": coin_status.get("inventory", {}),
            },
            "performance": performance,
            "bot": {
                "loop_count": int(self._bot._loop_count or 0),
                "loop_duration_secs": round(float(self._bot._last_loop_duration or 0), 2),
                "mid_price": str(self._bot._current_mid_price),
                "started_at": self._bot._start_time,
                "last_post_activity_secs_ago": round(max(0.0, time.time() - self._last_post_activity_at), 1)
                if self._last_post_activity_at > 0 else None,
                "last_fill_activity_secs_ago": round(max(0.0, time.time() - self._last_fill_activity_at), 1)
                if self._last_fill_activity_at > 0 else None,
            },
        }

    def _safe_free_coin_counts(self, wallet_buy: int = 0, wallet_sell: int = 0) -> Dict[str, int]:
        try:
            return self._bot.coin_manager.get_free_coin_counts(wallet_buy, wallet_sell)
        except Exception:
            return {
                "xch_spendable": 0,
                "cat_spendable": 0,
                "xch_free": 0,
                "cat_free": 0,
            }

    def _evaluate(self, snapshot: Dict) -> List[Dict]:
        now = time.time()
        market = snapshot["market"]
        coins = snapshot["coins"]
        performance = snapshot["performance"]
        active_conditions: List[Dict] = []

        gap_closer_running = bool(market.get("gap_closer_active"))
        for method, cfg_row in self._slow_thresholds.items():
            bucket = self._slow_samples.get(method, deque())
            if len(bucket) >= int(cfg_row["hits"]):
                self._slow_active.add(method)
            else:
                self._slow_active.discard(method)
        # Gap closer cancel+create operations take 60-90s on-chain — don't
        # flag the overall cycle as slow while it's doing its job.
        if gap_closer_running:
            self._slow_active.discard("_run_one_cycle")

        performance["active_methods"] = [
            {
                "method": method,
                "last_ms": round(self._slow_last_ms.get(method, 0.0), 1),
                "threshold_ms": self._slow_thresholds[method]["threshold_ms"],
            }
            for method in sorted(self._slow_active)
        ]

        # 180s grace: the initial ladder burst creates 70+ offers in ~20s, but
        # wallet lock propagation and the first full wallet-sync cycle can take
        # another 60-90s after that. 90s was too short — it fired the
        # divergence warning at ~2.5 min even though everything self-healed
        # within 20s. 180s covers the full post-ladder propagation window.
        startup_grace = now < (float(self._bot._start_time or now) + 180.0)
        wallet_fresh = bool(market.get("wallet_sync_fresh", True))

        stale_active = (not wallet_fresh)
        self._update_streak("wallet_sync_stale", stale_active)
        if self._apply_condition(
            "wallet_sync_stale",
            self._streaks["wallet_sync_stale"] >= self._stale_warn_polls,
            severity="warning",
            open_event="bot_health_wallet_stale",
            open_message=(
                "Wallet sync is stale or cached; live offer view may be unreliable "
                f"(failures={market.get('wallet_sync_failures', 0)}, "
                f"using_cache={market.get('wallet_sync_using_cache')})"
            ),
            close_event="bot_health_wallet_fresh",
            close_message="Wallet sync is fresh again",
        ):
            active_conditions.append(self._condition_entry(
                "wallet_sync_stale",
                "warning",
                "Wallet sync is stale/cached",
                detail=(
                    f"Wallet sync is stale/cached: failures={market.get('wallet_sync_failures', 0)}, "
                    f"using_cache={market.get('wallet_sync_using_cache')}"
                ),
            ))

        gap_total = abs(int(market["wallet_buy"]) - int(market["db_buy"])) + abs(
            int(market["wallet_sell"]) - int(market["db_sell"])
        )
        gap_closer_active = bool(market.get("gap_closer_active"))
        divergence_active = (not startup_grace) and wallet_fresh and gap_total >= 2 and not gap_closer_active
        self._update_streak("db_wallet_divergence", divergence_active)
        if self._apply_condition(
            "db_wallet_divergence",
            self._streaks["db_wallet_divergence"] >= 2,
            severity="warning",
            open_event="bot_health_db_wallet_gap",
            open_message=(
                f"DB and wallet offer counts diverged: wallet {market['wallet_buy']}/{market['wallet_sell']} "
                f"vs DB {market['db_buy']}/{market['db_sell']}"
            ),
            close_event="bot_health_db_wallet_ok",
            close_message="DB and wallet offer counts are aligned again",
        ):
            active_conditions.append(self._condition_entry(
                "db_wallet_divergence",
                "warning",
                "Wallet and DB offer counts differ",
                detail=(
                    f"Wallet {market['wallet_buy']}/{market['wallet_sell']} vs "
                    f"DB {market['db_buy']}/{market['db_sell']}"
                ),
            ))

        # The Dexie orderbook snapshot is capped to a page size, so counts on a
        # full top-of-book page are not authoritative for "missing offer" checks.
        dexie_gap_buy = 0 if bool(market.get("dexie_buy_truncated")) else max(
            0, int(market["wallet_buy"]) - int(market["dexie_our_buy"])
        )
        dexie_gap_sell = 0 if bool(market.get("dexie_sell_truncated")) else max(
            0, int(market["wallet_sell"]) - int(market["dexie_our_sell"])
        )
        dexie_visible = (
            not startup_grace
            and wallet_fresh
            and bool(getattr(cfg, "DEXIE_AUTO_POST", True))
            and bool(getattr(cfg, "DEXIE_POST_ENABLED", True))
            and int(market["dexie_queue_size"]) == 0
            and int(market["orderbook_refreshes"]) > 0
            and float(market.get("orderbook_age_secs", 0) or 0) < max(90, self._dexie_grace_secs)
            and (now - float(self._last_post_activity_at or 0)) > self._dexie_grace_secs
            and now >= float(self._dexie_reconcile_grace_until or 0)
        )
        self._update_streak("dexie_visibility_gap", dexie_visible and (dexie_gap_buy + dexie_gap_sell) >= 3)
        if self._apply_condition(
            "dexie_visibility_gap",
            self._streaks["dexie_visibility_gap"] >= 2,
            severity="warning",
            open_event="bot_health_dexie_gap",
            open_message=(
                f"Dexie live visibility lags wallet offers: wallet {market['wallet_buy']}/{market['wallet_sell']} "
                f"vs Dexie {market['dexie_our_buy']}/{market['dexie_our_sell']}"
            ),
            close_event="bot_health_dexie_ok",
            close_message="Dexie live offer counts are aligned with wallet offers again",
        ):
            active_conditions.append(self._condition_entry(
                "dexie_visibility_gap",
                "warning",
                "Dexie live counts are behind wallet offers",
                detail=(
                    f"Dexie {market['dexie_our_buy']}/{market['dexie_our_sell']} vs "
                    f"wallet {market['wallet_buy']}/{market['wallet_sell']}"
                ),
            ))

        tier_template = _tier_template_counts()
        expected_total = sum(tier_template.values())
        ladder_shape_mismatches: List[str] = []
        if (
            not startup_grace
            and wallet_fresh
            and bool(getattr(cfg, "TIER_ENABLED", False))
            and expected_total > 0
            and not coins.get("prep_running")
            and not coins.get("topup_running")
            and (now - float(self._last_post_activity_at or 0)) > max(45, self._dexie_grace_secs // 2)
        ):
            for side in ("buy", "sell"):
                actual = market.get(f"db_{side}_tiers", {}) or {}
                actual_total = sum(int(actual.get(tier, 0) or 0) for tier in tier_template)
                if actual_total != expected_total:
                    continue
                mismatched = [
                    f"{tier} {int(actual.get(tier, 0) or 0)}/{target}"
                    for tier, target in tier_template.items()
                    if int(actual.get(tier, 0) or 0) != target
                ]
                if mismatched:
                    ladder_shape_mismatches.append(f"{side} " + ", ".join(mismatched))

        self._update_streak("ladder_shape_drift", bool(ladder_shape_mismatches) and not gap_closer_active)
        # Signature = the exact mismatch breakdown. If the same signature
        # re-fires within 2 hours, suppress the alert — the misfit mix hasn't
        # changed (no fills, no reshape), so re-notifying is pure noise.
        # Post-mortem 2026-04-22: this condition was flapping every ~30 min
        # with an identical mismatch string because the underlying cause
        # (mid tier stuck at 0 + misfit-sized inner coins) couldn't resolve
        # without fills, and the bot has no fills on a quiet market.
        ladder_shape_signature = " | ".join(ladder_shape_mismatches)
        if self._apply_condition(
            "ladder_shape_drift",
            self._streaks["ladder_shape_drift"] >= 2,
            severity="warning",
            open_event="bot_health_ladder_shape",
            open_message=(
                "Live ladder tier mix differs from the configured template even though the side totals are full: "
                + " | ".join(ladder_shape_mismatches[:2])
            ),
            close_event="bot_health_ladder_shape_ok",
            close_message="Live ladder tier mix matches the configured template again",
            min_refire_secs=1800,  # max one re-notification per 30 min for transient shape drift
            signature=ladder_shape_signature,
            signature_refire_secs=7200,  # 2h — if same shape, don't renotify
        ):
            active_conditions.append(self._condition_entry(
                "ladder_shape_drift",
                "warning",
                "Live ladder tier mix does not match the configured template",
                detail=" | ".join(ladder_shape_mismatches[:2]),
            ))

        low_spares = (
            not startup_grace
            and not coins.get("prep_running")
            and not coins.get("topup_running")
            and (
                (bool(getattr(cfg, "ENABLE_BUY", True)) and int(coins["xch_free"]) <= 1)
                or (bool(getattr(cfg, "ENABLE_SELL", True)) and int(coins["cat_free"]) <= 1)
            )
        )
        self._update_streak("coin_headroom_low", low_spares)
        if self._apply_condition(
            "coin_headroom_low",
            self._streaks["coin_headroom_low"] >= 3,
            severity="info",
            open_event="bot_health_coin_headroom_low",
            open_message=(
                f"Coin headroom is low while book is live: free XCH={coins['xch_free']}, "
                f"free CAT={coins['cat_free']}, locked XCH={coins['xch_locked']}, "
                f"locked CAT={coins['cat_locked']}"
            ),
            close_event="bot_health_coin_headroom_ok",
            close_message="Coin headroom recovered",
        ):
            active_conditions.append(self._condition_entry(
                "coin_headroom_low",
                "info",
                "Coin headroom is low",
                detail=(
                    f"Free XCH={coins['xch_free']}, free CAT={coins['cat_free']}, "
                    f"locked XCH={coins['xch_locked']}, locked CAT={coins['cat_locked']}"
                ),
            ))

        topup_running = bool(coins.get("prep_running")) or bool(coins.get("topup_running"))
        if topup_running and self._topup_started_at <= 0:
            self._topup_started_at = now
            self._topup_baseline = {
                "xch_free": int(coins["xch_free"]),
                "cat_free": int(coins["cat_free"]),
            }
        if not topup_running and self._topup_started_at > 0:
            improved = (
                int(coins["xch_free"]) > int(self._topup_baseline.get("xch_free", 0))
                or int(coins["cat_free"]) > int(self._topup_baseline.get("cat_free", 0))
            )
            if improved:
                log_event("success", "bot_health_topup_recovered",
                          f"Coin prep/top-up finished with improved free coins: "
                          f"XCH {self._topup_baseline.get('xch_free', 0)}->{coins['xch_free']}, "
                          f"CAT {self._topup_baseline.get('cat_free', 0)}->{coins['cat_free']}")
            self._topup_started_at = 0.0
            self._topup_baseline = {}

        topup_lag_active = (
            topup_running
            and self._topup_started_at > 0
            and (now - self._topup_started_at) >= self._topup_warn_secs
            and int(coins["xch_free"]) <= int(self._topup_baseline.get("xch_free", 0))
            and int(coins["cat_free"]) <= int(self._topup_baseline.get("cat_free", 0))
        )
        if self._apply_condition(
            "topup_lag",
            topup_lag_active,
            severity="warning",
            open_event="bot_health_topup_lag",
            open_message=(
                "Coin prep/top-up is still running without improving free coin headroom "
                f"after {int(now - self._topup_started_at)}s"
            ),
            close_event="bot_health_topup_clear",
            close_message="Coin prep/top-up is no longer lagging",
        ):
            active_conditions.append(self._condition_entry(
                "topup_lag",
                "warning",
                "Coin prep/top-up is lagging",
                detail=f"Top-up is still running after {int(now - self._topup_started_at)}s without improving free coins",
            ))

        slow_detail = ", ".join(
            f"{method} {round(self._slow_last_ms.get(method, 0.0), 1)}ms"
            for method in sorted(
                self._slow_active,
                key=lambda item: self._slow_last_ms.get(item, 0.0),
                reverse=True,
            )[:3]
        )
        if self._apply_condition(
            "slow_runtime",
            (not startup_grace) and bool(self._slow_active),
            severity="warning",
            open_event="bot_health_perf_slow",
            open_message=(
                "Repeated slow runtime calls detected: "
                + ", ".join(
                    f"{method}={round(self._slow_last_ms.get(method, 0.0), 1)}ms"
                    for method in sorted(self._slow_active)
                )
            ),
            close_event="bot_health_perf_ok",
            close_message="Runtime slow-call alerts cleared",
        ):
            active_conditions.append(self._condition_entry(
                "slow_runtime",
                "warning",
                "Repeated slow runtime calls are active",
                detail=f"Slow calls: {slow_detail}" if slow_detail else "Repeated slow runtime calls are active",
            ))

        return active_conditions

    def _update_streak(self, key: str, active: bool):
        if key not in self._streaks:
            return
        if active:
            self._streaks[key] += 1
        else:
            self._streaks[key] = 0

    def _apply_condition(self, key: str, active: bool, severity: str,
                         open_event: str, open_message: str,
                         close_event: str, close_message: str,
                         min_refire_secs: float = 0.0,
                         signature: Optional[str] = None,
                         signature_refire_secs: float = 0.0) -> bool:
        """Evaluate a health condition and emit log events on transitions.

        min_refire_secs — if > 0, suppress the open-event log when the
        condition re-fires within this many seconds of the last firing.
        This prevents spurious re-notifications when a condition briefly
        clears for one health-check cycle and then re-triggers immediately.
        The active_conditions list (UI state) is unaffected.

        signature + signature_refire_secs — optional dedup key. If the
        same signature re-fires within signature_refire_secs, suppress the
        log (nothing materially changed for the user to act on). Used for
        persistent-state conditions like ladder-shape drift where the
        underlying misfit coin mix stays the same across re-firings because
        nothing in the environment has shifted (no fills, no reshape).
        """
        was_active = bool(self._conditions.get(key))
        self._conditions[key] = bool(active)
        now = time.monotonic()
        if active and not was_active:
            last_logged = self._condition_last_logged.get(key, 0.0)
            refire_ok = (
                min_refire_secs <= 0.0
                or (now - last_logged) >= min_refire_secs
            )
            sig_ok = True
            if signature is not None and signature_refire_secs > 0.0:
                last_sig = self._condition_last_signature.get(key, "")
                if last_sig == signature and (now - last_logged) < signature_refire_secs:
                    sig_ok = False
            if refire_ok and sig_ok:
                log_event(severity, open_event, open_message)
                self._condition_last_logged[key] = now
                if signature is not None:
                    self._condition_last_signature[key] = signature
                self._recent_findings.append({
                    "timestamp": _utc_now_iso(),
                    "severity": severity,
                    "code": key,
                    "message": open_message,
                })
        elif not active and was_active:
            log_event("success", close_event, close_message)
        return bool(active)

    def _condition_entry(self, code: str, severity: str, message: str, detail: Optional[str] = None) -> Dict:
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "detail": detail or message,
        }

    def _summarize_status(self, active_conditions: List[Dict]) -> str:
        if not active_conditions:
            self._last_status = "healthy"
            return "healthy"
        severity_rank = {"info": 0, "success": 0, "warning": 1, "error": 2}
        highest = max(active_conditions, key=lambda item: severity_rank.get(item.get("severity", "warning"), 1))
        self._last_status = "critical" if highest.get("severity") == "error" else "warning"
        return self._last_status

    def _sync_alert(self, active_conditions: List[Dict], status: str):
        if not active_conditions:
            self._bot._clear_alert("runtime_monitor")
            self._bot._clear_alert("bot_health_watch")
            return
        # Show the first 5 conditions (was 3). When more remain, append
        # an explicit overflow note so the operator knows there are more
        # problems beyond what the banner shows — previously the 4th+
        # condition was silently truncated and could go un-triaged while
        # the banner implied everything visible was the full story.
        max_visible = 5
        visible = active_conditions[:max_visible]
        overflow = max(0, len(active_conditions) - max_visible)
        lines = [item.get("detail") or item["message"] for item in visible]
        if overflow:
            lines.append(
                f"+{overflow} more condition{'s' if overflow != 1 else ''} "
                f"not shown — check the health log for the full list"
            )
        title = "Bot Health Warning" if status == "warning" else "Bot Health Critical"
        severity = "warning" if status == "warning" else "error"
        self._bot._emit_alert(
            "bot_health_watch",
            severity,
            title,
            " | ".join(lines),
        )

