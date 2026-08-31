"""The fleet lifecycle over a FAKE tmux (plan §5.3, §5.7, §7.3; §3.4 to §3.6).

Every test here drives ``services.fleet`` against the real store in an isolated
home and an in-memory :class:`FakeTmux` — no ``claude``, no ``gh``, and no tmux
server except in the one end-to-end test at the bottom, which runs on a private
socket, is skipped when tmux is absent, and kills its server afterwards.

The fake's runner refuses every call, so a method the service uses that the
fake forgot to override fails loudly instead of quietly reaching a real tmux.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.config import FleetRoleSettings, FleetSettings
from aisquare.core.ids import new_task_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.core.tmux import (
    Completed,
    PaneFacts,
    TmuxError,
    TmuxServer,
    TmuxUnavailable,
    WindowInfo,
)
from aisquare.models import FleetAgent, ProjectInfo, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.fleet import (
    ACTIVITY_WINDOW,
    NUDGE_TEXT,
    PROMPT_TIMEOUT,
    FleetError,
    FleetUnavailable,
    NoSuchAgent,
)

PYTHON_LAUNCHER = "python3"
"""What ``pane_current_command`` reads while ``python -m aisquare launch`` is resolving."""


# --- the fake tmux ---------------------------------------------------------------------


def _facts(pane_id: str, **overrides: object) -> PaneFacts:
    base = PaneFacts(
        pane_id=pane_id,
        width=200,
        height=50,
        cursor_x=0,
        cursor_y=0,
        cursor_visible=True,
        alternate_on=False,
        history_size=0,
        dead=False,
        dead_status=None,
        in_mode=False,
        current_command=PYTHON_LAUNCHER,
        title="",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class FakeTmux(TmuxServer):
    """A tmux server kept in memory: sessions, windows, panes, and what was typed."""

    def __init__(self) -> None:
        super().__init__("asq-test", runner=self._unexpected)
        self.installed = True
        """Whether ``tmux`` is on PATH — every fake call checks, as the real server does."""
        self.honours_exit = True
        """Whether a typed ``/exit`` makes the pane die with status 0 (a real agent does)."""
        self.status_lag = 0
        """Reads for which a dead pane's ``dead_status`` is still empty — measured on
        tmux 3.4 (its own -vv server log): one poll can see ``dead=1`` while
        ``pane_dead_status`` expands to ``''``; the status lands a beat later."""
        self.fail_input = False
        self.sessions: dict[str, list[WindowInfo]] = {}
        self.facts: dict[str, PaneFacts] = {}
        self.output_at: dict[str, datetime] = {}
        """When each pane last printed — what ``#{window_activity}`` reports."""
        self.spawned: list[dict[str, object]] = []
        self.typed: list[tuple[str, str, str]] = []
        """``(pane_id, kind, text)`` with kind ``literal`` / ``paste`` / ``key``."""
        self.killed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self._counter = 0

    @staticmethod
    def _unexpected(argv: Sequence[str], stdin: bytes | None) -> Completed:
        raise AssertionError(f"the fake tmux was asked to really run: {list(argv)}")

    # -- availability --
    def binary(self) -> str:
        if not self.installed:
            raise TmuxUnavailable("tmux is not installed (fake)")
        return "/fake/tmux"

    def version(self) -> tuple[int, int] | None:
        self.binary()
        return (3, 7)

    def conf_path(self) -> Path:
        return Path("/fake/fleet-tmux.conf")

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        """The one raw command the service sends: last-output times for every pane."""
        self.binary()
        if list(args) != ["list-panes", "-a", "-F", fleet_service._ACTIVITY_FORMAT]:
            raise AssertionError(f"the fake tmux was asked to really run: {list(args)}")
        return "".join(
            f"{pane_id}{fleet_service._SEP}{int(when.timestamp())}\n"
            for pane_id, when in self.output_at.items()
            if pane_id in self.facts
        )

    # -- sessions and windows --
    def list_sessions(self) -> list[str]:
        self.binary()
        return list(self.sessions)

    def has_session(self, name: str) -> bool:
        self.binary()
        return name in self.sessions

    def spawn_window(
        self,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        width: int = 200,
        height: int = 50,
    ) -> WindowInfo:
        self.binary()
        self._counter += 1
        pane_id = f"%{self._counter}"
        window = WindowInfo(
            session=session,
            window_id=f"@{self._counter}",
            name=name,
            pane_id=pane_id,
            dead=False,
            dead_status=None,
            current_command=PYTHON_LAUNCHER,
            activity=False,
        )
        self.sessions.setdefault(session, []).append(window)
        self.facts[pane_id] = _facts(pane_id)
        self.spawned.append(
            {
                "session": session,
                "name": name,
                "cwd": cwd,
                "command": list(command),
                "env": dict(env or {}),
            }
        )
        return window

    def list_windows(self, session: str) -> list[WindowInfo]:
        self.binary()
        return [self._current(window) for window in self.sessions.get(session, [])]

    def _current(self, window: WindowInfo) -> WindowInfo:
        facts = self.facts[window.pane_id]
        dead_status = facts.dead_status
        if facts.dead and self.status_lag > 0:
            self.status_lag -= 1  # the gap: dead, but no status on this read yet
            dead_status = None
        return replace(
            window,
            dead=facts.dead,
            dead_status=dead_status,
            current_command=facts.current_command,
            # Faithful to tmux 3.7c: the flag is set from creation and never clears
            # on a detached session, so it must not be what the service reads.
            activity=True,
        )

    def pane_facts(self, pane_id: str) -> PaneFacts | None:
        self.binary()
        return self.facts.get(pane_id)

    def kill_window(self, pane_id: str) -> None:
        self.binary()
        if pane_id not in self.facts:
            raise TmuxError(f"can't find pane: {pane_id}")
        self.vanish(pane_id)
        self.killed.append(pane_id)

    def kill_session(self, session: str) -> None:
        self.binary()
        for window in self.sessions.pop(session, []):
            self.facts.pop(window.pane_id, None)

    def rename_session(self, old: str, new: str) -> None:
        self.binary()
        if old not in self.sessions:
            raise TmuxError(f"can't find session: {old}")
        self.sessions[new] = self.sessions.pop(old)
        self.renamed.append((old, new))

    def attach_argv(self, session: str) -> list[str]:
        return [self.binary(), "-L", self.socket, "attach-session", "-t", f"={session}"]

    # -- input --
    def send_keys(self, pane_id: str, *keys: str) -> None:
        self._input(pane_id, "key", " ".join(keys))

    def send_literal(self, pane_id: str, text: str) -> None:
        self._input(pane_id, "literal", text)
        if text == "/exit" and self.honours_exit:
            self.die(pane_id, 0)

    def paste(self, pane_id: str, text: str) -> None:
        self._input(pane_id, "paste", text)

    def _input(self, pane_id: str, kind: str, text: str) -> None:
        self.binary()
        if self.fail_input:
            raise TmuxError("send-keys failed (fake)")
        if pane_id not in self.facts:
            raise TmuxError(f"can't find pane: {pane_id}")
        self.typed.append((pane_id, kind, text))

    # -- what the test does to the world --
    def die(self, pane_id: str, status: int) -> None:
        self.facts[pane_id] = replace(self.facts[pane_id], dead=True, dead_status=status)

    def set_command(self, pane_id: str, command: str) -> None:
        self.facts[pane_id] = replace(self.facts[pane_id], current_command=command)

    def printed(self, pane_id: str, *, ago: timedelta = timedelta(0)) -> None:
        self.output_at[pane_id] = datetime.now(tz=UTC) - ago

    def vanish(self, pane_id: str) -> None:
        self.facts.pop(pane_id, None)
        for windows in self.sessions.values():
            windows[:] = [window for window in windows if window.pane_id != pane_id]


