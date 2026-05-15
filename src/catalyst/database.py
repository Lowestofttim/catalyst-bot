"""Single source of truth for all persistent trading state

Owns the SQLite database that backs every piece of bot state the user cares
about surviving a restart: offers, fills, coins, inventory, price history,
events, config history, bot settings, splash incoming offers, pool snapshots,
market analysis cache, and trading pace. Connections are thread-local, WAL
mode is enabled for concurrent reads, and every query goes through
parameterised helpers so no other module needs to write raw SQL.

Key responsibilities:
    - Initialise schema and indexes; migrate older databases forward
    - Provide typed CRUD helpers for every table
    - Normalise coin IDs between Sage (no `0x`) and Chia / DB (`0x` prefix)
    - Delegate `reservation_leases` table creation to `reservation_manager`

All DB access in the codebase should route through this module; importing
`sqlite3` elsewhere is a smell.
"""

import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict


# ---------------------------------------------------------------------------
# Coin ID normalization — Sage returns without 0x, DB stores with 0x
# ---------------------------------------------------------------------------

def norm_coin_id(cid: str) -> str:
    """Normalize a coin ID to consistent format: lowercase with 0x prefix.

    Sage wallet returns coin IDs without 0x prefix (e.g., "02b56d64...").
    Chia wallet and our DB use 0x prefix (e.g., "0x02b56d64...").
    This ensures all comparisons work regardless of source.
    """
    if not cid:
        return ""
    cid = cid.strip().lower()
    if not cid.startswith("0x"):
        cid = "0x" + cid
    return cid


# ---------------------------------------------------------------------------
# Database path — lives under the per-user data directory so the app works
# when installed to a read-only location (e.g. C:\Program Files).
# user_paths.py handles first-launch migration of legacy dev-layout DBs.
# ---------------------------------------------------------------------------
try:
    from user_paths import database_file as _db_file
    DB_PATH = _db_file()
except Exception as _e:
    # Fallback for unusual dev setups.  In a packaged build this should
    # never execute because user_paths.py is bundled alongside database.py.
    print(f"[database] user_paths unavailable ({_e}); falling back to install dir", flush=True)
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")


# ---------------------------------------------------------------------------
# Thread-local connections (SQLite connections can't be shared across threads)
# ---------------------------------------------------------------------------
_local = threading.local()

# Guard that prevents init_database() from running migrations more than once
# per process for the same database file.  Werkzeug's reloader and
# multi-threaded Flask startup can import modules multiple times, causing the
# full migration sequence to spam the log.  Tests that swap DB_PATH for a temp
# file bypass the guard automatically because the path differs.
_db_initialized_path: str = ""
_db_init_lock = threading.Lock()


def _missing_splash_table(exc: Exception) -> bool:
    """Return True when an older/test DB has no Splash incoming table."""
    return (
        isinstance(exc, sqlite3.OperationalError)
        and "no such table: splash_incoming_offers" in str(exc).lower()
    )


def get_connection() -> sqlite3.Connection:
    """Get a thread-local database connection.

    Each thread gets its own connection because SQLite connections
    aren't safe to share across threads. The connection is reused
    within the same thread for efficiency.
    """
    if not hasattr(_local, "conn") or _local.conn is None:
        _new_db = not os.path.exists(DB_PATH)
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        if _new_db:
            # Restrict database file to owner-only access
            try:
                import stat
                os.chmod(DB_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            except OSError:
                pass  # Windows may not support POSIX permissions fully
        _local.conn.row_factory = sqlite3.Row  # Return rows as dict-like objects
        _local.conn.execute("PRAGMA journal_mode=WAL")  # Safe concurrent reads
        _local.conn.execute("PRAGMA foreign_keys=ON")    # Enforce relationships
        _local.conn.execute("PRAGMA busy_timeout=5000")  # Wait up to 5s if locked
        # synchronous=NORMAL is the SQLite-recommended setting for WAL mode:
        # crash-safe (durable on commit at the next checkpoint) but ~2x faster
        # than FULL because it skips an fsync on every COMMIT. The default of
        # FULL with WAL has no extra durability over NORMAL — only extra IO.
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        # Explicit autocheckpoint matches SQLite's default but documents the
        # threshold. The 26-04 incident showed the WAL grew unbounded past
        # this; the periodic checkpoint_wal() call from bot_loop is the
        # belt-and-braces backstop when readers stall the auto path.
        _local.conn.execute("PRAGMA wal_autocheckpoint=1000")
        # Super log: trace all SQL on this connection
        try:
            from super_log import trace_connection
            trace_connection(_local.conn, threading.current_thread().name)
        except ImportError:
            pass
    return _local.conn


def close_connection():
    """Close the thread-local connection. Call on thread shutdown."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None


def checkpoint_wal(mode: str = "TRUNCATE") -> Dict[str, int]:
    """Force a WAL checkpoint and (optionally) truncate the WAL file.

    The 26-04 corruption episode showed the WAL growing unbounded to >4MB
    while the main DB stayed at 663KB. SQLite's autocheckpoint can stall
    when a long-lived reader connection holds an old snapshot — every
    auto-trigger gives up because it can't reclaim WAL frames. Calling
    this from bot_loop on a fixed cadence with a fresh connection forces
    progress regardless of what the thread-local connections are doing.

    Uses a dedicated connection (not the thread-local cache) so that any
    open read transaction on the caller's connection doesn't block the
    checkpoint from completing.

    Mode:
        - PASSIVE: best-effort, never blocks. Returns even if it can't
          fully checkpoint due to active readers.
        - FULL:   waits for all readers to finish before checkpointing.
        - RESTART: like FULL but switches the WAL to a fresh segment.
        - TRUNCATE (default): like RESTART, then truncates WAL to zero.

    Returns ``{"busy": int, "log_pages": int, "checkpointed": int}``.
    On error returns ``{}``; safe to ignore — caller treats as non-fatal.
    """
    mode_upper = (mode or "TRUNCATE").upper()
    if mode_upper not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
        mode_upper = "TRUNCATE"
    try:
        # Fresh connection. busy_timeout matters here — checkpoint can wait
        # briefly for in-flight writes to commit.
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute(
                f"PRAGMA wal_checkpoint({mode_upper})"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    # row = (busy, log, checkpointed) per SQLite docs
    return {
        "busy": int(row[0]) if row[0] is not None else 0,
        "log_pages": int(row[1]) if row[1] is not None else 0,
        "checkpointed": int(row[2]) if row[2] is not None else 0,
        "mode": mode_upper,
    }


def check_db_integrity() -> Dict[str, object]:
    """Run ``PRAGMA integrity_check`` on the main DB.

    Returns a dict with::

        {"ok": bool, "result": "ok" | "<error1>;<error2>;...", "errors": [str]}

    SQLite's integrity_check returns the literal string "ok" when the file
    is healthy, or one or more textual error rows when it isn't. We run on
    a fresh connection so we surface the on-disk state, not whatever
    in-memory page cache the caller's thread-local connection might be
    holding.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "result": f"check_failed: {e}", "errors": [str(e)]}

    messages = [str(r[0]) for r in rows if r and r[0] is not None]
    healthy = (len(messages) == 1 and messages[0].strip().lower() == "ok")
    return {
        "ok": healthy,
        "result": "ok" if healthy else "; ".join(messages),
        "errors": [] if healthy else messages,
    }


def attempt_db_recovery() -> Dict[str, object]:
    """If ``bot.db`` is corrupt, salvage what's readable and swap in a clean
    file. Mirrors ``scripts/recover_db.py`` for use during desktop_app
    startup so users don't have to run a separate script.

    Returns a result dict with::

        {"action": "ok" | "recovered" | "failed",
         "result": <integrity message>,
         "skipped_statements": int,           # only if recovered
         "corrupt_backup": "<filename>",      # only if recovered
         "error": "<reason>"}                 # only if failed

    SAFETY — caller must guarantee no SQLite connection is open against
    ``bot.db`` when this is invoked (the swap rename will fail on Windows
    if any handle is alive). The right place to call this is from the
    desktop_app entrypoint, after the singleton lock is acquired and
    before ``init_database()`` is called.
    """
    import datetime as _dt
    import shutil as _sh
    from pathlib import Path as _P

    db = _P(DB_PATH)
    if not db.exists():
        # Nothing to check. init_database() will create a fresh file.
        return {"action": "ok", "result": "no_db_file"}

    check = check_db_integrity()
    if check.get("ok"):
        return {"action": "ok", "result": "ok"}

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    corrupt_backup = db.with_name(f"bot.db.corrupt_{stamp}")
    recovered = db.with_name("bot.db.recovered")
    if recovered.exists():
        try:
            recovered.unlink()
        except Exception:
            pass

    # Step 1: forensic backup of the corrupt original (DB + WAL + SHM)
    try:
        _sh.copy2(db, corrupt_backup)
        for suffix in ("-wal", "-shm"):
            side = db.with_suffix(db.suffix + suffix)
            if side.exists():
                try:
                    _sh.copy2(side, corrupt_backup.with_suffix(corrupt_backup.suffix + suffix))
                except Exception:
                    pass
    except Exception as e:
        return {"action": "failed", "result": str(check.get("result")),
                "error": f"backup_failed: {e}"}

    # Step 2: dump readable rows into a fresh DB. Try iterdump() first —
    # it preserves data when corruption is mild. If iterdump itself
    # explodes (the page that holds the schema is the corrupt one), fall
    # back to renaming the corrupt file aside and letting init_database()
    # create a fresh one. Better to lose some history than to leave the
    # user unable to start the app.
    skipped = 0
    iterdump_ok = False
    try:
        src = sqlite3.connect(str(db), timeout=10)
        dst = sqlite3.connect(str(recovered), timeout=10)
        try:
            with dst:
                for stmt in src.iterdump():
                    try:
                        dst.execute(stmt)
                    except Exception:
                        skipped += 1
            iterdump_ok = True
        finally:
            src.close()
            dst.close()
    except Exception:
        # iterdump itself failed (severe corruption). Drop the partial
        # recovered file so the fresh-start fallback below gets a clean
        # path to swap into.
        iterdump_ok = False
        try:
            recovered.unlink()
        except Exception:
            pass

    if not iterdump_ok:
        # Fresh-start fallback. The corrupt original is already saved as
        # corrupt_backup, so no data is lost — it just isn't auto-merged.
        # Remove the live files so init_database can build a clean DB.
        try:
            if db.exists():
                db.unlink()
            for suffix in ("-wal", "-shm"):
                side = db.with_suffix(db.suffix + suffix)
                if side.exists():
                    try:
                        side.unlink()
                    except Exception:
                        pass
        except Exception as e:
            return {"action": "failed", "result": str(check.get("result")),
                    "error": f"fresh_start_unlink_failed: {e}"}
        return {
            "action": "recovered",
            "result": str(check.get("result")),
            "skipped_statements": -1,  # signals fresh-start, no rows kept
            "corrupt_backup": corrupt_backup.name,
            "fallback": "fresh_start",
        }

    # Step 3: verify the recovered file passes integrity_check before swap
    try:
        v = sqlite3.connect(str(recovered), timeout=5)
        try:
            rows = v.execute("PRAGMA integrity_check").fetchall()
        finally:
            v.close()
    except Exception as e:
        try:
            recovered.unlink()
        except Exception:
            pass
        return {"action": "failed", "result": str(check.get("result")),
                "error": f"verify_failed: {e}"}
    msgs = [str(r[0]) for r in rows if r and r[0] is not None]
    if not (len(msgs) == 1 and msgs[0].strip().lower() == "ok"):
        try:
            recovered.unlink()
        except Exception:
            pass
        return {"action": "failed", "result": str(check.get("result")),
                "error": f"recovered_db_still_fails: {'; '.join(msgs[:3])}"}

    # Step 4: atomically swap. Remove the WAL/SHM that belong to the old
    # main DB so the recovered file owns its own WAL on first open.
    try:
        db.unlink()
        for suffix in ("-wal", "-shm"):
            side = db.with_suffix(db.suffix + suffix)
            if side.exists():
                try:
                    side.unlink()
                except Exception:
                    pass
        recovered.rename(db)
    except Exception as e:
        return {"action": "failed", "result": str(check.get("result")),
                "error": f"swap_failed: {e}"}

    return {
        "action": "recovered",
        "result": str(check.get("result")),
        "skipped_statements": int(skipped),
        "corrupt_backup": corrupt_backup.name,
    }


# ---------------------------------------------------------------------------
# Schema — all tables defined here
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- Offers table: every offer the bot creates
-- trade_id is the universal key (lesson from V1: never key by dexie_id)
CREATE TABLE IF NOT EXISTS offers (
    trade_id        TEXT PRIMARY KEY,
    side            TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    price_xch       TEXT NOT NULL,
    size_xch        TEXT NOT NULL,
    size_cat        TEXT NOT NULL,
    tier            TEXT DEFAULT 'mid' CHECK(tier IN ('inner', 'mid', 'outer', 'extreme', 'sniper', 'boost')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'filled', 'cancelled', 'expired')),
    dexie_id        TEXT,
    dexie_posted    INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    filled_at       TEXT,
    cancelled_at    TEXT,
    expires_at      TEXT,
    cat_asset_id    TEXT NOT NULL,
    coin_id         TEXT
);

-- Fills table: every detected fill
CREATE TABLE IF NOT EXISTS fills (
    fill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    price_xch       TEXT NOT NULL,
    size_xch        TEXT NOT NULL,
    size_cat        TEXT NOT NULL,
    filled_at       TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'legacy',
    round_trip_id   INTEGER,
    pnl_xch         TEXT,
    cat_asset_id    TEXT NOT NULL
);

-- Inventory snapshots: track net position over time
CREATE TABLE IF NOT EXISTS inventory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    cat_asset_id    TEXT NOT NULL,
    net_position    TEXT NOT NULL,
    xch_balance     TEXT,
    cat_balance     TEXT,
    mid_price       TEXT
);

-- Price history: for volatility calculation and backtesting
CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    cat_asset_id    TEXT NOT NULL,
    dexie_price     TEXT,
    tibet_price     TEXT,
    combined_price  TEXT NOT NULL,
    strategy_used   TEXT
);

-- Events log: replaces add_log() scattered logging
-- The GUI log panel reads from this instead of an in-memory list
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info' CHECK(severity IN ('info', 'success', 'warning', 'error')),
    message         TEXT NOT NULL,
    data            TEXT
);

-- Config change history: track when settings change
-- F26 (2026-04-08): re-introduced as a write+read audit trail for live
-- config changes. Each row records WHO/WHAT/WHEN — useful for
-- post-mortem investigation when something breaks after a settings
-- change. The 'source' column distinguishes GUI/API/restart paths.
CREATE TABLE IF NOT EXISTS config_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    key             TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    source          TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_history_time ON config_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_config_history_key ON config_history(key);

-- Coins table: tracks every coin the bot knows about
-- Persistent record of what's available, locked, or spent
CREATE TABLE IF NOT EXISTS coins (
    coin_id         TEXT PRIMARY KEY,
    wallet_type     TEXT NOT NULL CHECK(wallet_type IN ('xch', 'cat')),
    amount_mojos    INTEGER NOT NULL,
    tier            TEXT,
    status          TEXT NOT NULL DEFAULT 'free'
                    CHECK(status IN ('free', 'locked', 'spent', 'gone')),
    trade_id        TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_side ON offers(side);
CREATE INDEX IF NOT EXISTS idx_offers_cat ON offers(cat_asset_id);
CREATE INDEX IF NOT EXISTS idx_fills_trade_id ON fills(trade_id);
CREATE INDEX IF NOT EXISTS idx_fills_side ON fills(side);
CREATE INDEX IF NOT EXISTS idx_fills_cat ON fills(cat_asset_id);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(filled_at);
CREATE INDEX IF NOT EXISTS idx_fills_roundtrip ON fills(round_trip_id);
CREATE INDEX IF NOT EXISTS idx_inventory_cat ON inventory(cat_asset_id);
CREATE INDEX IF NOT EXISTS idx_price_history_cat ON price_history(cat_asset_id);
CREATE INDEX IF NOT EXISTS idx_price_history_time ON price_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_coins_status ON coins(status);
CREATE INDEX IF NOT EXISTS idx_coins_wallet ON coins(wallet_type);
CREATE INDEX IF NOT EXISTS idx_coins_trade ON coins(trade_id);

-- Simple key-value settings table (persists across restarts)
CREATE TABLE IF NOT EXISTS bot_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- V3: Splash incoming offers — received from the P2P network
-- Used for future sniper integration (detect arb offers from other makers)
CREATE TABLE IF NOT EXISTS splash_incoming_offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_bech32    TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK(status IN ('new', 'processed', 'ignored', 'expired')),
    pair_hint       TEXT,
    source_ip       TEXT
);

CREATE INDEX IF NOT EXISTS idx_splash_incoming_status ON splash_incoming_offers(status);
CREATE INDEX IF NOT EXISTS idx_splash_incoming_time ON splash_incoming_offers(received_at);
CREATE INDEX IF NOT EXISTS idx_splash_incoming_fp ON splash_incoming_offers(fingerprint);

-- Smart Defaults v2: Pool depth snapshots (build history over time)
-- Stored every bot loop cycle so we can track pool growth/shrinkage
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        TEXT NOT NULL,
    xch_reserve     REAL NOT NULL,
    cat_reserve     REAL NOT NULL,
    price           REAL NOT NULL,
    timestamp       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pool_snap_asset ON pool_snapshots(asset_id);
CREATE INDEX IF NOT EXISTS idx_pool_snap_time ON pool_snapshots(timestamp);

-- Smart Defaults v2: Market analysis cache (avoid re-fetching on every click)
CREATE TABLE IF NOT EXISTS market_analysis_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        TEXT NOT NULL,
    analysis_type   TEXT NOT NULL,
    data_json       TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_market_cache_asset ON market_analysis_cache(asset_id);
