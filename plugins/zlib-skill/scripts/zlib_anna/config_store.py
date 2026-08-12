"""Atomic, runtime-resolved storage for zlib-skill configuration."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_THREAD_LOCKS: dict[Path, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(path, threading.RLock())


class ConfigStoreError(Exception):
    """A stable, sanitized configuration storage failure."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ConfigTransaction:
    """Locked view used for multi-step configuration operations."""

    def __init__(self, store: ConfigStore, path: Path):
        self._store = store
        self.path = path

    def load(self, *, strict: bool = True) -> dict[str, Any]:
        return self._store._load_path(self.path, strict=strict)

    def replace(self, payload: dict[str, Any]) -> None:
        self._store._replace_path(self.path, payload)

    def update(self, transform: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        updated = transform(self.load())
        self.replace(updated)
        return updated

    def sidecar_path(self, name: str) -> Path:
        if not name or Path(name).name != name:
            raise ValueError("Sidecar name must be a single path component.")
        return self.path.with_name(name)

    def read_sidecar(self, name: str) -> dict[str, Any] | None:
        return self._store._load_optional_path(self.sidecar_path(name))

    def write_sidecar(self, name: str, payload: dict[str, Any]) -> None:
        self._store._replace_path(self.sidecar_path(name), payload)

    def delete_sidecar(self, name: str) -> None:
        self._store._delete_path(self.sidecar_path(name))


class ConfigStore:
    """Keep configuration persistence details behind a small interface."""

    def __init__(self, path_provider: Callable[[], Path]):
        self._path_provider = path_provider

    def _resolve_path(self) -> Path:
        """Resolve the active path without allowing provider details to escape."""
        try:
            return Path(self._path_provider()).expanduser()
        except Exception as exc:
            raise ConfigStoreError(
                "CONFIG_PATH_ERROR",
                "Configuration path could not be resolved.",
                details={"error_type": type(exc).__name__},
            ) from None

    @property
    def path(self) -> Path:
        return self._resolve_path()

    @staticmethod
    def _mode(path: Path) -> str | None:
        try:
            if not path.exists():
                return None
            return oct(path.stat().st_mode & 0o777)
        except OSError as exc:
            raise ConfigStoreError(
                "CONFIG_IO_ERROR",
                "Configuration path metadata could not be read.",
                details={"error_type": type(exc).__name__},
            ) from None

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError as exc:
            raise ConfigStoreError(
                "CONFIG_IO_ERROR",
                "Configuration path could not be inspected.",
                details={"error_type": type(exc).__name__},
            ) from None

    def repair_permissions(self) -> list[dict[str, str]]:
        path = self.path
        repairs: list[dict[str, str]] = []
        for target, desired, kind in (
            (path.parent, 0o700, "config_dir"),
            (path, 0o600, "config_file"),
        ):
            try:
                if not target.exists():
                    continue
                current = target.stat().st_mode & 0o777
                if current == desired:
                    continue
                os.chmod(target, desired)
                repairs.append(
                    {
                        "kind": kind,
                        "path": str(target),
                        "from": oct(current),
                        "to": oct(desired),
                    }
                )
            except OSError as exc:
                repairs.append(
                    {
                        "kind": kind,
                        "path": str(target),
                        "error": type(exc).__name__,
                    }
                )
        return repairs

    def status(self) -> dict[str, Any]:
        path = self.path
        repairs = self.repair_permissions()
        payload = {
            "config_dir": str(path.parent),
            "config_file": str(path),
            "config_dir_exists": self._exists(path.parent),
            "config_file_exists": self._exists(path),
            "config_dir_mode": self._mode(path.parent),
            "config_file_mode": self._mode(path),
        }
        if repairs:
            payload["permission_repairs"] = repairs
        return payload

    def load(self, *, strict: bool = True) -> dict[str, Any]:
        with self._locked_path() as path:
            return self._load_path(path, strict=strict)

    def _load_path(self, path: Path, *, strict: bool = True) -> dict[str, Any]:
        try:
            exists = path.exists()
        except OSError as exc:
            raise ConfigStoreError(
                "CONFIG_IO_ERROR",
                "Configuration path could not be inspected.",
                details={"error_type": type(exc).__name__},
            ) from None
        if not exists:
            return {}
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise ConfigStoreError(
                "CONFIG_PERMISSION_ERROR",
                "Configuration permissions could not be secured.",
                details={"path": str(path), "error_type": type(exc).__name__},
            ) from None
        try:
            encoded = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            if not strict:
                return {}
            raise ConfigStoreError(
                "CONFIG_INVALID",
                "Configuration file is not valid UTF-8.",
                details={"error_type": type(exc).__name__},
            ) from None
        except OSError as exc:
            raise ConfigStoreError(
                "CONFIG_IO_ERROR",
                "Configuration file could not be read.",
                details={"error_type": type(exc).__name__},
            ) from None
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as exc:
            if not strict:
                return {}
            raise ConfigStoreError(
                "CONFIG_INVALID",
                "Configuration file is not valid JSON.",
                details={"path": str(path), "error_type": type(exc).__name__},
            ) from None
        if not isinstance(payload, dict):
            if not strict:
                return {}
            raise ConfigStoreError(
                "CONFIG_INVALID",
                "Configuration file must contain a JSON object.",
                details={"path": str(path)},
            )
        return payload

    def _load_optional_path(self, path: Path) -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
        except OSError as exc:
            raise ConfigStoreError(
                "CONFIG_IO_ERROR",
                "Configuration sidecar could not be inspected.",
                details={"error_type": type(exc).__name__},
            ) from None
        return self._load_path(path)

    def replace(self, payload: dict[str, Any]) -> None:
        with self._locked_path() as path:
            self._replace_path(path, payload)

    def _replace_path(self, path: Path, payload: dict[str, Any]) -> None:
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception as exc:
            raise ConfigStoreError(
                "CONFIG_WRITE_ERROR",
                "Configuration file could not be written.",
                details={"error_type": type(exc).__name__},
            ) from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    if temporary.exists():
                        temporary.unlink()
                except OSError:
                    pass

    def _delete_path(self, path: Path) -> None:
        try:
            if not path.exists():
                return
            path.unlink()
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception as exc:
            raise ConfigStoreError(
                "CONFIG_WRITE_ERROR",
                "Configuration sidecar could not be removed.",
                details={"error_type": type(exc).__name__},
            ) from None

    def update(self, transform: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        with self._locked_path() as path:
            updated = transform(self._load_path(path))
            self._replace_path(path, updated)
            return updated

    @contextmanager
    def transaction(self) -> Iterator[ConfigTransaction]:
        with self._locked_path() as path:
            yield ConfigTransaction(self, path)

    @contextmanager
    def _locked_path(self) -> Iterator[Path]:
        path = self._resolve_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            lock_path = path.with_name(f".{path.name}.lock")
            thread_lock = _thread_lock_for(lock_path.resolve())
        except Exception as exc:
            raise ConfigStoreError(
                "CONFIG_LOCK_ERROR",
                "Configuration lock could not be prepared.",
                details={"error_type": type(exc).__name__},
            ) from None
        with thread_lock:
            handle = None
            locked = False
            try:
                handle = lock_path.open("a+b")
                os.chmod(lock_path, 0o600)
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except Exception as exc:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
                raise ConfigStoreError(
                    "CONFIG_LOCK_ERROR",
                    "Configuration lock could not be acquired.",
                    details={"error_type": type(exc).__name__},
                ) from None
            try:
                yield path
            finally:
                try:
                    if locked:
                        handle.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError as exc:
                    raise ConfigStoreError(
                        "CONFIG_LOCK_ERROR",
                        "Configuration lock could not be released.",
                        details={"error_type": type(exc).__name__},
                    ) from None
                finally:
                    try:
                        handle.close()
                    except OSError:
                        pass
