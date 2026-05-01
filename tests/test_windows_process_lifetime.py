import os
import sys
import unittest
from unittest.mock import patch

import sage_node
import win_subprocess


class WindowsProcessLifetimeTests(unittest.TestCase):
    def test_kill_on_close_job_allows_explicit_child_breakaway(self):
        sys.modules.pop("desktop_app", None)
        with patch.object(sys, "platform", "linux"):
            import desktop_app

        flags = desktop_app._kill_on_close_job_limit_flags()

        self.assertTrue(flags & desktop_app.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        self.assertTrue(flags & desktop_app.JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        self.assertFalse(flags & desktop_app.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)

    def test_hidden_subprocess_breakaway_is_opt_in(self):
        with (
            patch.object(win_subprocess.os, "name", "nt"),
            patch.object(win_subprocess.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            patch.object(win_subprocess.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, create=True),
        ):
            breakaway_flag = win_subprocess.subprocess.CREATE_BREAKAWAY_FROM_JOB
            default_flags = win_subprocess.hidden_subprocess_kwargs()["creationflags"]
            breakaway_flags = win_subprocess.hidden_subprocess_kwargs(
                breakaway_from_job=True
            )["creationflags"]

        self.assertFalse(default_flags & breakaway_flag)
        self.assertTrue(breakaway_flags & breakaway_flag)

    def test_sage_launch_requests_breakaway_from_catalyst_job(self):
        calls = {}

        def fake_hidden_kwargs(**kwargs):
            calls["kwargs"] = kwargs
            return {"creationflags": 123}

        class DummyProcess:
            pid = 1234

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sage_node.os.path, "isfile", return_value=True),
            patch.object(sage_node, "hidden_subprocess_kwargs", side_effect=fake_hidden_kwargs),
            patch.object(sage_node.subprocess, "Popen", return_value=DummyProcess()),
            patch.object(sage_node, "log_event"),
        ):
            launched = sage_node._launch_sage_exe(os.path.join("C:\\Sage", "sage-tauri.exe"))

        self.assertTrue(launched)
        self.assertEqual(
            calls["kwargs"],
            {
                "detached": True,
                "new_process_group": True,
                "breakaway_from_job": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
