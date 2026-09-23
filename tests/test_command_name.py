"""Tests for `appmem.command_name.command_display_name`.

Command lines here are made up: no real argv captured from this machine, per
the rule that process arguments can hold secrets.
"""

from __future__ import annotations

from appmem.command_name import command_display_name


def test_unknown_basename_is_left_alone() -> None:
    assert command_display_name("ghostty", ["--foo"]) is None
    assert command_display_name("bash", []) is None


# --- node/bun/deno: script basename -----------------------------------------


def test_node_shows_the_script_basename() -> None:
    assert command_display_name("node", ["/home/user/.ccs/mcp/websearch-server.cjs"]) == (
        "node:websearch-server.cjs"
    )


def test_node_skips_known_safe_flags_before_the_script() -> None:
    assert command_display_name("node", ["--no-warnings", "--enable-source-maps", "/a/b/x.js"]) == (
        "node:x.js"
    )


def test_node_never_includes_arguments_after_the_script() -> None:
    name = command_display_name("node", ["/a/b/server.cjs", "--port", "1234", "--token", "SECRET"])
    assert name == "node:server.cjs"
    assert "SECRET" not in (name or "")
    assert "1234" not in (name or "")


def test_node_generic_basename_falls_back_to_the_package_directory() -> None:
    path = "/home/user/.npm/_npx/abc123/node_modules/mcp-remote/dist/index.js"
    assert command_display_name("node", [path]) == "node:mcp-remote"


def test_node_generic_basename_keeps_scoped_package_name() -> None:
    path = "/opt/app/node_modules/@openai/codex/bin/cli.js"
    assert command_display_name("node", [path]) == "node:@openai/codex"


def test_node_generic_basename_without_node_modules_uses_nearest_meaningful_parent() -> None:
    assert command_display_name("node", ["/opt/myapp/dist/main.js"]) == "node:myapp"


def test_bun_and_deno_use_the_run_verb_skip() -> None:
    assert command_display_name("bun", ["run", "/a/build.ts"]) == "bun:build.ts"
    assert command_display_name("deno", ["run", "/a/server.ts"]) == "deno:server.ts"


# --- npm/npx -----------------------------------------------------------------


def test_npx_takes_the_package_name_directly() -> None:
    assert command_display_name("npx", ["mcp-remote"]) == "npx:mcp-remote"


def test_npx_skips_the_yes_flag() -> None:
    assert command_display_name("npx", ["-y", "pkg"]) == "npx:pkg"
    assert command_display_name("npx", ["--yes", "pkg"]) == "npx:pkg"


def test_npm_exec_skips_the_yes_flag() -> None:
    assert command_display_name("npm", ["exec", "-y", "pkg"]) == "npm:pkg"
    assert command_display_name("npm", ["exec", "--yes", "pkg"]) == "npm:pkg"


def test_yes_flag_does_not_let_inline_code_through() -> None:
    assert command_display_name("npx", ["-y", "-c", "FAKE-TOKEN"]) == "npx"
    assert command_display_name("npx", ["-y", "--call", "FAKE-TOKEN"]) == "npx"
    assert command_display_name("npm", ["exec", "--yes", "-c", "FAKE-TOKEN"]) == "npm"


def test_npm_skips_the_exec_verb_and_strips_the_version() -> None:
    assert command_display_name("npm", ["exec", "@scope/tool@latest"]) == "npm:@scope/tool"


def test_npm_skips_the_run_verb() -> None:
    assert command_display_name("npm", ["run", "build"]) == "npm:build"


# --- uv/uvx --------------------------------------------------------------------


def test_uvx_takes_the_package_name_directly() -> None:
    assert command_display_name("uvx", ["ruff"]) == "uvx:ruff"


def test_uv_skips_the_run_verb() -> None:
    assert command_display_name("uv", ["run", "backup.py"]) == "uv:backup.py"


def test_uv_skips_tool_run_verbs_in_sequence() -> None:
    assert command_display_name("uv", ["tool", "run", "some-tool"]) == "uv:some-tool"


# --- python --------------------------------------------------------------------


def test_python_shows_the_script_basename() -> None:
    assert command_display_name("python3", ["/opt/tools/backup.py"]) == "python3:backup.py"


def test_python_version_suffix_is_recognized() -> None:
    assert command_display_name("python3.12", ["backup.py"]) == "python3.12:backup.py"


def test_python_dash_m_uses_the_module_name() -> None:
    assert command_display_name("python", ["-m", "http.server"]) == "python:http.server"


def test_python_dash_m_skips_earlier_safe_flags() -> None:
    assert command_display_name("python", ["-O", "-m", "pkg"]) == "python:pkg"


def test_python_dash_m_value_that_is_not_a_module_name_gives_the_bare_interpreter() -> None:
    assert command_display_name("python", ["-m", "../FAKE/TOKEN"]) == "python"


def test_python_generic_main_basename_falls_back_to_the_package_directory() -> None:
    path = "/home/user/.local/pipx/venvs/mytool/lib/python3.12/site-packages/mytool/__main__.py"
    assert command_display_name("python3", [path]) == "python3:mytool"


def test_python_with_no_positional_argument_is_the_bare_interpreter() -> None:
    assert command_display_name("python3", ["--version"]) == "python3"
    assert command_display_name("python3", []) == "python3"


# --- allowlist: an option outside the known-safe set gives the bare
# interpreter, never a value or an inline-code fragment -------------------------
# One test per hostile input a review found leaking through the old table-of-
# dangerous-options design.


