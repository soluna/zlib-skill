"""Compatibility package that delegates implementation to the plugin source."""

from pathlib import Path

SKILL_VERSION = "0.3.1"
SCHEMA_VERSION = "2"

# Keep the historical ``scripts`` import path working for callers and tests,
# while ensuring there is only one implementation tree to audit and package.
_CANONICAL_PACKAGE = (
    Path(__file__).resolve().parents[2] / "plugins" / "zlib-skill" / "scripts" / "zlib_anna"
)
__path__ = [str(_CANONICAL_PACKAGE)]
