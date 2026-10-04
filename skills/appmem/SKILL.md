---
name: appmem
description: Diagnose per-application memory with the appmem CLI on Linux (RAM and swap from systemd cgroup v2) or on macOS 15+ on Apple Silicon (process physical footprints). Use when the user asks what is eating memory or swap, why the machine is slow and whether memory is the cause, or which app or process to close. Not for CPU, disk or network questions.
---

# appmem: per-application memory diagnosis for agents

appmem reports memory per application, with a drill-down to processes and commands.
Use its non-interactive commands; the live TUI (bare `appmem`) is for the human.

- Get it: `uv tool install appmem`; ask first unless the user already asked you to install it.
- Always pass `--json`; some agent shells look like a terminal and would get text.
- `appmem schema` and `appmem schema COMMAND` are the catalog: every command, flag, output field with its meaning, and exit code. Read a field's `description` there before interpreting it.
- The schema index has `platform`: `linux` or `darwin` (macOS 15+ on Apple Silicon). Linux and macOS documents share the envelope (`apps.items`, `has_more`, `next`, `processes.items`, `commands.items`) and differ in the memory fields, so read the schema of the platform you are on. In `snapshot` and `app` output only macOS carries a `platform` key.
- No root needed.

## Workflow

1. `appmem snapshot --json`.
   Linux: read `pressure`, `system.ram_available_bytes`, `system.swap_used_bytes`, then the top `apps.items` by `total_bytes` and by `swap_bytes`. Add `--system` when user apps don't explain the numbers. A busy app's `top_commands` often names what is inside it without step 3.
   macOS: read `pressure.level`, then rank `apps.items` by `footprint_bytes`; a null footprint is unknown, not zero. Check `coverage` (`partial`, `grouping_partial`) before comparing totals.
2. Decide whether memory is the problem right now before naming a culprit (see the section for your platform).
3. Drill into the top 2-5 apps: `appmem app NAME --json` (Linux: `--scope system` for a `system` item; macOS: pass the app's `id`). The snapshot's `next` field holds the command for the biggest one.
   Read `commands.items` first (processes summed by command name), then `processes.items` for PIDs; when `has_more` is true, rerun with `--limit` at least the app's `procs`.
   Terminals: everything started from a terminal counts as the terminal app. A terminal holding many GiB is usually not the terminal itself; name what `commands.items` shows inside it (agent sessions, node processes, builds), not the terminal.
4. Growth needs two samples: run `snapshot` again a few minutes later and compare the same apps (macOS: only when both have complete `coverage`). Swap traffic is the difference of `swap_in_bytes` and `swap_out_bytes` between the two samples (either may be null); one snapshot gives none.
5. Answer with numbers, say whether memory is short right now, and give one concrete action.

## Linux: judging the numbers

- `pressure.level` `none` with a lot of swap used means idle pages were paged out earlier; memory is probably not why the machine feels slow now (look at CPU, I/O, GPU). `some` or `high` means tasks are waiting for memory now; appmem shows how much, not which app causes it.
- Act when `ram_available_bytes` drops under ~10 % of `ram_total_bytes` together with `some` or `high`.
- A process with large `swap_bytes`, small `ram_bytes` and an `age_seconds` of days is an idle sleeper that was paged out: harmless under `none`, the first thing to free under `high`.
- Swap always belongs to a live process or cgroup and is freed when the owner exits. Long uptime is not a reason to reboot; closing or restarting the holder frees the same memory.
- `kernel_bytes` and `unattributed_*` are accounting remainders, not leaks.

## Linux: what to recommend

- Never kill or stop anything without the user's explicit ok. appmem itself never does.
- Ready commands: `systemctl --user stop 'UNIT'` for a unit from `units` of a `scope: "user"` app; `sudo systemctl stop 'UNIT'` for `scope: "system"`. Stopping a terminal's main unit closes every window in it, so prefer `kill PID` for the specific command or ask the user to close that tab.
- Don't recommend `swapoff` or `vm.swappiness` changes from one snapshot: `swapoff` needs free RAM for everything paged out, and under `none` swap is doing its job.
- `high` with most swap in one app: free that app. `high` with swap spread thin and `ram_available_bytes` near zero: the machine needs fewer things running or more RAM.
- With zswap on, `zswap_writeback_bytes` growing between two snapshots means the compressed pool is overflowing to the swap devices, which is slow: that is worth naming.

## macOS: what to claim

- Footprint is not resident RAM or a promise of reclaimable memory. Never derive host memory used by subtracting app footprints, and do not claim per-app swap or cache: macOS gives none.
- Compressed memory per app (`compressed_bytes`, meaning in the schema) sits inside the footprint: never add it on top, and a null is unknown, not zero (see `coverage.compressed_partial`). A large share says the app was squeezed to make room, not that it uses the most; the apps' sum is not the host's compressor figures.
- Helper services launchd starts for an app (WebKit and other XPC services) join that app's row; `via` on each process in `app` says which rule placed it. Shared launchd agents stay their own rows, so an app row can still omit helpers, and closing the app does not necessarily free them.
- `pressure.level` is a kernel state (`normal`, `warning`, `critical`), not a stall percentage. `normal` with swap in use means memory is not the problem now; `warning` or `critical` means the machine is short of memory now.
- `--system` and `--scope system` do not exist; never suggest `systemctl`. Suggest closing an identified app only when the evidence supports it.

## Errors

A failure writes `{"error": {...}}` as the last non-empty line on stderr, with `kind`, `message`, `action` (`agent`: fix the call yourself; `user`: only the human can) and, when present, `hint` and `next` (the argv to run instead). Follow `hint` and `next`.
`cgroup_unavailable` means this machine has no cgroup v2, no systemd user manager for this user (for example as root) or the memory controller off: say so and stop, appmem can't help here.
`platform_unavailable` means this system is not supported (Linux, or macOS 15+ on Apple Silicon): report it, do not substitute Linux counters.
`read_failed` (a native read failed on a supported Mac) and `interrupted` (a signal stopped the command): run it again.
