"""Slice 02-09 — market_intel.py unit tests.

No HTTP calls. Tests _bps_to_pct (pure), _parse_dexie_offer, _analyse_orderbook
(direct injection), and all state-query methods (get_competitor_spread,
get_cached_data, get_stats, reset_session_stats, get_market_summary,
get_orderbook_snapshot, get_spread_recommendation, check_dbx_eligibility).
"""

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

try:
    import market_intel as _mi_mod
    from market_intel import MarketIntel, _bps_to_pct

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_FAKE_CFG = SimpleNamespace(
    DBX_MAX_SPREAD_BPS=Decimal("500"),
    DEXIE_ORDERBOOK_PAGE_SIZE=200,
    CAT_ASSET_ID="abc123cat",
    BOT_TAG="catalyst-bot",
    DEXIE_API_BASE="https://dexie.space",
)


# ---------------------------------------------------------------------------
# Base class: patch market_intel.cfg for constructor and all method calls
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"market_intel unavailable: {_SKIP}")
class _MI(unittest.TestCase):
    def setUp(self):
        # patch.object targets the module object directly, not via sys.modules lookup.
        # test_market_intel_orderbook pops market_intel from sys.modules in tearDown,
        # so string-based patch("market_intel.cfg") resolves to the wrong module object.
        self._patcher = patch.object(_mi_mod, "cfg", _FAKE_CFG)
        self._patcher.start()
        self._mi = MarketIntel(price_engine=None)

    def tearDown(self):
        self._patcher.stop()

    def _make_offer(self, price, xch_amount, is_ours=False, side="buy"):
        return {
            "offer_id": "x",
            "side": side,
            "price": Decimal(str(price)),
            "xch_amount": Decimal(str(xch_amount)),
            "cat_amount": Decimal("100"),
            "is_ours": is_ours,
        }


# ===========================================================================
# _bps_to_pct  (module-level pure function — no base class needed)
# ===========================================================================


@unittest.skipIf(_SKIP is not None, f"market_intel unavailable: {_SKIP}")
class TestBpsToPct(unittest.TestCase):
    def test_small_value_two_decimal_places(self):
        self.assertEqual(_bps_to_pct(50), "0.50%")

    def test_large_value_one_decimal_place(self):
        self.assertEqual(_bps_to_pct(200), "2.0%")

    def test_zero_returns_formatted_percent(self):
        self.assertEqual(_bps_to_pct(0), "0.00%")

    def test_exactly_100_bps_boundary(self):
        # 100 / 100 = 1.0 — not < 1 → one decimal
        self.assertEqual(_bps_to_pct(100), "1.0%")

    def test_99_bps_below_boundary(self):
        # 99 / 100 = 0.99 → two decimals
        self.assertEqual(_bps_to_pct(99), "0.99%")

    def test_none_returns_str_none(self):
        self.assertEqual(_bps_to_pct(None), "None")

    def test_invalid_string_returns_string(self):
        self.assertEqual(_bps_to_pct("x"), "x")


# ===========================================================================
# _parse_dexie_offer
# ===========================================================================


