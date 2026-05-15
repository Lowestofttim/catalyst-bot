"""Tests for fill_classifier.py."""

import sys
import types
import unittest


class _FakeCfg:
    CAT_ASSET_ID = "aabbccdd"
    WALLET_ID_XCH = 1
    KNOWN_ARB_PUZZLE_HASHES = ["deadbeef000000", "cafebabe111111"]


def _install_fakes():
    fake_config = types.ModuleType("config")
    fake_config.cfg = _FakeCfg()
    sys.modules["config"] = fake_config

    fake_database = types.ModuleType("database")
    fake_database.get_connection = lambda: _FakeConn()
    sys.modules["database"] = fake_database


class _FakeConn:
    def __init__(self):
        self.last_sql = None
        self.last_params = None

    def execute(self, sql, params=()):
        self.last_sql = sql
        self.last_params = params
        return self

    def commit(self):
        pass

    def fetchone(self):
        return None


class FillClassifierTests(unittest.TestCase):
    def setUp(self):
        _install_fakes()
        sys.modules.pop("fill_classifier", None)
        import fill_classifier

        self.fc = fill_classifier

    def tearDown(self):
        for name in ["fill_classifier", "database", "config"]:
            sys.modules.pop(name, None)

    # ------------------------------------------------------------------
    # FillType constants
    # ------------------------------------------------------------------

    def test_fill_type_constants_exist(self):
        ft = self.fc.FillType
        self.assertEqual(ft.RETAIL, "retail")
        self.assertEqual(ft.ARB_SWEEP_BUY, "arb_sweep_buy")
        self.assertEqual(ft.ARB_SWEEP_SELL, "arb_sweep_sell")
        self.assertEqual(ft.DEXIE_COMBINED, "dexie_combined")
        self.assertEqual(ft.UNKNOWN, "unknown")

    # ------------------------------------------------------------------
    # No dexie_detail → UNKNOWN; dexie_detail + no arb signals → RETAIL
    # ------------------------------------------------------------------

    def test_no_dexie_detail_returns_unknown(self):
        result = self.fc.classify_fill(
            trade_id="0xabc",
            fill_detail={"side": "buy"},
            dexie_detail=None,
        )
        self.assertEqual(result.classification, "unknown")
        self.assertEqual(result.confidence, "low")
        self.assertIsNone(result.taker_puzzle_hash)
        self.assertIsNone(result.spent_block_index)

    def test_dexie_detail_no_arb_signals_returns_retail(self):
        """Fill with Dexie data but no known arb wallet and no combined flag → RETAIL."""
        result = self.fc.classify_fill(
            trade_id="0xretail1",
            fill_detail={"side": "buy"},
            dexie_detail={
                "spent_block_index": 123456,
                "output_coins": {
                    "xch": [{"puzzle_hash": "somerandompuzzle", "amount": 100}]
                },
            },
        )
        self.assertEqual(result.classification, "retail")
        self.assertEqual(result.confidence, "medium")

    def test_empty_dexie_detail_dict_returns_unknown(self):
        """An empty Dexie response dict has no useful data → UNKNOWN, not RETAIL.
        (Empty dict is falsy, same as None for classification purposes.)"""
        result = self.fc.classify_fill(
            trade_id="0xretail2",
            fill_detail={"side": "sell"},
            dexie_detail={},  # present but empty — no usable data
        )
        self.assertEqual(result.classification, "unknown")

    # ------------------------------------------------------------------
    # spent_block_index extraction
    # ------------------------------------------------------------------

    def test_spent_block_index_extracted_from_dexie_detail(self):
        result = self.fc.classify_fill(
            trade_id="0xabc",
            fill_detail={"side": "buy"},
            dexie_detail={"spent_block_index": 42000},
        )
        self.assertEqual(result.spent_block_index, 42000)

    def test_spent_block_index_string_coerced_to_int(self):
        result = self.fc.classify_fill(
            trade_id="0xabc",
            fill_detail={"side": "sell"},
            dexie_detail={"spent_block_index": "55000"},
        )
        self.assertEqual(result.spent_block_index, 55000)

    def test_invalid_spent_block_index_ignored(self):
        result = self.fc.classify_fill(
            trade_id="0xabc",
            fill_detail={"side": "buy"},
            dexie_detail={"spent_block_index": "bad"},
        )
        self.assertIsNone(result.spent_block_index)

    # ------------------------------------------------------------------
    # Known ARB puzzle hash detection
    # ------------------------------------------------------------------

    def test_known_arb_hash_on_sell_offer_classified_arb_sweep_buy(self):
        # We posted a SELL offer; taker bought from us → ARB_SWEEP_BUY
        arb_hash = "deadbeef000000"
        dexie_detail = {
            "spent_block_index": 10000,
            "output_coins": {
                "aabbccdd": [{"puzzle_hash": arb_hash, "amount": 1000}],
            },
        }
        result = self.fc.classify_fill(
            trade_id="0xtrade1",
            fill_detail={"side": "sell"},
            dexie_detail=dexie_detail,
        )
        self.assertEqual(result.classification, "arb_sweep_buy")
        self.assertEqual(result.confidence, "high")
        self.assertIn("deadbeef000000", result.taker_puzzle_hash)

    def test_known_arb_hash_on_buy_offer_classified_arb_sweep_sell(self):
        arb_hash = "cafebabe111111"
        dexie_detail = {
            "spent_block_index": 20000,
            "output_coins": {
                "xch": [{"puzzle_hash": arb_hash, "amount": 500000}],
            },
        }
        result = self.fc.classify_fill(
            trade_id="0xtrade2",
            fill_detail={"side": "buy"},
            dexie_detail=dexie_detail,
        )
        self.assertEqual(result.classification, "arb_sweep_sell")
        self.assertEqual(result.confidence, "high")

    def test_unknown_puzzle_hash_not_classified_as_arb(self):
        dexie_detail = {
            "output_coins": {
                "aabbccdd": [{"puzzle_hash": "0000111122223333", "amount": 100}],
            },
        }
        result = self.fc.classify_fill(
            trade_id="0xtrade3",
            fill_detail={"side": "sell"},
            dexie_detail=dexie_detail,
        )
        self.assertNotIn(result.classification, ("arb_sweep_buy", "arb_sweep_sell"))

    def test_arb_hash_with_0x_prefix_still_matches(self):
        """taker_puzzle_hash returned with 0x prefix should still match."""
        arb_hash = "0xdeadbeef000000"
        dexie_detail = {
            "output_coins": {
                "aabbccdd": [{"puzzle_hash": arb_hash, "amount": 100}],
            },
        }
        result = self.fc.classify_fill(
            trade_id="0xtrade4",
            fill_detail={"side": "sell"},
            dexie_detail=dexie_detail,
        )
        self.assertEqual(result.classification, "arb_sweep_buy")

    # ------------------------------------------------------------------
    # Dexie combined flag
    # ------------------------------------------------------------------

    def test_dexie_combined_flag_classifies_as_dexie_combined(self):
        result = self.fc.classify_fill(
            trade_id="0xtrade5",
            fill_detail={"side": "buy"},
            dexie_detail={"combined": True},
        )
        self.assertEqual(result.classification, "dexie_combined")
        self.assertEqual(result.confidence, "high")

    def test_is_combined_flag_classifies_as_dexie_combined(self):
        result = self.fc.classify_fill(
            trade_id="0xtrade6",
            fill_detail={"side": "sell"},
            dexie_detail={"is_combined": True},
        )
        self.assertEqual(result.classification, "dexie_combined")

    def test_multiple_matched_offers_classifies_as_dexie_combined(self):
        result = self.fc.classify_fill(
            trade_id="0xtrade7",
            fill_detail={"side": "buy"},
            dexie_detail={"matched_offers": ["a", "b", "c"]},
        )
        self.assertEqual(result.classification, "dexie_combined")

    def test_single_matched_offer_does_not_classify_as_combined(self):
        result = self.fc.classify_fill(
            trade_id="0xtrade8",
            fill_detail={"side": "buy"},
            dexie_detail={"matched_offers": ["only-one"]},
        )
        self.assertNotEqual(result.classification, "dexie_combined")

    # ------------------------------------------------------------------
    # FillClassification.is_arb()
    # ------------------------------------------------------------------

    def test_is_arb_returns_true_for_arb_types(self):
        for cls_name in ("arb_sweep_buy", "arb_sweep_sell", "dexie_combined"):
            fc = self.fc.FillClassification(trade_id="x", classification=cls_name)
            self.assertTrue(fc.is_arb(), msg=f"is_arb() should be True for {cls_name}")

    def test_is_arb_returns_false_for_retail_and_unknown(self):
        for cls_name in ("retail", "unknown"):
            fc = self.fc.FillClassification(trade_id="x", classification=cls_name)
            self.assertFalse(
                fc.is_arb(), msg=f"is_arb() should be False for {cls_name}"
            )

    # ------------------------------------------------------------------
    # _extract_taker_puzzle_hash
    # ------------------------------------------------------------------

    def test_extract_taker_hash_sell_side_uses_cat_key(self):
        detail = {
            "output_coins": {
                "aabbccdd": [{"puzzle_hash": "takercat111", "amount": 500}],
                "xch": [{"puzzle_hash": "selfxch000", "amount": 1000}],
            }
        }
        ph = self.fc._extract_taker_puzzle_hash(detail, "sell")
        self.assertEqual(ph, "takercat111")

    def test_extract_taker_hash_buy_side_uses_xch_key(self):
        detail = {
            "output_coins": {
                "xch": [{"puzzle_hash": "takerxch222", "amount": 2000}],
                "aabbccdd": [{"puzzle_hash": "selfcat000", "amount": 100}],
            }
        }
        ph = self.fc._extract_taker_puzzle_hash(detail, "buy")
        self.assertEqual(ph, "takerxch222")

    def test_extract_taker_hash_strips_0x(self):
        detail = {
            "output_coins": {
                "xch": [{"puzzle_hash": "0xABCDEF", "amount": 100}],
            }
        }
        ph = self.fc._extract_taker_puzzle_hash(detail, "buy")
        self.assertEqual(ph, "abcdef")

    def test_extract_taker_hash_empty_coins_returns_none(self):
        detail = {"output_coins": {"xch": []}}
        ph = self.fc._extract_taker_puzzle_hash(detail, "buy")
        self.assertIsNone(ph)

    def test_extract_taker_hash_no_output_coins_returns_none(self):
        ph = self.fc._extract_taker_puzzle_hash({}, "sell")
        self.assertIsNone(ph)

    def test_extract_taker_hash_fallback_to_first_non_empty_key(self):
        """When no asset-specific key matches, falls back to first non-empty."""
        detail = {
            "output_coins": {
                "some_other_token": [{"puzzle_hash": "fallbackhash", "amount": 50}],
            }
        }
        ph = self.fc._extract_taker_puzzle_hash(detail, "buy")
        self.assertEqual(ph, "fallbackhash")


