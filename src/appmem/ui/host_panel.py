"""Live host details, fed by the dashboard's existing collection lifecycle."""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from appmem.darwin_native import HostMemory
from appmem.fmt import format_pair, format_rate, pressure_word, size, total_amount
from appmem.model import SystemStats

_INDENT = 10
_KEY_WIDTH = 27
_TABLE_LABEL = 24
_TABLE_READ = 16
_NARROW_TABLE = 64
_NBSP = "\xa0"


@dataclass(frozen=True)
class Prose:
    """Paragraphs wrapped over the full width, one line break between them."""

    lines: tuple[str, ...]


@dataclass(frozen=True)
class Entry:
    """`label` in a ten-cell column, then lines wrapped under each other."""

    label: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Keyed:
    """A title and two-space indented `key value` rows."""

    title: str
    rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Activity:
    """Read/Written table: wide as columns, narrow as one labelled line per row."""

    rows: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Group:
    """Entries kept adjacent, without a blank line between them."""

    entries: tuple[Entry, ...]


Block = Prose | Entry | Keyed | Activity | Group


def _rate(value: int | None) -> str:
    return "unavailable" if value is None else format_rate(value)


def _percent(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.1f}%"


def _activity(
    rates: tuple[int | None, int | None],
    boot: tuple[int | None, int | None],
    session: tuple[int | None, int | None],
) -> Activity:
    return Activity(
        (
            ("Current rate", _rate(rates[0]), _rate(rates[1])),
            ("Since boot", total_amount(boot[0]), total_amount(boot[1])),
            ("This run", total_amount(session[0]), total_amount(session[1])),
        )
    )


def _zswap_blocks(
    stats: SystemStats, writeback_rate: int | None, *, rate_known: bool
) -> list[Block]:
    pool, logical = stats.zswap_pool_bytes, stats.zswapped_bytes
    ratio = (
        f"{logical / pool:.1f}:1"
        if pool is not None and logical is not None and pool > 0 and logical > 0
        else "unavailable"
    )
    percent = stats.zswap_max_pool_percent
    limit = (
        f"~{size(stats.mem_total * percent // 100)} · {percent}% of RAM"
        if type(percent) is int and percent >= 0
        else "unavailable"
    )
    writeback = (
        f"{_rate(writeback_rate)} now" if rate_known else "unavailable"
    ) + f" · {total_amount(stats.zswap_writeback_bytes)} since boot"
    return [
        Keyed(
            "Zswap",
            (
                ("RAM occupied", _amount(pool)),
                ("Pool limit", limit),
                ("Data held", _amount(logical)),
                ("Compression", ratio),
                ("Compressor", stats.zswap_compressor or "unavailable"),
                ("Writeback", writeback),
            ),
        ),
        Prose(
            (
                f"RAM used includes the compressed size: {_amount(pool)}.",
                "The pool limit is a policy; this RAM is not reserved in advance.",
                "Swap used includes the data held in zswap, even without writing them to disk.",
            )
        ),
    ]


def linux_details(
    stats: SystemStats,
    rates: tuple[int | None, int | None],
    session: tuple[int | None, int | None] = (None, None),
    writeback_rate: int | None = None,
) -> list[Block]:
    used = format_pair(stats.mem_total - stats.mem_available, stats.mem_total)
    blocks: list[Block] = [
        Entry(
            "RAM",
            (
                f"{used} used · available {size(stats.mem_available)}",
                f"Free {size(stats.mem_free)} · file cache {size(stats.mem_cache)} · "
                f"kernel cache (slab) {size(stats.mem_slab)}.",
                f"Shared {size(stats.mem_shared)} · shared memory and files stored in RAM.",
            ),
        )
    ]
    if stats.zswap_enabled:
        blocks += _zswap_blocks(stats, writeback_rate, rate_known=writeback_rate is not None)
    else:
        blocks.append(Entry("Zswap", ("disabled or unavailable",)))
    swap = (
        f"{format_pair(stats.swap_total - stats.swap_free, stats.swap_total)} used"
        if stats.swap_total
        else "off"
    )
    blocks.append(Entry("Swap", (swap,)))
    blocks.append(_activity(rates, (stats.swap_in_bytes, stats.swap_out_bytes), session))
    if stats.swap_disk_only:
        writes = (
            "Writes count only data sent to disk, directly or from zswap."
            if stats.zswap_enabled
            else "Writes count data sent to disk for swap."
        )
    else:
        writes = "Writes count data sent to swap devices."
        if stats.zswap_enabled:
            writes += " Data kept only in zswap is not counted."
    blocks.append(Prose((writes,)))
    return [*blocks, *_pressure_blocks(stats)]


