"""Interpreter/launcher-aware process display names.

`collect._read_proc_name` already picks the safe first token of a process's
argv; for a short list of known interpreters and launchers this module goes
one step further, replacing a name that would otherwise always read as the
bare `node` or `python` with the script or package it actually runs, e.g.
`node:mcp-remote`, `npx:mcp-remote`, `python:ccs-websearch`. Every other
process keeps its own plain basename, untouched.

Fails closed: a derived label is only produced when argv matches an exact,
known-safe shape for that interpreter -- an option outside the small
per-interpreter allowlist, inline code, a malformed target, or a target that
doesn't survive the final charset/length check all fall back to the bare
interpreter name, exactly like a process this module doesn't special-case at
all. When unsure, drop the label rather than risk showing a value.

Only ever called with real, NUL-delimited argv (never a setproctitle-style
single field joined by spaces, which may hide a secret past its first
token) -- see the call site in `collect.py`.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_PY_RE = re.compile(r"^python3?(\.\d+)?$")
_NODE_LIKE = frozenset({"node", "bun", "deno"})
_LAUNCHERS = frozenset({"npm", "npx", "uv", "uvx"})

_GENERIC_BASENAMES = frozenset({"index.js", "main.js", "cli.js", "__main__.py"})
_SKIP_PARENT_DIRS = frozenset({"bin", "dist", "build", "lib", "out", "src"})

# A leading "verb" subcommand to skip before the real script/package
# argument, one per launcher; interpreters that take the target directly
# (npx, uvx) have none.
_VERB_SKIP: dict[str, frozenset[str]] = {
    "npm": frozenset({"exec", "run", "x"}),
    "npx": frozenset(),
    "uv": frozenset({"tool", "run", "install"}),
    "uvx": frozenset(),
    "node": frozenset(),
    "bun": frozenset({"run"}),
    "deno": frozenset({"run"}),
}

# Value-less flags safe to skip before the target, one set per node-like/
# launcher interpreter -- an allowlist: an option that isn't in here (a
# value-taking flag, inline code such as -e/-c/-p, an unknown flag, or a
# bundled short-option group like -Sc, which never matches a single-flag
# entry here) ends the scan with the bare interpreter name instead.
_SAFE_OPTION_FLAGS: dict[str, frozenset[str]] = {
    "node": frozenset({"--no-warnings", "--enable-source-maps"}),
    "bun": frozenset(),
    "deno": frozenset(),
    # `-y`/`--yes` only skip npm's install prompt; MCP servers are usually
    # started as `npx -y <pkg>`.
    "npm": frozenset({"-y", "--yes"}),
    "npx": frozenset({"-y", "--yes"}),
    "uv": frozenset(),
    "uvx": frozenset(),
}
_PYTHON_SAFE_OPTION_FLAGS = frozenset({"-u", "-B", "-O", "-E", "-s", "-S", "-I"})

_MAX_LABEL_LEN = 40
_LABEL_RE = re.compile(r"^[A-Za-z0-9._@/+-]+$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_PACKAGE_RE = re.compile(r"^(@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*$")
# A plain (non-generic) script basename should never contain these -- the
# tell-tale leftovers of a URL glued onto a path (query, fragment, userinfo)
# that survived because the raw target had no "://" of its own.
_BASENAME_BLOCKLIST = ("?", "#", "=", "@")


def _is_flag(token: str) -> bool:
    return token.startswith("-")


def _has_control_or_space(text: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def _package_for_path(path_str: str) -> str:
    """The package a generic script basename (`index.js`, `__main__.py`,
    ...) belongs to: the directory right after the last `node_modules/`
    (both segments of a scoped `@org/pkg`), or otherwise the nearest parent
    directory that isn't a build-artifact name like `bin`/`dist`/`src`."""
    parts = [p for p in path_str.replace("\\", "/").split("/") if p]
    if "node_modules" in parts:
        index = len(parts) - 1 - parts[::-1].index("node_modules")
        after = parts[index + 1 :]
        if len(after) >= 2 and after[0].startswith("@"):
            return f"{after[0]}/{after[1]}"
        if after:
            return after[0]
    for part in reversed(parts[:-1]):
        if part not in _SKIP_PARENT_DIRS:
            return part
    return parts[-1] if parts else path_str


