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
import tempfile
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
        with patch("sage_node._is_sage_rpc_available", return_value=False), \
             patch("sage_node._is_sage_rpc_port_listening", return_value=False):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_running_key(self):
        with patch("sage_node._is_sage_rpc_available", return_value=False), \
             patch("sage_node._is_sage_rpc_port_listening", return_value=False):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertIn("running", resp.get_json())

    def test_running_true_when_available(self):
        with patch("sage_node._is_sage_rpc_available", return_value=True):
            resp = self.client.get("/api/wallet/sage-running",
                                   environ_base=self._LOOPBACK)
        self.assertTrue(resp.get_json()["running"])

    def test_running_false_when_unavailable(self):
        with patch("sage_node._is_sage_rpc_available", return_value=False), \
             patch("sage_node._is_sage_rpc_port_listening", return_value=False):
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
# 5a. POST /api/sage/fingerprint
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageFingerprintPersistence(_FlaskBase):

    def test_requires_token(self):
        resp = self._post("/api/sage/fingerprint",
                          {"fingerprint": "12345678"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_invalid_body_returns_400(self):
        resp = self.client.post(
            "/api/sage/fingerprint",
            data="not json",
            content_type="text/plain",
            headers=self.auth,
            environ_base=self._LOOPBACK,
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_digit_fingerprint_returns_400(self):
        resp = self._post("/api/sage/fingerprint",
                          {"fingerprint": "abc"})
        self.assertEqual(resp.status_code, 400)

    def test_rejects_change_while_bot_running(self):
        fake_bot = MagicMock()
        fake_bot.is_running.return_value = True
        with patch.object(api_server, "bot", fake_bot), \
             patch("chia_node.trigger_start", return_value={"success": True}):
            resp = self._post("/api/sage/fingerprint",
                              {"fingerprint": "12345678"})

        body = resp.get_json()
        self.assertEqual(resp.status_code, 409, body)
        self.assertFalse(body.get("success"))

    def test_valid_fingerprint_persists_and_triggers_start(self):
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = True
        fake_cfg.SAGE_FINGERPRINT = ""
        with patch.object(api_server, "bot", None), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch("chia_node.trigger_start",
                   return_value={"success": True}) as mock_trigger:
            resp = self._post("/api/sage/fingerprint",
                              {"fingerprint": "12345678"})

        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("fingerprint"), "12345678")
        fake_cfg.update.assert_called_once_with(
            "SAGE_FINGERPRINT",
            "12345678",
            source="sage_wallet_settings",
            note="User selected Sage wallet fingerprint",
        )
        mock_trigger.assert_called_once_with("12345678")

    def test_failed_start_does_not_persist_fingerprint(self):
        fake_cfg = MagicMock()
        fake_cfg.update.return_value = True
        with patch.object(api_server, "bot", None), \
             patch.object(api_server, "cfg", fake_cfg), \
             patch("chia_node.trigger_start",
                   return_value={"success": False, "error": "unsupported"}) as mock_trigger:
            resp = self._post("/api/sage/fingerprint",
                              {"fingerprint": "12345678"})

        body = resp.get_json()
        self.assertEqual(resp.status_code, 400, body)
        self.assertFalse(body.get("success"))
        fake_cfg.update.assert_not_called()
        mock_trigger.assert_called_once_with("12345678")


# ---------------------------------------------------------------------------
# 5b. POST /api/sage/setup-certs
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageSetupCerts(_FlaskBase):

    def _write_sage_cert_pair(self, data_dir):
        ssl_dir = os.path.join(data_dir, "ssl")
        os.makedirs(ssl_dir, exist_ok=True)
        cert_path = os.path.join(ssl_dir, "wallet.crt")
        key_path = os.path.join(ssl_dir, "wallet.key")
        with open(cert_path, "w", encoding="utf-8") as f:
            f.write("test certificate")
        with open(key_path, "w", encoding="utf-8") as f:
            f.write("test key")
        return cert_path, key_path

    def test_auto_detect_finds_localappdata_sage_cert_pair(self):
        with tempfile.TemporaryDirectory() as appdata, \
             tempfile.TemporaryDirectory() as localappdata:
            sage_data_dir = os.path.join(localappdata, "com.rigidnetwork.sage")
            cert_path, _ = self._write_sage_cert_pair(sage_data_dir)
            env = {
                "APPDATA": appdata,
                "LOCALAPPDATA": localappdata,
                "SAGE_CERT_PATH": "",
                "SAGE_KEY_PATH": "",
                "SAGE_DATA_DIR": "",
                "SAGE_HOME": "",
                "SAGE_ALLOWED_CERT_ROOTS": "",
            }
            with patch("platform.system", return_value="Windows"), \
                 patch.dict(os.environ, env, clear=False):
                resp = self._post("/api/sage/setup-certs", {})

        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body.get("success"))
        self.assertIn("saved", body.get("message", "").lower())
        self.assertIn(os.path.normpath(cert_path), body.get("cert_path", ""))

    def test_manual_custom_sage_data_dir_cert_pair_is_accepted(self):
        with tempfile.TemporaryDirectory() as appdata, \
             tempfile.TemporaryDirectory() as custom_root:
            cert_path, _ = self._write_sage_cert_pair(
                os.path.join(custom_root, "PortableSage")
            )
            env = {
                "APPDATA": appdata,
                "LOCALAPPDATA": os.path.join(appdata, "Local"),
                "SAGE_CERT_PATH": "",
                "SAGE_KEY_PATH": "",
                "SAGE_DATA_DIR": "",
                "SAGE_HOME": "",
                "SAGE_ALLOWED_CERT_ROOTS": "",
            }
            with patch.dict(os.environ, env, clear=False):
                resp = self._post("/api/sage/setup-certs", {"cert_path": cert_path})

        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body.get("success"))

    def test_manual_cert_rejects_non_wallet_cert_name(self):
        with tempfile.TemporaryDirectory() as appdata, \
             tempfile.TemporaryDirectory() as custom_root:
            cert_path = os.path.join(custom_root, "ssl", "client.crt")
            key_path = os.path.join(custom_root, "ssl", "client.key")
            os.makedirs(os.path.dirname(cert_path), exist_ok=True)
            with open(cert_path, "w", encoding="utf-8") as f:
                f.write("test certificate")
            with open(key_path, "w", encoding="utf-8") as f:
                f.write("test key")
            env = {
                "APPDATA": appdata,
                "LOCALAPPDATA": os.path.join(appdata, "Local"),
                "SAGE_CERT_PATH": "",
                "SAGE_KEY_PATH": "",
                "SAGE_HOME": "",
                "SAGE_ALLOWED_CERT_ROOTS": "",
            }
            with patch.dict(os.environ, env, clear=False):
                resp = self._post(
                    "/api/sage/setup-certs",
                    {"cert_path": cert_path, "key_path": key_path},
                )

        body = resp.get_json()
        self.assertEqual(resp.status_code, 400, body)
        self.assertIn("wallet.crt", body.get("error", ""))


