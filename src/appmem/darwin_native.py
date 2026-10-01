"""Low-level Darwin memory and process reads. No OS calls occur on import."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import ctypes as c
import errno
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar, cast

SysctlValue = TypeVar("SysctlValue", c.Structure, c.c_uint32, c.c_uint64)


class Unavailable(StrEnum):
    VANISHED = "vanished"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass(frozen=True)
class ReadResult[T]:
    value: T | None
    unavailable: Unavailable | None = None
    error_code: int | None = None


def classify_error[T](code: int, *, pid: bool = False) -> ReadResult[T]:  # pyright: ignore[reportInvalidTypeVarUse] - inferred from caller
    if code in (errno.EPERM, errno.EACCES):
        reason = Unavailable.DENIED
    elif pid and code in (errno.ESRCH, errno.ENOENT):
        reason = Unavailable.VANISHED
    elif code in (errno.ENOTSUP, errno.ENOSYS, errno.EINVAL) or (not pid and code == errno.ENOENT):
        reason = Unavailable.UNSUPPORTED
    else:
        reason = Unavailable.ERROR
    return ReadResult(None, reason, code)


class BSDShortInfo(c.Structure):
    _fields_ = [
        ("pid", c.c_uint32),
        ("ppid", c.c_uint32),
        ("pgid", c.c_uint32),
        ("status", c.c_uint32),
        ("comm", c.c_char * 16),
        ("flags", c.c_uint32),
        ("uid", c.c_uint32),
        ("gid", c.c_uint32),
        ("ruid", c.c_uint32),
        ("rgid", c.c_uint32),
        ("svuid", c.c_uint32),
        ("svgid", c.c_uint32),
        ("reserved", c.c_uint32),
    ]


class RUsageV4(c.Structure):
    _fields_ = [("uuid", c.c_uint8 * 16)] + [
        (name, c.c_uint64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
            "child_user_time",
            "child_system_time",
            "child_pkg_idle_wkups",
            "child_interrupt_wkups",
            "child_pageins",
            "child_elapsed_abstime",
            "diskio_bytesread",
            "diskio_byteswritten",
            "cpu_time_qos_default",
            "cpu_time_qos_maintenance",
            "cpu_time_qos_background",
            "cpu_time_qos_utility",
            "cpu_time_qos_legacy",
            "cpu_time_qos_user_initiated",
            "cpu_time_qos_user_interactive",
            "billed_system_time",
            "serviced_system_time",
            "logical_writes",
            "lifetime_max_phys_footprint",
            "instructions",
            "cycles",
            "billed_energy",
            "serviced_energy",
            "interval_max_phys_footprint",
            "runnable_time",
        )
    ]


class VMStatistics64(c.Structure):
    _fields_ = [
        ("free_count", c.c_uint32),
        ("active_count", c.c_uint32),
        ("inactive_count", c.c_uint32),
        ("wire_count", c.c_uint32),
        *[
            (name, c.c_uint64)
            for name in (
                "zero_fill_count",
                "reactivations",
                "pageins",
                "pageouts",
                "faults",
                "cow_faults",
                "lookups",
                "hits",
                "purges",
            )
        ],
        ("purgeable_count", c.c_uint32),
        ("speculative_count", c.c_uint32),
        *[
            (name, c.c_uint64)
            for name in (
                "decompressions",
                "compressions",
                "swapins",
                "swapouts",
            )
        ],
        ("compressor_page_count", c.c_uint32),
        ("throttled_count", c.c_uint32),
        ("external_page_count", c.c_uint32),
        ("internal_page_count", c.c_uint32),
        ("total_uncompressed_pages_in_compressor", c.c_uint64),
        ("swapped_count", c.c_uint64),
    ]


class SwapUsage(c.Structure):
    _fields_ = [
        ("total", c.c_uint64),
        ("avail", c.c_uint64),
        ("used", c.c_uint64),
        ("pagesize", c.c_uint32),
        ("encrypted", c.c_int32),
    ]


def validate_sdk_abi(actual: dict[str, int]) -> None:
    """Check the stable prefix for known 152/160-byte and SDK 27 416-byte revisions."""
    vm_prefix_size = VMStatistics64.total_uncompressed_pages_in_compressor.offset + 8
    expected = {
        "bsdshort_size": c.sizeof(BSDShortInfo),
        "rusage_size": c.sizeof(RUsageV4),
        "footprint_offset": RUsageV4.phys_footprint.offset,
        "vm_logical_offset": VMStatistics64.total_uncompressed_pages_in_compressor.offset,
        "swap_size": c.sizeof(SwapUsage),
    }
    if {key: actual.get(key) for key in expected} != expected:
        raise RuntimeError(f"SDK/ctypes required ABI mismatch: {actual} != {expected}")
    # SDK 27 appends revisions 3-5 while preserving the consumed revisions 1-2 prefix.
    if actual.get("vm_sdk_size") not in (vm_prefix_size, c.sizeof(VMStatistics64), 416):
        raise RuntimeError(f"unsupported SDK vm_statistics64 size: {actual.get('vm_sdk_size')}")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    uid: int
    status: int
    command: str
    start_abstime: int | None = None


@dataclass(frozen=True)
class ProcessMemory:
    footprint_bytes: int
    resident_bytes: int
    start_abstime: int


@dataclass(frozen=True)
class HostMemory:
    physical_bytes: int
    page_size: int
    vm_count: int
    free_bytes: int
    wired_bytes: int
    active_bytes: int
    inactive_bytes: int
    compressor_physical_bytes: int
    compressor_logical_bytes: int
    swapped_logical_bytes: int | None
    swap_used_bytes: int
    swap_total_bytes: int
    pressure_level: int | None
    pressure_unavailable: Unavailable | None
    pressure_error_code: int | None
    speculative_bytes: int | None = None
    file_backed_bytes: int | None = None
    purgeable_bytes: int | None = None

    @property
    def ram_partition(self) -> tuple[int, int, int] | None:
        """Used excluding file-backed, file-backed, and free excluding speculative.

        Native counters can be sampled inconsistently. Do not clamp them into a
        fabricated partition; reserved/unaccounted memory stays inside used.
        """
        values = (
            self.physical_bytes,
            self.free_bytes,
            self.speculative_bytes,
            self.file_backed_bytes,
            self.wired_bytes,
            self.compressor_physical_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values):
            return None
        speculative, backed = self.speculative_bytes, self.file_backed_bytes
        if speculative is None or backed is None or self.physical_bytes <= 0:
            return None
        if self.free_bytes > self.physical_bytes or backed > self.physical_bytes:
            return None
        if speculative > self.free_bytes or speculative > backed:
            return None
        free = self.free_bytes - speculative
        used = self.physical_bytes - free - backed
        if used < self.wired_bytes + self.compressor_physical_bytes:
            return None
        return used, backed, free


def decode_vm(vm: VMStatistics64, count: int, page_size: int) -> dict[str, int | None]:
    """The optional swapped_count suffix is absent from older valid revisions."""
    required = VMStatistics64.total_uncompressed_pages_in_compressor.offset + 8
    if count * 4 < required or count * 4 > c.sizeof(vm) or page_size <= 0:
        raise ValueError(f"invalid HOST_VM_INFO64 response: {count} words, page {page_size}")
    swapped = vm.swapped_count * page_size if count * 4 >= c.sizeof(vm) else None
    return {
        "free": vm.free_count * page_size,
        "speculative": vm.speculative_count * page_size,
        "file_backed": vm.external_page_count * page_size,
        "purgeable": vm.purgeable_count * page_size,
        "wired": vm.wire_count * page_size,
        "active": vm.active_count * page_size,
        "inactive": vm.inactive_count * page_size,
        "compressor_physical": vm.compressor_page_count * page_size,
        "compressor_logical": vm.total_uncompressed_pages_in_compressor * page_size,
        "swapped_logical": swapped,
    }


class DarwinNative:
    """ctypes wrappers with explicit missing-data results and no eager library load."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("Darwin native reader requires macOS")
        self._lib: c.CDLL = c.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self._proc: c.CDLL = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._bind()

    def _bind(self) -> None:
        lib, proc = self._lib, self._proc
        lib.sysctlbyname.argtypes = [
            c.c_char_p,
            c.c_void_p,
            c.POINTER(c.c_size_t),
            c.c_void_p,
            c.c_size_t,
        ]
        lib.sysctlbyname.restype = c.c_int
        lib.host_page_size.argtypes = [c.c_uint32, c.POINTER(c.c_size_t)]
        lib.host_page_size.restype = c.c_int
        lib.host_statistics64.argtypes = [c.c_uint32, c.c_int, c.c_void_p, c.POINTER(c.c_uint32)]
        lib.host_statistics64.restype = c.c_int
        lib.mach_host_self.argtypes = []
        lib.mach_host_self.restype = c.c_uint32
        lib.mach_port_deallocate.argtypes = [c.c_uint32, c.c_uint32]
        lib.mach_port_deallocate.restype = c.c_int
        proc.proc_listpids.argtypes = [c.c_uint32, c.c_uint32, c.c_void_p, c.c_int]
        proc.proc_listpids.restype = c.c_int
        proc.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
        proc.proc_pidinfo.restype = c.c_int
        proc.proc_pidpath.argtypes = [c.c_int, c.c_void_p, c.c_uint32]
        proc.proc_pidpath.restype = c.c_int
        proc.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
        proc.proc_pid_rusage.restype = c.c_int

    def _sysctl(self, name: str, typ: type[SysctlValue]) -> ReadResult[SysctlValue]:
        value = typ()
        size = c.c_size_t(c.sizeof(value))
        c.set_errno(0)
        if self._lib.sysctlbyname(name.encode(), c.byref(value), c.byref(size), None, 0):
            return classify_error(c.get_errno())
        if size.value != c.sizeof(value):
            return ReadResult(None, Unavailable.ERROR)
        return ReadResult(value)

    def host(self) -> ReadResult[HostMemory]:
        mem = self._sysctl("hw.memsize", c.c_uint64)
        swap = self._sysctl("vm.swapusage", SwapUsage)
        if mem.value is None or swap.value is None:
            return ReadResult(
                None, mem.unavailable or swap.unavailable, mem.error_code or swap.error_code
            )
        host = self._lib.mach_host_self()
        if not host:
            return ReadResult(None, Unavailable.ERROR)
        try:
            page = c.c_size_t()
            if self._lib.host_page_size(host, c.byref(page)) or not page.value:
                return ReadResult(None, Unavailable.ERROR)
            vm = VMStatistics64()
            count = c.c_uint32(c.sizeof(vm) // 4)
            if self._lib.host_statistics64(host, 4, c.byref(vm), c.byref(count)):
                return ReadResult(None, Unavailable.ERROR)
            try:
                values = decode_vm(vm, count.value, page.value)
            except ValueError:
                return ReadResult(None, Unavailable.ERROR)
        finally:
            task = self._task_self_port()
            self._lib.mach_port_deallocate(task, host)
        pressure = self._sysctl("kern.memorystatus_vm_pressure_level", c.c_uint32)
        return ReadResult(
            HostMemory(
                physical_bytes=mem.value.value,
                page_size=page.value,
                vm_count=count.value,
                free_bytes=cast(int, values["free"]),
                speculative_bytes=values["speculative"],
                file_backed_bytes=values["file_backed"],
                purgeable_bytes=values["purgeable"],
                wired_bytes=cast(int, values["wired"]),
                active_bytes=cast(int, values["active"]),
                inactive_bytes=cast(int, values["inactive"]),
                compressor_physical_bytes=cast(int, values["compressor_physical"]),
                compressor_logical_bytes=cast(int, values["compressor_logical"]),
                swapped_logical_bytes=values["swapped_logical"],
                swap_used_bytes=swap.value.used,
                swap_total_bytes=swap.value.total,
                pressure_level=pressure.value.value if pressure.value is not None else None,
                pressure_unavailable=pressure.unavailable,
                pressure_error_code=pressure.error_code,
            )
        )

    def _task_self_port(self) -> int:
        return c.c_uint32.in_dll(self._lib, "mach_task_self_").value  # pyright: ignore[reportUnknownArgumentType] - ctypes dynamic DLL

    def pids(self) -> ReadResult[list[int]]:
        c.set_errno(0)
        needed = cast(int, self._proc.proc_listpids(1, 0, None, 0))
        if needed <= 0:
            return classify_error(c.get_errno())
        capacity = min(needed // 4 + 256, 1_000_000)
        entries = (c.c_int32 * capacity)()
        buffer_bytes = capacity * 4
        c.set_errno(0)
        size = self._proc.proc_listpids(1, 0, entries, buffer_bytes)
        if size <= 0:
            return classify_error(c.get_errno())
        if size % 4 or size >= buffer_bytes:
            return ReadResult(None, Unavailable.ERROR)
        return ReadResult([pid for pid in entries[: size // 4] if pid > 0])

    def process(self, pid: int) -> ReadResult[ProcessIdentity]:
        info = BSDShortInfo()
        c.set_errno(0)
        size = self._proc.proc_pidinfo(pid, 13, 0, c.byref(info), c.sizeof(info))
        if size <= 0:
            return classify_error(c.get_errno(), pid=True)
        if size != c.sizeof(info):
            return ReadResult(None, Unavailable.ERROR)
        if info.pid != pid:
            return ReadResult(None, Unavailable.ERROR)
        return ReadResult(
            ProcessIdentity(
                pid,
                info.ppid,
                info.uid,
                info.status,
                info.comm.split(b"\0", 1)[0].decode(errors="replace"),
            )
        )

    def path(self, pid: int) -> ReadResult[str]:
        buf = c.create_string_buffer(4096)
        c.set_errno(0)
        length = self._proc.proc_pidpath(pid, buf, c.sizeof(buf))
        if length <= 0:
            return classify_error(c.get_errno(), pid=True)
        if length >= c.sizeof(buf) or b"\0" not in buf.raw[: length + 1]:
            return ReadResult(None, Unavailable.ERROR)
        return ReadResult(buf.value.decode(errors="surrogateescape"))

    def memory(self, pid: int) -> ReadResult[ProcessMemory]:
        usage = RUsageV4()
        c.set_errno(0)
        if self._proc.proc_pid_rusage(pid, 4, c.byref(usage)):
            return classify_error(c.get_errno(), pid=True)
        return ReadResult(
            ProcessMemory(usage.phys_footprint, usage.resident_size, usage.proc_start_abstime)
        )