def _pressure_blocks(stats: SystemStats) -> list[Block]:
    psi = (stats.pressure_some_avg10, stats.pressure_some_avg60, stats.pressure_full_avg10)
    word = "unavailable" if any(v is None for v in psi) else pressure_word(*psi)  # type: ignore[arg-type]
    lines = [word]
    if any(v is not None for v in (*psi, stats.pressure_full_avg60)):
        lines += [
            "Time waiting for memory, last 10 seconds / 60 seconds:",
            f"At least one task: {_percent(psi[0])} / {_percent(psi[1])}.",
            f"All active tasks: {_percent(psi[2])} / {_percent(stats.pressure_full_avg60)}.",
        ]
    elsewhere = "unavailable" if stats.elsewhere is None else size(stats.elsewhere)
    system = f"{size(stats.system_ram + stats.system_swap)} · system services (RAM + swap)."
    return [
        Group(
            (
                Entry("Pressure", tuple(lines)),
                Entry("System", (system,)),
                Entry("Elsewhere", (f"{elsewhere} · e.g. other users, VMs or containers.",)),
            )
        )
    ]


def _amount(value: int | None) -> str:
    return size(value) if type(value) is int and value >= 0 else "unavailable"


def darwin_details(
    host: HostMemory,
    rates: tuple[int | None, int | None],
    session: tuple[int | None, int | None] = (None, None),
) -> list[Block]:
    partition = host.ram_partition
    ram = (
        (
            f"{format_pair(partition[0], host.physical_bytes)} used",
            f"File-backed {size(partition[1])} · free {size(partition[2])}.",
        )
        if partition is not None
        else (f"used unavailable · total {_amount(host.physical_bytes)}",)
    )
    logical, physical = host.compressor_logical_bytes, host.compressor_physical_bytes
    ratio = (
        f" ({logical / physical:.1f}:1)"
        if type(logical) is int and type(physical) is int and logical > 0 and physical > 0
        else ""
    )
    compression = f"{_amount(logical)} of data compressed into {_amount(physical)} of RAM{ratio}."
    if not 0 <= host.swap_used_bytes <= host.swap_total_bytes:
        swap = "unavailable"
    elif host.swap_total_bytes == 0:
        swap = "0 B; not allocated"
    else:
        swap = format_pair(host.swap_used_bytes, host.swap_total_bytes) + " used / allocated now"
    pressure = {1: "normal", 2: "warning", 4: "critical"}.get(
        host.pressure_level or 0, "unavailable"
    )
    if host.pressure_unavailable is not None:
        pressure = "unavailable"
    return [
        Entry(
            "RAM",
            (
                *ram,
                f"Wired {_amount(host.wired_bytes)} · purgeable {_amount(host.purgeable_bytes)}.",
                "Wired cannot be swapped; purgeable can be reclaimed. These overlap RAM used.",
                "File-backed is not an available-memory estimate.",
            ),
        ),
        Entry(
            "Compress",
            (compression, f"RAM used includes the physical compressed size: {_amount(physical)}."),
        ),
        Entry(
            "Swap",
            (swap, "macOS allocates swap space dynamically; this is not a fixed capacity."),
        ),
        _activity(rates, (host.swap_in_bytes, host.swap_out_bytes), session),
        Prose(("Activity measures compressed data transferred to and from swap.",)),
        Group(
            (
                Entry("Pressure", (f"{pressure} · native macOS memory pressure",)),
                Entry("Apps", ("this user",)),
            )
        ),
    ]


