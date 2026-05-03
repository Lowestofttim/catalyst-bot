import importlib.util
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

try:
    import requests  # type: ignore
except ModuleNotFoundError:
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class HTTPError(RequestException):
        pass

    class DummySession:
        def __init__(self):
            self.headers = {}

        def get(self, *args, **kwargs):
            raise NotImplementedError("test should patch session.get")

    requests.Session = DummySession
    requests.HTTPError = HTTPError
    requests.exceptions = types.SimpleNamespace(
        Timeout=Timeout,
        RequestException=RequestException,
        HTTPError=HTTPError,
    )
    sys.modules["requests"] = requests

try:
    import dotenv  # type: ignore
except ModuleNotFoundError:
    dotenv = types.ModuleType("dotenv")

    def _noop(*args, **kwargs):
        return None

    dotenv.load_dotenv = _noop
    dotenv.set_key = _noop
    sys.modules["dotenv"] = dotenv


def _load_real_module(name: str, filename: str):
    root = Path(__file__).parent.parent
    module_path = root / filename
    if not module_path.exists():
        module_path = root / "src" / "catalyst" / filename
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if "config" not in sys.modules or not hasattr(getattr(sys.modules["config"], "cfg", object()), "SPACESCAN_TIMEOUT"):
    _load_real_module("config", "config.py")

if "database" not in sys.modules or not hasattr(sys.modules["database"], "get_connection"):
    _load_real_module("database", "database.py")

