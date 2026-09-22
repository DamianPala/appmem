"""Tests for `appmem.ui.rows` (SPEC.md "Main view", "Definitions": Δ)."""

from appmem.collect import AppStats
from appmem.ui.rows import (
    Row,
    build_rows,
    next_sort_state,
    reset_baseline,
    sort_rows,
    update_baseline,
)


def _app(name: str, *, swap: int = 0, ram: int = 0, total: int = 0, procs: int = 0) -> AppStats:
    return AppStats(name=name, ram=ram, cache=0, swap=swap, total=total, procs=procs, unit_paths=())


def _row(name: str, *, swap: int = 0, ram: int = 0, total: int = 0, procs: int = 0) -> Row:
    return Row(
        name=name,
        swap=swap,
        ram=ram,
        cache=0,
        total=total,
        delta_swap=0,
        delta_ram=0,
        procs=procs,
    )


# --- Δ baseline --------------------------------------------------------------


def test_update_baseline_adds_apps_first_seen() -> None:
    baseline = update_baseline([_app("ghostty", swap=10)], {})

    assert baseline == {"ghostty": _app("ghostty", swap=10)}


def test_update_baseline_keeps_existing_entries_untouched() -> None:
    existing = _app("ghostty", swap=10)
    baseline = update_baseline([_app("ghostty", swap=50)], {"ghostty": existing})

    assert baseline["ghostty"] is existing


def test_reset_baseline_replaces_every_current_app() -> None:
    baseline = reset_baseline([_app("ghostty", swap=50), _app("brave", swap=5)])

    assert baseline["ghostty"].swap == 50
    assert baseline["brave"].swap == 5


def test_build_rows_delta_is_current_minus_baseline() -> None:
    apps = [_app("ghostty", swap=60, ram=100, total=160)]
    baseline = {"ghostty": _app("ghostty", swap=10, ram=90, total=100)}

    rows = build_rows(apps, baseline)

    assert rows[0].delta_swap == 50
    assert rows[0].delta_ram == 10
    assert rows[0].swap == 60
    assert rows[0].total == 160


def test_build_rows_app_missing_from_baseline_gets_zero_delta() -> None:
    # First seen after the baseline was taken (SPEC.md "Definitions"): its own
    # first sample stands in for its baseline, so Δ is 0.
    apps = [_app("newapp", swap=42, ram=7)]

    rows = build_rows(apps, baseline={})

    assert rows[0].delta_swap == 0
    assert rows[0].delta_ram == 0


# --- sorting -------------------------------------------------------------------


def test_sort_rows_by_total_descending() -> None:
    rows = [_row("a", total=10), _row("b", total=30), _row("c", total=20)]

    ordered = sort_rows(rows, "total", reverse=True)

    assert [row.name for row in ordered] == ["b", "c", "a"]


def test_sort_rows_ties_break_by_name_ascending_when_not_reversed() -> None:
    rows = [_row("charlie", total=10), _row("alpha", total=10), _row("bravo", total=10)]

    ordered = sort_rows(rows, "total", reverse=False)

    assert [row.name for row in ordered] == ["alpha", "bravo", "charlie"]


def test_sort_rows_ties_break_by_name_ascending_even_when_reversed() -> None:
    # SPEC.md "Main view": "Rows with equal values keep a stable order by app
    # name" -- ascending regardless of the sort direction.
    rows = [_row("charlie", total=10), _row("alpha", total=10), _row("bravo", total=10)]

    ordered = sort_rows(rows, "total", reverse=True)

    assert [row.name for row in ordered] == ["alpha", "bravo", "charlie"]


def test_sort_rows_by_app_name() -> None:
    rows = [_row("ghostty"), _row("brave"), _row("code")]

    ordered = sort_rows(rows, "app", reverse=False)

    assert [row.name for row in ordered] == ["brave", "code", "ghostty"]


# --- sort-state transitions ----------------------------------------------------


def test_next_sort_state_clicking_a_new_column_uses_its_default_direction() -> None:
    key, reverse = next_sort_state("total", True, "app")

    assert (key, reverse) == ("app", False)  # app defaults ascending


def test_next_sort_state_clicking_the_same_column_reverses() -> None:
    key, reverse = next_sort_state("total", True, "total")

    assert (key, reverse) == ("total", False)

    key, reverse = next_sort_state(key, reverse, "total")

    assert (key, reverse) == ("total", True)
