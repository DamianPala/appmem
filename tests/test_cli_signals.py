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
import os
import sys
import time
from pathlib import Path
from appmem.cli import main
from appmem.ui.app import AppMemApp
from textual.drivers.linux_driver import LinuxDriver

_trace_fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
_marks = 0
def mark(stage):
    global _marks
    if _marks >= 64:
        return
    _marks += 1
    os.write(_trace_fd, f'{time.monotonic():.6f} {stage}\\n'.encode())

def wrap(cls, name):
    original = getattr(cls, name)
    def measured(self, *args, **kwargs):
        mark(name + '.start')
        try:
            return original(self, *args, **kwargs)
        finally:
            mark(name + '.end')
    setattr(cls, name, measured)

wrap(AppMemApp, 'exit')
wrap(LinuxDriver, 'disable_input')
wrap(LinuxDriver, 'stop_application_mode')
wrap(LinuxDriver, 'close')

original_loop = AppMemApp._process_messages_loop
async def measured_loop(self):
    mark('message_loop.start')
    try:
        return await original_loop(self)
    finally:
        mark('message_loop.end')
AppMemApp._process_messages_loop = measured_loop

original_shutdown = AppMemApp._shutdown
async def measured_shutdown(self):
    mark('shutdown.start')
    try:
        return await original_shutdown(self)
    finally:
        mark('shutdown.end')
AppMemApp._shutdown = measured_shutdown

mark('main.start')
code = main(sys.argv[3:], root=Path(sys.argv[1]), uid=1000)
mark('main.end')
os.close(_trace_fd)
sys.exit(code)
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


def _wait_for_exit(
    pid: int, master_fd: int, timeout: float
) -> tuple[int | None, float, bytes, float | None]:
    """Drain the PTY while polling for exit, so its output cannot block shutdown."""
    start = time.monotonic()
    deadline = start + timeout
    output = bytearray()
    restored_at: float | None = None
    while time.monotonic() < deadline:
        output.extend(_read_available(master_fd))
        if restored_at is None and b"\x1b[?1049l" in output:
            restored_at = time.monotonic() - start
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid == pid:
            output.extend(_read_available(master_fd))
            if restored_at is None and b"\x1b[?1049l" in output:
                restored_at = time.monotonic() - start
            return status, time.monotonic() - start, bytes(output), restored_at
        time.sleep(0.02)
    return None, timeout, bytes(output), restored_at


def test_sigterm_exits_promptly_instead_of_waiting_for_the_next_tick(tmp_path: Path) -> None:
    root = _base_tree(tmp_path)
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER)
    trace = tmp_path / "signal-trace.txt"

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(root), str(trace), "-i", "5"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,  # its own process group: SIGTERM below hits only this child
        cwd=Path(__file__).resolve().parent.parent,
    )
    os.close(slave_fd)
    try:
        # Wait for the sampled host header, not only the first "RAM" text,
        # which can arrive before the whole first frame has been written.
        deadline = time.monotonic() + 5.0
        seen = b""
        markers = (b"RAM", b"Pressure", b"GiB")
        while time.monotonic() < deadline and not all(marker in seen for marker in markers):
            seen += _read_available(master_fd)
            time.sleep(0.05)
        assert all(marker in seen for marker in markers), f"app never rendered its header: {seen!r}"

        sent_at = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        status, elapsed, remaining, restored_at = _wait_for_exit(proc.pid, master_fd, timeout=2.0)

        assert status is not None, "process did not exit within 2s of SIGTERM"
        assert elapsed < 1.0, (
            f"exit took {elapsed:.2f}s (restore at {restored_at}s; "
            f"drained {len(remaining)} bytes; trace from signal {sent_at}: "
            f"{trace.read_text()[:4096] if trace.exists() else '<missing>'}; "
            "must wake the loop immediately)"
        )
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 143

        # Terminal-restore sequences (Textual's `LinuxDriver`, SPEC.md
        # "Errors"): the alt screen and mouse tracking are turned back off,
        # not left engaged in the caller's terminal.
        output = seen + remaining
        assert b"\x1b[?1049l" in output, "alt screen was not turned off on exit"
        assert b"\x1b[?25h" in output, "cursor was not shown again on exit"
        print(
            f"SIGTERM to exit: {elapsed * 1000:.0f} ms; "
            f"trace from signal {sent_at}: {trace.read_text()[:4096]}"
        )
    finally:
        os.close(master_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
