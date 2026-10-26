"""A reading kept past the pick's window, over the line, with no reset since, counts as full
(manager decision, 2026-10-26, from tester-14's design question on #14).

Within one usage window usage only rises until the reset, so a kept reading (the last good
figure a failed read shows) that was OVER ``switch_at`` in a window that has not reset since is
a floor under the account now: the pick skips it as full, whatever its age, and says how old
the reading is. A kept reading UNDER the line and older than the pick's window says nothing of
now and stays unread (#14's M1: the launch's agent rung keeps its place), and one from before a
reset is no figure at all. An agent that reads its usage offline follows the same rule by the
age of the record it was read from: Codex's rollout, the synthetic agent's quota file.

Driven as ``tests/test_usage_backoff.py`` drives a kept reading: a scripted endpoint, the real
store, and a clock moved with the readings.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import NOW
from tests.test_codex_accounts import _rollout, _sign_in, _token_count, _window
from tests.test_usage_aware_accounts import _payload, _slot
from tests.test_usage_backoff import _Endpoint, _new_process

LINE = 85
"""``[accounts] switch_at``, the default."""


def _full_a_while_ago(
    resets_in: timedelta = timedelta(hours=3), *, percent: float = 95.0, week: float | None = None
) -> dict[str, Any]:
    return _payload(percent, resets_at=NOW + resets_in, week=week)


def _two(spare_percent: float = 50.0) -> tuple[Any, Any, _Endpoint]:
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint({"tok-work": _full_a_while_ago(), "tok-spare": _payload(spare_percent)})
    return work, spare, endpoint


def _decide(
    work: Any,
    spare: Any,
    endpoint: _Endpoint,
    *,
    when: datetime,
    max_age: float | None,
    **more: Any,
) -> service.HeadroomDecision:
    _new_process()
    return service.headroom_decision(
        [work, spare], switch_at=LINE, now=when, fetch=endpoint, max_age=max_age, **more
    )


@pytest.mark.parametrize(
    "max_age", [service.PICK_MAX_AGE_SECONDS, None], ids=["launch", "handover"]
)
@pytest.mark.parametrize("failure", [500, (429, {"Retry-After": "7200"})], ids=["http-500", "429"])
def test_a_kept_reading_over_the_line_with_no_reset_since_is_passed_over_as_full(
    fake_home: Path, max_age: float | None, failure: Any
) -> None:
    """``work`` read 95 % at NOW; two hours on its read fails and ``spare`` reads 50 %. The old
    figure is a floor (its five hours end at NOW+3h): ``work`` is full, the launch's agent rung
    included, which used to keep the place of any account it could not read."""
    work, spare, endpoint = _two()
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = failure
    later = NOW + timedelta(hours=2)

    for keeps_place in (False, True):
        decision = _decide(
            work, spare, endpoint, when=later, max_age=max_age, unread_keeps_place=keeps_place
        )
        assert decision.account is not None and decision.account.slot == spare.slot, decision.notes
        assert decision.measured, "a floor decided it"
        said = " | ".join(decision.notes)
        assert (
            f"{core.label(work)} " in said
            and "its reading from 2h ago is over the 85%-used line and no window has reset "
            "since, so it counts as full"
            in said
        ), decision.notes


@pytest.mark.parametrize(
    ("percent", "full"), [(LINE, True), (LINE - 0.5, False)], ids=["on-the-line", "just-under"]
)
def test_the_line_itself_is_full_and_just_under_it_is_not(
    fake_home: Path, percent: float, full: bool
) -> None:
    """``under`` means under ``switch_at`` (``pct < switch_at``): a floor ON the line is full."""
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint(
        {"tok-work": _full_a_while_ago(percent=percent), "tok-spare": _payload(50)}
    )
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500

    decision = _decide(
        work,
        spare,
        endpoint,
        when=NOW + timedelta(hours=2),
        max_age=service.PICK_MAX_AGE_SECONDS,
        unread_keeps_place=True,
    )

    assert any("counts as full" in n for n in decision.notes) is full, decision.notes
    expected = spare if full else work
    assert decision.account is not None and decision.account.slot == expected.slot


def test_a_fresh_kept_reading_inside_the_window_is_still_picked_by_and_said(
    fake_home: Path,
) -> None:
    """Control: the rule is for what the window no longer covers. A hand-over's window is five
    minutes: two minutes on, the 95 % reading is used as it always was (picked by, with its
    age) and the account is skipped by its figure."""
    work, spare, endpoint = _two()
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500

    decision = _decide(work, spare, endpoint, when=NOW + timedelta(minutes=2), max_age=None)

    assert decision.account is not None and decision.account.slot == spare.slot
    assert any("picked by its reading from 2m ago" in n for n in decision.notes), decision.notes
    assert not any("counts as full" in n for n in decision.notes)


@pytest.mark.parametrize(
    "max_age", [service.PICK_MAX_AGE_SECONDS, None], ids=["launch", "handover"]
)
def test_a_kept_reading_under_the_line_older_than_the_window_stays_unread(
    fake_home: Path, max_age: float | None
) -> None:
    """#14's M1, unchanged: 20 % two hours ago says nothing of now. A launch's agent rung
    keeps the account's place (unmeasured is not full); a pick that wants a measurement skips
    it, with the reason."""
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    endpoint = _Endpoint({"tok-work": _payload(20.0), "tok-spare": _payload(50.0)})
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500
    later = NOW + timedelta(hours=2)

    keeps = _decide(work, spare, endpoint, when=later, max_age=max_age, unread_keeps_place=True)
    assert keeps.account is not None and keeps.account.slot == work.slot
    assert not keeps.measured and any("keeps its place" in n for n in keeps.notes), keeps.notes

    skips = _decide(work, spare, endpoint, when=later, max_age=max_age)
    assert skips.account is not None and skips.account.slot == spare.slot
    assert any("its last reading is from 2h ago, too old to pick by" in n for n in skips.notes)
    assert not any("counts as full" in n for n in keeps.notes + skips.notes)


@pytest.mark.parametrize("unread_keeps_place", [False, True], ids=["pick", "agent-rung"])
def test_a_reading_from_before_a_reset_is_not_used_even_over_the_line(
    fake_home: Path, unread_keeps_place: bool
) -> None:
    """95 % in a five-hour window that ended at NOW+3h: at NOW+4h the new window starts near
    0 %, and the old figure is no floor. ``work`` is unread, not full."""
    work, spare, endpoint = _two()
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500
    endpoint.by_token["tok-spare"] = _payload(50, resets_at=NOW + timedelta(hours=9))

    decision = _decide(
        work,
        spare,
        endpoint,
        when=NOW + timedelta(hours=4),
        max_age=service.PICK_MAX_AGE_SECONDS,
        unread_keeps_place=unread_keeps_place,
    )

    said = " | ".join(decision.notes)
    assert "counts as full" not in said
    assert "and a window has reset since" in said or "keeps its place" in said, decision.notes
    expected = work if unread_keeps_place else spare
    assert decision.account is not None and decision.account.slot == expected.slot


def test_the_window_that_has_not_reset_is_the_floor_when_another_has() -> None:
    """The week is the limit: the account's five hours reset and its week, 95 %, did not."""
    from aisquare.models import ClaudeUsage, UsageWindow

    reading = ClaudeUsage(
        available=True,
        windows=(
            UsageWindow(name="session", minutes=300, used_percent=95.0, resets_at=NOW),
            UsageWindow(
                name="week", minutes=10080, used_percent=90.0, resets_at=NOW + timedelta(days=3)
            ),
        ),
    )

    floor = service._floor(reading, NOW + timedelta(hours=1))

    assert floor is not None and [w.name for w in floor.windows] == ["week"]
    assert service.headroom_percent(floor) == 90.0
    assert service._floor(reading, NOW + timedelta(days=4)) is None, "both over: nothing left"
    assert service._floor(reading.model_copy(update={"windows": ()}), NOW) is None
    undated = UsageWindow(name="session", minutes=300, used_percent=99.0)
    assert service._floor(reading.model_copy(update={"windows": (undated,)}), NOW) is None, (
        "a window that names no reset proves nothing about now"
    )


