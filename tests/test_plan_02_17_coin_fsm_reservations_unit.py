"""Slice 02-17 — coin_fsm.py + coin_reservations.py unit tests.

Both modules are pure (no DB/wallet/network). Tests cover CoinState,
validate_transition (legal/illegal edges, identity, terminal, unknown values),
is_terminal, STATUSES/DESIGNATIONS vocabularies, ReservationRegistry
(reserve, release, release_by_owner, is_reserved, is_reserved_by,
filter_unreserved, gc_expired, stats, contested), and _normalise.
"""

import time
import unittest

try:
    from coin_fsm import (
        CoinState,
        STATUSES,
        DESIGNATIONS,
        validate_transition,
        is_terminal,
    )

    _SKIP_FSM = None
except ModuleNotFoundError as exc:
    _SKIP_FSM = str(exc)

try:
    from coin_reservations import ReservationRegistry, _normalise

    _SKIP_RES = None
except ModuleNotFoundError as exc:
    _SKIP_RES = str(exc)


# ===========================================================================
# coin_fsm — vocabulary sets
# ===========================================================================


@unittest.skipIf(_SKIP_FSM is not None, f"coin_fsm unavailable: {_SKIP_FSM}")
class TestCoinFsmVocabulary(unittest.TestCase):
    def test_statuses_contains_free(self):
        self.assertIn("free", STATUSES)

    def test_statuses_contains_spent(self):
        self.assertIn("spent", STATUSES)

    def test_designations_contains_tier_spare(self):
        self.assertIn("tier_spare", DESIGNATIONS)

    def test_designations_contains_reserve(self):
        self.assertIn("reserve", DESIGNATIONS)

    def test_statuses_and_designations_are_sets(self):
        self.assertIsInstance(STATUSES, (set, frozenset))
        self.assertIsInstance(DESIGNATIONS, (set, frozenset))


# ===========================================================================
# coin_fsm — CoinState dataclass
# ===========================================================================


@unittest.skipIf(_SKIP_FSM is not None, f"coin_fsm unavailable: {_SKIP_FSM}")
class TestCoinState(unittest.TestCase):
    def test_str_representation(self):
        s = str(CoinState("free", "tier_spare"))
        self.assertIn("free", s)
        self.assertIn("tier_spare", s)

    def test_frozen(self):
        cs = CoinState("free", "reserve")
        with self.assertRaises((AttributeError, TypeError)):
            cs.status = "locked"  # type: ignore

    def test_equality(self):
        self.assertEqual(CoinState("free", "dust"), CoinState("free", "dust"))
        self.assertNotEqual(CoinState("free", "dust"), CoinState("locked", "dust"))


# ===========================================================================
# coin_fsm — is_terminal
# ===========================================================================


@unittest.skipIf(_SKIP_FSM is not None, f"coin_fsm unavailable: {_SKIP_FSM}")
class TestIsTerminal(unittest.TestCase):
    def test_spent_is_terminal(self):
        self.assertTrue(is_terminal(CoinState("spent", "tier_active")))

    def test_free_is_not_terminal(self):
        self.assertFalse(is_terminal(CoinState("free", "tier_spare")))

    def test_locked_is_not_terminal(self):
        self.assertFalse(is_terminal(CoinState("locked", "tier_active")))

    def test_gone_is_not_terminal(self):
        # gone coins can reappear (reanimate)
        self.assertFalse(is_terminal(CoinState("gone", "unknown")))


# ===========================================================================
# coin_fsm — validate_transition
# ===========================================================================


@unittest.skipIf(_SKIP_FSM is not None, f"coin_fsm unavailable: {_SKIP_FSM}")
class TestValidateTransition(unittest.TestCase):
    def _ok(self, old_st, old_ds, new_st, new_ds):
        ok, _ = validate_transition(
            CoinState(old_st, old_ds), CoinState(new_st, new_ds)
        )
        return ok

    def test_identity_always_ok(self):
        for st in list(STATUSES)[:2]:
            for ds in list(DESIGNATIONS)[:2]:
                ok, _ = validate_transition(CoinState(st, ds), CoinState(st, ds))
                self.assertTrue(ok, f"identity {st}/{ds} failed")

    def test_free_unknown_to_free_tier_spare_allowed(self):
        self.assertTrue(self._ok("free", "unknown", "free", "tier_spare"))

    def test_free_tier_spare_to_locked_tier_active_allowed(self):
        self.assertTrue(self._ok("free", "tier_spare", "locked", "tier_active"))

    def test_locked_tier_active_to_free_tier_spare_allowed(self):
        # cancel → back to free
        self.assertTrue(self._ok("locked", "tier_active", "free", "tier_spare"))

    def test_locked_tier_active_to_spent_allowed(self):
        self.assertTrue(self._ok("locked", "tier_active", "spent", "tier_active"))

    def test_free_reserve_to_spent_reserve_allowed(self):
        self.assertTrue(self._ok("free", "reserve", "spent", "reserve"))

    def test_free_sniper_to_locked_sniper_allowed(self):
        self.assertTrue(self._ok("free", "sniper", "locked", "sniper"))

    def test_gone_to_free_allowed(self):
        self.assertTrue(self._ok("gone", "unknown", "free", "tier_spare"))

    # --- illegal transitions ---

    def test_spent_to_free_is_terminal(self):
        ok, reason = validate_transition(
            CoinState("spent", "tier_active"), CoinState("free", "tier_spare")
        )
        self.assertFalse(ok)
        self.assertIn("terminal", reason)

    def test_unknown_old_status_rejects(self):
        ok, reason = validate_transition(
            CoinState("bogus", "tier_spare"), CoinState("free", "tier_spare")
        )
        self.assertFalse(ok)
        self.assertIn("unknown old status", reason)

    def test_unknown_old_designation_rejects(self):
        ok, reason = validate_transition(
            CoinState("free", "nonexistent"), CoinState("free", "tier_spare")
        )
        self.assertFalse(ok)
        self.assertIn("unknown old designation", reason)

    def test_unknown_new_status_rejects(self):
        ok, reason = validate_transition(
            CoinState("free", "tier_spare"), CoinState("phantom", "tier_spare")
        )
        self.assertFalse(ok)
        self.assertIn("unknown new status", reason)

    def test_reason_empty_when_ok(self):
        _, reason = validate_transition(
            CoinState("free", "tier_spare"), CoinState("locked", "tier_active")
        )
        self.assertEqual(reason, "")

    def test_reason_non_empty_when_rejected(self):
        _, reason = validate_transition(
            CoinState("spent", "tier_active"), CoinState("locked", "tier_active")
        )
        self.assertNotEqual(reason, "")


