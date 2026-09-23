"""Main-view header line text (SPEC.md "Main view", header lines 1 and 2)."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text

from appmem.collect import SystemStats
from appmem.fmt import format_elapsed, format_pair, pressure_word, size
from appmem.ui.layout import fit_line

_ELSEWHERE_THRESHOLD = 1024 * 1024

# Main header drop order when the line doesn't fit -- elsewhere first,
# then system, then avail; RAM, Swap and pressure are never dropped.
_DROP_ORDER = ("elsewhere", "system", "avail")

_PRESSURE_COLOR = {"none": "green", "some": "yellow", "high": "red"}


def _pressure_part(stats: SystemStats) -> Text | None:
    if (
        stats.pressure_some_avg10 is None
        or stats.pressure_some_avg60 is None
        or stats.pressure_full_avg10 is None
    ):
        return None
    word = pressure_word(
        stats.pressure_some_avg10, stats.pressure_some_avg60, stats.pressure_full_avg10
    )
    # Bold + coloured pressure word only (none green, some yellow, high
    # red); any "(some X % ...)" qualifier after it stays plain. The word
    # itself never changes with color (NO_COLOR strips the style, not the
    # text), so this alone already says what pressure means in words.
    head, _, rest = word.partition(" ")
    styled = Text(head, style=f"bold {_PRESSURE_COLOR[head]}")
    if rest:
        styled = styled + Text(f" {rest}")
    return Text("pressure 10s: ") + styled


def _swap_style(used: int, total: int) -> str | None:
    fraction = used / total
    if fraction > 0.8:
        return "red"
    if fraction > 0.5:
        return "yellow"
    return None


def _swap_part(stats: SystemStats) -> Text:
    if stats.swap_total == 0:
        return Text("Swap off")
    used = stats.swap_total - stats.swap_free
    pair = format_pair(used, stats.swap_total)
    # Swap used/total coloured yellow > 50 %, red > 80 % of total.
    style = _swap_style(used, stats.swap_total)
    return Text("Swap ") + Text(pair, style=style or "")


def format_line1(stats: SystemStats, width: int) -> Text:
    """RAM/avail, swap, pressure (when readable), the hidden system.slice total
    and memory charged outside the walked trees.

    Never wraps: built from parts with a priority, dropping the lowest below
    `width` and recomputed on every resize (SPEC.md "Main-view polish").
    Used/total share a unit when they render to the same one
    (`format_pair`), and the pressure label drops the redundant "memory" word.

    The system.slice total is shown regardless of whether system rows are
    currently displayed (`x`): it is a summary hint, not a duplicate of the
    table. `SystemStats.system_ram`/`system_swap` carry no process count (the
    collector skips that costly recursive count for system.slice), so this line
    never renders a PROCS-like number for it.

    `elsewhere` (SPEC.md "Behaviour details") follows `system`: memory charged
    outside the walked trees (other users, VMs, containers), omitted below
    1 MiB or when the root `memory.stat` couldn't be read.
    """
    ram_used = stats.mem_total - stats.mem_available
    ram_part = Text(f"RAM {format_pair(ram_used, stats.mem_total)}")
    avail_part = Text(f"avail {size(stats.mem_available)}")
    system_total = stats.system_ram + stats.system_swap
    system_part = Text(f"system {size(system_total)} [x]")
    elsewhere_part = None
    if stats.elsewhere is not None and stats.elsewhere >= _ELSEWHERE_THRESHOLD:
        elsewhere_part = Text(f"elsewhere {size(stats.elsewhere)}")

    parts: list[tuple[str, Text | None]] = [
        ("ram", ram_part),
        ("avail", avail_part),
        ("swap", _swap_part(stats)),
        ("pressure", _pressure_part(stats)),
        ("system", system_part),
        ("elsewhere", elsewhere_part),
    ]
    return fit_line(parts, _DROP_ORDER, width)


def format_line2(baseline_time: datetime, now: datetime) -> str:
    """`Δ since HH:MM (<elapsed>)`."""
    elapsed = format_elapsed((now - baseline_time).total_seconds())
    return f"Δ since {baseline_time:%H:%M} ({elapsed})"
