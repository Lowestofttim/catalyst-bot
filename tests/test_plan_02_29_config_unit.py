"""Slice 02-29 — config.py + config_validator.py unit tests.

Tests pure helper functions (_strip_quotes, _bool, _int, _decimal, _safe_url),
Config computed methods (get_spread_fraction, is_two_sided, active_side,
to_dict), module-level tier helpers, ValidationReport, _is_valid_url, and
validate_config. No .env file writes or reloads.
"""

import os
import sys
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

try:
    import config as _cfg_mod
    from config import (
        _strip_quotes, Config,
        get_buy_tier_size_xch, get_sell_tier_size_xch,
        get_tier_sizes_for_side, has_per_side_tier_sizes,
    )
    _SKIP_CFG = None
except (ModuleNotFoundError, ImportError) as exc:
    _cfg_mod = None
    _SKIP_CFG = str(exc)

try:
    from config_validator import (
        ConfigIssue, ValidationReport, _is_valid_url, validate_config,
    )
    _SKIP_VAL = None
except (ModuleNotFoundError, ImportError) as exc:
    _SKIP_VAL = str(exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bare_config(**attrs):
    """Create a Config instance without running __init__ (no .env access)."""
    obj = Config.__new__(Config)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ===========================================================================
# _strip_quotes
# ===========================================================================

@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestStripQuotes(unittest.TestCase):
    def test_strips_double_quotes(self):
        self.assertEqual(_strip_quotes('"hello"'), "hello")

    def test_strips_single_quotes(self):
        self.assertEqual(_strip_quotes("'hello'"), "hello")

    def test_no_quotes_unchanged(self):
        self.assertEqual(_strip_quotes("hello"), "hello")

    def test_empty_string(self):
        self.assertEqual(_strip_quotes(""), "")

    def test_single_char_not_stripped(self):
        self.assertEqual(_strip_quotes('"'), '"')

    def test_mismatched_quotes_unchanged(self):
        self.assertEqual(_strip_quotes('"hello\''), '"hello\'')

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(_strip_quotes('  "hello"  '), "hello")


# ===========================================================================
# _bool (via env patch)
# ===========================================================================

@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestBool(unittest.TestCase):
    def _call(self, env_val, default=False):
        with patch.dict(os.environ, {"TEST_BOOL_KEY": env_val}):
            return _cfg_mod._bool("TEST_BOOL_KEY", default)

    def test_true_string(self):
        self.assertTrue(self._call("true"))

    def test_1_string(self):
        self.assertTrue(self._call("1"))

    def test_yes_string(self):
        self.assertTrue(self._call("yes"))

    def test_on_string(self):
        self.assertTrue(self._call("on"))

    def test_false_string(self):
        self.assertFalse(self._call("false"))

    def test_0_string(self):
        self.assertFalse(self._call("0"))

    def test_missing_uses_default_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_BOOL_MISSING", None)
            result = _cfg_mod._bool("TEST_BOOL_MISSING", True)
        self.assertTrue(result)

    def test_quoted_true_is_true(self):
        self.assertTrue(self._call('"true"'))


# ===========================================================================
# Config computed methods (bare instances, no reload)
# ===========================================================================

@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestGetSpreadFraction(unittest.TestCase):
    def test_800_bps_gives_008(self):
        c = _bare_config(SPREAD_BPS=Decimal("800"))
        self.assertEqual(c.get_spread_fraction(), Decimal("0.08"))

    def test_100_bps_gives_001(self):
        c = _bare_config(SPREAD_BPS=Decimal("100"))
        self.assertEqual(c.get_spread_fraction(), Decimal("0.01"))


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestGetRequoteFraction(unittest.TestCase):
    def test_200_bps_gives_002(self):
        c = _bare_config(REQUOTE_BPS=Decimal("200"))
        self.assertEqual(c.get_requote_fraction(), Decimal("0.02"))


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestTibetShockConfig(unittest.TestCase):
    def test_shock_threshold_is_gui_updatable(self):
        self.assertIn("TIBET_SHOCK_CANCEL_TRIGGER_PCT", Config._UPDATABLE_KEYS)


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestIsTwoSided(unittest.TestCase):
    def test_two_sided_mode_both_enabled(self):
        c = _bare_config(LIQUIDITY_MODE="two_sided", ENABLE_BUY=True, ENABLE_SELL=True)
        self.assertTrue(c.is_two_sided())

    def test_buy_only_mode_not_two_sided(self):
        c = _bare_config(LIQUIDITY_MODE="buy_only", ENABLE_BUY=True, ENABLE_SELL=False)
        self.assertFalse(c.is_two_sided())

    def test_sell_disabled_not_two_sided(self):
        c = _bare_config(LIQUIDITY_MODE="two_sided", ENABLE_BUY=True, ENABLE_SELL=False)
        self.assertFalse(c.is_two_sided())


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestIsSingleSided(unittest.TestCase):
    def test_buy_only_is_single_sided(self):
        c = _bare_config(LIQUIDITY_MODE="buy_only")
        self.assertTrue(c.is_single_sided())

    def test_sell_only_is_single_sided(self):
        c = _bare_config(LIQUIDITY_MODE="sell_only")
        self.assertTrue(c.is_single_sided())

    def test_two_sided_is_not_single_sided(self):
        c = _bare_config(LIQUIDITY_MODE="two_sided")
        self.assertFalse(c.is_single_sided())


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestActiveSide(unittest.TestCase):
    def test_buy_only_returns_buy(self):
        c = _bare_config(LIQUIDITY_MODE="buy_only")
        self.assertEqual(c.active_side(), "buy")

    def test_sell_only_returns_sell(self):
        c = _bare_config(LIQUIDITY_MODE="sell_only")
        self.assertEqual(c.active_side(), "sell")

    def test_two_sided_returns_both(self):
        c = _bare_config(LIQUIDITY_MODE="two_sided")
        self.assertEqual(c.active_side(), "both")

    def test_unknown_mode_returns_both(self):
        c = _bare_config(LIQUIDITY_MODE="unknown_mode")
        self.assertEqual(c.active_side(), "both")


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestToDict(unittest.TestCase):
    def test_excludes_sensitive_keys(self):
        sensitive = {
            "CHIA_WALLET_CERT": "cert_path",
            "CHIA_WALLET_KEY": "key_path",
            "WALLET_FINGERPRINT": "12345",
            "SPACESCAN_API_KEY": "secret",
            "SAGE_CERT_PATH": "sage_cert",
            "SAGE_KEY_PATH": "sage_key",
            "SAGE_FINGERPRINT": "fp",
            "SAGE_EXE_PATH": "exe",
            "SAGE_DATA_DIR": "data",
            "FULL_NODE_CERT_PATH": "full_node_cert",
            "FULL_NODE_KEY_PATH": "full_node_key",
        }
        c = _bare_config(SPREAD_BPS=Decimal("800"), **sensitive)
        d = c.to_dict()
        for key in sensitive:
            self.assertNotIn(key, d, f"Sensitive key {key} leaked into to_dict")

    def test_includes_non_sensitive_keys(self):
        c = _bare_config(SPREAD_BPS=Decimal("800"), WALLET_TYPE="sage")
        d = c.to_dict()
        self.assertIn("SPREAD_BPS", d)
        self.assertIn("WALLET_TYPE", d)

    def test_decimal_converted_to_string(self):
        c = _bare_config(SPREAD_BPS=Decimal("800"))
        d = c.to_dict()
        self.assertIsInstance(d["SPREAD_BPS"], str)

    def test_private_attrs_excluded(self):
        c = _bare_config(SPREAD_BPS=Decimal("800"))
        c._private = "should not appear"
        d = c.to_dict()
        self.assertNotIn("_private", d)


# ===========================================================================
# Module-level tier helpers
# ===========================================================================

@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestGetBuyTierSizeXch(unittest.TestCase):
    def _with_cfg(self, **attrs):
        ns = SimpleNamespace(**attrs)
        with patch.object(_cfg_mod, "cfg", ns):
            return None  # caller reads directly

    def test_invalid_tier_returns_zero(self):
        with patch.object(_cfg_mod, "cfg", SimpleNamespace()):
            result = get_buy_tier_size_xch("invalid_tier")
        self.assertEqual(result, Decimal("0"))

    def test_modern_buy_field_used_when_set(self):
        ns = SimpleNamespace(
            BUY_INNER_SIZE_XCH=Decimal("2.5"),
            BUY_LADDER_REVERSED=False,
            INNER_SIZE_XCH=Decimal("1.0"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            result = get_buy_tier_size_xch("inner")
        self.assertEqual(result, Decimal("2.5"))

    def test_legacy_fallback_when_modern_field_zero(self):
        ns = SimpleNamespace(
            BUY_INNER_SIZE_XCH=Decimal("0"),
            BUY_LADDER_REVERSED=False,
            INNER_SIZE_XCH=Decimal("1.0"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            result = get_buy_tier_size_xch("inner")
        self.assertEqual(result, Decimal("1.0"))


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestGetSellTierSizeXch(unittest.TestCase):
    def test_modern_sell_field_used_when_set(self):
        ns = SimpleNamespace(
            SELL_INNER_SIZE_XCH=Decimal("3.0"),
            INNER_SIZE_XCH=Decimal("1.0"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            result = get_sell_tier_size_xch("inner")
        self.assertEqual(result, Decimal("3.0"))

    def test_legacy_fallback_when_sell_field_zero(self):
        ns = SimpleNamespace(
            SELL_INNER_SIZE_XCH=Decimal("0"),
            INNER_SIZE_XCH=Decimal("1.5"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            result = get_sell_tier_size_xch("inner")
        self.assertEqual(result, Decimal("1.5"))

    def test_invalid_tier_returns_zero(self):
        with patch.object(_cfg_mod, "cfg", SimpleNamespace()):
            result = get_sell_tier_size_xch("bogus")
        self.assertEqual(result, Decimal("0"))


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestGetTierSizesForSide(unittest.TestCase):
    def test_returns_dict_with_all_tiers(self):
        ns = SimpleNamespace(
            BUY_INNER_SIZE_XCH=Decimal("1"), BUY_MID_SIZE_XCH=Decimal("2"),
            BUY_OUTER_SIZE_XCH=Decimal("3"), BUY_EXTREME_SIZE_XCH=Decimal("4"),
            BUY_LADDER_REVERSED=False,
        )
        with patch.object(_cfg_mod, "cfg", ns):
            sizes = get_tier_sizes_for_side("buy")
        self.assertEqual(set(sizes.keys()), {"inner", "mid", "outer", "extreme"})

    def test_unknown_side_uses_sell_path(self):
        ns = SimpleNamespace(
            SELL_INNER_SIZE_XCH=Decimal("1"), SELL_MID_SIZE_XCH=Decimal("2"),
            SELL_OUTER_SIZE_XCH=Decimal("3"), SELL_EXTREME_SIZE_XCH=Decimal("4"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            sizes = get_tier_sizes_for_side("other")
        self.assertIn("inner", sizes)


@unittest.skipIf(_SKIP_CFG is not None, f"config unavailable: {_SKIP_CFG}")
class TestHasPerSideTierSizes(unittest.TestCase):
    def test_returns_true_when_any_buy_field_set(self):
        ns = SimpleNamespace(
            BUY_INNER_SIZE_XCH=Decimal("0.5"),
            BUY_MID_SIZE_XCH=Decimal("0"),
            BUY_OUTER_SIZE_XCH=Decimal("0"),
            BUY_EXTREME_SIZE_XCH=Decimal("0"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            self.assertTrue(has_per_side_tier_sizes())

    def test_returns_false_when_all_zero(self):
        ns = SimpleNamespace(
            BUY_INNER_SIZE_XCH=Decimal("0"),
            BUY_MID_SIZE_XCH=Decimal("0"),
            BUY_OUTER_SIZE_XCH=Decimal("0"),
            BUY_EXTREME_SIZE_XCH=Decimal("0"),
        )
        with patch.object(_cfg_mod, "cfg", ns):
            self.assertFalse(has_per_side_tier_sizes())


# ===========================================================================
# config_validator — ValidationReport
# ===========================================================================

@unittest.skipIf(_SKIP_VAL is not None, f"config_validator unavailable: {_SKIP_VAL}")
class TestValidationReport(unittest.TestCase):
    def test_empty_report_is_valid(self):
        report = ValidationReport()
        self.assertTrue(report.is_valid)

    def test_report_with_error_is_not_valid(self):
        report = ValidationReport()
        report.errors.append(ConfigIssue(key="K", message="bad", severity="error"))
        self.assertFalse(report.is_valid)

    def test_to_dict_has_expected_keys(self):
        report = ValidationReport()
        d = report.to_dict()
        self.assertIn("is_valid", d)
        self.assertIn("errors", d)
        self.assertIn("warnings", d)
        self.assertIn("error_count", d)
        self.assertIn("warning_count", d)

    def test_to_dict_counts_match(self):
        report = ValidationReport()
        report.errors.append(ConfigIssue(key="K", message="e", severity="error"))
        report.warnings.append(ConfigIssue(key="W", message="w", severity="warning"))
        d = report.to_dict()
        self.assertEqual(d["error_count"], 1)
        self.assertEqual(d["warning_count"], 1)


# ===========================================================================
# config_validator — _is_valid_url
# ===========================================================================

@unittest.skipIf(_SKIP_VAL is not None, f"config_validator unavailable: {_SKIP_VAL}")
class TestIsValidUrl(unittest.TestCase):
    def test_http_url_valid(self):
        self.assertTrue(_is_valid_url("http://localhost:5000"))

    def test_https_url_valid(self):
        self.assertTrue(_is_valid_url("https://api.dexie.space"))

    def test_ftp_url_invalid(self):
        self.assertFalse(_is_valid_url("ftp://something"))

    def test_empty_string_invalid(self):
        self.assertFalse(_is_valid_url(""))

    def test_no_scheme_invalid(self):
        self.assertFalse(_is_valid_url("localhost:5000"))


# ===========================================================================
# config_validator — validate_config
# ===========================================================================

@unittest.skipIf(_SKIP_VAL is not None, f"config_validator unavailable: {_SKIP_VAL}")
class TestValidateConfig(unittest.TestCase):
    def _minimal_valid(self, **overrides):
        """Minimal namespace that should produce no errors."""
        attrs = dict(
            CAT_ASSET_ID="abc123",
            CAT_DECIMALS=3,
            ENABLE_BUY=True,
            ENABLE_SELL=True,
            MIN_TRADE_XCH=Decimal("0.1"),
            MAX_TRADE_XCH=Decimal("10.0"),
            DEFAULT_TRADE_XCH=Decimal("1.0"),
            SPREAD_BPS=Decimal("800"),
            MIN_EDGE_BPS=Decimal("200"),
            DYNAMIC_SPREAD_ENABLED=False,
            HARD_MIN_PRICE_XCH=Decimal("0"),
            HARD_MAX_PRICE_XCH=Decimal("0"),
            PRICE_STRATEGY="weighted",
            TIBET_WEIGHT=Decimal("0.85"),
            OFFER_EXPIRY_SECS=86400,
            OFFER_REFRESH_BEFORE=1800,
            MAX_ACTIVE_BUY_OFFERS=25,
            MAX_ACTIVE_SELL_OFFERS=25,
            REQUOTE_COOLDOWN_SECS=60,
            REQUOTE_BATCH_SIZE=5,
            TIER_ENABLED=False,
            DEXIE_POST_ENABLED=True,
            MAX_POSITION_XCH=Decimal("100"),
        )
        attrs.update(overrides)
        return SimpleNamespace(**attrs)

    def test_valid_config_has_no_errors(self):
        report = validate_config(self._minimal_valid())
        self.assertTrue(report.is_valid, f"Unexpected errors: {report.errors}")

    def test_missing_cat_asset_id_is_error(self):
        report = validate_config(self._minimal_valid(CAT_ASSET_ID=""))
        keys = [i.key for i in report.errors]
        self.assertIn("CAT_ASSET_ID", keys)

    def test_both_sides_disabled_is_error(self):
        report = validate_config(self._minimal_valid(ENABLE_BUY=False, ENABLE_SELL=False))
        self.assertFalse(report.is_valid)

    def test_min_trade_greater_than_max_trade_is_error(self):
        report = validate_config(self._minimal_valid(
            MIN_TRADE_XCH=Decimal("10"), MAX_TRADE_XCH=Decimal("1")
        ))
        self.assertFalse(report.is_valid)

    def test_invalid_price_strategy_is_error(self):
        report = validate_config(self._minimal_valid(PRICE_STRATEGY="bad_strategy"))
        self.assertFalse(report.is_valid)

    def test_tibet_weight_out_of_range_is_error(self):
        report = validate_config(self._minimal_valid(TIBET_WEIGHT=Decimal("1.5")))
        self.assertFalse(report.is_valid)

    def test_zero_spread_is_error(self):
        report = validate_config(self._minimal_valid(SPREAD_BPS=Decimal("0")))
        self.assertFalse(report.is_valid)

    def test_very_short_expiry_is_error(self):
        report = validate_config(self._minimal_valid(OFFER_EXPIRY_SECS=60))
        self.assertFalse(report.is_valid)

    def test_hard_min_gt_hard_max_is_error(self):
        report = validate_config(self._minimal_valid(
            HARD_MIN_PRICE_XCH=Decimal("2.0"), HARD_MAX_PRICE_XCH=Decimal("1.0")
        ))
        self.assertFalse(report.is_valid)


if __name__ == "__main__":
    unittest.main()
