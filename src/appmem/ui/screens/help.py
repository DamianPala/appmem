"""Help screen: static text explaining the numbers (SPEC.md "Help screen").

Screen already scrolls when its content overflows the terminal (Textual's
default `overflow-y: auto`), so nothing extra is needed for "it must fit and
scroll if the terminal is smaller" (SPEC.md "Help screen").
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import Static

HELP_TEXT = """\
appmem shows memory the kernel can't just drop, grouped per app instead of per process.

RAM       anon + shmem + kernel: memory an app holds that isn't page cache, so the
          kernel can't just free it under pressure.
CACHE     Page cache (file - shmem): reclaimable, hidden by default (press c),
          never counted in TOTAL.
SWAP      memory.swap.current. With zswap enabled this also includes pages held
          compressed in RAM, not only pages written out to a swap device.
TOTAL     SWAP + RAM.

Memory pressure is the share of the last 10 s spent waiting for memory:
  none            almost no waiting: memory isn't the bottleneck right now
  some (X.X %)    some waiting, shown with the percentage
  high            a lot of waiting: memory is the bottleneck right now
  Big SWAP with pressure none just means idle pages were paged out to make room
  for something else. Not a problem by itself.

Δ is the change since the baseline: appmem start, or the last z. Apps that
appear later count from their first sample.

Process rows don't add up to the app row for two reasons: a shared page counts
once in every process that maps it, so rows can sum to more than the app. Memory
the app holds without any process mapping it (GPU buffers, memfd, tmpfs) belongs
to no process, so rows can also fall short. The other row in the process view
is that gap: the app total minus the sum of its processes, clamped at 0.

A closed app can still show a row: helper processes, crash handlers, or a unit
that outlives its processes (PROCS 0) can keep holding memory on their own.

Terminals: anything started from a terminal (node, claude, python, ...) lives in
the terminal's cgroup, so it counts as the terminal. Press g to see what is
really running inside it.

What to do about it:
  close the app normally
  systemctl --user stop UNIT     stop the unit cleanly
  systemctl --user kill UNIT     kill everything in the unit's cgroup
  kill PID                       kill a single process
esc / ? / q   close this screen
"""


class HelpScreen(Screen[None]):
    """Static help text (SPEC.md "Help screen"). Opens from either view."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("?", "close", "close", show=False),
        # Shadows AppMemApp's `q` -> quit binding while this screen is on top:
        # `q` closes the help screen instead of quitting (SPEC.md "Help screen").
        Binding("q", "close", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-text")

    def action_close(self) -> None:
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.pop_screen()  # pyright: ignore[reportUnknownMemberType]
