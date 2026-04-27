"""Regression tests for security findings from plan slice 01-02 (bandit scan).

Covers the one real HIGH finding fixed in this slice:
  F1 — tx_fees._full_node_rpc used verify=False unconditionally; now uses
       the Chia private CA cert for TLS server verification when available.
"""
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock


class TestTxFeesFullNodeRpcTLS(unittest.TestCase):
    """_full_node_rpc must prefer proper TLS verification over verify=False."""

    def _make_fake_cert_tree(self, tmp_dir: str) -> dict:
        """Create a minimal fake Chia SSL directory tree."""
        ssl_root = os.path.join(tmp_dir, "ssl")
        os.makedirs(os.path.join(ssl_root, "wallet"), exist_ok=True)
        os.makedirs(os.path.join(ssl_root, "full_node"), exist_ok=True)
        os.makedirs(os.path.join(ssl_root, "ca"), exist_ok=True)

        paths = {
            "wallet_cert": os.path.join(ssl_root, "wallet", "private_wallet.crt"),
            "wallet_key":  os.path.join(ssl_root, "wallet", "private_wallet.key"),
            "fn_cert":     os.path.join(ssl_root, "full_node", "private_full_node.crt"),
            "fn_key":      os.path.join(ssl_root, "full_node", "private_full_node.key"),
            "ca_cert":     os.path.join(ssl_root, "ca", "private_ca.crt"),
        }
        for p in paths.values():
            open(p, "w").close()
        return paths

    def test_get_chia_ca_cert_returns_path_when_ca_exists(self):
        """_get_chia_ca_cert must return the CA cert path when the file exists."""
        import tx_fees
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._make_fake_cert_tree(tmp)
            with patch.object(tx_fees.cfg, "CHIA_WALLET_CERT", paths["wallet_cert"]):
                result = tx_fees._get_chia_ca_cert()
        self.assertEqual(result, paths["ca_cert"])

    def test_get_chia_ca_cert_returns_none_when_ca_missing(self):
        """_get_chia_ca_cert must return None when the CA cert file is absent."""
        import tx_fees
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._make_fake_cert_tree(tmp)
            os.remove(paths["ca_cert"])  # CA cert absent
            with patch.object(tx_fees.cfg, "CHIA_WALLET_CERT", paths["wallet_cert"]):
                result = tx_fees._get_chia_ca_cert()
        self.assertIsNone(result)

    def test_full_node_rpc_uses_ca_cert_when_available(self):
        """_full_node_rpc must pass verify=<ca_cert_path> when the CA cert exists."""
        import tx_fees
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._make_fake_cert_tree(tmp)
            captured_kwargs = {}

            def fake_post(url, **kwargs):
                captured_kwargs.update(kwargs)
                mock_resp = MagicMock()
                mock_resp.json.return_value = {"success": True}
                return mock_resp

            with patch.object(tx_fees.cfg, "CHIA_WALLET_CERT", paths["wallet_cert"]):
                with patch.object(tx_fees.cfg, "CHIA_WALLET_KEY",  paths["wallet_key"]):
                    with patch("requests.post", side_effect=fake_post):
                        tx_fees._full_node_rpc("get_fee_estimate", {})

        # verify must be the CA cert path, not False
        self.assertEqual(captured_kwargs.get("verify"), paths["ca_cert"],
                         "verify should be the CA cert path when CA cert is available")

    def test_full_node_rpc_falls_back_to_false_when_ca_missing(self):
        """_full_node_rpc must fall back to verify=False when CA cert is absent."""
        import tx_fees
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._make_fake_cert_tree(tmp)
            os.remove(paths["ca_cert"])  # CA cert absent
            captured_kwargs = {}

            def fake_post(url, **kwargs):
                captured_kwargs.update(kwargs)
                mock_resp = MagicMock()
                mock_resp.json.return_value = {"success": True}
                return mock_resp

            with patch.object(tx_fees.cfg, "CHIA_WALLET_CERT", paths["wallet_cert"]):
                with patch.object(tx_fees.cfg, "CHIA_WALLET_KEY",  paths["wallet_key"]):
                    with patch("requests.post", side_effect=fake_post):
                        tx_fees._full_node_rpc("get_fee_estimate", {})

        self.assertFalse(captured_kwargs.get("verify"),
                         "verify must be False (falsy) when CA cert is unavailable")

    def test_verify_false_not_hardcoded_as_kwarg_in_full_node_rpc(self):
        """requests.post must not be called with literal verify=False in _full_node_rpc."""
        import ast
        import inspect
        import tx_fees
        src = inspect.getsource(tx_fees._full_node_rpc)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (kw.arg == "verify" and
                            isinstance(kw.value, ast.Constant) and
                            kw.value.value is False):
                        self.fail("requests.post is called with literal verify=False "
                                  "— it must use the _tls_verify variable instead")


if __name__ == "__main__":
    unittest.main()
