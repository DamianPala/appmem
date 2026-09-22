# appmem: spec v0.2

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

- One command, `appmem`, opens the live view straight away.
- Refresh every 1 s (`-i/--interval SECONDS` to change).
- One row per app, sortable by clicking a column header or by key.
- Enter on a row opens the app's process view, which can also group processes by command.
- A `?` screen explaining the numbers.
- Terminal only.

**Not in v1:** JSON output, recording/history, charts, CPU or I/O stats, killing processes, config files, login-session scopes, running as root, macOS/Windows, cgroup v1.

## Screens

### Main view

```
RAM 19.9 / 30.9 GiB  avail 11.0   Swap 20.8 / 32.0 GiB   memory pressure: none   system 0.5 GiB (x)
Δ since 14:02 (5m)

  APP              SWAP      RAM       TOTAL ▾   ΔSWAP     ΔRAM      PROCS
  ghostty          10.9 GiB  6.0 GiB   16.8 GiB  +2 MiB    +60 MiB     278
  plasma           2.2 GiB   1.4 GiB   3.6 GiB   0         +12 MiB      32
  chrome           1.2 GiB   2.6 GiB   3.8 GiB   +35 MiB   +480 MiB     37
  code             896 MiB   1.8 GiB   2.6 GiB   0         +4 MiB       32
  ...

 click header / s r t d  sort   enter  processes   x  system   z  reset Δ   ?  help   q  quit
```

- **Header line 1:**
  - System RAM used (`MemTotal - MemAvailable`) and available.
  - System swap used and total, or `Swap off` when `SwapTotal` is 0.
  - Memory pressure as a word (see Definitions), omitted when `/proc/pressure/memory` does not exist.
  - Total of the hidden system services, so the user notices when the culprit is there.
- **Header line 2:** when the Δ baseline was taken and how long ago.
- The sort marker `▴`/`▾` sits on the sorted column. Default sort: TOTAL descending.
- Clicking the sorted column again flips the direction.
- Rows with equal values keep a stable order by app name.

### Process view (after Enter)

```
ghostty   278 procs   swap 10.9 GiB   RAM 6.0 GiB             g  group by command   esc  back

      PID  NAME       SWAP      RAM       TOTAL ▾   AGE   UNIT
     6091  ghostty    3.3 GiB   181 MiB   3.5 GiB   86d   app-com.mitchellh.ghostty.service
  1504671  ghostty    591 MiB   18 MiB    609 MiB   6d    app-ghostty-surface-transient-1504671.scope
  2026292  claude     136 MiB   258 MiB   394 MiB   2h    app-ghostty\x2d2@7b755c4e18184688b9c5e64a8ceb245d.service
  ...
           other      857 MiB   297 MiB   1.1 GiB         (held by the app, not by any process)
```

With `g` (group by command):

```
  NAME       SWAP      RAM       TOTAL ▾   PROCS
  ghostty    4.2 GiB   ...                    12
  node       1.7 GiB   ...                    40
  claude     1.3 GiB   ...                     8
  npm        800 MiB   ...                    22
  ...
```

- The process view opens sorted by the same column as the main view (SWAP stays SWAP, TOTAL stays TOTAL).
- SWAP, RAM and TOTAL are per-process values (see Definitions).
- The `other` row is the app row minus the sum of process rows, clamped at 0. It shows memory the app holds without any process mapping it. Without that row the gap would look like a bug.
- UNIT is the last column and is never truncated, so it can be copied into `systemctl --user`. The table scrolls sideways when it doesn't fit.
- Both layouts refresh with the same interval as the main view.

### Help screen (`?`)

A static screen with the definitions below in plain words.
It covers what RAM, CACHE, SWAP, TOTAL and pressure mean, why rows don't add up to the header, why a closed app can still have a row, and how to act on what you see.

## Keys

| Key | Action |
|---|---|
| click header | sort by that column, click again to reverse |
| `s` / `r` / `t` / `d` | sort by SWAP / RAM / TOTAL / ΔSWAP (repeat to reverse); other columns sort by click |
| `↑` `↓` `PgUp` `PgDn` | move |
| `Enter` | open the process view for the selected app |
| `g` | process view: toggle grouping by command |
| `Esc` | back to the main view |
| `c` | toggle the CACHE column |
| `x` | toggle system services |
| `z` | reset the Δ baseline to now |
| `?` | help screen |
| `q` / `Ctrl+C` | quit |

## Data sources

All reads are plain, world-readable files. No root needed.

| What | Source |
|---|---|
| App units | see "Finding units" below |
| SWAP per app | `memory.swap.current` of the unit |
| RAM per app | `memory.stat` of the unit: `anon + shmem + kernel` |
| CACHE per app | `memory.stat` of the unit: `file - shmem` |
| Processes of an app | `cgroup.procs` of the unit and every directory below it |
| SWAP per process | `/proc/PID/status` → `VmSwap` |
| RAM per process | `/proc/PID/status` → `RssAnon + RssShmem` |
| Process name | basename of `/proc/PID/cmdline` first field, `/proc/PID/comm` when cmdline is empty |
| Process age | `/proc/PID/stat` field 22 (`starttime`), parsed after the last `)` because names may contain spaces and parentheses |
| System totals | `/proc/meminfo`: `MemTotal`, `MemAvailable`, `SwapTotal`, `SwapFree` |
| Pressure | `/proc/pressure/memory`, `avg10` of `some` and `full` |
| Hidden system total | `memory.stat` and `memory.swap.current` of `/sys/fs/cgroup/system.slice` |

