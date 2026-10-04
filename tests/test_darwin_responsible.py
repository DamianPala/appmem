"""macOS: launchd-parented helpers join the app macOS holds responsible for them."""

from __future__ import annotations

import ctypes
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import Static

from appmem import darwin_report, darwin_schema, schema
from appmem.darwin_backend import VIA_RULES, DarwinApp, DarwinBackend
from appmem.darwin_native import DarwinNative, Unavailable
from appmem.ui.app import AppMemApp
from appmem.ui.screens.darwin import DarwinHelpScreen, DarwinMainScreen, DarwinProcessesScreen
from appmem.ui.table import RowTable
from test_darwin_backend import Reader

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
SAFARI = "/Applications/Safari.app/Contents/MacOS/Safari"
WEBKIT = "/System/Library/Frameworks/WebKit.framework/XPCServices"
WEB_CONTENT = f"{WEBKIT}/WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent"


def _safari(reader: Reader) -> None:
    reader.add(10, 1, "Safari", SAFARI, 100, start=100)


def _by_name(apps: list[DarwinApp]) -> dict[str, DarwinApp]:
    return {app.name: app for app in apps}


def _vias(app: DarwinApp) -> dict[int, str]:
    return {process.pid: process.via for process in app.members}


def test_a_launchd_helper_joins_the_app_responsible_for_it() -> None:
    reader = Reader()
    _safari(reader)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.add(21, 1, "WebContent", WEB_CONTENT, 50, start=210)
    reader.add(22, 21, "child", "/usr/bin/child", 5, start=220)
    reader.responsibles = {20: 10, 21: 10}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari"}
    safari = apps["Safari"]
    assert safari.footprint_bytes == 355
    assert _vias(safari) == {10: "bundle", 20: "responsible", 21: "responsible", 22: "responsible"}
    assert not safari.grouping_partial
    assert sorted(reader.responsible_calls) == [20, 21]  # once per top of chain, not per member


def test_a_helper_responsible_to_a_bundleless_process_stays_a_root() -> None:
    reader = Reader()
    reader.add(10, 1, "daemon", "/usr/libexec/daemon", 100, start=100)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.responsibles = {20: 10}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"daemon", "com.apple.WebKit.WebContent"}
    assert _vias(apps["com.apple.WebKit.WebContent"]) == {20: "root"}


def test_a_responsible_process_resolves_through_its_own_ancestry() -> None:
    reader = Reader()
    _safari(reader)
    reader.add(11, 10, "tool", "/usr/bin/tool", 10, start=110)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.responsibles = {20: 11}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari"}
    assert _vias(apps["Safari"]) == {10: "bundle", 11: "ancestry", 20: "responsible"}


def test_a_self_responsible_helper_stays_a_root() -> None:
    reader = Reader()
    _safari(reader)
    history = f"{WEBKIT}/History.xpc/Contents/MacOS/com.apple.Safari.History"
    reader.add(20, 1, "History", history, 30, start=200)
    reader.responsibles = {20: 20}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari", "com.apple.Safari.History"}
    assert _vias(apps["com.apple.Safari.History"]) == {20: "root"}


def test_a_responsible_pid_outside_the_sample_is_ignored() -> None:
    reader = Reader()
    _safari(reader)
    reader.add(30, 1, "Other", "/Applications/Other.app/Contents/MacOS/Other", 10, uid=0)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.add(21, 1, "WebContent", WEB_CONTENT, 50, start=210)
    reader.responsibles = {20: 999, 21: 30}  # gone, and owned by another user
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari", "com.apple.WebKit.WebContent"}
    assert _vias(apps["com.apple.WebKit.WebContent"]) == {20: "root", 21: "root"}


def test_a_responsible_pid_that_started_later_is_a_reused_pid() -> None:
    reader = Reader()
    reader.add(10, 1, "Safari", SAFARI, 100, start=500)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.responsibles = {20: 10}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari", "com.apple.WebKit.WebContent"}


def test_a_start_time_that_cannot_be_compared_is_not_trusted() -> None:
    reader = Reader()
    _safari(reader)
    reader.add(20, 1, "WebContent", WEB_CONTENT, None, start=200)  # footprint unreadable: no start
    reader.responsibles = {20: 10}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari", "com.apple.WebKit.WebContent"}


def test_a_missing_responsibility_call_leaves_grouping_as_it_was() -> None:
    reader = Reader()
    _safari(reader)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.responsibles = {20: 10}
    reader.responsible_missing = True
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert set(apps) == {"Safari", "com.apple.WebKit.WebContent"}
    assert apps["Safari"].footprint_bytes == 100
    assert all(not app.grouping_partial for app in apps.values())


def test_an_app_child_keeps_ancestry_and_responsibility_is_not_consulted() -> None:
    reader = Reader()
    reader.add(5, 1, "Terminal", "/Applications/Terminal.app/Contents/MacOS/Terminal", 40, start=50)
    reader.add(10, 5, "Editor", "/Applications/Editor.app/Contents/MacOS/Editor", 100, start=100)
    reader.add(11, 10, "server", "/usr/local/bin/server", 20, start=110)
    reader.responsibles = {10: 5, 11: 5}  # TCC says the terminal; the tree says the editor
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert _vias(apps["Editor"]) == {10: "bundle", 11: "ancestry"}
    assert _vias(apps["Terminal"]) == {5: "bundle"}
    assert reader.responsible_calls == []


