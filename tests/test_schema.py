"""Tests for `appmem schema`'s introspection index and command detail."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from appmem import schema
from appmem.cli import main


def _true() -> bool:
    return True


def test_index_has_every_required_field() -> None:
    index = schema.index()
    for field in (
        "schema_version",
        "tool_version",
        "conformance",
        "global_flags",
        "format_defaults",
        "exit_codes",
        "commands",
        "command",
    ):
        assert field in index


def test_commands_sorted_with_unnamed_first() -> None:
    commands = cast("list[dict[str, object]]", schema.index()["commands"])
    names = [cast(str, command["name"]) for command in commands]
    assert names == sorted(names)
    assert names[0] == ""
    assert names == ["", "app", "snapshot"]


def test_command_field_equals_root_detail() -> None:
    assert schema.index()["command"] == schema.root_detail()


def test_global_flags_is_only_json() -> None:
    flags = cast("list[dict[str, object]]", schema.index()["global_flags"])
    assert [flag["name"] for flag in flags] == ["json"]


@pytest.mark.parametrize("name", ["snapshot", "app"])
def test_detail_has_every_required_field_and_no_tool_wide_overrides(name: str) -> None:
    detail = schema.detail([name])
    assert detail is not None
    for field in ("name", "description", "args", "flags", "effects", "confirm", "interactive"):
        assert field in detail
    assert "exit_codes" not in detail  # neither command adds or refines a code
    assert "format_defaults" not in detail  # neither command differs from the tool default


def test_unknown_path_returns_none() -> None:
    assert schema.detail(["bogus"]) is None
    assert schema.detail(["snapshot", "extra"]) is None
    assert schema.detail([""]) is None  # the unnamed command has no `schema <path>` form


def test_cli_unknown_schema_path_is_invalid_input_with_next(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["schema", "bogus"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    last_line = capsys.readouterr().err.strip().splitlines()[-1]
    error = json.loads(last_line)["error"]
    assert error["kind"] == "invalid_input"
    assert error["next"] == ["appmem", "schema"]


def test_cli_schema_json_flag_writes_the_same_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["schema"], root=tmp_path, stdin_isatty=_true, stdout_isatty=_true)
    without_flag = capsys.readouterr().out

    main(["schema", "--json"], root=tmp_path, stdin_isatty=_true, stdout_isatty=_true)
    with_flag = capsys.readouterr().out

    assert without_flag == with_flag
    assert json.loads(without_flag) == schema.index()


def test_cli_schema_stdout_is_exactly_one_json_value(capsys: pytest.CaptureFixture[str]) -> None:
    main(["schema", "snapshot"], stdin_isatty=_true, stdout_isatty=_true)
    out = capsys.readouterr().out
    # One value, one trailing newline: a second `loads` on the remainder would
    # fail if anything followed the first JSON value.
    json.loads(out)
    assert out.count("\n") == 1


def test_cli_schema_does_not_touch_the_cgroup_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No cgroup tree at all under `tmp_path`: `schema` must still succeed.
    exit_code = main(["schema"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == schema.index()
