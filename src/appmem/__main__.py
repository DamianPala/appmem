"""CLI entry point.

The real CLI (flags, TTY checks, the Textual app) arrives in a later slice.
For now this only proves the `appmem` script entry point works.
"""

from appmem import __version__


def main() -> None:
    """Print the current version."""
    print(f"appmem {__version__}")


if __name__ == "__main__":
    main()
