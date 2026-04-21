"""Cycle-level self-audit for ladder shape and coin-accounting invariants

Observes current state each bot cycle and raises clear, actionable findings
when reality drifts from the configured ladder shape or the coin books go
inconsistent with wallet totals. Strictly informational — the watchdog never
creates offers, moves coins, or mutates state; corrective actions are left
to other modules that can consume its findings.

Key responsibilities:
    - `audit_ladder_shape` — check offer count, size taper, monotonicity,
      and price inversions across the open ladder
    - `check_coin_invariants` — verify inventory bucket sums match wallet
      totals and every locked coin points to an open offer
    - `run_periodic_audit` — aggregator called once per cycle by `bot_loop`

Findings are emitted as structured log events so downstream consumers
(scheduled monitors, `bot_health`) can pick them up and apply fixes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(Enum):
    INFO = "info"
    WARN = "warning"
    ERROR = "error"


@dataclass
class Issue:
    """One specific problem found during audit."""
    severity: Severity
    code: str            # stable identifier for logging/filtering
    message: str         # human-readable description
    suggested_action: str = ""   # what the caller/operator should do
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditResult:
    """Result of a single audit run."""
    ok: bool             # True if no ERROR-severity issues
    issues: List[Issue] = field(default_factory=list)
    # Diagnostic summary — useful for logging even when ok=True
    summary: Dict[str, Any] = field(default_factory=dict)

    def has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)

    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARN for i in self.issues)


# ---------------------------------------------------------------------------
# Ladder shape audit
# ---------------------------------------------------------------------------

def audit_ladder_shape(
    side: str,
    offers: List[Dict[str, Any]],
    tier_sizes_xch: Dict[str, Decimal],
    tier_counts: Dict[str, int],
    reversed_ladder: bool = False,
    *,
    size_tolerance: Decimal = Decimal("0.05"),
) -> AuditResult:
    """Audit the configured ladder shape against the actual open offers.

    Checks:
        1. **Slot count** — total offers matches sum of tier_counts.
        2. **Size taper** — offer sizes per tier match the configured size
           within ``size_tolerance`` (default 5%).
        3. **Monotonic ordering** — for reverse ladders, inner offers are
           larger than outer ones (and vice versa for non-reverse).
        4. **No size inversions** — no tier N's offers are larger than
           tier N-1's (would indicate a misfit-backed offer leaked
           through).

    Args:
        side: "buy" or "sell".
        offers: List of offer dicts with at least ``size_xch`` and
            ``price``. May come from ``/api/offers`` response or the DB.
        tier_sizes_xch: ``{"inner": Decimal, "mid": Decimal, ...}`` —
            the configured tier sizes for this side (what the ladder
            SHOULD look like).
        tier_counts: ``{"inner": 10, "mid": 5, "outer": 3, "extreme": 2}``
            — the configured slot counts per tier.
        reversed_ladder: When True, inner = largest / extreme = smallest.
            When False (standard), inner = smallest / extreme = largest.
        size_tolerance: Fractional tolerance for size matching
            (0.05 = within 5%). Tighter = more vigilant; 5% is a sensible
            default that allows for change-coin slack without being so
            loose that misfits slip through.

    Returns:
        :class:`AuditResult` with issues listed. Critical issues
        (size inversions, tier_count mismatches) are ERROR. Size-tolerance
        violations are WARN.
    """
    result = AuditResult(ok=True)

    n_offers = len(offers)
    expected_count = sum(int(c or 0) for c in tier_counts.values())

    # Build a normalised picture of the offers — sorted by "distance from
    # mid", which for a sell is ascending price, for a buy is descending
    # price. Then we can walk the ladder from the inner slot outward.
    def _distance_key(o):
        p = o.get("price")
        try:
            p = Decimal(str(p)) if p is not None else Decimal("0")
        except Exception:
            p = Decimal("0")
        # For sells, lower price = closer to mid = inner. For buys,
        # higher price = closer to mid = inner. So "distance ascending"
        # means ascending for sells, descending for buys.
        return p if side == "sell" else -p

    sorted_offers = sorted(offers, key=_distance_key)
    result.summary["side"] = side
    result.summary["open_count"] = n_offers
    result.summary["expected_count"] = expected_count
    result.summary["reversed"] = reversed_ladder

    # Check 1: total offer count
    if expected_count and n_offers not in (expected_count, expected_count - 1, expected_count + 1):
        # ±1 tolerance for transient states (sniper probe, mid-requote)
        result.issues.append(Issue(
            severity=Severity.WARN,
            code="ladder_count_mismatch",
            message=(
                f"{side} ladder has {n_offers} offers, configured total is "
                f"{expected_count} (tier_counts={dict(tier_counts)})"
            ),
            suggested_action=(
                "Expected during requote transitions. If persists >5 cycles, "
                "check offer_manager.create_ladder for silent failures."
            ),
            details={"observed": n_offers, "expected": expected_count},
        ))

    # Check 2: walk each offer, decide which tier it SHOULD belong to
    # based on its slot position, and compare size to configured tier_size.
    tier_order = ["inner", "mid", "outer", "extreme"]
    # Build a slot→tier map following the same logic as offer_manager:
    # first inner_count slots are inner, next mid_count are mid, etc.
    slot_to_tier: List[str] = []
    for tier in tier_order:
        slot_to_tier.extend([tier] * int(tier_counts.get(tier, 0) or 0))

    # Track size violations
    size_violations: List[Dict[str, Any]] = []
    # Track per-tier observed sizes for taper check
    sizes_by_tier: Dict[str, List[Decimal]] = {t: [] for t in tier_order}
    # Track per-tier trade_ids so downstream (dashboard) can cancel the
    # specific offers backing a tier on inversion.
    trade_ids_by_tier: Dict[str, List[str]] = {t: [] for t in tier_order}

    for i, offer in enumerate(sorted_offers):
        if i >= len(slot_to_tier):
            # Extra offer beyond configured slot count
            continue
        expected_tier = slot_to_tier[i]
        expected_size = tier_sizes_xch.get(expected_tier)
        if expected_size is None or expected_size <= 0:
            continue
        actual_size = Decimal(str(offer.get("size_xch") or 0))
        if actual_size <= 0:
            continue
        sizes_by_tier[expected_tier].append(actual_size)
        tid = str(offer.get("trade_id") or "")
        if tid:
            trade_ids_by_tier[expected_tier].append(tid)
        # Tolerance check — within ±size_tolerance × expected_size
        lower = expected_size * (Decimal("1") - size_tolerance)
        upper = expected_size * (Decimal("1") + size_tolerance)
        if not (lower <= actual_size <= upper):
            size_violations.append({
                "slot": i,
                "trade_id": tid,
                "expected_tier": expected_tier,
                "expected_size": float(expected_size),
                "actual_size": float(actual_size),
                "drift_pct": float(abs(actual_size - expected_size) / expected_size * 100),
            })

    if size_violations:
        # Collect the trade_ids of the offending offers so the dashboard
        # action button has something concrete to cancel.
        offender_tids = [v["trade_id"] for v in size_violations if v.get("trade_id")]
        result.issues.append(Issue(
            severity=Severity.WARN,
            code="ladder_size_taper_violated",
            message=(
                f"{side} ladder has {len(size_violations)} offer(s) whose size "
                f"does not match the configured tier size (>{float(size_tolerance)*100:.0f}% drift)"
            ),
            suggested_action=(
                "A slot was filled with a misfit-sized coin. Cancel the offending "
                "offer(s) and let topup reshape the misfit into a tier-correct coin "
                "before the next requote."
            ),
            details={
                "side": side,
                "violations": size_violations[:5],   # cap for log size
                "trade_ids": offender_tids,          # full list for cancel action
            },
        ))

    # Check 3: per-tier size monotonicity with respect to reverse-ladder config
    # For a reverse ladder, inner_size > mid_size > outer_size > extreme_size.
    # For a standard ladder, inner_size < mid_size < outer_size < extreme_size.
    # Check the MEDIAN size per tier — robust to 1-2 outlier slots.
    def _median(xs: List[Decimal]) -> Optional[Decimal]:
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / Decimal("2")

    medians = {t: _median(sizes_by_tier[t]) for t in tier_order}
    result.summary["size_medians"] = {t: float(v) if v is not None else None for t, v in medians.items()}

    # Build list of (tier, median) in "inner-to-extreme" order, skipping empty tiers
    seq = [(t, medians[t]) for t in tier_order if medians[t] is not None]
    # Float-precision tolerance: tier sizes are typically 0.1–5 XCH. Storage
    # and arithmetic can introduce sub-mojo drift (e.g. 2.5349000000035 vs
    # 2.534900000007). Without a tolerance, `Decimal` comparison flags such
    # ~3e-12 XCH differences as an "inversion" and escalates the watchdog
    # to ERROR level. 0.0001 XCH (= 100,000 mojos) sits well above float
    # noise but well below any real tier-size gap, so genuine inversions
    # still trip while precision drift no longer does.
    _MEDIAN_EPS = Decimal("0.0001")
    for i in range(len(seq) - 1):
        t1, m1 = seq[i]
        t2, m2 = seq[i + 1]
        # Reverse: m1 should be >= m2 (inner is largest)
        # Standard: m1 should be <= m2 (inner is smallest)
        if reversed_ladder:
            if m1 < m2 - _MEDIAN_EPS:
                # Cancel the smaller-side offers at t1 — those are the
                # misfit-backed ones that caused the inversion.
                offender_tids = list(trade_ids_by_tier.get(t1, []))
                result.issues.append(Issue(
                    severity=Severity.ERROR,
                    code="ladder_inversion_reverse",
                    message=(
                        f"{side} reverse-ladder INVERSION: {t1} median "
                        f"{float(m1):.4f} XCH < {t2} median {float(m2):.4f} XCH. "
                        f"Inner should be LARGEST in reverse layout."
                    ),
                    suggested_action=(
                        "A misfit-backed offer is at an outer slot. Force-rebuild "
                        "the ladder OR wait for topup to reshape and next natural "
                        "requote to correct the layout."
                    ),
                    details={
                        "side": side,
                        "reverse": True,
                        "tier_a": t1, "median_a": float(m1),
                        "tier_b": t2, "median_b": float(m2),
                        "trade_ids": offender_tids,
                    },
                ))
        else:
            if m1 > m2 + _MEDIAN_EPS:
                offender_tids = list(trade_ids_by_tier.get(t1, []))
                result.issues.append(Issue(
                    severity=Severity.ERROR,
                    code="ladder_inversion_standard",
                    message=(
                        f"{side} standard-ladder INVERSION: {t1} median "
                        f"{float(m1):.4f} XCH > {t2} median {float(m2):.4f} XCH. "
                        f"Inner should be SMALLEST in standard layout."
                    ),
                    suggested_action=(
                        "A misfit-backed offer is at an inner slot. Force-rebuild "
                        "the ladder OR wait for topup to reshape."
                    ),
                    details={
                        "side": side,
                        "reverse": False,
                        "tier_a": t1, "median_a": float(m1),
                        "tier_b": t2, "median_b": float(m2),
                        "trade_ids": offender_tids,
                    },
                ))

    result.ok = not result.has_errors()
    return result


# ---------------------------------------------------------------------------
# Coin-accounting invariant checks
# ---------------------------------------------------------------------------

def check_coin_invariants(
    wallet_totals: Dict[str, int],        # {"xch_total": N, "cat_total": N}
    inventory: Dict[str, Dict[str, int]], # {"xch": {"free": N, "locked": N}, "cat": {...}}
    open_offers_count: Dict[str, int],    # {"buy": N, "sell": N}
    db_locked_count: Dict[str, int],      # {"xch": N, "cat": N}
) -> AuditResult:
    """Cycle-level cross-view reconciliation.

    Checks the accounting invariants that MUST hold between Sage (wallet),
    our in-memory inventory, the DB, and the live-offer book. A violation
    means one of those views has drifted from reality.

    Args:
        wallet_totals: Total coin counts reported by Sage for each type.
        inventory: Our in-memory count of free/locked per wallet type.
        open_offers_count: Count of open offers by side from the DB.
        db_locked_count: DB count of ``status='locked'`` coins per type.

    Invariants checked:
        - inventory.free + inventory.locked == wallet.total (per coin type)
        - db_locked_count[xch] approximately == open_buys (buy offers lock XCH)
        - db_locked_count[cat] approximately == open_sells (sell offers lock CAT)
        - ±1 tolerance for sniper probe coins (one locked probe per side)
    """
    result = AuditResult(ok=True)

    result.summary["wallet_totals"] = dict(wallet_totals)
    result.summary["inventory_totals"] = {
        k: sum(v.values()) for k, v in inventory.items()
    }
    result.summary["open_offers"] = dict(open_offers_count)
    result.summary["db_locked"] = dict(db_locked_count)

    # Invariant 1: inventory totals match wallet totals
    for wtype in ("xch", "cat"):
        inv = inventory.get(wtype, {})
        inv_total = int(inv.get("free", 0) or 0) + int(inv.get("locked", 0) or 0)
        wallet_total = int(wallet_totals.get(f"{wtype}_total", 0) or 0)
        if wallet_total > 0 and inv_total != wallet_total:
            diff = wallet_total - inv_total
            result.issues.append(Issue(
                severity=Severity.WARN,
                code="inventory_count_mismatch",
                message=(
                    f"{wtype.upper()} inventory count ({inv_total}) does not "
                    f"match wallet total ({wallet_total}), diff={diff:+d}"
                ),
                suggested_action=(
                    "Small transient drift expected during mempool settlement. "
                    "If persists >2 cycles, trigger reconcile_with_wallet() "
                    "to re-sync DB and inventory."
                ),
                details={"wallet_type": wtype, "inv_total": inv_total,
                         "wallet_total": wallet_total, "diff": diff},
            ))

    # Invariant 2: DB-locked counts approximately match open offer counts
    # (±2 for sniper probes and temporary requote-transition state)
    xch_locked = int(db_locked_count.get("xch", 0) or 0)
    buy_offers = int(open_offers_count.get("buy", 0) or 0)
    if abs(xch_locked - buy_offers) > 2:
        result.issues.append(Issue(
            severity=Severity.WARN,
            code="xch_locked_vs_buys_mismatch",
            message=(
                f"DB has {xch_locked} XCH coins marked 'locked' but "
                f"{buy_offers} open buy offers. Divergence >2 suggests "
                f"either phantom locks or untracked live offers."
            ),
            suggested_action=(
                "Trigger orphan cleanup and offer-coin relinking. "
                "Check for zombie offers in Sage that don't match the DB."
            ),
            details={"xch_locked": xch_locked, "buy_offers": buy_offers},
        ))

    cat_locked = int(db_locked_count.get("cat", 0) or 0)
    sell_offers = int(open_offers_count.get("sell", 0) or 0)
    if abs(cat_locked - sell_offers) > 2:
        result.issues.append(Issue(
            severity=Severity.WARN,
            code="cat_locked_vs_sells_mismatch",
            message=(
                f"DB has {cat_locked} CAT coins marked 'locked' but "
                f"{sell_offers} open sell offers. Divergence >2 suggests "
                f"either phantom locks or untracked live offers."
            ),
            suggested_action=(
                "Trigger orphan cleanup and offer-coin relinking."
            ),
            details={"cat_locked": cat_locked, "sell_offers": sell_offers},
        ))

    result.ok = not result.has_errors()
    return result


# ---------------------------------------------------------------------------
# Combined periodic audit — called from bot_loop
# ---------------------------------------------------------------------------

def run_periodic_audit(
    *,
    offers_buy: List[Dict[str, Any]],
    offers_sell: List[Dict[str, Any]],
    buy_tier_sizes_xch: Dict[str, Decimal],
    sell_tier_sizes_xch: Dict[str, Decimal],
    buy_tier_counts: Dict[str, int],
    sell_tier_counts: Dict[str, int],
    buy_reversed: bool,
    sell_reversed: bool,
    wallet_totals: Dict[str, int],
    inventory: Dict[str, Dict[str, int]],
    db_locked_count: Dict[str, int],
) -> List[Issue]:
    """Run all audits at once. Returns a flat list of issues.

    Intended for use from ``bot_loop.py``'s periodic integrity tick.
    Call it every N cycles (default 10, configurable).
    """
    issues: List[Issue] = []

    buy_audit = audit_ladder_shape(
        side="buy", offers=offers_buy,
        tier_sizes_xch=buy_tier_sizes_xch,
        tier_counts=buy_tier_counts,
        reversed_ladder=buy_reversed,
    )
    issues.extend(buy_audit.issues)

    sell_audit = audit_ladder_shape(
        side="sell", offers=offers_sell,
        tier_sizes_xch=sell_tier_sizes_xch,
        tier_counts=sell_tier_counts,
        reversed_ladder=sell_reversed,
    )
    issues.extend(sell_audit.issues)

    coin_audit = check_coin_invariants(
        wallet_totals=wallet_totals,
        inventory=inventory,
        open_offers_count={"buy": len(offers_buy), "sell": len(offers_sell)},
        db_locked_count=db_locked_count,
    )
    issues.extend(coin_audit.issues)

    return issues


__all__ = [
    "Severity",
    "Issue",
    "AuditResult",
    "audit_ladder_shape",
    "check_coin_invariants",
    "run_periodic_audit",
]
