"""Builds the JSON-ready documents for `appmem snapshot` and `appmem app`.

Pure functions over `collect`: no UI imports, no argument parsing, no stdout.
Reuses the same grouping and remainder math the TUI's process view already
has, so a number an agent reads here matches what a person sees on screen for
the same (scope, name).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from appmem.collect import (
    AppStats,
    CommandStats,
    ProcStats,
    SystemStats,
    Unit,
    filter_visible_apps,
    find_app_units,
    find_units,
    group_apps,
    group_by_command,
    read_procs,
    read_system,
    read_unit,
    unattributed_row,
    unit_scope,
)


class AppNotFoundError(Exception):
    """No cgroup unit currently maps to the (scope, name) the caller asked for."""


# --- snapshot ------------------------------------------------------------------


def _collect_visible_apps(root: Path, uid: int, include_system: bool) -> list[AppStats]:
    """Every app worth showing, sorted by total descending then scope then
    name: the same visibility and ordering `snapshot` promises. Read once;
    both the JSON page and the text report's total come from this one list,
    so nothing walks the cgroup tree twice for a single call."""
    unit_paths = find_units(root, uid, include_system=include_system, strict=True)
    units = [
        Unit(path=path, stats=stats, scope=unit_scope(root, path))
        for path in unit_paths
        if (stats := read_unit(path)) is not None
    ]
    apps = filter_visible_apps(group_apps(units))
    return sorted(apps, key=lambda app: (-app.total, app.scope, app.name))


def _pressure_level(some_avg10: float, full_avg10: float) -> str:
    if full_avg10 > 5 or some_avg10 > 20:
        return "high"
    if some_avg10 >= 1:
        return "some"
    return "none"


def _pressure_dict(stats: SystemStats) -> dict[str, Any] | None:
    if (
        stats.pressure_some_avg10 is None
        or stats.pressure_some_avg60 is None
        or stats.pressure_full_avg10 is None
        or stats.pressure_full_avg60 is None
    ):
        return None
    return {
        "level": _pressure_level(stats.pressure_some_avg10, stats.pressure_full_avg10),
        "some_avg10_percent": stats.pressure_some_avg10,
        "some_avg60_percent": stats.pressure_some_avg60,
        "full_avg10_percent": stats.pressure_full_avg10,
        "full_avg60_percent": stats.pressure_full_avg60,
    }


def _system_dict(stats: SystemStats) -> dict[str, Any]:
    return {
        "ram_total_bytes": stats.mem_total,
        "ram_used_bytes": stats.mem_total - stats.mem_available,
        "ram_available_bytes": stats.mem_available,
        "swap_total_bytes": stats.swap_total,
        "swap_used_bytes": stats.swap_total - stats.swap_free,
        "system_services_ram_bytes": stats.system_ram,
        "system_services_swap_bytes": stats.system_swap,
        "elsewhere_bytes": stats.elsewhere,
    }


def _app_item(app: AppStats) -> dict[str, Any]:
    return {
        "name": app.name,
        "scope": app.scope,
        "ram_bytes": app.ram,
        "swap_bytes": app.swap,
        "total_bytes": app.total,
        "cache_bytes": app.cache,
        "procs": app.procs,
        "units": len(app.unit_paths),
    }


class SnapshotResult(NamedTuple):
    """`snapshot_document`'s return: the JSON document, plus the total
    visible-app count from the same pass the document's own page came from.
    The text report's "N of M" line needs a real total; reusing this count
    instead of collecting again keeps a `snapshot` call to one tree walk."""

    document: dict[str, Any]
    total_apps: int


def snapshot_document(
    root: Path, uid: int, *, include_system: bool, limit: int, now: datetime
) -> SnapshotResult:
    """One sample of the machine and every app big enough to matter, capped
    at `limit` items and breadcrumbed to the largest one."""
    stats = read_system(root, uid)
    apps = _collect_visible_apps(root, uid, include_system)
    page = apps[:limit]

    document: dict[str, Any] = {
        "taken_at": now.isoformat(timespec="seconds"),
        "system": _system_dict(stats),
        "pressure": _pressure_dict(stats),
        "apps": {"items": [_app_item(app) for app in page], "has_more": len(apps) > limit},
    }
    if page:
        first = page[0]
        next_call = ["appmem", "app", first.name]
        if first.scope == "system":
            next_call.extend(["--scope", "system"])
        document["next"] = next_call
    return SnapshotResult(document, len(apps))


# --- app -----------------------------------------------------------------------


def _process_item(proc: ProcStats) -> dict[str, Any]:
    return {
        "pid": proc.pid,
        "name": proc.name,
        "swap_bytes": proc.swap,
        "ram_bytes": proc.ram,
        "total_bytes": proc.swap + proc.ram,
        "age_seconds": proc.age_seconds,
        "unit": proc.unit,
    }


def _command_item(command: CommandStats) -> dict[str, Any]:
    return {
        "name": command.name,
        "swap_bytes": command.swap,
        "ram_bytes": command.ram,
        "total_bytes": command.swap + command.ram,
        "procs": command.count,
    }


def _paged[T](items: list[T], limit: int, to_item: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    page = items[:limit]
    return {"items": [to_item(item) for item in page], "has_more": len(items) > limit}


def _build_app_stats(name: str, scope: str, unit_paths: list[Path]) -> AppStats | None:
    """`None` when every unit vanished between `find_app_units` finding it and
    this read: churn, but with nothing left to report, the same as no unit
    ever matching (scope, name)."""
    unit_stats = [stats for path in unit_paths if (stats := read_unit(path)) is not None]
    if not unit_stats:
        return None
    return AppStats(
        name=name,
        scope=scope,
        ram=sum(s.ram for s in unit_stats),
        cache=sum(s.cache for s in unit_stats),
        swap=sum(s.swap for s in unit_stats),
        total=sum(s.total for s in unit_stats),
        procs=sum(s.procs for s in unit_stats),
        kernel=sum(s.kernel for s in unit_stats),
        unit_paths=tuple(unit_paths),
    )


class AppResult(NamedTuple):
    """`app_document`'s return: the JSON document, plus the total process and
    command counts from the same read the document's own paged lists came
    from, so the text report's "N of M" lines need no second read."""

    document: dict[str, Any]
    total_processes: int
    total_commands: int


