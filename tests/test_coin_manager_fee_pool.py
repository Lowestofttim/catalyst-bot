import importlib
import os
import sys
import types
import unittest
from decimal import Decimal


_MODS_TO_RESTORE = (
    "coin_manager",
    "tx_fees",
    "wallet",
    "wallet_sage",
    "database",
    "config",
)


class CoinManagerFeePoolTests(unittest.TestCase):
    def setUp(self):
        self._saved_wallet_type = os.environ.get("WALLET_TYPE")
        os.environ["WALLET_TYPE"] = "chia"
        self._saved_modules = {name: sys.modules.get(name) for name in _MODS_TO_RESTORE}

        _TIER_SIZES = {
            "inner": Decimal("1.0"),
            "mid": Decimal("0.5"),
            "outer": Decimal("0.25"),
            "extreme": Decimal("0.1"),
        }

        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            COINSET_ENABLED=False,
            WALLET_ID_XCH=1,
            CAT_WALLET_ID=2,
            TIER_ENABLED=True,
            ENABLE_COIN_PREP=True,
            CAT_DECIMALS=3,
            INNER_SIZE_XCH=Decimal("1.0"),
            MID_SIZE_XCH=Decimal("0.5"),
            OUTER_SIZE_XCH=Decimal("0.25"),
            EXTREME_SIZE_XCH=Decimal("0.1"),
            # Per-side sizes (required by coin_manager._configured_tier_sizes_xch)
            BUY_INNER_SIZE_XCH=Decimal("1.0"),
            BUY_MID_SIZE_XCH=Decimal("0.5"),
            BUY_OUTER_SIZE_XCH=Decimal("0.25"),
            BUY_EXTREME_SIZE_XCH=Decimal("0.1"),
            SELL_INNER_SIZE_XCH=Decimal("1.0"),
            SELL_MID_SIZE_XCH=Decimal("0.5"),
            SELL_OUTER_SIZE_XCH=Decimal("0.25"),
            SELL_EXTREME_SIZE_XCH=Decimal("0.1"),
            BUY_LADDER_REVERSED=False,
            SNIPER_ENABLED=False,
            SNIPER_SIZE_XCH=Decimal("0"),
            SNIPER_PREP_COUNT=0,
            COIN_PREP_HEADROOM_PCT=Decimal("10"),
            CAT_COIN_SIZE=Decimal("4000"),
            TRANSACTION_FEE_MODE="manual",
            TRANSACTION_FEE_XCH=Decimal("0.00000050"),
            TRANSACTION_FEE_TARGET_SECS=300,
            TRANSACTION_FEE_ESTIMATE_COST=20_000_000,
            FEE_PREP_COUNT=20,
            FEE_COIN_SIZE_XCH=Decimal("0.0001"),
        )
        # Module-level helpers imported directly by coin_manager (not via cfg)
        fake_config.get_buy_tier_size_xch = lambda tier: _TIER_SIZES.get(
            (tier or "").strip().lower(), Decimal("0")
        )
        fake_config.get_sell_tier_size_xch = lambda tier: _TIER_SIZES.get(
            (tier or "").strip().lower(), Decimal("0")
        )
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.log_event = lambda *args, **kwargs: None
        sys.modules["database"] = fake_database

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_exact_spendable_coins_rpc = lambda wallet_id: {
            "success": True,
            "records": [],
        }
        fake_wallet.get_all_coins_for_wallet = lambda *args, **kwargs: []
        fake_wallet.get_wallet_balance = lambda *args, **kwargs: {
            "wallet_balance": {"spendable_balance": 0}
        }
        fake_wallet.get_next_address = lambda *args, **kwargs: {
            "success": True,
            "address": "xch1test",
        }
        fake_wallet.send_transaction = lambda *args, **kwargs: {"success": True}
        fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
        fake_wallet.get_wallet_type = lambda: "chia"
        fake_wallet.WALLET_ID_XCH = 1
        fake_wallet.get_owned_coins = lambda *args, **kwargs: {}
        fake_wallet.get_owned_coins_detailed = lambda *a, **kw: None
        fake_wallet.rpc = lambda *args, **kwargs: {"fingerprint": "123"}
        sys.modules["wallet"] = fake_wallet

        sys.modules.pop("tx_fees", None)
        sys.modules.pop("coin_manager", None)
        self.coin_manager = importlib.import_module("coin_manager")
        self.manager = self.coin_manager.CoinManager()

    def tearDown(self):
        for name, saved in self._saved_modules.items():
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

        if self._saved_wallet_type is None:
            os.environ.pop("WALLET_TYPE", None)
        else:
            os.environ["WALLET_TYPE"] = self._saved_wallet_type

    def test_fee_tier_is_xch_only(self):
        xch_sizes = self.manager._get_tier_sizes_mojos(is_cat=False)
        cat_sizes = self.manager._get_tier_sizes_mojos(is_cat=True)

        self.assertIn("fees", xch_sizes)
        self.assertEqual(xch_sizes["fees"], 100_000_000)
        self.assertNotIn("fees", cat_sizes)


if __name__ == "__main__":
    unittest.main()
