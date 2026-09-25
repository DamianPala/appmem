"""Unit directory name to app name normalization (SPEC.md "Grouping").

Implements the 12-step pipeline from SPEC.md: unescape systemd ``\\xNN``
escapes, strip the unit suffix, special-case snap and flatpak units, strip
the ``app-`` prefix and instance part, collapse reverse-DNS ids, collapse
snap desktop ids, lowercase, and apply the built-in alias map. Step 8 (a
generic desktop id named after its leader process) needs `/proc`, so it
lives in `collect.unit_app_name`; this module only recognizes such ids.
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
    "mullvad": "mullvad-vpn",
}
_ALIAS_PREFIX = {
    "libreoffice-": "libreoffice",
    "plasma-": "plasma",
}

# Desktop ids Electron apps and Chromium forks without an id of their own
# report to the compositor (SPEC.md "Grouping"), so KDE names their transient
# scope after the id instead of the program -- several unrelated programs
# then collapse into one row unless the unit is named after its leader
# process instead.
_GENERIC_DESKTOP_IDS = frozenset({"org.chromium.chromium"})


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
    """Drop the leading TLD + vendor labels of a 3+-label reverse-DNS id (step 9)."""
    labels = name.split(".")
    if len(labels) >= 3:
        return ".".join(labels[2:])
    return name


def _collapse_snap_desktop_id(name: str) -> str:
    """Collapse ``<x>_<x>`` snap desktop ids to ``<x>`` (step 10)."""
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


def unit_label(unit_dirname: str) -> str:
    """Decode a unit directory name for display, without the rest of the app
    normalization pipeline: just the systemd ``\\xNN`` unescape and UTF-8
    decode `app_name` also starts from. Callers
    that print this to a terminal still need `render.escape_control_chars`,
    same as any other name read straight off the system."""
    return _unescape(unit_dirname)


def app_name(unit_dirname: str) -> str:
    """Normalize a unit directory name into an app name.

    Implements SPEC.md "Grouping" steps 1-7 and 9-12: unescape, strip the unit
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
        # Snap: go straight to lowercase + alias (steps 11-12).
        return _apply_alias(snap_match.group(1).lower())

    flatpak_match = _FLATPAK_RE.match(name)
    if flatpak_match:
        # Flatpak: go straight to reverse-DNS collapse onward (steps 9-12).
        name = _collapse_reverse_dns(flatpak_match.group(1))
        name = _collapse_snap_desktop_id(name)
        return _apply_alias(name.lower())

    if name.startswith("app-"):
        name = name[len("app-") :]
    name = _strip_instance_part(name)
    name = _collapse_reverse_dns(name)
    name = _collapse_snap_desktop_id(name)
    return _apply_alias(name.lower())


def _strip_app_prefix_and_instance(unit_dirname: str) -> str:
    """Unescape and strip the unit suffix, the ``app-`` prefix and the
    instance part (steps 1-3, 6-7) -- the point at which a generic desktop id
    is recognized, before the reverse-DNS collapse (step 9) would erase it."""
    name = _unescape(unit_dirname)
    name = _strip_unit_suffix(name)
    if name.startswith("app-"):
        name = name[len("app-") :]
    return _strip_instance_part(name)


def is_generic_desktop_id(unit_dirname: str) -> bool:
    """Whether `unit_dirname` names a desktop id Electron apps and Chromium
    forks without an id of their own report to the compositor (SPEC.md
    "Grouping"), compared lowercase after steps 1-3 and 6-7, before the
    reverse-DNS collapse."""
    return _strip_app_prefix_and_instance(unit_dirname).lower() in _GENERIC_DESKTOP_IDS


def scope_leader_pid(unit_dirname: str) -> int | None:
    """The pid in a `.scope` unit's trailing ``-<digits>`` instance part
    (SPEC.md "Grouping": a generic desktop id's leader process), or `None`
    for an ``@<instance>`` unit or one with no trailing digits."""
    name = _unescape(unit_dirname)
    name = _strip_unit_suffix(name)
    if "@" in name:
        return None
    match = _DIGIT_SUFFIX_RE.search(name)
    return int(match.group()[1:]) if match else None


def normalize_process_name(name: str) -> str:
    """Steps 11-12 of the app-name pipeline (lowercase, then the alias map)
    applied to a name that didn't come from a unit directory -- a generic
    desktop id's leader process name (SPEC.md "Grouping")."""
    return _apply_alias(name.lower())
