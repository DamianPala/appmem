"""Tests for `appmem.fmt` (SPEC.md "Behaviour details", "Definitions")."""

import pytest

from appmem.fmt import (
    format_age,
    format_delta,
    format_elapsed,
    format_pair,
    pressure_word,
    size,
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
    assert pressure_word(some_avg10=0.99, full_avg10=0.0) == "none"


def test_pressure_high_above_five_percent_full() -> None:
    assert pressure_word(some_avg10=2.0, full_avg10=5.1) == "high"


def test_pressure_some_shows_value() -> None:
    assert pressure_word(some_avg10=3.2, full_avg10=1.0) == "some (3.2 %)"


def test_pressure_boundaries_are_exclusive() -> None:
    # some avg10 == 1 is not < 1, so it is not "none".
    assert pressure_word(some_avg10=1.0, full_avg10=0.0) == "some (1.0 %)"
    # full avg10 == 5 is not > 5, so it is not "high".
    assert pressure_word(some_avg10=2.0, full_avg10=5.0) == "some (2.0 %)"


def test_format_delta_zero_is_bare_zero() -> None:
    assert format_delta(0) == "0"


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
