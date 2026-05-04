import importlib
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


class _FakeCursor:
    def fetchone(self):
        return [0]


class _FakeConnection:
    def execute(self, *args, **kwargs):
        return _FakeCursor()


def _load_coin_manager_with_fakes():
    names = [
        "config", "database", "wallet", "tx_fees", "win_subprocess",
        "coin_reservations", "coin_manager",
    ]
    originals = {name: sys.modules.get(name) for name in names}

    fake_config = types.ModuleType("config")
    fake_config.cfg = types.SimpleNamespace(
        TIER_ENABLED=True,
        MAX_ACTIVE_BUY_OFFERS=4,
        MAX_ACTIVE_SELL_OFFERS=4,
        ENABLE_BUY=True,
        ENABLE_SELL=True,
        COIN_PREP_MULTIPLIER=Decimal("2.0"),
        WALLET_FINGERPRINT="123456",
        WALLET_ID_XCH=1,
        CAT_WALLET_ID=2,
        SNIPER_ENABLED=False,
        SNIPER_PREP_COUNT=0,
    )
    sys.modules["config"] = fake_config

    fake_database = types.ModuleType("database")
    fake_database.log_event = lambda *args, **kwargs: None
    fake_database.get_connection = lambda: _FakeConnection()
    fake_database.get_tier_spare_counts = lambda wallet_type: {}
    sys.modules["database"] = fake_database

    fake_wallet = types.ModuleType("wallet")
    fake_wallet.get_exact_spendable_coins_rpc = (
        lambda *args, **kwargs: {"records": []}
    )
    fake_wallet.get_next_address = lambda *args, **kwargs: {"success": True}
    fake_wallet.send_transaction = lambda *args, **kwargs: {"success": True}
    fake_wallet.split_coins_rpc = lambda *args, **kwargs: {"success": True}
    fake_wallet.get_wallet_type = lambda: "sage"
    fake_wallet.WALLET_ID_XCH = 1
    fake_wallet.get_owned_coins = lambda *args, **kwargs: {}
    fake_wallet.get_owned_coins_detailed = lambda *args, **kwargs: {}
    sys.modules["wallet"] = fake_wallet

    fake_tx_fees = types.ModuleType("tx_fees")
    fake_tx_fees.fee_pool_enabled = lambda: False
    fake_tx_fees.get_effective_transaction_fee_mojos = lambda: 0
    fake_tx_fees.get_fee_coin_size_mojos = lambda: 0
    fake_tx_fees.get_fee_coin_size_xch = lambda: Decimal("0")
    fake_tx_fees.get_fee_pool_count = lambda: 0
    fake_tx_fees.get_fee_tier_name = lambda: "fees"
    sys.modules["tx_fees"] = fake_tx_fees

    fake_win_subprocess = types.ModuleType("win_subprocess")
    fake_win_subprocess.hidden_subprocess_kwargs = lambda: {}
    sys.modules["win_subprocess"] = fake_win_subprocess

    fake_reservations = types.ModuleType("coin_reservations")
    fake_reservations.ReservationRegistry = type("ReservationRegistry", (), {})
    sys.modules["coin_reservations"] = fake_reservations

    sys.modules.pop("coin_manager", None)
    module = importlib.import_module("coin_manager")
    return module, originals


def _restore_modules(originals):
    for name, module in originals.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class CoinReadinessBufferStatusTests(unittest.TestCase):
    def test_low_spares_are_ready_with_buffer_status(self):
        coin_manager, originals = _load_coin_manager_with_fakes()
        try:
            manager = coin_manager.CoinManager()
            manager._xch_inventory.update({
                "inner": [object()],
                "mid": [],
                "outer": [],
                "extreme": [],
            })
            manager._cat_inventory.update({
                "inner": [object()],
                "mid": [],
                "outer": [],
                "extreme": [],
            })

            dist = {"inner": 2, "mid": 0, "outer": 0, "extreme": 0}
            prep = {"inner": 4, "mid": 0, "outer": 0, "extreme": 0}
            with patch.object(coin_manager, "get_tier_distribution", return_value=dist), \
                    patch.object(coin_manager, "get_weighted_tier_prep_counts",
                                 return_value=prep):
                report = manager.coin_readiness_report()

            self.assertTrue(report["overall_ready"])
            self.assertEqual(report["overall_status"], "SPARE_BUFFER_LOW")
            self.assertEqual(report["tiers"]["inner"]["xch_status"], "LOW")
            self.assertEqual(report["tiers"]["inner"]["cat_status"], "LOW")
        finally:
            _restore_modules(originals)

    def test_per_tier_readiness_lines_are_debug_but_summary_stays_info(self):
        coin_manager, originals = _load_coin_manager_with_fakes()
        try:
            manager = coin_manager.CoinManager()
            manager._xch_inventory.update({
                "inner": [object()],
                "mid": [],
                "outer": [],
                "extreme": [],
            })
            manager._cat_inventory.update({
                "inner": [object()],
                "mid": [],
                "outer": [],
                "extreme": [],
            })

            dist = {"inner": 2, "mid": 0, "outer": 0, "extreme": 0}
            prep = {"inner": 4, "mid": 0, "outer": 0, "extreme": 0}
            with patch.object(coin_manager, "get_tier_distribution", return_value=dist), \
                    patch.object(coin_manager, "get_weighted_tier_prep_counts",
                                 return_value=prep), \
                    patch.object(coin_manager, "log_event") as log_event:
                manager.coin_readiness_report()

            readiness_calls = [
                call for call in log_event.call_args_list
                if len(call.args) >= 3 and call.args[1] == "coin_readiness"
            ]
            self.assertGreaterEqual(len(readiness_calls), 2)
            tier_lines = [
                call for call in readiness_calls
                if "COIN READINESS:" not in call.args[2]
            ]
            summary_lines = [
                call for call in readiness_calls
                if "COIN READINESS:" in call.args[2]
            ]
            self.assertTrue(tier_lines)
            self.assertEqual({call.args[0] for call in tier_lines}, {"debug"})
            self.assertEqual({call.args[0] for call in summary_lines}, {"info"})
        finally:
            _restore_modules(originals)


if __name__ == "__main__":
    unittest.main()
