"""Host header with native Darwin meanings on the shared grid."""

from __future__ import annotations

from rich.text import Text

from appmem.darwin_native import HostMemory
from appmem.fmt import format_elapsed, size, size_in_unit, unit_of
from appmem.ui.header import ThemeColors, activity_items, bar_text, placeholder_bar
from appmem.ui.host_grid import (
    Item,
    Layout,
    Slots,
    State,
    amount,
    choose,
    field_width,
    gauge_left,
    grid_row,
    pair,
    pair_width,
    parts,
    spaces,
    span,
    state_left,
    unknown,
    value_text,
)

_PRESSURE_WORD = len("unavailable")
_SCOPE = "apps: this user"
_BASELINE_NEED = len("Δ since 00:00 (99h59m)")
_LABELS = (5, 5)  # wired / data / in, free / ratio / out
_NOT_ALLOCATED = "0 B; not allocated"
_USED_UNAVAILABLE = "used unavailable"


def _nonnegative(value: int | None) -> bool:
    return type(value) is int and value >= 0


def _known(value: int | None) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _slots(host: HostMemory) -> Slots:
    """Slot widths from the host's physical memory and swap allocation."""
    physical = _known(host.physical_bytes) or 0
    swap = _known(host.swap_total_bytes)
    values = [pair_width(physical, "used"), len(_USED_UNAVAILABLE)]
    if swap:
        values.append(pair_width(swap, "used/alloc"))
    elif swap == 0:
        values.append(len(_NOT_ALLOCATED))
    return Slots(*_LABELS, max(values), field_width(physical), _PRESSURE_WORD + 1 + len(_SCOPE))


def _pressure_state(host: HostMemory, colors: ThemeColors) -> State:
    level = host.pressure_level if type(host.pressure_level) is int else None
    word = {1: "normal", 2: "warning", 4: "critical"}.get(level or 0)
    if host.pressure_unavailable is not None:
        word = None
    color = {"normal": colors.success, "warning": colors.warning, "critical": colors.error}
    text = Text.assemble((word or "unavailable", f"bold {color[word]}" if word else "dim"))
    return State(text, _PRESSURE_WORD, text, _PRESSURE_WORD)


def _ram_row(host: HostMemory, lay: Layout, colors: ThemeColors, *, ascii_bars: bool) -> Text:
    """An unknown partition keeps all four fields and shows `—` in the unknown ones."""
    used, backed, free = host.ram_partition or (None, None, None)
    physical = _known(host.physical_bytes)
    bar: Text | None = None
    if not physical:
        value = _USED_UNAVAILABLE
    else:
        unit = unit_of(physical)
        number = size_in_unit(physical, unit)
        if used is None:
            value = f"{unknown(ascii_bars):>{len(number)}}/{number} {unit} used"
        else:
            value = value_text(used, physical, "used", unit, number)
            bar = bar_text(used, physical, lay.gauge, colors, ascii_bars=ascii_bars)
    field = lay.slots.amount
    backed_text = Text("file-backed " + amount(backed, ascii_bars=ascii_bars).rjust(field))
    purgeable = amount(_known(host.purgeable_bytes), ascii_bars=ascii_bars)
    purgeable_text = Text("purgeable " + purgeable.rjust(field))
    return grid_row(
        "RAM",
        gauge_left(lay, bar, Text(value)),
        lay,
        [
            pair("wired", amount(_known(host.wired_bytes), ascii_bars=ascii_bars), 1, lay),
            pair("free", amount(free, ascii_bars=ascii_bars), 2, lay),
            *parts(
                [
                    (backed_text, len("file-backed ") + field),
                    (purgeable_text, len("purgeable ") + field),
                ],
                ascii_bars=ascii_bars,
            ),
        ],
        ascii_bars=ascii_bars,
    )


