"""R16-A (owner, 2026-11-13): a Claude Max plan with a WEEKLY limit alone is a usable account.

"claude code max account has gotten rid of daily usage and only has a weekly limit now, so
that kinda breaks our auto account management, fix that in UI too". The owner's 0.9.2 store
holds a week of such readings for a max 20x slot: every row ``session_percent`` NULL and
``session_resets_at`` NULL, with the week read (2.0, a family Fable week at 0.0). The endpoint
answered without its five-hour window, the parser kept that window as one unread, and a
reading whose shortest window went unread ranks nowhere, so the account with the MOST room was
left out of every pick, the ``N usable`` count, the fleet's week, the sidebar's ``#slot·%`` and
the doctor's headroom, and its session cell read ``—``.

A plan's readings are now the windows it HAS (``models.session_and_week``), as a Codex business
plan's week alone always was: a weekly-only reading ranks, counts and shows by its week, and its
session cell says ``no 5h limit`` (``NO_SESSION_LIMIT``). A reading that failed, or an answer
with neither window, is unread exactly as before; a 5x plan's five hours (``resets_at`` null
while idle) are unchanged. The stored rows cannot tell ``five_hour: null`` from a missing key or
an all-null window (each parses to nothing), so every one of the three is pinned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import accounts as accounts_cli
from aisquare.cli.app import app
from aisquare.cli.ui import attach
from aisquare.cli.ui.views.accounts import PROBLEM_MARK, account_cells
from aisquare.core import claude_accounts as core
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import (
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeUsage,
    ProjectInfo,
    UsageSample,
)
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from tests.test_claude_accounts import LIVE_USAGE_FULL, NOW, _Fetch, _sign_in
from tests.test_usage_aware_accounts import _settings, _slot, _Usage

NO_5H = service.NO_SESSION_LIMIT
WEEK_RESETS = NOW + timedelta(days=4)


def _week_alone(week: float, *, shape: str = "null", fable: float = 0.0) -> dict[str, Any]:
    """The answer of a Max plan with a weekly limit alone, in the W0 shape the endpoint sends
    (``LIVE_USAGE_FULL``): its week at ``week`` % used, a Fable week at ``fable``, and its
    five-hour window ``shape``: ``null`` (the per-model windows' way of saying none), left
    ``missing``, or an ``empty`` window whose every field is null."""
    payload: dict[str, Any] = json.loads(json.dumps(LIVE_USAGE_FULL))
    payload["seven_day"]["utilization"] = week
    payload["seven_day"]["resets_at"] = WEEK_RESETS.isoformat()
    if shape == "null":
        payload["five_hour"] = None
    elif shape == "missing":
        del payload["five_hour"]
    else:
        payload["five_hour"] = {key: None for key in payload["five_hour"]}
    payload["limits"] = [
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": week,
            "severity": "normal",
            "resets_at": WEEK_RESETS.isoformat(),
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": fable,
            "severity": "normal",
            "resets_at": WEEK_RESETS.isoformat(),
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ]
    return payload


def _five_and_week(session: float, week: float, *, session_resets: bool = True) -> dict[str, Any]:
    """A 5x plan's answer: both windows; ``session_resets=False`` is an idle five hours, whose
    ``resets_at`` the endpoint sends null (the owner's slots 2 and 3 read 0.0 with none)."""
    payload: dict[str, Any] = json.loads(json.dumps(LIVE_USAGE_FULL))
    payload["five_hour"]["utilization"] = session
    payload["five_hour"]["resets_at"] = (
        (NOW + timedelta(hours=3)).isoformat() if session_resets else None
    )
    payload["seven_day"]["utilization"] = week
    payload["seven_day"]["resets_at"] = WEEK_RESETS.isoformat()
    payload["limits"] = []
    return payload


def _status(account: ClaudeAccount, usage: ClaudeUsage | None) -> ClaudeAccountStatus:
    return ClaudeAccountStatus(
        account=account,
        label=core.label(account),
        identity=ClaudeIdentity(email=f"slot{account.slot}@example.com"),
        signed_in=True,
        token_state="ok",
        subscription="max 20x",
        usage=usage,
    )


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    project = team_project(root)
    with store_session() as store:
        store.onboard_project(project)
    return project


# --------------------------------------------------------------------------- the reading


@pytest.mark.parametrize("shape", ["null", "missing", "empty"])
def test_a_weekly_only_answer_is_its_week_alone_and_ranks_by_it(
    fake_home: Path, shape: str
) -> None:
    account = core.create_account()
    _sign_in(account, "max@example.com")

    usage = service.usage(account, now=NOW, fetch=_Fetch(body=_week_alone(2.0, shape=shape)))

    assert usage.available and usage.reason is None and usage.cause is None
    assert [window.name for window in usage.windows] == ["week"]
    assert (usage.session_percent, usage.session_resets_at) == (None, None)
    assert (usage.week_percent, usage.week_resets_at) == (2.0, WEEK_RESETS)
    assert usage.family_week_percent == {"fable": 0.0}
    assert usage.weekly_only
    assert usage.limiting is not None and usage.limiting.name == "week"
    assert usage.ranked_percent == 2.0
    assert service.headroom_percent(usage) == service.headroom_percent(usage, "fable") == 2.0
    assert service.percent_left(usage) == 98.0
    assert service.describe_headroom(account, usage) == "account 2 week 2% used (98% left)"
    # No --json key is added: the reading's keys are a 5x reading's, ``windows`` and
    # ``limiting`` saying it has a week alone.
    five = service.usage(account, now=NOW, fetch=_Fetch(body=_five_and_week(13, 18)))
    dumped = usage.model_dump(mode="json")
    assert sorted(dumped) == sorted(five.model_dump(mode="json"))
    assert "weekly_only" not in dumped
    assert [w["name"] for w in dumped["windows"]] == ["week"]
    assert dumped["limiting"]["name"] == "week" and dumped["session_used_percent"] is None


def test_a_5x_plans_idle_five_hours_with_no_reset_are_still_its_five_hours(
    fake_home: Path,
) -> None:
    """The owner's slots 2 and 3: ``five_hour`` at 0.0 with ``resets_at`` null. A window it
    has, read at 0 %: never ``no 5h limit``, ranked by the fuller of its two windows."""
    account = core.create_account()
    _sign_in(account, "five@example.com")

    usage = service.usage(
        account, now=NOW, fetch=_Fetch(body=_five_and_week(0.0, 22, session_resets=False))
    )

    assert [window.name for window in usage.windows] == ["session", "week"]
    assert (usage.session_percent, usage.session_resets_at) == (0.0, None)
    assert not usage.weekly_only and usage.ranked_percent == 22.0
    assert accounts_cli._usage_cells(usage, now=NOW)[0] == "0%"
    assert account_cells(_status(account, usage), usage, now=NOW)["session"].plain == "▯▯▯▯▯ 0%"


@pytest.mark.parametrize(
    "fetch",
    [
        _Fetch(body={"five_hour": None, "seven_day": None, "limits": []}),
        _Fetch(body={"seven_day": {"utilization": None, "resets_at": None}}),
        _Fetch(status=503, body=b"down"),
        _Fetch(status=401, body={"error": "x"}),
        _Fetch(error=OSError("no route")),
    ],
    ids=["both-null", "week-empty", "503", "401", "offline"],
)
def test_a_failed_read_or_an_answer_with_neither_window_is_still_no_reading(
    fake_home: Path, fetch: _Fetch
) -> None:
    """A reading that failed has no windows, so it is never taken for a plan with a week
    alone: unread, ranked nowhere, its session cell the reason (``!`` on the page)."""
    account = core.create_account()
    _sign_in(account, "max@example.com")

    usage = service.usage(account, now=NOW, fetch=fetch)

    assert not usage.available and usage.windows == ()
    assert not usage.weekly_only
    assert usage.ranked_percent is None and service.headroom_percent(usage) is None
    session, week = accounts_cli._usage_cells(usage, now=NOW)
    assert session != NO_5H and NO_5H not in session and week == ""
    cells = account_cells(_status(account, usage), usage, now=NOW)
    assert cells["session"].plain == PROBLEM_MARK
    summary = service.fleet_headroom([account], {account.slot: usage})
    assert (summary.usable, summary.limit_by_slot, summary.percent_left) == (0, {}, None)
    assert summary.week_left_by_slot == {}


def test_a_five_hour_window_sent_without_its_figure_is_unread_not_absent(
    fake_home: Path,
) -> None:
    """The line between the two: a ``five_hour`` with a reset and no utilization is a window
    the plan HAS that went unread, as is a kept reading's five hours that have reset since.
    Each is skipped as 0.9.2 skipped it, and its session cell is never ``no 5h limit``."""
    account = core.create_account()
    _sign_in(account, "five@example.com")
    payload = _five_and_week(0, 18)
    payload["five_hour"]["utilization"] = None

    unread = service.usage(account, now=NOW, fetch=_Fetch(body=payload))
    reset = ClaudeUsage(
        available=True,
        session_resets_at=NOW - timedelta(minutes=45),
        week_percent=18.0,
        week_resets_at=WEEK_RESETS,
        fetched_at=NOW - timedelta(hours=1),
        last_failed_reason="rate-limited until 14:32",
    )

    for usage in (unread, reset):
        assert [window.name for window in usage.windows] == ["session", "week"]
        assert usage.available and not usage.weekly_only
        assert usage.ranked_percent is None and service.headroom_percent(usage) is None
        assert accounts_cli._usage_cells(usage, now=NOW)[0] != NO_5H
        cells = account_cells(_status(account, usage), usage, now=NOW)
        assert NO_5H not in cells["session"].plain
        assert service.fleet_headroom([account], {account.slot: usage}).usable == 0
    assert accounts_cli._usage_cells(unread, now=NOW)[0].startswith("—")


def test_a_0_9_2_row_of_a_weekly_only_account_is_served_as_its_week(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.9.2 kept no ``windows`` (NULL in the owner's v43 store): the five-minute cache rebuilt
    such a row with an unread five-hour window, and skipped the account until a fresh read.
    Its session NULL and its week read are a week alone now, and asked nothing."""
    account = _slot("max@example.com", "tok-max")
    token = service._token_fingerprint(account)
    with store_session() as store:
        store.add_usage_sample(
            UsageSample(
                slot=account.slot,
                fetched_at=NOW - timedelta(minutes=1),
                week_percent=2.0,
                week_resets_at=WEEK_RESETS,
                token_fingerprint=token,
            )
        )
    fetch = _Usage({})
    monkeypatch.setattr(service, "_http_get", fetch)

    served = service.cached_usage([account], now=NOW)[account.slot]

    assert fetch.calls == [], "served from the row"
    assert served.available and served.weekly_only
    assert [window.name for window in served.windows] == ["week"]
    assert served.ranked_percent == 2.0


# --------------------------------------------------------------------------- the pick


def test_the_pick_takes_a_weekly_only_account_first_in_the_order(fake_home: Path) -> None:
    """The owner's ladder with the max 20x slot (week 2 %) arranged first: it was skipped as
    unread and slot 3, a 5x at 13 %/18 %, was taken; it is taken now, its note naming its
    week. Behind a 5x under the line it waits its turn: priority still decides."""
    weekly = _slot("max@example.com", "tok-max")  # slot 2
    five = _slot("five@example.com", "tok-five")  # slot 3
    fetch = _Usage({"tok-max": _week_alone(2.0), "tok-five": _five_and_week(13, 18)})
    service.reorder([str(weekly.slot), str(five.slot)])

    picked, notes = service.headroom_choice(service.list_accounts(), switch_at=85, fetch=fetch)

    assert picked is not None and picked.slot == weekly.slot, notes
    assert any(
        "account 2 week 2% used (98% left)" in note
        and "account 3 13% used (week 18% used, 82% left)" in note
        and "account 2 is first under the 85%-used line" in note
        for note in notes
    ), notes
    assert not any("skipped" in note for note in notes), notes

    service.reorder([str(five.slot), str(weekly.slot)])
    behind, _ = service.headroom_choice(service.list_accounts(), switch_at=85, fetch=fetch)
    assert behind is not None and behind.slot == five.slot


@pytest.mark.parametrize(
    ("weekly", "session", "week", "expected"),
    [
        (2.0, 13.0, 18.0, "weekly"),  # the owner's: 98 % left beats 82 %
        (30.0, 5.0, 50.0, "weekly"),  # 70 % left beats 50 %, though the five hours read 5 %
        (60.0, 10.0, 20.0, "five"),  # 80 % left beats 40 %: no five hours is not 0 % used
    ],
)
def test_every_account_over_the_line_is_ranked_by_what_is_left_of_its_limiting_window(
    fake_home: Path, weekly: float, session: float, week: float, expected: str
) -> None:
    """With every account over the line (a spawn's least bad answer), a weekly-only account
    and a five-hour+week one are compared by what is LEFT of the window that limits each:
    the week alone for one, the fuller of the two for the other; never by the five hours."""
    five = _slot("five@example.com", "tok-five")  # slot 2, first in the order
    weekly_slot = _slot("max@example.com", "tok-max")  # slot 3
    fetch = _Usage({"tok-max": _week_alone(weekly), "tok-five": _five_and_week(session, week)})

    picked, notes = service.headroom_choice(service.list_accounts(), switch_at=1, fetch=fetch)

    want = weekly_slot if expected == "weekly" else five
    left = 100 - (weekly if expected == "weekly" else max(session, week))
    assert picked is not None and picked.slot == want.slot, notes
    assert any(
        f"account {want.slot} has the most room ({left:.0f}% left)" in note for note in notes
    ), notes


def test_a_failed_read_of_the_first_account_still_leaves_it_out(fake_home: Path) -> None:
    """The control: a weekly-only account whose read FAILED is skipped and uncounted, as
    0.9.2 skipped it; the next account is taken."""
    weekly = _slot("max@example.com", "tok-max")
    five = _slot("five@example.com", "tok-five")
    fetch = _Usage({"tok-max": 503, "tok-five": _five_and_week(13, 18)})

    picked, notes = service.headroom_choice(service.list_accounts(), switch_at=85, fetch=fetch)

    assert picked is not None and picked.slot == five.slot
    assert any(f"account {weekly.slot} skipped" in n and "HTTP 503" in n for n in notes), notes
    readings = service.read_usage(service.list_accounts(), now=NOW, fetch=fetch)
    summary = service.fleet_headroom(service.list_accounts(), readings)
    assert summary.usable == 1 and set(summary.limit_by_slot) == {five.slot}


def test_a_spawn_and_the_automatic_hand_over_take_the_weekly_only_account(
    fake_home: Path, work: ProjectInfo
) -> None:
    leaving = _slot("left@example.com", "tok-left")  # slot 2: at its five-hour limit
    five = _slot("five@example.com", "tok-five")  # slot 3: a 5x at 90 % of its week
    weekly = _slot("max@example.com", "tok-max")  # slot 4: the max 20x, week 2 %
    fetch = _Usage(
        {
            "tok-left": _five_and_week(100, 40),
            "tok-five": _five_and_week(10, 90),
            "tok-max": _week_alone(2.0),
        }
    )
    _settings(switch_at=85)

    handed = service.choose_for_handover(
        role="coder", project=work, exclude=(leaving.slot,), automatic=True, fetch=fetch
    )
    assert handed.account is not None and handed.account.slot == weekly.slot, handed.notes

    service.reorder([str(weekly.slot), str(leaving.slot), str(five.slot)])
    spawned = service.choose(role="coder", project=work, fetch=fetch)
    assert spawned.source == "headroom" and spawned.account is not None
    assert spawned.account.slot == weekly.slot, spawned.notes


# --------------------------------------------------------------------------- what is shown


def _ladder(monkeypatch: pytest.MonkeyPatch) -> dict[str, ClaudeAccount]:
    """The owner's four slots as read on 2026-11-13: 13 %/18 %, two idle 5x and the max."""
    slots = {
        "one": _slot("one@example.com", "tok-one"),
        "two": _slot("two@example.com", "tok-two"),
        "three": _slot("three@example.com", "tok-three"),
        "max": _slot("max@example.com", "tok-max"),
    }
    fetch = _Usage(
        {
            "tok-one": _five_and_week(13, 18),
            "tok-two": _five_and_week(0, 2, session_resets=False),
            "tok-three": _five_and_week(1, 22),
            "tok-max": _week_alone(2.0),
        }
    )
    monkeypatch.setattr(service, "_http_get", fetch)
    return slots


def test_accounts_usage_counts_and_shows_the_weekly_only_account(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's ``accounts usage`` said ``3 usable`` and ``week: 2.6 of 3 accounts left``
    with the max slot's session ``—``. Text and ``--json`` count it, by its week."""
    slots = _ladder(monkeypatch)
    monkeypatch.setenv("COLUMNS", "200")
    wk = slots["max"].slot

    text = runner.invoke(app, ["accounts", "usage"])

    assert text.exit_code == 0, text.output
    row = next(line for line in text.stdout.splitlines() if "max@example.com" in line)
    assert NO_5H in row and "2%" in row and "—" not in row, row
    assert "4 usable" in text.stdout, text.stdout
    assert "week: 3.6 of 4 accounts left" in text.stdout, text.stdout
    others = [line for line in text.stdout.splitlines() if "@example.com" in line and line != row]
    assert len(others) == 3 and not any(NO_5H in line for line in others), others

    raw = runner.invoke(app, ["--json", "accounts", "usage", "--summary"])
    assert raw.exit_code == 0, raw.output
    answer = json.loads(raw.stdout)
    usage = next(s["usage"] for s in answer["accounts"] if s["account"]["slot"] == wk)
    assert usage["session_percent"] is None and usage["week_percent"] == 2.0
    assert [w["name"] for w in usage["windows"]] == ["week"]
    assert usage["limiting"]["name"] == "week" and "weekly_only" not in usage
    summary = answer["summary"]
    assert summary["usable"] == 4 and summary["limit_by_slot"][str(wk)] == 2.0
    assert summary["left_by_slot"][str(wk)] == 98.0 and str(wk) in summary["week_left_by_slot"]
    assert str(wk) not in summary["session_by_slot"], "0.9.2's key: the five hours only"

    listing = runner.invoke(app, ["accounts", "list", "--usage"])
    assert listing.exit_code == 0, listing.output
    listed = next(line for line in listing.stdout.splitlines() if "max@example.com" in line)
    assert NO_5H in listed, listed


def test_the_page_the_info_line_the_sidebar_and_doctor_show_the_weekly_only_account(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = _ladder(monkeypatch)
    accounts = service.list_accounts()
    readings = service.read_usage(accounts, now=NOW)
    weekly = slots["max"]
    usage = readings[weekly.slot]

    cells = {k: t.plain for k, t in account_cells(_status(weekly, usage), usage, now=NOW).items()}
    assert cells["session"] == NO_5H and cells["session-resets"] == ""
    assert cells["week"] == "▯▯▯▯▯ 2%" and cells["week-resets"] == "in 4d"
    assert len(NO_5H) <= 16, "the session column's cells"
    assert attach.account_text(_status(weekly, usage)).endswith(" · week 2%")

    summary = service.fleet_headroom(accounts, readings)
    assert summary.usable == 4 and summary.limit_by_slot[weekly.slot] == 2.0  # #slot·2%
    assert summary.weeks_left == pytest.approx(3.56)

    check = diagnostics._claude_account_headroom_check()
    assert check is not None and check.status.value == "ok", check
    assert "account 5 week 2% used (98% left)" in check.detail, check.detail
    assert "unreadable" not in check.detail, check.detail


def test_a_kept_weekly_only_reading_says_its_age_after_no_5h_limit(fake_home: Path) -> None:
    """The marks a session cell carries for a reading that is not live stay: a kept one's
    age, in the cell's yellow, after the words; the reset cell stays empty."""
    account = ClaudeAccount(slot=4, config_dir=Path("/h/4"), managed=True)
    kept = ClaudeUsage(
        available=True,
        week_percent=2.0,
        week_resets_at=WEEK_RESETS,
        fetched_at=NOW - timedelta(minutes=12),
        last_failed_reason="rate-limited until 14:32",
    )
    cells = account_cells(_status(account, kept), kept, now=NOW)
    assert cells["session"].plain == f"{NO_5H} · 12m"
    assert cells["session-resets"].plain == ""


# --------------------------------------------------------------------------- Codex's twin


def test_a_codex_weekly_only_plan_gets_the_same_answer(fake_home: Path) -> None:
    """The shared path, not Claude's alone: a Codex business plan records its week alone, and
    it reads, ranks, counts and shows exactly as a Max plan's week alone does."""
    from tests.test_codex_accounts import _codex_slot, _rollout, _token_count
    from tests.test_codex_accounts import _window as codex_window

    week = 7 * 24 * 60
    recorded = datetime.now(tz=UTC)
    resets = recorded + timedelta(days=2)
    alone = _codex_slot("business@example.com")
    _rollout(alone.config_dir, [_token_count(recorded, codex_window(30, week, resets))])
    both = _codex_slot("plus@example.com")
    _rollout(
        both.config_dir,
        [
            _token_count(
                recorded,
                codex_window(5, 300, recorded + timedelta(hours=2)),
                codex_window(50, week, resets),
            )
        ],
    )
    readings = {a.slot: service.usage(a, now=recorded) for a in (alone, both)}
    weekly = readings[alone.slot]

    assert weekly.weekly_only and not readings[both.slot].weekly_only
    assert [w.name for w in weekly.windows] == ["week"] and weekly.ranked_percent == 30.0
    assert service.percent_left(weekly) == 70.0 and service.percent_left(readings[both.slot]) == 50
    assert accounts_cli._usage_cells(weekly, now=recorded)[0] == NO_5H
    page = account_cells(_status(alone, weekly), weekly, now=recorded)["session"].plain
    assert page.startswith(NO_5H), page
    summary = service.fleet_headroom([alone, both], readings)
    assert summary.usable == 2 and summary.left_by_slot == {alone.slot: 70.0, both.slot: 50.0}

    picked, notes = service.headroom_choice([both, alone], switch_at=1, now=recorded)
    assert picked is not None and picked.slot == alone.slot, notes
