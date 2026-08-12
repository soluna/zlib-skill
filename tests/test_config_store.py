"""Behavior tests for the configuration and credential store interfaces."""

import multiprocessing
import stat
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import zlib_anna.config_store as config_store_module
import zlib_anna.credential_store as credential_store_module
from zlib_anna.config_store import ConfigStore, ConfigStoreError
from zlib_anna.credential_store import (
    Credential,
    CredentialManager,
    CredentialStoreError,
    FileCredentialStore,
    SystemKeychainCredentialStore,
    _SecurityFrameworkBackend,
)


class _InMemoryKeychainBackend:
    def __init__(self, *, available=True):
        self.available = available
        self.secret = None
        self.calls = []

    def capability(self):
        return self.available

    def read(self, service, account):
        self.calls.append(("read", service, account))
        return self.secret

    def write(self, service, account, secret):
        self.calls.append(("write", service, account))
        self.secret = bytes(secret)

    def delete(self, service, account):
        self.calls.append(("delete", service, account))
        self.secret = None

    def delete_if_matches(self, service, account, expected):
        if self.secret != expected:
            raise RuntimeError("source changed")
        self.delete(service, account)


class _InjectedCrash(RuntimeError):
    pass


class _ProcessKeychainBackend:
    """A deterministic, file-backed stand-in for cross-process tests."""

    def __init__(self, path):
        self.path = Path(path)

    def capability(self):
        return True

    def read(self, _service, _account):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def write(self, _service, _account, secret):
        self.path.write_bytes(bytes(secret))

    def delete(self, _service, _account):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def delete_if_matches(self, service, account, expected):
        if self.read(service, account) != expected:
            raise RuntimeError("source changed")
        self.delete(service, account)


class _LifecycleSecurityBackend(_SecurityFrameworkBackend):
    class Security:
        def __init__(self, add_status, copy_status):
            self.add_status = add_status
            self.copy_status = copy_status
            self.update_calls = 0

        def SecItemCopyMatching(self, _query, _result):
            return self.copy_status

        def SecItemAdd(self, _query, _result):
            return self.add_status

        def SecItemUpdate(self, _query, _attributes):
            self.update_calls += 1
            return 0

    def __init__(
        self,
        *,
        add_status=-25299,
        copy_status=-25300,
        fail_string_call=None,
    ):
        self._security = self.Security(add_status, copy_status)
        self._core_foundation = object()
        self._next_reference = 100
        self.allocated = []
        self.released = []
        self._string_calls = 0
        self._fail_string_call = fail_string_call

    def _allocate(self):
        self._next_reference += 1
        self.allocated.append(self._next_reference)
        return self._next_reference

    def _constant(self, _name):
        return 1

    def _core_constant(self, _name):
        return 2

    def _string(self, _value):
        self._string_calls += 1
        if self._string_calls == self._fail_string_call:
            raise RuntimeError("partial allocation failure")
        return self._allocate()

    def _data(self, _value):
        return self._allocate()

    def _dictionary(self, _pairs):
        return self._allocate()

    def _release(self, *references):
        self.released.extend(reference for reference in references if reference)


def _process_update(path, key, value, start):
    store = ConfigStore(lambda: Path(path))
    start.wait()

    def delayed_update(current):
        time.sleep(0.1)
        return {**current, key: value}

    store.update(delayed_update)


def _process_manager_save(path, user_id, metadata_key, start):
    config = ConfigStore(lambda: Path(path))
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend(available=False))
    start.wait()
    CredentialManager(config, keychain=keychain).save(
        Credential(user_id=user_id, user_key=f"{user_id}-secret"),
        metadata={metadata_key: True},
    )


def _process_manager_migrate(path, keychain_path, start):
    config = ConfigStore(lambda: Path(path))
    keychain = SystemKeychainCredentialStore(backend=_ProcessKeychainBackend(keychain_path))
    start.wait()
    CredentialManager(config, keychain=keychain).migrate("system-keychain")


def _process_manager_save_with_keychain(path, keychain_path, start):
    config = ConfigStore(lambda: Path(path))
    keychain = SystemKeychainCredentialStore(backend=_ProcessKeychainBackend(keychain_path))
    start.wait()
    CredentialManager(config, keychain=keychain).save(
        Credential(user_id="new-user", user_key="new-secret"),
        metadata={"saved_concurrently": True},
    )


def test_config_store_resolves_its_path_at_runtime_and_roundtrips_atomically(tmp_path):
    active_path = {"value": tmp_path / "first" / "config.json"}
    store = ConfigStore(lambda: active_path["value"])

    store.replace({"domain": "first.example"})
    active_path["value"] = tmp_path / "second" / "config.json"
    store.replace({"domain": "second.example"})

    assert store.load() == {"domain": "second.example"}
    assert (tmp_path / "first" / "config.json").read_text(encoding="utf-8")
    assert stat.S_IMODE(active_path["value"].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(active_path["value"].stat().st_mode) == 0o600
    assert list(active_path["value"].parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "operation",
    [
        lambda store: store.path,
        lambda store: store.load(),
        lambda store: store.replace({"domain": "example.test"}),
        lambda store: store.update(lambda current: {**current, "domain": "example.test"}),
        lambda store: store.status(),
        lambda store: store.repair_permissions(),
    ],
)
def test_config_store_sanitizes_path_provider_failures(tmp_path, operation):
    marker = "path-provider-marker"

    def failing_provider():
        raise RuntimeError(marker)

    store = ConfigStore(failing_provider)
    with pytest.raises(ConfigStoreError) as raised:
        operation(store)

    assert raised.value.code == "CONFIG_PATH_ERROR"
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value.details)


def test_config_store_serializes_thread_updates_without_losing_fields(tmp_path):
    store = ConfigStore(lambda: tmp_path / "config.json")
    ready = threading.Barrier(2)

    def update(key, value):
        ready.wait()
        store.update(lambda current: {**current, key: value})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(update, "remix_userkey", "token-value"),
            pool.submit(update, "domain", "z-library.example"),
        ]
        for future in futures:
            future.result()

    assert store.load() == {
        "remix_userkey": "token-value",
        "domain": "z-library.example",
    }


def test_config_store_serializes_process_updates_without_losing_fields(tmp_path):
    path = tmp_path / "config.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_process_update,
            args=(str(path), "remix_userkey", "token-value", start),
        ),
        context.Process(
            target=_process_update,
            args=(str(path), "domain", "z-library.example", start),
        ),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert ConfigStore(lambda: path).load() == {
        "remix_userkey": "token-value",
        "domain": "z-library.example",
    }


