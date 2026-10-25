"""F2 of #400 — move a fleet agent to another account BEFORE it hits a usage limit.

``[accounts] before_limit`` (``off`` by default; ``warn``; ``switch``) acts at the two safe
points an agent has, its turn end (the ``Stop`` hook) and Claude Code's idle notice, never
mid-turn. The agent's account is read through the usage cache, five minutes deep, and only
when it reads over ``switch_at`` are the others read, a launch's minute deep: no loop, no
request the cache would not have made. Usage is scripted (``_Usage`` counts every request);
the switch itself is ``fleet switch``'s, exercised in ``tests/test_fleet_service.py`` — here
only the decision, the worker's call and what the board is told.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness, selfcli
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


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every hand-over worker the hooks start; nothing is switched inline."""
    workers: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", lambda argv: workers.append(list(argv)))

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the hook must not switch inline")

    monkeypatch.setattr(fleet_service, "switch", never)
    return workers


@pytest.fixture
def nudged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    woken: list[str] = []
    monkeypatch.setattr(
        team_service,
        "_nudge_manager",
        lambda project_id, *, reason, board: woken.append(reason),
    )
    return woken


def _accounts(
    monkeypatch: pytest.MonkeyPatch, *, work: dict[str, Any] | int, home: dict[str, Any] | int
) -> _Usage:
    """Slot 2 (``work``, the agent's) and slot 3 (``home``) signed in, with a counting endpoint."""
    _slot("work@example.com", "tok-work")
    _slot("home@example.com", "tok-home")
    endpoint = _Usage({"tok-work": work, "tok-home": home})
    monkeypatch.setattr(service, "_http_get", endpoint)
    return endpoint


def _settings(**overrides: Any) -> None:
    config: AppConfig = load_config()
    config.accounts = AccountsSettings(**overrides)
    save_config(config)


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
    """A fleet agent on slot 2 whose board row is ``state``, started ``age`` ago. A row of
    another ``agent_id`` replaces the live one (an upsert keeps a row's ``created_at``)."""
    now = datetime.now(tz=UTC)
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


def _stop(runner: CliRunner, project: ProjectInfo) -> None:
    payload = json.dumps({"session_id": SESSION, "cwd": str(project.root)})
    done = runner.invoke(app, ["hook", "stop"], input=payload)
    assert done.exit_code == 0 and done.stdout == "", done.output


def _notify(runner: CliRunner, project: ProjectInfo, kind: str | None, message: str) -> None:
    payload: dict[str, Any] = {"session_id": SESSION, "cwd": str(project.root), "message": message}
    if kind is not None:
        payload["notification_type"] = kind
    assert runner.invoke(app, ["hook", "notification"], input=json.dumps(payload)).exit_code == 0


def _events(project: ProjectInfo) -> list[tuple[str, str]]:
    with store_session() as store:
        return [(e.kind, e.text) for e in store.recent_events(project.id, limit=50)]


def _worker() -> list[str]:
    return [*selfcli.argv_for([]), "--quiet", "hook", "hand-over", SESSION, "--before-limit"]


def _forget_the_cool_down(project: ProjectInfo) -> None:
    with store_session() as store:
        store.delete_meta(f"before_limit:{project.id}:{LABEL}")


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


# --------------------------------------------------------------------------- switch


