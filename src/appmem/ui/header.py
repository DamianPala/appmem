"""Main-view header: three lines, RAM/Swap/Pressure, each with a
used/avail (or used/free) gauge bar (SPEC.md "Main view").

Layout is decided from the terminal size and a handful of state flags only
(zswap on, writeback active, `elsewhere` >= 1 MiB, pressure readable, Swap
off) -- never from the current values, so nothing jitters tick to tick. Two
mechanisms make that true:

- Every figure that can change width (a used/total pair, a breakdown
  number, the zswap pool, `to disk`'s rate, `elsewhere`'s size) is padded to
  a fixed slot: the width its *total* would need in the worst case for a
  figure bounded by one, or a literal worst-case string for one that isn't
  (a rate has no natural total to bound it). A value crossing a digit
  boundary (`9.9` -> `10.0`) never shifts anything to its right.
- Which parts show is a plain "keep dropping the next-lowest-priority part
  until it fits" pass per line, using those same fixed slot widths for the
  fit check -- so the same combination of parts is chosen at a given width
  regardless of the actual numbers, and dropping never has to overshoot and
  refill.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from rich.text import Text

from appmem.collect import SystemStats
from appmem.fmt import (
    format_elapsed,
    format_pair,
    format_rate,
    format_zswap_part,
    pressure_word,
    size,
    size_in_unit,
    unit_of,
)

_ELSEWHERE_THRESHOLD = 1024 * 1024

# Label column (SPEC.md "Main view"): "RAM", "Swap" and "Pressure" each
# padded to this width before anything else starts.
_LABEL_WIDTH = 10

# Fixed-width slots for parts whose content never depends on machine size,
# so their literal worst-case text is known up front (SPEC.md "Main view"):
# `none (some 99.9 % last min)` (27), `system 999.9 GiB [x]`-ish (19), `Δ
# since HH:MM (elapsed)` with elapsed up to 6 cells (22), `to disk 1023
# MiB/s` (19), `elsewhere ` plus an 8-cell value (18).
_PRESSURE_SLOT = 27
_SYSTEM_SLOT = 19
_DELTA_SLOT = 22
_WRITEBACK_SLOT = len("to disk 1023 MiB/s")
_ELSEWHERE_VALUE_SLOT = 8  # "1023 MiB" / "99.9 GiB"-ish, same ceiling as `to disk`'s rate
_ELSEWHERE_SLOT = len("elsewhere ") + _ELSEWHERE_VALUE_SLOT
# A zswap pool is a RAM cost, so its worst case comes from RAM's own total,
# not Swap's -- at least this flat minimum for a small machine.
_ZSWAP_POOL_MIN_SLOT = 8  # "1023 MiB" / "99.9 GiB"-ish
# The H < 18, W >= 80 two-line form's pressure block, right of the RAM line.
_PRESSURE_BLOCK_SLOT = 36

# Same threshold as the ΔRAM/ΔSWAP table columns (`ui/screens/main.py`
# `_NARROW_WIDTH`): Δ is dropped unconditionally below this width, on top of
# whatever the per-line fit loop would have decided on its own.
_DELTA_HIDE_WIDTH = 95

# Bar cell count by terminal width (SPEC.md "Main view"), widest match
# first; below the narrowest entry there is no bar at all.
_BAR_WIDTHS: tuple[tuple[int, int], ...] = ((110, 20), (90, 16), (70, 10), (60, 6))

_EIGHTHS = "▏▎▍▌▋▊▉"  # 1/8 .. 7/8; a full cell is a plain "█"

# Height/width thresholds between the three header shapes (SPEC.md "Main
# view"): 3 lines, the two-line text form, or the compact two-liner. The
# two-line form's own worst case -- label 10 + bar 10 (its bucket from 70
# columns up) + 2 + a typical pair 13 + " used" 5 + 3 + the 36-cell pressure
# block -- comes to 79, so 80 is the narrowest width that can promise not to
# crop the pressure word.
_MIN_HEIGHT_FOR_THREE_LINES = 18
_MIN_WIDTH_FOR_TEXT_TWO_LINE = 80

# Per-line drop order, lowest priority first (SPEC.md "Main view"). RAM's
# `avail_breakdown` step removes only the `(free, cache, slab)` bracket,
# keeping bare `avail`; Swap's `zswap_long` step shortens the zswap bracket
# to `(X zswapped)` before `zswap` drops it entirely.
_RAM_STEPS: tuple[str, ...] = ("avail_breakdown", "avail", "shared")
_SWAP_STEPS: tuple[str, ...] = ("zswap_long", "zswap")
_PRESSURE_STEPS: tuple[str, ...] = ("elsewhere", "delta", "system")
_TWO_LINE_SWAP_STEPS: tuple[str, ...] = (*_SWAP_STEPS, "delta", "system")
# H < 18, W < 80 compact form (SPEC.md "Main view"): "Pressure …, then to
# disk …, then system, as they fit" -- system is the first to go.
_COMPACT_STEPS: tuple[str, ...] = ("system", "to_disk")


@dataclass(frozen=True)
class ThemeColors:
    """The theme colours the header borrows, read once per render from
    `App.current_theme` by the caller. Kept as plain hex/rich colour strings
    here rather than importing `textual.theme.Theme`, so this module stays
    pure and testable without a running app. `primary` is the bar fill
    colour (SPEC.md "Main view", "Colour"): normally the theme's own accent,
    but the caller substitutes the theme's foreground colour instead on a
    theme whose accent falls short of a readable contrast against the
    background -- never one of the severity colours."""

    success: str
    warning: str
    error: str
    primary: str


@dataclass(frozen=True)
class _RenderCtx:
    """The handful of render inputs every line builder needs, bundled so a
    line's own signature stays about its own parts (`disabled`, `width`)
    instead of repeating `colors`/`bar_width`/`ascii_bars`/`writeback_rate`
    everywhere."""

    colors: ThemeColors
    bar_width: int
    ascii_bars: bool
    writeback_rate: int | None


def _bar_width(width: int) -> int:
    for threshold, bar_width in _BAR_WIDTHS:
        if width >= threshold:
            return bar_width
    return 0


def _bar_glyphs(used: int, total: int, width: int, *, ascii_bars: bool) -> tuple[str, str]:
    """The bar's `(fill, track)` glyph strings, `width` cells between them.
    `ascii_bars` swaps in `#`/`.` with no eighth-block precision, for a
    non-UTF-8 locale (SPEC.md "Main view")."""
    if width <= 0:
        return "", ""
    fraction = 0.0 if total <= 0 else min(max(used / total, 0.0), 1.0)
    if ascii_bars:
        filled = round(fraction * width)
        return "#" * filled, "." * (width - filled)
    eighths_total = round(fraction * width * 8)
    full_cells, eighth = divmod(eighths_total, 8)
    full_cells = min(full_cells, width)
    if eighth and full_cells < width:
        return "█" * full_cells + _EIGHTHS[eighth - 1], "░" * (width - full_cells - 1)
    return "█" * full_cells, "░" * (width - full_cells)


def _bar_text(used: int, total: int, width: int, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    fill, track = _bar_glyphs(used, total, width, ascii_bars=ascii_bars)
    # No fullness colouring (SPEC.md "Main view", "Colour"): one neutral
    # fill colour, a dim track -- under NO_COLOR the glyphs alone (`█`/`#`
    # vs `░`/`.`) still carry the whole picture, styles or not.
    return Text(fill, style=colors.primary) + Text(track, style="dim")


def _pair_slot(used: int, total: int) -> str:
    """The used/total pair, right-aligned in a slot as wide as
    `total/total unit` -- the widest that pair could ever render at this
    total, so a used value crossing a digit boundary never moves anything
    after it (SPEC.md "Main view", "Layout")."""
    unit = unit_of(total)
    total_num = size_in_unit(total, unit)
    slot_width = len(f"{total_num}/{total_num} {unit}")
    return format_pair(used, total).rjust(slot_width)


def _shared_bracket(stats: SystemStats, unit: str) -> str:
    # The padding goes after the closing paren, not before it -- pad the
    # whole `(...)` slot, not just the text inside it.
    worst = size_in_unit(stats.mem_total, unit)
    value = size_in_unit(stats.mem_shared, unit)
    return f"({value} shared)".ljust(len(f"({worst} shared)"))


def _avail_breakdown(stats: SystemStats, unit: str) -> str:
    free = size_in_unit(stats.mem_free, unit)
    cache = size_in_unit(stats.mem_cache, unit)
    slab = size_in_unit(stats.mem_slab, unit)
    return f"{free} free, {cache} cache, {slab} slab"


def _avail_bracket(stats: SystemStats, unit: str) -> str:
    # Same "ljust the whole `(...)` slot" rule as `_shared_bracket`: the
    # breakdown's own numbers stay unpadded, but the worst case comes from
    # `mem_total` in every position, not from the breakdown's own (smaller,
    # and itself varying) numbers.
    worst = size_in_unit(stats.mem_total, unit)
    breakdown = _avail_breakdown(stats, unit)
    worst_breakdown = f"{worst} free, {worst} cache, {worst} slab"
    return f"({breakdown})".ljust(len(f"({worst_breakdown})"))


def _avail_part(stats: SystemStats, unit: str, *, show_breakdown: bool) -> Text:
    # Shown in the total's unit, not avail's own -- the same rule as the
    # used/total pair, so avail's slot is trivial: a digit-count change in
    # avail alone can never move anything after it.
    worst_num = size_in_unit(stats.mem_total, unit)
    avail_num = size_in_unit(stats.mem_available, unit).rjust(len(worst_num))
    text = Text(f"avail {avail_num} {unit}")
    if show_breakdown:
        text = text + Text(f" {_avail_bracket(stats, unit)}")
    return text


def _ram_line(stats: SystemStats, ctx: _RenderCtx, *, disabled: frozenset[str]) -> Text:
    used = stats.mem_total - stats.mem_available
    unit = unit_of(stats.mem_total)
    text = Text("RAM".ljust(_LABEL_WIDTH))
    if ctx.bar_width > 0:
        bar = _bar_text(used, stats.mem_total, ctx.bar_width, ctx.colors, ascii_bars=ctx.ascii_bars)
        text = text + bar + Text("  ")
    text = text + Text(_pair_slot(used, stats.mem_total)) + Text(" used")
    if "shared" not in disabled:
        text = text + Text(" ") + Text(_shared_bracket(stats, unit))
    if "avail" not in disabled:
        show_breakdown = "avail_breakdown" not in disabled
        text = text + Text("   ") + _avail_part(stats, unit, show_breakdown=show_breakdown)
    return text


def _swap_style(used: int, total: int, colors: ThemeColors) -> str | None:
    # Error colour only above 90 % used; the 50 % warning is gone (owner
    # decision: at typical swap levels with pressure `none`, a
    # warning-coloured pair year-round taught the user to ignore colour --
    # SPEC.md "Main view").
    if total > 0 and used / total > 0.9:
        return colors.error
    return None


def _zswap_pool_slot(mem_total: int) -> int:
    return max(_ZSWAP_POOL_MIN_SLOT, len(size(mem_total)))


def _zswap_bracket(stats: SystemStats, unit: str, disabled: frozenset[str]) -> str | None:
    # Same "pad the whole `(...)` slot" rule as `_shared_bracket`.
    if not stats.zswap_enabled or stats.zswapped_bytes is None or stats.zswap_pool_bytes is None:
        return None
    if "zswap" in disabled:
        return None
    worst_zswapped = size_in_unit(stats.swap_total, unit)
    pool_slot = _zswap_pool_slot(stats.mem_total)
    if "zswap_long" in disabled:
        text = format_zswap_part(stats.zswapped_bytes, stats.zswap_pool_bytes, unit, short=True)
        return f"({text})".ljust(len(f"({worst_zswapped} zswapped)"))
    text = format_zswap_part(stats.zswapped_bytes, stats.zswap_pool_bytes, unit)
    # The pool's own worst-case width, not its current value -- the pool
    # changes every tick same as anything else here.
    worst_text = f"({worst_zswapped} zswapped into {'0' * pool_slot} RAM)"
    return f"({text})".ljust(len(worst_text))


def _writeback_token(writeback_rate: int | None, colors: ThemeColors) -> Text | None:
    if writeback_rate is None or writeback_rate <= 0:
        return None
    text = f"to disk {format_rate(writeback_rate)}".ljust(_WRITEBACK_SLOT)
    return Text(text, style=colors.warning)


def _swap_line(stats: SystemStats, ctx: _RenderCtx, *, disabled: frozenset[str]) -> Text:
    label = Text("Swap".ljust(_LABEL_WIDTH))
    if stats.swap_total == 0:
        return label + Text("off")
    used = stats.swap_total - stats.swap_free
    unit = unit_of(stats.swap_total)
    text = label
    if ctx.bar_width > 0:
        bar = _bar_text(
            used, stats.swap_total, ctx.bar_width, ctx.colors, ascii_bars=ctx.ascii_bars
        )
        text = text + bar + Text("  ")
    style = _swap_style(used, stats.swap_total, ctx.colors)
    text = text + Text(_pair_slot(used, stats.swap_total), style=style or "") + Text(" used")
    bracket = _zswap_bracket(stats, unit, disabled)
    if bracket is not None:
        text = text + Text(" ") + Text(bracket)
    wb_token = _writeback_token(ctx.writeback_rate, ctx.colors)
    if wb_token is not None:
        text = text + Text("   ") + wb_token
    return text


def _pressure_word_text(stats: SystemStats, colors: ThemeColors) -> Text:
    if (
        stats.pressure_some_avg10 is None
        or stats.pressure_some_avg60 is None
        or stats.pressure_full_avg10 is None
    ):
        return Text("unavailable")
    word = pressure_word(
        stats.pressure_some_avg10, stats.pressure_some_avg60, stats.pressure_full_avg10
    )
    pressure_color = {"none": colors.success, "some": colors.warning, "high": colors.error}
    head, _, rest = word.partition(" ")
    styled = Text(head, style=f"bold {pressure_color[head]}")
    if rest:
        styled = styled + Text(f" {rest}")
    return styled


def _system_token(stats: SystemStats) -> str:
    system_total = stats.system_ram + stats.system_swap
    return f"system {size(system_total)} [x]"


def format_delta_since(baseline_time: datetime, now: datetime) -> str:
    """`Δ since HH:MM (<elapsed>)`, the Pressure line's Δ part."""
    elapsed = format_elapsed((now - baseline_time).total_seconds())
    return f"Δ since {baseline_time:%H:%M} ({elapsed})"


