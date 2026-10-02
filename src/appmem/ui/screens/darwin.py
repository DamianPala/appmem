"""Experimental Apple Silicon footprint screens."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, time
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static

from appmem.darwin_backend import DarwinApp, DarwinBackend, DarwinProcess, DarwinUnavailableError
from appmem.darwin_native import HostMemory
from appmem.fmt import format_delta, format_elapsed, size, truncate_name
from appmem.rate import Sample, update_rate
from appmem.render import escape_control_chars
from appmem.total import SessionCounter
from appmem.ui.darwin_header import render_host_header
from appmem.ui.darwin_rows import DarwinRow, build_rows, sort_rows, update_baseline
from appmem.ui.header import ThemeColors
from appmem.ui.host_panel import HostPanel, darwin_details
from appmem.ui.layout import build_footer
from appmem.ui.screens.live import LiveScreen

# Share existing palette contrast, ANSI conversion and locale detection with Linux.
from appmem.ui.screens.main import (
    _bar_fill_colour,  # pyright: ignore[reportPrivateUsage]
    _detect_ascii_bars,  # pyright: ignore[reportPrivateUsage]
    _rich_color,  # pyright: ignore[reportPrivateUsage]
)
from appmem.ui.table import RowTable


def _amount(value: int | None) -> str:
    return "?" if value is None else size(value)


def _read_error(had_data: bool) -> str:
    return (
        "Read unavailable; showing stale values; retrying"
        if had_data
        else "Read unavailable; retrying"
    )


@dataclass(frozen=True)
class _MainFrame:
    generation: int
    host: HostMemory
    apps: list[DarwinApp]


class DarwinMainTick(Message):
    bubble: ClassVar[bool] = False

    def __init__(self, generation: int, frame: _MainFrame | BaseException) -> None:
        super().__init__()
        self.generation = generation
        self.frame = frame


class DarwinMainScreen(LiveScreen):
    DEFAULT_CSS = """
    DarwinMainScreen #table { height: 1fr; }
    DarwinMainScreen Static { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h", "host", "host", show=False),
        Binding("f", "sort('footprint')", "sort memory", show=False),
        Binding("r", "sort('resident')", "sort resident", show=False),
        Binding("d", "sort('delta')", "sort growth", show=False),
        Binding("b", "reset_delta", "reset Δ", show=False),
        Binding("?", "help", "help", show=False),
    ]

    def __init__(self, backend: DarwinBackend, interval: float) -> None:
        super().__init__()
        self._backend = backend
        self._interval = interval
        self._baseline: dict[str, DarwinApp] = {}
        self._apps: list[DarwinApp] = []
        self._rows: dict[str, DarwinRow] = {}
        self._host: HostMemory | None = None
        self._sort_key = "footprint"
        self._reverse = True
        self._column_widths: tuple[int | None, ...] = ()
        self._resume_scrolls = False
        self._read_failed = False
        self._ascii_bars = _detect_ascii_bars()
        self._baseline_time: str | None = None
        self._baseline_started = 0.0
        self._swap_in_history: tuple[Sample, ...] = ()
        self._swap_out_history: tuple[Sample, ...] = ()
        self._swap_in_rate: int | None = None
        self._swap_out_rate: int | None = None
        self._swap_in_session = SessionCounter()
        self._swap_out_session = SessionCounter()

    def compose(self) -> ComposeResult:
        yield Static(id="header1")
        yield Static(id="header2")
        yield Static(id="header3")
        yield Static(id="header4")
        table: RowTable = RowTable(id="table")
        self._rebuild_columns(table)
        yield table
        yield Static(id="footer")

    def _table(self) -> RowTable:
        return self.query_one("#table", RowTable)

    def on_mount(self) -> None:
        self.watch(self._table(), "show_vertical_scrollbar", self._on_scrollbar, init=False)
        self.refresh_now()
        self._start_live_timer(self._interval, self._tick)

    def _on_scrollbar(self, before: bool, after: bool) -> None:
        if before != after:
            self.call_after_refresh(self._sync_columns)

    def _tick(self) -> None:
        self._launch_tick(self._read_tick)

    def _read_tick(self, generation: int) -> None:
        try:
            frame: _MainFrame | BaseException = _MainFrame(
                generation, self._backend.read_system(), self._backend.collect_apps()
            )
        except BaseException as exc:
            frame = exc
        self.post_message(DarwinMainTick(generation, frame))

    def on_darwin_main_tick(self, event: DarwinMainTick) -> None:
        self._finish_tick()
        if isinstance(event.frame, BaseException) and not isinstance(
            event.frame, (DarwinUnavailableError, OSError)
        ):
            raise event.frame
        if not self._accept_tick(event.generation):
            return
        if isinstance(event.frame, BaseException):
            self._read_failed = True
            self._render_header()
            return
        self._apply_frame(event.frame.host, event.frame.apps)

    def refresh_now(self, *, scroll: bool = False) -> None:
        try:
            self._apply_frame(
                self._backend.read_system(), self._backend.collect_apps(), scroll=scroll
            )
        except (DarwinUnavailableError, OSError):
            self._read_failed = True
            self._render_header()

    def on_screen_resume(self) -> None:
        if self._resumed_from_host_panel():
            return
        self._invalidate_tick()
        scroll, self._resume_scrolls = self._resume_scrolls, False
        self.refresh_now(scroll=scroll)

    def _apply_frame(
        self, host: HostMemory, apps: list[DarwinApp], *, scroll: bool = False
    ) -> None:
        now, wall = monotonic(), time()
        self._swap_in_session.update(host.swap_in_bytes)
        self._swap_out_session.update(host.swap_out_bytes)
        self._swap_in_history, self._swap_in_rate = update_rate(
            self._swap_in_history, now, host.swap_in_bytes, wall=wall, interval=self._interval
        )
        self._swap_out_history, self._swap_out_rate = update_rate(
            self._swap_out_history, now, host.swap_out_bytes, wall=wall, interval=self._interval
        )
        self._host = host
        if self._baseline_time is None:
            self._reset_baseline_clock()
        self._read_failed = False
        self._apps = apps
        self._baseline = update_baseline(apps, self._baseline)
        self._apply_rows(build_rows(apps, self._baseline), scroll=scroll)
        self._render_header()
        self._render_footer()

    def _render_header(self) -> None:
        self._notify_host_panel()
        if self._read_failed:
            self.query_one("#header1", Static).update(
                truncate_name(_read_error(self._host is not None), self.size.width)
            )
            return
        if self._host is None:
            return
        theme = self.app.current_theme  # pyright: ignore[reportUnknownMemberType]
        colors = ThemeColors(
            success=_rich_color(theme.success or "green"),
            warning=_rich_color(theme.warning or "yellow"),
            error=_rich_color(theme.error or "red"),
            primary=_bar_fill_colour(theme),
        )
        lines = render_host_header(
            self._host,
            self.size.width,
            colors=colors,
            ascii_bars=self._ascii_bars,
            baseline_time=self._baseline_time,
            swap_in_rate=self._swap_in_rate,
            swap_out_rate=self._swap_out_rate,
            baseline_elapsed=max(0, int(monotonic() - self._baseline_started)),
        )
        for index, line in enumerate(lines, 1):
            self.query_one(f"#header{index}", Static).update(line)

    def refresh_theme(self) -> None:
        self._render_header()

    def _render_footer(self) -> None:
        items = [
            (
                ("f", "d", "r")
                if self.size.width >= 100
                else ("f", "d")
                if self.size.width >= 65
                else ("f",),
                "sort",
            ),
            (("enter",), "procs"),
            (("b",), "reset Δ"),
            (("T",), "theme"),
            (("h",), "host"),
            (("?",), "help"),
            (("q",), "quit"),
        ]
        self.query_one("#footer", Static).update(
            build_footer(
                tuple(items),
                width=self.size.width,
                drop_order=("theme", "reset Δ", "procs", "sort"),
            )
        )

    def _host_content(self) -> str:
        if self._host is None:
            return "Host memory unavailable; waiting for a successful reading."
        stale = "Read failed; showing last successful reading.\n\n" if self._read_failed else ""
        elapsed = format_elapsed(max(0, int(monotonic() - self._baseline_started)))
        changes = (
            f"Changes   Δ shows memory changes since {(self._baseline_time or '?')[:5]}"
            f" ({elapsed} ago)."
        )
        content = (
            stale
            + darwin_details(
                self._host,
                (self._swap_in_rate, self._swap_out_rate),
                (self._swap_in_session.total, self._swap_out_session.total),
            )
            + "\n"
            + changes
        )
        return content.replace("·", "|").replace("Δ", "delta") if self._ascii_bars else content

    def action_host(self) -> None:
        self._host_panel_open = True
        self.app.push_screen(HostPanel(self, self._host_content, self.action_help))  # pyright: ignore[reportUnknownMemberType]

    def _specs(self, table: RowTable) -> list[tuple[str, str, int | None]]:
        specs: list[tuple[str, str, int | None]] = [
            ("app", "APP", None),
            ("footprint", "MEMORY", 12),
        ]
        if self.size.width >= 65:
            specs.append(("delta", "ΔMEM", 11))
        if self.size.width >= 100:
            specs.append(("resident", "RESIDENT", 12))
        specs.append(("procs", "PROCS", 7))
        usable = table.scrollable_content_region.width or self.size.width
        other = sum(2 + (column_width or 0) for key, _, column_width in specs if key != "app")
        specs[0] = ("app", "APP", max(8, min(32, usable - other - 2)))
        return specs

    def _rebuild_columns(self, table: RowTable) -> None:
        table.clear(columns=True)
        specs = self._specs(table)
        for key, label, width in specs:
            marker = " ▾" if self._reverse else " ▴"
            table.add_column(
                label + marker if key == self._sort_key else label, key=key, width=width
            )
        self._column_widths = tuple(width for _, _, width in specs)

    def _cells(self, row: DarwinRow, table: RowTable) -> list[Text]:
        app_width = self._specs(table)[0][2] or 32
        values = {
            "app": truncate_name(escape_control_chars(row.name), app_width),
            "footprint": _amount(row.footprint_bytes)
            + ("*" if row.partial and row.footprint_bytes is not None else ""),
            "delta": "?" if row.delta_bytes is None else format_delta(row.delta_bytes),
            "resident": _amount(row.resident_bytes)
            + ("*" if row.resident_partial and row.resident_bytes is not None else ""),
            "procs": str(row.procs),
        }
        return [
            Text(values[key], justify="left" if key == "app" else "right")
            for key, _, _ in self._specs(table)
        ]

    def _selected(self, table: RowTable) -> tuple[str | None, int]:
        return table.cursor_key, table.cursor_row

    def _restore(self, table: RowTable, key: str | None, index: int, scroll: bool) -> None:
        if table.row_count:
            target = (
                table.get_row_index(key)
                if key is not None and key in self._rows
                else min(index, table.row_count - 1)
            )
            table.move_cursor(row=target, scroll=scroll)

    def _apply_rows(self, rows: list[DarwinRow], *, scroll: bool) -> None:
        table = self._table()
        selected, index = self._selected(table)
        new_rows = {row.key: row for row in rows}
        changed_count = len(new_rows) != len(self._rows)
        for key in self._rows.keys() - new_rows.keys():
            table.remove_row(key)
        for key, row in new_rows.items():
            old = self._rows.get(key)
            if old is None:
                table.add_row(*self._cells(row, table), key=key)
            elif old != row:
                for column, before, after in zip(
                    table.column_keys, self._cells(old, table), self._cells(row, table), strict=True
                ):
                    if before.plain != after.plain:
                        table.update_cell(key, column, after)
        self._rows = new_rows
        table.reorder([row.key for row in sort_rows(rows, self._sort_key, self._reverse)])
        self._restore(table, selected, index, scroll)
        if changed_count:
            self.call_after_refresh(self._sync_columns)

    def _sync_columns(self, *, force: bool = False, preserve_scroll: bool = True) -> None:
        table = self._table()
        widths = tuple(width for _, _, width in self._specs(table))
        if widths == self._column_widths and not force:
            return
        selected, index = self._selected(table)
        previous_scroll_y = table.scroll_y
        self._rebuild_columns(table)
        for row in self._rows.values():
            table.add_row(*self._cells(row, table), key=row.key)
        table.reorder(
            [
                row.key
                for row in sort_rows(list(self._rows.values()), self._sort_key, self._reverse)
            ],
        )
        self._restore(table, selected, index, False)
        if preserve_scroll and previous_scroll_y > 0:
            table.scroll_to(y=previous_scroll_y, animate=False)

    def on_resize(self, event: events.Resize) -> None:
        if (self._sort_key == "delta" and self.size.width < 65) or (
            self._sort_key == "resident" and self.size.width < 100
        ):
            self._sort_key, self._reverse = "footprint", True
        self._render_header()
        self._sync_columns(force=True, preserve_scroll=False)
        table = self._table()
        table.move_cursor(table.cursor_row, scroll=True)
        self._render_footer()

    def action_sort(self, key: str) -> None:
        if key not in {column for column, _, _ in self._specs(self._table())}:
            return
        self._reverse = not self._reverse if self._sort_key == key else key != "app"
        self._sort_key = key
        self._sync_columns(force=True)
        table = self._table()
        selected, index = self._selected(table)
        table.reorder([row.key for row in sort_rows(list(self._rows.values()), key, self._reverse)])
        self._restore(table, selected, index, True)

    def on_row_table_header_selected(self, event: RowTable.HeaderSelected) -> None:
        self.action_sort(event.column_key)

    def _reset_baseline_clock(self) -> None:
        self._baseline_time = datetime.now().strftime("%H:%M")
        self._baseline_started = monotonic()

    def action_reset_delta(self) -> None:
        self.refresh_now(scroll=True)
        if self._read_failed:
            return
        self._reset_baseline_clock()
        self._baseline = {app.id: app for app in self._apps}
        self._apply_rows(build_rows(self._apps, self._baseline), scroll=True)
        self._render_header()

    def action_help(self) -> None:
        self.app.push_screen(DarwinHelpScreen())  # pyright: ignore[reportUnknownMemberType]

    def on_row_table_row_selected(self, event: RowTable.RowSelected) -> None:
        app_id = event.row_key
        if app_id in self._rows:
            self._resume_scrolls = True
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                DarwinProcessesScreen(self._backend, app_id, self._interval)
            )


