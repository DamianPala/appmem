"""Tests for `appmem.ui.header` (SPEC.md "Main view" header lines)."""

from dataclasses import replace
from datetime import datetime
from typing import Any

from rich.text import Text

from appmem.collect import SystemStats
from appmem.fmt import format_pair
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

_WIDE = 200  # wide enough that nothing droppable ever needs to go


def _stats(**overrides: Any) -> SystemStats:
    return replace(_DEFAULT_STATS, **overrides)


def _line(width: int = _WIDE, **overrides: Any) -> str:
    return format_line1(_stats(**overrides), width).plain


def _style_at(text: Text, substr: str) -> str:
    start = text.plain.index(substr)
    end = start + len(substr)
    styles = [str(span.style) for span in text.spans if span.start <= start and span.end >= end]
    assert styles, f"no style span covers {substr!r} in {text.plain!r}"
    return styles[-1]


def test_line1_includes_ram_avail_swap_and_system_total() -> None:
    line = _line(system_ram=500 * 1024**2, system_swap=0)

    assert "RAM" in line
    assert "avail" in line
    assert "Swap" in line
    assert "system 500 MiB [x]" in line


def test_line1_swap_off_when_swap_total_zero() -> None:
    line = _line(swap_total=0, swap_free=0)

    assert "Swap off" in line
    assert "Swap 0" not in line


def test_line1_omits_pressure_when_none() -> None:
    line = _line(pressure_some_avg10=None, pressure_some_avg60=None, pressure_full_avg10=None)

    assert "pressure" not in line


def test_line1_shows_pressure_word_with_window_label() -> None:
    line = _line(pressure_some_avg10=0.5, pressure_some_avg60=0.0, pressure_full_avg10=0.0)

    assert "pressure 10s: none" in line


def test_line1_system_total_is_ram_plus_swap_with_no_process_count() -> None:
    # SystemStats.system_ram/system_swap carry no process count (the collector
    # skips the recursive count for system.slice); this must never render as if
    # it had one.
    line = _line(system_ram=300 * 1024**2, system_swap=200 * 1024**2)

    assert "system 500 MiB [x]" in line


def test_line1_shares_unit_between_used_and_total_when_equal() -> None:
    # SPEC.md "Main-view polish": `RAM 18.7/30.9 GiB`, not `RAM 18.7 GiB / 30.9 GiB`.
    line = _line(mem_total=30_900_000_000, mem_available=12_100_000_000)

    assert "RAM 17.5/28.8 GiB" in line


def test_line1_shows_elsewhere_after_system_when_above_threshold() -> None:
    line = _line(elsewhere=2 * 1024**2)

    assert line.endswith("elsewhere 2 MiB")


def test_line1_omits_elsewhere_below_1mib() -> None:
    line = _line(elsewhere=1024**2 - 1)

    assert "elsewhere" not in line


def test_line1_omits_elsewhere_when_none() -> None:
    line = _line(elsewhere=None)

    assert "elsewhere" not in line


def test_line2_format() -> None:
    baseline = datetime(2026, 1, 1, 14, 2, 0)
    now = datetime(2026, 1, 1, 14, 7, 0)

    line = format_line2(baseline, now)

    assert line == "Δ since 14:02 (5m)"


# --- narrow terminals drop parts by priority -------------------------------------


def test_line1_drops_elsewhere_first_when_narrow() -> None:
    full = _stats(elsewhere=2 * 1024**2, system_ram=500 * 1024**2)
    wide_line = format_line1(full, _WIDE).plain
    width = len(wide_line) - 1  # one column too narrow for everything

    narrow = format_line1(full, width).plain

    assert "elsewhere" not in narrow
    assert "system" in narrow  # not dropped yet


def test_line1_drops_system_before_avail() -> None:
    full = _stats(elsewhere=2 * 1024**2, system_ram=500 * 1024**2)
    wide_line = format_line1(full, _WIDE).plain
    elsewhere_segment = "   elsewhere 2 MiB"
    assert wide_line.endswith(elsewhere_segment)
    without_elsewhere = wide_line[: -len(elsewhere_segment)]
    assert "system" in without_elsewhere
    width = len(without_elsewhere) - 1  # one column short even after that drop

    narrow = format_line1(full, width).plain

    assert "elsewhere" not in narrow
    assert "system" not in narrow
    assert "avail" in narrow


def test_line1_never_drops_ram_swap_or_pressure() -> None:
    full = _stats(
        elsewhere=2 * 1024**2,
        system_ram=500 * 1024**2,
        pressure_some_avg10=0.5,
        pressure_some_avg60=0.0,
        pressure_full_avg10=0.0,
    )

    narrow = format_line1(full, 1).plain  # absurdly narrow

    assert "RAM" in narrow
    assert "Swap" in narrow
    assert "pressure" in narrow


def test_line1_never_wraps() -> None:
    text = format_line1(_stats(elsewhere=2 * 1024**2), 1)

    assert text.no_wrap is True


# --- pressure word and swap fraction are bold/coloured --------------------------


def test_line1_pressure_word_none_is_green() -> None:
    text = format_line1(
        _stats(pressure_some_avg10=0.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0), _WIDE
    )
    assert "green" in _style_at(text, "none")
    assert "bold" in _style_at(text, "none")


def test_line1_pressure_word_some_is_yellow() -> None:
    text = format_line1(
        _stats(pressure_some_avg10=3.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0), _WIDE
    )
    assert "yellow" in _style_at(text, "some")


def test_line1_pressure_word_high_is_red() -> None:
    text = format_line1(
        _stats(pressure_some_avg10=25.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0), _WIDE
    )
    assert "red" in _style_at(text, "high")


def test_line1_swap_fraction_uncoloured_under_50_percent() -> None:
    text = format_line1(_stats(swap_total=1000, swap_free=600), _WIDE)  # 40 % used
    assert text.plain.count("Swap") == 1
    # No span covering the pair carries a colour.
    pair_start = text.plain.index("Swap ") + len("Swap ")
    styles = [s.style for s in text.spans if s.start <= pair_start < s.end]
    assert not any("yellow" in str(s) or "red" in str(s) for s in styles)


def test_line1_swap_fraction_yellow_above_50_percent() -> None:
    text = format_line1(_stats(swap_total=1000, swap_free=400), _WIDE)  # 60 % used
    pair = format_pair(600, 1000)
    assert "yellow" in _style_at(text, pair)


def test_line1_swap_fraction_red_above_80_percent() -> None:
    text = format_line1(_stats(swap_total=1000, swap_free=100), _WIDE)  # 90 % used
    pair = format_pair(900, 1000)
    assert "red" in _style_at(text, pair)
