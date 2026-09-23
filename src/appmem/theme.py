"""Theme persistence: `$XDG_CONFIG_HOME/appmem/config.toml` (SPEC.md "Command
line"), the only setting appmem keeps in a config file.

Only `theme = "<name>"` is ever written, and only by a theme choice made in
the running app (`AppMemApp.watch_theme`); `--theme`/`APPMEM_THEME` read the
file but never write it. Every name is one of Textual's own built-in themes
(`textual.theme.BUILTIN_THEMES`) -- no custom themes.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from textual.theme import BUILTIN_THEMES

THEME_NAMES: tuple[str, ...] = tuple(sorted(BUILTIN_THEMES))
"""Every accepted theme name, for `--theme`'s choices and the config/env
validation below. Sorted so `--theme bogus`'s error message and argparse's
own usage line list them in a stable order."""

APPMEM_THEME_ENV = "APPMEM_THEME"
TEXTUAL_THEME_ENV = "TEXTUAL_THEME"

TEXTUAL_BUILTIN_DEFAULT = "textual-dark"
"""Textual's own hard-coded fallback (`textual.constants.DEFAULT_THEME`'s
default when `TEXTUAL_THEME` is unset). Named here instead of importing that
constant: `textual.constants` reads `TEXTUAL_THEME` once, at import time, so
it can't tell a valid value from an invalid one at request time and can't be
pointed at a different value mid-test -- `resolve_theme` below reads and
validates `TEXTUAL_THEME` itself instead, the same way it already does for
`APPMEM_THEME`."""

_CONFIG_FILE_NAME = "config.toml"


def config_path() -> Path:
    """`$XDG_CONFIG_HOME/appmem/config.toml`, default `~/.config/appmem/config.toml`.

    Reads the environment fresh on every call rather than caching it at
    import time, so a test can point it at a tmp dir per test without the
    real `~/.config` ever coming into play (AGENTS.md).
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "appmem" / _CONFIG_FILE_NAME


def read_config_theme(path: Path) -> tuple[str | None, str | None]:
    """The file's `theme` value, or `None`; plus a problem message, or `None`.

    Never both at once: a missing file, or a file with no `theme` key, is
    quietly `(None, None)` -- nothing configured, not an error. Any other
    problem (unreadable, malformed TOML, wrong type, unknown name) is
    `(None, message)`. Unknown keys in the file are ignored -- only `theme`
    is ever inspected.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"can't read {path}: {exc.strerror or exc}"
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        return None, f"{path} is not valid TOML: {exc}"
    if "theme" not in data:
        return None, None
    value = data["theme"]
    if not isinstance(value, str):
        return None, f"{path}: 'theme' must be a string, not {type(value).__name__}"
    if value not in THEME_NAMES:
        return None, f"{path}: unknown theme {value!r}"
    return value, None


def write_config_theme(path: Path, theme_name: str) -> str | None:
    """Write `theme = "<name>"`, atomically (temp file in the same directory,
    then `os.replace`), creating the directory if it's missing.

    Returns an error message on failure (read-only directory, permissions),
    `None` on success -- the caller notifies and keeps the app running
    rather than raising (SPEC.md "Command line"). `theme_name` is always one
    of `THEME_NAMES` (alphanumerics and hyphens only), so it never needs TOML
    string escaping.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f'theme = "{theme_name}"\n')
            os.replace(tmp_name, path)
        except OSError:
            os.unlink(tmp_name)
            raise
    except OSError as exc:
        return f"can't save the theme to {path}: {exc.strerror or exc}"
    return None


@dataclass(frozen=True)
class ThemeResolution:
    """Startup theme precedence (SPEC.md "Command line"): `effective` is what
    the TUI opens with. `config_theme` is the config file's own valid value
    (or `None`) regardless of what overrode it this run -- the baseline a
    later in-app pick is compared against, so picking back to the file's own
    value doesn't cause a needless rewrite. `warnings` are shown as in-app
    notifications once the TUI is running, one per broken source that was
    actually consulted.
    """

    effective: str
    config_theme: str | None
    warnings: tuple[str, ...]


def resolve_theme(
    *,
    cli_theme: str | None,
    env_theme: str | None,
    config_path: Path,
    textual_theme: str | None = None,
) -> ThemeResolution:
    """`--theme` > `APPMEM_THEME` > config file > `TEXTUAL_THEME` (if it
    names a real theme) > Textual's own hard-coded default.

    `cli_theme` is assumed already validated: the CLI parser rejects an
    unknown `--theme` as a usage error before this ever runs. A lower
    source's own problem is only turned into a warning when it was actually
    consulted, i.e. every higher source was absent or itself invalid --
    `TEXTUAL_THEME` included, so an unknown value there warns and falls back
    to the hard-coded default instead of ever reaching `App.theme` unvalidated
    (which would raise `InvalidThemeError` at startup).
    """
    config_theme, config_warning = read_config_theme(config_path)
    if cli_theme is not None:
        return ThemeResolution(cli_theme, config_theme, ())

    warnings: list[str] = []
    if env_theme is not None:
        if env_theme in THEME_NAMES:
            return ThemeResolution(env_theme, config_theme, ())
        warnings.append(f"{APPMEM_THEME_ENV}={env_theme!r} is not a known theme; ignoring it")

    if config_theme is not None:
        return ThemeResolution(config_theme, config_theme, tuple(warnings))
    if config_warning is not None:
        warnings.append(config_warning)

    if textual_theme is not None:
        if textual_theme in THEME_NAMES:
            return ThemeResolution(textual_theme, config_theme, tuple(warnings))
        warnings.append(f"{TEXTUAL_THEME_ENV}={textual_theme!r} is not a known theme; ignoring it")

    return ThemeResolution(TEXTUAL_BUILTIN_DEFAULT, config_theme, tuple(warnings))
