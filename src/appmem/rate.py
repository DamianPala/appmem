"""Pure cumulative-counter rates with a five-second window and paired clocks."""

from dataclasses import dataclass
from math import isfinite

WINDOW_SECONDS = 5.0
# A rate over a shorter span than this is noise: the two samples taken at
# start-up (mount, then resume) are 10 to 20 ms apart and would show a
# spike from a single page. Capped at half a second so a long refresh
# interval does not delay the first rate beyond its second tick.
_MIN_SPAN_SECONDS = 0.5


@dataclass(frozen=True)
class Sample:
    monotonic: float
    wall: float
    value: int


def update_rate(
    history: tuple[Sample, ...],
    now: float,
    cumulative_bytes: int | None,
    *,
    wall: float,
    interval: float = 1.0,
) -> tuple[tuple[Sample, ...], int | None]:
    """Unknown on invalid/reset/discontinuous samples; zero only after measurement.

    Keep the sample immediately before the window boundary to cover sparse ticks.
    Unknown until the oldest kept sample is at least half an interval (at most
    half a second) behind the newest.
    Long gaps allow three configured refresh intervals, including sixty-second ticks.
    """
    if type(cumulative_bytes) is not int or cumulative_bytes < 0:
        return (), None
    if not isfinite(now) or not isfinite(wall):
        return (), None
    sample = Sample(now, wall, cumulative_bytes)
    if history:
        previous = history[-1]
        elapsed = now - previous.monotonic
        wall_elapsed = wall - previous.wall
        if (
            elapsed <= 0
            or elapsed > max(30.0, 3 * interval)
            or abs(wall_elapsed - elapsed) > 5.0
            or cumulative_bytes < previous.value
        ):
            return (sample,), None
    history = (*history, sample)
    cutoff = now - WINDOW_SECONDS
    while len(history) > 2 and history[1].monotonic <= cutoff:
        history = history[1:]
    if len(history) < 2:
        return history, None
    elapsed = now - history[0].monotonic
    if elapsed < min(0.5 * interval, _MIN_SPAN_SECONDS):
        return history, None
    return history, round((cumulative_bytes - history[0].value) / elapsed)
