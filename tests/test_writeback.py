"""Tests for `appmem.writeback.update_writeback` (SPEC.md "Main view": the
`to disk` header token, a sliding ~10 s window over `zswpwb` samples)."""

from __future__ import annotations

from appmem.writeback import update_writeback


def test_first_sample_has_no_rate() -> None:
    history, rate = update_writeback((), 0.0, 1000)

    assert rate is None
    assert history == ((0.0, 1000),)


def test_source_unavailable_has_no_rate_and_clears_history() -> None:
    history, _ = update_writeback((), 0.0, 1000)
    history, rate = update_writeback(history, 1.0, None)

    assert rate is None
    assert history == ()


def test_two_samples_one_second_apart_give_bytes_per_second() -> None:
    history, _ = update_writeback((), 0.0, 0)
    _, rate = update_writeback(history, 1.0, 1024 * 1024)

    assert rate == 1024 * 1024


def test_rate_uses_the_oldest_sample_still_inside_the_window() -> None:
    # Growth happens early in a 10 s window, then flatlines: the rate should
    # still reflect the whole window (oldest-to-newest), not just the last
    # tick's zero delta -- this is what keeps the token from flickering off
    # on the very next quiet tick.
    history: tuple[tuple[float, int], ...] = ()
    history, _ = update_writeback(history, 0.0, 0)
    history, _ = update_writeback(history, 1.0, 10 * 1024 * 1024)  # grew by 10 MiB in 1 s
    history, rate = update_writeback(history, 2.0, 10 * 1024 * 1024)  # flat since

    # Window spans [0, 2]: 10 MiB grown over 2 s = 5 MiB/s, not 0.
    assert rate == 5 * 1024 * 1024


def test_rate_goes_to_zero_once_every_sample_in_the_window_agrees() -> None:
    history: tuple[tuple[float, int], ...] = ()
    history, _ = update_writeback(history, 0.0, 0)
    history, _ = update_writeback(history, 1.0, 10 * 1024 * 1024)
    # Keep sampling a flat value until the growing sample ages out of the
    # 10 s window entirely.
    rate: int | None = None
    for t in range(2, 12):
        history, rate = update_writeback(history, float(t), 10 * 1024 * 1024)

    assert rate == 0


def test_counter_reset_gives_no_token_and_does_not_crash() -> None:
    history, _ = update_writeback((), 0.0, 5 * 1024 * 1024)
    history, rate = update_writeback(history, 1.0, 1024)  # smaller than before: a reset

    assert rate is None
    assert history == ((1.0, 1024),)  # restarted, not crashed


def test_counter_reset_then_recovers_with_a_fresh_rate() -> None:
    history, _ = update_writeback((), 0.0, 5 * 1024 * 1024)
    history, _ = update_writeback(history, 1.0, 0)  # reset
    _, rate = update_writeback(history, 2.0, 2 * 1024 * 1024)

    assert rate == 2 * 1024 * 1024


def test_equal_consecutive_values_are_not_treated_as_a_reset() -> None:
    history, _ = update_writeback((), 0.0, 1000)
    history, rate = update_writeback(history, 1.0, 1000)

    assert rate == 0
    assert history == ((0.0, 1000), (1.0, 1000))
