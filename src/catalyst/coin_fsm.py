"""Non-blocking FSM validator for coin (status, designation) transitions

Provides pure validation functions that the database write path consults as an
observability safety net. validate_transition(old, new) returns (ok, reason)
for a proposed transition of the coins table row, and is_terminal(state)
identifies dead-end states. Violations are logged but never prevented — the
module exists to surface state-machine bugs without risking a regression from
refusing a write the rest of the code expects to succeed.

Key responsibilities:
    - Define the STATUSES and DESIGNATIONS vocabularies
    - Provide validate_transition() as a pure (old, new) -> (ok, reason) check
    - Provide is_terminal() to identify sink states
    - Remain side-effect free and safe to call from any DB code path

Spent is strictly terminal: once a coin reaches that status no further
transitions are valid. The validator is deliberately stateless so it can be
called from anywhere without threading concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set, Tuple


# -------------------------------------------------------------------------
# State representation
# -------------------------------------------------------------------------

STATUSES: Set[str] = {"free", "locked", "spent", "gone"}
DESIGNATIONS: Set[str] = {
    "reserve",
    "tier_spare",
    "tier_active",
    "dust",
    "unknown",
    "sniper",
    "fees",
}


@dataclass(frozen=True)
class CoinState:
    """Composite state (status, designation) at a point in time."""
    status: str
    designation: str

    def __str__(self) -> str:
        return f"({self.status},{self.designation})"


# -------------------------------------------------------------------------
# Transition rules — the allowed edges in the state graph
#
# Expressed as a dict of (from_status, from_desig) -> set of allowed
# (to_status, to_desig) pairs. A "*" in the to-pair matches any value
# for that component.
# -------------------------------------------------------------------------

# Helper to express "any value" for one half of a state.
_ANY = "*"


def _expand(patterns: Dict[Tuple[str, str], Set[Tuple[str, str]]]) -> Dict[Tuple[str, str], Set[Tuple[str, str]]]:
    """Expand a compact rule dict: convert * wildcards to explicit tuples.

    This lets rule definitions use (from_status, "*") -> ... as a shortcut
    for "any designation". We expand those to concrete state pairs so the
    runtime check is just a set-membership test.
    """
    expanded: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}

    def _expand_key(k: Tuple[str, str]) -> Set[Tuple[str, str]]:
        fs, fd = k
        out: Set[Tuple[str, str]] = set()
        stat_set = STATUSES if fs == _ANY else {fs}
        desig_set = DESIGNATIONS if fd == _ANY else {fd}
        for s in stat_set:
            for d in desig_set:
                out.add((s, d))
        return out

    def _expand_vals(vs: Set[Tuple[str, str]]) -> Set[Tuple[str, str]]:
        out: Set[Tuple[str, str]] = set()
        for v in vs:
            out |= _expand_key(v)
        return out

    for k, v in patterns.items():
        concrete_keys = _expand_key(k)
        concrete_vals = _expand_vals(v)
        for ck in concrete_keys:
            expanded.setdefault(ck, set()).update(concrete_vals)

    return expanded


# The rule set. Each entry: (from_status, from_desig) -> set of allowed
# (to_status, to_desig) successors. See docs/COIN_FSM_DESIGN.md for the
# rationale behind each rule.
_RULES: Dict[Tuple[str, str], Set[Tuple[str, str]]] = _expand({
    # Newly seen coins: unknown → any of the classified designations
    # once classify_coin() runs, OR directly locked if they're picked
    # for an offer before classification resolves.
    ("free", "unknown"): {
        ("free", "tier_spare"),
        ("free", "reserve"),
        ("free", "dust"),
        ("free", "sniper"),
        ("free", "fees"),
        ("free", "unknown"),   # no-op (re-seen before classify)
        ("locked", "tier_active"),
        ("locked", "unknown"),
        ("spent", _ANY),
        ("gone", _ANY),
    },
    # Tier-spare coin: can lock into an offer (becomes tier_active),
    # or be re-designated if classify changes tier size, or absorbed
    # (becomes spent during consolidation).
    ("free", "tier_spare"): {
        ("locked", "tier_active"),
        ("free", "tier_spare"),       # self (tier change)
        ("free", "reserve"),          # reclassified
        ("free", "unknown"),          # shouldn't happen but tolerable
        ("free", "dust"),             # reclassified smaller
        ("spent", _ANY),              # consolidated / filled
        ("gone", _ANY),
    },
    # Tier-active coin (locked in an offer): can return to free on
    # cancel, or be spent on fill.
    ("locked", "tier_active"): {
        ("free", "tier_spare"),       # cancel
        ("free", "tier_active"),      # brief DB state during cancel
        ("spent", _ANY),              # filled
        ("gone", _ANY),
        ("locked", "tier_active"),    # self (no-op)
    },
    # Legacy: coins marked locked but still flagged tier_spare.
    # Transition to tier_active on next write, or spent on fill.
    ("locked", "tier_spare"): {
        ("locked", "tier_active"),    # DB catch-up
        ("free", "tier_spare"),       # cancel
        ("spent", _ANY),
        ("gone", _ANY),
        ("locked", "tier_spare"),     # self
    },
    # Reserve coin: consumed in split or absorb TXs (becomes spent),
    # or demoted to tier_spare after being split down.
    ("free", "reserve"): {
        ("spent", "reserve"),
        ("free", "reserve"),          # self
        ("free", "tier_spare"),       # exceptional reclassify
        ("free", "unknown"),
        ("gone", _ANY),
        ("locked", "tier_active"),    # occasional use as backing
    },
    # Dust: consolidated into reserve (becomes spent in the TX).
    ("free", "dust"): {
        ("spent", "dust"),
        ("free", "dust"),             # self
        ("free", "tier_spare"),       # reclassified up (rare)
        ("free", "unknown"),
        ("gone", _ANY),
    },
    # Sniper pool coins: used in probe offers.
    ("free", "sniper"): {
        ("locked", "sniper"),
        ("free", "sniper"),
        ("spent", _ANY),
        ("gone", _ANY),
    },
    ("locked", "sniper"): {
        ("free", "sniper"),           # probe cancel
        ("spent", _ANY),              # probe fill
        ("gone", _ANY),
    },
    # Fee pool coins: reserved for per-offer fees.
    ("free", "fees"): {
        ("locked", "fees"),
        ("free", "fees"),
        ("spent", _ANY),
        ("gone", _ANY),
    },
    ("locked", "fees"): {
        ("free", "fees"),
        ("spent", _ANY),
        ("gone", _ANY),
    },
    # Gone coins can rarely reappear (unconfirmed spend reversed).
    # Allow reanimation to free status with whatever designation.
    ("gone", _ANY): {
        ("free", _ANY),               # reanimate
        ("gone", _ANY),                # stays gone
    },
    # Spent is effectively terminal — no transitions out.
    # We don't put it in the rules at all, which means
    # validate_transition returns False for any edge from spent.
})


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

def validate_transition(old: CoinState, new: CoinState) -> Tuple[bool, str]:
    """Return ``(ok, reason)`` for a proposed transition.

    ``ok`` is True when the transition is allowed per the rules.
    ``reason`` is a short human-readable string for the log message
    (empty when ok=True).

    Unknown statuses or designations (values outside :data:`STATUSES` /
    :data:`DESIGNATIONS`) return ``(False, "unknown state value")`` —
    surfacing typos in callers.
    """
    # Basic value checks first
    if old.status not in STATUSES:
        return False, f"unknown old status '{old.status}'"
    if old.designation not in DESIGNATIONS:
        return False, f"unknown old designation '{old.designation}'"
    if new.status not in STATUSES:
        return False, f"unknown new status '{new.status}'"
    if new.designation not in DESIGNATIONS:
        return False, f"unknown new designation '{new.designation}'"

    # Identity transition (no-op) is always allowed
    if old == new:
        return True, ""

    from_key = (old.status, old.designation)
    allowed = _RULES.get(from_key)
    if allowed is None:
        # spent is terminal — no transitions out
        if old.status == "spent":
            return False, "spent is terminal"
        return False, f"no rules defined for {old}"

    to_pair = (new.status, new.designation)
    if to_pair in allowed:
        return True, ""

    return False, f"{old} → {new} not in allowed transitions"


def is_terminal(state: CoinState) -> bool:
    """True if ``state`` is a terminal state (spent). Gone is NOT
    terminal — coins can reappear (reanimation)."""
    return state.status == "spent"


__all__ = [
    "CoinState",
    "STATUSES",
    "DESIGNATIONS",
    "validate_transition",
    "is_terminal",
]
