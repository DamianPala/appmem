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
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey, RowKey

from appmem.collect import (
    AppStats,
    CgroupUnavailableError,
    MemoryStatUnavailableError,
    SystemStats,
    Unit,
    filter_visible_apps,
    group_apps,
    read_system,
    unit_scope,
)
from appmem.collect import find_units as collect_find_units
from appmem.collect import read_unit as collect_read_unit
from appmem.fmt import format_delta, size, truncate_name
from appmem.ui.header import format_line1, format_line2
from appmem.ui.layout import build_footer
from appmem.ui.process_rows import initial_process_sort
from appmem.ui.rows import (
    DEFAULT_SORT_KEY,
    DEFAULT_SORT_REVERSE,
    Row,
    SortKey,
    build_rows,
    next_sort_state,
    reset_baseline,
    row_key,
    sort_rows,
    update_baseline,
)
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.processes import ProcessesScreen
from appmem.ui.table_order import reorder_rows

# Key caps (reverse video); at full width the plain text is exactly
# " r s t d sort  enter procs  x system  c cache  z reset Δ  ? help  q quit".
# `d` (sort by ΔSWAP) drops out of the "sort" item's own key caps -- not the
# whole item -- while the Δ columns are hidden by width (SPEC.md "Main view":
# a key that does nothing in the current view doesn't appear).
_FOOTER_DROP_ORDER = ("reset Δ", "cache", "system", "procs", "sort")

# Below this width, ΔSWAP/ΔRAM are hidden (SPEC.md "Main view"). Re-shown
# above it.
_NARROW_WIDTH = 95

# Width `None` = auto: the APP column grows to the longest name (capped at 32 by
# `truncate_name` in `_format_cell`), so short names are never cut short of that.
_BASE_COLUMNS: tuple[tuple[SortKey, str, int | None], ...] = (
    ("app", "APP", None),
    ("ram", "RAM", 10),
    ("swap", "SWAP", 10),
)
_CACHE_COLUMN: tuple[SortKey, str, int | None] = ("cache", "CACHE", 10)
_TOTAL_COLUMN: tuple[SortKey, str, int | None] = ("total", "TOTAL", 10)
_DELTA_COLUMNS: tuple[tuple[SortKey, str, int | None], ...] = (
    ("delta_ram", "ΔRAM", 9),
    ("delta_swap", "ΔSWAP", 9),
)
_PROCS_COLUMN: tuple[SortKey, str, int | None] = ("procs", "PROCS", 6)
_DELTA_KEYS = frozenset({"delta_swap", "delta_ram"})


