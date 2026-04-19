"""Graduated requote-severity framework for incremental, budget-capped reactions

Instead of wholesale ladder rebuilds on every price move, this module gives
`bot_loop` and `offer_manager` a shared vocabulary for reacting proportionally
to drift: a severity enum, a per-cycle action budget, and pure classification
helpers that decide which tiers and offers need attention this cycle.

Key responsibilities:
    - `RequoteSeverity` enum — NONE / INNER / INNER_MID / FULL / EMERGENCY
    - `CycleBudget` dataclass — caps cancels, requotes, and replacements
    - `classify_drift` — drift magnitude to severity
    - `tiers_for_severity` — severity to affected tier set
    - `compute_offer_staleness` and `filter_offers_by_tiers` — selection helpers

All functions here are pure: no DB, no wallet, no side effects. Consumers
combine these signals with the natural 24h staggered expiry rotation, which
remains the primary self-correction mechanism for small drifts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, List

# ---------------------------------------------------------------------------
# RequoteSeverity — graduated response to price drift
# ---------------------------------------------------------------------------

class RequoteSeverity(Enum):
    """How urgently offers need adjusting based on price drift magnitude."""
    NONE      = "none"         # No action needed
    INNER     = "inner"        # Small drift — adjust inner offers only
    INNER_MID = "inner_mid"    # Medium drift — adjust inner + mid
    FULL      = "full"         # Large drift — adjust all tiers (budget-capped)
    EMERGENCY = "emergency"    # Offers are arbable — cancel immediately


# ---------------------------------------------------------------------------
# CycleBudget — caps how many on-chain actions the bot can take per cycle
# ---------------------------------------------------------------------------

@dataclass
class CycleBudget:
    """Tracks how many cancels/creates the bot has left this cycle.

    Threaded through _handle_requoting, Step 7, and _create_offers_if_needed.
    Any action that doesn't fit in the budget carries over naturally to the
    next cycle (the offer stays stale until then — acceptable per strategy).
    """
    max_cancels: int = 6
    max_creates: int = 6
    cancels_used: int = 0
    creates_used: int = 0

    def can_cancel(self, n: int = 1) -> bool:
        return self.cancels_used + n <= self.max_cancels

    def use_cancel(self, n: int = 1):
        self.cancels_used += n

    def can_create(self, n: int = 1) -> bool:
        return self.creates_used + n <= self.max_creates

    def use_create(self, n: int = 1):
        self.creates_used += n

    @property
    def remaining_cancels(self) -> int:
        return max(0, self.max_cancels - self.cancels_used)

    @property
    def remaining_creates(self) -> int:
        return max(0, self.max_creates - self.creates_used)

    @property
    def remaining_total(self) -> int:
        return self.remaining_cancels + self.remaining_creates

    @property
    def exhausted(self) -> bool:
        return self.remaining_cancels <= 0 and self.remaining_creates <= 0

    def __repr__(self):
        return (f"CycleBudget(cancels={self.cancels_used}/{self.max_cancels}, "
                f"creates={self.creates_used}/{self.max_creates})")


# ---------------------------------------------------------------------------
# Staleness scoring — which offers are most out-of-position?
# ---------------------------------------------------------------------------

def compute_offer_staleness(offer: Dict, ideal_price: Decimal) -> Decimal:
    """How far an offer is from where it should be, as a fraction of ideal.

    Returns 0.0 for perfectly placed, 0.05 for 5% off, etc.
    Higher = more urgent to fix.
    """
    try:
        actual = Decimal(str(offer.get("price_xch") or "0"))
    except Exception:
        return Decimal("0")
    if ideal_price <= 0 or actual <= 0:
        return Decimal("0")
    return abs(actual - ideal_price) / ideal_price


def classify_drift(move_fraction: Decimal,
                   inner_threshold: Decimal = Decimal("0.003"),
                   mid_threshold: Decimal = Decimal("0.008"),
                   full_threshold: Decimal = Decimal("0.02"),
                   emergency_threshold: Decimal = Decimal("0.05"),
                   ) -> RequoteSeverity:
    """Map a price-move fraction to a graduated severity level.

    Thresholds (as fractions, not bps):
      inner_threshold:     0.003  = 30 bps  (current REQUOTE_BPS default)
      mid_threshold:       0.008  = 80 bps  (current AMM_DRIFT_REQUOTE_BPS)
      full_threshold:      0.02   = 200 bps
      emergency_threshold: 0.05   = 500 bps
    """
    if move_fraction >= emergency_threshold:
        return RequoteSeverity.EMERGENCY
    if move_fraction >= full_threshold:
        return RequoteSeverity.FULL
    if move_fraction >= mid_threshold:
        return RequoteSeverity.INNER_MID
    if move_fraction >= inner_threshold:
        return RequoteSeverity.INNER
    return RequoteSeverity.NONE


# Tier ordering for budget-priority filtering
TIER_PRIORITY = {"inner": 0, "mid": 1, "outer": 2, "extreme": 3}

def tiers_for_severity(severity: RequoteSeverity) -> set:
    """Which tiers should be processed for a given severity."""
    if severity == RequoteSeverity.EMERGENCY:
        return {"inner", "mid", "outer", "extreme"}  # all — cancel what's arbable
    if severity == RequoteSeverity.FULL:
        return {"inner", "mid", "outer", "extreme"}  # all but budget-limited
    if severity == RequoteSeverity.INNER_MID:
        return {"inner", "mid"}
    if severity == RequoteSeverity.INNER:
        return {"inner"}
    return set()


def filter_offers_by_tiers(offers: List[Dict],
                           allowed_tiers: set) -> List[Dict]:
    """Keep only offers whose tier is in the allowed set."""
    return [o for o in offers
            if str(o.get("tier") or "mid").lower() in allowed_tiers]
