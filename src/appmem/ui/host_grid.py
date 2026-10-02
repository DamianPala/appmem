"""Fixed terminal geometry for host labels, gauges, values and metadata."""

from __future__ import annotations

from rich.text import Text


def geometry(width: int) -> tuple[int, int, int]:
    """Slots depend only on terminal width, never on sampled counter magnitudes."""
    gauge = 20 if width >= 110 else 10 if width >= 80 else 6 if width >= 60 else 0
    return 10, gauge, 20 if width >= 60 else 26


def grid_row(
    label: str,
    value: Text,
    width: int,
    *,
    gauge: Text | None = None,
    metadata: tuple[tuple[int, Text], ...] = (),
    ascii_bars: bool = False,
    hidden: bool = False,
) -> Text:
    label_width, gauge_width, value_width = geometry(width)
    text = Text(label.ljust(label_width))
    if not gauge_width and gauge is not None and gauge.cell_len:
        value = gauge + Text(" ") + value
    if gauge_width:
        state = (gauge or Text()).copy()
        hidden = hidden or state.cell_len > gauge_width
        state.truncate(gauge_width)
        text += state + Text(" " * max(0, gauge_width - state.cell_len) + "  ")
    primary = value.copy()
    hidden = hidden or primary.cell_len > value_width
    primary.truncate(value_width, overflow="crop")
    text += primary + Text(" " * max(0, value_width - primary.cell_len))
    fields, omitted = _metadata(metadata, width - text.cell_len - 2)
    hidden = hidden or omitted
    if fields:
        text += Text("| " if ascii_bars else "│ ", style="dim")
        text += Text(" ").join(fields)
    if text.cell_len > width - int(hidden):
        hidden = True
        text.truncate(max(0, width - 1))
    if hidden:
        text += Text(" " * max(0, width - 1 - text.cell_len))
        text += Text(">" if ascii_bars else "…", style="dim")
    text.no_wrap = True
    text.overflow = "crop"
    return text


def _metadata(entries: tuple[tuple[int, Text], ...], available: int) -> tuple[list[Text], bool]:
    fields: list[Text] = []
    omitted = False
    for slot, token in entries:
        budget = slot + (1 if fields else 0)
        if budget > available:
            omitted = True
            continue
        fields.append(token)
        available -= budget
    return fields, omitted
