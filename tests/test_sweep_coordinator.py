"""Tests for sweep_coordinator.py."""

import sys
import types
import unittest


# ---------------------------------------------------------------------------
# Minimal fakes
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.updates = []

    def execute(self, sql, params=()):
        self.updates.append((sql, params))
        return self

    def commit(self):
        pass

    def fetchone(self):
        return None


_fake_conn: _FakeConn = _FakeConn()


def _install_fakes():
    fake_config = types.ModuleType("config")
    fake_config.cfg = types.SimpleNamespace(
        SWEEP_WINDOW_SECS=15.0,
        SWEEP_MIN_FILLS=2,   # tests exercise the 2-fill boundary explicitly
    )
    sys.modules["config"] = fake_config

    fake_database = types.ModuleType("database")
    fake_database.get_connection = lambda: _fake_conn
    sys.modules["database"] = fake_database

    fake_fill_classifier = types.ModuleType("fill_classifier")

    class _FakeType:
        RETAIL        = "retail"
        ARB_SWEEP_BUY = "arb_sweep_buy"
        ARB_SWEEP_SELL= "arb_sweep_sell"
        DEXIE_COMBINED= "dexie_combined"
        UNKNOWN       = "unknown"

    fake_fill_classifier.FillType = _FakeType
    sys.modules["fill_classifier"] = fake_fill_classifier


