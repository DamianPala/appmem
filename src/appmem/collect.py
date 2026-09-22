"""Reads `/sys/fs/cgroup` and `/proc` and returns plain dataclasses.

No UI imports here. Every reader takes a `root: Path` standing for `/`
(production passes `Path("/")`), so tests point it at fixture trees and
never touch the live filesystem. See SPEC.md "Data sources", "Finding
units", "Definitions" and "Behaviour details" for the source contract.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from appmem.naming import app_name

_MEMORY_STAT_KEYS = ("anon", "shmem", "kernel", "file")
_USER_SLICES = ("app.slice", "session.slice", "background.slice")


class CgroupUnavailableError(Exception):
    """Raised when the user's cgroup v2 tree, or its memory.stat, is missing.

    The message names the specific missing path, so the CLI slice can map
    it to the `cgroup_unavailable` error kind.
    """


@dataclass(frozen=True)
class SystemStats:
    """System-wide memory, swap, pressure and hidden system.slice totals."""

    mem_total: int
    mem_available: int
    swap_total: int
    swap_free: int
    pressure_some_avg10: float | None
    pressure_full_avg10: float | None
    system_ram: int
    system_swap: int


@dataclass(frozen=True)
class UnitStats:
    """Counters for a single cgroup unit (SPEC.md "Definitions")."""

    ram: int
    cache: int
    swap: int
    total: int
    procs: int


@dataclass(frozen=True)
class Unit:
    """A cgroup unit path paired with its counters, ready for grouping."""

    path: Path
    stats: UnitStats


@dataclass(frozen=True)
class AppStats:
    """Counters for an app: one or more units merged by `naming.app_name`."""

    name: str
    ram: int
    cache: int
    swap: int
    total: int
    procs: int
    unit_paths: tuple[Path, ...]


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


# --- system ---------------------------------------------------------------


def read_system(root: Path) -> SystemStats:
    """Read system-wide memory/swap/pressure and the hidden system.slice total."""
    meminfo = _read_meminfo(root / "proc" / "meminfo")
    some_avg10, full_avg10 = _read_pressure(root / "proc" / "pressure" / "memory")
    # The header never shows a system procs count, so skip the costly recursive count.
    system_slice = os.path.join(str(root), "sys", "fs", "cgroup", "system.slice")
    system_stats = _read_unit_stats(system_slice, count_procs=False)
    return SystemStats(
        mem_total=meminfo.get("MemTotal", 0),
        mem_available=meminfo.get("MemAvailable", 0),
        swap_total=meminfo.get("SwapTotal", 0),
        swap_free=meminfo.get("SwapFree", 0),
        pressure_some_avg10=some_avg10,
        pressure_full_avg10=full_avg10,
        system_ram=system_stats.ram if system_stats else 0,
        system_swap=system_stats.swap if system_stats else 0,
    )


def _read_meminfo(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        content = path.read_text()
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


def _read_pressure(path: Path) -> tuple[float | None, float | None]:
    try:
        content = path.read_text()
    except FileNotFoundError:
        return None, None
    values: dict[str, float] = {}
    for line in content.splitlines():
        kind, _, rest = line.partition(" ")
        for field in rest.split():
            key, _, value = field.partition("=")
            if key == "avg10":
                values[kind] = float(value)
    return values.get("some"), values.get("full")


# --- finding units ----------------------------------------------------------


def find_units(root: Path, uid: int, include_system: bool) -> list[Path]:
    """Find unit directories under the user's cgroup tree (SPEC.md "Finding units").

    Raises `CgroupUnavailableError` when the user's `user@$UID.service` tree
    or its `memory.stat` is missing (memory controller not enabled there).
    """
    cgroup_root = os.path.join(str(root), "sys", "fs", "cgroup")
    user_root = os.path.join(cgroup_root, "user.slice", f"user-{uid}.slice", f"user@{uid}.service")
    if not os.path.isdir(user_root):
        raise CgroupUnavailableError(f"missing cgroup path: {user_root}")
    memory_stat = os.path.join(user_root, "memory.stat")
    if not os.path.isfile(memory_stat):
        raise CgroupUnavailableError(f"missing cgroup path: {memory_stat}")

    unit_paths: list[str] = []
    for slice_name in _USER_SLICES:
        unit_paths.extend(_walk_slice(os.path.join(user_root, slice_name)))
    if include_system:
        unit_paths.extend(_walk_slice(os.path.join(cgroup_root, "system.slice")))
    return [Path(p) for p in unit_paths]


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


# --- unit counters ----------------------------------------------------------


def read_unit(path: Path) -> UnitStats | None:
    """Read a unit's counters. Returns `None` if the unit vanished mid-read."""
    return _read_unit_stats(str(path))


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
    kernel = stat["kernel"]
    file_ = stat["file"]
    ram = anon + shmem + kernel
    cache = file_ - shmem
    total = swap + ram
    return UnitStats(ram=ram, cache=cache, swap=swap, total=total, procs=procs)


