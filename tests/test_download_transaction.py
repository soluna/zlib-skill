from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from zlib_anna.download_transaction import (  # noqa: E402
    DownloadRejected,
    DownloadTooLarge,
    DownloadTransaction,
    write_bounded_stream,
)


def test_transaction_commits_atomically_and_cleans_part(tmp_path):
    destination = tmp_path / "book.pdf"
    result = DownloadTransaction(destination, max_bytes=100).run(
        lambda path: write_bounded_stream([b"%PDF-1.7", b" data"], path, max_bytes=100)
    )
    assert result[0] == destination
    assert destination.read_bytes() == b"%PDF-1.7 data"
    assert not list(tmp_path.glob("*.part"))


def test_transaction_rejects_oversize_checksum_and_symlink(tmp_path):
    destination = tmp_path / "book.pdf"
    with pytest.raises(DownloadTooLarge):
        DownloadTransaction(destination, max_bytes=2).run(
            lambda path: write_bounded_stream([b"123"], path, max_bytes=2)
        )
    with pytest.raises(DownloadRejected):
        DownloadTransaction(destination, max_bytes=10, expected_md5="0" * 32).run(
            lambda path: write_bounded_stream([b"abc"], path, max_bytes=10)
        )
    target = tmp_path / "real.pdf"
    target.write_bytes(b"old")
    destination.symlink_to(target)
    with pytest.raises(DownloadRejected):
        DownloadTransaction(destination, max_bytes=10).run(lambda path: 1)
    assert target.read_bytes() == b"old"


def test_transaction_serializes_same_destination(tmp_path):
    destination = tmp_path / "same.pdf"

    def writer(path, value):
        path.write_bytes(value)
        return len(value)

    threads = [
        threading.Thread(
            target=lambda value=value: DownloadTransaction(destination, max_bytes=20).run(
                lambda path: writer(path, value)
            ),
            args=(),
        )
        for value in (b"one", b"two")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert destination.read_bytes() in {b"one", b"two"}
    assert not list(tmp_path.glob("*.part"))
