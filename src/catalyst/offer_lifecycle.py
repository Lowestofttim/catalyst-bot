"""Pure state-machine definitions for offer tracking

Defines the `OfferState` and `OfferSignal` enums plus `apply_signal(state, signal)
-> OfferTransition`, which is the single authority for legal offer state changes.
This module is stateless and has no external dependencies — callers feed in a
state and a signal and receive back the new state and a recommended action.

Key responsibilities:
    - Enumerate extended lifecycle states and the signals that drive them
    - Compute deterministic `(state, signal) -> transition` results
    - Preserve backward compatibility with the legacy 4-value `status` column
      through `coarse_status()`

The `REFRESH_POSTED` signal transitions `REFRESH_DUE -> CANCELLED` because the
refreshed offer is a new row that replaces the old one; the old ID is treated
as cancelled rather than filled or expired.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum
    class StrEnum(str, Enum):
        pass


class OfferState(StrEnum):
    """Extended offer lifecycle states."""
    OPEN = "open"                              # live on wallet, tradeable
    REFRESH_DUE = "refresh_due"                # expiry approaching, needs requote
    CANCEL_REQUESTED = "cancel_requested"      # cancel RPC sent, awaiting confirmation
    CANCELLED = "cancelled"                    # terminal: cancel confirmed
    MEMPOOL_OBSERVED = "mempool_observed"       # potential take seen in mempool
    FILLED = "filled"                          # terminal: fill detected & verified
    EXPIRED = "expired"                        # terminal: time-expired
    PHANTOM_REJECTED = "phantom_rejected"      # terminal: self-spend/false fill rejected


class OfferSignal(StrEnum):
    """Signals that drive state transitions."""
    EXPIRY_NEAR = "expiry_near"                # refresh window entered
    CANCEL_SENT = "cancel_sent"                # cancel RPC dispatched
    CANCEL_CONFIRMED = "cancel_confirmed"      # wallet confirmed cancel
    CANCEL_FAILED = "cancel_failed"            # cancel RPC failed, revert to previous
    FILL_DETECTED = "fill_detected"            # offer disappeared (not our cancel)
    FILL_VERIFIED = "fill_verified"            # on-chain verification passed
    FILL_REJECTED = "fill_rejected"            # phantom/self-spend detected
    TIME_EXPIRED = "time_expired"              # max_time passed
    REFRESH_POSTED = "refresh_posted"          # replacement offer created
    MEMPOOL_SEEN = "mempool_seen"              # potential take in mempool


@dataclass(frozen=True, slots=True)
class OfferTransition:
    """Result of applying a signal to a state."""
    old_state: OfferState
    new_state: OfferState
    signal: OfferSignal
    action: str      # what the caller should do
    reason: str      # human-readable explanation


# Terminal states — no further transitions allowed
_TERMINAL_STATES = frozenset({
    OfferState.CANCELLED,
    OfferState.FILLED,
    OfferState.EXPIRED,
    OfferState.PHANTOM_REJECTED,
})


def apply_signal(state: OfferState, signal: OfferSignal) -> OfferTransition:
    """Pure function: apply a signal to an offer state, return transition.

    If the signal is invalid for the current state, returns a noop transition.
    """
    # Terminal states reject all signals
    if state in _TERMINAL_STATES:
        return OfferTransition(
            old_state=state, new_state=state, signal=signal,
            action="noop", reason="offer_in_terminal_state",
        )

    # ---- OPEN state transitions ----
    if state == OfferState.OPEN:
        if signal == OfferSignal.EXPIRY_NEAR:
            return OfferTransition(
                old_state=state, new_state=OfferState.REFRESH_DUE,
                signal=signal, action="schedule_requote",
                reason="refresh_window_entered",
            )
        if signal == OfferSignal.CANCEL_SENT:
            return OfferTransition(
                old_state=state, new_state=OfferState.CANCEL_REQUESTED,
                signal=signal, action="await_cancel_confirm",
                reason="cancel_dispatched",
            )
        if signal == OfferSignal.FILL_DETECTED:
            return OfferTransition(
                old_state=state, new_state=OfferState.FILLED,
                signal=signal, action="record_fill",
                reason="offer_disappeared_not_our_cancel",
            )
        if signal == OfferSignal.TIME_EXPIRED:
            return OfferTransition(
                old_state=state, new_state=OfferState.EXPIRED,
                signal=signal, action="cleanup_expired",
                reason="offer_time_expired",
            )
        if signal == OfferSignal.MEMPOOL_SEEN:
            return OfferTransition(
                old_state=state, new_state=OfferState.MEMPOOL_OBSERVED,
                signal=signal, action="mark_mempool_observed",
                reason="potential_take_seen",
            )

    # ---- REFRESH_DUE transitions ----
    if state == OfferState.REFRESH_DUE:
        if signal == OfferSignal.REFRESH_POSTED:
            # This offer is being replaced — mark it cancelled
            return OfferTransition(
                old_state=state, new_state=OfferState.CANCELLED,
                signal=signal, action="track_replacement",
                reason="offer_replaced_by_refresh",
            )
        if signal == OfferSignal.CANCEL_SENT:
            return OfferTransition(
                old_state=state, new_state=OfferState.CANCEL_REQUESTED,
                signal=signal, action="await_cancel_confirm",
                reason="cancel_during_refresh",
            )
        if signal == OfferSignal.FILL_DETECTED:
            return OfferTransition(
                old_state=state, new_state=OfferState.FILLED,
                signal=signal, action="record_fill",
                reason="filled_while_awaiting_refresh",
            )
        if signal == OfferSignal.TIME_EXPIRED:
            return OfferTransition(
                old_state=state, new_state=OfferState.EXPIRED,
                signal=signal, action="cleanup_expired",
                reason="expired_before_refresh",
            )
        if signal == OfferSignal.MEMPOOL_SEEN:
            return OfferTransition(
                old_state=state, new_state=OfferState.MEMPOOL_OBSERVED,
                signal=signal, action="mark_mempool_observed",
                reason="potential_take_while_refresh_due",
            )

    # ---- CANCEL_REQUESTED transitions ----
    if state == OfferState.CANCEL_REQUESTED:
        if signal == OfferSignal.CANCEL_CONFIRMED:
            return OfferTransition(
                old_state=state, new_state=OfferState.CANCELLED,
                signal=signal, action="finalize_cancel",
                reason="cancel_confirmed_by_wallet",
            )
        if signal == OfferSignal.CANCEL_FAILED:
            # Revert to open — cancel didn't take
            return OfferTransition(
                old_state=state, new_state=OfferState.OPEN,
                signal=signal, action="retry_or_revert",
                reason="cancel_rpc_failed",
            )
        if signal == OfferSignal.FILL_DETECTED:
            # Race: filled while cancel was in flight
            return OfferTransition(
                old_state=state, new_state=OfferState.FILLED,
                signal=signal, action="record_fill",
                reason="filled_during_cancel",
            )
        if signal == OfferSignal.TIME_EXPIRED:
            return OfferTransition(
                old_state=state, new_state=OfferState.EXPIRED,
                signal=signal, action="cleanup_expired",
                reason="expired_during_cancel",
            )
        if signal == OfferSignal.MEMPOOL_SEEN:
            # Mempool take observed while cancel is in flight — note but
            # stay in cancel_requested; the fill or cancel will resolve it
            return OfferTransition(
                old_state=state, new_state=state,
                signal=signal, action="note_mempool_during_cancel",
                reason="mempool_seen_but_cancel_pending",
            )

    # ---- MEMPOOL_OBSERVED transitions ----
    if state == OfferState.MEMPOOL_OBSERVED:
        if signal == OfferSignal.FILL_DETECTED:
            return OfferTransition(
                old_state=state, new_state=OfferState.FILLED,
                signal=signal, action="record_fill",
                reason="mempool_take_confirmed",
            )
        if signal == OfferSignal.FILL_VERIFIED:
            return OfferTransition(
                old_state=state, new_state=OfferState.FILLED,
                signal=signal, action="record_verified_fill",
                reason="on_chain_verification_passed",
            )
        if signal == OfferSignal.TIME_EXPIRED:
            return OfferTransition(
                old_state=state, new_state=OfferState.EXPIRED,
                signal=signal, action="cleanup_expired",
                reason="expired_after_mempool",
            )
        if signal == OfferSignal.CANCEL_SENT:
            return OfferTransition(
                old_state=state, new_state=OfferState.CANCEL_REQUESTED,
                signal=signal, action="await_cancel_confirm",
                reason="cancel_despite_mempool",
            )

    # ---- FILLED → verification sub-signals ----
    # Note: FILLED is terminal for most signals, but we allow
    # FILL_REJECTED to transition to PHANTOM_REJECTED.
    # This is handled specially since FILLED is in _TERMINAL_STATES.
    # Callers should use apply_fill_verification() for this path.

    # Default: no valid transition
    return OfferTransition(
        old_state=state, new_state=state, signal=signal,
        action="noop", reason="signal_ignored_for_state",
    )


def apply_fill_verification(state: OfferState, signal: OfferSignal) -> OfferTransition:
    """Handle post-fill verification signals.

    Separated from apply_signal() because FILLED is normally terminal.
    Only FILL_VERIFIED (stays filled) and FILL_REJECTED (phantom) are valid.
    """
    if state == OfferState.FILLED:
        if signal == OfferSignal.FILL_VERIFIED:
            return OfferTransition(
                old_state=state, new_state=OfferState.FILLED,
                signal=signal, action="confirm_fill",
                reason="verification_passed",
            )
        if signal == OfferSignal.FILL_REJECTED:
            return OfferTransition(
                old_state=state, new_state=OfferState.PHANTOM_REJECTED,
                signal=signal, action="revert_fill_record",
                reason="self_spend_or_phantom_detected",
            )

    return OfferTransition(
        old_state=state, new_state=state, signal=signal,
        action="noop", reason="not_a_fill_verification_context",
    )


def coarse_status(lifecycle_state: str) -> str:
    """Map an extended lifecycle state to the legacy 4-value status.

    Used for backward-compatible DB queries and GUI display.
    """
    _map = {
        "open": "open",
        "refresh_due": "open",
        "cancel_requested": "open",      # still live until confirmed
        "cancelled": "cancelled",
        "mempool_observed": "open",       # still live until confirmed
        "filled": "filled",
        "expired": "expired",
        "phantom_rejected": "cancelled",  # treat as cancelled for legacy
    }
    return _map.get(lifecycle_state, "open")


def is_terminal(state: str) -> bool:
    """Check if a lifecycle state is terminal (no further transitions)."""
    return state in {"cancelled", "filled", "expired", "phantom_rejected"}

