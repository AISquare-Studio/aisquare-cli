"""The captain's attention queue (card T7): what every agent needs from the owner, deduplicated.

Every test drives ``services.captain.queue`` against an in-memory
:class:`FakeSources` over three projects and a clock the test moves — no
tmux, no ``gh``, no store — except the one at the bottom that reads a real
``SqliteStore`` in the isolated home through the default sources. The card's
acceptance lines are the test names: one row with count 3 from repeated
identical requests, a near-duplicate folding, resolve-then-repeat re-opening
the same row with its history, the ranking pinned, the ``--json`` shape
pinned, and the whole refresh over the fixture under a second.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.ids import new_agent_id, new_event_id, new_task_id
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import (
    FleetAgent,
    FleetAgentState,
    FleetAgentStatus,
    ProjectInfo,
    TaskStatus,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import queue as captain_queue
from aisquare.services.captain.queue import (
    EVENT_LIMIT,
    HISTORY_KEEP,
    NEAR_WINDOW,
    QUESTION_HORIZON,
    RANK,
    RETAIN_RESOLVED,
    SNOOZE_MAX_MINUTES,
    STALE_AFTER,
    AlreadyResolvedError,
    AmbiguousItemError,
    AttentionQueue,
    PullRequest,
    QueueError,
    QueueUnavailable,
    StoreSources,
    UnknownItemError,
    as_json,
    card_in,
    dedup_key,
    near_duplicates,
    normalise,
    plain,
)

T0 = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def tick(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeSources:
    """Every source the queue reads, as plain lists the test edits between refreshes."""

    _projects: list[ProjectInfo] = field(default_factory=list)
    _agents: dict[str, list[FleetAgentStatus]] = field(default_factory=dict)
    _tasks: dict[str, list[TeamTask]] = field(default_factory=dict)
    _events: dict[str, list[TeamEvent]] = field(default_factory=dict)
    _sessions: dict[str, list[TeamSession]] = field(default_factory=dict)
    _tails: dict[str, list[str]] = field(default_factory=dict)
    _prs: dict[str, list[PullRequest]] = field(default_factory=dict)
    _unobservable: set[str] = field(default_factory=set)
    """Agent ids whose pane cannot be read this pass (``pane_tail`` answers ``None``)."""
    calls: list[str] = field(default_factory=list)

    def projects(self) -> list[ProjectInfo]:
        self.calls.append("projects")
        return list(self._projects)

    def agents(self, project: ProjectInfo) -> list[FleetAgentStatus]:
        self.calls.append(f"agents:{project.id}")
        return list(self._agents.get(project.id, []))

    def tasks(self, project_id: str) -> list[TeamTask]:
        return list(self._tasks.get(project_id, []))

    def events(self, project_id: str, *, since_seq: int) -> list[TeamEvent]:
        return [e for e in self._events.get(project_id, []) if e.seq > since_seq]

    def recent_events(self, project_id: str, *, limit: int) -> list[TeamEvent]:
        newest = sorted(self._events.get(project_id, []), key=lambda e: e.seq)[-limit:]
        return newest

    def events_about(self, project_id: str, task_id: str, *, since: datetime) -> list[TeamEvent]:
        return [
            e
            for e in self._events.get(project_id, [])
            if e.task_id == task_id and e.created_at >= since
        ]

    def latest_seq(self, project_id: str) -> int:
        return max((e.seq for e in self._events.get(project_id, [])), default=0)

    def sessions(self, project_id: str) -> list[TeamSession]:
        return list(self._sessions.get(project_id, []))

    def pane_tail(self, status: FleetAgentStatus) -> list[str] | None:
        if status.agent.id in self._unobservable:
            return None
        return list(self._tails.get(status.agent.id, []))

    def pull_requests(self, project: ProjectInfo) -> list[PullRequest]:
        return list(self._prs.get(project.id, []))


def _project(tmp_path: Path, name: str) -> ProjectInfo:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return ProjectInfo(id=f"prj_{name}", root=root)


def _session(
    project: ProjectInfo, sid: str, role: str, *, last_seen: datetime, ended: bool = False
) -> TeamSession:
    return TeamSession(
        id=sid,
        project_id=project.id,
        role=role,
        started_at=last_seen - timedelta(hours=1),
        last_seen_at=last_seen,
        ended_at=last_seen if ended else None,
    )


def _event(
    project: ProjectInfo,
    text: str,
    seq: int,
    *,
    at: datetime,
    kind: str = "question",
    session_id: str | None = "manager-1",
    to_role: str | None = None,
    task_id: str | None = None,
) -> TeamEvent:
    return TeamEvent(
        seq=seq,
        id=new_event_id(),
        project_id=project.id,
        session_id=session_id,
        kind=kind,
        text=text,
        task_id=task_id,
        to_role=to_role,
        created_at=at,
    )


def _task(project: ProjectInfo, title: str, status: TaskStatus, *, at: datetime) -> TeamTask:
    tid = new_task_id()
    return TeamTask(
        id=tid,
        project_id=project.id,
        key=tid,
        title=title,
        status=status,
        created_at=at - timedelta(hours=2),
        updated_at=at,
    )


def _agent(
    project: ProjectInfo,
    label: str,
    state: FleetAgentState,
    *,
    detail: str | None = None,
    session: TeamSession | None = None,
    at: datetime = T0,
    agent_id: str | None = None,
) -> FleetAgentStatus:
    agent = FleetAgent(
        id=agent_id or new_agent_id(),
        project_id=project.id,
        label=label,
        role="coder",
        pane_id="%1",
        cwd=project.root,
        created_at=at - timedelta(hours=1),
        session_id=session.id if session is not None else None,
    )
    return FleetAgentStatus(agent=agent, state=state, detail=detail, session=session)


@dataclass
class Fixture:
    alpha: ProjectInfo
    beta: ProjectInfo
    gamma: ProjectInfo
    sources: FakeSources
    clock: FakeClock
    path: Path

    def queue(self) -> AttentionQueue:
        return AttentionQueue(self.path, sources=self.sources, clock=self.clock)

    def manager(self, project: ProjectInfo, *, live: bool = True) -> TeamSession:
        seen = self.clock.now if live else self.clock.now - STALE_AFTER - timedelta(minutes=1)
        session = _session(project, f"manager-{project.id}", "manager", last_seen=seen)
        self.sources._sessions.setdefault(project.id, []).append(session)
        return session

    def ask(
        self,
        project: ProjectInfo,
        text: str,
        *,
        seq: int,
        session_id: str | None = None,
        to_role: str | None = None,
        at: datetime | None = None,
        task_id: str | None = None,
    ) -> TeamEvent:
        event = _event(
            project,
            text,
            seq,
            at=at or self.clock.now,
            session_id=session_id or f"manager-{project.id}",
            to_role=to_role,
            task_id=task_id,
        )
        self.sources._events.setdefault(project.id, []).append(event)
        return event


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    alpha, beta, gamma = (_project(tmp_path, name) for name in ("alpha", "beta", "gamma"))
    sources = FakeSources(_projects=[alpha, beta, gamma])
    return Fixture(alpha, beta, gamma, sources, FakeClock(), tmp_path / "home" / "queue.json")


def _seed_three_projects(fx: Fixture) -> None:
    """One item of a different kind in each project, so a refresh touches all three."""
    fx.manager(fx.alpha)
    fx.manager(fx.beta)
    fx.ask(fx.beta, "Merge the release PR when you are back", seq=10)
    fx.sources._tasks[fx.gamma.id] = [
        _task(fx.gamma, "Rotate the staging token", "blocked", at=fx.clock.now)
    ]
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "coder1", "attention", detail="permission prompt")
    ]


# --- dedup ---------------------------------------------------------------------------------


def test_three_projects_with_the_same_request_repeated_fold_into_one_row_with_count_three(
    fx: Fixture,
) -> None:
    _seed_three_projects(fx)
    for seq in (21, 22, 23):
        fx.clock.tick(seconds=30)
        # Identical but for the noise ids stripped by normalise: an event seq and a
        # session prefix. A different TASK id would be a different question (card_in).
        fx.ask(
            fx.alpha,
            f"Owner, please approve the deploy of the release train (seq {seq}, 5e12e94e{seq})",
            seq=seq,
        )
    queue = fx.queue()
    snapshot = queue.refresh()
    repeated = [
        item for item in queue.items() if item.project == fx.alpha.id and item.kind == "question"
    ]
    assert len(repeated) == 1
    row = repeated[0]
    assert row.count == 3
    assert row.source_seq == 23
    assert row.first_seen < row.last_seen == fx.clock.now
    assert row.status == "open"
    # Beta's question, gamma's blocked task and alpha's waiting pane are their own rows.
    assert {item.kind for item in queue.items()} == {"question", "blocked", "waiting"}
    assert len(queue.items()) == 4
    assert snapshot.added == 4 and snapshot.folded == 2


def test_normalise_strips_ids_seqs_and_timestamps_but_keeps_ordinary_numbers() -> None:
    a = normalise(
        "Approve tsk_01m3bq0ymcbjbdbdpymts5vepn for coder3a bdb27461 "
        "at 2026-09-25T07:14:16Z, seq 13011"
    )
    b = normalise(
        "approve   TSK_01m3zzzzzzzzzzzzzzzzzzzzzz for coder3a f912c47a "
        "at 2026-09-24T01:02:03Z, seq 12"
    )
    assert a == b
    assert normalise("retry 3 times") != normalise("retry 4 times")
    assert dedup_key("p", "a", "question", "x  y") == dedup_key("p", "a", "question", "X Y")
    assert dedup_key("p", "a", "question", "x") != dedup_key("p", "b", "question", "x")


def test_a_near_duplicate_within_the_window_folds_into_the_first_row(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    first = fx.ask(fx.alpha, "Please approve the staging deploy for the release train", seq=1)
    fx.clock.tick(minutes=2)
    fx.ask(fx.alpha, "Please approve the staging deploy for the release train now", seq=2)
    queue = fx.queue()
    queue.refresh()
    rows = queue.items()
    assert len(rows) == 1
    assert rows[0].count == 2
    assert rows[0].text == first.text
    assert rows[0].source_seq == 2


def test_a_near_duplicate_outside_the_window_is_its_own_row(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Please approve the staging deploy for the release train", seq=1)
    fx.clock.tick(seconds=NEAR_WINDOW.total_seconds() + 60)
    fx.ask(fx.alpha, "Please approve the staging deploy for the release train now", seq=2)
    queue = fx.queue()
    queue.refresh()
    assert len(queue.items()) == 2


def test_a_different_request_from_the_same_agent_is_its_own_row(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    fx.ask(fx.alpha, "Which label goes on BE#3533, LGTM or hold?", seq=2)
    queue = fx.queue()
    queue.refresh()
    assert len(queue.items()) == 2


# --- resolve, re-open, history --------------------------------------------------------------


def test_resolving_then_a_repeat_reopens_the_same_row_with_its_history(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy of the release train", seq=1)
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    fx.clock.tick(minutes=1)
    resolved = queue.resolve(row.id, "told the manager: approved")
    assert resolved.status == "resolved"
    assert [h.action for h in resolved.history] == ["resolved"]
    assert resolved.history[0].how == "told the manager: approved"
    assert queue.attention() == []

    fx.clock.tick(minutes=5)
    fx.ask(fx.alpha, "Approve the deploy of the release train", seq=2)
    snapshot = queue.refresh()
    (again,) = queue.items()
    assert again.id == row.id
    assert again.status == "open"
    assert again.count == 2
    assert [h.action for h in again.history] == ["resolved", "reopened"]
    assert snapshot.reopened == 1
    assert queue.attention()[0].id == row.id


def test_a_repeat_already_folded_does_not_reopen_a_resolved_row(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    queue.resolve(row.id, "done")
    queue.refresh()  # the same event is behind the cursor: nothing comes back
    (still,) = queue.items()
    assert still.status == "resolved"
    assert still.count == 1


def test_resolve_by_id_prefix_and_the_lookup_errors(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    fx.ask(fx.alpha, "Rotate the token", seq=2)
    queue = fx.queue()
    queue.refresh()
    first, second = queue.attention()
    assert queue.resolve(first.id[:6], "ok").id == first.id
    with pytest.raises(UnknownItemError):
        queue.resolve("q_nothing", "ok")
    with pytest.raises(AmbiguousItemError):
        queue.resolve("q", "ok")
    assert isinstance(UnknownItemError("x"), LookupError)
    assert isinstance(AmbiguousItemError("x"), LookupError)
    assert queue.get(second.id).status == "open"


# --- ranking, next, snooze ------------------------------------------------------------------


def test_ranking_is_pinned_owner_questions_then_blocked_then_waiting_then_review_then_stale(
    fx: Fixture,
) -> None:
    fx.manager(fx.alpha)
    stale_session = _session(
        fx.alpha, "s-stale", "coder", last_seen=fx.clock.now - STALE_AFTER - timedelta(minutes=5)
    )
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "old", "waiting", session=stale_session),
        _agent(fx.alpha, "prompted", "attention", detail="permission prompt"),
    ]
    fx.sources._tasks[fx.alpha.id] = [
        _task(fx.alpha, "Gate the fold", "review", at=fx.clock.now),
        _task(fx.alpha, "Rotate the token", "blocked", at=fx.clock.now),
    ]
    fx.sources._prs[fx.alpha.id] = [PullRequest(number=7, title="fold", url="https://x/7")]
    fx.ask(fx.alpha, "Which label goes on the ticket?", seq=1)
    queue = fx.queue()
    ranked = queue.attention(refresh=True)
    assert [item.kind for item in ranked] == [
        "question",
        "blocked",
        "waiting",
        "review",
        "pr",
        "stale",
    ]
    assert RANK["question"] < RANK["blocked"] < RANK["waiting"] < RANK["review"] < RANK["pr"]
    assert RANK["pr"] < RANK["stale"]


def test_ties_within_a_rank_go_to_the_row_that_has_waited_longest(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.manager(fx.beta)
    fx.ask(fx.alpha, "First question", seq=1)
    fx.clock.tick(minutes=1)
    fx.ask(fx.beta, "Second question", seq=2)
    queue = fx.queue()
    queue.refresh()
    fx.clock.tick(minutes=1)
    fx.ask(fx.alpha, "First question", seq=3)  # a repeat moves last_seen, not first_seen
    queue.refresh()
    assert [item.text for item in queue.attention()] == ["First question", "Second question"]


def test_next_is_the_top_of_the_list_and_resolve_moves_on(fx: Fixture) -> None:
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    top = queue.top()
    assert top is not None and top.kind == "question"
    queue.resolve(top.id, "answered by voice")
    following = queue.top()
    assert following is not None and following.kind == "blocked"
    queue.resolve(following.id, "released the coder")
    third = queue.top()
    assert third is not None and third.kind == "waiting"
    queue.resolve(third.id, "pressed y")
    assert queue.top() is None


def test_snooze_hides_an_item_until_its_time_is_up(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.attention()
    snoozed = queue.snooze(row.id, 15)
    assert snoozed.status == "snoozed"
    assert snoozed.snoozed_until == fx.clock.now + timedelta(minutes=15)
    assert queue.attention() == []
    assert queue.top() is None
    fx.clock.tick(minutes=14)
    assert queue.attention() == []
    fx.clock.tick(minutes=2)
    (back,) = queue.attention()
    assert back.id == row.id and back.status == "open" and back.snoozed_until is None
    assert [h.action for h in back.history] == ["snoozed"]


# --- state-derived items clear themselves ---------------------------------------------------


def test_a_state_item_clears_itself_when_the_condition_is_gone_and_reopens_when_it_returns(
    fx: Fixture,
) -> None:
    task = _task(fx.gamma, "Rotate the staging token", "blocked", at=fx.clock.now)
    fx.sources._tasks[fx.gamma.id] = [task]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert row.kind == "blocked" and row.source_ref == task.id

    fx.clock.tick(minutes=3)
    fx.sources._tasks[fx.gamma.id] = [task.model_copy(update={"status": "doing"})]
    snapshot = queue.refresh()
    (cleared,) = queue.items()
    assert cleared.status == "resolved"
    assert [(h.action, h.how) for h in cleared.history] == [("cleared", "the task is doing")]
    assert snapshot.cleared == 1
    assert queue.attention() == []

    fx.clock.tick(minutes=3)
    fx.sources._tasks[fx.gamma.id] = [task.model_copy(update={"status": "blocked"})]
    queue.refresh()
    (back,) = queue.items()
    assert back.id == row.id and back.status == "open" and back.count == 2
    assert [h.action for h in back.history] == ["cleared", "reopened"]


def test_a_resolved_state_item_does_not_bounce_back_while_the_condition_persists(
    fx: Fixture,
) -> None:
    aid = new_agent_id()  # one fleet row, whatever state it is in
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "coder1", "attention", detail="permission prompt", agent_id=aid)
    ]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    queue.resolve(row.id, "told coder1 to go ahead")
    for _ in range(3):
        fx.clock.tick(seconds=20)
        queue.refresh()  # the pane is still parked on the prompt: the owner already spoke
    (still,) = queue.items()
    assert still.status == "resolved" and still.count == 1
    assert queue.attention() == []
    # Once the prompt is gone and a NEW one appears, the row comes back.
    fx.sources._agents[fx.alpha.id] = [_agent(fx.alpha, "coder1", "working", agent_id=aid)]
    queue.refresh()
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "coder1", "attention", detail="permission prompt", agent_id=aid)
    ]
    queue.refresh()
    (back,) = queue.items()
    assert back.id == row.id and back.status == "open" and back.count == 2
    assert [h.action for h in back.history] == ["resolved", "reopened"]


# --- what counts -----------------------------------------------------------------------------


def test_which_questions_are_the_owners(fx: Fixture) -> None:
    manager = fx.manager(fx.alpha)
    coder = _session(fx.alpha, "coder-a", "coder", last_seen=fx.clock.now)
    fx.sources._sessions[fx.alpha.id].append(coder)
    fx.ask(fx.alpha, "manager asks the owner", seq=1, session_id=manager.id)
    fx.ask(fx.alpha, "coder asks the manager", seq=2, session_id=coder.id, to_role="manager")
    fx.ask(fx.alpha, "coder asks everyone", seq=3, session_id=coder.id, to_role="all")
    fx.ask(fx.alpha, "coder asks nobody in particular", seq=4, session_id=coder.id)
    fx.ask(fx.alpha, "coder asks the owner", seq=5, session_id=coder.id, to_role="owner")
    fx.ask(fx.alpha, "coder asks a runner", seq=6, session_id=coder.id, to_role="runner")
    # Beta has no live manager: a coder's question to the manager falls to the owner.
    beta_coder = _session(fx.beta, "coder-b", "coder", last_seen=fx.clock.now)
    fx.sources._sessions[fx.beta.id] = [beta_coder]
    fx.ask(
        fx.beta, "coder asks an absent manager", seq=7, session_id=beta_coder.id, to_role="manager"
    )
    queue = fx.queue()
    queue.refresh()
    assert sorted(item.text for item in queue.items()) == sorted(
        [
            "manager asks the owner",
            "coder asks everyone",
            "coder asks nobody in particular",
            "coder asks the owner",
            "coder asks an absent manager",
        ]
    )


def test_questions_older_than_the_horizon_are_not_queued_on_first_sight(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "an old one", seq=1, at=fx.clock.now - QUESTION_HORIZON - timedelta(hours=1))
    fx.ask(fx.alpha, "a recent one", seq=2, at=fx.clock.now - timedelta(hours=1))
    queue = fx.queue()
    queue.refresh()
    assert [item.text for item in queue.items()] == ["a recent one"]


def test_after_first_sight_a_question_counts_however_long_the_captain_was_away(
    fx: Fixture,
) -> None:
    fx.manager(fx.alpha)
    queue = fx.queue()
    queue.refresh()  # first sight: nothing yet
    fx.ask(fx.alpha, "asked while the captain was off", seq=1, at=fx.clock.now + timedelta(hours=1))
    fx.clock.tick(days=3)
    queue.refresh()
    assert [item.text for item in queue.items()] == ["asked while the captain was off"]


def test_first_sight_reads_only_the_tail_of_a_long_history(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    for seq in range(1, EVENT_LIMIT + 201):
        fx.ask(fx.alpha, "the same ask, again", seq=seq, at=fx.clock.now - timedelta(minutes=1))
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert row.count == EVENT_LIMIT and row.source_seq == EVENT_LIMIT + 200
    fx.ask(fx.alpha, "the same ask, again", seq=EVENT_LIMIT + 201)
    queue.refresh()
    (row,) = queue.items()
    assert row.count == EVENT_LIMIT + 1


def test_a_question_whose_card_is_closed_resolves_itself(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    card = _task(fx.alpha, "Rotate the token", "doing", at=fx.clock.now)
    fx.sources._tasks[fx.alpha.id] = [card]
    fx.ask(fx.alpha, "Owner, which vault holds the new token?", seq=1, task_id=card.id)
    fx.ask(fx.alpha, "Owner, a question about nothing in particular", seq=2)
    queue = fx.queue()
    queue.refresh()
    assert [item.status for item in queue.items()] == ["open", "open"]
    fx.clock.tick(minutes=10)
    fx.sources._tasks[fx.alpha.id] = [
        card.model_copy(update={"status": "done", "updated_at": fx.clock.now})
    ]
    snapshot = queue.refresh()
    about_card = queue.get(next(i.id for i in queue.items() if i.card == card.id))
    assert about_card.status == "resolved"
    assert [(h.action, h.how) for h in about_card.history] == [("cleared", "closed")]
    assert snapshot.cleared == 1
    (still_open,) = queue.attention()
    assert still_open.card is None


def test_the_four_names_t1_calls_return_dicts(
    fx: Fixture, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ranked``, ``next_item``, ``resolve`` and ``snooze`` are the T1 contract's seam (seq
    13010): T1's tools call them by these names and pass the dicts through."""
    now = datetime.now(UTC)
    fx.clock.now = now
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1, at=now - timedelta(minutes=2))
    fx.ask(fx.alpha, "Rotate the token", seq=2, at=now - timedelta(minutes=1))
    monkeypatch.setattr(captain_queue, "StoreSources", lambda: fx.sources)
    rows = captain_queue.ranked(limit=1)
    assert [row["text"] for row in rows] == ["Approve the deploy"]
    assert captain_queue.queue_path().exists()
    top = captain_queue.next_item()
    assert top is not None and top["text"] == "Approve the deploy"
    done = captain_queue.resolve(str(top["id"]), "spoken: approved")
    assert done["status"] == "resolved"
    later = captain_queue.next_item()
    assert later is not None and later["text"] == "Rotate the token"
    parked = captain_queue.snooze(str(later["id"])[:6], 30)
    assert parked["status"] == "snoozed" and parked["snoozed_until"] is not None
    assert captain_queue.next_item() is None
    with pytest.raises(ValueError):
        captain_queue.ranked(limit=0)


