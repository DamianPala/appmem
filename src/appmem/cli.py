"""CLI: argument parsing, pre-start checks and process exit codes.

`snapshot`, `app` and `schema` follow the CLI Design Standard 0.1.0 and
describe themselves through `appmem schema`; SPEC.md owns the live view. Every parser is built
from the descriptors in `schema.py`, so a flag or default declared there is
the one the parser actually accepts: nothing here re-states a flag name,
help text or default on its own. All pre-start failures print a human line
to stderr, then one JSON error object as the last stderr line; `main()`
returns the process exit code rather than calling `sys.exit` itself, so
tests can call it directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, Protocol

from appmem import __version__, darwin_report, darwin_schema, report, schema
from appmem.backend import Backend, select_backend
from appmem.collect import CgroupUnavailableError
from appmem.darwin_backend import DarwinBackend, DarwinUnavailableError
from appmem.render import render_app_text, render_snapshot_text
from appmem.theme import (
    APPMEM_THEME_ENV,
    TEXTUAL_THEME_ENV,
    THEME_NAMES,
    canonical_theme_name,
    config_path,
    resolve_theme,
)
from appmem.ui.app import AppMemApp
from appmem.ui.screens.darwin import DarwinMainScreen

MIN_INTERVAL = 0.2

ERROR_KINDS: tuple[str, ...] = (
    "invalid_input",
    "terminal_required",
    "cgroup_unavailable",
    "not_found",
    "interrupted",
    "platform_unavailable",
)
"""Every `kind` an error object can carry. The single source for both the
runtime check below and `tests/test_docs.py`, so a kind renamed here without
updating README.md or SKILL.md fails the build instead of drifting quietly."""

_HELP_TEXT = """\
appmem: live terminal view of RAM and swap usage per application, not per process.

Usage:
  appmem [-i SECONDS] [--system] [--theme NAME]
  appmem snapshot [--system] [--limit N] [--json]
  appmem app NAME [--scope user|system] [--limit N] [--json]
  appmem schema [COMMAND]
  appmem --help | -h
  appmem --version | -V

Flags:
  -i, --interval SECONDS   Refresh interval, a number >= 0.2 (default: 1); the live view only
  --system                 Start with system services shown (same as pressing x)
  --theme NAME             One of appmem's theme names (appmem schema lists them; terminal-dark
                           and terminal-light use your terminal's own colours); overrides
                           APPMEM_THEME and the config file for this run, the live view only,
                           never written back
  --json                   Write JSON instead of text; the default when stdout isn't a terminal
  -h, --help               Show this help and exit
  -V, --version            Show the version and exit

Commands:
  snapshot   One sample of the machine and every app
  app NAME   One app's units, processes, commands and remainder
  schema     Describe the commands, flags, output shapes and exit codes as JSON

Run `appmem schema` for the full machine-readable interface, or
`appmem snapshot --help` (any command works the same way) for that command's own flags.

Keys:
  click header         sort by that column, click again to reverse
  r / s / t / d / z    sort by RAM / SWAP / TOTAL / ΔSWAP / ZSWAP (repeat to reverse;
                       z only where the ZSWAP column is shown); other columns sort by click
  up/down PgUp PgDn    move
  Home End             jump to the first/last row
  Enter                open the process view for the selected app;
                       grouped: the processes of the selected command
  g                    process view: toggle grouping by command
  Esc                  back to the main view
  c                    toggle the CACHE column
  w                    toggle the ZSWAP column (shown by default where zswap is on)
  x                    toggle system services
  b                    reset the Δ baseline to now
  T / Ctrl+P           change the theme (opens on the current theme, remembered in the
                       config file)
  ?                    help screen
  q / Ctrl+C           quit

Memory pressure:
  none             few memory stalls in the last 10 s
  some (X.X %)     some time spent waiting; shown with the percentage
  high             a lot of time spent waiting: memory stalls are happening,
                   but this alone doesn't say which app is causing them
  Big swap with pressure none just means idle pages were paged out.

Example:
  appmem -i 2 --system
"""

_SNAPSHOT_HELP_TEXT = f"""\
appmem snapshot: {schema.SNAPSHOT_DESCRIPTION}

Usage:
  appmem snapshot [--system] [--limit N] [--json]
  appmem snapshot --help | -h

