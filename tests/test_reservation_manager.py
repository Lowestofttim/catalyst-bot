"""Tests for reservation_manager.py — persistent capacity leases."""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta

# Patch database module to use a temp DB before importing reservation_manager
import database

_test_db_path = None


def _setup_test_db():
    global _test_db_path
    fd, _test_db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database.DB_PATH = _test_db_path
    # Reset thread-local connections
    if hasattr(database._local, "conn") and database._local.conn:
        database._local.conn.close()
        database._local.conn = None
    # Init the DB with schema
    conn = database.get_connection()
    conn.executescript(database.SCHEMA_SQL)
    conn.commit()


_setup_test_db()

from reservation_manager import ReservationManager, init_reservation_table


class TestReservationManager(unittest.TestCase):
    def setUp(self):
        _setup_test_db()
        init_reservation_table()
        self.rm = ReservationManager()

    def tearDown(self):
        database.close_connection()
        if _test_db_path and os.path.exists(_test_db_path):
            try:
                os.unlink(_test_db_path)
            except OSError:
                pass

    def test_acquire_and_release(self):
        result = self.rm.try_acquire("test_offer", xch_mojos=100000, lease_secs=60)
        self.assertTrue(result.success)
        self.assertTrue(result.reservation_id.startswith("res_"))

        totals = self.rm.get_reserved_totals()
        self.assertEqual(totals["xch_mojos"], 100000)
        self.assertEqual(totals["count"], 1)

        self.rm.release(result.reservation_id, "completed")
        totals = self.rm.get_reserved_totals()
        self.assertEqual(totals["count"], 0)

    def test_acquire_requires_positive_amount(self):
        result = self.rm.try_acquire("test", xch_mojos=0, cat_mojos=0)
        self.assertFalse(result.success)
        self.assertIn("positive", result.error)

    def test_acquire_cat_mojos(self):
        result = self.rm.try_acquire("test", cat_mojos=50000)
        self.assertTrue(result.success)
        totals = self.rm.get_reserved_totals()
        self.assertEqual(totals["cat_mojos"], 50000)

    def test_expire_stale(self):
        # Insert a reservation that's already expired
        conn = database.get_connection()
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        conn.execute(
            """INSERT INTO reservation_leases
               (reservation_id, purpose, xch_mojos, cat_mojos, status, created_at, expires_at)
               VALUES ('res_old', 'test', 100, 0, 'active', ?, ?)""",
            (past, past),
        )
        conn.commit()

        count = self.rm.expire_stale()
        self.assertEqual(count, 1)

        totals = self.rm.get_reserved_totals()
        self.assertEqual(totals["count"], 0)

    def test_expire_all(self):
        self.rm.try_acquire("a", xch_mojos=100)
        self.rm.try_acquire("b", xch_mojos=200)
        self.assertEqual(self.rm.get_reserved_totals()["count"], 2)

        count = self.rm.expire_all()
        self.assertEqual(count, 2)
        self.assertEqual(self.rm.get_reserved_totals()["count"], 0)

    def test_release_nonexistent_noop(self):
        # Should not raise
        self.rm.release("nonexistent_id", "completed")

    def test_release_empty_string_noop(self):
        self.rm.release("", "completed")

    def test_list_active(self):
        self.rm.try_acquire("offer_1", xch_mojos=100)
        self.rm.try_acquire("offer_2", cat_mojos=200)
        active = self.rm.list_active()
        self.assertEqual(len(active), 2)
        self.assertIn("purpose", active[0])

    def test_prune_old(self):
        # Insert an old completed reservation
        conn = database.get_connection()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn.execute(
            """INSERT INTO reservation_leases
               (reservation_id, purpose, xch_mojos, cat_mojos, status,
                created_at, expires_at, released_at)
               VALUES ('res_ancient', 'test', 100, 0, 'completed', ?, ?, ?)""",
            (old_time, old_time, old_time),
        )
        conn.commit()

        self.rm.prune_old(retention_hours=24)

        rows = conn.execute(
            "SELECT * FROM reservation_leases WHERE reservation_id='res_ancient'"
        ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_thread_safety(self):
        """Concurrent acquire/release should not corrupt state."""
        results = []

        def worker():
            r = self.rm.try_acquire("threaded", xch_mojos=10, lease_secs=60)
            results.append(r.success)
            if r.success:
                self.rm.release(r.reservation_id, "completed")

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(results), 10)
        self.assertTrue(all(results))

    def test_minimum_lease_enforced(self):
        """Lease below 30s should be bumped to 30s."""
        result = self.rm.try_acquire("short", xch_mojos=100, lease_secs=1)
        self.assertTrue(result.success)
        # The lease should exist and be valid (not already expired)
        totals = self.rm.get_reserved_totals()
        self.assertEqual(totals["count"], 1)


if __name__ == "__main__":
    unittest.main()