def test_a_chain_ending_at_another_users_parent_is_not_asked() -> None:
    reader = Reader()
    _safari(reader)
    reader.add(5, 1, "sshd-session", "/usr/libexec/sshd-session", 10, uid=0)
    reader.add(20, 5, "zsh", "/bin/zsh", 3, start=200)
    reader.responsibles = {20: 10}
    apps = _by_name(DarwinBackend(501, reader).collect_apps())
    assert _vias(apps["zsh"]) == {20: "root"}
    assert reader.responsible_calls == []


def test_bundleless_roots_are_looked_up_once_each_and_stay_roots_without_an_answer() -> None:
    reader = Reader()
    reader.add(10, 1, "daemon", "/usr/libexec/daemon", 100, start=100)
    reader.add(11, 10, "worker", "/usr/libexec/worker", 5, start=110)
    reader.add(20, 1, "daemon", "/usr/libexec/daemon", 100, start=120)
    apps = DarwinBackend(501, reader).collect_apps()
    assert [_vias(app) for app in apps] == [{10: "root", 11: "root", 20: "root"}]
    assert sorted(reader.responsible_calls) == [10, 20]  # roots only, once each


# --- native call -----------------------------------------------------------------


def _native(lib: object) -> DarwinNative:
    native = object.__new__(DarwinNative)
    native._lib = lib  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    return native


def test_the_native_call_reports_a_missing_symbol_without_raising() -> None:
    native = _native(SimpleNamespace())  # attribute lookup on a real CDLL raises the same way
    for _ in range(2):
        result = native.responsible(10)
        assert result.value is None and result.unavailable == Unavailable.UNSUPPORTED


def test_the_native_call_binds_once_and_maps_failure_to_unavailable() -> None:
    answers = {10: 5, 11: -1, 12: 0}
    calls: list[int] = []

    def function(pid: int) -> int:
        calls.append(pid)
        return answers[pid]

    native = _native(SimpleNamespace(responsibility_get_pid_responsible_for_pid=function))
    assert native.responsible(10).value == 5
    assert native.responsible(11).unavailable == Unavailable.ERROR
    assert native.responsible(12).unavailable == Unavailable.ERROR
    assert calls == [10, 11, 12]
    assert function.argtypes == [ctypes.c_int]  # pyright: ignore[reportFunctionMemberAccess]
    assert function.restype is ctypes.c_int  # pyright: ignore[reportFunctionMemberAccess]


# --- documents -------------------------------------------------------------------


def _app_document(reader: Reader) -> dict[str, Any]:
    document, _, _ = darwin_report.app_document(
        DarwinBackend(501, reader), "Safari", limit=10, now=NOW
    )
    return document


def _reader_with_helper() -> Reader:
    reader = Reader()
    _safari(reader)
    reader.add(20, 1, "WebContent", WEB_CONTENT, 200, start=200)
    reader.responsibles = {20: 10}
    return reader


def test_the_app_document_and_its_schema_carry_via_with_a_description() -> None:
    document = _app_document(_reader_with_helper())
    processes = document["processes"]["items"]
    assert {p["pid"]: p["via"] for p in processes} == {10: "bundle", 20: "responsible"}
    detail = cast("dict[str, Any]", darwin_schema.detail(["app"]))
    field: dict[str, Any] = detail["output"]["properties"]["processes"]["properties"]["items"][
        "items"
    ]
    assert "via" in field["required"]
    assert field["properties"]["via"]["enum"] == list(VIA_RULES)
    assert field["properties"]["via"]["description"]
    assert set(processes[0]) == set(field["properties"])


def test_the_text_report_lists_via_per_process() -> None:
    lines = darwin_report.render_app_text(_app_document(_reader_with_helper())).splitlines()
    assert lines[1].split()[-1] == "VIA"
    assert [line.split()[-1] for line in lines[2:4]] == ["responsible", "bundle"]


def test_linux_documents_and_schemas_have_no_via() -> None:
    text = json.dumps([schema.index(), *(schema.detail([name]) for name in ("snapshot", "app"))])
    assert '"via"' not in text


# --- live view -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_process_view_status_line_names_the_rule_and_help_explains_it() -> None:
    reader = _reader_with_helper()
    backend = DarwinBackend(501, reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        detail = pilot.app.screen
        assert isinstance(detail, DarwinProcessesScreen)
        table = detail.query_one("#table", RowTable)
        status = detail.query_one("#status", Static)
        seen: dict[str, str] = {}
        for index in range(table.row_count):
            table.move_cursor(row=index)
            await pilot.pause()
            seen[str(table.get_cell(table.row_keys[index], "name"))] = str(status.content)
        assert seen["Safari"].startswith("via bundle  /Applications/Safari.app")
        assert seen["com.apple.WebKit.WebContent"].startswith("via responsible  /System/")
    body = DarwinHelpScreen._body(200)  # pyright: ignore[reportPrivateUsage]
    assert "responsible" in body and "status line" in body


def test_every_rule_places_some_process() -> None:
    reader = _reader_with_helper()
    reader.add(30, 1, "daemon", "/usr/libexec/daemon", 5, start=300)
    reader.add(31, 10, "tool", "/usr/bin/tool", 5, start=310)
    seen = {p.via for app in DarwinBackend(501, reader).collect_apps() for p in app.members}
    assert seen == set(VIA_RULES)
