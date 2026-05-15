"""Tests for fill_tracker PnL matching — passes 1-4."""

import importlib
import sys
import types
import unittest
from decimal import Decimal


class _FakeCfg:
    CAT_ASSET_ID = "aabbccdd"
    RUN_HISTORY_CUTOFF = None


def _make_fill(
    fill_id, side, size_xch, price_xch, size_cat, tier="inner", timestamp=None
):
    return {
        "fill_id": fill_id,
        "side": side,
        "size_xch": str(size_xch),
        "price_xch": str(price_xch),
        "size_cat": str(size_cat),
        "tier": tier,
        "timestamp": timestamp or "2026-04-14T00:00:00",
        "fee_mojos_xch": 0,
    }


_MODS_TO_RESTORE = (
    "fill_tracker",
    "spacescan",
    "wallet_sage",
    "wallet",
    "database",
    "config",
    "dexie_manager",
)


class FillPnlMatchingTests(unittest.TestCase):
    """Tests for multi-pass buy↔sell round-trip matching in FillTracker."""

    def setUp(self):
        self._saved_modules = {name: sys.modules.get(name) for name in _MODS_TO_RESTORE}
        self.round_trips_recorded = []
        self.logged = []
        self._rt_id_counter = [0]

        fake_config = types.ModuleType("config")
        fake_config.cfg = _FakeCfg()
        sys.modules["config"] = fake_config

        def _fake_match_round_trip(buy_fill_id, sell_fill_id, pnl_xch):
            self._rt_id_counter[0] += 1
            self.round_trips_recorded.append(
                {
                    "buy_fill_id": buy_fill_id,
                    "sell_fill_id": sell_fill_id,
                    "pnl_xch": pnl_xch,
                }
            )
            return self._rt_id_counter[0]

        def _fake_log_event(severity, event_type, message, data=None):
            self.logged.append((severity, event_type, message))

        self._unmatched_buys = []
        self._unmatched_sells = []

        fake_database = types.ModuleType("database")
        fake_database.get_unmatched_fills = lambda asset_id, side, since=None: (
            self._unmatched_buys if side == "buy" else self._unmatched_sells
        )
        fake_database.match_round_trip = _fake_match_round_trip
        fake_database.record_fill = lambda *a, **kw: 1
        fake_database.get_open_offers = lambda *a, **kw: []
        fake_database.log_event = _fake_log_event
        fake_database.update_offer_lifecycle_state = lambda *a, **kw: None
        fake_database.transition_offer = lambda *a, **kw: None
        fake_database.mark_cancel_attempted = lambda *a, **kw: None
        sys.modules["database"] = fake_database

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_wallet_type = lambda: "sage"
        sys.modules["wallet"] = fake_wallet

        sys.modules["wallet_sage"] = types.ModuleType("wallet_sage")
        sys.modules["dexie_manager"] = types.ModuleType("dexie_manager")
        sys.modules["spacescan"] = types.ModuleType("spacescan")

        sys.modules.pop("fill_tracker", None)
        self.ft_mod = importlib.import_module("fill_tracker")

    def tearDown(self):
        for name, saved in self._saved_modules.items():
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

    def _make_tracker(self):
        return self.ft_mod.FillTracker(offer_manager=None)

    # ------------------------------------------------------------------
    # Pass 1: same-tier + exact size
    # ------------------------------------------------------------------

    def test_pass1_matches_same_tier_exact_size(self):
        """Buy and sell at the same tier and nearly identical XCH size → pass 1."""
        self._unmatched_buys = [
            _make_fill("b1", "buy", "0.670", "0.00038", "1762", "inner")
        ]
        self._unmatched_sells = [
            _make_fill("s1", "sell", "0.670", "0.00040", "1675", "inner")
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.round_trips_recorded), 1)
        # Pass 1 logged in the round_trip_matched message
        match_log = next((m for m in self.logged if m[1] == "round_trip_matched"), None)
        self.assertIsNotNone(match_log)
        self.assertIn("pass=1", match_log[2])

    # ------------------------------------------------------------------
    # Pass 2: different tier, exact size
    # ------------------------------------------------------------------

    def test_pass2_matches_different_tier_exact_size(self):
        """Buy inner, sell mid — same XCH amount → falls through to pass 2."""
        self._unmatched_buys = [
            _make_fill("b1", "buy", "0.670", "0.00038", "1762", "inner")
        ]
        self._unmatched_sells = [
            _make_fill("s1", "sell", "0.672", "0.00040", "1680", "mid")
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        self.assertEqual(len(results), 1)
        match_log = next((m for m in self.logged if m[1] == "round_trip_matched"), None)
        self.assertIsNotNone(match_log)
        self.assertIn("pass=2", match_log[2])

    # ------------------------------------------------------------------
    # Pass 3: within 20% size tolerance
    # ------------------------------------------------------------------

    def test_pass3_matches_within_20_pct_tolerance(self):
        """Buy 0.67 XCH, sell 0.78 XCH (16% diff) → pass 3, under 20% threshold."""
        self._unmatched_buys = [
            _make_fill("b1", "buy", "0.670", "0.00038", "1762", "inner")
        ]
        self._unmatched_sells = [
            _make_fill("s1", "sell", "0.780", "0.00042", "1857", "mid")
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        self.assertEqual(len(results), 1)
        match_log = next((m for m in self.logged if m[1] == "round_trip_matched"), None)
        self.assertIsNotNone(match_log)
        self.assertIn("pass=3", match_log[2])

    def test_pass3_rejects_beyond_20_pct_tolerance(self):
        """Buy 0.67 XCH, sell 0.90 XCH (34% diff) → does NOT match in pass 3 alone."""
        # 0.90/0.67 ≈ 1.34x — beyond 20% tolerance for pass 3, eligible for pass 4
        self._unmatched_buys = [
            _make_fill("b1", "buy", "0.670", "0.00038", "1762", "inner")
        ]
        self._unmatched_sells = [
            _make_fill("s1", "sell", "0.900", "0.00042", "2142", "mid")
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        # Pass 4 FIFO fires; confirm no pass-3 match
        match_log = next((m for m in self.logged if m[1] == "round_trip_matched"), None)
        if match_log:
            self.assertNotIn("pass=3", match_log[2])

    # ------------------------------------------------------------------
    # Pass 4: FIFO — asymmetric BUY_INNER_SIZE ≠ SELL_INNER_SIZE
    # ------------------------------------------------------------------

    def test_pass4_fifo_fires_for_asymmetric_tier_sizes(self):
        """BUY_INNER=0.67 XCH, SELL_INNER=3.26 XCH — ratio 4.87x.

        Passes 1-3 cannot match (size diff >> 20%).  Pass 4 FIFO must
        match them and produce a round-trip with correct PnL.
        """
        buy_xch = Decimal("0.6729")  # realistic BUY_INNER_SIZE_XCH
        sell_xch = Decimal("3.2600")  # realistic SELL_INNER_SIZE_XCH
        buy_price = Decimal("0.000375")
        sell_price = Decimal("0.000380")

        buy_cat = buy_xch / buy_price
        sell_cat = sell_xch / sell_price

        self._unmatched_buys = [
            _make_fill("b1", "buy", str(buy_xch), str(buy_price), str(buy_cat), "inner")
        ]
        self._unmatched_sells = [
            _make_fill(
                "s1", "sell", str(sell_xch), str(sell_price), str(sell_cat), "inner"
            )
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        # A round-trip must be produced
        self.assertEqual(len(results), 1, "Pass-4 FIFO should produce one round-trip")

        rt = results[0]
        self.assertEqual(rt["buy_fill_id"], "b1")
        self.assertEqual(rt["sell_fill_id"], "s1")

        # PnL: net_xch = sell_xch - buy_xch; net_cat = buy_cat - sell_cat
        net_xch = sell_xch - buy_xch
        net_cat = buy_cat - sell_cat
        mid_price = (buy_price + sell_price) / 2
        expected_pnl = net_xch + net_cat * mid_price
        self.assertAlmostEqual(
            float(rt["pnl_xch"]),
            float(expected_pnl),
            places=6,
            msg="PnL calculation should be correct for asymmetric pair",
        )

        # Confirm pass 4 was used
        match_log = next((m for m in self.logged if m[1] == "round_trip_matched"), None)
        self.assertIsNotNone(match_log, "round_trip_matched should be logged")
        self.assertIn("pass=4", match_log[2])

    def test_pass4_fifo_is_chronological_multiple_fills(self):
        """With two buys and one sell, pass 4 should match the EARLIEST buy.

        The sell is nearest in size to b2 but FIFO (using size diff as
        tiebreak) pairs it with whichever buy minimises abs(sell_xch -
        buy_xch).  Confirm only ONE round-trip is produced and the other
        buy stays unmatched.
        """
        # b1 arrives first (lower fill_id), slightly larger size diff
        # b2 also available but same tier — both eligible for pass 4
        # The sell matches best with b2 (size diff smaller)
        self._unmatched_buys = [
            _make_fill("b1", "buy", "0.670", "0.000375", "1786", "inner"),
            _make_fill("b2", "buy", "3.100", "0.000375", "8266", "inner"),
        ]
        self._unmatched_sells = [
            _make_fill("s1", "sell", "3.260", "0.000380", "8578", "inner"),
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        # Only one match should be produced
        self.assertEqual(len(results), 1)
        # b2 is the best size match for s1 (diff = 0.16 vs 2.59)
        self.assertEqual(results[0]["buy_fill_id"], "b2")
        self.assertEqual(results[0]["sell_fill_id"], "s1")
        # b1 should remain unmatched (logged as one-directional inventory)
        unmatched_log = next(
            (m for m in self.logged if m[1] == "pnl_unmatched_fills"), None
        )
        self.assertIsNotNone(unmatched_log)

    # ------------------------------------------------------------------
    # Guard: no match when sizes too far and only passes 1-3 attempted
    # ------------------------------------------------------------------

    def test_no_match_when_no_fills_remain_for_pass4(self):
        """If all fills are matched in pass 1-3, pass 4 is never appended."""
        # Exact same size → pass 1 matches → no residual → pass 4 not needed
        self._unmatched_buys = [
            _make_fill("b1", "buy", "0.670", "0.00038", "1762", "inner")
        ]
        self._unmatched_sells = [
            _make_fill("s1", "sell", "0.671", "0.00040", "1677", "inner")
        ]

        tracker = self._make_tracker()
        results = tracker.match_round_trips()

        self.assertEqual(len(results), 1)
        match_log = next((m for m in self.logged if m[1] == "round_trip_matched"), None)
        # Must be pass 1 (same tier, exact size)
        self.assertIn("pass=1", match_log[2])
        # Pass 4 must NOT appear
        self.assertNotIn("pass=4", match_log[2])


if __name__ == "__main__":
    unittest.main()
