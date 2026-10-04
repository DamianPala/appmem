"""Darwin documents: the Linux envelope around macOS footprint metrics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from rich.cells import cell_len

from appmem.darwin_backend import DarwinApp, DarwinBackend, DarwinProcess
from appmem.fmt import format_pair, size
from appmem.render import escape_control_chars
from appmem.report import paged


class AppNotFoundError(Exception):
    """No current app matches the name or id; `candidates` are the near misses."""

    def __init__(self, name: str, candidates: list[str]) -> None:
        super().__init__(name)
        self.candidates = candidates


def _coverage(app: DarwinApp) -> dict[str, object]:
    return {
        "readable_processes": app.readable_processes,
        "unreadable_processes": app.unreadable_processes,
        "partial": app.partial,
        "grouping_partial": app.grouping_partial,
        "resident_readable_processes": app.resident_readable_processes,
        "resident_unreadable_processes": app.procs - app.resident_readable_processes,
        "resident_partial": app.resident_partial,
        "compressed_readable_processes": app.compressed_readable_processes,
        "compressed_unreadable_processes": app.procs - app.compressed_readable_processes,
        "compressed_partial": app.compressed_partial,
    }


def _app_item(app: DarwinApp) -> dict[str, object]:
    return {
        "id": app.id,
        "name": app.name,
        "footprint_bytes": app.footprint_bytes,
        "resident_bytes": app.resident_bytes,
        "compressed_bytes": app.compressed_bytes,
        "procs": app.procs,
        "coverage": _coverage(app),
    }


def _process_item(process: DarwinProcess) -> dict[str, object]:
    return {
        "pid": process.pid,
        "start_abstime": process.start_abstime,
        "name": process.command,
        "footprint_bytes": process.footprint_bytes,
        "resident_bytes": process.resident_bytes,
        "compressed_bytes": process.compressed_bytes,
        "unavailable": process.unavailable,
        "via": process.via,
    }


def _by_size(footprint: int | None, name: str, pid: int = 0) -> tuple[bool, int, str, str, int]:
    """Largest first, unreadable last, ties A to Z ignoring case, then by PID."""
    return (footprint is None, -(footprint or 0), name.casefold(), name, pid)


def _commands(app: DarwinApp) -> list[dict[str, object]]:
    grouped: dict[str, list[DarwinProcess]] = defaultdict(list)
    for process in app.members:
        grouped[process.command].append(process)
    result: list[dict[str, object]] = []
    for name, members in grouped.items():
        known = [p.footprint_bytes for p in members if p.footprint_bytes is not None]
        residents = [p.resident_bytes for p in members if p.resident_bytes is not None]
        squeezed = [p.compressed_bytes for p in members if p.compressed_bytes is not None]
        result.append(
            {
                "name": name,
                "footprint_bytes": sum(known) if known else None,
                "resident_bytes": sum(residents) if residents else None,
                "resident_readable_processes": len(residents),
                "resident_unreadable_processes": len(members) - len(residents),
                "compressed_bytes": sum(squeezed) if squeezed else None,
                "compressed_readable_processes": len(squeezed),
                "compressed_unreadable_processes": len(members) - len(squeezed),
                "procs": len(members),
                "readable_processes": len(known),
                "unreadable_processes": len(members) - len(known),
            }
        )
    return sorted(
        result,
        key=lambda item: _by_size(
            item["footprint_bytes"] if isinstance(item["footprint_bytes"], int) else None,
            str(item["name"]),
        ),
    )


def snapshot_document(
    backend: DarwinBackend, *, limit: int, now: datetime
) -> tuple[dict[str, object], int]:
    host = backend.read_system()
    apps = backend.collect_apps()
    pressure = {1: "normal", 2: "warning", 4: "critical"}.get(host.pressure_level or 0)
    page = apps[:limit]
    document: dict[str, object] = {
        "taken_at": now.isoformat(timespec="seconds"),
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
        "apps": paged(apps, limit, _app_item),
    }
    if page:
        document["next"] = ["appmem", "app", page[0].id]
    return document, len(apps)


def app_document(
    backend: DarwinBackend, name: str, *, limit: int, now: datetime
) -> tuple[dict[str, object], int, int]:
    """One app, flat like the Linux document: its own fields, then paged processes
    and commands. Raises AppNotFoundError when nothing matches."""
    app, near = backend.lookup(name)
    if app is None:
        raise AppNotFoundError(name, [candidate.name for candidate in near[:5]])
    commands = _commands(app)
    processes = sorted(app.members, key=lambda p: _by_size(p.footprint_bytes, p.command, p.pid))
    document: dict[str, object] = {
        "taken_at": now.isoformat(timespec="seconds"),
        "platform": "darwin",
        **_app_item(app),
        "processes": paged(processes, limit, _process_item),
        "commands": paged(commands, limit, lambda item: item),
    }
    return document, app.procs, len(commands)


def _text_amount(value: object, *, partial: bool = False) -> str:
    if type(value) is not int or value < 0:
        return "unknown"
    return size(value) + ("*" if partial else "")


def _partial(coverage: dict[str, Any], metric: str) -> bool:
    """The live view's rule: a metric is partial when its own reads were, or when
    the app's grouping was (a member may be missing from the group)."""
    return bool(coverage[metric] or coverage["grouping_partial"])


