"""Main view: the live, sortable per-app table (SPEC.md "Main view").

Refreshes on a `set_interval` timer, whose tick reads in a plain thread
(`threading.Thread`, not `run_worker`: see `_tick_read`) rather than on the
event loop: the collector is normally fast (~7 ms on the dev machine,
SPEC.md "Tech" budget: under 1 % of one core at 1 s), but a slow filesystem
or a big tree can still make one read take much longer, and a blocking read
on the event loop would freeze key handling for its whole duration
(SPEC.md "Tech"). `refresh_now` stays
synchronous, for mount, explicit actions and tests. Existing rows are updated
in place and only appeared/vanished apps add or remove a row, so a refresh
never rebuilds the table (SPEC.md "Tech" notes).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.color import Color
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Static

from appmem.backend import Backend
from appmem.collect import CgroupUnavailableError, MemoryStatUnavailableError
from appmem.fmt import format_delta, size, truncate_name
from appmem.model import AppStats, SystemStats
from appmem.rate import Sample, update_rate
from appmem.render import escape_control_chars
from appmem.ui.header import ThemeColors, render_header
from appmem.ui.layout import build_footer
from appmem.ui.process_rows import initial_process_sort
from appmem.ui.rows import (
    DEFAULT_SORT_KEY,
    DEFAULT_SORT_REVERSE,
    Row,
    SortKey,
    build_rows,
    next_sort_state,
    reset_baseline,
    row_key,
    sort_rows,
    update_baseline,
)
from appmem.ui.screens.help import HelpScreen
from appmem.ui.screens.live import LiveScreen
from appmem.ui.screens.processes import ProcessesScreen
from appmem.ui.table import RowTable

# Key caps (reverse video); at full width the plain text is exactly
# " r s t d z sort  enter procs  x system  c cache  w zswap  b reset Δ  T theme
# ? help  q quit" (the "z" sort key and the "w zswap" item only where the
# ZSWAP column is shown/enabled). `d` (sort by ΔSWAP) and `z` (sort by ZSWAP)
# each drop out of the "sort" item's own key caps -- not the whole item --
# while their column is hidden, whether by width or (for ZSWAP) by `w` or by
# zswap being off (SPEC.md "Main view": a key that does nothing in the
# current view doesn't appear). `theme` is the lowest priority of all,
# dropped before `reset Δ` (SPEC.md "Command line"); `zswap` drops right
# after `cache` (SPEC.md "Main view").
_FOOTER_DROP_ORDER = ("theme", "reset Δ", "cache", "zswap", "system", "procs", "sort")

# Below this width, ΔSWAP/ΔRAM are hidden (SPEC.md "Main view"). Re-shown
# above it.
_NARROW_WIDTH = 95

# Below this width, ZSWAP is hidden too, on top of the Δ columns (SPEC.md
# "Main view": ZSWAP outranks the Δ columns, so its own cutoff sits below
# theirs). 85 is the owner's chosen number, consistent with the Δ columns'
# own 95: `_app_column_width` shrinks the APP column (toward its own
# `_APP_MIN_WIDTH` floor if needed) at every width, so a long name never
# pushes ZSWAP -- or any other numeric column -- off screen at 85.
_ZSWAP_MIN_WIDTH = 85

# The APP column's floor: `_app_column_width` shrinks it (with truncation)
# at every width so RAM, SWAP, TOTAL and PROCS stay whole on screen instead
# of being pushed off the edge by an APP column auto-sized to a long name
# (SPEC.md "Main view").
_APP_MIN_WIDTH = 8
_APP_MAX_WIDTH = 32
# `RowTable`'s own cell padding (1 cell each side of every column).
_CELL_PADDING = 2

# Width `None` = auto: the APP column grows to the longest name (capped at 32 by
# `truncate_name` in `_format_cell`), so short names are never cut short of that.
_BASE_COLUMNS: tuple[tuple[SortKey, str, int | None], ...] = (
    ("app", "APP", None),
    ("ram", "RAM", 10),
    ("swap", "SWAP", 10),
)
_CACHE_COLUMN: tuple[SortKey, str, int | None] = ("cache", "CACHE", 10)
_ZSWAP_COLUMN: tuple[SortKey, str, int | None] = ("zswap", "ZSWAP", 10)
_TOTAL_COLUMN: tuple[SortKey, str, int | None] = ("total", "TOTAL", 10)
_DELTA_COLUMNS: tuple[tuple[SortKey, str, int | None], ...] = (
    ("delta_ram", "ΔRAM", 9),
    ("delta_swap", "ΔSWAP", 9),
)
_PROCS_COLUMN: tuple[SortKey, str, int | None] = ("procs", "PROCS", 7)
# 7, not 6: "PROCS ▾"/"PROCS ▴" is 7 cells, so a 6-wide column always cut the
# sort marker off (SPEC.md "Main view"). Fits every narrow-terminal budget
# already: `_app_column_width` shrinks APP first, well before PROCS' own
# floor would ever matter.
_DELTA_KEYS = frozenset({"delta_swap", "delta_ram"})


def _format_app_cell(row: Row, app_cap: int) -> str:
    name = escape_control_chars(row.name)
    label = name if row.scope == "user" else f"{name} [sys]"
    return truncate_name(label, app_cap)


# One entry per non-"app" `SortKey`, keeping `_format_cell` itself a flat
# lookup instead of a long if-chain (ruff C901).
_CELL_FORMATTERS: dict[SortKey, Callable[[Row], str]] = {
    "swap": lambda row: size(row.swap),
    "ram": lambda row: size(row.ram),
    "cache": lambda row: size(row.cache),
    "zswap": lambda row: size(row.zswap),
    "total": lambda row: size(row.total),
    "delta_swap": lambda row: format_delta(row.delta_swap),
    "delta_ram": lambda row: format_delta(row.delta_ram),
    "procs": lambda row: str(row.procs),
}


def _format_cell(key: SortKey, row: Row, *, app_cap: int = _APP_MAX_WIDTH) -> str:
    if key == "app":
        return _format_app_cell(row, app_cap)
    return _CELL_FORMATTERS[key](row)


def _rich_color(theme_color: str) -> str:
    # `Theme.success`/`warning`/`error` are Textual colour specs, and the two
    # built-in `ansi-*` themes use Textual's own "ansi_red"-style names
    # (SPEC.md "Main view"), which Rich's `Style` parser doesn't understand
    # on its own -- `render_header` plugs this straight into a Rich style
    # string. Round-tripping through `textual.color.Color` normalises every
    # theme's colour (hex or `ansi_*`) to a form Rich always accepts.
    return Color.parse(theme_color).rich_color.name


_LOCALE_ENV_VARS = ("LC_ALL", "LC_CTYPE", "LANG")


def _locale_setting() -> str:
    """The first non-empty of `LC_ALL`/`LC_CTYPE`/`LANG` (the same
    resolution order glibc uses), or `"C"` -- the POSIX default -- when none
    of the three is set at all."""
    for var in _LOCALE_ENV_VARS:
        value = os.environ.get(var, "")
        if value:
            return value
    return "C"


def _is_non_utf8_locale(setting: str) -> bool:
    # A `@modifier` (`de_DE.UTF-8@euro`) is not part of the codeset.
    base, _, codeset = setting.partition("@")[0].partition(".")
    if base.upper() in ("C", "POSIX") and not codeset:
        return True
    if not codeset:
        return True  # no codeset at all (e.g. bare "en_US"): can't assume UTF-8
    return codeset.replace("-", "").upper() != "UTF8"


def _detect_ascii_bars() -> bool:
    """Whether the header's gauge bars must fall back to a plain `#`/`.`
    form: the Unicode Block Elements (`█ ░` and the eighth-block glyphs) need
    a UTF-8 locale (`header.py`'s module docstring, SPEC.md "Main view").
    Catches an explicit `LC_ALL=C`/`POSIX` and an installed non-UTF-8
    codeset. A bare `LANG=C` (or no locale at all) never gets here as C:
    Python's PEP 538 coercion exports `LC_CTYPE=C.UTF-8` at startup, so it
    counts as UTF-8, the same as the rest of the Textual UI's glyphs."""
    return _is_non_utf8_locale(_locale_setting())


_MIN_BAR_CONTRAST = 3.0
# Where a theme sets no background/foreground of its own: Textual's own
# built-in defaults (`textual.design.ColorSystem._generate`), so a theme's
# contrast is measured against what actually renders, not against black.
_DEFAULT_DARK_BACKGROUND = "#121212"
_DEFAULT_LIGHT_BACKGROUND = "#efefef"


def _contrast_ratio(a: Color, b: Color) -> float:
    """WCAG relative-luminance contrast ratio between two colours: 1
    (identical) to 21 (black on white)."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def luminance(color: Color) -> float:
        return 0.2126 * linear(color.r) + 0.7152 * linear(color.g) + 0.0722 * linear(color.b)

    lighter, darker = sorted((luminance(a), luminance(b)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _theme_background(theme: Theme) -> Color:
    if theme.background:
        return Color.parse(theme.background)
    return Color.parse(_DEFAULT_DARK_BACKGROUND if theme.dark else _DEFAULT_LIGHT_BACKGROUND)


def _bar_fill_colour(theme: Theme) -> str:
    """The gauge bars' fill colour (SPEC.md "Main view", "Colour"): the
    theme's own accent colour, unless it falls short of WCAG's 3:1 contrast
    floor against that theme's background (flexoki measures 2.93:1) -- then
    its foreground colour, chosen for text contrast, instead. Skipped for
    the two `ansi_*` themes, whose colours are the terminal's own with no
    real RGB behind them to measure a ratio against."""
    if theme.ansi:
        return _rich_color(theme.primary)
    primary = Color.parse(theme.primary)
    background = _theme_background(theme)
    if _contrast_ratio(primary, background) >= _MIN_BAR_CONTRAST:
        return _rich_color(theme.primary)
    foreground = Color.parse(theme.foreground) if theme.foreground else background.inverse
    return foreground.rich_color.name


# PROCS refreshes every 5th tick in the live main view, the memory columns
# every tick (SPEC.md "Main view"): the recursive `cgroup.procs` walk is a
# measurable share of the tick cost, and PROCS doesn't need 1 s freshness.
_PROCS_COUNT_INTERVAL = 5


@dataclass(frozen=True)
class _TickResult:
    """A background tick's outcome, plus the generation it was read under --
    so a result whose context changed while it was in flight (`x` toggled, or
    the screen covered/resumed) is discarded instead of applied stale
    (SPEC.md "Tech")."""

    generation: int
    stats: SystemStats | None = None
    apps: list[AppStats] | None = None
    cgroup_error: CgroupUnavailableError | None = None
    procs_by_unit: dict[str, int] | None = None
    """The unit-path -> procs map this tick read or carried forward (SPEC.md
    "Main view" PROCS cadence), to become the next tick's `previous_procs`."""


class TickDone(Message):
    """A tick's thread has finished: the read's result, or the exception it
    raised. Posted from the thread (`post_message` is thread-safe) and handled
    on the UI thread, the only one allowed to touch widgets."""

    bubble: ClassVar[bool] = False

    def __init__(self, payload: _TickResult | Exception) -> None:
        super().__init__()
        self.payload = payload


def _tick_worker(
    backend: Backend,
    include_system: bool,
    generation: int,
    *,
    count_procs: bool,
    previous_procs: Mapping[str, int],
) -> _TickResult:
    """Runs in a thread (SPEC.md "Tech"): blocking `/proc`/`/sys` reads
    only, no Textual calls. Transient errors are swallowed here, same as
    `refresh_now`'s `try` -- the tick is skipped, not the session."""
    try:
        stats = backend.read_system()
        apps, procs_by_unit = backend.collect_apps(
            include_system=include_system,
            strict=False,
            count_procs=count_procs,
            previous_procs=previous_procs,
        )
    except CgroupUnavailableError as exc:
        return _TickResult(generation=generation, cgroup_error=exc)
    except (MemoryStatUnavailableError, OSError, ValueError):
        return _TickResult(generation=generation)
    return _TickResult(generation=generation, stats=stats, apps=apps, procs_by_unit=procs_by_unit)


class MainScreen(LiveScreen):
    """The per-app table: header lines, the RowTable, and the footer (SPEC.md)."""

    # `RowTable` (a bare `ScrollView`) doesn't run away to fit its content the
    # way `DataTable`'s own `height: auto; max-height: 100%` default used to,
    # but `1fr` is kept explicit so the table only ever claims the space left
    # over from the header lines and the footer, regardless of row count.
    DEFAULT_CSS = """
    MainScreen #table { height: 1fr; }
    MainScreen #header1, MainScreen #header2, MainScreen #header3,
    MainScreen #header4, MainScreen #footer {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "sort('swap')", "sort SWAP", show=False),
        Binding("r", "sort('ram')", "sort RAM", show=False),
        Binding("t", "sort('total')", "sort TOTAL", show=False),
        Binding("d", "sort('delta_swap')", "sort ΔSWAP", show=False),
        Binding("z", "sort('zswap')", "sort ZSWAP", show=False),
        Binding("c", "toggle_cache", "toggle CACHE", show=False),
        Binding("w", "toggle_zswap", "toggle ZSWAP", show=False),
        Binding("x", "toggle_system", "toggle system", show=False),
        Binding("b", "reset_delta", "reset Δ", show=False),
        Binding("?", "help", "help", show=False),
    ]

    def __init__(self, *, backend: Backend, interval: float, include_system: bool) -> None:
        super().__init__()
        self._backend = backend
        self._interval = interval
        self._show_system = include_system
        self._show_cache = False
        self._show_zswap = True
        """The user's own ZSWAP choice (`w`), independent of whether the
        column is actually on screen right now (`_zswap_column_shown` below
        also needs zswap enabled and enough width). Starts `True`: ZSWAP is
        shown by default once zswap turns out to be enabled (SPEC.md "Main
        view")."""
        self._zswap_enabled = False
        """Whether this machine has zswap on (`SystemStats.zswap_enabled`
        from the last tick): gates the `w` key, the ZSWAP column, and the
        footer/help mentions of it (SPEC.md "Main view"). Starts `False`
        (unknown) until the first tick lands."""
        self._writeback_history: tuple[Sample, ...] = ()
        self._writeback_rate: int | None = None
        self._swap_in_history: tuple[Sample, ...] = ()
        self._swap_out_history: tuple[Sample, ...] = ()
        self._swap_in_rate: int | None = None
        self._swap_out_rate: int | None = None
        self._sort_key: SortKey = DEFAULT_SORT_KEY
        self._sort_reverse = DEFAULT_SORT_REVERSE
        self._baseline: dict[tuple[str, str], AppStats] = {}
        self._baseline_time = datetime.now()
        self._rows: dict[str, Row] = {}
        self._last_apps: list[AppStats] = []
        self._delta_columns_shown = True
        self._zswap_column_shown = False
        """Whether ZSWAP is actually on screen right now: `_show_zswap` (the
        user's `w` choice) and `_zswap_enabled` (a machine fact) both say
        yes, and the terminal is at least `_ZSWAP_MIN_WIDTH` wide. Tracked
        separately from those inputs (mirrors `_delta_columns_shown`) so a
        resize crossing the width threshold can fall back the active sort
        the same way `w`/zswap-disabling already do."""
        self._young_baseline = True
        self._delta_restyle_pending = False
        self._last_stats: SystemStats | None = None
        self._column_widths: tuple[int | None, ...] = ()
        self._ascii_bars = _detect_ascii_bars()
        self._tick_count = 0
        """Advanced on every collection (tick, mount, or an explicit action
        that re-collects), independent of `_generation`: drives the PROCS
        cadence (`_PROCS_COUNT_INTERVAL`), never reset by a context change."""
        self._procs_by_unit: dict[str, int] = {}
        """Last known procs count per unit path (`str(path)`), carried
        forward on a tick that skips the recursive `cgroup.procs` walk
        (SPEC.md "Main view")."""
        """Bumped on every context change (`x` toggled, screen covered or
        resumed). A background result carries the generation it was read
        under; `_apply_tick` discards one that no longer matches."""
        self._resume_scrolls = False
        """Set while the process view covers this screen: coming back from it
        is a drill-out and scrolls; closing help or the theme panel is not."""

    def compose(self) -> ComposeResult:
        self._delta_columns_shown = self._show_delta_columns()
        self._zswap_column_shown = self._show_zswap_column()
        yield Static(id="header1")
        yield Static(id="header2")
        yield Static(id="header3")
        yield Static(id="header4")
        table: RowTable = RowTable(id="table")
        self._rebuild_columns(table)
        yield table
        yield Static(self._footer_text(), id="footer")

    def _zswap_footer_shown(self) -> bool:
        """Whether the footer's `w zswap` item shows: `_zswap_enabled` (a
        machine fact) and wide enough for the ZSWAP column itself, but
        independent of `_show_zswap` (the user's own `w` choice, SPEC.md
        "Main view") -- `w` keeps toggling that choice either way, so the
        item stays while it has an effect the user can see right away, and
        only goes while the column is hidden by width, where pressing `w`
        would do nothing until the terminal widens back past
        `_ZSWAP_MIN_WIDTH`."""
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        return self._zswap_enabled and width >= _ZSWAP_MIN_WIDTH

    def _footer_items(self) -> tuple[tuple[tuple[str, ...], str], ...]:
        # `d` (sort by ΔSWAP) and `z` (sort by ZSWAP) are each a no-op while
        # their own column is hidden (`action_sort` refuses them), so they
        # drop out of the "sort" item's own key caps rather than staying as
        # dead text (SPEC.md "Main view").
        sort_keys = ["r", "s", "t"]
        if self._delta_columns_shown:
            sort_keys.append("d")
        if self._zswap_column_shown:
            sort_keys.append("z")
        items: list[tuple[tuple[str, ...], str]] = [
            (tuple(sort_keys), "sort"),
            (("enter",), "procs"),
            (("x",), "system"),
            (("c",), "cache"),
        ]
        if self._zswap_footer_shown():
            items.append((("w",), "zswap"))
        items.extend(
            [
                (("b",), "reset Δ"),
                (("T",), "theme"),
                (("?",), "help"),
                (("q",), "quit"),
            ]
        )
        return tuple(items)

    def _footer_text(self) -> Text:
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        return build_footer(self._footer_items(), width=width, drop_order=_FOOTER_DROP_ORDER)

    def on_mount(self) -> None:
        self.watch(self._table(), "show_vertical_scrollbar", self._on_scrollbar_change, init=False)
        self.refresh_now()
        self._start_live_timer(self._interval, self._tick)

    def _on_scrollbar_change(self, previous: bool, current: bool) -> None:
        if previous != current:
            self.call_after_refresh(self._sync_columns)

    def _tick(self) -> None:
        # Covered by the process view or help: skip the work, `on_screen_resume` catches up.
        # A previous tick's read is still in flight: skip, don't stack a second one
        # (SPEC.md "Tech": at most one read in flight per screen).
        if not self._can_launch_tick():
            return
        count_procs = self._next_count_procs()
        previous_procs = dict(self._procs_by_unit)
        self._launch_tick(
            lambda generation: self._tick_read(generation, count_procs, previous_procs)
        )

    def _next_count_procs(self) -> bool:
        """Advance the PROCS cadence counter and say whether this collection
        should recount every unit's `cgroup.procs`, or carry forward what a
        unit already in `_procs_by_unit` last reported (SPEC.md "Main
        view"). Shared by the periodic tick and the synchronous callers
        (`refresh_now`, `b`) so the cadence is one counter, not one per
        path."""
        count_procs = self._tick_count % _PROCS_COUNT_INTERVAL == 0
        self._tick_count += 1
        return count_procs

    def _tick_read(
        self, generation: int, count_procs: bool, previous_procs: dict[str, int]
    ) -> None:
        # Thread side of a tick: the blocking `/proc`/`/sys` reads happen
        # here, off the event loop, so a slow collector never blocks key
        # handling (SPEC.md "Tech"). A plain thread, not `run_worker`: Textual
        # keeps every finished `Worker` alive inside its task's context until
        # a cyclic GC pass, and on Python 3.14 that pass is thousands of ticks
        # away, so workers piled up (with their results, 1.1 GiB in ten hours;
        # bare, still about 7 MiB every 20 minutes). The outcome goes back as
        # a message, which a screen that is already closing simply drops.
        try:
            payload: _TickResult | Exception = _tick_worker(
                self._backend,
                self._show_system,
                generation,
                count_procs=count_procs,
                previous_procs=previous_procs,
            )
        except Exception as exc:  # re-raised on the UI thread by `on_tick_done`
            payload = exc
        self.post_message(TickDone(payload))

    def on_tick_done(self, event: TickDone) -> None:
        self._finish_tick()
        if isinstance(event.payload, Exception):
            # A bug in the collector (transient read errors never get here,
            # `_tick_worker` turns them into an empty result): crash loudly,
            # as an exception on the timer itself would, rather than let the
            # refreshes stop silently.
            raise event.payload
        self._apply_tick(event.payload)

    def _apply_tick(self, result: _TickResult) -> None:
        if result.cgroup_error is not None:
            self._fail_cgroup_unavailable(result.cgroup_error)
            return
        if result.stats is None:  # transient read/parse failure this tick: keep the last frame
            return
        if not self._accept_tick(result.generation):
            # `x` toggled (or the screen was covered/left) while this read was
            # in flight: its result no longer matches the current context,
            # discard it rather than show a stale table (SPEC.md "Tech").
            return
        self._procs_by_unit = result.procs_by_unit or {}
        self._apply_refresh(result.stats, result.apps or [], scroll=False)

    def on_screen_resume(self) -> None:
        # A read dispatched before the covering screen closed is now stale,
        # even if nothing we track here actually changed while it was up.
        # Resuming from the process view is a drill-out, same as Esc
        # elsewhere: the resort can move the selected app out of the old
        # viewport, so it scrolls; help and the theme panel keep a wheel
        # scroll where it was (SPEC.md "Main view").
        self._generation += 1
        scroll, self._resume_scrolls = self._resume_scrolls, False
        self.refresh_now(scroll=scroll)  # no stale numbers after Esc from the process view

    def on_resize(self, event: events.Resize) -> None:
        # Header, columns and footer never wrap -- recompute on every resize,
        # not just on the next tick. The header also reacts to height (2 vs
        # 3 lines), not only width.
        self._update_header_lines()
        self._sync_columns()
        # A height-only resize rebuilds nothing, but the viewport may have
        # shrunk under the cursor (SPEC.md "Main view": a resize scrolls the
        # selected row into view). Here, not in `_sync_columns`: ticks call
        # that too, and a tick must never scroll a wheel-scrolled view.
        table = self._table()
        table.move_cursor(table.cursor_row, scroll=True)
        self._update_footer()

    def _table(self) -> RowTable:
        return self.query_one("#table", RowTable)

    # --- collection tick ----------------------------------------------------

    def refresh_now(self, *, scroll: bool = False) -> None:
        """Collect and redraw immediately, instead of waiting for the next tick.

        Called on mount, on screen resume, and by the actions (`x`, `z`) that
        need their effect to show up right away rather than after a full
        interval. Also the deterministic re-tick hook the Textual pilot tests
        use instead of racing the real timer (SPEC.md "Tests"). Always
        synchronous, on the calling thread -- unlike the periodic timer tick
        (`_tick`), which reads in a thread so a slow collector never blocks
        key handling (SPEC.md "Tech").

        `scroll` (default `False`, a tick) says whether restoring the
        cursor's row is also allowed to scroll the viewport: a periodic tick
        must not, or mouse-wheel scrolling would snap back to the cursor on
        every refresh; an explicit user action (a sort) passes `True` so the
        selected row stays visible after it moves (SPEC.md "Main view").

        Only the collector reads are guarded: a transient OS-level read
        failure or a parse error from a half-written `/proc`/`/sys` file
        (`OSError`/`ValueError`, same treatment as `MemoryStatUnavailableError`)
        skips this tick and keeps the last frame, the next tick recovers
        (SPEC.md "Behaviour details"). Applying the result to the screen runs
        outside the `try`, so a programming error there (e.g. `RowTable.
        reorder`'s `ValueError` invariant check) still propagates and ends
        the session, instead of being swallowed alongside a transient read
        failure.
        """
        try:
            stats = self._backend.read_system()
            apps = self._collect_apps_now()
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
            return
        except MemoryStatUnavailableError:
            return  # transient this tick: keep the last data on screen, try again next tick
        except (OSError, ValueError):
            return  # transient read/parse failure: same treatment, try again next tick
        self._apply_refresh(stats, apps, scroll=scroll)

    def _collect_apps_now(self) -> list[AppStats]:
        """Backend collection with the PROCS cadence applied (SPEC.md "Main
        view"): the synchronous counterpart of the periodic tick's
        `_tick`/`_tick_worker` path, shared by `refresh_now` and
        `action_reset_delta` (`b`). An exception from `collect_apps`
        propagates before `_procs_by_unit` is updated, so a failed collection
        leaves the carried-forward counts exactly as a skipped tick would."""
        count_procs = self._next_count_procs()
        apps, self._procs_by_unit = self._backend.collect_apps(
            include_system=self._show_system,
            strict=False,
            count_procs=count_procs,
            previous_procs=self._procs_by_unit,
        )
        return apps

    def _apply_refresh(self, stats: SystemStats, apps: list[AppStats], *, scroll: bool) -> None:
        self._last_apps = apps
        self._baseline = update_baseline(apps, self._baseline)
        young_now = self._baseline_age() < 60
        if young_now != self._young_baseline:
            self._delta_restyle_pending = True
        self._young_baseline = young_now
        self._apply_rows(build_rows(apps, self._baseline), scroll=scroll)
        self._update_header(stats)

    def _fail_cgroup_unavailable(self, exc: CgroupUnavailableError) -> None:
        # No traceback, exit 1, JSON line after the terminal is restored
        # (SPEC.md "Errors").
        if self._timer is not None:
            self._timer.stop()
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve to `AppMemApp` (SPEC.md "Tech" notes).
        self.app.fail_cgroup_unavailable(  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
            str(exc)
        )

    def _baseline_age(self) -> float:
        return (datetime.now() - self._baseline_time).total_seconds()

    def _update_header(self, stats: SystemStats) -> None:
        self._last_stats = stats
        now, wall = time.monotonic(), time.time()
        self._writeback_history, self._writeback_rate = update_rate(
            self._writeback_history,
            now,
            stats.zswap_writeback_bytes,
            wall=wall,
            interval=self._interval,
        )
        self._swap_in_history, self._swap_in_rate = update_rate(
            self._swap_in_history, now, stats.swap_in_bytes, wall=wall, interval=self._interval
        )
        self._swap_out_history, self._swap_out_rate = update_rate(
            self._swap_out_history, now, stats.swap_out_bytes, wall=wall, interval=self._interval
        )
        if stats.zswap_enabled != self._zswap_enabled:
            # A footer item (and `w`'s effect) appears or disappears with it:
            # rare in practice (zswap is a machine fact, not a per-tick one),
            # but cheap to keep in step rather than assume it once at mount.
            self._zswap_enabled = stats.zswap_enabled
            if not self._zswap_enabled:
                # The column's data source just vanished, and `w` no longer
                # does anything to hide it by hand -- same fallback as hiding
                # it explicitly (`action_toggle_zswap`).
                self._show_zswap = False
            shown = self._show_zswap_column()
            if shown != self._zswap_column_shown:
                self._zswap_column_shown = shown
                if not shown and self._sort_key == "zswap":
                    self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
                self._rebuild_table(scroll=False)  # automatic, not a user selection
            # After `_zswap_column_shown`: the sort item's `z` cap follows it.
            self._update_footer()
        self._update_header_lines()

    def _theme_colors(self) -> ThemeColors:
        theme = self.app.current_theme  # pyright: ignore[reportUnknownMemberType]
        # Every built-in theme sets success/warning/error (checked against
        # `textual.theme.BUILTIN_THEMES` directly); the fallback is only for
        # `Theme.success`/`warning`/`error`'s nominal `str | None` type.
        return ThemeColors(
            success=_rich_color(theme.success or "green"),
            warning=_rich_color(theme.warning or "yellow"),
            error=_rich_color(theme.error or "red"),
            primary=_bar_fill_colour(theme),
        )

    _HEADER_IDS: ClassVar[tuple[str, ...]] = ("#header1", "#header2", "#header3", "#header4")

    def _update_header_lines(self) -> None:
        if self._last_stats is None:
            return
        lines = render_header(
            self._last_stats,
            self.app.size.width,  # pyright: ignore[reportUnknownMemberType]
            self.app.size.height,  # pyright: ignore[reportUnknownMemberType]
            colors=self._theme_colors(),
            baseline_time=self._baseline_time,
            now=datetime.now(),
            writeback_rate=self._writeback_rate,
            swap_in_rate=self._swap_in_rate,
            swap_out_rate=self._swap_out_rate,
            ascii_bars=self._ascii_bars,
        )
        for widget_id, content in zip(self._HEADER_IDS, lines, strict=False):
            widget = self.query_one(widget_id, Static)
            if widget.content != content:
                widget.update(content)
            widget.display = True
        # `render_header` returns 2 lines below H=18: hide the unused third
        # widget so the table gets its row back (`MainScreen #table {
        # height: 1fr }`), instead of leaving an empty line on screen.
        for widget_id in self._HEADER_IDS[len(lines) :]:
            self.query_one(widget_id, Static).display = False

    def refresh_theme(self) -> None:
        """Re-render the header right away after an in-app theme change
        (SPEC.md "Main view": "every colour appmem sets follows the
        theme"), instead of waiting up to a full interval for the next
        tick. Called by `AppMemApp.watch_theme`."""
        self._update_header_lines()

    def _update_footer(self) -> None:
        widget = self.query_one("#footer", Static)
        content = self._footer_text()
        if widget.content != content:
            widget.update(content)

    # --- columns --------------------------------------------------------------

    def _show_delta_columns(self) -> bool:
        return self.app.size.width >= _NARROW_WIDTH  # pyright: ignore[reportUnknownMemberType]

    def _show_zswap_column(self) -> bool:
        width = self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        return self._zswap_enabled and self._show_zswap and width >= _ZSWAP_MIN_WIDTH

    def _sync_columns(self) -> None:
        """Rebuild the table whenever a resize actually changes the computed
        column widths, not just when ΔSWAP/ΔRAM cross their own visibility
        threshold: `_app_column_width` recomputes the APP column's width on
        every resize, independent of the Δ/ZSWAP thresholds, and a resize
        that only moved APP used to leave the table built for the old width,
        pushing TOTAL past the terminal edge or leaving APP narrower than it
        needs to be (SPEC.md "Main view")."""
        show = self._show_delta_columns()
        if show != self._delta_columns_shown:
            self._delta_columns_shown = show
            # The active sort column can go away with ΔSWAP/ΔRAM: fall back
            # to the default sort instead of an invisible one.
            if not show and self._sort_key in _DELTA_KEYS:
                self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
        show_zswap = self._show_zswap_column()
        if show_zswap != self._zswap_column_shown:
            self._zswap_column_shown = show_zswap
            # Mirrors the Δ columns above: a resize can hide ZSWAP out from
            # under an active sort on it just as `w` or zswap turning off do.
            if not show_zswap and self._sort_key == "zswap":
                self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
        table = self._table()
        widths = tuple(width for _key, _label, width in self._column_specs(table))
        if widths != self._column_widths:
            self._rebuild_table(scroll=False)

    def _column_specs(self, table: RowTable) -> list[tuple[SortKey, str, int | None]]:
        specs = list(_BASE_COLUMNS)
        if self._show_cache:
            specs.append(_CACHE_COLUMN)
        if self._zswap_column_shown:
            specs.append(_ZSWAP_COLUMN)
        specs.append(_TOTAL_COLUMN)
        if self._delta_columns_shown:
            specs.extend(_DELTA_COLUMNS)
        specs.append(_PROCS_COLUMN)
        app_width = self._app_column_width(specs, table)
        if app_width is not None:
            specs[0] = ("app", "APP", app_width)
        return specs

    def _app_column_width(
        self, specs: list[tuple[SortKey, str, int | None]], table: RowTable
    ) -> int | None:
        """Shrink the (otherwise auto-sized) APP column so the fixed-width
        numeric columns after it always stay fully on screen, at every
        width, instead of being pushed past the terminal edge by a long name
        (SPEC.md "Main view"): a single "narrow mode" cutoff doesn't survive
        a new column being added (with ZSWAP on by default, a long name
        pushed PROCS off screen well above the old fixed threshold). `None`
        when the budget is already at least `_APP_MAX_WIDTH`: auto-size,
        capped at `_APP_MAX_WIDTH` by `truncate_name`, same as a dedicated
        width that wide would render. The table's own vertical scrollbar
        (when shown) narrows the usable width too, so it comes out of the
        same budget as the numeric columns."""
        total_width = table.scrollable_content_region.width or self.app.size.width  # pyright: ignore[reportUnknownMemberType]
        other = sum(_CELL_PADDING + (width or 0) for key, _label, width in specs if key != "app")
        budget = total_width - other - _CELL_PADDING
        if budget >= _APP_MAX_WIDTH:
            return None
        return max(budget, _APP_MIN_WIDTH)

    def _app_cap(self, table: RowTable) -> int:
        width = self._column_specs(table)[0][2]
        return width if width is not None else _APP_MAX_WIDTH

    def _header_label(self, key: SortKey, label: str) -> str:
        if key != self._sort_key:
            return label
        marker = "▾" if self._sort_reverse else "▴"
        return f"{label} {marker}"

    def _rebuild_columns(self, table: RowTable) -> None:
        table.clear(columns=True)
        specs = self._column_specs(table)
        for key, label, width in specs:
            table.add_column(self._header_label(key, label), width=width, key=key)
        self._column_widths = tuple(width for _key, _label, width in specs)

    def _refresh_column_labels(self, table: RowTable) -> None:
        for key, label, _width in self._column_specs(table):
            table.set_column_label(key, self._header_label(key, label))

    # --- row diffing ------------------------------------------------------------

    def _cell_value(self, key: SortKey, row: Row, *, app_cap: int) -> Text:
        # Always a `Text`: `RowTable` renders a cell's `.plain` text and base
        # `.style` literally, never through markup parsing, so an app name
        # containing `[bold]`-style brackets shows literally (SPEC.md
        # "Behaviour details").
        text = _format_cell(key, row, app_cap=app_cap)
        if key == "app":
            return Text(text)
        # A Δ cell is dim while its glyph is the small-delta `·`, or while the
        # whole baseline is still under 60 s old.
        dim = key in _DELTA_KEYS and (text == "·" or self._young_baseline)
        return Text(text, justify="right", style="dim" if dim else "")

    def _row_cells(self, row: Row, table: RowTable) -> list[Text]:
        app_cap = self._app_cap(table)
        return [
            self._cell_value(key, row, app_cap=app_cap)
            for key, _label, _width in self._column_specs(table)
        ]

    def _update_row_cells(
        self, table: RowTable, old: Row, row: Row, *, force_delta_restyle: bool
    ) -> None:
        # Only cells whose text changed: each `update_cell` still invalidates
        # that row's cached strip and refreshes its screen line, so a no-op
        # update is not free. Δ cells are the one exception: the baseline crossing 60 s changes
        # their dim style without necessarily changing their text.
        app_cap = self._app_cap(table)
        for key, _label, _width in self._column_specs(table):
            restyle = force_delta_restyle and key in _DELTA_KEYS
            old_text = _format_cell(key, old, app_cap=app_cap)
            new_text = _format_cell(key, row, app_cap=app_cap)
            if restyle or old_text != new_text:
                table.update_cell(
                    row_key(row.name, row.scope), key, self._cell_value(key, row, app_cap=app_cap)
                )

    def _current_selection(self, table: RowTable) -> tuple[str | None, int]:
        index = table.cursor_row
        if table.row_count == 0:
            return None, index
        return table.cursor_key, index

    def _restore_selection(
        self,
        table: RowTable,
        previous_key: str | None,
        previous_index: int,
        *,
        scroll: bool,
    ) -> None:
        if table.row_count == 0:
            return
        if previous_key is not None and previous_key in self._rows:
            table.move_cursor(row=table.get_row_index(previous_key), scroll=scroll)
        else:
            table.move_cursor(row=min(previous_index, table.row_count - 1), scroll=scroll)

    def _resort(self, table: RowTable) -> None:
        ordered = sort_rows(self._rows.values(), self._sort_key, self._sort_reverse)
        table.reorder([row_key(row.name, row.scope) for row in ordered])

    def _apply_rows(self, rows: list[Row], *, scroll: bool) -> None:
        table = self._table()
        new_by_key = {row_key(row.name, row.scope): row for row in rows}
        previous_key, previous_index = self._current_selection(table)
        force_delta_restyle = self._delta_restyle_pending
        self._delta_restyle_pending = False
        row_count_changed = len(new_by_key) != len(self._rows)

        for key in self._rows.keys() - new_by_key.keys():
            table.remove_row(key)
        for key, row in new_by_key.items():
            old = self._rows.get(key)
            if old is not None:
                self._update_row_cells(table, old, row, force_delta_restyle=force_delta_restyle)
            else:
                table.add_row(*self._row_cells(row, table), key=key)
        self._rows = new_by_key

        self._resort(table)
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)
        if row_count_changed:
            # More or fewer rows can show or hide the vertical scrollbar, which
            # narrows the APP budget without any resize event; its size is only
            # known after the next layout.
            self.call_after_refresh(self._sync_columns)

    # --- table rebuilds -----------------------------------------------------------

    def _rebuild_table(self, *, scroll: bool) -> None:
        """Rebuild the table's columns and repopulate its rows after a
        change to which columns are visible (CACHE toggle, Δ columns hidden or
        shown by width), preserving sort and selection."""
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        previous_scroll_y = table.scroll_y
        self._rebuild_columns(table)
        for key, row in self._rows.items():
            table.add_row(*self._row_cells(row, table), key=key)
        self._resort(table)
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)
        if not scroll and previous_scroll_y > 0:
            table.scroll_to(y=previous_scroll_y, animate=False)

    # --- actions ----------------------------------------------------------------

    def _set_sort(self, column: SortKey) -> None:
        self._sort_key, self._sort_reverse = next_sort_state(
            self._sort_key, self._sort_reverse, column
        )
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        self._resort(table)
        self._refresh_column_labels(table)
        # Explicit user action (key or header click): keep the selected row
        # visible even though the resort may have moved it far from where it
        # was on screen (SPEC.md "Main view").
        self._restore_selection(table, previous_key, previous_index, scroll=True)

    def action_sort(self, column: str) -> None:
        if column in _DELTA_KEYS and not self._delta_columns_shown:
            return  # `d` while ΔSWAP is hidden by width: no invisible sort
        if column == "zswap" and not self._zswap_column_shown:
            return  # `z` while ZSWAP is hidden (width, `w`, or zswap off): no invisible sort
        self._set_sort(cast("SortKey", column))

    def on_row_table_header_selected(self, event: RowTable.HeaderSelected) -> None:
        self._set_sort(cast("SortKey", event.column_key))

    def action_toggle_cache(self) -> None:
        self._show_cache = not self._show_cache
        # The active sort column can go away with the CACHE column: fall back
        # to the default sort instead of an invisible one (SPEC.md "Main
        # view").
        if not self._show_cache and self._sort_key == "cache":
            self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
        self._rebuild_table(scroll=True)  # explicit `c` key press

    def action_toggle_zswap(self) -> None:
        if not self._zswap_enabled:
            return  # zswap off or unsupported here: `w` does nothing (SPEC.md "Main view")
        self._show_zswap = not self._show_zswap
        self._zswap_column_shown = self._show_zswap_column()
        # Mirrors `action_toggle_cache`: the active sort column can go away
        # with the ZSWAP column, so fall back instead of an invisible sort.
        if not self._zswap_column_shown and self._sort_key == "zswap":
            self._sort_key, self._sort_reverse = DEFAULT_SORT_KEY, DEFAULT_SORT_REVERSE
        self._rebuild_table(scroll=True)  # explicit `w` key press
        self._update_footer()  # the sort item's own `z` key cap comes and goes with the column

    def action_toggle_system(self) -> None:
        self._show_system = not self._show_system
        self._generation += 1
        self.refresh_now(scroll=True)  # explicit `x` key press

    def action_reset_delta(self) -> None:
        # `b` takes a fresh sample before resetting, so the baseline and its
        # timestamp describe the same instant (SPEC.md "Definitions").
        try:
            apps = self._collect_apps_now()
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
            return
        except MemoryStatUnavailableError:
            return  # transient this tick: leave the baseline untouched, try again next tick
        except (OSError, ValueError):
            return  # transient read/parse failure: same treatment, try again next tick
        self._last_apps = apps
        self._baseline = reset_baseline(apps)
        self._baseline_time = datetime.now()
        if not self._young_baseline:
            self._delta_restyle_pending = True
        self._young_baseline = True
        self._apply_rows(build_rows(apps, self._baseline), scroll=True)  # explicit `b` key press
        self._update_header_lines()  # the Δ baseline time is on the Pressure line

    def action_help(self) -> None:
        # Textual's `Screen.app` is typed from a contextvar pyright can't fully
        # resolve; the call itself is fine (SPEC.md "Tech" notes).
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            HelpScreen(zswap_enabled=self._zswap_enabled)
        )

    def on_row_table_row_selected(self, event: RowTable.RowSelected) -> None:
        key = event.row_key
        app = next((app for app in self._last_apps if row_key(app.name, app.scope) == key), None)
        if app is None:  # row vanished between the click and the event
            return
        self._resume_scrolls = True
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ProcessesScreen(
                backend=self._backend,
                name=app.name,
                scope=app.scope,
                interval=self._interval,
                initial_sort=initial_process_sort(self._sort_key, self._sort_reverse),
            )
        )
