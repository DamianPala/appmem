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
    mem_free=2_500_000_000,
    mem_shared=3_000_000_000,
    mem_cache=6_100_000_000,
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


def test_line1_avail_breakdown_includes_slab_from_sreclaimable() -> None:
    # SReclaimable joins free/cache inside avail's breakdown, in that
    # order, still inside the parens.
    line = _line(
        mem_free=int(2.1 * 1024**3), mem_cache=int(5.1 * 1024**3), mem_slab=int(2.9 * 1024**3)
    )

    assert "(2.1 GiB free, 5.1 GiB cache, 2.9 GiB slab)" in line


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
    # SPEC.md "Main view": `RAM 18.7/30.9 GiB`, not `RAM 18.7 GiB / 30.9 GiB`.
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


def test_line1_narrow_drop_order() -> None:
    # Drop order (SPEC.md "Main view"):
    # elsewhere, system, avail's own breakdown, avail itself, then RAM's
    # shared last -- `shared` is the stickiest droppable part, so it
    # survives even once `avail` is gone entirely. RAM, Swap and pressure
    # never drop.
    full = _stats(elsewhere=2 * 1024**2, system_ram=500 * 1024**2)
    line = format_line1(full, _WIDE).plain
    assert "free" in line and "cache" in line  # avail breakdown present
    assert "shared" in line
    assert "elsewhere" in line
    assert "system" in line
    assert "avail" in line

    line = format_line1(full, len(line) - 1).plain  # one column short
    assert "elsewhere" not in line  # gone first
    assert "shared" in line
    assert "system" in line
    assert "avail" in line

    line = format_line1(full, len(line) - 1).plain
    assert "system" not in line  # gone second
    assert "shared" in line
    assert "avail" in line
    assert "free" in line and "cache" in line

    line = format_line1(full, len(line) - 1).plain
    assert "free" not in line and "cache" not in line  # avail breakdown: gone third
    assert "shared" in line
    assert "avail" in line

    line = format_line1(full, len(line) - 1).plain
    assert "avail" not in line  # gone fourth, whole figure this time
    assert "shared" in line  # not dropped yet

    line = format_line1(full, len(line) - 1).plain
    assert "shared" not in line  # gone last of the droppable parts
    assert "RAM" in line
    assert "Swap" in line


def test_line1_at_80_and_60_columns_never_wraps_and_keeps_ram_swap_pressure() -> None:
    full = _stats(
        elsewhere=2 * 1024**2,
        system_ram=500 * 1024**2,
        pressure_some_avg10=0.5,
        pressure_some_avg60=0.0,
        pressure_full_avg10=0.0,
    )
    for width in (80, 60):
        text = format_line1(full, width)
        assert text.no_wrap is True
        assert text.cell_len <= width  # fits by dropping parts, not by cropping
        assert "RAM" in text.plain
        assert "Swap" in text.plain
        assert "pressure" in text.plain


def test_line1_shared_survives_typical_widths_with_realistic_values() -> None:
    # With realistic sizes (a 31 GiB machine, 3 GiB shared), a 140-column
    # terminal shows both breakdowns (avail's free/cache/slab is the longest
    # droppable part, so it needs more room than 120 once slab joined it),
    # and shared must still survive down to an 80-column terminal even
    # though the bare `avail` figure is gone there.
    realistic = _stats(
        mem_total=int(30.9 * 1024**3),
        mem_available=int(10.8 * 1024**3),
        mem_free=int(2.5 * 1024**3),
        mem_cache=int(6.1 * 1024**3),
        mem_slab=int(1.2 * 1024**3),
        mem_shared=int(3.0 * 1024**3),
        swap_total=32 * 1024**3,
        swap_free=4 * 1024**3,
        system_ram=300 * 1024**2,
        system_swap=86 * 1024**2,
        elsewhere=213200896,
        pressure_some_avg10=0.0,
        pressure_some_avg60=0.0,
        pressure_full_avg10=0.0,
    )

    at_140 = format_line1(realistic, 140)
    assert at_140.cell_len <= 140
    assert "shared" in at_140.plain
    assert "free" in at_140.plain and "cache" in at_140.plain and "slab" in at_140.plain

    at_80 = format_line1(realistic, 80)
    assert at_80.cell_len <= 80
    assert "shared" in at_80.plain


def test_line1_shared_always_shown_even_when_zero() -> None:
    # No threshold, unlike `elsewhere` (SPEC.md "Main view").
    line = _line(mem_shared=0)

    assert "shared" in line


def test_line1_plain_text_conveys_everything_without_relying_on_colour() -> None:
    # NO_COLOR strips styles, not text: every distinction the header makes
    # (pressure level, swap fraction) must already be readable as plain words.
    text = format_line1(
        _stats(
            pressure_some_avg10=25.0,
            pressure_some_avg60=0.0,
            pressure_full_avg10=0.0,
            swap_total=1000,
            swap_free=100,
        ),
        _WIDE,
    )
    plain = text.plain

    assert "high" in plain  # pressure word, not colour, carries the level
    assert "RAM" in plain and "shared" in plain
    assert "avail" in plain and "free" in plain and "cache" in plain


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