import database
import market_data_collector as mdc


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class SpacescanCollectorTests(unittest.TestCase):
    def setUp(self):
        self.orig_key = getattr(mdc.cfg, "SPACESCAN_API_KEY", "")
        self.orig_timeout = getattr(mdc.cfg, "SPACESCAN_TIMEOUT", 10)
        mdc._spacescan_smart_last_call_at.clear()
        mdc._spacescan_smart_cooldown_until.clear()
        mdc._spacescan_smart_last_warned.clear()

    def tearDown(self):
        mdc.cfg.SPACESCAN_API_KEY = self.orig_key
        mdc.cfg.SPACESCAN_TIMEOUT = self.orig_timeout
        mdc._spacescan_smart_last_call_at.clear()
        mdc._spacescan_smart_cooldown_until.clear()
        mdc._spacescan_smart_last_warned.clear()

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_spacescan_smart_get_stops_retrying_after_429(self, _sleep):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(url)
            return FakeResponse(status_code=429, data={})

        with patch.object(mdc._session, "get", side_effect=fake_get):
            data, err = mdc._spacescan_smart_get(
                mdc.cfg.SPACESCAN_FREE_URL,
                "/token/holders/asset123",
                retries=2,
            )
            data2, err2 = mdc._spacescan_smart_get(
                mdc.cfg.SPACESCAN_FREE_URL,
                "/token/holders/asset123",
                retries=2,
            )

        self.assertIsNone(data)
        self.assertEqual(err, "HTTP 429")
        self.assertIsNone(data2)
        self.assertIn("cooldown", err2)
        self.assertEqual(len(calls), 1)

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_fetch_spacescan_data_prefers_pro_and_uses_fallback_activity_route(self, _sleep):
        mdc.cfg.SPACESCAN_API_KEY = "test-key"
        mdc.cfg.SPACESCAN_TIMEOUT = 15
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
            })

            if url.endswith("/token/info/asset123"):
                return FakeResponse(data={
                    "status": "success",
                    "info": {"name": "Monkeyzoo", "symbol": "MZ", "precision": 3},
                    "price": {"usd": 0.01, "xch": 0.000125},
                    "supply": {"total_supply": 1000, "circulating_supply": 750},
                })

            if url.endswith("/token/holders/asset123"):
                return FakeResponse(data={
                    "status": "success",
                    "tokens": [{"address": "xch1..."}],
                    "total_count": 321,
                })

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/activity":
                raise requests.exceptions.Timeout("timed out")

            if url == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/activity":
                return FakeResponse(data={
                    "status": "success",
                    "tokens": [{"id": 1}, {"id": 2}, {"id": 3}],
                })

            raise AssertionError(f"Unexpected URL {url}")

        with patch.object(mdc._session, "get", side_effect=fake_get):
            result = mdc._fetch_spacescan_data("asset123")

        self.assertTrue(result["has_data"])
        self.assertEqual(result["name"], "Monkeyzoo")
        self.assertEqual(result["holder_count"], 321)
        # F39 (2026-04-08): activity fetching runs AFTER the holders block.
        # F77 (2026-04-17): endpoint order reworked to pro-legacy then free.
        # The often-404'ing pro-plural route is intentionally not retried.
        # Retries bumped from 1 to 2 for better transient-failure resilience.
        # In this test: pro-legacy (/token/activity) times out 3 times (retries=2),
        # then free (/token/activity) succeeds on first try with
        # 3 items, so activity_count=3.
        self.assertEqual(result["activity_count"], 3)
        self.assertEqual(calls[0]["url"], f"{mdc.cfg.SPACESCAN_PRO_URL}/token/info/asset123")
        # Holders endpoint uses count=1 to minimise response size (only total_count needed)
        holders_calls = [c for c in calls if "/token/holders/" in c["url"]]
        self.assertTrue(len(holders_calls) > 0)
        self.assertEqual(holders_calls[0]["params"], {"count": 1})
        # Activity calls: 3 pro-legacy timeouts (retries=2) + 1 free success = 4 total.
        activity_calls = [
            c for c in calls
            if "/token/activity" in c["url"] or "/token/activities/" in c["url"]
        ]
        self.assertEqual(len(activity_calls), 4)
        self.assertEqual(activity_calls[-1]["url"], f"{mdc.cfg.SPACESCAN_FREE_URL}/token/activity")
        self.assertEqual(activity_calls[-1]["params"], {
            "asset_id": "asset123",
            "type": "transfer",
            "count": 100,
        })
        # Free tier succeeded, so pro-plural should NOT appear in the call log.
        pro_plural_calls = [
            c for c in activity_calls
            if mdc.cfg.SPACESCAN_PRO_URL in c["url"]
            and c["url"].endswith("/token/activities/asset123")
        ]
        self.assertEqual(len(pro_plural_calls), 0)
        self.assertEqual(calls[0]["headers"]["x-api-key"], "test-key")
        self.assertEqual(calls[0]["headers"]["version"], "v1")
        self.assertEqual(calls[0]["headers"]["network"], "xch")
        self.assertEqual(calls[0]["timeout"], (5, 20))

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_fetch_spacescan_data_skips_known_bad_pro_plural_after_free_rate_limit(self, _sleep):
        mdc.cfg.SPACESCAN_API_KEY = "test-key"
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({
                "url": url,
                "params": dict(params or {}),
            })

            if url.endswith("/token/info/asset123"):
                return FakeResponse(data={
                    "status": "success",
                    "info": {"name": "Monkeyzoo", "symbol": "MZ", "precision": 3},
                    "price": {"usd": 0.01, "xch": 0.000125},
                    "supply": {"total_supply": 1000, "circulating_supply": 750},
                })

            if url.endswith("/token/holders/asset123"):
                return FakeResponse(data={
                    "status": "success",
                    "tokens": [{"address": "xch1..."}],
                    "total_count": 321,
                })

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/activity":
                return FakeResponse(data={"status": "success", "data": []})

            if url == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/activity":
                return FakeResponse(status_code=429, data={})

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/activities/asset123":
                return FakeResponse(status_code=404, data={})

            raise AssertionError(f"Unexpected URL {url}")

        with patch.object(mdc._session, "get", side_effect=fake_get):
            result = mdc._fetch_spacescan_data("asset123")

        self.assertTrue(result["has_data"])
        self.assertEqual(result["holder_count"], 321)
        self.assertTrue(result.get("activity_fetch_failed"))
        pro_plural_calls = [
            c for c in calls
            if c["url"] == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/activities/asset123"
        ]
        self.assertEqual(pro_plural_calls, [])

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_fetch_spacescan_data_counts_pro_activity_tokens_without_free_fallback(self, _sleep):
        mdc.cfg.SPACESCAN_API_KEY = "test-key"
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({
                "url": url,
                "params": dict(params or {}),
            })

            if url.endswith("/token/info/asset123"):
                return FakeResponse(data={
                    "status": "success",
                    "info": {"name": "Monkeyzoo", "symbol": "MZ", "precision": 3},
                    "price": {"usd": 0.01, "xch": 0.000125},
                    "supply": {"total_supply": 1000, "circulating_supply": 750},
                })

            if url.endswith("/token/holders/asset123"):
                return FakeResponse(data={
                    "status": "success",
                    "tokens": [{"address": "xch1..."}],
                    "total_count": 321,
                })

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/activity":
                return FakeResponse(data={
                    "status": "success",
                    "tokens": [{"coin_id": "coin1"}, {"coin_id": "coin2"}],
                })

            if url == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/activity":
                return FakeResponse(data={
                    "status": "success",
                    "tokens": [{"id": "free"}],
                })

            raise AssertionError(f"Unexpected URL {url}")

        with patch.object(mdc._session, "get", side_effect=fake_get):
            result = mdc._fetch_spacescan_data("asset123")

        self.assertTrue(result["has_data"])
        self.assertEqual(result["activity_count"], 2)
        free_activity_calls = [
            c for c in calls
            if c["url"] == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/activity"
        ]
        self.assertEqual(free_activity_calls, [])

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_fetch_spacescan_data_falls_back_to_free_info(self, _sleep):
        mdc.cfg.SPACESCAN_API_KEY = "test-key"
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append(url)

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/info/asset123":
                raise requests.exceptions.Timeout("timed out")

            if url == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/info/asset123":
                return FakeResponse(data={
                    "status": "success",
                    "info": {"name": "Monkeyzoo", "symbol": "MZ", "precision": 3},
                    "price": {"usd": 0.01, "xch": 0.000125},
                    "supply": {"total_supply": 1000, "circulating_supply": 750},
                })

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/holders/asset123":
                return FakeResponse(data={"status": "success", "data": []})

            if url == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/holders/asset123":
                return FakeResponse(data={"status": "success", "data": []})

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/token/activity":
                return FakeResponse(data={"status": "success", "data": []})

            if url == f"{mdc.cfg.SPACESCAN_FREE_URL}/token/activity":
                return FakeResponse(data={"status": "success", "data": []})

            raise AssertionError(f"Unexpected URL {url}")

        with patch.object(mdc._session, "get", side_effect=fake_get):
            result = mdc._fetch_spacescan_data("asset123")

        self.assertTrue(result["has_data"])
        self.assertEqual(calls[:3], [
            f"{mdc.cfg.SPACESCAN_PRO_URL}/token/info/asset123",
            f"{mdc.cfg.SPACESCAN_PRO_URL}/token/info/asset123",
            f"{mdc.cfg.SPACESCAN_FREE_URL}/token/info/asset123",
        ])

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_xch_usd_price_falls_back_to_spacescan_when_coingecko_fails(self, _sleep):
        mdc.cfg.SPACESCAN_API_KEY = "test-key"
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
            })

            if url == f"{mdc.COINGECKO_BASE}/simple/price":
                return FakeResponse(status_code=503, data={})

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/stats/price":
                return FakeResponse(data={
                    "status": "success",
                    "price": 2.5,
                    "timestamp": 1770000000,
                })

            raise AssertionError(f"Unexpected URL {url}")

        with patch.object(mdc._session, "get", side_effect=fake_get):
            result = mdc._fetch_xch_usd_price()

        self.assertTrue(result["has_data"])
        self.assertEqual(result["source"], "spacescan")
        self.assertEqual(result["xch_usd"], 2.5)
        self.assertEqual(calls[1]["params"], {"cur": "USD"})
        self.assertEqual(calls[1]["headers"]["x-api-key"], "test-key")

    @patch("market_data_collector.time.sleep", return_value=None)
    def test_fetch_spacescan_enhanced_data_summarises_fee_and_cat_transactions(self, _sleep):
        mdc.cfg.SPACESCAN_API_KEY = "test-key"
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            calls.append({
                "url": url,
                "params": dict(params or {}),
            })

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/mempool/minfee":
                return FakeResponse(data={
                    "status": "success",
                    "data": [
                        {"timestamp": 1770000000, "minfees": 3, "fee": 9},
                        {"timestamp": 1770000060, "minfees": 5, "fee": 10},
                    ],
                })

            if url == f"{mdc.cfg.SPACESCAN_PRO_URL}/cat/transactions/asset123":
                return FakeResponse(data={
                    "status": "success",
                    "data": [
                        {"coin_name": "coin1", "timestamp": 1770000001},
                        {"coin_name": "coin2", "timestamp": 1770000002},
                    ],
                })

            raise AssertionError(f"Unexpected URL {url}")

        with patch.object(mdc._session, "get", side_effect=fake_get):
            result = mdc._fetch_spacescan_enhanced_data("asset123")

        self.assertTrue(result["has_data"])
        self.assertEqual(result["mempool_sample_count"], 2)
        self.assertEqual(result["mempool_min_fee"], 5.0)
        self.assertEqual(result["cat_tx_count"], 2)
        self.assertEqual(result["cat_last_tx_timestamp"], 1770000002)
        self.assertEqual(calls[1]["params"], {"count": 25})


class MarketAnalysisCacheTests(unittest.TestCase):
    def test_clear_market_analysis_cache_can_keep_spacescan(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE market_analysis_cache ("
            "asset_id TEXT, analysis_type TEXT, data_json TEXT, "
            "expires_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.executemany(
            "INSERT INTO market_analysis_cache (asset_id, analysis_type, data_json, expires_at) "
            "VALUES (?, ?, '{}', datetime('now', '+1 day'))",
            [
                ("asset123", "spacescan"),
                ("asset123", "dexie_ticker"),
                ("asset123", "dexie_trades"),
                ("other", "spacescan"),
            ],
        )
        conn.commit()

        with patch.object(database, "get_connection", return_value=conn):
            deleted = database.clear_market_analysis_cache(
                "asset123",
                keep_analysis_types=("spacescan",),
            )

        remaining = conn.execute(
            "SELECT asset_id, analysis_type FROM market_analysis_cache ORDER BY asset_id, analysis_type"
        ).fetchall()
        self.assertEqual(deleted, 2)
        self.assertEqual(remaining, [
            ("asset123", "spacescan"),
            ("other", "spacescan"),
        ])


if __name__ == "__main__":
    unittest.main()
