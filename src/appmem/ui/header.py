"""Shared host gauge styling and responsive Linux header."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.text import Text

from appmem.fmt import (
    format_elapsed,
    format_pair,
    format_rate,
    pressure_word,
    size,
    size_in_unit,
    unit_of,
)
from appmem.model import SystemStats
from appmem.total import total_amount
from appmem.ui.host_grid import geometry, grid_row

_ELSEWHERE_THRESHOLD = 1024 * 1024
_EIGHTHS = "▏▎▍▌▋▊▉"


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


def _bar_text(used: int, total: int, width: int, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    fill, track = _bar_glyphs(used, total, width, ascii_bars=ascii_bars)
    # No fullness colouring (SPEC.md "Main view", "Colour"): one neutral
    # fill colour, a dim track -- under NO_COLOR the glyphs alone (`█`/`#`
    # vs `░`/`.`) still carry the whole picture, styles or not.
    return Text(fill, style=colors.primary) + Text(track, style="dim")


def _swap_style(used: int, total: int, colors: ThemeColors) -> str | None:
    # Error colour only above 90 % used; the 50 % warning is gone (owner
    # decision: at typical swap levels with pressure `none`, a
    # warning-coloured pair year-round taught the user to ignore colour --
    # SPEC.md "Main view").
    if total > 0 and used / total > 0.9:
        return colors.error
    return None


def swap_activity(
    incoming: int | None,
    outgoing: int | None,
    session_written: int | None,
    boot_written: int | None,
    *,
    ascii_bars: bool,
) -> tuple[tuple[int, Text], ...]:
    """Keep rates, session total and boot total as ordered, whole fields."""
    unknown = "?" if ascii_bars else "—"
    read = unknown if incoming is None else format_rate(incoming)
    write = unknown if outgoing is None else format_rate(outgoing)
    separator = " · " if not ascii_bars else " / "
    rates = Text(f"in {read}{separator}out {write}")
    session_total = Text(f"({total_amount(session_written)} this run)")
    boot_total = Text(f"({total_amount(boot_written)} since boot)")
    return (
        (rates.cell_len, rates),
        (session_total.cell_len, session_total),
        (boot_total.cell_len, boot_total),
    )


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


def format_delta_since(baseline_time: datetime, now: datetime) -> str:
    """`Δ since HH:MM (<elapsed>)`, the Pressure line's Δ part."""
    elapsed = format_elapsed((now - baseline_time).total_seconds())
    return f"Δ since {baseline_time:%H:%M} ({elapsed})"


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
    """Fixed host grid, with two rows on short terminals and explicit omission markers."""
    _, bar_width, _ = geometry(width)

    def gauge(used: int, total: int) -> Text:
        return _bar_text(used, total, bar_width, colors, ascii_bars=ascii_bars)

    def row(
        label: str, value: Text, bar: Text | None, metadata: tuple[tuple[int, Text], ...]
    ) -> Text:
        return grid_row(
            label,
            value,
            width,
            gauge=bar,
            metadata=metadata,
            ascii_bars=ascii_bars,
            hidden=height < 18,
        )

    ram = row(
        "RAM",
        Text(format_pair(stats.mem_total - stats.mem_available, stats.mem_total) + " used"),
        gauge(stats.mem_total - stats.mem_available, stats.mem_total),
        _ram_metadata(stats),
    )
    swap_used = stats.swap_total - stats.swap_free
    swap = row(
        "Swap",
        Text(
            format_pair(swap_used, stats.swap_total) + " used" if stats.swap_total else "off",
            style=_swap_style(swap_used, stats.swap_total, colors) or "",
        ),
        gauge(swap_used, stats.swap_total) if stats.swap_total else Text("off"),
        swap_activity(
            swap_in_rate,
            swap_out_rate,
            swap_out_session_total,
            stats.swap_out_bytes,
            ascii_bars=ascii_bars,
        ),
    )
    pressure = row(
        "Pressure",
        Text(f"system {size(stats.system_ram + stats.system_swap)} [x]"),
        _pressure_word_text(stats, colors),
        (
            (
                25,
                Text(
                    format_delta_since(baseline_time, now).replace("Δ", "delta")
                    if ascii_bars
                    else format_delta_since(baseline_time, now)
                ),
            ),
            *(
                ((22, Text(f"elsewhere {size(stats.elsewhere)}")),)
                if stats.elsewhere is not None and stats.elsewhere >= _ELSEWHERE_THRESHOLD
                else ()
            ),
        ),
    )
    if height < 18:
        if width < 70:
            return _compact_header(stats, width, colors, ascii_bars=ascii_bars)
        return [
            ram,
            row(
                "Pressure",
                _pressure_word_text(stats, colors),
                None,
                (
                    (
                        30,
                        Text(
                            "Swap off"
                            if not stats.swap_total
                            else "Swap " + format_pair(swap_used, stats.swap_total)
                        ),
                    ),
                ),
            ),
        ]
    lines = [ram]
    if stats.zswap_enabled:
        lines.append(
            _zswap_grid(stats, width, colors, ascii_bars=ascii_bars, writeback_rate=writeback_rate)
        )
    return [*lines, swap, pressure]


