"""Regression test: boost-tier migration must preserve every column added by
later ADD COLUMN migrations.

The original bug: an older ``init_database()`` recreated the offers table
with a hand-coded CREATE listing only SCHEMA_SQL columns when the stored
tier CHECK was missing 'boost'. On a DB that already had post-SCHEMA columns
(``lifecycle_state``, ``offer_bech32``, ``cancel_last_attempt_at``,
``fee_mojos_xch``), those columns were silently dropped during the rebuild
and re-added by subsequent migrations, exposing a window where
``get_open_offers()`` could fail with "no such column: lifecycle_state".

This test simulates a legacy DB (offers table without 'boost' in its tier
CHECK, but with post-SCHEMA columns already present) and asserts that
``init_database()`` ends with all those columns intact AND with 'boost'
accepted by the CHECK constraint.
"""

import importlib
import gc
import os
import sqlite3
import sys
import tempfile
import time
import unittest


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.abspath(os.path.join(_TESTS_DIR, "..", "src", "catalyst"))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _load_database():
    sys.modules.pop("database", None)
    return importlib.import_module("database")


_LEGACY_OFFERS_SQL = """
CREATE TABLE offers (
    trade_id        TEXT PRIMARY KEY,
    side            TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    price_xch       TEXT NOT NULL,
    size_xch        TEXT NOT NULL,
    size_cat        TEXT NOT NULL,
    tier            TEXT DEFAULT 'mid' CHECK(tier IN ('inner', 'mid', 'outer', 'extreme', 'sniper')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'filled', 'cancelled', 'expired')),
    dexie_id        TEXT,
    dexie_posted    INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    filled_at       TEXT,
    cancelled_at    TEXT,
    expires_at      TEXT,
    cat_asset_id    TEXT NOT NULL,
    coin_id         TEXT,
    offer_bech32    TEXT,
    lifecycle_state TEXT DEFAULT 'open',
    cancel_last_attempt_at TEXT,
    fee_mojos_xch   INTEGER NOT NULL DEFAULT 0
)
"""


class BoostMigrationPreservesColumnsTest(unittest.TestCase):
    def setUp(self):
        self.database = _load_database()
        self._orig_db_path = self.database.DB_PATH
        self._orig_init_path = self.database._db_initialized_path
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self._tmp.close()
        self.database.DB_PATH = self._tmp.name
        self.database._db_initialized_path = ""
        self.database.close_connection()

    def tearDown(self):
        self.database.close_connection()
        self.database.DB_PATH = self._orig_db_path
        self.database._db_initialized_path = self._orig_init_path
        # Windows can keep SQLite file handles alive briefly under xdist.
        # Retry so teardown noise doesn't mask the migration regression.
        for path in (self._tmp.name, self._tmp.name + "-wal", self._tmp.name + "-shm"):
            for attempt in range(10):
                try:
                    os.unlink(path)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    gc.collect()
                    time.sleep(0.1 * (attempt + 1))

    def _seed_legacy_offers_table(self):
        """Build a DB with an offers table that has post-SCHEMA columns but
        an old tier CHECK without 'boost'."""
        conn = sqlite3.connect(self._tmp.name)
        try:
            conn.execute(_LEGACY_OFFERS_SQL)
            # A row that exercises every preserved column.
            conn.execute(
                """INSERT INTO offers (
                       trade_id, side, price_xch, size_xch, size_cat, tier,
                       created_at, cat_asset_id, coin_id,
                       offer_bech32, lifecycle_state,
                       cancel_last_attempt_at, fee_mojos_xch
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "trade-A",
                    "buy",
                    "0.1",
                    "1.0",
                    "1000",
                    "mid",
                    "2026-04-27T10:00:00+00:00",
                    "asset-x",
                    "coin-1",
                    "offer1...",
                    "cancel_requested",
                    "2026-04-27T10:05:00+00:00",
                    12345,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_offers_columns_preserved_after_init(self):
        self._seed_legacy_offers_table()

        # Sanity: the seeded table has lifecycle_state but NOT 'boost'
        # in its tier CHECK.
        conn = sqlite3.connect(self._tmp.name)
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='offers'"
            ).fetchone()[0]
            self.assertIn("lifecycle_state", sql)
            self.assertNotIn("'boost'", sql)
        finally:
            conn.close()

        self.database.init_database()
        # Drop the migration's connection so Windows lets us reopen.
        self.database.close_connection()

        conn = sqlite3.connect(self._tmp.name)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(offers)").fetchall()}
            # All post-SCHEMA columns must survive the rebuild.
            for must_exist in (
                "lifecycle_state",
                "offer_bech32",
                "cancel_last_attempt_at",
                "fee_mojos_xch",
            ):
                self.assertIn(
                    must_exist, cols, f"{must_exist} dropped during boost migration"
                )

            # And the seeded row's data for those columns must survive.
            row = conn.execute(
                "SELECT lifecycle_state, offer_bech32, "
                "cancel_last_attempt_at, fee_mojos_xch "
                "FROM offers WHERE trade_id='trade-A'"
            ).fetchone()
            self.assertEqual(row[0], "cancel_requested")
            self.assertEqual(row[1], "offer1...")
            self.assertEqual(row[2], "2026-04-27T10:05:00+00:00")
            self.assertEqual(row[3], 12345)

            # The new tier CHECK must accept 'boost'.
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='offers'"
            ).fetchone()[0]
            self.assertIn("'boost'", sql)
            conn.execute(
                """INSERT INTO offers (
                       trade_id, side, price_xch, size_xch, size_cat, tier,
                       created_at, cat_asset_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "trade-B",
                    "sell",
                    "0.2",
                    "2.0",
                    "2000",
                    "boost",
                    "2026-04-27T10:10:00+00:00",
                    "asset-x",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_get_open_offers_works_after_legacy_init(self):
        """End-to-end: the failure mode that triggered this fix."""
        self._seed_legacy_offers_table()
        self.database.init_database()
        # Must not raise "no such column: lifecycle_state".
        rows = self.database.get_open_offers()
        # Seeded row had lifecycle_state='cancel_requested' so it's
        # excluded by default. We only assert the call succeeded.
        self.assertIsInstance(rows, list)


if __name__ == "__main__":
    unittest.main()