# ---------------------------------------------------------------------------
# 5c. GET /api/sage/cert-candidates
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSageCertCandidates(_FlaskBase):

    def test_returns_sage_wallet_crt_candidates(self):
        with patch("sage_node.get_sage_cert_candidates",
                   return_value=["C:\\Users\\Tester\\AppData\\Roaming\\com.rigidnetwork.sage\\ssl\\wallet.crt"]), \
             patch("sage_node.detect_sage_cert_path", return_value=None):
            resp = self.client.get("/api/sage/cert-candidates",
                                   environ_base=self._LOOPBACK)

        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body.get("success"))
        self.assertEqual(
            body.get("candidates"),
            ["C:\\Users\\Tester\\AppData\\Roaming\\com.rigidnetwork.sage\\ssl\\wallet.crt"],
        )
        self.assertEqual(
            body.get("suggested_cert_path"),
            "C:\\Users\\Tester\\AppData\\Roaming\\com.rigidnetwork.sage\\ssl\\wallet.crt",
        )

    def test_candidate_helper_uses_default_sage_ssl_wallet_crt_shape(self):
        with tempfile.TemporaryDirectory() as appdata, \
             tempfile.TemporaryDirectory() as localappdata:
            env = {
                "APPDATA": appdata,
                "LOCALAPPDATA": localappdata,
                "USERPROFILE": os.path.join(appdata, "Profile"),
                "SAGE_DATA_DIR": "",
                "SAGE_HOME": "",
                "SAGE_ALLOWED_CERT_ROOTS": "",
            }
            with patch("platform.system", return_value="Windows"), \
                 patch.dict(os.environ, env, clear=False):
                import sage_node
                candidates = sage_node.get_sage_cert_candidates()

        self.assertGreaterEqual(len(candidates), 2)
        self.assertTrue(all(path.endswith(os.path.join("ssl", "wallet.crt"))
                            for path in candidates))
        self.assertTrue(any("com.rigidnetwork.sage" in path for path in candidates))


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
