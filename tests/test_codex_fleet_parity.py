"""Fleet and session parity for a Codex agent, on the fleet's fake tmux.

Every hook here goes through Codex's own decoder (``services.agent_events.handle``) with
payloads shaped as Codex 0.162.1 sends them, measured against the real binary (the
loopback fixture in ``tests/test_codex_native.py`` covers the same paths live): a
``/clear`` fires NO ``SessionEnd`` for the thread it leaves, only ``SessionStart(source=
clear)`` for a new id, from the same process, under the same launch variables.

A Codex hook has no pid of the agent's own. The ``CLAUDE_PID`` a Codex hook finds is
whoever started the tmux server's — a Claude Code session's as often as not — so every
test here runs with a stale one in the environment, which must decide nothing.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.settings import launch_line
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import (
    REVIEWER_ARGS,
    FleetRoleSettings,
    FleetSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession, TeamTask
from aisquare.services import agent_events, auto_mode
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import fakebin
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _command,
    _events,
    _flag,
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

    def become(self, *, launch: str | None = None) -> None:
        """Run under the row's window: its variables, a launch id, a stale CLAUDE_PID."""
        self.monkeypatch.setenv("AISQUARE_FLEET_AGENT", self.agent.id)
        self.monkeypatch.setenv("AISQUARE_LAUNCH_ID", launch or self.launch)
        self.monkeypatch.setenv("AISQUARE_ROLE", "coder")
        self.monkeypatch.setenv("CLAUDE_PID", STALE_CLAUDE_PID)

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
    included, and starts ``startup`` (or ``resume``) — never ``clear``. Every newly
    started session under the window used to rebind the row (``fleet-session:<row>``
    took the newest start), so the child became the row with the claim left behind."""
    codex, task = codex_agent
    first = codex.board_id("thread-one")
    codex.start("thread-one")
    team_service.claim_task(task.id, session_ref=first)

    for source in ("startup", "resume"):
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
    assert [row.session_id for row in listed if row.id == codex.agent.id] == [first]


def test_a_clear_from_another_launch_cannot_take_the_row(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """``source=clear`` alone proves nothing: a nested ``aisquare launch`` is a launch of
    its own, and its interactive child's ``/clear`` is not the pane's."""
    codex, task = codex_agent
    first = codex.board_id("thread-one")
    codex.start("thread-one")
    team_service.claim_task(task.id, session_ref=first)

    codex.become(launch="launch-nested")
    foreign = codex.start("thread-foreign", source="clear")

    assert "ASSIGNED TO YOU" not in foreign
    assert _row(codex.agent.id).session_id == first and _claimed_by(task.id) == first


def test_an_early_prompt_of_the_cleared_thread_neither_takes_nor_loses_the_row(
    codex_agent: tuple[Codex, TeamTask],
) -> None:
    """The race the other way round: a hook of the new thread that is not its start
    arrives first. A prompt carries no ``source``, so it proves nothing and moves
    nothing; the start that follows moves the row and the claims."""
    codex, task = codex_agent
    first = codex.board_id("thread-one")
    codex.start("thread-one")
    team_service.claim_task(task.id, session_ref=first)

    codex.hook("UserPromptSubmit", "thread-two", prompt="early", turn_id="turn-early")
    assert _row(codex.agent.id).session_id == first and _claimed_by(task.id) == first

    codex.start("thread-two", source="clear")
    second = codex.board_id("thread-two")
    assert _row(codex.agent.id).session_id == second and _claimed_by(task.id) == second


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
    # A nested child of the replacement is no first arrival and takes nothing.
    replacement.become()
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


@pytest.mark.parametrize(
    ("intent", "claude", "codex"),
    [
        ("workspace:auto", ["--permission-mode", "auto"], ["--approve-for-me"]),
        (
            "workspace:ask",
            ["--permission-mode", "manual"],
            ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
        ),
        (
            "full:never",
            ["--permission-mode", "bypassPermissions"],
            ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
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
    assert codex[:4] == ["--sandbox", "read-only", "--ask-for-approval", "on-request"]


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
    assert _spawn_native(tmux)[:4] == ["--sandbox", "read-only", "--ask-for-approval", "never"]

    fleet_service.restart(project, agent.label)

    assert _spawn_native(tmux)[:4] == ["--sandbox", "read-only", "--ask-for-approval", "never"]


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
    assert "--approve-for-me" not in native and _flag(native, "--sandbox") == "read-only"


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

    assert _spawn_native(tmux)[:4] == [
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "on-request",
    ]
    spec = receipt.started.launch_spec
    assert spec is not None and spec.permission_mode == "workspace:ask"


def test_the_settings_line_shows_one_permission_for_every_agent() -> None:
    settings = FleetRoleSettings(permission="workspace:auto")
    line = launch_line("coder", "codex", settings).plain
    assert line.index("Codex --approve-for-me") < line.index("Claude Code --permission-mode auto")
    assert "workspace:auto" in line and "--name" not in line
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
