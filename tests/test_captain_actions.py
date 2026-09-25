"""The captain's Actions MCP server: a fixed tool vocabulary, one audit event per call.

The captain is a Claude Code session and never types into a pane itself: every
effect is one of these tools, with one fixed meaning, and every call — a
success or a refusal — leaves exactly one ``captain_action`` event on a board.
The fleet services are replaced by recorders here, so each test pins what a
tool ASKED the fleet to do; the board, the tasks and the state file are real.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

import anyio
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.ids import new_agent_id, new_event_id, new_task_id
from aisquare.core.store import store_session
from aisquare.core.tmux import Capture, PaneFacts, TmuxError
from aisquare.core.workspace import project_id_for
from aisquare.models import (
    FleetAgent,
    FleetAgentState,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import fleet, mcp_server
from aisquare.services import team as team_service
from aisquare.services.captain import actions
from aisquare.services.captain import queue as captain_queue
from aisquare.services.captain import screen as screen_reader
from aisquare.services.captain import state as captain_state
from tests import captain_screens as shots
from tests.rendered import plain

CONTRACT_TOOLS = frozenset(
    {
        "projects",
        "board",
        "attention",
        "next",
        "resolve",
        "snooze",
        "since",
        "read_pane",
        "tell",
        "ask_manager",
        "spawn",
        "stop",
        "restart",
        "attach_persona",
        "task",
        "note",
        "press",
        "paste",
        "ui",
        "act",
        "speak",
        "thinking",
        "bt",
        "wololo",
    }
)
"""The tool list posted on the T1 card (seq 13010) — the vocabulary T2..T7 build on."""


# --- recorders --------------------------------------------------------------------------


@dataclass
class Pane:
    """One agent's pane as the tools see it: what is on screen, what was typed."""

    command: str = "claude"
    screen: list[str] = field(default_factory=lambda: ["$ ", "ready"])
    history: list[str] = field(default_factory=list)
    keys: list[tuple[str, ...]] = field(default_factory=list)
    pastes: list[str] = field(default_factory=list)
    typed: list[tuple[str, str]] = field(default_factory=list)
    """Everything that reached the pane, in order: ``("keys", "Enter")``, ``("paste", text)``."""
    answers: dict[str, list[str]] = field(default_factory=dict)
    """What the screen becomes when a key lands (T1b): a chooser answered by ``1`` goes
    back to the input box; a key the prompt ignores is simply not here."""

    def facts(self, pane_id: str) -> PaneFacts:
        return PaneFacts(
            pane_id=pane_id,
            width=120,
            height=len(self.screen),
            cursor_x=0,
            cursor_y=0,
            cursor_visible=True,
            alternate_on=False,
            history_size=len(self.history),
            dead=False,
            dead_status=None,
            in_mode=False,
            current_command=self.command,
            title="",
        )


class FakeServer:
    """The tmux server behind the fleet: routes each call to the pane it names."""

    def __init__(self, panes: dict[str, Pane]) -> None:
        self._panes = panes

    def send_keys(self, pane_id: str, *keys: str) -> None:
        pane = self._panes[pane_id]
        pane.keys.append(keys)
        pane.typed.append(("keys", " ".join(keys)))
        if keys and keys[0] in pane.answers:
            pane.screen = list(pane.answers[keys[0]])

    def paste(self, pane_id: str, text: str) -> None:
        self._panes[pane_id].pastes.append(text)
        self._panes[pane_id].typed.append(("paste", text))

    def pane_facts(self, pane_id: str) -> PaneFacts:
        return self._panes[pane_id].facts(pane_id)

    def capture(
        self, pane_id: str, *, scrollback: int = 0, height: int | None = None, flags: bool = False
    ) -> Capture:
        pane = self._panes[pane_id]
        offset = min(max(0, scrollback), len(pane.history))
        rows = pane.history + pane.screen
        top = len(pane.history) - offset
        return Capture(
            lines=rows[top : top + len(pane.screen)], facts=pane.facts(pane_id), scrollback=offset
        )


class Fleet:
    """Recorders standing in for ``services.fleet``: what each tool asked of it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.states: dict[str, FleetAgentState] = {}
        self.panes: dict[str, Pane] = {}
        self.on_tell: Callable[[ProjectInfo, str, str], None] | None = None
        self.effects: list[TeamEvent] = []
        """The board events the stand-ins wrote as their effect (what the real services write)."""
        self.crowd = False
        """Also write the same kind of event for ANOTHER agent first — a busy board."""
        self.relabel: str | None = None
        """A restart whose replacement re-picked its label, with no board session."""
        for name in ("tell", "spawn", "stop", "restart", "attach_persona", "list_agents"):
            monkeypatch.setattr(fleet, name, getattr(self, name))
        monkeypatch.setattr(fleet, "status_of", self.status_of)
        monkeypatch.setattr(fleet, "server_for", lambda socket, config=None: FakeServer(self.panes))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def status_of(self, agent: FleetAgent) -> FleetAgentStatus:
        return FleetAgentStatus(agent=agent, state=self.states.get(agent.label, "waiting"))

    def list_agents(
        self, project: ProjectInfo, *, live_only: bool = True
    ) -> list[FleetAgentStatus]:
        with store_session() as store:
            rows = store.fleet_agents(project.id, live_only=True)
        return [self.status_of(row) for row in rows]

    def tell(
        self, project: ProjectInfo, label: str, text: str, *, sender: str | None = None
    ) -> fleet.TellResult:
        self.calls.append(
            ("tell", {"project": project.id, "label": label, "text": text, "sender": sender})
        )
        if self.on_tell is not None:
            self.on_tell(project, label, text)
        return fleet.TellResult(True, "typed into its pane (it was waiting)")

    def spawn(self, project: ProjectInfo, role: str, **kwargs: Any) -> fleet.SpawnReceipt:
        self.calls.append(("spawn", {"project": project.id, "role": role, **kwargs}))
        agent = _row(project, kwargs.get("label") or f"{role}-9", role, "%9")
        return fleet.SpawnReceipt(
            agent=agent, asked_label=kwargs.get("label"), tmux_session="asq-x", notes=["noted"]
        )

    def _board_row(self, project: ProjectInfo, label: str) -> FleetAgent:
        with store_session() as store:
            row = store.fleet_agent_by_label(project.id, label, live_only=False)
        return row if row is not None else _row(project, label, "coder", "%1")

    def _effect(
        self,
        project: ProjectInfo,
        kind: str,
        text: str,
        *,
        session_id: str | None = None,
        to_role: str | None = None,
    ) -> TeamEvent:
        with store_session() as store:
            event = store.add_team_event(
                TeamEvent(
                    id=new_event_id(),
                    project_id=project.id,
                    session_id=session_id,
                    kind=kind,
                    text=text,
                    to_role=to_role,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return event

    def stop(self, project: ProjectInfo, label: str, **kwargs: Any) -> fleet.StopReceipt:
        self.calls.append(("stop", {"project": project.id, "label": label, **kwargs}))
        row = self._board_row(project, label)
        if self.crowd:
            self._effect(project, "agent_exited", "coder-2 exited (0)", session_id="sess-coder-2")
        self.effects.append(
            self._effect(project, "agent_exited", f"{label} exited (0)", session_id=row.session_id)
        )
        return fleet.StopReceipt(agent=row, released=[])

    def restart(self, project: ProjectInfo, label: str, **kwargs: Any) -> fleet.RestartReceipt:
        self.calls.append(("restart", {"project": project.id, "label": label, **kwargs}))
        row = self._board_row(project, label)
        if self.relabel is not None:
            row = row.model_copy(update={"label": self.relabel, "session_id": None})
        if self.crowd:
            self._effect(project, "restarted", "coder-2 restarted", session_id="sess-coder-2")
        self.effects.append(
            self._effect(
                project,
                "restarted",
                f"{label} restarted — resumed its session",
                session_id=row.session_id,
            )
        )
        return fleet.RestartReceipt(
            replaced=row, started=row, resumed=True, was_running=False, tmux_session="asq-x"
        )

    def attach_persona(
        self, project: ProjectInfo, label: str, name: str, *, sender: str | None = None
    ) -> fleet.AttachReceipt:
        self.calls.append(
            (
                "attach_persona",
                {"project": project.id, "label": label, "name": name, "sender": sender},
            )
        )
        if self.crowd:
            self._effect(
                project, "persona_attached", "persona x attached to coder-2", to_role="coder-2"
            )
        self.effects.append(
            self._effect(
                project,
                "persona_attached",
                f"persona {name} attached to {label}",
                session_id=sender,
                to_role=label,
            )
        )
        return fleet.AttachReceipt(
            agent=_row(project, label, "coder", "%1"),
            persona=name,
            replaced=None,
            delivered="typed",
            how="typed into its pane (it was waiting)",
        )


def _row(project: ProjectInfo, label: str, role: str, pane: str) -> FleetAgent:
    return FleetAgent(
        id=new_agent_id(),
        project_id=project.id,
        label=label,
        role=role,
        pane_id=pane,
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
    )


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def projects(tmp_path: Path) -> dict[str, ProjectInfo]:
    """Three onboarded projects, each with a codename — the captain spans all of them."""
    made: dict[str, ProjectInfo] = {}
    with store_session() as store:
        for name, codename in (
            ("alpha", "amber-otter"),
            ("beta", "blue-heron"),
            ("gamma", "green-finch"),
        ):
            root = (tmp_path / name).resolve()
            root.mkdir()
            info = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
            made[name] = store.set_codename(info.id, codename)
    return made


@pytest.fixture
def alpha(projects: dict[str, ProjectInfo]) -> ProjectInfo:
    return projects["alpha"]


@pytest.fixture
def fleet_rec(monkeypatch: pytest.MonkeyPatch) -> Fleet:
    return Fleet(monkeypatch)


@pytest.fixture
def agents(alpha: ProjectInfo, fleet_rec: Fleet) -> dict[str, FleetAgent]:
    """alpha's fleet: a manager and two coders on the board, one coder that never joined."""
    rows: dict[str, FleetAgent] = {}
    now = datetime.now(tz=UTC)
    with store_session() as store:
        for label, role, pane, session in (
            ("manager", "manager", "%0", "sess-manager"),
            ("coder-1", "coder", "%1", "sess-coder-1"),
            ("coder-2", "coder", "%2", "sess-coder-2"),
            ("coder-3", "coder", "%3", None),
        ):
            row = _row(alpha, label, role, pane).model_copy(update={"session_id": session})
            rows[label] = store.upsert_fleet_agent(row)
            fleet_rec.panes[pane] = Pane()
            if session is not None:
                store.upsert_session(
                    TeamSession(
                        id=session, project_id=alpha.id, role=role, started_at=now, last_seen_at=now
                    )
                )
    return rows


