"""Single source of truth for coin-to-tier classification

classify_coin() returns a CoinClassification dataclass that answers, for a
given coin amount, "can this coin back an offer at tier X?" The return value
carries a CoinFit enum (EXACT, OVERSIZE_FIT, UNDER_FLOOR, OVER_CEILING), the
suggested designation, and both a best_tier (if one fits) and a nearest_tier
(for diagnostics only). This module replaces five inconsistent ad-hoc
classifiers that previously lived in coin_manager, database, and
coin_prep_worker with one deterministic set of thresholds.

Key responsibilities:
    - Provide a single classify_coin() entry point used across the codebase
    - Define the CoinFit vocabulary and CoinClassification result shape
    - Let callers pass custom (floor_tolerance, max_ratio) without forking logic
    - Expose nearest_tier strictly for logs; best_tier is the actionable answer

Call sites with different strictness (misfit absorber vs offer selector vs
reconciler) vary only by passing different tolerance parameters — the core
decision logic is shared so all subsystems agree on what a coin is for.
    # cls.designation == "unknown"  (for DB write)
    # cls.candidates == {"inner": CoinFit.UNDER_FLOOR, "mid": CoinFit.OVER_CEILING, ...}

Rules:
    1. A coin is a MISFIT when it does not fit any tier at strict bounds.
       It cannot back an offer (offer selector must reject it).
       It is a reshape candidate (absorber must absorb it into reserve).

    2. A coin is an EXACT match for a tier when floor <= amount <= tier_size.
       This is the preferred-use tier.

    3. A coin is an OVERSIZE_FIT for a tier when tier_size < amount <= ceiling.
       Usable for that tier (with acceptable slack), but prefer an exact match
       if one exists.

    4. Coins above the LARGEST tier ceiling are RESERVE candidates.
    5. Coins below the SMALLEST tier floor are DUST.

All thresholds are inclusive on the floor side, inclusive on the ceiling side.
Thresholds are applied as integer mojos after multiplying by Decimal to avoid
float drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Canonical thresholds — the ONLY place these are defined
# ---------------------------------------------------------------------------

# Floor tolerance for tier-backed offers. A coin must be at least this
# fraction of the tier size to back an offer in that tier without forcing a
# reshape. 0.98 means "within 2% of tier size on the small side". Chosen to
# accommodate fee-rounding artefacts from coin-prep splits (a freshly-minted
# coin can be a handful of mojos below the exact tier floor) while still
# rejecting genuine misfits (like tonight's 23.4k coin at 12% below inner).
DEFAULT_FLOOR_TOLERANCE = Decimal("0.98")

# Maximum size ratio — a coin up to this fraction ABOVE tier size is usable
# for that tier (change coins from fills, coins slightly oversize due to
# fee accounting). Previously this was COIN_MAX_SIZE_RATIO=1.5 in .env.
# Using it consistently here.
DEFAULT_MAX_RATIO = Decimal("1.5")

# Dust threshold — coins below this fraction of the smallest tier are dust.
# Not usable for any tier offer; candidates for consolidation.
DEFAULT_DUST_FRACTION = Decimal("0.5")

# Reserve promotion threshold — coins ABOVE this multiple of the largest
# tier are reserve candidates (too big for any tier slot; function as
# topup fuel). Tracks DEFAULT_MAX_RATIO so there is no gap between the
# largest tier's ceiling and the reserve floor. Before 2026-04-22 this was
# 2.0 while max_ratio was 1.5, which left coins in the 1.5×–2.0× range as
# misfits (UNKNOWN) — seen in practice when a split's change coin (e.g. an
# 8 XCH leftover from an extreme-tier split) sat unclassified while the
# reserve showed topup_pool=0. With the threshold pulled down, those
# change coins land in reserve and the topup pool refills naturally.
DEFAULT_RESERVE_MULTIPLE = Decimal("1.5")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class CoinFit(Enum):
    """How well a coin fits a given tier."""
    EXACT = "exact"              # floor <= amount <= tier_size
    OVERSIZE_FIT = "oversize"    # tier_size < amount <= ceiling (ratio * size)
    UNDER_FLOOR = "under"        # amount < floor — too small for this tier
    OVER_CEILING = "over"        # amount > ceiling — too big for this tier


class CoinDesignation(Enum):
    """Canonical designation vocabulary. Matches the DB column values."""
    TIER_SPARE = "tier_spare"    # free coin, sized for a trading tier
    TIER_ACTIVE = "tier_active"  # coin currently backing an active offer
    RESERVE = "reserve"          # too big for any tier, used as topup fuel
    DUST = "dust"                # too small for any tier
    UNKNOWN = "unknown"          # misfit — doesn't cleanly fit any bucket
    SNIPER = "sniper"            # dedicated sniper-pool coin (distinct sizing)
    FEES = "fees"                # dedicated fee-pool coin (distinct sizing)


@dataclass
class CoinClassification:
    """The full classification result for one coin.

    `best_tier` is the tier name to actually USE this coin for (None if the
    coin is dust/misfit/reserve). `nearest_tier` is diagnostic only — the
    closest tier by size — and is populated even for misfits so callers
    logging the classification can show "this misfit is closest to inner".
    """
    amount_mojos: int
    fit: CoinFit                           # overall best fit across all tiers
    best_tier: Optional[str]                # tier to USE this coin for, or None
    nearest_tier: Optional[str]             # diagnostic only
    designation: CoinDesignation            # recommended DB designation
    candidates: Dict[str, CoinFit] = field(default_factory=dict)
                                            # per-tier fit, for debugging
    is_misfit: bool = False                 # convenience: true when best_tier is None
                                            # AND designation is UNKNOWN (not dust/reserve)


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

def classify_coin(
    amount_mojos: int,
    tier_sizes_mojos: Dict[str, int],
    *,
    floor_tolerance: Decimal = DEFAULT_FLOOR_TOLERANCE,
    max_ratio: Decimal = DEFAULT_MAX_RATIO,
    dust_fraction: Decimal = DEFAULT_DUST_FRACTION,
    reserve_multiple: Decimal = DEFAULT_RESERVE_MULTIPLE,
) -> CoinClassification:
    """Classify a single coin against the configured tiers.

    Returns a :class:`CoinClassification` with a unique, authoritative
    verdict: either the coin fits a specific tier (``best_tier`` set),
    or it belongs in one of the non-tier categories (dust/reserve/misfit).

    Args:
        amount_mojos: Coin size in mojos. Must be > 0.
        tier_sizes_mojos: Mapping of tier name -> tier size in mojos. Keys
            are typically ``{"inner", "mid", "outer", "extreme"}`` for the
            trading tiers (sniper/fees are excluded from this mapping since
            they use distinct flat sizes). Tiers may be passed in any order
            — the classifier handles ordering internally.
        floor_tolerance: Fraction of tier size below which a coin is
            UNDER_FLOOR for that tier. Default 0.98 (within 2%).
        max_ratio: Fraction of tier size above which a coin is OVER_CEILING
            for that tier. Default 1.5.
        dust_fraction: Fraction of smallest tier below which a coin is dust.
            Default 0.5.
        reserve_multiple: Multiple of largest tier above which a coin is a
            reserve candidate. Default 2.0.

    Returns:
        :class:`CoinClassification`.

    Invariants:
        - If ``fit`` is EXACT or OVERSIZE_FIT, ``best_tier`` is non-None.
        - If ``best_tier`` is None, ``is_misfit`` may be True (designation
          UNKNOWN), OR designation is DUST / RESERVE (both legitimate).
        - ``nearest_tier`` is populated for every classification with ≥1 tier
          defined.
    """
    if amount_mojos <= 0 or not tier_sizes_mojos:
        return CoinClassification(
            amount_mojos=amount_mojos,
            fit=CoinFit.UNDER_FLOOR,
            best_tier=None,
            nearest_tier=None,
            designation=CoinDesignation.UNKNOWN,
            is_misfit=True,
        )

    # Normalise inputs and sort tiers by size (smallest first) so we can
    # apply dust and reserve thresholds against the canonical extremes.
    # Also filter out zero/negative tiers silently — they're misconfigured
    # callers, and we don't want to classify against them.
    valid = {
        str(t): int(s)
        for t, s in tier_sizes_mojos.items()
        if int(s or 0) > 0
    }
    if not valid:
        return CoinClassification(
            amount_mojos=amount_mojos,
            fit=CoinFit.UNDER_FLOOR,
            best_tier=None,
            nearest_tier=None,
            designation=CoinDesignation.UNKNOWN,
            is_misfit=True,
        )

    tiers_sorted = sorted(valid.items(), key=lambda kv: kv[1])
    smallest_name, smallest_size = tiers_sorted[0]
    largest_name, largest_size = tiers_sorted[-1]

    # ---- Check dust and reserve extremes first ---------------------------
    # Dust: below dust_fraction × smallest. Not a misfit (explicit category).
    dust_threshold = int(Decimal(smallest_size) * dust_fraction)
    if amount_mojos < dust_threshold:
        return CoinClassification(
            amount_mojos=amount_mojos,
            fit=CoinFit.UNDER_FLOOR,
            best_tier=None,
            nearest_tier=smallest_name,
            designation=CoinDesignation.DUST,
            candidates={smallest_name: CoinFit.UNDER_FLOOR},
            is_misfit=False,   # dust is its own category, not a misfit
        )

    # Reserve: above reserve_multiple × largest. Not a misfit (topup fuel).
    # Strict `>` so that a coin at exactly reserve_multiple × largest still
    # flows into the tier loop and registers as OVERSIZE_FIT of the largest
    # tier (preserves the candidates map callers expect for diagnostics).
    # When reserve_multiple tracks max_ratio, the coin would match the
    # largest tier's ceiling and exit as OVERSIZE_FIT; any amount strictly
    # above that goes to reserve.
    reserve_threshold = int(Decimal(largest_size) * reserve_multiple)
    if amount_mojos > reserve_threshold:
        return CoinClassification(
            amount_mojos=amount_mojos,
            fit=CoinFit.OVER_CEILING,
            best_tier=None,
            nearest_tier=largest_name,
            designation=CoinDesignation.RESERVE,
            candidates={largest_name: CoinFit.OVER_CEILING},
            is_misfit=False,   # reserve is its own category, not a misfit
        )

    # ---- Check each tier for EXACT / OVERSIZE_FIT / UNDER_FLOOR / OVER_CEILING
    candidates: Dict[str, CoinFit] = {}
    best_tier: Optional[str] = None
    best_fit: Optional[CoinFit] = None
    # Track nearest tier by absolute distance from the coin amount
    nearest_tier = smallest_name
    nearest_distance = abs(amount_mojos - smallest_size)

    for tier_name, tier_size in tiers_sorted:
        floor = int(Decimal(tier_size) * floor_tolerance)
        ceiling = int(Decimal(tier_size) * max_ratio)

        if amount_mojos < floor:
            candidates[tier_name] = CoinFit.UNDER_FLOOR
        elif amount_mojos <= tier_size:
            candidates[tier_name] = CoinFit.EXACT
            if best_fit is None or best_fit == CoinFit.OVERSIZE_FIT:
                # EXACT beats OVERSIZE_FIT; also pick the FIRST (smallest)
                # exact match when multiple exist — prefer the smallest
                # tier the coin fits into (so inner-size coins don't
                # get wasted on mid slots).
                best_tier = tier_name
                best_fit = CoinFit.EXACT
        elif amount_mojos <= ceiling:
            candidates[tier_name] = CoinFit.OVERSIZE_FIT
            if best_fit is None:
                best_tier = tier_name
                best_fit = CoinFit.OVERSIZE_FIT
            # Don't upgrade from EXACT to OVERSIZE_FIT; keep the EXACT match.
        else:
            candidates[tier_name] = CoinFit.OVER_CEILING

        dist = abs(amount_mojos - tier_size)
        if dist < nearest_distance:
            nearest_distance = dist
            nearest_tier = tier_name

    # ---- Decide the overall verdict --------------------------------------
    if best_tier is not None:
        return CoinClassification(
            amount_mojos=amount_mojos,
            fit=best_fit or CoinFit.EXACT,
            best_tier=best_tier,
            nearest_tier=nearest_tier,
            designation=CoinDesignation.TIER_SPARE,
            candidates=candidates,
            is_misfit=False,
        )

    # No tier fits — this is a misfit. Pick the most-informative fit
    # (closest side of which tier we missed). This is diagnostic only;
    # callers should NOT use best_tier when is_misfit is True.
    # For misfits, derive the "dominant miss direction": if the coin
    # is larger than every tier's ceiling, the fit summary is OVER_CEILING.
    # If it's smaller than every tier's floor, UNDER_FLOOR. If mixed
    # (e.g. over one tier's ceiling, under another's floor), prefer the
    # "smaller than floor" summary since such coins CAN be consolidated
    # with others up to tier size, whereas "over ceiling" coins typically
    # have to go through reserve absorption.
    any_over = any(f == CoinFit.OVER_CEILING for f in candidates.values())
    any_under = any(f == CoinFit.UNDER_FLOOR for f in candidates.values())
    if any_under and not any_over:
        summary_fit = CoinFit.UNDER_FLOOR
    elif any_over and not any_under:
        summary_fit = CoinFit.OVER_CEILING
    else:
        # Mixed (typical for coins that sit between two tiers) — treat as
        # UNDER_FLOOR of the nearest-above tier since that's the shape most
        # callers care about for reshape.
        summary_fit = CoinFit.UNDER_FLOOR

    return CoinClassification(
        amount_mojos=amount_mojos,
        fit=summary_fit,
        best_tier=None,
        nearest_tier=nearest_tier,
        designation=CoinDesignation.UNKNOWN,
        candidates=candidates,
        is_misfit=True,
    )


# ---------------------------------------------------------------------------
# Convenience wrappers — maintain backward-compat with the 5 old APIs
# ---------------------------------------------------------------------------

def is_misfit_coin(
    amount_mojos: int,
    tier_sizes_mojos: Dict[str, int],
    max_size_ratio: float = float(DEFAULT_MAX_RATIO),
    floor_tolerance: float = float(DEFAULT_FLOOR_TOLERANCE),
) -> bool:
    """Legacy-compatible misfit check. Preserves the old signature so
    existing callers can route through classify_coin() unchanged.

    Returns True if the coin cannot back an offer in ANY tier at the
    given bounds. Note: this returns True only for the UNKNOWN
    designation category; dust and reserve coins return False (they are
    not misfits — they belong in their respective explicit categories).
    """
    # If the ratio guard is disabled upstream (float('inf') pattern),
    # treat it as a very large but finite multiplier so we don't lose
    # decimal precision.
    if max_size_ratio == float("inf") or max_size_ratio <= 0:
        _max = Decimal("1000000")
    else:
        _max = Decimal(str(max_size_ratio))
    _floor = Decimal(str(floor_tolerance))
    cls = classify_coin(
        amount_mojos, tier_sizes_mojos,
        floor_tolerance=_floor, max_ratio=_max,
    )
    return cls.is_misfit


def infer_designation_by_size(
    amount_mojos: int,
    tier_sizes_mojos: Dict[str, int],
) -> Tuple[str, str]:
    """Legacy-compatible designation inference. Returns
    ``(designation_str, assigned_tier_str)`` matching the old API used by
    :func:`_infer_designation_by_size` and
    :func:`_infer_reconcile_designation_by_size`.

    **Key change from the old behaviour:** uses the strict 0.98/1.5 bounds
    via classify_coin() instead of the previous 0.80/1.20 bounds. This means
    coins that would previously have been classified as ``tier_spare/inner``
    at 85–98% of inner size are now ``unknown/none`` (misfits) and will be
    correctly absorbed into reserve rather than used to back malformed
    offers. This is the permanent fix for the 2026-04-17 ladder bug.
    """
    cls = classify_coin(amount_mojos, tier_sizes_mojos)
    if cls.designation == CoinDesignation.DUST:
        return ("dust", "none")
    if cls.designation == CoinDesignation.RESERVE:
        return ("reserve", "none")
    if cls.designation == CoinDesignation.TIER_SPARE and cls.best_tier:
        return ("tier_spare", cls.best_tier)
    # Misfit / unknown
    return ("unknown", "none")


__all__ = [
    "CoinFit",
    "CoinDesignation",
    "CoinClassification",
    "DEFAULT_FLOOR_TOLERANCE",
    "DEFAULT_MAX_RATIO",
    "DEFAULT_DUST_FRACTION",
    "DEFAULT_RESERVE_MULTIPLE",
    "classify_coin",
    "is_misfit_coin",
    "infer_designation_by_size",
]
