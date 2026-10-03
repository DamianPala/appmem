"""Tests for `appmem.ui.screens.help`'s body text (SPEC.md "Help screen")."""

from __future__ import annotations

import re

from appmem import cli as cli_module
from appmem.ui.screens.darwin import DarwinHelpScreen
from appmem.ui.screens.help import _build_body  # pyright: ignore[reportPrivateUsage]


def test_body_explains_slab_as_part_of_avail_not_an_exact_sum() -> None:
    # slab (SReclaimable) joins free/cache in the avail explanation, with
    # the same "not an exact sum" caveat.
    body = _build_body(200)

    assert "slab" in body
    assert "kernel caches of file names and inodes" in body
    assert "not an exact sum" in body
    assert "free" in body and "cache" in body


def test_body_mentions_theme_key_and_the_config_file() -> None:
    body = _build_body(200)

    assert "T (or Ctrl+P)" in body
    assert "config.toml" in body


def test_body_mentions_the_last_b_not_the_last_z_for_the_delta_baseline() -> None:
    # `b` took over "reset Δ" from `z`, which now sorts by ZSWAP (SPEC.md
    # "Main view").
    body = _build_body(200)

    assert "the last b" in body
    assert "the last z" not in body


# --- zswap definitions, only when this session has zswap (SPEC.md "Main view") --


def test_body_omits_zswap_definitions_by_default() -> None:
    body = _build_body(200)

    # The SWAP definition already mentions zswap in passing (pre-existing);
    # what must be absent is the ZSWAP column and the header-bracket note.
    assert "ZSWAP" not in body
    assert "AFTER compression" not in body
    assert "writeback is part of swap out" not in body


def test_body_includes_zswap_definitions_when_enabled() -> None:
    body = _build_body(200, zswap_enabled=True)

    assert "ZSWAP" in body
    assert "not extra memory" in body
    assert "AFTER compression" in body
    assert "writeback is part of swap out" in body


def test_body_always_explains_the_bar_glyphs() -> None:
    # Unlike the zswap note, the bars are on every machine's RAM/Swap lines,
    # so this isn't gated on `zswap_enabled`.
    body = _build_body(200)

    assert "'█' is used" in body
    assert "avail for RAM, free for Swap" in body


def test_body_ends_with_the_key_list() -> None:
    # SPEC.md "Help screen": the key list closes the body, one line per key,
    # so `?` answers "which key does what" as well as "what does this number mean".
    body = _build_body(200)
    keys_block = body.split("Keys:")[-1]

    assert body.count("Keys:") == 1
    for key in (
        "click header",
        "r / s / t / d",
        "up/down PgUp PgDn",
        "Home End",
        "Enter",
        "g",
        "Esc",
        "c",
        "x",
        "b",
        "T / Ctrl+P",
        "?",
        "q",
    ):
        assert f"  {key} " in keys_block
    assert "  z " not in keys_block
    assert "  w " not in keys_block


def test_key_list_adds_z_and_w_only_with_zswap() -> None:
    body = _build_body(200, zswap_enabled=True)
    keys_block = body.split("Keys:")[-1]

    assert "sort by ZSWAP" in keys_block
    assert "toggle the ZSWAP column" in keys_block
    # `z` sits with the sort keys, `w` right after `c` (the other column toggle).
    assert keys_block.index("  r / s / t / d") < keys_block.index("  z ")
    assert keys_block.index("  c ") < keys_block.index("  w ") < keys_block.index("  x ")


def test_body_explains_the_none_was_qualifier() -> None:
    body = _build_body(200)

    assert "'none (was X.X %)' means stalls just stopped" in body


def test_both_helps_list_theme_before_host_dashboard_as_the_spec_does() -> None:
    linux = _build_body(200).split("Keys:")[-1]
    mac = DarwinHelpScreen._body(200).split("Keys:")[-1]  # pyright: ignore[reportPrivateUsage]
    for keys in (linux, mac):
        host_key = re.search(r"^\s*h\s{2,}", keys, re.MULTILINE)
        assert host_key is not None
        assert keys.index("T / Ctrl+P") < host_key.start()
    darwin_help = cli_module._DARWIN_HELP_TEXT  # pyright: ignore[reportPrivateUsage]
    assert darwin_help.index("T/Ctrl+P theme") < darwin_help.index("h main dashboard")


def test_mac_help_ties_memory_to_activity_monitor_and_warns_that_ram_reads_higher() -> None:
    body = " ".join(DarwinHelpScreen._body(200).split())  # pyright: ignore[reportPrivateUsage]

    assert "Activity Monitor's Memory column shows per process" in body
    assert "holds back at boot, so it reads higher than Activity Monitor" in body
    assert "session total" not in body and "AppMem started" not in body
