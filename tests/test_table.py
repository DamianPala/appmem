"""Tests for `RowTable` (SPEC.md "Tech", `appmem.ui.table`).

A minimal `App` hosting a bare `RowTable` with fixture rows -- no live
`/sys` or `/proc`, no `AppMemApp` needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.message import Message
from textual.screen import Screen

from appmem.ui.table import RowTable, RowTableError, _fit  # pyright: ignore[reportPrivateUsage]

_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("a", "A", 6),
    ("b", "B", 6),
    ("c", "C", 6),
)
_ROW_COUNT = 40


class _TableApp(App[None]):
    """Hosts one `RowTable`; every `RowTable` message it posts is recorded in
    `events`, in arrival order, so a test can assert on the exact sequence."""

    def __init__(
        self,
        *,
        columns: tuple[tuple[str, str, int | None], ...] = _COLUMNS,
        row_count: int = _ROW_COUNT,
    ) -> None:
        super().__init__()
        self._columns = columns
        self._row_count = row_count
        self.events: list[Message] = []

    def compose(self) -> ComposeResult:
        table = RowTable(id="table")
        for key, label, width in self._columns:
            table.add_column(label, width=width, key=key)
        for i in range(self._row_count):
            cells = (Text(f"{key}{i}") for key, _label, _width in self._columns)
            table.add_row(*cells, key=f"r{i}")
        yield table

    def on_row_table_header_selected(self, event: RowTable.HeaderSelected) -> None:
        self.events.append(event)

    def on_row_table_row_selected(self, event: RowTable.RowSelected) -> None:
        self.events.append(event)

    def on_row_table_row_highlighted(self, event: RowTable.RowHighlighted) -> None:
        self.events.append(event)


def _table(app: App[None]) -> RowTable:
    return app.query_one("#table", RowTable)


def _line_text(table: RowTable, y: int) -> str:
    return "".join(segment.text for segment in table.render_line(y))


def _cell(text: str, width: int, *, right: bool = False) -> str:
    """One column's padded cell, matching `RowTable`'s own `" {fit} "`
    format: one space of padding on each side."""
    return f" {_fit(text, width, right=right)} "


@pytest.mark.asyncio
async def test_header_and_rows_render_expected_text() -> None:
    app = _TableApp()
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        # Content only: `render_line` extends the strip to the widget's full
        # width, padding the tail beyond the last column with blanks.
        assert _line_text(table, 0).startswith(_cell("A", 6) + _cell("B", 6) + _cell("C", 6))
        assert _line_text(table, 1).startswith(_cell("a0", 6) + _cell("b0", 6) + _cell("c0", 6))
        assert _line_text(table, 3).startswith(_cell("a2", 6) + _cell("b2", 6) + _cell("c2", 6))


@pytest.mark.asyncio
async def test_right_justify_pads_on_the_left() -> None:
    app = _TableApp(row_count=1)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.update_cell("r0", "b", Text("9", justify="right"))
        expected = _cell("a0", 6) + _cell("9", 6, right=True) + _cell("c0", 6)
        assert _line_text(table, 1).startswith(expected)


@pytest.mark.asyncio
async def test_left_justify_pads_on_the_right() -> None:
    app = _TableApp(row_count=1)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.update_cell("r0", "b", Text("9"))
        expected = _cell("a0", 6) + _cell("9", 6) + _cell("c0", 6)
        assert _line_text(table, 1).startswith(expected)


@pytest.mark.asyncio
async def test_dim_style_is_carried_into_the_rendered_segment() -> None:
    app = _TableApp(row_count=1)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.update_cell("r0", "b", Text("x", style="dim"))
        strip = table.render_line(1)
        cell_segment = next(seg for seg in strip if seg.text.strip() == "x")
        assert cell_segment.style is not None
        assert cell_segment.style.dim is True


@pytest.mark.asyncio
async def test_markup_like_text_renders_literally() -> None:
    """`Text("[bold]x")` (built directly, not via `Text.from_markup`) has no
    spans and must show its brackets literally (SPEC.md "Behaviour
    details")."""
    wide_columns = (("a", "A", 20), ("b", "B", 6), ("c", "C", 6))
    app = _TableApp(columns=wide_columns, row_count=1)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.update_cell("r0", "a", Text("[bold]x"))
        assert "[bold]x" in _line_text(table, 1)


@pytest.mark.asyncio
async def test_update_cell_changes_only_that_cell() -> None:
    app = _TableApp(row_count=3)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.update_cell("r1", "b", Text("changed"))
        assert table.get_cell("r1", "b").plain == "changed"
        assert table.get_cell("r1", "a").plain == "a1"
        assert table.get_cell("r0", "b").plain == "b0"
        assert table.get_cell("r2", "b").plain == "b2"


@pytest.mark.asyncio
async def test_update_cell_refreshes_a_one_line_region() -> None:
    app = _TableApp(row_count=10)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        with patch.object(table, "refresh", wraps=table.refresh) as spy:
            table.update_cell("r2", "b", Text("changed"))
        assert spy.call_count == 1
        (region,) = spy.call_args.args
        assert region.height == 1


@pytest.mark.asyncio
async def test_row_above_the_fold_after_scrolling_keeps_its_updated_text() -> None:
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        for y in range(table.size.height):
            table.render_line(y)  # warm the visible rows' cache
        table.update_cell("r3", "b", Text("ZZ"))
        table.scroll_to(y=20, animate=False)
        await pilot.pause()
        for y in range(table.size.height):
            table.render_line(y)  # scroll the updated row off screen
        table.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert "ZZ" in _line_text(table, 4)  # header (y=0) + row r3 at y=4


@pytest.mark.asyncio
async def test_row_below_the_fold_renders_correctly_once_scrolled_into_view() -> None:
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.update_cell("r35", "b", Text("QQ"))  # never rendered yet (below the fold)
        table.scroll_to(y=30, animate=False)
        await pilot.pause()
        assert "QQ" in _line_text(table, 6)  # header (y=0) + row r35 at y=6 (scroll_y=30)


@pytest.mark.asyncio
async def test_reorder_puts_rows_in_the_given_order_and_keeps_cursor_on_the_same_key() -> None:
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(2)  # cursor on "r2"
        new_order = ["r4", "r3", "r2", "r1", "r0"]
        table.reorder(new_order)
        assert table.row_keys == tuple(new_order)
        assert table.cursor_key == "r2"
        assert table.cursor_row == 2  # r2's new position


@pytest.mark.asyncio
async def test_reorder_is_a_no_op_when_the_order_is_unchanged() -> None:
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        for y in range(table.size.height):
            table.render_line(y)
        strip_before = table._strips.get("r2")  # pyright: ignore[reportPrivateUsage]
        table.reorder(list(table.row_keys))
        assert table._strips.get("r2") is strip_before  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_reorder_rejects_a_set_of_keys_that_does_not_match() -> None:
    app = _TableApp(row_count=3)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        with pytest.raises(RowTableError):
            table.reorder(["r0", "r1"])  # missing r2


@pytest.mark.asyncio
async def test_add_row_and_remove_row() -> None:
    app = _TableApp(row_count=3)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.add_row(Text("aX"), Text("bX"), Text("cX"), key="rX")
        assert table.row_count == 4
        assert table.get_cell("rX", "a").plain == "aX"

        table.remove_row("r1")
        assert table.row_count == 3
        assert "r1" not in table.row_keys


@pytest.mark.asyncio
async def test_remove_row_clamps_cursor_when_the_cursor_row_is_removed() -> None:
    app = _TableApp(row_count=3)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(2)  # last row
        table.remove_row("r2")
        assert table.cursor_row == 1  # clamped to the new last row


@pytest.mark.asyncio
async def test_cursor_up_and_down_move_by_one_row() -> None:
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(2)
        table.action_cursor_down()
        assert table.cursor_row == 3
        table.action_cursor_up()
        table.action_cursor_up()
        assert table.cursor_row == 1


@pytest.mark.asyncio
async def test_home_and_end_move_to_first_and_last_row() -> None:
    app = _TableApp(row_count=10)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(4)
        table.action_scroll_end()
        assert table.cursor_row == 9
        table.action_scroll_home()
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_page_up_and_page_down_move_by_a_screenful() -> None:
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        page = max(1, table.size.height - 1)
        table.action_page_down()
        assert table.cursor_row == page
        table.action_page_up()
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_enter_posts_row_selected_for_the_cursor_row() -> None:
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(2)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        selected = [e for e in app.events if isinstance(e, RowTable.RowSelected)]
        assert [e.row_key for e in selected] == ["r2"]


@pytest.mark.asyncio
async def test_header_click_posts_header_selected_with_the_clicked_column() -> None:
    app = _TableApp()
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        region = table.column_region("b")
        await pilot.click("#table", offset=(region.x + 1, 0))
        await pilot.pause()
        selected = [e for e in app.events if isinstance(e, RowTable.HeaderSelected)]
        assert [e.column_key for e in selected] == ["b"]


@pytest.mark.asyncio
async def test_row_click_moves_the_cursor_and_posts_row_highlighted() -> None:
    app = _TableApp(row_count=10)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        await pilot.click("#table", offset=(1, 4))  # header (y=0) + row r3 at y=4
        await pilot.pause()
        assert table.cursor_key == "r3"
        highlighted = [e for e in app.events if isinstance(e, RowTable.RowHighlighted)]
        assert [e.row_key for e in highlighted] == ["r3"]


@pytest.mark.asyncio
async def test_double_click_on_a_row_posts_row_selected() -> None:
    app = _TableApp(row_count=10)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        await pilot.double_click("#table", offset=(1, 4))
        await pilot.pause()
        selected = [e for e in app.events if isinstance(e, RowTable.RowSelected)]
        assert [e.row_key for e in selected] == ["r3"]
        assert table.cursor_key == "r3"


@pytest.mark.asyncio
async def test_move_cursor_scroll_false_does_not_scroll() -> None:
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(30, scroll=False)
        await pilot.pause()
        assert table.scroll_offset.y == 0


@pytest.mark.asyncio
async def test_move_cursor_scroll_true_scrolls_the_cursor_into_view() -> None:
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(30, scroll=True)
        await pilot.pause()
        assert table.scroll_offset.y > 0


@pytest.mark.asyncio
async def test_cursor_style_applies_only_to_the_cursor_line() -> None:
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(2)
        await pilot.pause()
        cursor_bg = table.get_component_rich_style("rowtable--cursor").bgcolor
        assert cursor_bg is not None
        cursor_line = table.render_line(3)  # header + row r2 at y=3
        other_line = table.render_line(1)  # row r0, not the cursor
        assert any(seg.style is not None and seg.style.bgcolor == cursor_bg for seg in cursor_line)
        assert not any(
            seg.style is not None and seg.style.bgcolor == cursor_bg for seg in other_line
        )


@pytest.mark.asyncio
async def test_unknown_row_key_raises_row_table_error() -> None:
    app = _TableApp(row_count=3)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        with pytest.raises(RowTableError):
            table.update_cell("no-such-row", "a", Text("x"))
        with pytest.raises(RowTableError):
            table.get_cell("no-such-row", "a")
        with pytest.raises(RowTableError):
            table.get_row_index("no-such-row")


@pytest.mark.asyncio
async def test_unknown_column_key_raises_row_table_error() -> None:
    app = _TableApp(row_count=3)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        with pytest.raises(RowTableError):
            table.update_cell("r0", "no-such-column", Text("x"))
        with pytest.raises(RowTableError):
            table.get_cell("r0", "no-such-column")


def test_style_helper_parses_a_string_style() -> None:
    from appmem.ui.table import _cell_style  # pyright: ignore[reportPrivateUsage]

    resolved = _cell_style(Text("x", style="dim italic"))
    assert resolved == Style.parse("dim italic")


@pytest.mark.asyncio
async def test_click_beyond_the_last_column_moves_the_cursor() -> None:
    """The fill segment `render_line` appends past the last column carries
    no `row` meta; a click there must still move the cursor, as on `main`
    (row-table-review-1.md MUST 1)."""
    app = _TableApp(row_count=10)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        x = table.column_region("c").right + 5
        await pilot.click("#table", offset=(x, 4))  # header (y=0) + row r3 at y=4
        await pilot.pause()
        assert table.cursor_key == "r3"


@pytest.mark.asyncio
async def test_double_click_beyond_the_last_column_posts_row_selected() -> None:
    app = _TableApp(row_count=10)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        x = table.column_region("c").right + 5
        await pilot.double_click("#table", offset=(x, 4))
        await pilot.pause()
        selected = [e for e in app.events if isinstance(e, RowTable.RowSelected)]
        assert [e.row_key for e in selected] == ["r3"]


@pytest.mark.asyncio
async def test_click_on_the_header_fill_selects_nothing_while_scrolled() -> None:
    """Scrolled down, `offset.y - 1 + scroll_y` of the header line is a real
    row index, so only the `offset.y >= 1` guard keeps the header's fill
    from selecting a row."""
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.scroll_to(y=10, animate=False)
        await pilot.pause()
        await pilot.click("#table", offset=(table.column_region("c").right + 5, 0))
        await pilot.pause()
        assert table.cursor_row == 0
        assert app.events == []


@pytest.mark.asyncio
async def test_auto_width_grows_on_a_wide_cell_and_shrinks_when_it_goes() -> None:
    columns = (("a", "A", None), ("b", "B", 6))
    app = _TableApp(columns=columns, row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        assert table.column_width("a") == 2  # "a0".."a4"
        table.add_row(Text("x" * 15), Text("b"), key="wide")
        assert table.column_width("a") == 15
        table.remove_row("wide")
        assert table.column_width("a") == 2
        table.update_cell("r0", "a", Text("y" * 12))
        assert table.column_width("a") == 12
        table.update_cell("r0", "a", Text("a0"))
        assert table.column_width("a") == 2


@pytest.mark.asyncio
async def test_reorder_keeps_cached_strip_objects_for_unmoved_rows() -> None:
    """A changed order, not just the no-op case: `reorder` must never
    rebuild a row's cached strip, only remap positions (row-table-review-1.md
    MUST 2, M3)."""
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(0)  # r0 stays the cursor row (never cached)
        for y in range(table.size.height):
            table.render_line(y)  # warm every visible non-cursor row's cache
        strips_before = dict(table._strips)  # pyright: ignore[reportPrivateUsage]
        assert set(strips_before) == {"r1", "r2", "r3", "r4"}
        table.reorder(["r4", "r3", "r2", "r1", "r0"])
        for key, strip in strips_before.items():
            assert table._strips[key] is strip  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_theme_change_drops_cached_strips_and_rerenders_with_new_colours() -> None:
    """A cached `Strip` bakes in resolved colours; nothing else invalidates
    it when the theme changes (row-table-review-1.md MUST 2, M4)."""
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(1)  # keep r0 as a non-cursor row so it gets cached
        for y in range(table.size.height):
            table.render_line(y)
        strip_before = table._strips.get("r0")  # pyright: ignore[reportPrivateUsage]
        assert strip_before is not None
        target = "textual-light" if app.theme != "textual-light" else "textual-dark"
        app.theme = target
        await pilot.pause()
        # A live repaint may have already rebuilt and re-cached the row by
        # the time we check, so identity (not mere presence) is what proves
        # the old, stale-coloured object didn't survive the theme change.
        assert table._strips.get("r0") is not strip_before  # pyright: ignore[reportPrivateUsage]
        row_line = table.render_line(1)  # header (y=0) + row r0 at y=1
        new_bg = table.rich_style.bgcolor
        assert any(seg.style is not None and seg.style.bgcolor == new_bg for seg in row_line)


@pytest.mark.asyncio
async def test_remove_row_drops_the_removed_rows_cached_strip() -> None:
    """An unbounded cache over a long session otherwise (row-table-review-
    1.md MUST 2, M6)."""
    app = _TableApp(row_count=5)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        for y in range(table.size.height):
            table.render_line(y)
        assert "r3" in table._strips  # pyright: ignore[reportPrivateUsage]
        table.remove_row("r3")
        assert "r3" not in table._strips  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_cursor_stays_below_the_header_when_scrolling_up_past_the_top() -> None:
    """Regression for review fix 2 (row-table-review-1.md MUST 3a): the
    fixed header covers virtual line `scroll_offset.y`, so the cursor line
    must always be at least 1 below it."""
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(30)
        await pilot.pause()
        table.focus()
        for _ in range(35):
            await pilot.press("up")
            await pilot.pause()
            assert table.cursor_row + 1 - table.scroll_offset.y >= 1


@pytest.mark.asyncio
async def test_vertical_cursor_move_preserves_horizontal_scroll() -> None:
    """Regression for review fix 2 (row-table-review-1.md MUST 3b): a
    vertical cursor move must never reset a sideways scroll."""
    columns = tuple((f"c{i}", f"C{i}", 8) for i in range(10))
    app = _TableApp(columns=columns, row_count=20)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.scroll_to(x=30, animate=False)
        await pilot.pause()
        table.focus()
        await pilot.press("down")
        await pilot.pause()
        assert table.scroll_offset.x == 30


@pytest.mark.asyncio
async def test_fill_past_the_last_column_uses_the_right_style_per_line() -> None:
    """Regression for review fix 1 (row-table-review-1.md MUST 3c): the
    header and cursor bars fill the full widget width, not just the base
    style every other row uses."""
    app = _TableApp(row_count=5)  # 3 columns of width 6 = 24, well under 40
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        table.move_cursor(1)
        await pilot.pause()
        header_bg = table.get_component_rich_style("rowtable--header").bgcolor
        cursor_bg = (table.rich_style + table.get_component_rich_style("rowtable--cursor")).bgcolor
        base_bg = table.visual_style.rich_style.bgcolor

        header_fill = list(table.render_line(0))[-1]
        cursor_fill = list(table.render_line(2))[-1]  # header + row r1 (cursor) at y=2
        other_fill = list(table.render_line(1))[-1]  # row r0, not the cursor

        assert header_fill.style is not None and header_fill.style.bgcolor == header_bg
        assert cursor_fill.style is not None and cursor_fill.style.bgcolor == cursor_bg
        assert other_fill.style is not None and other_fill.style.bgcolor == base_bg


class _ResizeRestoringScreen(Screen[None]):
    """Restores the cursor once per new size from `on_resize`, as both
    screens' `_sync_columns` rebuild does: the first event for a size
    arrives before the table has that size."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: Size | None = None

    def compose(self) -> ComposeResult:
        table = RowTable(id="table")
        table.add_column("A", width=6, key="a")
        for i in range(40):
            table.add_row(Text(f"a{i}"), key=f"r{i}")
        yield table

    def on_resize(self, event: events.Resize) -> None:
        if event.size == self._seen:
            return
        self._seen = event.size
        table = self.query_one(RowTable)
        table.move_cursor(table.cursor_row, scroll=True)


@pytest.mark.asyncio
async def test_scroll_from_on_resize_keeps_the_cursor_visible_after_a_shrink() -> None:
    """The scroll must be checked again against the new, shorter window."""
    app = App[None]()
    async with app.run_test(size=(40, 20)) as pilot:
        await app.push_screen(_ResizeRestoringScreen())
        await pilot.pause()
        table = app.screen.query_one(RowTable)
        table.move_cursor(17)
        await pilot.pause()
        await pilot.resize_terminal(40, 10)
        await pilot.pause()
        screen_y = table.cursor_row + 1 - table.scroll_offset.y
        assert 1 <= screen_y < table.size.height


@pytest.mark.asyncio
async def test_page_down_from_the_top_scrolls_a_full_page_with_the_cursor() -> None:
    """Regression for review fix 3 (row-table-review-1.md MUST 3d): the
    viewport and the cursor move by a page together, so the cursor lands on
    the first visible row line, not the last."""
    app = _TableApp(row_count=40)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        page = max(1, table.size.height - 1)
        table.action_page_down()
        await pilot.pause()
        assert table.scroll_offset.y == page
        assert table.cursor_row == page
        assert table.cursor_row + 1 - table.scroll_offset.y == 1  # first visible row line
