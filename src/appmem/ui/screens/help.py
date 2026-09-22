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
appmem groups memory the kernel already tracks per app cgroup, instead of per process.

RAM       Anonymous + shared + charged kernel memory, excluding file cache.
CACHE     Page cache (file - shmem): reclaimable, hidden by default (press c),
          never counted in TOTAL.
SWAP      memory.swap.current. With zswap enabled this also includes pages held
          compressed in RAM, not only pages written out to a swap device.
TOTAL     SWAP + RAM: an accounting sum, not a prediction of what closing the
          app would free.

The header counts the whole machine (RAM/Swap/avail). Rows only cover the app
trees appmem walks, plus the hidden system and elsewhere totals -- they won't
add up to the header, and that's expected.

Memory pressure is the share of the last 10 s spent waiting for memory:
  none            few memory stalls in the last 10 s
  some (X.X %)    some waiting, shown with the percentage
  high            a lot of waiting: memory stalls are happening, but this
                  alone doesn't say which app is causing them
  Big SWAP with pressure none just means idle pages were paged out to make room
  for something else. Not a problem by itself.

Δ is the change since the baseline: appmem start, or the last z. Apps that
appear later count from their first sample; an app that closes and reopens
starts a fresh Δ instead of comparing against its old instance.

Process rows don't add up to the app row for two reasons: a shared page counts
once in every process that maps it, so rows can sum to more than the app. Memory
the app holds without any process mapping it (GPU buffers, memfd, tmpfs) belongs
to no process, so rows can also fall short. Per-process values here also leave
out file-backed pages, so they read smaller than htop's RES.
Two rows cover the gap: kernel is the app's own page tables, slab and stacks
(exact, part of the RAM total above); unattributed is everything else left
over -- an accounting difference, not a process.

A closed app can still show a row: helper processes, crash handlers, or a unit
that outlives its processes (PROCS 0) can keep holding memory on their own.

Terminals: anything started from a terminal (node, claude, python, ...) lives in
the terminal's cgroup, so it counts as the terminal. Press g to see what is
really running inside it.

System rows (toggled with x) show as "name [sys]": a user service and a
same-named system service stay two separate rows, with separate Δ.

What to do about it:
  close the app normally
  systemctl --user stop UNIT     stop a user unit cleanly
  systemctl --user kill UNIT     kill everything in a user unit's cgroup
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
