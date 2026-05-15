"""Slice 03-08 — requote flow integration test.

Tests the multi-module path: price drift detected by OfferManager →
RequoteSeverity returned via reaction_strategy.classify_drift →
correct tier set selected for graduated response.

Modules wired together:
  offer_manager.OfferManager.should_requote
  offer_manager.OfferManager.should_requote_graduated
  reaction_strategy.classify_drift
  reaction_strategy.tiers_for_severity
  reaction_strategy.RequoteSeverity

No wallet calls, no Flask server.  Cfg patched via patch.object.
"""

import sys
import os
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import offer_manager as _om_mod
    from offer_manager import OfferManager
    from reaction_strategy import RequoteSeverity, tiers_for_severity

    _SKIP = None
except ModuleNotFoundError as exc:
    _om_mod = None
    OfferManager = None
    RequoteSeverity = None
    tiers_for_severity = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Shared cfg factory
# ---------------------------------------------------------------------------


def _make_cfg(**overrides):
    defaults = dict(
        AUTO_REQUOTE=True,
        REQUOTE_COOLDOWN_SECS=0,
        REQUOTE_BPS=Decimal("30"),
        CAT_ASSET_ID="testcat",
        # Graduated drift thresholds (defaults from config.py)
        REQUOTE_DRIFT_INNER=Decimal("0.003"),
        REQUOTE_DRIFT_MID=Decimal("0.008"),
        REQUOTE_DRIFT_FULL=Decimal("0.02"),
        REQUOTE_DRIFT_EMERGENCY=Decimal("0.05"),
        AMM_DRIFT_REQUOTE_BPS=Decimal("80"),
    )
    defaults.update(overrides)
    ns = SimpleNamespace(**defaults)
    # Provide the method that config.Config has
    ns.get_requote_fraction = lambda: ns.REQUOTE_BPS / Decimal("10000")
    return ns


# ---------------------------------------------------------------------------
# 1. should_requote — basic detection
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestShouldRequote(unittest.TestCase):
    def setUp(self):
        self._cfg = _make_cfg()
        self._patch = patch.object(_om_mod, "cfg", self._cfg)
        self._patch.start()
        self.om = OfferManager()

    def tearDown(self):
        self._patch.stop()

    def test_no_requote_when_auto_requote_disabled(self):
        self._cfg.AUTO_REQUOTE = False
        self.assertFalse(
            self.om.should_requote("buy", Decimal("0.001"), Decimal("0.001"))
        )

    def test_no_requote_when_price_unchanged(self):
        self.assertFalse(
            self.om.should_requote("buy", Decimal("0.001"), Decimal("0.001"))
        )

    def test_no_requote_below_threshold(self):
        # 0.2% move < 0.3% (30bps) threshold
        current = Decimal("0.001002")
        last = Decimal("0.001")
        self.assertFalse(self.om.should_requote("buy", current, last))

    def test_requote_above_threshold(self):
        # 0.5% move > 0.3% (30bps) threshold
        current = Decimal("0.001005")
        last = Decimal("0.001")
        self.assertTrue(self.om.should_requote("buy", current, last))

    def test_requote_on_downward_move(self):
        # price fell 0.5%
        current = Decimal("0.000995")
        last = Decimal("0.001")
        self.assertTrue(self.om.should_requote("sell", current, last))

    def test_no_requote_during_cooldown(self):
        self._cfg.REQUOTE_COOLDOWN_SECS = 60
        # Stamp as just-requoted
        self.om._last_requote_time["buy"] = time.time()
        self.assertFalse(
            self.om.should_requote("buy", Decimal("0.002"), Decimal("0.001"))
        )

    def test_requote_after_cooldown_elapsed(self):
        self._cfg.REQUOTE_COOLDOWN_SECS = 1
        # Stamp as requoted 2s ago
        self.om._last_requote_time["buy"] = time.time() - 2
        self.assertTrue(
            self.om.should_requote("buy", Decimal("0.002"), Decimal("0.001"))
        )

    def test_zero_last_price_returns_false(self):
        self.assertFalse(self.om.should_requote("buy", Decimal("0.001"), Decimal("0")))


