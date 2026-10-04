# appmem: spec v0.23

A live terminal view of memory usage **per application**, not per process, on Linux and on Apple Silicon macOS.
Think `btm` or `htop`, but rows are apps (Ghostty, Brave, LibreOffice), each summing all of its processes.

## Why

`htop`, `btm` and `swaptop` show processes.
A browser or a terminal is dozens of processes, so the question "which app is eating my swap?" needs mental math.
On Linux, systemd already puts every desktop app into its own cgroup, and the kernel keeps per-cgroup memory and swap counters.
appmem reads those counters, groups them by app, and shows a sortable live table.
On macOS, appmem reads every process's native physical footprint and adds them up per app bundle, the same question answered from the counters that platform has.

The user it is built for: the machine feels slow, htop shows a wall of processes, and they want three answers in seconds.
Which app holds the memory and swap? Is memory actually the problem right now? What exactly should I close?

## Platforms

| | Linux | macOS |
|---|---|---|
| Requires | systemd user session, cgroup v2 with the memory controller, kernel 5.10+ (zswap figures need 5.19, Pressure needs PSI) | macOS 15+, Apple Silicon, a native arm64 Python |
| Apps seen | the user's units, plus system services on request | the current user's processes only |
| App memory | per-app cgroup counters: RAM, SWAP, CACHE, ZSWAP | per-process physical footprints (MEMORY), the compressed part of them (COMPRESSED) and resident sizes (RESIDENT), summed per app |
| Host memory | RAM, Zswap, Swap, PSI pressure | RAM, compression, Swap, native pressure level |
| Not available | cgroup v1, running as root | Intel Macs, macOS before 15, other users' processes, system scope, per-app swap, GPU memory |

- **Linux:** the kernel already sums each unit's memory, so appmem finds the units, names them and adds same-named units together.
- **macOS:** nothing is counted per app, so each sample lists the user's processes, reads each one natively and groups them by `.app` bundle, ancestry or the app macOS holds responsible for them. Per-app swap and cache don't exist there; each process's compressed bytes do, and are summed like its footprint.

Any other operating system fails with `platform_unavailable`.

## Scope

**In v1:**

- `appmem` opens the live view straight away.
- Refresh every 1 s (`-i/--interval SECONDS` to change).
- One row per app, sortable by clicking a column header or by key.
- Enter on a row opens the app's process view, which can also group processes by command and drill into one command.
- A host header: RAM, Swap and Pressure, a Zswap gauge on Linux, compression on macOS, swap in/out rates and written totals.
- `h`: a live host memory panel; `?`: a screen explaining the numbers.
- Themes: Textual's built-in themes, with `terminal-dark`/`terminal-light` for the terminal's own colours. `T` opens a side panel with a live preview, and the choice is remembered in a config file.
- Non-interactive commands for scripts and agents: `appmem snapshot`, `appmem app NAME`, `appmem schema`, plus an agent skill in `skills/appmem/SKILL.md`.

**Not in v1:** recording/history, charts over time, a streaming `watch` command, CPU or I/O stats, killing processes, settings other than the theme, custom themes (the terminal themes cover a tuned terminal palette), login-session scopes, Windows, and the platform limits in "Platforms".

## Screens

### Main view

Linux:

```
RAM      ██████████████▎░░░░░ 22.1/30.9 GiB used │ avail    8.8 GiB    shared    3.5 GiB    free  1.5 · cache  4.6 · slab  3.2
Zswap    ███▎░░░░░░░░░░░░░░░░    1.0/6.2 GiB RAM │ holds    3.0 GiB    ratio       3.0:1    limit 20% of RAM  · writeback 0 B/s
Swap     ██████████████▋░░░░░ 23.3/32.0 GiB used │ in         0 B/s    out      12 MiB/s    written this run   12 MiB · since boot 41.6 GiB
Pressure none                system  610 MiB [x] │ Δ since 15:58 (12m)                      elsewhere 109 MiB
 APP                        RAM         SWAP        ZSWAP       TOTAL ▾     ΔRAM       ΔSWAP      PROCS
 ghostty                       6.6 GiB    11.2 GiB     1.6 GiB    17.8 GiB     -3 MiB          ·     281
 plasma                        1.6 GiB     2.3 GiB     100 MiB     3.9 GiB          ·          ·      17
 chrome                        1.6 GiB     2.2 GiB     907 MiB     3.7 GiB     -1 MiB          ·      35
 ...
 r s t d z sort  enter procs  x system  c cache  w zswap  b reset Δ  T theme  h host  ? help  q quit
```

macOS:

```
RAM      ██████████████▍░░░░░     11.5/16.0 GiB used │ wired    2.4 GiB    free     926 MiB    file-backed  3.6 GiB · purgeable  310 MiB
Compress                                 1.3 GiB RAM │ data     4.1 GiB    ratio      3.2:1
Swap     ████████████░░░░░░░░ 1.2/2.0 GiB used/alloc │ in         0 B/s    out      3 MiB/s    written this run    8 MiB · since boot  5.2 GiB
Pressure normal                      apps: this user │ Δ since 15:58 (12m)
 APP                               MEMORY ▾      ΔMEM         RESIDENT      PROCS
 Google Chrome                          3.8 GiB      +12 MiB       5.3 GiB       24
 Ghostty                               2.0 GiB*            ?      2.8 GiB*        5
 com.apple.WebKit.WebContent            850 MiB            ·       1.2 GiB        2
 ...
 f d r sort  enter procs  b reset Δ  T theme  h host  ? help  q quit
```

Both target 105×30, with a compact 80×24 and a wide 160×40 view.

#### Header grid

Both platforms lay out their host rows on one grid.

| Part | Rule |
|---|---|
| Rows | Linux: RAM, Zswap (while zswap is enabled, also below 80 columns), Swap, Pressure. macOS: RAM, Compress, Swap, Pressure |
| Row | `label  left │ right` |
| Label | 9 cells from 80 columns, 10 below |
| Left part | a gauge, then the value right-aligned in a slot as wide as `total/total unit word` for this machine's totals (`22.1/30.9 GiB used` has no padding on a 30.9 GiB machine) |
| Gauge | none below 60 columns, 6 cells from 60, 10 from 80; from 105 the largest of 20, 16 and 12 that leaves every row complete, else 12 |
| Pressure slot | from 80 columns the left part also fits `unavailable` plus the Linux system amount or the macOS `apps: this user` |
| Right part | starts with a dim `│` (ASCII `|`); three columns at fixed positions |
| Columns 1 and 2 | a label as wide as the longest label in that column, then a value right-aligned in a 10-cell field (`1023 KiB/s`: rates are unbounded, so their field is the worst case) |
| Column 3 | detail items split by a dim `·` (ASCII `/`); cumulative totals in an 8-cell field, 100 GiB and more without the decimal |
| Word form | chosen after the gauge: wide words (`this run`, `since boot`, `of RAM`) and 4-cell gaps when every row is complete in that form, else short words (`run`, `boot`) and 2-cell gaps |
| Yielding | items yield whole from the right, so column 3 goes first; an item is never cut |
| Values | zero is shown as zero, unknown as `—` (ASCII `?`) |

