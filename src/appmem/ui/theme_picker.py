"""Theme picker (SPEC.md "Command line"): Textual 8.2.8's own `ThemeProvider`/
`CommandPalette` (what `App.search_themes` pushes) always opens with the
alphabetically first theme highlighted and gives no hint which theme is
already running. `AppMemApp.search_themes` pushes `ThemePalette` instead,
which starts the cursor on the current theme and marks it -- everything else
(fuzzy search, the full theme list, apply-and-close on Enter, Esc cancels and
writes nothing) is untouched, inherited straight from Textual's own classes.

Two small subclasses cover it, the least invasive route available: Textual
has no hook to seed the initial highlight or annotate a hit's display text,
so `ThemePalette` overrides `CommandPalette._refresh_command_list` (the one
place that sets the initial highlight) to move it, reading the option list
back through `query_one`/`options` -- the same public API a caller outside
Textual would use -- rather than trusting the private method's own
parameters. `pyproject.toml` puts no upper bound on `textual`, so this is
the one place in the app leaning on an internal: the override accepts any
signature and forwards it to `super()` unchanged, and any shape it doesn't
recognise leaves the default highlight (the first theme) instead of raising,
so a future Textual rename or signature change degrades this one detail
rather than crashing the app on `T`. `MarkedThemeProvider` wraps
`ThemeProvider`'s own `discover`/`search` to prefix a mark, leaving
`Hit.text` (used to match a hit back to its theme) exactly as Textual sets
it.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from rich.text import Text
from textual.command import Command, CommandList, CommandPalette, DiscoveryHit, Hit, Hits
from textual.content import Content
from textual.theme import ThemeProvider
from textual.visual import VisualType

_CURRENT_MARK = "✓ "
_NO_MARK = "  "
"""Same cell width as `_CURRENT_MARK`, so every row's theme name still lines
up whether or not it's the current one."""


def _marked(display: VisualType, *, current: bool) -> Content:
    # A search hit's display is `Content` carrying the fuzzy-match highlight
    # as spans; going through `str()` would drop it.
    mark = _CURRENT_MARK if current else _NO_MARK
    if isinstance(display, Text):
        return mark + Content.from_rich_text(display)
    if isinstance(display, (str, Content)):
        return mark + Content.from_markup(display)
    return mark + Content(str(display))


class MarkedThemeProvider(ThemeProvider):
    """`ThemeProvider`, with a mark on the app's current theme."""

    async def discover(self) -> Hits:
        current = self.app.theme
        async for hit in super().discover():
            assert isinstance(hit, DiscoveryHit)
            hit.display = _marked(hit.display, current=hit.text == current)
            yield hit

    async def search(self, query: str) -> Hits:
        current = self.app.theme
        async for hit in super().search(query):
            assert isinstance(hit, Hit)
            hit.match_display = _marked(hit.match_display, current=hit.text == current)
            yield hit


class ThemePalette(CommandPalette):
    """`CommandPalette`, opening with the cursor already on the app's
    current theme instead of always the first one alphabetically."""

    def _refresh_command_list(self, *args: Any, **kwargs: Any) -> None:
        # `*args`/`**kwargs`, forwarded unchanged: whatever signature this
        # private method has in the installed Textual, `super()` gets called
        # with it exactly as Textual itself called us. See the module
        # docstring for why.
        super()._refresh_command_list(*args, **kwargs)  # pyright: ignore[reportPrivateUsage]
        # Unrecognised shape: leave whatever Textual's own logic just highlighted.
        with suppress(Exception):
            self._highlight_current_theme()

    def _highlight_current_theme(self) -> None:
        # Reads the option list back through public API (`query_one`,
        # `options`) instead of trusting this private method's own
        # arguments, which Textual could rename or reshape independently.
        command_list = self.query_one(CommandList)
        options = list(command_list.options)
        if not options or not isinstance(options[0], Command):
            return
        if not isinstance(options[0].hit, DiscoveryHit):
            return  # a typed search, not the full list: leave the top match highlighted
        current = self.app.theme  # pyright: ignore[reportUnknownMemberType]
        for index, option in enumerate(options):
            if isinstance(option, Command) and option.hit.text == current:
                command_list.highlighted = index
                return