Flags:
  --system      {schema.SNAPSHOT_SYSTEM.description} (default: false)
  --limit N     {schema.SNAPSHOT_LIMIT.description} (default: {schema.SNAPSHOT_LIMIT.default})
  --json        {schema.JSON_FLAG.description}
  -h, --help    Show this help and exit

Text on a terminal, JSON otherwise. `appmem schema snapshot` describes the JSON shape.
"""

_APP_HELP_TEXT = f"""\
appmem app: {schema.APP_DESCRIPTION}

Usage:
  appmem app NAME [--scope user|system] [--limit N] [--json]
  appmem app --help | -h

Args:
  NAME           {schema.APP_NAME.description}

Flags:
  --scope VALUE  {schema.APP_SCOPE.description} (default: {schema.APP_SCOPE.default})
  --limit N      {schema.APP_LIMIT.description} (default: {schema.APP_LIMIT.default})
  --json         {schema.JSON_FLAG.description}
  -h, --help     Show this help and exit

Note: a process's ram_bytes is RSS -- shared pages count once in every process
that maps it, so don't sum processes; use the app's own ram_bytes instead.

Text on a terminal, JSON otherwise. `appmem schema app` describes the JSON shape.
"""

_SCHEMA_HELP_TEXT = """\
appmem schema: describe the commands, flags, output shapes and exit codes as JSON.

Usage:
  appmem schema [COMMAND]
  appmem schema --help | -h

With no argument, prints the introspection index (every command, the shared
flags, format defaults and exit codes). With a command name ("snapshot" or
"app"), prints that command's own flags, defaults and output schema. Always
writes JSON, with or without --json.
"""

_DARWIN_HELP_TEXT = """\
appmem: experimental Apple Silicon application footprint view.

Usage:
  appmem [-i SECONDS] [--theme NAME]
  appmem snapshot [--limit N] [--json]
  appmem app NAME [--scope user] [--limit N] [--json]
  appmem schema [COMMAND]

Options:
  -i, --interval SECONDS  Live refresh interval >= 0.2 (default: 1)
  --theme NAME            Live view theme
  --json                  Write JSON for named commands; live view requires a terminal
  -h, --help              Show this help and exit
  -V, --version           Show the version and exit

Commands:
  snapshot  Host memory and application footprints
  app NAME  One application's process and command footprints
  schema    Describe commands and output shapes as JSON

Run `appmem schema` for machine-readable fields, or `appmem snapshot --help`
and `appmem app --help` for each command's flags.

Footprint is a per-process native physical footprint, not resident RAM or
reclaimable memory. Denied process reads make app totals partial or unknown.
macOS 15+ Apple Silicon is required. --system is unsupported.

Keys: Enter details, g group by command, b reset growth baseline,
      up/down move, T theme, ? help, q/Ctrl+C quit.
"""
_DARWIN_SNAPSHOT_HELP_TEXT = """\
appmem snapshot: native host memory and user application footprints.
Usage: appmem snapshot [--limit N] [--json]
  --limit N   Maximum app items (default: 50)
  --json      Write JSON; also the default outside a terminal
  -h, --help  Show this help and exit
--system is unsupported on macOS. Use `appmem schema snapshot` for the JSON shape.
"""
_DARWIN_APP_HELP_TEXT = """\
appmem app: one application's captured process and command footprints.
Usage: appmem app NAME [--scope user] [--limit N] [--json]
  --scope user  User scope (default: user); system scope is unsupported
  --limit N     Maximum process and command items (default: 100)
  --json        Write JSON; also the default outside a terminal
  -h, --help    Show this help and exit
NAME is the stable app id or displayed name. Use `appmem schema app` for the JSON shape.
"""


class _SubparserFactory(Protocol):
    """What `_add_*_parser` needs from `ArgumentParser.add_subparsers()`'s
    result: just enough to add one named subparser, without naming argparse's
    own (private) type for it."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


class _ArgumentParser(argparse.ArgumentParser):
    """Routes argparse's own error path through the standard's JSON error format."""

    def error(self, message: str) -> NoReturn:
        usage = self.format_usage()
        sys.stderr.write(usage)  # fails "with the accepted form"
        accepted = " ".join(usage.removeprefix("usage:").split())
        _fail("invalid_input", message, 2, action="agent", hint=f"Accepted form: {accepted}")


