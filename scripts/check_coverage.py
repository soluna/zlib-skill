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


def main(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if float(data.get("totals", {}).get("percent_covered", 0)) <= 0:
        return 1
    files = data.get("files", {})
    for name in REQUIRED:
        matches = [value for key, value in files.items() if key.endswith(name)]
        if not matches:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
