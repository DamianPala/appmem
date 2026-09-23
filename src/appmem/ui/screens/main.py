"""Main view: the live, sortable per-app table (SPEC.md "Main view").

Refreshes on a `set_interval` timer, whose tick reads in a thread worker
(`run_worker(thread=True)`) rather than on the event loop: the collector is
normally fast (~7 ms on the dev machine, SPEC.md "Tech" budget: under 1 % of
one core at 1 s), but a slow filesystem or a big tree can still make one read
take much longer, and a blocking read on the event loop would freeze key
handling for its whole duration (SPEC.md "Tech"). `refresh_now` stays
synchronous, for mount, explicit actions and tests. Existing rows are updated
in place and only appeared/vanished apps add or remove a row, so a refresh
never rebuilds the table (SPEC.md "Tech" notes).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import ClassVar, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.color import Color
from textual.screen import Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey, RowKey
from textual.worker import Worker, WorkerState

from appmem.collect import (
    AppStats,
    CgroupUnavailableError,
    MemoryStatUnavailableError,
    SystemStats,
    Unit,
    filter_visible_apps,
    group_apps,
    read_system,
    unit_scope,
)
from appmem.collect import find_units as collect_find_units
from appmem.collect import read_unit as collect_read_unit
from appmem.fmt import format_delta, size, truncate_name
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
from appmem.ui.screens.processes import ProcessesScreen
from appmem.ui.table_order import reorder_rows
from appmem.writeback import Sample, update_writeback

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
# `DataTable`'s default `cell_padding` (1 cell each side of every column).
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
_PROCS_COLUMN: tuple[SortKey, str, int | None] = ("procs", "PROCS", 6)
_DELTA_KEYS = frozenset({"delta_swap", "delta_ram"})

_TICK_GROUP = "collect"


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


def _key_str(key: RowKey | ColumnKey) -> str:
    assert key.value is not None
    return key.value


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


def _collect_apps(root: Path, uid: int, include_system: bool) -> list[AppStats]:
    """Read every visible app's counters (SPEC.md "Grouping"). Pure and
    thread-safe (no Textual/UI state touched): shared by the synchronous
    `refresh_now` path and the threaded periodic tick (SPEC.md "Tech")."""
    unit_paths = collect_find_units(root, uid, include_system=include_system, strict=False)
    units = [
        Unit(path=path, stats=unit_stats, scope=unit_scope(root, path))
        for path in unit_paths
        if (unit_stats := collect_read_unit(path)) is not None
    ]
    return filter_visible_apps(group_apps(units))


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


def _tick_worker(root: Path, uid: int, include_system: bool, generation: int) -> _TickResult:
    """Runs in a thread (SPEC.md "Tech"): blocking `/proc`/`/sys` reads
    only, no Textual calls. Transient errors are swallowed here, same as
    `refresh_now`'s `try` -- the tick is skipped, not the session."""
    try:
        stats = read_system(root, uid)
        apps = _collect_apps(root, uid, include_system)
    except CgroupUnavailableError as exc:
        return _TickResult(generation=generation, cgroup_error=exc)
    except (MemoryStatUnavailableError, OSError, ValueError):
        return _TickResult(generation=generation)
    return _TickResult(generation=generation, stats=stats, apps=apps)