def test_an_agent_over_the_line_is_handed_over_at_its_turn_end_by_the_detached_worker(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
) -> None:
    """The hook DECIDES and a worker the pane cannot kill PERFORMS, as after a limit: one
    ``hook hand-over --before-limit``. Its account first, then the one it would go to; no
    manager is woken for a move that is going ahead."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    _stop(runner, work)

    assert started == [_worker()]
    assert endpoint.calls == ["tok-work", "tok-home"]
    assert nudged == []
    assert not [kind for kind, _ in _events(work) if kind == "handover_failed"]


def test_the_worker_moves_it_before_its_limit_reading_the_pick_from_the_cache(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    nudged: list[str],
) -> None:
    """The worker's switch: reason ``before limit``, the row the hook read, the automatic
    pick — and that pick reads the candidates the hook just read from the cache, so the move
    costs the endpoint no request of its own."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    agent = _agent(work)
    service.cached_usage(service.list_accounts())  # what the hook's two reads recorded
    assert len(endpoint.calls) == 2
    calls: list[dict[str, Any]] = []

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        calls.append({"label": label, **kwargs})
        picked = service.choose_for_handover(
            role="coder",
            project=project,
            exclude=(2,),
            automatic=kwargs["automatic"],
            max_age=kwargs["max_age"],
        )
        assert picked.account is not None and picked.account.slot == 3
        kwargs["before_stop"]()  # still at its prompt: no refusal
        return SimpleNamespace(failures=[])

    monkeypatch.setattr(fleet_service, "switch", switch)

    moved = runner.invoke(app, ["hook", "hand-over", SESSION, "--before-limit"])

    assert moved.exit_code == 0 and moved.stdout == ""
    [call] = calls
    assert call["label"] == LABEL and call["reason"] == "before limit"
    assert call["automatic"] is True and call["spawned_by"] == hooks_service.HANDOVER_SPAWNER
    assert call["agent_id"] == agent.id
    assert call["max_age"] == service.PICK_MAX_AGE_SECONDS
    assert len(endpoint.calls) == 2  # the pick was served from the cache
    assert nudged == []


def test_a_move_that_fails_is_the_one_thing_that_wakes_the_manager(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    nudged: list[str],
) -> None:
    _settings(before_limit="switch")
    _agent(work)

    def refuse(project: ProjectInfo, label: str, **kwargs: Any) -> None:
        raise fleet_service.FleetError(f"no account under the line for {label!r}")

    monkeypatch.setattr(fleet_service, "switch", refuse)

    assert runner.invoke(app, ["hook", "hand-over", SESSION, "--before-limit"]).exit_code == 0

    failed = [text for kind, text in _events(work) if kind == "handover_failed"]
    assert failed == [
        f"{LABEL}: not moved before its limit — no account under the line for '{LABEL}'"
    ]
    assert nudged == [f"{LABEL}: usage-limit hand-over failed"]

    # A worker that cannot even be started says so the same way.
    _accounts(monkeypatch, work=_payload(92), home=_payload(10))

    def no_fork(argv: list[str]) -> None:
        raise OSError("no fork for you")

    monkeypatch.setattr(hooks_service, "_detach", no_fork)
    _stop(runner, work)
    assert any(
        kind == "handover_failed"
        and text.endswith("could not start the hand-over worker (no fork for you)")
        for kind, text in _events(work)
    )
    assert len(nudged) == 2


# --------------------------------------------------------------------------- safe points