class TestParseDexieOffer(_MI):
    def _sell_offer(
        self, offer_id="offer-001", cat_amount="100", xch_amount="0.5", tags=None
    ):
        return {
            "id": offer_id,
            "offered": [{"code": "CAT", "id": "abc123cat", "amount": cat_amount}],
            "requested": [{"code": "XCH", "id": "xch", "amount": xch_amount}],
            "tags": tags or [],
            "date_found": "",
        }

    def test_valid_sell_offer_price_correct(self):
        parsed = self._mi._parse_dexie_offer(self._sell_offer(), "sell")
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(float(parsed["price"]), 0.005)  # 0.5 / 100

    def test_valid_sell_offer_side_preserved(self):
        parsed = self._mi._parse_dexie_offer(self._sell_offer(), "sell")
        self.assertEqual(parsed["side"], "sell")

    def test_zero_xch_returns_none(self):
        parsed = self._mi._parse_dexie_offer(self._sell_offer(xch_amount="0"), "sell")
        self.assertIsNone(parsed)

    def test_zero_cat_returns_none(self):
        parsed = self._mi._parse_dexie_offer(self._sell_offer(cat_amount="0"), "sell")
        self.assertIsNone(parsed)

    def test_is_ours_detected_via_known_dexie_ids(self):
        self._mi._known_dexie_ids = {"offer-abc"}
        offer = {
            "id": "offer-abc",
            "offered": [{"code": "XCH", "id": "xch", "amount": "0.5"}],
            "requested": [{"code": "CAT", "id": "abc123cat", "amount": "100"}],
            "tags": [],
        }
        parsed = self._mi._parse_dexie_offer(offer, "buy")
        self.assertTrue(parsed["is_ours"])

    def test_is_ours_detected_via_bot_tag(self):
        offer = {
            "id": "offer-tagged",
            "offered": [{"code": "XCH", "id": "xch", "amount": "0.5"}],
            "requested": [{"code": "CAT", "id": "abc123cat", "amount": "100"}],
            "tags": ["catalyst-bot", "other"],
        }
        parsed = self._mi._parse_dexie_offer(offer, "buy")
        self.assertTrue(parsed["is_ours"])

    def test_not_ours_when_unrelated_tag(self):
        offer = {
            "id": "offer-stranger",
            "offered": [{"code": "XCH", "id": "xch", "amount": "0.5"}],
            "requested": [{"code": "CAT", "id": "abc123cat", "amount": "100"}],
            "tags": ["other-bot"],
        }
        parsed = self._mi._parse_dexie_offer(offer, "buy")
        self.assertFalse(parsed["is_ours"])

    def test_malformed_offer_returns_none(self):
        # Causes an exception — should return None, not raise
        parsed = self._mi._parse_dexie_offer(None, "buy")
        self.assertIsNone(parsed)

    def test_offer_age_seconds_from_iso_date_found(self):
        offer = self._sell_offer()
        offer["date_found"] = "2026-05-16T11:59:00Z"

        with patch.object(_mi_mod.time, "time", return_value=1778932800):
            parsed = self._mi._parse_dexie_offer(offer, "sell")

        self.assertEqual(parsed["age_secs"], 60)

    def test_offer_age_seconds_from_epoch_milliseconds(self):
        offer = self._sell_offer()
        offer["date_found"] = "1778932790000"

        with patch.object(_mi_mod.time, "time", return_value=1778932800):
            parsed = self._mi._parse_dexie_offer(offer, "sell")

        self.assertEqual(parsed["age_secs"], 10)


# ===========================================================================
# _analyse_orderbook
# ===========================================================================


