"""Non-wrapping line layout, shared by the main header and the process-view
title/status line/footers (SPEC.md "Main view", "Process view"; final review
F2/F8/5.1/5.3).

Pure, no Textual imports (only `rich.text.Text`, which the screens already
build cells and titles from), so the priority-drop and key-cap logic is
unit-testable without a running app.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text


def fit_line(
    parts: Sequence[tuple[str, Text | None]],
    drop_order: Sequence[str],
    width: int,
    *,
    separator: str = "   ",
) -> Text:
    """Join `parts` (`(key, text)`, in display order) with `separator`,
    dropping the keys named in `drop_order` -- lowest priority first -- until
    the joined line's cell width fits `width` columns.

    A part whose text is `None` is already absent. A key not listed in
    `drop_order` is never dropped, so the result can still overflow `width`
    once every droppable part is gone (SPEC.md "Main view": RAM, Swap and
    pressure are always kept).
    """
    sep = Text(separator)
    order = [key for key, text in parts if text is not None]
    kept = {key: text for key, text in parts if text is not None}

    def joined() -> Text:
        return sep.join(kept[key] for key in order)

    for key in drop_order:
        if joined().cell_len <= width:
            break
        if key in kept:
            del kept[key]
            order.remove(key)
    result = joined()
    # Belt and braces: even the never-dropped parts could still overflow an
    # extreme width. Never wrap; crop with an ellipsis instead (SPEC.md "Main
    # view", "Process view"; final review F2/5.1: the line must never wrap).
    result.no_wrap = True
    result.overflow = "ellipsis"
    return result


def build_footer(
    items: Sequence[tuple[Sequence[str], str]],
    *,
    width: int | None = None,
    drop_order: Sequence[str] = (),
) -> Text:
    """Build a footer line with reverse-video key caps (SPEC.md "Main view",
    "Process view", "Help screen"; final review F8/5.3, review round 1 open
    item 3): each key its own styled span, a plain space, then the action
    label; items separated by two spaces, with a leading space before the
    first.

    With `width` given, the footer never wraps: items named in `drop_order`
    by their *label* (the action text, not the key caps -- `("z",), "reset
    Δ"` is dropped as `"reset Δ"`) -- lowest priority first -- are dropped
    until the line fits, the same idea as `fit_line`. Without `width` (the
    default) every item is always kept, unchanged from before this line
    existed.
    """
    parts = [
        (label, Text(" ").join(Text(key, style="reverse") for key in keys) + Text(f" {label}"))
        for keys, label in items
    ]
    if width is None:
        return Text(" ") + Text("  ").join(text for _label, text in parts)
    line = fit_line(parts, drop_order, max(width - 1, 0), separator="  ")
    result = Text(" ") + line
    result.no_wrap = True
    result.overflow = "ellipsis"
    return result
