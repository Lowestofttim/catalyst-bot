import unittest

from splash_receive import classify_offer_for_asset, normalize_offer_summary


class SplashReceiveTests(unittest.TestCase):
    def test_normalize_maker_taker_summary(self):
        summary = normalize_offer_summary(
            {
                "summary": {
                    "maker": [{"asset": {"asset_id": None}, "amount": 1000}],
                    "taker": [{"asset": {"asset_id": "abc123"}, "amount": 5000}],
                }
            }
        )
        self.assertEqual(summary["offered"], {"xch": 1000})
        self.assertEqual(summary["requested"], {"abc123": 5000})

    def test_classify_relevant_buy_offer(self):
        result = classify_offer_for_asset(
            {
                "summary": {
                    "offered": {"xch": 1000},
                    "requested": {"abc123": 5000},
                }
            },
            "abc123",
        )
        self.assertTrue(result["relevant"])
        self.assertEqual(result["side"], "buy")
        self.assertEqual(result["pair_hint"], "abc123")

    def test_classify_relevant_sell_offer(self):
        result = classify_offer_for_asset(
            {
                "summary": {
                    "offered": {"ABC123": 5000},
                    "requested": {"xch": 1000},
                }
            },
            "abc123",
        )
        self.assertTrue(result["relevant"])
        self.assertEqual(result["side"], "sell")
        self.assertEqual(result["pair_hint"], "abc123")

    def test_ignore_other_pair(self):
        result = classify_offer_for_asset(
            {
                "summary": {
                    "offered": {"xch": 1000},
                    "requested": {"othercat": 5000},
                }
            },
            "abc123",
        )
        self.assertFalse(result["relevant"])
        self.assertEqual(result["side"], "buy")
        self.assertEqual(result["pair_hint"], "othercat")

    def test_ignore_complex_offer(self):
        result = classify_offer_for_asset(
            {
                "summary": {
                    "offered": {"xch": 1000, "abc123": 5},
                    "requested": {"abc123": 5000},
                }
            },
            "abc123",
        )
        self.assertFalse(result["relevant"])
        self.assertEqual(result["side"], "")
        self.assertEqual(result["pair_hint"], "")


if __name__ == "__main__":
    unittest.main()
