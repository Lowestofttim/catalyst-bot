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
    def test_get_chia_health_reports_syncing_when_wallet_not_synced(self):
        with patch.object(wallet_sage, "get_wallet_sync_status", return_value={
            "reachable": True,
            "synced": False,
            "syncing": True,
            "sync_state": "not_synced",
        }):
            health = wallet_sage.get_chia_health()

        self.assertEqual(health["status"], "wallet_not_synced")
        self.assertFalse(health["healthy"])

    def test_get_chia_health_reports_unknown_when_sync_state_unknown(self):
        with patch.object(wallet_sage, "get_wallet_sync_status", return_value={
            "reachable": True,
            "synced": False,
            "syncing": False,
            "sync_state": "unknown",
        }):
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
