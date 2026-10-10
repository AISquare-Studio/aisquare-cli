"""Fleet, panes and send-keys work for ANY project, not just the server's own cwd.

``fleet()``, ``panes/<agent>`` and ``send-keys`` all used to call
``fleet_service.resolve_project(None)``, so the remote page could only ever see
the project the server was started in — picking another project in the UI
silently showed the wrong fleet. These pin the threading of an optional
``project`` through all three, the 404 an unknown one gets, the per-state agent
counts on ``GET /api/projects``, and the ``{subscribe_fleet}`` WS option.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.core.store import store_session
from aisquare.core.workspace import find_project_root, project_id_for
from aisquare.models import FleetAgent, FleetAgentState, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.remote_server import (
    COOKIE,
    NoSuchAgent,
    NoSuchProject,
    RequestError,
    Runtime,
    Sources,
    _agent_state_counts,
    _audit_keys,
    _resolve_project,
    build_app,
    live_sources,
    live_writes,
)
from tests.remote_kit_helpers import frame_within, make_client

PASSWORD = "Test1234"
T0 = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def _status(label: str, state: FleetAgentState) -> FleetAgentStatus:
    agent = FleetAgent(
        id=f"agt_{label}",
        project_id="prj_x",
        label=label,
        role="coder",
        pane_id="%1",
        cwd=Path("/tmp/x"),
        created_at=T0,
    )
    return FleetAgentStatus(agent=agent, state=state)


# --- the counts themselves (§ the Projects screen summarises without N calls) ----------


def test_the_five_wire_words_are_always_present_even_for_an_empty_fleet() -> None:
    """§4-K: a flat object, all five CLI words, zeros included."""
    assert _agent_state_counts([]) == {
        "working": 0,
        "waiting": 0,
        "attention": 0,
        "exited": 0,
        "lost": 0,
    }


def test_the_wire_word_is_attention_never_needs_you() -> None:
    """§4-K/§4-B: `asq --json` vocabulary verbatim; the FE maps it to "NEEDS YOU"."""
    counts = _agent_state_counts([_status("a", "attention"), _status("b", "attention")])
    assert counts["attention"] == 2
    assert "needs_you" not in counts


def test_a_mixed_fleet_is_counted_per_state() -> None:
    agents = [
        _status("a", "working"),
        _status("b", "working"),
        _status("c", "waiting"),
        _status("d", "attention"),
        _status("e", "exited"),
    ]
    assert _agent_state_counts(agents) == {
        "working": 2,
        "waiting": 1,
        "attention": 1,
        "exited": 1,
        "lost": 0,
    }


def test_lost_is_always_present_and_unknown_only_when_an_agent_is_in_it() -> None:
    """``unknown`` is the one state §4-K does not name — never dropped, so the
    counts cannot under-report a fleet, but never conjured either."""
    quiet = _agent_state_counts([_status("a", "working")])
    assert quiet["lost"] == 0, "one of the five, always present"
    assert "unknown" not in quiet
    noisy = _agent_state_counts([_status("a", "working"), _status("b", "unknown")])
    assert noisy["unknown"] == 1
    assert sum(noisy.values()) == 2, "every agent is counted exactly once"


# --- two real projects in one store ----------------------------------------------------


@pytest.fixture
def two_projects(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """``(current, other)`` — one project pinned active by ``init``, one merely registered.

    The second is what the remote page can now reach and never could before.
    """
    current_dir = tmp_path / "current"
    current_dir.mkdir()
    monkeypatch.chdir(current_dir)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    current = fleet_service.resolve_project(None)

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = ProjectInfo(
        id=project_id_for(find_project_root(other_dir)), root=other_dir, linked_repos=[]
    )
    with store_session() as store:
        # Onboarded, not merely ensured: a captured directory is not listed (#139).
        store.ensure_project(other)
        store.onboard_project(other)
    assert other.id != current.id
    return current.id, other.id


def _seed_agent(
    project_id: str, label: str, pane_id: str = "%9", *, ended_at: datetime | None = None
) -> None:
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{project_id[-6:]}_{label}",
                project_id=project_id,
                label=label,
                role="coder",
                pane_id=pane_id,
                cwd=Path("/tmp/x"),
                created_at=T0,
                ended_at=ended_at,
                exit_status=None if ended_at is None else 0,
            )
        )


class _FakeFacts:
    cursor_x = 0
    cursor_y = 0
    width = 80
    height = 24
    cursor_visible = True
    server_started = T0 - timedelta(hours=1)  # said with the capture, as tmux says it


class _FakeCapture:
    def __init__(self, pane_id: str) -> None:
        self.lines = [f"pane {pane_id}"]
        self.facts = _FakeFacts()


class _FakeTmux:
    """Enough tmux for ``panes`` and ``send-keys``; the real one needs a live server.

    Every pane runs the agent, on the server the rows were recorded on: it started
    before they were written (``T0``)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    def pane_facts(self, pane_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            dead=False, current_command="claude", server_started=self.started_at()
        )

    def started_at(self) -> datetime:
        return T0 - timedelta(hours=1)

    def capture(self, pane_id: str, **kwargs: Any) -> _FakeCapture:
        return _FakeCapture(pane_id)

    def send_literal(self, pane_id: str, text: str) -> None:
        self.sent.append(("literal", (pane_id, text)))

    def send_keys(self, pane_id: str, *keys: str) -> None:
        self.sent.append(("keys", (pane_id, *keys)))