def test_the_idle_notice_is_a_safe_point_and_a_prompt_mid_turn_is_not(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """Claude Code's idle notice comes a minute into a pause: the agent sits at its prompt.
    A permission prompt comes mid-turn, and the row says so: nothing is moved then."""
    _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    _notify(runner, work, "permission_prompt", "Claude needs your permission to use Bash")
    assert started == []

    _agent(work)  # answered, and its turn ended: waiting again
    _notify(runner, work, "idle_prompt", "Claude is waiting for your input")
    assert started == [_worker()]

    # An older Claude Code sends no type; the idle notice is told by its words.
    _forget_the_cool_down(work)
    _notify(runner, work, None, "Claude is waiting for your input")
    assert len(started) == 2


@pytest.mark.parametrize("state", ["working", "attention", "limited", "switching"])
def test_an_agent_that_is_not_waiting_at_its_prompt_is_never_moved(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    state: str,
) -> None:
    """Mid-turn (``working``), asking (``attention``), parked on a limit (``on_limit``'s), or
    already being moved: an idle notice that arrives then moves nothing, and reads nothing."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work, state=state)

    _notify(runner, work, "idle_prompt", "Claude is waiting for your input")

    assert started == [] and endpoint.calls == []


def test_a_turn_end_that_keeps_the_agent_going_is_not_a_safe_point(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """A note addressed to the agent keeps its turn going at the Stop (P0-W2): the turn is
    not over, so it is not moved then."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    team_service.add_note("check the migration first", to_role=LABEL, project_id=work.id)

    payload = json.dumps({"session_id": SESSION, "cwd": str(work.root)})
    kept_going = runner.invoke(app, ["hook", "stop"], input=payload)

    assert kept_going.exit_code == 0 and '"decision": "block"' in kept_going.stdout
    assert started == [] and endpoint.calls == []


def test_a_turn_begun_before_the_worker_stops_it_is_left_alone_and_retried_next_time(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    nudged: list[str],
) -> None:
    """The worker's last check, before anything is stopped (``switch``'s ``before_stop``):
    a prompt that arrived since the hook decided started a turn, which is not cut short.
    That is no failure — nobody is woken — and it costs no cool-down."""
    workers: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", lambda argv: workers.append(list(argv)))
    _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)
    _stop(runner, work)
    assert len(workers) == 1

    def switch(project: ProjectInfo, label: str, **kwargs: Any) -> Any:
        with store_session() as store:
            store.touch_session(SESSION, state="working")  # a prompt landed meanwhile
        kwargs["before_stop"]()
        raise AssertionError("stopped mid-turn")

    monkeypatch.setattr(fleet_service, "switch", switch)
    assert runner.invoke(app, ["hook", "hand-over", SESSION, "--before-limit"]).exit_code == 0

    assert nudged == []
    assert not [kind for kind, _ in _events(work) if kind == "handover_failed"]
    _stop(runner, work)  # the turn it began ends: asked again, the cool-down not spent
    assert len(workers) == 2

    # A prompt that landed before the worker even read the row: the same, and no switch.
    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an agent at work is not switched")

    monkeypatch.setattr(fleet_service, "switch", never)
    with store_session() as store:
        store.touch_session(SESSION, state="working")
    assert runner.invoke(app, ["hook", "hand-over", SESSION, "--before-limit"]).exit_code == 0
    _stop(runner, work)
    assert len(workers) == 3 and nudged == []


# --------------------------------------------------------------------------- when it waits


