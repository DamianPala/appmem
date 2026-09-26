"""Darwin collection contracts with a synthetic native reader only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from rich.text import Text
from textual.content import Content
from textual.widgets import DataTable, Static

from appmem import cli as cli_module
from appmem import darwin_report, darwin_schema
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
from appmem.ui.screens.darwin import DarwinMainScreen, DarwinProcessesScreen


class Reader:
    def __init__(self) -> None:
        self.identities: dict[int, ProcessIdentity] = {}
        self.paths: dict[int, str] = {}
        self.memories: dict[int, ProcessMemory | None] = {}
        self.memory_calls: dict[int, int] = {}
        self.process_calls: dict[int, int] = {}
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
    ) -> None:
        self.identities[pid] = ProcessIdentity(pid, ppid, 501, 1, command, start)
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
            return ReadResult(
                ProcessIdentity(
                    pid, value.ppid, value.uid, value.status, "New", value.start_abstime
                )
            )
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


def test_same_name_bundles_have_different_stable_identity() -> None:
    reader = Reader()
    reader.add(10, 1, "Editor", "/Applications/Editor.app/Contents/MacOS/Editor", 100)
    reader.add(20, 1, "Editor", "/Users/me/Editor.app/Contents/MacOS/Editor", 200)
    backend = DarwinBackend(501, reader)
    apps = backend.collect_apps()
    assert len({app.id for app in apps}) == 2
    assert len({app.name for app in apps}) == 2
    assert all(app.name.startswith("Editor [") for app in apps)
    assert backend.find_app(apps[0].id) is not None


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


def test_independent_bundleless_roots_and_partial_coverage() -> None:
    reader = Reader()
    reader.add(10, 1, "daemon", "/usr/bin/daemon", 100)
    reader.add(11, 10, "child", None, None)
    reader.add(20, 1, "daemon", "/usr/bin/daemon", None)
    apps = DarwinBackend(501, reader).collect_apps()
    assert len(apps) == 2
    assert len({app.id for app in apps}) == 2
    first = next(app for app in apps if any(p.pid == 10 for p in app.members))
    denied = next(app for app in apps if any(p.pid == 20 for p in app.members))
    assert first.footprint_bytes == 100
    assert first.readable_processes == first.unreadable_processes == 1
    assert denied.footprint_bytes is None
    assert denied.unreadable_processes == 1


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
    item = snapshot["apps"][0]
    assert item["footprint_bytes"] == 100
    assert item["coverage"]["partial"] is True
    assert snapshot["system"]["swap_used_bytes"] == 0
    assert "ram_bytes" not in item and "swap_bytes" not in item
    detail = darwin_report.app_document(backend, item["id"], limit=10, now=now)
    assert detail is not None
    raw_document, _, _ = detail
    document = cast("dict[str, Any]", raw_document)
    assert document["commands"][1]["footprint_bytes"] is None
    assert document["processes"][1]["command"] == "secret?name?more"
    schema_detail = darwin_schema.detail(["app"])
    assert schema_detail is not None
    output = cast("dict[str, Any]", schema_detail["output"])
    assert output["properties"]["app"]["properties"]["footprint_bytes"]["type"] == [
        "integer",
        "null",
    ]


def test_darwin_schema_describes_and_validates_emitted_documents() -> None:
    reader = Reader()
    reader.add(10, 1, "App", "/Applications/App.app/Contents/MacOS/App", 100)
    reader.add(11, 10, "helper", None, None)
    backend = DarwinBackend(501, reader)
    now = datetime(2026, 9, 26, tzinfo=UTC)
    snapshot, _ = darwin_report.snapshot_document(backend, limit=1, now=now)
    detail = darwin_report.app_document(
        backend, cast("list[dict[str, Any]]", snapshot["apps"])[0]["id"], limit=1, now=now
    )
    assert detail is not None
    app_document, _, _ = detail
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

    monkeypatch.setattr(cli_module, "DarwinBackend", backend_for_uid)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    assert cli_module.main(["snapshot", "--json"], uid=501) == 0
    import json

    snapshot = json.loads(capsys.readouterr().out)
    app_id = snapshot["apps"][0]["id"]
    assert cli_module.main(["app", app_id, "--json"], uid=501) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail["app"]["id"] == app_id
    assert detail["commands"][0]["footprint_bytes"] == 100


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
        table = cast("DataTable[str | Text]", pilot.app.screen.query_one("#table", DataTable))
        assert [column.key.value for column in table.ordered_columns] == [
            "app",
            "footprint",
            "delta",
            "procs",
        ]
        assert "Physical" in str(pilot.app.screen.query_one("#header1", Static).content)
        table.focus()
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        detail_table = cast(
            "DataTable[str | Text]", pilot.app.screen.query_one("#table", DataTable)
        )
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
        pilot.app.screen.query_one("#table", DataTable).focus()
        await pilot.press("enter")
        screen = pilot.app.screen
        assert isinstance(screen, DarwinProcessesScreen)
        title = cast("Content", screen.query_one("#title", Static).visual)
        assert title.plain.startswith("[bold]App[-]")
        assert title.spans == []

        table = cast("DataTable[str | Text]", screen.query_one("#table", DataTable))
        table.move_cursor(row=1)
        await pilot.pause()
        status = cast("Content", screen.query_one("#status", Static).visual)
        assert status.plain == "/tmp/[red]worker"
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
        main_table = cast("DataTable[str | Text]", pilot.app.screen.query_one("#table", DataTable))
        main_table.focus()
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        screen = pilot.app.screen
        table = cast("DataTable[str | Text]", screen.query_one("#table", DataTable))
        assert table.row_count == 36
        table.move_cursor(row=20)
        await pilot.pause()
        selected, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert table.scroll_y > 0
        reader.memories[35] = ProcessMemory(900, 900, 350)
        reader.add(55, 10, "worker", "/usr/bin/worker", 55)
        screen.refresh_now()
        await pilot.pause()
        selected_after, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        assert selected_after == selected
        assert table.row_count == 37
        assert table.scroll_y > 0
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
        main_columns = main_screen.query_one("#table", DataTable).ordered_columns
        assert [column.key.value for column in main_columns] == [
            "app",
            "footprint",
            "procs",
        ]
        pilot.app.screen.query_one("#table", DataTable).focus()
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, DarwinProcessesScreen)
        screen = pilot.app.screen
        table = cast("DataTable[str | Text]", screen.query_one("#table", DataTable))
        assert [column.key.value for column in table.ordered_columns] == [
            "pid",
            "name",
            "footprint",
        ]
        await pilot.press("g")
        assert [column.key.value for column in table.ordered_columns] == [
            "name",
            "footprint",
            "count",
        ]
        table.move_cursor(row=1)
        await pilot.press("enter")
        assert table.row_count == 1
        reader.identities.pop(11)
        screen.refresh_now()
        await pilot.pause()
        assert table.row_count == 1
        assert [column.key.value for column in table.ordered_columns] == [
            "name",
            "footprint",
            "count",
        ]
