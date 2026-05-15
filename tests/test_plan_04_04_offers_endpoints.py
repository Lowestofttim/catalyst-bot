"""Slice 04-04 — offers endpoint contract tests.

Tests /api/offers, /api/offers/cancel_all/status, /api/offers/open_count,
/api/offers/cancel_all (POST), /api/offers/cancel (POST):
  - Auth required for write endpoints
  - bot=None → 500 for bot-dependent reads
  - Response shapes and required keys
  - Input validation (missing trade_id, bad JSON)
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _FlaskBase(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        self.auth = {"X-Bot-Local-Token": self.token}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def _post(self, path, body=None, auth=True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body or {},
            headers=headers,
            environ_base=self._LOOPBACK,
        )


def _make_bot(offers=([], [], [])):
    bot = MagicMock()
    bot.is_running.return_value = True
    bot.offer_manager.sync_from_wallet.return_value = offers
    bot.offer_manager.cancel_all.return_value = {"cancelled": [], "failed": []}
    bot.offer_manager.cancel_offers.return_value = {"success": True}
    bot.coin_manager.is_busy.return_value = False
    return bot


# ---------------------------------------------------------------------------
# 1. GET /api/offers
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestOffersGet(_FlaskBase):
    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self.client.get("/api/offers", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 500)

    def test_bot_set_returns_200(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/offers", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_buys_sells_counts(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.get("/api/offers", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("buys", body)
        self.assertIn("sells", body)
        self.assertIn("buy_count", body)
        self.assertIn("sell_count", body)

    def test_empty_offers_returns_zero_counts(self):
        with patch.object(api_server, "bot", _make_bot(offers=([], [], []))):
            resp = self.client.get("/api/offers", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertEqual(body["buy_count"], 0)
        self.assertEqual(body["sell_count"], 0)


# ---------------------------------------------------------------------------
# 2. GET /api/offers/cancel_all/status
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCancelAllStatus(_FlaskBase):
    def test_returns_200_always(self):
        resp = self.client.get(
            "/api/offers/cancel_all/status", environ_base=self._LOOPBACK
        )
        self.assertEqual(resp.status_code, 200)

    def test_response_has_success_key(self):
        resp = self.client.get(
            "/api/offers/cancel_all/status", environ_base=self._LOOPBACK
        )
        body = resp.get_json()
        self.assertTrue(body.get("success"))


# ---------------------------------------------------------------------------
# 3. GET /api/offers/open_count
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestOpenOfferCount(_FlaskBase):
    """Stub database.get_open_offers to a clean empty list so the route's
    real DB fetch doesn't intermittently fail under parallel xdist
    workers competing for the same sqlite write-lock."""

    def setUp(self):
        super().setUp()
        import database

        self._orig_get_open_offers = database.get_open_offers
        database.get_open_offers = lambda *a, **kw: []

    def tearDown(self):
        import database

        database.get_open_offers = self._orig_get_open_offers
        super().tearDown()

    def test_returns_200(self):
        resp = self.client.get("/api/offers/open_count", environ_base=self._LOOPBACK)
        self.assertEqual(resp.status_code, 200)

    def test_response_has_open_count(self):
        resp = self.client.get("/api/offers/open_count", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertIn("open_count", body)
        self.assertIsInstance(body["open_count"], int)

    def test_success_key_true_on_success(self):
        resp = self.client.get("/api/offers/open_count", environ_base=self._LOOPBACK)
        body = resp.get_json()
        self.assertTrue(body.get("success"))


# ---------------------------------------------------------------------------
# 4. POST /api/offers/cancel
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCancelOffer(_FlaskBase):
    def test_requires_token(self):
        resp = self._post("/api/offers/cancel", {"trade_id": "abc123"}, auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_bot_none_returns_500(self):
        with patch.object(api_server, "bot", None):
            resp = self._post("/api/offers/cancel", {"trade_id": "abc123"})
        self.assertEqual(resp.status_code, 500)

    def test_invalid_body_returns_400(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self.client.post(
                "/api/offers/cancel",
                data="not json",
                content_type="text/plain",
                headers=self.auth,
                environ_base=self._LOOPBACK,
            )
        self.assertEqual(resp.status_code, 400)

    def test_missing_trade_id_returns_400(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self._post("/api/offers/cancel", {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_empty_trade_id_returns_400(self):
        with patch.object(api_server, "bot", _make_bot()):
            resp = self._post("/api/offers/cancel", {"trade_id": ""})
        self.assertEqual(resp.status_code, 400)

    def test_successful_cancel_returns_200(self):
        bot = _make_bot()
        bot.offer_manager.cancel_offers.return_value = {}
        with patch.object(api_server, "bot", bot):
            resp = self._post("/api/offers/cancel", {"trade_id": "trade-abc-001"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("success"))

    def test_cancel_response_has_trade_id(self):
        bot = _make_bot()
        bot.offer_manager.cancel_offers.return_value = {}
        with patch.object(api_server, "bot", bot):
            resp = self._post("/api/offers/cancel", {"trade_id": "trade-abc-001"})
        body = resp.get_json()
        self.assertEqual(body.get("trade_id"), "trade-abc-001")

    def test_cancel_result_error_returns_400(self):
        bot = _make_bot()
        bot.offer_manager.cancel_offers.return_value = {"error": "storm_protection"}
        with patch.object(api_server, "bot", bot):
            resp = self._post("/api/offers/cancel", {"trade_id": "trade-abc-001"})
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# 5. POST /api/offers/cancel_all
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestCancelAllPost(_FlaskBase):
    def test_requires_token(self):
        resp = self._post("/api/offers/cancel_all", auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_bot_none_uses_direct_wallet_path(self):
        # bot=None → direct wallet RPC path; no open offers → 200 success
        with (
            patch.object(api_server, "bot", None),
            patch("wallet.get_all_offers", return_value=[]),
            patch("wallet.cancel_offers_batch", return_value={}),
            patch("wallet.is_offer_time_expired", return_value=False),
        ):
            resp = self._post("/api/offers/cancel_all")
        self.assertEqual(resp.status_code, 200)

    def test_bot_running_returns_409(self):
        # Running bot must be stopped before cancel_all is allowed
        with patch.object(api_server, "bot", _make_bot()):
            resp = self._post("/api/offers/cancel_all")
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.get_json().get("success"))

    def test_bot_stopped_cancel_all_returns_success(self):
        stopped = _make_bot()
        stopped.is_running.return_value = False
        with (
            patch.object(api_server, "bot", stopped),
            patch("wallet.get_all_offers", return_value=[]),
            patch("wallet.cancel_offers_batch", return_value={}),
            patch("wallet.is_offer_time_expired", return_value=False),
        ):
            resp = self._post("/api/offers/cancel_all")
        self.assertIn(resp.status_code, (200, 202))


if __name__ == "__main__":
    unittest.main()
