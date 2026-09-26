"""Experimental Apple Silicon footprint screens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static

from appmem.darwin_backend import DarwinApp, DarwinBackend, DarwinProcess, DarwinUnavailableError
from appmem.darwin_native import HostMemory
from appmem.fmt import format_delta, size, truncate_name
from appmem.render import escape_control_chars
from appmem.ui.darwin_rows import DarwinRow, build_rows, sort_rows, update_baseline
from appmem.ui.layout import build_footer
from appmem.ui.screens.live import LiveScreen
from appmem.ui.table import RowTable


def _amount(value: int | None) -> str:
    return "?" if value is None else size(value)


def _host_lines(host: HostMemory, width: int) -> tuple[str, str, str]:
    pressure = {1: "normal", 2: "warning", 4: "critical"}.get(host.pressure_level or 0)
    return (
        truncate_name(
            f"Physical {size(host.physical_bytes)}  Free {size(host.free_bytes)}  "
            f"Wired {size(host.wired_bytes)}",
            width,
        ),
        truncate_name(
            f"Compressor physical {size(host.compressor_physical_bytes)}  "
            f"logical {size(host.compressor_logical_bytes)}  "
            f"Swap {size(host.swap_used_bytes)} allocated",
            width,
        ),
        truncate_name(
            f"Native pressure {pressure or 'unavailable'}  "
            "growth: per-app baseline  macOS experimental",
            width,
        ),
    )


@dataclass(frozen=True)
class _MainFrame:
    generation: int
    host: HostMemory
    apps: list[DarwinApp]


class DarwinMainTick(Message):
    bubble: ClassVar[bool] = False

    def __init__(self, frame: _MainFrame | Exception) -> None:
        super().__init__()
        self.frame = frame


class DarwinMainScreen(LiveScreen):
    DEFAULT_CSS = """
    DarwinMainScreen #table { height: 1fr; }
    DarwinMainScreen Static { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f", "sort('footprint')", "sort footprint", show=False),
        Binding("d", "sort('delta')", "sort growth", show=False),
        Binding("b", "reset_delta", "reset growth", show=False),
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

    def compose(self) -> ComposeResult:
        yield Static(id="header1")
        yield Static(id="header2")
        yield Static(id="header3")
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
            frame: _MainFrame | Exception = _MainFrame(
                generation, self._backend.read_system(), self._backend.collect_apps()
            )
        except (DarwinUnavailableError, OSError) as exc:
            frame = exc
        self.post_message(DarwinMainTick(frame))

    def on_darwin_main_tick(self, event: DarwinMainTick) -> None:
        self._finish_tick()
        if isinstance(event.frame, Exception) or not self._accept_tick(event.frame.generation):
            return
        self._apply_frame(event.frame.host, event.frame.apps)

    def refresh_now(self, *, scroll: bool = False) -> None:
        try:
            self._apply_frame(
                self._backend.read_system(), self._backend.collect_apps(), scroll=scroll
            )
        except (DarwinUnavailableError, OSError):
            return

    def _apply_frame(
        self, host: HostMemory, apps: list[DarwinApp], *, scroll: bool = False
    ) -> None:
        self._host = host
        self._apps = apps
        self._baseline = update_baseline(apps, self._baseline)
        self._apply_rows(build_rows(apps, self._baseline), scroll=scroll)
        self._render_header()
        self._render_footer()

    def _render_header(self) -> None:
        if self._host is None:
            return
        for index, line in enumerate(_host_lines(self._host, self.size.width), 1):
            self.query_one(f"#header{index}", Static).update(line)

    def refresh_theme(self) -> None:
        self._render_header()

    def _render_footer(self) -> None:
        items = [
            (("f", "d") if self.size.width >= 65 else ("f",), "sort"),
            (("enter",), "procs"),
            (("b",), "reset growth"),
            (("T",), "theme"),
            (("?",), "help"),
            (("q",), "quit"),
        ]
        self.query_one("#footer", Static).update(
            build_footer(
                tuple(items),
                width=self.size.width,
                drop_order=("theme", "reset growth", "procs", "sort"),
            )
        )

    def _specs(self, table: RowTable) -> list[tuple[str, str, int | None]]:
        specs: list[tuple[str, str, int | None]] = [
            ("app", "APP", None),
            ("footprint", "FOOTPRINT", 12),
        ]
        if self.size.width >= 65:
            specs.append(("delta", "ΔFOOT", 11))
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
        if self._sort_key == "delta" and self.size.width < 65:
            self._sort_key, self._reverse = "footprint", True
        self._render_header()
        self._sync_columns(force=True, preserve_scroll=False)
        self._render_footer()

    def action_sort(self, key: str) -> None:
        if key == "delta" and self.size.width < 65:
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

    def action_reset_delta(self) -> None:
        self.refresh_now(scroll=True)
        self._baseline = {app.id: app for app in self._apps}
        self._apply_rows(build_rows(self._apps, self._baseline), scroll=True)
        self._render_header()

    def action_help(self) -> None:
        self.app.push_screen(DarwinHelpScreen())  # pyright: ignore[reportUnknownMemberType]

    def on_row_table_row_selected(self, event: RowTable.RowSelected) -> None:
        app_id = event.row_key
        if app_id in self._rows:
            self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                DarwinProcessesScreen(self._backend, app_id, self._interval)
            )


