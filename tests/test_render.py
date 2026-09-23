"""Tests for `appmem.render`'s text reports and control-character escaping."""

from __future__ import annotations

from typing import Any

from rich.cells import cell_len

from appmem.fmt import size
from appmem.render import (
    _pad_cell,  # pyright: ignore[reportPrivateUsage]
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
        "ram_free_bytes": 6347834368,
        "ram_cache_bytes": 6000000000,
        "ram_slab_bytes": 2100000000,
        "ram_shared_bytes": 3221225472,
        "swap_total_bytes": 34342957056,
        "swap_used_bytes": 23782510592,
        "system_services_ram_bytes": 300 * 1024 * 1024,
        "system_services_swap_bytes": 86 * 1024 * 1024,
        "elsewhere_bytes": 213200896,
        "zswap_enabled": False,
        "zswap_pool_bytes": None,
        "zswapped_bytes": None,
        "zswap_writeback_bytes": None,
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
                "unit_count": 34,
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


def test_escape_control_chars_replaces_c1_controls_and_keeps_text_above_them() -> None:
    assert escape_control_chars("evil\x9b31mred\x85") == "evil\\x9b31mred\\x85"
    assert escape_control_chars("café 微信") == "café 微信"


def test_render_next_line_quotes_arguments() -> None:
    assert render_next_line(["appmem", "app", "ghostty"]) == "Next: appmem app ghostty"
    assert render_next_line(["appmem", "app", "a name"]) == "Next: appmem app 'a name'"
    assert "\x1b" not in render_next_line(["appmem", "app", "evil\x1b[2J"])


def test_snapshot_text_shows_the_short_pressure_qualifier() -> None:
    pressure = {**_SNAPSHOT_DOCUMENT["pressure"], "some_avg60_percent": 3.2}
    text = render_snapshot_text({**_SNAPSHOT_DOCUMENT, "pressure": pressure}, total_apps=41)

    assert "pressure 10s: none (was 3.2 %)" in text


def test_snapshot_text_has_header_table_cut_notice_and_next_line() -> None:
    text = render_snapshot_text(_SNAPSHOT_DOCUMENT, total_apps=41)

    assert "RAM" in text
    assert "Swap" in text
    assert "pressure 10s: none" in text
    assert "ghostty" in text
    assert "1 of 41 apps shown (--limit 41 shows all)" in text
    assert "Next: appmem app ghostty" in text


def test_snapshot_text_header_line_shows_shared_and_avail_breakdown() -> None:
    text = render_snapshot_text(_SNAPSHOT_DOCUMENT, total_apps=41)
    header_line = text.splitlines()[0]

    assert "shared" in header_line
    assert "free" in header_line
    assert "cache" in header_line
    assert header_line.index("RAM") < header_line.index("shared")
    assert header_line.index("avail") < header_line.index("free")


def test_snapshot_text_header_shows_zswap_bracket_when_enabled() -> None:
    document = {
        **_SNAPSHOT_DOCUMENT,
        "system": {
            **_SNAPSHOT_DOCUMENT["system"],
            "zswap_enabled": True,
            "zswap_pool_bytes": int(1.9 * 1024**3),
            "zswapped_bytes": int(6.7 * 1024**3),
        },
    }
    text = render_snapshot_text(document, total_apps=41)
    header_line = text.splitlines()[0]

    assert "6.7 zswapped into 1.9 GiB RAM" in header_line
    assert "to disk" not in header_line  # a one-shot snapshot never has a rate


def test_snapshot_text_header_omits_zswap_bracket_when_disabled() -> None:
    text = render_snapshot_text(_SNAPSHOT_DOCUMENT, total_apps=41)
    header_line = text.splitlines()[0]

    assert "zswap" not in header_line


def test_snapshot_table_column_order_is_app_ram_swap_total() -> None:
    text = render_snapshot_text(_SNAPSHOT_DOCUMENT, total_apps=1)
    table_header = text.splitlines()[1]

    assert table_header.index("APP") < table_header.index("RAM")
    assert table_header.index("RAM") < table_header.index("SWAP")
    assert table_header.index("SWAP") < table_header.index("TOTAL")


def test_app_process_and_command_table_column_order_is_ram_before_swap() -> None:
    text = render_app_text(_APP_DOCUMENT, total_processes=1, total_commands=1)
    lines = text.splitlines()
    process_header = next(line for line in lines if "PID" in line)
    command_header = next(line for line in lines if "COMMAND" in line)

    assert process_header.index("RAM") < process_header.index("SWAP")
    assert command_header.index("RAM") < command_header.index("SWAP")


def test_app_title_line_shows_ram_before_swap() -> None:
    text = render_app_text(_APP_DOCUMENT, total_processes=1, total_commands=1)
    title_line = text.splitlines()[0]

    assert title_line.index("RAM") < title_line.index("swap")


def test_snapshot_text_header_line_shows_slab_in_avail_breakdown() -> None:
    # slab (SReclaimable) joins free/cache inside avail's breakdown.
    text = render_snapshot_text(_SNAPSHOT_DOCUMENT, total_apps=41)
    header_line = text.splitlines()[0]

    assert "slab" in header_line
    assert header_line.index("free") < header_line.index("cache") < header_line.index("slab")
    assert size(2100000000) in header_line


def test_snapshot_table_cjk_app_name_column_stays_aligned() -> None:
    # The name column is padded by terminal cells (`set_cell_size`), not
    # code points -- a CJK name (2 cells/char) padded by code points would
    # push every column after it out of alignment.
    document = {
        **_SNAPSHOT_DOCUMENT,
        "apps": {
            "items": [
                {
                    "name": "微信",
                    "scope": "user",
                    "ram_bytes": 1,
                    "swap_bytes": 1,
                    "total_bytes": 2,
                    "cache_bytes": 0,
                    "procs": 1,
                    "unit_count": 1,
                }
            ],
            "has_more": False,
        },
    }
    document.pop("next")

    text = render_snapshot_text(document, total_apps=1)
    lines = text.splitlines()
    row_line = next(line for line in lines if "微信" in line)

    # The name column is padded to exactly 24 terminal cells (`_pad_cell`),
    # even though the CJK name has fewer *characters* than an ASCII name
    # would (each CJK char is 1 code point but 2 cells) -- a `str.format`
    # `:<24` padding would count code points and leave the numeric columns
    # after it shifted right by the difference.
    numbers = f"{size(1):>10}{size(1):>10}{size(2):>10}{1:>7}{1:>7}"
    assert row_line == _pad_cell("微信", 24) + numbers


def test_pad_cell_pads_cjk_text_by_cells_not_code_points() -> None:
    # Direct unit test of the padding helper: 2 CJK chars (4 cells) padded to
    # 10 cells must add 6 cells of trailing space, i.e. 6 space characters --
    # not `10 - len("微信") == 8` spaces, which is what `str.format` would add.
    padded = _pad_cell("微信", 10)

    assert cell_len(padded) == 10
    assert padded == "微信" + " " * 6


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
                    "unit_count": 1,
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
