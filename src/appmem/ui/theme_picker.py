"""Theme panel (SPEC.md "Command line", "Main view"): `T` / `Ctrl+P` ->
Theme opens `ThemePanel`, a small panel docked to the right, instead of
Textual's own `CommandPalette`/`ThemeProvider` pair, which always opened
full width (covering most of the app), started on the alphabetically first
theme, and only applied a pick on Enter -- no way to see a theme before
picking it.

An own `ModalScreen` holding an `OptionList` covers it: no fuzzy search
(~20 names fit without one, so arrows/PgUp/PgDn/Home/End are enough), no
dimming (`background: transparent` overrides `ModalScreen`'s own 60% dim --
the rest of the app stays visible behind the panel, since that's where the
live preview shows), and every highlight change applies the theme to the
app at once instead of waiting for Enter. This also removes the app's only
dependency on a Textual internal: the previous version overrode the private
`CommandPalette._refresh_command_list` to seed the initial highlight,
which Textual's own `OptionList.highlighted` now does directly.
"""

from __future__ import annotations

from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from appmem.theme import TERMINAL_THEME_NAMES, THEME_NAMES

_CURRENT_MARK = "✓ "
_NO_MARK = "  "
"""Same cell width as `_CURRENT_MARK`, so every row's theme name still lines
up whether or not it's the current one."""

_TITLE = "Theme"
_TERMINAL_INFO = "your terminal's colours"
_HINT_MOVE = "↑↓ preview"
_HINT_KEYS = "enter keep  esc cancel"
"""Two lines rather than one: joined, they are 35 cells and would crop at
every panel width. Each fits the panel's ~24-cell text width on its own."""

_SCROLLBAR_WIDTH = 2

_PANEL_WIDTH = max(len(name) for name in THEME_NAMES) + len(_CURRENT_MARK) + 4 + _SCROLLBAR_WIDTH
"""As narrow as the longest theme name, the mark, the option list's own
`0 1` padding, its scrollbar and the panel's own `round` border allow
(SPEC.md "Main view": the rest of the app stays visible, not just dimmed).
The scrollbar counts too: on a terminal too short for the whole list it
takes its cells from the names, and the longest one would wrap."""


def _labelled(name: str, *, current: bool) -> str:
    return (_CURRENT_MARK if current else _NO_MARK) + name


class ThemePanel(ModalScreen[None]):
    """Right-docked, full-height, live-preview theme panel.

    Every highlight change applies that theme to the app at once
    (`AppMemApp.watch_theme` re-renders the themed screens on every
    change); only Enter, or a click on an option, persists the pick, to
    the config file when it differs from the file's own current value.
    Esc, `T` pressed again, or a click outside the panel restores the
    theme that was running when the panel opened and writes nothing.
    """

    DEFAULT_CSS = f"""
    ThemePanel {{
        background: transparent;
    }}
    ThemePanel > #panel {{
        dock: right;
        width: {_PANEL_WIDTH};
        height: 100%;
        border: round $primary;
        background: $panel;
    }}
    ThemePanel #panel-title {{
        height: 1;
        padding: 0 1;
        text-align: center;
        text-style: bold;
    }}
    ThemePanel #theme-list {{
        height: 1fr;
        border: none;
        scrollbar-size-vertical: {_SCROLLBAR_WIDTH};
        background: transparent;
    }}
    ThemePanel .panel-line {{
        height: 1;
        padding: 0 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    ThemePanel #panel-info {{
        text-style: dim;
    }}
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "cancel", show=False),
        # `T` while the panel is open does the same as Esc, rather than
        # bubbling up to `AppMemApp`'s own binding and pushing a second
        # panel on top of this one (SPEC.md "Command line").
        Binding("T", "cancel", "cancel", show=False),
        # `q` on its own, not `AppMemApp`'s `Binding("q", "quit", ...)`:
        # `ModalScreen` blocks that App-level binding since it isn't
        # `priority=True` (unlike `ctrl+c`), so without this `q` did nothing
        # while the panel was open, right next to the main footer's own
        # visible `q quit` (SPEC.md "Theme panel": quitting here saves
        # nothing, same as Esc).
        Binding("q", "quit_app", "quit", show=False),
    ]

    def __init__(self, current_theme: str) -> None:
        super().__init__()
        self._original_theme = current_theme
        """The theme running when the panel opened: restored on cancel, and
        the baseline the cursor opens on and marks `✓`."""

    def compose(self) -> ComposeResult:
        with Vertical(id="panel"):
            yield Static(_TITLE, id="panel-title")
            yield OptionList(
                *(
                    Option(_labelled(name, current=name == self._original_theme), id=name)
                    for name in THEME_NAMES
                ),
                id="theme-list",
            )
            yield Static("", id="panel-info", classes="panel-line")
            yield Static(_HINT_MOVE, id="panel-hint-move", classes="panel-line")
            yield Static(_HINT_KEYS, id="panel-hint-keys", classes="panel-line")

    def on_mount(self) -> None:
        option_list = self.query_one("#theme-list", OptionList)
        option_list.highlighted = THEME_NAMES.index(self._original_theme)
        option_list.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._apply(event.option_id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._apply(event.option_id)
        self.app.persist_theme(  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            event.option_id
        )
        self._close()

    def on_click(self, event: events.Click) -> None:
        # Bubbles here from every descendant (`OptionList` included) since
        # nothing below stops it; `event.widget` is the widget actually
        # under the pointer, not whichever node is handling the bubble, so
        # this still tells inside from outside correctly either way.
        panel = self.query_one("#panel")
        if event.widget is None or panel not in event.widget.ancestors_with_self:
            self.action_cancel()

    def action_cancel(self) -> None:
        if self not in self.app.screen_stack:  # pyright: ignore[reportUnknownMemberType]
            return  # a second event queued behind the one that already closed the panel
        self.app.theme = self._original_theme  # pyright: ignore[reportUnknownMemberType]
        self._close()

    def _close(self) -> None:
        # A double click queues two events for this screen before the first
        # one's dismissal runs; the second must not pop the screen below.
        if self in self.app.screen_stack:  # pyright: ignore[reportUnknownMemberType]
            self.dismiss()

    def action_quit_app(self) -> None:
        # `persist_theme` is only ever called from `on_option_list_option_
        # selected` (Enter or a click), never from here, so quitting saves
        # nothing regardless of how far the live preview has moved from the
        # theme the panel opened with (SPEC.md "Theme panel").
        self.app.exit()  # pyright: ignore[reportUnknownMemberType]

    def _apply(self, theme_name: str | None) -> None:
        assert theme_name is not None  # every `Option` above is built with an `id`
        self.app.theme = theme_name  # pyright: ignore[reportUnknownMemberType]
        self._update_info_line(theme_name)

    def _update_info_line(self, theme_name: str) -> None:
        # Fixed height either way (`.panel-line` CSS above): blank rather
        # than removed, so the hint lines below never move.
        text = _TERMINAL_INFO if theme_name in TERMINAL_THEME_NAMES else ""
        self.query_one("#panel-info", Static).update(text)
