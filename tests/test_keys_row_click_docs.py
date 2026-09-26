"""Docs-vs-code drift check for the row-click Keys entry (SPEC.md "Keys", run-2
fixes A9): SPEC.md's Keys table, `help.py`'s `_KEYS` and `cli.py`'s `--help`
text must all carry it with the same wording, right after "click header".

Pure text reads (SPEC.md, the source modules); never the live `/sys` or
`/proc`.
"""

from __future__ import annotations

from pathlib import Path

from appmem.cli import _HELP_TEXT  # pyright: ignore[reportPrivateUsage]
from appmem.ui.screens.help import _KEYS  # pyright: ignore[reportPrivateUsage]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _REPO_ROOT / "SPEC.md"

_ROW_CLICK_KEY = "click a row"
_ROW_CLICK_TEXT = (
    "select it, double click opens it (process view, or a command's processes when grouped)"
)


def test_spec_keys_table_lists_row_click_right_after_click_header() -> None:
    spec = _SPEC_PATH.read_text()
    keys_section = spec.split("## Keys", 1)[1].split("## Data sources", 1)[0]
    table_rows = [line for line in keys_section.splitlines() if line.startswith("|")]
    header_index = next(i for i, row in enumerate(table_rows) if "click header" in row)
    assert table_rows[header_index + 1] == f"| {_ROW_CLICK_KEY} | {_ROW_CLICK_TEXT} |"


def test_help_keys_lists_row_click_right_after_click_header() -> None:
    assert _KEYS[0][0] == "click header"
    assert _KEYS[1] == (_ROW_CLICK_KEY, _ROW_CLICK_TEXT)


def test_cli_help_text_lists_row_click_right_after_click_header() -> None:
    lines = _HELP_TEXT.splitlines()
    header_index = next(
        i for i, line in enumerate(lines) if line.strip().startswith("click header")
    )
    # The row wraps onto a second, indented line in `--help`.
    row = " ".join(" ".join(lines[header_index + 1 : header_index + 3]).split())
    assert row == f"{_ROW_CLICK_KEY} {_ROW_CLICK_TEXT}"
