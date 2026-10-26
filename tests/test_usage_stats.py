"""Local usage stats: the sweep that records every Claude Code session on the machine, and the
ranges, rollups, accounts and repositories read from what it and the hooks recorded.

The hooks' half is ``tests/test_metrics_usage.py``; this is everything the dashboard adds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import feature_use, paths, repo_profile
from aisquare.core import usage as usage_core
from aisquare.core.store import store_session
from aisquare.models import SessionUsage, TurnUsage, UsageStats
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from aisquare.services import usage_stats
from tests.projects import add_project
from tests.usage_stats_support import (
    IST,
    MODEL,
    NOW,
    Machine,
    prompt,
    request,
    stamp,
    tokens,
    tool_result,
    write,
)

SESSION = "11111111-2222-3333-4444-555555555555"


def _day(offset: int, hour: int = 10, minute: int = 0):  # type: ignore[no-untyped-def]
    """``hour:minute`` IST, ``offset`` days from the stats clock's day (negative: before)."""
    base = NOW.replace(hour=hour, minute=minute)
    return base + timedelta(days=offset)


def _one_turn(
    m: Machine,
    repo: Path,
    session_id: str,
    when: datetime,
    *,
    model: str = MODEL,
    input: int = 10,
    cache_write: int = 100,
    cache_read: int = 1000,
    output: int = 20,
) -> Path:
    """A session with a prompt and one request at ``when``: ``tokens(...)`` in one turn."""
    return m.session(
        1,
        repo,
        session_id,
        prompt(when - timedelta(seconds=5), cwd=repo),
        request(
            when,
            f"msg_{session_id}",
            cwd=repo,
            model=model,
            input=input,
            cache_write=cache_write,
            cache_read=cache_read,
            output=output,
        ),
    )


def _read(range_: str = "7d", *, store=None) -> UsageStats:  # type: ignore[no-untyped-def]
    return usage_stats.stats(range_, now=NOW, tz=IST, store=store)  # type: ignore[arg-type]


# --- the scanner: turns cut at prompts and at local midnight ----------


def _day_of(stamp_text: str) -> str:
    return usage_stats._day_key(stamp_text, IST)


def test_scan_turns_sums_to_what_scan_reads_and_cuts_at_each_prompt(tmp_path: Path) -> None:
    cwd = tmp_path
    transcript = write(
        tmp_path / "s.jsonl",
        prompt(_day(0, 9), cwd=cwd),
        request(_day(0, 9, 1), "m1", cwd=cwd, tool="Read"),
        tool_result(_day(0, 9, 2), cwd=cwd),  # not a prompt: the turn goes on
        request(_day(0, 9, 3), "m2", cwd=cwd, cache_read=2000),
        prompt(_day(0, 9, 30), cwd=cwd),
        request(_day(0, 9, 31), "m3", cwd=cwd, tool="Edit"),
    )
    scanned, _ = usage_core.scan_turns(transcript, day_of=_day_of)
    whole, _ = usage_core.scan(transcript)

    assert [chunk.usage.requests for chunk in scanned.chunks] == [2, 1]
    assert scanned.total().to_dict() == whole.to_dict()
    assert scanned.chunks[0].ended_at == stamp(_day(0, 9, 3))


@pytest.mark.parametrize(
    "not_a_prompt",
    [
        {"isSidechain": True},
        {"isMeta": True},
        {"isCompactSummary": True},
        {"toolUseResult": "x"},
    ],
)
def test_only_a_typed_message_opens_a_turn(tmp_path: Path, not_a_prompt: dict[str, object]) -> None:
    other = {**prompt(_day(0, 9, 5), cwd=tmp_path), **not_a_prompt}
    transcript = write(
        tmp_path / "s.jsonl",
        prompt(_day(0, 9), cwd=tmp_path),
        request(_day(0, 9, 1), "m1", cwd=tmp_path),
        other,
        request(_day(0, 9, 6), "m2", cwd=tmp_path),
    )
    scanned, _ = usage_core.scan_turns(transcript)
    assert len(scanned.chunks) == 1


def test_a_turn_across_local_midnight_is_two_turns_each_in_its_own_day(tmp_path: Path) -> None:
    before, after = _day(-1, 23, 58), _day(0, 0, 3)  # IST: either side of local midnight
    transcript = write(
        tmp_path / "s.jsonl",
        prompt(_day(-1, 23, 50), cwd=tmp_path),
        request(before, "m1", cwd=tmp_path),
        request(after, "m2", cwd=tmp_path, cache_read=3000),
    )
    scanned, _ = usage_core.scan_turns(transcript, day_of=_day_of)
    whole, _ = usage_core.scan(transcript)

    assert [(_day_of(c.ended_at), c.usage.totals().total) for c in scanned.chunks] == [
        ("2026-10-25", tokens()),
        ("2026-10-26", tokens(cache_read=3000)),
    ]
    assert scanned.total().to_dict() == whole.to_dict()


def test_a_second_scan_from_the_state_finds_nothing_and_a_longer_file_only_the_rest(
    tmp_path: Path,
) -> None:
    transcript = write(
        tmp_path / "s.jsonl",
        prompt(_day(0, 9), cwd=tmp_path),
        request(_day(0, 9, 1), "m1", cwd=tmp_path),
    )
    first, state = usage_core.scan_turns(transcript)
    again, state = usage_core.scan_turns(transcript, state)
    write(
        transcript, prompt(_day(0, 10), cwd=tmp_path), request(_day(0, 10, 1), "m2", cwd=tmp_path)
    )
    more, _ = usage_core.scan_turns(transcript, state)

    assert (len(first.chunks), len(again.chunks), len(more.chunks)) == (1, 0, 1)
    assert more.total().totals().total == tokens()


# --- range bucketing, in local days ----------


def _five_ranges_of_history(m: Machine) -> Path:
    repo = m.repo("app")
    when = {
        "t1": _day(0, 0, 5),  # 00:05 IST today: still "yesterday" in UTC
        "t2": _day(-1, 23, 55),  # 23:55 IST yesterday
        "t3": _day(-6, 10),  # the first day of the 7d window
        "t4": _day(-7, 10),  # one day before it
        "t5": _day(-29, 10),  # the first day of the 30d window
        "t6": _day(-30, 10),  # one day before that
    }
    for index, (name, moment) in enumerate(when.items()):
        _one_turn(m, repo, f"s-{name}", moment, cache_read=1000 * 2**index)
    return repo


