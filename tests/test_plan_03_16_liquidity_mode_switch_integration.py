"""Slice 03-16 — liquidity-mode switch cycle integration test.

Tests that LIQUIDITY_MODE drives ENABLE_BUY / ENABLE_SELL / active_side() /
is_single_sided() correctly across the full two_sided → buy_only → sell_only →
two_sided cycle.  Uses real .env I/O via Config.update() and a temp .env so
the production .env is never modified.

No Flask server, wallet calls, or live DB needed.
"""

import os
import sys
import tempfile
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
# Helper
# ---------------------------------------------------------------------------


def _write_env(path: str, **kwargs):
    with open(path, "w", encoding="utf-8") as fh:
        for k, v in kwargs.items():
            fh.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# Base — isolates LIQUIDITY_MODE (and raw enable flags) from the real .env
# ---------------------------------------------------------------------------


class _TempEnv(unittest.TestCase):
    _ENV_KEYS = ("LIQUIDITY_MODE", "ENABLE_BUY", "ENABLE_SELL")

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            suffix=".env", mode="w", delete=False, encoding="utf-8"
        )
        self._tmp_path = self._tmp.name
        self._tmp.close()

        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        self._orig_env_path = _cfg_mod._ENV_PATH

        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
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
        _write_env(self._tmp_path, **env_kwargs)
        _cfg_mod._ENV_PATH = self._tmp_path
        return Config()

    def _update_env(self, **env_kwargs):
        _write_env(self._tmp_path, **env_kwargs)