def test_node_dash_e_inline_code_gives_the_bare_interpreter() -> None:
    name = command_display_name("node", ["-e", "console.log(TOKEN_abc)"])
    assert name == "node"
    assert "TOKEN" not in (name or "")


def test_node_dash_dash_eval_and_dash_p_give_the_bare_interpreter() -> None:
    assert command_display_name("node", ["--eval", "TOKEN_abc"]) == "node"
    assert command_display_name("node", ["-p", "TOKEN_abc"]) == "node"


def test_bun_dash_e_inline_code_gives_the_bare_interpreter() -> None:
    assert command_display_name("bun", ["-e", "TOKEN"]) == "bun"


def test_python_bundled_dash_capital_s_lowercase_c_gives_the_bare_interpreter() -> None:
    # `-Sc "..."` bundles `-S` (a safe flag) with `-c` (inline code); the
    # bundle as a whole is not in the safe-flag set, so it never gets to run
    # the inline code through the module at all.
    assert command_display_name("python3", ["-Sc", "token='abc'"]) == "python3"


def test_python_bundled_dash_capital_i_lowercase_c_gives_the_bare_interpreter() -> None:
    assert command_display_name("python3", ["-Ic", "token=abc/def"]) == "python3"


def test_npx_option_value_never_becomes_the_target() -> None:
    name = command_display_name("npx", ["--token", "abc", "pkg"])
    assert name == "npx"
    assert "abc" not in (name or "")


def test_node_dash_dash_require_and_dash_r_option_value_gives_the_bare_interpreter() -> None:
    assert command_display_name("node", ["--require", "/x/secret.js", "app.js"]) == "node"
    assert command_display_name("node", ["-r", "/x/secret.js", "app.js"]) == "node"


def test_node_dash_dash_import_option_value_gives_the_bare_interpreter() -> None:
    assert command_display_name("node", ["--import", "tsx", "app.ts"]) == "node"


def test_python_dash_capital_w_and_dash_capital_x_option_values_give_the_bare_interpreter() -> None:
    assert command_display_name("python3", ["-W", "ignore", "app.py"]) == "python3"
    assert command_display_name("python3", ["-X", "importtime", "app.py"]) == "python3"


def test_npm_dash_dash_prefix_option_value_gives_the_bare_interpreter() -> None:
    name = command_display_name("npm", ["--prefix", "/home/u/secretproj", "run", "dev"])
    assert name == "npm"
    assert "secretproj" not in (name or "")


def test_uv_dash_dash_with_option_value_gives_the_bare_interpreter() -> None:
    name = command_display_name("uv", ["run", "--with", "extra-pkg", "script.py"])
    assert name == "uv"
    assert "extra-pkg" not in (name or "")


def test_uv_dash_dash_directory_option_value_gives_the_bare_interpreter() -> None:
    # A deliberately accepted loss: --directory isn't in uv's safe-flag set,
    # so the label is lost even though the value itself isn't a secret here.
    assert command_display_name("uv", ["run", "--directory", "/home/u/proj", "script.py"]) == "uv"


def test_deno_dash_dash_allow_net_option_gives_the_bare_interpreter() -> None:
    name = command_display_name("deno", ["run", "--allow-net", "https://host/x.ts?token=abc"])
    assert name == "deno"
    assert "token" not in (name or "")


def test_npx_url_with_userinfo_gives_the_bare_interpreter() -> None:
    name = command_display_name("npx", ["https://user:TOKEN@host"])
    assert name == "npx"
    assert "TOKEN" not in (name or "")


def test_npx_scoped_spec_with_path_traversal_gives_the_bare_interpreter() -> None:
    name = command_display_name("npx", ["@scope/pkg/../../etc/secret"])
    assert name == "npx"
    assert "secret" not in (name or "")


# --- script-path target validation, beyond what an unsafe option already
# blocks --------------------------------------------------------------------


def test_query_string_glued_onto_a_script_path_gives_the_bare_interpreter() -> None:
    name = command_display_name("node", ["/a/x.ts?token=abc"])
    assert name == "node"
    assert "token" not in (name or "")


def test_userinfo_leftover_in_a_script_basename_gives_the_bare_interpreter() -> None:
    # "@" passes the final charset gate, so only the basename check stops it.
    assert command_display_name("deno", ["run", "/a/FAKE-TOKEN@host.ts"]) == "deno"


def test_control_character_in_the_script_path_gives_the_bare_interpreter() -> None:
    assert command_display_name("node", ["/a/evil\x1b[2Jname.js"]) == "node"


def test_whitespace_in_the_script_path_gives_the_bare_interpreter() -> None:
    assert command_display_name("node", ["/a/evil name.js"]) == "node"


def test_bare_url_script_target_gives_the_bare_interpreter() -> None:
    assert command_display_name("node", ["https://host/app.js"]) == "node"


# --- final label charset and length cap, defense in depth ----------------------


def test_disallowed_character_in_a_resolved_package_name_gives_the_bare_interpreter() -> None:
    # A made-up scope containing "!" would never appear on a real system, but
    # the final charset gate must still catch it rather than trust the
    # node_modules-resolution path unconditionally.
    path = "/opt/app/node_modules/@sc!ope/pkg/index.js"
    assert command_display_name("node", [path]) == "node"


def test_overlong_target_gives_the_bare_interpreter() -> None:
    long_name = "a" * 41 + ".js"
    assert command_display_name("node", [f"/opt/app/{long_name}"]) == "node"