def _pressure_line(
    stats: SystemStats,
    ctx: _RenderCtx,
    baseline_time: datetime,
    now: datetime,
    *,
    width: int,
    disabled: frozenset[str],
) -> Text:
    word = _pressure_word_text(stats, ctx.colors)
    pad = " " * max(_PRESSURE_SLOT - word.cell_len, 0)
    text = Text("Pressure".ljust(_LABEL_WIDTH)) + word + Text(pad)
    if "system" not in disabled:
        text = text + Text("   ") + Text(_system_token(stats).ljust(_SYSTEM_SLOT))
    # Δ is dropped outright below the width the ΔRAM/ΔSWAP columns hide at,
    # on top of the ordinary per-line fit loop (SPEC.md "Main view",
    # Pressure row).
    if width >= _DELTA_HIDE_WIDTH and "delta" not in disabled:
        text = text + Text("   ") + Text(format_delta_since(baseline_time, now).ljust(_DELTA_SLOT))
    elsewhere = stats.elsewhere
    if "elsewhere" not in disabled and elsewhere is not None and elsewhere >= _ELSEWHERE_THRESHOLD:
        text = text + Text("   ") + Text(f"elsewhere {size(elsewhere)}".ljust(_ELSEWHERE_SLOT))
    return text


def _fit_by_steps(
    build: Callable[[frozenset[str]], Text], steps: Sequence[str], width: int
) -> Text:
    """Drop `steps` in order, lowest priority first, until `build`'s result
    fits `width`. Every part `build` can render sits in a fixed-width slot
    (module docstring), so a given `disabled` set always produces the same
    length regardless of the actual values: the same combination of parts is
    chosen at a given width every time, with nothing to refill afterwards."""
    disabled: set[str] = set()
    text = build(frozenset(disabled))
    while text.cell_len > width:
        remaining = [step for step in steps if step not in disabled]
        if not remaining:
            break
        disabled.add(remaining[0])
        text = build(frozenset(disabled))
    # Belt and braces: even the never-dropped parts could still overflow an
    # extreme width. Never wrap; crop with an ellipsis instead.
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def _three_line_header(
    stats: SystemStats, width: int, ctx: _RenderCtx, baseline_time: datetime, now: datetime
) -> list[Text]:
    ram = _fit_by_steps(lambda d: _ram_line(stats, ctx, disabled=d), _RAM_STEPS, width)
    swap = _fit_by_steps(lambda d: _swap_line(stats, ctx, disabled=d), _SWAP_STEPS, width)
    pressure = _fit_by_steps(
        lambda d: _pressure_line(stats, ctx, baseline_time, now, width=width, disabled=d),
        _PRESSURE_STEPS,
        width,
    )
    return [ram, swap, pressure]


