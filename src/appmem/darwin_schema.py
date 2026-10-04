"""Darwin CLI introspection. Linux descriptors and output schemas stay untouched."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from appmem import __version__, schema
from appmem.darwin_backend import VIA_RULES


def _field(type_name: str | list[str], description: str) -> dict[str, object]:
    return {"type": type_name, "description": description}


_TAKEN_AT = "When this sample was taken, RFC 3339 with the local UTC offset."


def _paged(item: Mapping[str, object], items_description: str, more: str) -> dict[str, object]:
    return {
        "type": "object",
        "description": items_description,
        "required": ["items", "has_more"],
        "properties": {
            "items": {"type": "array", "description": "The page of items.", "items": item},
            "has_more": _field("boolean", more),
        },
    }


_COMPRESSED = _field(
    ["integer", "null"],
    "Bytes of readable members held by the compressor, in RAM or swapped out, at their "
    "uncompressed size; already part of footprint_bytes, not on top of it; null if none readable",
)

_COVERAGE = {
    "type": "object",
    "description": (
        "Independent footprint, resident, compressed and grouping coverage for captured "
        "member processes"
    ),
    "required": [
        "readable_processes",
        "unreadable_processes",
        "resident_readable_processes",
        "resident_unreadable_processes",
        "compressed_readable_processes",
        "compressed_unreadable_processes",
        "partial",
        "grouping_partial",
        "resident_partial",
        "compressed_partial",
    ],
    "properties": {
        "compressed_partial": _field("boolean", "Some member compressed values are unavailable"),
        "compressed_readable_processes": _field(
            "integer", "Members with readable compressed bytes"
        ),
        "compressed_unreadable_processes": _field(
            "integer", "Members with unavailable compressed bytes"
        ),
        "resident_partial": _field("boolean", "Some member resident values are unavailable"),
        "resident_readable_processes": _field("integer", "Members with readable resident bytes"),
        "resident_unreadable_processes": _field(
            "integer", "Members with unavailable resident bytes"
        ),
        "readable_processes": _field("integer", "Processes with a readable footprint"),
        "unreadable_processes": _field("integer", "Processes whose footprint is unavailable"),
        "partial": _field("boolean", "Some process footprints are unavailable"),
        "grouping_partial": _field("boolean", "Ancestry was missing or cyclic"),
    },
}
_APP = {
    "type": "object",
    "description": "One application group and its known footprint",
    "required": [
        "id",
        "name",
        "footprint_bytes",
        "resident_bytes",
        "compressed_bytes",
        "procs",
        "coverage",
    ],
    "properties": {
        "compressed_bytes": _COMPRESSED,
        "resident_bytes": _field(
            ["integer", "null"],
            "Resident bytes of readable members; shared/file-backed pages may double count; "
            "not additive with footprint and their difference is not swap",
        ),
        "id": _field("string", "Stable identity of the bundle path or session root"),
        "name": _field(
            "string",
            "Bundle name, or the executable name for apps without a bundle (same-named roots are "
            "one app); two bundles with one name add their bundle id in parentheses",
        ),
        "footprint_bytes": _field(
            ["integer", "null"],
            "Sum of readable process physical footprints; null if none readable",
        ),
        "procs": _field("integer", "Captured member process count"),
        "coverage": _COVERAGE,
    },
}
_PROCESS = {
    "type": "object",
    "description": "One captured process, identified by PID and native start time where available",
    "required": [
        "pid",
        "start_abstime",
        "name",
        "footprint_bytes",
        "resident_bytes",
        "compressed_bytes",
        "unavailable",
        "via",
    ],
    "properties": {
        "compressed_bytes": _field(
            ["integer", "null"],
            "Bytes of the process held by the compressor, in RAM or swapped out, at their "
            "uncompressed size; already part of footprint_bytes; null when unreadable",
        ),
        "resident_bytes": _field(
            ["integer", "null"],
            "Resident bytes of readable members; shared/file-backed pages may double count; "
            "not additive with footprint and their difference is not swap",
        ),
        "via": {
            "type": "string",
            "enum": list(VIA_RULES),
            "description": (
                "The rule that placed the process in this app: bundle (its executable is "
                "inside the app's .app), ancestry (a parent is), responsible (launchd started "
                "it and macOS holds the app responsible for it) or root (no app: grouped by "
                "its topmost process's name)"
            ),
        },
        "pid": _field("integer", "Process ID"),
        "start_abstime": _field(["integer", "null"], "Native process start identity, if readable"),
        "name": _field("string", "Executable file name from its path, never arguments"),
        "footprint_bytes": _field(["integer", "null"], "Physical footprint, null when unreadable"),
        "unavailable": _field(["string", "null"], "Reason the footprint is unavailable"),
    },
}
_COMMAND = {
    "type": "object",
    "description": "Captured processes grouped by executable name",
    "required": [
        "name",
        "footprint_bytes",
        "resident_bytes",
        "procs",
        "readable_processes",
        "unreadable_processes",
        "resident_readable_processes",
        "resident_unreadable_processes",
        "compressed_bytes",
        "compressed_readable_processes",
        "compressed_unreadable_processes",
    ],
    "properties": {
        "compressed_bytes": _COMPRESSED,
        "compressed_readable_processes": _field(
            "integer", "Members with readable compressed bytes"
        ),
        "compressed_unreadable_processes": _field(
            "integer", "Members with unavailable compressed bytes"
        ),
        "resident_readable_processes": _field("integer", "Members with readable resident bytes"),
        "resident_unreadable_processes": _field(
            "integer", "Members with unavailable resident bytes"
        ),
        "resident_bytes": _field(
            ["integer", "null"],
            "Resident bytes of readable members; shared/file-backed pages may double count; "
            "not additive with footprint and their difference is not swap",
        ),
        "name": _field("string", "Executable file name from its path"),
        "footprint_bytes": _field(["integer", "null"], "Sum of readable member footprints"),
        "procs": _field("integer", "Captured process count"),
        "readable_processes": _field("integer", "Members with readable footprints"),
        "unreadable_processes": _field("integer", "Members with unavailable footprints"),
    },
}


USER_SCOPE = ("user",)


def _user_scope() -> dict[str, object]:
    result = schema.APP_SCOPE.to_dict()
    result["description"] = "Only the current user's processes are visible on macOS"
    result["enum"] = list(USER_SCOPE)
    return result


def _snapshot_output() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["taken_at", "platform", "system", "pressure", "apps"],
        "properties": {
            "taken_at": _field("string", _TAKEN_AT),
            "platform": {
                "type": "string",
                "enum": ["darwin"],
                "description": "Darwin data contract",
            },
            "system": {
                "type": "object",
                "description": (
                    "Native host counters and validated derived partition, "
                    "not a sum of app footprints"
                ),
                "required": [
                    "physical_bytes",
                    "free_bytes",
                    "speculative_bytes",
                    "file_backed_bytes",
                    "purgeable_bytes",
                    "used_excluding_file_backed_bytes",
                    "free_excluding_speculative_bytes",
                    "wired_bytes",
                    "compressor_physical_bytes",
                    "compressor_logical_bytes",
                    "swap_in_bytes",
                    "swap_out_bytes",
                    "swap_used_bytes",
                    "swap_total_bytes",
                ],
                "properties": {
                    name: _field("integer", description)
                    for name, description in {
                        "physical_bytes": "Installed physical memory",
                        "free_bytes": (
                            "Native free pages including speculative; not available memory"
                        ),
                        "wired_bytes": "Wired physical pages",
                        "compressor_physical_bytes": "Physical memory occupied by the compressor",
                        "compressor_logical_bytes": "Logical bytes represented by compressed pages",
                        "swap_used_bytes": ("Global swap bytes used within the current allocation"),
                        "swap_total_bytes": (
                            "Currently allocated swap space, grows dynamically; zero means "
                            "unallocated"
                        ),
                    }.items()
                }
                | {
                    name: _field(["integer", "null"], description)
                    for name, description in {
                        "swap_in_bytes": (
                            "Lifetime host_statistics64 swapins times "
                            "host_page_size: page-rounded compressed segment "
                            "bytes read from swap files, including housekeeping; "
                            "null when unavailable."
                        ),
                        "swap_out_bytes": (
                            "Lifetime host_statistics64 swapouts times "
                            "host_page_size: page-rounded compressed segment "
                            "bytes written to swap files, including housekeeping; "
                            "not logical app bytes or SSD throughput; null when "
                            "unavailable."
                        ),
                        "speculative_bytes": (
                            "Native speculative pages, included in free and file-backed"
                        ),
                        "file_backed_bytes": "Native external pages; not all immediately available",
                        "purgeable_bytes": "Purgeable pages overlapping used, not an extra bucket",
                        "used_excluding_file_backed_bytes": (
                            "Physical minus true free minus file-backed; includes "
                            "reserved/unaccounted; null if inconsistent"
                        ),
                        "free_excluding_speculative_bytes": (
                            "Native free minus speculative; null if partition counters are "
                            "inconsistent"
                        ),
                    }.items()
                },
            },
            "pressure": {
                "type": "object",
                "description": "Native kernel pressure state; not Linux PSI",
                "required": ["level", "source", "unavailable"],
                "properties": {
                    "level": _field(["string", "null"], "Native normal, warning or critical state"),
                    "source": _field("string", "Native pressure sysctl name"),
                    "unavailable": _field(
                        ["string", "null"], "Read failure or unrecognized level, if any"
                    ),
                },
            },
            "apps": _paged(
                _APP,
                "Captured user application groups, largest footprint first, ties by name",
                "true when more apps exist than limit allowed through.",
            ),
            "next": {
                "type": "array",
                "description": (
                    "The argv to run appmem app on the first (largest) item; omitted when "
                    "there are no items, repeats --json when the call passed it."
                ),
                "items": _field("string", "One command argument"),
            },
        },
    }


def _app_output() -> dict[str, object]:
    properties = cast("dict[str, object]", _APP["properties"])
    return {
        "type": "object",
        "required": [
            "taken_at",
            "platform",
            *cast("list[str]", _APP["required"]),
            "processes",
            "commands",
        ],
        "properties": {
            "taken_at": _field("string", _TAKEN_AT),
            "platform": {
                "type": "string",
                "enum": ["darwin"],
                "description": "Darwin data contract",
            },
            **properties,
            "processes": _paged(
                _PROCESS,
                "Captured member processes, largest footprint first, ties by name then pid",
                "true when more processes exist than limit allowed through.",
            ),
            "commands": _paged(
                _COMMAND,
                "Processes summed by executable name, largest footprint first, ties by name",
                "true when more commands exist than limit allowed through.",
            ),
        },
    }


DARWIN_ERROR_KINDS: tuple[str, ...] = (
    "invalid_input",
    "terminal_required",
    "not_found",
    "interrupted",
    "platform_unavailable",
    "read_failed",
)


def index() -> dict[str, object]:
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "platform": "darwin",
        "tool_version": __version__,
        "conformance": {
            "name": "cli-design-standard",
            "standard": schema.CONFORMANCE_STANDARD_VERSION,
            "extensions": [],
        },
        "global_flags": [schema.JSON_FLAG.to_dict()],
        "format_defaults": dict(schema.FORMAT_DEFAULTS),
        "exit_codes": {
            "0": "success",
            "1": (
                "runtime failure (unsupported platform, native read failed and may succeed on "
                "retry, app not found, or no terminal)"
            ),
            "2": "usage error",
            "130": "interrupted by SIGINT",
            "143": "terminated by SIGTERM",
        },
        "error_kinds": schema.error_kinds(DARWIN_ERROR_KINDS),
        "commands": [
            {
                "name": "",
                "description": "Live Apple Silicon footprint view",
                "effects": "read_only",
            },
            {
                "name": "snapshot",
                "description": "Host memory and app footprints",
                "effects": "read_only",
            },
            {
                "name": "app",
                "description": "One app's process and command footprints",
                "effects": "read_only",
            },
        ],
        "command": root_detail(),
    }


def root_detail() -> dict[str, object]:
    return {
        "name": "",
        "description": "Open the live app footprint view on macOS 15+ Apple Silicon",
        "args": [],
        "flags": [
            schema.ROOT_INTERVAL.to_dict(),
            schema.ROOT_THEME.to_dict(),
        ],
        "effects": "read_only",
        "confirm": False,
        "interactive": True,
    }


def detail(path: list[str]) -> dict[str, object] | None:
    if path == ["snapshot"]:
        return {
            "name": "snapshot",
            "description": "One native host and app footprint sample",
            "args": [],
            "flags": [schema.SNAPSHOT_LIMIT.to_dict()],
            "effects": "read_only",
            "confirm": False,
            "interactive": False,
            "output": _snapshot_output(),
            "output_description": (
                "Physical footprint sums may be partial; host values include native counters "
                "and a validated derived RAM partition"
            ),
        }
    if path == ["app"]:
        return {
            "name": "app",
            "description": "One app's captured process and command footprints",
            "args": [
                {**schema.APP_NAME.to_dict(), "description": "Stable app id or displayed name"}
            ],
            "flags": [_user_scope(), schema.APP_LIMIT.to_dict()],
            "effects": "read_only",
            "confirm": False,
            "interactive": False,
            "output": _app_output(),
            "output_description": (
                "Use the stable app id or display name; only user scope is supported. "
                "processes.items and commands.items are sorted by footprint_bytes descending, "
                "unreadable last"
            ),
        }
    return None
