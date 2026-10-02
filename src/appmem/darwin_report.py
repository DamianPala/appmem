"""Darwin documents, separate from the stable Linux JSON contract."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from rich.cells import cell_len

from appmem.darwin_backend import DarwinApp, DarwinBackend, DarwinProcess
from appmem.fmt import format_pair, size
from appmem.render import escape_control_chars


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
            "swap_in_bytes": host.swap_in_bytes,
            "swap_out_bytes": host.swap_out_bytes,
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


def _text_amount(value: object, *, partial: bool = False) -> str:
    if type(value) is not int or value < 0:
        return "unknown"
    return size(value) + ("*" if partial else "")


def _text_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [
        max(cell_len(header), *(cell_len(row[i]) for row in rows)) if rows else cell_len(header)
        for i, header in enumerate(headers)
    ]

    def line(row: tuple[str, ...]) -> str:
        return "  ".join(
            value + " " * (width - cell_len(value))
            if headers[i] in ("APP", "COMMAND")
            else " " * (width - cell_len(value)) + value
            for i, (value, width) in enumerate(zip(row, widths, strict=True))
        ).rstrip()

    return [line(headers), *(line(row) for row in rows)]


def render_snapshot_text(document: dict[str, Any]) -> str:
    system = document["system"]
    used = system["used_excluding_file_backed_bytes"]
    total = system["physical_bytes"]
    ram = (
        format_pair(used, total) + " used"
        if type(used) is int and type(total) is int and total > 0
        else f"used unavailable; total {_text_amount(total)}"
    )
    ram += f" ({_text_amount(system['wired_bytes'])} wired"
    if system["purgeable_bytes"] is not None:
        ram += f", {_text_amount(system['purgeable_bytes'])} purgeable"
    ram += (
        f")  file-backed {_text_amount(system['file_backed_bytes'])}"
        f"  free {_text_amount(system['free_excluding_speculative_bytes'])}"
    )
    logical, physical = system["compressor_logical_bytes"], system["compressor_physical_bytes"]
    compression = f"{_text_amount(logical)} data -> {_text_amount(physical)} RAM"
    if type(logical) is int and type(physical) is int and logical > 0 and physical > 0:
        compression += f"  ({logical / physical:.1f}:1)"
    swap_used, allocated = system["swap_used_bytes"], system["swap_total_bytes"]
    if type(swap_used) is not int or type(allocated) is not int or not 0 <= swap_used <= allocated:
        swap = "unavailable"
    elif allocated == 0:
        swap = "0 B used; not allocated"
    else:
        swap = format_pair(swap_used, allocated) + " used/allocated now"
    lines = [
        "macOS application footprints (experimental)",
        f"RAM       {ram}",
        f"Compress  {compression}",
        f"Swap      {swap}",
        f"Pressure  {document['pressure']['level'] or 'unavailable'}  current user",
        "",
    ]
    rows = [
        (
            escape_control_chars(app["name"]),
            _text_amount(app["footprint_bytes"], partial=app["coverage"]["partial"]),
            _text_amount(app["resident_bytes"], partial=app["coverage"]["resident_partial"]),
            str(app["procs"]),
            "partial" if app["coverage"]["partial"] else "complete",
        )
        for app in document["apps"]
    ]
    lines.extend(_text_table(("APP", "MEMORY", "RESIDENT", "PROCS", "COVERAGE"), rows))
    lines.append("* partial known sum; unknown = unavailable for that metric")
    return "\n".join(lines)


def render_app_text(document: dict[str, Any]) -> str:
    app = document["app"]
    coverage = "partial" if app["coverage"]["partial"] else "complete"
    lines = [
        f"{escape_control_chars(app['name'])} ({escape_control_chars(app['id'])})  "
        f"memory {_text_amount(app['footprint_bytes'])}  {coverage}"
    ]
    rows = [
        (
            str(p["pid"]),
            escape_control_chars(p["command"]),
            _text_amount(p["footprint_bytes"]),
            _text_amount(p["resident_bytes"]),
        )
        for p in document["processes"]
    ]
    lines.extend(_text_table(("PID", "COMMAND", "MEMORY", "RESIDENT"), rows))
    rows = [
        (
            escape_control_chars(c["command"]),
            _text_amount(c["footprint_bytes"], partial=bool(c["unreadable_processes"])),
            _text_amount(c["resident_bytes"], partial=bool(c["resident_unreadable_processes"])),
            str(c["procs"]),
            "partial" if c["unreadable_processes"] else "complete",
        )
        for c in document["commands"]
    ]
    lines.extend(_text_table(("COMMAND", "MEMORY", "RESIDENT", "PROCS", "COVERAGE"), rows))
    lines.append("* partial known sum; unknown = unavailable for that metric")
    return "\n".join(lines)