@dataclass
class Clock:
    """``ask_manager``'s clock: sleeping advances it, so a timeout costs no real time."""

    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(actions, "_clock", fake.monotonic)
    monkeypatch.setattr(actions, "_sleep", fake.sleep)
    return fake


# --- helpers ----------------------------------------------------------------------------


def ok(result: str) -> dict[str, Any]:
    data = json.loads(result)
    assert isinstance(data, dict), result
    return data


def refused(call: Callable[[], str]) -> str:
    with pytest.raises(ToolError) as caught:
        call()
    return str(caught.value)


def audit(project_id: str) -> list[dict[str, Any]]:
    """The ``captain_action`` events on one board, oldest first, decoded."""
    with store_session() as store:
        events = store.filtered_events(project_id, kind="captain_action", since_seq=0, limit=5000)
    decoded = [json.loads(event.text) for event in events]
    assert all(isinstance(item, dict) for item in decoded)
    return decoded


def audit_count(projects: dict[str, ProjectInfo]) -> int:
    boards = [p.id for p in projects.values()] + [captain_state.home_project().id]
    return sum(len(audit(board)) for board in boards)


def add_events(
    project: ProjectInfo, session_id: str | None, count: int, *, kind: str = "note"
) -> list[TeamEvent]:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        return [
            store.add_team_event(
                TeamEvent(
                    id=new_event_id(),
                    project_id=project.id,
                    session_id=session_id,
                    kind=kind,
                    text=f"event {n}",
                    created_at=now,
                )
            )
            for n in range(count)
        ]


def add_task(project: ProjectInfo, title: str) -> TeamTask:
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


def _events_of(project: ProjectInfo, kind: str) -> list[TeamEvent]:
    with store_session() as store:
        return store.filtered_events(project.id, kind=kind, since_seq=0, limit=500)


def task_now(task_id: str) -> TeamTask:
    with store_session() as store:
        task = store.get_task(task_id)
    assert task is not None
    return task


def write_config(text: str) -> None:
    paths.ensure_home()
    paths.config_path().write_text(text, encoding="utf-8")


def _unix_socket() -> socket.socket:
    """A unix stream socket. The guard is one mypy reads: Windows typeshed has no AF_UNIX."""
    if sys.platform == "win32":
        raise NotImplementedError("unix sockets: the ui tests skip on Windows")
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


@pytest.fixture
def short_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short folder of the test's own standing in for /tmp, so a long home's socket
    folder is made there — never in the machine's shared /tmp. In-process only: a child
    server process cannot see this patch."""
    folder = Path(tempfile.mkdtemp(prefix="asq", dir=None if sys.platform == "win32" else "/tmp"))
    monkeypatch.setattr(captain_state, "_short_root", lambda: folder)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


UNIX_SOCKETS = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
    reason="the ui socket is a unix socket",
)


# --- the vocabulary and the server ------------------------------------------------------


def test_the_server_exposes_exactly_the_contract_tools() -> None:
    async def go() -> set[str]:
        async with Client(actions.build_server(), mode="legacy") as client:
            listed = await client.list_tools()
            return {tool.name for tool in listed.tools}

    assert anyio.run(go) == CONTRACT_TOOLS
    assert {name for name, _ in actions.TOOLS} == CONTRACT_TOOLS


def test_every_tool_records_the_owners_words() -> None:
    """``utterance`` is the last parameter of every tool: the audit's "why"."""
    import inspect

    for name, tool in actions.TOOLS:
        params = list(inspect.signature(tool).parameters.values())
        assert params[-1].name == "utterance", name
        assert params[-1].default == "", name


def test_the_server_starts_and_answers_in_under_a_second() -> None:
    async def go() -> int:
        async with Client(actions.build_server(), mode="legacy") as client:
            return len((await client.list_tools()).tools)

    started = time.perf_counter()
    count = anyio.run(go)
    elapsed = time.perf_counter() - started
    assert count == len(CONTRACT_TOOLS)
    assert elapsed < 1.0, f"build + handshake + list took {elapsed:.3f}s"


def test_a_call_the_sdk_rejects_before_the_tool_runs_is_audited_too(
    projects: dict[str, ProjectInfo],
) -> None:
    """A missing argument or an unknown tool never reaches _run: without this, the call —
    and the owner's words in it — left no captain_action. A call the TOOL refuses is still
    audited once, by the tool, never a second time by the rejection path."""

    async def go() -> tuple[Any, Any, Any, Any]:
        async with Client(actions.build_server(), mode="legacy") as client:
            missing = await client.call_tool("spawn", {"project": "alpha", "utterance": "a coder"})
            unknown = await client.call_tool("fly", {"utterance": "fly"})
            fine = await client.call_tool("projects", {"utterance": "list"})
            inside = await client.call_tool("stop", {"project": "alpha", "label": "coder-1"})
            return missing, unknown, fine, inside

    missing, unknown, fine, inside = anyio.run(go)
    home = audit(captain_state.home_project().id)
    assert [a["tool"] for a in home] == ["spawn", "fly", "projects"], "one event per call"
    assert [a["tool"] for a in audit(projects["alpha"].id)] == ["stop"], "audited once"
    assert inside.is_error and inside.content[0].text.startswith("refused: stopping coder-1")
    assert home[0]["utterance"] == "a coder" and home[0]["ok"] is False
    assert home[0]["said"].startswith("refused: the call was rejected before the tool ran")
    assert home[0]["args"] == {"project": "alpha"}
    assert missing.is_error and unknown.is_error and not fine.is_error
    assert missing.content[0].text.endswith(f"(action seq {_home_seqs()[0]})")


def _home_seqs() -> list[int]:
    with store_session() as store:
        return [
            e.seq
            for e in store.filtered_events(
                captain_state.home_project().id, kind="captain_action", since_seq=0, limit=50
            )
        ]


def test_the_server_tells_the_captain_every_tool_that_needs_confirm() -> None:
    assert "stop, spawn and restart need confirm=true" in actions.INSTRUCTIONS


def test_a_call_through_the_protocol_reaches_the_tool_and_a_refusal_is_an_error_result(
    projects: dict[str, ProjectInfo],
) -> None:
    async def go() -> tuple[Any, Any]:
        async with Client(actions.build_server(), mode="legacy") as client:
            good = await client.call_tool("projects", {"utterance": "which projects"})
            bad = await client.call_tool("board", {"project": "nowhere"})
            return good, bad

    good, bad = anyio.run(go)
    assert not good.is_error
    listed = json.loads(good.content[0].text)
    assert sorted(p["name"] for p in listed["projects"]) == ["alpha", "beta", "gamma"]
    assert bad.is_error
    assert bad.content[0].text.startswith("refused: no project matches 'nowhere'")


SERVE_ENTRIES = {
    "cli": ["-m", "aisquare", "captain", "serve", "--stdio"],
    "lean": ["-m", "aisquare.services.captain", "--stdio"],
}
"""Both ways to start the server: the CLI verb, and the module entry that skips the CLI tree."""


@pytest.mark.parametrize("entry", sorted(SERVE_ENTRIES))
def test_the_stdio_server_closes_itself_after_the_idle_deadline(
    entry: str, isolated_home: Path
) -> None:
    """``--close-after`` is the same idle deadline ``aisquare serve --stdio`` keeps (#19)."""
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, *SERVE_ENTRIES[entry], "--close-after", "1"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    assert proc.stdin is not None and proc.stderr is not None
    try:
        # stdin stays OPEN — an EOF would end the server the other way, on its own.
        proc.wait(timeout=30)
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        proc.stdin.close()
    # Bytes, as test_serve_lifecycle compares them: a piped stderr on Windows is
    # the ANSI code page, and the notice's dash is not UTF-8 there.
    err = proc.stderr.read()
    proc.stderr.close()
    assert proc.returncode == 0, err
    assert b"aisquare captain serve --stdio: no client messages for 1s" in err
    assert time.perf_counter() - started < 30


def test_the_lean_entry_speaks_stdio_only() -> None:
    from aisquare.services.captain import __main__ as lean

    with pytest.raises(SystemExit) as caught:
        lean.main(["--close-after", "5"])
    assert caught.value.code == 2


@dataclass
class Tick:
    """A monotonic clock the test moves by hand."""

    now: float = 50.0

    def __call__(self) -> float:
        return self.now


def test_the_idle_clock_stands_still_while_a_tool_call_runs() -> None:
    """13038 item 2: a call longer than --close-after is activity, not silence."""
    tick = Tick()
    idle = mcp_server.IdleClock(tick)
    tick.now += 10
    assert idle.idle_for() == 10
    idle.started()
    tick.now += 400  # ask_manager(timeout=400) under the default --close-after 300
    assert idle.idle_for() == 0, "a running call keeps the server awake"
    idle.finished()
    assert idle.idle_for() == 0, "the answer going out is activity too"
    tick.now += 7
    assert idle.idle_for() == 7
    idle.started()
    idle.started()
    idle.finished()
    tick.now += 400
    assert idle.idle_for() == 0, "one of two calls is still running"