CREATE INDEX IF NOT EXISTS idx_market_cache_type ON market_analysis_cache(analysis_type);
"""


def init_database():
    """Create all tables and indexes if they don't exist.

    Safe to call multiple times — uses CREATE IF NOT EXISTS.
    After the first call in a process the function returns immediately so
    repeated imports (Werkzeug reloader, multi-threaded Flask startup, etc.)
    don't replay the full migration sequence and spam the log.
    """
    global _db_initialized_path
    with _db_init_lock:
        if _db_initialized_path == DB_PATH:
            return
        _db_initialized_path = DB_PATH
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)

    # The boost tier CHECK migration runs AFTER all ADD COLUMN migrations
    # (see _migrate_offers_tier_check_for_boost below). Older revisions of
    # this code recreated the offers table here with a hand-coded CREATE
    # listing only SCHEMA_SQL columns, which silently dropped post-SCHEMA
    # columns (lifecycle_state, offer_bech32, fee_mojos_xch, etc.) on already-
    # migrated DBs and exposed a window where get_open_offers() could fail
    # with "no such column: lifecycle_state".

    conn.commit()

    # Migration: add coin_id column to offers table if it doesn't exist.
    # This tracks which specific coin was locked by each offer.
    try:
        conn.execute("SELECT coin_id FROM offers LIMIT 1")
    except sqlite3.OperationalError:
        # Column doesn't exist — add it
        conn.execute("ALTER TABLE offers ADD COLUMN coin_id TEXT")
        conn.commit()
        log_event("info", "db_migration", "Migrated offers table: added 'coin_id' column")

    # Migration: ensure coins table exists (for databases created before coin tracking)
    try:
        conn.execute("SELECT coin_id FROM coins LIMIT 1")
    except sqlite3.OperationalError:
        # Table doesn't exist — the SCHEMA_SQL CREATE IF NOT EXISTS should handle it,
        # but run it explicitly just in case
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS coins (
                coin_id         TEXT PRIMARY KEY,
                wallet_type     TEXT NOT NULL CHECK(wallet_type IN ('xch', 'cat')),
                amount_mojos    INTEGER NOT NULL,
                tier            TEXT,
                status          TEXT NOT NULL DEFAULT 'free'
                                CHECK(status IN ('free', 'locked', 'spent', 'gone')),
                trade_id        TEXT,
                first_seen      TEXT NOT NULL,
                last_seen       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_coins_status ON coins(status);
            CREATE INDEX IF NOT EXISTS idx_coins_wallet ON coins(wallet_type);
            CREATE INDEX IF NOT EXISTS idx_coins_trade ON coins(trade_id);
        """)
        conn.commit()
        log_event("info", "db_migration", "Created coins table for comprehensive coin tracking")

    # Migration: add designation and assigned_tier columns to coins table
    # These replace amount-based classification with explicit role tracking
    try:
        conn.execute("SELECT designation FROM coins LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE coins ADD COLUMN designation TEXT DEFAULT 'unknown'")
        conn.commit()
        log_event("info", "db_migration", "Added 'designation' column to coins table")

    try:
        conn.execute("SELECT assigned_tier FROM coins LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE coins ADD COLUMN assigned_tier TEXT DEFAULT 'none'")
        conn.commit()
        log_event("info", "db_migration", "Added 'assigned_tier' column to coins table")

    # Migration: create trading_pace table for adaptive replenishment
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trading_pace (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            fills_last_hour INTEGER DEFAULT 0,
            pace_level      TEXT DEFAULT 'normal'
                            CHECK(pace_level IN ('slow', 'normal', 'busy')),
            active_offers   INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_pace_ts ON trading_pace(timestamp);
    """)

    conn.commit()

    # Migration: add tier column to fills table for smart round-trip matching.
    # Without tier, FIFO matching pairs sniper buys with tiered sells (wrong).
    try:
        conn.execute("SELECT tier FROM fills LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE fills ADD COLUMN tier TEXT DEFAULT 'unknown'")
        conn.commit()
        # Backfill tier from offers table for existing fills
        try:
            conn.execute("""
                UPDATE fills SET tier = (
                    SELECT o.tier FROM offers o WHERE o.trade_id = fills.trade_id
                ) WHERE tier = 'unknown' OR tier IS NULL
            """)
            conn.commit()
            log_event("info", "db_migration",
                      "Migrated fills table: added 'tier' column and backfilled from offers")
        except Exception as backfill_e:
            log_event("warning", "db_migration",
                      "Added 'tier' column to fills but backfill failed: %s" % backfill_e)

    # Migration: mark pre-verification-era fills as legacy so GUI/PnL can
    # exclude them by default. New fills are inserted as verification_status='verified'.
    try:
        conn.execute("SELECT verification_status FROM fills LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE fills ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'legacy'")
        conn.commit()
        log_event("info", "db_migration",
                  "Added 'verification_status' column to fills table (existing rows marked legacy)")

    # Migration: clear bad round-trip matches where sizes don't match.
    # FIFO matching was pairing 0.2 XCH sniper buys with 0.9+ XCH tiered sells.
    try:
        bad_matches = conn.execute("""
            SELECT b.fill_id as buy_id, s.fill_id as sell_id, b.round_trip_id,
                   b.size_xch as buy_size, s.size_xch as sell_size
            FROM fills b
            JOIN fills s ON b.round_trip_id = s.round_trip_id AND b.fill_id != s.fill_id
            WHERE b.side = 'buy' AND s.side = 'sell'
              AND b.round_trip_id IS NOT NULL
              AND ABS(CAST(b.size_xch AS REAL) - CAST(s.size_xch AS REAL)) > 0.01
        """).fetchall()
        if bad_matches:
            # Clear the bad matches so they can be re-matched correctly
            rt_ids = set(r['round_trip_id'] for r in bad_matches)
            for rt_id in rt_ids:
                conn.execute(
                    "UPDATE fills SET round_trip_id = NULL, pnl_xch = NULL WHERE round_trip_id = ?",
                    (rt_id,))
            conn.commit()
            log_event("info", "db_migration",
                      "Cleared %d bad round-trip matches (size mismatch)" % len(rt_ids))
    except Exception as fix_e:
        log_event("warning", "db_migration",
                  "Failed to check/fix bad round-trip matches: %s" % fix_e)

    # Migration: add offer_bech32 column to offers table.
    # Stores the bech32 offer string so we can repost to Dexie on startup
    # without calling wallet RPC for each offer (saves ~200s on startup).
    try:
        conn.execute("SELECT offer_bech32 FROM offers LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE offers ADD COLUMN offer_bech32 TEXT")
        conn.commit()
        log_event("info", "db_migration",
                  "Migrated offers table: added 'offer_bech32' column for fast Dexie repost")

    conn.commit()

    # Migration: add lifecycle_state column to offers table.
    # Extended offer lifecycle (open, refresh_due, cancel_requested, etc.)
    # alongside the existing 4-value status column for backward compatibility.
    try:
        conn.execute("SELECT lifecycle_state FROM offers LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE offers ADD COLUMN lifecycle_state TEXT DEFAULT 'open'")
        # Backfill: set lifecycle_state from status for existing offers
        conn.execute("UPDATE offers SET lifecycle_state = status WHERE lifecycle_state IS NULL OR lifecycle_state = 'open'")
        conn.commit()
        log_event("info", "db_migration",
                  "Migrated offers table: added 'lifecycle_state' column for extended offer lifecycle")

    # Migration: add cancel_last_attempt_at column to offers table.
    # Used by bot_health.check_pending_cancels() to throttle retries when
    # a cancel TX hasn't confirmed yet (e.g. fee=0 bulk cancel sat in
    # mempool and got displaced). Lets the verifier wait N minutes between
    # retries instead of hammering Sage every cycle.
    try:
        conn.execute("SELECT cancel_last_attempt_at FROM offers LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE offers ADD COLUMN cancel_last_attempt_at TEXT")
        conn.commit()
        log_event("info", "db_migration",
                  "Migrated offers table: added 'cancel_last_attempt_at' column for cancel-retry throttling")

    # Migration: add event_category column to events table.
    # Canonical event categories for filtering and routing.
    try:
        conn.execute("SELECT event_category FROM events LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE events ADD COLUMN event_category TEXT DEFAULT 'system'")
        conn.commit()
        log_event("info", "db_migration",
                  "Migrated events table: added 'event_category' column for event taxonomy")

    # Migration: create reservation_leases table for capacity reservations.
    try:
        from reservation_manager import init_reservation_table
        init_reservation_table()
    except Exception as res_e:
        try:
            log_event("warning", "db_migration",
                      "Failed to create reservation_leases table: %s" % res_e)
        except Exception:
            pass

    # Migration: add fill classification columns to fills table.
    # fill_classification: RETAIL | ARB_SWEEP_BUY | ARB_SWEEP_SELL |
    #                      DEXIE_COMBINED | UNKNOWN
    # taker_puzzle_hash: hex puzzle hash of the wallet that took the offer
    # spent_block_index: block height when the offer coin was spent
    # sweep_group_id: groups fills from the same atomic sweep transaction
    for _col, _defn in [
        ("fill_classification", "TEXT DEFAULT 'unknown'"),
        ("taker_puzzle_hash",   "TEXT"),
        ("spent_block_index",   "INTEGER"),
        ("sweep_group_id",      "TEXT"),
    ]:
        try:
            conn.execute(f"SELECT {_col} FROM fills LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE fills ADD COLUMN {_col} {_defn}")
            conn.commit()
            log_event("info", "db_migration",
                      f"Migrated fills table: added '{_col}' column for fill classification")

    # Migration: add fee_mojos_xch column to fills table.
    # Records the transaction fee paid when the offer was created.
    # Used to deduct fees from round-trip PnL calculations.
    try:
        conn.execute("SELECT fee_mojos_xch FROM fills LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE fills ADD COLUMN fee_mojos_xch INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        log_event("info", "db_migration",
                  "Migrated fills table: added 'fee_mojos_xch' column for fee-aware PnL")

    # F43 (2026-04-08): persist post-fill enrichment data to the fills
    # table. Previously the F41/F42 enrichment was logged-only — now it
    # writes structured columns so the data is queryable forever.
    # All four columns are nullable so historical fills are unaffected.
    _enrichment_cols = [
        ("spent_block_height", "INTEGER"),
        ("header_hash",        "TEXT"),
        ("receive_coin_id",    "TEXT"),
        ("receive_amount_mojos", "INTEGER"),
    ]
    for _col, _defn in _enrichment_cols:
        try:
            conn.execute(f"SELECT {_col} FROM fills LIMIT 1")
        except sqlite3.OperationalError:
            try:
                conn.execute(f"ALTER TABLE fills ADD COLUMN {_col} {_defn}")
                conn.commit()
                log_event("info", "db_migration",
                          f"Migrated fills table: added '{_col}' column for "
                          f"post-fill block enrichment (F43)")
            except Exception as _mig_err:
                log_event("warning", "db_migration",
                          f"Could not add fills.{_col}: {_mig_err}")

    # Migration: add fee_mojos_xch column to offers table.
    # Persists the exact fee attached to the offer at creation time.
    # Previously, fill recording used the current config fee (which can change)
    # or hardcoded 0 during repair backfills. Now fills read the offer's stored fee.
    try:
        conn.execute("SELECT fee_mojos_xch FROM offers LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE offers ADD COLUMN fee_mojos_xch INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        log_event("info", "db_migration",
                  "Migrated offers table: added 'fee_mojos_xch' column for per-offer fee tracking")

    # F5 fix (2026-04-08): add UNIQUE index on fills.trade_id to prevent
    # double-counting fills if detect_fills() ever fires twice for the same
    # offer (e.g., race during a wallet sync glitch). The index is created
    # idempotently and is a no-op if it already exists. If existing duplicate
    # rows are present we log a warning and skip — operator can clean up
    # manually rather than blocking startup.
    try:
        # First check whether the unique index is already in place (avoids
        # the duplicate-row scan on every startup)
        existing = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='uniq_fills_trade_id'"
        ).fetchone()
        if existing is None:
            # Detect any existing duplicates before creating the constraint
            dupes = conn.execute(
                "SELECT trade_id, COUNT(*) AS n FROM fills "
                "GROUP BY trade_id HAVING n > 1 LIMIT 5"
            ).fetchall()
            if dupes:
                dupe_summary = ", ".join(
                    f"{row['trade_id'][:16]}...×{row['n']}" for row in dupes
                )
                log_event("warning", "db_migration",
                          f"Cannot add UNIQUE index uniq_fills_trade_id — "
                          f"duplicate trade_ids already exist in fills "
                          f"({len(dupes)}+ groups: {dupe_summary}). "
                          f"Manual cleanup required. New fills are NOT "
                          f"protected from double-counting until resolved.")
            else:
                conn.execute(
                    "CREATE UNIQUE INDEX uniq_fills_trade_id "
                    "ON fills(trade_id)"
                )
                conn.commit()
                log_event("info", "db_migration",
                          "Added UNIQUE index uniq_fills_trade_id on fills "
                          "to prevent double-counted fills")
    except Exception as uniq_err:
        log_event("warning", "db_migration",
                  f"Failed to add UNIQUE index on fills.trade_id: {uniq_err}")

    # F26 (2026-04-08): config_history audit table — add source/note columns
    # if upgrading from a pre-F26 schema. Both default to NULL.
    try:
        conn.execute("SELECT source FROM config_history LIMIT 1")
    except sqlite3.OperationalError:
        try:
            conn.execute("ALTER TABLE config_history ADD COLUMN source TEXT")
            conn.commit()
            log_event("info", "db_migration",
                      "Migrated config_history: added 'source' column")
        except Exception:
            pass
    try:
        conn.execute("SELECT note FROM config_history LIMIT 1")
    except sqlite3.OperationalError:
        try:
            conn.execute("ALTER TABLE config_history ADD COLUMN note TEXT")
            conn.commit()
            log_event("info", "db_migration",
                      "Migrated config_history: added 'note' column")
        except Exception:
            pass

    # Migration: add 'boost' to the tier CHECK constraint on the offers
    # table. SQLite has no ALTER CHECK so the table must be recreated.
    # MUST run AFTER every ADD COLUMN migration above so the rebuild
    # preserves columns those migrations introduced (lifecycle_state,
    # offer_bech32, cancel_last_attempt_at, fee_mojos_xch, etc.). The
    # rebuild SQL is derived from the current CREATE TABLE recorded in
    # sqlite_master, so any future ADD COLUMN added before this block
    # is preserved automatically.
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='offers'"
        ).fetchone()
        if row and row[0] and "'boost'" not in row[0] and '"boost"' not in row[0]:
            import re as _re
            existing_sql = row[0]
            # Replace the tier CHECK constraint while leaving every other
            # column definition intact. The regex is anchored on `tier IN`
            # so it can't accidentally match the side/status CHECKs.
            new_sql, n_subs = _re.subn(
                r"CHECK\s*\(\s*tier\s+IN\s*\([^\)]+\)\s*\)",
                "CHECK(tier IN ('inner', 'mid', 'outer', 'extreme', 'sniper', 'boost'))",
                existing_sql, count=1, flags=_re.IGNORECASE,
            )
            if n_subs == 0:
                # Old schema with no tier CHECK at all; nothing to fix.
                pass
            else:
                # Rebuild via offers_new so we never have a window where
                # the table is missing.
                new_sql = _re.sub(
                    r'CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["`\[]?offers["`\]]?',
                    "CREATE TABLE offers_new",
                    new_sql, count=1, flags=_re.IGNORECASE,
                )
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(new_sql)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(offers)").fetchall()]
                col_list = ", ".join(f'"{c}"' for c in cols)
                conn.execute(
                    f"INSERT INTO offers_new ({col_list}) SELECT {col_list} FROM offers"
                )
                conn.execute("DROP TABLE offers")
                conn.execute("ALTER TABLE offers_new RENAME TO offers")
                # Indexes were dropped with the old table.
                conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_side ON offers(side)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_cat ON offers(cat_asset_id)")
                conn.commit()
                log_event("info", "db_migration",
                          "Migrated offers table: added 'boost' tier (preserved all columns)")
    except Exception as boost_mig_err:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_migration_failed",
                  f"Boost-tier migration failed: {boost_mig_err}")
        raise

    # Repair pass: reconcile lifecycle_state with terminal status values.
    # `update_offer_status()` writes both fields atomically, but legacy
    # code paths (or older migrations) set `status` directly and left
    # `lifecycle_state` stuck at 'open'. Rows like this bloat
    # `get_open_offers()` noise and pollute dashboard queries — the
    # affected DB had 1,880 such rows. Idempotent, runs once per init.
    try:
        cur = conn.execute(
            "UPDATE offers SET lifecycle_state = CASE status "
            "    WHEN 'cancelled' THEN 'cancelled' "
            "    WHEN 'filled'    THEN 'filled' "
            "    WHEN 'expired'   THEN 'expired' "
            "    ELSE lifecycle_state END "
            "WHERE status IN ('cancelled', 'filled', 'expired') "
            "  AND (lifecycle_state IS NULL "
            "       OR lifecycle_state = 'open' "
            "       OR lifecycle_state = 'refresh_due')"
        )
        repaired = cur.rowcount or 0
        if repaired > 0:
            conn.commit()
            log_event("info", "db_lifecycle_repair",
                      f"Reconciled {repaired} offer rows where status was "
                      f"terminal but lifecycle_state was still open/refresh_due")
    except Exception as _repair_err:
        log_event("warning", "db_lifecycle_repair_failed",
                  f"Lifecycle_state repair pass failed (non-fatal): {_repair_err}")

    conn.commit()
    log_event("info", "database_init", "Database initialized successfully")

    # Startup integrity check — surface DB corruption immediately rather
    # than letting it bleed through as random "database disk image is
    # malformed" errors mid-trade like the 26-04 incident. Runs on a
    # fresh connection so it inspects the on-disk file, not the page
    # cache. Logged as warning + emitted to the alert pipeline; the
    # bot doesn't refuse to start (operator may want to attempt
    # recovery via the GUI before stopping the bot entirely).
    try:
        result = check_db_integrity()
        if not result.get("ok"):
            log_event(
                "warning", "database_integrity_failed",
                f"PRAGMA integrity_check failed: "
                f"{result.get('result', 'unknown')}. Database is corrupted — "
                f"recover with `sqlite3 bot.db .recover` or restore from "
                f"backups/ before continuing to trade.",
                data={"errors": result.get("errors", [])[:10]},
            )
        else:
            log_event(
                "info", "database_integrity_ok",
                "Database integrity check passed at startup",
            )
    except Exception as _integrity_err:
        log_event(
            "warning", "database_integrity_check_failed",
            f"Database integrity check raised (non-fatal): {_integrity_err}",
        )

    # Force a clean WAL checkpoint at startup. If the previous run left
    # the WAL bloated (4MB+ in the 26-04 incident) this collapses it
    # back to zero before the new run starts accumulating frames.
    try:
        ckpt = checkpoint_wal("TRUNCATE")
        if ckpt:
            log_event(
                "info", "database_wal_startup_checkpoint",
                f"Startup WAL checkpoint: mode={ckpt.get('mode')}, "
                f"busy={ckpt.get('busy')}, "
                f"log_pages={ckpt.get('log_pages')}, "
                f"checkpointed={ckpt.get('checkpointed')}",
            )
    except Exception:
        pass  # non-fatal — bot can run with a non-zero WAL

    # Recent-corruption surfacing. attempt_db_recovery() in desktop_app
    # leaves bot.db.corrupt_<timestamp> backups behind whenever it has to
    # rebuild the DB. Bursts of these (4 in 4 minutes on 26-04) are the
    # tell-tale of a singleton-lock race or a multi-process WAL writer
    # hitting a known-bad SQLite case. Surface them at startup so the
    # operator sees the pattern instead of finding it only when trades
    # start failing.
    try:
        import glob as _glob
        import time as _time
        db_dir = os.path.dirname(os.path.abspath(DB_PATH)) or "."
        backups = _glob.glob(os.path.join(db_dir, "bot.db.corrupt_*"))
        # Filter to canonical files (skip -wal / -shm sidecars).
        primaries = [
            p for p in backups
            if not p.endswith(("-wal", "-shm"))
            and "_" in os.path.basename(p)
        ]
        now = _time.time()
        recent = []
        for p in primaries:
            try:
                age_h = (now - os.path.getmtime(p)) / 3600.0
                if age_h <= 24.0:
                    recent.append((p, age_h))
            except OSError:
                continue
        if recent:
            log_event(
                "warning", "database_recent_corruption_detected",
                f"Found {len(recent)} corrupt DB backup(s) in the last 24h "
                f"({len(primaries)} total). Likely cause: two desktop_app "
                f"processes wrote to the same bot.db (singleton lock race "
                f"or coin_prep_worker subprocess survived a parent kill). "
                f"Most recent: {os.path.basename(recent[0][0])} "
                f"({recent[0][1]:.1f}h ago).",
                data={"recent_backups": [os.path.basename(p) for p, _ in recent[:10]]},
            )
    except Exception:
        pass  # diagnostic only — never block startup


# ---------------------------------------------------------------------------
# Helper: get current UTC timestamp in ISO format
# ---------------------------------------------------------------------------
def _now() -> str:
    """Current UTC timestamp in SQLite-compatible format (YYYY-MM-DD HH:MM:SS).

    Uses the same format as SQLite's datetime('now') so that time-window
    queries like ``WHERE filled_at > datetime('now', '-1 hours')`` compare
    correctly against application-inserted timestamps.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _sqlite_ts(value) -> str:
    """Normalize a datetime or ISO string to SQLite timestamp format.

    _now() writes timestamps as 'YYYY-MM-DD HH:MM:SS', but Python's
    .isoformat() produces 'YYYY-MM-DDTHH:MM:SS+00:00'.  SQLite does
    lexical comparison, so mixing formats silently breaks time-window
    queries.  Pass any datetime or string through this helper before
    using it in a WHERE clause or INSERT.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    return text.replace("T", " ").replace("Z", "").split("+")[0]


def _get_reconcile_tier_sizes_mojos(wallet_type: str) -> Dict[str, int]:
    """Build tier sizes for reconcile-time auto-designation.

    F62 (2026-04-09): side-aware. XCH wallet uses buy tier sizes,
    CAT wallet uses sell tier sizes. Falls back to legacy shared
    fields via the per-side helpers when the per-side fields are zero.
    """
    try:
        from config import cfg as _cfg, get_buy_tier_size_xch, get_sell_tier_size_xch
        if not bool(getattr(_cfg, "TIER_ENABLED", False)):
            return {}

        headroom_pct = Decimal(str(getattr(_cfg, "COIN_PREP_HEADROOM_PCT", 10) or 10))
        if headroom_pct < 0:
            headroom_pct = Decimal("0")
        prep_mult = Decimal("1") + (headroom_pct / Decimal("100"))

        # XCH wallet funds BUY offers → use buy tier sizes.
        # CAT wallet funds SELL offers → use sell tier sizes.
        if wallet_type == "xch" and bool(getattr(_cfg, "BUY_LADDER_REVERSED", False)):
            # BUY_* values are slot-position indexed when the buy ladder is
            # reversed, but coin rows are labelled by coin-size bucket.
            # Mirror coin_manager.get_tier_sizes_mojos_from_cfg().
            buy_reversed_position_to_coin_size = {
                "inner": "extreme",
                "mid": "outer",
                "outer": "mid",
                "extreme": "inner",
            }
            buy_reversed_coin_size_to_position = {
                coin_size: position
                for position, coin_size in buy_reversed_position_to_coin_size.items()
            }
            tier_sizes_xch = {}
            for coin_tier in ("inner", "mid", "outer", "extreme"):
                position_tier = buy_reversed_coin_size_to_position[coin_tier]
                tier_sizes_xch[coin_tier] = Decimal(str(get_buy_tier_size_xch(position_tier) or 0))
        else:
            _get = get_sell_tier_size_xch if wallet_type == "cat" else get_buy_tier_size_xch
            tier_sizes_xch = {
                "inner":   Decimal(str(_get("inner") or 0)),
                "mid":     Decimal(str(_get("mid") or 0)),
                "outer":   Decimal(str(_get("outer") or 0)),
                "extreme": Decimal(str(_get("extreme") or 0)),
            }

        sniper_enabled = bool(getattr(_cfg, "SNIPER_ENABLED", False))
        sniper_prep_count = int(getattr(_cfg, "SNIPER_PREP_COUNT", 0) or 0)
        sniper_size = Decimal(str(getattr(_cfg, "SNIPER_SIZE_XCH", 0) or 0))
        if sniper_enabled and sniper_prep_count > 0 and sniper_size > 0:
            tier_sizes_xch["sniper"] = sniper_size

        tier_sizes_xch = {
            tier_name: size_xch
            for tier_name, size_xch in tier_sizes_xch.items()
            if size_xch > 0
        }
        if not tier_sizes_xch:
            return {}

        if wallet_type == "xch":
            result = {
                tier_name: int((size_xch * prep_mult) * Decimal("1000000000000"))
                for tier_name, size_xch in tier_sizes_xch.items()
            }
            try:
                from tx_fees import fee_pool_enabled, get_fee_coin_size_mojos
                if fee_pool_enabled():
                    fee_mojos = int(get_fee_coin_size_mojos() or 0)
                    if fee_mojos > 0:
                        result["fees"] = fee_mojos
            except Exception:
                pass
            return result

        price = None
        try:
            conn = get_connection()
            row = conn.execute(
                """SELECT combined_price
                   FROM price_history
                   WHERE combined_price IS NOT NULL
                   ORDER BY id DESC
                   LIMIT 1"""
            ).fetchone()
            if row and row["combined_price"] is not None:
                price = Decimal(str(row["combined_price"]))
        except Exception:
            price = None

        cat_scale = Decimal(10) ** Decimal(getattr(_cfg, "CAT_DECIMALS", 3))
        fallback_cat_amount = Decimal(str(getattr(_cfg, "CAT_COIN_SIZE", 0) or 0))
        result = {}
        for tier_name, size_xch in tier_sizes_xch.items():
            if price and price > 0:
                cat_amount = (size_xch / price * prep_mult).quantize(Decimal("1"))
            else:
                cat_amount = fallback_cat_amount
            result[tier_name] = int(cat_amount * cat_scale)
        return result
    except Exception:
        return {}


def _infer_reconcile_designation_by_size(amt: int, tier_sizes_mojos: Dict[str, int]) -> tuple[str, str]:
    """Infer designation for a newly-seen coin during reconciliation.

    Routes through the single-source-of-truth classifier in
    :mod:`coin_classifier` so that reconcile agrees with the misfit
    absorber and the offer selector on what "fits a tier" means. This
    eliminates the class of bugs where reconcile's loose ±20% bounds put
    a coin in a tier bucket that the absorber's strict 0.98/1.5 bounds
    would have rejected.
    """
    from coin_classifier import infer_designation_by_size as _cc_infer
    return _cc_infer(amt, tier_sizes_mojos)


# ---------------------------------------------------------------------------
# Offers — create, update status, query
# ---------------------------------------------------------------------------

def add_offer(trade_id: str, side: str, price_xch: Decimal, size_xch: Decimal,
              size_cat: Decimal, cat_asset_id: str, tier: str = "mid",
              expires_at: str = None, coin_id: str = None,
              fee_mojos_xch: int = 0) -> bool:
    """Record a new offer in the database.

    Args:
        trade_id: Chia wallet trade ID (the universal key)
        side: 'buy' or 'sell'
        price_xch: Price in XCH per CAT token
        size_xch: Offer size in XCH
        size_cat: Offer size in CAT tokens
        cat_asset_id: Which CAT pair this offer is for
        tier: 'inner', 'mid', 'outer', 'extreme', 'sniper', or 'boost'
        expires_at: ISO timestamp when this offer expires
        coin_id: The specific coin locked by this offer (from before/after snapshot)
        fee_mojos_xch: Transaction fee in mojos attached to this offer at creation

    Returns:
        True if inserted successfully, False on error
    """
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO offers (trade_id, side, price_xch, size_xch, size_cat,
               tier, status, cat_asset_id, created_at, expires_at, coin_id, fee_mojos_xch)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
            (trade_id, side, str(price_xch), str(size_xch), str(size_cat),
             tier, cat_asset_id, _now(), expires_at, coin_id, int(fee_mojos_xch))
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError as e:
        err = str(e)
        if "UNIQUE constraint failed" in err:
            # trade_id already exists — this is fine on restart/resume
            print(f"  ⚠️ [DB] add_offer SKIPPED — trade_id {trade_id[:16]}... already exists", flush=True)
            log_event("warning", "offer_duplicate", f"Offer {trade_id[:16]}... already in DB — skipped")
            return False
        # Any other constraint failure (e.g. CHECK on tier/side/status) is a real error
        print(f"  ❌ [DB] add_offer CONSTRAINT ERROR for {trade_id[:16]}...: {err}", flush=True)
        log_event("error", "db_constraint_error", f"Failed to add offer {trade_id[:16]}...: {err}")
        return False
    except Exception as e:
        print(f"  ❌ [DB] add_offer FAILED for {trade_id[:16]}...: {e}", flush=True)
        log_event("error", "db_error", f"Failed to add offer {trade_id}: {e}")
        return False


def recover_unknown_offers(wallet_offers: list, cat_asset_id: str) -> dict:
    """Import wallet offers that are NOT in the database.

    This handles the scenario where the bot created offers on-chain
    but couldn't write to the DB (e.g., DB was locked, bot crashed).
    On restart, this function discovers the gap and imports them.

    Args:
        wallet_offers: List of offer dicts from classify_offers_from_list()
                       (both buys and sells combined)
        cat_asset_id: The CAT asset ID for this trading pair

    Returns:
        dict with keys: recovered, skipped, errors
    """
    from config import cfg

    stats = {"recovered": 0, "skipped": 0, "errors": 0, "trade_ids": []}

    if not wallet_offers:
        return stats

    # Dedicated connection with isolation_level=None (true autocommit).
    # Avoids Python's implicit transaction management which causes lock
    # upgrade failures when Flask GUI threads hold uncommitted write txns.
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # Super log trace
    try:
        from super_log import trace_connection
        trace_connection(conn, "recover-offers")
    except ImportError:
        pass

    # Get all trade_ids already in DB (any status — not just open)
    db_trade_ids = set()
    try:
        rows = conn.execute("SELECT trade_id FROM offers").fetchall()
        for r in rows:
            db_trade_ids.add(r['trade_id'])
    except sqlite3.OperationalError as e:
        print(f"  ⚠️ [DB] Failed to read offers for recovery: {e}", flush=True)

    # Tier size thresholds for assignment (XCH amounts)
    # Match offer size to nearest tier
    tier_sizes = [
        (cfg.INNER_SIZE_XCH, 'inner'),
        (cfg.MID_SIZE_XCH, 'mid'),
        (cfg.OUTER_SIZE_XCH, 'outer'),
        (cfg.EXTREME_SIZE_XCH, 'extreme'),
    ]
    sniper_size = getattr(cfg, "SNIPER_SIZE_XCH", None)
    if getattr(cfg, "SNIPER_ENABLED", False) and sniper_size and Decimal(str(sniper_size)) > 0:
        tier_sizes.append((Decimal(str(sniper_size)), 'sniper'))

    for offer in wallet_offers:
        tid = offer.get('trade_id', '')
        if not tid:
            continue

        if tid in db_trade_ids:
            stats["skipped"] += 1
            continue

        try:
            summary = offer.get('summary', {})
            offered = summary.get('offered', {})
            requested = summary.get('requested', {})

            # Determine side
            is_buy = 'xch' in offered and cat_asset_id in requested
            is_sell = cat_asset_id in offered and 'xch' in requested
            if not is_buy and not is_sell:
                stats["skipped"] += 1
                continue

            side = 'buy' if is_buy else 'sell'

            # Extract amounts
            if is_buy:
                xch_mojos = int(offered.get('xch', 0))
                cat_mojos = int(requested.get(cat_asset_id, 0))
            else:
                cat_mojos = int(offered.get(cat_asset_id, 0))
                xch_mojos = int(requested.get('xch', 0))

            size_xch = Decimal(str(xch_mojos)) / Decimal('1000000000000')
            cat_decimals = getattr(cfg, 'CAT_DECIMALS', 3)
            size_cat = Decimal(str(cat_mojos)) / Decimal(str(10 ** cat_decimals))

            # Calculate price (XCH per CAT)
            if size_cat > 0:
                price_xch = size_xch / size_cat
            else:
                price_xch = Decimal('0')

            # Assign tier by matching offer size to configured tier sizes
            tier = 'mid'  # default fallback
            best_match = None
            best_diff = None
            for tier_size, tier_name in tier_sizes:
                diff = abs(size_xch - tier_size)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_match = tier_name
            if best_match:
                tier = best_match

            # Get expiry if available
            valid_times = offer.get('valid_times', {})
            max_time = valid_times.get('max_time')
            expires_at = None
            if max_time and int(max_time) > 0:
                from datetime import datetime, timezone
                expires_at = _sqlite_ts(datetime.fromtimestamp(
                    int(max_time), tz=timezone.utc
                ))

            # Insert into DB (retry on lock) — explicit transaction per offer
            # since we're using isolation_level=None (autocommit mode)
            for _attempt in range(3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO offers
                           (trade_id, side, price_xch, size_xch, size_cat,
                            tier, status, cat_asset_id, created_at, expires_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                        (tid, side, str(price_xch), str(size_xch), str(size_cat),
                         tier, cat_asset_id, _now(), expires_at)
                    )
                    conn.execute("COMMIT")
                    if cur.rowcount:
                        stats["recovered"] += 1
                        stats["trade_ids"].append(tid)
                        db_trade_ids.add(tid)
                    else:
                        stats["skipped"] += 1
                    break
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    import time
                    time.sleep(0.5)
            else:
                stats["errors"] += 1

        except Exception as e:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            print(f"  ❌ [DB] recover offer {tid[:16]}... failed: {e}", flush=True)
            stats["errors"] += 1

    conn.close()

    if stats["recovered"] > 0:
        log_event("info", "offers_recovered",
                  f"Recovered {stats['recovered']} unknown wallet offers into DB "
                  f"(skipped {stats['skipped']}, errors {stats['errors']})")

    return stats


def update_offer_coin_id(trade_id: str, coin_id: str) -> bool:
    """Record which coin was locked by this offer.

    Called after before/after snapshot detects the locked coin.
    """
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE offers SET coin_id=? WHERE trade_id=?",
            (coin_id, trade_id)
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to update coin_id for {trade_id}: {e}")
        return False


def update_offer_status(trade_id: str, status: str) -> bool:
    """Update an offer's status (e.g., 'filled', 'cancelled', 'expired').

    Also sets the appropriate timestamp (filled_at or cancelled_at).
    Also updates the coins table:
      - filled → coin is destroyed on-chain → mark_coin_spent()
      - cancelled → secure cancel destroys coin → mark_coin_spent()
      - expired → coin unlocked (no on-chain tx) → free_coin()
    """
    import time as _time
    lifecycle_state = status
    coarse_status = "expired" if status == "not_submitted" else status
    for _attempt in range(3):
        try:
            conn = get_connection()
            now = _now()
            row = conn.execute(
                "SELECT status, filled_at, cancelled_at FROM offers WHERE trade_id=?",
                (trade_id,),
            ).fetchone()

            # A real fill must win over any later cancel/expiry bookkeeping.
            # This avoids sniper-cleanup or cancel-retry races downgrading a
            # verified fill into a cancelled row.
            if (row and coarse_status in ("cancelled", "expired")
                    and (row["status"] == "filled" or row["filled_at"])):
                return True

            # Derive lifecycle_state from coarse status.
            # Callers can also set lifecycle_state directly via
            # update_offer_lifecycle_state() for finer-grained tracking.
            # not_submitted is stored as status=expired to preserve the
            # legacy CHECK constraint while keeping the precise lifecycle.

            if coarse_status == "filled":
                conn.execute(
                    "UPDATE offers SET status=?, lifecycle_state=?, filled_at=?, cancelled_at=NULL WHERE trade_id=?",
                    (coarse_status, lifecycle_state, now, trade_id)
                )
            elif coarse_status in ("cancelled", "expired"):
                conn.execute(
                    "UPDATE offers SET status=?, lifecycle_state=?, cancelled_at=?, filled_at=NULL WHERE trade_id=?",
                    (coarse_status, lifecycle_state, now, trade_id)
                )
            else:
                conn.execute(
                    "UPDATE offers SET status=?, lifecycle_state=? WHERE trade_id=?",
                    (coarse_status, lifecycle_state, trade_id)
                )

            conn.commit()

            # ---- Update coin status based on what happened to the offer ----
            # Sage can lock multiple source coins for one offer, so we must
            # update every currently locked coin tied to this trade_id.
            coin_ids = get_locked_coin_ids_for_trade(trade_id)

            # Backward-compatibility fallback: very old rows may only have the
            # primary offers.coin_id recorded.
            if not coin_ids:
                row = conn.execute(
                    "SELECT coin_id FROM offers WHERE trade_id=?", (trade_id,)
                ).fetchone()
                coin_id = row["coin_id"] if row else None
                if coin_id:
                    coin_ids = [coin_id]

            for coin_id in coin_ids:
                if coarse_status == "filled":
                    mark_coin_spent(coin_id)
                elif coarse_status == "cancelled":
                    # Secure cancel destroys the old coin and creates a new one.
                    # The new coin will be auto-discovered on next snapshot.
                    mark_coin_spent(coin_id)
                elif coarse_status == "expired":
                    # Expired/not-submitted offers unlock the coin; no on-chain tx.
                    free_coin(coin_id)

            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and _attempt < 2:
                _time.sleep(0.5 * (_attempt + 1))
                continue
            try:
                conn.rollback()
            except Exception:
                pass
            log_event("error", "db_error", f"Failed to update offer {trade_id}: {e}")
            return False
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log_event("error", "db_error", f"Failed to update offer {trade_id}: {e}")
        return False
    return False  # all retries exhausted


def mark_cancel_attempted(trade_id: str) -> bool:
    """Stamp cancel_last_attempt_at = now() for the given offer.

    Used by the cancel path to record when a cancel RPC was last sent so
    the bot_health verifier can throttle retries (don't re-cancel a still-
    pending offer every cycle — wait the configured backoff period).
    """
    from datetime import datetime, timezone
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE offers SET cancel_last_attempt_at=? WHERE trade_id=?",
            (datetime.now(timezone.utc).isoformat(), trade_id),
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("warning", "mark_cancel_attempted_failed",
                  f"Failed to stamp cancel_last_attempt_at for {trade_id[:16]}...: {e}")
        return False


def update_offer_lifecycle_state(trade_id: str, lifecycle_state: str) -> bool:
    """Update only the lifecycle_state column (extended state tracking).

    Also updates the coarse status column for backward compatibility.
    Does NOT touch coin status — only update_offer_status does that.
    """
    try:
        from offer_lifecycle import coarse_status
        coarse = coarse_status(lifecycle_state)
    except Exception:
        coarse = lifecycle_state if lifecycle_state in ("open", "filled", "cancelled", "expired") else "open"

    try:
        conn = get_connection()
        conn.execute(
            "UPDATE offers SET lifecycle_state=?, status=? WHERE trade_id=?",
            (lifecycle_state, coarse, trade_id),
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# F21 (2026-04-08): lifecycle state machine observability counters.
# Tracks how often noop transitions happen (signal didn't match the
# current state) so we can spot misuse without making the FSM strict.
# Strict mode (raise on noop) would break legacy callers; observation
# mode is non-disruptive.
_lifecycle_noop_counter: Dict[str, int] = {}
_lifecycle_invalid_signal_counter: Dict[str, int] = {}
_lifecycle_counter_lock = threading.Lock()


def get_lifecycle_observability_stats() -> Dict[str, Dict[str, int]]:
    """Return a snapshot of lifecycle FSM observability counters.

    F21: useful for periodic logging or doctor reports. Counts noop
    transitions (signal valid but not appropriate for current state)
    and invalid signal names (caller passed something not in the enum).
    """
    with _lifecycle_counter_lock:
        return {
            "noop_transitions": dict(_lifecycle_noop_counter),
            "invalid_signals": dict(_lifecycle_invalid_signal_counter),
        }


def reset_lifecycle_observability_stats() -> None:
    """Reset the lifecycle observability counters (e.g. after logging)."""
    with _lifecycle_counter_lock:
        _lifecycle_noop_counter.clear()
        _lifecycle_invalid_signal_counter.clear()


def transition_offer(trade_id: str, signal: str):
    """Apply a lifecycle signal to an offer via the canonical FSM.

    Reads the current lifecycle_state from the DB, passes it through
    offer_lifecycle.apply_signal(), writes the new state back, and
    returns the OfferTransition dataclass.  Returns None on any error
    (fail-open — callers must not block critical paths on this).

    F21 (2026-04-08): observability counters track noop transitions
    and invalid signals. The FSM stays fail-open (we don't raise on
    bad inputs) but we count them so the operator can detect drift.

    Args:
        trade_id: The offer's trade_id.
        signal:   String name of an OfferSignal value (e.g. "cancel_sent").
    """
    try:
        from offer_lifecycle import OfferSignal, OfferState, apply_signal
        conn = get_connection()
        row = conn.execute(
            "SELECT lifecycle_state FROM offers WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        if row is None:
            return None
        current_raw = (row["lifecycle_state"] or "open").strip()
        try:
            current_state = OfferState(current_raw)
        except ValueError:
            current_state = OfferState.OPEN
        try:
            sig = OfferSignal(signal)
        except ValueError:
            # F21: count invalid signal names per signal value
            with _lifecycle_counter_lock:
                _lifecycle_invalid_signal_counter[signal] = (
                    _lifecycle_invalid_signal_counter.get(signal, 0) + 1
                )
            return None
        transition = apply_signal(current_state, sig)
        if transition.new_state != current_state:
            update_offer_lifecycle_state(trade_id, str(transition.new_state))
        else:
            # F21: count noop transitions (state didn't change despite a
            # valid signal). Key by "state→signal" so we can spot which
            # combinations are misused.
            if transition.action == "noop":
                key = f"{current_state.value}→{sig.value}"
                with _lifecycle_counter_lock:
                    _lifecycle_noop_counter[key] = (
                        _lifecycle_noop_counter.get(key, 0) + 1
                    )
        return transition
    except Exception:
        return None


def get_locked_coin_ids_for_trade(trade_id: str) -> List[str]:
    """Return all currently locked coin ids associated with a trade."""
    if not trade_id:
        return []
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT coin_id FROM coins WHERE trade_id=? AND status='locked' ORDER BY coin_id",
            (trade_id,)
        ).fetchall()
        return [row["coin_id"] for row in rows if row["coin_id"]]
    except Exception:
        return []


def batch_cancel_stale_offers(stale_trade_ids: list) -> int:
    """Cancel multiple stale offers in a SINGLE transaction.

    This replaces calling update_offer_status() 80 times (80 lock acquisitions)
    with ONE batch UPDATE (1 lock acquisition). Eliminates DB lock contention
    during startup cleanup.

    IMPORTANT: Uses a dedicated connection instead of the thread-local one.
    The thread-local connection often has an implicit read transaction open
    (from get_open_offers() SELECT), which prevents lock upgrades and makes
    busy_timeout ineffective. A fresh connection has no open transaction,
    so busy_timeout works correctly.

    Returns the number of offers successfully cancelled.
    """
    if not stale_trade_ids:
        return 0

    try:
        from super_log import slog
        slog("DB_WRITE", f"batch_cancel_stale_offers: {len(stale_trade_ids)} offers")
    except ImportError:
        pass
    # Dedicated connection with isolation_level=None (true autocommit mode).
    # This bypasses Python's implicit transaction management entirely.
    # We use explicit BEGIN IMMEDIATE to acquire the write lock with
    # busy_timeout retry, rather than relying on implicit BEGIN which
    # can get stuck behind cascading uncommitted transactions from
    # Flask GUI polling threads.
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")  # 30s — startup can afford to wait
    try:
        from super_log import trace_connection
        trace_connection(conn, "batch-cancel")
    except ImportError:
        pass

    try:
        now = _now()
        # Safe: f-string only interpolates the count of ? placeholders, never user values.
        # All actual values are passed as parameterised arguments — no SQL injection risk.
        placeholders = ",".join("?" * len(stale_trade_ids))
        # BEGIN IMMEDIATE acquires write lock upfront with busy_timeout retry
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE offers SET status='cancelled', lifecycle_state='cancelled', cancelled_at=? "
            f"WHERE trade_id IN ({placeholders})",
            [now] + list(stale_trade_ids)
        )
        # Free/spend coins that were locked by these offers
        conn.execute(
            f"UPDATE coins SET status='spent', last_seen=? "
            f"WHERE trade_id IN ({placeholders}) AND status='locked'",
            [now] + list(stale_trade_ids)
        )
        conn.execute("COMMIT")
        return len(stale_trade_ids)
    except sqlite3.OperationalError as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        log_event("error", "db_error",
                  f"Batch cancel failed: {e}")
        return 0
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        log_event("error", "db_error", f"Batch cancel failed: {e}")
        return 0
    finally:
        conn.close()


def update_offer_dexie(trade_id: str, dexie_id: str) -> bool:
    """Record that an offer was successfully posted to Dexie."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE offers SET dexie_id=?, dexie_posted=1 WHERE trade_id=?",
            (dexie_id, trade_id)
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to update Dexie link for {trade_id}: {e}")
        return False


def update_offer_bech32(trade_id: str, offer_bech32: str) -> bool:
    """Store the bech32 offer string for fast Dexie repost on startup.

    Called right after offer creation when the bech32 is available.
    This avoids needing a wallet RPC call per offer during startup repost.
    """
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE offers SET offer_bech32=? WHERE trade_id=?",
            (offer_bech32, trade_id)
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False

_NON_ACTIONABLE_OPEN_LIFECYCLE_STATES = (
    "cancel_requested",
    "cancel_sent",
    "mempool_observed",
)


def _actionable_open_lifecycle_clause() -> str:
    states = ", ".join(f"'{state}'" for state in _NON_ACTIONABLE_OPEN_LIFECYCLE_STATES)
    return f"(lifecycle_state IS NULL OR lifecycle_state NOT IN ({states}))"


def get_offers_for_repost(cat_asset_id: str = None) -> List[Dict]:
    """Get open offers with their bech32 strings for Dexie repost.

    Returns only offers that have a stored bech32 string.
    Used during startup to repost offers without calling wallet RPC.
    """
    conn = get_connection()
    query = f"""SELECT trade_id, offer_bech32, dexie_id, side
                FROM offers
                WHERE status='open'
                  AND offer_bech32 IS NOT NULL
                  AND {_actionable_open_lifecycle_clause()}"""
    params = []
    if cat_asset_id:
        query += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_open_offers(side: str = None, cat_asset_id: str = None,
                    include_pending_cancel: bool = False,
                    include_mempool_observed: bool = False) -> List[Dict]:
    """Get all open offers, optionally filtered by side and/or CAT pair.

    By default, excludes offers whose lifecycle_state is 'cancel_requested'
    (cancel RPC sent, awaiting on-chain confirmation). These offers still
    have status='open' in the DB but the bot has already asked Sage to
    cancel them — for cap counting, requote selection, dashboard display,
    and trim decisions they are effectively gone.

    Pass include_pending_cancel=True only when you specifically need to
    examine pending-cancel offers (e.g. the bot_health verifier loop that
    re-checks them against Dexie/Sage).
    Pass include_mempool_observed=True only when investigating parked
    fill-verification rows; they are protected in DB but hidden from normal
    active-book views by default.

    Returns list of dicts with all offer fields.
    """
    conn = get_connection()
    query = "SELECT * FROM offers WHERE status='open'"
    params = []

    excluded_lifecycle_states = []
    if not include_pending_cancel:
        excluded_lifecycle_states.extend(("cancel_requested", "cancel_sent"))
    if not include_mempool_observed:
        excluded_lifecycle_states.append("mempool_observed")
    if excluded_lifecycle_states:
        placeholders = ", ".join("?" for _ in excluded_lifecycle_states)
        query += (
            " AND (lifecycle_state IS NULL "
            f"OR lifecycle_state NOT IN ({placeholders}))"
        )
        params.extend(excluded_lifecycle_states)

    if side:
        query += " AND side=?"
        params.append(side)
    if cat_asset_id:
        query += " AND cat_asset_id=?"
        params.append(cat_asset_id)

    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_offer_lifecycle_summary(cat_asset_id: str = None) -> Dict:
    """Return compact offer status/lifecycle counts for GUI diagnostics."""
    empty_side = {"open": 0, "filled": 0, "cancelled": 0, "expired": 0}
    summary = {
        "total": 0,
        "by_side": {
            "buy": dict(empty_side),
            "sell": dict(empty_side),
        },
        "by_status": dict(empty_side),
        "by_lifecycle": {},
        "pending_cancel": 0,
    }

    conn = get_connection()
    query = (
        "SELECT side, status, COALESCE(lifecycle_state, status, 'open') AS lifecycle_state, "
        "COUNT(*) AS count FROM offers"
    )
    params = []
    if cat_asset_id:
        query += " WHERE cat_asset_id=?"
        params.append(cat_asset_id)
    query += " GROUP BY side, status, COALESCE(lifecycle_state, status, 'open')"

    for row in conn.execute(query, params).fetchall():
        side = str(row["side"] or "").lower()
        status = str(row["status"] or "open").lower()
        lifecycle = str(row["lifecycle_state"] or status or "open").lower()
        count = int(row["count"] or 0)

        summary["total"] += count
        if status not in summary["by_status"]:
            summary["by_status"][status] = 0
        summary["by_status"][status] += count

        if side not in summary["by_side"]:
            summary["by_side"][side] = dict(empty_side)
        if status not in summary["by_side"][side]:
            summary["by_side"][side][status] = 0
        summary["by_side"][side][status] += count

        summary["by_lifecycle"][lifecycle] = (
            summary["by_lifecycle"].get(lifecycle, 0) + count
        )
        if lifecycle in ("cancel_requested", "cancel_sent"):
            summary["pending_cancel"] += count

    return summary


def get_offer(trade_id: str) -> Optional[Dict]:
    """Get a single offer by trade_id."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM offers WHERE trade_id=?", (trade_id,)).fetchone()
    return dict(row) if row else None


def get_offers_by_trade_ids(trade_ids: list) -> list:
    """Batch-fetch offer records for a list of trade_ids.
    More efficient than N individual get_offer() calls.
    Returns list of row dicts (may be shorter than input if some not found).
    """
    if not trade_ids:
        return []
    try:
        conn = get_connection()
        placeholders = ",".join("?" * len(trade_ids))
        rows = conn.execute(
            f"SELECT * FROM offers WHERE trade_id IN ({placeholders})",
            trade_ids
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log_event("warning", "get_offers_batch_failed",
                  f"Batch offer fetch failed: {e}")
        return []


def get_trade_dexie_map(cat_asset_id: str = None) -> Dict[str, str]:
    """Get mapping of trade_id -> dexie_id for all posted offers.

    This replaces the old offers_state.json trade_dexie_map.
    """
    conn = get_connection()
    query = "SELECT trade_id, dexie_id FROM offers WHERE dexie_posted=1 AND dexie_id IS NOT NULL"
    params = []

    if cat_asset_id:
        query += " AND cat_asset_id=?"
        params.append(cat_asset_id)

    rows = conn.execute(query, params).fetchall()
    return {row["trade_id"]: row["dexie_id"] for row in rows}


# ---------------------------------------------------------------------------
# Coins — comprehensive coin tracking (free, locked, spent, gone)
# ---------------------------------------------------------------------------

def upsert_coin(coin_id: str, wallet_type: str, amount_mojos: int,
                tier: str = None, designation: str = None,
                assigned_tier: str = None, **kwargs) -> bool:
    """Insert a new coin or update last_seen if it already exists.

    Called from update_coin_counts() after each snapshot. If the coin
    was previously 'gone', it gets reset to 'free' AND its designation
    is reset to 'unknown' (needs re-classification after reappearing).

    New coins get designation='unknown' and assigned_tier='none' unless
    explicitly provided. Existing coins preserve their designation
    (won't get overwritten by None).

    Args:
        coin_id: The unique Chia coin ID (hex string)
        wallet_type: 'xch' or 'cat'
        amount_mojos: Coin size in mojos
        tier: Classification tier (inner/mid/outer/extreme/reserve/small/unknown)
        designation: Role designation (reserve/tier_spare/tier_active/dust/unknown)
        assigned_tier: Which tier this coin serves (inner/mid/outer/extreme/none)
    """
    try:
        conn = get_connection()
        now = _now()
        # Default designation for new coins
        desig = designation or 'unknown'
        atier = assigned_tier or 'none'
        # Normalize coin_id before any DB operation — ensures consistency
        # with reconcile_coins_with_wallet() which also normalizes.
        coin_id = norm_coin_id(coin_id)

        # Check if coin already exists and its current status (for logging)
        existing = conn.execute(
            "SELECT status, amount_mojos, designation FROM coins WHERE coin_id=?",
            (coin_id,)
        ).fetchone()
        # Try INSERT first, on conflict update last_seen and potentially status
        # Key behavior:
        # - NEW coins: get the provided designation (or 'unknown')
        # - EXISTING coins: keep their current designation (COALESCE preserves it)
        # - REAPPEARING coins (was 'gone'): reset designation to 'unknown'
        conn.execute(
            """INSERT INTO coins (coin_id, wallet_type, amount_mojos, tier, status,
                                  first_seen, last_seen, designation, assigned_tier)
               VALUES (?, ?, ?, ?, 'free', ?, ?, ?, ?)
               ON CONFLICT(coin_id) DO UPDATE SET
                   last_seen = ?,
                   tier = COALESCE(?, tier),
                   amount_mojos = ?,
                   status = CASE
                       WHEN coins.status = 'gone' THEN 'free'
                       ELSE coins.status
                   END,
                   designation = CASE
                       WHEN coins.status = 'gone' THEN 'unknown'
                       ELSE COALESCE(coins.designation, 'unknown')
                   END,
                   assigned_tier = CASE
                       WHEN coins.status = 'gone' THEN 'none'
                       ELSE COALESCE(coins.assigned_tier, 'none')
                   END""",
            (coin_id, wallet_type, amount_mojos, tier, now, now, desig, atier,
             now, tier, amount_mojos)
        )
        if not kwargs.get("_skip_commit"):
            conn.commit()
        # Log the coin lifecycle event with structured data
        if existing is None:
            # Brand new coin
            if wallet_type == 'xch':
                amt_str = f"{amount_mojos / 1_000_000_000_000:.4f} XCH"
            else:
                amt_str = f"{amount_mojos} mojos"
            log_event("debug", "coin_upserted",
                      f"New {wallet_type.upper()} coin {coin_id[:16]}... ({amt_str})",
                      data={"coin_id": coin_id, "amount_mojos": amount_mojos,
                            "wallet_type": wallet_type, "is_new": True,
                            "reappearing": False})
        elif existing['status'] == 'gone':
            # Reappearing coin
            if wallet_type == 'xch':
                amt_str = f"{amount_mojos / 1_000_000_000_000:.4f} XCH"
            else:
                amt_str = f"{amount_mojos} mojos"
            log_event("debug", "coin_upserted",
                      f"{wallet_type.upper()} coin {coin_id[:16]}... reappeared ({amt_str})"
                      f" — was gone, now free",
                      data={"coin_id": coin_id, "amount_mojos": amount_mojos,
                            "wallet_type": wallet_type, "is_new": False,
                            "reappearing": True})
        return True
    except Exception as e:
        log_event("error", "db_error", f"Failed to upsert coin {coin_id[:16]}...: {e}")
        return False


def batch_upsert_coins(coins: list, wallet_type: str = "xch") -> int:
    """Batch upsert multiple coins with a single commit.

    Args:
        coins: List of dicts with keys: coin_id, amount_mojos, tier
        wallet_type: 'xch' or 'cat'

    Returns number of coins successfully upserted.
    """
    count = 0
    failures = 0
    first_error: Optional[str] = None
    conn = get_connection()
    for c in coins:
        try:
            upsert_coin(
                c["coin_id"], wallet_type, c["amount_mojos"],
                tier=c.get("tier", "unknown"),
                _skip_commit=True,
            )
            count += 1
        except Exception as e:
            failures += 1
            if first_error is None:
                first_error = f"{type(e).__name__}: {e}"
    try:
        conn.commit()
    except Exception as e:
        # Commit failure is worse than any individual upsert failure —
        # the whole batch is lost. Log it so it's visible.
        try:
            log_event("error", "batch_upsert_commit_failed",
                      f"batch_upsert_coins commit failed "
                      f"({wallet_type}, {count} staged): {type(e).__name__}: {e}")
        except Exception:
            pass
        return 0
    if failures > 0:
        try:
            log_event("warning", "batch_upsert_partial_failure",
                      f"batch_upsert_coins ({wallet_type}): {failures}/{len(coins)} "
                      f"failed, first error: {first_error}")
        except Exception:
            pass
    return count


def lock_coin(coin_id: str, trade_id: str) -> bool:
    """Mark a coin as locked by a specific offer.

    Called after offer creation when the before/after snapshot
    detects which coin was consumed.

    Args:
        coin_id: The coin that got locked
        trade_id: The offer that locked it
    """
    try:
        conn = get_connection()
        # Get coin details before locking (for logging)
        row = conn.execute(
            "SELECT wallet_type, amount_mojos, designation, assigned_tier FROM coins WHERE coin_id=?",
            (coin_id,)
        ).fetchone()
        conn.execute(
            "UPDATE coins SET status='locked', trade_id=?, last_seen=? WHERE coin_id=?",
            (trade_id, _now(), coin_id)
        )
        conn.commit()
        # Log the lock event with full details + structured data
        if row:
            wt = row['wallet_type'].upper()
            if row['wallet_type'] == 'xch':
                amt_str = f"{row['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
            else:
                amt_str = f"{row['amount_mojos']} mojos"
            log_event("debug", "coin_locked",
                      f"{wt} coin {coin_id[:16]}... LOCKED by offer {trade_id[:12]}..."
                      f" ({amt_str} | {row['designation']}/{row['assigned_tier']})",
                      data={"coin_id": coin_id, "trade_id": trade_id,
                            "amount_mojos": row['amount_mojos'],
                            "wallet_type": row['wallet_type'],
                            "designation": row['designation'],
                            "assigned_tier": row['assigned_tier']})
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to lock coin {coin_id[:16]}...: {e}")
        return False


def free_coin(coin_id: str) -> bool:
    """Mark a coin as free (available for new offers).

    Called when an offer expires — the coin is unlocked but
    not destroyed (no on-chain transaction for expiry).

    Args:
        coin_id: The coin to mark as free
    """
    try:
        conn = get_connection()
        # Get coin details before freeing (for logging)
        row = conn.execute(
            "SELECT wallet_type, amount_mojos, trade_id, designation, assigned_tier FROM coins WHERE coin_id=?",
            (coin_id,)
        ).fetchone()
        conn.execute(
            "UPDATE coins SET status='free', trade_id=NULL, last_seen=? WHERE coin_id=?",
            (_now(), coin_id)
        )
        conn.commit()
        # Log the free event with full details + structured data
        if row:
            wt = row['wallet_type'].upper()
            if row['wallet_type'] == 'xch':
                amt_str = f"{row['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
            else:
                amt_str = f"{row['amount_mojos']} mojos"
            old_tid = row['trade_id'] or 'none'
            log_event("debug", "coin_freed",
                      f"{wt} coin {coin_id[:16]}... FREED ({amt_str})"
                      f" — was locked by {old_tid[:12] if old_tid != 'none' else 'none'}..."
                      f" | {row['designation']}/{row['assigned_tier']}",
                      data={"coin_id": coin_id, "amount_mojos": row['amount_mojos'],
                            "wallet_type": row['wallet_type'],
                            "old_trade_id": row['trade_id'],
                            "designation": row['designation'],
                            "assigned_tier": row['assigned_tier']})
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to free coin {coin_id[:16]}...: {e}")
        return False


def mark_coin_spent(coin_id: str) -> bool:
    """Mark a coin as spent (destroyed on-chain).

    Called when an offer is filled or cancelled (secure cancel
    destroys the original coin and creates a new one with a
    different coin_id).

    Args:
        coin_id: The coin that was destroyed
    """
    if not coin_id:
        return False
    try:
        conn = get_connection()
        # Get coin details before marking spent (for logging)
        row = conn.execute(
            "SELECT wallet_type, amount_mojos, trade_id, designation, assigned_tier FROM coins WHERE coin_id=?",
            (coin_id,)
        ).fetchone()
        conn.execute(
            """UPDATE coins SET status='spent', last_seen=?,
               designation='unknown', assigned_tier='none'
               WHERE coin_id=?""",
            (_now(), coin_id)
        )
        conn.commit()
        # Log the spent event with full details + structured data
        if row:
            wt = row['wallet_type'].upper()
            if row['wallet_type'] == 'xch':
                amt_str = f"{row['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
            else:
                amt_str = f"{row['amount_mojos']} mojos"
            tid = row['trade_id'] or 'none'
            log_event("debug", "coin_spent",
                      f"{wt} coin {coin_id[:16]}... SPENT/DESTROYED ({amt_str})"
                      f" — was {row['designation']}/{row['assigned_tier']}"
                      f" | offer {tid[:12] if tid != 'none' else 'none'}...",
                      data={"coin_id": coin_id, "amount_mojos": row['amount_mojos'],
                            "wallet_type": row['wallet_type'],
                            "trade_id": row['trade_id'],
                            "designation": row['designation'],
                            "assigned_tier": row['assigned_tier']})
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to mark coin spent {coin_id[:16]}...: {e}")
        return False


def mark_coins_gone(coin_ids: List[str]) -> int:
    """Batch mark coins as 'gone' (vanished from wallet).

    Called after a snapshot when coins that were 'free' in the DB
    are no longer visible in the wallet. This could mean they were
    spent externally, or the wallet hasn't synced yet.

    Args:
        coin_ids: List of coin IDs that disappeared

    Returns:
        Number of coins marked as gone
    """
    if not coin_ids:
        return 0
    try:
        conn = get_connection()
        now = _now()
        coin_list = list(coin_ids)
        placeholders = ",".join("?" * len(coin_list))

        # Batch SELECT for logging details
        gone_details = []
        rows = conn.execute(
            f"SELECT coin_id, wallet_type, amount_mojos, designation, assigned_tier "
            f"FROM coins WHERE coin_id IN ({placeholders}) AND status='free'",
            coin_list
        ).fetchall()
        for row in rows:
            gone_details.append((row["coin_id"], dict(row)))

        # Batch UPDATE
        cursor = conn.execute(
            f"""UPDATE coins SET status='gone', last_seen=?,
                designation='unknown', assigned_tier='none'
                WHERE coin_id IN ({placeholders}) AND status='free'""",
            [now] + coin_list
        )
        count = cursor.rowcount
        conn.commit()
        # Log each individual coin that disappeared with structured data
        for cid, details in gone_details:
            wt = details['wallet_type'].upper()
            if details['wallet_type'] == 'xch':
                amt_str = f"{details['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
            else:
                amt_str = f"{details['amount_mojos']} mojos"
            log_event("debug", "coin_gone",
                      f"{wt} coin {cid[:16]}... GONE from wallet ({amt_str})"
                      f" — was {details['designation']}/{details['assigned_tier']}",
                      data={"coin_id": cid, "amount_mojos": details['amount_mojos'],
                            "wallet_type": details['wallet_type'],
                            "designation": details['designation'],
                            "assigned_tier": details['assigned_tier']})
        if count > 0:
            log_event("info", "coins_gone_summary",
                      f"Marked {count} coins as gone (no longer in wallet)",
                      data={"count": count})
        return count
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to mark coins gone: {e}")
        return 0


def get_free_coins(wallet_type: str) -> List[Dict]:
    """Get all free (available) coins for a wallet type.

    Returns every row from `coins` where status='free', largest first. Callers
    that want tier/designation filtering should inspect the returned dicts'
    `designation` and `assigned_tier` fields — the legacy `tier` column is
    always 'unknown' in current writes (see upsert_coin) and is retained only
    for schema compatibility with older DBs.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM coins WHERE status='free' AND wallet_type=? "
        "ORDER BY amount_mojos DESC",
        [wallet_type],
    ).fetchall()
    return [dict(row) for row in rows]


def get_smallest_free_tier_spare(wallet_type: str) -> Optional[Dict]:
    """Return the smallest free tier-spare coin for a wallet type."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT coin_id, amount_mojos, assigned_tier
        FROM coins
        WHERE status='free'
          AND designation='tier_spare'
          AND wallet_type=?
        ORDER BY amount_mojos ASC, coin_id ASC
        LIMIT 1
        """,
        (wallet_type,),
    ).fetchone()
    if not row:
        return None
    return {
        "coin_id": row["coin_id"],
        "amount_mojos": int(row["amount_mojos"]),
        "assigned_tier": row["assigned_tier"],
    }


    # NOTE: get_all_coins_state() is defined once, further below (after get_coin_summary).
    # A duplicate first definition was removed here.


