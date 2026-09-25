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
import os
import socket
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
from aisquare.services import fleet
from aisquare.services import team as team_service
from aisquare.services.captain import actions
from aisquare.services.captain import queue as captain_queue
from aisquare.services.captain import state as captain_state

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
        self._panes[pane_id].keys.append(keys)

    def paste(self, pane_id: str, text: str) -> None:
        self._panes[pane_id].pastes.append(text)

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

    def stop(self, project: ProjectInfo, label: str, **kwargs: Any) -> fleet.StopReceipt:
        self.calls.append(("stop", {"project": project.id, "label": label, **kwargs}))
        return fleet.StopReceipt(agent=_row(project, label, "coder", "%1"), released=[])

    def restart(self, project: ProjectInfo, label: str, **kwargs: Any) -> fleet.RestartReceipt:
        self.calls.append(("restart", {"project": project.id, "label": label, **kwargs}))
        row = _row(project, label, "coder", "%1")
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


def task_now(task_id: str) -> TeamTask:
    with store_session() as store:
        task = store.get_task(task_id)
    assert task is not None
    return task


def write_config(text: str) -> None:
    paths.ensure_home()
    paths.config_path().write_text(text, encoding="utf-8")


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


def test_the_stdio_server_closes_itself_after_the_idle_deadline(isolated_home: Path) -> None:
    """``--close-after`` is the same idle deadline ``aisquare serve --stdio`` keeps (#19)."""
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "aisquare", "captain", "serve", "--stdio", "--close-after", "1"],
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


def test_captain_serve_speaks_stdio_only(runner: CliRunner) -> None:
    result = runner.invoke(app, ["captain", "serve"])
    assert result.exit_code == 2
    assert "--stdio" in result.output


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
    ("stop", {"project": "alpha", "label": "coder-1"}),
    ("stop", {"project": "alpha", "label": "coder-1", "confirm": True}),
    ("restart", {"project": "alpha", "label": "coder-1"}),
    ("attach_persona", {"project": "alpha", "label": "coder-1", "name": "skeptic"}),
    ("task", {"project": "alpha", "verb": "add", "ref": "write the docs"}),
    ("task", {"project": "alpha", "verb": "explode", "ref": "x"}),
    ("note", {"project": "alpha", "text": "all good"}),
    ("press", {"project": "alpha", "label": "coder-1", "key": "y"}),
    ("press", {"project": "alpha", "label": "coder-1", "key": "F13"}),
    ("paste", {"project": "alpha", "label": "coder-1", "text": "a\nb"}),
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
) -> None:
    def gone(self: FakeServer, pane_id: str, **kwargs: Any) -> Capture:
        raise TmuxError(f"can't find pane: {pane_id}")

    monkeypatch.setattr(FakeServer, "capture", gone)
    captain_state.set_watermark(alpha.id, "coder-1", 0)
    mine = add_events(alpha, "sess-coder-1", 1)
    shown = ok(actions.since("alpha", agent="coder-1"))
    assert [e["seq"] for e in shown["events"]] == [mine[0].seq]
    assert shown["pane"] is None


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


def test_spawn_goes_through_fleet_spawn_marked_as_the_captains(
    alpha: ProjectInfo, fleet_rec: Fleet
) -> None:
    result = ok(actions.spawn("alpha", "coder", label="coder-7", task="tsk_1", persona="skeptic"))
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


def test_restart_goes_through_fleet_restart(alpha: ProjectInfo, fleet_rec: Fleet) -> None:
    result = ok(actions.restart("alpha", "coder-1"))
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


