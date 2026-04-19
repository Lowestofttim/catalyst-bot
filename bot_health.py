"""Active health-repair sister of `runtime_monitor` — detects and fixes drift

Where `runtime_monitor` only observes, this module performs corrective
actions when runtime invariants break. External systems are treated as the
source of truth: Dexie for offer state, Sage RPC for coin state, Spacescan
for on-chain confirmation; the bot's own DB is a hypothesis to be validated
and repaired against those sources.

Key responsibilities:
    - `check_pending_cancels` — reconcile DB pending-cancel rows
    - `check_orphan_locks` — release coin locks with no matching open offer
    - `check_stale_dexie_posts` — repost offers missing from Dexie
    - `check_ladder_overbuild` — trim ladders that have drifted past shape
    - `run_runtime_checks` — aggregator that runs the full suite

Repairs are gated by an `auto_repair` flag: low-risk fixes execute
automatically, while position- or fill-related anomalies are surfaced for
human review. Complementary to `runtime_monitor`, not superseded by it.
"""

from __future__ import annotations

import time
import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from config import cfg
from super_log import slog


# ── Dexie offer status codes (per https://dexie.space/docs) ────────────
DEXIE_STATUS_ACTIVE = 0
DEXIE_STATUS_PENDING = 1
DEXIE_STATUS_CANCELLED = 3
DEXIE_STATUS_COMPLETED = 4
DEXIE_STATUS_UNKNOWN = 5
DEXIE_STATUS_EXPIRED = 6


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """A single runtime health check result."""
    name: str
    category: str       # "offers", "coins", "position", "wallet"
    status: str         # "pass", "warn", "fail"
    severity: str       # "info", "warning", "error"
    message: str
    anomaly_count: int = 0
    repaired_count: int = 0
    repair_log: List[str] = field(default_factory=list)


@dataclass
class HealthReport:
    """Complete runtime-health report."""
    checks: List[HealthCheck] = field(default_factory=list)
    timestamp: float = 0.0
    duration_ms: float = 0.0
    auto_repair: bool = False

    @property
    def healthy(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def anomalies(self) -> int:
        return sum(c.anomaly_count for c in self.checks)

    @property
    def repaired(self) -> int:
        return sum(c.repaired_count for c in self.checks)

    @property
    def summary(self) -> str:
        return (f"{len(self.checks)} checks, {self.anomalies} anomalies, "
                f"{self.repaired} repaired")

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "summary": self.summary,
            "anomalies": self.anomalies,
            "repaired": self.repaired,
            "auto_repair": self.auto_repair,
            "timestamp": self.timestamp,
            "duration_ms": round(self.duration_ms, 1),
            "checks": [
                {
                    "name": c.name,
                    "category": c.category,
                    "status": c.status,
                    "severity": c.severity,
                    "message": c.message,
                    "anomaly_count": c.anomaly_count,
                    "repaired_count": c.repaired_count,
                    "repair_log": list(c.repair_log),
                }
                for c in self.checks
            ],
        }


# ── Dexie API helpers ──────────────────────────────────────────────────

def _dexie_get_offer(dexie_id: str, timeout: float = 10.0) -> Optional[dict]:
    """Fetch a single offer from Dexie. Returns the offer dict or None."""
    if not dexie_id:
        return None
    base = (getattr(cfg, "DEXIE_API_BASE", "") or "https://api.dexie.space").rstrip("/")
    url = f"{base}/v1/offers/{dexie_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MM_BOT/health"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if not data.get("success"):
            return None
        return data.get("offer")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None


# ── Check 1: pending-cancel verifier ───────────────────────────────────

# Backoff between cancel retries — the bulk-cancel path is fee=0 and may
# legitimately take a few minutes to confirm in low-priority mempool slots.
# Don't re-fire too aggressively or we'll just stack more zero-fee TXs.
_CANCEL_RETRY_BACKOFF_SECS = 300  # 5 minutes

# Initial grace period after cancel was sent — give the bulk TX time to
# confirm before declaring it a zombie. ~2 mempool windows.
_CANCEL_INITIAL_GRACE_SECS = 60


