"""Fixture-tree builders shared by the appmem test suite.

Build minimal cgroup v2 / proc-like directory trees under `tmp_path`. Tests
must never read the live `/sys` or `/proc` (see AGENTS.md).
"""

from __future__ import annotations

from pathlib import Path


def write_memory_stat(
    unit_dir: Path,
    *,
    anon: int = 0,
    shmem: int = 0,
    kernel: int = 0,
    file: int = 0,
    zswapped: int | None = None,
    zswap: int | None = None,
) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"anon {anon}", f"shmem {shmem}", f"kernel {kernel}", f"file {file}"]
    if zswapped is not None:
        lines.append(f"zswapped {zswapped}")
    if zswap is not None:
        # The pool's own RAM cost, a subset of `kernel` on kernels that
        # report it there.
        assert zswap <= kernel, "fixture: zswap pool > kernel"
        lines.append(f"zswap {zswap}")
    (unit_dir / "memory.stat").write_text("\n".join(lines) + "\n")


def write_swap_current(unit_dir: Path, value: int) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "memory.swap.current").write_text(f"{value}\n")


def write_cgroup_procs(unit_dir: Path, pids: list[int]) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    content = "\n".join(str(pid) for pid in pids)
    (unit_dir / "cgroup.procs").write_text(content + "\n" if pids else "")


def make_unit(  # noqa: PLR0913 -- one keyword-only field per memory.stat/swap.current line
    unit_dir: Path,
    *,
    anon: int = 0,
    shmem: int = 0,
    kernel: int = 0,
    file: int = 0,
    zswapped: int | None = None,
    zswap: int | None = None,
    swap: int | None = 0,
    pids: list[int] | None = None,
) -> Path:
    """Create a full unit dir: memory.stat, memory.swap.current, cgroup.procs."""
    # zswapped pages are a subset of the unit's swap on a real kernel.
    assert zswapped is None or zswapped <= (swap or 0), "fixture: zswapped > swap"
    write_memory_stat(
        unit_dir, anon=anon, shmem=shmem, kernel=kernel, file=file, zswapped=zswapped, zswap=zswap
    )
    if swap is not None:
        write_swap_current(unit_dir, swap)
    write_cgroup_procs(unit_dir, pids or [])
    return unit_dir


def write_proc(
    root: Path,
    pid: int,
    *,
    cmdline: str = "",
    comm: str = "proc",
    vm_swap_kb: int | None = None,
    rss_anon_kb: int | None = None,
    rss_shmem_kb: int | None = None,
    starttime_ticks: int = 0,
) -> Path:
    """Create `proc/PID/{cmdline,comm,status,stat}` for a fixture process."""
    proc_dir = root / "proc" / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "cmdline").write_bytes(cmdline.encode() if cmdline else b"")
    (proc_dir / "comm").write_text(f"{comm}\n")

    status_lines: list[str] = []
    if vm_swap_kb is not None:
        status_lines.append(f"VmSwap:\t{vm_swap_kb} kB")
    if rss_anon_kb is not None:
        status_lines.append(f"RssAnon:\t{rss_anon_kb} kB")
    if rss_shmem_kb is not None:
        status_lines.append(f"RssShmem:\t{rss_shmem_kb} kB")
    (proc_dir / "status").write_text("\n".join(status_lines) + "\n" if status_lines else "")

    (proc_dir / "stat").write_text(write_stat_line(pid, comm, starttime_ticks))
    return proc_dir


def write_stat_line(pid: int, comm: str, starttime_ticks: int) -> str:
    """Build a `/proc/PID/stat` line with `starttime` at field 22 (index 19 after ')')."""
    # Fields 3..21 (19 values, index 0-18), then field 22 = starttime (index 19),
    # then a few more trailing fields for realism.
    before = ["S"] + ["0"] * 18
    after = ["0"] * 17
    fields = [*before, str(starttime_ticks), *after]
    return f"{pid} ({comm}) " + " ".join(fields) + "\n"


def write_uptime(root: Path, seconds: float) -> None:
    proc_dir = root / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "uptime").write_text(f"{seconds} 0\n")


