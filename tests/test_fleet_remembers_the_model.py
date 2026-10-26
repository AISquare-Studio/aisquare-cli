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
import uuid
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
    """The owner's built-in ``/model`` or ``/effort``, as Claude Code records it (the frame of
    ``tests/fixtures/claude_code/model_commands_2.1.311.jsonl``): the caveat, the command its
    child, the answer the command's child."""
    caveat, command, answer = (str(uuid.uuid4()) for _ in range(3))
    _write(
        _transcript(project),
        {"type": "user", "uuid": caveat, "isMeta": True,
         "message": {"role": "user", "content": "<local-command-caveat>The command below…"}},
        {"type": "user", "uuid": command, "parentUuid": caveat,
         "message": {"role": "user", "content": f"<command-name>/{setting}</command-name>\n"
                     f"            <command-message>{setting}</command-message>\n"
                     f"            <command-args>{args}</command-args>"}},
        {"type": "user", "uuid": answer, "parentUuid": command,
         "message": {"role": "user",
                     "content": f"<local-command-stdout>{stdout}</local-command-stdout>"}},
    )  # fmt: skip


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


FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
"""What real Claude Code recorded, probed on a scratch config dir (round 6 of #441, seq
3628): ``model_commands_2.1.310.jsonl`` and ``model_commands_2.1.311.jsonl``. Each holds a
plain turn whose Stop hook blocked once with a reason that QUOTES a ``/model`` record (as a
board note does), a typed prompt quoting one, ``/quick`` (``model: haiku``), ``/model haiku``,
``/effort low``, the ``/model`` picker's ``Default (recommended)`` and its Esc (``Kept model as``,
``system`` ``local_command`` records), the ``/effort`` picker's Esc (``Cancelled``); 2.1.310 the
session-only picks, 2.1.311 a ``/compact`` and its summary (``isCompactSummary``). Trimmed to
the fields the reader anchors on (``uuid``, ``parentUuid``, ``isMeta``, ``isCompactSummary``,
``version``), every content string as written."""


def _events(name: str) -> list[str | tuple[str, str, str]]:
    scan = transcripts.commands_since(FIXTURES / name, 0)
    assert scan is not None and scan.end == (FIXTURES / name).stat().st_size
    return [
        event if isinstance(event, str) else (event.setting, event.args, event.stdout)
        for event in scan.events
    ]


SET = "Set model to `{}` and saved as your default for new sessions"


def test_only_claude_codes_own_frame_is_a_command_on_2_1_311() -> None:
    """The caveat, then the command its child, then the answer its child (or the picker's
    ``system`` pair): nothing else, though the Stop hook's feedback, a typed prompt and the
    compact summary all quote a ``/model`` record."""
    events = _events("model_commands_2.1.311.jsonl")
    commands = [event for event in events if not isinstance(event, str)]
    assert [event for event in events if isinstance(event, str)] == [
        "plain", "plain", "command", "plain", "command", "plain",
    ]  # fmt: skip
    assert [(setting, args) for setting, args, _ in commands] == [
        ("model", "haiku"), ("effort", "low"), ("model", ""), ("effort", ""),
        ("model", ""), ("model", ""),
    ]  # fmt: skip
    answers = [answer for _, _, answer in commands]
    assert answers[0] == SET.format("Haiku 5.5")
    assert answers[2] == SET.format("Sonnet 5.5")
    assert answers[3] == "Cancelled"
    assert answers[4] == SET.format("Opus 5.5 (default)")
    assert answers[5] == "Kept model as `Opus 5.5 (default)`"


def test_only_claude_codes_own_frame_is_a_command_on_2_1_310() -> None:
    events = _events("model_commands_2.1.310.jsonl")
    commands = [event for event in events if not isinstance(event, str)]
    assert [(setting, args) for setting, args, _ in commands] == [
        ("model", "haiku"), ("effort", "low"), ("model", ""), ("model", ""), ("effort", ""),
        ("model", ""), ("effort", ""),
    ]  # fmt: skip
    answers = [answer for _, _, answer in commands]
    assert answers[2] == SET.format("Opus 5.5 (default)")
    assert answers[3] == "Kept model as `Opus 5.5 (default)`"
    assert answers[4] == "Cancelled"
    assert str(answers[5]).endswith("for this session only")
    assert str(answers[6]).startswith("Set effort level to medium (this session only)")