def get_locked_coins(wallet_type: str = None) -> List[Dict]:
    """Get all locked coins (in active offers).

    Args:
        wallet_type: Optional filter — 'xch' or 'cat'. None = both.

    Returns:
        List of locked coin dicts with coin_id, trade_id, amount_mojos, etc.
    """
    conn = get_connection()
    query = "SELECT * FROM coins WHERE status='locked'"
    params = []

    if wallet_type:
        query += " AND wallet_type=?"
        params.append(wallet_type)

    query += " ORDER BY amount_mojos DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_oversized_locked_offers(max_ratio: float = 1.5,
                                cat_decimals: int = 3) -> List[Dict]:
    """Find open offers whose locked trade coin is too large for the offer.

    This is a recovery guard for tiered coin mode. Normal offers should spend
    a correctly sized tier coin. If a reserve/topup-pool coin or a wildly
    oversized coin becomes locked to a small offer, live topup cannot use that
    coin to rebuild depleted spare pools until the offer is cancelled.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            o.trade_id, o.side, o.size_xch, o.size_cat, o.tier,
            o.lifecycle_state, o.coin_id,
            c.wallet_type, c.amount_mojos, c.designation, c.assigned_tier
        FROM offers o
        JOIN coins c ON c.coin_id = o.coin_id
        WHERE o.status='open'
          AND (o.lifecycle_state IS NULL
               OR o.lifecycle_state NOT IN ('cancel_requested', 'cancel_sent', 'mempool_observed'))
          AND c.status='locked'
        """,
    ).fetchall()

    flagged: List[Dict] = []
    ratio = Decimal(str(max(max_ratio, 1.0)))
    cat_scale = Decimal(10) ** Decimal(int(cat_decimals or 0))
    xch_scale = Decimal("1000000000000")

    for row in rows:
        side = str(row["side"] or "").lower()
        wallet_type = str(row["wallet_type"] or "").lower()
        if side == "buy":
            if wallet_type != "xch":
                continue
            expected = Decimal(str(row["size_xch"] or "0")) * xch_scale
        elif side == "sell":
            if wallet_type != "cat":
                continue
            expected = Decimal(str(row["size_cat"] or "0")) * cat_scale
        else:
            continue

        if expected <= 0:
            continue

        amount = Decimal(int(row["amount_mojos"] or 0))
        designation = str(row["designation"] or "").lower()
        reason = None
        if designation == "reserve":
            reason = "reserve_coin_locked"
        elif amount > expected * ratio:
            reason = "oversized_coin_locked"

        if reason:
            item = dict(row)
            item["expected_mojos"] = int(expected)
            item["ratio"] = str((amount / expected).quantize(Decimal("0.0001")))
            item["reason"] = reason
            flagged.append(item)

    return flagged


