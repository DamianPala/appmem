"""Exact cumulative session differences, independent of rate windows and clocks."""

from dataclasses import dataclass

from appmem.fmt import size


@dataclass
class SessionCounter:
    """One direction's baseline begins on the first accepted host snapshot.

    Missing initial coverage or any observed decrease permanently invalidates the
    full-session difference. Temporary missing readings retain the original baseline.
    """

    started: bool = False
    baseline: int | None = None
    previous: int | None = None
    total: int | None = None

    def update(self, value: int | None) -> None:
        valid = type(value) is int and value >= 0
        if not self.started:
            self.started = True
            self.baseline = value if valid else None
        if not valid:
            self.total = None
            return
        assert value is not None
        if self.previous is not None and value < self.previous:
            self.baseline = None
        self.previous = value
        self.total = None if self.baseline is None else value - self.baseline


def total_amount(value: int | None) -> str:
    """Cumulative traffic can reach TiB and beyond while memory sizes stay small."""
    if type(value) is not int or value < 0:
        return "unavailable"
    for power, unit in ((60, "EiB"), (50, "PiB"), (40, "TiB")):
        if value >= 2**power:
            return f"{value / 2**power:.1f} {unit}"
    return size(value)