@pytest.fixture
def fake_tmux(monkeypatch: pytest.MonkeyPatch) -> _FakeTmux:
    server = _FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: server)
    return server


# --- _resolve_project, the one place the threading lands -------------------------------


def test_no_project_is_still_the_current_one(two_projects: tuple[str, str]) -> None:
    current, _other = two_projects
    assert _resolve_project(None).id == current


def test_a_named_project_resolves_to_that_project(two_projects: tuple[str, str]) -> None:
    _current, other = two_projects
    assert _resolve_project(other).id == other


def test_an_unknown_project_raises_a_lookup_error(two_projects: tuple[str, str]) -> None:
    """A ``LookupError``, so every existing ``except LookupError`` → 404 covers it."""
    with pytest.raises(NoSuchProject) as raised:
        _resolve_project("no-such-project")
    assert isinstance(raised.value, LookupError)
    assert "no-such-project" in str(raised.value)


# --- fleet ------------------------------------------------------------------------------


def test_fleet_without_a_project_is_the_current_project(two_projects: tuple[str, str]) -> None:
    current, other = two_projects
    _seed_agent(current, "coder-1")
    _seed_agent(other, "coder-2")
    payload = live_sources().fleet(None)
    assert isinstance(payload, dict)
    assert payload["project"]["id"] == current
    assert [row["agent"]["label"] for row in payload["agents"]] == ["coder-1"]


def test_fleet_with_a_project_returns_that_projects_agents(
    two_projects: tuple[str, str],
) -> None:
    current, other = two_projects
    _seed_agent(current, "coder-1")
    _seed_agent(other, "coder-2")
    reads = live_sources()
    scoped = reads.fleet(other)
    assert isinstance(scoped, dict)
    assert scoped["project"]["id"] == other
    assert [row["agent"]["label"] for row in scoped["agents"]] == ["coder-2"]
    assert scoped != reads.fleet(None), "the two projects must not answer alike"


def test_fleet_payload_is_what_the_json_command_builds(two_projects: tuple[str, str]) -> None:
    """§4-B: the shape stays ``asq --json fleet ls``, whichever project is asked for."""
    from aisquare.cli.fleet import agents_json

    _current, other = two_projects
    _seed_agent(other, "coder-2")
    target = fleet_service.resolve_project(other)
    expected = agents_json(target, fleet_service.list_agents(target, live_only=True))
    assert live_sources().fleet(other) == expected
    assert set(expected) == {"project", "name", "codename", "tmux_session", "agents"}


