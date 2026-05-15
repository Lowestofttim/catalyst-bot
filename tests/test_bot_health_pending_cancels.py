"""Tests for bot_health.check_pending_cancels() — the runtime verifier
that detects zombie offers (DB cancelled, Dexie still active) and repairs
by re-issuing cancels via the single-offer + priority-fee path.
"""

import sys
import types
import unittest
from unittest.mock import patch, MagicMock


# Stub heavy modules before bot_health import
def _ensure_stubs():
    if "dotenv" not in sys.modules:
        d = types.ModuleType("dotenv")
        d.load_dotenv = lambda *a, **kw: None
        d.set_key = lambda *a, **kw: None
        sys.modules["dotenv"] = d
    if "requests" not in sys.modules:
        r = types.ModuleType("requests")

        class _Resp:
            status_code = 200

            def json(self):
                return {}

            def raise_for_status(self):
                pass

        class _Session:
            headers = {}

            def get(self, *a, **kw):
                return _Resp()

            def mount(self, *a, **kw):
                pass

        r.get = lambda *a, **kw: _Resp()
        r.Session = _Session
        r.exceptions = types.SimpleNamespace(
            Timeout=Exception, ConnectionError=Exception
        )
        a = types.ModuleType("requests.adapters")
        a.HTTPAdapter = object
        r.adapters = a
        sys.modules["requests"] = r
        sys.modules["requests.adapters"] = a
    if "urllib3" not in sys.modules:
        u = types.ModuleType("urllib3")
        u.Retry = object
        u.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
        u.disable_warnings = lambda *a, **kw: None
        sys.modules["urllib3"] = u


_ensure_stubs()

import bot_health  # noqa: E402


def _offer(
    trade_id,
    dexie_id="abc123",
    lifecycle_state="cancel_sent",
    cancel_last_attempt_at=None,
):
    """Build a fake DB offer dict matching get_open_offers() shape."""
    return {
        "trade_id": trade_id,
        "dexie_id": dexie_id,
        "lifecycle_state": lifecycle_state,
        "status": "open",
        "side": "sell",
        "tier": "inner",
        "cancel_last_attempt_at": cancel_last_attempt_at,
    }


class _ModuleStubMixin:
    """Save and restore sys.modules entries between tests so this file's
    aggressive stubbing of database/wallet/wallet_sage doesn't pollute
    later test files (test_coin_manager_topup_fail_closed and friends
    that do real imports of those modules)."""

    _STUBBED_NAMES = ("database", "wallet", "wallet_sage")

    def setUp(self):
        self._saved_modules = {}
        for name in self._STUBBED_NAMES:
            if name in sys.modules:
                self._saved_modules[name] = sys.modules[name]
        # Reset throttle cache between tests
        bot_health._last_run_lock_ts = 0.0
        bot_health._last_report = None

    def tearDown(self):
        # Remove stubs and restore originals so other test files import
        # the real modules.
        for name in self._STUBBED_NAMES:
            sys.modules.pop(name, None)
            if name in self._saved_modules:
                sys.modules[name] = self._saved_modules[name]


