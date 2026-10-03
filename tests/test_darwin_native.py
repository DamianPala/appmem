"""ABI and failure handling fixtures; these do not invoke host APIs."""

from __future__ import annotations

import ctypes
import errno
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

_PROBE = Path(__file__).resolve().parents[1] / "scripts/darwin_probe.py"
_SPEC = importlib.util.spec_from_file_location("darwin_probe", _PROBE)
assert _SPEC is not None and _SPEC.loader is not None
_probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _probe
_SPEC.loader.exec_module(_probe)
validate_sdk_abi = _probe.validate_sdk_abi


def test_fixed_abi_layout() -> None:
    assert ctypes.sizeof(BSDShortInfo) == 64
    assert ctypes.sizeof(RUsageV4) == 296
    assert RUsageV4.phys_footprint.offset == 72
    assert ctypes.sizeof(SwapUsage) == 32
    assert VMStatistics64.total_uncompressed_pages_in_compressor.offset < ctypes.sizeof(
        VMStatistics64
    )
    assert VMStatistics64.swapped_count.offset == ctypes.sizeof(VMStatistics64) - 8


def test_host_page_size_binding_uses_pointer_width(monkeypatch: pytest.MonkeyPatch) -> None:
    class Function:
        def __init__(self) -> None:
            self.argtypes: list[type[object]] = []
            self.restype: type[object] = object

    lib_names = (
        "sysctlbyname",
        "host_page_size",
        "host_statistics64",
        "mach_host_self",
        "mach_port_deallocate",
    )
    proc_names = ("proc_listpids", "proc_pidinfo", "proc_pidpath", "proc_pid_rusage")
    lib = SimpleNamespace(**{name: Function() for name in lib_names})
    proc = SimpleNamespace(**{name: Function() for name in proc_names})
    reader = object.__new__(DarwinNative)
    monkeypatch.setattr(reader, "_lib", lib, raising=False)
    monkeypatch.setattr(reader, "_proc", proc, raising=False)
    reader._bind()  # pyright: ignore[reportPrivateUsage]
    assert lib.host_page_size.argtypes == [ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)]


def test_short_vm_host_read_returns_error_and_releases_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[tuple[int, int]] = []

    def page_size(_host: int, pointer: Any) -> int:
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_size_t))[0] = 16_384
        return 0

    def statistics(_host: int, _kind: int, _vm: object, count: Any) -> int:
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = 10
        return 0

    def release(task: int, port: int) -> None:
        released.append((task, port))

    def sysctl(name: str, _typ: object) -> ReadResult[ctypes.c_uint64 | SwapUsage]:
        return ReadResult(ctypes.c_uint64(16_384) if name == "hw.memsize" else SwapUsage())

    lib = SimpleNamespace(
        mach_host_self=lambda: 7,
        host_page_size=page_size,
        host_statistics64=statistics,
        mach_port_deallocate=release,
    )
    reader = object.__new__(DarwinNative)
    monkeypatch.setattr(reader, "_lib", lib, raising=False)
    monkeypatch.setattr(reader, "_task_self_port", lambda: 42, raising=False)
    monkeypatch.setattr(reader, "_sysctl", sysctl, raising=False)
    result = reader.host()
    assert result.value is None
    assert result.unavailable == Unavailable.ERROR
    assert released == [(42, 7)]


@pytest.mark.parametrize("vm_sdk_size", [152, 160, 416])
def test_probe_accepts_only_supported_sdk_vm_layouts(vm_sdk_size: int) -> None:
    layout = {
        "bsdshort_size": ctypes.sizeof(BSDShortInfo),
        "rusage_size": ctypes.sizeof(RUsageV4),
        "footprint_offset": RUsageV4.phys_footprint.offset,
        "vm_sdk_size": vm_sdk_size,
        "vm_logical_offset": VMStatistics64.total_uncompressed_pages_in_compressor.offset,
        "swap_size": ctypes.sizeof(SwapUsage),
        "vm_swapins_offset": VMStatistics64.swapins.offset,
        "vm_swapouts_offset": VMStatistics64.swapouts.offset,
    }
    validate_sdk_abi(layout)
    for offset in (
        "footprint_offset",
        "vm_logical_offset",
        "vm_swapins_offset",
        "vm_swapouts_offset",
    ):
        with pytest.raises(RuntimeError, match="required ABI mismatch"):
            validate_sdk_abi({**layout, offset: 0})


@pytest.mark.parametrize("vm_sdk_size", [144, 168, 408, 424])
def test_probe_rejects_unknown_sdk_vm_layouts(vm_sdk_size: int) -> None:
    # The physical SDK 27 probe measured these consumed sizes and offsets.
    layout = {
        "bsdshort_size": 64,
        "rusage_size": 296,
        "footprint_offset": 72,
        "vm_sdk_size": vm_sdk_size,
        "vm_logical_offset": 144,
        "swap_size": 32,
        "vm_swapins_offset": 112,
        "vm_swapouts_offset": 120,
    }
    with pytest.raises(RuntimeError, match="unsupported SDK"):
        validate_sdk_abi(layout)


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


@pytest.mark.parametrize("count", [38, 40])
def test_legacy_vm_prefix_decodes_speculative_file_backed_and_purgeable(count: int) -> None:
    vm = VMStatistics64()
    vm.free_count, vm.speculative_count = 100, 20
    vm.external_page_count, vm.purgeable_count = 80, 30
    values = decode_vm(vm, count, 16_384)
    assert values["free"] == 100 * 16_384
    assert values["speculative"] == 20 * 16_384
    assert values["file_backed"] == 80 * 16_384
    assert values["purgeable"] == 30 * 16_384
    assert VMStatistics64.external_page_count.offset + 4 <= 38 * 4
    assert VMStatistics64.purgeable_count.offset + 4 <= 38 * 4
    assert VMStatistics64.speculative_count.offset + 4 <= 38 * 4
