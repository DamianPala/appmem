"""Tests for `CellTable` (SPEC.md "Tech", `appmem.ui.table`).

A minimal `App` hosting a bare `CellTable`/`DataTable` with fixture rows --
no live `/sys` or `/proc`, no `AppMemApp` needed.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist

from appmem.ui import table as table_module
from appmem.ui.table import CellTable

_COLUMN_NAMES = ("a", "b", "c")
_ROW_COUNT = 40


class _TableApp(App[None]):
    """Hosts one table, `table_cls` unless a test needs the plain `DataTable`
    for comparison."""

    def __init__(self, table_cls: type[DataTable[str]] = CellTable) -> None:
        super().__init__()
        self._table_cls = table_cls

    def compose(self) -> ComposeResult:
        table: DataTable[str] = self._table_cls(id="table", cursor_type="row")
        table.add_columns(*_COLUMN_NAMES)
        for i in range(_ROW_COUNT):
            table.add_row(*[f"{name}{i}" for name in _COLUMN_NAMES], key=f"r{i}")
        yield table


def _table(app: App[None]) -> DataTable[str]:
    # `isinstance()` (which `query_one` uses) rejects a parameterized generic,
    # so we query by the bare class and `cast` to the concrete cell type.
    return cast("DataTable[str]", app.query_one("#table", DataTable))


def _render_all(table: DataTable[Any]) -> None:
    for y in range(table.size.height):
        table.render_line(y)


def _line_text(table: DataTable[Any], y: int) -> str:
    return "".join(segment.text for segment in table.render_line(y))


@pytest.mark.asyncio
async def test_update_cell_rerenders_at_most_one_call_per_column() -> None:
    """A changed cell should cost Textual's `Console.render_lines` at most once
    per column of that row, not once per visible cell (the brief's "waste is
    all in Textual" claim -- see `_render_count` below for the plain
    `DataTable` comparison, which is much higher, kept out of this suite so
    CI stays green)."""
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 12)) as pilot:
        table = _table(app)
        await pilot.pause()
        # Warm twice: right after mount, cache keys carry a pre-focus
        # `_pseudo_class_state` that changes once the table gains focus, so a
        # single render pass here would still miss on every cell during the
        # "real" pass below, inflating the count for the wrong reason.
        _render_all(table)
        _render_all(table)

        row_key = "r2"
        column_key = table.ordered_columns[1].key
        with patch.object(app.console, "render_lines", wraps=app.console.render_lines) as spy:
            table.update_cell(row_key, column_key, "ZZ")
            _render_all(table)

        assert 1 <= spy.call_count <= len(_COLUMN_NAMES)


@pytest.mark.asyncio
async def test_update_cell_purges_stale_generation_entries() -> None:
    """Structural ops (`add_row`, `remove_row`, `sort`, `clear`, resize) still
    bump `_update_count` even though the fast path never does, so a bump
    strands a screen's worth of `_cell_render_cache`/`_row_render_cache`
    entries under the old generation -- otherwise only cleared by the
    10 000-entry LRU cap, which grows the eviction scan over a session
    (review round 2). One `update_cell` after a bump must purge every
    stale-generation entry from both caches, not just the touched cell/row."""
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 12)) as pilot:
        table = _table(app)
        await pilot.pause()
        _render_all(table)
        _render_all(table)  # settle, then warm every visible cell/row

        # A structural op's bump, without a `_clear_caches()` call of its own
        # (matches what `add_row`/`remove_row` do to `_update_count`).
        table._update_count += 1  # pyright: ignore[reportPrivateUsage]
        _render_all(table)  # re-warm at the new generation; the old one is now stale

        generation = table._update_count  # pyright: ignore[reportPrivateUsage]
        column_key = table.ordered_columns[1].key
        table.update_cell("r2", column_key, "ZZ")

        cell_cache_keys = list(table._cell_render_cache.keys())  # pyright: ignore[reportPrivateUsage]
        row_cache_keys = list(table._row_render_cache.keys())  # pyright: ignore[reportPrivateUsage]
        stale_cell = [key for key in cell_cache_keys if key[-2] != generation]
        stale_row = [key for key in row_cache_keys if key[-2] != generation]
        assert stale_cell == []
        assert stale_row == []


@pytest.mark.asyncio
async def test_update_cell_renders_new_text_above_the_fold_after_scrolling() -> None:
    """A row that was already on screen (so its `_row_renderable_cache` entry
    is warm) must not keep rendering its pre-update text once scrolled past --
    the bug this slice's `_row_renderable_cache` eviction fixes (module
    docstring)."""
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        _render_all(table)  # rows near the top are now warm

        # Short marker: the column's auto-width was set from "b0".."b39" (<= 3
        # chars), so a longer value would be silently truncated on render.
        column_key = table.ordered_columns[1].key
        table.update_cell("r3", column_key, "ZZ")
        table.scroll_to(y=20, animate=False)
        await pilot.pause()
        _render_all(table)  # scroll the updated row back off screen and warm others

        table.scroll_to(y=0, animate=False)
        await pilot.pause()
        text = _line_text(table, 4)  # header (y=0) + row r3 at y=4
        assert "ZZ" in text
        assert "b3" not in text


@pytest.mark.asyncio
async def test_update_cell_renders_new_text_below_the_fold_after_scrolling() -> None:
    """Same guarantee for a row that was never rendered before the update (a
    cold `_row_renderable_cache`/`_cell_render_cache`), then scrolled into
    view."""
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        _render_all(table)  # only rows 0-8 or so are warm

        column_key = table.ordered_columns[1].key
        table.update_cell("r35", column_key, "QQ")
        table.scroll_to(y=30, animate=False)
        await pilot.pause()
        _render_all(table)
        text = _line_text(table, 6)  # header (y=0) + row r35 at y=6 (scroll_y=30)
        assert "QQ" in text
        assert "b35" not in text


@pytest.mark.asyncio
async def test_cursor_row_highlight_survives_an_update_on_the_cursor_row() -> None:
    """Compare the rendered cursor-row line against a plain `DataTable` given
    the exact same data, cursor position and update -- the styling comes from
    `_get_styles_to_render_cell` on every recompute, not from what's cached,
    so it must be identical."""
    cell_lines: dict[type[DataTable[str]], list[tuple[str, object]]] = {}
    for table_cls in (CellTable, DataTable):
        app = _TableApp(table_cls)
        async with app.run_test(size=(40, 10)) as pilot:
            table = _table(app)
            await pilot.pause()
            _render_all(table)
            table.move_cursor(row=2)
            await pilot.pause()
            column_key = table.ordered_columns[1].key
            table.update_cell("r2", column_key, "CURSORVAL")
            await pilot.pause()
            strip = table.render_line(3)  # header (y=0) + cursor row r2 at y=3
            cell_lines[table_cls] = [(seg.text, seg.style) for seg in strip]

    assert cell_lines[CellTable] == cell_lines[DataTable]


@pytest.mark.asyncio
async def test_update_width_true_widens_the_column() -> None:
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        column = table.ordered_columns[1]
        width_before = column.content_width

        table.update_cell("r0", column.key, "a-much-longer-cell-value", update_width=True)
        assert table._require_update_dimensions is True  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()

        assert column.content_width > width_before
        assert column.content_width >= len("a-much-longer-cell-value")


@pytest.mark.asyncio
async def test_update_width_true_relayouts_every_line_like_plain_datatable() -> None:
    """A widened column moves that column in every row and the header, so every
    warm line must be re-rendered, not only the updated row's."""
    rendered: dict[type[DataTable[str]], list[str]] = {}
    for table_cls in (CellTable, DataTable):
        app = _TableApp(table_cls)
        async with app.run_test(size=(60, 10)) as pilot:
            table = _table(app)
            await pilot.pause()
            _render_all(table)
            column_key = table.ordered_columns[1].key
            table.update_cell("r2", column_key, "a-much-longer-cell-value", update_width=True)
            await pilot.pause()
            rendered[table_cls] = [_line_text(table, y) for y in range(table.size.height)]

    assert rendered[CellTable] == rendered[DataTable]


