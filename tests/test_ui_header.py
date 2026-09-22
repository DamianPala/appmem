"""Tests for `appmem.ui.header` (SPEC.md "Main view" header lines)."""

from dataclasses import replace
from datetime import datetime
from typing import Any

from appmem.collect import SystemStats
from appmem.ui.header import format_line1, format_line2

_DEFAULT_STATS = SystemStats(
    mem_total=30_000_000_000,
    mem_available=11_000_000_000,
    swap_total=32_000_000_000,
    swap_free=12_000_000_000,
    pressure_some_avg10=None,
    pressure_some_avg60=None,
    pressure_full_avg10=None,
    pressure_full_avg60=None,
    system_ram=0,
    system_swap=0,
    elsewhere=None,
)


def _stats(**overrides: Any) -> SystemStats:
    return replace(_DEFAULT_STATS, **overrides)


def test_line1_includes_ram_avail_swap_and_system_total() -> None:
    line = format_line1(_stats(system_ram=500 * 1024**2, system_swap=0))

    assert "RAM" in line
    assert "avail" in line
    assert "Swap" in line
    assert "system 500 MiB (x)" in line


def test_line1_swap_off_when_swap_total_zero() -> None:
    line = format_line1(_stats(swap_total=0, swap_free=0))

    assert "Swap off" in line
    assert "Swap 0" not in line


def test_line1_omits_pressure_when_none() -> None:
    line = format_line1(
        _stats(pressure_some_avg10=None, pressure_some_avg60=None, pressure_full_avg10=None)
    )

    assert "pressure" not in line


def test_line1_shows_pressure_word_with_window_label() -> None:
    line = format_line1(
        _stats(pressure_some_avg10=0.5, pressure_some_avg60=0.0, pressure_full_avg10=0.0)
    )

    assert "pressure 10s: none" in line


def test_line1_system_total_is_ram_plus_swap_with_no_process_count() -> None:
    # SystemStats.system_ram/system_swap carry no process count (the collector
    # skips the recursive count for system.slice); this must never render as if
    # it had one.
    line = format_line1(_stats(system_ram=300 * 1024**2, system_swap=200 * 1024**2))

    assert "system 500 MiB (x)" in line


def test_line1_shares_unit_between_used_and_total_when_equal() -> None:
    # SPEC.md "Main-view polish": `RAM 18.7/30.9 GiB`, not `RAM 18.7 GiB / 30.9 GiB`.
    line = format_line1(_stats(mem_total=30_900_000_000, mem_available=12_100_000_000))

    assert "RAM 17.5/28.8 GiB" in line


def test_line1_shows_elsewhere_after_system_when_above_threshold() -> None:
    line = format_line1(_stats(elsewhere=2 * 1024**2))

    assert line.endswith("elsewhere 2 MiB")


def test_line1_omits_elsewhere_below_1mib() -> None:
    line = format_line1(_stats(elsewhere=1024**2 - 1))

    assert "elsewhere" not in line


def test_line1_omits_elsewhere_when_none() -> None:
    line = format_line1(_stats(elsewhere=None))

    assert "elsewhere" not in line


def test_line2_format() -> None:
    baseline = datetime(2026, 1, 1, 14, 2, 0)
    now = datetime(2026, 1, 1, 14, 7, 0)

    line = format_line2(baseline, now)

    assert line == "Δ since 14:02 (5m)"