- Every position and fit decision comes from the terminal width, the totals and the worst-case widths, never from the current values: nothing appears, disappears or moves when a number changes. If a total changes (swapon, macOS swap growth) the slots recompute.
- Examples: a 30.9/32.0 GiB Linux machine is complete from 116 columns with short words and from 139 with wide words; an 8 GiB Mac with 1 GiB swap allocated, from 124 and 142.
- **Hidden data:** a normal-foreground `…` (ASCII `>`) right after the last visible field means host information was omitted for space; a row whose left part alone does not fit is cropped before it. Unavailable readings are distinct from omitted information.
- Header rows never wrap or grow on a sample, and data changes never choose a new gauge width or row count. The table gets the remaining height.

#### Header rows

- **RAM:** used/total and a neutral gauge.
  - Linux: used = `MemTotal - MemAvailable`. `avail` and `shared` fill columns 1 and 2; `free`, `cache` and `slab` follow in column 3 in the total's unit. `shared` is `Shmem`; `free` is `MemFree`, `cache` is `Cached - Shmem` clamped at zero, `slab` is `SReclaimable`. These are not an exact sum of available RAM.
  - macOS: used is the RAM partition (see Definitions); `wired` and `free`, then `file-backed` and `purgeable`. An unknown partition keeps every field in place with `—` (`—/8.0 GiB used`); with no physical total the value reads `used unavailable`.
- **Zswap** (Linux): physical pool / approximate `max_pool_percent × RAM` limit in the gauge and value, `holds` (logical data) and `ratio` in columns 1 and 2, then `limit N% of RAM` and the `writeback` rate in column 3. The gauge is filled from the pool's RAM against its limit, never from the logical data. The limit is a policy, not reserved RAM. Unknown and zero limits have neutral placeholder gauges. A pool above its limit turns the value and the limit item to the error colour and the word reads `above N%` instead of `limit N%`; the item keeps its place either way. Swap used includes the held logical data before compression; RAM used includes the physical pool. Do not add either again or infer disk-only swap by subtraction.
- **Compress** (macOS): the compressor's RAM as the value, no gauge (there is no limit), then `data` (logical bytes) and `ratio` (only when both are above zero).
- **Swap:** used/total and a neutral gauge. Linux shows `off` with a placeholder gauge when total is zero, and turns the value the theme's error colour above 90% used. macOS divides by the swap allocated now (`used/alloc`); zero allocation reads `0 B; not allocated` with a placeholder gauge, invalid counters `unavailable`.
  - Columns 1 and 2: the `in` and `out` rates. Column 3: `written` with the total this run and since boot (`written run 1.9 GiB · boot 2.4 TiB`, wide `written this run 1.9 GiB · since boot 2.4 TiB`); since boot yields first.
  - Rates are averaged over a 5-second window of lifetime counters. The first frame shows `—` until two samples at least half a refresh interval apart (at most half a second) exist. Missing, invalid or decreasing samples, duplicate timestamps and discontinuities are unknown; measured zero is `0 B/s`. A wall/monotonic clock disagreement over 5 seconds, or a gap longer than max(30 seconds, 3×interval), restarts measurement. `b` does not reset rates.
- **Pressure:** the word, bold, in the theme's success/warning/error colour, or `unavailable`, in the gauge column. `Δ since HH:MM (12m)` (baseline time and compact age, whole days from 100 h on) spans columns 1 and 2.
  - Linux: `system N [x]` right-aligned at the end of the left part, then `Δ since` and `elsewhere` (shown from 1 MiB). The long form (`none (was 3.4 %)`) gives way to the short word before the system amount does; below 80 columns the amount leads the right part instead, or is omitted. The word is never cut.
  - macOS: `normal`, `warning` or `critical`, with `apps: this user` where Linux has `system N [x]`. Pressure is the machine's; only the app rows are this user's.
- **Bars:** `█` fill with an eighth-block edge on a dim `░` track. The fill uses the theme's accent colour, or its foreground where the accent is under 3:1 contrast. A `#`/`.` ASCII form applies when `LC_ALL`/`LC_CTYPE`/`LANG` names a non-UTF-8 locale; a bare `LANG=C` still gets Unicode bars, because Python coerces it to UTF-8 at startup. Bars carry no information that the numbers don't.
- **Short terminals** (Linux): below 18 terminal rows the header keeps two rows. From 70 columns: RAM with the pressure word leading its right part, then `shared`, `avail` and the breakdown; Swap with the system amount, `Δ since` and the zswapped data (zswapped first below 100 columns), then the activity. Below 70 columns: `RAM u/t  Swap u/t …`, then Pressure with the system amount. The macOS header keeps four rows at every height.

#### Host panel

- `h` on the main view opens a live, scrollable host memory overlay at every supported width; `h` or Esc closes it. It rewraps on resize and keeps its scroll position across refreshes.
- It runs on the dashboard's own collector, rate history and timer; closing it adds no sample and keeps the table's selection and viewport. After a failed refresh it says it shows the last successful reading.
- It is laid out from structured blocks for the current width, never by re-reading its own text; a tick that changes nothing does not touch the widget.
- Both: RAM, Swap used, an Activity table (Read / Written by Current rate, Since boot, This run), Pressure, and when the Δ baseline started.
- Linux adds the RAM breakdown, a Zswap block (RAM occupied, approximate Pool limit with its RAM percentage, Data held, Compression, Compressor, Writeback rate and since-boot total) with its accounting notes, the PSI percentages, the system services total and `elsewhere`. Writes are called disk writes only when every current `/proc/swaps` entry is a recognised swap file or disk partition; zram, mixed, aliased, device-mapper or unreadable entries get generic swap-device wording.
- macOS adds file-backed, free, wired, purgeable, compressed data versus its RAM, and that swap space is allocated dynamically.

#### Table

- The sort marker `▴`/`▾` sits on the sorted column. Default sort: TOTAL descending on Linux, MEMORY descending on macOS.
- Clicking the sorted column again flips the direction.
- Rows with equal values keep a stable order by app name; on macOS unknown values sort last.
- Sorting by a column a resize hides falls back to the default sort.
- Δ below 1 MiB either way shows as `·` (dim on Linux).
- A footer with key caps sits at the bottom of every view and drops its lowest-priority items instead of wrapping. It shows only keys that act in the current view and mode, labelled by what they do there (e.g. `d` is absent while the Δ columns are hidden). `T theme` is the first item to drop, then `b reset Δ`, then `h host`, so `enter procs` survives at 40 columns.
- Mouse-wheel scrolling stays where the user put it across refreshes: a tick restores the cursor without scrolling the viewport; only explicit actions (sort, toggles, `b`, drill in/out, a resize) scroll the selected row into view.

Columns, in order:

| Platform | Columns | Shown |
|---|---|---|
| Linux | APP, RAM, SWAP, CACHE, ZSWAP, TOTAL, ΔRAM, ΔSWAP, PROCS | CACHE only after `c`; ZSWAP by default while zswap is enabled (`w` toggles), from 85 columns; ΔRAM and ΔSWAP from 95 columns |
| macOS | APP, MEMORY, COMPRESSED, ΔMEM, RESIDENT, PROCS | ΔMEM from 65 columns; COMPRESSED from 80 columns; RESIDENT from 100 columns |

