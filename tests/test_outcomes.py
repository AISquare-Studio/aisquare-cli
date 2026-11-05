"""``aisquare outcomes``: one week of what the agents achieved, what it took, and who steered.

A machine is built on disk (``tests/usage_stats_support.py``): repositories with commits, Claude
Code transcripts the sweep records, turn rows as the hooks write them, board events, a fleet's
rows, and ``gh`` replaced by a recorder. The report is read at a fixed clock in IST, whose local
midnight is 18:30 UTC the day before: the week's edge the tests are about.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from aisquare.cli import outcomes as outcomes_cli
from aisquare.cli.app import app
from aisquare.core import prompt_origin, repo_profile
from aisquare.core.ids import new_trace_id
from aisquare.core.store import store_session
from aisquare.models import (
    BASELINE_REASONS,
    ClientReason,
    FleetAgent,
    ProjectInfo,
    RepoProfile,
    TaskEventRow,
    TeamEvent,
    TeamSession,
    TurnMetric,
)
from aisquare.services import fleet as fleet_service
from aisquare.services import github_prs, outcomes, transcript_facts, usage_stats
from tests.rendered import plain
from tests.usage_stats_support import IST, Machine, prompt, request, stamp, tokens, write

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=IST)
"""Thursday of 2026-W44, 15:00 IST. The week began Monday 26 Oct 00:00 IST (Sunday 18:30 UTC)."""

S_MGR = "aaaaaaaa-0000-0000-0000-000000000001"
S_COD = "aaaaaaaa-0000-0000-0000-000000000002"
S_PLAIN = "aaaaaaaa-0000-0000-0000-000000000003"
S_LAST = "aaaaaaaa-0000-0000-0000-000000000004"
ME = "me@example.com"


def _at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """``hour:minute`` IST on ``day`` October 2026."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=IST)


# --- transcript entries ----------------------------------------------------------------------


def _edit(
    moment: datetime, message_id: str, *, cwd: Path, path: str, name: str = "Edit"
) -> dict[str, Any]:
    entry = request(moment, message_id, cwd=cwd, tool=name)
    entry["message"]["content"][0]["input"] = {"file_path": path}
    return entry


def _user_text(moment: datetime, *, cwd: Path, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp(moment),
        "cwd": str(cwd),
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def _denial(moment: datetime, *, cwd: Path) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": stamp(moment),
        "cwd": str(cwd),
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_denied",
                    "is_error": True,
                    "content": "The user doesn't want to proceed with this tool use. The tool use "
                    "was rejected (eg. if it was a file edit, the new_string was NOT written).",
                }
            ],
        },
    }


def _stop_feedback(moment: datetime, *, cwd: Path) -> dict[str, Any]:
    return {
        "type": "user",
        "isMeta": True,
        "timestamp": stamp(moment),
        "cwd": str(cwd),
        "message": {"role": "user", "content": "Stop hook feedback:\n2 board update(s) arrived"},
    }


# --- store rows ------------------------------------------------------------------------------


def _turn(
    session_id: str,
    project_id: str,
    start: datetime,
    end: datetime | None = None,
    *,
    origin: str | None = None,
) -> None:
    """A turn row as ``UserPromptSubmit`` opens it, closed at ``end`` as ``Stop`` closes it."""
    with store_session() as store:
        store.open_turn(
            TurnMetric(
                trace_id=new_trace_id(),
                project_id=project_id,
                session_id=session_id,
                started_at=start,
                trigger="prompt_submit",
                origin=origin,
            )
        )
        if end is not None:
            store.close_turn(session_id, ended_at=end)


def _event(project_id: str, kind: str, task_id: str, at: datetime) -> None:
    with store_session() as store:
        store.add_team_event(
            TeamEvent(
                id=f"evt_{kind}_{task_id}_{at.timestamp():.0f}",
                project_id=project_id,
                kind=kind,
                text=task_id,
                task_id=task_id,
                created_at=at,
            )
        )


def _fleet(
    session_id: str,
    project_id: str,
    role: str,
    cwd: Path,
    *,
    label: str | None = None,
    spawned_by: str | None = None,
) -> None:
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project_id,
                role=role,
                started_at=_at(26, 9),
                last_seen_at=_at(26, 9),
            )
        )
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{label or role}",
                project_id=project_id,
                label=label or role,
                role=role,
                pane_id=f"%{len(label or role)}",
                spawned_by=spawned_by,
                session_id=session_id,
                cwd=cwd,
                created_at=_at(26, 9),
                first_prompt="acked",
            )
        )


# --- git -------------------------------------------------------------------------------------


def _git(repo: Path, *args: str, when: datetime, author: str = ME, committer: str = ME) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": author,
        "GIT_AUTHOR_DATE": when.isoformat(),
        "GIT_COMMITTER_NAME": "c",
        "GIT_COMMITTER_EMAIL": committer,
        "GIT_COMMITTER_DATE": when.isoformat(),
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def _commit(repo: Path, name: str, text: str, subject: str, when: datetime, **who: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name, when=when, **who)
    _git(repo, "commit", "-q", "-m", subject, when=when, **who)


# --- gh --------------------------------------------------------------------------------------


class FakeGh:
    """``gh api graphql``, answering each alias ``r<n>_<week>_<kind>`` from ``counts``."""

    def __init__(self, counts: dict[str, int], *, returncode: int = 0, stderr: str = "") -> None:
        self.counts = counts
        self.returncode = returncode
        self.stderr = stderr
        self.queries: list[str] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        query = next(arg for arg in args if arg.startswith("query="))
        self.queries.append(query)
        data = {
            alias: {"issueCount": count}
            for alias, count in self.counts.items()
            if f"{alias}:" in query
        }
        return subprocess.CompletedProcess(
            list(args), self.returncode, json.dumps({"data": data}), self.stderr
        )


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh(
        {"r0_this_merged": 3, "r0_this_opened": 4, "r0_last_merged": 1, "r0_last_opened": 2}
    )
    monkeypatch.setattr(github_prs, "_gh", fake)
    monkeypatch.setattr(outcomes, "_gh_installed", lambda: True)
    return fake


# --- the world -------------------------------------------------------------------------------


@pytest.fixture
def world(machine: Machine, gh: FakeGh) -> dict[str, Any]:
    """A week of work: a manager and a coder in ``widget`` (on GitHub), a plain session in
    ``notes`` (no remote, no hook), and one turn the week before. See each test for what it
    adds up to."""
    widget = machine.repo("widget", remote="git@github.com:acme/widget.git")
    notes = machine.repo("notes")
    subprocess.run(["git", "-C", str(widget), "config", "user.email", ME], check=True)
    found = usage_stats.repo_of(widget)
    assert found is not None
    pid = found[0]
    with store_session() as store:
        store.ensure_project(ProjectInfo(id=pid, root=widget))
        store.set_project_nickname(pid, "My Widget Nickname")

    # The manager: spawned (its first message), woken, then the person, a continuation, an Esc.
    machine.session(
        1,
        widget,
        S_MGR,
        prompt(_at(26, 10), cwd=widget, text="You are the manager of widget."),
        request(_at(26, 10, 0, 30), "m1", cwd=widget, tool="Bash"),
        prompt(_at(26, 11), cwd=widget, text=fleet_service.NUDGE_TEXT),
        request(_at(26, 11, 0, 20), "m2", cwd=widget),
        prompt(_at(26, 12), cwd=widget, text="please also fix the docs"),
        request(_at(26, 12, 0, 30), "m3", cwd=widget),
        _stop_feedback(_at(26, 12, 5), cwd=widget),
        request(_at(26, 12, 5, 20), "m4", cwd=widget),
        _user_text(_at(26, 12, 6), cwd=widget, text="[Request interrupted by user]"),
        prompt(_at(27, 9), cwd=widget, text="ship it"),
        request(_at(27, 9, 0, 30), "m5", cwd=widget),
    )
    _fleet(S_MGR, pid, "manager", widget)
    _turn(S_MGR, pid, _at(26, 10), _at(26, 10, 30))
    _turn(S_MGR, pid, _at(26, 11), _at(26, 11, 10))
    _turn(S_MGR, pid, _at(26, 12))  # no Stop: the person's Esc at 12:06 ended it
    _turn(S_MGR, pid, _at(27, 9), _at(27, 9, 20), origin=prompt_origin.HUMAN_TYPED)

    # The coder: every turn's origin recorded; two edits, a denial, a subagent's edit.
    transcript = machine.session(
        1,
        widget,
        S_COD,
        prompt(_at(26, 10, 5), cwd=widget, text="Claim task 7 and do it."),
        _edit(_at(26, 10, 5, 30), "c1", cwd=widget, path="/w/widget/a.py"),
        _edit(_at(26, 10, 6), "c2", cwd=widget, path="/w/widget/b.py", name="Write"),
        _denial(_at(26, 10, 7), cwd=widget),
        _user_text(
            _at(26, 10, 7, 1), cwd=widget, text="[Request interrupted by user for tool use]"
        ),
        request(_at(26, 10, 8), "c3", cwd=widget),
        prompt(_at(26, 13), cwd=widget, text="rebase onto main"),
        request(_at(26, 13, 0, 30), "c4", cwd=widget),
    )
    write(
        transcript.with_suffix("") / "subagents" / "agent-1.jsonl",
        _edit(_at(26, 10, 10), "c5", cwd=widget, path="/w/widget/c.py"),
    )
    _fleet(S_COD, pid, "coder", widget)
    _turn(S_COD, pid, _at(26, 10, 5), _at(26, 10, 45), origin="agent:spawn")
    _turn(S_COD, pid, _at(26, 13), _at(26, 13, 30), origin="agent:tell")

    # A session no hook saw, in a repository with no remote: four prompts, all a person's.
    machine.session(
        1,
        notes,
        S_PLAIN,
        *[
            entry
            for index, minute in enumerate((0, 30, 60, 90))
            for entry in (
                prompt(_at(27, 10) + timedelta(minutes=minute), cwd=notes, text=f"note {index}"),
                request(_at(27, 10, 1) + timedelta(minutes=minute), f"p{index}", cwd=notes),
            )
        ],
    )

    # The week before: one turn, an hour, a person's.
    machine.session(
        1,
        widget,
        S_LAST,
        prompt(_at(20, 10), cwd=widget, text="start"),
        request(_at(20, 10, 1), "l1", cwd=widget),
    )
    _turn(S_LAST, pid, _at(20, 10), _at(20, 11), origin=prompt_origin.HUMAN_TYPED)

    # The board: tsk_1 done twice (one task), reopened once; tsk_2 done; tsk_0 the week before.
    _event(pid, "task_done", "tsk_1", _at(26, 14))
    _event(pid, "task_reopened", "tsk_1", _at(26, 15))
    _event(pid, "task_done", "tsk_1", _at(27, 11))
    _event(pid, "task_done", "tsk_2", _at(28, 9))
    _event(pid, "task_done", "tsk_0", _at(21, 9))

    # The commits: one the week before; two of mine this week (+3 -1, then +2), one by someone
    # else, one GitHub made, and mine cherry-picked onto a branch (the same commit again).
    _commit(widget, "a.txt", "one\n", "last week's", _at(20, 11))
    _commit(widget, "a.txt", "uno\ntwo\nthree\n", "add a", _at(26, 10, 20))
    _commit(widget, "o.txt", "x\n", "someone else's", _at(26, 10, 30), author="o@example.com")
    _commit(
        widget,
        "g.txt",
        "x\n",
        "squashed on github",
        _at(26, 10, 40),
        committer="noreply@github.com",
    )
    _commit(widget, "b.txt", "1\n2\n", "add b", _at(26, 10, 50))
    _git(widget, "checkout", "-q", "-b", "copy", "HEAD~3", when=_at(27, 10))
    _git(widget, "cherry-pick", "HEAD@{1}", when=_at(27, 10))  # "add b", rebased elsewhere
    _git(widget, "checkout", "-q", "-", when=_at(27, 10))

    usage_stats.sweep(now=NOW, tz=IST)
    return {"widget": widget, "notes": notes, "pid": pid}


