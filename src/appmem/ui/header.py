"""Gauge styling and the responsive Linux host header on the shared grid."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.text import Text

from appmem.fmt import (
    format_elapsed,
    format_rate,
    pressure_word,
    size,
    size_in_unit,
    unit_of,
)
from appmem.model import SystemStats
from appmem.ui.host_grid import (
    RATE_FIELD,
    TOTAL_FIELD,
    Item,
    Layout,
    Slots,
    State,
    amount,
    choose,
    compact_total,
    field_width,
    gauge_left,
    grid_row,
    pair,
    pair_width,
    parts,
    rate,
    spaces,
    span,
    state_left,
    unknown,
    value_text,
)

_ELSEWHERE_THRESHOLD = 1024 * 1024
_EIGHTHS = "▏▎▍▌▋▊▉"
_SHORT_HEIGHT = 18
_COMPACT_WIDTH = 70
_PRESSURE_FULL = len("none (was 99.9 %)")
_PRESSURE_SHORT = len("unavailable")
_LABELS = (5, 6)  # avail / holds / in, shared / ratio / out
_DELTA_NEED = len("Δ since 00:00 (99h59m)")
_DELTA_FIRST_WIDTH = 100


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


def bar_text(used: int, total: int, width: int, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    fill, track = _bar_glyphs(used, total, width, ascii_bars=ascii_bars)
    # No fullness colouring (SPEC.md "Main view", "Colour"): one neutral
    # fill colour, a dim track -- under NO_COLOR the glyphs alone (`█`/`#`
    # vs `░`/`.`) still carry the whole picture, styles or not.
    return Text(fill, style=colors.primary) + Text(track, style="dim")


def placeholder_bar(width: int) -> Text:
    """The neutral gauge of a row whose total is zero or unknown."""
    return Text("-" * width, style="dim")


def _swap_style(used: int, total: int, colors: ThemeColors) -> str | None:
    # Error colour only above 90 % used; the 50 % warning is gone (owner
    # decision: at typical swap levels with pressure `none`, a
    # warning-coloured pair year-round taught the user to ignore colour --
    # SPEC.md "Main view").
    if total > 0 and used / total > 0.9:
        return colors.error
    return None


def activity_items(
    lay: Layout,
    *,
    incoming: int | None,
    outgoing: int | None,
    session_written: int | None,
    boot_written: int | None,
    ascii_bars: bool,
) -> list[Item]:
    """Swap activity: `in` and `out` rates in columns 1 and 2, the written
    totals in column 3 as label-first pairs. An unknown value is `—` in its own field."""
    run, boot = ("this run", "since boot") if lay.wide else ("run", "boot")
    written = Text(f"written {run} {compact_total(session_written, ascii_bars=ascii_bars)}")
    since = Text(f"{boot} {compact_total(boot_written, ascii_bars=ascii_bars)}")
    return [
        pair("in", rate(incoming, ascii_bars=ascii_bars), 1, lay),
        pair("out", rate(outgoing, ascii_bars=ascii_bars), 2, lay),
        *parts(
            [
                (written, len(f"written {run} ") + TOTAL_FIELD),
                (since, len(f"{boot} ") + TOTAL_FIELD),
            ],
            ascii_bars=ascii_bars,
        ),
    ]


def _pressure_state(stats: SystemStats, colors: ThemeColors) -> State:
    if (
        stats.pressure_some_avg10 is None
        or stats.pressure_some_avg60 is None
        or stats.pressure_full_avg10 is None
    ):
        word = Text("unavailable")
        # Full form reserves the worst case too, so PSI going away moves nothing.
        return State(word, _PRESSURE_FULL, word, _PRESSURE_SHORT)
    full = pressure_word(
        stats.pressure_some_avg10, stats.pressure_some_avg60, stats.pressure_full_avg10
    )
    pressure_color = {"none": colors.success, "some": colors.warning, "high": colors.error}
    head, _, rest = full.partition(" ")
    short = Text(head, style=f"bold {pressure_color[head]}")
    long = short + Text(f" {rest}") if rest else short
    return State(long, _PRESSURE_FULL, short, _PRESSURE_SHORT)


def format_delta_since(baseline_time: datetime, now: datetime) -> str:
    """`Δ since HH:MM (<elapsed>)`, the Pressure line's Δ part."""
    elapsed = format_elapsed((now - baseline_time).total_seconds())
    return f"Δ since {baseline_time:%H:%M} ({elapsed})"


