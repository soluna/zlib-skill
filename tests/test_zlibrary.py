"""
Tests for Zlibrary.py — domain switching and API wrapper basics.

All tests are unit tests (no network calls).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zlib_anna.zlibrary import Zlibrary


class TestZlibraryDomain:
    """Domain switching (Hermes patch)."""

    def test_default_domain(self):
        """Default domain should be 1lib.sk (upstream default)."""
        z = Zlibrary()
        assert z.getDomain() == "1lib.sk"

    def test_set_domain(self):
        """setDomain should update the domain."""
        z = Zlibrary()
        z.setDomain("z-library.sk")
        assert z.getDomain() == "z-library.sk"

    def test_switch_back(self):
        """Domain switching should be reversible."""
        z = Zlibrary()
        original = z.getDomain()
        z.setDomain("test.example.com")
        assert z.getDomain() == "test.example.com"
        z.setDomain(original)
        assert z.getDomain() == original

    def test_multiple_switches(self):
        """Multiple domain switches should all work."""
        z = Zlibrary()
        domains = ["a.example.com", "b.example.com", "c.example.com"]
        for d in domains:
            z.setDomain(d)
            assert z.getDomain() == d

    @pytest.mark.parametrize(
        "domain",
        [
            "https://z-library.sk",
            "z-library.sk/path",
            "127.0.0.1",
            "user@example.com",
        ],
    )
    def test_set_domain_rejects_unsafe_or_malformed_values(self, domain):
        with pytest.raises(ValueError):
            Zlibrary().setDomain(domain)

    @pytest.mark.parametrize(
        "domain",
        ["z-lib.is", "z-lib.id", "zlibrary.to", "proxy.example.workers.dev"],
    )
    def test_set_domain_rejects_service_policy_blocks(self, domain):
        with pytest.raises(ValueError, match="source policy"):
            Zlibrary().setDomain(domain)


class TestZlibraryInit:
    """Constructor behavior."""

    def test_not_logged_in_initially(self):
        """New instance should not be logged in."""
        z = Zlibrary()
        assert z.isLoggedIn() is False

    def test_init_without_args(self):
        """Constructor should work without any arguments."""
        z = Zlibrary()
        assert z is not None

    def test_init_is_idempotent(self):
        """Multiple instances should be independent."""
        z1 = Zlibrary()
        z2 = Zlibrary()
        z1.setDomain("a.example.com")
        assert z2.getDomain() == "1lib.sk"  # z2 unaffected
        assert z1.getDomain() == "a.example.com"


def test_streaming_download_rejects_declared_oversize_before_writing(tmp_path):
    z = Zlibrary()
    response = MagicMock()
    response.headers = {"content-length": "100"}
    response.raise_for_status = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = response
    context.__exit__.return_value = False
    output = tmp_path / "book.part"

    with (
        patch.object(
            z,
            "_Zlibrary__getBookFileInfo",
            return_value=("book.pdf", "https://files.example/book.pdf"),
        ),
        patch("zlib_anna.zlibrary.safe_get", return_value=context),
    ):
        with pytest.raises(ValueError, match="size limit"):
            z.downloadUrlToPath("https://files.example/book.pdf", output, max_bytes=10)

    assert not output.exists()


def test_search_is_available_without_login():
    z = Zlibrary()
    response = MagicMock()
    response.is_redirect = False
    response.raise_for_status = MagicMock()
    response.json.return_value = {"success": 1, "books": [{"id": "123", "title": "Book"}]}

    with patch.object(z._Zlibrary__session, "post", return_value=response) as mock_post:
        result = z.search(message="python", limit=5)

    assert result["success"] == 1
    assert result["books"][0]["title"] == "Book"
    mock_post.assert_called_once()