def _format_cell(key: SortKey, row: Row) -> str:
    if key == "app":
        label = row.name if row.scope == "user" else f"{row.name} [sys]"
        return truncate_name(label)
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
        self._baseline: dict[tuple[str, str], AppStats] = {}
        self._baseline_time = datetime.now()
        self._rows: dict[str, Row] = {}
        self._last_apps: list[AppStats] = []
        self._timer: Timer | None = None
        self._delta_columns_shown = True
        self._young_baseline = True
        self._delta_restyle_pending = False
        self._last_stats: SystemStats | None = None

    def compose(self) -> ComposeResult:
        self._delta_columns_shown = self._show_delta_columns()
        yield Static(id="header1")
        yield Static(id="header2")
        table: DataTable[str | Text] = DataTable(id="table", cursor_type="row")
        self._rebuild_columns(table)
        yield table
        yield Static(self._footer_text(), id="footer")

    def _footer_items(self) -> tuple[tuple[tuple[str, ...], str], ...]:
        # `d` (sort by ΔSWAP) is a no-op while the Δ columns are hidden by
        # width (`action_sort` refuses it), so it drops out of the "sort"
        # item's own key caps rather than staying as dead text (SPEC.md
        # "Main view").
        sort_keys = ("r", "s", "t", "d") if self._delta_columns_shown else ("r", "s", "t")
        return (
            (sort_keys, "sort"),
            (("enter",), "procs"),
            (("x",), "system"),
            (("c",), "cache"),
            (("z",), "reset Δ"),
            (("?",), "help"),
            (("q",), "quit"),
        )

    def _footer_text(self) -> Text:
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        return build_footer(self._footer_items(), width=width, drop_order=_FOOTER_DROP_ORDER)

    def on_mount(self) -> None:
        self.refresh_now()
        self._timer = self.set_interval(self._interval, self._tick)

    def _tick(self) -> None:
        # Covered by the process view or help: skip the work, `on_screen_resume` catches up.
        if self.is_active:
            self.refresh_now()

    def on_screen_resume(self) -> None:
        self.refresh_now()  # no stale numbers after Esc from the process view

    def on_resize(self, event: events.Resize) -> None:
        # Header, columns and footer never wrap -- recompute on every resize,
        # not just on the next tick.
        self._update_header_line1()
        self._sync_delta_columns()
        self._update_footer()

    def _table(self) -> DataTable[str | Text]:
        # `isinstance()` (which `query_one` uses) rejects a parameterized generic,
        # so we query by the bare class and `cast` to the concrete cell type.
        return cast("DataTable[str | Text]", self.query_one("#table", DataTable))

    # --- collection tick ----------------------------------------------------

    def _collect_apps(self) -> list[AppStats]:
        # `strict=False`: this runs every tick, not just at start-up, so a
        # transient `memory.stat` read failure on the user root raises
        # `MemoryStatUnavailableError` (skip the tick) rather than
        # `CgroupUnavailableError` (fatal) -- only the directory vanishing is
        # fatal here. The pre-start check in `cli.py` calls `find_units`
        # directly, `strict=True`.
        unit_paths = collect_find_units(
            self._root, self._uid, include_system=self._show_system, strict=False
        )
        units = [
            Unit(path=path, stats=unit_stats, scope=unit_scope(self._root, path))
            for path in unit_paths
            if (unit_stats := collect_read_unit(path)) is not None
        ]
        return filter_visible_apps(group_apps(units))

    def refresh_now(self, *, scroll: bool = False) -> None:
        """Collect and redraw immediately, instead of waiting for the next tick.

        Called on mount, on the refresh timer, and by the actions (`x`, `z`)
        that need their effect to show up right away rather than after a full
        interval. Also the deterministic re-tick hook the Textual pilot tests
        use instead of racing the real timer (SPEC.md "Tests").

        `scroll` (default `False`, a tick) says whether restoring the
        cursor's row is also allowed to scroll the viewport: a periodic tick
        must not, or mouse-wheel scrolling would snap back to the cursor on
        every refresh; an explicit user action (a sort) passes `True` so the
        selected row stays visible after it moves (SPEC.md "Main view").

        Only the collector reads are guarded: a transient OS-level read
        failure or a parse error from a half-written `/proc`/`/sys` file
        (`OSError`/`ValueError`, same treatment as `MemoryStatUnavailableError`)
        skips this tick and keeps the last frame, the next tick recovers
        (SPEC.md "Behaviour details"). Applying the result to the screen runs
        outside the `try`, so a programming error there (e.g. `reorder_rows`'s
        `ValueError` invariant check) still propagates and ends the session,
        instead of being swallowed alongside a transient read failure.
        """
        try:
            stats = read_system(self._root, self._uid)
            apps = self._collect_apps()
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
            return
        except MemoryStatUnavailableError:
            return  # transient this tick: keep the last data on screen, try again next tick
        except (OSError, ValueError):
            return  # transient read/parse failure: same treatment, try again next tick
        self._apply_refresh(stats, apps, scroll=scroll)

    def _apply_refresh(self, stats: SystemStats, apps: list[AppStats], *, scroll: bool) -> None:
        self._last_apps = apps
        self._baseline = update_baseline(apps, self._baseline)
        young_now = self._baseline_age() < 60
        if young_now != self._young_baseline:
            self._delta_restyle_pending = True
        self._young_baseline = young_now
        self._apply_rows(build_rows(apps, self._baseline), scroll=scroll)
        self._update_header(stats)

    def _fail_cgroup_unavailable(self, exc: CgroupUnavailableError) -> None:
        # No traceback, exit 1, JSON line after the terminal is restored
        # (SPEC.md "Errors").
        if self._timer is not None:
            self._timer.stop()
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve to `AppMemApp` (SPEC.md "Tech" notes).
        self.app.fail_cgroup_unavailable(  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            str(exc)
        )

    def _baseline_age(self) -> float:
        return (datetime.now() - self._baseline_time).total_seconds()

    def _update_header(self, stats: SystemStats) -> None:
        self._last_stats = stats
        self._update_header_line1()
        self._update_header_line2()

    def _update_header_line1(self) -> None:
        if self._last_stats is None:
            return
        widget = self.query_one("#header1", Static)
        content = format_line1(
            self._last_stats,
            self.app.size.width,  # pyright: ignore[reportUnknownMemberType]
        )
        if widget.content != content:
            widget.update(content)

    def _update_header_line2(self) -> None:
        self._set_static("#header2", format_line2(self._baseline_time, datetime.now()))

    def _update_footer(self) -> None:
        widget = self.query_one("#footer", Static)
        content = self._footer_text()
        if widget.content != content:
            widget.update(content)

    def _set_static(self, selector: str, text: str) -> None:
        # Skip unchanged text: every `update` costs a layout pass and a repaint.
        widget = self.query_one(selector, Static)
        if widget.content != text:
            widget.update(text)

    # --- columns --------------------------------------------------------------

    def _show_delta_columns(self) -> bool:
        return self.app.size.width >= _NARROW_WIDTH  # pyright: ignore[reportUnknownMemberType]

    def _sync_delta_columns(self) -> None:
        show = self._show_delta_columns()
        if show == self._delta_columns_shown:
            return
        self._delta_columns_shown = show
        # The active sort column can go away with ΔSWAP/ΔRAM: fall back to the
        # default sort instead of an invisible one (SPEC.md "Main view"),
        # extended here to the width-based hiding above.
        if not show and self._sort_key in _DELTA_KEYS:
            self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
        self._rebuild_table(scroll=False)  # a resize, not an explicit selection action

    def _column_specs(self) -> list[tuple[SortKey, str, int | None]]:
        specs = list(_BASE_COLUMNS)
        if self._show_cache:
            specs.append(_CACHE_COLUMN)
        specs.append(_TOTAL_COLUMN)
        if self._delta_columns_shown:
            specs.extend(_DELTA_COLUMNS)
        specs.append(_PROCS_COLUMN)
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

    def _cell_value(self, key: SortKey, row: Row) -> Text:
        # Always a literal `Text`, never a plain `str`: `DataTable` renders a `str`
        # cell through `Text.from_markup`, so an app name containing `[bold]`-style
        # brackets would otherwise be parsed as markup instead of shown literally
        # (SPEC.md "Behaviour details").
        text = _format_cell(key, row)
        if key == "app":
            return Text(text)
        # A Δ cell is dim while its glyph is the small-delta `·`, or while the
        # whole baseline is still under 60 s old.
        dim = key in _DELTA_KEYS and (text == "·" or self._young_baseline)
        return Text(text, justify="right", style="dim" if dim else "")

    def _row_cells(self, row: Row) -> list[str | Text]:
        return [self._cell_value(key, row) for key, _label, _width in self._column_specs()]

    def _update_row_cells(
        self, table: DataTable[str | Text], old: Row, row: Row, *, force_delta_restyle: bool
    ) -> None:
        # Only cells whose text changed: any `update_cell` invalidates the whole
        # DataTable render cache, so a no-op update still repaints every row.
        # Δ cells are the one exception: the baseline crossing 60 s changes
        # their dim style without necessarily changing their text.
        for key, _label, _width in self._column_specs():
            restyle = force_delta_restyle and key in _DELTA_KEYS
            if restyle or _format_cell(key, old) != _format_cell(key, row):
                table.update_cell(row_key(row.name, row.scope), key, self._cell_value(key, row))

    def _current_selection(self, table: DataTable[str | Text]) -> tuple[str | None, int]:
        index = table.cursor_row
        if table.row_count == 0:
            return None, index
        row_key_obj, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        return _key_str(row_key_obj), index

    def _restore_selection(
        self,
        table: DataTable[str | Text],
        previous_key: str | None,
        previous_index: int,
        *,
        scroll: bool,
    ) -> None:
        if table.row_count == 0:
            return
        if previous_key is not None and previous_key in self._rows:
            table.move_cursor(row=table.get_row_index(previous_key), scroll=scroll)
        else:
            table.move_cursor(row=min(previous_index, table.row_count - 1), scroll=scroll)

    def _resort(self, table: DataTable[str | Text]) -> None:
        ordered = sort_rows(self._rows.values(), self._sort_key, self._sort_reverse)
        reorder_rows(table, [row_key(row.name, row.scope) for row in ordered])

    def _apply_rows(self, rows: list[Row], *, scroll: bool) -> None:
        table = self._table()
        new_by_key = {row_key(row.name, row.scope): row for row in rows}
        previous_key, previous_index = self._current_selection(table)
        force_delta_restyle = self._delta_restyle_pending
        self._delta_restyle_pending = False

        for key in self._rows.keys() - new_by_key.keys():
            table.remove_row(key)
        for key, row in new_by_key.items():
            old = self._rows.get(key)
            if old is not None:
                self._update_row_cells(table, old, row, force_delta_restyle=force_delta_restyle)
            else:
                table.add_row(*self._row_cells(row), key=key)
        self._rows = new_by_key

        self._resort(table)
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)

    # --- table rebuilds -----------------------------------------------------------

    def _rebuild_table(self, *, scroll: bool) -> None:
        """Rebuild the DataTable's columns and repopulate its rows after a
        change to which columns are visible (CACHE toggle, Δ columns hidden or
        shown by width), preserving sort and selection."""
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        self._rebuild_columns(table)
        for key, row in self._rows.items():
            table.add_row(*self._row_cells(row), key=key)
        self._resort(table)
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)

    # --- actions ----------------------------------------------------------------

    def _set_sort(self, column: SortKey) -> None:
        self._sort_key, self._sort_reverse = next_sort_state(
            self._sort_key, self._sort_reverse, column
        )
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        self._resort(table)
        self._refresh_column_labels(table)
        # Explicit user action (key or header click): keep the selected row
        # visible even though the resort may have moved it far from where it
        # was on screen (SPEC.md "Main view").
        self._restore_selection(table, previous_key, previous_index, scroll=True)

    def action_sort(self, column: str) -> None:
        if column in _DELTA_KEYS and not self._delta_columns_shown:
            return  # `d` while ΔSWAP is hidden by width: no invisible sort
        self._set_sort(cast("SortKey", column))

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        self._set_sort(cast("SortKey", _key_str(event.column_key)))

    def action_toggle_cache(self) -> None:
        self._show_cache = not self._show_cache
        # The active sort column can go away with the CACHE column: fall back
        # to the default sort instead of an invisible one (SPEC.md "Main
        # view").
        if not self._show_cache and self._sort_key == "cache":
            self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
        self._rebuild_table(scroll=True)  # explicit `c` key press

    def action_toggle_system(self) -> None:
        self._show_system = not self._show_system
        self.refresh_now(scroll=True)  # explicit `x` key press

    def action_reset_delta(self) -> None:
        # `z` takes a fresh sample before resetting, so the baseline and its
        # timestamp describe the same instant (SPEC.md "Definitions").
        try:
            apps = self._collect_apps()
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
            return
        except MemoryStatUnavailableError:
            return  # transient this tick: leave the baseline untouched, try again next tick
        except (OSError, ValueError):
            return  # transient read/parse failure: same treatment, try again next tick
        self._last_apps = apps
        self._baseline = reset_baseline(apps)
        self._baseline_time = datetime.now()
        if not self._young_baseline:
            self._delta_restyle_pending = True
        self._young_baseline = True
        self._apply_rows(build_rows(apps, self._baseline), scroll=True)  # explicit `z` key press
        self._update_header_line2()

    def action_help(self) -> None:
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.push_screen(HelpScreen())  # pyright: ignore[reportUnknownMemberType]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = _key_str(event.row_key)
        app = next((app for app in self._last_apps if row_key(app.name, app.scope) == key), None)
        if app is None:  # row vanished between the click and the event
            return
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ProcessesScreen(
                root=self._root,
                uid=self._uid,
                include_system=self._show_system,
                name=app.name,
                scope=app.scope,
                interval=self._interval,
                initial_sort=initial_process_sort(self._sort_key, self._sort_reverse),
            )
        )
