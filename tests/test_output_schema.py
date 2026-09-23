"""Every JSON document `snapshot` and `app` emit must match the output schema
`appmem schema` publishes for it: no stray fields, no missing required ones,
`null` only where the schema allows it.

A tiny validator over the restricted keyword set (`type`, `enum`,
`properties`, `required`, `items`) is enough, and keeps the check free of a
JSON Schema dependency. It is strict on purpose: a field the schema does not
list is an error, because the schema's `properties` must name every field
the command can return.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from appmem import schema
from appmem.cli import main
from helpers import (
    make_unit,
    user_service_root,
    write_meminfo,
    write_memory_stat,
    write_pressure,
    write_proc,
    write_uptime,
    write_vmstat,
    write_zswap_enabled,
)

_MIB = 1024 * 1024


def _matches_type(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    python_type = {"string": str, "boolean": bool, "array": list, "object": dict}[name]
    return isinstance(value, python_type)


def _errors(value: Any, node: dict[str, Any], path: str = "$") -> list[str]:
    declared = node["type"]
    types = cast("list[str]", declared if isinstance(declared, list) else [declared])
    if value is None:
        return [] if "null" in types else [f"{path}: null where the schema allows {types}"]
    if not any(_matches_type(value, name) for name in types if name != "null"):
        return [f"{path}: {value!r} is not {types}"]
    if "enum" in node and value not in node["enum"]:
        return [f"{path}: {value!r} not in {node['enum']}"]
    # `json.loads` gives str keys and untyped values; the casts only say so.
    if isinstance(value, dict):
        return _object_errors(cast("dict[str, Any]", value), node, path)
    if isinstance(value, list):
        return [
            error
            for index, item in enumerate(cast("list[Any]", value))
            for error in _errors(item, node["items"], f"{path}[{index}]")
        ]
    return []


def _object_errors(value: dict[str, Any], node: dict[str, Any], path: str) -> list[str]:
    properties = node["properties"]
    errors = [f"{path}.{key}: not in the schema" for key in value if key not in properties]
    errors += [
        f"{path}.{key}: required but missing" for key in node["required"] if key not in value
    ]
    for key, child in value.items():
        if key in properties:
            errors += _errors(child, properties[key], f"{path}.{key}")
    return errors


def _tree(root: Path, *, with_pressure: bool, with_root_stat: bool) -> None:
    user_root = user_service_root(root, uid=1000)
    write_memory_stat(user_root, anon=1)
    write_meminfo(
        root,
        mem_total_kb=32 * 1024,
        mem_available_kb=16 * 1024,
        swap_total_kb=8 * 1024,
        swap_free_kb=4 * 1024,
        # `with_root_stat` also exercises the non-null zswap fields, so the
        # schema check covers both shapes (SPEC.md "Main view"). zswapped
        # stays <= swap used (4096 kB) -- the pool is always a subset of swap.
        zswap_kb=1024 if with_root_stat else None,
        zswapped_kb=2048 if with_root_stat else None,
    )
    write_uptime(root, 1000)
    if with_pressure:
        write_pressure(root, some_avg10=2.5, full_avg10=0.0, some_avg60=1.25)
    if with_root_stat:
        write_memory_stat(root / "sys" / "fs" / "cgroup", anon=64 * _MIB)
        write_zswap_enabled(root, enabled=True)
        write_vmstat(root, zswpwb=7)
    unit = user_root / "app.slice" / "app-ghostty.service"
    make_unit(
        unit, anon=8 * _MIB, kernel=_MIB, file=2 * _MIB, swap=3 * _MIB, zswapped=_MIB, pids=[10, 11]
    )
    write_proc(root, 10, cmdline="ghostty", comm="ghostty", rss_anon_kb=2048, vm_swap_kb=512)
    write_proc(root, 11, cmdline="node", comm="node", rss_anon_kb=1024, vm_swap_kb=0)
    make_unit(root / "sys" / "fs" / "cgroup" / "system.slice" / "cups.service", anon=4 * _MIB)


def _emitted(argv: list[str], capsys: pytest.CaptureFixture[str], root: Path) -> Any:
    exit_code = main(
        argv, root=root, uid=1000, stdin_isatty=lambda: False, stdout_isatty=lambda: False
    )
    assert exit_code == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("with_extras", [True, False])
@pytest.mark.parametrize(
    "argv",
    [
        ["snapshot"],
        ["snapshot", "--system", "--limit", "1", "--json"],
    ],
)
def test_snapshot_documents_match_the_published_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], with_extras: bool
) -> None:
    _tree(tmp_path, with_pressure=with_extras, with_root_stat=with_extras)

    document = _emitted(argv, capsys, tmp_path)

    assert _errors(document, schema.SNAPSHOT_OUTPUT) == []


def test_snapshot_document_without_items_matches_the_published_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000), anon=1)

    document = _emitted(["snapshot"], capsys, tmp_path)

    assert document["apps"]["items"] == []
    assert _errors(document, schema.SNAPSHOT_OUTPUT) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["app", "ghostty"],
        ["app", "ghostty", "--limit", "1", "--json"],
        ["app", "cups", "--scope", "system"],  # a unit with no processes
    ],
)
def test_app_documents_match_the_published_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    _tree(tmp_path, with_pressure=True, with_root_stat=True)

    document = _emitted(argv, capsys, tmp_path)

    assert _errors(document, schema.APP_OUTPUT) == []


def test_the_validator_rejects_stray_missing_and_null_fields() -> None:
    node: dict[str, Any] = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "integer"}, "b": {"type": ["string", "null"]}},
    }
    assert _errors({"a": 1, "b": None}, node) == []
    assert _errors({"a": 1, "c": 2}, node) == ["$.c: not in the schema"]
    assert _errors({"b": "x"}, node) == ["$.a: required but missing"]
    assert _errors({"a": None}, node) == ["$.a: null where the schema allows ['integer']"]
    assert _errors({"a": True}, node) == ["$.a: True is not ['integer']"]
