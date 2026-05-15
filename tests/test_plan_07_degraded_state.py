"""Layer 7 — Degraded-state / disaster-recovery contract tests (mockable slices).

Slices covered:
  07-03: Dexie API 5xx intermittently — retry loop + eventual failure returns
         error dict (not exception); 429 respects Retry-After; rate-limit
         cooldown enforced via _rate_limited_until float
  07-04: TibetSwap API 5xx — PriceEngine falls back to stale cache or Dexie;
         both sources failing returns None (bot does not crash)
  07-06: DB row inconsistency — fill_tracker._get_offer_context() handles
         get_offer() returning None; exception from get_offer caught
  07-07: Disk space exhausted — record_fill/record_price/log_event return
         failure sentinels (not raise); each calls rollback to release write lock
  07-08: System clock jumps backward — get_last_price() with negative age
         does not erroneously expire valid price; uptime int() handles negative;
         DexieManager rate-limit float works correctly under clock weirdness
"""

import os
import sys
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# 07-03: Dexie API 5xx — retry + graceful failure
# ---------------------------------------------------------------------------


class TestDexie5xxRetry(unittest.TestCase):
    """DexieManager should retry 5xx responses up to DEXIE_POST_RETRIES times
    and return an error dict (never raise) when retries are exhausted."""

    def _make_manager(self):
        from dexie_manager import DexieManager

        return DexieManager()

    def _make_5xx_response(self, status=500):
        resp = MagicMock()
        resp.status_code = status
        resp.text = "Internal Server Error"
        return resp

    def test_5xx_response_does_not_raise(self):
        mgr = self._make_manager()
        mock_resp = self._make_5xx_response(500)
        with (
            patch("dexie_manager.requests.post", return_value=mock_resp),
            patch("dexie_manager.time.sleep"),
        ):
            result = mgr._post_single("offer1abc", "tid123")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))

    def test_5xx_exhausts_retries(self):
        """All attempts return 5xx — manager tries DEXIE_POST_RETRIES+1 times."""
        from config import cfg

        mgr = self._make_manager()
        mock_resp = self._make_5xx_response(503)
        post_mock = MagicMock(return_value=mock_resp)
        with (
            patch("dexie_manager.requests.post", post_mock),
            patch("dexie_manager.time.sleep"),
        ):
            mgr._post_single("offer1abc", "tid456")
        expected_calls = cfg.DEXIE_POST_RETRIES + 1
        self.assertEqual(post_mock.call_count, expected_calls)

    def test_5xx_then_success_returns_success(self):
        """5xx on first attempt then 200 on retry — should return success."""
        mgr = self._make_manager()
        fail_resp = self._make_5xx_response(500)
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"id": "dexie-id-123"}

        with (
            patch("dexie_manager.requests.post", side_effect=[fail_resp, success_resp]),
            patch("dexie_manager.time.sleep"),
            patch("dexie_manager.update_offer_dexie"),
        ):
            result = mgr._post_single("offer1test", "trade_abc")
        self.assertTrue(result.get("success"))

    def test_429_sets_rate_limited_until(self):
        """429 response sets _rate_limited_until to a future timestamp."""
        mgr = self._make_manager()
        rate_limited_resp = MagicMock()
        rate_limited_resp.status_code = 429
        rate_limited_resp.headers = {"Retry-After": "60"}

        before = time.time()
        with (
            patch("dexie_manager.requests.post", return_value=rate_limited_resp),
            patch("dexie_manager.time.sleep"),
        ):
            mgr._post_single("offer1xyz", "tid789")
        self.assertGreater(mgr._rate_limited_until, before + 30)

    def test_429_respects_retry_after_header(self):
        """Retry-After: 90 → _rate_limited_until is ~90s in the future."""
        mgr = self._make_manager()
        rate_limited_resp = MagicMock()
        rate_limited_resp.status_code = 429
        rate_limited_resp.headers = {"Retry-After": "90"}

        before = time.time()
        with (
            patch("dexie_manager.requests.post", return_value=rate_limited_resp),
            patch("dexie_manager.time.sleep"),
        ):
            mgr._post_single("offer1rrr", "trrr")
        self.assertGreaterEqual(mgr._rate_limited_until, before + 89)

    def test_rate_limited_until_respected(self):
        """When _rate_limited_until is in the future, _post_single returns without calling requests.post."""
        mgr = self._make_manager()
        mgr._rate_limited_until = time.time() + 3600  # far future
        post_mock = MagicMock()
        with patch("dexie_manager.requests.post", post_mock):
            result = mgr._post_single("offer1abc", "tid")
        # Should short-circuit without making any HTTP call
        post_mock.assert_not_called()
        self.assertFalse(result.get("success"))

    def test_rate_limit_cleared_after_window(self):
        """When _rate_limited_until is in the past, requests proceed."""
        mgr = self._make_manager()
        mgr._rate_limited_until = time.time() - 1  # expired
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"id": "ok-id"}
        with (
            patch("dexie_manager.requests.post", return_value=success_resp),
            patch("dexie_manager.update_offer_dexie"),
        ):
            result = mgr._post_single("offer1ok", "tok")
        # Requests.post should be called since rate limit expired
        self.assertTrue(result.get("success"))

    def test_connection_error_does_not_raise(self):
        """Network-level ConnectionError must be caught and return error dict."""
        import requests as req

        mgr = self._make_manager()
        with (
            patch(
                "dexie_manager.requests.post",
                side_effect=req.ConnectionError("connection refused"),
            ),
            patch("dexie_manager.time.sleep"),
        ):
            result = mgr._post_single("offer1conn", "tcnn")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))

    def test_timeout_does_not_raise(self):
        """Timeout must be caught and return error dict."""
        import requests as req

        mgr = self._make_manager()
        with (
            patch("dexie_manager.requests.post", side_effect=req.Timeout("timed out")),
            patch("dexie_manager.time.sleep"),
        ):
            result = mgr._post_single("offer1to", "tto")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))


