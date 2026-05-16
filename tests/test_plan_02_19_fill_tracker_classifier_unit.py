"""Slice 02-19 — fill_tracker.py + fill_classifier.py unit tests.

No network calls. Tests fill_classifier pure paths (FillType, FillClassification,
classify_fill decision tree, _extract_taker_puzzle_hash) and FillTracker pure
helpers (_check_mass_disappearance, _parse_iso_ts, _extract_dexie_coin_ids,
should_protect_side, time_since_last_fill, get_fill_history, set_baseline).
"""

import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# fill_classifier
# ---------------------------------------------------------------------------
try:
    from fill_classifier import (
        FillType,
        FillClassification,
        classify_fill,
        _extract_taker_puzzle_hash,
    )

    _SKIP_FC = None
except ModuleNotFoundError as exc:
    _SKIP_FC = str(exc)

# ---------------------------------------------------------------------------
# fill_tracker
# ---------------------------------------------------------------------------
try:
    import fill_tracker as _ft_mod
    from fill_tracker import FillTracker

    _SKIP_FT = None
except ModuleNotFoundError as exc:
    _SKIP_FT = str(exc)


# ===========================================================================
# FillType constants
# ===========================================================================


@unittest.skipIf(_SKIP_FC is not None, f"fill_classifier unavailable: {_SKIP_FC}")
class TestFillTypeConstants(unittest.TestCase):
    def test_all_expected_types_defined(self):
        for attr in (
            "RETAIL",
            "ARB_SWEEP_BUY",
            "ARB_SWEEP_SELL",
            "DEXIE_COMBINED",
            "UNKNOWN",
        ):
            self.assertTrue(hasattr(FillType, attr))

    def test_values_are_strings(self):
        self.assertIsInstance(FillType.RETAIL, str)
        self.assertIsInstance(FillType.UNKNOWN, str)


# ===========================================================================
# FillClassification.is_arb
# ===========================================================================


@unittest.skipIf(_SKIP_FC is not None, f"fill_classifier unavailable: {_SKIP_FC}")
class TestFillClassificationIsArb(unittest.TestCase):
    def test_retail_is_not_arb(self):
        fc = FillClassification(trade_id="t1", classification=FillType.RETAIL)
        self.assertFalse(fc.is_arb())

    def test_unknown_is_not_arb(self):
        fc = FillClassification(trade_id="t1", classification=FillType.UNKNOWN)
        self.assertFalse(fc.is_arb())

    def test_arb_sweep_buy_is_arb(self):
        fc = FillClassification(trade_id="t1", classification=FillType.ARB_SWEEP_BUY)
        self.assertTrue(fc.is_arb())

    def test_arb_sweep_sell_is_arb(self):
        fc = FillClassification(trade_id="t1", classification=FillType.ARB_SWEEP_SELL)
        self.assertTrue(fc.is_arb())

    def test_dexie_combined_is_arb(self):
        fc = FillClassification(trade_id="t1", classification=FillType.DEXIE_COMBINED)
        self.assertTrue(fc.is_arb())


# ===========================================================================
# classify_fill decision tree
# ===========================================================================


@unittest.skipIf(_SKIP_FC is not None, f"fill_classifier unavailable: {_SKIP_FC}")
class TestClassifyFill(unittest.TestCase):
    def _fill_detail(self, side="buy"):
        return {"side": side, "coin_id": "0xabc", "tier": "inner"}

    def test_no_dexie_data_returns_unknown(self):
        result = classify_fill("t1", self._fill_detail(), dexie_detail=None)
        self.assertEqual(result.classification, FillType.UNKNOWN)
        self.assertEqual(result.confidence, "low")

    def test_dexie_data_no_signals_returns_retail(self):
        dexie = {"status": "filled"}
        result = classify_fill("t1", self._fill_detail(), dexie_detail=dexie)
        self.assertEqual(result.classification, FillType.RETAIL)
        self.assertEqual(result.confidence, "medium")

    def test_dexie_combined_flag_returns_dexie_combined(self):
        dexie = {"combined": True}
        result = classify_fill("t1", self._fill_detail(), dexie_detail=dexie)
        self.assertEqual(result.classification, FillType.DEXIE_COMBINED)
        self.assertEqual(result.confidence, "high")

    def test_matched_offers_list_returns_dexie_combined(self):
        dexie = {"matched_offers": ["offer1", "offer2"]}
        result = classify_fill("t1", self._fill_detail(), dexie_detail=dexie)
        self.assertEqual(result.classification, FillType.DEXIE_COMBINED)

    def test_known_arb_hash_sell_returns_arb_sweep_buy(self):
        arb_hash = "abcdef1234567890"
        dexie = {
            "output_coins": {
                "xch": [{"puzzle_hash": f"0x{arb_hash}", "amount": 1000}],
            }
        }
        fake_cfg = SimpleNamespace(
            KNOWN_ARB_PUZZLE_HASHES=[f"0x{arb_hash}"],
            WALLET_ID_XCH=1,
            CAT_ASSET_ID="cat_asset",
        )
        with patch("config.cfg", fake_cfg):
            result = classify_fill("t1", {"side": "sell"}, dexie_detail=dexie)
        self.assertEqual(result.classification, FillType.ARB_SWEEP_BUY)
        self.assertEqual(result.confidence, "high")

    def test_known_arb_hash_buy_returns_arb_sweep_sell(self):
        arb_hash = "abcdef1234567890"
        dexie = {
            "output_coins": {
                "xch": [{"puzzle_hash": f"0x{arb_hash}", "amount": 1000}],
            }
        }
        fake_cfg = SimpleNamespace(
            KNOWN_ARB_PUZZLE_HASHES=[f"0x{arb_hash}"],
            WALLET_ID_XCH=1,
            CAT_ASSET_ID="cat_asset",
        )
        with patch("config.cfg", fake_cfg):
            result = classify_fill("t1", {"side": "buy"}, dexie_detail=dexie)
        self.assertEqual(result.classification, FillType.ARB_SWEEP_SELL)

    def test_result_has_reasons_populated(self):
        result = classify_fill("t1", self._fill_detail(), dexie_detail=None)
        self.assertIsInstance(result.reasons, list)
        self.assertGreater(len(result.reasons), 0)

    def test_spent_block_index_extracted_from_dexie(self):
        dexie = {"spent_block_index": 1234567, "status": "filled"}
        result = classify_fill("t1", self._fill_detail(), dexie_detail=dexie)
        self.assertEqual(result.spent_block_index, 1234567)


