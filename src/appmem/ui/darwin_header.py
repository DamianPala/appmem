"""Three host lines with native Darwin meanings and shared gauge styling."""

from __future__ import annotations

from rich.text import Text

from appmem.darwin_native import HostMemory
from appmem.fmt import format_pair, size, size_in_unit, unit_of
from appmem.ui.header import (
    ThemeColors,
    _bar_text,  # pyright: ignore[reportPrivateUsage] - reuse Linux's gauge glyphs and styles
    _bar_width,  # pyright: ignore[reportPrivateUsage] - keep the existing width buckets
)
from appmem.ui.layout import fit_line


def _nonnegative(value: int) -> bool:
    return type(value) is int and value >= 0


def _amount(value: int) -> str:
    return size(value) if _nonnegative(value) else "unavailable"


def _physical(host: HostMemory, width: int) -> Text:
    physical = _amount(host.physical_bytes) if host.physical_bytes > 0 else "unavailable"
    return fit_line(
        (
            ("physical", Text(f"Physical  {physical}")),
            ("free", Text(f"Free {_amount(host.free_bytes)}")),
            ("wired", Text(f"Wired {_amount(host.wired_bytes)}")),
        ),
        ("wired", "free"),
        width,
        separator="  ",
    )


def _swap(host: HostMemory, width: int, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    used, total = host.swap_used_bytes, host.swap_total_bytes
    label = Text("Swap".ljust(10 if width >= 60 else 6))
    if not (_nonnegative(used) and _nonnegative(total) and used <= total):
        return label + Text("unavailable")
    if total == 0:
        return label + Text("0 B used; not allocated")
    pair = Text(format_pair(used, total))
    explanation = " used / currently allocated" if width >= 60 else " used/allocated now"
    value = pair + Text(explanation)
    bar_width = _bar_width(width)
    if label.cell_len + bar_width + 2 + value.cell_len > width:
        bar_width = 0
    bar = (
        _bar_text(used, total, bar_width, colors, ascii_bars=ascii_bars) + Text("  ")
        if bar_width
        else Text()
    )
    return label + bar + value


def _compression(host: HostMemory, *, compact: bool, ascii_bars: bool) -> Text:
    logical, physical = host.compressor_logical_bytes, host.compressor_physical_bytes
    if not (_nonnegative(logical) and _nonnegative(physical)):
        return Text("Compress unavailable")
    arrow = ">" if ascii_bars else "→"
    if compact:
        unit = unit_of(max(logical, physical))
        left, right = size_in_unit(logical, unit), size_in_unit(physical, unit)
        return Text(f"Comp {left}{arrow}{right} {unit} RAM")
    return Text(f"Compress {size(logical)} data {arrow} {size(physical)} RAM")


def _pressure(host: HostMemory, colors: ThemeColors) -> Text:
    level = host.pressure_level if type(host.pressure_level) is int else None
    word = {1: "normal", 2: "warning", 4: "critical"}.get(level or 0)
    if host.pressure_unavailable is not None:
        word = None
    color = {"normal": colors.success, "warning": colors.warning, "critical": colors.error}
    return Text(word or "unavailable", style=f"bold {color[word]}" if word else "dim")


def _status(host: HostMemory, width: int, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    pressure = Text("Pressure".ljust(10 if width >= 60 else 9)) + _pressure(host, colors)
    compression = _compression(host, compact=width < 60, ascii_bars=ascii_bars)
    ratio = None
    if (
        _nonnegative(host.compressor_logical_bytes)
        and _nonnegative(host.compressor_physical_bytes)
        and host.compressor_logical_bytes > 0
        and host.compressor_physical_bytes > 0
    ):
        ratio = Text(f"({host.compressor_logical_bytes / host.compressor_physical_bytes:.1f}:1)")
    return fit_line(
        (("pressure", pressure), ("compression", compression), ("ratio", ratio)),
        ("ratio", "compression"),
        width,
        separator="  ",
    )


def render_host_header(
    host: HostMemory, width: int, *, colors: ThemeColors, ascii_bars: bool = False
) -> tuple[Text, Text, Text]:
    """Swap's denominator is allocated space now, not maximum disk capacity.

    Compression reads logical data -> physical RAM; pressure is a native state.
    Narrow layouts drop the compression ratio, then compression, and wired/free
    figures as needed. Physical memory, swap and pressure retain their labels.
    """
    lines = (
        _physical(host, width),
        _swap(host, width, colors, ascii_bars=ascii_bars),
        _status(host, width, colors, ascii_bars=ascii_bars),
    )
    for line in lines:
        line.no_wrap = True
        line.overflow = "ellipsis"
    return lines
