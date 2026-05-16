"""Slice 02-27 — boost_manager.py unit tests.

Covers: _bps_to_pct (pure), BoostManager._find_stale_offers
(tested with a minimal fake offer_manager providing a price cache).
No offer creation, network calls, or database access.
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

try:
    import boost_manager as _bm_mod
    from boost_manager import _bps_to_pct, BoostManager

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_SKIP_MSG = f"boost_manager unavailable: {_SKIP}"


class _FakeOfferManager:
    """Minimal fake with a price cache so _find_stale_offers can find prices."""

    def __init__(self, prices=None):
        self._offer_details_cache = {
            tid: {"price": price} for tid, price in (prices or {}).items()
        }
        self._cycle_used_coin_ids = set()


# ---------------------------------------------------------------------------
# _bps_to_pct
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestBoostBpsToPct(unittest.TestCase):
    def test_30_bps(self):
        self.assertEqual(_bps_to_pct(30), "0.30%")

    def test_100_bps(self):
        self.assertEqual(_bps_to_pct(100), "1.0%")

    def test_invalid_input(self):
        result = _bps_to_pct("not_a_number")
        self.assertIsInstance(result, str)


# ---------------------------------------------------------------------------
# BoostManager._find_stale_offers
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestFindStaleOffers(unittest.TestCase):
    """_find_stale_offers uses offer_manager._offer_details_cache for prices."""

    def _make_manager(self, prices=None):
        return BoostManager(offer_manager=_FakeOfferManager(prices))

    def test_empty_offers_returns_empty(self):
        mgr = self._make_manager()
        result = mgr._find_stale_offers([], Decimal("0.001"), "buy", Decimal("0.05"))
        self.assertEqual(result, [])

    def test_zero_mid_price_returns_empty(self):
        prices = {"tid1": Decimal("0.001")}
        mgr = self._make_manager(prices)
        offers = [{"trade_id": "tid1"}]
        result = mgr._find_stale_offers(offers, Decimal("0"), "buy", Decimal("0.05"))
        self.assertEqual(result, [])

    def test_no_offer_manager_returns_empty(self):
        mgr = BoostManager(offer_manager=None)
        offers = [{"trade_id": "tid1"}]
        result = mgr._find_stale_offers(
            offers, Decimal("0.001"), "buy", Decimal("0.05")
        )
        self.assertEqual(result, [])

    def test_stale_offer_identified(self):
        # mid=0.001, spread=0.05 → target_bps=500
        # offer at 0.002 → distance = 0.001/0.001 * 10000 = 10000 bps > 500 → stale
        prices = {"tid1": Decimal("0.002")}
        mgr = self._make_manager(prices)
        offers = [{"trade_id": "tid1"}]
        result = mgr._find_stale_offers(
            offers, Decimal("0.001"), "buy", Decimal("0.05")
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["trade_id"], "tid1")

    def test_fresh_offer_not_stale(self):
        # offer at 0.00103 → distance = 0.00003/0.001 * 10000 = 300 bps < 500 → not stale
        prices = {"tid1": Decimal("0.00103")}
        mgr = self._make_manager(prices)
        offers = [{"trade_id": "tid1"}]
        result = mgr._find_stale_offers(
            offers, Decimal("0.001"), "buy", Decimal("0.05")
        )
        self.assertEqual(result, [])

    def test_sorted_most_stale_first(self):
        # tid1: 0.0015 → 5000 bps from 0.001, tid2: 0.002 → 10000 bps → tid2 first
        prices = {"tid1": Decimal("0.0015"), "tid2": Decimal("0.002")}
        mgr = self._make_manager(prices)
        offers = [{"trade_id": "tid1"}, {"trade_id": "tid2"}]
        result = mgr._find_stale_offers(
            offers, Decimal("0.001"), "buy", Decimal("0.05")
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["trade_id"], "tid2")  # most stale first

    def test_offers_missing_trade_id_skipped(self):
        mgr = self._make_manager({"": Decimal("0.002")})
        offers = [{"no_trade_id": True}]
        result = mgr._find_stale_offers(
            offers, Decimal("0.001"), "buy", Decimal("0.05")
        )
        self.assertEqual(result, [])

    def test_distance_bps_appended_to_result(self):
        prices = {"tid1": Decimal("0.002")}
        mgr = self._make_manager(prices)
        offers = [{"trade_id": "tid1"}]
        result = mgr._find_stale_offers(
            offers, Decimal("0.001"), "buy", Decimal("0.05")
        )
        self.assertIn("_distance_bps", result[0])
        self.assertGreater(result[0]["_distance_bps"], 0)


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestFlexibleProbeSize(unittest.TestCase):
    def test_sell_probe_retries_with_smaller_sniper_coin(self):
        class FlexibleOfferManager:
            def __init__(self):
                self.calls = []
                self._cycle_used_coin_ids = set()
                self._offer_details_cache = {}

            def create_offer_with_retry(self, offer_dict, **kwargs):
                self.calls.append((offer_dict, kwargs))
                if kwargs.get("selected_coin_id") == "cat-sniper-79000":
                    return {
                        "success": True,
                        "trade_id": "tid-flex",
                        "offer": "offer-flex",
                        "locked_coin_id": "cat-sniper-79000",
                    }
                return {
                    "success": False,
                    "error": "no_preferred_tier_coin",
                    "preferred_tier": "sniper",
                }

        fake_cfg = SimpleNamespace(
            CAT_DECIMALS=3,
            CAT_ASSET_ID="asset",
            CAT_WALLET_ID=2,
            COIN_IDS_ENABLED=True,
            DEXIE_AUTO_POST=False,
            DRY_RUN=False,
            SNIPER_EXPIRY_SECS=600,
            WALLET_ID_XCH=1,
        )
        offer_manager = FlexibleOfferManager()
        mgr = BoostManager(offer_manager=offer_manager)

        with (
            patch.object(_bm_mod, "cfg", fake_cfg),
            patch.object(_bm_mod, "add_offer", return_value=True),
            patch.object(_bm_mod, "lock_coin"),
            patch.object(
                mgr,
                "_find_flexible_sniper_coin",
                return_value={"coin_id": "cat-sniper-79000", "amount_mojos": 79000},
                create=True,
            ),
        ):
            result = mgr._create_single_offer(
                "sell", Decimal("0.0001175"), Decimal("0.01")
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["trade_id"], "tid-flex")
        self.assertEqual(result["size_cat"], Decimal("79"))
        self.assertEqual(result["size_xch"], Decimal("0.0092825"))
        self.assertEqual(len(offer_manager.calls), 2)

        retry_offer, retry_kwargs = offer_manager.calls[1]
        self.assertEqual(retry_offer[str(fake_cfg.CAT_WALLET_ID)], -79000)
        self.assertEqual(retry_kwargs["selected_coin_id"], "cat-sniper-79000")


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestInvertedCascadeBroadcast(unittest.TestCase):
    def test_inverted_cascade_queues_new_offers_to_dexie_and_splash(self):
        class QueuePoster:
            def __init__(self):
                self.queued = []

            def queue_post(self, bech32, trade_id):
                self.queued.append((bech32, trade_id))

        class CascadeOfferManager:
            def __init__(self):
                self._bot_cancelled_ids = set()
                self.cancelled = []

            def create_ladder(self, _mid_price, side, **_kwargs):
                return [
                    {
                        "offer_bech32": f"offer1-{side}-1",
                        "trade_id": f"{side}-new-1",
                    },
                    {
                        "offer_bech32": f"offer1-{side}-2",
                        "trade_id": f"{side}-new-2",
                    },
                ]

            def cancel_offers(self, trade_ids, **_kwargs):
                self.cancelled.extend(trade_ids)
                return {tid: {"success": True} for tid in trade_ids}

        fake_cfg = SimpleNamespace(
            CAT_ASSET_ID="asset",
            COIN_IDS_ENABLED=True,
            DEXIE_AUTO_POST=True,
            ENABLE_BUY=True,
            ENABLE_SELL=True,
            GAP_PROBE_CASCADE_COUNT_PER_SIDE=2,
            GAP_PROBE_CASCADE_HALF_SPREAD_BPS=50,
            SPLASH_ENABLED=True,
        )
        old_offers = {
            "buy": [
                {"trade_id": "buy-old-1", "tier": "inner", "price": "0.00009"},
                {"trade_id": "buy-old-2", "tier": "inner", "price": "0.00008"},
            ],
            "sell": [
                {"trade_id": "sell-old-1", "tier": "inner", "price": "0.00012"},
                {"trade_id": "sell-old-2", "tier": "inner", "price": "0.00013"},
            ],
        }

        def get_open_offers(side=None, **_kwargs):
            return old_offers.get(side, [])

        offer_manager = CascadeOfferManager()
        dexie = QueuePoster()
        splash = QueuePoster()
        mgr = BoostManager(
            offer_manager=offer_manager,
            dexie_manager=dexie,
            risk_manager=object(),
            splash_manager=splash,
        )
        mgr._boost_mid_price = Decimal("0.00010")

        with (
            patch.object(_bm_mod, "cfg", fake_cfg),
            patch("database.get_open_offers", side_effect=get_open_offers),
            patch.object(_bm_mod, "log_event"),
        ):
            mgr._cascade_after_inverted_floor()

        expected = [
            ("offer1-buy-1", "buy-new-1"),
            ("offer1-buy-2", "buy-new-2"),
            ("offer1-sell-1", "sell-new-1"),
            ("offer1-sell-2", "sell-new-2"),
        ]
        self.assertEqual(dexie.queued, expected)
        self.assertEqual(splash.queued, expected)


if __name__ == "__main__":
    unittest.main()