class TestAnalyseOrderbook(_MI):
    def test_empty_orderbook_all_zeros(self):
        self._mi._analyse_orderbook([], [])
        c = self._mi._competitors
        self.assertEqual(c["best_bid"], Decimal("0"))
        self.assertEqual(c["best_ask"], Decimal("0"))
        self.assertEqual(c["competitor_spread_bps"], Decimal("0"))
        self.assertEqual(c["thin_side"], "")

    def test_competitor_spread_calculated_correctly(self):
        buys = [self._make_offer(0.009, 1.0, side="buy")]
        sells = [self._make_offer(0.011, 1.0, side="sell")]
        self._mi._analyse_orderbook(buys, sells)
        # mid=0.010, spread=(0.011-0.009)/0.010 * 10000 = 2000 bps
        self.assertAlmostEqual(
            float(self._mi._competitors["competitor_spread_bps"]), 2000, delta=1
        )

    def test_inverted_book_zeros_best_bid_and_ask(self):
        # bid > ask → inverted → zeroed
        buys = [self._make_offer(0.015, 1.0, side="buy")]
        sells = [self._make_offer(0.010, 1.0, side="sell")]
        self._mi._analyse_orderbook(buys, sells)
        self.assertEqual(self._mi._competitors["best_bid"], Decimal("0"))
        self.assertEqual(self._mi._competitors["best_ask"], Decimal("0"))

    def test_own_offers_excluded_from_competitor_bid(self):
        buys = [
            self._make_offer(0.010, 1.0, is_ours=True, side="buy"),
            self._make_offer(0.009, 1.0, is_ours=False, side="buy"),
        ]
        sells = [self._make_offer(0.011, 1.0, side="sell")]
        self._mi._analyse_orderbook(buys, sells)
        self.assertEqual(self._mi._competitors["best_bid"], Decimal("0.009"))
        self.assertEqual(self._mi._competitors["num_buy_offers"], 2)
        self.assertEqual(self._mi._competitors["num_competitor_buys"], 1)

    def test_thin_sell_side_detected(self):
        # buy_depth=10, sell_depth=2 → ratio=5 > 3 → "sell" is thin
        buys = [self._make_offer(0.010, 10.0, side="buy")]
        sells = [self._make_offer(0.011, 2.0, side="sell")]
        self._mi._analyse_orderbook(buys, sells)
        self.assertEqual(self._mi._competitors["thin_side"], "sell")

    def test_thin_buy_side_detected(self):
        # buy_depth=2, sell_depth=10 → ratio=0.2 < 0.33 → "buy" is thin
        buys = [self._make_offer(0.010, 2.0, side="buy")]
        sells = [self._make_offer(0.011, 10.0, side="sell")]
        self._mi._analyse_orderbook(buys, sells)
        self.assertEqual(self._mi._competitors["thin_side"], "buy")

    def test_depth_ignores_far_out_junk_offers(self):
        buys = [self._make_offer("0.000114", "10", side="buy")]
        sells = [
            self._make_offer("0.000123", "7", side="sell"),
            self._make_offer("0.0022", "10000", side="sell"),
        ]

        self._mi._analyse_orderbook(buys, sells)

        self.assertEqual(self._mi._competitors["buy_depth_xch"], Decimal("10"))
        self.assertEqual(self._mi._competitors["sell_depth_xch"], Decimal("7"))
        self.assertEqual(self._mi._competitors["thin_side"], "")

    def test_whale_orders_captured(self):
        buys = [self._make_offer(0.010, 2.0, side="buy")]  # xch_amount >= 1 → whale
        sells = [self._make_offer(0.011, 0.5, side="sell")]  # not whale
        self._mi._analyse_orderbook(buys, sells)
        self.assertEqual(len(self._mi._competitors["whale_orders"]), 1)
        self.assertEqual(self._mi._competitors["whale_orders"][0]["side"], "buy")

    def test_whale_orders_capped_at_5(self):
        buys = [self._make_offer(0.010 - i * 0.001, 2.0, side="buy") for i in range(7)]
        self._mi._analyse_orderbook(buys, [])
        self.assertLessEqual(len(self._mi._competitors["whale_orders"]), 5)


# ===========================================================================
# State query methods
# ===========================================================================