def _make_help_action(text: str) -> type[argparse.Action]:
    """Build a `--help` action that prints a fixed cheat sheet instead of
    argparse's generated help; one per command, since each has its own text."""

    class _Help(argparse.Action):
        def __init__(
            self,
            option_strings: Sequence[str],
            dest: str = argparse.SUPPRESS,
            default: str = argparse.SUPPRESS,
            help: str | None = None,
        ) -> None:
            super().__init__(
                option_strings=option_strings, dest=dest, default=default, nargs=0, help=help
            )

        def __call__(
            self,
            parser: argparse.ArgumentParser,
            namespace: argparse.Namespace,
            values: str | Sequence[Any] | None,
            option_string: str | None = None,
        ) -> None:
            print(text, end="")
            parser.exit()

    return _Help


def _interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid interval {value!r}: must be a number >= {MIN_INTERVAL}"
        ) from exc
    if not math.isfinite(parsed) or parsed < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f"invalid interval {value!r}: must be >= {MIN_INTERVAL}")
    return parsed


def _theme_name(value: str) -> str:
    canonical = canonical_theme_name(value)
    if canonical not in THEME_NAMES:
        valid = ", ".join(THEME_NAMES)
        raise argparse.ArgumentTypeError(f"invalid theme {value!r}: must be one of {valid}")
    return canonical


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid limit {value!r}: must be an integer >= 1"
        ) from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"invalid limit {value!r}: must be an integer >= 1")
    return parsed


def _option_strings(flag: schema.Flag) -> list[str]:
    short = [f"-{alias}" for alias in flag.aliases if len(alias) == 1]
    long_aliases = [f"--{alias}" for alias in flag.aliases if len(alias) != 1]
    return [*short, f"--{flag.name}", *long_aliases]


def _add_json_flag(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    parser.add_argument(
        *_option_strings(schema.JSON_FLAG),
        action="store_true",
        default=argparse.SUPPRESS if suppress else schema.JSON_FLAG.default,
        help=schema.JSON_FLAG.description,
    )


def _build_parser(*, darwin: bool = False) -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="appmem", add_help=False)
    parser.add_argument(
        "-h",
        "--help",
        action=_make_help_action(_DARWIN_HELP_TEXT if darwin else _HELP_TEXT),
        help="show this help and exit",
    )
    parser.add_argument(
        *_option_strings(schema.ROOT_INTERVAL),
        type=_interval,
        default=None,  # None means "not given"; a named command rejects an explicit value
        metavar="SECONDS",
        help=schema.ROOT_INTERVAL.description,
    )
    parser.add_argument(
        *_option_strings(schema.ROOT_SYSTEM),
        action="store_true",
        default=schema.ROOT_SYSTEM.default,
        help=schema.ROOT_SYSTEM.description,
    )
    parser.add_argument(
        *_option_strings(schema.ROOT_THEME),
        # Not `choices=`: that would list `ansi-dark`/`ansi-light` (still
        # accepted, see `_theme_name`) in argparse's own usage/error text
        # right alongside the honest names, or reject them outright if left
        # out of `choices` -- `_theme_name` accepts both and only ever
        # reports the canonical `THEME_NAMES` on a real miss.
        type=_theme_name,
        default=None,  # None means "not given"; a named command rejects an explicit value
        metavar="NAME",
        help=schema.ROOT_THEME.description,
    )
    _add_json_flag(parser, suppress=False)
    parser.add_argument("-V", "--version", action="version", version=__version__)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    _add_snapshot_parser(subparsers, darwin=darwin)
    _add_app_parser(subparsers, darwin=darwin)
    _add_schema_parser(subparsers)
    return parser


def _add_snapshot_parser(subparsers: _SubparserFactory, *, darwin: bool = False) -> None:
    parser = subparsers.add_parser("snapshot", add_help=False)
    parser.add_argument(
        "-h",
        "--help",
        action=_make_help_action(_DARWIN_SNAPSHOT_HELP_TEXT if darwin else _SNAPSHOT_HELP_TEXT),
        help="show this help and exit",
    )
    parser.add_argument(
        *_option_strings(schema.SNAPSHOT_SYSTEM),
        action="store_true",
        # SUPPRESS: a bare "appmem --system snapshot" must keep the root's value,
        # not have this parser's own default silently overwrite it.
        default=argparse.SUPPRESS,
        help=schema.SNAPSHOT_SYSTEM.description,
    )
    parser.add_argument(
        *_option_strings(schema.SNAPSHOT_LIMIT),
        type=_positive_int,
        default=schema.SNAPSHOT_LIMIT.default,
        metavar="N",
        help=schema.SNAPSHOT_LIMIT.description,
    )
    _add_json_flag(parser, suppress=True)


