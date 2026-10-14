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

import os
import subprocess
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.agent import key_spellings
from aisquare.cli.ui.views.settings import launch_line
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import harness
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import AgentAdapter, store_dir
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
    return Codex(agent, project, home, tmux, monkeypatch), task


# --- /clear keeps the claims (acceptance 6) ----------------------------------------------


def test_a_codex_clear_moves_the_row_and_its_claims_to_the_new_thread(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    codex, task = codex_agent
    first = codex.board_id("thread-one")
    board = codex.start("thread-one")
    assert f"ASSIGNED TO YOU: {task.id}" in board
    assert team_service.claim_task(task.id, session_ref=first).status == "doing"

    # Codex's /clear: no SessionEnd for thread-one, a start of thread-two from the
    # same process (measured on 0.162.1).
    cleared = codex.start("thread-two", source="clear")

    second = codex.board_id("thread-two")
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
    codex.start("thread-three", source="clear")
    assert _claimed_by(task.id) == codex.board_id("thread-three")


def test_a_nested_codex_under_the_window_takes_neither_the_row_nor_the_task(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """A ``codex exec`` the agent runs inherits the window's variables, launch id
    included. Every newly started session under the window used to rebind the row
    (``fleet-session:<row>`` took the newest start), so the child became the row with
    the claim left behind; whatever start it reports, it takes nothing now."""
    codex, task = codex_agent
    first = codex.board_id("thread-one")
    codex.start("thread-one")
    team_service.claim_task(task.id, session_ref=first)

    codex.child("thread-one")
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


def test_the_agents_own_resume_to_an_earlier_thread_takes_the_row_and_its_launch_back(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """Review r1 of #113, finding 6: a ``/resume`` to a thread the launch had already
    seen left the launch (the MCP server's identity) and the row on the abandoned one,
    and the MCP server's calls revived it and recorded claims under it. The pane's own
    process goes back to its earlier thread with the row, the claims and the launch; a
    late redelivery of a first start does not."""
    codex, task = codex_agent
    first, second = codex.board_id("thread-one"), codex.board_id("thread-two")
    codex.start("thread-one")
    codex.start("thread-two", source="clear")
    team_service.claim_task(task.id, session_ref=second)

    codex.start("thread-one", source="startup")  # a late redelivery: nothing moves
    with store_session() as store:
        assert store.get_meta(f"launch-session:{codex.launch}") == second
    back = codex.start("thread-one", source="resume")

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
    first = codex.board_id("thread-one")
    codex.start("thread-one")
    team_service.claim_task(task.id, session_ref=first)

    codex.child("thread-one")
    codex.hook("UserPromptSubmit", "child-thread", prompt="nested", turn_id="turn-child")
    assert _row(codex.agent.id).session_id == first and _claimed_by(task.id) == first

    codex.become()
    codex.hook("UserPromptSubmit", "thread-two", prompt="early", turn_id="turn-early")
    second = codex.board_id("thread-two")
    assert _row(codex.agent.id).session_id == second and _claimed_by(task.id) == second
    codex.start("thread-two", source="clear")
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
    _rollout(codex, "thread-one")
    codex.start("thread-one")
    first = codex.board_id("thread-one")
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
    assert native[at : at + 2] == ["resume", "thread-one"], "the thread by its native id"
    assert command[-2:] == ["--", fleet_service._restart_prompt(receipt.started)]
    assert "--resume" not in command, "no Claude Code flag"
    # The window's launch puts every option before `resume <id>`, and the continue line
    # after Codex's own `--`: `codex <options> resume <id> -- <prompt>`.
    argv = _launched_argv(command, runner, monkeypatch)
    at = argv.index("resume")
    assert argv[at : at + 2] == ["resume", "thread-one"]
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
    rollout = _rollout(codex, "thread-old")
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
    _rollout(codex, "thread-one")
    codex.start("thread-one")
    first = codex.board_id("thread-one")
    team_service.claim_task(task.id, session_ref=first)

    receipt = fleet_service.restart(codex.project, codex.agent.label, fresh=True)

    assert receipt.resumed is False and receipt.started.session_id == first, "parked on it"
    assert _claimed_by(task.id) == first
    replacement = Codex(receipt.started, codex.project, codex.home, tmux, monkeypatch, launch="b")
    board = replacement.start("thread-two")
    second = replacement.board_id("thread-two")
    assert _row(receipt.started.id).session_id == second
    assert _claimed_by(task.id) == second and "You are the one working it" in board
    codex.become()  # the old process's own SessionEnd, late
    codex.hook("SessionEnd", "thread-one", reason="other")
    assert _claimed_by(task.id) == second
    # A nested child of the replacement takes nothing.
    replacement.child("thread-two")
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
    codex.start("thread-one")
    codex.hook("UserPromptSubmit", "thread-one", prompt="work", turn_id="turn-1")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "Queued message m for thread t.\n", "")

    monkeypatch.setattr(fleet_service, "_run_native", run)

    told = fleet_service.tell(codex.project, codex.agent.label, "rebase on main first")

    assert told.delivered is True and "queued for its next turn" in told.how
    [(argv, env)] = calls
    assert Path(argv[0]).name.startswith("codex")
    assert argv[1:] == ["queue", "--thread", "thread-one", "--message", "rebase on main first"]
    assert env["CODEX_HOME"] == str(codex.home), "the queue of the home the thread runs under"
    assert _notes_to(codex.project, codex.agent.label) == []


def test_a_queue_that_fails_leaves_the_message_on_the_board(
    codex_agent: tuple[Codex, TeamTask], monkeypatch: pytest.MonkeyPatch
) -> None:
    codex, _ = codex_agent
    codex.start("thread-one")
    codex.hook("UserPromptSubmit", "thread-one", prompt="work", turn_id="turn-1")

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
    codex.start("thread-one")
    codex.hook("PermissionRequest", "thread-one")
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
    codex.start("thread-m")
    codex.hook("UserPromptSubmit", "thread-m", prompt="run the team", turn_id="turn-1")
    codex.hook("Stop", "thread-m", turn_id="turn-1", stop_hook_active=False)
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
    with store_session() as store:  # a row whose agent and binary disagree
        assert isinstance(store, SqliteStore)
        store._conn.execute(
            "UPDATE fleet_agent SET agent = 'claude-code' WHERE id = ?", (agent.id,)
        )
        store._conn.commit()
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError, match="runs codex, but claude-code"):
        fleet_service.restart(project, agent.label)

    assert tmux.typed == [] and agent.pane_id not in tmux.killed
    assert len(tmux.spawned) == spawned


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
    codex.start("thread-one")
    first = codex.board_id("thread-one")
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
    codex.start("thread-one")
    codex.hook("UserPromptSubmit", "thread-one", prompt="slow work", turn_id="turn-1")
    codex.hook("UserPromptSubmit", "thread-one", prompt="steered in", turn_id="turn-1")
    codex.hook("UserPromptSubmit", "thread-one", prompt="steered in", turn_id="turn-1")

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
    codex.start("thread-one")
    first = codex.board_id("thread-one")

    def state() -> str | None:
        with store_session() as store:
            session = store.get_session(first)
        return session.state if session is not None else None

    codex.hook("UserPromptSubmit", "thread-one", prompt="work", turn_id="turn-1")
    codex.hook("Stop", "thread-one", turn_id="turn-1", stop_hook_active=False)
    assert state() == "waiting"
    codex.hook("UserPromptSubmit", "thread-one", prompt="one more thing", turn_id="turn-1")
    assert state() == "working"
    codex.hook("Stop", "thread-one", turn_id="turn-1", stop_hook_active=False)
    assert state() == "waiting"
    with store_session() as store:
        store.touch_session(first, state="working")
    codex.hook("Stop", "thread-one", turn_id="turn-1", stop_hook_active=False)
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