# ---------------------------------------------------------------------------
# 07-04: TibetSwap API 5xx — PriceEngine fallback
# ---------------------------------------------------------------------------

try:
    from price_engine import PriceEngine, _tibet_cache, _tibet_lock

    _PE_SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    PriceEngine = None
    _tibet_cache = None
    _tibet_lock = None
    _PE_SKIP = str(exc)


@unittest.skipIf(_PE_SKIP is not None, f"price_engine unavailable: {_PE_SKIP}")
class TestTibetSwap5xxFallback(unittest.TestCase):
    """PriceEngine should fall back to Dexie price when TibetSwap returns 5xx."""

    def setUp(self):
        with _tibet_lock:
            _tibet_cache["pairs"] = []
            _tibet_cache["fetched_at"] = 0

    def tearDown(self):
        with _tibet_lock:
            _tibet_cache["pairs"] = []
            _tibet_cache["fetched_at"] = 0

    def _make_engine(self):
        return PriceEngine()

    def _fail_resp(self, status=500):
        import requests as _req

        resp = MagicMock()
        resp.status_code = status
        resp.raise_for_status.side_effect = _req.HTTPError(f"HTTP {status}")
        return resp

    def test_tibet_5xx_returns_empty_pairs(self):
        """5xx with no stale cache → _get_tibet_pairs returns []."""
        engine = self._make_engine()
        with patch.object(engine._session, "get", return_value=self._fail_resp()):
            pairs = engine._get_tibet_pairs()
        self.assertEqual(pairs, [])

    def test_tibet_5xx_falls_back_to_stale_cache(self):
        """When cache is within max_stale_secs, stale pairs are returned."""
        engine = self._make_engine()
        with _tibet_lock:
            _tibet_cache["pairs"] = [{"asset_id": "abc", "xch_reserve": 1000}]
            _tibet_cache["fetched_at"] = time.time() - 60  # 1 min old

        with patch.object(engine._session, "get", return_value=self._fail_resp()):
            pairs = engine._get_tibet_pairs()
        self.assertEqual(len(pairs), 1)

    def test_tibet_5xx_does_not_raise(self):
        """5xx must be caught — _get_tibet_pairs never propagates exceptions."""
        engine = self._make_engine()
        try:
            with patch.object(
                engine._session, "get", return_value=self._fail_resp(503)
            ):
                engine._get_tibet_pairs()
        except Exception as exc:
            self.fail(f"_get_tibet_pairs raised: {exc}")

    def test_get_price_returns_none_when_both_sources_fail(self):
        """When Tibet and Dexie both fail, get_price returns None (not crash)."""
        engine = self._make_engine()
        with (
            patch.object(engine, "_fetch_tibet_price", return_value=None),
            patch.object(engine, "_fetch_dexie_price", return_value=None),
            patch.object(engine, "_apply_safety_guards", side_effect=lambda p: p),
            patch("price_engine.record_price"),
        ):
            result = engine.get_price("a" * 64, cat_decimals=3, ticker_id="TST_XCH")
        self.assertIsNone(result)

    def test_get_price_uses_dexie_when_tibet_fails(self):
        """tibet_price=None + dexie_price available → strategy falls through to dexie."""
        engine = self._make_engine()
        dexie_price = Decimal("0.001")

        with (
            patch.object(engine, "_fetch_tibet_price", return_value=None),
            patch.object(engine, "_fetch_dexie_price", return_value=dexie_price),
            patch.object(engine, "_apply_safety_guards", side_effect=lambda p: p),
            patch("price_engine.record_price"),
        ):
            result = engine.get_price("a" * 64, cat_decimals=3, ticker_id="TST_XCH")
        self.assertEqual(result["mid_price"], dexie_price)

    def test_tibet_429_does_not_use_stale_empty_cache(self):
        """429 with no cached data → returns []."""
        engine = self._make_engine()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        with patch.object(engine._session, "get", return_value=resp_429):
            pairs = engine._get_tibet_pairs()
        self.assertEqual(pairs, [])


