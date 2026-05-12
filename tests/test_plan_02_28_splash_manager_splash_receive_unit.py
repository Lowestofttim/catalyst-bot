"""Slice 02-28 — splash_manager.py + splash_receive.py unit tests.

splash_manager:  SplashManager._fingerprint (static method, pure SHA256).
splash_receive:  _asset_key, _normalize_side, _from_maker_taker,
                 normalize_offer_summary, classify_offer_for_asset.
All functions are stateless transformations — no network, DB, or file I/O.
"""

import unittest

try:
    from splash_manager import SplashManager
    _SKIP_SM = None
except ModuleNotFoundError as exc:
    _SKIP_SM = str(exc)

try:
    from splash_receive import (
        _asset_key, _normalize_side, _from_maker_taker,
        normalize_offer_summary, classify_offer_for_asset,
    )
    _SKIP_SR = None
except ModuleNotFoundError as exc:
    _SKIP_SR = str(exc)

_ASSET = "b8edcc6a7cf3738a3806fdbadb1bbcfc2540ec37f6732ab3a6a4bbcd2dbec105"


# ---------------------------------------------------------------------------
# SplashManager._fingerprint
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SM is not None, f"splash_manager unavailable: {_SKIP_SM}")
class TestSplashFingerprint(unittest.TestCase):
    def test_returns_64_char_hex(self):
        result = SplashManager._fingerprint("offer1xyz")
        self.assertEqual(len(result), 64)
        int(result, 16)  # must be valid hex

    def test_deterministic(self):
        r1 = SplashManager._fingerprint("offer1abc")
        r2 = SplashManager._fingerprint("offer1abc")
        self.assertEqual(r1, r2)

    def test_different_inputs_give_different_fingerprints(self):
        r1 = SplashManager._fingerprint("offer1aaa")
        r2 = SplashManager._fingerprint("offer1bbb")
        self.assertNotEqual(r1, r2)

    def test_strips_whitespace(self):
        r1 = SplashManager._fingerprint("offer1xyz")
        r2 = SplashManager._fingerprint("  offer1xyz  ")
        self.assertEqual(r1, r2)


@unittest.skipIf(_SKIP_SM is not None, f"splash_manager unavailable: {_SKIP_SM}")
class TestSplashQueuePurge(unittest.TestCase):
    def test_purge_trade_ids_removes_only_matching_queued_offers(self):
        manager = SplashManager()
        manager.queue_post("offer1aaa", trade_id="keep")
        manager.queue_post("offer1bbb", trade_id="drop")
        manager.queue_post("offer1ccc", trade_id="keep-too")

        manager.purge_trade_ids(["drop", "missing"])

        remaining = [item["trade_id"] for item in manager._queue]
        self.assertEqual(remaining, ["keep", "keep-too"])


# ---------------------------------------------------------------------------
# _asset_key
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SR is not None, f"splash_receive unavailable: {_SKIP_SR}")
class TestAssetKey(unittest.TestCase):
    def test_none_returns_xch(self):
        self.assertEqual(_asset_key(None), "xch")

    def test_empty_string_returns_xch(self):
        self.assertEqual(_asset_key(""), "xch")

    def test_xch_string_returns_xch(self):
        self.assertEqual(_asset_key("xch"), "xch")
        self.assertEqual(_asset_key("XCH"), "xch")

    def test_asset_id_lowercased(self):
        result = _asset_key("ABCDEF")
        self.assertEqual(result, "abcdef")

    def test_whitespace_stripped(self):
        self.assertEqual(_asset_key("  xch  "), "xch")

    def test_real_asset_id(self):
        result = _asset_key(_ASSET)
        self.assertEqual(result, _ASSET)


# ---------------------------------------------------------------------------
# _normalize_side
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SR is not None, f"splash_receive unavailable: {_SKIP_SR}")
class TestNormalizeSide(unittest.TestCase):
    def test_non_dict_returns_empty(self):
        self.assertEqual(_normalize_side("bad"), {})
        self.assertEqual(_normalize_side(None), {})

    def test_normalizes_keys(self):
        result = _normalize_side({"XCH": 100, _ASSET: 50})
        self.assertIn("xch", result)
        self.assertIn(_ASSET, result)

    def test_none_key_becomes_xch(self):
        result = _normalize_side({None: 100})
        self.assertIn("xch", result)

    def test_preserves_values(self):
        result = _normalize_side({"xch": 999})
        self.assertEqual(result["xch"], 999)


