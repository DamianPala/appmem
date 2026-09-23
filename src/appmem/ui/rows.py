"""Main-view row model, Δ baseline bookkeeping and sorting (SPEC.md "Main view").

Pure functions with no Textual imports, so the Δ math and tie-break ordering are
unit-testable without a running app (SPEC.md "Tests").
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from appmem.collect import AppStats

SortKey = Literal[
    "app", "swap", "ram", "cache", "zswap", "total", "delta_swap", "delta_ram", "procs"
]
"""A `Literal`, not a plain `str`: pyright strict then catches a typo'd column
name at the call site instead of it surfacing as a runtime `KeyError` in
`_DEFAULT_REVERSE`. Still doubles as a `DataTable` column key, which only
ever needs `str`."""

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
    scope: str
    swap: int
    ram: int
    cache: int
    zswap: int
    total: int
    delta_swap: int
    delta_ram: int
    procs: int


Identity = tuple[str, str]
"""`(scope, name)`: app identity (SPEC.md "Grouping"). A user and a system
unit that normalize to the same name are two separate identities."""


def _identity(app: AppStats) -> Identity:
    return (app.scope, app.name)


def row_key(name: str, scope: str) -> str:
    """Unique `DataTable`/dict-safe key for an app identity. `\\0` can't appear
    in a systemd unit (and therefore app) name, so this never collides across
    scopes (SPEC.md "Grouping": app identity is scope + name)."""
    return f"{scope}\0{name}"


def update_baseline(
    apps: Iterable[AppStats], baseline: Mapping[Identity, AppStats]
) -> dict[Identity, AppStats]:
    """Add apps seen for the first time to the Δ baseline, and drop entries for
    apps no longer present (SPEC.md "Definitions"): a closed and reopened app
    starts its Δ at 0 instead of comparing against its old instance.

    Existing (still-present) entries are kept untouched: only `reset_baseline`
    (key `z`, or startup) moves an already-known app's baseline forward.
    """
    current = {_identity(app) for app in apps}
    updated = {identity: base for identity, base in baseline.items() if identity in current}
    for app in apps:
        identity = _identity(app)
        if identity not in updated:
            updated[identity] = app
    return updated


def reset_baseline(apps: Iterable[AppStats]) -> dict[Identity, AppStats]:
    """Baseline reset (`z`): every currently visible app's Δ starts counting from now."""
    return {_identity(app): app for app in apps}


def build_rows(apps: Iterable[AppStats], baseline: Mapping[Identity, AppStats]) -> list[Row]:
    """Build display rows with Δ computed against the baseline.

    An app missing from `baseline` (first seen after the baseline was taken) gets
    Δ 0: its own current values stand in for its baseline.
    """
    rows: list[Row] = []
    for app in apps:
        base = baseline.get(_identity(app), app)
        rows.append(
            Row(
                name=app.name,
                scope=app.scope,
                swap=app.swap,
                ram=app.ram,
                cache=app.cache,
                zswap=app.zswapped,
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
    "zswap": lambda row: row.zswap,
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
    "zswap": True,
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
    """Sort rows by `key`. Ties always break by `(name, scope)` ascending, both
    directions -- full identity, not just the name a same-named user/system
    pair share (SPEC.md "Main view").

    Two stable passes: sort by identity first, then by `key` with `reverse`.
    Python's sort is stable, and `reverse=True` does not reverse the order of
    equal elements, so equal-`key` rows keep the identity-ascending order from
    the first pass.
    """
    getter = _SORT_GETTERS[key]
    by_identity = sorted(rows, key=lambda row: (row.name, row.scope))
    return sorted(by_identity, key=getter, reverse=reverse)
