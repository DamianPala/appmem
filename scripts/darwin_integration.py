"""Installed-wheel Darwin acceptance on a bounded, unprivileged CI runner."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import inspect
import json
import os
import platform
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent
HELPER = Path(__file__).with_name("darwin_probe_helper.c")
CLEAN_ENV = {
    key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME")
}
STAGE_NAMES = (
    "wheel_install",
    "helper_compile",
    "native_abi",
    "schema",
    "workload_spawn",
    "pagination",
    "footprints",
    "pty",
    "unsupported_flags",
    "cleanup",
)


@dataclass
class Progress:
    stage: str = "preflight"
    passed: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(STAGE_NAMES, False))
    installed_python: str | None = None

    def start(self, name: str) -> None:
        self.stage = name

    def finish(self) -> None:
        self.passed[self.stage] = True


class CheckError(RuntimeError):
    def __init__(self, message: str, check_id: str) -> None:
        super().__init__(message)
        self.check_id = check_id


def require(ok: bool, message: str) -> None:
    if not ok:
        caller = inspect.stack(context=0)[1]
        raise CheckError(message, f"{caller.function}:{caller.lineno}")


def remaining(deadline: float, cap: float = 10.0) -> float:
    left = deadline - time.monotonic()
    require(left > 0, "integration deadline exceeded")
    return min(left, cap)


def run_command(
    command: list[str | Path], *, cwd: Path, deadline: float, cap: float = 30.0
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=remaining(deadline, cap),
        check=False,
        env=CLEAN_ENV,
    )
    require(result.returncode == 0, f"command failed with exit {result.returncode}")
    return result


def installed_cli(root: Path, python_version: str, deadline: float) -> tuple[Path, str, str]:
    dist = root / "dist"
    run_command(["uv", "build", "--wheel", "--out-dir", dist, ROOT], cwd=root, deadline=deadline)
    wheels = list(dist.glob("appmem-*.whl"))
    require(len(wheels) == 1, "wheel build did not produce exactly one appmem wheel")
    venv = root / "venv"
    run_command(["uv", "venv", "--python", python_version, venv], cwd=root, deadline=deadline)
    python = venv / "bin/python"
    installed_python = run_command(
        [python, "-I", "-c", "import platform; print(platform.python_version())"],
        cwd=root,
        deadline=deadline,
    ).stdout.strip()
    require(bool(installed_python), "installed Python version unavailable")
    run_command(
        ["uv", "pip", "install", "--python", python, wheels[0]], cwd=root, deadline=deadline
    )
    imported = run_command(
        [python, "-I", "-c", "import appmem; print(appmem.__file__)"],
        cwd=root,
        deadline=deadline,
    ).stdout.strip()
    require(Path(imported).is_relative_to(venv), "appmem imported outside clean wheel environment")
    cli = venv / "bin/appmem"
    version_result = run_command([cli, "--version"], cwd=root, deadline=deadline)
    require(not version_result.stderr, "installed appmem version emitted stderr")
    version = version_result.stdout.strip()
    require(bool(version) and "\n" not in version, "installed appmem version is unavailable")
    return cli, version, installed_python


def compile_helpers(root: Path, deadline: float) -> tuple[Path, Path, Path]:
    binaries = [
        root / branch / "Twin.app/Contents/Frameworks/Inner.app/Contents/MacOS/probe"
        for branch in ("A", "B")
    ]
    loose = root / "loose/probe"
    for binary in (*binaries, loose):
        binary.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["cc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", HELPER, "-o", binaries[0]],
        cwd=root,
        deadline=deadline,
    )
    shutil.copy2(binaries[0], binaries[1])
    shutil.copy2(binaries[0], loose)
    return binaries[0], binaries[1], loose


_VM_COUNT_CODE = """
import json
from appmem.darwin_native import DarwinNative
host = DarwinNative().host().value
print(json.dumps({"vm_count": None if host is None else host.vm_count}))
"""

_PID_ABSENCE_CODE = """
import json
import sys
from appmem.darwin_native import DarwinNative
reader = DarwinNative()
present = reader.pids().value
if present is None:
    raise RuntimeError("native PID inventory unavailable")
gone = []
for pid, start in json.loads(sys.argv[1]):
    value = reader.memory(pid).value if pid in present else None
    gone.append(pid not in present or (value is not None and value.start_abstime != start))
