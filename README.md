# appmem

Find out which apps are eating your memory. Live, in the terminal, one row per app, on Linux and on Apple Silicon Macs.

The laptop starts swapping, the fan spins up, and `htop` shows forty processes called `chrome`, thirty called `node` and something called `Isolated Web Co`.
appmem shows the same memory as the apps you actually opened: Chrome, Firefox, VS Code, your terminal, each with its memory and how much it grew since you started looking.
The header tells you whether the swapping is a problem at all, and one key opens the processes behind any row.

On Linux:

![appmem's main view on Linux: one row per app, sorted by TOTAL](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/main.svg)

On a Mac:

![appmem's main view on a Mac: one row per app, sorted by MEMORY](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/main-macos.svg)

You can also skip the reading and hand the job to an AI agent.
Paste this into Claude Code, Codex or any agent that runs shell commands:

```
Install appmem (uv tool install appmem), read
https://raw.githubusercontent.com/DamianPala/appmem/main/skills/appmem/SKILL.md
and tell me what is eating my memory and what to close.
```

The agent installs appmem, reads the numbers the way the skill file describes and tells you what to close.
What the agent gets is in [For agents](#for-agents).

Reach for it when:

- the machine got slow and you want the culprit, not a process list;
- swap is full and you want to know whether that matters right now;
- a "terminal" holds 17 GiB and you want to know what is really running inside it;
- you want to watch one app grow while you use it.

## Install

```
uv tool install appmem
appmem
```

No root, no config file.
From a checkout, `uv run appmem`.
Python 3.12 or newer; uv fetches it for you.

On a Mac you need Apple Silicon and macOS 15 or newer.

On Linux you need systemd and a desktop that starts apps as systemd units (KDE Plasma and GNOME do), on cgroup v2, which current distributions use by default.
Kernel 5.10 or newer; the zswap figures need 5.19 and the Pressure line needs pressure tracking (PSI) switched on in the kernel, without them those parts stay off and the rest works.

## Using it

The main view lists your apps, biggest first.
Press the first letter of a column or click a header to sort by something else.
The Pressure line in the header tells you whether memory is a problem right now: a full swap with Pressure at `none` (`normal` on a Mac) only means idle pages were moved out of the way; anything above that means programs are waiting for memory.

`Enter` on an app shows its processes.
On Linux the bottom line has the command ready for the selected row, for example `systemctl --user stop 'app-firefox.service'` or `kill 41233`.

Everything you start from a terminal counts as the terminal.
Press `g` in the process view to group by command.
That is how a "terminal holding 17 GiB" turns out to be eighteen `claude` processes plus the terminal itself, running for 86 days with 3.4 GiB in swap.

![The process view, grouped by command: several claude processes collapse into one row](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/processes.svg)

The Δ columns show how each app grew or shrank since you started appmem (`b` resets the baseline).
`h` opens the full memory details of the machine.
On Linux, `x` adds system services and `c` shows the CACHE column.

`T` opens a theme panel with a live preview: Textual's built-in themes plus `terminal-dark` and `terminal-light`, which use your terminal's own colours.
The theme you keep is saved to `~/.config/appmem/config.toml`; `appmem --theme NAME` or `APPMEM_THEME` override it for one run.

![The theme panel open on dracula, live-previewed on the main view behind it](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/theme-panel.svg)

`?` inside the app explains every number and lists the keys; `appmem --help` has the same list plus the options.

## How to read the numbers

### On Linux

RAM is what the app is using right now.
SWAP is what the system moved out of RAM to make room: onto the disk or, with zswap, into a compressed corner of RAM.
TOTAL is RAM + SWAP, one number to sort by.
Closing the app gives you back less than that: memory shared with other apps stays, and freed swap is mostly space on the disk, not RAM.
CACHE (press `c`) is file data the kernel keeps around because something read or wrote it recently.
It is not part of RAM or TOTAL: the kernel can drop it when it needs the space, and an app that just read a big file would otherwise look like a memory hog.
The rows don't add up to the header, because system services, virtual machines, containers and other users hold the rest.

In the header, `in` and `out` on the Swap row are how fast the system is reading from swap and writing to it right now, and `written` is how much it wrote since you started appmem and since the machine booted.
A steady `out` while Pressure is above `none` means the machine is short of memory right now; a full swap with both at zero does not.
With zswap, the Zswap row shows how much RAM the compressed pool takes and how much data it holds; both are already inside the RAM and Swap figures, so don't add them again.
A `…` at the end of a header row means the terminal is too narrow for the rest; `h` shows all of it.

### On a Mac

MEMORY is the number Activity Monitor shows in its Memory column, added up per app.
ΔMEM is how much it grew or shrank since you started appmem.
RESIDENT (from 100 columns) is what the app has in RAM at this moment, including files it shares with other apps; it overlaps with MEMORY, so don't add the two.
A `*` after a number means some of the app's processes could not be read, `?` means none could.

macOS does not say how much of each app sits in swap or in compressed memory, so those appear in the header, for the whole machine: the Compress row shows how much data macOS squeezed and how much RAM it takes now, the Swap row what went to disk, with the same `in`, `out` and `written` as on Linux.
RAM used reads higher than in Activity Monitor, because it includes memory the system sets aside at boot.
Pressure is macOS's own verdict: `normal`, `warning` or `critical`.

The rows are your own apps.
Helper services that macOS starts on an app's behalf, such as Safari's web content processes, can show up as rows of their own.

## For agents

appmem is made to be driven by an AI agent as much as by you; the prompt at the top is all it needs.
The agent gets the same data as the TUI as JSON, and [skills/appmem/SKILL.md](https://github.com/DamianPala/appmem/blob/main/skills/appmem/SKILL.md) tells it how to read it: pressure against swap, what hides inside a terminal, what to recommend.

```
appmem snapshot [--system] [--limit N] [--json]
appmem app NAME [--scope user|system] [--limit N] [--json]
appmem schema [COMMAND]
```

`snapshot` covers the machine and every app, `app NAME` one app's processes and commands (`--system` and `--scope` are Linux only).
Both print text on a terminal and JSON when piped; `--json` forces JSON.
`appmem schema` describes the commands, flags, output fields and exit codes as JSON for the system it runs on, Linux or macOS.
Every error is one JSON object on the last line of stderr with a stable `kind` such as `not_found`; exit code 1 is a runtime failure, 2 an invalid call.
Bare `appmem` without a terminal exits 1 and points to `appmem snapshot`.

## Cost

Reading the counters once a second takes about 1 % of one CPU core.
The live view takes 3 to 12 % of one core on a busy Linux desktop, more with a tall terminal and many apps; on the Mac we tested it took about 3 %.
Refreshing every two seconds instead of every second (`appmem -i 2`) cuts that in half.

## Development

```
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

The screenshots come from fixture data, never a real machine; regenerate them with `uv run python scripts/screenshots.py`.

## License

MIT
