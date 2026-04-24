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
    """Write the secrets file.  Must be called while _LOCK is held."""
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    # Restrict file permissions to owner only (defense-in-depth for secrets)
    try:
        import os
        os.chmod(path, 0o600)
    except (OSError, AttributeError):
        pass  # Windows may not support chmod; ACLs would be needed there


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

