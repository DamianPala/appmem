"""Regressions for crashes found in the merge review (fix slice A, part 1)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from textual import events
from textual.pilot import Pilot
from textual.widgets import Static

from appmem.collect import LinuxBackend
from appmem.darwin_backend import DarwinBackend
from appmem.ui.app import AppMemApp
from appmem.ui.screens.darwin import DarwinMainScreen, DarwinProcessesScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.screens.processes import ProcessesScreen
from appmem.ui.table import RowTable
from appmem.ui.theme_picker import ThemePanel
from helpers import (
    make_unit,
    user_service_root,
    write_meminfo,
    write_memory_stat,
    write_proc,
    write_uptime,
)
from test_darwin_backend import Reader


async def _cover_resize_unwind(
    pilot: Pilot[None], covers: str, sizes: tuple[int, int], change: Callable[[], None]
) -> None:
    """Open a second screen over the drill-down, resize and change the data
    while both cover the main view, then close both."""
    first, second = sizes
    await pilot.pause()
    pilot.app.screen.query_one("#table", RowTable).focus()
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press(covers)
    await pilot.pause()
    await pilot.resize_terminal(second, 30)
    await pilot.pause()
    change()
    await pilot.press("escape")
    await pilot.pause()
    await pilot.press("escape")
    await pilot.pause()
    assert pilot.app.size.width == second
    assert first != second


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "second"), [(120, 90), (80, 120), (120, 60), (64, 90)], ids=str)
async def test_darwin_main_survives_resize_while_two_screens_cover_it(
    first: int, second: int
) -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", "/usr/bin/helper", 20)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(first, 30)) as pilot:

        def change() -> None:
            reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 900)

        await _cover_resize_unwind(pilot, "question_mark", (first, second), change)
        screen = pilot.app.screen
        assert isinstance(screen, DarwinMainScreen)
        table = screen.query_one("#table", RowTable)
        widths = tuple(w for _, _, w in screen._specs(table))  # pyright: ignore[reportPrivateUsage]
        assert widths == screen._column_widths  # pyright: ignore[reportPrivateUsage]
        assert len(table.column_keys) == len(widths)


@pytest.mark.asyncio
async def test_darwin_detail_survives_resize_while_help_covers_it() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", "/usr/bin/helper", 20)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        await pilot.press("g")
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        detail = pilot.app.screen
        assert isinstance(detail, DarwinProcessesScreen)
        table = detail.query_one("#table", RowTable)
        assert len(table.column_keys) == len(detail._column_widths)  # pyright: ignore[reportPrivateUsage]


def _linux_tree(root: Path, *, ram: int = 6 * 1024**2) -> None:
    write_memory_stat(user_service_root(root, 1000))
    write_meminfo(
        root,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
    )
    write_uptime(root, seconds=100_000.0)
    unit = user_service_root(root, 1000) / "app.slice" / "app-ghostty.service"
    make_unit(unit, anon=ram, swap=0, pids=[100, 101])
    write_proc(root, 100, cmdline="ghostty", rss_anon_kb=ram // 2048)
    write_proc(root, 101, cmdline="node", rss_anon_kb=ram // 2048)


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "second"), [(120, 90), (90, 120), (120, 60)], ids=str)
async def test_linux_screens_survive_resize_while_two_screens_cover_them(
    tmp_path: Path, first: int, second: int
) -> None:
    _linux_tree(tmp_path)
    app = AppMemApp(backend=LinuxBackend(tmp_path, 1000), interval=3600, include_system=False)
    async with app.run_test(size=(first, 30)) as pilot:
        await pilot.pause()
        main_screen = pilot.app.screen
        assert isinstance(main_screen, MainScreen)
        assert main_screen.query_one("#table", RowTable).row_count == 1
        main_screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        detail = pilot.app.screen
        assert isinstance(detail, ProcessesScreen)
        detail_table = detail.query_one(RowTable)
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.resize_terminal(second, 30)
        await pilot.pause()
        _linux_tree(tmp_path, ram=9 * 1024**2)
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is detail
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is main_screen
        for screen, table in (
            (main_screen, main_screen.query_one("#table", RowTable)),
            (detail, detail_table),
        ):
            assert table.row_count >= 1
            assert len(table.column_keys) == len(screen._column_widths)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_double_click_outside_theme_panel_closes_it_once(tmp_path: Path) -> None:
    _linux_tree(tmp_path)
    app = AppMemApp(backend=LinuxBackend(tmp_path, 1000), interval=3600, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()
        panel = pilot.app.screen
        assert isinstance(panel, ThemePanel)
        # The second click of a double click is already queued for the panel
        # when the first one dismisses it.
        for chain in (1, 2):
            panel.post_message(
                events.Click(
                    panel,
                    1,
                    1,
                    0,
                    0,
                    1,
                    False,
                    False,
                    False,
                    screen_x=1,
                    screen_y=1,
                    chain=chain,
                )
            )
        await pilot.pause()
        assert isinstance(pilot.app.screen, MainScreen)
        assert pilot.app.is_running
        assert pilot.app.query_one("#header1", Static)
