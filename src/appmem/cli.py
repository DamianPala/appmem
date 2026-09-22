"""CLI: argument parsing, pre-start checks and process exit codes.

SPEC.md "Command line" and "Errors" are the contract. All pre-start failures
print a human line to stderr, then one JSON error object as the last stderr
line; `main()` returns the process exit code rather than calling `sys.exit`
itself, so tests can call it directly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from appmem import __version__
from appmem.collect import CgroupUnavailableError, find_units
from appmem.ui.app import AppMemApp

MIN_INTERVAL = 0.2

_HELP_TEXT = """\
appmem: live terminal view of RAM and swap usage per application, not per process.

Usage:
  appmem [-i SECONDS] [--system]
  appmem --help | -h
  appmem --version | -V

Flags:
  -i, --interval SECONDS   Refresh interval, a number >= 0.2 (default: 1)
  --system                 Start with system services shown (same as pressing x)
  -h, --help               Show this help and exit
  -V, --version            Show the version and exit

Keys:
  click header         sort by that column, click again to reverse
  s / r / t / d        sort by SWAP / RAM / TOTAL / ΔSWAP (repeat to reverse);
                       other columns sort by click
  up/down PgUp PgDn    move
  Enter                open the process view for the selected app
  g                    process view: toggle grouping by command
  Esc                  back to the main view
  c                    toggle the CACHE column
  x                    toggle system services
  z                    reset the Δ baseline to now
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


class _ArgumentParser(argparse.ArgumentParser):
    """Routes argparse's own error path through the SPEC.md JSON error format."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)  # SPEC.md: usage errors show the accepted form
        _fail("usage", message, 2)


class _HelpAction(argparse.Action):
    """Prints the SPEC.md cheat sheet instead of argparse's generated help."""

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
        print(_HELP_TEXT, end="")
        parser.exit()


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


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="appmem", add_help=False)
    parser.add_argument("-h", "--help", action=_HelpAction, help="show this help and exit")
    parser.add_argument(
        "-i",
        "--interval",
        type=_interval,
        default=1.0,
        metavar="SECONDS",
        help="refresh interval in seconds, >= 0.2 (default: 1)",
    )
    parser.add_argument("--system", action="store_true", help="start with system services shown")
    parser.add_argument("-V", "--version", action="version", version=f"appmem {__version__}")
    return parser


def _print_error_json(kind: str, message: str, *, action: str | None = None) -> None:
    print(f"appmem: {message}", file=sys.stderr)
    error: dict[str, object] = {"kind": kind, "message": message}
    if action is not None:
        error["action"] = action
    print(json.dumps({"error": error}), file=sys.stderr)


def _fail(kind: str, message: str, code: int, *, action: str | None = None) -> NoReturn:
    _print_error_json(kind, message, action=action)
    raise SystemExit(code)


def _run_app(app: AppMemApp) -> int:
    """Run the Textual app, translating an external SIGTERM/SIGINT into a graceful exit.

    Ctrl+C typed in the running TUI never reaches these handlers: Textual's raw
    terminal mode stops the terminal from turning it into a signal in the first
    place, so it arrives as an ordinary key event instead (SPEC.md "Errors").
    """

    def _handle_sigterm(signum: int, frame: object) -> None:
        app.exit(return_code=143)

    def _handle_sigint(signum: int, frame: object) -> None:
        app.exit(return_code=130)

    previous_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)
    previous_sigint = signal.signal(signal.SIGINT, _handle_sigint)
    try:
        app.run()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    # A screen recorded a vanished cgroup tree (SPEC.md "Errors"): the JSON
    # line prints here, after Textual has restored the terminal, reusing the
    # same error-printing path as the pre-start checks.
    if app.cgroup_error_message is not None:
        _print_error_json("cgroup_unavailable", app.cgroup_error_message)
        return 1
    return app.return_code if app.return_code is not None else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    uid: int | None = None,
    stdin_isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    stdout_isatty: Callable[[], bool] = lambda: sys.stdout.isatty(),
) -> int:
    """Parse args, run the pre-start checks, then the Textual app.

    Returns the process exit code instead of calling `sys.exit` so tests (and
    `--help`/`--version`/usage errors, which raise `SystemExit` from argparse)
    stay easy to drive directly.
    """
    args = _build_parser().parse_args(argv)
    resolved_root = Path("/") if root is None else root

    if not (stdin_isatty() and stdout_isatty()):
        _fail(
            "not_a_tty",
            "appmem needs an interactive terminal on stdin and stdout",
            2,
            action="user",
        )

    resolved_uid = os.getuid() if uid is None else uid
    try:
        find_units(resolved_root, resolved_uid, include_system=args.system)
    except CgroupUnavailableError as exc:
        _fail("cgroup_unavailable", str(exc), 1)

    app = AppMemApp(
        root=resolved_root, uid=resolved_uid, interval=args.interval, include_system=args.system
    )
    return _run_app(app)
