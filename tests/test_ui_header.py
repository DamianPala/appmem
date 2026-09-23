"""Tests for `appmem.ui.header` (SPEC.md "Main view" header). Structural and
behavioural checks throughout rather than pixel-exact string matches against
illustrative mock-ups: the numbered layout/slot/drop-order rules are
normative, ASCII art never is.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from rich.text import Text
from textual.theme import BUILTIN_THEMES

from appmem.collect import SystemStats
from appmem.fmt import format_pair, format_zswap_part, size_in_unit, unit_of
from appmem.ui.header import (
    _COMPACT_STEPS,  # pyright: ignore[reportPrivateUsage]
    _MIN_WIDTH_FOR_TEXT_TWO_LINE,  # pyright: ignore[reportPrivateUsage]
    _PRESSURE_STEPS,  # pyright: ignore[reportPrivateUsage]
    _RAM_STEPS,  # pyright: ignore[reportPrivateUsage]
    _SWAP_STEPS,  # pyright: ignore[reportPrivateUsage]
    _TWO_LINE_SWAP_STEPS,  # pyright: ignore[reportPrivateUsage]
    ThemeColors,
    _bar_glyphs,  # pyright: ignore[reportPrivateUsage]
    _bar_width,  # pyright: ignore[reportPrivateUsage]
    _compact_status_line,  # pyright: ignore[reportPrivateUsage]
    _pressure_line,  # pyright: ignore[reportPrivateUsage]
    _ram_line,  # pyright: ignore[reportPrivateUsage]
    _RenderCtx,  # pyright: ignore[reportPrivateUsage]
    _swap_line,  # pyright: ignore[reportPrivateUsage]
    _two_line_swap,  # pyright: ignore[reportPrivateUsage]
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


_CTX = _RenderCtx(colors=_COLORS, bar_width=20, ascii_bars=False, writeback_rate=None)
_CTX_WB = replace(_CTX, writeback_rate=12 * _MIB)


# --- bar maths (pure glyph function) -----------------------------------------


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


def test_bar_width_table_boundaries() -> None:
    assert _bar_width(110) == 20
    assert _bar_width(109) == 16
    assert _bar_width(90) == 16
    assert _bar_width(89) == 10
    assert _bar_width(70) == 10
    assert _bar_width(69) == 6
    assert _bar_width(60) == 6
    assert _bar_width(59) == 0


def test_bar_track_is_dim_styled() -> None:
    ram, _swap, _pressure = _render(_stats(mem_available=int(20.0 * _GIB)), 200, 40)
    assert "dim" in _style_at(ram, "░")


# --- structural: every line fits, RAM/Swap/Pressure always present ----------


_SIZES = [(120, 40), (100, 30), (80, 24), (120, 15), (60, 20), (40, 10)]


def test_lines_never_exceed_their_width_across_sizes_and_flags() -> None:
    for width, height in _SIZES:
        for stats in (_DEFAULT_STATS, _ZSWAP_STATS, _stats(swap_total=0, swap_free=0)):
            for rate in (None, 12 * _MIB):
                lines = _render(stats, width, height, writeback_rate=rate)
                assert 2 <= len(lines) <= 3
                for line in lines:
                    assert line.cell_len <= width, (width, height, line.plain)


def test_three_lines_at_height_18_two_below_it() -> None:
    assert len(_render(_DEFAULT_STATS, 120, 18)) == 3
    assert len(_render(_DEFAULT_STATS, 120, 17)) == 2


def test_never_fewer_than_two_lines_even_at_extreme_sizes() -> None:
    assert len(_render(_DEFAULT_STATS, 1, 1)) == 2


def test_three_line_mode_shows_ram_swap_pressure_labels() -> None:
    ram, swap, pressure = _render(_DEFAULT_STATS, 120, 40)
    assert ram.plain.startswith("RAM")
    assert swap.plain.startswith("Swap")
    assert pressure.plain.startswith("Pressure")


def test_two_line_v3_form_keeps_ram_and_swap_and_the_pressure_word() -> None:
    line1, line2 = _render(_DEFAULT_STATS, 120, 15)
    assert line1.plain.startswith("RAM")
    assert "Pressure" in line1.plain
    assert line2.plain.startswith("Swap")


def test_compact_form_keeps_bare_ram_swap_and_pressure() -> None:
    line1, line2 = _render(_DEFAULT_STATS, 40, 10)
    assert "RAM" in line1.plain and "Swap" in line1.plain
    assert line2.plain.startswith("Pressure")


# --- flags decide content, not values ----------------------------------------


def test_zswap_bracket_shown_only_when_enabled() -> None:
    _ram, swap, _pressure = _render(_ZSWAP_STATS, 200, 40)
    assert "zswapped" in swap.plain

    _ram2, swap_off, _p2 = _render(_DEFAULT_STATS, 200, 40)
    assert "zswapped" not in swap_off.plain


def test_zswap_long_form_wording() -> None:
    _ram, swap, _pressure = _render(_ZSWAP_STATS, 200, 40)
    assert _ZSWAP_STATS.zswapped_bytes is not None
    assert _ZSWAP_STATS.zswap_pool_bytes is not None
    unit = unit_of(_ZSWAP_STATS.swap_total)
    expected = format_zswap_part(_ZSWAP_STATS.zswapped_bytes, _ZSWAP_STATS.zswap_pool_bytes, unit)
    assert expected in swap.plain
    assert "into 1.0 GiB RAM" in swap.plain


def test_swap_off_has_no_bar_and_no_pair() -> None:
    _ram, swap, _pressure = _render(_stats(swap_total=0, swap_free=0), 200, 40)
    assert swap.plain == "Swap      off"


def test_to_disk_token_shown_while_writeback_rate_positive() -> None:
    _ram, swap, _pressure = _render(_ZSWAP_STATS, 200, 40, writeback_rate=12 * _MIB)
    assert "to disk 12 MiB/s" in swap.plain


def test_to_disk_token_hidden_when_rate_is_zero_or_none() -> None:
    for rate in (None, 0):
        _ram, swap, _pressure = _render(_ZSWAP_STATS, 200, 40, writeback_rate=rate)
        assert "to disk" not in swap.plain


def test_to_disk_styled_with_the_theme_warning_colour() -> None:
    _ram, swap, _pressure = _render(_ZSWAP_STATS, 200, 40, writeback_rate=12 * _MIB)
    assert "yellow" in _style_at(swap, "to disk 12 MiB/s")


def test_elsewhere_shown_above_threshold_hidden_below_and_when_none() -> None:
    _r, _s, at_threshold = _render(_stats(elsewhere=2 * _MIB), 200, 40)
    assert "elsewhere 2 MiB" in at_threshold.plain

    _r2, _s2, below = _render(_stats(elsewhere=_MIB - 1), 200, 40)
    assert "elsewhere" not in below.plain

    _r3, _s3, none_ = _render(_stats(elsewhere=None), 200, 40)
    assert "elsewhere" not in none_.plain


def test_pressure_word_reflects_state_not_a_fixed_label() -> None:
    _r, _s, high = _render(
        _stats(pressure_some_avg10=25.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0), 200, 40
    )
    assert "high" in high.plain

    no_psi = _stats(pressure_some_avg10=None, pressure_some_avg60=None, pressure_full_avg10=None)
    _r2, _s2, unavailable = _render(no_psi, 200, 40)
    assert "unavailable" in unavailable.plain


# --- swap colour: error only above 90 %, no 50 % warning ---------------------


def test_swap_pair_uncoloured_at_85_percent() -> None:
    stats = _stats(swap_total=1000, swap_free=150)  # 85 % used
    _ram, swap, _pressure = _render(stats, 200, 40)
    pair = format_pair(850, 1000)
    start = swap.plain.index(pair)
    end = start + len(pair)
    # No span with a colour covers the pair (an uncoloured pair may carry no
    # span at all, unlike the coloured case `_style_at` covers).
    styles = [str(s.style) for s in swap.spans if s.start <= start and s.end >= end]
    assert not any("red" in style or "yellow" in style for style in styles)


def test_swap_pair_error_coloured_above_90_percent() -> None:
    stats = _stats(swap_total=1000, swap_free=90)  # 91 % used
    _ram, swap, _pressure = _render(stats, 200, 40)
    pair = format_pair(910, 1000)
    assert "red" in _style_at(swap, pair)


# --- no jitter: differing values keep every part at the same column ---------
#
# Each test below renders the same size and flags twice, with only one value
# changed, and checks that every part is at the same column (or, where a
# value's own size decides whether a *flag* is even met, that the same flag
# decision is reached both times) -- rather than hand-computing the exact
# widths involved, which the bar-width table's big steps and the >= 95
# column Δ cutoff make easy to get wrong by construction (see the drop-order
# section below).


def test_ram_pair_digit_boundary_does_not_move_what_follows() -> None:
    # 9.9 -> 10.0 GiB used, both against a fixed 99.9 GiB total: the pair
    # slot is sized off the total (always 4 digits here), so the "used"
    # keyword and the shared bracket after it must land in the same column
    # either side of the boundary, even though `avail` (total - used) is
    # itself a genuinely different, non-jittering number the other side of
    # it (90.0 -> 89.9, also 4 digits, so it doesn't move anything either).
    low = _stats(mem_total=int(99.9 * _GIB), mem_available=int(99.9 * _GIB) - int(9.9 * _GIB))
    high = _stats(mem_total=int(99.9 * _GIB), mem_available=int(99.9 * _GIB) - int(10.0 * _GIB))
    ram_low, _s1, _p1 = _render(low, 200, 40)
    ram_high, _s2, _p2 = _render(high, 200, 40)

    assert ram_low.cell_len == ram_high.cell_len
    assert ram_low.plain.index(" used") == ram_high.plain.index(" used")
    assert ram_low.plain.index("shared") == ram_high.plain.index("shared")
    assert ram_low.plain.index("avail") == ram_high.plain.index("avail")
    assert ram_low.plain.index("free") == ram_high.plain.index("free")


def test_avail_shown_in_the_totals_unit_not_its_own() -> None:
    # mem_available alone (536870912 B) would pick MiB on its own
    # (`unit_of` -> "512 MiB"); forced into the RAM total's GiB it must read
    # "0.5 GiB" instead (right-padded to the total's own digit width, "30.9"
    # -> 4 cells), so its slot never depends on which unit it would
    # otherwise have picked.
    stats = _stats(mem_available=int(0.5 * _GIB))
    ram = _render(stats, 200, 40)[0]
    assert "avail  0.5 GiB" in ram.plain


def test_ram_line_length_stable_across_avail_values_when_breakdown_shown() -> None:
    # If avail's width followed its own value, RAM used 22.1 -> 10.0 GiB
    # would drop avail between 58 and 70 columns; its slot comes from
    # `mem_total` instead.
    low = _render(_stats(mem_available=int(0.8 * _GIB)), 200, 40)[0]
    high = _render(_stats(mem_available=int(12.0 * _GIB)), 200, 40)[0]
    assert low.cell_len == high.cell_len


def test_ram_avail_presence_does_not_depend_on_used_value() -> None:
    for width in (60, 64, 68):
        low_used = _render(_stats(mem_available=int(20.9 * _GIB)), width, 40)[0]
        high_used = _render(_stats(mem_available=int(8.8 * _GIB)), width, 40)[0]
        assert ("avail" in low_used.plain) == ("avail" in high_used.plain)


def test_ram_shared_jitter_does_not_move_avail() -> None:
    small = _render(_stats(mem_shared=int(0.1 * _GIB)), 200, 40)[0]
    big = _render(_stats(mem_shared=int(12.3 * _GIB)), 200, 40)[0]
    assert small.plain.index("avail") == big.plain.index("avail")


def test_zswap_pool_jitter_does_not_move_to_disk() -> None:
    small_pool = replace(_ZSWAP_STATS, zswap_pool_bytes=9 * _MIB)
    big_pool = replace(_ZSWAP_STATS, zswap_pool_bytes=int(1.2 * _GIB))  # crosses MiB -> GiB
    swap_small = _render(small_pool, 200, 40, writeback_rate=12 * _MIB)[1]
    swap_big = _render(big_pool, 200, 40, writeback_rate=12 * _MIB)[1]
    assert swap_small.cell_len == swap_big.cell_len
    assert swap_small.plain.index("to disk") == swap_big.plain.index("to disk")


def test_zswapped_jitter_does_not_move_to_disk() -> None:
    small = replace(_ZSWAP_STATS, zswapped_bytes=int(0.1 * _GIB))
    big = replace(_ZSWAP_STATS, zswapped_bytes=int(20.0 * _GIB))
    swap_small = _render(small, 200, 40, writeback_rate=12 * _MIB)[1]
    swap_big = _render(big, 200, 40, writeback_rate=12 * _MIB)[1]
    assert swap_small.plain.index("to disk") == swap_big.plain.index("to disk")


def test_writeback_rate_jitter_does_not_flip_zswap_bracket_form() -> None:
    # At ~98 columns an unpadded rate would pick the long zswap form for
    # `12 MiB/s` and the short one for `150 MiB/s` or `900 KiB/s`: the
    # long/short choice must depend only on the width.
    forms = {
        "into" in _render(_ZSWAP_STATS, 98, 40, writeback_rate=rate)[1].plain
        for rate in (
            5 * 1024,
            900 * 1024,
            12 * _MIB,
            150 * _MIB,
        )
    }
    assert len(forms) == 1


def test_elsewhere_presence_does_not_depend_on_its_own_size() -> None:
    # Unpadded, `elsewhere` would appear or disappear between 77 and 104
    # columns depending on `2 MiB` vs `1000 MiB`'s own text width.
    for width in (90, 96, 104):
        small = _render(_stats(elsewhere=2 * _MIB), width, 40)[2]
        big = _render(_stats(elsewhere=1000 * _MIB), width, 40)[2]
        assert ("elsewhere" in small.plain) == ("elsewhere" in big.plain)

    small = _render(_stats(elsewhere=2 * _MIB), 200, 40)[2]
    big = _render(_stats(elsewhere=1000 * _MIB), 200, 40)[2]
    assert small.plain.index("elsewhere") == big.plain.index("elsewhere")
    assert small.cell_len == big.cell_len


def test_pressure_system_value_jitter_does_not_move_delta() -> None:
    small = _render(_stats(system_ram=5 * _MIB, system_swap=0), 200, 40)[2]
    big = _render(_stats(system_ram=int(12.5 * _GIB), system_swap=0), 200, 40)[2]
    assert small.plain.index("Δ") == big.plain.index("Δ")


def test_pressure_delta_elapsed_jitter_does_not_move_elsewhere() -> None:
    stats = _stats(elsewhere=2 * _MIB)
    now_short = _BASELINE + timedelta(seconds=5)  # "5s"
    now_long = _BASELINE + timedelta(hours=3, minutes=45)  # "3h45m"
    short = render_header(stats, 200, 40, colors=_COLORS, baseline_time=_BASELINE, now=now_short)[2]
    long_ = render_header(stats, 200, 40, colors=_COLORS, baseline_time=_BASELINE, now=now_long)[2]
    assert short.plain.index("elsewhere") == long_.plain.index("elsewhere")


def test_multi_day_baseline_does_not_move_elsewhere() -> None:
    stats = _stats(elsewhere=2 * _MIB)
    columns: set[tuple[int, int]] = set()
    for elapsed in (
        timedelta(seconds=5),
        timedelta(days=4, hours=4, minutes=15),  # 100h15m unbounded: 7 cells
        timedelta(days=120, hours=5, minutes=17),
    ):
        line = render_header(
            stats, 200, 40, colors=_COLORS, baseline_time=_BASELINE, now=_BASELINE + elapsed
        )[2]
        columns.add((line.plain.index("elsewhere"), line.cell_len))
    assert len(columns) == 1


def test_pressure_qualifier_growing_does_not_move_what_follows() -> None:
    bare = _stats(pressure_some_avg10=0.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0)
    qualified = _stats(pressure_some_avg10=0.0, pressure_some_avg60=1.2, pressure_full_avg10=0.0)
    _r1, _s1, p_bare = _render(bare, 200, 40)
    _r2, _s2, p_qualified = _render(qualified, 200, 40)

    assert p_bare.plain.index("system") == p_qualified.plain.index("system")


# --- two-line form: never cropped, left-aligned, only from 70 cols up -------
#
# The floor moved from 80 to 70 when the pressure qualifier shrank (SPEC.md
# "Main view"): the two-line form's own worst case -- label 10 + bar 10 + 2
# + a typical pair 13 + " used" 5 + 3 + the pressure block (26, was 36) --
# now comes to 69, one below the new floor.


def test_two_line_form_used_only_from_70_columns_up() -> None:
    assert _MIN_WIDTH_FOR_TEXT_TWO_LINE == 70
    compact = _render(_DEFAULT_STATS, 69, 15)
    two_line = _render(_DEFAULT_STATS, 70, 15)
    assert compact[1].plain.startswith("Pressure")  # compact form's line 2
    assert two_line[1].plain.startswith("Swap")  # two-line form's line 2


def test_two_line_pressure_block_is_left_aligned() -> None:
    bare = _stats(pressure_some_avg10=0.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0)
    qualified = _stats(pressure_some_avg10=0.0, pressure_some_avg60=1.2, pressure_full_avg10=0.0)
    line1_bare = _render(bare, 90, 15)[0]
    line1_qualified = _render(qualified, 90, 15)[0]

    assert line1_bare.plain.index("Pressure") == line1_qualified.plain.index("Pressure")
    assert "1.2" in line1_qualified.plain  # the qualifier actually renders, not cropped


def test_two_line_first_line_length_does_not_depend_on_the_pressure_word() -> None:
    bare = _stats(pressure_some_avg10=0.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0)
    worst = _stats(pressure_some_avg10=0.0, pressure_some_avg60=99.9, pressure_full_avg10=0.0)
    for width in (70, 90, 120):
        assert _render(bare, width, 15)[0].cell_len == _render(worst, width, 15)[0].cell_len


def test_two_line_pressure_block_unmoved_by_ram_avail_jitter() -> None:
    low_avail = _stats(mem_available=int(0.5 * _GIB))
    high_avail = _stats(mem_available=int(20.0 * _GIB))
    line1_low = _render(low_avail, 90, 15)[0]
    line1_high = _render(high_avail, 90, 15)[0]
    assert line1_low.plain.index("Pressure") == line1_high.plain.index("Pressure")


_LONGEST_PRESSURE_WORD = "none (was 99.9 %)"


def test_longest_pressure_word_never_cropped_at_required_sizes() -> None:
    stats = _stats(pressure_some_avg10=0.0, pressure_some_avg60=99.9, pressure_full_avg10=0.0)
    big_ram = replace(stats, mem_total=int(125.7 * _GIB), mem_available=int(60.0 * _GIB))
    sizes = [(120, 40), (100, 30), (80, 24), (90, 15), (80, 15), (70, 15), (40, 10)]
    for case, (width, height) in itertools.product((stats, big_ram), sizes):
        # `.plain` is never cropped; the widget crops what is wider than the terminal.
        holder = [ln for ln in _render(case, width, height) if _LONGEST_PRESSURE_WORD in ln.plain]
        assert holder, (width, height)
        assert holder[0].cell_len <= width, (width, height, holder[0].plain)


# --- one grid: `system` in the RAM/Swap pair's own column -------------------

_PRESSURE_LEVELS: dict[str, dict[str, float]] = {
    "none": {"pressure_some_avg10": 0.0, "pressure_some_avg60": 0.0, "pressure_full_avg10": 0.0},
    "none_qualified": {
        "pressure_some_avg10": 0.0,
        "pressure_some_avg60": 99.9,
        "pressure_full_avg10": 0.0,
    },
    "some": {"pressure_some_avg10": 3.2, "pressure_some_avg60": 0.0, "pressure_full_avg10": 1.0},
    "high": {"pressure_some_avg10": 25.0, "pressure_some_avg60": 0.0, "pressure_full_avg10": 0.0},
}


def _ram_pair_column(ram: Text, stats: SystemStats) -> int:
    used = stats.mem_total - stats.mem_available
    pair = format_pair(used, stats.mem_total)
    return ram.plain.index(pair)


def test_pressure_system_aligns_with_the_ram_swap_pair_column() -> None:
    # SPEC.md "Main view": one grid for the header's three lines -- `system`
    # starts in exactly the RAM/Swap pair's own column, for every pressure
    # level including the qualifier's own worst case, with or without
    # zswap, at both 120 and 100 columns (100 still fits: its bar column is
    # wide enough for the new, shorter qualifier).
    for width in (120, 100):
        for base in (_DEFAULT_STATS, _ZSWAP_STATS):
            for fields in _PRESSURE_LEVELS.values():
                stats = replace(base, **fields)
                ram, _swap, pressure = _render(stats, width, 40)
                assert pressure.plain.index("system") == _ram_pair_column(ram, stats), (
                    width,
                    fields,
                )


def test_pressure_system_column_is_width_only_where_the_grid_does_not_fit() -> None:
    # Below the bar column that can hold the qualifier's own worst case
    # (SPEC.md "Main view"), `system` falls back to a fixed, width-only
    # column -- the same for every pressure level, not the RAM/Swap pair's
    # own column (bar_width=10 here, too short for the grid to engage).
    columns: set[int] = set()
    for fields in _PRESSURE_LEVELS.values():
        stats = replace(_DEFAULT_STATS, **fields)
        ram, _swap, pressure = _render(stats, 80, 40)
        columns.add(pressure.plain.index("system"))
        assert pressure.plain.index("system") != _ram_pair_column(ram, stats)
        assert "   system" in pressure.plain  # the usual 3-space gap, even after the worst case
    assert len(columns) == 1


def test_compact_form_values_never_move_or_drop_parts() -> None:
    base = _stats(pressure_some_avg10=0.0, pressure_some_avg60=0.0, pressure_full_avg10=0.0)
    varied = _stats(
        mem_available=int(30.9 * _GIB) - int(9.9 * _GIB),
        pressure_some_avg10=0.0,
        pressure_some_avg60=84.9,
        pressure_full_avg10=0.0,
        system_ram=5 * _MIB,
    )
    for width in (40, 47, 69):
        lines_base = _render(base, width, 15, writeback_rate=12 * _MIB)
        lines_varied = _render(varied, width, 15, writeback_rate=900 * 1024)
        for one, other in zip(lines_base, lines_varied, strict=True):
            assert one.cell_len == other.cell_len, (width, one.plain, other.plain)
            for part in ("Swap", "to disk", "system"):
                assert one.plain.find(part) == other.plain.find(part), (width, part)


def test_two_line_avail_bracket_value_does_not_move_the_pressure_block() -> None:
    small = _stats(mem_free=int(0.1 * _GIB), mem_cache=int(0.2 * _GIB), mem_slab=int(0.1 * _GIB))
    big = _stats(mem_free=int(12.1 * _GIB), mem_cache=int(14.6 * _GIB), mem_slab=int(10.2 * _GIB))
    line1_small = _render(small, 250, 15)[0]
    line1_big = _render(big, 250, 15)[0]
    assert "slab" in line1_small.plain
    assert line1_small.plain.index("Pressure") == line1_big.plain.index("Pressure")


# --- drop order: each line's own step tuple, applied directly ---------------
#
# Widening the terminal isn't monotonic in the parts shown -- the bar-width
# table has big steps, so crossing e.g. 110 -> 109 can *shrink* the bar by 4
# cells and free more room than 1 column of width cost, and below 95 columns
# dropping Δ unconditionally can do the same for whatever is left on the
# Pressure line. Sweeping the width can't tell "drop order" apart from those
# two effects, so the drop order itself is checked directly against each
# line's declared step tuple instead.


def test_ram_steps_drop_breakdown_then_avail_then_shared() -> None:
    assert _RAM_STEPS == ("avail_breakdown", "avail", "shared")
    stats = _DEFAULT_STATS

    none = _ram_line(stats, _CTX, disabled=frozenset())
    assert "free" in none.plain and "avail" in none.plain and "shared" in none.plain

    breakdown_gone = _ram_line(stats, _CTX, disabled=frozenset({"avail_breakdown"}))
    assert "free" not in breakdown_gone.plain
    assert "avail" in breakdown_gone.plain and "shared" in breakdown_gone.plain

    avail_gone = _ram_line(stats, _CTX, disabled=frozenset({"avail_breakdown", "avail"}))
    assert "avail" not in avail_gone.plain
    assert "shared" in avail_gone.plain

    all_gone = _ram_line(stats, _CTX, disabled=frozenset({"avail_breakdown", "avail", "shared"}))
    assert "shared" not in all_gone.plain
    assert all_gone.plain.startswith("RAM")


def test_swap_steps_drop_long_zswap_then_short_then_gone() -> None:
    assert _SWAP_STEPS == ("zswap_long", "zswap")
    stats = _ZSWAP_STATS

    none = _swap_line(stats, _CTX, disabled=frozenset())
    assert "into" in none.plain and "zswapped" in none.plain

    long_gone = _swap_line(stats, _CTX, disabled=frozenset({"zswap_long"}))
    assert "into" not in long_gone.plain
    assert "zswapped" in long_gone.plain

    all_gone = _swap_line(stats, _CTX, disabled=frozenset({"zswap_long", "zswap"}))
    assert "zswapped" not in all_gone.plain
    assert all_gone.plain.startswith("Swap")


def test_pressure_steps_drop_elsewhere_then_delta_then_system() -> None:
    assert _PRESSURE_STEPS == ("elsewhere", "delta", "system")
    stats = _stats(elsewhere=2 * _MIB)

    none = _pressure_line(stats, _CTX, _BASELINE, _NOW, width=200, disabled=frozenset())
    assert "elsewhere" in none.plain and "Δ" in none.plain and "system" in none.plain

    elsewhere_gone = _pressure_line(
        stats, _CTX, _BASELINE, _NOW, width=200, disabled=frozenset({"elsewhere"})
    )
    assert "elsewhere" not in elsewhere_gone.plain
    assert "Δ" in elsewhere_gone.plain and "system" in elsewhere_gone.plain

    delta_gone = _pressure_line(
        stats, _CTX, _BASELINE, _NOW, width=200, disabled=frozenset({"elsewhere", "delta"})
    )
    assert "Δ" not in delta_gone.plain
    assert "system" in delta_gone.plain

    every_step = frozenset({"elsewhere", "delta", "system"})
    all_gone = _pressure_line(stats, _CTX, _BASELINE, _NOW, width=200, disabled=every_step)
    assert "system" not in all_gone.plain
    assert all_gone.plain.startswith("Pressure")


def test_two_line_swap_steps_drop_order() -> None:
    assert _TWO_LINE_SWAP_STEPS == ("zswap_long", "zswap", "delta", "system")
    stats = _ZSWAP_STATS

    none = _two_line_swap(stats, _CTX, _BASELINE, _NOW, width=200, disabled=frozenset())
    assert "into" in none.plain and "Δ" in none.plain and "system" in none.plain

    long_gone = _two_line_swap(
        stats, _CTX, _BASELINE, _NOW, width=200, disabled=frozenset({"zswap_long"})
    )
    assert "into" not in long_gone.plain and "zswapped" in long_gone.plain
    assert "Δ" in long_gone.plain and "system" in long_gone.plain

    zswap_gone = _two_line_swap(
        stats, _CTX, _BASELINE, _NOW, width=200, disabled=frozenset({"zswap_long", "zswap"})
    )
    assert "zswapped" not in zswap_gone.plain
    assert "Δ" in zswap_gone.plain and "system" in zswap_gone.plain

    delta_gone = _two_line_swap(
        stats,
        _CTX,
        _BASELINE,
        _NOW,
        width=200,
        disabled=frozenset({"zswap_long", "zswap", "delta"}),
    )
    assert "Δ" not in delta_gone.plain and "system" in delta_gone.plain

    all_gone = _two_line_swap(
        stats,
        _CTX,
        _BASELINE,
        _NOW,
        width=200,
        disabled=frozenset({"zswap_long", "zswap", "delta", "system"}),
    )
    assert "system" not in all_gone.plain
    assert all_gone.plain.startswith("Swap")


def test_compact_steps_drop_system_then_to_disk() -> None:
    assert _COMPACT_STEPS == ("system", "to_disk")
    stats = _stats()

    none = _compact_status_line(stats, _CTX_WB, disabled=frozenset())
    assert "to disk" in none.plain and "system" in none.plain

    system_gone = _compact_status_line(stats, _CTX_WB, disabled=frozenset({"system"}))
    assert "system" not in system_gone.plain
    assert "to disk" in system_gone.plain

    all_gone = _compact_status_line(stats, _CTX_WB, disabled=frozenset({"system", "to_disk"}))
    assert "to disk" not in all_gone.plain
    assert all_gone.plain.startswith("Pressure")


def test_delta_hidden_below_95_columns_even_when_it_would_otherwise_fit() -> None:
    stats = _stats()  # no elsewhere/system pressure competing for room
    wide = _render(stats, 94, 40)[2]
    narrow_ok = _render(stats, 95, 40)[2]
    assert "Δ" not in wide.plain
    assert "Δ" in narrow_ok.plain


def test_never_drops_ram_swap_or_pressure_label() -> None:
    stats = _stats(elsewhere=2 * _MIB)
    lines = _render(stats, 1, 40)
    assert lines[0].plain.startswith("RAM")
    assert lines[1].plain.startswith("Swap")
    assert lines[2].plain.startswith("Pressure")


def test_lines_are_no_wrap_with_ellipsis_overflow() -> None:
    for line in _render(_stats(elsewhere=2 * _MIB), 1, 40):
        assert line.no_wrap is True
        assert line.overflow == "ellipsis"


# --- format_delta_since -------------------------------------------------------


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


# --- size-only breakdown padding: numbers inherit the parent's unit ---------


def test_ram_shared_bracket_inherits_rams_unit_without_repeating_it() -> None:
    ram, _swap, _pressure = _render(_DEFAULT_STATS, 200, 40)
    unit = unit_of(_DEFAULT_STATS.mem_total)
    value = size_in_unit(_DEFAULT_STATS.mem_shared, unit)
    assert f"({value} shared)" in ram.plain
    assert f"{unit} shared" not in ram.plain  # the unit isn't repeated