def test_a_window_that_resets_soon_is_waited_for_without_reading_another_account(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """``wait_if_reset_within_minutes``, as after a limit: a window back under the line in
    ten minutes is cheaper than a cold start elsewhere — and then the other accounts are
    not read either."""
    endpoint = _accounts(
        monkeypatch, work=_payload(92, resets_at=NOW + timedelta(minutes=10)), home=_payload(10)
    )
    _settings(before_limit="switch", wait_if_reset_within_minutes=15)
    _agent(work)

    _stop(runner, work)

    assert started == [] and endpoint.calls == ["tok-work"]


def test_a_reset_that_leaves_another_window_over_the_line_is_no_reason_to_wait(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """The five hours reset in ten minutes, but the week is over the line too: the account
    stays full after the reset, so the agent is moved."""
    _accounts(
        monkeypatch,
        work=_payload(92, resets_at=NOW + timedelta(minutes=10), week=96),
        home=_payload(10),
    )
    _settings(before_limit="switch", wait_if_reset_within_minutes=15)
    _agent(work)

    _stop(runner, work)

    assert started == [_worker()]


def test_no_move_and_no_word_when_no_other_account_is_under_the_line(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
) -> None:
    """Every other account over the line: the agent carries on where it is. It has hit no
    limit, so this is no failure, and the manager is not woken for it."""
    _accounts(monkeypatch, work=_payload(92), home=_payload(90))
    _settings(before_limit="switch")
    _agent(work)

    _stop(runner, work)

    assert started == [] and nudged == []
    assert not [k for k, _ in _events(work) if k in {"handover_failed", "before_limit"}]


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

    _stop(runner, work)

    assert started == [] and endpoint.calls == ["tok-work"]


# --------------------------------------------------------------------------- pins and pauses


@pytest.mark.parametrize("pin", ["--account", "role binding", "CLAUDE_CONFIG_DIR by hand"])
def test_an_agent_pinned_to_its_account_stays_there_and_nothing_is_read(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
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

    assert started == [] and endpoint.calls == []


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
    with store_session() as store:
        team_service.set_signals_in(store, [(work.id, harness.PAUSE_SIGNAL)], "on")

    _stop(runner, work)
    assert started == [] and endpoint.calls == []

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


def test_one_move_per_agent_per_cool_down(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """A second turn end while the first worker is at work starts no second one; nor does a
    row the last move (or any launch) started within the cool-down. Past it, it may again."""
    _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

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

    with store_session() as store:  # the last move was a cool-down ago
        stamp = datetime.now(tz=UTC) - hooks_service.HANDOVER_COOLDOWN
        store.set_meta(f"before_limit:{work.id}:{LABEL}", stamp.isoformat())
    _agent(work, agent_id="agt_busy3")
    _stop(runner, work)
    assert len(started) == 2


# --------------------------------------------------------------------------- no extra reads


def test_turn_ends_ask_the_endpoint_no_more_often_than_the_cache_does(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """Many turn ends, one request: the agent's account is read through the shared cache,
    five minutes deep. Past the five minutes, one more. The counting fake is the proof."""
    endpoint = _accounts(monkeypatch, work=_payload(50), home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    for _ in range(5):
        _stop(runner, work)
    assert endpoint.calls == ["tok-work"]

    later = NOW + timedelta(seconds=service.USAGE_MAX_AGE_SECONDS + 1)
    monkeypatch.setattr(service, "_now", lambda: later)
    for _ in range(3):
        _stop(runner, work)
    assert endpoint.calls == ["tok-work", "tok-work"]
    assert started == []


def test_a_backed_off_account_is_not_asked_and_moves_nobody(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """A 429 starts a wait every process honours (F1 of #400): no turn end asks during it,
    and the wait's reading — kept, not live — moves no agent."""
    endpoint = _accounts(monkeypatch, work=429, home=_payload(10))
    _settings(before_limit="switch")
    _agent(work)

    _stop(runner, work)
    assert endpoint.calls == ["tok-work"]
    for _ in range(3):
        _stop(runner, work)

    assert endpoint.calls == ["tok-work"]
    assert started == []


def test_a_kept_reading_is_never_taken_for_a_live_one(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
) -> None:
    """The last good figure a failed read shows, 92 % ten minutes ago, is of unknown age now:
    the page shows it, aged; it moves nobody (#408)."""
    endpoint = _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="switch")
    earlier = NOW - timedelta(minutes=10)
    monkeypatch.setattr(service, "_now", lambda: earlier)
    [work_slot] = [a for a in service.list_accounts() if a.slot == 2]
    service.cached_usage([work_slot])  # recorded ten minutes ago
    monkeypatch.setattr(service, "_now", lambda: NOW)
    endpoint.by_token["tok-work"] = 500  # and now the read fails
    _agent(work)
    shown = service.cached_usage([work_slot])[2]
    assert shown.kept and shown.session_percent == 92  # the premise: kept, and over the line

    _stop(runner, work)

    assert started == []


# --------------------------------------------------------------------------- warn


def test_warn_puts_the_move_on_the_board_and_does_nothing_else(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    started: list[list[str]],
    nudged: list[str],
) -> None:
    """``warn``: one ``before_limit`` line naming the account, the line, the account with
    room and the switch that moves it — once per cool-down — and no move, no wake-up."""
    _accounts(monkeypatch, work=_payload(92), home=_payload(10))
    _settings(before_limit="warn")
    _agent(work)

    _stop(runner, work)
    _stop(runner, work)

    warned = [text for kind, text in _events(work) if kind == team_service.BEFORE_LIMIT_KIND]
    assert len(warned) == 1, warned
    [line] = warned
    assert line.startswith(f"{LABEL}: account 2 92% used (8% left) — over the 85%-used line;")
    assert "account 3 has room" in line
    assert f"aisquare fleet switch --project {work.id} {LABEL}" in line
    assert started == [] and nudged == []
    assert team_service.BEFORE_LIMIT_KIND not in team_service.MANAGER_WAKE_KINDS


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

    monkeypatch.setattr(service, "near_limit", broken)
    payload = json.dumps({"session_id": SESSION, "cwd": str(work.root)})
    done = runner.invoke(app, ["hook", "stop"], input=payload)

    assert done.exit_code == 0
    assert "before-limit check failed (RuntimeError: usage store gone)" in done.stderr
    assert "will not show this session as waiting" not in done.stderr
    with store_session() as store:
        session = store.get_session(SESSION)
    assert session is not None and session.state == "waiting"
    assert started == []
