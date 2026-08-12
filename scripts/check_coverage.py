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
MINIMUM_PERCENT = 85.0
CANONICAL_PREFIX = "plugins/zlib-skill/scripts/"


def main(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if float(data.get("totals", {}).get("percent_covered", 0)) < MINIMUM_PERCENT:
        return 1
    files = data.get("files", {})
    if not any(CANONICAL_PREFIX in key for key in files):
        return 1
    for name in REQUIRED:
        matches = [
            value for key, value in files.items() if key.endswith(name) and CANONICAL_PREFIX in key
        ]
        if not matches:
            return 1
        highest = max(
            float(value.get("summary", {}).get("percent_covered", 0)) for value in matches
        )
        if highest < MINIMUM_PERCENT:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
