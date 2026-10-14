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
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import harness
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ClaudeUsage
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import NOW
from tests.test_claude_accounts import fake_home as _redirected_home
from tests.test_usage_aware_accounts import _payload, _slot, _Usage

#: Re-exported so pytest collects it here (see tests/test_account_defaults.py).
fake_home = _redirected_home


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


def test_the_managers_cycle_says_figures_are_used_and_headroom_is_left() -> None:
    cycle = " ".join(harness.role_cycle("manager", "abcd1234"))
    assert (
        "Usage figures are the share USED (40% used = 60% left); headroom is what is left." in cycle
    )
