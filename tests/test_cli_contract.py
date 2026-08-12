"""Black-box contract tests for schema 2 CLI responses."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import run as runner
from zlib_anna import SKILL_VERSION, engine, schema

ROOT = Path(__file__).parent.parent
SCHEMA_ARTIFACT = ROOT / "references" / "schema-v2.json"


def _schema_document():
    return json.loads(SCHEMA_ARTIFACT.read_text(encoding="utf-8"))


def _assert_schema(value, definition_name):
    document = _schema_document()
    assert definition_name in document["$defs"], f"missing schema definition: {definition_name}"
    _assert_fragment(value, document["$defs"][definition_name], document)
    _assert_fragment(value, document["$defs"]["publicResponse"], document)


def _assert_fragment(value, fragment, document):
    if "$ref" in fragment:
        prefix = "#/$defs/"
        assert fragment["$ref"].startswith(prefix)
        _assert_fragment(value, document["$defs"][fragment["$ref"][len(prefix) :]], document)
    for item in fragment.get("allOf", []):
        _assert_fragment(value, item, document)
    if "anyOf" in fragment:
        matches = []
        for item in fragment["anyOf"]:
            try:
                _assert_fragment(value, item, document)
            except AssertionError:
                continue
            matches.append(item)
        assert matches, f"value did not match any schema variant: {value!r}"

    expected_type = fragment.get("type")
    if expected_type:
        allowed_types = expected_type if isinstance(expected_type, list) else [expected_type]
        type_checks = {
            "array": lambda item: isinstance(item, list),
            "boolean": lambda item: isinstance(item, bool),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "null": lambda item: item is None,
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "object": lambda item: isinstance(item, dict),
            "string": lambda item: isinstance(item, str),
        }
        assert any(type_checks[item](value) for item in allowed_types), (
            f"expected {allowed_types}, got {type(value).__name__}"
        )
    if "const" in fragment:
        assert value == fragment["const"]
    if "enum" in fragment:
        assert value in fragment["enum"]
    if isinstance(value, dict):
        for key in fragment.get("required", []):
            assert key in value, f"missing required field: {key}"
        for key, item in fragment.get("properties", {}).items():
            if key in value:
                _assert_fragment(value[key], item, document)
    if isinstance(value, list) and "items" in fragment:
        for item in value:
            _assert_fragment(item, fragment["items"], document)


def _run_json(argv, capsys):
    exit_code = engine.main([*argv, "--json"])
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out)


def test_schema_module_builds_compatible_public_envelopes():
    success = schema.success_envelope(example="value")
    failure = schema.failure_envelope(
        code="EXAMPLE_ERROR",
        message="Example failure.",
        recoverable=True,
        suggestions=["Retry."],
        details={"reason": "fixture"},
    )

    assert success == {
        "ok": True,
        "schema_version": "2",
        "skill_version": SKILL_VERSION,
        "example": "value",
    }
    assert failure == {
        "ok": False,
        "schema_version": "2",
        "skill_version": SKILL_VERSION,
        "error": {
            "code": "EXAMPLE_ERROR",
            "message": "Example failure.",
            "recoverable": True,
            "suggestions": ["Retry."],
            "details": {"reason": "fixture"},
        },
    }


def test_schema_artifact_declares_the_public_envelope_contract():
    document = json.loads(SCHEMA_ARTIFACT.read_text(encoding="utf-8"))

    assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert document["$ref"] == "#/$defs/publicResponse"
    assert {
        "envelope",
        "error",
        "errorEnvelope",
        "sourceStatus",
        "publicResponse",
    } <= document["$defs"].keys()
    assert document["$defs"]["envelope"]["required"] == [
        "ok",
        "schema_version",
        "skill_version",
    ]
    assert document["$defs"]["error"]["required"] == [
        "code",
        "message",
        "recoverable",
    ]


def test_auth_status_is_a_schema_valid_public_response(monkeypatch, tmp_path, capsys):
    config_dir = tmp_path / "config"
    monkeypatch.setattr(engine, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(engine, "CONFIG_FILE", config_dir / "config.json")

    exit_code, payload = _run_json(["auth", "status"], capsys)

    assert exit_code == 0
    _assert_schema(payload, "authResponse")


def test_search_controlled_failure_is_a_schema_valid_public_response(capsys):
    exit_code, payload = _run_json(["search", ""], capsys)

    assert exit_code == 1
    assert payload["error"]["code"] == "QUERY_REQUIRED"
    _assert_schema(payload, "searchResponse")


def test_resolve_controlled_failure_is_a_schema_valid_public_response(capsys):
    exit_code, payload = _run_json(["resolve", "unknown-id"], capsys)

    assert exit_code == 1
    assert payload["error"]["code"] == "SOURCE_REQUIRED"
    _assert_schema(payload, "resolveResponse")


def test_download_controlled_failure_is_a_schema_valid_public_response(capsys):
    exit_code, payload = _run_json(["download", "unknown-id"], capsys)

    assert exit_code == 1
    assert payload["error"]["code"] == "SOURCE_REQUIRED"
    _assert_schema(payload, "downloadResponse")


def test_account_info_controlled_failure_is_a_schema_valid_public_response(
    monkeypatch, tmp_path, capsys
):
    config_dir = tmp_path / "config"
    monkeypatch.setattr(engine, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(engine, "CONFIG_FILE", config_dir / "config.json")

    exit_code, payload = _run_json(["info"], capsys)

    assert exit_code == 1
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    _assert_schema(payload, "accountResponse")


def test_domains_offline_success_is_a_schema_valid_public_response(monkeypatch, capsys):
    def offline(*_args, **_kwargs):
        raise engine.requests.ConnectionError("offline fixture")

    monkeypatch.setattr(engine.requests, "get", offline)

    exit_code, payload = _run_json(["domains"], capsys)

    assert exit_code == 0
    assert payload["source"] == "fallback"
    assert all(item["available"] is False for item in payload["domains"])
    _assert_schema(payload, "domainsResponse")


def test_popular_controlled_failure_is_a_schema_valid_public_response(
    monkeypatch, tmp_path, capsys
):
    config_dir = tmp_path / "config"
    monkeypatch.setattr(engine, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(engine, "CONFIG_FILE", config_dir / "config.json")

    exit_code, payload = _run_json(["popular"], capsys)

    assert exit_code == 1
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    _assert_schema(payload, "popularResponse")


def test_doctor_offline_success_is_a_schema_valid_public_response(monkeypatch, tmp_path, capsys):
    config_dir = tmp_path / "config"
    monkeypatch.setattr(engine, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(engine, "CONFIG_FILE", config_dir / "config.json")

    def offline(*_args, **_kwargs):
        raise engine.requests.ConnectionError("offline fixture")

    monkeypatch.setattr(engine.requests, "get", offline)

    exit_code, payload = _run_json(["doctor"], capsys)

    assert exit_code == 0
    assert {item["source"] for item in payload["sources"]} == {"zlib", "anna"}
    assert all(item["available"] is False for item in payload["sources"])
    assert payload["overall_status"] == "unavailable"
    assert payload["usable"] is False
    assert isinstance(payload["next_actions"], list)
    assert all(item["outcome"] == "unavailable" for item in payload["sources"])
    _assert_schema(payload, "doctorResponse")


@pytest.mark.parametrize(
    ("available", "expected_status", "expected_usable"),
    [(2, "healthy", True), (1, "degraded", True), (0, "unavailable", False)],
)
def test_doctor_overall_status_is_explicit(monkeypatch, capsys, available, expected_status, expected_usable):
    statuses = [
        engine.SourceStatus(
            source=name,
            available=index < available,
            can_search=index < available,
            status="ok" if index < available else "unavailable",
        )
        for index, name in enumerate(("zlib", "anna"))
    ]
    monkeypatch.setattr(engine, "load_config", lambda **_: {})
    monkeypatch.setattr(engine, "config_status", lambda: {})
    monkeypatch.setattr(engine, "check_zlib", lambda _cfg: statuses[0])
    monkeypatch.setattr(engine, "check_anna", lambda _args: statuses[1])
    exit_code, payload = _run_json(["doctor"], capsys)
    assert exit_code == 0
    assert payload["overall_status"] == expected_status
    assert payload["usable"] is expected_usable
    assert len(payload["next_actions"]) >= (0 if expected_status == "healthy" else 1)
    _assert_schema(payload, "doctorResponse")


def test_batch_deadline_is_shared_and_late_items_are_cancelled(monkeypatch, tmp_path, capsys):
    batch_file = tmp_path / "batch.txt"
    batch_file.write_text("anna:0123456789abcdef0123456789abcdef\n" * 2, encoding="utf-8")
    calls = []

    def slow_download(_args, _item_id):
        calls.append(True)
        raise engine.SkillError("SOURCE_TIMEOUT", "timed out")

    monkeypatch.setattr(engine, "download_anna", slow_download)
    exit_code, payload = _run_json(["batch", str(batch_file), "--deadline-seconds", "0.001"], capsys)
    assert exit_code == 0
    assert payload["count"] == 2
    assert len(calls) <= 1
    assert all("error" in item for item in payload["results"])
    assert any(item["error"]["code"] in {"SOURCE_TIMEOUT", "OPERATION_TIMED_OUT", "OPERATION_CANCELLED"} for item in payload["results"])


def test_batch_controlled_failure_is_a_schema_valid_public_response(tmp_path, capsys):
    missing_batch = tmp_path / "missing.txt"

    exit_code, payload = _run_json(["batch", str(missing_batch)], capsys)

    assert exit_code == 1
    assert payload["error"]["code"] == "FILE_NOT_FOUND"
    _assert_schema(payload, "batchResponse")


def test_runner_setup_failure_is_a_schema_valid_public_response(monkeypatch, capsys):
    def fail_setup():
        raise runner.RuntimeSetupError("install_dependencies", RuntimeError("private detail"))

    monkeypatch.setattr(runner, "ensure_runtime", fail_setup)

    exit_code = runner.main(["auth", "status", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["error"]["code"] == "RUNTIME_SETUP_FAILED"
    assert payload["error"]["details"] == {
        "step": "install_dependencies",
        "error_type": "RuntimeError",
    }
    assert "private detail" not in json.dumps(payload)
    _assert_schema(payload, "runnerResponse")
