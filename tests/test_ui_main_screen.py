"""Textual pilot tests for `AppMemApp`/`MainScreen` (SPEC.md "Main view", "Tests").

No live `/sys` or `/proc`: `AppMemApp` takes the same fixture `root`/`uid` as
`collect` (AGENTS.md). The interval is set high enough that the automatic
timer never fires during a test; refreshes are triggered deterministically by
calling `MainScreen.refresh_now()` after mutating the fixture on disk.
"""

from __future__ import annotations

import gc
import os
import shutil
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.color import Color
from textual.pilot import Pilot
from textual.strip import Strip
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import OptionList, Static
from textual.worker import Worker

from appmem import collect
from appmem.collect import LinuxBackend
from appmem.fmt import format_pair
from appmem.model import AppStats
from appmem.theme import TERMINAL_THEMES, THEME_NAMES, config_path, resolve_theme
from appmem.ui import app as ui_app
from appmem.ui.app import AppMemApp
from appmem.ui.rows import row_key
from appmem.ui.screens import help as help_screen
from appmem.ui.screens import main as main_screen
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.screens.processes import ProcessesScreen
from appmem.ui.table import RowTable
from appmem.ui.theme_picker import ThemePanel
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


def _table(pilot: Pilot[None]) -> RowTable:
    return pilot.app.query_one(RowTable)


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
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=include_system,
    )


def _row_names(table: RowTable) -> list[str]:
    # Row keys are "<scope>\0<name>" (SPEC.md "Grouping"); every app in these
    # fixtures is "user" scope, so stripping the prefix recovers the app name.
    names: list[str] = []
    for key in table.row_keys:
        _scope, _, name = key.partition("\0")
        names.append(name)
    return names


def _cursor_visible(table: RowTable) -> bool:
    top = table.scroll_offset.y
    rows_on_screen = table.size.height - 1  # line 0 is the fixed header
    return top <= table.cursor_row < top + rows_on_screen


async def _click_header(pilot: Pilot[None], table: RowTable, column_key: str) -> None:
    # A genuine pilot `click`, not calling `on_row_table_header_selected`
    # directly (SPEC.md "Tests"): computes the header cell's x offset from
    # the table's own column layout and clicks the header row (y=0).
    region = table.column_region(column_key)
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

        header_event = RowTable.HeaderSelected(table, "swap")
        screen.on_row_table_header_selected(header_event)
        assert _row_names(table) == ["bravo", "charlie", "alpha"]

        screen.on_row_table_header_selected(header_event)
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

        assert table.cursor_key == row_key("charlie", "user")


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
        header_event = RowTable.HeaderSelected(table, "cache")
        screen.on_row_table_header_selected(header_event)  # explicit sort by CACHE desc
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
        assert "cache" not in table.column_keys

        await pilot.press("c")
        assert "cache" in table.column_keys
        cache_cell = table.get_cell(row_key("alpha", "user"), "cache")
        assert isinstance(cache_cell, Text)
        assert cache_cell.plain == "5 MiB"

        await pilot.press("c")
        assert "cache" not in table.column_keys


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

        assert "zswap" in table.column_keys  # shown from the first tick, no `w` needed
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

        assert "zswap" not in table.column_keys


