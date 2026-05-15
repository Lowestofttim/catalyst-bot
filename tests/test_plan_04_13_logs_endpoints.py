"""Slice 04-13 — logs endpoint contract tests.

Tests GET /api/logs, POST /api/logs/clear, GET /api/logs/download:
  - No auth required for reads
  - Response shapes and required keys
  - logs/clear sets _logs_cleared_at + success key
  - download returns a zip file (Content-Type check)
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
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

    def _post(self, path, body=None, auth=True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body or {},
            headers=headers,
            environ_base=self._LOOPBACK,
        )


# ---------------------------------------------------------------------------
# 1. GET /api/logs
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestLogsGet(_FlaskBase):

    def test_returns_200(self):
        with patch("database.get_events_since", return_value=[]), \
             patch("database.get_recent_events", return_value=[]):
            resp = self.client.get("/api/logs", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_logs_key(self):
        with patch("database.get_events_since", return_value=[]), \
             patch("database.get_recent_events", return_value=[]):
            resp = self.client.get("/api/logs", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("logs", body)
        self.assertIsInstance(body["logs"], list)

    def test_limit_parameter_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return []

        with patch("database.get_recent_events", side_effect=capture), \
             patch("database.get_events_since", side_effect=capture):
            self.client.get("/api/logs?limit=100", environ_base=self._LOOPBACK)
        # Either endpoint is called with limit=100
        self.assertEqual(captured.get("limit"), 100)

    def test_category_filter_accepted(self):
        with patch("database.get_events_since", return_value=[]), \
             patch("database.get_recent_events", return_value=[]):
            resp = self.client.get("/api/logs?category=offer",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 2. POST /api/logs/clear
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestLogsClear(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/logs/clear", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200(self):
        with patch("database.set_setting"):
            resp = self._post("/api/logs/clear")
        self.assertEqual(resp.status_code, 200)

    def test_success_key_true(self):
        with patch("database.set_setting"):
            resp = self._post("/api/logs/clear")
        self.assertTrue(resp.get_json().get("success"))

    def test_sets_logs_cleared_at(self):
        original = api_server._logs_cleared_at
        with patch("database.set_setting"):
            self._post("/api/logs/clear")
        self.assertIsNotNone(api_server._logs_cleared_at)
        self.assertNotEqual(api_server._logs_cleared_at, original)


# ---------------------------------------------------------------------------
# 3. GET /api/logs/download
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestLogsDownload(_FlaskBase):

    def test_returns_200(self):
        with patch("database.get_recent_events", return_value=[]), \
             patch("super_log.get_archive_summary", return_value=[]), \
             patch("super_log.get_log_path", return_value=None), \
             patch("super_log.get_log_stats", return_value={}):
            resp = self.client.get("/api/logs/download",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_is_zip(self):
        with patch("database.get_recent_events", return_value=[]), \
             patch("super_log.get_archive_summary", return_value=[]), \
             patch("super_log.get_log_path", return_value=None), \
             patch("super_log.get_log_stats", return_value={}):
            resp = self.client.get("/api/logs/download",
                                   environ_base=self._LOOPBACK)
        content_type = resp.content_type or ""
        self.assertIn("zip", content_type.lower())

    def test_bundle_includes_market_toxicity_snapshot(self):
        toxicity = {
            "score": 82,
            "level": "high",
            "buy_spread_multiplier": "1.75",
            "throttled_sides": ["buy"],
            "reasons": [{"key": "fast_fills", "detail": "buy fills clustered"}],
        }
        risk_manager = MagicMock()
        risk_manager.get_market_toxicity.return_value = toxicity
        bot = MagicMock()
        bot.risk_manager = risk_manager
        bot.is_running.return_value = True
        bot.coin_manager = None
        bot._recovery_state = {}
        bot._probe_state = {}
        bot.get_price_info.return_value = {}
        bot.sniper = None
        bot.market_intel.get_market_summary.return_value = {}
        bot.runtime_monitor.get_state.return_value = {}
        bot.splash_manager.get_stats.return_value = {}
        bot.splash_node.get_status.return_value = {}
        bot.get_splash_receive_stats.return_value = {}

        with patch.object(api_server, "bot", bot), \
             patch("database.get_recent_events", return_value=[]), \
             patch("database.get_open_offers", return_value=[]), \
             patch("database.get_fills", return_value=[]), \
             patch("database.get_live_tier_group_counts", return_value={}), \
             patch("database.get_coin_summary", return_value={}), \
             patch("super_log.get_archive_summary", return_value=[]), \
             patch("super_log.get_log_path", return_value=None), \
             patch("super_log.get_log_stats", return_value={}):
            resp = self.client.get("/api/logs/download",
                                   environ_base=self._LOOPBACK)

        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            self.assertIn("snapshots/market_toxicity.json", zf.namelist())
            snapshot = json.loads(zf.read("snapshots/market_toxicity.json"))
            readme = zf.read("README.txt").decode("utf-8", errors="replace")

        self.assertEqual(snapshot["score"], 82)
        self.assertEqual(snapshot["level"], "high")
        self.assertEqual(snapshot["throttled_sides"], ["buy"])
        self.assertIn("market_toxicity.json", readme)

    def test_bundle_includes_sanitized_settings_audit_context(self):
        config_history = [
            {
                "id": 7,
                "timestamp": "2026-05-15T07:00:00Z",
                "key": "BASE_SPREAD_BPS",
                "old_value": "500",
                "new_value": "900",
                "source": "api_settings_save",
                "note": "tester changed spread",
            },
            {
                "id": 8,
                "timestamp": "2026-05-15T07:01:00Z",
                "key": "SPACESCAN_API_KEY",
                "old_value": "old-secret-value",
                "new_value": "new-secret-value",
                "source": "api_settings_save",
                "note": "",
            },
        ]
        bot_settings = [
            {
                "key": "last_selected_liquidity_mode",
                "value": "sell_only",
                "updated_at": "2026-05-15T07:02:00Z",
            },
            {
                "key": "api_token",
                "value": "bot-setting-secret",
                "updated_at": "2026-05-15T07:03:00Z",
            },
        ]

        with patch.object(api_server, "bot", None), \
             patch("database.get_recent_events", return_value=[]), \
             patch("database.get_open_offers", return_value=[]), \
             patch("database.get_fills", return_value=[]), \
             patch("database.get_live_tier_group_counts", return_value={}), \
             patch("database.get_coin_summary", return_value={}), \
             patch("database.get_config_history", return_value=config_history), \
             patch("database.get_all_settings", return_value=bot_settings, create=True), \
             patch("super_log.get_archive_summary", return_value=[]), \
             patch("super_log.get_log_path", return_value=None), \
             patch("super_log.get_log_stats", return_value={}):
            resp = self.client.get("/api/logs/download",
                                   environ_base=self._LOOPBACK)

        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            self.assertIn("snapshots/config_history.json", zf.namelist())
            self.assertIn("snapshots/bot_settings.json", zf.namelist())
            history = json.loads(zf.read("snapshots/config_history.json"))
            settings = json.loads(zf.read("snapshots/bot_settings.json"))
            bundle_text = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in zf.namelist()
            )

        self.assertEqual(history[0]["key"], "BASE_SPREAD_BPS")
        self.assertEqual(history[0]["new_value"], "900")
        self.assertEqual(history[1]["old_value"], "<secret-redacted>")
        self.assertEqual(history[1]["new_value"], "<secret-redacted>")
        self.assertEqual(settings[0]["key"], "last_selected_liquidity_mode")
        self.assertEqual(settings[0]["value"], "sell_only")
        self.assertEqual(settings[1]["value"], "<secret-redacted>")
        self.assertNotIn("old-secret-value", bundle_text)
        self.assertNotIn("new-secret-value", bundle_text)
        self.assertNotIn("bot-setting-secret", bundle_text)

    def test_bundle_redacts_wallet_identifiers_and_excludes_config_secrets(self):
        sensitive_address = "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
        event = {
            "timestamp": "2026-05-10T12:00:00Z",
            "severity": "info",
            "event_type": "wallet",
            "message": (
                f"paid {sensitive_address} with fingerprint: 1234567890"
            ),
            "details": {
                "recipient": sensitive_address,
                "sage": "fingerprint=1234567890",
                "SPACESCAN_API_KEY": "sk_live_secret_123456",
                "auth_token": "token_secret_123456",
                "Authorization": "Bearer structured_bearer_secret_123456",
                "headers": {"X-API-Key": "header_api_key_secret_123456"},
                "password": "password_secret_123456",
                "trade_id": "trade-public-id",
            },
        }

        with patch.object(api_server.cfg, "FULL_NODE_CERT_PATH", "C:/secret/full_node.crt"), \
             patch.object(api_server.cfg, "FULL_NODE_KEY_PATH", "C:/secret/full_node.key"), \
             patch.object(api_server.cfg, "SPACESCAN_API_KEY", "spacescan-secret"), \
             patch("database.get_recent_events", return_value=[event]), \
             patch("database.get_open_offers", return_value=[]), \
             patch("database.get_fills", return_value=[]), \
             patch("database.get_live_tier_group_counts", return_value={}), \
             patch("database.get_coin_summary", return_value={}), \
             patch("super_log.get_archive_summary", return_value=[]), \
             patch("super_log.get_log_path", return_value=None), \
             patch("super_log.get_log_stats", return_value={}):
            resp = self.client.get("/api/logs/download",
                                   environ_base=self._LOOPBACK)

        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            bundle_text = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in zf.namelist()
            )
            config = json.loads(zf.read("snapshots/config.json"))

        self.assertNotIn(sensitive_address, bundle_text)
        self.assertNotIn("1234567890", bundle_text)
        self.assertIn("xch1<redacted>", bundle_text)
        self.assertIn("fingerprint: <redacted>", bundle_text)
        self.assertEqual(config.get("FULL_NODE_CERT_PATH"), None)
        self.assertEqual(config.get("FULL_NODE_KEY_PATH"), None)
        self.assertNotIn("spacescan-secret", bundle_text)
        self.assertNotIn("sk_live_secret_123456", bundle_text)
        self.assertNotIn("token_secret_123456", bundle_text)
        self.assertNotIn("structured_bearer_secret_123456", bundle_text)
        self.assertNotIn("header_api_key_secret_123456", bundle_text)
        self.assertNotIn("password_secret_123456", bundle_text)

    def test_bundle_redacts_tls_paths_from_log_tails(self):
        sage_cert = r"C:\Users\Alice\AppData\Roaming\Sage\mainnet\ssl\wallet.crt"
        sage_key = r"C:\Users\Alice\AppData\Roaming\Sage\mainnet\ssl\wallet.key"
        full_node_cert = (
            r"C:\Users\Alice\.chia\mainnet\config\ssl\full_node"
            r"\private_full_node.crt"
        )
        user_log_path = r"C:\Users\Alice\AppData\Roaming\Catalyst\bot_superlog.log"

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "bot_superlog_20260510.log")
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "Passing configured Sage RPC certificate to coin prep "
                    f"worker: {sage_cert}\n"
                    f"Sage key path: {sage_key}\n"
                    f"Full node cert: {full_node_cert}\n"
                    "SPACESCAN_API_KEY=sk_log_secret_123456\n"
                    "Authorization: Bearer bearer_secret_123456\n"
                    "seed phrase: abandon abandon abandon abandon\n"
                    f"Logging to {user_log_path}\n"
                )

            with patch("database.get_recent_events", return_value=[]), \
                 patch("database.get_open_offers", return_value=[]), \
                 patch("database.get_fills", return_value=[]), \
                 patch("database.get_live_tier_group_counts", return_value={}), \
                 patch("database.get_coin_summary", return_value={}), \
                 patch("super_log.get_archive_summary", return_value=[]), \
                 patch("super_log.get_log_path", return_value=log_path), \
                 patch("super_log.get_log_stats", return_value={}):
                resp = self.client.get("/api/logs/download",
                                       environ_base=self._LOOPBACK)

        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            bundle_text = "\n".join(
                zf.read(name).decode("utf-8", errors="replace")
                for name in zf.namelist()
            )

        self.assertNotIn(sage_cert, bundle_text)
        self.assertNotIn(sage_key, bundle_text)
        self.assertNotIn(full_node_cert, bundle_text)
        self.assertNotIn("sk_log_secret_123456", bundle_text)
        self.assertNotIn("bearer_secret_123456", bundle_text)
        self.assertNotIn("abandon abandon", bundle_text)
        self.assertNotIn(r"C:\Users\Alice", bundle_text)
        self.assertIn("<tls-path-redacted>", bundle_text)
        self.assertIn("<user-home>", bundle_text)


if __name__ == "__main__":
    unittest.main()
