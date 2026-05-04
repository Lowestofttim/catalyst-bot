import os
import tempfile
import unittest
from unittest.mock import patch

try:
    import wallet_sage
    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    wallet_sage = None
    _IMPORT_ERROR = exc


@unittest.skipIf(wallet_sage is None, f"wallet_sage import unavailable: {_IMPORT_ERROR}")
class TestWalletSageStartupReadiness(unittest.TestCase):
    def test_reload_connection_settings_picks_up_sage_cert_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cert_path = os.path.join(temp_dir, "wallet.crt")
            key_path = os.path.join(temp_dir, "wallet.key")
            with open(cert_path, "w", encoding="utf-8") as f:
                f.write("test certificate")
            with open(key_path, "w", encoding="utf-8") as f:
                f.write("test key")
            env_path = os.path.join(temp_dir, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("SAGE_RPC_URL=https://127.0.0.1:9257\n")
                f.write(f"SAGE_CERT_PATH={cert_path}\n")
                f.write(f"SAGE_KEY_PATH={key_path}\n")

            old_cert = wallet_sage.CERT_PATH
            old_key = wallet_sage.KEY_PATH
            old_url = wallet_sage.WALLET_URL
            old_host = wallet_sage._SAGE_HOST
            old_port = wallet_sage._SAGE_PORT
            try:
                with patch.object(wallet_sage, "_env_file", return_value=env_path), \
                     patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("SAGE_RPC_URL", None)
                    os.environ.pop("SAGE_CERT_PATH", None)
                    os.environ.pop("SAGE_KEY_PATH", None)
                    wallet_sage.reload_connection_settings()
                    self.assertEqual(wallet_sage.CERT_PATH, cert_path)
                    self.assertEqual(wallet_sage.KEY_PATH, key_path)
                    self.assertEqual(wallet_sage._SAGE_HOST, "127.0.0.1")
                    self.assertEqual(wallet_sage._SAGE_PORT, 9257)
            finally:
                wallet_sage.CERT_PATH = old_cert
                wallet_sage.KEY_PATH = old_key
                wallet_sage.WALLET_URL = old_url
                wallet_sage._SAGE_HOST = old_host
                wallet_sage._SAGE_PORT = old_port
                wallet_sage._conn_local.conn = None

    def test_reload_connection_settings_preserves_process_env_over_env_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_cert = os.path.join(temp_dir, "file.crt")
            file_key = os.path.join(temp_dir, "file.key")
            env_cert = os.path.join(temp_dir, "env.crt")
            env_key = os.path.join(temp_dir, "env.key")
            env_path = os.path.join(temp_dir, ".env")
            with open(env_path, "w", encoding="utf-8") as f:
                f.write("SAGE_RPC_URL=https://127.0.0.1:9999\n")
                f.write(f"SAGE_CERT_PATH={file_cert}\n")
                f.write(f"SAGE_KEY_PATH={file_key}\n")

            old_cert = wallet_sage.CERT_PATH
            old_key = wallet_sage.KEY_PATH
            old_url = wallet_sage.WALLET_URL
            old_host = wallet_sage._SAGE_HOST
            old_port = wallet_sage._SAGE_PORT
            try:
                with patch.object(wallet_sage, "_env_file", return_value=env_path), \
                     patch.dict(os.environ, {
                         "SAGE_RPC_URL": "https://127.0.0.1:9257",
                         "SAGE_CERT_PATH": env_cert,
                         "SAGE_KEY_PATH": env_key,
                     }, clear=False):
                    wallet_sage.reload_connection_settings()
                    self.assertEqual(wallet_sage.WALLET_URL, "https://127.0.0.1:9257")
                    self.assertEqual(wallet_sage.CERT_PATH, env_cert)
                    self.assertEqual(wallet_sage.KEY_PATH, env_key)
            finally:
                wallet_sage.CERT_PATH = old_cert
                wallet_sage.KEY_PATH = old_key
                wallet_sage.WALLET_URL = old_url
                wallet_sage._SAGE_HOST = old_host
                wallet_sage._SAGE_PORT = old_port
                wallet_sage._conn_local.conn = None

    def test_get_chia_health_reports_syncing_when_wallet_not_synced(self):
        # Mock get_peer_connections to avoid real network calls that may fail
        # when the Sage wallet is under load from earlier tests in the suite.
        with patch.object(wallet_sage, "get_wallet_sync_status", return_value={
            "reachable": True,
            "synced": False,
            "syncing": True,
            "sync_state": "not_synced",
        }), patch.object(wallet_sage, "get_peer_connections", return_value=[
            {"peer_host": "127.0.0.1"},
        ]):
            health = wallet_sage.get_chia_health()

        self.assertEqual(health["status"], "wallet_not_synced")
        self.assertFalse(health["healthy"])

    def test_get_chia_health_reports_unknown_when_sync_state_unknown(self):
        with patch.object(wallet_sage, "get_wallet_sync_status", return_value={
            "reachable": True,
            "synced": False,
            "syncing": False,
            "sync_state": "unknown",
        }), patch.object(wallet_sage, "get_peer_connections", return_value=[
            {"peer_host": "127.0.0.1"},
        ]):
            health = wallet_sage.get_chia_health()

        self.assertEqual(health["status"], "wallet_sync_unknown")
        self.assertFalse(health["healthy"])

    def test_get_wallets_does_not_crash_when_no_configured_cat(self):
        # Reset init state in case previous tests set it
        wallet_sage._init_ok = True
        wallet_sage._init_last_attempt = 0.0

        sample_cats = {
            "cats": [
                {"asset_id": "a" * 64, "name": "Alpha", "ticker": "ALPHA"},
                {"asset_id": "b" * 64, "name": "Beta", "ticker": "BETA"},
            ]
        }
        if hasattr(wallet_sage.get_wallets, "_discovery_logged"):
            delattr(wallet_sage.get_wallets, "_discovery_logged")

        with patch.object(wallet_sage, "_get_cat_asset_id", return_value=None):
            with patch.object(wallet_sage, "rpc", return_value=sample_cats):
                result = wallet_sage.get_wallets()

        self.assertTrue(result["success"])
        self.assertGreaterEqual(len(result["wallets"]), 3)  # XCH + discovered CATs


if __name__ == "__main__":
    unittest.main()