def test_credential_manager_serializes_cross_process_login_metadata(tmp_path):
    path = tmp_path / "config.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_process_manager_save,
            args=(str(path), "first-user", "first_login", start),
        ),
        context.Process(
            target=_process_manager_save,
            args=(str(path), "second-user", "second_login", start),
        ),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    persisted = ConfigStore(lambda: path).load()
    assert persisted["first_login"] is True
    assert persisted["second_login"] is True
    assert persisted["remix_userid"] in {"first-user", "second-user"}


def test_credential_manager_serializes_cross_process_migration_and_login(tmp_path):
    path = tmp_path / "config.json"
    keychain_path = tmp_path / "keychain.bin"
    original = Credential(user_id="old-user", user_key="old-secret")
    FileCredentialStore(ConfigStore(lambda: path)).write(original)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_process_manager_migrate,
            args=(str(path), str(keychain_path), start),
        ),
        context.Process(
            target=_process_manager_save_with_keychain,
            args=(str(path), str(keychain_path), start),
        ),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    config = ConfigStore(lambda: path)
    assert config.load()["credential_storage"] == "system-keychain"
    assert FileCredentialStore(config).read() is None
    keychain = SystemKeychainCredentialStore(backend=_ProcessKeychainBackend(keychain_path))
    assert CredentialManager(config, keychain=keychain).load() == Credential(
        user_id="new-user",
        user_key="new-secret",
    )
    assert config.load()["saved_concurrently"] is True


def test_config_store_rejects_corrupt_json_without_exposing_its_contents(tmp_path):
    path = tmp_path / "config.json"
    marker = "opaque-must-not-escape"
    path.write_text('{"remix_userkey": "' + marker + '"', encoding="utf-8")
    store = ConfigStore(lambda: path)

    try:
        store.load()
    except ConfigStoreError as error:
        assert error.code == "CONFIG_INVALID"
        assert marker not in str(error)
        assert marker not in repr(error.details)
    else:
        raise AssertionError("corrupt JSON must fail closed")

    assert store.load(strict=False) == {}


def test_config_store_repairs_legacy_directory_and_file_permissions(tmp_path):
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir()
    path.write_text('{"domain": "z-library.example"}', encoding="utf-8")
    path.parent.chmod(0o755)
    path.chmod(0o644)

    assert ConfigStore(lambda: path).load() == {"domain": "z-library.example"}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_config_store_reports_permission_failure_without_raw_error(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"domain": "z-library.example"}', encoding="utf-8")
    real_chmod = config_store_module.os.chmod

    def denied_chmod(target, mode):
        if Path(target) == path:
            raise PermissionError("private-filesystem-detail")
        return real_chmod(target, mode)

    monkeypatch.setattr(config_store_module.os, "chmod", denied_chmod)

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: path).load()

    assert raised.value.code == "CONFIG_PERMISSION_ERROR"
    assert "private-filesystem-detail" not in str(raised.value)
    assert "private-filesystem-detail" not in repr(raised.value.details)


def test_config_store_reports_directory_at_config_path_as_stable_io_error(tmp_path):
    path = tmp_path / "config.json"
    path.mkdir()

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: path).load()

    assert raised.value.code == "CONFIG_IO_ERROR"
    assert "config.json" not in str(raised.value)


def test_config_store_rejects_invalid_utf8_without_exposing_bytes(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b'\xff{"remix_userkey":"opaque-value"}')

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: path).load()

    assert raised.value.code == "CONFIG_INVALID"
    assert "opaque-value" not in str(raised.value)
    assert ConfigStore(lambda: path).load(strict=False) == {}


def test_config_store_sanitizes_mkstemp_failure(tmp_path, monkeypatch):
    marker = "opaque-mkstemp-detail"

    def fail_mkstemp(*_args, **_kwargs):
        raise PermissionError(marker)

    monkeypatch.setattr(config_store_module.tempfile, "mkstemp", fail_mkstemp)

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: tmp_path / "config.json").replace({"token": "value"})

    assert raised.value.code == "CONFIG_WRITE_ERROR"
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value.details)


def test_config_store_sanitizes_replace_failure_and_cleans_temporary(tmp_path, monkeypatch):
    marker = "opaque-replace-detail"

    def fail_replace(_source, _target):
        raise OSError(marker)

    monkeypatch.setattr(config_store_module.os, "replace", fail_replace)
    path = tmp_path / "config.json"

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: path).replace({"token": "value"})

    assert raised.value.code == "CONFIG_WRITE_ERROR"
    assert marker not in str(raised.value)
    assert list(tmp_path.glob("*.tmp")) == []


def test_config_store_sanitizes_fsync_failure_and_cleans_temporary(tmp_path, monkeypatch):
    marker = "opaque-fsync-detail"

    def fail_fsync(_descriptor):
        raise OSError(marker)

    monkeypatch.setattr(config_store_module.os, "fsync", fail_fsync)
    path = tmp_path / "config.json"

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: path).replace({"token": "value"})

    assert raised.value.code == "CONFIG_WRITE_ERROR"
    assert marker not in str(raised.value)
    assert list(tmp_path.glob("*.tmp")) == []


def test_config_store_sanitizes_lock_open_failure(tmp_path, monkeypatch):
    marker = "opaque-lock-open-detail"
    real_open = Path.open

    def fail_lock_open(path, *args, **kwargs):
        if path.name == ".config.json.lock":
            raise OSError(marker)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_lock_open)

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: tmp_path / "config.json").load()

    assert raised.value.code == "CONFIG_LOCK_ERROR"
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value.details)


def test_config_store_sanitizes_temporary_permission_failure(tmp_path, monkeypatch):
    marker = "opaque-write-permission-detail"

    def fail_fchmod(_descriptor, _mode):
        raise PermissionError(marker)

    monkeypatch.setattr(config_store_module.os, "fchmod", fail_fchmod)

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: tmp_path / "config.json").replace({"token": "value"})

    assert raised.value.code == "CONFIG_WRITE_ERROR"
    assert marker not in str(raised.value)
    assert list(tmp_path.glob("*.tmp")) == []


def test_config_store_sanitizes_serialization_failure(tmp_path):
    marker = "opaque-object-representation"

    class Unserializable:
        def __repr__(self):
            return marker

    with pytest.raises(ConfigStoreError) as raised:
        ConfigStore(lambda: tmp_path / "config.json").replace({"token": Unserializable()})

    assert raised.value.code == "CONFIG_WRITE_ERROR"
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value.details)
    assert list(tmp_path.glob("*.tmp")) == []


