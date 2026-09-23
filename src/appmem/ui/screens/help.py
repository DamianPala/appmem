"""Help screen: static text explaining the numbers (SPEC.md "Help screen").

The body soft-wraps to the current terminal width with hanging indents:
each paragraph/labeled item is wrapped fresh via `textwrap` instead of being
pre-broken at a fixed column, so it never double-wraps into isolated
single-word lines at 80 columns. A fixed title (with the close keys) sits
above it and a footer below it; only the middle `#help-scroll` area scrolls
when its content overflows the terminal (a bare `Static` with
`overflow-y: auto` never scrolls because its virtual size always equals its
region -- it needs an actual `VerticalScroll` ancestor whose height is
smaller than its child's).
"""

from __future__ import annotations

import textwrap
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from appmem.ui.layout import build_footer

TITLE_TEXT = "Help  esc/?/q close"

_INTRO = "appmem groups memory the kernel already tracks per app cgroup, instead of per process."

_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("RAM", "Anonymous + shared + charged kernel memory, excluding file cache."),
    (
        "CACHE",
        "Page cache (file - shmem): reclaimable, hidden by default (press c), "
        "never counted in TOTAL.",
    ),
    (
        "SWAP",
        "memory.swap.current. With zswap enabled this also includes pages held "
        "compressed in RAM, not only pages written out to a swap device.",
    ),
    (
        "TOTAL",
        "SWAP + RAM: an accounting sum, not a prediction of what closing the app would free.",
    ),
)

_ZSWAP_DEFINITION: tuple[str, str] = (
    "ZSWAP",
    "The part of SWAP held compressed in RAM (press w), not extra memory of its own.",
)

_ZSWAP_HEADER_NOTE = (
    "In the header, 'X zswap in Y' (inside Swap) is X: swapped data kept compressed in "
    "RAM, already part of Swap used; Y: the RAM that pool costs, already part of RAM used. "
    "'wb' next to it means the pool is overflowing to the disk swap, which is slow."
)

_HEADER_NOTE = (
    "The header counts the whole machine (RAM/Swap/avail). Rows only cover the app "
    "trees appmem walks, plus the hidden system and elsewhere totals -- they won't "
    "add up to the header, and that's expected."
)

_HEADER_TERMS_NOTE = (
    "shared (in the RAM figure) is tmpfs (/tmp, /dev/shm), shared memory and GPU "
    "buffers: the kernel cannot drop it, only swap it out, and a tmpfs file counts "
    "toward the app that wrote it. avail is the kernel's estimate of what can be "
    "allocated before swapping; free, cache (reclaimable file pages) and slab "
    "(kernel caches of file names and inodes, dropped on demand) are its main "
    "parts and come close to it, but are not an exact sum -- the kernel reserves "
    "some headroom."
)

_PRESSURE_INTRO = "Memory pressure is the share of the last 10 s spent waiting for memory:"
_PRESSURE_ITEMS: tuple[tuple[str, str], ...] = (
    ("none", "few memory stalls in the last 10 s"),
    ("some (X.X %)", "some waiting, shown with the percentage"),
    (
        "high",
        "a lot of waiting: memory stalls are happening, but this alone doesn't say "
        "which app is causing them",
    ),
)
_PRESSURE_NOTE = (
    "Big SWAP with pressure none just means idle pages were paged out to make room "
    "for something else. Not a problem by itself."
)

_DELTA_NOTE = (
    "Δ is the change since the baseline: appmem start, or the last z. Apps that "
    "appear later count from their first sample; an app that closes and reopens "
    "starts a fresh Δ instead of comparing against its old instance."
)

_PROCESS_ROWS_NOTE = (
    "Process rows don't add up to the app row for two reasons: a shared page counts "
    "once in every process that maps it, so rows can sum to more than the app. Memory "
    "the app holds without any process mapping it (GPU buffers, memfd, tmpfs) belongs "
    "to no process, so rows can also fall short. Per-process values here also leave "
    "out file-backed pages, so they read smaller than htop's RES."
)
_KERNEL_UNATTRIBUTED_NOTE = (
    "Two rows cover the gap: kernel is the app's own page tables, slab and stacks "
    "(exact, part of the RAM total above); unattributed is everything else left "
    "over -- an accounting difference, not a process."
)

_CLOSED_APP_NOTE = (
    "A closed app can still show a row: helper processes, crash handlers, or a unit "
    "that outlives its processes (PROCS 0) can keep holding memory on their own."
)

_TERMINALS_NOTE = (
    "Terminals: anything started from a terminal (node, claude, python, ...) lives in "
    "the terminal's cgroup, so it counts as the terminal. Press g to see what is "
    "really running inside it, then Enter on a command to see just its processes."
)

_SYSTEM_ROWS_NOTE = (
    'System rows (toggled with x) show as "name [sys]": a user service and a '
    "same-named system service stay two separate rows, with separate Δ."
)

_THEME_NOTE = (
    "T (or Ctrl+P) opens Textual's own theme picker; the choice is remembered in "
    "~/.config/appmem/config.toml (or $XDG_CONFIG_HOME/appmem/config.toml)."
)

_ACTIONS_INTRO = "What to do about it:"
_ACTIONS_PLAIN = "close the app normally"
_ACTIONS_COMMANDS: tuple[tuple[str, str], ...] = (
    ("systemctl --user stop UNIT", "stop a user unit cleanly"),
    ("systemctl --user kill UNIT", "kill everything in a user unit's cgroup"),
    ("kill PID", "kill a single process"),
)

