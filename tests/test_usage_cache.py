"""The shared five-minute usage cache, the fleet's headroom in one word, and "% USED".

Three asks from the human, each with the control that would catch it going blind:

- **"Refresh max once every 5 mins."** ``cached_usage`` serves the newest
  recorded reading while it is younger than ``max_age`` and fetches only the
  rest, in one round; a fetch that failed is not retried inside the window
  either, unless the token it sent has changed. Fixed clock, scripted fetch.
- **"Green/orange/red dot … n x claude."** ``fleet_headroom`` is pure over
  accounts and readings; its bands and formula are pinned here.
- **"The agent saw usage percentage … as percentage remaining."** Every figure an
  agent reads says USED, and the JSON names both sides.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import harness
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ClaudeUsage, UsageSample
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import NOW
from tests.test_usage_aware_accounts import _payload, _slot, _Usage


def _reading(session: float | None, week: float | None = None, **more: Any) -> ClaudeUsage:
    return ClaudeUsage(
        available=True, session_percent=session, week_percent=week, fetched_at=NOW, **more
    )


def _account(slot: int, *, disabled: bool = False) -> ClaudeAccount:
    return ClaudeAccount(slot=slot, config_dir=Path(f"/nowhere/{slot}"), disabled=disabled)


# --------------------------------------------------------------------------- the cache


def test_a_reading_younger_than_five_minutes_is_served_without_a_request(
    fake_home: Path,
) -> None:
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(40)})

    first = service.cached_usage([work], now=NOW, fetch=fetch)
    assert fetch.calls == ["tok-work"] and first[work.slot].session_percent == 40
    assert first[work.slot].fetched_at == NOW

    fetch.by_token["tok-work"] = _payload(70)
    served = service.cached_usage([work], now=NOW + timedelta(minutes=4, seconds=59), fetch=fetch)
    assert fetch.calls == ["tok-work"]  # no second request inside the window
    assert served[work.slot].session_percent == 40
    assert served[work.slot].fetched_at == NOW  # "refreshed 4m ago", not now

    later = service.cached_usage([work], now=NOW + timedelta(minutes=5, seconds=1), fetch=fetch)
    assert fetch.calls == ["tok-work", "tok-work"] and later[work.slot].session_percent == 70


def test_force_fetches_and_max_age_is_the_callers(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(40)})
    service.cached_usage([work], now=NOW, fetch=fetch)

    service.cached_usage([work], now=NOW + timedelta(seconds=10), force=True, fetch=fetch)
    assert len(fetch.calls) == 2
    service.cached_usage([work], now=NOW + timedelta(seconds=41), max_age=30.0, fetch=fetch)
    assert len(fetch.calls) == 3


def test_only_the_stale_accounts_are_fetched_in_one_recording_round(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _slot("work@example.com", "tok-work")
    personal = _slot("personal@example.com", "tok-personal")
    fetch = _Usage({"tok-work": _payload(40), "tok-personal": _payload(60)})
    service.cached_usage([work], now=NOW, fetch=fetch)
    rounds: list[list[int]] = []
    real = service.read_usage

    def spy(accounts: Any, **kwargs: Any) -> dict[int, ClaudeUsage]:
        rounds.append([account.slot for account in accounts])
        return real(accounts, **kwargs)

    monkeypatch.setattr(service, "read_usage", spy)
    readings = service.cached_usage([personal, work], now=NOW + timedelta(minutes=1), fetch=fetch)

    assert rounds == [[personal.slot]]
    assert list(readings) == [personal.slot, work.slot]  # the caller's order
    with store_session() as store:  # the fetched one was recorded, for the next reader
        assert len(store.usage_samples(personal.slot, since=NOW - timedelta(days=1))) == 1


def test_a_reading_whose_window_has_reset_since_is_not_served(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(97, resets_at=NOW + timedelta(minutes=2))})
    service.cached_usage([work], now=NOW, fetch=fetch)

    fetch.by_token["tok-work"] = _payload(1, resets_at=NOW + timedelta(hours=5))
    after_reset = service.cached_usage([work], now=NOW + timedelta(minutes=3), fetch=fetch)
    assert len(fetch.calls) == 2 and after_reset[work.slot].session_percent == 1


def test_a_failed_fetch_waits_out_the_window_unless_the_token_changes(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({})  # every token rejected

    first = service.cached_usage([work], now=NOW, fetch=fetch)
    assert not first[work.slot].available and first[work.slot].needs_login
    assert first[work.slot].fetched_at == NOW  # when the failure was found out

    service.cached_usage([work], now=NOW + timedelta(minutes=2), fetch=fetch)
    assert fetch.calls == ["tok-work"]  # a rejected token is not re-sent every tick

    # A session refreshed the token: the new one is asked at once.
    creds = core.credentials_path(work)
    data = json.loads(creds.read_text())
    data["claudeAiOauth"]["accessToken"] = "tok-fresh"
    creds.write_text(json.dumps(data))
    fetch.by_token["tok-fresh"] = _payload(30)
    fresh = service.cached_usage([work], now=NOW + timedelta(minutes=3), fetch=fetch)
    assert fetch.calls == ["tok-work", "tok-fresh"] and fresh[work.slot].session_percent == 30


def test_an_expired_token_needs_a_login_and_is_never_sent(fake_home: Path) -> None:
    stale = _slot("stale@example.com", "tok-stale", expires_in=timedelta(hours=-1))
    fetch = _Usage({"tok-stale": _payload(10)})

    reading = service.cached_usage([stale], now=NOW, fetch=fetch)[stale.slot]
    assert not reading.available and reading.needs_login and fetch.calls == []
    not_signed_in = service.usage(core.create_account(), now=NOW, fetch=fetch)
    assert not not_signed_in.available and not not_signed_in.needs_login


def _swap_token(account: ClaudeAccount, token: str) -> None:
    """What a /login into another account, or Claude Code refreshing it, leaves on disk."""
    creds = core.credentials_path(account)
    data = json.loads(creds.read_text())
    data["claudeAiOauth"]["accessToken"] = token
    creds.write_text(json.dumps(data))


def test_a_newer_failure_is_not_masked_by_an_older_success(fake_home: Path) -> None:
    """A refresh that found the token rejected stays the answer: the success recorded
    before it is older, and the newest result wins (tester's repro 1, round 1)."""
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(40)})
    assert service.cached_usage([work], now=NOW, fetch=fetch)[work.slot].session_percent == 40

    del fetch.by_token["tok-work"]  # now 401
    refreshed = service.cached_usage(
        [work], now=NOW + timedelta(minutes=1), force=True, fetch=fetch
    )
    assert refreshed[work.slot].needs_login

    served = service.cached_usage([work], now=NOW + timedelta(minutes=2), fetch=fetch)
    assert len(fetch.calls) == 2, "the failure is served, not re-asked"
    reading = served[work.slot]
    assert not reading.available and reading.needs_login
    assert reading.fetched_at == NOW + timedelta(minutes=1)
    summary = service.fleet_headroom([work], served)
    assert (summary.level, summary.needs_login, summary.usable) == ("empty", 1, 0)

    # And the other way round: a success after the failure (the same token, the
    # endpoint back) is newer than it, and is what is served.
    fetch.by_token["tok-work"] = _payload(55)
    service.cached_usage([work], now=NOW + timedelta(minutes=3), force=True, fetch=fetch)
    again = service.cached_usage([work], now=NOW + timedelta(minutes=4), fetch=fetch)
    assert again[work.slot].session_percent == 55 and len(fetch.calls) == 3


