"""macOS: per-process and per-app compressed memory, from a fake native reader only."""

from __future__ import annotations

import ctypes
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.widgets import Static

from appmem import darwin_report, darwin_schema, schema
from appmem.darwin_backend import DarwinBackend
from appmem.darwin_native import DarwinNative, TaskVMInfoRev1, Unavailable
from appmem.ui.app import AppMemApp
from appmem.ui.screens.darwin import DarwinHelpScreen, DarwinMainScreen, DarwinProcessesScreen
from appmem.ui.table import RowTable
from test_darwin_backend import Reader

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
MIB = 1024**2


def _app(reader: Reader, name: str, pid: int, footprint: int, **kwargs: Any) -> None:
    reader.add(pid, 1, name, f"/Applications/{name}.app/Contents/MacOS/{name}", footprint, **kwargs)


def _backend(reader: Reader) -> DarwinBackend:
    return DarwinBackend(501, reader)


# --- native call -----------------------------------------------------------------


def test_the_task_vm_info_layout_matches_the_kernel_header() -> None:
    assert ctypes.sizeof(TaskVMInfoRev1) == 152  # TASK_VM_INFO_REV1_COUNT, 38 words
    assert TaskVMInfoRev1.compressed.offset == 120
    assert TaskVMInfoRev1.phys_footprint.offset == 144


class _Mach:
    """task_name_for_pid and task_info, with the answers and releases recorded."""

    def __init__(
        self, *, name_code: int = 0, info_code: int = 0, compressed: int = 0, words: int = 38
    ) -> None:
        self.name_code, self.info_code = name_code, info_code
        self.compressed, self.words = compressed, words
        self.released: list[tuple[int, int]] = []
        self.requested_words: list[int] = []

    def task_name_for_pid(self, _task: int, _pid: int, port: Any) -> int:
        if not self.name_code:
            ctypes.cast(port, ctypes.POINTER(ctypes.c_uint32))[0] = 77
        return self.name_code

    def task_info(self, port: int, flavor: int, info: Any, count: Any) -> int:
        assert (port, flavor) == (77, 22)
        words = ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))
        self.requested_words.append(words[0])
        words[0] = self.words
        ctypes.cast(info, ctypes.POINTER(TaskVMInfoRev1))[0].compressed = self.compressed
        return self.info_code

    def mach_port_deallocate(self, task: int, port: int) -> int:
        self.released.append((task, port))
        return 0


def _native(monkeypatch: pytest.MonkeyPatch, mach: _Mach) -> DarwinNative:
    reader = object.__new__(DarwinNative)
    monkeypatch.setattr(reader, "_lib", SimpleNamespace(**vars_of(mach)), raising=False)
    monkeypatch.setattr(reader, "_task_self_port", lambda: 42, raising=False)
    return reader


def vars_of(mach: _Mach) -> dict[str, object]:
    return {
        "task_name_for_pid": mach.task_name_for_pid,
        "task_info": mach.task_info,
        "mach_port_deallocate": mach.mach_port_deallocate,
    }


def test_compressed_decodes_the_field_and_releases_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mach = _Mach(compressed=5 * MIB)
    result = _native(monkeypatch, mach).compressed(10)
    assert result.value == 5 * MIB
    assert mach.released == [(42, 77)]
    assert mach.requested_words == [38]  # the revision macOS 12 and later accept


def test_a_zero_compressed_size_is_a_reading_not_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _native(monkeypatch, _Mach(compressed=0)).compressed(10)
    assert result.value == 0 and result.unavailable is None


@pytest.mark.parametrize(
    ("name_code", "info_code", "reason"),
    [
        (5, 0, Unavailable.DENIED),  # KERN_FAILURE: the name port is refused
        (0, 5, Unavailable.DENIED),
        (0, 4, Unavailable.VANISHED),  # KERN_INVALID_ARGUMENT: the task is gone
        (0, 49, Unavailable.VANISHED),  # KERN_TERMINATED
        (0, 99, Unavailable.ERROR),
    ],
)
def test_compressed_failures_are_unavailable_and_never_leak_a_port(
    monkeypatch: pytest.MonkeyPatch, name_code: int, info_code: int, reason: Unavailable
) -> None:
    mach = _Mach(name_code=name_code, info_code=info_code, compressed=1)
    result = _native(monkeypatch, mach).compressed(10)
    assert result.value is None and result.unavailable == reason
    assert mach.released == ([] if name_code else [(42, 77)])


