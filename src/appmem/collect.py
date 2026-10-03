"""Reads `/sys/fs/cgroup` and `/proc` and returns plain dataclasses.

No UI imports here. Every reader takes a `root: Path` standing for `/`
(production passes `Path("/")`), so tests point it at fixture trees and
never touch the live filesystem. See SPEC.md "Data sources", "Finding
units", "Definitions" and "Behaviour details" for the source contract.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from appmem.command_name import command_display_name
from appmem.model import AppStats, ProcStats, SystemStats, filter_visible_apps
from appmem.naming import app_name, is_generic_desktop_id, normalize_process_name, scope_leader_pid

_REQUIRED_MEMORY_STAT_KEYS = ("anon", "shmem", "file")
_KERNEL_FALLBACK_KEYS = ("slab", "kernel_stack", "pagetables", "percpu")
_MEMORY_STAT_KEYS = frozenset(
    {"anon", "shmem", "kernel", "file", "zswapped", "zswap", *_KERNEL_FALLBACK_KEYS}
)
_USER_SLICES = ("app.slice", "session.slice", "background.slice")
_STATUS_KEYS = ("VmSwap", "RssAnon", "RssShmem")


class CgroupUnavailableError(Exception):
    """Raised when the user's cgroup v2 tree directory itself is missing (or,
    in `strict` mode, when its `memory.stat` can't be parsed either).

    The message names the specific missing path, so the CLI slice can map
    it to the `cgroup_unavailable` error kind.
    """


class MemoryStatUnavailableError(Exception):
    """Raised by a non-`strict` `find_units`/`find_app_units` call when the
    user's tree directory exists but its `memory.stat` couldn't be parsed
    this tick (e.g. read mid-write). Transient, unlike `CgroupUnavailableError`:
    callers should skip the tick and keep the last data on screen, not treat
    the session as over.
    """


@dataclass(frozen=True)
class UnitStats:
    """Counters for a single cgroup unit (SPEC.md "Definitions")."""

    ram: int
    cache: int
    swap: int
    total: int
    procs: int
    kernel: int = 0
    """The app's charged kernel memory (page tables, slab, stacks), **excluding**
    the zswap pool (`zswap_pool` below); both are already included in `ram`.
    Carried separately so the process view can show it as its own row
    (SPEC.md "Definitions": RAM = anon + shmem + kernel)."""
    zswapped: int = 0
    """`memory.stat` `zswapped`: this unit's swapped data held compressed in
    the pool, already included in `swap` -- not extra memory (SPEC.md
    "Definitions")."""
    zswap_pool: int = 0
    """`memory.stat` `zswap`: the RAM this unit's own share of the compressed
    pool costs. On kernels that report it, this is charged inside
    `memory.stat`'s own `kernel` field (confirmed by live measurement), so
    it has already been split back out of
    `kernel` above -- `kernel + zswap_pool` equals that raw figure, and `ram`
    (computed from the raw, unsplit figure) is unchanged either way."""


@dataclass(frozen=True)
class Unit:
    """A cgroup unit path paired with its counters, ready for grouping."""

    path: Path
    stats: UnitStats
    scope: str = "user"
    """"user" or "system" (SPEC.md "Grouping"): which root `find_units` found
    this unit under. Part of app identity, alongside the app name."""


# --- system ---------------------------------------------------------------


def read_system(
    root: Path,
    uid: int,
    *,
    page_size: int | None = None,
    swap_disk_only: bool | None = None,
) -> SystemStats:
    """Read system-wide memory/swap/pressure, the hidden system.slice total and
    the `elsewhere` figure (SPEC.md "Behaviour details"). `uid` locates the
    user tree for the `elsewhere` subtraction. `swap_disk_only=None` reads
    `/proc/swaps` now; `LinuxBackend` passes a cached answer instead."""
    meminfo = _read_meminfo(root / "proc" / "meminfo")
    some_avg10, some_avg60, full_avg10, full_avg60 = _read_pressure(
        root / "proc" / "pressure" / "memory"
    )
    # The header never shows a system procs count, so skip the costly recursive count.
    system_slice = os.path.join(str(root), "sys", "fs", "cgroup", "system.slice")
    system_stats = _read_unit_stats(system_slice, count_procs=False)
    shmem = meminfo.get("Shmem", 0)
    cached = meminfo.get("Cached", 0)
    zswap_enabled = (
        _read_zswap_enabled(root / "sys" / "module" / "zswap" / "parameters" / "enabled")
        and "Zswap" in meminfo
        and "Zswapped" in meminfo
    )
    vmstat = _read_vmstat(root / "proc" / "vmstat")
    page_size = os.sysconf("SC_PAGE_SIZE") if page_size is None else page_size
    zswap_params_dir = root / "sys" / "module" / "zswap" / "parameters"
    zswap_pool_bytes = meminfo.get("Zswap") if zswap_enabled else None
    zswapped_bytes = meminfo.get("Zswapped") if zswap_enabled else None
    return SystemStats(
        mem_total=meminfo.get("MemTotal", 0),
        mem_available=meminfo.get("MemAvailable", 0),
        swap_total=meminfo.get("SwapTotal", 0),
        swap_free=meminfo.get("SwapFree", 0),
        pressure_some_avg10=some_avg10,
        pressure_some_avg60=some_avg60,
        pressure_full_avg10=full_avg10,
        pressure_full_avg60=full_avg60,
        system_ram=system_stats.ram if system_stats else 0,
        system_swap=system_stats.swap if system_stats else 0,
        elsewhere=_read_elsewhere(root, uid, system_stats),
        mem_free=meminfo.get("MemFree", 0),
        mem_shared=shmem,
        mem_cache=max(cached - shmem, 0),
        mem_slab=meminfo.get("SReclaimable", 0),
        zswap_enabled=zswap_enabled,
        zswap_pool_bytes=zswap_pool_bytes,
        zswapped_bytes=zswapped_bytes,
        zswap_writeback_bytes=_counter_bytes(vmstat, "zswpwb", page_size),
        swap_in_bytes=_counter_bytes(vmstat, "pswpin", page_size),
        swap_out_bytes=_counter_bytes(vmstat, "pswpout", page_size),
        swap_disk_only=(
            _read_swap_disk_only(root / "proc" / "swaps")
            if swap_disk_only is None
            else swap_disk_only
        ),
        zswap_compressor=(
            _read_zswap_str_param(zswap_params_dir / "compressor") if zswap_enabled else None
        ),
        zswap_max_pool_percent=(
            _read_zswap_int_param(zswap_params_dir / "max_pool_percent") if zswap_enabled else None
        ),
        zswap_compression_ratio=_zswap_compression_ratio(zswap_pool_bytes, zswapped_bytes),
    )


def _read_swap_disk_only(path: Path) -> bool:
    """Fail closed on unknown topology; never infer historical storage targets."""
    try:
        lines = _read_small_file(str(path), until_eof=True).splitlines()
    except OSError:
        return False
    if not lines or lines[0].split() != ["Filename", "Type", "Size", "Used", "Priority"]:
        return False
    entries = [line.split() for line in lines[1:] if line.strip()]
    return bool(entries) and all(_disk_swap_entry(entry) for entry in entries)


def _disk_swap_entry(entry: list[str]) -> bool:
    if len(entry) != 5:
        return False
    name, kind, capacity, used, priority = entry
    if re.fullmatch(r"[0-9]+ [0-9]+ -?[0-9]+", f"{capacity} {used} {priority}") is None:
        return False
    if int(capacity) <= 0 or int(used) > int(capacity):
        return False
    if kind == "file":
        return name.startswith("/") and not name.startswith("/dev/")
    # Aliases/device-mapper targets need mapping knowledge; keep their wording generic.
    return (
        kind == "partition"
        and re.fullmatch(
            r"/dev/(?:[shv]d[a-z]+[0-9]*|xvd[a-z]+[0-9]*|nvme[0-9]+n[0-9]+(?:p[0-9]+)?)", name
        )
        is not None
    )


def _zswap_compression_ratio(pool_bytes: int | None, zswapped_bytes: int | None) -> float | None:
    if not pool_bytes or not zswapped_bytes:  # None or 0 either side: nothing to divide
        return None
    return zswapped_bytes / pool_bytes


def _read_elsewhere(root: Path, uid: int, system_stats: UnitStats | None) -> int | None:
    """Charged memory outside the walked trees: the root cgroup's total minus
    the user tree minus `system.slice`, clamped at 0 (SPEC.md "Behaviour
    details"). `None` when the root `memory.stat` is missing or incomplete."""
    cgroup_root = os.path.join(str(root), "sys", "fs", "cgroup")
    root_ram = _read_ram(os.path.join(cgroup_root, "memory.stat"))
    if root_ram is None:
        return None
    user_root = os.path.join(cgroup_root, "user.slice", f"user-{uid}.slice", f"user@{uid}.service")
    user_ram = _read_ram(os.path.join(user_root, "memory.stat")) or 0
    system_ram = system_stats.ram if system_stats else 0
    return max(root_ram - user_ram - system_ram, 0)


def _read_meminfo(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        content = _read_small_file(str(path))
    except FileNotFoundError:
        return result
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith("kB"):
            rest = rest[:-2].strip()
        if rest.isdigit():
            result[key] = int(rest) * 1024
    return result


def _read_pressure(
    path: Path,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return `(some avg10, some avg60, full avg10, full avg60)`."""
    try:
        content = _read_small_file(str(path))
    except FileNotFoundError:
        return None, None, None, None
    values: dict[str, dict[str, float]] = {}
    for line in content.splitlines():
        kind, _, rest = line.partition(" ")
        fields: dict[str, float] = {}
        for item in rest.split():
            key, _, value = item.partition("=")
            if key in ("avg10", "avg60"):
                fields[key] = float(value)
        values[kind] = fields
    some = values.get("some", {})
    full = values.get("full", {})
    return some.get("avg10"), some.get("avg60"), full.get("avg10"), full.get("avg60")


def _read_zswap_enabled(path: Path) -> bool:
    """`Y`/`N` from `/sys/module/zswap/parameters/enabled`; a missing file
    (no zswap module, or an ancient kernel) reads as `N` (SPEC.md "Data
    sources")."""
    try:
        return _read_small_file(str(path)).strip() == "Y"
    except FileNotFoundError:
        return False


def _read_zswap_str_param(path: Path) -> str | None:
    """A plain-text `/sys/module/zswap/parameters/*` value (e.g. `compressor`),
    or `None` if the file doesn't exist (SPEC.md "Data sources")."""
    try:
        value = _read_small_file(str(path)).strip()
    except FileNotFoundError:
        return None
    return value or None


def _read_zswap_int_param(path: Path) -> int | None:
    """An integer `/sys/module/zswap/parameters/*` value (e.g.
    `max_pool_percent`), or `None` if the file is missing or not a plain
    integer."""
    try:
        value = _read_small_file(str(path)).strip()
    except FileNotFoundError:
        return None
    return int(value) if value.lstrip("-").isdigit() else None


def _counter_bytes(values: dict[str, int], key: str, page_size: int) -> int | None:
    value = values.get(key)
    return value * page_size if value is not None and page_size > 0 else None


def _read_vmstat(path: Path) -> dict[str, int]:
    """Read page counters once, through EOF for multi-record seq_file responses."""
    try:
        content = _read_small_file(str(path), until_eof=True)
    except OSError:
        return {}
    values: dict[str, int] = {}
    for line in content.splitlines():
        fields = line.split()
        if (
            len(fields) == 2
            and fields[0] in {"pswpin", "pswpout", "zswpwb"}
            and fields[1].isascii()
            and fields[1].isdigit()
        ):
            values[fields[0]] = int(fields[1])
    return values


# --- finding units ----------------------------------------------------------


_UNIT_PATH_CACHE: dict[str, Path] = {}
"""Reuses `Path` objects across `find_units` calls for units that still exist,
so `str(path)` in `read_unit`/the main screen's app-cell formatting hits
`Path`'s own cached string instead of rebuilding a fresh `Path` (and
recomputing its string) for every unit on every tick (SPEC.md "Tech": a
busy desktop has ~150 units). Replaced wholesale, not appended to, on every
`find_units` call, so a unit that vanished is dropped from the cache instead
of leaking forever. Module-level state shared by the UI's tick threads (main
and process view) and the synchronous refresh paths: two overlapping calls
can at worst leave the union of two current unit sets, which the next call
replaces, or cost an extra `Path()` rebuild; never wrong data (no stats are
cached here, only the `Path`, keyed by its full string, so any root works)."""


def _cached_unit_paths(unit_dirs: list[str]) -> list[Path]:
    paths = {unit_dir: _UNIT_PATH_CACHE.get(unit_dir) or Path(unit_dir) for unit_dir in unit_dirs}
    _UNIT_PATH_CACHE.clear()
    _UNIT_PATH_CACHE.update(paths)
    return [paths[unit_dir] for unit_dir in unit_dirs]


def find_units(root: Path, uid: int, include_system: bool, *, strict: bool = True) -> list[Path]:
    """Find unit directories under the user's cgroup tree (SPEC.md "Finding units").

    Always raises `CgroupUnavailableError` when the user's `user@$UID.service`
    tree directory itself is missing.

    `strict` (default `True`, the pre-start check in `cli.py`) also raises
    `CgroupUnavailableError` when that tree's `memory.stat` can't be parsed
    (memory controller not enabled there). A per-tick caller passes
    `strict=False`: the same parse failure there is usually transient (e.g. a
    read mid-write), so it raises the lighter `MemoryStatUnavailableError`
    instead, which callers treat as "skip this tick" rather than fatal.
    """
    cgroup_root = os.path.join(str(root), "sys", "fs", "cgroup")
    user_root = os.path.join(cgroup_root, "user.slice", f"user-{uid}.slice", f"user@{uid}.service")
    if not os.path.isdir(user_root):
        raise CgroupUnavailableError(f"missing cgroup path: {user_root}")
    memory_stat = os.path.join(user_root, "memory.stat")
    # Parses the file rather than just checking it exists, so a controller
    # enabled without anon/shmem/file (memory accounting not really on) fails
    # here instead of showing an empty table.
    if _read_ram(memory_stat) is None:
        if strict:
            raise CgroupUnavailableError(f"missing cgroup path: {memory_stat}")
        raise MemoryStatUnavailableError(f"unreadable this tick: {memory_stat}")

    unit_paths: list[str] = []
    for slice_name in _USER_SLICES:
        unit_paths.extend(_walk_slice(os.path.join(user_root, slice_name)))
    if include_system:
        unit_paths.extend(_walk_slice(os.path.join(cgroup_root, "system.slice")))
    return _cached_unit_paths(unit_paths)


def _walk_slice(slice_dir: str) -> list[str]:
    """Descend through `*.slice` dirs, stopping at the first unit (never below it).

    Plain str + `os.scandir` (one call per slice, `DirEntry.is_dir()` instead of a
    `Path.is_dir()` stat per entry): a busy desktop has ~250 cgroup dirs, and the
    per-entry `pathlib` stat calls were a measurable share of the tick cost.
    """
    try:
        with os.scandir(slice_dir) as it:
            listed = sorted(it, key=lambda e: e.name)
    except OSError:  # Missing, or vanished between the check and the listing.
        return []
    units: list[str] = []
    for entry in listed:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:  # Entry vanished mid-scan.
            continue
        if not is_dir:
            continue
        if entry.name.endswith(".slice"):
            units.extend(_walk_slice(entry.path))
        elif entry.name.endswith((".service", ".scope")):
            units.append(entry.path)
        # else: *.socket, *.mount, *.swap or anything else is ignored.
    return units


def unit_scope(root: Path, unit_path: Path) -> str:
    """ "user" or "system": which root `find_units` found `unit_path` under
    (SPEC.md "Finding units"). Part of app identity (SPEC.md "Grouping")."""
    # A string prefix test, not `Path.relative_to`: this runs for every unit on
    # every tick, and `relative_to` measured ~15x slower (~3 ms per tick on 189 units).
    system_slice = os.path.join(str(root), "sys", "fs", "cgroup", "system.slice") + os.sep
    return "system" if str(unit_path).startswith(system_slice) else "user"


def unit_app_name(root: Path, unit_path: Path) -> str:
    """The app name for one unit (SPEC.md "Grouping"), used by both
    `group_apps` and `find_app_units` so they agree on one name per unit.

    `app_name(unit_path.name)` for almost every unit. A generic desktop id
    (only `org.chromium.Chromium` for now) is named after its leader process
    instead: Electron apps and Chromium forks without an id of their own all
    report the same id to the compositor, so the unit name alone can't tell
    them apart. Cheap for the common case (pure string work); the extra
    `/proc` read only happens for a generic unit, a handful at most.
    """
    unit_name = unit_path.name
    if not is_generic_desktop_id(unit_name):
        return app_name(unit_name)
    leader_name = _generic_leader_name(root, unit_path)
    if leader_name is None:
        return app_name(unit_name)
    return normalize_process_name(leader_name)


_MAX_LEADER_PID_ATTEMPTS = 3
"""Cap on how many pids `_generic_leader_name` reads `/proc` for: a generic
unit is rare and its pid list short, but a unit full of dead pids must still
cost only a handful of reads, not one per process."""


def _generic_leader_name(root: Path, unit_path: Path) -> str | None:
    """The leader process name for a generic-desktop-id unit (SPEC.md
    "Grouping"): the pid in the scope name, but only while it still belongs
    to the unit (a scope can outlive the process it was named after, and
    after pid wrap-around that pid can be anyone); otherwise the lowest pid
    in the unit's own `cgroup.procs` whose name can be read. `None` when
    nothing in the unit can be read within `_MAX_LEADER_PID_ATTEMPTS` tries.
    """
    pids = _pids_under(str(unit_path))
    scope_pid = scope_leader_pid(unit_path.name)
    candidates = [scope_pid] if scope_pid is not None and scope_pid in pids else []
    candidates += [pid for pid in pids if pid != scope_pid]
    for pid in candidates[:_MAX_LEADER_PID_ATTEMPTS]:
        name = _read_proc_name_or_none(root, pid)
        if name is not None:
            return name
    return None


def _read_proc_name_or_none(root: Path, pid: int) -> str | None:
    try:
        return _read_proc_name(root / "proc" / str(pid))
    except (OSError, IndexError, ValueError):
        return None


def find_app_units(
    root: Path, uid: int, include_system: bool, scope: str, name: str, *, strict: bool = True
) -> list[Path]:
    """Unit paths for one app identity, using the same naming/scope rules as
    `group_apps`. The process view calls this every tick (with `strict=False`,
    see `find_units`) instead of reusing a unit list captured once at Enter,
    so units added or replaced while it's open are picked up (SPEC.md
    "Process view"). Cheap: a directory walk plus pure-string naming; the
    only `memory.stat` read is `find_units`' check of the user root.
    """
    return [
        path
        for path in find_units(root, uid, include_system=include_system, strict=strict)
        if unit_scope(root, path) == scope and unit_app_name(root, path) == name
    ]


# --- unit counters ----------------------------------------------------------


def read_unit(path: Path, *, count_procs: bool = True) -> UnitStats | None:
    """Read a unit's counters. Returns `None` if the unit vanished mid-read.

    `count_procs=False` skips the recursive `cgroup.procs` walk (`procs` comes
    back `0`) for a caller that carries a previous count forward instead of
    reading it fresh this tick (SPEC.md "Main view": PROCS in the live main
    view refreshes every 5th tick, the memory columns every tick)."""
    return _read_unit_stats(str(path), count_procs=count_procs)


def _read_unit_stats(unit_dir: str, *, count_procs: bool = True) -> UnitStats | None:
    try:
        stat = _read_memory_stat(os.path.join(unit_dir, "memory.stat"))
        if stat is None:
            return None
        swap = _read_swap_current(os.path.join(unit_dir, "memory.swap.current"))
        procs = _count_procs(unit_dir) if count_procs else 0
    # OSError covers ENOENT and the ENODEV a removed cgroup's open file returns;
    # ValueError covers a truncated memory.swap.current.
    except (OSError, ValueError):
        return None
    anon = stat["anon"]
    shmem = stat["shmem"]
    kernel_raw = stat["kernel"]  # may already include the zswap pool, see zswap_pool below
    file_ = stat["file"]
    ram = anon + shmem + kernel_raw
    cache = file_ - shmem
    total = swap + ram
    zswapped = stat.get("zswapped", 0)  # absent on a kernel/cgroup without zswap accounting
    # `zswap` (the pool's own RAM cost) is charged inside `kernel` on kernels
    # that report it there (confirmed by live measurement); split it back out so
    # the process view can show it as its own row. `ram` above stays computed
    # from the raw, unsplit `kernel_raw`, so RAM itself never changes.
    zswap_pool = min(stat.get("zswap", 0), kernel_raw)
    kernel = kernel_raw - zswap_pool
    return UnitStats(
        ram=ram,
        cache=cache,
        swap=swap,
        total=total,
        procs=procs,
        kernel=kernel,
        zswapped=zswapped,
        zswap_pool=zswap_pool,
    )


_READ_CHUNK_SIZE = 65536


def _read_small_file_bytes(path: str, *, until_eof: bool = False) -> bytes:
    """Raw bytes underneath `_read_small_file`, for the one reader
    (`_read_proc_name`) that must see NUL bytes rather than a decoded string.

    The kernel serves two different shapes here, and they need different read
    strategies:

    - **Single-record files** (`single_open`: `/proc/meminfo`, `/proc/pressure/*`,
      `/proc/uptime`, `/proc/PID/{status,stat,comm,smaps_rollup}`; cgroup files
      with no `seq_start` of their own, such as `memory.stat` and
      `memory.swap.current`; sysfs attributes) build their whole answer in one
      buffer sized to fit, and always come back complete from a single
      `os.read`, however large that read's request. One call is the default
      here: open/read/close, not open/read/read/close -- the second call used
      to be a pure EOF probe for a quarter of the collector's reads.
    - **Multi-record seq_file files** (`/proc/vmstat`, `cgroup.procs`) hand back
      at most one seq buffer -- one page, 4096 bytes on this kernel -- per
      `read()` call, no matter how big a buffer is requested; a short first
      read here does not mean EOF. A single read of one of these silently
      truncates instead of raising, since the file itself isn't exhausted,
      only this read of it. Their two callers (`_read_vmstat`,
      `_iter_procs_content`) pass `until_eof=True`, which instead loops until
      an empty read confirms EOF: open/read/read/close for a file under one
      page (the same as every read cost before this split), plus one read per
      further page.

    Bypasses the buffered `TextIOWrapper` `open()` sets up for every call,
    which measurably matters here too: the collector opens a few hundred of
    these files per tick.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks: list[bytes] = [os.read(fd, _READ_CHUNK_SIZE)]
        while chunks[-1] and (until_eof or len(chunks[-1]) == _READ_CHUNK_SIZE):
            chunks.append(os.read(fd, _READ_CHUNK_SIZE))
    finally:
        os.close(fd)
    return b"".join(chunks)


def _read_small_file(path: str, *, until_eof: bool = False) -> str:
    """Read a small proc/cgroup file with raw `os.open`/`os.read` (see
    `_read_small_file_bytes` for the single-read-vs-`until_eof` split),
    decoded as UTF-8 with replacement."""
    return _read_small_file_bytes(path, until_eof=until_eof).decode("utf-8", errors="replace")


def _read_memory_stat(path: str) -> dict[str, int] | None:
    """Parse `memory.stat`. `anon`/`shmem`/`file` are required on every kernel;
    returns `None` if any is missing (unit vanished mid-write, or the memory
    controller isn't really enabled there).

    `kernel` was added in Linux 5.18; when absent, it falls back to
    `slab + kernel_stack + pagetables + percpu` (each part optional, 0 if
    missing), so older kernels (5.15/Ubuntu 22.04, 5.10/Debian 11, 5.14/RHEL 9)
    still read a usable RAM figure instead of every unit reading as vanished.
    """
    content = _read_small_file(path)  # OSError propagates: unit vanished.
    values: dict[str, int] = {}
    for line in content.splitlines():
        key, _, value = line.partition(" ")
        if key in _MEMORY_STAT_KEYS and value.strip().lstrip("-").isdigit():
            values[key] = int(value.strip())
            if len(values) == len(_MEMORY_STAT_KEYS):
                break  # Found everything we could use; the rest of the file doesn't matter.
    if not all(key in values for key in _REQUIRED_MEMORY_STAT_KEYS):
        return None
    if "kernel" not in values:
        values["kernel"] = sum(values.get(key, 0) for key in _KERNEL_FALLBACK_KEYS)
    return values


def _read_ram(path: str) -> int | None:
    """`anon + shmem + kernel` from a `memory.stat` file, or `None` if missing
    or incomplete (SPEC.md "Definitions"; used by the pre-start check and the
    header `elsewhere` token)."""
    try:
        stat = _read_memory_stat(path)
    except OSError:
        return None
    if stat is None:
        return None
    return stat["anon"] + stat["shmem"] + stat["kernel"]


def _read_swap_current(path: str) -> int:
    try:
        return int(_read_small_file(path).strip())
    except FileNotFoundError:
        return 0


def _count_procs(unit_dir: str) -> int:
    """Sum `cgroup.procs` line counts for the unit and every dir below it."""
    total = 0
    for content in _iter_procs_content(unit_dir):
        total += sum(1 for line in content.splitlines() if line.strip())
    return total


def _may_have_subdirs(cgroup_dir: str) -> bool:
    """False only when the cgroup dir surely has no immediate child directory.

    On cgroupfs (and tmpfs, ext4) a directory's link count is 2 plus its number of
    immediate subdirectories, so a single `stat()` answers "any children?" without
    opening anything. btrfs and some other filesystems report 1 for every
    directory, so only exactly 2 means "leaf"; any other count falls back to a scan.
    """
    try:
        return os.stat(cgroup_dir).st_nlink != 2
    except OSError:  # Cgroup removed mid-walk.
        return False


def _iter_procs_content(cgroup_dir: str) -> Iterator[str]:
    """Yield the `cgroup.procs` content of a cgroup dir and of every dir below it.

    Plain str + `os.path`/`os.scandir`: a busy desktop has ~250 cgroup dirs (one
    Konsole window alone keeps 101 `tab(...).scope` children), and building a
    `Path` per entry was most of the tick cost. Skips the subdirectory scan
    entirely for a leaf dir (no children), which is most units.

    `until_eof=True`: `cgroup.procs` is a multi-record seq_file, served one
    page per `read()` (`_read_small_file_bytes`) -- a busy unit's PID list can
    run past that (about 500 PIDs, a `make -j` in a terminal scope gets
    there), and a single read would silently drop the rest.
    """
    try:
        yield _read_small_file(os.path.join(cgroup_dir, "cgroup.procs"), until_eof=True)
    except OSError:  # Cgroup removed mid-walk: ENOENT, or ENODEV for an open handle.
        return

    if not _may_have_subdirs(cgroup_dir):
        return

    try:
        with os.scandir(cgroup_dir) as entries:
            subdirs = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for subdir in subdirs:
        yield from _iter_procs_content(subdir)


# --- grouping ----------------------------------------------------------------


def group_apps(root: Path, units: Iterable[Unit]) -> list[AppStats]:
    """Merge units by `(scope, unit_app_name)`, summing counters (SPEC.md
    "Grouping"). A user and a system unit that normalize to the same name stay
    two rows (SPEC.md "Grouping": app identity is scope + name)."""
    groups: dict[tuple[str, str], list[Unit]] = {}
    for unit in units:
        name = unit_app_name(root, unit.path)
        groups.setdefault((unit.scope, name), []).append(unit)

    apps: list[AppStats] = []
    for (scope, name), unit_list in groups.items():
        apps.append(
            AppStats(
                name=name,
                scope=scope,
                ram=sum(u.stats.ram for u in unit_list),
                cache=sum(u.stats.cache for u in unit_list),
                swap=sum(u.stats.swap for u in unit_list),
                total=sum(u.stats.total for u in unit_list),
                procs=sum(u.stats.procs for u in unit_list),
                kernel=sum(u.stats.kernel for u in unit_list),
                zswapped=sum(u.stats.zswapped for u in unit_list),
                zswap_pool=sum(u.stats.zswap_pool for u in unit_list),
                unit_paths=tuple(u.path for u in unit_list),
            )
        )
    return apps


@dataclass
class _SwapTopology:
    """`/proc/swaps` only picks a sentence in the host panel and changes only on
    swapon/swapoff, which also move SwapTotal: it is read on the first sample and
    again when SwapTotal changes, not on every tick."""

    swap_total: int | None = None
    disk_only: bool = False


@dataclass(frozen=True)
class LinuxBackend:
    """Linux cgroup collection with a fixture-injectable filesystem root."""

    root: Path
    uid: int
    _swap_topology: _SwapTopology = field(default_factory=_SwapTopology, repr=False, compare=False)

    def check(self) -> None:
        find_units(self.root, self.uid, include_system=False, strict=True)

    def read_system(self) -> SystemStats:
        topology = self._swap_topology
        stats = read_system(self.root, self.uid, swap_disk_only=topology.disk_only)
        if stats.swap_total != topology.swap_total:
            topology.swap_total = stats.swap_total
            topology.disk_only = _read_swap_disk_only(self.root / "proc" / "swaps")
            stats = replace(stats, swap_disk_only=topology.disk_only)
        return stats

    def collect_apps(
        self,
        *,
        include_system: bool,
        strict: bool,
        count_procs: bool,
        previous_procs: Mapping[str, int],
    ) -> tuple[list[AppStats], dict[str, int]]:
        paths = find_units(self.root, self.uid, include_system=include_system, strict=strict)
        units: list[Unit] = []
        procs_by_unit: dict[str, int] = {}
        for path in paths:
            key = str(path)
            recount = count_procs or key not in previous_procs
            stats = read_unit(path, count_procs=recount)
            if stats is None:
                continue
            if not recount:
                stats = replace(stats, procs=previous_procs[key])
            procs_by_unit[key] = stats.procs
            units.append(Unit(path=path, stats=stats, scope=unit_scope(self.root, path)))
        return filter_visible_apps(group_apps(self.root, units)), procs_by_unit

    def find_app(self, scope: str, name: str, *, strict: bool) -> AppStats | None:
        paths = find_app_units(
            self.root,
            self.uid,
            include_system=(scope == "system"),
            scope=scope,
            name=name,
            strict=strict,
        )
        if not paths:
            return None
        stats = [item for path in paths if (item := read_unit(path)) is not None]
        if not stats:
            return None
        return AppStats(
            name=name,
            scope=scope,
            ram=sum(item.ram for item in stats),
            cache=sum(item.cache for item in stats),
            swap=sum(item.swap for item in stats),
            total=sum(item.total for item in stats),
            procs=sum(item.procs for item in stats),
            kernel=sum(item.kernel for item in stats),
            zswapped=sum(item.zswapped for item in stats),
            zswap_pool=sum(item.zswap_pool for item in stats),
            unit_paths=tuple(paths),
        )

    def read_procs(self, app: AppStats) -> list[ProcStats]:
        return read_procs(app.unit_paths, self.root)

    def private_bytes(self, pid: int) -> int | None:
        return read_private_bytes(self.root, pid)


# --- processes ----------------------------------------------------------------


def read_procs(unit_paths: Iterable[Path], root: Path) -> list[ProcStats]:
    """Read per-process counters for PIDs found recursively under each unit path."""
    clk_tck = os.sysconf("SC_CLK_TCK")
    uptime = _read_uptime(root / "proc" / "uptime")
    procs: list[ProcStats] = []
    for unit_path in unit_paths:
        for pid in _pids_under(str(unit_path)):
            proc = _read_proc_stats(root, pid, unit_path.name, uptime, clk_tck)
            if proc is not None:
                procs.append(proc)
    return procs


_PRIVATE_KEYS = ("Private_Clean", "Private_Dirty")


def read_private_bytes(root: Path, pid: int) -> int | None:
    """USS (`Private_Clean + Private_Dirty`) from `/proc/PID/smaps_rollup`,
    in bytes. `None` when the file is missing, empty, or unreadable -- a
    sandboxed process can deny this even though its `/proc/PID/status` reads
    fine. Used by `appmem app NAME` only: it's one
    extra file read per process shown, too costly to also do for every
    process of every app in a `snapshot`."""
    path = os.path.join(str(root), "proc", str(pid), "smaps_rollup")
    try:
        content = _read_small_file(path)
    except OSError:
        return None
    total_kb = 0
    found = False
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        if key not in _PRIVATE_KEYS:
            continue
        value = rest.strip().removesuffix("kB").strip()
        if value.isdigit():
            total_kb += int(value)
            found = True
    return total_kb * 1024 if found else None


def _pids_under(unit_dir: str) -> list[int]:
    pids: set[int] = set()
    for content in _iter_procs_content(unit_dir):
        for line in content.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    return sorted(pids)


def _read_uptime(path: Path) -> float:
    # `IndexError` (an empty or half-written file, no fields to split) is
    # folded into `ValueError` here so a transient read during a tick reads
    # as one of the two error types the screens already treat as "skip this
    # tick" (SPEC.md "Behaviour details").
    try:
        return float(_read_small_file(str(path)).split()[0])
    except IndexError as exc:
        raise ValueError(f"empty or malformed uptime file: {path}") from exc


def _read_proc_stats(
    root: Path, pid: int, unit_name: str, uptime: float, clk_tck: int
) -> ProcStats | None:
    proc_dir = root / "proc" / str(pid)
    try:
        status = _read_status(proc_dir / "status")
        name = _read_proc_name(proc_dir)
        starttime = _read_starttime(proc_dir / "stat")
    except (OSError, IndexError, ValueError):
        return None
    age_seconds = max(uptime - starttime / clk_tck, 0.0)
    return ProcStats(
        pid=pid,
        name=name,
        swap=status.get("VmSwap", 0) * 1024,
        ram=(status.get("RssAnon", 0) + status.get("RssShmem", 0)) * 1024,
        age_seconds=age_seconds,
        unit=unit_name,
    )


def _read_status(path: Path) -> dict[str, int]:
    # Raw read + `find` on the three keys used: splitting all ~57 status lines was
    # most of the process view's tick cost (278 PIDs in one app).
    content = "\n" + _read_small_file(str(path))  # FileNotFoundError: PID vanished.
    result: dict[str, int] = {}
    for key in _STATUS_KEYS:
        start = content.find(f"\n{key}:")
        if start == -1:
            continue  # Kernel threads have no Vm*/Rss* lines.
        end = content.find("\n", start + 1)
        rest = content[start + len(key) + 2 : end if end != -1 else None].strip()
        rest = rest.removesuffix("kB").strip()
        if rest.isdigit():
            result[key] = int(rest)
    return result


def _read_comm(proc_dir: Path) -> str:
    return _read_small_file(str(proc_dir / "comm")).strip()


def _first_token_name(field: bytes) -> str | None:
    """The safe fallback shared by a truly empty cmdline and a
    setproctitle-style rewrite (the whole argv, secrets included, collapsed
    into one NUL field with spaces): only the first whitespace token is ever
    looked at. `None` means "fall back to comm" (an `exe`/`/proc/`-prefixed
    argv0, same as a re-exec through `/proc/self/exe`)."""
    tokens = field.decode(errors="replace").split(maxsplit=1)
    if not tokens:
        return None
    argv0 = tokens[0]
    basename = Path(argv0).name
    if basename == "exe" or argv0.startswith("/proc/"):
        return None
    return basename


def _read_proc_name(proc_dir: Path) -> str:
    raw = _read_small_file_bytes(str(proc_dir / "cmdline"))  # FileNotFoundError: PID vanished.
    # A setproctitle rewrite shorter than the original argv leaves the rest
    # NUL-padded (or holding leftovers of the old argv), so trailing empty
    # fields and a whitespace-holding argv0 both mean "title, not argv".
    fields = raw.rstrip(b"\0").split(b"\0")
    if len(fields) <= 1 or len(fields[0].split(maxsplit=1)) > 1:
        name = _first_token_name(fields[0])
        return name if name is not None else _read_comm(proc_dir)
    # Real, NUL-delimited argv: safe to look at more than argv0, since each
    # argument is its own field rather than text an attacker-controlled
    # process chose to put after a space.
    argv0 = fields[0].decode(errors="replace")
    basename = Path(argv0).name
    if basename == "exe" or argv0.startswith("/proc/"):
        return _read_comm(proc_dir)
    args = [field.decode(errors="replace") for field in fields[1:]]
    enriched = command_display_name(basename, args)
    return enriched if enriched is not None else basename


def _read_starttime(path: Path) -> float:
    content = _read_small_file(str(path))  # FileNotFoundError: PID vanished.
    after_paren = content.rsplit(")", 1)[1]
    fields = after_paren.split()
    # After the closing ')', the remaining fields start at field 3 (state),
    # so field 22 (starttime) is at index 22 - 3 = 19.
    return float(fields[19])