# ===========================================================================
# coin_reservations — _normalise helper
# ===========================================================================


@unittest.skipIf(_SKIP_RES is not None, f"coin_reservations unavailable: {_SKIP_RES}")
class TestNormalise(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(_normalise(""), "")

    def test_strips_0x_prefix(self):
        self.assertEqual(_normalise("0xABCDEF"), "abcdef")

    def test_lowercases(self):
        self.assertEqual(_normalise("ABCDEF"), "abcdef")

    def test_already_normalised_unchanged(self):
        self.assertEqual(_normalise("abcdef"), "abcdef")

    def test_whitespace_stripped(self):
        self.assertEqual(_normalise("  0xabc  "), "abc")


# ===========================================================================
# coin_reservations — ReservationRegistry
# ===========================================================================


@unittest.skipIf(_SKIP_RES is not None, f"coin_reservations unavailable: {_SKIP_RES}")
class TestReservationRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = ReservationRegistry()

    def test_reserve_returns_coin_ids(self):
        result = self.reg.reserve(["0xabc", "0xdef"], owner="op1", purpose="test")
        self.assertEqual(len(result), 2)

    def test_is_reserved_after_reserve(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        self.assertTrue(self.reg.is_reserved("0xabc"))

    def test_is_not_reserved_initially(self):
        self.assertFalse(self.reg.is_reserved("0xfff"))

    def test_is_reserved_by_correct_owner(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        self.assertTrue(self.reg.is_reserved_by("0xabc", "op1"))
        self.assertFalse(self.reg.is_reserved_by("0xabc", "op2"))

    def test_release_frees_coin(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        self.reg.release(["0xabc"], owner="op1")
        self.assertFalse(self.reg.is_reserved("0xabc"))

    def test_release_wrong_owner_does_not_free(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        self.reg.release(["0xabc"], owner="op2")
        self.assertTrue(self.reg.is_reserved("0xabc"))

    def test_release_by_owner_frees_all(self):
        self.reg.reserve(["0xabc", "0xdef"], owner="op1", purpose="test")
        self.reg.release_by_owner("op1")
        self.assertFalse(self.reg.is_reserved("0xabc"))
        self.assertFalse(self.reg.is_reserved("0xdef"))

    def test_contested_coin_not_reserved_by_second_owner(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        result = self.reg.reserve(["0xabc"], owner="op2", purpose="test")
        self.assertNotIn("abc", result)  # normalised

    def test_same_owner_refreshes_ttl(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test", ttl_seconds=30)
        result = self.reg.reserve(
            ["0xabc"], owner="op1", purpose="test", ttl_seconds=60
        )
        self.assertEqual(len(result), 1)  # still reserved by op1

    def test_filter_unreserved_excludes_reserved(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        result = self.reg.filter_unreserved(["0xabc", "0xdef"])
        self.assertNotIn("abc", result)
        self.assertIn("def", result)

    def test_gc_expired_removes_stale(self):
        # TTL floor is max(1.0,…); use _gc_locked with far-future now to
        # simulate expiry without sleeping.
        self.reg.reserve(["0xabc"], owner="op1", purpose="test", ttl_seconds=1.0)
        with self.reg._lock:
            removed = self.reg._gc_locked(time.time() + 10.0)
        self.assertGreaterEqual(removed, 1)

    def test_expired_reservation_not_seen(self):
        # Use is_reserved(now=far_future) to simulate post-expiry check.
        self.reg.reserve(["0xabc"], owner="op1", purpose="test", ttl_seconds=1.0)
        future_now = time.time() + 10.0
        self.assertFalse(self.reg.is_reserved("0xabc", now=future_now))

    def test_stats_all_keys_present(self):
        s = self.reg.stats()
        self.assertIn("currently_reserved", s)
        self.assertIn("total_reserved", s)
        self.assertIn("total_contested", s)

    def test_stats_contested_increments(self):
        self.reg.reserve(["0xabc"], owner="op1", purpose="test")
        self.reg.reserve(["0xabc"], owner="op2", purpose="test")
        s = self.reg.stats()
        self.assertEqual(s["total_contested"], 1)

    def test_empty_coin_ids_returns_empty(self):
        result = self.reg.reserve([], owner="op1", purpose="test")
        self.assertEqual(result, [])

    def test_empty_owner_returns_empty(self):
        result = self.reg.reserve(["0xabc"], owner="", purpose="test")
        self.assertEqual(result, [])

    def test_release_returns_count(self):
        self.reg.reserve(["0xabc", "0xdef"], owner="op1", purpose="test")
        count = self.reg.release(["0xabc"], owner="op1")
        self.assertEqual(count, 1)

    def test_release_by_owner_returns_count(self):
        self.reg.reserve(["0xabc", "0xdef"], owner="op1", purpose="test")
        count = self.reg.release_by_owner("op1")
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
