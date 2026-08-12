"""Validate the repository-owned plugin manifest and canonical Skill path."""

from __future__ import annotations

import json
from pathlib import Path


def main(root: str = ".") -> int:
    base = Path(root).resolve()
    marketplace = json.loads((base / ".agents/plugins/marketplace.json").read_text())
    manifest_path = base / "plugins/zlib-skill/.codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text())
    entries = marketplace.get("plugins", [])
    if marketplace.get("name") != "zlib-skill" or not any(
        item.get("name") == "zlib-skill" and item.get("source") == "./plugins/zlib-skill"
        for item in entries
    ):
        return 1
    if manifest.get("name") != "zlib-skill" or not manifest.get("version"):
        return 1
    if manifest.get("skills") != ["./skills/zlib-skill"]:
        return 1
    if any(key in manifest for key in ("mcp", "apps", "hooks")):
        return 1
    if not (base / "plugins/zlib-skill/skills/zlib-skill/SKILL.md").is_file():
        return 1
    if not (base / "plugins/zlib-skill/scripts/run.py").is_file():
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
