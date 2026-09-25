"""Tests for `appmem.collect.unit_app_name` and its use by `group_apps` and
`find_app_units` (SPEC.md "Grouping"): a unit whose name is a generic desktop
id (only `org.chromium.Chromium` for now) is named after its leader process
instead of the shared id every such unit reports to the compositor.

Fixture trees only, built with `tests/helpers.py`; never the live `/proc` or
`/sys` (see AGENTS.md).
"""

from __future__ import annotations

from pathlib import Path

from appmem.collect import (
    Unit,
    find_app_units,
    find_units,
    group_apps,
    read_unit,
    unit_scope,
)
from helpers import make_unit, user_service_root, write_proc

_UID = 1000
_CHROMIUM_SCOPE = "app-org.chromium.Chromium-1907544.scope"
_BRAVE_SERVICE = "app-brave\\x2dbrowser@c16f78223b3c4371a764a76609ae9ef0.service"


def _units(root: Path, uid: int = _UID) -> list[Unit]:
    """The same `Unit` list a real caller builds: `find_units` paired with
    each unit's counters read from the fixture tree."""
    units: list[Unit] = []
    for path in find_units(root, uid, include_system=False):
        stats = read_unit(path)
        assert stats is not None, f"fixture unit unreadable: {path}"
        units.append(Unit(path=path, stats=stats, scope=unit_scope(root, path)))
    return units


def test_chromium_scope_merges_with_its_leaders_own_unit(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    app_slice = user_root / "app.slice"
    make_unit(app_slice / _CHROMIUM_SCOPE, anon=100, swap=10, pids=[1907544, 1907600])
    make_unit(app_slice / _BRAVE_SERVICE, anon=200, swap=20, pids=[1800000])
    write_proc(tmp_path, 1907544, comm="brave")

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"brave"}
    brave = apps[0]
    assert len(brave.unit_paths) == 2
    assert (brave.ram, brave.swap, brave.procs) == (300, 30, 3)

    units = find_app_units(tmp_path, _UID, include_system=False, scope="user", name="brave")
    assert {p.name for p in units} == {_CHROMIUM_SCOPE, _BRAVE_SERVICE}


def test_scope_name_pid_wins_over_a_lower_cgroup_pid(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.chromium.Chromium-5000.scope"
    make_unit(unit_dir, pids=[4000, 5000])
    write_proc(tmp_path, 4000, comm="helper")
    write_proc(tmp_path, 5000, comm="obsidian")

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"obsidian"}


def test_stale_scope_pid_reused_by_another_process_is_ignored(tmp_path: Path) -> None:
    # The scope-name pid (8000) is gone from the unit's own cgroup.procs --
    # wrap-around gave it to an unrelated process -- so it must never be
    # read, even though /proc/8000 itself is readable.
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.chromium.Chromium-8000.scope"
    make_unit(unit_dir, pids=[3000])
    write_proc(tmp_path, 8000, comm="cargo")
    write_proc(tmp_path, 3000, comm="obsidian")

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"obsidian"}


def test_lowest_cgroup_pid_unreadable_tries_the_next_one(tmp_path: Path) -> None:
    # Neither cgroup pid is the scope-name pid, and the lowest one (2000) has
    # no /proc entry: the next one (2001) must be tried, not the unit name.
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.chromium.Chromium-9999.scope"
    make_unit(unit_dir, pids=[2000, 2001])
    write_proc(tmp_path, 2001, comm="obsidian")
    # No /proc/9999 (scope pid) and no /proc/2000 (lowest cgroup pid).

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"obsidian"}


def test_at_most_three_pids_are_tried(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.chromium.Chromium-9999.scope"
    make_unit(unit_dir, pids=[2000, 2001, 2002, 2003])
    write_proc(tmp_path, 2003, comm="obsidian")
    # Only the fourth pid is readable: past the cap, so the unit name wins.

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"chromium"}


def test_leader_pid_gone_falls_back_to_the_lowest_cgroup_pid(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.chromium.Chromium-999.scope"
    make_unit(unit_dir, pids=[1000, 1001])
    write_proc(tmp_path, 1000, comm="obsidian")
    # No /proc/999: the scope's own leader pid vanished.

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"obsidian"}


def test_nothing_readable_falls_back_to_the_unit_name(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    unit_dir = user_root / "app.slice" / "app-org.chromium.Chromium-999.scope"
    make_unit(unit_dir, pids=[])
    # No cgroup.procs entries and no /proc/999 at all.

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"chromium"}


def test_mullvad_leader_merges_with_the_mullvad_vpn_unit(tmp_path: Path) -> None:
    user_root = user_service_root(tmp_path, _UID)
    make_unit(user_root)
    app_slice = user_root / "app.slice"
    make_unit(app_slice / "app-org.chromium.Chromium-4242.scope", pids=[4242])
    make_unit(app_slice / "app-mullvad\\x2dvpn@autostart.service")
    # The GUI's argv0 holds a space, so its process name is the first token.
    write_proc(tmp_path, 4242, cmdline="/opt/Mullvad VPN/mullvad-gui\0--x\0", comm="mullvad-gui")

    apps = group_apps(tmp_path, _units(tmp_path))

    assert {app.name for app in apps} == {"mullvad-vpn"}
    assert len(apps[0].unit_paths) == 2