# ---------------------------------------------------------------------------
# 07-06: DB row inconsistency — fill tracker handles missing offer gracefully
# ---------------------------------------------------------------------------

try:
    from fill_tracker import FillTracker

    _FT_SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    FillTracker = None
    _FT_SKIP = str(exc)


@unittest.skipIf(_FT_SKIP is not None, f"fill_tracker unavailable: {_FT_SKIP}")
class TestDBInconsistency(unittest.TestCase):
    """fill_tracker must not crash when DB returns None for an offer lookup."""

    def _make_tracker(self):
        offer_mgr = MagicMock()
        offer_mgr.is_bot_cancelled.return_value = False
        return FillTracker(offer_manager=offer_mgr)

    def test_get_offer_context_handles_none_db_offer(self):
        """_get_offer_context should not raise when get_offer returns None."""
        tracker = self._make_tracker()
        # The function does `from database import get_offer` inside a try block
        with patch("database.get_offer", return_value=None):
            try:
                ctx = tracker._get_offer_context("nonexistent-trade-id", "buy", {})
            except Exception as exc:
                self.fail(f"_get_offer_context raised: {exc}")
        self.assertIsInstance(ctx, dict)

    def test_get_offer_context_defaults_on_missing_offer(self):
        """Missing DB offer produces default 'unknown' tier and None price."""
        tracker = self._make_tracker()
        with patch("database.get_offer", return_value=None):
            ctx = tracker._get_offer_context("ghost-trade", "sell", {})
        self.assertEqual(ctx.get("tier"), "unknown")
        self.assertIsNone(ctx.get("price"))

    def test_get_offer_context_uses_cache_when_db_missing(self):
        """Details cache is preferred over DB; missing DB doesn't overwrite good cache."""
        tracker = self._make_tracker()
        cache = {
            "good-trade": {
                "price": "0.001",
                "tier": "inner",
                "size_xch": "1.0",
                "size_cat": "1000",
            }
        }
        with patch("database.get_offer", return_value=None):
            ctx = tracker._get_offer_context("good-trade", "buy", cache)
        self.assertEqual(ctx.get("price"), "0.001")
        self.assertEqual(ctx.get("tier"), "inner")

    def test_get_offer_context_handles_db_exception(self):
        """Exception from get_offer is caught and does not propagate."""
        tracker = self._make_tracker()
        with patch("database.get_offer", side_effect=Exception("DB connection lost")):
            try:
                ctx = tracker._get_offer_context("bad-trade", "buy", {})
            except Exception as exc:
                self.fail(f"DB exception propagated: {exc}")
        self.assertIsInstance(ctx, dict)

    def test_get_offer_context_trade_id_preserved(self):
        """trade_id must be echoed back even when DB and cache are both empty."""
        tracker = self._make_tracker()
        with patch("database.get_offer", return_value=None):
            ctx = tracker._get_offer_context("my-trade-id", "buy", {})
        self.assertEqual(ctx.get("trade_id"), "my-trade-id")


