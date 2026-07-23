"""#24: the five global output flags are accepted anywhere on the command line."""

from __future__ import annotations

import json
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


def _declarations(command: Any) -> set[str]:
    declared: set[str] = set()
    for parameter in command.params:
        declared |= set(getattr(parameter, "opts", ()))
        declared |= set(getattr(parameter, "secondary_opts", ()))
    return declared


def test_every_node_exposes_the_five_globals() -> None:
    for path, command in all_nodes():
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
    for declaration in ("--json", "--verbose", "--quiet", "--no-color", "--profile"):
        assert declaration in result.output, declaration


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