def _report(week: str | None = None, **kwargs: Any) -> Any:
    return outcomes.week_report(week, now=NOW, tz=IST, **kwargs)


# --- the week ----------------------------------------------------------------------------------


def test_the_default_week_is_this_iso_week_from_local_monday_midnight_to_now() -> None:
    week = outcomes.parse_week(None, now=NOW, tz=IST)
    assert week.label == "2026-W44"
    assert week.start == _at(26, 0) and week.start.astimezone(UTC) == datetime(
        2026, 10, 25, 18, 30, tzinfo=UTC
    )
    assert week.end == datetime(2026, 11, 2, 0, 0, tzinfo=IST)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("this", "2026-W44"),
        ("last", "2026-W43"),
        ("2026-W43", "2026-W43"),
        ("2026w43", "2026-W43"),
        ("2026-W3", "2026-W03"),
        (" 2025-W52 ", "2025-W52"),
        ("2026-W01", "2026-W01"),
    ],
)
def test_week_names_a_week(text: str, label: str) -> None:
    assert outcomes.parse_week(text, now=NOW, tz=IST).label == label


def test_the_local_week_starts_at_local_midnight_whatever_utc_says() -> None:
    """01:00 Monday IST is still Sunday in UTC: the week is already the new one."""
    monday_night = datetime(2026, 10, 26, 1, 0, tzinfo=IST)
    assert monday_night.astimezone(UTC).weekday() == 6
    assert outcomes.parse_week(None, now=monday_night, tz=IST).label == "2026-W44"
    sunday_late = datetime(2026, 10, 25, 23, 59, tzinfo=IST)
    assert outcomes.parse_week(None, now=sunday_late, tz=IST).label == "2026-W43"


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("yesterday", "is not a week: give 'last', or an ISO week such as 2026-W44"),
        ("2026-43", "is not a week"),
        ("W43", "is not a week"),
        ("2025-W53", "2025 has no week 53: its weeks are W01 to W52"),
        ("2026-W00", "2026 has no week 0"),
        ("2026-W45", "2026-W45 has not begun: this week is 2026-W44"),
        ("9999-W52", "9999-W52 has not begun: this week is 2026-W44"),
        ("0000-W01", "0000-W01 is before 1970: give a week from 1970-W01 on, such as 2026-W44"),
        ("0001-W01", "0001-W01 is before 1970"),
    ],
)
def test_a_bad_week_is_refused_saying_what_is_accepted(text: str, said: str) -> None:
    with pytest.raises(outcomes.WeekError, match=said.replace("(", r"\(")):
        outcomes.parse_week(text, now=NOW, tz=IST)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("yesterday", "is not a week"),
        ("0000-W01", "is before 1970"),
        ("0001-W01", "is before 1970"),
        ("9999-W52", "has not begun"),
    ],
)
def test_a_bad_week_on_the_command_line_is_a_usage_error_naming_the_option(
    text: str, said: str
) -> None:
    """Exit 2 with the usual usage error, never a traceback: a year this clock cannot place
    (0, 1, 9999) crashed with exit 1 (review of 8ad337d7, 2)."""
    result = CliRunner().invoke(app, ["outcomes", "--week", text, "--no-gh"])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    flat = plain(result.output)  # GitHub Actions styles the `-` of `--week` as its own span
    assert "--week" in flat and said in flat, flat


def test_turns_either_side_of_local_midnight_land_in_their_own_week(machine: Machine) -> None:
    """23:50 Sunday IST is the week before; 00:10 Monday IST is this week (both are Sunday in
    UTC)."""
    _turn("s-edge", "prj_edge", _at(25, 23, 50), _at(25, 23, 55), origin=prompt_origin.HUMAN_TYPED)
    _turn("s-edge", "prj_edge", _at(26, 0, 10), _at(26, 0, 20), origin=prompt_origin.HUMAN_TYPED)
    this, last = _report(use_gh=False), _report("last", use_gh=False)
    assert (this.effort.turns, this.effort.agent_hours) == (1, round(10 / 60, 2))
    assert (last.effort.turns, last.effort.agent_hours) == (1, round(5 / 60, 2))
    assert this.since == _at(26, 0) and last.until == _at(26, 0)


# --- an empty week -----------------------------------------------------------------------------


def test_an_empty_week_is_one_nothing_recorded_line(machine: Machine) -> None:
    result = _report("2026-W40", use_gh=False)
    assert result.empty
    assert outcomes_cli.render_outcomes(result) == [
        "Nothing recorded in 2026-W40: no agent turn, no usage, no board task moved."
    ]
    printed = CliRunner().invoke(app, ["outcomes", "--week", "2026-W40", "--no-gh"])
    assert printed.exit_code == 0, printed.output
    assert printed.output.strip().splitlines() == [
        "Nothing recorded in 2026-W40: no agent turn, no usage, no board task moved."
    ]


# --- 1. outcomes -------------------------------------------------------------------------------


def test_outcomes_count_prs_commits_and_tasks_per_repository_by_its_real_name(
    world: dict[str, Any], gh: FakeGh
) -> None:
    result = _report()
    o = result.outcomes
    names = [repo.name for repo in o.repos]
    assert names == ["widget", "notes"], "the remote's name, else the folder's; never a nickname"
    widget, notes = o.repos
    assert (widget.github, widget.prs_merged, widget.prs_opened) == ("acme/widget", 3, 4)
    assert (notes.github, notes.prs_merged, notes.prs_opened) == (None, None, None)
    # Mine only, once each: not someone else's, not GitHub's squash, not the cherry-pick again.
    assert (widget.commits, widget.lines_added, widget.lines_deleted) == (2, 5, 1)
    assert (widget.tasks_done, widget.tasks_reopened) == (2, 1)
    assert (o.prs_merged, o.prs_opened, o.commits, o.tasks_done, o.tasks_reopened) == (
        3,
        4,
        2,
        2,
        1,
    )
    assert o.prs_note is None
    [query] = gh.queries
    assert (
        "repo:acme/widget is:pr author:@me "
        "merged:2026-10-26T00:00:00+05:30..2026-10-29T14:59:59+05:30" in query
    )
    assert "created:2026-10-19T00:00:00+05:30..2026-10-22T14:59:59+05:30" in query


_N = repo_profile.normalise_remote


@pytest.mark.parametrize(
    ("remote", "github"),
    [
        pytest.param("github.com/acme/widget", "acme/widget", id="normalised"),
        pytest.param(_N("git@github.com:Acme/Widget.git"), "acme/widget", id="scp"),
        pytest.param(_N("https://u:tok@github.com/acme/widget.git"), "acme/widget", id="https"),
        pytest.param(_N("ssh://git@github.com:22/acme/widget/"), "acme/widget", id="ssh-port"),
        pytest.param("github.com.evil.com/x/y", None, id="host-suffixed"),
        pytest.param(_N("https://github.com.evil.com/x/y"), None, id="host-suffixed-url"),
        pytest.param("evilgithub.com/x/y", None, id="host-prefixed"),
        pytest.param("x/github.com/y", None, id="in-path"),
        pytest.param(_N("git@evil.com:github.com/x/y"), None, id="in-path-url"),
        pytest.param("github.com/", None, id="empty-path"),
        pytest.param("github.com", None, id="no-path"),
        pytest.param("github.com/acme", None, id="owner-only"),
        pytest.param("github.com/acme/widget/wiki", None, id="deeper-path"),
        pytest.param("gitlab.com/acme/widget", None, id="gitlab"),
        pytest.param(None, None, id="no-remote"),
    ],
)
def test_only_a_remote_on_github_com_itself_is_asked_of_github(
    remote: str | None, github: str | None
) -> None:
    profile = RepoProfile(
        repo_id="p1", name="widget", root="/srv/widget", remote=remote, refreshed_at=NOW
    )
    event = TaskEventRow(created_at=NOW, kind="task_done", project_id="p1")
    store: Any = None  # a profiled checkout's root is its profile's: the store is not asked
    repos = outcomes._week_repos(store, {"p1": profile}, lambda pid: pid, [], [event], [])
    assert repos["p1"].github == github


def test_without_gh_one_line_says_pull_requests_were_skipped(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outcomes, "_gh_installed", lambda: False)
    result = _report()
    assert (result.outcomes.prs_merged, result.outcomes.prs_opened) == (None, None)
    assert result.outcomes.prs_note == "skipped: gh is not installed"
    assert result.outcomes.commits == 2, "the rest is counted all the same"
    assert result.effort.cost_per_merged_pr is None
    lines = outcomes_cli.render_outcomes(result)
    assert "  pull requests   skipped: gh is not installed" in lines


def test_a_failing_gh_is_said_and_counts_nothing(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        github_prs, "_gh", FakeGh({}, returncode=1, stderr="gh: To get started, run: gh auth login")
    )
    result = _report()
    assert result.outcomes.prs_merged is None
    assert result.outcomes.prs_note == "skipped: gh failed (gh: To get started, run: gh auth login)"


# --- 2. effort ---------------------------------------------------------------------------------


def test_effort_sums_turn_durations_tokens_tools_and_edited_files(world: dict[str, Any]) -> None:
    effort = _report().effort
    # Manager 30 + 10 + 6 (no Stop: its Esc at 12:06 ended it) + 20; coder 40 + 30 minutes.
    assert effort.agent_hours == round(136 / 60, 2)
    # 11 turns: 4 + 2 rows, the manager's continuation and the plain session's 4 prompts,
    # these 5 with no end recorded.
    assert (effort.turns, effort.timed_turns, effort.untimed_turns) == (11, 6, 5)
    assert effort.sessions == 3
    assert effort.tool_calls == 4  # Bash, Edit, Write, and the subagent's Edit
    assert effort.files_edited == 3  # a.py, b.py and the subagent's c.py
    assert effort.tokens.total == 14 * tokens()
    assert effort.cost_usd > 0
    assert effort.cost_per_merged_pr == round(effort.cost_usd / 3, 2)
    assert effort.cost_per_done_task == round(effort.cost_usd / 2, 2)


# --- 3. direction ------------------------------------------------------------------------------


def test_direction_says_who_started_each_turn_recorded_or_inferred(world: dict[str, Any]) -> None:
    d = _report().direction
    assert d.by_origin == {
        "human:typed": 6,  # the manager's two, the plain session's four
        "agent:spawn": 2,  # the manager's first message (inferred), the coder's (recorded)
        "agent:continuation": 1,  # the manager's Stop hook, from its transcript
        "agent:tell": 1,
        "agent:wake": 1,  # the nudge's words
    }
    assert (d.turns, d.human_turns, d.agent_turns, d.human_share) == (11, 6, 5, round(6 / 11, 4))
    # Recorded: the manager's Tuesday turn and the coder's two; the other eight inferred.
    assert d.inferred_turns == 8


