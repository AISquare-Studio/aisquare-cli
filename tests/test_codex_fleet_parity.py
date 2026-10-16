"""Fleet and session parity for a Codex agent, on the fleet's fake tmux.

Every hook here goes through Codex's own decoder (``services.agent_events.handle``) with
payloads shaped as Codex 0.162.1 sends them, measured against the real binary (the
loopback fixture in ``tests/test_codex_native.py`` covers the same paths live): a
``/clear`` fires NO ``SessionEnd`` for the thread it leaves, only ``SessionStart(source=
clear)`` for a new id, from the same process, under the same launch variables; and
Codex sets ``CODEX_THREAD_ID`` in its tools' shells, never in its own hooks, so a
``codex exec`` one of its tools runs fires every hook with the parent's thread there.

A Codex hook has no pid of the agent's own. The ``CLAUDE_PID`` a Codex hook finds is
whoever started the tmux server's — a Claude Code session's as often as not — so every
test here runs with a stale one in the environment, which must decide nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path, PureWindowsPath
from typing import Any, cast
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.agent import key_spellings
from aisquare.cli.ui.views.settings import launch_line
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import harness, launch_seal
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import AgentAdapter, prepare_store, store_dir
from aisquare.core.config import (
    REVIEWER_ARGS,
    AgentModelDefault,
    AgentModelSettings,
    FleetRoleSettings,
    FleetSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.keys import Drop, Translation, translate
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import (
    ClaudeAccount,
    FleetAgent,
    FleetAgentStatus,
    LaunchSpec,
    ProjectInfo,
    TeamSession,
    TeamTask,
)
from aisquare.services import agent_events, agent_launch, auto_mode, diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import fakebin
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeClock,
    FakeTmux,
    _command,
    _events,
    _flag,
    _timed_input,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)

#: Codex's thread ids are UUIDs (its hooks' ``session_id``, ``codex exec --json``'s
#: ``thread_id``, 0.162.1): what a resume and a queue put on its command line, and the
#: only shape they put there (review r3 of #113, finding 2).
THREAD_ONE = "019a2b3c-4d5e-7f60-8a1b-000000000001"
THREAD_TWO = "019a2b3c-4d5e-7f60-8a1b-000000000002"
THREAD_THREE = "019a2b3c-4d5e-7f60-8a1b-000000000003"
THREAD_OLD = "019a2b3c-4d5e-7f60-8a1b-000000000004"
THREAD_M = "019a2b3c-4d5e-7f60-8a1b-000000000005"

CODEX = get_adapter("codex")
PANE_PID = 4242
#: The pid of the Claude Code session that happened to start the tmux server.
STALE_CLAUDE_PID = "31337"


def _task(where: ProjectInfo, title: str) -> TeamTask:
    task, _created = team_service.add_task(title, role="coder", cwd=where.root)
    return task


def _row(agent_id: str) -> FleetAgent:
    with store_session() as store:
        row = store.get_fleet_agent(agent_id)
    assert row is not None
    return row


def _claimed_by(task_id: str) -> str | None:
    with store_session() as store:
        task = store.get_task(task_id)
    assert task is not None
    return task.claimed_by


class Codex:
    """The Codex process in a fleet pane, firing its hooks as 0.162.1 does."""

    def __init__(
        self,
        agent: FleetAgent,
        where: ProjectInfo,
        home: Path,
        server: FakeTmux,
        monkeypatch: pytest.MonkeyPatch,
        *,
        launch: str = "launch-a",
    ) -> None:
        self.agent, self.project, self.home = agent, where, home
        self.monkeypatch, self.launch = monkeypatch, launch
        server.pids[agent.pane_id] = PANE_PID
        # Its empty composer and the footer under it, which a turn keeps too (0.162.1):
        # a test that draws something else on its screen sets it.
        server.screens.setdefault(agent.pane_id, IDLE_PROMPT)
        self.become()

    def become(self) -> None:
        """Run as the agent itself: the window's variables, a stale CLAUDE_PID, and no
        tool-shell marker, which ``aisquare launch`` clears for the agent it starts."""
        self.monkeypatch.setenv("AISQUARE_FLEET_AGENT", self.agent.id)
        self.monkeypatch.setenv("AISQUARE_LAUNCH_ID", self.launch)
        self.monkeypatch.setenv("AISQUARE_ROLE", "coder")
        self.monkeypatch.setenv("CLAUDE_PID", STALE_CLAUDE_PID)
        self.monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    def child(self, parent_native: str) -> None:
        """Run as a ``codex exec`` one of the agent's tools started: the same window
        variables, and the parent's thread in ``CODEX_THREAD_ID``."""
        self.become()
        self.monkeypatch.setenv("CODEX_THREAD_ID", parent_native)

    def board_id(self, native: str) -> str:
        return agent_events.session_key(CODEX, self.home, native)

    def hook(self, event: str, native: str, **extra: object) -> str:
        payload = {
            "hook_event_name": event,
            "session_id": native,
            "cwd": str(self.project.root),
            "transcript_path": str(self.home / "sessions" / f"rollout-{native}.jsonl"),
            **extra,
        }
        return agent_events.handle(CODEX, payload, self.home)

    def start(self, native: str, source: str = "startup") -> str:
        return self.hook("SessionStart", native, source=source, model="gpt-test")


@pytest.fixture
def codex_agent(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Codex, TeamTask]:
    task = _task(project, "the task this codex coder is for")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", task_id=task.id, worktree=False
    ).agent
    assert agent.agent == "codex" and agent.session_id is None, "Codex's id binds at its start"
    home = tmp_path / "codex-home"
    home.mkdir()
    # The plain Codex home, as the shell the agent was spawned from names it: a home
    # `codex queue` runs under has to be one of Codex's own (`fleet._native_home`).
    monkeypatch.setenv("CODEX_HOME", str(home))
    return Codex(agent, project, home, tmux, monkeypatch), task


# --- /clear keeps the claims (acceptance 6) ----------------------------------------------