class CheckPendingCancelsTests(_ModuleStubMixin, unittest.TestCase):
    def _patch_db(self, pending_offers, **kwargs):
        """Patch database imports used inside check_pending_cancels."""
        fake_db = types.ModuleType("database")
        fake_db.get_open_offers = lambda **kw: list(pending_offers)
        fake_db.get_connection = lambda: MagicMock()
        fake_db.update_offer_status = MagicMock(return_value=True)
        fake_db.transition_offer = MagicMock(return_value=True)
        fake_db.mark_cancel_attempted = MagicMock(return_value=True)
        for k, v in kwargs.items():
            setattr(fake_db, k, v)
        sys.modules["database"] = fake_db
        return fake_db

    # ─── Empty case ─────────────────────────────────────────────────

    def test_no_pending_returns_pass(self):
        self._patch_db([])
        with patch("bot_health._dexie_get_offer", return_value=None):
            check = bot_health.check_pending_cancels(auto_repair=True)
        self.assertEqual(check.status, "pass")
        self.assertEqual(check.anomaly_count, 0)
        self.assertEqual(check.repaired_count, 0)

    # ─── Repair B: confirmed cancel ─────────────────────────────────

    def test_dexie_cancelled_marks_db_cancelled(self):
        offers = [_offer("tid1")]
        fake_db = self._patch_db(offers)
        # Dexie says status=3 (CANCELLED)
        with patch(
            "bot_health._dexie_get_offer",
            return_value={"status": bot_health.DEXIE_STATUS_CANCELLED},
        ):
            check = bot_health.check_pending_cancels(auto_repair=True)
        self.assertEqual(check.repaired_count, 1)
        fake_db.update_offer_status.assert_called_once_with("tid1", "cancelled")
        self.assertIn("confirmed_cancelled", check.repair_log[0])

    def test_dexie_expired_marks_db_cancelled(self):
        offers = [_offer("tid1")]
        fake_db = self._patch_db(offers)
        with patch(
            "bot_health._dexie_get_offer",
            return_value={"status": bot_health.DEXIE_STATUS_EXPIRED},
        ):
            check = bot_health.check_pending_cancels(auto_repair=True)
        self.assertEqual(check.repaired_count, 1)
        fake_db.update_offer_status.assert_called_once_with("tid1", "cancelled")

    # ─── Repair A: zombie re-cancel with backoff ────────────────────

    def test_dexie_active_within_grace_does_not_retry(self):
        # Recent cancel attempt (just now) — should NOT retry yet
        from datetime import datetime, timezone

        recent = datetime.now(timezone.utc).isoformat()
        offers = [_offer("tid1", cancel_last_attempt_at=recent)]
        self._patch_db(offers)

        cancel_offer_mock = MagicMock()
        fake_wallet = types.ModuleType("wallet")
        fake_wallet.cancel_offer = cancel_offer_mock
        sys.modules["wallet"] = fake_wallet

        with patch(
            "bot_health._dexie_get_offer",
            return_value={"status": bot_health.DEXIE_STATUS_ACTIVE},
        ):
            check = bot_health.check_pending_cancels(auto_repair=True)

        cancel_offer_mock.assert_not_called()
        self.assertEqual(check.repaired_count, 0)
        # But anomaly is still recorded
        self.assertEqual(check.anomaly_count, 1)
        self.assertIn("still active on Dexie", check.message)

    def test_dexie_active_past_backoff_re_cancels_with_priority_fee(self):
        # Cancel attempt 10 minutes ago — past 5-min retry window
        from datetime import datetime, timezone, timedelta

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        offers = [_offer("tid1", cancel_last_attempt_at=old)]
        self._patch_db(offers)

        cancel_offer_mock = MagicMock(
            return_value={
                "success": True,
                "method": "submitted_pending_confirm",
            }
        )
        fake_wallet = types.ModuleType("wallet")
        fake_wallet.cancel_offer = cancel_offer_mock
        sys.modules["wallet"] = fake_wallet

        fake_sage = sys.modules.get("wallet_sage") or types.ModuleType("wallet_sage")
        fake_sage.get_effective_transaction_fee_mojos = lambda: 13_079_100  # 1.3e-5 XCH
        sys.modules["wallet_sage"] = fake_sage

        with patch(
            "bot_health._dexie_get_offer",
            return_value={"status": bot_health.DEXIE_STATUS_ACTIVE},
        ):
            check = bot_health.check_pending_cancels(auto_repair=True)

        cancel_offer_mock.assert_called_once()
        args, kwargs = cancel_offer_mock.call_args
        # Verify priority fee was passed (not zero)
        self.assertEqual(args[0], "tid1")
        self.assertEqual(kwargs.get("fee_mojos"), 13_079_100)
        self.assertGreater(
            kwargs.get("fee_mojos"), 0, "Re-cancel must use a priority fee, not zero"
        )
        self.assertEqual(check.repaired_count, 1)
        self.assertIn("re_cancelled", check.repair_log[0])

    # ─── Repair C: suspected fill — flag only, no auto-process ──────

    def test_dexie_completed_flags_suspected_fill(self):
        offers = [_offer("tid1")]
        fake_db = self._patch_db(offers)
        with patch(
            "bot_health._dexie_get_offer",
            return_value={"status": bot_health.DEXIE_STATUS_COMPLETED},
        ):
            check = bot_health.check_pending_cancels(auto_repair=True)
        # Suspected fills are NEVER auto-repaired (would risk corrupting position)
        self.assertEqual(check.repaired_count, 0)
        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(check.status, "warn")
        # update_offer_status NOT called (would mark cancelled, hiding the fill)
        fake_db.update_offer_status.assert_not_called()

    # ─── Unreachable Dexie ──────────────────────────────────────────

    def test_dexie_unreachable_does_not_act(self):
        offers = [_offer("tid1")]
        fake_db = self._patch_db(offers)
        with patch("bot_health._dexie_get_offer", return_value=None):
            check = bot_health.check_pending_cancels(auto_repair=True)
        self.assertEqual(check.repaired_count, 0)
        fake_db.update_offer_status.assert_not_called()
        self.assertIn("unreachable", check.message)

    # ─── auto_repair=False is read-only ─────────────────────────────

    def test_auto_repair_false_does_not_mutate(self):
        offers = [_offer("tid1")]
        fake_db = self._patch_db(offers)
        with patch(
            "bot_health._dexie_get_offer",
            return_value={"status": bot_health.DEXIE_STATUS_CANCELLED},
        ):
            check = bot_health.check_pending_cancels(auto_repair=False)
        # Anomaly detected but no repair executed
        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(check.repaired_count, 0)
        fake_db.update_offer_status.assert_not_called()

    # ─── Mixed batch ────────────────────────────────────────────────

    def test_mixed_batch_handles_each_correctly(self):
        from datetime import datetime, timezone, timedelta

        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        offers = [
            _offer("ok_cancelled"),  # → confirmed cancelled
            _offer("zombie", cancel_last_attempt_at=old),  # → re-cancel
            _offer("filled"),  # → suspected fill
            _offer("dunno"),  # → unreachable
        ]
        fake_db = self._patch_db(offers)

        cancel_offer_mock = MagicMock(return_value={"success": True, "method": "x"})
        sys.modules["wallet"] = types.ModuleType("wallet")
        sys.modules["wallet"].cancel_offer = cancel_offer_mock
        sys.modules["wallet_sage"] = types.ModuleType("wallet_sage")
        sys.modules["wallet_sage"].get_effective_transaction_fee_mojos = lambda: 100

        # Custom dexie response per dexie_id — but here all use the same
        # default dexie_id, so route by trade_id via patching the lookup.
        responses_by_tid = {
            "ok_cancelled": {"status": bot_health.DEXIE_STATUS_CANCELLED},
            "zombie": {"status": bot_health.DEXIE_STATUS_ACTIVE},
            "filled": {"status": bot_health.DEXIE_STATUS_COMPLETED},
            "dunno": None,
        }

        # _dexie_get_offer takes dexie_id, but we want to route by trade_id —
        # so set distinct dexie_ids in the offers and key the mock on those.
        for o in offers:
            o["dexie_id"] = f"dex-{o['trade_id']}"

        def fake_dexie(dexie_id, timeout=10.0):
            tid = dexie_id.replace("dex-", "")
            return responses_by_tid.get(tid)

        with patch("bot_health._dexie_get_offer", side_effect=fake_dexie):
            check = bot_health.check_pending_cancels(auto_repair=True)

        self.assertEqual(check.anomaly_count, 3)  # cancelled + zombie + fill
        self.assertEqual(check.repaired_count, 2)  # mark-cancelled + re-cancel
        cancel_offer_mock.assert_called_once()
        # Confirm DB updated for the cancelled one
        fake_db.update_offer_status.assert_called_once_with("ok_cancelled", "cancelled")