def test_config_transaction_sanitizes_sidecar_delete_failure(tmp_path, monkeypatch):
    store = ConfigStore(lambda: tmp_path / "config.json")
    with store.transaction() as transaction:
        transaction.write_sidecar("journal.json", {"phase": "prepare"})
    real_unlink = Path.unlink
    marker = "opaque-sidecar-delete-detail"

    def fail_sidecar_unlink(path, *args, **kwargs):
        if path.name == "journal.json":
            raise OSError(marker)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_sidecar_unlink)

    with pytest.raises(ConfigStoreError) as raised:
        with store.transaction() as transaction:
            transaction.delete_sidecar("journal.json")

    assert raised.value.code == "CONFIG_WRITE_ERROR"
    assert marker not in str(raised.value)


def test_file_credential_store_roundtrips_and_deletes_only_credentials(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"domain": "z-library.example"})
    credentials = FileCredentialStore(config)
    credential = Credential(
        user_id="42",
        user_key="token-value",
        email="reader@example.com",
        name="Reader",
    )

    assert credentials.capability() is True
    credentials.write(credential)
    assert credentials.read() == credential
    credentials.delete()

    assert credentials.read() is None
    assert config.load() == {"domain": "z-library.example"}


def test_file_credential_store_rejects_partial_credentials_without_exposure(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    marker = "opaque-must-not-escape"
    config.replace({"remix_userkey": marker})

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config).effective_config()

    assert raised.value.code == "CREDENTIAL_INVALID"
    assert marker not in str(raised.value)


def test_system_keychain_capability_comes_from_native_backend():
    assert SystemKeychainCredentialStore(platform_name="linux").capability() is False
    assert (
        SystemKeychainCredentialStore(
            backend=_InMemoryKeychainBackend(available=False)
        ).capability()
        is False
    )
    assert (
        SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend(available=True)).capability()
        is True
    )


def test_system_keychain_capability_fails_closed_when_backend_probe_errors():
    class FailingProbeBackend(_InMemoryKeychainBackend):
        def capability(self):
            raise RuntimeError("private native probe failure")

    assert SystemKeychainCredentialStore(backend=FailingProbeBackend()).capability() is False


def test_system_keychain_capability_is_false_when_framework_cannot_load(monkeypatch):
    def unavailable_framework(_path):
        raise OSError("private framework load failure")

    monkeypatch.setattr(credential_store_module.ctypes, "CDLL", unavailable_framework)

    assert SystemKeychainCredentialStore(platform_name="darwin").capability() is False


def test_system_keychain_capability_is_false_when_native_symbol_is_missing(monkeypatch):
    class MissingSymbols:
        def __getattr__(self, _name):
            raise AttributeError("private missing symbol detail")

    monkeypatch.setattr(
        credential_store_module.ctypes,
        "CDLL",
        lambda _path: MissingSymbols(),
    )

    assert SystemKeychainCredentialStore(platform_name="darwin").capability() is False


def test_system_keychain_roundtrips_through_injected_native_backend_without_commands():
    backend = _InMemoryKeychainBackend()
    credentials = SystemKeychainCredentialStore(backend=backend)
    credential = Credential(
        user_id="42",
        user_key="token-value",
        email="reader@example.com",
        name="Reader",
    )

    credentials.write(credential)
    assert credentials.read() == credential
    credentials.delete()
    assert credentials.read() is None

    assert backend.calls == [
        ("write", "com.openai.zlib-skill", "zlib-account"),
        ("read", "com.openai.zlib-skill", "zlib-account"),
        ("delete", "com.openai.zlib-skill", "zlib-account"),
        ("read", "com.openai.zlib-skill", "zlib-account"),
    ]
    assert "token-value" not in repr(backend.calls)


def test_system_keychain_write_updates_an_existing_backend_item():
    backend = _InMemoryKeychainBackend()
    credentials = SystemKeychainCredentialStore(backend=backend)

    credentials.write(Credential(user_id="first", user_key="first-secret"))
    credentials.write(Credential(user_id="second", user_key="second-secret"))

    assert credentials.read() == Credential(
        user_id="second",
        user_key="second-secret",
    )
    assert [call[0] for call in backend.calls] == ["write", "write", "read"]
    assert "first-secret" not in repr(backend.calls)
    assert "second-secret" not in repr(backend.calls)


