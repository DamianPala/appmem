"""Main-view header line text (SPEC.md "Main view", header lines 1 and 2)."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text

from appmem.collect import SystemStats
from appmem.fmt import format_elapsed, format_pair, pressure_word, size

_ELSEWHERE_THRESHOLD = 1024 * 1024

# Main header drop order when the line doesn't fit. `shared` goes last of the
# droppable parts (RAM/Swap/pressure never drop), so it stays visible at
# typical widths even once the bare `avail` figure is gone.
_DROP_STEPS = ("elsewhere", "system", "avail_breakdown", "avail", "shared")

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


def _ram_part(stats: SystemStats, *, show_shared: bool) -> Text:
    ram_used = stats.mem_total - stats.mem_available
    text = Text(f"RAM {format_pair(ram_used, stats.mem_total)}")
    if show_shared:
        text = text + Text(f" ({size(stats.mem_shared)} shared)")
    return text


def _avail_part(stats: SystemStats, *, show_breakdown: bool) -> Text:
    text = Text(f"avail {size(stats.mem_available)}")
    if show_breakdown:
        text = text + Text(
            f" ({size(stats.mem_free)} free, {size(stats.mem_cache)} cache, "
            f"{size(stats.mem_slab)} slab)"
        )
    return text


def _assemble_line1(stats: SystemStats, disabled: frozenset[str]) -> Text:
    parts: list[Text] = [_ram_part(stats, show_shared="shared" not in disabled)]
    if "avail" not in disabled:
        parts.append(_avail_part(stats, show_breakdown="avail_breakdown" not in disabled))
    parts.append(_swap_part(stats))
    pressure_part = _pressure_part(stats)
    if pressure_part is not None:
        parts.append(pressure_part)
    if "system" not in disabled:
        system_total = stats.system_ram + stats.system_swap
        parts.append(Text(f"system {size(system_total)} [x]"))
    elsewhere = stats.elsewhere
    if "elsewhere" not in disabled and elsewhere is not None and elsewhere >= _ELSEWHERE_THRESHOLD:
        parts.append(Text(f"elsewhere {size(elsewhere)}"))
    return Text("  ").join(parts)


def format_line1(stats: SystemStats, width: int) -> Text:
    """RAM (used/total, with a shared-memory breakdown), avail (with a
    free/cache/slab breakdown), swap, pressure (when readable), the hidden
    system.slice total and memory charged outside the walked trees.

    Never wraps: built from parts with a priority, dropping the lowest below
    `width` and recomputed on every resize (SPEC.md "Main view"). Parts are
    joined with two spaces. Used/total share a unit when they render to the
    same one (`format_pair`), and the pressure label drops the redundant
    "memory" word.

    `shared` (tmpfs, shared memory, GPU buffers -- swappable but not
    reclaimable) is always shown, no threshold. `avail`'s own breakdown into
    `free` (truly free), `cache` (reclaimable page cache) and `slab`
    (reclaimable kernel caches, `SReclaimable`) sits inside it, the same
    idea: parts grouped so they sit inside the totals they belong to. The
    three parts don't sum exactly to `avail` (a kernel estimate that also
    reserves some headroom), only come close to it.
    Below `width`, parts drop in this order: `elsewhere`, `system`, avail's
    own breakdown, `avail` itself, then RAM's `shared` last (`_DROP_STEPS`)
    -- `shared` is the stickiest droppable part, so it
    stays visible at typical widths even once `avail` is gone entirely. RAM,
    Swap and pressure never drop.

    The system.slice total is shown regardless of whether system rows are
    currently displayed (`x`): it is a summary hint, not a duplicate of the
    table. `SystemStats.system_ram`/`system_swap` carry no process count (the
    collector skips that costly recursive count for system.slice), so this line
    never renders a PROCS-like number for it.

    `elsewhere` (SPEC.md "Behaviour details") follows `system`: memory charged
    outside the walked trees (other users, VMs, containers), omitted below
    1 MiB or when the root `memory.stat` couldn't be read.
    """
    disabled: set[str] = set()
    text = _assemble_line1(stats, frozenset(disabled))
    while text.cell_len > width:
        remaining = [step for step in _DROP_STEPS if step not in disabled]
        if not remaining:
            break
        disabled.add(remaining[0])
        text = _assemble_line1(stats, frozenset(disabled))
    # Belt and braces: even the never-dropped parts could still overflow an
    # extreme width. Never wrap; crop with an ellipsis instead (SPEC.md "Main
    # view": the line must never wrap).
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def format_line2(baseline_time: datetime, now: datetime) -> str:
    """`Δ since HH:MM (<elapsed>)`."""
    elapsed = format_elapsed((now - baseline_time).total_seconds())
    return f"Δ since {baseline_time:%H:%M} ({elapsed})"
