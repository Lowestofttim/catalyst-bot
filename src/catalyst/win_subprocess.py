"""Windows-only subprocess helpers to suppress console-window flashes

When the desktop app runs without an attached terminal, spawning child
processes with default flags briefly flashes a console window. These
helpers return the right `Popen` kwargs (creation flags plus a hidden
STARTUPINFO) so those processes stay invisible. On non-Windows
platforms the helper returns an empty dict, making it a no-op.

Key responsibilities:
    - `hidden_subprocess_kwargs(detached, new_process_group)` builds the
      kwargs dict with CREATE_NO_WINDOW or DETACHED_PROCESS flags
    - Optional CREATE_NEW_PROCESS_GROUP for children that need their own
      signal group
    - Cross-platform safe: returns `{}` when `os.name != "nt"`

Used by anything that launches a child process (Sage daemon, Splash
binary, coin-prep worker, PyInstaller build subprocesses).
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, Any


def hidden_subprocess_kwargs(*, detached: bool = False, new_process_group: bool = False) -> Dict[str, Any]:
    """Return subprocess kwargs that suppress Windows console windows.

    On non-Windows platforms this returns an empty dict.
    """
    if os.name != "nt":
        return {}

    creationflags = 0
    if detached:
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    kwargs: Dict[str, Any] = {}
    if creationflags:
        kwargs["creationflags"] = creationflags

    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs

