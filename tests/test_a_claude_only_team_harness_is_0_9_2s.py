"""``aisquare team harness`` on a Claude-only machine is 0.9.2's, byte for byte (C1's audit).

#113 rewrote the matrix's text line for every role:
``coder      claude-code [default] sonnet→opus effort=high → sonnet [optimistic]
bin=claude [default]``. A Claude-only user gained the agent and an always-on ``bin=``, and
lost 0.9.2's effort offset ``(+1)`` and the ``+Nargs`` count of a role's bound arguments.
The column alignment went too. None of it was in [0.10.0] (the manager's ruling, seq 11720).

The agent (``agent=claude-code [default]``) now comes after the model, and only with
accounts of more than one agent listed: the test ``aisquare accounts`` and the Accounts
page name agents by. :data:`HARNESS_0_9_2` and :data:`BOUND_0_9_2` are what 0.9.2
(6a77e650, under typer 0.28.0) printed in a scratch home: with nothing bound, and with the
three bindings :func:`_bind` makes (the live diff, R14 rig ``harness-diff.sh``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core

HARNESS_0_9_2 = """\
base effort: high (default)
planner    fable→opus→sonnet    effort=high      ( 0) → fable [optimistic]
coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
runner     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
validator  fable→opus           effort=xhigh     (+1) → fable [optimistic]
manager    fable→opus→sonnet    effort=high      ( 0) → fable [optimistic]
tester     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
reviewer   sonnet→opus          effort=high      ( 0) → sonnet [optimistic]
ui-tester  sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  role_args=--chrome
Resolution shown without probing — `aisquare team spawn <role>` verifies live.
"""

BOUND_0_9_2 = {
    "coder": (
        "coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]"
        "  bin=claude2 [config]  env=FOO+2args"
    ),
    "tester": (
        "tester     sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  env=GH_TOKEN"
    ),
    "reviewer": (
        "reviewer   sonnet→opus          effort=high      ( 0) → sonnet [optimistic]  env=+1args"
    ),
}


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    # The matrix as a terminal of any width reads it: Rich never folds a role's line.
    monkeypatch.setenv("COLUMNS", "250")
    for name in ("AISQUARE_MODEL_CODER", "AISQUARE_EFFORT_CODER", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return CliRunner()


def _bind(runner: CliRunner, tmp_path: Path) -> None:
    token = tmp_path / "tok"
    token.write_text("tok\n")
    coder = ["--bin", "claude2", "--env", "FOO=1", "--arg", "--verbose", "--arg", "--debug"]
    for argv in (
        ["team", "bind", "coder", *coder],
        ["team", "bind", "tester", "--env-file", f"GH_TOKEN={token}"],
        ["team", "bind", "reviewer", "--arg", "--x"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.output


def test_a_claude_only_machines_matrix_is_0_9_2s(runner: CliRunner) -> None:
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    assert result.stdout == HARNESS_0_9_2


def test_a_claude_only_machines_bound_roles_read_as_0_9_2s(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The offset, ``bin=`` for a binary that is not the default, and ``env=<keys>+<N>args``,
    as 0.9.2 printed them."""
    _bind(runner, tmp_path)
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line[:1] != " "}
    for role, line in BOUND_0_9_2.items():
        assert lines[role] == line, result.stdout
    assert lines["planner"] == HARNESS_0_9_2.splitlines()[1], "an unbound role as before"


def test_beside_another_agents_accounts_each_role_says_its_agent_after_the_model(
    runner: CliRunner, tmp_path: Path
) -> None:
    core.create_account("codex")
    _bind(runner, tmp_path)
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.stdout.splitlines() if line[:1] != " "}
    assert lines["coder"] == (
        "coder      sonnet→opus          effort=high      ( 0) → sonnet [optimistic]"
        "  agent=claude-code [default]  bin=claude2 [config]  env=FOO+2args"
    ), result.stdout
    assert lines["validator"].endswith("(+1) → fable [optimistic]  agent=claude-code [default]")
    assert result.stdout.endswith(
        "Resolution shown without probing; native defaults stay with the selected agent.\n"
    )