def test_fleet_with_an_unknown_project_raises(two_projects: tuple[str, str]) -> None:
    with pytest.raises(NoSuchProject):
        live_sources().fleet("no-such-project")


# --- panes ------------------------------------------------------------------------------


def test_panes_without_a_project_reads_the_current_projects_agent(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    current, other = two_projects
    _seed_agent(current, "coder-1", "%1")
    _seed_agent(other, "coder-1", "%2")  # SAME label, other project — scoping, not luck
    assert live_sources().panes("coder-1", None, 0)["rows"] == ["pane %1"]


def test_panes_with_a_project_reads_that_projects_agent(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    current, other = two_projects
    _seed_agent(current, "coder-1", "%1")
    _seed_agent(other, "coder-1", "%2")
    assert live_sources().panes("coder-1", other, 0)["rows"] == ["pane %2"]


def test_panes_for_an_agent_the_named_project_does_not_have(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    current, other = two_projects
    _seed_agent(current, "only-here")
    with pytest.raises(NoSuchAgent):
        live_sources().panes("only-here", other, 0)


def test_panes_with_an_unknown_project_raises(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    with pytest.raises(NoSuchProject):
        live_sources().panes("coder-1", "no-such-project", 0)


# --- send-keys (still a §4-E write: 403 unless allow_write) -----------------------------


def test_send_keys_without_a_project_targets_the_current_one(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    current, other = two_projects
    _seed_agent(current, "coder-1", "%1")
    _seed_agent(other, "coder-1", "%2")
    result, summary = live_writes().handlers["send-keys"]({"agent": "coder-1", "enter": True})
    assert result == {"agent": "coder-1", "project": current, "sent": True}
    assert fake_tmux.sent == [("keys", ("%1", "Enter"))]
    assert current in summary, "the audit line names the project it reached"


def test_send_keys_with_a_project_targets_that_one(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    current, other = two_projects
    _seed_agent(current, "coder-1", "%1")
    _seed_agent(other, "coder-1", "%2")
    result, _summary = live_writes().handlers["send-keys"](
        {"agent": "coder-1", "project": other, "text": "hi"}
    )
    assert result == {"agent": "coder-1", "project": other, "sent": True}
    assert fake_tmux.sent == [("literal", ("%2", "hi"))]


def test_send_keys_with_an_unknown_project_raises(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    with pytest.raises(NoSuchProject):
        live_writes().handlers["send-keys"](
            {"agent": "coder-1", "project": "no-such-project", "enter": True}
        )


def test_send_keys_with_an_unknown_agent_in_a_real_project_still_says_so(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    _current, other = two_projects
    with pytest.raises(NoSuchAgent):
        live_writes().handlers["send-keys"]({"agent": "ghost", "project": other, "enter": True})


# --- the audit line for a key send (§4-E) ------------------------------------------------
#
# The FE's five write controls (confirmed against coder-fe-scaffold's payloads):
# reply field {text, enter}, Stop 1st {keys:[Escape]}, Stop 2nd {keys:[C-c]},
# key strip {keys:[Up|Down|Enter|Escape|Tab]}, digit {text:"3"}. A trail that
# logged only a COUNT could not tell a Ctrl-C to a live agent from an arrow key.


def _summary_for(body: dict[str, Any]) -> str:
    _result, summary = live_writes().handlers["send-keys"]({"agent": "coder-1", **body})
    return summary


def test_each_write_control_leaves_a_distinct_audit_summary(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    current, _other = two_projects
    _seed_agent(current, "coder-1", "%1")
    here = f"coder-1@{current}"

    reply = _summary_for({"text": "hello world!", "enter": True})
    stop_first = _summary_for({"keys": ["Escape"]})
    stop_escalated = _summary_for({"keys": ["C-c"]})
    named_key = _summary_for({"keys": ["Up"]})
    digit = _summary_for({"text": "3"})

    assert reply == f"{here} text=12ch keys=0 enter=True"
    assert stop_first == f"{here} text=0ch keys=[Escape] enter=False"
    assert stop_escalated == f"{here} text=0ch keys=[C-c] enter=False"
    assert named_key == f"{here} text=0ch keys=[Up] enter=False"
    assert digit == f"{here} text=1ch keys=0 enter=False"

    lines = [reply, stop_first, stop_escalated, named_key, digit]
    assert len(set(lines)) == 5, "every control must be distinguishable in the log"


def test_an_interrupt_reads_as_an_escalation_not_a_bare_fact(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    """Stop escalates, so a real interrupt logs Escape then C-c — the human tried
    the soft one first. That sequence is the forensic payoff of names over counts."""
    current, _other = two_projects
    _seed_agent(current, "coder-1", "%1")
    trail = [_summary_for({"keys": ["Escape"]}), _summary_for({"keys": ["C-c"]})]
    assert [line.split("keys=")[1].split(" ")[0] for line in trail] == ["[Escape]", "[C-c]"]


def test_the_typed_text_is_counted_never_captured(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    """§4-E: key names are a bounded vocabulary; what a human typed is not."""
    current, _other = two_projects
    _seed_agent(current, "coder-1", "%1")
    secret = "hunter2 my-api-key"
    summary = _summary_for({"text": secret, "enter": True})
    assert f"text={len(secret)}ch" in summary
    assert secret not in summary
    for word in ("hunter2", "my-api-key"):
        assert word not in summary


def test_a_key_name_cannot_forge_an_audit_line(
    two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    """The log is one line per write, and a key name is caller-controlled — a newline in
    one must not buy a second line. The allowlist refuses such a key before anything is
    sent, and the refusal itself carries no newline; the summary's own scrub stays as
    defence in depth (SPEC §2.7)."""
    current, _other = two_projects
    _seed_agent(current, "coder-1", "%1")
    forged = "Up\n2026-01-01T00:00:00+00:00 someone-else note nothing-to-see"
    with pytest.raises(RequestError) as refused:
        _summary_for({"keys": [forged]})
    assert refused.value.error == "invalid_key"
    assert "\n" not in refused.value.message and "\r" not in refused.value.message
    assert fake_tmux.sent == [], "nothing reached the pane"
    scrubbed = _audit_keys([forged])
    assert "\n" not in scrubbed and scrubbed.startswith("[Up?")


def test_the_audit_line_written_to_disk_carries_the_key_name(
    runtime: Runtime, tmp_path: Path, two_projects: tuple[str, str], fake_tmux: _FakeTmux
) -> None:
    """End to end: through the HTTP write gate and into remote-audit.log."""
    current, _other = two_projects
    _seed_agent(current, "coder-1", "%1")
    client = make_client(
        build_app(runtime, sources=_sources(FLEETS), writes=live_writes(), dist_dir=tmp_path)
    )
    assert (
        client.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD}).status_code
        == 200
    )
    runtime.set_allow_write(True)
    sent = client.post(
        f"/r/{runtime.token}/api/send-keys", json={"agent": "coder-1", "keys": ["C-c"]}
    )
    assert sent.status_code == 200
    line = remote_audit_path().read_text().splitlines()[-1]
    _ts, device_id, endpoint, summary = line.split(" ", 3)
    assert endpoint == "send-keys"
    assert device_id == client.get(f"/r/{runtime.token}/api/devices").json()[0]["id"]
    assert device_id != client.cookies[COOKIE], "the trail names the device, never its cookie"
    assert summary == f"coder-1@{current} text=0ch keys=[C-c] enter=False"


# --- GET /api/projects carries the counts ------------------------------------------------


def test_projects_rows_carry_agent_state_counts(
    two_projects: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    current, other = two_projects
    _seed_agent(current, "a")
    _seed_agent(other, "c", ended_at=datetime.now(tz=UTC) - timedelta(hours=1))

    def fake_list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        assert live_only is True, "the counts come from the same live_only listing"
        if project.id == current:
            return [_status("a", "working"), _status("b", "attention")]
        return [_status("c", "exited")]

    monkeypatch.setattr(fleet_service, "list_agents", fake_list_agents)
    rows = live_sources().projects()
    assert isinstance(rows, list)
    by_id = {row["id"]: row for row in rows}
    assert by_id[current]["agents"] == {
        "working": 1,
        "waiting": 0,
        "attention": 1,
        "exited": 0,
        "lost": 0,
    }
    assert by_id[other]["agents"] == {
        "working": 0,
        "waiting": 0,
        "attention": 0,
        "exited": 1,
        "lost": 0,
    }


def test_a_project_with_no_agent_within_the_day_is_counted_without_reading_its_history(
    two_projects: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``list_agents`` reads every row and session a project ever had, and the store deletes
    none: the Projects screen paid that for every project on each read, a dormant one
    included, whose listing is empty anyway. Listed now: a project with a live row, or one
    that ended within the day, whose window may linger as an ``exited`` row (sweep of
    #243, round 4)."""
    current, other = two_projects
    now = datetime.now(tz=UTC)
    _seed_agent(current, "live")
    _seed_agent(other, "old", ended_at=now - fleet_service.RECENTLY_ENDED - timedelta(hours=1))
    listed: list[str] = []

    def fake_list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        listed.append(project.id)
        return [_status("a", "working")]

    monkeypatch.setattr(fleet_service, "list_agents", fake_list_agents)
    rows = live_sources().projects()
    assert isinstance(rows, list)
    by_id = {row["id"]: row for row in rows}
    assert listed == [current], "the dormant project's history was read"
    assert by_id[other]["agents"] == _agent_state_counts([])
    assert by_id[current]["agents"] == _agent_state_counts([_status("a", "working")])
    _seed_agent(other, "recent", ended_at=now - timedelta(hours=1))
    listed.clear()
    live_sources().projects()
    assert sorted(listed) == sorted([current, other]), "an ended row within the day is listed"


def test_projects_rows_keep_every_field_the_json_command_prints(
    two_projects: tuple[str, str],
) -> None:
    from aisquare.cli.common import projects_json
    from aisquare.services import project as project_service

    rows = live_sources().projects()
    assert isinstance(rows, list)
    plain = projects_json(project_service.list_projects())
    assert [{k: v for k, v in row.items() if k != "agents"} for row in rows] == plain


# --- the endpoints, over HTTP ------------------------------------------------------------


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    rt = Runtime(remote_state_path(), remote_audit_path())
    rt._state.password = PASSWORD
    rt._save_state()
    return rt


def _sources(fleets: dict[str | None, dict[str, object]]) -> Sources:
    def fleet(project: str | None) -> object:
        if project not in fleets:
            raise NoSuchProject(f"no project matches {project!r}")
        return fleets[project]

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        if project is not None and project not in fleets:
            raise NoSuchProject(f"no project matches {project!r}")
        return {"rows": [f"{project}:{agent}:h{history}"], "width": 1, "height": 1}

    return Sources(
        projects=lambda: [],
        fleet=fleet,
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=panes,
        explainability=lambda agent, project: {"available": False},
    )


def _client(runtime: Runtime, sources: Sources, tmp_path: Path, tick: float = 1.0) -> TestClient:
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=tick))
    unlocked = client.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD})
    assert unlocked.status_code == 200
    return client


FLEETS: dict[str | None, dict[str, object]] = {
    None: {"name": "current", "agents": []},
    "prj_other": {"name": "other", "agents": []},
}


def test_get_fleet_without_a_query_param_is_unchanged(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path)
    response = client.get(f"/r/{runtime.token}/api/fleet")
    assert response.status_code == 200
    assert response.json() == {"name": "current", "agents": []}


def test_get_fleet_with_a_project_query_param(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path)
    response = client.get(f"/r/{runtime.token}/api/fleet", params={"project": "prj_other"})
    assert response.status_code == 200
    assert response.json() == {"name": "other", "agents": []}


def test_get_fleet_with_an_unknown_project_is_404(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path)
    response = client.get(f"/r/{runtime.token}/api/fleet", params={"project": "nope"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert "nope" in response.json()["message"]


def test_get_panes_forwards_the_project_query_param(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path)
    scoped = client.get(f"/r/{runtime.token}/api/panes/coder-1", params={"project": "prj_other"})
    assert scoped.json()["rows"] == ["prj_other:coder-1:h0"]
    assert client.get(f"/r/{runtime.token}/api/panes/coder-1").json()["rows"] == ["None:coder-1:h0"]


def test_get_panes_with_an_unknown_project_is_404(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path)
    response = client.get(f"/r/{runtime.token}/api/panes/coder-1", params={"project": "nope"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_the_project_query_param_still_needs_the_cookie(runtime: Runtime, tmp_path: Path) -> None:
    app = build_app(runtime, sources=_sources(FLEETS), dist_dir=tmp_path)
    anonymous = make_client(app)
    for path in ("api/fleet", "api/panes/coder-1"):
        response = anonymous.get(f"/r/{runtime.token}/{path}", params={"project": "prj_other"})
        assert response.status_code == 401, path


# --- the WS fleet frame (§4-D) -------------------------------------------------------------


def _frame(ws: Any, kind: str, *, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        frame = frame_within(ws)
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind} frame in {limit} frames")


def test_ws_fleet_frames_follow_subscribe_fleet(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path, tick=0.02)
    with client.websocket_connect(f"/r/{runtime.token}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_fleet": None}))
        first = _frame(ws, "fleet")
        assert first["payload"] == {"name": "current", "agents": []}
        assert set(first) == {"type", "payload", "ts"}, "§4-D shape is unchanged"

        ws.send_text(json.dumps({"subscribe_fleet": "prj_other"}))
        switched = _frame(ws, "fleet")
        assert switched["payload"] == {"name": "other", "agents": []}

        ws.send_text(json.dumps({"subscribe_fleet": ""}))
        assert _frame(ws, "fleet")["payload"] == {"name": "current", "agents": []}


def test_ws_survives_a_subscribe_fleet_for_a_project_that_is_gone(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A bad name costs that frame, not the socket — board and remote keep arriving."""
    client = _client(runtime, _sources(FLEETS), tmp_path, tick=0.02)
    with client.websocket_connect(f"/r/{runtime.token}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_fleet": ""}))
        _frame(ws, "fleet")
        ws.send_text(json.dumps({"subscribe_fleet": "nope"}))
        runtime.set_allow_write(True)
        assert _frame(ws, "remote")["payload"]["allow_write"] is True
        ws.send_text(json.dumps({"subscribe_fleet": "prj_other"}))
        assert _frame(ws, "fleet")["payload"] == {"name": "other", "agents": []}


def test_ws_still_needs_the_cookie(runtime: Runtime, tmp_path: Path) -> None:
    from starlette.testclient import WebSocketDenialResponse

    app = build_app(runtime, sources=_sources(FLEETS), dist_dir=tmp_path, tick=0.02)
    anonymous = make_client(app)
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        anonymous.websocket_connect(f"/r/{runtime.token}/ws"),
    ):
        pass
    assert denied.value.status_code == 401


def test_send_keys_over_http_is_still_403_until_allow_write(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Project scoping does not open a write path (§4-E boundary)."""
    client = _client(runtime, _sources(FLEETS), tmp_path)
    response = client.post(
        f"/r/{runtime.token}/api/send-keys",
        json={"agent": "coder-1", "project": "prj_other", "enter": True},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "read_only"
    endpoints = [line.split(" ")[2] for line in remote_audit_path().read_text().splitlines()]
    assert endpoints == ["unlock"], "the unlock is on the trail; the refused write is not"


def test_devices_and_unlock_are_untouched_by_the_query_param(
    runtime: Runtime, tmp_path: Path
) -> None:
    client = _client(runtime, _sources(FLEETS), tmp_path)
    rows = client.get(f"/r/{runtime.token}/api/devices").json()
    assert [row["current"] for row in rows] == [True]
    assert client.cookies[COOKIE] not in json.dumps(rows), "a device is listed by id"
