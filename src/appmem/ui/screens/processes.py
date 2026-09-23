"""Process view: per-process table for one app, with grouping by command and
a drill-down into one command's members (SPEC.md "Process view").

Mirrors `MainScreen`'s tick/diff/sort patterns (row diffing via row keys,
`update_cell` only on changed text, cursor restore by key). The app's unit
list is re-derived every tick from `find_app_units` (same identity/naming
rules as the main view), not captured once at Enter, so a unit added or
replaced while the screen is open is picked up; the app is considered gone
only once no unit maps to its identity any more.

Three display shapes share this one screen: flat (one row per process),
grouped by command (one row per command name, `g`), and a drill-down into one
command's member processes (Enter on a command row) -- flat process columns
again, scoped to that command's PIDs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar, cast

from rich.text import Text
from textual import events
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
from appmem.fmt import format_age, size, status_line_command, truncate_name
from appmem.ui.layout import build_footer, fit_line
from appmem.ui.process_rows import (
    KERNEL_KEY,
    KERNEL_UNIT_TEXT,
    SHARED_SORT_KEYS,
    UNATTRIBUTED_KEY,
    UNATTRIBUTED_UNIT_TEXT,
    CommandRow,
    GroupSortKey,
    ProcessRow,
    ProcessSortKey,
    ScreenSortKey,
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
# AGE hidden below `_NARROW_WIDTH` columns.
_PROCESS_COLUMNS_NARROW: tuple[tuple[str, str, int | None], ...] = tuple(
    column for column in _PROCESS_COLUMNS if column[0] != "age"
)
_GROUP_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("name", "NAME", None),
    ("swap", "SWAP", 10),
    ("ram", "RAM", 10),
    ("total", "TOTAL", 10),
    ("procs", "PROCS", 6),
)
_LEFT_ALIGNED = {"name", "unit"}

_NARROW_WIDTH = 95

# Key caps; at full width the plain text is exactly " s r t sort  g
# group  enter (grouped: members)  ? help  esc back  q quit".
_FOOTER_ITEMS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("s", "r", "t"), "sort"),
    (("g",), "group"),
    (("enter",), "(grouped: members)"),
    (("?",), "help"),
    (("esc",), "back"),
    (("q",), "quit"),
)
# Below the footer's natural width, drop items lowest priority first;
# `help`, `back` and `quit` are never in this list, so they always stay.
_FOOTER_DROP_ORDER = ("(grouped: members)", "group", "sort")

# Process-view title drop order -- procs count first, then swap; the
# app/breadcrumb name and RAM are always kept.
_TITLE_DROP_ORDER = ("procs", "swap")


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
    # or unit names instead of showing them literally.
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
        self._scope = scope  # user/system: which `systemctl` the status line offers
        self._interval = interval
        self._sort_key: ScreenSortKey = initial_sort[0]
        self._sort_reverse = initial_sort[1]
        self._grouped = False
        self._drill_command: str | None = None
        """The command currently drilled into from grouped mode, or
        `None` when showing the flat or grouped table as usual."""
        self._process_rows: dict[str, ProcessRow] = {}
        self._command_rows: dict[str, CommandRow] = {}
        self._last_app: AppStats | None = None
        self._last_proc_count = 0
        self._age_shown = True
        self._timer: Timer | None = None

    @property
    def _showing_group_table(self) -> bool:
        return self._grouped and self._drill_command is None

    def compose(self) -> ComposeResult:
        self._age_shown = self._show_age_column()
        yield Static(id="title", markup=False)
        table: DataTable[str | Text] = DataTable(id="table", cursor_type="row")
        self._rebuild_columns(table)
        yield table
        yield Static(id="status")
        yield Static(self._footer_text(), id="footer")

    def _footer_text(self) -> Text:
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        return build_footer(_FOOTER_ITEMS, width=width, drop_order=_FOOTER_DROP_ORDER)

    def on_mount(self) -> None:
        self.refresh_now()
        self._timer = self.set_interval(self._interval, self._tick)

    def _tick(self) -> None:
        # Covered by help: skip the work, `on_screen_resume` catches up.
        if self.is_active:
            self.refresh_now()

    def on_screen_resume(self) -> None:
        self.refresh_now()  # no stale numbers when a screen pushed on top of us closes

    def on_resize(self, event: events.Resize) -> None:
        # Title and footer never wrap -- recompute on every resize, not just
        # on the next tick. The status line's ellipsis point moves too.
        self._render_title()
        self._update_status_line()
        self._sync_age_column()
        self._set_rich("#footer", self._footer_text())

    def _table(self) -> DataTable[str | Text]:
        return cast("DataTable[str | Text]", self.query_one("#table", DataTable))

    def _set_rich(self, selector: str, content: Text) -> None:
        widget = self.query_one(selector, Static)
        if widget.content != content:
            widget.update(content)

    # --- columns --------------------------------------------------------------

    def _show_age_column(self) -> bool:
        return self.app.size.width >= _NARROW_WIDTH  # pyright: ignore[reportUnknownMemberType]

    def _sync_age_column(self) -> None:
        show = self._show_age_column()
        if show == self._age_shown:
            return
        self._age_shown = show
        if self._showing_group_table:
            return  # nothing on screen depends on it right now
        sort_hidden = not show and self._sort_key == "age"
        if sort_hidden:
            self._sort_key, self._sort_reverse = "total", True
        self._rebuild_current_table()
        if sort_hidden:
            self.refresh_now()  # the cached rows are still in AGE order: re-sort them

    def _columns(self) -> tuple[tuple[str, str, int | None], ...]:
        if self._showing_group_table:
            return _GROUP_COLUMNS
        return _PROCESS_COLUMNS if self._age_shown else _PROCESS_COLUMNS_NARROW

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

    def _rebuild_current_table(self) -> None:
        """Rebuild columns and repopulate from the already-sorted cached rows
        (no re-collection): used when only column visibility changed (AGE
        hidden/shown by width), not the underlying data shape."""
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        self._rebuild_columns(table)
        if self._showing_group_table:
            for key, row in self._command_rows.items():
                table.add_row(*self._command_cells(row), key=key)
        else:
            for key, row in self._process_rows.items():
                table.add_row(*self._process_cells(row), key=key)
        self._restore_selection(table, previous_key, previous_index)
        self._update_status_line()

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
        # fatal here.
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
        if self._drill_command is not None:
            members = [proc for proc in procs if proc.name == self._drill_command]
            # The breadcrumb's counts are the command's, as on its grouped row.
            swap, ram = sum(p.swap for p in members), sum(p.ram for p in members)
            self._set_title(replace(app, swap=swap, ram=ram), len(members))
            self._apply_process_rows(members, app, include_synthetic=False)
            return
        self._set_title(app, len(procs))
        if self._showing_group_table:
            self._apply_command_rows(procs, app)
        else:
            self._apply_process_rows(procs, app, include_synthetic=True)

    def _fail_cgroup_unavailable(self, exc: CgroupUnavailableError) -> None:
        if self._timer is not None:
            self._timer.stop()
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve to `AppMemApp` (SPEC.md "Tech" notes).
        self.app.fail_cgroup_unavailable(  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            str(exc)
        )

    def _show_gone(self) -> None:
        # Pick the rows on screen before leaving the drill-down: while drilled,
        # the table holds process rows, not the stale `_command_rows`.
        current = self._command_rows if self._showing_group_table else self._process_rows
        self._last_app = None
        self._drill_command = None
        self._set_rich("#title", Text(f"{self._name}   (app no longer running)"))
        self._set_rich("#status", Text(""))
        table = self._table()
        for key in current:
            table.remove_row(key)
        self._process_rows = {}
        self._command_rows = {}

    # --- title, above the table --------------------------------------------------

    def _set_title(self, app: AppStats, proc_count: int) -> None:
        self._last_app = app
        self._last_proc_count = proc_count
        self._render_title()

    def _render_title(self) -> None:
        if self._last_app is None:
            return
        app = self._last_app
        name_part = (
            Text(f"{app.name} › {self._drill_command}")  # noqa: RUF001 -- breadcrumb separator
            if self._drill_command is not None
            else Text(app.name)
        )
        parts: list[tuple[str, Text | None]] = [
            ("name", name_part),
            ("procs", Text(f"{self._last_proc_count} procs")),
            ("swap", Text(f"swap {size(app.swap)}")),
            ("ram", Text(f"RAM {size(app.ram)}")),
        ]
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        self._set_rich("#title", fit_line(parts, _TITLE_DROP_ORDER, width))

    # --- status line, above the footer --------------------------------------------

    def _selected_key(self) -> str | None:
        table = self._table()
        key, _index = self._current_selection(table)
        return key

    def _update_status_line(self) -> None:
        self._set_rich("#status", self._status_content())

    def _status_content(self) -> Text:
        if self._showing_group_table:
            return self._command_status_content()
        return self._process_status_content()

    def _process_status_content(self) -> Text:
        key = self._selected_key()
        row = self._process_rows.get(key) if key is not None else None
        if row is None:
            return Text("")
        if row.pid is None:
            # Synthetic row: `row.unit` already carries its one-line
            # explanation (SPEC.md "Process view").
            return Text(row.unit, style="dim italic")
        command = status_line_command(
            self._scope,
            row.unit,
            row.pid,
            self.app.size.width,  # pyright: ignore[reportUnknownMemberType]
        )
        return Text(command)

    def _command_status_content(self) -> Text:
        # Grouped mode is where the terminal use case lives (ghostty ->
        # claude), so it gets a status line too, not just the flat/drilled
        # views.
        key = self._selected_key()
        row = self._command_rows.get(key) if key is not None else None
        if row is None:
            return Text("")
        if key in (KERNEL_KEY, UNATTRIBUTED_KEY):
            text = KERNEL_UNIT_TEXT if key == KERNEL_KEY else UNATTRIBUTED_UNIT_TEXT
            return Text(text, style="dim italic")
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        if len(row.units) == 1:
            return Text(status_line_command(self._scope, row.units[0], None, width))
        # Several units share this command name: no single truthful command
        # covers all of them (e.g. the same shell run in two terminals).
        return Text(f"{len(row.units)} units, Enter lists the processes", style="dim")

    # --- row diffing: process rows --------------------------------------------

    def _process_cells(self, row: ProcessRow) -> list[str | Text]:
        return [
            _cell_value(key, _format_process_cell(key, row), dim=row.dim)
            for key, _label, _width in self._columns()
        ]

    def _apply_process_rows(
        self, procs: list[ProcStats], app: AppStats, *, include_synthetic: bool
    ) -> None:
        table = self._table()
        process_key = cast("ProcessSortKey", self._sort_key)
        real_rows = sort_process_rows(build_process_rows(procs), process_key, self._sort_reverse)
        # `kernel` then `unattributed`, always last, both dim (SPEC.md
        # "Definitions"). Omitted while drilled into one command: they cover
        # the whole app's residual memory, not that command's share of it.
        ordered = (
            [*real_rows, kernel_process_row(app), unattributed_process_row(app, procs)]
            if include_synthetic
            else real_rows
        )
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
        self._update_status_line()

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
        group_key = cast("GroupSortKey", self._sort_key)
        real_rows = sort_command_rows(build_command_rows(procs), group_key, self._sort_reverse)
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
        self._update_status_line()

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
        rows = self._command_rows if self._showing_group_table else self._process_rows
        if previous_key is not None and previous_key in rows:
            table.move_cursor(row=table.get_row_index(previous_key))
        else:
            table.move_cursor(row=min(previous_index, table.row_count - 1))

    # --- actions ----------------------------------------------------------------

    def _reset_sort_if_unsupported(self) -> None:
        if self._sort_key not in SHARED_SORT_KEYS:
            self._sort_key, self._sort_reverse = "total", True

    def _set_sort(self, column: str) -> None:
        key, reverse = next_sort_state(
            self._sort_key, self._sort_reverse, cast("ScreenSortKey", column)
        )
        self._sort_key, self._sort_reverse = key, reverse
        table = self._table()
        self.refresh_now()
        self._refresh_column_labels(table)

    def action_sort(self, column: str) -> None:
        self._set_sort(column)

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        self._set_sort(_key_str(event.column_key))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_status_line()  # live as the cursor moves, not just on Enter

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a command in grouped mode drills into its members. A
        # no-op everywhere else, including on the synthetic rows.
        if not self._showing_group_table:
            return
        key = _key_str(event.row_key)
        if key in (KERNEL_KEY, UNATTRIBUTED_KEY):
            return
        self._enter_drill(key)

    def _enter_drill(self, command: str) -> None:
        self._drill_command = command
        self._process_rows = {}
        self._reset_sort_if_unsupported()
        table = self._table()
        self._rebuild_columns(table)
        self.refresh_now()

    def action_toggle_group(self) -> None:
        self._grouped = not self._grouped
        self._drill_command = None
        self._reset_sort_if_unsupported()
        table = self._table()
        self._process_rows = {}
        self._command_rows = {}
        self._rebuild_columns(table)
        self.refresh_now()

    def action_back(self) -> None:
        if self._drill_command is not None:
            self._exit_drill()
            return
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _exit_drill(self) -> None:
        command = self._drill_command
        self._drill_command = None
        self._process_rows = {}
        self._command_rows = {}
        self._reset_sort_if_unsupported()
        table = self._table()
        self._rebuild_columns(table)
        self.refresh_now()  # repopulates `self._command_rows` fresh
        if command is not None and command in self._command_rows:
            table.move_cursor(row=table.get_row_index(command))

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())  # pyright: ignore[reportUnknownMemberType]
