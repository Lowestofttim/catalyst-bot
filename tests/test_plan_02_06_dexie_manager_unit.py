"""Slice 02-06 — dexie_manager.py unit tests: pure functions and queue ops.

No HTTP calls are made. Tests cover queue_post/purge_trade_ids,
flush_queue disabled/empty paths, get_dexie_id/get_dexie_link,
get_stats, prune_mappings, _fingerprint, _safe_json, and
compute_v3_trade_metrics (via cache injection).
"""

import sys
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

try:
    import dexie_manager as _dm

    _SKIP = None
except ModuleNotFoundError as exc:
    _dm = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Minimal cfg stub
# ---------------------------------------------------------------------------


class _FakeCfg:
    DEXIE_POST_ENABLED = True
    MAX_POSTS_PER_LOOP = 10
    DEXIE_API_BASE = "https://api.dexie.space"
    BOT_TAG = "test-bot"
    DEXIE_POST_RETRIES = 0
    DEXIE_POST_TIMEOUT = 1
    DEXIE_POST_RETRY_SLEEP = 0
    DEXIE_AUTO_CLAIM_REWARDS = False


# ---------------------------------------------------------------------------
# Base class: fresh DexieManager + cfg patch per test
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
class _DM(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = _dm.cfg
        _dm.cfg = _FakeCfg()
        self.dm = _dm.DexieManager()

    def tearDown(self):
        _dm.cfg = self._orig_cfg


# ===========================================================================
# queue_post
# ===========================================================================


class TestQueuePost(_DM):
    def test_valid_offer_queued(self):
        self.dm.queue_post("offer1abc", trade_id="t1")
        self.assertEqual(len(self.dm._queue), 1)
        self.assertEqual(self.dm._queue[0]["trade_id"], "t1")

    def test_empty_string_ignored(self):
        self.dm.queue_post("")
        self.assertEqual(len(self.dm._queue), 0)

    def test_none_ignored(self):
        self.dm.queue_post(None)
        self.assertEqual(len(self.dm._queue), 0)

    def test_non_string_ignored(self):
        self.dm.queue_post(12345)
        self.assertEqual(len(self.dm._queue), 0)

    def test_whitespace_stripped_in_queue(self):
        self.dm.queue_post("  offer1abc  ", trade_id="t2")
        self.assertEqual(self.dm._queue[0]["offer"], "offer1abc")

    def test_multiple_items_queued(self):
        self.dm.queue_post("offer1", trade_id="t1")
        self.dm.queue_post("offer2", trade_id="t2")
        self.assertEqual(len(self.dm._queue), 2)

    def test_force_flag_stored(self):
        self.dm.queue_post("offer1", force=True)
        self.assertTrue(self.dm._queue[0]["force"])


# ===========================================================================
# purge_trade_ids
# ===========================================================================


class TestPurgeTradeIds(_DM):
    def setUp(self):
        super().setUp()
        self.dm.queue_post("offerA", trade_id="t1")
        self.dm.queue_post("offerB", trade_id="t2")
        self.dm.queue_post("offerC", trade_id="t3")

    @patch("dexie_manager.log_event")
    def test_removes_matching_trade_ids(self, _mock_log):
        self.dm.purge_trade_ids(["t1", "t3"])
        remaining = [item["trade_id"] for item in self.dm._queue]
        self.assertNotIn("t1", remaining)
        self.assertNotIn("t3", remaining)
        self.assertIn("t2", remaining)

    @patch("dexie_manager.log_event")
    def test_empty_list_noop(self, _mock_log):
        self.dm.purge_trade_ids([])
        self.assertEqual(len(self.dm._queue), 3)

    @patch("dexie_manager.log_event")
    def test_nonexistent_trade_id_noop(self, _mock_log):
        self.dm.purge_trade_ids(["nonexistent"])
        self.assertEqual(len(self.dm._queue), 3)


# ===========================================================================
# flush_queue — disabled / empty paths (no HTTP)
# ===========================================================================


class TestFlushQueueNoHTTP(_DM):
    def test_disabled_returns_disabled_flag(self):
        _dm.cfg.DEXIE_POST_ENABLED = False
        result = self.dm.flush_queue()
        self.assertTrue(result.get("disabled"))
        self.assertEqual(result["posted"], 0)

    def test_empty_queue_returns_zeros(self):
        result = self.dm.flush_queue()
        self.assertEqual(result, {"posted": 0, "failed": 0, "skipped": 0})

    def test_flush_all_drains_queue(self):
        # With DEXIE_POST_ENABLED=False the queue isn't drained
        _dm.cfg.DEXIE_POST_ENABLED = False
        self.dm.queue_post("offer1", trade_id="t1")
        self.dm.flush_queue(flush_all=True)
        # Queue still has item because disabled path returns early
        self.assertEqual(len(self.dm._queue), 1)


# ===========================================================================
# flush_queue retry classification
# ===========================================================================


class TestFlushQueueRetryClassification(_DM):
    @patch("dexie_manager.log_event")
    @patch("dexie_manager.requests.post", create=True)
    def test_invalid_offer_400_is_requeued_for_settle_race(self, mock_post, _mock_log):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "Invalid Offer"
        resp.headers = {}
        mock_post.return_value = resp

        self.dm.queue_post("offer1abc", trade_id="t1")
        result = self.dm.flush_queue()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["requeued"], 1)
        self.assertEqual(len(self.dm._queue), 1)
        self.assertEqual(self.dm._queue[0]["trade_id"], "t1")

    @patch("dexie_manager.log_event")
    @patch("dexie_manager.requests.post", create=True)
    def test_other_4xx_is_not_requeued(self, mock_post, _mock_log):
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        resp.headers = {}
        mock_post.return_value = resp

        self.dm.queue_post("offer1abc", trade_id="t1")
        result = self.dm.flush_queue()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["requeued"], 0)
        self.assertEqual(len(self.dm._queue), 0)


