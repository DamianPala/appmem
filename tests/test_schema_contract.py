"""Checks that the argparse parsers and the introspection output actually
agree with each other, and that every output schema stays inside the
restricted JSON Schema keyword set the standard allows.

This is what makes the descriptors in `schema.py` trustworthy: a reader who
only ever looks at `appmem schema` must see exactly what the parser accepts.
"""

from __future__ import annotations

import argparse
from typing import Any, cast

import pytest

from appmem import schema
from appmem.cli import _build_parser  # pyright: ignore[reportPrivateUsage]

_ALLOWED_SCHEMA_KEYS = {"type", "enum", "properties", "required", "items"}


def _sub_parser(name: str) -> argparse.ArgumentParser:
    root = _build_parser()
    action = next(  # pyright: ignore[reportUnknownVariableType]
        a
        for a in root._actions  # pyright: ignore[reportPrivateUsage, reportUnknownArgumentType]
        if isinstance(a, argparse._SubParsersAction)  # pyright: ignore[reportPrivateUsage]
    )
    subparsers_action = cast(
        "argparse._SubParsersAction[argparse.ArgumentParser]",  # pyright: ignore[reportPrivateUsage]
        action,
    )
    parser = subparsers_action.choices[name]
    assert isinstance(parser, argparse.ArgumentParser)
    return parser


def _action_by_dest(parser: argparse.ArgumentParser, dest: str) -> argparse.Action:
    return next(a for a in parser._actions if a.dest == dest)  # pyright: ignore[reportPrivateUsage]


def _assert_flag_matches(
    parser: argparse.ArgumentParser, flag: schema.Flag, *, check_default: bool = True
) -> None:
    action = _action_by_dest(parser, flag.name)
    option_strings = {s.lstrip("-") for s in action.option_strings}
    assert flag.name in option_strings
    for alias in flag.aliases:
        assert alias in option_strings
    if check_default and action.default is not argparse.SUPPRESS:
        assert action.default == flag.default
    if flag.enum is not None:
        assert tuple(action.choices) == flag.enum  # pyright: ignore[reportArgumentType]


def test_root_parser_matches_root_descriptors() -> None:
    root = _build_parser()
    # `-i/--interval` parses to `None` ("not given") at the argparse level, so
    # a named command can tell "not given" apart from the descriptor's real
    # default of 1, which only applies once the live view actually starts.
    _assert_flag_matches(root, schema.ROOT_INTERVAL, check_default=False)
    _assert_flag_matches(root, schema.ROOT_SYSTEM)
    _assert_flag_matches(root, schema.JSON_FLAG)


def test_snapshot_parser_matches_snapshot_descriptors() -> None:
    parser = _sub_parser("snapshot")
    _assert_flag_matches(parser, schema.SNAPSHOT_SYSTEM)
    _assert_flag_matches(parser, schema.SNAPSHOT_LIMIT)
    _assert_flag_matches(parser, schema.JSON_FLAG)


def test_app_parser_matches_app_descriptors() -> None:
    parser = _sub_parser("app")
    name_action = _action_by_dest(parser, "name")
    assert name_action.required
    _assert_flag_matches(parser, schema.APP_SCOPE)
    _assert_flag_matches(parser, schema.APP_LIMIT)
    _assert_flag_matches(parser, schema.JSON_FLAG)


def _parser_inputs(parser: argparse.ArgumentParser) -> set[str]:
    """Every input the parser accepts, minus --help/--version (the index leaves
    those out by rule) and the subcommand selector."""
    skipped = {"--help", "--version"}
    return {
        action.dest
        for action in parser._actions  # pyright: ignore[reportPrivateUsage]
        if action.dest != "command" and not skipped & set(action.option_strings)
    }


@pytest.mark.parametrize(
    ("command", "descriptors"),
    [
        (None, [schema.ROOT_INTERVAL, schema.ROOT_SYSTEM]),
        ("snapshot", [schema.SNAPSHOT_SYSTEM, schema.SNAPSHOT_LIMIT]),
        ("app", [schema.APP_NAME, schema.APP_SCOPE, schema.APP_LIMIT]),
    ],
)
def test_parser_accepts_nothing_the_descriptors_do_not_publish(
    command: str | None, descriptors: list[schema.Arg]
) -> None:
    parser = _build_parser() if command is None else _sub_parser(command)
    published = {d.name for d in descriptors} | {schema.JSON_FLAG.name}
    assert _parser_inputs(parser) == published


def test_app_scope_choices_match_the_enum_descriptor() -> None:
    parser = _sub_parser("app")
    action = _action_by_dest(parser, "scope")
    assert tuple(action.choices) == schema.APP_SCOPE.enum  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("value", "should_fail"),
    [("0", True), ("-1", True), ("x", True), ("1", False), ("50", False)],
)
def test_snapshot_limit_validator_matches_the_descriptor_rule(
    value: str, should_fail: bool
) -> None:
    parser = _sub_parser("snapshot")
    if should_fail:
        with pytest.raises(SystemExit):
            parser.parse_args(["--limit", value])
    else:
        args = parser.parse_args(["--limit", value])
        assert args.limit == int(value)


def _walk_schema(value: object) -> None:
    if not isinstance(value, dict):
        return
    schema_value = cast("dict[str, Any]", value)
    extra = set(schema_value) - _ALLOWED_SCHEMA_KEYS
    assert not extra, f"schema uses a keyword outside the allowed set: {extra}"
    value_type = schema_value.get("type")
    if value_type == "object" or (isinstance(value_type, list) and "object" in value_type):
        assert "properties" in schema_value
        assert "required" in schema_value
    if value_type == "array" or (isinstance(value_type, list) and "array" in value_type):
        assert "items" in schema_value
    for child in schema_value.get("properties", {}).values():
        _walk_schema(child)
    items = schema_value.get("items")
    if items is not None:
        _walk_schema(items)


@pytest.mark.parametrize("output", [schema.SNAPSHOT_OUTPUT, schema.APP_OUTPUT])
def test_output_schemas_use_only_the_allowed_keywords(output: dict[str, Any]) -> None:
    _walk_schema(output)
