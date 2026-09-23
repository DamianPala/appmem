---
name: appmem
description: Diagnose what is using RAM and swap on a Linux machine, per application, with the appmem CLI. Use when the user asks what is eating memory or swap, why the machine is slow and whether memory is the cause, or which app or process to close. Linux with systemd and cgroup v2 only; not for CPU, disk or network questions.
---

# appmem: RAM and swap diagnosis for agents

appmem sums the memory and swap counters the kernel keeps for every systemd app cgroup and reports them per application, with a drill-down to processes and commands.
You use its non-interactive commands; the live TUI is for the human.

## Get it running

- Get it: `git clone REPO_URL appmem` (needs git and [uv](https://docs.astral.sh/uv/); uv fetches Python 3.12+ itself if missing).
  Skip this when you are already in a checkout.
- Run it from any directory: `uv run --project appmem appmem snapshot --json` (the first run creates the venv).
  Or install it once with `uv tool install ./appmem` and call `appmem` directly.
- No root needed; everything it reads is world-readable.
- Always pass `--json`.
  Without it the output is text whenever stdout is a terminal, and some agent shells are.
- `appmem --help` and `appmem schema` describe every command, flag, output field and exit code.
  Treat `appmem schema` as the catalog; this file covers the workflow and the error kinds.
- Never run bare `appmem`: that is the live TUI, and without a terminal it fails with `terminal_required`.

## Workflow

1. `appmem snapshot --json`.
   Read `pressure`, `system.ram_available_bytes`, `system.swap_used_bytes`, then the top `apps.items` by `total_bytes` and by `swap_bytes`.
   Add `--system` when user apps don't explain the numbers.
2. Decide whether memory is the problem right now (next section) before naming a culprit.
3. Drill into the top 1-3 apps: `appmem app NAME --json`, adding `--scope system` when the item's `scope` is `system`.
   The snapshot's `next` field already holds the command for the biggest one.
   Read `commands.items` first (the app's processes already summed per command name, like the TUI's `g`), then `processes.items`.
   To act on one command, find its PIDs and units in `processes.items` by `name`.
   If `processes.has_more` is true, rerun with `--limit` at least the app's `procs`.
4. Growth needs two samples: run `snapshot` again a few minutes later and compare the same apps.
5. Answer with numbers: which app, how much RAM and swap, which processes or commands inside it, whether memory stalls are happening now, and one concrete action.

## Reading the numbers

- `pressure.level` covers the last 10 s.
  `none` with a lot of swap used means idle pages were paged out earlier; memory is probably not why the machine feels slow now (look at CPU, I/O, GPU).
  `some` or `high` means tasks are waiting for memory now.
  appmem shows how much, not which app causes the stalls.
- `some_avg60_percent` above 1 while `level` is `none`: stalls happened within the last minute and just stopped.
- `pressure` is `null` when the kernel has no pressure data (`/proc/pressure/memory` missing).
  Then judge by `ram_available_bytes` alone, and say you can't tell whether stalls are happening.
- Act when `ram_available_bytes` is low (under ~10 % of `ram_total_bytes`) together with `some` or `high`.
- `ram_bytes` is anonymous + shared + charged kernel memory, without page cache (`cache_bytes`, reclaimable).
  `total_bytes` is RAM + swap, an accounting sum, not what closing the app frees.
- In `snapshot`'s `system`, `ram_shared_bytes` is tmpfs, shared memory and GPU buffers: part of `ram_used_bytes`, swappable but not droppable.
  `ram_free_bytes` (truly free), `ram_cache_bytes` (droppable file cache) and `ram_slab_bytes` (kernel caches of file names and inodes, dropped on demand) are the main parts of `ram_available_bytes`, which is a kernel estimate, not their exact sum -- they come close to it, but the kernel reserves some headroom.
- Apps don't add up to the `system` totals: `system_services_*` and `elsewhere_bytes` (VMs, containers, other users, login sessions) cover the rest.
- Terminals: everything started from a terminal counts as the terminal app.
  A terminal with 17 GiB is usually not the terminal itself; `commands.items` shows the agent sessions, node processes and builds inside it.
  Name those, not the terminal.
- `kernel_bytes` (page tables, slab, stacks) and `unattributed_*` (shared pages, memfd, GPU buffers) explain why processes don't add up to the app.
  Neither is a leak by itself.
- Per-process values leave out file-backed pages, so they are smaller than htop's RES.
  Compare within appmem, not across tools.
- A process with large `swap_bytes`, small `ram_bytes` and an `age_seconds` of days is an idle sleeper that was paged out.
  Harmless under `none`; the first thing to free under `high`.
- Swap always belongs to a live process or cgroup: the kernel frees it when the owner exits, so there is no swap "left over" from processes long gone.
  Long uptime is not a reason to reboot; closing or restarting the holder frees the same memory.

## What to recommend

- The smallest action that frees the most: close or restart one app or one command inside it ("N `claude` processes inside ghostty hold X GiB; close the sessions you are done with").
  Give the numbers and let the user choose.
- Never kill or stop anything without the user's explicit ok.
  appmem itself never does.
- Ready commands: `systemctl --user stop 'UNIT'` for a unit from `units` of a `scope: "user"` app; `sudo systemctl stop 'UNIT'` for `scope: "system"`; `kill PID` for one process.
- Stopping a terminal's main unit closes every window in it.
  Prefer `kill PID` for the specific command, or ask the user to close that tab.
- Don't recommend `swapoff` or `vm.swappiness` changes from one snapshot.
  `swapoff` needs free RAM for everything paged out, and under `none` swap is doing its job.
- `high` with most swap in one app: free that app.
  `high` with swap spread thin and `ram_available_bytes` near zero: the machine needs fewer things running or more RAM.
- With zswap enabled, swap includes pages kept compressed in RAM; appmem does not separate them.

## Errors

A failure writes one JSON object as the last non-empty line on stderr: `{"error": {"kind": ..., "message": ..., "hint": ..., "next": [...]}}`.
Branch on `kind`.
`hint` says what to change, and `next`, when present, is the argv of the command to run instead.

- `terminal_required`: you ran bare `appmem`; run `appmem snapshot --json`.
- `not_found`: no app with that name in that scope; names come from `snapshot`, and system services need `--scope system`.
- `cgroup_unavailable`: no cgroup v2, no systemd user manager for this user (for example when run as root), or the memory controller is off.
  The message names the missing path.
  Say so and stop; appmem can't help on this machine.
- `invalid_input`: fix the call; `appmem schema COMMAND` lists the flags.
- `interrupted`: the command was stopped by a signal; run it again.