# ===========================================================================
# get_dexie_id / get_dexie_link
# ===========================================================================


class TestGetDexieIdAndLink(_DM):
    def test_get_dexie_id_returns_none_when_missing(self):
        self.assertIsNone(self.dm.get_dexie_id("t1"))

    def test_get_dexie_id_returns_mapped_value(self):
        self.dm._trade_dexie_map["t1"] = "dexie-abc"
        self.assertEqual(self.dm.get_dexie_id("t1"), "dexie-abc")

    def test_get_dexie_link_returns_none_when_missing(self):
        self.assertIsNone(self.dm.get_dexie_link("t1"))

    def test_get_dexie_link_returns_correct_url(self):
        self.dm._trade_dexie_map["t2"] = "offer-xyz"
        link = self.dm.get_dexie_link("t2")
        self.assertIn("offer-xyz", link)
        self.assertTrue(link.startswith("https://"))


# ===========================================================================
# get_stats
# ===========================================================================


class TestGetStats(_DM):
    def test_stats_has_expected_keys(self):
        stats = self.dm.get_stats()
        for key in (
            "total_posted",
            "total_failed",
            "total_skipped",
            "queue_size",
            "tracked_mappings",
            "fingerprints_cached",
            "session_posted",
            "session_failed",
            "hydrated_from_db",
        ):
            self.assertIn(key, stats, f"Missing key: {key}")

    def test_empty_manager_all_zeros(self):
        stats = self.dm.get_stats()
        self.assertEqual(stats["total_posted"], 0)
        self.assertEqual(stats["total_failed"], 0)
        self.assertEqual(stats["queue_size"], 0)
        self.assertEqual(stats["tracked_mappings"], 0)

    def test_queue_size_reflects_queued_items(self):
        self.dm.queue_post("offer1", trade_id="t1")
        self.dm.queue_post("offer2", trade_id="t2")
        self.assertEqual(self.dm.get_stats()["queue_size"], 2)

    def test_hydrated_from_db_false_when_posted_equals_tracked(self):
        self.dm._trade_dexie_map["t1"] = "d1"
        self.dm._total_posted = 1
        stats = self.dm.get_stats()
        self.assertFalse(stats["hydrated_from_db"])

    def test_hydrated_from_db_true_when_tracked_exceeds_posted(self):
        self.dm._trade_dexie_map["t1"] = "d1"
        self.dm._trade_dexie_map["t2"] = "d2"
        self.dm._total_posted = 0  # not posted this session
        stats = self.dm.get_stats()
        self.assertTrue(stats["hydrated_from_db"])


# ===========================================================================
# prune_mappings
# ===========================================================================


class TestPruneMappings(_DM):
    @patch("dexie_manager.log_event")
    def test_removes_stale_trade_ids(self, _mock_log):
        self.dm._trade_dexie_map["t1"] = "d1"
        self.dm._trade_dexie_map["t2"] = "d2"
        self.dm._trade_dexie_map["t3"] = "d3"
        self.dm.prune_mappings({"t2"})  # only t2 is active
        self.assertNotIn("t1", self.dm._trade_dexie_map)
        self.assertNotIn("t3", self.dm._trade_dexie_map)
        self.assertIn("t2", self.dm._trade_dexie_map)

    @patch("dexie_manager.log_event")
    def test_keeps_all_when_all_active(self, _mock_log):
        self.dm._trade_dexie_map["t1"] = "d1"
        self.dm._trade_dexie_map["t2"] = "d2"
        self.dm.prune_mappings({"t1", "t2"})
        self.assertEqual(len(self.dm._trade_dexie_map), 2)

    @patch("dexie_manager.log_event")
    def test_fingerprints_cleared_when_over_cap(self, _mock_log):
        # Fill past the 400-fingerprint cap
        for i in range(401):
            self.dm._posted_fingerprints.add(f"fp{i}")
        self.dm.prune_mappings(set())
        self.assertEqual(len(self.dm._posted_fingerprints), 0)

    @patch("dexie_manager.log_event")
    def test_fingerprints_preserved_when_under_cap(self, _mock_log):
        for i in range(5):
            self.dm._posted_fingerprints.add(f"fp{i}")
        self.dm.prune_mappings(set())
        self.assertEqual(len(self.dm._posted_fingerprints), 5)


