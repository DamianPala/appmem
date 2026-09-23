"""Tests for `appmem.fmt` (SPEC.md "Behaviour details", "Definitions")."""

import pytest
from rich.cells import cell_len

from appmem.fmt import (
    ellipsize_middle,
    format_age,
    format_delta,
    format_elapsed,
    format_pair,
    format_rate,
    format_zswap_part,
    pressure_word,
    size,
    status_line_command,
    truncate_name,
)

SIZE_CASES = [
    (0, "0 B"),
    (1023, "1023 B"),
    (1024, "1 KiB"),
    (1023 * 1024, "1023 KiB"),
    (1024**2, "1 MiB"),
    (1023 * 1024**2, "1023 MiB"),
    (1024**3, "1.0 GiB"),
    (1024**2 - 1, "1 MiB"),
    (1024**3 - 1, "1.0 GiB"),
]


@pytest.mark.parametrize(("num_bytes", "expected"), SIZE_CASES)
def test_size_unit_boundaries(num_bytes: int, expected: str) -> None:
    assert size(num_bytes) == expected


def test_size_gib_keeps_one_decimal() -> None:
    assert size(int(16.8 * 1024**3)) == "16.8 GiB"


def test_pressure_none_below_one_percent() -> None:
    assert pressure_word(some_avg10=0.99, some_avg60=0.0, full_avg10=0.0) == "none"


def test_pressure_high_above_five_percent_full() -> None:
    assert pressure_word(some_avg10=2.0, some_avg60=0.0, full_avg10=5.1) == "high"


def test_pressure_high_when_some_avg10_above_twenty_percent() -> None:
    # A lot of tasks waiting even without a full stall yet.
    assert pressure_word(some_avg10=20.1, some_avg60=0.0, full_avg10=0.0) == "high"


def test_pressure_some_shows_value() -> None:
    assert pressure_word(some_avg10=3.2, some_avg60=0.0, full_avg10=1.0) == "some (3.2 %)"


def test_pressure_boundaries_are_exclusive() -> None:
    # some avg10 == 1 is "some" (threshold is >=).
    assert pressure_word(some_avg10=1.0, some_avg60=0.0, full_avg10=0.0) == "some (1.0 %)"
    # full avg10 == 5 is not > 5, so it is not "high".
    assert pressure_word(some_avg10=2.0, some_avg60=0.0, full_avg10=5.0) == "some (2.0 %)"
    # some avg10 == 20 is not > 20, so it is not "high" via that trigger.
    assert pressure_word(some_avg10=20.0, some_avg60=0.0, full_avg10=0.0) == "some (20.0 %)"


def test_pressure_none_mentions_last_minute_when_avg60_above_one_percent() -> None:
    assert (
        pressure_word(some_avg10=0.5, some_avg60=1.5, full_avg10=0.0)
        == "none (some 1.5 % last min)"
    )


def test_pressure_none_bare_when_avg60_at_or_below_one_percent() -> None:
    assert pressure_word(some_avg10=0.5, some_avg60=1.0, full_avg10=0.0) == "none"


def test_format_delta_zero_is_dim_dot() -> None:
    # |Δ| < 1 MiB renders as a dim `·`, not a bare "0".
    assert format_delta(0) == "·"


def test_format_delta_under_one_mib_is_dim_dot() -> None:
    assert format_delta(1024**2 - 1) == "·"
    assert format_delta(-(1024**2 - 1)) == "·"


def test_format_delta_positive_gets_plus_sign() -> None:
    assert format_delta(2 * 1024**2) == "+2 MiB"


def test_format_delta_negative_gets_minus_sign() -> None:
    assert format_delta(-2 * 1024**2) == "-2 MiB"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (42, "42s"),
        (59, "59s"),
        (60, "1m"),
        (300, "5m"),
        (3599, "59m"),
        (3600, "1h"),
        (3600 + 12 * 60, "1h12m"),
    ],
)
def test_format_elapsed(seconds: float, expected: str) -> None:
    assert format_elapsed(seconds) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m"),
        (12 * 60, "12m"),
        (3599, "59m"),
        (3600, "1h"),
        (2 * 3600, "2h"),
        (86399, "23h"),
        (86400, "1d"),
        (86 * 86400, "86d"),
    ],
)
def test_format_age(seconds: float, expected: str) -> None:
    # Unlike format_elapsed, AGE never compounds units (SPEC.md "Process view").
    assert format_age(seconds) == expected


def test_format_pair_shares_unit_when_both_sides_pick_the_same_one() -> None:
    assert format_pair(int(18.7 * 1024**3), int(30.9 * 1024**3)) == "18.7/30.9 GiB"


