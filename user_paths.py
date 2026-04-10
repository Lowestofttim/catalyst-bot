"""
user_paths.py — Canonical location of user-writable files.

All files the app needs to READ AND WRITE at runtime live under a
per-user data directory so the app can be installed to a read-only
location like C:\\Program Files\\.  The install directory is
read-only in a packaged build; only bundled assets (splash.exe,
favicon.ico, bot_gui.html, etc.) live there.

Platform layout:
    Windows: %APPDATA%\\ChiaMarketMaker\\
    macOS:   ~/Library/Application Support/ChiaMarketMaker/
    Linux:   $XDG_DATA_HOME/ChiaMarketMaker/
             (defaults to ~/.local/share/ChiaMarketMaker/)

Files under the data dir:
    .env                       — user config (settings panel writes here)
    bot.db                     — SQLite trading database (WAL mode)
    bot.db-shm, bot.db-wal     — SQLite WAL sidecar files
    .window_state.json         — saved window size/position
    crash.log                  — uncaught exception dump
    bot_superlog_*.log         — structured per-run log files
    user_secrets.json          — Spacescan API key etc.
    worker_cancelled_ids.json  — coin prep worker state
    backups/                   — database backups
        bot_backup_YYYYMMDD_HHMMSS.db

Developer override:
    Set the CMM_DATA_DIR environment variable to override the auto-
    detected path.  Useful for CI, tests, and power users who want
    everything in a portable directory.

First-launch migration:
    On first launch, if an .env / bot.db / etc. exists in the
    install directory (legacy dev layout), they are copied to the
    data dir automatically.  See `migrate_legacy_files()`.
"""

import os
import sys
import shutil


APP_NAME = "ChiaMarketMaker"


def _install_dir() -> str:
    """Absolute path to the directory containing THIS module.

    In a packaged build this is the install directory (read-only).
    In dev mode this is the repo root.
    """
    return os.path.dirname(os.path.abspath(__file__))


def _default_data_dir() -> str:
    """Platform-appropriate per-user data directory for this app."""
    override = os.environ.get("CMM_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))

    if sys.platform == "win32":
        # %APPDATA% is defined on all modern Windows installs
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)

    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"),
            APP_NAME,
        )

    # Linux / *BSD / other
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    if not base:
        base = os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


# Resolve once at import time.
_DATA_DIR = _default_data_dir()
_INSTALL_DIR = _install_dir()


def data_dir() -> str:
    """Return the user-writable data directory (creating it if needed)."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
    except Exception as e:
        # If we can't create the canonical dir, fall back to the install
        # dir.  This preserves legacy behaviour for dev runs and at least
        # fails visibly rather than silently dropping writes.
        print(
            f"[user_paths] WARNING: could not create data dir {_DATA_DIR}: {e}. "
            f"Falling back to install dir {_INSTALL_DIR}",
            flush=True,
        )
        return _INSTALL_DIR
    return _DATA_DIR


def install_dir() -> str:
    """Return the read-only install directory (bundled resources live here)."""
    return _INSTALL_DIR


# ── Canonical file paths ────────────────────────────────────────────

def env_file() -> str:
    """Path to the .env config file."""
    return os.path.join(data_dir(), ".env")


def database_file() -> str:
    """Path to the SQLite bot.db file."""
    return os.path.join(data_dir(), "bot.db")


def window_state_file() -> str:
    """Path to the window geometry JSON."""
    return os.path.join(data_dir(), ".window_state.json")


def crash_log_file() -> str:
    """Path to the crash log (written from the uncaught exception handler)."""
    return os.path.join(data_dir(), "crash.log")


def worker_cancelled_ids_file() -> str:
    """Path to the coin prep worker's cancelled-ids state file."""
    return os.path.join(data_dir(), "worker_cancelled_ids.json")


def protected_offers_file() -> str:
    """Path to the graceful-migration protected offers JSON (read by coin prep)."""
    return os.path.join(data_dir(), "protected_offers.json")


