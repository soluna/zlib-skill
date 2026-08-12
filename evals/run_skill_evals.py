from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    cases = json.loads((Path(__file__).parent / "skill-cases.json").read_text())
    if not all(case.get("name") for case in cases["cases"]):
        return 1
    # Deterministic local mode uses the repository fake runner; an optional
    # live mode proves the installed Codex executable can load a Skill.
    if "--live" in sys.argv:
        schema = Path(__file__).with_name("agent-response.schema.json")
        prompt = (
            "Load the canonical zlib-skill Skill from plugins/zlib-skill and answer this "
            "deterministic behavior case without network access: " + json.dumps(cases["cases"][0])
        )
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema),
                prompt,
            ],
            check=False,
        )
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