@dataclass(frozen=True)
class _DetailFrame:
    generation: int
    app: DarwinApp | None


class DarwinDetailTick(Message):
    bubble: ClassVar[bool] = False

    def __init__(self, frame: _DetailFrame | Exception) -> None:
        super().__init__()
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


class DarwinProcessesScreen(LiveScreen):
    DEFAULT_CSS = """
    DarwinProcessesScreen #table { height: 1fr; }
    DarwinProcessesScreen Static { text-wrap: nowrap; text-overflow: ellipsis; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f", "sort('footprint')", "sort footprint", show=False),
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
            frame: _DetailFrame | Exception = _DetailFrame(
                generation, self._backend.find_app(self._app_id)
            )
        except (DarwinUnavailableError, OSError) as exc:
            frame = exc
        self.post_message(DarwinDetailTick(frame))

    def on_darwin_detail_tick(self, event: DarwinDetailTick) -> None:
        self._finish_tick()
        if isinstance(event.frame, Exception) or not self._accept_tick(event.frame.generation):
            return
        self._app = event.frame.app
        self._render_detail()

    def refresh_now(self, *, scroll: bool = False) -> None:
        try:
            self._app = self._backend.find_app(self._app_id)
        except (DarwinUnavailableError, OSError):
            return
        self._render_detail()

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
                rows.append(
                    _DetailRow(
                        command,
                        command,
                        None,
                        sum(known) if known else None,
                        len(members),
                        unreadable,
                        f"{len(known)} readable, {unreadable} unreadable processes",
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
        known = [row for row in rows if row.footprint_bytes is not None]
        unknown = [row for row in rows if row.footprint_bytes is None]
        known.sort(key=lambda row: (row.footprint_bytes or 0, row.name), reverse=self._reverse)
        return known + sorted(unknown, key=lambda row: row.name)

    def _specs(self, table: RowTable) -> list[tuple[str, str, int | None]]:
        grouped = self._grouped and self._command is None
        specs: list[tuple[str, str, int | None]] = []
        if not grouped:
            specs.append(("pid", "PID", 7))
        specs.extend((("name", "COMMAND", None), ("footprint", "FOOTPRINT", 12)))
        if grouped:
            specs.append(("count", "PROCS", 7))
            if self.size.width >= 75:
                specs.append(("unreadable", "UNREADABLE", 11))
        elif self.size.width >= 75:
            specs.append(("state", "STATE", 10))
        usable = table.scrollable_content_region.width or self.size.width
        fixed = sum(2 + (width or 0) for key, _, width in specs if key != "name")
        name_width = max(8, min(32, usable - fixed - 2))
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

    def _restore(self, table: RowTable, key: str | None, index: int) -> None:
        if table.row_count:
            target = (
                table.get_row_index(key) if key in self._rows else min(index, table.row_count - 1)
            )
            table.move_cursor(row=target, scroll=False)

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

    def _apply_rows(self, rows: list[_DetailRow], *, force_columns: bool = False) -> None:
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
        self._restore(table, selected, index)
        table.scroll_to(y=scroll_y, animate=False)
        if scroll_y > 0:
            self.call_after_refresh(lambda: table.scroll_to(y=scroll_y, animate=False))
        if changed_count:
            self.call_after_refresh(self._sync_columns)

    def _sync_columns(self) -> None:
        table = self._table()
        specs = self._specs(table)
        if tuple(width for _, _, width in specs) != self._column_widths:
            self._apply_rows(list(self._rows.values()), force_columns=True)

    def _render_detail(self, *, force_columns: bool = False) -> None:
        app = self._app
        if self._command is not None and not self._visible_processes():
            self._command = None
            force_columns = True
        if self._sort_key == "unreadable" and "unreadable" not in {
            key for key, _, _ in self._specs(self._table())
        }:
            self._sort_key, self._reverse = "footprint", True
            force_columns = True
        title = (
            "Application vanished"
            if app is None
            else f"{escape_control_chars(app.name)}  footprint {_amount(app.footprint_bytes)}"
        )
        self.query_one("#title", Static).update(truncate_name(title, self.size.width))
        self._apply_rows(self._build_rows(), force_columns=force_columns)
        self._render_status()
        sort_keys = ("f", "n", "p", "u") if "unreadable" in self._column_keys else ("f", "n", "p")
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
        status = "Captured process footprints; ? means unreadable"
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
            self._render_detail(force_columns=True)

    def action_sort(self, key: str) -> None:
        if key not in ("footprint", "name", "count", "unreadable"):
            return
        if key == "unreadable" and "unreadable" not in self._column_keys:
            return
        self._reverse = not self._reverse if self._sort_key == key else key != "name"
        self._sort_key = key
        self._apply_rows(list(self._rows.values()), force_columns=True)

    def on_row_table_header_selected(self, event: RowTable.HeaderSelected) -> None:
        key = event.column_key
        self.action_sort({"pid": "count"}.get(key, key))

    def action_toggle_group(self) -> None:
        self._grouped = not self._grouped
        self._command = None
        self._invalidate_tick()
        self._render_detail(force_columns=True)

    def action_back(self) -> None:
        if self._command is not None:
            self._command = None
            self._invalidate_tick()
            self._render_detail(force_columns=True)
        else:
            self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]

    def action_help(self) -> None:
        self.app.push_screen(DarwinHelpScreen())  # pyright: ignore[reportUnknownMemberType]

    def on_resize(self, event: events.Resize) -> None:
        self._render_detail(force_columns=True)


class DarwinHelpScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "back", "back", show=False)]

    def compose(self) -> ComposeResult:
        yield Static(
            "macOS 15+ Apple Silicon (experimental)\n\n"
            "FOOTPRINT is native process physical footprint. It is not resident RAM, "
            "reclaimable memory, or an Activity Monitor total.\n"
            "* marks a partial app total; ? means all member footprints are unreadable. "
            "Growth stays unknown until a complete sample sets that app's baseline. "
            "The first complete sample shows zero. A partial current sample shows unknown; "
            "recovery compares with the retained complete baseline.\n"
            "Bundleless processes follow the nearest app ancestor or a separate session root. "
            "Missing ancestry can make grouping partial.\n"
            "Host Free is free physical pages, not available memory. Compressor physical "
            "and logical sizes differ. Native pressure is a kernel state, not PSI. "
            "Zero global swap means none allocated. Per-app swap is unavailable.\n\n"
            "f/d sort footprint/growth, b reset growth, Enter details, g group commands, "
            "Esc back, T theme, q quit."
        )

    def action_back(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
