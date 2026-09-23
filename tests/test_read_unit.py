"""Tests for `appmem.collect.read_unit` (SPEC.md "Definitions")."""

import os
from pathlib import Path

import pytest

from appmem.collect import read_unit
from helpers import make_unit, write_cgroup_procs, write_memory_stat


def test_ram_cache_swap_total_math(tmp_path: Path) -> None:
    unit_dir = make_unit(
        tmp_path / "unit.service",
        anon=100,
        shmem=20,
        kernel=5,
        file=50,
        swap=30,
        pids=[1, 2, 3],
    )

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.ram == 100 + 20 + 5  # anon + shmem + kernel
    assert stats.cache == 50 - 20  # file - shmem
    assert stats.swap == 30
    assert stats.total == 30 + (100 + 20 + 5)  # swap + RAM
    assert stats.procs == 3
    assert stats.kernel == 5


def test_zswapped_read_from_memory_stat(tmp_path: Path) -> None:
    unit_dir = make_unit(tmp_path / "unit.service", anon=100, swap=30, zswapped=12)

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.zswapped == 12


def test_zswapped_defaults_to_zero_when_the_field_is_absent(tmp_path: Path) -> None:
    # A kernel/cgroup without zswap accounting: no `zswapped` line at all,
    # not an error (SPEC.md "Data sources").
    unit_dir = make_unit(tmp_path / "unit.service", anon=100, swap=30)

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.zswapped == 0


def test_kernel_missing_falls_back_to_slab_stack_pagetables_percpu(tmp_path: Path) -> None:
    # Linux < 5.18 has no `kernel` line in memory.stat.
    unit_dir = tmp_path / "unit.service"
    unit_dir.mkdir()
    (unit_dir / "memory.stat").write_text(
        "anon 100\nshmem 20\nfile 50\nslab 3\nkernel_stack 2\npagetables 4\npercpu 1\n"
    )
    write_cgroup_procs(unit_dir, [])

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.kernel == 3 + 2 + 4 + 1
    assert stats.ram == 100 + 20 + (3 + 2 + 4 + 1)


def test_kernel_missing_and_no_fallback_parts_defaults_to_zero(tmp_path: Path) -> None:
    unit_dir = tmp_path / "unit.service"
    unit_dir.mkdir()
    (unit_dir / "memory.stat").write_text("anon 100\nshmem 0\nfile 0\n")
    write_cgroup_procs(unit_dir, [])

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.kernel == 0
    assert stats.ram == 100


def test_missing_swap_current_defaults_to_zero(tmp_path: Path) -> None:
    unit_dir = tmp_path / "unit.service"
    write_memory_stat(unit_dir, anon=10, shmem=0, kernel=0, file=0)
    write_cgroup_procs(unit_dir, [])
    # No memory.swap.current file written.

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.swap == 0
    assert stats.total == stats.ram


def test_missing_unit_returns_none(tmp_path: Path) -> None:
    assert read_unit(tmp_path / "gone.service") is None


def test_empty_memory_stat_returns_none(tmp_path: Path) -> None:
    unit_dir = tmp_path / "unit.service"
    unit_dir.mkdir()
    (unit_dir / "memory.stat").write_text("")

    assert read_unit(unit_dir) is None


def test_partial_memory_stat_returns_none(tmp_path: Path) -> None:
    unit_dir = tmp_path / "unit.service"
    unit_dir.mkdir()
    # Only "anon" present: a unit that vanished mid-write of its own counters.
    (unit_dir / "memory.stat").write_text("anon 100\n")

    assert read_unit(unit_dir) is None


def test_procs_counted_recursively_below_the_unit(tmp_path: Path) -> None:
    unit_dir = make_unit(tmp_path / "unit.service", pids=[1, 2])
    write_cgroup_procs(unit_dir / "tab(1).scope", [3, 4, 5])

    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.procs == 5


def test_procs_counted_recursively_when_dirs_report_link_count_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # btrfs reports st_nlink == 1 for every directory, so the "leaf dir has
    # link count 2" shortcut must not skip the scan there.
    unit_dir = make_unit(tmp_path / "unit.service", pids=[1, 2])
    write_cgroup_procs(unit_dir / "tab(1).scope", [3, 4, 5])
    real_stat = os.stat

    def btrfs_stat(path: str) -> os.stat_result:
        result = real_stat(path)
        return os.stat_result((*result[:3], 1, *result[4:]))

    monkeypatch.setattr(os, "stat", btrfs_stat)
    stats = read_unit(unit_dir)

    assert stats is not None
    assert stats.procs == 5
