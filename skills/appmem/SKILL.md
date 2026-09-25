---
name: appmem
description: Diagnose what is using RAM and swap on a Linux machine, per application, with the appmem CLI. Use when the user asks what is eating memory or swap, why the machine is slow and whether memory is the cause, or which app or process to close. Linux with systemd and cgroup v2 only; not for CPU, disk or network questions.
---

# appmem: RAM and swap diagnosis for agents

appmem sums the memory and swap counters the kernel keeps for every systemd app cgroup and reports them per application, with a drill-down to processes and commands.
Use its non-interactive commands; the live TUI (bare `appmem`) is for the human.

- Get it: `uv tool install appmem` (skip in a checkout with a venv).
- Always pass `--json`; some agent shells look like a terminal and would get text.
- `appmem schema` and `appmem schema COMMAND` are the catalog: every command, flag, output field with its meaning, and exit code. Read a field's `description` there before interpreting it.
- No root needed.

## Workflow

1. `appmem snapshot --json`.
   Read `pressure`, `system.ram_available_bytes`, `system.swap_used_bytes`, then the top `apps.items` by `total_bytes` and by `swap_bytes`.
   Add `--system` when user apps don't explain the numbers.
   A busy app's `top_commands` often names what is inside it without step 3.
2. Decide whether memory is the problem right now (next section) before naming a culprit.
3. Drill into the top 1-3 apps: `appmem app NAME --json` (`--scope system` for a `system` item; the snapshot's `next` field holds the command for the biggest one).
   Read `commands.items` first (processes summed by command name), then `processes.items` for PIDs and units; when `has_more` is true, rerun with `--limit` at least the app's `procs`.
4. Growth needs two samples: run `snapshot` again a few minutes later and compare the same apps.
5. Answer with numbers: which app, how much RAM and swap, which processes or commands inside it, whether memory stalls are happening now, and one concrete action.

## Judging the numbers

- `pressure.level` is the last 10 s. `none` with a lot of swap used means idle pages were paged out earlier; memory is probably not why the machine feels slow now (look at CPU, I/O, GPU). `some` or `high` means tasks are waiting for memory now; appmem shows how much, not which app causes it.
- Act when `ram_available_bytes` drops under ~10 % of `ram_total_bytes` together with `some` or `high`.
- `pressure` is `null` when the kernel has no pressure data: judge by `ram_available_bytes` alone and say you can't tell whether stalls are happening.
- Never sum process `ram_bytes` to size an app: RSS counts a shared page once per process. Use the app's own `ram_bytes`.
- Terminals: everything started from a terminal counts as the terminal app. A terminal with 17 GiB is usually not the terminal itself; name what `commands.items` shows inside it (agent sessions, node processes, builds), not the terminal.
- A process with large `swap_bytes`, small `ram_bytes` and an `age_seconds` of days is an idle sleeper that was paged out: harmless under `none`, the first thing to free under `high`.
- Swap always belongs to a live process or cgroup and is freed when the owner exits. Long uptime is not a reason to reboot; closing or restarting the holder frees the same memory.
- Compare within appmem, not against htop: per-process values here leave out file-backed pages.
- `kernel_bytes` and `unattributed_*` explain why processes don't add up to the app; neither is a leak by itself.

## What to recommend

- The smallest action that frees the most: close or restart one app or one command inside it ("N `claude` processes inside ghostty hold X GiB; close the sessions you are done with"). Give the numbers and let the user choose.
- Never kill or stop anything without the user's explicit ok. appmem itself never does.
- Ready commands: `systemctl --user stop 'UNIT'` for a unit from `units` of a `scope: "user"` app; `sudo systemctl stop 'UNIT'` for `scope: "system"`; `kill PID` for one process. Stopping a terminal's main unit closes every window in it, so prefer `kill PID` for the specific command or ask the user to close that tab.
- Don't recommend `swapoff` or `vm.swappiness` changes from one snapshot: `swapoff` needs free RAM for everything paged out, and under `none` swap is doing its job.
- `high` with most swap in one app: free that app. `high` with swap spread thin and `ram_available_bytes` near zero: the machine needs fewer things running or more RAM.
- With zswap on, `zswap_writeback_bytes` growing between two snapshots means the compressed pool is overflowing to the disk swap, which is slow: that is worth naming.

## Errors

A failure writes `{"error": {...}}` as the last non-empty line on stderr, with `kind`, `message`, `action` (`agent`: fix the call yourself; `user`: only the human can) and, when present, `hint` and `next` (the argv to run instead). Follow `hint` and `next`.
`cgroup_unavailable` means this machine has no cgroup v2, no systemd user manager for this user (for example as root) or the memory controller off: say so and stop, appmem can't help here.
`interrupted` (a signal stopped the command): run it again.