def _script_target(raw: str) -> str | None:
    """A node/bun/deno/python script-path target: its basename, or the
    owning package for a generic basename -- `None` when the raw target
    doesn't look like a plain filesystem path (a URL, a path carrying query/
    fragment/userinfo leftovers, whitespace or a control character)."""
    if _has_control_or_space(raw) or "://" in raw:
        return None
    basename = PurePosixPath(raw.replace("\\", "/")).name
    if basename in _GENERIC_BASENAMES:
        return _package_for_path(raw)  # may legitimately contain "@"/"/" (a scope)
    if any(marker in basename for marker in _BASENAME_BLOCKLIST):
        return None
    return basename


def _strip_package_version(spec: str) -> str:
    """Drop a trailing `@version` from a package spec, keeping a leading
    scope's own `@` intact (`@scope/pkg@1.2.3` -> `@scope/pkg`)."""
    if spec.startswith("@"):
        slash = spec.find("/")
        if slash == -1:
            return spec
        at = spec.find("@", slash)
    else:
        at = spec.find("@")
    return spec[:at] if at != -1 else spec


def _package_target(raw: str) -> str | None:
    """An npm/npx/uv/uvx package spec target: `@scope/name` with any
    `@version` stripped, or `None` when it isn't a valid package name (also
    rejects a URL, a path with `..` segments, or anything else outside the
    package-name charset)."""
    spec = _strip_package_version(raw)
    return spec if _PACKAGE_RE.match(spec) else None


def _module_target(raw: str) -> str | None:
    return raw if _MODULE_RE.match(raw) else None


def _finalize(target: str | None) -> str | None:
    """The last gate before a target reaches the caller, applied uniformly
    regardless of which validator produced it: charset and length."""
    if target is None or len(target) > _MAX_LABEL_LEN or not _LABEL_RE.match(target):
        return None
    return target


def _python_target(args: list[str]) -> str | None:
    """The part to show after `python:`, or `None` for the bare interpreter
    name -- an option outside the small safe set (this covers `-e`/`-c` and
    any bundled short-option group such as `-Sc`), or no positional argument
    found."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-m":
            return _finalize(_module_target(args[index + 1])) if index + 1 < len(args) else None
        if arg in _PYTHON_SAFE_OPTION_FLAGS:
            index += 1
            continue
        if _is_flag(arg):
            return None
        return _finalize(_script_target(arg))
    return None


def _launcher_target(interpreter: str, args: list[str]) -> str | None:
    verbs = _VERB_SKIP[interpreter]
    safe_flags = _SAFE_OPTION_FLAGS[interpreter]
    resolve = _script_target if interpreter in _NODE_LIKE else _package_target
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in verbs:
            index += 1
            continue
        if _is_flag(arg):
            if arg in safe_flags:
                index += 1
                continue
            return None
        return _finalize(resolve(arg))
    return None


def command_display_name(basename: str, args: list[str]) -> str | None:
    """`"{interpreter}:{target}"` for a known interpreter/launcher whose argv
    matches an exact, known-safe shape, the bare interpreter name alone
    otherwise, or `None` when `basename` isn't one this module special-cases
    -- the caller keeps its own name then. Never returns anything but the
    interpreter name and, at most, one derived script/package/module label:
    no other argument, no full path and no flag value ever reaches the
    result."""
    if _PY_RE.match(basename):
        target = _python_target(args)
        return f"{basename}:{target}" if target else basename
    if basename in _NODE_LIKE or basename in _LAUNCHERS:
        target = _launcher_target(basename, args)
        return f"{basename}:{target}" if target else basename
    return None
