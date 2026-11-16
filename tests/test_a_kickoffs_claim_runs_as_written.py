"""A kickoff's claim runs as it is written, for every agent (SMOKE12, F4).

A Codex agent's id is known only once it starts, so its kickoff, built at the spawn, named
its label: ``task claim <id> --as coder1``. Nothing resolved a label, and the CLI reported
the session it could not find as the TASK: "nothing matches '<task id>'", 2 of 2 times on
real Codex 0.166.0. A task's agent's label is ``coder-<task>``, longer than a short id, and
the kickoff cut it to one (``--as coder-01``). Now ``--as`` takes a live fleet label of the
caller's project as that agent's current session, the kickoff names the label whole, and a
session that is not there is said as one.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.agent_adapters import register, unregister
from aisquare.core.store import AmbiguousIdError, store_session
from aisquare.models import ProjectInfo, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests.fake_agent import FakeAgentAdapter
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import (  # noqa: F401 - fixture, by name
    THREAD_ONE,
    Codex,
    _claimed_by,
    _row,
    codex_agent,
)
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _first_lines,
    claude_on_path,
    project,
    repo,
    tmux,
)

CLAIM = re.compile(r"`aisquare (task claim [^`]+)`")


def _claim_as_written(kickoff: str) -> list[str]:
    """The claim the kickoff tells its agent to run, as an argv: its own words, split as a
    shell splits them."""
    found = CLAIM.search(kickoff)
    assert found is not None, kickoff
    return shlex.split(found.group(1))


def _run(argv: list[str]) -> Any:
    return CliRunner().invoke(app, argv)


class _FirstWordsOnItsCommandLine(FakeAgentAdapter):
    """The fake agent, given its first message as its argument (as Claude Code and Codex
    are), so its spawn hands the kickoff over without waiting for a screen to type into."""

    capabilities = replace(FakeAgentAdapter.capabilities, positional_prompt=True)


@pytest.fixture
def fake_agent() -> Iterator[FakeAgentAdapter]:
    adapter = _FirstWordsOnItsCommandLine()
    register(adapter)
    try:
        yield adapter
    finally:
        unregister(adapter.id)


def test_a_codex_kickoffs_claim_runs_as_written_by_its_whole_label(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kickoff names the agent's label whole (``coder-<task>``, longer than a short id),
    and the claim it names runs once the agent's first hook has bound its session: the task
    is that session's."""
    codex, task = codex_agent
    [kickoff] = _first_lines(tmux)
    argv = _claim_as_written(kickoff)
    label = codex.agent.label
    assert len(label) > 8, f"{label}: longer than a short id, as a task agent's is"
    assert argv == ["task", "claim", task.id, "--as", label], kickoff

    codex.start(THREAD_ONE)
    monkeypatch.chdir(codex.project.root)
    result = _run(argv)

    assert result.exit_code == 0, result.output
    session = _row(codex.agent.id).session_id
    assert session == codex.board_id(THREAD_ONE) and _claimed_by(task.id) == session