- Linux: at the default 1 s interval, PROCS in the live table refreshes every five seconds (every 5th tick); the memory columns (RAM, SWAP, CACHE, ZSWAP, TOTAL) refresh every tick. A unit seen for the first time is always counted right away. Δ columns render dim while the baseline is younger than 60 s.
- macOS: a partial known sum is marked `*`, a wholly unknown value is `?`; a known zero reads `0 B`. Every group is shown, whatever its size.
- macOS width: the APP name column gives way first (it shrinks from 32 cells to 28 at 80 columns, never below 8); below 80 columns COMPRESSED goes, below 65 ΔMEM, and RESIDENT only appears from 100. A sort on a column that goes falls back to MEMORY.

### Process view (after Enter)

Linux:

```
ghostty   281 procs   swap 11.2 GiB   RAM 6.6 GiB
 PID      NAME                  RAM         SWAP        TOTAL ▾     AGE     UNIT
    6091  ghostty                  129 MiB     3.3 GiB     3.4 GiB     86d  app-com.mitchellh.ghostty.service
 1504671  ghostty                   19 MiB     591 MiB     610 MiB     12d  app-ghostty\x2d2@ad7f69d9c06a4d43bae214674…
 2026292  claude                   268 MiB     135 MiB     404 MiB      1d  app-ghostty-surface-transient-4172209.scope
 ...
          kernel                   ...                                      (page tables, slab, stacks)
          zswap pool               ...                                      (compressed swap kept in RAM)
          unattributed             ...                                      (held by the app, not by any process)

systemctl --user stop 'app-com.mitchellh.ghostty.service'   kill 6091
 r s t sort  g group  ? help  esc back  q quit
```

With `g` (group by command):

```
 NAME                  RAM         SWAP        TOTAL ▾     PROCS
 claude                   4.2 GiB     1.3 GiB     5.5 GiB      18
 ghostty                  207 MiB     4.3 GiB     4.5 GiB       3
 node                     307 MiB     1.7 GiB     2.0 GiB      68
 ...
 r s t sort  g group  enter members  ? help  esc back  q quit
```

Enter on a command drills into its member processes (title `ghostty › claude`, flat columns, live); Esc (footer `esc groups`) returns to the grouped list on the same command. If the app disappears during a drill-down, the view falls back to the app's empty state with its own columns.

- The process view opens sorted by the same column as the main view (SWAP stays SWAP, TOTAL stays TOTAL).
- SWAP, RAM and TOTAL are per-process values (see Definitions).
- Dim rows are pinned last in the flat and grouped layouts (not in a drill-down, since they belong to the whole app):
  - `kernel`: the app's charged kernel memory (the `kernel` field of `memory.stat`) minus its zswap pool.
  - `zswap pool`: the RAM the app's compressed swap takes (`memory.stat` `zswap`, charged inside `kernel`); only when above 0.
  - `unattributed`: app SWAP minus the process SWAP sum, and app RAM minus `kernel` minus `zswap pool` minus the process RAM sum, each clamped at 0. Memory the app holds without any process mapping it. Without that row the gap would look like a bug.
- The status line above the footer describes the selected row:
  - process row: the full unit name and `systemctl --user stop '<unit>'` (`sudo systemctl stop '<unit>'` for system units) plus `kill <PID>`;
  - grouped command in one unit: the stop command without `kill`; in several units: `N units, Enter lists the processes`;
  - `kernel`/`zswap pool`/`unattributed`: a one-line explanation.
  The unit is shortened in the middle only when the line is wider than the terminal.
- The UNIT column is the last one and may be cut at the screen edge; the status line carries the full name. NAME takes the width left after the numeric columns at every width (long names get `…`), and re-syncs when a scrollbar appears or goes, so RAM, SWAP, TOTAL and PROCS are never cut.
- Under 95 columns AGE is hidden. The title drops `procs`, then `swap`, instead of wrapping.
- `g`, Enter into a drill-down and Esc out of it switch only after the new view's data was read; if that read fails, the current view stays as it was.
- All layouts refresh with the same interval as the main view.

macOS:

```
Google Chrome  memory 3.8 GiB
 PID      COMMAND                           MEMORY ▾    COMPRESSED      RESIDENT      STATE
     100  Google Chrome                          900 MiB       310 MiB       1.2 GiB  readable
     101  Google Chrome Helper (Renderer)        130 MiB        64 MiB       182 MiB  readable
     102  Google Chrome Helper (Renderer)        130 MiB             ?       182 MiB  readable
 ...
via bundle  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
 f c n p r sort  g group  esc back  T theme  ? help  q quit
```

With `g` (group by command):

```
 COMMAND                           MEMORY ▾    COMPRESSED      RESIDENT      PROCS    UNREADABLE
 Google Chrome Helper (Renderer)        2.9 GiB       1.1 GiB       4.1 GiB       23             0
 Google Chrome                          900 MiB       310 MiB       1.2 GiB        1             0
23 memory readable, 0 unreadable processes; resident 23/23 readable; compressed 23/23 readable
 f c n p u r sort  g ungroup  enter members  esc back  T theme  ? help  q quit
```

- Title: the app name and its MEMORY, or `Application vanished`.
- Flat: PID, COMMAND, MEMORY, COMPRESSED (from 80 columns), RESIDENT (from 100 columns), STATE (`readable`/`unreadable`, from 75 columns). The status line shows `via <rule>` (see "Grouping"), then the selected process's executable path, or why its memory is unavailable.
- Grouped: COMMAND, MEMORY, COMPRESSED (from 80 columns), RESIDENT (from 100 columns), PROCS, UNREADABLE (from 75 columns), with readable counts for all three metrics in the status line. Enter drills into a command's processes, live; Esc returns to the groups on the same command.
- All layouts refresh with the same interval as the main view.
- Sorted by MEMORY descending, unknown last; a hidden sort column falls back to that.
- No stop commands and no kernel or unattributed rows: nothing is held outside the processes' footprints.

### Help screen (`?`)

A scrolling screen with the definitions below in plain words, soft-wrapped to the width, with `esc/?/q close` in its title line, ending with the platform's key list, one line per key.