def _add_app_parser(subparsers: _SubparserFactory, *, darwin: bool = False) -> None:
    parser = subparsers.add_parser("app", add_help=False)
    parser.add_argument(
        "-h",
        "--help",
        action=_make_help_action(_DARWIN_APP_HELP_TEXT if darwin else _APP_HELP_TEXT),
        help="show this help and exit",
    )
    parser.add_argument("name", metavar="NAME", help=schema.APP_NAME.description)
    parser.add_argument(
        *_option_strings(schema.APP_SCOPE),
        choices=schema.APP_SCOPE.enum,
        default=schema.APP_SCOPE.default,
        help=schema.APP_SCOPE.description,
    )
    parser.add_argument(
        *_option_strings(schema.APP_LIMIT),
        type=_positive_int,
        default=schema.APP_LIMIT.default,
        metavar="N",
        help=schema.APP_LIMIT.description,
    )
    _add_json_flag(parser, suppress=True)


def _add_schema_parser(subparsers: _SubparserFactory) -> None:
    parser = subparsers.add_parser("schema", add_help=False)
    parser.add_argument(
        "-h", "--help", action=_make_help_action(_SCHEMA_HELP_TEXT), help="show this help and exit"
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="COMMAND",
        help="command to describe; omit for the index",
    )
    _add_json_flag(parser, suppress=True)


def _print_error_json(
    kind: str,
    message: str,
    *,
    action: str | None = None,
    hint: str | None = None,
    next_argv: list[str] | None = None,
) -> None:
    assert kind in ERROR_KINDS, f"undeclared error kind: {kind!r}"
    print(f"appmem: {message}", file=sys.stderr)
    error: dict[str, object] = {"kind": kind, "message": message}
    if action is not None:
        error["action"] = action
    if hint is not None:
        error["hint"] = hint
    if next_argv is not None:
        error["next"] = next_argv
    print(json.dumps({"error": error}), file=sys.stderr)


def _fail(
    kind: str,
    message: str,
    code: int,
    *,
    action: str | None = None,
    hint: str | None = None,
    next_argv: list[str] | None = None,
) -> NoReturn:
    _print_error_json(kind, message, action=action, hint=hint, next_argv=next_argv)
    raise SystemExit(code)


def _write_result(text: str) -> None:
    """Print a command's result. If a downstream reader has already closed
    the pipe, stop writing and exit 0 instead of letting the interpreter's
    own shutdown print a 'Broken pipe' traceback."""
    try:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        raise SystemExit(0) from None


def _run_app(app: AppMemApp) -> int:
    """Run the Textual app, translating an external SIGTERM/SIGINT into a graceful exit.

    Ctrl+C typed in the running TUI never reaches these handlers: Textual's raw
    terminal mode stops the terminal from turning it into a signal in the first
    place, so it arrives as an ordinary key event instead (SPEC.md "Errors").

    Registered with `loop.add_signal_handler` rather than `signal.signal`: a
    `signal.signal` handler only runs the next time the event loop wakes up on
    its own, so `app.exit()` could sit unapplied until the next periodic tick;
    `add_signal_handler` wakes the loop immediately via its self-pipe instead.
    Requires an explicit loop, created and closed here (`loop.close()` also
    removes the handlers registered on it), instead of the implicit one
    `app.run()` would otherwise create.
    """

    def _handle_sigterm() -> None:
        app.exit(return_code=143)

    def _handle_sigint() -> None:
        app.exit(return_code=130)

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    try:
        app.run(loop=loop)
    finally:
        loop.close()
    # A screen recorded a vanished cgroup tree (SPEC.md "Errors"): the JSON
    # line prints here, after Textual has restored the terminal, reusing the
    # same error-printing path as the pre-start checks.
    if app.cgroup_error_message is not None:
        _print_error_json("cgroup_unavailable", app.cgroup_error_message, action="user")
        return 1
    return app.return_code if app.return_code is not None else 0


