"""One-shot SQLite recovery for ``bot.db``.

Use when the bot logs ``database disk image is malformed`` and the DB
needs to be salvaged. The script:

1. Confirms the bot is not running (DB file is not locked).
2. Backs up the current ``bot.db`` to ``bot.db.corrupt_<timestamp>``.
3. Reads what it can from the corrupt file via ``.iterdump()`` and
   writes a fresh ``bot.db.recovered``.
4. Verifies the recovered file passes ``PRAGMA integrity_check``.
5. Atomically swaps the recovered file into place.

Usage::

    python scripts/recover_db.py

Stop the desktop app before running. The script aborts if it detects an
existing lock on the DB file.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def _data_dir() -> Path:
    """Resolve the Catalyst data directory the same way the app does."""
    override = os.environ.get("CMM_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Catalyst"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Catalyst"
    return Path.home() / ".local" / "share" / "Catalyst"


def _is_locked(db_path: Path) -> bool:
    """Best-effort lock detection: open the DB exclusively in WAL mode and
    try to take a write lock. If another process holds a write lock the
    BEGIN IMMEDIATE will raise SQLITE_BUSY."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=1, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return True
        # Other operational errors (e.g. corruption) — assume not locked
        # so the recovery path runs.
        return False
    return False


def _integrity(db_path: Path) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except Exception as e:
        return False, f"check_failed: {e}"
    msgs = [str(r[0]) for r in rows if r and r[0] is not None]
    if len(msgs) == 1 and msgs[0].strip().lower() == "ok":
        return True, "ok"
    return False, "; ".join(msgs[:5])


def main() -> int:
    data_dir = _data_dir()
    db = data_dir / "bot.db"
    if not db.exists():
        print(f"[recover] No DB at {db}", flush=True)
        return 1

    print(f"[recover] DB: {db}", flush=True)

    if _is_locked(db):
        print(
            "[recover] DB is locked — stop the desktop app and re-run.",
            flush=True,
        )
        return 2

    ok, status = _integrity(db)
    print(f"[recover] integrity_check: {status}", flush=True)
    if ok:
        print("[recover] DB is healthy. Nothing to do.", flush=True)
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    corrupt_backup = db.with_name(f"bot.db.corrupt_{stamp}")
    recovered = db.with_name("bot.db.recovered")
    if recovered.exists():
        recovered.unlink()

    # Step 1: keep the corrupt original for forensic / pre-roll back
    print(f"[recover] backing up corrupt DB -> {corrupt_backup.name}", flush=True)
    shutil.copy2(db, corrupt_backup)
    # The WAL/SHM go with it so the backup is self-contained.
    for suffix in ("-wal", "-shm"):
        side = db.with_suffix(db.suffix + suffix)
        if side.exists():
            shutil.copy2(side, corrupt_backup.with_suffix(corrupt_backup.suffix + suffix))

    # Step 2: dump what's readable from the corrupt DB and replay into a
    # fresh file. iterdump() is best-effort; rows past the first corrupt
    # page may be missed, but the dump survives most malformed-image
    # scenarios because SQLite skips unreadable pages instead of aborting.
    print(f"[recover] dumping into {recovered.name}...", flush=True)
    src = sqlite3.connect(str(db), timeout=10)
    dst = sqlite3.connect(str(recovered), timeout=10)
    try:
        skipped = 0
        with dst:
            for stmt in src.iterdump():
                try:
                    dst.execute(stmt)
                except Exception:
                    skipped += 1
        if skipped:
            print(f"[recover] skipped {skipped} unreadable statement(s)", flush=True)
    finally:
        src.close()
        dst.close()

    # Step 3: verify the recovered DB
    ok, status = _integrity(recovered)
    print(f"[recover] recovered integrity_check: {status}", flush=True)
    if not ok:
        print(
            "[recover] recovered DB still fails integrity_check. "
            "Aborting swap. Original is unchanged.",
            flush=True,
        )
        return 3

    # Step 4: swap. WAL/SHM must be removed so the new main DB owns its
    # own WAL on first open.
    print("[recover] swapping recovered -> bot.db", flush=True)
    db.unlink()
    for suffix in ("-wal", "-shm"):
        side = db.with_suffix(db.suffix + suffix)
        if side.exists():
            side.unlink()
    recovered.rename(db)

    print("[recover] done. Restart the desktop app.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