class FakeClock:
    """``_sleep`` / ``_monotonic`` for the service: time passes only when slept."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture
def tmux(monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    fake = FakeTmux()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: fake)
    return fake


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(fleet_service, "_sleep", fake.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", fake.monotonic)
    return fake


@pytest.fixture
def claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``claude`` the binary check finds — a shell script, never Claude Code."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text('#!/bin/sh\necho "fake claude: $*"\nread line\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=fleet@test", "-c", "user.name=fleet", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.fixture
def project(repo: Path) -> ProjectInfo:
    info = team_project(repo)
    with store_session() as store:
        store.ensure_project(info)
    return info


@pytest.fixture
def plain_project(tmp_path: Path) -> ProjectInfo:
    """A directory that is not a git checkout — a parent of several repos, say."""
    path = tmp_path / "plain"
    path.mkdir()
    info = team_project(path)
    with store_session() as store:
        store.ensure_project(info)
    return info


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> FleetSettings:
    config = FleetSettings(**overrides)
    monkeypatch.setattr(fleet_service, "settings", lambda: config)
    return config


def _codename(project: ProjectInfo) -> str:
    with store_session() as store:
        stored = store.get_project(project.id)
    assert stored is not None and stored.codename
    return stored.codename


def _flag(command: Sequence[str], name: str) -> str | None:
    """The value after ``name`` in a command line, or ``None`` when absent."""
    if name not in command:
        return None
    return command[list(command).index(name) + 1]


def _command(tmux: FakeTmux, index: int = -1) -> list[str]:
    command = tmux.spawned[index]["command"]
    assert isinstance(command, list)
    return command


def _board_session(agent: FleetAgent, state: str, *, seen_ago: timedelta = timedelta(0)) -> None:
    """The ``team_session`` row the agent's hooks would have written."""
    assert agent.session_id is not None
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now - seen_ago,
                last_seen_at=now - seen_ago,
            )
        )
        if seen_ago == timedelta(0):
            store.touch_session(agent.session_id, state=state)
        else:
            assert state == "working", "a stale row can only be inserted in its default state"


def _add_task(project: ProjectInfo, title: str) -> TeamTask:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        task, _ = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=project.id,
                key=team_service.task_key(title),
                title=title,
                created_at=now,
                updated_at=now,
            )
        )
    return task


def _events(project: ProjectInfo, kind: str) -> list[str]:
    with store_session() as store:
        return [e.text for e in store.recent_events(project.id, limit=50) if e.kind == kind]


def _coder(project: ProjectInfo, **kwargs: object) -> FleetAgent:
    kwargs.setdefault("worktree", False)
    return fleet_service.spawn(project, "coder", **kwargs).agent  # type: ignore[arg-type]


# --- spawn ---------------------------------------------------------------------------


def test_spawn_manager_builds_the_launch_command_and_records_the_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "manager")
    agent = receipt.agent

    assert agent.label == "manager" and agent.role == "manager"
    assert receipt.asked_label is None and receipt.notes == []
    assert receipt.tmux_session == f"asq-{_codename(project)}"
    spawned = tmux.spawned[0]
    assert spawned["session"] == receipt.tmux_session and spawned["name"] == "manager"
    command = _command(tmux)
    assert command[:5] == [sys.executable, "-m", "aisquare", "launch", "manager"]
    assert _flag(command, "--permission-mode") == "auto"
    assert agent.session_id and _flag(command, "--session-id") == agent.session_id
    assert _flag(command, "--name") == "manager"
    assert "--command" not in command, "no --bin given: launch resolves the binary itself"
    assert spawned["env"] == {
        "AISQUARE_FLEET_AGENT": agent.id,
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    }
    assert spawned["cwd"] == project.root and agent.cwd == project.root and not agent.worktree
    assert agent.pane_id == "%1" and agent.binary == "claude" and agent.spawned_by == "user"
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "manager") == agent


