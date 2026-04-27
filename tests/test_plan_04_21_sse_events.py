"""Slice 04-21 — SSE events stream contract tests.

Tests GET /api/events:
  - Auth guard: requires token (returns 401 without token)
  - Response headers: Cache-Control: no-cache, X-Accel-Buffering: no
  - bot=None → no initial state event, stream still starts
  - bot present → initial 'state' event emitted immediately
  - subscribe/unsubscribe lifecycle called correctly
  - Stream messages serialized as JSON data: lines ending with double newline

Design note: the SSE generator loops forever on queue.get(timeout=30).
Tests use a _finite_queue() mock whose get() side_effect raises GeneratorExit
after the test messages, causing the stream to terminate cleanly.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def _sse_get(client, token, *, with_auth=True):
    headers = {"X-Bot-Local-Token": token} if with_auth else {}
    return client.get("/api/events",
                      headers=headers,
                      environ_base=_LOOPBACK)


def _finite_queue(*items):
    """Queue mock that yields items then raises GeneratorExit to end stream.

    The SSE generator catches GeneratorExit at its outer try/except, calling
    events.unsubscribe() in the finally block and terminating cleanly.
    """
    q = MagicMock()
    q.get = MagicMock(side_effect=list(items) + [GeneratorExit()])
    return q


def _read_all_sse(resp):
    """Fully consume a (finite) SSE response and return decoded text."""
    buf = b""
    for chunk in resp.response:
        buf += chunk
    return buf.decode("utf-8", errors="replace")


class _FlaskBase(unittest.TestCase):

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()


# ---------------------------------------------------------------------------
# 1. Auth guard
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSSEAuth(_FlaskBase):

    def test_no_token_returns_401(self):
        resp = _sse_get(self.client, self.token, with_auth=False)
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self):
        resp = self.client.get("/api/events",
                               headers={"X-Bot-Local-Token": "wrong-token"},
                               environ_base=_LOOPBACK)
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# 2. Basic response shape (no body consumption needed — headers only)
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSSEResponseShape(_FlaskBase):

    def _resp(self):
        q = _finite_queue({"type": "ping"})
        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", None):
            return _sse_get(self.client, self.token)

    def test_returns_200(self):
        self.assertEqual(self._resp().status_code, 200)

    def test_content_type_is_event_stream(self):
        self.assertIn("text/event-stream", self._resp().content_type)

    def test_cache_control_no_cache(self):
        self.assertEqual(self._resp().headers.get("Cache-Control"), "no-cache")

    def test_x_accel_buffering_no(self):
        self.assertEqual(self._resp().headers.get("X-Accel-Buffering"), "no")


# ---------------------------------------------------------------------------
# 3. Subscribe / unsubscribe lifecycle
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSSESubscribeLifecycle(_FlaskBase):

    def test_subscribe_called_on_connect(self):
        q = _finite_queue({"type": "ping"})
        with patch.object(api_server.events, "subscribe", return_value=q) as mock_sub, \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", None):
            resp = _sse_get(self.client, self.token)
            _read_all_sse(resp)

        mock_sub.assert_called_once()

    def test_unsubscribe_called_after_stream_ends(self):
        q = _finite_queue({"type": "ping"})
        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe") as mock_unsub, \
             patch.object(api_server, "bot", None):
            resp = _sse_get(self.client, self.token)
            _read_all_sse(resp)

        mock_unsub.assert_called_once_with(q)


# ---------------------------------------------------------------------------
# 4. Initial state event
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSSEInitialState(_FlaskBase):

    def test_bot_none_first_event_is_sentinel(self):
        """Without bot, the first data event should not be 'state'."""
        q = _finite_queue({"type": "sentinel"})
        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", None):
            resp = _sse_get(self.client, self.token)
            raw = _read_all_sse(resp)

        lines = [line for line in raw.splitlines() if line.startswith("data:")]
        self.assertTrue(len(lines) >= 1)
        first = json.loads(lines[0].split("data: ", 1)[1])
        self.assertNotEqual(first.get("type"), "state")

    def test_bot_present_first_event_is_state(self):
        """With bot, the very first event must be type='state'."""
        q = _finite_queue({"type": "after_state"})
        fake_bot = MagicMock()
        fake_bot.get_state.return_value = {"running": True}

        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", fake_bot):
            resp = _sse_get(self.client, self.token)
            raw = _read_all_sse(resp)

        lines = [line for line in raw.splitlines() if line.startswith("data:")]
        self.assertTrue(len(lines) >= 1, f"No data lines in: {raw!r}")
        first = json.loads(lines[0].split("data: ", 1)[1])
        self.assertEqual(first.get("type"), "state")

    def test_initial_state_data_is_dict(self):
        """State event must carry a 'data' key that is a dict."""
        q = _finite_queue()
        fake_bot = MagicMock()
        fake_bot.get_state.return_value = {"running": False, "mode": "idle"}

        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", fake_bot):
            resp = _sse_get(self.client, self.token)
            raw = _read_all_sse(resp)

        lines = [line for line in raw.splitlines() if line.startswith("data:")]
        self.assertTrue(len(lines) >= 1)
        first = json.loads(lines[0].split("data: ", 1)[1])
        self.assertIsInstance(first.get("data"), dict)


# ---------------------------------------------------------------------------
# 5. Message format
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class TestSSEMessageFormat(_FlaskBase):

    def test_dict_message_serialized_as_json(self):
        q = _finite_queue({"type": "price_update", "price": "0.001"})
        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", None):
            resp = _sse_get(self.client, self.token)
            raw = _read_all_sse(resp)

        lines = [line for line in raw.splitlines() if line.startswith("data:")]
        self.assertTrue(len(lines) >= 1)
        msg = json.loads(lines[0].split("data: ", 1)[1])
        self.assertEqual(msg.get("type"), "price_update")

    def test_events_terminated_by_double_newline(self):
        """SSE spec: each event ends with \\n\\n."""
        q = _finite_queue({"type": "ping"})
        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", None):
            resp = _sse_get(self.client, self.token)
            raw = _read_all_sse(resp)

        self.assertIn("\n\n", raw)

    def test_multiple_messages_all_parsed(self):
        q = _finite_queue({"type": "msg1"}, {"type": "msg2"})
        with patch.object(api_server.events, "subscribe", return_value=q), \
             patch.object(api_server.events, "unsubscribe"), \
             patch.object(api_server, "bot", None):
            resp = _sse_get(self.client, self.token)
            raw = _read_all_sse(resp)

        lines = [line for line in raw.splitlines() if line.startswith("data:")]
        self.assertEqual(len(lines), 2)
        types = [json.loads(line.split("data: ", 1)[1]).get("type") for line in lines]
        self.assertIn("msg1", types)
        self.assertIn("msg2", types)


if __name__ == "__main__":
    unittest.main()
