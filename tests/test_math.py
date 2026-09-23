"""Tests for grouping and process-view math (SPEC.md "Grouping", "Process view")."""

from pathlib import Path

from appmem.collect import (
    AppStats,
    ProcStats,
    Unit,
    UnitStats,
    filter_visible_apps,
    group_apps,
    group_by_command,
    unattributed_row,
)


def _unit(name: str, *, scope: str = "user", **stats_kwargs: int) -> Unit:
    defaults: dict[str, int] = {
        "ram": 0,
        "cache": 0,
        "swap": 0,
        "total": 0,
        "procs": 0,
        "kernel": 0,
    }
    defaults.update(stats_kwargs)
    return Unit(path=Path(f"/fake/{name}"), stats=UnitStats(**defaults), scope=scope)


def test_group_apps_merges_units_by_app_name_and_sums_counters() -> None:
    units = [
        _unit(
            "app-com.mitchellh.ghostty.service",
            ram=100,
            cache=10,
            swap=5,
            total=105,
            procs=3,
            kernel=7,
        ),
        _unit(
            "app-ghostty-surface-transient-1.scope",
            ram=50,
            cache=5,
            swap=1,
            total=51,
            procs=2,
            kernel=3,
        ),
        _unit("pipewire.service", ram=20, cache=0, swap=0, total=20, procs=1),
    ]

    apps = {app.name: app for app in group_apps(units)}

    assert set(apps) == {"ghostty", "pipewire"}
    ghostty = apps["ghostty"]
    assert ghostty.ram == 150
    assert ghostty.cache == 15
    assert ghostty.swap == 6
    assert ghostty.total == 156
    assert ghostty.procs == 5
    assert ghostty.kernel == 10
    assert ghostty.scope == "user"
    assert len(ghostty.unit_paths) == 2


def test_group_apps_keeps_user_and_system_same_name_as_separate_rows() -> None:
    # SPEC.md "Grouping": app identity is (scope, name), so a user dbus and a
    # system dbus stay two rows instead of merging.
    units = [
        _unit("dbus.service", scope="user", ram=10),
        _unit("dbus.service", scope="system", ram=20),
    ]

    apps = group_apps(units)

    assert len(apps) == 2
    by_scope = {app.scope: app for app in apps}
    assert by_scope["user"].ram == 10
    assert by_scope["system"].ram == 20
    assert all(app.name == "dbus" for app in apps)


def test_filter_visible_apps_hides_rows_under_1mib_total() -> None:
    small = AppStats(
        name="tiny", ram=0, cache=0, swap=0, total=1024 * 1024 - 1, procs=0, unit_paths=()
    )
    boundary = AppStats(
        name="boundary", ram=0, cache=999_999_999, swap=0, total=1024 * 1024, procs=0, unit_paths=()
    )

    visible = filter_visible_apps([small, boundary])

    assert [app.name for app in visible] == ["boundary"]


def _proc(name: str, swap: int, ram: int) -> ProcStats:
    return ProcStats(pid=1, name=name, swap=swap, ram=ram, age_seconds=0.0, unit="u")


def test_unattributed_row_is_app_minus_process_sum_and_kernel_clamped_at_zero() -> None:
    app = AppStats(
        name="ghostty", ram=1000, cache=0, swap=500, total=1500, procs=2, unit_paths=(), kernel=200
    )
    procs = [_proc("a", swap=100, ram=300), _proc("b", swap=50, ram=200)]

    swap, ram = unattributed_row(app, procs)

    assert swap == 500 - 150
    assert ram == 1000 - 500 - 200


def test_unattributed_row_clamps_at_zero_when_processes_and_kernel_exceed_app_total() -> None:
    # Shared pages can make process rows sum to more than the app.
    app = AppStats(
        name="ghostty", ram=100, cache=0, swap=50, total=150, procs=1, unit_paths=(), kernel=50
    )
    procs = [_proc("a", swap=200, ram=500)]

    swap, ram = unattributed_row(app, procs)

    assert swap == 0
    assert ram == 0


def test_group_by_command_sums_swap_ram_and_count_per_name() -> None:
    procs = [
        _proc("node", swap=10, ram=20),
        _proc("node", swap=5, ram=15),
        _proc("claude", swap=1, ram=2),
    ]

    groups = {g.name: g for g in group_by_command(procs)}

    assert groups["node"].swap == 15
    assert groups["node"].ram == 35
    assert groups["node"].count == 2
    assert groups["claude"].count == 1
