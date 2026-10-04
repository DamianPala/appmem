"""Pure Darwin row and growth calculations."""

from __future__ import annotations

from dataclasses import dataclass

from appmem.darwin_backend import DarwinApp


@dataclass(frozen=True)
class DarwinRow:
    key: str
    name: str
    footprint_bytes: int | None
    delta_bytes: int | None
    procs: int
    partial: bool
    resident_bytes: int | None = None
    resident_partial: bool = False
    compressed_bytes: int | None = None
    compressed_partial: bool = False


def _members(app: DarwinApp) -> set[tuple[int, int]]:
    return {
        (process.pid, process.start_abstime)
        for process in app.members
        if process.start_abstime is not None
    }


def update_baseline(apps: list[DarwinApp], baseline: dict[str, DarwinApp]) -> dict[str, DarwinApp]:
    """Drop vanished groups and reset a reused bundle path with no shared member."""
    updated: dict[str, DarwinApp] = {}
    for app in apps:
        previous = baseline.get(app.id)
        if (
            previous is None
            or previous.partial
            or previous.grouping_partial
            or not (_members(previous) & _members(app))
        ):
            updated[app.id] = app
        else:
            updated[app.id] = previous
    return updated


def build_rows(apps: list[DarwinApp], baseline: dict[str, DarwinApp]) -> list[DarwinRow]:
    rows: list[DarwinRow] = []
    for app in apps:
        previous = baseline.get(app.id)
        complete = not app.partial and not app.grouping_partial
        old_complete = (
            previous is not None and not previous.partial and not previous.grouping_partial
        )
        delta = None
        if (
            complete
            and old_complete
            and previous is not None
            and previous.footprint_bytes is not None
            and app.footprint_bytes is not None
        ):
            delta = app.footprint_bytes - previous.footprint_bytes
        rows.append(
            DarwinRow(
                key=app.id,
                name=app.name,
                footprint_bytes=app.footprint_bytes,
                delta_bytes=delta,
                procs=app.procs,
                partial=app.partial or app.grouping_partial,
                resident_bytes=app.resident_bytes,
                resident_partial=app.resident_partial or app.grouping_partial,
                compressed_bytes=app.compressed_bytes,
                compressed_partial=app.compressed_partial or app.grouping_partial,
            )
        )
    return rows


def sort_rows(rows: list[DarwinRow], key: str, reverse: bool) -> list[DarwinRow]:
    if key == "app":
        return sorted(rows, key=lambda row: (row.name.casefold(), row.key), reverse=reverse)
    attribute = {
        "footprint": "footprint_bytes",
        "delta": "delta_bytes",
        "resident": "resident_bytes",
        "compressed": "compressed_bytes",
        "procs": "procs",
    }[key]
    known = [row for row in rows if getattr(row, attribute) is not None]
    unknown = [row for row in rows if getattr(row, attribute) is None]
    # Stable sort twice: ties stay A to Z whichever way the values run.
    known.sort(key=lambda row: (row.name.casefold(), row.key))
    known.sort(key=lambda row: getattr(row, attribute), reverse=reverse)
    return known + sorted(unknown, key=lambda row: row.name.casefold())
