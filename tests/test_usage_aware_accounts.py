"""#146 — spawn where there is headroom, and hand an agent over when a usage limit hits.

Three things, each with the control that would catch it going blind:

- **The signal.** Claude Code fires ``StopFailure`` (not ``Stop``) when a turn
  ends on an API error, with ``error: rate_limit`` and the rendered text
  ``You've hit your session limit · resets 12:30am (America/Toronto)`` — the
  shape read out of this machine's own transcripts on 2026-09-13. The hook
  parks the session as ``limited`` with that reset, once, and wakes the
  manager; any other error is a ``waiting`` row and a feed line.
- **The pick.** ``[accounts] pick = "headroom"`` reads every enabled,
  signed-in account's five-hour window and takes, in priority order, the
  first under ``switch_at`` — or the one with the most room when all are over.
  Usage is scripted here (a recorded payload per slot), never fetched.
- **The trend.** Two readings of the same window say how fast it fills; the
  page and ``accounts usage`` say "≈ 40 min to the limit". Fixed clock.

The hand-over itself (``fleet switch``) is exercised against the fake tmux in
``tests/test_fleet_service.py``; here only the hook's decision to call it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import paths, selfcli
from aisquare.core.config import AccountsSettings, AppConfig, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo, TeamSession, TurnMetric
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from aisquare.services import team as team_service
from tests.test_claude_accounts import LIVE_USAGE, NOW, _sign_in
from tests.test_claude_accounts import fake_home as _redirected_home

#: Re-exported so pytest collects it here (see tests/test_account_defaults.py).
fake_home = _redirected_home

#: ``NOW`` is the sibling file's clock (the credentials its ``_sign_in`` writes expire
#: relative to it); the parser tests use a SUNDAY so the weekday arithmetic is visible.
SUNDAY = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)  # 08:00 in Toronto (EDT)
TORONTO_MIDNIGHT_UTC = datetime(2026, 9, 13, 4, 30, tzinfo=UTC)  # 00:30 EDT that Sunday
SESSION_LIMIT = "You've hit your session limit · resets 12:30am (America/Toronto)"
WEEKLY_LIMIT = "You've hit your weekly limit · resets Mon 12:00am (America/Toronto)"
API_KEY_429 = "API Error: Request rejected (429) · this may be a temporary capacity issue."


# --------------------------------------------------------------------------- helpers


class _Usage:
    """A scripted usage endpoint: one payload per slot, keyed by the token each slot holds."""

    def __init__(self, by_token: dict[str, dict[str, Any] | int]) -> None:
        self.by_token = by_token
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        token = headers["Authorization"].removeprefix("Bearer ")
        self.calls.append(token)
        answer = self.by_token.get(token, 401)
        if isinstance(answer, int):
            return answer, b""
        return 200, json.dumps(answer).encode()


def _payload(
    percent: float,
    *,
    resets_at: datetime = NOW + timedelta(hours=3),
    week: float | None = None,
) -> dict[str, Any]:
    """The live payload with the five-hour window at ``percent`` (and the week at ``week``)."""
    payload: dict[str, Any] = json.loads(json.dumps(LIVE_USAGE))
    payload["five_hour"]["utilization"] = percent
    payload["five_hour"]["resets_at"] = resets_at.isoformat()
    if week is not None:
        payload["seven_day"]["utilization"] = week
    return payload


def _slot(email: str, token: str, *, expires_in: timedelta = timedelta(hours=7)) -> ClaudeAccount:
    """A signed-in managed slot whose credentials carry ``token`` (the fetch's key)."""
    account = core.create_account()
    _sign_in(account, email, expires_in=expires_in)
    creds = core.credentials_path(account)
    data = json.loads(creds.read_text())
    data["claudeAiOauth"]["accessToken"] = token
    creds.write_text(json.dumps(data))
    return account


def _settings(**overrides: Any) -> AccountsSettings:
    config: AppConfig = load_config()
    config.accounts = AccountsSettings(**overrides)
    save_config(config)
    return config.accounts


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project


def _session(project: ProjectInfo, session_id: str, *, role: str = "coder") -> TeamSession:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        return store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
                transcript_path=f"/tmp/{session_id}.jsonl",
            )
        )


def _state(session_id: str) -> tuple[str, datetime | None]:
    with store_session() as store:
        session = store.get_session(session_id)
        assert session is not None
        return session.state, session.limit_resets_at


def _events(project: ProjectInfo) -> list[tuple[str, str]]:
    with store_session() as store:
        return [(e.kind, e.text) for e in store.recent_events(project.id, limit=50)]


# --------------------------------------------------------------------------- the signal


@pytest.mark.parametrize(
    ("text", "window", "resets_utc"),
    [
        (SESSION_LIMIT, "session", TORONTO_MIDNIGHT_UTC + timedelta(days=1)),
        (WEEKLY_LIMIT, "weekly", datetime(2026, 9, 14, 4, 0, tzinfo=UTC)),
        ("You've hit your Opus limit · resets 3:45pm (America/Toronto)", "opus", None),
        ("You've hit your session limit", "session", None),
    ],
)
def test_the_limit_message_is_read_for_its_window_and_its_reset(
    text: str, window: str, resets_utc: datetime | None
) -> None:
    """The four shapes the errors reference documents, resolved against a fixed clock."""
    notice = core.parse_limit_notice(text, now=SUNDAY)  # 08:00 Toronto, a Sunday

    assert notice is not None and notice.window == window
    if resets_utc is None and "resets" in text:
        # 3:45pm Toronto on the same day: the clock is 08:00, so today.
        assert notice.resets_at == datetime(2026, 9, 13, 19, 45, tzinfo=UTC)
    else:
        assert notice.resets_at == resets_utc  # 12:30am has passed today → tomorrow


def test_text_after_the_zone_costs_neither_the_window_nor_the_reset() -> None:
    """A period, a second sentence or more lines follow the zone (review of #205, finding 7)."""
    for trailing in (".", ". Upgrade for more usage.", "\nUpgrade for more usage.", " — try later"):
        notice = core.parse_limit_notice(SESSION_LIMIT + trailing, now=SUNDAY)
        assert notice is not None and notice.window == "session", trailing
        assert notice.resets_at == TORONTO_MIDNIGHT_UTC + timedelta(days=1), trailing
    weekly = core.parse_limit_notice(WEEKLY_LIMIT + ".\nMore usage is available", now=SUNDAY)
    assert weekly is not None and weekly.resets_at == datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
    bare = core.parse_limit_notice("You've hit your session limit. Try again.", now=SUNDAY)
    assert bare is not None and bare.window == "session" and bare.resets_at is None
    # `13:00pm` is no clock time: the raw hour is what needed the guard (third round).
    odd = core.parse_limit_notice(
        "You've hit your session limit · resets 13:00pm (America/Toronto)", now=SUNDAY
    )
    assert odd is not None and odd.window == "session" and odd.resets_at is None
    fine = core.parse_limit_notice(SESSION_LIMIT, now=SUNDAY)
    assert fine is not None and fine.resets_at is not None  # 12:30am is still a clock time


def test_a_reset_with_no_zone_named_is_resolved_in_the_local_rules_across_a_dst_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #205, fourth round: with no zone in the message — or one ``ZoneInfo`` cannot
    find — the local zone was ``now.astimezone().tzinfo``, the offset in force NOW, and a
    weekly reset on the far side of a DST change came out an hour early. Toronto, Friday
    2026-10-30 (EDT); Monday 00:00 is after the 1 November change, so it is 05:00 UTC.

    Skipped where ``time`` has no ``tzset`` (Windows): the zone cannot be switched for the
    process, and #65's windows-latest leg runs this file (review of #205, fifth round)."""
    # In the body and on `sys.platform`, not a `hasattr` skipif: `pytest.skip` is
    # `NoReturn`, so mypy's run on the Windows leg narrows past it and does not
    # report `time.tzset` as missing from that platform's `time`.
    if sys.platform == "win32":
        pytest.skip("time.tzset is POSIX-only: the process zone cannot be switched for the test")
    friday = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)  # noon EDT (-04:00)
    monday_midnight_est = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    with monkeypatch.context() as local:
        local.setenv("TZ", "America/Toronto")
        time.tzset()
        try:
            bare = core.parse_limit_notice(
                "You've hit your weekly limit · resets Mon 12:00am", now=friday
            )
            unknown = core.parse_limit_notice(
                "You've hit your weekly limit · resets Mon 12:00am (Mars/Olympus)", now=friday
            )
            named = core.parse_limit_notice(WEEKLY_LIMIT, now=friday)  # the control
        finally:
            local.undo()
            time.tzset()
    assert bare is not None and bare.resets_at == monday_midnight_est
    assert unknown is not None and unknown.resets_at == monday_midnight_est
    assert named is not None and named.resets_at == monday_midnight_est