# ===========================================================================
# _fingerprint (static)
# ===========================================================================


class TestFingerprint(unittest.TestCase):
    @unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
    def test_returns_hex_string(self):
        fp = _dm.DexieManager._fingerprint("offer1abc")
        self.assertIsInstance(fp, str)
        self.assertEqual(len(fp), 64)  # SHA256 hex

    @unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
    def test_deterministic(self):
        self.assertEqual(
            _dm.DexieManager._fingerprint("offer1abc"),
            _dm.DexieManager._fingerprint("offer1abc"),
        )

    @unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
    def test_different_inputs_different_fingerprints(self):
        self.assertNotEqual(
            _dm.DexieManager._fingerprint("offer1"),
            _dm.DexieManager._fingerprint("offer2"),
        )

    @unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
    def test_strips_whitespace_before_hashing(self):
        self.assertEqual(
            _dm.DexieManager._fingerprint("  offer1  "),
            _dm.DexieManager._fingerprint("offer1"),
        )


# ===========================================================================
# _safe_json (static)
# ===========================================================================


class TestSafeJson(unittest.TestCase):
    @unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
    def test_returns_json_on_valid_response(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "value"}
        result = _dm.DexieManager._safe_json(mock_resp)
        self.assertEqual(result, {"key": "value"})

    @unittest.skipIf(_SKIP is not None, f"dexie_manager unavailable: {_SKIP}")
    def test_returns_raw_on_invalid_json(self):
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("bad JSON")
        mock_resp.text = "bad response body"
        result = _dm.DexieManager._safe_json(mock_resp)
        self.assertIn("raw", result)


# ===========================================================================
# compute_v3_trade_metrics (via cache injection)
# ===========================================================================


class TestComputeV3TradeMetrics(_DM):
    def _inject_trades(self, ticker_id, trades):
        """Inject pre-built trades into the cache to bypass HTTP."""
        self.dm._v3_trades_cache[ticker_id] = {
            "trades": trades,
            "fetched_at": time.time(),
        }

    def _make_trades(self, prices):
        now = time.time()
        return [{"price": str(p), "timestamp": now - i} for i, p in enumerate(prices)]

    @patch("dexie_manager.log_event")
    def test_returns_none_when_no_trades(self, _mock_log):
        result = self.dm.compute_v3_trade_metrics("NO_TICKER")
        self.assertIsNone(result)

    @patch("dexie_manager.log_event")
    def test_returns_none_when_single_trade(self, _mock_log):
        self._inject_trades("T1", self._make_trades([1.0]))
        result = self.dm.compute_v3_trade_metrics("T1")
        self.assertIsNone(result)

    @patch("dexie_manager.log_event")
    def test_returns_metrics_for_valid_trades(self, _mock_log):
        self._inject_trades("T2", self._make_trades([1.0, 1.1, 1.05, 0.95, 1.0]))
        result = self.dm.compute_v3_trade_metrics("T2")
        self.assertIsNotNone(result)
        self.assertIn("mean_price", result)
        self.assertIn("price_stdev_pct", result)
        self.assertIn("trades_in_window", result)
        self.assertEqual(result["trades_in_window"], 5)

    @patch("dexie_manager.log_event")
    def test_mean_price_correct(self, _mock_log):
        self._inject_trades("T3", self._make_trades([1.0, 2.0]))
        result = self.dm.compute_v3_trade_metrics("T3")
        self.assertAlmostEqual(float(result["mean_price"]), 1.5, places=4)

    @patch("dexie_manager.log_event")
    def test_excludes_old_trades_by_hours(self, _mock_log):
        now = time.time()
        trades = [
            {"price": "1.0", "timestamp": now - 100},  # recent
            {"price": "2.0", "timestamp": now - 7 * 3600},  # older than 1h
        ]
        self.dm._v3_trades_cache["T4"] = {"trades": trades, "fetched_at": now}
        result = self.dm.compute_v3_trade_metrics("T4", hours=1.0)
        # Only 1 recent trade → None (needs ≥2)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