# ---------------------------------------------------------------------------
# 07-08: System clock jumps backward — negative time deltas handled gracefully
# ---------------------------------------------------------------------------


@unittest.skipIf(_PE_SKIP is not None, f"price_engine unavailable: {_PE_SKIP}")
class TestClockJump(unittest.TestCase):
    """All time-sensitive code must handle a backward clock gracefully."""

    def test_get_last_price_negative_age_does_not_expire(self):
        """If the clock jumps backward, age is negative.
        A valid cached price must NOT be considered stale (age <= max_age_secs)."""
        engine = PriceEngine()
        engine._last_mid_price = Decimal("0.001")
        engine._last_price_time = time.time() + 9999  # "future" — clock went back

        price = engine.get_last_price(max_age_secs=300)
        # age = now - (now+9999) = negative → -9999 > 300 is False → not expired
        self.assertEqual(price, Decimal("0.001"))

    def test_get_last_price_zero_bypass_always_returns_price(self):
        """max_age_secs=0 skips staleness check regardless of clock."""
        engine = PriceEngine()
        engine._last_mid_price = Decimal("0.002")
        engine._last_price_time = 0

        price = engine.get_last_price(max_age_secs=0)
        self.assertEqual(price, Decimal("0.002"))

    def test_get_last_price_none_returns_none(self):
        """No cached price → None regardless of clock state."""
        engine = PriceEngine()
        engine._last_mid_price = None
        price = engine.get_last_price(max_age_secs=300)
        self.assertIsNone(price)

    def test_uptime_negative_does_not_crash(self):
        """int(time.time() - future_start) yields a negative int — no crash."""
        start_time_future = time.time() + 10000
        uptime = int(time.time() - start_time_future)
        self.assertIsInstance(uptime, int)
        self.assertLess(uptime, 0)

    def test_dexie_rate_limit_in_far_future(self):
        """After a clock jump backward, _rate_limited_until appears far in future.
        _post_single must still short-circuit and return an error dict."""
        from dexie_manager import DexieManager

        mgr = DexieManager()
        mgr._rate_limited_until = time.time() + 86400  # 24h "in future"
        post_mock = MagicMock()
        with patch("dexie_manager.requests.post", post_mock):
            result = mgr._post_single("offer1rl", "t_rl")
        post_mock.assert_not_called()
        self.assertFalse(result.get("success"))

    def test_dexie_rate_limit_past_timestamp(self):
        """If clock jumped forward past _rate_limited_until, HTTP calls proceed."""
        from dexie_manager import DexieManager

        mgr = DexieManager()
        mgr._rate_limited_until = time.time() - 1  # already expired
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.json.return_value = {"id": "test-id"}
        with (
            patch("dexie_manager.requests.post", return_value=success_resp),
            patch("dexie_manager.update_offer_dexie"),
        ):
            result = mgr._post_single("offer1ok", "tok")
        self.assertTrue(result.get("success"))

    def test_tibet_cache_negative_stale_age_does_not_crash(self):
        """Clock jumped backward → fetched_at > now → stale_age is negative.
        The stale-cache guard (age <= max_stale_secs) still evaluates correctly."""
        engine = PriceEngine()
        with _tibet_lock:
            _tibet_cache["pairs"] = [{"test": True}]
            _tibet_cache["fetched_at"] = time.time() + 9999  # "future"

        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = Exception("HTTP 500")
        try:
            with patch.object(engine._session, "get", return_value=resp):
                pairs = engine._get_tibet_pairs()
        except Exception as exc:
            self.fail(f"Clock-jump stale-cache check raised: {exc}")
        # Negative age (-9999) <= max_stale_secs (300) → True → stale pairs returned
        self.assertIsInstance(pairs, list)
        self.assertTrue(len(pairs) >= 1)

        with _tibet_lock:
            _tibet_cache["pairs"] = []
            _tibet_cache["fetched_at"] = 0