class RunRuntimeChecksTests(_ModuleStubMixin, unittest.TestCase):
    pass  # uses inherited setUp/tearDown

    def _stub_db(self):
        """Stub the full database surface used by every health check."""
        fake_db = types.ModuleType("database")
        fake_db.get_open_offers = lambda **kw: []
        # check_orphan_locks + check_stale_dexie_posts call get_connection()
        # and execute SELECT statements; a default MagicMock returns a chain
        # that fetchall()s to []
        empty_cursor = MagicMock()
        empty_cursor.fetchall.return_value = []
        empty_conn = MagicMock()
        empty_conn.execute.return_value = empty_cursor
        fake_db.get_connection = lambda: empty_conn
        fake_db.update_offer_status = MagicMock()
        fake_db.transition_offer = MagicMock()
        fake_db.mark_cancel_attempted = MagicMock()
        fake_db.free_coin = MagicMock(return_value=True)
        # check_topup_budget_drift + check_funds_advisory read/write settings.
        fake_db.get_setting = MagicMock(return_value=None)
        fake_db.set_setting = MagicMock(return_value=True)
        sys.modules["database"] = fake_db
        return fake_db

    def test_run_runtime_checks_throttles(self):
        """Calling within 60s returns the cached report."""
        self._stub_db()
        r1 = bot_health.run_runtime_checks(auto_repair=False)
        r2 = bot_health.run_runtime_checks(auto_repair=False)
        self.assertIs(r1, r2)

    def test_run_runtime_checks_force_bypasses_cache(self):
        self._stub_db()
        r1 = bot_health.run_runtime_checks(auto_repair=False)
        r2 = bot_health.run_runtime_checks(auto_repair=False, force=True)
        self.assertIsNot(r1, r2)


if __name__ == "__main__":
    unittest.main()
