"""Slice 03-09 — fill detection + PnL round-trip match (integration test).

Tests the full flow: offers created in DB → fills recorded → unmatched fills
queried → buy+sell matched into round-trip → PnL stored and net position updated.

Uses a real SQLite temp DB (identical to what the bot uses at runtime).
No Flask server or wallet calls needed — all logic is in the database layer.
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db

    _SKIP = None
except ModuleNotFoundError as exc:
    _db = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Temp-DB base class (same pattern as test_plan_02_30)
# ---------------------------------------------------------------------------


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path

        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.init_database()

    def tearDown(self):
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass

    def _add_offer(
        self,
        trade_id,
        side,
        price,
        size_xch=Decimal("0.1"),
        size_cat=Decimal("100"),
        asset="testcat",
    ):
        return _db.add_offer(trade_id, side, price, size_xch, size_cat, asset)

    def _record_fill(
        self,
        trade_id,
        side,
        price,
        size_xch=Decimal("0.1"),
        size_cat=Decimal("100"),
        asset="testcat",
    ):
        return _db.record_fill(trade_id, side, price, size_xch, size_cat, asset)


# ---------------------------------------------------------------------------
# 1. record_fill stores fills correctly
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestRecordFill(_TempDB):
    def test_fill_stored_and_retrievable(self):
        self._add_offer("tid-buy-1", "buy", Decimal("0.001"))
        fid = self._record_fill("tid-buy-1", "buy", Decimal("0.001"))
        self.assertGreater(fid, 0)
        fills = _db.get_fills(cat_asset_id="testcat")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["trade_id"], "tid-buy-1")
        self.assertEqual(fills[0]["side"], "buy")

    def test_fill_idempotent(self):
        self._add_offer("tid-buy-2", "buy", Decimal("0.001"))
        fid1 = self._record_fill("tid-buy-2", "buy", Decimal("0.001"))
        fid2 = self._record_fill("tid-buy-2", "buy", Decimal("0.001"))
        self.assertEqual(fid1, fid2)
        fills = _db.get_fills(cat_asset_id="testcat")
        self.assertEqual(len(fills), 1)

    def test_offer_marked_filled(self):
        self._add_offer("tid-sell-1", "sell", Decimal("0.002"))
        self._record_fill("tid-sell-1", "sell", Decimal("0.002"))
        offer = _db.get_offer("tid-sell-1")
        self.assertEqual(offer["status"], "filled")

    def test_multiple_fills_different_sides(self):
        self._add_offer("t-buy", "buy", Decimal("0.001"))
        self._add_offer("t-sell", "sell", Decimal("0.002"))
        self._record_fill("t-buy", "buy", Decimal("0.001"))
        self._record_fill("t-sell", "sell", Decimal("0.002"))
        buy_fills = _db.get_fills(cat_asset_id="testcat", side="buy")
        sell_fills = _db.get_fills(cat_asset_id="testcat", side="sell")
        self.assertEqual(len(buy_fills), 1)
        self.assertEqual(len(sell_fills), 1)


# ---------------------------------------------------------------------------
# 2. get_unmatched_fills — only returns unmatched verified fills
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestGetUnmatchedFills(_TempDB):
    def test_fresh_fills_are_unmatched(self):
        self._add_offer("b1", "buy", Decimal("0.001"))
        self._add_offer("b2", "buy", Decimal("0.001"))
        self._record_fill("b1", "buy", Decimal("0.001"))
        self._record_fill("b2", "buy", Decimal("0.001"))
        unmatched = _db.get_unmatched_fills("testcat", "buy")
        self.assertEqual(len(unmatched), 2)

    def test_matched_fills_excluded(self):
        self._add_offer("b1", "buy", Decimal("0.001"))
        self._add_offer("s1", "sell", Decimal("0.002"))
        bid = self._record_fill("b1", "buy", Decimal("0.001"))
        sid = self._record_fill("s1", "sell", Decimal("0.002"))
        _db.match_round_trip(bid, sid, Decimal("0.0001"))
        buy_unmatched = _db.get_unmatched_fills("testcat", "buy")
        sell_unmatched = _db.get_unmatched_fills("testcat", "sell")
        self.assertEqual(len(buy_unmatched), 0)
        self.assertEqual(len(sell_unmatched), 0)

    def test_filters_by_asset_id(self):
        self._add_offer("ba", "buy", Decimal("0.001"), asset="assetA")
        _db.record_fill(
            "ba", "buy", Decimal("0.001"), Decimal("0.1"), Decimal("100"), "assetA"
        )
        self._add_offer("bb", "buy", Decimal("0.001"), asset="assetB")
        _db.record_fill(
            "bb", "buy", Decimal("0.001"), Decimal("0.1"), Decimal("100"), "assetB"
        )
        ua = _db.get_unmatched_fills("assetA", "buy")
        ub = _db.get_unmatched_fills("assetB", "buy")
        self.assertEqual(len(ua), 1)
        self.assertEqual(len(ub), 1)
        self.assertEqual(ua[0]["cat_asset_id"], "assetA")


# ---------------------------------------------------------------------------
# 3. match_round_trip — links buy and sell fills, stores PnL
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestMatchRoundTrip(_TempDB):
    def _pair(self, buy_price=Decimal("0.001"), sell_price=Decimal("0.0011")):
        self._add_offer("b", "buy", buy_price)
        self._add_offer("s", "sell", sell_price)
        bid = self._record_fill("b", "buy", buy_price)
        sid = self._record_fill("s", "sell", sell_price)
        return bid, sid

    def test_returns_positive_round_trip_id(self):
        bid, sid = self._pair()
        rt_id = _db.match_round_trip(bid, sid, Decimal("0.0001"))
        self.assertGreater(rt_id, 0)

    def test_pnl_stored_on_both_fills(self):
        bid, sid = self._pair()
        pnl = Decimal("0.000100")
        _db.match_round_trip(bid, sid, pnl)
        fills = _db.get_fills(cat_asset_id="testcat", include_legacy=True)
        pnls = {
            f["fill_id"]: Decimal(str(f["pnl_xch"]))
            for f in fills
            if f.get("pnl_xch") is not None
        }
        self.assertAlmostEqual(float(pnls[bid]), float(pnl))
        self.assertAlmostEqual(float(pnls[sid]), float(pnl))

    def test_round_trip_id_set_on_both_fills(self):
        bid, sid = self._pair()
        rt_id = _db.match_round_trip(bid, sid, Decimal("0.0001"))
        fills = _db.get_fills(cat_asset_id="testcat", include_legacy=True)
        rt_ids = {f["fill_id"]: f["round_trip_id"] for f in fills}
        self.assertEqual(rt_ids[bid], rt_id)
        self.assertEqual(rt_ids[sid], rt_id)

    def test_matched_fills_no_longer_in_unmatched(self):
        bid, sid = self._pair()
        _db.match_round_trip(bid, sid, Decimal("0.0001"))
        self.assertEqual(_db.get_unmatched_fills("testcat", "buy"), [])
        self.assertEqual(_db.get_unmatched_fills("testcat", "sell"), [])


# ---------------------------------------------------------------------------
# 4. Full PnL round-trip integration flow
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestPnLRoundTripFlow(_TempDB):
    """End-to-end: ladder fill → PnL match → net position update."""

    def test_buy_then_sell_round_trip(self):
        # Simulate: bot bought 100 CAT @ 0.001 XCH, then sold 100 CAT @ 0.0011 XCH
        self._add_offer(
            "buy-1",
            "buy",
            Decimal("0.001"),
            size_xch=Decimal("0.1"),
            size_cat=Decimal("100"),
        )
        self._add_offer(
            "sell-1",
            "sell",
            Decimal("0.0011"),
            size_xch=Decimal("0.11"),
            size_cat=Decimal("100"),
        )

        buy_id = self._record_fill(
            "buy-1",
            "buy",
            Decimal("0.001"),
            size_xch=Decimal("0.1"),
            size_cat=Decimal("100"),
        )
        sell_id = self._record_fill(
            "sell-1",
            "sell",
            Decimal("0.0011"),
            size_xch=Decimal("0.11"),
            size_cat=Decimal("100"),
        )

        # Expected PnL: (sell_price - buy_price) * size_cat = 0.0001 * 100 = 0.01 XCH
        pnl = (Decimal("0.0011") - Decimal("0.001")) * Decimal("100")
        _db.match_round_trip(buy_id, sell_id, pnl)

        # Verify both fills matched
        self.assertEqual(_db.get_unmatched_fills("testcat", "buy"), [])
        self.assertEqual(_db.get_unmatched_fills("testcat", "sell"), [])

        # Net position = 0 after a complete round-trip
        net = _db.get_net_position("testcat")
        self.assertEqual(net, Decimal("0"))

    def test_net_position_after_unmatched_buys(self):
        for i in range(3):
            tid = f"buy-{i}"
            self._add_offer(
                tid,
                "buy",
                Decimal("0.001"),
                size_xch=Decimal("0.1"),
                size_cat=Decimal("100"),
            )
            self._record_fill(
                tid,
                "buy",
                Decimal("0.001"),
                size_xch=Decimal("0.1"),
                size_cat=Decimal("100"),
            )

        net = _db.get_net_position("testcat")
        self.assertEqual(net, Decimal("300"))  # 3 × 100 CAT

    def test_fifo_matching_order(self):
        # Buy at two prices — unmatched returns oldest first (FIFO)
        self._add_offer("buy-early", "buy", Decimal("0.001"))
        self._add_offer("buy-late", "buy", Decimal("0.0012"))
        self._record_fill("buy-early", "buy", Decimal("0.001"))
        self._record_fill("buy-late", "buy", Decimal("0.0012"))

        unmatched = _db.get_unmatched_fills("testcat", "buy")
        self.assertEqual(len(unmatched), 2)
        # Oldest fill first (filled_at ascending)
        self.assertEqual(unmatched[0]["trade_id"], "buy-early")

    def test_multiple_round_trips_independent(self):
        for i in range(3):
            self._add_offer(f"b{i}", "buy", Decimal("0.001"))
            self._add_offer(f"s{i}", "sell", Decimal("0.0011"))
            bid = self._record_fill(f"b{i}", "buy", Decimal("0.001"))
            sid = self._record_fill(f"s{i}", "sell", Decimal("0.0011"))
            pnl = Decimal("0.01")
            rt_id = _db.match_round_trip(bid, sid, pnl)
            self.assertGreater(rt_id, 0)

        # All matched
        self.assertEqual(_db.get_unmatched_fills("testcat", "buy"), [])
        self.assertEqual(_db.get_unmatched_fills("testcat", "sell"), [])
        net = _db.get_net_position("testcat")
        self.assertEqual(net, Decimal("0"))


# ---------------------------------------------------------------------------
# 5. get_net_position semantics
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestNetPosition(_TempDB):
    def test_empty_fills_returns_zero(self):
        self.assertEqual(_db.get_net_position("testcat"), Decimal("0"))

    def test_buys_give_positive_position(self):
        self._add_offer("b", "buy", Decimal("0.001"), size_cat=Decimal("500"))
        self._record_fill("b", "buy", Decimal("0.001"), size_cat=Decimal("500"))
        self.assertEqual(_db.get_net_position("testcat"), Decimal("500"))

    def test_sells_give_negative_position(self):
        self._add_offer("s", "sell", Decimal("0.001"), size_cat=Decimal("200"))
        self._record_fill("s", "sell", Decimal("0.001"), size_cat=Decimal("200"))
        self.assertEqual(_db.get_net_position("testcat"), Decimal("-200"))

    def test_net_zero_after_equal_buys_and_sells(self):
        self._add_offer("b", "buy", Decimal("0.001"), size_cat=Decimal("100"))
        self._add_offer("s", "sell", Decimal("0.002"), size_cat=Decimal("100"))
        self._record_fill("b", "buy", Decimal("0.001"), size_cat=Decimal("100"))
        self._record_fill("s", "sell", Decimal("0.002"), size_cat=Decimal("100"))
        self.assertEqual(_db.get_net_position("testcat"), Decimal("0"))

    def test_different_assets_isolated(self):
        self._add_offer(
            "ba", "buy", Decimal("0.001"), size_cat=Decimal("100"), asset="catA"
        )
        _db.record_fill(
            "ba", "buy", Decimal("0.001"), Decimal("0.1"), Decimal("100"), "catA"
        )
        self._add_offer(
            "bb", "buy", Decimal("0.001"), size_cat=Decimal("200"), asset="catB"
        )
        _db.record_fill(
            "bb", "buy", Decimal("0.001"), Decimal("0.2"), Decimal("200"), "catB"
        )
        self.assertEqual(_db.get_net_position("catA"), Decimal("100"))
        self.assertEqual(_db.get_net_position("catB"), Decimal("200"))


if __name__ == "__main__":
    unittest.main()