def _compression_row(host: HostMemory, lay: Layout, *, ascii_bars: bool) -> Text:
    logical, physical = host.compressor_logical_bytes, host.compressor_physical_bytes
    valid = _nonnegative(logical) and _nonnegative(physical)
    value = Text(f"{size(physical)} RAM" if valid else "unavailable")
    ratio = f"{logical / physical:.1f}:1" if valid and logical > 0 and physical > 0 else None
    return grid_row(
        "Compress",
        gauge_left(lay, None, value),
        lay,
        [
            pair("data", amount(_known(logical), ascii_bars=ascii_bars), 1, lay),
            pair("ratio", unknown(ascii_bars) if ratio is None else ratio, 2, lay),
        ],
        ascii_bars=ascii_bars,
    )


def _swap_row(
    host: HostMemory,
    lay: Layout,
    colors: ThemeColors,
    rates: tuple[int | None, int | None],
    session_written: int | None,
    *,
    ascii_bars: bool,
) -> Text:
    used, total = host.swap_used_bytes, host.swap_total_bytes
    valid = _nonnegative(used) and _nonnegative(total) and used <= total
    if not valid:
        value, bar = "unavailable", None
    elif total:
        unit = unit_of(total)
        value = value_text(used, total, "used/alloc", unit, size_in_unit(total, unit))
        bar = bar_text(used, total, lay.gauge, colors, ascii_bars=ascii_bars)
    else:
        value, bar = _NOT_ALLOCATED, placeholder_bar(lay.gauge)
    return grid_row(
        "Swap",
        gauge_left(lay, bar, Text(value)),
        lay,
        activity_items(
            lay,
            incoming=rates[0],
            outgoing=rates[1],
            session_written=session_written,
            boot_written=host.swap_out_bytes,
            ascii_bars=ascii_bars,
        ),
        ascii_bars=ascii_bars,
    )


def _pressure_row(
    host: HostMemory,
    lay: Layout,
    colors: ThemeColors,
    baseline_time: str | None,
    baseline_elapsed: int,
    *,
    ascii_bars: bool,
) -> Text:
    left, hidden = state_left(lay, _pressure_state(host, colors), Text(_SCOPE), len(_SCOPE))
    items: list[Item] = []
    if hidden:  # no room beside the word below 80 columns: it leads the right part, as on Linux
        items, hidden = [Item(Text(_SCOPE), len(_SCOPE), spaces(lay.gap))], False
    if baseline_time:
        word = "delta" if ascii_bars else "Δ"
        text = f"{word} since {baseline_time[:5]} ({format_elapsed(max(0, baseline_elapsed))})"
        items.append(span(Text(text), _BASELINE_NEED + (4 if ascii_bars else 0), lay))
    return grid_row("Pressure", left, lay, items, ascii_bars=ascii_bars, hidden=hidden)


def render_host_header(  # noqa: PLR0913 - independent keyword-only render inputs
    host: HostMemory,
    width: int,
    *,
    colors: ThemeColors,
    ascii_bars: bool = False,
    baseline_time: str | None = None,
    baseline_elapsed: int = 0,
    swap_in_rate: int | None = None,
    swap_out_rate: int | None = None,
    swap_out_session_total: int | None = None,
) -> tuple[Text, Text, Text, Text]:
    """RAM excludes file-backed; compression is logical data -> physical RAM.

    Swap uses dynamically allocated space now. Pressure is native, never a RAM
    percentage. Growth timing identifies the session/reset epoch; app baselines
    can be newer when identity or coverage changes.
    """

    def rows(lay: Layout) -> tuple[Text, Text, Text, Text]:
        return (
            _ram_row(host, lay, colors, ascii_bars=ascii_bars),
            _compression_row(host, lay, ascii_bars=ascii_bars),
            _swap_row(
                host,
                lay,
                colors,
                (swap_in_rate, swap_out_rate),
                swap_out_session_total,
                ascii_bars=ascii_bars,
            ),
            _pressure_row(
                host, lay, colors, baseline_time, baseline_elapsed, ascii_bars=ascii_bars
            ),
        )

    return rows(choose(width, _slots(host), rows))
