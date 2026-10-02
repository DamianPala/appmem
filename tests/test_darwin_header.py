"""Synthetic Darwin host values; no native API or live process reads."""

from __future__ import annotations

from dataclasses import replace

import pytest
from rich.text import Text
from textual.theme import BUILTIN_THEMES
from textual.widgets import Static

from appmem.darwin_backend import DarwinApp, DarwinBackend
from appmem.darwin_native import HostMemory, Unavailable
from appmem.theme import TERMINAL_THEMES
from appmem.ui.app import AppMemApp
from appmem.ui.darwin_header import render_host_header
from appmem.ui.header import ThemeColors
from appmem.ui.screens.darwin import DarwinMainScreen
from appmem.ui.screens.main import (
    _bar_fill_colour,  # pyright: ignore[reportPrivateUsage] - existing palette rules
    _rich_color,  # pyright: ignore[reportPrivateUsage] - existing ANSI conversion
)

GIB = 1024**3
MIB = 1024**2
HOST = HostMemory(
    physical_bytes=8 * GIB,
    page_size=16_384,
    vm_count=40,
    free_bytes=GIB,
    wired_bytes=2 * GIB,
    active_bytes=2 * GIB,
    inactive_bytes=GIB,
    compressor_physical_bytes=GIB,
    compressor_logical_bytes=3 * GIB,
    swapped_logical_bytes=None,
    swap_used_bytes=512 * MIB,
    swap_total_bytes=GIB,
    pressure_level=1,
    pressure_unavailable=None,
    pressure_error_code=None,
    speculative_bytes=128 * MIB,
    file_backed_bytes=2 * GIB,
    purgeable_bytes=128 * MIB,
)
THEMES = {theme.name: theme for theme in (*BUILTIN_THEMES.values(), *TERMINAL_THEMES)}
COLORS = ThemeColors(success="green", warning="yellow", error="red", primary="blue")


def test_swap_ratio_tracks_current_allocation_instead_of_fixed_capacity() -> None:
    half = render_host_header(HOST, 80, colors=COLORS)[2]
    grown = render_host_header(replace(HOST, swap_total_bytes=2 * GIB), 80, colors=COLORS)[2]
    assert "0.5/1.0 GiB" in half.plain
    assert "0.5/2.0 GiB" in grown.plain
    assert half.plain.count("█") > grown.plain.count("█")
    assert all(span.style in ("blue", "dim") for span in grown.spans)


@pytest.mark.parametrize("width", [40, 65, 80, 120])
def test_zero_swap_is_not_allocated_with_neutral_placeholder(width: int) -> None:
    swap = render_host_header(
        replace(HOST, swap_used_bytes=0, swap_total_bytes=0), width, colors=COLORS
    )[2]
    assert "0 B; not allocated" in swap.plain
    assert all(glyph not in swap.plain for glyph in ("/", "█", "░", "off", "disabled"))


@pytest.mark.parametrize(("used", "total"), [(-1, 100), (1, 0), (101, 100), (0, -1)])
def test_invalid_swap_does_not_render_a_valid_gauge(used: int, total: int) -> None:
    swap = render_host_header(
        replace(HOST, swap_used_bytes=used, swap_total_bytes=total), 120, colors=COLORS
    )[2]
    assert "unavailable" in swap.plain
    assert "█" not in swap.plain and "used" not in swap.plain


@pytest.mark.parametrize(("logical", "physical"), [(0, 0), (0, MIB), (MIB, 0)])
def test_zero_compression_has_values_without_division_ratio(logical: int, physical: int) -> None:
    status = render_host_header(
        replace(HOST, compressor_logical_bytes=logical, compressor_physical_bytes=physical),
        120,
        colors=COLORS,
    )[1]
    assert "Compress" in status.plain and "data →" in status.plain and "RAM" in status.plain
    assert ":1" not in status.plain


@pytest.mark.parametrize(("logical", "physical"), [(-1, MIB), (MIB, -1)])
def test_invalid_compression_is_unavailable(logical: int, physical: int) -> None:
    status = render_host_header(
        replace(HOST, compressor_logical_bytes=logical, compressor_physical_bytes=physical),
        120,
        colors=COLORS,
    )[1]
    assert "Compress" in status.plain and "unavailable" in status.plain
    assert ":1" not in status.plain


