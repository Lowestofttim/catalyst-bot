"""Slice 02-26 — sniper.py unit tests.

Covers: _bps_to_pct (pure), Sniper.prune_active_snipes, Sniper.get_stats,
and Sniper._calculate_snipe_size (cfg-patched).
No offer creation or network calls are made.
"""

import threading
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

try:
    import sniper as _sniper_mod
    from sniper import _bps_to_pct, Sniper

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

_SKIP_MSG = f"sniper unavailable: {_SKIP}"


# ---------------------------------------------------------------------------
# _bps_to_pct
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestBpsToPct(unittest.TestCase):
    def test_small_value_two_decimal_places(self):
        # 30 bps = 0.30% → "0.30%"
        self.assertEqual(_bps_to_pct(30), "0.30%")

    def test_exactly_100_bps_is_one_pct(self):
        # 100 bps = 1.0% — one decimal because n >= 1
        self.assertEqual(_bps_to_pct(100), "1.0%")

    def test_value_below_100_bps_two_decimals(self):
        # 50 bps = 0.50%
        self.assertEqual(_bps_to_pct(50), "0.50%")

    def test_large_value_one_decimal(self):
        # 1000 bps = 10.0%
        self.assertEqual(_bps_to_pct(1000), "10.0%")

    def test_string_input(self):
        self.assertEqual(_bps_to_pct("30"), "0.30%")

    def test_invalid_input_returns_str(self):
        self.assertEqual(_bps_to_pct("bad"), "bad")

    def test_none_returns_str(self):
        self.assertEqual(_bps_to_pct(None), "None")


# ---------------------------------------------------------------------------
# Sniper.prune_active_snipes
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestPruneActiveSnipes(unittest.TestCase):
    def _make_sniper(self, active_ids=None, active_sides=None):
        s = Sniper()
        s._active_snipe_ids = list(active_ids or [])
        s._active_snipe_sides = dict(active_sides or {})
        return s

    def test_empty_ids_no_change(self):
        s = self._make_sniper()
        s.prune_active_snipes({"tid1"})
        self.assertEqual(s._active_snipe_ids, [])

    def test_all_still_open(self):
        s = self._make_sniper(["tid1", "tid2"])
        s.prune_active_snipes({"tid1", "tid2"})
        self.assertEqual(sorted(s._active_snipe_ids), ["tid1", "tid2"])

    def test_some_closed(self):
        s = self._make_sniper(["tid1", "tid2", "tid3"])
        s.prune_active_snipes({"tid1"})
        self.assertEqual(s._active_snipe_ids, ["tid1"])

    def test_all_closed(self):
        s = self._make_sniper(["tid1", "tid2"])
        s.prune_active_snipes(set())
        self.assertEqual(s._active_snipe_ids, [])

    def test_side_tracking_pruned_too(self):
        s = self._make_sniper(["tid1", "tid2"], {"tid1": "buy", "tid2": "sell"})
        s.prune_active_snipes({"tid1"})
        self.assertIn("tid1", s._active_snipe_sides)
        self.assertNotIn("tid2", s._active_snipe_sides)


# ---------------------------------------------------------------------------
# Sniper.get_stats
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestGetStats(unittest.TestCase):
    def test_returns_expected_keys(self):
        s = Sniper()
        stats = s.get_stats()
        for key in (
            "total_snipes",
            "total_skipped",
            "active_snipes",
            "max_active_snipes",
            "last_snipe_time",
            "recent_snipes",
        ):
            self.assertIn(key, stats)

    def test_initial_counts_zero(self):
        s = Sniper()
        stats = s.get_stats()
        self.assertEqual(stats["total_snipes"], 0)
        self.assertEqual(stats["total_skipped"], 0)
        self.assertEqual(stats["active_snipes"], 0)

    def test_active_snipes_reflects_list(self):
        s = Sniper()
        s._active_snipe_ids = ["tid1", "tid2"]
        self.assertEqual(s.get_stats()["active_snipes"], 2)

    def test_thread_safe_snapshot(self):
        """get_stats must not raise under concurrent reads."""
        s = Sniper()
        errors = []

        def reader():
            try:
                for _ in range(50):
                    s.get_stats()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# Sniper._calculate_snipe_size
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, _SKIP_MSG)
class TestCalculateSnipeSize(unittest.TestCase):
    def _sniper_with_cfg(
        self, sniper_size=None, default=Decimal("0.01"), max_trade=Decimal("1.0")
    ):
        fake_cfg = SimpleNamespace(
            SNIPER_SIZE_XCH=sniper_size,
            DEFAULT_TRADE_XCH=default,
            MAX_TRADE_XCH=max_trade,
        )
        s = Sniper()
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            result = s._calculate_snipe_size(Decimal("100"))
        return result

    def test_uses_sniper_size_when_set(self):
        result = self._sniper_with_cfg(sniper_size="0.005", max_trade=Decimal("1.0"))
        self.assertEqual(result, Decimal("0.005"))

    def test_falls_back_to_default_when_none(self):
        result = self._sniper_with_cfg(sniper_size=None, default=Decimal("0.01"))
        self.assertEqual(result, Decimal("0.01"))

    def test_capped_by_max_trade(self):
        result = self._sniper_with_cfg(sniper_size="5.0", max_trade=Decimal("1.0"))
        self.assertEqual(result, Decimal("1.0"))

    def test_arb_gap_ignored(self):
        # arb_gap_bps is immediately deleted — size is always config-driven
        s = Sniper()
        fake_cfg = SimpleNamespace(
            SNIPER_SIZE_XCH="0.003",
            DEFAULT_TRADE_XCH=Decimal("0.01"),
            MAX_TRADE_XCH=Decimal("1.0"),
        )
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            r1 = s._calculate_snipe_size(Decimal("10"))
            r2 = s._calculate_snipe_size(Decimal("5000"))
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
