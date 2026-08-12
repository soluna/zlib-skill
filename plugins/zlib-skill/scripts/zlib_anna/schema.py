"""Schema 2 response constructors shared by the engine and runtime runner."""

from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION, SKILL_VERSION


def error_object(
    *,
    code: str,
    message: str,
    recoverable: bool,
    suggestions: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable public error object used by schema 2 responses."""
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    }
    if suggestions:
        payload["suggestions"] = suggestions
    if details:
        payload["details"] = details
    return payload


def success_envelope(**fields: Any) -> dict[str, Any]:
    """Build a schema 2 success envelope while preserving command-specific fields."""
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
        **fields,
    }


def failure_envelope(
    *,
    code: str,
    message: str,
    recoverable: bool,
    suggestions: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema 2 failure envelope."""
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
        "error": error_object(
            code=code,
            message=message,
            recoverable=recoverable,
            suggestions=suggestions,
            details=details,
        ),
    }
