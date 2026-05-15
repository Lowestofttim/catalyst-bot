"""Tests for config_validator.py — structured config validation."""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from config_validator import validate_config


def _make_cfg(**overrides):
    """Create a mock config with safe defaults, overridden by kwargs."""
    defaults = {
        "CAT_ASSET_ID": "abcd1234",
        "CAT_DECIMALS": 3,
        "CAT_NAME": "TEST",
        "ENABLE_BUY": True,
        "ENABLE_SELL": True,
        "MIN_TRADE_XCH": Decimal("0.005"),
        "MAX_TRADE_XCH": Decimal("0.050"),
        "DEFAULT_TRADE_XCH": Decimal("0.0275"),
        "SPREAD_BPS": Decimal("800"),
        "MIN_EDGE_BPS": Decimal("300"),
        "TIBET_SHOCK_CANCEL_TRIGGER_PCT": Decimal("3"),
        "DYNAMIC_SPREAD_ENABLED": False,
        "MIN_SPREAD_BPS": Decimal("300"),
        "MAX_SPREAD_BPS": Decimal("3000"),
        "HARD_MIN_PRICE_XCH": Decimal("0"),
        "HARD_MAX_PRICE_XCH": Decimal("0"),
        "OFFER_EXPIRY_SECS": 86400,
        "OFFER_REFRESH_BEFORE": 1800,
        "MAX_ACTIVE_BUY_OFFERS": 25,
        "MAX_ACTIVE_SELL_OFFERS": 25,
        "REQUOTE_COOLDOWN_SECS": 60,
        "REQUOTE_BATCH_SIZE": 5,
        "LOOP_SECONDS": 90,
        "XCH_RESERVE": Decimal("0.03"),
        "CAT_RESERVE": Decimal("0"),
        "TIER_ENABLED": False,
        "INNER_TIER_COUNT": 0,
        "MID_TIER_COUNT": 0,
        "OUTER_TIER_COUNT": 0,
        "EXTREME_TIER_COUNT": 0,
        "INNER_SIZE_XCH": Decimal("1.0"),
        "MID_SIZE_XCH": Decimal("0.5"),
        "OUTER_SIZE_XCH": Decimal("0.25"),
        "EXTREME_SIZE_XCH": Decimal("0.1"),
        "WALLET_TYPE": "sage",
        "SAGE_RPC_URL": "https://127.0.0.1:9257",
        "CHIA_WALLET_RPC_URL": "https://localhost:9256",
        "DEXIE_API_BASE": "https://api.dexie.space",
        "TIBET_API_BASE": "https://api.v2.tibetswap.io",
        "SNIPER_ENABLED": False,
        "SNIPER_SIZE_XCH": Decimal("0.001"),
        "SNIPER_EXPIRY_SECS": 600,
        "SNIPER_COOLDOWN_SECS": 30,
        "LADDER_CREATE_PARALLELISM": 5,
        "BOOST_SIZE_XCH": Decimal("0.2"),
        "ENABLE_COIN_PREP": False,
        "XCH_TARGET_COINS": 50,
        "CAT_TARGET_COINS": 50,
        # Fee coin settings — must be explicit Decimal so validator comparisons
        # (fee_coin_size > Decimal("0")) don't receive a MagicMock attribute.
        "FEE_COIN_SIZE_XCH": Decimal("0"),
        "FEE_PREP_COUNT": 0,
    }
    defaults.update(overrides)
    cfg = MagicMock()
    for k, v in defaults.items():
        setattr(cfg, k, v)
    return cfg


