"""Pre-flight deterministic ladder planner — produces a plan, never side effects

`plan_ladder()` takes the current coin inventory, tier configuration, and
requested ladder shape, and returns a slot-by-slot `LadderPlan` describing
which available coin would back each tier slot. Each slot is classified as
EXACT, OVERSIZE_ACCEPTABLE, MISFIT, or NO_COIN, so the caller can decide
whether the plan is viable or whether a reshape is needed first.

Key responsibilities:
    - Map requested tier slots to concrete coins without reserving them
    - Classify each slot's fit quality (EXACT / OVERSIZE_ACCEPTABLE /
      MISFIT / NO_COIN)
    - Expose `is_viable()` and summary helpers for deferral decisions
    - Remain fully pure — no wallet RPC, no DB writes, no locks

Currently exercised primarily via `tests/test_ladder_planner.py`; the plan
output is intended to let the caller defer submission instead of producing
a ragged ladder that later needs cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class SlotStatus(Enum):
    """The readiness state of a single slot in the plan."""
    READY = "ready"                        # tier-correct coin assigned
    OVERSIZE_ACCEPTABLE = "oversize"       # coin > tier but within ceiling
    MISFIT_COIN_AVAILABLE = "misfit_coin"  # only misfit coins available
    NO_COIN_AVAILABLE = "no_coin"          # no candidate coins at all
    SKIPPED = "skipped"                    # slot intentionally skipped (suspended)


@dataclass
class SlotPlan:
    """One slot's entry in the plan."""
    slot_idx: int                        # position in the ladder (0 = innermost)
    tier: str                            # "inner" | "mid" | "outer" | "extreme"
    target_size_mojos: int               # expected tier size (mojos of offer asset)
    target_price: Decimal                # ladder price for this slot
    status: SlotStatus
    coin_id: Optional[str] = None
    coin_amount_mojos: Optional[int] = None
    # For diagnostics when status != READY
    reason: str = ""