def _zswap_grid(
    stats: SystemStats,
    width: int,
    colors: ThemeColors,
    *,
    ascii_bars: bool,
    writeback_rate: int | None,
) -> Text:
    _, bar_width, _ = geometry(width)
    pool, logical = stats.zswap_pool_bytes, stats.zswapped_bytes
    percent = stats.zswap_max_pool_percent
    limit = stats.mem_total * percent // 100 if type(percent) is int and percent >= 0 else None
    bar = (
        _bar_text(pool, limit, bar_width, colors, ascii_bars=ascii_bars)
        if pool is not None and limit
        else Text("-" * bar_width, style="dim")
    )
    value = Text(
        "unavailable"
        if pool is None
        else format_pair(pool, limit) + " RAM"
        if limit is not None
        else size(pool) + "/? RAM"
    )
    ratio = stats.zswap_compression_ratio
    suffix = f" ({ratio:.1f}:1)" if ratio is not None else ""
    details = [
        (23, Text("holds unavailable" if logical is None else f"holds {size(logical)}{suffix}"))
    ]
    details.append((22, Text("limit ?" if limit is None else f"limit {percent}% of RAM")))
    if pool is not None and limit is not None and pool > limit:
        details.insert(0, (12, Text("over-limit", style=colors.error)))
    if writeback_rate is not None:
        details.append((25, Text(f"writeback {format_rate(writeback_rate)}")))
    return grid_row(
        "Zswap", value, width, gauge=bar, metadata=tuple(details), ascii_bars=ascii_bars
    )


def _ram_metadata(stats: SystemStats) -> tuple[tuple[int, Text], ...]:
    available = Text(f"avail {size(stats.mem_available)}")
    shared = Text(f"shared {size(stats.mem_shared)}")
    unit = unit_of(stats.mem_total)
    breakdown = Text(
        f"({size_in_unit(stats.mem_free, unit)} free, "
        f"{size_in_unit(stats.mem_cache, unit)} cache, "
        f"{size_in_unit(stats.mem_slab, unit)} slab)"
    )
    return ((15, available), (16, shared), (breakdown.cell_len, breakdown))


def _compact_header(
    stats: SystemStats, width: int, colors: ThemeColors, *, ascii_bars: bool
) -> list[Text]:
    ram = format_pair(stats.mem_total - stats.mem_available, stats.mem_total)
    swap = (
        format_pair(stats.swap_total - stats.swap_free, stats.swap_total)
        if stats.swap_total
        else "off"
    )
    values = Text(f"RAM {ram}  Swap {swap}")
    values.truncate(max(0, width - 1))
    values.rstrip()
    values += Text(" " if values.cell_len < width - 1 else "")
    values += Text(">" if ascii_bars else "…", style="")
    values.no_wrap = True
    return [
        values,
        grid_row(
            "Pressure",
            _pressure_word_text(stats, colors),
            width,
            ascii_bars=ascii_bars,
            hidden=True,
        ),
    ]
