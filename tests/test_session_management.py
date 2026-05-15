"""Tests for session management — fresh-start flag lifecycle.

These tests cover the _FRESH_START_FLAG path that caused the real bug where
the resume modal would reappear after the user had already chosen "Start Fresh".

The flag is a plain file on disk so it survives process restarts.  Tests patch
api_server._FRESH_START_FLAG to a temp path so no artifacts are left behind.

Import strategy: api_server is imported lazily in setUpClass so that the
bot_loop test stubs (which contaminate sys.modules["database"] at module level)
have been fully restored before we attempt the api_server import.
"""

import os
import sys
import unittest
import tempfile
from unittest.mock import patch


def _get_api_server():
    """Import (or return cached) api_server, handling stub contamination."""
    # If api_server is already properly imported, return it.
    existing = sys.modules.get("api_server")
    if existing is not None and hasattr(existing, "_FRESH_START_FLAG"):
        return existing, None

    # Remove any stub database modules that bot_loop tests may have left behind
    # so api_server gets the real database on import.
    _db = sys.modules.get("database")
    _db_is_stub = _db is not None and not hasattr(_db, "init_database")
    _db_backup = _db if _db_is_stub else None
    if _db_is_stub:
        sys.modules.pop("database", None)

    try:
        import api_server as _api

        return _api, None
    except (ImportError, ModuleNotFoundError) as exc:
        return None, exc
    finally:
        # Restore whatever was in sys.modules["database"] before we touched it
        if _db_is_stub and _db_backup is not None:
            sys.modules["database"] = _db_backup


_api_server, _IMPORT_ERROR = _get_api_server()