def test_a_pane_tail_with_a_yes_no_prompt_is_a_waiting_item(fx: Fixture) -> None:
    quiet = _agent(fx.alpha, "codex1", "waiting", detail="no hooks")
    fx.sources._agents[fx.alpha.id] = [quiet, _agent(fx.alpha, "busy", "working")]
    fx.sources._tails[quiet.agent.id] = ["", "Do you want to proceed? [y/N]", ""]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert row.kind == "waiting" and row.agent == "codex1"
    assert "Do you want to proceed? [y/N]" in row.text


def test_stale_is_a_quiet_agent_whose_session_went_dark_or_a_lost_pane(fx: Fixture) -> None:
    dark = _session(
        fx.alpha, "s-dark", "coder", last_seen=fx.clock.now - STALE_AFTER - timedelta(minutes=1)
    )
    fresh = _session(fx.alpha, "s-fresh", "coder", last_seen=fx.clock.now)
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "dark", "waiting", session=dark),
        _agent(fx.alpha, "fresh", "waiting", session=fresh),
        _agent(fx.alpha, "gone", "lost", detail="pane gone"),
        _agent(fx.alpha, "done", "exited", detail="exit 0"),
    ]
    queue = fx.queue()
    queue.refresh()
    assert sorted((item.kind, item.agent or "") for item in queue.items()) == [
        ("stale", "dark"),
        ("stale", "gone"),
    ]


