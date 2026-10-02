"""User-shaped host content and full Textual resize cells, using fixture backends."""

from dataclasses import replace
from datetime import datetime

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Static

from appmem.collect import LinuxBackend
from appmem.darwin_backend import DarwinApp, DarwinBackend
from appmem.model import AppStats, SystemStats
from appmem.ui.app import AppMemApp
from appmem.ui.darwin_header import render_host_header
from appmem.ui.header import render_header
from appmem.ui.host_grid import grid_row
from appmem.ui.host_panel import HostPanel
from appmem.ui.screens.darwin import DarwinMainScreen
from appmem.ui.screens.main import MainScreen
from appmem.ui.table import RowTable
from test_darwin_header import COLORS, GIB, HOST
from test_swap_activity import STATS

BOOT = int(41.6 * GIB)
SESSION = 12 * 1024**2
LINUX = replace(STATS, swap_out_bytes=BOOT, swap_free=int(12.6 * GIB))
MAC = replace(HOST, swap_out_bytes=BOOT)


def headers(
    platform: str,
    width: int,
    incoming: int | None = 1023,
    outgoing: int | None = 243 * 1024,
    ascii_bars: bool = False,
    *,
    session: int | None = SESSION,
) -> list[Text]:
    if platform == "mac":
        return list(
            render_host_header(
                MAC,
                width,
                colors=COLORS,
                swap_in_rate=incoming,
                swap_out_rate=outgoing,
                swap_out_session_total=session,
                ascii_bars=ascii_bars,
            )
        )
    return render_header(
        LINUX,
        width,
        30,
        colors=COLORS,
        baseline_time=datetime(2026, 1, 1),
        now=datetime(2026, 1, 1),
        swap_in_rate=incoming,
        swap_out_rate=outgoing,
        swap_out_session_total=session,
        ascii_bars=ascii_bars,
    )


@pytest.mark.parametrize("platform", ["linux", "mac"])
@pytest.mark.parametrize("width", [104, 105, 106, 109, 110, 120, 160])
@pytest.mark.parametrize("incoming", [1023, 1024])
def test_105_keeps_rates_session_total_and_native_primary(
    platform: str, width: int, incoming: int
) -> None:
    lines = headers(platform, width, incoming)
    swap = lines[2].plain
    assert "out 243 KiB/s" in swap
    assert ("in 1023 B/s" if incoming == 1023 else "in 1 KiB/s") in swap
    assert ("(12 MiB this run)" in swap) is (width >= 104)
    assert ("(41.6 GiB since boot)" in swap) is (width == 160)
    assert "used/alloc" in swap if platform == "mac" else "19.4/32.0 GiB used" in swap
    assert swap.endswith("…") is (width < 160)
    assert all(line.cell_len <= width for line in lines)
    if width >= 105:
        assert sum(c in "█░▏▎▍▌▋▊▉" for c in swap) == 20
    ram = lines[0].plain
    assert all(
        token in ram
        for token in (("file-backed", "free") if platform == "mac" else ("avail", "shared"))
    )
    anchors = [line.plain.index("│") for line in lines if "│" in line.plain]
    assert len(set(anchors)) == 1


@pytest.mark.parametrize("platform", ["linux", "mac"])
@pytest.mark.parametrize("rates", [(None, None), (0, 0), (10**30, 10**30)])
@pytest.mark.parametrize("width", [40, 60, 80, 105, 160])
def test_extreme_rates_do_not_detach_total_or_overflow(
    platform: str, rates: tuple[int | None, int | None], width: int
) -> None:
    lines = headers(platform, width, *rates)
    assert all(line.cell_len <= width for line in lines)
    swap = lines[2].plain
    if "this run" in swap or "since boot" in swap:
        assert "out " in swap and "in " in swap
    if "this run" in swap:
        assert "(12 MiB this run)" in swap
    if "since boot" in swap:
        assert "(41.6 GiB since boot)" in swap and "this run" in swap
    if rates == (None, None) and "in " in swap:
        assert "in —" in swap
    if rates == (0, 0) and "in " in swap:
        assert "in 0 B/s" in swap


@pytest.mark.parametrize("platform", ["linux", "mac"])
@pytest.mark.parametrize("ascii_bars", [False, True])
@pytest.mark.parametrize("width", [80, 105, 110, 160])
@pytest.mark.parametrize("session", [None, 0, 2**64 - 1])
def test_session_total_is_whole_and_precedes_boot(
    platform: str, ascii_bars: bool, width: int, session: int | None
) -> None:
    from appmem.total import total_amount

    swap = headers(platform, width, ascii_bars=ascii_bars, session=session)[2]
    session_text = f"({total_amount(session)} this run)"
    assert swap.cell_len <= width
    if "this run" in swap.plain:
        assert session_text in swap.plain
        assert "in " in swap.plain and "out " in swap.plain
    if "since boot" in swap.plain:
        assert "this run" in swap.plain and "(41.6 GiB since boot)" in swap.plain
    assert swap.plain.count("(") == swap.plain.count(")")


@pytest.mark.parametrize("platform", ["linux", "mac"])
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_hidden_rates_hide_both_whole_totals(platform: str, ascii_bars: bool) -> None:
    swap = headers(
        platform,
        160,
        incoming=10**100,
        outgoing=10**100,
        ascii_bars=ascii_bars,
        session=12 * 1024**2,
    )[2]
    assert swap.cell_len <= 160
    assert "in " not in swap.plain and "out " not in swap.plain
    assert "this run" not in swap.plain and "since boot" not in swap.plain


