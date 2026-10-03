"""Deterministic checks for the installed-wheel acceptance harness."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/darwin_integration.py"
SPEC = importlib.util.spec_from_file_location("darwin_integration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


SNAPSHOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["apps"],
    "properties": {
        "apps": {
            "type": "object",
            "required": ["items", "has_more"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "string"}},
                    },
                },
                "has_more": {"type": "boolean"},
            },
        },
        "next": {"type": "array", "items": {"type": "string"}},
    },
}
PAGED: dict[str, Any] = {
    "type": "object",
    "required": ["has_more"],
    "properties": {"has_more": {"type": "boolean"}},
}
DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "processes", "commands"],
    "properties": {
        "platform": {"type": "string"},
        "id": {"type": "string"},
        "processes": PAGED,
        "commands": PAGED,
    },
}


def _fake_cli(inventory: list[str]) -> Any:
    def run_command(
        command: list[str | Path], *, cwd: Path, deadline: float, cap: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        args = [str(item) for item in command[1:]]
        if args[0] == "snapshot":
            limit = int(args[args.index("--limit") + 1])
            page: dict[str, Any] = {
                "apps": {
                    "items": [{"id": app_id} for app_id in inventory[:limit]],
                    "has_more": len(inventory) > limit,
                },
                "next": ["appmem", "app", inventory[0], "--json"],
            }
        else:
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 100
            page = {
                "platform": "darwin",
                "id": args[1],
                "processes": {"has_more": limit == 1},
                "commands": {"has_more": False},
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(page), "")

    return run_command


def test_pagination_opens_the_top_app_and_finds_both_ids_in_the_full_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "run_command", _fake_cli(["other", "A", "B"]))
    harness.pagination(
        tmp_path / "appmem",
        tmp_path,
        SNAPSHOT_SCHEMA,
        DETAIL_SCHEMA,
        app_ids=("A", "B"),
        deadline=time.monotonic() + 5,
    )


def test_require_error_exposes_only_static_check_id() -> None:
    with pytest.raises(harness.CheckError) as error:
        harness.require(False, "sensitive dynamic diagnostic")
    assert error.value.check_id.startswith("test_require_error_exposes_only_static_check_id:")
    assert "sensitive" not in error.value.check_id


def test_pagination_fails_when_a_controlled_identity_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "run_command", _fake_cli(["other", "A"]))
    with pytest.raises(harness.CheckError, match="full snapshot omitted"):
        harness.pagination(
            tmp_path / "appmem",
            tmp_path,
            SNAPSHOT_SCHEMA,
            DETAIL_SCHEMA,
            app_ids=("A", "B"),
            deadline=time.monotonic() + 5,
        )


@pytest.mark.parametrize("field_count", [8, 6])
def test_native_counts_requires_current_eight_field_helper_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field_count: int
) -> None:
    calls: list[list[str]] = []
    fields = ["64", "232", "80", "416", "144", "32", "112", "120"]

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = " ".join(fields[:field_count]) if command[1] == "abi" else '{"vm_count": 40}'
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(harness.subprocess, "run", run)
    cli, binary = tmp_path / "bin/appmem", tmp_path / "probe"
    if field_count == 6:
        with pytest.raises(harness.CheckError, match="C helper ABI response malformed"):
            harness.native_counts(cli, binary, tmp_path, time.monotonic() + 5)
        assert calls == [[str(binary), "abi"]]
    else:
        result = harness.native_counts(cli, binary, tmp_path, time.monotonic() + 5)
        assert result == {"vm_sdk_size_bytes": 416, "vm_runtime_count": 40}
        assert len(calls) == 2
        assert calls[1][:3] == [str(cli.parent / "python"), "-I", "-c"]
