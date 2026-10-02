"""Pure cumulative-counter rates with a ten-second window and paired clocks."""

from dataclasses import dataclass
from math import isfinite

WINDOW_SECONDS = 10.0


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
    return history, round((cumulative_bytes - history[0].value) / elapsed)
