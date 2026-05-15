"""Slice 04-11 — trading-pair endpoint contract tests.

Tests GET /api/cats, POST /api/cat/select, POST /api/cat/refresh:
  - Auth required for write endpoints
  - Input validation (asset_id format, name/ticker length, decimals range)
  - bot-running gate on cat/select (409)
  - Response shapes
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

# Valid 64-hex asset_id for test use
_VALID_ASSET_ID = "a" * 64
_VALID_BODY = {
    "asset_id": _VALID_ASSET_ID,
    "wallet_id": 2,
    "name": "TestCAT",
    "decimals": 3,
}


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
# 1. GET /api/cats
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCatsGet(_FlaskBase):
    def test_returns_200(self):
        with (
            patch("wallet.get_wallets", return_value={"success": True, "wallets": []}),
            patch("wallet.get_wallet_type", return_value="sage"),
        ):
            resp = self.client.get("/api/cats", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_cats_list(self):
        with (
            patch("wallet.get_wallets", return_value={"success": True, "wallets": []}),
            patch("wallet.get_wallet_type", return_value="sage"),
        ):
            resp = self.client.get("/api/cats", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("cats", body)
        self.assertIsInstance(body["cats"], list)

    def test_no_wallet_cats_returns_list(self):
        # With no wallet CATs, list may still contain the active CAT
        # (pre-populated from _active_cat / .env). Just verify it's a list.
        with (
            patch("wallet.get_wallets", return_value={"success": True, "wallets": []}),
            patch("wallet.get_wallet_type", return_value="sage"),
        ):
            resp = self.client.get("/api/cats", environ_base=self._LOOPBACK)
        self.assertIsInstance(resp.get_json()["cats"], list)


# ---------------------------------------------------------------------------
# 2. POST /api/cat/select
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCatSelect(_FlaskBase):
    def _mocked_post(self, body):
        with (
            patch.object(api_server.cfg, "update"),
            patch("threading.Thread") as mt,
            patch("wallet_sage.notify_cat_asset_id_changed"),
            patch.object(api_server, "bot", None),
        ):
            mt.return_value.start = MagicMock()
            return self._post("/api/cat/select", body)

    def test_requires_token(self):
        resp = self._post("/api/cat/select", _VALID_BODY, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_body_returns_error(self):
        # Flask returns 415 Unsupported Media Type for non-JSON content type
        # (newer Flask), or 400 if get_json() returns None and the route checks it
        resp = self.client.post(
            "/api/cat/select",
            data="not json",
            content_type="text/plain",
            headers=self.auth,
            environ_base=self._LOOPBACK,
        )
        self.assertIn(resp.status_code, (400, 415))

    def test_asset_id_too_short_returns_400(self):
        body = {**_VALID_BODY, "asset_id": "abc123"}
        resp = self._mocked_post(body)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json().get("success"))

    def test_asset_id_non_hex_returns_400(self):
        body = {**_VALID_BODY, "asset_id": "z" * 64}
        resp = self._mocked_post(body)
        self.assertEqual(resp.status_code, 400)

    def test_name_too_long_returns_400(self):
        body = {**_VALID_BODY, "name": "x" * 129}
        resp = self._mocked_post(body)
        self.assertEqual(resp.status_code, 400)

    def test_ticker_too_long_returns_400(self):
        body = {**_VALID_BODY, "ticker_id": "T" * 65}
        resp = self._mocked_post(body)
        self.assertEqual(resp.status_code, 400)

    def test_invalid_decimals_returns_400(self):
        body = {**_VALID_BODY, "decimals": 99}
        resp = self._mocked_post(body)
        self.assertEqual(resp.status_code, 400)

    def test_negative_decimals_returns_400(self):
        body = {**_VALID_BODY, "decimals": -1}
        resp = self._mocked_post(body)
        self.assertEqual(resp.status_code, 400)

    def test_bot_running_returns_409(self):
        running_bot = MagicMock()
        running_bot.is_running.return_value = True
        with patch.object(api_server, "bot", running_bot):
            resp = self._post("/api/cat/select", _VALID_BODY)
        self.assertEqual(resp.status_code, 409)

    def test_valid_body_returns_200(self):
        resp = self._mocked_post(_VALID_BODY)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("success"))

    def test_response_echoes_asset_id(self):
        resp = self._mocked_post(_VALID_BODY)
        body = resp.get_json()
        self.assertEqual(body.get("asset_id"), _VALID_ASSET_ID)


# ---------------------------------------------------------------------------
# 3. POST /api/cat/refresh
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCatRefresh(_FlaskBase):
    def test_requires_token(self):
        resp = self._post("/api/cat/refresh", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200(self):
        with patch.object(api_server.cfg, "reload"):
            resp = self._post("/api/cat/refresh")
        self.assertEqual(resp.status_code, 200)

    def test_success_key_true(self):
        with patch.object(api_server.cfg, "reload"):
            resp = self._post("/api/cat/refresh")
        self.assertTrue(resp.get_json().get("success"))

    def test_cfg_reload_is_called(self):
        with patch.object(api_server.cfg, "reload") as mock_reload:
            self._post("/api/cat/refresh")
        mock_reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