def _turns_of(stats: UsageStats) -> int:
    return stats.turns


def test_each_range_holds_the_local_days_it_names_and_no_others(machine: Machine) -> None:
    _five_ranges_of_history(machine)
    usage_stats.sweep(tz=IST)

    got = {name: _read(name) for name in ("today", "7d", "30d", "lifetime")}

    cache_read = {name: stats.tokens.cache_read for name, stats in got.items()}
    assert cache_read == {
        "today": 1000,  # t1
        "7d": 1000 + 2000 + 4000,  # t1 t2 t3
        "30d": 1000 + 2000 + 4000 + 8000 + 16000,  # + t4 t5
        "lifetime": 1000 + 2000 + 4000 + 8000 + 16000 + 32000,
    }
    assert [len(got[name].days) for name in ("today", "7d", "30d", "lifetime")] == [1, 7, 30, 31]
    assert got["today"].since == got["today"].until == "2026-10-26"
    assert (got["7d"].since, got["30d"].since) == ("2026-10-20", "2026-09-27")
    assert got["lifetime"].since == "2026-09-26"
    assert got["lifetime"].turns == 6 and got["lifetime"].sessions == 6
    assert got["lifetime"].active_days == 6


def test_a_session_across_local_midnight_is_in_both_days_and_counted_once(
    machine: Machine,
) -> None:
    repo = machine.repo("night")
    machine.session(
        1,
        repo,
        SESSION,
        prompt(_day(-1, 23, 50), cwd=repo),
        request(_day(-1, 23, 58), "m1", cwd=repo),
        request(_day(0, 0, 3), "m2", cwd=repo, cache_read=3000),
    )
    usage_stats.sweep(tz=IST)

    lifetime = _read("lifetime")
    today = _read("today")
    by_day = {day.date: day for day in lifetime.days}

    assert by_day["2026-10-25"].tokens.total == tokens()
    assert by_day["2026-10-26"].tokens.total == tokens(cache_read=3000)
    assert (by_day["2026-10-25"].sessions, by_day["2026-10-26"].sessions) == (1, 1)
    assert lifetime.sessions == 1  # one session, not one per day
    assert today.tokens.total == tokens(cache_read=3000) and today.sessions == 1
    assert lifetime.tokens.total == tokens() + tokens(cache_read=3000)


def test_weekdays_and_hours_are_counted_on_the_local_clock(machine: Machine) -> None:
    repo = machine.repo("clock")
    _one_turn(machine, repo, "a", _day(0, 0, 30))  # Monday 00:30 IST (Sunday evening UTC)
    _one_turn(machine, repo, "b", _day(-2, 15, 10))  # Saturday 15:10 IST
    usage_stats.sweep(tz=IST)

    stats = _read("7d")

    assert stats.by_weekday == [1, 0, 0, 0, 0, 1, 0]
    assert stats.by_hour[0] == 1 and stats.by_hour[15] == 1 and sum(stats.by_hour) == 2


def test_the_busiest_days_are_the_five_with_the_most_tokens(machine: Machine) -> None:
    repo = machine.repo("busy")
    for offset in range(7):  # day -k has k+1 thousand cache-read tokens
        _one_turn(machine, repo, f"s{offset}", _day(-offset, 10), cache_read=1000 * (offset + 1))
    usage_stats.sweep(tz=IST)

    stats = _read("7d")

    assert [day.date for day in stats.busiest_days] == [
        "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24",
    ]  # fmt: skip
    assert stats.busiest_days[0].tokens.cache_read == 7000


def test_input_and_input_plus_output_are_reported_beside_the_total(machine: Machine) -> None:
    repo = machine.repo("figures")
    _one_turn(machine, repo, "a", _day(0, 9), input=7, output=30, cache_write=5, cache_read=11)
    usage_stats.sweep(tz=IST)

    figures = _read("today").tokens

    assert (figures.input, figures.output, figures.input_output) == (7, 30, 37)
    assert figures.total == 7 + 30 + 5 + 11
    assert figures.model_dump()["total"] == figures.total


def test_nothing_recorded_is_zeros_and_no_first_day(machine: Machine) -> None:
    stats = _read("lifetime")

    assert (stats.turns, stats.sessions, stats.tokens.total) == (0, 0, 0)
    assert stats.since is None and stats.days == [] and stats.repos.rows == []
    assert [day.date for day in _read("7d").days][-1] == "2026-10-26"
    with pytest.raises(ValueError, match="unknown range"):
        usage_stats.stats("yesterday")  # type: ignore[arg-type]


# --- the same sessions, however they were read ----------


def test_the_totals_are_those_of_metrics_tokens_for_the_same_sessions(
    machine: Machine,
    tmp_path: Path,
) -> None:
    add_project(machine.repo("hooked"))
    hooked = machine.root / "repos" / "hooked"
    swept = machine.repo("swept")
    _one_turn(machine, hooked, "h1", _day(-1, 10), cache_read=4000)
    hooks_service.turn_stopped(
        hooked, session_id="h1", transcript_path=str(machine.transcript(1, hooked, "h1"))
    )
    _one_turn(machine, swept, "s1", _day(-3, 10), cache_read=2000, model="claude-sonnet-5-5")
    machine.session(
        2,
        swept,
        "s2",
        prompt(_day(0, 9), cwd=swept),
        request(_day(0, 9, 1), "a", cwd=swept, tool="Edit"),
        prompt(_day(0, 9, 30), cwd=swept),
        request(_day(0, 9, 31), "b", cwd=swept, output=500),
    )
    usage_stats.sweep(tz=IST)

    summary = metrics_service.summarize_tokens(metrics_service.session_usages())
    stats = _read("lifetime")

    assert stats.tokens.model_dump(exclude={"input_output"}) == {
        "input": summary.input_tokens,
        "cache_write": summary.cache_write_tokens,
        "cache_read": summary.cache_read_tokens,
        "output": summary.output_tokens,
        "total": summary.total_tokens,
    }
    assert (stats.sessions, stats.turns) == (summary.sessions, summary.turns) == (3, 4)
    assert stats.cost_usd == pytest.approx(summary.cost_usd)
    assert {row.model: row.tokens.total for row in stats.models} == {
        name: values["input"] + values["cache_write_5m"] + values["cache_write_1h"]
        + values["cache_read"] + values["output"]
        for name, values in summary.models.items()
    }  # fmt: skip


# --- the sweep and the hook never count a session twice ----------


