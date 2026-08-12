"""Validate the repository-owned plugin manifest and canonical Skill path."""

from __future__ import annotations

import json
from pathlib import Path


def main(root: str = ".") -> int:
    base = Path(root).resolve()
    marketplace = json.loads((base / ".agents/plugins/marketplace.json").read_text())
    manifest_path = base / "plugins/zlib-skill/.codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    if marketplace.get("name") != "zlib-skill" or manifest.get("name") != "zlib-skill":
        return 1
    if not (base / "plugins/zlib-skill/skills/zlib-skill/SKILL.md").is_file():
        return 1
    if not (base / "plugins/zlib-skill/scripts/run.py").is_file():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
