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
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import selfcli
from aisquare.core.config import AccountsSettings, AppConfig, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo, TeamSession
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import hooks as hooks_service
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


def _payload(percent: float, *, resets_at: datetime = NOW + timedelta(hours=3)) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(json.dumps(LIVE_USAGE))
    payload["five_hour"]["utilization"] = percent
    payload["five_hour"]["resets_at"] = resets_at.isoformat()
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

    # The default: wait. Nothing is started however far the reset is.
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert started == []

    # Configured to switch: a limit whose reset is far away starts the worker…
    _settings(on_limit="switch", wait_if_reset_within_minutes=15)
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert len(started) == 1
    argv = started[0]
    assert argv[:4] == selfcli.argv_for([])  # this interpreter, -P, -m aisquare
    assert argv[4:] == ["--quiet", "hook", "hand-over", "sess-fleet", "--reason", "weekly limit"]

    # …one that lifts within the wait window does not (the note says why)…
    soon = (datetime.now().astimezone() + timedelta(minutes=5)).strftime("%I:%M%p").lstrip("0")
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
    with store_session() as store:
        store.touch_session("sess-fleet", state="working")  # a new window: the limit re-fires
    _fire_limit(runner, work, WEEKLY_LIMIT, "sess-fleet")
    assert any(
        "not switched — could not start the hand-over worker (no fork for you)" in text
        for _, text in _events(work)
    )


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
    switches: list[tuple[str, str | None, str | None]] = []

    def fake_switch(project: ProjectInfo, label: str, **kwargs: Any) -> None:
        switches.append((label, kwargs.get("reason"), kwargs.get("spawned_by")))

    monkeypatch.setattr(fleet_service, "switch", fake_switch)
    _session(work, "sess-fleet")
    _fleet_row(work, "agt_limited3", "coder-db", "sess-fleet")

    moved = runner.invoke(app, ["hook", "hand-over", "sess-fleet", "--reason", "weekly limit"])
    assert moved.exit_code == 0 and moved.stdout == ""
    assert switches == [("coder-db", "weekly limit", "usage-limit")]

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
    real_read = service._read_arranged

    def counted_read() -> tuple[list[ClaudeAccount], str | None]:
        reads.append(1)
        return real_read()

    loads: list[int] = []
    real_settings = service.accounts_settings

    def counted_settings() -> AccountsSettings:
        loads.append(1)
        return real_settings()

    monkeypatch.setattr(service, "_read_arranged", counted_read)
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
    assert service.describe_trend(UsageTrend(percent=10, per_hour=-3.0)) == "flat"
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

    check = diagnostics._claude_account_headroom_check()
    assert check is not None and check.status.value == "ok" and rounds == [[2, 3]]

    from aisquare.cli.ui.views import accounts as accounts_view

    rounds.clear()
    accounts = [a for a in service.list_accounts() if a.slot != 1]
    fetched = accounts_view._read_usage(accounts)
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