@pytest.mark.parametrize(
    ("level", "word", "color"),
    [
        (1, "normal", "green"),
        (2, "warning", "yellow"),
        (4, "critical", "red"),
        (None, "unavailable", "dim"),
        (8, "unavailable", "dim"),
    ],
)
def test_pressure_is_colored_native_state_without_numeric_percentage(
    level: int | None,
    word: str,
    color: str,
) -> None:
    status = render_host_header(replace(HOST, pressure_level=level), 120, colors=COLORS)[3]
    assert "Pressure" in status.plain and word in status.plain
    assert "%" not in status.plain
    styles = [str(span.style) for span in status.spans]
    assert any(color in style for style in styles)


def test_pressure_failure_overrides_stale_level() -> None:
    status = render_host_header(
        replace(HOST, pressure_unavailable=Unavailable.ERROR), 120, colors=COLORS
    )[3]
    assert "Pressure" in status.plain and "unavailable" in status.plain
    assert "normal" not in status.plain


@pytest.mark.parametrize("ascii_bars", [False, True])
@pytest.mark.parametrize("theme_name", list(THEMES))
def test_all_themes_render_gauges_and_pressure_with_existing_palette(
    theme_name: str,
    ascii_bars: bool,
) -> None:
    theme = THEMES[theme_name]
    colors = ThemeColors(
        success=_rich_color(theme.success or "green"),
        warning=_rich_color(theme.warning or "yellow"),
        error=_rich_color(theme.error or "red"),
        primary=_bar_fill_colour(theme),
    )
    _, compression, swap, status = render_host_header(
        HOST, 120, colors=colors, ascii_bars=ascii_bars
    )
    assert any(span.style == colors.primary for span in swap.spans)
    assert any(span.style == f"bold {colors.success}" for span in status.spans)
    if ascii_bars:
        assert "#" in swap.plain and "." in swap.plain and ">" in compression.plain
        assert all(ord(char) < 128 for line in (swap, status) for char in line.plain)
    else:
        assert "█" in swap.plain and "░" in swap.plain and "→" in compression.plain


@pytest.mark.asyncio
async def test_live_screen_updates_header_on_resize_theme_and_ascii_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "C")
    backend = object.__new__(DarwinBackend)
    monkeypatch.setattr(backend, "read_system", lambda: HOST)
    empty_apps: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: empty_apps)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        swap = app.screen.query_one("#header3", Static).content
        assert isinstance(swap, Text) and "#" in swap.plain
        before = swap.spans
        app.theme = "dracula"
        await pilot.pause()
        swap = app.screen.query_one("#header3", Static).content
        assert isinstance(swap, Text) and swap.spans != before
        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        swap = app.screen.query_one("#header3", Static).content
        assert isinstance(swap, Text) and swap.cell_len <= 40 and swap.plain.endswith(">")
        assert "Swap" in swap.plain


def test_partition_counts_speculative_once_and_keeps_reserved_used() -> None:
    partition = HOST.ram_partition
    assert partition is not None
    assert partition == (5 * GIB + 128 * MIB, 2 * GIB, GIB - 128 * MIB)
    assert sum(partition) == HOST.physical_bytes
    # Purgeable overlaps used and must not change the partition.
    assert replace(HOST, purgeable_bytes=0).ram_partition == HOST.ram_partition
    assert replace(HOST, active_bytes=0, inactive_bytes=0).ram_partition == HOST.ram_partition


@pytest.mark.parametrize(
    "changes",
    [
        {"speculative_bytes": None},
        {"file_backed_bytes": None},
        {"speculative_bytes": -1},
        {"file_backed_bytes": -1},
        {"speculative_bytes": GIB + 1},
        {"file_backed_bytes": 64 * MIB},
        {"file_backed_bytes": 9 * GIB},
        {"free_bytes": 9 * GIB},
        {"physical_bytes": 0},
        {"physical_bytes": True},
        {"wired_bytes": 6 * GIB},
        {"compressor_physical_bytes": -1},
        {"file_backed_bytes": 8 * GIB},
    ],
)
def test_contradictory_partition_has_explicit_unavailable(changes: dict[str, int | None]) -> None:
    host = replace(HOST, **changes)
    assert host.ram_partition is None
    for width in (40, 80, 120):
        ram = render_host_header(host, width, colors=COLORS)[0]
        assert "used unavailable" in ram.plain
        assert "█" not in ram.plain
        assert ram.cell_len <= width


