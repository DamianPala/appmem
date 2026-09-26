"""Tests for `appmem.report.app_document`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from appmem import collect
from appmem.collect import LinuxBackend
from appmem.report import AppNotFoundError, app_document
from helpers import (
    make_unit,
    user_service_root,
    write_memory_stat,
    write_proc,
    write_smaps_rollup,
    write_uptime,
)

_NOW = datetime(2026, 9, 23, 0, 30, 41, tzinfo=timezone(timedelta(hours=2)))
_MIB = 1024 * 1024


def _base_tree(root: Path, uid: int = 1000) -> Path:
    user_root = user_service_root(root, uid=uid)
    write_memory_stat(user_root, anon=1)
    write_uptime(root, 1_000_000)
    return user_root


def test_processes_and_commands_sorted_and_cut_independently(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=10 * _MIB, shmem=0, kernel=1 * _MIB, file=0, swap=1 * _MIB, pids=[1, 2, 3])
    write_proc(tmp_path, 1, cmdline="node", comm="node", rss_anon_kb=3 * 1024, vm_swap_kb=0)
    write_proc(tmp_path, 2, cmdline="node", comm="node", rss_anon_kb=2 * 1024, vm_swap_kb=0)
    write_proc(tmp_path, 3, cmdline="ghostty", comm="ghostty", rss_anon_kb=4 * 1024, vm_swap_kb=0)

    document, total_processes, total_commands = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=2, now=_NOW
    )

    # Per-process total: ghostty 4 MiB, node(1) 3 MiB, node(2) 2 MiB.
    process_names = [item["name"] for item in document["processes"]["items"]]
    assert process_names == ["ghostty", "node"]  # sorted by total desc; only 2 of 3 processes
    assert document["processes"]["has_more"] is True
    assert total_processes == 3  # the real total, not the page size

    # Per-command total: node 5 MiB (3+2), ghostty 4 MiB -- summed over both node
    # processes even though the process page above only showed one of them.
    command_names = [item["name"] for item in document["commands"]["items"]]
    assert command_names == ["node", "ghostty"]
    assert document["commands"]["has_more"] is False  # only 2 distinct commands exist at all
    assert total_commands == 2
    node_command = next(item for item in document["commands"]["items"] if item["name"] == "node")
    assert node_command["procs"] == 2  # summed over *all* processes, not just the returned page
    assert node_command["ram_bytes"] == 5 * 1024 * 1024


def test_app_document_sums_zswapped_bytes_across_units(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    make_unit(
        user_root / "app.slice" / "app-ghostty.service",
        anon=10 * _MIB,
        swap=5 * _MIB,
        zswapped=2 * _MIB,
        pids=[],
    )
    make_unit(
        user_root / "app.slice" / "app-ghostty@1.service",
        anon=1 * _MIB,
        swap=1 * _MIB,
        zswapped=1 * _MIB,
        pids=[],
    )

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert document["zswapped_bytes"] == 3 * _MIB


def test_kernel_and_unattributed_math_with_clamp(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=20 * _MIB, shmem=0, kernel=2 * _MIB, file=0, swap=10 * _MIB, pids=[1])
    write_proc(
        tmp_path, 1, cmdline="ghostty", comm="ghostty", rss_anon_kb=3 * 1024, vm_swap_kb=1024
    )

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert document["kernel_bytes"] == 2 * _MIB
    # ram_bytes = anon + kernel = 22 MiB; minus process ram(3) minus kernel(2) = 17 MiB
    assert document["unattributed_ram_bytes"] == 17 * _MIB
    # swap_bytes(10 MiB) - process swap(1 MiB) = 9 MiB
    assert document["unattributed_swap_bytes"] == 9 * _MIB


def test_unattributed_clamps_at_zero_when_processes_outweigh_the_app(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=1 * _MIB, shmem=0, kernel=0, file=0, swap=0, pids=[1, 2])
    # A shared page counted once per mapping process can make the sum exceed the app row.
    write_proc(tmp_path, 1, cmdline="ghostty", comm="ghostty", rss_anon_kb=800)
    write_proc(tmp_path, 2, cmdline="ghostty", comm="ghostty", rss_anon_kb=800)

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert document["unattributed_ram_bytes"] == 0
    assert document["unattributed_swap_bytes"] == 0


def test_units_are_listed_raw_with_escapes_intact(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    escaped_name = "app-ghostty\\x2d2@abc.service"
    make_unit(user_root / "app.slice" / escaped_name, anon=2 * _MIB)

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert document["units"] == [{"name": escaped_name, "label": "app-ghostty-2@abc.service"}]


def test_age_seconds_is_an_integer(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=1 * _MIB, pids=[1])
    write_proc(tmp_path, 1, cmdline="ghostty", comm="ghostty", starttime_ticks=100)

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    age = document["processes"]["items"][0]["age_seconds"]
    assert isinstance(age, int)


def test_process_private_bytes_present_and_null_when_unreadable(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=2 * _MIB, pids=[1, 2])
    write_proc(tmp_path, 1, cmdline="ghostty", comm="ghostty")
    write_proc(tmp_path, 2, cmdline="sandboxed", comm="sandboxed")
    write_smaps_rollup(tmp_path, 1, private_clean_kb=100, private_dirty_kb=50)
    # PID 2: no smaps_rollup written -- unreadable, same as a sandboxed process.

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    by_pid = {item["pid"]: item["private_bytes"] for item in document["processes"]["items"]}
    assert by_pid[1] == (100 + 50) * 1024
    assert by_pid[2] is None


def test_zswap_pool_bytes_is_the_apps_own_share(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=1 * _MIB, kernel=3 * _MIB, zswap=1 * _MIB, swap=1 * _MIB, pids=[])

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert document["zswap_pool_bytes"] == 1 * _MIB
    assert document["kernel_bytes"] == 2 * _MIB  # 3 MiB kernel - 1 MiB pool


def test_app_with_zero_processes_succeeds_with_empty_items(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=5 * _MIB, shmem=0, kernel=1 * _MIB, file=0, swap=2 * _MIB, pids=[])

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert document["processes"]["items"] == []
    assert document["commands"]["items"] == []
    # ram_bytes = anon + kernel = 6 MiB; minus processes(0) minus kernel(1) = 5 MiB
    assert document["unattributed_ram_bytes"] == 5 * _MIB
    assert document["unattributed_swap_bytes"] == 2 * _MIB


def test_scope_system_finds_a_system_unit_scope_user_does_not(tmp_path: Path) -> None:
    _base_tree(tmp_path)
    system_slice = tmp_path / "sys" / "fs" / "cgroup" / "system.slice"
    make_unit(system_slice / "cups.service", anon=3 * _MIB)

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "cups", "system", limit=100, now=_NOW
    )
    assert document["scope"] == "system"

    with pytest.raises(AppNotFoundError):
        app_document(LinuxBackend(tmp_path, 1000), "cups", "user", limit=100, now=_NOW)


def test_not_found_when_no_unit_matches(tmp_path: Path) -> None:
    _base_tree(tmp_path)

    with pytest.raises(AppNotFoundError):
        app_document(LinuxBackend(tmp_path, 1000), "nosuch", "user", limit=100, now=_NOW)


def test_not_found_when_every_matching_unit_vanishes_before_it_can_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `find_app_units` sees the unit directory, but by the time its
    # `memory.stat` is read the cgroup is gone: the same "app is gone" the
    # TUI's process view shows, not a zeroed-out document.
    user_root = _base_tree(tmp_path)
    make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * _MIB)

    def _vanished(path: Path) -> None:
        return None

    monkeypatch.setattr(collect, "read_unit", _vanished)

    with pytest.raises(AppNotFoundError):
        app_document(LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW)


def test_a_process_vanishing_mid_read_is_skipped_and_the_document_is_complete(
    tmp_path: Path,
) -> None:
    user_root = _base_tree(tmp_path)
    unit = user_root / "app.slice" / "app-ghostty.service"
    # PID 2 is listed in cgroup.procs but has no /proc/2 directory at all: churn,
    # not a partial result.
    make_unit(unit, anon=5 * _MIB, shmem=0, kernel=0, file=0, swap=0, pids=[1, 2])
    write_proc(tmp_path, 1, cmdline="ghostty", comm="ghostty", rss_anon_kb=1024)

    document, _, _ = app_document(
        LinuxBackend(tmp_path, 1000), "ghostty", "user", limit=100, now=_NOW
    )

    assert [item["pid"] for item in document["processes"]["items"]] == [1]
    # `procs` is the unit's own cgroup.procs line count, not the number of
    # processes actually read: PID 2 is still counted there even though it
    # vanished before `/proc/2` could be read.
    assert document["procs"] == 2
