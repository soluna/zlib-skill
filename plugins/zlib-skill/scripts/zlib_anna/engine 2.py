#!/usr/bin/env python3
"""Deterministic execution engine bundled with zlib-anna-skill."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import SCHEMA_VERSION, SKILL_VERSION, annas_archive
from .network_safety import (
    ALLOW_INSECURE_HTTP_ENV,
    LEGACY_ALLOW_INSECURE_HTTP_ENV,
    UnsafeUrlError,
    env_flag,
    safe_get,
    url_origin,
    validate_http_url,
)

ANNAS_AVAILABLE = True


def default_config_dir() -> Path:
    override = os.environ.get("ZLIB_ANNA_CONFIG_DIR") or os.environ.get("ZLIB_CLI_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "zlib_cli"
    return Path.home() / ".config" / "zlib_cli"


CONFIG_DIR = default_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Books"
ENTRY_POINTS = [
    "https://1lib.sk/eapi/info/domains",
    "https://singlelogin.rs/eapi/info/domains",
]

FALLBACK_ZLIB_DOMAINS = ["z-library.sk", "1lib.sk", "article.sk", "articles.sk"]
ZLIB_DOMAIN_ENV_KEYS = ("ZLIBRARY_DOMAIN", "ZLIB_DOMAIN")
ALLOW_UNTRUSTED_ZLIB_DOMAIN_ENV = "ZLIBRARY_ALLOW_UNTRUSTED_DOMAIN"
RUNNER_COMMAND = "python3 {baseDir}/scripts/run.py"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

DOWNLOAD_RETRY_MAX = 3
DOWNLOAD_RETRY_BACKOFF = 2
DIRECT_DOWNLOAD_TIMEOUT = (10, 300)
DEFAULT_MAX_DOWNLOAD_SIZE_MB = 2048
BYTES_PER_MIB = 1024 * 1024
ANNA_MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$", re.I)
ZLIB_BOOK_ID_PATTERN = re.compile(r"^[0-9]+$")
ZLIB_HASH_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_FILENAME_BYTES = 240
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
EBOOK_EXTENSIONS = {
    ".azw",
    ".azw3",
    ".cb7",
    ".cbr",
    ".cbz",
    ".djvu",
    ".epub",
    ".fb2",
    ".mobi",
    ".pdf",
    ".rtf",
    ".txt",
}
EBOOK_MEDIA_TYPE_EXTENSIONS = {
    "application/epub+zip": ".epub",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.amazon.ebook": ".azw",
    "application/x-djvu": ".djvu",
    "application/x-fictionbook+xml": ".fb2",
    "application/x-mobipocket-ebook": ".mobi",
    "image/vnd.djvu": ".djvu",
    "text/plain": ".txt",
}
HTML_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
}


def anna_base_url() -> str | None:
    if not ANNAS_AVAILABLE:
        return None
    return os.environ.get("ANNAS_BASE_URL") or getattr(annas_archive, "BASE_URL", None)


def anna_base_origin() -> str | None:
    value = anna_base_url()
    return url_origin(value) if value else None


class SkillError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = True,
        suggestions: list[str] | None = None,
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.suggestions = suggestions or []
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }
        if self.suggestions:
            payload["suggestions"] = self.suggestions
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass
class SourceStatus:
    source: str
    available: bool
    authenticated: bool = False
    can_search: bool = False
    can_download: bool = False
    can_attempt_download: bool = False
    status: str = "unknown"
    message: str = ""
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source": self.source,
            "available": self.available,
            "authenticated": self.authenticated,
            "can_search": self.can_search,
            "can_download": self.can_download,
            "can_attempt_download": self.can_attempt_download,
            "status": self.status,
        }
        if self.message:
            payload["message"] = self.message
        if self.details:
            payload["details"] = self.details
        return payload


def print_human(args: argparse.Namespace, message: str = "") -> None:
    stream = sys.stderr if getattr(args, "json", False) else sys.stdout
    print(message, file=stream)


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def fail(code: str, message: str, **kwargs: Any) -> None:
    raise SkillError(code, message, **kwargs)


def ok_payload(**kwargs: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
        **kwargs,
    }


def error_payload(error: SkillError) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "skill_version": SKILL_VERSION,
        "error": error.to_dict(),
    }


def display_path(path: Path) -> str:
    try:
        relative = path.expanduser().relative_to(Path.home())
    except ValueError:
        return str(path)
    return str(Path("~") / relative)


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if not name:
        masked = "*"
    elif len(name) <= 2:
        masked = name[0] + "*"
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _mode(path: Path) -> str | None:
    if not path.exists():
        return None
    return oct(stat.S_IMODE(path.stat().st_mode))


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CONFIG_DIR, 0o700)


def repair_config_permissions() -> list[dict[str, str]]:
    repairs: list[dict[str, str]] = []
    targets = [
        (CONFIG_DIR, 0o700, "config_dir"),
        (CONFIG_FILE, 0o600, "config_file"),
    ]
    for path, desired_mode, kind in targets:
        if not path.exists():
            continue
        try:
            current_mode = stat.S_IMODE(path.stat().st_mode)
            if current_mode == desired_mode:
                continue
            os.chmod(path, desired_mode)
            repairs.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "from": oct(current_mode),
                    "to": oct(desired_mode),
                }
            )
        except OSError as exc:
            repairs.append({"kind": kind, "path": str(path), "error": str(exc)})
    return repairs


def load_config(strict: bool = True, repair_permissions: bool = True) -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    if repair_permissions:
        repair_config_permissions()
    try:
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        if not strict:
            return {}
        fail(
            "CONFIG_INVALID",
            f"Config file is not valid JSON: {CONFIG_FILE}",
            details={"error": str(exc), "path": str(CONFIG_FILE)},
        )
    if not isinstance(payload, dict):
        if not strict:
            return {}
        fail(
            "CONFIG_INVALID",
            f"Config file must contain a JSON object: {CONFIG_FILE}",
            details={"path": str(CONFIG_FILE)},
        )
    return payload


def save_config(cfg: dict[str, Any]) -> None:
    ensure_config_dir()
    tmp_file = CONFIG_FILE.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp_file, 0o600)
    os.replace(tmp_file, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)


def remove_zlib_auth(cfg: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(cfg)
    for key in ("remix_userid", "remix_userkey", "email", "name"):
        cleaned.pop(key, None)
    return cleaned


def has_zlib_auth(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("remix_userid") and cfg.get("remix_userkey"))


def config_status() -> dict[str, Any]:
    permission_repairs = repair_config_permissions()
    cfg = load_config(strict=False, repair_permissions=False)
    zlib_domain_env = next(
        (os.environ[key] for key in ZLIB_DOMAIN_ENV_KEYS if os.environ.get(key)),
        None,
    )
    payload = {
        "config_dir": display_path(CONFIG_DIR),
        "config_file": display_path(CONFIG_FILE),
        "config_dir_exists": CONFIG_DIR.exists(),
        "config_file_exists": CONFIG_FILE.exists(),
        "config_dir_mode": _mode(CONFIG_DIR),
        "config_file_mode": _mode(CONFIG_FILE),
        "zlib": {
            "has_token": has_zlib_auth(cfg),
            "email": mask_email(cfg.get("email")),
            "domain": cfg.get("domain"),
            "domain_trusted": bool(cfg.get("domain_trusted")),
            "domain_env": normalize_domain(zlib_domain_env) if zlib_domain_env else None,
        },
        "anna": {
            "base_origin": anna_base_origin(),
        },
    }
    if permission_repairs:
        payload["permission_repairs"] = permission_repairs
    return payload


def safe_json_response(resp: requests.Response) -> dict[str, Any] | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def fetch_domains() -> list[str]:
    for url in ENTRY_POINTS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=False)
            data = safe_json_response(resp)
            if resp.status_code == 200 and data and data.get("success"):
                domains = []
                for item in data.get("domains", []):
                    domain = normalize_domain(item.get("domain"))
                    if domain and item.get("contentAvailable") and not item.get("isRedirector"):
                        domains.append(domain)
                return domains
        except requests.RequestException:
            continue
    return []


def normalize_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    value = domain.strip()
    parsed = urlparse(value if "://" in value else f"//{value}")
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        if parsed.port is not None:
            return None
    except ValueError:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    try:
        return hostname.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError:
        return None


def env_zlib_domain() -> str | None:
    for key in ZLIB_DOMAIN_ENV_KEYS:
        domain = normalize_domain(os.environ.get(key))
        if domain:
            return domain
    return None


def test_domain(domain: str) -> bool:
    domain = normalize_domain(domain) or domain
    try:
        validate_http_url(f"https://{domain}", require_https=True)
        resp = requests.get(
            f"https://{domain}/eapi/info/domains",
            headers=HEADERS,
            timeout=10,
            allow_redirects=False,
        )
        data = safe_json_response(resp)
        return resp.status_code == 200 and bool(data and data.get("success"))
    except (requests.RequestException, UnsafeUrlError):
        return False


def find_working_domain(
    preferred: str | None = None,
    *,
    preferred_trusted: bool = False,
) -> tuple[str | None, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    env_domain = env_zlib_domain()
    preferred = normalize_domain(preferred)
    fallback_domains = {
        domain for item in FALLBACK_ZLIB_DOMAINS if (domain := normalize_domain(item)) is not None
    }
    allow_untrusted = env_flag(ALLOW_UNTRUSTED_ZLIB_DOMAIN_ENV)

    env_trust_basis = None
    if env_domain in fallback_domains:
        env_trust_basis = "fallback"
    elif allow_untrusted:
        env_trust_basis = "explicit_opt_in"
    preferred_trust_basis = None
    if preferred_trusted:
        preferred_trust_basis = "cached"
    elif preferred in fallback_domains:
        preferred_trust_basis = "fallback"
    early_candidates = [
        (env_domain, "env", env_trust_basis),
        (preferred, "config", preferred_trust_basis),
    ]
    deferred: list[tuple[str, str]] = []
    seen: set[str] = set()
    for domain, source, trust_basis in early_candidates:
        if not domain or domain in seen:
            continue
        seen.add(domain)
        if not trust_basis:
            deferred.append((domain, source))
            continue
        ok = test_domain(domain)
        checks.append(
            {
                "domain": domain,
                "available": ok,
                "source": source,
                "trusted": True,
                "trust_basis": trust_basis,
            }
        )
        if ok:
            return domain, checks

    discovered_domains = fetch_domains()
    discovered_set = set(discovered_domains)
    for domain, source in deferred:
        if domain not in discovered_set:
            checks.append(
                {
                    "domain": domain,
                    "available": False,
                    "source": source,
                    "trusted": False,
                    "reason": "untrusted_domain",
                }
            )
            continue
        ok = test_domain(domain)
        checks.append(
            {
                "domain": domain,
                "available": ok,
                "source": source,
                "trusted": True,
                "trust_basis": "discovered",
            }
        )
        if ok:
            return domain, checks

    for source, domains in (
        ("discovered", discovered_domains),
        ("fallback", FALLBACK_ZLIB_DOMAINS),
    ):
        for item in domains:
            domain = normalize_domain(item)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            ok = test_domain(domain)
            checks.append(
                {
                    "domain": domain,
                    "available": ok,
                    "source": source,
                    "trusted": True,
                    "trust_basis": source,
                }
            )
            if ok:
                return domain, checks
    return None, checks


def domain_source(domain: str, checks: list[dict[str, Any]]) -> str | None:
    for check in reversed(checks):
        if check.get("domain") == domain and check.get("available"):
            return str(check.get("source"))
    return None


def domain_trust_is_persistent(domain: str, checks: list[dict[str, Any]]) -> bool:
    for check in reversed(checks):
        if check.get("domain") == domain and check.get("available"):
            return check.get("trust_basis") != "explicit_opt_in"
    return False


def init_zlibrary(
    cfg: dict[str, Any],
    *,
    require_auth: bool = True,
    update_config: bool = True,
    resolved_domain: str | None = None,
):
    script_dir = Path(__file__).parent.resolve()
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    from .zlibrary import Zlibrary

    if require_auth and not has_zlib_auth(cfg):
        fail(
            "AUTH_REQUIRED",
            "Z-Library credentials are not configured.",
            suggestions=[
                "Run: python3 {baseDir}/scripts/run.py auth login zlib --email <you@example.com>",
                "Or use: python3 {baseDir}/scripts/run.py search <query> --source anna --json",
            ],
        )

    if resolved_domain:
        working = normalize_domain(resolved_domain)
        checks: list[dict[str, Any]] = []
    else:
        working, checks = find_working_domain(
            cfg.get("domain"),
            preferred_trusted=bool(cfg.get("domain_trusted")),
        )
    if not working:
        fail(
            "SOURCE_UNAVAILABLE",
            "No reachable Z-Library mirror was found.",
            suggestions=[
                "Run: python3 {baseDir}/scripts/run.py doctor --json",
                "Configure a proxy with HTTPS_PROXY or ALL_PROXY if your network blocks access.",
                "Try Anna's Archive with: --source anna",
                "Only trust a manually supplied domain after verifying it independently.",
            ],
            details={"domain_checks": checks},
        )

    if update_config and (working != cfg.get("domain") or not cfg.get("domain_trusted")):
        cfg["domain"] = working
        cfg["domain_source"] = domain_source(working, checks)
        cfg["domain_trusted"] = domain_trust_is_persistent(working, checks)
        save_config(cfg)

    z = Zlibrary(
        remix_userid=cfg.get("remix_userid"),
        remix_userkey=cfg.get("remix_userkey"),
    )
    z.setDomain(working)
    if require_auth and not z.isLoggedIn():
        fail(
            "AUTH_INVALID",
            "Saved Z-Library token is invalid or expired.",
            suggestions=[
                "Run: python3 {baseDir}/scripts/run.py auth login zlib --email <you@example.com>",
                "Run: python3 {baseDir}/scripts/run.py auth logout to clear the saved token.",
            ],
            details={"domain": working},
        )
    return z


def build_search_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "message": args.query,
        "page": args.page,
        "limit": args.limit,
    }
    if args.year_from:
        kwargs["yearFrom"] = args.year_from
    if args.year_to:
        kwargs["yearTo"] = args.year_to
    if args.lang:
        kwargs["languages"] = args.lang
    if args.ext:
        kwargs["extensions"] = parse_csv(args.ext)
    if args.order:
        kwargs["order"] = args.order
    return kwargs


def normalize_zlib_book(book: dict[str, Any]) -> dict[str, Any]:
    book_id = str(book.get("id", ""))
    hash_id = str(book.get("hash", ""))
    valid_ref = bool(
        ZLIB_BOOK_ID_PATTERN.fullmatch(book_id) and ZLIB_HASH_PATTERN.fullmatch(hash_id)
    )
    return {
        "result_id": f"zlib:{book_id}:{hash_id}",
        "source": "zlib",
        "id": book_id,
        "hash": hash_id,
        "title": book.get("title") or book.get("name") or "",
        "author": book.get("author") or "",
        "year": book.get("year"),
        "language": book.get("language"),
        "extension": book.get("extension"),
        "size": book.get("filesizeString") or book.get("filesize"),
        "can_download": valid_ref,
        "requires_account": True,
    }


def normalize_anna_book(book: dict[str, Any]) -> dict[str, Any]:
    md5 = str(book.get("md5", "")).lower()
    valid_ref = bool(ANNA_MD5_PATTERN.fullmatch(md5))
    return {
        "result_id": f"anna:{md5}",
        "source": "anna",
        "md5": md5,
        "title": book.get("title") or "",
        "author": book.get("author") or "",
        "year": book.get("year"),
        "language": book.get("language"),
        "extension": book.get("ext"),
        "size": book.get("size"),
        "sources": book.get("sources", []),
        "detail_url": book.get("detail_url"),
        "can_download": False,
        "can_attempt_download": valid_ref,
        "download_guaranteed": False,
        "download_strategy": "html_best_effort",
        "requires_account": False,
        "best_effort": True,
    }


def search_zlib(
    args: argparse.Namespace,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], SourceStatus]:
    if not has_zlib_auth(cfg):
        status = SourceStatus(
            source="zlib",
            available=False,
            authenticated=False,
            can_search=False,
            can_download=False,
            can_attempt_download=False,
            status="auth_required",
            message="Z-Library token is not configured.",
        )
        if args.source == "zlib":
            fail(
                "AUTH_REQUIRED",
                "Z-Library search requires login.",
                suggestions=[f"Run: {RUNNER_COMMAND} auth login zlib --email <you@example.com>"],
            )
        return [], status

    z = init_zlibrary(cfg, require_auth=True)
    result = z.search(**build_search_kwargs(args))
    if not result or not result.get("success"):
        fail(
            "SEARCH_FAILED",
            "Z-Library search failed.",
            details={"response_received": bool(result)},
        )

    books = [normalize_zlib_book(book) for book in result.get("books", [])]
    return books, SourceStatus(
        source="zlib",
        available=True,
        authenticated=True,
        can_search=True,
        can_download=True,
        can_attempt_download=True,
        status="ok",
        details={"domain": z.getDomain()},
    )


def search_anna(args: argparse.Namespace) -> tuple[list[dict[str, Any]], SourceStatus]:
    if not ANNAS_AVAILABLE:
        status = SourceStatus(
            source="anna",
            available=False,
            authenticated=False,
            can_search=False,
            can_download=False,
            can_attempt_download=False,
            status="module_missing",
            message="annas_archive.py could not be imported.",
        )
        if args.source == "anna":
            fail("MODULE_MISSING", "Anna's Archive module is unavailable.")
        return [], status

    try:
        client = annas_archive.AnnasArchiveClient()
        results = client.search(
            args.query,
            limit=args.limit,
            page=args.page,
            ext_filter=parse_csv(args.ext),
        )
    except Exception as exc:
        status = SourceStatus(
            source="anna",
            available=False,
            authenticated=False,
            can_search=False,
            can_download=False,
            can_attempt_download=False,
            status="unavailable",
            message="Anna's Archive request failed.",
            details={"base_origin": anna_base_origin(), "error_type": type(exc).__name__},
        )
        if args.source == "anna":
            fail(
                "SOURCE_UNAVAILABLE",
                "Anna's Archive is unavailable.",
                suggestions=[
                    "Set ANNAS_BASE_URL to a reachable mirror.",
                    "Configure HTTPS_PROXY or ALL_PROXY if your network blocks access.",
                    "Run: python3 {baseDir}/scripts/run.py doctor --json",
                ],
                details=status.details,
            )
        return [], status

    books = [
        normalize_anna_book(book)
        for book in results
        if ANNA_MD5_PATTERN.fullmatch(str(book.get("md5", "")))
    ]
    language = (getattr(args, "lang", None) or "").strip().lower()
    year_from = getattr(args, "year_from", None)
    year_to = getattr(args, "year_to", None)
    filtered_books = []
    for book in books:
        book_language = str(book.get("language") or "").lower()
        language_code_match = re.search(r"\[([a-z]{2,3})\]", book_language)
        language_code = language_code_match.group(1) if language_code_match else book_language
        if language and language not in {language_code, book_language}:
            continue
        try:
            year = int(str(book.get("year")))
        except (TypeError, ValueError):
            year = None
        if year_from is not None and (year is None or year < year_from):
            continue
        if year_to is not None and (year is None or year > year_to):
            continue
        filtered_books.append(book)

    details: dict[str, Any] = {
        "base_origin": anna_base_origin(),
        "mode": "html_best_effort",
    }
    if getattr(args, "order", None):
        details["ignored_filters"] = ["order"]

    return filtered_books, SourceStatus(
        source="anna",
        available=True,
        authenticated=False,
        can_search=True,
        can_download=False,
        can_attempt_download=True,
        status="ok",
        details=details,
    )


def print_search_table(args: argparse.Namespace, results: list[dict[str, Any]]) -> None:
    if not results:
        print_human(args, "No results found.")
        return

    print_human(args, f"Found {len(results)} result(s):")
    print_human(args, f"{'Result ID':<44} {'Fmt':<6} {'Year':<6} {'Size':<10} Title / Author")
    print_human(args, "-" * 110)
    for item in results:
        result_id = item.get("result_id", "")
        ext = str(item.get("extension") or "")[:6]
        year = str(item.get("year") or "")[:6]
        size = str(item.get("size") or "")[:10]
        title = str(item.get("title") or "")[:42]
        author = str(item.get("author") or "")[:24]
        print_human(args, f"{result_id:<44} {ext:<6} {year:<6} {size:<10} {title} / {author}")


def cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    if not args.query.strip():
        fail("QUERY_REQUIRED", "Search query must not be empty.")
    if len(args.query) > 500:
        fail("INVALID_QUERY", "Search query must be 500 characters or fewer.")
    if args.year_from and args.year_to and args.year_from > args.year_to:
        fail("INVALID_FILTER", "--year-from must be less than or equal to --year-to.")

    cfg = load_config()
    source = args.source.lower()
    results: list[dict[str, Any]] = []
    statuses: list[SourceStatus] = []

    if source in ("zlib", "all"):
        try:
            zlib_results, zlib_status = search_zlib(args, cfg)
        except SkillError as exc:
            if source == "zlib":
                raise
            zlib_results = []
            zlib_status = SourceStatus(
                source="zlib",
                available=False,
                authenticated=has_zlib_auth(cfg),
                status="error",
                message=exc.message,
                details={"error": exc.to_dict()},
            )
        except Exception as exc:
            if source == "zlib":
                fail(
                    "SEARCH_FAILED",
                    "Z-Library search failed unexpectedly.",
                    details={"error_type": type(exc).__name__},
                )
            zlib_results = []
            zlib_status = SourceStatus(
                source="zlib",
                available=False,
                authenticated=has_zlib_auth(cfg),
                status="error",
                message="Z-Library search failed unexpectedly.",
                details={"error_type": type(exc).__name__},
            )
        results.extend(zlib_results)
        statuses.append(zlib_status)

    if source in ("anna", "all"):
        try:
            anna_results, anna_status = search_anna(args)
        except SkillError:
            if source == "anna":
                raise
            anna_results = []
            anna_status = SourceStatus(
                source="anna",
                available=False,
                status="error",
                message="Anna's Archive search failed.",
            )
        results.extend(anna_results)
        statuses.append(anna_status)

    if not results and not any(status.available for status in statuses):
        fail(
            "NO_SOURCES_AVAILABLE",
            "No requested source is available.",
            suggestions=[
                f"Run: {RUNNER_COMMAND} doctor --json",
                f"Login to Z-Library with: {RUNNER_COMMAND} auth login zlib "
                "--email <you@example.com>",
                "Set ANNAS_BASE_URL if Anna's Archive is blocked.",
            ],
            details={"sources": [status.to_dict() for status in statuses]},
        )

    payload = ok_payload(
        query=args.query,
        count=len(results),
        results=results,
        sources=[status.to_dict() for status in statuses],
    )
    if args.json:
        emit_json(payload)
    else:
        for status in statuses:
            if status.status != "ok":
                print_human(args, f"[{status.source}] {status.status}: {status.message}")
        print_search_table(args, results)
    return payload


def parse_result_ref(
    value: str,
    source: str = "auto",
    hash_id: str | None = None,
) -> tuple[str, str, str | None]:
    if value.startswith("zlib:"):
        parts = value.split(":", 2)
        if (
            len(parts) != 3
            or not ZLIB_BOOK_ID_PATTERN.fullmatch(parts[1])
            or not ZLIB_HASH_PATTERN.fullmatch(parts[2])
        ):
            fail("INVALID_RESULT_ID", "Z-Library result id must be zlib:<book_id>:<hash>.")
        return "zlib", parts[1], parts[2]
    if value.startswith("anna:"):
        md5 = value.split(":", 1)[1].lower()
        if not ANNA_MD5_PATTERN.fullmatch(md5):
            fail("INVALID_RESULT_ID", "Anna result id must be anna:<32-character MD5>.")
        return "anna", md5, None
    if source == "zlib" or hash_id:
        if (
            not ZLIB_BOOK_ID_PATTERN.fullmatch(value)
            or not hash_id
            or not ZLIB_HASH_PATTERN.fullmatch(hash_id)
        ):
            fail("INVALID_RESULT_ID", "Z-Library id/hash contains invalid characters.")
        return "zlib", value, hash_id
    if source == "anna":
        md5 = value.lower()
        if not ANNA_MD5_PATTERN.fullmatch(md5):
            fail("INVALID_RESULT_ID", "Anna id must be a 32-character hexadecimal MD5.")
        return "anna", md5, None
    fail(
        "SOURCE_REQUIRED",
        "Cannot infer source. Use a result_id from search or pass --source zlib/anna.",
        suggestions=[
            "Use zlib:<book_id>:<hash> from search results.",
            "Use anna:<32-character-md5> from search results.",
        ],
    )


def sanitize_filename(name: str, fallback: str = "book") -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", " ", name)
    name = re.sub(r"[<>:\"/\\|?*]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name or fallback

    path = Path(name)
    if path.stem.upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"

    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_FILENAME_BYTES:
        return name

    suffix = Path(name).suffix
    suffix_bytes = len(suffix.encode("utf-8"))
    byte_budget = max(1, MAX_FILENAME_BYTES - suffix_bytes)
    stem = Path(name).stem
    while stem and len(stem.encode("utf-8")) > byte_budget:
        stem = stem[:-1]
    return f"{stem or fallback[:20]}{suffix}"


def max_download_bytes(args: argparse.Namespace) -> int:
    return int(getattr(args, "max_size_mb", DEFAULT_MAX_DOWNLOAD_SIZE_MB)) * BYTES_PER_MIB


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def ensure_output_dir(output: str | None) -> Path:
    output_dir = Path(output).expanduser() if output else DEFAULT_DOWNLOAD_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()
    if not os.access(output_dir, os.W_OK):
        fail("OUTPUT_NOT_WRITABLE", f"Output directory is not writable: {output_dir}")
    return output_dir


def download_zlib(args: argparse.Namespace, book_id: str, hash_id: str | None) -> dict[str, Any]:
    if not hash_id:
        fail(
            "HASH_REQUIRED",
            "Z-Library download requires a hash id.",
            suggestions=["Use result_id from search, e.g. zlib:<book_id>:<hash>."],
        )

    cfg = load_config()
    z = init_zlibrary(cfg, require_auth=True)

    info = z.getBookInfo(book_id, hash_id)
    if not info or not info.get("success"):
        fail("BOOK_INFO_FAILED", "Could not fetch Z-Library book metadata.")

    book = info.get("book", {})
    filename, download_url = z.getBookDownload(book_id, hash_id)
    filename = sanitize_filename(filename, fallback=f"{book_id}.{book.get('extension') or 'book'}")
    output_dir = ensure_output_dir(args.output)
    final_path = unique_path(output_dir / filename)
    part_path = final_path.with_name(final_path.name + ".part")

    bytes_written = 0
    started = time.time()
    size_limit = max_download_bytes(args)
    for attempt in range(1, DOWNLOAD_RETRY_MAX + 1):
        if part_path.exists():
            part_path.unlink()
        try:
            if attempt > 1:
                _, download_url = z.getBookDownload(book_id, hash_id)
            bytes_written = z.downloadUrlToPath(
                download_url,
                part_path,
                max_bytes=size_limit,
            )
            if bytes_written <= 0:
                raise ValueError("Downloaded file is empty")
            os.replace(part_path, final_path)
            break
        except Exception as exc:
            if part_path.exists():
                part_path.unlink()
            if isinstance(exc, ValueError) and "size limit" in str(exc):
                fail(
                    "DOWNLOAD_TOO_LARGE",
                    "Z-Library file exceeds the configured size limit.",
                    suggestions=["Increase --max-size-mb if you trust the expected file size."],
                    details={"max_size_mb": size_limit // BYTES_PER_MIB},
                )
            if attempt >= DOWNLOAD_RETRY_MAX:
                fail(
                    "DOWNLOAD_FAILED",
                    "Z-Library download failed after retries.",
                    details={
                        "error_type": type(exc).__name__,
                        "attempts": DOWNLOAD_RETRY_MAX,
                        "max_size_mb": size_limit // BYTES_PER_MIB,
                    },
                )
            time.sleep(DOWNLOAD_RETRY_BACKOFF**attempt)

    payload = ok_payload(
        downloaded=True,
        source="zlib",
        result_id=f"zlib:{book_id}:{hash_id}",
        path=str(final_path),
        size=bytes_written,
        elapsed_seconds=round(time.time() - started, 2),
        book={
            "title": book.get("title") or book.get("name"),
            "author": book.get("author"),
            "extension": book.get("extension"),
            "size": book.get("filesizeString") or book.get("filesize"),
        },
    )
    return payload


def anna_links(md5: str) -> dict[str, Any]:
    if not ANNAS_AVAILABLE:
        fail("MODULE_MISSING", "Anna's Archive module is unavailable.")
    client = annas_archive.AnnasArchiveClient()
    return client.get_download_links(md5)


def content_type(response: requests.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def is_html_response(response: requests.Response) -> bool:
    return content_type(response) in HTML_CONTENT_TYPES


def looks_like_file_response(response: requests.Response) -> bool:
    media_type = content_type(response)
    if media_type in HTML_CONTENT_TYPES:
        return False
    if media_type in {"application/json", "text/json"}:
        return False
    if media_type in EBOOK_MEDIA_TYPE_EXTENSIONS:
        return True

    disposition = response.headers.get("content-disposition", "")
    disposition_name = filename_from_content_disposition(disposition)
    if disposition_name and Path(disposition_name).suffix.lower() in EBOOK_EXTENSIONS:
        return True

    path = unquote(urlparse(response.url).path).lower()
    return any(path.endswith(ext) for ext in EBOOK_EXTENSIONS)


def filename_from_content_disposition(disposition: str) -> str | None:
    if not disposition:
        return None

    filename_star = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.I)
    if filename_star:
        return unquote(filename_star.group(1).strip().strip('"'))

    filename = re.search(r"filename\s*=\s*\"?([^\";]+)\"?", disposition, re.I)
    if filename:
        return filename.group(1).strip()
    return None


def filename_from_response(response: requests.Response, fallback: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    filename = filename_from_content_disposition(disposition)
    if filename:
        safe_name = sanitize_filename(filename, fallback=fallback)
        suffix = Path(safe_name).suffix.lower()
        expected_suffix = EBOOK_MEDIA_TYPE_EXTENSIONS.get(content_type(response))
        if suffix in EBOOK_EXTENSIONS:
            return safe_name
        if expected_suffix:
            return sanitize_filename(f"{Path(safe_name).stem}{expected_suffix}", fallback=fallback)

    path_name = Path(unquote(urlparse(response.url).path)).name
    path_suffix = Path(path_name).suffix.lower()
    if path_name and path_suffix in EBOOK_EXTENSIONS:
        return sanitize_filename(path_name, fallback=fallback)

    media_type = content_type(response)
    return sanitize_filename(
        fallback + EBOOK_MEDIA_TYPE_EXTENSIONS.get(media_type, ""),
        fallback=fallback,
    )


def write_response_to_path(
    response: requests.Response,
    path: Path,
    *,
    max_bytes: int,
) -> tuple[int, str]:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise ValueError("Download exceeds the configured size limit")

    bytes_written = 0
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            output.write(chunk)
            bytes_written += len(chunk)
            digest.update(chunk)
            if bytes_written > max_bytes:
                raise ValueError("Download exceeds the configured size limit")
    return bytes_written, digest.hexdigest()


def anna_candidate_urls(links: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []

    for item in links.get("fast_downloads") or []:
        url = item.get("url") if isinstance(item, dict) else item
        if url:
            candidates.append({"kind": "fast_download", "url": url})

    for key in ("libgen_li", "libgen_is", "libgen_rs"):
        url = links.get(key)
        if url:
            candidates.append({"kind": key, "url": url})

    seen = set()
    unique = []
    for candidate in candidates:
        url = candidate["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(candidate)
    return unique


def link_score(anchor_text: str, href: str) -> int:
    text = anchor_text.lower()
    href_lower = href.lower()
    score = 0
    if any(href_lower.split("?", 1)[0].endswith(ext) for ext in EBOOK_EXTENSIONS):
        score += 50
    if re.search(r"\b(get|download|download now|mirror)\b", text):
        score += 30
    if any(token in href_lower for token in ("/get.php", "/download", "/dl/", "/file.php")):
        score += 25
    if "ads.php" in href_lower:
        score += 5
    if href_lower.startswith("#") or href_lower.startswith("javascript:"):
        score -= 100
    return score


def extract_download_candidates(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        score = link_score(anchor.get_text(" ", strip=True), href)
        if score <= 0:
            continue
        scored.append((score, absolute))

    scored.sort(key=lambda item: item[0], reverse=True)
    urls: list[str] = []
    seen = set()
    for _, url in scored:
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def resolve_anna_file_response(
    session: requests.Session,
    start_url: str,
    *,
    max_depth: int = 3,
) -> tuple[requests.Response, str]:
    queue: list[tuple[str, int]] = [(start_url, 0)]
    visited: set[str] = set()
    last_error = ""

    while queue:
        url, depth = queue.pop(0)
        if url in visited or depth > max_depth:
            continue
        visited.add(url)

        try:
            response = safe_get(
                session,
                url,
                headers=HEADERS,
                timeout=DIRECT_DOWNLOAD_TIMEOUT,
                stream=True,
            )
            response.raise_for_status()
        except requests.RequestException:
            last_error = "Network request failed"
            continue

        if looks_like_file_response(response):
            return response, response.url

        if not is_html_response(response):
            response.close()
            last_error = f"Unsupported content type: {content_type(response) or 'unknown'}"
            continue

        try:
            html = response.text
        finally:
            response.close()

        for next_url in extract_download_candidates(html, response.url):
            if next_url not in visited:
                queue.append((next_url, depth + 1))
            if len(queue) >= 50:
                break

    raise ValueError(last_error or "No direct file response found")


def download_anna(args: argparse.Namespace, md5: str) -> dict[str, Any]:
    md5 = md5.lower()
    if not ANNA_MD5_PATTERN.fullmatch(md5):
        fail("INVALID_RESULT_ID", "Anna id must be a 32-character hexadecimal MD5.")

    try:
        links = anna_links(md5)
    except Exception as exc:
        fail(
            "LINK_RESOLUTION_FAILED",
            "Could not resolve Anna's Archive download links.",
            suggestions=[
                "Set ANNAS_BASE_URL to a reachable mirror.",
                "Configure HTTPS_PROXY or ALL_PROXY if your network blocks access.",
            ],
            details={"error_type": type(exc).__name__},
        )

    candidates = anna_candidate_urls(links)
    if not candidates:
        fail(
            "DOWNLOAD_LINKS_NOT_FOUND",
            "Anna's Archive did not expose any downloadable sources.",
            details={
                "detail_url": links.get("detail_url"),
                "available_link_kinds": [],
            },
        )

    output_dir = ensure_output_dir(args.output)
    session = requests.Session()
    session.headers.update(HEADERS)
    attempts = []
    started = time.time()
    size_limit = max_download_bytes(args)

    for candidate in candidates:
        part_path: Path | None = None
        try:
            response, final_url = resolve_anna_file_response(session, candidate["url"])
            filename = filename_from_response(response, fallback=f"anna-{md5}")
            final_path = unique_path(output_dir / filename)
            part_path = final_path.with_name(final_path.name + ".part")
            if part_path.exists():
                part_path.unlink()
            try:
                bytes_written, actual_md5 = write_response_to_path(
                    response,
                    part_path,
                    max_bytes=size_limit,
                )
            finally:
                response.close()

            if bytes_written <= 0:
                if part_path.exists():
                    part_path.unlink()
                raise ValueError("Downloaded file is empty")
            if actual_md5 != md5:
                if part_path.exists():
                    part_path.unlink()
                raise ValueError("Downloaded file checksum does not match the Anna MD5")

            os.replace(part_path, final_path)
            return ok_payload(
                downloaded=True,
                source="anna",
                result_id=f"anna:{md5}",
                path=str(final_path),
                size=bytes_written,
                elapsed_seconds=round(time.time() - started, 2),
                detail_url=links.get("detail_url"),
                link_kind=candidate["kind"],
                start_origin=url_origin(candidate["url"]),
                final_origin=url_origin(final_url),
                md5=actual_md5,
            )
        except Exception as exc:
            if part_path is not None and part_path.exists():
                part_path.unlink()
            attempts.append(
                {
                    "kind": candidate["kind"],
                    "origin": url_origin(candidate["url"]),
                    "error_type": type(exc).__name__,
                    "error": (
                        str(exc)
                        if isinstance(exc, (ValueError, UnsafeUrlError))
                        else "Network request failed"
                    ),
                }
            )

    fail(
        "DOWNLOAD_FAILED",
        "Anna's Archive download links were found, but none produced a downloadable file.",
        suggestions=[
            "Open one of the returned links in a browser.",
            "Set ANNAS_BASE_URL to another reachable mirror.",
            "Configure HTTPS_PROXY or ALL_PROXY if your network blocks downloads.",
        ],
        details={
            "attempts": attempts,
            "detail_url": links.get("detail_url"),
            "available_link_kinds": [item["kind"] for item in candidates],
            "max_size_mb": size_limit // BYTES_PER_MIB,
        },
    )


def cmd_download(args: argparse.Namespace) -> dict[str, Any]:
    source, item_id, hash_id = parse_result_ref(args.book_id, args.source, args.hash_id)
    if source == "zlib":
        payload = download_zlib(args, item_id, hash_id)
    else:
        payload = download_anna(args, item_id)

    if args.json:
        emit_json(payload)
    else:
        if payload.get("downloaded"):
            print_human(args, f"Downloaded: {payload['path']}")
            print_human(args, f"Size: {payload['size']} bytes")
        else:
            print_human(args, payload.get("message", "Download links resolved."))
            print_human(args, f"Detail URL: {payload.get('detail_url')}")
            links = payload.get("links", {})
            for key, value in links.items():
                if key == "detail_url" or not value:
                    continue
                print_human(args, f"{key}: {value}")
    return payload


def cmd_resolve(args: argparse.Namespace) -> dict[str, Any]:
    source, item_id, hash_id = parse_result_ref(args.result_id, args.source, None)
    if source == "zlib":
        if not hash_id:
            fail("HASH_REQUIRED", "Z-Library resolve requires zlib:<book_id>:<hash>.")
        cfg = load_config()
        z = init_zlibrary(cfg, require_auth=True)
        info = z.getBookInfo(item_id, hash_id)
        if not info or not info.get("success"):
            fail("BOOK_INFO_FAILED", "Could not fetch Z-Library book metadata.")
        payload = ok_payload(
            source="zlib",
            result_id=f"zlib:{item_id}:{hash_id}",
            book=normalize_zlib_book(info.get("book", {})),
        )
    else:
        try:
            links = anna_links(item_id)
        except Exception as exc:
            fail(
                "LINK_RESOLUTION_FAILED",
                "Could not resolve Anna's Archive download links.",
                suggestions=[
                    "Set ANNAS_BASE_URL to a reachable verified mirror.",
                    "Run: python3 {baseDir}/scripts/run.py doctor --json",
                ],
                details={"error_type": type(exc).__name__},
            )
        payload = ok_payload(
            source="anna",
            result_id=f"anna:{item_id}",
            detail_url=links.get("detail_url"),
            links=links,
        )

    if args.json:
        emit_json(payload)
    else:
        print_human(args, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def cmd_info(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    z = init_zlibrary(cfg, require_auth=True)
    profile = z.getProfile()
    if not profile or not profile.get("success"):
        fail("PROFILE_FAILED", "Could not fetch Z-Library profile.")

    user = profile["user"]
    payload = ok_payload(
        user={
            "name": user.get("name"),
            "email": mask_email(user.get("email")),
            "kindle_email": mask_email(user.get("kindle_email")),
            "downloads_today": user.get("downloads_today", 0),
            "downloads_limit": user.get("downloads_limit", 10),
            "downloads_left": z.getDownloadsLeft(),
        },
        domain=z.getDomain(),
    )
    if args.json:
        emit_json(payload)
    else:
        user_info = payload["user"]
        print_human(args, f"User: {user_info.get('name') or 'N/A'}")
        print_human(args, f"Email: {user_info.get('email') or 'N/A'}")
        print_human(
            args,
            f"Downloads: {user_info['downloads_today']} / {user_info['downloads_limit']} "
            f"({user_info['downloads_left']} left)",
        )
        print_human(args, f"Domain: {payload['domain']}")
    return payload


def cmd_domains(args: argparse.Namespace) -> dict[str, Any]:
    discovered = fetch_domains()
    domains = discovered or FALLBACK_ZLIB_DOMAINS
    checks = [{"domain": domain, "available": test_domain(domain)} for domain in domains]
    payload = ok_payload(
        domains=checks,
        count=len(checks),
        source="discovered" if discovered else "fallback",
    )
    if args.json:
        emit_json(payload)
    else:
        for item in checks:
            print_human(args, f"{item['domain']:<45} {'ok' if item['available'] else 'failed'}")
    return payload


def cmd_popular(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    z = init_zlibrary(cfg, require_auth=True)
    result = z.getMostPopular()
    if not result or not result.get("success"):
        fail("POPULAR_FAILED", "Could not fetch popular books.")
    books = [normalize_zlib_book(book) for book in result.get("books", [])[: args.limit]]
    payload = ok_payload(count=len(books), results=books, source="zlib")
    if args.json:
        emit_json(payload)
    else:
        print_search_table(args, books)
    return payload


def read_password(args: argparse.Namespace) -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass("Z-Library password: ")


def login_zlib(args: argparse.Namespace) -> dict[str, Any]:
    if not args.email:
        fail("EMAIL_REQUIRED", "Email is required for Z-Library login.")

    script_dir = Path(__file__).parent.resolve()
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from .zlibrary import Zlibrary

    working, checks = find_working_domain(None)
    if not working:
        fail(
            "SOURCE_UNAVAILABLE",
            "No reachable Z-Library mirror was found.",
            details={"domain_checks": checks},
        )

    password = read_password(args)
    if not password:
        fail("PASSWORD_REQUIRED", "Password is required for Z-Library login.")

    z = Zlibrary()
    z.setDomain(working)
    result = z.login(args.email, password)
    if not result or not result.get("success"):
        fail(
            "LOGIN_FAILED",
            "Z-Library login failed.",
            details={"domain": working},
            suggestions=["Check your email/password and try again."],
        )

    user = result["user"]
    cfg = load_config(strict=False)
    cfg["remix_userid"] = str(user["id"])
    cfg["remix_userkey"] = user["remix_userkey"]
    cfg["domain"] = working
    cfg["domain_source"] = domain_source(working, checks)
    cfg["domain_trusted"] = domain_trust_is_persistent(working, checks)
    cfg["email"] = args.email
    cfg["name"] = user.get("name", "")
    save_config(cfg)

    payload = ok_payload(
        source="zlib",
        authenticated=True,
        config_file=display_path(CONFIG_FILE),
        config_file_mode=_mode(CONFIG_FILE),
        user={"name": user.get("name"), "email": mask_email(user.get("email"))},
        domain=working,
    )
    return payload


def cmd_auth(args: argparse.Namespace) -> dict[str, Any]:
    if args.auth_command == "status":
        payload = ok_payload(**config_status())
    elif args.auth_command == "login" and args.auth_source == "zlib":
        payload = login_zlib(args)
    elif args.auth_command == "logout":
        cfg = load_config(strict=False)
        save_config(remove_zlib_auth(cfg))
        payload = ok_payload(
            source="zlib",
            authenticated=False,
            config_file=display_path(CONFIG_FILE),
        )
    else:
        fail("INVALID_COMMAND", "Unsupported auth command.")

    if args.json:
        emit_json(payload)
    elif args.auth_command == "status":
        zlib_status = payload["zlib"]
        authenticated = "yes" if zlib_status["has_token"] else "no"
        print_human(args, f"Z-Library authenticated: {authenticated}")
        if zlib_status.get("email"):
            print_human(args, f"Account: {zlib_status['email']}")
        print_human(args, f"Config: {payload['config_file']}")
    elif args.auth_command == "login":
        print_human(args, f"Logged in to Z-Library via {payload['domain']}.")
        print_human(
            args,
            f"Token saved to {payload['config_file']} ({payload['config_file_mode']}).",
        )
    else:
        print_human(args, "Z-Library credentials removed from local config.")
    return payload


def check_anna() -> SourceStatus:
    if not ANNAS_AVAILABLE:
        return SourceStatus(
            "anna",
            available=False,
            authenticated=False,
            can_search=False,
            can_download=False,
            can_attempt_download=False,
            status="module_missing",
            message="annas_archive.py could not be imported.",
        )
    base_url = anna_base_url()
    if not base_url:
        return SourceStatus(
            "anna",
            available=False,
            authenticated=False,
            can_search=False,
            can_download=False,
            can_attempt_download=False,
            status="module_missing",
            message="Anna's Archive base URL is unavailable.",
        )
    try:
        validate_http_url(
            base_url,
            require_https=not env_flag(
                ALLOW_INSECURE_HTTP_ENV,
                LEGACY_ALLOW_INSECURE_HTTP_ENV,
            ),
        )
        resp = requests.get(
            base_url,
            headers=HEADERS,
            timeout=15,
            allow_redirects=False,
        )
        available = 200 <= resp.status_code < 300
        return SourceStatus(
            "anna",
            available=available,
            authenticated=False,
            can_search=available,
            can_download=False,
            can_attempt_download=available,
            status="ok" if available else "unavailable",
            details={
                "base_origin": url_origin(base_url),
                "status_code": resp.status_code,
                "mode": "html_best_effort",
            },
        )
    except (requests.RequestException, UnsafeUrlError) as exc:
        return SourceStatus(
            "anna",
            available=False,
            authenticated=False,
            can_search=False,
            can_download=False,
            can_attempt_download=False,
            status="unavailable",
            message="Anna's Archive health check failed.",
            details={"base_origin": url_origin(base_url), "error_type": type(exc).__name__},
        )


def check_zlib(cfg: dict[str, Any]) -> SourceStatus:
    working, checks = find_working_domain(
        cfg.get("domain"),
        preferred_trusted=bool(cfg.get("domain_trusted")),
    )
    if not working:
        return SourceStatus(
            "zlib",
            available=False,
            authenticated=has_zlib_auth(cfg),
            can_search=False,
            can_download=False,
            status="unavailable",
            message="No reachable Z-Library mirror was found.",
            details={"domain_checks": checks},
        )
    authenticated = False
    if has_zlib_auth(cfg):
        try:
            z = init_zlibrary(
                dict(cfg),
                require_auth=True,
                update_config=False,
                resolved_domain=working,
            )
            authenticated = z.isLoggedIn()
        except Exception:
            authenticated = False
    return SourceStatus(
        "zlib",
        available=True,
        authenticated=authenticated,
        can_search=authenticated,
        can_download=authenticated,
        can_attempt_download=authenticated,
        status="ok" if authenticated else "auth_required",
        message="" if authenticated else "Z-Library is reachable but login is required.",
        details={"domain": working, "domain_checks": checks},
    )


def nearest_existing_parent(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def download_dir_status(path: Path = DEFAULT_DOWNLOAD_DIR) -> dict[str, Any]:
    path = path.expanduser()
    parent = path.parent
    nearest_parent = nearest_existing_parent(path)
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "writable": os.access(path, os.W_OK) if exists else False,
        "parent": str(parent),
        "parent_exists": parent.exists(),
        "parent_writable": os.access(parent, os.W_OK) if parent.exists() else False,
        "nearest_existing_parent": str(nearest_parent),
        "creatable": os.access(nearest_parent, os.W_OK),
    }


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(strict=False)
    config = config_status()
    zlib_status = check_zlib(cfg)
    anna_status = check_anna()
    proxy = {
        "HTTPS_PROXY": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
        "HTTP_PROXY": bool(os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")),
        "ALL_PROXY": bool(os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")),
    }
    payload = ok_payload(
        config=config,
        sources=[zlib_status.to_dict(), anna_status.to_dict()],
        proxy=proxy,
        download_dir=download_dir_status(),
    )
    if args.json:
        emit_json(payload)
    else:
        print_human(args, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def cmd_batch(args: argparse.Namespace) -> dict[str, Any]:
    batch_file = Path(args.file)
    if not batch_file.exists():
        fail("FILE_NOT_FOUND", f"Batch file does not exist: {batch_file}")

    if batch_file.stat().st_size > BYTES_PER_MIB:
        fail("BATCH_TOO_LARGE", "Batch file must be 1 MiB or smaller.")

    lines = [
        line.strip()
        for line in batch_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        fail("BATCH_EMPTY", "Batch file does not contain any entries.")
    if len(lines) > 1000:
        fail("BATCH_TOO_LARGE", "Batch file may contain at most 1000 entries.")

    results = []
    for index, line in enumerate(lines, 1):
        parts = line.split()
        try:
            source, item_id, hash_id = parse_result_ref(
                parts[0],
                args.source,
                parts[1] if len(parts) > 1 else "",
            )
            result = (
                download_zlib(args, item_id, hash_id)
                if source == "zlib"
                else download_anna(args, item_id)
            )
            results.append({"index": index, "input": line, "result": result})
        except SkillError as exc:
            results.append({"index": index, "input": line, "error": exc.to_dict()})
        except Exception as exc:
            results.append(
                {
                    "index": index,
                    "input": line,
                    "error": {
                        "code": "UNEXPECTED_ERROR",
                        "message": "Entry failed unexpectedly.",
                        "recoverable": True,
                        "details": {"error_type": type(exc).__name__},
                    },
                }
            )

    payload = ok_payload(
        count=len(results),
        success_count=sum(1 for item in results if "result" in item),
        fail_count=sum(1 for item in results if "error" in item),
        results=results,
    )
    if args.json:
        emit_json(payload)
    else:
        print_human(args, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON on stdout")


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def search_limit(value: str) -> int:
    number = positive_int(value)
    if number > 100:
        raise argparse.ArgumentTypeError("must be 100 or less")
    return number


def publication_year(value: str) -> int:
    number = int(value)
    if number < 1 or number > 3000:
        raise argparse.ArgumentTypeError("must be between 1 and 3000")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zlib-anna-skill",
        description="Bundled execution engine for Z-Library and Anna's Archive ebook tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  {RUNNER_COMMAND} doctor --json\n"
            f'  {RUNNER_COMMAND} search "Python Programming" --source anna --json\n'
            f'  {RUNNER_COMMAND} download "anna:<32-character-md5>" '
            "--output ~/Books --json\n"
            f"  {RUNNER_COMMAND} auth login zlib --email you@example.com\n"
            "\n"
            "If Z-Library domains are blocked or stale, set ZLIBRARY_DOMAIN to a reachable "
            "mirror and run doctor again."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {SKILL_VERSION}")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    auth_parser = subparsers.add_parser("auth", help="Manage local auth state")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_status = auth_subparsers.add_parser("status", help="Show local auth status")
    add_common(auth_status)
    auth_login = auth_subparsers.add_parser("login", help="Login to a source")
    add_common(auth_login)
    auth_login.add_argument("auth_source", choices=["zlib"], help="Source to authenticate")
    auth_login.add_argument("--email", required=True, help="Z-Library account email")
    auth_login.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from stdin",
    )
    auth_logout = auth_subparsers.add_parser("logout", help="Clear saved Z-Library token")
    add_common(auth_logout)

    search_parser = subparsers.add_parser(
        "search",
        help="Search books",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f'  {RUNNER_COMMAND} search "Python" --source anna --json\n'
            f'  {RUNNER_COMMAND} search "Clean Code" --source all '
            "--ext epub,pdf --json\n"
            f'  {RUNNER_COMMAND} search "database systems" --source zlib --lang en --json'
        ),
    )
    add_common(search_parser)
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--limit", type=search_limit, default=10, help="Results per source")
    search_parser.add_argument("--page", type=positive_int, default=1, help="Page number")
    search_parser.add_argument(
        "--year-from",
        type=publication_year,
        help="Minimum publication year",
    )
    search_parser.add_argument("--year-to", type=publication_year, help="Maximum publication year")
    search_parser.add_argument("--lang", help="Language filter, e.g. en or zh")
    search_parser.add_argument("--ext", help="Comma-separated format filter, e.g. epub,pdf")
    search_parser.add_argument("--order", help="Z-Library order, e.g. popular/newest/relevance")
    search_parser.add_argument(
        "--source",
        choices=["zlib", "anna", "all"],
        default="all",
        help="Search source (default: all)",
    )

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a result id to metadata/links")
    add_common(resolve_parser)
    resolve_parser.add_argument("result_id", help="result_id from search, e.g. zlib:<id>:<hash>")
    resolve_parser.add_argument("--source", choices=["auto", "zlib", "anna"], default="auto")

    download_parser = subparsers.add_parser(
        "download",
        help="Download a selected result",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f'  {RUNNER_COMMAND} download "anna:<32-character-md5>" '
            "--output ~/Books --json\n"
            f'  {RUNNER_COMMAND} download "zlib:<book_id>:<hash>" '
            "--output ~/Books --json\n"
            f"  {RUNNER_COMMAND} download <md5> --source anna --json"
        ),
    )
    add_common(download_parser)
    download_parser.add_argument("book_id", help="result_id, Z-Library book id, or Anna MD5")
    download_parser.add_argument("hash_id", nargs="?", default="", help="Z-Library hash id")
    download_parser.add_argument("--output", "-o", help="Download directory")
    download_parser.add_argument("--source", choices=["auto", "zlib", "anna"], default="auto")
    download_parser.add_argument(
        "--max-size-mb",
        type=positive_int,
        default=DEFAULT_MAX_DOWNLOAD_SIZE_MB,
        help=f"Maximum file size in MiB (default: {DEFAULT_MAX_DOWNLOAD_SIZE_MB})",
    )

    info_parser = subparsers.add_parser("info", help="Show Z-Library account info")
    add_common(info_parser)

    domains_parser = subparsers.add_parser("domains", help="List reachable Z-Library mirrors")
    add_common(domains_parser)

    popular_parser = subparsers.add_parser("popular", help="Show Z-Library popular books")
    add_common(popular_parser)
    popular_parser.add_argument("--limit", type=search_limit, default=10)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose source/auth/network availability",
    )
    add_common(doctor_parser)

    batch_parser = subparsers.add_parser("batch", help="Batch download entries from a file")
    add_common(batch_parser)
    batch_parser.add_argument("file", help="Batch file")
    batch_parser.add_argument("--output", "-o", help="Download directory")
    batch_parser.add_argument("--source", choices=["auto", "zlib", "anna"], default="auto")
    batch_parser.add_argument(
        "--max-size-mb",
        type=positive_int,
        default=DEFAULT_MAX_DOWNLOAD_SIZE_MB,
        help=f"Maximum size per file in MiB (default: {DEFAULT_MAX_DOWNLOAD_SIZE_MB})",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argument_list = list(argv) if argv is not None else sys.argv[1:]
    try:
        args = parser.parse_args(argument_list)
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
        if exit_code and "--json" in argument_list:
            error = SkillError(
                "INVALID_ARGUMENT",
                "Command-line arguments are invalid; see stderr for usage.",
                recoverable=True,
                exit_code=exit_code,
            )
            emit_json(error_payload(error))
        return exit_code

    if args.command is None:
        parser.print_help()
        return 1

    commands = {
        "auth": cmd_auth,
        "search": cmd_search,
        "resolve": cmd_resolve,
        "download": cmd_download,
        "batch": cmd_batch,
        "info": cmd_info,
        "domains": cmd_domains,
        "popular": cmd_popular,
        "doctor": cmd_doctor,
    }

    try:
        commands[args.command](args)
        return 0
    except SkillError as exc:
        if getattr(args, "json", False):
            emit_json(error_payload(exc))
        else:
            print(f"Error [{exc.code}]: {exc.message}", file=sys.stderr)
            for suggestion in exc.suggestions:
                print(f"- {suggestion}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        error = SkillError(
            "INTERRUPTED",
            "Command interrupted by the user.",
            recoverable=True,
            exit_code=130,
        )
        if getattr(args, "json", False):
            emit_json(error_payload(error))
        else:
            print(f"Error [{error.code}]: {error.message}", file=sys.stderr)
        return error.exit_code
    except Exception as exc:
        if env_flag("ZLIB_ANNA_DEBUG", "ZLIB_CLI_DEBUG"):
            raise
        error = SkillError(
            "UNEXPECTED_ERROR",
            "The command failed unexpectedly without exposing sensitive details.",
            recoverable=False,
            suggestions=[
                "Run: python3 {baseDir}/scripts/run.py doctor --json",
                "Retry with ZLIB_ANNA_DEBUG=1 for a traceback.",
            ],
            details={"error_type": type(exc).__name__},
        )
        if getattr(args, "json", False):
            emit_json(error_payload(error))
        else:
            print(f"Error [{error.code}]: {error.message}", file=sys.stderr)
            for suggestion in error.suggestions:
                print(f"- {suggestion}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
