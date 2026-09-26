"""ABI and failure handling fixtures; these do not invoke host APIs."""

from __future__ import annotations

import ctypes
import errno

import pytest

from appmem.darwin_native import (
    BSDShortInfo,
    DarwinNative,
    ReadResult,
    RUsageV4,
    SwapUsage,
    Unavailable,
    VMStatistics64,
    classify_error,
    decode_vm,
)


def test_fixed_abi_layout() -> None:
    assert ctypes.sizeof(BSDShortInfo) == 64
    assert ctypes.sizeof(RUsageV4) == 296
    assert RUsageV4.phys_footprint.offset == 72
    assert ctypes.sizeof(SwapUsage) == 32
    assert VMStatistics64.total_uncompressed_pages_in_compressor.offset < ctypes.sizeof(
        VMStatistics64
    )
    assert VMStatistics64.swapped_count.offset == ctypes.sizeof(VMStatistics64) - 8


def test_vm_response_accepts_older_revision_without_swapped_count() -> None:
    vm = VMStatistics64()
    vm.free_count = 3
    vm.compressor_page_count = 2
    vm.total_uncompressed_pages_in_compressor = 5
    vm.swapped_count = 7
    older_count = (VMStatistics64.swapped_count.offset) // 4
    old = decode_vm(vm, older_count, 16_384)
    assert old["free"] == 49_152
    assert old["compressor_physical"] == 32_768
    assert old["compressor_logical"] == 81_920
    assert old["swapped_logical"] is None
    newer = decode_vm(vm, ctypes.sizeof(vm) // 4, 16_384)
    assert newer["swapped_logical"] == 114_688


@pytest.mark.parametrize("count,page", [(0, 16_384), (1_000, 16_384), (10, 0)])
def test_vm_response_rejects_short_or_invalid_reads(count: int, page: int) -> None:
    with pytest.raises(ValueError, match="invalid HOST_VM_INFO64"):
        decode_vm(VMStatistics64(), count, page)


@pytest.mark.parametrize(
    ("code", "pid", "expected"),
    [
        (errno.ESRCH, True, Unavailable.VANISHED),
        (errno.EPERM, True, Unavailable.DENIED),
        (errno.EACCES, False, Unavailable.DENIED),
        (errno.ENOENT, False, Unavailable.UNSUPPORTED),
        (errno.EINVAL, True, Unavailable.UNSUPPORTED),
        (errno.EIO, True, Unavailable.ERROR),
    ],
)
def test_errno_classification(code: int, pid: bool, expected: Unavailable) -> None:
    result: ReadResult[object] = classify_error(code, pid=pid)
    assert result.value is None
    assert result.unavailable == expected
    assert result.error_code == code


def test_pid_listing_rejects_a_full_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        def proc_listpids(self, _kind: int, _info: int, buffer: object, size: int) -> int:
            return 4 if buffer is None else size

    reader = object.__new__(DarwinNative)
    monkeypatch.setattr(reader, "_proc", Proc(), raising=False)

    result = reader.pids()
    assert result.value is None
    assert result.unavailable == Unavailable.ERROR
    assert result.error_code is None


def test_short_successful_process_read_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        def proc_pidinfo(
            self, _pid: int, _flavor: int, _arg: int, _buffer: object, size: int
        ) -> int:
            return size - 4

    reader = object.__new__(DarwinNative)
    monkeypatch.setattr(reader, "_proc", Proc(), raising=False)

    result = reader.process(123)
    assert result.value is None
    assert result.unavailable == Unavailable.ERROR
    assert result.error_code is None