# ---------------------------------------------------------------------------
# 07-07: Disk space exhausted — write operations degrade gracefully
# ---------------------------------------------------------------------------


class TestDiskFull(unittest.TestCase):
    """Simulates sqlite3.OperationalError: database or disk is full.

    Every critical write function must:
    1. Not raise (bot continues running)
    2. Return a failure sentinel (-1 or False)
    3. Call rollback to release the write lock (prevents deadlock cascade)

    This is the real failure mode — a full disk causes the conn.commit() inside
    each function to raise OperationalError. Without a rollback, the RESERVED
    lock is held indefinitely, blocking ALL other writers.
    """

    _DISK_FULL_ERR = "database or disk is full"

    def _disk_full(self):
        import sqlite3

        return sqlite3.OperationalError(self._DISK_FULL_ERR)

    def test_record_fill_returns_minus_one_on_disk_full(self):
        """record_fill() returns -1 (not raises) when commit fails."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.commit.side_effect = self._disk_full()

        with (
            patch("database.get_connection", return_value=mock_conn),
            patch("database.log_event"),
        ):
            result = db.record_fill(
                "trade1",
                "buy",
                Decimal("0.001"),
                Decimal("1.0"),
                Decimal("1000"),
                "a" * 64,
            )
        self.assertEqual(result, -1)

    def test_record_fill_calls_rollback_on_disk_full(self):
        """record_fill() must call rollback after a failed commit."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.commit.side_effect = self._disk_full()

        with (
            patch("database.get_connection", return_value=mock_conn),
            patch("database.log_event"),
        ):
            db.record_fill(
                "trade2",
                "sell",
                Decimal("0.001"),
                Decimal("1.0"),
                Decimal("1000"),
                "a" * 64,
            )
        mock_conn.rollback.assert_called()

    def test_record_price_returns_false_on_disk_full(self):
        """record_price() returns False (not raises) when commit fails."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.commit.side_effect = self._disk_full()

        with patch("database.get_connection", return_value=mock_conn):
            result = db.record_price(
                "a" * 64,
                Decimal("0.001"),
                dexie_price=Decimal("0.001"),
                tibet_price=None,
            )
        self.assertFalse(result)

    def test_record_price_calls_rollback_on_disk_full(self):
        """record_price() must call rollback to release write lock."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.commit.side_effect = self._disk_full()

        with patch("database.get_connection", return_value=mock_conn):
            db.record_price("a" * 64, Decimal("0.001"))
        mock_conn.rollback.assert_called()

    def test_log_event_returns_false_on_disk_full(self):
        """log_event() returns False (not raises) when commit fails."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.commit.side_effect = self._disk_full()

        with (
            patch("database.get_connection", return_value=mock_conn),
            patch("database._sse_callback", None),
        ):
            result = db.log_event("error", "disk_full_test", "test message")
        self.assertFalse(result)

    def test_log_event_calls_rollback_on_disk_full(self):
        """log_event() must rollback to prevent permanent write lock."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.commit.side_effect = self._disk_full()

        with (
            patch("database.get_connection", return_value=mock_conn),
            patch("database._sse_callback", None),
        ):
            db.log_event("warning", "disk_full_test", "test message")
        mock_conn.rollback.assert_called()

    def test_consecutive_disk_full_does_not_cascade_exception(self):
        """Multiple consecutive write failures must each be swallowed,
        not cascade into an unhandled exception that crashes the thread."""
        import database as db

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        mock_conn.commit.side_effect = self._disk_full()

        with (
            patch("database.get_connection", return_value=mock_conn),
            patch("database.log_event"),
        ):
            try:
                for _ in range(5):
                    db.record_price("a" * 64, Decimal("0.001"))
                    db.log_event("error", "disk_full", "disk full")
            except Exception as exc:
                self.fail(f"Consecutive disk-full raised: {exc}")


if __name__ == "__main__":
    unittest.main()
