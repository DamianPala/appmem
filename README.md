# appmem

A live terminal view of RAM and swap **per application** on Linux.

`htop` and `btm` show processes. A browser or a terminal is dozens of processes, so "which app is eating my swap?" turns into mental math.
appmem reads the memory and swap counters the kernel already keeps for every systemd app cgroup, sums them per app, and shows a sortable table that refreshes every second.

```
RAM 17.1/30.9 GiB  avail 13.9 GiB   Swap 23.0/32.0 GiB   pressure 10s: none   system 560 MiB (x)
Δ since 00:01 (2s)
 APP                        SWAP        RAM         TOTAL ▾     ΔSWAP      ΔRAM       PROCS
 ghostty                      11.1 GiB     6.3 GiB    17.4 GiB     -8 KiB    -11 MiB     278
 chrome                        1.9 GiB     1.8 GiB     3.7 GiB          0   +168 KiB      35
 plasma                        2.1 GiB     1.3 GiB     3.5 GiB     -4 KiB   +516 KiB      16
 code                          1.9 GiB  1019 MiB     2.9 GiB    -12 KiB    -15 MiB      32
```

Press Enter on an app to see its processes, then `g` to group them by command.
That is how you find out that the "terminal" holding 17 GiB is really eight agent sessions and one terminal window that leaked 3 GiB over 86 days.

## Requirements

- Linux with cgroup v2 and systemd user sessions (any current desktop distro: KDE Plasma, GNOME, ...).
- Python 3.12+.
- No root. Everything it reads is world-readable.

## Install

```
uv tool install .
```

or run it from a checkout with `uv run appmem`.

## Usage

```
appmem [-i SECONDS] [--system]
```

| Flag | Default | Meaning |
|---|---|---|
| `-i`, `--interval SECONDS` | `1` | Refresh interval, ≥ 0.2 |
| `--system` | off | Also show system services (same as `x`) |

| Key | Action |
|---|---|
| click header, `s` `r` `t` `d` | sort by that column / SWAP / RAM / TOTAL / ΔSWAP, again to reverse |
| `Enter` | processes of the selected app |
| `g` | in the process view: group by command |
| `Esc` | back |
| `c` | show page cache |
| `x` | show system services |
| `z` | reset the Δ baseline |
| `?` | what the numbers mean |
| `q`, `Ctrl+C` | quit |

## What the numbers mean

- **RAM** is `anon + shmem + kernel` from the app's `memory.stat`: anonymous, shared and charged kernel memory, excluding file cache. Page cache is left out (press `c` to see it), because it makes an app that read a big file look like a hog.
- **SWAP** is the app's `memory.swap.current`.
- **TOTAL** is `SWAP + RAM`: an accounting sum, not a prediction of what closing the app would free.
- **pressure** is how much of the last 10 s tasks spent waiting for memory. A lot of swap with pressure `none` just means idle pages were paged out; `high` means memory stalls are happening, but not which app is causing them.
- In the process view, rows come from `/proc/PID/status`, leaving out file-backed pages, so they read smaller than htop's RES. They don't add up to the app row either: shared pages count in every process, and memory the app holds without any process mapping it (GPU buffers, memfd) belongs to no process. The `unattributed` row shows that gap, and `kernel` shows the app's own page tables, slab and stacks.
- Anything started from a terminal counts as the terminal, because that's the cgroup it lives in. Use `g` in the process view to see what is actually running there.

Press `?` in the app for the full explanation.

## Cost

About 1 % of a CPU core for reading the counters at the default 1 s interval. With the Textual UI on a busy desktop the whole process takes roughly 4-5 % of one core, in the process view of a 280-process app too.

## License

MIT
