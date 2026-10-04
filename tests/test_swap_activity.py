"""Fixture-only host activity and physical Zswap gauge contracts."""

from __future__ import annotations

import ctypes
import re
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import Static

import appmem.collect as collect
from appmem.collect import LinuxBackend
from appmem.darwin_native import VMStatistics64, decode_vm
from appmem.model import SystemStats
from appmem.rate import Sample, update_rate
from appmem.ui.app import AppMemApp
from appmem.ui.header import ThemeColors, render_header
from appmem.ui.screens.main import MainScreen
from helpers import make_unit, user_service_root, write_meminfo, write_zswap_enabled

GIB = 1024**3
COLORS = ThemeColors("green", "yellow", "red", "blue")
STATS = SystemStats(
    mem_total=30 * GIB,
    mem_available=10 * GIB,
    swap_total=32 * GIB,
    swap_free=24 * GIB,
    pressure_some_avg10=0,
    pressure_some_avg60=0,
    pressure_full_avg10=0,
    pressure_full_avg60=0,
    system_ram=0,
    system_swap=0,
    elsewhere=None,
    zswap_enabled=True,
    zswap_pool_bytes=GIB,
    zswapped_bytes=4 * GIB,
    zswap_max_pool_percent=20,
    zswap_compression_ratio=4,
)


def header(stats: SystemStats = STATS, width: int = 120, height: int = 30) -> list[Text]:
    return render_header(
        stats,
        width,
        height,
        colors=COLORS,
        baseline_time=datetime(2026, 1, 1),
        now=datetime(2026, 1, 1),
        swap_in_rate=0,
        swap_out_rate=1024,
    )


@pytest.mark.parametrize("page_size", [4096, 16384])
def test_linux_page_counters_are_independent_bytes(tmp_path: Path, page_size: int) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/vmstat").write_text("pswpin 3\npswpout 7\nzswpwb 2\n")
    stats = collect.read_system(tmp_path, 1000, page_size=page_size)
    assert stats.swap_in_bytes == 3 * page_size
    assert stats.swap_out_bytes == 7 * page_size
    assert stats.zswap_writeback_bytes == 2 * page_size
    assert stats.zswap_enabled is False


@pytest.mark.parametrize("value", ["", "-1", "oops", "2 extra"])
def test_linux_invalid_direction_does_not_hide_other(tmp_path: Path, value: str) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/vmstat").write_text(f"pswpin {value}\npswpout 0\n")
    stats = collect.read_system(tmp_path, 1000, page_size=4096)
    assert stats.swap_in_bytes is None
    assert stats.swap_out_bytes == 0


def test_linux_vmstat_reads_once_and_denial_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = collect._read_small_file  # pyright: ignore[reportPrivateUsage]
    reads: list[str] = []

    def read(path: str, **kwargs: bool) -> str:
        if path.endswith("/proc/vmstat"):
            reads.append(path)
            raise PermissionError(path)
        return original(path, **kwargs)

    monkeypatch.setattr(collect, "_read_small_file", read)
    stats = collect.read_system(tmp_path, 1000, page_size=4096)
    assert len(reads) == 1
    assert stats.swap_in_bytes is None and stats.swap_out_bytes is None


