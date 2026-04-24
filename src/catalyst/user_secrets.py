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
import platform
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


def _load_locked() -> dict:
    """Read the secrets file.  Must be called while _LOCK is held."""
    path = _secrets_path()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_locked(data: dict) -> None:
    """Write the secrets file.  Must be called while _LOCK is held.

    A wipe of this file cost a user an hour of re-setup once, so any
    write that drops from non-empty to empty (or empty-to-empty) is
    logged with a stack trace. That makes it possible to catch the
    caller next time there's a mystery wipe.
    """
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Forensic logging for suspicious writes: empty dict, or losing a
    # previously-stored SPACESCAN_API_KEY. Uses a bare stderr print +
    # traceback so it works even before super_log is initialised.
    try:
        if not data or (path.exists() and _looks_like_key_loss(path, data)):
            import sys as _sys
            import traceback as _tb
            _sys.stderr.write(
                f"[user_secrets] WARNING: writing sparse/empty secrets to {path} "
                f"(new keys: {sorted(data.keys())}). Stack:\n"
            )
            _tb.print_stack(file=_sys.stderr)
    except Exception:
        pass  # Never let diagnostics break the actual write

    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    # Restrict file permissions to owner only (defense-in-depth for secrets)
    try:
        import os
        os.chmod(path, 0o600)
    except (OSError, AttributeError):
        pass  # Windows may not support chmod; ACLs would be needed there


def _looks_like_key_loss(path: "Path", new_data: dict) -> bool:
    """Return True if the disk has a populated SPACESCAN_API_KEY and the
    new payload has dropped it. Used only to decide whether to log a
    forensic stack trace on write."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            old = json.load(fh)
        if not isinstance(old, dict):
            return False
        had_key = bool(old.get("SPACESCAN_API_KEY"))
        still_has_key = bool(new_data.get("SPACESCAN_API_KEY"))
        return had_key and not still_has_key
    except Exception:
        return False


def get_secret(key: str) -> str:
    """Return the stored value for *key*, or an empty string if not set."""
    with _LOCK:
        return str(_load_locked().get(key) or "")


def set_secret(key: str, value: str) -> None:
    """Persist *value* for *key*.  Passing an empty string removes the entry."""
    with _LOCK:
        data = _load_locked()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
        _save_locked(data)


def apply_to_config(cfg) -> None:
    """Load persisted secrets into *cfg* in-memory (does NOT write to .env).

    Call once at app startup so the rest of the codebase can read secrets
    via the normal cfg attributes without needing to import this module.
    """
    key = get_secret("SPACESCAN_API_KEY")
    if key:
        cfg.SPACESCAN_API_KEY = key

