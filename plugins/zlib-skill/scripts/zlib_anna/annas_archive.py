#!/usr/bin/env python3
"""
Anna's Archive 搜索模块
- 搜索 annas-archive.gl（无需登录）
- 返回书籍元数据和下载链接

License: MIT
Copyright (c) 2026 zlib-skill contributors
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import time
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .network_safety import (
    ALLOW_INSECURE_HTTP_ENV,
    LEGACY_ALLOW_INSECURE_HTTP_ENV,
    PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
    env_flag,
    safe_get,
    validate_http_url,
)
from .operation import (
    AttemptLog,
    CancellationToken,
    OperationBudget,
    OriginPool,
    PoolResult,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

OFFICIAL_BASE_URLS = (
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
)
BASE_URL = OFFICIAL_BASE_URLS[0]

# CSS 选择器降级链：从最精确到最宽泛
# Anna's Archive 改版时按优先级依次尝试，任一命中即停止
SELECTOR_CHAIN = [
    # 主选择器（当前有效）
    ('a.js-vim-focus[href^="/md5/"]', "primary: js-vim-focus"),
    # 降级 1：去掉 class 限制
    ('a[href^="/md5/"]', "fallback: any /md5/ link"),
    # 降级 2：更宽泛的搜索结果行匹配
    ('div[class*="search"] a[href^="/md5/"]', "fallback: search div > md5 link"),
]

RETRY_MAX = 3
RETRY_BACKOFF = 2  # seconds, exponential
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_ANNA_HTML_BYTES = DEFAULT_MAX_HTML_BYTES
MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$", re.I)
LIBGEN_HOSTS = {"libgen.li", "libgen.is", "libgen.rs"}
TRUSTED_PROXY_HOSTS = {
    *(urlparse(base_url).hostname for base_url in OFFICIAL_BASE_URLS),
    *LIBGEN_HOSTS,
}


class AnnaResponseError(ValueError):
    """An untrusted Anna response failed bounded validation."""

    code = "ANNA_RESPONSE_REJECTED"


class AnnaResponseTooLarge(AnnaResponseError):
    code = "ANNA_RESPONSE_TOO_LARGE"


def _response_header(response, name: str) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None:
            value = headers.get(name.title())
    except AttributeError:
        return None
    # ``requests`` headers are strings.  Treat permissive test doubles (for
    # example an unconfigured MagicMock) as an absent header rather than as a
    # malformed remote value.
    if isinstance(value, (str, bytes)):
        return value.decode() if isinstance(value, bytes) else value
    return None


def _bounded_response_bytes(response, *, max_bytes: int = DEFAULT_MAX_HTML_BYTES) -> bytes:
    """Read HTML with both declared and actual byte caps."""
    declared_value = _response_header(response, "content-length")
    if declared_value:
        try:
            declared = int(declared_value)
        except (TypeError, ValueError) as exc:
            raise AnnaResponseError("Anna response has invalid Content-Length") from exc
        if declared < 0:
            raise AnnaResponseError("Anna response has invalid Content-Length")
        if declared > max_bytes:
            raise AnnaResponseTooLarge("Anna response exceeds the size limit")

    media_type = (_response_header(response, "content-type") or "").split(";", 1)[0].strip().lower()
    if media_type and media_type not in {"text/html", "application/xhtml+xml"}:
        raise AnnaResponseError("Anna response has an unexpected content type")

    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        chunks: list[bytes] = []
        total = 0
        yielded = False
        try:
            for chunk in iterator(chunk_size=256 * 1024):
                if not chunk:
                    continue
                yielded = True
                item = bytes(chunk)
                total += len(item)
                if total > max_bytes:
                    raise AnnaResponseTooLarge("Anna response exceeds the size limit")
                chunks.append(item)
        except AnnaResponseError:
            raise
        except Exception as exc:
            raise AnnaResponseError("Anna response body is invalid") from exc
        if yielded:
            return b"".join(chunks)

    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray, memoryview)):
        body = bytes(content)
        if len(body) > max_bytes:
            raise AnnaResponseTooLarge("Anna response exceeds the size limit")
        return body

    text = getattr(response, "text", None)
    if isinstance(text, str):
        body = text.encode("utf-8")
        if len(body) > max_bytes:
            raise AnnaResponseTooLarge("Anna response exceeds the size limit")
        return body
    raise AnnaResponseError("Anna response body is unavailable")


def bounded_html_text(response, *, max_bytes: int = DEFAULT_MAX_HTML_BYTES) -> str:
    """Validate and decode one HTML response before parsing."""
    try:
        return _bounded_response_bytes(response, max_bytes=max_bytes).decode(
            "utf-8", errors="replace"
        )
    except UnicodeError as exc:
        raise AnnaResponseError("Anna response is not valid text") from exc


def _normalize_ext_filter(ext_filter) -> set[str]:
    if not ext_filter:
        return set()
    if isinstance(ext_filter, str):
        raw_items = ext_filter.split(",")
    else:
        raw_items = ext_filter
    return {str(item).strip().lower().lstrip(".") for item in raw_items if str(item).strip()}


def _http_get_with_retry(
    session: requests.Session,
    url: str,
    timeout: int = 30,
    label: str = "request",
    *,
    budget: OperationBudget | None = None,
    cancellation: CancellationToken | None = None,
) -> requests.Response:
    """
    HTTP GET with retry and exponential backoff.

    Handles: ConnectionError, Timeout, HTTPError (5xx only), generic RequestException.
    4xx errors are NOT retried (client error, not transient).

    Args:
        session: requests.Session
        url: Target URL
        timeout: Per-request timeout in seconds
        label: Human-readable label for log messages

    Returns:
        requests.Response on success

    Raises:
        requests.RequestException: after all retries exhausted
    """
    last_exc = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            request_timeout = budget.timeout(timeout) if budget is not None else timeout
            resp = safe_get(
                session,
                url,
                timeout=request_timeout,
                trusted_proxy_hosts=TRUSTED_PROXY_HOSTS,
            )
            # 4xx: client error, don't retry
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()
            # 5xx: server error, retry
            if resp.status_code >= 500:
                raise requests.HTTPError(
                    f"{resp.status_code} Server Error for {url}",
                    response=resp,
                )
            return resp
        except requests.ConnectionError as exc:
            last_exc = exc
            logger.warning(f"[{label}] ConnectionError (attempt {attempt}/{RETRY_MAX})")
        except requests.Timeout as exc:
            last_exc = exc
            logger.warning(f"[{label}] Timeout (attempt {attempt}/{RETRY_MAX})")
        except requests.HTTPError as exc:
            # Only retry 5xx
            if exc.response is not None and exc.response.status_code >= 500:
                last_exc = exc
                logger.warning(
                    f"[{label}] HTTP {exc.response.status_code} (attempt {attempt}/{RETRY_MAX})"
                )
            else:
                raise  # 4xx — don't retry
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(f"[{label}] RequestException (attempt {attempt}/{RETRY_MAX})")

        if attempt < RETRY_MAX:
            delay = RETRY_BACKOFF**attempt
            logger.info(f"[{label}] Retrying in {delay}s...")
            if budget is not None:
                budget.sleep(delay)
            elif cancellation is not None:
                if cancellation.wait(delay):
                    cancellation.raise_if_cancelled()
            else:
                time.sleep(delay)

    raise last_exc  # type: ignore


def _find_book_links(soup: BeautifulSoup) -> tuple[list, str]:
    """
    使用选择器降级链查找书籍链接。

    返回: (link_elements, selector_used_name)
    如果所有选择器都失败，返回空列表。
    """
    for selector, name in SELECTOR_CHAIN:
        links = soup.select(selector)
        if links:
            logger.info(f"CSS selector matched: {name} → {len(links)} results")
            return links, name
        logger.debug(f"CSS selector no match: {name}")

    logger.warning("All CSS selectors failed — Anna's Archive HTML may have changed significantly")
    return [], "none"


class AnnasArchiveClient:
    """Anna's Archive 客户端（含错误处理、重试、CSS 降级）"""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        requester: requests.Session | None = None,
        budget: OperationBudget | None = None,
        cancellation: CancellationToken | None = None,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    ):
        self.base_url = (base_url or os.environ.get("ANNAS_BASE_URL") or BASE_URL).rstrip("/")
        validate_http_url(
            self.base_url,
            require_https=not env_flag(
                ALLOW_INSECURE_HTTP_ENV,
                PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
                LEGACY_ALLOW_INSECURE_HTTP_ENV,
            ),
            resolve_dns=False,
        )
        self.session = requester or requests.Session()
        self.session.headers.update(HEADERS)
        self.budget = budget
        self.cancellation = cancellation or (budget.token if budget is not None else None)
        self.max_html_bytes = max(1, int(max_html_bytes))

    def search(
        self,
        query: str,
        limit: int = 10,
        page: int = 1,
        ext_filter: str | list[str] | None = None,
        language: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        *,
        lang: str | None = None,
        budget: OperationBudget | None = None,
        cancellation: CancellationToken | None = None,
    ) -> list[dict]:
        """
        搜索书籍（含 HTTP 重试 + CSS 选择器降级 + 分页）

        Args:
            query: 搜索关键词
            limit: 返回结果数量
            page: 页码（从 1 开始）
            ext_filter: 格式过滤（如 "pdf", "epub"），大小写不敏感

        Returns:
            List[Dict]: 搜索结果列表

        Raises:
            requests.RequestException: HTTP 错误（重试耗尽后）
            ValueError: 页面解析完全失败
        """
        url = f"{self.base_url}/search?q={quote(query, safe='')}"
        if page > 1:
            url += f"&page={page}"
        ext_filters = _normalize_ext_filter(ext_filter)

        # HTTP 请求（含重试）
        active_budget = budget or self.budget
        active_cancellation = cancellation or self.cancellation
        if active_budget is None and active_cancellation is None:
            # Keep the historical call shape for simple integrations and
            # existing deterministic tests that replace this helper.
            resp = _http_get_with_retry(self.session, url, timeout=30, label="search")
        else:
            resp = _http_get_with_retry(
                self.session,
                url,
                timeout=30,
                label="search",
                budget=active_budget,
                cancellation=active_cancellation,
            )
        resp.raise_for_status()

        html = bounded_html_text(resp, max_bytes=self.max_html_bytes)
        soup = BeautifulSoup(html, "html.parser")

        # CSS 选择器降级
        book_links, selector_used = _find_book_links(soup)

        if not book_links:
            # 所有选择器都失败了 — 做一次完整性检查
            page_text = soup.get_text(strip=True)[:500]
            if "search" not in page_text.lower() and "results" not in page_text.lower():
                raise ValueError("Anna's Archive page structure is unrecognized")
            # 页面看起来正常但没结果，可能就是搜不到
            logger.info(f"No results found for query: {query}")
            return []

        results = []
        language_filter = (lang or language or "").strip().lower()
        for link in book_links:
            href = link.get("href", "")
            if not href.startswith("/md5/"):
                continue

            md5 = href.removeprefix("/md5/").split("?", 1)[0].split("/", 1)[0]
            if not MD5_PATTERN.fullmatch(md5):
                logger.warning("Skipping malformed Anna result id")
                continue
            title = link.get_text(strip=True)

            # 找到父容器提取元数据（限制爬升深度，避免跨书籍数据污染）
            container = link
            for _ in range(2):
                parent = container.find_parent()
                if parent is None:
                    break
                container = parent

            full_text = container.get_text(separator="\n", strip=True) if container else ""
            lines = [line.strip() for line in full_text.split("\n") if line.strip()]

            # 解析元数据
            author = "Unknown"
            year = "Unknown"
            language = "Unknown"
            ext = "Unknown"
            size = "Unknown"
            sources = []

            for line in lines:
                if re.search(r"\w+\s+\[[a-z]{2}\]", line):
                    parts = line.split("·")
                    for part in parts:
                        part = part.strip()
                        if re.search(r"\w+\s+\[[a-z]{2}\]", part):
                            language = part
                        elif part.upper() in [
                            "PDF",
                            "EPUB",
                            "MOBI",
                            "AZW3",
                            "TXT",
                            "DJVU",
                            "CBR",
                            "CBZ",
                        ]:
                            ext = part.upper()
                        elif re.search(r"\d+\.?\d*\s*[MGK]B", part, re.I):
                            size = part
                        elif re.match(r"^(19|20)\d{2}$", part):
                            year = part
                        elif "🚀/" in part:
                            sources = [
                                s.strip() for s in part.replace("🚀/", "").split("/") if s.strip()
                            ]
                elif (
                    line != title
                    and len(line) < 60
                    and author == "Unknown"
                    and not line.startswith("http")
                ):
                    author = line

            # Apply every local filter before the result limit.  Anna's
            # server does not consistently honor these fields, so stopping
            # after the first N links would silently discard later matches.
            if ext_filters and ext.lower().lstrip(".") not in ext_filters:
                continue
            if language_filter:
                language_match = re.search(r"\[([a-z]{2,3})\]", language.lower())
                language_code = language_match.group(1) if language_match else language.lower()
                if language_filter not in {language_code, language.lower()}:
                    continue
            try:
                parsed_year = int(str(year))
            except (TypeError, ValueError):
                parsed_year = None
            if year_from is not None and (parsed_year is None or parsed_year < int(year_from)):
                continue
            if year_to is not None and (parsed_year is None or parsed_year > int(year_to)):
                continue

            results.append(
                {
                    "md5": md5,
                    "title": title,
                    "author": author,
                    "year": year,
                    "language": language,
                    "ext": ext,
                    "size": size,
                    "sources": sources,
                    "detail_url": f"{self.base_url}{href}",
                }
            )

            if len(results) >= limit:
                break

        return results

    def get_download_links(self, md5: str) -> dict:
        """
        获取书籍的下载链接（含 HTTP 重试）

        Args:
            md5: 书籍的 MD5

        Returns:
            Dict: 包含各种下载源的链接
        """
        if not MD5_PATTERN.fullmatch(md5):
            raise ValueError("Anna result id must contain a 32-character hexadecimal MD5")

        url = f"{self.base_url}/md5/{md5.lower()}"

        if self.budget is None and self.cancellation is None:
            resp = _http_get_with_retry(self.session, url, timeout=30, label="download_links")
        else:
            resp = _http_get_with_retry(
                self.session,
                url,
                timeout=30,
                label="download_links",
                budget=self.budget,
                cancellation=self.cancellation,
            )
        resp.raise_for_status()

        soup = BeautifulSoup(bounded_html_text(resp, max_bytes=self.max_html_bytes), "html.parser")

        links = {
            "libgen_li": None,
            "libgen_rs": None,
            "libgen_is": None,
            "fast_downloads": [],
            "detail_url": url,
        }

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            absolute = urljoin(url, href)
            parsed = urlparse(absolute)
            hostname = (parsed.hostname or "").lower()

            if hostname not in LIBGEN_HOSTS:
                if href.startswith("/fast_download/"):
                    links["fast_downloads"].append(absolute)
                continue

            if hostname == "libgen.li" and parsed.path in {"/ads.php", "/file.php"}:
                links["libgen_li"] = absolute
            elif hostname == "libgen.is" and parsed.path.startswith("/book"):
                links["libgen_is"] = absolute
            elif hostname == "libgen.rs" and parsed.path.startswith("/book"):
                links["libgen_rs"] = absolute
            elif href.startswith("/fast_download/"):
                links["fast_downloads"].append(absolute)

        return links