def get_coin_summary() -> Dict:
    """Get summary counts by wallet_type and status for the GUI.

    Returns a dict like:
    {
        'xch_free_count': 5, 'xch_free_mojos': 500000000000,
        'xch_locked_count': 3, 'xch_locked_mojos': 300000000000,
        'xch_total': 8,
        'cat_free_count': 4, 'cat_free_mojos': 40000,
        'cat_locked_count': 2, 'cat_locked_mojos': 20000,
        'cat_total': 6,
    }
    """
    conn = get_connection()
    summary = {
        'xch_free_count': 0, 'xch_free_mojos': 0,
        'xch_locked_count': 0, 'xch_locked_mojos': 0,
        'xch_total': 0,
        'cat_free_count': 0, 'cat_free_mojos': 0,
        'cat_locked_count': 0, 'cat_locked_mojos': 0,
        'cat_total': 0,
    }

    rows = conn.execute(
        """SELECT wallet_type, status, COUNT(*) as cnt,
                  COALESCE(SUM(amount_mojos), 0) as total_mojos
           FROM coins
           WHERE status IN ('free', 'locked')
           GROUP BY wallet_type, status"""
    ).fetchall()

    for row in rows:
        wt = row['wallet_type']
        st = row['status']
        key_count = f"{wt}_{st}_count"
        key_mojos = f"{wt}_{st}_mojos"
        if key_count in summary:
            summary[key_count] = row['cnt']
        if key_mojos in summary:
            summary[key_mojos] = row['total_mojos']

    summary['xch_total'] = summary['xch_free_count'] + summary['xch_locked_count']
    summary['cat_total'] = summary['cat_free_count'] + summary['cat_locked_count']

    return summary