def test_a_review_card_with_no_gate_is_queued_and_a_gated_one_is_not(fx: Fixture) -> None:
    ungated = _task(fx.alpha, "Gate the fold", "review", at=fx.clock.now - timedelta(hours=1))
    gated = _task(fx.alpha, "Gate the docs", "review", at=fx.clock.now - timedelta(hours=1))
    fx.sources._tasks[fx.alpha.id] = [ungated, gated]
    fx.sources._events[fx.alpha.id] = [
        _event(
            fx.alpha,
            "GATE PASS on the docs",
            seq=5,
            at=fx.clock.now - timedelta(minutes=30),
            kind="result",
            task_id=gated.id,
        )
    ]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert row.kind == "review" and row.source_ref == ungated.id
    assert row.text == "Gate the fold awaits a gate"


def test_pull_requests_come_from_the_provider_and_the_default_provider_returns_none(
    fx: Fixture, tmp_path: Path
) -> None:
    fx.sources._prs[fx.alpha.id] = [PullRequest(number=42, title="the fold", url="https://x/42")]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert row.kind == "pr" and row.source_ref == "42" and "#42" in row.text
    sources = StoreSources()
    assert sources.pull_requests(fx.alpha) == []


# --- shape, durability, budget ------------------------------------------------------------------