print(json.dumps(gone))
"""


def native_counts(cli: Path, binary: Path, root: Path, deadline: float) -> dict[str, int]:
    fields = run_command([binary, "abi"], cwd=root, deadline=deadline, cap=5).stdout.split()
    require(len(fields) == 6, "C helper ABI response malformed")
    sdk_bytes = int(fields[3])
    require(sdk_bytes in (152, 160), "unexpected SDK VM statistics size")
    document: object = json.loads(
        run_command(
            [cli.parent / "python", "-I", "-c", _VM_COUNT_CODE],
            cwd=root,
            deadline=deadline,
            cap=5,
        ).stdout
    )
    require(isinstance(document, dict), "installed native count response malformed")
    count = cast("dict[str, Any]", document)["vm_count"]
    require(type(count) is int and count >= 38, "native runtime VM count unavailable")
    return {"vm_sdk_size_bytes": sdk_bytes, "vm_runtime_count": count}


def require_gone(cli: Path, root: Path, identities: list[tuple[int, int]], deadline: float) -> None:
    output = run_command(
        [cli.parent / "python", "-I", "-c", _PID_ABSENCE_CODE, json.dumps(identities)],
        cwd=root,
        deadline=deadline,
        cap=5,
    ).stdout
    gone: object = json.loads(output)
    if not isinstance(gone, list):
        raise RuntimeError("installed native PID check malformed")
    flags = cast("list[object]", gone)
    require(len(flags) == len(identities), "installed native PID check malformed")
    require(all(flag is True for flag in flags), "owned PID/start identity survived cleanup")


def read_line(process: subprocess.Popen[bytes], deadline: float) -> bytes:
    pipe = process.stdout
    if pipe is None:
        raise RuntimeError("workload protocol output missing")
    result = bytearray()
    line_deadline = min(deadline, time.monotonic() + 5)
    while len(result) < 128:
        ready, _, _ = select.select([pipe], [], [], remaining(line_deadline, 5.0))
        require(bool(ready), "workload protocol timeout")
        byte = os.read(pipe.fileno(), 1)
        require(bool(byte), "workload exited during protocol")
        if byte == b"\n":
            return bytes(result)
        result.extend(byte)
    raise RuntimeError("workload protocol line too long")


def send(
    process: subprocess.Popen[bytes], command: bytes, expected: bytes | None, deadline: float
) -> None:
    pipe = process.stdin
    if pipe is None:
        raise RuntimeError("workload protocol input missing")
    pipe.write(command)
    pipe.flush()
    if expected is not None:
        require(read_line(process, deadline) == expected, "workload protocol mismatch")


def spawn(
    binary: Path, args: list[str | Path], deadline: float
) -> tuple[subprocess.Popen[bytes], int]:
    process = subprocess.Popen(
        [str(binary), *(str(arg) for arg in args)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=CLEAN_ENV,
    )
    try:
        ready = read_line(process, deadline).split()
        require(bool(ready) and ready[0] == b"ready", "workload did not become ready")
        child = int(ready[1]) if len(ready) == 2 else process.pid
        require(len(ready) in (1, 2), "unexpected workload ready record")
        return process, child
    except Exception:
        stop_group(process)
        raise


def stop_group(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    for pipe in (process.stdin, process.stdout):
        if pipe is not None:
            pipe.close()


def cli_json(cli: Path, root: Path, args: list[str], deadline: float) -> dict[str, Any]:
    result = run_command([cli, *args], cwd=root, deadline=deadline, cap=10)
    require(not result.stderr, "successful CLI emitted stderr")
    parsed: object = json.loads(result.stdout)
    require(isinstance(parsed, dict), "CLI did not return a JSON object")
    return cast("dict[str, Any]", parsed)


def _type_matches(value: object, kind: str) -> bool:
    return {
        "null": lambda: value is None,
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float),
        "string": lambda: isinstance(value, str),
        "boolean": lambda: type(value) is bool,
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
    }[kind]()


def validate(value: Any, node: dict[str, Any], path: str = "$") -> None:
    types = node.get("type")
    if types is not None:
        accepted = cast("list[str]", types) if isinstance(types, list) else [str(types)]
        require(any(_type_matches(value, kind) for kind in accepted), f"schema type: {path}")
    if "enum" in node:
        require(value in node["enum"], f"schema enum: {path}")
    if isinstance(value, dict):
        document = cast("dict[str, Any]", value)
        props = cast("dict[str, dict[str, Any]]", node.get("properties", {}))
        for required in cast("list[str]", node.get("required", [])):
            require(required in document, f"schema required: {path}.{required}")
        for key, item in document.items():
            require(key in props, f"schema unknown field: {path}.{key}")
            validate(item, props[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in node:
        for index, item in enumerate(cast("list[Any]", value)):
            validate(item, cast("dict[str, Any]", node["items"]), f"{path}[{index}]")


def schema_contract(
    cli: Path, root: Path, deadline: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    index = cli_json(cli, root, ["schema"], deadline)
    require(index["tool_version"], "schema tool version missing")
    snapshot = cli_json(cli, root, ["schema", "snapshot"], deadline)
    detail = cli_json(cli, root, ["schema", "app"], deadline)
    require(
        snapshot["name"] == "snapshot" and detail["name"] == "app",
        "Darwin schema commands mismatch",
    )
    return snapshot["output"], detail["output"]


def snapshot(cli: Path, root: Path, schema: dict[str, Any], deadline: float) -> dict[str, Any]:
    document = cli_json(cli, root, ["snapshot", "--limit", "1000", "--json"], deadline)
    validate(document, schema)
    require(document["platform"] == "darwin", "installed CLI did not select Darwin")
    system = document["system"]
    require(
        system["physical_bytes"] > 0 and system["free_bytes"] >= 0,
        "host physical/free values missing",
    )
    require(
        system["wired_bytes"] >= 0 and system["compressor_physical_bytes"] >= 0,
        "host wired/compressor values missing",
    )
    require(system["compressor_logical_bytes"] >= 0, "host logical compressor missing")
    require(
        0 <= system["swap_used_bytes"] <= system["swap_total_bytes"],
        "global swap usage is inconsistent",
    )
    pressure = document["pressure"]
    require(
        pressure["level"] in ("normal", "warning", "critical", None),
        "native pressure level invalid",
    )
    require(
        pressure["level"] is not None or pressure["unavailable"] is not None,
        "missing native pressure must be explicit",
    )
    return document


def detail(
    cli: Path, root: Path, app_id: str, schema: dict[str, Any], deadline: float
) -> dict[str, Any]:
    document = cli_json(cli, root, ["app", app_id, "--limit", "1000", "--json"], deadline)
    validate(document, schema)
    require(document["platform"] == "darwin", "detail selected wrong platform")
    require(document["app"]["id"] == app_id, "detail did not resolve stable app id")
    require(
        not document["has_more_processes"] and not document["has_more_commands"],
        "controlled detail was truncated",
    )
    return document


def follow_next(
    cli: Path,
    root: Path,
    document: dict[str, Any],
    schema: dict[str, Any],
    *,
    command: str,
    deadline: float,
) -> dict[str, Any]:
    next_value: object = document.get("next")
    require(isinstance(next_value, list), "published next command is invalid")
    next_argv = cast("list[object]", next_value)
    require(
        len(next_argv) >= 4
        and next_argv[:2] == ["appmem", command]
        and all(isinstance(item, str) for item in next_argv),
        "published next command is invalid",
    )
    continued = cli_json(cli, root, [*cast("list[str]", next_argv[1:]), "--json"], deadline)
    validate(continued, schema)
    return continued


def listed_ids(document: dict[str, Any]) -> set[str]:
    apps = cast("list[dict[str, Any]]", document["apps"])
    return {str(app["id"]) for app in apps}


def pagination(
    cli: Path,
    root: Path,
    snapshot_schema: dict[str, Any],
    app_schema: dict[str, Any],
    *,
    app_ids: tuple[str, str],
    deadline: float,
) -> None:
    first_page = cli_json(cli, root, ["snapshot", "--limit", "1", "--json"], deadline)
    validate(first_page, snapshot_schema)
    require(first_page["has_more"], "controlled snapshot did not publish next")
    page = first_page
    continued = first_page
    for _ in range(4):
        continued = follow_next(
            cli, root, page, snapshot_schema, command="snapshot", deadline=deadline
        )
        if set(app_ids) <= listed_ids(continued):
            break
        require(continued["has_more"], "snapshot next omitted a controlled identity")
        page = continued
    require(
        set(app_ids) <= listed_ids(continued),
        "snapshot next did not reach both controlled identities within four pages",
    )
    for app_id in app_ids:
        first_detail = cli_json(cli, root, ["app", app_id, "--limit", "1", "--json"], deadline)
        validate(first_detail, app_schema)
        require(first_detail["has_more_processes"], "controlled detail did not publish next")
        require(first_detail["app"]["id"] == app_id, "detail page resolved wrong app")
        continued_detail = follow_next(
            cli, root, first_detail, app_schema, command="app", deadline=deadline
        )
        require(
            continued_detail["app"]["id"] == app_id
            and not continued_detail["has_more_processes"]
            and not continued_detail["has_more_commands"],
            "app next did not resolve complete intended app",
        )


def controlled_groups(
    cli: Path,
    root: Path,
    snap: dict[str, Any],
    app_schema: dict[str, Any],
    *,
    controlled: tuple[int, int, int, int],
    deadline: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    parent_pid, child_pid, other_pid, other_child_pid = controlled
    candidates = [app for app in snap["apps"] if app["name"].startswith("Twin")]
    require(len(candidates) >= 2, "two independent Twin bundles were not listed")
    found: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        document = detail(cli, root, candidate["id"], app_schema, deadline)
        pids = {item["pid"] for item in document["processes"]}
        for pid in controlled:
            if pid in pids:
                found[pid] = document
    require(
        set(found) == set(controlled),
        "controlled processes were not fully attributed",
    )
    first, second = found[parent_pid], found[other_pid]
    require(
        found[child_pid]["app"]["id"] == first["app"]["id"],
        "bundleless descendant did not inherit parent app",
    )
    require(
        found[other_child_pid]["app"]["id"] == second["app"]["id"],
        "second bundleless descendant did not inherit parent app",
    )
    require(first["app"]["id"] != second["app"]["id"], "same-name bundles merged")
    require(
        first["app"]["name"] != second["app"]["name"], "same-name bundles were not disambiguated"
    )
    return first, second


def check_detail(document: dict[str, Any], controlled: set[int]) -> None:
    app = document["app"]
    processes = document["processes"]
    coverage = app["coverage"]
    require(controlled <= {proc["pid"] for proc in processes}, "controlled PID absent")
    require(
        coverage["readable_processes"] + coverage["unreadable_processes"] == app["procs"],
        "coverage count mismatch",
    )
    require(
        coverage["unreadable_processes"] == 0 and not coverage["partial"],
        "own controlled process footprint is unreadable",
    )
    require(not coverage["grouping_partial"], "controlled grouping is uncertain")
    require(
        all(proc["footprint_bytes"] is not None for proc in processes),
        "controlled process footprint missing",
    )
    require(
        app["footprint_bytes"] == sum(proc["footprint_bytes"] for proc in processes),
        "app footprint does not sum member footprints",
    )
    for proc in processes:
        require(
            proc["start_abstime"] is not None and proc["unavailable"] is None,
            "own process identity/availability missing",
        )
    commands = document["commands"]
    require(
        all(item["footprint_bytes"] is not None for item in commands),
        "controlled command footprint missing",
    )
    require(
        sum(item["footprint_bytes"] for item in commands) == app["footprint_bytes"],
        "command footprints do not sum app footprint",
    )
    require(
        all(item["readable_processes"] == item["procs"] for item in commands),
        "controlled command coverage incomplete",
    )


def expect_unsupported(cli: Path, root: Path, args: list[str], deadline: float) -> None:
    result = subprocess.run(
        [str(cli), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=remaining(deadline, 10),
        check=False,
        env=CLEAN_ENV,
    )
    require(result.returncode == 2 and not result.stdout, "unsupported flag was accepted")
    last = result.stderr.strip().splitlines()[-1]
    error = cast("dict[str, Any]", json.loads(last))
    require(error["error"]["kind"] == "invalid_input", "unsupported error is unstructured")


def wait_pty(fd: int, markers: tuple[bytes, ...], deadline: float) -> bytes:
    output = bytearray()
    view_deadline = min(deadline, time.monotonic() + 5)
    while not all(marker in output for marker in markers):
        ready, _, _ = select.select([fd], [], [], remaining(view_deadline, 5.0))
        require(bool(ready), "PTY view did not render")
        try:
            chunk = os.read(fd, 65536)
        except OSError as error:
            raise RuntimeError("PTY closed before expected view") from error
        require(bool(chunk), "PTY closed before expected view")
        output.extend(chunk)
        require(len(output) <= 2 * MIB, "PTY output exceeded bound")
    return bytes(output)


def tui(cli: Path, root: Path, deadline: float) -> int:
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 100, 0, 0))
    process = subprocess.Popen(
        [str(cli)],
        cwd=root,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env=CLEAN_ENV,
    )
    os.close(slave)
    start = time.monotonic()
    try:
        transcript = bytearray(wait_pty(master, (b"APP", b"FOOTPRINT", b"PROCS"), deadline))
        # Enter acts on the live table's highlighted row. Its order can change
        # between the CLI sample and this TUI sample, so do not infer its ID.
        os.write(master, b"\r")
        transcript.extend(wait_pty(master, (b"PID", b"COMMAND"), deadline))
        os.write(master, b"g")
        transcript.extend(wait_pty(master, (b"UNREADABLE",), deadline))
        os.write(master, b"\x1b")
        transcript.extend(wait_pty(master, (b"Physical", b"APP", b"PROCS"), deadline))
        os.write(master, b"q")
        exit_deadline = min(deadline, time.monotonic() + 5)
        while process.poll() is None:
            ready, _, _ = select.select([master], [], [], min(0.05, remaining(exit_deadline)))
            if ready:
                with contextlib.suppress(OSError):
                    transcript.extend(os.read(master, 65536))
        with contextlib.suppress(OSError):
            transcript.extend(os.read(master, 65536))
        require(process.returncode == 0, "TUI did not exit successfully")
        require(
            b"\x1b[?1049l" in transcript and b"\x1b[?25h" in transcript,
            "TUI did not restore terminal",
        )
        return int((time.monotonic() - start) * 1000)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.wait(timeout=5)
        os.close(master)


def identities(document: dict[str, Any]) -> dict[int, int]:
    return {int(proc["pid"]): int(proc["start_abstime"]) for proc in document["processes"]}


def footprint(document: dict[str, Any], pid: int, start: int) -> int:
    rows = [
        proc
        for proc in document["processes"]
        if proc["pid"] == pid and proc["start_abstime"] == start
    ]
    require(len(rows) == 1, "known PID/start member was lost")
    value = rows[0]["footprint_bytes"]
    require(type(value) is int and value >= 0, "known member footprint unavailable")
    return cast("int", value)


def measure(
    cli: Path,
    root: Path,
    schema: dict[str, Any],
    baseline: dict[str, Any],
    *,
    leader: subprocess.Popen[bytes],
    child_pid: int,
    deadline: float,
) -> dict[str, int]:
    app_id = baseline["app"]["id"]
    original = identities(baseline)
    require(set(original) == {leader.pid, child_pid}, "controlled bundle membership changed")
    before = footprint(baseline, child_pid, original[child_pid])
    send(leader, b"a", b"allocated", deadline)
    touched = detail(cli, root, app_id, schema, deadline)
    check_detail(touched, set(original))
    require(identities(touched) == original, "process identity changed after allocation")
    peak = footprint(touched, child_pid, original[child_pid])
    require(peak - before >= 80 * MIB, "known child footprint failed to rise")
    send(leader, b"f", b"freed", deadline)
    release_deadline = min(deadline, time.monotonic() + 3)
    while True:
        released = detail(cli, root, app_id, schema, deadline)
        check_detail(released, set(original))
        require(identities(released) == original, "process identity changed after release")
        low = footprint(released, child_pid, original[child_pid])
        if peak - low >= 64 * MIB:
            return {"before_bytes": before, "touched_bytes": peak, "released_bytes": low}
        require(time.monotonic() < release_deadline, "known child footprint failed to fall")
        time.sleep(min(0.05, max(0.0, release_deadline - time.monotonic())))


def collect(
    cli: Path,
    root: Path,
    snapshot_schema: dict[str, Any],
    app_schema: dict[str, Any],
    *,
    first: tuple[subprocess.Popen[bytes], int],
    second: tuple[subprocess.Popen[bytes], int],
    progress: Progress,
    deadline: float,
) -> tuple[dict[str, object], tuple[str, str], list[tuple[int, int]]]:
    first_process, first_child = first
    second_process, second_child = second
    baseline = snapshot(cli, root, snapshot_schema, deadline)
    first_base, second_base = controlled_groups(
        cli,
        root,
        baseline,
        app_schema,
        controlled=(first_process.pid, first_child, second_process.pid, second_child),
        deadline=deadline,
    )
    check_detail(first_base, {first_process.pid, first_child})
    check_detail(second_base, {second_process.pid, second_child})
    first_id = str(first_base["app"]["id"])
    second_id = str(second_base["app"]["id"])
    owned = [*identities(first_base).items(), *identities(second_base).items()]
    require(len(owned) == 4, "controlled PID/start inventory incomplete")
    progress.start("pagination")
    pagination(
        cli,
        root,
        snapshot_schema,
        app_schema,
        app_ids=(first_id, second_id),
        deadline=deadline,
    )
    progress.finish()
    progress.start("footprints")
    first_values = measure(
        cli,
        root,
        app_schema,
        first_base,
        leader=first_process,
        child_pid=first_child,
        deadline=deadline,
    )
    second_values = measure(
        cli,
        root,
        app_schema,
        second_base,
        leader=second_process,
        child_pid=second_child,
        deadline=deadline,
    )
    progress.finish()
    progress.start("pty")
    tui_ms = tui(cli, root, deadline)
    progress.finish()
    facts: dict[str, object] = {
        "bundle_ids_distinct": True,
        "descendant_membership": True,
        "controlled_processes": len(owned),
        "first_child": first_values,
        "second_child": second_values,
        "tui_navigation_scope": "observed live app",
        "tui_session_ms": tui_ms,
    }
    return facts, (first_id, second_id), owned


def integration(python_version: str, progress: Progress, deadline: float) -> dict[str, object]:
    require(
        sys.platform == "darwin" and platform.machine() == "arm64",
        "integration requires macOS ARM64",
    )
    require(os.geteuid() != 0, "integration must run without root")
    with tempfile.TemporaryDirectory(prefix="appmem-installed-") as directory:
        root = Path(directory)
        progress.start("wheel_install")
        cli, version, progress.installed_python = installed_cli(root, python_version, deadline)
        require(
            progress.installed_python.startswith(f"{python_version}."),
            "installed Python does not match requested version",
        )
        progress.finish()
        progress.start("helper_compile")
        first_binary, second_binary, loose_binary = compile_helpers(root, deadline)
        progress.finish()
        progress.start("native_abi")
        counts = native_counts(cli, first_binary, root, deadline)
        progress.finish()
        progress.start("schema")
        snapshot_schema, app_schema = schema_contract(cli, root, deadline)
        progress.finish()
        progress.start("workload_spawn")
        first, child_pid = spawn(first_binary, ["parent", loose_binary], deadline)
        try:
            second, second_child = spawn(second_binary, ["parent", loose_binary], deadline)
            try:
                progress.finish()
                facts, controlled_ids, owned = collect(
                    cli,
                    root,
                    snapshot_schema,
                    app_schema,
                    first=(first, child_pid),
                    second=(second, second_child),
                    progress=progress,
                    deadline=deadline,
                )
                progress.start("unsupported_flags")
                expect_unsupported(cli, root, ["snapshot", "--system", "--json"], deadline)
                expect_unsupported(
                    cli, root, ["app", "Twin", "--scope", "system", "--json"], deadline
                )
                progress.finish()
                progress.start("cleanup")
                send(first, b"q", None, deadline)
                send(second, b"q", None, deadline)
                require(
                    first.wait(timeout=remaining(deadline, 5)) == 0, "first helper failed to exit"
                )
                require(
                    second.wait(timeout=remaining(deadline, 5)) == 0, "second helper failed to exit"
                )
                facts["helper_exit_statuses"] = [first.returncode, second.returncode]
                after = snapshot(cli, root, snapshot_schema, deadline)
                require(
                    not any(app["id"] in controlled_ids for app in after["apps"]),
                    "controlled apps persisted after process exit",
                )
                require_gone(cli, root, owned, deadline)
                progress.finish()
            finally:
                stop_group(second)
        finally:
            stop_group(first)
    return {
        "wheel_version": version,
        **counts,
        **facts,
        "checks": "passed",
        "stages": progress.passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + 180
    progress = Progress()
    metadata = {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "requested_python": args.python,
    }
    try:
        metadata["source_commit"] = run_command(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, deadline=deadline, cap=5
        ).stdout.strip()
        result = {
            **metadata,
            **integration(args.python, progress, deadline),
            "installed_python": progress.installed_python,
        }
    except Exception as error:
        args.output.write_text(
            json.dumps(
                {
                    **metadata,
                    "checks": "failed",
                    "stage": progress.stage,
                    "stages": progress.passed,
                    "error_type": type(error).__name__,
                    "check_id": error.check_id if isinstance(error, CheckError) else None,
                    "installed_python": progress.installed_python,
                },
                indent=2,
            )
            + "\n"
        )
        raise SystemExit(
            f"Darwin integration failed at {progress.stage}: {type(error).__name__}"
        ) from None
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("Darwin installed-wheel integration passed")


if __name__ == "__main__":
    main()
