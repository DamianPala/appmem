"""Shared cumulative rate window, first sample, zero and counter reset behavior."""

from __future__ import annotations

import pytest

from appmem.rate import Sample, update_rate


def test_first_sample_has_no_rate() -> None:
    history, rate = update_rate((), 0.0, 1000, wall=0.0)

    assert rate is None
    assert history == (Sample(0.0, 0.0, 1000),)


def test_source_unavailable_has_no_rate_and_clears_history() -> None:
    history, _ = update_rate((), 0.0, 1000, wall=0.0)
    history, rate = update_rate(history, 1.0, None, wall=1.0)

    assert rate is None
    assert history == ()


def test_two_samples_one_second_apart_give_bytes_per_second() -> None:
    history, _ = update_rate((), 0.0, 0, wall=0.0)
    _, rate = update_rate(history, 1.0, 1024 * 1024, wall=1.0)

    assert rate == 1024 * 1024


def test_rate_uses_the_oldest_sample_still_inside_the_window() -> None:
    # Growth happens early in a 5 s window, then flatlines: the rate should
    # still reflect the whole window (oldest-to-newest), not just the last
    # tick's zero delta -- this is what keeps the token from flickering off
    # on the very next quiet tick.
    history: tuple[Sample, ...] = ()
    history, _ = update_rate(history, 0.0, 0, wall=0.0)
    history, _ = update_rate(history, 1.0, 10 * 1024 * 1024, wall=1.0)  # grew by 10 MiB in 1 s
    history, rate = update_rate(history, 2.0, 10 * 1024 * 1024, wall=2.0)  # flat since

    # Window spans [0, 2]: 10 MiB grown over 2 s = 5 MiB/s, not 0.
    assert rate == 5 * 1024 * 1024


def test_rate_goes_to_zero_once_every_sample_in_the_window_agrees() -> None:
    history: tuple[Sample, ...] = ()
    history, _ = update_rate(history, 0.0, 0, wall=0.0)
    history, _ = update_rate(history, 1.0, 10 * 1024 * 1024, wall=1.0)
    # Keep sampling a flat value until the growing sample ages out of the
    # 5 s window entirely.
    rate: int | None = None
    for t in range(2, 12):
        history, rate = update_rate(history, float(t), 10 * 1024 * 1024, wall=float(t))

    assert rate == 0


def test_window_remembers_a_burst_for_five_seconds_and_then_forgets_it() -> None:
    history: tuple[Sample, ...] = ()
    history, _ = update_rate(history, 0.0, 0, wall=0.0)
    history, _ = update_rate(history, 1.0, 10 * 1024 * 1024, wall=1.0)
    rates: dict[int, int | None] = {}
    for t in range(2, 8):
        history, rates[t] = update_rate(history, float(t), 10 * 1024 * 1024, wall=float(t))
    assert rates[5] == 2 * 1024 * 1024  # 10 MiB over the 5 s window
    assert rates[7] == 0


@pytest.mark.parametrize(("gap", "known"), [(0.01, False), (0.3, False), (0.6, True), (1.0, True)])
def test_first_frame_needs_samples_about_an_interval_apart(gap: float, known: bool) -> None:
    history, _ = update_rate((), 0.0, 0, wall=0.0)
    _, rate = update_rate(history, gap, 1024, wall=gap)
    assert (rate is not None) is known


def test_counter_reset_gives_no_token_and_does_not_crash() -> None:
    history, _ = update_rate((), 0.0, 5 * 1024 * 1024, wall=0.0)
    history, rate = update_rate(history, 1.0, 1024, wall=1.0)  # smaller than before: a reset

    assert rate is None
    assert history == (Sample(1.0, 1.0, 1024),)  # restarted, not crashed


def test_counter_reset_then_recovers_with_a_fresh_rate() -> None:
    history, _ = update_rate((), 0.0, 5 * 1024 * 1024, wall=0.0)
    history, _ = update_rate(history, 1.0, 0, wall=1.0)  # reset
    _, rate = update_rate(history, 2.0, 2 * 1024 * 1024, wall=2.0)

    assert rate == 2 * 1024 * 1024


def test_equal_consecutive_values_are_not_treated_as_a_reset() -> None:
    history, _ = update_rate((), 0.0, 1000, wall=0.0)
    history, rate = update_rate(history, 1.0, 1000, wall=1.0)

    assert rate == 0
    assert history == (Sample(0.0, 0.0, 1000), Sample(1.0, 1.0, 1000))