def test_direction_counts_leverage_interventions_and_session_modes(world: dict[str, Any]) -> None:
    d = _report().direction
    assert d.agent_minutes_per_human_prompt == round(round(136 / 60, 2) * 60 / 6, 1)
    assert d.tool_calls_per_human_prompt == round(4 / 6, 1)
    # One Esc; one tool denied (its "interrupted for tool use" line is that denial, not another).
    assert (d.interrupts, d.denied_permissions) == (1, 1)
    # Manager: 2 human prompts and an Esc, guided; coder: none, directive; plain: 4, collaborative.
    assert (d.sessions, d.directive, d.guided, d.collaborative) == (3, 1, 1, 1)


def test_a_worker_prompt_with_no_origin_recorded_is_its_managers_tell(machine: Machine) -> None:
    """A fleet coder's turns before origins were recorded: its first is the spawn's, the next
    its manager's tell; a session on no fleet row is a person's."""
    repo = machine.repo("solo")
    found = usage_stats.repo_of(repo)
    assert found is not None
    pid = found[0]
    with store_session() as store:
        store.ensure_project(ProjectInfo(id=pid, root=repo))
    machine.session(
        1,
        repo,
        S_COD,
        prompt(_at(26, 10), cwd=repo, text="Claim task 1."),
        request(_at(26, 10, 1), "x1", cwd=repo),
        prompt(_at(26, 11), cwd=repo, text="also add a test"),
        request(_at(26, 11, 1), "x2", cwd=repo),
    )
    _fleet(S_COD, pid, "coder", repo)
    _turn(S_COD, pid, _at(26, 10), _at(26, 10, 30))
    _turn(S_COD, pid, _at(26, 11), _at(26, 11, 30))
    usage_stats.sweep(now=NOW, tz=IST)
    d = _report(use_gh=False).direction
    assert d.by_origin == {"agent:spawn": 1, "agent:tell": 1}
    assert d.inferred_turns == 2 and d.human_turns == 0


# --- 4. against last week ------------------------------------------------------------------------


def test_vs_last_week_compares_to_the_same_point_of_the_week_before(world: dict[str, Any]) -> None:
    result = _report()
    assert result.compared_with == "2026-W43"
    assert (result.compared_since, result.compared_until) == (_at(19, 0), _at(22, 15))
    deltas = {d.name: d for d in result.vs_last_week}
    assert (deltas["prs_merged"].this_week, deltas["prs_merged"].last_week) == (3, 1)
    assert deltas["prs_merged"].change == 2 and deltas["prs_merged"].change_pct == 2.0
    assert (deltas["commits"].this_week, deltas["commits"].last_week) == (2, 1)
    assert (deltas["tasks_done"].this_week, deltas["tasks_done"].last_week) == (2, 1)
    assert (deltas["agent_hours"].this_week, deltas["agent_hours"].last_week) == (2.27, 1.0)
    assert (deltas["turns"].this_week, deltas["turns"].last_week) == (11, 1)
    assert deltas["human_share"].last_week == 1.0
    assert (deltas["interrupts"].this_week, deltas["interrupts"].last_week) == (1, 0)
    assert deltas["interrupts"].change_pct is None, "no percentage of a zero"


def test_a_past_week_is_compared_with_the_whole_week_before(world: dict[str, Any]) -> None:
    result = _report("last")
    assert result.week == "2026-W43" and not result.in_progress
    assert (result.since, result.until) == (_at(19, 0), _at(26, 0))
    assert (result.compared_since, result.compared_until) == (_at(12, 0), _at(19, 0))
    assert result.effort.turns == 1 and result.outcomes.tasks_done == 1


# --- the text and the JSON ---------------------------------------------------------------------


def test_the_text_has_the_four_sections_and_says_what_was_inferred(world: dict[str, Any]) -> None:
    lines = outcomes_cli.render_outcomes(_report())
    text = "\n".join(lines)
    assert lines[0] == "Outcomes — 2026-W44 (Mon 26 Oct → now, Thu 29 Oct 15:00)"
    for head in ("OUTCOMES", "EFFORT", "DIRECTION — who steered", "VS LAST WEEK — 2026-W43"):
        assert head in text
    assert "  pull requests   3 merged · 4 opened" in lines
    assert "  commits         2  (+5 -1 lines)" in lines
    assert "  board tasks     2 done · 1 reopened" in lines
    assert "  turn origin     55% human (6) · 45% agent (5) of 11 turns" in lines
    assert (
        "                  8 of them inferred "
        "(no origin recorded: older turns, or a session no hook saw)" in lines
    )
    assert "  interventions   1 interrupts · 1 tool uses denied" in lines
    assert lines[-1] == "How each figure is measured: aisquare outcomes --help"


def test_json_gives_the_same_data(world: dict[str, Any]) -> None:
    result = _report()
    data = outcomes.outcomes_json(result)
    assert data["week"] == "2026-W44" and data["in_progress"] is True
    assert data["outcomes"]["prs_merged"] == 3
    assert data["effort"]["files_edited"] == 3
    assert data["direction"]["by_origin"]["human:typed"] == 6
    assert {d["name"] for d in data["vs_last_week"]} >= {"prs_merged", "agent_hours", "human_share"}
    assert json.loads(json.dumps(data)) == data


def test_the_command_prints_json_under_json(machine: Machine) -> None:
    printed = CliRunner().invoke(app, ["--json", "outcomes", "--week", "2026-W40", "--no-gh"])
    assert printed.exit_code == 0, printed.output
    data = json.loads(printed.output)
    assert data["week"] == "2026-W40" and data["empty"] is True


def test_outcomes_is_listed_in_the_top_level_help() -> None:
    printed = CliRunner().invoke(app, ["--help"])
    assert "outcomes" in printed.output


def test_the_help_says_how_each_figure_is_measured() -> None:
    printed = CliRunner().invoke(app, ["outcomes", "--help"], terminal_width=100)
    for said in (
        "PRs you authored",
        "user.email",
        "UserPromptSubmit to Stop",
        "[Request interrupted by user]",
        "directive: at most 1 human prompt and no interrupt",
        "older turns are inferred",
    ):
        assert said in " ".join(printed.output.split()), said


# --- the transcripts' facts --------------------------------------------------------------------


def test_the_nudge_text_the_scan_knows_is_the_fleets() -> None:
    assert transcript_facts.NUDGE_TEXT == fleet_service.NUDGE_TEXT


def _session_file(tmp_path: Path) -> Path:
    cwd = tmp_path
    path = write(
        tmp_path / "s.jsonl",
        prompt(_at(26, 10), cwd=cwd, text="do the thing"),
        _edit(_at(26, 10, 1), "e1", cwd=cwd, path="/w/a.py"),
        prompt(_at(26, 11), cwd=cwd, text=transcript_facts.NUDGE_TEXT),
        prompt(
            _at(26, 12),
            cwd=cwd,
            text="<task-notification><task-id>b1</task-id></task-notification>",
        ),
        _stop_feedback(_at(26, 12, 5), cwd=cwd),
        _user_text(_at(26, 12, 6), cwd=cwd, text="[Request interrupted by user]"),
        _denial(_at(26, 12, 7), cwd=cwd),
        _user_text(_at(26, 12, 7, 1), cwd=cwd, text="[Request interrupted by user for tool use]"),
        {**prompt(_at(26, 12, 8), cwd=cwd, text="a subagent's task"), "isSidechain": True},
    )
    write(
        tmp_path / "s" / "subagents" / "agent-1.jsonl",
        prompt(_at(26, 10, 2), cwd=cwd, text="the subagent's prompt is no turn"),
        _edit(_at(26, 10, 3), "e2", cwd=cwd, path="/w/b.py", name="NotebookEdit"),
    )
    return path


def _kinds(facts: list[transcript_facts.Fact]) -> list[tuple[str, str]]:
    return [(fact.kind, fact.detail) for fact in facts]


def test_the_scan_finds_prompts_by_class_continuations_interrupts_denials_and_edits(
    tmp_path: Path,
) -> None:
    path = _session_file(tmp_path)
    facts = transcript_facts.read_facts([path], since=_at(19, 0))[path]
    assert _kinds(facts) == [
        ("p", "text"),
        ("e", "/w/a.py"),
        ("e", "/w/b.py"),  # the subagent's
        ("p", "wake"),
        ("p", "notice"),
        ("c", ""),
        ("i", ""),
        ("d", ""),  # and its "interrupted for tool use" is not a second intervention
    ]
    assert facts[0].at == _at(26, 10).astimezone(UTC)


def test_the_scan_reads_only_what_was_added_and_starts_over_on_a_rewritten_file(
    tmp_path: Path,
) -> None:
    path = _session_file(tmp_path)
    first = transcript_facts.read_facts([path], since=_at(19, 0))[path]
    write(path, prompt(_at(27, 9), cwd=tmp_path, text="more"))
    again = transcript_facts.read_facts([path], since=_at(19, 0))[path]
    assert _kinds(again) == [*_kinds(first), ("p", "text")]
    cached = json.loads(transcript_facts.facts_cache_path().read_text(encoding="utf-8"))
    assert cached["files"][str(path)]["offset"] == path.stat().st_size
    path.write_text(json.dumps(prompt(_at(28, 9), cwd=tmp_path, text="new")) + "\n")
    rewritten = transcript_facts.read_facts([path], since=_at(19, 0))[path]
    assert _kinds(rewritten) == [("e", "/w/b.py"), ("p", "text")], "its subagent's file is kept"


def test_a_half_written_last_line_is_read_next_time(tmp_path: Path) -> None:
    path = write(tmp_path / "s.jsonl", prompt(_at(26, 10), cwd=tmp_path, text="one"))
    line = json.dumps(prompt(_at(26, 11), cwd=tmp_path, text="two"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[:20])
    assert len(transcript_facts.read_facts([path], since=_at(19, 0))[path]) == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[20:] + "\n")
    assert len(transcript_facts.read_facts([path], since=_at(19, 0))[path]) == 2


def test_a_transcript_last_written_before_the_week_is_not_read(tmp_path: Path) -> None:
    path = _session_file(tmp_path)
    old = _at(1, 0).timestamp()
    os.utime(path, (old, old))
    assert transcript_facts.read_facts([path], since=_at(19, 0))[path] == [
        fact
        for fact in transcript_facts.read_facts([path], since=_at(1, 0))[path]
        if fact.kind == "e" and fact.detail == "/w/b.py"
    ], "only the subagent's file, written now, is read"


def test_a_session_of_an_agent_this_install_does_not_know_is_read_by_no_reader(
    tmp_path: Path,
) -> None:
    """A session row naming an agent with no adapter here (a plugin's not registered, a row a
    newer install wrote) gives no facts: its transcript is skipped and nothing of it cached,
    never read as the default agent's, whose reader finds a prompt, edits, an Esc and a denial
    in this very file (review r36 of #113, finding 4). A session that recorded no agent is
    still read as the default's: a transcript from before adapters."""
    path = _session_file(tmp_path)

    found = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "gemini-cli"})

    assert found == {path: []}
    assert not transcript_facts.facts_cache_path().exists(), "nothing of it was read"
    assert transcript_facts.read_facts([path], since=_at(19, 0), agents={path: None})[path]


