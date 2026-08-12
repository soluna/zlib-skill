"""Bounded, deterministic search coordination.

The engine owns source-specific adapters; this module owns the policy that is
common to a ``find book`` operation.  Adapters return ``(items, status)`` and
are deliberately easy to replace with in-memory functions in tests.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .operation import CancellationToken, OperationBudget, OperationCancelled, OperationTimedOut

_MD5 = re.compile(r"^[0-9a-f]{32}$", re.I)
_ZLIB_ID = re.compile(r"^zlib:([0-9]+):([A-Za-z0-9_-]+)$")
_ANNA_ID = re.compile(r"^anna:([0-9a-f]{32})$", re.I)
_ZLIB_BOOK = re.compile(r"^[0-9]+$")
_ZLIB_HASH = re.compile(r"^[A-Za-z0-9_-]+$")
_TEXT_FIELDS = (
    "title",
    "author",
    "edition",
    "publisher",
    "identifier",
    "language",
    "extension",
    "size",
)
_MAX_TEXT = 512
_MAX_URL = 2048
_MAX_DEPTH = 5
_SOURCE_ORDER = {"zlib": 0, "anna": 1}


@dataclass(frozen=True)
class SearchRequest:
    query: str
    source: str = "all"
    limit: int = 10
    page: int = 1
    year_from: int | None = None
    year_to: int | None = None
    lang: str | None = None
    ext: tuple[str, ...] = ()
    order: str | None = None


@dataclass
class SearchSourceResult:
    source: str
    items: list[dict[str, Any]] = field(default_factory=list)
    status: Any = None
    outcome: str = "ok"
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def status_dict(self) -> dict[str, Any]:
        value = self.status
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        if isinstance(value, Mapping):
            result = dict(value)
        else:
            result = {"source": self.source, "status": "ok" if self.outcome == "ok" else "error"}
        result.setdefault("source", self.source)
        result.setdefault("outcome", self.outcome)
        if self.message:
            result.setdefault("message", self.message)
        if self.details:
            result.setdefault("details", self.details)
        return result


@dataclass
class SearchWorkflowResult:
    results: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    outcome: str = "ok"
    discarded_late: int = 0


def _text(value: Any, *, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    value = str(value).replace("\x00", " ")
    value = " ".join(value.split())
    return value[:limit] if value else None


def _valid_url(value: Any) -> str | None:
    text = _text(value, limit=_MAX_URL)
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return text


def _depth(value: Any, current: int = 0) -> int:
    if current > _MAX_DEPTH:
        return current
    if isinstance(value, Mapping):
        return max([current, *(_depth(v, current + 1) for v in value.values())])
    if isinstance(value, (list, tuple)):
        return max([current, *(_depth(v, current + 1) for v in value)])
    return current


def _year(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return _text(value, limit=16)
    if not 0 <= number <= 3000:
        return None
    return value if isinstance(value, str) else number


def _result_id(item: Mapping[str, Any], source: str) -> str | None:
    raw = item.get("result_id")
    if isinstance(raw, str):
        if source == "zlib" and _ZLIB_ID.fullmatch(raw):
            return raw
        if source == "anna" and _ANNA_ID.fullmatch(raw):
            return "anna:" + raw.split(":", 1)[1].lower()
    if source == "zlib":
        book_id, hash_id = item.get("id"), item.get("hash")
        if isinstance(book_id, (str, int)) and isinstance(hash_id, str):
            book_id, hash_id = str(book_id), hash_id
            if _ZLIB_BOOK.fullmatch(book_id) and _ZLIB_HASH.fullmatch(hash_id):
                return f"zlib:{book_id}:{hash_id}"
    if source == "anna":
        md5 = item.get("md5")
        if isinstance(md5, str) and _MD5.fullmatch(md5):
            return "anna:" + md5.lower()
    return None


def normalize_result(item: Any, source: str) -> dict[str, Any] | None:
    """Normalize one untrusted adapter item, returning ``None`` if invalid."""
    if not isinstance(item, Mapping) or _depth(item) > _MAX_DEPTH:
        return None
    source = str(source).lower()
    result_id = _result_id(item, source)
    if result_id is None:
        return None
    result: dict[str, Any] = {"result_id": result_id, "source": source}
    for field_name in _TEXT_FIELDS:
        value = _text(item.get(field_name))
        if value is not None:
            result[field_name] = value
    if source == "zlib":
        if "id" in item:
            result["id"] = result_id.split(":", 2)[1]
        if "hash" in item:
            result["hash"] = result_id.rsplit(":", 1)[1]
    else:
        if "md5" in item:
            result["md5"] = result_id.split(":", 1)[1]
    year = _year(item.get("year"))
    if year is not None:
        result["year"] = year
    for field_name in ("detail_url", "url"):
        url = _valid_url(item.get(field_name))
        if url:
            result[field_name] = url
    # Lists from source adapters are kept bounded and only as safe strings/URLs.
    if isinstance(item.get("sources"), Sequence) and not isinstance(
        item.get("sources"), (str, bytes)
    ):
        urls = [_valid_url(value) for value in item["sources"][:20]]
        result["sources"] = [value for value in urls if value]
    for field_name in (
        "can_download",
        "can_attempt_download",
        "requires_account",
        "best_effort",
        "download_guaranteed",
    ):
        if isinstance(item.get(field_name), bool):
            result[field_name] = item[field_name]
    return result


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[\w]+", value.casefold()) if len(token) > 1}


def score_result(item: Mapping[str, Any], query: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    title = str(item.get("title") or "")
    author = str(item.get("author") or "")
    title_tokens, author_tokens = _tokens(title), _tokens(author)
    score = 0.0
    score += 10.0 * len(query_tokens & title_tokens)
    score += 3.0 * len(query_tokens & author_tokens)
    if query.casefold().strip() in title.casefold():
        score += 20.0
    return score


def _group_key(item: Mapping[str, Any]) -> tuple[str, str, Any]:
    title = " ".join(str(item.get("title") or "").casefold().split())
    author = " ".join(str(item.get("author") or "").casefold().split())
    year = item.get("year")
    return title, author, year if isinstance(year, int) else None


def merge_results(items: Sequence[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, Any], dict[str, Any]] = {}
    scores: dict[tuple[str, str, Any], float] = {}
    for item in items:
        key = _group_key(item)
        score = score_result(item, query)
        existing = groups.get(key)
        if existing is None:
            groups[key] = dict(item)
            scores[key] = score
            continue
        alternatives = existing.setdefault("alternatives", [])
        if existing.get("result_id") != item.get("result_id") and item not in alternatives:
            alternatives.append(dict(item))
        # Prefer a downloadable variant, then source rank, then stable id.
        current_rank = (
            not bool(existing.get("can_download")),
            _SOURCE_ORDER.get(str(existing.get("source")), 99),
            str(existing.get("result_id")),
        )
        candidate_rank = (
            not bool(item.get("can_download")),
            _SOURCE_ORDER.get(str(item.get("source")), 99),
            str(item.get("result_id")),
        )
        if candidate_rank < current_rank:
            replacement = dict(item)
            replacement["alternatives"] = [existing, *alternatives]
            groups[key] = replacement
    ordered = sorted(
        groups.items(),
        key=lambda pair: (
            _SOURCE_ORDER.get(str(pair[1].get("source")), 99),
            -scores[pair[0]],
            str(pair[1].get("title") or "").casefold(),
            str(pair[1].get("author") or "").casefold(),
            str(pair[1].get("result_id") or ""),
        ),
    )
    output = []
    for _, value in ordered[: max(0, int(limit))]:
        if "alternatives" in value:
            value["alternatives"] = value["alternatives"][:20]
        output.append(value)
    return output


def _invoke_adapter(
    adapter: Callable[..., Any],
    request: SearchRequest,
    budget: OperationBudget,
    token: CancellationToken,
) -> Any:
    try:
        params = inspect.signature(adapter).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    if "budget" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs["budget"] = budget
    if "cancellation" in params:
        kwargs["cancellation"] = token
    elif "token" in params:
        kwargs["token"] = token
    if "request" in params:
        kwargs["request"] = request
        return adapter(**kwargs)
    if kwargs:
        try:
            return adapter(request, **kwargs)
        except TypeError:
            return adapter(**kwargs)
    return adapter(request)


class SearchWorkflow:
    """Coordinate source adapters under one deadline and cancellation token."""

    def __init__(self, adapters: Mapping[str, Callable[..., Any]], *, max_workers: int = 2) -> None:
        self.adapters = dict(adapters)
        self.max_workers = max(1, int(max_workers))

    def find(
        self,
        request: SearchRequest | str,
        *,
        source: str | None = None,
        budget: OperationBudget | None = None,
        cancellation: CancellationToken | None = None,
    ) -> SearchWorkflowResult:
        if isinstance(request, str):
            request = SearchRequest(request, source=source or "all")
        selected = source or request.source
        names = (
            [selected]
            if selected != "all"
            else [name for name in ("zlib", "anna") if name in self.adapters]
        )
        budget = budget or OperationBudget.unlimited(cancellation=cancellation)
        if cancellation is not None and budget.token is not cancellation:
            budget = OperationBudget(deadline=budget.deadline, cancellation=cancellation)
        token = budget.token
        source_results: dict[str, SearchSourceResult] = {}
        all_items: list[dict[str, Any]] = []
        discarded_late = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(names) or 1)
        ) as executor:
            futures = {
                executor.submit(_invoke_adapter, self.adapters[name], request, budget, token): name
                for name in names
                if name in self.adapters
            }
            pending = set(futures)
            while pending:
                try:
                    timeout = budget.remaining()
                except OperationCancelled:
                    timeout = 0.0
                except OperationTimedOut:
                    timeout = 0.0
                done, pending = concurrent.futures.wait(pending, timeout=timeout)
                if not done and pending:
                    if token.cancelled:
                        outcome = "cancelled"
                    else:
                        outcome = "timed_out"
                        token.cancel("deadline exceeded")
                    for future in pending:
                        future.cancel()
                        name = futures[future]
                        source_results[name] = SearchSourceResult(
                            name, outcome=outcome, message="Search operation did not complete."
                        )
                    discarded_late += len(pending)
                    break
                for future in done:
                    name = futures[future]
                    if token.cancelled or (
                        budget.deadline is not None and time.monotonic() > budget.deadline
                    ):
                        discarded_late += 1
                        outcome = "cancelled" if token.cancelled else "timed_out"
                        source_results[name] = SearchSourceResult(
                            name, outcome=outcome, message="Late source result discarded."
                        )
                        continue
                    try:
                        value = future.result()
                        items, status = (
                            value if isinstance(value, tuple) and len(value) == 2 else (value, None)
                        )
                        normalized = [
                            item
                            for item in (normalize_result(raw, name) for raw in (items or []))
                            if item
                        ]
                        source_results[name] = SearchSourceResult(
                            name, normalized, status=status, outcome="ok"
                        )
                        all_items.extend(normalized)
                    except OperationCancelled:
                        source_results[name] = SearchSourceResult(
                            name, outcome="cancelled", message="Search cancelled."
                        )
                    except OperationTimedOut:
                        source_results[name] = SearchSourceResult(
                            name, outcome="timed_out", message="Search timed out."
                        )
                    except Exception as exc:
                        source_results[name] = SearchSourceResult(
                            name,
                            outcome="unavailable",
                            message="Source search failed.",
                            details={"error_type": type(exc).__name__},
                        )
        # Adapters missing from the selected set have a stable unavailable row.
        for name in names:
            source_results.setdefault(
                name,
                SearchSourceResult(
                    name, outcome="unavailable", message="Source adapter unavailable."
                ),
            )
        source_values = [source_results[name] for name in names]
        outcomes = {value.outcome for value in source_values}
        overall = (
            "ok"
            if any(value.outcome == "ok" for value in source_values)
            else (
                "cancelled"
                if "cancelled" in outcomes
                else "timed_out"
                if "timed_out" in outcomes
                else "unavailable"
            )
        )
        return SearchWorkflowResult(
            results=merge_results(all_items, request.query, request.limit),
            sources=[
                value.status_dict() | ({"outcome": value.outcome} if value.outcome != "ok" else {})
                for value in source_values
            ],
            outcome=overall,
            discarded_late=discarded_late,
        )


__all__ = [
    "SearchRequest",
    "SearchSourceResult",
    "SearchWorkflow",
    "SearchWorkflowResult",
    "merge_results",
    "normalize_result",
    "score_result",
]
