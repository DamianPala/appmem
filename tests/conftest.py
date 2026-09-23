"""Shared fixtures for the appmem test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_xdg_config_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every test at a private `XDG_CONFIG_HOME`, so nothing in this
    suite can ever read or write the real `~/.config` (AGENTS.md: "no writes
    outside the repo"; `appmem/theme.py`'s config file is written only by a
    theme choice made in the running app, never by a test)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config-home")))
    # The developer's shell must not leak into a test result: cleared even
    # though `resolve_theme` also takes `TEXTUAL_THEME` as an explicit
    # parameter, since `main()`'s own CLI path still reads it from `os.environ`.
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    monkeypatch.delenv("TEXTUAL_THEME", raising=False)
