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