def test_a_codex_clear_moves_the_row_and_its_claims_to_the_new_thread(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    codex, task = codex_agent
    first = codex.board_id(THREAD_ONE)
    board = codex.start(THREAD_ONE)
    assert f"ASSIGNED TO YOU: {task.id}" in board
    assert team_service.claim_task(task.id, session_ref=first).status == "doing"

    # Codex's /clear: no SessionEnd for thread-one, a start of thread-two from the
    # same process (measured on 0.162.1).
    cleared = codex.start(THREAD_TWO, source="clear")

    second = codex.board_id(THREAD_TWO)
    assert _row(codex.agent.id).session_id == second
    assert _claimed_by(task.id) == second, "kept, and moved with the row"
    assert "You are the one working it" in cleared
    assert "Claim it FIRST" not in cleared and "already doing by" not in cleared
    with store_session() as store:
        kinds = [event.kind for event in store.recent_events(codex.project.id, limit=20)]
    assert "task_released" not in kinds
    status = fleet_service.status_of(_row(codex.agent.id))
    assert status.session is not None and status.session.id == second
    # And again, from the id the first clear moved to.
    codex.start(THREAD_THREE, source="clear")
    assert _claimed_by(task.id) == codex.board_id(THREAD_THREE)


def test_a_nested_codex_under_the_window_takes_neither_the_row_nor_the_task(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """A ``codex exec`` the agent runs inherits the window's variables, launch id
    included. Every newly started session under the window used to rebind the row
    (``fleet-session:<row>`` took the newest start), so the child became the row with
    the claim left behind; whatever start it reports, it takes nothing now."""
    codex, task = codex_agent
    first = codex.board_id(THREAD_ONE)
    codex.start(THREAD_ONE)
    team_service.claim_task(task.id, session_ref=first)

    codex.child(THREAD_ONE)
    for source in ("startup", "resume", "clear"):
        child = codex.start(f"child-{source}", source=source)
        assert "ASSIGNED TO YOU" not in child

    assert _row(codex.agent.id).session_id == first
    assert _claimed_by(task.id) == first
    with store_session() as store:
        listed = store.fleet_agents(codex.project.id, live_only=True)
        assert store.fleet_agent_for_session(codex.project.id, first) is not None
        assert (
            store.fleet_agent_for_session(codex.project.id, codex.board_id("child-resume")) is None
        )
        assert store.get_meta(f"launch-session:{codex.launch}") == first, "MCP stays the agent's"
    assert [row.session_id for row in listed if row.id == codex.agent.id] == [first]


def test_a_codex_exec_from_a_claude_agents_tool_shell_takes_neither_its_row_nor_its_task(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r2 of #113, finding 1: a ``codex exec`` a Claude Code agent's Bash tool
    runs inherits the window's ``AISQUARE_FLEET_AGENT`` and the agent's own
    ``CLAUDE_PID``, and has no ``CODEX_THREAD_ID`` (only Codex's tools set it), so its
    hooks read as the pane's own Codex process: it took the Claude row and its claim,
    and its SessionEnd released the task the Claude agent was still working. A session
    of another agent than the row's is never the pane's own."""
    task = _task(project, "the claude coder's task")
    agent = fleet_service.spawn(
        project, "coder", agent="claude-code", task_id=task.id, worktree=False
    ).agent
    first = agent.session_id
    assert agent.agent == "claude-code" and first is not None
    tmux.pids[agent.pane_id] = PANE_PID
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("CLAUDE_PID", str(PANE_PID))
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(task.id, session_ref=first)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    home = tmp_path / "codex-home"
    home.mkdir()

    def hook(event: str, **extra: object) -> str:
        payload = {
            "hook_event_name": event,
            "session_id": "exec-thread",
            "cwd": str(project.root),
            "transcript_path": str(home / "sessions" / "rollout-exec-thread.jsonl"),
            **extra,
        }
        return agent_events.handle(CODEX, payload, home)

    child = hook("SessionStart", source="startup", model="gpt-test")

    assert "ASSIGNED TO YOU" not in child
    assert _row(agent.id).session_id == first and _claimed_by(task.id) == first
    hook("UserPromptSubmit", prompt="second opinion on this diff", turn_id="turn-1")
    hook("SessionEnd", reason="other")
    assert _row(agent.id).session_id == first and _claimed_by(task.id) == first


def test_the_agents_own_resume_to_an_earlier_thread_takes_the_row_and_its_launch_back(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Review r1 of #113, finding 6: a ``/resume`` to a thread the launch had already
    seen left the launch (the MCP server's identity) and the row on the abandoned one,
    and the MCP server's calls revived it and recorded claims under it. The pane's own
    process goes back to its earlier thread with the row, the claims and the launch; a
    late redelivery of a first start does not."""
    codex, task = codex_agent
    first, second = codex.board_id(THREAD_ONE), codex.board_id(THREAD_TWO)
    codex.start(THREAD_ONE)
    codex.start(THREAD_TWO, source="clear")
    team_service.claim_task(task.id, session_ref=second)

    codex.start(THREAD_ONE, source="startup")  # a late redelivery: nothing moves
    with store_session() as store:
        assert store.get_meta(f"launch-session:{codex.launch}") == second
    back = codex.start(THREAD_ONE, source="resume")

    assert _row(codex.agent.id).session_id == first and _claimed_by(task.id) == first
    assert "You are the one working it" in back
    with store_session() as store:
        assert store.get_meta(f"launch-session:{codex.launch}") == first
        assert store.get_meta(f"fleet-session:{codex.agent.id}") == first


def test_an_early_prompt_of_the_cleared_thread_is_the_agent_s_and_a_child_s_is_not(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """A hook of the new thread that is not its start, arriving first, moves the row
    when the agent's own process sends it (the bind lands a prompt early, as rule 1
    lets it land one late); the same hook from a child's thread moves nothing."""
    codex, task = codex_agent
    first = codex.board_id(THREAD_ONE)
    codex.start(THREAD_ONE)
    team_service.claim_task(task.id, session_ref=first)

    codex.child(THREAD_ONE)
    codex.hook("UserPromptSubmit", "child-thread", prompt="nested", turn_id="turn-child")
    assert _row(codex.agent.id).session_id == first and _claimed_by(task.id) == first

    codex.become()
    codex.hook("UserPromptSubmit", THREAD_TWO, prompt="early", turn_id="turn-early")
    second = codex.board_id(THREAD_TWO)
    assert _row(codex.agent.id).session_id == second and _claimed_by(task.id) == second
    codex.start(THREAD_TWO, source="clear")
    assert _row(codex.agent.id).session_id == second and _claimed_by(task.id) == second


def test_the_window_s_inherited_tool_marker_is_cleared_for_the_agent_launch_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tmux server a Codex tool started hands ``CODEX_THREAD_ID`` to every window;
    the agent ``aisquare launch`` starts there must not read as somebody's child."""
    monkeypatch.setenv("CODEX_THREAD_ID", "a-manager-thread")
    selected = agent_launch.resolve("coder", agent="codex", cwd=tmp_path)
    env = dict(os.environ)
    agent_launch.launch_identity(env, selected, None)
    assert "CODEX_THREAD_ID" not in env


# --- restart resumes the bound thread (acceptance 1) -------------------------------------


def _native(command: list[str]) -> list[str]:
    """What the window's ``aisquare launch`` hands the agent: everything after its ``--``."""
    return command[command.index("--") + 1 :]


def _launched_argv(
    command: list[str], runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """The argv the window's own ``aisquare launch`` execs for ``command``."""
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, command[command.index("launch") :])
    assert result.exit_code == 0, result.output
    argv: list[str] = execute.call_args.args[1]
    return argv


def _rollout(codex: Codex, native: str) -> Path:
    rollout = codex.home / "sessions" / f"rollout-{native}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("{}\n", encoding="utf-8")
    return rollout


def test_restart_of_a_running_codex_agent_resumes_its_bound_thread(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, task = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    first = codex.board_id(THREAD_ONE)
    team_service.claim_task(task.id, session_ref=first)

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.was_running is True and receipt.resumed is True
    assert tmux.typed[:2] == [
        (codex.agent.pane_id, "literal", "/exit"),
        (codex.agent.pane_id, "key", "Enter"),
    ], "stopped through the adapter's exit command"
    assert receipt.started.agent == "codex" and receipt.started.session_id == first
    command = _command(tmux)
    native = _native(command)
    at = native.index("resume")
    assert native[at : at + 2] == ["resume", THREAD_ONE], "the thread by its native id"
    assert command[-2:] == ["--", fleet_service._restart_prompt(receipt.started)]
    assert "--resume" not in command, "no Claude Code flag"
    # The window's launch puts every option before `resume <id>`, and the continue line
    # after Codex's own `--`: `codex <options> resume <id> -- <prompt>`.
    argv = _launched_argv(command, runner, monkeypatch)
    at = argv.index("resume")
    assert argv[at : at + 2] == ["resume", THREAD_ONE]
    assert "--approve-for-me" in argv[:at] and argv[-2:] == command[-2:]
    assert _claimed_by(task.id) == first, "the same thread keeps the claim"
    assert _events(codex.project, "restarted") == [
        f"{codex.agent.label} restarted — resumed its session"
    ]


def test_a_codex_row_with_no_bound_thread_restarts_fresh_and_says_so(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    codex, _ = codex_agent

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.resumed is False
    assert any(
        "no session is recorded for it" in note and "starts fresh" in note for note in receipt.notes
    ), receipt.notes
    assert "resume" not in _native(_command(tmux))


def test_a_codex_session_recorded_without_its_native_id_restarts_fresh_and_says_so(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    codex, _ = codex_agent
    rollout = _rollout(codex, THREAD_OLD)
    session = launch_seal.SessionFacts(id="board-only", agent="codex", transcript=str(rollout))
    _recorded(codex.agent, session=session)

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.resumed is False
    assert any("no native session id" in note for note in receipt.notes), receipt.notes
    assert "resume" not in _native(_command(tmux)) and "board-only" not in _command(tmux)


def test_a_fresh_codex_hand_over_is_adopted_by_the_replacements_first_start(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``restart --fresh`` of a running agent (and a switch onto another home) parks the
    claims on the old id for the replacement, whose Codex id binds only at its start.
    Its first start under the new window takes them over; the old process's late
    ``SessionEnd`` then has nothing left to release."""
    codex, task = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    first = codex.board_id(THREAD_ONE)
    team_service.claim_task(task.id, session_ref=first)

    receipt = fleet_service.restart(codex.project, codex.agent.label, fresh=True)

    assert receipt.resumed is False and receipt.started.session_id == first, "parked on it"
    assert _claimed_by(task.id) == first
    replacement = Codex(receipt.started, codex.project, codex.home, tmux, monkeypatch, launch="b")
    board = replacement.start(THREAD_TWO)
    second = replacement.board_id(THREAD_TWO)
    assert _row(receipt.started.id).session_id == second
    assert _claimed_by(task.id) == second and "You are the one working it" in board
    codex.become()  # the old process's own SessionEnd, late
    codex.hook("SessionEnd", THREAD_ONE, reason="other")
    assert _claimed_by(task.id) == second
    # A nested child of the replacement takes nothing.
    replacement.child(THREAD_TWO)
    replacement.start("child", source="startup")
    assert _row(receipt.started.id).session_id == second and _claimed_by(task.id) == second


# --- tell reaches a busy agent through its queue (acceptance 3) --------------------------


def _notes_to(where: ProjectInfo, label: str) -> list[str]:
    with store_session() as store:
        events = store.recent_events(where.id, limit=50)
    return [event.text for event in events if event.kind == "note" and event.to_role == label]


def test_tell_queues_for_a_busy_codex_agent_through_codex_queue(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "Queued message m for thread t.\n", "")

    monkeypatch.setattr(fleet_service, "_run_native", run)

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is True and "queued for its next turn" in told.how
    [(argv, env)] = calls
    assert Path(argv[0]).name.startswith("codex")
    assert argv[1:] == ["queue", "--thread", THREAD_ONE, "--message=rebase on main first"]
    assert env["CODEX_HOME"] == str(codex.home), "the queue of the home the thread runs under"
    assert _notes_to(codex.project, codex.agent.label) == []


@pytest.mark.parametrize(
    "text", ["--no-verify was wrong, amend the commit", "- [ ] step one\n- [ ] step two", "-"]
)
def test_a_told_message_that_starts_with_a_dash_is_the_queues_text_not_an_option(
    text: str,
) -> None:
    """``codex queue --message <text>`` reads a text that starts with ``-`` as an option
    and exits "unexpected argument" (0.162.1, measured); joined to ``--message=``, it is the
    message, whatever follows the dash."""
    argv = CODEX.queue_args(THREAD_ONE, text)

    assert argv == ["queue", "--thread", THREAD_ONE, f"--message={text}"]
    assert not any(word == text for word in argv), "never a word of its own"


def test_a_queue_that_fails_leaves_the_message_on_the_board(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")

    def refused(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "error: thread not found\n")

    monkeypatch.setattr(fleet_service, "_run_native", refused)

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is False
    assert "could not queue it (error: thread not found)" in told.how and "board note" in told.how
    assert _notes_to(codex.project, codex.agent.label) == ["rebase on main first"]


def test_tell_to_a_codex_agent_that_is_not_working_queues_nothing(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Needing the human (a permission prompt) is not busy: a board note, as before."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("PermissionRequest", THREAD_ONE)
    monkeypatch.setattr(fleet_service, "_run_native", Mock(side_effect=AssertionError("queued")))

    told = fleet_service.tell(codex.project, codex.agent.label, "hello")

    assert told.delivered is False and "board note" in told.how
    assert _notes_to(codex.project, codex.agent.label) == ["hello"]


# --- a line typed into a Codex pane: at its prompt, its Enter apart (lane B of #113) -----

#: Codex 0.162.1's screens under tmux 3.7c, trimmed: its empty prompt, the two dialogs a
#: fresh pane sits at just as quietly, and the prompt holding a draft of the human's.
#: ``CHEVRON`` is the U+203A Codex marks its prompt and a dialog's choice with.
CHEVRON = "\u203a"
IDLE_PROMPT = [
    "  >_ OpenAI Codex (v0.162.1)",
    "",
    f"{CHEVRON} Ask Codex to do anything",
    "",
    "  gpt-5.5 default · /work/repo",
    "  ? for shortcuts                                              ⚠ 2 warnings · f2 to view",
    "",
]
TRUST_DIALOG = [
    "  Folder access",
    "  Trust this folder? Codex can read, edit, and run files here, subject to your",
    "",
    f"{CHEVRON} 1. Trust and continue",
    "  2. Quit",
    "",
    "  enter continue · esc quit",
]
HOOK_REVIEW = [
    "  Hooks need review",
    "  8 hooks are new or changed.",
    "",
    f"{CHEVRON} 1. Review hooks",
    "  2. Trust all and continue",
    "  3. Continue without trusting (hooks won't run)",
    "",
    "  enter confirm · esc skip",
]
DRAFT = [
    f"{CHEVRON} half a sentence of the human's",
    "",
    "  gpt-5.5 default · /work/repo",
    "                                                               ⚠ 2 warnings · f2 to view",
]


def test_tell_types_into_a_codex_agent_at_its_empty_prompt_before_any_hook(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    clock: FakeClock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Welcome starts Codex coders with no prompt, and Codex fires no hook before its
    first one: every ``tell`` was a board note the coder never saw. Its quiet pane at the
    empty prompt takes the line, the Enter after its own gap."""
    codex, _ = codex_agent
    pane = codex.agent.pane_id
    tmux.set_command(pane, "codex")
    tmux.screens[pane] = IDLE_PROMPT
    typed = _timed_input(tmux, clock, monkeypatch)

    told = fleet_service.tell(codex.project, codex.agent.label, "reply ok")

    assert told.delivered is True and "typed" in told.how, told
    [(kind, text, typed_at), (key, enter, entered_at)] = typed
    assert (kind, text, key, enter) == ("paste", "reply ok", "key", "Enter")
    assert round(entered_at - typed_at, 9) >= fleet_service._KEY_GAP > 0
    assert _notes_to(codex.project, codex.agent.label) == []


@pytest.mark.parametrize(
    ("screen", "printing"),
    [(TRUST_DIALOG, False), (HOOK_REVIEW, False), (DRAFT, False), (IDLE_PROMPT, True)],
    ids=["folder-trust", "hook-review", "draft", "mid-turn"],
)
def test_a_codex_pane_not_quiet_at_its_empty_prompt_gets_the_note(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    screen: list[str],
    printing: bool,
) -> None:
    """An Enter at the folder-trust dialog trusts the folder for the human, at the hook
    review it opens the review, into a draft it sends the human's half sentence; and a
    turn keeps the footer while it redraws its timer every second."""
    codex, _ = codex_agent
    pane = codex.agent.pane_id
    tmux.set_command(pane, "codex")
    tmux.screens[pane] = screen
    if printing:
        tmux.printed(pane)

    told = fleet_service.tell(codex.project, codex.agent.label, "reply ok")

    assert told.delivered is False and "board note" in told.how
    assert "not at Codex's empty prompt" in told.how
    assert tmux.typed == []
    assert _notes_to(codex.project, codex.agent.label) == ["reply ok"]


def test_a_waiting_codex_manager_is_woken_with_the_enter_apart(
    tmux: FakeTmux,  # noqa: F811
    clock: FakeClock,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nudge's line and its Enter went in one burst, which Codex keeps as a paste:
    three nudges sat unsent in a waiting manager's prompt, and it woke for none."""
    manager = fleet_service.spawn(project, "manager", agent="codex", worktree=False).agent
    home = tmp_path / "codex-home"
    home.mkdir()
    codex = Codex(manager, project, home, tmux, monkeypatch)
    codex.start(THREAD_M)
    codex.hook("UserPromptSubmit", THREAD_M, prompt="run the team", turn_id="turn-1")
    codex.hook("Stop", THREAD_M, turn_id="turn-1", stop_hook_active=False)
    tmux.set_command(manager.pane_id, "codex")
    assert fleet_service.status_of(_row(manager.id)).state == "waiting"
    typed = _timed_input(tmux, clock, monkeypatch)

    assert fleet_service.nudge_manager(project.id, reason="task_review") is True

    [(kind, text, typed_at), (key, enter, entered_at)] = typed
    assert (kind, text, key, enter) == ("paste", fleet_service.NUDGE_TEXT, "key", "Enter")
    assert round(entered_at - typed_at, 9) >= fleet_service._KEY_GAP > 0
    # A wait past _STALE_AFTER leaves the hooks saying nothing fresh: the pane decides as
    # it does for tell, and a draft of the human's in the prompt is not typed behind.
    later = datetime.now(tz=UTC) + team_service._STALE_AFTER + timedelta(minutes=1)
    monkeypatch.setattr(fleet_service, "_now", lambda: later)
    tmux.screens[manager.pane_id] = DRAFT
    assert fleet_service.nudge_manager(project.id, reason="task_review") is False
    tmux.screens[manager.pane_id] = IDLE_PROMPT
    assert fleet_service.nudge_manager(project.id, reason="task_review") is True
    assert len(typed) == 4


#: Codex 0.162.1 after a turn near its five-hour limit, its Stop fired and its hooks saying
#: waiting (lane F, a loopback provider; SMOKE3's LIM-coder2-pane.txt): a dialog of its own,
#: where an Enter picks option 1 and switches the agent's model.
RATE_LIMIT_DIALOG = [
    "• ok",
    "  Worked for <1s • 11:29 PM",
    "  Approaching rate limits",
    "  Switch to gpt-6-luna for lower credit usage?",
    f"{CHEVRON} 1. Switch to gpt-6-luna                   Fast and affordable model for easier "
    "tasks.",
    "  2. Keep current model",
    "  3. Keep current model (never show again)  Hide future rate limit reminders about switching "
    "models",
    "  enter select · esc back",
]


def test_a_codex_dialog_after_its_stop_gets_no_keystroke_though_its_hooks_say_waiting(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lane F measured it live: ``fleet tell`` typed into the dialog, the line was lost and
    the Enter switched the agent to gpt-6-luna. Fresh hooks are not its prompt."""
    manager = _waiting_manager_on("codex", project, tmux, tmp_path / "codex-home", monkeypatch)
    tmux.screens[manager.pane_id] = RATE_LIMIT_DIALOG

    assert fleet_service.nudge_manager(project.id, reason="task_review") is False
    told = fleet_service.tell(project, manager.label, "carry on")
    assert not told.delivered and "hooks say waiting" in told.how, told
    assert "dialog" in told.how and "board note" in told.how, told
    assert tmux.typed == []

    receipt = fleet_service.stop(project, manager.label, grace=0)

    assert tmux.typed == [], "no /exit, whose Enter would pick the cheaper model"
    assert receipt.killed is not None and "dialog" in receipt.killed, receipt
    assert manager.pane_id in tmux.killed and receipt.agent.ended_at is not None


def test_tell_to_a_codex_agent_names_a_socket_that_is_gone(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMOKE5 of #113: with the fleet's socket moved away (from ``/tmp``, before the store
    profile held it read-only: review r10, finding 4), ``fleet tell`` said the agent was at
    "a dialog of its own" while ``stop`` named the socket. Both say what it is."""
    manager = _waiting_manager_on("codex", project, tmux, tmp_path / "codex-home", monkeypatch)
    tmux.screens[manager.pane_id] = []
    tmux.socket_file = False

    told = fleet_service.tell(project, manager.label, "carry on")

    assert not told.delivered and "socket" in told.how and "is gone" in told.how, told.how
    assert "dialog of its own" not in told.how and "SIGUSR1" in told.how
    assert tmux.typed == []


@pytest.mark.parametrize("rewritten", [None, "claude-code", "not-an-agent"])
def test_a_codex_row_whose_agent_column_was_rewritten_gets_no_keystroke_at_its_dialog(
    rewritten: str | None,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r6 of #113, finding 1: the placed row kept the store's ``agent``, and every
    check that keeps an Enter out of Codex's dialogs took its adapter from it. Set to NULL
    (read as Claude Code's) or an unknown name, ``tell`` pasted and pressed Enter into the
    rate-limit dialog and ``stop`` typed ``/exit`` and Enter there. The record's agent
    decides: ``tell`` and the nudge refuse a rewritten row, ``stop`` kills it unanswered."""
    manager = _waiting_manager_on("codex", project, tmux, tmp_path / "codex-home", monkeypatch)
    tmux.screens[manager.pane_id] = RATE_LIMIT_DIALOG
    _rewrite("fleet_agent", "agent", rewritten, manager.id)

    told = fleet_service.tell(project, manager.label, "carry on")
    assert not told.delivered and "its row was rewritten" in told.how, told
    assert fleet_service.nudge_manager(project.id, reason="task_review") is False
    receipt = fleet_service.stop(project, manager.label, grace=0)

    assert tmux.typed == [], "no line, no /exit: an Enter there picks the cheaper model"
    assert receipt.killed is not None and "dialog" in receipt.killed, receipt
    assert manager.pane_id in tmux.killed


def test_every_fleet_codex_launch_hides_its_rate_limit_dialog_unless_the_operator_sets_it(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, _ = codex_agent
    flag = ["-c", "notice.hide_rate_limit_model_nudge=true"]

    def hides() -> bool:
        """Whether the newest window's own ``aisquare launch`` passes the flag, run with the
        window's environment (``AISQUARE_FLEET_AGENT``, a blank ``AISQUARE_LAUNCH_ID``)."""
        spawned = tmux.spawned[-1]
        environment = spawned["env"]
        assert isinstance(environment, dict)
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        argv = _launched_argv(_command(tmux), runner, monkeypatch)
        codex.become()  # the tests' own process is the agent again
        return any(argv[at : at + 2] == flag for at in range(len(argv)))

    assert hides(), "spawn"
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    fleet_service.restart(codex.project, codex.agent.label)
    assert hides(), "restart"
    # Switch and the usage-limit hand-over replay through the same window launch.
    (codex.home / "config.toml").write_text(
        "[notice]\nhide_rate_limit_model_nudge = false\n", encoding="utf-8"
    )
    assert not hides(), "the operator's own setting"


@pytest.mark.parametrize(
    ("config", "args", "added"),
    [
        pytest.param("", [], True, id="none"),
        pytest.param("[notice]\nhide_full_access_warning = true\n", [], True, id="another"),
        pytest.param("[notice]\nhide_rate_limit_model_nudge = false\n", [], False, id="user"),
        pytest.param("", ["-c", "notice.hide_rate_limit_model_nudge=false"], False, id="override"),
        pytest.param("", ["-p", "quiet"], False, id="profile"),
        pytest.param(None, [], False, id="unreadable"),
    ],
)
def test_the_fleets_codex_setting_yields_to_any_layer_of_the_operators(
    tmp_path: Path, config: str | None, args: list[str], added: bool
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    if config is None:
        (home / "config.toml").mkdir()  # a layer that cannot be read may set it
    else:
        (home / "config.toml").write_text(config, encoding="utf-8")
    (home / "quiet.config.toml").write_text(
        "[notice]\nhide_rate_limit_model_nudge = false\n", encoding="utf-8"
    )
    selected = agent_launch.resolve(agent="codex")
    window = {"CODEX_HOME": str(home), "AISQUARE_FLEET_AGENT": "agt-1"}
    given = agent_launch.fleet_settings_args(selected, window, args)
    assert (["-c", "notice.hide_rate_limit_model_nudge=true"] in _pairs(given)) is added
    if config is None:
        assert given == [], "a layer that cannot be read may set any of them"
    # Outside a fleet window the human answers Codex's dialogs, and nothing is added.
    assert agent_launch.fleet_settings_args(selected, {"CODEX_HOME": str(home)}, args) == []
    claude = agent_launch.resolve(agent="claude-code")
    assert agent_launch.fleet_settings_args(claude, window, args) == []


def _pairs(argv: list[str]) -> list[list[str]]:
    return [argv[at : at + 2] for at in range(0, len(argv), 2)]


@pytest.mark.parametrize(
    ("config", "args", "added"),
    [
        pytest.param("", [], True, id="none"),
        pytest.param("check_for_update_on_startup = true\n", [], False, id="user"),
        pytest.param("", ["-c", "check_for_update_on_startup=true"], False, id="override"),
        pytest.param("", ["-p", "updating"], False, id="profile"),
    ],
)
def test_a_fleet_codex_window_starts_with_no_update_dialog_unless_the_operator_asks(
    tmp_path: Path, config: str, args: list[str], added: bool
) -> None:
    """Codex 0.162.1, npm-installed, opened ``Update available · 0.162.1 → 0.163.0`` in a
    starting pane once 0.163.0 was out (CI on 72a88fc0): its first choice, which an Enter
    picks, runs ``npm install -g @openai/codex``, and the agent waits on it. A fleet
    window launches with ``check_for_update_on_startup=false`` unless a layer of the
    operator's sets it."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")
    (home / "updating.config.toml").write_text(
        "check_for_update_on_startup = true\n", encoding="utf-8"
    )
    selected = agent_launch.resolve(agent="codex")
    window = {"CODEX_HOME": str(home), "AISQUARE_FLEET_AGENT": "agt-1"}
    given = agent_launch.fleet_settings_args(selected, window, args)
    assert (["-c", "check_for_update_on_startup=false"] in _pairs(given)) is added


def _waiting_manager_on(
    agent: str, where: ProjectInfo, server: FakeTmux, home: Path, monkeypatch: pytest.MonkeyPatch
) -> FleetAgent:
    """A manager of ``agent``'s at its prompt, its turn over, as its hooks leave it."""
    manager = fleet_service.spawn(where, "manager", agent=agent, worktree=False).agent
    if agent == "codex":
        home.mkdir()
        native = Codex(manager, where, home, server, monkeypatch, launch="launch-m")
        native.start("thread-m")
        native.hook("UserPromptSubmit", "thread-m", prompt="run the team", turn_id="turn-1")
        native.hook("Stop", "thread-m", turn_id="turn-1", stop_hook_active=False)
    else:
        assert manager.session_id is not None
        team_service.hook_session_start(manager.session_id, where.root, "startup")
        team_service.hook_stop(manager.session_id, where.root)
    server.set_command(manager.pane_id, "codex" if agent == "codex" else "claude")
    row = _row(manager.id)
    assert fleet_service.status_of(row).state == "waiting"
    return row


#: What Codex's sandbox does to the fleet's socket (measured on 0.162.1: the command runs
#: in namespaces of its own, and `tmux -L <socket>` there is `Operation not permitted`).
def _sandboxed(server: FakeTmux, inside: bool) -> None:
    server.socket_denied = inside


def _owed(where: ProjectInfo) -> str | None:
    with store_session() as store:
        return store.get_meta(fleet_service.NUDGE_OWED + where.id)


@pytest.mark.parametrize("manager_agent", ["codex", "claude-code"])
@pytest.mark.parametrize(
    "write",
    [
        lambda me, task: team_service.add_note("calc ok", kind="result", session_ref=me),
        lambda me, task: team_service.add_note("which db?", kind="question", session_ref=me),
        lambda me, task: team_service.add_note("fyi", to_role="manager", session_ref=me),
        lambda me, task: team_service.review_task(task, note="ready", session_ref=me),
        lambda me, task: team_service.finish_task(task, session_ref=me),
        lambda me, task: team_service.block_task(task, reason="needs spec", session_ref=me),
        lambda me, task: team_service.reopen_task(task, reason="fails on 3.12", session_ref=me),
    ],
    ids=["result", "question", "note-to-manager", "review", "done", "blocked", "reopen"],
)
def test_a_sandboxed_codex_write_wakes_the_waiting_manager_from_the_writers_hook(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_agent: str,
    write: Callable[[str, str], object],
) -> None:
    """SMOKE2 on 0.162.1: a Codex coder's board write ran in its sandbox, where the
    fleet's tmux socket is refused, so the nudge it owed a waiting manager — Codex's or
    Claude Code's — was lost, and the fleet stalled. The sandbox gets no tmux: its write
    leaves the nudge owed, and the coder's own PostToolUse, which Codex runs outside the
    sandbox, sends it the moment the command ends."""
    codex, task = codex_agent
    manager = _waiting_manager_on(
        manager_agent, codex.project, tmux, tmp_path / "manager-home", monkeypatch
    )
    codex.become()
    codex.start("thread-c")
    codex.hook("UserPromptSubmit", "thread-c", prompt="work the task", turn_id="turn-1")
    me = codex.board_id("thread-c")
    team_service.claim_task(task.id, session_ref=me)

    _sandboxed(tmux, True)
    write(me, task.id)
    _sandboxed(tmux, False)

    assert tmux.typed == [], "nothing reaches tmux from the sandbox"
    assert _owed(codex.project) is not None
    codex.hook("PostToolUse", "thread-c", turn_id="turn-1", tool_name="Bash")
    assert tmux.typed == [
        (manager.pane_id, "paste", fleet_service.NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]
    assert _owed(codex.project) is None, "sent once"
    codex.hook("Stop", "thread-c", turn_id="turn-1", stop_hook_active=False)
    assert len(tmux.typed) == 2


def test_a_codex_hook_with_no_nudge_owed_opens_the_store_no_further_time(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r4 of #113, finding 13: every Codex PostToolUse and Stop opened the store
    twice more to look for a nudge owed, even with none: the session again, right after
    the tool's own write read it, and the mark's take. The mark is read in the store
    session the hook has open; with one owed, it is sent as before."""
    codex, _ = codex_agent
    codex.become()
    codex.start("thread-c")
    codex.hook("UserPromptSubmit", "thread-c", prompt="work the task", turn_id="turn-1")
    opened: list[str] = []

    def counted(module: Any, name: str) -> None:
        real = module.store_session

        @contextmanager
        def counting() -> Iterator[Any]:
            opened.append(name)
            with real() as store:
                yield store

        monkeypatch.setattr(module, "store_session", counting)

    counted(agent_events, "hook")
    counted(fleet_service, "fleet")

    codex.hook("PostToolUse", "thread-c", turn_id="turn-1", tool_name="Bash")
    assert opened == ["hook", "hook"], "the launch's binding and the tool's write, no more"
    opened.clear()
    codex.hook("Stop", "thread-c", turn_id="turn-1", stop_hook_active=False)
    assert "fleet" not in opened, "no mark to take"
    sent: list[str] = []
    monkeypatch.setattr(fleet_service, "deliver_owed_nudge", sent.append)
    with store_session() as store:
        store.set_meta(fleet_service.NUDGE_OWED + codex.project.id, "now")
    codex.hook("PostToolUse", "thread-c", turn_id="turn-1", tool_name="Bash")
    codex.hook("Stop", "thread-c", turn_id="turn-2", stop_hook_active=False)  # not a redelivery
    assert sent == [codex.project.id, codex.project.id], "owed, it goes to be sent"


def test_an_owed_nudge_waits_for_a_hook_and_wakes_nobody_for_news_already_read(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark says a write was made, not that it is unread: a manager woken since by
    something else, which read the write in its delta, is not typed into for it."""
    codex, _ = codex_agent
    manager = _waiting_manager_on("codex", codex.project, tmux, tmp_path / "m", monkeypatch)
    codex.become()
    codex.start("thread-c")
    me = codex.board_id("thread-c")
    _sandboxed(tmux, True)
    team_service.add_note("calc ok", kind="result", session_ref=me)
    # Refused again from inside: owed once, never written over by a second refusal.
    owed = _owed(codex.project)
    assert owed is not None and fleet_service.deliver_owed_nudge(codex.project.id) is False
    assert _owed(codex.project) is None, "taken, and not owed again by the sandbox's taker"
    _sandboxed(tmux, False)

    team_service.add_note("calc ok again", kind="result", session_ref=me)  # sent directly
    assert len(tmux.typed) == 2 and _owed(codex.project) is None
    with store_session() as store:  # the manager's next prompt read the delta
        latest = store.latest_seq(codex.project.id)
        assert manager.session_id is not None
        store.touch_session(manager.session_id, cursor=latest, state="waiting")
        store.set_meta(fleet_service.NUDGE_OWED + codex.project.id, "stale")
        store.delete_meta(f"nudge:{manager.session_id}")
    codex.hook("PostToolUse", "thread-c", turn_id="turn-1", tool_name="Bash")
    assert len(tmux.typed) == 2, "nothing unread: no nudge"
    assert _owed(codex.project) is None


def test_a_manager_that_is_working_keeps_its_news_for_its_own_stop(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex, _ = codex_agent
    manager = _waiting_manager_on("codex", codex.project, tmux, tmp_path / "m", monkeypatch)
    with store_session() as store:
        assert manager.session_id is not None
        store.touch_session(manager.session_id, state="working")
    codex.become()
    codex.start("thread-c")
    _sandboxed(tmux, True)
    team_service.add_note("calc ok", kind="result", session_ref=codex.board_id("thread-c"))
    _sandboxed(tmux, False)
    codex.hook("PostToolUse", "thread-c", turn_id="turn-1", tool_name="Bash")
    assert tmux.typed == [] and _owed(codex.project) is None


def test_a_codex_start_comes_with_its_first_prompt_and_leaves_the_row_working(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Codex fires SessionStart as its first prompt is submitted, not at rest at its
    prompt (``starts_at_its_prompt`` is Claude Code's): the row is not made waiting."""
    codex, _ = codex_agent
    codex.start("thread-one")
    with store_session() as store:
        session = store.get_session(codex.board_id("thread-one"))
    assert session is not None and session.state == "working"


def test_a_codex_spawn_prompt_is_its_argument_and_nothing_is_typed_into_its_dialogs(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Codex takes its first prompt on its command line and submits it once its
    folder-trust and hook-review dialogs are answered (measured on 0.162.1), so a spawn
    types nothing into a pane those dialogs may hold."""
    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, prompt="start on tsk_1"
    )
    assert receipt.prompt_typed is True and tmux.typed == []
    assert _command(tmux)[-2:] == ["--", "start on tsk_1"]


def test_claude_codes_hooks_leave_an_owed_nudge_alone(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code runs its tools unsandboxed and sends its own nudges; its hooks are as
    they were."""
    claude = get_adapter("claude-code")
    coder = fleet_service.spawn(project, "coder", agent="claude-code", worktree=False).agent
    assert coder.session_id is not None
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", coder.id)
    team_service.hook_session_start(coder.session_id, project.root, "startup")
    called: list[str] = []
    monkeypatch.setattr(fleet_service, "deliver_owed_nudge", called.append)
    payload = {"session_id": coder.session_id, "cwd": str(project.root)}
    agent_events.handle(claude, {**payload, "hook_event_name": "Stop"}, Path("unused"))
    assert called == []


# --- who answers an approval decides the bell (t8 smoke, lane A) -------------------------

#: A PermissionRequest as 0.162.1 sends it, the same whoever answers it (measured).
APPROVAL = {
    "turn_id": "turn-1",
    "model": "gpt-test",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "aisquare note hi", "description": "post the board note"},
}


def _turn_under(codex: Codex, native: str, reviewer: str) -> None:
    """The turn Codex opens in its rollout for ``native``, answered by ``reviewer``."""
    path = codex.home / "sessions" / f"rollout-{native}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    context = {"approval_policy": "on-request", "approvals_reviewer": reviewer}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "turn_context", "payload": context}) + "\n")
    codex.hook("UserPromptSubmit", native, turn_id="turn-1", prompt="post a note")


def _state(codex: Codex) -> str:
    return fleet_service.status_of(_row(codex.agent.id)).state


def test_an_approval_codex_reviews_itself_rings_no_bell(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Under ``--approve-for-me`` Codex's own reviewer answers every escalation and no
    prompt reaches the pane, yet the hook fires all the same: 7 false bells for one small
    task in the t8 smoke. A human-reviewed request still rings, once."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    _turn_under(codex, THREAD_ONE, "auto_review")

    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)
    codex.hook("PostToolUse", THREAD_ONE, turn_id="turn-1", tool_name="Bash")

    assert _state(codex) == "working"
    assert _events(codex.project, "attention") == []

    _turn_under(codex, THREAD_ONE, "user")  # /permissions → Ask for approval
    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)

    assert _state(codex) == "attention"
    assert _events(codex.project, "attention") == ["Codex needs your approval"]


def test_an_unanswered_codex_approval_holds_the_bell_through_its_redraws(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex redraws its approval view every second while it waits (0.162.1: the pane's
    activity advanced each second, the screen unchanged), which read as the prompt
    answered: '▶ working' for the whole wait. Only a hook moves it: PostToolUse after
    'y', Interrupt after Esc."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    _turn_under(codex, THREAD_ONE, "user")
    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)
    tmux.printed(codex.agent.pane_id, ago=timedelta(seconds=-2))  # a redraw after the hook

    assert _state(codex) == "attention"

    # Past the board's stale window, no hook has fired and the view still redraws.
    now = fleet_service._now
    later = datetime.now(tz=UTC) + timedelta(hours=1)
    monkeypatch.setattr(fleet_service, "_now", lambda: later)
    tmux.printed(codex.agent.pane_id, ago=-timedelta(hours=1))
    assert _state(codex) == "attention"
    monkeypatch.setattr(fleet_service, "_now", now)

    codex.hook("PostToolUse", THREAD_ONE, turn_id="turn-1", tool_name="Bash")  # 'y'
    assert _state(codex) == "working"

    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)
    codex.hook("Interrupt", THREAD_ONE, turn_id="turn-1")  # Esc
    assert _state(codex) == "waiting"


#: Codex 0.162.1's screen under tmux 3.7c, trimmed: its approval view waiting on the human,
#: then the moment they answered ``y``, the approved command still running.
ASKING = [
    f"{CHEVRON} ESC sleep 12; echo slow-done",
    "• Running sleep 12; echo slow-done",
    "  Would you like to run the following command?",
    "  Reason: reach the aisquare board",
    "  $ sleep 12; echo slow-done",
    f"{CHEVRON} 1. Yes, proceed (y)",
    "  2. Yes, and don't ask again for commands that start with `sleep 12` (p)",
    "  3. No, and tell Codex what to do differently (esc)",
    "  Press enter to confirm or esc to cancel",
]
APPROVED = [
    f"{CHEVRON} ESC sleep 12; echo slow-done",
    "✔ You approved codex to run sleep 12; echo slow-done this time",
    "• Running sleep 12; echo slow-done",
    "• Working (7s • esc to interrupt)",
    f"{CHEVRON} Ask Codex to do anything",
    "  gpt-5.5 default · /work/repo                              ⚠ 3 warnings · f2 to view",
]


def test_an_approved_codex_command_reads_working_while_it_runs(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """Review r2 of #113, finding 7: after 'y' Codex's next hook is the approved command's
    PostToolUse, which fires when the command FINISHES, so a 12-minute suite read
    attention for 12 minutes: the bell on, and ``tell`` a board note. The approval view
    gives way to Codex's working line the moment the human allows (0.162.1, measured),
    and that ends the bell; the view itself, redrawn, and a screen tmux cannot read, keep
    it."""
    codex, _ = codex_agent
    pane = codex.agent.pane_id
    codex.start(THREAD_ONE)
    _turn_under(codex, THREAD_ONE, "user")
    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)
    tmux.screens[pane] = ASKING
    tmux.printed(pane, ago=timedelta(seconds=-2))  # a redraw after the hook

    assert _state(codex) == "attention"
    tmux.screens.pop(pane)
    assert _state(codex) == "attention", "nothing on the screen says it was answered"

    tmux.screens[pane] = APPROVED  # 'y'

    assert _state(codex) == "working"
    codex.hook("PostToolUse", THREAD_ONE, turn_id="turn-1", tool_name="Bash")
    assert _state(codex) == "working"
    assert _events(codex.project, "attention") == ["Codex needs your approval"]


#: Codex 0.162.1's approval view of a command that holds its status line's very words, its
#: own output above holding them too (measured under tmux 3.7c, a 100x40 pane).
CRAFTED = [
    "• Ran printf '%s\\n' '• Working (3s • esc to interrupt)' 'plain esc to interrupt'",
    "  └ • Working (3s • esc to interrupt)",
    "    plain esc to interrupt",
    "",
    "• local fixture complete",
    "",
    f"{CHEVRON} ESC printf 'a'; true # x",
    "  • Working (9s • esc to interrupt)",
    f"  {CHEVRON} 1. y",
    "",
    "• Running printf 'a'; true # x",
    "  │ • Working (9s • esc to interrupt)",
    f"  │ {CHEVRON} 1. y",
    "",
    "  Would you like to run the following command?",
    "  Environment: local",
    "  Reason: reach the aisquare board",
    "  $ printf 'a'; true # x",
    "  • Working (9s • esc to interrupt)",
    f"  {CHEVRON} 1. y",
    "",
    f"{CHEVRON} 1. Yes, proceed (y)",
    "  2. No, and tell Codex what to do differently (esc)",
    "",
    "  Press enter to confirm or esc to cancel",
]
#: The same view of a command whose first line ends as the status line does, below an
#: agent message shaped as one and the human's prompt after it.
SHAPED = [
    "• Working (9s • esc to interrupt)",
    f"{CHEVRON} go on",
    "• Running true # (9s • esc to interrupt)",
    "  Would you like to run the following command?",
    "  $ true # (9s • esc to interrupt)",
    f"{CHEVRON} 1. Yes, proceed (y)",
    "  2. No, and tell Codex what to do differently (esc)",
    "  Press enter to confirm or esc to cancel",
]
#: A turn running with a follow-up queued for it, its rows between the status line and
#: the composer (measured), and the screen once the turn is over.
QUEUED = [
    f"{CHEVRON} RUN sleep 8",
    "",
    "• Working (3s • esc to interrupt) · 1 background terminal running · /ps to view · "
    "/stop to close",
    "",
    "• Queued follow-up inputs",
    "  ↳ queued while working",
    "    shift+← edit last queued message",
    "",
    f"{CHEVRON} Ask Codex to do anything",
    "",
    "  local-fixture default · ~/w                         ⚠ 2 warnings · f2 to view",
    "",
]
DONE = [
    f"{CHEVRON} RUN sleep 8",
    "• Ran sleep 8",
    "  └ (no output)",
    "• local fixture complete",
    "  Worked for 8s • 7:05 PM",
    f"{CHEVRON} Ask Codex to do anything",
    "  local-fixture default · ~/w                         ⚠ 2 warnings · f2 to view",
]


#: The footer Codex draws under its composer, signed in (the tester's smoke 3).
FOOTER = [
    "  GPT-6-Astra max fast · /work/repo/.aisquare-worktrees/coder-1 · Reply with PONG",
    "  ? for shortcuts                                               ⚠ 1 warning · f2 to view",
]
TIP = "  └ Tip: When signed in with ChatGPT, use /usage to check your account usage and access"
#: An approved command running, with the hint Codex draws under its status line: the bell
#: held through it (the tester's smoke 3 on 1398cd07, signed in with ChatGPT).
TIPPED = [
    "• Working (8s • esc to interrupt)",
    f"{TIP} available usage limit resets.",
    "",
    f"{CHEVRON} Ask Codex to do anything",
    "",
    *FOOTER,
]
#: The same hint wrapped, and with follow-ups queued under it.
TIPPED_WRAPPED = [
    "• Working (8s • esc to interrupt)",
    TIP,
    "    available usage limit resets.",
    "• Queued follow-up inputs",
    "  ↳ first draft line",
    "    second draft line",
    "    shift+← edit last queued message",
    f"{CHEVRON} Ask Codex to do anything",
    *FOOTER,
]
#: A draft in the composer, one line and two (measured, loopback provider, 0.162.1).
DRAFTED = [
    "• Running sleep 40; echo slept",
    "• Working (11s • esc to interrupt) · 1 background terminal running · /ps to view · "
    "/stop to close",
    "",
    "",
    f"{CHEVRON} a draft of mine",
    "",
    "  tab to queue message                                                    100% context left",
]
DRAFTED_TWICE = [
    "• Working (20s • esc to interrupt)",
    f"{CHEVRON} first draft line",
    "  second draft line",
    "  tab to queue message                                                    100% context left",
]
#: A slow hook, a minute in, and a 60-column pane, which cuts the line short (measured).
HOOKED = [
    "• Working (12s • esc to interrupt) · Running hook",
    f"{CHEVRON} Ask Codex to do anything",
]
LONG = ["• Working (1m 00s • esc to interrupt)", f"{CHEVRON} Ask Codex to do anything"]
NARROW = [
    "• Working (11s • esc to interrupt) · 1 background terminal …",
    f"{CHEVRON} a draft of mine",
    "  tab to queue message                   100% context left",
]
#: The approval view in a 60x8 pane, the command imitating the status line, with the
#: choice moved to the second option (measured): the view's body is cut to one row.
SHORT_ASKING = [
    "  Would you like to run the following command?",
    "  [… 7 lines] ^a view all",
    "  1. Yes, proceed (y)",
    f"{CHEVRON} 2. Yes, and don't ask again for commands that start with",
    "     `true # (9s • esc to interrupt)` (p)",
    "  Press enter to confirm or esc to cancel",
]
#: The approval view of a command whose lines are a status line, a hint and a queued
#: follow-up: every one of them indented in the view, none at the margin.
TIP_ASKING = [
    "• Running printf a",
    "  │ • Working (9s • esc to interrupt)",
    "  │ └ Tip: x",
    "  Would you like to run the following command?",
    "  $ printf a",
    "  • Working (9s • esc to interrupt)",
    "  • Queued follow-up inputs",
    "  ↳ y",
    "  └ Tip: x",
    f"{CHEVRON} 1. Yes, proceed (y)",
    "  2. No, and tell Codex what to do differently (esc)",
    "  Press enter to confirm or esc to cancel",
]
#: A finished turn whose last message imitates the status line, its hint under it.
TIPPED_DONE = [
    "• Working (9s • esc to interrupt)",
    "  └ Tip: For models with adjustable reasoning, press alt+. to increase reasoning effort.",
    "  Worked for 2s • 8:58 PM",
    f"{CHEVRON} Ask Codex to do anything",
    *FOOTER,
]


@pytest.mark.parametrize(
    ("screen", "working"),
    [
        (ASKING, False),
        (APPROVED, True),
        (CRAFTED, False),
        (SHAPED, False),
        (QUEUED, True),
        (DONE, False),
        ([], False),
        (TIPPED, True),
        ([f"◦{TIPPED[0][1:]}", *TIPPED[1:]], True),  # the spinner's other frame
        (TIPPED_WRAPPED, True),
        (DRAFTED, True),
        (DRAFTED_TWICE, True),
        (HOOKED, True),
        (LONG, True),
        (NARROW, True),
        (SHORT_ASKING, False),
        (TIP_ASKING, False),
        (TIPPED_DONE, False),
    ],
)
def test_codex_reads_working_off_its_status_line_only_where_codex_draws_it(
    screen: list[str], working: bool
) -> None:
    """Review r3 of #113, finding 6: ``esc to interrupt`` anywhere on the screen read as
    working, and the approval view sits under the transcript: the words in the command
    awaiting approval, or in output on screen, cleared the bell while the human had not
    answered. Only the row right above the composer counts (0.162.1, measured)."""
    read = CODEX.capabilities.working_on_screen
    assert read is not None and read(screen) is working


#: Codex 0.162.1 in the terminal's own screen (``--no-alt-screen``), measured under tmux:
#: its footer has no ``? for shortcuts`` hint, at its empty prompt or in a turn.
INLINE_FOOTER = "  local-fixture default · /work/repo  ⚠ 6 warnings · f2 to view"
INLINE_IDLE = [
    f"{CHEVRON} Say OK",
    "• fixture ok",
    "  Worked for <1s • 1:12 AM",
    f"{CHEVRON} Ask Codex to do anything",
    INLINE_FOOTER,
]


@pytest.mark.parametrize(
    ("screen", "idle"),
    [
        pytest.param(IDLE_PROMPT, True, id="alternate-screen"),
        pytest.param(
            ["• Working (2s • esc to interrupt)", *IDLE_PROMPT], True, id="alternate-mid-turn"
        ),
        pytest.param(DRAFT, False, id="alternate-draft"),
        pytest.param(RATE_LIMIT_DIALOG, False, id="rate-limit-dialog"),
        pytest.param(ASKING, False, id="approval"),
        pytest.param(INLINE_IDLE, True, id="inline"),
        pytest.param(
            [f"{CHEVRON} SLOW x", "• Working (2s • esc to interrupt)", *INLINE_IDLE[-2:]],
            True,
            id="inline-mid-turn",
        ),
        pytest.param([f"{CHEVRON} half a draft", INLINE_FOOTER], False, id="inline-draft"),
        pytest.param([*INLINE_IDLE[:-2], *RATE_LIMIT_DIALOG[2:]], False, id="inline-dialog"),
        pytest.param([], False, id="nothing"),
    ],
)
def test_codex_reads_idle_only_at_its_empty_prompt_in_either_screen(
    screen: list[str], idle: bool
) -> None:
    """Measured on 0.162.1: ``? for shortcuts`` ends the alternate screen at an empty
    prompt, and the inline layout never draws it; there the empty composer's placeholder,
    the footer alone under it, is the prompt. A dialog or a draft is neither."""
    read = CODEX.capabilities.idle_on_screen
    assert read is not None and read(screen) is idle


def test_the_status_lines_words_in_a_command_awaiting_approval_keep_the_bell(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    codex, _ = codex_agent
    pane = codex.agent.pane_id
    codex.start(THREAD_ONE)
    _turn_under(codex, THREAD_ONE, "user")
    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)
    tmux.printed(pane, ago=timedelta(seconds=-2))

    for screen in (CRAFTED, SHAPED):
        tmux.screens[pane] = screen
        assert _state(codex) == "attention"
    tmux.screens[pane] = APPROVED  # 'y'
    assert _state(codex) == "working"


#: Codex 0.162.1 mid-turn in a window started with no ``COLORTERM`` (measured under tmux
#: 3.7c, a 120x40 pane, ``TERM=tmux-256color``): its spinner blinks, here at its hollow
#: frame; with ``COLORTERM=truecolor`` it stayed ``•`` on every read.
BLINKED = [
    f"{CHEVRON} say hi",
    "◦ Working (15s • esc to interrupt)",
    f"{CHEVRON} Ask Codex to do anything",
    "  gpt-5 default · ~/w",
    "  ← for agents · ? for shortcuts                                ⚠ 1 warning · f2",
]


def test_codex_reads_working_through_both_frames_of_its_blinking_spinner(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """Review r4 of #113, finding 7: the status line had to start with ``•``, and Codex
    blinks its spinner ``•``/``◦`` when its terminal does not report truecolor, which a
    window gets only from a spawner that had ``COLORTERM``. After an approval, the row of
    a headless, SSH or cron spawn flipped between working and attention on alternate
    reads: the bell came back, and ``tell`` alternated between typing and a note."""
    read = CODEX.capabilities.working_on_screen
    filled = [row.replace("◦", "•", 1) for row in BLINKED]
    assert read is not None and read(BLINKED) and read(filled)
    codex, _ = codex_agent
    pane = codex.agent.pane_id
    codex.start(THREAD_ONE)
    _turn_under(codex, THREAD_ONE, "user")
    codex.hook("PermissionRequest", THREAD_ONE, **APPROVAL)
    tmux.printed(pane, ago=timedelta(seconds=-2))

    for screen in (BLINKED, filled, BLINKED):  # 'y', then the spinner's frames
        tmux.screens[pane] = screen
        assert _state(codex) == "working"


# --- a replacement runs the row's own agent (review r1 of #113, finding 1) ---------------


def test_a_codex_wrapper_row_restarts_as_codex_whatever_the_role_resolves_today(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fleet spawn --agent codex --bin <wrapper>``: nothing but the row says the wrapper
    is Codex, and the replacement re-resolved the role, landing on Claude Code."""
    wrapper = fakebin.executable_fake(
        tmp_path / "wrap-bin", "codex-wrap", windows="set /p line=", posix="read line"
    )
    fakebin.prepend_to_path(wrapper.parent, monkeypatch)
    agent = fleet_service.spawn(
        project, "coder", agent="codex", binary="codex-wrap", worktree=False
    ).agent
    assert agent.agent == "codex" and agent.binary == "codex-wrap"

    receipt = fleet_service.restart(project, agent.label)

    command = _command(tmux)
    assert receipt.started.agent == "codex"
    assert _flag(command, "--agent") == "codex" and _flag(command, "--command") == "codex-wrap"
    native = _native(command)
    assert "--approve-for-me" in native and "--name" not in native, native


def test_a_role_bound_to_another_agent_since_still_restarts_the_rows_agent(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Re-resolved, ``codex`` under a role now bound to Claude Code is refused ('runs
    codex, but claude-code was selected') — after the original was already stopped."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(agent="claude-code")
    save_config(config)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.started.agent == "codex" and _flag(_command(tmux), "--agent") == "codex"


def test_a_replay_its_agent_refuses_is_refused_before_anything_is_stopped(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    _recorded(agent, agent="claude-code")  # agent and binary disagree, as recorded
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError, match="runs codex, but claude-code"):
        fleet_service.restart(project, agent.label)

    assert tmux.typed == [] and agent.pane_id not in tmux.killed
    assert len(tmux.spawned) == spawned


# --- nothing a sandbox writes in the store chooses what runs outside it -----------------
# (review r2 of #113, finding 3; r3, findings 1, 2, 3 and 5; r4, findings 1, 2, 5, 6 and
# 10: `core.launch_seal`)


#: How a replay of a row with no launch record to take is refused.
UNRECORDED = "no launch record was written for it"


def _recorded(row: FleetAgent, **fields: object) -> launch_seal.LaunchRecord:
    """``row``'s launch record with ``fields`` changed, as its spawn would have written
    it: only a spawner writes one (a sandbox cannot)."""
    record = launch_seal.read(row.id)
    assert isinstance(record, launch_seal.LaunchRecord), record
    record = record.model_copy(update=fields)
    assert launch_seal.write(record) is None
    return record


def _tamper(agent: FleetAgent, path: str, value: object) -> None:
    """A write to the row's launch spec in the store, as a sandboxed agent can make one."""
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute(
            "UPDATE fleet_agent SET launch_spec = json_set(launch_spec, ?, json(?)) WHERE id = ?",
            (path, json.dumps(value), agent.id),
        )
        store._conn.commit()


def _rewrite(table: str, column: str, value: object, row_id: str) -> None:
    """A write to one column of one row in the store, as a sandboxed agent can make one."""
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (value, row_id))
        store._conn.commit()


def test_the_store_profile_keeps_the_whole_store_out_of_the_sandboxs_reach() -> None:
    """The store is read-only from Codex's sandbox, all of it: the board's database, the
    digests of the launch specs a replay runs, the census, the tmux conf every fleet
    server loads, the registry ``upgrade`` reconnects hooks by, the model probe's cache
    (review r9 of #113, finding 14), keys and sign-in tokens. Its board calls go through
    its broker (``services.store_broker``). No path is denied reading: with one, Codex
    reads its own instructions through its sandbox helper, and on a host whose bwrap
    cannot bring up loopback (a GitHub runner) the session did not start (CI on
    ca6d43ea)."""
    store = store_dir()
    args = CODEX.store_access("workspace", store)
    table = args[args.index("-c", 4) + 1].partition("=")[2]

    assert tomllib.loads(f"t = {table}")["t"] == {str(store): "read"}


def test_the_store_profile_names_a_windows_home_as_codex_reads_it() -> None:
    """A Windows home's paths go into Codex's ``-c`` as TOML strings, each backslash
    escaped, and come out of TOML as Windows spells them. Asserted on any platform: the
    contract test that looked for ``C:\\Users\\…`` in the command line as written failed on
    the Windows runner only (CI on e8f95719), where the escaped spelling is the right one."""
    store = PureWindowsPath(r"C:\Users\me\.aisquare")
    inbox = store / "broker" / "launch-1" / "in"
    args = CODEX.store_access("workspace", cast(Path, store))
    table = args[args.index("-c", 4) + 1].partition("=")[2]

    assert r'"C:\\Users\\me\\.aisquare" = "read"' in table, "TOML's escape, as Codex reads it"
    assert tomllib.loads(f"t = {table}")["t"] == {r"C:\Users\me\.aisquare": "read"}
    launched = CODEX.broker_access(args, cast(Path, store), cast(Path, inbox))
    assert launched is not None
    entries = tomllib.loads(f"t = {launched[-1].partition('=')[2]}")["t"]
    assert entries == {
        r"C:\Users\me\.aisquare": "read",
        r"C:\Users\me\.aisquare\broker\launch-1\in": "write",
    }


def test_a_sandboxed_launch_makes_what_its_profile_names_before_the_sandbox_can() -> None:
    """Codex's sandbox leaves an empty file on the host where a path its profile names did
    not exist (0.162.1, measured), which no ``mkdir`` then makes. The launcher makes the
    store, and the launch records' directory; the broker's inbox is made where the broker
    is started (``services.store_broker.start_for_launch``)."""
    store = store_dir()
    shutil.rmtree(store, ignore_errors=True)
    prepare_store(store)

    assert store.is_dir()
    assert (store / launch_seal.DIR_NAME).is_dir()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("$.binary", "/tmp/x"),  # the program a replay runs
        ("$.extra_args", ["-c", 'notify=["sh","-c","/tmp/x"]']),  # one Codex runs
        ("$.permission_mode", "full:never"),
        ("binary", "/tmp/x"),  # the program `fleet tell` queued with
        ("role", "coder"),  # a reviewer's read-only seat came back workspace-write
        ("agent", "claude-code"),  # a Codex row came back with no OS sandbox
        ("cwd", "/tmp"),  # a directory the sandbox wrote
        ("worktree", 1),
        ("account_slot", 7),
        ("launch_spec", None),  # no spec: the role as it resolved today
    ],
)
def test_nothing_a_sandbox_writes_in_a_row_reaches_its_replacement(
    column: str,
    value: object,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The store is writable from Codex's sandbox and a row's launch is replayed outside
    it, by restart, switch and the usage-limit hand-over. Each round sealed one more of
    the row's columns and the next was found open (r2 3, r3 1, 2, 3 and 5, r4 1, 2, 5 and
    6). A replay reads its launch record and nothing else, so a row rewritten in the
    store, any column of it, starts the replacement exactly as the spawn recorded it."""
    agent = fleet_service.spawn(project, "reviewer", agent="codex", worktree=False).agent
    spawned = _command(tmux)
    if column.startswith("$."):
        _tamper(agent, column, value)
    else:
        _rewrite("fleet_agent", column, value, agent.id)

    receipt = fleet_service.restart(project, agent.label)

    command = _command(tmux)
    assert "/tmp/x" not in " ".join(command)
    assert command[command.index("launch") : command.index("launch") + 2] == ["launch", "reviewer"]
    assert _flag(command, "--agent") == "codex" and _flag(command, "--account") is None
    replayed = _native(command)
    assert replayed[: replayed.index("--")] == _native(spawned), "the reviewer's own flags"
    assert tmux.spawned[-1]["cwd"] == project.root
    assert receipt.started.role == "reviewer" and receipt.started.agent == "codex"


def test_a_project_directory_the_store_chose_is_refused(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """r4 finding 2: a replay outside a worktree started in ``project.root``, and the
    hand-over, asq's Restart and ``-p`` read the project from the store: a sandbox's
    ``UPDATE project SET root`` chose the directory the unsandboxed replacement started
    in, its hooks, config and instructions with it. It has to be the record's."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    elsewhere = tmp_path / "sandbox-chose"
    elsewhere.mkdir()
    _rewrite("project", "root", str(elsewhere), project.id)
    with store_session() as store:
        stored = store.get_project(project.id)
    assert stored is not None and stored.root == elsewhere
    spawned = len(tmux.spawned)

    for replay in (
        lambda: fleet_service.restart(stored, agent.label),
        lambda: fleet_service.switch(stored, agent.label, automatic=True),
    ):
        with pytest.raises(fleet_service.FleetError, match="is not the one it was spawned in"):
            replay()

    assert len(tmux.spawned) == spawned and tmux.typed == []


def test_a_replay_with_no_record_is_refused_and_none_is_minted(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """r4 finding 5: until the directory was marked ready every run sealed the store as
    it stood, and a sandboxed agent that kept it unready could have its rewritten row
    sealed. The store is read for records once, by the census of the fleet as it stood
    before any record was kept (review r8 of #113, rule A), taken before this spawn
    started its agent; a row whose record is lost since is refused, with the way out, and
    stays refused: the census is never taken again."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    assert launch_seal.census_taken() and launch_seal.legacy_records(project.id) == []
    launch_seal._path(agent.id).unlink()  # its record lost, since

    for _ in range(2):
        with pytest.raises(fleet_service.FleetError, match=UNRECORDED) as refused:
            fleet_service.restart(project, agent.label)
        assert "and spawn a new agent" in str(refused.value)
    assert not launch_seal._path(agent.id).exists()
    assert launch_seal.legacy_records(project.id) == [], "nothing minted from the store"


def test_a_refusal_is_said_on_the_board_once_per_row_and_reason(
    codex_agent: tuple[Codex, TeamTask],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMOKE3 on 8a83e6b0's predecessor: a refused replay told only its caller, so an
    unattended hand-over and a human reading the board saw no reason the row was not
    replaced. The reason goes on the board, once per row and reason, whichever path met
    it first: restart, switch, the hand-over, or a message told to the busy agent."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    launch_seal._path(codex.agent.id).unlink()
    monkeypatch.setattr(fleet_service, "_run_native", Mock(side_effect=AssertionError("ran")))

    for replay in (
        lambda: fleet_service.restart(codex.project, codex.agent.label),
        lambda: fleet_service.restart(codex.project, codex.agent.label),
        lambda: fleet_service.switch(codex.project, codex.agent.label, automatic=True),
    ):
        with pytest.raises(fleet_service.LaunchRefused):
            replay()
    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    said = [text for text in _events(codex.project, "note") if UNRECORDED in text]
    assert said == [
        f"{codex.agent.label}: cannot restart it — no launch record was written for it (it was "
        "spawned by an aisquare that keeps none, or its record could not be written), and it "
        "was not in the fleet when this aisquare first read it; end its agent in its own "
        f"window, then `aisquare fleet stop {codex.agent.label}` removes the window and ends "
        "its row, and spawn a new agent"
    ]
    assert told.delivered is False and UNRECORDED in told.how
    # Another reason for the same row is said again.
    launch_seal._path(codex.agent.id).write_text('{"version": 99}', encoding="utf-8")
    with pytest.raises(fleet_service.LaunchRefused):
        fleet_service.restart(codex.project, codex.agent.label)
    assert len([text for text in _events(codex.project, "note") if "cannot restart" in text]) == 2


def test_a_refusal_whose_note_was_not_written_is_said_by_the_next(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r5 of #113, finding 5: the reason was marked said before its note was
    written, so a note the store refused (busy past its timeout) silenced that reason for
    the row for good, the unattended hand-over's included."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    launch_seal._path(agent.id).unlink()
    emit = team_service._emit

    def busy(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(team_service, "_emit", busy)
    with pytest.raises(fleet_service.LaunchRefused):
        fleet_service.restart(project, agent.label)
    monkeypatch.setattr(team_service, "_emit", emit)
    with pytest.raises(fleet_service.LaunchRefused):
        fleet_service.restart(project, agent.label)

    said = [text for text in _events(project, "note") if UNRECORDED in text]
    assert len(said) == 1 and said[0].startswith(f"{agent.label}: cannot restart it")


@pytest.mark.parametrize("kind", ["codex", "claude-code"])
@pytest.mark.parametrize("seals", ["a file in its place", "a read-only directory"])
def test_a_spawn_refused_for_its_record_makes_no_worktree_and_captures_no_project(
    seals: str,
    kind: str,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Review r5 of #113, finding 9: refused once the codename and the worktree were made,
    a spawn whose record could not be written left a captured project and a new worktree
    and branch for an agent that never started. Asked first, as spawn's #139 order asks
    every refusal that needs no window, by writing in the directory: SMOKE4 found one that
    is there but read-only refused only at the record's own write. No agent starts without
    its record: it holds the pane every ``tell`` and ``stop`` is aimed at."""
    directory = launch_seal.seals_dir()
    if seals == "a file in its place":
        shutil.rmtree(directory, ignore_errors=True)
        directory.write_text("not a directory", encoding="utf-8")
    else:
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("a mode that refuses this user a write is a POSIX non-root fact")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o555)
    with store_session() as store:
        before = store.get_project(project.id)
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=project.root, capture_output=True, text=True, check=True
    ).stdout

    try:
        with pytest.raises(fleet_service.FleetError, match="no agent is started without one"):
            fleet_service.spawn(project, "coder", agent=kind, worktree=True)
    finally:
        if directory.is_dir():
            directory.chmod(0o755)

    with store_session() as store:
        assert store.get_project(project.id) == before, "not captured, no codename"
    assert not (project.root / fleet_service.settings().worktree_dir).exists()
    assert (
        subprocess.run(
            ["git", "branch", "--list"],
            cwd=project.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == branches
    )
    assert tmux.spawned == []


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("{not json", "could not be read"),
        ('{"version": 99}', "could not be read"),
    ],
)
def test_a_record_that_cannot_be_read_is_refused(
    body: str,
    why: str,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    launch_seal._path(agent.id).write_text(body, encoding="utf-8")

    with pytest.raises(fleet_service.FleetError, match=why):
        fleet_service.restart(project, agent.label)


def test_a_record_a_newer_aisquare_wrote_is_refused_naming_the_upgrade(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """r4 finding 10: a digest over today's model changed with every field a release
    adds, and refused every running agent's replay as tampered. A record is read back as
    written, a field it lacks takes its default; only a newer record's version refuses."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    path = launch_seal._path(agent.id)
    written = json.loads(path.read_text(encoding="utf-8"))
    del written["spec"]["recorded_by"]  # a field an older release did not write
    path.write_text(json.dumps(written), encoding="utf-8")
    assert isinstance(launch_seal.read(agent.id), launch_seal.LaunchRecord)
    path.write_text(json.dumps({**written, "version": launch_seal.VERSION + 1}), encoding="utf-8")

    with pytest.raises(fleet_service.FleetError, match="written by a newer aisquare"):
        fleet_service.restart(project, agent.label)


def test_an_agent_that_sandboxes_its_tools_is_not_started_without_its_record(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """r4 finding 5's other half: ``prepare_store`` started a sandboxed agent whatever
    became of the seals. A spawn that cannot write the record refuses a Codex agent
    before its window, and says why."""
    shutil.rmtree(launch_seal.seals_dir(), ignore_errors=True)
    launch_seal.seals_dir().write_text("not a directory", encoding="utf-8")
    spawned = len(tmux.spawned)

    with pytest.raises(
        fleet_service.FleetError, match="no agent is started without one"
    ) as refused:
        fleet_service.spawn(project, "coder", agent="codex", worktree=False)

    assert "could not be made" in str(refused.value) and len(tmux.spawned) == spawned


@pytest.mark.parametrize("forged", ["../planted", "a/../../planted", "{absolute}"])
def test_a_row_id_the_store_chose_never_names_the_record_that_is_read(
    forged: str,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """r3 finding 3: the seal's file was the row's id joined into the path, so an id with
    ``..`` or an absolute one named a file the sandbox wrote. The record's file is named
    by the id's own digest."""
    forged = forged.format(absolute=tmp_path / "planted")
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    planted = _recorded(agent).model_copy(update={"id": forged, "binary": "/tmp/x"})
    _rewrite("fleet_agent", "id", forged, agent.id)
    # Where a lookup that joined the id into the path read the record, planted.
    target = Path(os.path.normpath(launch_seal.seals_dir() / f"{forged}.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(planted.model_dump_json(), encoding="utf-8")

    with pytest.raises(fleet_service.FleetError, match=UNRECORDED):
        fleet_service.restart(project, agent.label)

    assert "/tmp/x" not in _command(tmux)
    assert launch_seal._path(forged).parent == launch_seal.seals_dir()


@pytest.mark.parametrize(
    "native", ["--dangerously-bypass-approvals-and-sandbox", "--last", "a-session-name"]
)
def test_a_native_id_the_store_chose_never_reaches_codex_resume(
    native: str,
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """r3 finding 2: ``codex resume <id>`` reads an id that starts with ``-`` as one of its
    own options (``-c``, ``-s``, ``--dangerously-bypass-approvals-and-sandbox``: 0.162.1's
    ``--help``) and any other word as a session's name. The id came from the session row,
    which the sandbox writes. It is the record's now, as the agent's own start hook
    reported it, and only a UUID even there."""
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    _rewrite("team_session", "native_session_id", native, codex.board_id(THREAD_ONE))

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.resumed is True and native not in _command(tmux)
    at = _native(_command(tmux)).index("resume")
    assert _native(_command(tmux))[at : at + 2] == ["resume", THREAD_ONE]

    session = _recorded(receipt.started).session
    assert session is not None
    _recorded(receipt.started, session=session.model_copy(update={"native_id": native}))
    again = fleet_service.restart(codex.project, codex.agent.label)
    assert again.resumed is False and native not in _command(tmux)
    assert any("is not one Codex makes" in note for note in again.notes), again.notes


def test_a_transcript_the_store_names_is_never_resumed(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """r4 finding 1: Claude Code resumes ``--resume <transcript>``, the path from the
    session row: a sandboxed Codex agent pointed any Claude row's session at a file it
    wrote, and the unsandboxed replacement started on a conversation of its making, its
    user turns included. The transcript is the one the agent's own hook reported."""
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    assert agent.session_id is not None
    real = tmp_path / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    forged = tmp_path / "sandbox-wrote" / "evil.jsonl"
    forged.parent.mkdir()
    forged.write_text('{"type":"user"}\n', encoding="utf-8")
    session = launch_seal.SessionFacts(
        id=agent.session_id, agent="claude-code", native_id=agent.session_id, transcript=str(real)
    )
    _recorded(agent, session=session)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now,
                last_seen_at=now,
                transcript_path=str(forged),
            )
        )
    tmux.die(agent.pane_id, 1)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.resumed is True
    assert _flag(_command(tmux), "--resume") == str(real) and str(forged) not in _command(tmux)


def test_tell_queues_with_the_records_binary_thread_and_home_never_the_stores(
    codex_agent: tuple[Codex, TeamTask],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """r3 finding 1: ``fleet tell`` to a busy Codex agent ran the row's ``binary`` as
    ``<binary> queue …`` with the caller's rights, with ``--thread`` and ``CODEX_HOME``
    from the session row, and a sandboxed agent can rewrite all three. The queue runs
    with its launch record's, as its spawn and its own hook wrote them."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.toml").write_text('notify = ["/tmp/x"]\n', encoding="utf-8")
    _rewrite("fleet_agent", "binary", "/tmp/x", codex.agent.id)
    board = codex.board_id(THREAD_ONE)
    _rewrite("team_session", "native_session_id", "--remote=ws://127.0.0.1:9", board)
    _rewrite("team_session", "account", str(elsewhere), board)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fleet_service, "_run_native", run)

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is True, told.how
    [(argv, env)] = calls
    assert Path(argv[0]).name.startswith("codex") and "/tmp/x" not in argv
    assert argv[1:4] == ["queue", "--thread", THREAD_ONE]
    assert env["CODEX_HOME"] == str(codex.home)


def test_tell_queues_nothing_for_a_row_with_no_record(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    launch_seal._path(codex.agent.id).unlink()
    monkeypatch.setattr(fleet_service, "_run_native", Mock(side_effect=AssertionError("ran")))

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is False and UNRECORDED in told.how, told.how
    assert _notes_to(codex.project, codex.agent.label) == ["rebase on main first"]


def test_a_tool_shells_hook_never_writes_the_record(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Only the agent's own process reports its session: a ``codex exec`` one of its tools
    starts (``CODEX_THREAD_ID`` set, and inside the sandbox besides) records nothing."""
    codex, _ = codex_agent
    codex.child(THREAD_TWO)
    codex.start(THREAD_THREE)
    record = launch_seal.read(codex.agent.id)
    assert isinstance(record, launch_seal.LaunchRecord) and record.session is None

    codex.become()
    codex.start(THREAD_ONE)
    record = launch_seal.read(codex.agent.id)
    assert isinstance(record, launch_seal.LaunchRecord) and record.session is not None
    assert record.session.native_id == THREAD_ONE


# --- one permission per role, mapped per agent (acceptance 4) ----------------------------


def _spawn_native(server: FakeTmux) -> list[str]:
    """The agent's own arguments in the last window's command, the positional prompt aside."""
    native = _native(_command(server))
    return native[: native.index("--")] if "--" in native else native


#: Who answers Codex's approvals, pinned (``approvals_reviewer``).
HUMAN = ["-c", 'approvals_reviewer="user"']
REVIEWED = ["-c", 'approvals_reviewer="auto_review"']


def _codex_flags(*flags: str) -> list[str]:
    """``flags`` with ``STORE:<write scope>`` standing for Codex's store profile for that
    scope, the aisquare home being this test's (known only once the test runs)."""
    return [
        arg
        for flag in flags
        for arg in (
            CODEX.store_access(flag.partition(":")[2], store_dir())  # type: ignore[arg-type]
            if flag.startswith("STORE:")
            else [flag]
        )
    ]


@pytest.mark.parametrize(
    ("intent", "claude", "codex"),
    [
        ("workspace:auto", ["--permission-mode", "auto"], ["--approve-for-me", "STORE:workspace"]),
        (
            "workspace:ask",
            ["--permission-mode", "manual"],
            ["--ask-for-approval", "on-request", *HUMAN, "STORE:workspace"],
        ),
        (
            "full:never",
            ["--permission-mode", "bypassPermissions"],
            ["--ask-for-approval", "never", "--sandbox", "danger-full-access"],
        ),
    ],
)
def test_one_role_permission_maps_to_each_agents_flags(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    intent: str,
    claude: list[str],
    codex: list[str],
) -> None:
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False, permission=intent)
    save_config(config)

    on_claude = fleet_service.spawn(project, "coder", agent="claude-code").agent
    claude_native = _spawn_native(tmux)
    on_codex = fleet_service.spawn(project, "coder", agent="codex").agent
    codex_native = _spawn_native(tmux)

    assert claude_native[: len(claude)] == claude and "--sandbox" not in claude_native
    codex = _codex_flags(*codex)
    assert codex_native[: len(codex)] == codex and "--permission-mode" not in codex_native
    for row in (on_claude, on_codex):
        assert row.launch_spec is not None and row.launch_spec.permission_mode == intent


def test_the_reviewers_read_only_seat_is_denied_its_editing_tools_once(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The reviewer's built-in role arguments already deny the tools that edit files;
    ``read_only`` on Claude Code is that same denial, given once."""
    config = load_config()
    config.fleet.roles["reviewer"] = config.fleet.roles["reviewer"].model_copy(
        update={"worktree": False, "permission": "read_only:auto"}
    )
    save_config(config)

    fleet_service.spawn(project, "reviewer", agent="claude-code")
    claude = _spawn_native(tmux)
    fleet_service.spawn(project, "reviewer", agent="codex")
    codex = _spawn_native(tmux)

    assert claude.count(REVIEWER_ARGS[0]) == 1 and _flag(claude, "--permission-mode") == "auto"
    seat = _codex_flags("--ask-for-approval", "on-request", *REVIEWED, "STORE:read_only")
    assert codex[: len(seat)] == seat


def test_a_codex_reviewer_on_the_built_in_config_spawns_without_the_legacy_note(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """SMOKE2: every Codex reviewer spawned on a fresh config was told 'Legacy fleet
    extra_args apply only to Claude Code', about the reviewer default ``init`` writes and
    no operator chose — a Codex manager read it in its tool output on every spawn."""
    config = load_config()
    reviewer = config.fleet.roles["reviewer"].model_copy(update={"worktree": False})
    assert reviewer.extra_args == list(REVIEWER_ARGS), "what init writes"
    config.fleet.roles["reviewer"] = reviewer
    save_config(config)

    on_codex = fleet_service.spawn(project, "reviewer", agent="codex")
    codex = _spawn_native(tmux)
    on_claude = fleet_service.spawn(project, "reviewer", agent="claude-code")
    claude = _spawn_native(tmux)

    assert not [note for note in [*on_codex.notes, *on_claude.notes] if "Legacy" in note]
    assert REVIEWER_ARGS[0] not in codex, "Claude Code's flag, never Codex's"
    assert claude.count(REVIEWER_ARGS[0]) == 1, "Claude Code's reviewer keeps it"
    # The operator's own legacy arguments still say where Codex's go.
    config.fleet.roles["reviewer"] = reviewer.model_copy(
        update={"extra_args": [*REVIEWER_ARGS, "--model", "opus"]}
    )
    save_config(config)
    told = fleet_service.spawn(project, "reviewer", agent="codex").notes
    assert any(
        "Legacy fleet extra_args apply only to Claude Code" in note
        and "fleet.roles.reviewer.agent_args.codex" in note
        for note in told
    ), told


def test_a_spawns_intent_wins_over_the_roles_and_a_restart_maps_it_again(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False, permission="workspace:auto")
    save_config(config)
    agent = fleet_service.spawn(
        project, "coder", agent="codex", permission_mode="read_only:never"
    ).agent
    flags = _codex_flags("--ask-for-approval", "never", "STORE:read_only")
    assert _spawn_native(tmux)[: len(flags)] == flags

    fleet_service.restart(project, agent.label)

    assert _spawn_native(tmux)[: len(flags)] == flags


def test_an_intent_is_refused_before_a_window_beside_a_native_flag_or_where_an_agent_cannot(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    with pytest.raises(fleet_service.FleetError, match="without --sandbox or --approval"):
        fleet_service.spawn(
            project, "coder", agent="codex", permission_mode="workspace:ask", sandbox="read-only"
        )
    with pytest.raises(fleet_service.FleetError, match="only in the workspace-write sandbox"):
        fleet_service.spawn(project, "coder", agent="codex", permission_mode="full:auto")
    with pytest.raises(fleet_service.FleetError, match="WRITE:APPROVALS"):
        fleet_service.spawn(project, "coder", agent="codex", permission_mode="workspace:maybe")
    assert tmux.spawned == []
    # A native flag for one spawn wins over the role's one value.
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False, permission="workspace:auto")
    save_config(config)
    fleet_service.spawn(project, "coder", agent="codex", sandbox="read-only")
    native = _spawn_native(tmux)
    flags = _codex_flags(*HUMAN, "STORE:read_only")
    assert "--approve-for-me" not in native and native[: len(flags)] == flags


def test_a_restart_refuses_a_permission_the_rows_agent_cannot_take_before_stopping_it(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """A Claude Code mode on a Codex row stopped the agent for a replacement Codex refused."""
    codex, _ = codex_agent
    spawned = len(tmux.spawned)
    for refused in ("auto", "full:auto"):
        with pytest.raises(fleet_service.FleetError, match="nothing was stopped"):
            fleet_service.restart(codex.project, codex.agent.label, permission_mode=refused)
    assert tmux.typed == [] and len(tmux.spawned) == spawned

    receipt = fleet_service.restart(
        codex.project, codex.agent.label, permission_mode="workspace:ask"
    )

    # The human answers on the resumed thread, which kept Codex's reviewer otherwise
    # (t8 smoke: the restarted coder's /permissions still read "Approve for me").
    flags = _codex_flags("--ask-for-approval", "on-request", *HUMAN, "STORE:workspace")
    assert _spawn_native(tmux)[: len(flags)] == flags
    assert "--approve-for-me" not in _spawn_native(tmux)
    spec = receipt.started.launch_spec
    assert spec is not None and spec.permission_mode == "workspace:ask"


def test_a_restart_checks_the_permission_against_the_agent_its_replacement_runs(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    """Review r2 of #113, finding 8: the check asked the agent whose hook wrote the row's
    session, the replacement runs the row's own. A Codex row whose session another
    agent's hook wrote (a Claude Code mode passes there) was stopped for a Codex
    replacement that refused the mode."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute(
            "UPDATE team_session SET agent = 'claude-code' WHERE id = ?",
            (codex.board_id(THREAD_ONE),),
        )
        store._conn.commit()
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError, match="nothing was stopped"):
        fleet_service.restart(codex.project, codex.agent.label, permission_mode="auto")

    assert tmux.typed == [] and codex.agent.pane_id not in tmux.killed
    assert len(tmux.spawned) == spawned


def test_the_settings_line_shows_one_permission_for_every_agent() -> None:
    settings = FleetRoleSettings(permission="workspace:auto")
    line = launch_line("coder", "codex", settings).plain
    assert line.index("Codex --approve-for-me") < line.index("Claude Code --permission-mode auto")
    assert "workspace:auto" in line and "--name" not in line
    # The store's profile is the same long run for every role: named, not spelled out.
    assert "+ the aisquare store, read-only in its workspace sandbox" in line
    assert "permissions.aisquare" not in line
    reviewer = launch_line("reviewer", "codex", FleetRoleSettings()).plain
    assert 'approvals_reviewer="auto_review"' in reviewer and "its read_only sandbox" in reviewer
    refused = launch_line("coder", "claude-code", FleetRoleSettings(permission="full:auto")).plain
    assert "Claude Code --permission-mode auto" in refused
    assert "Codex: refused at spawn" in refused


def test_an_intent_is_classifier_auto_only_where_its_approvals_are_auto() -> None:
    assert auto_mode.is_auto("auto") and auto_mode.is_auto("read_only:auto")
    assert not auto_mode.is_auto("workspace:ask") and not auto_mode.is_auto("acceptEdits")
    assert not auto_mode.is_auto("nonsense:auto") and not auto_mode.is_auto(None)
    roles = FleetSettings(
        roles={
            "coder": FleetRoleSettings(permission="workspace:never"),
            "tester": FleetRoleSettings(permission_mode="manual", permission="workspace:auto"),
        }
    )
    assert "coder" not in auto_mode.auto_roles(roles) and "tester" in auto_mode.auto_roles(roles)


def test_every_read_of_a_row_names_its_own_session_over_its_launch_binding(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Review r1 of #113, finding 7: ``fleet_agents`` (``fleet ls``, the sidebar) put the
    launch's binding over a row's own session id while ``get_fleet_agent`` kept the
    row's, so two reads of one row named two sessions. The row's own id comes first in
    every read; the binding fills only a row that has none."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    first = codex.board_id(THREAD_ONE)
    with store_session() as store:
        row = store.get_fleet_agent(codex.agent.id)
        assert row is not None and row.session_id == first, "the launch's binding fills it"
        store.upsert_fleet_agent(row.model_copy(update={"session_id": "its-own-id"}))
        store.set_meta(f"fleet-session:{codex.agent.id}", "a-stale-binding")
        listed = [
            agent.session_id
            for agent in store.fleet_agents(codex.project.id)
            if agent.id == codex.agent.id
        ]
        got = store.get_fleet_agent(codex.agent.id)
        by_session = store.fleet_agent_for_session(codex.project.id, "its-own-id")
        stale = store.fleet_agent_for_session(codex.project.id, "a-stale-binding")
    assert listed == ["its-own-id"] and got is not None and got.session_id == "its-own-id"
    assert by_session is not None and by_session.id == codex.agent.id and stale is None


# --- the role's model and effort at fleet spawn and launch (acceptance 5) ----------------


def _pair(argv: list[str], option: str) -> str | None:
    return argv[argv.index(option) + 1] if option in argv else None


def test_fleet_spawn_passes_the_roles_model_and_effort_for_both_agents(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Claude Code on its role's ladder, as ``team spawn`` resolves it (no paid probe at a
    spawn: the first rung), and Codex on ``[agents.models.codex]``; then
    ``[agents.models.claude-code]`` over the ladder."""
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    config.agents.models["codex"] = AgentModelSettings(model="gpt-test", effort="high")
    save_config(config)
    level, _source = harness.resolve_effort("coder")

    fleet_service.spawn(project, "coder", agent="claude-code")
    claude = _spawn_native(tmux)
    fleet_service.spawn(project, "coder", agent="codex")
    codex = _spawn_native(tmux)

    assert (_pair(claude, "--model"), _pair(claude, "--effort")) == ("sonnet", level)
    assert _pair(codex, "--model") == "gpt-test"
    assert 'model_reasoning_effort="high"' in codex and "--effort" not in codex

    config = load_config()
    config.agents.models["claude-code"] = AgentModelSettings(
        roles={"coder": AgentModelDefault(model="opus", effort="xhigh")}
    )
    save_config(config)
    fleet_service.spawn(project, "coder", agent="claude-code")
    pinned = _spawn_native(tmux)
    assert (_pair(pinned, "--model"), _pair(pinned, "--effort")) == ("opus", "xhigh")


def test_a_hand_launch_keeps_claude_codes_argv_and_gives_codex_its_pins(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
) -> None:
    """``aisquare launch`` by hand is ``AISQUARE_ROLE=<role> <agent>``: Claude Code's argv
    as it always was (tests/test_agent_config_regressions.py), Codex's with what is
    configured for it. The ladder and Claude Code's pins are the fleet's (above)."""
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr("aisquare.cli.launch._await_fleet_row", lambda: None)

    def argv(*args: str) -> list[str]:
        result = runner.invoke(app, ["launch", "coder", *args])
        assert result.exit_code == 0, result.output
        launched: list[str] = execute.call_args.args[1]
        return launched

    config = load_config()
    config.agents.models["claude-code"] = AgentModelSettings(model="opus")
    config.agents.models["codex"] = AgentModelSettings(effort="low")
    save_config(config)
    assert "--model" not in argv("--agent", "claude-code")
    assert 'model_reasoning_effort="low"' in argv("--agent", "codex")


def test_a_codex_ui_tester_spawns_without_claude_codes_flags(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    on_codex = fleet_service.spawn(project, "ui-tester", agent="codex")
    codex = _command(tmux)
    fleet_service.spawn(project, "ui-tester", agent="claude-code")
    claude = _command(tmux)

    assert "--chrome" not in codex and "--name" not in codex
    assert not any("--chrome" in note for note in on_codex.notes), on_codex.notes
    assert "--chrome" in claude, "Claude Code's ui-tester keeps its browser flag"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (
            '[plugins."browser@openai-bundled"]\nenabled = true\n',
            "the browser_use feature and the browser@openai-bundled plugin",
        ),
        ("[features]\nbrowser_use = false\n", "no browser of its own"),
    ],
)
def test_the_doctors_browser_row_reads_a_codex_ui_testers_own_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, expected: str
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    settings = load_config()
    settings.team.profiles["ui-tester"] = RoleLaunchProfile(agent="codex")
    save_config(settings)

    row = diagnostics._check_browser_tools(tmp_path)

    assert row.status.value == "ok" and "the ui-tester runs Codex" in row.detail
    assert expected in row.detail and "Claude in Chrome" not in row.detail


def test_key_chords_are_sent_in_the_shown_agents_own_spelling() -> None:
    codex, claude = get_adapter("codex"), get_adapter("claude-code")

    def sent(key: str, adapter: AgentAdapter, *, extended: bool = True) -> Translation | Drop:
        return translate(
            key,
            None,
            printable=False,
            extended_keys=extended,
            spellings=adapter.key_spellings,
        )

    # Codex submits on shift+enter and takes ctrl+J as its newline (0.162.1).
    assert sent("shift+enter", codex) == Translation("key", "C-j")
    assert sent("shift+enter", claude) == Translation("key", "S-Enter")
    assert sent("shift+enter", claude, extended=False) == Translation("key", "C-j")
    for adapter in (codex, claude):
        # Esc interrupts a turn, shift+tab cycles the mode, alt+p is forwarded as is.
        assert sent("escape", adapter) == Translation("key", "Escape")
        assert sent("shift+tab", adapter) == Translation("key", "BTab")
        assert translate(
            "alt+p", "p", printable=True, spellings=adapter.key_spellings
        ) == Translation("key", "M-p")
    row = FleetAgent(
        id="row",
        project_id="prj",
        label="coder-1",
        role="coder",
        agent="codex",
        binary="codex",
        cwd=Path("."),
        tmux_socket="test",
        pane_id="%1",
        created_at=datetime.now(UTC),
    )
    status = FleetAgentStatus(agent=row)
    assert key_spellings(status) == {"S-Enter": "C-j"}
    assert key_spellings(None) == {}


def test_a_prompt_steered_into_a_running_turn_is_its_own_prompt(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Codex 0.162.1 fires ``UserPromptSubmit`` for a prompt typed while a turn runs under
    that turn's id. Deduplicated by the turn alone it read as a redelivery of the first:
    not recorded, no heartbeat, the first prompt's reply handed back (a ``fleet tell``
    typed as the turn ended was lost from the board). A true redelivery still answers
    as the first time."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="slow work", turn_id="turn-1")
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="steered in", turn_id="turn-1")
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="steered in", turn_id="turn-1")

    with store_session() as store:
        said = [prompt.text for prompt in store.recent_prompts(codex.project.id)]
    assert said.count("steered in") == 1 and said.count("slow work") == 1, said


def test_a_turn_a_prompt_was_steered_into_after_its_stop_stops_again(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """The other half of the steer: typed as the turn ended, the prompt reopens it, and
    Codex's next Stop under the same turn id is a new stop — the agent waits again. A
    Stop redelivered with nothing in between still answers as the first did."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    first = codex.board_id(THREAD_ONE)

    def state() -> str | None:
        with store_session() as store:
            session = store.get_session(first)
        return session.state if session is not None else None

    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    codex.hook("Stop", THREAD_ONE, turn_id="turn-1", stop_hook_active=False)
    assert state() == "waiting"
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="one more thing", turn_id="turn-1")
    assert state() == "working"
    codex.hook("Stop", THREAD_ONE, turn_id="turn-1", stop_hook_active=False)
    assert state() == "waiting"
    with store_session() as store:
        store.touch_session(first, state="working")
    codex.hook("Stop", THREAD_ONE, turn_id="turn-1", stop_hook_active=False)
    assert state() == "working", "a redelivered Stop is answered from the cache"


def test_a_codex_row_on_a_codex_slot_restarts_on_that_slot(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The restart resolved the row's slot as a Claude Code account, and refused the
    plain codex (slot 0) as "a Codex account, and this launch runs Claude Code" (caught
    against the real binary by tests/test_codex_native.py)."""
    slot = accounts_core.create_account("codex")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    ).agent
    assert agent.account_slot == slot.slot

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.started.account_slot == slot.slot and receipt.started.agent == "codex"
    assert _flag(_command(tmux), "--account") == str(slot.slot)


def test_a_switch_resumes_only_onto_the_home_the_thread_ran_under(tmp_path: Path) -> None:
    """A Codex thread lives under its CODEX_HOME: a target on that home resumes it, any
    other starts fresh (``capabilities.resumes_across_homes``)."""
    home, other = tmp_path / "codex-a", tmp_path / "codex-b"
    home.mkdir()
    other.mkdir()
    spec = LaunchSpec(binary="codex")
    record = launch_seal.LaunchRecord(
        id="agt_x",
        project_id="prj",
        project_root=str(tmp_path),
        role="coder",
        agent="codex",
        binary="codex",
        cwd=str(tmp_path),
        account_home=str(other),
        spec=spec,
        session=launch_seal.SessionFacts(id="s", agent="codex", home=str(home)),
    )

    def account(where: Path) -> ClaudeAccount:
        return ClaudeAccount(slot=7, config_dir=where, managed=True, agent="codex")

    assert fleet_service._same_home(record, account(home)), "the session's, as its hook said"
    assert fleet_service._same_home(record, account(home / "." / ""))
    assert not fleet_service._same_home(record, account(other))
    unstarted = record.model_copy(update={"session": None})
    assert fleet_service._same_home(unstarted, account(other)), "else the spawn's account's"
    assert not fleet_service._same_home(
        unstarted.model_copy(update={"account_home": None}), account(home)
    )
    assert not get_adapter("codex").capabilities.resumes_across_homes


def test_the_fleet_refuses_a_codex_agent_whose_sandbox_may_write_its_aisquare(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r12 of #113, finding 9: the in-tree-install refusal was the window's own
    ``aisquare launch``'s alone, so a fleet spawn of a Codex agent in a project whose
    ``.venv`` holds aisquare took a receipt and a live row for a pane that died at once.
    The spawn refuses it first, naming the remedy, and makes neither window nor row."""
    from aisquare.services import store_broker

    inside = project.root / ".venv" / "bin" / "python"
    monkeypatch.setattr(store_broker, "install_paths", lambda *args, **kwargs: (inside,))

    with pytest.raises(fleet_service.FleetError, match="aisquare-cli"):
        fleet_service.spawn(project, "tester", agent="codex", worktree=False)

    assert tmux.sessions == {}
    with store_session() as store:
        assert store.fleet_agents(project.id) == []
    # A Claude Code agent runs its tools as its user does: no sandbox to keep it from.
    fleet_service.spawn(project, "tester", agent="claude-code", worktree=False)


def test_a_replacement_its_sandbox_would_refuse_is_refused_before_the_agent_stops(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r13 of #113, finding 7: restart, switch and the usage-limit hand-over stop the
    agent and only then spawn, whose install check then refused: the agent was stopped,
    nothing started, its claims abandoned. The replay is asked first, under the home it
    would start under: a switch's target slot, whose hooks may run another install."""
    from aisquare.core import launch_seal as seals
    from aisquare.services import store_broker

    codex, task = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    team_service.claim_task(task.id, session_ref=codex.board_id(THREAD_ONE))
    inside = codex.project.root / ".venv"
    monkeypatch.setattr(store_broker, "install_paths", lambda *args, **kwargs: (inside,))

    with pytest.raises(fleet_service.FleetError, match="aisquare-cli"):
        fleet_service.restart(codex.project, codex.agent.label)

    assert tmux.typed == [] and tmux.killed == [], "nothing typed into or killed"
    assert _claimed_by(task.id) == codex.board_id(THREAD_ONE), "its claim kept"
    # The target slot's home is the one asked: its hooks, not the current one's.
    target = codex.home.parent / "slot-2"
    asked: list[object] = []

    def paths_for(
        config_dir: Path | None = None, *args: object, **kwargs: object
    ) -> tuple[Path, ...]:
        asked.append(config_dir)
        return (inside,) if config_dir == target else ()

    monkeypatch.setattr(store_broker, "install_paths", paths_for)
    record = seals.read(codex.agent.id)
    assert isinstance(record, seals.LaunchRecord)
    fleet_service._refuse_a_replay_that_cannot_start(codex.project, record)
    with pytest.raises(fleet_service.FleetError):
        fleet_service._refuse_a_replay_that_cannot_start(codex.project, record, target)
    assert asked[-1] == target


def test_a_fleet_windows_launch_ignores_the_servers_pythonpath(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Review r14 of #113, finding 4: a fleet window's ``aisquare launch`` ran with the tmux
    server's ``PYTHONPATH``, which may name a directory a sandboxed agent writes. It runs
    with ``-E``; the agent it execs keeps the variable, as its tools need it."""
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    command = _command(tmux)
    assert command[:6] == [sys.executable, "-E", "-P", "-m", "aisquare", "launch"], command


def test_a_restart_under_a_new_permission_mode_is_checked_as_that_mode(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r14 of #113, finding 5: the check before the stop read the sandbox from the
    recorded command, while ``fleet restart --permission-mode`` gives the replacement
    another: ``full``, which no launch refuses, was refused, and a read-only seat restarted
    as a workspace one passed, was stopped, and then refused."""
    from aisquare.core import launch_seal as seals
    from aisquare.services import store_broker

    codex, _ = codex_agent
    inside = codex.project.root / ".venv"
    monkeypatch.setattr(store_broker, "install_paths", lambda *args, **kwargs: (inside,))
    record = seals.read(codex.agent.id)
    assert isinstance(record, seals.LaunchRecord)
    with pytest.raises(fleet_service.FleetError):
        fleet_service._refuse_a_replay_that_cannot_start(codex.project, record)
    fleet_service._refuse_a_replay_that_cannot_start(
        codex.project, record, permission_mode="full:never"
    )
    fleet_service._refuse_a_replay_that_cannot_start(
        codex.project, record, permission_mode="read_only:auto"
    )
    with pytest.raises(fleet_service.FleetError):
        fleet_service._refuse_a_replay_that_cannot_start(
            codex.project, record, permission_mode="workspace:auto"
        )


def test_the_fleets_install_check_reads_the_path_its_window_will_have(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r14 of #113, findings 6 and 7: spawn and the check before a stop read the
    caller's ``PATH`` while the window's launch has the tmux server's, so a spawn took a
    receipt for a window its launch then refused. It reads the server's while one answers.
    Any other failure of the check refuses the spawn, before any window."""
    from aisquare.services import store_broker

    assert fleet_service._window_path(None) == os.environ.get("PATH"), (
        "no server answers: this process's, which a server it starts takes"
    )
    seen: list[object] = []
    monkeypatch.setattr(tmux, "global_environment", lambda name: "/the/servers/bin", raising=False)
    monkeypatch.setattr(store_broker, "refuse_inside", lambda *args, **kwargs: seen.append(args[4]))
    fleet_service.spawn(project, "tester", label="tester-p", agent="codex", worktree=False)
    assert seen == ["/the/servers/bin"], seen

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(store_broker, "refuse_inside", broken)
    with pytest.raises(fleet_service.FleetError, match="could not work out"):
        fleet_service.spawn(project, "tester", label="tester-x", agent="codex", worktree=False)
    assert "tester-x" not in str(tmux.sessions)
