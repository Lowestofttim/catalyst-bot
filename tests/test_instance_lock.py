"""Regression test: cross-process singleton lock must reject the second
process even after the first one populates the lock file.

Original bug (Windows): ``msvcrt.locking()`` locks bytes from the *current
file position*. The acquire path opened the lock file with ``"a+"`` so
the position was end-of-file. Process #1 (empty file) locked byte 0,
wrote PID metadata, advancing the position. Process #2 opened the same
file populated by #1, found its position at byte 30+, locked that byte
instead. Both "won" the lock; both ran their own coin-prep workers,
racing the wallet -> MEMPOOL_CONFLICT cascade.

The fix in ``desktop_app._acquire_instance_lock`` is ``fh.seek(0)``
before ``msvcrt.locking()``, so every process contends on byte 0.

This test pins down the lock protocol by simulating the two-process flow
inside one process using two open file handles. It deliberately mirrors
the production sequence (open "a+" -> seek(0) -> lock 1 byte -> seek(1) ->
truncate -> write PID) so any drift in either side is caught.

POSIX is skipped because ``fcntl.flock`` locks the open file description,
not a byte range; file position doesn't matter on Linux/Mac.
"""

import os
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "win32", "Windows-specific lock semantics")
class InstanceLockMutexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".lock")
        self._tmp.close()
        with open(self._tmp.name, "wb"):
            pass

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except (FileNotFoundError, PermissionError):
            pass

    def _instance1_acquire_and_write(self):
        """Mirror desktop_app._acquire_instance_lock's success path."""
        import msvcrt

        fh = open(self._tmp.name, "a+", encoding="utf-8")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        # Post-lock write must happen at byte 1+ to avoid clipping the
        # locked region.
        fh.seek(1)
        fh.truncate()
        fh.write("pid=11111 started=1700000000\n")
        fh.flush()
        return fh

    def _instance2_try_acquire(self):
        """Second launcher's attempt must fail with OSError if seek(0)
        anchors the lock to byte 0.
        """
        import msvcrt

        fh = open(self._tmp.name, "a+", encoding="utf-8")
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        finally:
            # Caller may not reach a release path if locking() raised, so
            # always close.
            try:
                fh.close()
            except Exception:
                pass

    def test_second_acquire_blocked_when_first_holds_byte_zero(self):
        fh1 = self._instance1_acquire_and_write()
        try:
            with self.assertRaises(
                OSError, msg="second acquire succeeded; singleton race re-introduced"
            ):
                self._instance2_try_acquire()
        finally:
            try:
                import msvcrt

                fh1.seek(0)
                msvcrt.locking(fh1.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            fh1.close()

    def test_second_acquire_succeeds_after_first_releases(self):
        """Sanity: lock protocol is releasable so an honest restart works."""
        fh1 = self._instance1_acquire_and_write()
        import msvcrt

        fh1.seek(0)
        msvcrt.locking(fh1.fileno(), msvcrt.LK_UNLCK, 1)
        fh1.close()

        fh2 = open(self._tmp.name, "a+", encoding="utf-8")
        try:
            fh2.seek(0)
            msvcrt.locking(fh2.fileno(), msvcrt.LK_NBLCK, 1)
            # Cleanup
            fh2.seek(0)
            msvcrt.locking(fh2.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fh2.close()


@unittest.skipUnless(sys.platform == "win32", "Windows-specific lock semantics")
class DesktopAppUsesSeekZeroTest(unittest.TestCase):
    """Static check: the production acquire path must seek(0) before the
    msvcrt.locking() call. This catches the regression even if someone
    rewrites the test above to mask it.
    """

    def test_acquire_function_seeks_to_zero(self):
        import ast

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        src = os.path.join(repo_root, "desktop_app.py")
        with open(src, "r", encoding="utf-8") as fh:
            text = fh.read()
        tree = ast.parse(text)
        fn = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_acquire_instance_lock"
            ),
            None,
        )
        self.assertIsNotNone(fn, "_acquire_instance_lock function not found")

        seen_seek_zero = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                func = node.func
                # Detect fh.seek(0): Attribute on .seek with literal 0.
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "seek"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 0
                ):
                    seen_seek_zero = True
                # Detect msvcrt.locking(...): must come AFTER a seek(0).
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "locking"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "msvcrt"
                ):
                    # Reject LK_UNLCK calls; we only care about acquires.
                    if any(
                        isinstance(a, ast.Attribute) and a.attr == "LK_NBLCK"
                        for a in node.args
                    ):
                        self.assertTrue(
                            seen_seek_zero,
                            "msvcrt.locking(LK_NBLCK) called without a preceding "
                            "fh.seek(0); singleton race re-introduced",
                        )
                        return
        self.fail("msvcrt.locking(LK_NBLCK) call not found in _acquire_instance_lock")


if __name__ == "__main__":
    unittest.main()