def _text_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [
        max(cell_len(header), *(cell_len(row[i]) for row in rows)) if rows else cell_len(header)
        for i, header in enumerate(headers)
    ]

    def line(row: tuple[str, ...]) -> str:
        return "  ".join(
            value + " " * (width - cell_len(value))
            if headers[i] in ("APP", "COMMAND", "VIA")
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
        "macOS application footprints",
        f"RAM       {ram}",
        f"Compress  {compression}",
        f"Swap      {swap}",
        f"Pressure  {document['pressure']['level'] or 'unavailable'}",
        "",
    ]
    rows = [
        (
            escape_control_chars(app["name"]),
            _text_amount(app["footprint_bytes"], partial=_partial(app["coverage"], "partial")),
            _text_amount(
                app["compressed_bytes"], partial=_partial(app["coverage"], "compressed_partial")
            ),
            _text_amount(
                app["resident_bytes"], partial=_partial(app["coverage"], "resident_partial")
            ),
            str(app["procs"]),
            "partial" if _partial(app["coverage"], "partial") else "complete",
        )
        for app in document["apps"]["items"]
    ]
    lines.extend(
        _text_table(("APP", "MEMORY", "COMPRESSED", "RESIDENT", "PROCS", "COVERAGE"), rows)
    )
    lines.extend(_legend(rows))
    return "\n".join(lines)


def _legend(rows: list[tuple[str, ...]]) -> list[str]:
    """The footnote only when a shown amount is partial or unknown."""
    if any(cell.endswith("*") or cell == "unknown" for row in rows for cell in row[1:4]):
        return ["* partial known sum; unknown = unavailable for that metric"]
    return []


def render_app_text(document: dict[str, Any]) -> str:
    coverage = "partial" if _partial(document["coverage"], "partial") else "complete"
    lines = [
        f"{escape_control_chars(document['name'])} ({escape_control_chars(document['id'])})  "
        f"memory {_text_amount(document['footprint_bytes'])}  {coverage}"
    ]
    process_rows = [
        (
            str(p["pid"]),
            escape_control_chars(p["name"]),
            _text_amount(p["footprint_bytes"]),
            _text_amount(p["compressed_bytes"]),
            _text_amount(p["resident_bytes"]),
            p["via"],
        )
        for p in document["processes"]["items"]
    ]
    lines.extend(
        _text_table(("PID", "COMMAND", "MEMORY", "COMPRESSED", "RESIDENT", "VIA"), process_rows)
    )
    command_rows = [
        (
            escape_control_chars(c["name"]),
            _text_amount(c["footprint_bytes"], partial=bool(c["unreadable_processes"])),
            _text_amount(c["compressed_bytes"], partial=bool(c["compressed_unreadable_processes"])),
            _text_amount(c["resident_bytes"], partial=bool(c["resident_unreadable_processes"])),
            str(c["procs"]),
            "partial" if c["unreadable_processes"] else "complete",
        )
        for c in document["commands"]["items"]
    ]
    lines.extend(
        _text_table(
            ("COMMAND", "MEMORY", "COMPRESSED", "RESIDENT", "PROCS", "COVERAGE"), command_rows
        )
    )
    # Process rows lead with a PID, so their amounts sit one column to the right.
    lines.extend(_legend([row[1:] for row in process_rows] + command_rows))
    return "\n".join(lines)
