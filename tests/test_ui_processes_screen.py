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
from textual.pilot import Pilot
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey

from appmem.ui.app import AppMemApp
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


async def _open_ghostty_process_view(pilot: Pilot[None]) -> None:
    table = _table(pilot)
    table.move_cursor(row=table.get_row_index("ghostty"))
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
        assert "g  group by command" in text
        assert "esc  back" in text
        assert _row_keys(_table(pilot)) == ["100", "101", "other"]


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
        assert _row_keys(table) == ["101", "102", "100", "other"]

        screen.on_data_table_header_selected(header_event)  # second click reverses
        assert _row_keys(table) == ["100", "102", "101", "other"]

        await pilot.press("s")  # SWAP desc again
        assert _row_keys(table) == ["101", "102", "100", "other"]


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
        assert _row_keys(table) == ["node", "other"]
        node_cell = table.get_cell("node", "procs")
        assert isinstance(node_cell, Text)
        assert node_cell.plain == "2"

        await pilot.press("g")
        assert "pid" in table.columns
        assert "procs" not in table.columns
        # Sort state (TOTAL desc) carried over: pid 101 (20 KiB) before 100 (10 KiB).
        assert _row_keys(table) == ["101", "100", "other"]


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
        main_table.move_cursor(row=main_table.get_row_index("ghostty"))

        await _open_ghostty_process_view(pilot)
        assert isinstance(pilot.app.screen, ProcessesScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(pilot.app.screen, MainScreen)
        # Sort by SWAP desc survived the round trip: ghostty (5 MiB) before alpha (1 MiB).
        assert _row_keys(main_table) == ["ghostty", "alpha"]
        row_key, _column_key = main_table.coordinate_to_cell_key(main_table.cursor_coordinate)
        assert row_key.value == "ghostty"


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

        cell = main_table.get_cell("ghostty", "swap")
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
        body = screen.query_one("#help-text", Static)
        text = str(body.content)
        assert body.region.y == 0
        assert "RAM" in text
        assert "SWAP" in text
        assert "other" in text

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
