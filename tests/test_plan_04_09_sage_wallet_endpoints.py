"""Slice 04-09 — sage/wallet endpoint contract tests.

Tests /api/wallet/sage-running, /api/wallet/begin-startup (POST),
/api/sage/startup-status, /api/sage/fingerprints,
/api/sage/start-with-fingerprint (POST), /api/wallets/detect,
/api/wallets/switch (POST):
  - Auth required for write endpoints
  - Response shapes and required keys
  - Input validation (invalid fingerprint, invalid wallet type)
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
# 1. GET /api/wallet/sage-running
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestWalletSageRunning(_FlaskBase):

    def test_returns_200(self):
        with patch("sage_node._is_sage_rpc_available", return_value=False):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_running_key(self):
        with patch("sage_node._is_sage_rpc_available", return_value=False):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertIn("running", resp.get_json())

    def test_running_true_when_available(self):
        with patch("sage_node._is_sage_rpc_available", return_value=True):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertTrue(resp.get_json()["running"])

    def test_running_false_when_unavailable(self):
        with patch("sage_node._is_sage_rpc_available", return_value=False):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertFalse(resp.get_json()["running"])


# ---------------------------------------------------------------------------
# 2. POST /api/wallet/begin-startup
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestWalletBeginStartup(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/wallet/begin-startup", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_200(self):
        with patch("chia_node.set_auto_launch"), \
             patch("chia_node.start_preload"):
            resp = self._post("/api/wallet/begin-startup")
        self.assertEqual(resp.status_code, 200)

    def test_response_has_started_key(self):
        with patch("chia_node.set_auto_launch"), \
             patch("chia_node.start_preload"):
            resp = self._post("/api/wallet/begin-startup")
        self.assertTrue(resp.get_json().get("started"))

    def test_start_preload_is_called(self):
        with patch("chia_node.set_auto_launch"), \
             patch("chia_node.start_preload") as mock_preload:
            self._post("/api/wallet/begin-startup")
        mock_preload.assert_called_once()


# ---------------------------------------------------------------------------
# 2b. POST /api/sage/daemon/start
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageDaemonStart(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/sage/daemon/start",
                          {"services": "all"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_returns_start_chia_result(self):
        result = {"success": True, "message": "Sage wallet runs independently"}
        with patch("sage_node.start_chia", return_value=result) as mock_start:
            resp = self._post("/api/sage/daemon/start", {"services": "all"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), result)
        mock_start.assert_called_once_with("all")

    def test_defaults_services_to_all(self):
        with patch("sage_node.start_chia",
                   return_value={"success": True}) as mock_start:
            self._post("/api/sage/daemon/start")

        mock_start.assert_called_once_with("all")


# ---------------------------------------------------------------------------
# 3. GET /api/sage/startup-status
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageStartupStatus(_FlaskBase):

    def test_returns_200(self):
        with patch("chia_node.get_startup_status", return_value={"phase": "idle"}):
            resp = self.client.get("/api/sage/startup-status",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_is_dict(self):
        with patch("chia_node.get_startup_status",
                   return_value={"phase": "idle", "message": "waiting"}):
            resp = self.client.get("/api/sage/startup-status",
                                   environ_base=self._LOOPBACK)
        self.assertIsInstance(resp.get_json(), dict)


# ---------------------------------------------------------------------------
# 4. GET /api/sage/fingerprints
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageFingerprints(_FlaskBase):

    def test_returns_200(self):
        with patch("chia_node.get_available_fingerprints", return_value=[]):
            resp = self.client.get("/api/sage/fingerprints",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_fingerprints_list(self):
        with patch("chia_node.get_available_fingerprints",
                   return_value=["12345678"]):
            resp = self.client.get("/api/sage/fingerprints",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertIsInstance(body.get("fingerprints"), list)


# ---------------------------------------------------------------------------
# 5. POST /api/sage/start-with-fingerprint
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageStartWithFingerprint(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/sage/start-with-fingerprint",
                          {"fingerprint": "12345678"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_body_returns_400(self):
        with patch("chia_node.trigger_start", return_value={"success": True}):
            resp = self.client.post(
                "/api/sage/start-with-fingerprint",
                data="not json",
                content_type="text/plain",
                headers=self.auth,
                environ_base=self._LOOPBACK,
            )
        self.assertEqual(resp.status_code, 400)

    def test_empty_fingerprint_returns_400(self):
        with patch("chia_node.trigger_start", return_value={"success": True}):
            resp = self._post("/api/sage/start-with-fingerprint",
                              {"fingerprint": ""})
        self.assertEqual(resp.status_code, 400)

    def test_non_digit_fingerprint_returns_400(self):
        with patch("chia_node.trigger_start", return_value={"success": True}):
            resp = self._post("/api/sage/start-with-fingerprint",
                              {"fingerprint": "abc"})
        self.assertEqual(resp.status_code, 400)

    def test_valid_fingerprint_calls_trigger_start(self):
        with patch("chia_node.trigger_start",
                   return_value={"success": True}) as mock_trigger:
            self._post("/api/sage/start-with-fingerprint",
                       {"fingerprint": "12345678"})
        mock_trigger.assert_called_once_with("12345678")


# ---------------------------------------------------------------------------
# 6. GET /api/wallets/detect
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestWalletsDetect(_FlaskBase):

    def test_returns_200(self):
        with patch("wallet_chia.rpc", side_effect=Exception("not available")):
            resp = self.client.get("/api/wallets/detect",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_success_and_detected(self):
        with patch("wallet_chia.rpc", side_effect=Exception("not available")):
            resp = self.client.get("/api/wallets/detect",
                                   environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertIsInstance(body.get("detected"), list)

    def test_response_has_current_wallet_type(self):
        with patch("wallet_chia.rpc", side_effect=Exception("not available")):
            resp = self.client.get("/api/wallets/detect",
                                   environ_base=self._LOOPBACK)
        self.assertIn("current", resp.get_json())


# ---------------------------------------------------------------------------
# 7. POST /api/wallets/switch
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestWalletsSwitch(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/wallets/switch",
                          {"wallet_type": "chia"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_wallet_type_returns_error(self):
        resp = self._post("/api/wallets/switch", {"wallet_type": "bitcoin"})
        body = resp.get_json()
        self.assertFalse(body.get("success"))

    def test_invalid_body_returns_400(self):
        resp = self.client.post(
            "/api/wallets/switch",
            data="not json",
            content_type="text/plain",
            headers=self.auth,
            environ_base=self._LOOPBACK,
        )
        self.assertEqual(resp.status_code, 400)

    def test_valid_chia_switch_returns_success(self):
        with patch("dotenv.set_key"), \
             patch("api_server.log_event"):
            resp = self._post("/api/wallets/switch", {"wallet_type": "chia"})
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertTrue(body.get("restart_required"))

    def test_valid_sage_switch_returns_success(self):
        with patch("dotenv.set_key"), \
             patch("api_server.log_event"):
            resp = self._post("/api/wallets/switch", {"wallet_type": "sage"})
        self.assertTrue(resp.get_json().get("success"))


if __name__ == "__main__":
    unittest.main()
