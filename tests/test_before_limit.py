"""F2 of #400 — move a fleet agent to another account BEFORE it hits a usage limit.

``[accounts] before_limit`` (``off`` by default; ``warn``; ``switch``) acts at the two safe
points an agent has, its turn end (the ``Stop`` hook) and Claude Code's idle notice, never
mid-turn and never while its turn left background work running. The hook asks the usage
endpoint NOTHING: it decides from what the cache has on record whether the agent is worth a
look, and a detached worker (``hook before-limit``) does the reads that may fetch — the
agent's account five minutes deep, the others a minute deep — then warns or moves it. Usage is
scripted (``_Usage`` counts every request); the switch itself is ``fleet switch``'s,
exercised in ``tests/test_fleet_service.py`` — here the decision, the worker's call, the
checks around the stop, and what the board is told.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness, selfcli, transcripts
from aisquare.core.config import AccountsSettings, AppConfig, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, LaunchSpec, ProjectInfo, TeamSession
from aisquare.services import claude_accounts as service
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
from aisquare.services import settings as settings_service
from aisquare.services import team as team_service
from tests.test_claude_accounts import NOW
from tests.test_usage_aware_accounts import _payload, _slot, _Usage

SESSION = "sess-busy"
LABEL = "coder-db"
AN_HOUR_AGO = timedelta(hours=1)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    project = team_project(root)
    with store_session() as store:
        store.onboard_project(project)
    return project


@pytest.fixture(autouse=True)
def settled(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """The move's wait for the turn end to settle, recorded and not slept, as the moment it
    would end: the one test of the wait reads it, every other test is not held five seconds."""
    ends: list[datetime] = []
    monkeypatch.setattr(
        hooks_service,
        "_sleep",
        lambda seconds: ends.append(datetime.now(tz=UTC) + timedelta(seconds=seconds)),
    )
    return ends


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every worker the hooks start. Nothing is read, switched or warned inline."""
    workers: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", lambda argv: workers.append(list(argv)))
    return workers


@pytest.fixture
def switched(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every ``fleet switch`` the worker asks for, with its arguments; its two checks are
    asked as the real switch asks them, ``before_stop`` then ``after_mark``."""
    calls: list[dict[str, Any]] = []

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        calls.append({"label": label, **kwargs})
        if kwargs.get("before_stop") is not None:
            kwargs["before_stop"]()
        if kwargs.get("after_mark") is not None:
            kwargs["after_mark"]()
        return SimpleNamespace(failures=[])

    monkeypatch.setattr(fleet_service, "switch", switch)
    monkeypatch.setattr(hooks_service, "_pane_shows", lambda agent: "idle")
    return calls


@pytest.fixture
def nudged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    woken: list[str] = []
    monkeypatch.setattr(
        team_service,
        "_nudge_manager",
        lambda project_id, *, reason, board: woken.append(reason),
    )
    return woken


@pytest.fixture
def at(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Move the accounts code's clock to ``NOW`` plus some minutes."""

    def move(minutes: float) -> None:
        moment = NOW + timedelta(minutes=minutes)
        monkeypatch.setattr(service, "_now", lambda: moment)

    return move


def _accounts(
    monkeypatch: pytest.MonkeyPatch, *, work: dict[str, Any] | int, home: dict[str, Any] | int
) -> _Usage:
    """Slot 2 (``work``, the agent's) and slot 3 (``home``) signed in, with a counting endpoint."""
    _slot("work@example.com", "tok-work")
    _slot("home@example.com", "tok-home")
    endpoint = _Usage({"tok-work": work, "tok-home": home})
    monkeypatch.setattr(service, "_http_get", endpoint)
    return endpoint


def _on_record(endpoint: _Usage) -> None:
    """Put every account's reading on record, as the Accounts page or a launch would, and
    forget the requests that took: what is counted after is what the hooks cost."""
    service.cached_usage(service.list_accounts())
    endpoint.calls.clear()


def _settings(**overrides: Any) -> None:
    config: AppConfig = load_config()
    config.accounts = AccountsSettings(**overrides)
    save_config(config)


def _transcript(project: ProjectInfo) -> Path:
    return project.root / f"{SESSION}.jsonl"


def _write(path: Path, *records: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _agent(
    project: ProjectInfo,
    *,
    state: str = "waiting",
    age: timedelta = AN_HOUR_AGO,
    pinned: bool = False,
    role: str = "coder",
    spawned_by: str | None = None,
    agent_id: str = "agt_busy1",
) -> FleetAgent:
    """A fleet agent on slot 2 whose board row is ``state``, started ``age`` ago, with a
    transcript on disk. A row of another ``agent_id`` replaces the live one (an upsert keeps a
    row's ``created_at``)."""
    now = datetime.now(tz=UTC)
    transcript = _transcript(project)
    if not transcript.exists():
        _write(transcript, {"type": "user", "message": {"role": "user", "content": "go"}})
    with store_session() as store:
        live = store.fleet_agent_for_session(SESSION)
        if live is not None and live.id != agent_id:
            store.end_fleet_agent(live.id, ended_by="stopped")
        store.upsert_session(
            TeamSession(
                id=SESSION,
                project_id=project.id,
                role=role,
                started_at=now - age,
                last_seen_at=now,
                transcript_path=str(transcript),
            )
        )
        store.touch_session(SESSION, state=state)
        agent = FleetAgent(
            id=agent_id,
            project_id=project.id,
            label=LABEL,
            role=role,
            pane_id="%3",
            session_id=SESSION,
            cwd=project.root,
            account_slot=2,
            spawned_by=spawned_by,
            launch_spec=LaunchSpec(binary="claude", account_slot=2, account_pinned=pinned),
            created_at=now - age,
        )
        store.upsert_fleet_agent(agent)
    return agent


def _stamp(at: datetime | None = None) -> str:
    return (at or datetime.now(tz=UTC)).isoformat().replace("+00:00", "Z")


_RESULTS: dict[str, Any] = {
    "bash": lambda task: {"stdout": "", "stderr": "", "backgroundTaskId": task},
    "subagent": lambda task: {"isAsync": True, "status": "async_launched", "agentId": task},
    "monitor": lambda task: {"taskId": task, "timeoutMs": 1800000, "persistent": False},
    "forked skill": lambda task: {
        "status": "forked",
        "background": True,
        "agentId": task,
        "result": "Running in the background as @code-review",
    },
}
"""Each background kind's ``toolUseResult``, as this machine's transcripts hold them (Claude
Code 2.1.304 to 2.1.310; review of #423, round 2)."""


def _launch(project: ProjectInfo, kind: str, task: str, *, at: datetime | None = None) -> None:
    """A background launch in the transcript, as Claude Code writes one (:data:`_RESULTS`)."""
    result = _RESULTS[kind](task)
    text = f"Running in the background: {task}."
    _write(
        _transcript(project),
        {
            "type": "user",
            "timestamp": _stamp(at),
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}],
            },
            "toolUseResult": result,
        },
    )


