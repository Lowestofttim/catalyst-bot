import importlib
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


class TxFeesTests(unittest.TestCase):
    def setUp(self):
        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            WALLET_TYPE="sage",
            TRANSACTION_FEE_MODE="manual",
            TRANSACTION_FEE_XCH=Decimal("0.00000050"),
            TRANSACTION_FEE_TARGET_SECS=300,
            TRANSACTION_FEE_ESTIMATE_COST=20_000_000,
            FEE_PREP_COUNT=20,
            FEE_COIN_SIZE_XCH=Decimal("0.0001"),
            CHIA_WALLET_CERT="",
            CHIA_WALLET_KEY="",
            CHIA_FULL_NODE_RPC_URL="https://localhost:8555",
        )
        sys.modules["config"] = fake_config
        sys.modules.pop("tx_fees", None)
        self.tx_fees = importlib.import_module("tx_fees")
        self.cfg = fake_config.cfg

    def tearDown(self):
        sys.modules.pop("tx_fees", None)
        sys.modules.pop("config", None)

    def test_manual_fee_mode_uses_manual_value(self):
        effective = self.tx_fees.get_effective_transaction_fee_mojos()
        self.assertEqual(effective, 500_000)
        self.assertTrue(self.tx_fees.fee_pool_enabled())

    def test_auto_fee_mode_prefers_suggested_fee(self):
        self.cfg.TRANSACTION_FEE_MODE = "auto"
        with patch.object(
            self.tx_fees,
            "get_suggested_transaction_fee",
            return_value={
                "available": True,
                "fee_mojos": 1_234_567,
                "fee_xch": "0.000001234567",
            },
        ):
            self.assertEqual(
                self.tx_fees.get_effective_transaction_fee_mojos(), 1_234_567
            )

    def test_fee_pool_plan_reports_fee_tier(self):
        plan = self.tx_fees.get_fee_pool_plan()
        self.assertEqual(plan["tier_name"], "fees")
        self.assertEqual(plan["count"], 20)
        self.assertEqual(plan["coin_size_mojos"], 100_000_000)

    def test_sage_without_full_node_reports_manual_fallback_environment(self):
        # Disable coinset so the test exercises the pure sage/no-full-node fallback path.
        # (COINSET_ENABLED defaults to True globally, but this test verifies the
        # manual_fallback_only branch that runs when neither full-node nor coinset are available.)
        self.cfg.TRANSACTION_FEE_MODE = "auto"
        self.cfg.COINSET_ENABLED = False
        snapshot = self.tx_fees.get_fee_settings_snapshot()
        self.assertEqual(snapshot["wallet_type"], "sage")
        self.assertFalse(snapshot["environment"]["supports_auto_estimate"])
        self.assertEqual(snapshot["suggested"]["source"], "manual_fallback_only")


if __name__ == "__main__":
    unittest.main()