def _unreachable(url: str, headers: Any, timeout: float) -> tuple[int, bytes]:
    raise OSError("network is unreachable")


@pytest.mark.parametrize("outage", [_unreachable, _Usage({"tok-work": 503})], ids=["down", "503"])
def test_a_failure_that_says_nothing_of_the_token_never_hides_a_good_reading(
    fake_home: Path, outage: Any
) -> None:
    """Review of #261, m4: a forced refresh that could not reach the endpoint hid the 40 %
    read a minute before it for five minutes. The good reading serves while it is in the
    window; the failure says nothing against it, and is carried with it
    (``last_failed_at``) — by the forced round's own answer too, and by every read after:
    a forced try that failed showed the failure, the next plain read the figure with no
    word of it (reopened). Still at most one try per window."""
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(40)})
    service.cached_usage([work], now=NOW, fetch=fetch)
    failed_at = NOW + timedelta(minutes=1)

    tried = service.cached_usage([work], now=failed_at, force=True, fetch=outage)
    assert tried[work.slot].session_percent == 40, "the forced round keeps the figure"
    assert (tried[work.slot].fetched_at, tried[work.slot].last_failed_at) == (NOW, failed_at)

    for minutes in (2, 4):
        served = service.cached_usage([work], now=NOW + timedelta(minutes=minutes), fetch=fetch)
        assert served[work.slot].session_percent == 40
        assert served[work.slot].fetched_at == NOW  # "3m ago": how old it is, never "now"
        assert served[work.slot].last_failed_at == failed_at, "and the failed try with it"
    assert fetch.calls == ["tok-work"]

    # Past the good reading's window, the failure is the newest word until its own ends.
    gone = service.cached_usage([work], now=NOW + timedelta(minutes=5, seconds=1), fetch=fetch)
    assert not gone[work.slot].available and fetch.calls == ["tok-work"]
    back = service.cached_usage([work], now=NOW + timedelta(minutes=6, seconds=1), fetch=fetch)
    assert back[work.slot].session_percent == 40 and len(fetch.calls) == 2
    assert back[work.slot].last_failed_at is None, "the latest try read it"