def _two_line_ram(stats: SystemStats, ctx: _RenderCtx, *, disabled: frozenset[str]) -> Text:
    ram = _ram_line(stats, ctx, disabled=disabled)
    block = Text("Pressure ") + _pressure_word_text(stats, ctx.colors)
    # Left-aligned in its slot: a qualifier appearing on the pressure word
    # must never move where "Pressure" itself starts.
    pad = " " * max(_PRESSURE_BLOCK_SLOT - block.cell_len, 0)
    return ram + Text("   ") + block + Text(pad)


def _two_line_swap(
    stats: SystemStats,
    ctx: _RenderCtx,
    baseline_time: datetime,
    now: datetime,
    *,
    width: int,
    disabled: frozenset[str],
) -> Text:
    text = _swap_line(stats, ctx, disabled=disabled)
    if "system" not in disabled:
        text = text + Text("   ") + Text(_system_token(stats).ljust(_SYSTEM_SLOT))
    if width >= _DELTA_HIDE_WIDTH and "delta" not in disabled:
        text = text + Text("   ") + Text(format_delta_since(baseline_time, now).ljust(_DELTA_SLOT))
    return text


def _two_line_header(
    stats: SystemStats, width: int, ctx: _RenderCtx, baseline_time: datetime, now: datetime
) -> list[Text]:
    # H < 18, W >= 80 (SPEC.md "Main view"): RAM (with pressure fixed at the
    # right) then Swap (with system and Δ); `elsewhere` is gone entirely in
    # this mode, not just droppable.
    line1 = _fit_by_steps(lambda d: _two_line_ram(stats, ctx, disabled=d), _RAM_STEPS, width)
    line2 = _fit_by_steps(
        lambda d: _two_line_swap(stats, ctx, baseline_time, now, width=width, disabled=d),
        _TWO_LINE_SWAP_STEPS,
        width,
    )
    return [line1, line2]


