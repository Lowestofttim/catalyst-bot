"""Slice 04-05 — PnL + fills/purge endpoint contract tests.

Tests /api/pnl, /api/pnl/reset-preview, /api/pnl/reset (POST),
/api/fills/purge (POST):
  - Auth required for write endpoints
  - bot=None → 500 for bot-dependent reads
  - Confirmation token gate on /api/pnl/reset
  - Response shape and required keys
  - Risk manager callback when bot is set
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_stats():
    return {
        "realised_pnl_xch": "0.05",
        "total_fills": 10,
        "buy_fills": 5,
        "sell_fills": 5,
        "round_trips": 3,
        "win_rate": 80.0,
        "fill_rate_per_hour": 2.5,
        "avg_spread_capture": "0.002",
        "unmatched_buy_fills": 1,
        "unmatched_sell_fills": 0,
        "volume_xch": "1.5",
        "volume_cat": "500",
        "buy_volume_xch": "0.75",
        "buy_volume_cat": "250",
        "sell_volume_xch": "0.75",
        "sell_volume_cat": "250",
        "net_xch_flow": "0.0",
        "net_cat_flow": "0",
        "avg_fill_size_xch": "0.15",
        "avg_round_trip_secs": 300,
        "avg_pnl_per_trip_xch": "0.016",
    }


def _make_bot():
    bot = MagicMock()
    bot.is_running.return_value = True
    bot.risk_manager.get_inventory_state.return_value = {
        "net_position_cat": "0",
        "circuit_breaker_active": False,
    }
    bot.sniper.get_stats.return_value = {}
    return bot


def _make_db_conn(fill_count=0, rt_count=0):
    """Return a mock DB connection that satisfies SELECT COUNT + sqlite_master queries."""
    mock_conn = MagicMock()

    def _execute(sql, *args, **kwargs):
        result = MagicMock()
        sql_upper = sql.strip().upper()
        if "COUNT" in sql_upper and "FILLS" in sql_upper and "ROUND_TRIPS" not in sql_upper:
            result.fetchone.return_value = {"cnt": fill_count}
        elif "COUNT" in sql_upper and "ROUND_TRIPS" in sql_upper:
            result.fetchone.return_value = {"cnt": rt_count}
        elif "SQLITE_MASTER" in sql_upper and "ROUND_TRIPS" in sql_upper:
            result.fetchone.return_value = {"name": "round_trips"}
        else:
            result.fetchone.return_value = None
        return result

    mock_conn.execute.side_effect = _execute
    return mock_conn


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        self.auth = {"X-Bot-Local-Token": self.token}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def _post(self, path, body=None, auth=True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body or {},
            headers=headers,
            environ_base=self._LOOPBACK,
        )


# ---------------------------------------------------------------------------
# 1. GET /api/pnl
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestPnlGet(_FlaskBase):

    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_bot_set_returns_200(self):
        with patch.object(api_server, "bot", _make_bot()), \
             patch("api_server.get_stats", return_value=_fake_stats()):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_required_keys(self):
        with patch.object(api_server, "bot", _make_bot()), \
             patch("api_server.get_stats", return_value=_fake_stats()):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        body = resp.get_json()
        for key in ("realised_pnl_xch", "total_fills", "buy_fills", "sell_fills",
                    "round_trips", "net_position_cat", "circuit_breaker_active",
                    "volume_xch", "fill_rate_per_hour"):
            self.assertIn(key, body)

    def test_fill_counts_match_stats(self):
        with patch.object(api_server, "bot", _make_bot()), \
             patch("api_server.get_stats", return_value=_fake_stats()):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertEqual(body["total_fills"], 10)
        self.assertEqual(body["buy_fills"], 5)
        self.assertEqual(body["sell_fills"], 5)

    def test_sniper_key_present(self):
        with patch.object(api_server, "bot", _make_bot()), \
             patch("api_server.get_stats", return_value=_fake_stats()):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("sniper", body)

    def test_response_includes_usd_values_when_xch_price_available(self):
        with patch.object(api_server, "bot", _make_bot()), \
             patch("api_server.get_stats", return_value=_fake_stats()), \
             patch("market_data_collector.get_cached_xch_usd_price",
                   return_value={
                       "has_data": True,
                       "xch_usd": 2.10,
                       "source": "spacescan",
                   },
                   create=True), \
             patch("database.get_market_analysis_cache",
                   return_value={"price_usd": 0.01}):
            resp = self.client.get("/api/pnl", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertEqual(body["xch_usd_price"], "2.1")
        self.assertEqual(body["xch_usd_source"], "spacescan")
        self.assertEqual(body["realised_pnl_usd"], "0.1050")
        self.assertEqual(body["avg_pnl_per_trip_usd"], "0.0336")
        self.assertEqual(body["volume_usd"], "3.1500")
        self.assertEqual(body["cat_usd_price"], "0.01")


# ---------------------------------------------------------------------------
# 2. GET /api/pnl/reset-preview
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestPnlResetPreview(_FlaskBase):

    def test_returns_200(self):
        with patch("api_server.get_stats", return_value=_fake_stats()), \
             patch("database.get_connection", return_value=_make_db_conn()):
            resp = self.client.get("/api/pnl/reset-preview",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_success_key_true(self):
        with patch("api_server.get_stats", return_value=_fake_stats()), \
             patch("database.get_connection", return_value=_make_db_conn()):
            resp = self.client.get("/api/pnl/reset-preview",
                                   environ_base=self._LOOPBACK)
        self.assertTrue(resp.get_json().get("success"))

    def test_response_has_required_keys(self):
        with patch("api_server.get_stats", return_value=_fake_stats()), \
             patch("database.get_connection", return_value=_make_db_conn()):
            resp = self.client.get("/api/pnl/reset-preview",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        for key in ("has_data", "fills", "round_trips", "realised_pnl_xch"):
            self.assertIn(key, body)

    def test_fills_and_round_trips_are_integers(self):
        with patch("api_server.get_stats", return_value=_fake_stats()), \
             patch("database.get_connection", return_value=_make_db_conn()):
            resp = self.client.get("/api/pnl/reset-preview",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIsInstance(body["fills"], int)
        self.assertIsInstance(body["round_trips"], int)

    def test_has_data_false_when_everything_zero(self):
        zero_stats = {**_fake_stats(), "realised_pnl_xch": "0"}
        with patch("api_server.get_stats", return_value=zero_stats), \
             patch("database.get_connection", return_value=_make_db_conn(0, 0)), \
             patch.object(api_server, "bot", None):
            resp = self.client.get("/api/pnl/reset-preview",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertFalse(body["has_data"])


# ---------------------------------------------------------------------------
# 3. POST /api/pnl/reset
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestPnlReset(_FlaskBase):

    _FAKE_SUMMARY = {
        "fills_cleared": 0, "round_trips_cleared": 0,
        "price_history_cleared": False, "inventory_cleared": False,
        "coins_cleared": 0, "open_offers_cancelled": 0,
        "reset_at": "2026-01-01T00:00:00", "preserve_history": False,
    }

    def test_requires_token(self):
        resp = self._post("/api/pnl/reset", {"confirm": "RESET"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_missing_confirm_returns_400(self):
        resp = self._post("/api/pnl/reset", {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json().get("error"), "confirmation_required")

    def test_wrong_confirm_value_returns_400(self):
        resp = self._post("/api/pnl/reset", {"confirm": "yes"})
        self.assertEqual(resp.status_code, 400)

    def test_confirm_case_insensitive(self):
        # Handler does .strip().upper() — lowercase "reset" is accepted
        with patch.object(api_server, "_reset_fresh_run_session",
                          return_value=self._FAKE_SUMMARY):
            resp = self._post("/api/pnl/reset", {"confirm": "reset"})
        self.assertEqual(resp.status_code, 200)

    def test_correct_confirm_returns_200(self):
        with patch.object(api_server, "_reset_fresh_run_session",
                          return_value=self._FAKE_SUMMARY):
            resp = self._post("/api/pnl/reset", {"confirm": "RESET"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("success"))

    def test_response_includes_message(self):
        with patch.object(api_server, "_reset_fresh_run_session",
                          return_value=self._FAKE_SUMMARY):
            resp = self._post("/api/pnl/reset", {"confirm": "RESET"})
        self.assertIn("message", resp.get_json())


# ---------------------------------------------------------------------------
# 4. POST /api/fills/purge
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestFillsPurge(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/fills/purge", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200_and_success(self):
        conn = _make_db_conn(fill_count=5, rt_count=2)
        with patch("database.get_connection", return_value=conn), \
             patch("database.log_event"), \
             patch.object(api_server, "bot", None):
            resp = self._post("/api/fills/purge")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

    def test_response_has_purge_count_keys(self):
        conn = _make_db_conn(fill_count=3, rt_count=1)
        with patch("database.get_connection", return_value=conn), \
             patch("database.log_event"), \
             patch.object(api_server, "bot", None):
            resp = self._post("/api/fills/purge")
        body = resp.get_json()
        self.assertIn("fills_purged", body)
        self.assertIn("round_trips_purged", body)

    def test_resets_risk_manager_when_bot_set(self):
        conn = _make_db_conn()
        bot = MagicMock()
        bot.risk_manager = MagicMock()
        with patch("database.get_connection", return_value=conn), \
             patch("database.log_event"), \
             patch.object(api_server, "bot", bot):
            self._post("/api/fills/purge")
        bot.risk_manager.reset_position.assert_called_once()

    def test_no_risk_manager_call_when_bot_none(self):
        conn = _make_db_conn()
        bot = MagicMock()
        bot.risk_manager = MagicMock()
        # Only called when bot AND bot.risk_manager are set — verify integration
        with patch("database.get_connection", return_value=conn), \
             patch("database.log_event"), \
             patch.object(api_server, "bot", None):
            resp = self._post("/api/fills/purge")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