def _require_live_terminal(
    args: argparse.Namespace,
    stdin_isatty: Callable[[], bool],
    stdout_isatty: Callable[[], bool],
) -> None:
    json_flag = bool(args.json)
    no_input = bool(os.environ.get("NO_INPUT"))
    if not (stdin_isatty() and stdout_isatty()) or json_flag or no_input:
        next_argv = ["appmem", "snapshot"]
        if json_flag:
            next_argv.append("--json")
        # A context failure, not a usage error: exit 1 alongside cgroup_unavailable
        # and not_found, so exit 2 stays reserved for a real invalid_input call.
        _fail(
            "terminal_required",
            "the live view needs a terminal; for a one-shot report run: appmem snapshot",
            1,
            action="agent",
            hint="Run appmem snapshot for a one-shot report",
            next_argv=next_argv,
        )


def _run_root(
    args: argparse.Namespace,
    *,
    backend: Backend,
    stdin_isatty: Callable[[], bool],
    stdout_isatty: Callable[[], bool],
) -> int:
    """Bare `appmem`: the live view, but only in a terminal context."""
    _require_live_terminal(args, stdin_isatty, stdout_isatty)

    try:
        backend.check()
    except CgroupUnavailableError as exc:
        _fail("cgroup_unavailable", str(exc), 1, action="user")

    interval = args.interval if args.interval is not None else 1.0
    theme = resolve_theme(
        cli_theme=args.theme,
        env_theme=os.environ.get(APPMEM_THEME_ENV) or None,  # empty means unset, as NO_INPUT
        config_path=config_path(),
        textual_theme=os.environ.get(TEXTUAL_THEME_ENV) or None,
    )
    app = AppMemApp(
        backend=backend,
        interval=interval,
        include_system=args.system,
        theme=theme.effective,
        config_theme=theme.config_theme,
        theme_warnings=theme.warnings,
    )
    return _run_app(app)


def _run_snapshot(
    args: argparse.Namespace, *, backend: Backend, stdout_isatty: Callable[[], bool]
) -> int:
    json_flag = bool(args.json)
    include_system = bool(args.system)
    now = datetime.now().astimezone()
    try:
        document, total_apps = report.snapshot_document(
            backend, include_system=include_system, limit=args.limit, now=now
        )
    except KeyboardInterrupt:
        _fail("interrupted", "interrupted while reading the snapshot", 130, action="user")
    except CgroupUnavailableError as exc:
        _fail("cgroup_unavailable", str(exc), 1, action="user")

    next_argv = document.get("next")
    if json_flag and next_argv:
        next_argv.append("--json")

    if json_flag or not stdout_isatty():
        _write_result(json.dumps(document))
    else:
        _write_result(render_snapshot_text(document, total_apps=total_apps))
    return 0


def _run_app_command(
    args: argparse.Namespace, *, backend: Backend, stdout_isatty: Callable[[], bool]
) -> int:
    json_flag = bool(args.json)
    now = datetime.now().astimezone()
    try:
        document, total_processes, total_commands = report.app_document(
            backend, args.name, args.scope, limit=args.limit, now=now
        )
    except KeyboardInterrupt:
        _fail("interrupted", "interrupted while reading the app", 130, action="user")
    except CgroupUnavailableError as exc:
        _fail("cgroup_unavailable", str(exc), 1, action="user")
    except report.AppNotFoundError:
        next_argv = ["appmem", "snapshot"]
        if args.scope == "system":
            next_argv.append("--system")
        if json_flag:
            next_argv.append("--json")
        _fail(
            "not_found",
            f"no app named {args.name!r} in scope {args.scope!r}",
            1,
            action="agent",
            hint="Names are as listed by appmem snapshot; system services need --scope system",
            next_argv=next_argv,
        )

    if json_flag or not stdout_isatty():
        _write_result(json.dumps(document))
    else:
        _write_result(
            render_app_text(
                document, total_processes=total_processes, total_commands=total_commands
            )
        )
    return 0


def _run_schema(args: argparse.Namespace, *, darwin: bool = False) -> int:
    path: list[str] = [] if args.path is None else [args.path]
    if not path:
        document = darwin_schema.index() if darwin else schema.index()
    else:
        found = darwin_schema.detail(path) if darwin else schema.detail(path)
        if found is None:
            _fail(
                "invalid_input",
                f"unknown schema path {' '.join(path)!r}",
                2,
                action="agent",
                hint="Run appmem schema for the command index",
                next_argv=["appmem", "schema"],
            )
        document = found
    _write_result(json.dumps(document))
    return 0


