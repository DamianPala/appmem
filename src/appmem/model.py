"""Shared immutable collection values and pure grouping math."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SystemStats:
    """System-wide memory, swap, pressure and hidden system.slice totals."""

    mem_total: int
    mem_available: int
    swap_total: int
    swap_free: int
    pressure_some_avg10: float | None
    pressure_some_avg60: float | None
    pressure_full_avg10: float | None
    pressure_full_avg60: float | None
    system_ram: int
    system_swap: int
    elsewhere: int | None
    """Charged memory outside the walked trees (header `elsewhere` token).
    `None` when the root cgroup's `memory.stat` is missing (SPEC.md "Behaviour
    details"); the header omits the token below 1 MiB too, on the formatted
    value, not here."""
    mem_free: int = 0
    """`/proc/meminfo` `MemFree`: truly free RAM, no cache or shared memory in it."""
    mem_shared: int = 0
    """`/proc/meminfo` `Shmem`: tmpfs, shared memory segments and GPU buffers --
    memory the kernel can only swap out, never just drop."""
    mem_cache: int = 0
    """Reclaimable page cache: `Cached - Shmem`, clamped at 0 -- the same
    definition as the per-app CACHE column (`file - shmem`)."""
    mem_slab: int = 0
    """`/proc/meminfo` `SReclaimable`: kernel caches of file names and inodes
    (dentries, inodes), reclaimable on demand -- the third part of the header
    `avail` breakdown, alongside `free` and `cache`."""
    zswap_enabled: bool = False
    """`/sys/module/zswap/parameters/enabled` is `Y` and `/proc/meminfo` has
    the `Zswap`/`Zswapped` fields (a missing file or missing fields both mean
    "off": no knob, or no support to turn on). Gates the header's whole zswap
    bracket and the per-app ZSWAP column (SPEC.md "Main view")."""
    zswap_pool_bytes: int | None = None
    """`/proc/meminfo` `Zswap`: RAM the compressed pool itself costs. `None`
    whenever `zswap_enabled` is `False`."""
    zswapped_bytes: int | None = None
    """`/proc/meminfo` `Zswapped`: swapped data kept compressed in the pool,
    uncompressed size -- already part of `SWAP` used. `None` whenever
    `zswap_enabled` is `False`."""
    zswap_writeback_bytes: int | None = None
    """`/proc/vmstat` `zswpwb` (pages written back from the pool to disk
    swap) times the page size, cumulative since boot. `None` when the kernel
    has no `zswpwb` counter at all. A single snapshot has no rate of its own;
    the live view turns two ticks of this into the `to disk` MiB/s token,
    and an agent can do the same by diffing two snapshots."""
    zswap_compressor: str | None = None
    """`/sys/module/zswap/parameters/compressor` (e.g. `lzo`, `zstd`). `None`
    whenever `zswap_enabled` is `False`."""
    zswap_max_pool_percent: int | None = None
    """`/sys/module/zswap/parameters/max_pool_percent`: the pool's cap, as a
    percentage of total RAM. `None` whenever `zswap_enabled` is `False`."""
    zswap_compression_ratio: float | None = None
    """`zswapped_bytes / zswap_pool_bytes`: how much the pool shrinks what it
    holds. `None` whenever `zswap_enabled` is `False`, or either side is 0
    (nothing compressed yet, or the pool read as empty)."""


@dataclass(frozen=True)
class AppStats:
    """Counters for an app: one or more units merged by `unit_app_name`."""

    name: str
    ram: int
    cache: int
    swap: int
    total: int
    procs: int
    unit_paths: tuple[Path, ...]
    kernel: int = 0
    """Sum of the merged units' `kernel` (already excludes `zswap_pool`, see
    `UnitStats.kernel`)."""
    scope: str = "user"
    zswapped: int = 0
    """Sum of the merged units' `zswapped` (SPEC.md "Definitions"), already
    included in `swap`."""
    zswap_pool: int = 0
    """Sum of the merged units' `zswap_pool` (see `UnitStats.zswap_pool`),
    already included in `ram`."""


@dataclass(frozen=True)
class ProcStats:
    """Counters for a single process (SPEC.md "Data sources")."""

    pid: int
    name: str
    swap: int
    ram: int
    age_seconds: float
    unit: str


@dataclass(frozen=True)
class CommandStats:
    """Process rows summed by command name (process view "group by command")."""

    name: str
    swap: int
    ram: int
    count: int
    units: tuple[str, ...]
    """Sorted, de-duplicated unit names the group's processes belong to:
    a command usually lives in one unit (the terminal it was started from),
    but can legitimately span several (the same shell command run in two
    terminal windows)."""


def filter_visible_apps(apps: Iterable[AppStats]) -> list[AppStats]:
    """Hide rows with TOTAL < 1 MiB (SPEC.md "Behaviour details"). CACHE is excluded."""
    threshold = 1024 * 1024
    return [app for app in apps if app.total >= threshold]


def unattributed_row(app: AppStats, procs: Iterable[ProcStats]) -> tuple[int, int]:
    """SWAP/RAM the app holds without a matching process, its own kernel
    share or its zswap pool share, clamped at 0 each (SPEC.md "Definitions";
    the process view's `unattributed` row -- an accounting difference, not a
    process)."""
    procs = list(procs)
    swap = max(app.swap - sum(p.swap for p in procs), 0)
    ram = max(app.ram - sum(p.ram for p in procs) - app.kernel - app.zswap_pool, 0)
    return swap, ram


def group_by_command(procs: Iterable[ProcStats]) -> list[CommandStats]:
    """Sum SWAP/RAM/count per process name (process view "group by command")."""
    groups: dict[str, list[ProcStats]] = {}
    for proc in procs:
        groups.setdefault(proc.name, []).append(proc)
    return [
        CommandStats(
            name=name,
            swap=sum(p.swap for p in group),
            ram=sum(p.ram for p in group),
            count=len(group),
            units=tuple(sorted({p.unit for p in group})),
        )
        for name, group in groups.items()
    ]
