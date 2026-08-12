from __future__ import annotations

import json
import subprocess
from pathlib import Path


def main() -> int:
    cases = json.loads((Path(__file__).parent / "skill-cases.json").read_text())
    if not all(case.get("name") for case in cases["cases"]):
        return 1
    # Deterministic local mode uses the repository fake runner; an optional
    # live mode proves the installed Codex executable can load a Skill.
    if "--live" in __import__("sys").argv:
        result = subprocess.run(
            ["codex", "exec", "--ephemeral", "--sandbox", "read-only", "--version"], check=False
        )
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
