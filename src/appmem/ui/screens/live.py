"""Threaded refresh lifecycle shared by Linux and Darwin live screens."""

from __future__ import annotations

import threading
from collections.abc import Callable

from textual.screen import Screen
from textual.timer import Timer


class LiveScreen(Screen[None]):
    """One in-flight read, generation invalidation, and immediate resume refresh."""

    def __init__(self) -> None:
        super().__init__()
        self._timer: Timer | None = None
        self._tick_in_flight = False
        self._generation = 0

    def _start_live_timer(self, interval: float, callback: Callable[[], None]) -> None:
        self._timer = self.set_interval(interval, callback)

    def _launch_tick(self, worker: Callable[[int], None]) -> None:
        if not self._can_launch_tick():
            return
        self._tick_in_flight = True
        threading.Thread(
            target=worker, args=(self._generation,), name="appmem-tick", daemon=True
        ).start()

    def _can_launch_tick(self) -> bool:
        return self.is_active and not self._tick_in_flight

    def _finish_tick(self) -> None:
        self._tick_in_flight = False

    def _accept_tick(self, generation: int) -> bool:
        return generation == self._generation and self.is_active

    def _invalidate_tick(self) -> None:
        self._generation += 1

    def on_screen_resume(self) -> None:
        self._invalidate_tick()
        self.refresh_now()

    def refresh_now(self, *, scroll: bool = False) -> None:
        raise NotImplementedError
