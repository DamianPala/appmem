"""Tests for `appmem.report.snapshot_document`."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from appmem.cli import main
from appmem.report import snapshot_document
from helpers import (
    make_unit,
    user_service_root,
    write_meminfo,
    write_memory_stat,
    write_pressure,
)

_NOW = datetime(2026, 9, 23, 0, 30, 39, tzinfo=timezone(timedelta(hours=2)))
_MIB = 1024 * 1024
_RFC3339 = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")


def _base_tree(root: Path, uid: int = 1000) -> Path:
    user_root = user_service_root(root, uid=uid)
    write_memory_stat(user_root, anon=1)  # the user root's own memory.stat, for `find_units`
    write_meminfo(
        root,
        mem_total_kb=32 * 1024 * 1024,
        mem_available_kb=12 * 1024 * 1024,
        swap_total_kb=32 * 1024 * 1024,
        swap_free_kb=10 * 1024 * 1024,
    )
    return user_root


def test_document_matches_the_expected_shape_for_two_apps_and_a_hidden_one(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    make_unit(
        user_root / "app.slice" / "app-ghostty.service",
        anon=6 * _MIB,
        shmem=0,
        kernel=0,
        file=0,
        swap=11 * _MIB,
    )
    make_unit(
        user_root / "app.slice" / "app-brave-browser@1.service",
        anon=1 * _MIB,
        shmem=0,
        kernel=0,
        file=0,
        swap=2 * _MIB,
    )
    # Under 1 MiB total: hidden from every call regardless of --system.
    make_unit(
        user_root / "app.slice" / "app-tiny.service", anon=1000, shmem=0, kernel=0, file=0, swap=0
    )
    system_slice = tmp_path / "sys" / "fs" / "cgroup" / "system.slice"
    make_unit(system_slice / "cups.service", anon=2 * _MIB, shmem=0, kernel=0, file=0, swap=0)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    document, total_apps = snapshot_document(
        tmp_path, 1000, include_system=False, limit=50, now=_NOW
    )

    assert document["taken_at"] == "2026-09-23T00:30:39+02:00"
    assert _RFC3339.match(document["taken_at"])
    assert document["pressure"] == {
        "level": "none",
        "some_avg10_percent": 0.0,
        "some_avg60_percent": 0.0,
        "full_avg10_percent": 0.0,
        "full_avg60_percent": 0.0,
    }
    items = document["apps"]["items"]
    assert [item["name"] for item in items] == ["ghostty", "brave"]  # sorted by total_bytes desc
    assert document["apps"]["has_more"] is False
    assert total_apps == 2  # the tiny app and the hidden system unit don't count
    for item in items:
        for key in ("ram_bytes", "swap_bytes", "total_bytes", "cache_bytes", "procs", "units"):
            assert isinstance(item[key], int)
    assert document["next"] == ["appmem", "app", "ghostty"]


def test_system_units_hidden_without_system_and_scoped_with_it(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * _MIB)
    system_slice = tmp_path / "sys" / "fs" / "cgroup" / "system.slice"
    make_unit(system_slice / "cups.service", anon=5 * _MIB)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    without_system, total_without = snapshot_document(
        tmp_path, 1000, include_system=False, limit=50, now=_NOW
    )
    assert [item["name"] for item in without_system["apps"]["items"]] == ["ghostty"]
    assert total_without == 1

    with_system, total_with = snapshot_document(
        tmp_path, 1000, include_system=True, limit=50, now=_NOW
    )
    names_and_scopes = {(item["name"], item["scope"]) for item in with_system["apps"]["items"]}
    assert ("cups", "system") in names_and_scopes
    assert ("ghostty", "user") in names_and_scopes
    # The larger cups item sorts first and becomes the breadcrumb, with scope mirrored.
    assert with_system["apps"]["items"][0]["name"] == "cups"
    assert with_system["next"] == ["appmem", "app", "cups", "--scope", "system"]
    assert total_with == 2


def test_ties_break_by_scope_then_name(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    make_unit(user_root / "app.slice" / "app-zeta.service", anon=2 * _MIB)
    make_unit(user_root / "app.slice" / "app-alpha.service", anon=2 * _MIB)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    document, _ = snapshot_document(tmp_path, 1000, include_system=False, limit=50, now=_NOW)

    assert [item["name"] for item in document["apps"]["items"]] == ["alpha", "zeta"]


def test_limit_cuts_the_page_and_reports_has_more(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    make_unit(user_root / "app.slice" / "app-a.service", anon=3 * _MIB)
    make_unit(user_root / "app.slice" / "app-b.service", anon=2 * _MIB)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    document, total_apps = snapshot_document(
        tmp_path, 1000, include_system=False, limit=1, now=_NOW
    )

    assert [item["name"] for item in document["apps"]["items"]] == ["a"]
    assert document["apps"]["has_more"] is True
    assert document["next"] == ["appmem", "app", "a"]
    assert total_apps == 2


def test_has_more_is_false_when_exactly_limit_apps_exist(tmp_path: Path) -> None:
    user_root = _base_tree(tmp_path)
    make_unit(user_root / "app.slice" / "app-a.service", anon=3 * _MIB)
    make_unit(user_root / "app.slice" / "app-b.service", anon=2 * _MIB)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    document, total_apps = snapshot_document(
        tmp_path, 1000, include_system=False, limit=2, now=_NOW
    )

    assert document["apps"]["has_more"] is False
    assert total_apps == 2


def test_no_items_means_no_next_field(tmp_path: Path) -> None:
    _base_tree(tmp_path)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    document, total_apps = snapshot_document(
        tmp_path, 1000, include_system=False, limit=50, now=_NOW
    )

    assert document["apps"]["items"] == []
    assert "next" not in document
    assert total_apps == 0


def test_pressure_is_null_without_the_pressure_file(tmp_path: Path) -> None:
    _base_tree(tmp_path)
    # No `proc/pressure/memory` written at all.

    document, _ = snapshot_document(tmp_path, 1000, include_system=False, limit=50, now=_NOW)

    assert document["pressure"] is None


def test_system_includes_ram_free_cache_and_shared_bytes(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    write_memory_stat(user_root, anon=1)
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=0,
        swap_free_kb=0,
        mem_free_kb=2_000_000,
        cached_kb=7_000_000,
        shmem_kb=3_000_000,
    )

    document, _ = snapshot_document(tmp_path, 1000, include_system=False, limit=50, now=_NOW)

    system = document["system"]
    assert system["ram_free_bytes"] == 2_000_000 * 1024
    assert system["ram_shared_bytes"] == 3_000_000 * 1024
    assert system["ram_cache_bytes"] == (7_000_000 - 3_000_000) * 1024


def test_elsewhere_is_null_without_the_root_memory_stat(tmp_path: Path) -> None:
    # `_base_tree` never writes `sys/fs/cgroup/memory.stat` (only the user
    # root's own), so `elsewhere_bytes` stays null.
    _base_tree(tmp_path)
    write_pressure(tmp_path, some_avg10=0.0, full_avg10=0.0)

    document, _ = snapshot_document(tmp_path, 1000, include_system=False, limit=50, now=_NOW)

    assert document["system"]["elsewhere_bytes"] is None


@pytest.mark.parametrize(
    ("full_avg10", "some_avg10", "expected_level"),
    [(0.0, 0.5, "none"), (0.0, 1.0, "some"), (0.0, 21.0, "high"), (6.0, 0.0, "high")],
)
def test_pressure_level_thresholds(
    tmp_path: Path, full_avg10: float, some_avg10: float, expected_level: str
) -> None:
    _base_tree(tmp_path)
    write_pressure(tmp_path, some_avg10=some_avg10, full_avg10=full_avg10)

    document, _ = snapshot_document(tmp_path, 1000, include_system=False, limit=50, now=_NOW)

    assert document["pressure"]["level"] == expected_level


@pytest.mark.parametrize("limit_value", ["0", "x"])
def test_cli_bad_limit_is_invalid_input_with_empty_stdout(
    tmp_path: Path, limit_value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_tree(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["snapshot", "--limit", limit_value],
            root=tmp_path,
            uid=1000,
            stdin_isatty=lambda: True,
            stdout_isatty=lambda: True,
        )

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