def _totals() -> tuple[int, int, int]:
    """(total tokens, sessions, turn rows) as the store holds them."""
    with store_session() as store:
        sessions = store.session_usages()
        turns = sum(len(store.turn_usages(s.session_id)) for s in sessions)
    return sum(s.total_tokens for s in sessions), len(sessions), turns


def test_a_session_the_hook_counted_is_not_counted_again_by_the_sweep(machine: Machine) -> None:
    repo = machine.repo("hooked")
    add_project(repo)
    transcript = _one_turn(machine, repo, SESSION, _day(0, 9))
    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(transcript))
    hooked = _totals()

    report = usage_stats.sweep(tz=IST)
    usage_stats.sweep(tz=IST)

    assert hooked == (tokens(), 1, 1)
    assert _totals() == hooked
    assert report.sessions_new == 0 and report.turns == 0


def test_a_session_the_sweep_counted_is_not_counted_again_by_the_hook(machine: Machine) -> None:
    repo = machine.repo("swept")
    add_project(repo)
    transcript = _one_turn(machine, repo, SESSION, _day(0, 9))
    usage_stats.sweep(tz=IST)
    swept = _totals()

    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(transcript))

    assert swept == (tokens(), 1, 1)
    assert _totals() == swept


def test_what_each_reads_after_the_other_is_added_once(machine: Machine) -> None:
    repo = machine.repo("both")
    add_project(repo)
    transcript = _one_turn(machine, repo, SESSION, _day(0, 9))
    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(transcript))
    write(transcript, prompt(_day(0, 10), cwd=repo), request(_day(0, 10, 1), "m2", cwd=repo))
    usage_stats.sweep(tz=IST)  # the sweep reads the second turn only
    write(transcript, prompt(_day(0, 11), cwd=repo), request(_day(0, 11, 1), "m3", cwd=repo))
    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(transcript))
    usage_stats.sweep(tz=IST)  # and finds the third counted
    usage_stats.sweep(tz=IST)

    with store_session() as store:
        row = store.session_usage(SESSION)
        sources = [turn.source for turn in store.turn_usages(SESSION)]
    assert row is not None and row.total_tokens == 3 * tokens() and row.turns == 3
    assert sources == [None, "sweep", None]
    assert _read("lifetime").tokens.total == 3 * tokens()


def test_a_transcript_grown_since_the_last_sweep_adds_only_what_it_grew_by(
    machine: Machine,
) -> None:
    repo = machine.repo("growing")
    transcript = _one_turn(machine, repo, SESSION, _day(0, 9))
    usage_stats.sweep(tz=IST)
    write(transcript, prompt(_day(0, 10), cwd=repo), request(_day(0, 10, 1), "m2", cwd=repo))

    report = usage_stats.sweep(tz=IST)

    assert (report.sessions_new, report.sessions_updated, report.turns) == (0, 1, 1)
    assert _totals() == (2 * tokens(), 1, 2)


def test_one_message_written_on_two_lines_is_one_request(machine: Machine) -> None:
    repo = machine.repo("streamed")
    entry = request(_day(0, 9, 1), "same", cwd=repo)
    machine.session(1, repo, SESSION, prompt(_day(0, 9), cwd=repo), entry, entry)
    usage_stats.sweep(tz=IST)

    assert _read("today").tokens.total == tokens()


def test_a_session_found_under_two_directories_is_read_once(machine: Machine) -> None:
    repo = machine.repo("shared")
    first = _one_turn(machine, repo, SESSION, _day(0, 9))
    other = machine.transcript(2, repo, SESSION)
    other.parent.mkdir(parents=True)
    other.symlink_to(first)  # a shared projects folder
    usage_stats.sweep(tz=IST)

    assert _totals() == (tokens(), 1, 1)


def test_a_second_sweep_in_the_same_minute_is_not_run_but_a_forced_one_is(
    machine: Machine,
) -> None:
    repo = machine.repo("throttled")
    _one_turn(machine, repo, "a", _day(0, 9))
    first = usage_stats.maybe_sweep(now=NOW)
    _one_turn(machine, repo, "b", _day(0, 10))
    second = usage_stats.maybe_sweep(now=NOW + timedelta(minutes=9))
    third = usage_stats.maybe_sweep(now=NOW + timedelta(minutes=11))
    forced = usage_stats.sweep(tz=IST)

    assert (first.sessions_new, second.skipped, third.sessions_new) == (1, "swept recently", 1)
    assert forced.skipped is None and forced.sessions_new == 0


def test_two_sweeps_at_once_run_one(machine: Machine) -> None:
    from aisquare.core import locking

    paths.ensure_home()

    with locking.holding(paths.usage_sweep_lock_path(), wait_s=0):
        report = usage_stats.sweep(tz=IST)
    assert report.skipped == "another sweep is running"


def test_a_transcript_that_cannot_be_read_costs_that_one_and_no_other(
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = machine.repo("mixed")
    _one_turn(machine, repo, "good", _day(0, 9))
    bad = _one_turn(machine, repo, "bad", _day(0, 10))
    real = usage_stats._sweep_one

    def broken(store, found, *args, **kwargs):  # type: ignore[no-untyped-def]
        if found.path == bad:
            raise OSError("unreadable")
        return real(store, found, *args, **kwargs)

    monkeypatch.setattr(usage_stats, "_sweep_one", broken)
    report = usage_stats.sweep(tz=IST)

    assert (report.failed, report.sessions_new) == (1, 1)
    assert _totals() == (tokens(), 1, 1)


# --- which account, which subscription ----------


def test_each_session_is_attributed_to_the_account_directory_it_lives_in(machine: Machine) -> None:
    machine.account(1, "team")
    machine.account(2, "max")
    repo = machine.repo("accounts")
    _one_turn(machine, repo, "plain", _day(0, 9))  # slot 1
    machine.session(
        2,
        repo,
        "managed",
        prompt(_day(0, 9), cwd=repo),
        request(_day(0, 9, 1), "x", cwd=repo, cache_read=5000),
    )
    usage_stats.sweep(tz=IST)

    with store_session() as store:
        rows = {s.session_id: (s.account_slot, s.account_type) for s in store.session_usages()}
    accounts = _read("today").accounts

    assert rows == {"plain": (1, "team"), "managed": (2, "max")}
    assert accounts.registered == 2 and accounts.active == 2
    assert accounts.by_type == {"max": 1, "team": 1}
    assert [(row.slot, row.subscription) for row in accounts.rows] == [(2, "max"), (1, "team")]
    assert accounts.top is not None and accounts.top.slot == 2
    assert accounts.top.label == "account 2" and accounts.rows[1].label == "plain claude"
    assert [round(row.share, 2) for row in accounts.rows] == [0.82, 0.18]


def test_a_session_the_hook_recorded_before_the_type_was_known_gets_it_from_the_sweep(
    machine: Machine,
) -> None:
    machine.account(1, "pro")
    repo = machine.repo("hooked")
    add_project(repo)
    transcript = _one_turn(machine, repo, SESSION, _day(0, 9))
    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(transcript))
    with store_session() as store:
        recorded = store.session_usage(SESSION)
    assert recorded is not None
    assert (recorded.account_slot, recorded.account_type) == (1, None)  # the hook reads no login

    usage_stats.sweep(tz=IST)

    with store_session() as store:
        row = store.session_usage(SESSION)
    assert row is not None and (row.account_slot, row.account_type) == (1, "pro")
    assert row.total_tokens == tokens()


