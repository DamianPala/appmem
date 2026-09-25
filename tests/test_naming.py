"""Tests for `appmem.naming.app_name` (SPEC.md "Grouping")."""

import pytest

from appmem.naming import app_name, is_generic_desktop_id, scope_leader_pid

# SPEC.md "Grouping" acceptance table, copied verbatim. The
# `app-org.chromium.Chromium-4020402.scope` row is excluded: SPEC.md now
# describes that unit's app name as its leader process's name (see
# `test_generic_desktop_id_unit_app_name_still_reports_the_shared_id`
# below), which `app_name` -- tested here -- can't produce since it never
# looks at a process.
ACCEPTANCE_TABLE = [
    ("app-com.mitchellh.ghostty.service", "ghostty"),
    ("app-ghostty\\x2d2@7b755c4e18184688b9c5e64a8ceb245d.service", "ghostty"),
    ("app-ghostty-surface-transient-4172209.scope", "ghostty"),
    ("app-brave\\x2dbrowser@c16f78223b3c4371a764a76609ae9ef0.service", "brave"),
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
    # app-flatpak-<id>-<n> -> <id>, then reverse-DNS collapse (SPEC step 5 -> step 9).
    assert app_name("app-flatpak-org.mozilla.firefox-12345.scope") == "firefox"


def test_unknown_shape_keeps_normalized_name() -> None:
    assert app_name("weird-thing.service") == "weird-thing"


def test_empty_name() -> None:
    assert app_name("") == ""


# --- multi-byte UTF-8 unit names ------------------------------------------------


def test_cjk_name_escaped_byte_by_byte_decodes_as_utf8() -> None:
    # systemd escapes 微信 (WeChat) one UTF-8 byte at a time: each of the two
    # 3-byte characters becomes three separate \xNN escapes. Decoding the
    # escapes one at a time (instead of collecting the raw bytes first and
    # decoding once) would turn each byte into its own mangled character.
    escaped = "app-\\xe5\\xbe\\xae\\xe4\\xbf\\xa1.service"
    assert app_name(escaped) == "微信"


def test_emoji_name_escaped_byte_by_byte_decodes_as_utf8() -> None:
    # An emoji outside the BMP (4-byte UTF-8 sequence), same escaping shape.
    escaped = "app-\\xf0\\x9f\\x9a\\x80.service"
    assert app_name(escaped) == "🚀"


def test_invalid_byte_sequence_decodes_to_replacement_char_without_raising() -> None:
    # \xff is never valid as the start of a UTF-8 sequence.
    escaped = "app-\\xff\\xfe.service"
    assert app_name(escaped) == "��"


def test_generic_desktop_id_unit_app_name_still_reports_the_shared_id() -> None:
    # SPEC.md "Grouping" acceptance table: in the running app this unit's
    # name is its leader process's name, e.g. `obsidian` (see
    # `appmem.collect.unit_app_name`) -- but `app_name` itself is pure and
    # unaware of any process, so it still returns the shared desktop id
    # every such unit reports to the compositor.
    assert app_name("app-org.chromium.Chromium-4020402.scope") == "chromium"


# --- generic desktop ids ---------------------------------------------------------


def test_is_generic_desktop_id_true_for_chromium_scope() -> None:
    assert is_generic_desktop_id("app-org.chromium.Chromium-1907544.scope") is True


def test_is_generic_desktop_id_false_for_chrome_scope() -> None:
    # Chrome sets its own id (`com.google.Chrome`): not affected.
    assert is_generic_desktop_id("app-com.google.Chrome-3242011.scope") is False


def test_is_generic_desktop_id_false_for_flatpak_shape() -> None:
    assert is_generic_desktop_id("app-flatpak-org.chromium.Chromium-12345.scope") is False


def test_is_generic_desktop_id_false_for_snap_shape() -> None:
    id_ = "snap.chromium.chromium-93d0664d-1c21-435b-9ec0-b47f988103f8.scope"
    assert is_generic_desktop_id(id_) is False


def test_scope_leader_pid_parses_the_trailing_digits() -> None:
    assert scope_leader_pid("app-org.chromium.Chromium-1907544.scope") == 1907544


def test_scope_leader_pid_none_for_an_at_instance_unit() -> None:
    unit = "app-brave\\x2dbrowser@c16f78223b3c4371a764a76609ae9ef0.service"
    assert scope_leader_pid(unit) is None


def test_scope_leader_pid_none_with_no_trailing_digits() -> None:
    assert scope_leader_pid("pipewire.service") is None
