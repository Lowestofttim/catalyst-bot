"""Tests for the unclaimed-deposit advisory check in bot_health.

Detection rules (must all hold):
  - free / designation='reserve' coin
  - amount >= 10 × smallest trading tier size
  - first_seen within the last 15 minutes
  - coin_id not in deposit_advisory_advised_coins
  - no misfit absorption happened for this wallet type in the last 90s
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


class _FakeRow(dict):
    """sqlite3.Row-style accessor so rows behave like both dict and Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _FakeConn:
    """Mock sqlite conn that returns the provided rows only when the
    query's wallet_type arg matches the row's wallet_type key. The real
    check_unclaimed_deposits runs one query per wallet_type (xch, cat)
    and without filtering the mock would return the same row twice."""

    def __init__(self, rows):
        # Each row is a _FakeRow with a 'wallet_type' key we can filter on.
        self._rows = rows
        self._last_wallet_type = None

    def execute(self, sql, params=None):
        # First positional param is wallet_type in our query shape.
        if params:
            try:
                self._last_wallet_type = str(params[0]).lower()
            except Exception:
                self._last_wallet_type = None
        return self

    def fetchall(self):
        if self._last_wallet_type is None:
            return list(self._rows)
        return [r for r in self._rows
                if str(r.get("wallet_type", "cat")).lower()
                == self._last_wallet_type]


class _FakeEventBus:
    def __init__(self):
        self.alerts = {}
        self.cleared = []
        self._alert_store = self

    def alert(self, alert_id, severity, title, message,
              action=None, action_label=None, action_value=None):
        self.alerts[alert_id] = {
            "id": alert_id, "severity": severity,
            "title": title, "message": message,
            "action": action, "action_label": action_label,
            "action_value": action_value,
        }

    def set_alert(self, alert_id, severity, title, message, *args, **kwargs):
        self.alert(alert_id, severity, title, message,
                   action=kwargs.get("action"),
                   action_label=kwargs.get("action_label"),
                   action_value=kwargs.get("action_value"))

    def clear(self, alert_id):
        self.cleared.append(alert_id)
        self.alerts.pop(alert_id, None)

    def get_active(self):
        return [dict(v) for v in self.alerts.values()]


class UnclaimedDepositsTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)
        sys.modules.pop("api_server", None)

    def _install(self, *, rows=None, advised="", last_absorb_ts=0,
                 cat_reserve="50"):
        """Wire up fake DB, event bus, cfg. Returns (bus, cleanup)."""
        rows = rows or []
        bus = _FakeEventBus()
        fake_api = types.ModuleType("api_server")
        fake_api.events = bus
        sys.modules["api_server"] = fake_api

        # get_setting returns advised list + absorb timestamps.
        def _fake_get_setting(key, default=""):
            if key == "deposit_advisory_advised_coins":
                return advised
            if "_absorb_" in key:
                return str(last_absorb_ts)
            return default

        cfg = bot_health.cfg
        patchers = [
            patch("database.get_connection", return_value=_FakeConn(rows)),
            patch("database.get_setting", side_effect=_fake_get_setting),
            patch.object(cfg, "ENABLE_BUY", True),
            patch.object(cfg, "ENABLE_SELL", True),
            patch.object(cfg, "CAT_DECIMALS", 3),
            patch.object(cfg, "CAT_NAME", "MZ"),
            patch.object(cfg, "CAT_RESERVE", Decimal(str(cat_reserve))),
            patch.object(cfg, "BUY_INNER_SIZE_XCH", Decimal("0.6023")),
            patch.object(cfg, "SELL_INNER_SIZE_XCH", Decimal("0.6023")),
            patch.object(cfg, "INNER_SIZE_XCH", Decimal("0.6023")),
            patch.object(cfg, "TOPUP_POOL_XCH", Decimal("60")),
            patch.object(cfg, "TOPUP_POOL_CAT", Decimal("140")),
            patch.object(cfg, "XCH_RESERVE", Decimal("10")),
        ]
        for p in patchers:
            p.start()

        def cleanup():
            for p in patchers:
                p.stop()
            sys.modules.pop("api_server", None)

        return bus, cleanup

    # ------------------------------------------------------------------
    # Happy path: a fresh big reserve coin raises an alert
    # ------------------------------------------------------------------

    def test_large_fresh_reserve_raises_alert(self):
        # Single matching row — a 750k CAT coin landed 2 min ago.
        row = _FakeRow({
            "coin_id": "0x0ed57fad82d8283b902e4fcc63cbabd2",
            "amount_mojos": 750_000_000,   # 750,000 CAT at 3 decimals
            "first_seen": "2026-04-21 20:13:01",
            "wallet_type": "cat",
        })
        bus, cleanup = self._install(rows=[row])
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.anomaly_count, 1)
        alert_id = "deposit_advisory_0x0ed57fad82d8283b902e4fcc63cbabd2"
        self.assertIn(alert_id, bus.alerts)
        alert = bus.alerts[alert_id]
        self.assertEqual(alert["action"], "allocate_deposit")
        self.assertEqual(alert["action_label"], "Allocate")
        # action_value must carry the coin_id, amount, budget/reserve keys
        parts = alert["action_value"].split("|")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "cat")
        self.assertEqual(parts[1], "0x0ed57fad82d8283b902e4fcc63cbabd2")
        self.assertEqual(int(parts[2]), 750_000_000)
        self.assertEqual(parts[4], "TOPUP_POOL_CAT")
        self.assertEqual(parts[5], "CAT_RESERVE")

    # ------------------------------------------------------------------
    # Already-advised coin: no re-prompt
    # ------------------------------------------------------------------

    def test_advised_coin_is_skipped(self):
        row = _FakeRow({
            "coin_id": "0xdeadbeef",
            "amount_mojos": 750_000_000,
            "first_seen": "2026-04-21 20:13:01",
            "wallet_type": "cat",
        })
        bus, cleanup = self._install(
            rows=[row], advised="0xdeadbeef,0xcafebabe")
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "pass")
        self.assertEqual(bus.alerts, {})

    # ------------------------------------------------------------------
    # Cooldown: recent misfit absorption suppresses alert
    # ------------------------------------------------------------------

    def test_recent_absorb_suppresses_alert(self):
        row = _FakeRow({
            "coin_id": "0xfeed",
            "amount_mojos": 750_000_000,
            "first_seen": "2026-04-21 20:13:01",
            "wallet_type": "cat",
        })
        import time as _t
        # Absorption happened 30s ago — within the 90s cooldown.
        last = int(_t.time()) - 30
        bus, cleanup = self._install(rows=[row], last_absorb_ts=last)
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "pass")
        self.assertEqual(bus.alerts, {})

    # ------------------------------------------------------------------
    # Empty results: no-op, no alerts raised or left
    # ------------------------------------------------------------------

    def test_no_deposits_returns_pass(self):
        bus, cleanup = self._install(rows=[])
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()
        self.assertEqual(check.status, "pass")
        self.assertEqual(bus.alerts, {})

    # ------------------------------------------------------------------
    # Stale alert cleanup: coin that was advised since → clear the alert
    # ------------------------------------------------------------------

    def test_stale_alert_is_cleared_after_allocation(self):
        # Simulate: alert was previously raised for 0xdeadbeef, user
        # allocated it (so now on advised list), DB no longer returns
        # it as a candidate. The check should CLEAR the dangling alert.
        row = _FakeRow({
            "coin_id": "0xdeadbeef",
            "amount_mojos": 750_000_000,
            "first_seen": "2026-04-21 20:13:01",
            "wallet_type": "cat",
        })
        # First call: raise alert.
        bus, cleanup = self._install(rows=[row])
        try:
            bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()
        self.assertIn("deposit_advisory_0xdeadbeef", bus.alerts)

        # Second call: user has allocated → now on advised list, so
        # the DB-side filter would normally still return it but the
        # in-memory advised-set check short-circuits. With no matching
        # live coin in the check's returned set, the stale-cleanup
        # block clears the alert.
        # We pass the same alert through a new bus that inherits the
        # stale entry to prove the clear path works.
        bus2 = _FakeEventBus()
        bus2.alerts["deposit_advisory_0xdeadbeef"] = dict(
            bus.alerts["deposit_advisory_0xdeadbeef"])
        fake_api2 = types.ModuleType("api_server")
        fake_api2.events = bus2
        sys.modules["api_server"] = fake_api2

        cfg = bot_health.cfg
        patchers = [
            patch("database.get_connection", return_value=_FakeConn([row])),
            patch("database.get_setting",
                  side_effect=lambda k, d="": (
                      "0xdeadbeef" if k == "deposit_advisory_advised_coins"
                      else "0")),
            patch.object(cfg, "ENABLE_BUY", True),
            patch.object(cfg, "ENABLE_SELL", True),
            patch.object(cfg, "CAT_DECIMALS", 3),
            patch.object(cfg, "CAT_NAME", "MZ"),
            patch.object(cfg, "CAT_RESERVE", Decimal("50")),
            patch.object(cfg, "INNER_SIZE_XCH", Decimal("0.6023")),
            patch.object(cfg, "TOPUP_POOL_XCH", Decimal("60")),
            patch.object(cfg, "TOPUP_POOL_CAT", Decimal("140")),
            patch.object(cfg, "XCH_RESERVE", Decimal("10")),
        ]
        for p in patchers:
            p.start()
        try:
            bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            for p in patchers:
                p.stop()
            sys.modules.pop("api_server", None)

        # The stale alert must have been cleared.
        self.assertNotIn("deposit_advisory_0xdeadbeef", bus2.alerts)
        self.assertIn("deposit_advisory_0xdeadbeef", bus2.cleared)

    # ------------------------------------------------------------------
    # Stale deposit (pre-restart): still surfaces
    # ------------------------------------------------------------------

    def test_deposit_from_before_restart_still_prompts(self):
        """A coin first_seen hours ago, never advised on, must still
        trigger the prompt. Removing the freshness window was the fix —
        this test pins the behaviour so it doesn't regress."""
        # Row with a first_seen from yesterday — way past any plausible
        # time window.
        row = _FakeRow({
            "coin_id": "0xancient",
            "amount_mojos": 750_000_000,
            "first_seen": "2026-04-20 08:00:00",
            "wallet_type": "cat",
        })
        bus, cleanup = self._install(rows=[row])
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()
        self.assertEqual(check.anomaly_count, 1)
        self.assertIn("deposit_advisory_0xancient", bus.alerts)

    # ------------------------------------------------------------------
    # Flood cap: too many candidates → only the first N surface at once
    # ------------------------------------------------------------------

    def test_alert_flood_is_capped_per_pass(self):
        """When there are 20 pending reserves, only _MAX_ALERTS_PER_PASS
        fire this pass. The rest surface on subsequent passes as those
        are allocated."""
        rows = [_FakeRow({
            "coin_id": f"0xcoin{i:03d}",
            "amount_mojos": 750_000_000,
            "first_seen": "2026-04-21 20:13:01",
            "wallet_type": "cat",
        }) for i in range(20)]
        bus, cleanup = self._install(rows=rows)
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=True)
        finally:
            cleanup()
        self.assertEqual(check.anomaly_count,
                         bot_health._DEPOSIT_ADVISORY_MAX_ALERTS_PER_PASS)

    # ------------------------------------------------------------------
    # Read-only mode: detects but doesn't emit
    # ------------------------------------------------------------------

    def test_read_only_mode_reports_without_alerting(self):
        row = _FakeRow({
            "coin_id": "0xbeef",
            "amount_mojos": 750_000_000,
            "first_seen": "2026-04-21 20:13:01",
            "wallet_type": "cat",
        })
        bus, cleanup = self._install(rows=[row])
        try:
            check = bot_health.check_unclaimed_deposits(auto_repair=False)
        finally:
            cleanup()
        self.assertEqual(check.status, "warn")
        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(bus.alerts, {})  # no emission in read-only mode


if __name__ == "__main__":
    unittest.main()
