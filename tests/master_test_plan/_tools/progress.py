#!/usr/bin/env python3
"""Print a progress snapshot of the master test plan.

Reads tests/master_test_plan/MASTER_INDEX.md, parses the slice-status
columns, and prints:

  * Per-layer pending / in-progress / done / blocked counts
  * Overall percent complete
  * List of any in-progress or blocked slices (with notes)
  * Path to any open handoff files

Usage (from repo root):
    python tests/master_test_plan/_tools/progress.py
    python tests/master_test_plan/_tools/progress.py --json

Zero dependencies. Safe to run anytime — pure read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INDEX_PATH = REPO_ROOT / "tests" / "master_test_plan" / "MASTER_INDEX.md"
HANDOFFS_DIR = REPO_ROOT / "tests" / "master_test_plan" / "handoffs"

# Matches e.g.:
#   | 01-04 | TODO/FIXME sweep | `[x]` | done in 9ca64d2 |
# With some tolerance for whitespace.
_ROW = re.compile(
    r"^\|\s*(?P<id>\d{2}-\d{2})\s*\|\s*(?P<title>[^|]+?)\s*\|\s*`\[(?P<status>[ ~x!])\]`\s*\|\s*(?P<note>[^|]*?)\s*\|\s*$"
)

# Layer headings look like:  ## Layer 1 — Static analysis (8 slices)
_LAYER = re.compile(r"^##\s*Layer\s+(?P<n>\d+)\s*—\s*(?P<name>.+?)\s*\(.*\)\s*$")

_STATUS_KEY = {" ": "pending", "~": "in_progress", "x": "done", "!": "blocked"}


def parse_index(text: str) -> list[dict]:
    """Return a list of slice dicts with keys: id, layer, layer_name, title, status, note."""
    rows: list[dict] = []
    current_layer: int | None = None
    current_layer_name: str = ""
    for line in text.splitlines():
        m_layer = _LAYER.match(line)
        if m_layer:
            current_layer = int(m_layer.group("n"))
            current_layer_name = m_layer.group("name").strip()
            continue
        m_row = _ROW.match(line)
        if m_row and current_layer is not None:
            status_char = m_row.group("status")
            rows.append({
                "id": m_row.group("id"),
                "layer": current_layer,
                "layer_name": current_layer_name,
                "title": m_row.group("title").strip(),
                "status": _STATUS_KEY.get(status_char, "unknown"),
                "note": m_row.group("note").strip(),
            })
    return rows


def summarise(slices: list[dict]) -> dict:
    """Per-layer counts + overall totals."""
    by_layer: dict[int, dict] = {}
    totals = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0}
    for s in slices:
        layer = s["layer"]
        if layer not in by_layer:
            by_layer[layer] = {
                "name": s["layer_name"],
                "pending": 0,
                "in_progress": 0,
                "done": 0,
                "blocked": 0,
                "total": 0,
            }
        by_layer[layer][s["status"]] += 1
        by_layer[layer]["total"] += 1
        totals[s["status"]] += 1
    totals["total"] = sum(totals.get(k, 0) for k in ("pending", "in_progress", "done", "blocked"))
    return {"by_layer": by_layer, "totals": totals}


def open_handoffs() -> list[str]:
    if not HANDOFFS_DIR.exists():
        return []
    return sorted(str(p.relative_to(REPO_ROOT)) for p in HANDOFFS_DIR.glob("*.md"))


def render_text(slices: list[dict], summary: dict, handoffs: list[str]) -> str:
    by_layer = summary["by_layer"]
    totals = summary["totals"]

    out = ["Master Test Plan — progress"]
    out.append("=" * 40)
    out.append("")
    out.append(f"{'Layer':<8} {'Name':<42} {'Done':>5} {'WIP':>4} {'Pend':>5} {'Blkd':>4}  {'%':>5}")
    out.append("-" * 78)
    for n in sorted(by_layer.keys()):
        row = by_layer[n]
        pct = (row["done"] / row["total"] * 100) if row["total"] else 0
        name = row["name"][:42]
        out.append(
            f"{n:<8} {name:<42} {row['done']:>5} {row['in_progress']:>4} "
            f"{row['pending']:>5} {row['blocked']:>4}  {pct:>4.0f}%"
        )
    out.append("-" * 78)
    overall_pct = (totals["done"] / totals["total"] * 100) if totals["total"] else 0
    out.append(
        f"{'TOTAL':<8} {'':<42} {totals['done']:>5} {totals['in_progress']:>4} "
        f"{totals['pending']:>5} {totals['blocked']:>4}  {overall_pct:>4.0f}%"
    )
    out.append("")

    in_progress = [s for s in slices if s["status"] == "in_progress"]
    blocked = [s for s in slices if s["status"] == "blocked"]

    if in_progress:
        out.append("In progress:")
        for s in in_progress:
            note = s["note"] or "—"
            out.append(f"  • {s['id']}  {s['title']}  [{note}]")
        out.append("")

    if blocked:
        out.append("Blocked:")
        for s in blocked:
            note = s["note"] or "—"
            out.append(f"  • {s['id']}  {s['title']}  [{note}]")
        out.append("")

    if handoffs:
        out.append("Open handoffs:")
        for h in handoffs:
            out.append(f"  • {h}")
        out.append("")

    next_pending = next((s for s in slices if s["status"] == "pending"), None)
    if next_pending:
        out.append(f"Next pending slice: {next_pending['id']} — {next_pending['title']}")
    else:
        out.append("No pending slices remain.")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Master test plan progress report.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    if not INDEX_PATH.exists():
        print(f"ERROR: MASTER_INDEX.md not found at {INDEX_PATH}", file=sys.stderr)
        return 2

    text = INDEX_PATH.read_text(encoding="utf-8")
    slices = parse_index(text)
    summary = summarise(slices)
    handoffs = open_handoffs()

    if args.json:
        payload = {
            "slices": slices,
            "by_layer": summary["by_layer"],
            "totals": summary["totals"],
            "open_handoffs": handoffs,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_text(slices, summary, handoffs))

    return 0


if __name__ == "__main__":
    sys.exit(main())
