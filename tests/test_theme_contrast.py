"""Table-header contrast (SPEC.md "Main view").

`ansi-dark` on Ghostty's Tokyo Night palette left the header row (APP, RAM,
SWAP, ...) unreadable: Textual's own `&:ansi > .datatable--header` rule
colours it `ansi_bright_blue` on `ansi_default`, and both land on the same
blue on that palette. These tests read the style `DataTable` actually
renders with (`get_component_styles`, not a copy of the CSS rule) for every
built-in theme, so a future CSS change that regresses contrast fails here
instead of only showing up against a real terminal palette.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from rich.color import Color as RichColor
from rich.color_triplet import ColorTriplet
from rich.style import Style
from textual.widgets import DataTable

from appmem.collect import LinuxBackend
from appmem.theme import TERMINAL_THEME_NAMES, THEME_NAMES
from appmem.ui.app import AppMemApp

_MIN_CONTRAST = 4.5

_NON_TERMINAL_THEMES = tuple(name for name in THEME_NAMES if name not in TERMINAL_THEME_NAMES)


def _relative_luminance(rgb: ColorTriplet) -> float:
    # WCAG 2.0: https://www.w3.org/TR/WCAG20/#relativeluminancedef
    channels: list[float] = []
    for value in (rgb.red, rgb.green, rgb.blue):
        fraction = value / 255
        channels.append(
            fraction / 12.92 if fraction <= 0.03928 else ((fraction + 0.055) / 1.055) ** 2.4
        )
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: ColorTriplet, bg: ColorTriplet) -> float:
    lighter, darker = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


async def _header_style(theme_name: str) -> Style:
    app = AppMemApp(
        backend=LinuxBackend(Path("/nonexistent"), 1000),
        interval=100.0,
        include_system=False,
        theme=theme_name,
    )
    async with app.run_test(size=(120, 30)):
        table = cast("DataTable[object]", app.query_one(DataTable))
        return table.get_component_styles("datatable--header").rich_style


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", _NON_TERMINAL_THEMES)
async def test_header_contrast_is_at_least_4_5_to_1(theme_name: str) -> None:
    style = await _header_style(theme_name)
    assert style.color is not None and style.bgcolor is not None
    fg, bg = style.color.triplet, style.bgcolor.triplet
    assert fg is not None and bg is not None
    ratio = _contrast_ratio(fg, bg)
    assert ratio >= _MIN_CONTRAST, f"{theme_name}: header contrast {ratio:.2f} < {_MIN_CONTRAST}"


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", _NON_TERMINAL_THEMES)
async def test_header_stays_bold_in_every_theme(theme_name: str) -> None:
    style = await _header_style(theme_name)
    assert style.bold


@pytest.mark.asyncio
@pytest.mark.parametrize("theme_name", sorted(TERMINAL_THEME_NAMES))
async def test_terminal_theme_header_is_default_on_default_bold_underline(
    theme_name: str,
) -> None:
    style = await _header_style(theme_name)
    assert style.color == RichColor.default()
    assert style.bgcolor == RichColor.default()
    assert style.bold
    assert style.underline


_ANSI_CONVERTED_PROBE = """
from pathlib import Path
from textual.filter import ANSIToTruecolor
from appmem.collect import LinuxBackend
from appmem.ui.app import AppMemApp
app = AppMemApp(
    backend=LinuxBackend(Path("/nonexistent"), 1000),
    interval=100.0,
    include_system=False,
    theme="terminal-dark",
)
print(any(isinstance(f, ANSIToTruecolor) for f in app.get_line_filters()))
"""


@pytest.mark.parametrize("textual_theme", ["terminal-dark", None])
def test_terminal_theme_keeps_the_terminals_own_colours(textual_theme: str | None) -> None:
    # Textual reads `TEXTUAL_THEME` once, at import, so it needs a fresh
    # interpreter. Named there, the terminal theme must still switch off
    # Textual's conversion of ANSI colours to its own RGB palette.
    env = {key: value for key, value in os.environ.items() if key != "TEXTUAL_THEME"}
    if textual_theme is not None:
        env["TEXTUAL_THEME"] = textual_theme
    result = subprocess.run(
        [sys.executable, "-c", _ANSI_CONVERTED_PROBE],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
