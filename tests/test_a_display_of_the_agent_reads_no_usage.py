"""A command that only DISPLAYS which agent a role launches reads no usage (review of #14, M2
and tester-14's follow-up).

The launch's agent rung (``agent_launch.resolve``) measures accounts when several agents have
usable ones: up to ten seconds on a cold cache, and on a different footing from a display, which
asks the order alone (``measure=False``). Eight call sites pass it: ``doctor`` (five rows),
``team harness``, ``team bind --account`` and the hints of ``accounts add`` and ``accounts default
--role``. Only ``resolve(measure=False)`` itself was pinned, so any of them could start reading
usage again without a test noticing. Here the pick's entry point records every read it is asked
for, with both agents' accounts present and a pick that would measure them, and each command is
run.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import agent_launch
from aisquare.services import claude_accounts as service
from aisquare.services import team as team_service
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    CLAUDE,
    CODEX,
    _claude,
    _codex,
    _usage,
    machine,
)


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch, machine: Path) -> dict[str, Any]:  # noqa: F811
    """Claude Code's and Codex's accounts, Codex on top; every pick that would measure them
    is recorded in ``picks`` (and answers as the order alone would), every usage read in
    ``reads``."""
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    reads = _usage(monkeypatch, {codex.slot: 99.0, claude.slot: 5.0})
    picks: list[str] = []
    real = service.headroom_decision

    def decision(*args: Any, **kwargs: Any) -> Any:
        picks.append("a headroom pick measured the accounts")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "headroom_decision", decision)
    # The control: the very pick a launch makes does measure, so a display that dropped
    # `measure=False` would be seen.
    assert agent_launch.resolve("coder").adapter.id == CLAUDE
    assert picks and reads, "the control: a launch measures"
    picks.clear()
    reads.clear()
    return {"claude": claude, "codex": codex, "picks": picks, "reads": reads}


def _nothing_was_measured(seen: dict[str, Any]) -> None:
    assert seen["picks"] == [] and seen["reads"] == [], seen


def test_team_harness_shows_the_order_s_agent_and_measures_nothing(
    watched: dict[str, Any], runner: CliRunner
) -> None:
    team_service.activate(Path.cwd())  # the matrix needs a team

    result = runner.invoke(app, ["--json", "team", "harness"])

    assert result.exit_code == 0, result.output
    _nothing_was_measured(watched)


def test_team_bind_an_account_resolves_its_roles_agent_from_the_order_alone(
    watched: dict[str, Any], runner: CliRunner
) -> None:
    team_service.activate(Path.cwd())

    result = runner.invoke(app, ["team", "bind", "coder", "--account", str(watched["codex"].slot)])

    assert result.exit_code == 0, result.output
    _nothing_was_measured(watched)


def test_accounts_default_for_a_role_and_the_hint_of_accounts_add_ask_the_order_alone(
    watched: dict[str, Any], runner: CliRunner
) -> None:
    accounts_cli = import_module("aisquare.cli.accounts")
    team_cli = import_module("aisquare.cli.team")

    result = runner.invoke(
        app, ["accounts", "default", "--role", "coder", str(watched["codex"].slot)]
    )
    assert result.exit_code == 0, result.output
    assert accounts_cli._role_agent("coder", str(watched["claude"].slot)) == CLAUDE
    assert accounts_cli._role_agent("coder") == CODEX, "the order's agent"
    assert team_cli._bound_agent("coder", None) == CODEX
    assert team_cli._bound_agent("coder", "claude-code") == "claude-code"

    _nothing_was_measured(watched)


def test_the_doctor_reads_no_usage_for_the_agent_any_row_names(
    watched: dict[str, Any], runner: CliRunner, tmp_path: Path
) -> None:
    team_service.activate(Path.cwd())
    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exit_code in (0, 1), result.output
    # Its account rows read an offline agent's own figure; what it never does is the launch's
    # pick across agents, which measures every candidate's.
    assert watched["picks"] == [], watched
