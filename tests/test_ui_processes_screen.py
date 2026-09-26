"""Textual pilot tests for `ProcessesScreen`/`HelpScreen` (SPEC.md "Process view",
"Help screen", "Tests").

No live `/sys` or `/proc`: fixture trees only, refreshed deterministically via
`refresh_now()`/`screen.refresh_now()` instead of racing the real timer, same
pattern as `test_ui_main_screen.py`.
"""

from __future__ import annotations

import gc
import shutil
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import OptionList, Static
from textual.worker import Worker

from appmem import collect
from appmem.collect import LinuxBackend
from appmem.model import AppStats, ProcStats
from appmem.theme import THEME_NAMES, config_path
from appmem.ui.app import AppMemApp
from appmem.ui.process_rows import KERNEL_KEY, UNATTRIBUTED_KEY, ZSWAP_POOL_KEY
from appmem.ui.rows import row_key
from appmem.ui.screens import processes as processes_screen
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.screens.processes import ProcessesScreen
from appmem.ui.table import RowTable
from appmem.ui.theme_picker import ThemePanel
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


def _table(pilot: Pilot[None]) -> RowTable:
    # Scoped to the active (topmost) screen: `pilot.app.query_one` would also
    # match a RowTable on a screen still mounted underneath it.
    return pilot.app.screen.query_one(RowTable)


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
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=include_system,
    )


def _row_keys(table: RowTable) -> list[str]:
    return list(table.row_keys)


def _cursor_visible(table: RowTable) -> bool:
    top = table.scroll_offset.y
    rows_on_screen = table.size.height - 1  # line 0 is the fixed header
    return top <= table.cursor_row < top + rows_on_screen


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

        header_event = RowTable.HeaderSelected(table, "swap")
        screen.on_row_table_header_selected(header_event)
        assert _row_keys(table) == ["101", "102", "100", KERNEL_KEY, UNATTRIBUTED_KEY]

        screen.on_row_table_header_selected(header_event)  # second click reverses
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
        assert "pid" in table.column_keys
        assert "procs" not in table.column_keys

        await pilot.press("g")
        assert "procs" in table.column_keys
        assert "pid" not in table.column_keys
        assert _row_keys(table) == ["node", KERNEL_KEY, UNATTRIBUTED_KEY]
        node_cell = table.get_cell("node", "procs")
        assert isinstance(node_cell, Text)
        assert node_cell.plain == "2"

        await pilot.press("g")
        assert "pid" in table.column_keys
        assert "procs" not in table.column_keys
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
        assert main_table.cursor_key == row_key("ghostty", "user")


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
            screen.on_row_table_header_selected(RowTable.HeaderSelected(table, key))
            assert _row_keys(table)[-2:] == [KERNEL_KEY, UNATTRIBUTED_KEY]

        await pilot.press("g")  # grouped-by-command mode
        for key in ("name", "swap", "ram", "total", "procs"):
            screen.on_row_table_header_selected(RowTable.HeaderSelected(table, key))
            assert _row_keys(table)[-2:] == [KERNEL_KEY, UNATTRIBUTED_KEY]


# --- zswap-pool row split out of kernel ----------------------------------------


