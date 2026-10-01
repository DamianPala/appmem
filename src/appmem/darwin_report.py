"""Darwin documents, separate from the stable Linux JSON contract."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from appmem.darwin_backend import DarwinApp, DarwinBackend, DarwinProcess


def _coverage(app: DarwinApp) -> dict[str, object]:
    return {
        "readable_processes": app.readable_processes,
        "unreadable_processes": app.unreadable_processes,
        "partial": app.partial,
        "grouping_partial": app.grouping_partial,
        "resident_readable_processes": app.resident_readable_processes,
        "resident_unreadable_processes": app.procs - app.resident_readable_processes,
        "resident_partial": app.resident_partial,
    }


def _app_item(app: DarwinApp) -> dict[str, object]:
    return {
        "id": app.id,
        "name": app.name,
        "footprint_bytes": app.footprint_bytes,
        "resident_bytes": app.resident_bytes,
        "procs": app.procs,
        "coverage": _coverage(app),
    }


def _process_item(process: DarwinProcess) -> dict[str, object]:
    return {
        "pid": process.pid,
        "start_abstime": process.start_abstime,
        "command": process.command,
        "footprint_bytes": process.footprint_bytes,
        "resident_bytes": process.resident_bytes,
        "unavailable": process.unavailable,
    }


def _commands(app: DarwinApp) -> list[dict[str, object]]:
    grouped: dict[str, list[DarwinProcess]] = defaultdict(list)
    for process in app.members:
        grouped[process.command].append(process)
    result: list[dict[str, object]] = []
    for name, members in grouped.items():
        known = [p.footprint_bytes for p in members if p.footprint_bytes is not None]
        residents = [p.resident_bytes for p in members if p.resident_bytes is not None]
        result.append(
            {
                "command": name,
                "footprint_bytes": sum(known) if known else None,
                "resident_bytes": sum(residents) if residents else None,
                "resident_readable_processes": len(residents),
                "resident_unreadable_processes": len(members) - len(residents),
                "procs": len(members),
                "readable_processes": len(known),
                "unreadable_processes": len(members) - len(known),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["footprint_bytes"] is None,
            -(item["footprint_bytes"] if isinstance(item["footprint_bytes"], int) else 0),
            str(item["command"]),
        ),
    )


def snapshot_document(
    backend: DarwinBackend, *, limit: int, now: datetime
) -> tuple[dict[str, object], int]:
    host = backend.read_system()
    apps = backend.collect_apps()
    pressure = {1: "normal", 2: "warning", 4: "critical"}.get(host.pressure_level or 0)
    document: dict[str, object] = {
        "taken_at": now.isoformat(),
        "platform": "darwin",
        "system": {
            "physical_bytes": host.physical_bytes,
            "free_bytes": host.free_bytes,
            "speculative_bytes": host.speculative_bytes,
            "file_backed_bytes": host.file_backed_bytes,
            "purgeable_bytes": host.purgeable_bytes,
            "used_excluding_file_backed_bytes": (
                host.ram_partition[0] if host.ram_partition is not None else None
            ),
            "free_excluding_speculative_bytes": (
                host.ram_partition[2] if host.ram_partition is not None else None
            ),
            "wired_bytes": host.wired_bytes,
            "compressor_physical_bytes": host.compressor_physical_bytes,
            "compressor_logical_bytes": host.compressor_logical_bytes,
            "swap_used_bytes": host.swap_used_bytes,
            "swap_total_bytes": host.swap_total_bytes,
        },
        "pressure": {
            "level": pressure,
            "source": "kern.memorystatus_vm_pressure_level",
            "unavailable": (
                str(host.pressure_unavailable or "unknown_level") if pressure is None else None
            ),
        },
        "apps": [_app_item(app) for app in apps[:limit]],
        "has_more": len(apps) > limit,
    }
    if len(apps) > limit:
        document["next"] = ["appmem", "snapshot", "--limit", str(len(apps))]
    return document, len(apps)


def app_document(
    backend: DarwinBackend, name: str, *, limit: int, now: datetime
) -> tuple[dict[str, object], int, int] | None:
    app = backend.find_app(name)
    if app is None:
        return None
    commands = _commands(app)
    document: dict[str, object] = {
        "taken_at": now.isoformat(),
        "platform": "darwin",
        "app": _app_item(app),
        "processes": [_process_item(p) for p in app.members[:limit]],
        "commands": commands[:limit],
        "has_more_processes": app.procs > limit,
        "has_more_commands": len(commands) > limit,
    }
    if app.procs > limit or len(commands) > limit:
        document["next"] = ["appmem", "app", app.id, "--limit", str(max(app.procs, len(commands)))]
    return document, app.procs, len(commands)


def render_snapshot_text(document: dict[str, Any]) -> str:
    system = document["system"]
    lines = [
        "macOS application footprints (experimental)",
        f"Physical {system['physical_bytes']} B  "
        f"Native free (incl. speculative) {system['free_bytes']} B  "
        f"Wired {system['wired_bytes']} B",
        f"Compressor physical {system['compressor_physical_bytes']} B  "
        f"Swap used {system['swap_used_bytes']} B / "
        f"currently allocated {system['swap_total_bytes']} B",
        f"Native pressure {document['pressure']['level'] or 'unavailable'}",
        "APP  MEMORY (bytes)  PROCS  COVERAGE",
    ]
    for app in document["apps"]:
        footprint = app["footprint_bytes"]
        coverage = app["coverage"]
        amount = "unknown" if footprint is None else str(footprint)
        label = "partial" if coverage["partial"] else "complete"
        lines.append(f"{app['name']}  {amount}  {app['procs']}  {label}")
    return "\n".join(lines)


def render_app_text(document: dict[str, Any]) -> str:
    app = document["app"]
    amount = "unknown" if app["footprint_bytes"] is None else str(app["footprint_bytes"])
    lines = [
        f"{app['name']} ({app['id']})  memory {amount} B",
        "PID  COMMAND  MEMORY (bytes)",
    ]
    for process in document["processes"]:
        value = process["footprint_bytes"]
        lines.append(
            f"{process['pid']}  {process['command']}  {'unknown' if value is None else value}"
        )
    lines.append("COMMAND  MEMORY (bytes)  PROCS")
    for command in document["commands"]:
        value = command["footprint_bytes"]
        lines.append(
            f"{command['command']}  {'unknown' if value is None else value}  {command['procs']}"
        )
    return "\n".join(lines)
