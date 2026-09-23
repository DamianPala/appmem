"""appmem: live terminal view of RAM and swap usage per application."""

from importlib.metadata import version

# pyproject.toml's [project] version is the single source; read it back from the
# installed distribution's metadata instead of duplicating it here by hand.
__version__ = version(__name__)
