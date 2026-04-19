"""Tests for offer_lifecycle.py — canonical offer state machine."""

import unittest

from offer_lifecycle import (
    OfferState, OfferSignal, apply_signal, apply_fill_verification,
    coarse_status, is_terminal,
)


class TestOfferLifecycle(unittest.TestCase):

    # ---- OPEN state transitions ----

    def test_open_expiry_near(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.EXPIRY_NEAR)
        self.assertEqual(t.new_state, OfferState.REFRESH_DUE)
        self.assertEqual(t.action, "schedule_requote")

    def test_open_cancel_sent(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.CANCEL_SENT)
        self.assertEqual(t.new_state, OfferState.CANCEL_REQUESTED)

    def test_open_fill_detected(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)

    def test_open_time_expired(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.TIME_EXPIRED)
        self.assertEqual(t.new_state, OfferState.EXPIRED)

    def test_open_mempool_seen(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.MEMPOOL_SEEN)
        self.assertEqual(t.new_state, OfferState.MEMPOOL_OBSERVED)

    # ---- REFRESH_DUE transitions ----

    def test_refresh_due_posted(self):
        t = apply_signal(OfferState.REFRESH_DUE, OfferSignal.REFRESH_POSTED)
        self.assertEqual(t.new_state, OfferState.CANCELLED)
        self.assertEqual(t.action, "track_replacement")

    def test_refresh_due_fill(self):
        t = apply_signal(OfferState.REFRESH_DUE, OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)

    def test_refresh_due_expired(self):
        t = apply_signal(OfferState.REFRESH_DUE, OfferSignal.TIME_EXPIRED)
        self.assertEqual(t.new_state, OfferState.EXPIRED)

    # ---- CANCEL_REQUESTED transitions ----

    def test_cancel_confirmed(self):
        t = apply_signal(OfferState.CANCEL_REQUESTED, OfferSignal.CANCEL_CONFIRMED)
        self.assertEqual(t.new_state, OfferState.CANCELLED)

    def test_cancel_failed_reverts(self):
        t = apply_signal(OfferState.CANCEL_REQUESTED, OfferSignal.CANCEL_FAILED)
        self.assertEqual(t.new_state, OfferState.OPEN)

    def test_cancel_fill_race(self):
        t = apply_signal(OfferState.CANCEL_REQUESTED, OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)

    # ---- MEMPOOL_OBSERVED transitions ----

    def test_mempool_fill_detected(self):
        t = apply_signal(OfferState.MEMPOOL_OBSERVED, OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.FILLED)

    def test_mempool_fill_verified(self):
        t = apply_signal(OfferState.MEMPOOL_OBSERVED, OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.new_state, OfferState.FILLED)

    # ---- Terminal states reject all signals ----

    def test_filled_rejects_signals(self):
        t = apply_signal(OfferState.FILLED, OfferSignal.CANCEL_SENT)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.action, "noop")

    def test_cancelled_rejects_signals(self):
        t = apply_signal(OfferState.CANCELLED, OfferSignal.FILL_DETECTED)
        self.assertEqual(t.new_state, OfferState.CANCELLED)
        self.assertEqual(t.action, "noop")

    def test_expired_rejects_signals(self):
        t = apply_signal(OfferState.EXPIRED, OfferSignal.MEMPOOL_SEEN)
        self.assertEqual(t.new_state, OfferState.EXPIRED)
        self.assertEqual(t.action, "noop")

    # ---- Noop for invalid signal ----

    def test_invalid_signal_noop(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.CANCEL_CONFIRMED)
        self.assertEqual(t.new_state, OfferState.OPEN)
        self.assertEqual(t.action, "noop")

    # ---- Fill verification ----

    def test_fill_verified(self):
        t = apply_fill_verification(OfferState.FILLED, OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.new_state, OfferState.FILLED)
        self.assertEqual(t.action, "confirm_fill")

    def test_fill_rejected(self):
        t = apply_fill_verification(OfferState.FILLED, OfferSignal.FILL_REJECTED)
        self.assertEqual(t.new_state, OfferState.PHANTOM_REJECTED)
        self.assertEqual(t.action, "revert_fill_record")

    def test_fill_verification_wrong_state(self):
        t = apply_fill_verification(OfferState.OPEN, OfferSignal.FILL_VERIFIED)
        self.assertEqual(t.action, "noop")

    # ---- coarse_status mapping ----

    def test_coarse_open_states(self):
        self.assertEqual(coarse_status("open"), "open")
        self.assertEqual(coarse_status("refresh_due"), "open")
        self.assertEqual(coarse_status("cancel_requested"), "open")
        self.assertEqual(coarse_status("mempool_observed"), "open")

    def test_coarse_terminal_states(self):
        self.assertEqual(coarse_status("filled"), "filled")
        self.assertEqual(coarse_status("cancelled"), "cancelled")
        self.assertEqual(coarse_status("expired"), "expired")
        self.assertEqual(coarse_status("phantom_rejected"), "cancelled")

    # ---- is_terminal ----

    def test_is_terminal(self):
        self.assertTrue(is_terminal("filled"))
        self.assertTrue(is_terminal("cancelled"))
        self.assertTrue(is_terminal("expired"))
        self.assertTrue(is_terminal("phantom_rejected"))
        self.assertFalse(is_terminal("open"))
        self.assertFalse(is_terminal("refresh_due"))

    # ---- Transition dataclass ----

    def test_transition_is_frozen(self):
        t = apply_signal(OfferState.OPEN, OfferSignal.FILL_DETECTED)
        with self.assertRaises(AttributeError):
            t.new_state = OfferState.OPEN


if __name__ == "__main__":
    unittest.main()
