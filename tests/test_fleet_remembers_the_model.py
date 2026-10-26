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
from aisquare.core.config import FleetRoleSettings, load_config, save_config
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


def _prompt(text: str = "carry on") -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _typed(project: ProjectInfo, setting: str, args: str, stdout: str) -> None:
    """The owner's built-in ``/model`` or ``/effort``, as Claude Code 2.1.310 records it
    (``tests/fixtures/claude_code/model_commands_2.1.310.jsonl``)."""
    caveat = "<local-command-caveat>The command below was run directly in Claude Code…"
    name = (
        f"<command-name>/{setting}</command-name>\n            <command-message>{setting}"
        f"</command-message>\n            <command-args>{args}</command-args>"
    )
    _write(
        _transcript(project),
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": caveat}},
        {"type": "user", "message": {"role": "user", "content": name}},
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": f"<local-command-stdout>{stdout}</local-command-stdout>",
            },
        },
    )


SAVED = "and saved as your default for new sessions"


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
    """The agent started on fable at high; the owner's ``/model`` and ``/effort``, which
    Claude Code records, moved it to opus at xhigh. Its next turn end reads them, the pick
    every later spawn runs and what a restart runs, whatever the agent's role (``agent`` is
    the plain one)."""
    _manager(work, role=role, effort=None)
    _write(_transcript(work), _prompt(), _reply("claude-fable-5-1"))
    _stop(work, monkeypatch, effort="high")
    assert model_memory.remembered() == model_memory.Pick()
    _typed(work, "model", "claude-opus-5-5", f"Set model to `Opus 5.5` {SAVED}")
    _typed(work, "effort", "xhigh", "Set effort level to xhigh (saved as your default …): …")
    _write(_transcript(work), _prompt(), _reply("claude-opus-5-5"))
    _stop(work, monkeypatch, effort="xhigh")
    assert model_memory.remembered() == model_memory.Pick("claude-opus-5-5", "xhigh")
    session = _session(work)
    assert (session.model, session.effort) == ("claude-opus-5-5", "xhigh")


