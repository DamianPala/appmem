"""Tests for `appmem.theme` (SPEC.md "Command line": theme persistence).

`XDG_CONFIG_HOME` is pointed at a private tmp dir for every test in this
suite (`tests/conftest.py`, autouse), so `config_path()` here never resolves
anywhere near a real `~/.config` even when a test doesn't pass its own path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from appmem.theme import (
    TERMINAL_THEME_NAMES,
    TERMINAL_THEMES,
    THEME_NAMES,
    canonical_theme_name,
    config_path,
    read_config_theme,
    resolve_theme,
    write_config_theme,
)

# --- config_path ---------------------------------------------------------------


def test_config_path_uses_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert config_path() == tmp_path / "appmem" / "config.toml"


def test_config_path_defaults_to_dot_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert config_path() == tmp_path / ".config" / "appmem" / "config.toml"


# --- read_config_theme -----------------------------------------------------------


def test_read_config_theme_missing_file_is_quietly_none(tmp_path: Path) -> None:
    theme, warning = read_config_theme(tmp_path / "no-such" / "config.toml")

    assert theme is None
    assert warning is None


def test_read_config_theme_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\n')

    theme, warning = read_config_theme(path)

    assert theme == "nord"
    assert warning is None


def test_read_config_theme_ignores_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('other_setting = 42\ntheme = "gruvbox"\n')

    theme, warning = read_config_theme(path)

    assert theme == "gruvbox"
    assert warning is None


def test_read_config_theme_missing_key_is_quietly_none(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("other_setting = 42\n")

    theme, warning = read_config_theme(path)

    assert theme is None
    assert warning is None


def test_read_config_theme_unknown_name_warns(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "bogus-theme"\n')

    theme, warning = read_config_theme(path)

    assert theme is None
    assert warning is not None
    assert "bogus-theme" in warning


def test_read_config_theme_malformed_toml_warns(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("theme = [unterminated\n")

    theme, warning = read_config_theme(path)

    assert theme is None
    assert warning is not None


def test_read_config_theme_wrong_type_warns(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("theme = 42\n")

    theme, warning = read_config_theme(path)

    assert theme is None
    assert warning is not None
    assert "must be a string" in warning


def test_read_config_theme_unreadable_file_warns(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\n')
    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):  # running as root: the chmod above has no effect
            pytest.skip("cannot make a file unreadable to this user")
        theme, warning = read_config_theme(path)
    finally:
        path.chmod(0o644)

    assert theme is None
    assert warning is not None


# --- write_config_theme -----------------------------------------------------------


def test_write_config_theme_creates_the_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "appmem" / "config.toml"

    error = write_config_theme(path, "dracula")

    assert error is None
    assert path.read_text() == 'theme = "dracula"\n'


def test_write_config_theme_overwrites_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\nstray = 1\n')

    error = write_config_theme(path, "gruvbox")

    assert error is None
    assert path.read_text() == 'theme = "gruvbox"\n'


def test_write_config_theme_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    write_config_theme(tmp_path / "config.toml", "nord")

    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_write_config_theme_failure_returns_a_message_not_an_exception(tmp_path: Path) -> None:
    # A read-only parent directory: `mkdir` succeeds (it already exists) but
    # the temp file can't be created inside it.
    read_only_dir = tmp_path / "ro"
    read_only_dir.mkdir()
    read_only_dir.chmod(0o500)
    try:
        if os.access(read_only_dir, os.W_OK):  # running as root
            pytest.skip("cannot make a directory read-only to this user")
        error = write_config_theme(read_only_dir / "config.toml", "nord")
    finally:
        read_only_dir.chmod(0o700)

    assert error is not None
    assert not (read_only_dir / "config.toml").exists()


# --- resolve_theme: precedence ----------------------------------------------------


def test_resolve_theme_cli_wins_over_everything(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\n')

    result = resolve_theme(cli_theme="dracula", env_theme="gruvbox", config_path=path)

    assert result.effective == "dracula"
    assert result.warnings == ()
    # The file's own value is still the baseline for a later in-app pick,
    # even though it didn't win this run.
    assert result.config_theme == "nord"


def test_resolve_theme_env_wins_over_file_and_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\n')

    result = resolve_theme(cli_theme=None, env_theme="dracula", config_path=path)

    assert result.effective == "dracula"
    assert result.warnings == ()
    assert result.config_theme == "nord"


def test_resolve_theme_file_wins_over_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "gruvbox"\n')

    result = resolve_theme(cli_theme=None, env_theme=None, config_path=path)

    assert result.effective == "gruvbox"
    assert result.config_theme == "gruvbox"
    assert result.warnings == ()


def test_resolve_theme_falls_back_to_the_textual_default(tmp_path: Path) -> None:
    result = resolve_theme(cli_theme=None, env_theme=None, config_path=tmp_path / "config.toml")

    assert result.effective == "textual-dark"
    assert result.config_theme is None
    assert result.warnings == ()


def test_resolve_theme_textual_theme_wins_over_hardcoded_default(tmp_path: Path) -> None:
    result = resolve_theme(
        cli_theme=None,
        env_theme=None,
        config_path=tmp_path / "config.toml",
        textual_theme="gruvbox",
    )

    assert result.effective == "gruvbox"
    assert result.warnings == ()


def test_resolve_theme_file_wins_over_textual_theme(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\n')

    result = resolve_theme(
        cli_theme=None, env_theme=None, config_path=path, textual_theme="gruvbox"
    )

    assert result.effective == "nord"
    assert result.warnings == ()


# --- resolve_theme: broken lower sources ------------------------------------------


def test_resolve_theme_unknown_env_name_falls_through_to_file_with_a_warning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "nord"\n')

    result = resolve_theme(cli_theme=None, env_theme="not-a-theme", config_path=path)

    assert result.effective == "nord"
    assert len(result.warnings) == 1
    assert "not-a-theme" in result.warnings[0]


def test_resolve_theme_unknown_env_name_and_no_file_falls_through_to_default(
    tmp_path: Path,
) -> None:
    result = resolve_theme(
        cli_theme=None, env_theme="not-a-theme", config_path=tmp_path / "config.toml"
    )

    assert result.effective == "textual-dark"
    assert len(result.warnings) == 1


def test_resolve_theme_unknown_textual_theme_falls_through_to_default_with_a_warning(
    tmp_path: Path,
) -> None:
    # Never reaches `App.theme` unvalidated -- an unknown TEXTUAL_THEME must
    # not crash the app at startup (SPEC.md "Command line").
    result = resolve_theme(
        cli_theme=None,
        env_theme=None,
        config_path=tmp_path / "config.toml",
        textual_theme="not-a-theme",
    )

    assert result.effective == "textual-dark"
    assert len(result.warnings) == 1
    assert "not-a-theme" in result.warnings[0]


def test_resolve_theme_malformed_file_falls_through_to_default_with_a_warning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("theme = [unterminated\n")

    result = resolve_theme(cli_theme=None, env_theme=None, config_path=path)

    assert result.effective == "textual-dark"
    assert result.config_theme is None
    assert len(result.warnings) == 1


def test_resolve_theme_unknown_env_and_broken_file_reports_both_warnings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("theme = 42\n")

    result = resolve_theme(cli_theme=None, env_theme="not-a-theme", config_path=path)

    assert result.effective == "textual-dark"
    assert len(result.warnings) == 2
    assert "not-a-theme" in result.warnings[0]
    assert "must be a string" in result.warnings[1]


def test_resolve_theme_never_reads_config_warning_when_cli_overrides(tmp_path: Path) -> None:
    # The file is broken, but --theme wins outright: no warning surfaces for
    # a source that was never actually consulted this run.
    path = tmp_path / "config.toml"
    path.write_text("theme = [unterminated\n")

    result = resolve_theme(cli_theme="nord", env_theme=None, config_path=path)

    assert result.effective == "nord"
    assert result.warnings == ()


def test_every_theme_name_is_a_real_builtin_theme_or_a_terminal_theme() -> None:
    from textual.theme import BUILTIN_THEMES

    assert tuple(sorted(THEME_NAMES)) == THEME_NAMES
    for name in THEME_NAMES:
        assert name in BUILTIN_THEMES or name in TERMINAL_THEME_NAMES
    # `ansi-dark`/`ansi-light` are still real `BUILTIN_THEMES` entries --
    # just not ones `THEME_NAMES` shows, since `terminal-dark`/`terminal-light`
    # stand in for them (SPEC.md "Command line").
    assert "ansi-dark" not in THEME_NAMES
    assert "ansi-light" not in THEME_NAMES
    assert {"terminal-dark", "terminal-light"} == TERMINAL_THEME_NAMES


# --- terminal-dark/terminal-light: honest names for ansi-dark/ansi-light --------


def test_terminal_themes_clone_the_ansi_colours_under_the_new_name() -> None:
    from textual.theme import BUILTIN_THEMES

    by_name = {theme.name: theme for theme in TERMINAL_THEMES}
    assert set(by_name) == {"terminal-dark", "terminal-light"}
    pairs = (("terminal-dark", "ansi-dark"), ("terminal-light", "ansi-light"))
    for terminal_name, ansi_name in pairs:
        terminal_theme = by_name[terminal_name]
        ansi_theme = BUILTIN_THEMES[ansi_name]
        assert terminal_theme.ansi is True  # so the header's `:ansi` CSS still applies
        assert terminal_theme.primary == ansi_theme.primary
        assert terminal_theme.foreground == ansi_theme.foreground
        assert terminal_theme.background == ansi_theme.background


def test_canonical_theme_name_maps_the_old_ansi_aliases() -> None:
    assert canonical_theme_name("ansi-dark") == "terminal-dark"
    assert canonical_theme_name("ansi-light") == "terminal-light"


def test_canonical_theme_name_leaves_other_names_alone() -> None:
    assert canonical_theme_name("nord") == "nord"
    assert canonical_theme_name("terminal-dark") == "terminal-dark"
    assert canonical_theme_name("bogus-theme") == "bogus-theme"


def test_read_config_theme_accepts_the_ansi_dark_alias(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('theme = "ansi-dark"\n')

    theme, warning = read_config_theme(path)

    assert theme == "terminal-dark"
    assert warning is None


def test_write_then_read_config_theme_round_trips_terminal_dark(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    error = write_config_theme(path, "terminal-dark")

    assert error is None
    theme, warning = read_config_theme(path)
    assert theme == "terminal-dark"
    assert warning is None


def test_resolve_theme_cli_alias_resolves_to_the_terminal_name(tmp_path: Path) -> None:
    result = resolve_theme(
        cli_theme="ansi-light", env_theme=None, config_path=tmp_path / "config.toml"
    )

    assert result.effective == "terminal-light"
    assert result.warnings == ()


def test_resolve_theme_env_alias_resolves_to_the_terminal_name(tmp_path: Path) -> None:
    result = resolve_theme(
        cli_theme=None, env_theme="ansi-light", config_path=tmp_path / "config.toml"
    )

    assert result.effective == "terminal-light"
    assert result.warnings == ()


def test_resolve_theme_textual_theme_alias_resolves_to_the_terminal_name(
    tmp_path: Path,
) -> None:
    result = resolve_theme(
        cli_theme=None,
        env_theme=None,
        config_path=tmp_path / "config.toml",
        textual_theme="ansi-dark",
    )

    assert result.effective == "terminal-dark"
    assert result.warnings == ()