def test_a_weekly_floor_counts_after_the_five_hours_have_reset(fake_home: Path) -> None:
    work = _slot("work@example.com", "tok-work")
    spare = _slot("spare@example.com", "tok-spare")
    payload = _payload(30.0, week=95.0)
    payload["seven_day"]["resets_at"] = (NOW + timedelta(days=3)).isoformat()
    endpoint = _Endpoint({"tok-work": payload, "tok-spare": _payload(40.0)})
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500

    decision = _decide(
        work, spare, endpoint, when=NOW + timedelta(hours=5), max_age=service.PICK_MAX_AGE_SECONDS
    )

    assert decision.account is not None and decision.account.slot == spare.slot, decision.notes
    assert any("its reading from 5h ago is over the 85%-used line" in n for n in decision.notes)


def test_every_account_full_by_a_floor_leaves_no_answer_for_a_hand_over_that_wants_room(
    fake_home: Path,
) -> None:
    work, spare, endpoint = _two()
    endpoint.by_token["tok-spare"] = _payload(96.0, resets_at=NOW + timedelta(hours=3))
    service.cached_usage([work, spare], now=NOW, fetch=endpoint)
    endpoint.by_token["tok-work"] = 500
    endpoint.by_token["tok-spare"] = 500

    decision = _decide(
        work, spare, endpoint, when=NOW + timedelta(hours=1), max_age=None, least_bad=False
    )

    assert decision.account is None
    assert sum("counts as full" in n for n in decision.notes) == 2, decision.notes