def get_all_coins_state() -> Dict[str, Dict]:
    """Get all active coins (free or locked) with their full state.

    Used by the coin-watcher thread to compare wallet state against DB.

    Returns:
        {coin_id: {status, amount_mojos, wallet_type, designation,
                   assigned_tier, trade_id}}
    """
    try:
        conn = get_connection()
        rows = conn.execute(
            """SELECT coin_id, status, amount_mojos, wallet_type,
                      designation, assigned_tier, trade_id
               FROM coins WHERE status IN ('free', 'locked')"""
        ).fetchall()
        return {row['coin_id']: dict(row) for row in rows}
    except Exception:
        return {}


def reconcile_coins_with_wallet(wallet_selectable: dict, wallet_owned: dict,
                                wallet_type: str) -> dict:
    """Full reconciliation: sync DB coin state with wallet reality.

    Compares the DB's view of coins with what the wallet actually has,
    and fixes all discrepancies in a single pass.

    IMPORTANT: Queries ALL coin statuses (including gone/spent) so that
    reappearing coins get properly updated instead of silently skipped
    by INSERT OR IGNORE.

    Args:
        wallet_selectable: {norm_coin_id: amount_mojos} — free/spendable coins
        wallet_owned: {norm_coin_id: amount_mojos} — all held coins (free + locked)
        wallet_type: 'xch' or 'cat'

    Returns:
        dict with counts: {added, marked_gone, freed, locked, already_ok, reappeared}
    """
    stats = {"added": 0, "marked_gone": 0, "freed": 0, "locked": 0,
             "already_ok": 0, "reappeared": 0}

    # CRITICAL FIX: Collect log messages and emit AFTER commit.
    # log_event() uses the same thread-local connection. If its commit()
    # fails, its except handler calls conn.rollback() — which rolls back
    # ALL pending reconciliation changes on the shared connection.
    # This was the root cause of reconciliation "working" (stats counted)
    # but never persisting (DB unchanged after commit).
    deferred_logs = []

    try:
        conn = get_connection()
        now = _now()

        # Derive locked set.
        # Wallet is authoritative: owned - selectable = locked.
        #
        # Previous code tried to cap the locked count by cross-referencing
        # with the offers table ("pick first N"), but sets are unordered so
        # it chose RANDOM coins — leaving real locked coins marked "free".
        #
        # Sage's get_coins(filter_mode="selectable") already filters by
        # asset type, so querying XCH selectable only returns free XCH
        # coins, not phantom CAT coins. The difference IS the locked set.
        wallet_locked_ids = set(wallet_owned.keys()) - set(wallet_selectable.keys())

        # Get ALL DB coins for this wallet type — including gone/spent
        # This prevents INSERT OR IGNORE from silently skipping coins that
        # exist in the DB with a non-active status
        rows = conn.execute(
            "SELECT coin_id, status, amount_mojos, trade_id FROM coins "
            "WHERE wallet_type=?",
            (wallet_type,)
        ).fetchall()

        db_coins = {}
        for r in rows:
            # Normalize DB coin IDs for comparison
            nid = norm_coin_id(r['coin_id'])
            db_coins[nid] = {
                "raw_id": r['coin_id'],  # Keep original for DB updates
                "status": r['status'],
                "amount": r['amount_mojos'],
                "trade_id": r['trade_id'],
            }

        db_ids = set(db_coins.keys())
        wallet_all_ids = set(wallet_owned.keys())

        # 1. STALE: in DB as free/locked but not in wallet → mark gone
        #    Only mark active coins as gone (already gone/spent stay as-is)
        stale = db_ids - wallet_all_ids
        for nid in stale:
            db_status = db_coins[nid]["status"]
            if db_status not in ("free", "locked"):
                continue  # Already gone/spent — nothing to do
            raw_id = db_coins[nid]["raw_id"]
            conn.execute(
                "UPDATE coins SET status='gone', last_seen=? WHERE coin_id=?",
                (now, raw_id)
            )
            stats["marked_gone"] += 1
            amt = db_coins[nid]["amount"]
            deferred_logs.append(("debug", "reconcile_gone",
                      f"{wallet_type.upper()} coin {raw_id[:16]}... "
                      f"marked GONE (was {db_status}, {amt} mojos)"))

        # 2. Coins in wallet — either truly new or reappearing from gone/spent
        for nid in wallet_all_ids:
            is_locked = nid in wallet_locked_ids
            target_status = "locked" if is_locked else "free"
            amt = wallet_owned[nid]
            store_id = nid if nid.startswith("0x") else "0x" + nid

            if nid not in db_ids:
                # Truly new coin — never seen before
                # Auto-classify by amount so it immediately gets a useful
                # tier designation instead of staying 'unknown' until next
                # classification pass.
                new_desig = 'unknown'
                new_atier = 'none'
                try:
                    tier_sizes = _get_reconcile_tier_sizes_mojos(wallet_type)
                    if tier_sizes:
                        new_desig, new_atier = _infer_reconcile_designation_by_size(amt, tier_sizes)
                except Exception:
                    pass

                conn.execute(
                    """INSERT INTO coins
                       (coin_id, wallet_type, amount_mojos, tier, status,
                        first_seen, last_seen, designation, assigned_tier)
                       VALUES (?, ?, ?, 'unknown', ?, ?, ?, ?, ?)
                       ON CONFLICT(coin_id) DO UPDATE SET
                           status = ?,
                           amount_mojos = ?,
                           last_seen = ?,
                           wallet_type = ?""",
                    (store_id, wallet_type, amt, target_status, now, now,
                     new_desig, new_atier,
                     target_status, amt, now, wallet_type)
                )
                stats["added"] += 1
                deferred_logs.append(("debug", "reconcile_add",
                          f"{wallet_type.upper()} coin {store_id[:16]}... "
                          f"ADDED ({amt} mojos, {target_status}, "
                          f"{new_desig}/{new_atier})"))

            else:
                # Coin exists in DB — check if it needs updating
                db_status = db_coins[nid]["status"]
                raw_id = db_coins[nid]["raw_id"]

                if db_status in ("gone", "spent"):
                    # Reappearing coin! Reset to active status
                    conn.execute(
                        """UPDATE coins SET status=?, amount_mojos=?,
                           last_seen=?, designation='unknown',
                           assigned_tier='none', trade_id=NULL
                           WHERE coin_id=?""",
                        (target_status, amt, now, raw_id)
                    )
                    stats["reappeared"] += 1
                    deferred_logs.append(("debug", "reconcile_reappear",
                              f"{wallet_type.upper()} coin {raw_id[:16]}... "
                              f"REAPPEARED (was {db_status}, now {target_status}, "
                              f"{amt} mojos)"))

                elif is_locked and db_status == "free":
                    # Wallet says locked, DB says free → lock it
                    conn.execute(
                        "UPDATE coins SET status='locked', last_seen=? WHERE coin_id=?",
                        (now, raw_id)
                    )
                    stats["locked"] += 1

                elif not is_locked and db_status == "locked":
                    # Wallet says free, DB says locked → free it, but only if no active
                    # trade attribution. Guard: offer_manager may have locked this coin
                    # since we took our wallet snapshot (wallet data is 1 cycle stale).
                    cur = conn.execute(
                        """UPDATE coins SET status='free', trade_id=NULL,
                           last_seen=? WHERE coin_id=? AND trade_id IS NULL""",
                        (now, raw_id)
                    )
                    if cur.rowcount > 0:
                        stats["freed"] += 1
                    else:
                        # trade_id is set → coin was just locked by offer_manager;
                        # trust the offer attribution over the stale wallet snapshot.
                        stats["already_ok"] += 1

                else:
                    stats["already_ok"] += 1

        conn.commit()

        total_changes = (stats["added"] + stats["marked_gone"] + stats["freed"]
                         + stats["locked"] + stats["reappeared"])
        if total_changes > 0:
            deferred_logs.append(("info", "reconcile_complete",
                      f"{wallet_type.upper()} reconciliation: "
                      f"+{stats['added']} new, {stats['reappeared']} reappeared, "
                      f"-{stats['marked_gone']} gone, "
                      f"{stats['locked']} locked, {stats['freed']} freed, "
                      f"{stats['already_ok']} ok"))

    except Exception as e:
        deferred_logs.append(("error", "reconcile_error",
                  f"{wallet_type.upper()} reconciliation failed: {e}"))

    # NOW emit all log events — after the reconciliation transaction is done.
    # This way, if any log_event commit/rollback happens, it can't affect
    # the reconciliation data which is already committed.
    for sev, etype, msg in deferred_logs:
        try:
            log_event(sev, etype, msg)
        except Exception:
            pass

    return stats


def link_offers_to_locked_coins(active_offers: list, cat_asset_id: str) -> dict:
    """Match active offers to their locked coins and assign trade_ids.

    Each offer locks coins on ONE side only — the side being offered:
      - Buy offers (offering XCH) lock one XCH coin only
      - Sell offers (offering CAT) lock one CAT coin only

    The wallet does NOT lock coins on the requested side. The taker
    provides their own coins when they accept the offer.

    Single-pass matching: match each offer to a coin of the type
    being offered (buy→XCH coin, sell→CAT coin) by closest amount.

    Args:
        active_offers: List of normalized offer dicts from wallet
                       (must have 'trade_id' and 'summary' with 'offered'/'requested')
        cat_asset_id: The CAT asset ID string for this trading pair

    Returns:
        dict with counts: {linked, already_linked, unmatched_offers, unmatched_coins}
    """
    stats = {"linked": 0, "already_linked": 0,
             "unmatched_offers": 0, "unmatched_coins": 0}

    try:
        conn = get_connection()
        now = _now()

        # Get all locked coins, grouped by type
        rows = conn.execute(
            "SELECT coin_id, wallet_type, amount_mojos, trade_id "
            "FROM coins WHERE status='locked'"
        ).fetchall()

        # Build pools of unlinked locked coins: {coin_id: amount}
        xch_unlinked = {}
        cat_unlinked = {}
        already_linked_count = 0

        for r in rows:
            if r['trade_id']:
                already_linked_count += 1
                continue
            if r['wallet_type'] == 'xch':
                xch_unlinked[r['coin_id']] = r['amount_mojos']
            elif r['wallet_type'] == 'cat':
                cat_unlinked[r['coin_id']] = r['amount_mojos']

        stats["already_linked"] = already_linked_count

        # PRE-FETCH: Get linked coin counts per trade_id in ONE query
        # instead of running per-offer COUNT queries (which caused 25K+ queries
        # and crashed the bot via log/memory exhaustion).
        _link_counts = {}
        link_rows = conn.execute(
            "SELECT trade_id, COUNT(*) as cnt FROM coins "
            "WHERE status='locked' AND trade_id IS NOT NULL AND trade_id != '' "
            "GROUP BY trade_id"
        ).fetchall()
        for lr in link_rows:
            _link_counts[lr['trade_id']] = lr['cnt']

        def _find_and_link(pool: dict, trade_id: str, target_amount: int) -> bool:
            """Find closest coin in pool, link it to trade_id. Returns True if linked."""
            best_cid = None
            best_diff = float('inf')
            for cid, amt in pool.items():
                diff = abs(amt - target_amount)
                if diff < best_diff:
                    best_diff = diff
                    best_cid = cid
                    if diff == 0:
                        break
            if best_cid:
                conn.execute(
                    "UPDATE coins SET trade_id=?, last_seen=? WHERE coin_id=?",
                    (trade_id, now, best_cid)
                )
                # Backfill offers.coin_id so fill verification can find the coin
                # even when add_offer() was called before the coin was linked.
                conn.execute(
                    "UPDATE offers SET coin_id=? WHERE trade_id=? AND (coin_id IS NULL OR coin_id='')",
                    (best_cid, trade_id)
                )
                del pool[best_cid]
                # Update in-memory count for duplicate detection
                _link_counts[trade_id] = _link_counts.get(trade_id, 0) + 1
                return True
            return False

        # ---- Link the offered side of each offer (one coin per offer) ----
        for offer in active_offers:
            trade_id = offer.get("trade_id", "")
            if not trade_id:
                continue

            # Use pre-fetched count (no per-offer SQL query!)
            # Each offer locks exactly ONE coin (the offered side only)
            existing_cnt = _link_counts.get(trade_id, 0)
            if existing_cnt >= 1:
                stats["already_linked"] += existing_cnt
                continue

            # Parse offer summary
            summary = offer.get("summary") or {}
            offered = summary.get("offered") or {}

            # Extract amounts for each side
            offered_xch = int(offered.get("xch", 0) or 0)
            offered_cat = 0
            for key, val in offered.items():
                if key != "xch" and key != "unknown" and val:
                    offered_cat = int(val)
                    break

            # Link the offered-side coin (only if not already linked)
            if existing_cnt == 0:
                if offered_xch > 0:
                    # Buy → offered side is XCH
                    if _find_and_link(xch_unlinked, trade_id, offered_xch):
                        stats["linked"] += 1
                    else:
                        stats["unmatched_offers"] += 1
                        continue
                elif offered_cat > 0:
                    # Sell → offered side is CAT
                    if _find_and_link(cat_unlinked, trade_id, offered_cat):
                        stats["linked"] += 1
                    else:
                        stats["unmatched_offers"] += 1
                        continue
                else:
                    stats["unmatched_offers"] += 1
                    continue

        # Count remaining unlinked locked coins
        stats["unmatched_coins"] = len(xch_unlinked) + len(cat_unlinked)

        conn.commit()

        if stats["linked"] > 0:
            log_event("info", "offer_coin_link",
                      f"Linked {stats['linked']} offer↔coin pairs "
                      f"({stats['already_linked']} already linked, "
                      f"{stats['unmatched_offers']} unmatched offers, "
                      f"{stats['unmatched_coins']} unmatched coins)")

    except Exception as e:
        log_event("error", "offer_coin_link_error",
                  f"Offer-to-coin linking failed: {e}")

    return stats


# ---------------------------------------------------------------------------
# Coin Designations — role-based coin management (V3 adaptive system)
# ---------------------------------------------------------------------------

def set_coin_designation(coin_id: str, designation: str,
                         assigned_tier: str = "none") -> bool:
    """Mark a coin's role (reserve, tier_spare, tier_active, dust, unknown).

    This is the core of the designation-based system. Coins are what we
    SAY they are, not what their amount implies.

    Args:
        coin_id: The coin to designate
        designation: 'reserve', 'tier_spare', 'tier_active', 'dust', or 'unknown'
        assigned_tier: Which tier this coin serves ('inner', 'mid', 'outer',
                       'extreme', or 'none' for reserve/dust)
    """
    try:
        conn = get_connection()
        # Get current state before updating (for logging changes)
        row = conn.execute(
            "SELECT wallet_type, amount_mojos, designation AS old_desig, assigned_tier AS old_tier FROM coins WHERE coin_id=?",
            (coin_id,)
        ).fetchone()
        cursor = conn.execute(
            """UPDATE coins SET designation=?, assigned_tier=?, last_seen=?
               WHERE coin_id=?""",
            (designation, assigned_tier, _now(), coin_id)
        )
        conn.commit()
        if cursor.rowcount == 0:
            # UPDATE matched nothing — coin_id not in DB yet
            return False
        # Log designation change (skip if unchanged) with structured data
        if row:
            old_d = row['old_desig'] or 'unknown'
            old_t = row['old_tier'] or 'none'
            if old_d != designation or old_t != assigned_tier:
                wt = row['wallet_type'].upper()
                if row['wallet_type'] == 'xch':
                    amt_str = f"{row['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
                else:
                    amt_str = f"{row['amount_mojos']} mojos"
                log_event("debug", "coin_designated",
                          f"{wt} coin {coin_id[:16]}... ({amt_str})"
                          f" {old_d}/{old_t} → {designation}/{assigned_tier}",
                          data={"coin_id": coin_id, "amount_mojos": row['amount_mojos'],
                                "wallet_type": row['wallet_type'],
                                "designation": designation, "assigned_tier": assigned_tier,
                                "old_designation": old_d, "old_assigned_tier": old_t})
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error",
                  f"Failed to set designation for {coin_id[:16]}...: {e}")
        return False


def get_coins_by_designation(wallet_type: str, designation: str,
                             assigned_tier: str = None) -> List[Dict]:
    """Get all coins with a specific designation, optionally filtered by tier.

    Args:
        wallet_type: 'xch' or 'cat'
        designation: 'reserve', 'tier_spare', 'tier_active', 'dust', 'unknown'
        assigned_tier: Optional tier filter ('inner', 'mid', 'outer', 'extreme')

    Returns:
        List of coin dicts sorted by amount descending.
    """
    conn = get_connection()
    query = ("SELECT * FROM coins WHERE wallet_type=? AND designation=? "
             "AND status IN ('free', 'locked')")
    params: list = [wallet_type, designation]

    if assigned_tier:
        query += " AND assigned_tier=?"
        params.append(assigned_tier)

    query += " ORDER BY amount_mojos DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_reserve_coins(wallet_type: str) -> List[Dict]:
    """Get all coins designated as reserve, sorted largest first.

    Args:
        wallet_type: 'xch' or 'cat'

    Returns:
        List of reserve coin dicts (free or locked), largest first.
    """
    return get_coins_by_designation(wallet_type, 'reserve')


def designate_reserve(coin_id: str, wallet_type: str,
                      amount_mojos: int) -> bool:
    """Shorthand to mark a coin as reserve.

    Also logs the designation for visibility.

    Args:
        coin_id: Coin to make reserve
        wallet_type: 'xch' or 'cat'
        amount_mojos: Size (for logging only)
    """
    result = set_coin_designation(coin_id, 'reserve', 'none')
    if result:
        if wallet_type == 'xch':
            xch_val = amount_mojos / 1_000_000_000_000
            log_event("debug", "coin_designated",
                      f"Designated coin {coin_id[:16]}... ({xch_val:.4f} XCH) as reserve")
        else:
            log_event("debug", "coin_designated",
                      f"Designated coin {coin_id[:16]}... ({amount_mojos} mojos) as reserve")
    return result


