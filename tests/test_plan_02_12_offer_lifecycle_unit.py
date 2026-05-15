"""Slice 02-12 — offer_lifecycle.py unit tests.

Pure state-machine: no mocking needed. Tests every legal transition in
apply_signal, all terminal-state noop paths, apply_fill_verification,
coarse_status mappings, and is_terminal.
"""

import unittest

try:
    from offer_lifecycle import (
        OfferState,
        OfferSignal,
        OfferTransition,
        apply_signal,
        apply_fill_verification,
        coarse_status,
        is_terminal,
        _TERMINAL_STATES,
    )

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestOfferStateEnum(unittest.TestCase):
    def test_all_states_are_strings(self):
        for state in OfferState:
            self.assertIsInstance(str(state), str)

    def test_open_value(self):
        self.assertEqual(OfferState.OPEN, "open")

    def test_filled_value(self):
        self.assertEqual(OfferState.FILLED, "filled")


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestTerminalStates(unittest.TestCase):
    def test_cancelled_is_terminal(self):
        self.assertIn(OfferState.CANCELLED, _TERMINAL_STATES)

    def test_filled_is_terminal(self):
        self.assertIn(OfferState.FILLED, _TERMINAL_STATES)

    def test_expired_is_terminal(self):
        self.assertIn(OfferState.EXPIRED, _TERMINAL_STATES)

    def test_phantom_rejected_is_terminal(self):
        self.assertIn(OfferState.PHANTOM_REJECTED, _TERMINAL_STATES)

    def test_open_is_not_terminal(self):
        self.assertNotIn(OfferState.OPEN, _TERMINAL_STATES)

    def test_terminal_state_rejects_all_signals(self):
        for state in _TERMINAL_STATES:
            for signal in OfferSignal:
                t = apply_signal(state, signal)
                self.assertEqual(t.new_state, state)
                self.assertEqual(t.action, "noop")
                self.assertEqual(t.reason, "offer_in_terminal_state")


# ===========================================================================
# OPEN state transitions
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestOpenStateTransitions(unittest.TestCase):
    def _apply(self, signal):
        return apply_signal(OfferState.OPEN, signal)

    def test_expiry_near_transitions_to_refresh_due(self):
        t = self._apply(OfferSignal.EXPIRY_NEAR)
        self.assertEqual(t.new_state, OfferState.REFRESH_DUE)
        self.assertEqual(t.action, "schedule_requote")

    def test_cancel_sent_transitions_to_cancel_requested(self):
        t = self._apply(OfferSignal.CANCEL_SENT)
        self.assertEqual(t.new_state, OfferState.CANCEL_REQUESTED)
        self.assertEqual(t.action, "await_cancel_confirm")

    def test_fill_detected_transitions_to_filled(self):
        t = self._apply(OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.action, "record_fill")

    def test_time_expired_transitions_to_expired(self):
        t = self._apply(OfferSignal.TIME_EXPIRED)
        self.assertEqual(t.new_state, OfferState.EXPIRED)
        self.assertEqual(t.action, "cleanup_expired")

    def test_mempool_seen_transitions_to_mempool_observed(self):
        t = self._apply(OfferSignal.MEMPOOL_SEEN)
        self.assertEqual(t.new_state, OfferState.MEMPOOL_OBSERVED)
        self.assertEqual(t.action, "mark_mempool_observed")

    def test_unknown_signal_returns_noop(self):
        t = self._apply(OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.new_state, OfferState.OPEN)
        self.assertEqual(t.action, "noop")

    def test_transition_carries_old_state(self):
        t = self._apply(OfferSignal.FILL_DETECTED)
        self.assertEqual(t.old_state, OfferState.OPEN)

    def test_transition_carries_signal(self):
        t = self._apply(OfferSignal.CANCEL_SENT)
        self.assertEqual(t.signal, OfferSignal.CANCEL_SENT)


# ===========================================================================
# REFRESH_DUE state transitions
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestRefreshDueStateTransitions(unittest.TestCase):
    def _apply(self, signal):
        return apply_signal(OfferState.REFRESH_DUE, signal)

    def test_refresh_posted_transitions_to_cancelled(self):
        t = self._apply(OfferSignal.REFRESH_POSTED)
        self.assertEqual(t.new_state, OfferState.CANCELLED)
        self.assertEqual(t.action, "track_replacement")

    def test_cancel_sent_during_refresh(self):
        t = self._apply(OfferSignal.CANCEL_SENT)
        self.assertEqual(t.new_state, OfferState.CANCEL_REQUESTED)

    def test_fill_detected_during_refresh(self):
        t = self._apply(OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.reason, "filled_while_awaiting_refresh")

    def test_time_expired_before_refresh(self):
        t = self._apply(OfferSignal.TIME_EXPIRED)
        self.assertEqual(t.new_state, OfferState.EXPIRED)

    def test_mempool_seen_while_refresh_due(self):
        t = self._apply(OfferSignal.MEMPOOL_SEEN)
        self.assertEqual(t.new_state, OfferState.MEMPOOL_OBSERVED)

    def test_unknown_signal_noop(self):
        t = self._apply(OfferSignal.CANCEL_CONFIRMED)
        self.assertEqual(t.new_state, OfferState.REFRESH_DUE)
        self.assertEqual(t.action, "noop")


