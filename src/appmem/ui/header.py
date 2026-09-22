"""Main-view header line text (SPEC.md "Main view", header lines 1 and 2)."""

from __future__ import annotations

from datetime import datetime

from appmem.collect import SystemStats
from appmem.fmt import format_elapsed, pressure_word, size


def format_line1(stats: SystemStats) -> str:
    """RAM/avail, swap, pressure (when readable) and the hidden system.slice total.

    The system.slice total is shown regardless of whether system rows are
    currently displayed (`x`): it is a summary hint, not a duplicate of the
    table. `SystemStats.system_ram`/`system_swap` carry no process count (the
    collector skips that costly recursive count for system.slice), so this line
    never renders a PROCS-like number for it.
    """
    ram_used = stats.mem_total - stats.mem_available
    parts = [f"RAM {size(ram_used)} / {size(stats.mem_total)}  avail {size(stats.mem_available)}"]
    if stats.swap_total == 0:
        parts.append("Swap off")
    else:
        swap_used = stats.swap_total - stats.swap_free
        parts.append(f"Swap {size(swap_used)} / {size(stats.swap_total)}")
    if stats.pressure_some_avg10 is not None and stats.pressure_full_avg10 is not None:
        word = pressure_word(stats.pressure_some_avg10, stats.pressure_full_avg10)
        parts.append(f"memory pressure: {word}")
    system_total = stats.system_ram + stats.system_swap
    parts.append(f"system {size(system_total)} (x)")
    return "   ".join(parts)


def format_line2(baseline_time: datetime, now: datetime) -> str:
    """`Δ since HH:MM (<elapsed>)`."""
    elapsed = format_elapsed((now - baseline_time).total_seconds())
    return f"Δ since {baseline_time:%H:%M} ({elapsed})"
