# appmem: spec v0.12

A live terminal view of RAM and swap usage **per application**, not per process.
Think `btm` or `htop`, but rows are apps (Ghostty, Brave, LibreOffice), each summing all of its processes.

## Why

`htop`, `btm` and `swaptop` show processes.
A browser or a terminal is dozens of processes, so the question "which app is eating my swap?" needs mental math.
systemd already puts every desktop app into its own cgroup, and the kernel keeps per-cgroup memory and swap counters.
appmem reads those counters, groups them by app, and shows a sortable live table.

The user it is built for: the machine feels slow, htop shows a wall of processes, and they want three answers in seconds.
Which app holds the memory and swap? Is memory actually the problem right now? What exactly should I close?

## Scope

**In v1:**

- `appmem` opens the live view straight away.
- Refresh every 1 s (`-i/--interval SECONDS` to change).
- One row per app, sortable by clicking a column header or by key.
- Enter on a row opens the app's process view, which can also group processes by command and drill into one command.
- A `?` screen explaining the numbers.
- Themes: Textual's built-in themes, with `terminal-dark`/`terminal-light` for the terminal's own colours. `T` opens a side panel with a live preview, and the choice is remembered in a config file.
- Non-interactive commands for scripts and agents: `appmem snapshot`, `appmem app NAME`, `appmem schema`, plus an agent skill in `skills/appmem/SKILL.md`.

**Not in v1:** recording/history, charts over time, a streaming `watch` command, CPU or I/O stats, killing processes, settings other than the theme, custom themes (the terminal themes cover a tuned terminal palette), login-session scopes, running as root, macOS/Windows, cgroup v1.

## Screens

### Main view

```
RAM       ██████████████▎░░░░░  22.1/30.9 GiB used (3.5 shared)    avail  8.8 GiB (1.5 free, 4.6 cache, 3.2 slab)
Swap      ██████████████▋░░░░░  23.3/32.0 GiB used (3.0 zswapped into 1.0 GiB RAM)     to disk 12 MiB/s
Pressure  none                          system 610 MiB [x]    Δ since 15:58 (12m)      elsewhere 109 MiB
 APP                        RAM         SWAP        ZSWAP       TOTAL ▾     ΔRAM       ΔSWAP      PROCS
 ghostty                       6.6 GiB    11.2 GiB     1.6 GiB    17.8 GiB     -3 MiB          ·     281
 plasma                        1.6 GiB     2.3 GiB     100 MiB     3.9 GiB          ·          ·      17
 chrome                        1.6 GiB     2.2 GiB     907 MiB     3.7 GiB     -1 MiB          ·      35
 ...
 r s t d z sort  enter procs  x system  c cache  w zswap  b reset Δ  T theme  ? help  q quit
```