def _completes(project: ProjectInfo, task: str, *, status: str | None = "completed") -> None:
    """Its ``<task-notification>``, queued and taken as Claude Code queues and takes one: the
    final one with a ``<status>``, a Monitor's event without (``status=None``)."""
    said = f"\n<status>{status}</status>" if status else "\n<event>a line matched</event>"
    notice = f"<task-notification>\n<task-id>{task}</task-id>{said}"
    _write(
        _transcript(project),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": _stamp(),
            "content": notice,
        },
        {"type": "queue-operation", "operation": "dequeue", "timestamp": _stamp()},
    )


def _taken(project: ProjectInfo, text: str = "and the docs too") -> None:
    """A message queued and taken after the turn end — a task's notice, or a prompt in ``-p``
    — whose ``enqueue`` and ``dequeue`` Claude Code writes at once, before any hook runs. A
    typed prompt's own record comes only once its ``UserPromptSubmit`` hook has passed."""
    _write(
        _transcript(project),
        {"type": "queue-operation", "operation": "enqueue", "timestamp": _stamp(), "content": text},
        {"type": "queue-operation", "operation": "dequeue", "timestamp": _stamp()},
    )


def _turn_end_record(project: ProjectInfo) -> None:
    """What Claude Code writes once a turn's Stop hooks have run."""
    _write(
        _transcript(project),
        {"type": "system", "subtype": "stop_hook_summary", "timestamp": _stamp()},
        {"type": "system", "subtype": "turn_duration", "timestamp": _stamp()},
    )


def _claim(project: ProjectInfo, at: datetime) -> None:
    """The turn end a hook claims, as it claims it."""
    size = _transcript(project).stat().st_size
    value = json.dumps(
        {"at": at.isoformat(), "transcript": str(_transcript(project)), "offset": size}
    )
    with store_session() as store:
        store.set_ui_state(f"before-limit:{project.id}:{LABEL}", value)


def _stop(runner: CliRunner, project: ProjectInfo) -> str:
    payload = json.dumps(
        {
            "session_id": SESSION,
            "cwd": str(project.root),
            "transcript_path": str(_transcript(project)),
        }
    )
    done = runner.invoke(app, ["hook", "stop"], input=payload)
    assert done.exit_code == 0, done.output
    return done.stdout


def _notify(runner: CliRunner, project: ProjectInfo, kind: str | None, message: str) -> None:
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "cwd": str(project.root),
        "message": message,
        "transcript_path": str(_transcript(project)),
    }
    if kind is not None:
        payload["notification_type"] = kind
    assert runner.invoke(app, ["hook", "notification"], input=json.dumps(payload)).exit_code == 0


def _work(runner: CliRunner) -> None:
    """Run the worker a hook started (``aisquare hook before-limit``)."""
    done = runner.invoke(app, ["hook", "before-limit", SESSION])
    assert done.exit_code == 0 and done.stdout == "", done.output


def _events(project: ProjectInfo) -> list[tuple[str, str]]:
    with store_session() as store:
        return [(e.kind, e.text) for e in store.recent_events(project.id, limit=50)]


def _worker() -> list[str]:
    return [*selfcli.argv_for([]), "--quiet", "hook", "before-limit", SESSION]


def _cool_down(project: ProjectInfo) -> str | None:
    with store_session() as store:
        return store.ui_state(f"before-limit:{project.id}:{LABEL}")


def _forget_the_cool_down(project: ProjectInfo) -> None:
    with store_session() as store:
        store.set_ui_state(f"before-limit:{project.id}:{LABEL}", None)


def _state() -> str:
    with store_session() as store:
        session = store.get_session(SESSION)
    assert session is not None
    return session.state


# --------------------------------------------------------------------------- the setting