def _reject_live_view_flags(args: argparse.Namespace) -> None:
    """Root flags belong to the live view; a named command that has no such
    flag of its own must refuse them rather than silently drop them."""
    if args.command is not None and args.interval is not None:
        _fail(
            "invalid_input",
            "--interval applies to the live view; a named command takes one sample and ignores it",
            2,
            action="agent",
            hint="Drop -i/--interval, or run appmem with no command for the live view",
        )
    if args.command in ("app", "schema") and args.system:
        _fail(
            "invalid_input",
            f"--system applies to the live view and snapshot, not to {args.command}",
            2,
            action="agent",
            hint="Drop --system; for a system service run appmem app NAME --scope system",
        )
    if args.command is not None and args.theme is not None:
        _fail(
            "invalid_input",
            "--theme applies to the live view; a named command has nothing to colour",
            2,
            action="agent",
            hint="Drop --theme, or run appmem with no command for the live view",
        )


def _run_darwin(
    args: argparse.Namespace,
    uid: int,
    stdin_isatty: Callable[[], bool],
    stdout_isatty: Callable[[], bool],
) -> int:
    if args.system or (args.command == "app" and args.scope == "system"):
        _fail(
            "invalid_input",
            "system scope is unavailable on macOS",
            2,
            action="agent",
            hint="Use the user-scoped snapshot or app command without --system",
        )
    try:
        backend = DarwinBackend(uid)
        if args.command == "snapshot":
            document, _ = darwin_report.snapshot_document(
                backend, limit=args.limit, now=datetime.now().astimezone()
            )
            output = (
                json.dumps(document)
                if args.json or not stdout_isatty()
                else darwin_report.render_snapshot_text(document)
            )
            _write_result(output)
            return 0
        if args.command == "app":
            found = darwin_report.app_document(
                backend, args.name, limit=args.limit, now=datetime.now().astimezone()
            )
            if found is None:
                _fail(
                    "not_found",
                    f"no app named {args.name!r}",
                    1,
                    action="agent",
                    hint="Use an id or name from appmem snapshot",
                )
            document, _, _ = found
            output = (
                json.dumps(document)
                if args.json or not stdout_isatty()
                else darwin_report.render_app_text(document)
            )
            _write_result(output)
            return 0
        _require_live_terminal(args, stdin_isatty, stdout_isatty)
        backend.check()
        theme = resolve_theme(
            cli_theme=args.theme,
            env_theme=os.environ.get(APPMEM_THEME_ENV) or None,
            config_path=config_path(),
            textual_theme=os.environ.get(TEXTUAL_THEME_ENV) or None,
        )
        app = AppMemApp(
            interval=args.interval if args.interval is not None else 1.0,
            main_screen_factory=lambda: DarwinMainScreen(
                backend, args.interval if args.interval is not None else 1.0
            ),
            theme=theme.effective,
            config_theme=theme.config_theme,
            theme_warnings=theme.warnings,
        )
        return _run_app(app)
    except DarwinUnavailableError as exc:
        _fail(
            "platform_unavailable",
            str(exc),
            1,
            action="user",
            hint="Run on macOS 15 or newer on Apple Silicon",
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    uid: int | None = None,
    stdin_isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    stdout_isatty: Callable[[], bool] = lambda: sys.stdout.isatty(),
) -> int:
    """Parse args, dispatch to a command, and return its process exit code.

    Returns the exit code instead of calling `sys.exit` so tests (and
    `--help`/`--version`/usage errors, which raise `SystemExit` from
    argparse) stay easy to drive directly.
    """
    darwin = root is None and sys.platform == "darwin"
    args = _build_parser(darwin=darwin).parse_args(argv)
    if root is None and sys.platform not in ("linux", "darwin"):
        _fail(
            "platform_unavailable",
            f"unsupported operating system {sys.platform!r}",
            1,
            action="user",
            hint="Run on Linux or macOS 15+ Apple Silicon",
        )
    resolved_root = Path("/") if root is None else root
    resolved_uid = os.getuid() if uid is None else uid

    _reject_live_view_flags(args)

    if args.command == "schema":
        return _run_schema(args, darwin=darwin)

    if darwin:
        return _run_darwin(args, resolved_uid, stdin_isatty, stdout_isatty)

    backend = select_backend(resolved_root, resolved_uid)

    if args.command == "snapshot":
        return _run_snapshot(args, backend=backend, stdout_isatty=stdout_isatty)
    if args.command == "app":
        return _run_app_command(args, backend=backend, stdout_isatty=stdout_isatty)

    return _run_root(
        args,
        backend=backend,
        stdin_isatty=stdin_isatty,
        stdout_isatty=stdout_isatty,
    )
