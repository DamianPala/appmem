"""Fixture-only grid, color and gauge behavior for the Linux host header."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from rich.text import Text
from textual.theme import BUILTIN_THEMES

from appmem.collect import SystemStats
from appmem.fmt import format_pair
from appmem.ui.header import (
    ThemeColors,
    _bar_glyphs,  # pyright: ignore[reportPrivateUsage]
    format_delta_since,
    render_header,
)

_COLORS = ThemeColors(success="green", warning="yellow", error="red", primary="blue")

_BASELINE = datetime(2026, 1, 1, 15, 58, 0)
_NOW = datetime(2026, 1, 1, 16, 10, 0)  # 12m after baseline

# A worked example spanning every part this header can show: RAM 22.1/30.9
# GiB, avail 8.8 (1.5 free, 4.6 cache, 3.2 slab), shared 3.5, Swap 23.3/32.0,
# zswap 3.0 GiB in a 1.0 GiB pool, system 610 MiB, elsewhere 109 MiB,
# pressure none.
_GIB = 1024**3
_MIB = 1024**2
_DEFAULT_STATS = SystemStats(
    mem_total=int(30.9 * _GIB),
    mem_available=int(8.8 * _GIB),
    mem_free=int(1.5 * _GIB),
    mem_cache=int(4.6 * _GIB),
    mem_slab=int(3.2 * _GIB),
    mem_shared=int(3.5 * _GIB),
    swap_total=int(32.0 * _GIB),
    swap_free=int((32.0 - 23.3) * _GIB),
    pressure_some_avg10=0.0,
    pressure_some_avg60=0.0,
    pressure_full_avg10=0.0,
    pressure_full_avg60=0.0,
    system_ram=610 * _MIB,
    system_swap=0,
    elsewhere=109 * _MIB,
)

_ZSWAP_STATS = replace(
    _DEFAULT_STATS,
    zswap_enabled=True,
    zswapped_bytes=int(3.0 * _GIB),
    zswap_pool_bytes=int(1.0 * _GIB),
)


def _stats(**overrides: Any) -> SystemStats:
    return replace(_DEFAULT_STATS, **overrides)


def _render(
    stats: SystemStats,
    width: int,
    height: int = 40,
    *,
    colors: ThemeColors = _COLORS,
    writeback_rate: int | None = None,
    ascii_bars: bool = False,
) -> list[Text]:
    return render_header(
        stats,
        width,
        height,
        colors=colors,
        baseline_time=_BASELINE,
        now=_NOW,
        writeback_rate=writeback_rate,
        ascii_bars=ascii_bars,
    )


def _style_at(text: Text, substr: str) -> str:
    start = text.plain.index(substr)
    end = start + len(substr)
    styles = [str(span.style) for span in text.spans if span.start <= start and span.end >= end]
    assert styles, f"no style span covers {substr!r} in {text.plain!r}"
    return styles[-1]


def test_bar_glyphs_full_at_100_percent() -> None:
    fill, track = _bar_glyphs(100, 100, 20, ascii_bars=False)
    assert fill == "█" * 20
    assert track == ""


def test_bar_glyphs_empty_at_0_percent() -> None:
    fill, track = _bar_glyphs(0, 100, 20, ascii_bars=False)
    assert fill == ""
    assert track == "░" * 20


def test_bar_glyphs_eighth_block_rounding() -> None:
    # 14.3/20 cells = 114.4 eighths, rounds to 114 = 14 full cells + 2/8 (▎).
    fill, track = _bar_glyphs(143, 200, 20, ascii_bars=False)
    assert fill == "█" * 14 + "▎"
    assert track == "░" * 5
    assert len(fill) - 1 + len(track) == 19  # 14 full + 1 eighth + 5 track = 20 cells total


def test_bar_glyphs_clamps_used_above_total() -> None:
    # Defensive: used > total (shouldn't happen, but never over-fill).
    fill, track = _bar_glyphs(150, 100, 10, ascii_bars=False)
    assert fill == "█" * 10
    assert track == ""


def test_bar_glyphs_zero_total_is_empty() -> None:
    fill, track = _bar_glyphs(0, 0, 10, ascii_bars=False)
    assert fill == ""
    assert track == "░" * 10


def test_bar_glyphs_zero_width_is_blank() -> None:
    assert _bar_glyphs(50, 100, 0, ascii_bars=False) == ("", "")


def test_bar_glyphs_ascii_fallback_has_no_eighths() -> None:
    fill, track = _bar_glyphs(143, 200, 20, ascii_bars=True)
    assert set(fill) <= {"#"}
    assert set(track) <= {"."}
    assert len(fill) + len(track) == 20
    assert "▎" not in fill and "░" not in track


def test_bar_glyphs_rounds_rather_than_truncates() -> None:
    # 15/20 of 10 cells = 7.5 cells exactly: rounding gives 8, truncation 7.
    fill, _track = _bar_glyphs(15, 20, 10, ascii_bars=True)
    assert len(fill) == 8


def test_format_delta_since() -> None:
    assert format_delta_since(_BASELINE, _NOW) == "Δ since 15:58 (12m)"


# --- colours come from the active theme --------------------------------------


def _theme_colors(name: str) -> ThemeColors:
    theme = BUILTIN_THEMES[name]
    assert theme.success and theme.warning and theme.error and theme.primary
    return ThemeColors(
        success=theme.success, warning=theme.warning, error=theme.error, primary=theme.primary
    )


def test_pressure_and_swap_colours_equal_the_theme_colours() -> None:
    colors = _theme_colors("dracula")
    busy = _stats(
        pressure_some_avg10=25.0,
        pressure_some_avg60=0.0,
        pressure_full_avg10=0.0,
        swap_total=1000,
        swap_free=90,
    )
    _ram, swap, pressure = _render(busy, 200, 40, colors=colors)

    assert colors.error in _style_at(pressure, "high")
    pair = format_pair(910, 1000)
    assert colors.error in _style_at(swap, pair)


def test_bar_uses_the_theme_primary_colour() -> None:
    colors = _theme_colors("nord")
    ram, _swap, _pressure = _render(_DEFAULT_STATS, 200, 40, colors=colors)
    assert colors.primary in _style_at(ram, "█")


def test_ansi_theme_colours_are_accepted() -> None:
    # ansi-dark/ansi-light use Textual's own "ansi_*" names, already
    # normalised to Rich colour strings by the caller before they ever reach
    # this module (`ui/screens/main.py` `_rich_color`) -- this just checks
    # the header doesn't choke on that shape of string.
    theme = BUILTIN_THEMES["ansi-dark"]
    colors = ThemeColors(
        success="ansi_green", warning="ansi_yellow", error="ansi_red", primary=str(theme.primary)
    )
    lines = _render(_stats(swap_total=1000, swap_free=90), 200, 40, colors=colors)
    assert all(line.plain for line in lines)


@pytest.mark.parametrize("width", [40, 60, 80, 120, 160])
@pytest.mark.parametrize("height", [16, 24, 30, 40])
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_grid_preserves_cell_width_and_never_wraps(
    width: int, height: int, ascii_bars: bool
) -> None:
    lines = _render(_ZSWAP_STATS, width, height, ascii_bars=ascii_bars)
    assert all(line.cell_len <= width and line.no_wrap for line in lines)
    assert len(lines) == (2 if height < 18 else 4)
    if ascii_bars:
        assert all(ord(c) < 128 for line in lines for c in line.plain)
    if height < 18:
        assert all(line.plain.endswith(">" if ascii_bars else "…") for line in lines)


@pytest.mark.parametrize("width", [60, 80, 120, 160])
def test_primary_values_and_metadata_share_fixed_columns(width: int) -> None:
    lines = _render(replace(_ZSWAP_STATS, zswap_max_pool_percent=20), width)
    value_starts = [
        line.plain.index(token)
        for line, token in zip(lines[:3], ("22.1/", "1.0/", "23.3/"), strict=True)
    ]
    assert len(set(value_starts)) == 1
    separators = [line.plain.index("│") for line in lines if "│" in line.plain]
    assert len(set(separators)) <= 1
    changed = _render(replace(_ZSWAP_STATS, mem_available=29 * _GIB, mem_shared=12 * _GIB), width)
    for before, after in zip(lines, changed, strict=True):
        if "│" in before.plain:
            assert before.plain.index("│") == after.plain.index("│")


def test_shared_is_metadata_and_hidden_marker_only_means_omission() -> None:
    ram = _render(_DEFAULT_STATS, 160)[0].plain
    assert ram.index("shared") > ram.index("│")
    assert not ram.endswith("…")
    assert _render(_DEFAULT_STATS, 80)[0].plain.endswith("…")


def test_unknown_pressure_and_disabled_swap_are_not_zero_usage() -> None:
    lines = _render(_stats(pressure_some_avg10=None, swap_total=0, swap_free=0), 120)
    assert "off" in lines[1].plain and "0.0/" not in lines[1].plain
    assert "unavailable" in lines[2].plain


def test_system_and_baseline_positions_do_not_depend_on_values() -> None:
    first = _render(_DEFAULT_STATS, 160)[2].plain
    second = _render(_stats(system_ram=32 * _GIB, pressure_some_avg60=1.0), 160)[2].plain
    for token in ("system", "Δ since", "elsewhere"):
        assert first.index(token) == second.index(token)
