"""Tests for `appmem.collect.read_system` (SPEC.md "Data sources")."""

from pathlib import Path

from appmem.collect import read_system
from helpers import make_unit, write_meminfo, write_pressure


def test_reads_meminfo_and_hidden_system_total(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path,
        mem_total_kb=32_826_556,
        mem_available_kb=11_534_336,
        swap_total_kb=33_554_432,
        swap_free_kb=12_582_912,
    )
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)
    make_unit(
        tmp_path / "sys" / "fs" / "cgroup" / "system.slice",
        anon=100,
        shmem=0,
        kernel=0,
        file=0,
        swap=7,
    )

    stats = read_system(tmp_path)

    assert stats.mem_total == 32_826_556 * 1024
    assert stats.mem_available == 11_534_336 * 1024
    assert stats.swap_total == 33_554_432 * 1024
    assert stats.swap_free == 12_582_912 * 1024
    assert stats.pressure_some_avg10 == 0.0
    assert stats.pressure_full_avg10 == 0.0
    assert stats.system_ram == 100
    assert stats.system_swap == 7


def test_pressure_none_when_file_missing(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )

    stats = read_system(tmp_path)

    assert stats.pressure_some_avg10 is None
    assert stats.pressure_full_avg10 is None


def test_missing_system_slice_defaults_to_zero(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )

    stats = read_system(tmp_path)

    assert stats.system_ram == 0
    assert stats.system_swap == 0