def test_a_session_from_before_this_existed_is_attributed_from_its_transcript(
    machine: Machine,
) -> None:
    machine.account(2, "max")
    repo = machine.repo("old")
    transcript = machine.session(
        2, repo, SESSION, prompt(_day(0, 9), cwd=repo), request(_day(0, 9, 1), "m", cwd=repo)
    )
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE usage_session SET account_slot = NULL, account_type = NULL, repo_id = NULL"
        )
        store._conn.commit()  # type: ignore[attr-defined]

    report = usage_stats.sweep(tz=IST)

    with store_session() as store:
        row = store.session_usage(SESSION)
        profiles = store.repo_profiles()
    assert report.attributed == 1
    assert row is not None and (row.account_slot, row.account_type) == (2, "max")
    assert row.repo_id is not None and row.repo_id in profiles
    assert row.transcript_path == str(transcript)


def test_a_directory_no_account_names_is_other_directories_not_an_account(machine: Machine) -> None:
    repo = machine.repo("elsewhere")
    add_project(repo)
    strange = machine.root / "elsewhere-config" / "projects" / "x" / f"{SESSION}.jsonl"
    write(strange, prompt(_day(0, 9), cwd=repo), request(_day(0, 9, 1), "m", cwd=repo))
    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(strange))

    accounts = _read("lifetime").accounts

    assert usage_stats.account_slot(strange) is None
    assert [(row.slot, row.label) for row in accounts.rows] == [(None, "other directories")]
    assert accounts.active == 0 and accounts.top is None and accounts.by_type == {}


def test_an_account_with_only_an_api_key_is_api_and_one_with_nothing_is_unknown(
    machine: Machine,
) -> None:
    keyed = machine.account(2)
    (keyed / ".claude.json").write_text(
        json.dumps({"customApiKeyResponses": {"approved": ["abc"], "rejected": []}}),
        encoding="utf-8",
    )
    machine.account(3)
    with store_session() as store:
        types = usage_stats.account_types(store)

    assert types[2] == "api" and types[3] == "unknown"


# --- repositories ----------


def test_the_top_ten_repositories_are_the_ten_with_the_most_tokens_and_the_rest_are_counted(
    machine: Machine,
) -> None:
    for number in range(1, 13):  # repo-01 has the least, repo-12 the most
        repo = machine.repo(f"repo-{number:02d}")
        _one_turn(machine, repo, f"s{number}", _day(0, 9), cache_read=1000 * number)
    usage_stats.sweep(tz=IST)

    repos = _read("7d").repos

    assert repos.active == 12
    assert [row.name for row in repos.rows] == [f"repo-{n:02d}" for n in range(12, 2, -1)]
    assert repos.top is not None and repos.top.name == "repo-12"
    assert repos.rows[0].tokens.cache_read == 12000 and repos.rows[0].sessions == 1
    assert repos.rows[0].last_active == "2026-10-26"


def test_a_tie_in_tokens_is_ordered_by_name(machine: Machine) -> None:
    for name in ("zeta", "alpha", "mid"):
        _one_turn(machine, machine.repo(name), f"s-{name}", _day(0, 9))
    usage_stats.sweep(tz=IST)

    assert [row.name for row in _read("today").repos.rows] == ["alpha", "mid", "zeta"]


def test_worktrees_and_subdirectories_are_their_repository(machine: Machine) -> None:
    repo = machine.repo("main")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "x"],
        check=True,
    )  # fmt: skip
    work = repo / ".aisquare-worktrees" / "coder-x"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(work), "-b", "x"], check=True
    )
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    _one_turn(machine, repo, "a", _day(0, 9))
    _one_turn(machine, work, "b", _day(0, 10))
    _one_turn(machine, sub, "c", _day(0, 11))
    _one_turn(machine, repo / ".aisquare-worktrees" / "gone", "d", _day(0, 12))  # removed since
    usage_stats.sweep(tz=IST)

    repos = _read("today").repos

    assert repos.active == 1 and repos.rows[0].name == "main" and repos.rows[0].sessions == 4