def test_spawn_refuses_a_second_manager(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    with pytest.raises(FleetError, match="already has a manager"):
        fleet_service.spawn(project, "manager")
    assert len(tmux.spawned) == 1, "the refusal must come before any window is created"
    # Negative control: a second agent of another role is welcome.
    assert _coder(project).label == "coder-1"


def test_spawn_refuses_past_the_cap_and_names_the_count(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, max_agents_per_project=2)
    _coder(project)
    _coder(project)
    with pytest.raises(FleetError, match=r"2 agents \(max_agents_per_project = 2\)"):
        _coder(project)
    assert len(tmux.spawned) == 2
    # An ended agent frees its slot.
    fleet_service.stop(project, "coder-1", force=True)
    assert _coder(project).label == "coder-1"


def test_spawn_refuses_an_unknown_role_but_accepts_a_bound_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(FleetError, match="unknown role 'codr'"):
        fleet_service.spawn(project, "codr")
    assert tmux.spawned == []
    monkeypatch.setattr("aisquare.cli.launch._declared_roles", lambda: {"scribe"})
    receipt = fleet_service.spawn(project, "scribe")
    assert receipt.agent.role == "scribe" and receipt.agent.label == "scribe-1"
    assert _command(tmux)[4] == "scribe"


def test_spawn_refuses_a_worktree_outside_git(
    tmux: FakeTmux, claude_on_path: Path, plain_project: ProjectInfo
) -> None:
    with pytest.raises(
        FleetError,
        match="not a git repository — spawn without --worktree or pick a repo inside it",
    ):
        fleet_service.spawn(plain_project, "coder")  # a coder's default is a worktree
    assert tmux.spawned == []
    receipt = fleet_service.spawn(plain_project, "coder", worktree=False)
    assert receipt.agent.cwd == plain_project.root and not receipt.agent.worktree


def test_spawn_coder_gets_a_worktree_on_the_fleet_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder")
    agent = receipt.agent
    expected = project.root / ".aisquare-worktrees" / "coder-1"

    assert agent.worktree and agent.cwd == expected and expected.is_dir()
    assert tmux.spawned[0]["cwd"] == expected
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=expected) == (
        f"fleet/{_codename(project)}/coder-1"
    )
    exclude = project.root / ".git" / "info" / "exclude"
    assert ".aisquare-worktrees/" in exclude.read_text(encoding="utf-8").splitlines()
    assert _git("status", "--porcelain", cwd=project.root) == "", "the worktree dir is excluded"
    # A second worktree adds no second exclude line.
    fleet_service.spawn(project, "coder")
    assert exclude.read_text(encoding="utf-8").splitlines().count(".aisquare-worktrees/") == 1


def test_spawn_with_a_task_names_the_label_and_the_branch_after_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    task = _add_task(project, "Wire the auth flow!")
    short = task.id.removeprefix("tsk_")[:8]
    receipt = fleet_service.spawn(project, "coder", task_id=task.id[:12])

    assert receipt.agent.label == f"coder-{short}"
    assert receipt.agent.task_id == task.id, "the full id is recorded, not the prefix given"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=receipt.agent.cwd) == (
        f"fleet/{_codename(project)}/{short}-wire-the-auth-flow"
    )
    with pytest.raises(FleetError, match="no task matches 'tsk_nope'"):
        fleet_service.spawn(project, "coder", task_id="tsk_nope")


def test_spawn_permission_mode_flag_beats_role_config_beats_default(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(
        monkeypatch,
        roles={"coder": FleetRoleSettings(permission_mode="acceptEdits", worktree=False)},
    )
    _coder(project)
    assert _flag(_command(tmux), "--permission-mode") == "acceptEdits", "role config"
    _coder(project, permission_mode="plan")
    assert _flag(_command(tmux), "--permission-mode") == "plan", "the flag wins"
    _coder(project, permission_mode="")
    assert "--permission-mode" not in _command(tmux), "empty string = no flag"
    fleet_service.spawn(project, "tester")
    assert _flag(_command(tmux), "--permission-mode") == "auto", "built-in default"


def test_spawn_carries_role_extra_args_then_the_callers_args(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "reviewer", worktree=False, agent_args=["--model", "opus"])
    command = _command(tmux)
    assert "--restricted" in command, "the reviewer's built-in extra arg (§3.6)"
    assert command[-2:] == ["--model", "opus"]
    assert command.index("--restricted") < command.index("--model")


def test_spawn_respects_a_caller_supplied_session_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    given = "cccc3333-0000-0000-0000-000000000000"
    agent = _coder(project, agent_args=["--session-id", given])
    command = _command(tmux)
    assert command.count("--session-id") == 1 and _flag(command, "--session-id") == given
    assert agent.session_id == given


def test_spawn_records_no_session_for_a_binary_that_takes_no_session_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False, binary=sys.executable)
    command = _command(tmux)
    assert receipt.agent.session_id is None and "--session-id" not in command
    assert command[5:7] == ["--command", sys.executable], "an explicit --bin reaches launch"
    assert receipt.agent.binary == sys.executable
    assert any("no board join" in note for note in receipt.notes)
    assert fleet_service.status_of(receipt.agent).detail == "no hooks"


