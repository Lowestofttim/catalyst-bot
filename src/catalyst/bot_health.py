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
from decimal import Decimal
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

    # --- C: suspected fills — attempt Spacescan-verified recovery ---
    # A pending-cancel row with Dexie status COMPLETED means the offer
    # was filled BEFORE our cancel landed. fill_tracker may have missed
    # it because the trade_id no longer appears in a "disappeared" set
    # (it's out of the _previous_ids window by now). Run Spacescan
    # directly; if it confirms a real fill, commit the DB status change
    # and clear the bot-cancel marker so downstream flows reconcile.
    if auto_repair and suspected_fills:
        try:
            from spacescan import verify_fill as _spacescan_verify
        except Exception:
            _spacescan_verify = None
        try:
            from database import get_locked_coin_ids_for_trade as _get_locked
        except Exception:
            _get_locked = None
        our_address = str(getattr(cfg, "WALLET_ADDRESS", "") or "")
        for off, dexie_off in suspected_fills:
            tid = off.get("trade_id")
            coin_id = off.get("coin_id")
            candidate_coins = []
            if _get_locked:
                try:
                    candidate_coins = list(_get_locked(tid) or [])
                except Exception:
                    candidate_coins = []
            if coin_id and coin_id not in candidate_coins:
                candidate_coins.insert(0, coin_id)
            candidate_coins = [c for c in candidate_coins if c]

            verdict = None
            if _spacescan_verify and candidate_coins and our_address:
                for _c in candidate_coins:
                    try:
                        verdict = _spacescan_verify(_c, our_address)
                    except Exception as _sve:
                        slog("BOT_HEALTH",
                             f"Spacescan verify raised for tid={tid[:16]}... "
                             f"coin={_c[:16]}...: {_sve}",
                             level="debug")
                        verdict = None
                    if verdict is not None:
                        break

            if verdict == "filled":
                try:
                    update_offer_status(tid, "filled")
                    try:
                        transition_offer(tid, "fill_verified")
                    except Exception:
                        pass
                    msg = (f"recovered_fill tid={tid[:16]}... — Dexie COMPLETED + "
                           f"Spacescan confirmed on-chain spend")
                    repair_log.append(msg)
                    slog("BOT_HEALTH", msg, level="info",
                         data={"trade_id": tid, "dexie_id": off.get("dexie_id"),
                               "source": "bot_health.suspected_fill_recovery"})
                    repaired += 1
                except Exception as e:
                    slog("BOT_HEALTH",
                         f"Failed to commit recovered fill for tid={tid[:16]}...: {e}",
                         level="warn")
            else:
                verdict_str = verdict if verdict is not None else "unavailable"
                slog("BOT_HEALTH",
                     f"Offer tid={tid[:16]}... pending-cancel but Dexie reports "
                     f"COMPLETED. Spacescan verdict={verdict_str}. Leaving for "
                     f"next cycle; fill_tracker will retry and operator is notified.",
                     data={"trade_id": tid, "dexie_id": off.get("dexie_id"),
                           "spacescan_verdict": verdict_str},
                     level="warn")
    else:
        for off, dexie_off in suspected_fills:
            tid = off.get("trade_id")
            slog("BOT_HEALTH",
                 f"Offer tid={tid[:16]}... pending-cancel but Dexie reports "
                 f"COMPLETED (auto-repair disabled — operator review required).",
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


# ── Check 5: topup pool budget drift ──────────────────────────────────

# Drift tolerance: below this (mojos) we ignore the mismatch because small
# differences are expected from split fees, rounding, and mid-cycle timing
# jitter. Above it, we clamp the stored spent counter down to observed
# reality. Sized well below a single tier-split (~0.6 XCH / 4800 CAT) so a
# drift big enough to block a real refill is always caught.
_BUDGET_DRIFT_TOLERANCE_XCH_MOJOS = int(Decimal("0.1") * Decimal("1000000000000"))  # 0.1 XCH


def _reserve_mojos(wallet_type: str) -> int:
    """Sum mojos across all coins currently designated 'reserve'."""
    from database import get_reserve_coins
    total = 0
    try:
        for r in get_reserve_coins(wallet_type) or []:
            amt = r.get("amount_mojos")
            if amt is not None:
                total += int(amt)
    except Exception:
        pass
    return total


def check_topup_budget_drift(auto_repair: bool = True) -> HealthCheck:
    """Reconcile topup-pool spent counters against observed reserve size.

    The spent counter should track the NET amount carved from the reserve
    (splits add, misfit absorption returns subtract). Pre-2026-04-21 the
    refund path was missing, so the counter drifted permanently higher than
    reality and refused legitimate tier refills even while the reserve
    physically held coins. The refund is now wired up, but any counter drift
    from an earlier session persists in bot_settings across restarts.

    Invariant (idempotent): stored_spent == budget - reserve_size (± fees).
    When stored_spent exceeds that, the counter is stale; clamp it down.
    Never adjust upward — doing so would widen the allowance beyond what
    Smart Settings configured.

    Covers both XCH and CAT pools in a single check. Repair is safe: the
    hard-reserve guard (XCH_RESERVE / CAT_RESERVE) still runs on every
    actual split and protects capital independently of this counter.
    """
    from database import get_setting, set_setting
    from config import cfg

    findings = []   # one entry per (wallet_type, asset) that drifted
    repair_log = []
    repaired = 0

    specs = (
        {
            "label": "XCH",
            "wallet_type": "xch",
            "budget_cfg": "TOPUP_POOL_XCH",
            "spent_key": "topup_pool_xch_spent_mojos",
            "scale": Decimal("1000000000000"),  # 1e12 mojos per XCH
            "tolerance_mojos": _BUDGET_DRIFT_TOLERANCE_XCH_MOJOS,
            "display_scale": 1e12,
            "unit": "XCH",
        },
        {
            "label": "CAT",
            "wallet_type": "cat",
            "budget_cfg": "TOPUP_POOL_CAT",
            "spent_key": "topup_pool_cat_spent_mojos",
            # Scale depends on configured CAT decimals; computed inline.
            "scale": None,
            # CAT tolerance derived from 0.5% of the XCH-equivalent tier
            # threshold — comfortably below a meaningful split amount.
            "tolerance_mojos": None,
            "display_scale": None,
            "unit": "CAT",
        },
    )

    for spec in specs:
        try:
            # Resolve the unit scale for CAT (depends on CAT_DECIMALS).
            if spec["wallet_type"] == "cat":
                cat_decimals = int(getattr(cfg, "CAT_DECIMALS", 3))
                scale = Decimal(10) ** Decimal(cat_decimals)
                # Tolerance: 1 whole CAT unit — well below any tier split.
                tolerance_mojos = int(scale)
                display_scale = float(scale)
            else:
                scale = spec["scale"]
                tolerance_mojos = spec["tolerance_mojos"]
                display_scale = spec["display_scale"]

            budget_xch_or_cat = Decimal(str(getattr(cfg, spec["budget_cfg"], 0) or 0))
            if budget_xch_or_cat <= 0:
                continue  # unlimited — drift is meaningless
            budget_mojos = int(budget_xch_or_cat * scale)

            spent_mojos = int(str(get_setting(spec["spent_key"], "0") or "0"))
            reserve_mojos = _reserve_mojos(spec["wallet_type"])

            # The `observed_spent = budget - reserve` formula only holds when
            # the reserve was sized to match the budget. When the reserve
            # exceeds the budget (user over-funded, or a topup bypass consumed
            # more than the budget for an empty-tier recovery) the formula
            # produces bogus negative values and the healer resets the
            # counter to 0 — which then lets the next split bypass the
            # budget again without warning. Skip drift detection in that
            # case; the hard reserve guard still protects capital.
            if reserve_mojos >= budget_mojos:
                continue

            # Observed spend = what's actually been carved from the pool.
            observed_spent = budget_mojos - reserve_mojos

            drift = spent_mojos - observed_spent
            if drift <= tolerance_mojos:
                continue  # counter within tolerance — healthy

            # Drifted upward — stored counter exceeds what reality supports.
            # Build a human-readable summary before any repair.
            msg = (f"{spec['label']} spent counter "
                   f"{spent_mojos / display_scale:.4f} > observed "
                   f"{observed_spent / display_scale:.4f} "
                   f"(reserve={reserve_mojos / display_scale:.4f} "
                   f"of budget {budget_mojos / display_scale:.4f}); "
                   f"drift={drift / display_scale:.4f} {spec['unit']}")
            findings.append(msg)

            if auto_repair:
                try:
                    set_setting(spec["spent_key"], str(observed_spent))
                    repair_log.append(msg + " — clamped to observed.")
                    slog("BOT_HEALTH",
                         f"topup_budget_drift_healed {spec['label']}: "
                         f"{spent_mojos / display_scale:.4f} → "
                         f"{observed_spent / display_scale:.4f} "
                         f"{spec['unit']} (drift {drift / display_scale:.4f})",
                         level="info")
                    repaired += 1
                except Exception as e:
                    slog("BOT_HEALTH",
                         f"Failed to heal {spec['label']} budget drift: {e}",
                         level="warn")

        except Exception as e:
            slog("BOT_HEALTH",
                 f"topup_budget_drift check error on {spec['label']}: {e}",
                 level="warn")

    if not findings:
        return HealthCheck(
            name="topup_budget_drift",
            category="coins",
            status="pass",
            severity="info",
            message="Topup spent counters aligned with observed reserve size.",
        )

    return HealthCheck(
        name="topup_budget_drift",
        category="coins",
        status="warn" if (findings and not auto_repair) else "pass",
        severity="warning" if (findings and not auto_repair) else "info",
        message="; ".join(findings),
        anomaly_count=len(findings),
        repaired_count=repaired,
        repair_log=repair_log,
    )


# ── Check 6: low-funds advisory ───────────────────────────────────────

# How many "inner" tier splits the wallet must be able to support to count
# as healthy. Below this, topup can't refill a depleted tier without
# breaching the hard reserve — so we surface a user-actionable advisory
# with the wallet address and a suggested send amount.
_FUNDS_ADVISORY_TIER_BUFFER = 2  # need room for ~2 inner-size splits
_FUNDS_ADVISORY_FEE_HEADROOM_XCH_MOJOS = int(
    Decimal("0.01") * Decimal("1000000000000")
)  # reserve 0.01 XCH for tx fees over the advisory window

# Suggested top-up sizing: enough for ~5 inner-tier refills so the user
# doesn't need to come back for another advisory in 5 minutes.
_FUNDS_ADVISORY_SUGGEST_MULTIPLIER = 5


def check_funds_advisory(auto_repair: bool = True) -> HealthCheck:
    """Detect when the wallet is genuinely out of capital for tier refills.

    Distinct from the budget-drift check: that one heals a stale counter.
    This one flags the real-world case — the user's wallet doesn't have
    enough XCH or CAT above the hard reserve to support even one more
    inner-tier split. Without an advisory the only signal is the silent
    `blocked_by_reserve` log line, easily missed.

    When triggered, emits a persistent alert via the AlertStore showing:
      - Which asset is low (XCH or CAT)
      - How much to send (covers ~5 inner-tier refills)
      - The wallet's own receive address

    Auto-clears as soon as the wallet balance climbs back above the
    operating floor. `auto_repair` has no meaning here — funds can only
    be added by the operator — so both True and False behave identically
    except that False returns the check as "warn" rather than emitting
    the alert. (We still surface it in the report either way.)
    """
    findings = []
    alerts_raised = []
    alerts_cleared = []

    # Resolve the live event bus lazily; avoids a circular import at
    # module load, and degrades gracefully when running under tests
    # that don't spin up api_server.
    events_bus = None
    try:
        from api_server import events as events_bus  # type: ignore
    except Exception:
        events_bus = None

    def _emit_alert(alert_id: str, title: str, message: str,
                    severity: str = "warning") -> None:
        alerts_raised.append(alert_id)
        if events_bus is None or not auto_repair:
            return
        try:
            events_bus.alert(alert_id, severity, title, message)
        except Exception as e:
            slog("BOT_HEALTH",
                 f"Failed to emit funds advisory alert {alert_id}: {e}",
                 level="warn")

    def _clear_alert(alert_id: str) -> None:
        alerts_cleared.append(alert_id)
        if events_bus is None:
            return
        try:
            store = getattr(events_bus, "_alert_store", None)
            if store is not None:
                store.clear(alert_id)
        except Exception:
            pass

    # ---- XCH advisory ----
    try:
        from wallet_sage import get_wallet_balance as _sage_balance
        from wallet import get_wallet_type as _wt
        if _wt() == "sage":
            raw = _sage_balance(int(getattr(cfg, "WALLET_ID_XCH", 1) or 1)) or {}
            wb = raw.get("wallet_balance") or {}
            spendable_mojos = int(wb.get("spendable_balance", 0) or 0)
        else:
            spendable_mojos = None

        if spendable_mojos is not None:
            hard_reserve_mojos = int(
                Decimal(str(getattr(cfg, "XCH_RESERVE", 0) or 0))
                * Decimal("1000000000000")
            )
            # Smallest tier size — use the sell side for a conservative floor,
            # falling back to INNER_SIZE_XCH then to a 0.1 XCH default.
            inner_size_xch = Decimal(str(
                getattr(cfg, "SELL_INNER_SIZE_XCH", 0)
                or getattr(cfg, "INNER_SIZE_XCH", 0)
                or "0.1"
            ))
            inner_size_mojos = int(inner_size_xch * Decimal("1000000000000"))
            min_operating_mojos = (
                _FUNDS_ADVISORY_TIER_BUFFER * inner_size_mojos
                + _FUNDS_ADVISORY_FEE_HEADROOM_XCH_MOJOS
            )
            available_mojos = spendable_mojos - hard_reserve_mojos

            if available_mojos < min_operating_mojos:
                # Shortfall = how much extra XCH the operator must send
                # to cover the operating buffer + a few refill cycles.
                target_mojos = (
                    hard_reserve_mojos
                    + _FUNDS_ADVISORY_SUGGEST_MULTIPLIER * inner_size_mojos
                    + _FUNDS_ADVISORY_FEE_HEADROOM_XCH_MOJOS
                )
                shortfall_mojos = max(0, target_mojos - spendable_mojos)
                shortfall_xch = Decimal(shortfall_mojos) / Decimal("1000000000000")
                address = str(getattr(cfg, "WALLET_ADDRESS", "") or "")
                msg_lines = [
                    f"XCH spendable ({spendable_mojos / 1e12:.4f}) is below the "
                    f"operating floor needed to refill tiers."
                ]
                if shortfall_xch > 0:
                    msg_lines.append(
                        f"Send at least {shortfall_xch:.4f} XCH to resume "
                        f"full topup activity."
                    )
                if address:
                    msg_lines.append(f"Address: {address}")
                _emit_alert(
                    alert_id="funds_advisory_xch",
                    title="XCH running low",
                    message=" ".join(msg_lines),
                    severity="warning",
                )
                findings.append(
                    f"XCH shortfall {shortfall_xch:.4f} "
                    f"(spendable={spendable_mojos / 1e12:.4f}, "
                    f"floor={min_operating_mojos / 1e12:.4f})"
                )
            else:
                _clear_alert("funds_advisory_xch")
    except Exception as e:
        slog("BOT_HEALTH", f"XCH funds advisory check error: {e}", level="warn")

    # ---- CAT advisory ----
    try:
        from wallet_sage import get_wallet_balance as _sage_balance
        from wallet import get_wallet_type as _wt
        if _wt() == "sage":
            raw = _sage_balance(int(getattr(cfg, "CAT_WALLET_ID", 2) or 2)) or {}
            wb = raw.get("wallet_balance") or {}
            spendable_mojos = int(wb.get("spendable_balance", 0) or 0)
        else:
            spendable_mojos = None

        if spendable_mojos is not None:
            cat_decimals = int(getattr(cfg, "CAT_DECIMALS", 3))
            scale = Decimal(10) ** Decimal(cat_decimals)
            hard_reserve_mojos = int(
                Decimal(str(getattr(cfg, "CAT_RESERVE", 0) or 0)) * scale
            )
            # CAT inner tier size in CAT units. We size the CAT floor as
            # N × inner-CAT-per-offer — no fee headroom on CAT (fees are
            # paid in XCH by the cat_wallet under the hood).
            cat_inner_xch = Decimal(str(
                getattr(cfg, "SELL_INNER_SIZE_XCH", 0)
                or getattr(cfg, "INNER_SIZE_XCH", 0)
                or "0"
            ))
            # Approximate CAT size per offer: inner_xch / current_price.
            # We don't have a fresh live price in bot_health, so fall back
            # to 10% of the user's configured CAT_RESERVE when no better
            # signal is available.
            try:
                price_xch = Decimal(str(getattr(cfg, "LAST_QUOTED_MID", 0) or 0))
            except Exception:
                price_xch = Decimal("0")
            if price_xch > 0 and cat_inner_xch > 0:
                inner_size_cat = (cat_inner_xch / price_xch)
            else:
                inner_size_cat = Decimal(str(
                    getattr(cfg, "CAT_RESERVE", 0) or 0
                )) * Decimal("0.1")
            inner_size_mojos = int(inner_size_cat * scale)
            if inner_size_mojos <= 0:
                # No meaningful CAT tier size signal available (e.g. fresh
                # install before Smart Settings has run, or CAT_RESERVE=0
                # configured intentionally). Skip CAT side silently —
                # without a floor estimate there's nothing actionable to
                # say. Previously this raised and logged on every check,
                # spamming the log once per minute forever.
                pass
            else:
                min_operating_mojos = _FUNDS_ADVISORY_TIER_BUFFER * inner_size_mojos
                available_mojos = spendable_mojos - hard_reserve_mojos

                if available_mojos < min_operating_mojos:
                    target_mojos = (
                        hard_reserve_mojos
                        + _FUNDS_ADVISORY_SUGGEST_MULTIPLIER * inner_size_mojos
                    )
                    shortfall_mojos = max(0, target_mojos - spendable_mojos)
                    shortfall_cat = Decimal(shortfall_mojos) / scale
                    address = str(getattr(cfg, "WALLET_ADDRESS", "") or "")
                    ticker = str(getattr(cfg, "CAT_NAME", "") or "CAT")
                    msg_lines = [
                        f"{ticker} spendable ({spendable_mojos / float(scale):,.4f}) "
                        f"is below the operating floor."
                    ]
                    if shortfall_cat > 0:
                        msg_lines.append(
                            f"Send at least {shortfall_cat:,.4f} {ticker} "
                            f"to resume full topup activity."
                        )
                    if address:
                        msg_lines.append(f"Address: {address}")
                    _emit_alert(
                        alert_id="funds_advisory_cat",
                        title=f"{ticker} running low",
                        message=" ".join(msg_lines),
                        severity="warning",
                    )
                    findings.append(
                        f"{ticker} shortfall {shortfall_cat:,.4f} "
                        f"(spendable={spendable_mojos / float(scale):,.4f})"
                    )
                else:
                    _clear_alert("funds_advisory_cat")
    except Exception as e:
        slog("BOT_HEALTH", f"CAT funds advisory check error: {e}", level="warn")

    if not findings:
        return HealthCheck(
            name="funds_advisory",
            category="wallet",
            status="pass",
            severity="info",
            message="Wallet balances above operating floors.",
        )

    return HealthCheck(
        name="funds_advisory",
        category="wallet",
        status="warn",
        severity="warning",
        message="; ".join(findings),
        anomaly_count=len(findings),
        repaired_count=0,  # repair is out-of-band (user action)
        repair_log=[f"raised:{a}" for a in alerts_raised],
    )


# ── Check 7: Splash daemon hook fires ─────────────────────────────────

# How many offers the daemon must have gossiped before we're confident the
# hook silence isn't just peer-sparse bootstrap. 10 offers × 5s poll ≈ 50s
# of live P2P activity — well above any startup lull.
_SPLASH_HOOK_MIN_SEEN = 10
# How many of those offers must have reached our webhook before we
# consider the hook "working". 1 is enough to prove the pipeline.
_SPLASH_HOOK_MIN_DELIVERED = 1


def check_splash_daemon(auto_repair: bool = True) -> HealthCheck:
    """Detect silent Splash daemon states and surface them to the operator.

    Three distinct silent failures look identical from the counter-on-the-
    dashboard view ("0 received"):

      A. Daemon not running or metrics endpoint unreachable → misconfigured
         or crashed, user needs to restart it.
      B. Daemon running but zero peers → network/firewall blocking inbound
         P2P on port 11511.
      C. Daemon has peers AND has seen offers AND our webhook has delivered
         zero to the DB → offer-hook is broken (version mismatch, network
         loopback issue, or daemon bug). User gets actionable guidance.

    Read-only: no repair to perform, just accurate classification via the
    alert store so the Market Intel panel isn't silently lying.
    """
    events_bus = None
    try:
        from api_server import events as events_bus  # type: ignore
    except Exception:
        events_bus = None

    def _emit_alert(alert_id, title, message, severity="warning"):
        if events_bus is None or not auto_repair:
            return
        try:
            events_bus.alert(alert_id, severity, title, message)
        except Exception:
            pass

    def _clear_alert(alert_id):
        if events_bus is None:
            return
        try:
            store = getattr(events_bus, "_alert_store", None)
            if store is not None:
                store.clear(alert_id)
        except Exception:
            pass

    # Pull current snapshot from the running bot. No-bot or no-splash_node
    # means Splash management is off entirely — skip the check.
    try:
        import api_server
        bot = getattr(api_server, "bot", None)
    except Exception:
        bot = None

    if bot is None or getattr(bot, "splash_node", None) is None:
        # Clear any stale alerts — Splash is not in scope.
        for aid in ("splash_unreachable", "splash_no_peers", "splash_hook_broken"):
            _clear_alert(aid)
        return HealthCheck(
            name="splash_daemon",
            category="wallet",
            status="pass",
            severity="info",
            message="Splash management not active.",
        )

    # SPLASH_RECEIVE_ENABLED off → user opted out of inbound, don't nag.
    if not bool(getattr(cfg, "SPLASH_RECEIVE_ENABLED", False)):
        for aid in ("splash_unreachable", "splash_no_peers", "splash_hook_broken"):
            _clear_alert(aid)
        return HealthCheck(
            name="splash_daemon",
            category="wallet",
            status="pass",
            severity="info",
            message="Splash inbound listening disabled.",
        )

    metrics = {}
    try:
        metrics = bot.splash_node.get_metrics() or {}
    except Exception:
        metrics = {}

    reachable = bool(metrics.get("reachable", False))
    peers = int(metrics.get("peers", 0) or 0)
    offers_seen = int(metrics.get("offers_received", 0) or 0)

    # Webhook delivery count (DB-side).
    delivered_total = 0
    try:
        from database import get_splash_incoming_stats
        asset_id = str(getattr(cfg, "CAT_ASSET_ID", "") or "").strip().lower()
        stats = get_splash_incoming_stats(asset_id=asset_id) or {}
        delivered_total = int(stats.get("total", 0) or 0)
    except Exception:
        delivered_total = 0

    findings = []
    alerts_raised = []

    # Case A: daemon metrics endpoint unreachable (daemon down or port
    # misconfigured). Only alert when the process is supposed to be up.
    if not reachable:
        last_err = str(metrics.get("last_error") or "no metrics snapshot yet")
        findings.append(f"metrics endpoint unreachable ({last_err})")
        _emit_alert(
            alert_id="splash_unreachable",
            title="Splash daemon metrics unreachable",
            message=(
                f"Can't read splash.exe metrics at "
                f"{metrics.get('metrics_url') or 'the configured port'}. "
                f"Restart the Splash node from Market Intel, or check that "
                f"port {getattr(cfg, 'SPLASH_METRICS_PORT', 4001)} isn't "
                f"firewalled. Reason: {last_err}"
            ),
            severity="warning",
        )
        alerts_raised.append("splash_unreachable")
    else:
        _clear_alert("splash_unreachable")

    # Case B: reachable but zero peers — network/firewall issue.
    if reachable and peers == 0:
        findings.append("daemon has 0 peers")
        _emit_alert(
            alert_id="splash_no_peers",
            title="Splash daemon has no peers",
            message=(
                "Splash is running but not connected to any P2P peers. "
                "Check that your firewall/router allows inbound TCP on "
                f"port {getattr(cfg, 'SPLASH_P2P_PORT', 11511)}. Without "
                "peers the bot can't receive offers from the Splash network."
            ),
            severity="warning",
        )
        alerts_raised.append("splash_no_peers")
    else:
        _clear_alert("splash_no_peers")

    # Case C: daemon has seen plenty of offers but the webhook has zero —
    # the --offer-hook path is broken (version mismatch, bind issue, etc).
    if (reachable and peers > 0
            and offers_seen >= _SPLASH_HOOK_MIN_SEEN
            and delivered_total < _SPLASH_HOOK_MIN_DELIVERED):
        findings.append(
            f"daemon seen {offers_seen} offers but webhook got "
            f"{delivered_total}"
        )
        _emit_alert(
            alert_id="splash_hook_broken",
            title="Splash offer-hook not firing",
            message=(
                f"Splash daemon has seen {offers_seen:,} offers from its "
                f"{peers} peers but only {delivered_total} have reached "
                f"the bot's webhook. The --offer-hook path is likely "
                f"broken — check the splash binary version, or restart "
                f"the node. Inbound offers won't be processed until this "
                f"is resolved."
            ),
            severity="warning",
        )
        alerts_raised.append("splash_hook_broken")
    else:
        _clear_alert("splash_hook_broken")

    if not findings:
        return HealthCheck(
            name="splash_daemon",
            category="wallet",
            status="pass",
            severity="info",
            message=(f"Splash healthy: peers={peers}, "
                     f"offers_seen={offers_seen}, "
                     f"delivered={delivered_total}"),
        )

    return HealthCheck(
        name="splash_daemon",
        category="wallet",
        status="warn",
        severity="warning",
        message="; ".join(findings),
        anomaly_count=len(findings),
        repaired_count=0,  # out-of-band: user must act
        repair_log=[f"raised:{a}" for a in alerts_raised],
    )


# ── Check 8: Spacescan cache staleness → background refresh ───────────

# The Spacescan cache has a 24h TTL normally but drops to 30min when the
# activity endpoint silently fails (holder_count=0 or activity_fetch_failed).
# That 30min window is intentional — it's meant to encourage a retry —
# but nothing automatically triggers the retry. The result: after 30min
# the dashboard shows "Unknown / Unknown / —" until the user manually
# re-runs Smart Settings. This check fires the retry on the same 60s
# cadence as everything else in bot_health, in a background thread so a
# slow Spacescan call doesn't stall the health pass.

_spacescan_refresh_inflight: bool = False
_spacescan_refresh_last_at: float = 0.0


def check_spacescan_cache_stale(auto_repair: bool = True) -> HealthCheck:
    """Refresh the Spacescan cache when it's missing or expired.

    Read-only when auto_repair=False; otherwise spawns a single-shot
    background thread that calls `refresh_spacescan_cache(asset_id)`.
    Double-fire guarded by `_spacescan_refresh_inflight`, and a 60s
    min-interval enforced so a repeat upstream failure doesn't turn into
    a tight retry loop.

    Skips quietly when SPACESCAN_ENABLED is off or CAT_ASSET_ID isn't
    set — nothing to refresh.
    """
    global _spacescan_refresh_inflight, _spacescan_refresh_last_at

    if not bool(getattr(cfg, "SPACESCAN_ENABLED", True)):
        return HealthCheck(
            name="spacescan_cache", category="wallet", status="pass",
            severity="info", message="Spacescan disabled.",
        )

    asset_id = str(getattr(cfg, "CAT_ASSET_ID", "") or "").strip().lower()
    if not asset_id:
        return HealthCheck(
            name="spacescan_cache", category="wallet", status="pass",
            severity="info", message="No CAT selected.",
        )

    # Peek at cache age. An expired cache will appear as None here because
    # get_market_analysis_cache filters by expires_at.
    try:
        from database import get_market_analysis_cache
        cached = get_market_analysis_cache(asset_id, "spacescan")
    except Exception:
        cached = None

    if cached:
        return HealthCheck(
            name="spacescan_cache", category="wallet", status="pass",
            severity="info",
            message=(f"Spacescan cache warm for "
                     f"{asset_id[:16]}... (has_data="
                     f"{bool(cached.get('has_data'))})"),
        )

    # Cache is empty or expired. Rate-limit the refresh so we don't
    # hammer Spacescan if the upstream is down.
    now = time.time()
    if now - _spacescan_refresh_last_at < 60.0:
        return HealthCheck(
            name="spacescan_cache", category="wallet", status="pass",
            severity="info",
            message="Spacescan cache stale; refresh pending (throttled).",
        )
    if _spacescan_refresh_inflight:
        return HealthCheck(
            name="spacescan_cache", category="wallet", status="pass",
            severity="info",
            message="Spacescan refresh already in flight.",
        )

    if not auto_repair:
        return HealthCheck(
            name="spacescan_cache", category="wallet", status="warn",
            severity="warning",
            message=f"Spacescan cache missing/expired for {asset_id[:16]}...",
            anomaly_count=1,
        )

    # Kick off the refresh in a background thread — the Spacescan API call
    # chain can take several seconds, and we don't want to block the health
    # pass (which holds the loop back).
    import threading as _threading

    def _do_refresh():
        global _spacescan_refresh_inflight
        try:
            from market_data_collector import refresh_spacescan_cache
            result = refresh_spacescan_cache(asset_id)
            if result:
                slog("BOT_HEALTH",
                     f"spacescan_cache_refreshed {asset_id[:16]}... "
                     f"holders={result.get('holder_count', 0)} "
                     f"activity={result.get('activity_count', 0)}",
                     level="info")
            else:
                slog("BOT_HEALTH",
                     f"spacescan_cache_refresh_empty {asset_id[:16]}...",
                     level="info")
        except Exception as e:
            slog("BOT_HEALTH",
                 f"spacescan_cache_refresh_error: {e}",
                 level="warn")
        finally:
            _spacescan_refresh_inflight = False

    _spacescan_refresh_inflight = True
    _spacescan_refresh_last_at = now
    _threading.Thread(target=_do_refresh, daemon=True,
                      name="spacescan-refresh").start()

    return HealthCheck(
        name="spacescan_cache", category="wallet", status="pass",
        severity="info",
        message=f"Spacescan refresh dispatched for {asset_id[:16]}...",
        repaired_count=1,
    )


# ── Check 9: unclaimed deposits → three-way allocation prompt ─────────

# A reserve coin counts as a "significant deposit" candidate when its
# amount >= 10× the smallest configured trading tier size, AND it hasn't
# already been advised on (persisted list), AND no internal misfit
# absorption happened in the last 90 seconds (that creates a new reserve
# coin too but it's internal bucket reshuffle, not new capital).
#
# No time window on `first_seen` — a deposit that arrived before a
# restart is still unallocated and still deserves a prompt. The advised
# coin_id list is the authoritative dedup mechanism; a time filter on
# top would create false negatives (silently skip legit deposits that
# happen to be older than N minutes).
_DEPOSIT_ADVISORY_TIER_MULTIPLE = 10
_DEPOSIT_ADVISORY_ABSORB_COOLDOWN_SECS = 90
_DEPOSIT_ADVISORY_MAX_ALERTS_PER_PASS = 5  # cap first-run flood


def _advised_deposits() -> set:
    """Load the persisted set of coin_ids already shown to the user."""
    try:
        from database import get_setting
        raw = get_setting("deposit_advisory_advised_coins", "") or ""
    except Exception:
        return set()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _recent_absorb(key: str, now_ts: float) -> bool:
    """True if a misfit absorption happened within the cooldown window."""
    try:
        from database import get_setting
        ts = int(str(get_setting(key, "0") or "0"))
    except Exception:
        ts = 0
    return (now_ts - ts) < _DEPOSIT_ADVISORY_ABSORB_COOLDOWN_SECS


def check_unclaimed_deposits(auto_repair: bool = True) -> HealthCheck:
    """Prompt the user to allocate newly-arrived funds.

    When an external deposit lands (coin-watcher detects a NEW coin and
    the classifier promotes it to `reserve`), the coin sits in the
    topup-pool bucket but won't be spendable by the topup worker until
    the TOPUP_POOL_* budget is raised to match. This check surfaces a
    one-click allocation prompt so the operator can decide:

      - add all to trading pool (raises TOPUP_POOL_*),
      - keep as hard reserve (raises *_RESERVE),
      - split some %% to each.

    The alert is persistent and coin-scoped — dismissing acts on that
    specific coin_id and doesn't re-fire. Applying the allocation goes
    through `/api/deposit-advisory/allocate`.
    """
    try:
        from database import get_connection
    except Exception:
        return HealthCheck(
            name="unclaimed_deposits", category="wallet", status="pass",
            severity="info", message="Database unavailable.",
        )

    events_bus = None
    try:
        from api_server import events as events_bus  # type: ignore
    except Exception:
        events_bus = None

    def _emit_alert(alert_id, title, message, action_value, severity="info"):
        if events_bus is None or not auto_repair:
            return
        try:
            events_bus.alert(alert_id, severity, title, message,
                             action="allocate_deposit",
                             action_label="Allocate",
                             action_value=action_value)
        except Exception as e:
            slog("BOT_HEALTH",
                 f"Failed to emit deposit advisory {alert_id}: {e}",
                 level="warn")

    def _clear_alert(alert_id):
        if events_bus is None:
            return
        try:
            store = getattr(events_bus, "_alert_store", None)
            if store is not None:
                store.clear(alert_id)
        except Exception:
            pass

    findings = []
    alerts_raised = []
    now_ts = time.time()
    advised = _advised_deposits()
    live_alert_ids = set()

    for wallet_type, budget_cfg, reserve_cfg, is_cat in (
        ("xch", "TOPUP_POOL_XCH", "XCH_RESERVE", False),
        ("cat", "TOPUP_POOL_CAT", "CAT_RESERVE", True),
    ):
        # Skip if the side is disabled — no point prompting for a pair
        # the user isn't trading.
        if wallet_type == "xch" and not bool(getattr(cfg, "ENABLE_BUY", True)):
            continue
        if wallet_type == "cat" and not bool(getattr(cfg, "ENABLE_SELL", True)):
            continue

        # Internal cooldown — skip if misfit absorption just created a
        # fresh reserve coin (that's not an external deposit).
        absorb_key = f"last_misfit_absorb_{wallet_type}_at"
        if _recent_absorb(absorb_key, now_ts):
            continue

        # Compute the smallest tier size for this wallet type so we have
        # a meaningful "significant" floor.
        try:
            if is_cat:
                scale = Decimal(10) ** Decimal(str(getattr(cfg, "CAT_DECIMALS", 3)))
            else:
                scale = Decimal("1000000000000")
            inner_xch = Decimal(str(
                getattr(cfg, "SELL_INNER_SIZE_XCH" if is_cat else "BUY_INNER_SIZE_XCH", 0)
                or getattr(cfg, "INNER_SIZE_XCH", 0)
                or "0"
            ))
            # For CAT the tier size is inner_xch / price; fall back to
            # 1/10 of the current CAT_RESERVE when price isn't available
            # (same heuristic used in check_funds_advisory).
            if is_cat:
                try:
                    price = Decimal(str(getattr(cfg, "LAST_QUOTED_MID", 0) or 0))
                except Exception:
                    price = Decimal("0")
                if price > 0 and inner_xch > 0:
                    inner_size_asset = inner_xch / price
                else:
                    inner_size_asset = Decimal(str(
                        getattr(cfg, "CAT_RESERVE", 0) or 0
                    )) * Decimal("0.1")
                smallest_mojos = int(inner_size_asset * scale)
            else:
                smallest_mojos = int(inner_xch * scale)
            if smallest_mojos <= 0:
                continue  # no tier sizing signal — skip silently
            threshold_mojos = smallest_mojos * _DEPOSIT_ADVISORY_TIER_MULTIPLE

            # Coin prep designates the user's configured reserve as one
            # large `reserve` coin. That coin is intentional — often a
            # little bigger than XCH_RESERVE because prep consolidates
            # the leftover after tier/sniper/fee/topup allocation into
            # the reserve slot. Shift the alert threshold above
            # `reserve + one top-up pool's worth` so only coins that
            # wouldn't fit in the bot's next top-up cycle surface as
            # "new deposit" — rounding overhead stays quiet.
            try:
                configured_reserve = Decimal(str(
                    getattr(cfg, reserve_cfg, 0) or 0
                ))
            except Exception:
                configured_reserve = Decimal("0")
            topup_budget_cfg = (
                "TOPUP_POOL_XCH" if wallet_type == "xch" else "TOPUP_POOL_CAT"
            )
            try:
                topup_pool = Decimal(str(
                    getattr(cfg, topup_budget_cfg, 0) or 0
                ))
            except Exception:
                topup_pool = Decimal("0")
            reserve_mojos = int(configured_reserve * scale)
            topup_mojos = int(topup_pool * scale)
            if reserve_mojos > 0:
                # reserve + max(tier×10 headroom, one top-up cycle)
                headroom = max(threshold_mojos, topup_mojos)
                threshold_mojos = reserve_mojos + headroom
        except Exception:
            continue

        try:
            conn = get_connection()
            rows = conn.execute(
                "SELECT coin_id, amount_mojos, first_seen "
                "FROM coins "
                "WHERE wallet_type=? AND status='free' "
                "  AND designation='reserve' "
                "  AND amount_mojos >= ? "
                "ORDER BY amount_mojos DESC",
                (wallet_type, int(threshold_mojos)),
            ).fetchall()
        except Exception as e:
            slog("BOT_HEALTH",
                 f"Deposit advisory query failed for {wallet_type}: {e}",
                 level="warn")
            continue

        # Cap the alerts per pass to avoid flooding the Recommendations
        # panel on first deployment when there may be many pre-existing
        # reserve coins. Surface the largest ones first; the user can
        # dismiss or allocate each in turn.
        raised_this_pass = 0

        for row in rows:
            if raised_this_pass >= _DEPOSIT_ADVISORY_MAX_ALERTS_PER_PASS:
                break
            coin_id = row["coin_id"] or ""
            if not coin_id or coin_id in advised:
                continue

            amount_mojos = int(row["amount_mojos"] or 0)
            display_amount = Decimal(amount_mojos) / scale
            unit = (str(getattr(cfg, "CAT_NAME", "") or "CAT") if is_cat
                    else "XCH")

            alert_id = f"deposit_advisory_{coin_id}"
            live_alert_ids.add(alert_id)

            short_id = coin_id[:10] + "..." + coin_id[-6:]
            pool_current = Decimal(str(
                getattr(cfg, budget_cfg, 0) or 0
            ))
            reserve_current = Decimal(str(
                getattr(cfg, reserve_cfg, 0) or 0
            ))
            message = (
                f"Detected {display_amount:,.4f} {unit} in reserve coin "
                f"{short_id}. It's sitting in the topup pool but the "
                f"budget ({pool_current:,} {unit}) doesn't cover it yet. "
                f"Choose: add to trading pool, keep as hard reserve "
                f"({reserve_current:,} {unit}), or split."
            )
            # action_value carries enough for the GUI to open the modal
            # without another API round-trip.
            action_value = (f"{wallet_type}|{coin_id}|{amount_mojos}|"
                            f"{unit}|{budget_cfg}|{reserve_cfg}")
            _emit_alert(
                alert_id=alert_id,
                title=f"New {unit} deposit — allocate?",
                message=message,
                action_value=action_value,
                severity="info",
            )
            alerts_raised.append(alert_id)
            findings.append(
                f"{unit} {display_amount:,.4f} in {short_id}"
            )
            raised_this_pass += 1

    # Clear any previously-raised advisory alerts whose coins have since
    # been allocated (now in the advised list) — keeps the UI from stuck
    # alerts after the user clicks Allocate on one.
    if events_bus is not None:
        try:
            store = getattr(events_bus, "_alert_store", None)
            if store is not None:
                active = list(store.get_active())
                for item in active:
                    _id = str(item.get("id", ""))
                    if _id.startswith("deposit_advisory_") and _id not in live_alert_ids:
                        store.clear(_id)
        except Exception:
            pass

    if not findings:
        return HealthCheck(
            name="unclaimed_deposits", category="wallet", status="pass",
            severity="info",
            message="No unallocated deposits.",
        )
    return HealthCheck(
        name="unclaimed_deposits", category="wallet", status="warn",
        severity="warning",
        message="; ".join(findings),
        anomaly_count=len(findings),
        repaired_count=0,  # user action required
        repair_log=[f"raised:{a}" for a in alerts_raised],
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
    report.checks.append(check_topup_budget_drift(auto_repair=auto_repair))
    report.checks.append(check_funds_advisory(auto_repair=auto_repair))
    report.checks.append(check_splash_daemon(auto_repair=auto_repair))
    report.checks.append(check_spacescan_cache_stale(auto_repair=auto_repair))
    report.checks.append(check_unclaimed_deposits(auto_repair=auto_repair))

    report.duration_ms = (time.time() - start) * 1000.0
    _last_run_lock_ts = time.time()
    _last_report = report
    return report
