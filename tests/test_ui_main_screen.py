"""Textual pilot tests for `AppMemApp`/`MainScreen` (SPEC.md "Main view", "Tests").

No live `/sys` or `/proc`: `AppMemApp` takes the same fixture `root`/`uid` as
`collect` (AGENTS.md). The interval is set high enough that the automatic
timer never fires during a test; refreshes are triggered deterministically by
calling `MainScreen.refresh_now()` after mutating the fixture on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey

from appmem.ui.app import AppMemApp
from appmem.ui.screens.main import MainScreen
from helpers import make_unit, user_service_root, write_meminfo, write_memory_stat

UID = 1000
NO_AUTO_REFRESH_INTERVAL = 100.0


def _table(pilot: Pilot[None]) -> DataTable[str | Text]:
    return cast("DataTable[str | Text]", pilot.app.query_one(DataTable))


def _base_tree(tmp_path: Path) -> Path:
    write_memory_stat(user_service_root(tmp_path, UID))
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
    )
    return tmp_path


def _app_unit(
    root: Path, name: str, *, ram: int, swap: int, cache: int = 0, procs: int = 1
) -> None:
    unit_dir = user_service_root(root, UID) / "app.slice" / name
    make_unit(unit_dir, anon=ram, file=cache, swap=swap, pids=list(range(1, procs + 1)))


def _system_unit(root: Path, *, ram: int, swap: int) -> None:
    unit_dir = root / "sys" / "fs" / "cgroup" / "system.slice" / "cups.service"
    make_unit(unit_dir, anon=ram, swap=swap, pids=[9999])


def _app(root: Path, *, include_system: bool = False) -> AppMemApp:
    return AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=include_system
    )


def _row_names(table: DataTable[str | Text]) -> list[str]:
    names: list[str] = []
    for row in table.ordered_rows:
        assert row.key.value is not None
        names.append(row.key.value)
    return names


@pytest.mark.asyncio
async def test_default_sort_is_total_descending(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    _app_unit(root, "app-bravo.service", ram=3 * 1024**2, swap=0)
    _app_unit(root, "app-charlie.service", ram=2 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert _row_names(table) == ["bravo", "charlie", "alpha"]


@pytest.mark.asyncio
async def test_header_click_sorts_then_second_click_reverses(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=0, swap=1 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=0, swap=3 * 1024**2)
    _app_unit(root, "app-charlie.service", ram=0, swap=2 * 1024**2)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        header_event = DataTable.HeaderSelected(table, ColumnKey("swap"), 1, Text("SWAP"))
        screen.on_data_table_header_selected(header_event)
        assert _row_names(table) == ["bravo", "charlie", "alpha"]

        screen.on_data_table_header_selected(header_event)
        assert _row_names(table) == ["alpha", "charlie", "bravo"]


@pytest.mark.asyncio
async def test_sort_keys_s_r_t_d(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=3 * 1024**2, swap=1 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=1 * 1024**2, swap=3 * 1024**2)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)

        await pilot.press("s")  # SWAP desc
        assert _row_names(table) == ["bravo", "alpha"]

        await pilot.press("r")  # RAM desc
        assert _row_names(table) == ["alpha", "bravo"]

        await pilot.press("t")  # TOTAL desc: both 4 MiB, tie broken by name
        assert _row_names(table) == ["alpha", "bravo"]

        await pilot.press("d")  # ΔSWAP desc: both 0 on the first tick, tie broken by name
        assert _row_names(table) == ["alpha", "bravo"]


@pytest.mark.asyncio
async def test_cursor_stays_on_same_app_after_a_reordering_refresh(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=0, swap=2 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=0, swap=4 * 1024**2)
    _app_unit(root, "app-charlie.service", ram=0, swap=3 * 1024**2)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        # Default TOTAL desc: bravo, charlie, alpha. Select charlie (the middle row).
        table.move_cursor(row=table.get_row_index("charlie"))

        # Mutate the fixture so charlie drops to the bottom (but stays above the
        # 1 MiB visibility floor, SPEC.md "Behaviour details"), then re-tick.
        _app_unit(root, "app-charlie.service", ram=0, swap=1 * 1024**2)
        screen.refresh_now()

        row_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert row_key.value == "charlie"


@pytest.mark.asyncio
async def test_c_toggles_the_cache_column(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, cache=5 * 1024**2)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "cache" not in table.columns

        await pilot.press("c")
        assert "cache" in table.columns
        cache_cell = table.get_cell("alpha", "cache")
        assert isinstance(cache_cell, Text)
        assert cache_cell.plain == "5 MiB"

        await pilot.press("c")
        assert "cache" not in table.columns


@pytest.mark.asyncio
async def test_x_toggles_system_rows(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    _system_unit(root, ram=1 * 1024**2, swap=0)

    async with _app(root, include_system=False).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "cups" not in _row_names(table)

        await pilot.press("x")
        assert "cups" in _row_names(table)

        await pilot.press("x")
        assert "cups" not in _row_names(table)


@pytest.mark.asyncio
async def test_z_resets_delta_to_zero(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)
        screen.refresh_now()
        delta_before = table.get_cell("alpha", "delta_ram")
        assert isinstance(delta_before, Text)
        assert delta_before.plain != "0"

        await pilot.press("z")
        delta_after = table.get_cell("alpha", "delta_ram")
        assert isinstance(delta_after, Text)
        assert delta_after.plain == "0"


@pytest.mark.asyncio
async def test_header_lines_and_footer_stay_on_screen_with_many_rows(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    for index in range(60):  # more rows than a 35-line terminal can show
        _app_unit(root, f"app-app{index:02d}.service", ram=(index + 2) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        header1 = screen.query_one("#header1", Static)
        header2 = screen.query_one("#header2", Static)
        footer = screen.query_one("#footer", Static)

        assert screen.scroll_offset.y == 0
        assert header1.region.y == 0 and header2.region.y == 1
        assert footer.region.bottom == 35
        assert str(header1.content).startswith("RAM ")
        assert str(header2.content).startswith("Δ since ")


@pytest.mark.asyncio
async def test_long_app_names_are_not_truncated(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    name = "xdg-desktop-portal-gnome-helper"
    _app_unit(root, f"{name}.service", ram=2 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app_column = _table(pilot).columns[ColumnKey("app")]

        assert app_column.get_render_width(_table(pilot)) >= len(name)


@pytest.mark.asyncio
async def test_q_quits_with_exit_code_zero(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")

    assert app.return_code == 0


@pytest.mark.asyncio
async def test_ctrl_c_quits_with_exit_code_zero(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")

    assert app.return_code == 0
