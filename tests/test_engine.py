"""Unit tests for the bundled Skill engine contract."""

import argparse
import hashlib
import json
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from zlib_anna import SCHEMA_VERSION, SKILL_VERSION, engine
from zlib_anna.credential_store import SystemKeychainCredentialStore

PDF_BODY = b"%PDF fake"
PDF_MD5 = hashlib.md5(PDF_BODY, usedforsecurity=False).hexdigest()
EPUB_BODY = b"epub bytes"
EPUB_MD5 = hashlib.md5(EPUB_BODY, usedforsecurity=False).hexdigest()


@pytest.fixture(autouse=True)
def allow_mock_network(monkeypatch):
    monkeypatch.setenv("ZLIB_SKILL_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setenv("ANNAS_ALLOW_UNTRUSTED_DOMAIN", "1")


class FakeResponse:
    def __init__(
        self,
        url,
        *,
        headers=None,
        body=b"",
        text=None,
        status_code=200,
    ):
        self.url = url
        self.headers = headers or {}
        self.body = body
        self._text = text
        self.status_code = status_code

    @property
    def text(self):
        if self._text is not None:
            return self._text
        return self.body.decode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise engine.requests.HTTPError(str(self.status_code), response=self)

    def iter_content(self, chunk_size=1024):
        yield self.body

    def close(self):
        pass


class MemoryKeychainBackend:
    def __init__(self):
        self.secret = None

    def capability(self):
        return True

    def read(self, _service, _account):
        return self.secret

    def write(self, _service, _account, secret):
        self.secret = bytes(secret)

    def delete(self, _service, _account):
        self.secret = None

    def delete_if_matches(self, service, account, expected):
        if self.secret != expected:
            raise RuntimeError("source changed")
        self.delete(service, account)


@pytest.fixture
def temp_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "engine"
        config_file = config_dir / "config.json"
        with (
            patch("zlib_anna.engine.CONFIG_DIR", config_dir),
            patch("zlib_anna.engine.CONFIG_FILE", config_file),
        ):
            yield config_dir, config_file


def test_load_config_empty(temp_config):
    assert engine.load_config() == {}


def test_default_config_dir_prefers_skill_named_override(monkeypatch, tmp_path):
    preferred = tmp_path / "preferred"
    previous = tmp_path / "previous"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("ZLIB_SKILL_CONFIG_DIR", str(preferred))
    monkeypatch.setenv("ZLIB_ANNA_CONFIG_DIR", str(previous))
    monkeypatch.setenv("ZLIB_CLI_CONFIG_DIR", str(legacy))

    assert engine.default_config_dir() == preferred


def test_default_config_dir_accepts_previous_override_aliases(monkeypatch, tmp_path):
    previous = tmp_path / "previous"
    legacy = tmp_path / "legacy"
    monkeypatch.delenv("ZLIB_SKILL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ZLIB_ANNA_CONFIG_DIR", str(previous))
    monkeypatch.setenv("ZLIB_CLI_CONFIG_DIR", str(legacy))

    assert engine.default_config_dir() == previous

    monkeypatch.delenv("ZLIB_ANNA_CONFIG_DIR", raising=False)

    assert engine.default_config_dir() == legacy


def test_engine_config_store_resolves_environment_path_after_import(monkeypatch, tmp_path):
    runtime_config = tmp_path / "runtime-config"
    monkeypatch.setattr(engine, "CONFIG_DIR", None)
    monkeypatch.setattr(engine, "CONFIG_FILE", None)
    monkeypatch.setenv("ZLIB_SKILL_CONFIG_DIR", str(runtime_config))

    engine.save_config({"domain": "z-library.example"})

    assert engine.load_config() == {"domain": "z-library.example"}
    assert (runtime_config / "config.json").is_file()


def test_safe_json_response_rejects_non_object_payload():
    response = MagicMock()
    response.json.return_value = ["unexpected", "payload"]

    assert engine.safe_json_response(response) is None


def test_load_config_rejects_non_object_json(temp_config):
    config_dir, config_file = temp_config
    config_dir.mkdir(parents=True)
    config_file.write_text("[]", encoding="utf-8")

    with pytest.raises(engine.SkillError) as exc:
        engine.load_config()

    assert exc.value.code == "CONFIG_INVALID"
    assert engine.load_config(strict=False) == {}


def test_cli_maps_config_io_failure_to_stable_error_envelope(temp_config, capsys):
    _, config_file = temp_config
    config_file.mkdir(parents=True)

    exit_code = engine.main(["auth", "status", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_IO_ERROR"


def test_save_config_uses_private_permissions(temp_config):
    config_dir, config_file = temp_config
    cfg = {"remix_userid": "123", "remix_userkey": "token-value", "domain": "test.example"}

    engine.save_config(cfg)

    assert engine.load_config() == cfg
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_save_config_rejects_partial_credentials_without_replacing_existing_state(
    temp_config,
):
    engine.save_config({"domain": "z-library.example"})

    with pytest.raises(engine.SkillError) as raised:
        engine.save_config({"remix_userid": "42", "domain": "replacement.example"})

    assert raised.value.code == "CREDENTIAL_INVALID"
    assert engine.load_config() == {"domain": "z-library.example"}


def test_load_config_repairs_legacy_permissions(temp_config):
    config_dir, config_file = temp_config
    config_dir.mkdir(parents=True)
    config_file.write_text(
        json.dumps({"remix_userid": "123", "remix_userkey": "token-value"}),
        encoding="utf-8",
    )
    config_dir.chmod(0o755)
    config_file.chmod(0o644)

    cfg = engine.load_config()

    assert cfg["remix_userkey"] == "token-value"
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_config_status_reports_permission_repairs(temp_config):
    config_dir, config_file = temp_config
    config_dir.mkdir(parents=True)
    config_file.write_text(
        json.dumps({"remix_userid": "123", "remix_userkey": "token-value"}),
        encoding="utf-8",
    )
    config_dir.chmod(0o755)
    config_file.chmod(0o644)

    status = engine.config_status()

    assert status["config_dir_mode"] == "0o700"
    assert status["config_file_mode"] == "0o600"
    assert {item["kind"] for item in status["permission_repairs"]} == {
        "config_dir",
        "config_file",
    }


def test_config_status_redacts_sensitive_values(temp_config):
    _, config_file = temp_config
    engine.save_config(
        {
            "remix_userid": "42",
            "remix_userkey": "token-value",
            "domain": "z-library.example",
            "email": "secret@example.com",
            "name": "Test User",
        }
    )

    status = engine.config_status()
    encoded = json.dumps(status)

    assert status["zlib"]["has_token"] is True
    assert status["zlib"]["email"] == "s****t@example.com"
    assert "token-value" not in encoded
    assert str(config_file) in encoded


def test_auth_status_reports_selected_credential_adapter_without_secrets(temp_config, capsys):
    engine.save_config(
        {
            "remix_userid": "42",
            "remix_userkey": "token-value",
            "email": "reader@example.com",
        }
    )

    with patch("zlib_anna.credential_store.sys.platform", "linux"):
        exit_code = engine.main(["auth", "status", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["credential_storage"] == {
        "selected": "file",
        "has_credential": True,
        "adapters": {
            "file": {"capability": True, "has_credential": True},
            "system-keychain": {"capability": False, "has_credential": None},
        },
        "migration_phase": None,
    }
    assert payload["zlib"]["email"] == "r****r@example.com"
    assert "token-value" not in json.dumps(payload)


def test_auth_storage_file_is_an_idempotent_public_cli_operation(temp_config, capsys):
    engine.save_config(
        {
            "remix_userid": "42",
            "remix_userkey": "token-value",
            "email": "reader@example.com",
        }
    )

    keychain = SystemKeychainCredentialStore(backend=MemoryKeychainBackend())
    with patch(
        "zlib_anna.credential_store.SystemKeychainCredentialStore",
        return_value=keychain,
    ):
        exit_code = engine.main(["auth", "storage", "file", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["source"] == "zlib"
    assert payload["storage"] == "file"
    assert payload["migrated"] is False
    assert payload["authenticated"] is True
    assert "token-value" not in captured.out
    assert captured.err == ""


def test_auth_logout_clears_selected_system_keychain_without_plaintext_fallback(
    temp_config, capsys
):
    backend = MemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)

    with patch(
        "zlib_anna.credential_store.SystemKeychainCredentialStore",
        return_value=keychain,
    ):
        engine.save_config(
            {
                "remix_userid": "42",
                "remix_userkey": "token-value",
            }
        )
        assert engine.main(["auth", "storage", "system-keychain", "--json"]) == 0
        capsys.readouterr()
        exit_code = engine.main(["auth", "logout", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["authenticated"] is False
    assert backend.secret is None
    assert "token-value" not in temp_config[1].read_text(encoding="utf-8")
    assert "token-value" not in captured.out


def test_auth_storage_cli_migrates_to_keychain_and_back_without_secret_output(temp_config, capsys):
    backend = MemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)

    with patch(
        "zlib_anna.credential_store.SystemKeychainCredentialStore",
        return_value=keychain,
    ):
        engine.save_config(
            {
                "remix_userid": "42",
                "remix_userkey": "token-value",
            }
        )
        to_keychain = engine.main(["auth", "storage", "system-keychain", "--json"])
        keychain_payload = json.loads(capsys.readouterr().out)
        persisted_in_keychain_mode = temp_config[1].read_text(encoding="utf-8")
        to_file = engine.main(["auth", "storage", "file", "--json"])
        file_payload = json.loads(capsys.readouterr().out)

    assert to_keychain == 0
    assert keychain_payload["storage"] == "system-keychain"
    assert "token-value" not in json.dumps(keychain_payload)
    assert "token-value" not in persisted_in_keychain_mode
    assert to_file == 0
    assert file_payload["storage"] == "file"
    assert "token-value" not in json.dumps(file_payload)
    assert backend.secret is None
    assert engine.load_config()["remix_userkey"] == "token-value"


def test_config_status_reports_zlib_domain_override(temp_config):
    with patch.dict(engine.os.environ, {"ZLIBRARY_DOMAIN": "https://env.example/path"}):
        status = engine.config_status()

    assert status["zlib"]["domain_env"] == "env.example"


def test_config_status_lists_official_anna_fallback_origins(temp_config):
    with patch(
        "zlib_anna.engine.anna_base_urls",
        return_value=["https://annas-archive.gl", "https://annas-archive.pk/path"],
    ):
        status = engine.config_status()

    assert status["anna"]["candidate_origins"] == [
        "https://annas-archive.gl",
        "https://annas-archive.pk",
    ]


def test_config_status_warns_and_ignores_known_fraudulent_anna_domain(temp_config, monkeypatch):
    monkeypatch.setenv("ANNAS_BASE_URL", "https://annas-archive.is")

    status = engine.config_status()

    assert status["anna"]["configured_origin"] == "https://annas-archive.is"
    assert status["anna"]["configured_trusted"] is False
    assert status["anna"]["configuration_warning"] == "known_fraudulent_domain"
    assert "https://annas-archive.is" not in status["anna"]["candidate_origins"]


def test_mask_email_handles_empty_local_part():
    assert engine.mask_email("@example.com") == "*@example.com"


def test_fetch_domains_filters_unusable_domains():
    with patch("zlib_anna.engine.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "domains": [
                {"domain": "z-library.example", "contentAvailable": True, "isRedirector": False},
                {"domain": "redirect.example", "contentAvailable": True, "isRedirector": True},
                {"domain": "offline.example", "contentAvailable": False, "isRedirector": False},
                {"domain": "z-lib.is", "contentAvailable": True, "isRedirector": False},
                {
                    "domain": "proxy.example.workers.dev",
                    "contentAvailable": True,
                    "isRedirector": False,
                },
            ],
        }
        mock_get.return_value = mock_resp

        domains = engine.fetch_domains()

    assert domains == ["z-library.example"]


def test_fetch_domains_unions_all_discovery_entry_points():
    first = MagicMock(status_code=200)
    first.json.return_value = {
        "success": True,
        "domains": [
            {"domain": "one.example", "contentAvailable": True, "isRedirector": False},
            {"domain": "shared.example", "contentAvailable": True, "isRedirector": False},
        ],
    }
    second = MagicMock(status_code=200)
    second.json.return_value = {
        "success": True,
        "domains": [
            {"domain": "shared.example", "contentAvailable": True, "isRedirector": False},
            {"domain": "two.example", "contentAvailable": True, "isRedirector": False},
        ],
    }

    with (
        patch("zlib_anna.engine.ENTRY_POINTS", ["https://one.example", "https://two.example"]),
        patch("zlib_anna.engine.requests.get", side_effect=[first, second]),
    ):
        domains = engine.fetch_domains()

    assert domains == ["one.example", "shared.example", "two.example"]


def test_test_domain_handles_request_errors():
    with patch(
        "zlib_anna.engine.requests.get",
        side_effect=engine.requests.RequestException("network down"),
    ):
        assert engine.test_domain("bad.example") is False


def test_find_working_domain_prefers_env_override():
    with (
        patch.dict(
            engine.os.environ,
            {
                "ZLIBRARY_DOMAIN": "https://env.example/path",
                "ZLIBRARY_ALLOW_UNTRUSTED_DOMAIN": "1",
            },
        ),
        patch(
            "zlib_anna.engine.test_domain",
            return_value=True,
        ) as mock_test_domain,
    ):
        domain, checks = engine.find_working_domain("config.example")

    assert domain == "env.example"
    assert checks == [
        {
            "domain": "env.example",
            "available": True,
            "source": "env",
            "trusted": True,
            "trust_basis": "explicit_opt_in",
        }
    ]
    mock_test_domain.assert_called_once_with("env.example")
    assert engine.domain_trust_is_persistent(domain, checks) is False


def test_find_working_domain_does_not_contact_untrusted_override(monkeypatch):
    monkeypatch.setenv("ZLIBRARY_DOMAIN", "untrusted.example")
    monkeypatch.delenv("ZLIBRARY_ALLOW_UNTRUSTED_DOMAIN", raising=False)

    with (
        patch("zlib_anna.engine.fetch_domains", return_value=[]),
        patch(
            "zlib_anna.engine.test_domain",
            return_value=True,
        ) as mock_test_domain,
    ):
        domain, checks = engine.find_working_domain()

    assert domain == "z-library.sk"
    assert checks[0]["domain"] == "untrusted.example"
    assert checks[0]["reason"] == "untrusted_domain"
    assert "untrusted.example" not in {call.args[0] for call in mock_test_domain.call_args_list}


def test_find_working_domain_reuses_discovery_cache():
    discovery_cache = {}
    with (
        patch(
            "zlib_anna.engine.fetch_domains", return_value=["one.example", "two.example"]
        ) as fetch,
        patch("zlib_anna.engine.test_domain", return_value=False),
    ):
        engine.find_working_domain(discovery_cache=discovery_cache)
        engine.find_working_domain(discovery_cache=discovery_cache)

    assert discovery_cache == {"domains": ["one.example", "two.example"]}
    fetch.assert_called_once_with()


def test_credential_domain_requires_built_in_and_live_registry_confirmation(monkeypatch):
    monkeypatch.delenv("ZLIBRARY_DOMAIN", raising=False)
    monkeypatch.delenv("ZLIB_DOMAIN", raising=False)
    with (
        patch(
            "zlib_anna.engine.fetch_domains",
            return_value=["dynamic.example", "z-library.sk"],
        ),
        patch("zlib_anna.engine.test_domain", return_value=True) as probe,
    ):
        domain, checks = engine.find_working_domain(
            "dynamic.example",
            preferred_trusted=True,
            for_credentials=True,
        )

    assert domain == "z-library.sk"
    assert any(
        item.get("domain") == "dynamic.example" and item.get("reason") == "not_credential_trusted"
        for item in checks
    )
    assert checks[-1]["trust_basis"] == "registry_allowlist"
    assert checks[-1]["credential_trusted"] is True
    assert {call.args[0] for call in probe.call_args_list} == {"z-library.sk"}
    assert engine.domain_trust_is_persistent(domain, checks) is True


def test_known_fraudulent_zlib_override_is_never_contacted(monkeypatch):
    monkeypatch.setenv("ZLIBRARY_DOMAIN", "z-lib.is")
    monkeypatch.setenv("ZLIBRARY_ALLOW_UNTRUSTED_DOMAIN", "1")

    with (
        patch("zlib_anna.engine.fetch_domains", return_value=[]),
        patch("zlib_anna.engine.test_domain", return_value=False) as probe,
    ):
        _, checks = engine.find_working_domain(for_credentials=True)

    assert checks[0]["domain"] == "z-lib.is"
    assert checks[0]["reason"] == "known_fraudulent_domain"
    assert "z-lib.is" not in {call.args[0] for call in probe.call_args_list}


def test_download_dir_status_reports_creatable_when_home_is_missing(tmp_path):
    missing_home = tmp_path / "missing-home"
    download_dir = missing_home / "Books"

    status = engine.download_dir_status(download_dir)

    assert status["exists"] is False
    assert status["parent_exists"] is False
    assert status["nearest_existing_parent"] == str(tmp_path)
    assert status["creatable"] is True


def test_parser_supports_json_and_source_choices():
    parser = engine.build_parser()

    args = parser.parse_args(["search", "clean code", "--source", "all", "--json"])

    assert args.command == "search"
    assert args.source == "all"
    assert args.json is True


def test_parser_help_includes_first_run_examples():
    help_text = engine.build_parser().format_help()

    assert "Examples:" in help_text
    assert "ZLIBRARY_DOMAIN" in help_text
    assert "{baseDir}/scripts/run.py" in help_text
    assert "zlib-cli" not in help_text
    assert "==SUPPRESS==" not in help_text


def test_parser_rejects_non_positive_limits():
    parser = engine.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["search", "python", "--limit", "0"])


def test_main_emits_json_for_argument_errors(capsys):
    exit_code = engine.main(["search", "python", "--limit", "0", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out)["error"]["code"] == "INVALID_ARGUMENT"
    assert "usage:" in captured.err


def test_skill_version_has_single_source():
    assert engine.ok_payload()["skill_version"] == SKILL_VERSION


@pytest.mark.parametrize(
    ("value", "source", "hash_id", "expected"),
    [
        ("zlib:123:abc", "auto", None, ("zlib", "123", "abc")),
        (
            "anna:deadbeefdeadbeefdeadbeefdeadbeef",
            "auto",
            None,
            ("anna", "deadbeefdeadbeefdeadbeefdeadbeef", None),
        ),
        ("123", "zlib", "abc", ("zlib", "123", "abc")),
        (
            "deadbeefdeadbeefdeadbeefdeadbeef",
            "anna",
            None,
            ("anna", "deadbeefdeadbeefdeadbeefdeadbeef", None),
        ),
    ],
)
def test_parse_result_ref(value, source, hash_id, expected):
    assert engine.parse_result_ref(value, source, hash_id) == expected


@pytest.mark.parametrize(
    "value",
    [
        "anna:deadbeef",
        "anna:../../etc/passwd",
        "zlib:not-a-number:hash",
        "zlib:123:hash/with/slash",
    ],
)
def test_parse_result_ref_rejects_malformed_ids(value):
    with pytest.raises(engine.SkillError) as exc:
        engine.parse_result_ref(value)

    assert exc.value.code == "INVALID_RESULT_ID"


def test_search_all_includes_anonymous_zlib_without_auth(temp_config):
    args = argparse.Namespace(
        query="python",
        source="all",
        limit=10,
        page=1,
        year_from=None,
        year_to=None,
        lang=None,
        ext=None,
        order=None,
        json=False,
    )
    anna_book = {
        "result_id": "anna:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "source": "anna",
        "md5": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "title": "Python Book",
        "author": "A. Author",
        "year": "2024",
        "extension": "PDF",
        "size": "1MB",
    }
    anna_status = engine.SourceStatus(
        source="anna",
        available=True,
        authenticated=False,
        can_search=True,
        can_download=True,
        status="ok",
    )
    zlib_book = {
        "result_id": "zlib:123:abc",
        "source": "zlib",
        "id": "123",
        "hash": "abc",
        "title": "Anonymous Z-Library result",
    }
    zlib_status = engine.SourceStatus(
        source="zlib",
        available=True,
        authenticated=False,
        can_search=True,
        can_download=False,
        status="ok",
        details={"search_mode": "anonymous"},
    )

    with (
        patch("zlib_anna.engine.search_zlib", return_value=([zlib_book], zlib_status)),
        patch("zlib_anna.engine.search_anna", return_value=([anna_book], anna_status)),
    ):
        payload = engine.cmd_search(args)

    assert payload["ok"] is True
    assert payload["results"] == [zlib_book, anna_book]
    assert payload["sources"][0]["source"] == "zlib"
    assert payload["sources"][0]["status"] == "ok"
    assert payload["sources"][0]["authenticated"] is False
    assert payload["sources"][0]["can_search"] is True


def test_search_all_continues_when_zlib_source_errors(temp_config):
    args = argparse.Namespace(
        query="python",
        source="all",
        limit=10,
        page=1,
        year_from=None,
        year_to=None,
        lang=None,
        ext=None,
        order=None,
        json=False,
    )
    anna_book = {
        "result_id": "anna:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "source": "anna",
    }
    anna_status = engine.SourceStatus(
        source="anna",
        available=True,
        can_search=True,
        can_attempt_download=True,
        status="ok",
    )

    with (
        patch(
            "zlib_anna.engine.search_zlib",
            side_effect=engine.SkillError("SOURCE_UNAVAILABLE", "blocked"),
        ),
        patch("zlib_anna.engine.search_anna", return_value=([anna_book], anna_status)),
    ):
        payload = engine.cmd_search(args)

    assert payload["ok"] is True
    assert payload["results"] == [anna_book]
    assert payload["sources"][0]["source"] == "zlib"
    assert payload["sources"][0]["status"] == "error"


def test_search_zlib_without_auth_uses_anonymous_search(temp_config):
    args = argparse.Namespace(
        query="python",
        source="zlib",
        limit=10,
        page=1,
        year_from=None,
        year_to=None,
        lang=None,
        ext=None,
        order=None,
    )
    client = MagicMock()
    client.getDomain.return_value = "z-library.sk"
    client.isLoggedIn.return_value = False
    client.search.return_value = {
        "success": 1,
        "books": [{"id": "123", "hash": "abc", "title": "Anonymous result"}],
    }

    with (
        patch("zlib_anna.engine.find_working_domain", return_value=("z-library.sk", [])),
        patch("zlib_anna.engine.init_zlibrary", return_value=client) as mock_init,
    ):
        books, status = engine.search_zlib(args, {})

    assert [book["title"] for book in books] == ["Anonymous result"]
    assert books[0]["requires_account"] is True
    assert books[0]["can_download"] is False
    assert status.available is True
    assert status.authenticated is False
    assert status.can_search is True
    assert status.can_download is False
    mock_init.assert_called_once_with(
        {}, require_auth=False, update_config=False, resolved_domain="z-library.sk"
    )


def test_anonymous_zlib_client_never_loads_saved_credentials(temp_config):
    cfg = {
        "remix_userid": "42",
        "remix_userkey": "saved-secret",
        "domain": "z-library.sk",
        "domain_trusted": True,
    }
    client = MagicMock()
    client.isLoggedIn.return_value = False

    with patch("zlib_anna.zlibrary.Zlibrary", return_value=client) as constructor:
        initialized = engine.init_zlibrary(
            cfg,
            require_auth=False,
            update_config=False,
            resolved_domain="z-library.sk",
        )

    assert initialized is client
    constructor.assert_called_once_with()
    client.setDomain.assert_called_once_with("z-library.sk")


def test_authenticated_zlib_client_cannot_bypass_domain_resolution(temp_config):
    cfg = {"remix_userid": "42", "remix_userkey": "saved-secret"}

    with pytest.raises(engine.SkillError) as raised:
        engine.init_zlibrary(
            cfg,
            require_auth=True,
            update_config=False,
            resolved_domain="dynamic.example",
        )

    assert raised.value.code == "UNTRUSTED_DOMAIN"


def test_search_zlib_switches_domain_when_search_request_fails(temp_config):
    args = argparse.Namespace(
        query="python",
        source="zlib",
        limit=10,
        page=1,
        year_from=None,
        year_to=None,
        lang=None,
        ext=None,
        order=None,
    )
    first = MagicMock()
    first.getDomain.return_value = "one.example"
    first.search.side_effect = engine.requests.ConnectionError("mirror failed")
    second = MagicMock()
    second.getDomain.return_value = "two.example"
    second.isLoggedIn.return_value = False
    second.search.return_value = {"success": 1, "books": []}

    with (
        patch(
            "zlib_anna.engine.find_working_domain",
            side_effect=[
                ("one.example", [{"domain": "one.example", "available": True}]),
                ("two.example", [{"domain": "two.example", "available": True}]),
            ],
        ) as mock_find,
        patch("zlib_anna.engine.init_zlibrary", side_effect=[first, second]),
    ):
        books, status = engine.search_zlib(args, {})

    assert books == []
    assert status.details["domain"] == "two.example"
    assert status.details["failed_domains"] == ["one.example"]
    assert mock_find.call_args_list[1].kwargs["excluded"] == {"one.example"}


def test_normalize_anna_book_marks_download_as_best_effort():
    book = {
        "md5": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "title": "Example Book",
        "ext": "PDF",
        "detail_url": "https://annas.example/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }

    result = engine.normalize_anna_book(book)

    assert result["can_download"] is False
    assert result["can_attempt_download"] is True
    assert result["download_guaranteed"] is False
    assert result["download_strategy"] == "html_best_effort"
    assert result["best_effort"] is True


def test_anna_filters_year_and_language_locally():
    args = argparse.Namespace(
        query="python",
        source="anna",
        limit=10,
        page=1,
        year_from=2020,
        year_to=2025,
        lang="en",
        ext=None,
        order="popular",
    )
    client = MagicMock()
    client.search.return_value = [
        {
            "md5": "a" * 32,
            "title": "Keep",
            "language": "English [en]",
            "year": "2022",
        },
        {
            "md5": "b" * 32,
            "title": "Wrong language",
            "language": "Chinese [zh]",
            "year": "2022",
        },
        {
            "md5": "c" * 32,
            "title": "Too old",
            "language": "English [en]",
            "year": "2010",
        },
    ]

    with patch("zlib_anna.engine.annas_archive.AnnasArchiveClient", return_value=client):
        books, status = engine.search_anna(args)

    assert [book["title"] for book in books] == ["Keep"]
    assert status.can_download is False
    assert status.can_attempt_download is True
    assert status.details["ignored_filters"] == ["order"]


def test_search_anna_switches_to_next_official_base_url():
    args = argparse.Namespace(
        query="python",
        source="anna",
        limit=10,
        page=1,
        year_from=None,
        year_to=None,
        lang=None,
        ext=None,
        order=None,
    )
    failed = MagicMock()
    failed.search.side_effect = engine.requests.ConnectionError("first mirror failed")
    working = MagicMock()
    working.search.return_value = [{"md5": "a" * 32, "title": "Fallback result"}]

    with (
        patch(
            "zlib_anna.engine.anna_base_urls",
            return_value=["https://annas-archive.gl", "https://annas-archive.pk"],
        ),
        patch(
            "zlib_anna.engine.annas_archive.AnnasArchiveClient",
            side_effect=[failed, working],
        ) as factory,
    ):
        books, status = engine.search_anna(args)

    assert [book["title"] for book in books] == ["Fallback result"]
    assert status.details["base_origin"] == "https://annas-archive.pk"
    assert status.details["failed_origins"] == ["https://annas-archive.gl"]
    assert [call.kwargs["base_url"] for call in factory.call_args_list] == [
        "https://annas-archive.gl",
        "https://annas-archive.pk",
    ]


def test_anna_links_switches_base_url_after_failure():
    failed = MagicMock()
    failed.get_download_links.side_effect = engine.requests.ConnectionError("mirror failed")
    working = MagicMock()
    working.get_download_links.return_value = {"detail_url": "https://annas-archive.pk/md5/x"}

    with (
        patch(
            "zlib_anna.engine.anna_base_urls",
            return_value=["https://annas-archive.gl", "https://annas-archive.pk"],
        ),
        patch(
            "zlib_anna.engine.annas_archive.AnnasArchiveClient",
            side_effect=[failed, working],
        ),
    ):
        links = engine.anna_links("a" * 32)

    assert links["detail_url"].startswith("https://annas-archive.pk/")


def test_check_zlib_reports_anonymous_search_without_authentication():
    with patch(
        "zlib_anna.engine.find_working_domain",
        return_value=("z-library.sk", [{"domain": "z-library.sk", "available": True}]),
    ):
        status = engine.check_zlib({})

    assert status.available is True
    assert status.authenticated is False
    assert status.can_search is True
    assert status.can_download is False
    assert status.status == "ok"
    assert status.details["search_mode"] == "anonymous"


def test_check_anna_probes_search_capability_and_reports_access_block(monkeypatch):
    response = FakeResponse(
        "https://annas-archive.gl/search?q=zlib-skill-health-check",
        headers={"content-type": "text/html"},
        status_code=403,
    )
    monkeypatch.setattr(
        engine,
        "anna_base_urls",
        lambda: ["https://annas-archive.gl"],
    )

    with patch("zlib_anna.engine.safe_get", return_value=response) as request:
        status = engine.check_anna(budget=engine.operation_budget(seconds=5))

    assert status.available is False
    assert status.can_search is False
    assert status.status == "blocked"
    assert status.details["website_reachable"] is True
    assert status.details["capability_probe"] == "search"
    assert request.call_args.args[1].endswith("/search?q=zlib-skill-health-check")


def test_search_anna_sanitizes_upstream_failure(monkeypatch):
    args = argparse.Namespace(
        query="private query",
        source="all",
        limit=10,
        page=1,
        year_from=None,
        year_to=None,
        lang=None,
        ext=None,
        order=None,
    )
    client = MagicMock()
    client.search.side_effect = engine.requests.ConnectionError(
        "request failed for https://annas.example/private?token=secret"
    )
    monkeypatch.setenv("ANNAS_BASE_URL", "https://annas.example/private?token=secret")

    with patch("zlib_anna.engine.annas_archive.AnnasArchiveClient", return_value=client):
        books, status = engine.search_anna(args)

    assert books == []
    assert status.message == "Anna's Archive request failed."
    assert status.details == {
        "base_origin": "https://annas-archive.gl",
        "error_type": "ConnectionError",
        "failed_origins": [
            "https://annas-archive.gl",
            "https://annas-archive.pk",
            "https://annas-archive.gd",
        ],
    }
    assert "token" not in json.dumps(status.to_dict())


def test_main_json_error_is_machine_readable(capsys):
    exit_code = engine.main(["download", "unknown-id", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["skill_version"] == SKILL_VERSION
    assert "cli_version" not in payload
    assert payload["error"]["code"] == "SOURCE_REQUIRED"
    assert captured.err == ""


def test_download_anna_direct_file_response(tmp_path):
    args = argparse.Namespace(output=str(tmp_path))
    fake_session = MagicMock()
    fake_session.get.return_value = FakeResponse(
        "https://files.example/book.pdf",
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="Book.pdf"',
        },
        body=PDF_BODY,
    )

    with (
        patch(
            "zlib_anna.engine.anna_links",
            return_value={
                "detail_url": f"https://annas.example/md5/{PDF_MD5}",
                "libgen_li": "https://files.example/book.pdf",
                "libgen_is": None,
                "libgen_rs": None,
                "fast_downloads": [],
            },
        ),
        patch("zlib_anna.engine.requests.Session", return_value=fake_session),
    ):
        payload = engine.download_anna(args, PDF_MD5)

    assert payload["ok"] is True
    assert payload["downloaded"] is True
    assert payload["source"] == "anna"
    assert payload["path"].endswith("Book.pdf")
    assert Path(payload["path"]).read_bytes() == PDF_BODY


def test_download_anna_follows_html_download_link(tmp_path):
    args = argparse.Namespace(output=str(tmp_path))
    fake_session = MagicMock()
    fake_session.get.side_effect = [
        FakeResponse(
            "https://libgen.example/book",
            headers={"content-type": "text/html; charset=utf-8"},
            text=f'<html><a href="/get.php?md5={EPUB_MD5}">GET</a></html>',
        ),
        FakeResponse(
            f"https://libgen.example/get.php?md5={EPUB_MD5}",
            headers={"content-type": "application/epub+zip"},
            body=EPUB_BODY,
        ),
    ]

    with (
        patch(
            "zlib_anna.engine.anna_links",
            return_value={
                "detail_url": f"https://annas.example/md5/{EPUB_MD5}",
                "libgen_li": "https://libgen.example/book",
                "libgen_is": None,
                "libgen_rs": None,
                "fast_downloads": [],
            },
        ),
        patch("zlib_anna.engine.requests.Session", return_value=fake_session),
    ):
        payload = engine.download_anna(args, EPUB_MD5)

    assert payload["downloaded"] is True
    assert payload["final_origin"] == "https://libgen.example"
    assert Path(payload["path"]).name == f"anna-{EPUB_MD5}.epub"
    assert Path(payload["path"]).read_bytes() == EPUB_BODY


def test_download_anna_reports_links_when_no_candidate_downloads(tmp_path):
    args = argparse.Namespace(output=str(tmp_path))
    fake_session = MagicMock()
    fake_session.get.return_value = FakeResponse(
        "https://libgen.example/book",
        headers={"content-type": "text/html"},
        text="<html><p>captcha required</p></html>",
    )
    links = {
        "detail_url": "https://annas.example/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "libgen_li": "https://libgen.example/book",
        "libgen_is": None,
        "libgen_rs": None,
        "fast_downloads": [],
    }

    with (
        patch("zlib_anna.engine.anna_links", return_value=links),
        patch(
            "zlib_anna.engine.requests.Session",
            return_value=fake_session,
        ),
    ):
        with pytest.raises(engine.SkillError) as exc:
            engine.download_anna(args, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    assert exc.value.code == "DOWNLOAD_FAILED"
    assert exc.value.details["attempts"][0]["kind"] == "libgen_li"
    assert exc.value.details["available_link_kinds"] == ["libgen_li"]


def test_download_anna_does_not_expose_resolved_link_map_when_empty(tmp_path):
    args = argparse.Namespace(output=str(tmp_path), max_size_mb=1)
    detail_url = "https://annas.example/md5/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    with patch(
        "zlib_anna.engine.anna_links",
        return_value={
            "detail_url": detail_url,
            "libgen_li": None,
            "libgen_is": None,
            "libgen_rs": None,
            "fast_downloads": [],
        },
    ):
        with pytest.raises(engine.SkillError) as exc:
            engine.download_anna(args, "a" * 32)

    assert exc.value.code == "DOWNLOAD_LINKS_NOT_FOUND"
    assert exc.value.details == {
        "detail_url": detail_url,
        "available_link_kinds": [],
    }


def test_download_anna_rejects_checksum_mismatch_and_removes_partial(tmp_path):
    args = argparse.Namespace(output=str(tmp_path), max_size_mb=1)
    fake_session = MagicMock()
    fake_session.get.return_value = FakeResponse(
        "https://files.example/book.pdf",
        headers={"content-type": "application/pdf"},
        body=PDF_BODY,
    )
    wrong_md5 = "0" * 32

    with (
        patch(
            "zlib_anna.engine.anna_links",
            return_value={
                "detail_url": f"https://annas.example/md5/{wrong_md5}",
                "libgen_li": "https://files.example/book.pdf",
                "libgen_is": None,
                "libgen_rs": None,
                "fast_downloads": [],
            },
        ),
        patch("zlib_anna.engine.requests.Session", return_value=fake_session),
    ):
        with pytest.raises(engine.SkillError) as exc:
            engine.download_anna(args, wrong_md5)

    assert exc.value.code == "DOWNLOAD_FAILED"
    assert list(tmp_path.iterdir()) == []


def test_write_response_rejects_declared_file_over_size_limit(tmp_path):
    response = FakeResponse(
        "https://files.example/book.pdf",
        headers={"content-type": "application/pdf", "content-length": "100"},
        body=PDF_BODY,
    )

    with pytest.raises(ValueError, match="size limit"):
        engine.write_response_to_path(response, tmp_path / "book.part", max_bytes=10)


def test_filename_from_response_replaces_executable_extension():
    response = FakeResponse(
        "https://files.example/download",
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="book.exe"',
        },
    )

    assert engine.filename_from_response(response, "fallback") == "book.pdf"


def test_main_converts_unexpected_exception_to_safe_json(capsys):
    result_id = "anna:" + "a" * 32

    with patch("zlib_anna.engine.cmd_download", side_effect=RuntimeError("secret-url-token")):
        exit_code = engine.main(["download", result_id, "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["error"]["code"] == "UNEXPECTED_ERROR"
    assert payload["error"]["details"]["error_type"] == "RuntimeError"
    assert "secret-url-token" not in json.dumps(payload)


def test_imports_are_available():
    from zlib_anna import annas_archive
    from zlib_anna.zlibrary import Zlibrary

    assert hasattr(engine, "main")
    assert hasattr(engine, "build_parser")
    assert hasattr(annas_archive, "AnnasArchiveClient")
    assert hasattr(Zlibrary(), "setDomain")