def test_a_rejected_token_on_a_forced_read_still_wins_over_the_figure(fake_home: Path) -> None:
    """The one failure that contradicts a reading: a 401 says the token is refused, so a
    forced round that meets it shows it, and so does the next read."""
    work = _slot("work@example.com", "tok-work")
    service.cached_usage([work], now=NOW, fetch=_Usage({"tok-work": _payload(40)}))
    later = NOW + timedelta(minutes=1)
    tried = service.cached_usage([work], now=later, force=True, fetch=_Usage({"tok-work": 401}))
    assert not tried[work.slot].available and tried[work.slot].needs_login
    served = service.cached_usage([work], now=later + timedelta(minutes=1))
    assert served[work.slot].needs_login


def test_reads_of_one_slot_at_once_share_one_fetch(fake_home: Path) -> None:
    """Review of #261, m4: the sidebar's read and the page's, at the same moment, each sent
    a request. The second waits for the first and is served what it read."""
    import threading
    import time

    work = _slot("work@example.com", "tok-work")
    endpoint = _Usage({"tok-work": _payload(40)})

    def slow(url: str, headers: Any, timeout: float) -> tuple[int, bytes]:
        time.sleep(0.3)
        return endpoint(url, headers, timeout)

    start = threading.Barrier(3)
    got: list[float | None] = []

    def read() -> None:
        start.wait()
        got.append(service.cached_usage([work], now=NOW, fetch=slow)[work.slot].session_percent)

    readers = [threading.Thread(target=read) for _ in range(3)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join()
    assert endpoint.calls == ["tok-work"] and got == [40, 40, 40]

    # A forced read still asks, and a plain one beside it is served what it read.
    endpoint.by_token["tok-work"] = _payload(55)
    forced = service.cached_usage([work], now=NOW, force=True, fetch=slow)
    assert forced[work.slot].session_percent == 55 and len(endpoint.calls) == 2


def test_a_signed_out_slot_is_not_shown_its_last_figure(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/logout removes the credentials file: the slot sends no token, so nothing it
    recorded is served, and it says why, as before the cache (tester's repro 2)."""
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(40)})
    service.cached_usage([work], now=NOW, fetch=fetch)

    core.credentials_path(work).unlink()
    reading = service.cached_usage([work], now=NOW + timedelta(minutes=1), fetch=fetch)[work.slot]
    assert not reading.available and reading.reason == "no credentials file — not signed in"
    assert fetch.calls == ["tok-work"]  # told without a request

    # The same through the CLI, on the real clock.
    later = datetime.now(UTC) + timedelta(hours=3)
    personal = _slot("personal@example.com", "tok-personal")
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-personal": _payload(40, resets_at=later)})
    )
    first = runner.invoke(app, ["--json", "accounts", "usage", str(personal.slot)])
    assert json.loads(first.stdout)[0]["usage"]["session_percent"] == 40
    core.credentials_path(personal).unlink()
    gone = runner.invoke(app, ["--json", "accounts", "usage", str(personal.slot)])
    [row] = json.loads(gone.stdout)
    assert row["usage"]["available"] is False and row["signed_in"] is False
    assert row["usage"]["reason"] == "no credentials file — not signed in"


