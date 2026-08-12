"""Small operation-budget primitives shared by source implementations.

The source adapters deliberately remain separate modules.  This module only
owns the values that must be shared by a single user operation: a monotonic
deadline, cancellation, and a bounded attempt journal.  Nothing in here
knows about a particular HTTP client or a source's response format.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable

from .network_safety import (
    ALLOW_INSECURE_HTTP_ENV,
    LEGACY_ALLOW_INSECURE_HTTP_ENV,
    PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
    env_flag,
    validate_http_url,
)


class OperationError(RuntimeError):
    """Base class for an operation that cannot continue."""


class OperationTimedOut(OperationError):
    """Raised when the operation's total budget has elapsed."""


class OperationCancelled(OperationError):
    """Raised when a caller cancelled the operation."""


class CancellationToken:
    """Thread-safe, intentionally tiny cancellation token.

    A token can be shared by all source pools participating in one command.
    Cancellation is idempotent and never stores arbitrary exception text (the
    reason is a short, local diagnostic only).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "cancelled"

    def cancel(self, reason: str = "cancelled") -> bool:
        with self._lock:
            first = not self._event.is_set()
            if first:
                value = str(reason).strip()
                self._reason = value[:120] or "cancelled"
                self._event.set()
            return first

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    # A method is useful for callers that use the conventional API.
    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled(self._reason)


@dataclass
class OperationBudget:
    """A monotonic total deadline plus a shared cancellation token."""

    deadline: float | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)

    @classmethod
    def from_seconds(
        cls,
        seconds: float | int | None,
        *,
        cancellation: CancellationToken | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> OperationBudget:
        if seconds is None:
            deadline = None
        else:
            value = float(seconds)
            if not math.isfinite(value) or value < 0:
                raise ValueError("operation deadline must not be negative")
            deadline = clock() + value
        return cls(deadline=deadline, cancellation=cancellation or CancellationToken())

    @classmethod
    def unlimited(cls, *, cancellation: CancellationToken | None = None) -> OperationBudget:
        return cls(deadline=None, cancellation=cancellation or CancellationToken())

    @property
    def token(self) -> CancellationToken:
        """Alias used by requesters and tests."""
        return self.cancellation

    def remaining(self, *, clock: Callable[[], float] = time.monotonic) -> float | None:
        self.cancellation.raise_if_cancelled()
        if self.deadline is None:
            return None
        remaining = self.deadline - clock()
        if remaining <= 0:
            raise OperationTimedOut("operation deadline exceeded")
        return remaining

    def timeout(
        self,
        requested: float | int | None = None,
        *,
        minimum: float = 0.001,
        clock: Callable[[], float] = time.monotonic,
    ) -> float | None:
        """Return a per-request timeout bounded by the total deadline."""
        remaining = self.remaining(clock=clock)
        if remaining is None:
            return float(requested) if requested is not None else None
        value = remaining if requested is None else min(remaining, float(requested))
        if value <= 0:
            raise OperationTimedOut("operation deadline exceeded")
        return max(minimum, value) if remaining >= minimum else remaining

    def check(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.remaining(clock=clock)

    def sleep(
        self,
        seconds: float | int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Sleep for backoff while respecting cancellation and deadline."""
        remaining = self.remaining(clock=clock)
        wait_for = float(seconds)
        if remaining is not None:
            wait_for = min(wait_for, remaining)
        if wait_for <= 0:
            return
        if self.cancellation.wait(wait_for):
            self.cancellation.raise_if_cancelled()
        self.check(clock=clock)


# Friendly aliases used by older adapters and external callers.
OperationDeadline = OperationBudget
DeadlineExceeded = OperationTimedOut


@dataclass(frozen=True)
class Attempt:
    source: str
    origin: str
    operation: str
    outcome: str
    started_at: float
    elapsed_seconds: float
    error_type: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "origin": self.origin,
            "operation": self.operation,
            "outcome": self.outcome,
            "elapsed_seconds": round(max(0.0, self.elapsed_seconds), 3),
        }
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.detail:
            payload["detail"] = self.detail[:160]
        return payload


class AttemptLog:
    """Bounded, thread-safe record of origin attempts."""

    def __init__(self, *, max_entries: int = 256) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: list[Attempt] = []
        self._lock = threading.Lock()

    def append(self, attempt: Attempt) -> None:
        with self._lock:
            self._entries.append(attempt)
            if len(self._entries) > self.max_entries:
                del self._entries[: len(self._entries) - self.max_entries]

    record = append

    def snapshot(self) -> list[Attempt]:
        with self._lock:
            return list(self._entries)

    def to_list(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.snapshot()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


@dataclass
class PoolResult:
    """Stable result returned by :class:`OriginPool.execute`."""

    value: Any = None
    origin: str | None = None
    status: str = "unknown"
    outcome: str = "unavailable"
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "outcome": self.outcome,
            "attempts": list(self.attempts),
        }
        if self.origin:
            payload["origin"] = self.origin
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.value is not None:
            payload["value"] = self.value
        return payload