def _make_cls(trade_id, classification="unknown", block_idx=None,
              taker_ph=None, side=None):
    """Build a minimal FillClassification-like object."""

    class _Cls:
        pass

    c = _Cls()
    c.trade_id           = trade_id
    c.classification     = classification
    c.spent_block_index  = block_idx
    c.taker_puzzle_hash  = taker_ph
    c.side               = side
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class SweepCoordinatorTests(unittest.TestCase):
    def setUp(self):
        global _fake_conn
        _fake_conn = _FakeConn()
        _install_fakes()
        sys.modules.pop("sweep_coordinator", None)
        import sweep_coordinator
        self.sc_mod = sweep_coordinator
        self.sc_mod.reset_coordinator()

    def tearDown(self):
        for name in ["sweep_coordinator", "fill_classifier", "database", "config"]:
            sys.modules.pop(name, None)

    # ------------------------------------------------------------------
    # Basic grouping
    # ------------------------------------------------------------------

    def test_single_fill_no_block_index_returns_none(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.001)
        cls = _make_cls("t1", block_idx=None)
        result = sc.process_fill(1, cls)
        self.assertIsNone(result)

    def test_single_fill_with_block_index_no_group_yet(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=60.0)
        cls = _make_cls("t1", block_idx=100)
        result = sc.process_fill(1, cls)
        # Only one fill — no group ID yet
        self.assertIsNone(result)

    def test_two_fills_same_block_returns_group_id(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=60.0)
        cls1 = _make_cls("t1", block_idx=200)
        cls2 = _make_cls("t2", block_idx=200)
        sc.process_fill(1, cls1)
        gid = sc.process_fill(2, cls2)
        self.assertEqual(gid, "sweep_200")

    def test_two_fills_different_blocks_are_not_grouped(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=60.0)
        cls1 = _make_cls("t1", block_idx=300)
        cls2 = _make_cls("t2", block_idx=301)
        sc.process_fill(1, cls1)
        gid = sc.process_fill(2, cls2)
        self.assertIsNone(gid)

    # ------------------------------------------------------------------
    # Window expiry and event draining
    # ------------------------------------------------------------------

    def test_single_fill_group_not_emitted_as_event(self):
        """Groups with only one fill should not produce a SweepEvent."""
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls = _make_cls("t1", block_idx=400)
        sc.process_fill(1, cls)
        sc.tick()
        events = sc.drain_sweep_events()
        self.assertEqual(events, [])

    def test_two_fill_group_emits_sweep_event_after_window(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls1 = _make_cls("t1", block_idx=500)
        cls2 = _make_cls("t2", block_idx=500)
        sc.process_fill(1, cls1)
        sc.process_fill(2, cls2)
        sc.tick()
        events = sc.drain_sweep_events()
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.sweep_group_id, "sweep_500")
        self.assertEqual(evt.spent_block_index, 500)
        self.assertEqual(evt.fill_count, 2)
        self.assertIn("t1", evt.trade_ids)
        self.assertIn("t2", evt.trade_ids)

    def test_drain_clears_event_buffer(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        for i in range(3):
            sc.process_fill(i, _make_cls(f"t{i}", block_idx=600))
        sc.tick()
        first_drain  = sc.drain_sweep_events()
        second_drain = sc.drain_sweep_events()
        self.assertEqual(len(first_drain), 1)
        self.assertEqual(second_drain, [])

    # ------------------------------------------------------------------
    # UNKNOWN → DEXIE_COMBINED upgrade
    # ------------------------------------------------------------------

    def test_unknown_fills_upgraded_to_dexie_combined_in_db(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls1 = _make_cls("t1", classification="unknown", block_idx=700)
        cls2 = _make_cls("t2", classification="unknown", block_idx=700)
        sc.process_fill(10, cls1)
        sc.process_fill(11, cls2)
        sc.tick()
        sc.drain_sweep_events()

        # Both fills should have triggered DB UPDATE with dexie_combined
        sql_updates = [p for sql, p in _fake_conn.updates
                       if "fill_classification" in sql and "dexie_combined" in str(p)]
        self.assertEqual(len(sql_updates), 2)

    def test_already_classified_fills_not_reclassified(self):
        """ARB_SWEEP fills get sweep_group_id but not reclassified."""
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls1 = _make_cls("t1", classification="arb_sweep_buy", block_idx=800)
        cls2 = _make_cls("t2", classification="arb_sweep_buy", block_idx=800)
        sc.process_fill(20, cls1)
        sc.process_fill(21, cls2)
        sc.tick()
        sc.drain_sweep_events()

        # Should NOT have any dexie_combined updates
        dexie_combined_updates = [
            p for sql, p in _fake_conn.updates
            if "fill_classification" in sql and "dexie_combined" in str(p)
        ]
        self.assertEqual(dexie_combined_updates, [])

        # But SHOULD have sweep_group_id updates
        group_updates = [
            p for sql, p in _fake_conn.updates
            if "sweep_group_id" in sql
        ]
        self.assertGreater(len(group_updates), 0)

    # ------------------------------------------------------------------
    # get_pending_summary
    # ------------------------------------------------------------------

    def test_pending_summary_reflects_buffered_fills(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=60.0)
        for i in range(3):
            sc.process_fill(i, _make_cls(f"t{i}", block_idx=900 + i))
        summary = sc.get_pending_summary()
        self.assertEqual(summary["pending_block_groups"], 3)
        self.assertEqual(summary["pending_fill_count"], 3)

    def test_pending_summary_empty_after_tick(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        sc.process_fill(1, _make_cls("t1", block_idx=1000))
        sc.tick()
        summary = sc.get_pending_summary()
        self.assertEqual(summary["pending_block_groups"], 0)

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    def test_get_coordinator_returns_same_instance(self):
        c1 = self.sc_mod.get_coordinator()
        c2 = self.sc_mod.get_coordinator()
        self.assertIs(c1, c2)

    def test_reset_coordinator_creates_new_instance(self):
        c1 = self.sc_mod.get_coordinator()
        self.sc_mod.reset_coordinator()
        c2 = self.sc_mod.get_coordinator()
        self.assertIsNot(c1, c2)

    # ------------------------------------------------------------------
    # SweepEvent properties
    # ------------------------------------------------------------------

    def test_sweep_event_str_contains_key_info(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        for i in range(2):
            sc.process_fill(i, _make_cls(f"t{i}", block_idx=1100))
        sc.tick()
        events = sc.drain_sweep_events()
        self.assertEqual(len(events), 1)
        s = str(events[0])
        self.assertIn("1100", s)
        self.assertIn("2", s)

    # ------------------------------------------------------------------
    # side field propagation (Fix #2)
    # ------------------------------------------------------------------

    def test_side_propagated_to_sweep_entry(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls1 = _make_cls("t1", block_idx=1200, side="sell")
        cls2 = _make_cls("t2", block_idx=1200, side="sell")
        sc.process_fill(1, cls1)
        sc.process_fill(2, cls2)
        sc.tick()
        events = sc.drain_sweep_events()
        self.assertEqual(len(events), 1)
        for entry in events[0].fills:
            self.assertEqual(entry.side, "sell")

    def test_side_none_when_not_provided(self):
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls1 = _make_cls("t1", block_idx=1300)   # no side kwarg
        cls2 = _make_cls("t2", block_idx=1300)
        sc.process_fill(1, cls1)
        sc.process_fill(2, cls2)
        sc.tick()
        events = sc.drain_sweep_events()
        self.assertEqual(len(events), 1)
        for entry in events[0].fills:
            self.assertIsNone(entry.side)

    def test_mixed_sides_both_preserved(self):
        """Buy and sell fills in the same block both keep their sides."""
        sc = self.sc_mod.SweepCoordinator(window_secs=0.0)
        cls1 = _make_cls("t1", block_idx=1400, side="buy")
        cls2 = _make_cls("t2", block_idx=1400, side="sell")
        sc.process_fill(1, cls1)
        sc.process_fill(2, cls2)
        sc.tick()
        events = sc.drain_sweep_events()
        self.assertEqual(len(events), 1)
        sides = {e.side for e in events[0].fills}
        self.assertEqual(sides, {"buy", "sell"})


if __name__ == "__main__":
    unittest.main()
