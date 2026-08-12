from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zlib_anna.operation import CancellationToken, OperationBudget  # noqa: E402
from zlib_anna.search_workflow import (  # noqa: E402
    SearchRequest,
    SearchWorkflow,
    merge_results,
    normalize_result,
)


def zlib_item(book_id="1", hash_id="hash", title="Python"):
    return {
        "id": book_id,
        "hash": hash_id,
        "title": title,
        "author": "A",
        "year": 2024,
        "can_download": True,
    }


def anna_item(md5="a" * 32, title="Python"):
    return {
        "md5": md5,
        "title": title,
        "author": "A",
        "year": 2024,
        "detail_url": "https://annas.example/book",
    }


def test_normalize_rejects_bad_ids_urls_and_deep_metadata():
    assert normalize_result({"id": "x", "hash": "h"}, "zlib") is None
    assert normalize_result({"md5": "b" * 32, "url": "http://private"}, "anna")["md5"] == "b" * 32
    deep = {"id": "1", "hash": "h", "nested": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}}
    assert normalize_result(deep, "zlib") is None


def test_merge_groups_duplicate_books_and_preserves_alternatives():
    items = [normalize_result(zlib_item(), "zlib"), normalize_result(anna_item(), "anna")]
    merged = merge_results([item for item in items if item], "Python", 10)
    assert len(merged) == 1
    assert merged[0]["source"] == "zlib"
    assert merged[0]["alternatives"][0]["source"] == "anna"


def test_workflow_is_deterministic_and_keeps_partial_source_results():
    def zlib(_request, **_kwargs):
        time.sleep(0.01)
        return [zlib_item("1", "z")], {"source": "zlib", "status": "ok"}

    def anna(_request, **_kwargs):
        return [anna_item("c" * 32, "Other")], {"source": "anna", "status": "ok"}

    result = SearchWorkflow({"zlib": zlib, "anna": anna}).find(SearchRequest("Python", limit=10))
    assert result.outcome == "ok"
    assert [item["source"] for item in result.results] == ["zlib", "anna"]
    assert {item["source"] for item in result.sources} == {"zlib", "anna"}


def test_workflow_total_deadline_discards_late_source():
    def slow(_request, **_kwargs):
        time.sleep(0.05)
        return [zlib_item()], None

    result = SearchWorkflow({"zlib": slow}).find(
        SearchRequest("Python"), budget=OperationBudget.from_seconds(0.005)
    )
    assert result.outcome == "timed_out"
    assert result.discarded_late == 1
    assert result.results == []


def test_workflow_shared_cancellation_reports_cancelled():
    token = CancellationToken()
    token.cancel("caller")
    result = SearchWorkflow({"zlib": lambda *_args, **_kwargs: [zlib_item()]}).find(
        SearchRequest("Python"), cancellation=token
    )
    assert result.outcome == "cancelled"
    assert result.sources[0]["outcome"] == "cancelled"
