"""Darwin CLI introspection. Linux descriptors and output schemas stay untouched."""

from __future__ import annotations

from appmem import __version__, schema


def _field(type_name: str | list[str], description: str) -> dict[str, object]:
    return {"type": type_name, "description": description}


_COVERAGE = {
    "type": "object",
    "description": (
        "Independent footprint/resident and grouping coverage for captured member processes"
    ),
    "required": [
        "readable_processes",
        "unreadable_processes",
        "resident_readable_processes",
        "resident_unreadable_processes",
        "partial",
        "grouping_partial",
        "resident_partial",
    ],
    "properties": {
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
    "required": ["id", "name", "footprint_bytes", "resident_bytes", "procs", "coverage"],
    "properties": {
        "resident_bytes": _field(
            ["integer", "null"],
            "Resident bytes of readable members; shared/file-backed pages may double count; "
            "not additive with footprint and their difference is not swap",
        ),
        "id": _field("string", "Stable identity of the bundle path or session root"),
        "name": _field("string", "Display name; same-named independent roots are disambiguated"),
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
        "command",
        "footprint_bytes",
        "resident_bytes",
        "unavailable",
    ],
    "properties": {
        "resident_bytes": _field(
            ["integer", "null"],
            "Resident bytes of readable members; shared/file-backed pages may double count; "
            "not additive with footprint and their difference is not swap",
        ),
        "pid": _field("integer", "Process ID"),
        "start_abstime": _field(["integer", "null"], "Native process start identity, if readable"),
        "command": _field("string", "Short BSD executable name, never arguments"),
        "footprint_bytes": _field(["integer", "null"], "Physical footprint, null when unreadable"),
        "unavailable": _field(["string", "null"], "Reason the footprint is unavailable"),
    },
}
_COMMAND = {
    "type": "object",
    "description": "Captured processes grouped by short executable name",
    "required": [
        "command",
        "footprint_bytes",
        "resident_bytes",
        "procs",
        "readable_processes",
        "unreadable_processes",
        "resident_readable_processes",
        "resident_unreadable_processes",
    ],
    "properties": {
        "resident_readable_processes": _field("integer", "Members with readable resident bytes"),
        "resident_unreadable_processes": _field(
            "integer", "Members with unavailable resident bytes"
        ),
        "resident_bytes": _field(
            ["integer", "null"],
            "Resident bytes of readable members; shared/file-backed pages may double count; "
            "not additive with footprint and their difference is not swap",
        ),
        "command": _field("string", "Short BSD executable name"),
        "footprint_bytes": _field(["integer", "null"], "Sum of readable member footprints"),
        "procs": _field("integer", "Captured process count"),
        "readable_processes": _field("integer", "Members with readable footprints"),
        "unreadable_processes": _field("integer", "Members with unavailable footprints"),
    },
}


def _unsupported_system(flag: schema.Flag) -> dict[str, object]:
    result = flag.to_dict()
    result["description"] = "Unsupported on macOS; returns invalid_input"
    return result


def _user_scope() -> dict[str, object]:
    result = schema.APP_SCOPE.to_dict()
    result["description"] = "Only user scope is available; system returns invalid_input"
    return result


def _snapshot_output() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["taken_at", "platform", "system", "pressure", "apps", "has_more"],
        "properties": {
            "taken_at": _field("string", "Sample time in ISO 8601 format"),
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
            "apps": {
                "type": "array",
                "description": "Captured user application groups",
                "items": _APP,
            },
            "has_more": _field("boolean", "More app items exist than the limit"),
            "next": {
                "type": "array",
                "description": "Command arguments to retrieve all application groups",
                "items": _field("string", "One command argument"),
            },
        },
    }


def _app_output() -> dict[str, object]:
    return {
        "type": "object",
        "required": [
            "taken_at",
            "platform",
            "app",
            "processes",
            "commands",
            "has_more_processes",
            "has_more_commands",
        ],
        "properties": {
            "taken_at": _field("string", "Sample time in ISO 8601 format"),
            "platform": {
                "type": "string",
                "enum": ["darwin"],
                "description": "Darwin data contract",
            },
            "app": _APP,
            "processes": {
                "type": "array",
                "description": "Captured member processes, limited to the requested count",
                "items": _PROCESS,
            },
            "commands": {
                "type": "array",
                "description": "Command aggregates from captured members, limited by --limit",
                "items": _COMMAND,
            },
            "has_more_processes": _field("boolean", "More process items exist than the limit"),
            "has_more_commands": _field("boolean", "More command items exist than the limit"),
            "next": {
                "type": "array",
                "description": "Command arguments to retrieve all processes and commands",
                "items": _field("string", "One command argument"),
            },
        },
    }


def index() -> dict[str, object]:
    return {
        "schema_version": schema.SCHEMA_VERSION,
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
            "1": "runtime failure (platform unavailable, app not found, or no terminal)",
            "2": "usage error",
            "130": "interrupted by SIGINT",
            "143": "terminated by SIGTERM",
        },
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
            _unsupported_system(schema.ROOT_SYSTEM),
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
            "flags": [_unsupported_system(schema.SNAPSHOT_SYSTEM), schema.SNAPSHOT_LIMIT.to_dict()],
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
                "Use the stable app id or display name; only user scope is supported"
            ),
        }
    return None
