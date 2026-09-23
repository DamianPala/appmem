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

from appmem.theme import (
    TERMINAL_THEME_NAMES,
    TERMINAL_THEMES,
    TEXTUAL_BUILTIN_DEFAULT,
    config_path,
    write_config_theme,
)
from appmem.ui.screens.main import MainScreen
from appmem.ui.theme_picker import ThemePanel


class AppMemApp(App[None]):
    """Root app: bootstraps config, opens `MainScreen`, quits on `q`/`Ctrl+C`."""

    TITLE = "appmem"
    CSS: ClassVar[str] = """
    DataTable > .datatable--header {
        color: auto;
    }
    DataTable:ansi > .datatable--header {
        background: ansi_default;
        color: ansi_default;
        text-style: bold underline;
    }
    """
    """Every `DataTable` in the app (main view, process view, its grouped and
    drill-down modes -- one rule covers all of them, SPEC.md "Main view").

    `color: auto` picks black or white against whatever `background: $panel`
    (the base rule's own, kept) resolves to -- measured >= 6.5:1 across every
    built-in theme, the simplest fix that needs no per-theme special-casing.
    Terminal themes (`:ansi`, `theme.ansi` -- `terminal-*` included, since
    they're `ansi-dark`/`ansi-light` under a new name) get the terminal's own
    default-on-default instead of a coloured bar: the one pair every palette
    keeps readable. `text-style: bold underline` replaces the
    base rule's `bold` alone, since there's no colour left to mark the header
    row."""
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "quit", show=False),
        # `priority=True` so this wins over the base App's `ctrl+c` -> "how to
        # quit" notice binding (SPEC.md: "Ctrl+C typed in the TUI is a key and
        # quits with exit 0").
        Binding("ctrl+c", "quit", "quit", show=False, priority=True),
        # `action_change_theme` (inherited from `App`) calls `search_themes`
        # (overridden below) to open the theme panel. Bound at the app level,
        # not on a screen, so it works the same from the main view and the
        # process view (SPEC.md "Command line"). `T`, not `t`: that stays
        # "sort TOTAL" on both screens.
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
        baseline `persist_theme` compares a kept pick against --
        independent of `theme` below, which may come from `--theme`/
        `APPMEM_THEME` and never gets written back."""
        self._startup_theme_warnings = tuple(theme_warnings)
        self._theme_ready = False
        """Guards `watch_theme` against the startup assignment right below,
        which fires before any screen exists to re-render."""
        for terminal_theme in TERMINAL_THEMES:
            self.register_theme(terminal_theme)
        if self.theme == theme and theme in TERMINAL_THEME_NAMES:
            # `TEXTUAL_THEME` already names it, so Textual read it before it
            # was registered and set itself up for its own fallback theme;
            # assigning the same name again wouldn't re-apply anything.
            self.theme = TEXTUAL_BUILTIN_DEFAULT
        self.theme = theme

    def on_mount(self) -> None:
        self._theme_ready = True
        for message in self._startup_theme_warnings:
            self.notify(message, severity="warning", timeout=8)

    def search_themes(self) -> None:
        # Overrides `App.search_themes`, which `action_change_theme` (`T`,
        # and Ctrl+P -> Theme, SPEC.md "Command line") calls either way:
        # pushes our own `ThemePanel` (small, right-docked, live preview)
        # instead of Textual's own `CommandPalette`/`ThemeProvider` pair
        # (see `theme_picker.py`).
        # Ctrl+P -> Theme still reaches here while a panel is open; a second
        # panel would start from the previewed theme, so a pick kept there
        # could be saved and then undone by the first panel's own cancel.
        if any(isinstance(screen, ThemePanel) for screen in self.screen_stack):
            return
        self.push_screen(ThemePanel(self.theme))

    def watch_theme(self, old_theme: str, new_theme: str) -> None:
        # Fires for the startup assignment above too (guarded off by
        # `_theme_ready`) and for every `ThemePanel` highlight, a preview
        # that must never be saved: `persist_theme` below is a separate step
        # the panel takes only on Enter or a click, so this watcher only
        # re-renders the themed screens' colours.
        if not self._theme_ready:
            return
        self._refresh_themed_screens()

    def persist_theme(self, theme_name: str) -> None:
        """Write `theme_name` to the config file, unless it's already the
        file's own value. Called by `ThemePanel` on confirm -- the only
        place a theme is ever persisted (SPEC.md "Command line")."""
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
