"""Tests for `appmem.cli` (SPEC.md "Command line", "Errors").

Most of these never reach `AppMemApp.run()`: the interval/flag/version cases
fail during argument parsing, and the TTY/cgroup cases fail before the app is
built. The Textual pilot exercises `MainScreen`/`AppMemApp` directly; the one
exception here monkeypatches `AppMemApp.run` to check `_run_app`'s own
post-run wiring (the JSON line printed after a mid-run cgroup vanish, final
review slice 4 round 2 item 4) without driving a real Textual app.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from appmem import __version__
from appmem.cli import main
from appmem.ui.app import AppMemApp
from helpers import user_service_root, write_memory_stat


def _true() -> bool:
    return True


def _false() -> bool:
    return False


def _last_json_line(stderr: str) -> dict[str, object]:
    return json.loads(stderr.strip().splitlines()[-1])


def test_interval_below_minimum_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["-i", "0.1"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    error = _last_json_line(capsys.readouterr().err)
    assert error == {"error": {"kind": "usage", "message": error["error"]["message"]}}  # type: ignore[index]


def test_interval_not_a_number_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["-i", "abc"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "usage"  # type: ignore[index]


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_interval_not_finite_is_a_usage_error(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["-i", value], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "usage"  # type: ignore[index]


def test_unknown_flag_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--bogus"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "usage: appmem" in stderr  # SPEC.md: fails "with the accepted form"
    last_line = stderr.strip().splitlines()[-1]
    assert json.loads(last_line)["error"]["kind"] == "usage"


def test_non_tty_exits_with_not_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([], stdin_isatty=_false, stdout_isatty=_true)

    assert exc_info.value.code == 2
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "not_a_tty"  # type: ignore[index]
    assert error["error"]["action"] == "user"  # type: ignore[index]


def test_missing_user_cgroup_tree_exits_with_cgroup_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No user@$UID.service tree created under tmp_path at all.
    with pytest.raises(SystemExit) as exc_info:
        main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 1
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]
    assert "missing cgroup path" in error["error"]["message"]  # type: ignore[index,operator]


def test_missing_memory_stat_exits_with_cgroup_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The user@$UID.service dir exists but has no memory.stat (controller not
    # enabled there).
    user_service_root(tmp_path, uid=1000).mkdir(parents=True)

    with pytest.raises(SystemExit) as exc_info:
        main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 1
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]


def test_prints_json_error_line_after_the_run_when_cgroup_vanished_mid_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `_run_app` prints the `cgroup_unavailable` JSON line itself once
    # `app.cgroup_error_message` is set after `app.run()` returns (SPEC.md
    # "Errors"); nothing else in this suite reaches that branch, since the
    # pilot tests check `cgroup_error_message`/`return_code` directly instead
    # of running a real app through `main()` (final review, slice 4 round 2
    # item 4). The pre-start check passes here (a valid fixture tree), so
    # `main()` reaches `_run_app`; `AppMemApp.run` is faked to skip actually
    # driving a Textual app and just set the message a real mid-run failure
    # would have set.
    write_memory_stat(user_service_root(tmp_path, uid=1000))

    def _fake_run(self: AppMemApp, *args: object, **kwargs: object) -> None:
        self.cgroup_error_message = "missing cgroup path: fake mid-run vanish"

    monkeypatch.setattr(AppMemApp, "run", _fake_run)

    exit_code = main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert exit_code == 1
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]
    assert error["error"]["message"] == "missing cgroup path: fake mid-run vanish"  # type: ignore[index]


def test_version_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_lists_flags_keys_and_pressure_and_an_example(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "-i, --interval" in out
    assert "--system" in out
    assert "q / Ctrl+C" in out
    assert "Enter" in out  # slice-3 key, shipped in the same release, not "coming soon"
    assert "coming soon" not in out.lower()
    assert "memory pressure" in out.lower()
    assert "idle pages were paged out" in out
    assert "appmem -i 2 --system" in out
