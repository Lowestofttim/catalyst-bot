"""Fail if obviously sensitive files or hardcoded secrets are tracked.

This is intentionally lightweight and dependency-free. It is not a replacement
for GitHub secret scanning or a dedicated scanner, but it catches the mistakes
that would be most damaging in this repo: wallet env files, cert/key material,
live databases, logs, and simple hardcoded token assignments.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


SENSITIVE_EXACT_NAMES = {
    ".env",
    "user_secrets.json",
    "secrets.json",
}

SENSITIVE_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".log",
    ".key",
    ".pem",
    ".crt",
    ".cert",
    ".p12",
    ".pfx",
)

SENSITIVE_DIR_PARTS = {
    "sage_client_ssl",
}

SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    \b(api[_-]?key|private[_-]?key|secret|token|password)\b
    \s*[:=]\s*
    ["']([^"']{12,})["']
    """
)

PEM_PRIVATE_KEY_RE = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)

PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "change_me",
    "changeme",
    "your_",
    "dummy",
    "fake",
    "test",
    "redacted",
    "<",
    "${",
)


def _git_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(p.decode("utf-8")) for p in result.stdout.split(b"\0") if p]


def _is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if parts & SENSITIVE_DIR_PARTS:
        return True
    if name in SENSITIVE_EXACT_NAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    return name.endswith(SENSITIVE_SUFFIXES)


def _read_text_if_safe(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def main() -> int:
    failures: list[str] = []

    for path in _git_files():
        if _is_sensitive_path(path):
            failures.append(f"sensitive tracked path: {path.as_posix()}")
            continue

        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if PEM_PRIVATE_KEY_RE.search(raw):
            failures.append(f"private key material in: {path.as_posix()}")
            continue

        text = _read_text_if_safe(path)
        if text is None:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            match = SECRET_ASSIGNMENT_RE.search(line)
            if not match:
                continue
            value = match.group(2).strip()
            if _looks_like_placeholder(value):
                continue
            failures.append(
                f"possible hardcoded secret: {path.as_posix()}:{line_no}"
            )

    if failures:
        print("Tracked secret scan failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("OK: no obvious tracked secrets or sensitive files found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