def test_a_struct_too_short_to_hold_compressed_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mach = _Mach(compressed=9, words=TaskVMInfoRev1.compressed.offset // 4)
    result = _native(monkeypatch, mach).compressed(10)
    assert result.value is None and result.unavailable == Unavailable.ERROR
    assert mach.released == [(42, 77)]


# --- grouping and coverage -------------------------------------------------------


def test_an_app_sums_its_members_compressed_bytes() -> None:
    reader = Reader()
    _app(reader, "App", 10, 100, compressed=30)
    reader.add(11, 10, "helper", "/usr/bin/helper", 50, compressed=20)
    reader.add(12, 10, "idle", "/usr/bin/idle", 5, compressed=0)
    (app,) = _backend(reader).collect_apps()
    assert app.compressed_bytes == 50
    assert app.compressed_readable_processes == 3 and not app.compressed_partial
    assert app.footprint_bytes == 155  # compressed is inside the footprint, never added


def test_an_unreadable_member_makes_compressed_partial_but_not_the_footprint() -> None:
    reader = Reader()
    _app(reader, "App", 10, 100, compressed=30)
    reader.add(11, 10, "helper", "/usr/bin/helper", 50, compressed=None)
    (app,) = _backend(reader).collect_apps()
    assert app.compressed_bytes == 30 and app.compressed_partial
    assert app.compressed_readable_processes == 1
    assert not app.partial  # the footprint coverage is independent


def test_all_members_unreadable_is_unknown_not_zero() -> None:
    reader = Reader()
    _app(reader, "App", 10, 100, compressed=None)
    reader.add(11, 10, "helper", "/usr/bin/helper", 50, compressed=None)
    (app,) = _backend(reader).collect_apps()
    assert app.compressed_bytes is None and app.compressed_partial
    assert app.footprint_bytes == 150


def test_a_process_with_unreadable_footprint_can_still_have_compressed() -> None:
    reader = Reader()
    _app(reader, "App", 10, 100, compressed=30)
    reader.add(11, 10, "helper", "/usr/bin/helper", None, compressed=7)
    (app,) = _backend(reader).collect_apps()
    assert app.compressed_bytes == 37 and app.partial and not app.compressed_partial


# --- documents -------------------------------------------------------------------


def _documents(reader: Reader) -> tuple[dict[str, Any], dict[str, Any]]:
    backend = _backend(reader)
    snapshot, _ = darwin_report.snapshot_document(backend, limit=10, now=NOW)
    app_id = cast("dict[str, Any]", snapshot["apps"])["items"][0]["id"]
    app, _, _ = darwin_report.app_document(backend, app_id, limit=10, now=NOW)
    return cast("dict[str, Any]", snapshot), cast("dict[str, Any]", app)


def _two_helpers() -> Reader:
    reader = Reader()
    _app(reader, "App", 10, 100, compressed=30)
    reader.add(11, 10, "helper", "/usr/bin/helper", 50, compressed=20)
    reader.add(12, 10, "helper", "/usr/bin/helper", 40, compressed=None)
    return reader


def test_documents_carry_compressed_at_every_level() -> None:
    snapshot, app = _documents(_two_helpers())
    item = snapshot["apps"]["items"][0]
    assert item["compressed_bytes"] == 50
    coverage = item["coverage"]
    assert coverage["compressed_readable_processes"] == 2
    assert coverage["compressed_unreadable_processes"] == 1
    assert coverage["compressed_partial"] is True
    by_pid = {p["pid"]: p["compressed_bytes"] for p in app["processes"]["items"]}
    assert by_pid == {10: 30, 11: 20, 12: None}
    command = next(c for c in app["commands"]["items"] if c["name"] == "helper")
    assert command["compressed_bytes"] == 20
    assert command["compressed_readable_processes"] == 1
    assert command["compressed_unreadable_processes"] == 1


def test_the_schema_describes_and_validates_the_new_fields() -> None:
    snapshot, app = _documents(_two_helpers())
    for name, document in (("snapshot", snapshot), ("app", app)):
        detail = cast("dict[str, Any]", darwin_schema.detail([name]))
        _check(document, detail["output"], root=True)
    text = json.dumps(darwin_schema.detail(["app"]))
    assert text.count("compressed_bytes") >= 3
    assert "already part of footprint_bytes" in text


def _check(value: Any, field: dict[str, Any], *, root: bool = False) -> None:
    """Every emitted key is declared with a description, every required key is present."""
    if not root:
        assert field.get("description"), field
    if isinstance(value, dict):
        properties = field["properties"]
        assert set(field.get("required", [])) <= value.keys()
        assert value.keys() <= properties.keys()
        for key, item in cast("dict[str, Any]", value).items():
            _check(item, properties[key])
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            _check(item, field["items"])


def test_text_reports_show_a_compressed_column_and_mark_partial_sums() -> None:
    snapshot, app = _documents(_two_helpers())
    snapshot_lines = darwin_report.render_snapshot_text(snapshot).splitlines()
    header = next(line for line in snapshot_lines if line.startswith("APP"))
    assert header.split()[:3] == ["APP", "MEMORY", "COMPRESSED"]
    row = next(line for line in snapshot_lines if line.startswith("App"))
    assert row.split()[3:5] == ["50", "B*"]
    assert "* partial known sum" in "\n".join(snapshot_lines)
    detail = darwin_report.render_app_text(app).splitlines()
    assert detail[1].split() == ["PID", "COMMAND", "MEMORY", "COMPRESSED", "RESIDENT", "VIA"]
    unreadable = next(line for line in detail if line.split()[:1] == ["12"])
    assert "unknown" in unreadable


def test_linux_schemas_and_documents_have_no_compressed_field() -> None:
    text = json.dumps([schema.index(), *(schema.detail([name]) for name in ("snapshot", "app"))])
    assert "compressed_bytes" not in text and "compressed_partial" not in text


# --- live view -------------------------------------------------------------------


def _cells(table: RowTable, column: str) -> list[str]:
    return [str(table.get_cell(key, column)) for key in table.row_keys]


def _three_apps() -> Reader:
    reader = Reader()
    _app(reader, "Big", 10, 300, compressed=10 * MIB)
    _app(reader, "Mid", 20, 200, compressed=50 * MIB)
    _app(reader, "Small", 30, 100, compressed=None)
    return reader


@pytest.mark.asyncio
async def test_the_main_table_sorts_by_compressed_with_unknown_last() -> None:
    backend = _backend(_three_apps())
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, DarwinMainScreen)
        table = screen.query_one("#table", RowTable)
        assert _cells(table, "compressed") == ["10 MiB", "50 MiB", "?"]  # by MEMORY
        await pilot.press("c")
        assert _cells(table, "app") == ["Mid", "Big", "Small"]  # largest first, unknown last
        await pilot.press("c")
        assert _cells(table, "app") == ["Big", "Mid", "Small"]  # smallest first, unknown last
        assert table.get_column_label("compressed") == "COMPRESSED ▴"
        assert "c" in str(screen.query_one("#footer", Static).content)