def _compact_ram_swap_line(stats: SystemStats) -> Text:
    ram_pair = _pair_slot(stats.mem_total - stats.mem_available, stats.mem_total)
    text = Text(f"RAM {ram_pair}")
    if stats.swap_total == 0:
        return text + Text("  Swap off")
    swap_pair = _pair_slot(stats.swap_total - stats.swap_free, stats.swap_total)
    return text + Text(f"  Swap {swap_pair}")


def _compact_status_line(stats: SystemStats, ctx: _RenderCtx, *, disabled: frozenset[str]) -> Text:
    word = _pressure_word_text(stats, ctx.colors)
    # Same reserved slot as the Pressure line: the word never moves what
    # follows it, and `to disk`/`system` still fit under it at every width
    # this form is used at (9-cell label + 27 + 3 + 19 = 58 at most).
    pad = " " * max(_PRESSURE_SLOT - word.cell_len, 0)
    text = Text("Pressure ") + word + Text(pad)
    wb_token = _writeback_token(ctx.writeback_rate, ctx.colors)
    if wb_token is not None and "to_disk" not in disabled:
        text = text + Text("   ") + wb_token
    if "system" not in disabled:
        text = text + Text("   ") + Text(_system_token(stats).ljust(_SYSTEM_SLOT))
    return text


