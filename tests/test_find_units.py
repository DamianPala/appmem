"""Tests for `appmem.collect.find_units` (SPEC.md "Finding units")."""

from pathlib import Path

import pytest

from appmem.collect import CgroupUnavailableError, find_units
from helpers import make_unit, user_service_root


def test_finds_units_under_app_session_and_background_slice(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    make_unit(user_root / "app.slice" / "app-ghostty.service")
    make_unit(user_root / "session.slice" / "some-session.scope")
    make_unit(user_root / "background.slice" / "tracker-extract-3.service")
    # user@1000.service itself needs a memory.stat for the tree to count as available.
    make_unit(user_root)

    units = find_units(tmp_path, uid=1000, include_system=False)

    names = {p.name for p in units}
    assert names == {
        "app-ghostty.service",
        "some-session.scope",
        "tracker-extract-3.service",
    }


def test_nested_scope_counted_once_at_the_unit(tmp_path: Path) -> None:
    # Konsole keeps one tab(PID).scope per tab under its own unit; the walk must
    # stop at the unit and never descend into it.
    user_root = user_service_root(tmp_path, uid=1000)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.kde.konsole-123.scope"
    make_unit(unit_dir)
    make_unit(unit_dir / "tab(456).scope")

    units = find_units(tmp_path, uid=1000, include_system=False)

    assert units == [unit_dir]


def test_system_service_found_only_when_include_system(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    make_unit(user_root)
    system_root = tmp_path / "sys" / "fs" / "cgroup" / "system.slice"
    make_unit(system_root / "system-cups.slice" / "cups.service")

    without_system = find_units(tmp_path, uid=1000, include_system=False)
    with_system = find_units(tmp_path, uid=1000, include_system=True)

    assert without_system == []
    assert [p.name for p in with_system] == ["cups.service"]


def test_socket_and_mount_dirs_are_ignored(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    make_unit(user_root)
    app_slice = user_root / "app.slice"
    make_unit(app_slice / "app-ghostty.service")
    (app_slice / "some.socket").mkdir(parents=True)
    (app_slice / "some.mount").mkdir(parents=True)
    (app_slice / "some.swap").mkdir(parents=True)

    units = find_units(tmp_path, uid=1000, include_system=False)

    assert [p.name for p in units] == ["app-ghostty.service"]


def test_missing_user_tree_raises(tmp_path: Path) -> None:
    with pytest.raises(CgroupUnavailableError, match=r"user@1000\.service"):
        find_units(tmp_path, uid=1000, include_system=False)


def test_missing_memory_stat_on_user_root_raises(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    user_root.mkdir(parents=True)

    with pytest.raises(CgroupUnavailableError, match=r"memory\.stat"):
        find_units(tmp_path, uid=1000, include_system=False)
