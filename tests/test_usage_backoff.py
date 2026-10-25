"""A rate-limited usage read backs off, and the last good reading stays on the row (F1 of #400).

The owner's report: "HTTP 429 from the usage endpoint", and it stuck. ``_http_get``
dropped ``Retry-After``, a failure was remembered only in the process that saw it, and
``--refresh`` skipped even that, so every poller kept the account rate-limited while
the row showed ``—``. The controls, each against a scripted endpoint that counts the
requests it is sent:

- a 429's ``Retry-After`` is honoured by a second process and by ``--refresh``, with no
  request during the wait; with no header the wait doubles, and a good read resets it;
- a failed read shows the last good numbers, their age and the reason; ``—`` only when
  nothing was ever read;
- the headroom pick (a launch's and a hand-over's) never asks a backed-off account, and
  decides by a fresh reading or skips it, with a note;
- doctor names the accounts backed off, and until when.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.accounts import account_cells, kept_tooltip
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccountStatus, ClaudeIdentity, ClaudeUsage
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from tests.test_claude_accounts import NOW
from tests.test_usage_aware_accounts import _payload, _slot, _Usage


class _Endpoint(_Usage):
    """:class:`_Usage` that can answer with headers: ``(status, {header: value})`` per token."""

    by_token: dict[str, Any]

    def __init__(self, by_token: dict[str, Any]) -> None:
        super().__init__(by_token)

    def __call__(  # type: ignore[override]
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes] | tuple[int, bytes, Mapping[str, str]]:
        token = headers["Authorization"].removeprefix("Bearer ")
        answer = self.by_token.get(token, 401)
        if isinstance(answer, tuple):
            self.calls.append(token)
            status, sent = answer
            return status, b"", sent
        return super().__call__(url, headers, timeout)


def _new_process() -> None:
    """Forget what this process remembers, as a second ``aisquare`` process starts with
    nothing: only what is on disk (the store) carries over."""
    with service._UNANSWERED_LOCK:
        service._UNANSWERED.clear()
    with service._FETCH_LOCKS_LOCK:
        service._FETCH_ROUNDS.clear()


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(service, "_now", lambda: moment)


# --------------------------------------------------------------------------- the wait


def test_retry_after_is_honoured_by_a_second_process_and_by_refresh(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": _payload(40)})
    service.cached_usage([work], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = (429, {"Retry-After": "600"})
    limited_at = NOW + timedelta(minutes=6)

    first = service.cached_usage([work], now=limited_at, fetch=endpoint)[work.slot]
    assert len(endpoint.calls) == 2
    assert first.rate_limited and first.rate_limited_until == limited_at + timedelta(minutes=10)
    assert first.available and first.session_percent == 40, "the last good reading stays"

    endpoint.by_token["tok-work"] = _payload(70)  # the endpoint would answer now; it is not asked
    for minutes in (1, 5, 9):
        _new_process()
        moment = limited_at + timedelta(minutes=minutes)
        plain = service.cached_usage([work], now=moment, fetch=endpoint)[work.slot]
        forced = service.cached_usage([work], now=moment, force=True, fetch=endpoint)[work.slot]
        live = service.read_usage([work], now=moment, fetch=endpoint)[work.slot]  # a hand-over's
        for reading in (plain, forced):
            assert reading.session_percent == 40 and reading.fetched_at == NOW
            assert reading.rate_limited_until == limited_at + timedelta(minutes=10)
            assert reading.last_failed_reason is not None
            assert "rate-limited until" in reading.last_failed_reason
            assert "logging in again does not clear it" in reading.last_failed_reason
        assert not live.available and live.rate_limited
    assert len(endpoint.calls) == 2, "no request went out during the wait"

    _new_process()
    after = service.cached_usage(
        [work], now=limited_at + timedelta(minutes=10, seconds=1), fetch=endpoint
    )[work.slot]
    assert len(endpoint.calls) == 3 and after.session_percent == 70
    assert not after.rate_limited and after.last_failed_reason is None
    assert service.usage_backoffs([work], now=limited_at + timedelta(minutes=11)) == {}


def test_refresh_during_a_wait_sends_nothing_and_says_how_old_the_figure_is(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": _payload(40)})
    monkeypatch.setattr(service, "_http_get", endpoint)
    assert runner.invoke(app, ["accounts", "usage"]).exit_code == 0
    live = json.loads(runner.invoke(app, ["--json", "accounts", "usage"]).stdout)
    assert [row["usage"]["kept"] for row in live if row["account"]["slot"] == 2] == [False]

    endpoint.by_token["tok-work"] = (429, {"Retry-After": "900"})
    _at(monkeypatch, NOW + timedelta(minutes=12))
    limited = runner.invoke(app, ["accounts", "usage", "--refresh"])
    assert limited.exit_code == 0, limited.output
    until = (NOW + timedelta(minutes=27)).astimezone().strftime("%H:%M")
    line = f"account 2: rate-limited until {until} — logging in again does not clear it; "
    assert line + "showing the reading from 12m ago" in limited.stdout, limited.stdout
    assert "40%" in limited.stdout
    calls = len(endpoint.calls)

    _new_process()
    _at(monkeypatch, NOW + timedelta(minutes=20))
    again = runner.invoke(app, ["accounts", "usage", "--refresh"])
    assert len(endpoint.calls) == calls, "--refresh honours the wait"
    assert "showing the reading from 20m ago" in again.stdout and "40%" in again.stdout

    shown = runner.invoke(app, ["--json", "accounts", "usage", "--refresh"])
    rows = {row["account"]["slot"]: row for row in json.loads(shown.stdout)}
    usage = rows[2]["usage"]
    assert usage["kept"] is True, "review of #408, n3: said, not left to be worked out"
    assert usage["rate_limited"] is True and usage["session_used_percent"] == 40
    assert usage["rate_limited_until"] is not None and len(endpoint.calls) == calls


def test_without_retry_after_the_wait_doubles_and_a_good_read_starts_it_over(
    fake_home: Path,
) -> None:
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": 429})
    moment = NOW
    waits = []
    for _ in range(4):
        reading = service.read_usage([work], now=moment, fetch=endpoint)[work.slot]
        assert reading.rate_limited and reading.rate_limited_until is not None
        waits.append(reading.rate_limited_until - moment)
        assert service.read_usage([work], now=moment, fetch=endpoint)[work.slot].rate_limited
        moment = reading.rate_limited_until + timedelta(seconds=1)
    assert waits == [timedelta(seconds=s) for s in (60, 120, 240, 480)]
    assert len(endpoint.calls) == 4, "one request per wait, none inside one"

    endpoint.by_token["tok-work"] = _payload(30)
    assert service.read_usage([work], now=moment, fetch=endpoint)[work.slot].available
    endpoint.by_token["tok-work"] = 429
    later = moment + timedelta(minutes=1)
    reading = service.read_usage([work], now=later, fetch=endpoint)[work.slot]
    assert reading.rate_limited_until == later + timedelta(seconds=60), "started over"


def test_the_doubling_stops_at_half_an_hour(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": 429})
    moment = NOW
    for _ in range(9):
        reading = service.read_usage([work], now=moment, fetch=endpoint)[work.slot]
        assert reading.rate_limited_until is not None
        last = reading.rate_limited_until - moment
        moment = reading.rate_limited_until + timedelta(seconds=1)
    assert last == timedelta(seconds=service.USAGE_BACKOFF_MAX_SECONDS)


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        ((503, {"Retry-After": "90"}), timedelta(seconds=90)),
        ((429, {"retry-after": "Sat, 01 Jan 2000 00:00:00 GMT"}), timedelta(seconds=1)),
        ((429, {"Retry-After": "999999"}), timedelta(hours=6)),
        ((429, {"Retry-After": "soon"}), timedelta(seconds=60)),
    ],
    ids=["503-with-header", "date-in-the-past", "clamped", "unreadable"],
)
def test_retry_after_forms(
    fake_home: Path, answer: tuple[int, dict[str, str]], wait: timedelta
) -> None:
    work = _slot("work@example.com", "tok-work")
    reading = service.read_usage([work], now=NOW, fetch=_Endpoint({"tok-work": answer}))
    assert reading[work.slot].rate_limited_until == NOW + wait


def test_retry_after_as_an_http_date(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    when = (NOW + timedelta(minutes=3)).astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
    endpoint = _Endpoint({"tok-work": (429, {"Retry-After": when})})
    reading = service.read_usage([work], now=NOW, fetch=endpoint)[work.slot]
    assert reading.rate_limited_until == NOW.replace(microsecond=0) + timedelta(minutes=3)


def test_a_503_without_retry_after_is_not_a_back_off(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": 503})
    reading = service.read_usage([work], now=NOW, fetch=endpoint)[work.slot]
    assert not reading.rate_limited and reading.reason == "HTTP 503 from the usage endpoint"
    assert service.usage_backoffs([work], now=NOW) == {}


def test_the_real_fetch_hands_back_the_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_http_get`` returns the answer's headers, an HTTP error's included."""
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    sent = Message()
    sent["Retry-After"] = "30"

    def refuse(request: Any, timeout: float, **_: Any) -> Any:
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many", sent, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    status, body, headers = service._http_get("https://example.invalid", {}, 1.0)
    assert (status, body, dict(headers)) == (429, b"", {"Retry-After": "30"})