def test_a_codex_kickoff_built_on_its_session_claims_as_written_by_its_id(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built once the session exists (as a Claude Code agent's always is), the kickoff names
    its short id, and that claim runs as written too."""
    codex, task = codex_agent
    codex.start(THREAD_ONE)
    session = _row(codex.agent.id).session_id
    assert session is not None
    with store_session() as store:
        statuses = store.task_statuses(task.project_id)
    kickoff = team_service.kickoff_prompt(
        task, role="coder", label=codex.agent.label, session_id=session, statuses=statuses
    )
    argv = _claim_as_written(kickoff)
    assert argv[-2:] == ["--as", team_service.short_id(session)]

    monkeypatch.chdir(codex.project.root)
    result = _run(argv)

    assert result.exit_code == 0, result.output
    assert _claimed_by(task.id) == session


def test_a_claude_code_kickoffs_claim_runs_as_written(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, _ = team_service.add_task("wire auth", role="coder", cwd=project.root)
    agent = fleet_service.spawn(project, "coder", worktree=False, task_id=task.id).agent
    assert agent.session_id is not None, "Claude Code's id is minted at the spawn"
    [kickoff] = _first_lines(tmux)
    argv = _claim_as_written(kickoff)
    assert argv[-2:] == ["--as", team_service.short_id(agent.session_id)]
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    team_service.hook_session_start(agent.session_id, project.root, "startup")
    monkeypatch.chdir(project.root)

    result = _run(argv)

    assert result.exit_code == 0, result.output
    assert _claimed_by(task.id) == agent.session_id


def test_a_synthetic_agents_kickoffs_claim_runs_as_written(
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    fake_agent: FakeAgentAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent that learns its id at its start, as Codex does, through no code of Codex's:
    its kickoff's claim runs by its label once its first hook has bound a session."""
    from tests import fakebin

    bin_dir = tmp_path / "bin"
    fakebin.executable_fake(bin_dir, fake_agent.binary, windows="set /p line=", posix="read line")
    fakebin.prepend_to_path(bin_dir, monkeypatch)
    task, _ = team_service.add_task("wire auth", role="coder", cwd=project.root)
    agent = fleet_service.spawn(
        project, "coder", agent=fake_agent.id, worktree=False, task_id=task.id
    ).agent
    assert agent.session_id is None, "its id binds at its start"
    [kickoff] = _first_lines(tmux)
    argv = _claim_as_written(kickoff)
    assert argv[-2:] == ["--as", agent.label]
    native = "fake-session-0001"
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    team_service.hook_session_start(native, project.root, "startup", agent=fake_agent.id)
    monkeypatch.chdir(project.root)

    result = _run(argv)

    assert result.exit_code == 0, result.output
    bound = _row(agent.id).session_id
    assert bound is not None and _claimed_by(task.id) == bound


# --- the error names the session --------------------------------------------------------


def test_a_session_that_is_not_there_is_said_as_one_never_as_the_task(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, _ = team_service.add_task("wire auth", role="coder", cwd=project.root)
    monkeypatch.chdir(project.root)

    result = _run(["task", "claim", task.id, "--as", "nobody1"])

    assert result.exit_code != 0
    assert "no session matches 'nobody1'" in result.output, result.output
    assert task.id not in result.output, "the task is there: never said missing"
    said = json.loads(_run(["--json", "task", "claim", task.id, "--as", "nobody1"]).output)
    assert said == {"error": "not_found", "ref": "nobody1"}


def test_a_label_whose_agent_has_bound_no_session_yet_says_so(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, task = codex_agent
    monkeypatch.chdir(codex.project.root)

    result = _run(["task", "claim", task.id, "--as", codex.agent.label])

    assert result.exit_code != 0
    assert f"no session matches '{codex.agent.label}'" in result.output, result.output
    assert "has no session yet: its first hook has not run" in result.output
    assert _claimed_by(task.id) is None


# --- what a label names -----------------------------------------------------------------


def test_a_label_names_the_agents_current_session_from_its_project_alone(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """From the agent's own window (its row's project) and from its project's directory, a
    label is the agent's session; from another project's directory, with no row of its own
    there, it names nothing: labels are unique among one project's live agents only."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    session = _row(codex.agent.id).session_id
    label = codex.agent.label

    def named() -> str:
        with store_session() as store:
            found = team_service._resolve_session(store, label)
        assert found is not None
        return found.id

    monkeypatch.chdir(tmp_path)  # its own row answers, wherever it runs
    assert named() == session
    monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    monkeypatch.chdir(codex.project.root)
    assert named() == session
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(team_service.NoSuchSession):
        named()


def test_a_label_that_is_also_another_sessions_id_prefix_is_ambiguous(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never one of the two picked: acted as the prefix's session, a kickoff's claim would
    have been made as somebody else."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    label = codex.agent.label
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=f"{label}-0000-0000-0000",
                project_id=codex.project.id,
                started_at=now,
                last_seen_at=now,
            )
        )
        with pytest.raises(AmbiguousIdError):
            team_service._resolve_session(store, label)
