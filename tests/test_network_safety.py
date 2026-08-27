"""Unit tests for outbound URL and redirect safety."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zlib_anna.network_safety import UnsafeUrlError, safe_get, url_origin, validate_http_url
from zlib_anna.source_trust import (
    anna_base_url_policy,
    validate_anna_base_url,
    zlib_domain_rejection_reason,
)


def public_dns(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/file.pdf",
        "http://10.0.0.5/file.pdf",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/file.pdf",
        "file:///tmp/book.pdf",
        "https://user:password@example.com/book.pdf",  # pragma: allowlist secret
    ],
)
def test_validate_http_url_rejects_unsafe_targets(url, monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_ANNA_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_CLI_ALLOW_PRIVATE_NETWORK", raising=False)

    with pytest.raises(UnsafeUrlError):
        validate_http_url(url)


def test_validate_http_url_rejects_hostname_resolving_private(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_ANNA_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_CLI_ALLOW_PRIVATE_NETWORK", raising=False)
    private_dns = [(2, 1, 6, "", ("192.168.1.10", 443))]

    with patch("zlib_anna.network_safety.socket.getaddrinfo", return_value=private_dns):
        with pytest.raises(UnsafeUrlError):
            validate_http_url("https://mirror.example/book.pdf")


def test_validate_http_url_allows_proxy_fake_ip_for_trusted_https_host(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    fake_ip_dns = [(2, 1, 6, "", ("198.18.2.231", 443))]

    with patch("zlib_anna.network_safety.socket.getaddrinfo", return_value=fake_ip_dns):
        result = validate_http_url(
            "https://annas-archive.gl/search?q=python",
            trusted_proxy_hosts={"annas-archive.gl"},
        )

    assert result == "https://annas-archive.gl/search?q=python"


def test_validate_http_url_rejects_proxy_fake_ip_for_untrusted_host(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    fake_ip_dns = [(2, 1, 6, "", ("198.18.2.231", 443))]

    with patch("zlib_anna.network_safety.socket.getaddrinfo", return_value=fake_ip_dns):
        with pytest.raises(UnsafeUrlError):
            validate_http_url("https://unknown.example/book.pdf")


def test_validate_http_url_rejects_regular_private_ip_even_for_trusted_host(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    private_dns = [(2, 1, 6, "", ("192.168.1.10", 443))]

    with patch("zlib_anna.network_safety.socket.getaddrinfo", return_value=private_dns):
        with pytest.raises(UnsafeUrlError):
            validate_http_url(
                "https://annas-archive.gl/search?q=python",
                trusted_proxy_hosts={"annas-archive.gl"},
            )


def test_private_network_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", "1")

    assert validate_http_url("http://127.0.0.1/book.pdf") == ("http://127.0.0.1/book.pdf")


@pytest.mark.parametrize(
    "name",
    ["ZLIB_ANNA_ALLOW_PRIVATE_NETWORK", "ZLIB_CLI_ALLOW_PRIVATE_NETWORK"],
)
def test_previous_private_network_opt_ins_remain_compatible(monkeypatch, name):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_ANNA_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_CLI_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.setenv(name, "1")

    assert validate_http_url("http://127.0.0.1/book.pdf") == "http://127.0.0.1/book.pdf"


def test_safe_get_validates_redirect_before_following(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_ANNA_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_CLI_ALLOW_PRIVATE_NETWORK", raising=False)
    session = MagicMock()
    redirect = MagicMock()
    redirect.status_code = 302
    redirect.headers = {"location": "http://127.0.0.1/private"}
    redirect.url = "https://example.com/start"
    session.get.return_value = redirect

    with patch("zlib_anna.network_safety.socket.getaddrinfo", side_effect=public_dns):
        with pytest.raises(UnsafeUrlError):
            safe_get(session, "https://example.com/start")

    session.get.assert_called_once()
    redirect.close.assert_called_once()


def test_safe_get_allows_validated_public_redirect(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_ANNA_ALLOW_PRIVATE_NETWORK", raising=False)
    monkeypatch.delenv("ZLIB_CLI_ALLOW_PRIVATE_NETWORK", raising=False)
    session = MagicMock()
    redirect = MagicMock()
    redirect.status_code = 302
    redirect.headers = {"location": "https://cdn.example.org/book.pdf"}
    redirect.url = "https://example.com/start"
    final = MagicMock()
    final.status_code = 200
    final.headers = {"content-type": "application/pdf"}
    final.url = "https://cdn.example.org/book.pdf"
    session.get.side_effect = [redirect, final]

    with patch("zlib_anna.network_safety.socket.getaddrinfo", side_effect=public_dns):
        response = safe_get(session, "https://example.com/start")

    assert response is final
    assert session.get.call_count == 2


def test_safe_get_does_not_extend_fake_ip_trust_to_unknown_redirect(monkeypatch):
    monkeypatch.delenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", raising=False)
    session = MagicMock()
    redirect = MagicMock()
    redirect.status_code = 302
    redirect.headers = {"location": "https://unknown.example/file.pdf"}
    redirect.url = "https://annas-archive.gl/start"
    session.get.return_value = redirect

    def fake_dns(hostname, *_args, **_kwargs):
        address = "198.18.2.231" if hostname == "annas-archive.gl" else "198.18.2.232"
        return [(2, 1, 6, "", (address, 443))]

    with patch("zlib_anna.network_safety.socket.getaddrinfo", side_effect=fake_dns):
        with pytest.raises(UnsafeUrlError):
            safe_get(
                session,
                "https://annas-archive.gl/start",
                trusted_proxy_hosts={"annas-archive.gl"},
            )

    session.get.assert_called_once()
    redirect.close.assert_called_once()


def test_url_origin_removes_path_query_and_fragment():
    assert url_origin("https://files.example/private/token?key=secret#part") == (
        "https://files.example"
    )


@pytest.mark.parametrize(
    "domain",
    ["z-lib.is", "z-lib.id", "zlibrary.to"],
)
def test_known_fraudulent_zlib_domains_are_blocked(domain):
    assert zlib_domain_rejection_reason(domain) == "known_fraudulent_domain"


def test_known_fraudulent_zlib_subdomains_are_blocked():
    assert zlib_domain_rejection_reason("login.z-lib.id") == "known_fraudulent_domain"


def test_shared_hosting_zlib_discovery_domain_is_not_automatically_trusted():
    assert (
        zlib_domain_rejection_reason("proxy.zlibraryproxies.workers.dev") == "shared_hosting_domain"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://annas-archive.su",
        "https://annas-archive.io",
        "https://annas-archive.is",
    ],
)
def test_known_fraudulent_anna_domains_remain_blocked_with_opt_in(monkeypatch, url):
    monkeypatch.setenv("ANNAS_ALLOW_UNTRUSTED_DOMAIN", "1")

    with pytest.raises(UnsafeUrlError):
        validate_anna_base_url(url)


def test_known_fraudulent_anna_subdomains_remain_blocked_with_opt_in(monkeypatch):
    monkeypatch.setenv("ANNAS_ALLOW_UNTRUSTED_DOMAIN", "1")

    with pytest.raises(UnsafeUrlError):
        validate_anna_base_url("https://login.annas-archive.su")


def test_custom_anna_domain_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("ANNAS_ALLOW_UNTRUSTED_DOMAIN", raising=False)
    assert anna_base_url_policy("https://mirror.example")["reason"] == "untrusted_domain"

    monkeypatch.setenv("ANNAS_ALLOW_UNTRUSTED_DOMAIN", "1")
    policy = anna_base_url_policy("https://mirror.example")

    assert policy["allowed"] is True
    assert policy["trusted"] is False
    assert validate_anna_base_url("https://mirror.example") == "https://mirror.example"


def test_anna_base_url_rejects_paths_and_queries_even_with_opt_in(monkeypatch):
    monkeypatch.setenv("ANNAS_ALLOW_UNTRUSTED_DOMAIN", "1")

    policy = anna_base_url_policy("https://mirror.example/private?token=secret")

    assert policy["origin"] == "https://mirror.example"
    assert policy["allowed"] is False
    assert policy["reason"] == "base_url_must_be_origin"
