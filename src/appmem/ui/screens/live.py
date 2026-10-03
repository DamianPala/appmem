"""Threaded refresh lifecycle shared by Linux and Darwin live screens."""

from __future__ import annotations

import threading
from collections.abc import Callable

from rich.text import Text
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Static

from appmem.ui.host_panel import HostPanel


class LiveScreen(Screen[None]):
    """One in-flight read, generation invalidation, and immediate resume refresh."""

    def __init__(self) -> None:
        super().__init__()
        self._timer: Timer | None = None
        self._tick_in_flight = False
        self._generation = 0
        """Bumped by `_invalidate_tick` on every context change (screen covered or
        resumed, a mode switch). A background result carries the generation it was
        read under; one that no longer matches is discarded, so a slow read cannot
        overwrite what the change already drew."""
        self._host_panel_open = False

    def _set_static(self, selector: str, content: Text | str) -> None:
        """`Static.update` schedules a layout pass; skip it for unchanged content."""
        widget = self.query_one(selector, Static)
        if widget.content != content:
            widget.update(content)

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

    def refresh_theme(self) -> None:
        """Recolour what the theme colours; the main screens override, the rest have none."""

    def _resume(self) -> None:
        """What a screen's own `on_screen_resume` calls: Textual runs the handler of
        every class in the MRO, so the base defines none and cannot double-sample."""
        self._invalidate_tick()
        self.refresh_now()

    def refresh_now(self, *, scroll: bool = False) -> None:
        raise NotImplementedError
