"""Slice 02-18 — unit tests for shape_fix_orchestrator.py and sweep_coordinator.py pure types."""

import sys
import os
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shape_fix_orchestrator import (
    Stage,
    HaltReason,
    P1_PIPELINE,
    P2_PIPELINE,
    ACTIVE_PIPELINE,
    FlowState,
)
from sweep_coordinator import SweepEntry, SweepEvent


# ---------------------------------------------------------------------------
# Stage enum
# ---------------------------------------------------------------------------


class TestStageEnum(unittest.TestCase):
    def test_all_members(self):
        names = {m.name for m in Stage}
        self.assertEqual(
            names,
            {
                "CANCELLING",
                "WAITING_FOR_CONFIRMATION",
                "CHECKING_COINS",
                "RESHAPING",
                "REBUILDING",
                "COMPLETE",
                "HALTED",
            },
        )

    def test_string_values(self):
        self.assertEqual(Stage.CANCELLING.value, "cancelling")
        self.assertEqual(Stage.COMPLETE.value, "complete")
        self.assertEqual(Stage.HALTED.value, "halted")


# ---------------------------------------------------------------------------
# HaltReason enum
# ---------------------------------------------------------------------------


class TestHaltReasonEnum(unittest.TestCase):
    def test_all_members(self):
        names = {m.name for m in HaltReason}
        self.assertEqual(
            names,
            {
                "USER_ABORTED",
                "TIMEOUT_CONFIRMATION",
                "TIMEOUT_RESHAPE",
                "POSITION_GUARD_BLOCKED",
                "NO_TIER_COINS_POSSIBLE",
                "CANCEL_REJECTED",
                "INTERNAL_ERROR",
            },
        )

    def test_string_values(self):
        self.assertEqual(HaltReason.USER_ABORTED.value, "user_aborted")
        self.assertEqual(HaltReason.INTERNAL_ERROR.value, "internal_error")


# ---------------------------------------------------------------------------
# Pipeline constants
# ---------------------------------------------------------------------------


class TestPipelineConstants(unittest.TestCase):
    def test_p1_pipeline_stages(self):
        self.assertEqual(
            P1_PIPELINE, (Stage.CANCELLING, Stage.WAITING_FOR_CONFIRMATION)
        )

    def test_p2_pipeline_stages(self):
        self.assertEqual(
            P2_PIPELINE,
            (
                Stage.CANCELLING,
                Stage.WAITING_FOR_CONFIRMATION,
                Stage.CHECKING_COINS,
                Stage.REBUILDING,
            ),
        )

    def test_active_pipeline_is_p2(self):
        self.assertIs(ACTIVE_PIPELINE, P2_PIPELINE)


# ---------------------------------------------------------------------------
# FlowState.is_terminal
# ---------------------------------------------------------------------------


class TestFlowStateIsTerminal(unittest.TestCase):
    def _make(self, stage):
        return FlowState(flow_id="f1", side="buy", trade_ids=[], stage=stage)

    def test_complete_is_terminal(self):
        self.assertTrue(self._make(Stage.COMPLETE).is_terminal())

    def test_halted_is_terminal(self):
        self.assertTrue(self._make(Stage.HALTED).is_terminal())

    def test_running_stages_not_terminal(self):
        for stage in (
            Stage.CANCELLING,
            Stage.WAITING_FOR_CONFIRMATION,
            Stage.CHECKING_COINS,
            Stage.REBUILDING,
            Stage.RESHAPING,
        ):
            # subTest kwargs are serialized by pytest-xdist on pytest>=9;
            # pass the enum name (string) not the enum itself.
            with self.subTest(stage=stage.name):
                self.assertFalse(self._make(stage).is_terminal())


# ---------------------------------------------------------------------------
# FlowState.status property
# ---------------------------------------------------------------------------


class TestFlowStateStatus(unittest.TestCase):
    def _make(self, stage=Stage.CANCELLING, halt_reason=None):
        fs = FlowState(flow_id="f1", side="sell", trade_ids=[], stage=stage)
        fs.halt_reason = halt_reason
        return fs

    def test_running_status(self):
        self.assertEqual(self._make().status, "running")

    def test_complete_status(self):
        self.assertEqual(self._make(stage=Stage.COMPLETE).status, "complete")

    def test_halted_status_when_halt_reason_set(self):
        fs = self._make(stage=Stage.HALTED, halt_reason=HaltReason.USER_ABORTED)
        self.assertEqual(fs.status, "halted")

    def test_halt_reason_overrides_complete(self):
        # halt_reason takes priority (halt_reason check comes first)
        fs = self._make(stage=Stage.COMPLETE, halt_reason=HaltReason.INTERNAL_ERROR)
        self.assertEqual(fs.status, "halted")


# ---------------------------------------------------------------------------
# FlowState.to_dict
# ---------------------------------------------------------------------------