@dataclass
class LadderPlan:
    """A full plan for either the buy or sell side."""
    side: str                            # "buy" or "sell"
    mid_price: Decimal
    slots: List[SlotPlan] = field(default_factory=list)
    # Coins the planner consumed from the available pool — for the caller
    # to pass to the executor so it doesn't double-select.
    consumed_coin_ids: List[str] = field(default_factory=list)
    # Reasons the plan is not fully viable (if any)
    blockers: List[str] = field(default_factory=list)
    # Suggested corrective actions (for logging + optional auto-trigger)
    needed_reshapes: List[Dict[str, Any]] = field(default_factory=list)

    def ready_count(self) -> int:
        return sum(1 for s in self.slots if s.status == SlotStatus.READY)

    def oversize_count(self) -> int:
        return sum(1 for s in self.slots if s.status == SlotStatus.OVERSIZE_ACCEPTABLE)

    def unready_count(self) -> int:
        return sum(1 for s in self.slots
                   if s.status in (SlotStatus.NO_COIN_AVAILABLE,
                                   SlotStatus.MISFIT_COIN_AVAILABLE))

    def is_viable(self, min_ready_fraction: float = 0.9) -> bool:
        """Plan is viable when ≥ ``min_ready_fraction`` of slots are READY
        or OVERSIZE_ACCEPTABLE. Default threshold 90% — tolerates a small
        number of gaps (topup can patch them next cycle) but refuses to
        proceed with a majority-misfit ladder."""
        total = len(self.slots)
        if total == 0:
            return False
        healthy = self.ready_count() + self.oversize_count()
        return (healthy / total) >= min_ready_fraction

    def summary(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "total_slots": len(self.slots),
            "ready": self.ready_count(),
            "oversize_acceptable": self.oversize_count(),
            "unready": self.unready_count(),
            "blockers": list(self.blockers),
            "reshapes_needed": len(self.needed_reshapes),
        }


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def plan_ladder(
    *,
    side: str,
    mid_price: Decimal,
    tier_counts: Dict[str, int],
    tier_sizes_asset_mojos: Dict[str, int],
    slot_prices: List[Decimal],
    available_coins: List[Dict[str, Any]],
    spread_fraction: Optional[Decimal] = None,
    reversed_ladder: bool = False,
    reject_misfit_coins: bool = True,
) -> LadderPlan:
    """Produce a ladder plan.

    Args:
        side: "buy" or "sell".
        mid_price: Current mid price (Decimal XCH per CAT).
        tier_counts: e.g. ``{"inner": 10, "mid": 5, "outer": 3, "extreme": 2}``.
        tier_sizes_asset_mojos: For a BUY ladder these are XCH sizes (what we
            spend per offer); for a SELL ladder, CAT sizes. The unit must
            match the coins in ``available_coins``.
        slot_prices: List of prices (one per slot) in the order inner →
            extreme. Pre-computed by the caller's existing price-spacing
            math — the planner doesn't recompute this.
        available_coins: List of ``{"coin_id": str, "amount_mojos": int,
            "designation": str, "assigned_tier": str}`` for free coins
            the planner can allocate from.
        spread_fraction: Not used directly (caller pre-computed slot_prices)
            but included for log/debug context in the plan.
        reversed_ladder: True when the ladder is configured reverse (large
            at inner). Only affects the per-slot tier assignment direction.
            For a SELL ladder on a reverse config, slot 0 (inner) still uses
            the ``"inner"`` tier size — this flag doesn't change the
            tier→slot mapping here, it's reflected in which SIZE each tier
            points to (tier_sizes_asset_mojos already encodes that).
        reject_misfit_coins: When True (default), coins classified as
            MISFIT by :func:`coin_classifier.classify_coin` are excluded
            from candidate lists. When False, misfit coins are allowed
            but flagged in slot.status.

    Returns:
        :class:`LadderPlan`.
    """
    from coin_classifier import classify_coin

    plan = LadderPlan(side=side, mid_price=mid_price)

    # Build the expected slot→tier sequence (inner slots first, then mid, etc.).
    # The ordering of slot_prices must match: slot_prices[0] is the innermost.
    tier_order = ["inner", "mid", "outer", "extreme"]
    slot_tiers: List[str] = []
    for tier in tier_order:
        slot_tiers.extend([tier] * int(tier_counts.get(tier, 0) or 0))

    # Sanity: if slot_prices has fewer entries than the tier_count sum, trim
    # the plan to match (the caller might be doing a partial requote).
    n_slots = min(len(slot_tiers), len(slot_prices))
    if n_slots < len(slot_tiers):
        slot_tiers = slot_tiers[:n_slots]

    # Pre-classify all available coins once; lets us categorise them for
    # efficient per-slot matching.
    coins_by_tier: Dict[str, List[Dict[str, Any]]] = {t: [] for t in tier_order}
    misfit_coins: List[Dict[str, Any]] = []
    other_coins: List[Dict[str, Any]] = []  # dust / reserve / zero-tier

    for coin in (available_coins or []):
        amount = int(coin.get("amount_mojos") or coin.get("amount") or 0)
        if amount <= 0:
            continue
        cls = classify_coin(amount, tier_sizes_asset_mojos)
        if cls.is_misfit:
            misfit_coins.append(coin)
            continue
        if cls.best_tier in coins_by_tier:
            coins_by_tier[cls.best_tier].append(coin)
        else:
            other_coins.append(coin)

    # Sort each tier's bucket by surplus (smallest first) so we use the
    # closest-fitting coin for each slot. Misfit and other coins stay in
    # their lists unsorted.
    for t in tier_order:
        coins_by_tier[t].sort(
            key=lambda c: int(c.get("amount_mojos") or c.get("amount") or 0)
        )

    consumed_ids: set = set()

    # Iterate slots and assign coins. Greedy nearest-fit: prefer a tier's
    # own bucket; if empty, try oversize from a SMALLER tier's bucket only
    # if that coin still fits the current tier's ceiling.
    for slot_idx, (tier, price) in enumerate(zip(slot_tiers, slot_prices)):
        tier_size = tier_sizes_asset_mojos.get(tier, 0)

        # Pick the best-fitting coin for this slot.
        coin = None
        status = SlotStatus.NO_COIN_AVAILABLE
        reason = ""

        for candidate in coins_by_tier.get(tier, []):
            cid = str(candidate.get("coin_id") or "").lower()
            if not cid or cid in consumed_ids:
                continue
            coin = candidate
            # Decide EXACT vs OVERSIZE_ACCEPTABLE based on amount vs tier_size.
            amount = int(candidate.get("amount_mojos") or 0)
            if amount <= tier_size:
                status = SlotStatus.READY
            else:
                status = SlotStatus.OVERSIZE_ACCEPTABLE
            break

        if coin is None:
            # No tier-exact match. Can we use a misfit?
            if not reject_misfit_coins and misfit_coins:
                for cand in misfit_coins:
                    cid = str(cand.get("coin_id") or "").lower()
                    if cid and cid not in consumed_ids:
                        coin = cand
                        status = SlotStatus.MISFIT_COIN_AVAILABLE
                        reason = (
                            f"No tier-exact coin for {tier}; using misfit "
                            f"{amount_fmt(int(cand.get('amount_mojos') or 0))} "
                            f"(ladder shape will be off)"
                        )
                        break
            if coin is None:
                reason = (
                    f"No {tier} tier-correct coin available (and no fallback). "
                    f"{len(misfit_coins)} misfit coin(s) present — reshape required."
                )
                plan.slots.append(SlotPlan(
                    slot_idx=slot_idx,
                    tier=tier,
                    target_size_mojos=tier_size,
                    target_price=price,
                    status=status,
                    reason=reason,
                ))
                continue

        amount = int(coin.get("amount_mojos") or 0)
        cid = str(coin.get("coin_id") or "").lower()
        consumed_ids.add(cid)
        plan.consumed_coin_ids.append(cid)

        plan.slots.append(SlotPlan(
            slot_idx=slot_idx,
            tier=tier,
            target_size_mojos=tier_size,
            target_price=price,
            status=status,
            coin_id=cid,
            coin_amount_mojos=amount,
            reason=reason,
        ))

    # Aggregate reshape needs — one entry per missing tier.
    missing_by_tier: Dict[str, int] = {}
    for s in plan.slots:
        if s.status in (SlotStatus.NO_COIN_AVAILABLE, SlotStatus.MISFIT_COIN_AVAILABLE):
            missing_by_tier[s.tier] = missing_by_tier.get(s.tier, 0) + 1
    for tier, count in missing_by_tier.items():
        plan.needed_reshapes.append({
            "tier": tier,
            "shortfall": count,
            "target_size_mojos": tier_sizes_asset_mojos.get(tier, 0),
        })

    if misfit_coins and any(
        s.status in (SlotStatus.NO_COIN_AVAILABLE, SlotStatus.MISFIT_COIN_AVAILABLE)
        for s in plan.slots
    ):
        plan.blockers.append(
            f"{len(misfit_coins)} misfit coin(s) need to be absorbed+split "
            f"into tier-correct coins before the ladder can be built cleanly."
        )

    return plan


def amount_fmt(mojos: int) -> str:
    """Friendly amount formatter for logs."""
    if mojos >= 1_000_000_000_000:
        return f"{mojos/1e12:.4f} XCH"
    if mojos >= 1_000_000:
        return f"{mojos/1000:.2f} CAT"
    return f"{mojos} mojos"


__all__ = [
    "SlotStatus",
    "SlotPlan",
    "LadderPlan",
    "plan_ladder",
]
