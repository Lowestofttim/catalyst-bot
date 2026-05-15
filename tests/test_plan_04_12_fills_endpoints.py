"""Slice 04-12 — fills endpoint contract tests.

Tests GET /api/fills, GET /api/fills/classified, POST /api/fills/purge
(purge covered in 04-05; focus here on GET endpoints):
  - bot=None → 500 for bot-dependent reads
  - Response shape (fills list key)
  - Query parameter handling (limit, type/side filters)
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
    bot.is_running.return_value = True
    return bot


def _make_mock_conn(rows=None):
    """Mock DB connection for fills/classified queries."""
    mock_conn = MagicMock()

    def _execute(sql, *args, **kwargs):
        cur = MagicMock()
        cur.fetchone.return_value = (0,)  # COUNT(*) returns a tuple
        cur.fetchall.return_value = rows or []
        cur.__iter__ = MagicMock(return_value=iter(rows or []))
        return cur

    mock_conn.execute.side_effect = _execute
    return mock_conn


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()


# ---------------------------------------------------------------------------
# 1. GET /api/fills
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestFillsGet(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/fills", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_bot_set_returns_200(self):
        with (
            patch.object(api_server, "bot", _make_bot()),
            patch("database.get_fills", return_value=[]),
        ):
            resp = self.client.get("/api/fills", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_fills_key(self):
        with (
            patch.object(api_server, "bot", _make_bot()),
            patch("database.get_fills", return_value=[]),
        ):
            resp = self.client.get("/api/fills", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("fills", body)
        self.assertIsInstance(body["fills"], list)

    def test_default_limit_20(self):
        captured = {}

        def capture_get_fills(**kwargs):
            captured.update(kwargs)
            return []

        with (
            patch.object(api_server, "bot", _make_bot()),
            patch("database.get_fills", side_effect=capture_get_fills),
        ):
            self.client.get("/api/fills", environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("limit"), 20)

    def test_custom_limit_forwarded(self):
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return []

        with (
            patch.object(api_server, "bot", _make_bot()),
            patch("database.get_fills", side_effect=capture),
        ):
            self.client.get("/api/fills?limit=50", environ_base=self._LOOPBACK)
        self.assertEqual(captured.get("limit"), 50)


# ---------------------------------------------------------------------------
# 2. GET /api/fills/classified
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestFillsClassified(_FlaskBase):
    def test_returns_200(self):
        conn = _make_mock_conn()
        with patch("database.get_connection", return_value=conn):
            resp = self.client.get("/api/fills/classified", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_fills_key(self):
        conn = _make_mock_conn()
        with patch("database.get_connection", return_value=conn):
            resp = self.client.get("/api/fills/classified", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("fills", body)
        self.assertIsInstance(body["fills"], list)

    def test_response_has_pagination_metadata(self):
        conn = _make_mock_conn()
        with patch("database.get_connection", return_value=conn):
            resp = self.client.get("/api/fills/classified", environ_base=self._LOOPBACK)
        body = resp.get_json()
        for key in ("limit", "offset"):
            self.assertIn(key, body)

    def test_limit_capped_at_200(self):
        conn = _make_mock_conn()
        with patch("database.get_connection", return_value=conn):
            resp = self.client.get(
                "/api/fills/classified?limit=999", environ_base=self._LOOPBACK
            )
        body = resp.get_json()
        self.assertLessEqual(body.get("limit", 0), 200)

    def test_type_filter_does_not_crash(self):
        conn = _make_mock_conn()
        with patch("database.get_connection", return_value=conn):
            resp = self.client.get(
                "/api/fills/classified?type=retail", environ_base=self._LOOPBACK
            )
        self.assertEqual(resp.status_code, 200)

    def test_side_filter_buy_does_not_crash(self):
        conn = _make_mock_conn()
        with patch("database.get_connection", return_value=conn):
            resp = self.client.get(
                "/api/fills/classified?side=buy", environ_base=self._LOOPBACK
            )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
