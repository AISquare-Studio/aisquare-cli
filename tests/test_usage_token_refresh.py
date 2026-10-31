"""A stale login is refreshed quietly before the row says signed out (task #9 on the board).

The owner: an account no agent ran on lately read as "not signed in" and offered *Log in*,
though nothing was wrong with it — the access token expires within hours and only Claude
Code renews it, on the account's next session. Pressing *Log in* landed in a session with
nothing to do. Now a read that finds the token expired, or refused (HTTP 401), has Claude
Code refresh it (``claude mcp get`` of a server no one has, whose startup renews the token
and waits for it, with no model request) and reads again; failing that, once more a minute
on; only then does it say signed out. In between, the row shows the last good reading.

Every path against a scripted Claude Code (:class:`FakeClaude`) that rewrites the account's
credentials as a real refresh does, and a scripted usage endpoint; no ``claude`` runs:

1. expired + a refresh that takes: one read shows fresh figures, never a *Log in*;
2. a refresh that does not take, then a retry at >= 60 s that does: the kept reading, aged,
   in between, never signed out;
3. both fail: only then *Log in*, with the reason and fix it always had;
4. one refresh at a time per account: repaints, polls and other processes during the wait
   start none; disabled accounts, accounts never signed in and Keychain causes are as before.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.accounts import (
    account_cells,
    kept_tooltip,
    needs_login,
    problem_tooltip,
)
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ClaudeInstall, ClaudeUsage, UsageCause
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import NOW, _sign_in
from tests.test_usage_aware_accounts import _payload, _slot, _Usage
from tests.test_usage_backoff import _new_process

#: The real runner and command, before ``conftest.no_token_refresh`` stubs them per test.
REAL_RUN = service._run_token_refresh
REAL_COMMAND = service._token_refresh_command

_REFRESH_TOKEN = (
    "press Log in, or open a session on this account (aisquare accounts run {slot}): "
    "Claude Code refreshes the token"
)


def _renew(account: ClaudeAccount, token: str) -> None:
    """What a refresh by Claude Code leaves: a new access token, hours from expiring."""
    path = core.credentials_path(account)
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["accessToken"] = token
    data["claudeAiOauth"]["expiresAt"] = int((NOW + timedelta(hours=8)).timestamp() * 1000)
    path.write_text(json.dumps(data))


class FakeClaude:
    """Claude Code, scripted: each run renews the account's token or leaves it as it was
    (``takes``, in order; the last repeats), and is counted. No process starts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *takes: bool) -> None:
        self.takes = list(takes) or [True]
        self.runs: list[int] = []
        self.before: threading.Event | None = None  # set: each run waits for it
        self.started = threading.Event()
        command = ["/opt/bin/claude", *service.TOKEN_REFRESH_ARGS]
        monkeypatch.setattr(service, "_token_refresh_command", lambda: command)
        monkeypatch.setattr(service, "_run_token_refresh", self)

    def __call__(self, account: ClaudeAccount, command: Sequence[str]) -> bool:
        assert list(command[1:]) == ["mcp", "get", "aisquare token refresh"]
        self.runs.append(account.slot)
        self.started.set()
        if self.before is not None:
            assert self.before.wait(10), "the test never let the refresh finish"
        take = self.takes[min(len(self.runs), len(self.takes)) - 1]
        if take:
            _renew(account, f"tok-renewed-{len(self.runs)}")
        return True


def _expired(email: str = "work@example.com") -> ClaudeAccount:
    """A signed-in slot whose token ``tok-old`` expired an hour before NOW."""
    return _slot(email, "tok-old", expires_in=timedelta(hours=-1))


def _status(account: ClaudeAccount) -> Any:
    return service.describe(account)


def _row(account: ClaudeAccount) -> dict[str, Any] | None:
    with store_session() as store:
        raw = store.ui_state(service._TOKEN_REFRESH_KEY.format(slot=account.slot))
    return json.loads(raw) if raw else None