def test_the_json_shape_is_pinned(fx: Fixture) -> None:
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    rows = as_json(queue.attention())
    assert rows and all(
        set(row)
        == {
            "id",
            "project",
            "project_name",
            "agent",
            "kind",
            "text",
            "first_seen",
            "last_seen",
            "count",
            "source_seq",
            "source_ref",
            "card",
            "status",
            "snoozed_until",
            "history",
        }
        for row in rows
    )
    question = next(row for row in rows if row["kind"] == "question")
    assert question["project_name"] == "beta"
    assert question["status"] == "open" and question["count"] == 1
    assert isinstance(question["first_seen"], str) and question["first_seen"].endswith("Z")
    assert question["snoozed_until"] is None and question["history"] == []
    json.dumps(rows)  # every value is JSON-native
    assert [row["id"] for row in rows] == [item.id for item in queue.attention()]
    assert all(
        isinstance(row["id"], str) and row["id"].startswith("q") and len(row["id"]) == 9
        for row in rows
    )


def test_the_queue_is_durable_across_instances_and_the_file_is_a_json_object(fx: Fixture) -> None:
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    (question,) = (item for item in queue.attention() if item.kind == "question")
    queue.snooze(question.id, 5)
    other = AttentionQueue(fx.path, sources=fx.sources, clock=fx.clock)
    assert [item.id for item in other.items()] == [item.id for item in queue.items()]
    assert other.get(question.id).status == "snoozed"
    body = json.loads(fx.path.read_text(encoding="utf-8"))
    assert body["version"] == 1 and isinstance(body["items"], list)
    assert body["cursors"] == {fx.alpha.id: 0, fx.beta.id: 10, fx.gamma.id: 0}
    assert not list(fx.path.parent.glob(".queue.json.*.tmp"))