@pytest.mark.parametrize("page_size", [4096, 16384])
@pytest.mark.parametrize("count", [38, ctypes.sizeof(VMStatistics64) // 4])
def test_darwin_nonzero_distinct_directions_prefix(count: int, page_size: int) -> None:
    vm = VMStatistics64()
    vm.swapins, vm.swapouts = 3, 7
    decoded = decode_vm(vm, count, page_size)
    assert decoded["swap_in"] == 3 * page_size
    assert decoded["swap_out"] == 7 * page_size


@pytest.mark.parametrize("wall", [-10, 10, 100])
def test_clock_discontinuity_in_both_directions(wall: float) -> None:
    history = (Sample(0, 0, 0),)
    history, rate = update_rate(history, 1, 1000, wall=wall)
    assert history == (Sample(1, wall, 1000),) and rate is None
    _, rate = update_rate(history, 2, 2000, wall=wall + 1)
    assert rate == 1000


@pytest.mark.parametrize("value", [None, -1])
def test_missing_direction_clears_only_its_history(value: int | None) -> None:
    inside, _ = update_rate((Sample(0, 0, 100),), 1, value, wall=1)
    outside, rate = update_rate((Sample(0, 0, 100),), 1, 200, wall=1)
    assert inside == ()
    assert len(outside) == 2 and rate == 100


@pytest.mark.parametrize("interval", [1, 2, 60])
def test_refresh_intervals_measure_nonzero_and_zero(interval: float) -> None:
    history, _ = update_rate((), 0, 0, wall=0, interval=interval)
    history, rate = update_rate(history, interval, 600, wall=interval, interval=interval)
    assert rate == round(600 / interval)
    _, rate = update_rate(history, 2 * interval, 600, wall=2 * interval, interval=interval)
    assert rate == (0 if interval >= 10 else round(600 / (2 * interval)))


@pytest.mark.parametrize("elapsed", [0, -1, 31, 200])
def test_duplicate_backward_or_long_gap_is_unknown(elapsed: float) -> None:
    history, rate = update_rate((Sample(0, 0, 0),), elapsed, 1000, wall=elapsed)
    assert history == (Sample(elapsed, elapsed, 1000),) and rate is None


@pytest.mark.parametrize("width", [100, 120, 160])
def test_zswap_gauge_physical_and_activity_fit(width: int) -> None:
    ram, zswap, swap, pressure = header(width=width)
    assert all(line.cell_len <= width for line in (ram, zswap, swap, pressure))
    assert re.search(r"1\.0/6\.0\s+GiB RAM", zswap.plain)
    assert re.search(r"holds\s+4.0 GiB", zswap.plain)
    assert re.search(r"in\s+0 B/s", swap.plain) and re.search(r"out\s+1 KiB/s", swap.plain)


@pytest.mark.parametrize("percent", [None, 0, 1])
def test_zswap_unknown_zero_and_over_limit(percent: int | None) -> None:
    line = header(replace(STATS, zswap_max_pool_percent=percent), width=160)[1]
    assert line.cell_len <= 160
    assert re.search(r"holds\s+4.0 GiB", line.plain)
    if percent is None:
        assert "/— RAM" in line.plain and "limit —" in line.plain
    elif percent == 0:
        assert "above 0%" in line.plain and not line.plain.endswith("…")
    else:
        assert "above 1%" in line.plain
        assert "█" * 20 in line.plain


def test_off_missing_short_and_narrow_keep_previous_shapes() -> None:
    assert len(header(replace(STATS, zswap_enabled=False))) == 3
    assert len(header(replace(STATS, zswap_pool_bytes=None))) == 4
    assert "unavailable" in header(replace(STATS, zswap_pool_bytes=None))[1].plain
    assert len(header(width=79)) == 4
    assert len(header(height=17)) == 2
    assert len(header(width=40, height=10)) == 2


def test_unknown_zero_and_activity_slots_ascii_and_unicode() -> None:
    for ascii_bars in (False, True):
        rendered: list[str] = []
        for rate in (None, 0, 1024, 100 * 1024**2):
            lines = render_header(
                STATS,
                120,
                30,
                colors=COLORS,
                baseline_time=datetime(2026, 1, 1),
                now=datetime(2026, 1, 1),
                swap_in_rate=rate,
                swap_out_rate=rate,
                ascii_bars=ascii_bars,
            )
            rendered.append(lines[2].plain)
        assert ("?" if ascii_bars else "—") in rendered[0]
        assert "0 B/s" in rendered[1]
        assert len({line.index("in ") for line in rendered}) == 1
        assert all("out " in line for line in rendered)
        assert all(len(line) <= 120 for line in rendered)


@pytest.mark.asyncio
async def test_zswap_resize_header_height_preserves_table_selection(tmp_path: Path) -> None:
    write_meminfo(
        tmp_path,
        mem_total_kb=30 * 1024**2,
        mem_available_kb=10 * 1024**2,
        swap_total_kb=32 * 1024**2,
        swap_free_kb=24 * 1024**2,
        zswap_kb=1024**2,
        zswapped_kb=4 * 1024**2,
    )
    write_zswap_enabled(tmp_path, enabled=True)
    user = user_service_root(tmp_path, uid=1000)
    make_unit(user, anon=1024, shmem=0, kernel=0, file=0, swap=0)
    unit = user / "app.slice/app-one.service"
    make_unit(unit, anon=1024, shmem=0, kernel=0, file=0, swap=0)
    app = AppMemApp(backend=LinuxBackend(tmp_path, 1000), interval=3600, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        table = screen.query_one("#table")
        assert table.region.y == 4
        assert screen.query_one("#header4", Static).display
        now, wall = time.monotonic(), time.time()
        screen._swap_in_history = (Sample(now - 1, wall - 1, 0),)  # pyright: ignore[reportPrivateUsage]
        screen._swap_out_history = (Sample(now - 1, wall - 1, 0),)  # pyright: ignore[reportPrivateUsage]
        (tmp_path / "proc/vmstat").write_text("pswpin 3\npswpout 7\n")
        screen.refresh_now()
        assert screen._swap_in_rate is not None  # pyright: ignore[reportPrivateUsage]
        assert screen._swap_out_rate is not None  # pyright: ignore[reportPrivateUsage]
        await pilot.press("b")
        await pilot.pause()
        assert screen._swap_in_history[0].value == 0  # pyright: ignore[reportPrivateUsage]
        await pilot.press("?")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert screen._swap_out_history[0].value == 0  # pyright: ignore[reportPrivateUsage]
        assert screen._swap_out_rate is not None  # pyright: ignore[reportPrivateUsage]
        await pilot.resize_terminal(120, 17)
        await pilot.pause()
        assert table.region.y == 2, (
            pilot.app.size,
            screen.size,
            screen.query_one("#header4", Static).display,
        )
        assert not screen.query_one("#header4", Static).display
        await pilot.resize_terminal(120, 30)
        await pilot.pause()
        assert table.region.y == 4