def test_a_reading_is_served_only_to_the_token_that_took_it(fake_home: Path) -> None:
    """A slot signed into another login inside the window is asked, not shown the last
    login's figure (tester's round-1 note); back on the first token, its reading serves."""
    work = _slot("work@example.com", "tok-work")
    fetch = _Usage({"tok-work": _payload(95), "tok-other": _payload(5)})
    service.cached_usage([work], now=NOW, fetch=fetch)

    _swap_token(work, "tok-other")
    other = service.cached_usage([work], now=NOW + timedelta(minutes=1), fetch=fetch)
    assert fetch.calls == ["tok-work", "tok-other"] and other[work.slot].session_percent == 5

    _swap_token(work, "tok-work")
    back = service.cached_usage([work], now=NOW + timedelta(minutes=2), fetch=fetch)
    assert len(fetch.calls) == 2 and back[work.slot].session_percent == 95


def test_every_recording_reader_marks_its_token_and_never_stores_it(fake_home: Path) -> None:
    """A headroom pick's live round (``read_usage``) serves the cache too, and what the
    store keeps is a fingerprint: the token is nowhere in ``context.db``."""
    from aisquare.core.paths import db_path

    work = _slot("work@example.com", "tok-work-secret")
    fetch = _Usage({"tok-work-secret": _payload(40)})
    service.read_usage([work], now=NOW, fetch=fetch)
    service.cached_usage([work], now=NOW + timedelta(minutes=1), fetch=fetch)
    assert len(fetch.calls) == 1

    with store_session() as store:
        [sample] = store.usage_samples(work.slot, since=NOW - timedelta(days=1))
    assert sample.token_fingerprint and "tok-work-secret" not in sample.token_fingerprint
    assert b"tok-work-secret" not in db_path().read_bytes()


def test_a_reading_recorded_before_the_fingerprint_was_kept_is_not_served(
    fake_home: Path,
) -> None:
    """A v27 row says nothing of its token, so no token is served it: one fetch after
    the upgrade, and the trends still read it."""
    work = _slot("work@example.com", "tok-work")
    with store_session() as store:
        store.add_usage_sample(UsageSample(slot=work.slot, fetched_at=NOW, session_percent=12.0))
    fetch = _Usage({"tok-work": _payload(40)})
    reading = service.cached_usage([work], now=NOW + timedelta(minutes=1), fetch=fetch)
    assert fetch.calls == ["tok-work"] and reading[work.slot].session_percent == 40


def test_describe_age_is_the_refreshed_line() -> None:
    assert service.describe_age(NOW, now=NOW + timedelta(seconds=59)) == "just now"
    assert service.describe_age(NOW, now=NOW + timedelta(minutes=3, seconds=5)) == "3m ago"
    assert service.describe_age(NOW, now=NOW + timedelta(hours=2, minutes=1)) == "2h ago"
    assert service.describe_age(NOW + timedelta(minutes=1), now=NOW) == "just now"  # clock skew


# --------------------------------------------------------------------------- fleet headroom


@pytest.mark.parametrize(
    ("used", "level"),
    [
        (0.0, "good"),
        (49.0, "good"),  # 51 % left
        (50.0, "fair"),  # 50 % left is fair, not good
        (75.0, "fair"),  # 25 % left
        (75.5, "low"),
        (95.0, "low"),  # 5 % left
        (95.5, "critical"),
        (99.9, "critical"),
        (100.0, "empty"),
        (130.0, "empty"),  # over the limit is no room, not negative room
    ],
)
def test_fleet_headroom_bands(used: float, level: str) -> None:
    summary = service.fleet_headroom([_account(2)], {2: _reading(used)})
    assert summary.level == level and summary.usable == 1
    assert summary.percent_left == pytest.approx(max(0.0, 100.0 - used))


