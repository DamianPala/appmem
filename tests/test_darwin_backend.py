"""Darwin collection contracts with a synthetic native reader only."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import EllipsisType
from typing import Any, cast

import pytest
from textual.content import Content
from textual.widgets import Static

from appmem import cli as cli_module
from appmem import darwin_backend, darwin_report, darwin_schema
from appmem.darwin_backend import DarwinBackend
from appmem.darwin_native import (
    HostMemory,
    ProcessIdentity,
    ProcessMemory,
    ReadResult,
    Unavailable,
)
from appmem.ui.app import AppMemApp
from appmem.ui.darwin_rows import build_rows, update_baseline
from appmem.ui.screens.darwin import (
    DarwinDetailTick,
    DarwinHelpScreen,
    DarwinMainScreen,
    DarwinMainTick,
    DarwinProcessesScreen,
)
from appmem.ui.table import RowTable


class Reader:
    def __init__(self) -> None:
        self.identities: dict[int, ProcessIdentity] = {}
        self.paths: dict[int, str] = {}
        self.memories: dict[int, ProcessMemory | None] = {}
        self.memory_calls: dict[int, int] = {}
        self.process_calls: dict[int, int] = {}
        self.bundle_ids: dict[str, str] = {}
        self.compressions: dict[int, int] = {}
        self.responsibles: dict[int, int] = {}
        self.responsible_calls: list[int] = []
        self.responsible_missing = False
        self.reuse_pid: int | None = None
        self.reuse_start_pid: int | None = None
        self.fresh_command_pid: int | None = None
        self.host_value = HostMemory(
            16_000,
            16_384,
            38,
            2_000,
            3_000,
            4_000,
            5_000,
            600,
            1_200,
            100,
            0,
            0,
            1,
            None,
            None,
        )

    def add(
        self,
        pid: int,
        ppid: int,
        command: str,
        path: str | None,
        footprint: int | None,
        *,
        start: int | None = None,
        uid: int = 501,
        compressed: int | EllipsisType | None = ...,
    ) -> None:
        """`compressed` defaults to a quarter of a readable footprint; None is unreadable."""
        if compressed is ...:
            compressed = footprint // 4 if footprint is not None else None
        if compressed is not None:
            self.compressions[pid] = compressed
        self.identities[pid] = ProcessIdentity(pid, ppid, uid, 1, command)
        if path is not None:
            self.paths[pid] = path
        self.memories[pid] = (
            ProcessMemory(footprint, footprint, start or pid * 10)
            if footprint is not None
            else None
        )

    def host(self) -> ReadResult[HostMemory]:
        return ReadResult(self.host_value)

    def pids(self) -> ReadResult[list[int]]:
        return ReadResult(list(self.identities))

    def process(self, pid: int) -> ReadResult[ProcessIdentity]:
        self.process_calls[pid] = self.process_calls.get(pid, 0) + 1
        value = self.identities[pid]
        if pid == self.reuse_pid and self.memory_calls.get(pid, 0):
            return ReadResult(ProcessIdentity(pid, pid + 1, 501, 1, value.command))
        if pid == self.fresh_command_pid and self.process_calls[pid] > 1:
            return ReadResult(ProcessIdentity(pid, value.ppid, value.uid, value.status, "New"))
        return ReadResult(value)

    def path(self, pid: int) -> ReadResult[str]:
        value = self.paths.get(pid)
        return ReadResult(value, None if value is not None else Unavailable.DENIED)

    def memory(self, pid: int) -> ReadResult[ProcessMemory]:
        self.memory_calls[pid] = self.memory_calls.get(pid, 0) + 1
        value = self.memories[pid]
        if pid == self.reuse_start_pid and self.memory_calls[pid] == 2 and value is not None:
            value = ProcessMemory(
                value.footprint_bytes, value.resident_bytes, value.start_abstime + 1
            )
        return ReadResult(value, None if value is not None else Unavailable.DENIED)

    def compressed(self, pid: int) -> ReadResult[int]:
        value = self.compressions.get(pid)
        return ReadResult(value, None if value is not None else Unavailable.DENIED)

    def responsible(self, pid: int) -> ReadResult[int]:
        self.responsible_calls.append(pid)
        value = None if self.responsible_missing else self.responsibles.get(pid)
        return ReadResult(value, None if value is not None else Unavailable.UNSUPPORTED)

    def bundle_id(self, bundle: str) -> ReadResult[str]:
        value = self.bundle_ids.get(bundle)
        return ReadResult(value, None if value is not None else Unavailable.UNSUPPORTED)


def _assert_contract(value: Any, field: dict[str, Any], *, root: bool = False) -> None:
    if not root:
        assert field.get("description"), field
    allowed = field["type"]
    allowed_types = [allowed] if isinstance(allowed, str) else allowed
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    assert any(type(value) is types[name] for name in allowed_types), (value, field)
    if "enum" in field:
        assert value in field["enum"]
    if isinstance(value, dict):
        value = cast("dict[str, Any]", value)
        properties = field["properties"]
        assert set(field.get("required", [])) <= value.keys()
        assert value.keys() <= properties.keys()
        for key, item in value.items():
            _assert_contract(item, properties[key])
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            _assert_contract(item, field["items"])


def test_nested_bundle_and_terminal_child_join_outer_app() -> None:
    reader = Reader()
    reader.add(10, 1, "Outer", "/Applications/Outer.app/Contents/MacOS/Outer", 100)
    reader.add(
        11,
        10,
        "Nested",
        "/Applications/Outer.app/Contents/Helpers/Nested.app/Contents/MacOS/Nested",
        20,
    )
    reader.add(12, 11, "worker", "/usr/bin/worker", 30)
    apps = DarwinBackend(501, reader).collect_apps()
    assert len(apps) == 1
    assert apps[0].name == "Outer"
    assert apps[0].footprint_bytes == 150
    assert tuple(p.pid for p in apps[0].members) == (10, 11, 12)
    assert reader.memory_calls == {10: 2, 11: 2, 12: 2}


def _twin_editors(reader: Reader) -> None:
    reader.add(10, 1, "Editor", "/Applications/Editor.app/Contents/MacOS/Editor", 100)
    reader.add(20, 1, "Editor", "/Users/me/Editor.app/Contents/MacOS/Editor", 200)
    reader.bundle_ids["/Applications/Editor.app"] = "com.example.editor"
    reader.bundle_ids["/Users/me/Editor.app"] = "com.example.editor.beta"


def test_same_name_bundles_are_told_apart_by_bundle_id_not_a_hash() -> None:
    reader = Reader()
    _twin_editors(reader)
    backend = DarwinBackend(501, reader)
    apps = backend.collect_apps()
    assert {app.name for app in apps} == {
        "Editor (com.example.editor)",
        "Editor (com.example.editor.beta)",
    }
    assert len({app.id for app in apps}) == 2
    assert not any("[" in app.name for app in apps)
    assert backend.find_app(apps[0].id) is not None


def test_a_twin_quitting_does_not_break_the_name_an_agent_kept() -> None:
    reader = Reader()
    _twin_editors(reader)
    backend = DarwinBackend(501, reader)
    kept = "Editor (com.example.editor.beta)"
    app_id = next(app.id for app in backend.collect_apps() if app.name == kept)
    assert backend.find_app("Editor") is None  # two candidates: a plain name cannot choose
    reader.identities.pop(10)
    alone = backend.collect_apps()
    assert [app.name for app in alone] == ["Editor"]
    assert alone[0].id == app_id
    assert backend.find_app(kept) is not None
    assert backend.find_app("Editor") is not None
    assert backend.find_app(app_id) is not None


def test_bundle_without_a_readable_id_keeps_its_plain_name() -> None:
    reader = Reader()
    reader.add(10, 1, "Editor", "/Applications/Editor.app/Contents/MacOS/Editor", 100)
    reader.add(20, 1, "Editor", "/Users/me/Editor.app/Contents/MacOS/Editor", 200)
    apps = DarwinBackend(501, reader).collect_apps()
    assert [app.name for app in apps] == ["Editor", "Editor"]
    assert len({app.id for app in apps}) == 2


def test_names_come_from_the_executable_not_the_15_character_comm() -> None:
    reader = Reader()
    reader.add(10, 1, "containermanage", "/usr/libexec/containermanagerd", 100)
    reader.add(
        20, 1, "Google Chrome He", "/opt/Chrome.app/Contents/Frameworks/x/Google Chrome Helper", 7
    )
    reader.add(
        21,
        20,
        "Google Chrome He",
        "/opt/Chrome.app/Contents/Frameworks/x/Google Chrome Helper (GPU)",
        5,
    )
    reader.add(22, 20, "unreadable-path-proc", None, 3)
    apps = {app.name: app for app in DarwinBackend(501, reader).collect_apps()}
    assert set(apps) == {"containermanagerd", "Chrome"}
    assert [p.command for p in apps["Chrome"].members] == [
        "Google Chrome Helper",
        "Google Chrome Helper (GPU)",
        "unreadable-path-proc",
    ]


def test_same_named_roots_without_a_bundle_are_one_app() -> None:
    reader = Reader()
    for root, pid in enumerate((10, 20, 30)):
        reader.add(pid, 1, "mdworker_share", "/System/Library/mdworker_shared", 10 * (root + 1))
    reader.add(31, 30, "child", "/usr/bin/child", 5)
    reader.add(40, 1, "other", "/usr/bin/other", 1)
    backend = DarwinBackend(501, reader)
    apps = {app.name: app for app in backend.collect_apps()}
    merged = apps["mdworker_shared"]
    assert merged.procs == 4
    assert merged.footprint_bytes == 65
    assert set(apps) == {"mdworker_shared", "other"}
    assert not any("[" in name for name in apps)
    assert backend.find_app("mdworker_shared") == merged
    again = DarwinBackend(501, reader).collect_apps()
    assert {app.id for app in again} == {app.id for app in apps.values()}


def test_a_bundle_and_a_bundleless_root_with_one_name_stay_separate() -> None:
    reader = Reader()
    reader.add(10, 1, "Tool", "/Applications/Tool.app/Contents/MacOS/Tool", 100)
    reader.add(20, 1, "Tool", "/usr/local/bin/Tool", 50)
    reader.bundle_ids["/Applications/Tool.app"] = "com.example.tool"
    apps = DarwinBackend(501, reader).collect_apps()
    assert [app.name for app in apps] == ["Tool (com.example.tool)", "Tool"]


def test_framework_python_app_is_not_an_application_bundle() -> None:
    reader = Reader()
    python = "/opt/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
    reader.add(10, 1, "sshd-session", "/usr/libexec/sshd-session", 40)
    reader.add(11, 10, "Python", python, 60)
    reader.add(20, 1, "Other", "/opt/Other.app/Contents/Frameworks/Python.framework/x/Python", 5)
    apps = {app.name: app for app in DarwinBackend(501, reader).collect_apps()}
    assert set(apps) == {"sshd-session", "Other"}
    assert [p.pid for p in apps["sshd-session"].members] == [10, 11]


def test_control_characters_stay_in_names_and_are_escaped_where_shown() -> None:
    reader = Reader()
    reader.add(10, 1, "daemon", "/Applications/\x1b[41mEVIL\x1b[0m.app/Contents/MacOS/x", 100)
    reader.add(11, 10, "w", "/tmp/worker\x07", 5)
    backend = DarwinBackend(501, reader)
    now = datetime(2026, 9, 26, tzinfo=UTC)
    snapshot, _ = darwin_report.snapshot_document(backend, limit=5, now=now)
    text = darwin_report.render_snapshot_text(snapshot)
    assert "\\x1b[41mEVIL" in text and "\x1b" not in text and "?[41m" not in text
    app_id = cast("dict[str, Any]", snapshot["apps"])["items"][0]["id"]
    document, _, _ = darwin_report.app_document(backend, app_id, limit=5, now=now)
    detail = darwin_report.render_app_text(document)
    assert "worker\\x07" in detail and "\x07" not in detail


def test_own_bundle_beats_launcher_and_bundleless_child_uses_nearest_app() -> None:
    reader = Reader()
    reader.add(10, 1, "Terminal", "/Applications/Terminal.app/Contents/MacOS/Terminal", 100)
    reader.add(11, 10, "Editor", "/Applications/Editor.app/Contents/MacOS/Editor", 200)
    reader.add(12, 11, "worker", "/usr/bin/worker", 30)
    apps = DarwinBackend(501, reader).collect_apps()
    assert {app.name: tuple(p.pid for p in app.members) for app in apps} == {
        "Editor": (11, 12),
        "Terminal": (10,),
    }


def test_differently_named_bundleless_roots_and_partial_coverage() -> None:
    reader = Reader()
    reader.add(10, 1, "daemon", "/usr/bin/daemon", 100)
    reader.add(11, 10, "child", None, None)
    reader.add(20, 1, "other", "/usr/bin/other", None)
    apps = DarwinBackend(501, reader).collect_apps()
    assert len(apps) == 2
    assert len({app.id for app in apps}) == 2
    first = next(app for app in apps if any(p.pid == 10 for p in app.members))
    denied = next(app for app in apps if any(p.pid == 20 for p in app.members))
    assert first.footprint_bytes == 100
    assert first.readable_processes == first.unreadable_processes == 1
    assert denied.footprint_bytes is None
    assert denied.unreadable_processes == 1


def test_merged_roots_are_partial_when_any_member_is_unreadable() -> None:
    reader = Reader()
    reader.add(10, 1, "daemon", "/usr/bin/daemon", 100)
    reader.add(20, 1, "daemon", "/usr/bin/daemon", None)
    (app,) = DarwinBackend(501, reader).collect_apps()
    assert app.procs == 2 and app.footprint_bytes == 100
    assert app.readable_processes == app.unreadable_processes == 1 and app.partial


def test_launchd_webkit_service_stays_separate_from_safari() -> None:
    reader = Reader()
    reader.add(10, 1, "Safari", "/Applications/Safari.app/Contents/MacOS/Safari", 100)
    reader.add(
        20,
        1,
        "WebContent",
        "/System/Library/Frameworks/WebKit.framework/XPCServices/WebContent.xpc/Contents/MacOS/WebContent",
        200,
    )
    apps = DarwinBackend(501, reader).collect_apps()
    assert len(apps) == 2
    assert {app.name: tuple(member.pid for member in app.members) for app in apps} == {
        "Safari": (10,),
        "WebContent": (20,),
    }
    assert all(not app.grouping_partial for app in apps)


def test_missing_cycle_and_pid_reuse_are_bounded() -> None:
    reader = Reader()
    reader.add(10, 11, "a", None, 100)
    reader.add(11, 10, "b", None, 200)
    reader.add(30, 99, "orphan", None, None)
    reader.add(40, 1, "reused", None, 50)
    reader.add(50, 1, "restarted", "/Applications/Old.app/Contents/MacOS/Old", 80)
    reader.reuse_pid = 40
    reader.reuse_start_pid = 50
    apps = DarwinBackend(501, reader).collect_apps()
    assert all(all(p.pid != 40 for p in app.members) for app in apps)
    assert all(all(p.pid != 50 for p in app.members) for app in apps)
    assert any(app.grouping_partial for app in apps)
    assert any(app.footprint_bytes is None for app in apps)


def test_fresh_metadata_labels_the_bracketed_process_sample() -> None:
    reader = Reader()
    reader.add(10, 1, "Old", "/Applications/New.app/Contents/MacOS/New", 100)
    reader.fresh_command_pid = 10
    app = DarwinBackend(501, reader).collect_apps()[0]
    assert app.name == "New"
    assert app.members[0].command == "New"
    assert app.members[0].footprint_bytes == 100


def test_reused_parent_pid_cannot_absorb_an_older_child() -> None:
    reader = Reader()
    reader.add(20, 1, "New", "/Applications/New.app/Contents/MacOS/New", 100, start=200)
    reader.add(30, 20, "worker", "/usr/bin/worker", 50, start=100)
    apps = DarwinBackend(501, reader).collect_apps()
    assert len(apps) == 2
    child_group = next(app for app in apps if any(member.pid == 30 for member in app.members))
    assert child_group.name == "worker"
    assert child_group.grouping_partial
    assert tuple(member.pid for member in child_group.members) == (30,)


def test_report_schema_exposes_footprint_and_nullable_coverage() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "secret\nname\x9bmore", None, None)
    backend = DarwinBackend(501, reader)
    now = datetime(2026, 9, 26, tzinfo=UTC)
    raw_snapshot, _ = darwin_report.snapshot_document(backend, limit=10, now=now)
    snapshot = cast("dict[str, Any]", raw_snapshot)
    item = snapshot["apps"]["items"][0]
    assert item["footprint_bytes"] == 100
    assert item["coverage"]["partial"] is True
    assert snapshot["system"]["swap_used_bytes"] == 0
    assert "ram_bytes" not in item and "swap_bytes" not in item
    raw_document, _, _ = darwin_report.app_document(backend, item["id"], limit=10, now=now)
    document = cast("dict[str, Any]", raw_document)
    assert document["commands"]["items"][1]["footprint_bytes"] is None
    assert document["processes"]["items"][1]["name"] == "secret\nname\x9bmore"
    schema_detail = darwin_schema.detail(["app"])
    assert schema_detail is not None
    output = cast("dict[str, Any]", schema_detail["output"])
    assert output["properties"]["footprint_bytes"]["type"] == ["integer", "null"]


def test_darwin_schema_describes_and_validates_emitted_documents() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", None, None)
    backend = DarwinBackend(501, reader)
    now = datetime(2026, 9, 26, tzinfo=UTC)
    snapshot, _ = darwin_report.snapshot_document(backend, limit=1, now=now)
    app_document, _, _ = darwin_report.app_document(
        backend, cast("dict[str, Any]", snapshot["apps"])["items"][0]["id"], limit=1, now=now
    )
    for name, document in (("snapshot", snapshot), ("app", app_document)):
        schema_detail = darwin_schema.detail([name])
        assert schema_detail is not None
        _assert_contract(document, cast("dict[str, Any]", schema_detail["output"]), root=True)


def test_unknown_native_pressure_level_has_an_explicit_reason() -> None:
    reader = Reader()
    reader.host_value = replace(reader.host_value, pressure_level=3, pressure_unavailable=None)
    document, _ = darwin_report.snapshot_document(
        DarwinBackend(501, reader), limit=1, now=datetime(2026, 9, 26, tzinfo=UTC)
    )
    assert cast("dict[str, Any]", document["pressure"])["unavailable"] == "unknown_level"


def test_darwin_cli_snapshot_and_app_by_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    backend = DarwinBackend(501, reader)

    def backend_for_uid(uid: int) -> DarwinBackend:
        assert uid == 501
        return backend

    monkeypatch.setattr(darwin_backend, "DarwinBackend", backend_for_uid)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    assert cli_module.main(["snapshot", "--json"], uid=501) == 0
    snapshot = json.loads(capsys.readouterr().out)
    app_id = snapshot["apps"]["items"][0]["id"]
    assert snapshot["next"] == ["appmem", "app", app_id, "--json"]
    assert cli_module.main(["app", app_id, "--json"], uid=501) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["id"] == app_id
    assert detail["commands"]["items"][0]["footprint_bytes"] == 100


@pytest.mark.parametrize("command", ["snapshot", "app"])
@pytest.mark.parametrize("stage", ["backend", "document"])
def test_darwin_named_command_interrupt_is_structured(
    command: str,
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    backend = DarwinBackend(501, reader)

    def interrupt() -> None:
        raise KeyboardInterrupt

    def interrupt_backend(uid: int) -> DarwinBackend:
        assert uid == 501
        raise KeyboardInterrupt

    def backend_for_uid(uid: int) -> DarwinBackend:
        assert uid == 501
        return backend

    if stage == "backend":
        monkeypatch.setattr(darwin_backend, "DarwinBackend", interrupt_backend)
    else:
        monkeypatch.setattr(darwin_backend, "DarwinBackend", backend_for_uid)
        monkeypatch.setattr(reader, "pids", interrupt)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    args = ["snapshot", "--json"] if command == "snapshot" else ["app", "App", "--json"]
    with pytest.raises(SystemExit) as error:
        cli_module.main(args, uid=501, stdout_isatty=lambda: True)
    assert error.value.code == 130
    output = capsys.readouterr()
    assert output.out == ""
    assert "Traceback" not in output.err
    assert json.loads(output.err.splitlines()[-1])["error"]["kind"] == "interrupted"


def test_darwin_json_next_keeps_json_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "worker", "/usr/bin/worker", 20)
    reader.add(20, 1, "Other", "/Applications/Other.app/Contents/MacOS/Other", 200)
    backend = DarwinBackend(501, reader)

    def backend_for_uid(uid: int) -> DarwinBackend:
        assert uid == 501
        return backend

    monkeypatch.setattr(darwin_backend, "DarwinBackend", backend_for_uid)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    args = ["snapshot", "--limit", "1", "--json"]
    assert cli_module.main(args, uid=501, stdout_isatty=lambda: True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["apps"]["has_more"] is True
    assert first["next"][-1] == "--json"
    assert cli_module.main(first["next"][1:], uid=501, stdout_isatty=lambda: True) == 0
    continued = json.loads(capsys.readouterr().out)
    assert continued["platform"] == "darwin"
    assert continued["name"] == "Other"
    assert "next" not in continued


def test_darwin_system_scope_is_structured_invalid_input(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    with pytest.raises(SystemExit) as error:
        cli_module.main(["snapshot", "--system"], uid=501)
    assert error.value.code == 2
    assert '"kind": "invalid_input"' in capsys.readouterr().err


def test_unsupported_os_fails_before_linux_or_native_collection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    with pytest.raises(SystemExit) as error:
        cli_module.main(["snapshot", "--json"])
    assert error.value.code == 1
    assert '"kind": "platform_unavailable"' in capsys.readouterr().err


def test_darwin_schema_needs_no_native_collection(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    assert cli_module.main(["schema", "snapshot"], uid=501) == 0
    assert '"footprint_bytes"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "markers"),
    [
        (
            ["--help"],
            (
                "-i, --interval",
                "default: 1",
                "appmem snapshot --help",
                "f/c/d/r",
                "f/c/r/n/p/u",
                "Esc back",
                "PgUp/PgDn",
                "Ctrl+P",
            ),
        ),
        (["snapshot", "--help"], ("--limit N", "default: 50", "--system is unsupported")),
        (["app", "--help"], ("--scope user", "default: user", "default: 100")),
    ],
)
def test_darwin_help_lists_supported_flags_and_defaults(
    argv: list[str],
    markers: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    with pytest.raises(SystemExit) as error:
        cli_module.main(argv, uid=501)
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert all(marker in help_text for marker in markers)
    assert "idle pages were paged out" not in help_text


def test_darwin_schema_index_matches_cli_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    assert cli_module.main(["schema", "--json"], uid=501) == 0
    assert json.loads(capsys.readouterr().out) == darwin_schema.index()


def test_growth_requires_complete_coverage_and_resets_after_reopen() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    backend = DarwinBackend(501, reader)
    initial = backend.collect_apps()
    baseline = update_baseline(initial, {})
    assert build_rows(initial, baseline)[0].delta_bytes == 0

    reader.memories[10] = ProcessMemory(140, 140, 100)
    grown = backend.collect_apps()
    baseline = update_baseline(grown, baseline)
    assert build_rows(grown, baseline)[0].delta_bytes == 40

    reader.add(11, 10, "helper", None, None)
    partial = backend.collect_apps()
    baseline = update_baseline(partial, baseline)
    assert build_rows(partial, baseline)[0].delta_bytes is None

    reader.identities.pop(11)
    reader.paths.pop(10)
    reader.identities.pop(10)
    baseline = update_baseline(backend.collect_apps(), baseline)
    assert baseline == {}
    reader.add(20, 1, "App", "/Applications/App.app/Contents/MacOS/App", 200)
    reopened = backend.collect_apps()
    baseline = update_baseline(reopened, baseline)
    assert build_rows(reopened, baseline)[0].delta_bytes == 0


def test_growth_starts_when_initial_partial_coverage_recovers() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", None, None)
    backend = DarwinBackend(501, reader)
    partial = backend.collect_apps()
    baseline = update_baseline(partial, {})
    assert build_rows(partial, baseline)[0].delta_bytes is None

    reader.identities.pop(11)
    recovered = backend.collect_apps()
    baseline = update_baseline(recovered, baseline)
    assert build_rows(recovered, baseline)[0].delta_bytes == 0

    reader.memories[10] = ProcessMemory(120, 120, 100)
    grown = backend.collect_apps()
    baseline = update_baseline(grown, baseline)
    assert build_rows(grown, baseline)[0].delta_bytes == 20


@pytest.mark.asyncio
async def test_darwin_live_view_uses_footprint_columns_and_drill_down() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", "/usr/bin/helper", 20)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(
        interval=60,
        main_screen_factory=lambda: DarwinMainScreen(backend, 60),
    )
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, DarwinMainScreen)
        table = pilot.app.screen.query_one("#table", RowTable)
        assert list(table.column_keys) == [
            "app",
            "footprint",
            "compressed",
            "delta",
            "procs",
        ]
        assert "RAM" in str(pilot.app.screen.query_one("#header1", Static).content)
        table.focus()
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        detail_table = pilot.app.screen.query_one("#table", RowTable)
        assert detail_table.row_count == 2
        await pilot.press("g")
        assert detail_table.row_count == 2


@pytest.mark.asyncio
async def test_darwin_detail_renders_untrusted_markup_as_literal_text() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/[bold]App[-].app/Contents/MacOS/App", 100)
    reader.add(11, 10, "worker", "/tmp/[red]worker", 20)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        screen = pilot.app.screen
        assert isinstance(screen, DarwinProcessesScreen)
        title = cast("Content", screen.query_one("#title", Static).visual)
        assert title.plain.startswith("[bold]App[-]")
        assert title.spans == []

        table = screen.query_one("#table", RowTable)
        table.move_cursor(row=1)
        await pilot.pause()
        status = cast("Content", screen.query_one("#status", Static).visual)
        assert status.plain == "via ancestry  /tmp/[red]worker"
        assert status.spans == []


@pytest.mark.asyncio
async def test_darwin_process_view_preserves_selection_and_scroll_across_ticks() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    for pid in range(20, 55):
        reader.add(pid, 10, "worker", "/usr/bin/worker", pid)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 16)) as pilot:
        await pilot.pause()
        main_table = pilot.app.screen.query_one("#table", RowTable)
        main_table.focus()
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        screen = pilot.app.screen
        table = screen.query_one("#table", RowTable)
        assert table.row_count == 36
        table.move_cursor(row=20)
        await pilot.pause()
        selected = table.cursor_key
        assert table.scroll_y > 0
        scrolled_y = table.scroll_y
        reader.memories[35] = ProcessMemory(900, 900, 350)
        reader.add(55, 10, "worker", "/usr/bin/worker", 55)
        screen.refresh_now()
        await pilot.pause()
        await pilot.pause()
        selected_after = table.cursor_key
        assert selected_after == selected
        assert table.row_count == 37
        assert table.scroll_y == scrolled_y
        await pilot.press("g")
        assert table.row_count == 2
        table.move_cursor(row=0)
        await pilot.press("enter")
        assert table.row_count in (1, 36)
        await pilot.press("escape")
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_darwin_process_view_narrow_columns_and_gone_command() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "worker", "/usr/bin/worker", None)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(55, 18)) as pilot:
        await pilot.pause()
        main_screen = pilot.app.screen
        assert isinstance(main_screen, DarwinMainScreen)
        main_columns = main_screen.query_one("#table", RowTable).column_keys
        assert list(main_columns) == [
            "app",
            "footprint",
            "procs",
        ]
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        screen = pilot.app.screen
        table = screen.query_one("#table", RowTable)
        assert list(table.column_keys) == [
            "pid",
            "name",
            "footprint",
        ]
        assert table.column_region("footprint").right <= table.scrollable_content_region.width
        await pilot.press("g")
        assert list(table.column_keys) == [
            "name",
            "footprint",
            "count",
        ]
        assert table.column_region("count").right <= table.scrollable_content_region.width
        table.move_cursor(row=1)
        await pilot.press("enter")
        assert table.row_count == 1
        reader.identities.pop(11)
        screen.refresh_now()
        await pilot.pause()
        assert table.row_count == 1
        assert list(table.column_keys) == [
            "name",
            "footprint",
            "count",
        ]


@pytest.mark.asyncio
async def test_darwin_rowtable_navigation_and_resize() -> None:
    reader = Reader()
    for pid in range(10, 50):
        reader.add(
            pid,
            1,
            f"App{pid}",
            f"/Applications/App{pid}.app/Contents/MacOS/App{pid}",
            pid * 100,
        )
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 16)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, DarwinMainScreen)
        table = screen.query_one("#table", RowTable)
        assert table.row_count == 40
        table.focus()
        await pilot.press("end")
        assert table.cursor_row == 39
        await pilot.press("home")
        assert table.cursor_row == 0
        await pilot.press("pagedown")
        assert table.cursor_row > 0
        await pilot.press("pageup")
        assert table.cursor_row == 0
        table.scroll_to(y=3, animate=False)
        await pilot.pause()
        assert table.scroll_y == 3
        await pilot.resize_terminal(55, 16)
        assert table.column_keys == ("app", "footprint", "procs")
        assert table.column_region("procs").right <= table.scrollable_content_region.width
        assert table.scroll_y == 0
        await pilot.resize_terminal(90, 16)
        assert "delta" in table.column_keys
        assert table.scroll_y == 0


def _cursor_visible(table: RowTable) -> bool:
    return (
        table.scroll_y
        <= table.cursor_row
        < (table.scroll_y + table.scrollable_content_region.height)
    )


@pytest.mark.asyncio
async def test_darwin_main_resize_and_drill_out_reveal_selection() -> None:
    reader = Reader()
    for pid in range(10, 50):
        reader.add(pid, 1, f"App{pid}", f"/Applications/App{pid}.app/Contents/MacOS/App{pid}", pid)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, DarwinMainScreen)
        table = screen.query_one("#table", RowTable)
        table.focus()
        table.move_cursor(row=15, scroll=True)
        await pilot.resize_terminal(90, 10)
        await pilot.pause()
        await pilot.pause()
        assert _cursor_visible(table)

        selected = table.cursor_key
        table.scroll_to(y=0, animate=False)
        await pilot.pause()
        screen.refresh_now()
        await pilot.pause()
        await pilot.pause()
        assert table.scroll_y == 0
        await pilot.press("?")
        await pilot.press("escape")
        await pilot.pause()
        assert table.scroll_y == 0
        await pilot.press("f")
        assert _cursor_visible(table)
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        table.scroll_to(y=0, animate=False)
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is screen
        assert table.cursor_key == selected
        assert _cursor_visible(table)


@pytest.mark.asyncio
async def test_darwin_detail_group_back_and_overlay_scroll() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    for pid in range(20, 55):
        reader.add(pid, 10, f"worker{pid}", f"/usr/bin/worker{pid}", pid)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        screen = pilot.app.screen
        assert isinstance(screen, DarwinProcessesScreen)
        table = screen.query_one("#table", RowTable)
        table.move_cursor(row=20, scroll=True)
        selected = table.cursor_key
        await pilot.resize_terminal(90, 10)
        assert table.cursor_key == selected
        assert _cursor_visible(table)

        table.scroll_to(y=2, animate=False)
        await pilot.pause()
        scrolled_y = table.scroll_y
        await pilot.press("?")
        assert isinstance(pilot.app.screen, DarwinHelpScreen)
        await pilot.press("q")
        await pilot.pause()
        assert pilot.app.screen is screen
        assert table.scroll_y == scrolled_y

        await pilot.press("g")
        table.move_cursor(row=20, scroll=True)
        command = table.cursor_key
        await pilot.press("enter")
        assert table.row_count == 1
        await pilot.press("escape")
        assert table.row_count > 20
        assert table.cursor_key == command
        assert _cursor_visible(table)


@pytest.mark.asyncio
async def test_darwin_main_read_failure_is_visible_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    backend = DarwinBackend(501, reader)
    failed = True
    original_host = reader.host

    def host() -> ReadResult[HostMemory]:
        return ReadResult(None, Unavailable.ERROR) if failed else original_host()

    monkeypatch.setattr(reader, "host", host)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, DarwinMainScreen)
        table = screen.query_one("#table", RowTable)
        header = screen.query_one("#header1", Static)
        assert table.row_count == 0
        assert "Read unavailable; retrying" in str(header.content)

        failed = False
        screen.refresh_now()
        assert table.row_count == 1
        selected = table.cursor_key
        assert "RAM" in str(header.content)

        failed = True
        screen._read_tick(screen._generation)  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert table.cursor_key == selected
        assert "stale values" in str(header.content)
        failed = False
        screen._read_tick(screen._generation)  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert "RAM" in str(header.content)


@pytest.mark.asyncio
async def test_darwin_detail_read_failure_is_visible_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "worker", "/usr/bin/worker", 20)
    backend = DarwinBackend(501, reader)
    failed = False
    original_pids = reader.pids

    def pids() -> ReadResult[list[int]]:
        return ReadResult(None, Unavailable.ERROR) if failed else original_pids()

    monkeypatch.setattr(reader, "pids", pids)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        failed = True
        await pilot.press("enter")
        screen = pilot.app.screen
        assert isinstance(screen, DarwinProcessesScreen)
        table = screen.query_one("#table", RowTable)
        status = screen.query_one("#status", Static)
        assert table.row_count == 0
        assert "Read unavailable; retrying" in str(status.content)

        failed = False
        screen.refresh_now()
        assert table.row_count == 2
        selected = table.cursor_key
        assert "Read unavailable" not in str(status.content)

        failed = True
        screen._read_tick(screen._generation)  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert table.cursor_key == selected
        assert "stale values" in str(status.content)
        failed = False
        screen._read_tick(screen._generation)  # pyright: ignore[reportPrivateUsage]
        await pilot.pause()
        assert "Read unavailable" not in str(status.content)


@pytest.mark.parametrize(
    ("screen_type", "tick_type"),
    [(DarwinMainScreen, DarwinMainTick), (DarwinProcessesScreen, DarwinDetailTick)],
)
@pytest.mark.parametrize("error_type", [ValueError, SystemExit])
def test_darwin_worker_posts_unexpected_error_and_clears_inflight(
    screen_type: type[DarwinMainScreen] | type[DarwinProcessesScreen],
    tick_type: type[DarwinMainTick] | type[DarwinDetailTick],
    error_type: type[ValueError] | type[SystemExit],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = DarwinBackend(501, Reader())
    screen = (
        DarwinMainScreen(backend, 60)
        if screen_type is DarwinMainScreen
        else DarwinProcessesScreen(backend, "App", 60)
    )
    events: list[DarwinMainTick | DarwinDetailTick] = []
    monkeypatch.setattr(screen, "post_message", events.append)

    def programming_error(*_args: object) -> None:
        raise error_type("programming error")

    monkeypatch.setattr(
        backend,
        "read_system" if screen_type is DarwinMainScreen else "find_app",
        programming_error,
    )
    screen._tick_in_flight = True  # pyright: ignore[reportPrivateUsage]
    screen._read_tick(0)  # pyright: ignore[reportPrivateUsage]
    assert len(events) == 1
    assert isinstance(events[0], tick_type)
    with pytest.raises(error_type, match="programming error"):
        if isinstance(screen, DarwinMainScreen):
            screen.on_darwin_main_tick(cast("DarwinMainTick", events[0]))
        else:
            screen.on_darwin_detail_tick(cast("DarwinDetailTick", events[0]))
    assert not screen._tick_in_flight  # pyright: ignore[reportPrivateUsage]


def test_resident_preserves_unknown_and_independent_aggregate_coverage() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "worker", "/usr/bin/worker", 20)
    reader.add(12, 10, "worker", "/usr/bin/worker", None)
    reader.memories[10] = ProcessMemory(100, 800, 100)
    reader.memories[11] = ProcessMemory(20, 10, 110)
    backend = DarwinBackend(501, reader)
    app = backend.collect_apps()[0]
    assert app.resident_bytes == 810 and app.footprint_bytes == 120
    assert app.resident_partial and app.resident_readable_processes == 2
    assert app.members[2].resident_bytes is None
    # A fixture can have distinct metric coverage: never reuse footprint totals for resident.
    isolated = replace(app, members=(replace(app.members[0], resident_bytes=None),))
    assert isolated.resident_bytes is None
    assert isolated.resident_readable_processes == 0
    raw = darwin_report.app_document(backend, app.id, limit=10, now=datetime.now(UTC))
    doc = cast("dict[str, Any]", raw[0])
    assert doc["resident_bytes"] == 810
    assert doc["coverage"]["resident_unreadable_processes"] == 1
    worker = next(item for item in doc["commands"]["items"] if item["name"] == "worker")
    assert worker["resident_bytes"] == 10
    assert worker["resident_readable_processes"] == 1
    assert worker["resident_unreadable_processes"] == 1


@pytest.mark.asyncio
async def test_resident_main_detail_group_member_sort_and_resize() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "worker", "/usr/bin/worker", 20)
    reader.add(12, 10, "worker", "/usr/bin/worker", None)
    reader.add(20, 1, "Other", "/Applications/Other.app/Contents/MacOS/Other", 200)
    reader.memories[10] = ProcessMemory(100, 800, 100)
    reader.memories[11] = ProcessMemory(20, 10, 110)
    reader.memories[20] = ProcessMemory(200, 30, 200)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, DarwinMainScreen)
        table = main.query_one("#table", RowTable)
        assert "resident" in table.column_keys
        app_id = next(item.id for item in backend.collect_apps() if item.name == "App")
        table.move_cursor(row=table.get_row_index(app_id))
        await pilot.press("r")
        assert table.get_row_index(app_id) == 0
        assert table.cursor_key == app_id
        assert table.get_cell(app_id, "resident").plain.endswith("*")
        await pilot.press("enter")
        detail = app.screen
        assert isinstance(detail, DarwinProcessesScreen)
        table = detail.query_one("#table", RowTable)
        assert "resident" in table.column_keys
        await pilot.press("r")
        assert table.get_cell(table.row_keys[0], "pid").plain == "10"
        assert table.get_cell(table.row_keys[2], "resident").plain == "?"
        await pilot.press("g")
        assert "resident" in table.column_keys
        worker_index = next(
            i
            for i in range(table.row_count)
            if table.get_cell(table.row_keys[i], "name").plain == "worker"
        )
        table.move_cursor(row=worker_index)
        assert table.get_cell(table.row_keys[worker_index], "resident").plain.endswith("*")
        await pilot.press("enter")
        assert table.row_count == 2 and "resident" in table.column_keys
        selected = table.cursor_key
        detail.refresh_now()
        assert table.cursor_key == selected
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert "resident" not in table.column_keys
        assert detail._sort_key == "footprint"  # pyright: ignore[reportPrivateUsage]
        await pilot.press("escape", "escape", "escape")
        assert isinstance(app.screen, DarwinMainScreen)
        assert main._sort_key == "footprint"  # pyright: ignore[reportPrivateUsage]
        assert "resident" not in main.query_one("#table", RowTable).column_keys


@pytest.mark.asyncio
async def test_baseline_reset_updates_header_time_and_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from appmem.ui.screens import darwin as screens

    clock = [100.0]
    monkeypatch.setattr(screens, "monotonic", lambda: clock[0])

    class Clock:
        @staticmethod
        def now() -> datetime:
            return datetime(2026, 10, 1, 12, int(clock[0]) // 60, int(clock[0]) % 60)

    monkeypatch.setattr(screens, "datetime", Clock)
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, DarwinMainScreen)
        before_time = main._baseline_time  # pyright: ignore[reportPrivateUsage]
        clock[0] = 172.0
        main.refresh_now()
        header = main.query_one("#header4", Static)
        assert "(1m)" in str(header.content)
        reader.memories[10] = ProcessMemory(150, 150, 100)
        await pilot.press("b")
        assert "(0s)" in str(header.content)
        assert main._baseline_started == 172.0  # pyright: ignore[reportPrivateUsage]
        assert all(row.delta_bytes == 0 for row in main._rows.values())  # pyright: ignore[reportPrivateUsage]
        baseline_time = main._baseline_time  # pyright: ignore[reportPrivateUsage]
        assert baseline_time is not None and baseline_time in str(header.content)
        assert baseline_time != before_time


@pytest.mark.asyncio
async def test_darwin_help_scroll_reaches_keys_and_returns_at_80x24() -> None:
    from textual.containers import VerticalScroll

    reader = Reader()
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        main = app.screen
        await pilot.press("?")
        screen = app.screen
        assert isinstance(screen, DarwinHelpScreen)
        scroll = screen.query_one("#darwin-help-scroll", VerticalScroll)
        assert scroll.has_focus and scroll.max_scroll_y > 0
        assert screen.query_one("#darwin-help-title", Static).region.y == 0
        footer = screen.query_one("#footer", Static)
        assert footer.region.bottom <= 24
        await pilot.press("end")
        await pilot.pause()
        assert scroll.scroll_y == scroll.max_scroll_y
        body = screen.query_one("#darwin-help-text", Static)
        assert "q / Ctrl+C" in str(body.content)
        assert body.region.bottom <= scroll.region.bottom
        await pilot.press("home")
        await pilot.pause()
        assert scroll.scroll_y == 0
        await pilot.press("escape")
        assert app.screen is main


def test_darwin_help_reads_in_screen_order_and_names_the_setuid_limit() -> None:
    body = DarwinHelpScreen._body(100)  # pyright: ignore[reportPrivateUsage]
    order = [
        "\nMEMORY ",
        "\nCOMPRESSED ",
        "\nΔMEM ",
        "\nRESIDENT ",
        "\nPROCS ",
        "\n* / ? ",
        "\nRAM ",
        "\nfile-backed ",
        "\nCompress ",
        "\nSwap in/out ",
        "\nSwap ",
        "\nPressure ",
        "h on the main dashboard",
        "\nKeys:\n",
    ]
    positions = [("\n" + body).index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "setuid process you started" in body


def test_darwin_text_reports_use_aligned_units_and_keep_documents() -> None:
    reader = Reader()
    reader.add(10, 1, "Long App", "/Applications/Long App.app/Contents/MacOS/App", 1024**3)
    reader.add(11, 10, "helper", None, None)
    reader.add(20, 1, "Small", "/Applications/Small.app/Contents/MacOS/Small", 1024**2)
    backend = DarwinBackend(501, reader)
    document, _ = darwin_report.snapshot_document(backend, limit=10, now=datetime.now(UTC))
    original = json.dumps(document)
    text = darwin_report.render_snapshot_text(document)
    assert json.dumps(document) == original
    assert all(label in text for label in ("RAM       ", "Compress  ", "Swap      ", "Pressure  "))
    assert "1.0 GiB" in text and "1 MiB" in text and "partial" in text
    assert "Δ" not in text and "baseline" not in text and "bytes" not in text
    rows = [line for line in text.splitlines() if line.startswith(("Long App", "Small"))]
    assert rows[0].index("GiB") + 1 == rows[1].index("MiB")
    result = darwin_report.app_document(backend, "Long App", limit=10, now=datetime.now(UTC))
    detail = darwin_report.render_app_text(result[0])
    assert "1.0 GiB" in detail and "unknown" in detail and "partial" in detail
