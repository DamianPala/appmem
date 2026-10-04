"""`RowTable`: a Line-API table widget that replaces `CellTable`/`DataTable`
(SPEC.md "Tech").

Why: `DataTable` renders every cell through `Console.render_lines`, keyed by
an `_update_count` that bumps on structural ops and is part of every cached
cell's key, plus the compositor cost of a full-widget refresh even when only
one row's numbers changed. appmem's cells are plain `Text` -- a `.plain`
string, a `.justify` (`right` or unset), and at most a `dim`/`dim italic`
`.style`, fixed column widths, no markup, no wrapping, one line per row -- so
a widget that builds one `Strip` per row directly from `Segment`s, caches it,
and refreshes a single screen line on `update_cell` does the same job for a
fraction of the work. A spike (200x50, 43 rows, 9 columns, one
tick/second) measured this against `CellTable`: at
appmem's real median of 6 changed cells/tick, 1.25 % vs 0.27 % of one core; at
30, 2.55 % vs 0.83 %; at 100, 3.15 % vs 1.22 %.

Design: `_rows`/`_row_order` hold each row's key, its cells (verbatim `Text`
objects) and their insertion order; `_strips` caches one `Strip` per row key,
built once from `.plain`/`.justify`/`.style` and reused across frames
regardless of scroll position -- reordering never touches it, since it's
keyed by row identity, not screen position. The cursor row is never cached
(rebuilt on every `render_line` call for that one row instead): it is the
only row whose look depends on transient state (focus, in `get_component_
rich_style`'s resolution of `rowtable--cursor`), and rebuilding one row per
frame is free next to the savings above. `update_cell` invalidates only the
touched row's cached strip and refreshes that one screen line; `add_row`/
`remove_row`/`reorder` never touch another row's cached strip, only the
widget-wide repaint their own row-position change needs. Click and header-
click detection reuse Rich's own per-segment `meta`, the same mechanism
`DataTable._on_click` relies on, instead of hand-rolled offset math.

Never parses markup: a cell's `.plain` and `.style` are rendered literally,
column labels too -- the screens that feed this widget never build `Text`
with spans, and a cell's `.spans` are ignored on purpose (SPEC.md "Behaviour
details": an app name containing `[bold]`-style brackets must show literally).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.cells import cell_len, set_cell_size
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App
from textual.binding import Binding, BindingType
from textual.geometry import Region, Size, Spacing
from textual.message import Message
from textual.reactive import reactive
from textual.scroll_view import ScrollView
from textual.strip import Strip

# `DataTable`'s default `cell_padding` (1 cell each side of every column);
# the screens' own width math already budgets for this exact constant
# (`_CELL_PADDING` in `screens/main.py`/`screens/processes.py`).
_CELL_PADDING = 1


class RowTableError(ValueError):
    """An unknown row or column key was used."""


@dataclass
class _Column:
    key: str
    label: str
    width: int | None
    """`None` means auto: grows to the widest current label/cell (SPEC.md
    "Tech"), tracked in `RowTable._resolved_widths`."""


@dataclass
class _Row:
    key: str
    cells: list[Text]


def _fit(text: str, width: int, *, right: bool) -> str:
    """Pad or truncate `text` to exactly `width` terminal cells, wide-
    character-safe. `right` pads on the left (a `Text` with `justify="right"`);
    anything else pads on the right, matching every other `Text` this widget
    ever receives (SPEC.md "Tech": the screens already truncate, so the
    truncation branch here is a safety net, not the common path)."""
    if not right:
        return set_cell_size(text, width)
    current = cell_len(text)
    if current > width:
        text = set_cell_size(text, width)
        current = width
    return " " * (width - current) + text


def _cell_style(cell: Text) -> Style:
    style = cell.style
    if isinstance(style, Style):
        return style
    if style:
        return Style.parse(style)
    return Style()


class RowTable(ScrollView, can_focus=True):
    """One header line plus one row per line, cached (module docstring).
    Columns, rows and the cursor use plain `str` keys throughout -- no
    `RowKey`/`ColumnKey` wrapper types."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"rowtable--header", "rowtable--cursor"}

    DEFAULT_CSS = """
    RowTable {
        background: $surface;
        color: $foreground;

        &:focus {
            background-tint: $foreground 5%;

            & > .rowtable--cursor {
                background: $block-cursor-background;
                color: $block-cursor-foreground;
                text-style: $block-cursor-text-style;
            }

            & > .rowtable--header {
                background-tint: $foreground 5%;
            }
        }

        & > .rowtable--header {
            text-style: bold;
            background: $panel;
            color: $foreground;
        }
        &:ansi > .rowtable--header {
            background: ansi_bright_blue;
            color: ansi_default;
        }

        & > .rowtable--cursor {
            background: $block-cursor-blurred-background;
            color: $block-cursor-blurred-foreground;
            text-style: $block-cursor-blurred-text-style;
        }
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    class HeaderSelected(Message):
        """A click on a column's header line (SPEC.md "Tech")."""

        def __init__(self, row_table: RowTable, column_key: str) -> None:
            self.row_table = row_table
            self.column_key = column_key
            super().__init__()

        @property
        def control(self) -> RowTable:
            return self.row_table

    class RowSelected(Message):
        """Enter, or a double click, on a row."""

        def __init__(self, row_table: RowTable, row_key: str) -> None:
            self.row_table = row_table
            self.row_key = row_key
            super().__init__()

        @property
        def control(self) -> RowTable:
            return self.row_table

    class RowHighlighted(Message):
        """The cursor moved to a new row (never fired for a no-op move)."""

        def __init__(self, row_table: RowTable, row_key: str) -> None:
            self.row_table = row_table
            self.row_key = row_key
            super().__init__()

        @property
        def control(self) -> RowTable:
            return self.row_table

    cursor_row: reactive[int] = reactive(0, init=False)

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._columns: list[_Column] = []
        self._column_index: dict[str, int] = {}
        self._resolved_widths: dict[str, int] = {}
        self._rows: dict[str, _Row] = {}
        self._row_order: list[str] = []
        self._strips: dict[str, Strip] = {}
        self._header_strip: Strip | None = None
        self._last_click_at: float | None = None
        self.virtual_size = Size(0, 1)

    def notify_style_update(self) -> None:
        # Every cached `Strip` bakes in resolved colours (`self.rich_style`,
        # `get_component_rich_style`); a theme change (incl. the theme
        # panel's live preview) recomputes CSS without touching a single
        # row's content, so nothing else would invalidate them (SPEC.md
        # "Tech"). Textual calls this on every such recompute -- the same
        # hook `DataTable._clear_caches` uses for the same reason.
        super().notify_style_update()
        self._strips.clear()
        self._header_strip = None
        self.refresh()

    # --- columns --------------------------------------------------------------

    @property
    def column_keys(self) -> tuple[str, ...]:
        return tuple(column.key for column in self._columns)

    def add_column(self, label: str | Text, *, width: int | None, key: str) -> None:
        if key in self._column_index:
            raise RowTableError(f"column {key!r} already exists")
        text = label if isinstance(label, str) else label.plain
        self._columns.append(_Column(key=key, label=text, width=width))
        self._column_index[key] = len(self._columns) - 1
        self._resolved_widths[key] = width if width is not None else cell_len(text)
        self._header_strip = None
        self.virtual_size = self._compute_virtual_size()

    def set_column_label(self, key: str, label: str | Text) -> None:
        column = self._columns[self._require_column(key)]
        column.label = label if isinstance(label, str) else label.plain
        if column.width is None:
            self._recompute_auto_width(key)
        self._header_strip = None
        self.refresh()

    def get_column_label(self, key: str) -> str:
        return self._columns[self._require_column(key)].label

    def column_width(self, key: str) -> int:
        """The column's current content width, in cells -- not including its
        one-cell padding on each side."""
        self._require_column(key)
        return self._resolved_widths[key]

    def column_region(self, key: str) -> Region:
        """The column's screen-column region (its own padding on each side
        included), independent of horizontal scroll -- what `DataTable.
        _get_column_region` gave privately, needed to verify no column is
        pushed past the terminal edge (SPEC.md "Main view"/"Process view")."""
        index = self._require_column(key)
        x = sum(
            self._resolved_widths[column.key] + 2 * _CELL_PADDING
            for column in self._columns[:index]
        )
        width = self._resolved_widths[key] + 2 * _CELL_PADDING
        return Region(x, 0, width, 1)

    def clear(self, columns: bool = False) -> None:
        self._rows.clear()
        self._row_order.clear()
        self._strips.clear()
        self.set_reactive(RowTable.cursor_row, 0)
        if columns:
            self._columns.clear()
            self._column_index.clear()
            self._resolved_widths.clear()
            self._header_strip = None
        self.virtual_size = self._compute_virtual_size()
        self.refresh()

    def _recompute_auto_width(self, key: str) -> None:
        index = self._column_index[key]
        column = self._columns[index]
        widest = cell_len(column.label)
        for row in self._rows.values():
            widest = max(widest, cell_len(row.cells[index].plain))
        self._set_auto_width(key, widest)

    def _widen_auto_width(self, key: str, width: int) -> None:
        """A brand-new row can only ever grow an auto column's width, never
        shrink it -- only removing a row or shrinking a cell can do that, so
        `remove_row`/`update_cell` keep the full rescan (`_recompute_auto_
        width`). Letting `add_row` take `max(current, width)` instead avoids
        rescanning every existing row for each row added, which made
        rebuilding an n-row table O(n^2) `cell_len` calls."""
        if width > self._resolved_widths[key]:
            self._set_auto_width(key, width)

    def _set_auto_width(self, key: str, width: int) -> None:
        if self._resolved_widths.get(key) != width:
            self._resolved_widths[key] = width
            self._header_strip = None
            self._strips.clear()  # every cached row used the old width for this column
            self.virtual_size = self._compute_virtual_size()

    def _compute_virtual_size(self) -> Size:
        width = sum(
            self._resolved_widths[column.key] + 2 * _CELL_PADDING for column in self._columns
        )
        return Size(width, 1 + len(self._row_order))

    # --- rows -------------------------------------------------------------------

    @property
    def row_keys(self) -> tuple[str, ...]:
        return tuple(self._row_order)

    @property
    def row_count(self) -> int:
        return len(self._row_order)

    @property
    def cursor_key(self) -> str | None:
        if 0 <= self.cursor_row < len(self._row_order):
            return self._row_order[self.cursor_row]
        return None

    def get_row_index(self, key: str) -> int:
        try:
            return self._row_order.index(key)
        except ValueError:
            raise RowTableError(f"no row {key!r}") from None

    def add_row(self, *cells: Text, key: str) -> None:
        if key in self._rows:
            raise RowTableError(f"row {key!r} already exists")
        if len(cells) != len(self._columns):
            raise RowTableError(f"expected {len(self._columns)} cells, got {len(cells)}")
        self._rows[key] = _Row(key=key, cells=list(cells))
        self._row_order.append(key)
        for index, column in enumerate(self._columns):
            if column.width is None:
                self._widen_auto_width(column.key, cell_len(cells[index].plain))
        self.virtual_size = self._compute_virtual_size()
        self.refresh()

    def remove_row(self, key: str) -> None:
        self._require_row(key)
        del self._rows[key]
        self._row_order.remove(key)
        self._strips.pop(key, None)
        for column in self._columns:
            if column.width is None:
                self._recompute_auto_width(column.key)
        if self.cursor_row >= len(self._row_order):
            self.set_reactive(RowTable.cursor_row, max(0, len(self._row_order) - 1))
        self.virtual_size = self._compute_virtual_size()
        self.refresh()

    def update_cell(self, row_key: str, column_key: str, value: Text) -> None:
        row = self._rows.get(row_key)
        if row is None:
            raise RowTableError(f"no row {row_key!r}")
        index = self._require_column(column_key)
        row.cells[index] = value
        column = self._columns[index]
        if column.width is None:
            self._recompute_auto_width(column.key)
        self._strips.pop(row_key, None)
        self._refresh_row(row_key)

    def get_cell(self, row_key: str, column_key: str) -> Text:
        row = self._rows.get(row_key)
        if row is None:
            raise RowTableError(f"no row {row_key!r}")
        return row.cells[self._require_column(column_key)]

    def reorder(self, ordered_keys: list[str]) -> None:
        """Put rows in `ordered_keys` order (every existing key, each exactly
        once); a no-op when already in that order. Never rebuilds a row's
        cached strip -- the cache is keyed by row identity, not position --
        and keeps the cursor on the same row key without posting
        `RowHighlighted` (nothing was newly highlighted, just moved)."""
        if ordered_keys == self._row_order:
            return
        if sorted(ordered_keys) != sorted(self._row_order):
            raise RowTableError("reorder needs every existing row key exactly once")
        current_key = self.cursor_key
        self._row_order = list(ordered_keys)
        if current_key is not None:
            self.set_reactive(RowTable.cursor_row, self._row_order.index(current_key))
        self.refresh()

    def _require_column(self, key: str) -> int:
        try:
            return self._column_index[key]
        except KeyError:
            raise RowTableError(f"no column {key!r}") from None

    def _require_row(self, key: str) -> None:
        if key not in self._rows:
            raise RowTableError(f"no row {key!r}")

    def _refresh_row(self, key: str) -> None:
        try:
            index = self._row_order.index(key)
        except ValueError:
            return
        self._refresh_row_at(index)

    def _refresh_row_at(self, index: int) -> None:
        y = index + 1 - self.scroll_offset.y
        if 0 <= y < self.size.height:
            self.refresh(Region(0, y, self.size.width, 1))

    # --- cursor -------------------------------------------------------------------

    def move_cursor(self, row: int, *, scroll: bool = True) -> None:
        if not self._row_order:
            return
        clamped = max(0, min(row, len(self._row_order) - 1))
        self.cursor_row = clamped
        if scroll:
            self._scroll_cursor_into_view()
            # Again after the next refresh: a screen's `on_resize` calls this
            # before the table has its new size, so the window seen now can
            # still be the old, taller one.
            self.call_after_refresh(self._scroll_cursor_into_view)

    def _scroll_cursor_into_view(self) -> None:
        # Top spacing 1: the fixed header covers virtual line `scroll_y`.
        # `x = scroll_x`: a vertical move never resets a sideways scroll.
        self.scroll_to_region(
            Region(self.scroll_offset.x, self.cursor_row + 1, 1, 1),
            spacing=Spacing(1, 0, 0, 0),
            animate=False,
        )

    def watch_cursor_row(self, old_row: int, new_row: int) -> None:
        self._refresh_row_at(old_row)
        self._refresh_row_at(new_row)
        key = self.cursor_key
        if key is not None:
            self.post_message(self.RowHighlighted(self, key))

    def action_cursor_up(self) -> None:
        self.move_cursor(self.cursor_row - 1)

    def action_cursor_down(self) -> None:
        self.move_cursor(self.cursor_row + 1)

    def action_page_up(self) -> None:
        # Viewport and cursor move by a page together, as `DataTable`'s did.
        page = max(1, self.size.height - 1)
        self.scroll_relative(y=-page, animate=False)
        self.move_cursor(self.cursor_row - page)

    def action_page_down(self) -> None:
        page = max(1, self.size.height - 1)
        self.scroll_relative(y=page, animate=False)
        self.move_cursor(self.cursor_row + page)

    def action_scroll_home(self) -> None:
        self.move_cursor(0)

    def action_scroll_end(self) -> None:
        self.move_cursor(len(self._row_order) - 1)

    def action_select_cursor(self) -> None:
        key = self.cursor_key
        if key is not None:
            self.post_message(self.RowSelected(self, key))

    def _row_key_at_content_offset(self, event: events.Click) -> str | None:
        """A click right of the last column's padding lands on the fill
        segment `render_line` appends past the row's own content, which
        carries no `row` meta. Map it to a row by content offset instead,
        the same as clicking the row itself: `DataTable._on_click` accepts
        an out-of-bounds column for a row cursor the same way."""
        offset = event.get_content_offset(self)
        if offset is None or offset.y < 1:
            return None
        index = offset.y - 1 + self.scroll_offset.y
        if 0 <= index < len(self._row_order):
            return self._row_order[index]
        return None

    def _double_click_is_ours(self, event: events.Click) -> bool:
        """Textual counts a click chain app-wide, so the second click of a
        double click whose first click closed a panel above this table
        arrives here as `chain=2` without this table ever seeing the first.
        It is a click on a widget that was covered a moment ago, not a
        double click on a row. Times are the events' own, as Textual's chain
        uses, so a slow handler cannot split a real double click."""
        now = event.time
        previous, self._last_click_at = self._last_click_at, now
        if event.chain < 2:
            return False
        return previous is not None and now - previous <= App.CLICK_CHAIN_TIME_THRESHOLD

    def on_click(self, event: events.Click) -> None:
        opens = self._double_click_is_ours(event)
        meta = event.style.meta
        column_key = meta.get("column")
        if column_key is not None:
            self.post_message(self.HeaderSelected(self, column_key))
            event.stop()
            return
        row_key = meta.get("row") or self._row_key_at_content_offset(event)
        if row_key is None:
            return
        try:
            index = self._row_order.index(row_key)
        except ValueError:
            return
        self.move_cursor(index)
        if opens:
            self.post_message(self.RowSelected(self, row_key))
        event.stop()

    # --- rendering ----------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        base_style = self.visual_style.rich_style
        scroll_x, scroll_y = self.scroll_offset
        # The header bar and the cursor bar span the full width past the
        # last column, as `DataTable`'s did; other rows fill with the base.
        fill = base_style
        if y == 0:
            if self._header_strip is None:
                self._header_strip = self._build_header_strip()
            strip = self._header_strip
            fill = self.get_component_rich_style("rowtable--header")
        else:
            index = y - 1 + scroll_y
            if index >= len(self._row_order):
                return Strip.blank(width, base_style)
            key = self._row_order[index]
            if index == self.cursor_row:
                strip = self._build_row_strip(key, cursor=True)
                fill = self.rich_style + self.get_component_rich_style("rowtable--cursor")
            else:
                strip = self._strips.get(key)
                if strip is None:
                    strip = self._build_row_strip(key, cursor=False)
                    self._strips[key] = strip
        return strip.crop(scroll_x, scroll_x + width).extend_cell_length(width, fill)

    def _build_header_strip(self) -> Strip:
        style = self.get_component_rich_style("rowtable--header")
        segments = [
            Segment(
                f" {_fit(column.label, self._resolved_widths[column.key], right=False)} ",
                style + Style.from_meta({"column": column.key}),
            )
            for column in self._columns
        ]
        return Strip(segments)

    def _build_row_cell_text(self, column: _Column, cell: Text) -> str:
        return _fit(cell.plain, self._resolved_widths[column.key], right=cell.justify == "right")

    def _build_cell_segments(self, column: _Column, cell: Text, *, base: Style) -> list[Segment]:
        """One cell's own one-space padding on each side, in `base` (never the
        cell's own `.style`): a `dim` cell's padding is invisible either way
        (a blank glyph draws nothing), but SVG export still assigns every
        run a colour class, and a padding run inheriting `dim` would blend
        against a different base than the rest of the row's padding."""
        text = self._build_row_cell_text(column, cell)
        style = base + _cell_style(cell)
        return [Segment(" ", base), Segment(text, style), Segment(" ", base)]

    def _build_row_strip(self, key: str, *, cursor: bool) -> Strip:
        # `self.rich_style` first, an explicit `color`/`background` (SPEC.md
        # "Tech"): a cell's own `dim` attribute has no colour of its own, so
        # exporting it (`get_html_style`) blends `Style.color` -- `None` here
        # would blend the SVG export theme's generic foreground instead of
        # this widget's actual `$foreground`, a colour-accurate but visibly
        # wrong grey in a screenshot.
        row = self._rows[key]
        base = self.rich_style
        if cursor:
            base += self.get_component_rich_style("rowtable--cursor")
        meta = base + Style.from_meta({"row": key})
        segments = [
            segment
            for column, cell in zip(self._columns, row.cells, strict=True)
            for segment in self._build_cell_segments(column, cell, base=meta)
        ]
        return Strip(segments)