@pytest.mark.asyncio
async def test_update_cell_falls_back_to_datatable_when_internals_guard_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `_INTERNALS_OK` forced `False`, `update_cell` must behave exactly
    like `DataTable.update_cell` (including bumping `_update_count`, which the
    fast path deliberately never does)."""
    monkeypatch.setattr(table_module, "_INTERNALS_OK", False)
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        column_key = table.ordered_columns[1].key
        count_before = table._update_count  # pyright: ignore[reportPrivateUsage]

        table.update_cell("r0", column_key, "WW")
        await pilot.pause()

        assert table._update_count == count_before + 1  # pyright: ignore[reportPrivateUsage]
        text = _line_text(table, 1)
        assert "WW" in text
        assert "b0" not in text


@pytest.mark.asyncio
async def test_foreign_textual_version_sends_update_cell_down_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Textual build outside `_TESTED_TEXTUAL_MINOR` can keep every attribute
    name in `_REQUIRED_ATTRS` while reshuffling a cache key's field order --
    a shape change the attribute probe alone can't see (review round 2), so
    the guard checks the version string too. Patches the version and lets the
    real `_check_internals()` recompute `_INTERNALS_OK`, then confirms
    `update_cell` takes the fallback (the `_update_count` bump)."""
    monkeypatch.setattr(table_module.textual, "__version__", "9.9.9")
    monkeypatch.setattr(
        table_module,
        "_INTERNALS_OK",
        table_module._check_internals(),  # pyright: ignore[reportPrivateUsage]
    )

    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        column_key = table.ordered_columns[1].key
        count_before = table._update_count  # pyright: ignore[reportPrivateUsage]

        table.update_cell("r0", column_key, "VV")
        await pilot.pause()

        assert table._update_count == count_before + 1  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_update_cell_on_unknown_key_raises_cell_does_not_exist() -> None:
    app = _TableApp(CellTable)
    async with app.run_test(size=(40, 10)) as pilot:
        table = _table(app)
        await pilot.pause()
        column_key = table.ordered_columns[0].key
        with pytest.raises(CellDoesNotExist):
            table.update_cell("no-such-row", column_key, "x")
