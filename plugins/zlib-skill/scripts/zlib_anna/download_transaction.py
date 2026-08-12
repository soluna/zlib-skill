"""Atomic, bounded download commits shared by source adapters."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


class DownloadRejected(ValueError):
    """The response or destination failed a local safety check."""


class DownloadTooLarge(DownloadRejected):
    pass


class DownloadTransaction:
    """Own one destination lock, unique part file, validation and commit."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, destination: Path, *, max_bytes: int, expected_md5: str | None = None):
        self.destination = Path(destination)
        self.max_bytes = int(max_bytes)
        self.expected_md5 = expected_md5.lower() if expected_md5 else None
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        key = str(path.resolve())
        with cls._locks_guard:
            return cls._locks.setdefault(key, threading.Lock())

    @contextmanager
    def _locked(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        lock = self._lock_for(self.destination)
        with lock:
            lock_path = self.destination.with_name(self.destination.name + ".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                yield
            finally:
                try:
                    os.close(fd)
                finally:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass

    def run(self, writer: Callable[[Path], int | tuple[int, str]]) -> tuple[Path, int, str | None]:
        """Write to a unique sibling part, validate, then replace once."""
        with self._locked():
            if self.destination.is_symlink():
                raise DownloadRejected("destination symlink is not allowed")
            token = secrets.token_hex(12)
            part = self.destination.with_name(f".{self.destination.name}.{token}.part")
            fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                result = writer(part)
                size, digest = result if isinstance(result, tuple) else (int(result), None)
                if size <= 0:
                    raise DownloadRejected("download is empty")
                if size > self.max_bytes:
                    raise DownloadTooLarge("download exceeds configured size")
                if self.expected_md5 and digest and digest.lower() != self.expected_md5:
                    raise DownloadRejected("download checksum mismatch")
                os.replace(part, self.destination)
                return self.destination, size, digest
            finally:
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass


def write_bounded_stream(
    chunks: Iterable[bytes], path: Path, *, max_bytes: int, expected_md5: str | None = None
) -> tuple[int, str]:
    """Write bytes with an actual-byte cap and return size/MD5."""
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("wb") as output:
        for chunk in chunks:
            if not chunk:
                continue
            item = bytes(chunk)
            size += len(item)
            if size > max_bytes:
                raise DownloadTooLarge("download exceeds configured size")
            output.write(item)
            digest.update(item)
    value = digest.hexdigest()
    if expected_md5 and value != expected_md5.lower():
        raise DownloadRejected("download checksum mismatch")
    return size, value


__all__ = ["DownloadRejected", "DownloadTooLarge", "DownloadTransaction", "write_bounded_stream"]
