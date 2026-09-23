"""Tests for `appmem.render`'s text reports and control-character escaping."""

from __future__ import annotations

from typing import Any

from appmem.render import (
    escape_control_chars,
    render_app_text,
    render_next_line,
    render_snapshot_text,
)

_SNAPSHOT_DOCUMENT: dict[str, Any] = {
    "taken_at": "2026-09-23T00:30:39+02:00",
    "system": {
        "ram_total_bytes": 33223766016,
        "ram_used_bytes": 20875931648,
        "ram_available_bytes": 12347834368,
        "swap_total_bytes": 34342957056,
        "swap_used_bytes": 23782510592,
        "system_services_ram_bytes": 300 * 1024 * 1024,
        "system_services_swap_bytes": 86 * 1024 * 1024,
        "elsewhere_bytes": 213200896,
    },
    "pressure": {
        "level": "none",
        "some_avg10_percent": 0.0,
        "some_avg60_percent": 0.0,
        "full_avg10_percent": 0.0,
        "full_avg60_percent": 0.0,
    },
    "apps": {
        "items": [
            {
                "name": "ghostty",
                "scope": "user",
                "ram_bytes": 6764953600,
                "swap_bytes": 11918725120,
                "total_bytes": 18683678720,
                "cache_bytes": 412090368,
                "procs": 278,
                "units": 34,
            },
        ],
        "has_more": True,
    },
    "next": ["appmem", "app", "ghostty"],
}

_APP_DOCUMENT: dict[str, Any] = {
    "taken_at": "2026-09-23T00:30:41+02:00",
    "name": "ghostty",
    "scope": "user",
    "ram_bytes": 6764953600,
    "swap_bytes": 11918725120,
    "total_bytes": 18683678720,
    "cache_bytes": 412090368,
    "kernel_bytes": 681574400,
    "procs": 278,
    "units": ["app-com.mitchellh.ghostty.service"],
    "processes": {
        "items": [
            {
                "pid": 6091,
                "name": "ghostty",
                "swap_bytes": 3552768000,
                "ram_bytes": 148062208,
                "total_bytes": 3700830208,
                "age_seconds": 7446662.0,
                "unit": "app-com.mitchellh.ghostty.service",
            },
        ],
        "has_more": True,
    },
    "commands": {
        "items": [
            {
                "name": "ghostty",
                "swap_bytes": 4831838208,
                "ram_bytes": 1234173952,
                "total_bytes": 6066012160,
                "procs": 34,
            },
        ],
        "has_more": True,
    },
    "unattributed_ram_bytes": 314572800,
    "unattributed_swap_bytes": 898629632,
}


def test_escape_control_chars_replaces_control_bytes_and_leaves_text_alone() -> None:
    assert escape_control_chars("ghostty") == "ghostty"
    assert escape_control_chars("\x1b[2J") == "\\x1b[2J"
    assert escape_control_chars("line1\nline2") == "line1\\x0aline2"
    assert "\x1b" not in escape_control_chars("\x1b[2Jclear\ndone")


def test_render_next_line_quotes_arguments() -> None:
    assert render_next_line(["appmem", "app", "ghostty"]) == "Next: appmem app ghostty"
    assert render_next_line(["appmem", "app", "a name"]) == "Next: appmem app 'a name'"
    assert "\x1b" not in render_next_line(["appmem", "app", "evil\x1b[2J"])


def test_snapshot_text_has_header_table_cut_notice_and_next_line() -> None:
    text = render_snapshot_text(_SNAPSHOT_DOCUMENT, total_apps=41)

    assert "RAM" in text
    assert "Swap" in text
    assert "pressure 10s: none" in text
    assert "ghostty" in text
    assert "1 of 41 apps shown (--limit 41 shows all)" in text
    assert "Next: appmem app ghostty" in text


def test_snapshot_text_omits_cut_notice_when_nothing_was_cut() -> None:
    document = {**_SNAPSHOT_DOCUMENT, "apps": {**_SNAPSHOT_DOCUMENT["apps"], "has_more": False}}
    document.pop("next")
    text = render_snapshot_text(document, total_apps=1)

    assert "shown (--limit" not in text
    assert "Next:" not in text


def test_snapshot_text_escapes_a_control_character_in_a_name() -> None:
    document = {
        **_SNAPSHOT_DOCUMENT,
        "apps": {
            "items": [
                {
                    "name": "evil\x1b[2Japp",
                    "scope": "user",
                    "ram_bytes": 1,
                    "swap_bytes": 1,
                    "total_bytes": 2,
                    "cache_bytes": 0,
                    "procs": 1,
                    "units": 1,
                }
            ],
            "has_more": False,
        },
    }
    document.pop("next")

    text = render_snapshot_text(document, total_apps=1)

    assert "\x1b" not in text
    assert "\\x1b[2Japp" in text


def test_app_text_has_title_both_tables_and_kernel_and_unattributed_lines() -> None:
    text = render_app_text(_APP_DOCUMENT, total_processes=283, total_commands=12)

    assert "ghostty" in text
    assert "user" in text
    assert "278 procs" in text
    assert "PID" in text and "UNIT" in text
    assert "COMMAND" in text and "PROCS" in text
    assert "kernel" in text and "charged kernel memory" in text
    assert "unattributed" in text and "accounting difference" in text


def test_app_text_shows_the_real_totals_and_how_to_see_everything() -> None:
    text = render_app_text(_APP_DOCUMENT, total_processes=283, total_commands=12)

    assert "1 of 283 processes shown (--limit 283 shows all)" in text
    assert "1 of 12 commands shown (--limit 12 shows all)" in text


def test_app_text_renders_a_unit_name_containing_an_escape_and_a_newline() -> None:
    document: dict[str, Any] = {
        **_APP_DOCUMENT,
        "processes": {
            "items": [
                {
                    "pid": 1,
                    "name": "evil\nname",
                    "swap_bytes": 0,
                    "ram_bytes": 0,
                    "total_bytes": 0,
                    "age_seconds": 1.0,
                    "unit": "unit\x1b[2Jscope",
                }
            ],
            "has_more": False,
        },
        "commands": {"items": [], "has_more": False},
    }

    text = render_app_text(document, total_processes=1, total_commands=0)

    assert "\x1b" not in text
    assert "\\x1b[2J" in text
    assert "\\x0a" in text