def test_before_limit_is_off_by_default_and_then_reads_nothing(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """Off for one release (the plan's rule): a turn end over the line asks nothing at all."""
    assert AccountsSettings().before_limit == "off"
    assert load_config().accounts.before_limit == "off"
    for value in ("warn", "switch"):
        assert AccountsSettings(before_limit=value).before_limit == value
    with pytest.raises(ValidationError):
        AccountsSettings(before_limit="early")
    endpoint = _accounts(monkeypatch, work=_payload(95), home=_payload(10))
    _agent(work)

    _stop(runner, work)

    assert started == [] and endpoint.calls == []
    assert not [kind for kind, _ in _events(work) if kind == team_service.BEFORE_LIMIT_KIND]


# --------------------------------------------------------------------------- the hook asks nothing


def test_a_turn_end_asks_the_endpoint_nothing_and_leaves_the_reads_to_the_worker(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    nudged: list[str],
) -> None:
    """Nothing on record: the hook cannot tell, so it starts the worker and reads nothing —
    a hung endpoint never holds a turn end. The worker reads the agent's account, then the
    one it would go to, and moves it; nobody is woken for a move that goes ahead."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    agent = _agent(work)

    _stop(runner, work)

    assert started == [_worker()] and endpoint.calls == []
    _work(runner)
    assert endpoint.calls == ["tok-work", "tok-home"]
    [call] = switched
    assert call["label"] == LABEL and call["reason"] == "before limit"
    assert call["automatic"] is True and call["spawned_by"] == hooks_service.HANDOVER_SPAWNER
    assert call["agent_id"] == agent.id
    assert call["max_age"] == service.PICK_MAX_AGE_SECONDS
    assert nudged == []
    assert not [kind for kind, _ in _events(work) if kind == "handover_failed"]


def test_with_the_cache_warm_the_hook_decides_and_the_move_reads_nothing_more(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
) -> None:
    """Every account on record (the Accounts page read them): the turn end starts the worker
    on the cache's word, and the worker's reads and the move's pick are all served by it."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)

    _stop(runner, work)
    _work(runner)

    assert started == [_worker()] and len(switched) == 1
    assert endpoint.calls == []


def test_with_no_account_under_the_line_turn_ends_read_nothing_however_many(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    nudged: list[str],
    at: Callable[[float], None],
) -> None:
    """m1 of #423's review: every other account over the line, every turn end re-read each of
    them once a minute, for as long as the week lasted. On record, the hook knows and starts
    nothing; off record, the worker reads them once and keeps its cool-down, so the next
    turn ends read nothing either. No move, no word, nobody woken: no limit was hit."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(90))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)

    for minute in range(5):
        at(minute)
        _stop(runner, work)
    assert started == [] and endpoint.calls == []

    at(6)  # off record: one look, by the worker
    _stop(runner, work)
    _work(runner)
    assert len(started) == 1 and endpoint.calls == ["tok-work", "tok-home"]
    for minute in (7, 8, 9):
        at(minute)
        _stop(runner, work)
    assert len(started) == 1 and endpoint.calls == ["tok-work", "tok-home"]

    # The other account cannot be read: only the worker can say, and it keeps the
    # cool-down when it finds no room, so the next turn ends start nothing.
    endpoint.by_token["tok-home"] = 500
    at(20)
    _forget_the_cool_down(work)  # the cool-down runs on the wall clock: say it has passed
    _stop(runner, work)
    _work(runner)
    assert len(started) == 2
    for minute in (21, 22, 23):
        at(minute)
        _stop(runner, work)
    assert len(started) == 2
    assert switched == [] and nudged == []
    assert not [k for k, _ in _events(work) if k in {"handover_failed", "before_limit"}]


def test_turn_ends_ask_the_endpoint_no_more_often_than_the_cache_does(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    at: Callable[[float], None],
) -> None:
    """Under the line: the first turn end starts one worker, which reads the account once and
    gives the cool-down back; every turn end in the next five minutes decides from that
    reading, starting nothing. Past the five minutes, one look again."""
    endpoint = _accounts(monkeypatch, work=_payload(50), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    _stop(runner, work)
    _work(runner)
    assert endpoint.calls == ["tok-work"] and _cool_down(work) is None
    for minute in range(1, 5):
        at(minute)
        _stop(runner, work)
    assert len(started) == 1 and endpoint.calls == ["tok-work"]

    at(6)
    _stop(runner, work)
    _work(runner)
    assert len(started) == 2 and endpoint.calls == ["tok-work", "tok-work"]


def test_a_backed_off_account_is_not_asked_and_moves_nobody(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
) -> None:
    """A 429 starts a wait every process honours (F1 of #400): the wait's reading is kept, not
    live, and moves no agent; the worker keeps its cool-down, and no later look asks again
    while the wait runs."""
    endpoint = _accounts(monkeypatch, work=429, home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    _stop(runner, work)
    _work(runner)
    assert endpoint.calls == ["tok-work"] and _cool_down(work) is not None
    _forget_the_cool_down(work)
    _stop(runner, work)
    _work(runner)

    assert endpoint.calls == ["tok-work"] and switched == []


def test_a_kept_reading_is_never_taken_for_a_live_one(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    at: Callable[[float], None],
) -> None:
    """The last good figure a failed read shows, 92 % ten minutes ago, is of unknown age now:
    the page shows it, aged; it moves nobody (#408)."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    at(-10)
    [work_slot] = [a for a in service.list_accounts() if a.slot == 2]
    service.cached_usage([work_slot])  # recorded ten minutes ago
    at(0)
    endpoint.by_token["tok-work"] = 500  # and now the read fails
    _agent(work)

    _stop(runner, work)
    _work(runner)

    shown = service.cached_usage([work_slot])[2]
    assert shown.kept and shown.session_percent == 92  # the premise: kept, and over the line
    assert len(started) == 1 and switched == []


# --------------------------------------------------------------------------- safe points


def test_only_a_turn_that_is_over_and_the_idle_notice_are_looked_at(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two doors, each pinned: a Stop whose decision keeps the turn going (a note
    addressed to the agent, P0-W2) is no safe point and asks nothing; a plain Stop is one. A
    permission prompt comes mid-turn; the idle notice comes a minute into a pause — with or
    without its type, as an older Claude Code sends it."""
    _settings(before_limit="switch")
    _agent(work)
    looked: list[str] = []
    monkeypatch.setattr(
        hooks_service,
        "check_before_limit",
        lambda session_id, *, transcript_path=None: looked.append(session_id),
    )

    team_service.add_note("check the migration first", to_role=LABEL, project_id=work.id)
    assert '"decision": "block"' in _stop(runner, work)
    assert looked == []
    assert _stop(runner, work) == ""
    assert looked == [SESSION]

    _notify(runner, work, "permission_prompt", "Claude needs your permission to use Bash")
    assert looked == [SESSION]
    _notify(runner, work, "idle_prompt", "Claude is waiting for your input")
    _notify(runner, work, None, "Claude is waiting for your input")
    assert looked == [SESSION, SESSION, SESSION]


def test_the_idle_notice_starts_the_worker_and_a_prompt_mid_turn_does_not(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    _notify(runner, work, "permission_prompt", "Claude needs your permission to use Bash")
    assert started == []
    _agent(work)  # answered, and its turn ended: waiting again
    _notify(runner, work, "idle_prompt", "Claude is waiting for your input")
    assert started == [_worker()]


@pytest.mark.parametrize("state", ["working", "attention", "limited", "switching"])
def test_an_agent_that_is_not_waiting_at_its_prompt_is_never_moved(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    state: str,
) -> None:
    """Mid-turn (``working``), asking (``attention``), parked on a limit (``on_limit``'s), or
    already being moved: an idle notice then starts nothing, and a worker started before it
    moves nothing."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work, state=state)

    _notify(runner, work, "idle_prompt", "Claude is waiting for your input")
    _work(runner)

    assert started == [] and switched == [] and endpoint.calls == []


@pytest.mark.parametrize("kind", sorted(_RESULTS))
def test_background_work_a_turn_left_running_is_no_safe_point_until_it_reports(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    kind: str,
) -> None:
    """M1 of #423's review, every kind (R2-M1): a turn can end with a background Bash, a
    ``Monitor``, a background subagent or a forked skill still running, in the agent's own
    process; the ``/exit`` of a move kills it, and the resumed conversation waits on a notice
    that never comes. Until the transcript shows its final ``<task-notification>`` — the one
    with a ``<status>``: a Monitor's events carry none — no turn end looks (and spends no
    cool-down); after it, the next one does. A launch from before the row started — another
    process's, resumed — is not running work."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    agent = _agent(work)
    _on_record(endpoint)
    _launch(work, kind, "bk1")

    _stop(runner, work)
    _notify(runner, work, "idle_prompt", "Claude is waiting for your input")
    _completes(work, "bk1", status=None)  # an event, as a Monitor sends: it runs on
    _stop(runner, work)
    assert started == [] and _cool_down(work) is None

    _completes(work, "bk1", status="stopped" if kind == "monitor" else "completed")
    _launch(work, kind, "bk0", at=agent.created_at - timedelta(minutes=1))  # a resumed one's
    _stop(runner, work)
    assert started == [_worker()]
    assert transcripts.background_running(_transcript(work), since=agent.created_at) == []


def test_background_work_started_after_the_hook_looked_stops_the_move(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
) -> None:
    """The worker's checks around the stop ask again: work launched between the turn end and
    the move stops it before anything is marked or stopped — no failure, nobody woken, the
    cool-down given back."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    _stop(runner, work)
    asked: list[str] = []

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        asked.append(label)
        _launch(work, "bash", "late1")  # a notice started a turn, which launched it
        kwargs["before_stop"]()
        raise AssertionError("stopped background work")

    monkeypatch.setattr(fleet_service, "switch", switch)
    _work(runner)

    assert asked == [LABEL]
    assert nudged == [] and _cool_down(work) is None
    assert not [kind for kind, _ in _events(work) if kind == "handover_failed"]


def test_a_prompt_that_lands_after_the_first_check_is_caught_after_the_mark(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
) -> None:
    """m3 of #423's review: the hand-over mark hides a turn's ``working`` (``touch_session``
    keeps ``switching``), so a prompt whose hook ran after ``before_stop`` looked was cut by
    the ``/exit``. ``after_mark`` asks what the mark does not hide: ``prompted_at``, which
    every ``UserPromptSubmit`` stamps, and the pane. Either refuses; the turn is said, the
    cool-down given back, nobody woken."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    marked: list[str] = []

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        kwargs["before_stop"]()  # still at its prompt: passes
        with store_session() as store:
            assert store.mark_handover(SESSION, held_after=datetime.min.replace(tzinfo=UTC))
        marked.append(_state())
        team_service.hook_prompt_heartbeat(SESSION, work.root)  # a prompt lands
        try:
            kwargs["after_mark"]()
        finally:
            with store_session() as store:  # as switch takes its mark back
                store.unmark_handover(SESSION, "waiting")
        raise AssertionError("cut a turn short")

    monkeypatch.setattr(fleet_service, "switch", switch)
    monkeypatch.setattr(hooks_service, "_pane_shows", lambda agent: "idle")
    _stop(runner, work)
    _work(runner)

    assert marked == [team_service.HANDOVER_STATE]
    assert _state() == "working"  # the turn the mark hid, said
    assert nudged == [] and _cool_down(work) is None

    # The pane is what closes the window a prompt typed after the settle leaves: its stamp
    # and its record come only once its hook has started and passed, but its spinner shows
    # from the moment it is submitted. A turn on screen refuses, and is said; a pane that
    # cannot be read (a failed capture, a dialog) refuses too, and says nothing of a turn.
    reached: list[str] = []

    def at_the_pane(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        kwargs["before_stop"]()
        reached.append(label)
        kwargs["after_mark"]()
        raise AssertionError("stopped a pane that is not idle")

    monkeypatch.setattr(fleet_service, "switch", at_the_pane)
    for shown, state in (("busy", "working"), ("unknown", "waiting")):
        _agent(work)
        monkeypatch.setattr(hooks_service, "_pane_shows", lambda agent, shown=shown: shown)
        _stop(runner, work)
        _work(runner)
        assert _state() == state, shown
        assert nudged == [] and _cool_down(work) is None
    assert reached == [LABEL, LABEL]


def test_a_turn_begun_before_the_worker_reads_the_row_is_left_alone(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    nudged: list[str],
) -> None:
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    _stop(runner, work)
    with store_session() as store:
        store.touch_session(SESSION, state="working")  # a prompt landed meanwhile

    _work(runner)

    assert switched == [] and nudged == [] and _cool_down(work) is None
    _agent(work)  # its turn ended: the next turn end looks again
    _stop(runner, work)
    assert len(started) == 2


@pytest.mark.parametrize("seen", ["queued and taken", "prompt stamped"])
def test_a_prompt_queued_at_the_turn_end_is_not_cut_by_the_move(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
    seen: str,
) -> None:
    """#423's tester, scenario 1 (2 of 2): a message submitted as the Stop hook returns has
    its ``UserPromptSubmit`` hook, a process of its own, stamp the board 0.3 to 1.9 s later.
    Read in that gap, the board said ``waiting`` and the move typed ``/exit`` into the new
    turn. The move now waits for the turn end to settle, then asks against the turn end
    itself: a message queued and taken since (its ``enqueue`` and ``dequeue`` are on disk at
    once, before any hook), or ``prompted_at`` later than the turn end, though the state still
    says ``waiting``. Either refuses: no ``/exit``, nobody woken, the cool-down given back. (A
    typed prompt later still is the pane's to show: the test after this one's.)"""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    _stop(runner, work)
    assert len(started) == 1
    if seen == "queued and taken":
        _taken(work)  # its hook has not run yet, so the board still says waiting
        assert _state() == "waiting"
    else:
        with store_session() as store:  # its hook stamped the prompt, as the heartbeat does
            store.mark_prompted(SESSION, datetime.now(tz=UTC))
        assert _state() == "waiting"
    exited: list[str] = []

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        kwargs["before_stop"]()
        exited.append(label)
        raise AssertionError("typed /exit into the queued turn")

    monkeypatch.setattr(fleet_service, "switch", switch)
    _work(runner)

    assert exited == [] and nudged == [] and _cool_down(work) is None
    assert _state() == "working"


def test_a_message_still_queued_at_the_turn_end_starts_no_worker(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """A message typed while the agent worked is queued (``queue-operation`` ``enqueue``) and
    taken once the turn ends: that turn end is the start of the next turn, no safe point."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    queued = {"type": "queue-operation", "operation": "enqueue", "timestamp": _stamp()}
    _write(_transcript(work), {**queued, "content": "and the docs too"})

    _stop(runner, work)
    assert started == [] and _cool_down(work) is None

    taken = {"type": "queue-operation", "operation": "dequeue", "timestamp": _stamp()}
    _write(_transcript(work), taken)
    _stop(runner, work)  # taken and answered: this turn end is one
    assert started == [_worker()]


def test_the_move_waits_for_the_turn_end_to_settle_before_its_checks(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    switched: list[dict[str, Any]],
    settled: list[datetime],
) -> None:
    """``BEFORE_LIMIT_SETTLE_SECONDS`` after the turn end, past a queued prompt's hook
    start-up, before ``before_stop`` asks anything: a worker that starts a second after the
    turn end waits the rest."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    monkeypatch.setattr(hooks_service, "BEFORE_LIMIT_SETTLE_SECONDS", 5.0)
    turn_end = datetime.now(tz=UTC) - timedelta(seconds=1)
    _claim(work, turn_end)

    _work(runner)

    [until] = settled
    assert abs((until - turn_end).total_seconds() - 5.0) < 0.5
    assert len(switched) == 1


@pytest.mark.parametrize("role", ["manager", "captain"])
def test_the_manager_and_the_captain_are_not_moved_before_a_limit(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    role: str,
) -> None:
    """The manager runs the fleet's loop, its turns chained by board writes; moving it between
    two of its decisions, for no limit hit, is a cold start in the middle of the loop. The
    captain is the owner's. Either still moves after a limit (``on_limit``)."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work, role=role)
    _on_record(endpoint)

    _stop(runner, work)
    _work(runner)

    assert started == [] and switched == [] and endpoint.calls == []


# --------------------------------------------------------------------------- when it waits


def test_a_window_that_resets_soon_is_waited_for_without_reading_another_account(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
) -> None:
    """``wait_if_reset_within_minutes``, as after a limit: a window back under the line in
    ten minutes is cheaper than a cold start elsewhere. Off record, the worker finds it,
    reads no other account, and gives the cool-down back; on record, the hook knows it."""
    endpoint = _accounts(
        monkeypatch, work=_payload(92, resets_at=NOW + timedelta(minutes=10)), home=_payload(10)
    )
    _settings(before_limit="switch", wait_if_reset_within_minutes=15)
    _agent(work)

    _stop(runner, work)
    _work(runner)
    assert endpoint.calls == ["tok-work"] and switched == [] and _cool_down(work) is None
    _stop(runner, work)
    assert len(started) == 1 and endpoint.calls == ["tok-work"]


def test_a_reset_that_leaves_another_window_over_the_line_is_no_reason_to_wait(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
) -> None:
    """The five hours reset in ten minutes, but the week is over the line too: the account
    stays full after the reset, so the agent is moved."""
    endpoint = _accounts(
        monkeypatch,
        work=_payload(92, resets_at=NOW + timedelta(minutes=10), week=96),
        home=_payload(10),
    )
    _settings(before_limit="switch", wait_if_reset_within_minutes=15)
    _agent(work)
    _on_record(endpoint)

    _stop(runner, work)
    _work(runner)

    assert started == [_worker()] and len(switched) == 1


def test_an_account_under_the_line_is_left_alone(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    endpoint = _accounts(monkeypatch, work=_payload(84), home=_payload(10))
    _settings(before_limit="switch", switch_at=85)
    _agent(work)
    _on_record(endpoint)

    _stop(runner, work)

    assert started == [] and endpoint.calls == []


# --------------------------------------------------------------------------- the worker's outcomes


def test_a_move_that_fails_is_the_one_thing_that_wakes_the_manager(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    nudged: list[str],
) -> None:
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)

    def refuse(project: ProjectInfo, label: str, **kwargs: Any) -> None:
        raise fleet_service.FleetError(f"no account under the line for {label!r}")

    monkeypatch.setattr(fleet_service, "switch", refuse)
    _work(runner)

    failed = [text for kind, text in _events(work) if kind == "handover_failed"]
    assert failed == [
        f"{LABEL}: not moved before its limit — no account under the line for '{LABEL}'"
    ]
    assert nudged == [f"{LABEL}: usage-limit hand-over failed"]

    # A worker that cannot even be started says so the same way, and gives the claim back.
    def no_fork(argv: list[str]) -> None:
        raise OSError("no fork for you")

    monkeypatch.setattr(hooks_service, "_detach", no_fork)
    _stop(runner, work)
    assert any(
        kind == "handover_failed"
        and text.endswith("could not start the hand-over worker (no fork for you)")
        for kind, text in _events(work)
    )
    assert len(nudged) == 2 and _cool_down(work) is None


def test_the_worker_moves_no_row_started_within_the_cool_down(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    switched: list[dict[str, Any]],
) -> None:
    """n3 of #423's review: the worker's own brake, for a worker started however it was —
    before it reads anything."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work, age=timedelta(minutes=3), spawned_by=hooks_service.HANDOVER_SPAWNER)

    _work(runner)

    assert switched == [] and endpoint.calls == []


# --------------------------------------------------------------------------- pins and pauses


@pytest.mark.parametrize("pin", ["--account", "role binding", "CLAUDE_CONFIG_DIR by hand"])
def test_an_agent_pinned_to_its_account_stays_there_and_nothing_is_read(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    pin: str,
) -> None:
    """Put on its account by name — the spawn's ``--account``, ``team bind <role>
    --account``, or a binding that sets ``CLAUDE_CONFIG_DIR`` itself (#9) — it stays."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    if pin == "role binding":
        settings_service.bind_role("coder", account="2")
    elif pin == "CLAUDE_CONFIG_DIR by hand":
        settings_service.bind_role("coder", env={"CLAUDE_CONFIG_DIR": str(fake_home / "own")})
    _agent(work, pinned=pin == "--account")

    _stop(runner, work)
    _work(runner)

    assert started == [] and switched == [] and endpoint.calls == []


def test_an_agent_of_a_paused_fleet_is_left_alone(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """``fleet pause``: the fleet spawns nothing while it is on, and a move is a spawn."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    with store_session() as store:
        team_service.set_signals_in(store, [(work.id, harness.PAUSE_SIGNAL)], "on")

    _stop(runner, work)
    assert started == []

    with store_session() as store:
        team_service.set_signals_in(store, [(work.id, harness.PAUSE_SIGNAL)], "off")
    _stop(runner, work)
    assert started == [_worker()]


def test_a_hand_typed_session_is_the_operators_to_move(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=SESSION, project_id=work.id, role="coder", started_at=now, last_seen_at=now
            )
        )

    _stop(runner, work)

    assert started == [] and endpoint.calls == []


# --------------------------------------------------------------------------- once per cool-down


def test_one_look_per_agent_per_cool_down(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """A second turn end while the first worker is at work starts no second one; nor does a
    row the last move (or any launch) started within the cool-down. Past it, it may again."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)

    _stop(runner, work)
    _stop(runner, work)
    assert len(started) == 1

    _forget_the_cool_down(work)
    _agent(
        work,
        age=timedelta(minutes=3),
        spawned_by=hooks_service.HANDOVER_SPAWNER,
        agent_id="agt_busy2",
    )
    _stop(runner, work)
    assert len(started) == 1  # the replacement of a move three minutes ago

    with store_session() as store:  # the last look was a cool-down ago
        stamp = datetime.now(tz=UTC) - hooks_service.HANDOVER_COOLDOWN
        store.set_ui_state(f"before-limit:{work.id}:{LABEL}", stamp.isoformat())
    _agent(work, agent_id="agt_busy3")
    _stop(runner, work)
    assert len(started) == 2


def test_two_hooks_of_one_agent_at_once_start_one_worker(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """n2 of #423's review: the cool-down is CLAIMED, compare-and-set, after the checks. A
    second hook that read it unclaimed — run here inside the first, between its read and its
    claim — claims it first, and the first then starts nothing."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    real = service.room_on_record
    raced: list[bool] = []

    def the_other_hook(leaving: int, **kwargs: Any) -> bool:
        if not raced:
            raced.append(True)
            hooks_service.check_before_limit(SESSION, transcript_path=str(_transcript(work)))
        return real(leaving, **kwargs)

    monkeypatch.setattr(service, "room_on_record", the_other_hook)
    _stop(runner, work)

    assert raced == [True] and len(started) == 1


# --------------------------------------------------------------------------- warn


def test_warn_names_each_crossing_once_and_only_on_the_human_board(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    switched: list[dict[str, Any]],
    nudged: list[str],
    at: Callable[[float], None],
) -> None:
    """m5 of #423's review: one ``before_limit`` line per crossing of the line — not one per
    cool-down for as long as a week lasts — re-armed when the account reads under it again.
    It names the account, the line, the account with room and the switch; it moves nothing,
    wakes nobody, and is in no teammate's delta (a human-board kind)."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="warn")
    _agent(work)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="sess-peer", project_id=work.id, role="coder", started_at=now, last_seen_at=now
            )
        )

    def warned() -> list[str]:
        return [text for kind, text in _events(work) if kind == team_service.BEFORE_LIMIT_KIND]

    _on_record(endpoint)
    _stop(runner, work)
    _work(runner)
    [line] = warned()
    assert line.startswith(f"{LABEL}: account 2 92% used (8% left) — over the 85%-used line;")
    assert "account 3 has room" in line
    assert f"aisquare fleet switch --project {work.id} {LABEL}" in line
    assert switched == [] and nudged == []
    assert team_service.BEFORE_LIMIT_KIND not in team_service.MANAGER_WAKE_KINDS
    assert line not in team_service.hook_prompt_heartbeat("sess-peer", work.root)

    for minute in (12, 24, 36):  # cool-downs pass; the same crossing is not said again
        at(minute)
        _on_record(endpoint)
        _forget_the_cool_down(work)
        _stop(runner, work)
        _work(runner)
    assert len(warned()) == 1

    endpoint.by_token["tok-work"] = _payload(40)  # back under the line: re-armed
    at(48)
    _on_record(endpoint)
    _stop(runner, work)
    endpoint.by_token["tok-work"] = _payload(93)  # and over it again: a new crossing
    at(54)
    _on_record(endpoint)
    _forget_the_cool_down(work)
    _stop(runner, work)
    _work(runner)
    assert len(warned()) == 2


def test_a_failing_check_costs_the_early_move_and_never_the_turn_end(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """The check is an extra on the Stop: it fails on its own line, and the row still says
    waiting — the Stop's own cost line would say the board does not."""
    _settings(before_limit="switch")
    _agent(work, state="working")

    def broken(slot: int, **kwargs: Any) -> None:
        raise RuntimeError("usage store gone")

    monkeypatch.setattr(service, "line_reading", broken)
    payload = json.dumps({"session_id": SESSION, "cwd": str(work.root)})
    done = runner.invoke(app, ["hook", "stop"], input=payload)

    assert done.exit_code == 0
    assert "before-limit check failed (RuntimeError: usage store gone)" in done.stderr
    assert "will not show this session as waiting" not in done.stderr
    assert _state() == "waiting"
    assert started == []


# --------------------------------------------------------------------------- the transcript's rules


def _scan(tmp_path: Path, *records: dict[str, Any]) -> Path:
    path = tmp_path / "scan.jsonl"
    path.unlink(missing_ok=True)
    _write(path, *records)
    return path


def _result(result: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": _stamp(at),
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        "toolUseResult": result,
    }


def _notice(task: str, status: str | None) -> dict[str, Any]:
    said = f"<status>{status}</status>" if status else "<event>[a line matched]</event>"
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": f"<task-notification>\n<task-id>{task}</task-id>\n{said}",
    }


def test_a_monitor_runs_until_its_final_notice_a_task_stop_or_its_timeout(tmp_path: Path) -> None:
    """R2-M1: a ``Monitor`` sends events with no ``<status>`` (1,057 on this machine, 256
    monitors more than one) before its final one, which ends it; so does ``TaskStop``, its call
    or its result, and its own ``timeoutMs``, unless ``persistent``."""
    now = datetime.now(tz=UTC)
    launch = _result({"taskId": "mon1", "timeoutMs": 1800000, "persistent": False})

    def running(*more: dict[str, Any], at: datetime = now) -> list[str]:
        return transcripts.background_running(_scan(tmp_path, launch, *more), now=at)

    assert running() == ["mon1"]
    assert running(_notice("mon1", None), _notice("mon1", None)) == ["mon1"]
    assert running(_notice("mon1", None), _notice("mon1", "stopped")) == []
    assert running(_result({"message": "Successfully stopped task: mon1", "task_id": "mon1"})) == []
    stop_call = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "TaskStop", "input": {"task_id": "mon1"}}],
        },
    }
    assert running(stop_call) == []
    assert running(at=now + timedelta(minutes=31)) == []  # its timeout, not reported
    forever = _result({"taskId": "mon2", "timeoutMs": 1800000, "persistent": True})
    assert transcripts.background_running(
        _scan(tmp_path, forever), now=now + timedelta(hours=5)
    ) == ["mon2"]


def test_only_the_foreground_shapes_with_an_id_are_not_background_work(tmp_path: Path) -> None:
    """R2-M1: the rules are every kind this machine's 563 transcripts hold, and a shape they do
    not know that carries an id FAILS CLOSED, running until it reports. The two foreground
    results that carry an id run no more: a subagent that answered inline, and a skill
    forked in the foreground."""

    def running(result: dict[str, Any]) -> list[str]:
        return transcripts.background_running(_scan(tmp_path, _result(result)))

    for kind in _RESULTS:
        assert running(_RESULTS[kind]("t1")) == ["t1"], kind
    assert running({"status": "completed", "agentId": "a1", "content": []}) == []
    assert running({"status": "forked", "agentId": "a2", "result": "done"}) == []
    assert running({"agentId": "a3", "status": "running"}) == ["a3"]  # a status not known
    assert running({"shellId": "s1"}) == ["s1"]  # an older or newer id key


def test_an_unmatched_enqueue_does_not_hold_every_later_turn_end(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """R2-M2: counted over the whole file, one enqueue never matched — in 16 real sessions,
    the same notice queued twice and taken once — read as a message pending at every turn end
    after it, a restart's included, and the agent was never moved. The count starts again at
    each turn end the queue lived through (``stop_hook_summary``, written after the Stop
    hooks), so a message queued during THIS turn still reads pending at its Stop."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    twice = _notice("bzb1umtm0", "completed")
    taken = {"type": "queue-operation", "operation": "dequeue", "timestamp": _stamp()}
    _write(_transcript(work), twice, twice, taken)  # queued twice, taken once
    _turn_end_record(work)  # and a turn end after it

    _stop(runner, work)
    assert started == [_worker()]

    _forget_the_cool_down(work)
    _write(_transcript(work), _notice("late1", "completed"))  # queued during this turn
    _stop(runner, work)
    assert len(started) == 1


def test_an_account_over_the_line_with_the_others_unread_is_worth_a_look(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """R2-m2: the hook's usual case — the agent's account on record over the line, and the
    others not read in five minutes. Only a read can say whether they have room, and that is
    the worker's: the hook starts it, reading nothing."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    [work_slot] = [a for a in service.list_accounts() if a.slot == 2]
    service.cached_usage([work_slot])
    endpoint.calls.clear()

    _stop(runner, work)

    assert started == [_worker()] and endpoint.calls == []


def test_what_cannot_be_read_counts_as_a_turn_and_as_background_work(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    tmp_path: Path,
) -> None:
    """R2-m2: every way a transcript cannot answer FAILS CLOSED — no transcript, no length to
    compare, a file shorter than the turn end's length (rewritten), one that cannot be read —
    so a missing file never lets a move through with background work running."""
    now = datetime.now(tz=UTC)
    agent = _agent(work)
    with store_session() as store:
        session = store.get_session(SESSION)
    assert session is not None
    gone = tmp_path / "gone.jsonl"
    assert hooks_service._TurnEnd(now, None, None).begun_since(session)
    assert hooks_service._TurnEnd(now, _transcript(work), None).begun_since(session)
    assert hooks_service._TurnEnd(now, gone, 0).begun_since(session)
    size = _transcript(work).stat().st_size
    assert not hooks_service._TurnEnd(now, _transcript(work), size).begun_since(session)
    assert transcripts.activity_since(_transcript(work), size + 1)
    assert hooks_service._background_work(None, agent.created_at)
    assert hooks_service._background_work(gone, agent.created_at)
    assert not hooks_service._background_work(_transcript(work), agent.created_at)

    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _on_record(endpoint)
    _transcript(work).unlink()  # neither the hook's path nor the board's is on disk
    _stop(runner, work)
    assert started == []


@pytest.mark.parametrize("change", ["fleet pause", "a pin"])
def test_a_pause_or_a_pin_set_while_the_move_settles_stops_it(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
    change: str,
) -> None:
    """R2-m2: ``before_stop`` asks every guard again, not only the turn's: a ``fleet pause``
    or a binding to an account set during the five seconds of the settle stops the move."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _on_record(endpoint)
    _stop(runner, work)
    exited: list[str] = []

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        if change == "fleet pause":
            with store_session() as store:
                team_service.set_signals_in(store, [(work.id, harness.PAUSE_SIGNAL)], "on")
        else:
            settings_service.bind_role("coder", account="2")
        kwargs["before_stop"]()
        exited.append(label)
        raise AssertionError("moved after the guard changed")

    monkeypatch.setattr(fleet_service, "switch", switch)
    _work(runner)

    assert exited == [] and nudged == []


def test_two_workers_that_both_would_warn_post_one_line(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """The warned row is written compare-and-set before the line: a second worker that read it
    unwritten says nothing."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="warn")
    _agent(work)
    _on_record(endpoint)
    monkeypatch.setattr(hooks_service, "_warned", lambda raw, slot: False)  # both read it unset
    real = hooks_service._warn

    def stale(found: Any, line: Any, target: Any, settings: Any, warned: str | None) -> None:
        real(found, line, target, settings, None)

    monkeypatch.setattr(hooks_service, "_warn", stale)
    _work(runner)
    _work(runner)

    lines = [text for kind, text in _events(work) if kind == team_service.BEFORE_LIMIT_KIND]
    assert len(lines) == 1