class MainScreen(Screen[None]):
    """The per-app table: header lines, the DataTable, and the footer (SPEC.md)."""

    # DataTable defaults to `height: auto; max-height: 100%`, which with many rows
    # makes the screen taller than the terminal and scrolls both header lines and
    # the footer out of view. `1fr` gives the table only the space left over.
    DEFAULT_CSS = """
    MainScreen #table { height: 1fr; }
    MainScreen #header1, MainScreen #header2, MainScreen #header3, MainScreen #footer {
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

    def __init__(self, *, root: Path, uid: int, interval: float, include_system: bool) -> None:
        super().__init__()
        self._root = root
        self._uid = uid
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
        self._sort_key: SortKey = DEFAULT_SORT_KEY
        self._sort_reverse = DEFAULT_SORT_REVERSE
        self._baseline: dict[tuple[str, str], AppStats] = {}
        self._baseline_time = datetime.now()
        self._rows: dict[str, Row] = {}
        self._last_apps: list[AppStats] = []
        self._timer: Timer | None = None
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
        self._tick_in_flight = False
        self._column_widths: tuple[int | None, ...] = ()
        self._ascii_bars = _detect_ascii_bars()
        self._generation = 0
        """Bumped on every context change (`x` toggled, screen covered or
        resumed). A background result carries the generation it was read
        under; `on_worker_state_changed` discards one that no longer
        matches."""

    def compose(self) -> ComposeResult:
        self._delta_columns_shown = self._show_delta_columns()
        self._zswap_column_shown = self._show_zswap_column()
        yield Static(id="header1")
        yield Static(id="header2")
        yield Static(id="header3")
        table: DataTable[str | Text] = DataTable(id="table", cursor_type="row")
        self._rebuild_columns(table)
        yield table
        yield Static(self._footer_text(), id="footer")

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
        if self._zswap_enabled:  # `w` does nothing when zswap is off or unsupported
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
        self.refresh_now()
        self._timer = self.set_interval(self._interval, self._tick)

    def _tick(self) -> None:
        # Covered by the process view or help: skip the work, `on_screen_resume` catches up.
        # A previous tick's read is still in flight: skip, don't stack a second one
        # (SPEC.md "Tech": at most one read in flight per screen).
        if not self.is_active or self._tick_in_flight:
            return
        self._tick_in_flight = True
        self.run_worker(
            partial(_tick_worker, self._root, self._uid, self._show_system, self._generation),
            thread=True,
            exclusive=False,
            group=_TICK_GROUP,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # Blocking `/proc`/`/sys` reads happen in `_tick_worker`, off the event
        # loop, so a slow collector never blocks key handling (SPEC.md "Tech").
        # This applies the result back on the UI thread, the only thread
        # allowed to touch widgets.
        if event.worker.group != _TICK_GROUP:  # pyright: ignore[reportUnknownMemberType]
            return
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        self._tick_in_flight = False
        if event.state != WorkerState.SUCCESS:
            return
        # `Worker` (Textual's own type) is an unparameterized generic here, so
        # its `.result` is `Unknown` to pyright; `_tick_worker`'s own return
        # type is the real source of truth for what this cast recovers.
        result = cast(_TickResult, event.worker.result)  # pyright: ignore[reportUnknownMemberType]
        if result.cgroup_error is not None:
            self._fail_cgroup_unavailable(result.cgroup_error)
            return
        if result.stats is None:  # transient read/parse failure this tick: keep the last frame
            return
        if result.generation != self._generation or not self.is_active:
            # `x` toggled (or the screen was covered/left) while this read was
            # in flight: its result no longer matches the current context,
            # discard it rather than show a stale table (SPEC.md "Tech").
            return
        self._apply_refresh(result.stats, result.apps or [], scroll=False)

    def on_screen_resume(self) -> None:
        # A read dispatched before the covering screen closed is now stale,
        # even if nothing we track here actually changed while it was up.
        self._generation += 1
        self.refresh_now()  # no stale numbers after Esc from the process view

    def on_resize(self, event: events.Resize) -> None:
        # Header, columns and footer never wrap -- recompute on every resize,
        # not just on the next tick. The header also reacts to height (2 vs
        # 3 lines), not only width.
        self._update_header_lines()
        self._sync_columns()
        self._update_footer()

    def _table(self) -> DataTable[str | Text]:
        # `isinstance()` (which `query_one` uses) rejects a parameterized generic,
        # so we query by the bare class and `cast` to the concrete cell type.
        return cast("DataTable[str | Text]", self.query_one("#table", DataTable))

    # --- collection tick ----------------------------------------------------

    def refresh_now(self, *, scroll: bool = False) -> None:
        """Collect and redraw immediately, instead of waiting for the next tick.

        Called on mount, on screen resume, and by the actions (`x`, `z`) that
        need their effect to show up right away rather than after a full
        interval. Also the deterministic re-tick hook the Textual pilot tests
        use instead of racing the real timer (SPEC.md "Tests"). Always
        synchronous, on the calling thread -- unlike the periodic timer tick
        (`_tick`), which reads in a thread worker so a slow collector never
        blocks key handling (SPEC.md "Tech").

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
        outside the `try`, so a programming error there (e.g. `reorder_rows`'s
        `ValueError` invariant check) still propagates and ends the session,
        instead of being swallowed alongside a transient read failure.
        """
        try:
            stats = read_system(self._root, self._uid)
            apps = _collect_apps(self._root, self._uid, self._show_system)
        except CgroupUnavailableError as exc:
            self._fail_cgroup_unavailable(exc)
            return
        except MemoryStatUnavailableError:
            return  # transient this tick: keep the last data on screen, try again next tick
        except (OSError, ValueError):
            return  # transient read/parse failure: same treatment, try again next tick
        self._apply_refresh(stats, apps, scroll=scroll)

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
        self._writeback_history, self._writeback_rate = update_writeback(
            self._writeback_history, time.monotonic(), stats.zswap_writeback_bytes
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

    _HEADER_IDS: ClassVar[tuple[str, ...]] = ("#header1", "#header2", "#header3")

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
            self._rebuild_table(scroll=False)  # a resize, not an explicit selection action

    def _column_specs(self, table: DataTable[str | Text]) -> list[tuple[SortKey, str, int | None]]:
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
        self, specs: list[tuple[SortKey, str, int | None]], table: DataTable[str | Text]
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
        total_width = (
            self.app.size.width - table.scrollbar_size_vertical  # pyright: ignore[reportUnknownMemberType]
        )
        other = sum(_CELL_PADDING + (width or 0) for key, _label, width in specs if key != "app")
        budget = total_width - other - _CELL_PADDING
        if budget >= _APP_MAX_WIDTH:
            return None
        return max(budget, _APP_MIN_WIDTH)

    def _app_cap(self, table: DataTable[str | Text]) -> int:
        width = self._column_specs(table)[0][2]
        return width if width is not None else _APP_MAX_WIDTH

    def _header_label(self, key: SortKey, label: str) -> str:
        if key != self._sort_key:
            return label
        marker = "▾" if self._sort_reverse else "▴"
        return f"{label} {marker}"

    def _rebuild_columns(self, table: DataTable[str | Text]) -> None:
        table.clear(columns=True)
        specs = self._column_specs(table)
        for key, label, width in specs:
            table.add_column(self._header_label(key, label), width=width, key=key)
        self._column_widths = tuple(width for _key, _label, width in specs)

    def _refresh_column_labels(self, table: DataTable[str | Text]) -> None:
        for key, label, _width in self._column_specs(table):
            table.columns[ColumnKey(key)].label = Text(self._header_label(key, label))
        table.refresh()

    # --- row diffing ------------------------------------------------------------

    def _cell_value(self, key: SortKey, row: Row, *, app_cap: int) -> Text:
        # Always a literal `Text`, never a plain `str`: `DataTable` renders a `str`
        # cell through `Text.from_markup`, so an app name containing `[bold]`-style
        # brackets would otherwise be parsed as markup instead of shown literally
        # (SPEC.md "Behaviour details").
        text = _format_cell(key, row, app_cap=app_cap)
        if key == "app":
            return Text(text)
        # A Δ cell is dim while its glyph is the small-delta `·`, or while the
        # whole baseline is still under 60 s old.
        dim = key in _DELTA_KEYS and (text == "·" or self._young_baseline)
        return Text(text, justify="right", style="dim" if dim else "")

    def _row_cells(self, row: Row, table: DataTable[str | Text]) -> list[str | Text]:
        app_cap = self._app_cap(table)
        return [
            self._cell_value(key, row, app_cap=app_cap)
            for key, _label, _width in self._column_specs(table)
        ]

    def _update_row_cells(
        self, table: DataTable[str | Text], old: Row, row: Row, *, force_delta_restyle: bool
    ) -> None:
        # Only cells whose text changed: any `update_cell` invalidates the whole
        # DataTable render cache, so a no-op update still repaints every row.
        # Δ cells are the one exception: the baseline crossing 60 s changes
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

    def _current_selection(self, table: DataTable[str | Text]) -> tuple[str | None, int]:
        index = table.cursor_row
        if table.row_count == 0:
            return None, index
        row_key_obj, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        return _key_str(row_key_obj), index

    def _restore_selection(
        self,
        table: DataTable[str | Text],
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

    def _resort(self, table: DataTable[str | Text]) -> None:
        ordered = sort_rows(self._rows.values(), self._sort_key, self._sort_reverse)
        reorder_rows(table, [row_key(row.name, row.scope) for row in ordered])

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
        """Rebuild the DataTable's columns and repopulate its rows after a
        change to which columns are visible (CACHE toggle, Δ columns hidden or
        shown by width), preserving sort and selection."""
        table = self._table()
        previous_key, previous_index = self._current_selection(table)
        self._rebuild_columns(table)
        for key, row in self._rows.items():
            table.add_row(*self._row_cells(row, table), key=key)
        self._resort(table)
        self._restore_selection(table, previous_key, previous_index, scroll=scroll)

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

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        self._set_sort(cast("SortKey", _key_str(event.column_key)))

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
            apps = _collect_apps(self._root, self._uid, self._show_system)
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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = _key_str(event.row_key)
        app = next((app for app in self._last_apps if row_key(app.name, app.scope) == key), None)
        if app is None:  # row vanished between the click and the event
            return
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            ProcessesScreen(
                root=self._root,
                uid=self._uid,
                include_system=self._show_system,
                name=app.name,
                scope=app.scope,
                interval=self._interval,
                initial_sort=initial_process_sort(self._sort_key, self._sort_reverse),
            )
        )