def test_a_fleet_refusal_is_said_never_faked(
    alpha: ProjectInfo, fleet_rec: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_room(project: ProjectInfo, role: str, **kwargs: Any) -> fleet.SpawnReceipt:
        raise fleet.FleetError("alpha already runs 6 agents (max_agents_per_project = 6)")

    monkeypatch.setattr(fleet, "spawn", no_room)
    message = refused(lambda: actions.spawn("alpha", "coder"))
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
    assert message.startswith("refused: key 'F13' is not one of y, n, enter, esc")
    assert fleet_rec.panes["%1"].keys == []


def test_paste_is_one_bracketed_paste_and_never_an_enter(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    result = ok(actions.paste("alpha", "coder-1", "line one\nline two"))
    pane = fleet_rec.panes["%1"]
    assert pane.pastes == ["line one\nline two"]
    assert pane.keys == []
    assert result["chars"] == len("line one\nline two")


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


@contextlib.contextmanager
def _asq_socket(
    monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | None
) -> Iterator[list[dict[str, Any]]]:
    """A stand-in receiver on a short socket path (AF_UNIX paths are capped near 100 bytes).

    ``reply=None`` is a receiver that reads the request and never answers.
    """
    folder = Path(tempfile.mkdtemp(prefix="asq"))
    path = folder / "ui.sock"
    monkeypatch.setattr(captain_state, "ui_socket_path", lambda: path)
    received: list[dict[str, Any]] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
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


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the ui socket is a unix socket")
def test_ui_delivers_one_json_line_and_relays_the_receivers_answer(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _asq_socket(monkeypatch, {"ok": True, "said": "spawn dialog open"}) as received:
        result = ok(actions.ui("select_agent", "coder-1"))
    assert received == [{"v": 1, "action": "select_agent", "arg": "coder-1"}]
    assert (result["delivered"], result["said"]) == (True, "spawn dialog open")


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the ui socket is a unix socket")
def test_ui_says_the_receivers_refusal(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    with _asq_socket(monkeypatch, {"ok": False, "said": "unknown ui action 'fly'"}):
        message = refused(lambda: actions.ui("fly"))
    assert message.startswith("refused: asq said: unknown ui action 'fly'")


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the ui socket is a unix socket")
def test_ui_says_a_receiver_that_never_answers(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions, "UI_TIMEOUT_S", 0.2)
    with _asq_socket(monkeypatch, None):
        message = refused(lambda: actions.ui("open_spawn"))
    assert message.startswith("error: asq did not answer within 0.2s")


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the ui socket is a unix socket")
def test_a_socket_a_crashed_asq_left_behind_is_a_said_no_op(
    projects: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = Path(tempfile.mkdtemp(prefix="asq"))
    path = folder / "ui.sock"
    monkeypatch.setattr(captain_state, "ui_socket_path", lambda: path)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
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
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    fleet_rec.states["coder-1"] = "attention"
    result = ok(actions.act("approve_prompt", {"project": "alpha", "label": "coder-1"}))
    assert fleet_rec.panes["%1"].keys == [("y",)]
    assert [step["step"] for step in result["steps"]] == ["press y"]


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


def test_a_failing_step_stops_the_sequence_and_names_the_step(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config('[captain.actions.two]\nsteps = ["press y", "tell after"]\n')
    fleet_rec.states["coder-1"] = "working"
    message = refused(lambda: actions.act("two", {"project": "alpha", "label": "coder-1"}))
    assert message.startswith("refused: action two stopped at step 1 (press y): coder-1 is working")
    assert fleet_rec.calls == []


def test_a_malformed_config_action_refuses_itself_only(
    alpha: ProjectInfo, agents: dict[str, FleetAgent], fleet_rec: Fleet
) -> None:
    write_config('[captain.actions.broken]\nsteps = "press y"\n')
    assert refused(lambda: actions.act("broken")).startswith(
        "refused: captain.actions.broken in config.toml is not valid: steps must be a list"
    )
    fleet_rec.states["coder-1"] = "attention"
    ok(actions.act("approve_prompt", {"project": "alpha", "label": "coder-1"}))


# --- attention: the T7 seam -------------------------------------------------------------


def test_the_queue_tools_say_the_queue_is_not_built_yet(projects: dict[str, ProjectInfo]) -> None:
    for call in (
        lambda: actions.attention(),
        lambda: actions.next_item(),
        lambda: actions.resolve("q1", "told coder-1"),
        lambda: actions.snooze("q1", 10),
    ):
        assert refused(call).startswith("refused: the attention queue lands with T7")


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


def test_bt_clears_the_speech_queue_and_stamps_the_brake(projects: dict[str, ProjectInfo]) -> None:
    actions.speak("one")
    actions.speak("two")
    before = datetime.now(tz=UTC)
    result = ok(actions.bt())
    assert result["speech_cleared"] == 2
    assert captain_state.pending_speech() == []
    assert captain_state.brake_pulled_after(before)
    assert result["said"].startswith("brake: cleared 2 queued lines")


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
