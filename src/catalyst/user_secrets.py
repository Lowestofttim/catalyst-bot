"""Per-user secrets store kept out of the repo and out of the install dir

Persists sensitive per-user values such as third-party API keys in a JSON
file inside the OS user data directory, never inside the bot directory, so
secrets cannot be accidentally committed, shared via a network drive, or
reused on a different machine or OS account. `apply_to_config(cfg)` copies
known secrets (e.g. `SPACESCAN_API_KEY`) onto the running `Config` object
whenever the config is reloaded.

Key responsibilities:
    - Read / write a JSON file at the platform-appropriate user path
    - Serialise access with a module-level lock
    - Project known secret keys onto the `Config` singleton on reload
    - Set `0o600` permissions on Unix on every write

Windows provides no equivalent per-file protection, so on Windows the
secrets file is protected only by the user's profile ACLs.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _secrets_path() -> Path:
    """Return the full path to the secrets JSON file (does not create it).

    Delegates folder resolution to user_paths.data_dir(), which also
    handles the one-time rename from the legacy folder name so existing
    users don't lose their saved secrets.
    """
    from user_paths import data_dir
    return Path(data_dir()) / "user_secrets.json"


def _backup_path() -> Path:
    """Return the companion `.bak` path for the secrets file."""
    return _secrets_path().with_suffix(".json.bak")


def _read_json_dict(path: Path) -> dict:
    """Read *path* as a JSON object, or {} on any error / non-object."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _load_locked() -> dict:
    """Read the secrets file, auto-restoring from the backup if the
    live file looks wiped and the backup has content.  Must be called
    while _LOCK is held.

    Recovery is silent on the happy path (live file is normal) and loud
    when a restore happens (prints to stderr so the operator sees it
    even before super_log is initialised).
    """
    path = _secrets_path()
    live = _read_json_dict(path)
    if live:
        return live

    # Live is empty / missing — check the backup.
    bak = _backup_path()
    if not bak.exists():
        return live  # genuinely empty, nothing to restore

    backup = _read_json_dict(bak)
    if not backup:
        return live  # backup also empty — nothing to recover

    # Restore.  We write back to the live file so subsequent reads are
    # fast and so the restore is durable across processes.
    try:
        import sys as _sys
        _sys.stderr.write(
            f"[user_secrets] WARNING: live secrets file at {path} was empty; "
            f"restoring from backup {bak} (keys: {sorted(backup.keys())}).\n"
        )
    except Exception:
        pass
    try:
        _write_atomic(path, backup)
    except Exception:
        # Could not restore to disk; still return the in-memory value so
        # the caller sees the key this session.
        pass
    return backup


def _write_atomic(path: Path, data: dict) -> None:
    """Write *data* to *path* as JSON. Caller holds _LOCK."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp_path, 0o600)
        except (OSError, AttributeError):
            pass  # Windows relies on user-profile ACLs
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except (OSError, AttributeError):
            pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _save_locked(data: dict) -> None:
    """Write the secrets file and snapshot the previous content to
    .bak.  Must be called while _LOCK is held.

    Any write with content first copies the current live file to .bak
    so a subsequent accidental wipe is recoverable on next startup.
    """
    path = _secrets_path()
    bak = _backup_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Snapshot existing non-empty content to .bak BEFORE overwriting.
    # Only snapshot when there's something worth keeping — this keeps
    # .bak equal to the last known-good state, not junk.
    try:
        existing = _read_json_dict(path)
        if existing and existing != data:
            _write_atomic(bak, existing)
    except Exception:
        pass  # Never let backup failures block the actual write

    _write_atomic(path, data)


def get_secret(key: str) -> str:
    """Return the stored value for *key*, or an empty string if not set.

    Triggers the .bak auto-restore if the live file is empty but the
    backup has content.
    """
    with _LOCK:
        return str(_load_locked().get(key) or "")


def set_secret(key: str, value: str) -> None:
    """Persist a non-empty *value* for *key*.

    Empty values are rejected to prevent accidental wipes — callers that
    genuinely want to remove a key must use `clear_secret()` and accept
    that it also drops the .bak backup.  This split means a future bug
    that passes user input through unchecked can't silently erase stored
    secrets.
    """
    if not value:
        raise ValueError(
            "set_secret() refuses empty values; use clear_secret() to remove a key"
        )
    with _LOCK:
        data = _load_locked()
        data[key] = value
        _save_locked(data)


def clear_secret(key: str) -> None:
    """Remove *key* from stored secrets and invalidate the backup.

    Only to be called when the operator has explicitly asked for the
    secret to be forgotten.  Dropping the backup is part of the
    contract: otherwise the next startup would auto-restore the key and
    surprise the user.
    """
    with _LOCK:
        data = _load_locked()
        had_keys = bool(data)
        data.pop(key, None)
        if had_keys and not data:
            # User just cleared the last remaining secret — remove .bak
            # too so we don't ressurect it on next launch.
            try:
                _backup_path().unlink(missing_ok=True)
            except Exception:
                pass
        # Snapshot-before-write is skipped here because .bak is the
        # value we're trying to invalidate.  Write the new state directly.
        _write_atomic(_secrets_path(), data)


def apply_to_config(cfg) -> None:
    """Load persisted secrets into *cfg* in-memory (does NOT write to .env).

    Call once at app startup so the rest of the codebase can read secrets
    via the normal cfg attributes without needing to import this module.
    """
    key = get_secret("SPACESCAN_API_KEY")
    if key:
        cfg.SPACESCAN_API_KEY = key

