"""Credential adapters and migration orchestration for zlib-skill."""

from __future__ import annotations

import ctypes
import hashlib
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config_store import ConfigStore, ConfigTransaction

CREDENTIAL_FIELDS = ("remix_userid", "remix_userkey", "email", "name")
KEYCHAIN_SERVICE = "com.openai.zlib-skill"
KEYCHAIN_ACCOUNT = "zlib-account"


def _credential_fingerprint(credential: Credential | None) -> str | None:
    if credential is None:
        return None
    encoded = json.dumps(
        {
            "email": credential.email,
            "name": credential.name,
            "user_id": credential.user_id,
            "user_key": credential.user_key,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CredentialStoreError(Exception):
    """A stable failure that never includes credential material."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Credential:
    user_id: str
    user_key: str
    email: str | None = None
    name: str | None = None


class _KeychainBackend(Protocol):
    def capability(self) -> bool: ...

    def read(self, service: str, account: str) -> bytes | None: ...

    def write(self, service: str, account: str, secret: bytes) -> None: ...

    def delete(self, service: str, account: str) -> None: ...

    def delete_if_matches(self, service: str, account: str, expected: bytes) -> None: ...


class FileCredentialStore:
    """Store credentials in the locked configuration file."""

    name = "file"

    def __init__(self, config: ConfigStore):
        self._config = config

    def capability(self) -> bool:
        return True

    def read(
        self,
        transaction: ConfigTransaction | None = None,
        *,
        strict: bool = True,
    ) -> Credential | None:
        payload = (
            transaction.load(strict=strict)
            if transaction is not None
            else self._config.load(strict=strict)
        )
        user_id = payload.get("remix_userid")
        user_key = payload.get("remix_userkey")
        if bool(user_id) != bool(user_key):
            raise CredentialStoreError(
                "CREDENTIAL_INVALID",
                "Both credential identity fields are required.",
            )
        if not user_id:
            return None
        return Credential(
            user_id=str(user_id),
            user_key=str(user_key),
            email=payload.get("email"),
            name=payload.get("name"),
        )

    def write(self, credential: Credential, transaction: ConfigTransaction | None = None) -> None:
        def add_credential(payload):
            updated = dict(payload)
            updated.update(
                {
                    "remix_userid": credential.user_id,
                    "remix_userkey": credential.user_key,
                }
            )
            for key, value in (("email", credential.email), ("name", credential.name)):
                if value is None:
                    updated.pop(key, None)
                else:
                    updated[key] = value
            return updated

        target = transaction if transaction is not None else self._config
        target.update(add_credential)

    def delete(
        self,
        transaction: ConfigTransaction | None = None,
        *,
        expected: Credential | None = None,
    ) -> None:
        def remove_credential(payload):
            updated = dict(payload)
            if expected is not None:
                current_id = updated.get("remix_userid")
                current_key = updated.get("remix_userkey")
                current = (
                    Credential(
                        user_id=str(current_id),
                        user_key=str(current_key),
                        email=updated.get("email"),
                        name=updated.get("name"),
                    )
                    if current_id and current_key
                    else None
                )
                if current != expected:
                    raise CredentialStoreError(
                        "CREDENTIAL_MIGRATION_SOURCE_CHANGED",
                        "The migration source credential changed during recovery.",
                    )
            for key in CREDENTIAL_FIELDS:
                updated.pop(key, None)
            return updated

        target = transaction if transaction is not None else self._config
        target.update(remove_credential)


class _SecurityFrameworkBackend:
    """Native macOS Keychain backend; implementation is hidden behind the adapter."""

    SECURITY_PATH = "/System/Library/Frameworks/Security.framework/Security"
    CORE_FOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    SECURITY_FUNCTIONS = (
        "SecItemCopyMatching",
        "SecItemAdd",
        "SecItemUpdate",
        "SecItemDelete",
    )
    SECURITY_CONSTANTS = (
        "kSecClass",
        "kSecClassGenericPassword",
        "kSecAttrService",
        "kSecAttrAccount",
        "kSecValueData",
        "kSecReturnData",
        "kSecMatchLimit",
        "kSecMatchLimitOne",
    )
    CORE_FOUNDATION_FUNCTIONS = (
        "CFStringCreateWithCString",
        "CFDataCreate",
        "CFDictionaryCreate",
        "CFDataGetLength",
        "CFDataGetBytePtr",
        "CFRelease",
    )
    ERR_SUCCESS = 0
    ERR_ITEM_NOT_FOUND = -25300
    ERR_DUPLICATE_ITEM = -25299

    def __init__(self, *, platform_name: str | None = None):
        self._platform_name = platform_name if platform_name is not None else sys.platform
        self._security = None
        self._core_foundation = None
        if self._platform_name != "darwin":
            return
        try:
            security = ctypes.CDLL(self.SECURITY_PATH)
            core_foundation = ctypes.CDLL(self.CORE_FOUNDATION_PATH)
            for name in self.SECURITY_FUNCTIONS:
                getattr(security, name)
            for name in self.CORE_FOUNDATION_FUNCTIONS:
                getattr(core_foundation, name)
            for name in self.SECURITY_CONSTANTS:
                ctypes.c_void_p.in_dll(security, name)
            ctypes.c_void_p.in_dll(core_foundation, "kCFBooleanTrue")
            ctypes.c_char.in_dll(core_foundation, "kCFTypeDictionaryKeyCallBacks")
            ctypes.c_char.in_dll(core_foundation, "kCFTypeDictionaryValueCallBacks")
        except (AttributeError, OSError, ValueError):
            return
        self._security = security
        self._core_foundation = core_foundation
        self._configure_functions()

    def capability(self) -> bool:
        return self._security is not None and self._core_foundation is not None

    def _configure_functions(self) -> None:
        security = self._security
        core = self._core_foundation
        security.SecItemCopyMatching.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecItemCopyMatching.restype = ctypes.c_int32
        security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        security.SecItemAdd.restype = ctypes.c_int32
        security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecItemUpdate.restype = ctypes.c_int32
        security.SecItemDelete.argtypes = [ctypes.c_void_p]
        security.SecItemDelete.restype = ctypes.c_int32
        core.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        core.CFStringCreateWithCString.restype = ctypes.c_void_p
        core.CFDataCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_long,
        ]
        core.CFDataCreate.restype = ctypes.c_void_p
        core.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        core.CFDictionaryCreate.restype = ctypes.c_void_p
        core.CFDataGetLength.argtypes = [ctypes.c_void_p]
        core.CFDataGetLength.restype = ctypes.c_long
        core.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        core.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None

    def _constant(self, name: str) -> int:
        return int(ctypes.c_void_p.in_dll(self._security, name).value)

    def _core_constant(self, name: str) -> int:
        return int(ctypes.c_void_p.in_dll(self._core_foundation, name).value)

    def _string(self, value: str) -> int:
        reference = self._core_foundation.CFStringCreateWithCString(
            None,
            value.encode("utf-8"),
            0x08000100,
        )
        if not reference:
            raise RuntimeError("CoreFoundation string allocation failed")
        return int(reference)

    def _data(self, value: bytes) -> int:
        buffer = (ctypes.c_uint8 * len(value)).from_buffer_copy(value)
        reference = self._core_foundation.CFDataCreate(None, buffer, len(value))
        if not reference:
            raise RuntimeError("CoreFoundation data allocation failed")
        return int(reference)

    def _dictionary(self, pairs: list[tuple[int, int]]) -> int:
        keys = (ctypes.c_void_p * len(pairs))(*(key for key, _ in pairs))
        values = (ctypes.c_void_p * len(pairs))(*(value for _, value in pairs))
        key_callbacks = ctypes.addressof(
            ctypes.c_char.in_dll(
                self._core_foundation,
                "kCFTypeDictionaryKeyCallBacks",
            )
        )
        value_callbacks = ctypes.addressof(
            ctypes.c_char.in_dll(
                self._core_foundation,
                "kCFTypeDictionaryValueCallBacks",
            )
        )
        reference = self._core_foundation.CFDictionaryCreate(
            None,
            keys,
            values,
            len(pairs),
            key_callbacks,
            value_callbacks,
        )
        if not reference:
            raise RuntimeError("CoreFoundation dictionary allocation failed")
        return int(reference)

    def _base_query(self, service: str, account: str) -> tuple[int, list[int]]:
        owned: list[int] = []
        query = None
        try:
            service_ref = self._string(service)
            owned.append(service_ref)
            account_ref = self._string(account)
            owned.append(account_ref)
            query = self._dictionary(
                [
                    (
                        self._constant("kSecClass"),
                        self._constant("kSecClassGenericPassword"),
                    ),
                    (self._constant("kSecAttrService"), service_ref),
                    (self._constant("kSecAttrAccount"), account_ref),
                ]
            )
            return query, owned
        except Exception:
            self._release(query, *owned)
            raise

    def _release(self, *references: int | None) -> None:
        for reference in references:
            if reference:
                self._core_foundation.CFRelease(reference)

    def read(self, service: str, account: str) -> bytes | None:
        query = None
        owned: list[int] = []
        result = ctypes.c_void_p()
        try:
            service_ref = self._string(service)
            owned.append(service_ref)
            account_ref = self._string(account)
            owned.append(account_ref)
            query = self._dictionary(
                [
                    (
                        self._constant("kSecClass"),
                        self._constant("kSecClassGenericPassword"),
                    ),
                    (self._constant("kSecAttrService"), service_ref),
                    (self._constant("kSecAttrAccount"), account_ref),
                    (
                        self._constant("kSecReturnData"),
                        self._core_constant("kCFBooleanTrue"),
                    ),
                    (
                        self._constant("kSecMatchLimit"),
                        self._constant("kSecMatchLimitOne"),
                    ),
                ]
            )
            status = self._security.SecItemCopyMatching(query, ctypes.byref(result))
            if status == self.ERR_ITEM_NOT_FOUND:
                return None
            if status != self.ERR_SUCCESS:
                raise RuntimeError(f"Security framework read status {status}")
            length = self._core_foundation.CFDataGetLength(result.value)
            pointer = self._core_foundation.CFDataGetBytePtr(result.value)
            return ctypes.string_at(pointer, length)
        finally:
            self._release(result.value, query, *owned)

    def write(self, service: str, account: str, secret: bytes) -> None:
        query = None
        owned: list[int] = []
        try:
            service_ref = self._string(service)
            owned.append(service_ref)
            account_ref = self._string(account)
            owned.append(account_ref)
            secret_ref = self._data(secret)
            owned.append(secret_ref)
            query = self._dictionary(
                [
                    (
                        self._constant("kSecClass"),
                        self._constant("kSecClassGenericPassword"),
                    ),
                    (self._constant("kSecAttrService"), service_ref),
                    (self._constant("kSecAttrAccount"), account_ref),
                    (self._constant("kSecValueData"), secret_ref),
                ]
            )
            status = self._security.SecItemAdd(query, None)
            if status == self.ERR_DUPLICATE_ITEM:
                self._release(query)
                query = None
                query, base_owned = self._base_query(service, account)
                owned.extend(base_owned)
                attributes = self._dictionary([(self._constant("kSecValueData"), secret_ref)])
                try:
                    status = self._security.SecItemUpdate(query, attributes)
                finally:
                    self._release(attributes)
            if status != self.ERR_SUCCESS:
                raise RuntimeError(f"Security framework write status {status}")
        finally:
            self._release(query, *owned)

    def delete(self, service: str, account: str) -> None:
        query = None
        owned: list[int] = []
        try:
            query, owned = self._base_query(service, account)
            status = self._security.SecItemDelete(query)
            if status not in {self.ERR_SUCCESS, self.ERR_ITEM_NOT_FOUND}:
                raise RuntimeError(f"Security framework delete status {status}")
        finally:
            self._release(query, *owned)

    def delete_if_matches(self, service: str, account: str, expected: bytes) -> None:
        """Atomically delete a matching generic-password item.

        ``kSecValueData`` is a valid search key for generic passwords, so the
        expected bytes remain part of the same Security.framework delete
        query instead of being checked in a separate read/delete window.
        """
        query = None
        owned: list[int] = []
        try:
            service_ref = self._string(service)
            owned.append(service_ref)
            account_ref = self._string(account)
            owned.append(account_ref)
            secret_ref = self._data(expected)
            owned.append(secret_ref)
            query = self._dictionary(
                [
                    (
                        self._constant("kSecClass"),
                        self._constant("kSecClassGenericPassword"),
                    ),
                    (self._constant("kSecAttrService"), service_ref),
                    (self._constant("kSecAttrAccount"), account_ref),
                    (self._constant("kSecValueData"), secret_ref),
                ]
            )
            status = self._security.SecItemDelete(query)
            if status == self.ERR_ITEM_NOT_FOUND:
                raise RuntimeError("keychain item changed")
            if status != self.ERR_SUCCESS:
                raise RuntimeError(f"Security framework delete status {status}")
        finally:
            self._release(query, *owned)


class SystemKeychainCredentialStore:
    """Store one JSON credential in the macOS system keychain."""

    name = "system-keychain"

    def __init__(
        self,
        *,
        backend: _KeychainBackend | None = None,
        platform_name: str | None = None,
    ):
        self._backend = (
            backend
            if backend is not None
            else _SecurityFrameworkBackend(platform_name=platform_name)
        )
        self._conditional_delete_lock = threading.RLock()

    def capability(self) -> bool:
        try:
            return bool(self._backend.capability())
        except Exception:
            return False

    def _require_capability(self) -> None:
        if not self.capability():
            raise CredentialStoreError(
                "KEYCHAIN_UNAVAILABLE",
                "The macOS system keychain is not available.",
            )

    def read(self) -> Credential | None:
        self._require_capability()
        try:
            secret = self._backend.read(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except Exception:
            raise CredentialStoreError(
                "KEYCHAIN_READ_FAILED",
                "The system keychain credential could not be read.",
            ) from None
        if secret is None:
            return None
        try:
            encoded = bytes(secret).decode("utf-8")
        except (TypeError, UnicodeDecodeError):
            raise CredentialStoreError(
                "KEYCHAIN_INVALID",
                "The system keychain credential is invalid.",
            ) from None
        return self._decode_credential(encoded)

    @staticmethod
    def _decode_credential(encoded: str) -> Credential:
        try:
            payload = json.loads(encoded)
            user_id = payload["user_id"]
            user_key = payload["user_key"]
            if not isinstance(user_id, str) or not user_id:
                raise ValueError
            if not isinstance(user_key, str) or not user_key:
                raise ValueError
            email = payload.get("email")
            name = payload.get("name")
            if email is not None and not isinstance(email, str):
                raise ValueError
            if name is not None and not isinstance(name, str):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise CredentialStoreError(
                "KEYCHAIN_INVALID",
                "The system keychain credential is invalid.",
            ) from None
        return Credential(user_id=user_id, user_key=user_key, email=email, name=name)

    def write(self, credential: Credential) -> None:
        self._require_capability()
        secret = json.dumps(
            {
                "user_id": credential.user_id,
                "user_key": credential.user_key,
                "email": credential.email,
                "name": credential.name,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self._backend.write(
                KEYCHAIN_SERVICE,
                KEYCHAIN_ACCOUNT,
                secret.encode("utf-8"),
            )
        except Exception:
            raise CredentialStoreError(
                "KEYCHAIN_WRITE_FAILED",
                "The system keychain credential could not be written.",
            ) from None

    def delete(self, *, expected: Credential | None = None) -> None:
        self._require_capability()
        if expected is not None:
            expected_secret = json.dumps(
                {
                    "user_id": expected.user_id,
                    "user_key": expected.user_key,
                    "email": expected.email,
                    "name": expected.name,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        try:
            if expected is not None:
                delete_if_matches = getattr(self._backend, "delete_if_matches", None)
                with self._conditional_delete_lock:
                    if delete_if_matches is None:
                        raise CredentialStoreError(
                            "CREDENTIAL_DELETE_UNSUPPORTED",
                            "The credential storage cannot perform a conditional delete.",
                        )
                    else:
                        delete_if_matches(
                            KEYCHAIN_SERVICE,
                            KEYCHAIN_ACCOUNT,
                            expected_secret,
                        )
            else:
                self._backend.delete(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except Exception as exc:
            if isinstance(exc, CredentialStoreError):
                raise
            raise CredentialStoreError(
                "KEYCHAIN_DELETE_FAILED",
                "The system keychain credential could not be removed.",
            ) from None


class CredentialManager:
    """Select and migrate credentials without exposing storage details to callers."""

    STORAGE_KEY = "credential_storage"
    STORAGE_NAMES = {"file", "system-keychain"}
    JOURNAL_NAME = ".credential-migration.json"
    PHASES = (
        "prepare",
        "target_verified",
        "selector_switched",
        "source_removed",
        "complete",
    )

    def __init__(
        self,
        config: ConfigStore,
        *,
        keychain: SystemKeychainCredentialStore | None = None,
        phase_hook: Callable[[str], None] | None = None,
    ):
        self._config = config
        self._file = FileCredentialStore(config)
        self._keychain = keychain or SystemKeychainCredentialStore()
        self._phase_hook = phase_hook or (lambda _phase: None)

    @property
    def journal_path(self) -> Path:
        return self._config.path.with_name(self.JOURNAL_NAME)

    def _selected(self, payload: dict[str, Any]) -> str:
        selected = payload.get(self.STORAGE_KEY, "file")
        if selected not in self.STORAGE_NAMES:
            raise CredentialStoreError(
                "CREDENTIAL_STORAGE_INVALID",
                "The configured credential storage selector is invalid.",
            )
        return selected

    def _adapter(self, name: str):
        return self._file if name == "file" else self._keychain

    def _require_capability(self, name: str) -> None:
        if not self._adapter(name).capability():
            raise CredentialStoreError(
                "CREDENTIAL_STORAGE_UNAVAILABLE",
                f"Credential storage '{name}' is unavailable.",
            )

    def _read(
        self,
        name: str,
        transaction: ConfigTransaction,
        *,
        strict: bool = True,
    ) -> Credential | None:
        self._require_capability(name)
        if name == "file":
            return self._file.read(transaction, strict=strict)
        return self._keychain.read()

    def _write(self, name: str, credential: Credential, transaction: ConfigTransaction) -> None:
        self._require_capability(name)
        if name == "file":
            self._file.write(credential, transaction)
        else:
            self._keychain.write(credential)

    def _delete(
        self,
        name: str,
        transaction: ConfigTransaction,
        *,
        expected: Credential | None = None,
    ) -> None:
        self._require_capability(name)
        current = self._read(name, transaction)
        if expected is not None and current != expected:
            raise CredentialStoreError(
                "CREDENTIAL_MIGRATION_SOURCE_CHANGED",
                "The migration source credential changed during recovery.",
            )
        if name == "file":
            self._file.delete(transaction, expected=expected)
        else:
            self._keychain.delete(expected=expected)
        remaining = self._read(name, transaction)
        if remaining is not None:
            raise CredentialStoreError(
                "CREDENTIAL_DELETE_FAILED",
                "The selected credential storage could not be cleared.",
            )

    def _assert_no_conflict(self, transaction: ConfigTransaction, payload: dict[str, Any]) -> None:
        selected = self._selected(payload)
        selected_value = self._read(selected, transaction)
        other = "system-keychain" if selected == "file" else "file"
        other_value = self._read(other, transaction) if self._adapter(other).capability() else None
        if selected_value is not None and other_value is not None and selected_value != other_value:
            raise CredentialStoreError(
                "CREDENTIAL_CONFLICT",
                "Credential stores contain conflicting values; choose a storage explicitly.",
            )

    def _assert_mutation_unambiguous(
        self, transaction: ConfigTransaction, payload: dict[str, Any]
    ) -> None:
        selected = self._selected(payload)
        selected_value = self._read(selected, transaction)
        other = "system-keychain" if selected == "file" else "file"
        other_value = self._read(other, transaction) if self._adapter(other).capability() else None
        if other_value is None:
            return
        if selected_value is not None and selected_value != other_value:
            raise CredentialStoreError(
                "CREDENTIAL_CONFLICT",
                "Credential stores contain conflicting values; choose a storage explicitly.",
            )
        raise CredentialStoreError(
            "CREDENTIAL_STORAGE_AMBIGUOUS",
            "Unselected credential storage contains a value; choose a storage explicitly.",
        )

    def load(self) -> Credential | None:
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            payload = transaction.load()
            selected = self._selected(payload)
            selected_value = self._read(selected, transaction)
            other = "system-keychain" if selected == "file" else "file"
            other_value = (
                self._read(other, transaction)
                if self.STORAGE_KEY in payload and self._adapter(other).capability()
                else None
            )
            if (
                selected_value is not None
                and other_value is not None
                and selected_value != other_value
            ):
                raise CredentialStoreError(
                    "CREDENTIAL_CONFLICT",
                    "Credential stores contain conflicting values; choose a storage explicitly.",
                )
            return selected_value

    def effective_config(self, *, strict: bool = True) -> dict[str, Any]:
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            payload = transaction.load(strict=strict)
            selected = self._selected(payload)
            credential = self._read(selected, transaction, strict=strict)
            other = "system-keychain" if selected == "file" else "file"
            other_value = (
                self._read(other, transaction, strict=strict)
                if self.STORAGE_KEY in payload and self._adapter(other).capability()
                else None
            )
            if credential is not None and other_value is not None and credential != other_value:
                raise CredentialStoreError(
                    "CREDENTIAL_CONFLICT",
                    "Credential stores contain conflicting values; choose a storage explicitly.",
                )
            effective = dict(payload)
            for key in CREDENTIAL_FIELDS:
                effective.pop(key, None)
            if credential is not None:
                effective.update(
                    {
                        "remix_userid": credential.user_id,
                        "remix_userkey": credential.user_key,
                    }
                )
                if credential.email is not None:
                    effective["email"] = credential.email
                if credential.name is not None:
                    effective["name"] = credential.name
            return effective

    def update_config(self, fields: dict[str, Any]) -> dict[str, Any]:
        if any(key in CREDENTIAL_FIELDS or key == self.STORAGE_KEY for key in fields):
            raise CredentialStoreError(
                "CREDENTIAL_METADATA_INVALID",
                "Configuration update contains reserved credential fields.",
            )
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            self._assert_no_conflict(transaction, transaction.load())
            return transaction.update(lambda current: {**current, **fields})

    def replace_effective_config(self, payload: dict[str, Any]) -> None:
        """Atomically replace public config while routing credentials to the selector."""
        replacement = dict(payload)
        user_id = replacement.get("remix_userid")
        user_key = replacement.get("remix_userkey")
        if bool(user_id) != bool(user_key):
            raise CredentialStoreError(
                "CREDENTIAL_INVALID",
                "Both credential identity fields are required.",
            )
        credential = (
            Credential(
                user_id=str(user_id),
                user_key=str(user_key),
                email=replacement.get("email"),
                name=replacement.get("name"),
            )
            if user_id and user_key
            else None
        )
        for key in CREDENTIAL_FIELDS:
            replacement.pop(key, None)

        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            current = transaction.load()
            current_storage = self._selected(current)
            requested_storage = replacement.get(self.STORAGE_KEY, current_storage)
            if (self.STORAGE_KEY in replacement) != (
                self.STORAGE_KEY in current
            ) or requested_storage != current_storage:
                raise CredentialStoreError(
                    "CREDENTIAL_STORAGE_CHANGE_REQUIRES_MIGRATION",
                    "Credential storage changes require the auth storage command.",
                )
            self._assert_mutation_unambiguous(transaction, current)
            selected = self._selected(replacement)
            self._require_capability(selected)

            if selected == "file":
                if credential is not None:
                    replacement.update(
                        {
                            "remix_userid": credential.user_id,
                            "remix_userkey": credential.user_key,
                        }
                    )
                    if credential.email is not None:
                        replacement["email"] = credential.email
                    if credential.name is not None:
                        replacement["name"] = credential.name
                transaction.replace(replacement)
            else:
                if credential is None:
                    self._delete(selected, transaction)
                else:
                    self._write(selected, credential, transaction)
                    if self._read(selected, transaction) != credential:
                        raise CredentialStoreError(
                            "CREDENTIAL_VERIFICATION_FAILED",
                            "The selected credential storage could not be verified.",
                        )
                transaction.replace(replacement)

    def save(
        self,
        credential: Credential,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        metadata = dict(metadata or {})
        if any(key in CREDENTIAL_FIELDS or key == self.STORAGE_KEY for key in metadata):
            raise CredentialStoreError(
                "CREDENTIAL_METADATA_INVALID",
                "Credential metadata contains reserved fields.",
            )
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            payload = transaction.load()
            self._assert_mutation_unambiguous(transaction, payload)
            selected = self._selected(payload)
            self._write(selected, credential, transaction)
            if self._read(selected, transaction) != credential:
                raise CredentialStoreError(
                    "CREDENTIAL_VERIFICATION_FAILED",
                    "The selected credential storage could not be verified.",
                )
            if metadata:
                transaction.update(lambda current: {**current, **metadata})

    def status(self) -> dict[str, Any]:
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            payload = transaction.load(strict=False)
            selected = self._selected(payload)
            values: dict[str, Credential | None] = {}
            adapters: dict[str, dict[str, Any]] = {}
            for name in ("file", "system-keychain"):
                capable = self._adapter(name).capability()
                inspect_value = name == selected or self.STORAGE_KEY in payload
                value = self._read(name, transaction) if capable and inspect_value else None
                values[name] = value
                adapters[name] = {
                    "capability": capable,
                    "has_credential": value is not None if capable and inspect_value else None,
                }
            if (
                values["file"] is not None
                and values["system-keychain"] is not None
                and values["file"] != values["system-keychain"]
            ):
                raise CredentialStoreError(
                    "CREDENTIAL_CONFLICT",
                    "Credential stores contain conflicting values; choose a storage explicitly.",
                )
            selected_value = values[selected] if adapters[selected]["capability"] else None
            return {
                "selected": selected,
                "has_credential": (
                    selected_value is not None if adapters[selected]["capability"] else None
                ),
                "adapters": adapters,
                "migration_phase": None,
            }

    def clear(self) -> None:
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            payload = transaction.load(strict=False)
            self._assert_mutation_unambiguous(transaction, payload)
            selected = self._selected(payload)
            self._delete(selected, transaction)

    def _read_journal(self, transaction: ConfigTransaction) -> dict[str, Any] | None:
        journal = transaction.read_sidecar(self.JOURNAL_NAME)
        if journal is None:
            return None
        phase = journal.get("phase")
        source = journal.get("source")
        target = journal.get("target")
        identity = journal.get("identity")
        if (
            journal.get("version") != 2
            or phase not in self.PHASES
            or source not in self.STORAGE_NAMES
            or target not in self.STORAGE_NAMES
            or source == target
            or not isinstance(identity, dict)
            or not isinstance(identity.get("credential_present"), bool)
            or not isinstance(identity.get("target_authoritative", False), bool)
            or not self._valid_fingerprint(identity.get("credential_fingerprint"))
            or not self._valid_fingerprint(identity.get("source_fingerprint"))
        ):
            raise CredentialStoreError(
                "CREDENTIAL_MIGRATION_INVALID",
                "The credential migration journal is invalid.",
            )
        return journal

    @staticmethod
    def _valid_fingerprint(value: Any) -> bool:
        return value is None or (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _persist_phase(
        self,
        transaction: ConfigTransaction,
        journal: dict[str, Any],
        phase: str,
    ) -> None:
        journal = {**journal, "phase": phase}
        transaction.write_sidecar(self.JOURNAL_NAME, journal)
        self._phase_hook(phase)

    def _recover_locked(self, transaction: ConfigTransaction) -> None:
        journal = self._read_journal(transaction)
        if journal is None:
            return
        self._resume_migration(transaction, journal)

    def _resume_migration(self, transaction: ConfigTransaction, journal: dict[str, Any]) -> None:
        phase = journal["phase"]
        source = journal["source"]
        target = journal["target"]
        credential_present = journal["identity"]["credential_present"]
        target_authoritative = journal["identity"].get("target_authoritative", False)
        credential_fingerprint = journal["identity"]["credential_fingerprint"]
        source_fingerprint = journal["identity"]["source_fingerprint"]

        if phase == "complete":
            transaction.delete_sidecar(self.JOURNAL_NAME)
            return

        source_value = self._read(source, transaction)
        target_value = self._read(target, transaction)

        if phase == "prepare":
            if credential_present and source_value is None and not target_authoritative:
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_INCOMPLETE",
                    "The source credential is missing during migration recovery.",
                )
            if target_authoritative:
                if credential_present and target_value is None:
                    raise CredentialStoreError(
                        "CREDENTIAL_VERIFICATION_FAILED",
                        "The selected credential storage could not be verified.",
                    )
            elif source_value is not None:
                if target_value is not None and target_value != source_value:
                    raise CredentialStoreError(
                        "CREDENTIAL_CONFLICT",
                        "Credential stores contain conflicting values; "
                        "choose a storage explicitly.",
                    )
                self._write(target, source_value, transaction)
                target_value = self._read(target, transaction)
                if target_value != source_value:
                    raise CredentialStoreError(
                        "CREDENTIAL_VERIFICATION_FAILED",
                        "The target credential storage could not be verified.",
                    )
            self._persist_phase(transaction, journal, "target_verified")
            phase = "target_verified"

        if phase == "target_verified":
            if credential_present and target_value is None:
                target_value = self._read(target, transaction)
            if credential_present and target_value is None:
                raise CredentialStoreError(
                    "CREDENTIAL_VERIFICATION_FAILED",
                    "The target credential storage could not be verified.",
                )
            if _credential_fingerprint(target_value) != credential_fingerprint:
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_TARGET_CHANGED",
                    "The migration target credential changed during recovery.",
                )
            if (
                not target_authoritative
                and source_value is not None
                and target_value is not None
                and source_value != target_value
            ):
                raise CredentialStoreError(
                    "CREDENTIAL_CONFLICT",
                    "Credential stores contain conflicting values; choose a storage explicitly.",
                )
            transaction.update(lambda current: {**current, self.STORAGE_KEY: target})
            self._persist_phase(transaction, journal, "selector_switched")
            phase = "selector_switched"

        if phase == "selector_switched":
            target_value = self._read(target, transaction)
            source_value = self._read(source, transaction)
            if _credential_fingerprint(target_value) != credential_fingerprint:
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_TARGET_CHANGED",
                    "The migration target credential changed during recovery.",
                )
            if (
                source_value is not None
                and _credential_fingerprint(source_value) != source_fingerprint
            ):
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_SOURCE_CHANGED",
                    "The migration source credential changed during recovery.",
                )
            if source_value is not None:

                def restore_if_both_missing() -> None:
                    try:
                        source_after = self._read(source, transaction)
                    except Exception:
                        raise CredentialStoreError(
                            "CREDENTIAL_MIGRATION_RECOVERY_REQUIRED",
                            "The migration could not verify credential storage state.",
                        ) from None
                    try:
                        target_after = self._read(target, transaction)
                    except Exception:
                        if source_after is None:
                            try:
                                self._write(source, source_value, transaction)
                                if self._read(source, transaction) != source_value:
                                    raise RuntimeError("source restore verification failed")
                            except Exception:
                                raise CredentialStoreError(
                                    "CREDENTIAL_MIGRATION_ROLLBACK_FAILED",
                                    "The migration could not restore the source credential.",
                                ) from None
                        raise CredentialStoreError(
                            "CREDENTIAL_MIGRATION_RECOVERY_REQUIRED",
                            "The migration could not verify credential storage state.",
                        ) from None
                    if (
                        source_after is None
                        and _credential_fingerprint(target_after) != credential_fingerprint
                    ):
                        try:
                            self._write(source, source_value, transaction)
                            if self._read(source, transaction) != source_value:
                                raise RuntimeError("source restore verification failed")
                        except Exception:
                            raise CredentialStoreError(
                                "CREDENTIAL_MIGRATION_ROLLBACK_FAILED",
                                "The migration could not restore the source credential.",
                            ) from None

                try:
                    self._delete(source, transaction, expected=source_value)
                except CredentialStoreError as exc:
                    restore_if_both_missing()
                    raise exc
                except Exception:
                    restore_if_both_missing()
                    raise CredentialStoreError(
                        "CREDENTIAL_DELETE_FAILED",
                        "The migration source could not be cleared.",
                    ) from None
                source_after = self._read(source, transaction)
                if source_after is not None:
                    raise CredentialStoreError(
                        "CREDENTIAL_DELETE_FAILED",
                        "The migration source could not be cleared.",
                    )

            # Deleting the source is the irreversible step.  If the target
            # disappeared during that step, restore the source before
            # surfacing the failure so recovery never loses the only copy.
            try:
                target_after = self._read(target, transaction)
            except Exception:
                if source_value is not None:
                    restore_if_both_missing()
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_RECOVERY_REQUIRED",
                    "The migration could not verify credential storage state.",
                ) from None
            if _credential_fingerprint(target_after) != credential_fingerprint:
                if source_value is not None:
                    try:
                        self._write(source, source_value, transaction)
                        if self._read(source, transaction) != source_value:
                            raise RuntimeError("source restore verification failed")
                    except Exception:
                        raise CredentialStoreError(
                            "CREDENTIAL_MIGRATION_ROLLBACK_FAILED",
                            "The migration could not restore the source credential.",
                        ) from None
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_TARGET_CHANGED",
                    "The migration target credential changed during recovery.",
                )
            self._persist_phase(transaction, journal, "source_removed")
            phase = "source_removed"

        if phase == "source_removed":
            target_value = self._read(target, transaction)
            if _credential_fingerprint(target_value) != credential_fingerprint:
                raise CredentialStoreError(
                    "CREDENTIAL_MIGRATION_TARGET_CHANGED",
                    "The migration target credential changed during recovery.",
                )
            self._persist_phase(transaction, journal, "complete")

        transaction.delete_sidecar(self.JOURNAL_NAME)

    def migrate(self, target: str) -> dict[str, Any]:
        if target not in self.STORAGE_NAMES:
            raise CredentialStoreError(
                "CREDENTIAL_STORAGE_INVALID",
                "Credential storage must be 'file' or 'system-keychain'.",
            )
        with self._config.transaction() as transaction:
            self._recover_locked(transaction)
            payload = transaction.load()
            selected = self._selected(payload)
            self._require_capability(target)
            other = "system-keychain" if target == "file" else "file"
            target_value = self._read(target, transaction)
            other_value = (
                self._read(other, transaction) if self._adapter(other).capability() else None
            )
            if selected == target and other_value is None:
                return {"storage": target, "migrated": False}
            target_authoritative = target_value is not None and (
                selected == target or target_value != other_value
            )
            source = other if selected == target or target_authoritative else selected
            credential = target_value if target_authoritative else self._read(source, transaction)
            existing = target_value
            if not target_authoritative and existing is not None and existing != credential:
                raise CredentialStoreError(
                    "CREDENTIAL_CONFLICT",
                    "Credential stores contain conflicting values; choose a storage explicitly.",
                )
            source_value = self._read(source, transaction)
            journal = {
                "version": 2,
                "phase": "prepare",
                "source": source,
                "target": target,
                "identity": {
                    "credential_present": credential is not None,
                    "target_authoritative": target_authoritative,
                    "credential_fingerprint": _credential_fingerprint(credential),
                    "source_fingerprint": _credential_fingerprint(source_value),
                },
            }
            self._persist_phase(transaction, journal, "prepare")
            self._resume_migration(transaction, journal)
            return {"storage": target, "migrated": True}
