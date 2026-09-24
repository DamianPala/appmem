# appmem

Find out which apps are eating your RAM and swap. Live, in the terminal, one row per app.

The laptop starts swapping, the fan spins up, and `htop` shows forty processes called `chrome`, thirty called `node` and something called `plasmashell`.
appmem shows the same memory as the apps you actually opened: Chrome, VS Code, Thunderbird, your terminal, each with its RAM, its swap and how much it grew since you started looking.
The header tells you whether the swapping is a problem at all, and one key opens the processes behind any row.

![appmem's main view: one row per app, sorted by TOTAL](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/main.svg)

You can also skip the reading and hand the job to an AI agent.
Install appmem, tell Claude Code, Codex or any agent that runs shell commands "check what's eating my memory", and it runs `appmem snapshot`, reads the numbers the way this repo's skill describes and tells you what to close.
See [For agents](#for-agents).

Reach for it when:

- the machine got slow and you want the culprit, not a process list;
- swap is full and you want to know whether that matters right now;
- a "terminal" holds 17 GiB and you want to know what is really running inside it;
- you want to watch one app grow while you use it.

## Install

```
uv tool install appmem
# or: pipx install appmem
appmem
```

No root, no config file.
From a checkout, `uv run appmem`.

You need Linux with cgroup v2 and a systemd user session that starts apps as units (KDE Plasma and GNOME do), plus Python 3.12 or newer, which uv fetches for you.
appmem targets kernel 5.10 and newer.
The pressure reading needs PSI and the zswap fields need 5.19; without them those parts stay off and the rest works.

## Using it

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
`x` adds system services, `c` shows page cache.

`T` opens a theme panel with a live preview: Textual's built-in themes plus `terminal-dark` and `terminal-light`, which use your terminal's own colours.
The theme you keep is saved to `~/.config/appmem/config.toml`; `appmem --theme NAME` or `APPMEM_THEME` override it for one run.

![The theme panel open on dracula, live-previewed on the main view behind it](https://raw.githubusercontent.com/DamianPala/appmem/main/docs/screenshots/theme-panel.svg)

`?` inside the app explains every number and `appmem --help` lists all keys and options.

## How to read the numbers

RAM is what the app holds without page cache.
Cache is reclaimable and would make an app that just read a big file look like a hog, so it has its own column.
SWAP is what the kernel moved out of RAM for the app, including pages zswap keeps compressed in RAM.
TOTAL is the sum of the two: an accounting figure, not what closing the app would free.
Rows don't add up to the header, because system services and memory outside your session (VMs, containers, other users) make up the rest.

## For agents

appmem is made to be driven by an AI agent as much as by you.
Install it, then ask your agent to diagnose memory: it gets the same data as the TUI as JSON, plus a skill that explains how to read it.

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
The diagnosis itself (reading pressure against swap, what hides inside a terminal, what to recommend) is in [skills/appmem/SKILL.md](https://github.com/DamianPala/appmem/blob/main/skills/appmem/SKILL.md): point your agent at this repo, or copy that file into its skills.

## Cost

About 1 % of a CPU core to read the counters every second.
With the UI on a busy desktop, about 4 % of one core in the main view and 5 % in the process view.

## Development

```
uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

The screenshots come from fixture data, never a real machine; regenerate them with `uv run python scripts/screenshots.py`.

## License

MIT