def test_a_429_that_is_not_a_usage_limit_and_an_unreadable_time_degrade_gracefully() -> None:
    assert core.parse_limit_notice(API_KEY_429, now=SUNDAY) is None  # not a plan limit
    assert core.parse_limit_notice(None, now=SUNDAY) is None
    assert core.parse_limit_notice("", now=SUNDAY) is None
    odd = core.parse_limit_notice("You've hit your session limit · resets soon-ish", now=SUNDAY)
    assert odd is not None and odd.window == "session" and odd.resets_at is None
    zone = core.parse_limit_notice(
        "You've hit your session limit · resets 9:00am (Mars/Base)", now=SUNDAY
    )
    assert zone is not None and zone.resets_at is not None  # an unknown zone falls back, not None


def test_a_rate_limit_stop_failure_parks_the_session_limited_once_and_wakes_the_manager(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    nudges: list[str] = []
    monkeypatch.setattr(
        team_service, "_nudge_manager", lambda project_id, *, reason: nudges.append(reason)
    )
    _session(work, "sess-coder")
    payload = json.dumps(
        {
            "session_id": "sess-coder",
            "cwd": str(work.root),
            "hook_event_name": "StopFailure",
            "error": "rate_limit",
            "error_details": "429 Too Many Requests",
            "last_assistant_message": SESSION_LIMIT,
        }
    )

    result = runner.invoke(app, ["hook", "stop-failure"], input=payload)

    assert result.exit_code == 0 and result.stdout == ""  # output is ignored by Claude Code anyway
    state, resets = _state("sess-coder")
    assert state == "limited" and resets is not None
    limited = [text for kind, text in _events(work) if kind == "limited"]
    assert len(limited) == 1
    assert "hit its session limit" in limited[0] and "resets" in limited[0]
    assert "fleet switch" not in limited[0]  # a hand-typed session: nothing to switch
    assert nudges == ["sess-cod hit its usage limit"] or len(nudges) == 1

    # Claude Code re-fires for the same window: the state holds, the feed does not repeat.
    again = runner.invoke(app, ["hook", "stop-failure"], input=payload)
    assert again.exit_code == 0
    assert len([1 for kind, _ in _events(work) if kind == "limited"]) == 1
    assert len(nudges) == 1

    # The next prompt lifts it (Claude Code's own continue at the reset is a prompt).
    prompt = json.dumps({"session_id": "sess-coder", "cwd": str(work.root), "prompt": "go on"})
    runner.invoke(app, ["hook", "user-prompt-submit"], input=prompt)
    assert _state("sess-coder")[0] == "working"


def test_a_limited_fleet_agent_is_named_by_its_label_with_the_switch_command(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)
    _session(work, "sess-fleet")
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_limited1",
                project_id=work.id,
                label="coder-auth",
                role="coder",
                pane_id="%9",
                session_id="sess-fleet",
                cwd=work.root,
                created_at=datetime.now(tz=UTC),
            )
        )
    payload = json.dumps(
        {
            "session_id": "sess-fleet",
            "cwd": str(work.root),
            "error": "rate_limit",
            "last_assistant_message": SESSION_LIMIT,
        }
    )

    assert runner.invoke(app, ["hook", "stop-failure"], input=payload).exit_code == 0

    [text] = [text for kind, text in _events(work) if kind == "limited"]
    assert text.startswith("coder-auth hit its session limit")
    assert "aisquare fleet switch coder-auth" in text


def test_other_api_errors_end_the_turn_as_waiting_with_a_feed_line(
    fake_home: Path, work: ProjectInfo, runner: CliRunner
) -> None:
    _session(work, "sess-x")
    prompt = json.dumps({"session_id": "sess-x", "cwd": str(work.root), "prompt": "hi"})
    runner.invoke(app, ["hook", "user-prompt-submit"], input=prompt)
    assert _state("sess-x")[0] == "working"

    payload = json.dumps(
        {
            "session_id": "sess-x",
            "cwd": str(work.root),
            "error": "authentication_failed",
            "last_assistant_message": "API Error: 401 authentication failed",
        }
    )
    assert runner.invoke(app, ["hook", "stop-failure"], input=payload).exit_code == 0

    assert _state("sess-x") == ("waiting", None)
    failed = [text for kind, text in _events(work) if kind == "turn_failed"]
    assert failed == ["authentication_failed: API Error: 401 authentication failed"]
    assert not any(kind == "limited" for kind, _ in _events(work))


def test_an_unknown_session_and_a_missing_id_cost_nothing(
    fake_home: Path, work: ProjectInfo, runner: CliRunner
) -> None:
    for payload in (
        {"session_id": "nobody", "cwd": str(work.root), "error": "rate_limit"},
        {"cwd": str(work.root), "error": "rate_limit"},
    ):
        result = runner.invoke(app, ["hook", "stop-failure"], input=json.dumps(payload))
        assert result.exit_code == 0 and result.stdout == ""
    assert _events(work) == [] or not any(kind == "limited" for kind, _ in _events(work))


def _open_turn(project: ProjectInfo, session_id: str, trace_id: str) -> None:
    metrics_service.open_turn(
        TurnMetric(
            trace_id=trace_id,
            project_id=project.id,
            session_id=session_id,
            started_at=datetime.now(tz=UTC),
        )
    )


def test_a_board_write_that_fails_still_closes_the_turn_and_hands_nothing_over(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``turn_failed`` said its steps fail on their own, and none was guarded: a board write
    that raised skipped the metrics close, and ``close_turn`` closes only the NEWEST open row,
    so that turn stayed open for good (review of the #205 fold, round 1). The hand-over acts
    on the board's record and is skipped; the error still reaches the hook's cost line."""
    _session(work, "sess-coder")
    _open_turn(work, "sess-coder", "trc_limited")

    def refuses(session_id: str, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(team_service, "hook_stop_failure", refuses)
    handed: list[team_service.TurnFailure] = []
    monkeypatch.setattr(hooks_service, "_hand_over_if_configured", handed.append)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        hooks_service.turn_failed(
            session_id="sess-coder", error="rate_limit", message=SESSION_LIMIT
        )

    [turn] = metrics_service.recent(session_id="sess-coder")
    assert turn.ended_at is not None
    assert handed == []

    # `turn_stopped` had the same shape: a manager's failed wake-up raises after the row
    # says waiting, and the close was skipped with it.
    _open_turn(work, "sess-coder", "trc_stopped")

    def wake_fails(*args: Any, **kwargs: Any) -> None:
        raise team_service.ManagerWakeupError(RuntimeError("no delta"))

    monkeypatch.setattr(team_service, "hook_stop", wake_fails)
    with pytest.raises(team_service.ManagerWakeupError):
        hooks_service.turn_stopped(work.root, session_id="sess-coder")
    assert all(t.ended_at is not None for t in metrics_service.recent(session_id="sess-coder"))


def test_claude_codes_routine_notices_leave_a_parked_or_handed_over_row_as_it_is(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where #153 meets the parking above: a notice is not a bell over ``limited``.

    A limited agent sits idle until its reset, so Claude Code's idle notice
    (~60 s into the pause) and the ``quota_auto_resume_*`` family arrive while
    it is parked. When every ``Notification`` called ``mark_attention``, each
    wrote ``attention`` over the row — the reset it named left the board and
    the bell rang for an agent no one can help — and over a hand-over's mark,
    whose ``SessionEnd`` then released the claims the replacement was to
    inherit. Only a prompt that needs a human rings now.
    """
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)
    _session(work, "sess-coder")
    team_service.hook_stop_failure(
        "sess-coder", error="rate_limit", message=SESSION_LIMIT, details=None
    )
    parked = _state("sess-coder")
    assert parked[0] == "limited" and parked[1] is not None  # control: it is parked

    def notify(notification_type: str | None, message: str) -> None:
        payload: dict[str, Any] = {
            "session_id": "sess-coder",
            "cwd": str(work.root),
            "message": message,
        }
        if notification_type is not None:
            payload["notification_type"] = notification_type
        result = runner.invoke(app, ["hook", "notification"], input=json.dumps(payload))
        assert result.exit_code == 0, result.output

    notify("idle_prompt", "Claude is waiting for your input")
    notify(None, "Claude is waiting for your input")  # an older Claude Code: the text
    notify("quota_auto_resume_fired", "Usage limit reset — Claude is continuing your task")

    assert _state("sess-coder") == parked  # the same reset, still on the board
    assert [text for kind, text in _events(work) if kind == "notice"] == [
        "Usage limit reset — Claude is continuing your task"
    ]
    assert not any(kind == "attention" for kind, _ in _events(work))

    with store_session() as store:  # `fleet switch` has marked it and is waiting for the /exit
        store.touch_session("sess-coder", state=team_service.HANDOVER_STATE)
    notify("idle_prompt", "Claude is waiting for your input")
    assert _state("sess-coder")[0] == team_service.HANDOVER_STATE


# --------------------------------------------------------------------------- the hand-over decision


def _fleet_row(project: ProjectInfo, agent_id: str, label: str, session_id: str) -> None:
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=agent_id,
                project_id=project.id,
                label=label,
                role="coder",
                pane_id="%3",
                session_id=session_id,
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
            )
        )