def _delta_item(
    lay: Layout, baseline_time: datetime, now: datetime, *, ascii_bars: bool, packed: bool = False
) -> Item:
    text = format_delta_since(baseline_time, now)
    need = _DELTA_NEED
    if ascii_bars:
        text, need = text.replace("Δ", "delta"), need + 4
    return Item(Text(text), need, spaces(lay.gap)) if packed else span(Text(text), need, lay)


def _system_need(amount_width: int) -> int:
    return len("system ") + amount_width + len(" [x]")


def _system_text(stats: SystemStats, lay: Layout) -> Text:
    total = size(stats.system_ram + stats.system_swap)
    return Text(f"system {total:>{lay.slots.amount}} [x]")


def _zswap_limit(stats: SystemStats) -> int | None:
    percent = stats.zswap_max_pool_percent
    return stats.mem_total * percent // 100 if type(percent) is int and percent >= 0 else None


def _slots(stats: SystemStats) -> Slots:
    """Slot widths from this machine's totals: RAM, swap and the zswap pool limit."""
    values = [pair_width(stats.mem_total, "used")]
    values.append(pair_width(stats.swap_total, "used") if stats.swap_total else len("off"))
    limit = _zswap_limit(stats)
    if stats.zswap_enabled and limit:
        values.append(pair_width(limit, "RAM"))
    amount_width = field_width(stats.mem_total + stats.swap_total)
    return Slots(
        *_LABELS, max(values), amount_width, _PRESSURE_SHORT + 1 + _system_need(amount_width)
    )


def _pair_value(used: int, total: int, suffix: str) -> tuple[str, str]:
    unit = unit_of(total)
    number = size_in_unit(total, unit)
    return value_text(used, total, suffix, unit, number), unit


def _ram_row(ctx: _Ctx, *, lead: list[Item] | None = None, shared_first: bool = False) -> Text:
    """The RAM row. `lead` items (the short form's pressure word) come first."""
    stats, lay = ctx.stats, ctx.lay
    used = stats.mem_total - stats.mem_available
    value, unit = _pair_value(used, stats.mem_total, "used")
    left = gauge_left(
        lay,
        bar_text(used, stats.mem_total, lay.gauge, ctx.colors, ascii_bars=ctx.ascii_bars),
        Text(value),
    )
    avail = pair("avail", amount(stats.mem_available, ascii_bars=False), 1, lay)
    shared = pair("shared", amount(stats.mem_shared, ascii_bars=False), 2, lay)
    columns = [shared, avail] if shared_first else [avail, shared]
    items = [*(lead or []), *columns, *_breakdown(stats, unit, ascii_bars=ctx.ascii_bars)]
    return grid_row("RAM", left, lay, items, ascii_bars=ctx.ascii_bars)


def _breakdown(stats: SystemStats, unit: str, *, ascii_bars: bool) -> list[Item]:
    digits = len(size_in_unit(stats.mem_total, unit))
    entries = (("free", stats.mem_free), ("cache", stats.mem_cache), ("slab", stats.mem_slab))
    texts = [
        (Text(f"{name} {size_in_unit(value, unit):>{digits}}"), len(name) + 1 + digits)
        for name, value in entries
    ]
    return parts(texts, ascii_bars=ascii_bars)


