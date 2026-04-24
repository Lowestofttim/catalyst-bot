"""Cross-platform per-user data directory layout for all writable files

Resolves the canonical location of every runtime-writable file — database,
`.env`, log files, crash log, window state, backups, coin-prep sidecars —
under the OS user data dir (`%APPDATA%` on Windows, `~/Library/Application
Support` on macOS, `~/.local/share` on Linux). This lets the app be
installed to a read-only location like `C:\\Program Files\\`, with only
bundled assets (binaries, HTML, icons) staying beside the executable.

Key responsibilities:
    - Compute the data dir for the host OS and create it on demand
    - Expose helpers for `env_file()`, `db_path()`, `log_dir()`, etc.
    - Honour the `CMM_DATA_DIR` env var override for CI and portable setups
    - Migrate legacy files from the install dir at import time on first run

The `migrate_legacy_files()` pass runs once and is idempotent — existing
dev installs keep working transparently when upgraded to a packaged build.
"""

import os
import sys
import shutil


# On-disk data directory name on every OS. This is the folder under
# %APPDATA% (Windows), ~/Library/Application Support (macOS), or
# ~/.local/share (Linux) that holds bot.db, .env, logs, and secrets.
APP_NAME = "Catalyst"


def _install_dir() -> str:
    """Absolute path to the directory containing THIS module.

    In a packaged build this is the install directory (read-only).
    In dev mode this is the repo root.
    """
    return os.path.dirname(os.path.abspath(__file__))


def _appdata_base() -> str:
    """Return the platform's user-data base dir (no app folder appended)."""
    if sys.platform == "win32":
        return os.environ.get("APPDATA") or os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support")
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    return base or os.path.expanduser("~/.local/share")


def _default_data_dir() -> str:
    """Platform-appropriate per-user data directory for this app."""
    override = os.environ.get("CMM_DATA_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(_appdata_base(), APP_NAME)


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