class FillClassifierNoKnownHashesTests(unittest.TestCase):
    """Tests with KNOWN_ARB_PUZZLE_HASHES empty — arb detection falls through."""

    def setUp(self):
        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            CAT_ASSET_ID="aabbccdd",
            WALLET_ID_XCH=1,
            KNOWN_ARB_PUZZLE_HASHES=[],
        )
        sys.modules["config"] = fake_config
        sys.modules.pop("fill_classifier", None)
        import fill_classifier

        self.fc = fill_classifier

    def tearDown(self):
        sys.modules.pop("fill_classifier", None)
        sys.modules.pop("config", None)

    def test_no_known_hashes_with_dexie_detail_returns_retail(self):
        """With Dexie detail present but no arb signals → RETAIL (not UNKNOWN)."""
        result = self.fc.classify_fill(
            trade_id="0xtest",
            fill_detail={"side": "sell"},
            dexie_detail={
                "spent_block_index": 999,
                "output_coins": {"aabbccdd": [{"puzzle_hash": "abc", "amount": 1}]},
            },
        )
        self.assertEqual(result.classification, "retail")
        self.assertEqual(result.confidence, "medium")

    def test_no_dexie_detail_returns_unknown(self):
        """Without Dexie detail we genuinely can't classify → UNKNOWN."""
        result = self.fc.classify_fill(
            trade_id="0xtest2",
            fill_detail={"side": "sell"},
            dexie_detail=None,
        )
        self.assertEqual(result.classification, "unknown")