def check_pending_cancels(auto_repair: bool = True) -> HealthCheck:
    """Verify that DB pending-cancel offers really cancelled on-chain.

    A pending-cancel offer is one with lifecycle_state='cancel_requested'
    or 'cancel_sent' and status='open'. The bot fired a cancel RPC and is
    waiting for on-chain confirmation. This check uses Dexie as the source
    of truth — Dexie watches the mempool and on-chain state and reflects
    each offer's actual status within seconds.

    Anomaly types detected:
      A. Dexie says ACTIVE but DB says pending-cancel → real zombie, retry
         the cancel with a single-offer (priority-fee) RPC.
      B. Dexie says CANCELLED/EXPIRED → cancel succeeded, mark DB.
      C. Dexie says COMPLETED → offer was filled, NOT cancelled. Flag for
         the fill flow to handle (do not auto-process — needs careful
         coin/position bookkeeping that lives in fill_tracker).
      D. Dexie returns nothing / unreachable → leave as pending, try again
         next cycle.

    Repairs (when auto_repair=True):
      - For B: mark_offer_cancelled + transition_offer("cancel_confirmed")
      - For A (when age > 5 min since last attempt): re-issue cancel via
        the single-offer path with a priority fee (no aggregate-sig bug)
    """
    from database import (
        get_open_offers, update_offer_status, transition_offer, mark_cancel_attempted,
    )

    pending = get_open_offers(include_pending_cancel=True)
    pending = [o for o in pending
               if (o.get("lifecycle_state") or "") in ("cancel_requested", "cancel_sent")]

    if not pending:
        return HealthCheck(
            name="pending_cancels",
            category="offers",
            status="pass",
            severity="info",
            message="No pending cancels — all confirmed or none in flight.",
        )

    now = time.time()
    truly_zombie = []        # Dexie ACTIVE, ready for retry
    confirmed_cancelled = []  # Dexie CANCELLED/EXPIRED, just update DB
    suspected_fills = []     # Dexie COMPLETED — needs fill flow
    unreachable = []         # Dexie didn't answer — try again later

    for off in pending:
        dexie_id = off.get("dexie_id")
        tid = off.get("trade_id")
        if not dexie_id:
            unreachable.append((off, "no dexie_id"))
            continue

        dexie_off = _dexie_get_offer(dexie_id)
        if dexie_off is None:
            unreachable.append((off, "dexie unreachable"))
            continue

        st = dexie_off.get("status")
        if st in (DEXIE_STATUS_CANCELLED, DEXIE_STATUS_EXPIRED):
            confirmed_cancelled.append((off, st))
        elif st == DEXIE_STATUS_COMPLETED:
            suspected_fills.append((off, dexie_off))
        elif st in (DEXIE_STATUS_ACTIVE, DEXIE_STATUS_PENDING):
            truly_zombie.append((off, dexie_off))
        else:
            unreachable.append((off, f"dexie status {st}"))

    repair_log = []
    repaired = 0

    # --- Repair B: confirm cancellations ---
    if auto_repair and confirmed_cancelled:
        for off, st in confirmed_cancelled:
            tid = off.get("trade_id")
            try:
                update_offer_status(tid, "cancelled")
                try:
                    transition_offer(tid, "cancel_confirmed")
                except Exception:
                    pass
                msg = f"confirmed_cancelled tid={tid[:16]}... (dexie_status={st})"
                repair_log.append(msg)
                slog("BOT_HEALTH", msg, level="info")
                repaired += 1
            except Exception as e:
                slog("BOT_HEALTH",
                     f"Failed to mark confirmed-cancelled tid={tid[:16]}...: {e}",
                     level="warn")

    # --- Repair A: re-cancel zombies (with backoff) ---
    if auto_repair and truly_zombie:
        try:
            from wallet import cancel_offer
            from wallet_sage import get_effective_transaction_fee_mojos
        except Exception as imp_err:
            slog("BOT_HEALTH",
                 f"Cannot import cancel_offer for retry: {imp_err}",
                 level="warn")
            cancel_offer = None
            get_effective_transaction_fee_mojos = lambda: 0

        for off, dexie_off in truly_zombie:
            tid = off.get("trade_id")
            last_attempt = off.get("cancel_last_attempt_at")
            age = _seconds_since(last_attempt, now)

            if age is None:
                # First attempt was via fire-and-forget that didn't stamp the
                # column. Treat as unknown age — initial grace then retry.
                age = _CANCEL_INITIAL_GRACE_SECS + 1

            if age < _CANCEL_INITIAL_GRACE_SECS:
                continue  # let the original cancel TX settle first

            if age < _CANCEL_RETRY_BACKOFF_SECS:
                # Still within retry backoff window — wait
                continue

            if not cancel_offer:
                continue

            # Re-issue with single-offer path + priority fee. The single-
            # offer path is NOT subject to the BAD_AGGREGATE_SIGNATURE bug
            # that forces fee=0 on bulk, so we can pay a priority fee here.
            try:
                fee = max(int(get_effective_transaction_fee_mojos()), 0)
                result = cancel_offer(tid, secure=True, timeout=20, fee_mojos=fee)
                if result and result.get("success"):
                    method = (result.get("method") or "").strip()
                    msg = (f"re_cancelled tid={tid[:16]}... "
                           f"(dexie still ACTIVE after {int(age)}s, retry method={method})")
                    repair_log.append(msg)
                    slog("BOT_HEALTH", msg, level="info")
                    try:
                        mark_cancel_attempted(tid)
                    except Exception:
                        pass
                    repaired += 1
                else:
                    err = (result or {}).get("error") or "unknown"
                    slog("BOT_HEALTH",
                         f"Re-cancel RPC failed for tid={tid[:16]}...: {err}",
                         level="warn")
            except Exception as e:
                slog("BOT_HEALTH",
                     f"Re-cancel exception for tid={tid[:16]}...: {e}",
                     level="warn")

    # --- C: suspected fills — flag only, never auto-process ---
    for off, dexie_off in suspected_fills:
        tid = off.get("trade_id")
        slog("BOT_HEALTH",
             f"Offer tid={tid[:16]}... was marked pending-cancel but Dexie "
             f"reports COMPLETED — likely the offer filled before cancel "
             f"landed. Manual review recommended (fill_tracker should pick "
             f"this up but flagging here for visibility).",
             data={"trade_id": tid, "dexie_id": off.get("dexie_id")},
             level="warn")

    anomaly_count = (len(truly_zombie) + len(confirmed_cancelled)
                     + len(suspected_fills))

    # Build status
    if truly_zombie and not auto_repair:
        status = "fail"
        severity = "error"
    elif suspected_fills:
        status = "warn"
        severity = "warning"
    elif anomaly_count == 0 and not unreachable:
        status = "pass"
        severity = "info"
    else:
        status = "warn"
        severity = "warning"

    parts = []
    if confirmed_cancelled:
        parts.append(f"{len(confirmed_cancelled)} confirmed cancelled")
    if truly_zombie:
        parts.append(f"{len(truly_zombie)} still active on Dexie")
    if suspected_fills:
        parts.append(f"{len(suspected_fills)} suspected fills")
    if unreachable:
        parts.append(f"{len(unreachable)} unreachable")
    if not parts:
        parts.append("nothing pending")

    return HealthCheck(
        name="pending_cancels",
        category="offers",
        status=status,
        severity=severity,
        message=f"{len(pending)} pending — {', '.join(parts)}",
        anomaly_count=anomaly_count,
        repaired_count=repaired,
        repair_log=repair_log,
    )


