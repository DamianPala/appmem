"""Unit directory name to app name normalization (SPEC.md "Grouping").

Implements the 11-step pipeline from SPEC.md: unescape systemd ``\\xNN``
escapes, strip the unit suffix, special-case snap and flatpak units, strip
the ``app-`` prefix and instance part, collapse reverse-DNS ids, collapse
snap desktop ids, lowercase, and apply the built-in alias map.
"""

from __future__ import annotations

import re

_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_UUID_SUFFIX_RE = re.compile(
    r"-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DIGIT_SUFFIX_RE = re.compile(r"-\d+$")
_SNAP_RE = re.compile(r"^snap\.([^.]+)\.")
_FLATPAK_RE = re.compile(r"^app-flatpak-(.+)-\d+$")
_UNIT_SUFFIXES = (".service", ".scope")

# Alias map (SPEC "Grouping"): exact entries match the whole normalized name,
# prefix entries match its start. Values are already lowercase.
_ALIAS_EXACT = {
    "ghostty-surface-transient": "ghostty",
    "brave-browser": "brave",
    "google-chrome": "chrome",
    "element-desktop": "element",
    "superproductivity-bin": "superproductivity",
}
_ALIAS_PREFIX = {
    "libreoffice-": "libreoffice",
    "plasma-": "plasma",
}


def _unescape(name: str) -> str:
    """Unescape systemd ``\\xNN`` byte escapes and decode the result as UTF-8.

    systemd escapes a non-ASCII desktop id one byte at a time (``微信``
    becomes ``\\xe5\\xbe\\xae\\xe4\\xbf\\xa1``), so the escaped bytes are
    collected into one buffer and decoded together, not mapped one code
    point per escape -- that would turn each byte into its own character
    instead of the multi-byte UTF-8 character they spell out together. An
    invalid byte sequence decodes to U+FFFD instead of raising.
    """
    raw = bytearray()
    pos = 0
    for match in _ESCAPE_RE.finditer(name):
        raw += name[pos : match.start()].encode("utf-8", errors="replace")
        raw.append(int(match.group(1), 16))
        pos = match.end()
    raw += name[pos:].encode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _strip_unit_suffix(name: str) -> str:
    for suffix in _UNIT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _strip_instance_part(name: str) -> str:
    """Strip ``@<anything>``, a trailing ``-<uuid>``, then a trailing ``-<digits>``."""
    at_index = name.find("@")
    if at_index != -1:
        name = name[:at_index]
    name = _UUID_SUFFIX_RE.sub("", name)
    name = _DIGIT_SUFFIX_RE.sub("", name)
    return name


def _collapse_reverse_dns(name: str) -> str:
    """Drop the leading TLD + vendor labels of a 3+-label reverse-DNS id (step 8)."""
    labels = name.split(".")
    if len(labels) >= 3:
        return ".".join(labels[2:])
    return name


def _collapse_snap_desktop_id(name: str) -> str:
    """Collapse ``<x>_<x>`` snap desktop ids to ``<x>`` (step 9)."""
    parts = name.split("_")
    if len(parts) == 2 and parts[0] == parts[1]:
        return parts[0]
    return name


def _apply_alias(name: str) -> str:
    if name in _ALIAS_EXACT:
        return _ALIAS_EXACT[name]
    for prefix, app in _ALIAS_PREFIX.items():
        if name.startswith(prefix):
            return app
    return name


def app_name(unit_dirname: str) -> str:
    """Normalize a unit directory name into an app name.

    Implements SPEC.md "Grouping" steps 1-11: unescape, strip the unit
    suffix, special-case snap (``snap.<name>.<app>-<uuid>``) and flatpak
    (``app-flatpak-<id>-<n>``) names, strip the ``app-`` prefix and instance
    part, collapse reverse-DNS ids and snap desktop ids, lowercase, and
    apply the built-in alias map. Units that match nothing keep their
    normalized name as their own app.
    """
    name = _unescape(unit_dirname)
    name = _strip_unit_suffix(name)

    snap_match = _SNAP_RE.match(name)
    if snap_match:
        # Snap: go straight to lowercase + alias (steps 10-11).
        return _apply_alias(snap_match.group(1).lower())

    flatpak_match = _FLATPAK_RE.match(name)
    if flatpak_match:
        # Flatpak: go straight to reverse-DNS collapse onward (steps 8-11).
        name = _collapse_reverse_dns(flatpak_match.group(1))
        name = _collapse_snap_desktop_id(name)
        return _apply_alias(name.lower())

    if name.startswith("app-"):
        name = name[len("app-") :]
    name = _strip_instance_part(name)
    name = _collapse_reverse_dns(name)
    name = _collapse_snap_desktop_id(name)
    return _apply_alias(name.lower())
