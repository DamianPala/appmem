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

_REQUIRED_MEMORY_STAT_KEYS = ("anon", "shmem", "file")
_KERNEL_FALLBACK_KEYS = ("slab", "kernel_stack", "pagetables", "percpu")
_MEMORY_STAT_KEYS = frozenset({"anon", "shmem", "kernel", "file", *_KERNEL_FALLBACK_KEYS})
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
    the session as over (final review, slice 4 round 2 item 5).
    """


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


@dataclass(frozen=True)
class UnitStats:
    """Counters for a single cgroup unit (SPEC.md "Definitions")."""

    ram: int
    cache: int
    swap: int
    total: int
    procs: int
    kernel: int = 0
    """The app's charged kernel memory (page tables, slab, stacks), already
    included in `ram`. Carried separately so the process view can show it as
    its own row (SPEC.md "Definitions": RAM = anon + shmem + kernel)."""


@dataclass(frozen=True)
class Unit:
    """A cgroup unit path paired with its counters, ready for grouping."""

    path: Path
    stats: UnitStats
    scope: str = "user"
    """"user" or "system" (SPEC.md "Grouping"): which root `find_units` found
    this unit under. Part of app identity, alongside the app name."""


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
    kernel: int = 0
    scope: str = "user"


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


def read_system(root: Path, uid: int) -> SystemStats:
    """Read system-wide memory/swap/pressure, the hidden system.slice total and
    the `elsewhere` figure (SPEC.md "Behaviour details"). `uid` locates the
    user tree for the `elsewhere` subtraction."""
    meminfo = _read_meminfo(root / "proc" / "meminfo")
    some_avg10, some_avg60, full_avg10, full_avg60 = _read_pressure(
        root / "proc" / "pressure" / "memory"
    )
    # The header never shows a system procs count, so skip the costly recursive count.
    system_slice = os.path.join(str(root), "sys", "fs", "cgroup", "system.slice")
    system_stats = _read_unit_stats(system_slice, count_procs=False)
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
    )


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


def _read_pressure(
    path: Path,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return `(some avg10, some avg60, full avg10, full avg60)`."""
    try:
        content = path.read_text()
    except FileNotFoundError:
        return None, None, None, None
    values: dict[str, dict[str, float]] = {}
    for line in content.splitlines():
        kind, _, rest = line.partition(" ")
        fields: dict[str, float] = {}
        for field in rest.split():
            key, _, value = field.partition("=")
            if key in ("avg10", "avg60"):
                fields[key] = float(value)
        values[kind] = fields
    some = values.get("some", {})
    full = values.get("full", {})
    return some.get("avg10"), some.get("avg60"), full.get("avg10"), full.get("avg60")


# --- finding units ----------------------------------------------------------


def find_units(root: Path, uid: int, include_system: bool, *, strict: bool = True) -> list[Path]:
    """Find unit directories under the user's cgroup tree (SPEC.md "Finding units").

    Always raises `CgroupUnavailableError` when the user's `user@$UID.service`
    tree directory itself is missing.

    `strict` (default `True`, the pre-start check in `cli.py`) also raises
    `CgroupUnavailableError` when that tree's `memory.stat` can't be parsed
    (memory controller not enabled there, F1). A per-tick caller passes
    `strict=False`: the same parse failure there is usually transient (e.g. a
    read mid-write), so it raises the lighter `MemoryStatUnavailableError`
    instead, which callers treat as "skip this tick" rather than fatal
    (final review, slice 4 round 2 item 5).
    """
    cgroup_root = os.path.join(str(root), "sys", "fs", "cgroup")
    user_root = os.path.join(cgroup_root, "user.slice", f"user-{uid}.slice", f"user@{uid}.service")
    if not os.path.isdir(user_root):
        raise CgroupUnavailableError(f"missing cgroup path: {user_root}")
    memory_stat = os.path.join(user_root, "memory.stat")
    # Parses the file rather than just checking it exists, so a controller
    # enabled without anon/shmem/file (memory accounting not really on) fails
    # here instead of showing an empty table (F1).
    if _read_ram(memory_stat) is None:
        if strict:
            raise CgroupUnavailableError(f"missing cgroup path: {memory_stat}")
        raise MemoryStatUnavailableError(f"unreadable this tick: {memory_stat}")

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


def unit_scope(root: Path, unit_path: Path) -> str:
    """ "user" or "system": which root `find_units` found `unit_path` under
    (SPEC.md "Finding units"). Part of app identity (SPEC.md "Grouping")."""
    # A string prefix test, not `Path.relative_to`: this runs for every unit on
    # every tick, and `relative_to` measured ~15x slower (~3 ms per tick on 189 units).
    system_slice = os.path.join(str(root), "sys", "fs", "cgroup", "system.slice") + os.sep
    return "system" if str(unit_path).startswith(system_slice) else "user"


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
        if unit_scope(root, path) == scope and app_name(path.name) == name
    ]


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
    return UnitStats(ram=ram, cache=cache, swap=swap, total=total, procs=procs, kernel=kernel)


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
    """Merge units by `(scope, naming.app_name)`, summing counters (SPEC.md
    "Grouping"). A user and a system unit that normalize to the same name stay
    two rows (SPEC.md "Grouping": app identity is scope + name)."""
    groups: dict[tuple[str, str], list[Unit]] = {}
    for unit in units:
        name = app_name(unit.path.name)
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


def _read_proc_name(proc_dir: Path) -> str:
    raw = (proc_dir / "cmdline").read_bytes()  # FileNotFoundError: PID vanished.
    # setproctitle-style processes put the whole argv, secrets included, into the
    # first NUL field with spaces, so keep only its first whitespace token.
    tokens = raw.split(b"\0", 1)[0].decode(errors="replace").split(maxsplit=1)
    if tokens:
        argv0 = tokens[0]
        basename = Path(argv0).name
        # Electron/AppImage-style processes re-exec through /proc/self/exe, so
        # cmdline[0] is that self-referential path and its basename is just
        # "exe"; comm still carries the real program name in both cases (F6).
        if basename != "exe" and not argv0.startswith("/proc/"):
            return basename
    return (proc_dir / "comm").read_bytes().decode(errors="replace").strip()


def _read_starttime(path: Path) -> float:
    content = _read_small_file(str(path))  # FileNotFoundError: PID vanished.
    after_paren = content.rsplit(")", 1)[1]
    fields = after_paren.split()
    # After the closing ')', the remaining fields start at field 3 (state),
    # so field 22 (starttime) is at index 22 - 3 = 19.
    return float(fields[19])


# --- process-view math ---------------------------------------------------------


def unattributed_row(app: AppStats, procs: Iterable[ProcStats]) -> tuple[int, int]:
    """SWAP/RAM the app holds without a matching process or its own kernel
    share, clamped at 0 each (SPEC.md "Definitions"; the process view's
    `unattributed` row -- an accounting difference, not a process)."""
    procs = list(procs)
    swap = max(app.swap - sum(p.swap for p in procs), 0)
    ram = max(app.ram - sum(p.ram for p in procs) - app.kernel, 0)
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
        )
        for name, group in groups.items()
    ]