def _zswap_row(ctx: _Ctx, writeback_rate: int | None) -> Text:
    stats, lay, ascii_bars = ctx.stats, ctx.lay, ctx.ascii_bars
    pool, logical = stats.zswap_pool_bytes, stats.zswapped_bytes
    percent = stats.zswap_max_pool_percent
    limit = _zswap_limit(stats)
    over = pool is not None and limit is not None and pool > limit
    bar = (
        bar_text(pool, limit, lay.gauge, ctx.colors, ascii_bars=ascii_bars)
        if pool is not None and limit
        else placeholder_bar(lay.gauge)
    )
    if pool is None:
        value = "unavailable"
    elif limit is None:
        value = f"{size(pool)}/{unknown(ascii_bars)} RAM"
    else:
        value = _pair_value(pool, limit, "RAM")[0]
    shown = unknown(ascii_bars) if percent is None else f"{percent}%"
    limit_text = Text(f"{'above' if over else 'limit'} {shown}" + (" of RAM" if lay.wide else ""))
    if over:
        limit_text.stylize(ctx.colors.error)
    rate_text = unknown(ascii_bars) if writeback_rate is None else format_rate(writeback_rate)
    writeback = Text(f"writeback {rate_text}")
    ratio = stats.zswap_compression_ratio
    return grid_row(
        "Zswap",
        gauge_left(lay, bar, Text(value, style=ctx.colors.error if over else "")),
        lay,
        [
            pair("holds", amount(logical, ascii_bars=ascii_bars), 1, lay),
            pair("ratio", unknown(ascii_bars) if ratio is None else f"{ratio:.1f}:1", 2, lay),
            *parts(
                [
                    (limit_text, len("limit ") + len("100%") + (len(" of RAM") if lay.wide else 0)),
                    (writeback, len("writeback ") + RATE_FIELD),
                ],
                ascii_bars=ascii_bars,
            ),
        ],
        ascii_bars=ascii_bars,
    )


@dataclass(frozen=True)
class _Ctx:
    """What every row builder of one render needs."""

    stats: SystemStats
    lay: Layout
    colors: ThemeColors
    ascii_bars: bool


def _swap_left(ctx: _Ctx) -> Text:
    stats, lay = ctx.stats, ctx.lay
    used = stats.swap_total - stats.swap_free
    if stats.swap_total:
        value = Text(
            _pair_value(used, stats.swap_total, "used")[0],
            style=_swap_style(used, stats.swap_total, ctx.colors) or "",
        )
        bar = bar_text(used, stats.swap_total, lay.gauge, ctx.colors, ascii_bars=ctx.ascii_bars)
    else:
        value, bar = Text("off"), placeholder_bar(lay.gauge)
    return gauge_left(lay, bar, value)


def _pressure_row(ctx: _Ctx, baseline_time: datetime, now: datetime) -> Text:
    stats, lay = ctx.stats, ctx.lay
    left, hidden = state_left(
        lay,
        _pressure_state(stats, ctx.colors),
        _system_text(stats, lay),
        _system_need(lay.slots.amount),
    )
    items = [_delta_item(lay, baseline_time, now, ascii_bars=ctx.ascii_bars)]
    if hidden:  # no room beside the word below 80 columns: it leads the right part, as in 0.2.0
        system = Item(_system_text(stats, lay), _system_need(lay.slots.amount), spaces(lay.gap))
        items, hidden = [system, *items], False
    if stats.elsewhere is not None and stats.elsewhere >= _ELSEWHERE_THRESHOLD:
        text = Text(f"elsewhere {size(stats.elsewhere)}")
        items.append(Item(text, len("elsewhere ") + lay.slots.amount, Text()))
    return grid_row("Pressure", left, lay, items, ascii_bars=ctx.ascii_bars, hidden=hidden)


def _activity(
    ctx: _Ctx, rates: tuple[int | None, int | None], session_written: int | None
) -> list[Item]:
    return activity_items(
        ctx.lay,
        incoming=rates[0],
        outgoing=rates[1],
        session_written=session_written,
        boot_written=ctx.stats.swap_out_bytes,
        ascii_bars=ctx.ascii_bars,
    )


def render_header(  # noqa: PLR0913 - independent keyword-only render inputs
    stats: SystemStats,
    width: int,
    height: int,
    *,
    colors: ThemeColors,
    baseline_time: datetime,
    now: datetime,
    writeback_rate: int | None = None,
    swap_in_rate: int | None = None,
    swap_out_rate: int | None = None,
    swap_out_session_total: int | None = None,
    ascii_bars: bool = False,
) -> list[Text]:
    """The host header rows: four (three without zswap), two below 18 terminal rows."""
    rates = (swap_in_rate, swap_out_rate)

    def full_rows(lay: Layout) -> list[Text]:
        ctx = _Ctx(stats, lay, colors, ascii_bars)
        activity = _activity(ctx, rates, swap_out_session_total)
        rows = [_ram_row(ctx)]
        if stats.zswap_enabled:
            rows.append(_zswap_row(ctx, writeback_rate))
        rows.append(grid_row("Swap", _swap_left(ctx), lay, activity, ascii_bars=ascii_bars))
        rows.append(_pressure_row(ctx, baseline_time, now))
        return rows

    # The form follows the four-row layout at every height, so a resize of the
    # height never changes the gauge or the words.
    lay = choose(width, _slots(stats), full_rows)
    if height >= _SHORT_HEIGHT:
        return full_rows(lay)
    ctx = _Ctx(stats, lay, colors, ascii_bars)
    return _short_rows(ctx, baseline_time, now, _activity(ctx, rates, swap_out_session_total))


