"""Textual pilot tests for `AppMemApp`/`MainScreen` (SPEC.md "Main view", "Tests").

No live `/sys` or `/proc`: `AppMemApp` takes the same fixture `root`/`uid` as
`collect` (AGENTS.md). The interval is set high enough that the automatic
timer never fires during a test; refreshes are triggered deterministically by
calling `MainScreen.refresh_now()` after mutating the fixture on disk.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.color import Color
from textual.command import Command, CommandList
from textual.content import Content
from textual.coordinate import Coordinate
from textual.pilot import Pilot
from textual.theme import BUILTIN_THEMES
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey

from appmem.collect import AppStats
from appmem.fmt import format_pair
from appmem.theme import THEME_NAMES, config_path
from appmem.ui.app import AppMemApp
from appmem.ui.rows import row_key
from appmem.ui.screens import main as main_screen
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.theme_picker import ThemePalette
from helpers import (
    make_unit,
    user_service_root,
    write_meminfo,
    write_memory_stat,
    write_vmstat,
    write_zswap_enabled,
)

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


def _zswap_base_tree(tmp_path: Path) -> Path:
    """Like `_base_tree`, with zswap on machine-wide (SPEC.md "Main view")."""
    write_memory_stat(user_service_root(tmp_path, UID))
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
        zswap_kb=2_000_000,
        zswapped_kb=7_000_000,
    )
    write_zswap_enabled(tmp_path, enabled=True)
    return tmp_path


def _app_unit(
    root: Path,
    name: str,
    *,
    ram: int,
    swap: int,
    cache: int = 0,
    zswapped: int = 0,
    procs: int = 1,
) -> None:
    unit_dir = user_service_root(root, UID) / "app.slice" / name
    make_unit(
        unit_dir,
        anon=ram,
        file=cache,
        zswapped=zswapped,
        swap=swap,
        pids=list(range(1, procs + 1)),
    )


def _system_unit(root: Path, *, ram: int, swap: int) -> None:
    unit_dir = root / "sys" / "fs" / "cgroup" / "system.slice" / "cups.service"
    make_unit(unit_dir, anon=ram, swap=swap, pids=[9999])


def _app(root: Path, *, include_system: bool = False) -> AppMemApp:
    return AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=include_system
    )


def _row_names(table: DataTable[str | Text]) -> list[str]:
    # Row keys are "<scope>\0<name>" (SPEC.md "Grouping"); every app in these
    # fixtures is "user" scope, so stripping the prefix recovers the app name.
    names: list[str] = []
    for row in table.ordered_rows:
        assert row.key.value is not None
        _scope, _, name = row.key.value.partition("\0")
        names.append(name)
    return names


async def _click_header(pilot: Pilot[None], table: DataTable[str | Text], column_key: str) -> None:
    # A genuine pilot `click`, not calling `on_data_table_header_selected`
    # directly (SPEC.md "Tests"): computes the header cell's x offset from
    # the table's own column layout and clicks the header row (y=0).
    column_index = table.get_column_index(column_key)
    region = table._get_column_region(column_index)  # pyright: ignore[reportPrivateUsage]
    await pilot.click(table, offset=(region.x + 1, 0))
    await pilot.pause()


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
        table.move_cursor(row=table.get_row_index(row_key("charlie", "user")))

        # Mutate the fixture so charlie drops to the bottom (but stays above the
        # 1 MiB visibility floor, SPEC.md "Behaviour details"), then re-tick.
        _app_unit(root, "app-charlie.service", ram=0, swap=1 * 1024**2)
        screen.refresh_now()

        cursor_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert cursor_key.value == row_key("charlie", "user")


@pytest.mark.asyncio
async def test_hiding_cache_while_sorted_by_it_falls_back_to_total_desc(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    # alpha: low TOTAL (1 MiB, at the visibility floor), high CACHE.
    # bravo: high TOTAL (5 MiB), no CACHE. CACHE-desc and TOTAL-desc disagree.
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, cache=9 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=5 * 1024**2, swap=0, cache=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        await pilot.press("c")  # show CACHE
        header_event = DataTable.HeaderSelected(table, ColumnKey("cache"), 1, Text("CACHE"))
        screen.on_data_table_header_selected(header_event)  # explicit sort by CACHE desc
        assert _row_names(table) == ["alpha", "bravo"]

        await pilot.press("c")  # hide CACHE while it's the active sort column

        # Falls back to TOTAL desc (SPEC.md "Main view"), not just dropping
        # to insertion order or staying CACHE-ordered.
        assert _row_names(table) == ["bravo", "alpha"]


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
        cache_cell = table.get_cell(row_key("alpha", "user"), "cache")
        assert isinstance(cache_cell, Text)
        assert cache_cell.plain == "5 MiB"

        await pilot.press("c")
        assert "cache" not in table.columns


# --- zswap column (mirrors CACHE, SPEC.md "Main view") -------------------------


@pytest.mark.asyncio
async def test_zswap_column_shown_by_default_when_enabled(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    # zswapped <= swap always: the compressed pool is a subset of the unit's
    # swap, never more.
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=5 * 1024**2, zswapped=5 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:  # >= _ZSWAP_MIN_WIDTH
        await pilot.pause()
        table = _table(pilot)

        assert "zswap" in table.columns  # shown from the first tick, no `w` needed
        zswap_cell = table.get_cell(row_key("alpha", "user"), "zswap")
        assert isinstance(zswap_cell, Text)
        assert zswap_cell.plain == "5 MiB"


@pytest.mark.asyncio
async def test_zswap_column_absent_when_disabled(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)  # no zswap fixtures: disabled/unsupported
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "zswap" not in table.columns


@pytest.mark.asyncio
async def test_w_toggles_the_zswap_column(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=5 * 1024**2, zswapped=5 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:  # >= _ZSWAP_MIN_WIDTH
        await pilot.pause()
        table = _table(pilot)
        assert "zswap" in table.columns  # shown by default

        await pilot.press("w")
        assert "zswap" not in table.columns

        await pilot.press("w")
        assert "zswap" in table.columns
        zswap_cell = table.get_cell(row_key("alpha", "user"), "zswap")
        assert isinstance(zswap_cell, Text)
        assert zswap_cell.plain == "5 MiB"


@pytest.mark.asyncio
async def test_w_does_nothing_when_zswap_is_disabled(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)  # no zswap fixtures: disabled/unsupported
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)

        await pilot.press("w")

        assert "zswap" not in table.columns


# --- ZSWAP hides below its own width threshold, on top of `w`/zswap-off --------


@pytest.mark.asyncio
async def test_zswap_column_hidden_below_85_columns(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(84, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "zswap" not in table.columns


@pytest.mark.asyncio
async def test_zswap_column_shown_at_85_columns(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(85, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "zswap" in table.columns


@pytest.mark.asyncio
async def test_zswap_column_recomputes_on_resize(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "zswap" in table.columns

        await pilot.resize_terminal(80, 24)
        assert "zswap" not in table.columns

        await pilot.resize_terminal(120, 35)
        assert "zswap" in table.columns


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [85, 90, 100, 110])
async def test_numeric_columns_fit_on_screen_with_zswap_and_a_32_char_name(
    tmp_path: Path, width: int
) -> None:
    # A name at the 32-char truncation cap, with ZSWAP on: the APP column
    # must shrink (toward `_APP_MIN_WIDTH` if needed) at every one of these
    # widths so every numeric column stays fully on screen instead of being
    # pushed past the terminal edge (SPEC.md "Main view"). Before this,
    # `_app_column_width` only shrank APP below a fixed 70-column threshold,
    # so ZSWAP (default on from 85) pushed PROCS off at 85-89, and ΔSWAP/
    # PROCS (shown from 95) off at 95-110. 100 and 110 are also above the Δ
    # threshold, so this covers ZSWAP + Δ together, the densest column set.
    root = _zswap_base_tree(tmp_path)
    name = "x" * 32
    _app_unit(root, f"{name}.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "zswap" in table.columns
        assert ("delta_swap" in table.columns) == (width >= 95)
        for key in ("ram", "swap", "zswap", "total", "procs"):
            idx = table.get_column_index(key)
            region = table._get_column_region(idx)  # pyright: ignore[reportPrivateUsage]
            assert region.right <= table.size.width, (key, region, table.size.width)


@pytest.mark.asyncio
async def test_numeric_columns_still_fit_when_new_rows_bring_a_scrollbar(tmp_path: Path) -> None:
    # No resize happens when enough apps appear to need a vertical scrollbar,
    # yet it narrows the width the APP column was budgeted against.
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(85, 20)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        for i in range(40):
            _app_unit(root, f"app-n{i:02d}.service", ram=1 * 1024**2, swap=0)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        screen.refresh_now()
        await pilot.pause()

        assert table.scrollbar_size_vertical > 0
        region = table._get_column_region(table.get_column_index("procs"))  # pyright: ignore[reportPrivateUsage]
        assert region.right <= table.scrollable_content_region.width


@pytest.mark.asyncio
async def test_hiding_zswap_while_sorted_by_it_falls_back_to_total_desc(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    # zswapped <= swap always, so alpha's swap grows with its zswapped; bravo's
    # RAM is set high enough that its TOTAL still beats alpha's once ZSWAP
    # (alpha's own column) is no longer the sort key.
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=9 * 1024**2, zswapped=9 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=20 * 1024**2, swap=0, zswapped=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        # ZSWAP is already shown by default; sort by it (a header click, or `z`).
        header_event = DataTable.HeaderSelected(table, ColumnKey("zswap"), 1, Text("ZSWAP"))
        screen.on_data_table_header_selected(header_event)  # explicit sort by ZSWAP desc
        assert _row_names(table) == ["alpha", "bravo"]

        await pilot.press("w")  # hide ZSWAP while it's the active sort column

        assert _row_names(table) == ["bravo", "alpha"]  # falls back to TOTAL desc


@pytest.mark.asyncio
async def test_zswap_disabled_mid_session_hides_the_shown_column_and_falls_back_sort(
    tmp_path: Path,
) -> None:
    # zswap is normally a machine fact, not a per-tick one, but if it does
    # flip off mid-session the column (and its sort, if active) can't stay --
    # `w` would no longer do anything to hide it by hand.
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:  # >= _ZSWAP_MIN_WIDTH
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        # ZSWAP is already shown by default; sort by it.
        header_event = DataTable.HeaderSelected(table, ColumnKey("zswap"), 1, Text("ZSWAP"))
        screen.on_data_table_header_selected(header_event)  # sort by it
        assert "zswap" in table.columns
        assert screen._sort_key == "zswap"  # pyright: ignore[reportPrivateUsage]

        write_zswap_enabled(root, enabled=False)
        screen.refresh_now()

        assert "zswap" not in table.columns
        assert screen._sort_key != "zswap"  # pyright: ignore[reportPrivateUsage]
        footer = screen.query_one("#footer", Static).content
        assert isinstance(footer, Text)
        assert "r s t d sort" in footer.plain  # `z` left the sort item with the column

        await pilot.press("w")  # a no-op again, same as before zswap ever turned on
        assert "zswap" not in table.columns


@pytest.mark.asyncio
async def test_footer_hides_w_zswap_when_disabled_and_shows_it_when_enabled(
    tmp_path: Path,
) -> None:
    disabled_root = _base_tree(tmp_path)
    async with _app(disabled_root).run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.query_one("#footer", Static)
        assert isinstance(footer.content, Text)
        assert "zswap" not in footer.content.plain

    enabled_root = _zswap_base_tree(tmp_path)
    async with _app(enabled_root).run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.query_one("#footer", Static)
        assert isinstance(footer.content, Text)
        assert "w zswap" in footer.content.plain


@pytest.mark.asyncio
async def test_question_mark_passes_the_zswap_flag_to_help(tmp_path: Path) -> None:
    # `action_help` must actually pass `self._zswap_enabled` through to
    # `HelpScreen` -- `_build_body`'s own gating on that flag is covered in
    # test_help.py, not this hand-off from the running screen.
    zswap_root = _zswap_base_tree(tmp_path)
    async with _app(zswap_root).run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        body = str(pilot.app.screen.query_one("#help-text", Static).content)
        assert "ZSWAP" in body

    plain_root = _base_tree(tmp_path)
    async with _app(plain_root).run_test() as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        body = str(pilot.app.screen.query_one("#help-text", Static).content)
        assert "ZSWAP" not in body


@pytest.mark.asyncio
async def test_column_order_with_cache_and_zswap_shown_is_ram_swap_cache_zswap_total(
    tmp_path: Path,
) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("c")  # ZSWAP is already shown by default
        table = _table(pilot)
        keys = [column.key.value for column in table.ordered_columns]

        assert keys == [
            "app",
            "ram",
            "swap",
            "cache",
            "zswap",
            "total",
            "delta_ram",
            "delta_swap",
            "procs",
        ]


@pytest.mark.asyncio
async def test_header_shows_zswap_bracket_when_enabled(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)

    # Wide enough that the bracket survives the width-drop order (unit test in
    # test_ui_header.py covers the drop order itself); 120 already drops it here.
    async with _app(root).run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        header2 = pilot.app.query_one("#header2", Static)  # Swap line

        assert isinstance(header2.content, Text)
        assert "zswapped" in header2.content.plain


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
async def test_b_resets_delta_to_zero(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)
        screen.refresh_now()
        delta_before = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(delta_before, Text)
        assert delta_before.plain != "·"

        await pilot.press("b")
        delta_after = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(delta_after, Text)
        assert delta_after.plain == "·"


@pytest.mark.asyncio
async def test_z_no_longer_resets_delta(tmp_path: Path) -> None:
    # `b` took over "reset Δ" from `z`, which now sorts by ZSWAP instead
    # (SPEC.md "Main view"): pressing it must leave the baseline untouched.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)
        screen.refresh_now()
        delta_before = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(delta_before, Text)
        assert delta_before.plain != "·"

        await pilot.press("z")  # a no-op here: the ZSWAP column is hidden (zswap is off)
        delta_after = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(delta_after, Text)
        assert delta_after.plain == delta_before.plain  # untouched, not reset


@pytest.mark.asyncio
async def test_b_takes_a_fresh_sample_not_the_previous_ticks_data(tmp_path: Path) -> None:
    # `test_b_resets_delta_to_zero` above passes even if `b` reuses
    # `self._last_apps` from the previous tick, because nothing changes on
    # disk between `b` and the assertion. Here the fixture keeps moving after
    # the last tick, so a stale baseline and a fresh one disagree.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)
        screen.refresh_now()  # the screen's last tick sampled 5 MiB

        _app_unit(root, "app-alpha.service", ram=9 * 1024**2, swap=0)  # moves again, no tick yet
        await pilot.press("b")  # must sample fresh (9 MiB), not reuse the 5 MiB tick
        screen.refresh_now()  # disk value unchanged: a stale (5 MiB) baseline would show +4 MiB

        ram_cell = table.get_cell(row_key("alpha", "user"), "ram")
        assert isinstance(ram_cell, Text)
        assert ram_cell.plain == "9 MiB"

        delta_after = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(delta_after, Text)
        assert delta_after.plain == "·"


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
        header3 = screen.query_one("#header3", Static)
        footer = screen.query_one("#footer", Static)

        assert screen.scroll_offset.y == 0
        assert header1.region.y == 0 and header2.region.y == 1 and header3.region.y == 2
        assert footer.region.bottom == 35
        assert str(header1.content).startswith("RAM ")
        assert str(header2.content).startswith("Swap ")
        assert str(header3.content).startswith("Pressure ")
        assert "Δ since " in str(header3.content)


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


# --- column order: RAM before SWAP everywhere ----------------------------------


@pytest.mark.asyncio
async def test_column_order_is_app_ram_swap_total_procs(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        keys = [column.key.value for column in table.ordered_columns]

        assert keys == ["app", "ram", "swap", "total", "delta_ram", "delta_swap", "procs"]


@pytest.mark.asyncio
async def test_column_order_with_cache_shown_is_ram_swap_cache_total(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        table = _table(pilot)
        keys = [column.key.value for column in table.ordered_columns]

        assert keys == [
            "app",
            "ram",
            "swap",
            "cache",
            "total",
            "delta_ram",
            "delta_swap",
            "procs",
        ]


# --- mouse-wheel scroll survives a refresh tick --------------------------------


@pytest.mark.asyncio
async def test_wheel_scroll_does_not_snap_back_on_refresh_ticks(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    for index in range(60):  # more rows than a 20-line terminal can show
        _app_unit(root, f"app-app{index:02d}.service", ram=(index + 2) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        table.post_message(events.MouseScrollDown(table, 10, 10, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert table.scroll_y > 0  # the wheel actually scrolled the table
        scrolled_y = table.scroll_y

        for _ in range(3):  # simulate three refresh ticks
            screen.refresh_now()
        await pilot.pause()

        assert table.scroll_y == scrolled_y  # no snap-back to the cursor's row


# --- cursor follows the selected app across an explicit sort -------------------


@pytest.mark.asyncio
async def test_cursor_follows_selected_app_across_explicit_sort_by_key(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=10 * 1024**2, swap=1 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=0, swap=4 * 1024**2)
    _app_unit(root, "app-charlie.service", ram=0, swap=2 * 1024**2)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        # Default TOTAL desc: alpha, bravo, charlie. Select alpha (the first row).
        table.move_cursor(row=table.get_row_index(row_key("alpha", "user")))

        await pilot.press("s")  # explicit sort by SWAP desc: alpha moves to the last row

        assert _row_names(table)[-1] == "alpha"  # the sort actually moved it
        cursor_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert cursor_key.value == row_key("alpha", "user")


@pytest.mark.asyncio
async def test_cursor_follows_selected_app_across_a_real_pilot_header_click(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=10 * 1024**2, swap=1 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=0, swap=4 * 1024**2)
    _app_unit(root, "app-charlie.service", ram=0, swap=2 * 1024**2)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(row=table.get_row_index(row_key("alpha", "user")))  # first row

        await _click_header(pilot, table, "swap")  # a real click, SWAP desc

        assert _row_names(table) == ["bravo", "charlie", "alpha"]  # the click moved alpha
        cursor_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert cursor_key.value == row_key("alpha", "user")


# --- sorting compares full raw values, never truncated rendered text -----------


@pytest.mark.asyncio
async def test_sorting_by_app_uses_full_name_not_truncated_rendered_text(
    tmp_path: Path,
) -> None:
    # Both names share a 31-char prefix, so `truncate_name`'s 32-char cap
    # would render them identically ("<prefix>…"); only the untruncated name
    # tells them apart.
    prefix = "x" * 31
    root = _base_tree(tmp_path)
    _app_unit(root, f"{prefix}b1.service", ram=1 * 1024**2, swap=0)
    _app_unit(root, f"{prefix}c2.service", ram=2 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        assert _row_names(table) == [f"{prefix}c2", f"{prefix}b1"]  # TOTAL desc, not insertion

        header_event = DataTable.HeaderSelected(table, ColumnKey("app"), 1, Text("APP"))
        screen.on_data_table_header_selected(header_event)  # sort by APP, A-Z

        assert _row_names(table) == [f"{prefix}b1", f"{prefix}c2"]


# --- system rows render with a "[sys]" suffix -----------------------------------


@pytest.mark.asyncio
async def test_system_app_renders_with_sys_suffix_in_the_app_cell(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _system_unit(root, ram=1 * 1024**2, swap=0)

    async with _app(root, include_system=True).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)

        cell = table.get_cell(row_key("cups", "system"), "app")
        assert isinstance(cell, Text)
        assert cell.plain == "cups [sys]"


# --- externally sourced names render literally, markup never parsed ------------


@pytest.mark.asyncio
async def test_markup_like_app_name_is_rendered_literally(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-[link=evil]x.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)

        cell = table.get_cell(row_key("[link=evil]x", "user"), "app")
        assert isinstance(cell, Text)
        assert cell.plain == "[link=evil]x"
        assert cell.spans == []  # not parsed as Rich markup into a styled span


# --- a vanishing cgroup tree exits cleanly, no traceback ------------------------


@pytest.mark.asyncio
async def test_cgroup_vanishing_mid_tick_stops_the_timer_and_exits_with_code_1(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        shutil.rmtree(user_service_root(root, UID))  # the whole user tree vanishes
        screen.refresh_now()  # no CgroupUnavailableError traceback escapes this call

    assert app.return_code == 1
    assert app.cgroup_error_message is not None
    assert "missing cgroup path" in app.cgroup_error_message


@pytest.mark.asyncio
async def test_transient_memory_stat_failure_skips_the_tick_and_keeps_last_data(
    tmp_path: Path,
) -> None:
    # The user tree directory stays put but its memory.stat becomes
    # unparseable for one tick (e.g. read mid-write): the tick must be
    # skipped silently -- last data kept on screen, no exit -- not treated as
    # the tree vanishing.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    async with app.run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        assert _row_names(table) == ["alpha"]

        (user_service_root(root, UID) / "memory.stat").write_text("kernel 100\n")  # incomplete
        screen.refresh_now()  # must not raise, must not clear the table
        await pilot.press("z")  # `z` samples too: same transient failure, same skip

        assert _row_names(table) == ["alpha"]  # last known data stays on screen

    assert app.return_code is None  # the session is still running, not exited


@pytest.mark.asyncio
async def test_transient_os_error_skips_the_tick_and_next_tick_shows_fresh_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A transient OS-level read failure elsewhere in the collector (SPEC.md
    # "Behaviour details") must be treated the same as
    # `MemoryStatUnavailableError`: skip this tick, keep the last frame, and
    # let the next tick recover -- not a fatal, session-ending exception. The
    # fixture changes before the failing tick, so a passing test can't be
    # explained by the tick doing nothing either way: the failing tick must
    # still show the *old* value, and only the next tick shows the new one.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    real_read_system = main_screen.read_system
    calls = {"n": 0}

    def _flaky_read_system(root: Path, uid: int) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient read failure")
        return real_read_system(root, uid)

    async with app.run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        assert _row_names(table) == ["alpha"]

        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)  # changes before tick 1
        monkeypatch.setattr(main_screen, "read_system", _flaky_read_system)
        screen.refresh_now()  # tick 1: raises OSError internally, must not propagate
        ram_cell = table.get_cell(row_key("alpha", "user"), "ram")
        assert isinstance(ram_cell, Text)
        assert ram_cell.plain == "1 MiB"  # the failing tick kept the old frame
        assert app.return_code is None  # still running

        screen.refresh_now()  # tick 2: recovers, reads the already-changed fixture
        ram_cell = table.get_cell(row_key("alpha", "user"), "ram")
        assert isinstance(ram_cell, Text)
        assert ram_cell.plain == "5 MiB"  # fresh data, not the stale 1 MiB

    assert app.return_code is None  # the session ran to completion, not exited


@pytest.mark.asyncio
async def test_a_programming_error_in_a_tick_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only OSError/ValueError (transient reads) are swallowed. A programming
    # error (e.g. TypeError) is not "one bad tick" -- it must still surface,
    # not be silently eaten like a transient read failure.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    def _broken_read_system(root: Path, uid: int) -> object:
        raise TypeError("not a transient read failure")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        monkeypatch.setattr(main_screen, "read_system", _broken_read_system)
        with pytest.raises(TypeError):
            screen.refresh_now()


@pytest.mark.asyncio
async def test_a_value_error_from_the_apply_path_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `ValueError` is only swallowed when it comes from the collector reads
    # inside the `try`. `reorder_rows`'s own invariant check
    # (`src/appmem/ui/table_order.py`) also raises `ValueError`, but from the
    # apply step outside the `try` -- a broken invariant there is a
    # programming error, not a transient read failure, and must still
    # surface instead of silently freezing the table.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    def _broken_reorder_rows(table: object, ordered_keys: object) -> None:
        raise ValueError("broken invariant")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        monkeypatch.setattr(main_screen, "reorder_rows", _broken_reorder_rows)
        with pytest.raises(ValueError, match="broken invariant"):
            screen.refresh_now()


# --- narrow terminals hide ΔSWAP/ΔRAM below 95 columns ---------------------------


@pytest.mark.asyncio
async def test_delta_columns_hidden_below_95_columns(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "delta_swap" not in table.columns
        assert "delta_ram" not in table.columns


@pytest.mark.asyncio
async def test_delta_columns_shown_at_or_above_95_columns(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "delta_swap" in table.columns
        assert "delta_ram" in table.columns


@pytest.mark.asyncio
async def test_delta_columns_recompute_on_resize(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "delta_ram" in table.columns

        await pilot.resize_terminal(80, 24)
        assert "delta_ram" not in table.columns

        await pilot.resize_terminal(120, 35)
        assert "delta_ram" in table.columns


@pytest.mark.asyncio
async def test_sorting_by_delta_hidden_by_width_falls_back_to_total_desc(tmp_path: Path) -> None:
    # alpha: low TOTAL (1 MiB), bravo: high TOTAL (5 MiB); TOTAL-desc puts
    # bravo first. Sort by ΔSWAP while wide, then narrow the terminal so the
    # column (and thus the active sort) disappears.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    _app_unit(root, "app-bravo.service", ram=5 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        await pilot.press("d")  # sort by ΔSWAP desc

        await pilot.resize_terminal(80, 24)

        assert _row_names(table) == ["bravo", "alpha"]  # TOTAL desc, not left on ΔSWAP


@pytest.mark.asyncio
async def test_d_is_a_noop_while_delta_columns_are_hidden(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        await pilot.press("d")

        assert screen._sort_key == "total"  # pyright: ignore[reportPrivateUsage]


# --- `z` sorts by ZSWAP, inert while that column is hidden ----------------------


@pytest.mark.asyncio
async def test_z_sorts_by_zswap_and_reverses(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=0, swap=1 * 1024**2, zswapped=1 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=0, swap=3 * 1024**2, zswapped=3 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        await pilot.press("z")  # ZSWAP desc
        assert _row_names(table) == ["bravo", "alpha"]

        await pilot.press("z")  # repeat: reverses
        assert _row_names(table) == ["alpha", "bravo"]


@pytest.mark.asyncio
async def test_sorting_by_zswap_hidden_by_width_falls_back_to_total_desc(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=9 * 1024**2, zswapped=9 * 1024**2)
    _app_unit(root, "app-bravo.service", ram=20 * 1024**2, swap=0, zswapped=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        await pilot.press("z")  # sort by ZSWAP desc

        await pilot.resize_terminal(84, 24)  # below `_ZSWAP_MIN_WIDTH`

        assert _row_names(table) == ["bravo", "alpha"]  # TOTAL desc, not left on ZSWAP


@pytest.mark.asyncio
async def test_z_is_a_noop_while_zswap_hidden_by_width(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(84, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        await pilot.press("z")

        assert screen._sort_key == "total"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_z_is_a_noop_while_zswap_hidden_by_w(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        await pilot.press("w")  # hide it (shown by default)

        await pilot.press("z")

        assert screen._sort_key == "total"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_z_is_a_noop_while_zswap_is_off(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)  # no zswap fixtures: disabled/unsupported
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        await pilot.press("z")

        assert screen._sort_key == "total"  # pyright: ignore[reportPrivateUsage]


# --- header line never wraps, recomputed on resize -------------------------------


@pytest.mark.asyncio
async def test_header_line1_recomputes_on_resize(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    write_memory_stat(user_service_root(tmp_path, UID))

    async with _app(root).run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        header1 = screen.query_one("#header1", Static)
        wide_text = str(header1.content)

        await pilot.resize_terminal(30, 24)
        await pilot.pause()  # let the resize settle before reading the recomputed header
        narrow_text = str(header1.content)

        assert narrow_text != wide_text
        assert len(narrow_text) <= len(wide_text)


# --- Δ columns render dim for small deltas and a young baseline -----------------


@pytest.mark.asyncio
async def test_delta_cell_is_dim_while_baseline_is_young(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        cell = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(cell, Text)
        assert cell.plain == "·"  # baseline just taken: Δ is 0, under 1 MiB
        assert cell.style == "dim"


@pytest.mark.asyncio
async def test_delta_cell_not_dim_once_baseline_is_old_and_delta_is_large(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        screen._baseline_time -= timedelta(  # pyright: ignore[reportPrivateUsage]
            seconds=120
        )  # pretend the baseline is old

        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)  # +4 MiB: not small
        screen.refresh_now()

        cell = _table(pilot).get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(cell, Text)
        assert cell.plain == "+4 MiB"
        assert cell.style == ""


@pytest.mark.asyncio
async def test_delta_cell_undims_at_60s_even_when_its_text_is_unchanged(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        _app_unit(root, "app-alpha.service", ram=5 * 1024**2, swap=0)
        screen.refresh_now()  # "+4 MiB", still dim: the baseline is young
        young = _table(pilot).get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(young, Text)
        assert (young.plain, young.style) == ("+4 MiB", "dim")

        screen._baseline_time -= timedelta(seconds=120)  # pyright: ignore[reportPrivateUsage]
        screen.refresh_now()  # same data, same text: only the style changes

        cell = _table(pilot).get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(cell, Text)
        assert (cell.plain, cell.style) == ("+4 MiB", "")


@pytest.mark.asyncio
async def test_delta_cell_dim_dot_for_small_delta_even_once_seasoned(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        screen._baseline_time -= timedelta(  # pyright: ignore[reportPrivateUsage]
            seconds=120
        )  # pretend the baseline is old

        # A sub-1-MiB move: still "·", now for the small-delta reason, not youth.
        _app_unit(root, "app-alpha.service", ram=(1024**2) + 500 * 1024, swap=0)
        screen.refresh_now()

        cell = _table(pilot).get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(cell, Text)
        assert cell.plain == "·"
        assert cell.style == "dim"


# --- footer is present with key caps ----------------------------------------------


@pytest.mark.asyncio
async def test_footer_shows_key_caps(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        footer = screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)

        text = content.plain
        assert "sort" in text
        assert "q quit" in text
        s_index = text.index("s")
        styles = [str(span.style) for span in content.spans if span.start <= s_index < span.end]
        assert any("reverse" in style for style in styles)


@pytest.mark.asyncio
async def test_footer_drops_lowest_priority_items_at_55_columns(tmp_path: Path) -> None:
    # The footer never wraps. Below its natural width it drops items lowest
    # priority first, keeping help and quit no matter how narrow. `d` (sort
    # by ΔSWAP) already drops out of the "sort" item's own key caps below 95
    # columns (SPEC.md "Main view"), which shortens the line enough that
    # width 60 no longer forces any further drop -- 55 does.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(55, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        footer = screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)

        assert content.no_wrap is True
        assert content.cell_len <= 55
        assert "? help" in content.plain
        assert "q quit" in content.plain
        assert "reset Δ" not in content.plain  # lowest priority: dropped first
        assert "cache" not in content.plain


@pytest.mark.asyncio
async def test_theme_footer_item_shows_wide_and_drops_before_reset_delta(tmp_path: Path) -> None:
    # "theme" is the new lowest-priority item (SPEC.md "Main view"): it drops
    # before "reset Δ", which still fits at 75 columns.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "T theme" in content.plain

    async with _app(root).run_test(size=(75, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "T theme" not in content.plain
        assert "reset Δ" in content.plain


@pytest.mark.asyncio
async def test_zswap_footer_item_drops_right_after_cache(tmp_path: Path) -> None:
    # "zswap" sits in `_FOOTER_DROP_ORDER` right after "cache" (SPEC.md "Main
    # view"): it survives "cache" dropping, then drops itself before "system".
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "cache" not in content.plain
        assert "w zswap" in content.plain
        assert "x system" in content.plain

    async with _app(root).run_test(size=(55, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "w zswap" not in content.plain
        assert "x system" in content.plain


@pytest.mark.asyncio
async def test_sort_footer_item_drops_d_key_below_95_columns(tmp_path: Path) -> None:
    # `d` is a no-op while the Δ columns are hidden by width (`action_sort`
    # refuses it), so its key cap doesn't appear at all, rather than sitting
    # there doing nothing (SPEC.md "Main view").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "r s t sort" in content.plain
        assert "d" not in content.plain.split("sort")[0]

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "r s t d sort" in content.plain


@pytest.mark.asyncio
async def test_sort_footer_item_includes_z_only_while_zswap_is_shown(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "r s t d z sort" in content.plain  # ZSWAP shown by default

        await pilot.press("w")  # hide it
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "r s t d sort" in content.plain
        assert "z" not in content.plain.split("sort")[0]


@pytest.mark.asyncio
async def test_footer_shows_b_reset_delta_not_z(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "b reset Δ" in content.plain
        assert "z reset Δ" not in content.plain


# --- control characters never reach the terminal --------------------------------


@pytest.mark.asyncio
async def test_control_chars_in_app_name_are_escaped_in_the_table(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-evil\x1b[2Japp.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        cell = _table(pilot).get_cell(row_key("evil\x1b[2japp", "user"), "app")
        assert isinstance(cell, Text)
        assert "\x1b" not in cell.plain
        assert "\\x1b[2j" in cell.plain.lower()


@pytest.mark.asyncio
async def test_control_chars_in_system_app_name_are_escaped(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    unit_dir = tmp_path / "sys" / "fs" / "cgroup" / "system.slice" / "evil\x1b[2Jsys.service"
    make_unit(unit_dir, anon=1 * 1024**2, swap=0, pids=[9999])

    async with _app(root, include_system=True).run_test() as pilot:
        await pilot.pause()
        cell = _table(pilot).get_cell(row_key("evil\x1b[2jsys", "system"), "app")
        assert isinstance(cell, Text)
        assert "\x1b" not in cell.plain
        assert "\\x1b[2j" in cell.plain.lower()
        assert "[sys]" in cell.plain


@pytest.mark.asyncio
async def test_c1_control_in_app_name_is_escaped_in_the_table(tmp_path: Path) -> None:
    # U+009B (CSI) is a C1 control, not C0: a one-byte-at-a-time systemd
    # escape of its UTF-8 encoding (0xC2 0x9B) decodes to the real character,
    # which `escape_control_chars` must still catch (SPEC.md "Behaviour
    # details").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-evil\\xc2\\x9b31mred.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        keys = [key.value for key in table.rows]
        assert len(keys) == 1
        assert keys[0] is not None
        cell = table.get_cell(keys[0], "app")
        assert isinstance(cell, Text)
        assert not any(0x80 <= ord(char) <= 0x9F for char in cell.plain), repr(cell.plain)
        assert "\\x9b" in cell.plain


# --- CJK and other wide-character names -----------------------------------------


@pytest.mark.asyncio
async def test_cjk_app_name_decoded_and_shown_in_a_main_view_row(tmp_path: Path) -> None:
    # systemd escapes a non-ASCII unit name one UTF-8 byte at a time; the
    # decoded name must reach an actual rendered table row, not just
    # `app_name()`'s own return value (SPEC.md "Grouping").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-\\xe5\\xbe\\xae\\xe4\\xbf\\xa1.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert _row_names(table) == ["微信"]
        cell = table.get_cell(row_key("微信", "user"), "app")
        assert isinstance(cell, Text)
        assert cell.plain == "微信"


# --- header and footer never wrap, at any width ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 50, 40, 24, 1])
async def test_header_and_footer_stay_one_line_at_any_width(tmp_path: Path, width: int) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(width, 12)) as pilot:
        await pilot.pause()
        header1 = pilot.app.screen.query_one("#header1", Static)
        footer = pilot.app.screen.query_one("#footer", Static)
        assert header1.region.height == 1, (width, header1.region.height)
        assert footer.region.height == 1, (width, footer.region.height)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 40, 24, 1])
async def test_rendered_header_and_footer_lines_crop_to_width(tmp_path: Path, width: int) -> None:
    # `region.height == 1` alone doesn't prove the *text* fits: a `Static`
    # can report one line of height while Rich still wraps or overflows its
    # content within it. Render the actual strips Textual would paint and
    # measure their real cell width instead (SPEC.md "Main view").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(width, 12)) as pilot:
        await pilot.pause()
        for selector in ("#header1", "#header2", "#footer"):
            widget = pilot.app.screen.query_one(selector, Static)
            lines = widget.render_lines(widget.region.reset_offset)
            assert len(lines) == 1, (selector, len(lines))
            text = "".join(segment.text for segment in lines[0])
            assert cell_len(text) <= width, (selector, text)


@pytest.mark.asyncio
async def test_table_shows_data_rows_at_40x10(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(40, 10)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert table.row_count > 0
        assert table.scrollable_content_region.height >= 1


# --- numeric columns fit at 60 columns -------------------------------------------


@pytest.mark.asyncio
async def test_total_column_fits_on_screen_at_60_columns_with_a_long_name(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, f"app-{'x' * 40}.service", ram=12 * 1024**3, swap=3 * 1024**3)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        idx = table.get_column_index("total")
        region = table._get_column_region(idx)  # pyright: ignore[reportPrivateUsage]
        assert region.right <= table.size.width, (region, table.size.width)


@pytest.mark.asyncio
async def test_total_column_fits_after_resizing_from_90_to_60(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, f"app-{'a' * 25}.service", ram=12 * 1024**3, swap=3 * 1024**3)

    async with _app(root).run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        idx = table.get_column_index("app")
        cell = str(table.get_cell_at(Coordinate(0, idx)))
        assert "…" not in cell, "already cropped at 90 columns"

        await pilot.resize_terminal(60, 24)
        await pilot.pause()

        idx = table.get_column_index("total")
        region = table._get_column_region(idx)  # pyright: ignore[reportPrivateUsage]
        assert region.right <= table.size.width, (region, table.size.width)
        idx = table.get_column_index("app")
        cell = str(table.get_cell_at(Coordinate(0, idx)))
        assert "…" in cell, "long name not cropped at 60 columns"


@pytest.mark.asyncio
async def test_app_column_widens_back_after_resizing_from_60_to_90(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, f"app-{'a' * 25}.service", ram=12 * 1024**3, swap=3 * 1024**3)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        idx = table.get_column_index("app")
        cell = str(table.get_cell_at(Coordinate(0, idx)))
        assert "…" in cell, "long name not cropped at 60 columns"

        await pilot.resize_terminal(90, 24)
        await pilot.pause()

        idx = table.get_column_index("total")
        region = table._get_column_region(idx)  # pyright: ignore[reportPrivateUsage]
        assert region.right <= table.size.width, (region, table.size.width)
        idx = table.get_column_index("app")
        cell = str(table.get_cell_at(Coordinate(0, idx)))
        assert "…" not in cell, "still cropped back at 90 columns"


# --- slow reads must not freeze input --------------------------------------------


@pytest.mark.asyncio
async def test_slow_tick_does_not_block_key_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A synchronous tick blocks the whole event loop for its own blocking
    # call, so nothing else -- not even this test's polling -- can run
    # concurrently with it: only the *total* time from "a read started" to
    # "the key landed" tells the threaded and synchronous versions apart.
    # Threaded code finishes this in milliseconds; a reverted-to-synchronous
    # tick can't finish any faster than the collector's own bounded wait.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    _app_unit(root, "app-beta.service", ram=2 * 1024**2, swap=0)
    real_read_system = main_screen.read_system
    started = threading.Event()
    release = threading.Event()

    def blocking_read_system(root: Path, uid: int) -> object:
        started.set()
        release.wait(timeout=2)  # bounded: the worker thread never hangs forever
        return real_read_system(root, uid)

    app = AppMemApp(root=root, uid=UID, interval=0.05, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(main_screen, "read_system", blocking_read_system)
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
        assert isinstance(screen, MainScreen)
        deadline = time.monotonic() + 5
        while screen._tick_in_flight and time.monotonic() < deadline:  # pyright: ignore[reportPrivateUsage]
            await pilot.pause(0.01)
        assert not screen._tick_in_flight, "blocked read never finished"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_at_most_one_tick_read_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    real_collect_apps = main_screen._collect_apps  # pyright: ignore[reportPrivateUsage]
    lock = threading.Lock()
    counts = {"current": 0, "max": 0}

    def slow_collect_apps(root: Path, uid: int, include_system: bool) -> list[AppStats]:
        with lock:
            counts["current"] += 1
            counts["max"] = max(counts["max"], counts["current"])
        time.sleep(0.3)
        try:
            return real_collect_apps(root, uid, include_system)
        finally:
            with lock:
                counts["current"] -= 1

    app = AppMemApp(root=root, uid=UID, interval=0.1, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(main_screen, "_collect_apps", slow_collect_apps)
        await pilot.pause(0.6)  # several 0.1s intervals elapse while a 0.3s read is in flight

        assert counts["max"] <= 1, "two tick reads were in flight at once"


@pytest.mark.asyncio
async def test_stale_tick_result_is_discarded_after_toggling_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tick starts reading with `include_system=False`; before it finishes,
    # `x` toggles it on (a synchronous `refresh_now`). The slow tick's result
    # still carries the old `include_system=False` it was read with, so it
    # must be discarded on arrival instead of overwriting the just-toggled
    # table and silently hiding the system row again (SPEC.md "Tech").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    _system_unit(root, ram=2 * 1024**2, swap=0)
    real_collect_apps = main_screen._collect_apps  # pyright: ignore[reportPrivateUsage]

    def slow_collect_apps(root: Path, uid: int, include_system: bool) -> list[AppStats]:
        if not include_system:
            time.sleep(0.4)  # the stale (pre-toggle) read: lands after the toggle below
        return real_collect_apps(root, uid, include_system)

    app = AppMemApp(root=root, uid=UID, interval=0.1, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(main_screen, "_collect_apps", slow_collect_apps)
        await pilot.pause(0.15)  # a periodic tick is now reading with include_system=False
        await pilot.press("x")  # explicit toggle: synchronous refresh_now(include_system=True)
        await pilot.pause()
        assert "cups" in _row_names(_table(pilot))

        await pilot.pause(0.5)  # let the slow, now-stale background read arrive and try to apply
        assert "cups" in _row_names(_table(pilot)), "a stale pre-toggle result overwrote the table"


# --- theme picker (T / Ctrl+P) -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", THEME_NAMES)
async def test_every_builtin_theme_renders_the_main_view(tmp_path: Path, theme_name: str) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        root=root,
        uid=UID,
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme=theme_name,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.theme == theme_name
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        header = screen.query_one("#header1", Static).content
        assert isinstance(header, Text)
        assert header.plain
        footer = screen.query_one("#footer", Static)
        assert isinstance(footer.content, Text)
        assert "q quit" in footer.content.plain
    assert not config_path().exists()  # a startup theme is never written back


def _style_at(text: Text, substr: str) -> str:
    start = text.plain.index(substr)
    end = start + len(substr)
    styles = [str(span.style) for span in text.spans if span.start <= start and span.end >= end]
    assert styles, f"no style span covers {substr!r} in {text.plain!r}"
    return styles[-1]


@pytest.mark.asyncio
async def test_startup_theme_warning_shows_as_a_notification(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        root=root,
        uid=UID,
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme_warnings=("APPMEM_THEME='bogus-theme' is not a known theme; ignoring it",),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        notifications = list(pilot.app._notifications)  # pyright: ignore[reportPrivateUsage]
        assert any(n.severity == "warning" and "bogus-theme" in n.message for n in notifications)


@pytest.mark.asyncio
async def test_header_colours_follow_the_running_apps_current_theme(tmp_path: Path) -> None:
    # Swap 92 % used, above the header's 90 % error threshold (the 50 %
    # warning is gone -- SPEC.md "Main view").
    write_memory_stat(user_service_root(tmp_path, UID))
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=2_560_000,
    )
    _app_unit(tmp_path, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        root=tmp_path,
        uid=UID,
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="dracula",
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        header = screen.query_one("#header2", Static).content  # Swap line
        assert isinstance(header, Text)

        dracula_error = Color.parse(BUILTIN_THEMES["dracula"].error or "").rich_color.name
        # This fixture is kB (helpers.write_meminfo); read_system converts to
        # bytes, so the pair rendered in the header uses bytes too.
        swap_total = 32_000_000 * 1024
        swap_free = 2_560_000 * 1024
        pair = format_pair(swap_total - swap_free, swap_total)
        assert dracula_error in _style_at(header, pair)

        pilot.app.theme = "nord"
        await pilot.pause()
        header_after = screen.query_one("#header2", Static).content
        assert isinstance(header_after, Text)
        nord_error = Color.parse(BUILTIN_THEMES["nord"].error or "").rich_color.name
        assert nord_error != dracula_error
        assert nord_error in _style_at(header_after, pair)
        assert dracula_error not in _style_at(header_after, pair)


@pytest.mark.asyncio
async def test_writeback_rate_reaches_the_header_in_the_running_theme_colour(
    tmp_path: Path,
) -> None:
    # `_update_header_lines` must actually pass `self._writeback_rate` through
    # to `render_header` -- `update_writeback` and `render_header` are each
    # tested on their own (test_writeback.py, test_ui_header.py), but nothing
    # else covers this join, the `to disk` token as it runs live.
    root = _zswap_base_tree(tmp_path)
    write_vmstat(root, zswpwb=0)

    app = AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=False, theme="dracula"
    )
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        stats = screen._last_stats  # pyright: ignore[reportPrivateUsage]
        assert stats is not None

        # Seed a sample from "1 s ago" so this call has a deterministic
        # elapsed time to compute a rate from, instead of depending on how
        # much real wall-clock time the test happens to take.
        screen._writeback_history = ((time.monotonic() - 1.0, 0),)  # pyright: ignore[reportPrivateUsage]
        screen._update_header(replace(stats, zswap_writeback_bytes=10 * 1024 * 1024))  # pyright: ignore[reportPrivateUsage]

        header = screen.query_one("#header2", Static).content  # Swap line
        assert isinstance(header, Text)
        assert "to disk " in header.plain

        dracula_warning = Color.parse(BUILTIN_THEMES["dracula"].warning or "").rich_color.name
        assert dracula_warning in _style_at(header, "to disk ")


@pytest.mark.asyncio
async def test_t_opens_the_picker_and_picking_a_theme_sets_and_writes_it(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert pilot.app.theme != "dracula"  # sanity: not already the target
        assert not config_path().exists()

        await pilot.press("T")
        await pilot.pause()
        await pilot.press(*"dracula")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.theme == "dracula"
        assert config_path().read_text() == 'theme = "dracula"\n'


@pytest.mark.asyncio
async def test_esc_in_the_picker_reverts_and_writes_nothing(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        await pilot.press(*"dracula")  # typed into the picker's filter, never confirmed
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert pilot.app.theme == before
        assert not config_path().exists()
        assert pilot.app.screen_stack == [pilot.app.screen]  # picker actually closed


@pytest.mark.asyncio
async def test_browsing_the_picker_without_confirming_writes_nothing(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()

        assert pilot.app.theme == before
        assert not config_path().exists()

        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen_stack == [pilot.app.screen]


@pytest.mark.asyncio
async def test_write_failure_notifies_and_the_app_keeps_running(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        path = config_path()
        path.parent.mkdir(parents=True)
        path.parent.chmod(0o500)
        try:
            if os.access(path.parent, os.W_OK):  # running as root
                pytest.skip("cannot make a directory read-only to this user")

            await pilot.press("T")
            await pilot.pause()
            await pilot.press(*"dracula")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        finally:
            path.parent.chmod(0o700)

        # The theme still applies in the running app even though persisting
        # it failed (SPEC.md "Command line": a write failure never crashes).
        assert pilot.app.theme == "dracula"
        assert not path.exists()
        notifications = list(pilot.app._notifications)  # pyright: ignore[reportPrivateUsage]
        assert any(n.severity == "error" for n in notifications)

        # Still alive and responsive: an ordinary key still works after the failure.
        await pilot.press("c")  # toggle the cache column
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        assert "alpha" in _row_names(_table(pilot))


# --- picker opens on the current theme, marked ----------------------------------


def _prompt_text(option: Command) -> str:
    """An option's rendered prompt as plain text: `Command.prompt` is a
    `Content` in practice (built by `CommandPalette._gather_commands`), but
    typed as the broader `VisualType`, hence the `isinstance` narrowing
    rather than a bare `.plain` access."""
    prompt = option.prompt
    return prompt.plain if isinstance(prompt, Content) else str(prompt)


def _highlighted_theme(palette: ThemePalette) -> tuple[str | None, str]:
    """`(theme name, its rendered prompt text)` of the picker's highlighted
    row, or `(None, "")` if nothing is highlighted."""
    command_list = palette.query_one(CommandList)
    option = command_list.highlighted_option
    if not isinstance(option, Command):
        return None, ""
    return option.hit.text, _prompt_text(option)


@pytest.mark.asyncio
async def test_picker_opens_with_the_cursor_on_the_current_theme(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=False, theme="dracula"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ThemePalette)
        name, prompt = _highlighted_theme(screen)
        assert name == "dracula"
        assert prompt == "✓ dracula"


@pytest.mark.asyncio
async def test_picker_via_ctrl_p_theme_also_opens_on_the_current_theme(tmp_path: Path) -> None:
    # `T` and Ctrl+P -> Theme both call `App.search_themes` (SPEC.md
    # "Command line"): this covers the second entry point.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=False, theme="nord"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"Theme")
        await pilot.pause()
        await pilot.press("enter")  # select the "Theme" system command
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ThemePalette)
        name, prompt = _highlighted_theme(screen)
        assert name == "nord"
        assert prompt == "✓ nord"


@pytest.mark.asyncio
async def test_picker_cursor_moves_with_a_new_pick(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert pilot.app.theme != "dracula"

        await pilot.press("T")
        await pilot.pause()
        await pilot.press(*"dracula")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.theme == "dracula"

        await pilot.press("T")  # reopen: the cursor follows the just-made pick
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ThemePalette)
        name, prompt = _highlighted_theme(screen)
        assert name == "dracula"
        assert prompt == "✓ dracula"


@pytest.mark.asyncio
async def test_picker_cursor_on_a_config_theme_after_restart(tmp_path: Path) -> None:
    # A theme resolved from the config file (or `--theme`/`APPMEM_THEME`) is
    # `App.theme` before the picker ever opens, same as an in-app pick --
    # `search_themes` reads `self.app.theme` fresh, so no separate wiring is
    # needed for "restart with a config theme" to work.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    config_path().parent.mkdir(parents=True)
    config_path().write_text('theme = "gruvbox"\n')

    app = AppMemApp(
        root=root,
        uid=UID,
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="gruvbox",
        config_theme="gruvbox",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert pilot.app.theme == "gruvbox"

        await pilot.press("T")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ThemePalette)
        name, prompt = _highlighted_theme(screen)
        assert name == "gruvbox"
        assert prompt == "✓ gruvbox"


@pytest.mark.asyncio
async def test_picker_marks_only_the_current_theme(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        root=root, uid=UID, interval=NO_AUTO_REFRESH_INTERVAL, include_system=False, theme="dracula"
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ThemePalette)
        command_list = screen.query_one(CommandList)
        marks: list[str | None] = []
        for index in range(command_list.option_count):
            option = command_list.get_option_at_index(index)
            assert isinstance(option, Command)
            if _prompt_text(option).startswith("✓"):
                marks.append(option.hit.text)
        assert marks == ["dracula"]  # exactly one mark, on the current theme


@pytest.mark.asyncio
async def test_picker_search_keeps_the_fuzzy_match_highlight(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("T", *"drac")
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, ThemePalette)
        option = screen.query_one(CommandList).get_option_at_index(0)
        assert isinstance(option, Command)
        assert isinstance(option.prompt, Content)
        assert option.prompt.plain == "  dracula"
        assert option.prompt.spans  # the matched letters stay highlighted after the mark


@pytest.mark.asyncio
async def test_picker_still_previews_confirms_and_cancels_as_before(tmp_path: Path) -> None:
    # The cursor-on-current-theme change must not touch the rest of the
    # picker's behaviour (SPEC.md "Command line"): typed search still
    # filters, Enter still applies and persists, Esc still cancels and
    # writes nothing -- this is `test_t_opens_the_picker_...`/
    # `test_esc_in_the_picker_...` above, just via the new `ThemePalette`.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ThemePalette)
        await pilot.press(*"dracula")  # typed filter still narrows the list
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.theme == before  # Esc: cancelled, nothing applied
        assert not config_path().exists()

        await pilot.press("T")
        await pilot.pause()
        await pilot.press(*"dracula")
        await pilot.pause()
        await pilot.press("enter")  # Enter: applies and persists
        await pilot.pause()
        assert pilot.app.theme == "dracula"
        assert config_path().read_text() == 'theme = "dracula"\n'


# --- header locale/contrast detection (SPEC.md "Main view") ------------------


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("C", True),
        ("POSIX", True),
        ("C.UTF-8", False),  # a real UTF-8 locale, unlike a bare LANG=C
        ("en_US.UTF-8", False),
        ("en_US.utf8", False),
        ("en_US.ISO-8859-1", True),
        ("en_US", True),  # no codeset at all: can't assume UTF-8
        ("de_DE.UTF-8@euro", False),  # a modifier isn't part of the codeset
        ("de_DE.ISO-8859-15@euro", True),
    ],
)
def test_is_non_utf8_locale(setting: str, expected: bool) -> None:
    assert main_screen._is_non_utf8_locale(setting) is expected  # pyright: ignore[reportPrivateUsage]


def test_locale_setting_prefers_lc_all_then_lc_ctype_then_lang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_CTYPE", "")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert main_screen._locale_setting() == "en_US.UTF-8"  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("LC_CTYPE", "C")
    assert main_screen._locale_setting() == "C"  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("LC_ALL", "POSIX")
    assert main_screen._locale_setting() == "POSIX"  # pyright: ignore[reportPrivateUsage]


def test_locale_setting_defaults_to_c_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    assert main_screen._locale_setting() == "C"  # pyright: ignore[reportPrivateUsage]


def test_detect_ascii_bars_follows_the_locale_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real process started with a bare `LANG=C` already has
    # `LC_CTYPE=C.UTF-8` from Python's PEP 538 coercion; this checks that
    # the environment, not a value cached at import, decides.
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", "C")
    assert main_screen._detect_ascii_bars() is True  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert main_screen._detect_ascii_bars() is False  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_ascii_bars_flag_is_wired_from_detection_into_the_rendered_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", "C")
    root = _base_tree(tmp_path)

    async with _app(root).run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        assert screen._ascii_bars is True  # pyright: ignore[reportPrivateUsage]
        header1 = screen.query_one("#header1", Static).content
        assert isinstance(header1, Text)
        assert "#" in header1.plain
        assert "█" not in header1.plain and "░" not in header1.plain


# --- bar fill contrast (SPEC.md "Main view", "Colour") -----------------------


def test_contrast_ratio_of_black_and_white_is_maximal() -> None:
    ratio = main_screen._contrast_ratio(  # pyright: ignore[reportPrivateUsage]
        Color.parse("#000000"), Color.parse("#ffffff")
    )
    assert ratio == pytest.approx(21.0, abs=0.1)


def test_contrast_ratio_of_identical_colours_is_one() -> None:
    ratio = main_screen._contrast_ratio(  # pyright: ignore[reportPrivateUsage]
        Color.parse("#336699"), Color.parse("#336699")
    )
    assert ratio == pytest.approx(1.0, abs=0.001)


@pytest.mark.parametrize("theme_name", sorted(BUILTIN_THEMES))
def test_bar_fill_colour_clears_3_to_1_contrast_in_every_built_in_theme(theme_name: str) -> None:
    theme = BUILTIN_THEMES[theme_name]
    fill = main_screen._bar_fill_colour(theme)  # pyright: ignore[reportPrivateUsage]

    if theme.ansi:  # no real RGB to measure a ratio against; just don't crash
        assert fill
        return

    background = main_screen._theme_background(theme)  # pyright: ignore[reportPrivateUsage]
    # `fill` is already a Rich colour NAME (`_rich_color`'s output), not
    # necessarily a hex string `Color.parse` round-trips cleanly -- recompute
    # the same candidate the implementation chose between and check that one.
    primary = Color.parse(theme.primary)
    contrast_primary = main_screen._contrast_ratio(primary, background)  # pyright: ignore[reportPrivateUsage]
    if contrast_primary >= main_screen._MIN_BAR_CONTRAST:  # pyright: ignore[reportPrivateUsage]
        assert contrast_primary >= 3.0
    else:
        foreground = Color.parse(theme.foreground) if theme.foreground else background.inverse
        contrast_foreground = main_screen._contrast_ratio(  # pyright: ignore[reportPrivateUsage]
            foreground, background
        )
        assert contrast_foreground >= 3.0


def test_bar_fill_colour_falls_back_to_foreground_on_flexoki() -> None:
    # flexoki's own accent measures 2.93:1 against its background -- below
    # the 3:1 floor, so the bar must fall back to its foreground colour.
    theme = BUILTIN_THEMES["flexoki"]
    background = main_screen._theme_background(theme)  # pyright: ignore[reportPrivateUsage]
    primary_contrast = main_screen._contrast_ratio(  # pyright: ignore[reportPrivateUsage]
        Color.parse(theme.primary), background
    )
    assert primary_contrast < 3.0

    fill = main_screen._bar_fill_colour(theme)  # pyright: ignore[reportPrivateUsage]
    assert fill != Color.parse(theme.primary).rich_color.name
    foreground = Color.parse(theme.foreground) if theme.foreground else background.inverse
    assert fill == foreground.rich_color.name


def test_bar_fill_colour_skips_contrast_math_for_ansi_themes() -> None:
    theme = BUILTIN_THEMES["ansi-dark"]
    fill = main_screen._bar_fill_colour(theme)  # pyright: ignore[reportPrivateUsage]
    assert fill == Color.parse(theme.primary).rich_color.name


# --- #header3 hidden below H=18, table reclaims the row ----------------------


@pytest.mark.asyncio
async def test_header3_hidden_and_table_reclaims_its_row_below_height_18(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 15)) as pilot:
        await pilot.pause()
        header3 = pilot.app.query_one("#header3", Static)
        assert header3.display is False
        table = _table(pilot)
        assert table.region.y == 2  # header1 + header2, no header3 row

    async with _app(root).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        header3 = pilot.app.query_one("#header3", Static)
        assert header3.display is True
        table = _table(pilot)
        assert table.region.y == 3  # header1 + header2 + header3


# --- NO_COLOR: glyphs and content survive with styling suppressed ------------


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (100, 30), (80, 24), (90, 15), (70, 15), (40, 10)])
async def test_header_renders_under_no_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=size) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        header1 = screen.query_one("#header1", Static).content
        assert isinstance(header1, Text)
        assert "RAM" in header1.plain
        assert any(glyph in header1.plain for glyph in ("█", "#", "/"))  # bar or bare pair
