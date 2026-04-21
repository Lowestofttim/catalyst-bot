"""Tests for the topup-pool budget drift check in bot_health.

Scenario from the 2026-04-21 session: the cumulative `topup_pool_xch_spent_mojos`
counter drifted above reality after misfit absorption returned coins to the
reserve without a matching credit-back. The budget guard then refused every
tier refill ("blocked_by_budget") even though the reserve physically held
coins.

The topup-refund fix in coin_manager.py handles FUTURE absorptions, but
legacy drift persists across restarts because the counter lives in the
bot_settings DB. `check_topup_budget_drift` is the periodic self-healer
that clamps the counter back to observed reality.
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


import bot_health


class TopupBudgetDriftTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)
        for name in list(sys.modules):
            if name in ("bot_health", "config", "database", "super_log"):
                sys.modules.pop(name, None)

    # ------------------------------------------------------------------
    # Healthy: stored counter matches observed reserve
    # ------------------------------------------------------------------

    def test_no_drift_passes_cleanly(self):
        """Counter within tolerance → pass, no repair, no anomaly."""
        cfg = bot_health.cfg
        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("60")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")), \
             patch("database.get_setting",
                   return_value=str(40 * 10**12)), \
             patch("bot_health._reserve_mojos", return_value=20 * 10**12):
            # Budget 60 XCH, reserve 20 XCH, observed spend = 40 XCH.
            # Stored = 40 XCH → drift=0 → pass.
            check = bot_health.check_topup_budget_drift(auto_repair=True)

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.anomaly_count, 0)
        self.assertEqual(check.repaired_count, 0)

    # ------------------------------------------------------------------
    # Drift (the 2026-04-21 bug case)
    # ------------------------------------------------------------------

    def test_xch_drift_is_healed_on_auto_repair(self):
        """Stored counter 62.5 XCH with reserve 17.5 XCH (budget 63.6) →
        observed spend 46.1 XCH → heal down."""
        cfg = bot_health.cfg
        stored = {"topup_pool_xch_spent_mojos": str(62_544_900_000_000)}

        def _fake_get(key, default="0"):
            return stored.get(key, default)

        def _fake_set(key, value):
            stored[key] = value

        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("63.6378")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")), \
             patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set), \
             patch("bot_health._reserve_mojos",
                   return_value=17_552_200_000_000):
            check = bot_health.check_topup_budget_drift(auto_repair=True)

        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(check.repaired_count, 1)
        # Stored counter must now equal the observed depletion.
        expected = 63_637_800_000_000 - 17_552_200_000_000
        self.assertEqual(stored["topup_pool_xch_spent_mojos"], str(expected))

    def test_cat_drift_is_healed_independently(self):
        """CAT counter drift heals without touching XCH counter."""
        cfg = bot_health.cfg
        stored = {
            "topup_pool_cat_spent_mojos": str(500_000_000),   # 500k CAT
            "topup_pool_xch_spent_mojos": str(10_000_000_000_000),  # 10 XCH
        }

        def _fake_get(key, default="0"):
            return stored.get(key, default)

        def _fake_set(key, value):
            stored[key] = value

        def _reserve(wallet_type):
            return 100_000 if wallet_type == "cat" else 0  # 100 CAT × 10^3 scale

        with patch.object(cfg, "TOPUP_POOL_CAT", Decimal("300")), \
             patch.object(cfg, "TOPUP_POOL_XCH", Decimal("0")), \
             patch.object(cfg, "CAT_DECIMALS", 3), \
             patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set), \
             patch("bot_health._reserve_mojos", side_effect=_reserve):
            # Budget 300 CAT × 1000 = 300,000 mojos, reserve 100,000.
            # Observed spend = 200,000. Stored = 500,000 → drift = 300,000.
            check = bot_health.check_topup_budget_drift(auto_repair=True)

        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(check.repaired_count, 1)
        self.assertEqual(stored["topup_pool_cat_spent_mojos"], str(200_000))
        # XCH must remain untouched.
        self.assertEqual(stored["topup_pool_xch_spent_mojos"],
                         str(10_000_000_000_000))

    # ------------------------------------------------------------------
    # Auto-repair gating
    # ------------------------------------------------------------------

    def test_read_only_mode_reports_drift_without_fixing(self):
        """auto_repair=False must report anomaly but leave the counter alone."""
        cfg = bot_health.cfg
        stored = {"topup_pool_xch_spent_mojos": str(62_544_900_000_000)}

        def _fake_get(key, default="0"):
            return stored.get(key, default)

        def _fake_set(key, value):
            stored[key] = value

        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("63.6378")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")), \
             patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set), \
             patch("bot_health._reserve_mojos",
                   return_value=17_552_200_000_000):
            check = bot_health.check_topup_budget_drift(auto_repair=False)

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.severity, "warning")
        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(check.repaired_count, 0)
        # Unchanged.
        self.assertEqual(stored["topup_pool_xch_spent_mojos"],
                         str(62_544_900_000_000))

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    def test_never_adjusts_counter_upward(self):
        """If stored counter is BELOW observed (e.g. pool over-used but
        counter never recorded the spend), the check does NOT raise it.
        That would widen the allowance beyond Smart Settings config."""
        cfg = bot_health.cfg
        stored = {"topup_pool_xch_spent_mojos": str(10_000_000_000_000)}  # 10 XCH

        def _fake_get(key, default="0"):
            return stored.get(key, default)

        def _fake_set(key, value):
            stored[key] = value

        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("60")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")), \
             patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set), \
             patch("bot_health._reserve_mojos",
                   return_value=20 * 10**12):  # reserve 20 → observed 40
            check = bot_health.check_topup_budget_drift(auto_repair=True)

        # stored=10, observed=40, drift=-30 (negative) → no action
        self.assertEqual(check.anomaly_count, 0)
        self.assertEqual(stored["topup_pool_xch_spent_mojos"],
                         str(10_000_000_000_000))

    def test_unlimited_budget_skips_check(self):
        """TOPUP_POOL_XCH=0 means unlimited — drift is meaningless there."""
        cfg = bot_health.cfg
        with patch.object(cfg, "TOPUP_POOL_XCH", Decimal("0")), \
             patch.object(cfg, "TOPUP_POOL_CAT", Decimal("0")), \
             patch("database.get_setting", return_value=str(99 * 10**12)), \
             patch("bot_health._reserve_mojos", return_value=0):
            check = bot_health.check_topup_budget_drift(auto_repair=True)

        self.assertEqual(check.anomaly_count, 0)
        self.assertEqual(check.status, "pass")


if __name__ == "__main__":
    unittest.main()
