from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import check_coverage  # noqa: E402


def test_coverage_gate_rejects_low_total(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"totals": {"percent_covered": 1}, "files": {}}))
    assert check_coverage.main(str(path)) == 1


def _coverage_fixture(percent: float) -> dict:
    files = {
        f"plugins/zlib-skill/scripts/zlib_anna/{name}": {"summary": {"percent_covered": percent}}
        for name in check_coverage.REQUIRED
    }
    return {"totals": {"percent_covered": percent}, "files": files}


def test_coverage_gate_rejects_missing_canonical_module(tmp_path):
    payload = _coverage_fixture(100)
    payload["files"].pop("plugins/zlib-skill/scripts/zlib_anna/schema.py")
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(payload))
    assert check_coverage.main(str(path)) == 1


def test_coverage_gate_rejects_module_below_threshold(tmp_path):
    payload = _coverage_fixture(100)
    payload["files"]["plugins/zlib-skill/scripts/zlib_anna/schema.py"]["summary"][
        "percent_covered"
    ] = check_coverage.MINIMUM_PERCENT - 0.01
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(payload))
    assert check_coverage.main(str(path)) == 1


def test_coverage_gate_accepts_module_at_threshold(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_coverage_fixture(check_coverage.MINIMUM_PERCENT)))
    assert check_coverage.main(str(path)) == 0
