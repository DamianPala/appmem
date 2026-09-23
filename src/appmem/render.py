"""Text (TTY) renderings of the `snapshot` and `app` documents, and the
`Next:` breadcrumb line.

JSON needs no escaping here: `json.dumps` already encodes any character
safely. Text is the one place a name read straight from the system (a unit
directory, a process's argv0) reaches a terminal, so control characters are
replaced with a visible form here instead of trusting the terminal not to
act on them.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Any

from appmem.fmt import format_age, format_pair, pressure_word, size, truncate_name

_CONTROL_MAX = 0x1F
_DEL = 0x7F
_ELSEWHERE_THRESHOLD = 1024 * 1024


def escape_control_chars(text: str) -> str:
    """Replace ASCII control characters (including DEL) with a literal
    `\\xNN`, so a unit or process name copied straight from the system can't
    clear the screen or otherwise act on the terminal it's printed to."""
    return "".join(
        f"\\x{ord(ch):02x}" if ord(ch) <= _CONTROL_MAX or ord(ch) == _DEL else ch for ch in text
    )


def render_next_line(next_argv: Sequence[str]) -> str:
    """The `Next: ...` line: the same call the JSON `next` field carries,
    quoted so it can be pasted straight into a shell. Quoting does not stop
    a control character in an app name from reaching the terminal, so the
    quoted form is escaped too."""
    return "Next: " + " ".join(escape_control_chars(shlex.quote(part)) for part in next_argv)


def _pressure_line(pressure: dict[str, Any] | None) -> str:
    if pressure is None:
        return "pressure 10s: unavailable (no /proc/pressure/memory)"
    word = pressure_word(
        pressure["some_avg10_percent"],
        pressure["some_avg60_percent"],
        pressure["full_avg10_percent"],
    )
    return f"pressure 10s: {word}"


def _snapshot_header_line(document: dict[str, Any]) -> str:
    system = document["system"]
    system_total = system["system_services_ram_bytes"] + system["system_services_swap_bytes"]
    parts = [
        f"RAM {format_pair(system['ram_used_bytes'], system['ram_total_bytes'])}",
        f"avail {size(system['ram_available_bytes'])}",
        f"Swap {format_pair(system['swap_used_bytes'], system['swap_total_bytes'])}",
        _pressure_line(document["pressure"]),
        f"system {size(system_total)}",
    ]
    elsewhere = system["elsewhere_bytes"]
    if elsewhere is not None and elsewhere >= _ELSEWHERE_THRESHOLD:
        parts.append(f"elsewhere {size(elsewhere)}")
    return "  ".join(parts)


def _snapshot_table(items: list[dict[str, Any]]) -> list[str]:
    lines = [f"{'APP':<24}{'SWAP':>10}{'RAM':>10}{'TOTAL':>10}{'PROCS':>7}{'UNITS':>7}"]
    for item in items:
        name = escape_control_chars(truncate_name(str(item["name"]), 23))
        lines.append(
            f"{name:<24}{size(item['swap_bytes']):>10}{size(item['ram_bytes']):>10}"
            f"{size(item['total_bytes']):>10}{item['procs']:>7}{item['units']:>7}"
        )
    return lines


def render_snapshot_text(document: dict[str, Any], *, total_apps: int) -> str:
    """The `snapshot` command's text report: header line, app table, a cut
    notice when `--limit` hid apps, and the `Next:` breadcrumb. The cut
    notice names the real total and the `--limit` that would show it all,
    so a reader of a cut table knows it is partial and how to see the rest."""
    items = document["apps"]["items"]
    lines = [_snapshot_header_line(document), *_snapshot_table(items)]
    if document["apps"]["has_more"]:
        lines.append(f"{len(items)} of {total_apps} apps shown (--limit {total_apps} shows all)")
    next_argv = document.get("next")
    if next_argv:
        lines.append(render_next_line(next_argv))
    return "\n".join(lines)


def _app_title_line(document: dict[str, Any]) -> str:
    name = escape_control_chars(str(document["name"]))
    return (
        f"{name}  {document['scope']}  {document['procs']} procs  "
        f"{len(document['units'])} units  swap {size(document['swap_bytes'])}  "
        f"RAM {size(document['ram_bytes'])}  kernel {size(document['kernel_bytes'])}"
    )


def _process_table(items: list[dict[str, Any]]) -> list[str]:
    lines = [f"{'PID':>8}  {'NAME':<20}{'SWAP':>10}{'RAM':>10}{'TOTAL':>10}{'AGE':>8}  UNIT"]
    for item in items:
        name = escape_control_chars(truncate_name(str(item["name"]), 19))
        unit = escape_control_chars(str(item["unit"]))
        lines.append(
            f"{item['pid']:>8}  {name:<20}{size(item['swap_bytes']):>10}"
            f"{size(item['ram_bytes']):>10}{size(item['total_bytes']):>10}"
            f"{format_age(item['age_seconds']):>8}  {unit}"
        )
    return lines


def _command_table(items: list[dict[str, Any]]) -> list[str]:
    lines = [f"{'COMMAND':<20}{'SWAP':>10}{'RAM':>10}{'TOTAL':>10}{'PROCS':>7}"]
    for item in items:
        name = escape_control_chars(truncate_name(str(item["name"]), 19))
        lines.append(
            f"{name:<20}{size(item['swap_bytes']):>10}{size(item['ram_bytes']):>10}"
            f"{size(item['total_bytes']):>10}{item['procs']:>7}"
        )
    return lines


def render_app_text(document: dict[str, Any], *, total_processes: int, total_commands: int) -> str:
    """The `app` command's text report: a title line, the process and
    command tables (each with its own cut notice naming the real total and
    the `--limit` that would show it all), then the kernel and
    unattributed lines that explain why the rows don't add up to the app."""
    processes = document["processes"]
    commands = document["commands"]
    lines = [_app_title_line(document), *_process_table(processes["items"])]
    if processes["has_more"]:
        lines.append(
            f"{len(processes['items'])} of {total_processes} processes shown "
            f"(--limit {total_processes} shows all)"
        )
    lines.extend(_command_table(commands["items"]))
    if commands["has_more"]:
        lines.append(
            f"{len(commands['items'])} of {total_commands} commands shown "
            f"(--limit {total_commands} shows all)"
        )
    lines.append(
        f"kernel {size(document['kernel_bytes'])} "
        "(charged kernel memory: page tables, slab, stacks)"
    )
    lines.append(
        f"unattributed {size(document['unattributed_swap_bytes'])} swap, "
        f"{size(document['unattributed_ram_bytes'])} RAM (accounting difference, not a process)"
    )
    return "\n".join(lines)
