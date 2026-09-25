"""Tests for `appmem.collect.read_procs` (SPEC.md "Data sources")."""

import os
from pathlib import Path

import pytest

from appmem.collect import read_private_bytes, read_procs
from helpers import (
    write_cgroup_procs,
    write_proc,
    write_smaps_rollup,
    write_stat_line,
    write_uptime,
)


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


def test_nul_padded_setproctitle_keeps_only_program_basename(tmp_path: Path) -> None:
    # A title shorter than the original argv: the rest of the area is NULs.
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [779])
    write_proc(tmp_path, 779, cmdline="npm exec @a/b --key FAKE/TOKEN" + "\x00" * 8)

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "npm"


def test_exe_basename_falls_back_to_comm(tmp_path: Path) -> None:
    # Electron/AppImage-style processes re-exec through /proc/self/exe, so
    # cmdline[0]'s basename is just "exe".
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [888])
    write_proc(tmp_path, 888, cmdline="/proc/self/exe --flag\x00", comm="Typora")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "Typora"


def test_proc_prefixed_argv0_falls_back_to_comm(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [999])
    write_proc(tmp_path, 999, cmdline="/proc/123/exe\x00", comm="open-whispr-app")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "open-whispr-app"


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


def test_missing_comm_file_is_skipped_as_a_vanished_pid(tmp_path: Path) -> None:
    # An empty cmdline forces the `_read_comm` fallback (SPEC.md "Process
    # names"); the comm file itself vanishing mid-read (unlike cmdline/status/
    # stat, which every other test here exercises) must get the same "skip
    # this pid" treatment, not propagate.
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [321])
    proc_dir = tmp_path / "proc" / "321"
    proc_dir.mkdir(parents=True)
    (proc_dir / "cmdline").write_bytes(b"")
    (proc_dir / "status").write_text("VmSwap:\t0 kB\n")
    (proc_dir / "stat").write_text(write_stat_line(321, "x", starttime_ticks=10))
    # No comm file written at all.

    procs = read_procs([unit_dir], tmp_path)

    assert procs == []


def test_missing_uptime_file_raises_file_not_found(tmp_path: Path) -> None:
    # `read_procs` reads `/proc/uptime` once, up front, outside any per-pid
    # guard -- unlike a per-pid file vanishing, this can't be "skip one
    # process", so it propagates instead of being swallowed.
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [111])
    write_proc(tmp_path, 111, cmdline="alive\x00")
    # No proc/uptime written at all.

    with pytest.raises(FileNotFoundError):
        read_procs([unit_dir], tmp_path)


def test_pids_listed_in_full_past_one_page_of_cgroup_procs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `cgroup.procs` is a multi-record seq_file like `/proc/vmstat`: the
    # kernel hands back at most one page (4096 bytes) per `read()` call,
    # however large the buffer requested, so a short first read there does
    # not mean EOF (see `_read_small_file_bytes`'s docstring). Capping
    # `os.read` reproduces that; a `tmp_path` fixture written in one go would
    # never trigger it on its own (review round 1, MUST 1).
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    pids = list(range(100_000, 100_700))  # 700 lines of "100xxx\n" (7 bytes) > one page
    write_cgroup_procs(unit_dir, pids)
    assert (unit_dir / "cgroup.procs").stat().st_size > 4096
    for pid in pids:
        write_proc(tmp_path, pid, cmdline="alive\x00")

    real_read = os.read

    def page_capped_read(fd: int, count: int) -> bytes:
        return real_read(fd, min(count, 4096))

    monkeypatch.setattr(os, "read", page_capped_read)

    procs = read_procs([unit_dir], tmp_path)

    assert {p.pid for p in procs} == set(pids)


# --- interpreter/launcher naming -----------------------------------------------
# Command lines here are made up: process arguments can hold secrets.


def test_node_process_name_becomes_interpreter_colon_script(tmp_path: Path) -> None:
    write_uptime(tmp_path, seconds=100.0)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_cgroup_procs(unit_dir, [12])
    write_proc(tmp_path, 12, cmdline="node\x00/home/user/.ccs/mcp/websearch-server.cjs\x00")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "node:websearch-server.cjs"


def test_node_process_arguments_never_leak_into_the_name(tmp_path: Path) -> None:
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_uptime(tmp_path, seconds=100.0)
    write_cgroup_procs(unit_dir, [13])
    write_proc(
        tmp_path,
        13,
        cmdline="node\x00/opt/bridge.cjs\x00--token\x00SECRET-LOOKING-VALUE\x00",
    )

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "node:bridge.cjs"
    assert "SECRET" not in procs[0].name


def test_python_dash_m_process_name_uses_the_module(tmp_path: Path) -> None:
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_uptime(tmp_path, seconds=100.0)
    write_cgroup_procs(unit_dir, [14])
    write_proc(tmp_path, 14, cmdline="python3\x00-m\x00ccs_websearch\x00")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "python3:ccs_websearch"


def test_setproctitle_rewritten_cmdline_never_reaches_the_interpreter_naming(
    tmp_path: Path,
) -> None:
    # A single NUL field with an embedded interpreter-shaped first word is
    # not real argv; only the safe first-token fallback applies, same as any
    # other setproctitle-style process (never the interpreter/launcher
    # enrichment, which needs real argv to stay safe).
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_uptime(tmp_path, seconds=100.0)
    write_cgroup_procs(unit_dir, [15])
    write_proc(tmp_path, 15, cmdline="npm exec @scope/tool@latest\x00")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "npm"


def test_control_character_in_a_node_script_path_gives_the_bare_interpreter(
    tmp_path: Path,
) -> None:
    # Fails closed: a script path that isn't a plain filesystem path (here,
    # one carrying a raw control byte) never reaches the derived label.
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "unit.service"
    write_uptime(tmp_path, seconds=100.0)
    write_cgroup_procs(unit_dir, [16])
    write_proc(tmp_path, 16, cmdline="node\x00/a/evil\x1b[2Jname.js\x00")

    procs = read_procs([unit_dir], tmp_path)

    assert procs[0].name == "node"


# --- read_private_bytes: USS from smaps_rollup ---------------------------------


def test_read_private_bytes_sums_private_clean_and_dirty(tmp_path: Path) -> None:
    write_smaps_rollup(tmp_path, 21, private_clean_kb=1024, private_dirty_kb=512)

    assert read_private_bytes(tmp_path, 21) == (1024 + 512) * 1024


def test_read_private_bytes_is_none_when_the_file_is_missing(tmp_path: Path) -> None:
    # e.g. a sandboxed process denying access, or the PID vanished.
    assert read_private_bytes(tmp_path, 22) is None


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
