"""Process view: per-process table for one app, with grouping by command
(SPEC.md "Process view").

Mirrors `MainScreen`'s tick/diff/sort patterns (row diffing via row keys,
`update_cell` only on changed text, cursor restore by key). The app's
`unit_paths` are captured once, at Enter time, and re-read every tick
(SPEC.md: "re-read the app's units and processes (`read_procs` on the app's
`unit_paths`)"); the app is considered gone once every one of them stops
reading (SPEC.md "If the app disappears").
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey, RowKey

from appmem.collect import AppStats, ProcStats
from appmem.collect import read_procs as collect_read_procs
from appmem.collect import read_unit as collect_read_unit
from appmem.fmt import format_age, size, truncate_name
from appmem.ui.process_rows import (
    SHARED_SORT_KEYS,
    CommandRow,
    ProcessRow,
    ProcessSortKey,
    build_command_rows,
    build_process_rows,
    next_sort_state,
    other_command_row,
    other_process_row,
    sort_command_rows,
    sort_process_rows,
)
from appmem.ui.screens.help import HelpScreen

_PROCESS_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("pid", "PID", 7),  # pid_max 4194304: 7 digits
    ("name", "NAME", None),
    ("swap", "SWAP", 10),
    ("ram", "RAM", 10),
    ("total", "TOTAL", 10),
    ("age", "AGE", 6),
    ("unit", "UNIT", None),  # auto width: never truncated, table scrolls sideways instead
)
_GROUP_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("name", "NAME", None),
    ("swap", "SWAP", 10),
    ("ram", "RAM", 10),
    ("total", "TOTAL", 10),
    ("procs", "PROCS", 6),
)
_LEFT_ALIGNED = {"name", "unit"}
_OTHER_KEY = "other"


def _process_row_key(row: ProcessRow) -> str:
    return _OTHER_KEY if row.pid is None else str(row.pid)


def _command_row_key(row: CommandRow) -> str:
    return _OTHER_KEY if row.procs is None else row.name


def _format_process_cell(key: str, row: ProcessRow) -> str:
    if key == "pid":
        return "" if row.pid is None else str(row.pid)
    if key == "name":
        return truncate_name(row.name)
    if key == "swap":
        return size(row.swap)
    if key == "ram":
        return size(row.ram)
    if key == "total":
        return size(row.total)
    if key == "age":
        return "" if row.age_seconds is None else format_age(row.age_seconds)
    return row.unit  # "unit"


def _format_command_cell(key: str, row: CommandRow) -> str:
    if key == "name":
        return truncate_name(row.name)
    if key == "swap":
        return size(row.swap)
    if key == "ram":
        return size(row.ram)
    if key == "total":
        return size(row.total)
    return "" if row.procs is None else str(row.procs)  # "procs"


def _cell_value(key: str, text: str, *, other: bool = False) -> str | Text:
    # The `other` row renders dim italic so it reads as a remainder, not a process.
    style = "dim italic" if other else ""
    if key in _LEFT_ALIGNED:
        return Text(text, style=style) if other else text
    return Text(text, justify="right", style=style)


def _key_str(key: RowKey | ColumnKey) -> str:
    assert key.value is not None
    return key.value


class ProcessesScreen(Screen[None]):
    """Per-process table for one app (SPEC.md "Process view")."""

    # Same reason as MainScreen: DataTable's `height: auto` would push the
    # title off screen with many rows.
    DEFAULT_CSS = """
    ProcessesScreen #table { height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "sort('swap')", "sort SWAP", show=False),
        Binding("r", "sort('ram')", "sort RAM", show=False),
        Binding("t", "sort('total')", "sort TOTAL", show=False),
        Binding("g", "toggle_group", "group by command", show=False),
        Binding("?", "help", "help", show=False),
        Binding("escape", "back", "back", show=False),
    ]

    def __init__(
        self,
        *,
        root: Path,
        app_stats: AppStats,
        interval: float,
        initial_sort: tuple[ProcessSortKey, bool],
    ) -> None:
        super().__init__()
        self._root = root
        self._app_name = app_stats.name
        self._unit_paths = app_stats.unit_paths
        self._interval = interval
        self._sort_key: ProcessSortKey = initial_sort[0]
        self._sort_reverse = initial_sort[1]
        self._grouped = False
        self._process_rows: dict[str, ProcessRow] = {}
        self._command_rows: dict[str, CommandRow] = {}

    def compose(self) -> ComposeResult:
        yield Static(id="title")
        table: DataTable[str | Text] = DataTable(id="table", cursor_type="row")
        self._rebuild_columns(table)
        yield table

    def on_mount(self) -> None:
        self.refresh_now()
        self.set_interval(self._interval, self._tick)

    def _tick(self) -> None:
        # Covered by help: skip the work, `on_screen_resume` catches up.
        if self.is_active:
            self.refresh_now()

    def on_screen_resume(self) -> None:
        self.refresh_now()  # no stale numbers when a screen pushed on top of us closes

    def _table(self) -> DataTable[str | Text]:
        return cast("DataTable[str | Text]", self.query_one("#table", DataTable))

    def _set_static(self, selector: str, text: str) -> None:
        widget = self.query_one(selector, Static)
        if widget.content != text:
            widget.update(text)

    # --- columns --------------------------------------------------------------

    def _columns(self) -> tuple[tuple[str, str, int | None], ...]:
        return _GROUP_COLUMNS if self._grouped else _PROCESS_COLUMNS

    def _header_label(self, key: str, label: str) -> str:
        if key != self._sort_key:
            return label
        marker = "▾" if self._sort_reverse else "▴"
        return f"{label} {marker}"

    def _rebuild_columns(self, table: DataTable[str | Text]) -> None:
        table.clear(columns=True)
        for key, label, width in self._columns():
            table.add_column(self._header_label(key, label), width=width, key=key)

    def _refresh_column_labels(self, table: DataTable[str | Text]) -> None:
        for key, label, _width in self._columns():
            table.columns[ColumnKey(key)].label = Text(self._header_label(key, label))
        table.refresh()

    # --- collection tick ----------------------------------------------------

    def refresh_now(self) -> None:
        unit_stats = [
            stats for path in self._unit_paths if (stats := collect_read_unit(path)) is not None
        ]
        if not unit_stats:
            self._show_gone()
            return
        app = AppStats(
            name=self._app_name,
            ram=sum(s.ram for s in unit_stats),
            cache=sum(s.cache for s in unit_stats),
            swap=sum(s.swap for s in unit_stats),
            total=sum(s.total for s in unit_stats),
            procs=sum(s.procs for s in unit_stats),
            unit_paths=self._unit_paths,
        )
        procs = collect_read_procs(self._unit_paths, self._root)
        self._set_title(app, len(procs))
        if self._grouped:
            self._apply_command_rows(procs, app)
        else:
            self._apply_process_rows(procs, app)

    def _show_gone(self) -> None:
        self._set_static("#title", f"{self._app_name}   (app no longer running)   esc  back")
        table = self._table()
        current = self._command_rows if self._grouped else self._process_rows
        for key in current:
            table.remove_row(key)
        self._process_rows = {}
        self._command_rows = {}

    def _set_title(self, app: AppStats, proc_count: int) -> None:
        text = (
            f"{app.name}   {proc_count} procs   swap {size(app.swap)}   RAM {size(app.ram)}"
            "   g  group by command   esc  back"
        )
        self._set_static("#title", text)

    # --- row diffing: process rows --------------------------------------------

    def _process_cells(self, row: ProcessRow) -> list[str | Text]:
        return [
            _cell_value(key, _format_process_cell(key, row), other=row.pid is None)
            for key, _label, _width in self._columns()
        ]

    def _apply_process_rows(self, procs: list[ProcStats], app: AppStats) -> None:
        table = self._table()
        real_rows = sort_process_rows(build_process_rows(procs), self._sort_key, self._sort_reverse)
        ordered = [*real_rows, other_process_row(app, procs)]
        new_by_key = {_process_row_key(row): row for row in ordered}
        previous_key, previous_index = self._current_selection(table)

        for key in self._process_rows.keys() - new_by_key.keys():
            table.remove_row(key)
        for key, row in new_by_key.items():
            old = self._process_rows.get(key)
            if old is not None:
                self._update_process_cells(table, key, old, row)
            else:
                table.add_row(*self._process_cells(row), key=key)
        self._process_rows = new_by_key

        self._resort_process(table, ordered)
        self._restore_selection(table, previous_key, previous_index)

    def _update_process_cells(
        self, table: DataTable[str | Text], key: str, old: ProcessRow, row: ProcessRow
    ) -> None:
        for col_key, _label, _width in self._columns():
            old_text = _format_process_cell(col_key, old)
            new_text = _format_process_cell(col_key, row)
            if old_text != new_text:
                value = _cell_value(col_key, new_text, other=row.pid is None)
                table.update_cell(key, col_key, value)

    # --- row diffing: grouped (command) rows -----------------------------------

    def _command_cells(self, row: CommandRow) -> list[str | Text]:
        return [
            _cell_value(key, _format_command_cell(key, row), other=row.procs is None)
            for key, _label, _width in self._columns()
        ]

    def _apply_command_rows(self, procs: list[ProcStats], app: AppStats) -> None:
        table = self._table()
        real_rows = sort_command_rows(build_command_rows(procs), self._sort_key, self._sort_reverse)
        ordered = [*real_rows, other_command_row(app, procs)]
        new_by_key = {_command_row_key(row): row for row in ordered}
        previous_key, previous_index = self._current_selection(table)

        for key in self._command_rows.keys() - new_by_key.keys():
            table.remove_row(key)
        for key, row in new_by_key.items():
            old = self._command_rows.get(key)
            if old is not None:
                self._update_command_cells(table, key, old, row)
            else:
                table.add_row(*self._command_cells(row), key=key)
        self._command_rows = new_by_key

        self._resort_command(table, ordered)
        self._restore_selection(table, previous_key, previous_index)

    def _update_command_cells(
        self, table: DataTable[str | Text], key: str, old: CommandRow, row: CommandRow
    ) -> None:
        for col_key, _label, _width in self._columns():
            old_text = _format_command_cell(col_key, old)
            new_text = _format_command_cell(col_key, row)
            if old_text != new_text:
                value = _cell_value(col_key, new_text, other=row.procs is None)
                table.update_cell(key, col_key, value)

    # --- selection / resort, shared by both modes -------------------------------

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
        rows = self._command_rows if self._grouped else self._process_rows
        if previous_key is not None and previous_key in rows:
            table.move_cursor(row=table.get_row_index(previous_key))
        else:
            table.move_cursor(row=min(previous_index, table.row_count - 1))

    def _resort_process(self, table: DataTable[str | Text], ordered: list[ProcessRow]) -> None:
        ordered_keys = [_process_row_key(row) for row in ordered]
        if ordered_keys == [_key_str(row.key) for row in table.ordered_rows]:
            return  # same order: `table.sort` would only force a full repaint
        # PID is the first (leftmost) column: its rendered text is what `table.sort`'s
        # key function sees as `values[0]`, and "" is unique to the `other` row.
        rank = {_format_process_cell("pid", row): index for index, row in enumerate(ordered)}
        table.sort(key=lambda values: rank[str(values[0])])

    def _resort_command(self, table: DataTable[str | Text], ordered: list[CommandRow]) -> None:
        ordered_keys = [_command_row_key(row) for row in ordered]
        if ordered_keys == [_key_str(row.key) for row in table.ordered_rows]:
            return
        # NAME is the first column in grouped mode; command names are unique per tick.
        rank = {_format_command_cell("name", row): index for index, row in enumerate(ordered)}
        table.sort(key=lambda values: rank[str(values[0])])

    # --- actions ----------------------------------------------------------------

    def _set_sort(self, column: str) -> None:
        self._sort_key, self._sort_reverse = next_sort_state(
            self._sort_key, self._sort_reverse, column
        )
        table = self._table()
        self.refresh_now()
        self._refresh_column_labels(table)

    def action_sort(self, column: str) -> None:
        self._set_sort(column)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        self._set_sort(_key_str(event.column_key))

    def action_toggle_group(self) -> None:
        self._grouped = not self._grouped
        if self._sort_key not in SHARED_SORT_KEYS:
            self._sort_key, self._sort_reverse = "total", True
        table = self._table()
        self._process_rows = {}
        self._command_rows = {}
        self._rebuild_columns(table)
        self.refresh_now()

    def action_back(self) -> None:
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())  # pyright: ignore[reportUnknownMemberType]