# ---------------------------------------------------------------------------
# 1. Initial load — LIQUIDITY_MODE drives derived enable flags
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestLiquidityModeInitialLoad(_TempEnv):
    def test_two_sided_enables_both(self):
        cfg = self._make_config(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        self.assertTrue(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)

    def test_buy_only_forces_sell_disabled(self):
        cfg = self._make_config(LIQUIDITY_MODE="buy_only")
        self.assertTrue(cfg.ENABLE_BUY)
        self.assertFalse(cfg.ENABLE_SELL)

    def test_sell_only_forces_buy_disabled(self):
        cfg = self._make_config(LIQUIDITY_MODE="sell_only")
        self.assertFalse(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)

    def test_active_side_two_sided(self):
        cfg = self._make_config(LIQUIDITY_MODE="two_sided")
        self.assertEqual(cfg.active_side(), "both")

    def test_active_side_buy_only(self):
        cfg = self._make_config(LIQUIDITY_MODE="buy_only")
        self.assertEqual(cfg.active_side(), "buy")

    def test_active_side_sell_only(self):
        cfg = self._make_config(LIQUIDITY_MODE="sell_only")
        self.assertEqual(cfg.active_side(), "sell")

    def test_invalid_mode_defaults_to_two_sided(self):
        cfg = self._make_config(LIQUIDITY_MODE="bogus_mode")
        self.assertEqual(cfg.LIQUIDITY_MODE, "two_sided")
        self.assertTrue(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)


# ---------------------------------------------------------------------------
# 2. is_single_sided()
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestLiquidityModeSingleSided(_TempEnv):
    def test_two_sided_not_single_sided(self):
        cfg = self._make_config(LIQUIDITY_MODE="two_sided")
        self.assertFalse(cfg.is_single_sided())

    def test_buy_only_is_single_sided(self):
        cfg = self._make_config(LIQUIDITY_MODE="buy_only")
        self.assertTrue(cfg.is_single_sided())

    def test_sell_only_is_single_sided(self):
        cfg = self._make_config(LIQUIDITY_MODE="sell_only")
        self.assertTrue(cfg.is_single_sided())


# ---------------------------------------------------------------------------
# 3. Switch cycle — reload() picks up .env changes for LIQUIDITY_MODE
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestLiquidityModeSwitchCycle(_TempEnv):
    def test_reload_two_to_buy(self):
        cfg = self._make_config(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        self.assertEqual(cfg.active_side(), "both")
        self._update_env(LIQUIDITY_MODE="buy_only")
        cfg.reload()
        self.assertEqual(cfg.active_side(), "buy")
        self.assertFalse(cfg.ENABLE_SELL)

    def test_reload_two_to_sell(self):
        cfg = self._make_config(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        self.assertEqual(cfg.active_side(), "both")
        self._update_env(LIQUIDITY_MODE="sell_only")
        cfg.reload()
        self.assertEqual(cfg.active_side(), "sell")
        self.assertFalse(cfg.ENABLE_BUY)

    def test_reload_buy_to_sell(self):
        cfg = self._make_config(LIQUIDITY_MODE="buy_only")
        self.assertEqual(cfg.active_side(), "buy")
        self._update_env(LIQUIDITY_MODE="sell_only")
        cfg.reload()
        self.assertEqual(cfg.active_side(), "sell")
        self.assertFalse(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)

    def test_reload_sell_to_two(self):
        cfg = self._make_config(LIQUIDITY_MODE="sell_only")
        self.assertEqual(cfg.active_side(), "sell")
        self._update_env(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        cfg.reload()
        self.assertEqual(cfg.active_side(), "both")
        self.assertTrue(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)

    def test_full_cycle_two_buy_sell_two(self):
        cfg = self._make_config(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )

        self._update_env(LIQUIDITY_MODE="buy_only")
        cfg.reload()
        self.assertEqual(cfg.active_side(), "buy")
        self.assertFalse(cfg.ENABLE_SELL)

        self._update_env(LIQUIDITY_MODE="sell_only")
        cfg.reload()
        self.assertEqual(cfg.active_side(), "sell")
        self.assertFalse(cfg.ENABLE_BUY)

        self._update_env(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        cfg.reload()
        self.assertEqual(cfg.active_side(), "both")
        self.assertTrue(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)

    def test_update_method_switches_mode(self):
        """Config.update() writes .env + reloads in one call."""
        cfg = self._make_config(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        self.assertEqual(cfg.active_side(), "both")
        result = cfg.update("LIQUIDITY_MODE", "buy_only")
        self.assertTrue(result)
        self.assertEqual(cfg.active_side(), "buy")
        self.assertFalse(cfg.ENABLE_SELL)

    def test_update_all_three_modes_in_sequence(self):
        cfg = self._make_config(
            LIQUIDITY_MODE="two_sided", ENABLE_BUY="true", ENABLE_SELL="true"
        )
        for mode, expected_side in [
            ("buy_only", "buy"),
            ("sell_only", "sell"),
            ("two_sided", "both"),
        ]:
            result = cfg.update("LIQUIDITY_MODE", mode)
            self.assertTrue(result, f"update to {mode} failed")
            self.assertEqual(
                cfg.active_side(), expected_side, f"after switching to {mode}"
            )


# ---------------------------------------------------------------------------
# 4. Mode-consistent LIQUIDITY_MODE field value
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"config unavailable: {_SKIP}")
class TestLiquidityModeFieldConsistency(_TempEnv):
    def test_liquidity_mode_field_matches_active_side(self):
        """LIQUIDITY_MODE attribute is consistent with active_side() output."""
        for mode, expected_side in [
            ("two_sided", "both"),
            ("buy_only", "buy"),
            ("sell_only", "sell"),
        ]:
            with self.subTest(mode=mode):
                cfg = self._make_config(LIQUIDITY_MODE=mode)
                self.assertEqual(cfg.active_side(), expected_side)
                self.assertEqual(cfg.LIQUIDITY_MODE, mode)

    def test_enable_buy_exclusive_in_sell_only(self):
        cfg = self._make_config(LIQUIDITY_MODE="sell_only")
        self.assertFalse(cfg.ENABLE_BUY)
        self.assertTrue(cfg.ENABLE_SELL)

    def test_enable_sell_exclusive_in_buy_only(self):
        cfg = self._make_config(LIQUIDITY_MODE="buy_only")
        self.assertTrue(cfg.ENABLE_BUY)
        self.assertFalse(cfg.ENABLE_SELL)


if __name__ == "__main__":
    unittest.main()
