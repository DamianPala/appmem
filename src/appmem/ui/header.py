"""Main-view header line text (SPEC.md "Main view", header lines 1 and 2)."""

from __future__ import annotations

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
)

_ELSEWHERE_THRESHOLD = 1024 * 1024

# Main header drop order when the line doesn't fit. `shared` goes last of the
# droppable parts (RAM/Swap/pressure never drop), so it stays visible at
# typical widths even once the bare `avail` figure is gone. `zswap` (the "X
# zswap in Y" bracket) outlives avail's breakdown: it is a separate fact, while
# the breakdown only explains a figure that stays on screen, so the bracket
# fits at 140-160 columns. The `wb` writeback token is never in this list --
# it's short and only shows up when something is actually wrong.
_DROP_STEPS = ("elsewhere", "system", "avail_breakdown", "zswap", "avail", "shared")


@dataclass(frozen=True)
class ThemeColors:
    """The three theme colours the header borrows (SPEC.md "Main view":
    "every colour appmem sets follows the theme"), read once per render from
    `App.current_theme` by the caller. Kept as plain hex strings here rather
    than importing `textual.theme.Theme`, so this module stays pure and
    testable without a running app."""

    success: str
    warning: str
    error: str


def _pressure_part(stats: SystemStats, colors: ThemeColors) -> Text | None:
    if (
        stats.pressure_some_avg10 is None
        or stats.pressure_some_avg60 is None
        or stats.pressure_full_avg10 is None
    ):
        return None
    word = pressure_word(
        stats.pressure_some_avg10, stats.pressure_some_avg60, stats.pressure_full_avg10
    )
    # Bold + coloured pressure word only (none success, some warning, high
    # error, from the active theme); any "(some X % ...)" qualifier after it
    # stays plain. The word itself never changes with color (NO_COLOR strips
    # the style, not the text), so this alone already says what pressure
    # means in words.
    pressure_color = {"none": colors.success, "some": colors.warning, "high": colors.error}
    head, _, rest = word.partition(" ")
    styled = Text(head, style=f"bold {pressure_color[head]}")
    if rest:
        styled = styled + Text(f" {rest}")
    return Text("pressure 10s: ") + styled


def _swap_style(used: int, total: int, colors: ThemeColors) -> str | None:
    fraction = used / total
    if fraction > 0.8:
        return colors.error
    if fraction > 0.5:
        return colors.warning
    return None


def _zswap_bracket_parts(
    stats: SystemStats, colors: ThemeColors, *, show_zswap: bool, writeback_rate: int | None
) -> list[Text]:
    """The Swap part's trailing `(X zswap in Y, wb N MiB/s)` bracket, both
    halves gated on `stats.zswap_enabled` -- when zswap is off or
    unsupported, neither the pool figures nor a stale writeback rate has
    anything to say (SPEC.md "Main view"). `show_zswap` is the header's own
    width-drop decision for the "X zswap in Y" half only; `wb` never drops
    for width (SPEC.md "Main view": "short... only appears when something is
    wrong")."""
    if not stats.zswap_enabled:
        return []
    parts: list[Text] = []
    if show_zswap and stats.zswapped_bytes is not None and stats.zswap_pool_bytes is not None:
        parts.append(Text(format_zswap_part(stats.zswapped_bytes, stats.zswap_pool_bytes)))
    if writeback_rate is not None and writeback_rate > 0:
        parts.append(Text(f"wb {format_rate(writeback_rate)}", style=colors.warning))
    return parts


def _swap_part(
    stats: SystemStats,
    colors: ThemeColors,
    *,
    show_zswap: bool = True,
    writeback_rate: int | None = None,
) -> Text:
    if stats.swap_total == 0:
        return Text("Swap off")
    used = stats.swap_total - stats.swap_free
    pair = format_pair(used, stats.swap_total)
    # Swap used/total coloured theme warning > 50 %, theme error > 80 %.
    style = _swap_style(used, stats.swap_total, colors)
    text = Text("Swap ") + Text(pair, style=style or "")
    bracket_parts = _zswap_bracket_parts(
        stats, colors, show_zswap=show_zswap, writeback_rate=writeback_rate
    )
    if bracket_parts:
        text = text + Text(" (") + Text(", ").join(bracket_parts) + Text(")")
    return text


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


