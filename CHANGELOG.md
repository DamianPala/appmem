# Changelog

## [Unreleased]

### Added

- macOS: a COMPRESSED column, from 80 columns, in the main table and the process view, sortable with `c`. It is the part of each app's MEMORY that macOS holds compressed, in RAM or swapped out, so it answers "which app is being squeezed". It is already inside MEMORY, not added on top. `?` means no process of the app could be read, `*` a partial sum. The `snapshot` and `app` JSON gain `compressed_bytes` (per app, process and command) and the matching coverage counts, and the text reports gain a `COMPRESSED` column
- macOS: each process of an app shows which rule put it there (`via`: `bundle`, `ancestry`, `responsible` or `root`), in the process view's status line, in the `app` JSON and schema, and as a `VIA` column in the `app` text report

### Changed

- macOS: helpers that launchd starts for an app now count in that app. Safari's row includes its WebContent, GPU and Networking services, and any app's per-client XPC services follow it, instead of each showing as a separate row. appmem asks macOS which process is responsible for the helper and uses the answer only when that process is in the same sample, started no later than the helper, and belongs to an app; otherwise nothing changes. A GUI app started from a terminal keeps its helpers in the terminal's row

## [0.2.0] - 2026-09-26

### Added

- `Home` and `End` move the selection to the first and last row

### Changed

- A click on a row selects it and a double click opens it, as `?` and `--help` now list. Before, a single click on the already selected row opened it
- The live view uses about a third less CPU. At 200x50 the main view went from about 4.5 % to about 3 % of one core, and the process view from about 7.5 % to about 5 %. The table now repaints only the rows that changed, and each refresh reads less from the kernel
- The PROCS column refreshes every five seconds at the default interval instead of every second. The memory columns still refresh every second, and a newly started app gets its count right away

### Fixed

- The selected row stays on screen after `Esc` from one command's processes back to the grouped list, after a resize that changes only the terminal's height, and when you come back from the process view to a re-sorted list. A scroll made with the mouse wheel still stays where you left it
- `q` quits while the theme panel is open, without saving the previewed theme
- Sorting by PROCS shows its sort marker, which the column was one cell too narrow to fit
- The footer no longer offers `w zswap` when the terminal is too narrow to show the ZSWAP column

## [0.1.0] - 2026-09-25

First release. appmem shows which apps are using your RAM and swap, live in the terminal, with one row per app instead of one per process. It reads the memory counters the kernel keeps for every app systemd starts, so it needs Linux with systemd, cgroup v2 and Python 3.12 or newer, and no root.

### Added

- Live view with one row per app: RAM, swap and their total, sorted by total, with a browser's or terminal's dozens of processes summed into one named row. Flatpak, Snap, Electron and multi-window apps merge the same way
- Header with RAM, swap and memory pressure gauges, so you can tell whether a full swap is slowing the machine down right now or only holds idle pages
- Growth columns showing how much each app gained or lost in RAM and swap since you started looking; `b` resets the baseline
- Process view on `Enter`: an app's processes with their age, systemd unit and a ready `systemctl --user stop` or `kill` command for the selected row. `g` groups them by command and `Enter` opens one command's processes, which shows what really runs inside a terminal. Kernel and unattributed rows account for memory the app holds without any process mapping it
- Readable names for interpreter processes, such as `node:mcp-remote` or `python3:http.server` instead of rows of `node` and `python`, without ever showing arguments that could hold secrets
- zswap support: a ZSWAP column, the compressed pool size in the header, and the rate at which the pool writes back to disk swap
- System services (`x` or `--system`) and a page cache column (`c`), both off by default
- Themes: Textual's built-in themes plus `terminal-dark` and `terminal-light`, which use your terminal's own colours. `T` opens a side panel with a live preview and remembers the theme you keep; `--theme` and `APPMEM_THEME` override it for one run
- Help screen on `?` that explains every number and lists the keys
- Layout that fits 80x24 and narrower terminals without wrapping, with numbers in fixed-width slots so nothing jumps between refreshes
- Commands for scripts and AI agents: `appmem snapshot` for the machine and every app, `appmem app NAME` for one app's units, processes and commands, and `appmem schema` describing every command, flag, output field and exit code. They print text on a terminal and JSON when piped or with `--json`, and every error is one JSON line on stderr with a stable `kind`
- Agent skill in `skills/appmem/SKILL.md` that tells an AI agent how to read the numbers, when memory is really the problem and what to recommend closing

[0.2.0]: https://github.com/DamianPala/appmem/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/DamianPala/appmem/releases/tag/0.1.0