def test_marker_beside_content_and_absent_without_omissions() -> None:
    visible = grid_row("RAM", Text("1 GiB"), 80)
    assert not visible.plain.endswith("…")
    hidden = grid_row("RAM", Text("1 GiB"), 80, hidden=True)
    assert hidden.plain.endswith("1 GiB …")
    assert hidden.get_style_at_offset(Console(), len(hidden.plain) - 1).dim is not True


def check_visible_markers(owner: MainScreen | DarwinMainScreen, width: int, marker: str) -> bool:
    seen = False
    for index in range(1, 5):
        widget = owner.query_one(f"#header{index}", Static)
        if not widget.display:
            continue
        source = str(widget.content)
        rendered = widget.render_line(0).text.rstrip()
        assert widget.content_size.width == width
        if source.endswith(marker):
            seen = True
            assert rendered.endswith(marker)
            assert len(rendered[:-1]) - len(rendered[:-1].rstrip()) == 1
            segments = widget.render_line(0)._segments  # pyright: ignore[reportPrivateUsage]
            assert not segments[-1].style or segments[-1].style.dim is not True
    return seen


def fixture_screen(monkeypatch: pytest.MonkeyPatch, platform: str) -> MainScreen | DarwinMainScreen:
    if platform == "linux":

        def read(self: LinuxBackend) -> SystemStats:
            return LINUX

        monkeypatch.setattr(LinuxBackend, "read_system", read)
        apps = [AppStats(f"app-{i}", 1024**2, 0, 0, 1024**2, 1, ()) for i in range(40)]

        def collect(self: LinuxBackend, **kwargs: object) -> tuple[list[AppStats], dict[str, int]]:
            return apps, {}

        monkeypatch.setattr(LinuxBackend, "collect_apps", collect)
        owner = MainScreen(backend=object.__new__(LinuxBackend), interval=60, include_system=False)
    else:
        backend = object.__new__(DarwinBackend)
        monkeypatch.setattr(backend, "read_system", lambda: MAC)
        mac_apps: list[DarwinApp] = []
        monkeypatch.setattr(backend, "collect_apps", lambda: mac_apps)
        owner = DarwinMainScreen(backend, 60)
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["linux", "mac"])
@pytest.mark.parametrize("ascii_bars", [False, True])
async def test_full_render_resize_keeps_marker_near_content_and_panel_rows(
    monkeypatch: pytest.MonkeyPatch, platform: str, ascii_bars: bool
) -> None:
    owner = fixture_screen(monkeypatch, platform)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    marker = ">" if ascii_bars else "…"
    async with app.run_test(size=(160, 30)) as pilot:
        owner._ascii_bars = ascii_bars  # pyright: ignore[reportPrivateUsage]
        for width, height in (
            (160, 30),
            (120, 30),
            (110, 30),
            (105, 30),
            (80, 30),
            (60, 30),
            (40, 30),
            (60, 12),
            (105, 30),
        ):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            if platform == "linux":
                assert owner.query_one(RowTable).show_vertical_scrollbar
            seen = check_visible_markers(owner, width, marker)
            if width <= 80 or height < 18:
                assert seen
        if platform == "linux":
            await pilot.press("h")
            assert isinstance(app.screen, HostPanel)
            text = str(app.screen.query_one("#host-text", Static).content)
            assert "Zswap\n" in text
            for label in ("RAM occupied", "Pool limit", "Data held", "Compression"):
                assert any(line.strip().startswith(label) for line in text.splitlines())


@pytest.mark.asyncio
@pytest.mark.parametrize("allocation", [(10, 16), (100, 128)])
@pytest.mark.parametrize("ascii_bars", [False, True])
async def test_mac_multidigit_allocation_remains_whole_in_rendered_cells(
    monkeypatch: pytest.MonkeyPatch, allocation: tuple[int, int], ascii_bars: bool
) -> None:
    used, allocated = allocation
    host = replace(MAC, swap_used_bytes=used * GIB, swap_total_bytes=allocated * GIB)
    backend = object.__new__(DarwinBackend)
    monkeypatch.setattr(backend, "read_system", lambda: host)
    apps: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: apps)
    owner = DarwinMainScreen(backend, 60)
    app = AppMemApp(interval=60, main_screen_factory=lambda: owner)
    primary = f"{used:.1f}/{allocated:.1f} GiB used/alloc"
    async with app.run_test(size=(160, 30)) as pilot:
        owner._ascii_bars = ascii_bars  # pyright: ignore[reportPrivateUsage]
        owner._swap_in_rate = 1023  # pyright: ignore[reportPrivateUsage]
        owner._swap_out_rate = 243 * 1024  # pyright: ignore[reportPrivateUsage]
        for width in (160, 120, 110, 106, 105, 104, 80, 60, 40):
            await pilot.resize_terminal(width, 30)
            await pilot.pause()
            owner._render_header()  # pyright: ignore[reportPrivateUsage]
            await pilot.pause()
            swap = owner.query_one("#header3", Static).render_line(0)
            assert primary in swap.text
            assert swap.cell_length <= width
            separators = [
                owner.query_one(f"#header{index}", Static)
                .render_line(0)
                .text.index("|" if ascii_bars else "│")
                for index in range(1, 5)
                if ("|" if ascii_bars else "│")
                in owner.query_one(f"#header{index}", Static).render_line(0).text
            ]
            assert len(set(separators)) <= 1
            if width >= 105:
                glyphs = "#." if ascii_bars else "█░▏▎▍▌▋▊▉"
                assert sum(c in glyphs for c in swap.text[:30]) == 20
                assert "in 1023 B/s" in swap.text and "out 243 KiB/s" in swap.text
            if width == 160:
                assert "out 243 KiB/s" in swap.text
                assert "(0 B this run)" in swap.text
                assert "(41.6 GiB since boot)" in swap.text
