"""Host overlay and grid regressions using synthetic samples."""

import asyncio
import threading
from dataclasses import replace

import pytest
from textual import events
from textual.containers import VerticalScroll
from textual.widgets import Static

from appmem.collect import LinuxBackend
from appmem.darwin_backend import DarwinApp, DarwinBackend
from appmem.darwin_native import HostMemory
from appmem.model import AppStats, SystemStats
from appmem.ui.app import AppMemApp
from appmem.ui.host_panel import HostPanel, darwin_details, linux_details
from appmem.ui.screens.darwin import DarwinMainScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.table import RowTable
from test_darwin_header import HOST
from test_swap_activity import STATS
from test_ui_header import _ZSWAP_STATS, _render  # pyright: ignore[reportPrivateUsage]


def test_enabled_zswap_remains_visible_below_eighty_columns() -> None:
    lines = _render(_ZSWAP_STATS, 60, 24)
    assert any(line.plain.startswith("Zswap") for line in lines)
    assert any(line.plain.endswith("…") for line in lines)


def test_panel_linux_accounting_and_unknowns_are_current_sample() -> None:
    text = linux_details(STATS, (1023, 243 * 1024))
    assert "Data held                  4.0 GiB" in text
    assert "Compression                4.0:1" in text
    assert "Swap used includes the data held in zswap" in text
    assert "RAM used includes the compressed size: 1.0 GiB" in text
    assert "Pool limit                 ~6.0 GiB · 20% of RAM" in text
    assert "Current rate            1023 B/s        243 KiB/s" in text
    assert "18.8" not in text and "19.4" not in text
    assert "unavailable" in linux_details(replace(STATS, zswap_pool_bytes=None), (None, None))
    assert "disabled or unavailable" in linux_details(replace(STATS, zswap_enabled=False), (0, 0))
    assert "(0.0:1)" not in linux_details(replace(STATS, zswap_pool_bytes=0), (0, 0))


def test_panel_mac_accounting_remains_native() -> None:
    text = darwin_details(HOST, (0, None))
    assert "3.0 GiB of data compressed into 1.0 GiB of RAM (3.0:1)" in text
    assert "RAM used includes the physical compressed size: 1.0 GiB" in text
    assert "allocated now" in text and "dynamically" in text
    assert "native macOS" in text and "this user" in text
    assert not any(
        word in text for word in ("Zswap", "Elsewhere", "RAM limit", "Swap used includes")
    )
    assert "Current rate            0 B/s           unavailable" in text


def test_panel_pressure_keeps_available_fields_with_partial_readings() -> None:
    stats = replace(STATS, pressure_some_avg10=None, pressure_full_avg60=2.5)
    text = linux_details(stats, (None, None))
    assert "Pressure  unavailable" in text
    assert "Time waiting for memory, last 10 seconds / 60 seconds:" in text
    assert f"At least one task: unavailable / {stats.pressure_some_avg60:.1f}%." in text
    assert f"All active tasks: {stats.pressure_full_avg10:.1f}% / 2.5%." in text


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [40, 60, 80, 120, 160])
async def test_overlay_live_scroll_resize_focus_and_close(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    backend = object.__new__(DarwinBackend)
    readings = [HOST]
    calls: list[None] = []

    def read() -> HostMemory:
        calls.append(None)
        return readings[0]

    monkeypatch.setattr(backend, "read_system", read)
    apps: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: apps)
    owner = DarwinMainScreen(backend, 60)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    async with app.run_test(size=(width, 10)) as pilot:
        await pilot.pause()
        table = owner.query_one(RowTable)
        table_state = (table.cursor_row, table.scroll_y)
        before = len(calls)
        footer = owner.query_one("#footer", Static).content
        assert "host" in str(footer)
        await pilot.press("h")
        await pilot.pause()
        panel = app.screen
        assert isinstance(panel, HostPanel)
        scroll = panel.query_one(VerticalScroll)
        assert app.focused is scroll
        await pilot.press("end")
        await pilot.pause()
        assert scroll.scroll_y > 0
        y = scroll.scroll_y
        readings[0] = replace(HOST, compressor_logical_bytes=4 * 1024**3)
        owner._tick()  # pyright: ignore[reportPrivateUsage] - exercise existing worker
        await pilot.pause()
        assert "4.0 GiB of data" in str(panel.query_one("#host-text", Static).content)
        assert scroll.scroll_y == y
        scroll.post_message(events.MouseScrollUp(scroll, 5, 5, 0, 0, 0, False, False, False))
        await pilot.pause()
        assert scroll.scroll_y < y
        await pilot.resize_terminal(80 if width != 80 else 60, 16)
        await pilot.pause()
        assert app.screen is panel and app.focused is scroll
        count = len(calls)
        assert count == before + 1
        await pilot.press("h")
        await pilot.pause()
        assert app.screen is owner and app.focused is table
        assert len(calls) == count
        assert (table.cursor_row, table.scroll_y) == table_state
        await pilot.press("h", "escape")
        assert app.screen is owner
        await pilot.press("?")
        help_screen = app.screen
        await pilot.press("h")
        assert app.screen is help_screen
        await pilot.press("escape", "T")
        theme_screen = app.screen
        await pilot.press("h")
        assert app.screen is theme_screen
        await pilot.press("escape")
        await pilot.press("h", "?")
        nested_help = app.screen
        assert not isinstance(nested_help, HostPanel)
        await pilot.press("h")
        assert app.screen is nested_help
        await pilot.press("escape")
        assert isinstance(app.screen, HostPanel)
        await pilot.press("T")
        nested_theme = app.screen
        assert not isinstance(nested_theme, HostPanel)
        await pilot.press("h")
        assert app.screen is nested_theme
        await pilot.press("escape")
        assert isinstance(app.screen, HostPanel)
        await pilot.press("h")
        assert app.screen is owner


