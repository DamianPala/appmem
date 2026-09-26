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
    "required": ["apps", "has_more"],
    "properties": {
        "apps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
        },
        "has_more": {"type": "boolean"},
        "next": {"type": "array", "items": {"type": "string"}},
    },
}
DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["app", "has_more_processes", "has_more_commands"],
    "properties": {
        "app": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"}},
        },
        "has_more_processes": {"type": "boolean"},
        "has_more_commands": {"type": "boolean"},
        "next": {"type": "array", "items": {"type": "string"}},
    },
}


def test_pagination_follows_new_inventory_until_both_ids_are_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def run_command(
        command: list[str | Path], *, cwd: Path, deadline: float, cap: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        args = [str(item) for item in command[1:]]
        calls.append(args)
        if args[0] == "snapshot":
            limit = int(args[args.index("--limit") + 1])
            # Another app appears between samples. The published next limit
            # from the prior sample can no longer show both controlled IDs.
            inventory = ["other", "new", "A", "B"] if limit > 1 else ["other", "A", "B"]
            page: dict[str, Any] = {
                "apps": [{"id": app_id} for app_id in inventory[:limit]],
                "has_more": len(inventory) > limit,
            }
            if page["has_more"]:
                page["next"] = ["appmem", "snapshot", "--limit", str(len(inventory)), "--json"]
        else:
            app_id = args[1]
            limit = int(args[args.index("--limit") + 1])
            page = {
                "app": {"id": app_id},
                "has_more_processes": limit == 1,
                "has_more_commands": False,
            }
            if limit == 1:
                page["next"] = ["appmem", "app", app_id, "--limit", "2", "--json"]
        return subprocess.CompletedProcess(command, 0, json.dumps(page), "")

    monkeypatch.setattr(harness, "run_command", run_command)
    harness.pagination(
        tmp_path / "appmem",
        tmp_path,
        SNAPSHOT_SCHEMA,
        DETAIL_SCHEMA,
        app_ids=("A", "B"),
        deadline=time.monotonic() + 5,
    )
    assert [args[2] for args in calls if args[0] == "snapshot"] == ["1", "3", "4"]


def test_require_error_exposes_only_static_check_id() -> None:
    with pytest.raises(harness.CheckError) as error:
        harness.require(False, "sensitive dynamic diagnostic")
    assert error.value.check_id.startswith("test_require_error_exposes_only_static_check_id:")
    assert "sensitive" not in error.value.check_id


def test_pagination_stops_when_controlled_identity_never_appears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = 0

    def run_command(
        command: list[str | Path], *, cwd: Path, deadline: float, cap: float = 30.0
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        assert command[1] == "snapshot"
        calls += 1
        page = {
            "apps": [{"id": "other"}],
            "has_more": True,
            "next": ["appmem", "snapshot", "--limit", str(calls + 1), "--json"],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(page), "")

    monkeypatch.setattr(harness, "run_command", run_command)
    with pytest.raises(harness.CheckError, match="within four pages"):
        harness.pagination(
            tmp_path / "appmem",
            tmp_path,
            SNAPSHOT_SCHEMA,
            DETAIL_SCHEMA,
            app_ids=("A", "B"),
            deadline=time.monotonic() + 5,
        )
    assert calls == 5