class FillClassificationSideFieldTests(unittest.TestCase):
    """Verify the new side field on FillClassification (Fix #2)."""

    def setUp(self):
        _install_fakes()
        sys.modules.pop("fill_classifier", None)
        import fill_classifier

        self.fc = fill_classifier

    def tearDown(self):
        for name in ["fill_classifier", "database", "config"]:
            sys.modules.pop(name, None)

    def test_side_field_defaults_to_none(self):
        fc = self.fc.FillClassification(trade_id="x")
        self.assertIsNone(fc.side)

    def test_side_field_can_be_set(self):
        fc = self.fc.FillClassification(trade_id="x", side="buy")
        self.assertEqual(fc.side, "buy")

    def test_side_can_be_stamped_after_construction(self):
        """fill_tracker stamps side onto classification after calling classify_fill."""
        result = self.fc.classify_fill(
            trade_id="0xt",
            fill_detail={"side": "sell"},
            dexie_detail=None,
        )
        # classify_fill doesn't set side — caller stamps it
        self.assertIsNone(result.side)
        result.side = "sell"
        self.assertEqual(result.side, "sell")

    def test_is_arb_unaffected_by_side_field(self):
        for cls_name in ("arb_sweep_buy", "arb_sweep_sell", "dexie_combined"):
            fc = self.fc.FillClassification(
                trade_id="x", classification=cls_name, side="buy"
            )
            self.assertTrue(fc.is_arb())
        for cls_name in ("retail", "unknown"):
            fc = self.fc.FillClassification(
                trade_id="x", classification=cls_name, side="sell"
            )
            self.assertFalse(fc.is_arb())


if __name__ == "__main__":
    unittest.main()
