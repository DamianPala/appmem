"""Size and pressure formatting (SPEC.md "Behaviour details" and "Definitions").

Sizes use binary units and a dot decimal separator. GiB gets one decimal;
MiB and KiB are integers; below 1 KiB is shown in bytes.
"""

from __future__ import annotations

_KIB = 1024
_MIB = 1024**2
_GIB = 1024**3


def size(num_bytes: int) -> str:
    """Format a byte count using binary units, e.g. ``16.8 GiB``, ``677 MiB``, ``0 B``."""
    if num_bytes < _KIB:
        return f"{num_bytes} B"
    # Choose the unit by the rounded value, so 1 MiB - 1 B shows `1 MiB`, not `1024 KiB`.
    if (kib := round(num_bytes / _KIB)) < 1024:
        return f"{kib} KiB"
    if (mib := round(num_bytes / _MIB)) < 1024:
        return f"{mib} MiB"
    return f"{num_bytes / _GIB:.1f} GiB"


def pressure_word(some_avg10: float, full_avg10: float) -> str:
    """Format memory pressure as a word (SPEC.md "Definitions").

    ``none`` when ``some avg10`` < 1 %, ``high`` when ``full avg10`` > 5 %,
    otherwise ``some (X.X %)`` using the ``some avg10`` value.
    """
    if some_avg10 < 1:
        return "none"
    if full_avg10 > 5:
        return "high"
    return f"some ({some_avg10:.1f} %)"


def format_delta(num_bytes: int) -> str:
    """Format a Δ byte count: ``+``/``-`` sign and size, or ``0`` for no change."""
    if num_bytes == 0:
        return "0"
    sign = "+" if num_bytes > 0 else "-"
    return f"{sign}{size(abs(num_bytes))}"


def format_elapsed(seconds: float) -> str:
    """Compact elapsed-time string for the Δ baseline header line, e.g. ``5m``, ``1h12m``."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m" if minutes else f"{hours}h"