def test_spawn_suffixes_a_label_a_live_agent_holds(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    first = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    second = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    assert first.agent.label == "coder-auth" and first.notes == []
    assert second.agent.label == "coder-auth-2" and second.asked_label == "coder-auth"
    assert any("'coder-auth-2'" in note for note in second.notes)
    # An ended agent frees its label.
    fleet_service.stop(project, "coder-auth", force=True)
    third = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    assert third.agent.label == "coder-auth" and third.notes == []


def test_spawn_retries_the_label_when_the_live_index_trips(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race: another spawn takes the label between ``next_label`` and the write."""
    _coder(project, label="coder-auth")
    real = fleet_service.next_label
    calls: list[str | None] = []

    def racing(
        project_: ProjectInfo,
        role: str,
        *,
        wanted: str | None = None,
        task_id: str | None = None,
        store: object = None,
    ) -> str:
        calls.append(wanted)
        if len(calls) == 1:
            return "coder-auth"  # looked free a moment ago
        return real(project_, role, wanted=wanted, task_id=task_id, store=store)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "next_label", racing)
    receipt = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")

    assert receipt.agent.label == "coder-auth-2"
    assert any("was taken while starting" in note for note in receipt.notes)
    assert tmux.killed == [], "the window stays; only the row's label changed"
    with store_session() as store:
        labels = {agent.label for agent in store.fleet_agents(project.id, live_only=True)}
    assert labels == {"coder-auth", "coder-auth-2"}


def test_spawn_kills_the_window_when_no_label_can_be_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager race has no suffix to fall back on: the second window must not linger."""
    fleet_service.spawn(project, "manager")
    with store_session() as store:
        store.end_fleet_agent(store.fleet_agents(project.id)[0].id)  # looks free to the pre-check
    original = tmux.spawn_window

    def spawn_then_revive(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        with store_session() as store:  # the other spawn wins the row before we write ours
            first = store.fleet_agents(project.id)[0]
            store.upsert_fleet_agent(first.model_copy(update={"ended_at": None}))
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_revive)
    with pytest.raises(FleetError, match="already has a manager"):
        fleet_service.spawn(project, "manager")
    assert tmux.killed == ["%2"]


def test_spawn_refuses_the_reserved_manager_label_for_other_roles(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    with pytest.raises(FleetError, match="reserved for the manager"):
        _coder(project, label="manager")
    assert tmux.spawned == []


def test_spawn_manager_ignores_another_label_and_says_so(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "manager", label="boss")
    assert receipt.agent.label == "manager" and receipt.asked_label == "boss"
    assert receipt.notes == ["the manager is always labelled 'manager' (asked: 'boss')"]


@pytest.mark.parametrize("command", ["claude", "node", "aider"])
def test_the_agent_is_running_when_the_foreground_command_is_neither_shell_nor_launcher(
    command: str,
) -> None:
    assert fleet_service._agent_running(command)


@pytest.mark.parametrize(
    "command", ["", "  ", "sh", "bash", "zsh", "tmux", "python3", "python3.13"]
)
def test_the_agent_is_not_running_behind_a_shell_the_launcher_or_a_pane_before_its_exec(
    command: str,
) -> None:
    assert not fleet_service._agent_running(command)


def test_spawn_types_the_prompt_once_the_agent_is_up(
    tmux: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = 0
    original = tmux.pane_facts

    def coming_up(pane_id: str) -> PaneFacts | None:
        nonlocal polls
        polls += 1
        if polls == 3:
            tmux.set_command(pane_id, "claude")  # the launcher exec'd the agent
        return original(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", coming_up)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    pane = receipt.agent.pane_id
    assert tmux.typed == [(pane, "paste", "start on tsk_1"), (pane, "key", "Enter")]
    assert receipt.notes == []
    assert 0 < clock.now < PROMPT_TIMEOUT, "waited for the agent, not for the timeout"
    assert polls == 3, "typed as soon as the agent was up"
    # Negative control: no prompt, nothing typed.
    tmux.typed.clear()
    _coder(project)
    assert tmux.typed == []


def test_spawn_prompt_waits_out_the_pane_before_its_exec(
    tmux: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right after creation a pane reads ``tmux``; that is not the agent listening."""
    polls = 0
    original = tmux.pane_facts

    def forking(pane_id: str) -> PaneFacts | None:
        nonlocal polls
        polls += 1
        tmux.set_command(pane_id, "tmux" if polls < 3 else "claude")
        return original(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", forking)
    fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert polls == 3, "the two 'tmux' polls were not mistaken for a running agent"


def test_spawn_prompt_stops_waiting_at_the_timeout_and_types_anyway(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert PROMPT_TIMEOUT <= clock.now < PROMPT_TIMEOUT + 5
    assert any("did not come up within 20 s" in note for note in receipt.notes)
    assert (receipt.agent.pane_id, "paste", "hello") in tmux.typed


def test_spawn_prompt_is_not_typed_into_a_dead_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmux.spawn_window

    def spawn_and_crash(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        tmux.die(window.pane_id, 1)
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_and_crash)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert tmux.typed == []
    assert any("exited before the prompt" in note for note in receipt.notes)


def test_spawn_can_keep_native_agent_teams_on(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, disable_native_agent_teams=False)
    agent = _coder(project)
    env = tmux.spawned[0]["env"]
    assert env == {"AISQUARE_FLEET_AGENT": agent.id}


def test_spawn_without_tmux_is_fleet_unavailable(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    tmux.installed = False
    with pytest.raises(FleetUnavailable, match="tmux is not installed"):
        fleet_service.spawn(project, "manager")
    with store_session() as store:
        assert store.fleet_agents(project.id) == []


def test_spawn_without_the_agent_binary_names_it_and_who_chose_it(
    tmux: FakeTmux, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with pytest.raises(FleetError, match=r"'claude' is not on your PATH \(chosen by: default\)"):
        fleet_service.spawn(project, "manager")
    assert tmux.spawned == []


# --- derived state (§5.1) --------------------------------------------------------------


@pytest.mark.parametrize("state", ["working", "waiting", "attention"])
def test_a_fresh_board_row_decides_the_state(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, state: str
) -> None:
    agent = _coder(project)
    _board_session(agent, state)
    status = fleet_service.status_of(agent)
    assert status.state == state and status.detail is None
    assert status.session is not None and status.session.id == agent.session_id
    assert status.tmux_session == f"asq-{_codename(project)}"
    [listed] = fleet_service.list_agents(project)
    assert listed.state == state and listed.agent == agent


def test_a_stale_board_row_defers_to_the_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "working", seen_ago=team_service._STALE_AFTER + timedelta(minutes=1))
    assert fleet_service.status_of(agent).state == "waiting"
    tmux.printed(agent.pane_id)
    assert fleet_service.status_of(agent).state == "working"
    # Control: a row seen just now wins over recent output.
    _board_session(agent, "attention")
    assert fleet_service.status_of(agent).state == "attention"


def test_recent_output_is_working_and_old_output_is_waiting_without_hooks(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project, binary=sys.executable)  # no --session-id: tmux is the only source
    assert fleet_service.status_of(agent).state == "waiting", "no output yet"
    tmux.printed(agent.pane_id, ago=ACTIVITY_WINDOW - timedelta(seconds=1))
    assert fleet_service.status_of(agent).state == "working"
    tmux.printed(agent.pane_id, ago=ACTIVITY_WINDOW + timedelta(seconds=2))
    status = fleet_service.status_of(agent)
    assert status.state == "waiting" and status.detail == "no hooks"


def test_last_output_times_come_from_one_list_panes_and_fail_open() -> None:
    """The parser over tmux's own answer shape — and nothing when tmux cannot answer."""
    calls: list[list[str]] = []

    def answering(argv: Sequence[str], stdin: bytes | None) -> Completed:
        calls.append(list(argv))
        return Completed(0, "%3|~|1700000000\n%7|~|1700000042\n%9|~|\nbroken line\n", "")

    srv = TmuxServer("asq-test", binary=sys.executable, conf=Path("/fake.conf"), runner=answering)
    times = fleet_service._activity_times(srv)
    assert times == {
        "%3": datetime.fromtimestamp(1700000000, tz=UTC),
        "%7": datetime.fromtimestamp(1700000042, tz=UTC),
    }, "a pane with no time and a malformed line are skipped, not guessed"
    assert len(calls) == 1 and calls[0][-4:] == [
        "list-panes",
        "-a",
        "-F",
        "#{pane_id}|~|#{window_activity}",
    ]

    def refusing(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", "no server running")

    dead = TmuxServer("asq-test", binary=sys.executable, conf=Path("/fake.conf"), runner=refusing)
    assert fleet_service._activity_times(dead) == {}


def test_a_dead_pane_is_exited_whatever_the_board_says(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.die(agent.pane_id, 3)
    status = fleet_service.status_of(agent)
    assert status.state == "exited" and status.detail == "exit 3"


def test_a_vanished_pane_is_lost(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.vanish(agent.pane_id)
    status = fleet_service.status_of(agent)
    assert status.state == "lost" and status.detail == "pane gone"


def test_a_pane_under_the_old_session_name_is_still_found(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A codename rename tmux never heard of must not read as every agent lost."""
    agent = _coder(project)
    tmux.sessions["asq-elsewhere"] = tmux.sessions.pop(f"asq-{_codename(project)}")
    assert fleet_service.status_of(agent).state == "waiting"


def test_an_ended_row_is_exited_with_its_status_and_hidden_from_the_live_list(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    fleet_service.stop(project, agent.label)  # the fake honours /exit → status 0
    assert fleet_service.list_agents(project) == []
    [status] = fleet_service.list_agents(project, live_only=False)
    assert status.state == "exited" and status.detail == "exit 0"


def test_without_tmux_the_state_is_unknown_unless_the_board_knows(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    tmux.installed = False
    status = fleet_service.status_of(agent)
    assert status.state == "unknown" and status.detail == "tmux unavailable"
    _board_session(agent, "working")
    assert fleet_service.status_of(agent).state == "working"


def test_list_agents_orders_by_creation_and_knows_the_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    listed = fleet_service.list_agents(project)
    assert [status.agent.id for status in listed] == [manager.id, coder.id]
    assert {status.tmux_session for status in listed} == {f"asq-{_codename(project)}"}


# --- manager_of / resolve_project ------------------------------------------------------


def test_manager_of_is_the_live_manager_or_none(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    assert fleet_service.manager_of(project) is None
    manager = fleet_service.spawn(project, "manager").agent
    assert fleet_service.manager_of(project) == manager
    fleet_service.stop(project, "manager", force=True)
    assert fleet_service.manager_of(project) is None


def test_resolve_project_by_codename(project: ProjectInfo) -> None:
    named = fleet_service.ensure_codename(project)
    assert named.codename is not None
    assert fleet_service.resolve_project(named.codename).id == project.id
    with pytest.raises(fleet_service.NoSuchProject):
        fleet_service.resolve_project("nothing-here")


def test_resolve_project_names_the_codenames_when_basenames_collide(tmp_path: Path) -> None:
    """``~/work/api`` and ``~/oss/api`` (§5.7): the codename is what tells them apart."""
    infos = []
    for parent in ("work", "oss"):
        root = tmp_path / parent / "api"
        root.mkdir(parents=True)
        info = fleet_service.ensure_codename(team_project(root))
        infos.append(info)
    with pytest.raises(fleet_service.NoSuchProject, match="matches several projects") as caught:
        fleet_service.resolve_project("api")
    for info in infos:
        assert info.codename and info.codename in str(caught.value)
        assert fleet_service.resolve_project(info.codename).id == info.id


def test_the_pure_name_rules() -> None:
    """Labels, slugs and branches — the rules every other function builds on (§5.7)."""
    assert fleet_service.session_name("amber-otter") == "asq-amber-otter"
    for good in ("manager", "coder-auth", "tester-py311", "a1", "x" * 24):
        assert fleet_service.is_label(good), good
    for bad in ("", "a", "Coder", "coder.auth", "coder:auth", "coder auth", "1coder", "x" * 25):
        assert not fleet_service.is_label(bad), bad
    assert fleet_service.slugify("Wire the auth flow!") == "wire-the-auth-flow"
    assert fleet_service.slugify("--Émigré--") == "migr"
    assert fleet_service.slugify("a" * 40) == "a" * 32
    assert fleet_service.slugify("abcdefg-" * 5) == "abcdefg-" * 3 + "abcdefg", (
        "clip lands on a dash"
    )
    assert fleet_service.slugify("!!!") == ""
    assert (
        fleet_service.branch_name("amber-otter", task_id="tsk_01k9q8p3zzzz", title="Wire auth")
        == "fleet/amber-otter/01k9q8p3-wire-auth"
    )
    assert fleet_service.branch_name("amber-otter", task_id="tsk_01k9q8p3", title=None) == (
        "fleet/amber-otter/01k9q8p3"
    )
    assert fleet_service.branch_name("amber-otter", task_id=None, title="coder-auth") == (
        "fleet/amber-otter/coder-auth"
    )
    assert fleet_service.branch_name("amber-otter", task_id=None, title=None) == (
        "fleet/amber-otter/work"
    )
    with pytest.raises(FleetError, match="not valid"):
        fleet_service.next_label(ProjectInfo(id="prj_x", root=Path("/x")), "coder", wanted="Bad")


# --- tell ----------------------------------------------------------------------------


def test_tell_types_into_a_waiting_agent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert result.delivered and "typed" in result.how
    assert tmux.typed == [
        (agent.pane_id, "paste", "please rebase"),
        (agent.pane_id, "key", "Enter"),
    ]
    assert _events(project, "note") == []


@pytest.mark.parametrize("state", ["working", "attention"])
def test_tell_files_a_board_note_when_the_agent_is_not_waiting(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, state: str
) -> None:
    agent = _coder(project)
    _board_session(agent, state)
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert not result.delivered and state in result.how and "board note" in result.how
    assert tmux.typed == [], "never typed into a busy agent, never into a permission prompt"
    with store_session() as store:
        notes = [e for e in store.recent_events(project.id) if e.kind == "note"]
    assert len(notes) == 1 and notes[0].text == "please rebase" and notes[0].to_role == "coder-1"


def test_tell_falls_back_to_a_note_when_tmux_cannot_type(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.fail_input = True
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert not result.delivered and "tmux could not type" in result.how
    assert _events(project, "note") == ["please rebase"]


def test_tell_an_unknown_label_is_no_such_agent(tmux: FakeTmux, project: ProjectInfo) -> None:
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.tell(project, "coder-9", "hello")
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.stop(project, "coder-9")


def test_tell_attributes_the_note_to_the_sender(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    coder = _coder(project)
    _board_session(coder, "working")
    result = fleet_service.tell(project, "coder-1", "rebase first", sender=manager.session_id)
    assert not result.delivered
    with store_session() as store:
        [note] = [e for e in store.recent_events(project.id) if e.kind == "note"]
    assert note.session_id == manager.session_id and note.to_role == "coder-1"
    # Negative control: a sender the board has never seen is refused, not invented.
    with pytest.raises(FleetError, match="unknown sender session 'nope'"):
        fleet_service.tell(project, "coder-1", "again", sender="nope")
    assert _events(project, "note") == ["rebase first"]


# --- stop ----------------------------------------------------------------------------


def test_stop_exits_gracefully_and_records_the_status(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1")
    assert tmux.typed == [(agent.pane_id, "literal", "/exit"), (agent.pane_id, "key", "Enter")]
    assert ended.ended_at is not None and ended.exit_status == 0
    assert tmux.killed == [agent.pane_id]
    assert fleet_service.list_agents(project) == []


def test_stop_outwaits_the_dead_without_status_gap(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A dead pane whose status has not landed yet is polled again, not recorded.

    The gap is real: tmux 3.4's own -vv server log showed a poll where
    ``pane_dead`` was 1 while ``pane_dead_status`` still expanded to '' —
    reading that as final cost the exit status (and CI three red runs).
    """
    tmux.status_lag = 2
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1")
    assert ended.exit_status == 0, "the status that landed a beat later was collected"
    assert tmux.killed == [agent.pane_id]
    assert tmux.status_lag == 0, "the gap reads were consumed — the lag was exercised"


def test_stop_kills_after_the_grace_period_when_exit_is_ignored(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    tmux.honours_exit = False
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1", grace=2.0)
    assert clock.now >= 2.0, "waited the grace period"
    assert tmux.killed == [agent.pane_id] and ended.exit_status is None


def test_stop_force_skips_the_exit(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1", force=True)
    assert tmux.typed == [] and tmux.killed == [agent.pane_id] and clock.now == 0
    assert ended.ended_at is not None


def test_stop_ends_the_row_even_when_the_pane_is_already_gone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    tmux.vanish(agent.pane_id)
    ended = fleet_service.stop(project, "coder-1")
    assert ended.ended_at is not None and tmux.killed == []


# --- reap ----------------------------------------------------------------------------


def test_reap_ends_dead_panes_and_tells_the_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    tmux.die(coder.pane_id, 2)

    report = fleet_service.reap(project)

    assert [a.id for a in report.ended] == [coder.id] and report.ended[0].exit_status == 2
    assert report.lost == [] and report.worktrees_removed == []
    with store_session() as store:
        events = [e for e in store.recent_events(project.id) if e.kind == "agent_exited"]
    assert len(events) == 1
    assert events[0].text == "coder-1 exited (2)" and events[0].session_id == coder.session_id
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [manager.id]
    # Idempotent: a second pass finds nothing new and emits nothing more.
    assert fleet_service.reap(project).ended == []
    assert len(_events(project, "agent_exited")) == 1


def test_reap_marks_vanished_panes_lost_without_an_exit_event(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    tmux.vanish(coder.pane_id)
    report = fleet_service.reap(project)
    assert [a.id for a in report.lost] == [coder.id] and report.ended == []
    assert report.lost[0].ended_at is not None and report.lost[0].exit_status is None
    assert _events(project, "agent_exited") == []


def test_reap_leaves_live_agents_alone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    report = fleet_service.reap(project)
    assert report.ended == [] and report.lost == []
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]


def test_reap_nudges_a_waiting_manager_when_an_agent_exits(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    tmux.set_command(manager.pane_id, "claude")
    coder = _coder(project)
    tmux.die(coder.pane_id, 1)
    fleet_service.reap(project)
    assert tmux.typed == [
        (manager.pane_id, "literal", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]


def test_reap_does_nothing_when_tmux_cannot_be_asked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    tmux.installed = False
    report = fleet_service.reap(project)
    assert report.ended == [] and report.lost == []
    tmux.installed = True
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]


def test_reap_over_every_project(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    here = _coder(project)
    there = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    tmux.die(here.pane_id, 0)
    tmux.die(there.pane_id, 1)
    report = fleet_service.reap()
    assert {a.id for a in report.ended} == {here.id, there.id}


def test_reap_removes_a_merged_worktree_and_keeps_an_unmerged_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    merged = fleet_service.spawn(project, "coder", label="coder-merged").agent
    unmerged = fleet_service.spawn(project, "coder", label="coder-open").agent
    (unmerged.cwd / "work.txt").write_text("wip\n", encoding="utf-8")
    _git("add", "work.txt", cwd=unmerged.cwd)
    _git("commit", "-q", "-m", "wip", cwd=unmerged.cwd)
    fleet_service.stop(project, "coder-merged", force=True)
    fleet_service.stop(project, "coder-open", force=True)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [merged.cwd]
    assert not merged.cwd.exists() and unmerged.cwd.exists()
    assert fleet_service.reap(project).worktrees_removed == []


def test_reap_never_removes_a_live_agents_worktree(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    fleet_service.stop(project, "coder-auth", force=True)
    second = fleet_service.spawn(project, "coder", label="coder-auth")
    assert second.agent.cwd == first.cwd, "a respawn on the same label reuses the tree"
    assert any("reusing the existing worktree" in note for note in second.notes)
    report = fleet_service.reap(project)
    assert report.worktrees_removed == [] and first.cwd.exists()


def test_reap_leaves_worktrees_alone_when_they_are_still_live(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    live = fleet_service.spawn(project, "coder").agent
    assert fleet_service.reap(project).worktrees_removed == []
    assert live.cwd.exists()


# --- nudge (§7.3) ------------------------------------------------------------------------


def _waiting_manager(tmux: FakeTmux, project: ProjectInfo) -> FleetAgent:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    tmux.set_command(manager.pane_id, "claude")
    return manager


def test_nudge_types_one_fixed_line_into_a_waiting_manager(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = _waiting_manager(tmux, project)
    assert fleet_service.nudge_manager(project.id, reason="task_review") is True
    assert tmux.typed == [
        (manager.pane_id, "literal", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]
    assert not NUDGE_TEXT.startswith(("/", "!")), "Claude Code's command prefixes"
    assert "task_review" not in NUDGE_TEXT, "the nudge carries nothing; the delta does"


def test_nudge_is_debounced_through_team_meta(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = _waiting_manager(tmux, project)
    assert fleet_service.nudge_manager(project.id, reason="a") is True
    assert fleet_service.nudge_manager(project.id, reason="b") is False
    assert len(tmux.typed) == 2, "one nudge, not two"
    stale = (datetime.now(tz=UTC) - fleet_service.NUDGE_DEBOUNCE - timedelta(seconds=1)).isoformat()
    with store_session() as store:
        store.set_meta(f"nudge:{manager.session_id}", stale)
    assert fleet_service.nudge_manager(project.id, reason="c") is True
    assert len(tmux.typed) == 4


@pytest.mark.parametrize("state", ["attention", "working"])
def test_nudge_never_touches_a_manager_that_is_not_waiting(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, state: str
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, state)
    tmux.set_command(manager.pane_id, "claude")
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


@pytest.mark.parametrize("command", ["bash", PYTHON_LAUNCHER, "zsh"])
def test_nudge_needs_the_agent_in_the_foreground(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, command: str
) -> None:
    manager = _waiting_manager(tmux, project)
    tmux.set_command(manager.pane_id, command)
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_needs_a_live_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = _waiting_manager(tmux, project)
    tmux.die(manager.pane_id, 0)
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    tmux.vanish(manager.pane_id)
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_without_a_manager_or_its_board_row_is_false(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    fleet_service.spawn(project, "manager")  # no hooks have fired yet: no team_session row
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_never_raises(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waiting_manager(tmux, project)
    tmux.fail_input = True
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    tmux.fail_input = False
    tmux.installed = False
    assert fleet_service.nudge_manager(project.id, reason="x") is False

    def broken() -> Iterator[None]:
        raise RuntimeError("context.db is toast")

    monkeypatch.setattr(fleet_service, "store_session", broken)
    assert fleet_service.nudge_manager(project.id, reason="x") is False


# --- pause / resume ------------------------------------------------------------------------


def test_pause_and_resume_flip_the_board_signal(project: ProjectInfo) -> None:
    assert not fleet_service.is_paused(project)
    fleet_service.pause(project)
    assert fleet_service.is_paused(project)
    assert _events(project, "signal") == ["fleet-paused: on"]
    fleet_service.resume(project)
    assert not fleet_service.is_paused(project)
    assert _events(project, "signal")[-1] == "fleet-paused: off (was on)"


def test_pause_with_the_team_disabled_is_a_fleet_error(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    with pytest.raises(FleetError, match="pause signal"):
        fleet_service.pause(project)
    assert not fleet_service.is_paused(project)


# --- rename --------------------------------------------------------------------------


def test_rename_sets_the_codename_and_renames_the_tmux_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    old = _codename(project)
    updated = fleet_service.rename(project, "amber-otter")
    assert updated.codename == "amber-otter" and _codename(project) == "amber-otter"
    assert tmux.renamed == [(f"asq-{old}", "asq-amber-otter")]
    [status] = fleet_service.list_agents(project)
    assert status.tmux_session == "asq-amber-otter" and status.state == "waiting"


@pytest.mark.parametrize("bad", ["Amber-Otter", "amber_otter", "amber-otter-x"])
def test_rename_rejects_a_bad_shape(tmux: FakeTmux, project: ProjectInfo, bad: str) -> None:
    before = fleet_service.ensure_codename(project).codename
    with pytest.raises(FleetError, match="adjective-animal"):
        fleet_service.rename(project, bad)
    assert _codename(project) == before and tmux.renamed == []


def test_rename_refuses_a_codename_another_project_holds(
    tmux: FakeTmux, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    fleet_service.rename(plain_project, "amber-otter")
    with pytest.raises(FleetError, match="already taken"):
        fleet_service.rename(project, "amber-otter")
    # Negative control: a free codename is accepted by the same call.
    assert fleet_service.rename(project, "ruby-fox").codename == "ruby-fox"


def test_rename_without_tmux_still_renames_the_row(tmux: FakeTmux, project: ProjectInfo) -> None:
    fleet_service.ensure_codename(project)
    tmux.installed = False
    assert fleet_service.rename(project, "quiet-lynx").codename == "quiet-lynx"
    assert tmux.renamed == []


def test_rename_with_no_session_yet_touches_nothing_in_tmux(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    fleet_service.ensure_codename(project)
    assert fleet_service.rename(project, "quiet-lynx").codename == "quiet-lynx"
    assert tmux.renamed == [] and tmux.sessions == {}
    # Renaming to the current name is a no-op, not a clash with itself.
    assert fleet_service.rename(project, "quiet-lynx").codename == "quiet-lynx"


# --- attach --------------------------------------------------------------------------


def test_attach_argv_targets_the_project_session_exactly(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    argv = fleet_service.attach_argv(project)
    assert argv[-3:] == ["attach-session", "-t", f"=asq-{_codename(project)}"]


def test_attach_argv_refuses_when_there_is_no_session_to_attach_to(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    """The CLI execs this argv, so a session that does not exist is refused here,
    with a message — not handed to tmux to fail on (and not exec'd by a test sweep)."""
    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(project)
    assert tmux.sessions == {}


def test_attach_argv_without_tmux_is_fleet_unavailable(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    tmux.installed = False
    with pytest.raises(FleetUnavailable):
        fleet_service.attach_argv(project)


# --- the real thing, once ------------------------------------------------------------


_WINDOW_PROBE = (
    "#{window_id} #{window_name} #{pane_id} dead=#{pane_dead} status=#{pane_dead_status}"
)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_spawn_and_stop_on_a_real_tmux_server(
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End to end on a private socket: the command we build is one ``aisquare launch``
    accepts, the fake agent receives our flags, and ``/exit`` + Enter reaches it.

    Readiness is read off the SCREEN here, not off ``pane_current_command``: the
    fake agent is a ``#!/bin/sh`` script, which tmux reports as ``sh`` — the one
    shape the heuristic deliberately never trusts.
    """
    monkeypatch.setattr(fleet_service, "_sleep", time.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", time.monotonic)
    monkeypatch.chdir(tmp_path)  # the -vv server logs land in the server's cwd

    class LoggedServer(TmuxServer):
        """The real server, with tmux's own logging on — the CI autopsy channel."""

        def argv(self, *args: str) -> list[str]:
            base = super().argv(*args)
            return [base[0], "-vv", *base[1:]]

    def _autopsy(server: TmuxServer, session: str) -> str:
        """Everything tmux can still tell us, for an assert message on a runner."""
        parts: list[str] = []
        for label, args in (
            ("sessions", ("list-sessions", "-F", "#{session_name}")),
            ("windows", ("list-panes", "-s", "-t", f"={session}", "-F", _WINDOW_PROBE)),
        ):
            try:
                parts.append(f"{label}: {server.run(*args)!r}")
            except TmuxError as exc:
                parts.append(f"{label}: TmuxError({exc})")
        interesting = re.compile(
            r"destroy|kill|exited|dead|signal|lost|session_|window_|spawn|got \\d+|loop exit"
        )
        for log in sorted(tmp_path.glob("tmux-*.log")):
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            events = [line for line in lines if interesting.search(line)]
            parts.append(
                f"--- {log.name}: {len(lines)} lines; the events ---\n"
                + "\n".join(events[-160:])
                + "\n--- raw tail ---\n"
                + "\n".join(lines[-40:])
            )
        return "\n".join(parts)

    # A socket of our OWN: another file's test on a shared name can be mid
    # kill-server when this new-session arrives, which lands the session on the
    # dying server — it then vanishes with it (seen on CI as an empty window
    # list). Every real-tmux test in this suite now suffixes its socket.
    real = LoggedServer(f"asq-test-{os.getpid()}-fleet")
    monkeypatch.setattr(fleet_service, "server", lambda config=None: real)
    try:
        receipt = fleet_service.spawn(project, "coder", worktree=False)
        pane = receipt.agent.pane_id
        assert pane.startswith("%")
        deadline = time.monotonic() + 60
        screen = ""
        while time.monotonic() < deadline:
            facts = real.pane_facts(pane)
            assert facts is not None, "the pane vanished"
            screen = "\n".join(real.capture(pane).lines)
            assert not facts.dead, screen
            if "fake claude:" in screen:
                break
            time.sleep(0.2)
        assert "fake claude:" in screen, screen
        # The control behind the "gone" hunts: this server must keep dead panes.
        assert real.run("show-options", "-gv", "remain-on-exit").strip() == "on"
        assert f"--session-id {receipt.agent.session_id}" in screen, screen
        assert "--name coder-1" in screen and "--permission-mode auto" in screen, screen
        output_at = fleet_service._activity_times(real)
        assert pane in output_at, output_at
        assert datetime.now(tz=UTC) - output_at[pane] < timedelta(minutes=1)
        [status] = fleet_service.list_agents(project)
        assert status.state in ("working", "waiting"), status

        ended = fleet_service.stop(project, "coder-1", grace=15.0)

        if ended.exit_status != 0:
            # print(), not the assert message: pytest truncates long reprs, and
            # the whole point is the server log — captured stdout survives whole.
            print(f"ENDED ROW: {ended!r}")
            print(_autopsy(real, receipt.tmux_session))
        assert ended.exit_status == 0
        assert real.list_windows(receipt.tmux_session) == []
    finally:
        with suppress(TmuxError):
            real.run("kill-server")