def _assemble_line1(
    stats: SystemStats, disabled: frozenset[str], colors: ThemeColors, writeback_rate: int | None
) -> Text:
    parts: list[Text] = [_ram_part(stats, show_shared="shared" not in disabled)]
    if "avail" not in disabled:
        parts.append(_avail_part(stats, show_breakdown="avail_breakdown" not in disabled))
    parts.append(
        _swap_part(stats, colors, show_zswap="zswap" not in disabled, writeback_rate=writeback_rate)
    )
    pressure_part = _pressure_part(stats, colors)
    if pressure_part is not None:
        parts.append(pressure_part)
    if "system" not in disabled:
        system_total = stats.system_ram + stats.system_swap
        parts.append(Text(f"system {size(system_total)} [x]"))
    elsewhere = stats.elsewhere
    if "elsewhere" not in disabled and elsewhere is not None and elsewhere >= _ELSEWHERE_THRESHOLD:
        parts.append(Text(f"elsewhere {size(elsewhere)}"))
    return Text("  ").join(parts)


def format_line1(
    stats: SystemStats, width: int, colors: ThemeColors, writeback_rate: int | None = None
) -> Text:
    """RAM (used/total, with a shared-memory breakdown), avail (with a
    free/cache/slab breakdown), swap (with zswap, when enabled), pressure
    (when readable), the hidden system.slice total and memory charged
    outside the walked trees.

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
    own breakdown, the zswap `(X zswap in Y)` bracket, `avail` itself,
    then RAM's `shared` last (`_DROP_STEPS`) -- `shared` is the stickiest
    droppable part, so it stays visible at typical widths even once `avail`
    is gone entirely. RAM, Swap and pressure never drop.

    Dropping only ever removes whole parts, so it can overshoot: a part
    dropped early can free more room than it needed once a later, bigger
    part also drops. After the drop loop settles, a refill pass tries to put
    dropped parts back, most important first (`_DROP_STEPS` in reverse),
    keeping each one that still fits. This can only ever re-show parts the
    plain drop loop would have hidden; it never changes which width first
    needs a drop at all.

    When `stats.zswap_enabled`, the Swap part grows a trailing bracket:
    `(X zswap in Y)` (`format_zswap_part`), `X` = data held compressed in
    the pool (already part of Swap used), `Y` = the RAM the pool costs
    (already part of RAM used). While `writeback_rate` (bytes/sec, computed
    tick to tick by the caller -- a one-shot read has no rate of its own) is
    positive, a `wb N MiB/s` token joins it, styled in the theme's warning
    colour: the pool is overflowing to the slower disk swap. `wb` never
    drops for width and disappears on its own once the rate goes back to 0;
    when zswap is disabled or unsupported, neither ever appears.

    The system.slice total is shown regardless of whether system rows are
    currently displayed (`x`): it is a summary hint, not a duplicate of the
    table. `SystemStats.system_ram`/`system_swap` carry no process count (the
    collector skips that costly recursive count for system.slice), so this line
    never renders a PROCS-like number for it.

    `elsewhere` (SPEC.md "Behaviour details") follows `system`: memory charged
    outside the walked trees (other users, VMs, containers), omitted below
    1 MiB or when the root `memory.stat` couldn't be read.

    `colors` supplies the active theme's success/warning/error colours for
    the pressure word and the swap fraction (SPEC.md "Main view": "every
    colour appmem sets follows the theme"); the caller reads them from
    `App.current_theme` once per render.
    """
    disabled: set[str] = set()
    text = _assemble_line1(stats, frozenset(disabled), colors, writeback_rate)
    while text.cell_len > width:
        remaining = [step for step in _DROP_STEPS if step not in disabled]
        if not remaining:
            break
        disabled.add(remaining[0])
        text = _assemble_line1(stats, frozenset(disabled), colors, writeback_rate)
    # Refill: dropping whole parts can overshoot (a big later drop frees more
    # room than a small earlier one needed). Offer dropped parts back, most
    # important first, and keep whichever still fit.
    for step in reversed(_DROP_STEPS):
        if step not in disabled:
            continue
        candidate = _assemble_line1(stats, frozenset(disabled - {step}), colors, writeback_rate)
        if candidate.cell_len <= width:
            disabled.discard(step)
            text = candidate
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
