"""Docs-vs-CLI drift checks for README.md and skills/appmem/SKILL.md.

These tests never edit or read the live `/sys` or `/proc`: the CLI surface
comes from `appmem.schema` (the single source `cli.py` builds its parsers
from) and from `appmem.cli.main(["--help"], ...)`, which never touches a
cgroup tree. README.md and SKILL.md are read straight off disk; this file
never edits them.

Extraction only ever looks inside fenced code blocks and inline code spans
(`` `...` ``), never prose: a sentence like "appmem reads the memory
counters" is not a usage example and must never be mistaken for one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest

from appmem import schema
from appmem.cli import ERROR_KINDS, main

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README_PATH = _REPO_ROOT / "README.md"
_SKILL_PATH = _REPO_ROOT / "skills" / "appmem" / "SKILL.md"

# Descriptors are the single source `cli.py` builds its parsers from;
# `--help` and `--version` are argparse builtins wired directly in `cli.py`
# and have no descriptor of their own, so they are added by hand below.
_FLAG_DESCRIPTORS: tuple[schema.Flag, ...] = (
    schema.JSON_FLAG,
    schema.ROOT_INTERVAL,
    schema.ROOT_SYSTEM,
    schema.SNAPSHOT_SYSTEM,
    schema.SNAPSHOT_LIMIT,
    schema.APP_SCOPE,
    schema.APP_LIMIT,
)
_BUILTIN_LONG_FLAGS = frozenset({"--help", "--version"})

# `schema` is a reserved, always-valid top-level command that the
# introspection index deliberately omits from `commands`.
_RESERVED_COMMAND_NAMES = frozenset({"schema"})

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FENCED_BLOCK = re.compile(r"^```.*?\n(.*?)^```", re.DOTALL | re.MULTILINE)
_APPMEM_WORD = re.compile(r"\bappmem\b")
_NEXT_WORD = re.compile(r"^[ \t]+([a-z][a-z0-9_-]*)")
_LONG_FLAG = re.compile(r"--[a-z][a-z-]*")
_FLAG_AT_SPAN_START = re.compile(r"^(--[a-z][a-z-]*)\b")

_KIND_LIKE = re.compile(r"^[a-z][a-z0-9_]*$")
_KIND_SUFFIXES = ("_required", "_found", "_input", "_unavailable")


def _true() -> bool:
    return True


def _code_spans(markdown: str) -> list[str]:
    """Every fenced code block and inline code span, each as one string."""
    return [*_FENCED_BLOCK.findall(markdown), *_INLINE_CODE.findall(markdown)]


def _commands_mentioned(markdown: str) -> set[str]:
    """Every word directly following `appmem` in a code span (its
    subcommand), e.g. "app" from "`appmem app NAME`". A later `appmem`
    occurring as someone else's path argument, as in "`uv run --project
    appmem appmem snapshot`", never yields the bogus command "appmem"
    itself: that capture is explicitly dropped."""
    commands: set[str] = set()
    for span in _code_spans(markdown):
        for occurrence in _APPMEM_WORD.finditer(span):
            match = _NEXT_WORD.match(span[occurrence.end() :])
            if match and match.group(1) != "appmem":
                commands.add(match.group(1))
    return commands


def _flags_mentioned(markdown: str) -> set[str]:
    """Every `--xyz` flag mentioned as part of an appmem invocation (text
    following an `appmem` word in the same span) or standing for itself in
    a span with no `appmem` in it at all (e.g. a lone "`--json`"). A flag
    that belongs to another program mentioned earlier in the same span,
    as in "`uv run --project ... appmem snapshot --json`", is excluded:
    only text after `appmem` is scanned."""
    flags: set[str] = set()
    for span in _code_spans(markdown):
        stripped = span.strip()
        if "appmem" not in span:
            match = _FLAG_AT_SPAN_START.match(stripped)
            if match:
                flags.add(match.group(1))
            continue
        for occurrence in _APPMEM_WORD.finditer(span):
            flags.update(_LONG_FLAG.findall(span[occurrence.end() :]))
    return flags


def _looks_like_a_kind(word: str) -> bool:
    if word == "interrupted":
        return True
    return _KIND_LIKE.match(word) is not None and word.endswith(_KIND_SUFFIXES)


def _kinds_mentioned(markdown: str) -> set[str]:
    return {word for word in _INLINE_CODE.findall(markdown) if _looks_like_a_kind(word)}


def _known_commands() -> set[str]:
    commands = cast("list[dict[str, object]]", schema.index()["commands"])
    names = {cast(str, command["name"]) for command in commands}
    return (names - {""}) | _RESERVED_COMMAND_NAMES


def _known_long_flags() -> set[str]:
    flags = set(_BUILTIN_LONG_FLAGS)
    for flag in _FLAG_DESCRIPTORS:
        flags.add(f"--{flag.name}")
        flags.update(f"--{alias}" for alias in flag.aliases if len(alias) != 1)
    return flags


def _skill_frontmatter(markdown: str) -> dict[str, str]:
    assert markdown.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    end = markdown.index("\n---", 4)
    fields: dict[str, str] = {}
    for line in markdown[4:end].splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


@pytest.fixture(scope="module")
def readme_text() -> str:
    return _README_PATH.read_text()


@pytest.fixture(scope="module")
def skill_text() -> str:
    return _SKILL_PATH.read_text()


def test_skill_file_exists() -> None:
    assert _SKILL_PATH.is_file()


def test_readme_mentions_the_schema_command(readme_text: str) -> None:
    assert "schema" in _commands_mentioned(readme_text)


def test_readme_links_the_skill_file(readme_text: str) -> None:
    assert "skills/appmem/SKILL.md" in readme_text


def test_readme_commands_are_in_the_schema_index(readme_text: str) -> None:
    mentioned = _commands_mentioned(readme_text)
    assert mentioned  # the extraction actually found something to check
    assert mentioned <= _known_commands()


def test_skill_commands_are_in_the_schema_index(skill_text: str) -> None:
    mentioned = _commands_mentioned(skill_text)
    assert mentioned
    assert mentioned <= _known_commands()


def test_readme_flags_are_accepted_by_a_parser(readme_text: str) -> None:
    mentioned = _flags_mentioned(readme_text)
    assert mentioned
    assert mentioned <= _known_long_flags()


def test_skill_flags_are_accepted_by_a_parser(skill_text: str) -> None:
    mentioned = _flags_mentioned(skill_text)
    assert mentioned
    assert mentioned <= _known_long_flags()


def test_skill_has_no_flag_table(skill_text: str) -> None:
    for line in skill_text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("| --")
        assert not stripped.startswith("| `--")


def test_root_help_lists_every_index_command_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"], stdin_isatty=_true, stdout_isatty=_true)
    out = capsys.readouterr().out
    commands = cast("list[dict[str, object]]", schema.index()["commands"])
    for command in commands:
        name = cast(str, command["name"])
        if name:
            assert name in out


def test_readme_error_kinds_are_emittable(readme_text: str) -> None:
    kinds = _kinds_mentioned(readme_text)
    assert kinds
    assert kinds <= set(ERROR_KINDS)


def test_skill_error_kinds_are_emittable(skill_text: str) -> None:
    kinds = _kinds_mentioned(skill_text)
    assert kinds
    assert kinds <= set(ERROR_KINDS)


def test_skill_frontmatter_names_appmem_with_a_description(skill_text: str) -> None:
    frontmatter = _skill_frontmatter(skill_text)
    assert frontmatter.get("name") == "appmem"
    assert frontmatter.get("description")