class TestConfigValidator(unittest.TestCase):
    def test_valid_config_passes(self):
        report = validate_config(_make_cfg())
        self.assertTrue(report.is_valid)
        self.assertEqual(len(report.errors), 0)

    def test_empty_cat_asset_id_is_error(self):
        report = validate_config(_make_cfg(CAT_ASSET_ID=""))
        self.assertFalse(report.is_valid)
        self.assertTrue(any("CAT_ASSET_ID" in e.key for e in report.errors))

    def test_both_sides_disabled_is_error(self):
        report = validate_config(_make_cfg(ENABLE_BUY=False, ENABLE_SELL=False))
        self.assertFalse(report.is_valid)

    def test_inverted_trade_range_is_error(self):
        report = validate_config(
            _make_cfg(
                MIN_TRADE_XCH=Decimal("1.0"),
                MAX_TRADE_XCH=Decimal("0.01"),
            )
        )
        self.assertFalse(report.is_valid)

    def test_zero_spread_is_error(self):
        report = validate_config(_make_cfg(SPREAD_BPS=Decimal("0")))
        self.assertFalse(report.is_valid)

    def test_short_expiry_is_error(self):
        report = validate_config(_make_cfg(OFFER_EXPIRY_SECS=100))
        self.assertFalse(report.is_valid)

    def test_inverted_hard_limits_is_error(self):
        report = validate_config(
            _make_cfg(
                HARD_MIN_PRICE_XCH=Decimal("10"),
                HARD_MAX_PRICE_XCH=Decimal("1"),
            )
        )
        self.assertFalse(report.is_valid)

    def test_negative_reserve_is_error(self):
        report = validate_config(_make_cfg(XCH_RESERVE=Decimal("-1")))
        self.assertFalse(report.is_valid)

    def test_invalid_cat_decimals_is_error(self):
        report = validate_config(_make_cfg(CAT_DECIMALS=15))
        self.assertFalse(report.is_valid)

    def test_fast_loop_is_warning(self):
        report = validate_config(_make_cfg(LOOP_SECONDS=10))
        self.assertTrue(report.is_valid)  # warning, not error
        self.assertTrue(any("LOOP_SECONDS" in w.key for w in report.warnings))

    def test_edge_exceeds_spread_is_warning(self):
        report = validate_config(
            _make_cfg(
                MIN_EDGE_BPS=Decimal("900"),
                SPREAD_BPS=Decimal("800"),
            )
        )
        self.assertTrue(report.is_valid)
        self.assertTrue(any("MIN_EDGE_BPS" in w.key for w in report.warnings))

    def test_negative_tibet_shock_trigger_is_error(self):
        report = validate_config(
            _make_cfg(
                TIBET_SHOCK_CANCEL_TRIGGER_PCT=Decimal("-1"),
            )
        )
        self.assertFalse(report.is_valid)
        self.assertTrue(
            any("TIBET_SHOCK_CANCEL_TRIGGER_PCT" in e.key for e in report.errors)
        )

    def test_tier_enabled_zero_counts_is_warning(self):
        report = validate_config(_make_cfg(TIER_ENABLED=True))
        self.assertTrue(report.is_valid)
        self.assertTrue(any("TIER" in w.key for w in report.warnings))

    def test_tier_mode_ignores_legacy_max_trade_cap_for_ladder_sizes(self):
        report = validate_config(
            _make_cfg(
                TIER_ENABLED=True,
                INNER_TIER_COUNT=7,
                MID_TIER_COUNT=7,
                OUTER_TIER_COUNT=6,
                EXTREME_TIER_COUNT=4,
                DEFAULT_TRADE_XCH=Decimal("1.798"),
                MAX_TRADE_XCH=Decimal("0.2"),
                INNER_SIZE_XCH=Decimal("4.7615"),
                MID_SIZE_XCH=Decimal("3.9679"),
                OUTER_SIZE_XCH=Decimal("2.9759"),
                EXTREME_SIZE_XCH=Decimal("1.5872"),
            )
        )

        self.assertTrue(report.is_valid)
        noisy_keys = {
            "DEFAULT_TRADE_XCH",
            "INNER_SIZE_XCH",
            "MID_SIZE_XCH",
            "OUTER_SIZE_XCH",
            "EXTREME_SIZE_XCH",
        }
        self.assertFalse(noisy_keys.intersection({w.key for w in report.warnings}))

    def test_tier_with_zero_size_is_error(self):
        report = validate_config(
            _make_cfg(
                TIER_ENABLED=True,
                INNER_TIER_COUNT=5,
                INNER_SIZE_XCH=Decimal("0"),
            )
        )
        self.assertFalse(report.is_valid)

    def test_invalid_sage_url_is_error(self):
        report = validate_config(
            _make_cfg(
                WALLET_TYPE="sage",
                SAGE_RPC_URL="not-a-url",
            )
        )
        self.assertFalse(report.is_valid)

    def test_report_to_dict(self):
        report = validate_config(_make_cfg())
        d = report.to_dict()
        self.assertIn("is_valid", d)
        self.assertIn("errors", d)
        self.assertIn("warnings", d)

    def test_dynamic_spread_inverted_is_error(self):
        report = validate_config(
            _make_cfg(
                DYNAMIC_SPREAD_ENABLED=True,
                MIN_SPREAD_BPS=Decimal("5000"),
                MAX_SPREAD_BPS=Decimal("100"),
            )
        )
        self.assertFalse(report.is_valid)


if __name__ == "__main__":
    unittest.main()
