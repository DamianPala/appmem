# Changelog

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

[0.1.0]: https://github.com/DamianPala/appmem/releases/tag/0.1.0