def _read_small_file(path: str) -> str:
    """Read a small proc/cgroup file with raw `os.open`/`os.read`.

    Bypasses the buffered `TextIOWrapper` `open()` sets up for every call, which
    measurably matters here: the collector opens a few hundred of these files
    (one to a few hundred bytes each) per tick.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _read_memory_stat(path: str) -> dict[str, int] | None:
    """Parse `memory.stat`. Returns `None` on missing/empty/partial content."""
    content = _read_small_file(path)  # OSError propagates: unit vanished.
    values: dict[str, int] = {}
    for line in content.splitlines():
        key, _, value = line.partition(" ")
        if key in _MEMORY_STAT_KEYS and value.strip().lstrip("-").isdigit():
            values[key] = int(value.strip())
            if len(values) == len(_MEMORY_STAT_KEYS):
                break  # The keys sit near the top of a ~50-line file.
    if not all(key in values for key in _MEMORY_STAT_KEYS):
        return None
    return values


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
    """
    try:
        yield _read_small_file(os.path.join(cgroup_dir, "cgroup.procs"))
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


def group_apps(units: Iterable[Unit]) -> list[AppStats]:
    """Merge units by `naming.app_name`, summing counters and keeping unit paths."""
    groups: dict[str, list[Unit]] = {}
    for unit in units:
        name = app_name(unit.path.name)
        groups.setdefault(name, []).append(unit)

    apps: list[AppStats] = []
    for name, unit_list in groups.items():
        apps.append(
            AppStats(
                name=name,
                ram=sum(u.stats.ram for u in unit_list),
                cache=sum(u.stats.cache for u in unit_list),
                swap=sum(u.stats.swap for u in unit_list),
                total=sum(u.stats.total for u in unit_list),
                procs=sum(u.stats.procs for u in unit_list),
                unit_paths=tuple(u.path for u in unit_list),
            )
        )
    return apps


def filter_visible_apps(apps: Iterable[AppStats]) -> list[AppStats]:
    """Hide rows with TOTAL < 1 MiB (SPEC.md "Behaviour details"). CACHE is excluded."""
    threshold = 1024 * 1024
    return [app for app in apps if app.total >= threshold]


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


def _pids_under(unit_dir: str) -> list[int]:
    pids: set[int] = set()
    for content in _iter_procs_content(unit_dir):
        for line in content.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    return sorted(pids)


def _read_uptime(path: Path) -> float:
    return float(path.read_text().split()[0])


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
    result: dict[str, int] = {}
    for line in path.read_text().splitlines():  # FileNotFoundError: PID vanished.
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith("kB"):
            rest = rest[:-2].strip()
        if rest.lstrip("-").isdigit():
            result[key] = int(rest)
    return result


def _read_proc_name(proc_dir: Path) -> str:
    raw = (proc_dir / "cmdline").read_bytes()  # FileNotFoundError: PID vanished.
    # setproctitle-style processes put the whole argv, secrets included, into the
    # first NUL field with spaces, so keep only its first whitespace token.
    tokens = raw.split(b"\0", 1)[0].decode(errors="replace").split(maxsplit=1)
    if tokens:
        return Path(tokens[0]).name
    return (proc_dir / "comm").read_bytes().decode(errors="replace").strip()


def _read_starttime(path: Path) -> float:
    content = path.read_text()  # FileNotFoundError: PID vanished.
    after_paren = content.rsplit(")", 1)[1]
    fields = after_paren.split()
    # After the closing ')', the remaining fields start at field 3 (state),
    # so field 22 (starttime) is at index 22 - 3 = 19.
    return float(fields[19])


# --- process-view math ---------------------------------------------------------


def other_row(app: AppStats, procs: Iterable[ProcStats]) -> tuple[int, int]:
    """SWAP/RAM the app holds without a matching process, clamped at 0."""
    procs = list(procs)
    other_swap = max(app.swap - sum(p.swap for p in procs), 0)
    other_ram = max(app.ram - sum(p.ram for p in procs), 0)
    return other_swap, other_ram


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
        )
        for name, group in groups.items()
    ]