@pytest.mark.asyncio
async def test_w_toggles_the_zswap_column(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=5 * 1024**2, zswapped=5 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:  # >= _ZSWAP_MIN_WIDTH
        await pilot.pause()
        table = _table(pilot)
        assert "zswap" in table.column_keys  # shown by default

        await pilot.press("w")
        assert "zswap" not in table.column_keys

        await pilot.press("w")
        assert "zswap" in table.column_keys
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

        assert "zswap" not in table.column_keys


# --- ZSWAP hides below its own width threshold, on top of `w`/zswap-off --------


@pytest.mark.asyncio
async def test_zswap_column_hidden_below_85_columns(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(84, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "zswap" not in table.column_keys


@pytest.mark.asyncio
async def test_zswap_column_shown_at_85_columns(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(85, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "zswap" in table.column_keys


@pytest.mark.asyncio
async def test_zswap_column_recomputes_on_resize(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=1 * 1024**2, zswapped=1 * 1024**2)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "zswap" in table.column_keys

        await pilot.resize_terminal(80, 24)
        assert "zswap" not in table.column_keys

        await pilot.resize_terminal(120, 35)
        assert "zswap" in table.column_keys


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
        assert "zswap" in table.column_keys
        assert ("delta_swap" in table.column_keys) == (width >= 95)
        for key in ("ram", "swap", "zswap", "total", "procs"):
            region = table.column_region(key)
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

        region = table.column_region("procs")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if (
                table.scrollbar_size_vertical > 0
                and region.right <= table.scrollable_content_region.width
            ):
                break
            await pilot.pause(0.01)  # width sync runs after the row change's next refresh
            region = table.column_region("procs")
        assert table.scrollbar_size_vertical > 0
        assert region.right <= table.scrollable_content_region.width, (
            region,
            table.scrollable_content_region,
            screen._column_widths,  # pyright: ignore[reportPrivateUsage]
        )


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
        header_event = RowTable.HeaderSelected(table, "zswap")
        screen.on_row_table_header_selected(header_event)  # explicit sort by ZSWAP desc
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
        header_event = RowTable.HeaderSelected(table, "zswap")
        screen.on_row_table_header_selected(header_event)  # sort by it
        assert "zswap" in table.column_keys
        assert screen._sort_key == "zswap"  # pyright: ignore[reportPrivateUsage]

        write_zswap_enabled(root, enabled=False)
        screen.refresh_now()

        assert "zswap" not in table.column_keys
        assert screen._sort_key != "zswap"  # pyright: ignore[reportPrivateUsage]
        footer = screen.query_one("#footer", Static).content
        assert isinstance(footer, Text)
        assert "r s t d sort" in footer.plain  # `z` left the sort item with the column

        await pilot.press("w")  # a no-op again, same as before zswap ever turned on
        assert "zswap" not in table.column_keys


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
async def test_help_never_wraps_narrower_than_its_final_width(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user saw the help text flash as a 20-column strip with a scrollbar
    # before widening: the body was wrapped to the scroll container's size
    # before its first layout, when that size is still 0. Every wrap must use
    # the final content width, including the one that paints the first frame.
    widths: list[int] = []
    real_build_body = help_screen._build_body  # pyright: ignore[reportPrivateUsage]

    def spy(width: int, *, zswap_enabled: bool = False) -> str:
        widths.append(width)
        return real_build_body(width, zswap_enabled=zswap_enabled)

    monkeypatch.setattr(help_screen, "_build_body", spy)
    root = _base_tree(tmp_path)
    async with _app(root).run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        final_width = pilot.app.screen._content_width()  # pyright: ignore[reportPrivateUsage]
    assert widths
    assert final_width > 100  # a real width, not the pre-layout 0
    assert all(width == final_width for width in widths), widths


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
        keys = list(table.column_keys)

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
        assert _table(pilot).column_width("app") >= len(name)


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


# --- PROCS cadence: every 5th tick, a new unit is always counted (SPEC.md
# "Main view", "Tech") -----------------------------------------------------


@pytest.mark.asyncio
async def test_procs_recounts_every_fifth_tick_new_units_counted_immediately(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, procs=1)

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        table = _table(pilot)
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        def alpha_procs() -> str:
            cell = table.get_cell(row_key("alpha", "user"), "procs")
            assert isinstance(cell, Text)
            return cell.plain

        # Mount and the initial screen-resume each do their own synchronous
        # collection, so `_tick_count` is already past 0 by the time the
        # pilot pauses; advance to just after the next counting collection
        # so what follows lines up exactly with "tick 1 counts, ticks 2-5
        # carry forward, tick 6 recounts" (SPEC.md "Main view"), regardless
        # of exactly how many reads startup itself did.
        interval = main_screen._PROCS_COUNT_INTERVAL  # pyright: ignore[reportPrivateUsage]
        while screen._tick_count % interval != 1:  # pyright: ignore[reportPrivateUsage]
            screen.refresh_now()
        assert alpha_procs() == "1"  # tick 1: always counted

        _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, procs=5)

        screen.refresh_now()  # tick 2: carried forward, not recounted
        assert alpha_procs() == "1"

        _app_unit(root, "app-bravo.service", ram=1 * 1024**2, swap=0, procs=7)
        screen.refresh_now()  # tick 3: not a counting tick, but bravo is new
        bravo_cell = table.get_cell(row_key("bravo", "user"), "procs")
        assert isinstance(bravo_cell, Text)
        assert bravo_cell.plain == "7"  # a first-seen unit is always counted
        assert alpha_procs() == "1"  # alpha, already known, still carried forward

        screen.refresh_now()  # tick 4: still carried forward
        assert alpha_procs() == "1"

        screen.refresh_now()  # tick 5: still carried forward
        assert alpha_procs() == "1"

        screen.refresh_now()  # tick 6: every unit is recounted
        assert alpha_procs() == "5"


@pytest.mark.asyncio
async def test_periodic_tick_carries_forward_the_procs_count(tmp_path: Path) -> None:
    # Drives the real threaded path (`_tick` -> `_tick_worker` -> `TickDone`
    # -> `_apply_tick`), not `refresh_now`: the cadence test above only
    # exercises the synchronous path, so on its own it would not catch a
    # broken `self._procs_by_unit = result.procs_by_unit or {}` in
    # `_apply_tick` (review round 1, SHOULD 1). The interval is set high
    # enough that the automatic timer never fires (same as `_app`'s default
    # elsewhere in this file); `_tick()` is called by hand instead, one
    # collection at a time, waiting for its background thread and `TickDone`
    # to land before each assertion.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, procs=1)
    unit_key = str(user_service_root(root, UID) / "app.slice" / "app-alpha.service")

    async with _app(root).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        table = _table(pilot)

        def alpha_procs() -> str:
            cell = table.get_cell(row_key("alpha", "user"), "procs")
            assert isinstance(cell, Text)
            return cell.plain

        async def run_one_threaded_tick() -> None:
            screen._tick()  # pyright: ignore[reportPrivateUsage]
            deadline = time.monotonic() + 3
            while screen._tick_in_flight and time.monotonic() < deadline:  # pyright: ignore[reportPrivateUsage]
                await pilot.pause(0.02)
            assert not screen._tick_in_flight, "threaded tick never finished"  # pyright: ignore[reportPrivateUsage]

        # Mount and the initial screen-resume already did two synchronous
        # collections; land on a just-applied counting tick the same way the
        # cadence test above does, but by running real threaded ticks.
        interval = main_screen._PROCS_COUNT_INTERVAL  # pyright: ignore[reportPrivateUsage]
        while screen._tick_count % interval != 1:  # pyright: ignore[reportPrivateUsage]
            await run_one_threaded_tick()
        assert alpha_procs() == "1"

        # Change the fixture and run threaded ticks up to (and including) the
        # next counting tick: `_apply_tick` must copy the worker's own
        # `procs_by_unit` into `screen._procs_by_unit`, or the carried-forward
        # baseline for every later non-counting tick stays frozen at this
        # unit's very first count instead of following the real one (review
        # round 1, SHOULD 1) -- a counting tick alone can't tell the two
        # apart, since it always shows the fresh disk value either way.
        _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, procs=5)
        for _ in range(interval):
            await run_one_threaded_tick()
        assert alpha_procs() == "5"
        assert screen._procs_by_unit[unit_key] == 5  # pyright: ignore[reportPrivateUsage]

        # One more fixture change and a single non-counting tick: PROCS must
        # carry the just-counted "5" forward through the threaded path, not
        # jump to the new disk truth ("9") or fall back to a stale value.
        _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0, procs=9)
        await run_one_threaded_tick()
        assert alpha_procs() == "5"
        assert screen._procs_by_unit[unit_key] == 5  # pyright: ignore[reportPrivateUsage]


# --- column order: RAM before SWAP everywhere ----------------------------------


@pytest.mark.asyncio
async def test_column_order_is_app_ram_swap_total_procs(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        keys = list(table.column_keys)

        assert keys == ["app", "ram", "swap", "total", "delta_ram", "delta_swap", "procs"]


@pytest.mark.asyncio
async def test_column_order_with_cache_shown_is_ram_swap_cache_total(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        table = _table(pilot)
        keys = list(table.column_keys)

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

        # A tick that adds a row re-runs the column sync after layout; that
        # sync must not scroll either (only a resize may).
        _app_unit(root, "app-newcomer.service", ram=1024**2, swap=0)
        screen.refresh_now()
        await pilot.pause()
        await pilot.pause()
        assert table.scroll_y == scrolled_y


# --- a resize scrolls the selected row into view (run-2 fixes A2) --------------


@pytest.mark.asyncio
async def test_height_only_resize_keeps_cursor_on_screen(tmp_path: Path) -> None:
    # A2: `_sync_columns` only called `_rebuild_table(scroll=True)` when the
    # computed column widths actually changed, so a resize that only shrank
    # the height (leaving every width alone) never scrolled the cursor back
    # into the now-shorter viewport (SPEC.md "Main view").
    root = _base_tree(tmp_path)
    for index in range(60):  # more rows than either terminal size can show
        _app_unit(root, f"app-app{index:02d}.service", ram=(index + 2) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
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
async def test_width_and_height_shrink_still_keeps_cursor_on_screen(tmp_path: Path) -> None:
    # Unchanged existing behaviour: a resize that also crosses a column
    # threshold already rebuilt the table with `scroll=True`.
    root = _base_tree(tmp_path)
    for index in range(60):
        _app_unit(root, f"app-app{index:02d}.service", ram=(index + 2) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(table.row_count - 1)
        await pilot.pause()

        await pilot.resize_terminal(90, 20)  # crosses 95: ΔSWAP/ΔRAM hidden, columns rebuilt
        await pilot.pause()
        await pilot.pause()

        table = _table(pilot)
        assert _cursor_visible(table), (table.scroll_offset.y, table.cursor_row, table.size)


# --- resuming from the process view scrolls the selection into view (A3) -------


@pytest.mark.asyncio
async def test_back_from_process_view_keeps_selected_app_on_screen(tmp_path: Path) -> None:
    # A3: `on_screen_resume` called `refresh_now()` with the default
    # `scroll=False`, so a resort while the process view was open (the
    # selected app's rank dropping) could leave the selection off-screen
    # after Esc (SPEC.md "Main view": drilling out scrolls the selection
    # into view; returning from the process view is a drill-out).
    root = _base_tree(tmp_path)
    for index in range(40):
        _app_unit(root, f"app-app{index:02d}.service", ram=(40 - index) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.move_cursor(10)
        await pilot.pause()
        selected = table.cursor_key
        assert _cursor_visible(table)

        await pilot.press("enter")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProcessesScreen)

        # While the process view is open, the selected app's rank collapses to
        # last (1 MiB TOTAL, not less: a lower value would hide the row
        # instead of demoting it, per SPEC.md "Behaviour details").
        _app_unit(root, "app-app10.service", ram=1 * 1024**2, swap=0)

        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        table = _table(pilot)
        assert table.cursor_key == selected
        assert _cursor_visible(table), (table.scroll_offset.y, table.cursor_row, table.size)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["question_mark", "T"])
async def test_closing_help_or_theme_panel_keeps_wheel_scroll(tmp_path: Path, key: str) -> None:
    # Only a return from the process view is a drill-out; closing help or the
    # theme panel resumes this screen too but must not scroll (SPEC.md "Main view").
    root = _base_tree(tmp_path)
    for index in range(60):
        _app_unit(root, f"app-app{index:02d}.service", ram=(index + 2) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.post_message(events.MouseScrollDown(table, 10, 10, 0, 0, 0, False, False, False))
        await pilot.pause()
        scrolled_y = table.scroll_y
        assert scrolled_y > 0
        await pilot.press(key, "escape")
        await pilot.pause()
        await pilot.pause()
        assert table.scroll_y == scrolled_y


@pytest.mark.asyncio
async def test_column_resize_scrolls_selected_app_into_view(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    for index in range(60):
        _app_unit(root, f"app-app{index:02d}.service", ram=(index + 2) * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        table.post_message(events.MouseScrollDown(table, 10, 10, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert table.scroll_y > 0
        await pilot.resize_terminal(95, 20)
        await pilot.pause()
        assert table.scroll_y == 0
        assert _cursor_visible(table)


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
        assert table.cursor_key == row_key("alpha", "user")


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
        assert table.cursor_key == row_key("alpha", "user")


# --- PROCS shows its own sort marker (run-2 fixes A4) ---------------------------


@pytest.mark.asyncio
async def test_procs_sort_shows_marker(tmp_path: Path) -> None:
    # A4: PROCS was 6 cells wide, but "PROCS ▾"/"PROCS ▴" is 7, so `RowTable.
    # _fit` cut the marker off -- the only column where this happened
    # (SPEC.md "Main view": "the sort marker sits on the sorted column").
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    _app_unit(root, "app-bravo.service", ram=2 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        await _click_header(pilot, table, "procs")
        header = table.render_line(0).text
        region = table.column_region("procs")
        assert header[region.x : region.x + region.width].strip() in ("PROCS ▾", "PROCS ▴")

        await _click_header(pilot, table, "procs")
        header = table.render_line(0).text
        assert header[region.x : region.x + region.width].strip() in ("PROCS ▾", "PROCS ▴")


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

        header_event = RowTable.HeaderSelected(table, "app")
        screen.on_row_table_header_selected(header_event)  # sort by APP, A-Z

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

    real_read_system = collect.read_system
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
        monkeypatch.setattr(collect, "read_system", _flaky_read_system)
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

        monkeypatch.setattr(collect, "read_system", _broken_read_system)
        with pytest.raises(TypeError):
            screen.refresh_now()


@pytest.mark.asyncio
async def test_a_value_error_from_the_apply_path_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `ValueError` is only swallowed when it comes from the collector reads
    # inside the `try`. `RowTable.reorder`'s own invariant check also raises
    # `ValueError`, but from the apply step outside the `try` -- a broken
    # invariant there is a programming error, not a transient read failure,
    # and must still surface instead of silently freezing the table.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = _app(root)

    def _broken_reorder(self: RowTable, ordered_keys: list[str]) -> None:
        raise ValueError("broken invariant")

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)

        monkeypatch.setattr(RowTable, "reorder", _broken_reorder)
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

        assert "delta_swap" not in table.column_keys
        assert "delta_ram" not in table.column_keys


@pytest.mark.asyncio
async def test_delta_columns_shown_at_or_above_95_columns(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)

        assert "delta_swap" in table.column_keys
        assert "delta_ram" in table.column_keys


@pytest.mark.asyncio
async def test_delta_columns_recompute_on_resize(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        assert "delta_ram" in table.column_keys

        await pilot.resize_terminal(80, 24)
        assert "delta_ram" not in table.column_keys

        await pilot.resize_terminal(120, 35)
        assert "delta_ram" in table.column_keys


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
async def test_zswap_footer_item_hidden_below_85_columns(tmp_path: Path) -> None:
    # Run-2 fix A7: below `_ZSWAP_MIN_WIDTH` the ZSWAP column is already
    # hidden by width, so pressing `w` there has no visible effect until the
    # terminal widens back past 85 (SPEC.md "Main view"). Before the fix, the
    # footer item stayed until the *line's own* width forced a drop (well
    # below 85, alongside "cache"), long after the column itself was gone.
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(84, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "w zswap" not in content.plain
        assert "c cache" in content.plain  # the item right after it stays
        assert "x system" in content.plain

        await pilot.press("w")  # still toggles the remembered choice
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        assert screen._show_zswap is False  # pyright: ignore[reportPrivateUsage]

        await pilot.resize_terminal(90, 24)  # back above the width threshold
        await pilot.pause()
        table = _table(pilot)
        assert "zswap" not in table.column_keys  # the remembered `w` choice held


@pytest.mark.asyncio
async def test_zswap_footer_item_shown_at_85_columns(tmp_path: Path) -> None:
    root = _zswap_base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(85, 24)) as pilot:
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)
        assert "w zswap" in content.plain


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
        keys = list(table.row_keys)
        assert len(keys) == 1
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
        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)


@pytest.mark.asyncio
async def test_total_column_fits_after_resizing_from_90_to_60(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, f"app-{'a' * 25}.service", ram=12 * 1024**3, swap=3 * 1024**3)

    async with _app(root).run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        row_key = table.row_keys[0]
        cell = table.get_cell(row_key, "app").plain
        assert "…" not in cell, "already cropped at 90 columns"

        await pilot.resize_terminal(60, 24)
        await pilot.pause()

        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)
        cell = table.get_cell(row_key, "app").plain
        assert "…" in cell, "long name not cropped at 60 columns"


@pytest.mark.asyncio
async def test_app_column_widens_back_after_resizing_from_60_to_90(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, f"app-{'a' * 25}.service", ram=12 * 1024**3, swap=3 * 1024**3)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        table = _table(pilot)
        row_key = table.row_keys[0]
        cell = table.get_cell(row_key, "app").plain
        assert "…" in cell, "long name not cropped at 60 columns"

        await pilot.resize_terminal(90, 24)
        await pilot.pause()

        region = table.column_region("total")
        assert region.right <= table.size.width, (region, table.size.width)
        cell = table.get_cell(row_key, "app").plain
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
    real_read_system = collect.read_system
    started = threading.Event()
    release = threading.Event()

    def blocking_read_system(root: Path, uid: int) -> object:
        started.set()
        release.wait(timeout=2)  # bounded: the worker thread never hangs forever
        return real_read_system(root, uid)

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.05, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(collect, "read_system", blocking_read_system)
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
async def test_ticks_leave_no_workers_or_results_behind(tmp_path: Path) -> None:
    # A finished Textual `Worker` stays alive until a cyclic GC pass (its
    # task's context holds the worker, the worker holds the task), and on
    # Python 3.14 that pass can be thousands of ticks away: tick workers piled
    # up to 1.1 GiB in ten hours with their results, and still 7 MiB every 20
    # minutes without them. So a tick must create no `Worker` at all, and its
    # result must die by refcount right after it's applied, with no GC help.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.02, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.pause(1.0)  # ~50 ticks
        workers = sum(1 for o in gc.get_objects() if isinstance(o, Worker))
        results = [
            o
            for o in gc.get_objects()
            if isinstance(o, main_screen._TickResult)  # pyright: ignore[reportPrivateUsage]
        ]
    assert workers == 0, f"{workers} Textual workers alive"
    assert len(results) <= 2, f"{len(results)} tick results still alive"


@pytest.mark.asyncio
async def test_collector_bug_in_a_tick_exits_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tick's thread can't raise into the app by itself; a bug in the
    # collector must still end the run with the traceback, not leave the
    # table frozen on its last frame with the refreshes silently stopped.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    def broken_tick_worker(*args: object, **kwargs: object) -> object:
        raise RuntimeError("collector bug")

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.02, include_system=False)
    with pytest.raises(RuntimeError, match="collector bug"):
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(main_screen, "_tick_worker", broken_tick_worker)
            await pilot.pause(0.5)
    assert app.return_code == 1


def _self_referencing_strips() -> int:
    # Textual's `Strip.divide` stores `[self]` in the strip's own divide
    # cache: a reference cycle only the cyclic GC can free.
    return sum(
        1
        for o in gc.get_objects()
        if isinstance(o, Strip)
        and any(
            piece is o
            for pieces in o._divide_cache._cache.values()  # pyright: ignore[reportPrivateUsage]
            for piece in pieces
        )
    )


@pytest.mark.asyncio
async def test_changing_rows_leave_no_strip_cycles_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every table line rendered after a change leaves one self-referencing
    # `Strip` (about 6 KB with its caches) that only the cyclic GC frees, and
    # CPython 3.14's collector let thousands of them pile up: the live app
    # grew about 1 MiB a minute whenever rows were changing. The app collects
    # on its own timer; here that timer is fast and every tick changes every
    # row's numbers, so only the last frame's strips may remain.
    monkeypatch.setattr(ui_app, "GC_INTERVAL", 0.05)
    gc.collect()  # earlier tests' apps leave their own strips behind
    root = _base_tree(tmp_path)
    names = [f"app-x{i}.service" for i in range(12)]
    for i, name in enumerate(names):
        _app_unit(root, name, ram=(i + 1) * 1024**2, swap=0)
    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.02, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        # Relative to the first frame: apps of earlier tests can still be
        # alive (a kept traceback is enough) with their own last frame.
        before = _self_referencing_strips()
        for step in range(60):
            for i, name in enumerate(names):
                unit_dir = user_service_root(root, UID) / "app.slice" / name
                write_memory_stat(unit_dir, anon=(step * 7 + i * 3 + 1) * 1024**2)
            await pilot.pause(0.02)
        added = _self_referencing_strips() - before
        deadline = time.monotonic() + 3
        while added > 60 and time.monotonic() < deadline:
            await pilot.pause(0.05)  # the app's own timer collects; give it a chance
            added = _self_referencing_strips() - before
    assert added <= 60, f"{added} self-referencing strips added by 60 refreshes"


@pytest.mark.asyncio
async def test_at_most_one_tick_read_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    real_collect_apps = LinuxBackend.collect_apps
    lock = threading.Lock()
    counts = {"current": 0, "max": 0}

    def slow_collect_apps(
        backend: LinuxBackend,
        *,
        include_system: bool,
        strict: bool,
        count_procs: bool,
        previous_procs: dict[str, int],
    ) -> tuple[list[AppStats], dict[str, int]]:
        with lock:
            counts["current"] += 1
            counts["max"] = max(counts["max"], counts["current"])
        time.sleep(0.3)
        try:
            return real_collect_apps(
                backend,
                include_system=include_system,
                strict=strict,
                count_procs=count_procs,
                previous_procs=previous_procs,
            )
        finally:
            with lock:
                counts["current"] -= 1

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.1, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(LinuxBackend, "collect_apps", slow_collect_apps)
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
    real_collect_apps = LinuxBackend.collect_apps

    def slow_collect_apps(
        backend: LinuxBackend,
        *,
        include_system: bool,
        strict: bool,
        count_procs: bool,
        previous_procs: dict[str, int],
    ) -> tuple[list[AppStats], dict[str, int]]:
        if not include_system:
            time.sleep(0.4)  # the stale (pre-toggle) read: lands after the toggle below
        return real_collect_apps(
            backend,
            include_system=include_system,
            strict=strict,
            count_procs=count_procs,
            previous_procs=previous_procs,
        )

    app = AppMemApp(backend=LinuxBackend(root, UID), interval=0.1, include_system=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(LinuxBackend, "collect_apps", slow_collect_apps)
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
        backend=LinuxBackend(root, UID),
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
        backend=LinuxBackend(root, UID),
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
        backend=LinuxBackend(tmp_path, UID),
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
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="dracula",
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


def _panel(pilot: Pilot[None]) -> ThemePanel:
    screen = pilot.app.screen
    assert isinstance(screen, ThemePanel)
    return screen


def _theme_list(pilot: Pilot[None]) -> OptionList:
    return _panel(pilot).query_one("#theme-list", OptionList)


def _highlighted_theme_name(option_list: OptionList) -> str:
    assert option_list.highlighted is not None
    return THEME_NAMES[option_list.highlighted]


async def _highlight(pilot: Pilot[None], theme_name: str) -> None:
    """Move the panel's cursor straight to `theme_name` (like
    `table.move_cursor(row=...)` elsewhere in this file) instead of
    counting arrow presses -- the arrow/PgUp/PgDn/Home/End navigation
    itself is `OptionList`'s own, not appmem's, so a handful of plain
    `down` presses (below) already covers that appmem wires it up."""
    _theme_list(pilot).highlighted = THEME_NAMES.index(theme_name)
    await pilot.pause()


def _marks(pilot: Pilot[None]) -> list[str | None]:
    options = _theme_list(pilot).options
    return [option.id for option in options if str(option.prompt).startswith("✓")]


@pytest.mark.asyncio
async def test_t_opens_the_panel_on_the_current_theme_marked(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="dracula",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()

        option_list = _theme_list(pilot)
        # Cursor opens on the current theme, marked -- exactly one mark.
        assert _highlighted_theme_name(option_list) == "dracula"
        assert _marks(pilot) == ["dracula"]


@pytest.mark.asyncio
async def test_ctrl_p_theme_opens_the_same_panel(tmp_path: Path) -> None:
    # `T` and Ctrl+P -> Theme both call `App.search_themes` (SPEC.md
    # "Command line"): this covers the second entry point.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="nord",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"Theme")
        await pilot.pause()
        await pilot.press("enter")  # select the "Theme" system command
        await pilot.pause()

        option_list = _theme_list(pilot)
        assert _highlighted_theme_name(option_list) == "nord"
        assert _marks(pilot) == ["nord"]


@pytest.mark.asyncio
async def test_ctrl_p_theme_while_the_panel_is_open_adds_no_second_panel(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = AppMemApp(
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="nord",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()
        await _highlight(pilot, "dracula")

        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"Theme")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert sum(isinstance(s, ThemePanel) for s in pilot.app.screen_stack) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.theme == "nord"  # the one panel still restores its opening theme
        assert not any(isinstance(s, ThemePanel) for s in pilot.app.screen_stack)


@pytest.mark.asyncio
async def test_arrow_preview_changes_theme_and_header_colours_and_writes_nothing(
    tmp_path: Path,
) -> None:
    # Swap 92 % used, above the header's 90 % threshold, so the Swap line's
    # pair carries the theme's error colour.
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
        backend=LinuxBackend(tmp_path, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="dracula",
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme
        main_screen = pilot.app.screen_stack[0]
        assert isinstance(main_screen, MainScreen)
        swap_total = 32_000_000 * 1024
        swap_free = 2_560_000 * 1024
        pair = format_pair(swap_total - swap_free, swap_total)
        before_error = Color.parse(BUILTIN_THEMES[before].error or "").rich_color.name

        await pilot.press("T")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()

        after = pilot.app.theme
        assert after != before  # a highlight change previews at once
        assert not config_path().exists()

        header = main_screen.query_one("#header2", Static).content
        assert isinstance(header, Text)
        after_error = Color.parse(BUILTIN_THEMES[after].error or "").rich_color.name
        assert after_error != before_error
        assert after_error in _style_at(header, pair)  # MainScreen's own colours follow too


@pytest.mark.asyncio
async def test_esc_restores_the_original_theme_and_writes_nothing(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        await _highlight(pilot, "dracula")  # previewed, never confirmed
        assert pilot.app.theme == "dracula"

        await pilot.press("escape")
        await pilot.pause()

        assert pilot.app.theme == before
        assert not config_path().exists()
        assert pilot.app.screen_stack == [pilot.app.screen]  # panel actually closed


@pytest.mark.asyncio
async def test_t_again_while_open_also_cancels(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        await _highlight(pilot, "dracula")

        await pilot.press("T")  # `T` while open: same as Esc, not a second panel
        await pilot.pause()

        assert pilot.app.theme == before
        assert not config_path().exists()
        assert pilot.app.screen_stack == [pilot.app.screen]


@pytest.mark.asyncio
async def test_click_outside_the_panel_cancels(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = pilot.app.theme

        await pilot.press("T")
        await pilot.pause()
        await _highlight(pilot, "dracula")

        # The panel is docked right (see the geometry tests below); the
        # top-left corner is always outside it.
        await pilot.click(offset=(0, 0))
        await pilot.pause()

        assert pilot.app.theme == before
        assert not config_path().exists()
        assert pilot.app.screen_stack == [pilot.app.screen]


@pytest.mark.asyncio
async def test_click_inside_the_panel_but_off_the_list_keeps_it_open(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()
        await _highlight(pilot, "dracula")

        await pilot.click("#panel-title")
        await pilot.pause()

        assert isinstance(pilot.app.screen, ThemePanel)
        assert pilot.app.theme == "dracula"  # the preview stays
        assert not config_path().exists()


@pytest.mark.asyncio
async def test_enter_writes_the_highlighted_theme(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert pilot.app.theme != "dracula"  # sanity: not already the target
        assert not config_path().exists()

        await pilot.press("T")
        await pilot.pause()
        await _highlight(pilot, "dracula")
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.theme == "dracula"
        assert config_path().read_text() == 'theme = "dracula"\n'
        assert pilot.app.screen_stack == [pilot.app.screen]


@pytest.mark.asyncio
async def test_click_on_an_option_confirms_it(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()
        option_list = _theme_list(pilot)
        index = THEME_NAMES.index("dracula")
        # `OptionList` has no public "region of option N", but every option
        # is one line with no border above it, so option N sits N lines
        # below the top of the scrolled content.
        assert option_list.virtual_size.height == len(THEME_NAMES)
        line = index - option_list.scroll_offset.y
        await pilot.click(option_list, offset=(1, line))
        await pilot.pause()

        assert pilot.app.theme == "dracula"
        assert config_path().read_text() == 'theme = "dracula"\n'
        assert pilot.app.screen_stack == [pilot.app.screen]


@pytest.mark.asyncio
async def test_enter_on_the_files_own_value_writes_nothing(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    config_path().parent.mkdir(parents=True)
    config_path().write_text('theme = "gruvbox"\n')

    app = AppMemApp(
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="gruvbox",
        config_theme="gruvbox",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        mtime_before = config_path().stat().st_mtime_ns

        await pilot.press("T")
        await pilot.pause()
        assert _highlighted_theme_name(_theme_list(pilot)) == "gruvbox"  # already highlighted
        await pilot.press("enter")  # confirming the theme it started with
        await pilot.pause()

        assert pilot.app.theme == "gruvbox"
        assert config_path().read_text() == 'theme = "gruvbox"\n'
        assert config_path().stat().st_mtime_ns == mtime_before  # not rewritten


@pytest.mark.asyncio
async def test_enter_on_the_startup_theme_that_differs_from_the_file_writes_it(
    tmp_path: Path,
) -> None:
    # This is the behaviour change from Textual's own picker (SPEC.md
    # "Command line"): confirming the theme started from `--theme` (never
    # written by itself) now saves it too, because Enter is an explicit
    # choice -- not just "a pick that changes the running theme".
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    config_path().parent.mkdir(parents=True)
    config_path().write_text('theme = "gruvbox"\n')

    app = AppMemApp(
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme="dracula",  # e.g. from --theme; differs from the file
        config_theme="gruvbox",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()
        assert _highlighted_theme_name(_theme_list(pilot)) == "dracula"
        await pilot.press("enter")  # confirming the startup value, unchanged
        await pilot.pause()

        assert pilot.app.theme == "dracula"
        assert config_path().read_text() == 'theme = "dracula"\n'


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
            await _highlight(pilot, "dracula")
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


@pytest.mark.asyncio
async def test_quit_while_previewing_writes_nothing(tmp_path: Path) -> None:
    # A5 (run-2 fixes): `q` used to do nothing while the panel was open --
    # `ThemePanel` is a `ModalScreen`, which blocks `AppMemApp`'s own `q`
    # binding unless it's `priority=True` (only `ctrl+c` was) -- right next
    # to the main footer's own visible `q quit` (SPEC.md "Theme panel":
    # quitting while the panel is open saves nothing, same as Esc).
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    app = _app(root)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ThemePanel)
        await pilot.press("down")
        await pilot.pause()

        await pilot.press("q")
        await pilot.pause()

        assert app.return_code == 0
        assert not config_path().exists()


# --- panel geometry (SPEC.md "Main view": leaves the rest of the app visible) ----


@pytest.mark.asyncio
async def test_panel_docks_right_and_leaves_most_of_the_width_free(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()

        panel = _panel(pilot).query_one("#panel")
        assert panel.region.width <= 32
        assert panel.region.x + panel.region.width == 120  # flush with the right edge
        assert panel.region.height == 30  # full height

        # Not full-screen dimming: MainScreen's own content is still there,
        # right behind the panel.
        main_screen = pilot.app.screen_stack[0]
        assert isinstance(main_screen, MainScreen)
        footer = main_screen.query_one("#footer", Static).content
        assert isinstance(footer, Text)
        assert footer.plain
        # ...and shown as is: a `ModalScreen` dims everything under it
        # unless its own background is fully transparent.
        assert _panel(pilot).styles.background.a == 0


@pytest.mark.asyncio
async def test_panel_fits_a_narrow_screen(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(40, 10)) as pilot:
        await pilot.pause()

        await pilot.press("T")
        await pilot.pause()

        panel = _panel(pilot).query_one("#panel")
        assert panel.region.x >= 0
        assert panel.region.width <= 40
        assert panel.region.right <= 40

        # Too short for the whole list, so it scrolls, and its scrollbar
        # must not squeeze the longest name onto a second line.
        option_list = _theme_list(pilot)
        assert option_list.max_scroll_y > 0
        assert option_list.virtual_size.height == len(THEME_NAMES)


# --- terminal-dark/terminal-light, info line, hint lines ------------------------


@pytest.mark.asyncio
async def test_panel_shows_terminal_names_not_the_ansi_aliases(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()

        option_ids = [option.id for option in _theme_list(pilot).options]
        assert "terminal-dark" in option_ids
        assert "terminal-light" in option_ids
        assert "ansi-dark" not in option_ids
        assert "ansi-light" not in option_ids


@pytest.mark.asyncio
async def test_ansi_dark_still_opens_the_panel_on_terminal_dark(tmp_path: Path) -> None:
    # A config file (or --theme/APPMEM_THEME) still holding the old
    # `ansi-dark` name (SPEC.md "Command line") must open the panel on the
    # renamed row, not fail to find a match for the running theme.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)
    config_path().parent.mkdir(parents=True)
    config_path().write_text('theme = "ansi-dark"\n')
    resolution = resolve_theme(cli_theme=None, env_theme=None, config_path=config_path())

    app = AppMemApp(
        backend=LinuxBackend(root, UID),
        interval=NO_AUTO_REFRESH_INTERVAL,
        include_system=False,
        theme=resolution.effective,
        config_theme=resolution.config_theme,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert pilot.app.theme == "terminal-dark"
        await pilot.press("T")
        await pilot.pause()

        assert _highlighted_theme_name(_theme_list(pilot)) == "terminal-dark"
        assert _marks(pilot) == ["terminal-dark"]
        # The info line reflects the opening theme too, not only a later move.
        info = _panel(pilot).query_one("#panel-info", Static)
        assert info.content == "your terminal's colours"

        # Keeping the same theme is not a change: the old name stays on disk.
        await pilot.press("enter")
        await pilot.pause()
        assert config_path().read_text() == 'theme = "ansi-dark"\n'


@pytest.mark.asyncio
async def test_info_line_shows_only_while_a_terminal_theme_is_highlighted(
    tmp_path: Path,
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()

        info = _panel(pilot).query_one("#panel-info", Static)
        panel_height_before = _panel(pilot).query_one("#panel").region.height

        await _highlight(pilot, "dracula")
        assert info.content == ""  # blank for a built-in theme

        await _highlight(pilot, "terminal-dark")
        assert info.content == "your terminal's colours"

        await _highlight(pilot, "terminal-light")
        assert info.content == "your terminal's colours"

        await _highlight(pilot, "nord")
        assert info.content == ""  # back to blank, moving off a terminal theme

        # Fixed height throughout: the hint lines below never move.
        assert _panel(pilot).query_one("#panel").region.height == panel_height_before


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 30), (40, 10)])
async def test_hint_lines_are_fully_visible_and_not_cropped(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("T")
        await pilot.pause()

        panel = _panel(pilot)
        move = panel.query_one("#panel-hint-move", Static)
        keys = panel.query_one("#panel-hint-keys", Static)

        assert move.content == "↑↓ preview"
        assert keys.content == "enter keep  esc cancel"
        # `0 1` padding either side (theme_picker.py's `.panel-line` CSS):
        # the text itself must fit inside what's left of the region.
        assert cell_len(str(move.content)) <= move.region.width - 2
        assert cell_len(str(keys.content)) <= keys.region.width - 2


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


@pytest.mark.parametrize(
    "theme", [BUILTIN_THEMES["ansi-dark"], *TERMINAL_THEMES], ids=lambda theme: theme.name
)
def test_bar_fill_colour_skips_contrast_math_for_ansi_themes(theme: Theme) -> None:
    # The terminal themes are the ansi ones renamed; they must still take
    # this branch through `theme.ansi`, not through their name.
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
