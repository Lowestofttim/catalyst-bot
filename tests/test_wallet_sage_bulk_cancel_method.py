"""Regression test for the bulk-cancel method tag.

The bulk path was historically tagging cancels as method='bulk_3step'.
That string was NOT in CANCEL_PENDING_METHODS, so offer_manager would
mark the offer cancelled in DB on submit-to-mempool — even though the
fee=0 cancel TX hadn't actually confirmed on-chain. Result: ghost
'zombie' offers that DB called dead but Dexie still showed as ACTIVE.

Fix: when skip_confirmation=True (fire-and-forget requote path), tag
the bulk-cancelled offers with 'submitted_pending_confirm' so the
existing CANCEL_PENDING_METHODS guard keeps DB at status='open' until
the bot_health verifier confirms via Dexie.
"""
import sys
import types
import unittest
from unittest.mock import patch


# Stubs (same shape as other wallet_sage tests)
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
            def json(self): return {}
            def raise_for_status(self): pass
        class _Session:
            headers = {}
            def get(self, *a, **kw): return _Resp()
            def mount(self, *a, **kw): pass
        r.get = lambda *a, **kw: _Resp()
        r.Session = _Session
        r.exceptions = types.SimpleNamespace(Timeout=Exception, ConnectionError=Exception)
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

import wallet_sage  # noqa: E402
from offer_manager import CANCEL_PENDING_METHODS  # noqa: E402


class BulkCancelMethodTagTests(unittest.TestCase):
    """Verify the bulk path returns a method tag the offer_manager guard
    recognizes — preventing premature DB-cancel on submit-to-mempool."""

    def test_bulk_success_tags_submitted_pending_confirm(self):
        trade_ids = ["aaaa", "bbbb", "cccc"]

        with patch.object(wallet_sage, "_cancel_offers_bulk_proper",
                          return_value=True), \
             patch.object(wallet_sage, "get_spendable_coin_count",
                          return_value=10):
            results = wallet_sage.cancel_offers_batch(
                trade_ids, secure=True, skip_confirmation=True)

        self.assertEqual(len(results), 3)
        for tid in trade_ids:
            self.assertTrue(results[tid]["success"])
            method = results[tid]["method"]
            self.assertEqual(method, "submitted_pending_confirm",
                             f"Expected 'submitted_pending_confirm' for tid={tid}, got '{method}'")
            self.assertIn(method, CANCEL_PENDING_METHODS,
                          f"Method '{method}' must be in CANCEL_PENDING_METHODS so "
                          f"offer_manager keeps DB status=open until on-chain confirm")

    def test_bulk_success_records_submission_path(self):
        """Even though method is renamed, we keep submission_path so logs
        and downstream debugging can tell bulk vs sequential apart."""
        trade_ids = ["xxxx"]

        # bulk path triggers when len >= 2; for a single tid we'd hit
        # sequential. Use 2 to force bulk.
        trade_ids = ["xxxx", "yyyy"]

        with patch.object(wallet_sage, "_cancel_offers_bulk_proper",
                          return_value=True), \
             patch.object(wallet_sage, "get_spendable_coin_count",
                          return_value=10):
            results = wallet_sage.cancel_offers_batch(
                trade_ids, secure=True, skip_confirmation=True)

        for tid in trade_ids:
            self.assertEqual(results[tid].get("submission_path"), "bulk_3step",
                             "submission_path must indicate the bulk-3step path "
                             "so debug logs can distinguish bulk vs sequential cancels")

    def test_large_bulk_cancel_is_split_into_bounded_chunks(self):
        trade_ids = [f"trade-{i}" for i in range(61)]
        bulk_calls = []

        def _record_bulk(ids, fee_mojos=0):
            bulk_calls.append(list(ids))
            return True

        with patch.dict(wallet_sage.os.environ, {
                "SAGE_BULK_CANCEL_BATCH_SIZE": "25",
                "SAGE_BULK_CANCEL_BATCH_PAUSE_SECS": "0",
             }, clear=False), \
             patch.object(wallet_sage, "_cancel_offers_bulk_proper",
                          side_effect=_record_bulk), \
             patch.object(wallet_sage, "get_spendable_coin_count",
                          return_value=10), \
             patch.object(wallet_sage.time, "sleep", return_value=None):
            results = wallet_sage.cancel_offers_batch(
                trade_ids, secure=True, skip_confirmation=True)

        self.assertEqual([len(c) for c in bulk_calls], [25, 25, 11])
        self.assertEqual(len(results), len(trade_ids))
        for tid in trade_ids:
            self.assertTrue(results[tid]["success"])
            self.assertEqual(results[tid]["method"], "submitted_pending_confirm")


if __name__ == "__main__":
    unittest.main()
