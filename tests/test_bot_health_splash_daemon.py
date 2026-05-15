"""Tests for the Splash daemon health check in bot_health.

The check classifies three silent-failure modes that all look identical
from the Market Intel "0 received" counter:

    A. Metrics endpoint unreachable (daemon down or misconfigured)
    B. Daemon running but zero peers (network/firewall issue)
    C. Daemon has peers + has seen offers but webhook delivered 0
       (the --offer-hook is broken — the exact bug found in v0.2.0 during
       the 2026-04-21 session)
"""

import sys
import types
import unittest
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


class _FakeSplashNode:
    """Stand-in for SplashNode that returns a fixed metrics snapshot."""

    def __init__(self, metrics: dict):
        self._metrics = metrics

    def get_metrics(self):
        return dict(self._metrics)


class _FakeBot:
    def __init__(self, splash_node=None):
        self.splash_node = splash_node


class _FakeEventBus:
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


class SplashDaemonTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)
        sys.modules.pop("api_server", None)

    def _install(self, *, splash_node, receive_enabled=True, delivered_total=0):
        """Install fake api_server + patch splash-incoming DB stats.

        Returns (bus, cleanup_callable). Cleanup resets module state.
        """
        bus = _FakeEventBus()
        fake_api = types.ModuleType("api_server")
        fake_api.events = bus
        fake_api.bot = _FakeBot(splash_node=splash_node)
        sys.modules["api_server"] = fake_api

        cfg = bot_health.cfg
        patchers = [
            patch.object(cfg, "SPLASH_RECEIVE_ENABLED", receive_enabled),
            patch.object(cfg, "CAT_ASSET_ID", "abc123"),
            patch(
                "database.get_splash_incoming_stats",
                return_value={"total": delivered_total},
            ),
        ]
        for p in patchers:
            p.start()

        def cleanup():
            for p in patchers:
                p.stop()
            sys.modules.pop("api_server", None)

        return bus, cleanup

    # ------------------------------------------------------------------
    # Healthy daemon
    # ------------------------------------------------------------------

    def test_healthy_daemon_no_alerts(self):
        node = _FakeSplashNode(
            {
                "reachable": True,
                "peers": 5,
                "offers_received": 120,
                "offers_broadcasted": 46,
            }
        )
        bus, cleanup = self._install(splash_node=node, delivered_total=120)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.anomaly_count, 0)
        self.assertEqual(bus.alerts, {})

    # ------------------------------------------------------------------
    # Case A: unreachable
    # ------------------------------------------------------------------

    def test_unreachable_metrics_raises_alert(self):
        node = _FakeSplashNode(
            {
                "reachable": False,
                "last_error": "connection refused",
                "metrics_url": "http://127.0.0.1:4001/metrics",
                "peers": 0,
                "offers_received": 0,
                "offers_broadcasted": 0,
            }
        )
        bus, cleanup = self._install(splash_node=node)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "warn")
        self.assertIn("splash_unreachable", bus.alerts)
        self.assertIn("metrics", bus.alerts["splash_unreachable"]["title"].lower())

    # ------------------------------------------------------------------
    # Case B: no peers
    # ------------------------------------------------------------------

    def test_no_peers_raises_alert(self):
        node = _FakeSplashNode(
            {
                "reachable": True,
                "peers": 0,
                "offers_received": 0,
                "offers_broadcasted": 0,
            }
        )
        bus, cleanup = self._install(splash_node=node)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "warn")
        self.assertIn("splash_no_peers", bus.alerts)
        self.assertNotIn("splash_hook_broken", bus.alerts)

    # ------------------------------------------------------------------
    # Case C: hook broken — the 2026-04-21 bug
    # ------------------------------------------------------------------

    def test_hook_broken_raises_alert(self):
        """5 peers, 100 offers seen by daemon, 0 delivered to webhook."""
        node = _FakeSplashNode(
            {
                "reachable": True,
                "peers": 5,
                "offers_received": 100,
                "offers_broadcasted": 46,
            }
        )
        bus, cleanup = self._install(splash_node=node, delivered_total=0)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "warn")
        self.assertIn("splash_hook_broken", bus.alerts)
        alert = bus.alerts["splash_hook_broken"]
        self.assertIn("100", alert["message"])  # offers seen
        self.assertIn("offer-hook", alert["message"])
        self.assertNotIn("splash_no_peers", bus.alerts)
        self.assertNotIn("splash_unreachable", bus.alerts)

    def test_hook_gap_under_threshold_does_not_alert(self):
        """5 offers seen is below _SPLASH_HOOK_MIN_SEEN → be patient."""
        node = _FakeSplashNode(
            {
                "reachable": True,
                "peers": 5,
                "offers_received": 5,  # below the 10 threshold
                "offers_broadcasted": 46,
            }
        )
        bus, cleanup = self._install(splash_node=node, delivered_total=0)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "pass")
        self.assertNotIn("splash_hook_broken", bus.alerts)

    # ------------------------------------------------------------------
    # Scope: receive disabled → no noise
    # ------------------------------------------------------------------

    def test_receive_disabled_skips_all_checks(self):
        node = _FakeSplashNode(
            {
                "reachable": False,
                "peers": 0,
                "offers_received": 0,
                "offers_broadcasted": 0,
            }
        )
        bus, cleanup = self._install(splash_node=node, receive_enabled=False)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()

        self.assertEqual(check.status, "pass")
        self.assertEqual(bus.alerts, {})

    # ------------------------------------------------------------------
    # Recovery: once delivered > 0, the hook_broken alert clears
    # ------------------------------------------------------------------

    def test_hook_alert_clears_on_first_delivery(self):
        node = _FakeSplashNode(
            {
                "reachable": True,
                "peers": 5,
                "offers_received": 100,
                "offers_broadcasted": 46,
            }
        )
        # First pass: 0 delivered → alert raised.
        bus, cleanup = self._install(splash_node=node, delivered_total=0)
        try:
            bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup()
        self.assertIn("splash_hook_broken", bus.alerts)

        # Second pass: 1 delivered → alert cleared.
        bus2, cleanup2 = self._install(splash_node=node, delivered_total=1)
        try:
            check = bot_health.check_splash_daemon(auto_repair=True)
        finally:
            cleanup2()
        self.assertEqual(check.status, "pass")
        self.assertNotIn("splash_hook_broken", bus2.alerts)
        self.assertIn("splash_hook_broken", bus2.cleared)


if __name__ == "__main__":
    unittest.main()
