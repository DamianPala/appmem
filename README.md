# appmem

See which **applications** hold your RAM and swap on Linux, live, in the terminal.

`htop` and `btm` list processes.
A browser or a terminal is dozens of them, so "what is eating my swap?" turns into mental math.
appmem reads the memory counters the kernel already keeps for every app (systemd puts each one in its own cgroup), adds them up per app, and shows one sortable row per app, refreshed every second.

```
RAM 16.7/30.9 GiB (3.0 GiB shared)  avail 14.2 GiB (8.2 GiB free, 6.0 GiB cache)  Swap 24.0/32.0 GiB  pressure 10s: none  system 652 MiB [x]  elsewhere 116 MiB
Δ since 02:13 (3s)
 APP                         RAM        SWAP        TOTAL ▾     ΔRAM       ΔSWAP      PROCS
 ghostty                       6.6 GiB    11.2 GiB    17.8 GiB     -3 MiB          ·     281
 plasma                        1.6 GiB     2.3 GiB     3.9 GiB          ·          ·      17
 chrome                        1.6 GiB     2.2 GiB     3.7 GiB          ·     -1 MiB      35
 code                          1.4 GiB     1.5 GiB     2.9 GiB          ·          ·      32
 r s t d sort  enter procs  x system  c cache  z reset Δ  ? help  q quit
```

## Quick start

```
git clone <repo URL> appmem
cd appmem
uv run appmem
```

No root, no config.
To have `appmem` on your PATH, run `uv tool install .` once.

You need Linux with cgroup v2, a systemd user session, and a desktop that starts apps as systemd units (KDE Plasma and GNOME do).
Plus [uv](https://docs.astral.sh/uv/), which fetches Python 3.12+ if needed.

## What you can do with it

**Find the app.**
The main view lists apps by TOTAL (RAM + swap).
Click a column header or press `r` (RAM), `s` (swap), `t` (total), `d` (swap change) to sort; press again to reverse.
The header tells you whether memory is a problem right now.
`pressure 10s: none` with a full swap just means idle pages were moved out of the way; `some` or `high` means programs are waiting for memory.
`shared` is tmpfs, shared memory and GPU buffers the kernel can only swap out, never drop; `cache` (inside `avail`) is file pages it can drop on demand.

**Look inside it.**
Press `Enter` on an app to see its processes, with their age and the systemd unit each one lives in.
The line at the bottom gives you the ready command for the selected row, for example `systemctl --user stop 'app-firefox.service'   kill 41233`.

**Find out what's really in your terminal.**
Everything you start from a terminal counts as the terminal.
In the process view press `g` to group by command, then `Enter` on a command to see its processes.
That's how a "terminal holding 17 GiB" turns out to be eighteen `claude` processes plus the terminal's own main process, running for 86 days and sitting on 3.4 GiB of swap.

**Watch it change.**
ΔSWAP and ΔRAM show how each app grew or shrank since you started appmem (`z` resets the starting point).
A dim `·` means less than 1 MiB of change.

**See the rest.**
`x` adds system services, `c` shows page cache.
The `kernel` and `unattributed` rows at the bottom of the process view explain why the processes don't add up to the app.

## Keys

| Key | Where | Action |
|---|---|---|
| click a header | main, processes | sort by that column; click again to reverse |
| `r` `s` `t` | main, processes | sort by RAM / SWAP / TOTAL; press again to reverse |
| `d` | main | sort by ΔSWAP |
| ↑ ↓ PgUp PgDn | all | move or scroll |
| `Enter` | main, processes | main: processes of the app; grouped process view: processes of the command |
| `g` | processes | group by command |
| `Esc` | processes, help | back |
| `c` / `x` / `z` | main | show page cache / show system services / reset the Δ baseline |
| `?` | all | what the numbers mean |
| `q`, `Ctrl+C` | all | quit (`q` in help closes help) |

Options: `appmem -i SECONDS` sets the refresh interval (default 1, minimum 0.2), `appmem --system` starts with system services shown, `appmem --version` prints the version.

## What the numbers mean

- **RAM** is anonymous + shared + charged kernel memory of the app, without page cache.
  Page cache is reclaimable and makes an app that read a big file look like a hog, so it has its own column (`c`).
- **SWAP** is what the kernel moved out of RAM for that app.
  With zswap it also includes pages kept compressed in RAM.
- **TOTAL** is SWAP + RAM: an accounting sum, not a promise of what closing the app frees.
- **pressure** is the share of the last 10 s that programs spent waiting for memory.
  It tells you whether memory stalls are happening, not which app causes them.
- Rows don't add up to the header: system services (`x`) and memory outside your session (VMs, containers, other users: `elsewhere`) cover the rest.
- Process rows leave out file-backed pages, so they read smaller than htop's RES.

Press `?` in the app for the full explanation.

## For agents

appmem has a non-interactive interface next to the TUI, so you can give this repo's link to an AI agent and ask it what is eating your memory.

```
appmem snapshot [--system] [--limit N] [--json]
appmem app NAME [--scope user|system] [--limit N] [--json]
appmem schema [COMMAND]
```

`appmem snapshot` reports the machine and every app, and `appmem app NAME` reports the units, processes and commands of one app.
Both print a text report on a terminal and JSON otherwise; `--json` forces JSON.
`appmem schema` describes the commands, flags, output fields and exit codes as JSON.
Every error is one JSON object on the last line of stderr with a stable `kind` (listed below).
The diagnosis workflow (how to read pressure against swap, what hides inside a terminal, what to recommend) is in [`skills/appmem/SKILL.md`](skills/appmem/SKILL.md).
The command line follows CLI Design Standard 0.1.0, which `appmem schema` reports under `conformance`.

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | runtime failure (`not_found`, `cgroup_unavailable`) |
| 2 | invalid call (`invalid_input`, `terminal_required`) |
| 130, 143 | interrupted (Ctrl+C, SIGTERM) |

Bare `appmem` is the TUI.
Without a terminal (piped, `--json`, or `NO_INPUT` set) it exits 2 and points to `appmem snapshot`.
There are no shell completions yet.

## Cost

About 1 % of a CPU core for reading the counters at the default 1 s interval; with the UI, 3-4 % of one core on a busy desktop.

## License

MIT