def write_meminfo(  # noqa: PLR0913 -- one keyword-only field per /proc/meminfo line written below
    root: Path,
    *,
    mem_total_kb: int,
    mem_available_kb: int,
    swap_total_kb: int,
    swap_free_kb: int,
    mem_free_kb: int = 0,
    cached_kb: int = 0,
    shmem_kb: int = 0,
    sreclaimable_kb: int = 0,
    zswap_kb: int | None = None,
    zswapped_kb: int | None = None,
) -> None:
    proc_dir = root / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"MemTotal:       {mem_total_kb} kB",
        f"MemFree:        {mem_free_kb} kB",
        f"MemAvailable:   {mem_available_kb} kB",
        f"Cached:         {cached_kb} kB",
        f"SwapTotal:      {swap_total_kb} kB",
        f"SwapFree:       {swap_free_kb} kB",
        f"Shmem:          {shmem_kb} kB",
        f"SReclaimable:   {sreclaimable_kb} kB",
    ]
    # Omitted by default: a missing pair means "no zswap support" (SPEC.md
    # "Data sources"), tested separately from the enabled/disabled knob below.
    if zswap_kb is not None:
        lines.append(f"Zswap:          {zswap_kb} kB")
    if zswapped_kb is not None:
        # Zswapped is part of swap used on a real kernel.
        assert zswapped_kb <= swap_total_kb - swap_free_kb, "fixture: Zswapped > swap used"
        lines.append(f"Zswapped:       {zswapped_kb} kB")
    (proc_dir / "meminfo").write_text("\n".join(lines) + "\n")


def write_zswap_enabled(root: Path, enabled: bool) -> None:
    """`/sys/module/zswap/parameters/enabled`: `Y` or `N`. Not writing it at
    all (the default fixture state) reads as disabled, same as a real
    machine without the zswap module (SPEC.md "Data sources")."""
    param_dir = root / "sys" / "module" / "zswap" / "parameters"
    param_dir.mkdir(parents=True, exist_ok=True)
    (param_dir / "enabled").write_text(("Y" if enabled else "N") + "\n")


def write_zswap_params(
    root: Path, *, compressor: str | None = None, max_pool_percent: int | None = None
) -> None:
    """`/sys/module/zswap/parameters/{compressor,max_pool_percent}`. Either
    left `None` leaves that one file unwritten, same as an older kernel
    without that particular knob."""
    param_dir = root / "sys" / "module" / "zswap" / "parameters"
    param_dir.mkdir(parents=True, exist_ok=True)
    if compressor is not None:
        (param_dir / "compressor").write_text(compressor + "\n")
    if max_pool_percent is not None:
        (param_dir / "max_pool_percent").write_text(f"{max_pool_percent}\n")


def write_smaps_rollup(
    root: Path, pid: int, *, private_clean_kb: int, private_dirty_kb: int
) -> None:
    """`/proc/PID/smaps_rollup`, with just the two `Private_*` lines this
    suite cares about."""
    proc_dir = root / "proc" / str(pid)
    proc_dir.mkdir(parents=True, exist_ok=True)
    content = f"Private_Clean:  {private_clean_kb} kB\nPrivate_Dirty:  {private_dirty_kb} kB\n"
    (proc_dir / "smaps_rollup").write_text(content)


def write_vmstat(root: Path, *, zswpwb: int | None = None) -> None:
    """`/proc/vmstat`, with just the `zswpwb` line this suite cares about.
    `zswpwb=None` omits the line, same as a kernel with no zswap writeback
    counter (SPEC.md "Data sources")."""
    proc_dir = root / "proc"
    proc_dir.mkdir(parents=True, exist_ok=True)
    lines = ["nr_free_pages 1000"]  # a harmless unrelated line, for realism
    if zswpwb is not None:
        lines.append(f"zswpwb {zswpwb}")
    (proc_dir / "vmstat").write_text("\n".join(lines) + "\n")


def write_pressure(
    root: Path,
    *,
    some_avg10: float,
    full_avg10: float,
    some_avg60: float = 0.0,
    full_avg60: float = 0.0,
) -> None:
    pressure_dir = root / "proc" / "pressure"
    pressure_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"some avg10={some_avg10:.2f} avg60={some_avg60:.2f} avg300=0.00 total=0\n"
        f"full avg10={full_avg10:.2f} avg60={full_avg60:.2f} avg300=0.00 total=0\n"
    )
    (pressure_dir / "memory").write_text(content)


def user_service_root(root: Path, uid: int) -> Path:
    return (
        root / "sys" / "fs" / "cgroup" / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    )
