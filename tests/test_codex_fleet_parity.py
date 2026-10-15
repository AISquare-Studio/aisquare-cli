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
import sqlite3
import subprocess
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path, PureWindowsPath
from typing import cast
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
from aisquare.core.tmux import BUNDLED_CONF
from aisquare.models import (
    ClaudeAccount,
    FleetAgent,
    FleetAgentStatus,
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
        "no session is bound to the row" in note and "starts fresh" in note
        for note in receipt.notes
    ), receipt.notes
    assert "resume" not in _native(_command(tmux))


def test_a_codex_session_recorded_without_its_native_id_restarts_fresh_and_says_so(
    codex_agent: tuple[Codex, TeamTask],
    tmux: FakeTmux,  # noqa: F811
) -> None:
    codex, _ = codex_agent
    rollout = _rollout(codex, THREAD_OLD)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="board-only",
                project_id=codex.project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                transcript_path=str(rollout),
                agent="codex",
            )
        )
        row = store.get_fleet_agent(codex.agent.id)
        assert row is not None
        store.upsert_fleet_agent(row.model_copy(update={"session_id": "board-only"}))

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
    _rewrite("fleet_agent", "agent", "claude-code", agent.id)  # agent and binary disagree
    assert launch_seal.record(_row(agent.id)) is None, "as if spawn had sealed it so"
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError, match="runs codex, but claude-code"):
        fleet_service.restart(project, agent.label)

    assert tmux.typed == [] and agent.pane_id not in tmux.killed
    assert len(tmux.spawned) == spawned


# --- nothing a sandbox writes in the store chooses what runs outside it -----------------
# (review r2 of #113, finding 3; r3, findings 1, 2, 3, 5 and 10: `core.launch_seal`)


#: How a replay of a row the store changed since ``fleet spawn`` sealed it is refused.
UNSEALED = "is not the launch `fleet spawn` sealed"


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


def test_the_store_profile_keeps_what_runs_outside_the_sandbox_out_of_its_reach() -> None:
    """The store is writable from Codex's sandbox, the board's to write; what chooses or
    runs something outside it is read-only there (the tmux conf every fleet server loads,
    the registry ``upgrade`` reconnects hooks by, keys and sign-in tokens, the digests of
    the launch specs a replay runs). No path is denied reading: with one, Codex reads its
    own instructions through its sandbox helper, and on a host whose bwrap cannot bring up
    loopback (a GitHub runner) the session did not start (CI on ca6d43ea)."""
    store = store_dir()
    args = CODEX.store_access("workspace", store)
    table = args[args.index("-c", 4) + 1].partition("=")[2]
    entries = tomllib.loads(f"t = {table}")["t"]

    assert entries[str(store)] == "write"
    for kept in (
        "fleet-tmux.conf",
        "agents.json",
        "explainability-key",
        "cache/oidc",
        "config.toml",
        "credentials",
    ):
        assert entries[str(store / kept)] == "read", kept
    assert entries[str(store / launch_seal.DIR_NAME)] == "read"
    assert set(entries.values()) == {"write", "read"}
    assert str(store / "context.db") not in entries, "the board's"


def test_the_store_profile_names_a_windows_home_as_codex_reads_it() -> None:
    """A Windows home's paths go into Codex's ``-c`` as TOML strings, each backslash
    escaped, and come out of TOML as Windows spells them. Asserted on any platform: the
    contract test that looked for ``C:\\Users\\…`` in the command line as written failed on
    the Windows runner only (CI on e8f95719), where the escaped spelling is the right one."""
    store = PureWindowsPath(r"C:\Users\me\.aisquare")
    args = CODEX.store_access("workspace", cast(Path, store))
    table = args[args.index("-c", 4) + 1].partition("=")[2]

    assert r'"C:\\Users\\me\\.aisquare" = "write"' in table, "TOML's escape, as Codex reads it"
    entries = tomllib.loads(f"t = {table}")["t"]
    assert entries[r"C:\Users\me\.aisquare"] == "write"
    assert entries[r"C:\Users\me\.aisquare\cache\oidc"] == "read"
    assert entries[r"C:\Users\me\.aisquare\fleet-tmux.conf"] == "read"
    assert entries[rf"C:\Users\me\.aisquare\{launch_seal.DIR_NAME}"] == "read"
    assert all(key.startswith("C:\\Users\\me\\.aisquare") for key in entries), entries


