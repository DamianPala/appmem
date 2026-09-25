"""Tests for `appmem.collect._read_small_file` (SPEC.md "Tech")."""

import os
from pathlib import Path

import pytest

from appmem.collect import _read_small_file  # pyright: ignore[reportPrivateUsage]

_CHUNK_SIZE = 65536


def test_reads_a_small_file_with_exactly_one_os_read_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "small.txt"
    path.write_text("anon 100\nshmem 20\n")
    real_read = os.read
    calls = {"n": 0}

    def counting_read(fd: int, n: int) -> bytes:
        calls["n"] += 1
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", counting_read)

    content = _read_small_file(str(path))

    assert content == "anon 100\nshmem 20\n"
    assert calls["n"] == 1


def test_reads_a_file_larger_than_one_chunk_in_full(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    content = "x" * (_CHUNK_SIZE + 100)
    path.write_text(content)

    assert _read_small_file(str(path)) == content


def test_reads_a_file_that_is_an_exact_multiple_of_the_chunk_size(tmp_path: Path) -> None:
    # A first read that comes back exactly full still triggers one more
    # (EOF-probing) read, same trade-off as before this file's own size
    # passed the chunk size -- only files strictly smaller skip it.
    path = tmp_path / "exact.txt"
    content = "y" * _CHUNK_SIZE
    path.write_text(content)

    assert _read_small_file(str(path)) == content


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _read_small_file(str(tmp_path / "gone.txt"))


def test_empty_file_returns_empty_string(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("")

    assert _read_small_file(str(path)) == ""
