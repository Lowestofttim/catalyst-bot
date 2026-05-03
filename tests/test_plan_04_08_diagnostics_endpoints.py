"""Slice 04-08 — diagnostics endpoint contract tests.

Tests GET /api/diagnostics/runtime and GET /api/diagnostics/api-stats:
  - No auth required (read-only)
  - bot=None returns safe empty shapes
  - Response shape and required keys
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


def _make_bot():
    bot = MagicMock()
    bot.get_state.return_value = {"diagnostics": {"enabled": True, "recent_actions": []}}
    bot.coinset_client.get_stats.return_value = {
        "mode": "coinset", "total_calls": 0, "hit_rate": 0,
        "fallback_count": 0, "session_hits": 0, "session_misses": 0,
    }
    # Route calls getattr(bot.coinset_client, "_rate_limited_until", 0.0).
    # MagicMock auto-creates attributes, so pin this to a real float.
    bot.coinset_client._rate_limited_until = 0.0
    bot.dexie_manager.get_stats.return_value = {
        "total_posted": 0, "total_failed": 0, "total_skipped": 0,
        "queue_size": 0, "tracked_mappings": 0, "fingerprints_cached": 0,
        "session_posted": 0, "session_failed": 0, "session_skipped": 0,
        "known_mappings": 0, "hydrated_from_db": False,
    }
    bot.dexie_manager._rate_limited_until = 0.0
    bot.dexie_manager._v3_trades_cache = {}
    bot.dexie_manager._v3_pairs_cache = None
    bot.amm_monitor.get_stats.return_value = {}
    # /api/diagnostics/api-stats inspects splash + price_engine + bot._bot_state
    # — set them to None so the truthy-check short-circuits past MagicMock.
    bot.splash_manager = None
    bot.price_engine = None
    bot._bot_state = {}
    return bot


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()


# ---------------------------------------------------------------------------
# 1. GET /api/diagnostics/runtime
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestDiagnosticsRuntime(_FlaskBase):

    def test_returns_200(self):
        resp = self.client.get("/api/diagnostics/runtime",
                               environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_bot_none_returns_safe_shape(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/diagnostics/runtime",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body.get("enabled"))
        self.assertEqual(body.get("status"), "idle")
        self.assertIsInstance(body.get("recent_actions"), list)

    def test_bot_set_returns_diagnostics_payload(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/diagnostics/runtime",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsInstance(body, dict)

    def test_response_is_dict(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/diagnostics/runtime",
                                   environ_base=self._LOOPBACK)
        self.assertIsInstance(resp.get_json(), dict)


# ---------------------------------------------------------------------------
# 2. GET /api/diagnostics/api-stats
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestDiagnosticsApiStats(_FlaskBase):

    def test_returns_200(self):
        resp = self.client.get("/api/diagnostics/api-stats",
                               environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_top_level_keys(self):
        resp = self.client.get("/api/diagnostics/api-stats",
                               environ_base=self._LOOPBACK)
        body = resp.get_json()
        for key in ("spacescan", "coinset", "dexie"):
            self.assertIn(key, body)

    def test_bot_none_coinset_not_available(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/diagnostics/api-stats",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body["coinset"].get("available"))

    def test_bot_none_dexie_not_available(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/diagnostics/api-stats",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body["dexie"].get("available"))

    def test_bot_set_coinset_available(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/diagnostics/api-stats",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body["coinset"].get("available"))

    def test_bot_set_dexie_available(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/diagnostics/api-stats",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body["dexie"].get("available"))

    def test_spacescan_includes_token_context_cache_age(self):
        with patch.object(api_server, "bot", _make_bot()), \
             patch.dict(api_server._active_cat, {"asset_id": "asset123"}, clear=False), \
             patch("spacescan.get_api_stats", return_value={
                 "tier": "paid",
                 "calls_this_session": 1,
                 "calls_today": 1,
                 "session_uptime_hours": 2.0,
                 "daily_budget": "plan-dependent",
                 "call_interval_secs": 2.0,
                 "calls_by_endpoint": {"/stats/price": 1},
             }), \
             patch("database.get_market_analysis_cache", return_value={"has_data": True}), \
             patch("database.get_market_analysis_cache_age_secs", return_value=123):
            resp = self.client.get("/api/diagnostics/api-stats",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body["spacescan"].get("token_context_cached"))
        self.assertEqual(body["spacescan"].get("token_context_cache_age_secs"), 123)
        self.assertEqual(body["spacescan"].get("calls_by_endpoint"), {"/stats/price": 1})


if __name__ == "__main__":
    unittest.main()
