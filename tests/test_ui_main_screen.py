"""Textual pilot tests for `AppMemApp`/`MainScreen` (SPEC.md "Main view", "Tests").

No live `/sys` or `/proc`: `AppMemApp` takes the same fixture `root`/`uid` as
`collect` (AGENTS.md). The interval is set high enough that the automatic
timer never fires during a test; refreshes are triggered deterministically by
calling `MainScreen.refresh_now()` after mutating the fixture on disk.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey

from appmem.ui.app import AppMemApp
from appmem.ui.rows import row_key
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

        await pilot.press("z")
        delta_after = table.get_cell(row_key("alpha", "user"), "delta_ram")
        assert isinstance(delta_after, Text)
        assert delta_after.plain == "·"


@pytest.mark.asyncio
async def test_z_takes_a_fresh_sample_not_the_previous_ticks_data(tmp_path: Path) -> None:
    # `test_z_resets_delta_to_zero` above passes even if `z` reuses
    # `self._last_apps` from the previous tick, because nothing changes on
    # disk between `z` and the assertion. Here the fixture keeps moving after
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
        await pilot.press("z")  # must sample fresh (9 MiB), not reuse the 5 MiB tick
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
async def test_footer_drops_lowest_priority_items_at_60_columns(tmp_path: Path) -> None:
    # The footer never wraps. Below its natural
    # width (72 cols) it drops items lowest priority first, keeping help and
    # quit no matter how narrow.
    root = _base_tree(tmp_path)
    _app_unit(root, "app-alpha.service", ram=1 * 1024**2, swap=0)

    async with _app(root).run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, MainScreen)
        footer = screen.query_one("#footer", Static)
        content = footer.content
        assert isinstance(content, Text)

        assert content.no_wrap is True
        assert content.cell_len <= 60
        assert "? help" in content.plain
        assert "q quit" in content.plain
        assert "reset Δ" not in content.plain  # lowest priority: dropped first
        assert "cache" not in content.plain
