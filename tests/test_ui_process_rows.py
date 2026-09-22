"""Tests for `appmem.ui.process_rows` (SPEC.md "Process view")."""

from appmem.collect import AppStats, ProcStats
from appmem.ui.process_rows import (
    build_command_rows,
    build_process_rows,
    initial_process_sort,
    next_sort_state,
    other_command_row,
    other_process_row,
    sort_command_rows,
    sort_process_rows,
)


def _app(*, swap: int = 0, ram: int = 0) -> AppStats:
    return AppStats(
        name="ghostty", ram=ram, cache=0, swap=swap, total=swap + ram, procs=0, unit_paths=()
    )


def _proc(pid: int, name: str, *, swap: int = 0, ram: int = 0, age: float = 0.0) -> ProcStats:
    return ProcStats(pid=pid, name=name, swap=swap, ram=ram, age_seconds=age, unit=f"u{pid}.scope")


# --- process rows --------------------------------------------------------------


def test_build_process_rows_one_row_per_process() -> None:
    rows = build_process_rows([_proc(1, "a", swap=10, ram=20), _proc(2, "b", swap=5, ram=5)])

    assert [(row.pid, row.name, row.total) for row in rows] == [(1, "a", 30), (2, "b", 10)]


def test_other_process_row_is_app_minus_processes_clamped_at_zero() -> None:
    app = _app(swap=500, ram=1000)
    procs = [_proc(1, "a", swap=200, ram=600)]

    other = other_process_row(app, procs)

    assert other.pid is None
    assert other.age_seconds is None
    assert other.name == "other"
    assert other.swap == 300
    assert other.ram == 400
    assert other.total == 700


def test_other_process_row_clamps_at_zero() -> None:
    app = _app(swap=10, ram=10)
    procs = [_proc(1, "a", swap=999, ram=999)]

    other = other_process_row(app, procs)

    assert other.swap == 0
    assert other.ram == 0


def test_other_process_row_is_never_part_of_build_process_rows() -> None:
    # `other` is a synthetic row built separately and appended last by the
    # screen; the real-process builder never fabricates one.
    rows = build_process_rows([_proc(1, "a")])

    assert all(row.pid is not None for row in rows)


def test_sort_process_rows_ties_break_by_name_then_pid() -> None:
    rows = build_process_rows([_proc(3, "b"), _proc(1, "a"), _proc(2, "a")])

    ordered = sort_process_rows(rows, "total", reverse=False)

    assert [(row.name, row.pid) for row in ordered] == [("a", 1), ("a", 2), ("b", 3)]


def test_sort_process_rows_by_swap_descending() -> None:
    rows = build_process_rows(
        [_proc(1, "a", swap=10), _proc(2, "b", swap=30), _proc(3, "c", swap=20)]
    )

    ordered = sort_process_rows(rows, "swap", reverse=True)

    assert [row.pid for row in ordered] == [2, 3, 1]


# --- grouped (command) rows -----------------------------------------------------


def test_build_command_rows_sums_by_name() -> None:
    procs = [_proc(1, "node", swap=10, ram=5), _proc(2, "node", swap=5, ram=5), _proc(3, "claude")]

    rows = {row.name: row for row in build_command_rows(procs)}

    assert rows["node"].swap == 15
    assert rows["node"].ram == 10
    assert rows["node"].procs == 2
    assert rows["claude"].procs == 1


def test_other_command_row_has_no_procs_count() -> None:
    app = _app(swap=100, ram=100)
    procs = [_proc(1, "a", swap=40, ram=40)]

    other = other_command_row(app, procs)

    assert other.name == "other"
    assert other.procs is None
    assert other.swap == 60
    assert other.ram == 60


def test_sort_command_rows_ties_break_by_name() -> None:
    procs = [_proc(1, "charlie"), _proc(2, "alpha"), _proc(3, "bravo")]

    ordered = sort_command_rows(build_command_rows(procs), "total", reverse=False)

    assert [row.name for row in ordered] == ["alpha", "bravo", "charlie"]


# --- sort-state transitions ------------------------------------------------------


def test_next_sort_state_new_column_uses_its_default_direction() -> None:
    assert next_sort_state("total", True, "pid") == ("pid", False)
    assert next_sort_state("total", True, "name") == ("name", False)


def test_next_sort_state_same_column_reverses() -> None:
    key, reverse = next_sort_state("swap", True, "swap")
    assert (key, reverse) == ("swap", False)


# --- initial sort mapped from the main view --------------------------------------


def test_initial_process_sort_carries_over_shared_columns() -> None:
    assert initial_process_sort("swap", True) == ("swap", True)
    assert initial_process_sort("ram", False) == ("ram", False)
    assert initial_process_sort("total", True) == ("total", True)


def test_initial_process_sort_maps_app_to_name() -> None:
    assert initial_process_sort("app", False) == ("name", False)


def test_initial_process_sort_falls_back_to_total_desc_for_unmapped_columns() -> None:
    # CACHE, ΔSWAP, ΔRAM and PROCS have no process-view equivalent.
    for main_key in ("cache", "delta_swap", "delta_ram", "procs"):
        assert initial_process_sort(main_key, False) == ("total", True)