def test_system_keychain_adapter_rejects_interactive_command_runner_path():
    with pytest.raises(TypeError):
        SystemKeychainCredentialStore(
            command_runner=lambda *_args, **_kwargs: None,
            platform_name="darwin",
            security_path="/usr/bin/security",
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Security.framework only")
def test_system_keychain_production_backend_detects_security_framework():
    assert SystemKeychainCredentialStore().capability() is True


def test_system_keychain_backend_write_error_is_stable_and_sanitized():
    marker = "opaque-must-not-escape"

    class FailingBackend(_InMemoryKeychainBackend):
        def write(self, service, account, encoded):
            raise RuntimeError(f"native failure {marker}")

    credentials = SystemKeychainCredentialStore(backend=FailingBackend())

    with pytest.raises(CredentialStoreError) as raised:
        credentials.write(Credential(user_id="42", user_key=marker))

    assert raised.value.code == "KEYCHAIN_WRITE_FAILED"
    assert marker not in str(raised.value)


def test_system_keychain_backend_read_error_is_stable_and_sanitized():
    marker = "opaque-must-not-escape"

    class FailingBackend(_InMemoryKeychainBackend):
        def read(self, service, account):
            raise RuntimeError(f"native failure {marker}")

    credentials = SystemKeychainCredentialStore(backend=FailingBackend())

    with pytest.raises(CredentialStoreError) as raised:
        credentials.read()

    assert raised.value.code == "KEYCHAIN_READ_FAILED"
    assert marker not in str(raised.value)


def test_system_keychain_backend_delete_error_is_stable_and_sanitized():
    marker = "opaque-must-not-escape"

    class FailingBackend(_InMemoryKeychainBackend):
        def delete(self, service, account):
            raise RuntimeError(f"native failure {marker}")

    credentials = SystemKeychainCredentialStore(backend=FailingBackend())

    with pytest.raises(CredentialStoreError) as raised:
        credentials.delete()

    assert raised.value.code == "KEYCHAIN_DELETE_FAILED"
    assert marker not in str(raised.value)


def test_system_keychain_rejects_non_utf8_native_secret_with_stable_error():
    backend = _InMemoryKeychainBackend()
    backend.secret = b"\xffprivate-native-data"
    credentials = SystemKeychainCredentialStore(backend=backend)

    with pytest.raises(CredentialStoreError) as raised:
        credentials.read()

    assert raised.value.code == "KEYCHAIN_INVALID"
    assert "private-native-data" not in str(raised.value)


def test_native_backend_duplicate_base_query_failure_releases_each_cf_object_once():
    backend = _LifecycleSecurityBackend(fail_string_call=4)

    with pytest.raises(RuntimeError, match="partial allocation failure"):
        backend.write("service", "account", b"secret")

    release_counts = Counter(backend.released)
    assert set(backend.released) == set(backend.allocated)
    assert all(release_counts[reference] == 1 for reference in backend.allocated)


def test_native_backend_duplicate_item_updates_and_releases_each_cf_object_once():
    backend = _LifecycleSecurityBackend(add_status=-25299)

    backend.write("service", "account", b"secret")

    assert backend._security.update_calls == 1
    release_counts = Counter(backend.released)
    assert set(backend.released) == set(backend.allocated)
    assert all(release_counts[reference] == 1 for reference in backend.allocated)


def test_native_backend_not_found_read_releases_query_and_partial_values_once():
    backend = _LifecycleSecurityBackend(copy_status=-25300)

    assert backend.read("service", "account") is None

    release_counts = Counter(backend.released)
    assert set(backend.released) == set(backend.allocated)
    assert all(release_counts[reference] == 1 for reference in backend.allocated)


def test_native_backend_partial_read_allocation_releases_created_values_once():
    backend = _LifecycleSecurityBackend(fail_string_call=2)

    with pytest.raises(RuntimeError, match="partial allocation failure"):
        backend.read("service", "account")

    assert backend.released == backend.allocated


def test_credential_manager_migrates_file_credentials_to_system_keychain(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace(
        {
            "remix_userid": "42",
            "remix_userkey": "token-value",
            "email": "reader@example.com",
            "domain": "z-library.example",
        }
    )
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)

    result = manager.migrate("system-keychain")

    assert result == {"storage": "system-keychain", "migrated": True}
    assert manager.load() == Credential(
        user_id="42",
        user_key="token-value",
        email="reader@example.com",
    )
    persisted = config.load()
    assert persisted["credential_storage"] == "system-keychain"
    assert "remix_userid" not in persisted
    assert "remix_userkey" not in persisted
    assert persisted["domain"] == "z-library.example"


def test_migration_recovers_when_target_write_happened_before_phase_persist(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)

    class CrashAfterWriteBackend(_InMemoryKeychainBackend):
        crashed = False

        def write(self, service, account, secret):
            super().write(service, account, secret)
            if not self.crashed:
                self.crashed = True
                raise _InjectedCrash("target-write")

    backend = CrashAfterWriteBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).migrate("system-keychain")
    assert raised.value.code == "KEYCHAIN_WRITE_FAILED"

    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert FileCredentialStore(config).read() is None
    assert not recovered.journal_path.exists()


def test_migration_recovers_when_selector_switch_happened_before_phase_persist(
    tmp_path, monkeypatch
):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    original_persist = CredentialManager._persist_phase

    def crash_before_selector_phase(manager, transaction, journal, phase):
        if phase == "selector_switched":
            raise _InjectedCrash("selector-switch")
        return original_persist(manager, transaction, journal, phase)

    monkeypatch.setattr(CredentialManager, "_persist_phase", crash_before_selector_phase)
    with pytest.raises(_InjectedCrash, match="selector-switch"):
        CredentialManager(config, keychain=keychain).migrate("system-keychain")

    monkeypatch.setattr(CredentialManager, "_persist_phase", original_persist)
    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert config.load()["credential_storage"] == "system-keychain"
    assert not recovered.journal_path.exists()


def test_migration_recovers_when_source_delete_happened_before_phase_persist(tmp_path, monkeypatch):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    original_delete = FileCredentialStore.delete

    def delete_then_crash(store, transaction=None):
        original_delete(store, transaction)
        raise _InjectedCrash("source-delete")

    monkeypatch.setattr(FileCredentialStore, "delete", delete_then_crash)
    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).migrate("system-keychain")
    assert raised.value.code == "CREDENTIAL_DELETE_FAILED"

    monkeypatch.setattr(FileCredentialStore, "delete", original_delete)
    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert FileCredentialStore(config).read() is None
    assert not recovered.journal_path.exists()


def test_explicit_storage_selects_populated_target_when_source_is_empty(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"credential_storage": "file", "domain": "z-library.example"})
    credential = Credential(user_id="42", user_key="token-value")
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(credential)
    manager = CredentialManager(config, keychain=keychain)

    result = manager.migrate("system-keychain")

    assert result == {"storage": "system-keychain", "migrated": True}
    assert manager.load() == credential
    assert config.load() == {
        "credential_storage": "system-keychain",
        "domain": "z-library.example",
    }