# ===========================================================================
# _extract_taker_puzzle_hash
# ===========================================================================


@unittest.skipIf(_SKIP_FC is not None, f"fill_classifier unavailable: {_SKIP_FC}")
class TestExtractTakerPuzzleHash(unittest.TestCase):
    def test_none_detail_returns_none(self):
        self.assertIsNone(_extract_taker_puzzle_hash(None, "buy"))

    def test_no_output_coins_returns_none(self):
        self.assertIsNone(_extract_taker_puzzle_hash({}, "buy"))

    def test_buy_side_reads_xch_output(self):
        detail = {
            "output_coins": {
                "xch": [{"puzzle_hash": "0xABCDEF", "amount": 1000}],
            }
        }
        with patch("config.cfg", SimpleNamespace(WALLET_ID_XCH=1, CAT_ASSET_ID="")):
            ph = _extract_taker_puzzle_hash(detail, "buy")
        self.assertEqual(ph, "abcdef")  # lowercased, 0x stripped

    def test_fallback_to_first_key_when_no_match(self):
        detail = {
            "output_coins": {
                "some_token": [{"puzzle_hash": "0x1234", "amount": 500}],
            }
        }
        with patch("config.cfg", SimpleNamespace(WALLET_ID_XCH=1, CAT_ASSET_ID="")):
            ph = _extract_taker_puzzle_hash(detail, "buy")
        self.assertEqual(ph, "1234")


# ===========================================================================
# FillTracker — pure static helpers
# ===========================================================================