@pytest.mark.asyncio
async def test_zero_shows_as_zero_and_a_partial_sum_is_starred() -> None:
    reader = Reader()
    _app(reader, "Idle", 10, 300, compressed=0)
    _app(reader, "Half", 20, 200, compressed=4 * MIB)
    reader.add(21, 20, "helper", "/usr/bin/helper", 5, compressed=None)
    backend = _backend(reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#table", RowTable)
        assert _cells(table, "compressed") == ["0 B", "4 MiB*"]


@pytest.mark.asyncio
async def test_a_partly_grouped_app_stars_its_compressed_sum() -> None:
    reader = Reader()
    _app(reader, "App", 10, 300, compressed=4 * MIB)
    reader.add(11, 10, "helper", None, 5, compressed=1 * MIB)  # no path: grouping is partial
    backend = _backend(reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#table", RowTable)
        assert _cells(table, "compressed") == ["5 MiB*"]


@pytest.mark.asyncio
async def test_at_80_columns_the_app_name_gives_way_and_compressed_still_fits() -> None:
    backend = _backend(_three_apps())
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(80, 22)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, DarwinMainScreen)
        table = screen.query_one("#table", RowTable)
        assert list(table.column_keys) == ["app", "footprint", "compressed", "delta", "procs"]
        assert 8 <= table.column_width("app") < 32  # the name yields first
        fixed = ("footprint", "compressed", "delta", "procs")
        assert [table.column_width(key) for key in fixed] == [12, 12, 11, 7]
        assert table.get_column_label("compressed") == "COMPRESSED"
        await pilot.resize_terminal(79, 22)
        await pilot.pause()
        assert "compressed" not in list(table.column_keys)
        assert "delta" in list(table.column_keys)  # ΔMEM holds on until 65
        await pilot.resize_terminal(100, 22)
        await pilot.pause()
        assert list(table.column_keys) == [
            "app",
            "footprint",
            "compressed",
            "delta",
            "resident",
            "procs",
        ]


@pytest.mark.asyncio
async def test_sorting_by_a_column_a_resize_hides_falls_back_to_memory() -> None:
    backend = _backend(_three_apps())
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#table", RowTable)
        await pilot.press("c")
        assert _cells(table, "app")[0] == "Mid"
        await pilot.resize_terminal(70, 22)
        await pilot.pause()
        assert _cells(table, "app") == ["Big", "Mid", "Small"]
        await pilot.press("c")  # a hidden column's key does nothing
        assert _cells(table, "app") == ["Big", "Mid", "Small"]


@pytest.mark.asyncio
async def test_the_process_view_shows_and_sorts_compressed_flat_and_grouped() -> None:
    reader = Reader()
    _app(reader, "App", 10, 100, compressed=1 * MIB)
    reader.add(11, 10, "alpha", "/usr/bin/alpha", 40, compressed=9 * MIB)
    reader.add(12, 10, "alpha", "/usr/bin/alpha", 30, compressed=None)
    reader.add(13, 10, "beta", "/usr/bin/beta", 50, compressed=5 * MIB)
    backend = _backend(reader)
    app = AppMemApp(interval=60, main_screen_factory=lambda: DarwinMainScreen(backend, 60))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        pilot.app.screen.query_one("#table", RowTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        detail = pilot.app.screen
        assert isinstance(detail, DarwinProcessesScreen)
        table = detail.query_one("#table", RowTable)
        assert list(table.column_keys) == ["pid", "name", "footprint", "compressed", "state"]
        assert dict(zip(table.row_keys, _cells(table, "compressed"), strict=True)) == {
            "10:100": "1 MiB",
            "11:110": "9 MiB",
            "12:120": "?",
            "13:130": "5 MiB",
        }
        await pilot.press("c")
        assert _cells(table, "pid") == ["11", "13", "10", "12"]  # largest first, unknown last
        await pilot.press("g")
        assert list(table.column_keys) == [
            "name",
            "footprint",
            "compressed",
            "count",
            "unreadable",
        ]
        assert dict(zip(_cells(table, "name"), _cells(table, "compressed"), strict=True)) == {
            "App": "1 MiB",
            "alpha": "9 MiB*",
            "beta": "5 MiB",
        }
        assert _cells(table, "name")[0] == "alpha"  # the sort key and direction carry over
        await pilot.press("c")
        assert _cells(table, "name")[0] == "App"  # smallest first
        status = str(detail.query_one("#status", Static).content)
        assert "compressed" in status and "/" in status


def test_help_says_compressed_is_part_of_memory_and_what_a_large_share_means() -> None:
    body = " ".join(DarwinHelpScreen._body(200).split())  # pyright: ignore[reportPrivateUsage]
    assert "COMPRESSED" in body
    assert "included in MEMORY, not added on top" in body
    assert "squeezed to make room" in body
    assert "experimental" not in body.lower()
