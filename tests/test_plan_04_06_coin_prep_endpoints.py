"""Slice 04-06 — coin-prep endpoint contract tests.

Tests /api/coin-prep/status, /api/coin-prep/verify,
/api/coin-prep/trigger (POST), /api/coin-prep/reset (POST):
  - Auth required for write endpoints
  - Response shapes and required keys
  - Trigger returns immediately (background thread pattern)
  - Reset clears running state
"""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    from blueprints import coin_prep as coin_prep_blueprint

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    coin_prep_blueprint = None
    _SKIP = str(exc)


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
        api_server._coin_prep_state["running"] = False
        api_server._coin_prep_state["complete"] = False
        api_server._coin_prep_state["error"] = None
        api_server._coin_prep_state["phase"] = "idle"
        api_server._coin_prep_proc = None

    def _post(self, path, body=None, auth=True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body or {},
            headers=headers,
            environ_base=self._LOOPBACK,
        )


# ---------------------------------------------------------------------------
# 1. GET /api/coin-prep/status
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCoinPrepStatus(_FlaskBase):
    _DRIFT = [
        {
            "side": "xch",
            "tier": "inner",
            "ratio": 0.457,
            "coin_count": 11,
            "median_mojos": 457000000000,
            "live_size_mojos": 1000000000000,
        }
    ]

    def test_returns_200(self):
        with patch("database.get_coin_summary", return_value={}):
            resp = self.client.get("/api/coin-prep/status", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_success_key_true(self):
        with patch("database.get_coin_summary", return_value={}):
            resp = self.client.get("/api/coin-prep/status", environ_base=self._LOOPBACK)
        self.assertTrue(resp.get_json().get("success"))

    def test_response_has_running_complete_keys(self):
        with patch("database.get_coin_summary", return_value={}):
            resp = self.client.get("/api/coin-prep/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("running", body)
        self.assertIn("complete", body)

    def test_running_defaults_false(self):
        orig = api_server._coin_prep_state.get("running")
        api_server._coin_prep_state["running"] = False
        try:
            with patch("database.get_coin_summary", return_value={}):
                resp = self.client.get(
                    "/api/coin-prep/status", environ_base=self._LOOPBACK
                )
            self.assertFalse(resp.get_json()["running"])
        finally:
            api_server._coin_prep_state["running"] = orig

    def test_coin_counts_populated_from_summary(self):
        summary = {
            "xch_free_count": 5,
            "cat_free_count": 10,
            "xch_total": 5,
            "cat_total": 10,
        }
        with (
            patch("database.get_coin_summary", return_value=summary),
            patch.object(api_server, "bot", None),
        ):
            resp = self.client.get("/api/coin-prep/status", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertEqual(body.get("xch_free_coins"), 5)
        self.assertEqual(body.get("cat_free_coins"), 10)

    def test_tier_size_drift_marks_status_as_needing_prep(self):
        summary = {
            "xch_free_count": 5,
            "cat_free_count": 10,
            "xch_total": 5,
            "cat_total": 10,
        }
        with (
            patch("database.get_coin_summary", return_value=summary),
            patch(
                "coin_manager.check_tier_size_drift_standalone",
                return_value=self._DRIFT,
            ),
            patch.object(coin_prep_blueprint.cfg, "TIER_ENABLED", True),
        ):
            resp = self.client.get("/api/coin-prep/status", environ_base=self._LOOPBACK)

        body = resp.get_json()
        self.assertTrue(body.get("needs_coin_prep"))
        self.assertEqual(body.get("reason"), "tier_size_drift")
        self.assertEqual(body.get("tier_size_drift"), self._DRIFT)
        self.assertFalse(body.get("complete"))


# ---------------------------------------------------------------------------
# 2. GET /api/coin-prep/verify
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCoinPrepVerify(_FlaskBase):
    _EMPTY_COINS = {"success": True, "records": []}
    _ZERO_BALANCE = {
        "wallet_balance": {"confirmed_wallet_balance": 0, "spendable_balance": 0}
    }
    _ENOUGH_BALANCE = {
        "wallet_balance": {
            "confirmed_wallet_balance": 10_000_000_000_000,
            "spendable_balance": 10_000_000_000_000,
        }
    }
    _DRIFT = [
        {
            "side": "cat",
            "tier": "mid",
            "ratio": 2.25,
            "coin_count": 4,
            "median_mojos": 225000,
            "live_size_mojos": 100000,
        }
    ]

    def test_returns_200_flat_mode(self):
        with (
            patch("wallet.get_spendable_coins_rpc", return_value=self._EMPTY_COINS),
            patch("wallet.get_wallet_balance", return_value=self._ZERO_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=false&trade_size=0.5"
                "&prepared_xch_size=0.5&prepared_cat_size=500&max_buy=10&max_sell=10",
                environ_base=self._LOOPBACK,
            )
        self.assertEqual(resp.status_code, 200)

    def test_flat_mode_response_has_required_keys(self):
        with (
            patch("wallet.get_spendable_coins_rpc", return_value=self._EMPTY_COINS),
            patch("wallet.get_wallet_balance", return_value=self._ZERO_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=false&trade_size=0.5"
                "&max_buy=10&max_sell=10",
                environ_base=self._LOOPBACK,
            )
        body = resp.get_json()
        for key in (
            "success",
            "tier_enabled",
            "all_sufficient",
            "xch_total",
            "cat_total",
            "balance_sufficient",
        ):
            self.assertIn(key, body)

    def test_flat_mode_tier_enabled_false(self):
        with (
            patch("wallet.get_spendable_coins_rpc", return_value=self._EMPTY_COINS),
            patch("wallet.get_wallet_balance", return_value=self._ZERO_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=false",
                environ_base=self._LOOPBACK,
            )
        self.assertFalse(resp.get_json().get("tier_enabled"))

    def test_empty_wallet_not_sufficient(self):
        with (
            patch("wallet.get_spendable_coins_rpc", return_value=self._EMPTY_COINS),
            patch("wallet.get_wallet_balance", return_value=self._ZERO_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=false&trade_size=0.5"
                "&max_buy=10&max_sell=10",
                environ_base=self._LOOPBACK,
            )
        body = resp.get_json()
        self.assertFalse(body["all_sufficient"])

    def test_tier_verify_drift_overrides_matching_wallet_counts(self):
        xch_coins = {
            "success": True,
            "records": [
                {"coin": {"amount": 1_000_000_000_000}},
                {"coin": {"amount": 1_000_000_000_000}},
            ],
        }
        cat_coins = {
            "success": True,
            "records": [
                {"coin": {"amount": 10_000}},
                {"coin": {"amount": 10_000}},
            ],
        }

        with (
            patch("wallet.get_spendable_coins_rpc", side_effect=[xch_coins, cat_coins]),
            patch("wallet.get_wallet_balance", return_value=self._ENOUGH_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
            patch(
                "coin_manager.check_tier_size_drift_standalone",
                return_value=self._DRIFT,
            ),
            patch.object(coin_prep_blueprint.cfg, "TIER_ENABLED", True),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=true"
                "&inner_xch=1&inner_cat=10&inner_count=2",
                environ_base=self._LOOPBACK,
            )

        body = resp.get_json()
        self.assertFalse(body["all_sufficient"])
        self.assertTrue(body["needs_coin_prep"])
        self.assertEqual(body["reason"], "tier_size_drift")
        self.assertEqual(body["tier_size_drift"], self._DRIFT)

    def test_tier_verify_sell_only_cat_pool_does_not_require_xch_pool(self):
        cat_coins = {
            "success": True,
            "records": [
                {"coin": {"amount": 10_000}},
                {"coin": {"amount": 10_000}},
            ],
        }

        with (
            patch(
                "wallet.get_spendable_coins_rpc",
                side_effect=[self._EMPTY_COINS, cat_coins],
            ),
            patch("wallet.get_wallet_balance", return_value=self._ENOUGH_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
            patch("coin_manager.check_tier_size_drift_standalone", return_value=[]),
            patch.object(coin_prep_blueprint.cfg, "TIER_ENABLED", True),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=true&liquidity_mode=sell_only"
                "&inner_cat=10&inner_count=2",
                environ_base=self._LOOPBACK,
            )

        body = resp.get_json()
        self.assertTrue(body["all_sufficient"])
        self.assertEqual(body["tiers"]["inner"]["xch_have"], 0)
        self.assertEqual(body["tiers"]["inner"]["cat_have"], 2)

    def test_tier_verify_sell_only_still_requires_fee_xch_pool(self):
        cat_coins = {
            "success": True,
            "records": [
                {"coin": {"amount": 10_000}},
            ],
        }

        with (
            patch(
                "wallet.get_spendable_coins_rpc",
                side_effect=[self._EMPTY_COINS, cat_coins],
            ),
            patch("wallet.get_wallet_balance", return_value=self._ENOUGH_BALANCE),
            patch("wallet.WALLET_ID_XCH", 1),
            patch("coin_manager.check_tier_size_drift_standalone", return_value=[]),
            patch.object(coin_prep_blueprint.cfg, "TIER_ENABLED", True),
        ):
            resp = self.client.get(
                "/api/coin-prep/verify?tier_enabled=true&liquidity_mode=sell_only"
                "&inner_cat=10&inner_count=1&fees_xch=0.0005&fees_count=1",
                environ_base=self._LOOPBACK,
            )

        body = resp.get_json()
        self.assertFalse(body["all_sufficient"])
        self.assertFalse(body["tiers"]["fees"]["sufficient"])
        self.assertEqual(body["tiers"]["fees"]["xch_have"], 0)
        self.assertGreater(body["xch_needed_mojos"], 0)


# ---------------------------------------------------------------------------
# 3. POST /api/coin-prep/trigger
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCoinPrepTrigger(_FlaskBase):
    _FAKE_SUMMARY = {
        "fills_cleared": 0,
        "round_trips_cleared": 0,
        "price_history_cleared": False,
        "inventory_cleared": False,
        "coins_cleared": 0,
        "open_offers_cancelled": 0,
        "reset_at": "2026-01-01T00:00:00",
        "preserve_history": True,
    }

    def test_requires_token(self):
        resp = self._post("/api/coin-prep/trigger", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200_immediately(self):
        # Trigger returns immediately; background thread spawns subprocess
        with (
            patch("threading.Thread") as mock_thread,
            patch.object(
                api_server, "_reset_fresh_run_session", return_value=self._FAKE_SUMMARY
            ),
            patch.object(api_server, "bot", None),
        ):
            mock_thread.return_value.start = MagicMock()
            resp = self._post("/api/coin-prep/trigger")
        self.assertEqual(resp.status_code, 200)

    def test_response_has_success_and_message(self):
        with (
            patch("threading.Thread") as mock_thread,
            patch.object(
                api_server, "_reset_fresh_run_session", return_value=self._FAKE_SUMMARY
            ),
            patch.object(api_server, "bot", None),
        ):
            mock_thread.return_value.start = MagicMock()
            resp = self._post("/api/coin-prep/trigger")
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertIn("message", body)

    def test_sets_coin_prep_state_running(self):
        with (
            patch("threading.Thread") as mock_thread,
            patch.object(
                api_server, "_reset_fresh_run_session", return_value=self._FAKE_SUMMARY
            ),
            patch.object(api_server, "bot", None),
        ):
            mock_thread.return_value.start = MagicMock()
            self._post("/api/coin-prep/trigger")
        # State is set to running before thread starts
        self.assertTrue(api_server._coin_prep_state.get("running"))

    def test_stops_bot_if_running(self):
        bot = MagicMock()
        bot.is_running.return_value = True
        with (
            patch("threading.Thread") as mock_thread,
            patch.object(
                api_server, "_reset_fresh_run_session", return_value=self._FAKE_SUMMARY
            ),
            patch.object(api_server, "bot", bot),
        ):
            mock_thread.return_value.start = MagicMock()
            self._post("/api/coin-prep/trigger")
        bot.stop.assert_called_once()

    def test_duplicate_trigger_does_not_start_second_worker(self):
        with (
            patch("threading.Thread") as mock_thread,
            patch.object(
                api_server, "_reset_fresh_run_session", return_value=self._FAKE_SUMMARY
            ),
            patch.object(api_server, "bot", None),
        ):
            mock_thread.return_value.start = MagicMock()
            first = self._post("/api/coin-prep/trigger")
            second = self._post("/api/coin-prep/trigger")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.get_json().get("success"))
        self.assertEqual(second.get_json().get("status"), "already_running")
        self.assertEqual(mock_thread.call_count, 1)


# ---------------------------------------------------------------------------
# 4. POST /api/coin-prep/reset
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCoinPrepReset(_FlaskBase):
    def test_requires_token(self):
        resp = self._post("/api/coin-prep/reset", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200(self):
        resp = self._post("/api/coin-prep/reset")
        self.assertEqual(resp.status_code, 200)

    def test_success_key_true(self):
        resp = self._post("/api/coin-prep/reset")
        self.assertTrue(resp.get_json().get("success"))

    def test_clears_running_state(self):
        api_server._coin_prep_state["running"] = True
        self._post("/api/coin-prep/reset")
        self.assertFalse(api_server._coin_prep_state["running"])

    def test_unsets_coin_manager_prep_flag_when_bot_set(self):
        bot = MagicMock()
        bot.coin_manager._prep_running = True
        with patch.object(api_server, "bot", bot):
            self._post("/api/coin-prep/reset")
        self.assertFalse(bot.coin_manager._prep_running)


if __name__ == "__main__":
    unittest.main()
