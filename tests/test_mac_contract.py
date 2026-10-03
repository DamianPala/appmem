"""macOS names, grouping, JSON envelope, errors and the native layer, with fakes only.

The native reader is the only thing replaced; the Linux stub below stands in for the
cgroup tree so no test reads `/proc`, `/sys` or the real config.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import plistlib
import struct
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from appmem import cli as cli_module
from appmem import darwin_backend, darwin_report, darwin_schema, report, schema
from appmem.backend import Backend
from appmem.darwin_backend import DarwinBackend
from appmem.darwin_native import BSDShortInfo, DarwinNative, HostMemory, ReadResult, Unavailable
from appmem.model import AppStats, SystemStats
from appmem.ui.darwin_rows import DarwinRow, sort_rows
from test_darwin_backend import Reader
from test_darwin_header import HOST
from test_swap_activity import STATS

NOW = datetime(2026, 10, 3, 12, 0, 0, 123456, tzinfo=UTC)


class _LinuxStub:
    def __init__(self, stats: SystemStats) -> None:
        self.stats = stats

    def read_system(self) -> SystemStats:
        return self.stats

    def collect_apps(self, **_kwargs: object) -> tuple[list[AppStats], Mapping[str, int]]:
        return [], {}


class _MacStub:
    def read_system(self) -> HostMemory:
        return replace(HOST, swap_in_bytes=333, swap_out_bytes=444)

    def collect_apps(self) -> list[object]:
        return []


def _mac_cli(monkeypatch: pytest.MonkeyPatch, reader: Reader | None = None) -> None:
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    if reader is not None:
        backend = DarwinBackend(501, reader)

        def backend_for_uid(_uid: int) -> DarwinBackend:
            return backend

        monkeypatch.setattr(cli_module, "DarwinBackend", backend_for_uid)


def _error(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.out == ""
    return json.loads(captured.err.splitlines()[-1])["error"]


def _reader(*apps: tuple[str, int]) -> Reader:
    reader = Reader()
    for index, (name, footprint) in enumerate(apps):
        reader.add(
            10 + index, 1, name, f"/Applications/{name}.app/Contents/MacOS/{name}", footprint
        )
    return reader


# --- JSON envelope -----------------------------------------------------------


def test_snapshot_follows_the_linux_envelope() -> None:
    reader = _reader(("Big", 300), ("Mid", 200), ("Small", 100))
    backend = DarwinBackend(501, reader)
    document, total = darwin_report.snapshot_document(backend, limit=2, now=NOW)
    apps = cast("dict[str, Any]", document["apps"])
    assert total == 3
    assert [item["name"] for item in apps["items"]] == ["Big", "Mid"]
    assert apps["has_more"] is True
    assert document["next"] == ["appmem", "app", apps["items"][0]["id"]]
    assert document["taken_at"] == "2026-10-03T12:00:00+00:00"
    linux_document, _ = report.snapshot_document(
        cast("Backend", _LinuxStub(STATS)), include_system=False, limit=2, now=NOW
    )
    assert set(document) - {"platform", "next"} == set(linux_document) - {"next"}
    assert set(cast("dict[str, Any]", document["apps"])) == set(linux_document["apps"])


def test_snapshot_without_apps_has_no_next() -> None:
    document, _ = darwin_report.snapshot_document(DarwinBackend(501, Reader()), limit=5, now=NOW)
    assert document["apps"] == {"items": [], "has_more": False}
    assert "next" not in document


def test_app_document_is_flat_paged_and_ordered_by_size_under_limit() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 10)
    for pid, footprint in ((11, 500), (12, 30), (13, 400), (14, None)):
        reader.add(pid, 10, f"w{pid}", f"/usr/bin/w{pid}", footprint)
    document, procs, commands = darwin_report.app_document(
        DarwinBackend(501, reader), "App", limit=3, now=NOW
    )
    assert (procs, commands) == (5, 5)
    doc = cast("dict[str, Any]", document)
    assert doc["name"] == "App" and doc["procs"] == 5 and "app" not in doc
    assert doc["taken_at"] == "2026-10-03T12:00:00+00:00"
    assert [p["pid"] for p in doc["processes"]["items"]] == [11, 13, 12]
    assert doc["processes"]["has_more"] is True
    assert [c["name"] for c in doc["commands"]["items"]] == ["w11", "w13", "w12"]
    assert doc["commands"]["has_more"] is True
    assert "next" not in doc and "has_more_processes" not in doc


def test_equal_sizes_order_by_name_then_pid() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 1)
    reader.add(11, 10, "b", "/usr/bin/b", 7)
    reader.add(12, 10, "a", "/usr/bin/a", 7)
    reader.add(13, 10, "a", "/usr/bin/a", 7)
    document, _, _ = darwin_report.app_document(DarwinBackend(501, reader), "App", limit=9, now=NOW)
    names = [(p["name"], p["pid"]) for p in cast("dict[str, Any]", document)["processes"]["items"]]
    assert names == [("a", 12), ("a", 13), ("b", 11), ("App", 10)]


def test_swap_activity_values_reach_the_macos_document() -> None:
    document, _ = darwin_report.snapshot_document(
        cast("DarwinBackend", _MacStub()), limit=1, now=NOW
    )
    system = cast("dict[str, Any]", document["system"])
    assert (system["swap_in_bytes"], system["swap_out_bytes"]) == (333, 444)


def test_swap_activity_values_reach_the_linux_document() -> None:
    stats = replace(STATS, swap_in_bytes=111, swap_out_bytes=222)
    document, _ = report.snapshot_document(
        cast("Backend", _LinuxStub(stats)), include_system=False, limit=1, now=NOW
    )
    assert (document["system"]["swap_in_bytes"], document["system"]["swap_out_bytes"]) == (111, 222)


def test_macos_documents_validate_against_the_schema_when_paged() -> None:
    from test_darwin_backend import _assert_contract  # pyright: ignore[reportPrivateUsage]

    reader = _reader(("Big", 300), ("Mid", 200))
    reader.add(30, 10, "worker", "/usr/bin/worker", 5)
    backend = DarwinBackend(501, reader)
    snapshot, _ = darwin_report.snapshot_document(backend, limit=1, now=NOW)
    app_doc, _, _ = darwin_report.app_document(backend, "Big", limit=1, now=NOW)
    for name, document in (("snapshot", snapshot), ("app", app_doc)):
        detail = darwin_schema.detail([name])
        assert detail is not None
        output = cast("dict[str, Any]", detail["output"])
        _assert_contract(document, output, root=True)
        _assert_restricted_keywords(output)


def _assert_restricted_keywords(node: object) -> None:
    allowed = {"type", "enum", "properties", "required", "items", "description"}
    if isinstance(node, dict):
        mapping = cast("dict[str, Any]", node)
        if "type" in mapping:
            assert set(mapping) <= allowed, mapping.keys()
            assert len(mapping.get("description", "")) <= 300
        for value in mapping.values():
            _assert_restricted_keywords(value)
    elif isinstance(node, list):
        for value in cast("list[Any]", node):
            _assert_restricted_keywords(value)


# --- schema index and exit codes ----------------------------------------------


def test_schema_index_names_the_platform_on_both_systems(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert cli_module.main(["schema"], root=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["platform"] == "linux"
    _mac_cli(monkeypatch)
    assert cli_module.main(["schema"], uid=501) == 0
    assert json.loads(capsys.readouterr().out)["platform"] == "darwin"


def test_linux_documents_gain_no_platform_key(tmp_path: Path) -> None:
    document, _ = report.snapshot_document(
        cast("Backend", _LinuxStub(STATS)), include_system=False, limit=1, now=NOW
    )
    assert "platform" not in document


def test_exit_code_text_declares_the_new_failures() -> None:
    assert "unsupported platform" in cast("dict[str, str]", schema.index()["exit_codes"])["1"]
    mac = cast("dict[str, str]", darwin_schema.index()["exit_codes"])["1"]
    assert "unsupported platform" in mac and "retry" in mac
    assert "read_failed" in cli_module.ERROR_KINDS


# --- --system on macOS ---------------------------------------------------------


@pytest.mark.parametrize(
    "argv", [["snapshot", "--system"], ["--system"], ["app", "x", "--scope", "system"], ["--bogus"]]
)
def test_macos_usage_never_advertises_system(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mac_cli(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        cli_module.main(argv, uid=501)
    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    usage = stderr.split("\nappmem: ", 1)[0]
    assert usage.startswith("usage:") and "system" not in usage
    assert json.loads(stderr.splitlines()[-1])["error"]["kind"] == "invalid_input"


def test_linux_usage_still_has_system(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli_module.main(["--bogus"], root=Path("/nonexistent"))
    assert "--system" in capsys.readouterr().err


def test_macos_schema_lists_only_flags_that_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mac_cli(monkeypatch)
    flags: dict[str, list[dict[str, Any]]] = {}
    for path in ([], ["snapshot"], ["app"]):
        assert cli_module.main(["schema", *path], uid=501) == 0
        document = json.loads(capsys.readouterr().out)
        flags["/".join(path)] = (document.get("command") or document)["flags"]
    assert all(flag["name"] != "system" for listed in flags.values() for flag in listed)
    scope = next(flag for flag in flags["app"] if flag["name"] == "scope")
    assert scope["enum"] == ["user"]


# --- errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "failing"),
    [(["snapshot", "--json"], "pids"), (["snapshot", "--json"], "host"), (["app", "x"], "pids")],
)
def test_transient_native_read_failure_is_read_failed_not_platform_unavailable(
    command: list[str],
    failing: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader = _reader(("App", 1))
    failure: ReadResult[object] = ReadResult(None, Unavailable.ERROR)
    monkeypatch.setattr(reader, failing, lambda: failure)
    _mac_cli(monkeypatch, reader)
    with pytest.raises(SystemExit) as raised:
        cli_module.main(command, uid=501)
    assert raised.value.code == 1
    error = _error(capsys)
    assert error["kind"] == "read_failed"
    assert "again" in error["hint"] and "Apple Silicon" not in error["hint"]


@pytest.mark.parametrize(
    ("machine", "version", "accepted"),
    [
        ("arm64", "15.0", True),
        ("arm64", "27.0.1", True),
        ("arm64", "14.7", False),
        ("x86_64", "15.0", False),
        ("arm64", "", False),
        ("arm64", "not-a-version", False),
    ],
)
def test_unsupported_mac_is_platform_unavailable_with_exit_one(
    machine: str,
    version: str,
    accepted: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(darwin_backend.platform, "machine", lambda: machine)
    monkeypatch.setattr(darwin_backend.platform, "mac_ver", lambda: (version, ("", "", ""), ""))
    monkeypatch.setattr(darwin_backend, "DarwinNative", lambda: _reader(("App", 1)))
    if accepted:
        assert cli_module.main(["snapshot", "--json"], uid=501) == 0
        assert json.loads(capsys.readouterr().out)["platform"] == "darwin"
        return
    with pytest.raises(SystemExit) as raised:
        cli_module.main(["snapshot", "--json"], uid=501)
    assert raised.value.code == 1
    error = _error(capsys)
    assert error["kind"] == "platform_unavailable"
    assert "macOS 15" in error["hint"]


def test_rosetta_python_is_told_apart_from_an_intel_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(darwin_backend.sys, "platform", "darwin")
    monkeypatch.setattr(darwin_backend.platform, "machine", lambda: "x86_64")
    with pytest.raises(darwin_backend.DarwinUnavailableError, match="native arm64 Python"):
        DarwinBackend(501)


def test_app_not_found_lists_close_candidates_and_a_next_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = Reader()
    for pid in (10, 20):
        reader.add(pid, 1, "mdworker_share", "/System/mdworker_shared", 3)
    reader.add(30, 1, "sshd-session", "/usr/libexec/sshd-session", 9)
    reader.add(40, 1, "bad\x1b[31mname", "/usr/bin/mdworker\x1b[31mbad", 1)
    _mac_cli(monkeypatch, reader)
    with pytest.raises(SystemExit) as raised:
        cli_module.main(["app", "mdworker", "--json"], uid=501)
    assert raised.value.code == 1
    error = _error(capsys)
    assert error["kind"] == "not_found"
    assert "mdworker_shared" in error["hint"] and "sshd-session" not in error["hint"]
    assert "\x1b" not in error["hint"]
    assert error["next"] == ["appmem", "snapshot", "--json"]
    with pytest.raises(SystemExit):
        cli_module.main(["app", "zzz"], uid=501)
    error = _error(capsys)
    assert "close matches" not in error["hint"] and error["next"] == ["appmem", "snapshot"]


# --- grouping coverage and per-user filtering -------------------------------------


def test_other_users_are_never_listed_and_their_parent_is_not_a_gap() -> None:
    reader = _reader(("Mine", 100))
    reader.add(20, 1, "rootd", "/usr/sbin/rootd", 999, uid=0)
    reader.add(21, 20, "child", "/usr/bin/child", 50)
    reader.add(30, 1, "theirs", "/Applications/Theirs.app/Contents/MacOS/Theirs", 7, uid=502)
    apps = {app.name: app for app in DarwinBackend(501, reader).collect_apps()}
    assert set(apps) == {"Mine", "child"}
    assert apps["child"].footprint_bytes == 50
    assert not apps["child"].grouping_partial


@pytest.mark.parametrize("missing", ["path", "start"])
def test_a_member_with_no_path_or_start_makes_grouping_partial(missing: str) -> None:
    reader = _reader(("App", 100))
    if missing == "path":
        reader.add(30, 10, "helper", None, 5, start=300)
    else:
        reader.add(30, 10, "helper", "/usr/bin/helper", None)
    (app,) = DarwinBackend(501, reader).collect_apps()
    assert app.procs == 2 and app.grouping_partial
    complete = _reader(("App", 100))
    complete.add(30, 10, "helper", "/usr/bin/helper", 5, start=300)
    assert not DarwinBackend(501, complete).collect_apps()[0].grouping_partial


def test_descending_sort_keeps_ties_a_to_z() -> None:
    rows = [
        DarwinRow(name, name, 1, None, procs, False)
        for name, procs in (("b", 3), ("A", 3), ("c", 5), ("d", 1), ("e", 3))
    ]
    for reverse, expected in ((True, "cAbed"), (False, "dAbec")):
        assert "".join(r.name for r in sort_rows(rows, "procs", reverse)) == expected


# --- text ----------------------------------------------------------------------


def test_text_snapshot_does_not_qualify_pressure_and_footnotes_only_when_needed() -> None:
    complete = DarwinBackend(501, _reader(("App", 1024**2)))
    document, _ = darwin_report.snapshot_document(complete, limit=5, now=NOW)
    text = darwin_report.render_snapshot_text(document)
    assert "Pressure  normal\n" in text + "\n" and "current user" not in text
    assert "partial known sum" not in text
    reader = _reader(("App", 1024**2))
    reader.add(30, 10, "helper", "/usr/bin/helper", None)
    document, _ = darwin_report.snapshot_document(DarwinBackend(501, reader), limit=5, now=NOW)
    assert "* partial known sum" in darwin_report.render_snapshot_text(document)


# --- native layer ----------------------------------------------------------------


def _native(monkeypatch: pytest.MonkeyPatch, **proc: Any) -> DarwinNative:
    reader = object.__new__(DarwinNative)
    monkeypatch.setattr(reader, "_proc", type("Proc", (), proc)(), raising=False)
    return reader


def _write(buffer: Any, data: bytes) -> None:
    ctypes.memmove(buffer, data, len(data))


def test_path_returns_the_executable_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def pidpath(_self: object, _pid: int, buffer: Any, _size: int) -> int:
        raw = b"/Applications/Caf\xc3\xa9.app/Contents/MacOS/Caf\xc3\xa9"
        _write(buffer, raw + b"\0")
        return len(raw)

    result = _native(monkeypatch, proc_pidpath=pidpath).path(7)
    assert result.value == "/Applications/Café.app/Contents/MacOS/Café"


def test_path_filling_the_buffer_is_a_truncation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def pidpath(_self: object, _pid: int, buffer: Any, size: int) -> int:
        _write(buffer, b"/short\0")  # a NUL exists, so only the length can reveal truncation
        return size

    result = _native(monkeypatch, proc_pidpath=pidpath).path(7)
    assert result.value is None and result.unavailable == Unavailable.ERROR


def test_path_failure_is_classified_by_errno(monkeypatch: pytest.MonkeyPatch) -> None:
    def pidpath(_self: object, *_args: object) -> int:
        ctypes.set_errno(errno.ESRCH)
        return 0

    result = _native(monkeypatch, proc_pidpath=pidpath).path(7)
    assert result.value is None and result.unavailable == Unavailable.VANISHED


def test_memory_decodes_footprint_resident_and_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def rusage(_self: object, _pid: int, flavor: int, buffer: Any) -> int:
        assert flavor == 4
        usage = buffer._obj
        usage.phys_footprint, usage.resident_size, usage.proc_start_abstime = 111, 222, 333
        return 0

    result = _native(monkeypatch, proc_pid_rusage=rusage).memory(7)
    assert result.value is not None
    assert (
        result.value.footprint_bytes,
        result.value.resident_bytes,
        result.value.start_abstime,
    ) == (111, 222, 333)


def test_memory_denied_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    def rusage(_self: object, *_args: object) -> int:
        ctypes.set_errno(errno.EPERM)
        return -1

    result = _native(monkeypatch, proc_pid_rusage=rusage).memory(7)
    assert result.value is None and result.unavailable == Unavailable.DENIED


def _pidinfo(pid_in_reply: int, reply_size: int | None) -> Any:
    def pidinfo(_self: object, _pid: int, _flavor: int, _arg: int, buffer: Any, size: int) -> int:
        info = buffer._obj
        info.pid, info.ppid, info.uid, info.comm = pid_in_reply, 1, 501, b"comm"
        return size if reply_size is None else reply_size

    return pidinfo


def test_process_read_decodes_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _native(monkeypatch, proc_pidinfo=_pidinfo(7, None)).process(7)
    assert result.value is not None
    assert (result.value.pid, result.value.ppid, result.value.uid) == (7, 1, 501)
    assert result.value.command == "comm"


def test_short_process_read_fails_on_its_size_with_a_matching_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = _native(monkeypatch, proc_pidinfo=_pidinfo(7, ctypes.sizeof(BSDShortInfo) - 4))
    result = native.process(7)
    assert result.value is None and result.unavailable == Unavailable.ERROR


def test_process_reply_for_another_pid_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _native(monkeypatch, proc_pidinfo=_pidinfo(8, None)).process(7)
    assert result.value is None and result.unavailable == Unavailable.ERROR


def _bundle(tmp_path: Path, content: bytes | None) -> str:
    bundle = tmp_path / "Some.app"
    (bundle / "Contents").mkdir(parents=True)
    if content is not None:
        (bundle / "Contents" / "Info.plist").write_bytes(content)
    return str(bundle)


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY])
def test_bundle_id_reads_only_the_identifier(tmp_path: Path, fmt: plistlib.PlistFormat) -> None:
    plist = {"CFBundleIdentifier": "com.example.some", "CFBundleName": "Other"}
    bundle = _bundle(tmp_path, plistlib.dumps(plist, fmt=fmt))
    assert object.__new__(DarwinNative).bundle_id(bundle).value == "com.example.some"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, Unavailable.VANISHED),
        (b"not a plist", Unavailable.ERROR),
        (plistlib.dumps({"CFBundleName": "x"}), Unavailable.UNSUPPORTED),
        (plistlib.dumps({"CFBundleIdentifier": 7}), Unavailable.UNSUPPORTED),
        (plistlib.dumps(["list"]), Unavailable.ERROR),
        (b"x" * ((1 << 20) + 1), Unavailable.ERROR),
    ],
)
def test_bundle_id_failures_are_explicit(
    tmp_path: Path, content: bytes | None, reason: Unavailable
) -> None:
    result = object.__new__(DarwinNative).bundle_id(_bundle(tmp_path, content))
    assert result.value is None and result.unavailable == reason


def test_bundle_id_does_not_block_on_a_fifo(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, None)
    os.mkfifo(f"{bundle}/Contents/Info.plist")
    result = object.__new__(DarwinNative).bundle_id(bundle)
    assert result.value is None and result.unavailable == Unavailable.ERROR


def test_bundle_id_survives_a_deeply_nested_binary_plist(tmp_path: Path) -> None:
    # Each array holds one reference to the next: one parser recursion per level.
    depth = 50_000
    body = b"".join(b"\xa1" + (i + 1).to_bytes(4, "big") for i in range(depth - 1)) + b"\xa0"
    offsets = b"".join((8 + 5 * i).to_bytes(4, "big") for i in range(depth))
    trailer = struct.pack(">6xBBQQQ", 4, 4, depth, 0, 8 + len(body))
    bundle = _bundle(tmp_path, b"bplist00" + body + offsets + trailer)
    result = object.__new__(DarwinNative).bundle_id(bundle)
    assert result.value is None and result.unavailable == Unavailable.ERROR
