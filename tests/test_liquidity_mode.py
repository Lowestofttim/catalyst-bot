"""Tests for the LIQUIDITY_MODE config field and its derived behaviour.

Covers:
  * config.Config derives ENABLE_BUY / ENABLE_SELL from LIQUIDITY_MODE
  * config.is_two_sided() / is_single_sided() / active_side() helpers
  * coin_manager._sniper_pool_enabled() returns False in single-sided modes
  * sniper.try_snipe / try_snipe_single short-circuit in single-sided modes

Hermetic style: instead of reloading sys.modules["config"] (which
leaks a fresh cfg object to unrelated test files and breaks their
module-level fakes), we temporarily mutate cfg.LIQUIDITY_MODE /
cfg.ENABLE_BUY / cfg.ENABLE_SELL on the already-imported Config
instance and restore it on tearDown.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch


class LiquidityModeConfigTests(unittest.TestCase):
    """The Config class resolves LIQUIDITY_MODE into ENABLE flags."""

    @classmethod
    def setUpClass(cls):
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())

    def setUp(self):
        import config

        self.cfg = config.cfg
        # Snapshot the fields we mutate so tearDown can put them back.
        self._snap = {
            "LIQUIDITY_MODE": getattr(self.cfg, "LIQUIDITY_MODE", "two_sided"),
            "ENABLE_BUY": getattr(self.cfg, "ENABLE_BUY", True),
            "ENABLE_SELL": getattr(self.cfg, "ENABLE_SELL", True),
        }

    def tearDown(self):
        for k, v in self._snap.items():
            setattr(self.cfg, k, v)

    def _apply_mode(self, mode):
        self.cfg.LIQUIDITY_MODE = mode
        if mode == "buy_only":
            self.cfg.ENABLE_BUY = True
            self.cfg.ENABLE_SELL = False
        elif mode == "sell_only":
            self.cfg.ENABLE_BUY = False
            self.cfg.ENABLE_SELL = True
        else:
            self.cfg.ENABLE_BUY = True
            self.cfg.ENABLE_SELL = True

    def test_two_sided_flags(self):
        self._apply_mode("two_sided")
        self.assertTrue(self.cfg.ENABLE_BUY and self.cfg.ENABLE_SELL)
        self.assertTrue(self.cfg.is_two_sided())
        self.assertFalse(self.cfg.is_single_sided())
        self.assertEqual(self.cfg.active_side(), "both")

    def test_buy_only_flags(self):
        self._apply_mode("buy_only")
        self.assertTrue(self.cfg.ENABLE_BUY)
        self.assertFalse(self.cfg.ENABLE_SELL)
        self.assertFalse(self.cfg.is_two_sided())
        self.assertTrue(self.cfg.is_single_sided())
        self.assertEqual(self.cfg.active_side(), "buy")

    def test_sell_only_flags(self):
        self._apply_mode("sell_only")
        self.assertFalse(self.cfg.ENABLE_BUY)
        self.assertTrue(self.cfg.ENABLE_SELL)
        self.assertFalse(self.cfg.is_two_sided())
        self.assertTrue(self.cfg.is_single_sided())
        self.assertEqual(self.cfg.active_side(), "sell")


class ConfigDerivationAtLoadTests(unittest.TestCase):
    """Verify __init__-time derivation of LIQUIDITY_MODE → ENABLE flags.

    Uses the real Config class with patched env vars, constructed on a
    side instance so the module-level ``cfg`` singleton is untouched.
    """

    @classmethod
    def setUpClass(cls):
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())

    def _build(self, mode, *, enable_buy="true", enable_sell="true"):
        """Build a fresh Config instance with the given env state.

        ``Config.reload()`` calls ``load_dotenv(..., override=True)`` which
        overwrites os.environ from the real .env file and defeats a naive
        os.environ patch. Stub load_dotenv for the duration of the build so
        our env vars stick.
        """
        prev = {
            k: os.environ.get(k)
            for k in ("LIQUIDITY_MODE", "ENABLE_BUY", "ENABLE_SELL")
        }
        os.environ["LIQUIDITY_MODE"] = mode
        os.environ["ENABLE_BUY"] = enable_buy
        os.environ["ENABLE_SELL"] = enable_sell
        try:
            import config

            with patch("config.load_dotenv", lambda *a, **kw: None):
                c = config.Config()
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return c

    def test_valid_modes_round_trip(self):
        for mode, expect_buy, expect_sell, expect_side in (
            ("two_sided", True, True, "both"),
            ("buy_only", True, False, "buy"),
            ("sell_only", False, True, "sell"),
        ):
            c = self._build(mode)
            self.assertEqual(c.LIQUIDITY_MODE, mode, f"{mode} round-trip")
            self.assertEqual(c.ENABLE_BUY, expect_buy)
            self.assertEqual(c.ENABLE_SELL, expect_sell)
            self.assertEqual(c.active_side(), expect_side)

    def test_mode_overrides_conflicting_env_flags(self):
        c = self._build("buy_only", enable_buy="false", enable_sell="true")
        self.assertTrue(c.ENABLE_BUY)
        self.assertFalse(c.ENABLE_SELL)

    def test_unknown_mode_falls_back_to_two_sided(self):
        c = self._build("garbage")
        self.assertEqual(c.LIQUIDITY_MODE, "two_sided")
        self.assertTrue(c.ENABLE_BUY and c.ENABLE_SELL)


class SniperSingleSidedShortCircuitTests(unittest.TestCase):
    """Sniper.try_snipe / try_snipe_single return [] immediately when
    LIQUIDITY_MODE is pinned single-sided."""

    @classmethod
    def setUpClass(cls):
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())

    def setUp(self):
        import config

        self.cfg = config.cfg
        self._snap_mode = getattr(self.cfg, "LIQUIDITY_MODE", "two_sided")

    def tearDown(self):
        self.cfg.LIQUIDITY_MODE = self._snap_mode

    def test_try_snipe_empty_in_buy_only(self):
        # Sniper module reads cfg at call time — mutating the singleton
        # is enough; no need to re-import.
        from decimal import Decimal
        import sniper as s_mod

        self.cfg.LIQUIDITY_MODE = "buy_only"
        sniper = s_mod.Sniper(offer_manager=None, risk_manager=None, dexie_manager=None)
        self.assertEqual(sniper.try_snipe(Decimal("0.0001"), Decimal("0.0002")), [])
        self.assertEqual(sniper.try_snipe_single("buy", Decimal("0.0001")), [])

    def test_try_snipe_empty_in_sell_only(self):
        from decimal import Decimal
        import sniper as s_mod

        self.cfg.LIQUIDITY_MODE = "sell_only"
        sniper = s_mod.Sniper(offer_manager=None, risk_manager=None, dexie_manager=None)
        self.assertEqual(sniper.try_snipe(Decimal("0.0001"), Decimal("0.0002")), [])
        self.assertEqual(sniper.try_snipe_single("sell", Decimal("0.0002")), [])


class CoinManagerSniperPoolTests(unittest.TestCase):
    """_sniper_pool_enabled gates off the prep pool in single-sided modes."""

    @classmethod
    def setUpClass(cls):
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())

    def setUp(self):
        import config

        self.cfg = config.cfg
        self._snap = {
            "LIQUIDITY_MODE": getattr(self.cfg, "LIQUIDITY_MODE", "two_sided"),
            "TIER_ENABLED": getattr(self.cfg, "TIER_ENABLED", False),
            "SNIPER_ENABLED": getattr(self.cfg, "SNIPER_ENABLED", False),
            "SNIPER_PREP_COUNT": getattr(self.cfg, "SNIPER_PREP_COUNT", 0),
            "SNIPER_SIZE_XCH": getattr(self.cfg, "SNIPER_SIZE_XCH", 0),
        }
        # Force "sniper would be on in theory"
        self.cfg.TIER_ENABLED = True
        self.cfg.SNIPER_ENABLED = True
        self.cfg.SNIPER_PREP_COUNT = 20
        from decimal import Decimal

        self.cfg.SNIPER_SIZE_XCH = Decimal("0.01")

    def tearDown(self):
        for k, v in self._snap.items():
            setattr(self.cfg, k, v)

    def _mgr(self):
        import coin_manager as cm

        return cm.CoinManager.__new__(cm.CoinManager)

    def test_sniper_pool_off_in_buy_only(self):
        self.cfg.LIQUIDITY_MODE = "buy_only"
        self.assertFalse(self._mgr()._sniper_pool_enabled())

    def test_sniper_pool_off_in_sell_only(self):
        self.cfg.LIQUIDITY_MODE = "sell_only"
        self.assertFalse(self._mgr()._sniper_pool_enabled())

    def test_sniper_pool_on_in_two_sided(self):
        self.cfg.LIQUIDITY_MODE = "two_sided"
        self.assertTrue(self._mgr()._sniper_pool_enabled())


if __name__ == "__main__":
    unittest.main()