def test_format_pair_keeps_separate_units_when_they_differ() -> None:
    assert format_pair(512 * 1024**2, 1024**3) == "512 MiB / 1.0 GiB"


def test_truncate_name_keeps_short_names_as_is() -> None:
    assert truncate_name("ghostty") == "ghostty"


def test_truncate_name_caps_at_32_with_an_ellipsis() -> None:
    name = "x" * 40
    result = truncate_name(name)

    assert len(result) == 32
    assert result.endswith("…")
    assert result == "x" * 31 + "…"


def test_truncate_name_exactly_at_cap_is_not_truncated() -> None:
    name = "x" * 32
    assert truncate_name(name) == name


# --- truncate_name counts terminal cells, not code points ----------------------


def test_truncate_name_counts_cjk_cells_not_code_points() -> None:
    # Each CJK char is 2 cells wide: 20 of them (40 cells, but only 20 code
    # points) must be capped by cell width, not by `len()`, or the naive
    # code-point slice would leave up to 2x `cap` columns on screen.
    name = "微" * 20
    result = truncate_name(name, cap=32)

    assert cell_len(result) <= 32
    assert result.endswith("…")
    assert len(result) < len(name)  # actually cropped, not left as-is


def test_truncate_name_never_splits_a_wide_char_at_the_cut() -> None:
    # A cap that lands mid-way through a double-wide character must not
    # produce a truncated/invalid glyph: `set_cell_size` pads with a space
    # instead of slicing the character in half.
    name = "微" * 20
    result = truncate_name(name, cap=17)  # odd cap: an exact half-CJK-char cut

    assert cell_len(result) <= 17
    assert "微" * 8 in result  # 8 full CJK chars (16 cells) survive intact


def test_truncate_name_emoji_zwj_sequence_stays_intact_or_is_dropped_whole() -> None:
    # A family emoji is a ZWJ sequence (4 code points glued by U+200D): a
    # code-point-based cut could leave a dangling ZWJ or half the sequence.
    name = "👨‍👩‍👧‍👦" * 6
    result = truncate_name(name, cap=12)

    assert cell_len(result) <= 12
    assert "‍‍" not in result  # no orphaned double ZWJ from a bad cut


def test_truncate_name_combining_marks_count_as_their_base_char() -> None:
    # "e" + combining acute accent (U+0301) is one visual column, two code
    # points: a code-point cap would cut this name roughly twice as short as
    # a cell-based cap does.
    name = "é" * 20  # 20 visual columns, 40 code points
    result = truncate_name(name, cap=32)

    assert cell_len(result) <= 32
    # Cell-based capping keeps far more visual characters than a naive
    # `name[:31]` code-point slice (which would cut mid-pair, at column ~16).
    assert cell_len(result) > 16


def test_ellipsize_middle_keeps_short_text_as_is() -> None:
    assert ellipsize_middle("short", 32) == "short"


def test_ellipsize_middle_cuts_the_middle() -> None:
    assert ellipsize_middle("abcdefgh", 5) == "ab…gh"


def test_ellipsize_middle_exactly_at_cap_is_not_truncated() -> None:
    name = "x" * 10
    assert ellipsize_middle(name, 10) == name


def test_status_line_command_user_scope() -> None:
    line = status_line_command("user", "app-ghostty.service", None, width=200)
    assert line == "systemctl --user stop 'app-ghostty.service'"


def test_status_line_command_system_scope() -> None:
    line = status_line_command("system", "cups.service", None, width=200)
    assert line == "sudo systemctl stop 'cups.service'"


def test_status_line_command_adds_kill_pid_for_a_process_row() -> None:
    line = status_line_command("user", "app-ghostty.service", 12345, width=200)
    assert line == "systemctl --user stop 'app-ghostty.service'   kill 12345"


def test_format_rate_reuses_size_and_appends_per_second() -> None:
    assert format_rate(12 * 1024 * 1024) == "12 MiB/s"
    assert format_rate(0) == "0 B/s"


def test_format_zswap_part_reads_x_zswap_in_y() -> None:
    zswapped = int(6.7 * 1024**3)
    pool = int(1.9 * 1024**3)
    assert format_zswap_part(zswapped, pool) == "6.7 GiB zswap in 1.9 GiB"


def test_status_line_command_ellipsizes_unit_when_wider_than_terminal() -> None:
    unit = "app-" + "x" * 60 + ".service"
    line = status_line_command("user", unit, None, width=40)

    assert len(line) <= 40
    assert line.startswith("systemctl --user stop '")
    assert "…" in line