def app_document(
    root: Path, uid: int, name: str, scope: str, *, limit: int, now: datetime
) -> AppResult:
    """One app's units, processes, commands grouped by name, and the
    kernel/unattributed remainder, exactly as the TUI's process view computes
    them for the same (scope, name)."""
    unit_paths = find_app_units(
        root, uid, include_system=(scope == "system"), scope=scope, name=name, strict=True
    )
    if not unit_paths:
        raise AppNotFoundError(name)

    app = _build_app_stats(name, scope, unit_paths)
    if app is None:
        raise AppNotFoundError(name)
    procs = read_procs(unit_paths, root)
    unattributed_swap, unattributed_ram = unattributed_row(app, procs)

    processes_sorted = sorted(procs, key=lambda p: (-(p.swap + p.ram), p.name, p.pid))
    commands_sorted = sorted(
        group_by_command(procs), key=lambda command: (-(command.swap + command.ram), command.name)
    )

    document = {
        "taken_at": now.isoformat(timespec="seconds"),
        "name": name,
        "scope": scope,
        "ram_bytes": app.ram,
        "swap_bytes": app.swap,
        "total_bytes": app.total,
        "cache_bytes": app.cache,
        "kernel_bytes": app.kernel,
        "procs": app.procs,
        "units": [path.name for path in unit_paths],
        "processes": _paged(processes_sorted, limit, _process_item),
        "commands": _paged(commands_sorted, limit, _command_item),
        "unattributed_ram_bytes": unattributed_ram,
        "unattributed_swap_bytes": unattributed_swap,
    }
    return AppResult(document, len(processes_sorted), len(commands_sorted))
