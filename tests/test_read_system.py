"""Tests for `appmem.collect.read_system` (SPEC.md "Data sources", "Behaviour details")."""

import os
from pathlib import Path

from appmem.collect import read_system
from helpers import (
    make_unit,
    user_service_root,
    write_meminfo,
    write_memory_stat,
    write_pressure,
    write_vmstat,
    write_zswap_enabled,
)

UID = 1000


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

    stats = read_system(tmp_path, UID)

    assert stats.mem_total == 32_826_556 * 1024
    assert stats.mem_available == 11_534_336 * 1024
    assert stats.swap_total == 33_554_432 * 1024
    assert stats.swap_free == 12_582_912 * 1024
    assert stats.pressure_some_avg10 == 0.0
    assert stats.pressure_full_avg10 == 0.0
    assert stats.system_ram == 100
    assert stats.system_swap == 7


# --- RAM breakdown: free / shared / cache (SPEC.md "Definitions") ---


def test_reads_free_shared_and_cache_from_meminfo(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=0,
        swap_free_kb=0,
        mem_free_kb=2_500_000,
        cached_kb=8_600_000,
        shmem_kb=2_500_000,
    )

    stats = read_system(tmp_path, UID)

    assert stats.mem_free == 2_500_000 * 1024
    assert stats.mem_shared == 2_500_000 * 1024
    # cache = Cached - Shmem, the same definition as the per-app CACHE column.
    assert stats.mem_cache == (8_600_000 - 2_500_000) * 1024


def test_cache_is_clamped_at_zero_when_shmem_exceeds_cached(tmp_path: Path) -> None:
    # A malformed or transiently inconsistent meminfo (Shmem counted inside
    # Cached is supposed to be <=, but nothing guarantees it on every kernel):
    # the header must never show a negative cache figure.
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=0,
        swap_free_kb=0,
        cached_kb=1_000,
        shmem_kb=5_000,
    )

    stats = read_system(tmp_path, UID)

    assert stats.mem_cache == 0


def test_free_shared_and_cache_default_to_zero_when_meminfo_lacks_them(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )

    stats = read_system(tmp_path, UID)

    assert stats.mem_free == 0
    assert stats.mem_shared == 0
    assert stats.mem_cache == 0


def test_pressure_reads_avg60_for_some_and_full(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    write_pressure(tmp_path, some_avg10=0.5, full_avg10=0.0, some_avg60=2.3, full_avg60=1.1)

    stats = read_system(tmp_path, UID)

    assert stats.pressure_some_avg60 == 2.3
    assert stats.pressure_full_avg60 == 1.1


def test_pressure_none_when_file_missing(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )

    stats = read_system(tmp_path, UID)

    assert stats.pressure_some_avg10 is None
    assert stats.pressure_some_avg60 is None
    assert stats.pressure_full_avg10 is None
    assert stats.pressure_full_avg60 is None


def test_missing_system_slice_defaults_to_zero(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )

    stats = read_system(tmp_path, UID)

    assert stats.system_ram == 0
    assert stats.system_swap == 0


# --- elsewhere (SPEC.md "Behaviour details") ------------------------------------


def test_elsewhere_is_root_minus_user_tree_minus_system_slice(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    write_memory_stat(
        tmp_path / "sys" / "fs" / "cgroup", anon=1000, shmem=0, kernel=0, file=0
    )  # root total: 1000
    write_memory_stat(user_service_root(tmp_path, UID), anon=300, shmem=0, kernel=0, file=0)
    make_unit(
        tmp_path / "sys" / "fs" / "cgroup" / "system.slice", anon=200, shmem=0, kernel=0, file=0
    )

    stats = read_system(tmp_path, UID)

    assert stats.elsewhere == 1000 - 300 - 200


def test_elsewhere_clamped_at_zero(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    write_memory_stat(tmp_path / "sys" / "fs" / "cgroup", anon=10, shmem=0, kernel=0, file=0)
    write_memory_stat(user_service_root(tmp_path, UID), anon=300, shmem=0, kernel=0, file=0)

    stats = read_system(tmp_path, UID)

    assert stats.elsewhere == 0


def test_elsewhere_is_none_when_root_memory_stat_missing(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    write_memory_stat(user_service_root(tmp_path, UID), anon=300, shmem=0, kernel=0, file=0)

    stats = read_system(tmp_path, UID)

    assert stats.elsewhere is None


# --- zswap (SPEC.md "Data sources", "Main view") --------------------------------


def _zswap_meminfo(tmp_path: Path, *, zswap_kb: int, zswapped_kb: int) -> None:
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
        zswap_kb=zswap_kb,
        zswapped_kb=zswapped_kb,
    )


def test_zswap_enabled_with_pool_and_zswapped_bytes(tmp_path: Path) -> None:
    _zswap_meminfo(tmp_path, zswap_kb=2_000_000, zswapped_kb=7_000_000)
    write_zswap_enabled(tmp_path, enabled=True)

    stats = read_system(tmp_path, UID)

    assert stats.zswap_enabled is True
    assert stats.zswap_pool_bytes == 2_000_000 * 1024
    assert stats.zswapped_bytes == 7_000_000 * 1024


def test_zswap_disabled_by_the_parameter_file_nulls_the_bytes(tmp_path: Path) -> None:
    # Meminfo has the fields (support compiled in), but the knob reads N.
    _zswap_meminfo(tmp_path, zswap_kb=2_000_000, zswapped_kb=7_000_000)
    write_zswap_enabled(tmp_path, enabled=False)

    stats = read_system(tmp_path, UID)

    assert stats.zswap_enabled is False
    assert stats.zswap_pool_bytes is None
    assert stats.zswapped_bytes is None


def test_zswap_missing_parameter_file_reads_as_disabled(tmp_path: Path) -> None:
    _zswap_meminfo(tmp_path, zswap_kb=2_000_000, zswapped_kb=7_000_000)
    # No .../parameters/enabled written at all: no zswap knob on this kernel.

    stats = read_system(tmp_path, UID)

    assert stats.zswap_enabled is False
    assert stats.zswap_pool_bytes is None
    assert stats.zswapped_bytes is None


def test_zswap_missing_meminfo_fields_reads_as_unsupported(tmp_path: Path) -> None:
    # The knob says Y, but meminfo has no Zswap/Zswapped fields: an older
    # kernel with no zswap support at all overrides the knob.
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
    )
    write_zswap_enabled(tmp_path, enabled=True)

    stats = read_system(tmp_path, UID)

    assert stats.zswap_enabled is False
    assert stats.zswap_pool_bytes is None
    assert stats.zswapped_bytes is None


def test_zswap_writeback_bytes_from_vmstat_zswpwb(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    write_vmstat(tmp_path, zswpwb=100)

    stats = read_system(tmp_path, UID)

    assert stats.zswap_writeback_bytes == 100 * os.sysconf("SC_PAGE_SIZE")


def test_zswap_writeback_bytes_is_none_without_the_vmstat_counter(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    write_vmstat(tmp_path, zswpwb=None)

    stats = read_system(tmp_path, UID)

    assert stats.zswap_writeback_bytes is None


def test_zswap_writeback_bytes_is_none_without_a_vmstat_file(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path, mem_total_kb=1000, mem_available_kb=500, swap_total_kb=0, swap_free_kb=0
    )
    # No proc/vmstat written at all.

    stats = read_system(tmp_path, UID)

    assert stats.zswap_writeback_bytes is None