- **Header: three lines, each a labelled gauge** (`RAM`, `Swap`, `Pressure`, labels in a 10-cell column):
  - **RAM:** a bar (used vs total), then used/total in the total's unit, `used`, `(N shared)`; then `avail` with `(free, cache, slab)`.
    - used = `MemTotal - MemAvailable`.
    - `shared` = `Shmem`: tmpfs such as `/tmp` and `/dev/shm`, shared memory, GPU buffers. The kernel can only swap it out, never drop it.
    - `avail` = `MemAvailable`, what can be allocated before swapping.
    - `free` = `MemFree`; `cache` = `Cached - Shmem`, clamped at 0, the same definition as the CACHE column; `slab` = `SReclaimable`, kernel caches of file names and inodes, dropped on demand.
    - The three come close to `avail` but are not an exact sum: `avail` is a kernel estimate that also keeps reserves.
  - **Swap:** a bar and used/total, or `Swap off` when `SwapTotal` is 0. The pair turns the theme's error colour above 90 % used; a full swap alone is normal, pressure says whether it hurts.
    - With zswap enabled, `(X zswapped into Y RAM)` follows. X is swapped data kept compressed in RAM (`Zswapped`, already part of Swap used); Y is the RAM the pool takes (`Zswap`, already part of RAM used). Without zswap, nothing is shown.
    - While the pool writes back to the disk swap, `to disk N MiB/s` follows, in the theme's warning colour. The rate comes from `/proc/vmstat` `zswpwb` over a ~10 s window. It is shown only while above 0, and never on the first tick.
  - **Pressure:** the pressure word, bold, in the theme's success/warning/error colour (see Definitions), or `unavailable` without `/proc/pressure/memory`. The word sits in the bar column, so the next part starts in the same column as the RAM and Swap pairs whenever the longest word fits (from 90 columns). Narrower, it keeps a fixed slot that depends only on the width. Then the hidden system services' total (`system N [x]`), the Δ baseline (`Δ since 14:02 (37m)`, whole days from 100 h on; shown only while the Δ columns are, from 95 columns), and `elsewhere` (memory outside the user tree and `system.slice`: VMs, containers, other users, login sessions).
  - **Bars:** `█` fill with an eighth-block edge on a dim `░` track. The width steps with the terminal width. The fill uses the theme's accent colour, or its foreground where the accent is under 3:1 contrast. A `#`/`.` ASCII form applies when `LC_ALL`/`LC_CTYPE`/`LANG` names a non-UTF-8 locale. A bare `LANG=C` still gets Unicode bars, because Python coerces it to UTF-8 at startup. Bars carry no information that the numbers don't.
  - **No jitter.** Every part sits in a fixed-width slot sized for its worst case (from the totals, or a literal worst case for rates and durations). Which parts show depends only on the terminal size and state (zswap on, writeback active), never on the values. Nothing moves when only the numbers change.
  - **Narrow widths.** Each line drops its own parts, least important first:
    - RAM: the `(free, cache, slab)` breakdown, then `avail`, then `shared`;
    - Swap: the pool part of the bracket (`into Y RAM`), then the bracket;
    - Pressure: `elsewhere`, then Δ, then `system`.
    The label, the pair and the pressure word always stay; `to disk` stays in the three-line form. Below the width of the fixed parts, a line is cropped with an ellipsis. The pressure word is never cropped.
  - **Short terminals.** Below 18 rows the header takes two lines:
    - From 70 columns, when the RAM line fits with every droppable part removed: line 1 is the RAM line (same drops as above) plus the pressure word in its own slot. Line 2 is the Swap line plus `to disk`, Δ and `system`; it drops the zswap pool part, then the bracket, then Δ, then `system`.
    - Otherwise, a compact form. Line 1 is `RAM u/t  Swap u/t`. Line 2 is `Pressure` word, `to disk`, `system`, as far as they fit (`system` drops first). `to disk` fits from 47 columns, `system` from 48.
    The table takes the freed row.
- The sort marker `▴`/`▾` sits on the sorted column. Default sort: TOTAL descending.
- Clicking the sorted column again flips the direction.
- Rows with equal values keep a stable order by app name.
- Δ below 1 MiB either way shows as a dim `·`; Δ columns render dim while the baseline is younger than 60 s.
- With zswap enabled, the ZSWAP column is shown by default, between SWAP and TOTAL.
- Under 95 columns ΔSWAP and ΔRAM are hidden, and under 85 ZSWAP is hidden too. Sorting by a hidden column falls back to TOTAL descending.
- A footer with key caps sits at the bottom of every view and drops its lowest-priority items instead of wrapping. It shows only keys that act in the current view and mode, labelled by what they do there (e.g. `d` is absent while the Δ columns are hidden). `T theme` is the first item to drop.
- Mouse-wheel scrolling stays where the user put it across refreshes: a tick restores the cursor without scrolling the viewport; only explicit actions (sort, toggles, `b`, drill in/out) scroll the selected row into view.

### Process view (after Enter)

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

### Help screen (`?`)

