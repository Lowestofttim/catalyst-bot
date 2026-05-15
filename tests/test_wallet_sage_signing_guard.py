import unittest
from unittest.mock import patch

try:
    import wallet_sage

    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    wallet_sage = None
    _IMPORT_ERROR = exc


@unittest.skipIf(
    wallet_sage is None, f"wallet_sage import unavailable: {_IMPORT_ERROR}"
)
class TestWalletSageSigningGuard(unittest.TestCase):
    def test_allows_wallet_with_secrets(self):
        with patch.object(
            wallet_sage, "get_current_key", return_value={"has_secrets": True}
        ):
            self.assertTrue(wallet_sage._require_signing_capability())

    def test_blocks_watch_only_wallet(self):
        with patch.object(
            wallet_sage, "get_current_key", return_value={"has_secrets": False}
        ):
            self.assertFalse(wallet_sage._require_signing_capability())

    def test_blocks_when_active_key_missing(self):
        with patch.object(wallet_sage, "get_current_key", return_value=None):
            self.assertFalse(wallet_sage._require_signing_capability())

    def test_blocks_when_lookup_errors(self):
        with patch.object(
            wallet_sage, "get_current_key", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(wallet_sage._require_signing_capability())


if __name__ == "__main__":
    unittest.main()
