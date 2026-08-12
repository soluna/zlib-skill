"""Z-Library source-local pool and bounded JSON response handling.

The bundled ``zlibrary.Zlibrary`` class remains the protocol implementation;
this module gives it an operation-local origin pool and a narrow response
validation seam.  Tests and callers can inject a deterministic requester or
client factory without making real network calls.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable
from typing import Any, Callable

from .network_safety import (
    ALLOW_INSECURE_HTTP_ENV,
    LEGACY_ALLOW_INSECURE_HTTP_ENV,
    PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
    env_flag,
    validate_http_url,
)
from .operation import (
    AttemptLog,
    CancellationToken,
    OperationBudget,
    OriginPool,
    PoolResult,
)

DEFAULT_MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ZLIB_JSON_BYTES = DEFAULT_MAX_JSON_BYTES
JSON_CONTENT_TYPES = {"application/json", "text/json", "application/*+json"}


class ZlibResponseError(ValueError):
    """An untrusted Z-Library response failed bounded validation."""

    code = "ZLIB_RESPONSE_REJECTED"


class ZlibResponseTooLarge(ZlibResponseError):
    code = "ZLIB_RESPONSE_TOO_LARGE"


def _headers(response: Any) -> Any:
    return getattr(response, "headers", {}) or {}


def _header(response: Any, name: str) -> str | None:
    headers = _headers(response)
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None:
            value = headers.get(name.title())
    except AttributeError:
        return None
    return str(value) if value is not None else None


def _content_type(response: Any) -> str:
    return (_header(response, "content-type") or "").split(";", 1)[0].strip().lower()


def _declared_length(response: Any) -> int | None:
    value = _header(response, "content-length")
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ZlibResponseError("Z-Library response has invalid Content-Length") from exc
    if parsed < 0:
        raise ZlibResponseError("Z-Library response has invalid Content-Length")
    return parsed


def _iter_body(response: Any) -> Iterable[bytes]:
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        yielded = False
        for item in iterator(chunk_size=256 * 1024):
            if item:
                yielded = True
                yield bytes(item)
        if yielded:
            return
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray, memoryview)):
        yield bytes(content)
        return
    text = getattr(response, "text", None)
    if isinstance(text, str):
        yield text.encode("utf-8")


def bounded_response_bytes(
    response: Any,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
    expected_content_types: set[str] | None = None,
) -> bytes:
    """Read a response with declared-size and actual-byte limits.

    A missing content type is accepted for compatibility with old stand-ins;
    an explicitly incompatible type is always rejected.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    declared = _declared_length(response)
    if declared is not None and declared > max_bytes:
        raise ZlibResponseTooLarge("Z-Library response exceeds the size limit")
    media_type = _content_type(response)
    if expected_content_types and media_type:
        accepted = media_type in expected_content_types or (
            media_type.endswith("+json") and "application/*+json" in expected_content_types
        )
        if not accepted:
            raise ZlibResponseError("Z-Library response has an unexpected content type")
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in _iter_body(response):
            total += len(chunk)
            if total > max_bytes:
                raise ZlibResponseTooLarge("Z-Library response exceeds the size limit")
            chunks.append(chunk)
    except ZlibResponseError:
        raise
    except Exception as exc:
        raise ZlibResponseError("Z-Library response body is invalid") from exc
    return b"".join(chunks)


