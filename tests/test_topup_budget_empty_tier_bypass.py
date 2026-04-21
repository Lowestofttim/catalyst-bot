"""Tests for the empty-tier escape hatch on the topup budget guard.

When a trading tier has zero free coins, every offer on that slot is dead.
Refusing to split because of the *soft* TOPUP_POOL_CAT / TOPUP_POOL_XCH
budget would trade one protected number for a dead trading slot, so the
guard bypasses the budget (with a warning) specifically when `tier_is_empty`
is set. The *hard* reserve guard (XCH_RESERVE / CAT_RESERVE) is NEVER
bypassed — capital protection still applies.
"""

import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


_INSTALLED_STUBS: list = []

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    dotenv_stub.set_key = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub
    _INSTALLED_STUBS.append("dotenv")

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")

    class _DummyResponse:
        status_code = 200

        def json(self):
            return {"status": "success"}

        def raise_for_status(self):
            return None

    class _StubSession:
        def __init__(self):
            self.headers = {}

        def get(self, *args, **kwargs):
            return _DummyResponse()

        def mount(self, *args, **kwargs):
            pass

    requests_stub.get = lambda *args, **kwargs: _DummyResponse()
    requests_stub.Session = _StubSession
    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=Exception,
        ConnectionError=Exception,
    )
    requests_adapters_stub = types.ModuleType("requests.adapters")
    requests_adapters_stub.HTTPAdapter = object
    requests_stub.adapters = requests_adapters_stub
    sys.modules["requests"] = requests_stub
    sys.modules["requests.adapters"] = requests_adapters_stub
    _INSTALLED_STUBS.extend(["requests", "requests.adapters"])

if "urllib3" not in sys.modules:
    urllib3_stub = types.ModuleType("urllib3")
    urllib3_stub.Retry = object
    urllib3_stub.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
    urllib3_stub.disable_warnings = lambda *args, **kwargs: None
    sys.modules["urllib3"] = urllib3_stub
    _INSTALLED_STUBS.append("urllib3")


import coin_manager


class _FakeWalletBalance:
    """Patch target for wallet.get_wallet_balance with a controllable total."""

    def __init__(self, total_mojos):
        self.total_mojos = total_mojos

    def __call__(self, wallet_id):
        return {"wallet_balance": {"confirmed_wallet_balance": self.total_mojos}}


class EmptyTierBudgetBypassTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # Leave shared modules loaded. Popping `config` would force later
        # test classes to re-import it, but `bot_health` (and other test
        # modules) cache the cfg reference at import time — so the re-
        # imported cfg would be a DIFFERENT instance from the one
        # `bot_health.cfg` points at. That mismatch caused spurious
        # drift-check failures when this file ran before the bot_health
        # test suite.
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)

    def _make_manager(self):
        with patch.object(coin_manager.CoinManager, "_resolve_fingerprint",
                          return_value="123456789"):
            return coin_manager.CoinManager()

    # ------------------------------------------------------------------
    # CAT bypass scenarios
    # ------------------------------------------------------------------

    def test_cat_empty_tier_bypasses_exhausted_budget(self):
        """Empty CAT tier + exhausted budget + hard reserve OK → bypass with warning."""
        manager = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "CAT_RESERVE", Decimal("0")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("100")), \
             patch.object(cfg, "CAT_DECIMALS", 3), \
             patch("wallet.get_wallet_balance", _FakeWalletBalance(total_mojos=906_913_211)), \
             patch("database.get_setting", return_value=str(138_940_000)), \
             patch.object(coin_manager, "log_event") as log_spy:
            # Budget=100 CAT = 100,000 mojos, spent=138,940,000 mojos, request=50,000,000 mojos.
            # Standard check: 138,940,000 + 50,000,000 > 100,000 → blocked.
            # With tier_is_empty: should bypass and return True.
            result = manager._check_topup_reserve_guards(
                name="CAT-inner",
                wallet_id=2,
                pool_amount_mojos=50_000_000,
                is_cat=True,
                tier_is_empty=True,
            )
        self.assertTrue(result)
        bypass_calls = [c for c in log_spy.call_args_list
                        if len(c.args) >= 2
                        and "budget_bypass_empty_tier" in str(c.args[1])]
        self.assertTrue(bypass_calls, "Expected a budget_bypass_empty_tier log event")

    def test_cat_non_empty_tier_still_blocked_by_budget(self):
        """Non-empty tier hitting same exhausted budget → still blocked."""
        manager = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "CAT_RESERVE", Decimal("0")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("100")), \
             patch.object(cfg, "CAT_DECIMALS", 3), \
             patch("wallet.get_wallet_balance", _FakeWalletBalance(total_mojos=906_913_211)), \
             patch("database.get_setting", return_value=str(138_940_000)), \
             patch.object(coin_manager, "log_event") as log_spy:
            result = manager._check_topup_reserve_guards(
                name="CAT-inner",
                wallet_id=2,
                pool_amount_mojos=50_000_000,
                is_cat=True,
                tier_is_empty=False,
            )
        self.assertFalse(result)
        blocked_calls = [c for c in log_spy.call_args_list
                         if len(c.args) >= 2
                         and "blocked_by_budget" in str(c.args[1])]
        self.assertTrue(blocked_calls, "Expected a blocked_by_budget log event")

    # ------------------------------------------------------------------
    # Hard reserve is NEVER bypassed
    # ------------------------------------------------------------------

    def test_empty_tier_does_not_bypass_hard_reserve(self):
        """tier_is_empty=True must NOT bypass the hard reserve guard."""
        manager = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "CAT_RESERVE", Decimal("1000000")), \
             patch.object(cfg, "CAT_DECIMALS", 3), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")), \
             patch("wallet.get_wallet_balance", _FakeWalletBalance(total_mojos=500)), \
             patch.object(coin_manager, "log_event") as log_spy:
            # balance=500 mojos, reserve=1,000,000,000 mojos after scaling.
            # Split drops balance below reserve → must REFUSE even when empty.
            result = manager._check_topup_reserve_guards(
                name="CAT-inner",
                wallet_id=2,
                pool_amount_mojos=10_000_000,
                is_cat=True,
                tier_is_empty=True,
            )
        self.assertFalse(result)
        reserve_calls = [c for c in log_spy.call_args_list
                         if len(c.args) >= 2
                         and "blocked_by_reserve" in str(c.args[1])]
        self.assertTrue(reserve_calls, "Expected hard reserve to refuse even for empty tier")

    # ------------------------------------------------------------------
    # XCH bypass (symmetric)
    # ------------------------------------------------------------------

    def test_xch_empty_tier_bypasses_exhausted_budget(self):
        manager = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "XCH_RESERVE", Decimal("0")), \
             patch.object(cfg, "TOPUP_POOL_XCH", Decimal("1")), \
             patch("wallet.get_wallet_balance",
                   _FakeWalletBalance(total_mojos=100_000_000_000_000)), \
             patch("database.get_setting",
                   return_value=str(900_000_000_000)), \
             patch.object(coin_manager, "log_event") as log_spy:
            # Budget=1 XCH = 1e12 mojos, spent=0.9 XCH, request=0.5 XCH
            # 0.9 + 0.5 > 1.0 → would block. Empty tier → bypass.
            result = manager._check_topup_reserve_guards(
                name="XCH-inner",
                wallet_id=1,
                pool_amount_mojos=500_000_000_000,
                is_cat=False,
                tier_is_empty=True,
            )
        self.assertTrue(result)
        bypass_calls = [c for c in log_spy.call_args_list
                        if len(c.args) >= 2
                        and "budget_bypass_empty_tier" in str(c.args[1])]
        self.assertTrue(bypass_calls)


if __name__ == "__main__":
    unittest.main()