def _compact_header(stats: SystemStats, width: int, ctx: _RenderCtx) -> list[Text]:
    # H < 18, W < 80 (SPEC.md "Main view"): bare RAM/Swap pairs, then
    # pressure/to-disk/system "as they fit" -- this shape never draws a bar,
    # no Δ, no elsewhere.
    line1 = _compact_ram_swap_line(stats)
    line1.no_wrap = True
    line1.overflow = "ellipsis"
    line2 = _fit_by_steps(
        lambda d: _compact_status_line(stats, ctx, disabled=d), _COMPACT_STEPS, width
    )
    return [line1, line2]


def render_header(
    stats: SystemStats,
    width: int,
    height: int,
    *,
    colors: ThemeColors,
    baseline_time: datetime,
    now: datetime,
    writeback_rate: int | None = None,
    ascii_bars: bool = False,
) -> list[Text]:
    """The main-view header, 2 or 3 `Text` lines depending on the terminal's
    height and width (SPEC.md "Main view"):

    - `height >= 18`: three lines, RAM / Swap / Pressure, each with its own
      gauge bar and its own priority-drop rules.
    - `height < 18`, `width >= 80`: two lines, RAM (with Pressure fixed at
      the right) and Swap (with `system` and Δ); `elsewhere` is gone. Below
      80 columns this form's own worst case can't promise the Pressure word
      won't be cropped, so the compact form takes over instead.
    - `height < 18`, `width < 80`: two bare lines, no bars, no Δ, no
      `elsewhere` -- just RAM/Swap pairs and pressure/`to disk`/`system` as
      they fit.

    Never fewer than two lines. `colors` supplies the active theme's
    success/warning/error/primary colours (SPEC.md "Main view", "Colour"),
    read by the caller from `App.current_theme` once per render. `ascii_bars`
    swaps the gauge glyphs for a non-UTF-8-locale-safe `#`/`.` form.
    """
    ctx = _RenderCtx(
        colors=colors,
        bar_width=_bar_width(width),
        ascii_bars=ascii_bars,
        writeback_rate=writeback_rate,
    )
    if height >= _MIN_HEIGHT_FOR_THREE_LINES:
        return _three_line_header(stats, width, ctx, baseline_time, now)
    # The 80-column floor assumes a 2-digit-GiB RAM total; a wider pair
    # (`65.7/125.7 GiB`) would crop the pressure word, so check line 1 with
    # every droppable part gone. Every part sits in a fixed slot, so this
    # depends on the machine's RAM total, never on a tick's values.
    bare_line1 = _two_line_ram(stats, ctx, disabled=frozenset(_RAM_STEPS))
    if width >= _MIN_WIDTH_FOR_TEXT_TWO_LINE and bare_line1.cell_len <= width:
        return _two_line_header(stats, width, ctx, baseline_time, now)
    return _compact_header(stats, width, ctx)
