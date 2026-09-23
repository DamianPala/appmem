"""Descriptors for every argument and flag, and the introspection builders.

An agent that never reads the source still needs to know exactly what
`appmem` accepts and returns. These tables are that description, kept in one
place so `cli.py` can build its argparse parsers from them (a flag with no
entry here has nothing to add itself to), and so `appmem schema` can hand the
same facts back as JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from appmem import __version__

SCHEMA_VERSION = "1"
CONFORMANCE_STANDARD_VERSION = "0.1.0"

_NO_DEFAULT = object()
"""Marks a descriptor with no built-in default, distinct from a real default
of `None`, `False`, or `0`."""


@dataclass(frozen=True)
class Arg:
    """One positional argument or flag, following the input-descriptor shape
    every accepted input must publish."""

    name: str
    description: str
    type: str
    required: bool
    default: object = _NO_DEFAULT
    enum: tuple[str, ...] | None = None
    variadic: bool = False

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "required": self.required,
        }
        if self.default is not _NO_DEFAULT:
            data["default"] = self.default
        if self.enum is not None:
            data["enum"] = list(self.enum)
        if self.variadic:
            data["variadic"] = True
        return data


@dataclass(frozen=True)
class Flag(Arg):
    """Same shape as `Arg`, plus the alternate names a flag alone can have."""

    aliases: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        data = super().to_dict()
        if self.aliases:
            data["aliases"] = list(self.aliases)
        return data


# --- global ------------------------------------------------------------------

JSON_FLAG = Flag(
    name="json",
    description=(
        "Write JSON to stdout; the default when stdout is not a terminal. On the "
        "live view (bare appmem) it is an error: the view needs a terminal."
    ),
    type="boolean",
    required=False,
    default=False,
)

# --- root (unnamed) command ---------------------------------------------------

ROOT_INTERVAL = Flag(
    name="interval",
    description=(
        "Refresh interval in seconds, a number >= 0.2; only the live view refreshes, "
        "with a named command it is a usage error"
    ),
    type="number",
    required=False,
    default=1,
    aliases=("i",),
)
ROOT_SYSTEM = Flag(
    name="system",
    description=(
        "Start with system services shown (same as pressing x); before snapshot it is "
        "snapshot's own --system, with app or schema it is a usage error"
    ),
    type="boolean",
    required=False,
    default=False,
)
ROOT_DESCRIPTION = (
    "Open the live per-app RAM and swap view. Starts only in a terminal context: "
    "stdin and stdout are TTYs, --json is absent and NO_INPUT is unset; otherwise "
    "fails with kind terminal_required before reading anything and points to "
    "appmem snapshot."
)

# --- snapshot ------------------------------------------------------------------

SNAPSHOT_SYSTEM = Flag(
    name="system",
    description="Also include system services (system.slice units) as items with scope system",
    type="boolean",
    required=False,
    default=False,
)
SNAPSHOT_LIMIT = Flag(
    name="limit",
    description="Maximum number of app items, an integer >= 1; has_more reports a cut",
    type="integer",
    required=False,
    default=50,
)
SNAPSHOT_DESCRIPTION = (
    "One sample of the machine (RAM, swap, pressure, system services, elsewhere) "
    "and every app with at least 1 MiB of RAM plus swap, sorted by total descending."
)
SNAPSHOT_OUTPUT_DESCRIPTION = (
    "Apps with total_bytes >= 1048576, sorted by total_bytes descending, then scope, "
    "then name. pressure is null when /proc/pressure/memory is missing; elsewhere_bytes "
    "is null when the root memory.stat is missing. next names the first item "
    "(appmem app NAME [--scope system]) and is omitted when there are no items; it "
    "repeats --json when the call passed it. Failures return no result."
)

SNAPSHOT_OUTPUT: dict[str, object] = {
    "type": "object",
    "required": ["taken_at", "system", "pressure", "apps"],
    "properties": {
        "taken_at": {"type": "string"},
        "system": {
            "type": "object",
            "required": [
                "ram_total_bytes",
                "ram_used_bytes",
                "ram_available_bytes",
                "swap_total_bytes",
                "swap_used_bytes",
                "system_services_ram_bytes",
                "system_services_swap_bytes",
                "elsewhere_bytes",
            ],
            "properties": {
                "ram_total_bytes": {"type": "integer"},
                "ram_used_bytes": {"type": "integer"},
                "ram_available_bytes": {"type": "integer"},
                "swap_total_bytes": {"type": "integer"},
                "swap_used_bytes": {"type": "integer"},
                "system_services_ram_bytes": {"type": "integer"},
                "system_services_swap_bytes": {"type": "integer"},
                "elsewhere_bytes": {"type": ["integer", "null"]},
            },
        },
        "pressure": {
            "type": ["object", "null"],
            "required": [
                "level",
                "some_avg10_percent",
                "some_avg60_percent",
                "full_avg10_percent",
                "full_avg60_percent",
            ],
            "properties": {
                "level": {"type": "string", "enum": ["none", "some", "high"]},
                "some_avg10_percent": {"type": "number"},
                "some_avg60_percent": {"type": "number"},
                "full_avg10_percent": {"type": "number"},
                "full_avg60_percent": {"type": "number"},
            },
        },
        "apps": {
            "type": "object",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "name",
                            "scope",
                            "ram_bytes",
                            "swap_bytes",
                            "total_bytes",
                            "cache_bytes",
                            "procs",
                            "units",
                        ],
                        "properties": {
                            "name": {"type": "string"},
                            "scope": {"type": "string", "enum": ["user", "system"]},
                            "ram_bytes": {"type": "integer"},
                            "swap_bytes": {"type": "integer"},
                            "total_bytes": {"type": "integer"},
                            "cache_bytes": {"type": "integer"},
                            "procs": {"type": "integer"},
                            "units": {"type": "integer"},
                        },
                    },
                },
                "has_more": {"type": "boolean"},
            },
        },
        "next": {"type": "array", "items": {"type": "string"}},
    },
}

# --- app -------------------------------------------------------------------

APP_NAME = Arg(
    name="name",
    description="App name exactly as appmem snapshot lists it",
    type="string",
    required=True,
)
APP_SCOPE = Flag(
    name="scope",
    description="Scope the name belongs to; system services need system",
    type="string",
    required=False,
    default="user",
    enum=("user", "system"),
)
APP_LIMIT = Flag(
    name="limit",
    description=(
        "Maximum number of process items and, separately, of command items; an integer >= 1"
    ),
    type="integer",
    required=False,
    default=100,
)
APP_DESCRIPTION = (
    "One app's identity, units, processes, commands (processes summed by name) "
    "and the kernel / unattributed remainder."
)
APP_OUTPUT_DESCRIPTION = (
    "processes.items sorted by total_bytes descending, ties by name then pid; "
    "commands.items computed over all processes, sorted by total_bytes descending, "
    "ties by name. units lists every unit directory name of the app, raw. "
    "kernel_bytes is inside ram_bytes. unattributed_* are clamped at 0. "
    "Failures return no result."
)

APP_OUTPUT: dict[str, object] = {
    "type": "object",
    "required": [
        "taken_at",
        "name",
        "scope",
        "ram_bytes",
        "swap_bytes",
        "total_bytes",
        "cache_bytes",
        "kernel_bytes",
        "procs",
        "units",
        "processes",
        "commands",
        "unattributed_ram_bytes",
        "unattributed_swap_bytes",
    ],
    "properties": {
        "taken_at": {"type": "string"},
        "name": {"type": "string"},
        "scope": {"type": "string", "enum": ["user", "system"]},
        "ram_bytes": {"type": "integer"},
        "swap_bytes": {"type": "integer"},
        "total_bytes": {"type": "integer"},
        "cache_bytes": {"type": "integer"},
        "kernel_bytes": {"type": "integer"},
        "procs": {"type": "integer"},
        "units": {"type": "array", "items": {"type": "string"}},
        "processes": {
            "type": "object",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "pid",
                            "name",
                            "swap_bytes",
                            "ram_bytes",
                            "total_bytes",
                            "age_seconds",
                            "unit",
                        ],
                        "properties": {
                            "pid": {"type": "integer"},
                            "name": {"type": "string"},
                            "swap_bytes": {"type": "integer"},
                            "ram_bytes": {"type": "integer"},
                            "total_bytes": {"type": "integer"},
                            "age_seconds": {"type": "number"},
                            "unit": {"type": "string"},
                        },
                    },
                },
                "has_more": {"type": "boolean"},
            },
        },
        "commands": {
            "type": "object",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "swap_bytes", "ram_bytes", "total_bytes", "procs"],
                        "properties": {
                            "name": {"type": "string"},
                            "swap_bytes": {"type": "integer"},
                            "ram_bytes": {"type": "integer"},
                            "total_bytes": {"type": "integer"},
                            "procs": {"type": "integer"},
                        },
                    },
                },
                "has_more": {"type": "boolean"},
            },
        },
        "unattributed_ram_bytes": {"type": "integer"},
        "unattributed_swap_bytes": {"type": "integer"},
    },
}

# --- index / detail builders --------------------------------------------------

INDEX_COMMANDS: tuple[dict[str, object], ...] = (
    {
        "name": "",
        "description": "Open the live per-app RAM and swap view (needs a terminal)",
        "effects": "read_only",
    },
    {
        "name": "app",
        "description": "One app's units, processes, commands and remainder",
        "effects": "read_only",
    },
    {
        "name": "snapshot",
        "description": "One sample of the machine and every app",
        "effects": "read_only",
    },
)

FORMAT_DEFAULTS: dict[str, str] = {"tty": "text", "non_tty": "json"}

EXIT_CODES: dict[str, str] = {
    "0": "success",
    "1": "failure (cgroup tree unavailable, app not found)",
    "2": "usage error, or the live view started outside a terminal context",
    "130": "interrupted by SIGINT",
    "143": "terminated by SIGTERM",
}


def root_detail() -> dict[str, object]:
    return {
        "name": "",
        "description": ROOT_DESCRIPTION,
        "args": [],
        "flags": [ROOT_INTERVAL.to_dict(), ROOT_SYSTEM.to_dict()],
        "effects": "read_only",
        "confirm": False,
        "interactive": True,
    }


def snapshot_detail() -> dict[str, object]:
    return {
        "name": "snapshot",
        "description": SNAPSHOT_DESCRIPTION,
        "args": [],
        "flags": [SNAPSHOT_SYSTEM.to_dict(), SNAPSHOT_LIMIT.to_dict()],
        "effects": "read_only",
        "confirm": False,
        "interactive": False,
        "output": SNAPSHOT_OUTPUT,
        "output_description": SNAPSHOT_OUTPUT_DESCRIPTION,
    }


def app_detail() -> dict[str, object]:
    return {
        "name": "app",
        "description": APP_DESCRIPTION,
        "args": [APP_NAME.to_dict()],
        "flags": [APP_SCOPE.to_dict(), APP_LIMIT.to_dict()],
        "effects": "read_only",
        "confirm": False,
        "interactive": False,
        "output": APP_OUTPUT,
        "output_description": APP_OUTPUT_DESCRIPTION,
    }


_DETAIL_BUILDERS = {"snapshot": snapshot_detail, "app": app_detail}


def index() -> dict[str, object]:
    """The introspection index: every command, the flags shared by all of
    them, and the tool-wide defaults and exit codes."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "conformance": {
            "name": "cli-design-standard",
            "standard": CONFORMANCE_STANDARD_VERSION,
            "extensions": [],
        },
        "global_flags": [JSON_FLAG.to_dict()],
        "format_defaults": dict(FORMAT_DEFAULTS),
        "exit_codes": dict(EXIT_CODES),
        "commands": [dict(command) for command in INDEX_COMMANDS],
        "command": root_detail(),
    }


def detail(path: list[str]) -> dict[str, object] | None:
    """Detail for a command path, or `None` when the path names nothing.

    Only single-segment paths exist today (`snapshot`, `app`); every other
    path, including the empty root's own name, returns `None` so the caller
    can turn it into a usage error.
    """
    if len(path) != 1:
        return None
    builder = _DETAIL_BUILDERS.get(path[0])
    return builder() if builder is not None else None
