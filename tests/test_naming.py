"""Tests for `appmem.naming.app_name` (SPEC.md "Grouping")."""

import pytest

from appmem.naming import app_name

# SPEC.md "Grouping" acceptance table, copied verbatim.
ACCEPTANCE_TABLE = [
    ("app-com.mitchellh.ghostty.service", "ghostty"),
    ("app-ghostty\\x2d2@7b755c4e18184688b9c5e64a8ceb245d.service", "ghostty"),
    ("app-ghostty-surface-transient-4172209.scope", "ghostty"),
    ("app-brave\\x2dbrowser@c16f78223b3c4371a764a76609ae9ef0.service", "brave"),
    ("app-org.chromium.Chromium-4020402.scope", "chromium"),
    ("app-com.google.Chrome-3242011.scope", "chrome"),
    ("app-google\\x2dchrome@c16f78223b3c4371a764a76609ae9ef0.service", "chrome"),
    ("app-element-3663554.scope", "element"),
    ("app-element\\x2ddesktop@c16f78223b3c4371a764a76609ae9ef0.service", "element"),
    ("app-superproductivity-bin-100001.scope", "superproductivity"),
    ("app-libreoffice\\x2dcalc@c16f78223b3c4371a764a76609ae9ef0.service", "libreoffice"),
    ("app-org.kde.kate@c16f78223b3c4371a764a76609ae9ef0.service", "kate"),
    ("app-org.kde.discover.notifier@autostart.service", "discover.notifier"),
    ("app-org.kde.konsole-2315830.scope", "konsole"),
    (
        "snap.telegram-desktop.telegram-desktop-e44637ac-ec33-42ac-b09e-2fe496512c47.scope",
        "telegram-desktop",
    ),
    (
        "snap.thunderbird.thunderbird-9da00651-f149-4b5a-9e13-3825933dfa79.scope",
        "thunderbird",
    ),
    ("app-thunderbird_thunderbird@c16f78223b3c4371a764a76609ae9ef0.service", "thunderbird"),
    (
        "snap.bitwarden.bitwarden-93d0664d-1c21-435b-9ec0-b47f988103f8.scope",
        "bitwarden",
    ),
    ("app-bitwarden_bitwarden@c16f78223b3c4371a764a76609ae9ef0.service", "bitwarden"),
    ("plasma-kwin_wayland.service", "plasma"),
    ("plasma-powerdevil.service", "plasma"),
    ("pipewire.service", "pipewire"),
]


@pytest.mark.parametrize(("unit_dirname", "expected"), ACCEPTANCE_TABLE)
def test_acceptance_table(unit_dirname: str, expected: str) -> None:
    assert app_name(unit_dirname) == expected


def test_x2e_escape_is_unescaped_before_reverse_dns_collapse() -> None:
    # \x2e is ".": the escaped name is a 3-label reverse-DNS id, so it collapses
    # to its last label exactly like the unescaped equivalent would.
    assert app_name("app-org\\x2eexample\\x2etestapp.service") == "testapp"


def test_x2e_escape_in_instance_part() -> None:
    # \x2d is "-": unescaping must happen before the instance part is stripped,
    # otherwise the literal "\x2d" text would break the trailing-digits strip.
    assert app_name("app-foo\\x2dbar-123.service") == "foo-bar"


def test_flatpak_name() -> None:
    # app-flatpak-<id>-<n> -> <id>, then reverse-DNS collapse (SPEC step 5 -> step 8).
    assert app_name("app-flatpak-org.mozilla.firefox-12345.scope") == "firefox"


def test_unknown_shape_keeps_normalized_name() -> None:
    assert app_name("weird-thing.service") == "weird-thing"


def test_empty_name() -> None:
    assert app_name("") == ""
