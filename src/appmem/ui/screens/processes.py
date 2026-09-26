"""Process view: per-process table for one app, with grouping by command and
a drill-down into one command's members (SPEC.md "Process view").

Mirrors `MainScreen`'s tick/diff/sort patterns (row diffing via row keys,
`update_cell` only on changed text, cursor restore by key). The backend
finds the app again every tick, so a unit added or
replaced while the screen is open is picked up; the app is considered gone
only once no unit maps to its identity any more.

Three display shapes share this one screen: flat (one row per process),
grouped by command (one row per command name, `g`), and a drill-down into one
command's member processes (Enter on a command row) -- flat process columns
again, scoped to that command's PIDs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import ClassVar, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Static

from appmem.backend import Backend
from appmem.collect import CgroupUnavailableError, MemoryStatUnavailableError
from appmem.fmt import format_age, size, status_line_command, truncate_name
from appmem.model import AppStats, ProcStats
from appmem.render import escape_control_chars
from appmem.ui.layout import build_footer, fit_line
from appmem.ui.process_rows import (
    KERNEL_KEY,
    KERNEL_UNIT_TEXT,
    SHARED_SORT_KEYS,
    SYNTHETIC_KEYS,
    UNATTRIBUTED_KEY,
    UNATTRIBUTED_UNIT_TEXT,
    ZSWAP_POOL_KEY,
    ZSWAP_POOL_UNIT_TEXT,
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
    zswap_pool_command_row,
    zswap_pool_process_row,
)
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.live import LiveScreen
from appmem.ui.table import RowTable

_PROCESS_COLUMNS: tuple[tuple[str, str, int | None], ...] = (
    ("pid", "PID", 7),  # pid_max 4194304: 7 digits
    ("name", "NAME", None),
    ("ram", "RAM", 10),
    ("swap", "SWAP", 10),
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
    ("ram", "RAM", 10),
    ("swap", "SWAP", 10),
    ("total", "TOTAL", 10),
    ("procs", "PROCS", 6),
)
_LEFT_ALIGNED = {"name", "unit"}

_NARROW_WIDTH = 95

# `_with_name_width` shrinks the NAME column (with truncation, toward
# `_NAME_MIN_WIDTH` if needed) at every width, so RAM, SWAP and TOTAL/PROCS
# stay whole on screen instead of being pushed past the terminal edge by a
# long name (SPEC.md "Process view"). UNIT is excluded from the budget: it
# already scrolls sideways rather than shrinking.
_NAME_MIN_WIDTH = 8
_NAME_MAX_WIDTH = 32
# `RowTable`'s own cell padding (1 cell each side of every column).
_CELL_PADDING = 2

# Below the footer's natural width, drop items lowest priority first;
# `help`, `back`/`groups` and `quit` are never in this list, so they always
# stay. `theme` is the lowest priority of all (SPEC.md "Command line").
_FOOTER_DROP_ORDER = ("theme", "members", "group", "sort")

# Process-view title drop order -- procs count first, then swap; the
# app/breadcrumb name and RAM are always kept.
_TITLE_DROP_ORDER = ("procs", "swap")

_SYNTHETIC_UNIT_TEXT: dict[str, str] = {
    KERNEL_KEY: KERNEL_UNIT_TEXT,
    ZSWAP_POOL_KEY: ZSWAP_POOL_UNIT_TEXT,
    UNATTRIBUTED_KEY: UNATTRIBUTED_UNIT_TEXT,
}


def _format_process_cell(key: str, row: ProcessRow, *, name_cap: int = _NAME_MAX_WIDTH) -> str:
    if key == "pid":
        return "" if row.pid is None else str(row.pid)
    if key == "name":
        return truncate_name(escape_control_chars(row.name), name_cap)
    if key == "swap":
        return size(row.swap)
    if key == "ram":
        return size(row.ram)
    if key == "total":
        return size(row.total)
    if key == "age":
        return "" if row.age_seconds is None else format_age(row.age_seconds)
    return escape_control_chars(row.unit)  # "unit"


def _format_command_cell(key: str, row: CommandRow, *, name_cap: int = _NAME_MAX_WIDTH) -> str:
    if key == "name":
        return truncate_name(escape_control_chars(row.name), name_cap)
    if key == "swap":
        return size(row.swap)
    if key == "ram":
        return size(row.ram)
    if key == "total":
        return size(row.total)
    return "" if row.procs is None else str(row.procs)  # "procs"


def _cell_value(key: str, text: str, *, dim: bool = False) -> Text:
    # Always a `Text`: `RowTable` renders a cell's `.plain` text and base
    # `.style` literally, never through markup parsing, so a process or
    # unit name containing `[bold]`-style brackets shows literally.
    style = "dim italic" if dim else ""
    if key in _LEFT_ALIGNED:
        return Text(text, style=style)
    return Text(text, justify="right", style=style)


@dataclass(frozen=True)
class _TickResult:
    """A background tick's outcome, plus the generation it was read under --
    so a result whose context changed while it was in flight (`g`, drilling
    in/out, the screen covered/resumed) is discarded instead of applied
    stale (SPEC.md "Process view")."""

    generation: int
    app: AppStats | None = None
    procs: list[ProcStats] | None = None
    cgroup_error: CgroupUnavailableError | None = None


class TickDone(Message):
    """A tick's thread has finished: the read's result, or the exception it
    raised (see `MainScreen`'s `TickDone`)."""

    bubble: ClassVar[bool] = False

    def __init__(self, payload: _TickResult | Exception) -> None:
        super().__init__()
        self.payload = payload


def _tick_worker(backend: Backend, scope: str, name: str, *, generation: int) -> _TickResult:
    """Runs in a thread (SPEC.md "Tech"): blocking `/proc`/`/sys` reads
    only, no Textual calls."""
    try:
        app = backend.find_app(scope, name, strict=False)
        procs = backend.read_procs(app) if app is not None else []
    except CgroupUnavailableError as exc:
        return _TickResult(generation=generation, cgroup_error=exc)
    except (MemoryStatUnavailableError, OSError, ValueError):
        return _TickResult(generation=generation)
    return _TickResult(generation=generation, app=app, procs=procs)


class ProcessesScreen(LiveScreen):
    """Per-process table for one app (SPEC.md "Process view")."""

    # `RowTable` (a bare `ScrollView`) doesn't run away to fit its content the
    # way `DataTable`'s own `height: auto; max-height: 100%` default used to,
    # but `1fr` is kept explicit so the table only ever claims the space left
    # over from the title and the footer, regardless of row count.
    DEFAULT_CSS = """
    ProcessesScreen #table { height: 1fr; }
    ProcessesScreen #title, ProcessesScreen #status, ProcessesScreen #footer {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
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
        backend: Backend,
        name: str,
        scope: str,
        interval: float,
        initial_sort: tuple[ProcessSortKey, bool],
    ) -> None:
        super().__init__()
        self._backend = backend
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
        self._column_widths: tuple[int | None, ...] = ()
        """Bumped on every context change (`g`, drilling in/out, the app
        going away, the screen covered/resumed). A background result carries
        the generation it was read under; `_apply_tick` discards one that
        no longer matches, so a slow read that outlives a later
        context change can't overwrite what that change already drew."""

    @property
    def _showing_group_table(self) -> bool:
        return self._grouped and self._drill_command is None

    def compose(self) -> ComposeResult:
        self._age_shown = self._show_age_column()
        yield Static(id="title", markup=False)
        table: RowTable = RowTable(id="table")
        self._rebuild_columns(table)
        yield table
        yield Static(id="status")
        yield Static(self._footer_text(), id="footer")

    def _footer_items(self) -> tuple[tuple[tuple[str, ...], str], ...]:
        """Only the keys that do something in the current mode (SPEC.md
        "Process view"): `enter` (drill into a command's members) shows
        only in grouped mode outside a drill-down, since it's a no-op
        everywhere else, including on the synthetic rows. `esc` is labelled
        `groups` while drilled (it returns to the grouped list) and `back`
        otherwise. `s`/`r`/`t`, `g`, `T`, `?` and `q` all act in every mode,
        so they're never hidden (SPEC.md "Command line": `T` opens the theme
        picker from the process view too, same as the main view)."""
        items: list[tuple[tuple[str, ...], str]] = [(("r", "s", "t"), "sort"), (("g",), "group")]
        if self._showing_group_table:
            items.append((("enter",), "members"))
        items.append((("T",), "theme"))
        items.append((("?",), "help"))
        items.append((("esc",), "groups" if self._drill_command is not None else "back"))
        items.append((("q",), "quit"))
        return tuple(items)

    def _footer_text(self) -> Text:
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        return build_footer(self._footer_items(), width=width, drop_order=_FOOTER_DROP_ORDER)

    def _update_footer(self) -> None:
        self._set_rich("#footer", self._footer_text())

    def on_mount(self) -> None:
        self.watch(self._table(), "show_vertical_scrollbar", self._on_scrollbar_change, init=False)
        self.refresh_now()
        self._start_live_timer(self._interval, self._tick)

    def _on_scrollbar_change(self, previous: bool, current: bool) -> None:
        if previous != current:
            self.call_after_refresh(self._sync_columns)

    def _tick(self) -> None:
        # Covered by help: skip the work, `on_screen_resume` catches up.
        # A previous tick's read is still in flight: skip, don't stack a
        # second one (SPEC.md "Tech": at most one read in flight per screen).
        self._launch_tick(self._tick_read)

    def _tick_read(self, generation: int) -> None:
        # Thread side of a tick, a plain thread posting a message rather than
        # a Textual worker, for the reason given on `MainScreen._tick_read`:
        # finished workers stay alive until a cyclic GC pass, which Python
        # 3.14 can defer for hours.
        try:
            payload: _TickResult | Exception = _tick_worker(
                self._backend,
                self._scope,
                self._name,
                generation=generation,
            )
        except Exception as exc:  # re-raised on the UI thread by `on_tick_done`
            payload = exc
        self.post_message(TickDone(payload))

    def on_tick_done(self, event: TickDone) -> None:
        self._finish_tick()
        if isinstance(event.payload, Exception):
            raise event.payload  # a collector bug: exit loudly, see `MainScreen.on_tick_done`
        self._apply_tick(event.payload)

    def _apply_tick(self, result: _TickResult) -> None:
        if result.cgroup_error is not None:
            self._fail_cgroup_unavailable(result.cgroup_error)
            return
        if result.procs is None:  # transient read/parse failure this tick
            return
        if not self._accept_tick(result.generation):
            # `g`, a drill-down, or the screen being covered/left changed the
            # context while this read was in flight: discard rather than
            # show a stale table (SPEC.md "Tech").
            return
        self._apply_refresh(result.app, result.procs, scroll=False)

    def on_resize(self, event: events.Resize) -> None:
        # Title and footer never wrap -- recompute on every resize, not just
        # on the next tick. The status line's ellipsis point moves too.
        self._render_title()
        self._update_status_line()
        self._sync_columns()
        self._update_footer()

    def _table(self) -> RowTable:
        return self.query_one("#table", RowTable)

    def _set_rich(self, selector: str, content: Text) -> None:
        widget = self.query_one(selector, Static)
        if widget.content != content:
            widget.update(content)

    # --- columns --------------------------------------------------------------

    def _show_age_column(self) -> bool:
        return self.app.size.width >= _NARROW_WIDTH  # pyright: ignore[reportUnknownMemberType]

    def _sync_columns(self) -> None:
        """Rebuild the table whenever a resize actually changes the computed
        column widths, not just when AGE crosses its own visibility
        threshold: `_with_name_width` recomputes NAME's width on every
        resize, independent of AGE's own threshold, and a resize that only
        moved NAME used to leave the table built for the old width, pushing
        TOTAL/PROCS past the terminal edge or leaving NAME narrower than it
        needs to be (SPEC.md "Process view")."""
        show = self._show_age_column()
        if show != self._age_shown:
            self._age_shown = show
            sort_hidden = not show and self._sort_key == "age" and not self._showing_group_table
            if sort_hidden:
                self._sort_key, self._sort_reverse = "total", True
        else:
            sort_hidden = False
        table = self._table()
        widths = tuple(width for _key, _label, width in self._columns(table))
        if widths == self._column_widths:
            return
        self._rebuild_current_table()
        if sort_hidden:
            self.refresh_now()  # the cached rows are still in AGE order: re-sort them

    def _columns(self, table: RowTable) -> tuple[tuple[str, str, int | None], ...]:
        base = _GROUP_COLUMNS if self._showing_group_table else self._process_column_base()
        return self._with_name_width(base, table)

    def _process_column_base(self) -> tuple[tuple[str, str, int | None], ...]:
        return _PROCESS_COLUMNS if self._age_shown else _PROCESS_COLUMNS_NARROW

    def _with_name_width(
        self, columns: tuple[tuple[str, str, int | None], ...], table: RowTable
    ) -> tuple[tuple[str, str, int | None], ...]:
        """Shrink the (otherwise auto-sized) NAME column, toward
        `_NAME_MIN_WIDTH` if needed, at every width -- not only below a
        fixed threshold -- so RAM, SWAP and TOTAL/PROCS stay fully on screen
        instead of being pushed past the terminal edge by a long name
        (SPEC.md "Process view"): a single "narrow mode" cutoff doesn't
        survive the column set changing (flat vs grouped, AGE shown or
        hidden), so a resize (or a mode switch) above the old fixed
        threshold could still leave a numeric column off screen. UNIT is
        excluded from the reserved budget: it already scrolls sideways
        rather than shrinking. The table's own vertical scrollbar (when
        shown) narrows its usable width too, so it comes out of the same
        budget as the numeric columns."""
        total_width = table.scrollable_content_region.width or self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        reserved = sum(
            _CELL_PADDING + (width or 0)
            for key, _label, width in columns
            if key not in ("name", "unit")
        )
        budget = total_width - reserved - _CELL_PADDING
        if budget >= _NAME_MAX_WIDTH:
            return columns
        name_width = max(budget, _NAME_MIN_WIDTH)
        return tuple(
            (key, label, name_width) if key == "name" else (key, label, width)
            for key, label, width in columns
        )

    def _name_cap(self, table: RowTable) -> int:
        for key, _label, width in self._columns(table):
            if key == "name":
                return width if width is not None else _NAME_MAX_WIDTH
        return _NAME_MAX_WIDTH

    def _header_label(self, key: str, label: str) -> str:
        if key != self._sort_key:
            return label
        marker = "▾" if self._sort_reverse else "▴"
        return f"{label} {marker}"

    def _rebuild_columns(self, table: RowTable) -> None:
        table.clear(columns=True)
        specs = self._columns(table)
        for key, label, width in specs:
            table.add_column(self._header_label(key, label), width=width, key=key)
        self._column_widths = tuple(width for _key, _label, width in specs)

    def _refresh_column_labels(self, table: RowTable) -> None:
        for key, label, _width in self._columns(table):
            table.set_column_label(key, self._header_label(key, label))

    def _rebuild_current_table(self) -> None:
        """Rebuild columns and repopulate from the already-sorted cached rows
        (no re-collection): used when only column visibility changed (AGE
        hidden/shown by width), not the underlying data shape."""
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        previous_scroll_y = table.scroll_y
        self._rebuild_columns(table)
        if self._showing_group_table:
            for key, row in self._command_rows.items():
                table.add_row(*self._command_cells(row, table), key=key)
        else:
            for key, row in self._process_rows.items():
                table.add_row(*self._process_cells(row, table), key=key)
        self._restore_selection(table, previous_key, previous_index, scroll=False)
        if previous_scroll_y > 0:
            table.scroll_to(y=previous_scroll_y, animate=False)
        self._update_status_line()

    # --- collection tick ----------------------------------------------------

    def _read_once(self) -> tuple[AppStats | None, list[ProcStats]] | None:
        """One synchronous read, or `None` on a transient failure.

        Only OS-level read failures and half-written `/proc`/`/sys` parse
        errors (`OSError`/`ValueError`, same treatment as
        `MemoryStatUnavailableError`) are turned into `None`; the caller
        keeps whatever was on screen and tries again later (SPEC.md
        "Behaviour details"). The directory vanishing is fatal, same as
        always.
        """
        try:
            # `strict=False`: a transient `memory.stat` read failure on the
            # user root raises `MemoryStatUnavailableError` (skip the tick)
            # rather than `CgroupUnavailableError` (fatal) -- only the
            # directory vanishing is fatal here.
            app = self._backend.find_app(self._scope, self._name, strict=False)
            procs = self._backend.read_procs(app) if app is not None else []
            return app, procs
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
            return None
        except MemoryStatUnavailableError:
            return None  # transient this tick: keep the last data on screen, try again next tick
        except (OSError, ValueError):
            return None  # transient read/parse failure: same treatment, try again next tick

    def refresh_now(self, *, scroll: bool = False) -> None:
        """Collect and redraw immediately.

        `scroll` (default `False`, a tick) says whether restoring the
        cursor's row is also allowed to scroll the viewport: a periodic tick
        must not, or mouse-wheel scrolling would snap back to the cursor on
        every refresh; an explicit user action (a sort) passes `True` so the
        selected row stays visible (SPEC.md "Process view"). Always
        synchronous, on the calling thread -- unlike the periodic timer tick
        (`_tick`), which reads in a thread so a slow collector never blocks
        key handling (SPEC.md "Tech").

        A failing read (`_read_once` returning `None`) is not applied at
        all, leaving the last frame on screen; applying a successful one
        runs outside any `try`, so a programming error there (e.g. `RowTable.
        reorder`'s `ValueError` invariant check) still propagates and
        ends the session instead of being swallowed alongside a transient
        read failure. Switching mode (`g`, Enter, Esc) does not go through
        here -- see `_switch_mode`.
        """
        result = self._read_once()
        if result is None:
            return
        self._apply_refresh(*result, scroll=scroll)

    def _apply_refresh(
        self,
        app: AppStats | None,
        procs: list[ProcStats],
        *,
        scroll: bool,
    ) -> None:
        if app is None:
            self._show_gone()
            return
        if self._drill_command is not None:
            members = [proc for proc in procs if proc.name == self._drill_command]
            # The breadcrumb's counts are the command's, as on its grouped row.
            swap, ram = sum(p.swap for p in members), sum(p.ram for p in members)
            self._set_title(replace(app, swap=swap, ram=ram), len(members))
            self._apply_process_rows(members, app, include_synthetic=False, scroll=scroll)
            return
        self._set_title(app, len(procs))
        if self._showing_group_table:
            self._apply_command_rows(procs, app, scroll=scroll)
        else:
            self._apply_process_rows(procs, app, include_synthetic=True, scroll=scroll)

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
        was_drilled = self._drill_command is not None
        self._last_app = None
        self._drill_command = None
        self._generation += 1
        name = escape_control_chars(self._name)
        self._set_rich("#title", Text(f"{name}   (app no longer running)"))
        self._set_rich("#status", Text(""))
        table = self._table()
        self._process_rows = {}
        self._command_rows = {}
        if was_drilled:
            # Ending a drill-down changes which columns apply
            # (`_showing_group_table` depends on `_drill_command`, now cleared):
            # rebuild them, or a later reappearance would add rows shaped for
            # one column set into a table still built for the other (SPEC.md
            # "Process view"). Also drop a process-only sort key (e.g. `age`)
            # that the group columns don't support, same as `_exit_drill`.
            self._reset_sort_if_unsupported()
            self._rebuild_columns(table)  # also clears the rows just emptied above
        else:
            for key in current:
                table.remove_row(key)
        self._update_footer()  # a drill-down just ended: `esc` is `back` again

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
            Text(
                f"{escape_control_chars(app.name)} › {escape_control_chars(self._drill_command)}"  # noqa: RUF001 -- breadcrumb separator
            )
            if self._drill_command is not None
            else Text(escape_control_chars(app.name))
        )
        parts: list[tuple[str, Text | None]] = [
            ("name", name_part),
            ("procs", Text(f"{self._last_proc_count} procs")),
            ("ram", Text(f"RAM {size(app.ram)}")),
            ("swap", Text(f"swap {size(app.swap)}")),
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
            escape_control_chars(row.unit),
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
        if key in SYNTHETIC_KEYS:
            text = _SYNTHETIC_UNIT_TEXT[key]
            return Text(text, style="dim italic")
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        if len(row.units) == 1:
            unit = escape_control_chars(row.units[0])
            return Text(status_line_command(self._scope, unit, None, width))
        # Several units share this command name: no single truthful command
        # covers all of them (e.g. the same shell run in two terminals).
        return Text(f"{len(row.units)} units, Enter lists the processes", style="dim")

    # --- row diffing: process rows --------------------------------------------

    def _process_cells(self, row: ProcessRow, table: RowTable) -> list[Text]:
        name_cap = self._name_cap(table)
        return [
            _cell_value(key, _format_process_cell(key, row, name_cap=name_cap), dim=row.dim)
            for key, _label, _width in self._columns(table)
        ]

    def _apply_process_rows(
        self, procs: list[ProcStats], app: AppStats, *, include_synthetic: bool, scroll: bool
    ) -> None:
        # The table's columns already match this mode: a mode switch (`g`,
        # Enter, Esc) only calls this once its own read has succeeded and it
        # has rebuilt them itself (`_switch_mode`); a periodic tick never
        # changes the mode (SPEC.md "Process view").
        table = self._table()
        process_key = cast("ProcessSortKey", self._sort_key)
        real_rows = sort_process_rows(build_process_rows(procs), process_key, self._sort_reverse)
        # `kernel`, then `zswap pool` when the app has one, then
        # `unattributed`, always last, all dim (SPEC.md "Definitions").
        # Omitted while drilled into one command: they cover the whole app's
        # residual memory, not that command's share of it.
        synthetic = [kernel_process_row(app)]
        if app.zswap_pool > 0:
            synthetic.append(zswap_pool_process_row(app))
        synthetic.append(unattributed_process_row(app, procs))
        ordered = [*real_rows, *synthetic] if include_synthetic else real_rows
        new_by_key = {row.key: row for row in ordered}
        previous_key, previous_index = self._current_selection(table)
        row_count_changed = len(new_by_key) != len(self._process_rows)

        for key in self._process_rows.keys() - new_by_key.keys():
            table.remove_row(key)
        for key, row in new_by_key.items():
            old = self._process_rows.get(key)
            if old is not None:
                self._update_process_cells(table, key, old, row)
            else:
                table.add_row(*self._process_cells(row, table), key=key)
        self._process_rows = new_by_key

        table.reorder([row.key for row in ordered])
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)
        self._update_status_line()
        if row_count_changed:
            # More or fewer rows can show or hide the vertical scrollbar,
            # which narrows the NAME budget without any resize event; its
            # size is only known after the next layout.
            self.call_after_refresh(self._sync_columns)

    def _update_process_cells(
        self, table: RowTable, key: str, old: ProcessRow, row: ProcessRow
    ) -> None:
        name_cap = self._name_cap(table)
        for col_key, _label, _width in self._columns(table):
            old_text = _format_process_cell(col_key, old, name_cap=name_cap)
            new_text = _format_process_cell(col_key, row, name_cap=name_cap)
            if old_text != new_text:
                table.update_cell(key, col_key, _cell_value(col_key, new_text, dim=row.dim))

    # --- row diffing: grouped (command) rows -----------------------------------

    def _command_cells(self, row: CommandRow, table: RowTable) -> list[Text]:
        name_cap = self._name_cap(table)
        return [
            _cell_value(key, _format_command_cell(key, row, name_cap=name_cap), dim=row.dim)
            for key, _label, _width in self._columns(table)
        ]

    def _apply_command_rows(self, procs: list[ProcStats], app: AppStats, *, scroll: bool) -> None:
        # Same precondition as `_apply_process_rows`: the columns already
        # match this mode.
        table = self._table()
        group_key = cast("GroupSortKey", self._sort_key)
        real_rows = sort_command_rows(build_command_rows(procs), group_key, self._sort_reverse)
        synthetic = [kernel_command_row(app)]
        if app.zswap_pool > 0:
            synthetic.append(zswap_pool_command_row(app))
        synthetic.append(unattributed_command_row(app, procs))
        ordered = [*real_rows, *synthetic]
        new_by_key = {row.key: row for row in ordered}
        previous_key, previous_index = self._current_selection(table)
        row_count_changed = len(new_by_key) != len(self._command_rows)

        for key in self._command_rows.keys() - new_by_key.keys():
            table.remove_row(key)
        for key, row in new_by_key.items():
            old = self._command_rows.get(key)
            if old is not None:
                self._update_command_cells(table, key, old, row)
            else:
                table.add_row(*self._command_cells(row, table), key=key)
        self._command_rows = new_by_key

        table.reorder([row.key for row in ordered])
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)
        self._update_status_line()
        if row_count_changed:
            # Same reasoning as `_apply_process_rows`: a scrollbar appearing
            # or disappearing narrows/widens the NAME budget with no resize.
            self.call_after_refresh(self._sync_columns)

    def _update_command_cells(
        self, table: RowTable, key: str, old: CommandRow, row: CommandRow
    ) -> None:
        name_cap = self._name_cap(table)
        for col_key, _label, _width in self._columns(table):
            old_text = _format_command_cell(col_key, old, name_cap=name_cap)
            new_text = _format_command_cell(col_key, row, name_cap=name_cap)
            if old_text != new_text:
                table.update_cell(key, col_key, _cell_value(col_key, new_text, dim=row.dim))

    # --- selection, shared by both modes -----------------------------------------

    def _current_selection(self, table: RowTable) -> tuple[str | None, int]:
        index = table.cursor_row
        if table.row_count == 0:
            return None, index
        return table.cursor_key, index

    def _restore_selection(
        self,
        table: RowTable,
        previous_key: str | None,
        previous_index: int,
        *,
        scroll: bool,
    ) -> None:
        if table.row_count == 0:
            return
        rows = self._command_rows if self._showing_group_table else self._process_rows
        if previous_key is not None and previous_key in rows:
            table.move_cursor(row=table.get_row_index(previous_key), scroll=scroll)
        else:
            table.move_cursor(row=min(previous_index, table.row_count - 1), scroll=scroll)

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
        self.refresh_now(scroll=True)  # explicit sort action: keep the row visible
        self._refresh_column_labels(table)

    def action_sort(self, column: str) -> None:
        self._set_sort(column)

    def on_row_table_header_selected(self, event: RowTable.HeaderSelected) -> None:
        self._set_sort(event.column_key)

    def on_row_table_row_highlighted(self, event: RowTable.RowHighlighted) -> None:
        self._update_status_line()  # live as the cursor moves, not just on Enter

    def on_row_table_row_selected(self, event: RowTable.RowSelected) -> None:
        # Enter on a command in grouped mode drills into its members. A
        # no-op everywhere else, including on the synthetic rows.
        if not self._showing_group_table:
            return
        key = event.row_key
        if key in SYNTHETIC_KEYS:
            return
        self._enter_drill(key)

    def _switch_mode(
        self,
        *,
        grouped: bool,
        drill_command: str | None,
        scroll: bool,
        cursor_key: str | None = None,
    ) -> None:
        """Common path for `g`, Enter and Esc: read once for the new mode
        first, and only if it succeeds commit the mode change, rebuild the
        table's columns and apply the fresh rows. A failing read leaves the
        old mode's state and its table completely untouched -- neither
        `_grouped`/`_drill_command` nor the table's columns change -- so they
        never drift out of step with each other; the user can just press the
        key again (SPEC.md "Process view").

        `cursor_key` is the row the cursor should land on afterwards when
        it's known in advance (the command just left by Esc), overriding the
        row-applying methods' own previous-position fallback, which has
        nothing to fall back to right after a rebuild.
        """
        result = self._read_once()
        if result is None:
            return
        app, procs = result
        self._grouped = grouped
        self._drill_command = drill_command
        self._generation += 1
        self._reset_sort_if_unsupported()
        table = self._table()
        self._rebuild_columns(table)
        self._process_rows = {}
        self._command_rows = {}
        self._apply_refresh(app, procs, scroll=scroll)
        if cursor_key is not None and cursor_key in self._command_rows:
            table.move_cursor(row=table.get_row_index(cursor_key), scroll=scroll)
        self._update_footer()

    def _enter_drill(self, command: str) -> None:
        self._switch_mode(
            grouped=self._grouped, drill_command=command, scroll=True
        )  # explicit Enter: a whole new table shape

    def action_toggle_group(self) -> None:
        self._switch_mode(
            grouped=not self._grouped, drill_command=None, scroll=True
        )  # explicit `g` key press: a whole new table shape

    def action_back(self) -> None:
        if self._drill_command is not None:
            self._exit_drill()
            return
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def _exit_drill(self) -> None:
        command = self._drill_command
        self._switch_mode(
            grouped=self._grouped, drill_command=None, scroll=False, cursor_key=command
        )

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())  # pyright: ignore[reportUnknownMemberType]
