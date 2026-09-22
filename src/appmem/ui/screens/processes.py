"""Process view: per-process table for one app, with grouping by command
(SPEC.md "Process view").

Mirrors `MainScreen`'s tick/diff/sort patterns (row diffing via row keys,
`update_cell` only on changed text, cursor restore by key). The app's unit
list is re-derived every tick from `find_app_units` (same identity/naming
rules as the main view), not captured once at Enter, so a unit added or
replaced while the screen is open is picked up (final review A3); the app is
considered gone only once no unit maps to its identity any more.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey, RowKey

from appmem.collect import AppStats, CgroupUnavailableError, MemoryStatUnavailableError, ProcStats
from appmem.collect import find_app_units as collect_find_app_units
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
    kernel_command_row,
    kernel_process_row,
    next_sort_state,
    sort_command_rows,
    sort_process_rows,
    unattributed_command_row,
    unattributed_process_row,
)
from appmem.ui.screens.help import HelpScreen
from appmem.ui.table_order import reorder_rows

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


def _cell_value(key: str, text: str, *, dim: bool = False) -> Text:
    # Always a literal `Text`, never a plain `str`: `DataTable` renders a `str`
    # cell through `Text.from_markup`, which would parse markup-like process
    # or unit names instead of showing them literally (final review A11).
    style = "dim italic" if dim else ""
    if key in _LEFT_ALIGNED:
        return Text(text, style=style)
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
        uid: int,
        include_system: bool,
        name: str,
        scope: str,
        interval: float,
        initial_sort: tuple[ProcessSortKey, bool],
    ) -> None:
        super().__init__()
        self._root = root
        self._uid = uid
        self._include_system = include_system
        self._name = name
        self._scope = scope  # stored for slice 5's scope-aware action hints
        self._interval = interval
        self._sort_key: ProcessSortKey = initial_sort[0]
        self._sort_reverse = initial_sort[1]
        self._grouped = False
        self._process_rows: dict[str, ProcessRow] = {}
        self._command_rows: dict[str, CommandRow] = {}
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="title", markup=False)
        table: DataTable[str | Text] = DataTable(id="table", cursor_type="row")
        self._rebuild_columns(table)
        yield table

    def on_mount(self) -> None:
        self.refresh_now()
        self._timer = self.set_interval(self._interval, self._tick)

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
        try:
            self._refresh_now_unsafe()
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
        except MemoryStatUnavailableError:
            pass  # transient this tick: keep the last data on screen, try again next tick

    def _refresh_now_unsafe(self) -> None:
        # `strict=False`: a transient `memory.stat` read failure on the user
        # root raises `MemoryStatUnavailableError` (skip the tick) rather than
        # `CgroupUnavailableError` (fatal) -- only the directory vanishing is
        # fatal here (final review, slice 4 round 2 item 5).
        unit_paths = collect_find_app_units(
            self._root, self._uid, self._include_system, self._scope, self._name, strict=False
        )
        unit_stats = [
            stats for path in unit_paths if (stats := collect_read_unit(path)) is not None
        ]
        if not unit_stats:
            self._show_gone()
            return
        app = AppStats(
            name=self._name,
            scope=self._scope,
            ram=sum(s.ram for s in unit_stats),
            cache=sum(s.cache for s in unit_stats),
            swap=sum(s.swap for s in unit_stats),
            total=sum(s.total for s in unit_stats),
            procs=sum(s.procs for s in unit_stats),
            kernel=sum(s.kernel for s in unit_stats),
            unit_paths=tuple(unit_paths),
        )
        procs = collect_read_procs(unit_paths, self._root)
        self._set_title(app, len(procs))
        if self._grouped:
            self._apply_command_rows(procs, app)
        else:
            self._apply_process_rows(procs, app)

    def _fail_cgroup_unavailable(self, exc: CgroupUnavailableError) -> None:
        if self._timer is not None:
            self._timer.stop()
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve to `AppMemApp` (SPEC.md "Tech" notes).
        self.app.fail_cgroup_unavailable(  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            str(exc)
        )

    def _show_gone(self) -> None:
        self._set_static("#title", f"{self._name}   (app no longer running)   esc  back")
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
            _cell_value(key, _format_process_cell(key, row), dim=row.dim)
            for key, _label, _width in self._columns()
        ]

    def _apply_process_rows(self, procs: list[ProcStats], app: AppStats) -> None:
        table = self._table()
        real_rows = sort_process_rows(build_process_rows(procs), self._sort_key, self._sort_reverse)
        # `kernel` then `unattributed`, always last, both dim (SPEC.md "Definitions").
        ordered = [*real_rows, kernel_process_row(app), unattributed_process_row(app, procs)]
        new_by_key = {row.key: row for row in ordered}
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

        reorder_rows(table, [row.key for row in ordered])
        self._restore_selection(table, previous_key, previous_index)

    def _update_process_cells(
        self, table: DataTable[str | Text], key: str, old: ProcessRow, row: ProcessRow
    ) -> None:
        for col_key, _label, _width in self._columns():
            old_text = _format_process_cell(col_key, old)
            new_text = _format_process_cell(col_key, row)
            if old_text != new_text:
                table.update_cell(key, col_key, _cell_value(col_key, new_text, dim=row.dim))

    # --- row diffing: grouped (command) rows -----------------------------------

    def _command_cells(self, row: CommandRow) -> list[str | Text]:
        return [
            _cell_value(key, _format_command_cell(key, row), dim=row.dim)
            for key, _label, _width in self._columns()
        ]

    def _apply_command_rows(self, procs: list[ProcStats], app: AppStats) -> None:
        table = self._table()
        real_rows = sort_command_rows(build_command_rows(procs), self._sort_key, self._sort_reverse)
        ordered = [*real_rows, kernel_command_row(app), unattributed_command_row(app, procs)]
        new_by_key = {row.key: row for row in ordered}
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

        reorder_rows(table, [row.key for row in ordered])
        self._restore_selection(table, previous_key, previous_index)

    def _update_command_cells(
        self, table: DataTable[str | Text], key: str, old: CommandRow, row: CommandRow
    ) -> None:
        for col_key, _label, _width in self._columns():
            old_text = _format_command_cell(col_key, old)
            new_text = _format_command_cell(col_key, row)
            if old_text != new_text:
                table.update_cell(key, col_key, _cell_value(col_key, new_text, dim=row.dim))

    # --- selection, shared by both modes -----------------------------------------

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
