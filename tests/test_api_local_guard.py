import unittest
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

try:
    import api_server
    import sage_node
    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    api_server = None
    sage_node = None
    _IMPORT_ERROR = exc


@unittest.skipIf(api_server is None or sage_node is None, f"api_server import unavailable: {_IMPORT_ERROR}")
class TestApiLocalGuard(unittest.TestCase):
    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.loopback = {"REMOTE_ADDR": "127.0.0.1"}
        api_server._rate_limit_log.clear()

    def test_root_injects_local_token(self):
        resp = self.client.get("/", environ_base=self.loopback)
        body = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("window.__BOT_LOCAL_TOKEN", body)

    def test_debug_routes_are_disabled(self):
        resp = self.client.get("/api/debug/pricing", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 404)

    def test_post_requires_local_token(self):
        resp = self.client.post("/api/bot/stop", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 401)

    def test_events_require_local_token(self):
        resp = self.client.get("/api/events", environ_base=self.loopback)
        self.assertEqual(resp.status_code, 401)

    def test_post_with_token_reaches_handler(self):
        resp = self.client.post(
            "/api/bot/stop",
            headers={"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN},
            environ_base=self.loopback,
        )
        self.assertNotEqual(resp.status_code, 401)

    def test_splash_incoming_is_token_exempt_for_loopback(self):
        with patch.object(api_server.cfg, "SPLASH_RECEIVE_ENABLED", True), \
                patch.object(api_server, "bot", None), \
                patch("database.record_splash_incoming", return_value=True):
            resp = self.client.post(
                "/api/splash/incoming",
                json={"offer": "offer1qqqq"},
                environ_base=self.loopback,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])

    def test_splash_incoming_is_not_hit_by_generic_rate_limit(self):
        with patch.object(api_server.cfg, "SPLASH_RECEIVE_ENABLED", True), \
                patch.object(api_server, "bot", None), \
                patch("database.record_splash_incoming", return_value=False):
            statuses = []
            for i in range(25):
                resp = self.client.post(
                    "/api/splash/incoming",
                    json={"offer": f"offer1qqqq{i}"},
                    environ_base=self.loopback,
                )
                statuses.append(resp.status_code)
        self.assertNotIn(401, statuses)
        self.assertNotIn(429, statuses)

    def test_open_external_rejects_non_http_urls(self):
        resp = self.client.post(
            "/api/open-external",
            json={"url": "file:///C:/Windows/system32/calc.exe"},
            headers={"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN},
            environ_base=self.loopback,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "Only absolute http/https URLs are allowed")

    def test_open_external_uses_system_browser_for_http_urls(self):
        with patch.object(api_server.webbrowser, "open", return_value=True) as mock_open:
            resp = self.client.post(
                "/api/open-external",
                json={"url": "https://sagewallet.net/"},
                headers={"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN},
                environ_base=self.loopback,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        mock_open.assert_called_once_with("https://sagewallet.net/", new=2)

    def test_smart_defaults_orderbook_uses_dexie_v1_params(self):
        calls = []

        fake_requests = ModuleType("requests")

        def fake_get(url, params=None, timeout=None):
            calls.append((url, dict(params or {}), timeout))
            return SimpleNamespace(status_code=200, json=lambda: {"offers": []})

        fake_requests.get = fake_get

        with patch.dict(sys.modules, {"requests": fake_requests}):
            api_server._fetch_dexie_orderbook_standalone("test-cat")

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1].get("offered_asset_id"), "test-cat")
        self.assertNotIn("offered", calls[0][1])
        self.assertEqual(calls[1][1].get("requested_asset_id"), "test-cat")
        self.assertNotIn("requested", calls[1][1])

    def test_quote_setting_update_returns_next_requote_notice(self):
        headers = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
        fake_bot = SimpleNamespace(is_running=lambda: True)
        with patch.object(api_server, "bot", fake_bot), patch.object(api_server.cfg, "update", return_value=True):
            resp = self.client.post(
                "/api/config",
                json={"key": "BASE_SPREAD_BPS", "value": "920"},
                headers=headers,
                environ_base=self.loopback,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body.get("apply_mode"), "next_requote")
        self.assertIn("future requotes and new offers", body.get("warning", ""))

    def test_sage_change_address_setting_remains_api_updatable(self):
        self.assertIn("SAGE_SET_CHANGE_ADDRESS", api_server.cfg._UPDATABLE_KEYS)

    def test_coin_prep_status_complete_uses_total_counts_and_keeps_free_counts(self):
        headers = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
        fake_state = {
            "running": False,
            "complete": True,
            "error": None,
            "phase": "complete",
            "run_id": "run-1",
            "started_at": "2026-03-29T15:34:02+00:00",
        }
        fake_status = {
            "phase": "complete",
            "progress": 1.0,
            "message": "Complete!",
            "xch_coins_current": 111,
            "cat_coins_current": 91,
            "xch_coins_target": 111,
            "cat_coins_target": 91,
            "run_id": "run-1",
        }
        fake_summary = {
            "xch_free_count": 72,
            "xch_total": 112,
            "cat_free_count": 52,
            "cat_total": 92,
        }

        with patch.dict(api_server._coin_prep_state, fake_state, clear=True), \
                patch.object(api_server.os.path, "exists", side_effect=lambda p: p.endswith("coin_prep_status.json")), \
                patch.object(api_server, "bot", None), \
                patch.object(api_server, "_session_start_time", None), \
                patch("api_server.json.load", return_value=fake_status), \
                patch("builtins.open"), \
                patch("database.get_coin_summary", return_value=fake_summary), \
                patch("database.get_events_since", return_value=[]), \
                patch("database.get_recent_events", return_value=[]):
            resp = self.client.get("/api/coin-prep/status", headers=headers, environ_base=self.loopback)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["complete"])
        self.assertEqual(body["phase"], "complete")
        self.assertEqual(body["xch_target"], 111)
        self.assertEqual(body["cat_target"], 91)
        self.assertEqual(body["xch_coins"], 112)
        self.assertEqual(body["cat_coins"], 92)
        self.assertEqual(body["xch_free_coins"], 72)
        self.assertEqual(body["cat_free_coins"], 52)

    def test_sage_start_route_returns_unsupported_version_error(self):
        blocked = {
            "success": False,
            "unsupported_version": True,
            "error": "Sage v0.12.9 is too old.",
            "sage_version": "0.12.9",
            "sage_min_required_version": "0.12.10",
        }
        with patch("chia_node.trigger_start", return_value=blocked):
            resp = self.client.post(
                "/api/sage/start-with-fingerprint",
                json={"fingerprint": "1234567890"},
                headers={"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN},
                environ_base=self.loopback,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["unsupported_version"])
        self.assertEqual(body["sage_min_required_version"], "0.12.10")


if __name__ == "__main__":
    unittest.main()
