"""Tests for the topup request auto-scale in coin_manager.

When TOPUP_POOL_* is smaller than a full refill cost, the classic behaviour
was to let the budget guard refuse every request — leaving the tier
permanently short even while the budget had partial headroom. The
auto-scale clamps num_to_create to what the remaining budget can fund,
so partial refills make forward progress over multiple cycles.
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


class TopupBudgetAutoscaleTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)

    def _make_manager(self):
        with patch.object(coin_manager.CoinManager, "_resolve_fingerprint",
                          return_value="123456789"):
            return coin_manager.CoinManager()

    # ------------------------------------------------------------------
    # Unlimited budget → no cap, returns None
    # ------------------------------------------------------------------

    def test_unlimited_budget_returns_none(self):
        m = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("0")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")):
            self.assertIsNone(
                m._max_coins_within_topup_budget(
                    is_cat=False, trading_size_mojos=1_000_000_000_000))
            self.assertIsNone(
                m._max_coins_within_topup_budget(
                    is_cat=True, trading_size_mojos=10_000))

    # ------------------------------------------------------------------
    # Budget with room → returns remaining / trading_size
    # ------------------------------------------------------------------

    def test_cat_budget_scales_down_to_fit(self):
        """The 2026-04-21 scenario: budget=140,621 CAT, size=18,840.
        Full refill needs 9×18,840=169,560 → too big. Auto-scale gives 7."""
        m = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "TOPUP_POOL_CAT", Decimal("140.6219260")), \
             patch.object(cfg, "CAT_DECIMALS", 3), \
             patch("database.get_setting", return_value="0"):
            # budget = 140.6219260 CAT × 1000 = 140,621 mojos
            # trading_size = 18,840 mojos
            # max coins = 140,621 // 18,840 = 7
            result = m._max_coins_within_topup_budget(
                is_cat=True, trading_size_mojos=18_840)
        self.assertEqual(result, 7)

    def test_xch_budget_scales_down_to_fit(self):
        m = self._make_manager()
        cfg = coin_manager.cfg
        # TOPUP_POOL_XCH=10 XCH, spent=7 XCH → 3 XCH remaining
        # trading_size = 0.66253 XCH → max coins = 3e12 // 662_530_000_000 = 4
        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("10")), \
             patch("database.get_setting", return_value=str(7 * 10**12)):
            result = m._max_coins_within_topup_budget(
                is_cat=False, trading_size_mojos=662_530_000_000)
        self.assertEqual(result, 4)

    # ------------------------------------------------------------------
    # Budget exhausted → returns 0, caller skips
    # ------------------------------------------------------------------

    def test_exhausted_budget_returns_zero(self):
        m = self._make_manager()
        cfg = coin_manager.cfg
        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("5")), \
             patch("database.get_setting", return_value=str(6 * 10**12)):
            # spent > budget → remaining = 0 → 0 coins
            result = m._max_coins_within_topup_budget(
                is_cat=False, trading_size_mojos=1_000_000_000_000)
        self.assertEqual(result, 0)

    def test_partially_spent_budget_still_fits_some(self):
        m = self._make_manager()
        cfg = coin_manager.cfg
        # 60 XCH budget, 58 spent → 2 XCH left → fits 3 coins × 0.66 XCH
        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("60")), \
             patch("database.get_setting", return_value=str(58 * 10**12)):
            result = m._max_coins_within_topup_budget(
                is_cat=False, trading_size_mojos=662_530_000_000)
        self.assertEqual(result, 3)

    # ------------------------------------------------------------------
    # Defensive: bad trading size → returns None
    # ------------------------------------------------------------------

    def test_zero_trading_size_returns_none(self):
        m = self._make_manager()
        self.assertIsNone(
            m._max_coins_within_topup_budget(
                is_cat=False, trading_size_mojos=0))
        self.assertIsNone(
            m._max_coins_within_topup_budget(
                is_cat=False, trading_size_mojos=-1))

    # ------------------------------------------------------------------
    # Defensive: DB error on get_setting → treats spent as 0
    # ------------------------------------------------------------------

    def test_db_error_assumes_zero_spent(self):
        m = self._make_manager()
        cfg = coin_manager.cfg

        def _raise(*a, **kw):
            raise RuntimeError("DB offline")

        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("10")), \
             patch("database.get_setting", side_effect=_raise):
            # budget=10, spent treated as 0 → 10 coins at 1 XCH each
            result = m._max_coins_within_topup_budget(
                is_cat=False, trading_size_mojos=1_000_000_000_000)
        self.assertEqual(result, 10)


if __name__ == "__main__":
    unittest.main()
