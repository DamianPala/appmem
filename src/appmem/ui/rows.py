"""Main-view row model, Δ baseline bookkeeping and sorting (SPEC.md "Main view").

Pure functions with no Textual imports, so the Δ math and tie-break ordering are
unit-testable without a running app (SPEC.md "Tests").
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from appmem.collect import AppStats

SortKey = str
"""One of: ``app``, ``swap``, ``ram``, ``cache``, ``total``, ``delta_swap``,
``delta_ram``, ``procs``. A plain string (not an enum) because it doubles as a
`DataTable` column key."""

DEFAULT_SORT_KEY: SortKey = "total"
DEFAULT_SORT_REVERSE = True

# Keys `s`/`r`/`t`/`d` (SPEC.md "Keys"); other columns sort by header click only.
KEY_SORT_COLUMNS: dict[str, SortKey] = {
    "s": "swap",
    "r": "ram",
    "t": "total",
    "d": "delta_swap",
}


@dataclass(frozen=True)
class Row:
    """One main-view table row: an app's current counters plus its Δ."""

    name: str
    swap: int
    ram: int
    cache: int
    total: int
    delta_swap: int
    delta_ram: int
    procs: int


def update_baseline(
    apps: Iterable[AppStats], baseline: Mapping[str, AppStats]
) -> dict[str, AppStats]:
    """Add apps seen for the first time to the Δ baseline (SPEC.md "Definitions").

    Existing entries are kept untouched: only `reset_baseline` (key `z`, or
    startup) moves an already-known app's baseline forward.
    """
    updated = dict(baseline)
    for app in apps:
        if app.name not in updated:
            updated[app.name] = app
    return updated


def reset_baseline(apps: Iterable[AppStats]) -> dict[str, AppStats]:
    """Baseline reset (`z`): every currently visible app's Δ starts counting from now."""
    return {app.name: app for app in apps}


def build_rows(apps: Iterable[AppStats], baseline: Mapping[str, AppStats]) -> list[Row]:
    """Build display rows with Δ computed against the baseline.

    An app missing from `baseline` (first seen after the baseline was taken) gets
    Δ 0: its own current values stand in for its baseline.
    """
    rows: list[Row] = []
    for app in apps:
        base = baseline.get(app.name, app)
        rows.append(
            Row(
                name=app.name,
                swap=app.swap,
                ram=app.ram,
                cache=app.cache,
                total=app.total,
                delta_swap=app.swap - base.swap,
                delta_ram=app.ram - base.ram,
                procs=app.procs,
            )
        )
    return rows


_SORT_GETTERS: dict[SortKey, Callable[[Row], int | str]] = {
    "app": lambda row: row.name,
    "swap": lambda row: row.swap,
    "ram": lambda row: row.ram,
    "cache": lambda row: row.cache,
    "total": lambda row: row.total,
    "delta_swap": lambda row: row.delta_swap,
    "delta_ram": lambda row: row.delta_ram,
    "procs": lambda row: row.procs,
}

# First selection of a column: numeric columns start high-to-low (biggest first,
# matching the TOTAL-descending default), the app-name column starts A-Z. SPEC.md
# only pins down the TOTAL default; this picks a direction for the rest.
_DEFAULT_REVERSE: dict[SortKey, bool] = {
    "app": False,
    "swap": True,
    "ram": True,
    "cache": True,
    "total": True,
    "delta_swap": True,
    "delta_ram": True,
    "procs": True,
}


def next_sort_state(
    current_key: SortKey, current_reverse: bool, clicked_key: SortKey
) -> tuple[SortKey, bool]:
    """Sort-state transition for a header click or a repeated `s`/`r`/`t`/`d` key."""
    if clicked_key == current_key:
        return clicked_key, not current_reverse
    return clicked_key, _DEFAULT_REVERSE[clicked_key]


def sort_rows(rows: Iterable[Row], key: SortKey, reverse: bool) -> list[Row]:
    """Sort rows by `key`. Ties always break by app name ascending, both directions.

    Two stable passes: sort by name first, then by `key` with `reverse`. Python's
    sort is stable, and `reverse=True` does not reverse the order of equal
    elements, so equal-`key` rows keep the name-ascending order from the first pass.
    """
    getter = _SORT_GETTERS[key]
    by_name = sorted(rows, key=lambda row: row.name)
    return sorted(by_name, key=getter, reverse=reverse)