def cleanup_orphaned_locked_coins(open_trade_ids: set,
                                   wallet_confirmed_locked: set = None) -> dict:
    """Free locked coins whose offers no longer exist.

    After a restart, DB may have coins marked 'locked' with trade_ids
    that no longer correspond to any open offer in the wallet.
    These 'orphaned' locked coins block topup and waste inventory.

    Coins with a trade_id NOT in open_trade_ids → freed (if offer
    was cancelled/expired, the coin is back in the wallet).
    Coins with NO trade_id at all → freed (orphaned from a failed
    offer creation that never completed).

    V5 FIX: wallet_confirmed_locked parameter. If provided, coins in
    this set are NEVER freed regardless of trade_id status. The wallet
    has confirmed these coins have an offer_hash (are genuinely locked
    by an offer). This breaks the tug-of-war where reconcile marks
    coins locked → orphan cleanup frees them → reconcile locks again.

    Args:
        open_trade_ids: Set of trade_ids for currently open wallet offers
        wallet_confirmed_locked: Set of coin_ids the wallet confirms are
            offer-locked (have offer_id/offer_hash set). These are protected.

    Returns:
        dict with counts: {freed_no_trade, freed_stale_trade, skipped_wallet_locked, total_freed}
    """
    stats = {"freed_no_trade": 0, "freed_stale_trade": 0,
             "skipped_wallet_locked": 0, "total_freed": 0}
    if wallet_confirmed_locked is None:
        wallet_confirmed_locked = set()

    # Normalize the wallet_confirmed_locked set for comparison
    _wcl_normalized = set()
    for _wc in wallet_confirmed_locked:
        _wcl_normalized.add(norm_coin_id(_wc))

    try:
        conn = get_connection()
        now = _now()

        # Get all locked coins
        rows = conn.execute(
            "SELECT coin_id, wallet_type, amount_mojos, trade_id, "
            "designation, assigned_tier FROM coins WHERE status='locked'"
        ).fetchall()

        for row in rows:
            cid = row['coin_id']
            tid = row['trade_id']
            wt = row['wallet_type'].upper()
            nid = norm_coin_id(cid)

            # V5 FIX: If wallet confirms this coin is offer-locked, skip it.
            # The wallet is authoritative — it knows the offer_hash.
            if nid in _wcl_normalized:
                stats["skipped_wallet_locked"] += 1
                continue

            if not tid:
                # No trade_id → orphaned locked coin (offer creation failed)
                conn.execute(
                    "UPDATE coins SET status='free', trade_id=NULL, last_seen=? "
                    "WHERE coin_id=?", (now, cid)
                )
                stats["freed_no_trade"] += 1
                if row['wallet_type'] == 'xch':
                    amt_str = f"{row['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
                else:
                    amt_str = f"{row['amount_mojos']} mojos"
                log_event("info", "orphan_freed",
                          f"{wt} coin {cid[:16]}... FREED — no trade_id (orphaned) "
                          f"({amt_str} | {row['designation']}/{row['assigned_tier']})")

            elif tid not in open_trade_ids:
                # Has trade_id but offer no longer exists → stale lock
                conn.execute(
                    "UPDATE coins SET status='free', trade_id=NULL, last_seen=? "
                    "WHERE coin_id=?", (now, cid)
                )
                stats["freed_stale_trade"] += 1
                if row['wallet_type'] == 'xch':
                    amt_str = f"{row['amount_mojos'] / 1_000_000_000_000:.4f} XCH"
                else:
                    amt_str = f"{row['amount_mojos']} mojos"
                log_event("info", "orphan_freed",
                          f"{wt} coin {cid[:16]}... FREED — offer {tid[:12]}... "
                          f"no longer open ({amt_str})")

        conn.commit()
        stats["total_freed"] = stats["freed_no_trade"] + stats["freed_stale_trade"]

        if stats["total_freed"] > 0 or stats["skipped_wallet_locked"] > 0:
            log_event("info", "orphan_cleanup",
                      f"Freed {stats['total_freed']} orphaned locked coins "
                      f"({stats['freed_no_trade']} no trade_id, "
                      f"{stats['freed_stale_trade']} stale trade_id, "
                      f"{stats['skipped_wallet_locked']} protected by wallet)")

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "orphan_cleanup_error",
                  f"Orphaned locked coin cleanup failed: {e}")

    return stats


def coin_sanity_check(open_offer_count: int) -> dict:
    """Periodic sanity check: locked coin count vs open offer count.

    Detects divergence between what the DB thinks is locked vs how many
    offers are actually open. If they diverge significantly, it logs a
    warning so the operator can investigate.

    Also checks for:
    - Locked coins older than 24 hours with no trade_id (definitely orphaned)
    - Free coins that haven't been seen in 24+ hours (stale DB entries)

    Args:
        open_offer_count: Number of truly open offers from wallet RPC

    Returns:
        dict with: {locked_count, offer_count, divergence, stale_locked, warnings}
    """
    warnings = []
    stats = {"locked_count": 0, "offer_count": open_offer_count,
             "divergence": 0, "stale_locked": 0, "warnings": warnings}

    try:
        conn = get_connection()
        now = _now()

        # Count locked coins in DB
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM coins WHERE status='locked'"
        ).fetchone()
        locked_count = row['cnt'] if row else 0
        stats["locked_count"] = locked_count

        # Check divergence
        # Each offer locks coins on BOTH sides, so we expect roughly
        # 2x as many locked coins as offers (1 XCH + 1 CAT per offer).
        # But Sage may not mark both sides. So we just check against
        # a reasonable range.
        expected_locked = open_offer_count  # At minimum 1 coin per offer
        stats["divergence"] = locked_count - expected_locked

        if locked_count > open_offer_count * 3:
            warnings.append(
                f"Too many locked coins: {locked_count} locked vs "
                f"{open_offer_count} open offers (expected ~{open_offer_count * 2})")

        # Check for stale locked coins with no trade_id (older than 1 hour)
        stale_rows = conn.execute(
            "SELECT COUNT(*) as cnt FROM coins "
            "WHERE status='locked' AND (trade_id IS NULL OR trade_id='') "
            "AND last_seen < datetime(?, '-1 hour')",
            (now,)
        ).fetchone()
        stale_count = stale_rows['cnt'] if stale_rows else 0
        stats["stale_locked"] = stale_count

        if stale_count > 0:
            warnings.append(
                f"{stale_count} locked coins have no trade_id and are >1hr old")

        # Log results
        if warnings:
            for w in warnings:
                log_event("warning", "coin_sanity_warning", w)
        else:
            log_event("debug", "coin_sanity_ok",
                      f"Sanity check passed: {locked_count} locked, "
                      f"{open_offer_count} offers, no issues")

    except Exception as e:
        log_event("error", "coin_sanity_error",
                  f"Coin sanity check failed: {e}")

    return stats


def record_trading_pace(fills_hour: int, pace_level: str,
                        active_offers: int) -> bool:
    """Log a trading pace snapshot for adaptive replenishment.

    Called after fill detection to track how busy the market is.
    The coin manager uses the most recent record to decide topup urgency.

    Args:
        fills_hour: Number of fills in the last hour
        pace_level: 'slow', 'normal', or 'busy'
        active_offers: Current number of active offers
    """
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO trading_pace (timestamp, fills_last_hour,
               pace_level, active_offers)
               VALUES (?, ?, ?, ?)""",
            (_now(), fills_hour, pace_level, active_offers)
        )
        conn.commit()
        return True
    except Exception as e:
        log_event("error", "db_error", f"Failed to record trading pace: {e}")
        return False


def get_current_pace() -> str:
    """Get the most recent trading pace level.

    Returns:
        'slow', 'normal', or 'busy'. Defaults to 'normal' if no records.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT pace_level FROM trading_pace ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row['pace_level'] if row else 'normal'


def count_recent_fills(hours: int = 1) -> int:
    """Count fills recorded in the last N hours.

    Used by bot_loop to calculate trading pace.

    Args:
        hours: Lookback window in hours (default 1)

    Returns:
        Number of fills in the window.
    """
    conn = get_connection()
    from datetime import datetime, timezone, timedelta
    cutoff = _sqlite_ts(datetime.now(timezone.utc) - timedelta(hours=hours))
    row = conn.execute(
        """SELECT COUNT(*) as cnt FROM fills
           WHERE filled_at > ?
             AND COALESCE(verification_status, 'legacy') = 'verified'""",
        (cutoff,)
    ).fetchone()
    return row['cnt'] if row else 0


def get_designation_summary(wallet_type: str) -> Dict:
    """Get counts of coins by designation for a wallet type.

    Returns dict like:
    {
        'reserve': {'count': 1, 'total_mojos': 6400000000000},
        'tier_spare': {'count': 12, 'total_mojos': ...},
        'tier_active': {'count': 20, 'total_mojos': ...},
        'dust': {'count': 3, 'total_mojos': ...},
        'unknown': {'count': 0, 'total_mojos': 0},
    }
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT designation,
                  COUNT(*) as cnt,
                  COALESCE(SUM(amount_mojos), 0) as total_mojos
           FROM coins
           WHERE wallet_type=? AND status IN ('free', 'locked')
           GROUP BY designation""",
        (wallet_type,)
    ).fetchall()

    result = {}
    for d in ('reserve', 'tier_spare', 'tier_active', 'dust', 'unknown'):
        result[d] = {'count': 0, 'total_mojos': 0}

    for row in rows:
        desig = row['designation'] or 'unknown'
        if desig in result:
            result[desig] = {
                'count': row['cnt'],
                'total_mojos': row['total_mojos']
            }

    return result


def get_tier_spare_counts(wallet_type: str) -> Dict[str, int]:
    """Get spare coin counts per tier for a wallet type.

    Returns dict like: {'inner': 5, 'mid': 3, 'outer': 5, 'extreme': 2}
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT assigned_tier, COUNT(*) as cnt
           FROM coins
           WHERE wallet_type=? AND designation='tier_spare'
                 AND status='free'
           GROUP BY assigned_tier""",
        (wallet_type,)
    ).fetchall()

    result = {'inner': 0, 'mid': 0, 'outer': 0, 'extreme': 0, 'sniper': 0, 'fees': 0}
    for row in rows:
        tier = row['assigned_tier']
        if tier in result:
            result[tier] = row['cnt']
    return result


def get_live_tier_group_counts() -> Dict[str, Dict[str, int]]:
    """Get dashboard-ready free tier-group counts from DB designations.

    This is the live source of truth for the GUI tier-group card:
    - free `tier_spare` coins count toward their assigned tier
    - free `reserve` coins count toward `reserve`
    - free `dust` coins count toward `dust` (change outputs below the
      smallest tier size; still spendable, just not usable as tier coins
      without consolidation). Showing this row closes the arithmetic gap
      where tier totals wouldn't add up to spendable balance.

    It intentionally ignores locked/tier_active coins because the dashboard
    copy describes the pool as coins still available before a top-up is needed.
    """
    conn = get_connection()
    result = {
        "enabled": True,
        "xch": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0, "sniper": 0, "fees": 0, "reserve": 0, "dust": 0},
        "cat": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0, "sniper": 0, "fees": 0, "reserve": 0, "dust": 0},
        "meta": {"xch_order": "size_tier", "cat_order": "sell_position"},
    }

    rows = conn.execute(
        """SELECT wallet_type, designation, assigned_tier, COUNT(*) as cnt
           FROM coins
           WHERE status='free'
             AND designation IN ('tier_spare', 'reserve', 'dust')
           GROUP BY wallet_type, designation, assigned_tier"""
    ).fetchall()

    for row in rows:
        wallet_type = row["wallet_type"]
        if wallet_type not in ("xch", "cat"):
            continue
        if row["designation"] == "reserve":
            result[wallet_type]["reserve"] += int(row["cnt"] or 0)
            continue
        if row["designation"] == "dust":
            result[wallet_type]["dust"] += int(row["cnt"] or 0)
            continue
        assigned_tier = row["assigned_tier"] or "none"
        if assigned_tier in result[wallet_type]:
            result[wallet_type][assigned_tier] += int(row["cnt"] or 0)

    # Flip XCH (buy side) tier counts from coin-size labels back to position
    # order when BUY_LADDER_REVERSED is on. The DB stores coins by their
    # prepared size tier, but the user thinks in position terms (inner =
    # closest to mid = most likely to fill). Under reverse-buy, position
    # inner uses extreme-sized coins, so we flip for the dashboard and mark
    # the order in metadata for the UI tooltip.
    try:
        from config import cfg as _cfg_tc
        if getattr(_cfg_tc, "BUY_LADDER_REVERSED", False):
            xch = result["xch"]
            xch["inner"], xch["extreme"] = xch["extreme"], xch["inner"]
            xch["mid"], xch["outer"] = xch["outer"], xch["mid"]
            result["meta"]["xch_order"] = "buy_position"
    except Exception:
        pass  # config not available — show as-is

    return result


# ---------------------------------------------------------------------------
# Fills — record fills, match round-trips
# ---------------------------------------------------------------------------

