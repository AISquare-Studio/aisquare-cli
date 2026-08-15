"""#24: the five global output flags are accepted anywhere on the command line."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.global_flags import INJECTED_MARK, SHARED_FLAG_DECLARATIONS
from aisquare.core.state import get_state
from aisquare.services import team as team_service
from tests.cli_tree import all_nodes


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture(autouse=True)
def deterministic_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin rendering so output-content asserts are environment-independent.

    GitHub Actions force-enables rich color (typer freezes that off
    ``GITHUB_ACTIONS`` at import) and renders help at 80 columns — ANSI codes
    land INSIDE option tokens and shred raw substring asserts. ``NO_COLOR``
    is read at render time, so it neutralises the forced color wherever the
    suite runs; the width pins cover consoles that consult them. Wrapping is
    handled at the assert site (whitespace-collapse), not here, because
    typer's width constant is frozen at import time.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERMINAL_WIDTH", "200")


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Rendered output flattened for content asserts: no ANSI, no wrapping.

    ``NO_COLOR`` alone is not enough — rich keeps non-color attributes
    (bold/dim) under it, and typer's highlighter styles the leading ``-`` of
    an option as its own span, so escape codes land INSIDE tokens like
    ``--json`` on forced-color environments (GitHub Actions).
    """
    return " ".join(_ANSI.sub("", text).split())


def _declarations(command: Any) -> set[str]:
    declared: set[str] = set()
    for parameter in command.params:
        declared |= set(getattr(parameter, "opts", ()))
        declared |= set(getattr(parameter, "secondary_opts", ()))
    return declared


def test_every_node_exposes_the_five_globals() -> None:
    for path, command in all_nodes():
        if getattr(command, "context_settings", {}).get("ignore_unknown_options"):
            # Arg-forwarding commands (launch) are the documented exception:
            # flags after them belong to the exec'd program, so the globals
            # apply only BEFORE the subcommand — injection would parse them
            # out of the forwarded argv (test_launch pins that behavior).
            continue
        missing = set(SHARED_FLAG_DECLARATIONS) - _declarations(command)
        assert not missing, f"{' '.join(path) or '<root>'} lacks {sorted(missing)}"


def test_no_command_defines_a_colliding_local_param() -> None:
    # Guards future commands: below the root (whose callback owns the
    # canonical five) the shared declarations may only belong to the
    # injected globals, never to a command's own option.
    shared = set(SHARED_FLAG_DECLARATIONS)
    for path, command in all_nodes():
        if not path:
            continue
        for parameter in command.params:
            opts = set(getattr(parameter, "opts", ())) | set(
                getattr(parameter, "secondary_opts", ())
            )
            if opts & shared:
                assert getattr(parameter, INJECTED_MARK, False), (
                    f"{' '.join(path)}: local param {sorted(opts)} collides with a global flag"
                )


def test_globals_appear_in_leaf_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["task", "show", "-h"])
    assert result.exit_code == 0, result.output
    flat = _plain(result.output)
    for declaration in ("--json", "--verbose", "--quiet", "--no-color", "--profile"):
        assert declaration in flat, declaration


def test_help_renders_under_forced_color_at_80_cols(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CI rendering path itself (color on, narrow width) must not crash;
    # content assertions live in the NO_COLOR test above.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, ["task", "show", "-h"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_json_means_the_same_before_and_after_the_subcommand(runner: CliRunner) -> None:
    team_service.activate()
    task, _ = team_service.add_task("wire auth flow")
    before = runner.invoke(app, ["--json", "task", "show", task.id])
    after = runner.invoke(app, ["task", "show", task.id, "--json"])
    assert before.exit_code == 0, before.output
    assert after.exit_code == 0, after.output
    assert json.loads(before.stdout) == json.loads(after.stdout)


def test_boolean_flags_merge_after_the_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "-q", "-v"])
    assert result.exit_code == 0, result.output
    assert get_state().quiet is True
    assert get_state().verbose is True


def test_flag_on_a_nested_group_counts(runner: CliRunner) -> None:
    team_service.activate()
    result = runner.invoke(app, ["task", "--json", "list"])
    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.stdout), list)


def test_duplicate_flag_at_root_and_leaf_is_idempotent(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "status", "--json"])
    assert result.exit_code == 0, result.output
    assert get_state().json_output is True
    json.loads(result.stdout)  # still exactly one JSON document on stdout


def test_profile_after_subcommand_takes_effect(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status", "--profile", "alt"])
    assert result.exit_code == 0, result.output
    assert get_state().profile == "alt"


def test_profile_last_occurrence_wins(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--profile", "one", "status", "--profile", "two"])
    assert result.exit_code == 0, result.output
    assert get_state().profile == "two"


def test_quoted_positional_containing_json_is_not_a_flag(runner: CliRunner, work_dir: Path) -> None:
    team_service.activate()
    payload = json.dumps(
        {"cwd": str(work_dir), "session_id": "cccc3333-0000-0000-0000-000000000000"}
    )
    runner.invoke(app, ["hook", "session-start"], input=payload)

    with_flag = runner.invoke(
        app, ["note", "prefer --json for machine output", "--as", "cccc3333", "--json"]
    )
    assert with_flag.exit_code == 0, with_flag.output
    envelope = json.loads(with_flag.stdout)
    assert envelope["payload"]["text"] == "prefer --json for machine output"

    without_flag = runner.invoke(app, ["note", "again: --json stays text", "--as", "cccc3333"])
    assert without_flag.exit_code == 0, without_flag.output
    assert get_state().json_output is False  # the quoted value never parsed as a flag
    assert "--json stays text" in without_flag.output
