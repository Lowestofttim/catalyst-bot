"""Slice 03-14 — config reload integration test.

Tests Config.reload() with actual .env file writes, verifying that
cfg fields update after the file changes. Also tests Config.update()
(write to .env then reload) and the control-character injection guard.

No Flask server or wallet calls needed — purely file+config.

Key: the real .env is already loaded into os.environ at module import time.
To get predictable results, we use settings that have no legacy fallback
key and we clear the env var before loading our temp .env.
"""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config as _cfg_mod
    from config import Config

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    Config = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Helper — write a minimal .env file
# ---------------------------------------------------------------------------


def _write_env(path: str, **kwargs):
    with open(path, "w", encoding="utf-8") as fh:
        for k, v in kwargs.items():
            fh.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# Temp-env base — creates a temp .env file and patches config._ENV_PATH
# ---------------------------------------------------------------------------


class _TempEnv(unittest.TestCase):
    # Settings to clear from os.environ during the test (restored in tearDown)
    _ENV_KEYS = ("DRY_RUN", "CAT_TICKER_ID", "SNIPER_COOLDOWN_SECS")

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            suffix=".env", mode="w", delete=False, encoding="utf-8"
        )
        self._tmp_path = self._tmp.name
        self._tmp.close()

        # Save originals
        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        self._orig_env_path = _cfg_mod._ENV_PATH

        # Clear keys we'll test so they don't shadow the temp .env
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        # Restore env
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _cfg_mod._ENV_PATH = self._orig_env_path
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass

    def _make_config(self, **env_kwargs) -> Config:
        """Write temp .env, patch _ENV_PATH, and return a fresh Config."""
        _write_env(self._tmp_path, **env_kwargs)
        _cfg_mod._ENV_PATH = self._tmp_path
        return Config()

    def _update_env(self, **env_kwargs):
        """Overwrite the temp .env file."""
        _write_env(self._tmp_path, **env_kwargs)


# ---------------------------------------------------------------------------
# 1. Config.reload() picks up .env changes
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestConfigReload(_TempEnv):
    def test_initial_bool_field_true(self):
        cfg = self._make_config(DRY_RUN="True")
        self.assertTrue(cfg.DRY_RUN)

    def test_initial_bool_field_false(self):
        cfg = self._make_config(DRY_RUN="False")
        self.assertFalse(cfg.DRY_RUN)

    def test_reload_flips_bool_field(self):
        cfg = self._make_config(DRY_RUN="False")
        self.assertFalse(cfg.DRY_RUN)
        self._update_env(DRY_RUN="True")
        cfg.reload()
        self.assertTrue(cfg.DRY_RUN)

    def test_reload_flips_bool_back(self):
        cfg = self._make_config(DRY_RUN="True")
        self.assertTrue(cfg.DRY_RUN)
        self._update_env(DRY_RUN="False")
        cfg.reload()
        self.assertFalse(cfg.DRY_RUN)

    def test_reload_updates_string_field(self):
        cfg = self._make_config(CAT_TICKER_ID="TESTONE")
        self.assertEqual(cfg.CAT_TICKER_ID, "TESTONE")
        self._update_env(CAT_TICKER_ID="TESTTWO")
        cfg.reload()
        self.assertEqual(cfg.CAT_TICKER_ID, "TESTTWO")

    def test_reload_idempotent_when_file_unchanged(self):
        cfg = self._make_config(DRY_RUN="False")
        val_before = cfg.DRY_RUN
        cfg.reload()
        self.assertEqual(cfg.DRY_RUN, val_before)

    def test_multiple_reloads_follow_file(self):
        cfg = self._make_config(DRY_RUN="False")
        for expected in [True, False, True]:
            self._update_env(DRY_RUN=str(expected))
            cfg.reload()
            self.assertEqual(cfg.DRY_RUN, expected)


# ---------------------------------------------------------------------------
# 2. Config.update() — write to .env + reload
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestConfigUpdate(_TempEnv):
    def test_update_blocked_on_newline_injection(self):
        cfg = self._make_config(CAT_TICKER_ID="ORIGINAL")
        result = cfg.update("CAT_TICKER_ID", "abc\ndef")
        # Should be rejected (returns False)
        self.assertFalse(bool(result))
        # Field unchanged
        self.assertEqual(cfg.CAT_TICKER_ID, "ORIGINAL")

    def test_update_blocked_on_carriage_return_injection(self):
        cfg = self._make_config(CAT_TICKER_ID="ORIGINAL")
        result = cfg.update("CAT_TICKER_ID", "abc\rdef")
        self.assertFalse(bool(result))
        self.assertEqual(cfg.CAT_TICKER_ID, "ORIGINAL")

    def test_update_blocked_on_null_byte_injection(self):
        cfg = self._make_config(CAT_TICKER_ID="ORIGINAL")
        result = cfg.update("CAT_TICKER_ID", "abc\x00def")
        self.assertFalse(bool(result))
        self.assertEqual(cfg.CAT_TICKER_ID, "ORIGINAL")


# ---------------------------------------------------------------------------
# 3. Thread safety — concurrent reloads don't corrupt state
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestConfigReloadThreadSafety(_TempEnv):
    def test_concurrent_reloads_do_not_raise(self):
        cfg = self._make_config(DRY_RUN="False")
        errors = []

        def _reload():
            try:
                cfg.reload()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_reload) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"Concurrent reload raised: {errors}")

    def test_concurrent_reloads_leave_valid_bool_field(self):
        cfg = self._make_config(DRY_RUN="False")

        def _reload():
            cfg.reload()

        threads = [threading.Thread(target=_reload) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # After all threads finish, field must be a valid bool
        self.assertIsInstance(cfg.DRY_RUN, bool)


# ---------------------------------------------------------------------------
# 4. Reload strips surrounding quotes
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestConfigReloadQuoteStripping(_TempEnv):
    def test_single_quoted_string_stripped(self):
        cfg = self._make_config(CAT_TICKER_ID="'QUOTED'")
        self.assertEqual(cfg.CAT_TICKER_ID, "QUOTED")

    def test_double_quoted_string_stripped(self):
        cfg = self._make_config(CAT_TICKER_ID='"DQUOTED"')
        self.assertEqual(cfg.CAT_TICKER_ID, "DQUOTED")

    def test_unquoted_string_unchanged(self):
        cfg = self._make_config(CAT_TICKER_ID="PLAIN")
        self.assertEqual(cfg.CAT_TICKER_ID, "PLAIN")

    def test_quoted_bool_parsed_correctly(self):
        cfg = self._make_config(DRY_RUN="'True'")
        self.assertTrue(cfg.DRY_RUN)


if __name__ == "__main__":
    unittest.main()