class AnnasArchivePool:
    """Anna-specific origin pool with stable official fallback order."""

    def __init__(
        self,
        origins: tuple[str, ...] | list[str] | None = None,
        *,
        client_factory=None,
        budget: OperationBudget | None = None,
        cancellation: CancellationToken | None = None,
        attempt_log: AttemptLog | None = None,
        cooldown_seconds: float = 0.0,
        max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
    ) -> None:
        origin_values = tuple(origins or OFFICIAL_BASE_URLS)
        require_https = not env_flag(
            ALLOW_INSECURE_HTTP_ENV,
            PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
            LEGACY_ALLOW_INSECURE_HTTP_ENV,
        )
        for origin in origin_values:
            validate_http_url(origin, require_https=require_https, resolve_dns=False)
        self.pool = OriginPool(
            origin_values,
            source="anna",
            budget=budget,
            cancellation=cancellation,
            attempt_log=attempt_log,
            cooldown_seconds=cooldown_seconds,
        )
        self.client_factory = client_factory or (
            lambda origin, **kwargs: AnnasArchiveClient(
                base_url=origin,
                budget=kwargs.get("budget"),
                cancellation=kwargs.get("cancellation"),
                max_html_bytes=max_html_bytes,
            )
        )

    @property
    def origins(self) -> tuple[str, ...]:
        return self.pool.origins

    @property
    def budget(self) -> OperationBudget:
        return self.pool.budget

    @property
    def cancellation(self) -> CancellationToken:
        return self.pool.cancellation

    @property
    def attempt_log(self) -> AttemptLog:
        return self.pool.attempt_log

    def call(
        self, operation: str, method: str, *args, timeout: float | None = None, **kwargs
    ) -> PoolResult:
        def invoke(origin: str, request_timeout: float | None, token: CancellationToken):
            token.raise_if_cancelled()
            try:
                client = self.client_factory(
                    origin,
                    budget=self.budget,
                    cancellation=token,
                    timeout=request_timeout,
                )
            except TypeError:
                client = self.client_factory(origin)
            fn = getattr(client, method)
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

        return self.pool.execute(operation, invoke, timeout=timeout)

    def search(self, query: str, **kwargs) -> PoolResult:
        return self.call("search", "search", query, **kwargs)

    def resolve(self, md5: str, **kwargs) -> PoolResult:
        return self.call("resolve", "get_download_links", md5, **kwargs)

    def download(self, md5: str, **kwargs) -> PoolResult:
        return self.resolve(md5, **kwargs)

    def doctor(self, **kwargs) -> PoolResult:
        return self.call("doctor", "search", "", limit=0, **kwargs)

    def info(self, md5: str, **kwargs) -> PoolResult:
        return self.resolve(md5, **kwargs)


AnnaOriginPool = AnnasArchivePool
AnnasOriginPool = AnnasArchivePool


def search_books(
    query: str,
    limit: int = 10,
    page: int = 1,
    ext_filter: str | list[str] | None = None,
) -> list[dict]:
    """
    便捷函数：搜索书籍

    Args:
        query: 搜索关键词
        limit: 返回结果数量
        page: 页码
        ext_filter: 格式过滤（如 "pdf"）

    Returns:
        List[Dict]: 搜索结果列表
    """
    client = AnnasArchiveClient()
    return client.search(query, limit=limit, page=page, ext_filter=ext_filter)


__all__ = [
    "ALLOW_INSECURE_HTTP_ENV",
    "AnnaOriginPool",
    "AnnasArchiveClient",
    "AnnasArchivePool",
    "AnnasOriginPool",
    "BASE_URL",
    "DEFAULT_MAX_HTML_BYTES",
    "MAX_ANNA_HTML_BYTES",
    "OFFICIAL_BASE_URLS",
    "SELECTOR_CHAIN",
    "AnnaResponseError",
    "AnnaResponseTooLarge",
    "bounded_html_text",
    "search_books",
]
