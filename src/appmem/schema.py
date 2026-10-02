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
from appmem.theme import THEME_NAMES

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
ROOT_THEME = Flag(
    name="theme",
    description=(
        "Open the live view with this theme (terminal-dark/terminal-light use your "
        "terminal's own colours, the rest are Textual's built-in themes), overriding "
        "APPMEM_THEME and the config file for this run only; never written back. Only "
        "the live view uses it, a named command rejects it."
    ),
    type="string",
    required=False,
    enum=THEME_NAMES,
)
ROOT_DESCRIPTION = (
    "Open the live per-app RAM and swap view. Starts only in a terminal context: "
    "stdin and stdout are TTYs, --json is absent and NO_INPUT is unset; otherwise "
    "fails with kind terminal_required before reading anything and points to "
    "appmem snapshot. The theme (--theme, APPMEM_THEME, or the config file, in "
    "that order) is remembered across runs once chosen inside the app."
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
    "then name. next is omitted when there are no items. Failures return no result."
)

SNAPSHOT_OUTPUT: dict[str, object] = {
    "type": "object",
    "required": ["taken_at", "system", "pressure", "apps"],
    "properties": {
        "taken_at": {
            "type": "string",
            "description": "When this sample was taken, RFC 3339 with the local UTC offset.",
        },
        "system": {
            "type": "object",
            "description": (
                "Machine-wide RAM, swap and zswap; apps.items don't add up to it, "
                "system_services_* and elsewhere_bytes cover the rest."
            ),
            "required": [
                "ram_total_bytes",
                "ram_used_bytes",
                "ram_available_bytes",
                "ram_free_bytes",
                "ram_cache_bytes",
                "ram_slab_bytes",
                "ram_shared_bytes",
                "swap_in_bytes",
                "swap_out_bytes",
                "swap_total_bytes",
                "swap_used_bytes",
                "system_services_ram_bytes",
                "system_services_swap_bytes",
                "elsewhere_bytes",
                "zswap_enabled",
                "zswap_pool_bytes",
                "zswapped_bytes",
                "zswap_writeback_bytes",
                "zswap_compressor",
                "zswap_max_pool_percent",
                "zswap_compression_ratio",
            ],
            "properties": {
                "ram_total_bytes": {
                    "type": "integer",
                    "description": "Total installed RAM (/proc/meminfo MemTotal).",
                },
                "ram_used_bytes": {
                    "type": "integer",
                    "description": (
                        "ram_total_bytes minus ram_available_bytes, so reclaimable cache "
                        "does not count as used."
                    ),
                },
                "ram_available_bytes": {
                    "type": "integer",
                    "description": (
                        "Kernel estimate of RAM available for a new allocation before "
                        "swapping; ram_free_bytes, ram_cache_bytes and ram_slab_bytes are "
                        "its main parts, not an exact sum -- the kernel reserves headroom."
                    ),
                },
                "ram_free_bytes": {
                    "type": "integer",
                    "description": "Truly free RAM, with no cache or shared memory in it.",
                },
                "ram_cache_bytes": {
                    "type": "integer",
                    "description": (
                        "Reclaimable page cache (file cache without ram_shared_bytes), one "
                        "of the main parts of ram_available_bytes."
                    ),
                },
                "ram_slab_bytes": {
                    "type": "integer",
                    "description": (
                        "Reclaimable kernel caches of file names and inodes, one of the "
                        "main parts of ram_available_bytes."
                    ),
                },
                "ram_shared_bytes": {
                    "type": "integer",
                    "description": (
                        "tmpfs, shared memory and GPU buffers: part of ram_used_bytes, "
                        "swappable but not droppable by the kernel."
                    ),
                },
                "swap_in_bytes": {
                    "type": ["integer", "null"],
                    "description": (
                        "Lifetime since boot: /proc/vmstat pswpin times "
                        "SC_PAGE_SIZE. Swap device reads, including zram; "
                        "successful zswap hits excluded. null when unavailable."
                    ),
                },
                "swap_out_bytes": {
                    "type": ["integer", "null"],
                    "description": (
                        "Lifetime since boot: /proc/vmstat pswpout times "
                        "SC_PAGE_SIZE. Swap device writes, including zram and "
                        "zswap writeback; not SSD throughput. null when "
                        "unavailable."
                    ),
                },
                "swap_total_bytes": {
                    "type": "integer",
                    "description": "Total configured swap space.",
                },
                "swap_used_bytes": {
                    "type": "integer",
                    "description": "Swap space currently in use.",
                },
                "system_services_ram_bytes": {
                    "type": "integer",
                    "description": (
                        "RAM held by all system services together (system.slice); in no "
                        "user app, split per service into apps.items only with --system."
                    ),
                },
                "system_services_swap_bytes": {
                    "type": "integer",
                    "description": (
                        "Swap held by all system services together (system.slice); in no "
                        "user app, split per service into apps.items only with --system."
                    ),
                },
                "elsewhere_bytes": {
                    "type": ["integer", "null"],
                    "description": (
                        "RAM held outside this user's apps and the system services: VMs, "
                        "containers, other users, login sessions. null when the root "
                        "cgroup's memory.stat is missing."
                    ),
                },
                "zswap_enabled": {
                    "type": "boolean",
                    "description": (
                        "Whether zswap is on; when false, every zswap_* field here except "
                        "zswap_writeback_bytes is null."
                    ),
                },
                "zswap_pool_bytes": {
                    "type": ["integer", "null"],
                    "description": (
                        "RAM the compressed zswap pool itself costs, already inside "
                        "ram_used_bytes. null when zswap_enabled is false."
                    ),
                },
                "zswapped_bytes": {
                    "type": ["integer", "null"],
                    "description": (
                        "Swapped data held compressed in the zswap pool, already inside "
                        "swap_used_bytes. null when zswap_enabled is false."
                    ),
                },
                "zswap_writeback_bytes": {
                    "type": ["integer", "null"],
                    "description": (
                        "Pages written back from the zswap pool to swap devices, cumulative "
                        "since boot; one sample has no rate, so diff two snapshots. null "
                        "when the kernel has no writeback counter."
                    ),
                },
                "zswap_compressor": {
                    "type": ["string", "null"],
                    "description": (
                        "The kernel's zswap compressor name (e.g. lzo, zstd). null when "
                        "zswap_enabled is false."
                    ),
                },
                "zswap_max_pool_percent": {
                    "type": ["integer", "null"],
                    "description": (
                        "The zswap pool's configured cap, as a percentage of total RAM. "
                        "null when zswap_enabled is false."
                    ),
                },
                "zswap_compression_ratio": {
                    "type": ["number", "null"],
                    "description": (
                        "zswapped_bytes / zswap_pool_bytes: how much the pool shrinks "
                        "what it holds. null when zswap_enabled is false or either side "
                        "is 0."
                    ),
                },
            },
        },
        "pressure": {
            "type": ["object", "null"],
            "description": (
                "Memory pressure over the last 10 s and 60 s. null when "
                "/proc/pressure/memory is missing; judge by ram_available_bytes alone then."
            ),
            "required": [
                "level",
                "some_avg10_percent",
                "some_avg60_percent",
                "full_avg10_percent",
                "full_avg60_percent",
            ],
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["none", "some", "high"],
                    "description": (
                        "none, some or high, from the last 10 s of stalls; none with "
                        "some_avg60_percent above 1 means stalls just stopped."
                    ),
                },
                "some_avg10_percent": {
                    "type": "number",
                    "description": "Share of the last 10 s some task spent waiting for memory.",
                },
                "some_avg60_percent": {
                    "type": "number",
                    "description": "Share of the last 60 s some task spent waiting for memory.",
                },
                "full_avg10_percent": {
                    "type": "number",
                    "description": (
                        "Share of the last 10 s every task was waiting for memory at once."
                    ),
                },
                "full_avg60_percent": {
                    "type": "number",
                    "description": (
                        "Share of the last 60 s every task was waiting for memory at once."
                    ),
                },
            },
        },
        "apps": {
            "type": "object",
            "description": "Every app with at least 1 MiB of total_bytes, paged to limit.",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "description": (
                        "The page of apps, sorted by total_bytes descending, then scope, then name."
                    ),
                    "items": {
                        "type": "object",
                        "required": [
                            "name",
                            "scope",
                            "ram_bytes",
                            "swap_bytes",
                            "total_bytes",
                            "cache_bytes",
                            "zswapped_bytes",
                            "kernel_bytes",
                            "procs",
                            "unit_count",
                            "top_commands",
                        ],
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "The app's name, normalized from its cgroup unit(s)."
                                ),
                            },
                            "scope": {
                                "type": "string",
                                "enum": ["user", "system"],
                                "description": (
                                    "user or system: which tree this app's unit(s) came from."
                                ),
                            },
                            "ram_bytes": {
                                "type": "integer",
                                "description": (
                                    "Anonymous + shared + charged kernel memory, no page cache."
                                ),
                            },
                            "swap_bytes": {
                                "type": "integer",
                                "description": (
                                    "Swap this app's cgroup(s) hold; with zswap on, "
                                    "includes zswapped_bytes."
                                ),
                            },
                            "total_bytes": {
                                "type": "integer",
                                "description": (
                                    "ram_bytes + swap_bytes, an accounting sum, not what "
                                    "closing the app would free."
                                ),
                            },
                            "cache_bytes": {
                                "type": "integer",
                                "description": "Reclaimable page cache, not in total_bytes.",
                            },
                            "zswapped_bytes": {
                                "type": "integer",
                                "description": (
                                    "This app's share of swap held compressed in the "
                                    "zswap pool, already inside swap_bytes."
                                ),
                            },
                            "kernel_bytes": {
                                "type": "integer",
                                "description": (
                                    "This app's charged kernel memory (page tables, slab, "
                                    "stacks), excluding its zswap pool share; both stay "
                                    "inside ram_bytes."
                                ),
                            },
                            "procs": {
                                "type": "integer",
                                "description": "Number of processes in this app's cgroup(s).",
                            },
                            "unit_count": {
                                "type": "integer",
                                "description": "Number of unit directories merged into this row.",
                            },
                            "top_commands": {
                                "type": "array",
                                "description": (
                                    "This app's 3 largest commands by total_bytes, same "
                                    "grouping as appmem app NAME's commands; empty for an "
                                    "app with 6 or fewer processes."
                                ),
                                "items": {
                                    "type": "object",
                                    "required": ["name", "total_bytes", "procs"],
                                    "properties": {
                                        "name": {
                                            "type": "string",
                                            "description": "Command name.",
                                        },
                                        "total_bytes": {
                                            "type": "integer",
                                            "description": (
                                                "RAM + swap summed across this command's processes."
                                            ),
                                        },
                                        "procs": {
                                            "type": "integer",
                                            "description": (
                                                "Number of processes summed into this command."
                                            ),
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                "has_more": {
                    "type": "boolean",
                    "description": "true when more apps exist than limit allowed through.",
                },
            },
        },
        "next": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The argv to run appmem app on the first (largest) item; omitted when "
                "there are no items, repeats --json when the call passed it."
            ),
        },
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
    "processes.items sorted by total_bytes descending, ties by name then pid. "
    "commands.items computed over all processes, sorted by total_bytes descending, "
    "ties by name. Failures return no result."
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
        "zswapped_bytes",
        "kernel_bytes",
        "zswap_pool_bytes",
        "procs",
        "units",
        "processes",
        "commands",
        "unattributed_ram_bytes",
        "unattributed_swap_bytes",
    ],
    "properties": {
        "taken_at": {
            "type": "string",
            "description": "When this sample was taken, RFC 3339 with the local UTC offset.",
        },
        "name": {"type": "string", "description": "The app's name, as passed in."},
        "scope": {
            "type": "string",
            "enum": ["user", "system"],
            "description": "user or system, as passed in.",
        },
        "ram_bytes": {
            "type": "integer",
            "description": "Anonymous + shared + charged kernel memory, no page cache.",
        },
        "swap_bytes": {
            "type": "integer",
            "description": (
                "Swap this app's cgroup(s) hold; with zswap on, includes zswapped_bytes."
            ),
        },
        "total_bytes": {
            "type": "integer",
            "description": (
                "ram_bytes + swap_bytes, an accounting sum, not what closing the app would free."
            ),
        },
        "cache_bytes": {
            "type": "integer",
            "description": "Reclaimable page cache, not in total_bytes.",
        },
        "zswapped_bytes": {
            "type": "integer",
            "description": (
                "This app's share of swap held compressed in the zswap pool, already "
                "inside swap_bytes."
            ),
        },
        "kernel_bytes": {
            "type": "integer",
            "description": (
                "This app's charged kernel memory (page tables, slab, stacks), excluding "
                "zswap_pool_bytes; both stay inside ram_bytes."
            ),
        },
        "zswap_pool_bytes": {
            "type": "integer",
            "description": (
                "RAM this app's own share of the compressed zswap pool costs, already "
                "inside ram_bytes; 0 without zswap."
            ),
        },
        "procs": {
            "type": "integer",
            "description": "Number of processes across this app's units.",
        },
        "units": {
            "type": "array",
            "description": "Every unit directory merged into this app.",
            "items": {
                "type": "object",
                "required": ["name", "label"],
                "properties": {
                    "name": {"type": "string", "description": "Raw unit directory name."},
                    "label": {
                        "type": "string",
                        "description": (
                            "The unit name with systemd's own \\xNN escaping undone, "
                            "readable at a glance."
                        ),
                    },
                },
            },
        },
        "processes": {
            "type": "object",
            "description": "Every process in this app's cgroup(s), paged to limit.",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "description": (
                        "The page of processes, sorted by total_bytes descending, ties "
                        "by name then pid."
                    ),
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
                            "private_bytes",
                        ],
                        "properties": {
                            "pid": {"type": "integer", "description": "Process id."},
                            "name": {
                                "type": "string",
                                "description": "Process (or interpreter:target) name.",
                            },
                            "swap_bytes": {
                                "type": "integer",
                                "description": "This process's own swap (VmSwap).",
                            },
                            "ram_bytes": {
                                "type": "integer",
                                "description": (
                                    "This process's own RSS without file-backed pages "
                                    "(RssAnon + RssShmem, smaller than htop's RES); a shared "
                                    "page counts once per process that maps it, so don't "
                                    "sum this across processes -- use the app's own ram_bytes."
                                ),
                            },
                            "total_bytes": {
                                "type": "integer",
                                "description": "This process's own swap_bytes + ram_bytes.",
                            },
                            "age_seconds": {
                                "type": "integer",
                                "description": "How long this process has been running.",
                            },
                            "unit": {
                                "type": "string",
                                "description": (
                                    "The raw unit directory name this process belongs to."
                                ),
                            },
                            "private_bytes": {
                                "type": ["integer", "null"],
                                "description": (
                                    "This process's own unshared memory (USS), null when "
                                    "unreadable, e.g. a sandboxed process."
                                ),
                            },
                        },
                    },
                },
                "has_more": {
                    "type": "boolean",
                    "description": "true when more processes exist than limit allowed through.",
                },
            },
        },
        "commands": {
            "type": "object",
            "description": "Processes summed by command name, paged to limit.",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "description": (
                        "The page of commands, sorted by total_bytes descending, ties by name."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["name", "swap_bytes", "ram_bytes", "total_bytes", "procs"],
                        "properties": {
                            "name": {"type": "string", "description": "Command name."},
                            "swap_bytes": {
                                "type": "integer",
                                "description": "Swap summed across this command's processes.",
                            },
                            "ram_bytes": {
                                "type": "integer",
                                "description": "RAM summed across this command's processes.",
                            },
                            "total_bytes": {
                                "type": "integer",
                                "description": "swap_bytes + ram_bytes for this command.",
                            },
                            "procs": {
                                "type": "integer",
                                "description": "Number of processes summed into this command.",
                            },
                        },
                    },
                },
                "has_more": {
                    "type": "boolean",
                    "description": "true when more commands exist than limit allowed through.",
                },
            },
        },
        "unattributed_ram_bytes": {
            "type": "integer",
            "description": (
                "RAM the app holds beyond its processes' ram_bytes, kernel_bytes and "
                "zswap_pool_bytes (GPU buffers, memfd, tmpfs), an accounting remainder "
                "clamped at 0."
            ),
        },
        "unattributed_swap_bytes": {
            "type": "integer",
            "description": (
                "Swap the app holds beyond its processes' swap_bytes, an accounting "
                "remainder clamped at 0."
            ),
        },
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
    "1": "runtime failure (cgroup tree unavailable, app not found, no terminal for the live view)",
    "2": "usage error",
    "130": "interrupted by SIGINT",
    "143": "terminated by SIGTERM",
}


def root_detail() -> dict[str, object]:
    return {
        "name": "",
        "description": ROOT_DESCRIPTION,
        "args": [],
        "flags": [ROOT_INTERVAL.to_dict(), ROOT_SYSTEM.to_dict(), ROOT_THEME.to_dict()],
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