def _read_before_it_expired(account: ClaudeAccount, endpoint: _Usage, percent: float) -> None:
    """A good reading on record for ``tok-old``, taken two hours ago while it still held."""
    endpoint.by_token["tok-old"] = _payload(percent)
    taken = service.cached_usage([account], now=NOW - timedelta(hours=2), fetch=endpoint)
    assert taken[account.slot].session_percent == percent
    endpoint.by_token.pop("tok-old")
    _new_process()


# --------------------------------------------------------------------------- 1. it takes


def test_an_expired_token_is_refreshed_and_one_read_shows_fresh_figures(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    claude = FakeClaude(monkeypatch, True)
    endpoint = _Usage({"tok-renewed-1": _payload(55)})
    status = _status(work)
    assert status.token_state == "expired"
    assert not needs_login(status, None), "owed: no Log in before the reading says so"

    reading = service.cached_usage([work], now=NOW, fetch=endpoint)[work.slot]

    assert claude.runs == [work.slot], "Claude Code refreshed it, once"
    assert reading.available and reading.session_percent == 55 and not reading.kept
    assert endpoint.calls == ["tok-renewed-1"], "the expired token itself was never sent"
    assert _row(work) is None, "nothing left to wait for"
    # The row as the page paints it, with the frame described before the refresh:
    assert not needs_login(status, reading) and problem_tooltip(status, reading) is None
    assert account_cells(status, reading, now=NOW)["session"].plain.endswith("55%")
    assert service.usage_is_readable(status, reading)
    # Recorded for the new token: the next read is the cache's, and asks nothing.
    again = service.cached_usage([work], now=NOW + timedelta(minutes=1), fetch=endpoint)
    assert again[work.slot].session_percent == 55 and endpoint.calls == ["tok-renewed-1"]
    assert claude.runs == [work.slot]


def test_a_rejected_token_is_refreshed_too(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 on a token that has not expired: the same flow; read again after it, the login
    Claude Code holds now (another session's, say) is what is sent."""
    work = _slot("work@example.com", "tok-old")
    claude = FakeClaude(monkeypatch, True)
    endpoint = _Usage({"tok-old": 401, "tok-renewed-1": _payload(30)})

    reading = service.read_usage([work], now=NOW, fetch=endpoint)[work.slot]

    assert claude.runs == [work.slot]
    assert endpoint.calls == ["tok-old", "tok-renewed-1"]
    assert reading.available and reading.session_percent == 30


def test_accounts_usage_never_says_expired_when_the_refresh_takes(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _expired()
    FakeClaude(monkeypatch, True)
    monkeypatch.setattr(service, "_http_get", _Usage({"tok-renewed-1": _payload(55)}))

    shown = runner.invoke(app, ["accounts", "usage"])

    assert shown.exit_code == 0, shown.output
    work_row = next(line for line in shown.stdout.splitlines() if "work@example.com" in line)
    assert "55%" in work_row and "Log in" not in work_row, shown.stdout
    assert "expired" not in shown.stdout, "slot 1 (never signed in) is the only Log in"
    rows = json.loads(runner.invoke(app, ["--json", "accounts", "usage"]).stdout)
    usage = next(row["usage"] for row in rows if row["account"]["slot"] == 2)
    assert usage["available"] and not usage["needs_login"] and not usage["kept"]


# --------------------------------------------------------------------------- 2. the retry


def test_a_refresh_that_does_not_take_keeps_the_last_reading_and_retries_after_a_minute(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    endpoint = _Usage({"tok-renewed-2": _payload(70)})
    _read_before_it_expired(work, endpoint, 40)
    claude = FakeClaude(monkeypatch, False, True)
    status = _status(work)

    first = service.cached_usage([work], now=NOW, fetch=endpoint)[work.slot]

    retry_at = NOW + timedelta(seconds=service.TOKEN_REFRESH_RETRY_SECONDS)
    assert claude.runs == [work.slot]
    assert first.available and first.kept, "the last good reading, never signed out"
    assert first.session_percent == 40 and first.fetched_at == NOW - timedelta(hours=2)
    assert not first.needs_login and first.last_failed_cause is UsageCause.token_refreshing
    assert first.refresh_retry_at == retry_at
    clock = retry_at.astimezone().strftime("%H:%M")
    assert first.last_failed_reason == (
        f"the stored token has expired — Claude Code's refresh did not take, once more at {clock}"
    )
    # The page: the figure, aged, why on hover, and no Log in.
    assert not needs_login(status, first) and problem_tooltip(status, first) is None
    assert account_cells(status, first, now=NOW)["session"].plain.endswith("40% · 2h")
    assert kept_tooltip(first, slot=work.slot, now=NOW) == (
        f"reading from 2h ago · {first.last_failed_reason}\n"
        f"fix: none needed yet, Claude Code refreshes the token at {clock} "
        "(Log in is offered if that fails)"
    )
    assert service.fleet_headroom([work], {work.slot: first}).needs_login == 0

    # Repaints, polls, a forced refresh and other processes during the minute start none.
    for seconds in (1, 30, 59):
        moment = NOW + timedelta(seconds=seconds)
        for force in (False, True):
            shown = service.cached_usage([work], now=moment, force=force, fetch=endpoint)
            assert shown[work.slot].kept and shown[work.slot].session_percent == 40
            assert not needs_login(status, shown[work.slot])
        _new_process()
        live = service.read_usage([work], now=moment, fetch=endpoint)[work.slot]
        assert live.cause is UsageCause.token_refreshing and not live.needs_login
    assert claude.runs == [work.slot], "no other refresh during the wait"

    retried = service.cached_usage([work], now=retry_at, fetch=endpoint)[work.slot]

    assert claude.runs == [work.slot, work.slot], "the one retry, at the minute"
    assert retried.available and not retried.kept and retried.session_percent == 70
    assert _row(work) is None


def test_a_rejected_tokens_retry_is_made_by_the_pages_next_tick_not_five_minutes_on(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 on an unexpired token is remembered by the five-minute cache, as any failed fetch
    with a token is: the read due at the retry is not answered from that memory."""
    work = _slot("work@example.com", "tok-old")
    endpoint = _Usage({"tok-old": 401, "tok-renewed-2": _payload(20)})
    claude = FakeClaude(monkeypatch, False, True)

    first = service.cached_usage([work], now=NOW, fetch=endpoint)[work.slot]
    assert first.cause is UsageCause.token_refreshing and claude.runs == [work.slot]
    asked = len(endpoint.calls)
    served = service.cached_usage([work], now=NOW + timedelta(seconds=30), fetch=endpoint)
    assert served[work.slot].cause is UsageCause.token_refreshing
    assert len(endpoint.calls) == asked, "inside the minute: from memory, nothing sent"

    tick = service.cached_usage([work], now=NOW + timedelta(seconds=61), fetch=endpoint)

    assert claude.runs == [work.slot, work.slot]
    assert tick[work.slot].session_percent == 20


def test_a_refresh_running_in_another_process_is_waited_for_not_doubled(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    endpoint = _Usage({"tok-renewed-1": _payload(55)})
    _read_before_it_expired(work, endpoint, 40)
    claude = FakeClaude(monkeypatch, True)
    claude.before = threading.Event()
    first: dict[int, ClaudeUsage] = {}
    reader = threading.Thread(
        target=lambda: first.update(service.cached_usage([work], now=NOW, fetch=endpoint))
    )
    reader.start()
    try:
        assert claude.started.wait(10)
        # A hand-over's live read, and the page of another process, while it runs:
        live = service.read_usage([work], now=NOW, fetch=endpoint)[work.slot]
        _new_process()
        # Another process has fetch locks of its own: this one's page would wait for the read
        # under way and be served what it read (review of #261, m4).
        monkeypatch.setattr(service, "_FETCH_LOCKS", {})
        page = service.cached_usage([work], now=NOW, fetch=endpoint)[work.slot]
    finally:
        claude.before.set()
        reader.join(10)

    assert claude.runs == [work.slot], "at most one refresh in flight per account"
    assert live.cause is UsageCause.token_refreshing and live.refresh_retry_at is None
    assert live.reason == "the stored token has expired — Claude Code is refreshing it"
    assert page.kept and page.session_percent == 40 and not page.needs_login
    assert first[work.slot].session_percent == 55


# --------------------------------------------------------------------------- 3. both fail


def test_only_when_the_retry_fails_too_does_the_row_ask_for_a_log_in(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    endpoint = _Usage({})
    _read_before_it_expired(work, endpoint, 40)
    claude = FakeClaude(monkeypatch, False)
    status = _status(work)
    service.cached_usage([work], now=NOW, fetch=endpoint)
    after = NOW + timedelta(seconds=service.TOKEN_REFRESH_RETRY_SECONDS)

    last = service.cached_usage([work], now=after, fetch=endpoint)[work.slot]

    assert claude.runs == [work.slot, work.slot]
    assert not last.available and last.needs_login and not last.kept
    assert last.cause is UsageCause.token_expired
    assert last.reason == "the stored token has expired"
    assert last.fix == _REFRESH_TOKEN.format(slot=work.slot)
    assert needs_login(status, last)
    assert problem_tooltip(status, last) == (
        f"the stored token has expired\nfix: {_REFRESH_TOKEN.format(slot=work.slot)}"
    )
    assert (_row(work) or {}).get("phase") == "failed"
    # Given up on this token: later reads, forced or by another process, start nothing.
    for minutes in (2, 10):
        _new_process()
        again = service.cached_usage(
            [work], now=NOW + timedelta(minutes=minutes), force=True, fetch=endpoint
        )
        assert again[work.slot].needs_login
    assert claude.runs == [work.slot, work.slot]

    # A new token — a log-in, or a session's own refresh — that goes stale starts afresh.
    _renew(work, "tok-next")
    path = core.credentials_path(work)
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["expiresAt"] = int((NOW - timedelta(minutes=1)).timestamp() * 1000)
    path.write_text(json.dumps(data))
    service.read_usage([work], now=NOW + timedelta(minutes=11), fetch=endpoint)
    assert claude.runs == [work.slot] * 3


def test_a_claim_left_by_a_process_that_died_lapses(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that died mid-refresh does not hold the slot: once its claim lapses the next
    read makes the retry; a lapsed claim of the retry itself leaves a log-in to ask for."""
    work = _expired()
    claude = FakeClaude(monkeypatch, True)
    endpoint = _Usage({"tok-renewed-1": _payload(10)})
    token = service._login_digest(core.credentials(work))  # type: ignore[arg-type]
    key = service._TOKEN_REFRESH_KEY.format(slot=work.slot)
    died = service.TokenRefresh(
        slot=work.slot, token=token, phase="running", tries=1, until=NOW - timedelta(seconds=1)
    )
    with store_session() as store:
        store.set_ui_state(key, died.dump())

    reading = service.read_usage([work], now=NOW, fetch=endpoint)[work.slot]

    assert claude.runs == [work.slot] and reading.session_percent == 10
    lapsed = service.TokenRefresh(
        slot=work.slot, token="another", phase="running", tries=2, until=NOW
    )
    assert service._refresh_turn(lapsed, "another", NOW) is None
    assert service._refresh_turn(lapsed, "fresh-token", NOW) == 1, "another token: afresh"


# --------------------------------------------------------------------------- 4. as before


def test_a_disabled_account_is_never_refreshed_and_asks_for_a_log_in_at_once(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _expired()
    off = service.set_disabled("2", True)
    claude = FakeClaude(monkeypatch, True)
    status = _status(off)

    reading = service.cached_usage([off], now=NOW, fetch=_Usage({}))[off.slot]

    assert claude.runs == []
    assert reading.cause is UsageCause.token_expired and reading.needs_login
    assert needs_login(status, None) and needs_login(status, reading)
    assert not service.awaits_refresh(status, None)


@pytest.mark.parametrize("cause", ["not_signed_in", "token_missing", "keychain_locked"])
def test_no_login_and_keychain_causes_start_no_refresh(
    cause: str, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = core.create_account()
    if cause == "token_missing":
        _sign_in(account, "two@example.com", credentials=False)
    elif cause == "keychain_locked":
        _sign_in(account, "two@example.com")
        monkeypatch.setattr(
            core,
            "stored_login",
            lambda _account, fresh=False: core.StoredLogin(None, core.KEYCHAIN_LOCKED, "locked"),
        )
    claude = FakeClaude(monkeypatch, True)

    reading = service.read_usage([account], now=NOW, fetch=_Usage({}))[account.slot]

    assert claude.runs == []
    assert reading.cause is not UsageCause.token_refreshing and not reading.available


def test_without_claude_code_an_expired_token_reads_as_it_always_did(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    monkeypatch.setattr(service, "_token_refresh_command", REAL_COMMAND)
    monkeypatch.setattr(service, "install", lambda: ClaudeInstall(installed=False))
    ran: list[Any] = []

    def run(*args: Any) -> bool:
        ran.append(args)
        return True

    monkeypatch.setattr(service, "_run_token_refresh", run)

    reading = service.read_usage([work], now=NOW, fetch=_Usage({}))[work.slot]

    assert ran == [] and reading.cause is UsageCause.token_expired and reading.needs_login
    assert _row(work) is None, "nothing claimed for a refresh that cannot run"


def test_removing_an_account_forgets_its_refresh(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    FakeClaude(monkeypatch, False)
    service.read_usage([work], now=NOW, fetch=_Usage({}))
    assert (_row(work) or {}).get("phase") == "waiting"
    service.forget_arrangement(work.slot, work)
    assert _row(work) is None


# --------------------------------------------------------------------------- 5. a pick
#
# A launch's, a spawn's and a hand-over's headroom pick is waited on by its caller: a refresh
# there held up an admitted `fleet spawn` for up to a minute, saying nothing (review of #485,
# m1). The pick skips the stale account, as it did before the refresh existed, or decides by
# its kept reading; the page, `accounts usage` and the status line still refresh it.


def _pick(*accounts: ClaudeAccount, endpoint: _Usage, max_age: float | None) -> Any:
    return service.headroom_choice(
        list(accounts), switch_at=90, fetch=endpoint, now=NOW, max_age=max_age
    )


def _promptly(pick: Any) -> Any:
    """Run ``pick`` in a thread and give it 5 s: a pick that waited on the refresh (which the
    test holds until it is done) is still running then."""
    done: list[Any] = []
    picker = threading.Thread(target=lambda: done.append(pick()), daemon=True)
    picker.start()
    picker.join(5)
    assert not picker.is_alive(), "the pick waited on the refresh"
    return done[0]


@pytest.mark.parametrize(
    "max_age", [service.PICK_MAX_AGE_SECONDS, None], ids=["launch, cached", "hand-over, live"]
)
def test_a_headroom_pick_starts_no_refresh_and_takes_another_account(
    max_age: float | None, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _expired()
    home = _slot("home@example.com", "tok-home")
    endpoint = _Usage({"tok-home": _payload(20), "tok-renewed-1": _payload(55)})
    claude = FakeClaude(monkeypatch, True)
    claude.before = threading.Event()  # a refresh, were one started, would hold the pick
    try:
        picked, notes = _promptly(lambda: _pick(work, home, endpoint=endpoint, max_age=max_age))
    finally:
        claude.before.set()

    assert claude.runs == [], "the pick started no refresh"
    assert picked is not None and picked.slot == home.slot
    assert any(core.label(work) in note and "expired" in note for note in notes), notes
    assert _row(work) is None, "nothing claimed for it either"


def test_after_a_pick_skipped_a_rejected_token_the_page_still_refreshes_it(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 is a failure with a token, which the five-minute cache remembers: the pick's must
    not be, or the page's read a moment later is answered *Log in* from it, no refresh."""
    work = _slot("work@example.com", "tok-old")
    home = _slot("home@example.com", "tok-home")
    endpoint = _Usage({"tok-old": 401, "tok-home": _payload(20), "tok-renewed-1": _payload(30)})
    claude = FakeClaude(monkeypatch, True)

    picked, _notes = _pick(work, home, endpoint=endpoint, max_age=service.PICK_MAX_AGE_SECONDS)
    assert picked is not None and picked.slot == home.slot and claude.runs == []

    page = service.cached_usage([work], now=NOW + timedelta(seconds=10), fetch=endpoint)

    assert claude.runs == [work.slot], "the page's read refreshed it"
    assert page[work.slot].available and page[work.slot].session_percent == 30


def test_a_pick_gives_way_to_a_refresh_this_process_runs(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asq's sidebar refreshing a slot holds its fetch lock for as long as Claude Code takes; a
    spawn from the dialog, in the same process, does not wait for it: it reads that slot
    itself, unlocked and not refreshing, finds it being refreshed, and goes by home."""
    work = _expired()
    endpoint = _Usage({"tok-renewed-1": _payload(55)})
    _read_before_it_expired(work, endpoint, 40)
    home = _slot("home@example.com", "tok-home")
    endpoint.by_token["tok-home"] = _payload(20)
    claude = FakeClaude(monkeypatch, True)
    claude.before = threading.Event()
    page: dict[int, ClaudeUsage] = {}
    reader = threading.Thread(
        target=lambda: page.update(service.cached_usage([work], now=NOW, fetch=endpoint))
    )
    reader.start()
    try:
        assert claude.started.wait(10)
        picked, notes = _promptly(
            lambda: _pick(work, home, endpoint=endpoint, max_age=service.PICK_MAX_AGE_SECONDS)
        )
        assert reader.is_alive(), "the pick returned while the refresh still ran"
    finally:
        claude.before.set()
        reader.join(10)

    assert picked is not None and picked.slot == home.slot
    assert any(core.label(work) in note and "refreshing" in note for note in notes), notes
    assert claude.runs == [work.slot], "the page's refresh, and only it"
    assert page[work.slot].session_percent == 55


def test_a_pick_gives_way_to_a_refresh_round_that_holds_a_sibling_slot_too(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-485 round 2: the sidebar and the Accounts page read every account in one round,
    and the round holds each slot's fetch lock through the refresh of one. A pick needing a
    sibling slot (home) waited on its lock until Claude Code was done. It reads home itself,
    unlocked: one request more, and home is picked as it was on base (round 4)."""
    work = _expired()
    endpoint = _Usage({"tok-renewed-1": _payload(55)})
    _read_before_it_expired(work, endpoint, 40)
    home = _slot("home@example.com", "tok-home")
    endpoint.by_token["tok-home"] = _payload(20)
    claude = FakeClaude(monkeypatch, True)
    claude.before = threading.Event()
    page: dict[int, ClaudeUsage] = {}
    reader = threading.Thread(
        target=lambda: page.update(service.cached_usage([work, home], now=NOW, fetch=endpoint))
    )
    reader.start()
    try:
        assert claude.started.wait(10)
        asked = endpoint.calls.count("tok-home")
        picked, notes = _promptly(
            lambda: _pick(work, home, endpoint=endpoint, max_age=service.PICK_MAX_AGE_SECONDS)
        )
        assert reader.is_alive(), "the pick returned while the refresh still ran"
        assert endpoint.calls.count("tok-home") == asked + 1, "home read once, by the pick"
    finally:
        claude.before.set()
        reader.join(10)

    assert picked is not None and picked.slot == home.slot, notes
    assert any(core.label(work) in note and "refreshing" in note for note in notes), notes
    assert claude.runs == [work.slot], "the page's refresh, and only it"
    assert page[work.slot].session_percent == 55 and page[home.slot].session_percent == 20


@pytest.mark.parametrize(
    ("pick_at", "home_age"),
    [(61, 0), (0, 120), (0, 0)],
    ids=["(a) pick 61 s on", "(b) home read 2 min before", "home read just now"],
)
def test_a_pick_never_waits_on_a_slot_the_page_holds_but_does_not_refresh(
    pick_at: int, home_age: int, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-485 round 3, the tester's repro: the page's round takes home's and work's locks,
    another round records home meanwhile, so the page is served home from that and refreshes
    work alone, holding home's lock all the while. A pick that needed home waited on it for
    the whole refresh. It waits on no lock: whatever home's reading on record, older than the
    pick's minute, the pick reads home itself, once, and picks it."""
    work = _expired()
    endpoint = _Usage({"tok-renewed-1": _payload(55)})
    _read_before_it_expired(work, endpoint, 40)
    home = _slot("home@example.com", "tok-home")
    endpoint.by_token["tok-home"] = _payload(20)
    claude = FakeClaude(monkeypatch, True)
    claude.before = threading.Event()
    looked = threading.Event()
    real_served = service._served
    reader_ident: list[int] = []

    def served(*args: Any, **kwargs: Any) -> dict[int, ClaudeUsage]:
        out = real_served(*args, **kwargs)
        if reader_ident and threading.get_ident() == reader_ident[0]:
            looked.set()
        return out

    monkeypatch.setattr(service, "_served", served)
    other = service._fetching([home], service._fetch_rounds([home]))
    other.__enter__()  # another call in this process is fetching home
    page: dict[int, ClaudeUsage] = {}

    def read_page() -> None:
        reader_ident.append(threading.get_ident())
        page.update(service.cached_usage([work, home], now=NOW, fetch=endpoint))

    reader = threading.Thread(target=read_page)
    reader.start()
    try:
        assert looked.wait(10), "the page looked at the cache"
        service.read_usage([home], now=NOW - timedelta(seconds=home_age), fetch=endpoint)
        other.__exit__(None, None, None)  # its round ends: the page is served home from it
        assert claude.started.wait(10), "the page refreshes work, holding home's lock too"
        asked = endpoint.calls.count("tok-home")
        picked, notes = _promptly(
            lambda: service.headroom_choice(
                [work, home],
                switch_at=90,
                fetch=endpoint,
                now=NOW + timedelta(seconds=pick_at),
                max_age=service.PICK_MAX_AGE_SECONDS,
            )
        )
        assert reader.is_alive(), "the pick returned while the refresh still ran"
        fresh = pick_at == 0 and home_age == 0  # inside the pick's minute: served, not read
        assert endpoint.calls.count("tok-home") == asked + (0 if fresh else 1)
    finally:
        claude.before.set()
        reader.join(10)
    assert picked is not None and picked.slot == home.slot, notes
    assert claude.runs == [work.slot], "the page's refresh, and only it"


@pytest.mark.parametrize(
    ("force", "home_age"),
    [(True, 120), (False, 310)],
    ids=["the page's force refresh", "home's five-minute cache lapsed"],
)
def test_a_pick_never_waits_on_a_second_round_queued_behind_the_refresh(
    force: bool, home_age: int, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-485 round 3's triggers: the sidebar's round refreshes work, holding its lock;
    a second display round over [home, work] (the Accounts page's force refresh, or home's
    cache lapsing past five minutes meanwhile) takes home's lock, home sorting first, and
    queues on work's. A pick that needed home waited on that round, which waited on the
    refresh. Home's reading is past the pick's minute either way (120 s: inside the page's
    five, which the force skips; 310 s: past it), so the pick reads home itself, once,
    without waiting on either round, and picks it."""
    endpoint = _Usage({"tok-home": _payload(20), "tok-renewed-1": _payload(55)})
    home = _slot("home@example.com", "tok-home")  # its config dir sorts before work's
    service.read_usage([home], now=NOW - timedelta(seconds=home_age), fetch=endpoint)
    work = _expired()
    _read_before_it_expired(work, endpoint, 40)
    assert str(home.config_dir) < str(work.config_dir)
    claude = FakeClaude(monkeypatch, True)
    claude.before = threading.Event()
    rounds = [
        threading.Thread(target=lambda: service.cached_usage([work], now=NOW, fetch=endpoint)),
        threading.Thread(
            target=lambda: service.cached_usage([home, work], now=NOW, fetch=endpoint, force=force)
        ),
    ]
    try:
        rounds[0].start()  # the sidebar's: refreshes work, holding its lock
        assert claude.started.wait(10)
        rounds[1].start()  # the page's: takes home's lock, then queues on work's
        deadline = time.monotonic() + 10
        while not (
            (lock := service._FETCH_LOCKS.get(str(home.config_dir))) is not None and lock.locked()
        ):
            assert time.monotonic() < deadline, "the second round never took home's lock"
            time.sleep(0.01)
        asked = endpoint.calls.count("tok-home")
        picked, notes = _promptly(
            lambda: _pick(work, home, endpoint=endpoint, max_age=service.PICK_MAX_AGE_SECONDS)
        )
        assert all(r.is_alive() for r in rounds), "the pick returned while both rounds waited"
        assert endpoint.calls.count("tok-home") == asked + 1, "home read once, by the pick"
    finally:
        claude.before.set()
        for r in rounds:
            r.join(10)
    assert picked is not None and picked.slot == home.slot, notes
    assert any(core.label(work) in note and "refreshing" in note for note in notes), notes
    assert claude.runs == [work.slot], "the sidebar's refresh, and only it"


def test_with_every_lock_held_a_pick_reads_each_slot_itself_once(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-485 round 4's repro: a plain display round (no refresh in it) holds every slot's
    lock, work at 95 % and home at 20 %. The pick skipped both, and a spawn fell to the
    preferred account over the line. It reads each held slot itself, unlocked: one request
    more per slot, no wait, no refresh, and home is picked, as on base."""
    work = _slot("work@example.com", "tok-work")
    home = _slot("home@example.com", "tok-home")
    endpoint = _Usage({"tok-work": _payload(95), "tok-home": _payload(20)})
    claude = FakeClaude(monkeypatch, True)
    asked = len(endpoint.calls)
    with service._fetching([work, home], service._fetch_rounds([work, home])):
        picked, notes = _promptly(
            lambda: _pick(work, home, endpoint=endpoint, max_age=service.PICK_MAX_AGE_SECONDS)
        )
    assert picked is not None and picked.slot == home.slot, notes
    assert sorted(endpoint.calls[asked:]) == ["tok-home", "tok-work"], "one request per slot"
    assert claude.runs == []


def test_a_held_slot_whose_token_is_stale_is_skipped_with_no_request_and_no_refresh(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held slot read by the pick itself is read with ``refresh=False``: an expired token is
    answered as it stands, with no request for it and no refresh started, and the slot is
    skipped for this pick."""
    work = _expired()
    home = _slot("home@example.com", "tok-home")
    endpoint = _Usage({"tok-home": _payload(20), "tok-renewed-1": _payload(55)})
    claude = FakeClaude(monkeypatch, True)
    asked = len(endpoint.calls)
    with service._fetching([work, home], service._fetch_rounds([work, home])):
        picked, notes = _promptly(
            lambda: _pick(work, home, endpoint=endpoint, max_age=service.PICK_MAX_AGE_SECONDS)
        )
    assert picked is not None and picked.slot == home.slot, notes
    assert any(core.label(work) in note and "expired" in note for note in notes), notes
    assert endpoint.calls[asked:] == ["tok-home"], "nothing asked for the stale token"
    assert claude.runs == [] and _row(work) is None, "no refresh started, nothing claimed"


# --------------------------------------------------------------------------- the command


def test_the_refresh_runs_claude_code_headless_on_the_accounts_own_directory(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one process the flow starts: ``claude mcp get`` of a name no server can have, in
    the environment a session on the account runs with — no terminal, nothing on stdin."""
    work = _expired()
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        service, "install", lambda: ClaudeInstall(installed=True, binary="/opt/bin/claude")
    )
    command = REAL_COMMAND()
    assert command == ["/opt/bin/claude", "mcp", "get", "aisquare token refresh"]

    assert REAL_RUN(work, command) is True, "an exit status of 1 is no answer either way"

    argv, kwargs = seen[0]
    assert argv == command
    assert kwargs["env"][core.CONFIG_DIR_VAR] == str(work.config_dir)
    assert kwargs["cwd"] == work.config_dir
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True and kwargs["check"] is False
    assert kwargs["timeout"] == service.TOKEN_REFRESH_TIMEOUT_SECONDS

    def times_out(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    def missing(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", times_out)
    assert REAL_RUN(work, command) is True, "it ran: the login read after it says"
    monkeypatch.setattr(subprocess, "run", missing)
    assert REAL_RUN(work, command) is False


def test_a_refresh_row_this_version_cannot_read_is_no_refresh() -> None:
    assert service.TokenRefresh.load(2, "{not json") is None
    assert service.TokenRefresh.load(2, json.dumps({"token": "t", "phase": "odd"})) is None
    held = service.TokenRefresh(
        slot=2, token="t", phase="waiting", tries=1, until=datetime(2026, 9, 9, 12, 0)
    )
    assert service.TokenRefresh.load(2, held.dump()) == held
