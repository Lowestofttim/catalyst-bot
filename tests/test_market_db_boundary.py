import unittest
from unittest.mock import patch

import api_server
from blueprints import market


class MarketDatabaseBoundaryTests(unittest.TestCase):
    def test_sage_single_offer_test_uses_database_helper_for_spare_coins(self):
        helper_results = {
            "xch": {"coin_id": "xch-coin", "amount_mojos": 1_000_000_000, "assigned_tier": "small"},
            "cat": {"coin_id": "cat-coin", "amount_mojos": 8_000, "assigned_tier": "small"},
        }

        def fake_spare(wallet_type):
            return dict(helper_results[wallet_type])

        def fake_create_offer(*args, **kwargs):
            return {"trade_id": f"trade-{kwargs['coin_ids'][0]}"}

        with (
            api_server.app.test_request_context(
                "/api/debug/sage-single-offer-test",
                method="POST",
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            ),
            patch("wallet.get_wallet_type", return_value="sage"),
            patch("wallet.create_offer", side_effect=fake_create_offer),
            patch("wallet.cancel_offer", return_value={"success": True}),
            patch("wallet.get_owned_coins_detailed", return_value={}),
            patch("blueprints.market.time.sleep", return_value=None),
            patch("sqlite3.connect", side_effect=AssertionError("raw sqlite connect used")),
            patch("database.get_smallest_free_tier_spare", side_effect=fake_spare, create=True) as spare,
        ):
            response = market.api_debug_sage_single_offer_test()

        if isinstance(response, tuple):
            response = response[0]
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data.get("ok"), data)
        self.assertEqual([call.args[0] for call in spare.call_args_list], ["xch", "cat"])
        self.assertEqual(data["xch_coin"]["coin_id"], "xch-coin")
        self.assertEqual(data["cat_coin"]["coin_id"], "cat-coin")


if __name__ == "__main__":
    unittest.main()