@pytest.mark.asyncio
async def test_failed_read_marks_retained_panel_stale_and_recovery_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = object.__new__(DarwinBackend)
    fail = [False]

    def read() -> HostMemory:
        if fail[0]:
            raise OSError("fixture read failure")
        return HOST

    monkeypatch.setattr(backend, "read_system", read)
    apps: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: apps)
    owner = DarwinMainScreen(backend, 60)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("h")
        panel = app.screen
        fail[0] = True
        owner._tick()  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        text = str(panel.query_one("#host-text", Static).content)
        assert "Read failed; showing last successful reading." in text
        assert "3.0 GiB" in text
        fail[0] = False
        owner._tick()  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert "Read failed" not in str(panel.query_one("#host-text", Static).content)


@pytest.mark.asyncio
async def test_linux_overlay_retains_wheel_viewport_and_accepts_worker_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = object.__new__(LinuxBackend)
    readings = [STATS]
    calls: list[None] = []
    blocked = threading.Event()
    release = threading.Event()
    delay = [False]

    def read(self: LinuxBackend) -> SystemStats:
        calls.append(None)
        if delay[0]:
            blocked.set()
            assert release.wait(5)
        return readings[0]

    monkeypatch.setattr(LinuxBackend, "read_system", read)
    apps = [
        AppStats(f"app-{index}", 1024**2 * (50 - index), 0, 0, 1024**2 * (50 - index), 1, ())
        for index in range(40)
    ]

    def collect(self: LinuxBackend, **kwargs: object) -> tuple[list[AppStats], dict[str, int]]:
        return apps, {}

    monkeypatch.setattr(LinuxBackend, "collect_apps", collect)
    owner = MainScreen(backend=backend, interval=60, include_system=False)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        table = owner.query_one(RowTable)
        table.move_cursor(row=2, scroll=False)
        table.post_message(events.MouseScrollDown(table, 10, 10, 0, 0, 0, False, False, False))
        await pilot.pause()
        original = (table.cursor_row, table.scroll_y)
        assert original[1] > 0
        await pilot.press("h")
        await pilot.pause()
        delay[0] = True
        readings[0] = replace(STATS, zswapped_bytes=5 * 1024**3)
        count = len(calls)
        owner._tick()  # pyright: ignore[reportPrivateUsage]
        assert await asyncio.to_thread(blocked.wait, 3)
        await pilot.press("escape")
        assert len(calls) == count + 1
        release.set()
        await pilot.pause()
        assert owner._last_stats == readings[0]  # pyright: ignore[reportPrivateUsage]
        assert (table.cursor_row, table.scroll_y) == original
        assert app.focused is table
        await pilot.press("h")
        await pilot.pause()
        assert "Data held                  5.0 GiB" in str(
            app.screen.query_one("#host-text", Static).content
        )
        delay[0] = False

        def failed_read(self: LinuxBackend) -> SystemStats:
            raise OSError("failed")

        monkeypatch.setattr(LinuxBackend, "read_system", failed_read)
        owner._tick()  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert "last successful reading" in str(app.screen.query_one("#host-text", Static).content)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["q", "ctrl+c"])
async def test_quit_remains_available_from_host_panel(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    backend = object.__new__(DarwinBackend)
    monkeypatch.setattr(backend, "read_system", lambda: HOST)
    empty: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: empty)
    owner = DarwinMainScreen(backend, 60)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("h")
        assert isinstance(app.screen, HostPanel)
        await pilot.press(key)
        assert not app.is_running


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["end", "home"])
async def test_sample_during_keyboard_animation_preserves_scroll_intent(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    backend = object.__new__(DarwinBackend)
    monkeypatch.setattr(backend, "read_system", lambda: HOST)
    apps: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: apps)
    owner = DarwinMainScreen(backend, 60)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    async with app.run_test(size=(60, 16)) as pilot:
        await pilot.press("h")
        scroll = app.screen.query_one(VerticalScroll)
        assert scroll.max_scroll_y > 0
        if key == "home":
            await pilot.press("end")
        driver = app._driver  # pyright: ignore[reportPrivateUsage] - raw input preserves animation
        assert driver is not None
        # Pilot.press waits for animations, hiding a sample that interrupts a real key press.
        driver.send_message(events.Key(key, None))
        await asyncio.sleep(0.15)
        assert 0 < scroll.scroll_y < scroll.max_scroll_y
        owner._tick()  # pyright: ignore[reportPrivateUsage] - real worker/sample path
        await asyncio.sleep(1.2)
        assert scroll.scroll_y == (scroll.max_scroll_y if key == "end" else 0)
