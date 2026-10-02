"""Threaded refresh lifecycle shared by Linux and Darwin live screens."""

from __future__ import annotations

import threading
from collections.abc import Callable

from textual.screen import Screen
from textual.timer import Timer

from appmem.ui.host_panel import HostPanel


class LiveScreen(Screen[None]):
    """One in-flight read, generation invalidation, and immediate resume refresh."""

    def __init__(self) -> None:
        super().__init__()
        self._timer: Timer | None = None
        self._tick_in_flight = False
        self._generation = 0
        self._host_panel_open = False

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
        return self._live_visible() and not self._tick_in_flight

    def _resumed_from_host_panel(self) -> bool:
        if not self._host_panel_open:
            return False
        self._host_panel_open = False
        return True

    def _live_visible(self) -> bool:
        screen = self.app.screen  # pyright: ignore[reportUnknownMemberType]
        return self.is_active or (isinstance(screen, HostPanel) and screen.owner is self)

    def _notify_host_panel(self) -> None:
        screen = self.app.screen  # pyright: ignore[reportUnknownMemberType]
        if isinstance(screen, HostPanel) and screen.owner is self:
            screen.update_sample()

    def _finish_tick(self) -> None:
        self._tick_in_flight = False

    def _accept_tick(self, generation: int) -> bool:
        return generation == self._generation and self._live_visible()

    def _invalidate_tick(self) -> None:
        self._generation += 1

    def on_screen_resume(self) -> None:
        # Textual dispatches handlers on every class in the MRO. Dashboard
        # overrides already handle resume; running this too duplicates sampling.
        if type(self).on_screen_resume is not LiveScreen.on_screen_resume:
            return
        self._invalidate_tick()
        self.refresh_now()

    def refresh_now(self, *, scroll: bool = False) -> None:
        raise NotImplementedError
