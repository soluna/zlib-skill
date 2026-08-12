#!/usr/bin/env python3
"""
Anna's Archive 搜索模块
- 搜索 annas-archive.gl（无需登录）
- 返回书籍元数据和下载链接

License: MIT
Copyright (c) 2026 zlib-anna-skill contributors
"""

from __future__ import annotations

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
    env_flag,
    safe_get,
    validate_http_url,
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

BASE_URL = "https://annas-archive.gl"

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
MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$", re.I)
LIBGEN_HOSTS = {"libgen.li", "libgen.is", "libgen.rs"}


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
            resp = safe_get(session, url, timeout=timeout)
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

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("ANNAS_BASE_URL") or BASE_URL).rstrip("/")
        validate_http_url(
            self.base_url,
            require_https=not env_flag(
                ALLOW_INSECURE_HTTP_ENV,
                LEGACY_ALLOW_INSECURE_HTTP_ENV,
            ),
            resolve_dns=False,
        )
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def search(
        self,
        query: str,
        limit: int = 10,
        page: int = 1,
        ext_filter: str | list[str] | None = None,
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
        resp = _http_get_with_retry(self.session, url, timeout=30, label="search")
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

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

            # 格式过滤
            if ext_filters and ext.lower() not in ext_filters:
                results.pop()
                continue

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

        resp = _http_get_with_retry(self.session, url, timeout=30, label="download_links")
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

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