# ---------------------------------------------------------------------------
# _from_maker_taker
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SR is not None, f"splash_receive unavailable: {_SKIP_SR}")
class TestFromMakerTaker(unittest.TestCase):
    def test_empty_list_returns_empty(self):
        self.assertEqual(_from_maker_taker([]), {})

    def test_xch_item(self):
        items = [{"asset": None, "amount": 1000}]
        result = _from_maker_taker(items)
        self.assertIn("xch", result)
        self.assertEqual(result["xch"], 1000)

    def test_cat_item(self):
        items = [{"asset": {"asset_id": _ASSET}, "amount": 500}]
        result = _from_maker_taker(items)
        self.assertIn(_ASSET, result)

    def test_non_dict_items_skipped(self):
        self.assertEqual(_from_maker_taker(["bad", None]), {})

    def test_multiple_items(self):
        items = [
            {"asset": None, "amount": 1000},
            {"asset": {"asset_id": _ASSET}, "amount": 500},
        ]
        result = _from_maker_taker(items)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# normalize_offer_summary
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SR is not None, f"splash_receive unavailable: {_SKIP_SR}")
class TestNormalizeOfferSummary(unittest.TestCase):
    def test_none_returns_empty_dicts(self):
        result = normalize_offer_summary(None)
        self.assertEqual(result, {"offered": {}, "requested": {}})

    def test_offered_requested_style(self):
        view = {"summary": {"offered": {"xch": 1000}, "requested": {_ASSET: 500}}}
        result = normalize_offer_summary(view)
        self.assertIn("xch", result["offered"])
        self.assertIn(_ASSET, result["requested"])

    def test_maker_taker_style(self):
        view = {
            "summary": {
                "maker": [{"asset": None, "amount": 1000}],
                "taker": [{"asset": {"asset_id": _ASSET}, "amount": 500}],
            }
        }
        result = normalize_offer_summary(view)
        self.assertIn("xch", result["offered"])
        self.assertIn(_ASSET, result["requested"])

    def test_direct_offered_requested_in_view(self):
        view = {"offered": {"xch": 100}, "requested": {_ASSET: 50}}
        result = normalize_offer_summary(view)
        self.assertIn("xch", result["offered"])

    def test_nested_offer_object(self):
        view = {
            "offer": {
                "summary": {"offered": {"xch": 200}, "requested": {_ASSET: 100}}
            }
        }
        result = normalize_offer_summary(view)
        self.assertIn("xch", result["offered"])

    def test_result_always_has_offered_and_requested_keys(self):
        for val in (None, {}, "bad", 42):
            with self.subTest(val=val):
                result = normalize_offer_summary(val)
                self.assertIn("offered", result)
                self.assertIn("requested", result)


# ---------------------------------------------------------------------------
# classify_offer_for_asset
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SR is not None, f"splash_receive unavailable: {_SKIP_SR}")
class TestClassifyOfferForAsset(unittest.TestCase):
    def _buy_offer(self):
        return {"summary": {"offered": {"xch": 1000}, "requested": {_ASSET: 500}}}

    def _sell_offer(self):
        return {"summary": {"offered": {_ASSET: 500}, "requested": {"xch": 1000}}}

    def _wrong_asset_offer(self):
        return {"summary": {"offered": {"xch": 1000}, "requested": {"deadbeef": 500}}}

    def test_buy_offer_classified(self):
        result = classify_offer_for_asset(self._buy_offer(), _ASSET)
        self.assertTrue(result["relevant"])
        self.assertEqual(result["side"], "buy")

    def test_sell_offer_classified(self):
        result = classify_offer_for_asset(self._sell_offer(), _ASSET)
        self.assertTrue(result["relevant"])
        self.assertEqual(result["side"], "sell")

    def test_wrong_asset_not_relevant(self):
        result = classify_offer_for_asset(self._wrong_asset_offer(), _ASSET)
        self.assertFalse(result["relevant"])

    def test_result_has_required_keys(self):
        result = classify_offer_for_asset(self._buy_offer(), _ASSET)
        for key in ("relevant", "pair_hint", "side", "summary"):
            self.assertIn(key, result)

    def test_none_input_not_relevant(self):
        result = classify_offer_for_asset(None, _ASSET)
        self.assertFalse(result["relevant"])

    def test_pair_hint_matches_asset(self):
        result = classify_offer_for_asset(self._buy_offer(), _ASSET)
        self.assertEqual(result["pair_hint"], _ASSET)


if __name__ == "__main__":
    unittest.main()