@unittest.skipIf(_api_server is None, f"api_server import unavailable: {_IMPORT_ERROR}")
class SessionManagementTests(unittest.TestCase):
    """Tests for the fresh-start flag file lifecycle."""

    def setUp(self):
        # Create a unique temp file path for each test — start with the file
        # absent so each test controls whether it exists.
        self._tmpdir = tempfile.mkdtemp()
        self._flag_path = os.path.join(self._tmpdir, ".fresh_start_chosen")
        # Ensure the flag is absent at test start
        if os.path.exists(self._flag_path):
            os.remove(self._flag_path)
        _api_server.app.testing = True
        self._client = _api_server.app.test_client()
        self._loopback = {"REMOTE_ADDR": "127.0.0.1"}

    def tearDown(self):
        # Clean up the temp flag file if it was created
        if os.path.exists(self._flag_path):
            os.remove(self._flag_path)
        try:
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 1. api_session_fresh_start() creates the flag file
    # ------------------------------------------------------------------

    def test_fresh_start_flag_is_written_on_api_call(self):
        """Calling /api/session/fresh-start must create the flag file on disk."""
        with (
            patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path),
            patch.object(_api_server, "_reset_fresh_run_session", return_value={}),
        ):
            resp = self._client.post(
                "/api/session/fresh-start",
                headers={"X-Bot-Local-Token": _api_server._LOCAL_API_TOKEN},
                environ_base=self._loopback,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertTrue(
            os.path.exists(self._flag_path),
            "Flag file was not created after /api/session/fresh-start",
        )

    # ------------------------------------------------------------------
    # 2. api_check_resume() returns can_resume=False when flag is set
    # ------------------------------------------------------------------

    def test_check_resume_returns_no_resume_when_flag_set(self):
        """When the fresh-start flag exists, check-resume must report can_resume=False."""
        # Create the flag file before the request
        open(self._flag_path, "w").close()

        with patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path):
            resp = self._client.get(
                "/api/check-resume",
                headers={"X-Bot-Local-Token": _api_server._LOCAL_API_TOKEN},
                environ_base=self._loopback,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body.get("can_resume"))
        self.assertEqual(body.get("reason"), "fresh_start_chosen")

    # ------------------------------------------------------------------
    # 3. api_session_resume_chosen() clears the flag file
    # ------------------------------------------------------------------

    def test_resume_chosen_clears_flag(self):
        """Calling /api/session/resume-chosen must delete the flag file."""
        # Plant the flag first
        open(self._flag_path, "w").close()
        self.assertTrue(os.path.exists(self._flag_path))

        with patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path):
            resp = self._client.post(
                "/api/session/resume-chosen",
                headers={"X-Bot-Local-Token": _api_server._LOCAL_API_TOKEN},
                environ_base=self._loopback,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("success"))
        self.assertFalse(
            os.path.exists(self._flag_path),
            "Flag file was not removed after /api/session/resume-chosen",
        )

    # ------------------------------------------------------------------
    # 4. api_check_resume() shows can_resume=True when flag absent + offers exist
    # ------------------------------------------------------------------

    def test_check_resume_shows_modal_when_no_flag(self):
        """When flag is absent and live offers exist, check-resume returns can_resume=True."""
        # Flag is absent (setUp ensures this)
        fake_offer = {"trade_id": "abc", "status": "open"}

        with (
            patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path),
            patch("wallet.get_all_offers", return_value=[fake_offer]),
            patch(
                "wallet.classify_offers_from_list", return_value=([fake_offer], [], [])
            ),
        ):
            resp = self._client.get(
                "/api/check-resume",
                headers={"X-Bot-Local-Token": _api_server._LOCAL_API_TOKEN},
                environ_base=self._loopback,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(
            body.get("can_resume"),
            f"Expected can_resume=True when offers exist and flag is absent; got: {body}",
        )

    # ------------------------------------------------------------------
    # 5. Flag on disk is read correctly even by a fresh import reference
    # ------------------------------------------------------------------

    def test_fresh_start_flag_survives_process_restart(self):
        """The flag is a file, not memory — _fresh_start_is_set() reads it fresh each call."""
        # Verify absent → False
        with patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path):
            self.assertFalse(_api_server._fresh_start_is_set())

        # Create file outside of api_server (simulating another process writing it)
        open(self._flag_path, "w").close()

        # Now _fresh_start_is_set() must notice it without any in-memory state
        with patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path):
            self.assertTrue(_api_server._fresh_start_is_set())

        # Remove it and verify it disappears again
        os.remove(self._flag_path)
        with patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path):
            self.assertFalse(_api_server._fresh_start_is_set())

    # ------------------------------------------------------------------
    # 6. _FRESH_START_FLAG resolves to the project directory
    # ------------------------------------------------------------------

    def test_flag_path_is_in_project_directory(self):
        """_FRESH_START_FLAG must live next to api_server.py, not in a temp dir."""
        flag = _api_server._FRESH_START_FLAG
        expected_dir = os.path.dirname(os.path.abspath(_api_server.__file__))
        actual_dir = os.path.dirname(os.path.abspath(flag))
        self.assertEqual(
            actual_dir,
            expected_dir,
            f"Flag path {flag!r} is not in the project directory {expected_dir!r}",
        )
        self.assertTrue(
            flag.endswith(".fresh_start_chosen"),
            f"Flag file should be named .fresh_start_chosen, got: {flag!r}",
        )

    # ------------------------------------------------------------------
    # 7. api_check_resume() returns can_resume=False when bot is running
    # ------------------------------------------------------------------

    def test_check_resume_returns_no_resume_when_bot_running(self):
        """When the bot is actively running (loop_count>0), check-resume must
        return can_resume=False with reason='bot_already_running' so the GUI
        doesn't show a spurious 'Resume session?' modal on page reload."""
        import types as _types

        # Build a minimal fake bot with _loop_count > 0
        fake_bot = _types.SimpleNamespace(_loop_count=1)

        with (
            patch.object(_api_server, "_FRESH_START_FLAG", self._flag_path),
            patch.object(_api_server, "bot", fake_bot),
        ):
            resp = self._client.get(
                "/api/check-resume",
                headers={"X-Bot-Local-Token": _api_server._LOCAL_API_TOKEN},
                environ_base=self._loopback,
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(
            body.get("can_resume"), "can_resume must be False when bot is running"
        )
        self.assertEqual(body.get("reason"), "bot_already_running")


if __name__ == "__main__":
    unittest.main()
