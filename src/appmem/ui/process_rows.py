"""Process-view row model and sorting (SPEC.md "Process view").

Pure functions with no Textual imports, so the `kernel`/`unattributed`-row
math, grouping and sort-state transitions are unit-testable without a running
app (SPEC.md "Tests": "Process view math"). Mirrors `ui/rows.py`'s shape for
the main view.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from appmem.collect import AppStats, CommandStats, ProcStats, group_by_command, unattributed_row

# `\0` can't appear in a process/command name (`_read_proc_name` splits on it),
# so these keys never collide with a real PID string or command name -- even
# one literally named "kernel" or "unattributed".
KERNEL_KEY = "\0kernel"
UNATTRIBUTED_KEY = "\0unattributed"
KERNEL_UNIT_TEXT = "charged kernel memory: page tables, slab, stacks"
UNATTRIBUTED_UNIT_TEXT = "accounting difference, not a process"

ProcessSortKey = Literal["pid", "name", "swap", "ram", "total", "age", "unit"]

GroupSortKey = Literal["name", "swap", "ram", "total", "procs"]

ScreenSortKey = ProcessSortKey | GroupSortKey
"""The two sort-key sets `ProcessesScreen` juggles between its flat and
grouped-by-command tables. `Literal`, not `str`, so pyright strict catches a
typo'd column name."""

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
# the TOTAL default. Covers both `ProcessSortKey` and `GroupSortKey`: the flat
# and grouped-by-command tables share one sort-state variable.
_DEFAULT_REVERSE: dict[ScreenSortKey, bool] = {
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
    """One process-view row. `key` is the `DataTable` row key: `str(pid)` for a
    real process, `KERNEL_KEY`/`UNATTRIBUTED_KEY` for the two synthetic rows
    built by `kernel_process_row`/`unattributed_process_row` (`pid`/
    `age_seconds` are `None` only for those)."""

    key: str
    pid: int | None
    name: str
    swap: int
    ram: int
    total: int
    age_seconds: float | None
    unit: str
    dim: bool = False
    """Render dim/italic: true for the `kernel` and `unattributed` rows, which
    are an accounting split, not a process (SPEC.md "Definitions")."""


@dataclass(frozen=True)
class CommandRow:
    """One grouped-by-command row (SPEC.md "Process view", `g`). `procs` is
    `None` only for the two synthetic rows built by `kernel_command_row`/
    `unattributed_command_row`."""

    key: str
    name: str
    swap: int
    ram: int
    total: int
    procs: int | None
    dim: bool = False
    units: tuple[str, ...] = ()
    """Sorted, de-duplicated unit names the group's processes belong to
    (used by the status line). Empty for the two synthetic rows, which don't
    belong to one command."""


def build_process_rows(procs: Iterable[ProcStats]) -> list[ProcessRow]:
    """Build one row per process. Does not include the `kernel`/`unattributed` rows."""
    return [
        ProcessRow(
            key=str(proc.pid),
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


def kernel_process_row(app: AppStats) -> ProcessRow:
    """The app's charged kernel memory, exact (no process holds it directly)."""
    return ProcessRow(
        key=KERNEL_KEY,
        pid=None,
        name="kernel",
        swap=0,
        ram=app.kernel,
        total=app.kernel,
        age_seconds=None,
        unit=KERNEL_UNIT_TEXT,
        dim=True,
    )


def unattributed_process_row(app: AppStats, procs: Iterable[ProcStats]) -> ProcessRow:
    """The synthetic `unattributed` row: app total minus its processes minus its
    kernel share, clamped at 0."""
    swap, ram = unattributed_row(app, procs)
    return ProcessRow(
        key=UNATTRIBUTED_KEY,
        pid=None,
        name="unattributed",
        swap=swap,
        ram=ram,
        total=swap + ram,
        age_seconds=None,
        unit=UNATTRIBUTED_UNIT_TEXT,
        dim=True,
    )


def build_command_rows(procs: Iterable[ProcStats]) -> list[CommandRow]:
    """Build one row per command name (`g`), via `collect.group_by_command`."""
    return [_command_row(group) for group in group_by_command(procs)]


def _command_row(group: CommandStats) -> CommandRow:
    return CommandRow(
        key=group.name,
        name=group.name,
        swap=group.swap,
        ram=group.ram,
        total=group.swap + group.ram,
        procs=group.count,
        units=group.units,
    )


def kernel_command_row(app: AppStats) -> CommandRow:
    """The grouped view's `kernel` row: same value as `kernel_process_row`, no
    PROCS count."""
    return CommandRow(
        key=KERNEL_KEY,
        name="kernel",
        swap=0,
        ram=app.kernel,
        total=app.kernel,
        procs=None,
        dim=True,
    )


def unattributed_command_row(app: AppStats, procs: Iterable[ProcStats]) -> CommandRow:
    """The grouped view's `unattributed` row: same math as
    `unattributed_process_row`, no PROCS count."""
    swap, ram = unattributed_row(app, procs)
    return CommandRow(
        key=UNATTRIBUTED_KEY,
        name="unattributed",
        swap=swap,
        ram=ram,
        total=swap + ram,
        procs=None,
        dim=True,
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
    """Sort real process rows (never the `kernel`/`unattributed` rows) by `key`.

    Ties break by name then PID, both directions (SPEC.md "Process view").
    """
    getter = _PROCESS_SORT_GETTERS[key]
    by_pid = sorted(rows, key=lambda row: row.pid if row.pid is not None else 0)
    by_name = sorted(by_pid, key=lambda row: row.name)
    return sorted(by_name, key=getter, reverse=reverse)


def sort_command_rows(
    rows: Iterable[CommandRow], key: GroupSortKey, reverse: bool
) -> list[CommandRow]:
    """Sort real command rows (never the `kernel`/`unattributed` rows) by `key`.
    Ties break by name."""
    getter = _GROUP_SORT_GETTERS[key]
    by_name = sorted(rows, key=lambda row: row.name)
    return sorted(by_name, key=getter, reverse=reverse)


def next_sort_state(
    current_key: ScreenSortKey, current_reverse: bool, clicked_key: ScreenSortKey
) -> tuple[ScreenSortKey, bool]:
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