# --------------------------------------------------------------------------- the last good reading


def test_a_failed_read_shows_the_last_good_numbers_and_dash_only_when_none_was_read(
    fake_home: Path,
) -> None:
    work = _slot("work@example.com", "tok-work")
    fresh = _slot("fresh@example.com", "tok-fresh")
    endpoint = _Endpoint({"tok-work": _payload(40, week=12), "tok-fresh": 429})
    service.cached_usage([work], now=NOW, fetch=endpoint)

    def unreachable(url: str, headers: Any, timeout: float) -> tuple[int, bytes]:
        raise OSError("down")

    later = NOW + timedelta(hours=2)
    kept = service.cached_usage([work], now=later, fetch=unreachable)[work.slot]
    assert kept.available and (kept.session_percent, kept.week_percent) == (40, 12)
    assert kept.fetched_at == NOW and kept.last_failed_at == later
    assert kept.last_failed_reason == "could not reach the usage endpoint (OSError)"
    assert not kept.rate_limited

    never = service.cached_usage([fresh], now=later, fetch=endpoint)[fresh.slot]
    assert not never.available and never.rate_limited, "nothing on record: the reason alone"
    assert never.reason is not None and never.reason.startswith("rate-limited until")


def test_the_row_says_how_old_the_kept_reading_is_and_why() -> None:
    from aisquare.models import ClaudeAccount

    account = ClaudeAccount(slot=2, config_dir=Path("/h/.aisquare/claude-accounts/2"))
    status = ClaudeAccountStatus(
        account=account,
        label="work",
        identity=ClaudeIdentity(email="work@example.com"),
        signed_in=True,
        token_state="ok",
    )
    until = NOW + timedelta(minutes=8)
    usage = ClaudeUsage(
        available=True,
        session_percent=40,
        week_percent=10,
        fetched_at=NOW - timedelta(minutes=12),
        last_failed_at=NOW,
        last_failed_reason=service.backoff_reason(until),
        rate_limited=True,
        rate_limited_until=until,
    )
    cells = account_cells(status, usage, now=NOW)
    assert cells["session"].plain.endswith("40% · 12m"), "the kept figure, aged, never —"
    assert "status" not in cells, "#406 took the status column away: the age is the session's"
    assert cells["week"].plain.endswith("10%"), "a kept reading is no reason to blank the week"
    clock = until.astimezone().strftime("%H:%M")
    assert kept_tooltip(usage, now=NOW) == (
        f"reading from 12m ago · rate-limited until {clock} — logging in again does not clear it"
    )
    live = usage.model_copy(update={"last_failed_reason": None, "rate_limited": False})
    assert kept_tooltip(live, now=NOW) is None and account_cells(status, live, now=NOW)[
        "session"
    ].plain.endswith("40%")