- Linux: what RAM, CACHE, SWAP, TOTAL, pressure and the header's shared/free/cache/avail mean (tmpfs files count toward the app that wrote them), swap in/out and written totals, why rows don't add up to the header, why a closed app can still have a row, and how to act on what you see, plus `h` for the host panel, the trailing hidden-data marker (`…`, ASCII `>`), and `T` with where the theme is saved. When zswap is enabled, it also defines the physical Zswap gauge and logical ZSWAP, and lists `z` and `w`. It also explains the bar glyphs (`█` used, `░` what's left).
- macOS: swap in/out, MEMORY, COMPRESSED (part of MEMORY, not on top of it; a large share means the app's pages were squeezed to make room; it does not add up to the Compress line, which counts every user's memory still in RAM while the column counts this user's apps and includes what went on to swap), RESIDENT, ΔMEM, `*`/`?`, RAM, file-backed, Compress, Swap, Pressure, grouping, the host panel and the hidden-data marker.

## Keys

| Key | Action |
|---|---|
| click header | sort by that column, click again to reverse |
| click a row | select it, double click opens it (process view, or a command's processes when grouped) |
| `r` / `s` / `t` / `d` / `z` | Linux: sort by RAM / SWAP / TOTAL / ΔSWAP / ZSWAP (repeat to reverse); a key whose column is hidden is absent and does nothing; other columns sort by click |
| `f` / `c` / `d` / `r` | macOS main view: sort by MEMORY / COMPRESSED / ΔMEM / RESIDENT (repeat to reverse); a hidden column's key does nothing |
| `f` / `c` / `r` / `n` / `p` / `u` | macOS process view: sort by MEMORY / COMPRESSED / RESIDENT / command / PID (PROCS when grouped) / UNREADABLE |
| `↑` `↓` `PgUp` `PgDn` | move |
| `Home` `End` | jump to the first/last row |
| `Enter` | open the process view for the selected app; in grouped mode, the processes of the selected command |
| `g` | process view: toggle grouping by command |
| `Esc` | back to the main view (from a drill-down, to the groups) |
| `c` | Linux: toggle the CACHE column. macOS: sort by COMPRESSED (see the sort rows above) |
| `w` | Linux: toggle the ZSWAP column (main view, only while zswap is enabled; the choice lasts for the session) |
| `x` | Linux: toggle system services |
| `b` | reset the Δ baseline to now |
| `T` / `Ctrl+P` → Theme | theme panel (all views, see "Theme panel"); opening it again while open does nothing |
| `h` | main view: live host memory panel; `h` or Esc closes it |
| `?` | help screen |
| `q` / `Ctrl+C` | quit |

## Data sources

### Linux

All reads are plain, world-readable files. No root needed.

| What | Source |
|---|---|
| App units | see "Finding units" below |
| SWAP per app | `memory.swap.current` of the unit |
| RAM per app | `memory.stat` of the unit: `anon + shmem + kernel` (kernels before 5.18 have no `kernel` field: `slab + kernel_stack + pagetables + percpu`) |
| CACHE per app | `memory.stat` of the unit: `file - shmem` |
| ZSWAP per app | `memory.stat` of the unit: `zswapped` |
| zswap | `/sys/module/zswap/parameters/enabled` (`Y`; missing = off), `compressor`, `max_pool_percent`; `/proc/meminfo` `Zswap`/`Zswapped`; `/proc/vmstat` `zswpwb` |
| zswap pool per app | `memory.stat` of the unit: `zswap` (inside `kernel`) |
| Processes of an app | `cgroup.procs` of the unit and every directory below it |
| SWAP per process | `/proc/PID/status` → `VmSwap` |
| RAM per process | `/proc/PID/status` → `RssAnon + RssShmem` |
| Process name | basename of the first whitespace-separated token of the first `/proc/PID/cmdline` field; `/proc/PID/comm` when cmdline is empty, the result is `exe`, or argv[0] starts with `/proc/`. Interpreters and launchers get a label (see "Process names" below) |
| Private RAM per process (`app NAME` only) | `/proc/PID/smaps_rollup`: `Private_Clean + Private_Dirty`; null when unreadable |
| Process age | `/proc/PID/stat` field 22 (`starttime`), parsed after the last `)` because names may contain spaces and parentheses |
| System totals | `/proc/meminfo`: `MemTotal`, `MemAvailable`, `MemFree`, `Cached`, `Shmem`, `SReclaimable`, `SwapTotal`, `SwapFree` |
| Swap in/out | `/proc/vmstat` `pswpin`/`pswpout` × `SC_PAGE_SIZE` |
| Swap targets (panel wording) | `/proc/swaps` |
| Pressure | `/proc/pressure/memory`, `avg10` and `avg60` of `some` and `full` |
| Hidden system total | `memory.stat` and `memory.swap.current` of `/sys/fs/cgroup/system.slice` |
| `elsewhere` | root `/sys/fs/cgroup/memory.stat` minus the `user@$UID.service` tree minus `system.slice` |

PROCS is the line count of `cgroup.procs`, not `pids.current`, which counts threads.

### macOS

Native calls through `ctypes`: no privileges, no subprocesses, only the current user's processes.

| What | Source |
|---|---|
| Process list | `proc_listpids` |
| Parent, owner, short name | `proc_pidinfo` short BSD info |
| Executable path | `proc_pidpath` |
| Footprint, resident size, start time | `proc_pid_rusage` `RUSAGE_INFO_V4`: `ri_phys_footprint`, `ri_resident_size`, `ri_proc_start_abstime` |
| Compressed bytes | `task_name_for_pid` and `task_info(TASK_VM_INFO)` with the 38-word revision macOS 12 and later accept, field `compressed` only; the name port is released after every read. A refused or vanished process is unavailable for this metric alone |
| Bundle id (same-named bundles only) | `CFBundleIdentifier` in `<bundle>/Contents/Info.plist`, a regular file of at most 1 MiB |
| Physical memory | `sysctl hw.memsize` |
| Swap used and allocated | `sysctl vm.swapusage` |
| Page counters | `host_statistics64(HOST_VM_INFO64)` × `host_page_size`: free, speculative, external (file-backed), purgeable, wired, compressor pages, uncompressed pages in the compressor, swapins, swapouts |
| Pressure | `sysctl kern.memorystatus_vm_pressure_level`: 1 normal, 2 warning, 4 critical |

- Each process is read as identity, memory, path, identity again, memory again; a changed parent, owner or start time means the PID was reused mid-sample, and the process is skipped.
- Per-process failures are `denied`, `vanished`, `unsupported` or `error`; unreadable memory keeps the process in its group with an unknown value. Pressure can fail alone; the other host reads succeed or fail together.

### Process names

Linux arguments can hold secrets, so a name never shows one, except for this allowlist, which fails closed.
- For `node`, `bun`, `deno`, `npm`, `npx`, `uv`, `uvx` and `python`/`python3`/`python3.N`, with a real NUL-separated argv, the name is `interpreter:target` (`node:mcp-remote`, `npx:@scope/tool`, `python3:http.server`).
- Walking the arguments skips only known verbs (`npm exec/run/x`, `uv tool/run/install`, `bun`/`deno run`) and known value-less flags (python `-u -B -O -E -s -S -I`; node `--no-warnings --enable-source-maps`; npm/npx `-y --yes`).
- The target is a script basename, a `-m` module or a package spec with `@version` dropped. A generic script basename (`index.js`, `main.js`, `cli.js`, `__main__.py`) becomes the package: the directory after `node_modules/`, or the nearest meaningful parent.
- The target must match `[A-Za-z0-9._@/+-]`, at most 40 characters.
- Any other flag (inline code `-e`/`-c`, options with values), a URL, a query string or a failed check gives the bare interpreter name.

On macOS a name is the executable's basename, never an argument; the 15-character BSD short name, which merges helpers with a shared prefix, is only the fallback when the path is unreadable.

### Finding units

Roots: `/sys/fs/cgroup/user.slice/user-$UID.slice/user@$UID.service/{app,session,background}.slice`, plus `/sys/fs/cgroup/system.slice` when system services are shown.

- A **unit** is a directory named `*.service` or `*.scope`.
- Walk down from each root, descending only through `*.slice` directories. Stop at the first unit directory and never descend into it.
- Read counters at the unit directory. They are hierarchical: they already include every sub-cgroup (Konsole keeps one `tab(PID).scope` per tab under its unit).
- Never sum a unit with anything below it.
- Directories of other types (`*.socket`, `*.mount`, `*.swap`) are ignored.

Login-session scopes (`user-$UID.slice/session-N.scope`: the display manager helper, SSH and VT logins) are not shown in v1.

### Definitions

Linux:

- **RAM = anon + shmem + kernel.** Memory the app holds that the kernel can't just drop.
  `memory.current` would also count page cache, which makes an app that read a big file look like a hog, even though the kernel frees that cache instantly.
  In cgroup v2, `shmem` is counted inside `file`, not `anon`, so it is added explicitly.
  `kernel` is page tables, slab and kernel stacks charged to the app (tens of MiB for a browser).
- **CACHE = file - shmem.** Reclaimable page cache. Hidden by default, and never part of TOTAL.
- **SWAP = memory.swap.current.** With zswap enabled this includes pages held compressed in RAM.
- **ZSWAP = zswapped** (shown by default while zswap is enabled, `w` toggles): the part of the app's SWAP held compressed in RAM, not extra memory. The RAM the compressed pool takes is charged to the app as `kernel` memory, so it is already inside its RAM (verified live).
- **TOTAL = SWAP + RAM.**
- **Per-process RAM and SWAP** come from `/proc/PID/status`, the same numbers htop uses (RAM is RSS, so don't sum processes; use the app totals). They are readable for every process, including sandboxed browser processes and other users' processes.
  They don't add up to the app row, for two reasons. A shared page counts once in every process that maps it, so rows can add up to more than the app. Memory the app holds without any process mapping it (GPU buffers, memfd, tmpfs) belongs to no process, so rows can fall short. The `kernel` and `unattributed` rows show the gap.
- **Memory pressure** is the share of time tasks waited for memory over the last 10 s:
  - `high`: `full avg10` > 5 % or `some avg10` > 20 %
  - `some`: `some avg10` ≥ 1 %, shown with the value, e.g. `some (3.2 %)`
  - `none (was X %)`: below that, but `some avg60` > 1 % (X, capped at 99.9), so stalls just stopped
  - `none`: otherwise

  Big swap with pressure `none` means idle pages were paged out, and memory is not why the machine is slow right now.
- **App identity** is (scope, name): a user app and a system service with the same name are separate rows, and system rows show as `name [sys]`.

macOS:

- **MEMORY** sums the readable members' physical footprints: the memory charged to each process, including its compressed pages, as in Activity Monitor's Memory column. It is not resident RAM or what quitting frees, and footprint sums never yield host used, available or `elsewhere`.
- **COMPRESSED** sums the readable members' `compressed` bytes: the part of each process's memory held by the compressor, in RAM or swapped out, at its uncompressed size. It is already inside MEMORY, never added to it. Its coverage is independent of MEMORY's: an unreadable member makes the sum partial (`*`), no readable member makes it unknown (`?`, `unknown` in text, JSON `null`), not zero. Shared anonymous memory can count in more than one process, as in MEMORY, and the sum over apps is not the host's Compress figure (other users' processes and the kernel hold compressed pages too).
- **RESIDENT** sums the readable members' resident sizes, where shared and file-backed pages count in every process. It is not additive with MEMORY, and their difference is not swap.
- **Coverage:** each metric counts its own readable and unreadable members. A sum with an unreadable member, or from a partially grouped app, is marked `*`; no readable member is `?` (`unknown` in text), never zero.
- **Host RAM used = physical − (free − speculative) − file-backed.** Native free includes speculative, which file-backed also includes, so it is subtracted once. Reserved and unaccounted memory stays in used; wired and purgeable overlap used; file-backed is not all immediately available. The partition is unknown when a counter is missing or negative, free or file-backed exceeds physical, speculative exceeds free or file-backed, or used would be below wired plus compressed RAM.
- It reads higher than Activity Monitor's Memory Used (app memory without purgeable, plus wired, plus compressed) because it keeps purgeable pages and the memory no page counter covers (set aside at boot).
- **Compress** `data` is the logical bytes the compressor holds; its RAM is already inside RAM used. Zero swap allocation means no swap in use, not swap turned off. **Pressure** is the kernel's level, not PSI or a RAM percentage.

Both:

- **Swap in/out** rates come from lifetime host counters; they are not app bytes or SSD throughput.
  - Linux: swap-device reads and writes, including zram and zswap writeback (never add or subtract writeback again). Successful zswap hits are excluded; zero-page bypass depends on kernel version.
  - macOS: page-rounded compressed segments moved to and from swap files, including housekeeping, independent of the current allocation.
- **Written totals:** `since boot` is the lifetime counter; `this run` subtracts the first accepted sample, per direction. A missing first sample leaves that direction unavailable for the run; a later missing one hides only the current value; any decrease invalidates the run total for good. Rate resets, long gaps, `b` and navigation never reset it.
- **Δ** is the change since the baseline shown in the header: appmem start, or the last `b`. Apps that appear later count from their first sample.
  On macOS the baseline must be a complete sample (no unreadable member, grouping not partial); ΔMEM is `?` until then and while the current sample is partial, then compares with the retained baseline again. A vanished group loses its baseline; a bundle reopened with no shared member (PID and start time) starts a new one.

## Grouping

### Linux: unit → app name

Several units can belong to one app: many Ghostty windows, LibreOffice helper units, Chrome launched in two different ways.
Rows are merged by app name, and the counters are summed.

Normalization, in order:

1. Take the unit directory name.
2. Unescape every systemd `\xNN` escape (`\x2d` → `-`) and decode the resulting bytes as UTF-8, invalid sequences replaced (`\xe5\xbe\xae\xe4\xbf\xa1` → `微信`).
3. Strip the `.service` / `.scope` suffix.
4. Snap: `snap.<name>.<app>-<uuid>` → `<name>`, then go to step 11.
5. Flatpak: `app-flatpak-<id>-<n>` → `<id>`, then go to step 9.
6. Strip the `app-` prefix.
7. Strip the instance part: `@<anything>`, trailing `-<digits>`, trailing `-<uuid>`.
8. Generic desktop IDs (`org.chromium.Chromium`): Electron apps and Chromium forks without an id of their own report it to the compositor, so KDE names their scope after it instead of the program. Such a unit takes its app name from its leader process instead (the pid in the scope name while it is still in the unit, else the lowest readable pid under the unit, at most three pids tried in all; the process name as the process view shows it), then continues at step 11. When no process can be read, continue normally with the unit name (step 9).
9. Reverse-DNS IDs: when the name has 3 or more dot-separated labels, drop the first two (`com.mitchellh.ghostty` → `ghostty`, `org.kde.discover.notifier` → `discover.notifier`).
10. Snap desktop IDs: `<x>_<x>` → `<x>` (`thunderbird_thunderbird` → `thunderbird`).
11. Lowercase.
12. Apply the built-in alias map.

Built-in alias map. Exact entries match the whole name, prefix entries match its start:

| Match | Kind | App |
|---|---|---|
| `ghostty-surface-transient` | exact | `ghostty` |
| `brave-browser` | exact | `brave` |
| `google-chrome` | exact | `chrome` |
| `element-desktop` | exact | `element` |
| `superproductivity-bin` | exact | `superproductivity` |
| `mullvad` | exact | `mullvad-vpn` |
| `libreoffice-` | prefix | `libreoffice` |
| `plasma-` | prefix | `plasma` |

Units that don't match anything keep their normalized name as their own row (`pipewire`, `kded6`, `xdg-desktop-portal-gtk`).
The process view always shows the real unit name.

Acceptance table (real unit names from the dev machine, used as unit tests):

| Unit directory | App |
|---|---|
| `app-com.mitchellh.ghostty.service` | `ghostty` |
| `app-ghostty\x2d2@7b755c4e18184688b9c5e64a8ceb245d.service` | `ghostty` |
| `app-ghostty-surface-transient-4172209.scope` | `ghostty` |
| `app-brave\x2dbrowser@c16f78223b3c4371a764a76609ae9ef0.service` | `brave` |
| `app-org.chromium.Chromium-4020402.scope` | the leader's program name (e.g. `obsidian`) |
| `app-com.google.Chrome-3242011.scope` | `chrome` |
| `app-google\x2dchrome@c16f78223b3c4371a764a76609ae9ef0.service` | `chrome` |
| `app-element-3663554.scope` | `element` |
| `app-element\x2ddesktop@c16f78223b3c4371a764a76609ae9ef0.service` | `element` |
| `app-superproductivity-bin-100001.scope` | `superproductivity` |
| `app-libreoffice\x2dcalc@c16f78223b3c4371a764a76609ae9ef0.service` | `libreoffice` |
| `app-org.kde.kate@c16f78223b3c4371a764a76609ae9ef0.service` | `kate` |
| `app-org.kde.discover.notifier@autostart.service` | `discover.notifier` |
| `app-org.kde.konsole-2315830.scope` | `konsole` |
| `snap.telegram-desktop.telegram-desktop-e44637ac-ec33-42ac-b09e-2fe496512c47.scope` | `telegram-desktop` |
| `snap.thunderbird.thunderbird-9da00651-f149-4b5a-9e13-3825933dfa79.scope` | `thunderbird` |
| `app-thunderbird_thunderbird@c16f78223b3c4371a764a76609ae9ef0.service` | `thunderbird` |
| `snap.bitwarden.bitwarden-93d0664d-1c21-435b-9ec0-b47f988103f8.scope` | `bitwarden` |
| `app-bitwarden_bitwarden@c16f78223b3c4371a764a76609ae9ef0.service` | `bitwarden` |
| `plasma-kwin_wayland.service` | `plasma` |
| `plasma-powerdevil.service` | `plasma` |
| `pipewire.service` | `pipewire` |

### macOS: processes → app

Each sample is one immutable process inventory; the process view reuses its members.

1. **Bundle:** a process whose executable lies inside an `.app` belongs to the outermost `.app` on its path, so an app's helpers, frameworks and XPC services count as the app even when launchd started them. A `.framework` before any `.app` on the path means no bundle: that `.app` is a tool the framework ships (the Python framework's `Python.app`).
2. **Ancestry:** a process outside any bundle joins its nearest ancestor with a bundle (at most 64 levels), so a shell and everything run in Ghostty count as Ghostty.
3. **Responsible:** only when the walk of rule 2 ended below launchd (parent pid 1) without reaching a bundle, appmem asks macOS which process it holds responsible for the topmost process, with the private `responsibility_get_pid_responsible_for_pid` (the process the system attributes the helper's privacy and resource use to; WebKit's WebContent, GPU and Networking services and other per-client XPC services report the app that asked for them). The answer is trusted only when it is another process of the same sample that started no later than the helper (a pid is reused by a later process) and that resolves through rules 1 and 2 to a bundle; the helper then joins that app. Every other answer is ignored. A bundleless answer, a self-responsible helper such as `com.apple.Safari.History`, a failed call and an absent symbol all leave the helper to rule 4, exactly as without this rule. Rules 1 and 2 come first because responsibility follows the launch chain: a process started from a terminal is responsible to the terminal, and a helper an app disclaims is responsible to itself, so neither says which app the process belongs to. A terminal-launched app's own XPC helpers therefore land in the terminal's row, a known limit. The call is private (it has no header and Apple may change it), so it is looked up on first use and its absence is not an error; it needs no root.
4. **Roots:** otherwise the walk stops below launchd or at a parent owned by another user, and the topmost process is the root. Bundleless roots with one name are one app, as Linux merges units; a bundle and a bundleless root with one name stay apart.
5. **Names:** the bundle's directory name without `.app`, or the root's executable basename. Same-named bundles at different paths add their bundle id when readable, `Name (bundle.id)`; it stays with its bundle when the twin quits. Each app's `id` is the first 12 hex digits of a SHA-256 of its bundle path or root name.
6. **Partial grouping:** a vanished or unreadable parent, a parent started after its child (PID reuse), a cycle, the depth limit, or an unreadable path or start time. A launchd service rule 3 could not place is not partial: it is a row of its own.
7. **Reason:** each process records the rule that placed it, `via`: `bundle` (rule 1), `ancestry` (rule 2), `responsible` (rule 3) or `root` (rule 4). It is shown in the process view's status line and in the `app` document and text report.

WebKit's `com.apple.WebKit.WebContent`, `.GPU` and `.Networking` live in `WebKit.framework`, so rule 1 cannot place them; rule 3 puts them in the app that uses them, so a Safari row includes the pages it shows. Services launchd starts that no app is responsible for (shared agents such as `com.apple.Safari.History`, which is responsible to itself) are rows of their own.

`app NAME` matches an `id`, a full name, a plain name only one app has, or `Name (bundle.id)` after the twin has gone; a miss hints up to five names containing it.

### Terminals

Anything started from a terminal lives in the terminal's cgroup (on macOS, under its bundle by ancestry), so `claude`, `node` or `python` run from Ghostty count as Ghostty.
That is how the system sees it.
The process view with `g` shows what is really inside in one screen.
Splitting terminal children into their own main-view rows is v2.

## Behaviour details

- Sizes use binary units and a dot as the decimal separator. GiB gets one decimal, MiB and KiB are integers (`16.8 GiB`, `677 MiB`).
- Numeric columns are right-aligned with fixed widths, so values changing size don't re-flow the table.
- Linux rows with less than 1 MiB TOTAL are hidden. The CACHE toggle doesn't change which rows show.
- Apps that appear mid-session get a row. Apps that disappear drop out at the next refresh.
- A unit or process that vanishes between listing and reading is skipped silently. This is normal churn, not an error.
- A Linux row can have PROCS 0 and memory above 0: the unit outlives its processes while it still holds memory. It shows like any other row, and its process view shows only the `kernel` and `unattributed` rows.
- Linux: a tick whose reads fail transiently (`memory.stat` missing, any `OSError`, a parse error from a half-written `/proc` or `/sys` file) is skipped; the screen keeps the last data and the next tick recovers. Errors while applying the data to the screen are bugs and still end the app. Only a missing user tree ends the app as a runtime failure.
- macOS: a failed native read keeps the last data, says `Read unavailable; showing stale values; retrying` in the first header row or the process view's status line, and retries next tick; `b` then resets nothing.
- The cursor follows the selected app across refreshes and re-sorts. If that app disappears, the cursor stays at the same row index, or on the last row.
- Names (apps, processes, units, titles, status line) show C0/C1 control characters escaped (`\x1b[41m`), in the live view as in the text reports; nothing a process or unit is called can write to the terminal.
- Widths count terminal cells, not characters: names are cut at 32 cells with `…` and a wide character is never split. No line of any view wraps at any width. In the main view, the APP column takes only the width left after the numeric columns (capped at 32, never below 8), at every width and again when a scrollbar appears, so numbers are never cut; long names get `…` first.
- Below about 55 columns in the Linux main view (about 67 with CACHE shown), APP's own floor of 8 no longer leaves room for every numeric column to stay whole; a number can be cut from there down, the same way a name is above that floor.
- Periodic reads run off the UI thread, one at a time per screen; keys stay responsive while a read is slow, and a result read for a view the user has since left is dropped.

## Command line

```
Linux:
appmem [-i SECONDS] [--system] [--theme NAME]      live view (TUI)
appmem snapshot [--system] [--limit N] [--json]     machine state + apps
appmem app NAME [--scope user|system] [--limit N] [--json]
                                                    one app: units, processes, commands, remainder
macOS:
appmem [-i SECONDS] [--theme NAME]                  live view (TUI)
appmem snapshot [--limit N] [--json]                host memory + app footprints
appmem app NAME [--scope user] [--limit N] [--json] one app: processes, commands
Both:
appmem schema [COMMAND]                             interface description as JSON
appmem --help | -h
appmem --version | -V
```

The command line conforms to the house CLI Design Standard 0.1.0 (claimed in `appmem schema` under `conformance`).
`appmem schema` and each command's `--help` are the reference for flags, defaults and output fields on the running platform; this section only fixes the behaviour.

- **Live view** (no command): `-i/--interval` (default 1, ≥ 0.2) and, on Linux, `--system` (start with system services shown). It starts only in a terminal context: stdin and stdout are terminals, no `--json`, `NO_INPUT` unset or empty. Otherwise it exits `1` with `terminal_required` (the call is fine, the context isn't, so it's not a usage error) and `next: ["appmem","snapshot"]`, instead of drawing escape codes into a pipe. `-i` or `--theme` together with a command is `invalid_input`, and so is `--system` before `app` or `schema`.
- On macOS `--system` does not exist and `--scope` accepts only `user`; either is `invalid_input` with the macOS usage in the hint.
- **Theme** (live view only). The theme is taken from the first valid source in this order:
  1. `--theme NAME`;
  2. `APPMEM_THEME`;
  3. `$XDG_CONFIG_HOME/appmem/config.toml` (default `~/.config/appmem/config.toml`, key `theme`);
  4. `TEXTUAL_THEME`;
  5. `textual-dark`.

  Valid names: Textual's built-in themes, except that `ansi-dark`/`ansi-light` appear as `terminal-dark`/`terminal-light`, the same themes under names that say what they do: they use the terminal's own palette. The old names stay accepted as aliases from every source but are never listed. An unknown `--theme` name is `invalid_input`, and the message lists the valid names. A bad env value, or an unreadable, malformed or wrong-typed file, falls back to the next source with a notification, never a crash; unknown keys in the file are ignored. The file is written only by a confirm in the theme panel, and only when it holds a different value (never for previews, a cancelled panel, `--theme` or env), atomically, with mode 0600. A write failure is a notification. Agent commands ignore the file and the env vars.
- **Theme panel.** A narrow panel docked at the right edge, full height, with no dimming, so the app behind it is the preview. It lists the themes, opens with the cursor on the current one marked `✓`, and has no search.
  - Moving the cursor applies the highlighted theme to the whole app at once, but never writes the file.
  - Enter, or a click on an item, keeps the theme and saves it. Esc, `T` again, or a click outside the panel restores the theme from before it opened. Quitting while the panel is open saves nothing.
  - At the bottom are fixed-height lines, which never wrap and don't move the panel: an info line (`your terminal's colours` on `terminal-*`, blank otherwise), then `↑↓ preview` and `enter keep  esc cancel`.
  - The view behind the panel pauses while it's open, the same as under the help screen, and catches up the moment it closes.
- **Colour contrast.** Table header text reaches at least 4.5:1 against its background in every theme: black or white, whichever contrasts more. In terminal themes, table headers use the terminal's default colours, bold and underlined, since any other pair of palette slots can be unreadable in some palette.
- **snapshot**: one sample with the same numbers as the main view and header, apps sorted by TOTAL (Linux, apps ≥ 1 MiB) or MEMORY (macOS, unknown last), at most `--limit` (default 50).
- **app NAME**: one app with `processes` and `commands`, each paged by `--limit` (default 100). No match, or every unit gone before it is read: `not_found`, exit 1, with `next` pointing at `appmem snapshot`. Linux resolves (scope, name) exactly like the process view and adds `units` as `{name, label}` objects (raw name, and the systemd-unescaped label), `private_bytes` per process, `kernel_bytes`, `zswap_pool_bytes` and `unattributed_*`. macOS resolves names and ids as in "Grouping".
- **schema**: the index (platform, commands, global flags, format defaults, exit codes, conformance) or one command's detail (flags, args, output schema). Always JSON, for the running platform only; the index's `platform` is `linux` or `darwin`. Every output field carries a short `description`. This deliberately departs from the 0.1.0 claim, whose O4 allows only the five validation keywords; the 0.2 draft's O4a admits `description` as an annotation, which validators ignore. `conformance.extensions` stays empty: no 0.1.0 extension covers it.
- Output: text on a terminal, JSON otherwise; `--json` forces JSON. Sizes are integer bytes (`_bytes`), percentages `_percent`, ages integer `age_seconds`, `taken_at` is RFC 3339 with the local offset. Text reports contain no escape sequences, and names with control characters are shown escaped.
- macOS text reports show four host lines (RAM, Compress, Swap, Pressure) without gauges, rates or Δ, aligned app or process (with its `VIA` column) and command tables, each with a `COMPRESSED` column after `MEMORY`, and a footnote when `*` or `unknown` appears.
- A closed stdout pipe (`| head`) ends quietly with exit `0`.
- `--help` is a standalone cheat sheet: purpose, commands, flags, keys, how to read pressure, one example. Unknown flags and invalid values fail with exit `2` and the accepted form.
- Colour is never the only signal: sort direction uses `▴`/`▾`, deltas use `+`/`-` and `·`, pressure is a word. `NO_COLOR` is honoured.

### JSON documents

Both platforms share one envelope:

- `taken_at`, `system`, `pressure`, and `apps` as `{items, has_more}`.
- `next`: the `appmem app` argv for the first (largest) item, omitted without items, repeating `--json` when the call passed it.
- `app NAME` documents are flat: the app's own fields, then `processes` and `commands`, each `{items, has_more}`.
- `system.swap_in_bytes` and `swap_out_bytes`: nullable lifetime counters (see Definitions). A one-shot command has no rates; diff two snapshots.

Linux (no `platform` key):

- `system` has the RAM and swap totals, `system_services_*`, `elsewhere_bytes` and the zswap fields:
  - `zswap_enabled`;
  - `zswap_pool_bytes` and `zswapped_bytes`, both null without zswap;
  - `zswap_writeback_bytes`, cumulative since boot, null only on kernels without the counter;
  - `zswap_compressor`, `zswap_max_pool_percent` and `zswap_compression_ratio` (zswapped / pool, null when either is 0), all null without zswap.
- Each app item has RAM, SWAP, TOTAL, CACHE and ZSWAP bytes, `kernel_bytes` (without the zswap pool) and `top_commands`: its 3 largest commands by TOTAL (`name`, `total_bytes`, `procs`, grouped as in `app NAME`), empty for apps with 6 or fewer processes.
- `next` adds `--scope system` for a system service.

macOS (`platform: "darwin"` in every document):

- `system` has the native counters (`free_bytes` keeps its native meaning) and the partition fields `used_excluding_file_backed_bytes` and `free_excluding_speculative_bytes`, null when the partition is unknown.
- `pressure` is `level` (null when unknown), `source` and `unavailable` (the reason).
- App items have `id`, `name`, nullable `footprint_bytes`, `resident_bytes` and `compressed_bytes` (part of the footprint, not on top of it), `procs` and `coverage` (readable/unreadable counts per metric, `partial`, `resident_partial`, `compressed_partial`, `grouping_partial`); `next` uses the `id`.
- Processes have `pid`, `start_abstime`, `name`, nullable footprint, resident and compressed, `unavailable` and `via` (`bundle`, `ancestry`, `responsible` or `root`: the rule that placed the process in the app, see "Grouping"); commands have all three sums with their own counts.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success, or quit with `q`/`Ctrl+C` in the live view |
| `1` | Runtime or context failure: `cgroup_unavailable`, `not_found`, `terminal_required`, `platform_unavailable`, `read_failed` |
| `2` | Invalid call: `invalid_input` |
| `130`, `143` | Interrupted by SIGINT or SIGTERM (`interrupted` for `snapshot`/`app`) |

Every failure writes one JSON error object as the last non-empty stderr line, never on stdout:

```json
{"error": {"kind": "not_found", "message": "no app named 'nosuch' in scope 'user'", "action": "agent", "hint": "Names are as listed by appmem snapshot; system services need --scope system", "next": ["appmem", "snapshot"]}}
```

`kind` is stable; `hint` and `next` (the recovery command) appear where they help.

## Errors

- `cgroup_unavailable` (Linux), exit `1`: no cgroup v2 at `/sys/fs/cgroup`, no `user@$UID.service` tree (e.g. run as root), or the memory controller is not enabled there (`memory.stat` missing). The message names the missing path. In the live view this can also happen mid-run, when the user tree disappears; the JSON line is printed after the terminal is restored.
- `platform_unavailable`, exit `1`: neither Linux nor macOS, or a Mac without Apple Silicon, a native arm64 Python or macOS 15.
- `read_failed` (macOS), exit `1`: host counters or the process list could not be read for `snapshot`, `app` or the live view's start; a retry may succeed. In a running live view it is a stale tick instead.
- `SIGINT`/`SIGTERM` from outside (e.g. `kill`) in the live view: restore the terminal and exit promptly (not at the next tick) with the usual `128 + signal` code, with no JSON line. A signal that lands during a read exits once that read returns.
- Swap disabled on Linux: SWAP columns show `0` and the Swap row says `off`. The tool still runs.

## Tech

- Python ≥ 3.12, `uv`, [Textual](https://textual.textualize.io/).
- Modules:
  - `cli`: per-platform parsers, dispatch, error line, exit codes; `schema` and `darwin_schema`: the descriptors and output schemas they and `appmem schema` are built from.
  - Linux: `backend` (collection protocol, `select_backend`), `collect` (`LinuxBackend`, reading `/sys` and `/proc` under a fixture-injectable root), `model` (immutable values, grouping math), `naming`, `command_name`, `report` and `render` (documents, text reports).
  - macOS: `darwin_native` (`ctypes` reads, no OS call on import), `darwin_backend` (`DarwinBackend`, inventory and grouping, native reader injected), `darwin_report`.
  - Shared: `rate` (rate window), `total` (run totals), `fmt`, `theme`.
  - UI: `ui/app.py`; `ui/screens/main.py`, `processes.py`, `help.py` (Linux) and `darwin.py` (macOS); `ui/screens/live.py` (shared refresh lifecycle); `ui/host_grid.py` (the grid), `ui/header.py` and `ui/darwin_header.py` (rows), `ui/host_panel.py`; `ui/table.py`, `ui/rows.py`, `ui/process_rows.py`, `ui/darwin_rows.py`, `ui/layout.py`, `ui/theme_picker.py`.
- CLI reports and the TUI screens use the same backend instance. The screens keep refresh timing and display state; accounting and grouping stay in the backends.
- Textual notes for the implementer:
  - `RowTable` (`appmem.ui.table`) posts a `HeaderSelected` event on a header click; `reorder(ordered_keys)` puts rows in that order without rebuilding them. Sort state, the `▴`/`▾` marker and flip-on-second-click are ours to write.
  - Update cells in place with `update_cell`, and add or remove rows only for apps that appeared or vanished. Rebuilding the table every tick causes flicker and loses the cursor.
  - After a sort, `move_cursor` to the selected app's row key.
  - Textual binds `Ctrl+C` to a "no longer quits" notice by default. Rebind it to quit.
  - Exit with `sys.exit(app.return_code or 0)`.
  - All tables are `RowTable`, a hand-written `ScrollView` (Line API), not a `DataTable`, whose single render cache any changed cell invalidates. `RowTable` caches one `Strip` per row, built from each cell's `.plain`/`.justify`/`.style`, never markup; `update_cell` rebuilds only that row, and the cursor row is rebuilt on every `render_line`, since its look depends on focus. It uses no private `DataTable` attribute.

Performance budget: the Linux collector stays under 1 % of one CPU core at a 1 s interval; `appmem snapshot` takes about 0.25 s including interpreter start.
The live view's cost is mostly the table repaint, so it grows with the number of visible rows.

## Tests

No test reads the live system or the real config: Linux tests use fixture trees under an injected root, macOS tests a fake native reader.

- Grouping: the Linux acceptance table, plus escapes, unknown shapes and empty names; macOS bundles, the `.framework` rule, ancestry, the responsible-process rule (accepted, self-responsible, outside the sample, started later, symbol missing, terminal-launched child), merged roots, twins, PID reuse and partial grouping.
- Unit walk: fixture tree with nested sub-cgroups (Konsole tabs, `system-cups.slice/cups.service`) and ignored `*.socket`/`*.mount` directories.
- Collectors: fixture trees for `memory.stat`, `memory.swap.current`, `/proc/PID/*` (including names with spaces and parentheses), missing files, a process vanishing mid-read; macOS native decoding and failure classification, including the compressed read (struct layout, port release on every path, refused, vanished and short replies).
- Process view math: the `kernel`, `zswap pool` and `unattributed` rows, clamping at 0, grouping by command.
- Formatting: unit boundaries (1023 KiB, 1 MiB, 1023 MiB, 1 GiB) and pressure word thresholds.
- Header and panel: grid positions per width and totals, the RAM partition, rates, run totals and the Zswap gauge.
- UI: Textual pilot tests on both platforms for sorting, the process view, `g`, drill-down, the status line, narrow layouts (80x24, 60 columns), the help screen and the host panel.
- CLI: exit codes and the JSON error line for every kind; the terminal-context rules; parser-versus-descriptor parity; every emitted document validated against its published output schema.
- Docs: README and the skill name only commands, flags and error kinds that exist; the skill defines no fields, every output field described.
- macOS CI runs the suite on macOS 15 ARM64, a probe checking the `ctypes` layouts against a compiled C helper, and an installed-wheel run on Python 3.12 and 3.14 that drives `schema`, `snapshot`, `app`, paging, unsupported flags and the live view in a pseudo-terminal against real processes.

## Later (not v1)

- `watch`: an NDJSON stream of snapshots for agents that want growth over time.
- Per-app swap-in/swap-out rate (`pswpin`/`pswpout` from `memory.stat`) to answer "is this app thrashing right now".
- `record` + `history` for tracking slow growth over hours or days.
- Terminal children as their own main-view rows.
- Login-session scopes.
