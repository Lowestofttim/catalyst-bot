"""Slice 02-31 — super_log.py unit tests: ring buffer, level filtering, cycle stats.

No file I/O. Tests cover slog (category sanitize, message sanitize, ring buffer),
LEVELS dict, set_file_level/set_terminal_level, start_cycle/cycle_count/
cycle_note/end_cycle, log_db_write/log_db_lock, and get_log_stats keys.
"""

import sys
import time
import types
import unittest

try:
    import super_log as _sl
    _SKIP = None
except ModuleNotFoundError as exc:
    _sl = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Helper: ensure slog doesn't write to file (not initialized)
# ---------------------------------------------------------------------------

def _call_slog(*args, **kwargs):
    """Call slog in an uninitialized state (ring buffer only, no file I/O)."""
    orig = _sl._initialized
    _sl._initialized = False
    try:
        _sl.slog(*args, **kwargs)
    finally:
        _sl._initialized = orig


# ===========================================================================
# LEVELS constant
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestLevels(unittest.TestCase):
    def test_all_five_levels_present(self):
        for lvl in ("trace", "debug", "info", "warn", "error"):
            self.assertIn(lvl, _sl.LEVELS)

    def test_levels_ordered_correctly(self):
        self.assertLess(_sl.LEVELS["trace"], _sl.LEVELS["debug"])
        self.assertLess(_sl.LEVELS["debug"], _sl.LEVELS["info"])
        self.assertLess(_sl.LEVELS["info"], _sl.LEVELS["warn"])
        self.assertLess(_sl.LEVELS["warn"], _sl.LEVELS["error"])


# ===========================================================================
# slog — ring buffer and sanitization
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestSlogRingBuffer(unittest.TestCase):
    def setUp(self):
        _sl._ring_buffer.clear()

    def test_slog_appends_to_ring_buffer(self):
        _call_slog("TEST", "ring buffer check")
        self.assertGreater(len(_sl._ring_buffer), 0)

    def test_slog_last_line_contains_message(self):
        _call_slog("TEST", "unique_test_message_xyz")
        last = list(_sl._ring_buffer)[-1]
        self.assertIn("unique_test_message_xyz", last)

    def test_slog_last_line_contains_category(self):
        _call_slog("MYCAT", "test msg")
        last = list(_sl._ring_buffer)[-1]
        self.assertIn("MYCAT", last)

    def test_slog_category_sanitizes_newlines(self):
        _call_slog("CAT\nINJECT", "message")
        last = list(_sl._ring_buffer)[-1]
        self.assertNotIn("\n", last)

    def test_slog_message_sanitizes_newlines(self):
        _call_slog("TEST", "line1\nline2")
        last = list(_sl._ring_buffer)[-1]
        self.assertNotIn("\n", last)

    def test_slog_category_truncated_to_12(self):
        _call_slog("A" * 20, "msg")
        last = list(_sl._ring_buffer)[-1]
        # The category slot is 12 chars wide — should not contain the full 20-char string
        self.assertNotIn("A" * 20, last)

    def test_slog_with_data_includes_data_in_line(self):
        _call_slog("TEST", "msg", data={"key1": "val1"})
        last = list(_sl._ring_buffer)[-1]
        self.assertIn("key1", last)
        self.assertIn("val1", last)

    def test_slog_level_unknown_defaults_to_info(self):
        # Should not raise
        _call_slog("TEST", "msg", level="nonexistent_level")


# ===========================================================================
# set_file_level / set_terminal_level
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestSetLevel(unittest.TestCase):
    def setUp(self):
        self._orig_file = _sl._file_level
        self._orig_term = _sl._terminal_level

    def tearDown(self):
        _sl._file_level = self._orig_file
        _sl._terminal_level = self._orig_term

    def test_set_file_level_debug(self):
        _sl.set_file_level("debug")
        self.assertEqual(_sl._file_level, _sl.LEVELS["debug"])

    def test_set_file_level_error(self):
        _sl.set_file_level("error")
        self.assertEqual(_sl._file_level, _sl.LEVELS["error"])

    def test_set_terminal_level_trace(self):
        _sl.set_terminal_level("trace")
        self.assertEqual(_sl._terminal_level, _sl.LEVELS["trace"])

    def test_set_terminal_level_invalid_ignored(self):
        _sl.set_terminal_level("bogus_level")
        # Should not raise and level should be unchanged or fallback gracefully
        self.assertIsInstance(_sl._terminal_level, int)


# ===========================================================================
# _TeeWriter
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestTeeWriter(unittest.TestCase):
    def test_none_original_stream_is_safe_for_no_console_builds(self):
        writer = _sl._TeeWriter(None, _sl._log_lock)

        writer.write("message")
        writer.flush()

        with self.assertRaises(AttributeError):
            writer.encoding