def test_a_large_read_goes_through_a_pool_and_finds_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [_session_file(tmp_path / str(n)) for n in range(3)]
    serial = transcript_facts.read_facts(paths, since=_at(19, 0))
    transcript_facts.facts_cache_path().unlink()
    monkeypatch.setattr(transcript_facts, "PARALLEL_BYTES", 0)
    pooled = transcript_facts.read_facts(paths, since=_at(19, 0))
    assert {str(k): _kinds(v) for k, v in pooled.items()} == {
        str(k): _kinds(v) for k, v in serial.items()
    }


# --- a Codex session's facts (SCOPE #113 092 row 10) --------------------------------------------

CODEX_FACTS = Path(__file__).parent / "fixtures" / "agent_transcripts" / "codex-facts-0.164.0.jsonl"
"""Codex 0.164.0's own rollout (a loopback provider, the TUI under tmux, ``gpt-5.5``'s tools):
``PATCH it`` applied by its freeform ``apply_patch`` (a ``FileChange`` of
``/work/alpha/hello.txt``), ``ESC touch /work/refused`` whose approval was refused with Esc,
``SLOW one`` stopped with Esc while the model worked, ``RUN sleep 20`` stopped with Esc while the
command ran, ``CONT now`` kept going by a Stop hook's ``block``, and ``ESC touch /work/approved``
approved with ``1``."""
CODEX_LINES = CODEX_FACTS.read_text(encoding="utf-8").splitlines()
CODEX_THREAD = "01a1a3a6-20d4-7d93-83bf-8dc18769f53f"
CODEX_KINDS = [
    ("p", "text"),
    ("e", "/work/alpha/hello.txt"),
    ("p", "text"),
    ("d", ""),  # its approval refused: the turn it stopped is no interrupt besides
    ("p", "text"),
    ("i", ""),  # Esc while the model worked
    ("p", "text"),
    ("i", ""),  # Esc while its command ran: no approval held it, so no refusal
    ("p", "text"),
    ("c", ""),  # the Stop hook's block, back as <hook_prompt hook_run_id="stop:…">
    ("p", "text"),  # an escalated command approved: no refusal
]


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch ``CODEX_HOME``: Codex's plain slot, which the usage sweep reads."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _codex_rollout(
    home: Path, cwd: Path, *, turns: int | None = None, said: dict[str, str] | None = None
) -> Path:
    """The recorded rollout as Codex writes it in ``home``, run in ``cwd`` and moved to begin on
    Tuesday 27 Oct, 12:00 IST: its first ``turns`` turns (all of them by default), each of its
    words ``said`` maps made the words it maps them to."""
    first = datetime.fromisoformat(json.loads(CODEX_LINES[0])["timestamp"].replace("Z", "+00:00"))
    shift = _at(27, 12) - first
    path = (
        home
        / "sessions"
        / "2026"
        / "10"
        / "27"
        / f"rollout-2026-10-27T06-30-00-{CODEX_THREAD}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    started = 0
    with path.open("w", encoding="utf-8") as handle:
        for line in CODEX_LINES:
            record = json.loads(line)
            started += (record.get("payload") or {}).get("type") == "task_started"
            if turns is not None and started > turns:
                break
            moment = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")) + shift
            record["timestamp"] = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            handle.write(json.dumps(_moved(record, {"/work/alpha": str(cwd), **(said or {})})))
            handle.write("\n")
    return path


def _moved(value: Any, swaps: dict[str, str]) -> Any:
    """``value`` with each of ``swaps`` made in its strings (the recorded folder made the
    scratch one): never in the JSON's text, where a Windows path's backslashes are escapes."""
    if isinstance(value, str):
        for old, new in swaps.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_moved(item, swaps) for item in value]
    if isinstance(value, dict):
        return {_moved(key, swaps): _moved(item, swaps) for key, item in value.items()}
    return value


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_an_edit_a_refused_tool_and_an_esc_count_alike_whichever_agent_ran_them(
    machine: Machine, codex_home: Path, agent: str
) -> None:
    """The row's acceptance: a session that edits a file, has a tool refused at its permission
    prompt and is stopped with Esc counts one file edited, one denial and one interrupt in its
    week, read by its own agent's reader: Claude Code's transcript, and Codex's own rollout
    (its first three turns: a patch, an approval refused, an Esc). Before the slot a Codex
    rollout was read as Claude Code's JSONL, and none of the three was found."""
    repo = machine.repo("alpha")
    if agent == "codex":
        _codex_rollout(codex_home, repo, turns=3)
    else:
        machine.session(
            1,
            repo,
            S_PLAIN,
            prompt(_at(27, 12), cwd=repo, text="PATCH it"),
            _edit(_at(27, 12, 0, 1), "a1", cwd=repo, path=str(repo / "hello.txt")),
            prompt(_at(27, 12, 0, 2), cwd=repo, text="ESC touch /work/refused"),
            request(_at(27, 12, 0, 3), "a2", cwd=repo, tool="Bash"),
            _denial(_at(27, 12, 0, 4), cwd=repo),
            _user_text(
                _at(27, 12, 0, 4) + timedelta(milliseconds=100),
                cwd=repo,
                text="[Request interrupted by user for tool use]",
            ),
            prompt(_at(27, 12, 0, 5), cwd=repo, text="SLOW one"),
            _user_text(_at(27, 12, 0, 8), cwd=repo, text="[Request interrupted by user]"),
        )
    usage_stats.sweep(now=NOW, tz=IST)

    result = _report(use_gh=False)
    printed = CliRunner().invoke(app, ["--json", "outcomes", "--week", "2026-W44", "--no-gh"])

    assert result.effort.files_edited == 1
    d = result.direction
    assert (d.interrupts, d.denied_permissions) == (1, 1)
    assert d.by_origin == {"human:typed": 3}, "its three prompts, inferred: no hook watched it"
    assert (d.sessions, d.guided) == (1, 1), "three prompts of a person's and an Esc"
    assert printed.exit_code == 0, printed.output
    data = json.loads(printed.output)
    assert data["effort"]["files_edited"] == 1
    assert (data["direction"]["interrupts"], data["direction"]["denied_permissions"]) == (1, 1)


def test_codexs_rollout_says_who_steered_it_as_its_own_reader_reads_it(tmp_path: Path) -> None:
    """Every fact of the recorded rollout, in order: Esc while a command ran is an interrupt (no
    approval held it), an approved escalated command no refusal, a Stop hook's block a
    continuation, and the patch an edit of the file its ``FileChange`` names."""
    path = tmp_path / "rollout.jsonl"
    path.write_text(CODEX_FACTS.read_text(encoding="utf-8"), encoding="utf-8")
    facts = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    assert _kinds(facts) == CODEX_KINDS


CODEX_PATCH_REFUSED = CODEX_FACTS.with_name("codex-patch-refused-0.164.0.jsonl")
"""Codex 0.164.0's own rollout as :data:`CODEX_FACTS` was recorded: ``PATCH it`` applied, then
``ESC touch /work/refused`` and ``OUTPATCH it`` (a freeform ``apply_patch`` adding
``/work/outside.txt``, outside the workspace, which Codex asks about) each refused with ``n``."""


def test_a_codex_patch_refused_at_its_approval_is_a_refusal_however_it_was_called(
    tmp_path: Path,
) -> None:
    """A patch applies at once, so one answered ``aborted by user`` was held by its approval
    alone: a refusal, and the turn it stopped no interrupt. Called as Codex's freeform
    ``apply_patch`` tool (``aborted by user after 1.3s``), and as the ``apply_patch`` heredoc a
    model without that tool runs through ``exec_command`` (``local-fixture``'s, its records as
    0.164.0 wrote them)."""
    path = tmp_path / "freeform.jsonl"
    path.write_text(CODEX_PATCH_REFUSED.read_text(encoding="utf-8"), encoding="utf-8")
    heredoc = tmp_path / "heredoc.jsonl"
    patch = "*** Begin Patch\n*** Add File: /work/outside.txt\n+fresh\n*** End Patch\n"
    records = [
        (
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "OUTPATCH it"}],
            },
        ),
        (
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "c5",
                "arguments": json.dumps({"cmd": f"apply_patch <<'EOF'\n{patch}EOF\n"}),
            },
        ),
        (
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "c5",
                "output": "Wall time: 1.3 seconds\naborted by user",
            },
        ),
        ("event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
    ]
    heredoc.write_text(
        "".join(
            json.dumps({"timestamp": stamp(_at(27, 12, 0, n)), "type": kind, "payload": payload})
            + "\n"
            for n, (kind, payload) in enumerate(records)
        ),
        encoding="utf-8",
    )
    found = transcript_facts.read_facts(
        [path, heredoc], since=_at(19, 0), agents={path: "codex", heredoc: "codex"}
    )
    assert _kinds(found[path]) == [
        ("p", "text"),
        ("e", "/work/alpha/hello.txt"),
        ("p", "text"),
        ("d", ""),  # the command refused
        ("p", "text"),
        ("d", ""),  # the patch refused: no FileChange, and no interrupt
    ]
    assert _kinds(found[heredoc]) == [("p", "text"), ("d", "")]


CODEX_REVIEW_DENIED = {
    version: CODEX_FACTS.with_name(f"codex-review-denied-{version}.jsonl")
    for version in ("0.164.0", "0.166.0")
}
"""Codex's own rollouts (``codex exec --approve-for-me``, a loopback provider whose automatic
reviewer answers ``{"outcome": "deny", …}``), one per release the beta covers: ``ESC echo hello``
(asking to leave the sandbox) and ``RUN rm -f nothing.txt`` (inside it, which Codex asks its
reviewer about too), each denied. Trimmed to the turns' own records, the folder made
``/work/alpha``."""


@pytest.mark.parametrize("version", sorted(CODEX_REVIEW_DENIED))
def test_a_command_codexs_automatic_reviewer_denied_is_a_denial(
    tmp_path: Path, version: str
) -> None:
    """R7 of 0.10.0: under ``--approve-for-me`` Codex's automatic reviewer answers the
    approval, and a command it denies is never run: a denial, as one the human refused at
    the prompt is, whether it asked to leave the sandbox or not. Its turn goes on, so it is
    no interrupt. Read the same off 0.164.0's rollout and 0.166.0's, by Codex's adapter."""
    path = tmp_path / "rollout.jsonl"
    path.write_text(CODEX_REVIEW_DENIED[version].read_text(encoding="utf-8"), encoding="utf-8")
    facts = transcript_facts.read_facts([path], since=_at(1, 0), agents={path: "codex"})[path]
    assert _kinds(facts) == [("p", "text"), ("d", ""), ("p", "text"), ("d", "")]


