"""Tests for `appmem.ui.layout` (SPEC.md "Main view", "Process view"):
non-wrapping priority-drop lines and key-cap footers.
"""

from __future__ import annotations

from rich.text import Text

from appmem.ui.layout import build_footer, fit_line


def _parts(**texts: str | None) -> list[tuple[str, Text | None]]:
    return [(key, Text(text) if text is not None else None) for key, text in texts.items()]


def test_fit_line_keeps_everything_when_it_fits() -> None:
    parts = _parts(a="AAAA", b="BBBB", c="CCCC")
    line = fit_line(parts, drop_order=["c", "b"], width=200)

    assert line.plain == "AAAA   BBBB   CCCC"


def test_fit_line_drops_lowest_priority_first() -> None:
    parts = _parts(a="keep", b="drop-me-first", c="drop-me-second")
    width = len("keep   drop-me-second")  # fits with "b" dropped, not with both present

    line = fit_line(parts, drop_order=["b", "c"], width=width)

    assert line.plain == "keep   drop-me-second"


def test_fit_line_drops_multiple_parts_in_order() -> None:
    parts = _parts(a="keep", b="bbbbbbbbbb", c="cccccccccc")

    line = fit_line(parts, drop_order=["b", "c"], width=len("keep"))

    assert line.plain == "keep"


def test_fit_line_never_drops_a_key_outside_drop_order() -> None:
    parts = _parts(a="AAAAAAAAAA", b="BBBBBBBBBB")

    line = fit_line(parts, drop_order=["b"], width=1)

    # "b" is droppable and gone; "a" has nowhere listed to go, so it stays
    # even though the line still doesn't fit 1 column.
    assert line.plain == "AAAAAAAAAA"


def test_fit_line_skips_parts_already_none() -> None:
    parts = _parts(a="AAAA", b=None, c="CCCC")

    line = fit_line(parts, drop_order=["c"], width=200)

    assert line.plain == "AAAA   CCCC"


def test_fit_line_never_wraps() -> None:
    parts = _parts(a="AAAAAAAAAA")
    line = fit_line(parts, drop_order=[], width=200)

    assert line.no_wrap is True


def test_build_footer_plain_text_matches_main_view_footer() -> None:
    items: list[tuple[tuple[str, ...], str]] = [
        (("s", "r", "t", "d"), "sort"),
        (("enter",), "procs"),
        (("x",), "system"),
        (("c",), "cache"),
        (("z",), "reset Δ"),
        (("?",), "help"),
        (("q",), "quit"),
    ]

    footer = build_footer(items)

    assert (
        footer.plain == " s r t d sort  enter procs  x system  c cache  z reset Δ  ? help  q quit"
    )


def test_build_footer_styles_each_key_as_reverse_video() -> None:
    footer = build_footer([(("s", "r"), "sort")])

    s_index = footer.plain.index("s")
    r_index = footer.plain.index("r")
    styles_at_s = [span.style for span in footer.spans if span.start <= s_index < span.end]
    styles_at_r = [span.style for span in footer.spans if span.start <= r_index < span.end]

    assert any("reverse" in str(style) for style in styles_at_s)
    assert any("reverse" in str(style) for style in styles_at_r)


# --- footers never wrap ----------------------------------------------------------


def test_build_footer_without_width_keeps_every_item_unchanged() -> None:
    items: list[tuple[tuple[str, ...], str]] = [(("a",), "aaa"), (("b",), "bbb")]

    footer = build_footer(items)

    assert footer.plain == " a aaa  b bbb"


def test_build_footer_keeps_everything_when_it_fits_at_width() -> None:
    items: list[tuple[tuple[str, ...], str]] = [(("a",), "aaa"), (("b",), "bbb")]

    # drop_order names labels, not key caps -- items are identified the same
    # way `fit_line` identifies parts.
    footer = build_footer(items, width=200, drop_order=["bbb", "aaa"])

    assert footer.plain == " a aaa  b bbb"


def test_build_footer_drops_lowest_priority_item_first_at_width() -> None:
    items: list[tuple[tuple[str, ...], str]] = [(("a",), "aaa"), (("b",), "bbb")]

    footer = build_footer(items, width=len(" a aaa"), drop_order=["bbb", "aaa"])

    assert footer.plain == " a aaa"
    assert "bbb" not in footer.plain


def test_build_footer_never_drops_a_label_outside_drop_order() -> None:
    items: list[tuple[tuple[str, ...], str]] = [(("h",), "help"), (("q",), "quit")]

    footer = build_footer(items, width=1, drop_order=["help"])

    assert "quit" in footer.plain  # not in drop_order: kept even though 1 column never fits


def test_build_footer_at_width_never_wraps() -> None:
    footer = build_footer([(("a",), "aaa")], width=200, drop_order=[])

    assert footer.no_wrap is True
