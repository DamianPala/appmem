"""Tests for `appmem.ui.screens.help`'s body text (SPEC.md "Help screen")."""

from __future__ import annotations

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


# --- zswap definitions, only when this session has zswap (SPEC.md "Main view") --


def test_body_omits_zswap_definitions_by_default() -> None:
    body = _build_body(200)

    # The SWAP definition already mentions zswap in passing (pre-existing);
    # what must be absent is the ZSWAP column and the header-bracket note.
    assert "ZSWAP" not in body
    assert "zswap in Y" not in body
    assert "overflowing to the disk swap" not in body


def test_body_includes_zswap_definitions_when_enabled() -> None:
    body = _build_body(200, zswap_enabled=True)

    assert "ZSWAP" in body
    assert "not extra memory" in body
    assert "zswap in Y" in body
    assert "overflowing to the disk swap" in body
