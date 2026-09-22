"""Main view: the live, sortable per-app table (SPEC.md "Main view").

Refreshes on a plain `set_interval` timer: the collector costs ~7 ms per tick on
the dev machine (SPEC.md "Tech" budget: under 1 % of one core at 1 s), well
under anything that would make a timer-driven tick feel like it blocks input.
Existing rows are updated in place and only appeared/vanished apps add or
remove a row, so a refresh never rebuilds the table (SPEC.md "Tech" notes).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey, RowKey

from appmem.collect import AppStats, SystemStats, Unit, filter_visible_apps, group_apps, read_system
from appmem.collect import find_units as collect_find_units
from appmem.collect import read_unit as collect_read_unit
from appmem.fmt import format_delta, size, truncate_name
from appmem.ui.header import format_line1, format_line2
from appmem.ui.process_rows import initial_process_sort
from appmem.ui.rows import (
    DEFAULT_SORT_KEY,
    DEFAULT_SORT_REVERSE,
    Row,
    SortKey,
    build_rows,
    next_sort_state,
    reset_baseline,
    sort_rows,
    update_baseline,
)
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.processes import ProcessesScreen

FOOTER_TEXT = " s r t d sort  enter procs  x system  c cache  z reset Δ  ? help  q quit"

# Width `None` = auto: the APP column grows to the longest name (capped at 32 by
# `truncate_name` in `_format_cell`), so short names are never cut short of that.
_BASE_COLUMNS: tuple[tuple[SortKey, str, int | None], ...] = (
    ("app", "APP", None),
    ("swap", "SWAP", 10),
    ("ram", "RAM", 10),
)
_CACHE_COLUMN: tuple[SortKey, str, int | None] = ("cache", "CACHE", 10)
_TAIL_COLUMNS: tuple[tuple[SortKey, str, int | None], ...] = (
    ("total", "TOTAL", 10),
    ("delta_swap", "ΔSWAP", 9),
    ("delta_ram", "ΔRAM", 9),
    ("procs", "PROCS", 6),
)


def _format_cell(key: SortKey, row: Row) -> str:
    if key == "app":
        return truncate_name(row.name)
    if key == "swap":
        return size(row.swap)
    if key == "ram":
        return size(row.ram)
    if key == "cache":
        return size(row.cache)
    if key == "total":
        return size(row.total)
    if key == "delta_swap":
        return format_delta(row.delta_swap)
    if key == "delta_ram":
        return format_delta(row.delta_ram)
    return str(row.procs)  # "procs"


def _cell_value(key: SortKey, row: Row) -> str | Text:
    text = _format_cell(key, row)
    return text if key == "app" else Text(text, justify="right")


def _key_str(key: RowKey | ColumnKey) -> str:
    assert key.value is not None
    return key.value


class MainScreen(Screen[None]):
    """The per-app table: header lines, the DataTable, and the footer (SPEC.md)."""

    # DataTable defaults to `height: auto; max-height: 100%`, which with many rows
    # makes the screen taller than the terminal and scrolls both header lines and
    # the footer out of view. `1fr` gives the table only the space left over.
    DEFAULT_CSS = """
    MainScreen #table { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "sort('swap')", "sort SWAP", show=False),
        Binding("r", "sort('ram')", "sort RAM", show=False),
        Binding("t", "sort('total')", "sort TOTAL", show=False),
        Binding("d", "sort('delta_swap')", "sort ΔSWAP", show=False),
        Binding("c", "toggle_cache", "toggle CACHE", show=False),
        Binding("x", "toggle_system", "toggle system", show=False),
        Binding("z", "reset_delta", "reset Δ", show=False),
        Binding("?", "help", "help", show=False),
    ]

    def __init__(self, *, root: Path, uid: int, interval: float, include_system: bool) -> None:
        super().__init__()
        self._root = root
        self._uid = uid
        self._interval = interval
        self._show_system = include_system
        self._show_cache = False
        self._sort_key: SortKey = DEFAULT_SORT_KEY
        self._sort_reverse = DEFAULT_SORT_REVERSE
        self._baseline: dict[str, AppStats] = {}
        self._baseline_time = datetime.now()
        self._rows: dict[str, Row] = {}
        self._last_apps: list[AppStats] = []

    def compose(self) -> ComposeResult:
        yield Static(id="header1")
        yield Static(id="header2")
        table: DataTable[str | Text] = DataTable(id="table", cursor_type="row")
        self._rebuild_columns(table)
        yield table
        yield Static(FOOTER_TEXT, id="footer")

    def on_mount(self) -> None:
        self.refresh_now()
        self.set_interval(self._interval, self._tick)

    def _tick(self) -> None:
        # Covered by the process view or help: skip the work, `on_screen_resume` catches up.
        if self.is_active:
            self.refresh_now()

    def on_screen_resume(self) -> None:
        self.refresh_now()  # no stale numbers after Esc from the process view

    def _table(self) -> DataTable[str | Text]:
        # `isinstance()` (which `query_one` uses) rejects a parameterized generic,
        # so we query by the bare class and `cast` to the concrete cell type.
        return cast("DataTable[str | Text]", self.query_one("#table", DataTable))

    # --- collection tick ----------------------------------------------------

    def refresh_now(self) -> None:
        """Collect and redraw immediately, instead of waiting for the next tick.

        Called on mount, on the refresh timer, and by the actions (`x`, `z`)
        that need their effect to show up right away rather than after a full
        interval. Also the deterministic re-tick hook the Textual pilot tests
        use instead of racing the real timer (SPEC.md "Tests").
        """
        stats = read_system(self._root)
        unit_paths = collect_find_units(self._root, self._uid, include_system=self._show_system)
        units = [
            Unit(path=path, stats=unit_stats)
            for path in unit_paths
            if (unit_stats := collect_read_unit(path)) is not None
        ]
        apps = filter_visible_apps(group_apps(units))
        self._last_apps = apps
        self._baseline = update_baseline(apps, self._baseline)
        self._apply_rows(build_rows(apps, self._baseline))
        self._update_header(stats)

    def _update_header(self, stats: SystemStats) -> None:
        self._set_static("#header1", format_line1(stats))
        self._update_header_line2()

    def _update_header_line2(self) -> None:
        self._set_static("#header2", format_line2(self._baseline_time, datetime.now()))

    def _set_static(self, selector: str, text: str) -> None:
        # Skip unchanged text: every `update` costs a layout pass and a repaint.
        widget = self.query_one(selector, Static)
        if widget.content != text:
            widget.update(text)

    # --- columns --------------------------------------------------------------

    def _column_specs(self) -> list[tuple[SortKey, str, int | None]]:
        specs = list(_BASE_COLUMNS)
        if self._show_cache:
            specs.append(_CACHE_COLUMN)
        specs.extend(_TAIL_COLUMNS)
        return specs

    def _header_label(self, key: SortKey, label: str) -> str:
        if key != self._sort_key:
            return label
        marker = "▾" if self._sort_reverse else "▴"
        return f"{label} {marker}"

    def _rebuild_columns(self, table: DataTable[str | Text]) -> None:
        table.clear(columns=True)
        for key, label, width in self._column_specs():
            table.add_column(self._header_label(key, label), width=width, key=key)

    def _refresh_column_labels(self, table: DataTable[str | Text]) -> None:
        for key, label, _width in self._column_specs():
            table.columns[ColumnKey(key)].label = Text(self._header_label(key, label))
        table.refresh()

    # --- row diffing ------------------------------------------------------------

    def _row_cells(self, row: Row) -> list[str | Text]:
        return [_cell_value(key, row) for key, _label, _width in self._column_specs()]

    def _update_row_cells(self, table: DataTable[str | Text], old: Row, row: Row) -> None:
        # Only cells whose text changed: any `update_cell` invalidates the whole
        # DataTable render cache, so a no-op update still repaints every row.
        for key, _label, _width in self._column_specs():
            if _format_cell(key, old) != _format_cell(key, row):
                table.update_cell(row.name, key, _cell_value(key, row))

    def _current_selection(self, table: DataTable[str | Text]) -> tuple[str | None, int]:
        index = table.cursor_row
        if table.row_count == 0:
            return None, index
        row_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        return _key_str(row_key), index

    def _restore_selection(
        self, table: DataTable[str | Text], previous_key: str | None, previous_index: int
    ) -> None:
        if table.row_count == 0:
            return
        if previous_key is not None and previous_key in self._rows:
            table.move_cursor(row=table.get_row_index(previous_key))
        else:
            table.move_cursor(row=min(previous_index, table.row_count - 1))

    def _resort(self, table: DataTable[str | Text]) -> None:
        ordered = sort_rows(self._rows.values(), self._sort_key, self._sort_reverse)
        if [row.name for row in ordered] == [_key_str(row.key) for row in table.ordered_rows]:
            return  # same order: `table.sort` would only force a full repaint
        # Rank by the APP column's rendered (possibly truncated) text, since that
        # is what `table.sort`'s key function sees as `values[0]`, not `row.name`.
        rank = {_format_cell("app", row): index for index, row in enumerate(ordered)}
        table.sort(key=lambda values: rank[str(values[0])])

    def _apply_rows(self, rows: list[Row]) -> None:
        table = self._table()
        new_by_name = {row.name: row for row in rows}
        previous_key, previous_index = self._current_selection(table)

        for name in self._rows.keys() - new_by_name.keys():
            table.remove_row(name)
        for row in rows:
            old = self._rows.get(row.name)
            if old is not None:
                self._update_row_cells(table, old, row)
            else:
                table.add_row(*self._row_cells(row), key=row.name)
        self._rows = new_by_name

        self._resort(table)
        self._restore_selection(table, previous_key, previous_index)

    # --- actions ----------------------------------------------------------------

    def _set_sort(self, column: SortKey) -> None:
        self._sort_key, self._sort_reverse = next_sort_state(
            self._sort_key, self._sort_reverse, column
        )
        table = self._table()
        self._resort(table)
        self._refresh_column_labels(table)

    def action_sort(self, column: str) -> None:
        self._set_sort(column)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        self._set_sort(_key_str(event.column_key))

    def action_toggle_cache(self) -> None:
        self._show_cache = not self._show_cache
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        self._rebuild_columns(table)
        for row in self._rows.values():
            table.add_row(*self._row_cells(row), key=row.name)
        self._resort(table)
        self._restore_selection(table, previous_key, previous_index)

    def action_toggle_system(self) -> None:
        self._show_system = not self._show_system
        self.refresh_now()

    def action_reset_delta(self) -> None:
        self._baseline = reset_baseline(self._last_apps)
        self._baseline_time = datetime.now()
        self._apply_rows(build_rows(self._last_apps, self._baseline))
        self._update_header_line2()

    def action_help(self) -> None:
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.push_screen(HelpScreen())  # pyright: ignore[reportUnknownMemberType]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        app_name = _key_str(event.row_key)
        app = next((app for app in self._last_apps if app.name == app_name), None)
        if app is None:  # row vanished between the click and the event
            return
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ProcessesScreen(
                root=self._root,
                app_stats=app,
                interval=self._interval,
                initial_sort=initial_process_sort(self._sort_key, self._sort_reverse),
            )
        )
