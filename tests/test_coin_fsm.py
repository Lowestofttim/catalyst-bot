"""Tests for the coin FSM validator (non-blocking observer)."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from coin_fsm import CoinState, validate_transition, is_terminal


def _s(status, designation):
    return CoinState(status=status, designation=designation)


class TestBasicTransitions:
    def test_new_coin_classified_to_tier_spare(self):
        ok, _ = validate_transition(_s("free", "unknown"), _s("free", "tier_spare"))
        assert ok is True

    def test_tier_spare_locks_into_tier_active(self):
        ok, _ = validate_transition(
            _s("free", "tier_spare"), _s("locked", "tier_active")
        )
        assert ok is True

    def test_tier_active_cancels_back_to_free(self):
        ok, _ = validate_transition(
            _s("locked", "tier_active"), _s("free", "tier_spare")
        )
        assert ok is True

    def test_tier_active_fills_to_spent(self):
        ok, _ = validate_transition(
            _s("locked", "tier_active"), _s("spent", "tier_active")
        )
        assert ok is True


class TestSpentIsTerminal:
    def test_spent_cannot_become_free(self):
        ok, reason = validate_transition(
            _s("spent", "tier_active"), _s("free", "tier_spare")
        )
        assert ok is False
        assert "terminal" in reason.lower()

    def test_spent_cannot_become_locked(self):
        ok, _ = validate_transition(_s("spent", "dust"), _s("locked", "tier_active"))
        assert ok is False


class TestGoneReanimation:
    """Rare: a coin that disappeared can reappear (e.g. unconfirmed spend
    reversed). This should be allowed."""

    def test_gone_can_return_to_free(self):
        ok, _ = validate_transition(_s("gone", "tier_spare"), _s("free", "tier_spare"))
        assert ok is True


class TestIdentity:
    """The identity transition is always allowed (useful for idempotent writes)."""

    def test_same_state_ok(self):
        ok, _ = validate_transition(_s("free", "tier_spare"), _s("free", "tier_spare"))
        assert ok is True


class TestBadValues:
    def test_unknown_status_rejected(self):
        ok, reason = validate_transition(
            _s("nonsense", "tier_spare"), _s("free", "tier_spare")
        )
        assert ok is False
        assert "unknown" in reason.lower()

    def test_unknown_designation_rejected(self):
        ok, reason = validate_transition(_s("free", "bogus"), _s("free", "tier_spare"))
        assert ok is False


class TestSniperLifecycle:
    def test_sniper_free_to_locked(self):
        ok, _ = validate_transition(_s("free", "sniper"), _s("locked", "sniper"))
        assert ok is True

    def test_sniper_locked_to_free_on_cancel(self):
        ok, _ = validate_transition(_s("locked", "sniper"), _s("free", "sniper"))
        assert ok is True

    def test_sniper_locked_to_spent_on_fill(self):
        ok, _ = validate_transition(_s("locked", "sniper"), _s("spent", "sniper"))
        assert ok is True


class TestFeePoolLifecycle:
    def test_fee_coin_locks_for_tx_fee(self):
        ok, _ = validate_transition(_s("free", "fees"), _s("locked", "fees"))
        assert ok is True

    def test_fee_coin_returns_on_cancel(self):
        ok, _ = validate_transition(_s("locked", "fees"), _s("free", "fees"))
        assert ok is True


class TestReserveLifecycle:
    def test_reserve_consumed_on_split(self):
        ok, _ = validate_transition(_s("free", "reserve"), _s("spent", "reserve"))
        assert ok is True

    def test_reserve_can_downgrade_to_tier_spare(self):
        """After a large split, the change coin might fit a tier."""
        ok, _ = validate_transition(_s("free", "reserve"), _s("free", "tier_spare"))
        assert ok is True


class TestIsTerminal:
    def test_spent_is_terminal(self):
        assert is_terminal(_s("spent", "tier_active")) is True

    def test_gone_is_not_terminal(self):
        """Gone can be reanimated, so it's not terminal."""
        assert is_terminal(_s("gone", "tier_spare")) is False

    def test_free_is_not_terminal(self):
        assert is_terminal(_s("free", "tier_spare")) is False


class TestDisallowedTransitions:
    """The FSM catches the bug shapes we've seen in production."""

    def test_zombie_lock_shape_rejected(self):
        """A coin shouldn't jump from (free, tier_spare) directly to
        (locked, reserve) — reserve coins aren't locked in offers."""
        ok, _ = validate_transition(_s("free", "tier_spare"), _s("locked", "reserve"))
        assert ok is False

    def test_reserve_can_lock_into_tier_active(self):
        """The FSM allows (free, reserve) -> (locked, tier_active) for the
        occasional case where a reserve coin is used to back an offer.
        This documents that intent — adjust if policy tightens."""
        ok, _ = validate_transition(_s("free", "reserve"), _s("locked", "tier_active"))
        # Allowed by current policy.
        assert ok is True