A scrolling screen with the definitions below in plain words, soft-wrapped to the width, with `esc/?/q close` in its title line.
It covers what RAM, CACHE, SWAP, TOTAL, pressure and the header's shared/free/cache/avail mean (tmpfs files count toward the app that wrote them), why rows don't add up to the header, why a closed app can still have a row, and how to act on what you see, plus one line on `T` and where the theme is saved. When zswap is enabled, it also defines the zswap bracket, `to disk` and ZSWAP. It also explains the bar glyphs (`█` used, `░` what's left).

## Keys

| Key | Action |
|---|---|
| click header | sort by that column, click again to reverse |
| `r` / `s` / `t` / `d` / `z` | sort by RAM / SWAP / TOTAL / ΔSWAP / ZSWAP (repeat to reverse); a key whose column is hidden is absent and does nothing; other columns sort by click |
| `↑` `↓` `PgUp` `PgDn` | move |
| `Enter` | open the process view for the selected app; in grouped mode, the processes of the selected command |
| `g` | process view: toggle grouping by command |
| `Esc` | back to the main view |
| `c` | toggle the CACHE column |
| `w` | toggle the ZSWAP column (main view, only while zswap is enabled; the choice lasts for the session) |
| `x` | toggle system services |
| `b` | reset the Δ baseline to now |
| `T` / `Ctrl+P` → Theme | theme panel (all views, see "Theme panel"); opening it again while open does nothing |
| `?` | help screen |
| `q` / `Ctrl+C` | quit |

## Data sources

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
| System totals | `/proc/meminfo`: `MemTotal`, `MemAvailable`, `SwapTotal`, `SwapFree` |
| Pressure | `/proc/pressure/memory`, `avg10` and `avg60` of `some` and `full` |
| Hidden system total | `memory.stat` and `memory.swap.current` of `/sys/fs/cgroup/system.slice` |
| `elsewhere` | root `/sys/fs/cgroup/memory.stat` minus the `user@$UID.service` tree minus `system.slice` |

PROCS is the line count of `cgroup.procs`, not `pids.current`, which counts threads.

### Process names

Arguments can hold secrets, so a name never shows one, except for this allowlist, which fails closed.
- For `node`, `bun`, `deno`, `npm`, `npx`, `uv`, `uvx` and `python`/`python3`/`python3.N`, with a real NUL-separated argv, the name is `interpreter:target` (`node:mcp-remote`, `npx:@scope/tool`, `python3:http.server`).
- Walking the arguments skips only known verbs (`npm exec/run/x`, `uv tool/run/install`, `bun`/`deno run`) and known value-less flags (python `-u -B -O -E -s -S -I`; node `--no-warnings --enable-source-maps`; npm/npx `-y --yes`).
- The target is a script basename, a `-m` module or a package spec with `@version` dropped. A generic script basename (`index.js`, `main.js`, `cli.js`, `__main__.py`) becomes the package: the directory after `node_modules/`, or the nearest meaningful parent.
- The target must match `[A-Za-z0-9._@/+-]`, at most 40 characters.
- Any other flag (inline code `-e`/`-c`, options with values), a URL, a query string or a failed check gives the bare interpreter name.

### Finding units

Roots: `/sys/fs/cgroup/user.slice/user-$UID.slice/user@$UID.service/{app,session,background}.slice`, plus `/sys/fs/cgroup/system.slice` when system services are shown.

- A **unit** is a directory named `*.service` or `*.scope`.
- Walk down from each root, descending only through `*.slice` directories. Stop at the first unit directory and never descend into it.
- Read counters at the unit directory. They are hierarchical: they already include every sub-cgroup (Konsole keeps one `tab(PID).scope` per tab under its unit).
- Never sum a unit with anything below it.
- Directories of other types (`*.socket`, `*.mount`, `*.swap`) are ignored.

Login-session scopes (`user-$UID.slice/session-N.scope`: the display manager helper, SSH and VT logins) are not shown in v1.

### Definitions

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
- **Δ** is the change since the baseline shown in the header: appmem start, or the last `b`. Apps that appear later count from their first sample.

## Grouping: unit → app name

Several units can belong to one app: many Ghostty windows, LibreOffice helper units, Chrome launched in two different ways.
Rows are merged by app name, and the counters are summed.

Normalization, in order:

1. Take the unit directory name.
2. Unescape every systemd `\xNN` escape (`\x2d` → `-`) and decode the resulting bytes as UTF-8, invalid sequences replaced (`\xe5\xbe\xae\xe4\xbf\xa1` → `微信`).
3. Strip the `.service` / `.scope` suffix.
4. Snap: `snap.<name>.<app>-<uuid>` → `<name>`, then go to step 10.
5. Flatpak: `app-flatpak-<id>-<n>` → `<id>`, then go to step 8.
6. Strip the `app-` prefix.
7. Strip the instance part: `@<anything>`, trailing `-<digits>`, trailing `-<uuid>`.
8. Reverse-DNS IDs: when the name has 3 or more dot-separated labels, drop the first two (`com.mitchellh.ghostty` → `ghostty`, `org.kde.discover.notifier` → `discover.notifier`).
9. Snap desktop IDs: `<x>_<x>` → `<x>` (`thunderbird_thunderbird` → `thunderbird`).
10. Lowercase.
11. Apply the built-in alias map.

Built-in alias map. Exact entries match the whole name, prefix entries match its start:

| Match | Kind | App |
|---|---|---|
| `ghostty-surface-transient` | exact | `ghostty` |
| `brave-browser` | exact | `brave` |
| `google-chrome` | exact | `chrome` |
| `element-desktop` | exact | `element` |
| `superproductivity-bin` | exact | `superproductivity` |
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
| `app-org.chromium.Chromium-4020402.scope` | `chromium` |
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

### Terminals

Anything started from a terminal lives in the terminal's cgroup, so `claude`, `node` or `python` run from Ghostty count as Ghostty.
That is how the kernel sees it.
The process view with `g` shows what is really inside in one screen.
Splitting terminal children into their own main-view rows is v2.

## Behaviour details

- Sizes use binary units and a dot as the decimal separator. GiB gets one decimal, MiB and KiB are integers (`16.8 GiB`, `677 MiB`).
- Numeric columns are right-aligned with fixed widths, so values changing size don't re-flow the table.
- Rows with less than 1 MiB TOTAL are hidden. The CACHE toggle doesn't change which rows show.
- Apps that appear mid-session get a row. Apps that disappear drop out at the next refresh.
- A unit or process that vanishes between listing and reading is skipped silently. This is normal churn, not an error.
- A row can have PROCS 0 and memory above 0: the unit outlives its processes while it still holds memory. It shows like any other row, and its process view shows only the `kernel` and `unattributed` rows.
- A tick whose reads fail transiently (`memory.stat` missing, any `OSError`, a parse error from a half-written `/proc` or `/sys` file) is skipped; the screen keeps the last data and the next tick recovers. Errors while applying the data to the screen are bugs and still end the app. Only a missing user tree ends the app as a runtime failure.
- The cursor follows the selected app across refreshes and re-sorts. If that app disappears, the cursor stays at the same row index, or on the last row.
- Names (apps, processes, units, titles, status line) show C0/C1 control characters escaped (`\x1b[41m`), in the live view as in the text reports; nothing a process or unit is called can write to the terminal.
- Widths count terminal cells, not characters: names are cut at 32 cells with `…` and a wide character is never split. No line of any view wraps at any width. In the main view, the APP column takes only the width left after the numeric columns (capped at 32, never below 8), at every width and again when a scrollbar appears, so numbers are never cut; long names get `…` first.
- Periodic reads run off the UI thread, one at a time per screen; keys stay responsive while a read is slow, and a result read for a view the user has since left is dropped.

## Command line

```
appmem [-i SECONDS] [--system] [--theme NAME]      live view (TUI)
appmem snapshot [--system] [--limit N] [--json]     machine state + apps
appmem app NAME [--scope user|system] [--limit N] [--json]
                                                    one app: units, processes, commands, remainder
appmem schema [COMMAND]                             interface description as JSON
appmem --help | -h
appmem --version | -V
```

The command line conforms to the house CLI Design Standard 0.1.0 (claimed in `appmem schema` under `conformance`).
`appmem schema` and each command's `--help` are the reference for flags, defaults and output fields; this section only fixes the behaviour.

- **Live view** (no command): `-i/--interval` (default 1, ≥ 0.2) and `--system` (start with system services shown). It starts only in a terminal context: stdin and stdout are terminals, no `--json`, `NO_INPUT` unset or empty. Otherwise it exits `2` with `terminal_required` and `next: ["appmem","snapshot"]`, instead of drawing escape codes into a pipe. `-i` or `--theme` together with a command is `invalid_input`, and so is `--system` before `app` or `schema`.
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
- **Colour contrast.** Table header text reaches at least 4.5:1 against its background in every theme: black or white, whichever contrasts more. In terminal themes, table headers use the terminal's default colours, bold and underlined, since any other pair of palette slots can be unreadable in some palette.
- **snapshot**: one sample with the same numbers as the main view and header. Apps ≥ 1 MiB TOTAL, sorted by TOTAL, at most `--limit` (default 50) with `has_more`; `next` names the largest app. zswap fields:
  - `zswap_enabled`;
  - `zswap_pool_bytes` and `zswapped_bytes`, both null without zswap;
  - `zswap_writeback_bytes`, cumulative since boot. A one-shot command has no rate, so diff two snapshots. It is null only on kernels without the counter;
  - `zswap_compressor`, `zswap_max_pool_percent` and `zswap_compression_ratio` (zswapped / pool, null when either is 0), all null without zswap;
  - `zswapped_bytes` per app.

  Each app item also has `kernel_bytes` (without the zswap pool) and `top_commands`: its 3 largest commands by TOTAL (`name`, `total_bytes`, `procs`, grouped as in `app NAME`), empty for apps with 6 or fewer processes.
- **app NAME**: resolves (scope, name) exactly like the process view. `units` as `{name, label}` objects (raw name, and the systemd-unescaped label), `processes` (with `private_bytes`) and `commands` (each paged by `--limit`, default 100), `kernel_bytes`, `zswap_pool_bytes` and `unattributed_*`. No match, or every unit gone before it is read: `not_found`, exit 1.
- **schema**: the index (commands, global flags, format defaults, exit codes, conformance) or one command's detail (flags, args, output schema). Always JSON.
- Output: text on a terminal, JSON otherwise; `--json` forces JSON. Sizes are integer bytes (`_bytes`), percentages `_percent`, ages integer `age_seconds`, `taken_at` is RFC 3339 with the local offset. Text reports contain no escape sequences, and names with control characters are shown escaped.
- A closed stdout pipe (`| head`) ends quietly with exit `0`.
- `--help` is a standalone cheat sheet: purpose, commands, flags, keys, how to read pressure, one example. Unknown flags and invalid values fail with exit `2` and the accepted form.
- Colour is never the only signal: sort direction uses `▴`/`▾`, deltas use `+`/`-` and `·`, pressure is a word. `NO_COLOR` is honoured.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success, or quit with `q`/`Ctrl+C` in the live view |
| `1` | Runtime failure: `cgroup_unavailable`, `not_found` |
| `2` | Invalid call: `invalid_input`, `terminal_required` |
| `130`, `143` | Interrupted by SIGINT or SIGTERM (`interrupted` for `snapshot`/`app`) |

Every failure writes one JSON error object as the last non-empty stderr line, never on stdout:

```json
{"error": {"kind": "not_found", "message": "no app named 'nosuch' in scope 'user'", "action": "agent", "hint": "Names are as listed by appmem snapshot; system services need --scope system", "next": ["appmem", "snapshot"]}}
```

`kind` is stable; `hint` and `next` (the recovery command) appear where they help.

## Errors

- `cgroup_unavailable`, exit `1`: no cgroup v2 at `/sys/fs/cgroup`, no `user@$UID.service` tree (e.g. run as root), or the memory controller is not enabled there (`memory.stat` missing). The message names the missing path. In the live view this can also happen mid-run, when the user tree disappears; the JSON line is printed after the terminal is restored.
- `SIGINT`/`SIGTERM` from outside (e.g. `kill`) in the live view: restore the terminal and exit promptly (not at the next tick) with the usual `128 + signal` code, with no JSON line. A signal that lands during a read exits once that read returns.
- Swap disabled: SWAP columns show `0` and the header says `Swap off`. The tool still runs.

## Tech

- Python ≥ 3.12, `uv`, [Textual](https://textual.textualize.io/).
- Two layers:
  - `collect` reads `/sys` and `/proc` and returns plain dataclasses, with no UI imports. It takes a root path, so tests run against fixture directory trees.
  - `ui` is the Textual app.
- Textual notes for the implementer:
  - `DataTable` provides a `HeaderSelected` event and `sort(key, reverse=)`. Sort state, the `▴`/`▾` marker and flip-on-second-click are ours to write.
  - Update cells in place with `update_cell`, and add or remove rows only for apps that appeared or vanished. Rebuilding the table every tick causes flicker and loses the cursor.
  - After a sort, `move_cursor` to the selected app's row key.
  - Textual binds `Ctrl+C` to a "no longer quits" notice by default. Rebind it to quit.
  - Exit with `sys.exit(app.return_code or 0)`.

Performance budget: the collector stays under 1 % of one CPU core at a 1 s interval.
Measured on the dev machine: 4.5 ms per tick for 146 units, 10 ms for `/proc/PID/status` of all 542 user processes.
Measured with the UI at `-i 1` on the dev machine (2026-09-23, ~310-process app): main view about 4 %, process views about 5 % of one core. `appmem snapshot` takes about 0.2 s including interpreter start.

## Tests

- Grouping: the acceptance table, plus escapes, unknown shapes and empty names.
- Unit walk: fixture tree with nested sub-cgroups (Konsole tabs, `system-cups.slice/cups.service`) and ignored `*.socket`/`*.mount` directories.
- Collectors: fixture trees for `memory.stat`, `memory.swap.current`, `/proc/PID/*` (including names with spaces and parentheses), missing files, a process vanishing mid-read.
- Process view math: the `kernel`, `zswap pool` and `unattributed` rows, clamping at 0, grouping by command.
- Formatting: unit boundaries (1023 KiB, 1 MiB, 1023 MiB, 1 GiB) and pressure word thresholds.
- UI: Textual pilot tests for sorting, the process view, `g`, drill-down, the status line, narrow layouts (80x24, 60 columns) and the help screen.
- CLI: exit codes and the JSON error line for every kind; the terminal-context rules; parser-versus-descriptor parity; every emitted document validated against its published output schema.
- Docs: README and the skill name only commands, flags and error kinds that exist.

## Later (not v1)

- `watch`: an NDJSON stream of snapshots for agents that want growth over time.
- Per-app swap-in/swap-out rate (`pswpin`/`pswpout` from `memory.stat`) to answer "is this app thrashing right now".
- `record` + `history` for tracking slow growth over hours or days.
- Terminal children as their own main-view rows.
- Login-session scopes.
