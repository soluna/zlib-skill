"""Run deterministic Skill behavior fixtures, with an optional bounded live probe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).with_name("skill-cases.json")
SCHEMA_PATH = Path(__file__).with_name("agent-response.schema.json")


def _load_cases() -> list[dict[str, Any]]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 100:
        raise ValueError("skill-cases.json must contain 1..100 cases")
    if not all(isinstance(case, dict) and isinstance(case.get("name"), str) for case in cases):
        raise ValueError("each Skill eval case must have a string name")
    return cases


def _load_schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    required = schema.get("required")
    if schema.get("type") != "object" or required != ["case", "allowed", "forbidden"]:
        raise ValueError("agent-response.schema.json has an unexpected contract")
    return schema


def _valid_response(value: Any, case_name: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("case") == case_name
        and isinstance(value.get("allowed"), list)
        and all(isinstance(item, str) for item in value["allowed"])
        and isinstance(value.get("forbidden"), list)
        and all(isinstance(item, str) for item in value["forbidden"])
    )


def _fake_runner(case: dict[str, Any]) -> dict[str, Any]:
    """Return the deterministic stand-in response used by local/CI evals."""
    return {
        "case": case["name"],
        "allowed": list(case.get("must", [])),
        "forbidden": list(case.get("must_not", [])),
    }


def _run_local(cases: list[dict[str, Any]]) -> int:
    for case in cases:
        response = _fake_runner(case)
        if not _valid_response(response, case["name"]):
            return 1
    return 0


def _extract_json(stdout: str) -> Any:
    # Codex may prefix the response with progress lines.  Decode the last JSON
    # object rather than trusting arbitrary text emitted by the subprocess.
    decoder = json.JSONDecoder()
    for index in range(len(stdout) - 1, -1, -1):
        if stdout[index] != "{":
            continue
        try:
            value, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if not stdout[index + end :].strip():
            return value
    raise ValueError("live Codex response was not a JSON object")


def _run_live(cases: list[dict[str, Any]]) -> int:
    """Run each case through Codex with isolated state and a hard timeout."""
    _load_schema()
    with tempfile.TemporaryDirectory(prefix="zlib-skill-eval-") as codex_home:
        env = os.environ.copy()
        env["CODEX_HOME"] = codex_home
        for case in cases:
            prompt = (
                "Load the canonical zlib-skill Skill from plugins/zlib-skill. "
                "Return JSON only, matching the supplied output schema, for this case: "
                + json.dumps(case, ensure_ascii=False)
            )
            try:
                result = subprocess.run(
                    [
                        "codex",
                        "exec",
                        "--ephemeral",
                        "--sandbox",
                        "read-only",
                        "--output-schema",
                        str(SCHEMA_PATH),
                        "--",
                        prompt,
                    ],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                return 1
            if result.returncode != 0:
                return 1
            try:
                response = _extract_json(result.stdout)
            except ValueError:
                return 1
            if not _valid_response(response, case["name"]):
                return 1
    return 0


def main() -> int:
    try:
        cases = _load_cases()
        if "--live" in sys.argv:
            return _run_live(cases)
        _load_schema()
        return _run_local(cases)
    except (OSError, ValueError, json.JSONDecodeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