@pytest.mark.asyncio
async def test_zswap_pool_row_shown_between_kernel_and_unattributed_when_present(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    unit_dir = user_service_root(root, UID) / "app.slice" / "app-ghostty.service"
    make_unit(unit_dir, anon=5 * 1024**2, kernel=2 * 1024**2, zswap=512 * 1024, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        assert _row_keys(table)[-4:] == ["100", KERNEL_KEY, ZSWAP_POOL_KEY, UNATTRIBUTED_KEY]


@pytest.mark.asyncio
async def test_zswap_pool_row_absent_when_the_app_has_no_zswap_pool(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)

        assert ZSWAP_POOL_KEY not in _row_keys(_table(pilot))


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


@pytest.mark.asyncio
async def test_transient_os_error_skips_the_process_view_tick_and_shows_fresh_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient OS-level read failure elsewhere in the collector (SPEC.md
    # "Behaviour details") must be treated the same as
    # `MemoryStatUnavailableError`: skip this tick, keep the last rows, and
    # let the next tick recover. The fixture changes before the failing
    # tick, so a passing test can't be explained by the tick doing nothing
    # either way: the failing tick must still show the *old* value, and only
    # the next tick shows the new one.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty", ram_kb=1024)
    app = _app(root)

    real_read_procs = collect.read_procs
    calls = {"n": 0}

    def _flaky_read_procs(unit_paths: Iterable[Path], root: Path) -> list[ProcStats]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient read failure")
        return real_read_procs(unit_paths, root)

    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert "100" in _row_keys(table)

        _proc(root, 100, name="ghostty", ram_kb=4096)  # changes before tick 1
        monkeypatch.setattr(collect, "read_procs", _flaky_read_procs)
        screen.refresh_now()  # tick 1: raises OSError internally, must not propagate
        ram_cell = table.get_cell("100", "ram")
        assert isinstance(ram_cell, Text)
        assert ram_cell.plain == "1 MiB"  # the failing tick kept the old frame
        assert app.return_code is None

        screen.refresh_now()  # tick 2: recovers, reads the already-changed fixture
        ram_cell = table.get_cell("100", "ram")
        assert isinstance(ram_cell, Text)
        assert ram_cell.plain == "4 MiB"  # fresh data, not the stale 1 MiB

    assert app.return_code is None  # the session ran to completion, not exited


@pytest.mark.asyncio
async def test_a_programming_error_in_a_process_view_tick_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only OSError/ValueError (transient reads) are swallowed. A programming
    # error (e.g. TypeError) must still surface.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")
    app = _app(root)

    def _broken_read_procs(unit_paths: Iterable[Path], root: Path) -> list[ProcStats]:
        raise TypeError("not a transient read failure")

    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        monkeypatch.setattr(collect, "read_procs", _broken_read_procs)
        with pytest.raises(TypeError):
            screen.refresh_now()

    assert app.return_code is None


@pytest.mark.asyncio
async def test_a_value_error_from_the_process_view_apply_path_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `ValueError` is only swallowed when it comes from the collector reads
    # inside the `try`. `RowTable.reorder`'s own invariant check also raises
    # `ValueError`, but from the apply step outside the `try` -- a broken
    # invariant there is a programming error and must still surface, not
    # freeze the table.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")
    app = _app(root)

    def _broken_reorder(self: RowTable, ordered_keys: list[str]) -> None:
        raise ValueError("broken invariant")

    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        monkeypatch.setattr(RowTable, "reorder", _broken_reorder)
        with pytest.raises(ValueError, match="broken invariant"):
            screen.refresh_now()


# --- column order: RAM before SWAP everywhere ----------------------------------


@pytest.mark.asyncio
async def test_flat_and_drilled_column_order_is_pid_name_ram_swap_total(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        keys = list(table.column_keys)

        assert keys == ["pid", "name", "ram", "swap", "total", "age", "unit"]

        table.move_cursor(row=table.get_row_index("100"))
        await pilot.press("enter")  # a no-op here (not grouped), stays flat
        await pilot.pause()
        keys = list(table.column_keys)
        assert keys == ["pid", "name", "ram", "swap", "total", "age", "unit"]


@pytest.mark.asyncio
async def test_grouped_column_order_is_name_ram_swap_total_procs(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        keys = list(table.column_keys)

        assert keys == ["name", "ram", "swap", "total", "procs"]


@pytest.mark.asyncio
async def test_grouped_procs_sort_shows_marker(tmp_path: Path) -> None:
    # A4 (run-2 fixes): PROCS was 6 cells wide, but "PROCS ▾"/"PROCS ▴" is 7,
    # so `RowTable._fit` cut the marker off (SPEC.md "Process view", "the
    # sort marker sits on the sorted column").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node", ram_kb=1024)
    _proc(root, 101, name="claude", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)

        screen.on_row_table_header_selected(RowTable.HeaderSelected(table, "procs"))
        header = table.render_line(0).text
        region = table.column_region("procs")
        assert header[region.x : region.x + region.width].strip() in ("PROCS ▾", "PROCS ▴")

        screen.on_row_table_header_selected(RowTable.HeaderSelected(table, "procs"))
        header = table.render_line(0).text
        assert header[region.x : region.x + region.width].strip() in ("PROCS ▾", "PROCS ▴")


@pytest.mark.asyncio
async def test_drill_down_column_order_is_pid_name_ram_swap_total(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.press("enter")
        await pilot.pause()

        table = _table(pilot)
        keys = list(table.column_keys)
        assert keys == ["pid", "name", "ram", "swap", "total", "age", "unit"]


# --- mouse-wheel scroll survives a refresh tick --------------------------------


async def _scroll_and_assert_stable(pilot: Pilot[None], screen: ProcessesScreen) -> None:
    table = _table(pilot)
    table.post_message(events.MouseScrollDown(table, 10, 10, 0, 0, 0, False, False, False))
    await pilot.pause()
    assert table.scroll_y > 0  # the wheel actually scrolled the table
    scrolled_y = table.scroll_y

    for _ in range(3):  # simulate three refresh ticks
        screen.refresh_now()
    await pilot.pause()

    assert table.scroll_y == scrolled_y  # no snap-back to the cursor's row


@pytest.mark.asyncio
async def test_wheel_scroll_does_not_snap_back_flat(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    pids = list(range(100, 160))
    _app_unit(root, "app-ghostty.service", ram=2 * 1024**2, swap=0, pids=pids)
    for pid in pids:
        _proc(root, pid, name=f"proc{pid}", ram_kb=1024)

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        await _scroll_and_assert_stable(pilot, screen)
        scrolled_y = _table(pilot).scroll_y

        # A tick that adds a process re-runs the column sync after layout;
        # that sync must not scroll either (only a resize may).
        _app_unit(root, "app-ghostty.service", ram=2 * 1024**2, swap=0, pids=[*pids, 200])
        _proc(root, 200, name="proc200", ram_kb=1024)
        screen.refresh_now()
        await pilot.pause()
        await pilot.pause()
        assert _table(pilot).scroll_y == scrolled_y


@pytest.mark.asyncio
async def test_process_column_resize_scrolls_selection_into_view(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    pids = list(range(100, 160))
    _app_unit(root, "app-ghostty.service", ram=2 * 1024**2, swap=0, pids=pids)
    for pid in pids:
        _proc(root, pid, name=f"proc{pid}", ram_kb=1024)

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        table.post_message(events.MouseScrollDown(table, 10, 10, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert table.scroll_y > 0
        await pilot.resize_terminal(80, 20)
        await pilot.pause()
        assert table.scroll_y == 0
        assert _cursor_visible(table)


@pytest.mark.asyncio
async def test_wheel_scroll_does_not_snap_back_grouped(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    pids = list(range(100, 160))
    _app_unit(root, "app-ghostty.service", ram=2 * 1024**2, swap=0, pids=pids)
    for pid in pids:
        _proc(root, pid, name=f"cmd{pid}", ram_kb=1024)  # every process its own command

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        await _scroll_and_assert_stable(pilot, screen)


@pytest.mark.asyncio
async def test_wheel_scroll_does_not_snap_back_drilled(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    pids = list(range(100, 160))
    _app_unit(root, "app-ghostty.service", ram=2 * 1024**2, swap=0, pids=pids)
    for pid in pids:
        _proc(root, pid, name="node", ram_kb=1024)  # one command, 60 members

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.press("enter")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        await _scroll_and_assert_stable(pilot, screen)


# --- contextual footer: only keys that act in the current mode -----------------


@pytest.mark.asyncio
async def test_footer_hides_members_and_says_back_in_flat_mode(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert content.plain == " r s t sort  g group  T theme  ? help  esc back  q quit"


@pytest.mark.asyncio
async def test_footer_shows_members_and_back_in_grouped_non_drilled_mode(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()

        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert content.plain == (
            " r s t sort  g group  enter members  T theme  ? help  esc back  q quit"
        )


@pytest.mark.asyncio
async def test_footer_hides_members_and_relabels_esc_to_groups_when_drilled(
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
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.press("enter")
        await pilot.pause()

        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert content.plain == " r s t sort  g group  T theme  ? help  esc groups  q quit"


@pytest.mark.asyncio
async def test_footer_recomputes_on_resize_in_grouped_mode(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()

        await pilot.resize_terminal(60, 24)
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert content.no_wrap is True
        assert content.cell_len <= 60


# --- every rendered footer key does something in its mode ----------------------


@pytest.mark.asyncio
async def test_every_rendered_footer_key_acts_in_flat_mode(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="a", swap_kb=1024)
    _proc(root, 101, name="b", swap_kb=2048)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        for key, column in (("r", "ram"), ("s", "swap"), ("t", "total")):
            await pilot.press(key)
            label = table.get_column_label(column)
            assert "▴" in label or "▾" in label  # sort acted: this column is now marked

        await pilot.press("g")  # group: the table's column shape changes
        assert "procs" in table.column_keys
        await pilot.press("g")  # back to flat, for the remaining checks
        await pilot.pause()

        await pilot.press("?")  # help: the screen changes
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("escape")  # back: the screen changes
        await pilot.pause()
        assert isinstance(pilot.app.screen, MainScreen)


@pytest.mark.asyncio
async def test_every_rendered_footer_key_acts_in_grouped_mode(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="a", swap_kb=1024)
    _proc(root, 101, name="b", swap_kb=2048)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)

        for key, column in (("r", "ram"), ("s", "swap"), ("t", "total")):
            await pilot.press(key)
            label = table.get_column_label(column)
            assert "▴" in label or "▾" in label

        table.move_cursor(row=table.get_row_index("a"))
        await pilot.press("enter")  # enter: drills in, the screen's table shape changes
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        assert "pid" in _table(pilot).column_keys

        await pilot.press("escape")  # back to the grouped list
        await pilot.pause()
        await pilot.press("g")  # group: back to flat, the column shape changes
        assert "pid" in _table(pilot).column_keys

        await pilot.press("?")
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("escape")  # back: the screen changes
        await pilot.pause()
        assert isinstance(pilot.app.screen, MainScreen)


@pytest.mark.asyncio
async def test_every_rendered_footer_key_acts_in_drilled_mode(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node", swap_kb=1024)
    _proc(root, 101, name="node", swap_kb=2048)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.press("enter")
        await pilot.pause()
        table = _table(pilot)

        for key, column in (("r", "ram"), ("s", "swap"), ("t", "total")):
            await pilot.press(key)
            label = table.get_column_label(column)
            assert "▴" in label or "▾" in label

        await pilot.press("g")  # group: exits the drill-down entirely, to flat
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        assert not screen._showing_group_table  # pyright: ignore[reportPrivateUsage]
        assert screen._drill_command is None  # pyright: ignore[reportPrivateUsage]

        # Re-enter the drill-down to check `esc` (labelled "groups") and `?`.
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("node"))
        await pilot.press("enter")
        await pilot.pause()

        await pilot.press("?")
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("escape")  # groups: back to the grouped list, table shape changes
        await pilot.pause()
        assert "procs" in _table(pilot).column_keys


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
    # The footer never wraps. Below its natural width, in grouped mode where
    # "members" is a candidate item at all, it drops lowest priority first
    # (SPEC.md "Process view" drop order: theme, members, group, sort),
    # keeping help, back and quit no matter how narrow.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)

        assert content.no_wrap is True
        assert content.cell_len <= 60
        assert "? help" in content.plain
        assert "esc back" in content.plain
        assert "q quit" in content.plain
        assert "theme" not in content.plain  # lowest priority: dropped first
        assert "members" not in content.plain  # dropped next


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["flat", "grouped", "drilled"])
async def test_t_opens_the_panel_from_every_process_view_mode(tmp_path: Path, mode: str) -> None:
    # `T` is bound at the app level (SPEC.md "Command line"), not on either
    # screen, so it must keep working no matter which process-view mode is
    # on top.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node")
    _proc(root, 101, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        if mode in ("grouped", "drilled"):
            await pilot.press("g")
            await pilot.pause()
        if mode == "drilled":
            table = _table(pilot)
            table.move_cursor(row=table.get_row_index("node"))
            await pilot.press("enter")
            await pilot.pause()
        assert isinstance(pilot.app.screen, ProcessesScreen)

        await pilot.press("T")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ThemePanel)

        # `T` again while the panel is open: same as Esc, not a second panel.
        await pilot.press("T")
        await pilot.pause()
        assert sum(isinstance(s, ThemePanel) for s in pilot.app.screen_stack) == 0
        assert isinstance(pilot.app.screen, ProcessesScreen)
    assert not config_path().exists()  # closed without picking anything


@pytest.mark.asyncio
async def test_theme_panel_previews_and_confirms_from_the_process_view(tmp_path: Path) -> None:
    # The panel isn't only reachable from the process view (the test
    # above); it must actually preview and persist from there too, the
    # same as from the main view (SPEC.md "Command line").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="node")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ThemePanel)
        option_list = screen.query_one("#theme-list", OptionList)
        option_list.highlighted = THEME_NAMES.index("dracula")
        await pilot.pause()
        assert pilot.app.theme == "dracula"  # previewed at once
        assert not config_path().exists()

        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.theme == "dracula"
        assert config_path().read_text() == 'theme = "dracula"\n'
        assert isinstance(pilot.app.screen, ProcessesScreen)
        assert pilot.app.theme != before


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
        assert "pid" in table.column_keys  # flat process columns again
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
        assert "procs" in table.column_keys  # back to grouped columns
        assert _row_keys(table)[:-2] == ["node", "claude"]
        assert table.cursor_key == "claude"


@pytest.mark.asyncio
async def test_esc_from_drill_keeps_the_command_on_screen(tmp_path: Path) -> None:
    # A1 (run-2 fixes): `_exit_drill` passed `scroll=False` after `_switch_
    # mode` had already rebuilt the table, so Esc restored the cursor on the
    # right command but left the viewport at the top when that command sat
    # below the first screen (SPEC.md "Process view": Esc returns to the
    # grouped list on the same command; "Main view": drill in/out scrolls
    # the selected row into view).
    root = _base_tree(tmp_path)
    pids = list(range(100, 160))
    _app_unit(root, "app-ghostty.service", ram=len(pids) * 1024**2, swap=0, pids=pids)
    for i, pid in enumerate(pids):
        _proc(root, pid, name=f"cmd{i:02d}", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(50)  # well below the first screen, still a real command row
        await pilot.pause()
        command = table.cursor_key
        assert _cursor_visible(table)

        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        table = _table(pilot)
        assert table.cursor_key == command
        assert _cursor_visible(table), (table.scroll_offset.y, table.cursor_row, table.size)


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
        assert "procs" in table.column_keys  # still the grouped table, not drilled


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
        footer = screen.query_one("#footer", Static).content
        assert isinstance(footer, Text)
        assert "esc back" in footer.plain  # the drill-down ended: esc leaves the view now


@pytest.mark.asyncio
async def test_app_reappearing_after_vanishing_while_drilled_shows_grouped_rows_cleanly(
    tmp_path: Path,
) -> None:
    # Ending a drill-down (the
    # app vanished) leaves `_grouped` true and `_drill_command` cleared, so
    # `_showing_group_table` flips back to true. If the table's columns
    # aren't rebuilt for that, a later reappearance adds command-shaped cells
    # (NAME, RAM, SWAP, TOTAL, PROCS) into a table still built for the
    # drill-down's process columns (PID, NAME, RAM, SWAP, TOTAL, AGE, UNIT):
    # every cell lands one column off, silently wrong, not a crash.
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
        screen.refresh_now()  # tick: app gone, drill-down ends

        _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100])
        _proc(root, 100, name="claude", ram_kb=1024)
        screen.refresh_now()  # tick: app back, now in grouped (not drilled) mode

        table = _table(pilot)
        assert set(table.column_keys) == {"name", "ram", "swap", "total", "procs"}  # grouped shape
        assert _row_keys(table) == ["claude", KERNEL_KEY, UNATTRIBUTED_KEY]
        ram_cell = table.get_cell("claude", "ram")
        assert isinstance(ram_cell, Text)
        assert ram_cell.plain == "1 MiB"  # the command's own column, not shifted
        procs_cell = table.get_cell("claude", "procs")
        assert isinstance(procs_cell, Text)
        assert procs_cell.plain == "1"


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

        assert "age" not in table.column_keys
        assert "pid" in table.column_keys


@pytest.mark.asyncio
async def test_age_column_shown_at_or_above_95_columns(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)

        assert "age" in table.column_keys


@pytest.mark.asyncio
async def test_age_column_recomputes_on_resize(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty")

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        assert "age" in table.column_keys

        await pilot.resize_terminal(80, 24)
        assert "age" not in table.column_keys

        await pilot.resize_terminal(120, 35)
        assert "age" in table.column_keys


@pytest.mark.asyncio
async def test_height_only_resize_keeps_cursor_on_screen(tmp_path: Path) -> None:
    # A2 (run-2 fixes): `_sync_columns` only scrolled the cursor into view
    # when the computed column widths actually changed, so a resize that
    # only shrank the height (AGE and NAME's width both depend on width, not
    # height, so neither changes) never brought the cursor back into the
    # now-shorter viewport (SPEC.md "Process view").
    root = _base_tree(tmp_path)
    pids = list(range(100, 160))
    _app_unit(root, "app-ghostty.service", ram=len(pids) * 1024**2, swap=0, pids=pids)
    for i, pid in enumerate(pids):
        _proc(root, pid, name=f"p{i:02d}", ram_kb=1024)

    async with _app(root).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        table.move_cursor(table.row_count - 1)
        await pilot.pause()
        assert _cursor_visible(table)

        await pilot.resize_terminal(120, 20)  # width unchanged: no column rebuild
        await pilot.pause()
        await pilot.pause()

        table = _table(pilot)
        assert _cursor_visible(table), (table.scroll_offset.y, table.cursor_row, table.size)


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
        screen.on_row_table_header_selected(RowTable.HeaderSelected(_table(pilot), "age"))
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


# --- control characters never reach the terminal --------------------------------


@pytest.mark.asyncio
async def test_control_chars_in_process_name_are_escaped(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="/usr/bin/\x1b[41mRED\x1b[0m", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        cell = table.get_cell("100", "name")
        assert isinstance(cell, Text)
        assert "\x1b" not in cell.plain
        assert "\\x1b" in cell.plain


@pytest.mark.asyncio
async def test_control_chars_in_unit_name_are_escaped_in_process_rows_and_status(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-evil\x1b[2Junit.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key("evil\x1b[2junit", "user")))
        await pilot.press("enter")
        await pilot.pause()

        table = _table(pilot)
        unit_cell = table.get_cell("100", "unit")
        assert isinstance(unit_cell, Text)
        assert "\x1b" not in unit_cell.plain
        assert "\\x1b" in unit_cell.plain

        status = pilot.app.screen.query_one("#status", Static)
        assert "\x1b" not in str(status.content)


@pytest.mark.asyncio
async def test_control_chars_in_system_app_process_view_title_are_escaped(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _system_app_unit(root, "evil\x1b[2Jsys.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="daemon", ram_kb=1024)

    async with _app(root, include_system=True).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key("evil\x1b[2jsys", "system")))
        await pilot.press("enter")
        await pilot.pause()

        title = pilot.app.screen.query_one("#title", Static)
        assert "\x1b" not in str(title.content)
        assert "\\x1b" in str(title.content)


@pytest.mark.asyncio
async def test_c1_control_in_process_name_is_escaped(tmp_path: Path) -> None:
    # U+009B (CSI) is a C1 control, not C0: `escape_control_chars` must
    # catch it too, not just the C0/DEL range (SPEC.md "Behaviour details").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="/usr/bin/\x9b31mred", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        cell = table.get_cell("100", "name")
        assert isinstance(cell, Text)
        assert not any(0x80 <= ord(char) <= 0x9F for char in cell.plain), repr(cell.plain)
        assert "\\x9b" in cell.plain


# --- CJK and other wide-character names -----------------------------------------


@pytest.mark.asyncio
async def test_proc_40_cjk_name_keeps_total_on_screen_at_80x24(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="微" * 40, ram_kb=12 * 1024**2)

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)


# --- title, status line and footer never wrap, at any width ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 50, 40, 24, 1])
async def test_title_status_and_footer_stay_one_line_at_any_width(
    tmp_path: Path, width: int
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty", ram_kb=1024)

    async with _app(root).run_test(size=(width, 12)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        title = pilot.app.screen.query_one("#title", Static)
        status = pilot.app.screen.query_one("#status", Static)
        footer = pilot.app.screen.query_one("#footer", Static)
        assert title.region.height == 1, (width, title.region.height)
        assert status.region.height == 1, (width, status.region.height)
        assert footer.region.height == 1, (width, footer.region.height)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 40, 24, 1])
async def test_rendered_title_and_footer_lines_crop_to_width(tmp_path: Path, width: int) -> None:
    # `region.height == 1` alone doesn't prove the *text* fits: a `Static`
    # can report one line of height while Rich still wraps or overflows its
    # content within it. Render the actual strips Textual would paint and
    # measure their real cell width instead (SPEC.md "Process view").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty", ram_kb=1024)

    async with _app(root).run_test(size=(width, 12)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        for selector in ("#title", "#status", "#footer"):
            widget = pilot.app.screen.query_one(selector, Static)
            lines = widget.render_lines(widget.region.reset_offset)
            assert len(lines) == 1, (selector, len(lines))
            text = "".join(segment.text for segment in lines[0])
            assert cell_len(text) <= width, (selector, text)


@pytest.mark.asyncio
async def test_process_table_shows_data_rows_at_40x10(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty", ram_kb=1024)

    async with _app(root).run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        assert table.row_count > 0
        assert table.scrollable_content_region.height >= 1


# --- numeric columns fit at 60 columns -------------------------------------------


@pytest.mark.asyncio
async def test_total_column_fits_on_screen_at_60_columns_with_a_long_name(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="x" * 60, ram_kb=12 * 1024**2)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)


@pytest.mark.asyncio
async def test_total_column_fits_after_resizing_from_90_to_60(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="a" * 25, ram_kb=12 * 1024**2)

    async with _app(root).run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        cell = table.get_cell("100", "name").plain
        assert "…" not in cell, "already cropped at 90 columns"

        await pilot.resize_terminal(60, 24)
        await pilot.pause()

        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)
        cell = table.get_cell("100", "name").plain
        assert "…" in cell, "long name not cropped at 60 columns"


@pytest.mark.asyncio
async def test_name_column_widens_back_after_resizing_from_60_to_90(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="a" * 25, ram_kb=12 * 1024**2)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        cell = table.get_cell("100", "name").plain
        assert "…" in cell, "long name not cropped at 60 columns"

        await pilot.resize_terminal(90, 24)
        await pilot.pause()

        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)
        cell = table.get_cell("100", "name").plain
        assert "…" not in cell, "still cropped back at 90 columns"


# --- NAME budgeted at every width, not only below a fixed threshold ------------


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [70, 78, 85, 100])
async def test_numeric_columns_fit_on_screen_with_a_32_char_name(
    tmp_path: Path, width: int
) -> None:
    # A name at the 32-char truncation cap: every numeric column must stay
    # fully on screen at every one of these widths instead of being pushed
    # past the terminal edge (SPEC.md "Process view"). Before this,
    # `_with_name_width` only shrank NAME below a fixed 70-column threshold,
    # so TOTAL was pushed off at 70-78 (PID and AGE budgeted the same way).
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="x" * 32, ram_kb=12 * 1024**2)

    async with _app(root).run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        for key in table.column_keys:
            if key in ("name", "unit"):
                continue
            region = table.column_region(key)
            assert region.right <= table.size.width, (key, region, table.size.width)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [70, 78, 85, 100])
async def test_numeric_columns_fit_on_screen_grouped_with_a_32_char_command_name(
    tmp_path: Path, width: int
) -> None:
    # Same as above, grouped-by-command mode: before this, PROCS was pushed
    # off screen at 70-77.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="x" * 32, ram_kb=12 * 1024**2)

    async with _app(root).run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        table = _table(pilot)
        for key in table.column_keys:
            if key == "name":
                continue
            region = table.column_region(key)
            assert region.right <= table.size.width, (key, region, table.size.width)


@pytest.mark.asyncio
async def test_numeric_columns_still_fit_when_new_rows_bring_a_scrollbar(tmp_path: Path) -> None:
    # No resize happens when enough processes appear to need a vertical
    # scrollbar, yet it narrows the width NAME was budgeted against.
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="x" * 32, ram_kb=1024)

    async with _app(root).run_test(size=(78, 15)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)

        pids = list(range(100, 140))
        write_cgroup_procs(unit_dir, pids)
        for pid in pids:
            _proc(root, pid, name="x" * 32, ram_kb=1024)
        screen.refresh_now()
        await pilot.pause()

        assert table.scrollbar_size_vertical > 0
        region = table.column_region("total")
        assert region.right <= table.scrollable_content_region.width


@pytest.mark.asyncio
async def test_procs_column_still_fits_when_new_groups_bring_a_scrollbar(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    unit_dir = _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="x" * 32, ram_kb=1024)

    async with _app(root).run_test(size=(78, 15)) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)

        pids = list(range(100, 140))
        write_cgroup_procs(unit_dir, pids)
        for pid in pids:
            _proc(root, pid, name=f"{pid}" + "x" * 29, ram_kb=1024)  # one group each
        screen.refresh_now()
        await pilot.pause()

        assert table.scrollbar_size_vertical > 0
        region = table.column_region("procs")
        assert region.right <= table.scrollable_content_region.width


# --- slow reads must not freeze input --------------------------------------------


@pytest.mark.asyncio
async def test_slow_process_tick_does_not_block_key_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A synchronous tick blocks the whole event loop for its own blocking
    # call, so nothing else -- not even this test's polling -- can run
    # concurrently with it: only the *total* time from "a read started" to
    # "the key landed" tells the threaded and synchronous versions apart.
    # Threaded code finishes this in milliseconds; a reverted-to-synchronous
    # tick can't finish any faster than the collector's own bounded wait.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101, 102])
    _proc(root, 100, name="a", ram_kb=1024)
    _proc(root, 101, name="b", ram_kb=1024)
    _proc(root, 102, name="c", ram_kb=1024)
    real_read_procs = collect.read_procs
    started = threading.Event()
    release = threading.Event()

    def blocking_read_procs(unit_paths: Iterable[Path], root: Path) -> list[ProcStats]:
        started.set()
        release.wait(timeout=2)  # bounded: the worker thread never hangs forever
        return real_read_procs(unit_paths, root)

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.05, include_system=False)
    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        monkeypatch.setattr(collect, "read_procs", blocking_read_procs)
        table = _table(pilot)
        row = table.cursor_row

        t0 = time.monotonic()
        deadline = t0 + 5
        while not started.is_set() and time.monotonic() < deadline:
            await pilot.pause(0.01)
        assert started.is_set(), "the tick never started reading"

        await pilot.press("down")
        deadline = time.monotonic() + 5
        while table.cursor_row == row and time.monotonic() < deadline:
            await pilot.pause(0.01)
        elapsed = time.monotonic() - t0
        release.set()  # let a still-blocked read finish so the worker thread exits cleanly

        assert table.cursor_row != row, "the key was never handled"
        assert elapsed < 1.0, f"key handling waited {elapsed:.2f}s for the in-flight read"

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        deadline = time.monotonic() + 5
        while screen._tick_in_flight and time.monotonic() < deadline:  # pyright: ignore[reportPrivateUsage]
            await pilot.pause(0.01)
        assert not screen._tick_in_flight, "blocked read never finished"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_process_ticks_leave_no_workers_or_results_behind(tmp_path: Path) -> None:
    # Same guard as the main view's: a finished Textual `Worker` lives on
    # until a cyclic GC pass, which Python 3.14 can defer for hours, so a
    # tick creates none, and its result dies by refcount once applied.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="a", ram_kb=1024)
    _proc(root, 101, name="b", ram_kb=1024)
    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.02, include_system=False)
    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.pause(1.0)  # ~50 ticks
        workers = sum(1 for o in gc.get_objects() if isinstance(o, Worker))
        results = [
            o
            for o in gc.get_objects()
            if isinstance(o, processes_screen._TickResult)  # pyright: ignore[reportPrivateUsage]
        ]
    assert workers == 0, f"{workers} Textual workers alive"
    assert len(results) <= 2, f"{len(results)} tick results still alive"


@pytest.mark.asyncio
async def test_collector_bug_in_a_process_tick_exits_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As in the main view: a collector bug raised in the tick's thread must
    # end the run with its traceback, not silently stop the refreshes.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="a", ram_kb=1024)

    def broken_tick_worker(*args: object, **kwargs: object) -> object:
        raise RuntimeError("collector bug")

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.02, include_system=False)
    with pytest.raises(RuntimeError, match="collector bug"):
        async with app.run_test(size=SCREEN_SIZE) as pilot:
            await pilot.pause()
            await _open_ghostty_process_view(pilot)
            monkeypatch.setattr(processes_screen, "_tick_worker", broken_tick_worker)
            await pilot.pause(0.5)
    assert app.return_code == 1


@pytest.mark.asyncio
async def test_at_most_one_process_tick_read_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100])
    _proc(root, 100, name="ghostty", ram_kb=1024)
    real_read_procs = LinuxBackend.read_procs
    lock = threading.Lock()
    counts = {"current": 0, "max": 0}

    def slow_read_procs(backend: LinuxBackend, app: AppStats) -> list[ProcStats]:
        with lock:
            counts["current"] += 1
            counts["max"] = max(counts["max"], counts["current"])
        time.sleep(0.3)
        try:
            return real_read_procs(backend, app)
        finally:
            with lock:
                counts["current"] -= 1

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.1, include_system=False)
    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        monkeypatch.setattr(LinuxBackend, "read_procs", slow_read_procs)
        await pilot.pause(0.6)  # several 0.1s intervals elapse while a 0.3s read is in flight

        assert counts["max"] <= 1, "two process-view tick reads were in flight at once"


@pytest.mark.asyncio
async def test_stale_tick_result_is_discarded_after_toggling_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tick starts reading in flat mode; before it finishes, `g` switches to
    # grouped mode. The slow tick's result still carries the generation it
    # was dispatched under, now older than the switch, so it must be
    # discarded on arrival instead of clobbering the just-switched grouped
    # table (SPEC.md "Process view").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)
    real_read_procs = LinuxBackend.read_procs

    def slow_read_procs(backend: LinuxBackend, app: AppStats) -> list[ProcStats]:
        time.sleep(0.4)  # the stale (pre-toggle, flat-mode) read
        return real_read_procs(backend, app)

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.1, include_system=False)
    async with app.run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        monkeypatch.setattr(LinuxBackend, "read_procs", slow_read_procs)
        await pilot.pause(0.15)  # a periodic tick is now reading in flat mode
        await pilot.press("g")  # explicit toggle: synchronous refresh_now, grouped mode
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        assert "procs" in _table(pilot).column_keys  # grouped columns right after the toggle

        await pilot.pause(0.5)  # let the slow, now-stale flat-mode read arrive and try to apply
        assert "procs" in _table(pilot).column_keys, "a stale flat-mode result overwrote the table"


@pytest.mark.asyncio
async def test_stale_tick_is_still_discarded_after_toggling_group_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Toggling `g` twice returns to the same (flat) context the stale
    # periodic read was dispatched with. A staleness check that compares
    # context values directly, rather than a generation that only ever
    # increases, could mistake that old read for current again and overwrite
    # the freshly toggled rows with stale ones (SPEC.md "Process view").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)
    real_read_procs = LinuxBackend.read_procs
    snapshotted = threading.Event()
    release = threading.Event()

    def snapshot_then_block(backend: LinuxBackend, app: AppStats) -> list[ProcStats]:
        result = real_read_procs(backend, app)  # snapshot now, 2 procs
        snapshotted.set()
        release.wait(timeout=5)  # bounded: only returns once both toggles below have committed
        return result

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        monkeypatch.setattr(LinuxBackend, "read_procs", snapshot_then_block)
        screen._tick()  # pyright: ignore[reportPrivateUsage]  # exactly one stale worker, no interval

        deadline = time.monotonic() + 5
        while not snapshotted.is_set() and time.monotonic() < deadline:
            await pilot.pause(0.01)
        assert snapshotted.is_set(), "the periodic tick never took its (stale) snapshot"

        monkeypatch.setattr(LinuxBackend, "read_procs", real_read_procs)
        _proc(root, 102, name="extra", ram_kb=1024)  # a process the stale read never saw
        _app_unit(root, "app-ghostty.service", ram=1 * 1024**2, swap=0, pids=[100, 101, 102])
        await pilot.press("g")  # a newer generation: grouped, fast, sees 3 procs
        await pilot.pause()
        await pilot.press("g")  # flat again -- same context as the stale read, newer generation
        await pilot.pause()

        table = _table(pilot)
        assert "102" in _row_keys(table), "the second toggle's own fresh read did not apply"

        release.set()  # let the stale worker return its 2-proc snapshot
        deadline = time.monotonic() + 5
        while screen._tick_in_flight and time.monotonic() < deadline:  # pyright: ignore[reportPrivateUsage]
            await pilot.pause(0.01)
        assert not screen._tick_in_flight, "the stale worker never finished"  # pyright: ignore[reportPrivateUsage]

        table = _table(pilot)
        assert "102" in _row_keys(table), "a stale periodic read overwrote the freshly toggled rows"


# --- mode switches (g, Enter, Esc) are all-or-nothing ---------------------------


@pytest.mark.asyncio
async def test_esc_from_drill_during_a_failing_read_keeps_rows_then_switches_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")
        await pilot.pause()

        real = collect.read_procs

        def boom(*a: object, **k: object) -> list[ProcStats]:
            raise OSError("transient")

        monkeypatch.setattr(collect, "read_procs", boom)
        await pilot.press("escape")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert table.row_count > 0, "drilled rows blanked by a failing Esc"
        assert "pid" in table.column_keys, "still showing the drilled (process) columns"
        assert screen._drill_command == "claude", "mode switched despite the failing read"  # pyright: ignore[reportPrivateUsage]

        monkeypatch.setattr(collect, "read_procs", real)
        await pilot.press("escape")  # the user presses again; this read succeeds
        await pilot.pause()

        table = _table(pilot)
        assert "procs" in table.column_keys  # now rebuilt to the grouped shape
        assert table.cursor_key == "claude"


@pytest.mark.asyncio
async def test_enter_drill_during_a_failing_tick_keeps_the_grouped_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()
        table = _table(pilot)
        before_keys = set(_row_keys(table))

        def boom(*a: object, **k: object) -> list[ProcStats]:
            raise OSError("transient")

        monkeypatch.setattr(collect, "read_procs", boom)
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert table.row_count > 0, "grouped list blanked by a failing drill-down read"
        assert set(_row_keys(table)) == before_keys
        assert "procs" in table.column_keys  # not switched to the drilled (process) shape yet


@pytest.mark.asyncio
async def test_toggle_group_during_a_failing_tick_keeps_the_flat_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        table = _table(pilot)
        before_keys = set(_row_keys(table))

        def boom(*a: object, **k: object) -> list[ProcStats]:
            raise OSError("transient")

        monkeypatch.setattr(collect, "read_procs", boom)
        await pilot.press("g")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        table = _table(pilot)
        assert table.row_count > 0, "flat list blanked by a failing group-toggle read"
        assert set(_row_keys(table)) == before_keys
        assert "pid" in table.column_keys  # not switched to the grouped shape yet


@pytest.mark.asyncio
async def test_sort_key_still_works_after_a_failing_group_toggle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)

        def boom(*a: object, **k: object) -> list[ProcStats]:
            raise OSError("transient")

        monkeypatch.setattr(collect, "read_procs", boom)
        await pilot.press("g")  # fails: stays in flat mode
        await pilot.pause()
        await pilot.press("s")  # a sort key must still work in the old mode
        await pilot.pause()
        assert pilot.app.is_running


