"""Tests for the low-funds advisory check in bot_health.

Emits a persistent alert via the AlertStore when the wallet doesn't have
enough spendable XCH or CAT above the hard reserve to support even one
inner-tier refill split. Auto-clears when the balance climbs back above
the operating floor.
"""

import sys
import sqlite3
import types
import unittest
from contextlib import ExitStack
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


class _FakeEventBus:
    """Capture AlertStore calls in-memory."""

    def __init__(self):
        self.alerts = {}
        self.cleared = []
        self._alert_store = self

    def alert(
        self,
        alert_id,
        severity,
        title,
        message,
        action=None,
        action_label=None,
        action_value=None,
    ):
        self.alerts[alert_id] = {
            "id": alert_id,
            "severity": severity,
            "title": title,
            "message": message,
        }

    def set_alert(self, alert_id, severity, title, message, *args, **kwargs):
        self.alert(alert_id, severity, title, message)

    def clear(self, alert_id):
        self.cleared.append(alert_id)
        self.alerts.pop(alert_id, None)


class FundsAdvisoryTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # Leave bot_health / config / database loaded: tearing them down
        # and letting another test class re-import them triggers subtle
        # timing issues with module-level state (e.g. the bot_health
        # `_last_report` throttle cache). Stubs are safe to pop.
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)
        # Clear our fake api_server specifically so drift tests don't
        # inherit it.
        sys.modules.pop("api_server", None)

    def _install_fake_bus(self):
        """Install a fake event bus accessible via `from api_server import events`."""
        bus = _FakeEventBus()
        fake_api = types.ModuleType("api_server")
        fake_api.events = bus
        sys.modules["api_server"] = fake_api
        return bus

    def _uninstall_fake_bus(self):
        sys.modules.pop("api_server", None)

    # ------------------------------------------------------------------
    # Healthy wallet → no alert
    # ------------------------------------------------------------------

    def test_healthy_wallet_no_alert(self):
        """Spendable >> operating floor → pass, no alert raised."""
        bus = self._install_fake_bus()
        try:
            cfg = bot_health.cfg
            # Spendable 100 XCH, reserve 10 XCH, floor ~ 2×0.6 = 1.2 XCH
            balance = {"wallet_balance": {"spendable_balance": 100 * 10**12}}
            with (
                patch.object(cfg, "XCH_RESERVE", Decimal("10")),
                patch.object(cfg, "CAT_RESERVE", Decimal("0")),
                patch.object(cfg, "SELL_INNER_SIZE_XCH", Decimal("0.6023")),
                patch.object(cfg, "WALLET_ID_XCH", 1),
                patch.object(cfg, "CAT_WALLET_ID", 2),
                patch.object(cfg, "WALLET_ADDRESS", "xch1test..."),
                patch("wallet.get_wallet_type", return_value="sage"),
                patch("wallet_sage.get_wallet_balance", return_value=balance),
            ):
                check = bot_health.check_funds_advisory(auto_repair=True)

            self.assertEqual(check.status, "pass")
            self.assertEqual(check.anomaly_count, 0)
            self.assertNotIn("funds_advisory_xch", bus.alerts)
        finally:
            self._uninstall_fake_bus()

    # ------------------------------------------------------------------
    # Low XCH → alert raised with address + suggested amount
    # ------------------------------------------------------------------

    def test_low_xch_raises_alert_with_address(self):
        """Spendable below floor → alert with send-to address and amount."""
        bus = self._install_fake_bus()
        try:
            cfg = bot_health.cfg
            # Spendable 10.05 XCH, reserve 10 XCH → available 0.05 XCH.
            # Floor = 2×0.6023 + 0.01 = 1.2146 XCH. 0.05 < 1.21 → alert.
            balance = {"wallet_balance": {"spendable_balance": 10_050_000_000_000}}
            with (
                patch.object(cfg, "XCH_RESERVE", Decimal("10")),
                patch.object(cfg, "CAT_RESERVE", Decimal("0")),
                patch.object(cfg, "SELL_INNER_SIZE_XCH", Decimal("0.6023")),
                patch.object(cfg, "WALLET_ID_XCH", 1),
                patch.object(cfg, "CAT_WALLET_ID", 2),
                patch.object(cfg, "WALLET_ADDRESS", "xch1demo123..."),
                patch("wallet.get_wallet_type", return_value="sage"),
                patch("wallet_sage.get_wallet_balance", return_value=balance),
            ):
                check = bot_health.check_funds_advisory(auto_repair=True)

            self.assertEqual(check.status, "warn")
            self.assertGreaterEqual(check.anomaly_count, 1)
            self.assertIn("funds_advisory_xch", bus.alerts)
            alert = bus.alerts["funds_advisory_xch"]
            self.assertEqual(alert["severity"], "warning")
            self.assertIn("XCH", alert["title"])
            self.assertIn("xch1demo123...", alert["message"])
            # Suggested amount ≥ 5 × 0.6023 ≈ 3.01 XCH
            self.assertIn("Send at least", alert["message"])
        finally:
            self._uninstall_fake_bus()

    # ------------------------------------------------------------------
    # Recovery → alert auto-clears when funds replenished
    # ------------------------------------------------------------------

    def test_alert_auto_clears_when_funds_restored(self):
        """After the user tops up, the next check clears the alert."""
        bus = self._install_fake_bus()
        try:
            cfg = bot_health.cfg
            # First call: low.
            low_balance = {"wallet_balance": {"spendable_balance": 10_050_000_000_000}}
            with (
                patch.object(cfg, "XCH_RESERVE", Decimal("10")),
                patch.object(cfg, "CAT_RESERVE", Decimal("0")),
                patch.object(cfg, "SELL_INNER_SIZE_XCH", Decimal("0.6023")),
                patch.object(cfg, "WALLET_ID_XCH", 1),
                patch.object(cfg, "CAT_WALLET_ID", 2),
                patch.object(cfg, "WALLET_ADDRESS", "xch1..."),
                patch("wallet.get_wallet_type", return_value="sage"),
                patch("wallet_sage.get_wallet_balance", return_value=low_balance),
            ):
                bot_health.check_funds_advisory(auto_repair=True)

            self.assertIn("funds_advisory_xch", bus.alerts)

            # Second call: funds restored.
            healthy_balance = {"wallet_balance": {"spendable_balance": 50 * 10**12}}
            with (
                patch.object(cfg, "XCH_RESERVE", Decimal("10")),
                patch.object(cfg, "CAT_RESERVE", Decimal("0")),
                patch.object(cfg, "SELL_INNER_SIZE_XCH", Decimal("0.6023")),
                patch.object(cfg, "WALLET_ID_XCH", 1),
                patch.object(cfg, "CAT_WALLET_ID", 2),
                patch.object(cfg, "WALLET_ADDRESS", "xch1..."),
                patch("wallet.get_wallet_type", return_value="sage"),
                patch("wallet_sage.get_wallet_balance", return_value=healthy_balance),
            ):
                check = bot_health.check_funds_advisory(auto_repair=True)

            self.assertEqual(check.status, "pass")
            self.assertNotIn("funds_advisory_xch", bus.alerts)
            self.assertIn("funds_advisory_xch", bus.cleared)
        finally:
            self._uninstall_fake_bus()

    # ------------------------------------------------------------------
    # Read-only mode
    # ------------------------------------------------------------------

    def test_read_only_mode_reports_but_no_alert(self):
        """auto_repair=False reports findings but does NOT emit an alert."""
        bus = self._install_fake_bus()
        try:
            cfg = bot_health.cfg
            low_balance = {"wallet_balance": {"spendable_balance": 10_050_000_000_000}}
            with (
                patch.object(cfg, "XCH_RESERVE", Decimal("10")),
                patch.object(cfg, "CAT_RESERVE", Decimal("0")),
                patch.object(cfg, "SELL_INNER_SIZE_XCH", Decimal("0.6023")),
                patch.object(cfg, "WALLET_ID_XCH", 1),
                patch.object(cfg, "CAT_WALLET_ID", 2),
                patch.object(cfg, "WALLET_ADDRESS", "xch1..."),
                patch("wallet.get_wallet_type", return_value="sage"),
                patch("wallet_sage.get_wallet_balance", return_value=low_balance),
            ):
                check = bot_health.check_funds_advisory(auto_repair=False)

            # Condition still detected and reported...
            self.assertEqual(check.status, "warn")
            self.assertGreaterEqual(check.anomaly_count, 1)
            # ...but no user-visible alert was pushed.
            self.assertNotIn("funds_advisory_xch", bus.alerts)
        finally:
            self._uninstall_fake_bus()

    # ------------------------------------------------------------------
    # Tier-aware CAT shortage -> alert even when total CAT spendable is high
    # ------------------------------------------------------------------

    def test_cat_tier_reserve_shortage_raises_alert_despite_high_spendable(self):
        """CAT reserve too small for missing inner spares -> dashboard alert."""
        bus = self._install_fake_bus()
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE coins (
                wallet_type TEXT,
                status TEXT,
                designation TEXT,
                assigned_tier TEXT,
                amount_mojos INTEGER
            )
            """
        )
        # Live-like shape: no free CAT-inner spares, reserve can fund only
        # about two 45,084 CAT inner coins, while other CAT coins make the
        # whole-wallet spendable balance look healthy.
        conn.executemany(
            "INSERT INTO coins VALUES (?, ?, ?, ?, ?)",
            [
                ("cat", "free", "reserve", "none", 81_178_000),
                ("cat", "free", "reserve", "none", 14_154_603),
                ("cat", "free", "tier_spare", "mid", 18_000_000),
                ("cat", "free", "tier_spare", "outer", 12_000_000),
                ("cat", "free", "tier_spare", "extreme", 7_000_000),
                ("cat", "free", "tier_spare", "sniper", 90_000),
                ("cat", "locked", "tier_active", "inner", 45_084_000),
            ],
        )
        try:
            cfg = bot_health.cfg
            high_cat_balance = {"wallet_balance": {"spendable_balance": 700_000_000}}
            high_xch_balance = {"wallet_balance": {"spendable_balance": 100 * 10**12}}

            def _balance(wallet_id):
                return high_cat_balance if int(wallet_id) == 2 else high_xch_balance

            with ExitStack() as stack:
                for name, value in {
                    "TIER_ENABLED": True,
                    "ENABLE_SELL": True,
                    "MAX_ACTIVE_BUY_OFFERS": 24,
                    "MAX_ACTIVE_SELL_OFFERS": 24,
                    "SELL_INNER_TIER_COUNT": 7,
                    "SELL_MID_TIER_COUNT": 7,
                    "SELL_OUTER_TIER_COUNT": 6,
                    "SELL_EXTREME_TIER_COUNT": 4,
                    "SELL_INNER_TIER_SPARE_COUNT": 10,
                    "SELL_MID_TIER_SPARE_COUNT": 0,
                    "SELL_OUTER_TIER_SPARE_COUNT": 0,
                    "SELL_EXTREME_TIER_SPARE_COUNT": 0,
                    "SELL_INNER_SIZE_XCH": Decimal("4.5"),
                    "LAST_QUOTED_MID": Decimal("0.0001"),
                    "COIN_PREP_MULTIPLIER": Decimal("1.0"),
                    "CAT_RESERVE": Decimal("0"),
                    "CAT_DECIMALS": 3,
                    "CAT_NAME": "Monkeyzoo Token",
                    "CAT_WALLET_ID": 2,
                    "WALLET_ID_XCH": 1,
                    "WALLET_ADDRESS": "xch1demo123...",
                }.items():
                    stack.enter_context(patch.object(cfg, name, value, create=True))
                stack.enter_context(
                    patch("wallet.get_wallet_type", return_value="sage")
                )
                stack.enter_context(
                    patch("wallet_sage.get_wallet_balance", side_effect=_balance)
                )
                stack.enter_context(patch("database.get_connection", return_value=conn))
                stack.enter_context(
                    patch(
                        "coin_manager.get_tier_sizes_mojos_from_cfg",
                        return_value={
                            "inner": 45_084_000,
                            "mid": 18_000_000,
                            "outer": 12_000_000,
                            "extreme": 7_000_000,
                        },
                    )
                )
                check = bot_health.check_funds_advisory(auto_repair=True)

            self.assertEqual(check.status, "warn")
            self.assertIn("funds_advisory_cat", bus.alerts)
            alert = bus.alerts["funds_advisory_cat"]
            self.assertEqual(alert["severity"], "warning")
            self.assertIn("CAT-inner", alert["message"])
            self.assertIn("0/10", alert["message"])
            self.assertIn("topup pool", alert["message"])
        finally:
            conn.close()
            self._uninstall_fake_bus()


if __name__ == "__main__":
    unittest.main()