def test_the_payloads_own_effort_is_read_before_the_environments(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code reports ultracode as xhigh in ``CLAUDE_EFFORT``; its payload names it. A
    Stop's effort is a reading (the board's, a launch's baseline), never a pick by itself."""
    _manager(work, effort=None)
    _write(_transcript(work), _prompt(), _reply("claude-fable-5-1"))
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
    assert _session(work).effort == "ultracode"
    assert model_memory.remembered() == model_memory.Pick()


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
    _write(_transcript(work), _prompt(), _reply("claude-opus-5-5"))
    _stop(work, monkeypatch, effort="high")
    assert model_memory.remembered() == model_memory.Pick()
    assert (_session(work).model, _session(work).effort) == ("claude-opus-5-5", "high")

    _typed(work, "model", "claude-fable-5-1", f"Set model to `Fable 5.1` {SAVED}")
    _typed(work, "effort", "max", "Set effort level to max (saved as your default …): …")
    _write(_transcript(work), _prompt(), _reply("claude-fable-5-1"))
    _stop(work, monkeypatch, effort="max")
    assert model_memory.remembered() == model_memory.Pick("claude-fable-5-1", "max")


def test_an_alias_is_only_the_id_it_resolved_to() -> None:
    """Seq 3424 (1): ``opus`` was ANY opus id. Two readings are one model only when they
    name the same id, a ``[1m]`` suffix aside; ``opusplan`` runs both opus and sonnet."""
    from aisquare.core import harness

    assert harness.same_reading("claude-opus-5-5", "claude-opus-5-5[1m]")
    assert not harness.same_reading("claude-opus-5-5", "claude-opus-4-5")
    assert harness.runs_under("opusplan", "claude-sonnet-5-5")
    assert not harness.runs_under("opusplan", "claude-fable-5-1")
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
    _typed(work, "model", "claude-opus-4-5", f"Set model to `Opus 4.5` {SAVED}")
    _write(_transcript(work), _prompt(), _reply("claude-opus-4-5"))
    _hook("stop", work, monkeypatch, effort="xhigh")
    assert model_memory.remembered() == model_memory.Pick("claude-opus-4-5", None)


@pytest.mark.parametrize(
    ("role", "spawned_by"), [("tester", "ses_manager_1"), ("validator", "user")]
)
def test_an_inherited_claude_effort_is_never_the_owners_effort(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch, role: str, spawned_by: str
) -> None:
    """Seq 3513 (1): Claude Code 2.1.310's SessionStart carries the model and no effort, and
    its hooks there see the process's inherited environment: a fleet window inherits the
    tmux server's ``CLAUDE_EFFORT``, from whatever process started it. Read as the launch's
    effort, the agent's first Stop at its own ``--effort low`` (a manager's ``--as`` spawn,
    or a role pinned in its settings) looked like a ``/effort`` and became the pick."""
    launched = LaunchSpec(binary="claude", extra_args=["--effort", "low"])
    _manager(work, role=role, model=None, effort=None, spec=launched)
    with store_session() as store:
        row = store.get_fleet_agent(AGENT)
        assert row is not None
        store.upsert_fleet_agent(row.model_copy(update={"spawned_by": spawned_by}))
    model_memory.remember(model="opus", effort="xhigh")
    monkeypatch.setenv("AISQUARE_ROLE", role)
    _hook(
        "session-start",
        work,
        monkeypatch,
        effort="xhigh",
        source="startup",
        model="claude-opus-5-5",
    )
    _write(_transcript(work), _reply("claude-opus-5-5"))
    _hook("stop", work, monkeypatch, effort="low")
    assert model_memory.remembered() == model_memory.Pick("opus", "xhigh")


def test_an_effort_drift_is_never_a_pick_and_a_recorded_effort_is_even_in_the_first_turn(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seq 3513 and 3609: a Stop's effort says what the turn ran, which a command's
    ``effort:`` frontmatter changes for one turn; only Claude Code's record of ``/effort`` is
    the owner's. So no drift is a pick, and a recorded ``/effort`` is, in the very first
    turn too (no first-turn gap, as there was while a drift was the evidence)."""
    _manager(work, model=None, effort=None)
    monkeypatch.setenv("AISQUARE_ROLE", "manager")
    _hook(
        "session-start",
        work,
        monkeypatch,
        effort="xhigh",
        source="startup",
        model="claude-fable-5-1",
    )
    _typed(work, "effort", "low", "Set effort level to low (saved as your default …): …")
    _write(_transcript(work), _prompt(), _reply("claude-fable-5-1"))
    _hook("stop", work, monkeypatch, effort="low")
    assert model_memory.remembered() == model_memory.Pick(None, "low")
    _write(_transcript(work), _prompt(), _reply("claude-fable-5-1"))
    _hook("stop", work, monkeypatch, effort="max")
    assert model_memory.remembered() == model_memory.Pick(None, "low"), "a drift"


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


def test_a_model_the_owner_chose_for_an_agent_is_never_off_its_ladder(
    work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #441 (6), seq 3427: the chip stands down for the pick, the agent's own
    spawn args and its role's settings: every one of them is the owner's choice."""
    from aisquare.services import team as team_service

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv(FLEET_AGENT_ENV_VAR, AGENT)
    launched = LaunchSpec(binary="claude", extra_args=["--model", "haiku"])
    _manager(work, role="coder", model=None, effort=None, spec=launched)
    board = team_service.hook_session_start(SESSION, work.root, "startup", model="claude-haiku-4-5")
    assert "[claude-haiku-4-5]" in board and "off-ladder" not in board, "its spawn's args"

    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(extra_args=["--model", "fable"])
    save_config(config)
    board = team_service.hook_session_start(SESSION, work.root, "resume", model="claude-fable-5-1")
    assert "off-ladder" not in board, "its role's settings"
    board = team_service.hook_session_start(SESSION, work.root, "resume", model="claude-oops-1")
    assert "⚠ off-ladder" in board, "nobody chose that one"


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


FIXTURE = Path(__file__).parent / "fixtures" / "claude_code" / "model_commands_2.1.310.jsonl"
"""What real Claude Code 2.1.310 recorded (round 4 reopen, seq 3609; probed on a scratch
config dir): plain turns; ``/quick`` (a command with ``model: haiku``, replied as haiku),
``/deep`` (``effort: low``) and ``/peff`` (a skill with ``effort: low``); ``/model haiku``,
``/effort low`` twice, the ``/model`` picker saved and session-only, ``/model sonnet`` twice,
``/model claude-opus-5-5``, the ``/effort`` picker saved, session-only and cancelled, and the
``/model`` picker's Esc (``Kept model as``, a ``system`` ``local_command`` record). Trimmed to
the fields the reader takes; every content string as written."""


def _setting(setting: str, args: str, stdout: str) -> tuple[str, str, str]:
    return (setting, args, stdout)


def test_the_commands_claude_code_2_1_310_records_are_read_in_order() -> None:
    scan = transcripts.commands_since(FIXTURE, 0)
    assert scan is not None and scan.end == FIXTURE.stat().st_size
    seen = [
        event if isinstance(event, str) else (event.setting, event.args, event.stdout or "")
        for event in scan.events
    ]
    saved = "and saved as your default for new sessions"
    low = "Set effort level to low (saved as your default for new sessions): Quick, "
    assert [event if isinstance(event, str) else event[:2] for event in seen] == [
        "plain", "command", "plain", "command", "command", "plain",
        ("model", "haiku"), ("effort", "low"), ("effort", "low"),
        "plain",
        ("model", ""), ("model", ""), ("model", "sonnet"), ("model", "sonnet"),
        ("model", "claude-opus-5-5"), ("effort", ""), ("effort", ""), ("effort", ""),
        ("model", ""),
        "plain",
    ]  # fmt: skip
    commands = [event for event in seen if not isinstance(event, str)]
    assert commands[0][2] == f"Set model to `Haiku 5.5` {saved}"
    assert commands[1][2].startswith(low)
    assert commands[4][2] == "Set model to `Sonnet 5.5` for this session only"
    assert commands[10][2] == "Cancelled"
    assert commands[11][2] == "Kept model as `Opus 5.5`"
    # A read from where the last one ended sees nothing again.
    again = transcripts.commands_since(FIXTURE, scan.end)
    assert again is not None and again.events == ()


def test_a_commands_value_comes_from_its_args_else_what_claude_code_answered() -> None:
    scan = transcripts.commands_since(FIXTURE, 0)
    assert scan is not None
    values = [
        model_memory.command_value(event)
        for event in scan.events
        if isinstance(event, transcripts.SettingCommand)
    ]
    pick, live = True, False
    assert values == [
        model_memory.CommandValue("model", "haiku", pick),
        model_memory.CommandValue("effort", "low", pick),
        model_memory.CommandValue("effort", "low", pick),
        model_memory.CommandValue("model", "claude-haiku-5-5", pick),  # the picker, saved
        model_memory.CommandValue("model", "claude-sonnet-5-5", live),  # this session only
        model_memory.CommandValue("model", "sonnet", pick),
        model_memory.CommandValue("model", "sonnet", pick),
        model_memory.CommandValue("model", "claude-opus-5-5", pick),
        model_memory.CommandValue("effort", "high", pick),
        model_memory.CommandValue("effort", "medium", live),
        None,  # Cancelled
        model_memory.CommandValue("model", "claude-opus-5-5", pick),  # Kept model as
    ]
    unnamed = transcripts.SettingCommand(
        "model",
        "",
        "Set model to `Default (recommended)` and saved as your default for new sessions",
    )
    assert model_memory.command_value(unnamed) == model_memory.CommandValue("model", None, pick)
    refused = transcripts.SettingCommand("model", "nope", "Invalid model: nope")
    assert model_memory.command_value(refused) is None


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
