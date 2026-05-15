"""Layer 7 — Slice 07-05: coin_prep_worker crashed mid-run.

Tests:
  CoinManager.check_coin_prep_status():
    - No process → {"running": False}
    - Process running → {"running": True, "pid": <n>}
    - Process crashed (exit code != 0) → {"running": False, "exit_code": <n>}
    - Process succeeded (exit code 0) → {"running": False, "exit_code": 0}
    - Crash resets _prep_running flag on manager

  /api/coin-prep/status crash detection:
    - running state + crashed worker → phase="error", running=False
    - crash detection skipped when phase is "complete" in status file
    - non-running worker with exit_code=0 does not trigger error phase
    - error message includes the crash exit code

  /api/coin-prep/trigger after crash:
    - trigger resets state (running=True, error=None) even from previous error state
    - trigger response has success=True
"""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import api_server
    import coin_manager as cm_module

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    cm_module = None
    _SKIP = str(exc)


_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def _make_coin_manager():
    """Build a minimal CoinManager with just the attributes check_coin_prep_status needs."""
    mgr = cm_module.CoinManager.__new__(cm_module.CoinManager)
    mgr._prep_process = None
    mgr._prep_running = False
    mgr._lock = threading.Lock()
    mgr._worker_cancelled_ids = set()
    mgr.update_coin_counts = MagicMock()
    return mgr


# ---------------------------------------------------------------------------
# CoinManager.check_coin_prep_status() — unit tests
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCheckCoinPrepStatus(unittest.TestCase):
    """check_coin_prep_status() must report worker state correctly."""

    def test_no_process_returns_not_running(self):
        """When no subprocess has been launched, running=False."""
        mgr = _make_coin_manager()
        result = mgr.check_coin_prep_status()
        self.assertFalse(result.get("running"))

    def test_running_process_returns_running_with_pid(self):
        """Running subprocess returns running=True and pid."""
        mgr = _make_coin_manager()
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        proc.pid = 12345
        mgr._prep_process = proc
        result = mgr.check_coin_prep_status()
        self.assertTrue(result.get("running"))
        self.assertEqual(result.get("pid"), 12345)

    def test_crashed_process_returns_not_running_with_exit_code(self):
        """Crashed worker (exit code != 0) → running=False, exit_code set."""
        mgr = _make_coin_manager()
        proc = MagicMock()
        proc.poll.return_value = -1  # crash / SIGTERM
        proc.stdout = None
        proc.stderr = None
        mgr._prep_process = proc
        with (
            patch("coin_manager.log_event"),
            patch("os.path.exists", return_value=False),
        ):
            result = mgr.check_coin_prep_status()
        self.assertFalse(result.get("running"))
        self.assertEqual(result.get("exit_code"), -1)

    def test_successful_process_returns_exit_code_zero(self):
        """Clean exit (code 0) → running=False, exit_code=0."""
        mgr = _make_coin_manager()
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdout = None
        proc.stderr = None
        mgr._prep_process = proc
        with (
            patch("coin_manager.log_event"),
            patch("os.path.exists", return_value=False),
        ):
            result = mgr.check_coin_prep_status()
        self.assertFalse(result.get("running"))
        self.assertEqual(result.get("exit_code"), 0)

    def test_crash_sets_prep_running_false(self):
        """Worker crash also sets _prep_running=False on the manager."""
        mgr = _make_coin_manager()
        mgr._prep_running = True
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.stdout = None
        proc.stderr = None
        mgr._prep_process = proc
        with (
            patch("coin_manager.log_event"),
            patch("os.path.exists", return_value=False),
        ):
            mgr.check_coin_prep_status()
        self.assertFalse(mgr._prep_running)

    def test_check_does_not_raise_on_missing_stdout(self):
        """check_coin_prep_status() never raises even if stdout/stderr closed."""
        mgr = _make_coin_manager()
        proc = MagicMock()
        proc.poll.return_value = 2
        proc.stdout.read.side_effect = IOError("pipe closed")
        proc.stderr.read.side_effect = IOError("pipe closed")
        mgr._prep_process = proc
        with (
            patch("coin_manager.log_event"),
            patch("os.path.exists", return_value=False),
        ):
            try:
                result = mgr.check_coin_prep_status()
            except Exception as exc:
                self.fail(f"check_coin_prep_status raised: {exc}")
        self.assertFalse(result.get("running"))


