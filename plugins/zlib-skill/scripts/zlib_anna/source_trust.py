"""Service-specific origin trust policy.

Network target validation answers whether a URL is technically safe to contact.
This module answers the separate question of whether an ebook service claims the
origin and whether it is safe to use for credentials.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .network_safety import (
    ALLOW_INSECURE_HTTP_ENV,
    LEGACY_ALLOW_INSECURE_HTTP_ENV,
    PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
    UnsafeUrlError,
    env_flag,
    url_origin,
    validate_http_url,
)

ALLOW_UNTRUSTED_ANNA_DOMAIN_ENV = "ANNAS_ALLOW_UNTRUSTED_DOMAIN"

OFFICIAL_ANNA_BASE_URLS = (
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
)
OFFICIAL_ANNA_HOSTS = frozenset(urlparse(base_url).hostname for base_url in OFFICIAL_ANNA_BASE_URLS)

# Anna's Archive's own FAQ labels these domains fraudulent.  They remain
# blocked even when the general custom-domain development opt-in is enabled.
KNOWN_FRAUDULENT_ANNA_DOMAINS = frozenset(
    {
        "annas-archive.io",
        "annas-archive.is",
        "annas-archive.su",
    }
)

# Z-Library has publicly warned that these lookalikes are unaffiliated and may
# collect credentials or payments.  Do not make the explicit custom-domain
# escape hatch override a known fraud decision.
KNOWN_FRAUDULENT_ZLIB_DOMAINS = frozenset(
    {
        "z-lib.id",
        "z-lib.is",
        "zlibrary.to",
    }
)

# A discovery response can be compromised independently of TLS.  Shared
# hosting subdomains are therefore unsuitable as automatically trusted service
# origins: ownership cannot be inferred from the registrable platform suffix.
UNTRUSTED_SHARED_HOST_SUFFIXES = frozenset(
    {
        "fly.dev",
        "github.io",
        "gitlab.io",
        "netlify.app",
        "onrender.com",
        "pages.dev",
        "vercel.app",
        "workers.dev",
    }
)


def _normalized_hostname(value: str | None) -> str | None:
    if not value:
        return None
    hostname = value.rstrip(".").lower()
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _matches_domain(hostname: str, candidate: str) -> bool:
    return hostname == candidate or hostname.endswith(f".{candidate}")


def zlib_domain_rejection_reason(domain: str | None) -> str | None:
    """Return a stable reason when an automatically supplied domain is unsafe."""
    hostname = _normalized_hostname(domain)
    if not hostname:
        return "invalid_domain"
    if any(_matches_domain(hostname, domain) for domain in KNOWN_FRAUDULENT_ZLIB_DOMAINS):
        return "known_fraudulent_domain"
    if any(_matches_domain(hostname, suffix) for suffix in UNTRUSTED_SHARED_HOST_SUFFIXES):
        return "shared_hosting_domain"
    return None


def anna_base_url_policy(url: str | None) -> dict[str, Any]:
    """Classify one Anna base URL without exposing its path or query."""
    result: dict[str, Any] = {
        "origin": "<invalid-url>",
        "hostname": None,
        "allowed": False,
        "trusted": False,
        "reason": "invalid_url",
    }
    if not isinstance(url, str) or not url.strip():
        return result

    value = url.strip()
    result["origin"] = url_origin(value)
    require_https = not env_flag(
        ALLOW_INSECURE_HTTP_ENV,
        PREVIOUS_ALLOW_INSECURE_HTTP_ENV,
        LEGACY_ALLOW_INSECURE_HTTP_ENV,
    )
    try:
        validate_http_url(value, require_https=require_https, resolve_dns=False)
    except UnsafeUrlError:
        return result

    parsed = urlparse(value)
    hostname = _normalized_hostname(parsed.hostname)
    result["hostname"] = hostname
    if (
        not hostname
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        result["reason"] = "base_url_must_be_origin"
        return result
    if any(_matches_domain(hostname, domain) for domain in KNOWN_FRAUDULENT_ANNA_DOMAINS):
        result["reason"] = "known_fraudulent_domain"
        return result
    if hostname in OFFICIAL_ANNA_HOSTS:
        result.update(allowed=True, trusted=True, reason="official")
        return result
    if env_flag(ALLOW_UNTRUSTED_ANNA_DOMAIN_ENV):
        result.update(allowed=True, trusted=False, reason="explicit_opt_in")
        return result
    result["reason"] = "untrusted_domain"
    return result


def validate_anna_base_url(url: str) -> str:
    """Return a normalized permitted Anna origin or fail closed."""
    policy = anna_base_url_policy(url)
    if not policy["allowed"]:
        reason = policy["reason"]
        messages = {
            "known_fraudulent_domain": "Known fraudulent Anna's Archive domain is blocked",
            "base_url_must_be_origin": "Anna's Archive base URL must be an origin only",
            "untrusted_domain": (
                "Untrusted Anna's Archive domain requires explicit development opt-in"
            ),
        }
        raise UnsafeUrlError(messages.get(reason, "Anna's Archive base URL is invalid"))
    return str(policy["origin"])


__all__ = [
    "ALLOW_UNTRUSTED_ANNA_DOMAIN_ENV",
    "KNOWN_FRAUDULENT_ANNA_DOMAINS",
    "KNOWN_FRAUDULENT_ZLIB_DOMAINS",
    "OFFICIAL_ANNA_BASE_URLS",
    "OFFICIAL_ANNA_HOSTS",
    "UNTRUSTED_SHARED_HOST_SUFFIXES",
    "anna_base_url_policy",
    "validate_anna_base_url",
    "zlib_domain_rejection_reason",
]
