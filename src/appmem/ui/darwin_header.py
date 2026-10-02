"""Four host rows with native Darwin meanings and shared gauge styling."""

from __future__ import annotations

from rich.text import Text

from appmem.darwin_native import HostMemory
from appmem.fmt import format_elapsed, format_pair, size, size_in_unit, unit_of
from appmem.ui.header import (
    ThemeColors,
    _bar_text,  # pyright: ignore[reportPrivateUsage] - reuse Linux gauge styling
    _bar_width,  # pyright: ignore[reportPrivateUsage] - retain shared width buckets
    activity_token,
)
from appmem.ui.layout import fit_line


def _nonnegative(value: int) -> bool:
    return type(value) is int and value >= 0


def _amount(value: int) -> str:
    return size(value) if _nonnegative(value) else "unavailable"


def _ram(host: HostMemory, width: int, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    partition = host.ram_partition
    label = Text("RAM".ljust(10 if width >= 60 else 6))
    if partition is None:
        return fit_line(
            (
                ("ram", label + Text("used unavailable")),
                ("total", Text(f"total {_amount(host.physical_bytes)}")),
            ),
            ("total",),
            width,
            separator="; ",
        )
    used, backed, free = partition
    value = Text(format_pair(used, host.physical_bytes) + " used")
    details = Text(f"file-backed {size(backed)}  free {size(free)}")
    wired = _amount(host.wired_bytes)
    purgeable = host.purgeable_bytes
    annotation = Text(f"({wired} wired)")
    if type(purgeable) is int and 0 <= purgeable <= host.physical_bytes:
        annotation = Text(f"({wired} wired, {size(purgeable)} purgeable)")
    # Width buckets reserve slots for normal values. Tick-to-tick number changes
    # do not decide whether the gauge or the annotation exists.
    bar_width = 30 if width >= 160 else 16 if width >= 120 else 10 if width >= 80 else 0
    if width >= 80 and value.cell_len <= 20 and details.cell_len <= 35:
        bar = _bar_text(used, host.physical_bytes, bar_width, colors, ascii_bars=ascii_bars)
        primary = label + bar + Text("  ") + value
        primary.append(" " * (21 - value.cell_len))
        if width >= 120 and annotation.cell_len < 36:
            primary += annotation
            primary.append(" " * (36 - annotation.cell_len))
        return primary + details
    # Oversized counters degrade by dropping secondary details before units.
    bar_width = _bar_width(width) if width < 80 else 0
    if label.cell_len + bar_width + value.cell_len + details.cell_len + 4 > width:
        bar_width = 0
    bar = (
        _bar_text(used, host.physical_bytes, bar_width, colors, ascii_bars=ascii_bars) + Text("  ")
        if bar_width
        else Text()
    )
    return fit_line(
        (("ram", label + bar + value), ("details", details)), ("details",), width, separator="  "
    )


def _swap(
    host: HostMemory,
    width: int,
    colors: ThemeColors,
    *,
    ascii_bars: bool,
    swap_in_rate: int | None,
    swap_out_rate: int | None,
) -> Text:
    used, total = host.swap_used_bytes, host.swap_total_bytes
    label = Text("Swap".ljust(10 if width >= 60 else 6))
    if not (_nonnegative(used) and _nonnegative(total) and used <= total):
        return label + Text("unavailable")
    value = (
        Text("0 B used; not allocated")
        if total == 0
        else Text(
            format_pair(used, total).rjust(len(format_pair(total, total))) + " used/allocated now"
        )
    )
    dynamic = Text("(dynamic allocation)") if width >= 160 else None
    activity = (
        activity_token("in", swap_in_rate, ascii_bars=ascii_bars)
        + Text(" ")
        + activity_token("out", swap_out_rate, ascii_bars=ascii_bars)
        if width >= 80
        else None
    )
    bar_width = 20 if width >= 120 else 6 if width >= 80 else _bar_width(width)
    reserved = 32 if activity is not None else 0
    if label.cell_len + bar_width + 2 + value.cell_len + reserved > width:
        bar_width = 0
    bar = Text()
    if bar_width:
        bar = (
            Text("-" * bar_width, style="dim")
            if total == 0
            else _bar_text(used, total, bar_width, colors, ascii_bars=ascii_bars)
        ) + Text("  ")
    return fit_line(
        (("swap", label + bar + value), ("activity", activity), ("dynamic", dynamic)),
        ("dynamic",),
        width,
        separator="  ",
    )


def _compression(host: HostMemory, width: int, *, ascii_bars: bool) -> Text:
    logical, physical = host.compressor_logical_bytes, host.compressor_physical_bytes
    if not (_nonnegative(logical) and _nonnegative(physical)):
        return Text("Compress unavailable")
    arrow = ">" if ascii_bars else "→"
    if width < 60:
        unit = unit_of(max(logical, physical))
        left, right = size_in_unit(logical, unit), size_in_unit(physical, unit)
        compression = Text(f"Comp {left}{arrow}{right} {unit} RAM")
    else:
        compression = Text(f"Compress  {size(logical)} data {arrow} {size(physical)} RAM")
    ratio = Text(f"({logical / physical:.1f}:1)") if logical > 0 and physical > 0 else None
    return fit_line(
        (("compression", compression), ("ratio", ratio)), ("ratio",), width, separator="  "
    )


def _pressure(host: HostMemory, colors: ThemeColors) -> Text:
    level = host.pressure_level if type(host.pressure_level) is int else None
    word = {1: "normal", 2: "warning", 4: "critical"}.get(level or 0)
    if host.pressure_unavailable is not None:
        word = None
    color = {"normal": colors.success, "warning": colors.warning, "critical": colors.error}
    return Text(word or "unavailable", style=f"bold {color[word]}" if word else "dim")


def render_host_header(
    host: HostMemory,
    width: int,
    *,
    colors: ThemeColors,
    ascii_bars: bool = False,
    baseline_time: str | None = None,
    baseline_elapsed: int = 0,
    swap_in_rate: int | None = None,
    swap_out_rate: int | None = None,
) -> tuple[Text, Text, Text, Text]:
    """RAM excludes file-backed; compression is logical data -> physical RAM.

    Swap uses dynamically allocated space now. Pressure is native, never a RAM
    percentage. Growth timing identifies the session/reset epoch; app baselines
    can be newer when identity or coverage changes.
    """
    pressure = Text("Pressure".ljust(10 if width >= 60 else 9)) + _pressure(host, colors)
    scope = Text("current user")
    baseline = (
        Text(
            f"{'delta' if ascii_bars else 'Δ'} since {baseline_time[:5]} "
            f"({format_elapsed(max(0, baseline_elapsed))})"
        )
        if baseline_time is not None
        else None
    )
    if baseline_time is not None and width < 60:
        baseline = Text(f"{baseline_time[:5]} {format_elapsed(max(0, baseline_elapsed))}")
    lines = (
        _ram(host, width, colors, ascii_bars=ascii_bars),
        _compression(host, width, ascii_bars=ascii_bars),
        _swap(
            host,
            width,
            colors,
            ascii_bars=ascii_bars,
            swap_in_rate=swap_in_rate,
            swap_out_rate=swap_out_rate,
        ),
        fit_line(
            (("pressure", pressure), ("scope", scope), ("baseline", baseline)),
            ("baseline",),
            width,
            separator="  ",
        ),
    )
    for line in lines:
        line.no_wrap = True
        line.overflow = "ellipsis"
    return lines