def test_a_denial_by_codexs_reviewer_counts_in_the_weeks_outcomes(
    machine: Machine, codex_home: Path
) -> None:
    """`aisquare outcomes` counts the reviewer's denials with the human's: the recorded
    rollout's first turn (a patch), then 0.166.0's two denied commands in the same session."""
    repo = machine.repo("alpha")
    path = _codex_rollout(codex_home, repo, turns=1)
    denied = CODEX_REVIEW_DENIED["0.166.0"].read_text(encoding="utf-8").splitlines()
    with path.open("a", encoding="utf-8") as handle:
        for n, line in enumerate(denied):
            record = _moved(json.loads(line), {"/work/alpha": str(repo)})
            record["timestamp"] = stamp(_at(27, 12, 1, n))
            handle.write(json.dumps(record) + "\n")
    usage_stats.sweep(now=NOW, tz=IST)

    direction = _report(use_gh=False).direction

    assert (direction.denied_permissions, direction.interrupts) == (2, 0)


def test_a_codex_rollout_read_in_two_goes_finds_what_one_read_finds(tmp_path: Path) -> None:
    """Read incrementally, cut after any line or inside one, the rollout gives the same facts,
    none twice and none missed. A call at its approval is read again until its answer, and its
    refusal until the turn it stopped ends (its abort is no interrupt): the first read stops
    before it."""
    from aisquare.core.agent_adapters import get_adapter

    reader = get_adapter("codex").transcript_reader
    assert reader is not None
    data = CODEX_FACTS.read_bytes()
    whole, end = reader.facts(CODEX_FACTS, 0)
    assert end == len(data) and [(f.kind, f.detail) for f in whole] == CODEX_KINDS
    ends = [index + 1 for index, byte in enumerate(data) if byte == ord("\n")]
    path = tmp_path / "rollout.jsonl"
    for cut in sorted({*ends, *(start + 9 for start in [0, *ends[:-1]])}):
        path.write_bytes(data[:cut])
        first, offset = reader.facts(path, 0)
        path.write_bytes(data)
        rest, final = reader.facts(path, offset)
        assert ([*first, *rest], final) == (list(whole), len(data)), f"cut at byte {cut}"

    asking = next(i for i, line in enumerate(CODEX_LINES) if "require_escalated" in line)
    path.write_bytes(data[: ends[asking]])  # the call written, its approval up
    first, offset = reader.facts(path, 0)
    assert [(f.kind, f.detail) for f in first] == CODEX_KINDS[:3]
    assert offset == (ends[asking - 1] if asking else 0), "read again from the call"


CODEX_PATCH_FAILED = CODEX_FACTS.with_name("codex-patch-failed-0.164.0.jsonl")
"""Codex 0.164.0's own rollout as :data:`CODEX_FACTS` was recorded, ``hello.txt`` made
read-only first: ``PATCH it``, whose write the sandbox refused, retried outside it once its
approval (``command failed; retry without sandbox?``) was answered ``1``, and failed again (its
``FileChange`` ``failed``, the file unchanged); then ``RUN echo POSTBLOCK``, whose PostToolUse
hook answered ``block``: Codex put the reason in the call's output, and wrote no hook prompt."""


def _held(path: Path) -> bool:
    """Whether Codex's reader stops before the end of ``path``, read from its start."""
    from aisquare.core.agent_adapters import get_adapter

    reader = get_adapter("codex").transcript_reader
    assert reader is not None
    return reader.facts(path, 0)[1] != path.stat().st_size


def test_a_codex_rollout_that_ends_right_after_an_applied_patch_reports_its_edit(
    tmp_path: Path,
) -> None:
    """Codex killed mid-turn, its rollout ending on the patch's answer (``Exit code: 0 …
    Success``, a line no fact's mark is in): the edit is counted at once, as Claude Code's
    ``Edit`` is, and the read reaches the end (review of #57, seq 10325, repro A)."""
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(CODEX_LINES[:13]) + "\n", encoding="utf-8")
    facts = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    assert _kinds(facts) == [("p", "text"), ("e", "/work/alpha/hello.txt")]
    cached = json.loads(transcript_facts.facts_cache_path().read_text(encoding="utf-8"))
    assert cached["files"][str(path)]["offset"] == path.stat().st_size


def test_a_codex_call_is_held_only_while_its_approval_or_the_turn_it_refused_decides_it(
    tmp_path: Path,
) -> None:
    """Cut after each line, the read stops early only between a call only its approval can
    hold and its answer (the patch, the escalated command later approved), and between a
    refused call and the end of the turn it stopped. An answered call is held no longer:
    before, every patch and escalated call was held to the end of its turn."""
    calls = {
        record["payload"]["call_id"]: index
        for index, record in enumerate(json.loads(line) for line in CODEX_LINES)
        if record["payload"].get("type") in ("custom_tool_call", "function_call")
    }
    answered = {
        record["payload"]["call_id"]: index
        for index, record in enumerate(json.loads(line) for line in CODEX_LINES)
        if str(record["payload"].get("type")).endswith("_call_output")
    }
    aborted = [i for i, line in enumerate(CODEX_LINES) if '"type":"turn_aborted"' in line]
    patch, refused, sleep, approved = sorted(calls, key=calls.__getitem__)
    held = {
        *range(calls[patch], answered[patch]),
        *range(calls[refused], aborted[0]),  # its answer a refusal: held to its turn's abort
        *range(calls[approved], answered[approved]),
    }
    assert calls[sleep] not in held, "a sandboxed command is never held"
    path = tmp_path / "rollout.jsonl"
    found = set()
    for last in range(len(CODEX_LINES)):
        path.write_text("\n".join(CODEX_LINES[: last + 1]) + "\n", encoding="utf-8")
        if _held(path):
            found.add(last)
    assert found == held


def test_an_open_codex_turn_after_an_applied_patch_is_read_on_from_where_it_got_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn still open after its patch was applied, and 64 MB of its output after it: the
    first read reaches the end, and the next reads what was appended alone, not the turn again
    from its patch (review of #57, seq 10325, repro B; ``tests/budgets.py``)."""
    import time

    from aisquare.core.agent_adapters import codex_transcript
    from tests.budgets import budget

    path = tmp_path / "rollout.jsonl"
    pad = {"type": "function_call_output", "call_id": "c_pad", "output": "x" * 65536}
    padding = json.dumps(
        {"timestamp": "2026-10-27T06:30:00.000Z", "type": "response_item", "payload": pad}
    )
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(CODEX_LINES[:13]) + "\n")
        while handle.tell() < 64 * 1024 * 1024:
            handle.write(padding + "\n")
    asked: list[int] = []
    read = codex_transcript._rollout_facts

    def spied(file: Path, start: int) -> Any:
        asked.append(start)
        return read(file, start)

    monkeypatch.setattr(codex_transcript, "_rollout_facts", spied)
    size = path.stat().st_size
    first = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(padding + "\n")
    began = time.perf_counter()
    again = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    appended = time.perf_counter() - began

    assert _kinds(first) == _kinds(again) == [("p", "text"), ("e", "/work/alpha/hello.txt")]
    assert asked == [0, size], "the second read began where the first ended"
    assert appended < budget(0.5), f"a line appended to an open turn read in {appended:.2f} s"


def test_a_codex_turn_the_fleets_nudge_started_is_an_agents_wake(
    machine: Machine, codex_home: Path
) -> None:
    """A Codex turn from before origins were recorded has its origin inferred from its prompt
    as a Claude Code one's is: the fleet's nudge is an agent's wake, not a person's words."""
    repo = machine.repo("alpha")
    _codex_rollout(codex_home, repo, turns=1, said={"PATCH it": fleet_service.NUDGE_TEXT})
    usage_stats.sweep(now=NOW, tz=IST)
    d = _report(use_gh=False).direction
    assert d.by_origin == {"agent:wake": 1}
    assert d.inferred_turns == 1 and d.human_turns == 0


def test_a_codex_patch_that_failed_to_apply_is_no_edit_nor_a_post_tool_use_block_a_turn(
    tmp_path: Path,
) -> None:
    """Codex's ``FileChange`` of a patch it could not write is ``failed``, the file unchanged: no
    file was edited. A PostToolUse hook's ``block`` answers its call with the reason and writes
    no hook prompt: no continuation."""
    path = tmp_path / "rollout.jsonl"
    path.write_text(CODEX_PATCH_FAILED.read_text(encoding="utf-8"), encoding="utf-8")
    facts = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    assert _kinds(facts) == [("p", "text"), ("p", "text")]
    assert not _held(path)


def test_only_a_stop_hooks_prompt_is_a_codex_continuation(tmp_path: Path) -> None:
    """The recorded Stop hook's ``<hook_prompt hook_run_id="stop:…">``, its run id made another
    event's: no continuation, and no prompt of a person's either. (No other hook of 0.164.0's
    writes one: a PostToolUse ``block`` answers its call, :data:`CODEX_PATCH_FAILED`.)"""
    path = tmp_path / "rollout.jsonl"
    swap = {'hook_run_id="stop:': 'hook_run_id="session_start:'}
    lines = [json.dumps(_moved(json.loads(line), swap)) for line in CODEX_LINES]
    assert sum('hook_run_id=\\"session_start:' in line for line in lines) >= 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    facts = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    assert _kinds(facts) == [kind for kind in CODEX_KINDS if kind != ("c", "")]


