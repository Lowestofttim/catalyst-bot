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


if __name__ == "__main__":
    unittest.main()