# ---------------------------------------------------------------------------
# /api/coin-prep/status — crash detection in the endpoint
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCoinPrepStatusEndpointCrashDetection(unittest.TestCase):
    """Status endpoint must surface worker crash state without hiding it."""

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        api_server._rate_limit_log.clear()
        api_server._coin_prep_state["running"] = False
        api_server._coin_prep_state["complete"] = False
        api_server._coin_prep_state["error"] = None
        api_server._coin_prep_state.pop("phase", None)
        # Ensure no stale status file is found
        self._status_file_patch = patch("os.path.exists", return_value=False)
        self._status_file_patch.start()

    def tearDown(self):
        self._status_file_patch.stop()
        api_server._rate_limit_log.clear()
        api_server._coin_prep_state["running"] = False
        api_server._coin_prep_state["complete"] = False
        api_server._coin_prep_state["error"] = None

    def _get_status(self, bot=None):
        with patch.object(api_server, "bot", bot):
            return self.client.get(
                "/api/coin-prep/status",
                environ_base=_LOOPBACK,
            )

    def test_no_bot_returns_200_with_running_false(self):
        """Status endpoint succeeds even without bot."""
        resp = self._get_status(bot=None)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json().get("running"))

    def test_crashed_worker_detected_when_state_running(self):
        """When state=running but worker has crashed, endpoint sets phase=error."""
        api_server._coin_prep_state["running"] = True

        bot = MagicMock()
        bot.coin_manager.check_coin_prep_status.return_value = {
            "running": False,
            "exit_code": -1,
        }
        resp = self._get_status(bot=bot)
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body.get("phase"), "error")
        self.assertFalse(api_server._coin_prep_state.get("running"))

    def test_clean_exit_does_not_produce_error_phase(self):
        """exit_code=0 after the monitoring loop → no error phase."""
        api_server._coin_prep_state["running"] = True

        bot = MagicMock()
        bot.coin_manager.check_coin_prep_status.return_value = {
            "running": False,
            "exit_code": 0,
        }
        resp = self._get_status(bot=bot)
        body = resp.get_json()
        self.assertNotEqual(body.get("phase"), "error")

    def test_error_message_contains_exit_code(self):
        """Error string must include the non-zero crash exit code."""
        api_server._coin_prep_state["running"] = True

        bot = MagicMock()
        bot.coin_manager.check_coin_prep_status.return_value = {
            "running": False,
            "exit_code": 2,
        }
        resp = self._get_status(bot=bot)
        body = resp.get_json()
        # "Worker exited with code 2" is the expected message
        self.assertIn("2", str(body.get("error", "")))

    def test_running_false_state_no_crash_detection(self):
        """When state is already running=False, crash detection does not fire."""
        api_server._coin_prep_state["running"] = False

        bot = MagicMock()
        bot.coin_manager.check_coin_prep_status.return_value = {
            "running": False,
            "exit_code": -1,
        }
        resp = self._get_status(bot=bot)
        body = resp.get_json()
        # Should not change to error (was already not running)
        self.assertNotEqual(body.get("phase"), "error")


# ---------------------------------------------------------------------------
# /api/coin-prep/trigger — state reset after crash
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCoinPrepTriggerAfterCrash(unittest.TestCase):
    """After a crash, the trigger endpoint must reset state cleanly."""

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        api_server._rate_limit_log.clear()
        api_server._coin_prep_proc = None  # no stale process

    def tearDown(self):
        api_server._rate_limit_log.clear()
        api_server._coin_prep_state["running"] = False
        api_server._coin_prep_state["complete"] = False
        api_server._coin_prep_state["error"] = None
        api_server._coin_prep_proc = None

    def _trigger(self, bot_mock):
        # Patch threading.Thread so do_prep() never actually launches.
        # Route lives in blueprints.coin_prep since the api_server split;
        # patch threading/log_event there so the blueprint sees the mock.
        mock_thread = MagicMock()
        with (
            patch.object(api_server, "bot", bot_mock),
            patch("blueprints.coin_prep.threading") as mock_threading,
            patch("blueprints.coin_prep.log_event"),
            patch("os.path.exists", return_value=True),
            patch("builtins.open", unittest.mock.mock_open()),
        ):
            mock_threading.Thread.return_value = mock_thread
            resp = self.client.post(
                "/api/coin-prep/trigger",
                json={},
                headers={"X-Bot-Local-Token": self.token},
                environ_base=_LOOPBACK,
            )
        return resp

    def _make_bot(self):
        bot = MagicMock()
        bot.is_running.return_value = False
        bot.coin_manager.check_coin_prep_status.return_value = {"running": False}
        bot.coin_manager.get_coin_health.return_value = (5, 5)
        return bot

    def test_trigger_returns_success(self):
        """Trigger endpoint returns success=True."""
        resp = self._trigger(self._make_bot())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

    def test_trigger_sets_running_true(self):
        """After trigger, _coin_prep_state['running'] is True."""
        api_server._coin_prep_state["running"] = False
        api_server._coin_prep_state["error"] = "Worker exited with code -1"
        self._trigger(self._make_bot())
        self.assertTrue(api_server._coin_prep_state.get("running"))

    def test_trigger_clears_previous_error(self):
        """Re-trigger must clear the previous error."""
        api_server._coin_prep_state["error"] = "Worker exited with code 1"
        self._trigger(self._make_bot())
        self.assertIsNone(api_server._coin_prep_state.get("error"))

    def test_trigger_sets_phase_idle(self):
        """State after trigger has phase='idle', not the old 'error'."""
        api_server._coin_prep_state["phase"] = "error"
        self._trigger(self._make_bot())
        self.assertEqual(api_server._coin_prep_state.get("phase"), "idle")


if __name__ == "__main__":
    unittest.main()
