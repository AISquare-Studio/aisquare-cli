"""A ``/model`` inside any fleet agent is what the next spawn runs (#16 on the board).

#338 made ``fleet spawn`` pass the role ladder's top rung, so a manager always started on
``--model fable --effort high`` over the default the owner saved with ``/model``. A spawn now
runs the owner's last pick, one per home whatever the role. The spawn side (what a spawn
passes, and what a restart or switch replays) is in ``tests/test_fleet_service.py``; this
file holds the other writer of the pick: the Stop hook, which reads the model the turn ran
off the transcript's last reply and the effort off the hook's ``CLAUDE_EFFORT`` (or the
payload's ``effort.level``), and the readers it relies on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import transcripts
from aisquare.core.orchestrator import FLEET_AGENT_ENV_VAR, team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, LaunchSpec, ProjectInfo, TeamSession
from aisquare.services import model_memory

SESSION = "sess-manager"
AGENT = "agt_manager1"


@pytest.fixture
def work(tmp_path: Path) -> ProjectInfo:
    root = tmp_path / "repo"
    root.mkdir()
    project = team_project(root)
    with store_session() as store:
        store.onboard_project(project)
    return project


def _transcript(project: ProjectInfo) -> Path:
    return project.root / f"{SESSION}.jsonl"


def _write(path: Path, *records: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _reply(model: str, **extra: Any) -> dict[str, Any]:
    """An assistant entry as Claude Code writes one, its model in ``message.model``."""
    return {"type": "assistant", "message": {"model": model, "content": []}, **extra}


def _manager(
    project: ProjectInfo,
    *,
    role: str = "manager",
    model: str | None = "claude-fable-5-1",
    effort: str | None = "high",
    spec: LaunchSpec | None = None,
) -> FleetAgent:
    """A live fleet manager whose board session started on ``model`` at ``effort`` (what its
    SessionStart hook recorded), with a transcript on disk."""
    now = datetime.now(tz=UTC)
    _write(_transcript(project), {"type": "user", "message": {"role": "user", "content": "go"}})
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=SESSION,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
                transcript_path=str(_transcript(project)),
                model=model,
                effort=effort,
            )
        )
        agent = FleetAgent(
            id=AGENT,
            project_id=project.id,
            label="manager" if role == "manager" else f"{role}-1",
            role=role,
            pane_id="%3",
            session_id=SESSION,
            cwd=project.root,
            launch_spec=spec or LaunchSpec(binary="claude"),
            created_at=now,
        )
        store.upsert_fleet_agent(agent)
    return agent


def _stop(project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, *, effort: str = "xhigh") -> None:
    """The agent's Stop hook, as Claude Code runs it in the agent's window."""
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, AGENT)
    monkeypatch.setenv("CLAUDE_EFFORT", effort)
    payload = {
        "session_id": SESSION,
        "cwd": str(project.root),
        "transcript_path": str(_transcript(project)),
        "hook_event_name": "Stop",
    }
    done = CliRunner().invoke(app, ["hook", "stop"], input=json.dumps(payload))
    assert done.exit_code == 0, done.output


def _session(project: ProjectInfo) -> TeamSession:
    with store_session() as store:
        session = store.get_session(SESSION)
    assert session is not None
    return session


@pytest.mark.parametrize("role", ["manager", "coder", "agent"])
def test_a_model_command_in_any_fleet_agent_is_what_the_next_spawn_runs(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The agent started on fable at high; the owner's ``/model`` moved it to opus at xhigh.
    Its next turn end reads that, keeps it on its session (a restart starts it) and as the
    owner's pick, whatever the agent's role (``agent`` is the plain one)."""
    _manager(work, role=role)
    _write(_transcript(work), _reply("claude-opus-5-5"))
    _stop(work, monkeypatch, effort="xhigh")
    assert model_memory.remembered() == model_memory.Pick("claude-opus-5-5", "xhigh")
    session = _session(work)
    assert (session.model, session.effort) == ("claude-opus-5-5", "xhigh")


