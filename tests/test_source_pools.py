"""Deterministic contract tests for source-local pools and operation budgets."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zlib_anna.annas_archive import (  # noqa: E402
    AnnaResponseError,
    AnnaResponseTooLarge,
    AnnasArchiveClient,
    AnnasArchivePool,
)
from zlib_anna.network_safety import UnsafeUrlError  # noqa: E402
from zlib_anna.operation import (  # noqa: E402
    CancellationToken,
    OperationBudget,
    OriginPool,
)
from zlib_anna.zlib_source import (  # noqa: E402
    ZlibResponseError,
    ZlibResponseTooLarge,
    ZlibSourcePool,
    parse_zlib_json,
)


class Response:
    def __init__(self, body, *, content_type="application/json", content_length=None):
        self.content = body if isinstance(body, bytes) else str(body).encode()
        self.status_code = 200
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)

    def raise_for_status(self):
        return None


def test_origin_pool_switches_in_stable_order_and_records_attempts():
    calls = []

    def call(origin, timeout, token):
        calls.append((origin, timeout, token))
        if origin == "https://first":
            raise OSError("stand-in failure")
        return {"origin": origin}

    pool = OriginPool(
        ["https://first", "https://second"],
        source="stand-in",
        budget=OperationBudget.from_seconds(2),
    )
    result = pool.execute("search", call)

    assert result.outcome == "ok"
    assert result.origin == "https://second"
    assert [item[0] for item in calls] == ["https://first", "https://second"]
    assert [item["outcome"] for item in result.attempts] == ["error", "ok"]
    assert result.attempts[0]["detail"] == "request failed"


def test_origin_pool_cooldown_skips_failed_origin_on_next_operation():
    calls = []
    pool = OriginPool(["a", "b"], cooldown_seconds=30)

    def first(origin, _timeout, _token):
        calls.append(origin)
        if origin == "a":
            raise OSError("failure")
        return origin

    assert pool.execute("search", first).value == "b"
    assert pool.execute("search", first).value == "b"
    assert calls == ["a", "b", "b"]


def test_origin_pool_deadline_bounds_timeout_and_reports_timeout():
    observed = []
    budget = OperationBudget.from_seconds(0.01)
    pool = OriginPool(["a"], budget=budget)

    def slow(_origin, timeout, _token):
        observed.append(timeout)
        time.sleep(0.02)
        return "late"

    result = pool.execute("search", slow)
    assert result.outcome == "timed_out"
    assert observed and observed[0] <= 0.01


def test_origin_pool_shared_cancellation_is_observable():
    token = CancellationToken()
    pool = OriginPool(["a", "b"], cancellation=token)

    def cancelled(_origin, _timeout, received):
        assert received is token
        received.cancel("caller requested stop")
        received.raise_if_cancelled()

    result = pool.execute("search", cancelled)
    assert result.outcome == "cancelled"
    assert result.status == "cancelled"
    assert result.attempts[0]["error_type"] == "OperationCancelled"


def test_origin_pool_classifies_late_failure_as_timeout():
    pool = OriginPool(["https://example.com"], budget=OperationBudget.from_seconds(0.001))

    def late_failure(_origin, _timeout, _token):
        time.sleep(0.01)
        raise OSError("late")

    assert pool.execute("search", late_failure).outcome == "timed_out"


def test_zlib_json_parser_enforces_type_declared_and_actual_caps():
    assert parse_zlib_json(Response(b'{"success": true}')) == {"success": True}
    with pytest.raises(ZlibResponseError):
        parse_zlib_json(Response(b"[]"))
    with pytest.raises(ZlibResponseError):
        parse_zlib_json(Response(b"not-json"))
    with pytest.raises(ZlibResponseError):
        parse_zlib_json(Response(b"{}", content_type="text/html"))
    with pytest.raises(ZlibResponseTooLarge):
        parse_zlib_json(Response(b"{}", content_length=99), max_bytes=2)
    with pytest.raises(ZlibResponseTooLarge):
        parse_zlib_json(Response(b"123456"), max_bytes=2)


def test_zlib_source_pool_requester_is_one_attempt_per_origin():
    class Requester:
        def __init__(self):
            self.calls = []

        def get(self, url, **_kwargs):
            self.calls.append(url)
            if url.startswith("https://first"):
                raise OSError("first unavailable")
            return Response(b'{"success": true}')

    requester = Requester()
    pool = ZlibSourcePool(["https://first", "https://second"], requester=requester)
    result = pool.request_json("domains", "/eapi/info/domains")
    assert result.outcome == "ok"
    assert requester.calls == [
        "https://first/eapi/info/domains",
        "https://second/eapi/info/domains",
    ]
    assert result.value == {"success": True}


def test_zlib_source_pool_rejects_http_error_and_absolute_private_url():
    class Requester:
        def get(self, url, **_kwargs):
            if "127.0.0.1" in url:
                return Response(b'{"success": true}')
            response = Response(b"{}")
            response.status_code = 500
            response.raise_for_status = lambda: (_ for _ in ()).throw(RuntimeError("http"))
            return response

    pool = ZlibSourcePool(["https://example.com"], requester=Requester())
    assert pool.request_json("error", "/error").outcome == "unavailable"
    assert pool.request_json("private", "http://127.0.0.1/private").outcome == "unavailable"


def test_anna_html_parser_rejects_untrusted_content_before_soup(monkeypatch):
    monkeypatch.setenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", "1")

    class Requester:
        def __init__(self, response):
            self.response = response
            self.headers = {}

        def get(self, _url, **_kwargs):
            return self.response

    too_large = Response(b"<html>ok</html>", content_type="text/html", content_length=100)
    with pytest.raises(AnnaResponseTooLarge):
        AnnasArchiveClient(
            base_url="https://annas.example",
            requester=Requester(too_large),
            max_html_bytes=10,
        ).search("book")

    wrong_type = Response(b"{}", content_type="application/json")
    with pytest.raises(AnnaResponseError):
        AnnasArchiveClient(
            base_url="https://annas.example",
            requester=Requester(wrong_type),
        ).search("book")


def test_anna_filter_is_applied_before_limit(monkeypatch):
    monkeypatch.setenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", "1")
    html = """<html><body><div class='search-results'>
      <div class='result-item'><div class='result-card'><a class='js-vim-focus'
        href='/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'>Wrong</a>
        <span>Author · English [en] · EPUB · 1MB · 2010</span></div></div>
      <div class='result-item'><div class='result-card'><a class='js-vim-focus'
        href='/md5/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'>Match One</a>
        <span>Author · English [en] · EPUB · 1MB · 2022</span></div></div>
      <div class='result-item'><div class='result-card'><a class='js-vim-focus'
        href='/md5/cccccccccccccccccccccccccccccccc'>Match Two</a>
        <span>Author · English [en] · EPUB · 1MB · 2023</span></div></div>
    </div></body></html>"""

    class Requester:
        def __init__(self):
            self.headers = {}

        def get(self, _url, **_kwargs):
            return Response(html.encode(), content_type="text/html")

    client = AnnasArchiveClient(base_url="https://annas.example", requester=Requester())
    results = client.search("book", limit=1, ext_filter="epub", year_from=2020, language="en")
    assert [item["title"] for item in results] == ["Match One"]


def test_anna_pool_fallback_and_attempt_log():
    class StandIn:
        def __init__(self, origin):
            self.origin = origin

        def search(self, _query, **_kwargs):
            if self.origin.endswith("first"):
                raise OSError("first unavailable")
            return [{"md5": "a" * 32, "title": "ok"}]

    pool = AnnasArchivePool(
        ["https://first", "https://second"],
        client_factory=lambda origin, **_kwargs: StandIn(origin),
    )
    result = pool.search("book", limit=1)
    assert result.origin == "https://second"
    assert [item["outcome"] for item in result.attempts] == ["error", "ok"]


def test_source_pools_validate_custom_origins_before_injected_clients():
    with pytest.raises(UnsafeUrlError):
        AnnasArchivePool(["http://127.0.0.1"], client_factory=lambda origin: origin)
    with pytest.raises(UnsafeUrlError):
        ZlibSourcePool(["http://127.0.0.1"], client_factory=lambda origin: origin)