def test_a_sandboxed_launch_makes_what_its_profile_names_before_the_sandbox_can() -> None:
    """Codex's sandbox leaves an empty file on the host where a path its profile names did
    not exist (0.162.1, measured): a directory's place taken by a file, a file a writer
    that writes in place cannot write. The launcher makes them first."""
    store = store_dir()
    prepare_store(store)

    for directory in ("codex-accounts", "claude-accounts", "cache/codex-accounts", "cache/oidc"):
        assert (store / directory).is_dir(), directory
    assert (store / "fleet-tmux.conf").read_text(encoding="utf-8") == BUNDLED_CONF
    assert json.loads((store / "agents.json").read_text(encoding="utf-8")) == {
        "connected": [],
        "connections": {},
    }
    assert (store / launch_seal.DIR_NAME).is_dir()
    # What is there already is left as it is.
    (store / "agents.json").write_text('{"connected": ["codex"]}', encoding="utf-8")
    prepare_store(store)
    assert json.loads((store / "agents.json").read_text(encoding="utf-8")) == {
        "connected": ["codex"]
    }


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("$.binary", "/tmp/x"),  # the program a replay runs
        ("$.extra_args", ["-c", 'notify=["sh","-c","/tmp/x"]']),  # one Codex runs
        ("binary", "/tmp/x"),  # the program `fleet tell` queues with
        ("role", "coder"),  # a reviewer's read-only seat comes back workspace-write
        ("agent", "claude-code"),  # a Codex row comes back with no OS sandbox
        ("cwd", "/tmp"),  # a directory the sandbox wrote
        ("worktree", 1),
        ("account_slot", 7),
        ("launch_spec", None),  # no spec: the role as it resolves today
    ],
)
def test_a_row_the_store_changed_is_never_replayed(
    column: str,
    value: object,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The store is writable from Codex's sandbox and the row's launch is replayed outside
    it, by restart, switch and the usage-limit hand-over. Its spec was sealed and the rest
    of the row was not (r3 finding 5): the role, the agent and the worktree a replacement
    ran with came from the row, and a spec that failed its seal fell back to them. Any
    change since the spawn sealed it is refused now, with the reason, before anything is
    stopped; nothing falls back."""
    agent = fleet_service.spawn(project, "reviewer", agent="codex", worktree=False).agent
    assert launch_seal.holds(_row(agent.id)), "the row as the store holds it is the one sealed"
    if column.startswith("$."):
        _tamper(agent, column, value)
    else:
        _rewrite("fleet_agent", column, value, agent.id)
    spawned = len(tmux.spawned)

    for replay in (
        lambda: fleet_service.restart(project, agent.label),
        lambda: fleet_service.switch(project, agent.label, to="0"),
        lambda: fleet_service.switch(project, agent.label, automatic=True),
    ):
        with pytest.raises(fleet_service.FleetError, match=UNSEALED):
            replay()

    assert tmux.typed == [] and agent.pane_id not in tmux.killed, "left running"
    assert len(tmux.spawned) == spawned, "nothing started"


def test_a_row_untouched_since_its_seal_is_replayed_as_recorded(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Control: an untouched row still replays, its arguments the recorded ones, and its
    replacement is sealed in turn."""
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, agent_args=["--search"]
    ).agent

    receipt = fleet_service.restart(project, agent.label)

    assert "--search" in _command(tmux)
    assert launch_seal.holds(_row(receipt.started.id))


def test_the_rows_recorded_before_the_seals_keep_their_launch_across_the_upgrade(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """An agent spawned before this version left no seal: there was nowhere to. The first
    run that makes the seals' directory records every row the store holds, so the agents
    running across an upgrade still restart as recorded; a row changed AFTER that does
    not."""
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, agent_args=["--search"]
    ).agent
    shutil.rmtree(launch_seal.seals_dir())  # as the previous version left the home

    receipt = fleet_service.restart(project, agent.label)

    assert "--search" in _command(tmux)
    _tamper(receipt.started, "$.extra_args", ["--search", "--yolo"])
    with pytest.raises(fleet_service.FleetError, match=UNSEALED):
        fleet_service.restart(project, agent.label)
    assert "--yolo" not in _command(tmux)


def test_a_seal_directory_another_process_is_still_filling_is_filled_before_it_is_read(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """r3 finding 10: the directory was made before the rows were recorded into it, and a
    restart in that window read an untouched row as changed, dropping its spec for good.
    A directory not yet marked ready is filled by whoever reads it."""
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, agent_args=["--search"]
    ).agent
    shutil.rmtree(launch_seal.seals_dir())
    launch_seal.seals_dir().mkdir()  # made, nothing recorded yet

    fleet_service.restart(project, agent.label)

    assert "--search" in _command(tmux)
    assert (launch_seal.seals_dir() / launch_seal.READY).is_file()