@pytest.mark.parametrize("width", [65, 80, 120])
def test_zero_swap_placeholder_occupies_the_allocated_gauge_slot(width: int) -> None:
    allocated = render_host_header(HOST, width, colors=COLORS)[2]
    unallocated = render_host_header(
        replace(HOST, swap_used_bytes=0, swap_total_bytes=0), width, colors=COLORS
    )[2]
    # A neutral dash track cannot imply used/available capacity when no denominator exists.
    assert unallocated.plain.count("-") == sum(
        0x2580 <= ord(char) <= 0x259F for char in allocated.plain
    )
    assert all(span.style == "dim" for span in unallocated.spans)


def test_pressure_baseline_and_scope_fit_and_remain_ascii() -> None:
    status = render_host_header(
        HOST, 120, colors=COLORS, ascii_bars=True, baseline_time="12:34:56", baseline_elapsed=42
    )[3]
    assert "apps: this user" in status.plain
    assert "delta since 12:34 (42s)" in status.plain
    assert all(ord(char) < 128 for char in status.plain)


@pytest.mark.parametrize("width", [80, 100, 120, 160])
def test_ram_slots_stay_stable_across_normal_counter_changes(width: int) -> None:
    first = render_host_header(HOST, width, colors=COLORS, ascii_bars=True)[0].plain
    second = render_host_header(
        replace(HOST, physical_bytes=16 * GIB, wired_bytes=900 * MIB, purgeable_bytes=9 * MIB),
        width,
        colors=COLORS,
        ascii_bars=True,
    )[0].plain
    assert first.index("file-backed") == second.index("file-backed")
    assert set(first[10:20]) <= {"#", "."}
    assert "wired" in first if width >= 100 else "wired" not in first
    assert len(first) <= width and len(second) <= width


@pytest.mark.parametrize("width", [80, 100, 120, 160])
def test_long_elapsed_uses_existing_compact_format(width: int) -> None:
    line = render_host_header(
        HOST, width, colors=COLORS, baseline_time="09:41:59", baseline_elapsed=4320
    )[3].plain
    assert ("Δ since 09:41 (1h12m)" if width >= 110 else "Δ 09:41 (1h12m)") in line
    line = render_host_header(
        HOST, width, colors=COLORS, baseline_time="09:41", baseline_elapsed=120 * 86400
    )[3].plain
    assert "(120d)" in line


@pytest.mark.parametrize("width", [80, 100, 120, 160])
def test_swap_activity_retains_allocation_across_used_digit_boundary(width: int) -> None:
    lines = [
        render_host_header(
            replace(HOST, swap_used_bytes=used * GIB, swap_total_bytes=16 * GIB),
            width,
            colors=COLORS,
            swap_in_rate=1024,
            swap_out_rate=1024,
        )[2].plain
        for used in (5, 10)
    ]
    for used, line in zip((5, 10), lines, strict=True):
        assert f"{used:.1f}/16.0 GiB used/alloc" in line
        assert "in 1 KiB/s" in line and "out 1 KiB/s" in line
    assert all(len(line) <= width for line in lines)


@pytest.mark.parametrize("width", [40, 60, 80, 120, 160])
def test_host_grid_keeps_native_values_and_alignment(width: int) -> None:
    ram, compression, swap, pressure = render_host_header(HOST, width, colors=COLORS)
    assert all(
        line.cell_len <= width and line.no_wrap for line in (ram, compression, swap, pressure)
    )
    assert "5.1/8.0 GiB used" in ram.plain
    assert "1.0 GiB RAM" in compression.plain
    assert "0.5/1.0 GiB used" in swap.plain
    assert "normal" in pressure.plain
    assert ram.plain.index("5.1/") == compression.plain.index("1.0 GiB") == swap.plain.index("0.5/")
    assert not any(glyph in compression.plain for glyph in ("█", "░", "#"))
    if width >= 80:
        assert "in " in swap.plain and "used/alloc" in swap.plain
        assert "out " in swap.plain
    if width >= 120:
        assert all(label in ram.plain for label in ("file-backed", "free", "wired", "purgeable"))
        assert "used/alloc" in swap.plain


def test_large_native_values_preserve_metadata_fields_when_actual_text_fits() -> None:
    host = replace(HOST, physical_bytes=128 * GIB, wired_bytes=100 * GIB)
    ram = render_host_header(host, 121, colors=COLORS)[0]
    assert ram.cell_len <= 121
    assert all(label in ram.plain for label in ("file-backed", "free", "wired", "purgeable"))
    assert "wired 100.0 GiB" in ram.plain
    assert not ram.plain.endswith("…")