class OriginPool:
    """Source-local origin failover with a shared operation budget.

    ``operation`` is supplied by the caller and receives ``(origin, timeout,
    token)``.  The adapter owns what constitutes a successful response and may
    raise ``requests`` exceptions, ``OperationTimedOut`` or
    ``OperationCancelled``.  This keeps trust and response parsing local to
    each source instead of creating a generic remote-source interface.
    """

    def __init__(
        self,
        origins: Iterable[str],
        *,
        source: str = "source",
        budget: OperationBudget | None = None,
        cancellation: CancellationToken | None = None,
        attempt_log: AttemptLog | None = None,
        cooldown_seconds: float = 0.0,
        max_attempts: int | None = None,
    ) -> None:
        self.source = str(source)
        self.origins = tuple(str(item).rstrip("/") for item in origins if str(item).strip())
        self.budget = budget or OperationBudget.unlimited(cancellation=cancellation)
        if cancellation is not None and self.budget.token is not cancellation:
            self.budget = OperationBudget(self.budget.deadline, cancellation)
        self.cancellation = self.budget.token
        self.attempt_log = attempt_log or AttemptLog()
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.max_attempts = max_attempts
        self._cooldown_until: dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

    def _available_origins(self) -> list[str]:
        now = time.monotonic()
        with self._cooldown_lock:
            return [origin for origin in self.origins if self._cooldown_until.get(origin, 0) <= now]

    def cool(self, origin: str, seconds: float | None = None) -> None:
        with self._cooldown_lock:
            self._cooldown_until[origin] = time.monotonic() + (
                self.cooldown_seconds if seconds is None else max(0.0, float(seconds))
            )

    def _record(
        self,
        *,
        origin: str,
        operation: str,
        outcome: str,
        started: float,
        exc: BaseException | None = None,
    ) -> None:
        error_type = type(exc).__name__ if exc else None
        detail = None
        if exc and not isinstance(exc, (OperationTimedOut, OperationCancelled)):
            # Never copy remote exception text into the public attempt log.
            detail = "request failed"
        self.attempt_log.append(
            Attempt(
                source=self.source,
                origin=origin,
                operation=operation,
                outcome=outcome,
                started_at=started,
                elapsed_seconds=time.monotonic() - started,
                error_type=error_type,
                detail=detail,
            )
        )

    def execute(
        self,
        operation: str,
        call: Callable[[str, float | None, CancellationToken], Any],
        *,
        timeout: float | None = None,
        retryable: Callable[[BaseException], bool] | None = None,
    ) -> PoolResult:
        """Try origins in stable order and return a non-secret result."""
        origins = self._available_origins()
        if not origins:
            return PoolResult(
                status="unavailable", outcome="unavailable", attempts=self.attempt_log.to_list()
            )
        last_error: BaseException | None = None
        for index, origin in enumerate(origins):
            if self.max_attempts is not None and index >= self.max_attempts:
                break
            started = time.monotonic()
            try:
                self.budget.check()
                request_timeout = self.budget.timeout(timeout)
                value = call(origin, request_timeout, self.cancellation)
                self.budget.check()
                self._record(origin=origin, operation=operation, outcome="ok", started=started)
                return PoolResult(
                    value=value,
                    origin=origin,
                    status="ok",
                    outcome="ok",
                    attempts=self.attempt_log.to_list(),
                )
            except OperationCancelled as exc:
                self._record(
                    origin=origin,
                    operation=operation,
                    outcome="cancelled",
                    started=started,
                    exc=exc,
                )
                return PoolResult(
                    status="cancelled",
                    outcome="cancelled",
                    attempts=self.attempt_log.to_list(),
                    error_type=type(exc).__name__,
                )
            except OperationTimedOut as exc:
                self._record(
                    origin=origin,
                    operation=operation,
                    outcome="timed_out",
                    started=started,
                    exc=exc,
                )
                return PoolResult(
                    status="error",
                    outcome="timed_out",
                    attempts=self.attempt_log.to_list(),
                    error_type=type(exc).__name__,
                )
            except Exception as exc:  # classify and continue to next origin
                if self.cancellation.cancelled:
                    cancelled = OperationCancelled(self.cancellation.reason)
                    self._record(
                        origin=origin,
                        operation=operation,
                        outcome="cancelled",
                        started=started,
                        exc=cancelled,
                    )
                    return PoolResult(
                        status="cancelled",
                        outcome="cancelled",
                        attempts=self.attempt_log.to_list(),
                        error_type="OperationCancelled",
                    )
                try:
                    self.budget.check()
                except OperationTimedOut as timeout_exc:
                    self._record(
                        origin=origin,
                        operation=operation,
                        outcome="timed_out",
                        started=started,
                        exc=timeout_exc,
                    )
                    return PoolResult(
                        status="error",
                        outcome="timed_out",
                        attempts=self.attempt_log.to_list(),
                        error_type="OperationTimedOut",
                    )
                last_error = exc
                self._record(
                    origin=origin, operation=operation, outcome="error", started=started, exc=exc
                )
                self.cool(origin)
                if retryable is not None and not retryable(exc):
                    break
                # Cooldown is source-local state for subsequent operations. Do
                # not sleep before trying another origin: a failed mirror must
                # not consume the caller's total budget while a healthy
                # fallback is available.
        return PoolResult(
            status="error" if last_error else "unavailable",
            outcome="unavailable",
            attempts=self.attempt_log.to_list(),
            error_type=type(last_error).__name__ if last_error else None,
        )

    def request(
        self,
        operation: str,
        requester: Any,
        *,
        method: str = "get",
        path: str = "",
        timeout: float | None = None,
        **kwargs: Any,
    ) -> PoolResult:
        """Convenience request seam for deterministic stand-in requesters."""

        verb = method.lower()

        def call(origin: str, request_timeout: float | None, token: CancellationToken) -> Any:
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
            fn = getattr(requester, verb, None)
            if fn is None:
                fn = requester.request
                return fn(verb.upper(), url, timeout=request_timeout, **kwargs)
            return fn(url, timeout=request_timeout, **kwargs)

        return self.execute(operation, call, timeout=timeout)


__all__ = [
    "Attempt",
    "AttemptLog",
    "CancellationToken",
    "DeadlineExceeded",
    "OperationBudget",
    "OperationCancelled",
    "OperationDeadline",
    "OperationError",
    "OperationTimedOut",
    "OriginPool",
    "PoolResult",
]
