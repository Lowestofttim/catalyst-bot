"""Tests for the Spacescan cache self-heal in bot_health.

The Spacescan cache has two TTLs: 24h for healthy data, 30min for partial
(activity silently failed). Nothing triggers the retry automatically —
after the 30min window, the dashboard shows "Unknown/Unknown/—" until
the user re-runs Smart Settings manually. `check_spacescan_cache_stale`
closes that gap by dispatching a background refresh on the normal 60s
bot_health cadence.
"""

import sys
import types
import time
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


class SpacescanCacheTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)

    def setUp(self):
        bot_health._spacescan_refresh_inflight = False
        bot_health._spacescan_refresh_last_at = 0.0

    # ------------------------------------------------------------------
    # Skip when disabled / no CAT
    # ------------------------------------------------------------------

    def test_disabled_returns_pass(self):
        cfg = bot_health.cfg
        with patch.object(cfg, "SPACESCAN_ENABLED", False):
            check = bot_health.check_spacescan_cache_stale(auto_repair=True)
        self.assertEqual(check.status, "pass")

    def test_no_asset_id_returns_pass(self):
        cfg = bot_health.cfg
        with (
            patch.object(cfg, "SPACESCAN_ENABLED", True),
            patch.object(cfg, "CAT_ASSET_ID", ""),
        ):
            check = bot_health.check_spacescan_cache_stale(auto_repair=True)
        self.assertEqual(check.status, "pass")
        self.assertIn("No CAT", check.message)

    # ------------------------------------------------------------------
    # Warm cache → no refresh
    # ------------------------------------------------------------------

    def test_warm_cache_skips_refresh(self):
        cfg = bot_health.cfg
        cached = {"has_data": True, "holder_count": 3412}
        refresh_called = {"fired": False}

        def _fake_refresh(_asset_id):
            refresh_called["fired"] = True
            return None

        with (
            patch.object(cfg, "SPACESCAN_ENABLED", True),
            patch.object(cfg, "CAT_ASSET_ID", "b8edcc6a"),
            patch("database.get_market_analysis_cache", return_value=cached),
        ):
            check = bot_health.check_spacescan_cache_stale(auto_repair=True)

        self.assertEqual(check.status, "pass")
        self.assertIn("warm", check.message.lower())
        self.assertFalse(refresh_called["fired"])

    # ------------------------------------------------------------------
    # Expired cache → background refresh fires once
    # ------------------------------------------------------------------

    def test_expired_cache_dispatches_refresh(self):
        cfg = bot_health.cfg
        refresh_called = {"count": 0, "asset_id": None}

        def _fake_refresh(asset_id):
            refresh_called["count"] += 1
            refresh_called["asset_id"] = asset_id
            return {"has_data": True, "holder_count": 3412, "activity_count": 100}

        fake_module = types.ModuleType("market_data_collector")
        fake_module.refresh_spacescan_cache = _fake_refresh
        sys.modules["market_data_collector"] = fake_module

        try:
            with (
                patch.object(cfg, "SPACESCAN_ENABLED", True),
                patch.object(cfg, "CAT_ASSET_ID", "b8edcc6a"),
                patch("database.get_market_analysis_cache", return_value=None),
            ):
                check = bot_health.check_spacescan_cache_stale(auto_repair=True)
                # Wait briefly for the background thread to run.
                time.sleep(0.2)
        finally:
            sys.modules.pop("market_data_collector", None)

        self.assertEqual(check.status, "pass")
        self.assertEqual(check.repaired_count, 1)
        self.assertEqual(refresh_called["count"], 1)
        self.assertEqual(refresh_called["asset_id"], "b8edcc6a")

    # ------------------------------------------------------------------
    # Rate-limit: two rapid calls → only one refresh dispatched
    # ------------------------------------------------------------------

    def test_rate_limit_prevents_double_fire(self):
        cfg = bot_health.cfg
        refresh_called = {"count": 0}

        def _slow_refresh(asset_id):
            refresh_called["count"] += 1
            time.sleep(0.1)  # slow enough that second call sees inflight
            return None

        fake_module = types.ModuleType("market_data_collector")
        fake_module.refresh_spacescan_cache = _slow_refresh
        sys.modules["market_data_collector"] = fake_module

        try:
            with (
                patch.object(cfg, "SPACESCAN_ENABLED", True),
                patch.object(cfg, "CAT_ASSET_ID", "b8edcc6a"),
                patch("database.get_market_analysis_cache", return_value=None),
            ):
                c1 = bot_health.check_spacescan_cache_stale(auto_repair=True)
                c2 = bot_health.check_spacescan_cache_stale(auto_repair=True)
                time.sleep(0.3)  # let refresh complete
        finally:
            sys.modules.pop("market_data_collector", None)

        # Second call must see either the "inflight" or "throttled" path
        # and NOT dispatch a second refresh.
        self.assertEqual(refresh_called["count"], 1)
        self.assertEqual(c1.repaired_count, 1)
        self.assertEqual(c2.repaired_count, 0)

    # ------------------------------------------------------------------
    # Read-only mode: reports anomaly without dispatching
    # ------------------------------------------------------------------

    def test_read_only_reports_without_refreshing(self):
        cfg = bot_health.cfg
        refresh_called = {"count": 0}

        def _fake_refresh(asset_id):
            refresh_called["count"] += 1
            return None

        fake_module = types.ModuleType("market_data_collector")
        fake_module.refresh_spacescan_cache = _fake_refresh
        sys.modules["market_data_collector"] = fake_module

        try:
            with (
                patch.object(cfg, "SPACESCAN_ENABLED", True),
                patch.object(cfg, "CAT_ASSET_ID", "b8edcc6a"),
                patch("database.get_market_analysis_cache", return_value=None),
            ):
                check = bot_health.check_spacescan_cache_stale(auto_repair=False)
                time.sleep(0.1)
        finally:
            sys.modules.pop("market_data_collector", None)

        self.assertEqual(check.status, "warn")
        self.assertEqual(check.anomaly_count, 1)
        self.assertEqual(refresh_called["count"], 0)


if __name__ == "__main__":
    unittest.main()
