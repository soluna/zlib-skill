"""Repo-owned coverage threshold checker."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED = {
    "schema.py",
    "config_store.py",
    "credential_store.py",
    "operation.py",
    "zlib_source.py",
    "search_workflow.py",
    "download_transaction.py",
}
# The complete source tree includes optional native/network adapters that are
# intentionally exercised through injected seams.  Keep a realistic repository
# floor while holding the critical storage/operation modules to the same gate.
MINIMUM_PERCENT = 70.0
CANONICAL_PREFIX = "plugins/zlib-skill/scripts/zlib_anna/"


def canonical_coverage(files: dict) -> float:
    """Calculate coverage for the single implementation tree, excluding packaging shims."""
    summaries = [
        value.get("summary", {}) for key, value in files.items() if CANONICAL_PREFIX in key
    ]
    if not summaries:
        return 0.0
    statements = sum(int(summary.get("num_statements", 0)) for summary in summaries)
    if statements:
        covered = sum(int(summary.get("covered_lines", 0)) for summary in summaries)
        return covered / statements * 100
    return sum(float(summary.get("percent_covered", 0)) for summary in summaries) / len(summaries)


def main(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    files = data.get("files", {})
    if not any(CANONICAL_PREFIX in key for key in files):
        print("coverage report has no canonical plugin files", file=sys.stderr)
        return 1
    total = canonical_coverage(files)
    if total < MINIMUM_PERCENT:
        print(
            f"canonical package coverage {total:.2f}% is below {MINIMUM_PERCENT:.2f}%",
            file=sys.stderr,
        )
        for key, value in sorted(files.items()):
            if CANONICAL_PREFIX in key:
                percent = value.get("summary", {}).get("percent_covered", 0)
                print(f"  {key}: {percent:.2f}%", file=sys.stderr)
        return 1
    for name in REQUIRED:
        matches = [
            value for key, value in files.items() if key.endswith(name) and CANONICAL_PREFIX in key
        ]
        if not matches:
            print(f"coverage report is missing canonical {name}", file=sys.stderr)
            return 1
        highest = max(
            float(value.get("summary", {}).get("percent_covered", 0)) for value in matches
        )
        if highest < MINIMUM_PERCENT:
            print(
                f"canonical {name} coverage {highest:.2f}% is below {MINIMUM_PERCENT:.2f}%",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
