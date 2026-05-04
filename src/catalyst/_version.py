"""Runtime version helpers.

Packaged releases are stamped by scripts/sync_release_metadata.py before
PyInstaller runs. Source launches keep this fallback, then prefer the local
git describe output so a desktop shortcut pointed at the checkout does not
look like an old release forever.
"""

from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


__version__ = "1.2.5"


def _describe_to_version(describe: str) -> str:
    raw = str(describe or "").strip()
    if raw.lower().startswith("v"):
        raw = raw[1:]

    dirty = raw.endswith("-dirty")
    if dirty:
        raw = raw[:-6]

    match = re.fullmatch(r"(\d+\.\d+\.\d+)-(\d+)-g([0-9a-f]+)", raw)
    if match:
        base, commits, sha = match.groups()
        version = f"{base}+{commits}.g{sha}"
    else:
        version = raw

    if dirty:
        version = f"{version}.dirty" if "+" in version else f"{version}+dirty"
    return version or __version__


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_describe(repo_root: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "describe",
            "--tags",
            "--match",
            "v[0-9]*",
            "--dirty",
            "--always",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=1.5,
    )
    return result.stdout.strip()


@lru_cache(maxsize=1)
def get_version() -> str:
    if getattr(sys, "frozen", False):
        return __version__
    try:
        return _describe_to_version(_git_describe(_repo_root()))
    except Exception:
        return __version__
