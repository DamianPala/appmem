"""Host header with native Darwin meanings and shared fixed geometry."""

from __future__ import annotations

from rich.text import Text

from appmem.darwin_native import HostMemory
from appmem.fmt import format_elapsed, format_pair, size
from appmem.ui.header import (  # pyright: ignore[reportPrivateUsage]
    ThemeColors,
    _bar_text,  # pyright: ignore[reportPrivateUsage] - shared gauge style
    activity_token,
)
from appmem.ui.host_grid import geometry, grid_row


def _nonnegative(value: int) -> bool:
    return type(value) is int and value >= 0


def _amount(value: int | None) -> str:
    return size(value) if type(value) is int and value >= 0 else "unavailable"


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
    _, bar_width, _ = geometry(width)

    def row(
        label: str,
        value: Text,
        bar: Text | None = None,
        metadata: tuple[tuple[int, Text], ...] = (),
    ) -> Text:
        return grid_row(label, value, width, gauge=bar, metadata=metadata, ascii_bars=ascii_bars)

    partition = host.ram_partition
    if partition is None:
        ram = row(
            "RAM",
            Text("used unavailable"),
            metadata=((24, Text(f"total {_amount(host.physical_bytes)}")),),
        )
    else:
        used, backed, free = partition
        ram = row(
            "RAM",
            Text(format_pair(used, host.physical_bytes) + " used"),
            _bar_text(used, host.physical_bytes, bar_width, colors, ascii_bars=ascii_bars),
            (
                (20, Text(f"file-backed {size(backed)}")),
                (12, Text(f"free {size(free)}")),
                (14, Text(f"wired {_amount(host.wired_bytes)}")),
                (17, Text(f"purgeable {_amount(host.purgeable_bytes)}")),
            ),
        )
    logical, physical = host.compressor_logical_bytes, host.compressor_physical_bytes
    valid = _nonnegative(logical) and _nonnegative(physical)
    arrow = ">" if ascii_bars else "→"
    ratio = f"({logical / physical:.1f}:1)" if valid and logical > 0 and physical > 0 else "ratio ?"
    compression = row(
        "Compress",
        Text(_amount(physical) + " RAM" if valid else "unavailable"),
        metadata=((35, Text(f"{_amount(logical)} data {arrow} RAM {ratio}")),),
    )
    used, total = host.swap_used_bytes, host.swap_total_bytes
    valid_swap = _nonnegative(used) and _nonnegative(total) and used <= total
    swap_value = format_pair(used, total) + " used" if total else "0 B; not allocated"
    if width < 60 and total > 0:
        swap_value = format_pair(used, total) + " used/alloc now"
    swap_bar = (
        _bar_text(used, total, bar_width, colors, ascii_bars=ascii_bars)
        if total and valid_swap
        else Text("-" * bar_width, style="dim")
    )
    swap = row(
        "Swap",
        Text(swap_value if valid_swap else "unavailable"),
        swap_bar if valid_swap else None,
        (
            (14, Text("allocated now ")),
            (15, activity_token("in", swap_in_rate, ascii_bars=ascii_bars)),
            (16, activity_token("out", swap_out_rate, ascii_bars=ascii_bars)),
            (9, Text("(dynamic)")),
        ),
    )
    baseline = (
        f"{'delta' if ascii_bars else 'Δ'} since {baseline_time[:5]} "
        f"({format_elapsed(max(0, baseline_elapsed))})"
        if baseline_time
        else ""
    )
    if width < 110:
        baseline = baseline.replace(" since ", " ")
    pressure = row(
        "Pressure",
        Text(),
        _pressure(host, colors),
        metadata=(
            (15, Text("apps: this user")),
            *(((25 if width >= 110 else 19, Text(baseline)),) if baseline else ()),
        ),
    )
    return ram, compression, swap, pressure
