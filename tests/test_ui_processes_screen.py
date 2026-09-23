"""Textual pilot tests for `ProcessesScreen`/`HelpScreen` (SPEC.md "Process view",
"Help screen", "Tests").

No live `/sys` or `/proc`: fixture trees only, refreshed deterministically via
`refresh_now()`/`screen.refresh_now()` instead of racing the real timer, same
pattern as `test_ui_main_screen.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey

from appmem.ui.app import AppMemApp
from appmem.ui.process_rows import KERNEL_KEY, UNATTRIBUTED_KEY
from appmem.ui.rows import row_key
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.screens.processes import ProcessesScreen
from helpers import (
    make_unit,
    user_service_root,
    write_cgroup_procs,
    write_meminfo,
    write_memory_stat,
    write_proc,
    write_swap_current,
    write_uptime,
)

UID = 1000
NO_AUTO_REFRESH_INTERVAL = 100.0
SCREEN_SIZE = (120, 35)


def _table(pilot: Pilot[None]) -> DataTable[str | Text]:
    # Scoped to the active (topmost) screen: `pilot.app.query_one` would also
    # match a DataTable on a screen still mounted underneath it.
    return cast("DataTable[str | Text]", pilot.app.screen.query_one(DataTable))


def _base_tree(tmp_path: Path) -> Path:
    write_memory_stat(user_service_root(tmp_path, UID))
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
    )
    write_uptime(tmp_path, seconds=100_000.0)
    return tmp_path


def _app_unit(root: Path, name: str, *, ram: int, swap: int, pids: list[int]) -> Path:
    unit_dir = user_service_root(root, UID) / "app.slice" / name
    make_unit(unit_dir, anon=ram, swap=swap, pids=pids)
    return unit_dir


def _system_app_unit(root: Path, name: str, *, ram: int, swap: int, pids: list[int]) -> Path:
    unit_dir = root / "sys" / "fs" / "cgroup" / "system.slice" / name
    make_unit(unit_dir, anon=ram, swap=swap, pids=pids)
    return unit_dir


def _proc(root: Path, pid: int, *, name: str, swap_kb: int = 0, ram_kb: int = 0) -> None:
    write_proc(root, pid, cmdline=name, vm_swap_kb=swap_kb, rss_anon_kb=ram_kb, starttime_ticks=0)


def _app(root: Path, *, include_system: bool = False) -> AppMemApp:
    return AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=include_system
    )


def _row_keys(table: DataTable[str | Text]) -> list[str]:
    keys: list[str] = []
    for row in table.ordered_rows:
        assert row.key.value is not None
        keys.append(row.key.value)
    return keys


def _title_visual(title: Static) -> Content:
    # `.content` is always the raw string passed to `update()`, unaffected by
    # the `markup` flag either way; `.visual` is what's actually rendered, so
    # it's the one that shows whether markup got parsed. `Static.update()`
    # only ever builds a `Content` from a plain string (never another Visual
    # type here), so the cast is safe.
    return cast("Content", title.visual)


async def _open_ghostty_process_view(pilot: Pilot[None]) -> None:
    table = _table(pilot)
    table.move_cursor(row=table.get_row_index(row_key("ghostty", "user")))
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
async def test_enter_opens_process_view_with_right_title(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=6 * 1024**2, swap=2 * 1024**2, pids=[100, 101])
    _proc(root, 100, name="ghostty", swap_kb=1024, ram_kb=2048)
    _proc(root, 101, name="node", swap_kb=1024, ram_kb=2048)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        title = screen.query_one("#title", Static)
        text = str(title.content)

        assert title.region.y == 0  # actually on screen, not scrolled off
        assert text.startswith("ghostty")
        assert "2 procs" in text
        assert "swap 2 MiB" in text
        assert "RAM 6 MiB" in text
        # Key hints moved to the footer, no longer inlined in the title.
        assert "group by command" not in text
        assert "back" not in text
        assert _row_keys(_table(pilot)) == ["100", "101", KERNEL_KEY, UNATTRIBUTED_KEY]


@pytest.mark.asyncio
async def test_sort_by_header_click_and_key(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=0, swap=10 * 1024**2, pids=[100, 101, 102])
    _proc(root, 100, name="a", swap_kb=1 * 1024)
    _proc(root, 101, name="b", swap_kb=3 * 1024)
    _proc(root, 102, name="c", swap_kb=2 * 1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)

        header_event = DataTable.HeaderSelected(table, ColumnKey("swap"), 1, Text("SWAP"))
        screen.on_data_table_header_selected(header_event)
        assert _row_keys(table) == ["101", "102", "100", KERNEL_KEY, UNATTRIBUTED_KEY]

        screen.on_data_table_header_selected(header_event)  # second click reverses
        assert _row_keys(table) == ["100", "102", "101", KERNEL_KEY, UNATTRIBUTED_KEY]

        await pilot.press("s")  # SWAP desc again
        assert _row_keys(table) == ["101", "102", "100", KERNEL_KEY, UNATTRIBUTED_KEY]


@pytest.mark.asyncio
async def test_g_switches_columns_and_back(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node", swap_kb=10)
    _proc(root, 101, name="node", swap_kb=20)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        assert "pid" in table.columns
        assert "procs" not in table.columns

        await pilot.press("g")
        assert "procs" in table.columns
        assert "pid" not in table.columns
        assert _row_keys(table) == ["node", KERNEL_KEY, UNATTRIBUTED_KEY]
        node_cell = table.get_cell("node", "procs")
        assert isinstance(node_cell, Text)
        assert node_cell.plain == "2"

        await pilot.press("g")
        assert "pid" in table.columns
        assert "procs" not in table.columns
        # Sort state (TOTAL desc) carried over: pid 101 (20 KiB) before 100 (10 KiB).
        assert _row_keys(table) == ["101", "100", KERNEL_KEY, UNATTRIBUTED_KEY]


@pytest.mark.asyncio
async def test_refresh_removing_a_pid_keeps_cursor_sane(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=0, swap=6 * 1024**2, pids=[100, 101, 102])
    _proc(root, 100, name="a", swap_kb=1 * 1024)
    _proc(root, 101, name="b", swap_kb=2 * 1024)
    _proc(root, 102, name="c", swap_kb=3 * 1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("101"))

        write_cgroup_procs(unit_dir, [100, 102])  # 101 vanishes
        screen.refresh_now()

        assert "101" not in _row_keys(table)
        assert 0 <= table.cursor_row < table.row_count  # cursor stayed on a real row


@pytest.mark.asyncio
async def test_app_vanishing_shows_note_and_empty_table(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert table.row_count > 0

        shutil.rmtree(unit_dir)  # the whole unit disappears
        screen.refresh_now()

        title = screen.query_one("#title", Static)
        assert "(app no longer running)" in str(title.content)
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_esc_returns_with_main_cursor_and_sort_intact(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=0, swap=1 * 1024**2, pids=[])
    _app_unit(root, "app-ghostty.service", ram=0, swap=5 * 1024**2, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        main_table = _table(pilot)
        await pilot.press("s")  # sort by SWAP desc on the main view
        main_table.move_cursor(row=main_table.get_row_index(row_key("ghostty", "user")))

        await _open_ghostty_process_view(pilot)
        assert isinstance(pilot.app.screen, ProcessesScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(pilot.app.screen, MainScreen)
        # Sort by SWAP desc survived the round trip: ghostty (5 MiB) before alpha (1 MiB).
        assert _row_keys(main_table) == [row_key("ghostty", "user"), row_key("alpha", "user")]
        cursor_key, _column_key = main_table.coordinate_to_cell_key(main_table.cursor_coordinate)
        assert cursor_key.value == row_key("ghostty", "user")


@pytest.mark.asyncio
async def test_esc_refreshes_main_view_immediately(tmp_path: Path) -> None:
    # The main view skips its ticks while covered, so it must catch up on return.
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=0, swap=5 * 1024**2, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        main_table = _table(pilot)
        await _open_ghostty_process_view(pilot)

        write_swap_current(unit_dir, 7 * 1024**2)
        await pilot.press("escape")
        await pilot.pause()

        cell = main_table.get_cell(row_key("ghostty", "user"), "swap")
        assert isinstance(cell, Text)
        assert cell.plain == "7 MiB"


@pytest.mark.asyncio
async def test_help_opens_from_main_view_and_esc_closes(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, pids=[])

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, HelpScreen)
        title = screen.query_one("#help-title", Static)
        assert title.region.y == 0
        assert "esc" in str(title.content)
        body = screen.query_one("#help-text", Static)
        text = str(body.content)
        assert '"name [sys]"' in str(body.visual)  # rendered literally, not as markup
        assert "RAM" in text
        assert "SWAP" in text
        assert "unattributed" in text

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, MainScreen)


@pytest.mark.asyncio
async def test_help_opens_from_process_view_and_q_closes(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        assert isinstance(pilot.app.screen, ProcessesScreen)

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)

        # `q` closes the help screen here, it does not quit the app (SPEC.md).
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProcessesScreen)
        assert pilot.app.return_code is None


@pytest.mark.asyncio
async def test_help_scrolls_to_reach_the_kill_pid_line_at_80x24(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, pids=[])

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, HelpScreen)
        scroll = screen.query_one("#help-scroll", VerticalScroll)
        assert scroll.max_scroll_y > 0  # was 0 (never scrolled)

        await pilot.press("end")
        await pilot.pause()

        body = screen.query_one("#help-text", Static)
        lines = str(body.content).split("\n")
        kill_line = next(i for i, line in enumerate(lines) if "kill PID" in line)
        assert scroll.scroll_y <= kill_line < scroll.scroll_y + scroll.region.height


@pytest.mark.asyncio
async def test_help_body_lines_fit_the_content_width_no_orphan_rewrap(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, pids=[])

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, HelpScreen)
        scroll = screen.query_one("#help-scroll", VerticalScroll)
        # Every line must already fit the real content width (app width minus
        # the scrollbar gutter). If a line were wrapped for the wider app
        # width instead, Static would auto-wrap it again here, spilling its
        # last word onto an orphan line of its own.
        content_width = scroll.size.width - scroll.scrollbar_size_vertical
        body = screen.query_one("#help-text", Static)
        lines = str(body.content).split("\n")
        assert all(len(line) <= content_width for line in lines)


@pytest.mark.asyncio
async def test_help_body_height_follows_the_rewrap_after_widening(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, pids=[])

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, HelpScreen)

        await pilot.resize_terminal(60, 24)
        await pilot.pause()
        await pilot.resize_terminal(80, 24)
        await pilot.pause()

        # No blank scroll area left over from the narrower wrap.
        body = screen.query_one("#help-text", Static)
        assert body.size.height == len(str(body.content).split("\n"))


@pytest.mark.asyncio
async def test_help_title_and_footer_stay_visible_without_scrolling_at_80x24(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, pids=[])

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, HelpScreen)
        title = screen.query_one("#help-title", Static)
        footer = screen.query_one("#footer", Static)
        assert title.region.y == 0
        assert "esc" in str(title.content)
        assert footer.region.y == 23  # last row at 80x24
        assert "close" in str(footer.content)


# --- kernel/unattributed rows pinned last under every sort, both modes ----------


@pytest.mark.asyncio
async def test_kernel_and_unattributed_rows_stay_pinned_last_under_every_sort(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=5 * 1024**2, swap=10 * 1024**2, pids=[100, 101, 102])
    _proc(root, 100, name="a", swap_kb=1 * 1024)
    _proc(root, 101, name="b", swap_kb=3 * 1024)
    _proc(root, 102, name="c", swap_kb=2 * 1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)

        for key in ("pid", "name", "swap", "ram", "total", "age", "unit"):
            screen.on_data_table_header_selected(
                DataTable.HeaderSelected(table, ColumnKey(key), 1, Text(key.upper()))
            )
            assert _row_keys(table)[-2:] == [KERNEL_KEY, UNATTRIBUTED_KEY]

        await pilot.press("g")  # grouped-by-command mode
        for key in ("name", "swap", "ram", "total", "procs"):
            screen.on_data_table_header_selected(
                DataTable.HeaderSelected(table, ColumnKey(key), 1, Text(key.upper()))
            )
            assert _row_keys(table)[-2:] == [KERNEL_KEY, UNATTRIBUTED_KEY]


# --- the unit list is re-derived every tick, not captured once on Enter --------


@pytest.mark.asyncio
async def test_new_unit_for_the_same_app_is_picked_up_while_the_process_view_is_open(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert set(_row_keys(table)[:-2]) == {"100"}

        # A second ghostty window opens: a new unit for the same app identity.
        _app_unit(root, "app-ghostty-2.service", ram=1 * 1024**2, swap=0, pids=[200])
        _proc(root, 200, name="ghostty")
        screen.refresh_now()

        assert set(_row_keys(table)[:-2]) == {"100", "200"}


@pytest.mark.asyncio
async def test_unit_replaced_while_the_process_view_is_open_keeps_the_app_alive(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        shutil.rmtree(unit_dir)  # the old unit vanishes
        _app_unit(root, "app-ghostty-2.service", ram=2 * 1024**2, swap=0, pids=[300])
        _proc(root, 300, name="ghostty")
        screen.refresh_now()  # a replacement unit for the same identity appears at once

        title = screen.query_one("#title", Static)
        assert "(app no longer running)" not in str(title.content)
        assert "300" in _row_keys(_table(pilot))


# --- externally sourced names render literally, markup never parsed ------------


@pytest.mark.asyncio
async def test_markup_like_process_name_and_unit_text_render_literally(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=0, swap=1 * 1024**2, pids=[100])
    # Empty cmdline forces the `comm`-file fallback path (SPEC.md "Definitions"),
    # which is not run through `Path(...).name` and so keeps markup-like text intact.
    write_proc(root, 100, cmdline="", comm="[bold]x[/]", vm_swap_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        cell = table.get_cell("100", "name")
        assert isinstance(cell, Text)
        assert cell.plain == "[bold]x[/]"
        assert cell.spans == []  # not parsed as Rich markup into a styled span

        title = pilot.app.screen.query_one("#title", Static)
        assert _title_visual(title).plain.startswith("ghostty")  # the title itself is safe too


@pytest.mark.asyncio
async def test_markup_like_app_name_renders_literally_in_the_process_view_title(
    tmp_path: Path,
) -> None:
    # The test above opens "ghostty", so its title never contains markup-like
    # text: dropping the title `Static`'s `markup=False` still passed it. Here
    # the app name itself is markup-like, so the title's rendered text would
    # change (Rich would consume "[bold]" as a style tag) if markup parsing
    # ran.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-[bold]x[-].service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key("[bold]x[-]", "user")))
        await pilot.press("enter")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        title = screen.query_one("#title", Static)
        visual = _title_visual(title)
        assert visual.plain.startswith("[bold]x[-]")
        assert visual.spans == []  # not parsed into a "bold" style span


# --- a vanishing cgroup tree exits cleanly, no traceback ------------------------


@pytest.mark.asyncio
async def test_cgroup_vanishing_mid_tick_exits_with_code_1(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")
    app = _app(root)

    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        shutil.rmtree(user_service_root(root, UID))  # the whole user tree vanishes
        screen.refresh_now()  # no CgroupUnavailableError traceback escapes this call

    assert app.return_code == 1
    assert app.cgroup_error_message is not None
    assert "missing cgroup path" in app.cgroup_error_message


@pytest.mark.asyncio
async def test_transient_memory_stat_failure_skips_the_process_view_tick(tmp_path: Path) -> None:
    # Directory present, memory.stat unparseable for one tick: skip the tick,
    # keep the rows, no exit and no "app no longer running".
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")
    app = _app(root)

    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        (user_service_root(root, UID) / "memory.stat").write_text("kernel 100\n")  # incomplete
        screen.refresh_now()

        title = screen.query_one("#title", Static)
        assert "(app no longer running)" not in str(title.content)
        assert "100" in _row_keys(_table(pilot))

    assert app.return_code is None


# --- footer present in the process view and the help screen --------------------


@pytest.mark.asyncio
async def test_footer_present_in_process_view(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        text = content.plain
        assert "sort" in text
        assert "group" in text
        assert "back" in text
        assert "quit" in text


@pytest.mark.asyncio
async def test_footer_drops_lowest_priority_items_at_60_columns(tmp_path: Path) -> None:
    # The footer never wraps. Below its natural
    # width (72 cols) it drops items lowest priority first, keeping help,
    # back and quit no matter how narrow.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)

        assert content.no_wrap is True
        assert content.cell_len <= 60
        assert "? help" in content.plain
        assert "esc back" in content.plain
        assert "q quit" in content.plain
        assert "(grouped: members)" not in content.plain  # lowest priority: dropped first


@pytest.mark.asyncio
async def test_footer_present_in_help_screen(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, pids=[])

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert content.plain == " esc close"


# --- status line for the selected row --------------------------------------------


@pytest.mark.asyncio
async def test_status_line_user_unit_shows_stop_command_and_kill_pid(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("100"))
        await pilot.pause()

        status = pilot.app.screen.query_one("#status", Static)
        text = str(status.content)
        assert text == "systemctl --user stop 'app-ghostty.service'   kill 100"


@pytest.mark.asyncio
async def test_status_line_system_unit_shows_sudo_command(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _system_app_unit(root, "cups.service", ram=1 * 1024**2, swap=0, pids=[200])
    _proc(root, 200, name="cupsd")

    async with _app(root, include_system=True).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key("cups", "system")))
        await pilot.press("enter")
        await pilot.pause()

        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("200"))
        await pilot.pause()

        status = pilot.app.screen.query_one("#status", Static)
        text = str(status.content)
        assert text == "sudo systemctl stop 'cups.service'   kill 200"


@pytest.mark.asyncio
async def test_status_line_shows_explanation_for_synthetic_rows(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        table.move_cursor(row=table.get_row_index(KERNEL_KEY))
        await pilot.pause()
        status = pilot.app.screen.query_one("#status", Static)
        assert "kernel" in str(status.content) or "page tables" in str(status.content)

        table.move_cursor(row=table.get_row_index(UNATTRIBUTED_KEY))
        await pilot.pause()
        assert "accounting difference" in str(status.content)


@pytest.mark.asyncio
async def test_status_line_ellipsizes_long_unit_in_the_middle(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    long_name = "app-" + "x" * 80 + "-2.service"
    _app_unit(root, long_name, ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    app_name_row = "x" * 80
    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key(app_name_row, "user")))
        await pilot.press("enter")
        await pilot.pause()

        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("100"))
        await pilot.pause()

        status = pilot.app.screen.query_one("#status", Static)
        text = str(status.content)
        assert len(text) <= 60
        assert "…" in text
        assert text.startswith("systemctl --user stop '")
        assert "kill 100" in text


@pytest.mark.asyncio
async def test_status_line_in_grouped_mode_one_unit_shows_command_no_kill(
    tmp_path: Path,
) -> None:
    # Grouped mode is where the terminal use case
    # lives (ghostty -> claude), so a command backed by a single unit gets the
    # same stop command as a real process row, minus `kill` (several PIDs).
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node")
    _proc(root, 101, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()

        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.pause()

        status = pilot.app.screen.query_one("#status", Static)
        text = str(status.content)
        assert text == "systemctl --user stop 'app-ghostty.service'"
        assert "kill" not in text


@pytest.mark.asyncio
async def test_status_line_in_grouped_mode_several_units_shows_hint(tmp_path: Path) -> None:
    # Same command name in two units that normalize to the same app (two
    # ghostty windows): no single truthful command covers both, so a dim hint
    # takes its place.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _app_unit(root, "app-ghostty-2.service", ram=1 * 1024**2, swap=0, pids=[101])
    _proc(root, 100, name="node")
    _proc(root, 101, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()

        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.pause()

        status = pilot.app.screen.query_one("#status", Static)
        content = status.content
        assert isinstance(content, Text)
        assert content.plain == "2 units, Enter lists the processes"
        assert content.style == "dim"


@pytest.mark.asyncio
async def test_status_line_in_grouped_mode_on_kernel_and_unattributed_rows(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()

        table = _table(pilot)
        status = pilot.app.screen.query_one("#status", Static)

        table.move_cursor(row=table.get_row_index(KERNEL_KEY))
        await pilot.pause()
        assert "page tables" in str(status.content)

        table.move_cursor(row=table.get_row_index(UNATTRIBUTED_KEY))
        await pilot.pause()
        assert "accounting difference" in str(status.content)


# --- grouped drill-down ------------------------------------------------------------


@pytest.mark.asyncio
async def test_enter_on_a_command_drills_into_its_members(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101, 102])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="claude", ram_kb=1024)
    _proc(root, 102, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        title = screen.query_one("#title", Static)
        assert "ghostty › claude" in str(title.content)  # noqa: RUF001
        assert "2 procs" in str(title.content)  # the command's members, not the app's 3
        table = _table(pilot)
        assert "pid" in table.columns  # flat process columns again
        assert set(_row_keys(table)) == {"100", "101"}  # only claude's members
        assert KERNEL_KEY not in _row_keys(table)
        assert UNATTRIBUTED_KEY not in _row_keys(table)


@pytest.mark.asyncio
async def test_esc_from_drill_returns_to_grouped_list_on_the_same_command(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101, 102])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)
    _proc(root, 102, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        # "claude" (1 MiB) sorts below "node" (2 MiB): the cursor landing back on
        # it can't be the default first-row position.
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert "procs" in table.columns  # back to grouped columns
        assert _row_keys(table)[:-2] == ["node", "claude"]
        cursor_key, _col = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert cursor_key.value == "claude"


@pytest.mark.asyncio
async def test_enter_on_synthetic_rows_in_grouped_mode_is_a_noop(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(KERNEL_KEY))
        await pilot.press("enter")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert "procs" in table.columns  # still the grouped table, not drilled


@pytest.mark.asyncio
async def test_drill_view_refreshes_live(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="claude", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")
        await pilot.pause()

        _proc(root, 102, name="claude", ram_kb=1024)
        write_cgroup_procs(unit_dir, [100, 101, 102])
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        screen.refresh_now()

        assert set(_row_keys(_table(pilot))) == {"100", "101", "102"}


@pytest.mark.asyncio
async def test_app_gone_while_drilled_clears_the_table(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="claude", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")
        await pilot.pause()

        shutil.rmtree(unit_dir)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        screen.refresh_now()

        assert _row_keys(_table(pilot)) == []
        assert "no longer running" in str(screen.query_one("#title", Static).content)


# --- AGE hidden below 95 columns, process-view title never wraps ---------------


@pytest.mark.asyncio
async def test_age_column_hidden_below_95_columns(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        assert "age" not in table.columns
        assert "pid" in table.columns


@pytest.mark.asyncio
async def test_age_column_shown_at_or_above_95_columns(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        assert "age" in table.columns


@pytest.mark.asyncio
async def test_age_column_recomputes_on_resize(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        assert "age" in table.columns

        await pilot.resize_terminal(80, 24)
        assert "age" not in table.columns

        await pilot.resize_terminal(120, 35)
        assert "age" in table.columns


@pytest.mark.asyncio
async def test_sorting_by_age_hidden_by_width_resorts_by_total_desc(tmp_path: Path) -> None:
    # 100 is the oldest but the smallest: AGE desc puts it first, TOTAL desc last.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=8 * 1024**2, swap=0, pids=[100, 101])
    write_proc(root, 100, cmdline="ghostty", rss_anon_kb=1024, starttime_ticks=0)
    write_proc(root, 101, cmdline="node", rss_anon_kb=4096, starttime_ticks=500_000)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        screen.on_data_table_header_selected(
            DataTable.HeaderSelected(_table(pilot), ColumnKey("age"), 5, Text("AGE"))
        )
        assert _row_keys(_table(pilot))[:2] == ["100", "101"]

        await pilot.resize_terminal(80, 24)

        assert _row_keys(_table(pilot))[:2] == ["101", "100"]


@pytest.mark.asyncio
async def test_process_view_title_never_wraps_and_drops_lowest_priority_first(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    long_name = "app-" + "y" * 70 + ".service"
    _app_unit(root, long_name, ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key("y" * 70, "user")))
        await pilot.press("enter")
        await pilot.pause()

        title = pilot.app.screen.query_one("#title", Static)
        text = str(title.content)
        assert "y" * 70 in text  # the name itself is never dropped or truncated
        assert "procs" not in text  # lowest priority: dropped first at 40 columns
