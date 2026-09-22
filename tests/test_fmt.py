"""Tests for `appmem.fmt` (SPEC.md "Behaviour details", "Definitions")."""

import pytest

from appmem.fmt import pressure_word, size

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