# --------------------------------------------------------------------------- picks


def test_a_launchs_pick_uses_a_fresh_reading_of_a_backed_off_account_and_says_so(
    fake_home: Path,
) -> None:
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint({"tok-work": _payload(20), "tok-spare": _payload(50)})
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = (429, {"Retry-After": "600"})
    endpoint.by_token["tok-spare"] = _payload(55)
    moment = NOW + timedelta(minutes=2)
    service.read_usage([work], now=moment, fetch=endpoint)  # the 429: the wait is recorded
    calls = len(endpoint.calls)

    picked, notes = service.headroom_choice(
        [work, spare], switch_at=85, now=moment + timedelta(seconds=30), fetch=endpoint
    )
    assert picked is not None and picked.slot == work.slot
    assert any(
        "rate-limited until" in n and "picked by its reading from 2m ago" in n for n in notes
    )
    assert endpoint.calls[calls:] == ["tok-spare"], "the backed-off account was not asked"


def test_a_pick_skips_a_backed_off_account_whose_reading_is_old(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint({"tok-work": _payload(20), "tok-spare": _payload(50)})
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = (429, {"Retry-After": "3600"})
    moment = NOW + timedelta(minutes=30)
    service.read_usage([work], now=moment, fetch=endpoint)
    calls = len(endpoint.calls)

    for max_age in (None, service.PICK_MAX_AGE_SECONDS):  # a hand-over's read, a launch's
        _new_process()
        picked, notes = service.headroom_choice(
            [work, spare], switch_at=85, now=moment, fetch=endpoint, max_age=max_age
        )
        assert picked is not None and picked.slot == spare.slot
        skipped = [n for n in notes if n.startswith("headroom: account 2 skipped")]
        assert skipped and "rate-limited until" in skipped[0]
        assert "its last reading is from 30m ago, too old to pick by" in skipped[0]
    assert "tok-work" not in endpoint.calls[calls:]


# --------------------------------------------------------------------------- doctor, removal


def test_a_503_with_retry_after_is_named_by_doctor_as_a_back_off(fake_home: Path) -> None:
    """The tester's pass on #401, (d): an account backed off after a 503 with ``Retry-After``
    was named by doctor's row as backed off 'after HTTP 429'. Worded for both."""
    work = _slot("work@example.com", "tok-work")
    held = service.read_usage(
        [work], now=NOW, fetch=_Endpoint({"tok-work": (503, {"Retry-After": "600"})})
    )
    assert held[work.slot].rate_limited, "control: the 503 named a wait"
    checks = {c.name: c for c in diagnostics._claude_accounts_checks()}
    line = checks["claude-account-backoff"]
    assert "after HTTP 429 or 503" in line.detail and "account 2" in line.detail, line.detail


def test_doctor_names_the_backed_off_accounts(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    _slot("spare@example.com", "tok-spare")
    assert not any(
        c.name == "claude-account-backoff" for c in diagnostics._claude_accounts_checks()
    )
    until = datetime.now(tz=UTC) + timedelta(minutes=20)
    with store_session() as store:
        token = service._login_key(work, NOW)
        held = service.UsageBackoff(slot=work.slot, until=until, attempts=1, since=NOW, token=token)
        store.set_ui_state(service._BACKOFF_KEY.format(slot=work.slot), held.dump())
    checks = {c.name: c for c in diagnostics._claude_accounts_checks()}
    line = checks["claude-account-backoff"]
    assert line.status == "warn"
    assert f"account 2 rate-limited until {until.astimezone().strftime('%H:%M')}" in line.detail
    assert "after HTTP 429 or 503" in line.detail, "a 503 with Retry-After backs off too (d)"
    assert "spare" not in line.detail and "account 3" not in line.detail
    assert line.fix is not None and "does not clear a rate limit" in line.fix


def test_doctors_look_reads_the_login_only_of_a_slot_with_a_wait(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #408, n2: doctor's row read every slot's login (a Keychain read per slot on
    macOS) on every run. It reads it only for a slot with a wait recorded."""
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    service.read_usage([work], now=NOW, fetch=_Endpoint({"tok-work": 429}))
    asked: list[int] = []
    real = service._login_key

    def counted(account: Any, now: datetime) -> str | None:
        asked.append(account.slot)
        return real(account, now)

    monkeypatch.setattr(service, "_login_key", counted)

    waiting = service.usage_backoffs([work, spare], now=NOW)

    assert set(waiting) == {work.slot}
    assert asked == [work.slot], "the slot with no wait is not read"


def test_a_kept_reading_has_no_trend(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #408, n4: the page's trend ("≈ N min to the limit") was worked out for a kept
    reading too, describing a moment that can be hours ago. A kept one has none; a live one
    still has its own (control)."""
    work = _slot("work@example.com", "tok-work")
    pace = object()
    asked: list[int] = []

    def trend(slot: int, reading: ClaudeUsage, **_: Any) -> object:
        asked.append(slot)
        return pace

    monkeypatch.setattr(service, "usage_trend", trend)
    endpoint = _Endpoint({"tok-work": _payload(40)})
    live = service.cached_usage_with_trends([work], now=NOW, fetch=endpoint)[work.slot]
    endpoint.by_token["tok-work"] = (429, {"Retry-After": "600"})
    _new_process()
    later = NOW + timedelta(minutes=6)
    kept = service.cached_usage_with_trends([work], now=later, fetch=endpoint)[work.slot]

    assert live[1] is pace and not service.is_kept(live[0])
    assert service.is_kept(kept[0]) and kept[0].session_percent == 40
    assert kept[1] is None and asked == [work.slot], "no trend asked for the kept one"


def test_a_wait_that_has_ended_is_never_shown_and_the_account_is_asked(fake_home: Path) -> None:
    """Review of #408, n5: after a short ``Retry-After`` this process remembered the gated try
    for five minutes, so the row said "rate-limited until" a time already past and nothing
    was asked. Once the wait ends the account is asked, in the same process."""
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": _payload(40)})
    service.cached_usage([work], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = (429, {"Retry-After": "30"})
    limited_at = NOW + timedelta(minutes=6)
    held = service.cached_usage([work], now=limited_at, fetch=endpoint)[work.slot]
    assert held.rate_limited_until == limited_at + timedelta(seconds=30)
    endpoint.by_token["tok-work"] = _payload(55)

    later = limited_at + timedelta(minutes=2)
    shown = service.cached_usage([work], now=later, fetch=endpoint)[work.slot]

    assert not shown.rate_limited and shown.session_percent == 55, "asked again, read live"
    assert len(endpoint.calls) == 3


def test_removing_an_account_forgets_its_wait(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    service.read_usage([work], now=NOW, fetch=_Endpoint({"tok-work": 429}))
    with store_session() as store:
        assert store.ui_state(service._BACKOFF_KEY.format(slot=work.slot)) is not None
    service.forget_arrangement(work.slot)  # what `remove` calls once the directory is gone
    with store_session() as store:
        assert store.ui_state(service._BACKOFF_KEY.format(slot=work.slot)) is None


# --------------------------------------------------------------------------- review of #408


@pytest.mark.parametrize(("prefer", "picked"), [("priority", 2), ("reset-soonest", 3)])
def test_the_merged_pick_honours_rank_the_back_off_and_the_family_limit_at_once(
    fake_home: Path, prefer: str, picked: int
) -> None:
    """Merged in #401 for 0.9.0: #406's rank order (rank 1 is the machine default), #408's
    back-off (a kept reading is never picked by when old) and main's P0-W3 (a limit an
    account refused for a family is remembered; ``[accounts] prefer``), in one hand-over pick
    for an Opus agent. Rank 1 is skipped for its Opus mark, rank 2 for its old kept reading;
    of the two under the line, ``priority`` takes rank 3 and ``reset-soonest`` the one whose
    week resets first. With nothing to pick, rank 1 is still the machine default."""
    ranked = [_slot(f"{name}@example.com", f"tok-{name}") for name in ("a", "b", "c", "d")]
    a, b = ranked[:2]

    def week(percent: float, resets: datetime) -> dict[str, Any]:
        payload = _payload(percent, week=percent)
        payload["seven_day"]["resets_at"] = resets.isoformat()
        return payload

    endpoint = _Endpoint(
        {
            "tok-a": _payload(10),
            "tok-b": _payload(5),
            "tok-c": week(30, NOW + timedelta(days=5)),
            "tok-d": week(20, NOW + timedelta(days=1)),
        }
    )
    service.read_usage([b], now=NOW, fetch=endpoint)  # b's last good reading, at NOW
    endpoint.by_token["tok-b"] = (429, {"Retry-After": "3600"})
    later = NOW + timedelta(minutes=30)
    service.read_usage([b], now=later, fetch=endpoint)  # b is backed off from here
    with store_session() as store:  # a refused an Opus request: remembered until its reset
        service._write_limit(store, a.slot, "opus", NOW + timedelta(days=2), how="refused")

    choice, notes = service.headroom_choice(
        ranked,
        switch_at=90,
        now=later + timedelta(minutes=1),
        fetch=endpoint,
        least_bad=False,
        family="opus",
        prefer=prefer,
    )

    assert choice is not None and choice.slot == ranked[picked].slot, notes
    said = " | ".join(notes)
    assert f"{core.label(a)} skipped" in said, "the Opus mark"
    assert "too old to pick by" in said and core.label(b) in said, "the kept reading"
    assert "tok-a" not in endpoint.calls, "a marked account is never read"
    assert endpoint.calls.count("tok-b") == 2, "a backed-off account is not asked again"
    assert service.preferred_account(ranked) == a, "rank 1 is still the machine default"


@pytest.mark.parametrize(
    ("resets_in", "why"),
    [
        (timedelta(hours=3), "its last reading is from 4h ago, and a window has reset since"),
        (timedelta(hours=9), "its last reading is from 4h ago, too old to pick by"),
    ],
    ids=["window-reset", "window-open"],
)
@pytest.mark.parametrize(
    "max_age", [service.PICK_MAX_AGE_SECONDS, None], ids=["launch", "handover"]
)
def test_a_kept_reading_from_any_failure_is_never_picked_by_when_old(
    fake_home: Path, resets_in: timedelta, why: str, max_age: float | None
) -> None:
    """MAJ-1's probe: ``work`` read 20 % at NOW; at NOW+4h its read answers HTTP 500 and
    ``spare`` reads 50 %. The 4 h-old figure is shown on the row, never picked by."""
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint(
        {"tok-work": _payload(20, resets_at=NOW + resets_in), "tok-spare": _payload(50)}
    )
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500
    endpoint.by_token["tok-spare"] = _payload(50, resets_at=NOW + timedelta(hours=7))
    later = NOW + timedelta(hours=4)

    picked, notes = service.headroom_choice(
        [work, spare], switch_at=85, now=later, fetch=endpoint, max_age=max_age
    )
    assert picked is not None and picked.slot == spare.slot, notes
    skipped = [n for n in notes if n.startswith("headroom: account 2 skipped")]
    assert skipped == [f"headroom: account 2 skipped — HTTP 500 from the usage endpoint; {why}"]


def test_a_kept_reading_inside_the_picks_window_is_used_and_said(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint({"tok-work": _payload(20), "tok-spare": _payload(50)})
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500
    picked, notes = service.headroom_choice(
        [work, spare], switch_at=85, now=NOW + timedelta(minutes=2), fetch=endpoint
    )
    assert picked is not None and picked.slot == work.slot
    assert (
        "headroom: account 2 HTTP 500 from the usage endpoint — picked by its reading from 2m ago"
        in notes
    )


def test_the_fleet_dot_ignores_a_reset_or_old_kept_reading(fake_home: Path) -> None:
    """MAJ-1's probe: one account read 99 % with its window resetting at NOW+1h; at NOW+2h
    the read answers 500. The dot is empty (as on the rc base), not 'critical' from a
    window that is over — and a kept reading half an hour old does not count either."""
    work = _slot("work@example.com", "tok-work")
    service.cached_usage(
        [work],
        now=NOW,
        fetch=_Endpoint({"tok-work": _payload(99, resets_at=NOW + timedelta(hours=1))}),
    )
    down = _Endpoint({"tok-work": 500})
    summary = service.read_fleet_headroom([work], now=NOW + timedelta(hours=2), fetch=down)
    assert (summary.level, summary.usable, summary.left_by_slot) == ("empty", 0, {})
    shown = service.cached_usage([work], now=NOW + timedelta(hours=2), fetch=down)[work.slot]
    assert shown.available and shown.session_percent is None, "the reset window is emptied"
    assert shown.session_resets_at == NOW + timedelta(hours=1)

    other = _slot("other@example.com", "tok-other")
    service.cached_usage([other], now=NOW, fetch=_Endpoint({"tok-other": _payload(30)}))
    _new_process()
    old = service.read_fleet_headroom(
        [other], now=NOW + timedelta(minutes=30), fetch=_Endpoint({"tok-other": 500})
    )
    assert (old.level, old.usable) == ("empty", 0)
    _new_process()
    fresh = service.read_fleet_headroom(
        [other], now=NOW + timedelta(minutes=2), force=True, fetch=_Endpoint({"tok-other": 500})
    )
    assert fresh.usable == 1 and fresh.left_by_slot == {other.slot: 70.0}


def test_the_cli_shows_a_kept_readings_reset_window_as_reset(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slot("work@example.com", "tok-work")
    monkeypatch.setattr(
        service,
        "_http_get",
        _Endpoint({"tok-work": _payload(40, resets_at=NOW + timedelta(hours=1))}),
    )
    assert runner.invoke(app, ["accounts", "usage"]).exit_code == 0
    monkeypatch.setattr(service, "_http_get", _Endpoint({"tok-work": 503}))
    _at(monkeypatch, NOW + timedelta(hours=2))
    shown = runner.invoke(app, ["accounts", "usage"])
    assert "reset 1h ago" in shown.stdout, shown.stdout
    assert "40% ·" not in shown.stdout, "never the old figure beside its reset"
    assert "7% · resets" in shown.stdout  # the week's window is still open: its figure stays
    assert "showing the reading from 2h ago" in shown.stdout


def test_during_a_wait_the_cli_says_no_try_was_made_now(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: 40 min into a 6 h wait the line said 'last try failed just now … --refresh fetches
    now'. Nothing was sent, and --refresh will not send before the wait ends."""
    _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": _payload(40, resets_at=NOW + timedelta(hours=9))})
    monkeypatch.setattr(service, "_http_get", endpoint)
    runner.invoke(app, ["accounts", "usage"])
    endpoint.by_token["tok-work"] = (429, {"Retry-After": str(6 * 3600)})
    _at(monkeypatch, NOW + timedelta(minutes=5))
    runner.invoke(app, ["accounts", "usage", "--refresh"])
    _new_process()
    _at(monkeypatch, NOW + timedelta(minutes=45))
    shown = runner.invoke(app, ["accounts", "usage", "--refresh"])
    until = (NOW + timedelta(minutes=5, hours=6)).astimezone().strftime("%H:%M")
    assert "last try failed 40m ago" in shown.stdout, shown.stdout
    assert "just now" not in shown.stdout and "--refresh fetches now" not in shown.stdout
    assert f"rate-limited until {until}; --refresh waits too" in shown.stdout
    assert len(endpoint.calls) == 2


def test_a_stale_clear_never_removes_a_newer_wait(fake_home: Path) -> None:
    """N2: B reads an expired wait X; A records a new wait Y while B's read is in flight;
    B's 200 lands. B clears only the row it read, so Y stands."""
    work = _slot("work@example.com", "tok-work")
    key = service._BACKOFF_KEY.format(slot=work.slot)
    token = service._login_key(work, NOW)
    expired = service.UsageBackoff(
        slot=work.slot, until=NOW - timedelta(seconds=1), attempts=1, since=NOW, token=token
    )
    newer = service.UsageBackoff(
        slot=work.slot, until=NOW + timedelta(minutes=10), attempts=2, since=NOW, token=token
    )
    with store_session() as store:
        store.set_ui_state(key, expired.dump())
    answering = _Endpoint({"tok-work": _payload(30)})

    def racing(url: str, headers: Mapping[str, str], timeout: float) -> Any:
        with store_session() as store:  # another process records its 429 meanwhile
            store.set_ui_state(key, newer.dump())
        return answering(url, headers, timeout)

    assert service.read_usage([work], now=NOW, fetch=racing)[work.slot].available
    with store_session() as store:
        assert store.ui_state(key) == newer.dump()


def test_a_stale_write_never_shortens_a_newer_wait(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    key = service._BACKOFF_KEY.format(slot=work.slot)
    token = service._login_key(work, NOW)
    longer = service.UsageBackoff(
        slot=work.slot, until=NOW + timedelta(minutes=30), attempts=3, since=NOW, token=token
    )
    limited = _Endpoint({"tok-work": (429, {"Retry-After": "60"})})

    def racing(url: str, headers: Mapping[str, str], timeout: float) -> Any:
        with store_session() as store:
            store.set_ui_state(key, longer.dump())
        return limited(url, headers, timeout)

    service.read_usage([work], now=NOW, fetch=racing)
    with store_session() as store:
        assert store.ui_state(key) == longer.dump()


@pytest.mark.parametrize(
    "raw", ["{truncated", '{"until": "soon"}'], ids=["truncated", "other-shape"]
)
def test_a_wait_row_this_version_cannot_read_is_replaced(fake_home: Path, raw: str) -> None:
    """Review of #408, n1: an unreadable ``usage-backoff`` row was never replaced (compared as
    absent, its insert hit the row), so the slot was asked on every read. A 429 replaces it,
    and the wait it starts holds."""
    work = _slot("work@example.com", "tok-work")
    key = service._BACKOFF_KEY.format(slot=work.slot)
    with store_session() as store:
        store.set_ui_state(key, raw)
    endpoint = _Endpoint({"tok-work": (429, {"Retry-After": "600"})})

    service.read_usage([work], now=NOW, fetch=endpoint)
    again = service.read_usage([work], now=NOW + timedelta(minutes=1), fetch=endpoint)

    assert again[work.slot].rate_limited and endpoint.calls == ["tok-work"], "the wait holds"
    assert work.slot in service.usage_backoffs([work], now=NOW + timedelta(minutes=1))


def test_replacing_an_unreadable_wait_row_never_shortens_a_longer_one_written_meanwhile(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #401, n1: the unreadable row was overwritten blindly, so a valid, longer wait
    another process wrote between the read and the write was shortened. Replaced only as it
    was read, the longer wait stands."""
    work = _slot("work@example.com", "tok-work")
    key = service._BACKOFF_KEY.format(slot=work.slot)
    with store_session() as store:
        store.set_ui_state(key, "{truncated")
        cls = type(store)
    longer = service.UsageBackoff(
        slot=work.slot,
        until=NOW + timedelta(minutes=30),
        attempts=3,
        since=NOW,
        token=service._login_key(work, NOW),
    )
    real = cls.ui_state
    raced: list[bool] = []

    def ui_state(self: Any, name: str) -> str | None:
        value = real(self, name)
        if name == key and not raced and value == "{truncated":
            raced.append(True)  # another process writes its longer wait right after this read
            with store_session() as other:
                other.set_ui_state(key, longer.dump())
        return value

    monkeypatch.setattr(cls, "ui_state", ui_state)
    with store_session() as store:  # the write path alone, as a 429 records it
        held = service.UsageBackoff(
            slot=work.slot,
            until=NOW + timedelta(minutes=1),
            attempts=1,
            since=NOW,
            token=longer.token,
        )
        service._record_backoff(store, held, None)

    assert raced, "the race ran"
    with store_session() as store:
        assert real(store, key) == longer.dump(), "the longer wait stands"


def test_a_cached_read_keeps_the_family_weeks_the_live_one_saw(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tester's pass on #401, (a): a reading served from the store had no family weeks
    (the history has no columns for them), so ``accounts usage`` without ``--refresh``, nearly
    every read while asq runs, showed none, a spent Opus week included. They are kept with
    the sample they came with, and served with it; another sample of the slot is not given
    them."""
    work = _slot("work@example.com", "tok-work")
    payload = _payload(40, week=30)
    for family, used, days in (("opus", 100, 1), ("sonnet", 45, 3)):
        resets = (NOW + timedelta(days=days)).isoformat()
        payload[f"seven_day_{family}"] = {"utilization": used, "resets_at": resets}
    endpoint = _Endpoint({"tok-work": payload})
    live = service.cached_usage([work], now=NOW, fetch=endpoint)[work.slot]
    assert live.family_week_percent == {"opus": 100.0, "sonnet": 45.0}, "control: read live"

    _new_process()
    cached = service.cached_usage([work], now=NOW + timedelta(minutes=2), fetch=endpoint)
    assert len(endpoint.calls) == 1, "served from the store"
    assert cached[work.slot].family_week_percent == {"opus": 100.0, "sonnet": 45.0}
    assert cached[work.slot].family_week_resets_at["opus"] == NOW + timedelta(days=1)

    monkeypatch.setattr(service, "_http_get", endpoint)
    _at(monkeypatch, NOW + timedelta(minutes=3))
    _new_process()
    shown = runner.invoke(app, ["accounts", "usage"])
    assert shown.exit_code == 0, shown.output
    flat = " ".join(shown.stdout.split())  # the table wraps a long cell over its lines
    assert "opus 100%" in flat and "sonnet 45%" in flat, shown.stdout
    assert len(endpoint.calls) == 1, "still the stored reading"

    plain = _payload(20, week=10)  # a newer sample with no family week: none carried over
    endpoint.by_token["tok-work"] = plain
    _new_process()
    later = service.cached_usage([work], now=NOW + timedelta(minutes=10), fetch=endpoint)
    assert later[work.slot].family_week_percent == {}


@pytest.mark.parametrize(
    ("opus_resets", "picked"),
    [(timedelta(minutes=1), 0), (timedelta(days=1), 1)],
    ids=["reset-since", "control-still-spent"],
)
def test_a_cached_family_week_that_has_reset_is_not_read_as_spent(
    fake_home: Path, opus_resets: timedelta, picked: int
) -> None:
    """Review of #401, m2 (the reviewer's repro): a reads 10 % with its Opus week spent until
    NOW+1m, b 60 %. Three minutes on, a new process's pick for an Opus agent read a's Opus
    week as spent from the store cache, skipped it and took b. The week has reset: a is
    picked. Control: with the Opus week spent for a day, a is still skipped."""
    a = _slot("a@example.com", "tok-a")
    b = _slot("b@example.com", "tok-b")
    payload = _payload(10, week=10)
    payload["seven_day_opus"] = {
        "utilization": 100,
        "resets_at": (NOW + opus_resets).isoformat(),
    }
    endpoint = _Endpoint({"tok-a": payload, "tok-b": _payload(60, week=60)})
    service.read_usage([a, b], now=NOW, fetch=endpoint)
    _new_process()
    later = NOW + timedelta(minutes=3)

    choice, notes = service.headroom_choice(
        [a, b], switch_at=90, now=later, fetch=endpoint, max_age=300, family="opus"
    )

    assert choice is not None and choice.slot == [a, b][picked].slot, notes
    assert len(endpoint.calls) == 2, "served from the store cache"
    cached = service.cached_usage([a], now=later, fetch=endpoint)[a.slot]
    if picked == 0:
        assert "opus" not in cached.family_week_percent, "the figure for a week that is over"
        assert service._has_reset(cached, later), "a family reset counts as a reset"


def _new_token(account: Any, token: str) -> None:
    """An OAuth refresh, or a *Log in again* to the same account: a new token, the same login."""
    creds = core.credentials_path(account)
    data = json.loads(creds.read_text())
    data["claudeAiOauth"]["accessToken"] = token
    creds.write_text(json.dumps(data))


def test_a_wait_is_the_logins_not_the_slots(fake_home: Path) -> None:
    """N3: a slot signed into another account is not held to the last account's wait."""
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": (429, {"Retry-After": "3600"}), "tok-new": _payload(25)})
    service.read_usage([work], now=NOW, fetch=endpoint)
    assert work.slot in service.usage_backoffs([work], now=NOW)
    path = core.claude_json_path(work)
    data = json.loads(path.read_text())
    data["oauthAccount"].update(emailAddress="home@example.com", accountUuid="another-uuid")
    path.write_text(json.dumps(data))
    _new_token(work, "tok-new")
    reading = service.read_usage([work], now=NOW + timedelta(minutes=1), fetch=endpoint)
    assert reading[work.slot].session_percent == 25 and endpoint.calls == ["tok-work", "tok-new"]
    assert service.usage_backoffs([work], now=NOW + timedelta(minutes=1)) == {}


def test_a_token_refresh_of_the_same_login_keeps_the_wait_and_its_doubling(
    fake_home: Path,
) -> None:
    """Review of #408, m1: keyed to the token, every OAuth refresh and every *Log in again*
    lifted the wait, sent a request at once and started the doubling over, while the row said
    logging in again does not clear it. The same account keeps its wait, and its doubling."""
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": 429, "tok-refreshed": 429})
    first = service.read_usage([work], now=NOW, fetch=endpoint)[work.slot]
    assert first.rate_limited_until == NOW + timedelta(seconds=60)
    _new_token(work, "tok-refreshed")

    inside = service.read_usage([work], now=NOW + timedelta(seconds=30), fetch=endpoint)
    assert inside[work.slot].rate_limited and endpoint.calls == ["tok-work"], "still waiting"
    assert work.slot in service.usage_backoffs([work], now=NOW + timedelta(seconds=30))

    after = NOW + timedelta(seconds=61)
    again = service.read_usage([work], now=after, fetch=endpoint)[work.slot]
    assert endpoint.calls == ["tok-work", "tok-refreshed"]
    assert again.rate_limited_until == after + timedelta(seconds=120), "the doubling goes on"


def test_a_reading_recorded_just_after_anothers_429_is_kept_for_the_whole_wait(
    fake_home: Path,
) -> None:
    """Review of #408, MAJOR-1: process A's read is answered 429 with an hour to wait; process
    B's request, sent before A's wait was on disk, is answered 200 a second later and
    recorded. Forty minutes on, still inside the wait, that reading was served as live: no
    age, no wait, and the fleet's dot counted it. It is kept, with its age and its wait."""
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": (429, {"Retry-After": "3600"})})
    limited_at = NOW
    service.read_usage([work], now=limited_at, fetch=endpoint)  # A's 429: the wait is on disk
    taken = limited_at + timedelta(seconds=1)
    with store_session() as store:  # B's 200, recorded after it
        service._record_sample(
            store,
            work,
            ClaudeUsage(
                available=True,
                session_percent=10,
                session_resets_at=limited_at + timedelta(hours=4),
                week_percent=5,
                week_resets_at=limited_at + timedelta(days=5),
                fetched_at=taken,
            ),
            taken,
        )
    _new_process()
    later = limited_at + timedelta(minutes=40)

    shown = service.cached_usage([work], now=later, fetch=endpoint)[work.slot]
    dot = service.read_fleet_headroom([work], now=later, fetch=endpoint)

    assert shown.available and shown.session_percent == 10 and shown.fetched_at == taken
    assert service.is_kept(shown), "never served as live"
    assert shown.rate_limited and shown.rate_limited_until == limited_at + timedelta(hours=1)
    assert shown.last_failed_reason is not None and "rate-limited until" in shown.last_failed_reason
    assert (dot.level, dot.usable) == ("empty", 0), "the dot does not count a 40-minute-old figure"
    assert endpoint.calls == ["tok-work"], "nothing sent during the wait"


def test_sample_usage_honours_the_wait(fake_home: Path) -> None:
    """N5: ``sample_usage`` is public; it goes through the gate like every other read."""
    work = _slot("work@example.com", "tok-work")
    endpoint = _Endpoint({"tok-work": (429, {"Retry-After": "600"})})
    service.sample_usage(work, now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = _payload(30)
    held = service.sample_usage(work, now=NOW + timedelta(minutes=5), fetch=endpoint)
    assert held.rate_limited and endpoint.calls == ["tok-work"]
