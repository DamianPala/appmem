"""Size and pressure formatting (SPEC.md "Behaviour details" and "Definitions").

Sizes use binary units and a dot decimal separator. GiB gets one decimal;
MiB and KiB are integers; below 1 KiB is shown in bytes.
"""

from __future__ import annotations

from rich.cells import cell_len, set_cell_size

_KIB = 1024
_MIB = 1024**2
_GIB = 1024**3


def _unit_for(num_bytes: int) -> str:
    """The unit `size()` would pick for `num_bytes`, on its own so a
    breakdown figure can be forced into another number's unit (`unit_of`,
    `size_in_unit`) instead of always picking its own."""
    if num_bytes < _KIB:
        return "B"
    # Choose the unit by the rounded value, so 1 MiB - 1 B shows `1 MiB`, not `1024 KiB`.
    if round(num_bytes / _KIB) < 1024:
        return "KiB"
    if round(num_bytes / _MIB) < 1024:
        return "MiB"
    return "GiB"


def _format_as(num_bytes: int, unit: str) -> str:
    """`num_bytes` forced into `unit`, with that unit's own precision (GiB
    keeps one decimal, everything else is a rounded integer)."""
    if unit == "B":
        return f"{num_bytes} B"
    if unit == "KiB":
        return f"{round(num_bytes / _KIB)} KiB"
    if unit == "MiB":
        return f"{round(num_bytes / _MIB)} MiB"
    return f"{num_bytes / _GIB:.1f} GiB"


def size(num_bytes: int) -> str:
    """Format a byte count using binary units, e.g. ``16.8 GiB``, ``677 MiB``, ``0 B``."""
    return _format_as(num_bytes, _unit_for(num_bytes))


def unit_of(num_bytes: int) -> str:
    """The unit `size(num_bytes)` renders in, e.g. ``"GiB"``. Lets a caller
    force a breakdown figure into another number's unit instead of its own
    (the main-view header's "a breakdown inherits its parent's unit, instead
    of repeating it" rule, SPEC.md "Main view")."""
    return _unit_for(num_bytes)


def size_in_unit(num_bytes: int, unit: str) -> str:
    """`num_bytes` formatted in a caller-chosen `unit` (from `unit_of`),
    without the unit suffix -- the number half of a breakdown figure that
    inherits its parent's unit instead of repeating it."""
    formatted = _format_as(num_bytes, unit)
    num, _, _ = formatted.rpartition(" ")
    return num


def pressure_word(some_avg10: float, some_avg60: float, full_avg10: float) -> str:
    """Format memory pressure as a word, over the last 10 s (SPEC.md "Definitions").

    ``high`` when ``full avg10`` > 5 % or ``some avg10`` > 20 % (a lot of tasks
    waiting, even without a full stall yet). ``some (X.X %)`` when ``some
    avg10`` >= 1 %. Otherwise ``none``, or ``none (was X.X %)`` when ``some
    avg60`` > 1 % -- no waiting right now, but there was some over the last
    minute.
    """
    if full_avg10 > 5 or some_avg10 > 20:
        return "high"
    if some_avg10 >= 1:
        return f"some ({some_avg10:.1f} %)"
    if some_avg60 > 1:
        # Capped so the text never outgrows the header's fixed pressure slot.
        return f"none (was {min(some_avg60, 99.9):.1f} %)"
    return "none"


def format_delta(num_bytes: int) -> str:
    """Format a Δ byte count: ``+``/``-`` sign and size, or ``·`` when
    ``|num_bytes|`` is under 1 MiB -- noise at KiB granularity in the first
    minutes (SPEC.md "Main view")."""
    if abs(num_bytes) < _MIB:
        return "·"
    sign = "+" if num_bytes > 0 else "-"
    return f"{sign}{size(abs(num_bytes))}"


def format_elapsed(seconds: float) -> str:
    """Compact elapsed-time string for the Δ baseline header line, e.g. ``5m``, ``1h12m``.

    From 100 h on, whole days only (``4d``, ``120d``), so the text never
    outgrows the header's 6-cell elapsed slot (``99h59m``).
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours >= 100:
        return f"{hours // 24}d"
    return f"{hours}h{minutes}m" if minutes else f"{hours}h"


def format_age(seconds: float) -> str:
    """Compact process age using only its largest unit (SPEC.md "Process view").

    Unlike `format_elapsed`, never compounds units: ``45s``, ``12m``, ``2h``,
    ``86d``.
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = total // 3600
    if hours < 24:
        return f"{hours}h"
    return f"{total // 86400}d"


