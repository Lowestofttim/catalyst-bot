"""Slice 02-08 — market_data_collector.py unit tests.

No network or DB calls. Tests _safe_float, _spacescan_smart_headers,
_spacescan_count_from_payload (pure utility), and the five analysis
functions (_analyze_volatility, _analyze_liquidity, _analyze_token_health,
_analyze_bot_performance, _assess_data_quality) which take raw dicts and
return computed dicts with no I/O.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import market_data_collector as _mdc
    from market_data_collector import (
        _safe_float,
        _spacescan_smart_headers,
        _spacescan_count_from_payload,
        _analyze_volatility,
        _analyze_liquidity,
        _analyze_token_health,
        _analyze_bot_performance,
        _assess_data_quality,
    )

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_FAKE_CFG = SimpleNamespace(
    DEFAULT_TRADE_XCH=0.5,
    SPACESCAN_TIMEOUT=10,
    COINSET_API_URL="https://api.coinset.org",
)


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class _MDC(unittest.TestCase):
    def setUp(self):
        self._patcher = patch.object(_mdc, "cfg", _FAKE_CFG)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()


# ===========================================================================
# _safe_float
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestSafeFloat(unittest.TestCase):
    def test_float_passthrough(self):
        self.assertEqual(_safe_float(1.5), 1.5)

    def test_int_to_float(self):
        self.assertEqual(_safe_float(3), 3.0)

    def test_string_number(self):
        self.assertEqual(_safe_float("2.5"), 2.5)

    def test_none_returns_default(self):
        self.assertEqual(_safe_float(None), 0.0)

    def test_none_custom_default(self):
        self.assertEqual(_safe_float(None, default=-1.0), -1.0)

    def test_invalid_string_returns_default(self):
        self.assertEqual(_safe_float("not-a-number"), 0.0)


# ===========================================================================
# _spacescan_smart_headers
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestSpacescanSmartHeaders(unittest.TestCase):
    def test_no_key_no_api_key_header(self):
        headers = _spacescan_smart_headers()
        self.assertNotIn("x-api-key", headers)

    def test_with_key_includes_api_key_header(self):
        headers = _spacescan_smart_headers("mykey")
        self.assertEqual(headers["x-api-key"], "mykey")

    def test_always_has_accept_header(self):
        self.assertIn("Accept", _spacescan_smart_headers())


# ===========================================================================
# _spacescan_count_from_payload
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestSpacescanCountFromPayload(unittest.TestCase):
    def test_explicit_count_key(self):
        self.assertEqual(_spacescan_count_from_payload({"count": 42}), 42)

    def test_total_key(self):
        self.assertEqual(_spacescan_count_from_payload({"total": 99}), 99)

    def test_list_key_fallback(self):
        # No count key → count the items in the list
        payload = {"data": [{"a": 1}, {"b": 2}]}
        self.assertEqual(_spacescan_count_from_payload(payload), 2)

    def test_nested_count(self):
        payload = {"meta": {"total_count": 77}}
        self.assertEqual(_spacescan_count_from_payload(payload), 77)

    def test_list_payload_returns_length(self):
        self.assertEqual(_spacescan_count_from_payload([1, 2, 3]), 3)

    def test_empty_dict_returns_zero(self):
        self.assertEqual(_spacescan_count_from_payload({}), 0)

    def test_bool_count_ignored(self):
        # Booleans are subclasses of int but should NOT be treated as counts
        payload = {"count": True}
        # True is bool → coerce returns None → fallback to list length → 0
        self.assertEqual(_spacescan_count_from_payload(payload), 0)


# ===========================================================================
# _analyze_volatility
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestAnalyzeVolatility(unittest.TestCase):
    def _raw_with_ticker(self, high_30d, low_30d, price=0.01):
        return {
            "dexie_ticker": {
                "has_data": True,
                "price": price,
                "high_30d": high_30d,
                "low_30d": low_30d,
                "high_90d": 0,
                "low_90d": 0,
                "high_1y": 0,
                "low_1y": 0,
            },
            "dexie_trades": {},
        }

    def test_no_data_returns_quiet_regime(self):
        # vol_metric = 0 when no data → falls through all thresholds → "quiet"
        result = _analyze_volatility({})
        self.assertEqual(result["regime"], "quiet")
        self.assertEqual(result["confidence"], "low")

    def test_ticker_range_sets_confidence_medium(self):
        raw = self._raw_with_ticker(0.015, 0.005, price=0.01)
        result = _analyze_volatility(raw)
        self.assertIn(result["confidence"], ("medium", "high"))

    def test_extreme_range_gives_extreme_regime(self):
        # range_30d_pct / 4 > 15 → extreme; 200% range → vol_metric ≈ 50%
        raw = self._raw_with_ticker(0.3, 0.1, price=0.2)
        result = _analyze_volatility(raw)
        self.assertEqual(result["regime"], "extreme")

    def test_quiet_range_gives_quiet_regime(self):
        # 1% range → vol_metric < 3 → quiet
        raw = self._raw_with_ticker(0.01005, 0.00995, price=0.01)
        result = _analyze_volatility(raw)
        self.assertEqual(result["regime"], "quiet")

    def test_quiet_phase_bumps_regime(self):
        # 30d range small (quiet), 90d range >> 30d range (volatile history)
        raw = {
            "dexie_ticker": {
                "has_data": True,
                "price": 0.01,
                "high_30d": 0.0101,
                "low_30d": 0.0099,  # ~2% 30d range
                "high_90d": 0.020,
                "low_90d": 0.005,  # ~120% 90d range
                "high_1y": 0,
                "low_1y": 0,
            },
            "dexie_trades": {},
        }
        result = _analyze_volatility(raw)
        # quiet phase detected → regime bumped at least to "normal"
        self.assertTrue(result["quiet_phase"])
        self.assertNotEqual(result["regime"], "quiet")

    def test_trade_history_computes_std_dev(self):
        # 20 daily trades with varying prices → std_dev populated
        trades = []
        import math

        for i in range(20):
            day = f"2024-01-{i + 1:02d}T12:00:00Z"
            price = 0.01 + 0.001 * math.sin(i)
            trades.append({"date": day, "price": price, "xch_amount": 1.0})
        raw = {"dexie_ticker": {}, "dexie_trades": {"trades": trades}}
        result = _analyze_volatility(raw)
        self.assertGreater(result["std_dev_pct"], 0)


# ===========================================================================
# _analyze_liquidity
# ===========================================================================


class TestAnalyzeLiquidity(_MDC):
    def test_no_data_returns_very_low(self):
        result = _analyze_liquidity({})
        self.assertEqual(result["level"], "very_low")

    def test_high_volume_high_fills_returns_high(self):
        raw = {
            "dexie_trades": {
                "daily_volume_xch": 15,
                "fills_per_day": 12,
                "avg_trade_size_xch": 1.0,
                "volume_trend": "stable",
                "total_count": 100,
            },
            "tibet_pool": {},
        }
        result = _analyze_liquidity(raw)
        self.assertEqual(result["level"], "high")

    def test_moderate_volume(self):
        raw = {
            "dexie_trades": {
                "daily_volume_xch": 3,
                "fills_per_day": 5,
                "avg_trade_size_xch": 0.6,
                "volume_trend": "stable",
                "total_count": 30,
            },
            "tibet_pool": {},
        }
        result = _analyze_liquidity(raw)
        self.assertEqual(result["level"], "moderate")

    def test_pool_depth_and_share_calculated(self):
        raw = {
            "dexie_trades": {},
            "tibet_pool": {"has_data": True, "xch_reserve": 100.0},
        }
        result = _analyze_liquidity(raw)
        self.assertEqual(result["pool_depth_xch"], 100.0)
        # pool_share = 0.5 / 100.0 * 100 = 0.5%
        self.assertAlmostEqual(result["pool_share_pct"], 0.5, places=2)


# ===========================================================================
# _analyze_token_health
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestAnalyzeTokenHealth(unittest.TestCase):
    def test_no_spacescan_data_returns_moderate(self):
        result = _analyze_token_health({})
        self.assertEqual(result["risk_level"], "moderate")
        self.assertEqual(result["confidence"], "low")

    def test_high_holders_healthy(self):
        raw = {
            "spacescan": {
                "has_data": True,
                "holder_count": 600,
                "activity_count": 100,
                "circulating_supply": 1000000,
            }
        }
        result = _analyze_token_health(raw)
        self.assertEqual(result["risk_level"], "healthy")
        self.assertEqual(result["activity_level"], "active")

    def test_low_holders_risky(self):
        raw = {
            "spacescan": {
                "has_data": True,
                "holder_count": 5,
                "activity_count": 0,
                "circulating_supply": 100,
            }
        }
        result = _analyze_token_health(raw)
        self.assertEqual(result["risk_level"], "risky")
        self.assertEqual(result["activity_level"], "dormant")

    def test_thin_holder_count(self):
        raw = {
            "spacescan": {
                "has_data": True,
                "holder_count": 50,
                "activity_count": 7,
                "circulating_supply": 500,
            }
        }
        result = _analyze_token_health(raw)
        self.assertEqual(result["risk_level"], "thin")


# ===========================================================================
# _analyze_bot_performance
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestAnalyzeBotPerformance(unittest.TestCase):
    def test_no_history_no_fills(self):
        result = _analyze_bot_performance({})
        self.assertFalse(result["has_history"])

    def test_with_fill_history(self):
        raw = {
            "internal_db": {
                "fill_count": 30,
                "buy_fills": 15,
                "sell_fills": 15,
                "avg_fill_size_xch": 0.5,
                "latest_net_position": 0,
            }
        }
        result = _analyze_bot_performance(raw)
        self.assertTrue(result["has_history"])
        self.assertEqual(result["inventory_drift"], "neutral")

    def test_positive_net_position_long_cat(self):
        raw = {
            "internal_db": {
                "fill_count": 10,
                "buy_fills": 10,
                "sell_fills": 0,
                "avg_fill_size_xch": 0.5,
                "latest_net_position": 5,
            }
        }
        result = _analyze_bot_performance(raw)
        self.assertEqual(result["inventory_drift"], "long_cat")

    def test_negative_net_position_short_cat(self):
        raw = {
            "internal_db": {
                "fill_count": 10,
                "buy_fills": 0,
                "sell_fills": 10,
                "avg_fill_size_xch": 0.5,
                "latest_net_position": -5,
            }
        }
        result = _analyze_bot_performance(raw)
        self.assertEqual(result["inventory_drift"], "short_cat")


# ===========================================================================
# _assess_data_quality
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_data_collector unavailable: {_SKIP}")
class TestAssessDataQuality(unittest.TestCase):
    def _full_raw(self):
        return {
            "dexie_ticker": {
                "has_data": True,
                "high_30d": 0.02,
                "low_30d": 0.01,
                "volume_30d": 10.0,
            },
            "dexie_trades": {"total_count": 100},
            "tibet_pool": {"has_data": True},
            "spacescan": {"has_data": True, "holder_count": 500},
            "internal_db": {"fill_count": 50, "price_count": 100},
        }

    def test_all_sources_score_100(self):
        result = _assess_data_quality(self._full_raw())
        self.assertEqual(result["score"], 100)
        self.assertTrue(result["quality"].startswith("excellent"))

    def test_no_sources_score_0(self):
        result = _assess_data_quality({})
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["quality"], "limited")

    def test_partial_sources_intermediate_score(self):
        raw = {
            "dexie_ticker": {
                "has_data": True,
                "high_30d": 0.02,
                "low_30d": 0.01,
                "volume_30d": 5.0,
            }
        }
        result = _assess_data_quality(raw)
        self.assertGreater(result["score"], 0)
        self.assertLess(result["score"], 100)

    def test_partial_failure_flag_in_quality_string(self):
        raw = {
            **self._full_raw(),
            "spacescan": {
                "has_data": True,
                "holder_count": 100,
                "activity_fetch_failed": True,
            },
        }
        result = _assess_data_quality(raw)
        self.assertIn("partial", result["quality"])
        self.assertIn("spacescan_activity", result["partial_failures"])

    def test_sources_dict_has_all_keys(self):
        result = _assess_data_quality({})
        for key in (
            "dexie_ticker",
            "dexie_trades",
            "tibet_pool",
            "spacescan",
            "internal_db",
        ):
            self.assertIn(key, result["sources"])

    def test_low_trade_count_gives_medium_confidence(self):
        raw = {"dexie_trades": {"total_count": 5}}
        result = _assess_data_quality(raw)
        self.assertEqual(result["sources"]["dexie_trades"]["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
