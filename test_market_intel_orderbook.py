import importlib
import sys
import types
import unittest
from decimal import Decimal


class MarketIntelOrderbookTests(unittest.TestCase):
    def setUp(self):
        fake_config = types.ModuleType("config")
        fake_config.cfg = types.SimpleNamespace(
            CAT_ASSET_ID="test-cat",
            DBX_MAX_SPREAD_BPS=Decimal("500"),
        )
        fake_database = types.ModuleType("database")
        fake_database.log_event = lambda *args, **kwargs: None
        fake_database.get_trade_dexie_map = lambda *args, **kwargs: {}
        fake_requests = types.ModuleType("requests")

        class _FakeSession:
            def __init__(self):
                self.headers = {}

        fake_requests.Session = _FakeSession

        sys.modules["config"] = fake_config
        sys.modules["database"] = fake_database
        sys.modules["requests"] = fake_requests
        sys.modules.pop("market_intel", None)
        self.market_intel = importlib.import_module("market_intel")
        self.intel = self.market_intel.MarketIntel()

    def tearDown(self):
        sys.modules.pop("market_intel", None)
        sys.modules.pop("config", None)
        sys.modules.pop("database", None)
        sys.modules.pop("requests", None)

    def test_inverted_competitor_book_is_ignored(self):
        buy_offers = [
            {"price": Decimal("0.00012"), "xch_amount": Decimal("1.0"), "side": "buy", "is_ours": False},
        ]
        sell_offers = [
            {"price": Decimal("0.00011"), "xch_amount": Decimal("1.0"), "side": "sell", "is_ours": False},
        ]

        self.intel._analyse_orderbook(buy_offers, sell_offers)
        summary = self.intel.get_market_summary()

        self.assertEqual(summary["best_bid"], "0")
        self.assertEqual(summary["best_ask"], "0")
        self.assertEqual(summary["competitor_spread_bps"], "0")
        self.assertEqual(summary["overall_spread_bps"], "0")

    def test_parse_dexie_offer_marks_known_dexie_ids_as_ours(self):
        self.intel._known_dexie_ids = {"dexie-123"}

        parsed = self.intel._parse_dexie_offer(
            {
                "id": "dexie-123",
                "offered": [{"id": "", "code": "XCH", "amount": "1.2"}],
                "requested": [{"id": "test-cat", "code": "TEST", "amount": "1000"}],
                "tags": [],
            },
            "buy",
        )

        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["is_ours"])


if __name__ == "__main__":
    unittest.main()
