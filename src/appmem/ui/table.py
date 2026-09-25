"""`CellTable`: a `DataTable` whose `update_cell` invalidates only the changed
cell (SPEC.md "Tech").

py-spy on the live main view (2026-09-25, 200x50, 43 apps, 1 s interval)
showed 52 % of appmem's CPU in `DataTable._render_cell`. Textual's
`DataTable.update_cell` (textual 8.2.8, `widgets/_data_table.py:871-913`)
bumps `_update_count` on every call, and that counter is part of the key
of `_cell_render_cache`, `_row_render_cache` and (via `_get_row_renderables`)
`_row_renderable_cache`, so one changed cell invalidates every cached render
in the table -- about 390 cells per tick on that view, even though appmem
already calls `update_cell` only for cells whose text actually changed
(`screens/main.py`, `screens/processes.py`). `CellTable.update_cell` evicts
only the entries that belong to the changed row/column instead, and leaves
`_update_count` (and therefore `_offset_cache`, which only depends on row
heights and never changes on a cell update) untouched. Measured ceiling with
a scratch monkeypatch of this same idea: 7.8 % -> 4.4 % of one core.

`_row_renderable_cache` is keyed by `(update_count, row_index)`, not by row
or column key, and Textual's own `update_cell` relies on the `update_count`
bump to invalidate it -- it never clears this cache directly. Since
`CellTable` doesn't bump `update_count`, it must evict this row's entry
itself, or a row that was already on screen keeps rendering its pre-update
text forever (verified with a scratch repro: skipping this step renders the
old value after the exact eviction steps described for
`_cell_render_cache`/`_row_render_cache`/`_line_cache` alone).

If Textual's internals move -- an attribute renamed or removed, or
`LRUCache` loses `keys`/`discard` -- `_INTERNALS_OK` is `False` and
`update_cell` falls back to `DataTable.update_cell` unmodified: correct,
just back to the old cost. The same fallback covers `update_width=True`
(a width change moves the whole column, not just this cell) and a Textual
build outside the one minor this class is tested against (an attribute can
keep its name while a cache key's field order changes shape underneath it,
which the attribute probe alone can't see).

Review round 2 (a live-desktop measurement of an instrumented build):
`_update_count` still bumps on structural operations (`add_row`, `remove_row`,
`sort`, `clear`, resize) even though `CellTable.update_cell` never bumps it
itself, so every such bump strands a screen's worth of `_cell_render_cache`/
`_row_render_cache` entries under the old generation -- dead weight that only
the 10 000-entry LRU cap would otherwise clear, growing the per-update scan
as the session goes on (measured: ~9 ms/s of scanning after the cache filled
up in a churny 209-process view). `_invalidate_cell` now discards
stale-generation entries in the same pass it evicts the changed cell/row.
"""

from __future__ import annotations

from typing import Any

import textual
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist, CellType, ColumnKey, RowKey

_REQUIRED_ATTRS = (
    "_data",
    "_cell_render_cache",
    "_row_render_cache",
    "_row_renderable_cache",
    "_line_cache",
)

# The one minor this class is tested against (pyproject.toml pins `textual>=8.2.8`
# with no upper bound, so `uv tool`/`pipx` installs can still land a newer one that
# the suite never ran). A version outside this prefix can keep every attribute name
# in `_REQUIRED_ATTRS` while still reshuffling a cache key's field order or adding a
# new cache keyed by `_update_count` -- shape changes the attribute probe below
# can't see, but that would make the fast path invalidate the wrong entries (or
# none) instead of raising. Cheaper and less brittle than an upper-bound dependency
# cap: installs stay unblocked, they just take the slower, always-correct path.
_TESTED_TEXTUAL_MINOR = "8.2."


def _check_internals() -> bool:
    """Probe a throwaway `DataTable` for the private attributes `CellTable.update_cell`
    relies on, plus the `LRUCache` methods it calls on them, and confirm this Textual
    build is the one minor this class is tested against. Runs once at import time;
    `False` sends every `CellTable` instance down the `DataTable.update_cell` fallback.
    """
    if not textual.__version__.startswith(_TESTED_TEXTUAL_MINOR):
        return False
    probe: DataTable[Any] = DataTable()
    if not all(hasattr(probe, attr) for attr in _REQUIRED_ATTRS):
        return False
    cache = getattr(probe, "_cell_render_cache", None)
    return hasattr(cache, "keys") and hasattr(cache, "discard")


_INTERNALS_OK = _check_internals()


class CellTable(DataTable[CellType]):
    """A `DataTable` that invalidates only the updated cell on `update_cell`
    instead of Textual's whole render cache. See the module docstring for why
    and the numbers behind it. Falls back to `DataTable.update_cell` when the
    Textual internals it relies on have changed shape (`_INTERNALS_OK`) and for
    `update_width=True`, which can move the whole column.
    """

    def update_cell(
        self,
        row_key: RowKey | str,
        column_key: ColumnKey | str,
        value: CellType,
        *,
        update_width: bool = False,
    ) -> None:
        # A width change moves the column in every row and the header, and the
        # cell cache key has no width, so it needs Textual's full invalidation.
        if not _INTERNALS_OK or update_width:
            super().update_cell(row_key, column_key, value, update_width=update_width)
            return

        if isinstance(row_key, str):
            row_key = RowKey(row_key)
        if isinstance(column_key, str):
            column_key = ColumnKey(column_key)

        if row_key not in self._row_locations or column_key not in self._column_locations:
            raise CellDoesNotExist(
                f"No cell exists for row_key={row_key!r}, column_key={column_key!r}."
            )

        self._data[row_key][column_key] = value
        self._invalidate_cell(row_key, column_key)
        self.refresh()

    def _invalidate_cell(self, row_key: RowKey, column_key: ColumnKey) -> None:
        """Evict this cell's and this row's cached renders. Leaves the *current*
        `_update_count` untouched, so `_offset_cache` (row heights) stays valid
        without being cleared.

        Also discards, in the same pass, every entry left over from an earlier
        generation: `_update_count` still bumps on structural operations
        (`add_row`, `remove_row`, `sort`, `clear`, resize), and this fast path
        never re-reads an old generation's entries, so they'd otherwise only
        leave via the 10 000-entry LRU cap -- a screen's worth of dead entries
        per bump, growing the scan below until the cache fills up. A key's
        generation is `key[-2]` in both caches (the field right before
        `_pseudo_class_state`; `_data_table.py`'s `CellCacheKey`/`RowCacheKey`).
        Discarding an extra entry is always safe -- a miss just re-renders --
        so this can only cost speed, never correctness, if that index is ever
        wrong.
        """
        generation = self._update_count
        for key in list(self._cell_render_cache.keys()):
            if (key[0] == row_key and key[1] == column_key) or key[-2] != generation:
                self._cell_render_cache.discard(key)
        for key in list(self._row_render_cache.keys()):
            if key[0] == row_key or key[-2] != generation:
                self._row_render_cache.discard(key)
        row_index = self._row_locations.get(row_key)
        if row_index is not None:
            self._row_renderable_cache.discard((self._update_count, row_index))
        self._line_cache.clear()
