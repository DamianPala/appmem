"""The Textual application shell (SPEC.md "Tech", "Command line").

Owns the collector config (root/uid/interval/system toggle) and opens the main
view. Screens hold the interactive state; this class only rebinds the quit
keys, since Textual's own default leaves `q` unbound and turns `ctrl+c` into a
"press q to quit" notice instead of quitting (SPEC.md "Tech" notes). It also
owns the theme: applying the resolved startup value, persisting an in-app
choice, and re-rendering the main view's header when it changes (SPEC.md
"Command line", "Main view").
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from appmem.theme import TEXTUAL_BUILTIN_DEFAULT, config_path, write_config_theme
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
        # `action_change_theme` (inherited from `App`) opens Textual's own
        # theme picker -- a `CommandPalette` scoped to `ThemeProvider`, which
        # only ever lists `App.available_themes` (the built-ins here, since
        # nothing registers a custom one). Bound at the app level, not on a
        # screen, so it works the same from the main view and the process
        # view (SPEC.md "Command line"). `T`, not `t`: that stays "sort
        # TOTAL" on both screens.
        Binding("T", "change_theme", "theme", show=False),
    ]

    def __init__(
        self,
        *,
        root: Path,
        uid: int,
        interval: float,
        include_system: bool,
        theme: str = TEXTUAL_BUILTIN_DEFAULT,
        config_theme: str | None = None,
        theme_warnings: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._root = root
        self._uid = uid
        self._interval = interval
        self._include_system = include_system
        self.cgroup_error_message: str | None = None
        """Set by a screen when the cgroup tree vanishes mid-tick; `cli._run_app`
        prints the JSON `cgroup_unavailable` line after Textual restores the
        terminal (SPEC.md "Errors")."""
        self._config_theme = config_theme
        """The config file's own last-known-good theme (or `None`), the
        baseline `watch_theme` compares a later in-app pick against --
        independent of `theme` below, which may come from `--theme`/
        `APPMEM_THEME` and never gets written back."""
        self._startup_theme_warnings = tuple(theme_warnings)
        self._theme_ready = False
        """Guards `watch_theme` against the startup assignment right below:
        only a change made *after* the app has mounted (an in-app pick, via
        `T`/Ctrl+P) is a user choice worth persisting."""
        self.theme = theme

    def on_mount(self) -> None:
        self._theme_ready = True
        for message in self._startup_theme_warnings:
            self.notify(message, severity="warning", timeout=8)

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        # Fires for the startup assignment above too (guarded off by
        # `_theme_ready`) and for `action_change_theme`'s picker, which only
        # ever sets `App.theme` once, on Enter -- Textual's own
        # `CommandPalette`/`ThemeProvider` has no live-preview-on-highlight
        # in this version, so Esc (which never touches `App.theme`) already
        # "reverts and writes nothing" for free.
        if not self._theme_ready:
            return
        self._persist_theme(new_theme)
        self._refresh_themed_screens()

    def _persist_theme(self, theme_name: str) -> None:
        if theme_name == self._config_theme:
            return  # already the file's own value: nothing to write
        error = write_config_theme(config_path(), theme_name)
        if error is not None:
            self.notify(error, severity="error", timeout=8)
            return
        self._config_theme = theme_name

    def _refresh_themed_screens(self) -> None:
        # Only `MainScreen` colours anything from the theme (pressure word,
        # swap fraction); it's always mounted (the default screen), whether
        # or not it's the one currently on top.
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                screen.refresh_theme()

    def fail_cgroup_unavailable(self, message: str) -> None:
        """Record a vanished cgroup tree and exit (SPEC.md "Errors"): no
        traceback, exit code 1, the JSON error line printed by the caller of
        `run()` once the terminal is restored."""
        self.cgroup_error_message = message
        self.exit(return_code=1)

    def get_default_screen(self) -> MainScreen:
        return MainScreen(
            root=self._root,
            uid=self._uid,
            interval=self._interval,
            include_system=self._include_system,
        )