def parse_zlib_json(
    response: Any,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
) -> dict[str, Any]:
    """Validate and parse one bounded object JSON response."""
    body = bounded_response_bytes(
        response,
        max_bytes=max_bytes,
        expected_content_types=JSON_CONTENT_TYPES,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZlibResponseError("Z-Library response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ZlibResponseError("Z-Library response JSON must be an object")
    return payload


def parse_json_response(
    response: Any, *, max_bytes: int = DEFAULT_MAX_JSON_BYTES
) -> dict[str, Any]:
    """Compatibility alias for callers that do not mention the source."""
    return parse_zlib_json(response, max_bytes=max_bytes)


class ZlibSourcePool:
    """Z-Library's own origin pool; no Anna policy is shared here."""

    def __init__(
        self,
        origins: Iterable[str],
        *,
        client_factory: Callable[[str], Any] | None = None,
        requester: Any | None = None,
        budget: OperationBudget | None = None,
        cancellation: CancellationToken | None = None,
        attempt_log: AttemptLog | None = None,
        cooldown_seconds: float = 0.0,
        max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
    ) -> None:
        origin_values = tuple(origins)
        require_https = not env_flag(
            ALLOW_INSECURE_HTTP_ENV,
            PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
            LEGACY_ALLOW_INSECURE_HTTP_ENV,
        )
        for origin in origin_values:
            validate_http_url(origin, require_https=require_https, resolve_dns=False)
        self.pool = OriginPool(
            origin_values,
            source="zlib",
            budget=budget,
            cancellation=cancellation,
            attempt_log=attempt_log,
            cooldown_seconds=cooldown_seconds,
        )
        self.client_factory = client_factory
        self.requester = requester
        self.max_json_bytes = max_json_bytes

    @property
    def origins(self) -> tuple[str, ...]:
        return self.pool.origins

    @property
    def attempt_log(self) -> AttemptLog:
        return self.pool.attempt_log

    @property
    def cancellation(self) -> CancellationToken:
        return self.pool.cancellation

    @property
    def budget(self) -> OperationBudget:
        return self.pool.budget

    def request_json(
        self,
        operation: str,
        path: str,
        *,
        method: str = "get",
        timeout: float | None = None,
        **kwargs: Any,
    ) -> PoolResult:
        if self.requester is None:
            raise ValueError("requester is required for request_json")

        def call(
            origin: str, request_timeout: float | None, token: CancellationToken
        ) -> dict[str, Any]:
            token.raise_if_cancelled()
            url = (
                path
                if path.startswith(("http://", "https://"))
                else f"{origin.rstrip('/')}/{path.lstrip('/')}"
            )
            validate_http_url(
                url,
                require_https=not env_flag(
                    ALLOW_INSECURE_HTTP_ENV,
                    PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
                    LEGACY_ALLOW_INSECURE_HTTP_ENV,
                ),
                resolve_dns=False,
            )
            fn = getattr(self.requester, method.lower(), None)
            if fn is None:
                fn = self.requester.request
                response = fn(method.upper(), url, timeout=request_timeout, **kwargs)
            else:
                response = fn(url, timeout=request_timeout, **kwargs)
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            return parse_zlib_json(response, max_bytes=self.max_json_bytes)

        return self.pool.execute(operation, call, timeout=timeout)

    def call_client(
        self, operation: str, method: str, *args: Any, timeout: float | None = None, **kwargs: Any
    ) -> PoolResult:
        """Invoke a bundled client through this source's pool.

        ``client_factory`` is intentionally injected, which makes origin
        switching and timeout/cancellation behavior deterministic in tests.
        """
        if self.client_factory is None:
            raise ValueError("client_factory is required for call_client")

        def call(origin: str, request_timeout: float | None, token: CancellationToken) -> Any:
            token.raise_if_cancelled()
            client = self.client_factory(origin)
            fn = getattr(client, method)
            # Bundled client methods do not expose timeout; injected clients
            # may.  Keep compatibility while still passing budget to clients
            # that explicitly accept it.
            try:
                parameters = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                parameters = {}
            call_kwargs = dict(kwargs)
            if "timeout" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            ):
                call_kwargs["timeout"] = request_timeout
            if "cancellation" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            ):
                call_kwargs["cancellation"] = token
            return fn(*args, **call_kwargs)

        return self.pool.execute(operation, call, timeout=timeout)

    def search(self, **kwargs: Any) -> PoolResult:
        return self.call_client("search", "search", **kwargs)

    def resolve(self, *args: Any, **kwargs: Any) -> PoolResult:
        return self.call_client("resolve", "getBookInfo", *args, **kwargs)

    def download(self, *args: Any, **kwargs: Any) -> PoolResult:
        return self.call_client("download", "getBookDownload", *args, **kwargs)

    def doctor(self, **kwargs: Any) -> PoolResult:
        method = "doctor" if self.client_factory else "getDomains"
        return self.call_client("doctor", method, **kwargs)

    def login(self, *args: Any, **kwargs: Any) -> PoolResult:
        return self.call_client("login", "login", *args, **kwargs)

    def info(self, **kwargs: Any) -> PoolResult:
        return self.call_client("info", "getInfo", **kwargs)

    def domains(self, **kwargs: Any) -> PoolResult:
        return self.call_client("domains", "getDomains", **kwargs)

    def popular(self, **kwargs: Any) -> PoolResult:
        return self.call_client("popular", "getMostPopular", **kwargs)


ZlibOriginPool = ZlibSourcePool


__all__ = [
    "DEFAULT_MAX_JSON_BYTES",
    "JSON_CONTENT_TYPES",
    "MAX_ZLIB_JSON_BYTES",
    "ZlibOriginPool",
    "ZlibResponseError",
    "ZlibResponseTooLarge",
    "ZlibSourcePool",
    "bounded_response_bytes",
    "parse_json_response",
    "parse_zlib_json",
]
