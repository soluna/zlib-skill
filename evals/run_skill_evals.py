from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    cases = json.loads((Path(__file__).parent / "skill-cases.json").read_text())
    return 0 if all(case.get("name") for case in cases["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