def test_fleet_headroom_is_the_mean_left_of_the_usable_accounts_and_counts_logins() -> None:
    accounts = [_account(1), _account(2), _account(3), _account(4), _account(5, disabled=True)]
    readings = {
        1: _reading(20.0, week=10.0),  # 80 left
        2: _reading(5.0, week=90.0),  # the week is fuller: 10 left
        3: ClaudeUsage(available=False, reason="the token was rejected", needs_login=True),
        4: ClaudeUsage(available=False, reason="HTTP 503 from the usage endpoint"),
        5: _reading(0.0),  # disabled: never counted, however empty
    }
    summary = service.fleet_headroom(accounts, readings)

    assert summary.usable == 2 and summary.needs_login == 1
    assert summary.left_by_slot == {1: 80.0, 2: 10.0}
    assert summary.percent_left == pytest.approx(45.0) and summary.level == "fair"
    assert summary.fetched_at == NOW


def test_fleet_headroom_says_unknown_before_any_reading_and_empty_with_nothing_usable() -> None:
    assert service.fleet_headroom([_account(2)], {}).level == "unknown"
    assert service.fleet_headroom([], {}).level == "empty"
    assert service.fleet_headroom([_account(2, disabled=True)], {2: _reading(0)}).level == "empty"
    expired = ClaudeUsage(available=False, reason="expired", needs_login=True)
    nothing = service.fleet_headroom([_account(2), _account(3)], {2: expired})
    assert nothing.level == "empty" and nothing.usable == 0 and nothing.needs_login == 1
    assert nothing.percent_left is None


def test_fleet_headroom_documents_its_formula() -> None:
    """The help screen quotes this docstring; it must keep saying how the figure is made."""
    doc = service.fleet_headroom.__doc__ or ""
    assert "100 - max(session % used, week % used)" in doc
    for word in ("good", "fair", "low", "critical", "empty", "unknown", "mean"):
        assert word in doc
    assert (service.HEADROOM_GOOD_ABOVE, service.HEADROOM_FAIR_FROM) == (50.0, 25.0)
    assert service.HEADROOM_LOW_FROM == 5.0


def test_read_fleet_headroom_reads_the_signed_in_enabled_accounts_through_the_cache(
    fake_home: Path,
) -> None:
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    fetch = _Usage({"tok-work": _payload(40), "tok-personal": _payload(90)})
    service.set_disabled("3", True)

    summary = service.read_fleet_headroom(now=NOW, fetch=fetch)
    assert fetch.calls == ["tok-work"]  # not slot 1 (signed out), not the disabled slot 3
    assert summary.usable == 1 and summary.percent_left == pytest.approx(60.0)
    assert summary.level == "good"

    service.read_fleet_headroom(now=NOW + timedelta(minutes=1), fetch=fetch)
    assert fetch.calls == ["tok-work"]  # the sidebar's tick is served by the cache


# --------------------------------------------------------------------------- "% USED"


def test_the_json_names_used_and_left_and_keeps_the_old_keys() -> None:
    dumped = _reading(40.0, week=7.0).model_dump(mode="json")
    assert dumped["session_percent"] == 40.0 and dumped["week_percent"] == 7.0
    assert dumped["session_used_percent"] == 40.0 and dumped["session_left_percent"] == 60.0
    assert dumped["week_used_percent"] == 7.0 and dumped["week_left_percent"] == 93.0
    unread = ClaudeUsage(available=False, reason="no reading").model_dump(mode="json")
    assert unread["session_left_percent"] is None and unread["needs_login"] is False
    assert ClaudeUsage.model_validate(dumped).session_percent == 40.0  # still reads back
    assert "percent USED (0-100)" in (ClaudeUsage.__doc__ or "")


