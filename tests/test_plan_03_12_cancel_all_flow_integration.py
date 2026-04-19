"""Slice 03-12 — cancel-all flow integration test.

Tests the full stop-button flow: open offers in DB → cancel_all() bulk cancel →
DB offers marked cancelled/kept pending based on wallet response.

Uses real SQLite temp DB. cancel_offers_batch (wallet RPC) is mocked.
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db
    from database import add_offer, get_open_offers, init_database
    _SKIP_DB = None
except ModuleNotFoundError as exc:
    _db = None
    _SKIP_DB = str(exc)

try:
    import offer_manager as _om_mod
    from offer_manager import OfferManager
    _SKIP_OM = None
except ModuleNotFoundError as exc:
    OfferManager = None
    _SKIP_OM = str(exc)


_P = Decimal("0.001")
_SX = Decimal("1.0")
_SC = Decimal("1000")
_ASSET = "aabbcc1122"


def _add_offer(trade_id: str, side: str = "buy"):
    add_offer(trade_id, side, _P, _SX, _SC, _ASSET, tier="inner")


def _fake_cfg(**overrides):
    defaults = dict(CAT_ASSET_ID=_ASSET)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Temp-DB base class
# ---------------------------------------------------------------------------

class _TempDB(unittest.TestCase):
    def setUp(self):
        sys.modules["database"] = _db

        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path
        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.init_database()

    def tearDown(self):
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path
        sys.modules["database"] = _db
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass

    def _open_offer_count(self) -> int:
        return len(get_open_offers(cat_asset_id=_ASSET))

    def _offer_status(self, trade_id: str) -> str:
        conn = _db.get_connection()
        row = conn.execute(
            "SELECT status FROM offers WHERE trade_id=?", (trade_id,)
        ).fetchone()
        return dict(row)["status"] if row else None


# ---------------------------------------------------------------------------
# 1. cancel_all() — confirmed cancel path
# ---------------------------------------------------------------------------

@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_OM is not None,
    f"dependencies unavailable: db={_SKIP_DB} om={_SKIP_OM}"
)
class TestCancelAllConfirmed(_TempDB):

    def _run_cancel(self, trade_ids, bulk_response):
        """Patch cancel_offers_batch and run cancel_all()."""
        om = OfferManager()
        fake_cfg = _fake_cfg()
        with patch.object(_om_mod, "cfg", fake_cfg), \
             patch.object(_om_mod, "cancel_offers_batch",
                          return_value=bulk_response), \
             patch.object(_om_mod, "get_all_offers", return_value=[]):
            return om.cancel_all(cat_asset_id=_ASSET)

    def test_no_open_offers_returns_empty_dict(self):
        result = self._run_cancel([], {})
        self.assertEqual(result, {})

    def test_single_offer_cancelled_on_success(self):
        _add_offer("tid-a")
        bulk = {"tid-a": {"success": True, "method": "bulk"}}
        self._run_cancel(["tid-a"], bulk)
        self.assertEqual(self._offer_status("tid-a"), "cancelled")

    def test_multiple_offers_all_cancelled(self):
        for i in range(3):
            _add_offer(f"tid-{i}")
        self.assertEqual(self._open_offer_count(), 3)

        bulk = {f"tid-{i}": {"success": True, "method": "bulk"} for i in range(3)}
        self._run_cancel([f"tid-{i}" for i in range(3)], bulk)
        self.assertEqual(self._open_offer_count(), 0)

    def test_failed_cancel_leaves_offer_open(self):
        _add_offer("tid-fail")
        bulk = {"tid-fail": {"success": False, "error": "rpc error"}}
        self._run_cancel(["tid-fail"], bulk)
        # Offer NOT marked cancelled — remains open
        status = self._offer_status("tid-fail")
        self.assertNotEqual(status, "cancelled")

    def test_mixed_success_failure(self):
        _add_offer("tid-ok")
        _add_offer("tid-nok")
        bulk = {
            "tid-ok":  {"success": True,  "method": "bulk"},
            "tid-nok": {"success": False, "error": "timeout"},
        }
        self._run_cancel(["tid-ok", "tid-nok"], bulk)
        self.assertEqual(self._offer_status("tid-ok"),  "cancelled")
        self.assertNotEqual(self._offer_status("tid-nok"), "cancelled")

    def test_return_dict_contains_all_trade_ids(self):
        for i in range(2):
            _add_offer(f"rt-{i}")
        bulk = {f"rt-{i}": {"success": True, "method": "bulk"} for i in range(2)}
        result = self._run_cancel([f"rt-{i}" for i in range(2)], bulk)
        for i in range(2):
            self.assertIn(f"rt-{i}", result)


# ---------------------------------------------------------------------------
# 2. cancel_all() — pending-cancel path (submitted but not confirmed)
# ---------------------------------------------------------------------------

@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_OM is not None,
    f"dependencies unavailable: db={_SKIP_DB} om={_SKIP_OM}"
)
class TestCancelAllPending(_TempDB):

    def test_pending_cancel_leaves_offer_open_in_db(self):
        _add_offer("tid-pending")
        om = OfferManager()
        fake_cfg = _fake_cfg()
        # "submitted_pending_confirm" is in CANCEL_PENDING_METHODS
        bulk = {"tid-pending": {"success": True,
                                "method": "submitted_pending_confirm"}}
        with patch.object(_om_mod, "cfg", fake_cfg), \
             patch.object(_om_mod, "cancel_offers_batch", return_value=bulk), \
             patch.object(_om_mod, "get_all_offers", return_value=[]):
            om.cancel_all(cat_asset_id=_ASSET)
        # Pending cancel — DB status unchanged (still "open")
        self.assertEqual(self._offer_status("tid-pending"), "open")


# ---------------------------------------------------------------------------
# 3. cancel_all() — side filter
# ---------------------------------------------------------------------------

@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_OM is not None,
    f"dependencies unavailable: db={_SKIP_DB} om={_SKIP_OM}"
)
class TestCancelAllSideFilter(_TempDB):

    def _cancel_side(self, side_filter, bulk_response):
        om = OfferManager()
        fake_cfg = _fake_cfg()
        with patch.object(_om_mod, "cfg", fake_cfg), \
             patch.object(_om_mod, "cancel_offers_batch",
                          return_value=bulk_response), \
             patch.object(_om_mod, "get_all_offers", return_value=[]):
            return om.cancel_all(cat_asset_id=_ASSET, side_filter=side_filter)

    def test_buy_filter_cancels_only_buy_offers(self):
        _add_offer("buy-1", "buy")
        _add_offer("sell-1", "sell")
        bulk = {"buy-1": {"success": True, "method": "bulk"}}
        self._cancel_side("buy", bulk)
        self.assertEqual(self._offer_status("buy-1"),  "cancelled")
        self.assertEqual(self._offer_status("sell-1"), "open")

    def test_sell_filter_cancels_only_sell_offers(self):
        _add_offer("buy-2", "buy")
        _add_offer("sell-2", "sell")
        bulk = {"sell-2": {"success": True, "method": "bulk"}}
        self._cancel_side("sell", bulk)
        self.assertEqual(self._offer_status("buy-2"),  "open")
        self.assertEqual(self._offer_status("sell-2"), "cancelled")

    def test_no_filter_cancels_all_sides(self):
        _add_offer("buy-3", "buy")
        _add_offer("sell-3", "sell")
        bulk = {
            "buy-3":  {"success": True, "method": "bulk"},
            "sell-3": {"success": True, "method": "bulk"},
        }
        self._cancel_side("", bulk)
        self.assertEqual(self._offer_status("buy-3"),  "cancelled")
        self.assertEqual(self._offer_status("sell-3"), "cancelled")


# ---------------------------------------------------------------------------
# 4. cancel_all() — exception during bulk cancel
# ---------------------------------------------------------------------------

@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_OM is not None,
    f"dependencies unavailable: db={_SKIP_DB} om={_SKIP_OM}"
)
class TestCancelAllExceptionHandling(_TempDB):

    def test_exception_in_bulk_cancel_does_not_crash_caller(self):
        _add_offer("tid-exc")
        om = OfferManager()
        fake_cfg = _fake_cfg()
        with patch.object(_om_mod, "cfg", fake_cfg), \
             patch.object(_om_mod, "cancel_offers_batch",
                          side_effect=RuntimeError("wallet offline")), \
             patch.object(_om_mod, "get_all_offers", return_value=[]):
            # Should not raise
            result = om.cancel_all(cat_asset_id=_ASSET)
        # Still returns a result dict (may be empty or with the tid)
        self.assertIsNotNone(result)
        # Offer not marked cancelled (cancel didn't succeed)
        status = self._offer_status("tid-exc")
        self.assertNotEqual(status, "cancelled")


if __name__ == "__main__":
    unittest.main()