def test_explicit_file_storage_selects_populated_target_when_keychain_is_empty(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    config.update(lambda current: {**current, "credential_storage": "system-keychain"})
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    manager = CredentialManager(config, keychain=keychain)

    result = manager.migrate("file")

    assert result == {"storage": "file", "migrated": True}
    assert manager.load() == credential
    assert config.load()["credential_storage"] == "file"
    assert keychain.read() is None


def test_explicit_selected_target_cleans_matching_unselected_credential(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    config.update(lambda current: {**current, "credential_storage": "file"})
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(credential)
    manager = CredentialManager(config, keychain=keychain)

    result = manager.migrate("file")

    assert result == {"storage": "file", "migrated": True}
    assert manager.load() == credential
    assert keychain.read() is None


def test_explicit_selected_keychain_cleans_matching_file_credential(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    config.update(lambda current: {**current, "credential_storage": "system-keychain"})
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(credential)
    manager = CredentialManager(config, keychain=keychain)

    result = manager.migrate("system-keychain")

    assert result == {"storage": "system-keychain", "migrated": True}
    assert manager.load() == credential
    assert FileCredentialStore(config).read() is None


@pytest.mark.parametrize("target", ["file", "system-keychain"])
def test_explicit_selected_empty_target_moves_unselected_credential(tmp_path, target):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    if target == "file":
        config.replace({"credential_storage": "file"})
        keychain.write(credential)
    else:
        FileCredentialStore(config).write(credential)
        config.update(lambda current: {**current, "credential_storage": "system-keychain"})
    manager = CredentialManager(config, keychain=keychain)

    result = manager.migrate(target)

    assert result == {"storage": target, "migrated": True}
    assert manager.load() == credential
    if target == "file":
        assert keychain.read() is None
    else:
        assert FileCredentialStore(config).read() is None


@pytest.mark.parametrize("target", ["system-keychain", "file"])
@pytest.mark.parametrize(
    "interrupted_phase",
    ["prepare", "target_verified", "selector_switched", "source_removed", "complete"],
)
def test_explicit_populated_target_recovers_after_every_migration_phase(
    tmp_path, target, interrupted_phase
):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    if target == "system-keychain":
        config.replace({"credential_storage": "file"})
        keychain.write(credential)
    else:
        FileCredentialStore(config).write(credential)
        config.update(lambda current: {**current, "credential_storage": "system-keychain"})

    def interrupt(phase):
        if phase == interrupted_phase:
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(config, keychain=keychain, phase_hook=interrupt)
    with pytest.raises(_InjectedCrash, match=interrupted_phase):
        interrupted.migrate(target)

    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert config.load()["credential_storage"] == target
    assert not interrupted.journal_path.exists()


@pytest.mark.parametrize(
    "interrupted_phase",
    ["prepare", "target_verified", "selector_switched", "source_removed", "complete"],
)
def test_credential_migration_recovers_after_every_persisted_phase(tmp_path, interrupted_phase):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(
        user_id="42",
        user_key="token-value",
        email="reader@example.com",
    )
    FileCredentialStore(config).write(credential)
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)

    def interrupt(phase):
        if phase == interrupted_phase:
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(config, keychain=keychain, phase_hook=interrupt)

    with pytest.raises(_InjectedCrash, match=interrupted_phase):
        interrupted.migrate("system-keychain")

    journal = interrupted.journal_path
    encoded_journal = journal.read_text(encoding="utf-8")
    assert "token-value" not in encoded_journal
    assert "reader@example.com" not in encoded_journal

    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert config.load()["credential_storage"] == "system-keychain"
    assert FileCredentialStore(config).read() is None
    assert not journal.exists()


def test_file_to_keychain_recovery_keeps_source_when_verified_target_is_lost(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())

    def lose_target_after_switch(phase):
        if phase == "selector_switched":
            keychain.delete()
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=lose_target_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("system-keychain")

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).load()

    assert raised.value.code == "CREDENTIAL_MIGRATION_TARGET_CHANGED"
    assert FileCredentialStore(config).read() == credential
    assert interrupted.journal_path.exists()
    journal = interrupted.journal_path.read_text(encoding="utf-8")
    assert "token-value" not in journal
    assert "token-value" not in str(raised.value)


def test_file_to_keychain_recovery_keeps_source_when_target_is_replaced(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())

    def replace_target_after_switch(phase):
        if phase == "selector_switched":
            keychain.write(Credential(user_id="attacker", user_key="replacement-secret"))
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=replace_target_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("system-keychain")

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).load()

    assert raised.value.code == "CREDENTIAL_MIGRATION_TARGET_CHANGED"
    assert FileCredentialStore(config).read() == credential
    assert interrupted.journal_path.exists()
    assert "replacement-secret" not in str(raised.value)


def test_file_to_keychain_recovery_accepts_source_already_deleted_after_switch(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())

    def interrupt_after_switch(phase):
        if phase == "selector_switched":
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=interrupt_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("system-keychain")
    journal = interrupted.journal_path.read_text(encoding="utf-8")
    FileCredentialStore(config).delete()

    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert FileCredentialStore(config).read() is None
    assert keychain.read() == credential
    assert not interrupted.journal_path.exists()
    assert "token-value" not in journal


def test_file_to_keychain_source_removed_recovery_rejects_missing_target(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())

    def interrupt_after_source_removed(phase):
        if phase == "source_removed":
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=interrupt_after_source_removed,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("system-keychain")
    keychain.delete()

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).load()

    assert raised.value.code == "CREDENTIAL_MIGRATION_TARGET_CHANGED"
    assert FileCredentialStore(config).read() is None
    assert interrupted.journal_path.exists()
    assert "token-value" not in interrupted.journal_path.read_text(encoding="utf-8")
    assert "token-value" not in str(raised.value)


def test_migration_recovery_does_not_delete_source_changed_after_switch(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    original = Credential(user_id="42", user_key="token-value")
    replacement = Credential(user_id="attacker", user_key="replacement-secret")
    FileCredentialStore(config).write(original)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())

    def interrupt_after_switch(phase):
        if phase == "selector_switched":
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=interrupt_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("system-keychain")
    FileCredentialStore(config).write(replacement)

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).load()

    assert raised.value.code == "CREDENTIAL_MIGRATION_SOURCE_CHANGED"
    assert FileCredentialStore(config).read() == replacement
    assert keychain.read() == original
    assert interrupted.journal_path.exists()
    assert "replacement-secret" not in str(raised.value)