def _fire_limit(runner: CliRunner, project: ProjectInfo, message: str, session: str) -> None:
    payload = json.dumps(
        {
            "session_id": session,
            "cwd": str(project.root),
            "error": "rate_limit",
            "last_assistant_message": message,
        }
    )
    assert runner.invoke(app, ["hook", "stop-failure"], input=payload).exit_code == 0


def test_the_hook_starts_a_detached_hand_over_only_when_configured_and_the_reset_is_far(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook DECIDES; a worker the pane cannot kill PERFORMS (review of #205, finding 1).

    Run inline, the hand-over was a child of the pane ``switch`` kills, and the
    kill took it down before the replacement was spawned. So the hook's whole
    output is one detached ``aisquare hook hand-over``; nothing is switched in
    this process, and ``fleet.switch`` must not be reached from here at all.
    """
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)
    started: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", lambda argv: started.append(list(argv)))

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the hook must not switch inline")

    monkeypatch.setattr(fleet_service, "switch", never)
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_limited2", "coder-db", "sess-fleet")

    def unpark() -> None:  # a re-fire for the SAME window decides nothing (second round)
        with store_session() as store:
            store.touch_session("sess-fleet", state="working")

    # The default: wait. Nothing is started however far the reset is.
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert started == []

    # Configured to switch: a limit whose reset is far away starts the worker…
    _settings(on_limit="switch", wait_if_reset_within_minutes=15)
    unpark()
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert len(started) == 1
    argv = started[0]
    assert argv[:4] == selfcli.argv_for([])  # this interpreter, -P, -m aisquare
    assert argv[4:] == ["--quiet", "hook", "hand-over", "sess-fleet", "--reason", "weekly limit"]

    # …one that lifts within the wait window does not (the note says why)…
    soon = (datetime.now().astimezone() + timedelta(minutes=5)).strftime("%I:%M%p").lstrip("0")
    unpark()
    _fire_limit(
        runner, work, f"You've hit your session limit · resets {soon.lower()}", "sess-fleet"
    )
    assert len(started) == 1
    assert any("waiting for the reset instead of switching" in text for _, text in _events(work))

    # …and a session that is not a fleet agent is the operator's to move.
    _session(work, "sess-hand")
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-hand")
    assert len(started) == 1

    # A worker that cannot be started is a board line, not a silent stall.
    def refuse(argv: list[str]) -> None:
        raise OSError("no fork for you")

    monkeypatch.setattr(hooks_service, "_detach", refuse)
    unpark()
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert any(
        "not switched — could not start the hand-over worker (no fork for you)" in text
        for _, text in _events(work)
    )


def test_a_re_fire_for_the_same_window_starts_no_second_worker(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code re-fires ``StopFailure`` for one window; the second firing found the row
    already ``limited`` and still started a second worker — two switches of one agent at
    once (review of #205, second round). ``TurnFailure.already_limited`` now says so."""
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)
    started: list[list[str]] = []
    monkeypatch.setattr(hooks_service, "_detach", lambda argv: started.append(list(argv)))
    _settings(on_limit="switch", wait_if_reset_within_minutes=15)
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_refire", "coder-db", "sess-fleet")

    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")

    assert len(started) == 1
    # The record itself carries the fact, for any other caller.
    again = team_service.hook_stop_failure(
        "sess-fleet", error="rate_limit", message=WEEKLY_LIMIT, details=None
    )
    assert again is not None and again.limited and again.already_limited

    # [third round] A row mid hand-over is already handled too: no worker, and the
    # mark `fleet switch` set is not written over — its SessionEnd must still park.
    with store_session() as store:
        store.touch_session("sess-fleet", state=team_service.HANDOVER_STATE)
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert len(started) == 1
    assert _state("sess-fleet")[0] == team_service.HANDOVER_STATE
    mid = team_service.hook_stop_failure(
        "sess-fleet", error="rate_limit", message=WEEKLY_LIMIT, details=None
    )
    assert mid is not None and mid.already_limited


def test_another_api_error_mid_hand_over_keeps_the_mark_and_the_claims(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #205, fourth round: the ``rate_limit`` branch kept ``fleet switch``'s mark
    (third round), the other one wrote ``waiting`` over it — an ``overloaded`` landing
    while the switch waits for the ``/exit``, and the SessionEnd that followed released
    the claims the replacement was to inherit."""
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_overload", "coder-db", "sess-fleet")
    task, _created = team_service.add_task("Keep it", role="coder", cwd=work.root)
    assert team_service.claim_task(task.id, session_ref="sess-fleet").status == "doing"
    with store_session() as store:
        store.touch_session("sess-fleet", state=team_service.HANDOVER_STATE)
    payload = json.dumps(
        {
            "session_id": "sess-fleet",
            "cwd": str(work.root),
            "error": "overloaded",
            "last_assistant_message": "API Error: 529 Overloaded",
        }
    )

    assert runner.invoke(app, ["hook", "stop-failure"], input=payload).exit_code == 0

    assert _state("sess-fleet")[0] == team_service.HANDOVER_STATE
    assert ("turn_failed", "overloaded: API Error: 529 Overloaded") in _events(work)
    team_service.hook_session_end("sess-fleet", work.root, reason="prompt_input_exit")
    with store_session() as store:
        kept = store.get_task(task.id)
    assert kept is not None and kept.status == "doing" and kept.claimed_by == "sess-fleet"
    # The control: the same error on an unmarked row still ends the turn as `waiting`.
    _session(work, "sess-plain")
    plain = team_service.hook_stop_failure(
        "sess-plain", error="overloaded", message="API Error: 529 Overloaded", details=None
    )
    assert plain is not None and _state("sess-plain")[0] == "waiting"


@pytest.mark.parametrize(
    ("hook", "payload"),
    [
        ("stop", {"stop_hook_active": False}),
        ("notification", {"message": "Claude needs your permission to use Bash"}),
    ],
)
def test_a_turn_that_ends_or_asks_mid_hand_over_keeps_the_mark_and_the_claims(
    fake_home: Path,
    work: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    hook: str,
    payload: dict[str, Any],
) -> None:
    """Review of the #205 fold, round 1: the fourth round kept ``fleet switch``'s mark over a
    ``StopFailure`` only. A plain ``Stop`` — the turn of an agent switched mid-turn ending
    within ``stop``'s grace — wrote ``waiting`` over it, a permission prompt in the same
    window wrote ``attention``, and the ``SessionEnd`` that followed released the claims the
    replacement was to inherit: the task back to ``todo``, ``task_released`` on the feed."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_midturn", "coder-db", "sess-fleet")
    task, _created = team_service.add_task("Keep it", role="coder", cwd=work.root)
    assert team_service.claim_task(task.id, session_ref="sess-fleet").status == "doing"
    with store_session() as store:
        store.touch_session("sess-fleet", state=team_service.HANDOVER_STATE)
    body = {"session_id": "sess-fleet", "cwd": str(work.root), **payload}

    result = runner.invoke(app, ["hook", hook], input=json.dumps(body))

    assert result.exit_code == 0, result.output
    assert _state("sess-fleet")[0] == team_service.HANDOVER_STATE
    assert not any(kind == "attention" for kind, _ in _events(work))  # no bell for an exit
    team_service.hook_session_end("sess-fleet", work.root, reason="prompt_input_exit")
    with store_session() as store:
        kept = store.get_task(task.id)
    assert kept is not None and kept.status == "doing" and kept.claimed_by == "sess-fleet"
    assert not any(kind == "task_released" for kind, _ in _events(work))
    # The control: the same hook on an unmarked row still moves it on.
    _session(work, "sess-plain")
    plain = {**body, "session_id": "sess-plain"}
    assert runner.invoke(app, ["hook", hook], input=json.dumps(plain)).exit_code == 0
    assert _state("sess-plain")[0] == ("waiting" if hook == "stop" else "attention")


def test_only_the_sessions_own_start_or_the_switch_itself_replaces_a_hand_overs_mark(
    fake_home: Path, work: ProjectInfo
) -> None:
    """The mark is kept in the STORE, by every state writer, so a hook added later cannot
    forget it the way ``hook_stop`` and ``hook_notification`` did (review of the #205 fold,
    round 1). The rest of each write still lands; only the state is kept."""
    _session(work, "sess-fleet")
    mark = team_service.HANDOVER_STATE
    with store_session() as store:
        store.touch_session("sess-fleet", state=mark)
        store.touch_session("sess-fleet", state="waiting")  # a Stop
        store.touch_session("sess-fleet", cursor=7, state="working")  # a prompt, a manager wake
        assert store.mark_attention("sess-fleet") is False  # a permission prompt: no transition
        store.mark_limited("sess-fleet", NOW)  # a StopFailure re-fire
        kept = store.get_session("sess-fleet")
    assert kept is not None and kept.state == mark
    assert kept.cursor == 7  # the heartbeat itself landed

    # `fleet switch` takes its own mark back when the stop it marked the session for failed…
    with store_session() as store:
        store.unmark_handover("sess-fleet", "limited")
    assert _state("sess-fleet")[0] == "limited"
    # …and only its mark: a row that has moved on since is not written back.
    with store_session() as store:
        store.touch_session("sess-fleet", state="working")
        store.unmark_handover("sess-fleet", "limited")
    assert _state("sess-fleet")[0] == "working"

    # The resumed agent's own start replaces it, as it always did.
    with store_session() as store:
        store.touch_session("sess-fleet", state=mark)
    _session(work, "sess-fleet")
    assert _state("sess-fleet")[0] == "working"


def test_the_worker_refuses_a_hand_over_in_flight_or_one_that_just_happened(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two brakes in ``hand_over`` (review of #205, second round): a session already marked
    ``switching``, and a replacement row younger than ``HANDOVER_COOLDOWN`` that the last
    hand-over spawned. Each is a board line; ``fleet.switch`` is never reached."""
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)

    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a braked hand-over must not switch")

    monkeypatch.setattr(fleet_service, "switch", never)
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_inflight", "coder-db", "sess-fleet")
    with store_session() as store:
        store.touch_session("sess-fleet", state=team_service.HANDOVER_STATE)
    assert runner.invoke(app, ["hook", "hand-over", "sess-fleet"]).exit_code == 0
    assert any("not switched — a hand-over is already in flight" in t for _, t in _events(work))

    _session(work, "sess-moved")
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_justmoved",
                project_id=work.id,
                label="coder-db2",
                role="coder",
                pane_id="%4",
                session_id="sess-moved",
                cwd=work.root,
                spawned_by=hooks_service.HANDOVER_SPAWNER,
                created_at=datetime.now(tz=UTC) - timedelta(minutes=3),
            )
        )
    assert runner.invoke(app, ["hook", "hand-over", "sess-moved"]).exit_code == 0
    assert any(
        "coder-db2: not switched — moved 3 min ago; waiting for the reset" in t
        for _, t in _events(work)
    )

    # Past the cooldown a replacement is handed over again (the switch is reached); a row
    # of its own, because an upsert keeps the original row's created_at.
    reached: list[str] = []
    monkeypatch.setattr(fleet_service, "switch", lambda project, label, **kw: reached.append(label))
    _session(work, "sess-moved-long-ago")
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_movedlongago",
                project_id=work.id,
                label="coder-db3",
                role="coder",
                pane_id="%5",
                session_id="sess-moved-long-ago",
                cwd=work.root,
                spawned_by=hooks_service.HANDOVER_SPAWNER,
                created_at=datetime.now(tz=UTC) - hooks_service.HANDOVER_COOLDOWN,
            )
        )
    assert runner.invoke(app, ["hook", "hand-over", "sess-moved-long-ago"]).exit_code == 0
    assert reached == ["coder-db3"]


def test_the_detach_puts_the_worker_in_its_own_session_without_this_agents_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What ``_detach`` asks of Popen: no terminal, a new session, a clean environment."""
    calls: list[dict[str, Any]] = []

    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls.append({"argv": argv, **kwargs})

    monkeypatch.setattr("aisquare.services.hooks.subprocess.Popen", FakePopen)
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_me")
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "pipe-1")
    monkeypatch.setenv("AISQUARE_HOME", "/somewhere")

    hooks_service._detach(["python", "-m", "aisquare", "hook", "hand-over", "s"])

    [call] = calls
    assert call["argv"][-3:] == ["hook", "hand-over", "s"]
    assert call["start_new_session"] is True
    assert call["stdin"] is call["stdout"] is call["stderr"] is subprocess.DEVNULL
    env = call["env"]
    assert "AISQUARE_FLEET_AGENT" not in env and "AISQUARE_PIPELINE_ID" not in env
    assert env["AISQUARE_HOME"] == "/somewhere"  # the home travels; the identity does not


def test_the_detached_half_moves_the_agent_and_puts_a_refusal_on_the_board(
    fake_home: Path, work: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_service, "_nudge_manager", lambda project_id, *, reason: None)
    switches: list[tuple[str, str | None, str | None, bool | None]] = []

    def fake_switch(project: ProjectInfo, label: str, **kwargs: Any) -> None:
        switches.append(
            (label, kwargs.get("reason"), kwargs.get("spawned_by"), kwargs.get("automatic"))
        )

    monkeypatch.setattr(fleet_service, "switch", fake_switch)
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_limited3", "coder-db", "sess-fleet")

    moved = runner.invoke(app, ["hook", "hand-over", "sess-fleet", "--reason", "weekly limit"])
    assert moved.exit_code == 0 and moved.stdout == ""
    assert switches == [
        ("coder-db", "weekly limit", "usage-limit", True)
    ]  # automatic: no least-bad

    # A refusal is a board line, and the agent stays parked with its own wait intact.
    def refuse(project: ProjectInfo, label: str, **kwargs: Any) -> None:
        raise fleet_service.FleetError("no other account with headroom for 'coder-db'")

    monkeypatch.setattr(fleet_service, "switch", refuse)
    with store_session() as store:
        store.mark_limited("sess-fleet", None)
    refused = runner.invoke(app, ["hook", "hand-over", "sess-fleet"])
    assert refused.exit_code == 0 and refused.stdout == ""
    assert _state("sess-fleet")[0] == "limited"
    assert any("not switched — no other account with headroom" in t for _, t in _events(work))

    # A hand-typed session and an unknown one are nobody's to move: silent, nothing called.
    _session(work, "sess-hand")
    for session_id in ("sess-hand", "sess-none"):
        assert runner.invoke(app, ["hook", "hand-over", session_id]).exit_code == 0
    assert len(switches) == 1


# --------------------------------------------------------------------------- the pick


def test_headroom_picks_the_first_account_under_the_line_in_priority_order(
    fake_home: Path,
) -> None:
    work_slot = _slot("work@example.com", "tok-work")
    personal = _slot("personal@example.com", "tok-personal")
    spare = _slot("spare@example.com", "tok-spare")
    service.reorder(["3", "2", "4"])  # personal, work, spare
    fetch = _Usage(
        {"tok-work": _payload(20), "tok-personal": _payload(90), "tok-spare": _payload(5)}
    )

    picked, notes = service.headroom_choice(service.list_accounts(), switch_at=85, fetch=fetch)

    assert (
        picked is not None and picked.slot == work_slot.slot
    )  # first UNDER 85 in order, not lowest
    assert any("first under 85%" in note for note in notes)
    assert sorted(fetch.calls) == ["tok-personal", "tok-spare", "tok-work"]  # one read each
    # The control: slot 1 (no credentials in this home) was never a candidate.
    assert "tok" not in "".join(n for n in notes if "plain claude" in n)
    assert personal.slot == 3 and spare.slot == 4


def test_headroom_takes_the_most_room_when_every_account_is_over_the_line(
    fake_home: Path,
) -> None:
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    fetch = _Usage({"tok-work": _payload(96), "tok-personal": _payload(88)})

    picked, notes = service.headroom_choice(service.list_accounts(), switch_at=85, fetch=fetch)

    assert picked is not None and picked.slot == 3  # 12 % left beats 4 % left
    assert any("every account is over 85%" in note and "12% left" in note for note in notes)


def test_an_account_that_has_spent_its_week_has_no_headroom_however_empty_its_five_hours(
    fake_home: Path, work: ProjectInfo
) -> None:
    """Final review of #203, accounts F1: the pick read the five-hour window alone.

    An account that has spent its week builds no five-hour usage, so that window
    reads near 0 % once it rolls over and the account ranked first. The weekly
    limit is the case the automatic hand-over exists for, and it moved the agent
    onto an account that refused its first request; the cooldown then kept it
    parked until a reset days away. Each account is now as full as the fuller of
    its two windows, in the pick, the hand-over and the note that explains them.
    """
    _slot("left@example.com", "tok-left")  # slot 2: the account being left
    _slot("spent@example.com", "tok-spent")  # slot 3: first in order, its week gone
    _slot("room@example.com", "tok-room")  # slot 4: room in both windows
    fetch = _Usage(
        {
            "tok-left": _payload(95, week=100),
            "tok-spent": _payload(0, week=100),
            "tok-room": _payload(40, week=10),
        }
    )

    picked, notes = service.headroom_choice(
        service.list_accounts(), switch_at=85, exclude=[2], fetch=fetch, least_bad=False
    )
    assert picked is not None and picked.slot == 4
    assert any(
        "account 3 0% (week 100%)" in note and "account 4 is first under 85%" in note
        for note in notes
    ), notes

    handed = service.choose_for_handover(
        role="coder", project=work, exclude=(2,), automatic=True, fetch=fetch
    )
    assert handed.account is not None and handed.account.slot == 4

    # Every week spent: the hook's hand-over refuses rather than bounce the agent…
    spent = _Usage(
        {
            "tok-left": _payload(95, week=100),
            "tok-spent": _payload(0, week=100),
            "tok-room": _payload(40, week=99),
        }
    )
    refused = service.choose_for_handover(
        role="coder", project=work, exclude=(2,), automatic=True, fetch=spent
    )
    assert refused.account is None
    # …and by hand the least bad is judged on the fuller window too.
    by_hand = service.choose_for_handover(role="coder", project=work, exclude=(2,), fetch=spent)
    assert by_hand.account is not None and by_hand.account.slot == 4
    assert any("account 4 has the most room (1% left)" in note for note in by_hand.notes)


def test_headroom_skips_what_it_cannot_read_and_honours_disabled_and_exclude(
    fake_home: Path,
) -> None:
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    _slot("spare@example.com", "tok-spare")
    service.set_disabled("4", True)
    fetch = _Usage({"tok-work": 503, "tok-personal": _payload(50), "tok-spare": _payload(1)})

    picked, notes = service.headroom_choice(service.list_accounts(), switch_at=85, fetch=fetch)
    assert picked is not None and picked.slot == 3  # work unreadable, spare disabled
    assert any("account 2 skipped" in note and "HTTP 503" in note for note in notes)
    assert "tok-spare" not in fetch.calls  # disabled: never even asked

    excluded, _ = service.headroom_choice(
        service.list_accounts(), switch_at=85, exclude=[3], fetch=fetch
    )
    assert excluded is None  # the only readable one is excluded; nothing measured

    nothing, notes = service.headroom_choice(
        service.list_accounts(), switch_at=85, fetch=_Usage({})
    )
    assert nothing is None
    assert any("no account's usage could be read" in note for note in notes)


def test_choose_uses_headroom_only_when_configured_and_falls_back_to_the_default(
    fake_home: Path, work: ProjectInfo
) -> None:
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    service.set_default("2")
    fetch = _Usage({"tok-work": _payload(95), "tok-personal": _payload(10)})

    plain = service.choose(role="coder", project=work, fetch=fetch)
    assert plain.source == "machine default" and plain.account is not None
    assert plain.account.slot == 2 and fetch.calls == []  # pick=default: no request at all

    _settings(pick="headroom", switch_at=85)
    spread = service.choose(role="coder", project=work, fetch=fetch)
    assert spread.source == "headroom" and spread.account is not None
    assert spread.account.slot == 3
    assert spread.describe() == "account 3 · headroom"

    # A binding still outranks headroom: explicit beats automatic.
    from aisquare.services import settings as settings_service

    settings_service.bind_role("coder", account="2")
    assert service.choose(role="coder", project=work, fetch=fetch).source == "role binding"

    # Nothing readable: the default decides, with the notes saying why.
    offline = service.choose(role="tester", project=work, fetch=_Usage({}))
    assert offline.source == "machine default" and offline.account is not None
    assert offline.account.slot == 2
    assert any("no account's usage could be read" in note for note in offline.notes)


def test_a_hand_over_never_re_picks_the_account_it_is_leaving_on_an_arranged_machine(
    fake_home: Path, work: ProjectInfo
) -> None:
    """A binding or a project default naming the current slot is skipped with a note, and
    the ladder goes on to headroom (review of #205, finding 2)."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    fetch = _Usage({"tok-work": _payload(95), "tok-personal": _payload(10)})
    from aisquare.services import settings as settings_service

    settings_service.bind_role("coder", account="2")
    bound = service.choose(role="coder", project=work, exclude=(2,), spread=True, fetch=fetch)
    assert bound.account is not None and bound.account.slot == 3
    assert bound.source == "headroom"
    assert "account 2 (bound to coder) is the account being left — skipped" in bound.notes

    service.set_default("2", project=work)
    preferred = service.choose(role="tester", project=work, exclude=(2,), spread=True, fetch=fetch)
    assert preferred.account is not None and preferred.account.slot == 3
    assert "account 2 (project default) is the account being left — skipped" in preferred.notes

    # Without `exclude` both rungs still win, exactly as before.
    assert service.choose(role="coder", project=work, fetch=fetch).source == "role binding"
    assert service.choose(role="tester", project=work, fetch=fetch).source == "project default"


def test_choose_for_handover_asks_headroom_before_the_binding_and_refuses_when_automatic(
    fake_home: Path, work: ProjectInfo
) -> None:
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    from aisquare.services import settings as settings_service

    settings_service.bind_role("coder", account="2")
    service.set_default("2", project=work)
    room = _Usage({"tok-work": _payload(95), "tok-personal": _payload(10)})
    picked = service.choose_for_handover(role="coder", project=work, exclude=(2,), fetch=room)
    assert picked.account is not None and picked.account.slot == 3 and picked.source == "headroom"
    assert not any("bound to coder" in note for note in picked.notes)  # never consulted

    full = _Usage({"tok-work": _payload(95), "tok-personal": _payload(99)})
    by_hand = service.choose_for_handover(role="coder", project=work, exclude=(2,), fetch=full)
    assert by_hand.account is not None and by_hand.account.slot == 3  # the least bad, by hand
    automatic = service.choose_for_handover(
        role="coder", project=work, exclude=(2,), automatic=True, fetch=full
    )
    assert automatic.account is None
    assert any("every account is over 85%; nothing to switch to" in n for n in automatic.notes)

    # An iterator as `exclude` is spent by its first reader: normalised once (third round).
    lazy = service.choose_for_handover(
        role="coder", project=work, exclude=(slot for slot in (2,)), fetch=room
    )
    assert lazy.account is not None and lazy.account.slot == 3
    lazier = service.choose(
        role="tester", project=work, exclude=(slot for slot in (2,)), spread=True, fetch=room
    )
    assert lazier.account is not None and lazier.account.slot == 3  # never the excluded one

    # `--to` is still the flag rung; nothing readable falls to the ladder minus the current slot.
    assert (
        service.choose_for_handover("3", role="coder", project=work, exclude=(2,)).source == "flag"
    )
    service.set_default("3")
    blind = service.choose_for_handover(role="coder", project=work, exclude=(2,), fetch=_Usage({}))
    assert blind.account is not None and blind.account.slot == 3
    assert blind.source == "machine default"
    assert (
        service.choose_for_handover(
            role="coder", project=work, exclude=(2,), automatic=True, fetch=_Usage({})
        ).account
        is None
    )


def test_a_hand_over_picks_nothing_from_a_registry_it_cannot_read(
    fake_home: Path, work: ProjectInfo
) -> None:
    """Review of the #205 fold, round 1: with ``context.db`` unreadable, headroom ran over the
    bare directories, where a slot the operator disabled reads as enabled, and a hand-over
    moved the agent onto it. ``choose`` already picked nothing there; nor does this now."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    service.set_disabled("3", True)
    room = _Usage({"tok-work": _payload(95), "tok-personal": _payload(10)})
    readable = service.choose_for_handover(role="coder", project=work, exclude=(2,), fetch=room)
    assert readable.account is None  # the control: disabled is never the pick
    paths.db_path().write_bytes(b"this is not a sqlite database, and the hand-over must say so")

    for automatic in (False, True):
        choice = service.choose_for_handover(
            role="coder", project=work, exclude=(2,), automatic=automatic, fetch=room
        )
        assert choice.account is None and choice.source is None
        assert any("accounts registry unreadable" in note for note in choice.notes)
    # `--to` still names one: the flag rung does not check, as for a launch.
    named = service.choose_for_handover("3", role="coder", project=work, exclude=(2,))
    assert named.account is not None and named.account.slot == 3 and named.source == "flag"


def test_one_launch_reads_the_registry_and_the_settings_once(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag and the binding resolve against the one read (review of #205, finding 12)."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    fetch = _Usage({"tok-work": _payload(95), "tok-personal": _payload(10)})
    from aisquare.services import settings as settings_service

    settings_service.bind_role("coder", account="2")
    _settings(pick="headroom", switch_at=85)
    reads: list[int] = []
    real_read = service._read_registry

    def counted_read(
        project: ProjectInfo | None = None,
    ) -> tuple[list[ClaudeAccount], str | None, int | None]:
        reads.append(1)
        return real_read(project)

    loads: list[int] = []
    real_settings = service.accounts_settings

    def counted_settings() -> AccountsSettings:
        loads.append(1)
        return real_settings()

    monkeypatch.setattr(service, "_read_registry", counted_read)
    monkeypatch.setattr(service, "accounts_settings", counted_settings)

    assert service.choose(role="coder", project=work, fetch=fetch).source == "role binding"
    assert len(reads) == 1 and loads == []  # the binding decided: no settings needed
    reads.clear()
    assert service.choose("3", role="coder", project=work, fetch=fetch).source == "flag"
    assert len(reads) == 1
    reads.clear()
    assert service.choose(role="tester", project=work, fetch=fetch).source == "headroom"
    assert len(reads) == 1 and len(loads) == 1


# --------------------------------------------------------------------------- the trend


def test_samples_are_recorded_and_the_trend_says_how_long_the_window_has(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _slot("work@example.com", "tok-work")
    resets = NOW + timedelta(hours=4)

    first = service.sample_usage(
        account, now=NOW, fetch=_Usage({"tok-work": _payload(40, resets_at=resets)})
    )
    assert first.available
    assert service.usage_trend(account.slot, first, now=NOW) is not None
    single = service.usage_trend(account.slot, first, now=NOW)
    assert single is not None and single.per_hour is None  # one reading: no rate

    later = NOW + timedelta(minutes=30)
    second = service.sample_usage(
        account, now=later, fetch=_Usage({"tok-work": _payload(50, resets_at=resets)})
    )
    trend = service.usage_trend(account.slot, second, now=later)
    assert trend is not None and trend.per_hour == pytest.approx(20.0)  # 10 points in 30 min
    assert trend.minutes_to_limit == pytest.approx(150.0)  # 50 points left at 20/h
    assert trend.span_minutes == pytest.approx(30.0)
    monkeypatch.setattr(service, "_now", lambda: later)
    assert service.describe_trend(trend) == "≈ 2.5 h to the limit"

    # A reading from BEFORE the window reset is not part of this window's trend.
    after_reset = later + timedelta(hours=5)
    fresh = service.sample_usage(
        account,
        now=after_reset,
        fetch=_Usage({"tok-work": _payload(5, resets_at=resets + timedelta(hours=5))}),
    )
    restarted = service.usage_trend(account.slot, fresh, now=after_reset)
    assert restarted is not None and restarted.per_hour is None

    # A window that resets before it would fill says so rather than promising a limit.
    soon = later + timedelta(minutes=10)
    near_reset = service.sample_usage(
        account,
        now=soon,
        fetch=_Usage({"tok-work": _payload(55, resets_at=later + timedelta(minutes=20))}),
    )
    monkeypatch.setattr(service, "_now", lambda: soon)
    steep = service.usage_trend(account.slot, near_reset, now=soon)
    # Rated against the oldest same-window sample — but the reset time changed, so it is
    # its own window: only readings sharing THIS resets_at count.
    assert steep is not None
    with store_session() as store:
        assert len(store.usage_samples(account.slot, since=NOW - timedelta(days=1))) == 4


def test_the_trend_survives_the_endpoints_jitter_in_resets_at(fake_home: Path) -> None:
    """Consecutive readings of one window differ by a fraction of a second (measured live);
    compared for equality, no trend was ever computed (review of #205, finding 3)."""
    account = _slot("work@example.com", "tok-work")
    resets = NOW + timedelta(hours=4)
    service.sample_usage(
        account, now=NOW, fetch=_Usage({"tok-work": _payload(40, resets_at=resets)})
    )
    later = NOW + timedelta(minutes=30)
    jittered = resets + timedelta(milliseconds=400)  # 08:59:59.86 → 09:00:00.26
    second = service.sample_usage(
        account, now=later, fetch=_Usage({"tok-work": _payload(50, resets_at=jittered)})
    )

    trend = service.usage_trend(account.slot, second, now=later)

    assert trend is not None and trend.per_hour == pytest.approx(20.0)
    assert trend.minutes_to_limit == pytest.approx(150.0)
    # The rule itself: a minute of jitter is the same window; the next window is not.
    assert service._same_window(resets, resets + service.RESET_JITTER)
    assert not service._same_window(resets, resets + timedelta(hours=5))
    assert service._same_window(None, None) and not service._same_window(resets, None)


def test_describe_trend_words(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisquare.models import UsageTrend

    monkeypatch.setattr(service, "_now", lambda: NOW)
    assert service.describe_trend(None) == ""
    assert service.describe_trend(UsageTrend(percent=10)) == ""  # no rate yet
    assert service.describe_trend(UsageTrend(percent=10, per_hour=0.0)) == "flat"
    assert service.describe_trend(UsageTrend(percent=10, per_hour=-3.0)) == "falling"
    assert (
        service.describe_trend(UsageTrend(percent=80, per_hour=40.0, minutes_to_limit=30.0))
        == "≈ 30 min to the limit"
    )
    assert (
        service.describe_trend(
            UsageTrend(
                percent=80,
                per_hour=40.0,
                minutes_to_limit=30.0,
                resets_at=NOW + timedelta(minutes=10),
            )
        )
        == "resets before the limit"
    )


def test_accounts_usage_prints_the_pace_and_records_the_reading(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(40)})
    monkeypatch.setattr(service, "_http_get", fetch)

    first = runner.invoke(app, ["accounts", "usage", "2"])
    assert first.exit_code == 0, first.output
    with store_session() as store:
        assert len(store.usage_samples(account.slot, since=NOW - timedelta(days=1))) == 1


def test_accounts_usage_and_list_usage_read_every_account_in_one_round_with_labels(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two CLI usage surfaces go through ``read_usage`` (one round trip) and the arranged
    list — priority order, the alias, ``(disabled)`` — like every other surface (third round)."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    service.set_alias("2", "work")
    service.set_disabled("3", True)
    service.reorder(["3", "2"])
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-work": _payload(40), "tok-personal": _payload(60)})
    )
    rounds: list[list[int]] = []
    real = service.read_usage

    def spy(accounts: Any, **kwargs: Any) -> dict[int, Any]:
        rounds.append([account.slot for account in accounts])
        return real(accounts, **kwargs)

    monkeypatch.setattr(service, "read_usage", spy)

    usage = runner.invoke(app, ["accounts", "usage"])
    assert usage.exit_code == 0, usage.output
    assert rounds == [[3, 2]]  # one round, in priority order
    lines = [line for line in usage.stdout.splitlines() if line.strip()]
    assert lines[1].startswith("3") and "(disabled)" in lines[1] and "60%" in lines[1]
    assert lines[2].startswith("2") and "work" in lines[2] and "40%" in lines[2]

    rounds.clear()
    listing = runner.invoke(app, ["accounts", "list", "--usage"])
    assert listing.exit_code == 0, listing.output
    assert rounds == [[3, 2]]


def test_reorder_and_move_read_the_registry_once(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each reference used to reopen the store and rescan the directories (third round)."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    reads: list[int] = []
    real = service._read_registry

    def counted(project: ProjectInfo | None = None) -> Any:
        reads.append(1)
        return real(project)

    monkeypatch.setattr(service, "_read_registry", counted)
    assert [a.slot for a in service.reorder(["3", "work@example.com", "1"])] == [3, 2, 1]
    assert len(reads) == 1
    reads.clear()
    assert [a.slot for a in service.move("1", "top")] == [1, 3, 2]
    assert len(reads) == 1


def test_the_pages_tick_opens_the_store_once_for_every_sample_and_trend(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight opens a minute for four accounts before: one now (third round)."""
    import contextlib

    a = _slot("work@example.com", "tok-work")
    b = _slot("personal@example.com", "tok-personal")
    fetch = _Usage({"tok-work": _payload(40), "tok-personal": _payload(60)})
    service.sample_usage(
        a, now=NOW - timedelta(minutes=30), fetch=_Usage({"tok-work": _payload(20)})
    )
    opens: list[int] = []
    real_session = store_session

    @contextlib.contextmanager
    def counted_session() -> Any:
        opens.append(1)
        with real_session() as store:
            yield store

    monkeypatch.setattr("aisquare.services.claude_accounts.store_session", counted_session)
    fetched = service.read_usage_with_trends([a, b], now=NOW, fetch=fetch)

    assert opens == [1]
    assert fetched[a.slot][0].session_percent == 40 and fetched[b.slot][0].session_percent == 60
    trend = fetched[a.slot][1]
    assert trend is not None and trend.per_hour == pytest.approx(40.0)  # 20 → 40 in 30 min
    with store_session() as store:
        assert len(store.usage_samples(a.slot, since=NOW - timedelta(days=1))) == 2
        assert len(store.usage_samples(b.slot, since=NOW - timedelta(days=1))) == 1


def test_a_recording_usage_round_opens_the_store_once_on_the_callers_thread(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``read_usage`` recorded from each pool thread — every headroom pick, ``accounts usage``,
    ``list --usage`` and ``doctor --live``: up to four concurrent writers on ``context.db``,
    on the launch path (review of #205, fourth round). The threads fetch; one session
    records every answered reading, and an unanswered one leaves no row."""
    import contextlib
    import threading

    a = _slot("work@example.com", "tok-work")
    b = _slot("personal@example.com", "tok-personal")
    c = _slot("third@example.com", "tok-third")
    fetch = _Usage({"tok-work": _payload(40), "tok-personal": _payload(60), "tok-third": 401})
    opens: list[str] = []
    real_session = store_session

    @contextlib.contextmanager
    def counted_session() -> Any:
        opens.append(threading.current_thread().name)
        with real_session() as store:
            yield store

    monkeypatch.setattr("aisquare.services.claude_accounts.store_session", counted_session)
    readings = service.read_usage([a, b, c], now=NOW, fetch=fetch)

    assert opens == [threading.current_thread().name]
    assert readings[a.slot].session_percent == 40 and readings[b.slot].session_percent == 60
    assert not readings[c.slot].available
    since = NOW - timedelta(days=1)
    with store_session() as store:
        assert len(store.usage_samples(a.slot, since=since)) == 1
        assert len(store.usage_samples(b.slot, since=since)) == 1
        assert store.usage_samples(c.slot, since=since) == []
    opens.clear()
    service.read_usage([a], now=NOW, fetch=fetch, record=False)
    assert opens == []  # the page's path records in a session of its own


def test_the_board_and_watch_name_an_aliased_slot_1_as_the_rest_does(
    fake_home: Path, work: ProjectInfo
) -> None:
    """Review of #205, fourth round: ``account_label`` read the slot with ``managed_slot``,
    which knows only the directories under ``accounts_root``, so slot 1 — the plain
    claude's own directory — was ``.claude`` on the board and in ``watch`` while
    ``fleet ls`` and the agent header said its alias."""
    from aisquare.cli.watch import _session_lines

    two = _slot("work@example.com", "tok-work")
    service.set_alias("1", "personal")
    plain_dir = str(core.default_config_dir())
    now = datetime.now(tz=UTC)
    sessions = [
        TeamSession(
            id=f"sess-{name}",
            project_id=work.id,
            role="coder",
            started_at=now,
            last_seen_at=now,
            account=account,
        )
        for name, account in (("plain", plain_dir), ("work", str(two.config_dir)))
    ]
    labels = service.slot_labels()

    assert team_service.account_label(plain_dir, labels) == "personal"
    assert team_service.account_label(plain_dir) == "plain claude"  # the built-in name, unlabelled
    rendered = _session_lines(sessions, labels).plain
    assert "personal" in rendered and ".claude" not in rendered
    block = team_service._render_board(work, sessions, [], [], me=None, labels=labels)
    assert "[personal]" in block and "[.claude]" not in block


def test_the_board_a_hook_renders_under_a_managed_slot_names_the_aliased_slot_1_too(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #205, fifth round: ``slot_of`` knew slot 1 by comparing with
    ``default_config_dir()``, which honours ``CLAUDE_CONFIG_DIR`` — and every hook of an
    agent on a managed slot runs with that variable naming ITS slot (``launch_env``). So the
    board those hooks render, the one the agents read, still showed an aliased slot 1 as
    ``.claude``; the previous test renders from a plain shell and could not see it."""
    two = _slot("work@example.com", "tok-work")
    service.set_alias("1", "personal")
    plain = fake_home / ".claude"

    def transcript(config_dir: Path, session_id: str) -> str:
        return str(config_dir / "projects" / "-repo" / f"{session_id}.jsonl")

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start(
        "sess-plain", work.root, "startup", transcript_path=transcript(plain, "sess-plain")
    )
    monkeypatch.setenv(core.CONFIG_DIR_VAR, str(two.config_dir))  # an agent on slot 2's hook
    board = team_service.hook_session_start(
        "sess-work", work.root, "startup", transcript_path=transcript(two.config_dir, "sess-work")
    )

    assert "[personal]" in board and "[.claude]" not in board
    assert service.slot_of(plain) == 1 and service.slot_of(two.config_dir) == 2
    assert service.slot_of(fake_home / ".claude-c2") is None  # a hand-made layout is no slot
    # A variable that is the operator's own, not one of our slots, still names the plain claude.
    monkeypatch.setenv(core.CONFIG_DIR_VAR, str(fake_home / ".claude-c2"))
    assert service.slot_of(fake_home / ".claude-c2") == 1 and service.slot_of(plain) is None


def test_the_board_block_and_watch_name_the_account_as_the_rest_does(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alias reaches the injected board (through the hook's own store handle) and
    `aisquare watch` (third round)."""
    from aisquare.cli.watch import _session_lines

    two = _slot("work@example.com", "tok-work")
    three = _slot("personal@example.com", "tok-personal")
    service.set_alias("2", "work")
    now = datetime.now(tz=UTC)
    sessions = [
        TeamSession(
            id=f"sess-on-{account.slot}",
            project_id=work.id,
            role="coder",
            started_at=now,
            last_seen_at=now,
            account=str(account.config_dir),
        )
        for account in (two, three)
    ]
    assert (
        "work" in _session_lines(sessions).plain and "account 3" in _session_lines(sessions).plain
    )

    with store_session() as store:
        labels = team_service.slot_labels_via(store)  # no second connection under the hook's own

        def no_second_connection() -> Any:
            raise AssertionError("a second connection under the hook's own")

        monkeypatch.setattr("aisquare.services.claude_accounts.store_session", no_second_connection)
        again = team_service.slot_labels_via(store)
    assert labels == {1: "plain claude", 2: "work", 3: "account 3"} and again == labels
    block = team_service._render_board(work, sessions, [], [], me=None, labels=labels)
    assert "[work]" in block and "[account 3]" in block and "[account 2]" not in block


def test_a_board_on_one_account_reads_no_labels(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the #205 fold, round 1: every SessionStart read the slot labels — a store
    read and a directory scan — for a label only a board spanning several accounts prints.
    Read only then now, as ``watch`` already did."""
    two = _slot("work@example.com", "tok-work")
    reads: list[int] = []
    real = service.slot_labels

    def counted(store: Any = None) -> dict[int, str]:
        reads.append(1)
        return real(store=store)

    monkeypatch.setattr(service, "slot_labels", counted)

    def transcript(config_dir: Path, session_id: str) -> str:
        return str(config_dir / "projects" / "-repo" / f"{session_id}.jsonl")

    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    alone = team_service.hook_session_start(
        "sess-plain", work.root, "startup", transcript_path=transcript(fake_home / ".claude", "p")
    )
    assert reads == [] and "[plain claude]" not in alone
    with store_session() as store:
        sessions = store.team_sessions(work.id)
    team_service.render_board(work, sessions, [], [])  # `asq board` too
    assert reads == []

    # A second account live: the labels are shown, so they are read.
    monkeypatch.setenv(core.CONFIG_DIR_VAR, str(two.config_dir))
    both = team_service.hook_session_start(
        "sess-work", work.root, "startup", transcript_path=transcript(two.config_dir, "w")
    )
    assert reads == [1] and "[plain claude]" in both and "[account 2]" in both


def test_a_removed_slots_readings_do_not_rate_the_next_occupant(fake_home: Path) -> None:
    """``forget_arrangement`` drops the slot's ``claude_usage`` rows too (third round)."""
    account = _slot("work@example.com", "tok-work")
    service.sample_usage(account, now=NOW, fetch=_Usage({"tok-work": _payload(70)}))
    with store_session() as store:
        assert len(store.usage_samples(account.slot, since=NOW - timedelta(days=1))) == 1

    service.forget_arrangement(account.slot)

    with store_session() as store:
        assert store.usage_samples(account.slot, since=NOW - timedelta(days=1)) == []


# --------------------------------------------------------------------------- doctor


def test_doctor_names_limited_agents_and_is_silent_without_any(
    fake_home: Path, work: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    core.create_account()
    assert not any(c.name == "claude-account-limits" for c in diagnostics._claude_accounts_checks())
    _session(work, "sess-fleet")
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_limited4",
                project_id=work.id,
                label="coder-auth",
                role="coder",
                pane_id="%2",
                session_id="sess-fleet",
                cwd=work.root,
                created_at=datetime.now(tz=UTC),
            )
        )
        store.mark_limited("sess-fleet", datetime.now(tz=UTC) + timedelta(hours=2))

    checks = {c.name: c for c in diagnostics._claude_accounts_checks()}

    limits = checks["claude-account-limits"]
    assert limits.status.value == "warn"
    assert "coder-auth" in limits.detail and "resets" in limits.detail
    assert limits.fix is not None and "aisquare fleet switch <label>" in limits.fix


def test_doctor_live_headroom_warns_only_when_every_account_is_over_the_line(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-work": _payload(90), "tok-personal": _payload(30)})
    )
    assert diagnostics._claude_account_headroom_check() is None  # no store yet: nothing created
    service.list_accounts()  # the registry exists from here on
    check = diagnostics._claude_account_headroom_check()
    assert check is not None and check.status.value == "ok"
    assert "work@" not in check.detail and "account 2 90%" in check.detail

    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-work": _payload(90), "tok-personal": _payload(86)})
    )
    check = diagnostics._claude_account_headroom_check()
    assert check is not None and check.status.value == "warn"
    assert "every account is at or over 85%" in check.detail

    monkeypatch.setattr(service, "_http_get", _Usage({}))
    check = diagnostics._claude_account_headroom_check()
    assert check is not None and check.status.value == "warn"
    assert "could be read" in check.detail

    # Offline doctor never reaches it; --live does (the wiring, not just the function).
    offline = [c.name for c in diagnostics.doctor(live=False)]
    assert "claude-account-headroom" not in offline


def test_doctor_live_headroom_counts_a_spent_week_as_over_the_line(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor's row applies the pick's rule (final review of #203, accounts F1): an
    account whose week is spent read "ok" on its empty five-hour window."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    monkeypatch.setattr(
        service,
        "_http_get",
        _Usage({"tok-work": _payload(90), "tok-personal": _payload(0, week=100)}),
    )
    service.list_accounts()  # the registry exists: doctor may read it

    check = diagnostics._claude_account_headroom_check()

    assert check is not None and check.status.value == "warn", check
    assert "every account is at or over 85%" in check.detail
    assert "account 3 0% (week 100%)" in check.detail


def test_doctor_live_and_the_page_read_every_account_in_one_round(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both go through ``read_usage``, once, with every account (review of #205, finding 11)."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-work": _payload(10), "tok-personal": _payload(20)})
    )
    rounds: list[list[int]] = []
    real = service.read_usage

    def spy(accounts: Any, **kwargs: Any) -> dict[int, Any]:
        rounds.append([account.slot for account in accounts])
        return real(accounts, **kwargs)

    monkeypatch.setattr(service, "read_usage", spy)
    service.list_accounts()  # the registry exists: doctor may read it

    check = diagnostics._claude_account_headroom_check()
    assert check is not None and check.status.value == "ok" and rounds == [[2, 3]]

    rounds.clear()
    accounts = [a for a in service.list_accounts() if a.slot != 1]
    fetched = service.read_usage_with_trends(accounts)  # what the page's usage worker runs
    assert rounds == [[2, 3]]
    assert fetched[2][0].session_percent == 10 and fetched[3][0].session_percent == 20
    assert fetched[2][1] is not None  # the reading was recorded, so a trend object exists


def test_the_accounts_section_defaults_and_round_trips_through_config_set(
    runner: CliRunner,
) -> None:
    assert AppConfig().accounts == AccountsSettings(
        pick="default", switch_at=85, on_limit="wait", wait_if_reset_within_minutes=15
    )
    assert service.accounts_settings().pick == "default"  # no file: the defaults

    result = runner.invoke(app, ["config", "set", "accounts.pick", "headroom"])
    assert result.exit_code == 0, result.output
    assert load_config().accounts.pick == "headroom"
    assert service.accounts_settings().pick == "headroom"
    bad = runner.invoke(app, ["config", "set", "accounts.pick", "sometimes"])
    assert bad.exit_code != 0 and load_config().accounts.pick == "headroom"
    limit = runner.invoke(app, ["config", "set", "accounts.on_limit", "switch"])
    assert limit.exit_code == 0 and load_config().accounts.on_limit == "switch"
    shown = runner.invoke(app, ["config", "get", "accounts.switch_at"])
    assert shown.stdout.strip() == "accounts.switch_at = 85"
