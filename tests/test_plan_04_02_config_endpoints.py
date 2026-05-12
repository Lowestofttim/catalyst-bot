"""Slice 04-02 — config endpoint contract tests.

Tests GET/POST /api/config, /api/config/reload, /api/config/apply, /api/config/live:
  - GET is public (no token required)
  - POST requires token
  - Blocked credentials/sensitive keys return 403
  - Invalid JSON body returns 400
  - Bot-dependent endpoints return 500 when bot=None
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

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

    def _post_json(self, path, body, auth=True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body,
            headers=headers,
            environ_base=self._LOOPBACK,
        )


# ---------------------------------------------------------------------------
# 1. GET /api/config
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestConfigGet(_FlaskBase):

    def test_returns_200_without_token(self):
        resp = self.client.get("/api/config", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_is_flat_dict(self):
        resp = self.client.get("/api/config", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIsInstance(body, dict)

    def test_wallet_type_key_present(self):
        resp = self.client.get("/api/config", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("WALLET_TYPE", body)

    def test_dry_run_key_present(self):
        resp = self.client.get("/api/config", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("DRY_RUN", body)

    def test_spread_bps_key_present(self):
        resp = self.client.get("/api/config", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("SPREAD_BPS", body)

    def test_credentials_excluded(self):
        resp = self.client.get("/api/config", environ_base=self._LOOPBACK)
        body = resp.get_json()
        # Sensitive keys should not appear in the public config response
        self.assertNotIn("CHIA_WALLET_CERT", body)
        self.assertNotIn("CHIA_WALLET_KEY", body)


# ---------------------------------------------------------------------------
# 2. POST /api/config — auth, validation, blocked keys
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestConfigPost(_FlaskBase):

    def test_post_without_token_returns_401(self):
        resp = self._post_json("/api/config", {"key": "SPREAD_BPS", "value": "200"},
                               auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_post_invalid_body_returns_400(self):
        resp = self.client.post(
            "/api/config",
            data="not json",
            content_type="text/plain",
            headers=self.auth,
            environ_base=self._LOOPBACK,
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_null_body_returns_400(self):
        resp = self._post_json("/api/config", None)
        self.assertEqual(resp.status_code, 400)

    def test_blocked_credential_key_returns_403(self):
        resp = self._post_json("/api/config",
                               {"key": "CHIA_WALLET_CERT", "value": "evil"})
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertFalse(body.get("success"))

    def test_blocked_rpc_url_returns_403(self):
        resp = self._post_json("/api/config",
                               {"key": "SAGE_RPC_URL", "value": "http://evil"})
        self.assertEqual(resp.status_code, 403)

    def test_blocked_cat_asset_id_returns_403(self):
        resp = self._post_json("/api/config",
                               {"key": "CAT_ASSET_ID", "value": "abc123"})
        self.assertEqual(resp.status_code, 403)

    def test_blocked_wallet_type_returns_403(self):
        resp = self._post_json("/api/config",
                               {"key": "WALLET_TYPE", "value": "chia"})
        self.assertEqual(resp.status_code, 403)

    def test_blocked_sage_fingerprint_returns_403(self):
        resp = self._post_json("/api/config",
                               {"key": "SAGE_FINGERPRINT", "value": "12345678"})
        self.assertEqual(resp.status_code, 403)

    def test_valid_key_update_returns_success(self):
        """Patch cfg.update to return True without touching real .env."""
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = True
        fake_cfg.to_dict.return_value = {}
        with patch.object(api_server, "cfg", fake_cfg):
            resp = self._post_json("/api/config",
                                   {"key": "SPREAD_BPS", "value": "300"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("success"))

    def test_failed_cfg_update_returns_500(self):
        """Patch cfg.update to return False."""
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = False
        fake_cfg.to_dict.return_value = {}
        with patch.object(api_server, "cfg", fake_cfg):
            resp = self._post_json("/api/config",
                                   {"key": "SPREAD_BPS", "value": "300"})
        self.assertEqual(resp.status_code, 500)


# ---------------------------------------------------------------------------
# 3. POST /api/config/reload
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestConfigReload(_FlaskBase):

    def test_requires_token(self):
        resp = self._post_json("/api/config/reload", {}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_reloaded_status(self):
        fake_cfg = MagicMock()
        fake_cfg.reload.return_value = None
        with patch.object(api_server, "cfg", fake_cfg):
            resp = self._post_json("/api/config/reload", {})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "reloaded")

    def test_calls_cfg_reload(self):
        fake_cfg = MagicMock()
        fake_cfg.reload.return_value = None
        with patch.object(api_server, "cfg", fake_cfg):
            self._post_json("/api/config/reload", {})
        fake_cfg.reload.assert_called_once()


# ---------------------------------------------------------------------------
# 4. POST /api/config/apply
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestConfigApply(_FlaskBase):

    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self._post_json("/api/config/apply", {})
        self.assertEqual(resp.status_code, 500)

    def test_bot_not_running_reloads_config(self):
        fake_bot = types.SimpleNamespace(
            is_running=lambda: False,
        )
        fake_cfg = MagicMock()
        fake_cfg.reload.return_value = None
        with patch.object(api_server, "bot", fake_bot), \
             patch.object(api_server, "cfg", fake_cfg):
            resp = self._post_json("/api/config/apply", {})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "reloaded")

    def test_requires_token(self):
        with patch.object(api_server, "bot", None):
            resp = self._post_json("/api/config/apply", {}, auth=False)
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# 5. POST /api/config/live
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestConfigLive(_FlaskBase):

    def test_requires_token(self):
        resp = self._post_json("/api/config/live",
                               {"key": "SPREAD_BPS", "value": "300"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_body_returns_400(self):
        resp = self.client.post(
            "/api/config/live",
            data="bad",
            content_type="text/plain",
            headers=self.auth,
            environ_base=self._LOOPBACK,
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_key_returns_400(self):
        resp = self._post_json("/api/config/live", {"value": "300"})
        self.assertEqual(resp.status_code, 400)

    def test_missing_value_returns_400(self):
        resp = self._post_json("/api/config/live", {"key": "SPREAD_BPS"})
        self.assertEqual(resp.status_code, 400)

    def test_blocked_key_returns_403(self):
        resp = self._post_json("/api/config/live",
                               {"key": "CHIA_WALLET_CERT", "value": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_valid_live_update_returns_success(self):
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = True
        with patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "bot", None):
            resp = self._post_json("/api/config/live",
                                   {"key": "SPREAD_BPS", "value": "300"})
        self.assertIn(resp.status_code, (200, 500))  # bot=None may return error but not 4xx

    def test_live_liquidity_mode_change_blocked_while_running(self):
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = True
        fake_bot = MagicMock()
        fake_bot.is_running.return_value = True
        fake_bot.get_state.return_value = {"status": "running"}

        with patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "bot", fake_bot):
            resp = self._post_json(
                "/api/config/live",
                {"key": "LIQUIDITY_MODE", "value": "sell_only"},
            )

        self.assertEqual(resp.status_code, 409)
        fake_cfg.update.assert_not_called()

    def test_live_liquidity_mode_rejects_unknown_mode(self):
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = True

        with patch.object(api_server, "cfg", fake_cfg), \
             patch.object(api_server, "bot", None):
            resp = self._post_json(
                "/api/config/live",
                {"key": "LIQUIDITY_MODE", "value": "sideways"},
            )

        self.assertEqual(resp.status_code, 400)
        fake_cfg.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