PROCS is the line count of `cgroup.procs`, not `pids.current`, which counts threads.

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
- **SWAP = memory.swap.current.** With zswap enabled this includes pages held compressed in RAM. v1 does not separate them.
- **TOTAL = SWAP + RAM.**
- **Per-process RAM and SWAP** come from `/proc/PID/status`, the same numbers htop uses. They are readable for every process, including sandboxed browser processes and other users' processes.
  They don't add up to the app row, for two reasons. A shared page counts once in every process that maps it, so rows can add up to more than the app. Memory the app holds without any process mapping it (GPU buffers, memfd, tmpfs) belongs to no process, so rows can fall short. The `other` row shows the shortfall.
- **Memory pressure** is the share of time tasks waited for memory over the last 10 s:
  - `none`: `some avg10` < 1 %
  - `high`: `full avg10` > 5 %
  - `some`: anything in between, shown with the value, e.g. `some (3.2 %)`

  Big swap with pressure `none` means idle pages were paged out, and memory is not why the machine is slow right now.
- **Δ** is the change since the baseline shown in the header: appmem start, or the last `z`. Apps that appear later count from their first sample.

## Grouping: unit → app name

Several units can belong to one app: many Ghostty windows, LibreOffice helper units, Chrome launched in two different ways.
Rows are merged by app name, and the counters are summed.

Normalization, in order:

1. Take the unit directory name.
2. Unescape every systemd `\xNN` escape (`\x2d` → `-`).
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
- A row can have PROCS 0 and memory above 0: the unit outlives its processes while it still holds memory. It shows like any other row, and its process view shows only the `other` row.
- The cursor follows the selected app across refreshes and re-sorts. If that app disappears, the cursor stays at the same row index, or on the last row.

## Command line

```
appmem [-i SECONDS] [--system]
appmem --help | -h
appmem --version | -V
```

| Flag | Default | Meaning |
|---|---|---|
| `-i`, `--interval SECONDS` | `1` | Refresh interval, number ≥ 0.2 |
| `--system` | off | Start with system services shown (same as `x`) |

Follows the house CLI Design Standard 0.1.0 where it applies to an interactive-only tool.
Full conformance (`schema`, `--json`) comes with `snapshot --json`.

- `--help` is a standalone cheat sheet: purpose, flags, keys, how to read pressure, one example.
- Unknown flags and invalid values fail before the TUI starts, with exit `2` and the accepted form.
- The TUI starts only when stdin and stdout are both terminals. Otherwise appmem exits `2` and says so, instead of drawing escape codes into a pipe.
- Colour is never the only signal: sort direction uses `▴`/`▾`, deltas use `+`/`-`. `NO_COLOR` is honoured.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Quit with `q` or `Ctrl+C` |
| `1` | Runtime failure (no usable cgroup v2 tree) |
| `2` | Usage error, or not running in a terminal |

For the pre-start failures, where a script may be the caller, the last stderr line is one JSON error object:

```json
{"error":{"kind":"not_a_tty","message":"appmem needs an interactive terminal on stdin and stdout","action":"user"}}
```

Error kinds: `usage`, `not_a_tty`, `cgroup_unavailable`.

## Errors

- `cgroup_unavailable`, exit `1`: no cgroup v2 at `/sys/fs/cgroup`, no `user@$UID.service` tree (e.g. run as root), or the memory controller is not enabled there (`memory.stat` missing). The message names the missing path.
- `SIGINT`/`SIGTERM` from outside (e.g. `kill`): restore the terminal and exit with the usual `128 + signal` code, with no JSON line.
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
Measure UI repaint cost in the first build and state it.

## Tests

- Grouping: the acceptance table, plus escapes, unknown shapes and empty names.
- Unit walk: fixture tree with nested sub-cgroups (Konsole tabs, `system-cups.slice/cups.service`) and ignored `*.socket`/`*.mount` directories.
- Collectors: fixture trees for `memory.stat`, `memory.swap.current`, `/proc/PID/*` (including names with spaces and parentheses), missing files, a process vanishing mid-read.
- Process view math: the `other` row, clamping at 0, grouping by command.
- Formatting: unit boundaries (1023 KiB, 1 MiB, 1023 MiB, 1 GiB) and pressure word thresholds.
- UI: Textual pilot tests that sort by header click, open the process view, toggle `g`, and return.
- CLI: exit codes and the JSON error line for a bad flag, a non-TTY run and a missing cgroup tree.

## Later (not v1)

- `appmem snapshot --json` and `appmem schema` for scripts and agents, with full CLI standard conformance.
- Per-app swap-in/swap-out rate (`pswpin`/`pswpout` from `memory.stat`) to answer "is this app thrashing right now".
- `record` + `history` for tracking slow growth over hours or days.
- Terminal children as their own main-view rows.
- Login-session scopes.