# ===========================================================================
# Cycle tracking
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestCycleStats(unittest.TestCase):
    def setUp(self):
        _sl._ring_buffer.clear()
        _sl.start_cycle(1)

    def test_start_cycle_sets_cycle_num(self):
        self.assertEqual(_sl._cycle_stats.cycle_num, 1)

    def test_start_cycle_zeros_fills(self):
        self.assertEqual(_sl._cycle_stats.fills, 0)

    def test_start_cycle_clears_notes(self):
        self.assertEqual(_sl._cycle_stats.notes, [])

    def test_cycle_count_increments(self):
        _sl.cycle_count("fills", 1)
        _sl.cycle_count("fills", 1)
        self.assertEqual(_sl._cycle_stats.fills, 2)

    def test_cycle_count_increments_by_value(self):
        _sl.cycle_count("snipes", 3)
        self.assertEqual(_sl._cycle_stats.snipes, 3)

    def test_cycle_note_appends(self):
        _sl.cycle_note("test note 1")
        _sl.cycle_note("test note 2")
        self.assertIn("test note 1", _sl._cycle_stats.notes)
        self.assertIn("test note 2", _sl._cycle_stats.notes)

    def test_end_cycle_logs_to_ring_buffer(self):
        _sl._ring_buffer.clear()
        _sl.end_cycle(mid_price=1.05, spread_bps=800, inventory=0.5, open_offers=10)
        self.assertGreater(len(_sl._ring_buffer), 0)

    def test_end_cycle_line_contains_cycle_num(self):
        _sl._ring_buffer.clear()
        _sl.start_cycle(42)
        _sl.end_cycle()
        last = list(_sl._ring_buffer)[-1]
        self.assertIn("42", last)

    def test_start_cycle_clears_previous_counters(self):
        _sl.cycle_count("fills", 5)
        _sl.start_cycle(2)
        self.assertEqual(_sl._cycle_stats.fills, 0)


# ===========================================================================
# log_db_write / log_db_lock
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestLogDbHelpers(unittest.TestCase):
    def setUp(self):
        _sl._ring_buffer.clear()
        _sl._initialized = False

    def test_log_db_write_appends_to_ring_buffer(self):
        _sl.log_db_write("INSERT", "offers table")
        self.assertGreater(len(_sl._ring_buffer), 0)

    def test_log_db_write_contains_operation(self):
        _sl.log_db_write("UPDATE_OFFERS")
        last = list(_sl._ring_buffer)[-1]
        self.assertIn("UPDATE_OFFERS", last)

    def test_log_db_lock_appends_to_ring_buffer(self):
        _sl.log_db_lock("SELECT_LOCK", 0)
        self.assertGreater(len(_sl._ring_buffer), 0)


# ===========================================================================
# get_log_stats
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestGetLogStats(unittest.TestCase):
    def test_returns_expected_keys(self):
        stats = _sl.get_log_stats()
        for key in ("bytes_written", "ring_buffer_size", "ring_buffer_capacity",
                    "error_dumps", "file_level", "terminal_level", "max_log_mb"):
            self.assertIn(key, stats, f"Missing key: {key}")

    def test_ring_buffer_size_matches_actual(self):
        _sl._ring_buffer.clear()
        _sl.slog("TEST", "stat check msg")
        stats = _sl.get_log_stats()
        self.assertGreaterEqual(stats["ring_buffer_size"], 1)

    def test_ring_buffer_capacity_is_positive(self):
        stats = _sl.get_log_stats()
        self.assertGreater(stats["ring_buffer_capacity"], 0)


@unittest.skipIf(_SKIP is not None, f"super_log unavailable: {_SKIP}")
class TestLogEventInterceptor(unittest.TestCase):
    def test_intercept_log_event_is_idempotent(self):
        calls = []

        fake_database = types.ModuleType("database")

        def original_log_event(severity, event_type, message, data=None):
            calls.append((severity, event_type, message, data))
            return True

        fake_database.log_event = original_log_event

        direct_modules = [
            "coin_manager", "bot_loop", "offer_manager", "fill_tracker",
            "risk_manager", "price_engine", "market_intel", "sniper",
            "boost_manager", "splash_manager", "dexie_manager",
            "coin_prep_worker", "wallet_sage", "wallet_chia",
        ]
        saved_database = sys.modules.get("database")
        saved_direct_attrs = []
        for name in direct_modules:
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "log_event"):
                saved_direct_attrs.append((module, getattr(module, "log_event")))

        orig_initialized = _sl._initialized
        orig_original_log_event = _sl._original_log_event
        sys.modules["database"] = fake_database
        _sl._initialized = False
        try:
            _sl.intercept_log_event()
            first_wrapper = fake_database.log_event
            _sl.intercept_log_event()

            self.assertIs(fake_database.log_event._super_log_original,
                          original_log_event)
            self.assertIsNot(fake_database.log_event._super_log_original,
                             first_wrapper)

            result = fake_database.log_event("info", "test_event", "message")
            self.assertTrue(result)
            self.assertEqual(calls, [("info", "test_event", "message", None)])
        finally:
            if saved_database is None:
                sys.modules.pop("database", None)
            else:
                sys.modules["database"] = saved_database
            for module, attr in saved_direct_attrs:
                module.log_event = attr
            _sl._initialized = orig_initialized
            _sl._original_log_event = orig_original_log_event


if __name__ == "__main__":
    unittest.main()