def test_the_payloads_own_effort_is_read_before_the_environments(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code reports ultracode as xhigh in ``CLAUDE_EFFORT``; its payload names it."""
    _manager(work)
    _write(_transcript(work), _reply("claude-fable-5-1"))
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, AGENT)
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    payload = {
        "session_id": SESSION,
        "cwd": str(work.root),
        "transcript_path": str(_transcript(work)),
        "effort": {"level": "ultracode"},
    }
    done = CliRunner().invoke(app, ["hook", "stop"], input=json.dumps(payload))
    assert done.exit_code == 0, done.output
    assert model_memory.remembered() == model_memory.Pick(None, "ultracode")


@pytest.mark.parametrize("damage", ["missing", "corrupt", "no reply"])
def test_a_transcript_that_says_nothing_changes_nothing_and_the_hook_exits_0(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    _manager(work)
    model_memory.remember(model="opus", effort="xhigh")
    transcript = _transcript(work)
    if damage == "missing":
        transcript.unlink()
    elif damage == "corrupt":
        transcript.write_bytes(b'{"type": "assistant", "message": {"mod\x00\xff\n\n{]]\n')
    _stop(work, monkeypatch, effort="max")
    assert model_memory.remembered() == model_memory.Pick("opus", "xhigh")
    session = _session(work)
    assert (session.model, session.effort) == ("claude-fable-5-1", "high")


def test_a_store_that_cannot_be_read_costs_the_reading_and_never_the_turn_end(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manager(work)
    _write(_transcript(work), _reply("claude-opus-5-5"))

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store gone")

    monkeypatch.setattr(model_memory, "observe", broken)
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, AGENT)
    payload = {"session_id": SESSION, "cwd": str(work.root)}
    done = CliRunner().invoke(app, ["hook", "stop"], input=json.dumps(payload))
    assert done.exit_code == 0
    assert "the agent's model was not read (RuntimeError: store gone)" in done.stderr
    assert _session(work).state == "waiting", "the turn end itself is unharmed"


def test_a_session_no_fleet_window_runs_is_not_read(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's own Claude Code session has no ``AISQUARE_FLEET_AGENT``: its ``/model`` is
    no fleet role's, and its Stop reads no transcript."""
    _manager(work)
    _write(_transcript(work), _reply("claude-opus-5-5"))
    read: list[Path] = []
    monkeypatch.setattr(transcripts, "latest_model", lambda path, **k: read.append(path))
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    payload = {"session_id": SESSION, "cwd": str(work.root)}
    assert CliRunner().invoke(app, ["hook", "stop"], input=json.dumps(payload)).exit_code == 0
    assert read == [] and model_memory.remembered() == model_memory.Pick()


def test_the_first_reading_of_an_agent_launched_with_no_pick_is_its_accounts_default(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing said what it ran before this turn: what the transcript names is the account's
    own default, no owner's pick, and is only kept on the session for the next reading."""
    _manager(work, model=None, effort=None)
    _write(_transcript(work), _reply("claude-opus-5-5"))
    _stop(work, monkeypatch, effort="high")
    assert model_memory.remembered() == model_memory.Pick()
    assert (_session(work).model, _session(work).effort) == ("claude-opus-5-5", "high")

    _write(_transcript(work), _reply("claude-fable-5-1"))  # and now a `/model`
    _stop(work, monkeypatch, effort="max")
    assert model_memory.remembered() == model_memory.Pick("claude-fable-5-1", "max")


def test_an_alias_is_only_the_id_it_resolved_to() -> None:
    """Seq 3424 (1): ``opus`` was ANY opus id. Two readings are one model only when they
    name the same id; the first reading after a launch is the launch's own unless it is of
    another family than an alias, or another id than an id."""
    from aisquare.core import harness

    assert harness.same_reading("claude-opus-5-5", "claude-opus-5-5[1m]")
    assert not harness.same_reading("claude-opus-5-5", "claude-opus-4-5")
    assert not harness.ran_another("claude-opus-5-5", "opus")
    assert harness.ran_another("claude-fable-5-1", "opus")
    assert harness.ran_another("claude-opus-4-5", "claude-opus-5-5")
    assert not harness.ran_another("claude-opus-5-5", None), "no flag: the account's own"
    assert not harness.ran_another("claude-sonnet-5-5", "opusplan"), "a mode alias runs both"
    assert harness.same_effort("ultracode", "xhigh") and not harness.same_effort("high", "max")


def _hook(
    event: str,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str = SESSION,
    effort: str | None = None,
    **payload: Any,
) -> None:
    """``aisquare hook <event>`` as Claude Code runs it in the agent's window."""
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, AGENT)
    if effort is None:
        monkeypatch.delenv("CLAUDE_EFFORT", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_EFFORT", effort)
    body = {
        "session_id": session_id,
        "cwd": str(project.root),
        "transcript_path": str(project.root / f"{session_id}.jsonl"),
        **payload,
    }
    done = CliRunner().invoke(app, ["hook", event], input=json.dumps(body))
    assert done.exit_code == 0, done.output


def test_a_model_command_within_the_launch_aliases_family_is_a_pick(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seq 3424 (1), through the hooks: launched ``--model opus``, whose session started on
    claude-opus-5-5; a ``/model claude-opus-4-5`` is the pick."""
    _manager(work, model=None, effort=None, spec=LaunchSpec(binary="claude", model="opus"))
    monkeypatch.setenv("AISQUARE_ROLE", "manager")
    _hook(
        "session-start",
        work,
        monkeypatch,
        effort="xhigh",
        source="startup",
        model="claude-opus-5-5",
    )
    _write(_transcript(work), _reply("claude-opus-4-5"))
    _hook("stop", work, monkeypatch, effort="xhigh")
    assert model_memory.remembered() == model_memory.Pick("claude-opus-4-5", None)


def test_an_effort_changed_in_the_first_turn_is_a_pick(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seq 3424 (a): the session start reads ``CLAUDE_EFFORT`` too, so the effort it started
    on is known before the first Stop, and a ``/model`` that changes it in that turn is a
    pick. (The payload carries no ``effort`` here, as on a Claude Code that sends none.)"""
    _manager(work, model=None, effort=None)
    monkeypatch.setenv("AISQUARE_ROLE", "manager")
    _hook(
        "session-start",
        work,
        monkeypatch,
        effort="high",
        source="startup",
        model="claude-fable-5-1",
    )
    assert _session(work).effort == "high"
    _write(_transcript(work), _reply("claude-fable-5-1"))
    _hook("stop", work, monkeypatch, effort="max")
    assert model_memory.remembered() == model_memory.Pick(None, "max")


def test_a_nested_claude_in_a_fleet_agents_shell_never_writes_the_pick(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seq 3424 (2): a ``claude -p`` run from an agent's shell inherits its
    ``AISQUARE_FLEET_AGENT``, and its Stop, on the account's default at high, overwrote the
    pick. Only the session the row is bound to speaks for it (``team._fleet_row``)."""
    launched = LaunchSpec(binary="claude", extra_args=["--model", "opus", "--effort", "xhigh"])
    _manager(work, role="coder", model="claude-opus-5-5", effort="xhigh", spec=launched)
    model_memory.remember(model="opus", effort="xhigh")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("CLAUDE_PID", "999999")  # another process than the pane's
    nested = "sess-nested"
    _hook(
        "session-start",
        work,
        monkeypatch,
        session_id=nested,
        effort="high",
        source="startup",
        model="claude-sonnet-5-5",
    )
    _write(work.root / f"{nested}.jsonl", _reply("claude-sonnet-5-5"))
    _hook("stop", work, monkeypatch, session_id=nested, effort="high")
    assert model_memory.remembered() == model_memory.Pick("opus", "xhigh")
    with store_session() as store:
        row = store.get_fleet_agent(AGENT)
    assert row is not None and row.session_id == SESSION, "the row stayed its own session's"


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("fable", "max"),
        ("opus", "xhigh"),
        ("sonnet", "low"),
        ("haiku", "medium"),
        ("claude-opus-5-5", "ultracode"),
        ("opus[1m]", "high"),
    ],
)
def test_any_model_and_effort_claude_code_takes_round_trips_verbatim(
    model: str, effort: str
) -> None:
    model_memory.remember(model=model, effort=effort)
    assert model_memory.remembered() == model_memory.Pick(model, effort)


def test_one_value_is_kept_apart_from_the_other_and_a_blank_forgets_it() -> None:
    model_memory.remember(model="opus", effort="xhigh")
    model_memory.remember(effort="max")
    assert model_memory.remembered() == model_memory.Pick("opus", "max")
    model_memory.remember(model="")
    assert model_memory.remembered() == model_memory.Pick(None, "max")
    model_memory.remember(model="opus; rm -rf /", effort="extreme")
    assert model_memory.remembered() == model_memory.Pick(None, "max"), "not a model"


def test_the_latest_reply_is_read_off_the_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    assert transcripts.latest_model(path) is None  # missing
    _write(
        path,
        _reply("claude-sonnet-5-5"),
        _reply("claude-opus-5-5"),
        _reply("claude-haiku-5-5", isSidechain=True),  # a subagent's own turn
        {"type": "assistant", "isApiErrorMessage": True, "message": {"model": "<synthetic>"}},
        {"type": "system", "subtype": "stop_hook_summary"},
    )
    assert transcripts.latest_model(path) == "claude-opus-5-5"
