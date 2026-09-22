"""CLI entry point (SPEC.md "Command line")."""

from __future__ import annotations

from appmem.cli import main as _main


def main() -> None:
    """Run the CLI and exit with its process exit code."""
    raise SystemExit(_main())


if __name__ == "__main__":
    main()
