"""Live host details, fed by the dashboard's existing collection lifecycle."""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from appmem.darwin_native import HostMemory
from appmem.fmt import format_pair, format_rate, pressure_word, size
from appmem.model import SystemStats


def _rate(value: int | None) -> str:
    return "unavailable" if value is None else format_rate(value)


def _percent(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.1f}%"


def linux_details(stats: SystemStats, rates: tuple[int | None, int | None]) -> str:
    blocks = [
        f"RAM       {format_pair(stats.mem_total - stats.mem_available, stats.mem_total)} used · "
        f"available {size(stats.mem_available)}\n"
        f"          Free {size(stats.mem_free)} · file cache {size(stats.mem_cache)} · "
        f"kernel cache (slab) {size(stats.mem_slab)}.\n"
        f"          Shared {size(stats.mem_shared)} · shared memory and files stored in RAM."
    ]
    if not stats.zswap_enabled:
        blocks.append("Zswap     disabled or unavailable")
    elif stats.zswap_pool_bytes is None or stats.zswapped_bytes is None:
        blocks.append("Zswap     unavailable")
    else:
        pool, logical = stats.zswap_pool_bytes, stats.zswapped_bytes
        ratio = f" ({logical / pool:.1f}:1)" if pool > 0 and logical > 0 else ""
        percent = stats.zswap_max_pool_percent
        limit = (
            f"~{size(stats.mem_total * percent // 100)} ({percent}%)"
            if type(percent) is int and percent >= 0
            else "unavailable"
        )
        blocks.append(
            f"Zswap     {size(logical)} of data compressed into {size(pool)} of RAM{ratio}.\n"
            f"          RAM used includes the compressed size: {size(pool)}.\n"
            f"          Swap used includes these {size(logical)}, even without writing them "
            "to disk.\n"
            f"          RAM limit: {limit}; this RAM is not reserved in advance."
        )
    blocks.append(
        f"Swap      {format_pair(stats.swap_total - stats.swap_free, stats.swap_total)} used · "
        f"in {_rate(rates[0])} · out {_rate(rates[1])}"
    )
    psi = (stats.pressure_some_avg10, stats.pressure_some_avg60, stats.pressure_full_avg10)
    word = "unavailable" if any(v is None for v in psi) else pressure_word(*psi)  # type: ignore[arg-type]
    pressure = f"Pressure  {word}"
    if any(v is not None for v in (*psi, stats.pressure_full_avg60)):
        pressure += (
            "\n          Time waiting for memory, last 10 seconds / 60 seconds:"
            f"\n          At least one task: {_percent(psi[0])} / {_percent(psi[1])}."
            f"\n          All active tasks: {_percent(psi[2])} / "
            f"{_percent(stats.pressure_full_avg60)}."
        )
    elsewhere = "unavailable" if stats.elsewhere is None else size(stats.elsewhere)
    blocks.append(
        pressure + f"\nSystem    {size(stats.system_ram + stats.system_swap)} · "
        f"system services (RAM + swap).\nElsewhere {elsewhere} · "
        "e.g. other users, VMs or containers."
    )
    return "\n\n".join(blocks)


def _amount(value: int | None) -> str:
    return size(value) if type(value) is int and value >= 0 else "unavailable"


def darwin_details(host: HostMemory, rates: tuple[int | None, int | None]) -> str:
    partition = host.ram_partition
    ram = (
        f"{format_pair(partition[0], host.physical_bytes)} used\n"
        f"          File-backed {size(partition[1])} · free {size(partition[2])}."
        if partition is not None
        else f"used unavailable · total {_amount(host.physical_bytes)}"
    )
    logical, physical = host.compressor_logical_bytes, host.compressor_physical_bytes
    ratio = (
        f" ({logical / physical:.1f}:1)"
        if type(logical) is int and type(physical) is int and logical > 0 and physical > 0
        else ""
    )
    compression = f"{_amount(logical)} of data compressed into {_amount(physical)} of RAM{ratio}."
    swap = (
        format_pair(host.swap_used_bytes, host.swap_total_bytes) + " used / allocated now"
        if 0 <= host.swap_used_bytes <= host.swap_total_bytes
        else "unavailable"
    )
    pressure = {1: "normal", 2: "warning", 4: "critical"}.get(
        host.pressure_level or 0, "unavailable"
    )
    if host.pressure_unavailable is not None:
        pressure = "unavailable"
    return (
        f"RAM       {ram}\n          Wired {_amount(host.wired_bytes)} · "
        f"purgeable {_amount(host.purgeable_bytes)}.\n"
        "          Wired cannot be swapped; purgeable can be reclaimed. These overlap RAM used.\n"
        "          File-backed is not an available-memory estimate.\n\n"
        f"Compress  {compression}\n"
        f"          RAM used includes the physical compressed size: {_amount(physical)}.\n\n"
        f"Swap      {swap} · in {_rate(rates[0])} · out {_rate(rates[1])}\n"
        "          macOS allocates swap space dynamically; this is not a fixed capacity.\n\n"
        f"Pressure  {pressure} · native macOS memory pressure\nApps      this user"
    )


class HostPanel(Screen[None]):
    """Scrollable overlay without a collector or a timer of its own."""

    DEFAULT_CSS = """
    HostPanel { background: $surface; }
    HostPanel #host-title { height: 1; }
    HostPanel #host-scroll { height: 1fr; scrollbar-gutter: stable; }
    HostPanel #host-text { height: auto; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h,escape", "close", "close", show=False),
        Binding("?", "help", "help", show=False),
    ]

    def __init__(
        self, owner: Screen[None], content: Callable[[], str], help_action: Callable[[], None]
    ) -> None:
        super().__init__()
        self.owner = owner
        self._content = content
        self._help_action = help_action

    def compose(self) -> ComposeResult:
        yield Static("Host memory    h / Esc close", id="host-title")
        with VerticalScroll(id="host-scroll"):
            yield Static("", id="host-text", markup=False)

    def on_mount(self) -> None:
        self.query_one("#host-scroll", VerticalScroll).focus()
        self.update_sample()

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.update_sample)

    def update_sample(self) -> None:
        scroll = self.query_one("#host-scroll", VerticalScroll)
        width = max(10, scroll.size.width - scroll.scrollbar_size_vertical)
        body = "\n".join(
            textwrap.fill(
                line.strip(),
                width=width,
                initial_indent="          " if line.startswith("          ") and width > 24 else "",
                subsequent_indent="          " if width > 24 else "",
            )
            if line
            else ""
            for line in self._content().splitlines()
        )
        self.query_one("#host-text", Static).update(body)

    def action_help(self) -> None:
        self._help_action()

    def action_close(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
