"""Prepare public release notes for the signed updater manifest."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def sanitize_release_notes(text: str, private_repo: str) -> str:
    """Remove private source-repo links while keeping readable change bullets."""
    notes = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    repo = str(private_repo or "").strip().strip("/")
    if repo:
        escaped_repo = re.escape(repo)
        notes = re.sub(
            rf"\s+in\s+https://github\.com/{escaped_repo}/pull/\d+",
            "",
            notes,
            flags=re.IGNORECASE,
        )
        notes = re.sub(
            rf"\[([^\]]+)\]\(https://github\.com/{escaped_repo}/(?:pull|issues)/\d+\)",
            r"\1",
            notes,
            flags=re.IGNORECASE,
        )
        notes = re.sub(
            rf"(?im)^\s*\*\*Full Changelog\*\*:\s+https://github\.com/{escaped_repo}/compare/[^\n]+\n?",
            "",
            notes,
        )
        notes = re.sub(
            rf"https://github\.com/{escaped_repo}/\S+",
            "",
            notes,
            flags=re.IGNORECASE,
        )
    notes = re.sub(r"\n{3,}", "\n\n", notes).strip()
    return notes or "Maintenance update."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip private repository links from public update release notes."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--private-repo", required=True)
    args = parser.parse_args()

    cleaned = sanitize_release_notes(
        args.input.read_text(encoding="utf-8"), args.private_repo
    )
    args.output.write_text(cleaned + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