class TestStateQueryMethods(_MI):
    def test_get_competitor_spread_returns_dict(self):
        result = self._mi.get_competitor_spread()
        self.assertIsInstance(result, dict)
        self.assertIn("best_bid", result)

    def test_get_competitor_spread_is_copy(self):
        r1 = self._mi.get_competitor_spread()
        r2 = self._mi.get_competitor_spread()
        self.assertIsNot(r1, r2)

    def test_get_cached_data_matches_competitor_spread(self):
        self._mi._competitors["best_bid"] = Decimal("0.010")
        self.assertEqual(self._mi.get_competitor_spread(), self._mi.get_cached_data())

    def test_get_stats_has_all_keys(self):
        stats = self._mi.get_stats()
        for key in (
            "competitor_spread_bps",
            "best_bid",
            "best_ask",
            "buy_depth_xch",
            "sell_depth_xch",
            "thin_side",
        ):
            self.assertIn(key, stats)

    def test_get_stats_values_are_strings(self):
        stats = self._mi.get_stats()
        self.assertIsInstance(stats["competitor_spread_bps"], str)
        self.assertIsInstance(stats["best_bid"], str)
        self.assertIsInstance(stats["thin_side"], str)

    def test_reset_session_stats_clears_counters(self):
        self._mi._orderbook["refresh_count"] = 10
        self._mi._orderbook["errors"] = 3
        self._mi._known_dexie_ids = {"id1", "id2"}
        self._mi._dbx["eligible_offers"] = 1
        self._mi.reset_session_stats()
        self.assertEqual(self._mi._orderbook["refresh_count"], 0)
        self.assertEqual(self._mi._orderbook["errors"], 0)
        self.assertEqual(len(self._mi._known_dexie_ids), 0)
        self.assertEqual(self._mi._dbx["eligible_offers"], 0)

    def test_get_market_summary_decimals_are_strings(self):
        self._mi._competitors["best_bid"] = Decimal("0.010")
        summary = self._mi.get_market_summary()
        self.assertIsInstance(summary["best_bid"], str)

    def test_get_market_summary_has_dbx_block(self):
        summary = self._mi.get_market_summary()
        self.assertIn("dbx", summary)
        self.assertIn("eligible", summary["dbx"])
        self.assertIn("max_spread_bps", summary["dbx"])

    def test_get_market_summary_has_orderbook_metadata(self):
        summary = self._mi.get_market_summary()
        self.assertIn("orderbook_age_secs", summary)
        self.assertIn("orderbook_refreshes", summary)
        self.assertIn("orderbook_errors", summary)

    def test_get_orderbook_snapshot_empty(self):
        snap = self._mi.get_orderbook_snapshot()
        self.assertEqual(snap["buy_count"], 0)
        self.assertEqual(snap["sell_count"], 0)
        self.assertEqual(snap["our_buy_count"], 0)

    def test_get_orderbook_snapshot_counts(self):
        self._mi._orderbook["buy_offers"] = [
            {"price": Decimal("0.010"), "xch_amount": Decimal("1"), "is_ours": True},
            {"price": Decimal("0.009"), "xch_amount": Decimal("1"), "is_ours": False},
        ]
        self._mi._orderbook["sell_offers"] = [
            {"price": Decimal("0.011"), "xch_amount": Decimal("1"), "is_ours": False},
        ]
        snap = self._mi.get_orderbook_snapshot()
        self.assertEqual(snap["buy_count"], 2)
        self.assertEqual(snap["sell_count"], 1)
        self.assertEqual(snap["our_buy_count"], 1)
        self.assertEqual(snap["our_sell_count"], 0)

    def test_get_orderbook_snapshot_our_best_bid(self):
        self._mi._orderbook["buy_offers"] = [
            {"price": Decimal("0.010"), "xch_amount": Decimal("1"), "is_ours": True},
            {"price": Decimal("0.009"), "xch_amount": Decimal("1"), "is_ours": True},
        ]
        snap = self._mi.get_orderbook_snapshot()
        self.assertEqual(snap["our_best_bid"], "0.010")


# ===========================================================================
# get_spread_recommendation
# ===========================================================================


class TestGetSpreadRecommendation(_MI):
    def test_no_competitor_data_returns_zero(self):
        # comp_spread defaults to 0 → return 0
        adj = self._mi.get_spread_recommendation(
            "buy", Decimal("100"), Decimal("0.010")
        )
        self.assertEqual(adj, Decimal("0"))

    def test_zero_mid_price_returns_zero(self):
        self._mi._competitors["competitor_spread_bps"] = Decimal("300")
        adj = self._mi.get_spread_recommendation("buy", Decimal("100"), Decimal("0"))
        self.assertEqual(adj, Decimal("0"))

    def test_competitors_much_wider_returns_positive_adjustment(self):
        # comp=600, ours=100 → diff=500 > 200 → widen by 500*0.25=125
        self._mi._competitors["competitor_spread_bps"] = Decimal("600")
        adj = self._mi.get_spread_recommendation(
            "buy", Decimal("100"), Decimal("0.010")
        )
        self.assertGreater(adj, Decimal("0"))

    def test_competitors_tighter_returns_negative_adjustment(self):
        # comp=50, ours=300 → diff=-250 < -100 → tighten
        self._mi._competitors["competitor_spread_bps"] = Decimal("50")
        adj = self._mi.get_spread_recommendation(
            "buy", Decimal("300"), Decimal("0.010")
        )
        self.assertLess(adj, Decimal("0"))

    def test_thin_side_adds_50bps_tightening(self):
        # Both calls have comp wider by >200 bps → positive base adjustment.
        # When this side is thin → subtract 50 → result should be lower.
        self._mi._competitors["competitor_spread_bps"] = Decimal("600")
        self._mi._competitors["thin_side"] = "buy"
        adj_not_thin = self._mi.get_spread_recommendation(
            "sell", Decimal("100"), Decimal("0.010")
        )
        adj_thin = self._mi.get_spread_recommendation(
            "buy", Decimal("100"), Decimal("0.010")
        )
        self.assertLess(adj_thin, adj_not_thin)