def record_fill(trade_id: str, side: str, price_xch: Decimal, size_xch: Decimal,
                size_cat: Decimal, cat_asset_id: str, tier: str = "unknown",
                verification_status: str = "verified",
                filled_at: str = None, fee_mojos_xch: int = 0) -> int:
    """Record a detected fill.

    Returns the fill_id of the new record, or -1 on error.
    """
    try:
        conn = get_connection()
        existing = conn.execute(
            """SELECT fill_id, side, price_xch, size_xch, size_cat FROM fills
               WHERE trade_id=?
               ORDER BY fill_id DESC
               LIMIT 1""",
            (trade_id,),
        ).fetchone()
        if existing:
            # Idempotent: if fill exists but offer is still open, fix it
            try:
                conn.execute(
                    "UPDATE offers SET status='filled', lifecycle_state='filled', "
                    "filled_at=? WHERE trade_id=? AND status='open'",
                    (_now(), trade_id)
                )
                conn.commit()
            except Exception:
                pass
            # Warn if key parameters differ from what was recorded
            try:
                existing_side = existing["side"]
                existing_price = Decimal(str(existing["price_xch"] or 0))
                if existing_side != side or abs(existing_price - price_xch) > Decimal("0.00000001"):
                    log_event("warning", "record_fill_mismatch",
                              f"Fill {trade_id[:16]}... already recorded but parameters differ: "
                              f"stored side={existing_side} price={existing_price} "
                              f"vs incoming side={side} price={price_xch}")
            except Exception:
                pass
            return int(existing["fill_id"])

        now = filled_at or _now()
        cursor = conn.execute(
            """INSERT INTO fills (trade_id, side, price_xch, size_xch, size_cat,
               filled_at, cat_asset_id, tier, verification_status, fee_mojos_xch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, side, str(price_xch), str(size_xch), str(size_cat),
             now, cat_asset_id, tier, verification_status, int(fee_mojos_xch))
        )
        # Atomically mark offer as filled in the same transaction
        conn.execute(
            "UPDATE offers SET status='filled', lifecycle_state='filled', "
            "filled_at=?, cancelled_at=NULL WHERE trade_id=?",
            (now, trade_id)
        )
        conn.commit()

        # Coin status update is secondary (ok if separate — coin state
        # is recovered at next reconciliation if this fails)
        try:
            coin_ids = get_locked_coin_ids_for_trade(trade_id)
            if not coin_ids:
                row = conn.execute(
                    "SELECT coin_id FROM offers WHERE trade_id=?", (trade_id,)
                ).fetchone()
                coin_id = row["coin_id"] if row else None
                if coin_id:
                    coin_ids = [coin_id]
            for cid in coin_ids:
                mark_coin_spent(cid)
        except Exception:
            pass

        return cursor.lastrowid
    except sqlite3.IntegrityError as ie:
        # F5 fix (2026-04-08): UNIQUE constraint on fills.trade_id may fire if
        # two threads race past the SELECT pre-check above. Treat this as an
        # idempotent success and return the already-recorded fill_id rather
        # than -1, so the caller doesn't double-count.
        #
        # F8 fix (2026-04-08): the original fallback did a single SELECT and
        # returned -1 if it failed. With long-running SQLite WAL contention,
        # the fallback SELECT can itself fail transiently. We now retry the
        # SELECT up to 3 times with brief backoff (0.05s, 0.10s, 0.20s)
        # before giving up. -1 returns are also escalated to a CRITICAL
        # rate-tracked alert (see _record_fill_returns_negative_one_alert
        # at the module level — when we ever lose a fill from PnL we
        # want a loud, fail-loud, fail-fast operator-visible signal).
        try:
            conn.rollback()
        except Exception:
            pass
        for attempt, delay in enumerate((0, 0.05, 0.10, 0.20)):
            if delay:
                try:
                    time.sleep(delay)
                except Exception:
                    pass
            try:
                row = conn.execute(
                    "SELECT fill_id FROM fills WHERE trade_id=? "
                    "ORDER BY fill_id DESC LIMIT 1",
                    (trade_id,)
                ).fetchone()
                if row:
                    log_event("info", "record_fill_race",
                              f"UNIQUE constraint caught race for {trade_id[:16]}... "
                              f"— returning existing fill_id {row['fill_id']}"
                              + (f" (after {attempt} retries)" if attempt else ""))
                    return int(row["fill_id"])
            except Exception:
                pass
        # All retries failed — this is a SILENT FILL LOSS condition.
        # Escalate via the per-hour rate alert in addition to the error log.
        log_event("error", "fill_record_failed_critical",
                  f"CRITICAL: failed to record fill for {trade_id} after "
                  f"IntegrityError + 4 SELECT retries. Fill is silently lost "
                  f"from PnL math. Original error: {ie}",
                  data={"trade_id": trade_id, "side": side,
                        "price_xch": str(price_xch), "size_xch": str(size_xch)})
        return -1
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "fill_record_failed_critical",
                  f"CRITICAL: failed to record fill for {trade_id}: {e}",
                  data={"trade_id": trade_id, "side": side,
                        "price_xch": str(price_xch), "size_xch": str(size_xch)})
        return -1


def update_fill_enrichment(fill_id: int,
                           spent_block_height: Optional[int] = None,
                           header_hash: Optional[str] = None,
                           receive_coin_id: Optional[str] = None,
                           receive_amount_mojos: Optional[int] = None) -> bool:
    """Persist post-fill enrichment data to the fills table (F43, 2026-04-08).

    Called by FillTracker._post_fill_enrichment after walking the
    Coinset additions/removals chain. All four columns are nullable
    so partial enrichment writes are fine — pass only what you know.

    Returns True if at least one column was written, False otherwise.
    """
    if not fill_id or fill_id <= 0:
        return False

    updates: List[str] = []
    params: List = []
    if spent_block_height is not None:
        updates.append("spent_block_height = ?")
        params.append(int(spent_block_height))
    if header_hash is not None:
        updates.append("header_hash = ?")
        params.append(str(header_hash))
    if receive_coin_id is not None:
        updates.append("receive_coin_id = ?")
        params.append(str(receive_coin_id))
    if receive_amount_mojos is not None:
        updates.append("receive_amount_mojos = ?")
        params.append(int(receive_amount_mojos))

    if not updates:
        return False

    params.append(int(fill_id))
    sql = f"UPDATE fills SET {', '.join(updates)} WHERE fill_id = ?"
    try:
        conn = get_connection()
        conn.execute(sql, params)
        conn.commit()
        return True
    except Exception as e:
        log_event("warning", "fill_enrichment_persist_failed",
                  f"Could not write enrichment for fill_id={fill_id}: {e}")
        return False


def backfill_verified_fills_from_offers(limit: int = 50,
                                        since: str = None) -> List[Dict]:
    """Create verified fill rows for offers already marked filled.

    This repairs gaps where an offer was later confirmed filled by wallet/Sage
    housekeeping, but no row was ever inserted into the fills table. Those gaps
    break PnL and fill-rate reporting because dashboard stats read from fills.
    """
    if limit <= 0:
        return []

    try:
        conn = get_connection()
        repaired: List[Dict] = []

        params = [_now()]
        query = """SELECT o.trade_id, o.side, o.price_xch, o.size_xch, o.size_cat,
                          COALESCE(o.filled_at, o.created_at, ?) AS effective_filled_at,
                          o.cat_asset_id, COALESCE(o.tier, 'unknown') AS tier,
                          COALESCE(o.fee_mojos_xch, 0) AS fee_mojos_xch
                   FROM offers o
                   LEFT JOIN fills f ON f.trade_id = o.trade_id
                   WHERE o.status='filled' AND f.trade_id IS NULL"""
        if since:
            query += " AND COALESCE(o.filled_at, o.created_at) >= ?"
            params.append(_sqlite_ts(since))
        query += " ORDER BY COALESCE(o.filled_at, o.created_at) ASC LIMIT ?"
        params.append(int(limit))
        missing_rows = conn.execute(query, params).fetchall()

        for row in missing_rows:
            cursor = conn.execute(
                """INSERT INTO fills (trade_id, side, price_xch, size_xch, size_cat,
                   filled_at, cat_asset_id, tier, verification_status, fee_mojos_xch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)""",
                (
                    row["trade_id"],
                    row["side"],
                    row["price_xch"],
                    row["size_xch"],
                    row["size_cat"],
                    row["effective_filled_at"],
                    row["cat_asset_id"],
                    row["tier"] or "unknown",
                    int(row["fee_mojos_xch"] or 0),
                ),
            )
            repaired.append({
                "fill_id": int(cursor.lastrowid),
                "trade_id": row["trade_id"],
                "side": row["side"],
                "price_xch": row["price_xch"],
                "size_xch": row["size_xch"],
                "size_cat": row["size_cat"],
                "filled_at": row["effective_filled_at"],
                "cat_asset_id": row["cat_asset_id"],
                "tier": row["tier"] or "unknown",
                "verification_status": "verified",
                "created": True,
                "upgraded": False,
            })

        remaining = max(int(limit) - len(repaired), 0)
        if remaining > 0:
            # F48 (2026-04-09): preserve any verification_status starting
            # with 'verified' — previously we only checked for exact
            # equality with 'verified' which meant backfill markers like
            # 'verified_backfill_f48' got silently overwritten, destroying
            # audit provenance for manually-reconstructed fills.
            legacy_rows = conn.execute(
                """SELECT f.fill_id, f.trade_id, f.side, f.price_xch, f.size_xch, f.size_cat,
                          COALESCE(o.filled_at, f.filled_at) AS effective_filled_at,
                          f.cat_asset_id, COALESCE(o.tier, 'unknown') AS tier
                   FROM fills f
                   JOIN offers o ON o.trade_id = f.trade_id
                   WHERE o.status='filled'
                     AND COALESCE(f.verification_status, 'legacy') NOT LIKE 'verified%'
                   ORDER BY COALESCE(o.filled_at, f.filled_at) ASC
                   LIMIT ?""",
                (remaining,),
            ).fetchall()

            for row in legacy_rows:
                conn.execute(
                    """UPDATE fills
                       SET verification_status='verified',
                           filled_at=COALESCE(filled_at, ?)
                       WHERE fill_id=?""",
                    (row["effective_filled_at"], row["fill_id"]),
                )
                repaired.append({
                    "fill_id": int(row["fill_id"]),
                    "trade_id": row["trade_id"],
                    "side": row["side"],
                    "price_xch": row["price_xch"],
                    "size_xch": row["size_xch"],
                    "size_cat": row["size_cat"],
                    "filled_at": row["effective_filled_at"],
                    "cat_asset_id": row["cat_asset_id"],
                    "tier": row["tier"] or "unknown",
                    "verification_status": "verified",
                    "created": False,
                    "upgraded": True,
                })

        if repaired:
            conn.commit()

        return repaired
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to backfill verified fills: {e}")
        return []


def get_fills(cat_asset_id: str = None, side: str = None,
              since: str = None, limit: int = 100,
              include_legacy: bool = False) -> List[Dict]:
    """Get fills, optionally filtered.

    Args:
        cat_asset_id: Filter by CAT pair
        side: Filter by 'buy' or 'sell'
        since: ISO timestamp — only fills after this time
        limit: Max number of fills to return

    Returns list of fill dicts, newest first.
    """
    conn = get_connection()
    query = "SELECT * FROM fills WHERE 1=1"
    params = []

    if not include_legacy:
        query += " AND COALESCE(verification_status, 'legacy') = 'verified'"

    if cat_asset_id:
        query += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    if side:
        query += " AND side=?"
        params.append(side)
    if since:
        query += " AND filled_at>=?"
        params.append(since)

    query += " ORDER BY filled_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_unmatched_fills(cat_asset_id: str, side: str,
                        since: str = None) -> List[Dict]:
    """Get fills that haven't been matched into round-trips yet.

    Used by the PnL engine to pair buys with sells (FIFO matching).
    """
    conn = get_connection()
    params = [cat_asset_id, side]
    query = """SELECT * FROM fills
           WHERE cat_asset_id=? AND side=? AND round_trip_id IS NULL
             AND COALESCE(verification_status, 'legacy') = 'verified'"""
    if since:
        query += " AND filled_at>=?"
        params.append(_sqlite_ts(since))
    query += " ORDER BY filled_at ASC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def match_round_trip(buy_fill_id: int, sell_fill_id: int,
                     pnl_xch: Decimal) -> int:
    """Link a buy fill and sell fill as a completed round-trip.

    Args:
        buy_fill_id: The buy fill's ID
        sell_fill_id: The sell fill's ID
        pnl_xch: Profit/loss in XCH for this round-trip

    Returns a round_trip_id (just uses the buy_fill_id as the ID).
    """
    round_trip_id = buy_fill_id  # Simple: use the buy fill's ID
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE fills SET round_trip_id=?, pnl_xch=? WHERE fill_id=?",
            (round_trip_id, str(pnl_xch), buy_fill_id)
        )
        conn.execute(
            "UPDATE fills SET round_trip_id=?, pnl_xch=? WHERE fill_id=?",
            (round_trip_id, str(pnl_xch), sell_fill_id)
        )
        conn.commit()
        return round_trip_id
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to match round-trip: {e}")
        return -1


# ---------------------------------------------------------------------------
# Inventory — track net position over time
# ---------------------------------------------------------------------------

def record_inventory_snapshot(cat_asset_id: str, net_position: Decimal,
                               xch_balance: Decimal = None,
                               cat_balance: Decimal = None,
                               mid_price: Decimal = None,
                               unrealised_pnl: Decimal = None) -> bool:
    """Save a snapshot of current inventory position.

    Called after each fill to track how position changes over time.
    """
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO inventory (timestamp, cat_asset_id, net_position,
               xch_balance, cat_balance, mid_price)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), cat_asset_id, str(net_position),
             str(xch_balance) if xch_balance is not None else None,
             str(cat_balance) if cat_balance is not None else None,
             str(mid_price) if mid_price is not None else None)
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("error", "db_error", f"Failed to record inventory snapshot: {e}")
        return False


def get_net_position(cat_asset_id: str, since: str = None) -> Decimal:
    """Get the current net position for a CAT pair.

    Calculated from fills: sum of buy sizes minus sum of sell sizes.
    Positive = long CAT (accumulated more than sold).
    Negative = short CAT (sold more than accumulated).
    If `since` is provided, only fills from that timestamp onward are used.
    """
    conn = get_connection()

    query = (
        "SELECT side, size_cat FROM fills WHERE cat_asset_id=? "
        "AND COALESCE(verification_status, 'legacy') != 'phantom'"
    )
    params = [cat_asset_id]
    if since:
        query += " AND filled_at>=?"
        params.append(_sqlite_ts(since))

    rows = conn.execute(
        query,
        params,
    ).fetchall()
    net = sum(
        Decimal(str(r["size_cat"])) * (Decimal("1") if r["side"] == "buy" else Decimal("-1"))
        for r in rows
    )
    return net


# ---------------------------------------------------------------------------
# Price History — for volatility calculation
# ---------------------------------------------------------------------------

def record_price(cat_asset_id: str, combined_price: Decimal,
                 dexie_price: Decimal = None, tibet_price: Decimal = None,
                 strategy_used: str = None) -> bool:
    """Save a price data point for volatility tracking."""
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO price_history (timestamp, cat_asset_id, dexie_price,
               tibet_price, combined_price, strategy_used)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), cat_asset_id,
             str(dexie_price) if dexie_price is not None else None,
             str(tibet_price) if tibet_price is not None else None,
             str(combined_price), strategy_used)
        )
        conn.commit()
        return True
    except Exception:
        # CRITICAL: rollback on failure to release the write lock.
        # Without this, a failed commit leaves an open transaction on the
        # thread-local connection, holding a RESERVED lock that blocks
        # ALL other writers (including batch_cancel during startup).
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def get_recent_prices(cat_asset_id: str, hours: float = 4.0,
                      limit: int = 500) -> List[Dict]:
    """Get recent price history for volatility calculation.

    Args:
        hours: How far back to look
        limit: Max data points to return
    """
    conn = get_connection()
    # Calculate cutoff time
    from datetime import timedelta
    cutoff = _sqlite_ts(datetime.now(timezone.utc) - timedelta(hours=hours))

    rows = conn.execute(
        """SELECT * FROM price_history
           WHERE cat_asset_id=? AND timestamp>=?
           ORDER BY timestamp ASC LIMIT ?""",
        (cat_asset_id, cutoff, limit)
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Events — replaces add_log() and debug_log()
# ---------------------------------------------------------------------------

# SSE callback — set by api_server.py so log_event can push to the console
_sse_callback = None


def set_log_sse_callback(callback):
    """Register an SSE callback so log_event pushes to the live console.

    Called once from api_server.py at startup:
        from database import set_log_sse_callback
        set_log_sse_callback(events.emit)
    """
    global _sse_callback
    _sse_callback = callback


def log_event(severity: str, event_type: str, message: str,
              data: Dict = None) -> bool:
    """Log an event to the database AND push to the live console via SSE.

    This replaces the V1 add_log() function. The GUI log panel
    reads from the events table instead of an in-memory list.
    The pop-out console receives events in real-time via SSE.

    Args:
        severity: 'info', 'success', 'warning', or 'error'
        event_type: Category like 'fill', 'offer_created', 'price_change', etc.
        message: Human-readable description
        data: Optional dict with structured data (stored as JSON)
    """
    now = _now()

    # ALWAYS push to SSE first — even if DB write fails (e.g. locked),
    # the console and system log still get the event in real-time.
    if _sse_callback:
        try:
            payload = {
                "severity": severity,
                "event_type": event_type,
                "message": message,
                "timestamp": now,
            }
            if isinstance(data, dict):
                payload["data"] = data
                for key, value in data.items():
                    if key not in payload:
                        payload[key] = value
            _sse_callback("log", payload)
        except Exception:
            pass  # Don't let SSE errors affect logging

    # Then persist to database (best-effort — DB locks shouldn't kill events)
    try:
        # Auto-tag with event category from taxonomy (best-effort)
        event_category = None
        try:
            from event_taxonomy import categorize_event
            event_category = categorize_event(event_type)
        except Exception:
            pass

        conn = get_connection()
        # The events.severity column has a CHECK constraint allowing only
        # info / success / warning / error. Callers that log incidents at
        # "critical" (oracle hard-pause, ladder/topup/prep zombies, etc.)
        # would silently fail the INSERT — the event flashes in SSE and
        # then vanishes from the DB-backed log, so post-mortem review
        # after a GUI refresh couldn't find the worst incidents. Map
        # critical to "error" at the storage layer to keep persistence
        # reliable while the live SSE stream (emitted above at line
        # 3446) still carries the original "critical" severity for
        # high-visibility UI rendering.
        db_severity = "error" if str(severity).lower() == "critical" else severity

        # Short-timeout write path.
        # The connection's default busy_timeout is 5000 ms so queries
        # wait up to 5 s on a locked writer. During log storms from
        # watchers/posters that adds 5 s of latency to every hot-path
        # thread that happens to call log_event, amplifying the DB
        # contention it's trying to observe. Drop the timeout to 500 ms
        # for this specific insert; on contention we'd rather lose the
        # low-severity log than stall the cycle, and high-severity logs
        # get one-shot retry at the full timeout via the OperationalError
        # branch below.
        try:
            conn.execute("PRAGMA busy_timeout=500")
            if event_category:
                conn.execute(
                    """INSERT INTO events (timestamp, event_type, severity, message, data, event_category)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (now, event_type, db_severity, message,
                     json.dumps(data) if data else None, event_category)
                )
            else:
                conn.execute(
                    """INSERT INTO events (timestamp, event_type, severity, message, data)
                       VALUES (?, ?, ?, ?, ?)""",
                    (now, event_type, db_severity, message,
                     json.dumps(data) if data else None)
                )
            conn.commit()
        except sqlite3.OperationalError as _lock_err:
            if "locked" in str(_lock_err).lower() and db_severity in ("error", "warning"):
                # High-severity event: don't drop silently. Retry once at the
                # full connection timeout before giving up.
                try:
                    conn.execute("PRAGMA busy_timeout=5000")
                    if event_category:
                        conn.execute(
                            """INSERT INTO events (timestamp, event_type, severity, message, data, event_category)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (now, event_type, db_severity, message,
                             json.dumps(data) if data else None, event_category)
                        )
                    else:
                        conn.execute(
                            """INSERT INTO events (timestamp, event_type, severity, message, data)
                               VALUES (?, ?, ?, ?, ?)""",
                            (now, event_type, db_severity, message,
                             json.dumps(data) if data else None)
                        )
                    conn.commit()
                except Exception:
                    raise  # fall through to outer except rollback
            else:
                # Low-severity event under contention — drop and release
                # the lock fast so hot-path threads aren't stalled. The
                # SSE stream already delivered this event in real time
                # (see line 3446) so operators still see it live; only
                # the DB-backed history loses this entry under load.
                try:
                    conn.rollback()
                except Exception:
                    pass
                return False
        finally:
            # Always restore the connection-level timeout so other
            # callers on this thread-local connection behave normally.
            try:
                conn.execute("PRAGMA busy_timeout=5000")
            except Exception:
                pass
        return True
    except Exception:
        # CRITICAL: rollback on failure to release the write lock.
        # Without this, a failed commit leaves the thread-local connection
        # holding a RESERVED lock that blocks ALL other writers permanently.
        # This was the root cause of the "database is locked" cascade —
        # the splash-output thread fires rapid log_events, the first
        # commit fails, and the RESERVED lock is never released.
        # NOTE: conn.rollback() does NOT call log_event, so no recursion.
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def get_recent_events(limit: int = 50, severity: str = None,
                      event_type: str = None,
                      category: str = None) -> List[Dict]:
    """Get recent events for the GUI log panel.

    Args:
        limit: Max events to return
        severity: Filter by severity level
        event_type: Filter by event type
        category: Filter by event category (lifecycle/offer/pricing/wallet/
                  exchange/risk/system/coin) — uses event_taxonomy categories
    """
    conn = get_connection()
    query = "SELECT * FROM events WHERE 1=1"
    params = []

    if severity:
        query += " AND severity=?"
        params.append(severity)
    if event_type:
        query += " AND event_type=?"
        params.append(event_type)
    if category:
        query += " AND event_category=?"
        params.append(category)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_events_since(since: str, limit: int = 100,
                     category: str = None) -> List[Dict]:
    """Get events newer than a given timestamp (for GUI clear support).

    Args:
        since: ISO timestamp string — only events after this are returned
        limit: Max events to return
        category: Optional category filter (see get_recent_events)
    """
    # DB stores timestamps as "2026-04-20 02:52:06.xxx" (space separator, no tz).
    # Callers may pass ISO format "2026-04-20T02:52:06+00:00" (T + tz offset).
    # ASCII space (32) < T (84), so without normalization all space-format rows
    # compare as less than any T-format cutoff, returning nothing.
    since_normalized = since.replace("T", " ").split("+")[0].split("Z")[0]
    conn = get_connection()
    if category:
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp > ? AND event_category=?"
            " ORDER BY timestamp DESC LIMIT ?",
            (since_normalized, category, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
            (since_normalized, limit)
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Config History — track setting changes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Utility — stats, cleanup, backup
# ---------------------------------------------------------------------------

def get_stats(cat_asset_id: str = None, since: str = None) -> Dict:
    """Get summary statistics for the dashboard.

    Returns counts of open offers, total fills, realised PnL, etc.
    """
    conn = get_connection()
    stats = {}

    # Open offers count
    open_lifecycle_clause = _actionable_open_lifecycle_clause()
    if cat_asset_id:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM offers "
            f"WHERE status='open' AND {open_lifecycle_clause} AND cat_asset_id=?",
            (cat_asset_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM offers "
            f"WHERE status='open' AND {open_lifecycle_clause}"
        ).fetchone()
    stats["open_offers"] = row["cnt"]

    # Open offers by side
    query_base = (
        "SELECT side, COUNT(*) as cnt FROM offers "
        f"WHERE status='open' AND {open_lifecycle_clause}"
    )
    params = []
    if cat_asset_id:
        query_base += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    query_base += " GROUP BY side"
    rows = conn.execute(query_base, params).fetchall()
    stats["open_buys"] = 0
    stats["open_sells"] = 0
    for row in rows:
        if row["side"] == "buy":
            stats["open_buys"] = row["cnt"]
        elif row["side"] == "sell":
            stats["open_sells"] = row["cnt"]

    # Total fills
    query_base = "SELECT COUNT(*) as cnt FROM fills WHERE COALESCE(verification_status, 'legacy') = 'verified'"
    params = []
    if cat_asset_id:
        query_base += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    if since:
        query_base += " AND filled_at>=?"
        params.append(_sqlite_ts(since))
    row = conn.execute(query_base, params).fetchone()
    stats["total_fills"] = row["cnt"]

    # Realised PnL (from matched round-trips)
    query_base = """SELECT pnl_xch FROM fills
                    WHERE round_trip_id IS NOT NULL AND side='buy'
                      AND COALESCE(verification_status, 'legacy') = 'verified'
                      AND pnl_xch IS NOT NULL"""
    params = []
    if cat_asset_id:
        query_base += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    if since:
        query_base += " AND filled_at>=?"
        params.append(_sqlite_ts(since))
    rows = conn.execute(query_base, params).fetchall()
    stats["realised_pnl_xch"] = str(sum((Decimal(str(r["pnl_xch"])) for r in rows), Decimal("0")))

    # Round-trip stats
    query_base = """SELECT COUNT(*) as cnt FROM fills
                    WHERE round_trip_id IS NOT NULL AND side='buy'
                      AND COALESCE(verification_status, 'legacy') = 'verified'"""
    params = []
    if cat_asset_id:
        query_base += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    if since:
        query_base += " AND filled_at>=?"
        params.append(_sqlite_ts(since))
    row = conn.execute(query_base, params).fetchone()
    stats["round_trips"] = row["cnt"]

    # Win rate (profitable round-trips / total round-trips)
    if stats["round_trips"] > 0:
        query_base = """SELECT COUNT(*) as cnt FROM fills
                        WHERE round_trip_id IS NOT NULL AND side='buy'
                        AND COALESCE(verification_status, 'legacy') = 'verified'
                        AND CAST(pnl_xch AS REAL) > 0"""
        params = []
        if cat_asset_id:
            query_base += " AND cat_asset_id=?"
            params.append(cat_asset_id)
        if since:
            query_base += " AND filled_at>=?"
            params.append(_sqlite_ts(since))
        row = conn.execute(query_base, params).fetchone()
        stats["win_rate"] = round(row["cnt"] / stats["round_trips"] * 100, 1)
    else:
        stats["win_rate"] = 0

    # Fill counts by side
    for side in ["buy", "sell"]:
        query_base = """SELECT COUNT(*) as cnt FROM fills
                        WHERE side=? AND COALESCE(verification_status, 'legacy') = 'verified'"""
        params = [side]
        if cat_asset_id:
            query_base += " AND cat_asset_id=?"
            params.append(cat_asset_id)
        if since:
            query_base += " AND filled_at>=?"
            params.append(_sqlite_ts(since))
        row = conn.execute(query_base, params).fetchone()
        stats[f"{side}_fills"] = row["cnt"]

    # Verified fill rate (last hour)
    from datetime import datetime, timezone, timedelta as _timedelta
    _cutoff_1h = _sqlite_ts(datetime.now(timezone.utc) - _timedelta(hours=1))
    query_base = """SELECT COUNT(*) as cnt FROM fills
                    WHERE filled_at > ?
                      AND COALESCE(verification_status, 'legacy') = 'verified'"""
    params = [_cutoff_1h]
    if cat_asset_id:
        query_base += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    if since:
        query_base += " AND filled_at>=?"
        params.append(_sqlite_ts(since))
    row = conn.execute(query_base, params).fetchone()
    stats["fill_rate_per_hour"] = float(row["cnt"] or 0)

    # Net position
    if cat_asset_id:
        stats["net_position"] = str(get_net_position(cat_asset_id, since=since))
    else:
        stats["net_position"] = "0"

    # Unmatched fills by side (open legs waiting for a round-trip partner)
    for side in ["buy", "sell"]:
        query_base = """SELECT COUNT(*) as cnt FROM fills
                        WHERE side=? AND round_trip_id IS NULL
                          AND COALESCE(verification_status, 'legacy') = 'verified'"""
        params = [side]
        if cat_asset_id:
            query_base += " AND cat_asset_id=?"
            params.append(cat_asset_id)
        if since:
            query_base += " AND filled_at>=?"
            params.append(_sqlite_ts(since))
        row = conn.execute(query_base, params).fetchone()
        stats[f"unmatched_{side}_fills"] = row["cnt"]

    # Volume traded (total XCH and CAT across all verified fills)
    query_base = """SELECT side, size_xch, size_cat,
                           COALESCE(fee_mojos_xch, 0) AS fee_mojos_xch
                    FROM fills
                    WHERE COALESCE(verification_status, 'legacy') = 'verified'
                      AND (size_xch IS NOT NULL OR size_cat IS NOT NULL)"""
    params = []
    if cat_asset_id:
        query_base += " AND cat_asset_id=?"
        params.append(cat_asset_id)
    if since:
        query_base += " AND filled_at>=?"
        params.append(_sqlite_ts(since))
    rows = conn.execute(query_base, params).fetchall()

    # Split by side so the dashboard can show "bought vs sold" gross amounts.
    # Buy fill = we paid XCH and received CAT.
    # Sell fill = we received XCH and gave CAT.
    _buy_xch = Decimal("0")
    _buy_cat = Decimal("0")
    _sell_xch = Decimal("0")
    _sell_cat = Decimal("0")
    _fee_mojos = 0
    for r in rows:
        _sx = Decimal(str(r["size_xch"] or 0))
        _sc = Decimal(str(r["size_cat"] or 0))
        try:
            _fee_mojos += int(r["fee_mojos_xch"] or 0)
        except Exception:
            pass
        if r["side"] == "buy":
            _buy_xch += _sx
            _buy_cat += _sc
        elif r["side"] == "sell":
            _sell_xch += _sx
            _sell_cat += _sc

    stats["volume_xch"] = str(_buy_xch + _sell_xch)
    stats["volume_cat"] = str(_buy_cat + _sell_cat)
    # Per-side gross volumes — what the user actually traded on each side.
    stats["buy_volume_xch"] = str(_buy_xch)   # XCH spent acquiring CAT
    stats["buy_volume_cat"] = str(_buy_cat)   # CAT received from buys
    stats["sell_volume_xch"] = str(_sell_xch) # XCH received from sells
    stats["sell_volume_cat"] = str(_sell_cat) # CAT delivered from sells
    # Net cashflow — the simple "did I end up with more XCH or less?" number.
    # This is GROSS (doesn't match round-trip PnL; it's the raw XCH delta
    # from all fills, positive = we took in more XCH than we paid out).
    stats["net_xch_flow"] = str(_sell_xch - _buy_xch)
    _fee_xch = Decimal(_fee_mojos) / Decimal("1000000000000")
    stats["fee_xch"] = str(_fee_xch)
    stats["net_xch_flow_after_fees"] = str((_sell_xch - _buy_xch) - _fee_xch)
    # Net CAT inventory change — how the position shifted this window.
    # Positive = we net bought CAT (gained inventory), negative = net sold.
    stats["net_cat_flow"] = str(_buy_cat - _sell_cat)

    # Average fill size (XCH)
    if stats["total_fills"] > 0:
        query_base = """SELECT size_xch FROM fills
                        WHERE COALESCE(verification_status, 'legacy') = 'verified'
                          AND size_xch IS NOT NULL"""
        params = []
        if cat_asset_id:
            query_base += " AND cat_asset_id=?"
            params.append(cat_asset_id)
        if since:
            query_base += " AND filled_at>=?"
            params.append(_sqlite_ts(since))
        rows = conn.execute(query_base, params).fetchall()
        if rows:
            total = sum((Decimal(str(r["size_xch"])) for r in rows), Decimal("0"))
            stats["avg_fill_size_xch"] = str(total / Decimal(len(rows)))
        else:
            stats["avg_fill_size_xch"] = "0"
    else:
        stats["avg_fill_size_xch"] = "0"

    # Average round trip time (seconds between buy and sell legs of a matched pair)
    if stats["round_trips"] > 0:
        try:
            query_base = """SELECT ABS(AVG((julianday(f2.filled_at) - julianday(f1.filled_at)) * 86400)) AS avg_secs
                            FROM fills f1
                            JOIN fills f2 ON f1.round_trip_id = f2.round_trip_id
                                         AND f1.side != f2.side
                            WHERE f1.side = 'buy' AND f1.round_trip_id IS NOT NULL
                              AND COALESCE(f1.verification_status, 'legacy') = 'verified'"""
            params = []
            if cat_asset_id:
                query_base += " AND f1.cat_asset_id=?"
                params.append(cat_asset_id)
            if since:
                query_base += " AND f1.filled_at>=?"
                params.append(_sqlite_ts(since))
            row = conn.execute(query_base, params).fetchone()
            stats["avg_round_trip_secs"] = float(row["avg_secs"] or 0)
        except Exception:
            stats["avg_round_trip_secs"] = 0
    else:
        stats["avg_round_trip_secs"] = 0

    # Average PnL per round trip
    if stats["round_trips"] > 0:
        stats["avg_pnl_per_trip_xch"] = str(
            Decimal(stats["realised_pnl_xch"]) / Decimal(stats["round_trips"])
        )
    else:
        stats["avg_pnl_per_trip_xch"] = "0"

    return stats


def record_config_change(key: str, old_value, new_value,
                         source: str = "unknown", note: str = "") -> bool:
    """F26 (2026-04-08): write+read audit trail for live config changes.

    Records the who/what/when of every config change so post-mortem
    investigation has a definitive log when something breaks after a
    settings tweak. Called from cfg.update() in config.py.

    Args:
        key: config key name (e.g. "BASE_SPREAD_BPS")
        old_value: previous value (any type — coerced to str)
        new_value: new value (any type — coerced to str)
        source: where the change came from (e.g. "gui_live_control",
                "smart_settings", "api", "startup")
        note: optional human-readable explanation
    """
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO config_history
               (timestamp, key, old_value, new_value, source, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), str(key), str(old_value or ""), str(new_value or ""),
             str(source or "unknown"), str(note or ""))
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # Audit logging is best-effort — never block a config change on
        # an audit failure. The change still happens, just unaudited.
        log_event("debug", "config_audit_failed",
                  f"Failed to write config_history row for {key}: {e}")
        return False


def get_config_history(limit: int = 100, key: str = None,
                       since_hours: int = None) -> List[Dict]:
    """Read recent config change audit rows.

    F26 (2026-04-08). Used by the API endpoint that surfaces the audit
    trail to the operator dashboard.

    Args:
        limit: max rows to return
        key: filter to a specific config key (None = all keys)
        since_hours: only return rows from the last N hours (None = all)
    """
    try:
        conn = get_connection()
        sql = "SELECT id, timestamp, key, old_value, new_value, source, note FROM config_history"
        params = []
        clauses = []
        if key:
            clauses.append("key = ?")
            params.append(str(key))
        if since_hours and since_hours > 0:
            clauses.append("timestamp > datetime('now', ? )")
            params.append(f"-{int(since_hours)} hours")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def get_wal_size_mb() -> float:
    """Return the current size of the SQLite WAL file in MB.

    F22 (2026-04-08): WAL monitoring helper. The write-ahead log
    grows when checkpoints can't keep up with writes (long-running
    readers, busy writes). If left unchecked it can grow into the
    GBs and cause slow startup + disk pressure.
    """
    try:
        wal_path = DB_PATH + "-wal"
        if not os.path.exists(wal_path):
            return 0.0
        return os.path.getsize(wal_path) / (1024 * 1024)
    except Exception:
        return 0.0


def force_wal_checkpoint() -> bool:
    """Force a WAL checkpoint with TRUNCATE mode.

    Returns True on success, False on failure. Uses TRUNCATE mode
    which drops the WAL file size to zero after the checkpoint
    completes (vs PASSIVE which leaves the file at its high-water
    mark). May briefly block other writers during the truncate.

    F22 (2026-04-08).
    """
    try:
        conn = get_connection()
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # Result is (busy_count, log_pages, checkpointed_pages)
        if result is not None:
            log_event("debug", "wal_checkpoint",
                      f"WAL checkpoint: busy={result[0]}, log_pages={result[1]}, "
                      f"checkpointed={result[2]}")
            return result[0] == 0  # 0 = no busy readers blocked us
        return True
    except Exception as e:
        log_event("warning", "wal_checkpoint_failed",
                  f"WAL checkpoint failed: {e}")
        return False


def cleanup_old_pool_snapshots(days: int = 30) -> int:
    """Remove pool snapshots older than the specified number of days.

    Prevents the pool_snapshots table from growing unbounded.
    Returns the number of rows deleted.
    """
    try:
        conn = get_connection()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute("DELETE FROM pool_snapshots WHERE timestamp < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def cleanup_old_trading_pace(days: int = 7) -> int:
    """Remove trading_pace entries older than the specified number of days.

    Prevents the trading_pace table from growing unbounded.
    Returns the number of rows deleted.
    """
    try:
        conn = get_connection()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute("DELETE FROM trading_pace WHERE timestamp < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def cleanup_old_events(days: int = 30, severity_keep_days: int = 90) -> int:
    """Remove old rows from the events table.

    Without retention, every log_event() append over weeks/months grows
    the DB without bound, slows GUI queries, and bloats backups. We
    keep the full 30-day info/debug history plus a 90-day tail of
    errors/warnings so incident investigations still have context, but
    anything older than that is pruned.

    Returns the number of rows deleted.
    """
    try:
        conn = get_connection()
        now = datetime.now(timezone.utc)
        info_cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        severity_cutoff = (now - timedelta(days=severity_keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        # Delete older info/debug/success events first.
        cur_info = conn.execute(
            "DELETE FROM events "
            "WHERE timestamp < ? "
            "  AND (severity IS NULL OR severity NOT IN ('error', 'warning', 'critical'))",
            (info_cutoff,),
        )
        # Then prune the long tail of errors/warnings beyond the incident window.
        cur_sev = conn.execute(
            "DELETE FROM events "
            "WHERE timestamp < ? "
            "  AND severity IN ('error', 'warning', 'critical')",
            (severity_cutoff,),
        )
        conn.commit()
        return (cur_info.rowcount or 0) + (cur_sev.rowcount or 0)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def backup_database(backup_path: str = None) -> str:
    """Create a backup of the database.

    Args:
        backup_path: Where to save the backup. Defaults to
                     <data_dir>/backups/bot_backup_YYYYMMDD_HHMMSS.db

    Returns the path to the backup file.
    """
    if not backup_path:
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            from user_paths import backups_dir
            backup_dir = backups_dir()
        except Exception:
            backup_dir = os.path.dirname(DB_PATH)
        backup_path = os.path.join(backup_dir, f"bot_backup_{date_str}.db")

    conn = get_connection()
    backup_conn = sqlite3.connect(backup_path)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()

    log_event("info", "backup", f"Database backed up to {backup_path}")

    # Retention: keep the 10 most recent backups, prune older ones.
    # Runs best-effort — failures shouldn't break the backup itself.
    try:
        _prune_old_backups(os.path.dirname(backup_path), keep=10)
    except Exception as _prune_err:
        print(f"[database] backup retention prune failed: {_prune_err}", flush=True)

    return backup_path


def _prune_old_backups(backup_dir: str, keep: int = 10) -> int:
    """Delete all but the `keep` most recent bot_backup_*.db files.

    Returns the number of files deleted.
    """
    import glob
    try:
        files = glob.glob(os.path.join(backup_dir, "bot_backup_*.db"))
        if len(files) <= keep:
            return 0
        # Sort by modified time, newest first
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        to_delete = files[keep:]
        deleted = 0
        for f in to_delete:
            try:
                os.remove(f)
                deleted += 1
            except Exception:
                pass
        return deleted
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Bot Settings — simple key-value store (persists across restarts)
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = None) -> str:
    """Get a setting value by key. Returns default if not found."""
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else default


def get_all_settings() -> List[Dict]:
    """Return all persistent app settings for support/debug snapshots."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT key, value, updated_at FROM bot_settings ORDER BY key ASC"
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def set_setting(key: str, value: str) -> bool:
    """Set a setting value (insert or update).

    Returns True on successful commit, False if the write failed.
    Callers that maintain counters (topup_pool_*_spent_mojos refunds,
    etc.) MUST check the return value — the old signature returned
    None and silently swallowed the exception, which let topup budget
    counters drift without any signal, eventually producing permanent
    refill lock-out that looked like smart-settings misconfiguration.
    """
    try:
        conn = get_connection()
        conn.execute(
            """INSERT INTO bot_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value, _sqlite_ts(datetime.now(timezone.utc)))
        )
        conn.commit()
        return True
    except Exception as _set_err:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            log_event(
                "warning",
                "set_setting_failed",
                f"bot_settings write failed for key={key!r}: {_set_err}. "
                f"Callers that maintain counters should treat this as a "
                f"hard failure and re-queue the intended update.",
                data={"key": key},
            )
        except Exception:
            # Logging itself may fail during a disk-full scenario — don't
            # cascade the failure further. Returning False is enough.
            pass
        return False


# ---------------------------------------------------------------------------
# V3: Splash Incoming Offers — received from the P2P network
# ---------------------------------------------------------------------------

def record_splash_incoming(offer_bech32: str, fingerprint: str,
                           pair_hint: str = None, source_ip: str = None) -> bool:
    """Record an offer received from the Splash P2P network.

    Args:
        offer_bech32: The offer1... bech32 string received from a peer
        fingerprint: SHA256 fingerprint of the offer (for dedup)
        pair_hint: Optional hint about which pair this offer is for
        source_ip: Optional IP address of the sending peer

    Returns True if recorded (new), False if duplicate fingerprint.
    """
    conn = get_connection()
    try:
        # Skip if we already have this fingerprint (dedup)
        existing = conn.execute(
            "SELECT id FROM splash_incoming_offers WHERE fingerprint = ?",
            (fingerprint,)
        ).fetchone()
        if existing:
            return False

        conn.execute(
            """INSERT INTO splash_incoming_offers
               (offer_bech32, fingerprint, received_at, status, pair_hint, source_ip)
               VALUES (?, ?, ?, 'new', ?, ?)""",
            (offer_bech32, fingerprint, _now(), pair_hint, source_ip)
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("warning", "splash_db_error", f"Failed to record incoming offer: {e}")
        return False


def get_splash_incoming_offers(status: str = None, limit: int = 50) -> List[Dict]:
    """Get incoming offers from the Splash network.

    Args:
        status: Filter by status ('new', 'processed', 'ignored', 'expired')
        limit: Max number of results

    Returns list of offer dicts.
    """
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM splash_incoming_offers WHERE status = ? "
                "ORDER BY received_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM splash_incoming_offers ORDER BY received_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        if _missing_splash_table(e):
            return []
        raise


def update_splash_incoming_status(offer_id: int, status: str,
                                  pair_hint: str = None) -> bool:
    """Update the status of a Splash incoming offer.

    Args:
        offer_id: The database ID of the offer
        status: New status ('processed', 'ignored', 'expired')
    """
    conn = get_connection()
    try:
        if pair_hint is None:
            conn.execute(
                "UPDATE splash_incoming_offers SET status = ? WHERE id = ?",
                (status, offer_id)
            )
        else:
            conn.execute(
                "UPDATE splash_incoming_offers SET status = ?, pair_hint = ? WHERE id = ?",
                (status, pair_hint, offer_id)
            )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log_event("warning", "splash_db_error", f"Failed to update offer {offer_id}: {e}")
        return False


def get_splash_incoming_stats(asset_id: str = None) -> Dict:
    """Summarize inbound Splash offers for the GUI/bot state."""
    conn = get_connection()
    normalized_asset = (asset_id or "").strip().lower()

    try:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS new_count,
                SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) AS processed_count,
                SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END) AS ignored_count,
                SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END) AS expired_count,
                MAX(received_at) AS last_received_at
            FROM splash_incoming_offers
            """,
        ).fetchone()

        relevant = None
        if normalized_asset:
            relevant = conn.execute(
                """
                SELECT
                    COUNT(*) AS relevant_count,
                    MAX(received_at) AS last_relevant_at
                FROM splash_incoming_offers
                WHERE status = 'processed' AND lower(coalesce(pair_hint, '')) = ?
                """,
                (normalized_asset,)
            ).fetchone()
    except Exception as e:
        if not _missing_splash_table(e):
            raise
        totals = None
        relevant = None

    return {
        "total": int((totals["total"] or 0) if totals else 0),
        "new": int((totals["new_count"] or 0) if totals else 0),
        "processed": int((totals["processed_count"] or 0) if totals else 0),
        "ignored": int((totals["ignored_count"] or 0) if totals else 0),
        "expired": int((totals["expired_count"] or 0) if totals else 0),
        "relevant": int((relevant["relevant_count"] or 0) if relevant else 0),
        "last_received_at": (totals["last_received_at"] if totals else None),
        "last_relevant_at": (relevant["last_relevant_at"] if relevant else None),
    }