def format_rate(bytes_per_second: int) -> str:
    """Format a byte rate, e.g. ``12 MiB/s`` (zswap writeback, SPEC.md "Main view")."""
    return f"{size(bytes_per_second)}/s"


def format_zswap_part(
    zswapped_bytes: int, zswap_pool_bytes: int, unit: str, *, short: bool = False
) -> str:
    """``X zswapped into Y RAM``, short form ``X zswapped`` (SPEC.md "Main
    view", main-view header). `X` (swapped data kept
    compressed in RAM, already part of Swap used) is given in `unit` -- the
    Swap pair's own unit (`unit_of`) -- without repeating it, the same rule
    as every other header breakdown. `Y` (the RAM the pool costs, already
    part of RAM used) is a separate quantity charged against RAM rather than
    Swap, so it keeps its own unit. Shared by the live header and the
    `snapshot` text report, so the Swap part reads the same wherever it's
    rendered.
    """
    zswapped = size_in_unit(zswapped_bytes, unit)
    if short:
        return f"{zswapped} zswapped"
    return f"{zswapped} zswapped into {size(zswap_pool_bytes)} RAM"


def format_pair(used: int, total: int) -> str:
    """Format a used/total byte pair, always in the total's unit.

    ``18.7/30.9 GiB``, or ``0.5/1.0 GiB`` when `used` alone would otherwise
    pick a smaller unit (SPEC.md main-view header line 1: never a mixed
    ``512 MiB / 1.0 GiB``).
    """
    unit = _unit_for(total)
    used_num = size_in_unit(used, unit)
    total_num = size_in_unit(total, unit)
    return f"{used_num}/{total_num} {unit}"


def truncate_name(name: str, cap: int = 32) -> str:
    """Cap a display name at `cap` terminal cells, ending a cut name with ``…``.

    Counts cells (`rich.cells.cell_len`), not code points: a name made of
    wide (CJK, fullwidth) characters would otherwise cost up to twice `cap`
    columns on screen. Cropping (`rich.cells.set_cell_size`) never splits a
    wide character in half -- one that would straddle the cut becomes a
    space instead, so a name is at most `cap` cells including the ellipsis.

    Used for the main view's APP column and the process view's NAME column
    (SPEC.md "Behaviour details": "APP column ... cap at 32 characters").
    """
    if cell_len(name) <= cap:
        return name
    return set_cell_size(name, cap - 1) + "…"


def ellipsize_middle(text: str, max_len: int) -> str:
    """Shorten `text` to `max_len` characters by cutting its middle, e.g.
    ``ellipsize_middle("abcdefgh", 5)`` -> ``"ab…gh"``.

    Used for the process-view status line's unit name, which is otherwise
    never truncated: only when the whole line is wider than the terminal
    (SPEC.md "Process view").
    """
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return "…"[:max_len]
    keep = max_len - 1
    left = (keep + 1) // 2
    right = keep - left
    return text[:left] + "…" + (text[len(text) - right :] if right else "")


def status_line_command(scope: str, unit: str, pid: int | None, width: int) -> str:
    """Build the process-view status line for a real unit/process row: the
    ready-made stop command for its unit, plus `kill PID` when the selected
    row is an actual process (SPEC.md "Process view").

    The unit name is never truncated unless the whole line would be wider
    than `width`, in which case only the unit name is ellipsized in the
    middle -- the command stays intact and copyable either side of it.
    """
    prefix = "systemctl --user stop" if scope == "user" else "sudo systemctl stop"
    suffix = f"   kill {pid}" if pid is not None else ""
    overhead = len(prefix) + 3 + len(suffix)  # `" '"` + the closing `"'"`
    if overhead + len(unit) <= width:
        return f"{prefix} '{unit}'{suffix}"
    return f"{prefix} '{ellipsize_middle(unit, max(width - overhead, 1))}'{suffix}"
