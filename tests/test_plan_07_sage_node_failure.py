"""Layer 7 — Sage RPC + node sync failure contracts (slices 07-01, 07-02).

Slices covered:
  07-01: Sage RPC disconnected mid-cycle — all RPC calls return structured
         errors (not raise); wallet functions degrade gracefully; sync-status
         reports reachable=False; rpc() returns error dict on ConnectionError
  07-02: Chia node loses sync — get_wallet_sync_status() returns synced=False;
         get_combined_sync_status() marks healthy=False; callers see correct
         degraded state without crashing

Note: wallet_sage is the Sage backend; wallet.py dispatches to it based on
WALLET_TYPE setting. We test wallet_sage directly (unit-level chaos).
"""

import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import wallet_sage

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    wallet_sage = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# 07-01: Sage RPC disconnected — rpc() returns error struct (never raises)
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"wallet_sage unavailable: {_SKIP}")
class TestSageRPCDisconnected(unittest.TestCase):
    """When Sage is not running, rpc() must return error dicts, never raise."""

    def setUp(self):
        # Guard against test_wallet_sync_fail_closed.tearDown popping wallet_sage
        # from sys.modules. patch("wallet_sage.X") resolves via sys.modules, so
        # it must point to the same module object our tests hold a reference to.
        sys.modules["wallet_sage"] = wallet_sage

    def _connection_refused(self):
        return ConnectionError("Connection refused — Sage not running")

    def test_rpc_returns_dict_on_connection_error(self):
        """rpc() catches ConnectionError and returns an error dict."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch("wallet_sage._sage_post", side_effect=self._connection_refused()),
        ):
            result = wallet_sage.rpc("get_wallets", {})
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))

    def test_rpc_does_not_raise_on_connection_error(self):
        """rpc() must never propagate ConnectionError to callers."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch("wallet_sage._sage_post", side_effect=self._connection_refused()),
        ):
            try:
                wallet_sage.rpc("get_wallets", {})
            except Exception as exc:
                self.fail(f"rpc() propagated: {exc}")

    def test_rpc_returns_none_on_unexpected_exception(self):
        """Any non-ConnectionError exception returns None (not raise)."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch("wallet_sage._sage_post", side_effect=RuntimeError("weird")),
        ):
            result = wallet_sage.rpc("get_wallets", {})
        self.assertIsNone(result)

    def test_rpc_succeeded_false_on_none(self):
        """_rpc_succeeded(None) returns False — callers detect RPC failure."""
        self.assertFalse(wallet_sage._rpc_succeeded(None))

    def test_rpc_succeeded_false_on_error_dict(self):
        """_rpc_succeeded({"success": False}) returns False."""
        self.assertFalse(wallet_sage._rpc_succeeded({"success": False, "error": "x"}))

    def test_rpc_succeeded_true_on_success_dict(self):
        """_rpc_succeeded({"success": True}) returns True."""
        self.assertTrue(wallet_sage._rpc_succeeded({"success": True}))

    def test_ensure_initialized_returns_false_when_port_unreachable(self):
        """When Sage port is not listening, ensure_initialized() returns False."""
        with (
            patch("wallet_sage._sage_rpc_port_reachable", return_value=False),
            patch("wallet_sage._init_ok", False),
        ):
            result = wallet_sage.ensure_initialized(force_retry=True)
        self.assertFalse(result)

    def test_get_wallet_sync_status_returns_offline_when_not_initialized(self):
        """If ensure_initialized() fails, sync status is offline."""
        with patch("wallet_sage.ensure_initialized", return_value=False):
            status = wallet_sage.get_wallet_sync_status()
        self.assertFalse(status.get("reachable"))
        self.assertFalse(status.get("synced"))
        self.assertEqual(status.get("sync_state"), "offline")

    def test_get_wallet_sync_status_returns_offline_on_exception(self):
        """Exception from rpc() returns offline status (not raise)."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch("wallet_sage.rpc", side_effect=Exception("conn refused")),
        ):
            try:
                status = wallet_sage.get_wallet_sync_status()
            except Exception as exc:
                self.fail(f"get_wallet_sync_status raised: {exc}")
        self.assertFalse(status.get("reachable"))
        self.assertFalse(status.get("synced"))

    def test_get_wallet_balance_returns_error_dict_on_rpc_failure(self):
        """get_wallet_balance() returns {"success": False} dict when RPC fails."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch("wallet_sage.rpc", return_value=None),
        ):
            result = wallet_sage.get_wallet_balance(1)
        # Returns error dict or None — never raises
        if result is not None:
            self.assertFalse(result.get("success", True))

    def test_get_spendable_coins_returns_none_on_rpc_failure(self):
        """get_spendable_coins_rpc() returns None when RPC fails (not raises)."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch("wallet_sage.rpc", return_value=None),
        ):
            try:
                result = wallet_sage.get_spendable_coins_rpc(1)
            except Exception as exc:
                self.fail(f"get_spendable_coins_rpc raised: {exc}")
        # Returns None or a dict — never raises
        self.assertFalse(isinstance(result, list))

    def test_port_reachable_returns_false_on_connection_refused(self):
        """_sage_rpc_port_reachable() returns False (not raise) on connect error."""
        with patch("wallet_sage._sock") as mock_sock:
            mock_socket = MagicMock()
            mock_socket.connect.side_effect = ConnectionRefusedError("refused")
            mock_sock.socket.return_value = mock_socket
            result = wallet_sage._sage_rpc_port_reachable()
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 07-02: Node sync loss — sync status functions report degraded state
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"wallet_sage unavailable: {_SKIP}")
class TestNodeSyncLoss(unittest.TestCase):
    """When the node loses sync, sync-status functions must report it correctly."""

    def setUp(self):
        sys.modules["wallet_sage"] = wallet_sage

    def test_sync_status_synced_false_when_rpc_says_not_synced(self):
        """get_wallet_sync_status() returns synced=False for explicit False."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch(
                "wallet_sage.rpc",
                return_value={
                    "success": True,
                    "synced": False,
                    "synced_coins": 100,
                    "total_coins": 500,
                },
            ),
        ):
            status = wallet_sage.get_wallet_sync_status()
        self.assertFalse(status.get("synced"))
        self.assertTrue(status.get("reachable"))

    def test_sync_status_syncing_true_when_not_synced(self):
        """syncing=True is set when the wallet reports not synced."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch(
                "wallet_sage.rpc",
                return_value={
                    "success": True,
                    "synced": False,
                    "synced_coins": 50,
                    "total_coins": 500,
                },
            ),
        ):
            status = wallet_sage.get_wallet_sync_status()
        self.assertTrue(status.get("syncing"))

    def test_sync_status_synced_true_when_coins_match(self):
        """Infers synced=True when synced_coins == total_coins (no boolean field)."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch(
                "wallet_sage.rpc",
                return_value={
                    "success": True,
                    "synced_coins": 100,
                    "total_coins": 100,
                },
            ),
        ):
            status = wallet_sage.get_wallet_sync_status()
        self.assertTrue(status.get("synced"))
        self.assertEqual(status.get("sync_state"), "synced")

    def test_sync_status_not_synced_when_coins_behind(self):
        """synced_coins < total_coins → synced=False (no explicit boolean)."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch(
                "wallet_sage.rpc",
                return_value={
                    "success": True,
                    "synced_coins": 10,
                    "total_coins": 500,
                },
            ),
        ):
            status = wallet_sage.get_wallet_sync_status()
        self.assertFalse(status.get("synced"))

    def test_combined_sync_status_healthy_false_when_not_synced(self):
        """get_chia_health() healthy=False when wallet not synced."""
        not_synced = {
            "reachable": True,
            "synced": False,
            "syncing": True,
            "sync_state": "not_synced",
        }
        node = {"synced": True, "reachable": True}
        with (
            patch("wallet_sage.get_wallet_sync_status", return_value=not_synced),
            patch("wallet_sage.get_full_node_sync_status", return_value=node),
            patch("wallet_sage.get_peer_connections", return_value=[{"peer": 1}]),
        ):
            combined = wallet_sage.get_chia_health()
        self.assertFalse(combined.get("healthy"))

    def test_combined_sync_status_healthy_true_when_synced(self):
        """get_chia_health() healthy=True when wallet is fully synced with peers."""
        synced = {
            "reachable": True,
            "synced": True,
            "syncing": False,
            "sync_state": "synced",
        }
        node = {"synced": True, "reachable": True}
        with (
            patch("wallet_sage.get_wallet_sync_status", return_value=synced),
            patch("wallet_sage.get_full_node_sync_status", return_value=node),
            patch("wallet_sage.get_peer_connections", return_value=[{"peer": 1}]),
        ):
            combined = wallet_sage.get_chia_health()
        self.assertTrue(combined.get("healthy"))

    def test_combined_sync_status_offline_reports_not_healthy(self):
        """If Sage is unreachable, get_chia_health() must report healthy=False."""
        offline = {
            "reachable": False,
            "synced": False,
            "syncing": False,
            "sync_state": "offline",
        }
        node = {"synced": False, "reachable": False}
        with (
            patch("wallet_sage.get_wallet_sync_status", return_value=offline),
            patch("wallet_sage.get_full_node_sync_status", return_value=node),
            patch("wallet_sage.get_peer_connections", return_value=[]),
        ):
            combined = wallet_sage.get_chia_health()
        self.assertFalse(combined.get("healthy"))

    def test_sync_status_unknown_when_zero_coins(self):
        """total_coins == 0 → sync state unknown (wallet still loading)."""
        with (
            patch("wallet_sage.ensure_initialized", return_value=True),
            patch(
                "wallet_sage.rpc",
                return_value={
                    "success": True,
                    "synced_coins": 0,
                    "total_coins": 0,
                },
            ),
        ):
            status = wallet_sage.get_wallet_sync_status()
        self.assertEqual(status.get("sync_state"), "unknown")


if __name__ == "__main__":
    unittest.main()
