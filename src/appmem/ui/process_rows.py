"""Process-view row model and sorting (SPEC.md "Process view").

Pure functions with no Textual imports, so the `other`-row math, grouping and
sort-state transitions are unit-testable without a running app (SPEC.md
"Tests": "Process view math"). Mirrors `ui/rows.py`'s shape for the main view.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from appmem.collect import AppStats, CommandStats, ProcStats, group_by_command, other_row

OTHER_UNIT_TEXT = "(held by the app, not by any process)"

ProcessSortKey = str
"""One of: ``pid``, ``name``, ``swap``, ``ram``, ``total``, ``age``, ``unit``."""

GroupSortKey = str
"""One of: ``name``, ``swap``, ``ram``, ``total``, ``procs``."""

DEFAULT_SORT_KEY: ProcessSortKey = "total"
DEFAULT_SORT_REVERSE = True

# Columns shared between the process view and the grouped-by-command view
# (SPEC.md "Process view", `g`): sort state carries over across the two only
# for these.
SHARED_SORT_KEYS = frozenset({"name", "swap", "ram", "total"})

# `s`/`r`/`t` (SPEC.md "Keys"); other columns sort by header click only.
KEY_SORT_COLUMNS: dict[str, ProcessSortKey] = {"s": "swap", "r": "ram", "t": "total"}

# First selection of a column: numeric columns start high-to-low, name/unit
# start A-Z. Mirrors `ui/rows.py`'s `_DEFAULT_REVERSE`; SPEC.md only pins down
# the TOTAL default.
_DEFAULT_REVERSE: dict[str, bool] = {
    "pid": False,
    "name": False,
    "swap": True,
    "ram": True,
    "total": True,
    "age": True,
    "unit": False,
    "procs": True,
}

_MAIN_TO_PROCESS_SORT: dict[str, ProcessSortKey] = {
    "swap": "swap",
    "ram": "ram",
    "total": "total",
    "app": "name",
}


@dataclass(frozen=True)
class ProcessRow:
    """One process-view row. `pid`/`age_seconds` are `None` only for the
    synthetic `other` row built by `other_process_row`."""

    pid: int | None
    name: str
    swap: int
    ram: int
    total: int
    age_seconds: float | None
    unit: str


@dataclass(frozen=True)
class CommandRow:
    """One grouped-by-command row (SPEC.md "Process view", `g`). `procs` is
    `None` only for the synthetic `other` row built by `other_command_row`."""

    name: str
    swap: int
    ram: int
    total: int
    procs: int | None


def build_process_rows(procs: Iterable[ProcStats]) -> list[ProcessRow]:
    """Build one row per process. Does not include the `other` row."""
    return [
        ProcessRow(
            pid=proc.pid,
            name=proc.name,
            swap=proc.swap,
            ram=proc.ram,
            total=proc.swap + proc.ram,
            age_seconds=proc.age_seconds,
            unit=proc.unit,
        )
        for proc in procs
    ]


def other_process_row(app: AppStats, procs: Iterable[ProcStats]) -> ProcessRow:
    """The synthetic `other` row: app total minus its processes, clamped at 0."""
    other_swap, other_ram = other_row(app, procs)
    return ProcessRow(
        pid=None,
        name="other",
        swap=other_swap,
        ram=other_ram,
        total=other_swap + other_ram,
        age_seconds=None,
        unit=OTHER_UNIT_TEXT,
    )


def build_command_rows(procs: Iterable[ProcStats]) -> list[CommandRow]:
    """Build one row per command name (`g`), via `collect.group_by_command`."""
    return [_command_row(group) for group in group_by_command(procs)]


def _command_row(group: CommandStats) -> CommandRow:
    return CommandRow(
        name=group.name,
        swap=group.swap,
        ram=group.ram,
        total=group.swap + group.ram,
        procs=group.count,
    )


def other_command_row(app: AppStats, procs: Iterable[ProcStats]) -> CommandRow:
    """The synthetic `other` row for the grouped view: same math, no PROCS count."""
    other_swap, other_ram = other_row(app, procs)
    return CommandRow(
        name="other", swap=other_swap, ram=other_ram, total=other_swap + other_ram, procs=None
    )


_PROCESS_SORT_GETTERS: dict[ProcessSortKey, Callable[[ProcessRow], int | float | str]] = {
    "pid": lambda row: row.pid if row.pid is not None else 0,
    "name": lambda row: row.name,
    "swap": lambda row: row.swap,
    "ram": lambda row: row.ram,
    "total": lambda row: row.total,
    "age": lambda row: row.age_seconds if row.age_seconds is not None else 0.0,
    "unit": lambda row: row.unit,
}

_GROUP_SORT_GETTERS: dict[GroupSortKey, Callable[[CommandRow], int | str]] = {
    "name": lambda row: row.name,
    "swap": lambda row: row.swap,
    "ram": lambda row: row.ram,
    "total": lambda row: row.total,
    "procs": lambda row: row.procs if row.procs is not None else 0,
}


def sort_process_rows(
    rows: Iterable[ProcessRow], key: ProcessSortKey, reverse: bool
) -> list[ProcessRow]:
    """Sort real process rows (never the `other` row) by `key`.

    Ties break by name then PID, both directions (SPEC.md "Process view").
    """
    getter = _PROCESS_SORT_GETTERS[key]
    by_pid = sorted(rows, key=lambda row: row.pid if row.pid is not None else 0)
    by_name = sorted(by_pid, key=lambda row: row.name)
    return sorted(by_name, key=getter, reverse=reverse)


def sort_command_rows(
    rows: Iterable[CommandRow], key: GroupSortKey, reverse: bool
) -> list[CommandRow]:
    """Sort real command rows (never the `other` row) by `key`. Ties break by name."""
    getter = _GROUP_SORT_GETTERS[key]
    by_name = sorted(rows, key=lambda row: row.name)
    return sorted(by_name, key=getter, reverse=reverse)


def next_sort_state(current_key: str, current_reverse: bool, clicked_key: str) -> tuple[str, bool]:
    """Sort-state transition for a header click or a repeated `s`/`r`/`t` key."""
    if clicked_key == current_key:
        return clicked_key, not current_reverse
    return clicked_key, _DEFAULT_REVERSE[clicked_key]


def initial_process_sort(
    main_sort_key: str, main_sort_reverse: bool
) -> tuple[ProcessSortKey, bool]:
    """Map the main view's current sort column to the process view (SPEC.md).

    SWAP/RAM/TOTAL and APP->NAME carry over their column and direction; any
    other main-view column (CACHE, the two Δ columns, PROCS) has no
    process-view equivalent, so the process view falls back to its own
    default (TOTAL desc).
    """
    process_key = _MAIN_TO_PROCESS_SORT.get(main_sort_key)
    if process_key is None:
        return DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
    return process_key, main_sort_reverse