def test_a_corrupt_queue_file_reads_as_empty_and_says_so(
    fx: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    fx.path.parent.mkdir(parents=True)
    fx.path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.queue"):
        queue = fx.queue()
        assert queue.items() == []
    corrupt = [r for r in caplog.records if "is not a queue file" in r.getMessage()]
    assert len(corrupt) == 1, "said once per process, not on every read"
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.queue"):
        queue.items()
        queue.attention()
    corrupt = [r for r in caplog.records if "is not a queue file" in r.getMessage()]
    assert len(corrupt) == 1
    _seed_three_projects(fx)
    queue.refresh()
    assert len(queue.items()) == 3
    # Corrupted again after a write: news again, exactly once.
    fx.path.write_text("[]", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.queue"):
        queue.items()
        queue.items()
    corrupt = [r for r in caplog.records if "is not a queue file" in r.getMessage()]
    assert len(corrupt) == 2


@pytest.mark.parametrize("body", [b"", b"   \n", b"\x00\x00\x00"])
def test_a_blank_queue_file_is_nothing_yet_not_corrupt(
    fx: Fixture, caplog: pytest.LogCaptureFixture, body: bytes
) -> None:
    """A ``touch``ed or crash-truncated file has nothing in it to protect (runner2, seq 13051)."""
    fx.path.parent.mkdir(parents=True)
    fx.path.write_bytes(body)
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.queue"):
        queue = fx.queue()
        assert queue.items() == []
        queue.refresh()
    assert not [r for r in caplog.records if "queue.json" in r.getMessage()]
    assert json.loads(fx.path.read_text(encoding="utf-8"))["version"] == 1


def test_a_held_lock_is_a_refusal_the_owner_can_retry_and_a_write_failure_is_an_error(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1's ``_failure`` maps ``QueueUnavailable`` to ``refused:`` and ``OSError`` to ``error:``."""
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    monkeypatch.setattr(captain_queue, "LOCK_WAIT_S", 0.1)
    lock = fx.path.with_name("queue.json.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    lock_exclusive(fd)
    try:
        with pytest.raises(QueueUnavailable, match="held by another writer"):
            queue.refresh()
        assert queue.attention()  # reads never wait on the lock
    finally:
        unlock(fd)
        os.close(fd)
    assert issubclass(QueueUnavailable, RuntimeError) and issubclass(QueueError, OSError)
    (top,) = queue.attention()[:1]
    monkeypatch.setattr(
        captain_queue, "write_replacing", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(QueueError, match="could not be written"):
        queue.resolve(top.id, "ok")


def test_the_whole_refresh_over_the_fixture_runs_under_a_second(fx: Fixture) -> None:
    _seed_three_projects(fx)
    for project in (fx.alpha, fx.beta, fx.gamma):
        for seq in range(100, 160):
            fx.ask(project, f"question number {seq % 7} about the fold", seq=seq)
    queue = fx.queue()
    started = time.perf_counter()
    queue.refresh()
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"refresh took {elapsed:.2f}s"
    assert len(queue.items()) < 60  # the 180 repeats folded into a few rows


def test_refresh_reads_every_project_once(fx: Fixture) -> None:
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    assert fx.sources.calls.count("projects") == 1
    assert sorted(c for c in fx.sources.calls if c.startswith("agents:")) == sorted(
        f"agents:{p.id}" for p in (fx.alpha, fx.beta, fx.gamma)
    )


# --- the gate 1 fix round (coderp, PR #218 comment 5830221685): each pinned --------------------


def test_a_question_about_a_card_that_was_already_closed_is_still_the_owners(fx: Fixture) -> None:
    """Blocking 1a: the closing must come AFTER the question to answer it."""
    fx.manager(fx.alpha)
    done = _task(fx.alpha, "the fold", "done", at=fx.clock.now - timedelta(hours=1))
    fx.sources._tasks[fx.alpha.id] = [done]
    fx.ask(fx.alpha, "The fold card is done — may I merge it to main?", seq=1, task_id=done.id)
    queue = fx.queue()
    snapshot = queue.refresh()
    (row,) = queue.attention()
    assert row.status == "open" and row.card == done.id and snapshot.cleared == 0


def test_questions_about_two_cards_are_two_rows_and_only_the_closed_cards_row_clears(
    fx: Fixture,
) -> None:
    """Blocking 1b: the card is part of the key; ids are stripped from the text."""
    fx.manager(fx.alpha)
    a = _task(fx.alpha, "A", "doing", at=fx.clock.now)
    b = _task(fx.alpha, "B", "doing", at=fx.clock.now)
    fx.sources._tasks[fx.alpha.id] = [a, b]
    fx.ask(fx.alpha, f"Can I merge {a.id}?", seq=1, task_id=a.id)
    fx.clock.tick(minutes=1)
    fx.ask(fx.alpha, f"Can I merge {b.id}?", seq=2, task_id=b.id)
    queue = fx.queue()
    queue.refresh()
    rows = {row.card: row for row in queue.attention() if row.kind == "question"}
    assert set(rows) == {a.id, b.id}
    fx.clock.tick(minutes=5)
    fx.sources._tasks[fx.alpha.id] = [
        a.model_copy(update={"status": "done", "updated_at": fx.clock.now}),
        b,
    ]
    snapshot = queue.refresh()
    assert snapshot.cleared == 1
    assert queue.get(rows[a.id].id).status == "resolved"
    assert queue.get(rows[b.id].id).status == "open", "B's ask still waits"


def test_an_unreadable_queue_file_refuses_every_call_and_is_never_overwritten(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking 2: one failed read must not become an empty queue written back."""
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    before = fx.path.read_bytes()
    real = Path.read_bytes

    def denied(self: Path) -> bytes:
        if self == fx.path:
            raise PermissionError(errno.EACCES, "denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(QueueError, match="could not be read"):
        queue.refresh()
    with pytest.raises(QueueError):
        queue.attention()
    with pytest.raises(QueueError):
        queue.resolve("q", "x")
    monkeypatch.undo()
    assert fx.path.read_bytes() == before
    assert len(queue.items()) == 3
    assert issubclass(QueueError, OSError), "T1 says error:, not refused:"


def test_resolve_and_snooze_on_a_resolved_row_are_refused_and_the_absence_survives(
    fx: Fixture,
) -> None:
    """Blocking 3: the parked-prompt sequence from the review."""
    aid = new_agent_id()
    prompt = _agent(fx.alpha, "coder1", "attention", detail="permission prompt", agent_id=aid)
    fx.sources._agents[fx.alpha.id] = [prompt]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    fx.sources._agents[fx.alpha.id] = [_agent(fx.alpha, "coder1", "working", agent_id=aid)]
    fx.clock.tick(seconds=30)
    queue.refresh()  # the owner granted it: the tick clears the row
    assert queue.get(row.id).status == "resolved"
    with pytest.raises(AlreadyResolvedError, match="already resolved") as caught:
        queue.resolve(row.id, "pressed y")  # the captain's bookkeeping, a tick late
    assert isinstance(caught.value, LookupError), "T1 says refused:"
    with pytest.raises(AlreadyResolvedError):
        queue.snooze(row.id, 5)
    assert len(queue.get(row.id).history) == 1, "no silent second entry"
    fx.clock.tick(minutes=2)
    fx.sources._agents[fx.alpha.id] = [prompt]
    queue.refresh()  # the next prompt: the row comes back
    back = queue.get(row.id)
    assert back.status == "open" and back.count == 2


def test_a_snoozed_row_can_still_be_resolved(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.attention()
    queue.snooze(row.id, 5)
    assert queue.resolve(row.id, "done anyway").status == "resolved"


def test_first_sight_reads_the_boards_own_newest_events_not_a_global_window(fx: Fixture) -> None:
    """Should-fix 4: seqs are global; a quiet board next to a busy one keeps its questions."""
    fx.manager(fx.alpha)
    fx.manager(fx.beta)
    fx.ask(fx.alpha, "The quiet board's one question", seq=1, at=fx.clock.now - timedelta(hours=1))
    for seq in range(2, EVENT_LIMIT + 200):
        fx.sources._events.setdefault(fx.beta.id, []).append(
            _event(fx.beta, f"busy note {seq}", seq, at=fx.clock.now, kind="note")
        )
    queue = fx.queue()
    queue.refresh()
    assert [item.text for item in queue.attention()] == ["The quiet board's one question"]


def test_a_pane_that_could_not_be_read_keeps_its_rows_unchanged(fx: Fixture) -> None:
    """Should-fix 5: "could not observe" is not "observed absent"."""
    aid = new_agent_id()
    quiet = _agent(fx.alpha, "codex1", "waiting", detail="no hooks", agent_id=aid)
    fx.sources._agents[fx.alpha.id] = [quiet]
    fx.sources._tails[aid] = ["Do you want to proceed? [y/N]"]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.attention()
    fx.sources._unobservable.add(aid)
    fx.clock.tick(seconds=30)
    snapshot = queue.refresh()
    assert snapshot.cleared == 0 and queue.get(row.id).status == "open"
    fx.sources._unobservable.discard(aid)
    fx.sources._tails[aid] = ["$ "]
    queue.refresh()
    assert queue.get(row.id).status == "resolved" and queue.get(row.id).count == 1


def test_only_the_bottom_lines_of_a_screen_can_hold_the_prompt(fx: Fixture) -> None:
    quiet = _agent(fx.alpha, "codex1", "waiting", detail="no hooks")
    fx.sources._agents[fx.alpha.id] = [quiet]
    fx.sources._tails[quiet.agent.id] = [
        "Do you want to proceed? [y/N] y",
        "installing...",
        "fetching...",
        "linking...",
        "checking...",
        "cleaning...",
        "done.",
        "",
        "$ ",
    ]
    queue = fx.queue()
    queue.refresh()
    assert queue.items() == []


def test_a_menu_prompt_with_the_question_above_its_options_is_a_waiting_item(
    fx: Fixture,
) -> None:
    """runner2 (seq 13126): a hook-less binary that prints the question, then the options."""
    quiet = _agent(fx.alpha, "codex1", "waiting", detail="no hooks")
    fx.sources._agents[fx.alpha.id] = [quiet]
    fx.sources._tails[quiet.agent.id] = [
        "Do you want to proceed?",
        "",
        "1. Yes",
        "2. No",
        "3. No, and tell me what to do differently",
        "> ",
    ]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert row.kind == "waiting" and "Do you want to proceed?" in row.text


def test_a_question_that_names_one_card_in_its_words_is_about_that_card(fx: Fixture) -> None:
    """runner2 (seq 13126): without --task, the one tsk_ id in the text is the card."""
    fx.manager(fx.alpha)
    a = _task(fx.alpha, "A", "doing", at=fx.clock.now)
    b = _task(fx.alpha, "B", "doing", at=fx.clock.now)
    fx.sources._tasks[fx.alpha.id] = [a, b]
    fx.ask(fx.alpha, f"Can I merge {a.id}?", seq=1)
    fx.clock.tick(minutes=1)
    fx.ask(fx.alpha, f"Can I merge {b.id}?", seq=2)
    fx.ask(fx.alpha, f"Merge {a.id} and {b.id} together?", seq=3)
    queue = fx.queue()
    queue.refresh()
    rows = queue.attention()
    assert sorted(str(row.card) for row in rows) == sorted([a.id, b.id, "None"])
    assert card_in("nothing here") is None and card_in(f"{a.id} or {b.id}") is None
    fx.clock.tick(minutes=5)
    fx.sources._tasks[fx.alpha.id] = [
        a.model_copy(update={"status": "done", "updated_at": fx.clock.now}),
        b,
    ]
    queue.refresh()
    assert {row.card: row.status for row in queue.items()} == {
        a.id: "resolved",
        b.id: "open",
        None: "open",
    }


def test_the_captains_own_question_is_never_the_owners(fx: Fixture) -> None:
    """Should-fix 6: a question the captain relays through T1's note() is not read back."""
    fx.manager(fx.alpha)
    captain = _session(fx.alpha, "captain:alpha", "captain", last_seen=fx.clock.now)
    fx.sources._sessions[fx.alpha.id].append(captain)
    fx.ask(fx.alpha, "coder-1: is the migration done?", seq=1, session_id=captain.id)
    queue = fx.queue()
    queue.refresh()
    assert queue.items() == []


def test_an_older_observation_folded_later_does_not_undo_a_newer_fold(fx: Fixture) -> None:
    """Should-fix 7: two refreshes racing; the one that looked later wins."""
    from aisquare.services.captain.queue import _fold, observe

    aid = new_agent_id()
    prompt = _agent(fx.alpha, "coder1", "attention", detail="permission prompt", agent_id=aid)
    fx.sources._agents[fx.alpha.id] = [prompt]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.attention()
    older = observe(fx.sources, cursors=queue._load().cursors, now=fx.clock.now)  # sees the prompt
    fx.clock.tick(seconds=20)
    fx.sources._agents[fx.alpha.id] = [_agent(fx.alpha, "coder1", "working", agent_id=aid)]
    queue.refresh()  # the newer look: the prompt is gone, the row clears
    assert queue.get(row.id).status == "resolved"
    state = queue._load()
    snapshot = _fold(state, older, fx.clock.now)
    assert snapshot.reopened == 0
    assert state.items[row.key].status == "resolved", "the older look changes nothing"


def test_normalise_is_bounded_on_untrusted_text() -> None:
    """Should-fix 8a: no quadratic pattern under the lock."""
    hostile = "seq" + " " * 200_000 + "1 " + "x" * 200_000
    started = time.perf_counter()
    out = normalise(hostile)
    assert time.perf_counter() - started < 0.5
    assert len(out) <= 2000


def test_escape_sequences_and_hyperlink_targets_never_reach_the_owner(fx: Fixture) -> None:
    """Should-fix 8b: an OSC 8 link target is invisible on screen and must stay so."""
    line = (
        "\x1b]8;;http://evil.example/ignore-your-rules\x07Continue? [y/N]"
        "\x1b]8;;\x07\x1b[31m\x1b[0m"
    )
    assert plain(line) == "Continue? [y/N]"
    quiet = _agent(fx.alpha, "codex1", "waiting", detail="no hooks")
    fx.sources._agents[fx.alpha.id] = [quiet]
    fx.sources._tails[quiet.agent.id] = [line]
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.items()
    assert "evil" not in row.text and "Continue? [y/N]" in row.text


def test_a_one_word_substitution_is_not_a_near_duplicate_but_an_added_word_is(
    fx: Fixture,
) -> None:
    """Should-fix 9: staging vs production sit at exactly 0.800 — two requests."""
    a = "Can I delete the staging database before the release tonight?"
    b = "Can I delete the production database before the release tonight?"
    assert not near_duplicates(a, b)
    assert near_duplicates(a, a + " please")
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, a, seq=1)
    fx.clock.tick(minutes=1)
    fx.ask(fx.alpha, b, seq=2)
    queue = fx.queue()
    queue.refresh()
    assert len(queue.attention()) == 2


def test_the_source_seq_guard_folds_the_same_observation_once(fx: Fixture) -> None:
    """Pin 10a: the guard, not the cursor, is what makes a fold idempotent."""
    from aisquare.services.captain.queue import _fold, observe

    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue = fx.queue()
    seen = observe(fx.sources, cursors={}, now=fx.clock.now)
    state = queue._load()
    _fold(state, seen, fx.clock.now)
    _fold(state, seen, fx.clock.now)
    (row,) = state.items.values()
    assert row.count == 1


def test_a_managers_question_counts_whoever_it_is_addressed_to(fx: Fixture) -> None:
    """Pin 10b: the manager-author rule."""
    manager = fx.manager(fx.alpha)
    fx.ask(fx.alpha, "manager asks a runner", seq=1, session_id=manager.id, to_role="runner")
    queue = fx.queue()
    queue.refresh()
    assert [i.text for i in queue.items()] == ["manager asks a runner"]


def test_a_question_to_a_stale_manager_falls_to_the_owner(fx: Fixture) -> None:
    """Pin 10c: "live" means seen within STALE_AFTER."""
    fx.manager(fx.alpha, live=False)
    coder = _session(fx.alpha, "coder-a", "coder", last_seen=fx.clock.now)
    fx.sources._sessions[fx.alpha.id].append(coder)
    fx.ask(
        fx.alpha,
        "coder asks a manager who went dark",
        seq=1,
        session_id=coder.id,
        to_role="manager",
    )
    queue = fx.queue()
    queue.refresh()
    assert len(queue.items()) == 1


def test_the_same_text_on_two_boards_is_two_rows(fx: Fixture) -> None:
    """Pin 10d: the project is part of the key."""
    fx.manager(fx.alpha)
    fx.manager(fx.beta)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    fx.ask(fx.beta, "Approve the deploy", seq=2)
    queue = fx.queue()
    queue.refresh()
    assert sorted(i.project for i in queue.items()) == sorted([fx.alpha.id, fx.beta.id])


@pytest.mark.parametrize(
    ("noisy", "clean"),
    [
        ("ask about 0c9e1a2b-3d4e-5f60-7182-93a4b5c6d7e8 now", "ask about now"),
        ("ask about deadbeefcafe now", "ask about now"),
        ("ask at 2026-09-25T07:14:16Z now", "ask at now"),
        ("ask at 07:14 now", "ask at now"),
        ("ask on evt_01abc now", "ask on now"),
        ("ask #13011 now", "ask now"),
        ("ask seq 13011 now", "ask now"),
    ],
)
def test_each_stripping_pattern_earns_its_place(noisy: str, clean: str) -> None:
    """Pin 10e: every pattern in _ID_PATTERNS is exercised on its own."""
    assert normalise(noisy) == clean


def test_a_decision_event_gates_a_review_card_too(fx: Fixture) -> None:
    """Pin 10f: GATE_KINDS is result AND decision."""
    card = _task(fx.alpha, "Gate the fold", "review", at=fx.clock.now - timedelta(hours=1))
    fx.sources._tasks[fx.alpha.id] = [card]
    fx.sources._events[fx.alpha.id] = [
        _event(fx.alpha, "APPROVE", seq=5, at=fx.clock.now, kind="decision", task_id=card.id)
    ]
    queue = fx.queue()
    queue.refresh()
    assert queue.items() == []


def test_history_entries_are_part_of_the_json_shape(fx: Fixture) -> None:
    """Pin 10g."""
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.attention()
    queue.snooze(row.id, 5)
    (dumped,) = as_json(queue.items())
    history = dumped["history"]
    assert isinstance(history, list) and len(history) == 1
    assert set(history[0]) == {"at", "action", "how"}
    assert history[0]["action"] == "snoozed" and history[0]["how"] == "5 min"


def test_review_with_no_gate_anchors_on_the_task_review_event_not_updated_at(fx: Fixture) -> None:
    """Should-fix 12: a later bump of updated_at must not hide the gate."""
    entered = fx.clock.now - timedelta(hours=2)
    card = _task(fx.alpha, "Gate the fold", "review", at=fx.clock.now)  # updated_at bumped NOW
    fx.sources._tasks[fx.alpha.id] = [card]
    fx.sources._events[fx.alpha.id] = [
        _event(fx.alpha, "to review", seq=3, at=entered, kind="task_review", task_id=card.id),
        _event(
            fx.alpha,
            "GATE PASS",
            seq=4,
            at=entered + timedelta(minutes=30),
            kind="result",
            task_id=card.id,
        ),
    ]
    queue = fx.queue()
    queue.refresh()
    assert queue.items() == [], "gated after it entered review; the bump is not a re-entry"


def test_history_is_capped_and_old_resolved_rows_are_dropped(fx: Fixture) -> None:
    """Should-fix 13a: the file must not only grow."""
    fx.manager(fx.alpha)
    queue = fx.queue()
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue.refresh()
    (row,) = queue.attention()
    for seq in range(2, HISTORY_KEEP + 12):
        queue.resolve(row.id, f"round {seq}")
        fx.clock.tick(minutes=1)
        fx.ask(fx.alpha, "Approve the deploy", seq=seq)
        queue.refresh()
    assert len(queue.get(row.id).history) <= HISTORY_KEEP
    queue.resolve(row.id, "for good")
    fx.clock.tick(seconds=RETAIN_RESOLVED.total_seconds() + 60)
    queue.refresh()
    assert queue.items() == []


def test_an_unchanged_fold_does_not_rewrite_the_file(fx: Fixture) -> None:
    """Should-fix 13b: no rewrite, no fsync, when nothing moved."""
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    before = fx.path.read_bytes()
    stamp = fx.path.stat().st_mtime_ns
    fx.clock.tick(seconds=5)
    queue.refresh()
    assert fx.path.read_bytes() == before and fx.path.stat().st_mtime_ns == stamp


def test_a_crlf_file_from_a_windows_write_is_still_an_unchanged_fold(fx: Fixture) -> None:
    """13147: write_replacing writes text mode, so on Windows queue.json holds \\r\\n and
    the unchanged check compared LF text against the CRLF file: identical content was
    rewritten on every fold (st_mtime_ns moved 7 ms on CI). Newlines are normalised on
    read, so a cold queue over a CRLF file is a no-op there too."""
    _seed_three_projects(fx)
    fx.queue().refresh()
    fx.path.write_bytes(fx.path.read_bytes().replace(b"\n", b"\r\n"))
    before = fx.path.read_bytes()
    stamp = fx.path.stat().st_mtime_ns
    fx.clock.tick(seconds=5)
    fx.queue().refresh()  # a fresh queue reads the CRLF file cold
    assert fx.path.read_bytes() == before and fx.path.stat().st_mtime_ns == stamp


def test_snooze_is_bounded(fx: Fixture) -> None:
    fx.manager(fx.alpha)
    fx.ask(fx.alpha, "Approve the deploy", seq=1)
    queue = fx.queue()
    queue.refresh()
    (row,) = queue.attention()
    with pytest.raises(ValueError, match="minutes"):
        queue.snooze(row.id, 10**10)
    assert queue.snooze(row.id, SNOOZE_MAX_MINUTES).status == "snoozed"


def test_a_lock_error_that_is_not_held_is_refused_at_once(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fd: int) -> None:
        raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setattr(captain_queue, "lock_exclusive", broken)
    started = time.perf_counter()
    with pytest.raises(QueueError, match="could not be taken"):
        fx.queue().refresh()
    assert time.perf_counter() - started < 1.0


def test_a_bom_is_read_and_invalid_utf8_is_corrupt_said_once(
    fx: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_three_projects(fx)
    queue = fx.queue()
    queue.refresh()
    fx.path.write_bytes(b"\xef\xbb\xbf" + fx.path.read_bytes())
    assert len(queue.items()) == 3, "a BOM is not corruption"
    fx.path.write_bytes(b"\xff\xfe{not utf8")
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.queue"):
        assert queue.items() == []
        queue.items()
    assert len([r for r in caplog.records if "is not a queue file" in r.getMessage()]) == 1
    queue.refresh()
    assert len(queue.items()) == 3, "the next write replaces the bad file"


# --- the default sources read the store ---------------------------------------------------------


def test_store_sources_read_projects_tasks_and_questions_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = [tmp_path / name for name in ("one", "two")]
    for root in roots:
        root.mkdir()
    one, two = (team_project(root) for root in roots)
    now = datetime.now(UTC)
    with store_session() as store:
        for info in (one, two):
            store.onboard_project(info)
        manager = _session(one, "m-one", "manager", last_seen=now)
        store.upsert_session(manager)
        store.add_team_event(_event(one, "Owner: which train?", 0, at=now, session_id=manager.id))
        store.add_team_event(_event(one, "a plain note", 0, at=now, kind="note"))
        tid = new_task_id()
        store.upsert_task(
            TeamTask(
                id=tid,
                project_id=two.id,
                key=tid,
                title="Rotate the token",
                status="blocked",
                created_at=now,
                updated_at=now,
            )
        )
    listed: list[str] = []

    def fake_list_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        listed.append(project.id)
        return []

    monkeypatch.setattr(fleet_service, "list_agents", fake_list_agents)
    queue = AttentionQueue(tmp_path / "queue.json", sources=StoreSources())
    queue.refresh()
    rows = queue.attention()
    assert [(row.kind, row.project_name, row.text) for row in rows] == [
        ("question", "one", "Owner: which train?"),
        ("blocked", "two", "Rotate the token"),
    ]
    assert sorted(listed) == sorted([one.id, two.id])


def test_the_default_queue_lives_under_the_home(isolated_home: Path) -> None:
    assert captain_queue.queue_path() == isolated_home / "captain" / "queue.json"
