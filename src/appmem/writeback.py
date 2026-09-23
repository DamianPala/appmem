"""Zswap writeback rate: a sliding ~10 s window over `zswpwb` samples
(SPEC.md "Main view", the `wb` header token).

Pure, over a plain tuple of `(clock, cumulative_bytes)` samples -- no
Textual imports, mirroring `ui/rows.py`'s baseline-dict shape, so
`MainScreen` just holds the returned tuple as one field between ticks and
this stays unit-testable without a running app or a real clock.
"""

from __future__ import annotations

WINDOW_SECONDS = 10.0

Sample = tuple[float, int]
"""`(clock, cumulative_bytes)`. `clock` is whatever monotonic seconds the
caller feeds in (`time.monotonic()` in production); tests pass synthetic
values instead of waiting out a real 10 s window."""


def update_writeback(
    history: tuple[Sample, ...], now: float, cumulative_bytes: int | None
) -> tuple[tuple[Sample, ...], int | None]:
    """Feed one new cumulative `zswpwb` reading (already in bytes); returns
    the updated history and the current rate in bytes/sec.

    The rate is `None`:
    - on the first sample (no history to compare against yet);
    - right after the counter resets or decreases (history is dropped and
      restarted instead of showing a nonsense negative rate);
    - when `cumulative_bytes` is `None` (no zswap support).

    Otherwise it's measured between the newest sample and the oldest one
    still inside the last `WINDOW_SECONDS`, not just the previous tick: a
    sliding window instead of a per-tick delta, so a single quiet tick right
    after writeback stops doesn't flicker the token off and back on -- it
    only goes to 0 once every sample in the window agrees (SPEC.md "Main
    view").
    """
    if cumulative_bytes is None:
        return (), None
    if history and cumulative_bytes < history[-1][1]:
        history = ()  # counter reset (or a kernel wrap): start over, no crash
    history = (*history, (now, cumulative_bytes))

    cutoff = now - WINDOW_SECONDS
    while len(history) > 2 and history[1][0] <= cutoff:
        history = history[1:]  # drop samples older than the window, keep >= 2

    if len(history) < 2:
        return history, None
    oldest_time, oldest_value = history[0]
    newest_time, newest_value = history[-1]
    elapsed = newest_time - oldest_time
    if elapsed <= 0:
        return history, None
    return history, round((newest_value - oldest_value) / elapsed)
