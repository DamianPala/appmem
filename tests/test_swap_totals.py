"""Cumulative boot/session activity with synthetic platform samples only."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from textual.widgets import Static

from appmem.collect import LinuxBackend, read_system
from appmem.darwin_backend import DarwinApp, DarwinBackend
from appmem.darwin_native import HostMemory
from appmem.model import AppStats, SystemStats
from appmem.rate import Sample, update_rate
from appmem.total import SessionCounter, total_amount
from appmem.ui.app import AppMemApp
from appmem.ui.darwin_header import render_host_header
from appmem.ui.host_panel import darwin_details, linux_details, wrap_details
from appmem.ui.screens.darwin import DarwinMainScreen
from appmem.ui.screens.main import MainScreen
from test_darwin_header import HOST
from test_swap_activity import COLORS, GIB, STATS, header


def test_session_is_exact_difference_and_initial_zero_is_valid() -> None:
    counter = SessionCounter()
    counter.update(0)
    assert counter.total == 0
    counter.update(12345)
    assert counter.total == 12345
    counter.update(12345)
    assert counter.total == 12345


@pytest.mark.parametrize("value", [None, -1, True, False])
def test_missing_or_invalid_initial_counter_never_claims_complete_session(
    value: int | None,
) -> None:
    counter = SessionCounter()
    counter.update(value)
    counter.update(1000)
    counter.update(2000)
    assert counter.total is None


@pytest.mark.parametrize("value", [None, -1, True, False])
def test_temporary_missing_or_invalid_counter_recovers_original_baseline(value: int | None) -> None:
    counter = SessionCounter()
    counter.update(1000)
    counter.update(value)
    assert counter.total is None
    counter.update(2000)
    assert counter.total == 1000


def test_any_decrease_invalidates_session_even_above_initial_baseline() -> None:
    counter = SessionCounter()
    for value in (1000, 3000, None, 2000, 5000):
        counter.update(value)
    assert counter.total is None


@pytest.mark.parametrize(("now", "wall"), [(1000, 1000), (1, 100), (-1, -1)])
def test_long_pause_and_clock_discontinuities_reset_rates_only(now: float, wall: float) -> None:
    counter = SessionCounter()
    counter.update(0)
    counter.update(123456)
    history, rate = update_rate((Sample(0, 0, 0),), now, 123456, wall=wall)
    assert rate is None and len(history) == 1
    assert counter.total == 123456
    counter.update(234567)
    assert counter.total == 234567


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0 B"),
        (round(2.4 * 1024**4), "2.4 TiB"),
        (3 * 1024**5, "3.0 PiB"),
        (2**64 - 1, "16.0 EiB"),
    ],
)
def test_cumulative_units_handle_since_boot_sizes(value: int, text: str) -> None:
    assert total_amount(value) == text
    linux = header(replace(STATS, swap_out_bytes=value), width=160)[2]
    mac = render_host_header(replace(HOST, swap_out_bytes=value), 160, colors=COLORS)[2]
    assert f"({text} since boot)" in linux.plain
    assert f"({text} since boot)" in mac.plain
    assert linux.cell_len <= 160 and mac.cell_len <= 160


def test_zero_mac_allocation_does_not_erase_nonzero_native_activity() -> None:
    host = replace(
        HOST, swap_used_bytes=0, swap_total_bytes=0, swap_in_bytes=GIB, swap_out_bytes=2 * GIB
    )
    text = darwin_details(host, (0, 0), (0, 0))
    assert "0/0 B used / allocated now" in text
    assert "Since boot              1.0 GiB         2.0 GiB" in text
    assert "(2.0 GiB since boot)" in render_host_header(host, 160, colors=COLORS)[2].plain


@pytest.mark.parametrize(
    ("entry", "disk"),
    [
        ("/swapfile file 100 1 -2", True),
        ("/dev/sda2 partition 100 1 -2", True),
        ("/dev/nvme0n1p3 partition 100 0 -2", True),
        ("/dev/zram0 partition 100 1 100", False),
        ("/dev/zram0 partition 100 1 100\n/dev/sda2 partition 100 1 -2", False),
        ("/dev/mapper/swap partition 100 1 -2", False),
        ("/dev/disk/by-uuid/unknown partition 100 1 -2", False),
        ("/swapfile file bad 1 -2", False),
        ("/swapfile file ² 1 -2", False),
        ("/swapfile file 100 1 --2", False),
        (f"/swapfile file {'1' * 4301} 1 -2", False),
        ("/swapfile file 100 101 -2", False),
        ("/swapfile file 0 0 -2", False),
        ("/swapfile other 100 1 -2", False),
        ("/swapfile file 100 1 -2 extra", False),
        ("", False),
    ],
)
def test_current_topology_fails_closed_for_zram_mixed_and_unknown(
    tmp_path: Path, entry: str, disk: bool
) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/swaps").write_text("Filename Type Size Used Priority\n" + entry + "\n")
    stats = read_system(tmp_path, 1000, page_size=4096)
    assert stats.swap_disk_only is disk
    text = linux_details(stats, (0, 0))
    assert ("Writes count data sent to disk for swap." in text) is disk
    assert ("swap devices" in text) is not disk


@pytest.mark.parametrize("content", [None, "", "bad header\n/swapfile file 100 1 -2\n"])
def test_missing_empty_or_malformed_topology_cannot_assert_disk(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "proc").mkdir()
        (tmp_path / "proc/swaps").write_text(content)
    assert read_system(tmp_path, 1000, page_size=4096).swap_disk_only is False


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("width", [40, 60, 80, 120, 160, 200])
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_session_and_boot_totals_are_secondary_and_no_loss_or_overflow(
    platform: str, width: int, ascii_bars: bool
) -> None:
    if platform == "linux":
        # Existing wrapper also supplies measured rates.
        if not ascii_bars:
            lines = header(replace(STATS, swap_out_bytes=round(41.6 * GIB)), width=width)
        else:
            from datetime import datetime

            from appmem.ui.header import render_header

            lines = render_header(
                replace(STATS, swap_out_bytes=round(41.6 * GIB)),
                width,
                30,
                colors=COLORS,
                baseline_time=datetime(2026, 1, 1),
                now=datetime(2026, 1, 1),
                ascii_bars=True,
                swap_in_rate=0,
                swap_out_rate=1024,
            )
    else:
        lines = render_host_header(
            replace(HOST, swap_out_bytes=round(41.6 * GIB)),
            width,
            colors=COLORS,
            ascii_bars=ascii_bars,
            swap_in_rate=0,
            swap_out_rate=1024,
        )
    swap = next(line for line in lines if line.plain.startswith("Swap"))
    assert all(line.cell_len <= width for line in lines)
    assert len(lines) == 4
    assert ("(unavailable this run)" in swap.plain) is (width >= 105)
    assert ("(41.6 GiB since boot)" in swap.plain) is (width >= 160)
    if width == 80:
        assert swap.plain.endswith(">" if ascii_bars else "…")
        assert "in 0 B/s" in swap.plain
        if platform == "linux":
            assert "out 1 KiB/s" in swap.plain
        else:
            assert "used/alloc" in swap.plain
    if width >= 160:
        assert "in 0 B/s" in swap.plain and "out 1 KiB/s" in swap.plain
        assert "(unavailable this run)" in swap.plain


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("width", [20, 40, 60, 80, 160])
def test_activity_table_labels_and_values_survive_wrapping(platform: str, width: int) -> None:
    host = replace(
        STATS if platform == "linux" else HOST,
        swap_in_bytes=3 * GIB,
        swap_out_bytes=round(41.6 * GIB),
    )
    text = (
        linux_details(cast(SystemStats, host), (0, 1024), (1024, 2048))
        if platform == "linux"
        else darwin_details(cast(HostMemory, host), (0, 1024), (1024, 2048))
    )
    rendered = wrap_details(text, width)
    assert all(len(line) <= width for line in rendered.splitlines())
    words = " ".join(rendered.split())
    for label in (
        "Activity",
        "Read",
        "Written",
        "Current rate",
        "Since boot",
        "Since AppMem started",
        "41.6 GiB",
        "1 KiB",
        "2 KiB",
    ):
        assert label in words
    if width >= 80:
        assert "Since boot              3.0 GiB         41.6 GiB" in rendered
    if platform == "darwin":
        assert "allocated now" in words
        assert "Zswap" not in words


def test_linux_approved_copy_and_boot_counter_is_not_adjusted_by_writeback() -> None:
    text = linux_details(
        replace(STATS, swap_disk_only=True, swap_out_bytes=3 * GIB, zswap_writeback_bytes=GIB),
        (0, 0),
        (0, 0),
    )
    assert "Writes count only data sent to disk, directly or from zswap." in text
    assert "Swap used includes the data held in zswap, even without writing them to disk." in text
    assert "Since boot              unavailable     3.0 GiB" in text
    assert "Current swap targets" not in text


@pytest.mark.parametrize("zswap_enabled", [False, True])
def test_generic_topology_copy_only_explains_zswap_when_enabled(zswap_enabled: bool) -> None:
    text = linux_details(replace(STATS, swap_disk_only=False, zswap_enabled=zswap_enabled), (0, 0))
    assert "Writes count data sent to swap devices." in text
    assert ("Data kept only in zswap is not counted." in text) is zswap_enabled
    assert "RAM-backed zram" not in text and "zswap hits" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_session_survives_baseline_navigation_pause_clock_shift_and_independent_reset(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    initial = replace(STATS if platform == "linux" else HOST, swap_in_bytes=0, swap_out_bytes=1000)
    readings = [initial]
    if platform == "linux":
        backend = object.__new__(LinuxBackend)

        def read(self: LinuxBackend) -> SystemStats:
            return cast(SystemStats, readings[0])

        monkeypatch.setattr(LinuxBackend, "read_system", read)
        apps: list[AppStats] = []

        def collect(self: LinuxBackend, **kwargs: object) -> tuple[list[AppStats], dict[str, int]]:
            return apps, {}

        monkeypatch.setattr(LinuxBackend, "collect_apps", collect)
        owner = MainScreen(backend=backend, interval=3600, include_system=False)
    else:
        backend = object.__new__(DarwinBackend)
        monkeypatch.setattr(backend, "read_system", lambda: cast(HostMemory, readings[0]))
        mac_apps: list[DarwinApp] = []
        monkeypatch.setattr(backend, "collect_apps", lambda: mac_apps)
        owner = DarwinMainScreen(backend, 3600)
    app = AppMemApp(interval=3600, main_screen_factory=lambda: owner)
    async with app.run_test(size=(160, 24)) as pilot:
        await pilot.pause()
        assert owner._swap_in_session.total == 0  # pyright: ignore[reportPrivateUsage]
        assert owner._swap_out_session.total == 0  # pyright: ignore[reportPrivateUsage]
        first_header = owner.query_one("#header3", Static).content
        assert "(0 B this run)" in str(first_header)
        readings[0] = replace(initial, swap_in_bytes=12345, swap_out_bytes=123456)
        await pilot.press("b", "?", "escape", "T", "escape")
        await pilot.resize_terminal(80, 24)
        owner.refresh_now()
        assert owner._swap_in_session.total == 12345  # pyright: ignore[reportPrivateUsage]
        assert owner._swap_out_session.total == 122456  # pyright: ignore[reportPrivateUsage]
        await pilot.resize_terminal(160, 24)
        await pilot.pause()
        header = str(owner.query_one("#header3", Static).content)
        assert "(120 KiB this run)" in header
        content = owner._host_content()  # pyright: ignore[reportPrivateUsage]
        assert "Since AppMem started" in content and "120 KiB" in content
        await pilot.press("h")
        panel = str(app.screen.query_one("#host-text", Static).content)
        assert "Since AppMem started" in panel and "120 KiB" in panel
        await pilot.press("escape")
        # Reset the rate window as a long pause or clock discontinuity would.
        owner._swap_in_history = ()  # pyright: ignore[reportPrivateUsage]
        owner._swap_out_history = ()  # pyright: ignore[reportPrivateUsage]
        readings[0] = replace(initial, swap_in_bytes=None, swap_out_bytes=130000)
        owner.refresh_now()
        assert owner._swap_in_session.total is None  # pyright: ignore[reportPrivateUsage]
        readings[0] = replace(initial, swap_in_bytes=20000, swap_out_bytes=120000)
        owner.refresh_now()
        assert owner._swap_in_session.total == 20000  # pyright: ignore[reportPrivateUsage]
        assert owner._swap_out_session.total is None  # pyright: ignore[reportPrivateUsage]
        assert "117 KiB" in owner._host_content()  # pyright: ignore[reportPrivateUsage]
