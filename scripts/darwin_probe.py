"""Bounded, unprivileged ARM64 CI exercise of the production native reader."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from appmem.darwin_native import (
    DarwinNative,
    ReadResult,
    Unavailable,
    validate_sdk_abi,
)

MIB = 1024 * 1024
SOURCE = Path(__file__).with_name("darwin_probe_helper.c")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def present[T](value: T | None, message: str) -> T:
    if value is None:
        raise RuntimeError(message)
    return value


def observation[T](result: ReadResult[T]) -> dict[str, object]:
    return {
        "available": result.value is not None,
        "unavailable": result.unavailable,
        "errno": result.error_code,
    }


def read_line(process: subprocess.Popen[bytes], *, timeout: float = 5) -> bytes:
    output = present(process.stdout, "workload stdout missing")
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < 256:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "workload protocol line timed out")
        ready, _, _ = select.select([output], [], [], remaining)
        require(bool(ready), "workload protocol line timed out")
        byte = os.read(output.fileno(), 1)
        require(bool(byte), "workload protocol ended before a full line")
        if byte == b"\n":
            return bytes(data).strip()
        data.extend(byte)
    raise RuntimeError("workload protocol line exceeded 256 bytes")


def line(process: subprocess.Popen[bytes], expected: bytes) -> None:
    require(read_line(process) == expected, "workload protocol mismatch")


def send(process: subprocess.Popen[bytes], command: bytes) -> None:
    input_pipe = present(process.stdin, "workload stdin missing")
    input_pipe.write(command)
    input_pipe.flush()


def executable(root: Path, name: str) -> Path:
    return root / f"{name}.app/Contents/Frameworks/Inner.app/Contents/MacOS/probe"


def abi(binary: Path) -> dict[str, int]:
    output = subprocess.check_output([binary, "abi"], timeout=5, text=True)
    keys = (
        "bsdshort_size",
        "rusage_size",
        "footprint_offset",
        "vm_sdk_size",
        "vm_logical_offset",
        "swap_size",
    )
    actual: dict[str, int] = dict(zip(keys, map(int, output.split()), strict=True))
    validate_sdk_abi(actual)
    return actual


def sample(reader: DarwinNative, pid: int) -> int:
    result = reader.memory(pid)
    value = present(result.value, f"own-child footprint unavailable: {result.unavailable}")
    require(value.start_abstime > 0, "missing process start identity")
    return value.footprint_bytes


def workload(reader: DarwinNative, binary: Path, *, bound: int = 20) -> dict[str, object]:
    process = subprocess.Popen(
        [binary, "workload"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        line(process, b"ready")
        identity = reader.process(process.pid)
        proc_info = present(identity.value, "own-child BSD metadata unavailable")
        require(proc_info.ppid == os.getpid(), "own-child parent mismatch")
        path = reader.path(process.pid)
        actual_path = present(path.value, "own-child executable path unavailable")
        require(
            os.path.realpath(binary) == actual_path,
            "own-child executable path unavailable or different",
        )
        require(
            actual_path.split(".app/", 1)[0].endswith(binary.parents[5].stem),
            "outermost bundle path could not be found",
        )
        pids = reader.pids()
        require(
            pids.value is not None and process.pid in pids.value,
            "own-child absent from PID listing",
        )
        before = sample(reader, process.pid)
        send(process, b"a")
        line(process, b"allocated")
        allocated = sample(reader, process.pid)
        require(
            allocated - before >= 80 * MIB,
            f"footprint grew only {allocated - before} bytes for 128 MiB touched",
        )
        send(process, b"f")
        line(process, b"freed")
        time.sleep(0.2)
        freed = sample(reader, process.pid)
        require(
            freed <= allocated - 64 * MIB,
            f"footprint failed to fall after munmap: {freed} vs {allocated}",
        )
        send(process, b"q")
        require(process.wait(timeout=bound) == 0, "workload exit failed")
        missing = reader.memory(process.pid)
        require(
            missing.value is None and missing.unavailable == Unavailable.VANISHED,
            f"exited PID was not reported vanished: {missing}",
        )
        return {
            "before_bytes": before,
            "allocated_bytes": allocated,
            "freed_bytes": freed,
            "exit": process.returncode,
            "vanished": missing.unavailable,
        }
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()


def tree(reader: DarwinNative) -> dict[str, object]:
    code = (
        "import subprocess; "
        "p=subprocess.Popen(['/bin/sleep','3']); "
        "print(p.pid,flush=True); p.wait()"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
    )
    completed = False
    try:
        child_pid = int(read_line(parent))
        info = present(reader.process(child_pid).value, "tree child metadata unavailable")
        require(info.ppid == parent.pid, "tree child ancestry mismatch")
        require(parent.wait(timeout=5) == 0, "tree parent failed")
        gone = reader.process(child_pid)
        require(
            gone.value is None and gone.unavailable == Unavailable.VANISHED,
            f"tree child exit was not confirmed: {gone.unavailable}",
        )
        completed = True
        return {"ancestry": "verified", "child_exit": "verified"}
    finally:
        if not completed:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        if parent.stdout is not None:
            parent.stdout.close()


def run() -> dict[str, object]:
    require(
        sys.platform == "darwin" and platform.machine() == "arm64",
        "native probe requires macOS ARM64",
    )
    require(os.geteuid() != 0, "native probe must run without root")
    reader = DarwinNative()
    host = reader.host()
    host_value = present(host.value, f"required host counters unavailable: {host.unavailable}")
    require(
        host_value.physical_bytes >= MIB and host_value.page_size in (4096, 16_384),
        "implausible host RAM or page size",
    )
    require(
        any(
            (
                host_value.free_bytes,
                host_value.wired_bytes,
                host_value.active_bytes,
                host_value.inactive_bytes,
            )
        ),
        "required VM page counters are all zero",
    )
    require(
        all(
            value is not None and value >= 0
            for value in (
                host_value.speculative_bytes,
                host_value.file_backed_bytes,
                host_value.purgeable_bytes,
            )
        ),
        "legacy backing counters unavailable",
    )
    require(host_value.ram_partition is not None, "host partition counters are inconsistent")
    require(host_value.swap_used_bytes <= host_value.swap_total_bytes, "swap used exceeds total")
    require(
        host_value.pressure_level is None or host_value.pressure_level in (1, 2, 4),
        "unexpected pressure level",
    )
    own = present(reader.memory(os.getpid()).value, "own-process rusage unavailable")
    require(own.footprint_bytes > 0 and own.start_abstime > 0, "own-process footprint missing")
    foreign = {
        "process": observation(reader.process(1)),
        "path": observation(reader.path(1)),
        "memory": observation(reader.memory(1)),
    }
    with tempfile.TemporaryDirectory(prefix="appmem-darwin-") as directory:
        root = Path(directory)
        first = executable(root, "Outer")
        second = executable(root, "Other")
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        subprocess.run(
            [
                "cc",
                "-std=gnu11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(SOURCE),
                "-o",
                str(first),
            ],
            check=True,
            timeout=30,
        )
        shutil.copy2(first, second)
        layout = abi(first)
        result = workload(reader, first)
        second_result = workload(reader, second)
        ancestry = tree(reader)
    return {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "own_footprint_bytes": own.footprint_bytes,
        "page_size": host_value.page_size,
        "abi": layout,
        "host": {
            "physical_bytes": host_value.physical_bytes,
            "vm_count": host_value.vm_count,
            "speculative_bytes": host_value.speculative_bytes,
            "file_backed_bytes": host_value.file_backed_bytes,
            "purgeable_bytes": host_value.purgeable_bytes,
            "compressor_physical_bytes": host_value.compressor_physical_bytes,
            "compressor_logical_bytes": host_value.compressor_logical_bytes,
            "swapped_logical_bytes": host_value.swapped_logical_bytes,
            "swap_used_bytes": host_value.swap_used_bytes,
            "swap_total_bytes": host_value.swap_total_bytes,
            "pressure_level": host_value.pressure_level,
            "pressure_unavailable": host_value.pressure_unavailable,
            "pressure_error_code": host_value.pressure_error_code,
        },
        "foreign_pid_1": foreign,
        "outer_bundle": result,
        "other_bundle": second_result,
        "tree": ancestry,
        "checks": "passed",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run()
    except Exception as error:
        failure = {
            "checks": "failed",
            "error_type": type(error).__name__,
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
        }
        args.output.write_text(json.dumps(failure, indent=2, sort_keys=True) + "\n")
        raise
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("Darwin native probe passed")


if __name__ == "__main__":
    main()