def test_a_commands_value_comes_from_its_args_else_what_claude_code_answered() -> None:
    """Round 6 decisions (seq 3628): ``Kept model as`` (the picker's Esc) sets nothing, nor
    does ``Cancelled``; ``Default (recommended)`` forgets the model (``""``: the account's
    default from then on); "this session only" moves the agent and is no pick."""
    value = model_memory.command_value

    def command(setting: str, args: str, answer: str) -> transcripts.SettingCommand:
        return transcripts.SettingCommand(setting, args, answer, "2.1.311")

    assert value(command("model", "haiku", SET.format("Haiku 5.5"))) == model_memory.CommandValue(
        "model", "haiku", True
    )
    assert value(command("model", "", SET.format("Sonnet 5.5"))) == model_memory.CommandValue(
        "model", "claude-sonnet-5-5", True
    )
    assert value(command("model", "", SET.format("Opus 5.5 (default)"))) == (
        model_memory.CommandValue("model", "", True)
    )
    assert value(command("model", "", "Kept model as `Opus 5.5 (default)`")) is None
    assert value(command("model", "", "Kept model as `Fable 5.1`")) is None
    assert value(command("effort", "", "Cancelled")) is None
    assert value(
        command("model", "", "Set model to `Sonnet 5.5` for this session only")
    ) == model_memory.CommandValue("model", "claude-sonnet-5-5", False)
    assert value(
        command("effort", "", "Set effort level to medium (this session only): Balanced")
    ) == model_memory.CommandValue("effort", "medium", False)
    assert value(
        command("effort", "low", "Set effort level to low (saved as your default for new …")
    ) == model_memory.CommandValue("effort", "low", True)


def test_an_answer_this_version_cannot_read_is_said_and_explicit_args_still_count() -> None:
    """Round 6 minor 3: a reworded answer must not fail silently. It is unread
    (``understood=False``: the canary), and only the command's own args are a value."""
    value = model_memory.command_value
    reworded = transcripts.SettingCommand("model", "haiku", "Model set to Haiku 5.5", "2.2.0")
    assert value(reworded) == model_memory.CommandValue("model", "haiku", True, understood=False)
    bare = transcripts.SettingCommand("model", "", "Model set to Haiku 5.5", "2.2.0")
    assert value(bare) == model_memory.CommandValue("model", None, True, understood=False)


def _framed(path: Path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_a_command_with_no_answer_yet_is_none_and_never_holds_the_read(tmp_path: Path) -> None:
    """Round 6 minor 4: a command whose answer is not written yet never pinned the offset
    there (every Stop re-read the transcript from it). The read moves on past it; a command
    with no answer is no command."""
    path = tmp_path / "t.jsonl"
    _framed(
        path,
        {"type": "user", "uuid": "c1", "isMeta": True,
         "message": {"role": "user", "content": "<local-command-caveat>The command below…"}},
        {"type": "user", "uuid": "m1", "parentUuid": "c1",
         "message": {"role": "user", "content": "<command-name>/model</command-name>\n"
                     "            <command-args>fable</command-args>"}},
    )  # fmt: skip
    scan = transcripts.commands_since(path, 0)
    assert scan is not None and scan.events == () and scan.end == path.stat().st_size


def test_a_prompt_that_mentions_a_tool_result_is_a_prompt(tmp_path: Path) -> None:
    """Round 6 minor 6: the tool-result filter skipped every line that said ``tool_result``."""
    path = tmp_path / "t.jsonl"
    prompt = {"role": "user", "content": "why is the tool_result empty?"}
    _framed(path, {"type": "user", "uuid": "p1", "message": prompt})
    scan = transcripts.commands_since(path, 0)
    assert scan is not None and scan.events == ("plain",)


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
