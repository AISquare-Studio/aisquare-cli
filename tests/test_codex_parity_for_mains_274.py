"""Codex parity for main's #274 features (SCOPE #113 274, lanes L4-L6), on the fleet's fake tmux.

- L4, the role-less agent: ``fleet spawn agent --agent codex`` is a plain Codex session on
  the board, as the Spawn dialog's *(none)* role is on Claude Code: launched as ``agent``,
  no worktree, no cycle, no ladder.
- L5, the launcher's prompt: a ``--task`` spawn with no ``--prompt`` gives Codex the task's
  kickoff (``team.kickoff_prompt``, #295's W1a) as the argument it takes its first prompt by
  (``positional_prompt``), from the file its launch reads, and types nothing into a pane its
  start dialogs may hold.
- L6, Restart all: a Codex coder that died of an outside SIGTERM is brought back, its
  hand-off on its command line, and a Codex manager restarted after it is told its dead team
  there too, with the command that brings each one back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import harness
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamTask
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import delivery
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import (  # noqa: F401 - fixtures, by name
    THREAD_ONE,
    Codex,
    _native,
    _rollout,
    codex_agent,
)
from tests.test_fleet_bulk import _project
from tests.test_fleet_bulk import tmux as bulk_tmux  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _add_task,
    _command,
    _flag,
    claude_on_path,
    project,
    repo,
    tmux,
)

KILLED = 143
"""An outside SIGTERM's exit status (#38's crash)."""


def _argument(tmux: FakeTmux, index: int = -1) -> str:  # noqa: F811
    """The first prompt a Codex window's launch hands the agent after its ``--``: the file the
    window's command names (``--first-prompt-file``, P0-W1a), never the command itself."""
    command = _command(tmux, index)
    path = _flag(command, delivery.FIRST_PROMPT_FLAG)
    assert path is not None, command
    text = Path(path).read_text(encoding="utf-8")
    assert text not in command, "never in the window's command"
    return text


# --- L4: the role-less agent ---------------------------------------------------------------


def test_no_role_spawns_a_plain_codex_from_the_cli(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    runner: CliRunner,
) -> None:
    result = runner.invoke(
        app, ["fleet", "spawn", fleet_service.PLAIN_ROLE, "--agent", "codex", "-P", project.id]
    )
    assert result.exit_code == 0, result.output
    [agent] = fleet_service.list_agents(project)
    row = agent.agent
    assert (row.role, row.label, row.agent) == ("agent", "agent-1", "codex")
    assert row.cwd == project.root  # no worktree, as on Claude Code
    assert _command(tmux)[5:7] == ["launch", "agent"]
    assert harness.role_cycle("agent", "abcd1234", fleet=lambda verb: verb) == []


def test_the_spawn_dialogs_no_role_names_no_agent() -> None:
    """The *(none)* choice and its note name no agent: the Agent field picks which one."""
    from aisquare.cli.ui.spawn import NO_ROLE_CHOICE, NO_ROLE_NOTE

    for text in (NO_ROLE_CHOICE, NO_ROLE_NOTE):
        assert "Claude" not in text and "Codex" not in text, text


# --- L5: the launcher's prompt ---------------------------------------------------------------