# ---------------------------------------------------------------------------
# 2. should_requote_graduated — reaction_strategy wiring
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestShouldRequoteGraduated(unittest.TestCase):
    def setUp(self):
        self._cfg = _make_cfg()
        self._patch = patch.object(_om_mod, "cfg", self._cfg)
        self._patch.start()
        self.om = OfferManager()

    def tearDown(self):
        self._patch.stop()

    def _drift(self, pct: float):
        """Return (current_price, last_price) for a given % drift."""
        last = Decimal("0.001")
        current = last * Decimal(str(1 + pct / 100))
        return current, last

    def test_no_drift_returns_none(self):
        current, last = self._drift(0)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.NONE)

    def test_small_drift_returns_none(self):
        # 0.2% < REQUOTE_DRIFT_INNER (0.3%)
        current, last = self._drift(0.2)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.NONE)

    def test_inner_drift(self):
        # 0.4% → INNER
        current, last = self._drift(0.4)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.INNER)

    def test_mid_drift(self):
        # 1.5% → INNER_MID
        current, last = self._drift(1.5)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.INNER_MID)

    def test_full_drift(self):
        # 3% → FULL
        current, last = self._drift(3.0)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.FULL)

    def test_emergency_drift(self):
        # 6% → EMERGENCY
        current, last = self._drift(6.0)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.EMERGENCY)

    def test_auto_requote_disabled_returns_none(self):
        self._cfg.AUTO_REQUOTE = False
        current, last = self._drift(10.0)
        sev = self.om.should_requote_graduated("buy", current, last)
        self.assertEqual(sev, RequoteSeverity.NONE)

    def test_cooldown_suppresses_graduated(self):
        self._cfg.REQUOTE_COOLDOWN_SECS = 60
        self.om._last_requote_time["sell"] = time.time()
        current, last = self._drift(10.0)
        sev = self.om.should_requote_graduated("sell", current, last)
        self.assertEqual(sev, RequoteSeverity.NONE)

    def test_symmetric_drift_same_severity(self):
        # Up vs down drift at same magnitude should give same severity
        last = Decimal("0.001")
        up = last * Decimal("1.04")  # +4% → FULL
        dn = last * Decimal("0.96")  # -4% → FULL
        sev_up = self.om.should_requote_graduated("buy", up, last)
        sev_dn = self.om.should_requote_graduated("sell", dn, last)
        self.assertEqual(sev_up, sev_dn)


# ---------------------------------------------------------------------------
# 3. Graduated severity → tier set wiring
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestSeverityToTierSetWiring(unittest.TestCase):
    """Verify that RequoteSeverity values map to expected tier sets.

    This tests the offer_manager → reaction_strategy → tier configuration
    pipeline end-to-end.
    """

    def setUp(self):
        self._cfg = _make_cfg()
        self._patch = patch.object(_om_mod, "cfg", self._cfg)
        self._patch.start()
        self.om = OfferManager()

    def tearDown(self):
        self._patch.stop()

    def _drift_severity(self, pct: float):
        last = Decimal("0.001")
        current = last * Decimal(str(1 + pct / 100))
        return self.om.should_requote_graduated("buy", current, last)

    def test_inner_severity_targets_inner_only(self):
        sev = self._drift_severity(0.4)
        tiers = tiers_for_severity(sev)
        self.assertIn("inner", tiers)
        self.assertNotIn("outer", tiers)

    def test_full_severity_targets_all_tiers(self):
        sev = self._drift_severity(3.0)
        tiers = tiers_for_severity(sev)
        for tier in ("inner", "mid", "outer"):
            self.assertIn(tier, tiers)

    def test_emergency_severity_targets_all_tiers(self):
        sev = self._drift_severity(6.0)
        tiers = tiers_for_severity(sev)
        for tier in ("inner", "mid", "outer"):
            self.assertIn(tier, tiers)

    def test_none_severity_targets_no_tiers(self):
        sev = self._drift_severity(0.1)
        self.assertEqual(sev, RequoteSeverity.NONE)
        tiers = tiers_for_severity(sev)
        self.assertEqual(len(tiers), 0)

    def test_tier_set_is_superset_as_severity_grows(self):
        inner_tiers = tiers_for_severity(RequoteSeverity.INNER)
        inner_mid_tiers = tiers_for_severity(RequoteSeverity.INNER_MID)
        full_tiers = tiers_for_severity(RequoteSeverity.FULL)
        self.assertTrue(inner_tiers.issubset(inner_mid_tiers))
        self.assertTrue(inner_mid_tiers.issubset(full_tiers))


# ---------------------------------------------------------------------------
# 4. Custom drift thresholds via cfg override
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"offer_manager unavailable: {_SKIP}")
class TestCustomDriftThresholds(unittest.TestCase):
    """Verify that graduated thresholds are read from cfg, not hardcoded."""

    def setUp(self):
        # Set all thresholds to very wide values
        self._cfg = _make_cfg(
            REQUOTE_DRIFT_INNER=Decimal("0.10"),  # 10%
            REQUOTE_DRIFT_MID=Decimal("0.20"),  # 20%
            REQUOTE_DRIFT_FULL=Decimal("0.30"),  # 30%
            REQUOTE_DRIFT_EMERGENCY=Decimal("0.50"),  # 50%
        )
        self._patch = patch.object(_om_mod, "cfg", self._cfg)
        self._patch.start()
        self.om = OfferManager()

    def tearDown(self):
        self._patch.stop()

    def _sev(self, pct):
        last = Decimal("0.001")
        current = last * Decimal(str(1 + pct / 100))
        return self.om.should_requote_graduated("buy", current, last)

    def test_5pct_drift_is_none_with_wide_thresholds(self):
        # 5% drift < 10% INNER threshold → NONE
        self.assertEqual(self._sev(5), RequoteSeverity.NONE)

    def test_15pct_drift_is_inner_with_wide_thresholds(self):
        # 15% drift ≥ 10% INNER, < 20% MID → INNER
        self.assertEqual(self._sev(15), RequoteSeverity.INNER)

    def test_25pct_drift_is_inner_mid_with_wide_thresholds(self):
        # 25% drift ≥ 20% MID, < 30% FULL → INNER_MID
        self.assertEqual(self._sev(25), RequoteSeverity.INNER_MID)


if __name__ == "__main__":
    unittest.main()