_MIN_WRAP_WIDTH = 20


def _wrap(text: str, width: int) -> str:
    return textwrap.fill(text, width=width)


def _wrap_item(label: str, body: str, width: int, column: int) -> str:
    first = f"{label:<{column}}"
    return textwrap.fill(
        body, width=max(width, column + 10), initial_indent=first, subsequent_indent=" " * column
    )


def _build_body(width: int, *, zswap_enabled: bool = False) -> str:
    w = max(width, _MIN_WRAP_WIDTH)
    # Only mentioned when this machine actually has zswap: nothing to say
    # about a header bracket and a column that never appear otherwise
    # (SPEC.md "Main view": "hidden from the footer and help").
    definitions = (*_DEFINITIONS, _ZSWAP_DEFINITION) if zswap_enabled else _DEFINITIONS
    blocks = [
        _wrap(_INTRO, w),
        "\n".join(_wrap_item(f"{key}  ", desc, w, column=10) for key, desc in definitions),
        _wrap(_HEADER_NOTE, w),
        _wrap(_HEADER_TERMS_NOTE, w),
        *([_wrap(_ZSWAP_HEADER_NOTE, w)] if zswap_enabled else []),
        "\n".join(
            [
                _wrap(_PRESSURE_INTRO, w),
                *(_wrap_item(f"  {key}", desc, w, column=18) for key, desc in _PRESSURE_ITEMS),
            ]
        ),
        _wrap(_PRESSURE_NOTE, w),
        _wrap(_DELTA_NOTE, w),
        _wrap(_PROCESS_ROWS_NOTE, w),
        _wrap(_KERNEL_UNATTRIBUTED_NOTE, w),
        _wrap(_CLOSED_APP_NOTE, w),
        _wrap(_TERMINALS_NOTE, w),
        _wrap(_SYSTEM_ROWS_NOTE, w),
        _wrap(_THEME_NOTE, w),
        "\n".join(
            [
                _wrap(_ACTIONS_INTRO, w),
                _wrap_item("  ", _ACTIONS_PLAIN, w, column=2),
                *(_wrap_item(f"  {cmd}", desc, w, column=30) for cmd, desc in _ACTIONS_COMMANDS),
            ]
        ),
    ]
    return "\n\n".join(blocks)


_FOOTER_ITEMS: tuple[tuple[tuple[str, ...], str], ...] = ((("esc",), "close"),)


class HelpScreen(Screen[None]):
    """Static help text (SPEC.md "Help screen"). Opens from either view."""

    DEFAULT_CSS = """
    HelpScreen #help-scroll { height: 1fr; scrollbar-gutter: stable; }
    HelpScreen #help-text { height: auto; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("?", "close", "close", show=False),
        # Shadows AppMemApp's `q` -> quit binding while this screen is on top:
        # `q` closes the help screen instead of quitting (SPEC.md "Help screen").
        Binding("q", "close", "close", show=False),
    ]

    def __init__(self, *, zswap_enabled: bool = False) -> None:
        super().__init__()
        self._zswap_enabled = zswap_enabled
        """Whether the caller's machine has zswap on: the ZSWAP column and
        header definitions only make sense to mention then (SPEC.md "Main
        view"). Defaults `False` for a caller with no zswap context of its
        own, such as the process view."""

    def compose(self) -> ComposeResult:
        yield Static(TITLE_TEXT, id="help-title")
        # Initial body uses the full app width as a stand-in: `#help-scroll`
        # isn't mounted yet during its own compose, so its real content width
        # (minus the scrollbar gutter) isn't known until `on_mount` below.
        with VerticalScroll(id="help-scroll"):
            # `markup=False`: the text shows `"name [sys]"` literally (markup ate it).
            initial_width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
            body = _build_body(initial_width, zswap_enabled=self._zswap_enabled)
            yield Static(body, id="help-text", markup=False)
        yield Static(build_footer(_FOOTER_ITEMS), id="footer")

    def on_mount(self) -> None:
        # The scrollbar reserves a fixed gutter (`scrollbar-gutter: stable`)
        # regardless of whether the body currently overflows, so wrapping at
        # `app.size.width` alone re-wraps a line's last word once a scrollbar
        # is actually shown. Refresh now that `#help-scroll` is mounted and
        # its real content width is known; also gives it keyboard focus so
        # up/down/pageup/pagedown/home/end (its built-in bindings) work.
        self._refresh_body()
        self.query_one("#help-scroll", VerticalScroll).focus()

    def on_resize(self, event: events.Resize) -> None:
        # Soft-wraps at any width -- rebuild the wrap on every resize.
        # After the refresh: updated mid-layout, the body's auto height is
        # measured at its old width and stays too tall when widening.
        self.call_after_refresh(self._refresh_body)

    def _refresh_body(self) -> None:
        widget = self.query_one("#help-text", Static)
        widget.update(_build_body(self._content_width(), zswap_enabled=self._zswap_enabled))

    def _content_width(self) -> int:
        scroll = self.query_one("#help-scroll", VerticalScroll)
        return scroll.size.width - scroll.scrollbar_size_vertical

    def action_close(self) -> None:
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