def test_the_stdio_runner_counts_a_tool_call_in_flight_for_its_whole_run(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = actions.build_server()
    idle = mcp_server.IdleClock()
    mcp_server.track_tool_calls(server, idle)
    seen: list[int] = []
    real = actions._projects

    def observed() -> actions.Outcome:
        seen.append(idle.in_flight)
        return real()

    monkeypatch.setattr(actions, "_projects", observed)

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool("projects", {})

    result = anyio.run(go)
    assert not result.is_error
    assert seen == [1], "the tool ran inside the tracked call"
    assert idle.in_flight == 0, "and the call is over when the answer is out"


@UNIX_SOCKETS
def test_a_tool_call_longer_than_the_idle_deadline_still_returns(
    projects: dict[str, ProjectInfo],
) -> None:
    """The real process: a 1 s deadline, a ui call that takes 2 s (a receiver that never
    answers). Counting inbound lines only, the server exited mid-call at 1 s."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    path = captain_state.ui_socket_path(create=True)
    receiver = _unix_socket()
    receiver.bind(str(path))
    receiver.listen(1)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "aisquare", "captain", "serve", "--stdio", "--close-after", "1"],
        env=dict(os.environ),
    )

    async def go() -> tuple[bool, str]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("ui", {"action": "open_spawn"})
            block = result.content[0]
            return result.is_error, getattr(block, "text", "")

    try:
        is_error, text = anyio.run(go)
    finally:
        receiver.close()
        path.unlink(missing_ok=True)
    assert is_error
    assert text.startswith(f"error: asq did not answer within {actions.UI_TIMEOUT_S:g}s")
    # runner2's 13043 pin: the call the watchdog used to kill left NO audit row.
    uis = [a for a in audit(captain_state.home_project().id) if a["tool"] == "ui"]
    assert len(uis) == 1 and uis[0]["ok"] is False


@UNIX_SOCKETS
def test_a_client_that_hangs_up_mid_call_still_gets_the_call_audited(
    projects: dict[str, ProjectInfo],
) -> None:
    """13043/13044: the audit lands even when a call is cut short. The client sends a 2 s
    call and closes stdin at once: the server finishes the call, writes its one event,
    then exits on the EOF."""
    path = captain_state.ui_socket_path(create=True)
    receiver = _unix_socket()
    receiver.bind(str(path))
    receiver.listen(1)  # accepts at the kernel, never answers
    proc = subprocess.Popen(
        [sys.executable, "-m", "aisquare", "captain", "serve", "--stdio", "--close-after", "30"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ),
    )
    assert proc.stdin is not None and proc.stdout is not None
    hello = {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    }
    try:
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()
        assert b'"id":1' in proc.stdout.readline().replace(b" ", b"")
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ui", "arguments": {"action": "open_spawn"}},
        }
        proc.stdin.write((json.dumps(call) + "\n").encode())
        proc.stdin.flush()
        proc.stdin.close()  # the client is gone before the call can answer
        assert proc.wait(timeout=30) == 0
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        receiver.close()
        path.unlink(missing_ok=True)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    uis = [a for a in audit(captain_state.home_project().id) if a["tool"] == "ui"]
    assert len(uis) == 1, "exactly one audit for the call the client walked away from"
    assert uis[0]["said"].startswith("error: asq did not answer within")


def test_captain_serve_speaks_stdio_only(runner: CliRunner) -> None:
    result = runner.invoke(app, ["captain", "serve"])
    assert result.exit_code == 2
    # plain(): on GitHub Actions typer forces a styled terminal, and the highlighter puts
    # an escape code INSIDE "--stdio" (tests/rendered.py says why).
    assert "--stdio" in plain(result.output)


# --- one event per call -----------------------------------------------------------------


EVERY_CALL: list[tuple[str, dict[str, Any]]] = [
    ("projects", {}),
    ("board", {"project": "alpha"}),
    ("board", {"project": "nowhere"}),
    ("attention", {}),
    ("next", {}),
    ("resolve", {"item": "q1", "how": "told coder-1"}),
    ("snooze", {"item": "q1", "minutes": 10}),
    ("since", {"project": "alpha"}),
    ("read_pane", {"project": "alpha", "label": "coder-1"}),
    ("tell", {"project": "alpha", "label": "coder-1", "text": "hi"}),
    ("spawn", {"project": "alpha", "role": "coder"}),
    ("spawn", {"project": "alpha", "role": "coder", "confirm": True}),
    ("stop", {"project": "alpha", "label": "coder-1"}),
    ("stop", {"project": "alpha", "label": "coder-1", "confirm": True}),
    ("restart", {"project": "alpha", "label": "coder-1"}),
    ("restart", {"project": "alpha", "label": "coder-1", "confirm": True}),
    ("attach_persona", {"project": "alpha", "label": "coder-1", "name": "skeptic"}),
    ("task", {"project": "alpha", "verb": "add", "ref": "write the docs"}),
    ("task", {"project": "alpha", "verb": "explode", "ref": "x"}),
    ("note", {"project": "alpha", "text": "all good"}),
    ("press", {"project": "alpha", "label": "coder-1", "key": "y"}),
    ("press", {"project": "alpha", "label": "coder-1", "key": "F13"}),
    ("paste", {"project": "alpha", "label": "coder-1", "text": "a\nb"}),
    ("paste", {"project": "alpha", "label": "coder-1", "text": "a\nb", "submit": True}),
    ("ui", {"action": "open_spawn"}),
    ("act", {"name": "approve_prompt", "args": {"project": "alpha", "label": "coder-1"}}),
    ("act", {"name": "no_such_action"}),
    ("speak", {"text": "on it"}),
    ("thinking", {"state": "on"}),
    ("bt", {}),
    ("wololo", {"project": "alpha", "label": "coder-3", "task": "tsk_nothing"}),
]


def test_the_every_call_table_covers_the_whole_vocabulary() -> None:
    assert {name for name, _ in EVERY_CALL} | {"ask_manager"} == CONTRACT_TOOLS


@pytest.mark.parametrize(("tool", "kwargs"), EVERY_CALL, ids=lambda v: str(v)[:40])
def test_every_call_writes_exactly_one_captain_action_success_or_refusal(
    tool: str,
    kwargs: dict[str, Any],
    projects: dict[str, ProjectInfo],
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
) -> None:
    before = audit_count(projects)
    function = dict(actions.TOOLS)[tool]
    with contextlib.suppress(ToolError):
        function(**kwargs, utterance=f"owner asked for {tool}")
    assert audit_count(projects) == before + 1


def test_ask_manager_writes_one_captain_action_too(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    before = len(audit(alpha.id))
    with contextlib.suppress(ToolError):
        actions.ask_manager("alpha", "status?", timeout=2)
    assert len(audit(alpha.id)) == before + 1


def test_the_audit_event_names_the_tool_args_utterance_outcome_and_receipt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    result = ok(actions.note("alpha", "ship it", kind="decision", utterance="tell them to ship"))
    event = audit(alpha.id)[-1]
    assert event["v"] == 1
    assert event["tool"] == "note"
    assert event["project"] == alpha.id
    assert event["args"] == {"project": "alpha", "text": "ship it", "kind": "decision"}
    assert event["utterance"] == "tell them to ship"
    assert event["ok"] is True
    assert event["receipt"] == result["seq"], "the note's own seq is the receipt"
    assert result["action_seq"] > result["seq"], "the audit follows the effect"


def test_a_refusal_is_audited_with_its_reason_and_names_its_audit_seq(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    message = refused(lambda: actions.stop("alpha", "coder-1", utterance="kill coder one"))
    event = audit(alpha.id)[-1]
    assert event["ok"] is False
    assert event["said"].startswith("refused: stopping coder-1 ends its session")
    assert message.startswith(event["said"])
    assert "action seq" in message


def test_a_call_that_names_no_project_is_audited_on_the_home_board(
    projects: dict[str, ProjectInfo],
) -> None:
    ok(actions.speak("item one"))
    home = captain_state.home_project()
    assert [event["tool"] for event in audit(home.id)] == ["speak"]
    assert all(audit(p.id) == [] for p in projects.values())


def test_an_unknown_project_is_refused_and_audited_on_the_home_board(
    projects: dict[str, ProjectInfo],
) -> None:
    message = refused(lambda: actions.board("nowhere"))
    assert message.startswith("refused: no project matches 'nowhere'")
    assert audit(captain_state.home_project().id)[-1]["tool"] == "board"


def test_the_audit_never_reaches_a_teammates_delta(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """Audit lines are for the owner's board — the captain reads panes often, and each
    read in every coder's prompt would take a delta slot from real news."""
    assert "captain_action" in team_service.HUMAN_BOARD_KINDS
    actions.read_pane("alpha", "coder-1")
    add_events(alpha, "sess-coder-2", 1)
    delta = team_service.hook_prompt_heartbeat("sess-coder-1", alpha.root)
    assert "event 0" in delta, "the real news still arrives"
    assert "captain_action" not in delta and "read_pane" not in delta


def test_the_captains_session_keeps_its_start_across_calls(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    ok(actions.note("alpha", "first"))
    with store_session() as store:
        first = store.get_session(captain_state.session_id_for(alpha.id))
    time.sleep(0.01)
    ok(actions.note("alpha", "second"))
    with store_session() as store:
        later = store.get_session(captain_state.session_id_for(alpha.id))
    assert first is not None and later is not None
    assert later.started_at == first.started_at
    assert later.last_seen_at > first.last_seen_at


def test_writes_route_by_the_captains_session_never_by_the_hub(
    projects: dict[str, ProjectInfo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub pins every cwd-resolved write to one board; the captain names its board."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    result = ok(actions.note("beta", "for beta only"))
    with store_session() as store:
        event = store.get_event_by_seq(result["seq"])
        hub_board = store.recent_events(project_id_for(hub.resolve()), limit=50)
    assert event is not None and event.text == "for beta only"
    assert event.project_id == projects["beta"].id
    assert event.session_id == captain_state.session_id_for(projects["beta"].id)
    assert hub_board == []


# --- read tools -------------------------------------------------------------------------


def test_projects_lists_every_onboarded_project_with_its_codename(
    projects: dict[str, ProjectInfo],
) -> None:
    listed = ok(actions.projects())["projects"]
    assert [(p["name"], p["codename"]) for p in listed] == [
        ("alpha", "amber-otter"),
        ("beta", "blue-heron"),
        ("gamma", "green-finch"),
    ]
    assert {p["id"] for p in listed} == {p.id for p in projects.values()}
    assert captain_state.home_project().id not in {p["id"] for p in listed}


def test_board_shows_agents_with_their_state_open_tasks_and_recent_events(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.states["coder-1"] = "working"
    add_task(alpha, "open card")
    done = add_task(alpha, "finished card")
    with store_session() as store:
        store.set_task_status(done.id, "done")
    add_events(alpha, "sess-coder-1", 20)
    shown = ok(actions.board("amber-otter"))
    assert shown["project"]["id"] == alpha.id
    by_label = {a["label"]: a for a in shown["agents"]}
    assert by_label["coder-1"]["state"] == "working"
    assert by_label["manager"]["role"] == "manager"
    assert [t["title"] for t in shown["tasks"]] == ["open card"]
    assert len(shown["events"]) == 15
    assert shown["events"][-1]["text"] == "event 19"


def test_read_pane_returns_the_tail_without_escapes_and_marks_it_untrusted(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    pane = fleet_rec.panes["%1"]
    pane.history = [f"old {n}" for n in range(10)]
    pane.screen = ["\x1b[31mred line\x1b[0m", "last line", "", ""]
    shown = ok(actions.read_pane("alpha", "coder-1", lines=5))
    assert shown["lines"] == ["old 7", "old 8", "old 9", "red line", "last line"]
    assert shown["untrusted"] is True
    assert shown["state"] == "waiting"
    assert ok(actions.read_pane("alpha", "coder-1", lines=500))["lines"][0] == "old 0"


def test_read_pane_strips_a_hyperlink_ended_by_st(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """coderp's S1 on #219: T1's pattern stripped a hyperlink ended by ST (ESC and one
    backslash). The shared one must too, or read_pane hands the captain link targets."""
    pane = fleet_rec.panes["%1"]
    pane.screen = ["see \x1b]8;;https://example.com/pr/219\x1b\\the PR\x1b]8;;\x1b\\ now", ""]
    assert ok(actions.read_pane("alpha", "coder-1", lines=5))["lines"][-1] == "see the PR now"


def test_read_pane_refuses_an_agent_that_is_not_there(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    message = refused(lambda: actions.read_pane("alpha", "coder-9"))
    assert message.startswith("refused: no live agent coder-9 in alpha")


# --- since and the watermark ------------------------------------------------------------


def test_since_without_a_watermark_shows_the_latest_fifty(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    add_events(alpha, "sess-coder-1", 60)
    shown = ok(actions.since("alpha"))
    assert len(shown["events"]) == 50
    assert shown["events"][-1]["text"] == "event 59"
    assert shown["from_seq"] is None
    assert shown["advanced"] is False
    assert captain_state.watermark(alpha.id, None) is None


def test_since_first_look_shows_fifty_real_events_on_a_board_the_captain_reads_a_lot(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    real = add_events(alpha, "sess-coder-1", 60)
    add_events(alpha, captain_state.session_id_for(alpha.id), 250, kind="captain_action")
    shown = ok(actions.since("alpha"))
    assert [e["seq"] for e in shown["events"]] == [e.seq for e in real[-50:]]


def test_since_moves_its_watermark_only_once_its_audit_has_landed(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moved first and the audit then failing, the events were never delivered and the
    next since started after them."""
    first = add_events(alpha, "sess-coder-1", 2)
    captain_state.set_watermark(alpha.id, None, first[0].seq)
    real = actions._audit

    def locked(tool: str, *args: Any, **kwargs: Any) -> int:
        if tool == "since" and kwargs.get("ok"):
            raise sqlite3.OperationalError("database is locked")
        return real(tool, *args, **kwargs)

    monkeypatch.setattr(actions, "_audit", locked)
    assert refused(lambda: actions.since("alpha", advance=True)).startswith("error: since was done")
    assert captain_state.watermark(alpha.id, None) == first[0].seq, "not moved"
    monkeypatch.setattr(actions, "_audit", real)
    shown = ok(actions.since("alpha", advance=True))
    assert shown["advanced"] is True
    assert captain_state.watermark(alpha.id, None) == first[1].seq


def test_a_watermark_that_cannot_be_moved_is_said_in_the_answer(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.core.state_file import StateUnwritableError

    add_events(alpha, "sess-coder-1", 1)

    def stuck(project_id: str, agent: str | None, seq: int) -> None:
        raise StateUnwritableError("state.json.lock is held by another process")

    monkeypatch.setattr(captain_state, "set_watermark", stuck)
    shown = ok(actions.since("alpha", advance=True))
    assert shown["advanced"] is False
    assert shown["after_error"] == "error: state.json.lock is held by another process"
    assert len(shown["events"]) == 1, "the events still arrive"


def test_since_advance_moves_the_watermark_and_the_next_read_starts_there(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    first = add_events(alpha, "sess-coder-1", 3)
    captain_state.set_watermark(alpha.id, None, first[0].seq)
    shown = ok(actions.since("alpha", advance=True))
    assert [e["text"] for e in shown["events"]] == ["event 1", "event 2"]
    assert shown["from_seq"] == first[0].seq
    assert shown["to_seq"] == first[2].seq
    assert shown["advanced"] is True
    assert captain_state.watermark(alpha.id, None) == first[2].seq
    later = add_events(alpha, "sess-coder-2", 1)
    again = ok(actions.since("alpha"))
    assert [e["seq"] for e in again["events"]] == [later[0].seq]


def test_since_leaves_the_captains_own_audit_out(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    captain_state.set_watermark(alpha.id, None, 0)
    ok(actions.read_pane("alpha", "coder-1"))
    add_events(alpha, "sess-coder-1", 1)
    shown = ok(actions.since("alpha"))
    assert [e["kind"] for e in shown["events"]] == ["note"]


def test_since_for_an_agent_is_its_own_events_plus_its_pane_tail(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    captain_state.set_watermark(alpha.id, "coder-1", 0)
    add_events(alpha, "sess-coder-2", 2)
    mine = add_events(alpha, "sess-coder-1", 2)
    fleet_rec.panes["%1"].screen = ["tests pass", "PR opened"]
    shown = ok(actions.since("alpha", agent="coder-1", advance=True))
    assert [e["seq"] for e in shown["events"]] == [m.seq for m in mine]
    assert shown["pane"] == ["tests pass", "PR opened"]
    assert captain_state.watermark(alpha.id, "coder-1") == shown["to_seq"]
    assert captain_state.watermark(alpha.id, None) is None, "per agent, never the board's"


def test_since_still_answers_when_the_agents_pane_is_gone(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def gone(self: FakeServer, pane_id: str, **kwargs: Any) -> Capture:
        raise TmuxError(f"can't find pane: {pane_id}")

    monkeypatch.setattr(FakeServer, "capture", gone)
    captain_state.set_watermark(alpha.id, "coder-1", 0)
    mine = add_events(alpha, "sess-coder-1", 1)
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.actions"):
        shown = ok(actions.since("alpha", agent="coder-1"))
    assert [e["seq"] for e in shown["events"]] == [mine[0].seq]
    assert shown["pane"] is None
    assert shown["pane_error"] == "can't find pane: %1"
    assert "coder-1" in caplog.text and "can't find pane: %1" in caplog.text
    assert audit(alpha.id)[-1]["said"].endswith("(its pane could not be read)")


def test_since_pages_at_two_hundred_and_says_it_was_cut(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    made = add_events(alpha, "sess-coder-1", 205)
    captain_state.set_watermark(alpha.id, None, 0)
    shown = ok(actions.since("alpha", advance=True))
    assert len(shown["events"]) == 200
    assert shown["truncated"] is True
    assert captain_state.watermark(alpha.id, None) == made[199].seq
    rest = ok(actions.since("alpha"))
    assert [e["seq"] for e in rest["events"]] == [m.seq for m in made[200:]]
    assert rest["truncated"] is False


# --- effects through the fleet ----------------------------------------------------------


def test_tell_goes_through_fleet_tell_as_the_captain(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    result = ok(actions.tell("alpha", "coder-1", "rebase on the rc"))
    assert fleet_rec.calls == [
        (
            "tell",
            {
                "project": alpha.id,
                "label": "coder-1",
                "text": "rebase on the rc",
                "sender": captain_state.session_id_for(alpha.id),
            },
        )
    ]
    assert result["delivered"] is True
    assert result["how"] == "typed into its pane (it was waiting)"


def test_spawn_refuses_without_confirm_and_never_reaches_the_fleet(
    alpha: ProjectInfo, fleet_rec: Fleet
) -> None:
    """Starting a session spends quota: plan section 3's confirm step (13013)."""
    message = refused(lambda: actions.spawn("alpha", "coder", label="coder-7"))
    assert message.startswith("refused: spawning a coder in alpha starts a session")
    assert "confirm=true" in message
    assert fleet_rec.calls == []


def test_spawn_goes_through_fleet_spawn_marked_as_the_captains(
    alpha: ProjectInfo, fleet_rec: Fleet
) -> None:
    result = ok(
        actions.spawn(
            "alpha", "coder", label="coder-7", task="tsk_1", persona="skeptic", confirm=True
        )
    )
    assert fleet_rec.calls == [
        (
            "spawn",
            {
                "project": alpha.id,
                "role": "coder",
                "label": "coder-7",
                "task_id": "tsk_1",
                "persona": "skeptic",
                "spawned_by": "captain",
            },
        )
    ]
    assert (result["label"], result["role"], result["tmux_session"]) == (
        "coder-7",
        "coder",
        "asq-x",
    )
    assert result["notes"] == ["noted"]


def test_stop_refuses_without_confirm_and_never_reaches_the_fleet(
    alpha: ProjectInfo, fleet_rec: Fleet
) -> None:
    message = refused(lambda: actions.stop("alpha", "coder-1"))
    assert message.startswith("refused: stopping coder-1 ends its session")
    assert "confirm=true" in message
    assert fleet_rec.calls == []
    result = ok(actions.stop("alpha", "coder-1", force=True, confirm=True))
    assert fleet_rec.calls == [("stop", {"project": alpha.id, "label": "coder-1", "force": True})]
    assert result["label"] == "coder-1"
    assert result["released"] == []


def test_restart_refuses_without_confirm_and_never_reaches_the_fleet(
    alpha: ProjectInfo, fleet_rec: Fleet
) -> None:
    message = refused(lambda: actions.restart("alpha", "coder-1"))
    assert message.startswith("refused: restarting coder-1 starts a session")
    assert "confirm=true" in message
    assert fleet_rec.calls == []


def test_restart_goes_through_fleet_restart(alpha: ProjectInfo, fleet_rec: Fleet) -> None:
    result = ok(actions.restart("alpha", "coder-1", confirm=True))
    assert fleet_rec.calls == [
        ("restart", {"project": alpha.id, "label": "coder-1", "spawned_by": "captain"})
    ]
    assert result["resumed"] is True


def test_attach_persona_goes_through_the_fleet_as_the_captain(
    alpha: ProjectInfo, fleet_rec: Fleet
) -> None:
    result = ok(actions.attach_persona("alpha", "coder-1", "skeptic"))
    assert fleet_rec.calls == [
        (
            "attach_persona",
            {
                "project": alpha.id,
                "label": "coder-1",
                "name": "skeptic",
                "sender": captain_state.session_id_for(alpha.id),
            },
        )
    ]
    assert (result["persona"], result["delivered"]) == ("skeptic", "typed")


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("stop", {"project": "alpha", "label": "coder-1", "confirm": True}),
        ("restart", {"project": "alpha", "label": "coder-1", "confirm": True}),
        ("attach_persona", {"project": "alpha", "label": "coder-1", "name": "skeptic"}),
    ],
    ids=["stop", "restart", "attach_persona"],
)
def test_the_receipt_is_the_effects_own_event_even_on_a_busy_board(
    tool: str,
    kwargs: dict[str, Any],
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
) -> None:
    """13010: the effect's seq is the receipt — for every effect, not only a Delivery's.
    Another agent's event of the same kind lands first, and must not be taken for it."""
    fleet_rec.crowd = True
    ok(dict(actions.TOOLS)[tool](**kwargs))
    (effect,) = fleet_rec.effects
    assert audit(alpha.id)[-1]["receipt"] == effect.seq


def test_a_receipt_that_cannot_be_read_never_turns_a_done_stop_into_an_error(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop happened: a store too busy to read its receipt back afterwards must not
    make it an error the owner answers with a second stop (or a second restart)."""
    from aisquare.core.store import SqliteStore

    real = SqliteStore.filtered_events

    def busy(self: SqliteStore, project_id: str, **kwargs: Any) -> list[TeamEvent]:
        if kwargs.get("limit") == 500:  # the receipt lookup, and only it
            raise sqlite3.OperationalError("database is locked")
        return real(self, project_id, **kwargs)

    monkeypatch.setattr(SqliteStore, "filtered_events", busy)
    result = ok(actions.stop("alpha", "coder-1", confirm=True))
    assert result["label"] == "coder-1"
    event = audit(alpha.id)[-1]
    assert (event["ok"], event["receipt"]) == (True, None)
    assert event["said"] == "stopped coder-1 (receipt unknown: database is locked)"


def test_a_restart_whose_replacement_re_picked_its_label_still_gets_its_receipt(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """No board session to match, and the replacement now reads coder-4: the fleet writes
    the label it was ASKED to restart, so that is what the receipt matches."""
    fleet_rec.crowd = True
    fleet_rec.relabel = "coder-4"
    ok(actions.restart("alpha", "coder-3", confirm=True))
    (effect,) = fleet_rec.effects
    assert effect.text.startswith("coder-3 restarted")
    assert audit(alpha.id)[-1]["receipt"] == effect.seq


def test_a_fleet_refusal_is_said_never_faked(
    alpha: ProjectInfo, fleet_rec: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_room(project: ProjectInfo, role: str, **kwargs: Any) -> fleet.SpawnReceipt:
        raise fleet.FleetError("alpha already runs 6 agents (max_agents_per_project = 6)")

    monkeypatch.setattr(fleet, "spawn", no_room)
    message = refused(lambda: actions.spawn("alpha", "coder", confirm=True))
    assert message.startswith("refused: alpha already runs 6 agents")
    assert audit(alpha.id)[-1]["ok"] is False


# --- ask_manager ------------------------------------------------------------------------


def test_ask_manager_returns_the_note_the_manager_addresses_back(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    def manager_answers(project: ProjectInfo, label: str, text: str) -> None:
        team_service.add_note(
            "coder-1 is on the rebase", session_ref="sess-manager", to_role="captain"
        )

    fleet_rec.on_tell = manager_answers
    result = ok(actions.ask_manager("alpha", "who is on the rebase?", timeout=30))
    assert result["reply"]["text"] == "coder-1 is on the rebase"
    assert result["reply"]["by"] == "sess-manager"
    ((name, call),) = fleet_rec.calls
    assert (name, call["label"]) == ("tell", "manager")
    assert call["text"].startswith("who is on the rebase?")
    assert 'answer with: aisquare note "..." --to captain' in call["text"]


def test_ask_manager_ignores_a_note_to_the_captain_from_before_the_question(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    team_service.add_note("an old answer", session_ref="sess-manager", to_role="captain")
    message = refused(lambda: actions.ask_manager("alpha", "anything new?", timeout=3))
    assert message.startswith("refused: the manager of alpha did not answer in 3s")
    assert sum(clock.slept) == pytest.approx(3.0)


def test_ask_manager_caps_the_wait(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    message = refused(lambda: actions.ask_manager("alpha", "hello?", timeout=10_000))
    assert f"did not answer in {actions.ASK_TIMEOUT_MAX}s" in message


def test_the_brake_cancels_a_waiting_ask_manager(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    def owner_pulls_the_brake(project: ProjectInfo, label: str, text: str) -> None:
        captain_state.pull_brake()

    fleet_rec.on_tell = owner_pulls_the_brake
    message = refused(lambda: actions.ask_manager("alpha", "hello?", timeout=60))
    assert message.startswith("refused: the brake (bt) cancelled the wait")
    assert sum(clock.slept) < 60


# --- task and note ----------------------------------------------------------------------


def test_task_verbs_move_the_card_as_the_captain(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    captain = captain_state.session_id_for(alpha.id)
    added = ok(actions.task("alpha", "add", "wire the socket", note="T4 needs it"))
    card = task_now(added["id"])
    assert (card.title, card.detail, card.project_id) == (
        "wire the socket",
        "T4 needs it",
        alpha.id,
    )
    assert ok(actions.task("alpha", "claim", card.id))["status"] == "doing"
    assert task_now(card.id).claimed_by == captain
    assert ok(actions.task("alpha", "release", card.id))["status"] == "todo"
    assert ok(actions.task("alpha", "block", card.id, note="waits on T1"))["status"] == "blocked"
    assert ok(actions.task("alpha", "reopen", card.id, note="T1 landed"))["status"] == "todo"
    assert ok(actions.task("alpha", "done", card.id))["status"] == "done"


def test_task_refuses_a_bad_verb_a_missing_reason_and_a_foreign_card(
    projects: dict[str, ProjectInfo], agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    card = add_task(projects["alpha"], "a card")
    foreign = add_task(projects["beta"], "beta's card")
    assert refused(lambda: actions.task("alpha", "explode", card.id)).startswith(
        "refused: task verb must be one of add, claim, done, reopen, release, block"
    )
    assert refused(lambda: actions.task("alpha", "block", card.id)).startswith(
        "refused: block needs a note (the reason)"
    )
    assert refused(lambda: actions.task("alpha", "reopen", card.id)).startswith(
        "refused: reopen needs a note (the feedback)"
    )
    assert refused(lambda: actions.task("alpha", "claim", foreign.id)).startswith(
        f"refused: {foreign.id} is on beta's board, not alpha's"
    )
    assert task_now(foreign.id).status == "todo"


def test_note_lands_on_the_named_board_and_refuses_an_unknown_kind(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    result = ok(actions.note("alpha", "owner says ship", kind="decision"))
    with store_session() as store:
        event = next(e for e in store.recent_events(alpha.id, limit=5) if e.seq == result["seq"])
    assert (event.kind, event.text) == ("decision", "owner says ship")
    assert refused(lambda: actions.note("alpha", "x", kind="shout")).startswith(
        "refused: note kind must be one of note, decision, question, result"
    )


# --- press and paste --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "sent"),
    [
        ("y", "y"),
        ("n", "n"),
        ("enter", "Enter"),
        ("esc", "Escape"),
        ("up", "Up"),
        ("down", "Down"),
        ("left", "Left"),
        ("right", "Right"),
        ("tab", "Tab"),
        ("space", "Space"),
        ("ctrl-c", "C-c"),
    ],
)
def test_press_sends_exactly_the_named_key(
    key: str, sent: str, alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.states["coder-1"] = "attention"
    result = ok(actions.press("alpha", "coder-1", key))
    assert fleet_rec.panes["%1"].keys == [(sent,)]
    assert (result["key"], result["state"]) == (key, "attention")


def test_press_refuses_a_busy_pane_and_types_nothing(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.states["coder-1"] = "working"
    message = refused(lambda: actions.press("alpha", "coder-1", "y"))
    assert message.startswith("refused: coder-1 is working")
    assert fleet_rec.panes["%1"].keys == []


@pytest.mark.parametrize("state", ["limited", "exited", "lost", "unknown"])
def test_press_refuses_every_state_but_waiting_and_attention(
    state: FleetAgentState, alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.states["coder-1"] = state
    assert refused(lambda: actions.press("alpha", "coder-1", "y")).startswith(
        f"refused: coder-1 is {state}"
    )
    assert fleet_rec.panes["%1"].keys == []


def test_press_refuses_a_pane_whose_foreground_is_not_the_agent(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.panes["%1"].command = "bash"
    message = refused(lambda: actions.press("alpha", "coder-1", "y"))
    assert message.startswith("refused: coder-1's pane is not running the agent")
    assert fleet_rec.panes["%1"].keys == []


def test_press_refuses_a_key_outside_the_list_by_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    message = refused(lambda: actions.press("alpha", "coder-1", "F13"))
    assert message.startswith("refused: key 'F13' is not one of yes, no, y, n, enter, esc")
    assert fleet_rec.panes["%1"].keys == []


# --- press answers Claude Code's real prompts (T1b) --------------------------------------
#
# The screens are runner2-1's captures from a REAL Claude Code 2.1.282 (board 13265): on
# its permission chooser and on the trust dialog the letter y does nothing; the digit 1
# (Yes), Enter with Yes highlighted and the arrows do.

MARK = shots.MARK
RULE = shots.REAL_RULE
IDLE = shots.REAL_IDLE
CHOOSER = shots.REAL_CHOOSER
TRUST = shots.REAL_TRUST
YES_NO = shots.YES_NO
QUOTED = shots.REAL_QUOTED


def _at(fleet_rec: Fleet, screen: list[str], **answers: list[str]) -> Pane:
    pane = fleet_rec.panes["%1"]
    pane.screen = list(screen)
    pane.answers = dict(answers)
    fleet_rec.states["coder-1"] = "attention"
    return pane


def test_the_permission_chooser_is_read_as_one_with_its_yes_digit() -> None:
    prompt = screen_reader.prompt_showing(CHOOSER)
    assert prompt is not None
    assert (prompt.shape, prompt.yes_key, prompt.no_key) == ("chooser", "1", "Escape")
    assert prompt.question == "Do you want to create probe2.txt?"


def test_a_chooser_whose_first_option_is_no_answers_yes_with_its_yes_digit() -> None:
    screen = [" Allow this?", f" {MARK} 1. No", "   2. Yes", " Esc to cancel"]
    prompt = screen_reader.prompt_showing(screen)
    assert prompt is not None and prompt.yes_key == "2", "never a blind 1"


def test_a_y_n_line_is_read_as_one() -> None:
    prompt = screen_reader.prompt_showing(YES_NO)
    assert prompt is not None
    assert (prompt.shape, prompt.yes_key, prompt.no_key) == ("yn", "y", "n")


def test_the_trust_dialog_is_read_as_its_own_shape() -> None:
    prompt = screen_reader.prompt_showing(TRUST)
    assert prompt is not None and prompt.shape == "trust"
    assert prompt.yes_key is None and prompt.no_key is None


@pytest.mark.parametrize("screen", [IDLE, QUOTED, ["$ ", "ready"], []])
def test_an_input_box_or_a_plain_screen_is_no_prompt(screen: list[str]) -> None:
    """A reply that quotes a whole chooser sits ABOVE the input box: it is no prompt (13264)."""
    assert screen_reader.prompt_showing(screen) is None


def test_an_input_box_at_the_bottom_is_no_prompt_whatever_else_shows() -> None:
    """13264's rule on its own: the box means the agent is at its input. Here the footer
    under the box ALSO mentions Esc, and a chooser is quoted above — still no prompt."""
    screen = [*QUOTED[:5], RULE, f"{MARK} ", RULE, "  ⏸ manual mode on · Esc to cancel a draft"]
    assert screen_reader.prompt_showing(screen) is None


def test_a_numbered_list_mid_turn_is_no_prompt_without_a_dialog_footer() -> None:
    """The chooser's footer is what makes numbered lines a dialog: mid-turn output that
    happens to highlight a line, with no Esc/Enter footer, is not one."""
    screen = [
        "Here are the options:",
        f" {MARK} 1. Yes",
        "   2. No",
        "✻ Thinking… (esc to interrupt)",
    ]
    assert screen_reader.prompt_showing(screen) is None


# --- ready by the screen while the hook still says working (13313) -----------------------------


def _working(fleet_rec: Fleet, lines: list[str], **answers: list[str]) -> Pane:
    """A pane the fleet reads WORKING: a fresh real claude before its first Stop hook, or
    the 5 s activity window after a chooser draws (runner2's 13308 and 13323)."""
    pane = fleet_rec.panes["%1"]
    pane.screen = list(lines)
    pane.answers = dict(answers)
    fleet_rec.states["coder-1"] = "working"
    return pane


def test_a_fresh_coder_takes_a_paste_when_its_box_is_drawn_and_idle(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """Acceptance line 2 (13313; runner2's red-before at 13323): a just-spawned real claude
    reads working until its first Stop hook. Its drawn, idle box is the evidence."""
    pane = _working(fleet_rec, shots.REAL_IDLE_AFTER_STOP)
    result = ok(actions.paste("alpha", "coder-1", "run the fold's tests", submit=True))
    assert result["submitted"] is True
    assert pane.typed == [("paste", "run the fold's tests"), ("keys", "Enter")]


def test_the_real_idle_pane_is_ready_its_finished_turns_line_is_no_spinner(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    pane = _working(fleet_rec, IDLE)
    ok(actions.paste("alpha", "coder-1", "next"))
    assert pane.pastes == ["next"]


@pytest.mark.parametrize("state", ["working", "waiting", "attention"])
def test_a_paste_never_types_into_the_trust_dialog_and_names_the_step(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, state: FleetAgentState
) -> None:
    """A coder spawned into a folder Claude Code never trusted parks at its OWN trust dialog
    (13498): a paste's Enter there answers it and the coder exits. press refuses it by name
    (13308); paste must too, whatever the fleet reads (13500)."""
    pane = _working(fleet_rec, TRUST)
    fleet_rec.states["coder-1"] = state
    message = refused(
        lambda: actions.paste("alpha", "coder-1", "run the fold's tests", submit=True)
    )
    assert "trust" in message and "coder-1" in message, message
    assert pane.typed == []


def test_a_box_drawn_mid_turn_refuses_the_paste(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """Real Claude Code keeps its box during a turn: a live spinner above it and 'esc to
    interrupt' in its footer are what say the agent is working."""
    pane = _working(fleet_rec, shots.REAL_WORKING)
    message = refused(lambda: actions.paste("alpha", "coder-1", "run the fold's tests"))
    assert "coder-1 is working" in message
    assert pane.typed == []


def test_a_chooser_just_drawn_takes_press_yes_while_the_fleet_still_reads_working(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """A prompt showing is the agent asking, whatever the activity window says (13313)."""
    pane = _working(fleet_rec, CHOOSER, **{"1": IDLE})
    result = ok(actions.press("alpha", "coder-1", "yes"))
    assert pane.keys == [("1",)] and result["answered"] is True


@pytest.mark.parametrize("state", ["limited", "exited", "lost"])
def test_only_working_is_read_past_an_idle_screen(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, state: str
) -> None:
    """The screen overrides the activity window, never a stop: an agent parked on its usage
    limit, or gone, is refused whatever its last screen shows."""
    pane = _working(fleet_rec, shots.REAL_IDLE_AFTER_STOP)
    fleet_rec.states["coder-1"] = state  # type: ignore[assignment]
    message = refused(lambda: actions.paste("alpha", "coder-1", "next"))
    assert f"coder-1 is {state}" in message or "no live agent" in message
    assert pane.typed == []


def test_a_working_pane_that_cannot_be_read_stays_refused_as_working(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _working(fleet_rec, shots.REAL_IDLE_AFTER_STOP)

    def unreadable(*args: object, **kwargs: object) -> Capture:
        raise TmuxError("tmux capture-pane failed: no such pane")

    monkeypatch.setattr(FakeServer, "capture", unreadable)
    message = refused(lambda: actions.paste("alpha", "coder-1", "next"))
    assert "coder-1 is working" in message and pane.typed == []


def test_the_captains_persona_says_yes_through_approve_prompt_and_its_result() -> None:
    """13273: the persona line rides with whichever of T2 (the persona's owner) and T1b lands
    second. T2 landed first (e49c5464), so it is T1b's."""
    from aisquare.core import personas

    persona = personas.resolve("captain", paths.aisquare_home())
    body = "\n".join(personas.briefing(persona))
    assert "approve_prompt" in body and "its result" in body


def test_press_yes_answers_the_real_chooser_with_1_and_reads_it_gone(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, CHOOSER, **{"1": IDLE})
    result = ok(actions.press("alpha", "coder-1", "yes"))
    assert pane.keys == [("1",)], "the digit, never the ignored y"
    assert (result["key"], result["sent"], result["answered"]) == ("yes", "1", True)
    assert result["prompt"] == "Do you want to create probe2.txt?"
    assert audit(alpha.id)[-1]["ok"] is True


def test_press_no_on_the_chooser_is_esc(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, CHOOSER, Escape=IDLE)
    result = ok(actions.press("alpha", "coder-1", "no"))
    assert pane.keys == [("Escape",)] and result["answered"] is True


def test_press_yes_on_a_y_n_line_is_y(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, YES_NO, y=["Installing 3 packages.", "Proceed? [y/N] y", "done"])
    result = ok(actions.press("alpha", "coder-1", "yes"))
    assert pane.keys == [("y",)] and result["answered"] is True


def test_a_press_the_prompt_ignores_is_a_said_failure_never_a_success(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """y on the real chooser does nothing (13265): the captain must not report it pressed."""
    pane = _at(fleet_rec, CHOOSER)
    message = refused(lambda: actions.press("alpha", "coder-1", "y"))
    assert message.startswith("error: pressed y in coder-1 but the prompt is still showing")
    assert "Do you want to create probe2.txt?" in message
    assert pane.keys == [("y",)]
    assert audit(alpha.id)[-1]["ok"] is False


def test_yes_with_no_prompt_showing_is_refused_and_nothing_is_sent(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, QUOTED)
    message = refused(lambda: actions.press("alpha", "coder-1", "yes"))
    assert message.startswith("refused: no prompt is showing on coder-1")
    assert pane.keys == []


def test_yes_on_the_trust_dialog_is_refused_as_the_owners_to_answer(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, TRUST)
    message = refused(lambda: actions.press("alpha", "coder-1", "yes"))
    assert message.startswith("refused: the trust dialog is showing on coder-1")
    assert "owner" in message and pane.keys == []


def test_a_navigation_key_on_a_chooser_is_not_a_failure(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, CHOOSER)
    result = ok(actions.press("alpha", "coder-1", "down"))
    assert pane.keys == [("Down",)] and result["answered"] is False
    assert result["prompt"] == "Do you want to create probe2.txt?"


@pytest.mark.parametrize("digit", ["1", "2", "3", "9"])
def test_press_sends_a_digit(
    digit: str, alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, CHOOSER, **{digit: IDLE})
    ok(actions.press("alpha", "coder-1", digit))
    assert pane.keys == [(digit,)]


def test_approve_prompt_answers_the_real_chooser_with_1(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    pane = _at(fleet_rec, CHOOSER, **{"1": IDLE})
    result = ok(actions.act("approve_prompt", {"project": "alpha", "label": "coder-1"}))
    assert pane.keys == [("1",)]
    assert result["steps"][0]["step"] == "press yes"
    assert actions.BUNDLED_ACTIONS["unblock"] == ("press yes", "read_pane 20")


def test_paste_is_one_bracketed_paste_and_never_an_enter(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    result = ok(actions.paste("alpha", "coder-1", "line one\nline two"))
    pane = fleet_rec.panes["%1"]
    assert pane.pastes == ["line one\nline two"]
    assert pane.keys == []
    assert pane.typed == [("paste", "line one\nline two")]
    assert result["chars"] == len("line one\nline two")


def test_paste_submit_sends_exactly_one_enter_after_the_paste(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    """One Enter for the whole paste — never one per newline (13013, acceptance line 2)."""
    result = ok(actions.paste("alpha", "coder-1", "line one\nline two\n", submit=True))
    assert fleet_rec.panes["%1"].typed == [("paste", "line one\nline two\n"), ("keys", "Enter")]
    assert result["submitted"] is True
    plain = ok(actions.paste("alpha", "coder-1", "again"))
    assert plain["submitted"] is False
    assert fleet_rec.panes["%1"].typed[-1] == ("paste", "again")


def test_an_enter_that_fails_after_the_paste_says_the_text_is_waiting_in_the_input(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_enter(self: FakeServer, pane_id: str, *keys: str) -> None:
        raise TmuxError("can't find pane: %1")

    monkeypatch.setattr(FakeServer, "send_keys", no_enter)
    message = refused(lambda: actions.paste("alpha", "coder-1", "abc", submit=True))
    assert message.startswith("error: pasted 3 characters into coder-1 but the Enter failed")
    assert "do not paste it again" in message
    assert fleet_rec.panes["%1"].pastes == ["abc"], "the paste itself landed"


def test_paste_refuses_a_busy_pane(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.states["coder-1"] = "working"
    assert refused(lambda: actions.paste("alpha", "coder-1", "x")).startswith(
        "refused: coder-1 is working"
    )
    assert fleet_rec.panes["%1"].pastes == []


# --- ui ---------------------------------------------------------------------------------


def test_ui_without_a_running_asq_is_a_said_no_op(projects: dict[str, ProjectInfo]) -> None:
    result = ok(actions.ui("open_spawn"))
    assert result["delivered"] is False
    assert result["said"] == "asq is not running"


def test_ui_is_a_said_no_op_under_a_home_too_long_for_a_unix_socket(
    projects: dict[str, ProjectInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_root: Path,
) -> None:
    """runner2's repro (13041): a 100+ byte AISQUARE_HOME gave 'AF_UNIX path too long'."""
    deep = tmp_path / ("h" * 60) / ("o" * 60) / "home"
    monkeypatch.setenv("AISQUARE_HOME", str(deep))
    assert len(str(deep / "captain" / "ui.sock")) > 110
    result = ok(actions.ui("open_spawn"))
    assert (result["delivered"], result["said"]) == (False, "asq is not running")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_ui_never_dials_into_a_fallback_folder_someone_else_could_have_made(
    projects: dict[str, ProjectInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_root: Path,
) -> None:
    """The fix round's own review: a long home's socket lives in a shared place, and a
    folder there another account pre-created could hold a listener that reads every ui
    request and answers it as asq. The client holds it to the binder's rule first."""
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / ("h" * 60) / ("o" * 60) / "home"))
    squatted = short_root / f"aisquare-{captain_state._user_tag()}"
    squatted.mkdir()
    os.chmod(squatted, 0o777)
    message = refused(lambda: actions.ui("open_spawn"))
    assert message.startswith("error: refusing the ui socket folder")
    assert "not a private folder" in message


@UNIX_SOCKETS
def test_ui_reaches_a_receiver_bound_at_the_helpers_path_under_a_long_home(
    projects: dict[str, ProjectInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    short_root: Path,
) -> None:
    """The one helper both sides use (T4 binds it): it fits, so bind and connect both work."""
    deep = tmp_path / ("h" * 60) / ("o" * 60) / "home"
    monkeypatch.setenv("AISQUARE_HOME", str(deep))
    path = captain_state.ui_socket_path(create=True)
    assert path.is_relative_to(short_root), "the long home's socket is in the short root"
    server = _unix_socket()
    server.bind(str(path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn, conn.makefile("rwb") as stream:
            stream.readline()
            stream.write(b'{"ok": true, "said": "done"}\n')
            stream.flush()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        result = ok(actions.ui("open_spawn"))
    finally:
        thread.join(timeout=5)
        server.close()
        path.unlink(missing_ok=True)
    assert (result["delivered"], result["said"]) == (True, "done")


@contextlib.contextmanager
def _asq_socket(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | None
) -> Iterator[list[dict[str, Any]]]:
    """A stand-in receiver on a short socket path (AF_UNIX paths are capped near 100 bytes).

    ``reply=None`` is a receiver that reads the request and never answers.
    """
    folder = Path(tempfile.mkdtemp(prefix="asq"))
    path = folder / "ui.sock"
    monkeypatch.setattr(captain_state, "ui_socket_path", lambda **_: path)
    received: list[dict[str, Any]] = []
    server = _unix_socket()
    server.bind(str(path))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn, conn.makefile("rwb") as stream:
            received.append(json.loads(stream.readline()))
            if reply is None:
                stream.read()  # hold on until the caller gives up and hangs up
                return
            stream.write((json.dumps(reply) + "\n").encode())
            stream.flush()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield received
    finally:
        thread.join(timeout=5)
        server.close()
        path.unlink(missing_ok=True)
        folder.rmdir()


@UNIX_SOCKETS
def test_ui_delivers_one_json_line_and_relays_the_receivers_answer(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _asq_socket(monkeypatch, {"ok": True, "said": "spawn dialog open"}) as received:
        result = ok(actions.ui("select_agent", "coder-1"))
    assert received == [{"v": 1, "action": "select_agent", "arg": "coder-1"}]
    assert (result["delivered"], result["said"]) == (True, "spawn dialog open")


@UNIX_SOCKETS
def test_ui_says_the_receivers_refusal(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _asq_socket(monkeypatch, {"ok": False, "said": "unknown ui action 'fly'"}):
        message = refused(lambda: actions.ui("fly"))
    assert message.startswith("refused: asq said: unknown ui action 'fly'")


@UNIX_SOCKETS
def test_ui_says_a_receiver_that_never_answers(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions, "UI_TIMEOUT_S", 0.2)
    with _asq_socket(monkeypatch, None):
        message = refused(lambda: actions.ui("open_spawn"))
    assert message.startswith("error: asq did not answer within 0.2s")


@UNIX_SOCKETS
def test_a_socket_a_crashed_asq_left_behind_is_a_said_no_op(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = Path(tempfile.mkdtemp(prefix="asq"))
    path = folder / "ui.sock"
    monkeypatch.setattr(captain_state, "ui_socket_path", lambda **_: path)
    stale = _unix_socket()
    stale.bind(str(path))
    stale.close()  # the file stays; nothing listens on it
    try:
        result = ok(actions.ui("open_spawn"))
    finally:
        path.unlink(missing_ok=True)
        folder.rmdir()
    assert (result["delivered"], result["said"]) == (False, "asq is not running")


# --- act: the owner's action list -------------------------------------------------------


def test_act_runs_a_bundled_action(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    """approve_prompt is ``press yes`` (T1b): the real chooser's Yes digit, read off the pane."""
    _at(fleet_rec, CHOOSER, **{"1": IDLE})
    result = ok(actions.act("approve_prompt", {"project": "alpha", "label": "coder-1"}))
    assert fleet_rec.panes["%1"].keys == [("1",)]
    assert [step["step"] for step in result["steps"]] == ["press yes"]


def test_act_runs_a_config_defined_sequence_in_order_with_its_placeholders(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config(
        "[captain.actions.nudge_and_look]\n"
        'description = "say something, then read the pane"\n'
        'steps = ["tell {text}", "press enter", "read_pane 2"]\n'
    )
    fleet_rec.panes["%1"].screen = ["one", "two", "three"]
    result = ok(
        actions.act(
            "nudge_and_look", {"project": "alpha", "label": "coder-1", "text": "keep going"}
        )
    )
    assert fleet_rec.names() == ["tell"]
    assert fleet_rec.calls[0][1]["text"] == "keep going"
    assert fleet_rec.panes["%1"].keys == [("Enter",)]
    assert [s["step"] for s in result["steps"]] == ["tell keep going", "press enter", "read_pane 2"]
    assert result["steps"][2]["result"]["lines"] == ["two", "three"]


def test_act_refuses_an_unknown_primitive_by_name_before_any_step_runs(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config('[captain.actions.risky]\nsteps = ["press y", "shell rm -rf /"]\n')
    message = refused(lambda: actions.act("risky", {"project": "alpha", "label": "coder-1"}))
    assert message.startswith("refused: action risky step 2: unknown primitive 'shell'")
    assert "press, paste, tell, read_pane, ui, task" in message
    assert fleet_rec.panes["%1"].keys == [], "nothing runs from a sequence that fails validation"


def test_act_refuses_a_missing_placeholder_by_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config('[captain.actions.say]\nsteps = ["tell {text}"]\n')
    message = refused(lambda: actions.act("say", {"project": "alpha", "label": "coder-1"}))
    assert message.startswith("refused: action say step 1 needs args: text")
    assert fleet_rec.calls == []


def test_act_refuses_an_unknown_action_and_lists_the_known_ones(
    projects: dict[str, ProjectInfo],
) -> None:
    write_config('[captain.actions.mine]\nsteps = ["ui open_stop"]\n')
    message = refused(lambda: actions.act("no_such_action"))
    assert message.startswith("refused: no action named 'no_such_action'")
    assert "approve_prompt" in message and "mine" in message


def test_a_config_action_wins_over_a_bundled_one_of_the_same_name(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config('[captain.actions.approve_prompt]\nsteps = ["press enter"]\n')
    ok(actions.act("approve_prompt", {"project": "alpha", "label": "coder-1"}))
    assert fleet_rec.panes["%1"].keys == [("Enter",)]


def test_a_step_that_fails_keeps_error_and_a_step_that_writes_keeps_its_receipt(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = add_task(alpha, "take me")
    write_config(
        '[captain.actions.take]\nsteps = ["task claim {task}"]\n'
        '[captain.actions.nudge]\nsteps = ["tell {text}"]\n'
    )
    taken = ok(actions.act("take", {"project": "alpha", "task": card.id}))
    claims = [e for e in _events_of(alpha, "task_claimed") if e.task_id == card.id]
    assert taken["steps"][0]["receipt"] == claims[-1].seq
    assert audit(alpha.id)[-1]["receipt"] == claims[-1].seq

    def tmux_down(
        project: ProjectInfo, label: str, text: str, *, sender: str | None = None
    ) -> fleet.TellResult:
        raise TmuxError("no server running on /tmp/tmux-1001/asq")

    monkeypatch.setattr(fleet, "tell", tmux_down)
    message = refused(
        lambda: actions.act("nudge", {"project": "alpha", "label": "coder-1", "text": "hi"})
    )
    assert message.startswith("error: action nudge stopped at step 1 (tell hi)"), (
        "a failure is not a rule: the captain may retry it"
    )


def test_a_failing_step_stops_the_sequence_and_names_the_step(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config('[captain.actions.two]\nsteps = ["press y", "tell after"]\n')
    fleet_rec.states["coder-1"] = "working"
    message = refused(lambda: actions.act("two", {"project": "alpha", "label": "coder-1"}))
    assert message.startswith("refused: action two stopped at step 1 (press y): coder-1 is working")
    assert fleet_rec.calls == []


def test_a_malformed_config_action_refuses_itself_only(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet, clock: Clock
) -> None:
    write_config('[captain.actions.broken]\nsteps = "press y"\n')
    assert refused(lambda: actions.act("broken")).startswith(
        "refused: captain.actions.broken in config.toml is not valid: steps must be a list"
    )
    _at(fleet_rec, CHOOSER, **{"1": IDLE})
    ok(actions.act("approve_prompt", {"project": "alpha", "label": "coder-1"}))


# --- attention: the T7 seam -------------------------------------------------------------


def test_the_queue_tools_answer_from_the_real_queue(projects: dict[str, ProjectInfo]) -> None:
    """With T7 in, the four tools read the home's queue: an empty home is an empty list (the
    truth, now that there is a queue to read), and an unknown item is refused in the queue's
    own words through the same frame. The T1 stub's refusal ("lands with T7") is gone."""
    result = ok(actions.attention())
    assert result["items"] == [] and result["action_seq"] > 0
    assert ok(actions.next_item())["item"] is None
    assert refused(lambda: actions.resolve("q1", "told coder-1")).startswith(
        "refused: no queue item matches 'q1'"
    )
    assert refused(lambda: actions.snooze("q1", 10)).startswith(
        "refused: no queue item matches 'q1'"
    )


def test_the_queue_tools_hand_the_queue_seams_answer_through(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    item: dict[str, object] = {"id": "q1", "kind": "question", "text": "merge?"}
    seen: list[int] = []

    def ranked(limit: int) -> list[dict[str, object]]:
        seen.append(limit)
        return [item]

    monkeypatch.setattr(captain_queue, "ranked", ranked)
    monkeypatch.setattr(captain_queue, "next_item", lambda: item)
    monkeypatch.setattr(
        captain_queue, "resolve", lambda item_id, how: {**item, "status": "resolved"}
    )
    monkeypatch.setattr(
        captain_queue, "snooze", lambda item_id, minutes: {**item, "status": "snoozed"}
    )
    assert ok(actions.attention(limit=3))["items"] == [item]
    assert seen == [3]
    assert ok(actions.next_item())["item"] == item
    assert ok(actions.resolve("q1", "said yes"))["item"]["status"] == "resolved"
    assert ok(actions.snooze("q1", 5))["item"]["status"] == "snoozed"


# --- speak, thinking, bt ----------------------------------------------------------------


def test_speak_spools_for_the_speaker(projects: dict[str, ProjectInfo]) -> None:
    first = ok(actions.speak("item one: coder-1 asks to merge"))
    second = ok(actions.speak("item two"))
    assert (first["queued"], second["queued"]) == (1, 2)
    assert [s.text for s in captain_state.pending_speech()] == [
        "item one: coder-1 asks to merge",
        "item two",
    ]
    assert captain_state.pending_speech()[0].id == first["id"]


def test_thinking_sets_and_clears_the_busy_flag(projects: dict[str, ProjectInfo]) -> None:
    assert ok(actions.thinking("on"))["busy"] is True
    assert captain_state.busy_since() is not None
    assert ok(actions.thinking("off"))["busy"] is False
    assert captain_state.busy_since() is None
    assert refused(lambda: actions.thinking("maybe")).startswith(
        "refused: thinking takes on or off"
    )


def test_bt_undoes_a_recorded_claim(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    card = add_task(alpha, "a card the captain grabbed by mistake")
    ok(actions.task("alpha", "claim", card.id))
    assert task_now(card.id).status == "doing"
    result = ok(actions.bt())
    assert result["undid"] == {
        "kind": "claim",
        "task": card.id,
        "project": alpha.id,
        "how": "released",
    }
    released = _events_of(alpha, "task_released")
    assert audit(alpha.id)[-1]["tool"] == "task"  # the claim's audit; bt audits on home
    assert audit(captain_state.home_project().id)[-1]["receipt"] == released[-1].seq
    after = task_now(card.id)
    assert (after.status, after.claimed_by) == ("todo", None)
    assert ok(actions.bt())["undid"] is None


def test_bt_reopens_a_task_the_captain_closed(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    card = add_task(alpha, "closed too early")
    ok(actions.task("alpha", "done", card.id))
    result = ok(actions.bt())
    assert result["undid"]["how"] == "reopened"
    assert task_now(card.id).status == "todo"
    reopened = _events_of(alpha, "task_reopened")
    assert audit(captain_state.home_project().id)[-1]["receipt"] == reopened[-1].seq


def test_a_bt_whose_undo_fails_puts_it_back_and_says_what_it_did_do(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = add_task(alpha, "first")
    second = add_task(alpha, "second")
    ok(actions.task("alpha", "claim", first.id))
    ok(actions.task("alpha", "claim", second.id))
    actions.speak("one")

    def locked(ref: str, *, session_ref: str | None = None) -> TeamTask:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(team_service, "release_task", locked)
    message = refused(lambda: actions.bt())
    assert message.startswith(
        f"error: brake: cleared 1 queued line; undoing the claim of {second.id}"
    )
    assert "back on the undo list" in message
    back = captain_state.pop_undo()
    assert back is not None and back.task_id == second.id, "the SAME entry, not an older one"


def test_bt_clears_the_speech_queue_and_stamps_the_brake(projects: dict[str, ProjectInfo]) -> None:
    actions.speak("one")
    actions.speak("two")
    before = datetime.now(tz=UTC)
    result = ok(actions.bt())
    assert result["speech_cleared"] == 2
    assert captain_state.pending_speech() == []
    assert captain_state.brake_pulled_after(before)
    assert result["said"].startswith("brake: cleared 2 queued lines")
    one = ok(actions.speak("three"))
    assert one["queued"] == 1
    assert ok(actions.bt())["said"].startswith("brake: cleared 1 queued line,")


# --- wololo -----------------------------------------------------------------------------


def test_wololo_converts_an_idle_agent_to_a_new_task(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    old = add_task(alpha, "the old job")
    new = add_task(alpha, "the new job")
    team_service.claim_task(old.id, session_ref="sess-coder-1")
    result = ok(actions.wololo("alpha", "coder-1", new.id))
    assert result["released"] == [old.id]
    assert result["claimed"] == new.id
    assert task_now(old.id).status == "todo"
    moved = task_now(new.id)
    assert (moved.status, moved.claimed_by) == ("doing", "sess-coder-1")
    ((name, call),) = fleet_rec.calls
    assert (name, call["label"]) == ("tell", "coder-1")
    assert new.id in call["text"]
    assert result["said"] == f"Wololo! coder-1 converts to {new.id}"
    claimed = [e for e in _events_of(alpha, "task_claimed") if e.task_id == new.id]
    assert audit(alpha.id)[-1]["receipt"] == claimed[-1].seq


def test_wololo_refuses_a_working_agent_and_changes_nothing(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    old = add_task(alpha, "the old job")
    new = add_task(alpha, "the new job")
    team_service.claim_task(old.id, session_ref="sess-coder-1")
    fleet_rec.states["coder-1"] = "working"
    assert refused(lambda: actions.wololo("alpha", "coder-1", new.id)).startswith(
        "refused: coder-1 is working"
    )
    assert task_now(old.id).status == "doing"
    assert task_now(new.id).status == "todo"
    assert fleet_rec.calls == []


def test_wololo_refuses_a_card_outside_the_pool_and_keeps_the_agents_work(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    old = add_task(alpha, "the old job")
    taken = add_task(alpha, "someone else's job")
    team_service.claim_task(old.id, session_ref="sess-coder-1")
    team_service.claim_task(taken.id, session_ref="sess-coder-2")
    assert refused(lambda: actions.wololo("alpha", "coder-1", taken.id)).startswith(
        f"refused: {taken.id} is doing — wololo takes a card from the pool"
    )
    assert task_now(old.id).claimed_by == "sess-coder-1"
    assert task_now(taken.id).claimed_by == "sess-coder-2"
    assert fleet_rec.calls == []


def test_wololo_that_loses_the_claim_race_leaves_the_agents_work_alone(
    alpha: ProjectInfo,
    agents: dict[str, FleetAgent],
    fleet_rec: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card was in the pool when wololo looked and claimed when it reached for it."""
    old = add_task(alpha, "the old job")
    new = add_task(alpha, "the new job")
    team_service.claim_task(old.id, session_ref="sess-coder-1")
    real_claim = team_service.claim_task

    def raced(ref: str, *, session_ref: str | None = None) -> TeamTask:
        if ref == new.id:
            real_claim(new.id, session_ref="sess-coder-2")  # someone got there first
        return real_claim(ref, session_ref=session_ref)

    monkeypatch.setattr(team_service, "claim_task", raced)
    assert refused(lambda: actions.wololo("alpha", "coder-1", new.id)).startswith("refused: ")
    assert task_now(old.id).claimed_by == "sess-coder-1", "the old claim was never given up"
    assert fleet_rec.calls == []


def test_wololo_refuses_an_agent_that_never_joined_the_board(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    new = add_task(alpha, "the new job")
    assert refused(lambda: actions.wololo("alpha", "coder-3", new.id)).startswith(
        "refused: coder-3 has not joined the board"
    )
