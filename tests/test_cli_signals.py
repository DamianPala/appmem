"""A real subprocess, in a PTY, must exit promptly on SIGTERM/SIGINT instead
of waiting for the next periodic tick (SPEC.md "Errors", "Tech").

A genuine subprocess is needed here (not the Textual pilot, which never runs
a real event loop + real OS signal delivery): the bug is specifically about
`loop.add_signal_handler` waking the asyncio loop immediately versus
`signal.signal`'s handler only running the next time the loop happens to wake
up on its own. A PTY (not a pipe) is needed because the bare `appmem` command
requires `stdin`/`stdout` to both be a terminal (SPEC.md "Command line"); no
tmux, no live `/proc`/`/sys` -- a fixture root, passed to `appmem.cli.main`
directly via a small runner script, same as the collector fixtures elsewhere
in this suite.
"""

from __future__ import annotations

import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

from helpers import user_service_root, write_meminfo, write_memory_stat, write_uptime

_RUNNER = """
import sys
from pathlib import Path
from appmem.cli import main
sys.exit(main(sys.argv[2:], root=Path(sys.argv[1]), uid=1000))
"""


def _base_tree(tmp_path: Path) -> Path:
    write_memory_stat(user_service_root(tmp_path, 1000))
    write_meminfo(
        tmp_path,
        mem_total_kb=32_000_000,
        mem_available_kb=11_000_000,
        swap_total_kb=32_000_000,
        swap_free_kb=12_000_000,
    )
    write_uptime(tmp_path, seconds=100_000.0)
    return tmp_path


def _read_available(fd: int) -> bytes:
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except (BlockingIOError, OSError):
        pass
    return b"".join(chunks)


def _wait_for_exit(pid: int, master_fd: int, timeout: float) -> tuple[int | None, float, bytes]:
    """Drain the PTY while polling for exit, so its output cannot block shutdown."""
    start = time.monotonic()
    deadline = start + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        output.extend(_read_available(master_fd))
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid == pid:
            output.extend(_read_available(master_fd))
            return status, time.monotonic() - start, bytes(output)
        time.sleep(0.02)
    return None, timeout, bytes(output)


def test_sigterm_exits_promptly_instead_of_waiting_for_the_next_tick(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER)

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(root), "-i", "5"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,  # its own process group: SIGTERM below hits only this child
        cwd=Path(__file__).resolve().parent.parent,
    )
    os.close(slave_fd)
    try:
        # Give it time to start Textual, enter the alt screen and take its
        # first sample (well under one tick at -i 5): reading until the
        # screen's own output settles is more robust than a fixed sleep.
        deadline = time.monotonic() + 5.0
        seen = b""
        while time.monotonic() < deadline and b"RAM" not in seen:
            seen += _read_available(master_fd)
            time.sleep(0.05)
        assert b"RAM" in seen, f"app never rendered its header: {seen!r}"

        os.kill(proc.pid, signal.SIGTERM)
        status, elapsed, remaining = _wait_for_exit(proc.pid, master_fd, timeout=2.0)

        assert status is not None, "process did not exit within 2s of SIGTERM"
        assert elapsed < 1.0, f"exit took {elapsed:.2f}s (must wake the loop immediately)"
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 143

        # Terminal-restore sequences (Textual's `LinuxDriver`, SPEC.md
        # "Errors"): the alt screen and mouse tracking are turned back off,
        # not left engaged in the caller's terminal.
        output = seen + remaining
        assert b"\x1b[?1049l" in output, "alt screen was not turned off on exit"
        assert b"\x1b[?25h" in output, "cursor was not shown again on exit"
        print(f"SIGTERM to exit: {elapsed * 1000:.0f} ms")
    finally:
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