def _wrap(text: str, width: int, indent: str = "", hang: str = "") -> list[str]:
    wrapped = textwrap.wrap(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=hang,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped or [indent.rstrip()]


def _entry_lines(block: Entry, width: int) -> list[str]:
    indent = " " * _INDENT if width > 24 else ""
    label = block.label.ljust(_INDENT)
    lines: list[str] = []
    for position, line in enumerate(block.lines):
        first = label if position == 0 else indent
        lines += _wrap(line, width, first, indent)
    return lines


def _keyed_lines(block: Keyed, width: int) -> list[str]:
    lines = [block.title]
    for key, value in block.rows:
        if width >= 2 + _KEY_WIDTH + len(value):
            lines.append(f"  {key:<{_KEY_WIDTH}}{value}")
        else:
            lines += _wrap(f"{key}: {value}", width, "  ", "    ")
    return lines


def _activity_lines(block: Activity, width: int) -> list[str]:
    if width >= _NARROW_TABLE:
        head = "Activity".ljust(_INDENT) + f"{'':{_TABLE_LABEL}}{'Read':{_TABLE_READ}}Written"
        rows = [
            " " * _INDENT + f"{label:<{_TABLE_LABEL}}{read:<{_TABLE_READ}}{written}"
            for label, read, written in block.rows
        ]
        return [head, *rows]
    lines = _wrap("Activity (Read / Written)", width)
    for label, read, written in block.rows:
        # Keep each value together with its unit when the line wraps.
        text = f"{label}: Read {read}; Written {written}".replace(" B", f"{_NBSP}B")
        text = text.replace(" KiB", f"{_NBSP}KiB").replace(" MiB", f"{_NBSP}MiB")
        text = text.replace(" GiB", f"{_NBSP}GiB").replace(" TiB", f"{_NBSP}TiB")
        text = text.replace(" PiB", f"{_NBSP}PiB").replace(" EiB", f"{_NBSP}EiB")
        lines += [line.replace(_NBSP, " ") for line in _wrap(text, width, "  ", "    ")]
    return lines


def render_blocks(blocks: Sequence[Block], width: int, *, ascii_bars: bool = False) -> str:
    """Lay the blocks out for `width` cells, one blank line between them
    (Entry and Activity runs of the same section stay adjacent as before)."""
    chunks: list[str] = []
    for block in blocks:
        if isinstance(block, Prose):
            lines = [row for line in block.lines for row in _wrap(line, width)]
        elif isinstance(block, Entry):
            lines = _entry_lines(block, width)
        elif isinstance(block, Group):
            lines = [row for entry in block.entries for row in _entry_lines(entry, width)]
        elif isinstance(block, Keyed):
            lines = _keyed_lines(block, width)
        else:
            lines = _activity_lines(block, width)
        chunks.append("\n".join(lines))
    text = "\n\n".join(chunks)
    return text.replace("·", "|").replace("Δ", "delta") if ascii_bars else text


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
        self,
        owner: Screen[None],
        content: Callable[[], Sequence[Block]],
        help_action: Callable[[], None],
        *,
        ascii_bars: bool = False,
    ) -> None:
        super().__init__()
        self.owner = owner
        self._content = content
        self._help_action = help_action
        self._ascii_bars = ascii_bars
        self._body: str | None = None

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
        body = render_blocks(self._content(), width, ascii_bars=self._ascii_bars)
        if body != self._body:  # an unchanged tick must not relayout the panel
            self._body = body
            self.query_one("#host-text", Static).update(body)

    def action_help(self) -> None:
        self._help_action()

    def action_close(self) -> None:
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
