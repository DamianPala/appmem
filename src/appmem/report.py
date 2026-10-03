"""Builds the JSON-ready documents for `appmem snapshot` and `appmem app`.

Pure functions over `Backend`: no UI imports, no argument parsing, no stdout.
Reuses the same grouping and remainder math the TUI's process view already
has, so a number an agent reads here matches what a person sees on screen for
the same (scope, name).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, NamedTuple

from appmem.backend import Backend
from appmem.model import (
    AppStats,
    CommandStats,
    ProcStats,
    SystemStats,
    group_by_command,
    unattributed_row,
)
from appmem.naming import unit_label


class AppNotFoundError(Exception):
    """No cgroup unit currently maps to the (scope, name) the caller asked for."""


# --- snapshot ------------------------------------------------------------------


def _collect_visible_apps(backend: Backend, include_system: bool) -> list[AppStats]:
    """Every app worth showing, sorted by total descending then scope then
    name: the same visibility and ordering `snapshot` promises. Read once;
    both the JSON page and the text report's total come from this one list,
    so nothing walks the cgroup tree twice for a single call."""
    apps, _ = backend.collect_apps(
        include_system=include_system, strict=True, count_procs=True, previous_procs={}
    )
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
        "ram_free_bytes": stats.mem_free,
        "ram_cache_bytes": stats.mem_cache,
        "ram_slab_bytes": stats.mem_slab,
        "ram_shared_bytes": stats.mem_shared,
        "swap_total_bytes": stats.swap_total,
        "swap_in_bytes": stats.swap_in_bytes,
        "swap_out_bytes": stats.swap_out_bytes,
        "swap_used_bytes": stats.swap_total - stats.swap_free,
        "system_services_ram_bytes": stats.system_ram,
        "system_services_swap_bytes": stats.system_swap,
        "elsewhere_bytes": stats.elsewhere,
        "zswap_enabled": stats.zswap_enabled,
        "zswap_pool_bytes": stats.zswap_pool_bytes,
        "zswapped_bytes": stats.zswapped_bytes,
        "zswap_writeback_bytes": stats.zswap_writeback_bytes,
        "zswap_compressor": stats.zswap_compressor,
        "zswap_max_pool_percent": stats.zswap_max_pool_percent,
        "zswap_compression_ratio": stats.zswap_compression_ratio,
    }


_TOP_COMMANDS_MIN_PROCS = 6
"""Skip the extra per-process read behind `top_commands` for an app at or
below this many processes: reading `/proc` for every process of every app in
a snapshot page would multiply its cost by the page size, and a handful of
processes rarely groups into anything more informative than the app's own
totals anyway (the exact threshold is a judgment call, not a spec)."""


def _top_command_item(command: CommandStats) -> dict[str, Any]:
    return {
        "name": command.name,
        "total_bytes": command.swap + command.ram,
        "procs": command.count,
    }


def _top_commands(backend: Backend, app: AppStats) -> list[dict[str, Any]]:
    """The 3 largest commands by total, same grouping as `app NAME`. `[]` for
    an app at or below `_TOP_COMMANDS_MIN_PROCS` processes, whose
    `unit_paths` are never read for this."""
    if app.procs <= _TOP_COMMANDS_MIN_PROCS:
        return []
    procs = backend.read_procs(app)
    commands = sorted(group_by_command(procs), key=lambda c: -(c.swap + c.ram))[:3]
    return [_top_command_item(command) for command in commands]


def _app_item(backend: Backend, app: AppStats) -> dict[str, Any]:
    return {
        "name": app.name,
        "scope": app.scope,
        "ram_bytes": app.ram,
        "swap_bytes": app.swap,
        "total_bytes": app.total,
        "cache_bytes": app.cache,
        "zswapped_bytes": app.zswapped,
        "kernel_bytes": app.kernel,
        "procs": app.procs,
        "unit_count": len(app.unit_paths),
        "top_commands": _top_commands(backend, app),
    }


class SnapshotResult(NamedTuple):
    """`snapshot_document`'s return: the JSON document, plus the total
    visible-app count from the same pass the document's own page came from.
    The text report's "N of M" line needs a real total; reusing this count
    instead of collecting again keeps a `snapshot` call to one tree walk."""

    document: dict[str, Any]
    total_apps: int


def snapshot_document(
    backend: Backend, *, include_system: bool, limit: int, now: datetime
) -> SnapshotResult:
    """One sample of the machine and every app big enough to matter, capped
    at `limit` items and breadcrumbed to the largest one."""
    stats = backend.read_system()
    apps = _collect_visible_apps(backend, include_system)
    page = apps[:limit]

    document: dict[str, Any] = {
        "taken_at": now.isoformat(timespec="seconds"),
        "system": _system_dict(stats),
        "pressure": _pressure_dict(stats),
        "apps": {"items": [_app_item(backend, app) for app in page], "has_more": len(apps) > limit},
    }
    if page:
        first = page[0]
        next_call = ["appmem", "app", first.name]
        if first.scope == "system":
            next_call.extend(["--scope", "system"])
        document["next"] = next_call
    return SnapshotResult(document, len(apps))


# --- app -----------------------------------------------------------------------


def _process_item(proc: ProcStats, *, backend: Backend) -> dict[str, Any]:
    return {
        "pid": proc.pid,
        "name": proc.name,
        "swap_bytes": proc.swap,
        "ram_bytes": proc.ram,
        "total_bytes": proc.swap + proc.ram,
        "age_seconds": int(proc.age_seconds),
        "unit": proc.unit,
        "private_bytes": backend.private_bytes(proc.pid),
    }


def _command_item(command: CommandStats) -> dict[str, Any]:
    return {
        "name": command.name,
        "swap_bytes": command.swap,
        "ram_bytes": command.ram,
        "total_bytes": command.swap + command.ram,
        "procs": command.count,
    }


def paged[T](items: list[T], limit: int, to_item: Callable[[T], dict[str, Any]]) -> dict[str, Any]:
    page = items[:limit]
    return {"items": [to_item(item) for item in page], "has_more": len(items) > limit}


class AppResult(NamedTuple):
    """`app_document`'s return: the JSON document, plus the total process and
    command counts from the same read the document's own paged lists came
    from, so the text report's "N of M" lines need no second read."""

    document: dict[str, Any]
    total_processes: int
    total_commands: int


def app_document(
    backend: Backend, name: str, scope: str, *, limit: int, now: datetime
) -> AppResult:
    """One app's units, processes, commands grouped by name, and the
    kernel/unattributed remainder, exactly as the TUI's process view computes
    them for the same (scope, name)."""
    app = backend.find_app(scope, name, strict=True)
    if app is None:
        raise AppNotFoundError(name)
    procs = backend.read_procs(app)
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
        "zswapped_bytes": app.zswapped,
        "kernel_bytes": app.kernel,
        "zswap_pool_bytes": app.zswap_pool,
        "procs": app.procs,
        "units": [{"name": path.name, "label": unit_label(path.name)} for path in app.unit_paths],
        "processes": paged(
            processes_sorted, limit, lambda proc: _process_item(proc, backend=backend)
        ),
        "commands": paged(commands_sorted, limit, _command_item),
        "unattributed_ram_bytes": unattributed_ram,
        "unattributed_swap_bytes": unattributed_swap,
    }
    return AppResult(document, len(processes_sorted), len(commands_sorted))
