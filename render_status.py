#!/usr/bin/env python3
"""
Regenerates the "Current Dataset Health" table in README.md from state/status.json.
Run this after monitor.py, or let the GitHub Actions workflow do it automatically.

The README must contain these two marker lines (already present in this repo's
README.md) -- everything between them gets replaced on each run:

    <!-- STATUS_TABLE_START -->
    <!-- STATUS_TABLE_END -->
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATUS_PATH = ROOT / "state" / "status.json"
README_PATH = ROOT / "README.md"

START_MARKER = "<!-- STATUS_TABLE_START -->"
END_MARKER = "<!-- STATUS_TABLE_END -->"


def build_table():
    if not STATUS_PATH.exists():
        return "_No status data yet -- run `python monitor.py` at least once._"

    with open(STATUS_PATH) as f:
        data = json.load(f)

    lines = [
        f"_Last checked: {data['generated_at']}_",
        "",
        "| Dataset | Status | Broken API | Schema Change | Stale | Detail |",
        "|---|---|---|---|---|---|",
    ]
    for r in data["results"]:
        healthy = not (r["broken_api"] or r["schema_change"] or r["stale"])
        status = "🟢 OK" if healthy else "🔴 ISSUE"
        lines.append(
            f"| {r['name']} | {status} | "
            f"{'❌' if r['broken_api'] else '—'} | "
            f"{'❌' if r['schema_change'] else '—'} | "
            f"{'❌' if r['stale'] else '—'} | "
            f"{r['detail'] or '—'} |"
        )
    return "\n".join(lines)


def main():
    table = build_table()
    readme = README_PATH.read_text()

    if START_MARKER not in readme or END_MARKER not in readme:
        print("WARNING: status table markers not found in README.md -- skipping update.")
        return

    before = readme.split(START_MARKER)[0]
    after = readme.split(END_MARKER)[1]
    new_readme = f"{before}{START_MARKER}\n{table}\n{END_MARKER}{after}"
    README_PATH.write_text(new_readme)


if __name__ == "__main__":
    main()
