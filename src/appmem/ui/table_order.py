"""Shared row-reorder helper for `DataTable` (SPEC.md "Tech" sort notes).

`DataTable.sort(key=...)`'s key function only ever sees a row's *cell values*,
never its `RowKey` (see Textual's `DataTable.sort`), so a sort built by ranking
rows via their *rendered* first-column text can silently collide: two
different app/command names truncated to the same prefix, or a synthetic row
whose display text happens to equal a real command's name (SPEC.md "Tech";
final review F12/A6).

`reorder_rows` instead ranks each row by its real `RowKey` string -- the same
identity already used to add/update/remove that row -- and hands `sort` a key
that maps a first-column cell back to its row by object identity. Every cell
is its own freshly built `Text`, and `DataTable` stores and passes the exact
objects it was given, so this uses public API only and never compares text.
"""

from __future__ import annotations

from typing import Any

from textual.widgets import DataTable


def reorder_rows(table: DataTable[Any], ordered_keys: list[str]) -> None:
    """Put `table`'s rows in `ordered_keys` order (every existing row key,
    each exactly once). A no-op when the table is already in that order, to
    avoid forcing a repaint.
    """
    current = [row.key.value for row in table.ordered_rows]
    if current == ordered_keys:
        return
    first_column = table.ordered_columns[0].key
    rank = {id(table.get_cell(key, first_column)): index for index, key in enumerate(ordered_keys)}
    if len(rank) != len(ordered_keys):
        raise ValueError("reorder_rows needs a distinct first-column cell object per row")
    table.sort(first_column, key=lambda cell: rank[id(cell)])
