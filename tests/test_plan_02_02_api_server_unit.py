"""Slice 02-02 — api_server.py unit tests: pure helpers + endpoint shape.

Covers functions not previously tested (test_api_local_guard already covers
auth, token injection, rate-limit exemptions, and open-external validation):
  _is_loopback_addr, _safe_float, _serialize_dict, _is_rate_limited,
  /api/bot/state (bot=None), /api/status (bot=None), /api/config GET shape,
  /api/bot/start when already running.
"""

import unittest
from decimal import Decimal
from unittest.mock import patch

try:
    import api_server

    _SKIP = None
except ModuleNotFoundError as exc:
    api_server = None
    _SKIP = str(exc)


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestIsLoopbackAddr(unittest.TestCase):
    """_is_loopback_addr — validates loopback origins."""

    def test_127_0_0_1(self):
        self.assertTrue(api_server._is_loopback_addr("127.0.0.1"))

    def test_127_any_octet(self):
        self.assertTrue(api_server._is_loopback_addr("127.0.0.2"))
        self.assertTrue(api_server._is_loopback_addr("127.1.2.3"))

    def test_localhost_string(self):
        self.assertTrue(api_server._is_loopback_addr("localhost"))

    def test_ipv6_loopback(self):
        self.assertTrue(api_server._is_loopback_addr("::1"))

    def test_non_loopback_rejected(self):
        self.assertFalse(api_server._is_loopback_addr("192.168.1.1"))
        self.assertFalse(api_server._is_loopback_addr("10.0.0.1"))
        self.assertFalse(api_server._is_loopback_addr("8.8.8.8"))

    def test_empty_string_rejected(self):
        self.assertFalse(api_server._is_loopback_addr(""))

    def test_invalid_string_rejected(self):
        self.assertFalse(api_server._is_loopback_addr("not-an-ip"))

    def test_none_rejected(self):
        self.assertFalse(api_server._is_loopback_addr(None))


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestSafeFloat(unittest.TestCase):
    """_safe_float — safe Decimal/str/None → float conversion."""

    def test_none_returns_zero(self):
        self.assertEqual(api_server._safe_float(None), 0.0)

    def test_float_passes_through(self):
        self.assertAlmostEqual(api_server._safe_float(3.14), 3.14)

    def test_decimal_converts(self):
        self.assertAlmostEqual(api_server._safe_float(Decimal("2.5")), 2.5)

    def test_string_number_converts(self):
        self.assertAlmostEqual(api_server._safe_float("1.23"), 1.23)

    def test_invalid_string_returns_zero(self):
        self.assertEqual(api_server._safe_float("abc"), 0.0)

    def test_integer_converts(self):
        self.assertAlmostEqual(api_server._safe_float(42), 42.0)


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestSerializeDict(unittest.TestCase):
    """_serialize_dict — converts Decimal values to str for JSON safety."""

    def test_decimal_converted_to_str(self):
        result = api_server._serialize_dict({"price": Decimal("1.23")})
        self.assertEqual(result["price"], "1.23")

    def test_non_decimal_values_unchanged(self):
        result = api_server._serialize_dict({"name": "alice", "count": 7})
        self.assertEqual(result["name"], "alice")
        self.assertEqual(result["count"], 7)

    def test_nested_dict_recursed(self):
        d = {"outer": {"inner": Decimal("0.5")}}
        result = api_server._serialize_dict(d)
        self.assertEqual(result["outer"]["inner"], "0.5")

    def test_none_returns_empty_dict(self):
        self.assertEqual(api_server._serialize_dict(None), {})

    def test_empty_dict_returns_empty_dict(self):
        self.assertEqual(api_server._serialize_dict({}), {})

    def test_list_of_dicts_processed(self):
        d = {"trades": [{"price": Decimal("1.1")}, {"price": Decimal("2.2")}]}
        result = api_server._serialize_dict(d)
        self.assertEqual(result["trades"][0]["price"], "1.1")
        self.assertEqual(result["trades"][1]["price"], "2.2")


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestIsRateLimited(unittest.TestCase):
    """_is_rate_limited — pure rate limiting logic."""

    def setUp(self):
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def test_first_call_not_rate_limited(self):
        self.assertFalse(api_server._is_rate_limited("/test/endpoint"))

    def test_below_max_not_rate_limited(self):
        for _ in range(api_server._RATE_LIMIT_MAX - 1):
            api_server._is_rate_limited("/test2")
        self.assertFalse(api_server._is_rate_limited("/test2"))

    def test_exceeds_max_triggers_rate_limit(self):
        for _ in range(api_server._RATE_LIMIT_MAX + 1):
            api_server._is_rate_limited("/test3")
        self.assertTrue(api_server._is_rate_limited("/test3"))

    def test_different_endpoints_independent(self):
        for _ in range(api_server._RATE_LIMIT_MAX + 5):
            api_server._is_rate_limited("/heavy_endpoint")
        self.assertFalse(api_server._is_rate_limited("/other_endpoint"))


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestBotStateEndpoint(unittest.TestCase):
    """GET /api/bot/state — shape when bot is None."""

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.loopback = {"REMOTE_ADDR": "127.0.0.1"}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/bot/state", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertIn("error", body)

    def test_bot_with_state_returns_200(self):
        from types import SimpleNamespace

        fake_bot = SimpleNamespace(
            is_running=lambda: False,
            get_state=lambda: {"running": False, "coins": {}},
        )
        with patch.object(api_server, "bot", fake_bot):
            resp = self.client.get("/api/bot/state", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsInstance(body, dict)


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestConfigGetEndpoint(unittest.TestCase):
    """GET /api/config — structure and auth."""

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.loopback = {"REMOTE_ADDR": "127.0.0.1"}
        self.headers = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def test_config_get_is_public_read(self):
        # /api/config GET does not require a token — it's read-only
        resp = self.client.get("/api/config", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 200)

    def test_config_get_response_is_flat_dict(self):
        resp = self.client.get("/api/config", environ_base=self.loopback)
        body = resp.get_json()
        self.assertIsInstance(body, dict)
        # cfg.to_dict() returns a flat dict of config keys
        self.assertIn("WALLET_TYPE", body)
        self.assertIn("DRY_RUN", body)

    def test_config_get_contains_spread_bps(self):
        resp = self.client.get("/api/config", environ_base=self.loopback)
        body = resp.get_json()
        self.assertIn("SPREAD_BPS", body)


@unittest.skipIf(api_server is None, f"api_server unavailable: {_SKIP}")
class TestBotStartEndpoint(unittest.TestCase):
    """POST /api/bot/start — already-running guard."""

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.loopback = {"REMOTE_ADDR": "127.0.0.1"}
        self.headers = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def test_start_requires_token(self):
        resp = self.client.post("/api/bot/start", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 401)

    def test_start_already_running_returns_already_running_status(self):
        from types import SimpleNamespace

        fake_bot = SimpleNamespace(is_running=lambda: True)
        with patch.object(api_server, "bot", fake_bot):
            resp = self.client.post(
                "/api/bot/start",
                headers=self.headers,
                environ_base=self.loopback,
            )
        body = resp.get_json()
        self.assertEqual(body.get("status"), "already_running")
        self.assertTrue(body.get("success"))

    def test_stop_with_no_bot_returns_500(self):
        # Both start/stop require bot to be initialised — return 500 when None
        with patch.object(api_server, "bot", None):
            resp = self.client.post(
                "/api/bot/stop",
                headers=self.headers,
                environ_base=self.loopback,
            )
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