@pytest.mark.asyncio
async def test_sort_key_still_works_after_a_failing_enter_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)
        await pilot.press("g")
        await pilot.pause()

        def boom(*a: object, **k: object) -> list[ProcStats]:
            raise OSError("transient")

        monkeypatch.setattr(collect, "read_procs", boom)
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index("claude"))
        await pilot.press("enter")  # fails: stays in grouped mode
        await pilot.pause()
        await pilot.press("r")  # a sort key must still work in the old mode
        await pilot.pause()
        assert pilot.app.is_running


@pytest.mark.asyncio
async def test_enter_on_stale_row_after_a_failing_group_toggle_does_not_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the fix, `on_row_table_row_selected` trusted `_showing_group_table`
    # against a table that a failing `g` had left in the old (flat) shape,
    # drilling into a PID string mistaken for a command name.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-ghostty.service", ram=3 * 1024**2, swap=0, pids=[100, 101])
    _proc(root, 100, name="claude", ram_kb=1024)
    _proc(root, 101, name="node", ram_kb=1024)

    async with _app(root).run_test(size=SCREEN_SIZE) as pilot:
        await pilot.pause()
        await _open_ghostty_process_view(pilot)

        def boom(*a: object, **k: object) -> list[ProcStats]:
            raise OSError("transient")

        monkeypatch.setattr(collect, "read_procs", boom)
        await pilot.press("g")  # fails: stays in flat mode
        await pilot.pause()
        await pilot.press("enter")  # a no-op outside grouped mode
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)
        assert screen._drill_command is None, screen._drill_command  # pyright: ignore[reportPrivateUsage]