@dataclass(frozen=True)
class _DetailFrame:
    generation: int
    app: DarwinApp | None


class DarwinDetailTick(Message):
    bubble: ClassVar[bool] = False

    def __init__(self, generation: int, frame: _DetailFrame | BaseException) -> None:
        super().__init__()
        self.generation = generation
        self.frame = frame


@dataclass(frozen=True)
class _DetailRow:
    key: str
    name: str
    pid: int | None
    footprint_bytes: int | None
    procs: int
    unreadable: int
    status: str
    resident_bytes: int | None = None
    resident_unreadable: int = 0


class DarwinProcessesScreen(LiveScreen):
    DEFAULT_CSS = """
    DarwinProcessesScreen #table { height: 1fr; }
    DarwinProcessesScreen Static { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f", "sort('footprint')", "sort memory", show=False),
        Binding("r", "sort('resident')", "sort resident", show=False),
        Binding("n", "sort('name')", "sort command", show=False),
        Binding("p", "sort('count')", "sort PID or count", show=False),
        Binding("u", "sort('unreadable')", "sort unreadable", show=False),
        Binding("g", "toggle_group", "group", show=False),
        Binding("escape", "back", "back", show=False),
        Binding("?", "help", "help", show=False),
    ]

    def __init__(self, backend: DarwinBackend, app_id: str, interval: float) -> None:
        super().__init__()
        self._backend = backend
        self._app_id = app_id
        self._interval = interval
        self._app: DarwinApp | None = None
        self._grouped = False
        self._command: str | None = None
        self._rows: dict[str, _DetailRow] = {}
        self._sort_key = "footprint"
        self._reverse = True
        self._column_widths: tuple[int | None, ...] = ()
        self._column_keys: tuple[str, ...] = ()
        self._read_failed = False

    def compose(self) -> ComposeResult:
        yield Static(id="title", markup=False)
        table: RowTable = RowTable(id="table")
        yield table
        yield Static(id="status", markup=False)
        yield Static(id="footer")

    def _table(self) -> RowTable:
        return self.query_one("#table", RowTable)

    def on_mount(self) -> None:
        self.watch(self._table(), "show_vertical_scrollbar", self._on_scrollbar, init=False)
        self.refresh_now()
        self._start_live_timer(self._interval, self._tick)

    def _on_scrollbar(self, before: bool, after: bool) -> None:
        if before != after:
            self.call_after_refresh(self._sync_columns)

    def _tick(self) -> None:
        self._launch_tick(self._read_tick)

    def _read_tick(self, generation: int) -> None:
        try:
            frame: _DetailFrame | BaseException = _DetailFrame(
                generation, self._backend.find_app(self._app_id)
            )
        except BaseException as exc:
            frame = exc
        self.post_message(DarwinDetailTick(generation, frame))

    def on_darwin_detail_tick(self, event: DarwinDetailTick) -> None:
        self._finish_tick()
        if isinstance(event.frame, BaseException) and not isinstance(
            event.frame, (DarwinUnavailableError, OSError)
        ):
            raise event.frame
        if not self._accept_tick(event.generation):
            return
        if isinstance(event.frame, BaseException):
            self._read_failed = True
            self._render_status()
            return
        self._app = event.frame.app
        self._read_failed = False
        self._render_detail()

    def refresh_now(self, *, scroll: bool = False) -> None:
        try:
            self._app = self._backend.find_app(self._app_id)
        except (DarwinUnavailableError, OSError):
            self._read_failed = True
            self._render_status()
            return
        self._read_failed = False
        self._render_detail(scroll=scroll)

    def _visible_processes(self) -> tuple[DarwinProcess, ...]:
        if self._app is None:
            return ()
        if self._command is None:
            return self._app.members
        return tuple(p for p in self._app.members if p.command == self._command)

    def _build_rows(self) -> list[_DetailRow]:
        if self._grouped and self._command is None:
            grouped: dict[str, list[DarwinProcess]] = {}
            for process in self._visible_processes():
                grouped.setdefault(process.command, []).append(process)
            rows: list[_DetailRow] = []
            for command, members in grouped.items():
                known = [p.footprint_bytes for p in members if p.footprint_bytes is not None]
                unreadable = len(members) - len(known)
                residents = [p.resident_bytes for p in members if p.resident_bytes is not None]
                rows.append(
                    _DetailRow(
                        command,
                        command,
                        None,
                        sum(known) if known else None,
                        len(members),
                        unreadable,
                        f"{len(known)} memory readable, {unreadable} unreadable processes; "
                        f"resident {len(residents)}/{len(members)} readable",
                        sum(residents) if residents else None,
                        len(members) - len(residents),
                    )
                )
            return rows
        return [
            _DetailRow(
                f"{p.pid}:{p.start_abstime}",
                p.command,
                p.pid,
                p.footprint_bytes,
                1,
                int(p.footprint_bytes is None),
                p.unavailable or escape_control_chars(p.path or "path unavailable"),
                p.resident_bytes,
                int(p.resident_bytes is None),
            )
            for p in self._visible_processes()
        ]

    def _sorted_rows(self, rows: list[_DetailRow]) -> list[_DetailRow]:
        if self._sort_key == "name":
            return sorted(
                rows, key=lambda row: (row.name.casefold(), row.key), reverse=self._reverse
            )
        if self._sort_key == "count":
            return sorted(
                rows,
                key=lambda row: (
                    row.procs if self._grouped and self._command is None else row.pid or 0,
                    row.name,
                ),
                reverse=self._reverse,
            )
        if self._sort_key == "unreadable":
            return sorted(rows, key=lambda row: (row.unreadable, row.name), reverse=self._reverse)
        attribute = "resident_bytes" if self._sort_key == "resident" else "footprint_bytes"
        known = [row for row in rows if getattr(row, attribute) is not None]
        unknown = [row for row in rows if getattr(row, attribute) is None]
        known.sort(key=lambda row: (getattr(row, attribute), row.name), reverse=self._reverse)
        return known + sorted(unknown, key=lambda row: row.name)

    def _specs(self, table: RowTable) -> list[tuple[str, str, int | None]]:
        grouped = self._grouped and self._command is None
        specs: list[tuple[str, str, int | None]] = []
        if not grouped:
            specs.append(("pid", "PID", 7))
        specs.extend((("name", "COMMAND", None), ("footprint", "MEMORY", 12)))
        if self.size.width >= 100:
            specs.append(("resident", "RESIDENT", 12))
        if grouped:
            specs.append(("count", "PROCS", 7))
            if self.size.width >= 75:
                specs.append(("unreadable", "UNREADABLE", 12))
        elif self.size.width >= 75:
            specs.append(("state", "STATE", 10))
        usable = table.scrollable_content_region.width or self.size.width
        fixed = sum(2 + (width or 0) for key, _, width in specs if key != "name")
        name_width = max(9, min(32, usable - fixed - 2))
        return [(key, label, name_width if key == "name" else width) for key, label, width in specs]

    def _rebuild_columns(self, table: RowTable) -> None:
        table.clear(columns=True)
        specs = self._specs(table)
        sort_column = "pid" if self._sort_key == "count" and not self._grouped else self._sort_key
        for key, label, width in specs:
            marker = " ▾" if self._reverse else " ▴"
            table.add_column(label + marker if key == sort_column else label, key=key, width=width)
        self._column_keys = tuple(key for key, _, _ in specs)
        self._column_widths = tuple(width for _, _, width in specs)

    def _cells(self, row: _DetailRow, table: RowTable) -> list[Text]:
        specs = self._specs(table)
        name_width = next(width for key, _, width in specs if key == "name") or 32
        values = {
            "pid": str(row.pid or ""),
            "name": truncate_name(escape_control_chars(row.name), name_width),
            "footprint": _amount(row.footprint_bytes)
            + ("*" if row.unreadable and row.footprint_bytes is not None else ""),
            "resident": _amount(row.resident_bytes)
            + ("*" if row.resident_unreadable and row.resident_bytes is not None else ""),
            "count": str(row.procs),
            "unreadable": str(row.unreadable),
            "state": "unreadable" if row.unreadable else "readable",
        }
        return [
            Text(values[key], justify="left" if key in ("name", "state") else "right")
            for key, _, _ in specs
        ]

    def _selection(self, table: RowTable) -> tuple[str | None, int]:
        return table.cursor_key, table.cursor_row

    def _restore(self, table: RowTable, key: str | None, index: int, *, scroll: bool) -> None:
        if table.row_count:
            target = (
                table.get_row_index(key) if key in self._rows else min(index, table.row_count - 1)
            )
            table.move_cursor(row=target, scroll=scroll)

    def _set_detail_row(self, table: RowTable, row: _DetailRow) -> None:
        old = self._rows.get(row.key)
        if old is None:
            table.add_row(*self._cells(row, table), key=row.key)
        elif old != row:
            for column, before, after in zip(
                table.column_keys, self._cells(old, table), self._cells(row, table), strict=True
            ):
                if before.plain != after.plain:
                    table.update_cell(row.key, column, after)

    def _apply_rows(
        self, rows: list[_DetailRow], *, force_columns: bool = False, scroll: bool = False
    ) -> None:
        table = self._table()
        selected, index = self._selection(table)
        scroll_y = table.scroll_y
        specs = self._specs(table)
        keys = tuple(key for key, _, _ in specs)
        widths = tuple(width for _, _, width in specs)
        rebuild = force_columns or keys != self._column_keys or widths != self._column_widths
        if rebuild:
            self._rebuild_columns(table)
            self._rows = {}
        new_rows = {row.key: row for row in rows}
        changed_count = len(new_rows) != len(self._rows)
        for key in self._rows.keys() - new_rows.keys():
            table.remove_row(key)
        for row in new_rows.values():
            self._set_detail_row(table, row)
        self._rows = new_rows
        table.reorder([row.key for row in self._sorted_rows(rows)])
        self._restore(table, selected, index, scroll=scroll)
        if not scroll:
            table.scroll_to(y=scroll_y, animate=False)
        if not scroll and scroll_y > 0:
            self.call_after_refresh(lambda: table.scroll_to(y=scroll_y, animate=False))
        if changed_count:
            self.call_after_refresh(self._sync_columns)

    def _sync_columns(self) -> None:
        table = self._table()
        specs = self._specs(table)
        if tuple(width for _, _, width in specs) != self._column_widths:
            self._apply_rows(list(self._rows.values()), force_columns=True)

    def _render_detail(self, *, force_columns: bool = False, scroll: bool = False) -> None:
        app = self._app
        if self._command is not None and not self._visible_processes():
            self._command = None
            force_columns = True
        if self._sort_key in ("unreadable", "resident") and self._sort_key not in {
            key for key, _, _ in self._specs(self._table())
        }:
            self._sort_key, self._reverse = "footprint", True
            force_columns = True
        title = (
            "Application vanished"
            if app is None
            else f"{escape_control_chars(app.name)}  memory {_amount(app.footprint_bytes)}"
        )
        self.query_one("#title", Static).update(truncate_name(title, self.size.width))
        self._apply_rows(self._build_rows(), force_columns=force_columns, scroll=scroll)
        self._render_status()
        sort_keys = ("f", "n", "p", "u") if "unreadable" in self._column_keys else ("f", "n", "p")
        if "resident" in self._column_keys:
            sort_keys += ("r",)
        footer: list[tuple[tuple[str, ...], str]] = [
            (sort_keys, "sort"),
            (("g",), "ungroup" if self._grouped else "group"),
        ]
        if self._grouped and self._command is None and self._rows:
            footer.append((("enter",), "members"))
        footer.extend(((("esc",), "back"), (("T",), "theme"), (("?",), "help"), (("q",), "quit")))
        self.query_one("#footer", Static).update(
            build_footer(footer, width=self.size.width, drop_order=("theme", "members", "sort"))
        )

    def _render_status(self) -> None:
        table = self._table()
        if self._read_failed:
            self.query_one("#status", Static).update(
                truncate_name(_read_error(self._app is not None), self.size.width)
            )
            return
        status = "Captured process memory; ? means unreadable"
        if self._command is not None:
            status = f"Command {escape_control_chars(self._command)}"
        elif table.row_count:
            selected, _ = self._selection(table)
            if selected is not None and selected in self._rows:
                status = self._rows[selected].status
        self.query_one("#status", Static).update(truncate_name(status, self.size.width))

    def on_row_table_row_highlighted(self, event: RowTable.RowHighlighted) -> None:
        self._render_status()

    def on_row_table_row_selected(self, event: RowTable.RowSelected) -> None:
        if self._grouped and self._command is None and event.row_key in self._rows:
            self._command = event.row_key
            self._invalidate_tick()
            self._render_detail(force_columns=True, scroll=True)

    def action_sort(self, key: str) -> None:
        if key not in ("footprint", "resident", "name", "count", "unreadable"):
            return
        if key in ("unreadable", "resident") and key not in self._column_keys:
            return
        self._reverse = not self._reverse if self._sort_key == key else key != "name"
        self._sort_key = key
        self._apply_rows(list(self._rows.values()), force_columns=True, scroll=True)

    def on_row_table_header_selected(self, event: RowTable.HeaderSelected) -> None:
        key = event.column_key
        self.action_sort({"pid": "count"}.get(key, key))

    def action_toggle_group(self) -> None:
        self._grouped = not self._grouped
        self._command = None
        self._invalidate_tick()
        self._render_detail(force_columns=True, scroll=True)

    def action_back(self) -> None:
        if self._command is not None:
            command = self._command
            self._command = None
            self._invalidate_tick()
            self._render_detail(force_columns=True, scroll=True)
            table = self._table()
            if command in self._rows:
                table.move_cursor(row=table.get_row_index(command), scroll=True)
        else:
            self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_help(self) -> None:
        self.app.push_screen(DarwinHelpScreen())  # pyright: ignore[reportUnknownMemberType]

    def on_resize(self, event: events.Resize) -> None:
        self._render_detail(force_columns=True)
        table = self._table()
        table.move_cursor(table.cursor_row, scroll=True)


class DarwinHelpScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "back", show=False),
        Binding("?", "back", "back", show=False),
        Binding("q", "back", "back", show=False),
    ]

    DEFAULT_CSS = """
    DarwinHelpScreen #darwin-help-scroll { height: 1fr; scrollbar-gutter: stable; }
    DarwinHelpScreen #darwin-help-text { height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Static("macOS help  esc/?/q close", id="darwin-help-title")
        with VerticalScroll(id="darwin-help-scroll"):
            yield Static(
                self._body(self.app.size.width - 2),  # pyright: ignore[reportUnknownMemberType]
                id="darwin-help-text",
                markup=False,
            )
        yield Static(build_footer(((("esc",), "close"),)), id="footer")

    def on_mount(self) -> None:
        self.query_one("#darwin-help-scroll", VerticalScroll).focus()

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._refresh_body)

    def _refresh_body(self) -> None:
        scroll = self.query_one("#darwin-help-scroll", VerticalScroll)
        self.query_one("#darwin-help-text", Static).update(
            self._body(scroll.size.width - scroll.scrollbar_size_vertical)
        )

    @staticmethod
    def _body(width: int) -> str:
        definitions = (
            (
                "Swap in/out",
                "Host activity averaged over about 10 seconds in this session. "
                "Native counters count page-rounded compressed segments transferred to/from swap "
                "files, including housekeeping, not logical app bytes or SSD throughput. "
                "Unknown means insufficient/unavailable samples; 0 B/s is measured zero. "
                "Clock discontinuities reset rates; b resets growth only. "
                "Headers from 105 columns show out with written bytes since boot; "
                "h shows Read/Written rates, "
                "boot totals and exact totals since AppMem started. Session totals survive "
                "navigation and rate resets. Missing initial counters or any decrease leave "
                "that direction unavailable; temporary missing readings retain its baseline.",
            ),
            (
                "MEMORY",
                "App physical footprint, including native compression accounting; "
                "not resident RAM, reclaimable memory or an Activity Monitor total.",
            ),
            (
                "RESIDENT",
                "Resident shared/file-backed pages can double count between processes. "
                "Do not add to MEMORY or subtract to infer swap.",
            ),
            (
                "ΔMEM",
                "Change since the app's first complete sample or b reset. Partial current "
                "samples stay unknown; recovery uses the retained complete baseline. Reopened "
                "apps start fresh. The header clock is the session/reset epoch.",
            ),
            (
                "* / ?",
                "Partial known sum / unknown for that metric. MEMORY and RESIDENT "
                "have independent coverage; grouping can also be partial.",
            ),
            (
                "RAM",
                "Host physical usage: physical - (native free - speculative) - file-backed. "
                "Reserved/unaccounted memory stays used. Wired and purgeable overlap used.",
            ),
            (
                "file-backed",
                "Not all immediately available. Free excludes speculative and "
                "is not an available-memory estimate.",
            ),
            ("Compress", "Logical data -> physical RAM, with a ratio when both are nonzero."),
            (
                "Swap",
                "Used / currently allocated space, allocated dynamically. Zero total "
                "means none allocated; per-app swap is unavailable.",
            ),
            ("Pressure", "Native kernel state: normal, warning or critical; not Linux PSI."),
        )
        keys = (
            ("click header", "sort; click again reverses"),
            ("click row", "select; double click opens"),
            ("f / d / r", "main: sort MEMORY / ΔMEM / RESIDENT (when visible)"),
            (
                "f / r / n / p / u",
                "details: sort MEMORY / RESIDENT / command / PID or count / unreadable",
            ),
            ("up/down PgUp PgDn", "move"),
            ("Home / End", "first / last row"),
            ("Enter", "app details or grouped command members"),
            ("g", "details: toggle grouping by command"),
            ("Esc", "back"),
            ("b", "reset Δ and the displayed baseline clock"),
            ("h", "main dashboard: live host memory; h/Esc close"),
            ("T / Ctrl+P", "theme"),
            ("?", "help"),
            ("q / Ctrl+C", "quit live view; esc/?/q close help"),
        )

        def items(entries: tuple[tuple[str, str], ...], column: int) -> str:
            return "\n".join(
                textwrap.fill(
                    body,
                    width=max(width, column + 10),
                    initial_indent=f"{label:<{column}}",
                    subsequent_indent=" " * column,
                )
                for label, body in entries
            )

        grouping = (
            "h on the main dashboard opens live, scrollable host memory details. "
            "A trailing … (ASCII >) means data hidden for space, not an unavailable reading. "
            "Bundles follow the outermost .app path; bundleless processes follow "
            "the nearest app ancestor or a session root. Shared launchd XPC/WebKit "
            "services can remain separate, so related footprints may be omitted. "
            "App sums do not equal host RAM. macOS 15+ Apple Silicon (experimental)."
        )
        return (
            items(definitions, 13)
            + "\n\n"
            + textwrap.fill(grouping, width=max(width, 20))
            + "\n\nKeys:\n"
            + items(keys, 22)
        )

    def action_back(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