def _short_rows(
    ctx: _Ctx, baseline_time: datetime, now: datetime, activity: list[Item]
) -> list[Text]:
    """Two rows below 18 terminal rows: RAM with the pressure word, Swap with
    the system amount, the Δ baseline and the zswapped data, then the usual
    items as space allows. Width alone picks which of Δ and zswapped goes first."""
    lay, stats = ctx.lay, ctx.stats
    if lay.width < _COMPACT_WIDTH:
        return _compact_rows(ctx)
    state = _pressure_state(stats, ctx.colors)
    word, need = (state.full, state.full_need) if lay.wide else (state.short, state.short_need)
    label = Text("Pressure ") + word
    ram = _ram_row(
        ctx, lead=[Item(label, len("Pressure ") + need, spaces(lay.gap))], shared_first=True
    )
    delta = _delta_item(lay, baseline_time, now, ascii_bars=ctx.ascii_bars, packed=True)
    system = Item(_system_text(stats, lay), _system_need(lay.slots.amount), spaces(lay.gap))
    zswapped = _zswapped_item(ctx)
    ordered = (zswapped, delta) if lay.width < _DELTA_FIRST_WIDTH else (delta, zswapped)
    middle = [item for item in ordered if item is not None]
    swap = grid_row(
        "Swap",
        _swap_left(ctx),
        lay,
        [system, *middle, *activity],
        ascii_bars=ctx.ascii_bars,
    )
    return [ram, swap]


def _zswapped_item(ctx: _Ctx) -> Item | None:
    """`5.1 zswapped`, with `into 1.2 GiB RAM` when wide: the swapped data the
    pool holds, in the Swap pair's unit."""
    stats, lay = ctx.stats, ctx.lay
    if not stats.zswap_enabled or not stats.swap_total:
        return None  # without swap there is no unit to give it and nothing it can hold
    unit = unit_of(stats.swap_total)
    digits = len(size_in_unit(stats.swap_total, unit))
    held = stats.zswapped_bytes
    number = unknown(ctx.ascii_bars) if held is None else size_in_unit(held, unit)
    text = f"{number:>{digits}} zswapped"
    need = digits + len(" zswapped")
    pool = stats.zswap_pool_bytes
    if lay.wide:
        shown = unknown(ctx.ascii_bars) if pool is None else size(pool)
        text += f" into {shown:>{lay.slots.amount}} RAM"
        need += len(" into ") + lay.slots.amount + len(" RAM")
    return Item(Text(text), need, spaces(lay.gap))


def _compact_rows(ctx: _Ctx) -> list[Text]:
    """Below 70 columns: the two pairs on one row, then the pressure row."""
    stats, lay = ctx.stats, ctx.lay
    ram = _pair_value(stats.mem_total - stats.mem_available, stats.mem_total, "")[0].rstrip()
    swap = (
        _pair_value(stats.swap_total - stats.swap_free, stats.swap_total, "")[0].rstrip()
        if stats.swap_total
        else "off"
    )
    values = Text(f"RAM {ram}  Swap {swap}")
    values.truncate(max(0, lay.width - 1))
    values.rstrip()
    values += Text(" " if values.cell_len < lay.width - 1 else "")
    values += Text(">" if ctx.ascii_bars else "…", style="")
    values.no_wrap = True
    state, hidden = state_left(lay, _pressure_state(stats, ctx.colors), None, 0)
    pressure = grid_row(
        "Pressure",
        state,
        lay,
        [Item(_system_text(stats, lay), _system_need(lay.slots.amount), Text())],
        ascii_bars=ctx.ascii_bars,
        hidden=hidden,
    )
    return [values, pressure]