def test_a_codex_task_spawn_with_no_prompt_is_given_its_task_as_its_argument(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    task = _add_task(project, "Ship auth")
    receipt = fleet_service.spawn(project, "coder", agent="codex", worktree=False, task_id=task.id)
    assert receipt.prompt_typed == "yes" and tmux.typed == []
    assert f"ASSIGNED TO YOU: {task.id}" in _argument(tmux)


def test_a_codex_spawn_with_no_task_and_no_prompt_has_no_argument(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    receipt = fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    assert receipt.prompt_typed is None and tmux.typed == []
    native = _command(tmux)
    assert native[-2] != "--" or native.count("--") == 1, native
    assert delivery.FIRST_PROMPT_FLAG not in native


# --- L6: Restart all brings back a dead Codex -------------------------------------------------


@pytest.fixture
def codex_team(
    bulk_tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    tmp_path: Path,
) -> tuple[ProjectInfo, FleetAgent, FleetAgent]:
    """alpha: a Codex manager and a Codex coder, both running."""
    alpha = _project(tmp_path, "alpha")
    manager = fleet_service.spawn(alpha, "manager", agent="codex").agent
    coder = fleet_service.spawn(
        alpha, "coder", agent="codex", worktree=False, label="coder-auth", prompt=""
    ).agent
    return alpha, manager, coder


def test_restart_all_brings_back_a_killed_codex_coder(
    bulk_tmux: FakeTmux,  # noqa: F811
    codex_team: tuple[ProjectInfo, FleetAgent, FleetAgent],
) -> None:
    alpha, _manager, coder = codex_team
    bulk_tmux.die(coder.pane_id, KILLED)

    report = fleet_service.bulk("restart")

    [revived] = [r for r in report.receipts if r.agent.id == coder.id]
    assert revived.outcome == "done" and revived.dead, revived.why
    assert isinstance(revived.result, fleet_service.RestartReceipt)
    started = revived.result.started
    assert (started.label, started.agent) == ("coder-auth", "codex")
    with store_session() as store:
        live = {a.id for a in store.fleet_agents(alpha.id, live_only=True)}
    assert started.id in live
    assert report.summary() == "restarted 2 (1 running, 1 dead) · skipped 0 · failed 0"


def test_a_restarted_codex_manager_is_told_its_dead_team_on_its_command_line(
    bulk_tmux: FakeTmux,  # noqa: F811
    codex_team: tuple[ProjectInfo, FleetAgent, FleetAgent],
) -> None:
    alpha, _manager, coder = codex_team
    bulk_tmux.die(coder.pane_id, KILLED)
    bulk_tmux.typed.clear()

    started = fleet_service.restart(alpha, "manager").started

    assert started.agent == "codex"
    assert [t for t in bulk_tmux.typed if t[0] == started.pane_id and t[1] == "paste"] == []
    prompt = _argument(bulk_tmux)
    with store_session() as store:
        named = store.get_project(alpha.id)
    assert named is not None
    command = project_service.fleet_command("restart", named, "coder-auth")
    assert f"`{command}`" in prompt, prompt
    assert f"coder-auth (exited ({KILLED})" in prompt and "1 of your team died" in prompt


# --- SMOKE9 F1: a record with no slot keeps the plain home on a restart ---------------------


def test_a_claude_code_row_on_no_account_is_restarted_on_its_plain_home(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Main's bug too: a row spawned on "(this shell's)" (no slot) went back through the
    ladder, and the machine default moved it to slot 2. Restart all says it keeps the account.
    Asked for one, it still moves."""
    source = fleet_service.spawn(project, "coder", worktree=False).agent
    assert source.account_slot is None
    accounts_core.create_account()  # slot 2
    accounts_service.set_default("2")

    kept = fleet_service.restart(project, source.label).started
    plain = accounts_core.default_account().slot
    assert kept.account_slot == plain, "its own plain home, named so the window keeps it too"
    assert _command(tmux)[_command(tmux).index("--account") + 1] == str(plain)

    moved = fleet_service.restart(project, kept.label, account="2").started
    assert moved.account_slot == 2


def test_a_codex_row_on_no_account_is_restarted_on_its_home_and_resumes_its_thread(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """SMOKE9 F1 of #113: a Codex coder spawned on "(this shell's)" was moved by Restart all
    onto the managed slot the ladder picked, where its thread is not, and started fresh."""
    codex, _task = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    assert codex.agent.account_slot is None
    slot = accounts_core.create_account("codex")
    accounts_service.set_default(str(slot.slot))

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    plain = accounts_core.default_account(agent="codex").slot
    assert receipt.resumed is True and receipt.started.account_slot == plain
    native = _native(_command(tmux))
    at = native.index("resume")
    assert native[at : at + 2] == ["resume", THREAD_ONE]
