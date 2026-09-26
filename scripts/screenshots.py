"""Regenerate the README screenshots (`docs/screenshots/*.svg`) from fixture data.

Dev script, not part of the installed package: `uv run python scripts/screenshots.py
[OUT_DIR]` (default `docs/screenshots/`). Never touches the real `/proc`, `/sys` or
`~/.config`: it builds a throwaway cgroup-v2/proc-like tree with the same fixture
builders the test suite uses (`tests/helpers.py`), points a headless `AppMemApp` at
it, and saves three SVGs. Every app, process, command line and number below is
invented -- no real paths, usernames or hostnames.

Deterministic: the header's `Δ since HH:MM (...)` is the only wall-clock read
(`datetime.now()` in `ui.screens.main`, patched here to a fixed instant), so a
rerun produces byte-identical SVGs apart from Rich's own per-export random
`terminal-<N>` id in each file, unrelated to content.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))  # tests/helpers.py: no second fixture builder

from textual.widgets import OptionList

from appmem.collect import LinuxBackend
from appmem.theme import THEME_NAMES
from appmem.ui.app import AppMemApp
from appmem.ui.rows import row_key
from appmem.ui.screens.main import MainScreen
from appmem.ui.table import RowTable
from appmem.ui.theme_picker import ThemePanel
from helpers import (
    make_unit,
    user_service_root,
    write_meminfo,
    write_memory_stat,
    write_pressure,
    write_proc,
    write_uptime,
    write_vmstat,
    write_zswap_enabled,
    write_zswap_params,
)

_UID = 1000
_GIB = 1024**3
# Each screen sized to its own content plus a couple of spare rows, not the
# tallest screen shared by all three: a fixed 120x32 left ~16 blank rows below
# main's table and even more below processes'.
_MAIN_SIZE = (120, 20)
_PROCESSES_SIZE = (120, 16)
# A few columns wider than the other two: at exactly 120 the panel, docked
# over the main view's right edge, covers the PROCS column's digits. 28 rows is the panel's own
# content: border, "Theme", 21 themes, the blank info line, "↑↓ preview" and
# "enter keep esc cancel", closing border -- one row taller would just add
# blank padding above the bottom lines.
_THEME_PANEL_SIZE = (130, 28)
_NO_AUTO_REFRESH_INTERVAL = 100.0  # higher than any capture takes: the timer never fires
_UPTIME_SECONDS = 300_000.0  # ~3.5 days: a desktop session that's been running a while
_CLK_TCK = 100  # os.sysconf("SC_CLK_TCK") is fixed at 100 on Linux regardless of kernel HZ
_PREVIEW_THEME = "dracula"
_CLOCK_BASE = datetime(2026, 9, 23, 9, 41, 0)  # header's own baseline instant
_CLOCK_ELAPSED = timedelta(minutes=12)  # -> "Δ since 09:41 (12m)", never "(0s)"


def _gib(value: float) -> int:
    return round(value * _GIB)


@dataclass(frozen=True)
class _ProcSpec:
    pid: int
    comm: str
    cmdline: str
    ram_mib: int
    swap_mib: int
    age_seconds: float


# Several `claude` processes, two `node:<script>`-labelled MCP servers and a
# `python:<pkg>`-labelled one (`command_name.py`), so both `g` grouping and
# interpreter labels show in `processes.svg`. Ages vary so the process view's
# AGE column isn't a wall of identical values. The processes add up to all but
# ~0.7 GiB RAM / ~0.8 GiB swap of `_GHOSTTY`, so `unattributed` stays modest
# instead of reading like a bug.
_GHOSTTY_PROCS: tuple[_ProcSpec, ...] = (
    _ProcSpec(40001, "ghostty", "/usr/bin/ghostty\x00", 700, 500, _UPTIME_SECONDS),
    _ProcSpec(40002, "bash", "-bash\x00", 10, 4, _UPTIME_SECONDS),
    _ProcSpec(40003, "claude", "claude\x00", 1700, 3900, 21_600.0),
    _ProcSpec(40004, "claude", "claude\x00", 1500, 3500, 10_800.0),
    _ProcSpec(40005, "claude", "claude\x00", 1350, 3000, 2_700.0),
    _ProcSpec(
        40006,
        "node",
        "node\x00/home/user/.cache/mcp-remote/dist/index.js\x00",
        260,
        1024,
        _UPTIME_SECONDS,
    ),
    _ProcSpec(
        40007,
        "node",
        "node\x00/home/user/.npm/_npx/6f8a1c/node_modules/mcp-remote/dist/index.js\x00",
        230,
        870,
        7_200.0,
    ),
    _ProcSpec(40008, "python", "python\x00-m\x00http.server\x00", 180, 717, 1_200.0),
)


@dataclass(frozen=True)
class _AppSpec:
    """One `app.slice`/system-scope unit, sizes in GiB."""

    unit_name: str
    anon_gib: float
    swap_gib: float
    cache_gib: float = 0.0
    zswapped_gib: float = 0.0
    kernel_gib: float = 0.0
    zswap_pool_gib: float = 0.0
    procs: int = 1
    pid_start: int = 1


# A believable ~31 GiB RAM / 32 GiB swap desktop with zswap on: a terminal doing
# real work, two browsers, an editor, the desktop shell, an office suite and a
# few smaller apps. Names picked so `naming.app_name` decodes them the same way
# real systemd unit names would (aliases and reverse-DNS collapsing included).
_GHOSTTY = _AppSpec(
    "app-ghostty-surface-transient-48213.scope",
    anon_gib=6.2,
    swap_gib=14.0,
    cache_gib=0.3,
    zswapped_gib=8.0,
    kernel_gib=0.4,
    zswap_pool_gib=0.12,
    procs=len(_GHOSTTY_PROCS),
    pid_start=_GHOSTTY_PROCS[0].pid,
)
_CHROME = _AppSpec(
    "app-google-chrome-51402.scope",
    anon_gib=3.8,
    swap_gib=4.0,
    cache_gib=0.4,
    zswapped_gib=2.5,
    procs=42,
    pid_start=41001,
)
_APPS: tuple[_AppSpec, ...] = (
    _GHOSTTY,
    _CHROME,
    _AppSpec(
        "app-brave-browser-52117.scope",
        anon_gib=2.1,
        swap_gib=2.2,
        cache_gib=0.2,
        zswapped_gib=1.3,
        procs=28,
        pid_start=42001,
    ),
    _AppSpec(
        "app-code-53008.scope",
        anon_gib=1.6,
        swap_gib=2.0,
        cache_gib=0.1,
        zswapped_gib=1.1,
        procs=24,
        pid_start=43001,
    ),
    _AppSpec(
        "plasma-plasmashell.service",
        anon_gib=1.3,
        swap_gib=0.8,
        cache_gib=0.05,
        zswapped_gib=0.5,
        procs=19,
        pid_start=44001,
    ),
    _AppSpec(
        "app-thunderbird-54321.scope",
        anon_gib=0.9,
        swap_gib=0.6,
        cache_gib=0.05,
        zswapped_gib=0.35,
        procs=11,
        pid_start=45001,
    ),
    _AppSpec(
        "app-libreoffice-writer-55555.scope",
        anon_gib=0.6,
        swap_gib=0.3,
        cache_gib=0.03,
        zswapped_gib=0.15,
        procs=6,
        pid_start=46001,
    ),
    _AppSpec(
        "app-org.kde.dolphin-56789.scope",
        anon_gib=0.35,
        swap_gib=0.1,
        cache_gib=0.02,
        procs=4,
        pid_start=47001,
    ),
    _AppSpec(
        "app-org.kde.kate-57002.scope",
        anon_gib=0.15,
        swap_gib=0.04,
        cache_gib=0.01,
        procs=2,
        pid_start=48001,
    ),
    _AppSpec(
        "app-signal-desktop-58110.scope",
        anon_gib=0.25,
        swap_gib=0.1,
        cache_gib=0.01,
        procs=3,
        pid_start=49001,
    ),
    _AppSpec(
        "app-org.kde.okular-59044.scope",
        anon_gib=0.12,
        swap_gib=0.02,
        cache_gib=0.005,
        procs=2,
        pid_start=50001,
    ),
)


def _write_app_unit(root: Path, spec: _AppSpec) -> None:
    unit_dir = user_service_root(root, _UID) / "app.slice" / spec.unit_name
    make_unit(
        unit_dir,
        anon=_gib(spec.anon_gib),
        kernel=_gib(spec.kernel_gib),
        file=_gib(spec.cache_gib),
        zswapped=_gib(spec.zswapped_gib),
        zswap=_gib(spec.zswap_pool_gib) if spec.zswap_pool_gib else None,
        swap=_gib(spec.swap_gib),
        pids=list(range(spec.pid_start, spec.pid_start + spec.procs)),
    )


def _write_system_service(root: Path) -> None:
    # A modest system.slice unit, just so the Pressure line's "system X [x]"
    # token shows a plausible number instead of "0 B" (SPEC.md "Main view").
    # `read_system` reads system.slice's own memory.stat directly, not a sum
    # of its children (real cgroup v2 keeps that file cumulative for the
    # whole subtree), so the slice dir itself needs the same figures too.
    slice_dir = root / "sys" / "fs" / "cgroup" / "system.slice"
    make_unit(slice_dir, anon=_gib(0.18), swap=0, pids=[])
    unit_dir = slice_dir / "NetworkManager.service"
    make_unit(unit_dir, anon=_gib(0.18), swap=0, pids=[900])


def _write_system_stats(root: Path) -> None:
    write_meminfo(
        root,
        mem_total_kb=32_505_856,  # 31 GiB
        mem_available_kb=13_841_203,  # 13.2 GiB
        swap_total_kb=33_554_432,  # 32 GiB
        swap_free_kb=8_178_892,  # 7.8 GiB -> ~24.2 GiB used
        mem_free_kb=7_864_320,  # 7.5 GiB
        cached_kb=7_549_747,  # 7.2 GiB raw (4.2 GiB cache once Shmem is split out)
        shmem_kb=3_145_728,  # 3.0 GiB
        sreclaimable_kb=1_572_864,  # 1.5 GiB slab
        zswap_kb=3_774_874,  # 3.6 GiB: the pool's own RAM cost
        zswapped_kb=15_204_352,  # 14.5 GiB: data kept compressed in the pool
    )
    write_zswap_enabled(root, enabled=True)
    write_zswap_params(root, compressor="zstd", max_pool_percent=20)
    write_vmstat(root, zswpwb=None)  # the pool isn't overflowing: no "to disk" marker
    # Pressure `none`: a README screenshot shouldn't look like a crisis.
    write_pressure(root, some_avg10=0.0, full_avg10=0.0, some_avg60=0.0, full_avg60=0.0)
    write_uptime(root, seconds=_UPTIME_SECONDS)
    _write_system_service(root)


def _write_ghostty_processes(root: Path) -> None:
    for spec in _GHOSTTY_PROCS:
        starttime_ticks = round((_UPTIME_SECONDS - spec.age_seconds) * _CLK_TCK)
        write_proc(
            root,
            spec.pid,
            cmdline=spec.cmdline,
            comm=spec.comm,
            vm_swap_kb=spec.swap_mib * 1024,
            rss_anon_kb=spec.ram_mib * 1024,
            starttime_ticks=starttime_ticks,
        )


def _build_fixture(root: Path) -> None:
    write_memory_stat(user_service_root(root, _UID))  # the user session's own cgroup
    _write_system_stats(root)
    for spec in _APPS:
        _write_app_unit(root, spec)
    _write_ghostty_processes(root)


def _bump_a_few_values(root: Path) -> None:
    """Move two apps' numbers before the second tick, so ΔRAM/ΔSWAP read
    non-zero in `main.svg` instead of `·` (SPEC.md "Main view")."""
    _write_app_unit(root, replace(_GHOSTTY, anon_gib=_GHOSTTY.anon_gib + 300 / 1024))
    _write_app_unit(root, replace(_CHROME, swap_gib=_CHROME.swap_gib - 200 / 1024))


class _FrozenClock:
    """Stands in for the `datetime` name in `ui.screens.main`'s own module
    namespace (patched in `_capture`, not touching `appmem` itself): that
    module's only two calls are `datetime.now()` for the Δ baseline (once, at
    `MainScreen.__init__`) and for each header render's `now`. Returning a
    fixed instant for the first call and a fixed instant 12 minutes later for
    every call after makes the header read a stable `Δ since 09:41 (12m)`
    instead of the real wall clock and a `(0s)` young baseline, and makes a
    rerun byte-identical."""

    def __init__(self) -> None:
        self._calls = 0

    def now(self) -> datetime:
        self._calls += 1
        return _CLOCK_BASE if self._calls == 1 else _CLOCK_BASE + _CLOCK_ELAPSED


async def _capture(root: Path, out_dir: Path) -> None:
    app = AppMemApp(
        backend=LinuxBackend(root, _UID), interval=_NO_AUTO_REFRESH_INTERVAL, include_system=False
    )
    with patch("appmem.ui.screens.main.datetime", new=_FrozenClock()):
        async with app.run_test(size=_MAIN_SIZE) as pilot:
            await pilot.pause()
            screen = pilot.app.screen
            assert isinstance(screen, MainScreen)

            _bump_a_few_values(root)
            screen.refresh_now()
            await pilot.pause()
            app.save_screenshot("main.svg", path=str(out_dir))

            table = pilot.app.query_one(RowTable)
            table.move_cursor(row=table.get_row_index(row_key("ghostty", "user")))
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("g")  # group by command: several `claude`/`node:*` rows collapse
            await pilot.pause()
            # Smaller than `_MAIN_SIZE`: the process view's own content (one
            # summary line, the table, the kernel/zswap pool/unattributed
            # rows) is shorter than the main view's app table.
            await pilot.resize_terminal(*_PROCESSES_SIZE)
            await pilot.pause()
            app.save_screenshot("processes.svg", path=str(out_dir))

            await pilot.press("escape")
            await pilot.pause()
            # Wider, so the docked panel doesn't cover the PROCS column.
            await pilot.resize_terminal(*_THEME_PANEL_SIZE)
            await pilot.pause()
            await pilot.press("T")
            await pilot.pause()
            theme_screen = pilot.app.screen
            assert isinstance(theme_screen, ThemePanel)
            option_list = theme_screen.query_one(OptionList)
            option_list.highlighted = THEME_NAMES.index(_PREVIEW_THEME)  # live preview, not Enter
            await pilot.pause()
            app.save_screenshot("theme-panel.svg", path=str(out_dir))


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else _REPO_ROOT / "docs" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="appmem-screenshots-") as tmp_name:
        root = Path(tmp_name)
        os.environ["XDG_CONFIG_HOME"] = str(root / "xdg-config")  # never the real ~/.config
        # The caller's NO_COLOR or a non-UTF-8 locale would turn the SVGs
        # monochrome or give the gauges ASCII bars.
        os.environ.pop("NO_COLOR", None)
        os.environ["LC_ALL"] = "C.UTF-8"
        _build_fixture(root)
        asyncio.run(_capture(root, out_dir))
    print(f"wrote {out_dir / 'main.svg'}, processes.svg, theme-panel.svg")


if __name__ == "__main__":
    main()
