"""The Textual application shell (SPEC.md "Tech", "Command line").

Owns the collector config (root/uid/interval/system toggle) and opens the main
view. Screens hold the interactive state; this class only rebinds the quit
keys, since Textual's own default leaves `q` unbound and turns `ctrl+c` into a
"press q to quit" notice instead of quitting (SPEC.md "Tech" notes).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from appmem.ui.screens.main import MainScreen


class AppMemApp(App[None]):
    """Root app: bootstraps config, opens `MainScreen`, quits on `q`/`Ctrl+C`."""

    TITLE = "appmem"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit", show=False),
        # `priority=True` so this wins over the base App's `ctrl+c` -> "how to
        # quit" notice binding (SPEC.md: "Ctrl+C typed in the TUI is a key and
        # quits with exit 0").
        Binding("ctrl+c", "quit", "quit", show=False, priority=True),
    ]

    def __init__(self, *, root: Path, uid: int, interval: float, include_system: bool) -> None:
        super().__init__()
        self._root = root
        self._uid = uid
        self._interval = interval
        self._include_system = include_system

    def get_default_screen(self) -> MainScreen:
        return MainScreen(
            root=self._root,
            uid=self._uid,
            interval=self._interval,
            include_system=self._include_system,
        )