# ===========================================================================
# CANCEL_REQUESTED state transitions
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestCancelRequestedStateTransitions(unittest.TestCase):
    def _apply(self, signal):
        return apply_signal(OfferState.CANCEL_REQUESTED, signal)

    def test_cancel_confirmed_transitions_to_cancelled(self):
        t = self._apply(OfferSignal.CANCEL_CONFIRMED)
        self.assertEqual(t.new_state, OfferState.CANCELLED)
        self.assertEqual(t.action, "finalize_cancel")

    def test_cancel_failed_reverts_to_open(self):
        t = self._apply(OfferSignal.CANCEL_FAILED)
        self.assertEqual(t.new_state, OfferState.OPEN)
        self.assertEqual(t.action, "retry_or_revert")

    def test_fill_during_cancel_is_fill(self):
        t = self._apply(OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.reason, "filled_during_cancel")

    def test_time_expired_during_cancel(self):
        t = self._apply(OfferSignal.TIME_EXPIRED)
        self.assertEqual(t.new_state, OfferState.EXPIRED)

    def test_mempool_seen_during_cancel_stays_in_state(self):
        t = self._apply(OfferSignal.MEMPOOL_SEEN)
        self.assertEqual(t.new_state, OfferState.CANCEL_REQUESTED)
        self.assertEqual(t.action, "note_mempool_during_cancel")

    def test_unknown_signal_noop(self):
        t = self._apply(OfferSignal.EXPIRY_NEAR)
        self.assertEqual(t.new_state, OfferState.CANCEL_REQUESTED)
        self.assertEqual(t.action, "noop")


# ===========================================================================
# MEMPOOL_OBSERVED state transitions
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestMempoolObservedStateTransitions(unittest.TestCase):
    def _apply(self, signal):
        return apply_signal(OfferState.MEMPOOL_OBSERVED, signal)

    def test_fill_detected_confirms_fill(self):
        t = self._apply(OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.reason, "mempool_take_confirmed")

    def test_fill_verified_transitions_to_filled(self):
        t = self._apply(OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.action, "record_verified_fill")

    def test_time_expired_after_mempool(self):
        t = self._apply(OfferSignal.TIME_EXPIRED)
        self.assertEqual(t.new_state, OfferState.EXPIRED)

    def test_cancel_sent_despite_mempool(self):
        t = self._apply(OfferSignal.CANCEL_SENT)
        self.assertEqual(t.new_state, OfferState.CANCEL_REQUESTED)
        self.assertEqual(t.reason, "cancel_despite_mempool")

    def test_unknown_signal_noop(self):
        t = self._apply(OfferSignal.EXPIRY_NEAR)
        self.assertEqual(t.new_state, OfferState.MEMPOOL_OBSERVED)
        self.assertEqual(t.action, "noop")


# ===========================================================================
# apply_fill_verification
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestApplyFillVerification(unittest.TestCase):
    def test_fill_verified_stays_filled(self):
        t = apply_fill_verification(OfferState.FILLED, OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.action, "confirm_fill")

    def test_fill_rejected_transitions_to_phantom(self):
        t = apply_fill_verification(OfferState.FILLED, OfferSignal.FILL_REJECTED)
        self.assertEqual(t.new_state, OfferState.PHANTOM_REJECTED)
        self.assertEqual(t.action, "revert_fill_record")

    def test_wrong_state_returns_noop(self):
        t = apply_fill_verification(OfferState.OPEN, OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.new_state, OfferState.OPEN)
        self.assertEqual(t.action, "noop")

    def test_wrong_signal_returns_noop(self):
        t = apply_fill_verification(OfferState.FILLED, OfferSignal.CANCEL_SENT)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.action, "noop")


# ===========================================================================
# coarse_status
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestCoarseStatus(unittest.TestCase):
    def test_open_maps_to_open(self):
        self.assertEqual(coarse_status("open"), "open")

    def test_refresh_due_maps_to_open(self):
        self.assertEqual(coarse_status("refresh_due"), "open")

    def test_cancel_requested_maps_to_open(self):
        self.assertEqual(coarse_status("cancel_requested"), "open")

    def test_mempool_observed_maps_to_open(self):
        self.assertEqual(coarse_status("mempool_observed"), "open")

    def test_cancelled_maps_to_cancelled(self):
        self.assertEqual(coarse_status("cancelled"), "cancelled")

    def test_filled_maps_to_filled(self):
        self.assertEqual(coarse_status("filled"), "filled")

    def test_expired_maps_to_expired(self):
        self.assertEqual(coarse_status("expired"), "expired")

    def test_phantom_rejected_maps_to_cancelled(self):
        self.assertEqual(coarse_status("phantom_rejected"), "cancelled")

    def test_unknown_state_maps_to_open(self):
        self.assertEqual(coarse_status("unexpected_state"), "open")


# ===========================================================================
# is_terminal
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestIsTerminal(unittest.TestCase):
    def test_cancelled_is_terminal(self):
        self.assertTrue(is_terminal("cancelled"))

    def test_filled_is_terminal(self):
        self.assertTrue(is_terminal("filled"))

    def test_expired_is_terminal(self):
        self.assertTrue(is_terminal("expired"))

    def test_phantom_rejected_is_terminal(self):
        self.assertTrue(is_terminal("phantom_rejected"))

    def test_open_not_terminal(self):
        self.assertFalse(is_terminal("open"))

    def test_cancel_requested_not_terminal(self):
        self.assertFalse(is_terminal("cancel_requested"))

    def test_refresh_due_not_terminal(self):
        self.assertFalse(is_terminal("refresh_due"))


# ===========================================================================
# OfferTransition dataclass is frozen (immutable)
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"offer_lifecycle unavailable: {_SKIP}")
class TestOfferTransitionImmutable(unittest.TestCase):
    def test_transition_is_frozen(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.FILL_DETECTED)
        with self.assertRaises((AttributeError, TypeError)):
            t.new_state = OfferState.CANCELLED  # type: ignore


if __name__ == "__main__":
    unittest.main()
