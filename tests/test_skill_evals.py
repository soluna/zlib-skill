from __future__ import annotations

import json
from pathlib import Path


def test_skill_cases_are_bounded_and_structured():
    payload = json.loads((Path(__file__).parent.parent / "evals/skill-cases.json").read_text())
    assert len(payload["cases"]) <= 100
    assert all(isinstance(item["name"], str) for item in payload["cases"])
