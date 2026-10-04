"""The host header grid: fixed positions, one layout decision per width, both platforms."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest
from rich.text import Text
from textual.widgets import Static

from appmem.darwin_backend import DarwinApp, DarwinBackend
from appmem.darwin_native import HostMemory
from appmem.model import SystemStats
from appmem.ui.app import AppMemApp
from appmem.ui.darwin_header import _slots as mac_slots  # pyright: ignore[reportPrivateUsage]
from appmem.ui.darwin_header import render_host_header
from appmem.ui.header import ThemeColors, render_header
from appmem.ui.header import _slots as linux_slots  # pyright: ignore[reportPrivateUsage]
from appmem.ui.host_grid import Item, Layout, Slots, ValueFields, grid_row, pair_width, spaces
from appmem.ui.host_grid import _make as make_layout  # pyright: ignore[reportPrivateUsage]
from appmem.ui.host_panel import HostPanel
from appmem.ui.screens.darwin import DarwinMainScreen, DarwinProcessesScreen
from appmem.ui.table import RowTable
from test_darwin_backend import Reader
from test_darwin_header import HOST
from test_swap_activity import STATS

GIB = 1024**3
MIB = 1024**2
COLORS = ThemeColors("green", "yellow", "red", "blue")
NOW = datetime(2026, 1, 1, 16, 10)
BASELINE = datetime(2026, 1, 1, 15, 58)
WIDTHS = (40, 60, 80, 100, 105, 115, 120, 135, 160, 200)
FULL_STATS = replace(
    STATS,
    mem_free=2 * GIB,
    mem_cache=6 * GIB,
    mem_slab=2 * GIB,
    mem_shared=3 * GIB,
    system_ram=700 * MIB,
    elsewhere=276 * MIB,
    swap_out_bytes=2 * 1024**4,
)
# The owner's kind of machine: 30.9 GiB RAM, 32.0 GiB swap, written totals under 100 GiB.
TYPICAL = replace(
    FULL_STATS,
    mem_total=int(30.9 * GIB),
    mem_available=int(10.5 * GIB),
    swap_total=32 * GIB,
    swap_free=int(8.7 * GIB),
    swap_out_bytes=int(2.4 * 1024**4),
)


def linux(
    stats: SystemStats = FULL_STATS,
    width: int = 160,
    height: int = 30,
    *,
    ascii_bars: bool = False,
    rates: tuple[int | None, int | None] = (4096, 0),
    session: int | None = GIB,
    writeback: int | None = 0,
) -> list[Text]:
    return render_header(
        stats,
        width,
        height,
        colors=COLORS,
        baseline_time=BASELINE,
        now=NOW,
        ascii_bars=ascii_bars,
        swap_in_rate=rates[0],
        swap_out_rate=rates[1],
        swap_out_session_total=session,
        writeback_rate=writeback,
    )


def mac(
    host: HostMemory = HOST,
    width: int = 160,
    *,
    ascii_bars: bool = False,
    rates: tuple[int | None, int | None] = (4096, 0),
    session: int | None = GIB,
) -> list[Text]:
    return list(
        render_host_header(
            host,
            width,
            colors=COLORS,
            ascii_bars=ascii_bars,
            baseline_time="15:58",
            baseline_elapsed=720,
            swap_in_rate=rates[0],
            swap_out_rate=rates[1],
            swap_out_session_total=session,
        )
    )


Render = Callable[..., list[Text]]
PLATFORMS: list[tuple[str, Render]] = [("linux", linux), ("darwin", mac)]


def style_at(text: Text, substring: str) -> str:
    start = text.plain.index(substring)
    styles = [
        str(span.style)
        for span in text.spans
        if span.start <= start and span.end >= start + len(substring)
    ]
    assert styles, f"no style covers {substring!r} in {text.plain!r}"
    return styles[-1]


@pytest.mark.parametrize(("name", "render"), PLATFORMS)
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_every_row_fits_and_never_wraps(
    name: str, render: Render, width: int, ascii_bars: bool
) -> None:
    lines = render(width=width, ascii_bars=ascii_bars)
    assert len(lines) == 4
    assert all(line.cell_len <= width and line.no_wrap for line in lines)
    if ascii_bars:
        assert all(ord(char) < 128 for line in lines for char in line.plain)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_short_terminal_has_two_rows_that_fit(width: int, ascii_bars: bool) -> None:
    lines = linux(width=width, height=16, ascii_bars=ascii_bars)
    assert len(lines) == 2
    assert all(line.cell_len <= width and line.no_wrap for line in lines)


# --- positions come from the width, never from the values ----------------------

LOW = replace(
    FULL_STATS,
    mem_available=29 * GIB,
    mem_shared=0,
    mem_free=0,
    swap_free=FULL_STATS.swap_total,
    swap_out_bytes=0,
    system_ram=0,
    zswapped_bytes=0,
    zswap_pool_bytes=0,
)
HIGH = replace(
    FULL_STATS,
    mem_available=GIB,
    mem_shared=29 * GIB,
    mem_free=29 * GIB,
    swap_free=0,
    swap_out_bytes=2**50,
    system_ram=900 * MIB,
    zswapped_bytes=500 * GIB,
    zswap_pool_bytes=6 * GIB,
)
LABELS = re.compile(
    r"\b(avail|shared|holds|ratio|in|out|written|free|limit|above|writeback|system|since|elsewhere)\b"
)


def label_columns(line: Text) -> dict[str, int]:
    return {match.group(1): match.start() for match in LABELS.finditer(line.plain)}


@pytest.mark.parametrize("width", [100, 120, 135, 160, 200])
def test_linux_columns_do_not_move_with_the_values(width: int) -> None:
    low = linux(LOW, width, rates=(0, 0), session=0, writeback=0)
    high = linux(HIGH, width, rates=(1023 * 1024, 1023 * 1024), session=2**40, writeback=900)
    for before, after in zip(low, high, strict=True):
        first, second = label_columns(before), label_columns(after)
        shared = first.keys() & second.keys()
        assert shared
        assert all(first[key] == second[key] for key in shared)
        assert ("│" in before.plain) is ("│" in after.plain)
        if "│" in before.plain:
            assert before.plain.index("│") == after.plain.index("│")


@pytest.mark.parametrize("width", [100, 120, 135, 160, 200])
def test_darwin_columns_do_not_move_with_the_values(width: int) -> None:
    high_host = replace(
        HOST,
        swap_used_bytes=GIB,
        free_bytes=7 * GIB,
        compressor_logical_bytes=500 * GIB,
        swap_out_bytes=2**50,
    )
    low = mac(HOST, width, rates=(0, 0), session=0)
    high = mac(high_host, width, rates=(1023 * 1024, 1023 * 1024), session=2**40)
    for before, after in zip(low, high, strict=True):
        first, second = label_columns(before), label_columns(after)
        assert all(first[key] == second[key] for key in first.keys() & second.keys())
        if "│" in before.plain:
            assert before.plain.index("│") == after.plain.index("│")


@pytest.mark.parametrize(("name", "render"), PLATFORMS)
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_zero_is_shown_and_unknown_is_a_dash_in_the_same_place(
    name: str, render: Render, ascii_bars: bool
) -> None:
    dash = "?" if ascii_bars else "—"
    known = render(width=200, ascii_bars=ascii_bars, rates=(0, 0), session=0)[2]
    unknown = render(width=200, ascii_bars=ascii_bars, rates=(None, None), session=None)[2]
    assert re.search(r"in\s+0 B/s", known.plain) and re.search(r"out\s+0 B/s", known.plain)
    dash = re.escape(dash)
    assert re.search(rf"in\s+{dash}(\s|$)", unknown.plain)
    assert re.search(rf"out\s+{dash}(\s|$)", unknown.plain)
    assert re.search(rf"written this run\s+{dash}(\s|$)", unknown.plain)
    assert known.cell_len == unknown.cell_len
    assert label_columns(known) == label_columns(unknown)
    assert known.plain.index("│" if not ascii_bars else "|") == unknown.plain.index(
        "│" if not ascii_bars else "|"
    )


def test_fields_stay_when_the_values_are_zero() -> None:
    lines = linux(
        replace(FULL_STATS, mem_shared=0, swap_free=FULL_STATS.swap_total, elsewhere=0),
        200,
        rates=(0, 0),
        session=0,
        writeback=0,
    )
    plain = [line.plain for line in lines]
    assert "shared" in plain[0] and "holds" in plain[1] and "writeback" in plain[1]
    assert re.search(r"written this run\s+0 B", plain[2])


# --- one layout form per width ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "render", "wide_from"), [("linux", linux, 139), ("darwin", mac, 136)]
)
def test_wide_words_start_where_the_wide_rows_are_complete(
    name: str, render: Render, wide_from: int
) -> None:
    for width in range(100, 201):
        known = render(width=width, rates=(0, 0), session=0)[2].plain
        unknown = render(width=width, rates=(None, None), session=None)[2].plain
        assert ("this run" in known) is ("this run" in unknown) is (width >= wide_from), width
        if width >= wide_from:
            assert "since boot" in known and not known.endswith("…")


def is_complete(lines: list[Text]) -> bool:
    return not any(line.plain.endswith(("…", ">")) for line in lines)


@pytest.mark.parametrize("width", [116, 117, 120, 124, 139, 200])
def test_every_row_is_complete_from_116_columns_with_both_written_totals(width: int) -> None:
    rows = linux(TYPICAL, width, session=int(1.9 * GIB))
    assert is_complete(rows)
    swap = rows[2].plain
    assert re.search(r"written (this )?run\s+1\.9 GiB", swap)
    assert re.search(r"(since )?boot\s+2\.4 TiB", swap)


def test_the_boot_total_is_the_first_to_go_below_116_columns() -> None:
    swap = linux(TYPICAL, 115, session=int(1.9 * GIB))[2].plain
    assert re.search(r"written run\s+1\.9 GiB …$", swap)
    assert "boot" not in swap


def test_written_totals_are_label_first_pairs_with_aligned_numbers() -> None:
    short = linux(TYPICAL, 120, session=0)[2].plain
    assert re.search(r"written run\s+0 B · boot\s+2\.4 TiB$", short)
    wide = linux(TYPICAL, 160, session=int(1.9 * GIB))[2].plain
    assert re.search(r"written this run\s+1\.9 GiB · since boot\s+2\.4 TiB$", wide)
    big = linux(TYPICAL, 160, session=512 * GIB)[2].plain  # 100 GiB and more: no decimal
    assert re.search(r"written this run\s+512 GiB · since boot", big)


def test_numbers_end_at_the_end_of_their_field() -> None:
    quiet = linux(rates=(0, 0))[2].plain
    busy = linux(rates=(12 * MIB, 1023 * 1024))[2].plain
    assert quiet.index("B/s") == busy.index("B/s")
    low = linux(replace(FULL_STATS, mem_available=2 * GIB, mem_shared=0))[0].plain
    high = linux(replace(FULL_STATS, mem_available=12 * GIB, mem_shared=3 * GIB))[0].plain
    for label in ("avail", "shared"):
        ends = [re.search(rf"{label}\s+\S+ \S+", row) for row in (low, high)]
        assert ends[0] and ends[1] and ends[0].end() == ends[1].end(), label


def test_an_item_without_room_for_the_marker_is_dropped_whole() -> None:
    lay = Layout(27, 9, 0, 2, False, Slots(5, 6, 4, 8, 0), 4)
    items = [Item(Text("x" * 10), 10, spaces(2)), Item(Text("y" * 10), 10, Text())]
    row = grid_row("RAM", Text("abcd"), lay, items).plain
    assert row.endswith("…") and "x" not in row and len(row) <= 27


def test_the_marker_follows_the_last_visible_item_not_its_padding() -> None:
    lay = Layout(40, 9, 0, 2, False, Slots(5, 6, 4, 8, 0), 4)
    items = [Item(Text("x" * 4), 10, spaces(2)), Item(Text("y" * 4), 10, Text())]
    row = grid_row("RAM", Text("abcd"), lay, items, hidden=True).plain
    assert row.endswith("yyyy …"), row
    for width in range(60, 201):
        lines = [*linux(TYPICAL, width, 16), *linux(TYPICAL, width, 30), *mac(HOST, width)]
        for line in lines:
            if "│" in line.plain and line.plain.endswith("…"):
                assert re.search(r"\S ?…$", line.plain), (width, line.plain)


def test_slots_follow_the_totals_and_recompute_when_a_total_changes() -> None:
    small = linux(TYPICAL, 160)
    # `30.9/30.9 GiB used` needs no padding on this machine.
    assert re.search(r"[█░] \d+\.\d/30\.9 GiB used │", small[0].plain)
    bigger = replace(TYPICAL, mem_total=128 * GIB, mem_available=100 * GIB)
    large = linux(bigger, 160)
    # `128.0` is one digit wider than `30.9`, twice (used and total).
    assert large[0].plain.index("│") - small[0].plain.index("│") == 2
    assert len({line.plain.index("│") for line in large if "│" in line.plain}) == 1


def gauge_cells(line: Text) -> int:
    return sum(char in "█▏▎▍▌▋▊▉░" for char in line.plain.split("│")[0])


def test_gauge_takes_the_largest_size_that_leaves_every_row_complete() -> None:
    cells = {width: gauge_cells(linux(TYPICAL, width)[0]) for width in range(105, 201)}
    assert {cells[width] for width in range(105, 116)} == {12}
    assert {cells[width] for width in range(116, 120)} == {12}
    assert {cells[width] for width in range(120, 124)} == {16}
    assert {cells[width] for width in range(124, 201)} == {20}
    assert gauge_cells(linux(TYPICAL, 100)[0]) == 10


def assert_yields_from_the_right(line: Text, order: tuple[str, ...], width: int) -> None:
    """The fields present are a prefix of `order`; the marker shows iff one is missing."""
    present = [bool(re.search(rf"\b{word}\b", line.plain)) for word in order]
    assert present == sorted(present, reverse=True), (width, line.plain)
    assert line.plain.endswith("…") is not all(present), (width, line.plain)


def test_linux_column_3_yields_from_the_right_and_the_marker_says_so() -> None:
    for width in range(100, 201):
        ram, _zswap, swap, _pressure = linux(width=width)
        assert_yields_from_the_right(ram, ("avail", "shared", "free", "cache", "slab"), width)
        assert_yields_from_the_right(swap, ("in", "out", "written", "boot"), width)


def test_darwin_column_3_yields_from_the_right_and_the_marker_says_so() -> None:
    for width in range(100, 201):
        ram, _compress, swap, _pressure = mac(width=width)
        assert_yields_from_the_right(ram, ("wired", "free", "file-backed", "purgeable"), width)
        assert_yields_from_the_right(swap, ("in", "out", "written", "boot"), width)


# --- the pressure word is never cut (F1) -----------------------------------------

PRESSURES = {
    "none": {},
    "some": {"pressure_some_avg10": 12.0},
    "high": {"pressure_some_avg10": 30.0},
    "unavailable": {"pressure_some_avg10": None},
}


@pytest.mark.parametrize("state", PRESSURES)
@pytest.mark.parametrize("height", [30, 16])
def test_linux_pressure_word_is_whole_at_every_width(state: str, height: int) -> None:
    for width in range(40, 201):
        lines = linux(replace(FULL_STATS, **PRESSURES[state]), width, height)
        row = next(line.plain for line in lines if "Pressure" in line.plain)
        assert re.search(rf"Pressure\s+{state}(?!\w)", row), (width, row)


@pytest.mark.parametrize(
    ("level", "word"), [(1, "normal"), (2, "warning"), (4, "critical"), (None, "unavailable")]
)
def test_darwin_pressure_word_is_whole_at_every_width(level: int | None, word: str) -> None:
    for width in range(40, 201):
        lines = mac(replace(HOST, pressure_level=level), width)
        assert re.search(rf"Pressure\s+{word}(?!\w)", lines[3].plain), (width, lines[3].plain)


# --- a short terminal shows at least what 0.2.0 showed -----------------------------


@pytest.mark.parametrize("width", [60, 80, 100, 105, 115, 120, 135, 160, 200])
def test_two_row_header_keeps_the_0_2_0_fields(width: int) -> None:
    ram, swap = (line.plain for line in linux(width=width, height=16))
    assert re.search(r"Pressure\s+none", ram + swap)
    assert "system" in swap
    if width >= 70:
        assert "20.0/30.0 GiB used" in ram
        assert "8.0/32.0 GiB used" in swap
    if 80 <= width < 100 or width >= 115:  # 0.2.0 showed it at 80 to 99 and from 115
        assert "zswapped" in swap
    if width >= 100:
        assert "Δ since" in swap and "shared" in ram
    if width >= 115:
        assert "avail" in ram


def test_swap_pair_survives_on_the_narrowest_short_terminal() -> None:
    first = linux(width=40, height=16)[0].plain
    assert re.search(r"RAM 20\.0/30\.0 GiB\s+Swap\s+8\.0/32\.0 GiB", first)


def test_pressure_row_keeps_the_system_amount_from_60_columns() -> None:
    for width in (60, 70, 79, 80):
        assert "system" in linux(width=width)[3].plain, width


def test_swap_off_and_unknown_pressure_are_not_usage_and_move_nothing() -> None:
    off = replace(FULL_STATS, swap_total=0, swap_free=0, zswapped_bytes=0)
    for height in (30, 16):
        lines = [line.plain for line in linux(off, 200, height)]
        swap = next(line for line in lines if line.startswith("Swap"))
        assert re.search(r"\boff\b", swap) and "GiB used" not in swap
        assert "zswapped" not in swap  # no swap, no unit and nothing for the pool to hold
    for height in (30, 16):
        known = linux(FULL_STATS, 200, height)
        unknown = linux(replace(FULL_STATS, **PRESSURES["unavailable"]), 200, height)
        assert [label_columns(row) for row in known] == [label_columns(row) for row in unknown]


# --- colours and thresholds ---------------------------------------------------------


def test_swap_pair_is_error_coloured_only_above_ninety_percent() -> None:
    def colour(free: int) -> str:
        stats = replace(FULL_STATS, swap_total=1000, swap_free=free, zswap_enabled=False)
        swap = linux(stats, 200)[1]
        match = re.search(r"[\d.]+/[\d.]+\s+\w+\s+used", swap.plain)
        assert match
        start, end = match.span()
        covering = [str(s.style) for s in swap.spans if s.start <= start and s.end >= end]
        return covering[-1] if covering else ""

    # Below the threshold the pair has no colour of its own (not the gauge's either).
    assert colour(100) == ""  # exactly 90 %
    assert colour(500) == ""
    assert COLORS.error in colour(99)
    assert COLORS.error in colour(0)


def test_pressure_some_is_warning_and_high_is_error() -> None:
    some = linux(replace(FULL_STATS, pressure_some_avg10=12.0), 200)[3]
    high = linux(replace(FULL_STATS, pressure_some_avg10=30.0), 200)[3]
    none = linux(FULL_STATS, 200)[3]
    assert COLORS.warning in style_at(some, "some")
    assert COLORS.error in style_at(high, "high")
    assert COLORS.success in style_at(none, "none")


@pytest.mark.parametrize(
    ("elsewhere", "shown"),
    [(None, False), (0, False), (MIB - 1, False), (MIB, True), (276 * MIB, True)],
)
def test_elsewhere_appears_from_one_mebibyte(elsewhere: int | None, shown: bool) -> None:
    pressure = linux(replace(FULL_STATS, elsewhere=elsewhere), 200)[3].plain
    assert ("elsewhere" in pressure) is shown


def test_zswap_over_limit_flips_exactly_above_the_limit() -> None:
    limit = FULL_STATS.mem_total * 20 // 100
    at = linux(replace(FULL_STATS, zswap_pool_bytes=limit), 200)[1]
    over = linux(replace(FULL_STATS, zswap_pool_bytes=limit + 1), 200)[1]
    assert "limit 20%" in at.plain and "above" not in at.plain
    assert "above 20%" in over.plain and "limit 20%" not in over.plain
    assert COLORS.error in style_at(over, "above 20%")
    assert all(COLORS.error not in str(span.style) for span in at.spans)


def test_zswap_gauge_is_the_pools_ram_against_its_limit() -> None:
    limit = FULL_STATS.mem_total * 20 // 100
    half = linux(replace(FULL_STATS, zswap_pool_bytes=limit // 2, zswapped_bytes=500 * GIB), 160)[1]
    quarter = linux(replace(FULL_STATS, zswap_pool_bytes=limit // 4, zswapped_bytes=GIB), 160)[1]
    assert half.plain.count("█") == 10
    assert quarter.plain.count("█") == 5


# --- F4 and F6 ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_darwin_command_drill_down_marks_the_pid_column_when_sorted_by_pid() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", "/usr/bin/helper", 20)
    reader.add(12, 10, "helper", "/usr/bin/helper", 30)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("p", "g")
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        detail = pilot.app.screen
        assert isinstance(detail, DarwinProcessesScreen)
        table = detail.query_one("#table", RowTable)
        labels = [table.get_column_label(key) for key in table.column_keys]
        assert labels[0].startswith("PID") and labels[0] != "PID", labels
        assert sum("▴" in label or "▾" in label for label in labels) == 1


@pytest.mark.asyncio
async def test_unchanged_content_never_reaches_static_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = object.__new__(DarwinBackend)
    monkeypatch.setattr(backend, "read_system", lambda: HOST)
    apps: list[DarwinApp] = []
    monkeypatch.setattr(backend, "collect_apps", lambda: apps)
    owner = DarwinMainScreen(backend, 3600)
    app = AppMemApp(interval=3600, main_screen_factory=lambda: owner)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        current = owner.query_one("#header1", Static).content
        calls: list[object] = []
        original = Static.update

        def counting(self: Static, content: object = "", **kwargs: bool) -> None:
            calls.append(content)
            original(self, content, **kwargs)  # pyright: ignore[reportArgumentType]

        monkeypatch.setattr(Static, "update", counting)
        assert isinstance(current, Text)
        owner._set_static("#header1", current)  # pyright: ignore[reportPrivateUsage]
        assert calls == []
        owner._set_static("#header1", "changed")  # pyright: ignore[reportPrivateUsage]
        assert calls == ["changed"]
        await pilot.press("h")
        await pilot.pause()
        panel = app.screen
        assert isinstance(panel, HostPanel)
        calls.clear()
        panel.update_sample()
        panel.update_sample()
        assert calls == []


# --- a value going unknown never changes the layout ---------------------------------

WORDS = re.compile(
    r"\b(wired|free|file-backed|purgeable|data|ratio|in|out|written|run|boot|since|avail|shared"
    r"|holds|limit|above|writeback|system|elsewhere|apps|total)\b"
)


def fingerprint(lines: list[Text]) -> tuple[object, ...]:
    """Everything that must not move: the gauge of the Swap row, each row's
    separator and the cell of every label."""
    swap = next(line for line in lines if line.plain.startswith("Swap"))
    rows = [
        (line.plain.find("│"), {m.group(1): m.start() for m in WORDS.finditer(line.plain)})
        for line in lines
    ]
    return (gauge_cells(swap), rows)


def test_mac_partition_going_unknown_and_back_moves_nothing() -> None:
    for width in range(116, 141):
        known = mac(HOST, width)
        unknown = mac(replace(HOST, file_backed_bytes=None), width)
        assert HOST.ram_partition is not None
        assert replace(HOST, file_backed_bytes=None).ram_partition is None
        assert fingerprint(unknown) == fingerprint(known), width
        assert fingerprint(mac(HOST, width)) == fingerprint(known)
        # the four fields stay, with a dash in the unknown ones
        ram = unknown[0].plain
        assert all(word in ram for word in ("wired", "free", "file-backed", "purgeable")) or (
            ram.endswith("…")
        )
        assert re.search(r"—/8\.0 GiB used", ram)


@pytest.mark.parametrize("width", [116, 120, 124, 130, 139, 142, 160])
def test_mac_unknown_values_keep_every_label_in_its_cell(width: int) -> None:
    broken = replace(
        HOST,
        file_backed_bytes=None,
        wired_bytes=-1,
        purgeable_bytes=None,
        compressor_logical_bytes=-1,
        swap_out_bytes=None,
        pressure_level=None,
    )
    assert fingerprint(mac(broken, width, rates=(None, None), session=None)) == fingerprint(
        mac(HOST, width, rates=(1023 * 1024, 1023 * 1024), session=2**64 - 1)
    )
    # absurd rates and totals still cannot change the gauge
    wild = mac(HOST, width, rates=(10**30, 10**30), session=10**30)
    assert fingerprint(wild)[0] == fingerprint(mac(HOST, width))[0]


def test_linux_values_going_unknown_or_absurd_move_nothing() -> None:
    absurd = replace(
        TYPICAL,
        zswap_pool_bytes=None,
        zswap_compression_ratio=None,
        zswap_writeback_bytes=None,
        swap_out_bytes=None,
        pressure_some_avg10=None,
        elsewhere=None,
    )
    for width in range(116, 141):
        plain = linux(TYPICAL, width, rates=(0, 0), session=0, writeback=0)
        odd = linux(absurd, width, rates=(None, None), session=None, writeback=None)
        wild = linux(TYPICAL, width, rates=(10**30, 10**30), session=10**30, writeback=10**30)
        assert fingerprint(plain)[0] == fingerprint(odd)[0] == fingerprint(wild)[0], width
        for before, after in zip(fingerprint(plain)[1], fingerprint(odd)[1], strict=True):  # type: ignore[arg-type]
            assert before[0] == after[0]


def test_zswap_writeback_shows_a_measured_zero_and_a_dash_for_unknown() -> None:
    zero = linux(width=200, writeback=0)[1].plain
    unknown = linux(width=200, writeback=None)[1].plain
    assert re.search(r"writeback\s+0 B/s", zero)
    assert re.search(r"writeback\s+—", unknown) and "0 B/s" not in unknown.split("writeback")[1]


_BAR_GLYPHS = set("█▏▎▍▌▋▊▉░#.-")
_PRESSURE_WORDS = {"none", "some", "high", "normal", "warning", "critical"}


@pytest.mark.parametrize(("name", "render"), PLATFORMS)
@pytest.mark.parametrize("width", [80, 105, 130, 160])
@pytest.mark.parametrize("ascii_bars", [False, True])
def test_only_the_gauge_and_the_pressure_word_are_coloured(
    name: str, render: Render, width: int, ascii_bars: bool
) -> None:
    """A gauge's or a state word's colour must not run on into the value beside it."""
    for line in render(width=width, ascii_bars=ascii_bars):
        assert str(line.style) in ("", "none"), line.plain
        for span in line.spans:
            covered = line.plain[span.start : span.end]
            style = str(span.style)
            if "blue" in style:
                assert set(covered) <= _BAR_GLYPHS, (name, covered)
            if "bold" in style:
                assert covered in _PRESSURE_WORDS, (name, covered)


# --- the value beside a gauge: fixed cells --------------------------------------------

_VALUE = re.compile(r"/\S+\s+(\w+)\s+(used|RAM)")
_ALIGN_WIDTHS = (80, 105, 130, 160)


def value_cells(row: Text) -> tuple[int, int, int, int]:
    """Columns of the slash, the unit, the word and the separator of a gauge row."""
    match = _VALUE.search(row.plain)
    assert match, row.plain
    return (row.plain.index("/"), match.start(1), match.start(2), row.plain.index("│"))


def gauge_rows(lines: list[Text]) -> list[Text]:
    return [line for line in lines if _VALUE.search(line.plain) and "│" in line.plain]


@pytest.mark.parametrize("width", _ALIGN_WIDTHS)
@pytest.mark.parametrize("height", [30, 16])
def test_linux_slash_unit_and_word_start_at_one_cell_on_every_gauge_row(
    width: int, height: int
) -> None:
    rows = gauge_rows(linux(TYPICAL, width, height))
    assert len(rows) == (3 if height >= 18 else 2)
    assert len({value_cells(row) for row in rows}) == 1, [row.plain for row in rows]


@pytest.mark.parametrize("width", _ALIGN_WIDTHS)
def test_macos_slash_unit_and_word_start_at_one_cell_on_every_gauge_row(width: int) -> None:
    lines = mac(HOST, width)
    rows = gauge_rows(lines)
    assert len(rows) == 2
    assert len({value_cells(row) for row in rows}) == 1, [row.plain for row in rows]
    slash, unit, word, separator = value_cells(rows[0])
    compress = re.search(r"^Compress\s+(\S+) (\w+)\s+RAM\s+│", lines[1].plain)
    assert compress, lines[1].plain
    # one number: at the total's cell, its unit and word in the unit and word columns
    assert (compress.start(1), compress.start(2)) == (slash + 1, unit)
    assert lines[1].plain.index("RAM", compress.start(2)) == word
    assert lines[1].plain.index("│") == separator


def test_linux_value_cells_match_the_approved_mock() -> None:
    ram, zswap, swap, _ = linux(TYPICAL, 160)
    assert re.search(r"[█░] \d{2}\.\d/30\.9 GiB used │", ram.plain)
    assert re.search(r"[█░]\s+1\.0/6\.2  GiB RAM  │", zswap.plain)
    assert re.search(r"[█░] \d{2}\.\d/32\.0 GiB used │", swap.plain)


def test_macos_value_cells_match_the_approved_mock() -> None:
    ram, compress, swap, _ = mac(HOST, 160)
    assert re.search(r"[█░] 5\.1/8\.0 GiB used │", ram.plain)
    assert re.search(r"Compress\s+1\.0 GiB RAM  │", compress.plain)
    assert re.search(r"[█░] 0\.5/1\.0 GiB used │", swap.plain)


def linux_variants() -> list[SystemStats]:
    limit = TYPICAL.mem_total * 20 // 100
    return [
        TYPICAL,
        replace(TYPICAL, mem_available=TYPICAL.mem_total),  # RAM used 0
        replace(TYPICAL, mem_available=0),  # RAM used all of it
        replace(TYPICAL, swap_free=TYPICAL.swap_total),  # swap used 0
        replace(TYPICAL, swap_free=0),  # swap used all of it (error colour)
        replace(TYPICAL, zswap_pool_bytes=0),
        replace(TYPICAL, zswap_pool_bytes=limit),
        replace(TYPICAL, zswap_pool_bytes=limit + 1),  # over its limit (error colour)
        replace(TYPICAL, zswap_pool_bytes=None),  # `unavailable`
    ]


@pytest.mark.parametrize("width", _ALIGN_WIDTHS)
def test_linux_value_cells_do_not_move_with_the_values(width: int) -> None:
    reference = [value_cells(row) for row in gauge_rows(linux(TYPICAL, width))]
    for stats in linux_variants():
        rows = linux(stats, width)
        assert len({row.plain.index("│") for row in rows if "│" in row.plain}) == 1
        for row in gauge_rows(rows):
            assert value_cells(row) in reference, row.plain


@pytest.mark.parametrize("width", _ALIGN_WIDTHS)
def test_macos_value_cells_do_not_move_with_the_values(width: int) -> None:
    reference = {value_cells(row) for row in gauge_rows(mac(HOST, width))}
    variants = [
        HOST,
        replace(HOST, free_bytes=4 * GIB),  # RAM used drops
        replace(HOST, free_bytes=0, file_backed_bytes=0),  # RAM used rises
        replace(HOST, swap_used_bytes=0),
        replace(HOST, swap_used_bytes=HOST.swap_total_bytes),
        replace(HOST, compressor_physical_bytes=1023 * MIB),
        replace(HOST, compressor_physical_bytes=0, compressor_logical_bytes=0),
    ]
    for host in variants:
        lines = mac(host, width)
        assert len({line.plain.index("│") for line in lines if "│" in line.plain}) == 1
        assert {value_cells(row) for row in gauge_rows(lines)} <= reference


def test_special_forms_stay_inside_the_field_without_moving_the_separator() -> None:
    off = linux(replace(TYPICAL, swap_total=0, swap_free=0, zswapped_bytes=0), 160)
    assert len({row.plain.index("│") for row in off if "│" in row.plain}) == 1
    assert re.search(r"[░-]\s+off │", off[2].plain)
    for host in (
        replace(HOST, swap_used_bytes=0, swap_total_bytes=0),
        replace(HOST, swap_used_bytes=-1, swap_total_bytes=1),
        replace(HOST, file_backed_bytes=None),
    ):
        lines = mac(host, 160)
        assert len({line.plain.index("│") for line in lines if "│" in line.plain}) == 1
    swap_off = mac(replace(HOST, swap_used_bytes=0, swap_total_bytes=0), 160)[2].plain
    assert re.search(r"0 B; not allocated │", swap_off)
    assert re.search(r"—/8\.0\s+GiB used", mac(replace(HOST, file_backed_bytes=None), 160)[0].plain)


def test_a_pool_over_a_mib_limit_with_one_more_digit_keeps_the_separator() -> None:
    # 4 GiB of RAM: the 20 % limit is 819 MiB, and a pool left above it can reach 1229 MiB.
    small = replace(
        TYPICAL,
        mem_total=4 * GIB,
        mem_available=GIB,
        swap_total=4 * GIB,
        swap_free=2 * GIB,
        zswap_enabled=True,
        zswap_max_pool_percent=20,
    )
    for pool in (500 * MIB, 1229 * MIB):
        rows = linux(replace(small, zswap_pool_bytes=pool), 130)
        assert len({row.plain.index("│") for row in rows if "│" in row.plain}) == 1
    assert "1229/819 MiB RAM │" in linux(replace(small, zswap_pool_bytes=1229 * MIB), 130)[1].plain


def error_span(row: Text) -> str:
    """The error-coloured text left of the separator (the value, not the limit word)."""
    separator = row.plain.index("│")
    covered = [
        row.plain[s.start : s.end]
        for s in row.spans
        if COLORS.error in str(s.style) and s.end < separator
    ]
    assert len(covered) == 1, covered
    return covered[0]


def test_error_colour_covers_the_figures_of_the_value_not_its_padding() -> None:
    limit = TYPICAL.mem_total * 20 // 100
    over = linux(replace(TYPICAL, zswap_pool_bytes=limit + 1), 160)[1]
    assert re.fullmatch(r"\d\.\d/6\.2  GiB RAM", error_span(over))
    nearly_full = linux(replace(TYPICAL, swap_free=0), 160)[2]
    assert error_span(nearly_full) == "32.0/32.0 GiB used"
    # the text next to the error colour carries none of it
    assert "│" not in error_span(nearly_full)


def legacy_value(pairs: list[tuple[int, str]]) -> int:
    return max(pair_width(total, word) for total, word in pairs)


def machines() -> list[tuple[int, int, int | None]]:
    rams = [int(x * GIB) for x in (2, 4.9, 8, 15.5, 16, 30.9, 32, 64, 127.5, 128)]
    swaps = [0, GIB // 2, GIB, 2 * GIB, 8 * GIB, int(9.9 * GIB), 10 * GIB, 32 * GIB, 100 * GIB]
    return [(ram, swap, pct) for ram in rams for swap in swaps for pct in (None, 20, 50)]


@pytest.mark.parametrize("gauge", [0, 6, 10, 12, 16, 20])
def test_the_left_part_never_grows_beyond_the_old_composition(gauge: int) -> None:
    """Aligned cells that would widen the left part for a machine are not used:
    that machine keeps the old composition (same width, empty fields)."""
    for ram, swap, pct in machines():
        stats = replace(
            TYPICAL,
            mem_total=ram,
            mem_available=ram // 3,
            swap_total=swap,
            swap_free=swap // 2,
            zswap_enabled=pct is not None,
            zswap_max_pool_percent=pct,
        )
        slots = linux_slots(stats)
        pairs = [(ram, "used")] + ([(swap, "used")] if swap else [])
        if pct is not None and ram * pct // 100:
            pairs.append((ram * pct // 100, "RAM"))
        old = replace(slots, value=max(legacy_value(pairs), 0 if swap else 3), fields=ValueFields())
        assert slots.value == old.value, (ram, swap, pct)
        new_left = make_layout(100, slots, gauge, wide=False).left
        assert new_left == make_layout(100, old, gauge, wide=False).left, (ram, swap, pct)
    for ram, swap, _ in machines():
        host = replace(HOST, physical_bytes=ram, swap_total_bytes=swap, swap_used_bytes=swap // 2)
        slots = mac_slots(host)
        pairs = [(ram, "used")] + ([(swap, "used")] if swap else [])
        floor = len("used unavailable") if swap else len("0 B; not allocated")
        assert slots.value == max(legacy_value(pairs), floor), (ram, swap)
        if swap:  # both words are `used`: the aligned cells are never wider
            assert slots.fields != ValueFields(), (ram, swap)


@pytest.mark.parametrize("ram", [8, 16, 32, 64])
@pytest.mark.parametrize("swap", [GIB // 2, GIB, 3 * GIB, 8 * GIB, 12 * GIB, 40 * GIB])
@pytest.mark.parametrize("width", [80, 105, 130, 160])
def test_macos_cells_align_on_every_memory_size_with_small_and_large_swap(
    ram: int, swap: int, width: int
) -> None:
    host = replace(HOST, physical_bytes=ram * GIB, swap_total_bytes=swap, swap_used_bytes=swap // 2)
    lines = mac(host, width)
    assert mac_slots(host).fields != ValueFields()
    assert len({line.plain.index("│") for line in lines if "│" in line.plain}) == 1
    assert len({value_cells(row) for row in gauge_rows(lines)}) == 1, [r.plain for r in lines]


def test_the_rare_linux_machine_with_a_wider_zswap_limit_keeps_the_old_composition() -> None:
    # About 4.9 GiB of RAM with a 20 % pool limit of 1004 MiB: a 4-digit total on the RAM-word row
    # beside a 3-digit one would make the aligned cells one wider, so no cells are used.
    stats = replace(
        TYPICAL,
        mem_total=int(4.9 * GIB),
        mem_available=GIB,
        swap_total=3 * GIB,
        swap_free=GIB,
        zswap_enabled=True,
    )
    assert linux_slots(stats).fields == ValueFields()
    rows = linux(stats, 160)
    assert len({row.plain.index("│") for row in rows if "│" in row.plain}) == 1
    assert linux_slots(TYPICAL).fields != ValueFields() and mac_slots(HOST).fields != ValueFields()