def test_a_store_unreadable_at_the_first_making_leaves_the_seals_to_the_next_run(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3 finding 10: a store that could not be read when the directory was first made
    left every row before it unsealed for good, and the refusal blamed a change. The
    directory stays unready, the refusal names what happened, and the next run seals."""
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, agent_args=["--search"]
    ).agent
    shutil.rmtree(launch_seal.seals_dir())
    with monkeypatch.context() as patched:
        busy = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        patched.setattr(SqliteStore, "every_fleet_agent", busy)
        with pytest.raises(fleet_service.FleetError, match="no launch seal could be checked"):
            fleet_service.restart(project, agent.label)
        assert not (launch_seal.seals_dir() / launch_seal.READY).exists()

    fleet_service.restart(project, agent.label)

    assert "--search" in _command(tmux)


def test_a_home_where_the_seals_cannot_be_made_says_so(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """r3 finding 10: "changed since, or recorded by another version" was said of a home
    where the directory could not be made at all."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    shutil.rmtree(launch_seal.seals_dir())
    launch_seal.seals_dir().write_text("not a directory", encoding="utf-8")

    with pytest.raises(fleet_service.FleetError, match="could not be made") as refused:
        fleet_service.restart(project, agent.label)

    assert "no launch seal could be checked" in str(refused.value)
    assert "changed" not in str(refused.value)


@pytest.mark.parametrize("forged", ["../planted", "a/../../planted", "{absolute}"])
def test_a_row_id_the_store_chose_never_names_the_seal_that_is_read(
    forged: str,
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """r3 finding 3: the seal's file was the row's id joined into the path, so an id with
    ``..`` or an absolute one named a file the sandbox wrote, holding the digest of
    whatever the row carries. The file is named by the id's own digest."""
    forged = forged.format(absolute=tmp_path / "planted")
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    _tamper(agent, "$.binary", "/tmp/x")
    _rewrite("fleet_agent", "id", forged, agent.id)
    row = _row(forged)
    # Where a lookup that joined the id into the path read the seal, planted to hold.
    planted = Path(os.path.normpath(launch_seal.seals_dir() / f"{forged}.sha256"))
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(launch_seal.digest(row) + "\n", encoding="ascii")

    with pytest.raises(fleet_service.FleetError, match="no launch seal was recorded"):
        fleet_service.restart(project, agent.label)

    assert "/tmp/x" not in _command(tmux)
    assert launch_seal._seal_path(forged).parent == launch_seal.seals_dir()


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
    which the sandbox writes; only a UUID, Codex's thread id, is resumed by now."""
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    _rewrite("team_session", "native_session_id", native, codex.board_id(THREAD_ONE))

    receipt = fleet_service.restart(codex.project, codex.agent.label)

    assert receipt.resumed is False
    assert any(
        "is not one Codex makes" in note and "starts fresh" in note for note in receipt.notes
    ), receipt.notes
    assert "resume" not in _native(_command(tmux)) and native not in _command(tmux)


def test_a_transcript_the_store_names_relatively_is_never_resumed(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code resumes ``--resume <transcript>``, the path from the session row: one
    the store names relatively (``--dangerously-skip-permissions``, a file of that name
    where the restart runs) would be an option of Claude Code's, not its value."""
    flag = "--dangerously-skip-permissions"
    monkeypatch.chdir(tmp_path)
    (tmp_path / flag).write_text("{}\n", encoding="utf-8")
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    assert agent.session_id is not None
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now,
                last_seen_at=now,
                transcript_path=flag,
            )
        )

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.resumed is False and flag not in _command(tmux)


def test_tell_never_queues_with_a_binary_the_store_chose(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    """r3 finding 1: ``fleet tell`` to a busy Codex agent ran the row's ``binary`` as
    ``<binary> queue …`` with the caller's rights, and a sandboxed agent can rewrite its
    row. The message goes to the board instead, saying why."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    _rewrite("fleet_agent", "binary", "/tmp/x", codex.agent.id)
    monkeypatch.setattr(fleet_service, "_run_native", Mock(side_effect=AssertionError("ran")))

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is False and UNSEALED in told.how, told.how
    assert _notes_to(codex.project, codex.agent.label) == ["rebase on main first"]


@pytest.mark.parametrize(
    ("column", "value", "why"),
    [
        ("native_session_id", "--remote=ws://127.0.0.1:9", "is not one Codex makes"),
        ("account", "{elsewhere}", "is not one of Codex's account homes"),
    ],
)
def test_tell_never_queues_on_a_thread_or_home_the_store_chose(
    column: str,
    value: str,
    why: str,
    codex_agent: tuple[Codex, TeamTask],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``codex queue`` runs outside any sandbox with ``--thread`` and ``CODEX_HOME`` from
    the session row: a thread only in Codex's shape, and a home only one of Codex's own,
    never a directory whose config the sandbox wrote."""
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, prompt="work", turn_id="turn-1")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.toml").write_text('notify = ["/tmp/x"]\n', encoding="utf-8")
    _rewrite("team_session", column, value.format(elsewhere=elsewhere), codex.board_id(THREAD_ONE))
    monkeypatch.setattr(fleet_service, "_run_native", Mock(side_effect=AssertionError("ran")))

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is False and why in told.how, told.how
    assert _notes_to(codex.project, codex.agent.label) == ["rebase on main first"]


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
    assert "+ the aisquare store, writable in its workspace sandbox" in line
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
    now = datetime.now(UTC)
    session = TeamSession(
        id="s",
        project_id="prj",
        role="coder",
        started_at=now,
        last_seen_at=now,
        agent="codex",
        account=str(home),
    )

    def account(where: Path) -> ClaudeAccount:
        return ClaudeAccount(slot=7, config_dir=where, managed=True, agent="codex")

    assert fleet_service._same_home(session, account(home))
    assert fleet_service._same_home(session, account(home / "." / ""))
    assert not fleet_service._same_home(session, account(other))
    assert not fleet_service._same_home(None, account(home))
    assert not get_adapter("codex").capabilities.resumes_across_homes
