"""Network safety helpers for user-configured and remotely discovered URLs."""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests

ALLOW_PRIVATE_NETWORK_ENV = "ZLIB_SKILL_ALLOW_PRIVATE_NETWORK"
ALLOW_INSECURE_HTTP_ENV = "ZLIB_SKILL_ALLOW_INSECURE_HTTP"
PREVIOUS_ALLOW_PRIVATE_NETWORK_ENV = "ZLIB_ANNA_ALLOW_PRIVATE_NETWORK"
PREVIOUS_ALLOW_INSECURE_HTTP_ENV = "ZLIB_ANNA_ALLOW_INSECURE_HTTP"
LEGACY_ALLOW_PRIVATE_NETWORK_ENV = "ZLIB_CLI_ALLOW_PRIVATE_NETWORK"
LEGACY_ALLOW_INSECURE_HTTP_ENV = "ZLIB_CLI_ALLOW_INSECURE_HTTP"
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
PROXY_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)


class UnsafeUrlError(ValueError):
    """Raised when a URL could reach an unsafe or unexpected network target."""


def env_flag(*names: str) -> bool:
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"} for name in names
    )


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global


def _is_proxy_fake_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return any(address in network for network in PROXY_FAKE_IP_NETWORKS)


def _normalized_hostnames(values: set[str] | None) -> set[str]:
    return {value.rstrip(".").lower() for value in (values or set()) if value}


def validate_http_url(
    url: str,
    *,
    require_https: bool = False,
    resolve_dns: bool = True,
    trusted_proxy_hosts: set[str] | None = None,
) -> str:
    """Validate an HTTP(S) URL and reject local/private destinations by default."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL is empty")

    value = url.strip()
    parsed = urlparse(value)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes:
        expected = "HTTPS" if require_https else "HTTP or HTTPS"
        raise UnsafeUrlError(f"URL must use {expected}")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs containing embedded credentials are not allowed")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError("URL must include a hostname")
    hostname = hostname.rstrip(".").lower()

    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL contains an invalid port") from exc

    if env_flag(
        ALLOW_PRIVATE_NETWORK_ENV,
        PREVIOUS_ALLOW_PRIVATE_NETWORK_ENV,
        LEGACY_ALLOW_PRIVATE_NETWORK_ENV,
    ):
        return value

    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrlError(f"Local network target is blocked: {hostname}")

    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        if not literal_address.is_global:
            raise UnsafeUrlError(f"Private or non-routable target is blocked: {hostname}")
        return value

    if not resolve_dns:
        return value

    try:
        addresses = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Hostname could not be resolved: {hostname}") from exc

    resolved = {item[4][0] for item in addresses if item[4]}
    if not resolved:
        raise UnsafeUrlError(f"Hostname did not resolve to an address: {hostname}")
    blocked = sorted(address for address in resolved if not _is_public_ip(address))
    if blocked:
        trusted_hosts = _normalized_hostnames(trusted_proxy_hosts)
        if (
            parsed.scheme.lower() == "https"
            and hostname in trusted_hosts
            and all(_is_proxy_fake_ip(address) for address in blocked)
        ):
            return value
        raise UnsafeUrlError(f"Hostname resolves to a private or non-routable address: {hostname}")
    return value


def safe_get(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: Any = 30,
    stream: bool = False,
    max_redirects: int = 5,
    trusted_proxy_hosts: set[str] | None = None,
) -> requests.Response:
    """GET a URL while validating every redirect target."""
    current_url = url
    for _ in range(max_redirects + 1):
        validate_http_url(current_url, trusted_proxy_hosts=trusted_proxy_hosts)
        response = session.get(
            current_url,
            headers=headers,
            timeout=timeout,
            stream=stream,
            allow_redirects=False,
        )
        if response.status_code not in REDIRECT_STATUS_CODES:
            return response

        location = response.headers.get("location")
        response_url = getattr(response, "url", None) or current_url
        response.close()
        if not location:
            raise requests.TooManyRedirects("Redirect response did not include a Location header")
        current_url = urljoin(response_url, location)

    raise requests.TooManyRedirects(f"More than {max_redirects} redirects")


def url_origin(url: str) -> str:
    """Return only the origin of a URL for logs and error payloads."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return "<invalid-url>"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if port:
        host = f"{host}:{port}"
    return urlunparse((parsed.scheme, host, "", "", "", ""))