def test_scratch_repositories_are_counted_as_one_but_their_tokens_still_count(
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = machine.root / "scratch"
    monkeypatch.setattr(usage_stats, "_ephemeral_roots", lambda: (scratch,))
    for number in range(3):
        repo = scratch / f"try-{number}"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _one_turn(machine, repo, f"t{number}", _day(0, 9))
    real = machine.repo("real")
    _one_turn(machine, real, "r", _day(0, 9))
    usage_stats.sweep(tz=IST)

    repos = _read("today").repos

    assert repos.active == 2
    assert {row.name: row.sessions for row in repos.rows} == {"real": 1, "temporary directories": 3}
    assert _read("today").tokens.total == 4 * tokens()


def test_a_repository_is_profiled_once_a_day(
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = machine.repo("profiled", remote="git@github.com:Org/Profiled.git")
    (repo / "a.py").write_text("x = 1\n")
    (repo / "b.py").write_text("y = 2\n")
    (repo / "c.md").write_text("# hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _one_turn(machine, repo, SESSION, _day(0, 9))
    calls: list[str] = []
    real = usage_stats.profile_repo

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append("git")
        return real(*args, **kwargs)

    monkeypatch.setattr(usage_stats, "profile_repo", counted)

    usage_stats.sweep(now=NOW, tz=IST)
    with store_session() as store:
        (profile,) = store.repo_profiles().values()
        # a later sweep the same day, with the repository seen again
        usage_stats.refresh_profiles(store, {profile.repo_id: repo}, now=NOW + timedelta(hours=23))
        again = len(calls)
        usage_stats.refresh_profiles(store, {profile.repo_id: repo}, now=NOW + timedelta(hours=25))

    assert profile.name == "profiled" and profile.root == str(repo.resolve())
    assert profile.remote == "github.com/org/profiled"
    assert (profile.traits.host, profile.traits.language) == ("github", "python")
    assert (profile.traits.size_bucket, profile.traits.monorepo, profile.files) == ("xs", False, 3)
    assert again == 1 and len(calls) == 2


@pytest.mark.parametrize(
    ("url", "normalised", "host"),
    [
        ("git@github.com:Org/Repo.git", "github.com/org/repo", "github"),
        ("https://user:token@github.com/Org/Repo", "github.com/org/repo", "github"),
        ("ssh://git@gitlab.example.com:2222/a/b.git", "gitlab.example.com/a/b", "gitlab"),
        ("https://bitbucket.org/a/b/", "bitbucket.org/a/b", "bitbucket"),
        ("git@git.corp.example:team/x.git", "git.corp.example/team/x", "other"),
        ("/srv/git/x.git", None, "none"),
        ("file:///srv/git/x.git", None, "none"),
        ("", None, "none"),
    ],
)
def test_a_remote_is_normalised_without_credentials_and_classified(
    url: str, normalised: str | None, host: str
) -> None:
    remote = repo_profile.normalise_remote(url)
    assert remote == normalised and repo_profile.host_of(remote) == host
    assert "token" not in (remote or "")


@pytest.mark.parametrize(
    ("files", "bucket"),
    [(0, "xs"), (50, "xs"), (51, "s"), (500, "s"), (501, "m"), (5000, "m"), (5001, "l"),
     (50000, "l"), (50001, "xl")],
)  # fmt: skip
def test_size_buckets(files: int, bucket: str) -> None:
    assert repo_profile.size_bucket(files) == bucket


def test_monorepo_means_a_workspace_file_or_two_packages_below_the_root() -> None:
    assert repo_profile.is_monorepo(["pnpm-workspace.yaml", "x.ts"])
    assert repo_profile.is_monorepo(["packages/a/package.json", "packages/b/package.json"])
    assert repo_profile.is_monorepo(["svc/api/pyproject.toml", "svc/web/package.json"])
    assert not repo_profile.is_monorepo(["package.json", "src/index.ts"])
    assert not repo_profile.is_monorepo(["pyproject.toml", "docs/conf/package.json"])
    assert not repo_profile.is_monorepo(
        ["node_modules/a/package.json", "node_modules/b/package.json"]
    )


# --- the models and the rollups the sharing path builds on ----------


def test_the_models_are_split_with_their_share_and_cost(machine: Machine) -> None:
    repo = machine.repo("models")
    machine.session(
        1,
        repo,
        SESSION,
        prompt(_day(0, 9), cwd=repo),
        request(_day(0, 9, 1), "a", cwd=repo),
        request(_day(0, 9, 2), "b", cwd=repo, model="claude-sonnet-5-5", cache_read=0),
        request(_day(0, 9, 3), "c", cwd=repo, model="claude-nonesuch-9"),
    )
    usage_stats.sweep(tz=IST)

    stats = _read("today")

    assert [row.model for row in stats.models] == [
        "claude-nonesuch-9", "claude-opus-5-5", "claude-sonnet-5-5",
    ]  # fmt: skip
    assert stats.unpriced_models == ["claude-nonesuch-9"]
    assert sum(row.share for row in stats.models) == pytest.approx(1.0, abs=0.001)
    assert next(row for row in stats.models if row.model == "claude-nonesuch-9").priced is False
    assert stats.cost_usd == pytest.approx(sum(row.cost_usd for row in stats.models))


def test_daily_rollups_are_one_record_per_active_local_day_in_the_contracts_shape(
    machine: Machine,
) -> None:
    machine.account(1, "team")
    machine.account(2, "max")
    alpha, beta = machine.repo("alpha", remote="https://github.com/o/alpha"), machine.repo("beta")
    _one_turn(machine, alpha, "a", _day(-1, 23, 55), cache_read=3000)
    machine.session(
        2, beta, "b", prompt(_day(0, 0, 4), cwd=beta), request(_day(0, 0, 5), "x", cwd=beta)
    )
    _one_turn(machine, alpha, "c", _day(0, 9))
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        store.add_feature_uses({("2026-10-26", "cli.fleet.spawn"): 3})

    days = usage_stats.daily_rollups(date(2026, 10, 20), date(2026, 10, 26), now=NOW, tz=IST)
    by_date = {day.date: day for day in days}
    today = by_date["2026-10-26"].model_dump(mode="json")

    assert list(by_date) == ["2026-10-25", "2026-10-26"]
    assert set(today) == {
        "date", "tz_offset_minutes", "active", "tokens", "cost_usd_list", "sessions", "turns",
        "models", "accounts", "repos_active", "repos", "features", "fleet",
    }  # fmt: skip
    assert today["tz_offset_minutes"] == 330 and today["active"] is True
    assert set(today["tokens"]) == {"input", "cache_write", "cache_read", "output", "total"}
    assert today["tokens"]["total"] == tokens() * 2
    assert today["models"] == {
        MODEL: {"input": 20, "cache_write": 200, "cache_read": 2000, "output": 40}
    }
    assert today["accounts"] == {
        "active": 2, "registered": 2, "by_type": {"max": 1, "team": 1}, "top_account_share": 0.5,
    }  # fmt: skip
    assert (today["sessions"], today["turns"], today["repos_active"]) == (2, 2, 2)
    assert today["features"] == {"cli.fleet.spawn": 3}
    assert {repo["key"]: repo["tokens_total"] for repo in today["repos"]} == {
        "github.com/o/alpha": tokens(),
        str((machine.root / "repos" / "beta").resolve()): tokens(),
    }
    assert set(today["repos"][0]) == {
        "repo_id",
        "key",
        "tokens_total",
        "sessions",
        "turns",
        "traits",
    }
    assert set(today["repos"][0]["traits"]) == {"host", "language", "size_bucket", "monorepo"}
    # the day's figure is the dashboard's, and nothing else is in the rollup
    assert by_date["2026-10-25"].tokens.total == tokens(cache_read=3000)
    assert sum(day.tokens.total for day in days) == _read("lifetime").tokens.total


def test_a_rollup_keeps_the_25_repositories_with_the_most_tokens_and_counts_them_all(
    machine: Machine,
) -> None:
    for number in range(30):
        _one_turn(
            machine,
            machine.repo(f"r{number:02d}"),
            f"s{number}",
            _day(0, 9),
            cache_read=1000 + number,
        )
    usage_stats.sweep(tz=IST)

    (day,) = usage_stats.daily_rollups(date(2026, 10, 26), date(2026, 10, 26), now=NOW, tz=IST)

    assert day.repos_active == 30 and len(day.repos) == 25
    assert [repo.tokens_total for repo in day.repos] == sorted(
        (repo.tokens_total for repo in day.repos), reverse=True
    )
    assert day.repos[0].tokens_total == tokens(cache_read=1029)


def test_a_day_with_only_feature_uses_is_still_a_rollup(machine: Machine) -> None:
    with store_session() as store:
        store.add_feature_uses({("2026-10-25", "tui.launch"): 1})
        (day,) = usage_stats.daily_rollups(
            date(2026, 10, 25), date(2026, 10, 26), store=store, tz=IST
        )
    assert (day.date, day.turns, day.tokens.total, day.features) == (
        "2026-10-25",
        0,
        0,
        {"tui.launch": 1},
    )


# --- feature counters ----------


def test_a_command_is_counted_by_its_path_and_never_by_what_was_typed() -> None:
    paths.ensure_home()
    CliRunner().invoke(
        app, ["task", "show", "do-not-record-this-argument", "--project", "secret-project"]
    )
    CliRunner().invoke(app, ["config", "list"])
    spool = paths.feature_use_path().read_text(encoding="utf-8")

    names = [line.split("\t")[1] for line in spool.splitlines()]
    assert names == ["cli.task.show", "cli.config.list"]
    assert "secret" not in spool and "argument" not in spool and "--project" not in spool


def test_the_features_in_the_spool_are_folded_into_the_store_by_day_and_name() -> None:
    paths.ensure_home()
    feature_use.record("cli.fleet.spawn", now=datetime(2026, 10, 25, 9, tzinfo=IST))
    feature_use.record("cli.fleet.spawn", now=datetime(2026, 10, 25, 23, tzinfo=IST))
    feature_use.record("tui.view.usage_stats", now=datetime(2026, 10, 26, 1, tzinfo=IST))

    with store_session() as store:
        assert feature_use.fold(store) == 3
        assert feature_use.fold(store) == 0  # the spool is empty once folded
        rows = store.feature_uses(None, None)
        stats = usage_stats.stats("lifetime", now=NOW, tz=IST, store=store)

    assert rows == [
        ("2026-10-25", "cli.fleet.spawn", 2),
        ("2026-10-26", "tui.view.usage_stats", 1),
    ]
    assert not paths.feature_use_path().exists()
    assert [(row.name, row.count) for row in stats.features] == [
        ("cli.fleet.spawn", 2), ("tui.view.usage_stats", 1),
    ]  # fmt: skip


def test_a_name_that_is_not_a_feature_name_is_not_recorded() -> None:
    paths.ensure_home()
    bad = ["", "Cli.Task", "cli task", "cli.task show", "/etc/passwd", "cli..x", "x" * 200, "a=b"]
    assert [feature_use.record(name) for name in bad] == [False] * len(bad)
    assert feature_use.record("cli.config.set") is True
    assert paths.feature_use_path().read_text(encoding="utf-8").count("\n") == 1


def test_hooks_and_the_mcp_server_are_not_features_the_user_used() -> None:
    paths.ensure_home()
    CliRunner().invoke(app, ["hook", "--help"])
    CliRunner().invoke(app, ["serve", "--help"])
    assert not paths.feature_use_path().exists()


def test_counting_fails_open_and_a_command_never_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths.ensure_home()
    paths.feature_use_path().mkdir()  # an unwritable spool: a directory where the file goes
    assert feature_use.record("cli.config.set") is False
    result = CliRunner().invoke(
        app, ["metrics", "stats", "--range", "today", "--no-sweep", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["range"] == "today"

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    shutil.rmtree(paths.feature_use_path().parent / "feature-use.log", ignore_errors=True)
    monkeypatch.setattr(feature_use, "record", explode)
    result = CliRunner().invoke(
        app, ["metrics", "stats", "--range", "today", "--no-sweep", "--json"]
    )
    assert result.exit_code == 0, result.output


def test_a_full_spool_stops_growing_and_a_dead_folds_leftovers_are_still_read() -> None:
    paths.ensure_home()
    spool = paths.feature_use_path()
    spool.write_bytes(b"x" * (feature_use.MAX_SPOOL_BYTES + 1))
    assert feature_use.record("cli.config.set") is False
    spool.unlink()
    leftover = spool.with_name(f"{spool.name}.99999.folding")  # a fold that died after its rename
    leftover.write_text("2026-10-25\tcli.config.set\n2026-10-25\tnot a name\n", encoding="utf-8")

    with store_session() as store:
        folded = feature_use.fold(store)
        rows = store.feature_uses(None, None)

    assert folded == 1 and rows == [("2026-10-25", "cli.config.set", 1)]
    assert not leftover.exists()


def test_recording_a_use_is_a_few_microseconds_not_a_database_open() -> None:
    import time

    paths.ensure_home()
    # CPU time, not the wall clock: on a machine running other agents (or a CI runner) a wall
    # budget measures the load, and this one failed under a load average of 100.
    began = time.process_time()
    for _ in range(200):
        feature_use.record("cli.metrics.stats")
    per_call = (time.process_time() - began) / 200
    assert per_call < 0.005, f"{per_call * 1e6:.0f} µs of CPU a use"
    assert os.path.getsize(paths.feature_use_path()) < 20_000


# --- the commands ----------


def test_metrics_stats_prints_the_model_as_json_and_as_a_page(machine: Machine) -> None:
    repo = machine.repo("cli")
    _one_turn(machine, repo, SESSION, _day(0, 9))
    runner = CliRunner()

    backfill = runner.invoke(app, ["--json", "metrics", "backfill"])
    as_json = runner.invoke(
        app, ["--json", "metrics", "stats", "--range", "lifetime", "--no-sweep"]
    )
    as_text = runner.invoke(app, ["metrics", "stats", "--range", "lifetime", "--no-sweep"])

    report = json.loads(backfill.output)
    assert (report["sessions_new"], report["turns"], report["failed"]) == (1, 1, 0)
    parsed = UsageStats.model_validate(json.loads(as_json.output))
    assert parsed.range == "lifetime" and parsed.tokens.total == tokens()
    assert "Usage — Lifetime" in as_text.output and "cli" in as_text.output
    assert "[bold]" not in as_text.output


def test_metrics_stats_on_an_empty_machine_says_how_to_fill_it() -> None:
    result = CliRunner().invoke(app, ["metrics", "stats", "--no-sweep"])
    assert result.exit_code == 0
    assert "No usage recorded for 7 days yet" in result.output
    assert "aisquare metrics backfill" in result.output


def test_an_empty_range_says_nothing_is_in_it_when_other_ranges_have_something(
    machine: Machine,
) -> None:
    _one_turn(machine, machine.repo("old"), SESSION, _day(-20, 9))  # long before the real today
    usage_stats.sweep(tz=IST)
    runner = CliRunner()

    quiet = runner.invoke(app, ["metrics", "stats", "--range", "today", "--no-sweep"])
    everything = runner.invoke(app, ["metrics", "stats", "--range", "lifetime", "--no-sweep"])

    assert "Nothing in today: Claude Code was not used in it." in quiet.output
    assert "1 turns are recorded in all: try --range lifetime." in quiet.output
    assert "backfill" not in quiet.output
    assert "Usage — Lifetime" in everything.output


def test_metrics_stats_refuses_a_range_it_does_not_know() -> None:
    result = CliRunner().invoke(app, ["metrics", "stats", "--range", "yesterday"])
    assert result.exit_code == 2
    assert "lifetime" in result.output and "'yesterday'" in result.output


def test_metrics_stats_reads_the_transcripts_first_unless_told_not_to(machine: Machine) -> None:
    _one_turn(machine, machine.repo("auto"), SESSION, _day(0, 9))
    runner = CliRunner()

    skipped = runner.invoke(
        app, ["--json", "metrics", "stats", "--range", "lifetime", "--no-sweep"]
    )
    swept = runner.invoke(app, ["--json", "metrics", "stats", "--range", "lifetime"])

    assert json.loads(skipped.output)["turns"] == 0
    assert json.loads(swept.output)["turns"] == 1


def test_the_utc_stamp_the_sweep_writes_sorts_with_the_hooks() -> None:
    """Both write ``usage_turn.recorded_at`` as an ISO string; the range read compares them as
    strings, so the sweep's stamp (from the transcript) must be the same shape as a hook's."""
    hook = datetime.now(UTC).isoformat()
    sweep = usage_stats._transcript_stamp("2026-10-25T04:12:33.123Z")
    assert sweep is not None and sweep.isoformat().endswith(hook[-6:])


def test_forgetting_a_project_for_good_removes_its_repository_profile(
    machine: Machine,
) -> None:
    """The profile holds the repository's root and remote: a purge takes it with the rest."""
    repo = machine.repo("purged")
    add_project(repo)
    _one_turn(machine, repo, SESSION, _day(0, 9))
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        (profile,) = store.repo_profiles().values()
        store.purge_project(profile.repo_id)
        assert store.repo_profiles() == {}
        assert store.usage_turn_rows(None, None) == []


# --- lifetime is everything recorded -----------------------------------------------------------


def _seed_turn(session_id: str, recorded_at: datetime, *, cache_read: int = 5000) -> None:
    """A session with one turn recorded at ``recorded_at``, as a hook with a skewed clock (or a
    sweep before the clamp) left it."""
    usage = usage_core.Usage(
        models={
            MODEL: usage_core.ModelUsage(requests=1, input=10, cache_read=cache_read, output=20)
        }
    )
    total = usage.totals()
    with store_session() as store:
        store.record_usage(
            SessionUsage(
                session_id=session_id,
                project_id="prj_x",
                repo_id="prj_x",
                usage=usage.to_dict(),
                turns=1,
                total_tokens=total.total,
                started_at=recorded_at,
                updated_at=recorded_at,
            ),
            TurnUsage(
                id=f"use_{session_id}",
                session_id=session_id,
                project_id="prj_x",
                seq=1,
                trigger="stop",
                recorded_at=recorded_at,
                usage=usage.to_dict(),
                total_tokens=total.total,
            ),
            previous_state=None,
        )


def test_a_turn_dated_after_today_is_in_lifetime_as_in_metrics_tokens(machine: Machine) -> None:
    _seed_turn("now", NOW, cache_read=1000)
    _seed_turn("skewed", datetime(2030, 1, 1, 8, tzinfo=UTC), cache_read=7000)  # a clock ahead

    lifetime = _read("lifetime")
    summary = metrics_service.summarize_tokens(metrics_service.session_usages())

    assert lifetime.tokens.total == summary.total_tokens == 2 * 30 + 1000 + 7000
    assert (lifetime.sessions, lifetime.turns) == (summary.sessions, summary.turns) == (2, 2)
    assert lifetime.until == "2030-01-01" and lifetime.days[-1].date == "2030-01-01"
    assert sum(day.tokens.total for day in lifetime.days) == lifetime.tokens.total
    assert (
        len(lifetime.days) == 2
    )  # today, and the one later day that has a turn: not 1,200 empty ones
    # the fixed ranges end today: the skewed turn is in none of them
    assert _read("30d").tokens.total == 30 + 1000 and _read("30d").until == "2026-10-26"


def test_the_sweep_never_dates_a_turn_after_now(machine: Machine) -> None:
    repo = machine.repo("skew")
    when = datetime(
        2030, 1, 1, 8, tzinfo=UTC
    )  # a transcript synced from a machine with its clock ahead
    machine.session(
        1,
        repo,
        SESSION,
        prompt(when - timedelta(seconds=5), cwd=repo),
        request(when, "future", cwd=repo, cache_read=7000),
    )
    usage_stats.sweep(now=NOW, tz=IST)

    with store_session() as store:
        (turn,) = store.turn_usages(SESSION)
    lifetime = _read("lifetime")
    summary = metrics_service.summarize_tokens(metrics_service.session_usages())

    assert turn.recorded_at == NOW.astimezone(UTC)  # clamped to the sweep's now
    assert lifetime.tokens.total == summary.total_tokens == tokens(cache_read=7000)
    assert lifetime.until == "2026-10-26"  # no day in 2030, in the page, the rollups or a batch
    assert _read("today").tokens.total == tokens(cache_read=7000)


def test_the_hooks_clock_is_the_stats_clock_in_these_tests(machine: Machine) -> None:
    """The fixture pins what the Stop hook stamps: three tests read at ``NOW`` what the hook
    recorded at the wall clock, and failed on every run after 18:30 UTC on 2026-10-26."""
    repo = machine.repo("clock")
    add_project(repo)
    transcript = _one_turn(machine, repo, SESSION, _day(0, 9))
    hooks_service.turn_stopped(repo, session_id=SESSION, transcript_path=str(transcript))

    with store_session() as store:
        (turn,) = store.turn_usages(SESSION)
        session = store.session_usage(SESSION)

    assert turn.recorded_at == NOW.astimezone(UTC)
    assert session is not None and session.updated_at == NOW.astimezone(UTC)
    assert _read("today").turns == 1


# --- one repository, however many clones --------------------------------------------------------


def test_clones_and_checkouts_of_one_remote_are_one_repository(machine: Machine) -> None:
    app = "git@github.com:Org/App.git"
    main = machine.repo("ws2-app", remote=app)
    clone = machine.repo("ws3-app", remote="https://user:token@github.com/org/app.git")
    other = machine.repo("other", remote="https://github.com/org/other")
    local = machine.repo("local-only")
    _one_turn(machine, main, "a", _day(0, 9), cache_read=5000)
    _one_turn(machine, clone, "b", _day(0, 10), cache_read=3000)
    _one_turn(machine, clone, "c", _day(-1, 10), cache_read=1000)
    _one_turn(machine, other, "d", _day(0, 11))
    _one_turn(machine, local, "e", _day(0, 12))
    usage_stats.sweep(tz=IST)

    week = _read("7d").repos
    merged = next(row for row in week.rows if row.sessions == 3)
    (day,) = [
        d
        for d in usage_stats.daily_rollups(date(2026, 10, 26), date(2026, 10, 26), now=NOW, tz=IST)
    ]

    assert week.active == 3 and len(week.rows) == 3  # not 4: ws2 and ws3 are one
    assert merged.tokens.total == tokens(cache_read=5000) + tokens(cache_read=3000) + tokens(
        cache_read=1000
    )
    assert merged.name == "ws2-app"  # the checkout that spent the most names it
    assert merged.repo_id == "remote:github.com/org/app"
    assert sorted(row.name for row in week.rows) == ["local-only", "other", "ws2-app"]
    # in a day's rollup too: one DayRepo per repository, whose key is the remote
    assert day.repos_active == 3 and len(day.repos) == 3
    keys = sorted(repo.key for repo in day.repos)
    assert keys == sorted(["github.com/org/app", "github.com/org/other", str(local.resolve())])
    app_row = next(repo for repo in day.repos if repo.key == "github.com/org/app")
    assert (app_row.sessions, app_row.tokens_total) == (
        2,
        tokens(cache_read=5000) + tokens(cache_read=3000),
    )


def test_a_repository_not_profiled_yet_is_counted_as_recorded_and_merges_once_it_is(
    machine: Machine,
) -> None:
    remote = "https://github.com/org/app"
    first, second = machine.repo("one", remote=remote), machine.repo("two", remote=remote)
    _one_turn(machine, first, "a", _day(0, 9))
    _one_turn(machine, second, "b", _day(0, 10))
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        profiles = store.repo_profiles()
        store._conn.execute("DELETE FROM repo_profile")  # type: ignore[attr-defined]
        store._conn.commit()  # type: ignore[attr-defined]
    assert len(profiles) == 2
    assert _read("today").repos.active == 2  # no profile, no remote known: two

    with store_session() as store:
        roots = {profile.repo_id: Path(profile.root) for profile in profiles.values()}
        usage_stats.refresh_profiles(store, roots, now=NOW)

    assert _read("today").repos.active == 1


def test_a_nickname_names_the_repository_it_was_given_to(machine: Machine) -> None:
    repo = machine.repo("dirname", remote="https://github.com/org/app")
    project = add_project(repo)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE project SET nickname = ? WHERE id = ?", ("the big one", project.id)
        )
        store._conn.commit()  # type: ignore[attr-defined]
    _one_turn(machine, repo, SESSION, _day(0, 9))
    usage_stats.sweep(tz=IST)

    assert [row.name for row in _read("today").repos.rows] == ["the big one"]


def test_a_rollup_counts_the_agents_started_by_role_and_the_tasks_done_each_local_day(
    machine: Machine,
) -> None:
    with store_session() as store:
        conn = store._conn  # type: ignore[attr-defined]
        for number, (role, when) in enumerate(
            [
                ("coder", _day(0, 9)), ("coder", _day(0, 10)), ("tester", _day(0, 11)),
                ("hush-custom-role", _day(0, 12)), ("manager", _day(-1, 23)),
                ("coder", _day(1, 0)),
            ]
        ):  # fmt: skip
            conn.execute(
                "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at) "
                "VALUES (?, 'prj_x', ?, ?, '%1', '/x', ?)",
                (f"a{number}", f"label-{number}", role, when.astimezone(UTC).isoformat()),
            )
        for number, when in enumerate([_day(0, 9), _day(0, 23), _day(-1, 9)]):
            conn.execute(
                "INSERT INTO team_event (id, project_id, kind, text, created_at) "
                "VALUES (?, 'prj_x', 'task_done', 't', ?)",
                (f"e{number}", when.astimezone(UTC).isoformat()),
            )
        conn.execute(  # a task that was merely claimed is not a task done
            "INSERT INTO team_event (id, project_id, kind, text, created_at) "
            "VALUES ('c', 'prj_x', 'task_claimed', 't', ?)",
            (_day(0, 9).astimezone(UTC).isoformat(),),
        )
        conn.commit()
        days = usage_stats.daily_rollups(
            date(2026, 10, 25), date(2026, 10, 27), now=NOW, tz=IST, store=store
        )

    fleet = {day.date: day.fleet.model_dump() for day in days}
    assert fleet == {
        "2026-10-25": {"agents_spawned": {"manager": 1}, "tasks_done": 1},
        "2026-10-26": {"agents_spawned": {"coder": 2, "other": 1, "tester": 1}, "tasks_done": 2},
        "2026-10-27": {"agents_spawned": {"coder": 1}, "tasks_done": 0},
    }  # the custom role is "other": a role's name is text an operator chose