def clear_splash_incoming() -> int:
    """Delete all stored Splash incoming offers.

    The bot only needs inbound Splash counts for the current run. On a fresh
    start or bot restart, stale inbound offers should not bleed into the new
    session's Market Intel / Splash widgets.
    """
    conn = get_connection()
    try:
        cursor = conn.execute("DELETE FROM splash_incoming_offers")
        conn.commit()
        deleted = int(cursor.rowcount or 0)
        if deleted > 0:
            log_event("info", "splash_reset",
                      f"Cleared {deleted} stored Splash incoming offers for a new run")
        return deleted
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if _missing_splash_table(e):
            return 0
        log_event("warning", "splash_db_error", f"Failed to clear incoming offers: {e}")
        return 0


def prune_splash_incoming(max_age_hours: int = 24) -> int:
    """Delete old Splash incoming offers to prevent unbounded growth.

    Args:
        max_age_hours: Delete offers older than this many hours

    Returns number of rows deleted.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM splash_incoming_offers WHERE received_at < datetime('now', ?)",
            (f"-{max_age_hours} hours",)
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            log_event("debug", "splash_pruned",
                      f"Pruned {deleted} old Splash incoming offers (>{max_age_hours}h)")
        return deleted
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if _missing_splash_table(e):
            return 0
        log_event("warning", "splash_db_error", f"Failed to prune incoming offers: {e}")
        return 0


# ---------------------------------------------------------------------------
# Smart Defaults v2: Pool snapshots + market analysis cache
# ---------------------------------------------------------------------------

def record_pool_snapshot(asset_id: str, xch_reserve: float,
                         cat_reserve: float, price: float) -> bool:
    """Store a TibetSwap pool snapshot for historical tracking.

    Called every bot loop cycle to build up pool depth history over time.
    Smart Defaults v2 uses this to detect pool growth/shrinkage trends.
    """
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pool_snapshots (asset_id, xch_reserve, cat_reserve, price) "
            "VALUES (?, ?, ?, ?)",
            (asset_id, xch_reserve, cat_reserve, price)
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def get_pool_snapshots(asset_id: str, hours: float = 720) -> List[Dict]:
    """Get pool snapshots for an asset within the given time window.

    Default: 30 days (720 hours). Returns newest-first.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM pool_snapshots "
            "WHERE asset_id = ? AND timestamp >= datetime('now', ?) "
            "ORDER BY timestamp DESC",
            (asset_id, f"-{int(hours)} hours")
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_market_analysis_cache(asset_id: str, analysis_type: str) -> Optional[Dict]:
    """Retrieve a cached market analysis result if it hasn't expired.

    Returns the parsed JSON data or None if cache miss / expired.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT data_json, expires_at FROM market_analysis_cache "
            "WHERE asset_id = ? AND analysis_type = ? "
            "AND expires_at > datetime('now') "
            "ORDER BY created_at DESC LIMIT 1",
            (asset_id, analysis_type)
        ).fetchone()
        if row:
            return json.loads(row["data_json"])
        return None
    except Exception:
        return None


def get_market_analysis_cache_age_secs(asset_id: str, analysis_type: str) -> Optional[int]:
    """Age in seconds of the latest non-expired cache entry for this pair.

    Returns None on cache miss or expiry. Used by the advisor layer so tips
    that depend on Spacescan / full_analysis data can flag themselves when
    the underlying cache is old (e.g. Spacescan returned 429s for hours and
    the app is still serving from the 24h cache).
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_secs "
            "FROM market_analysis_cache "
            "WHERE asset_id = ? AND analysis_type = ? "
            "  AND expires_at > datetime('now') "
            "ORDER BY created_at DESC LIMIT 1",
            (asset_id, analysis_type)
        ).fetchone()
        if row and row["age_secs"] is not None:
            return max(0, int(row["age_secs"]))
        return None
    except Exception:
        return None


def set_market_analysis_cache(asset_id: str, analysis_type: str,
                               data: dict, ttl_minutes: int = 60) -> bool:
    """Store a market analysis result with an expiry time.

    Args:
        asset_id: CAT asset ID
        analysis_type: e.g. 'trade_history', 'volatility', 'token_health'
        data: The analysis result dict (stored as JSON)
        ttl_minutes: How long the cache is valid (default 60 min)
    """
    conn = get_connection()
    try:
        # Clear old entries for this asset/type
        conn.execute(
            "DELETE FROM market_analysis_cache WHERE asset_id = ? AND analysis_type = ?",
            (asset_id, analysis_type)
        )
        conn.execute(
            "INSERT INTO market_analysis_cache (asset_id, analysis_type, data_json, expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            (asset_id, analysis_type, json.dumps(data), f"+{ttl_minutes} minutes")
        )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def clear_market_analysis_cache(asset_id: str,
                                keep_analysis_types: tuple[str, ...] = ()) -> int:
    """Clear cached market analysis entries for an asset.

    Args:
        asset_id: CAT asset ID whose cache should be cleared.
        keep_analysis_types: Optional analysis types to preserve.
    """
    conn = get_connection()
    try:
        if keep_analysis_types:
            # Safe: f-string only builds the ? placeholder count; values are parameterised.
            placeholders = ",".join("?" for _ in keep_analysis_types)
            cursor = conn.execute(
                f"DELETE FROM market_analysis_cache "
                f"WHERE asset_id = ? AND analysis_type NOT IN ({placeholders})",
                (asset_id, *keep_analysis_types)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM market_analysis_cache WHERE asset_id = ?",
                (asset_id,)
            )
        conn.commit()
        return cursor.rowcount
    except Exception:
        return 0
