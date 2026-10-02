# appmem

On Linux, find out which apps are eating your RAM and swap. On Apple Silicon, compare application footprints. Live, in the terminal, one row per app.

On Linux, the laptop starts swapping, the fan spins up, and `htop` shows forty processes called `chrome`, thirty called `node` and something called `Isolated Web Co`.
appmem shows the same memory as the apps you actually opened: Chrome, Firefox, VS Code, your terminal, each with its RAM, its swap and how much it grew since you started looking.
The header tells you whether the swapping is a problem at all, and one key opens the processes behind any row.

![appmem's main view: one row per app, sorted by TOTAL](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/main.svg)

On Linux, you can also skip the reading and hand the job to an AI agent.
Paste this into Claude Code, Codex or any agent that runs shell commands:

```
On Linux, install appmem (uv tool install appmem), read
https://raw.githubusercontent.com/DamianPala/appmem/main/skills/appmem/SKILL.md
and tell me what is eating my memory and what to close.
```

The agent installs the published Linux build, reads the numbers the way the skill file describes and tells you what to close.
What the agent gets is in [For agents](#for-agents).

For Linux, reach for it when:

- the machine got slow and you want the culprit, not a process list;
- swap is full and you want to know whether that matters right now;
- a "terminal" holds 17 GiB and you want to know what is really running inside it;
- you want to watch one app grow while you use it.

## Install

For the published Linux release:

```
uv tool install appmem
appmem
```

No root, no config file.
From a checkout, `uv run appmem`.

The published 0.2.0 package supports Linux. You need Linux with systemd and a desktop that starts apps as systemd units (KDE Plasma and GNOME do), on cgroup v2, which current distributions use by default.
Python 3.12 or newer; uv fetches it for you.
Kernel 5.10 or newer; the zswap figures need 5.19 and the Pressure line needs pressure tracking (PSI) switched on in the kernel, without them those parts stay off and the rest works.

### Experimental macOS support in the development branch

The `feat/platform-backends` development branch adds experimental support for macOS 15 or newer on Apple Silicon. It has not been released to PyPI; `uv tool install appmem` still installs the Linux-only 0.2.0 package. From a checkout of that branch, run `uv tool install .` to install this build.

The Mac view ranks apps by **physical footprint**, with `APP`, `MEMORY`, `ΔMEM` and `PROCS`, plus `RESIDENT` from 100 columns. MEMORY is the native footprint reported for captured processes, including native accounting for compressed memory, not resident RAM, memory you will necessarily reclaim by closing an app, or an exact Activity Monitor total. `*` marks an app whose known total is partial because some process footprints are unreadable; `?` means none were readable. Growth stays unknown until that app has a complete sample, which establishes a zero baseline. A partial current sample shows unknown; when coverage recovers, growth compares with the retained complete baseline. Vanished or reopened apps start a new baseline. Bundles are grouped by their outermost `.app` path, and bundleless processes follow the nearest app ancestor or a separate session root; missing ancestry can leave grouping partial. Same-named copies at different paths remain separate. Shared XPC and WebKit services started by launchd can appear as separate root rows, so a Safari or other app row may omit related service footprints. This is a grouping limit, not an estimate of memory reclaimed by closing the app; physical desktop validation is still pending.

The Mac header has four rows: RAM used/physical with file-backed/free and wired/purgeable metadata where it fits, physical compressed RAM aligned with primary values, logical data and ratio in metadata, global swap, and native pressure with separately labelled app scope and growth baseline time. RAM used is **physical minus (native free minus speculative) minus file-backed**; it includes reserved/unaccounted memory. Free excludes speculative, which is already included in file-backed. File-backed is not all immediately available, and purgeable is an overlapping part of used. Inconsistent counters show unavailable. The swap gauge compares used with **currently allocated space**, which grows dynamically; zero reads `0 B; not allocated` beside a neutral placeholder where a gauge fits. Native pressure is a kernel state (`normal`, `warning`, `critical`, or unavailable), not a RAM percentage. The primary layout targets 120×30. At 80×24, RAM retains its gauge and file-backed/free before wired/purgeable; narrower layouts retain main values and units. The baseline reads `Δ since HH:MM (1h12m)` and `b reset Δ` resets it.

RESIDENT includes shared/file-backed pages and may double count between processes. It is not additive with MEMORY and their difference is not swap. Its own readable/unreadable coverage is retained; partial known sums use `*`, no readable members use `?`. Appmem cannot attribute per-app swap, cache or compression on macOS. The Mac view has process and command drill-down (`Enter`, then `g` to group); `f`/`d`/`r` sort memory, growth or resident when visible, and `b` resets growth and its displayed timing. Individual app baselines can be newer than the displayed session/reset epoch after identity or coverage changes. Hiding the sorted column on resize returns to MEMORY descending. `--system` and `--scope system` return an input error on Mac.

Help (`?`) has structured definitions and keys in a focused scroll area, with a visible close hint. Terminal `snapshot` uses four host rows without gauges or session Δ, followed by aligned app columns with binary units; `app` uses aligned process and command columns. `unknown` and partial coverage remain explicit. Host RAM is physical usage and app MEMORY is footprint; these categories do not sum to one another.

Agents can use `appmem schema`, `appmem snapshot --json`, and `appmem app ID --json` from this development build. The Mac JSON contract has `platform: "darwin"` and nullable `footprint_bytes` and `resident_bytes` with separate readable/unreadable coverage counts, plus native backing counters and the validated derived RAM partition. Linux retains its existing values with additive nullable host swap counters. `platform_unavailable` means this Mac build is running on an unsupported OS or processor, or a required native read failed.

## Using it on Linux

The main view sorts by TOTAL (RAM + swap).
Press the first letter of a column (`r`, `s`, `t`) or click a header to sort by something else.
The Pressure figure in the header tells you whether memory is a problem right now: `none` with a full swap only means idle pages were moved out of the way, `some` or `high` means programs are waiting for memory.

`Enter` on an app shows its processes, their age and the systemd unit each one lives in.
The bottom line has the command ready for the selected row, for example `systemctl --user stop 'app-firefox.service'` or `kill 41233`.

Everything you start from a terminal counts as the terminal.
Press `g` in the process view to group by command.
That is how a "terminal holding 17 GiB" turns out to be eighteen `claude` processes plus the terminal itself, running for 86 days with 3.4 GiB in swap.

![The process view, grouped by command: several claude processes collapse into one row](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/processes.svg)

ΔSWAP and ΔRAM show how each app grew or shrank since you started appmem (`b` resets the baseline).
`x` adds system services, `c` shows the CACHE column.

`T` opens a theme panel with a live preview: Textual's built-in themes plus `terminal-dark` and `terminal-light`, which use your terminal's own colours.
The theme you keep is saved to `~/.config/appmem/config.toml`; `appmem --theme NAME` or `APPMEM_THEME` override it for one run.

![The theme panel open on dracula, live-previewed on the main view behind it](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/theme-panel.svg)

`h` on either main dashboard opens live, scrollable host memory details; `h` or Esc closes it and preserves the table position. A dim `…` at the right edge (`>` in ASCII) means host information was hidden for space, while unavailable means a missing reading. The panel uses the same samples and refresh cadence, and marks retained data after failed reads.

`?` inside the app explains every number and lists the keys; `appmem --help` has the same list plus the options.

## How to read the Linux numbers

RAM is what the app is using right now.
SWAP is what the system moved out of RAM to make room: onto the disk or, with zswap, into a compressed corner of RAM.
TOTAL is RAM + SWAP, one number to sort by.
Closing the app gives you back less than that: memory shared with other apps stays, and freed swap is mostly space on the disk, not RAM.
CACHE (press `c`) is file data the kernel keeps around because something read or wrote it recently.
It is not part of RAM or TOTAL: the kernel can drop it when it needs the space, and an app that just read a big file would otherwise look like a memory hog.
The rows don't add up to the header, because system services, virtual machines, containers and other users hold the rest.

The Linux header targets 120×30, with compact 80×24 and wide 160×40 layouts. With zswap enabled, RAM/Zswap/Swap/Pressure are separate rows, including below 80 columns when height permits. Labels, gauges or pressure state, primary values and metadata follow one grid; short terminals mark omitted host details. Zswap shows its physical RAM pool against an approximate configured policy limit, plus the logical data it holds. Swap used includes the size before compression; RAM used includes the pool after compression. For 5.7 GiB held in 1.4 GiB RAM, those amounts are already included, so do not add them again. The per-app ZSWAP column is logical; the gauge is physical. Zswap does not expand swap capacity; occupied swap slots are not bytes solely on SSD.

Linux shows both neutral Swap in/out rates from 80 columns. Compact Mac prioritizes `allocated now`, so at 80 columns its `out` rate is hidden and marked with `…`; `h` shows both rates on either platform. Rates are averaged over about 10 seconds. Unknown means insufficient or unavailable samples; 0 B/s means measured zero. Linux counts swap-device activity, including zram and excluding successful zswap hits. macOS counts page-rounded compressed segment transfers to/from swap files, including housekeeping, not logical app bytes. Neither measures SSD throughput. Wide Linux views may show writeback, already included in out. Growth reset leaves rates running; snapshots expose lifetime counters in JSON without rates or session Δ.

## For agents

On Linux, the prompt at the top is all an agent needs. The agent gets the same data as the TUI as JSON, and [skills/appmem/SKILL.md](skills/appmem/SKILL.md) tells it how to read it: pressure against swap, what hides inside a terminal, what to recommend. The skill also describes the Mac workflow for the development build.

```
appmem snapshot [--system] [--limit N] [--json]
appmem app NAME [--scope user|system] [--limit N] [--json]
appmem schema [COMMAND]
```

`snapshot` covers the machine and every app, `app NAME` one app's units, processes and commands.
Both print text on a terminal and JSON when piped; `--json` forces JSON.
`appmem schema` describes the commands, flags, output fields and exit codes as JSON.
Every error is one JSON object on the last line of stderr with a stable `kind` such as `not_found`; exit code 1 is a runtime failure, 2 an invalid call.
Bare `appmem` without a terminal exits 1 and points to `appmem snapshot`.

## Cost on Linux

Reading the counters once a second takes about 1 % of one CPU core on Linux.
The Linux live view takes 3 to 12 % of one core on a busy desktop, more with a tall terminal and many apps.
Refreshing every two seconds instead of every second (`appmem -i 2`) cuts that in half.
Mac sustained CPU cost has not been measured.

## Development

```
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

The screenshots come from fixture data, never a real machine; regenerate them with `uv run python scripts/screenshots.py`.

## License

MIT