def _seconds_since(iso_ts: Optional[str], now: float) -> Optional[float]:
    """Parse an ISO timestamp and return seconds elapsed, or None on failure."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return now - dt.timestamp()
    except (ValueError, TypeError):
        return None


# ── Check 2: orphan coin locks ─────────────────────────────────────────

# How long a coin can be locked with no matching open offer before we
# consider it stuck. The reconcile loop usually fixes this within a cycle
# or two, so anything > 5 min suggests a real drift (likely a cancel or
# fill that didn't propagate the lock release).
_ORPHAN_LOCK_AGE_SECS = 300


def check_orphan_locks(auto_repair: bool = True) -> HealthCheck:
    """Find coins marked locked in DB whose trade_id points to no open offer.

    Common causes:
      - Offer was cancelled but the coin's lock wasn't released (broken
        cancel path, partial transaction failure)
      - Offer was filled and removed but coin lineage tracking lagged
      - trade_id points to a stale/never-existed offer (rare data corruption)

    Repair: free the lock so the coin returns to its tier pool. This is
    safe because:
      - If a real offer DOES still hold the coin via Sage, the next
        reconcile cycle will re-lock it (bot trusts wallet state).
      - If the coin is genuinely free in Sage, freeing the DB lock is
        the correct action.
    """
    from database import get_connection, free_coin

    conn = get_connection()
    rows = conn.execute(
        "SELECT c.coin_id, c.wallet_type, c.amount_mojos, c.trade_id, "
        "       c.assigned_tier, c.last_seen "
        "FROM coins c "
        "WHERE c.status = 'locked' "
        "  AND (c.trade_id IS NULL "
        "       OR c.trade_id NOT IN ("
        "          SELECT trade_id FROM offers "
        "          WHERE status = 'open' AND trade_id IS NOT NULL"
        "       ))"
    ).fetchall()
    orphans = [dict(r) for r in rows]

    if not orphans:
        return HealthCheck(
            name="orphan_locks",
            category="coins",
            status="pass",
            severity="info",
            message="No orphan coin locks.",
        )

    # Filter by age — only act on locks older than the threshold so we
    # don't race in-flight reconcile updates.
    now = time.time()
    actionable = []
    too_fresh = 0
    for o in orphans:
        age = _seconds_since(o.get("last_seen"), now)
        if age is None or age >= _ORPHAN_LOCK_AGE_SECS:
            actionable.append(o)
        else:
            too_fresh += 1

    repair_log = []
    repaired = 0
    if auto_repair:
        for o in actionable:
            cid = o["coin_id"]
            try:
                if free_coin(cid):
                    msg = (f"freed_orphan_lock coin={cid[:18]}... "
                           f"wt={o['wallet_type']} amt={o['amount_mojos']:,} "
                           f"prior_trade={(o.get('trade_id') or 'none')[:16]}")
                    repair_log.append(msg)
                    slog("BOT_HEALTH", msg, level="info")
                    repaired += 1
            except Exception as e:
                slog("BOT_HEALTH",
                     f"Failed to free orphan lock {cid[:18]}...: {e}",
                     level="warn")

    if not actionable and too_fresh:
        return HealthCheck(
            name="orphan_locks", category="coins", status="pass",
            severity="info",
            message=f"{too_fresh} recent orphan locks (within "
                    f"{_ORPHAN_LOCK_AGE_SECS}s grace) — likely in-flight reconcile.",
            anomaly_count=0,
        )

    return HealthCheck(
        name="orphan_locks",
        category="coins",
        status="warn" if (actionable and not auto_repair) else "pass",
        severity="warning" if (actionable and not auto_repair) else "info",
        message=(f"{len(actionable)} orphan locks "
                 f"({too_fresh} too fresh to act on)"),
        anomaly_count=len(actionable),
        repaired_count=repaired,
        repair_log=repair_log,
    )


# ── Check 3: stale Dexie posts ─────────────────────────────────────────

# How long an offer can sit in DB with dexie_posted=0 before we re-queue.
# The Dexie posting queue normally drains within a cycle, so > 2 min means
# a post failure that didn't get queued for retry.
_DEXIE_POST_STALE_SECS = 120


def check_stale_dexie_posts(auto_repair: bool = True) -> HealthCheck:
    """Find open offers that never made it to Dexie.

    Some create paths (notably partial-failure recovery) can persist an
    offer to DB without queuing the Dexie post. Without a Dexie listing
    the offer exists in Sage but no one can find it on the open market —
    invisible inventory.

    Repair: re-queue via dexie_manager.queue_post(). Idempotent because
    the queue dedupes on trade_id.
    """
    from database import get_connection

    conn = get_connection()
    rows = conn.execute(
        "SELECT trade_id, side, tier, offer_bech32, created_at "
        "FROM offers "
        "WHERE status='open' AND dexie_posted=0 "
        "  AND offer_bech32 IS NOT NULL AND offer_bech32 != '' "
    ).fetchall()
    stale = []
    now = time.time()
    for r in rows:
        age = _seconds_since(r["created_at"], now)
        if age is not None and age >= _DEXIE_POST_STALE_SECS:
            stale.append(dict(r))

    if not stale:
        return HealthCheck(
            name="stale_dexie_posts",
            category="offers",
            status="pass",
            severity="info",
            message="No stale Dexie posts.",
        )

    repair_log = []
    repaired = 0
    if auto_repair:
        try:
            from dexie_manager import queue_post
        except Exception as e:
            slog("BOT_HEALTH",
                 f"dexie_manager unavailable for re-queue: {e}",
                 level="warn")
            queue_post = None

        if queue_post:
            for off in stale:
                tid = off["trade_id"]
                bech = off.get("offer_bech32")
                try:
                    queue_post(bech, tid)
                    msg = (f"requeued_dexie_post tid={tid[:16]}... "
                           f"side={off['side']} tier={off['tier']}")
                    repair_log.append(msg)
                    slog("BOT_HEALTH", msg, level="info")
                    repaired += 1
                except Exception as e:
                    slog("BOT_HEALTH",
                         f"Failed to re-queue Dexie post for {tid[:16]}...: {e}",
                         level="warn")

    return HealthCheck(
        name="stale_dexie_posts",
        category="offers",
        status="warn" if (stale and not auto_repair) else "pass",
        severity="warning" if (stale and not auto_repair) else "info",
        message=f"{len(stale)} offers older than {_DEXIE_POST_STALE_SECS}s "
                f"with no Dexie post.",
        anomaly_count=len(stale),
        repaired_count=repaired,
        repair_log=repair_log,
    )


# ── Check 4: ladder over-build (wallet count vastly exceeds DB tracked) ──

# How long a wallet count > target overage can persist before we cancel
# the excess. Bot's normal post-storm cleanup takes ~5 min via the verifier
# re-cancelling zombies; this catches the case where the wallet keeps
# growing because new offers are created faster than old ones get cancelled.
_OVERBUILD_GRACE_SECS = 60
# How far over the configured cap is "over-built". We allow some slack
# because requote storms create overlap before old offers cancel.
_OVERBUILD_RATIO = 1.30  # wallet > 1.30 × target → flag


def check_ladder_overbuild(auto_repair: bool = True) -> HealthCheck:
    """Detect when the wallet has far more offers than the DB tracks.

    During a requote storm the bot can create new offers faster than the
    old ones cancel — wallet count climbs above the configured ladder cap
    while DB stays accurate. The pending-cancel verifier resolves it
    eventually (re-cancels zombies), but in the meantime the over-built
    wallet is exposed and confuses observers (Dexie, dashboard, this bot).

    Detection: wallet_buy or wallet_sell > 1.30 × MAX_ACTIVE_BUY/SELL.

    Repair: defer to the existing pending_cancels verifier — we don't
    add a separate cancel path here because it risks racing with the
    requote machinery. Just flag prominently so the operator knows the
    bot is in a transient over-built state.
    """
    from config import cfg as _cfg
    from database import get_connection

    # F82 fix (2026-04-18): the effective ladder cap is the SUM of per-tier
    # counts (BUY_INNER_TIER_COUNT + BUY_MID_TIER_COUNT + ...) when those
    # are set by smart-defaults, NOT the legacy MAX_ACTIVE_BUY/SELL knobs
    # which often stay at the .env default (12) even when the live ladder
    # cap is 24. Without this, every healthy 24-offer ladder fired this
    # warning. Use the larger of the two to avoid false positives.
    def _effective_cap(prefix: str, fallback: int) -> int:
        try:
            tier_sum = sum(
                int(getattr(_cfg, f"{prefix}_{t}_TIER_COUNT", 0) or 0)
                for t in ("INNER", "MID", "OUTER", "EXTREME")
            )
        except Exception:
            tier_sum = 0
        return max(tier_sum, int(fallback or 0)) or fallback

    try:
        max_buy = _effective_cap("BUY", int(getattr(_cfg, "MAX_ACTIVE_BUY", 12) or 12))
        max_sell = _effective_cap("SELL", int(getattr(_cfg, "MAX_ACTIVE_SELL", 12) or 12))
    except Exception:
        max_buy = max_sell = 12

    # Read the bot's last-known wallet counts from the diagnostics state.
    # We can't query Sage directly here without a session; the bot loop
    # writes the latest wallet counts into the offers/coins tables on each
    # reconcile, so use the open-offer counts as a proxy for "what we
    # think is live" (they shouldn't diverge except during a storm).
    try:
        conn = get_connection()
        # F83 fix: exclude pending-cancel offers from the count. Those are
        # already on the way out via the verifier, so flagging them as
        # "overbuild" produces noise during requote storms. Match the
        # default semantics of database.get_open_offers().
        rows = conn.execute(
            "SELECT side, COUNT(*) as n FROM offers "
            "WHERE status='open' "
            "  AND (lifecycle_state IS NULL "
            "       OR lifecycle_state NOT IN ('cancel_requested', 'cancel_sent')) "
            "GROUP BY side"
        ).fetchall()
        db_counts = {r["side"]: int(r["n"]) for r in rows}
    except Exception:
        db_counts = {}

    db_buy = db_counts.get("buy", 0)
    db_sell = db_counts.get("sell", 0)

    buy_overbuild = db_buy > int(max_buy * _OVERBUILD_RATIO)
    sell_overbuild = db_sell > int(max_sell * _OVERBUILD_RATIO)

    if not (buy_overbuild or sell_overbuild):
        return HealthCheck(
            name="ladder_overbuild",
            category="offers",
            status="pass",
            severity="info",
            message=f"Ladder within bounds (buy {db_buy}/{max_buy}, sell {db_sell}/{max_sell}).",
        )

    parts = []
    if buy_overbuild:
        parts.append(f"buy {db_buy} > 1.30 × {max_buy}")
    if sell_overbuild:
        parts.append(f"sell {db_sell} > 1.30 × {max_sell}")

    return HealthCheck(
        name="ladder_overbuild",
        category="offers",
        status="warn",
        severity="warning",
        message=("Ladder over-built: " + ", ".join(parts)
                 + ". pending_cancels verifier will re-cancel zombies — "
                 + "monitor for recovery within ~5 min. If persistent, the "
                 + "requote machinery is creating faster than cancels confirm."),
        anomaly_count=int(buy_overbuild) + int(sell_overbuild),
    )


# ── Top-level runner ───────────────────────────────────────────────────

# Cache to avoid running checks more often than needed (the bot loop
# calls every cycle but checks need at most ~once per minute)
_last_run_lock_ts: float = 0.0
_last_report: Optional[HealthReport] = None
_MIN_INTERVAL_SECS_IDLE = 60.0   # normal throttle — full 60s between runs
_MIN_INTERVAL_SECS_BUSY = 15.0   # F84: when last run found pending cancels,
                                  # shorten the window so they get resolved
                                  # faster. The 5-hour zombie test showed
                                  # that waiting a full 60s after each cleanup
                                  # pass leaves offers mis-tracked for ~5 min
                                  # longer than necessary — the verifier
                                  # could be confirming them within 15s.


def run_runtime_checks(auto_repair: bool = True,
                       force: bool = False) -> HealthReport:
    """Run all runtime health checks and return a structured report.

    F84 (2026-04-18): adaptive throttle — when the last run found pending
    cancels (i.e. there's churn to resolve), the next run can fire after
    15s instead of 60s. When the last run was clean (no anomalies) we stay
    on the 60s idle cadence.
    """
    global _last_run_lock_ts, _last_report

    now = time.time()
    # Pick throttle based on last-run state — busy = pending work = fast
    # cycle; idle = nothing to do = slow cycle.
    if _last_report and any(
        c.name == "pending_cancels" and c.anomaly_count > 0
        for c in _last_report.checks
    ):
        throttle = _MIN_INTERVAL_SECS_BUSY
    else:
        throttle = _MIN_INTERVAL_SECS_IDLE

    if not force and _last_report and (now - _last_run_lock_ts) < throttle:
        return _last_report

    start = now
    report = HealthReport(timestamp=start, auto_repair=auto_repair)

    # Add new checks here as we identify recurring anomalies
    report.checks.append(check_pending_cancels(auto_repair=auto_repair))
    report.checks.append(check_orphan_locks(auto_repair=auto_repair))
    report.checks.append(check_stale_dexie_posts(auto_repair=auto_repair))
    report.checks.append(check_ladder_overbuild(auto_repair=auto_repair))

    report.duration_ms = (time.time() - start) * 1000.0
    _last_run_lock_ts = time.time()
    _last_report = report
    return report