# --- an agent that reads its usage offline: Codex's rollout --------------------------------


def _codex_account(percent: float, *, recorded_ago: timedelta, resets_in: timedelta) -> Any:
    """A Codex slot whose newest rollout record, ``recorded_ago`` before NOW+3h, read ``percent``
    in a five-hour window that resets ``resets_in`` after NOW."""
    account = core.create_account("codex")
    _sign_in(account.config_dir, "x@example.com")
    recorded = NOW + timedelta(hours=3) - recorded_ago
    _rollout(
        account.config_dir,
        [_token_count(recorded, _window(percent, 300, NOW + resets_in))],
    )
    return account


@pytest.fixture
def both_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    real = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: (
            f"/bin/{name}" if name in {"claude", "codex"} else real(name, *a, **k)
        ),
    )


def test_a_codex_reading_over_the_line_from_hours_ago_is_full_and_says_how_old_it_is(
    fake_home: Path, both_installed: None
) -> None:
    """Its age is its rollout record's: 95 % recorded at NOW, in a window that ends at NOW+4h,
    read at NOW+3h. Unlike a kept reading it needs no store sample, and it is skipped the same."""
    claude = _slot("work@example.com", "tok-work")
    codex = _codex_account(95.0, recorded_ago=timedelta(hours=3), resets_in=timedelta(hours=4))
    service.move(str(codex.slot), "top")
    endpoint = _Endpoint({"tok-work": _payload(20.0, resets_at=NOW + timedelta(hours=9))})

    decision = service.headroom_decision(
        [codex, claude],
        switch_at=LINE,
        now=NOW + timedelta(hours=3),
        fetch=endpoint,
        max_age=service.PICK_MAX_AGE_SECONDS,
        unread_keeps_place=True,
    )

    assert decision.account is not None and decision.account.slot == claude.slot, decision.notes
    assert any(
        "its reading from 3h ago is over the 85%-used line and no window has reset since" in n
        for n in decision.notes
    ), decision.notes


def test_a_codex_reading_whose_window_has_reset_reads_empty_and_is_not_full(
    fake_home: Path, both_installed: None
) -> None:
    claude = _slot("work@example.com", "tok-work")
    codex = _codex_account(95.0, recorded_ago=timedelta(hours=3), resets_in=timedelta(hours=1))
    service.move(str(codex.slot), "top")
    endpoint = _Endpoint({"tok-work": _payload(20.0, resets_at=NOW + timedelta(hours=9))})

    decision = service.headroom_decision(
        [codex, claude],
        switch_at=LINE,
        now=NOW + timedelta(hours=3),
        fetch=endpoint,
        max_age=service.PICK_MAX_AGE_SECONDS,
        unread_keeps_place=True,
    )

    assert decision.account is not None and decision.account.slot == codex.slot, decision.notes
    assert not any("counts as full" in n for n in decision.notes)


def test_a_codex_reading_under_the_line_is_still_a_measured_one_whatever_its_age(
    fake_home: Path, both_installed: None
) -> None:
    """An offline agent has no live figure to wait for: its newest record is its reading, and a
    hand-over can move onto an account whose record says it has room."""
    claude = _slot("work@example.com", "tok-work")
    codex = _codex_account(30.0, recorded_ago=timedelta(hours=3), resets_in=timedelta(hours=4))
    endpoint = _Endpoint({"tok-work": _payload(96.0, resets_at=NOW + timedelta(hours=9))})

    decision = service.headroom_decision(
        [claude, codex],
        switch_at=LINE,
        now=NOW + timedelta(hours=3),
        fetch=endpoint,
        max_age=None,
    )

    assert decision.account is not None and decision.account.slot == codex.slot, decision.notes
    assert decision.measured
