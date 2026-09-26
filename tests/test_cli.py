"""Tests for `appmem.cli` (SPEC.md "Command line", "Errors").

Most of these never reach `AppMemApp.run()`: the interval/flag/version cases
fail during argument parsing, and the TTY/cgroup cases fail before the app is
built. The Textual pilot exercises `MainScreen`/`AppMemApp` directly; the one
exception here monkeypatches `AppMemApp.run` to check `_run_app`'s own
post-run wiring (the JSON line printed after a mid-run cgroup vanish)
without driving a real Textual app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from appmem import __version__, collect, report
from appmem.cli import main
from appmem.collect import CgroupUnavailableError
from appmem.ui.app import AppMemApp
from helpers import make_unit, user_service_root, write_meminfo, write_memory_stat, write_uptime


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
    error = _last_json_line(capsys.readouterr().err)["error"]
    assert error["kind"] == "invalid_input"  # type: ignore[index]
    assert error["action"] == "agent"  # type: ignore[index]
    assert error["hint"].startswith("Accepted form: appmem [-h]")  # type: ignore[index,union-attr]


def test_interval_not_a_number_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["-i", "abc"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "invalid_input"  # type: ignore[index]


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_interval_not_finite_is_a_usage_error(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["-i", value], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "invalid_input"  # type: ignore[index]


def test_unknown_flag_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--bogus"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "usage: appmem" in stderr  # fails "with the accepted form"
    last_line = stderr.strip().splitlines()[-1]
    assert json.loads(last_line)["error"]["kind"] == "invalid_input"


def test_non_tty_exits_with_terminal_required(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([], root=tmp_path, uid=1000, stdin_isatty=_false, stdout_isatty=_true)

    assert exc_info.value.code == 1
    error = _last_json_line(capsys.readouterr().err)
    assert error["error"]["kind"] == "terminal_required"  # type: ignore[index]
    assert error["error"]["action"] == "agent"  # type: ignore[index]
    assert error["error"]["next"] == ["appmem", "snapshot"]  # type: ignore[index]


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
    # of running a real app through `main()`. The pre-start check passes here
    # (a valid fixture tree), so
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
    assert capsys.readouterr().out == f"{__version__}\n"


@pytest.mark.parametrize(
    "case",
    [
        (False, True, [], None),  # piped stdin
        (True, False, [], None),  # piped stdout
        (True, True, ["--json"], None),  # explicit --json
        (True, True, [], "1"),  # NO_INPUT set
    ],
)
def test_bare_root_off_a_terminal_is_terminal_required(
    case: tuple[bool, bool, list[str], str | None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_ok, stdout_ok, extra_argv, no_input = case
    if no_input is not None:
        monkeypatch.setenv("NO_INPUT", no_input)
    else:
        monkeypatch.delenv("NO_INPUT", raising=False)

    # An empty fixture root: if the check ever let this call through, it fails
    # on the missing tree instead of reading the live /sys and starting the TUI.
    with pytest.raises(SystemExit) as exc_info:
        main(
            extra_argv,
            root=tmp_path,
            uid=1000,
            stdin_isatty=lambda: stdin_ok,
            stdout_isatty=lambda: stdout_ok,
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = _last_json_line(captured.err)["error"]
    assert error["kind"] == "terminal_required"  # type: ignore[index]
    base = ["appmem", "snapshot"]
    expected = [*base, "--json"] if "--json" in extra_argv else base
    assert error["next"] == expected  # type: ignore[index]


def test_interval_with_a_named_command_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["-i", "2", "snapshot"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "invalid_input"  # type: ignore[index]


@pytest.mark.parametrize("argv", [["--system", "app", "ghostty"], ["--system", "schema"]])
def test_root_system_with_a_command_that_has_no_system_flag_is_invalid_input(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(argv, root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _last_json_line(captured.err)["error"]["kind"] == "invalid_input"  # type: ignore[index]


@pytest.mark.parametrize(
    "argv",
    [
        ["snapshot", "--limit", "0"],  # argparse's own error path
        ["-i", "2", "snapshot"],
        ["schema", "bogus"],
        [],  # terminal_required (stdin is not a TTY below)
        ["app", "nosuch"],  # not_found
        ["snapshot", "--json"],  # cgroup_unavailable: no user tree in the fixture
    ],
)
def test_every_error_object_carries_kind_message_and_action(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if argv == ["app", "nosuch"]:
        write_memory_stat(user_service_root(tmp_path, uid=1000))

    with pytest.raises(SystemExit):
        main(argv, root=tmp_path, uid=1000, stdin_isatty=_false, stdout_isatty=_false)

    error = _last_json_line(capsys.readouterr().err)["error"]
    assert {"kind", "message", "action"} <= set(error)  # type: ignore[arg-type]


def test_system_before_or_after_the_command_name_behaves_the_same(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    write_memory_stat(user_root)
    write_meminfo(
        tmp_path, mem_total_kb=1024, mem_available_kb=512, swap_total_kb=0, swap_free_kb=0
    )
    write_uptime(tmp_path, 1)
    system_slice = tmp_path / "sys" / "fs" / "cgroup" / "system.slice"
    make_unit(system_slice / "cups.service", anon=5 * 1024 * 1024)

    main(
        ["--system", "snapshot", "--json"],
        root=tmp_path,
        uid=1000,
        stdin_isatty=_true,
        stdout_isatty=_true,
    )
    before = capsys.readouterr().out

    main(
        ["snapshot", "--system", "--json"],
        root=tmp_path,
        uid=1000,
        stdin_isatty=_true,
        stdout_isatty=_true,
    )
    after = capsys.readouterr().out

    assert before == after
    assert json.loads(before)["apps"]["items"]  # the system unit is actually there


def test_root_help_names_the_commands_schema_and_json_and_how_to_get_command_help(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--help"], root=tmp_path, stdin_isatty=_true, stdout_isatty=_true)

    out = capsys.readouterr().out
    assert "appmem schema" in out
    assert "--json" in out
    assert "snapshot" in out
    assert "app" in out
    assert "appmem snapshot --help" in out


@pytest.mark.parametrize(
    ("argv", "expected_flags"),
    [
        (["snapshot", "--help"], ["--system", "--limit"]),
        (["app", "--help"], ["--scope", "--limit"]),
        (["schema", "--help"], []),
    ],
)
def test_each_command_help_names_its_own_flags_and_defaults(
    argv: list[str], expected_flags: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(argv, root=tmp_path, stdin_isatty=_true, stdout_isatty=_true)

    out = capsys.readouterr().out
    for flag in expected_flags:
        assert flag in out
    if expected_flags:
        assert "default" in out.lower()


@pytest.mark.parametrize("argv", [["--bogus"], ["-i", "2", "snapshot"], ["schema", "bogus"]])
def test_error_object_is_the_last_stderr_line_and_never_on_stdout(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(argv, stdin_isatty=_true, stdout_isatty=_true)

    captured = capsys.readouterr()
    assert captured.out == ""
    last_line = captured.err.strip().splitlines()[-1]
    parsed = json.loads(last_line)
    assert "error" in parsed
    assert "kind" in parsed["error"]


def test_snapshot_cgroup_unavailable_when_the_user_tree_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            ["snapshot", "--json"],
            root=tmp_path,
            uid=1000,
            stdin_isatty=_true,
            stdout_isatty=_true,
        )

    assert exc_info.value.code == 1
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]


def test_app_cgroup_unavailable_when_the_user_tree_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            ["app", "ghostty", "--json"],
            root=tmp_path,
            uid=1000,
            stdin_isatty=_true,
            stdout_isatty=_true,
        )

    assert exc_info.value.code == 1
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]


def test_snapshot_cgroup_unavailable_when_the_tree_vanishes_mid_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    write_meminfo(
        tmp_path, mem_total_kb=1024, mem_available_kb=512, swap_total_kb=0, swap_free_kb=0
    )

    def _vanished(*args: object, **kwargs: object) -> None:
        raise CgroupUnavailableError("missing cgroup path: vanished mid-read")

    monkeypatch.setattr(collect, "read_system", _vanished)

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["snapshot", "--json"],
            root=tmp_path,
            uid=1000,
            stdin_isatty=_true,
            stdout_isatty=_true,
        )

    assert exc_info.value.code == 1
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]


def test_app_cgroup_unavailable_when_the_tree_vanishes_mid_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))

    def _raise(*args: object, **kwargs: object) -> list[Path]:
        raise CgroupUnavailableError("missing cgroup path: vanished mid-read")

    monkeypatch.setattr(collect, "find_app_units", _raise)

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["app", "ghostty", "--json"],
            root=tmp_path,
            uid=1000,
            stdin_isatty=_true,
            stdout_isatty=_true,
        )

    assert exc_info.value.code == 1
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "cgroup_unavailable"  # type: ignore[index]


def test_keyboard_interrupt_during_snapshot_is_interrupted_with_no_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))

    def _raise(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(report, "snapshot_document", _raise)

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["snapshot", "--json"],
            root=tmp_path,
            uid=1000,
            stdin_isatty=_true,
            stdout_isatty=_true,
        )

    assert exc_info.value.code == 130
    error = _last_json_line(capsys.readouterr().err)["error"]
    assert error["kind"] == "interrupted"  # type: ignore[index]
    assert error["action"] == "user"  # type: ignore[index]


def test_broken_pipe_on_stdout_exits_zero_with_no_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    write_meminfo(
        tmp_path, mem_total_kb=1024, mem_available_kb=512, swap_total_kb=0, swap_free_kb=0
    )
    write_uptime(tmp_path, 1)

    def _broken_write(text: str) -> int:
        raise BrokenPipeError

    def _fake_fileno() -> int:
        return 1  # capsys's stdout has no real fd

    def _fake_dup2(*args: object, **kwargs: object) -> int:
        return 0  # keep the real fd 1 intact for capsys

    monkeypatch.setattr("sys.stdout.write", _broken_write)
    monkeypatch.setattr("sys.stdout.fileno", _fake_fileno)
    monkeypatch.setattr(os, "dup2", _fake_dup2)

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["snapshot", "--json"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true
        )

    assert exc_info.value.code == 0
    assert capsys.readouterr().err == ""


def test_help_lists_flags_keys_and_pressure_and_an_example(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"], root=tmp_path, stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "-i, --interval" in out
    assert "--system" in out
    assert "--theme" in out  # SPEC.md "Command line": --help lists --theme
    assert "T / Ctrl+P" in out
    assert "q / Ctrl+C" in out
    assert "Enter" in out  # slice-3 key, shipped in the same release, not "coming soon"
    assert "coming soon" not in out.lower()
    assert "memory pressure" in out.lower()
    assert "idle pages were paged out" in out
    assert "appmem -i 2 --system" in out


@pytest.mark.parametrize(
    ("argv", "expected_next"),
    [
        (["app", "nosuch"], ["appmem", "snapshot"]),
        (
            ["app", "nosuch", "--scope", "system", "--json"],
            ["appmem", "snapshot", "--system", "--json"],
        ),
    ],
)
def test_app_not_found_mirrors_scope_and_json_into_next(
    argv: list[str], expected_next: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))

    with pytest.raises(SystemExit) as exc_info:
        main(argv, root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = _last_json_line(captured.err)["error"]
    assert error["kind"] == "not_found"  # type: ignore[index]
    assert error["next"] == expected_next  # type: ignore[index]


def test_snapshot_json_flag_is_mirrored_into_next_only_when_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    write_memory_stat(user_root)
    write_meminfo(
        tmp_path, mem_total_kb=1024, mem_available_kb=512, swap_total_kb=0, swap_free_kb=0
    )
    write_uptime(tmp_path, 1)
    make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * 1024 * 1024)

    main(["snapshot", "--json"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)
    with_flag = json.loads(capsys.readouterr().out)
    assert with_flag["next"][-1] == "--json"

    # Non-TTY stdout also writes JSON, but --json was never passed on this
    # call, so nothing is mirrored into `next`.
    main(["snapshot"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_false)
    without_flag = json.loads(capsys.readouterr().out)
    assert "--json" not in without_flag["next"]


def test_keyboard_interrupt_during_app_is_interrupted_with_no_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))

    def _raise(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(report, "app_document", _raise)

    with pytest.raises(SystemExit) as exc_info:
        main(
            ["app", "ghostty", "--json"],
            root=tmp_path,
            uid=1000,
            stdin_isatty=_true,
            stdout_isatty=_true,
        )

    assert exc_info.value.code == 130
    error = _last_json_line(capsys.readouterr().err)["error"]
    assert error["kind"] == "interrupted"  # type: ignore[index]
    assert error["action"] == "user"  # type: ignore[index]


def test_snapshot_on_a_tty_prints_text_and_with_json_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    write_memory_stat(user_root)
    write_meminfo(
        tmp_path, mem_total_kb=1024, mem_available_kb=512, swap_total_kb=0, swap_free_kb=0
    )
    write_uptime(tmp_path, 1)
    make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * 1024 * 1024)

    main(["snapshot"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)
    text_out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(text_out)
    assert "RAM" in text_out
    assert "ghostty" in text_out

    main(["snapshot", "--json"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)
    json_out = capsys.readouterr().out
    assert json.loads(json_out)["apps"]["items"]


def test_app_on_a_tty_prints_text_and_with_json_prints_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_root = user_service_root(tmp_path, uid=1000)
    write_memory_stat(user_root)
    write_uptime(tmp_path, 1)
    make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * 1024 * 1024, pids=[])

    main(["app", "ghostty"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)
    text_out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(text_out)
    assert "ghostty" in text_out
    assert "PID" in text_out

    main(
        ["app", "ghostty", "--json"],
        root=tmp_path,
        uid=1000,
        stdin_isatty=_true,
        stdout_isatty=_true,
    )
    json_out = capsys.readouterr().out
    assert json.loads(json_out)["name"] == "ghostty"


def test_snapshot_text_reads_the_cgroup_tree_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The total in "N of M apps shown" must come from the same pass as the
    # page: a second walk doubles the collector cost of every default TTY call
    # and can take M from a different sample than N.
    user_root = user_service_root(tmp_path, uid=1000)
    write_memory_stat(user_root)
    write_meminfo(
        tmp_path, mem_total_kb=1024, mem_available_kb=512, swap_total_kb=0, swap_free_kb=0
    )
    write_uptime(tmp_path, 1)
    make_unit(user_root / "app.slice" / "app-ghostty.service", anon=2 * 1024 * 1024)

    real_find_units = collect.find_units
    calls: list[None] = []

    def _counting_find_units(*args: object, **kwargs: object) -> list[Path]:
        calls.append(None)
        return real_find_units(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(collect, "find_units", _counting_find_units)

    main(["snapshot"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert len(calls) == 1


# --- --theme (SPEC.md "Command line": startup theme precedence) ------------------


def _capture_theme_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Let `AppMemApp.__init__` run for real (so `_run_app`'s post-construction
    reads of `cgroup_error_message`/`return_code` stay valid), but record its
    keyword arguments and replace `.run()` with a no-op so no real Textual
    app is driven (same idea as the mid-run cgroup-vanish test above)."""
    captured: dict[str, object] = {}
    real_init = AppMemApp.__init__

    def _capturing_init(self: AppMemApp, **kwargs: object) -> None:
        captured.update(kwargs)
        real_init(self, **kwargs)  # type: ignore[arg-type]

    def _fake_run(self: AppMemApp, *args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(AppMemApp, "__init__", _capturing_init)
    monkeypatch.setattr(AppMemApp, "run", _fake_run)
    return captured


def test_theme_bogus_is_invalid_input_with_valid_names_listed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--theme", "bogus"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    error = _last_json_line(capsys.readouterr().err)["error"]
    assert error["kind"] == "invalid_input"  # type: ignore[index]
    message = error["message"]  # type: ignore[index]
    assert "nord" in message and "dracula" in message  # type: ignore[operator]
    assert "ansi-dark" not in message and "ansi-light" not in message  # type: ignore[operator]


def test_theme_terminal_dark_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    captured = _capture_theme_kwargs(monkeypatch)

    main(
        ["--theme", "terminal-dark"],
        root=tmp_path,
        uid=1000,
        stdin_isatty=_true,
        stdout_isatty=_true,
    )

    assert captured["theme"] == "terminal-dark"


def test_theme_ansi_dark_alias_is_accepted_and_resolves_to_terminal_dark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    captured = _capture_theme_kwargs(monkeypatch)

    main(["--theme", "ansi-dark"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "terminal-dark"  # the app only ever runs the honest name


def test_theme_with_a_named_command_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--theme", "nord", "snapshot"], stdin_isatty=_true, stdout_isatty=_true)

    assert exc_info.value.code == 2
    assert _last_json_line(capsys.readouterr().err)["error"]["kind"] == "invalid_input"  # type: ignore[index]


def test_theme_flag_wins_over_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("APPMEM_THEME", "gruvbox")
    (xdg / "appmem").mkdir(parents=True)
    (xdg / "appmem" / "config.toml").write_text('theme = "nord"\n')
    captured = _capture_theme_kwargs(monkeypatch)

    main(["--theme", "dracula"], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "dracula"
    assert captured["config_theme"] == "nord"  # the file's own value, kept as the baseline
    assert captured["theme_warnings"] == ()


def test_theme_env_wins_over_file_when_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("APPMEM_THEME", "gruvbox")
    (xdg / "appmem").mkdir(parents=True)
    (xdg / "appmem" / "config.toml").write_text('theme = "nord"\n')
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "gruvbox"
    assert captured["config_theme"] == "nord"
    assert captured["theme_warnings"] == ()


def test_theme_file_wins_over_default_when_no_flag_or_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    (xdg / "appmem").mkdir(parents=True)
    (xdg / "appmem" / "config.toml").write_text('theme = "monokai"\n')
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "monokai"
    assert captured["config_theme"] == "monokai"


def test_theme_falls_back_to_textual_default_with_nothing_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "textual-dark"
    assert captured["config_theme"] is None
    assert captured["theme_warnings"] == ()


def test_theme_unknown_env_name_falls_through_to_file_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("APPMEM_THEME", "not-a-real-theme")
    (xdg / "appmem").mkdir(parents=True)
    (xdg / "appmem" / "config.toml").write_text('theme = "nord"\n')
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "nord"
    warnings = captured["theme_warnings"]
    assert len(warnings) == 1  # type: ignore[arg-type]
    assert "not-a-real-theme" in warnings[0]  # type: ignore[index]


def test_theme_malformed_config_file_falls_back_to_default_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    (xdg / "appmem").mkdir(parents=True)
    (xdg / "appmem" / "config.toml").write_text("theme = [unterminated\n")
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "textual-dark"
    warnings = captured["theme_warnings"]
    assert len(warnings) == 1  # type: ignore[arg-type]


def test_theme_textual_theme_wins_when_nothing_else_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    monkeypatch.setenv("TEXTUAL_THEME", "gruvbox")
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "gruvbox"
    assert captured["theme_warnings"] == ()


def test_theme_config_file_wins_over_textual_theme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    monkeypatch.setenv("TEXTUAL_THEME", "gruvbox")
    (xdg / "appmem").mkdir(parents=True)
    (xdg / "appmem" / "config.toml").write_text('theme = "nord"\n')
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "nord"
    assert captured["theme_warnings"] == ()


def test_theme_unknown_textual_theme_falls_back_with_a_warning_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    monkeypatch.setenv("TEXTUAL_THEME", "not-a-real-theme")
    captured = _capture_theme_kwargs(monkeypatch)

    main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)

    assert captured["theme"] == "textual-dark"
    warnings = captured["theme_warnings"]
    assert len(warnings) == 1  # type: ignore[arg-type]
    assert "not-a-real-theme" in warnings[0]  # type: ignore[index]


def test_theme_unreadable_config_directory_falls_back_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_memory_stat(user_service_root(tmp_path, uid=1000))
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("APPMEM_THEME", raising=False)
    appmem_dir = xdg / "appmem"
    appmem_dir.mkdir(parents=True)
    (appmem_dir / "config.toml").write_text('theme = "nord"\n')
    appmem_dir.chmod(0o000)
    captured = _capture_theme_kwargs(monkeypatch)
    try:
        if os.access(appmem_dir / "config.toml", os.R_OK):  # running as root
            pytest.skip("cannot make a directory unreadable to this user")
        main([], root=tmp_path, uid=1000, stdin_isatty=_true, stdout_isatty=_true)
    finally:
        appmem_dir.chmod(0o700)

    assert captured["theme"] == "textual-dark"
    warnings = captured["theme_warnings"]
    assert len(warnings) == 1  # type: ignore[arg-type]
