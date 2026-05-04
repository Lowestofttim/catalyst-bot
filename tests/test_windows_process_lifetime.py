import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import coin_manager
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

    def test_packaged_coin_prep_launch_uses_catalyst_worker_mode(self):
        exe_path = os.path.join("C:\\Program Files", "CATalyst", "Catalyst.exe")
        worker_path = os.path.join(
            "C:\\Program Files", "CATalyst", "_internal", "coin_prep_worker.py"
        )

        with (
            patch.object(coin_manager.sys, "executable", exe_path),
            patch.object(coin_manager.sys, "frozen", True, create=True),
        ):
            command = coin_manager._coin_prep_worker_command(worker_path)

        self.assertEqual(command, [exe_path, "--coin-prep-worker"])

    def test_no_coin_prep_launcher_uses_plain_python_worker_script(self):
        repo_root = Path(__file__).resolve().parent.parent
        sources = [
            repo_root / "src" / "catalyst" / "coin_manager.py",
            repo_root / "src" / "catalyst" / "blueprints" / "coin_prep.py",
        ]

        for source_path in sources:
            source = source_path.read_text(encoding="utf-8")
            self.assertNotIn('"python", worker_path', source)
            self.assertNotIn("'python', worker_path", source)

    def test_coin_prep_worker_mode_dispatches_remaining_args(self):
        sys.modules.pop("desktop_app", None)
        with patch.object(sys, "platform", "linux"):
            import desktop_app

        captured = {}
        fake_worker = types.ModuleType("coin_prep_worker")

        def fake_main():
            captured["argv"] = list(sys.argv)
            raise SystemExit(17)

        fake_worker.main = fake_main
        old_argv = list(sys.argv)
        old_worker = sys.modules.get("coin_prep_worker")
        sys.modules["coin_prep_worker"] = fake_worker
        try:
            result = desktop_app.main([
                "--coin-prep-worker",
                "--xch-target",
                "3",
                "--live-price",
                "0.00012",
            ])
        finally:
            sys.argv = old_argv
            if old_worker is None:
                sys.modules.pop("coin_prep_worker", None)
            else:
                sys.modules["coin_prep_worker"] = old_worker

        self.assertEqual(result, 17)
        self.assertEqual(
            captured["argv"][1:],
            ["--xch-target", "3", "--live-price", "0.00012"],
        )


if __name__ == "__main__":
    unittest.main()