def test_accounts_usage_says_used_in_the_headers_and_the_json(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slot("work@example.com", "tok-work")
    monkeypatch.setattr(service, "_http_get", _Usage({"tok-work": _payload(40, week=12)}))

    table = runner.invoke(app, ["accounts", "usage", "2"])
    assert table.exit_code == 0, table.output
    assert "session used" in table.stdout and "week used" in table.stdout
    assert "percent USED" in table.stdout

    as_json = runner.invoke(app, ["--json", "accounts", "usage", "2"])
    [row] = json.loads(as_json.stdout)
    usage = row["usage"]
    assert usage["session_used_percent"] == 40.0 and usage["session_left_percent"] == 60.0
    assert usage["week_used_percent"] == 12.0 and usage["week_left_percent"] == 88.0


def test_the_cli_never_calls_a_failed_read_a_refresh(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #261, m5: with the endpoint down, ``accounts usage`` and ``list --usage``
    said "usage as of just now". They follow the Accounts page's rule: the age of the
    oldest reading actually read, and "last try failed" for a usable row that read nothing;
    a row that needs a login counts in neither half."""
    _slot("work@example.com", "tok-work")
    _slot("personal@example.com", "tok-personal")
    monkeypatch.setattr(service, "_http_get", _unreachable)
    for command in (["accounts", "usage"], ["accounts", "list", "--usage"]):
        down = runner.invoke(app, command)
        assert down.exit_code == 0, down.output
        assert "usage as of" not in down.stdout, command
        assert "no usage read · last try failed just now" in down.stdout, command

    _slot("third@example.com", "tok-third")
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-work": _payload(40), "tok-personal": 503})
    )  # tok-third: refused (401)
    mixed = runner.invoke(app, ["accounts", "usage", "--refresh"])
    assert "usage as of just now · last try failed just now (percent USED" in mixed.stdout
    monkeypatch.setattr(service, "_now", lambda: NOW + timedelta(minutes=3))
    later = runner.invoke(app, ["accounts", "usage"])  # served: the 40 % and the 503
    assert "usage as of 3m ago · last try failed 3m ago (percent USED" in later.stdout

    refused = runner.invoke(app, ["accounts", "usage", "4"])  # needs a login: its row says so
    assert refused.exit_code == 0, refused.output
    assert "usage as of" not in refused.stdout and "last try" not in refused.stdout


def test_a_refresh_that_fails_keeps_the_figure_and_the_cli_says_the_try_failed(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tester's repro (reopen of the m4/m5 fix): 40 % read at T0, the endpoint at 503,
    ``--refresh`` a minute later said "no usage read · last try failed just now" though
    the 40 % stood, and a plain read a minute after that said "usage as of 2m ago" with no
    word of the try. The figure stays, and the line says both."""
    _slot("work@example.com", "tok-work")
    monkeypatch.setattr(service, "_http_get", _Usage({"tok-work": _payload(40)}))
    first = runner.invoke(app, ["accounts", "usage"])
    assert "usage as of just now (percent USED" in first.stdout, first.stdout

    monkeypatch.setattr(service, "_http_get", _Usage({"tok-work": 503}))
    monkeypatch.setattr(service, "_now", lambda: NOW + timedelta(minutes=1))
    refreshed = runner.invoke(app, ["accounts", "usage", "--refresh"])
    assert refreshed.exit_code == 0, refreshed.output
    assert "40%" in refreshed.stdout and "503" not in refreshed.stdout, refreshed.stdout
    assert "usage as of 1m ago · last try failed just now (percent USED" in refreshed.stdout

    monkeypatch.setattr(service, "_now", lambda: NOW + timedelta(minutes=2))
    later = runner.invoke(app, ["accounts", "usage"])
    assert "40%" in later.stdout
    assert "usage as of 2m ago · last try failed 1m ago (percent USED" in later.stdout


def test_the_managers_cycle_says_figures_are_used_and_headroom_is_left() -> None:
    cycle = " ".join(harness.role_cycle("manager", "abcd1234"))
    assert (
        "Usage figures are the share USED (40% used = 60% left); headroom is what is left." in cycle
    )