@unittest.skipIf(_SKIP_FT is not None, f"fill_tracker unavailable: {_SKIP_FT}")
class TestFillTrackerParseIsoTs(unittest.TestCase):
    def test_valid_iso_returns_float(self):
        ts = FillTracker._parse_iso_ts("2024-01-15T12:00:00Z")
        self.assertIsInstance(ts, float)
        self.assertGreater(ts, 0)

    def test_none_returns_none(self):
        self.assertIsNone(FillTracker._parse_iso_ts(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(FillTracker._parse_iso_ts(""))

    def test_invalid_format_returns_none(self):
        self.assertIsNone(FillTracker._parse_iso_ts("not-a-date"))


@unittest.skipIf(_SKIP_FT is not None, f"fill_tracker unavailable: {_SKIP_FT}")
class TestFillTrackerExtractDexieCoinIds(unittest.TestCase):
    def test_extracts_from_input_coins(self):
        detail = {
            "input_coins": {
                "xch": [{"id": "0xABC", "amount": 1000}],
            }
        }
        ids = FillTracker._extract_dexie_coin_ids(detail)
        self.assertIn("abc", ids)

    def test_extracts_from_output_coins(self):
        detail = {
            "output_coins": {
                "xch": [{"id": "0xDEF", "amount": 500}],
            }
        }
        ids = FillTracker._extract_dexie_coin_ids(detail)
        self.assertIn("def", ids)

    def test_empty_detail_returns_empty_set(self):
        ids = FillTracker._extract_dexie_coin_ids({})
        self.assertIsInstance(ids, set)
        self.assertEqual(len(ids), 0)

    def test_strips_0x_prefix(self):
        detail = {"input_coins": {"xch": [{"id": "0x1234abc", "amount": 100}]}}
        ids = FillTracker._extract_dexie_coin_ids(detail)
        self.assertIn("1234abc", ids)


# ===========================================================================
# FillTracker — _check_mass_disappearance
# ===========================================================================


@unittest.skipIf(_SKIP_FT is not None, f"fill_tracker unavailable: {_SKIP_FT}")
class TestCheckMassDisappearance(unittest.TestCase):
    def _make_ft(self):
        with patch("fill_tracker.log_event"):
            ft = FillTracker(offer_manager=None)
        return ft

    @patch("fill_tracker.log_event")
    def test_zero_previous_is_safe(self, _mock_log):
        ft = self._make_ft()
        self.assertTrue(ft._check_mass_disappearance(10, 0))

    @patch("fill_tracker.log_event")
    def test_small_disappearance_is_safe(self, _mock_log):
        ft = self._make_ft()
        # 2 disappeared out of 10 = 20%, below 50% threshold
        self.assertTrue(ft._check_mass_disappearance(2, 10))

    @patch("fill_tracker.log_event")
    def test_mass_disappearance_first_call_returns_false(self, _mock_log):
        ft = self._make_ft()
        # 8 disappeared out of 10 = 80%, triggers guard on first hit → returns False
        result = ft._check_mass_disappearance(8, 10)
        self.assertFalse(result)
        self.assertEqual(ft._mass_disappearance_count, 1)

    @patch("fill_tracker.log_event")
    def test_mass_disappearance_three_strikes_returns_true(self, _mock_log):
        ft = self._make_ft()
        ft._check_mass_disappearance(8, 10)  # 1st — False
        ft._check_mass_disappearance(8, 10)  # 2nd — False
        result = ft._check_mass_disappearance(8, 10)  # 3rd — True
        self.assertTrue(result)
        self.assertEqual(ft._mass_disappearance_count, 0)  # reset after 3rd


# ===========================================================================
# FillTracker — state helpers
# ===========================================================================


@unittest.skipIf(_SKIP_FT is not None, f"fill_tracker unavailable: {_SKIP_FT}")
class TestFillTrackerStateHelpers(unittest.TestCase):
    def _make_ft(self):
        with patch("fill_tracker.log_event"):
            return FillTracker()

    @patch("fill_tracker.log_event")
    def test_time_since_last_fill_inf_before_any_fill(self, _mock_log):
        ft = self._make_ft()
        self.assertEqual(ft.time_since_last_fill("buy"), float("inf"))

    @patch("fill_tracker.log_event")
    def test_time_since_last_fill_recent(self, _mock_log):
        ft = self._make_ft()
        ft._last_fill_time["buy"] = time.time() - 5
        elapsed = ft.time_since_last_fill("buy")
        self.assertLess(elapsed, 10)
        self.assertGreater(elapsed, 0)

    def test_offer_filled_log_message_is_ascii_console_safe(self):
        msg = FillTracker._format_offer_filled_log_message(
            side="sell",
            coin_id="0xabcdef1234567890",
            price=1.25,
            size_xch="2.5",
            size_cat=1000,
            tier="mid",
            mempool_warned=False,
        )

        msg.encode("ascii")
        self.assertIn("SELL offer filled", msg)

    def test_offer_filled_log_message_marks_mempool_hit(self):
        msg = FillTracker._format_offer_filled_log_message(
            side="sell",
            coin_id="0xabcdef1234567890",
            price=1.25,
            size_xch="2.5",
            size_cat=1000,
            tier="mid",
            mempool_warned=True,
        )

        self.assertIn("mempool-hit", msg)
        self.assertNotIn("mempool-miss", msg)

    @patch("fill_tracker.log_event")
    def test_get_fill_history_empty(self, _mock_log):
        ft = self._make_ft()
        self.assertEqual(ft.get_fill_history(), [])

    @patch("fill_tracker.log_event")
    def test_get_fill_history_limit_respected(self, _mock_log):
        ft = self._make_ft()
        ft._fill_history = [{"tid": f"t{i}"} for i in range(30)]
        result = ft.get_fill_history(limit=5)
        self.assertEqual(len(result), 5)

    @patch("fill_tracker.log_event")
    def test_get_fill_counts_returns_dict(self, _mock_log):
        ft = self._make_ft()
        counts = ft.get_fill_counts()
        self.assertIn("buy", counts)
        self.assertIn("sell", counts)

    @patch("fill_tracker.log_event")
    def test_set_baseline_stores_ids(self, _mock_log):
        ft = self._make_ft()
        ft.set_baseline({"id1", "id2"}, {"id3"})
        self.assertIn("id1", ft._previous_ids["buy"])
        self.assertIn("id3", ft._previous_ids["sell"])


if __name__ == "__main__":
    unittest.main()