def test_conflicting_adapters_fail_closed_until_storage_is_explicitly_selected(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    file_credential = Credential(user_id="file-user", user_key="file-secret")
    keychain_credential = Credential(user_id="keychain-user", user_key="keychain-secret")
    FileCredentialStore(config).write(file_credential)
    config.update(lambda current: {**current, "credential_storage": "file"})
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    keychain.write(keychain_credential)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as caught:
        manager.load()

    assert caught.value.code == "CREDENTIAL_CONFLICT"
    assert "file-secret" not in str(caught.value)
    assert "keychain-secret" not in str(caught.value)

    assert manager.migrate("system-keychain") == {
        "storage": "system-keychain",
        "migrated": True,
    }
    assert manager.load() == keychain_credential
    assert FileCredentialStore(config).read() is None


def test_login_fails_closed_without_changing_conflicting_adapters(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    file_credential = Credential(user_id="file-user", user_key="file-secret")
    keychain_credential = Credential(user_id="keychain-user", user_key="keychain-secret")
    FileCredentialStore(config).write(file_credential)
    config.update(lambda current: {**current, "credential_storage": "file"})
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    keychain.write(keychain_credential)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.save(Credential(user_id="new-user", user_key="new-secret"))

    assert raised.value.code == "CREDENTIAL_CONFLICT"
    assert FileCredentialStore(config).read() == file_credential
    assert keychain.read() == keychain_credential


def test_login_with_default_file_fails_closed_when_keychain_has_credential(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    keychain_credential = Credential(user_id="keychain-user", user_key="keychain-secret")
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(keychain_credential)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.save(Credential(user_id="new-user", user_key="new-secret"))

    assert raised.value.code == "CREDENTIAL_STORAGE_AMBIGUOUS"
    assert FileCredentialStore(config).read() is None
    assert keychain.read() == keychain_credential


def test_logout_fails_closed_without_deleting_conflicting_adapters(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    file_credential = Credential(user_id="file-user", user_key="file-secret")
    keychain_credential = Credential(user_id="keychain-user", user_key="keychain-secret")
    FileCredentialStore(config).write(file_credential)
    config.update(lambda current: {**current, "credential_storage": "file"})
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    keychain.write(keychain_credential)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.clear()

    assert raised.value.code == "CREDENTIAL_CONFLICT"
    assert FileCredentialStore(config).read() == file_credential
    assert keychain.read() == keychain_credential


def test_logout_with_default_file_fails_closed_when_keychain_has_credential(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    keychain_credential = Credential(user_id="keychain-user", user_key="keychain-secret")
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(keychain_credential)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.clear()

    assert raised.value.code == "CREDENTIAL_STORAGE_AMBIGUOUS"
    assert FileCredentialStore(config).read() is None
    assert keychain.read() == keychain_credential


def test_selected_unavailable_keychain_fails_closed_without_plaintext_fallback(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace(
        {
            "credential_storage": "system-keychain",
            "domain": "z-library.example",
        }
    )
    unavailable = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend(available=False))
    manager = CredentialManager(config, keychain=unavailable)
    credential = Credential(user_id="42", user_key="token-value")

    with pytest.raises(CredentialStoreError) as caught:
        manager.save(credential)

    assert caught.value.code == "CREDENTIAL_STORAGE_UNAVAILABLE"
    persisted = config.load()
    assert persisted == {
        "credential_storage": "system-keychain",
        "domain": "z-library.example",
    }
    assert "token-value" not in config.path.read_text(encoding="utf-8")


def test_keychain_write_failure_is_sanitized_and_never_falls_back_to_file(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"credential_storage": "system-keychain"})
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    marker = "opaque-must-not-escape"

    class FailingBackend(_InMemoryKeychainBackend):
        def write(self, service, account, encoded):
            raise RuntimeError(f"opaque error {marker}")

    keychain = SystemKeychainCredentialStore(backend=FailingBackend())

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).save(Credential(user_id="42", user_key=marker))

    assert raised.value.code == "KEYCHAIN_WRITE_FAILED"
    assert marker not in str(raised.value)
    assert marker not in config.path.read_text(encoding="utf-8")
    assert FileCredentialStore(config).read() is None


def test_credential_status_reports_capability_and_presence_without_secrets(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    FileCredentialStore(config).write(
        Credential(
            user_id="42",
            user_key="token-value",
            email="reader@example.com",
        )
    )
    unavailable = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend(available=False))

    status = CredentialManager(config, keychain=unavailable).status()

    assert status == {
        "selected": "file",
        "has_credential": True,
        "adapters": {
            "file": {"capability": True, "has_credential": True},
            "system-keychain": {"capability": False, "has_credential": None},
        },
        "migration_phase": None,
    }
    encoded = repr(status)
    assert "token-value" not in encoded
    assert "reader@example.com" not in encoded


def test_credential_clear_removes_selected_keychain_secret_without_file_fallback(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace(
        {
            "credential_storage": "system-keychain",
            "domain": "z-library.example",
        }
    )
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    manager.save(Credential(user_id="42", user_key="token-value"))

    manager.clear()

    assert keychain.read() is None
    assert config.load() == {
        "credential_storage": "system-keychain",
        "domain": "z-library.example",
    }


def test_effective_config_merges_selected_keychain_only_in_memory(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace(
        {
            "credential_storage": "system-keychain",
            "domain": "z-library.example",
        }
    )
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    manager.save(
        Credential(
            user_id="42",
            user_key="token-value",
            email="reader@example.com",
            name="Reader",
        )
    )

    effective = manager.effective_config()

    assert effective == {
        "credential_storage": "system-keychain",
        "domain": "z-library.example",
        "remix_userid": "42",
        "remix_userkey": "token-value",
        "email": "reader@example.com",
        "name": "Reader",
    }
    persisted = config.path.read_text(encoding="utf-8")
    assert "token-value" not in persisted
    assert "reader@example.com" not in persisted


def test_nonsecret_config_update_preserves_keychain_credential_without_plaintext(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"credential_storage": "system-keychain"})
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    credential = Credential(user_id="42", user_key="token-value")
    manager.save(credential)

    manager.update_config(
        {
            "domain": "z-library.example",
            "domain_trusted": True,
        }
    )

    assert manager.load() == credential
    assert config.load() == {
        "credential_storage": "system-keychain",
        "domain": "z-library.example",
        "domain_trusted": True,
    }


def test_nonsecret_config_update_fails_closed_on_conflicting_adapters(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    FileCredentialStore(config).write(Credential(user_id="file-user", user_key="file-secret"))
    config.update(lambda current: {**current, "credential_storage": "file"})
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(Credential(user_id="keychain-user", user_key="keychain-secret"))
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.update_config({"domain": "replacement.example"})

    assert raised.value.code == "CREDENTIAL_CONFLICT"
    assert "domain" not in config.load()


def test_replace_effective_config_updates_metadata_and_credentials_in_one_interface(
    tmp_path,
):
    config = ConfigStore(lambda: tmp_path / "config.json")
    manager = CredentialManager(config)
    manager.save(
        Credential(user_id="old-user", user_key="old-secret"),
        metadata={"obsolete": True},
    )

    manager.replace_effective_config(
        {
            "remix_userid": "new-user",
            "remix_userkey": "new-secret",
            "email": "reader@example.com",
            "domain": "z-library.example",
        }
    )

    assert manager.effective_config() == {
        "remix_userid": "new-user",
        "remix_userkey": "new-secret",
        "email": "reader@example.com",
        "domain": "z-library.example",
    }
    assert "obsolete" not in config.load()


def test_replace_effective_config_rejects_selector_change_without_migration(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    original = Credential(user_id="old-user", user_key="old-secret")
    FileCredentialStore(config).write(original)
    config.update(lambda current: {**current, "credential_storage": "file"})
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.replace_effective_config(
            {
                "credential_storage": "system-keychain",
                "remix_userid": "new-user",
                "remix_userkey": "new-secret",
            }
        )

    assert raised.value.code == "CREDENTIAL_STORAGE_CHANGE_REQUIRES_MIGRATION"
    assert config.load()["credential_storage"] == "file"
    assert FileCredentialStore(config).read() == original
    assert keychain.read() is None


def test_replace_effective_config_rejects_adding_default_file_selector(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    manager = CredentialManager(
        config,
        keychain=SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend()),
    )

    with pytest.raises(CredentialStoreError) as raised:
        manager.replace_effective_config({"credential_storage": "file"})

    assert raised.value.code == "CREDENTIAL_STORAGE_CHANGE_REQUIRES_MIGRATION"
    assert config.load() == {}


def test_replace_effective_config_rejects_omitting_existing_selector(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"credential_storage": "file", "domain": "original.example"})
    manager = CredentialManager(
        config,
        keychain=SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend()),
    )

    with pytest.raises(CredentialStoreError) as raised:
        manager.replace_effective_config({"domain": "replacement.example"})

    assert raised.value.code == "CREDENTIAL_STORAGE_CHANGE_REQUIRES_MIGRATION"
    assert config.load() == {
        "credential_storage": "file",
        "domain": "original.example",
    }


def test_replace_effective_config_preserves_same_explicit_selector(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"credential_storage": "file", "domain": "original.example"})
    manager = CredentialManager(
        config,
        keychain=SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend()),
    )

    manager.replace_effective_config(
        {"credential_storage": "file", "domain": "replacement.example"}
    )

    assert config.load() == {
        "credential_storage": "file",
        "domain": "replacement.example",
    }


def test_replace_with_default_file_fails_closed_when_keychain_has_credential(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    keychain_credential = Credential(user_id="keychain-user", user_key="keychain-secret")
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    keychain.write(keychain_credential)
    manager = CredentialManager(config, keychain=keychain)

    with pytest.raises(CredentialStoreError) as raised:
        manager.replace_effective_config(
            {"remix_userid": "new-user", "remix_userkey": "new-secret"}
        )

    assert raised.value.code == "CREDENTIAL_STORAGE_AMBIGUOUS"
    assert config.load() == {}
    assert keychain.read() == keychain_credential


def test_replace_effective_config_serializes_concurrent_config_update(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    config.replace({"credential_storage": "system-keychain"})
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    write_started = threading.Event()
    release_write = threading.Event()

    class BlockingBackend(_InMemoryKeychainBackend):
        def write(self, service, account, secret):
            write_started.set()
            assert release_write.wait(timeout=5)
            super().write(service, account, secret)

    keychain = SystemKeychainCredentialStore(backend=BlockingBackend())
    replacing = CredentialManager(config, keychain=keychain)
    updating = CredentialManager(config, keychain=keychain)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replace = pool.submit(
            replacing.replace_effective_config,
            {
                "credential_storage": "system-keychain",
                "remix_userid": "42",
                "remix_userkey": "token-value",
                "domain": "z-library.example",
            },
        )
        assert write_started.wait(timeout=5)
        update = pool.submit(updating.update_config, {"domain_trusted": True})
        assert update.done() is False
        release_write.set()
        replace.result(timeout=5)
        update.result(timeout=5)

    assert replacing.effective_config() == {
        "credential_storage": "system-keychain",
        "remix_userid": "42",
        "remix_userkey": "token-value",
        "domain": "z-library.example",
        "domain_trusted": True,
    }
    assert "token-value" not in config.path.read_text(encoding="utf-8")


def test_credential_manager_migrates_system_keychain_back_to_file(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    manager.migrate("system-keychain")

    result = manager.migrate("file")

    assert result == {"storage": "file", "migrated": True}
    assert manager.load() == credential
    assert FileCredentialStore(config).read() == credential
    assert keychain.read() is None
    assert config.load()["credential_storage"] == "file"


def test_migration_rejects_a_keychain_delete_that_did_not_remove_source(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)

    class DeleteNoOpBackend(_InMemoryKeychainBackend):
        def delete(self, service, account):
            self.calls.append(("delete-no-op", service, account))

    backend = DeleteNoOpBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    manager.migrate("system-keychain")
    # Put the credential back in the source adapter and select keychain so the
    # reverse migration exercises the conditional source deletion.
    FileCredentialStore(config).write(credential)
    config.update(lambda current: {**current, "credential_storage": "system-keychain"})

    with pytest.raises(CredentialStoreError) as raised:
        manager.migrate("file")

    assert raised.value.code == "CREDENTIAL_DELETE_FAILED"
    assert FileCredentialStore(config).read() == credential
    assert keychain.read() == credential
    assert manager.journal_path.exists()


def test_migration_restores_source_if_target_disappears_after_source_delete(tmp_path, monkeypatch):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    original_delete = FileCredentialStore.delete

    def delete_then_drop_target(store, transaction=None, *, expected=None):
        original_delete(store, transaction, expected=expected)
        backend.secret = None

    monkeypatch.setattr(FileCredentialStore, "delete", delete_then_drop_target)

    with pytest.raises(CredentialStoreError) as raised:
        manager.migrate("system-keychain")

    assert raised.value.code == "CREDENTIAL_MIGRATION_TARGET_CHANGED"
    assert FileCredentialStore(config).read() == credential
    assert keychain.read() is None
    assert manager.journal_path.exists()


def test_migration_restores_source_after_delete_side_effect_then_error(tmp_path, monkeypatch):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    original_delete = FileCredentialStore.delete

    def delete_then_clear_target_and_fail(store, transaction=None, *, expected=None):
        original_delete(store, transaction, expected=expected)
        backend.secret = None
        raise RuntimeError("post-delete failure")

    monkeypatch.setattr(FileCredentialStore, "delete", delete_then_clear_target_and_fail)
    with pytest.raises(CredentialStoreError) as raised:
        manager.migrate("system-keychain")

    assert raised.value.code == "CREDENTIAL_DELETE_FAILED"
    assert FileCredentialStore(config).read() == credential
    assert keychain.read() is None
    assert manager.journal_path.exists()


def test_migration_restores_source_when_target_read_fails_after_delete(tmp_path, monkeypatch):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    manager = CredentialManager(config, keychain=keychain)
    original_delete = FileCredentialStore.delete
    original_read = backend.read
    deleted = {"value": False}

    def delete_then_fail_target_read(store, transaction=None, *, expected=None):
        original_delete(store, transaction, expected=expected)
        deleted["value"] = True

    def failing_target_read(service, account):
        if deleted["value"]:
            raise RuntimeError("target read unavailable")
        return original_read(service, account)

    monkeypatch.setattr(FileCredentialStore, "delete", delete_then_fail_target_read)
    monkeypatch.setattr(backend, "read", failing_target_read)
    with pytest.raises(CredentialStoreError):
        manager.migrate("system-keychain")

    assert FileCredentialStore(config).read() == credential


@pytest.mark.parametrize(
    "interrupted_phase",
    ["prepare", "target_verified", "selector_switched", "source_removed", "complete"],
)
def test_reverse_credential_migration_recovers_after_every_persisted_phase(
    tmp_path, interrupted_phase
):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    CredentialManager(config, keychain=keychain).migrate("system-keychain")

    def interrupt(phase):
        if phase == interrupted_phase:
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(config, keychain=keychain, phase_hook=interrupt)
    with pytest.raises(_InjectedCrash, match=interrupted_phase):
        interrupted.migrate("file")

    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert config.load()["credential_storage"] == "file"
    assert FileCredentialStore(config).read() == credential
    assert keychain.read() is None
    assert not interrupted.journal_path.exists()


def test_keychain_to_file_recovery_keeps_source_when_verified_target_is_lost(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    CredentialManager(config, keychain=keychain).migrate("system-keychain")

    def interrupt_after_switch(phase):
        if phase == "selector_switched":
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=interrupt_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("file")
    FileCredentialStore(config).delete()

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).load()

    assert raised.value.code == "CREDENTIAL_MIGRATION_TARGET_CHANGED"
    assert keychain.read() == credential
    assert interrupted.journal_path.exists()
    assert "token-value" not in str(raised.value)


def test_keychain_to_file_recovery_keeps_source_when_target_is_replaced(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    CredentialManager(config, keychain=keychain).migrate("system-keychain")

    def interrupt_after_switch(phase):
        if phase == "selector_switched":
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=interrupt_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("file")
    FileCredentialStore(config).write(Credential(user_id="attacker", user_key="replacement-secret"))

    with pytest.raises(CredentialStoreError) as raised:
        CredentialManager(config, keychain=keychain).load()

    assert raised.value.code == "CREDENTIAL_MIGRATION_TARGET_CHANGED"
    assert keychain.read() == credential
    assert interrupted.journal_path.exists()
    assert "replacement-secret" not in str(raised.value)


def test_keychain_to_file_recovery_accepts_source_already_deleted_after_switch(tmp_path):
    config = ConfigStore(lambda: tmp_path / "config.json")
    credential = Credential(user_id="42", user_key="token-value")
    FileCredentialStore(config).write(credential)
    keychain = SystemKeychainCredentialStore(backend=_InMemoryKeychainBackend())
    CredentialManager(config, keychain=keychain).migrate("system-keychain")

    def interrupt_after_switch(phase):
        if phase == "selector_switched":
            raise _InjectedCrash(phase)

    interrupted = CredentialManager(
        config,
        keychain=keychain,
        phase_hook=interrupt_after_switch,
    )
    with pytest.raises(_InjectedCrash):
        interrupted.migrate("file")
    journal = interrupted.journal_path.read_text(encoding="utf-8")
    keychain.delete()

    recovered = CredentialManager(config, keychain=keychain)
    assert recovered.load() == credential
    assert FileCredentialStore(config).read() == credential
    assert keychain.read() is None
    assert not interrupted.journal_path.exists()
    assert "token-value" not in journal


@pytest.mark.parametrize("concurrent_action", ["save", "clear"])
def test_migration_serializes_concurrent_login_and_logout(tmp_path, concurrent_action):
    config = ConfigStore(lambda: tmp_path / "config.json")
    original = Credential(user_id="old-user", user_key="old-secret")
    replacement = Credential(user_id="new-user", user_key="new-secret")
    FileCredentialStore(config).write(original)
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    migration_holds_lock = threading.Event()
    release_migration = threading.Event()

    def hold_at_prepare(phase):
        if phase == "prepare":
            migration_holds_lock.set()
            assert release_migration.wait(timeout=5)

    migrator = CredentialManager(config, keychain=keychain, phase_hook=hold_at_prepare)
    concurrent = CredentialManager(config, keychain=keychain)
    action_started = threading.Event()

    def run_action():
        action_started.set()
        if concurrent_action == "save":
            concurrent.save(replacement, metadata={"domain": "new.example"})
        else:
            concurrent.clear()

    with ThreadPoolExecutor(max_workers=2) as pool:
        migration = pool.submit(migrator.migrate, "system-keychain")
        assert migration_holds_lock.wait(timeout=5)
        action = pool.submit(run_action)
        assert action_started.wait(timeout=5)
        assert action.done() is False
        release_migration.set()
        assert migration.result(timeout=5)["storage"] == "system-keychain"
        action.result(timeout=5)

    final = concurrent.load()
    if concurrent_action == "save":
        assert final == replacement
        assert config.load()["domain"] == "new.example"
    else:
        assert final is None
    assert FileCredentialStore(config).read() is None


@pytest.mark.parametrize("concurrent_action", ["save", "clear"])
def test_reverse_migration_serializes_concurrent_login_and_logout(tmp_path, concurrent_action):
    config = ConfigStore(lambda: tmp_path / "config.json")
    original = Credential(user_id="old-user", user_key="old-secret")
    replacement = Credential(user_id="new-user", user_key="new-secret")
    FileCredentialStore(config).write(original)
    security = tmp_path / "security"
    security.write_text("fixture", encoding="utf-8")
    security.chmod(0o700)
    backend = _InMemoryKeychainBackend()
    keychain = SystemKeychainCredentialStore(backend=backend)
    CredentialManager(config, keychain=keychain).migrate("system-keychain")
    migration_holds_lock = threading.Event()
    release_migration = threading.Event()

    def hold_at_prepare(phase):
        if phase == "prepare":
            migration_holds_lock.set()
            assert release_migration.wait(timeout=5)

    migrator = CredentialManager(config, keychain=keychain, phase_hook=hold_at_prepare)
    concurrent = CredentialManager(config, keychain=keychain)

    def run_action():
        if concurrent_action == "save":
            concurrent.save(replacement)
        else:
            concurrent.clear()

    with ThreadPoolExecutor(max_workers=2) as pool:
        migration = pool.submit(migrator.migrate, "file")
        assert migration_holds_lock.wait(timeout=5)
        action = pool.submit(run_action)
        assert action.done() is False
        release_migration.set()
        assert migration.result(timeout=5)["storage"] == "file"
        action.result(timeout=5)

    if concurrent_action == "save":
        assert concurrent.load() == replacement
    else:
        assert concurrent.load() is None
    assert keychain.read() is None