class TestFlowStateToDict(unittest.TestCase):
    def _make(self, **kw):
        defaults = dict(flow_id="f42", side="buy", trade_ids=["t1", "t2"])
        defaults.update(kw)
        return FlowState(**defaults)

    def test_required_keys_present(self):
        d = self._make().to_dict()
        for key in (
            "flow_id",
            "side",
            "stage",
            "status",
            "detail",
            "elapsed_ms",
            "pipeline",
            "stages_completed",
            "halt_reason",
            "summary",
        ):
            self.assertIn(key, d)

    def test_pipeline_uses_active_by_default(self):
        d = self._make().to_dict()
        self.assertEqual(d["pipeline"], [s.value for s in ACTIVE_PIPELINE])

    def test_custom_pipeline_honoured(self):
        d = self._make().to_dict(pipeline=P1_PIPELINE)
        self.assertEqual(d["pipeline"], [s.value for s in P1_PIPELINE])

    def test_halt_reason_none_serialised_as_none(self):
        d = self._make().to_dict()
        self.assertIsNone(d["halt_reason"])

    def test_halt_reason_serialised_as_value(self):
        fs = self._make()
        fs.halt_reason = HaltReason.TIMEOUT_CONFIRMATION
        d = fs.to_dict()
        self.assertEqual(d["halt_reason"], "timeout_waiting_for_confirmation")

    def test_summary_subkeys(self):
        d = self._make().to_dict()
        self.assertIn("cancelled_count", d["summary"])
        self.assertIn("new_offer_count", d["summary"])
        self.assertIn("total_requested", d["summary"])

    def test_elapsed_ms_non_negative(self):
        d = self._make().to_dict()
        self.assertGreaterEqual(d["elapsed_ms"], 0)

    def test_stage_serialised_as_string(self):
        fs = self._make()
        fs.stage = Stage.CHECKING_COINS
        d = fs.to_dict()
        self.assertEqual(d["stage"], "checking_coins")


# ---------------------------------------------------------------------------
# SweepEntry dataclass
# ---------------------------------------------------------------------------


class TestSweepEntry(unittest.TestCase):
    def test_construction(self):
        e = SweepEntry(
            fill_id=1,
            trade_id="t1",
            classification="DEXIE_COMBINED",
            spent_block_index=42,
        )
        self.assertEqual(e.fill_id, 1)
        self.assertEqual(e.trade_id, "t1")
        self.assertEqual(e.spent_block_index, 42)

    def test_optional_fields_default_none(self):
        e = SweepEntry(
            fill_id=2, trade_id="t2", classification="UNKNOWN", spent_block_index=10
        )
        self.assertIsNone(e.taker_puzzle_hash)
        self.assertIsNone(e.side)

    def test_added_at_defaults_monotonic(self):
        before = time.monotonic()
        e = SweepEntry(
            fill_id=3, trade_id="t3", classification="x", spent_block_index=0
        )
        after = time.monotonic()
        self.assertGreaterEqual(e.added_at, before)
        self.assertLessEqual(e.added_at, after)


# ---------------------------------------------------------------------------
# SweepEvent.fill_count
# ---------------------------------------------------------------------------


def _make_entry(trade_id="t1", block=100):
    return SweepEntry(
        fill_id=1, trade_id=trade_id, classification="x", spent_block_index=block
    )


class TestSweepEventFillCount(unittest.TestCase):
    def test_empty_fills(self):
        ev = SweepEvent(sweep_group_id="g1", spent_block_index=100, fills=[])
        self.assertEqual(ev.fill_count, 0)

    def test_single_fill(self):
        ev = SweepEvent(
            sweep_group_id="g1", spent_block_index=100, fills=[_make_entry()]
        )
        self.assertEqual(ev.fill_count, 1)

    def test_multiple_fills(self):
        ev = SweepEvent(
            sweep_group_id="g1",
            spent_block_index=100,
            fills=[_make_entry("t1"), _make_entry("t2"), _make_entry("t3")],
        )
        self.assertEqual(ev.fill_count, 3)


# ---------------------------------------------------------------------------
# SweepEvent.trade_ids
# ---------------------------------------------------------------------------


class TestSweepEventTradeIds(unittest.TestCase):
    def test_empty_returns_empty_list(self):
        ev = SweepEvent(sweep_group_id="g1", spent_block_index=100, fills=[])
        self.assertEqual(ev.trade_ids, [])

    def test_returns_trade_ids_in_order(self):
        ev = SweepEvent(
            sweep_group_id="g1",
            spent_block_index=100,
            fills=[_make_entry("alpha"), _make_entry("beta")],
        )
        self.assertEqual(ev.trade_ids, ["alpha", "beta"])


# ---------------------------------------------------------------------------
# SweepEvent.__str__
# ---------------------------------------------------------------------------


class TestSweepEventStr(unittest.TestCase):
    def test_contains_block_index(self):
        ev = SweepEvent(
            sweep_group_id="grp1", spent_block_index=999, fills=[_make_entry()]
        )
        self.assertIn("999", str(ev))

    def test_contains_fill_count(self):
        ev = SweepEvent(
            sweep_group_id="grp1",
            spent_block_index=5,
            fills=[_make_entry("a"), _make_entry("b")],
        )
        self.assertIn("2", str(ev))

    def test_contains_group_id(self):
        ev = SweepEvent(sweep_group_id="mygroup", spent_block_index=1, fills=[])
        self.assertIn("mygroup", str(ev))


if __name__ == "__main__":
    unittest.main()