def test_a_rollout_main_read_as_claude_codes_is_read_again_by_codexs_reader(
    tmp_path: Path,
) -> None:
    """Main's scan (Claude Code's reader) cached a rollout at its whole length with no fact and
    no agent; Codex's reader reads it again from its start. An entry main cached for a Claude
    Code transcript is its reader's still: read on from its offset, its facts kept."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(CODEX_FACTS.read_text(encoding="utf-8"), encoding="utf-8")
    claude = write(
        tmp_path / "s.jsonl",
        prompt(_at(26, 10), cwd=tmp_path, text="one"),
        prompt(_at(26, 11), cwd=tmp_path, text="two"),
    )
    kept = [stamp(_at(26, 10)), "p", "text"]
    cache = {
        "version": transcript_facts.CACHE_VERSION,
        "files": {
            str(rollout): {"offset": rollout.stat().st_size, "facts": []},
            str(claude): {"offset": claude.stat().st_size, "facts": [kept]},
        },
    }
    transcript_facts.facts_cache_path().parent.mkdir(parents=True, exist_ok=True)
    transcript_facts.facts_cache_path().write_text(json.dumps(cache), encoding="utf-8")

    found = transcript_facts.read_facts(
        [rollout, claude], since=_at(19, 0), agents={rollout: "codex", claude: None}
    )

    assert _kinds(found[rollout]) == CODEX_KINDS
    assert _kinds(found[claude]) == [("p", "text")], "not read again: the cached fact alone"
    entries = json.loads(transcript_facts.facts_cache_path().read_text(encoding="utf-8"))["files"]
    assert entries[str(rollout)]["agent"] == "codex"


def test_a_large_codex_rollout_is_read_once_and_then_from_where_it_got_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """64 MB of rollout is read whole once, by its markers' bytes (a quarter of a second here),
    and a turn appended to it is read alone: the reader is asked from the offset the first read
    reached, never from the start (``tests/budgets.py``)."""
    import time

    from aisquare.core.agent_adapters import codex_transcript
    from tests.budgets import budget

    path = tmp_path / "rollout.jsonl"
    pad = {"type": "function_call_output", "call_id": "c_pad", "output": "x" * 65536}
    padding = json.dumps(
        {"timestamp": "2026-10-27T06:30:00.000Z", "type": "response_item", "payload": pad}
    )
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(CODEX_LINES) + "\n")
        while handle.tell() < 64 * 1024 * 1024:
            handle.write(padding + "\n")
    asked: list[int] = []
    read = codex_transcript._rollout_facts

    def spied(file: Path, start: int) -> Any:
        asked.append(start)
        return read(file, start)

    monkeypatch.setattr(codex_transcript, "_rollout_facts", spied)
    size = path.stat().st_size

    began = time.perf_counter()
    first = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    whole = time.perf_counter() - began
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(CODEX_LINES[1:19]) + "\n")  # the patch's turn again
    began = time.perf_counter()
    again = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    appended = time.perf_counter() - began

    assert _kinds(first) == CODEX_KINDS
    added = [("p", "text"), ("e", "/work/alpha/hello.txt")]
    assert Counter(_kinds(again)) == Counter([*CODEX_KINDS, *added])
    assert asked == [0, size], "the second read began where the first ended"
    assert whole < budget(5.0), f"64 MB read in {whole:.2f} s"
    assert appended < budget(0.5), f"a turn appended read in {appended:.2f} s"


# --- Codex 0.166.0: code mode, and its reviewer's rollouts (SMOKE12 F2, F3) ----------------------

CODEX_CODE_MODE = CODEX_FACTS.with_name("codex-code-mode-0.166.0.jsonl")
"""Codex 0.166.0's own rollout (SMOKE12 item 8, sanitized: a fleet coder on GPT-6.1-Sol with
``--permission-mode workspace:ask``, which calls every tool through the code-mode ``exec``
script): its kickoff, one script that patches two files, a ``git commit && git push`` script
asking to leave the sandbox (``sandbox_permissions:"require_escalated"`` in its JavaScript)
refused with ``3. No``, then ``sleep 40`` stopped with Esc, then a docstring patched."""
CODE_MODE_KINDS = [
    ("p", "text"),
    ("e", "/work/alpha/tests/test_calc.py"),
    ("e", "/work/alpha/calc/__init__.py"),
    ("d", ""),  # the escalated script refused: its turn's abort is no interrupt
    ("p", "text"),
    ("i", ""),  # Esc after a script that asked nothing: an interrupt
    ("p", "text"),
    ("e", "/work/alpha/calc/__init__.py"),
]
CODEX_GUARDED = CODEX_FACTS.with_name("codex-guardian-parent-0.166.0.jsonl")
"""Codex 0.166.0's own rollout (SMOKE12 item 2, sanitized): a fleet coder run with
``--approve-for-me``, its kickoff, a patch and a push outside its worktree asking to leave the
sandbox, which the auto-reviewer allowed."""
CODEX_GUARDIAN = CODEX_FACTS.with_name("codex-guardian-0.166.0.jsonl")
"""That review's own rollout, beside the coder's: ``session_meta`` ``source`` ``{"subagent":
{"other": "guardian"}}``, ``thread_source`` ``guardian_review``, ``parent_thread_id`` the coder's
thread; its review request a ``user`` message, one ``token_usage_record``."""
GUARDED_THREAD = "01a1a450-987b-7000-8000-00000000c0d1"
GUARDIAN_THREAD = "01a1a450-989d-7000-8000-0000000a0d1f"


def test_a_code_mode_script_refused_at_its_approval_is_a_denial_not_an_interrupt(
    tmp_path: Path,
) -> None:
    """SMOKE12 F3: a script asking to leave the sandbox, its approval answered ``3. No``, is a
    refusal, and the turn it stopped no interrupt; an Esc after a script that asked nothing is
    one. Before, ``sandbox_permissions`` was read in a function call's JSON alone, and the
    refusal read as a second interrupt (interrupts 2, denied 0)."""
    path = tmp_path / "rollout.jsonl"
    path.write_text(CODEX_CODE_MODE.read_text(encoding="utf-8"), encoding="utf-8")
    facts = transcript_facts.read_facts([path], since=_at(19, 0), agents={path: "codex"})[path]
    assert _kinds(facts) == CODE_MODE_KINDS


def test_a_code_mode_rollout_read_in_two_goes_finds_what_one_read_finds(tmp_path: Path) -> None:
    """Cut after any line, the 0.166.0 rollout gives the same facts: its escalated script is
    held until its answer, and its refusal until the turn it stopped ends."""
    from aisquare.core.agent_adapters import get_adapter

    reader = get_adapter("codex").transcript_reader
    assert reader is not None
    data = CODEX_CODE_MODE.read_bytes()
    whole, end = reader.facts(CODEX_CODE_MODE, 0)
    assert end == len(data) and [(f.kind, f.detail) for f in whole] == CODE_MODE_KINDS
    path = tmp_path / "rollout.jsonl"
    for cut in [index + 1 for index, byte in enumerate(data) if byte == ord("\n")]:
        path.write_bytes(data[:cut])
        first, offset = reader.facts(path, 0)
        path.write_bytes(data)
        rest, final = reader.facts(path, offset)
        assert ([*first, *rest], final) == (list(whole), len(data)), f"cut at byte {cut}"


_ESCALATED = 'sandbox_permissions:"require_escalated"'
_PATCH = 'await tools.apply_patch("*** Begin Patch\\n*** Add File: /w/a\\n+a\\n*** End Patch");'


@pytest.mark.parametrize(
    ("script", "asks"),
    [
        (f'text(await tools.exec_command({{cmd:"git push",{_ESCALATED}}}));', True),
        ("await tools.exec_command({'sandbox_permissions': 'require_escalated', cmd: 'x'});", True),
        ('await tools.exec_command({cmd:"rm -rf b", with_escalated_permissions: true});', True),
        (
            f'tools.exec_command({{cmd:"ls"}}); tools.exec_command({{cmd:"x", {_ESCALATED}}});',
            True,
        ),
        (_PATCH, True),
        (f'{_PATCH} text(await tools.exec_command({{cmd:"pytest"}}));', False),
        (
            'tools.exec_command({cmd:"grep \\"sandbox_permissions: \'require_escalated\'\\" s"});',
            False,
        ),
        (f'// {_ESCALATED}\nawait tools.exec_command({{cmd:"ls"}});', False),
        ('await tools.exec_command({cmd:"ls", sandbox_permissions:"use_default"});', False),
        ('text(await tools.exec_command({cmd:"apply_patch is a word here"}));', False),
        # One left open ends where JavaScript stops reading it (r37 finding 12): its words
        # before it still ask, and those it swallows ask nothing, as the script cannot run.
        (f'tools.exec_command({{cmd:"x", {_ESCALATED}}}); /* left open', True),
        (f'tools.exec_command({{cmd:"ls"}}); /* {_ESCALATED}', False),
        (f'tools.exec_command({{cmd:"ls"}}); `{_ESCALATED}', False),
        (f"tools.exec_command({{cmd:'ls'}}); 'left open {_ESCALATED}", False),
        ('x = "a string its line ends\n' + _PATCH, True),
        (f"tools.exec_command({{cmd:'x', {_ESCALATED}}}); 'a lone backslash last\\", True),
        # The limit _SCRIPT_WORDS names (#70's reopen, seq 10546): a regular expression
        # literal is not read as one, so one holding an opener swallows what follows, though
        # the script runs. Should regex literals be read, these flip, and the limit goes.
        ('s = t.replace(/`/g, "");\n' + f'tools.exec_command({{cmd:"x", {_ESCALATED}}});', False),
        (
            's = t.replace(/^\\/*/, "");\n' + f'tools.exec_command({{cmd:"x", {_ESCALATED}}});',
            False,
        ),
        (f's = t.replace(/\'/g, ""); tools.exec_command({{cmd:"x", {_ESCALATED}}});', False),
        (f's = t.replace(/a/g, ""); tools.exec_command({{cmd:"x", {_ESCALATED}}});', True),
        (f'{_PATCH} s.replace(/`/g, ""); tools.exec_command({{cmd:"pytest"}});', True),
    ],
)
def test_a_code_mode_script_asks_first_by_its_own_fields_never_by_a_commands_words(
    script: str, asks: bool
) -> None:
    """What holds a script at its approval is read off its own words: an object it passes
    asking to leave the sandbox (keys bare or quoted), or the patch tool as the one tool it
    calls. A command's text, a patch's and a comment that hold those words ask nothing, nor
    does a patch that rides with a command (an Esc while the command ran is the likelier)."""
    from aisquare.core.agent_adapters import codex_transcript

    call = {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": script}
    assert codex_transcript._asks_first(call) is asks


@pytest.mark.parametrize(
    "opener",
    ["/* ", "\\`", '\\"', "\\'", 'sandbox_permissions:\\"', "`${", "tools "],
    ids=["comment", "template", "double", "single", "key", "placeholder", "tools"],
)
def test_a_code_mode_script_is_read_in_time_linear_in_its_length(opener: str) -> None:
    """r37 finding 12: an opener that never closed was scanned to the end and given up on,
    and the next one scanned it again, so the script's read was quadratic in the text the
    model writes (60 KB of unclosed ``/*`` took 2.5 s; 30 KB of ``\\"`` 1.5 s). 200 KB of
    each shape is read in one pass, well under a budget the quadratic read missed by
    minutes."""
    from aisquare.core.agent_adapters import codex_transcript

    script = opener * (200_000 // len(opener))
    started = time.perf_counter()
    codex_transcript._script_asks_first(script)
    took = time.perf_counter() - started
    assert took < 0.25, f"{len(script)} chars of {opener!r} took {took:.2f} s"


def _rollout_at(source: Path, home: Path, cwd: Path, swaps: dict[str, str]) -> Path:
    """The recorded 0.166.0 rollout ``source`` as Codex writes it in ``home``, run in ``cwd``,
    each of ``swaps`` made (a thread id another), moved to begin on Tuesday 27 Oct 12:00 IST
    as :func:`_codex_rollout`'s is, the review rollouts by the same shift as their sessions'."""
    lines = source.read_text(encoding="utf-8").splitlines()
    first = datetime.fromisoformat(
        json.loads(CODEX_GUARDED.read_text(encoding="utf-8").splitlines()[0])["timestamp"].replace(
            "Z", "+00:00"
        )
    )
    shift = _at(27, 12) - first
    thread = json.loads(lines[0])["payload"]["id"]
    named = swaps.get(thread, thread)
    path = home / "sessions" / "2026" / "10" / "27" / f"rollout-2026-10-27T06-30-00-{named}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            record = json.loads(line)
            moment = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")) + shift
            record["timestamp"] = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            handle.write(json.dumps(_moved(record, {"/work/alpha": str(cwd), **swaps})) + "\n")
    return path


def test_a_codex_reviewers_rollouts_are_no_sessions_and_its_requests_no_ones_turns(
    machine: Machine, codex_home: Path
) -> None:
    """SMOKE12 F2's acceptance: five Codex sessions run with ``--approve-for-me``, two of whom
    asked its reviewer once, each review a rollout of its own beside its session's. The week
    counts five sessions, the five kickoffs as their turns and no review request as anyone's,
    and each review's tokens once, in its session. Before, each review was a session of its own
    and its request a person's typed prompt: seven sessions and seven typed turns."""
    from aisquare.core.agent_adapters import get_adapter

    repo = machine.repo("alpha")
    threads = [f"01a1a450-987b-7000-8000-0000000000{n:02d}" for n in range(5)]
    sessions = [
        _rollout_at(CODEX_GUARDED, codex_home, repo, {GUARDED_THREAD: thread}) for thread in threads
    ]
    reviews = [
        _rollout_at(
            CODEX_GUARDIAN,
            codex_home,
            repo,
            {
                GUARDED_THREAD: threads[n],
                GUARDIAN_THREAD: f"01a1a450-989d-7000-8000-0000000a0d{n:02d}",
            },
        )
        for n in (0, 3)
    ]
    reader = get_adapter("codex").transcript_reader
    assert reader is not None
    assert [reader.parent_session(path) for path in reviews] == [threads[0], threads[3]]

    usage_stats.sweep(now=NOW, tz=IST)
    result = _report(use_gh=False)

    def tokens(path: Path) -> int:
        scan = reader.usage(path, None)
        assert scan is not None
        return scan.delta.totals().total

    d, effort = result.direction, result.effort
    assert (d.sessions, effort.sessions) == (5, 5)
    assert d.by_origin == {"human:typed": 5}, "the kickoffs alone: no review request is a turn"
    assert d.inferred_turns == 5 and d.human_turns == 5
    assert effort.tokens.total == sum(map(tokens, sessions)) + sum(map(tokens, reviews))


# --- recording who started a turn --------------------------------------------------------------


@pytest.fixture
def as_human(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDECODE", "AISQUARE_FLEET_AGENT"):
        monkeypatch.delenv(name, raising=False)


def test_a_marker_names_its_text_whitespace_folded_and_is_taken_once(as_human: None) -> None:
    prompt_origin.mark_origin("start on\n  tsk_1 ", prompt_origin.TELL)
    assert prompt_origin.take_origin("start on tsk_1") == "human:tell"
    assert prompt_origin.take_origin("start on tsk_1") is None, "taken once"
    assert prompt_origin.origin_of_prompt("start on tsk_1") == "human:typed"


def test_a_marker_says_agent_inside_an_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    prompt_origin.mark_origin("go", prompt_origin.TELL)
    assert prompt_origin.take_origin("go") == "agent:tell"
    with prompt_origin.sent_by(prompt_origin.HUMAN):
        prompt_origin.mark_origin("from the phone", prompt_origin.TELL)
    assert prompt_origin.take_origin("from the phone") == "human:tell"


def test_a_stale_marker_is_ignored(as_human: None) -> None:
    prompt_origin.mark_origin(
        "late", prompt_origin.WAKE, now=datetime.now(UTC) - timedelta(hours=1)
    )
    assert prompt_origin.take_origin("late") is None


def test_a_queued_marker_whose_holder_is_not_known_waits_for_its_bound(as_human: None) -> None:
    """A text a busy agent's queue holds waits for the turn it starts past ``MAX_AGE``: with no
    process known to hold the queue (tmux did not say), up to ``QUEUED_MAX_AGE`` alone. A
    typed text's marker is gone at ``MAX_AGE``, as before."""
    then = datetime.now(UTC) - timedelta(hours=3)
    prompt_origin.mark_origin("queued", prompt_origin.TELL, now=then, queued=True)
    prompt_origin.mark_origin("typed", prompt_origin.TELL, now=then)
    assert prompt_origin.take_origin("typed") is None
    assert prompt_origin.take_origin("queued") == "human:tell"

    prompt_origin.mark_origin("queued", prompt_origin.TELL, now=then, queued=True)
    past = then + prompt_origin.QUEUED_MAX_AGE + timedelta(seconds=1)
    assert prompt_origin.take_origin("queued", now=past) is None


SLASH_COMMAND = (
    "<command-name>/model</command-name><command-message>model</command-message>"
    "<command-args>sonnet</command-args>"
)


def test_only_a_task_notification_is_claude_codes_notice_and_a_slash_command_a_persons() -> None:
    notice = "<task-notification><task-id>x</task-id></task-notification>"
    assert prompt_origin.only_task_notifications(notice)
    assert not prompt_origin.only_task_notifications(notice + " and also fix it")
    assert prompt_origin.only_harness_blocks(SLASH_COMMAND)
    assert not prompt_origin.only_task_notifications(SLASH_COMMAND), "a person typed /model"
    assert prompt_origin.origin_of_prompt(notice, notice=True) == "agent:notice"


@pytest.mark.parametrize("kind", ["wake", "upkeep", "continuation", "notice"])
def test_what_no_person_writes_is_an_agents_whoever_sends_it(as_human: None, kind: str) -> None:
    """The ``aisquare watch`` daemon, asq and a person's ``task done`` send wakes from no agent's
    environment: the wake is still the agent's (review of 8ad337d7, 1)."""
    prompt_origin.mark_origin("aisquare: board update", kind)
    assert prompt_origin.take_origin("aisquare: board update") == f"agent:{kind}"
    prompt_origin.mark_origin("by hand", kind, by=prompt_origin.HUMAN)
    assert prompt_origin.take_origin("by hand") == f"agent:{kind}"
    assert prompt_origin.authored(f"human:{kind}") == f"agent:{kind}"
    assert prompt_origin.authored("human:tell") == "human:tell"


def test_a_spawns_first_message_is_its_spawners(monkeypatch: pytest.MonkeyPatch) -> None:
    """A person's spawn (``user``, from no agent's environment) is theirs; an agent session's,
    by its ``--as`` or its environment, and aisquare's own (``usage-limit``) are an agent's."""
    from aisquare.services import delivery

    for name in ("CLAUDECODE", "AISQUARE_FLEET_AGENT"):
        monkeypatch.delenv(name, raising=False)
    assert fleet_service._spawn_author("user") == "human"
    assert fleet_service._spawn_author("ses-manager") == "agent"
    assert fleet_service._spawn_author("usage-limit") == "agent"
    with prompt_origin.sent_by(fleet_service._spawn_author("ses-manager")):
        delivery.write_prompt_file("agt_01y", "You are a coder.")
    assert prompt_origin.take_origin("You are a coder.") == "agent:spawn"
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_manager")
    assert fleet_service._spawn_author("user") == "agent", "a fleet agent's own shell"


def test_a_spawns_first_message_file_leaves_a_spawn_marker(as_human: None) -> None:
    from aisquare.services import delivery

    delivery.write_prompt_file("agt_01x", "You are a coder. Claim task 3.")
    assert prompt_origin.take_origin("You are a coder. Claim task 3.") == "human:spawn"


def test_a_typed_tell_leaves_a_marker_and_a_refused_one_none(
    clock: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import delivery
    from tests.test_first_prompt_delivery import ClaudeCode, _claude_pane

    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_manager")
    fake = ClaudeCode()
    pane = _claude_pane(fake, tmp_path)
    seen: list[str | None] = []
    fake.on_submit = lambda _pane: seen.append(prompt_origin.take_origin("fix the tests"))
    done = delivery.deliver_typed(
        fake,
        pane,
        "fix the tests",
        "coder-1",
        press="press Enter",
        acked=lambda: bool(seen),
        reads_box=True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        origin=prompt_origin.TELL,
    )
    assert done.told.typed == "yes" and seen == ["agent:tell"]

    def refuse(_pane: str, _text: str) -> None:
        from aisquare.core.tmux import TmuxError

        raise TmuxError("no server")

    monkeypatch.setattr(fake, "paste", refuse)
    delivery.deliver_typed(
        fake,
        pane,
        "never pasted",
        "coder-1",
        press="press Enter",
        acked=None,
        reads_box=True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        origin=prompt_origin.TELL,
    )
    assert prompt_origin.take_origin("never pasted") is None, (
        "a paste tmux refused leaves no marker"
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.test_fleet_service import FakeClock

    fake = FakeClock()
    monkeypatch.setattr(fleet_service, "_sleep", fake.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", fake.monotonic)
    return fake


def _rows(session_id: str) -> list[TurnMetric]:
    from aisquare.services import metrics as metrics_service

    return sorted(metrics_service.recent(session_id=session_id), key=lambda t: t.started_at)


def test_the_prompt_hook_records_the_origin_of_the_turn(tmp_path: Path, as_human: None) -> None:
    from aisquare.services import hooks as hooks_service
    from tests.projects import add_project

    add_project(tmp_path)
    hooks_service.capture_prompt("fix the flaky test", tmp_path, session_id="ses-o")
    prompt_origin.mark_origin("board news", prompt_origin.WAKE, by=prompt_origin.AGENT)
    hooks_service.capture_prompt("board news", tmp_path, session_id="ses-o")
    hooks_service.capture_prompt(
        "<task-notification><task-id>b</task-id></task-notification>", tmp_path, session_id="ses-o"
    )
    assert [row.origin for row in _rows("ses-o")] == ["human:typed", "agent:wake", "agent:notice"]


def test_a_stop_hook_that_keeps_the_turn_going_opens_a_continuation_turn(
    tmp_path: Path, as_human: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import hooks as hooks_service
    from aisquare.services import team as team_service
    from tests.projects import add_project

    add_project(tmp_path)
    hooks_service.capture_prompt("run the fleet", tmp_path, session_id="ses-m")
    decisions = [team_service.StopDecision(reason="2 board updates", cursor=7), None]
    monkeypatch.setattr(team_service, "hook_stop", lambda *a, **k: decisions.pop(0))
    assert hooks_service.turn_stopped(tmp_path, session_id="ses-m") is not None
    assert hooks_service.turn_stopped(tmp_path, session_id="ses-m") is None
    rows = _rows("ses-m")
    assert [row.origin for row in rows] == ["human:typed", "agent:continuation"]
    assert all(row.ended_at is not None for row in rows), "each closed by its own Stop"
    assert [row.client_reason for row in rows] == [ClientReason.disabled] * 2, "both baseline"


@pytest.mark.parametrize(
    ("before", "tagged"),
    [
        (ClientReason.not_configured, ClientReason.not_configured),
        (ClientReason.none, ClientReason.no_prompt),
        (ClientReason.trigger_not_in_descriptor, ClientReason.no_prompt),
        (ClientReason.transport_error, ClientReason.no_prompt),
    ],
)
def test_a_continuation_is_tagged_in_its_turns_arm_never_baseline_after_a_consulted_one(
    tmp_path: Path,
    as_human: None,
    monkeypatch: pytest.MonkeyPatch,
    before: ClientReason,
    tagged: ClientReason,
) -> None:
    """REVIEW-RC092-MI round 2, minor 2: with the CI experiment on, a consulted turn that
    ``hook_stop`` kept going opened an ``agent:continuation`` row tagged ``disabled``, a
    baseline reason, and ``summarize`` counted it as control data beside the consulted turn.
    The continuation asks the CI nothing: ``no_prompt`` (by design) unless the experiment was
    off for the turn before, whose baseline reason it keeps."""
    from aisquare.services import hooks as hooks_service
    from aisquare.services import metrics as metrics_service
    from aisquare.services import team as team_service
    from tests.projects import add_project

    project = add_project(tmp_path)
    turn = metrics_service.baseline_turn(project.id, session_id="ses-x", reason=before)
    assert metrics_service.open_turn(turn) is not None
    decision = team_service.StopDecision(reason="2 board updates", cursor=7)
    monkeypatch.setattr(team_service, "hook_stop", lambda *a, **k: decision)
    hooks_service.turn_stopped(tmp_path, session_id="ses-x")
    rows = _rows("ses-x")
    assert [(row.origin, row.client_reason) for row in rows][-1] == (
        "agent:continuation",
        tagged,
    )
    summary = metrics_service.summarize(rows)
    assert summary.baseline == (2 if before in BASELINE_REASONS else 0), "the arms never mix"


def test_a_repository_is_named_in_its_remotes_own_case(machine: Machine) -> None:
    """The profile's remote is lower case (it groups clones); the name is read off the URL a
    checkout has, so it keeps the remote's spelling, whatever the folder is called."""
    repo = machine.repo("clone-7", remote="git@github.com:Acme/WidgetApp.git")
    machine.session(
        1,
        repo,
        S_PLAIN,
        prompt(_at(26, 10), cwd=repo, text="go"),
        request(_at(26, 10, 1), "w1", cwd=repo),
    )
    usage_stats.sweep(now=NOW, tz=IST)
    [row] = _report(use_gh=False).outcomes.repos
    assert (row.name, row.github, row.key) == (
        "WidgetApp",
        "acme/widgetapp",
        "github.com/acme/widgetapp",
    )


def test_a_local_path_remote_never_names_a_repository(machine: Machine) -> None:
    """A remote that is a local path names no repository: the checkout whose ``origin`` is
    ``/srv/git/x.git`` and whose other remote is GitHub's is named by GitHub's, as its profile
    is, not ``x.git``; one with only a local path is named by its folder."""
    mirrored = machine.repo("x-checkout", remote="/srv/git/x.git")
    subprocess.run(
        ["git", "-C", str(mirrored), "remote", "add", "github", "git@github.com:Acme/X.git"],
        check=True,
    )
    local = machine.repo("tool-checkout", remote="/srv/git/tool.git")
    for slot, (repo, session_id) in enumerate(((mirrored, S_MGR), (local, S_COD)), 1):
        machine.session(
            slot,
            repo,
            session_id,
            prompt(_at(26, 10), cwd=repo, text="go"),
            request(_at(26, 10, 1), f"w{slot}", cwd=repo),
        )
    usage_stats.sweep(now=NOW, tz=IST)
    names = {row.name: row.key for row in _report(use_gh=False).outcomes.repos}
    assert names.keys() == {"X", "tool-checkout"}
    assert names["X"] == "github.com/acme/x"


# --- the reopen of 8ad337d7 --------------------------------------------------------------------


def _solo_project(machine: Machine, name: str) -> tuple[Path, str]:
    repo = machine.repo(name)
    found = usage_stats.repo_of(repo)
    assert found is not None
    with store_session() as store:
        store.ensure_project(ProjectInfo(id=found[0], root=repo))
    return repo, found[0]


def test_a_recorded_wake_is_an_agents_whatever_its_sender_wrote(machine: Machine) -> None:
    """Review of 8ad337d7, 1, repro B: one person's prompt, three wakes and a compaction line,
    as a sender-trusting build recorded them (``human:wake``…): one human turn of five, and the
    session is directive."""
    _turn("s-wakes", "prj_w", _at(26, 9), _at(26, 9, 10), origin="human:typed")
    for hour in (10, 11, 12):
        _turn("s-wakes", "prj_w", _at(26, hour), _at(26, hour, 5), origin="human:wake")
    _turn("s-wakes", "prj_w", _at(26, 13), _at(26, 13, 5), origin="human:upkeep")
    d = _report(use_gh=False).direction
    assert (d.turns, d.human_turns) == (5, 1)
    assert d.by_origin == {"agent:wake": 3, "agent:upkeep": 1, "human:typed": 1}
    assert (d.directive, d.guided, d.collaborative) == (1, 0, 0)


def test_an_older_spawn_is_inferred_from_who_the_row_says_spawned_it(machine: Machine) -> None:
    """The same rule as recording: the row's ``spawned_by`` is ``user`` (a person: shell, asq,
    phone) or a session (an agent). Inferred, and counted as inferred."""
    repo, pid = _solo_project(machine, "spawns")
    for session_id, label, spawner in (
        (S_COD, "coder-mine", "user"),
        (S_PLAIN, "coder-managers", "ses-manager-1"),
    ):
        machine.session(
            1,
            repo,
            session_id,
            prompt(_at(26, 10), cwd=repo, text=f"Claim the task, {label}."),
            request(_at(26, 10, 1), f"{label}-1", cwd=repo),
        )
        _fleet(session_id, pid, "coder", repo, label=label, spawned_by=spawner)
        _turn(session_id, pid, _at(26, 10), _at(26, 10, 30))
    usage_stats.sweep(now=NOW, tz=IST)
    d = _report(use_gh=False).direction
    assert d.by_origin == {"human:spawn": 1, "agent:spawn": 1}
    assert d.inferred_turns == 2 and d.human_turns == 1


def test_a_slash_command_is_a_persons_even_in_a_workers_pane(
    machine: Machine, tmp_path: Path, as_human: None
) -> None:
    """``/model`` is all harness blocks, but a person typed it: not Claude Code's notice, and not
    a manager's tell either (review of 8ad337d7, 5). Recorded the same way."""
    from aisquare.services import hooks as hooks_service
    from tests.projects import add_project

    repo, pid = _solo_project(machine, "slash")
    machine.session(
        1,
        repo,
        S_COD,
        prompt(_at(26, 10), cwd=repo, text="Claim task 1."),
        request(_at(26, 10, 1), "s1", cwd=repo),
        prompt(_at(26, 11), cwd=repo, text=SLASH_COMMAND),
        request(_at(26, 11, 1), "s2", cwd=repo),
        prompt(_at(26, 12), cwd=repo, text="<task-notification><id>b</id></task-notification>"),
        request(_at(26, 12, 1), "s3", cwd=repo),
    )
    _fleet(S_COD, pid, "coder", repo, spawned_by="ses-manager-1")
    for hour in (10, 11, 12):
        _turn(S_COD, pid, _at(26, hour), _at(26, hour, 30))
    usage_stats.sweep(now=NOW, tz=IST)
    d = _report(use_gh=False).direction
    assert d.by_origin == {"agent:spawn": 1, "human:typed": 1, "agent:notice": 1}

    add_project(tmp_path)  # onboarded: a hook in a project nobody added records nothing
    hooks_service.capture_prompt(SLASH_COMMAND, tmp_path, session_id="ses-slash")
    assert [row.origin for row in _rows("ses-slash")] == ["human:typed"]


NEW_YORK = ZoneInfo("America/New_York")
"""Its clocks went back on Sunday 1 Nov 2026, 02:00 EDT: 2026-W44 is 169 hours long."""


def test_a_whole_week_is_compared_with_the_whole_week_before_across_a_clock_change(
    machine: Machine,
) -> None:
    """Review of 8ad337d7, 3: W45 against W44, all of it, though W44 is an hour longer."""
    now = datetime(2026, 11, 10, 12, 0, tzinfo=NEW_YORK)
    result = outcomes.week_report("2026-W45", now=now, tz=NEW_YORK, use_gh=False)
    assert result.compared_since == datetime(2026, 10, 26, 0, 0, tzinfo=NEW_YORK)
    assert result.compared_until == datetime(2026, 11, 2, 0, 0, tzinfo=NEW_YORK)
    elapsed = result.compared_until.astimezone(UTC) - result.compared_since.astimezone(UTC)
    assert elapsed == timedelta(days=7, hours=1)


def test_a_week_in_progress_is_compared_at_the_same_local_weekday_and_time(
    machine: Machine,
) -> None:
    """Sunday 12:00 EST is compared with the Sunday before at 12:00 EDT, not 13:00."""
    now = datetime(2026, 11, 1, 12, 0, tzinfo=NEW_YORK)
    result = outcomes.week_report(None, now=now, tz=NEW_YORK, use_gh=False)
    assert result.week == "2026-W44" and result.in_progress
    until = result.compared_until
    assert until is not None
    local = until.astimezone(NEW_YORK)
    assert (local.date(), local.hour, local.minute) == (datetime(2026, 10, 25).date(), 12, 0)
    assert local.utcoffset() == timedelta(hours=-4)


def test_the_usage_sweep_fills_the_facts_cache_so_the_report_reads_it_warm(
    machine: Machine,
) -> None:
    """Review of 8ad337d7, 4: the sweep reads the transcripts anyway, so it brings the facts
    cache to their ends; the report then has nothing left to read."""
    repo = machine.repo("warm")
    path = machine.session(
        1,
        repo,
        S_PLAIN,
        prompt(_at(26, 10), cwd=repo, text="go"),
        request(_at(26, 10, 1), "w1", cwd=repo),
    )
    usage_stats.sweep(now=datetime.now(UTC), tz=IST)
    cached = json.loads(transcript_facts.facts_cache_path().read_text(encoding="utf-8"))
    assert cached["files"][str(path)]["offset"] == path.stat().st_size
    assert cached["files"][str(path)]["facts"] == [[stamp(_at(26, 10)), "p", "text"]]


# --- the reopen of d09a5940: one marker per write ----------------------------------------------


def test_two_wakes_waiting_at_once_are_two_agent_wakes(as_human: None) -> None:
    """The watcher's wake and a teammate's ``task done`` wake land within seconds: the same text,
    one marker each, so neither turn reads as a person's (review of d09a5940, 1)."""
    text = fleet_service.NUDGE_TEXT
    prompt_origin.mark_origin(text, prompt_origin.WAKE)
    prompt_origin.mark_origin(text, prompt_origin.WAKE)
    assert [prompt_origin.origin_of_prompt(text) for _ in range(2)] == ["agent:wake"] * 2
    assert not list(prompt_origin.origin_markers_dir().iterdir()), "both markers taken"


def test_a_wake_with_no_marker_is_still_an_agents(as_human: None) -> None:
    """Its marker gone stale or taken: the nudge's words are no person's, as inferred too."""
    assert prompt_origin.origin_of_prompt(fleet_service.NUDGE_TEXT) == "agent:wake"
    assert prompt_origin.origin_of_prompt(f"  {fleet_service.NUDGE_TEXT}\n") == "agent:wake"
    assert prompt_origin.origin_of_prompt("aisquare: board update") == "human:typed"


def test_two_identical_tells_keep_an_origin_each_and_a_withdrawn_one_only_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_manager")
    first = prompt_origin.mark_origin("status?", prompt_origin.TELL)
    prompt_origin.mark_origin("status?", prompt_origin.TELL)
    prompt_origin.mark_origin("status?", prompt_origin.TELL)
    prompt_origin.unmark_origin(first)  # the first paste failed: its marker, and only it, goes
    taken = [prompt_origin.take_origin("status?") for _ in range(3)]
    assert taken == ["agent:tell", "agent:tell", None]


def test_the_oldest_live_marker_is_taken_first(as_human: None) -> None:
    prompt_origin.mark_origin("go", prompt_origin.TELL, by=prompt_origin.AGENT)
    prompt_origin.mark_origin("go", prompt_origin.TELL, by=prompt_origin.HUMAN)
    assert [prompt_origin.take_origin("go"), prompt_origin.take_origin("go")] == [
        "agent:tell",
        "human:tell",
    ]


def test_the_nudge_text_is_said_once_for_the_hook_and_the_scan() -> None:
    assert prompt_origin.WAKE_TEXT == fleet_service.NUDGE_TEXT == transcript_facts.NUDGE_TEXT
