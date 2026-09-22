"""Tests for `appmem.collect.read_procs` (SPEC.md "Data sources")."""

import os
from pathlib import Path

from appmem.collect import read_procs
from helpers import write_cgroup_procs, write_proc, write_stat_line, write_uptime


def test_reads_name_swap_ram_age_and_unit(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=1000.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "app-ghostty.service"
    write_cgroup_procs(unit_dir, [111])
    write_proc(
        tmp_path,
        111,
        cmdline="/usr/bin/ghostty\x00--foo\x00",
        vm_swap_kb=1024,
        rss_anon_kb=2048,
        rss_shmem_kb=512,
        starttime_ticks=100,
    )

    clk_tck = os.sysconf("SC_CLK_TCK")
    procs = read_procs([unit_dir], tmp_path)

    assert len(procs) == 1
    proc = procs[0]
    assert proc.pid == 111
    assert proc.name == "ghostty"
    assert proc.swap == 1024 * 1024
    assert proc.ram == (2048 + 512) * 1024
    assert proc.unit == "app-ghostty.service"
    assert proc.age_seconds == 1000.0 - 100 / clk_tck


def test_comm_with_spaces_and_parens_in_stat(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=500.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [222])
    proc_dir = tmp_path / "proc" / "222"
    proc_dir.mkdir(parents=True)
    (proc_dir / "cmdline").write_bytes(b"")
    (proc_dir / "comm").write_text("my proc (2)\n")
    (proc_dir / "status").write_text("VmSwap:\t0 kB\n")
    (proc_dir / "stat").write_text(write_stat_line(222, "my proc (2)", starttime_ticks=50))

    procs = read_procs([unit_dir], tmp_path)

    assert len(procs) == 1
    assert procs[0].name == "my proc (2)"
    # Parsing after the first ')' would read a different field as starttime.
    assert procs[0].age_seconds == 500.0 - 50 / os.sysconf("SC_CLK_TCK")


def test_space_joined_cmdline_keeps_only_program_basename(tmp_path: Path) -> None:
    # setproctitle-style: the whole argv as one NUL field joined with spaces.
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [777])
    write_proc(
        tmp_path, 777, cmdline="/opt/bin/scraper --header Authorization: Bearer FAKE-TOKEN\x00"
    )

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "scraper"


def test_empty_cmdline_falls_back_to_comm(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [333])
    write_proc(tmp_path, 333, cmdline="", comm="kworker/0:1")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "kworker/0:1"


def test_missing_vm_swap_defaults_to_zero(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [444])
    write_proc(tmp_path, 444, cmdline="kthread\x00", rss_anon_kb=10)

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].swap == 0
    assert procs[0].ram == 10 * 1024


def test_vanished_pid_is_skipped(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [555, 666])
    write_proc(tmp_path, 555, cmdline="alive\x00")
    # PID 666 listed in cgroup.procs but has no /proc/666 dir: vanished mid-read.

    procs = read_procs([unit_dir], tmp_path)

    assert [p.pid for p in procs] == [555]


def test_pids_collected_recursively_below_the_unit(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "app-konsole.service"
    write_cgroup_procs(unit_dir, [1])
    write_cgroup_procs(unit_dir / "tab(2).scope", [2, 3])
    write_proc(tmp_path, 1, cmdline="konsole\x00")
    write_proc(tmp_path, 2, cmdline="bash\x00")
    write_proc(tmp_path, 3, cmdline="vim\x00")

    procs = read_procs([unit_dir], tmp_path)

    assert {p.pid for p in procs} == {1, 2, 3}
    # PIDs found in a nested scope are still attributed to the top unit passed in.
    assert {p.unit for p in procs} == {"app-konsole.service"}