def log_dir() -> str:
    """Directory where bot_superlog_*.log files are written."""
    return data_dir()


def backups_dir() -> str:
    """Directory where database backups are written."""
    path = os.path.join(data_dir(), "backups")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


# ── First-launch migration ──────────────────────────────────────────

_LEGACY_FILES = [
    # (basename, dest_fn)
    (".env", env_file),
    ("bot.db", database_file),
    ("bot.db-shm", lambda: os.path.join(data_dir(), "bot.db-shm")),
    ("bot.db-wal", lambda: os.path.join(data_dir(), "bot.db-wal")),
    (".window_state.json", window_state_file),
    ("crash.log", crash_log_file),
    ("worker_cancelled_ids.json", worker_cancelled_ids_file),
]

_MIGRATION_MARKER_NAME = ".migration_complete"


def migrate_legacy_files() -> None:
    """Migrate files from the install dir into the data dir on first launch.

    Historically these files lived alongside bot.py / api_server.py in
    the install dir.  When we move to a per-user data dir we want
    existing dev installs (and any earlier beta builds) to keep
    working — so on first launch we copy any that exist.

    The migration only runs ONCE per data dir (tracked via a marker
    file).  After that the data dir is authoritative and the install
    dir copies are ignored (but left in place so a rollback is possible).
    """
    dd = data_dir()
    marker = os.path.join(dd, _MIGRATION_MARKER_NAME)
    if os.path.exists(marker):
        return

    # Also migrate any existing bot_superlog_*.log from the install dir.
    # And any existing bot_backup_*.db from the install dir (move to
    # backups/ subdir).
    import glob as _glob
    moved = []
    try:
        for basename, dest_fn in _LEGACY_FILES:
            src = os.path.join(_INSTALL_DIR, basename)
            if not os.path.isfile(src):
                continue
            dst = dest_fn()
            if os.path.exists(dst):
                # Don't overwrite — data dir wins if it already has it
                continue
            try:
                shutil.copy2(src, dst)
                moved.append(basename)
            except Exception as e:
                print(f"[user_paths] Could not migrate {basename}: {e}", flush=True)

        # bot_superlog_*.log (multiple files)
        for src in _glob.glob(os.path.join(_INSTALL_DIR, "bot_superlog_*.log")):
            basename = os.path.basename(src)
            dst = os.path.join(dd, basename)
            if os.path.exists(dst):
                continue
            try:
                shutil.copy2(src, dst)
                moved.append(basename)
            except Exception as e:
                print(f"[user_paths] Could not migrate {basename}: {e}", flush=True)

        # bot_backup_*.db → backups/
        bd = backups_dir()
        for src in _glob.glob(os.path.join(_INSTALL_DIR, "bot_backup_*.db")):
            basename = os.path.basename(src)
            dst = os.path.join(bd, basename)
            if os.path.exists(dst):
                continue
            try:
                shutil.copy2(src, dst)
                moved.append(f"backups/{basename}")
            except Exception as e:
                print(f"[user_paths] Could not migrate {basename}: {e}", flush=True)

        # Mark the migration complete so we don't repeat it every launch.
        try:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(
                    "Migration from install dir to data dir completed.\n"
                    "Delete this file to re-run the migration.\n"
                )
        except Exception:
            pass

        if moved:
            print(
                f"[user_paths] Migrated {len(moved)} legacy file(s) from install "
                f"dir to data dir: {', '.join(moved[:5])}"
                + (f" (+{len(moved)-5} more)" if len(moved) > 5 else ""),
                flush=True,
            )
    except Exception as e:
        print(f"[user_paths] Legacy migration failed: {e}", flush=True)


# Best-effort: run migration at import time so it happens before any
# module reads these files.  If migration fails we still return usable
# paths, we just might have legacy copies sitting in the install dir.
try:
    migrate_legacy_files()
except Exception as _e:
    print(f"[user_paths] Import-time migration skipped: {_e}", flush=True)