# ===========================================================================
# check_dbx_eligibility
# ===========================================================================


class TestCheckDbxEligibility(_MI):
    """Eligibility now reads per-pair limits from /v1/incentives (cached).

    The tests stub ``dexie_incentives.get_pair_incentives`` so they don't
    hit the network — eligibility flips on whether the stub reports the
    pair as incentivized AND whether the spread is within the live cap.
    """

    _FAKE_PAIR = {
        "incentivized": True,
        "buy": {
            "range_min": 0.1,
            "range_max": 20.0,
            "range_unit": "XCH",
            "max_spread_bps": 500,
            "max_spread_pct": 0.05,
            "reward_token": "DBX",
            "reward_amount_per_day": 100.0,
            "estimated_apr": 0.5,
            "within_spread_liquidity": 350.0,
            "market_price": 0.0001,
        },
        "sell": {
            "range_min": 10000.0,
            "range_max": 1000000.0,
            "range_unit": "CAT",
            "max_spread_bps": 500,
            "max_spread_pct": 0.05,
            "reward_token": "DBX",
            "reward_amount_per_day": 100.0,
            "estimated_apr": 0.4,
            "within_spread_liquidity": 4000000.0,
            "market_price": 0.0001,
        },
    }
    _UN_PAIR = {"incentivized": False, "buy": None, "sell": None}

    def _patch_pair(self, pair):
        import dexie_incentives

        return patch.object(dexie_incentives, "get_pair_incentives", return_value=pair)

    def test_spread_within_limit_is_eligible(self):
        self._mi._dbx["last_check"] = 0
        with self._patch_pair(self._FAKE_PAIR):
            result = self._mi.check_dbx_eligibility(Decimal("200"), Decimal("0.010"))
        # Both buy and sell sides qualify at 200 bps (cap is 500)
        self.assertEqual(result["eligible_offers"], 2)
        self.assertGreater(result["estimated_dbx_rate"], Decimal("0"))

    def test_spread_exceeds_limit_is_ineligible(self):
        self._mi._dbx["last_check"] = 0
        with self._patch_pair(self._FAKE_PAIR):
            result = self._mi.check_dbx_eligibility(Decimal("600"), Decimal("0.010"))
        self.assertEqual(result["eligible_offers"], 0)

    def test_pair_not_incentivized(self):
        self._mi._dbx["last_check"] = 0
        with self._patch_pair(self._UN_PAIR):
            result = self._mi.check_dbx_eligibility(Decimal("200"), Decimal("0.010"))
        self.assertEqual(result["eligible_offers"], 0)
        self.assertFalse(result["pair_incentivized"])

    def test_second_call_within_interval_returns_cached(self):
        self._mi._dbx["last_check"] = 0
        with self._patch_pair(self._FAKE_PAIR):
            r1 = self._mi.check_dbx_eligibility(Decimal("200"), Decimal("0.010"))
        with self._patch_pair(self._UN_PAIR):
            # Within cooldown — even a different pair stub should NOT flip the result
            r2 = self._mi.check_dbx_eligibility(Decimal("600"), Decimal("0.010"))
        self.assertEqual(r2["eligible_offers"], r1["eligible_offers"])


if __name__ == "__main__":
    unittest.main()
