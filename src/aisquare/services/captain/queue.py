"""The captain's attention queue: what every agent needs from the owner, across every project.

The queue is what the owner hears on "what is up": item one, resolve, next. It
is DURABLE — a JSON file under the home, ``captain/queue.json``, written
whole under its own lock the way ``core.state_file`` writes ``state.json`` —
so a request the owner snoozed on the phone is still snoozed from the TUI,
and a row's history survives the captain's restart. A file rather than a
``captain_queue`` table (the card allows either): the store's migration ladder
is shared by every card on the train, and the fork the two v15 ladders left
(``store._converge_v15_fork``) is what a table would risk again; nothing here
needs a join.

Sources, read on every refresh through the :class:`Sources` seam (the store
and the fleet by default, plain lists in tests): ``question`` events that are
the owner's (:func:`is_owner_question`), blocked tasks, review cards nothing
has gated, panes parked on a permission prompt (the fleet's ``attention``
state, which the Notification hook sets — ``team.classify_notification``) or
on a y/N line a hook-less binary printed (the pane tail, the one place text is
read), agents gone quiet past ``STALE_AFTER`` or whose pane is lost, and pull
requests from a provider the caller plugs in (nothing calls ``gh`` inside the
refresh: its budget is a second over three projects).

Dedup is by a STABLE KEY: for a question, the project, the asking agent and
the text with ids, seqs and timestamps stripped (:func:`normalise`), so the
same request raises ``count`` instead of adding a row; a near-duplicate — same
project, agent and kind within :data:`NEAR_WINDOW`, token-set similarity at
least :data:`NEAR_THRESHOLD` — folds into the first row; for an item derived
from STATE (a blocked task, a parked pane) the key is the thing itself (the
task id, the agent id), so its text may change without minting a new row. A
state item clears itself when the condition is gone on a refresh (``cleared``
in its history, with what the state is now); a question is only ever resolved
by the owner. A resolved row that comes back re-opens with its history: a
question on a NEW event, a state item only after its condition went away and
returned — so "I told the coder to go ahead" does not bounce back on the next
tick while the pane is still drawing the prompt.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError
from aisquare.models import FleetAgentStatus, ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

log = logging.getLogger(__name__)

ItemKind = Literal["question", "blocked", "waiting", "review", "pr", "stale"]
ItemStatus = Literal["open", "resolved", "snoozed"]
HistoryAction = Literal["resolved", "snoozed", "reopened", "cleared"]

#: The ranking the owner hears: their questions first, then what is blocked, then
#: panes waiting on a key, then cards waiting on a gate, then PRs waiting on a
#: merge, then agents that went quiet. Lower comes first; ties go to the row that has waited
#: longest (``first_seen``).
RANK: dict[str, int] = {
    "question": 0,
    "blocked": 1,
    "waiting": 2,
    "review": 3,
    "pr": 4,
    "stale": 5,
}
#: Token-set (Jaccard) similarity at or above which two questions from the same
#: project, agent and kind, close in time, are one request said twice.
NEAR_THRESHOLD = 0.8
#: How close in time a near-duplicate must be to the row it folds into.
NEAR_WINDOW = timedelta(minutes=10)
#: On a queue's FIRST sight of a board, questions older than this (and further back
#: than :data:`EVENT_LIMIT` events) are history, not an agenda: the owner is not read
#: a month of answered questions on day one. After that every new question counts.
QUESTION_HORIZON = timedelta(hours=24)
#: A waiting agent whose session has not been seen for this long is stale — the
#: fleet's own presence window (``services.team._STALE_AFTER``).
STALE_AFTER = timedelta(minutes=30)
#: Roles a question is addressed to that mean "the owner": the human, everyone.
OWNER_ROLES: frozenset[str] = frozenset({"owner", "user", "all"})
#: Kinds derived from the state of things (not from an event): these clear
#: themselves when the state moves on.
STATE_KINDS: frozenset[str] = frozenset({"blocked", "waiting", "review", "pr", "stale"})
#: The events kinds that count as a card having been gated while in review.
GATE_KINDS: frozenset[str] = frozenset({"result", "decision"})
#: Rows of a pane tail read for a y/N line, and how many events one refresh reads.
PANE_TAIL_LINES = 12
EVENT_LIMIT = 500
#: The file's format, for a reader of a later version.
FILE_VERSION = 1
#: How long a writer waits for the queue's lock before giving up.
LOCK_WAIT_S = 2.0

#: What :func:`normalise` strips before texts are compared: task, project, agent
#: and event ids, UUIDs and hex ids (session prefixes, shas), event seqs and
#: ``#123`` refs, ISO timestamps and clock times. Ordinary numbers stay — "retry 3
#: times" and "retry 4 times" are different requests.
_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:tsk|prj|agt|evt|ses|sess|mcp|run)_[a-z0-9]+\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"\b[0-9a-f]{8,}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),
    re.compile(r"\bseq\s*#?\s*\d+\b"),
    re.compile(r"#\d+\b"),
)
_TOKEN = re.compile(r"[a-z0-9]+")
_SPACE = re.compile(r"\s+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: Lines a parked binary prints when it wants a key from a human.
_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\s*y\s*/\s*n\s*\]", re.IGNORECASE),
    re.compile(r"\(\s*y\s*/\s*n\s*\)", re.IGNORECASE),
    re.compile(r"\byes\s*/\s*no\b", re.IGNORECASE),
    re.compile(r"\bdo you want to (?:proceed|continue|allow|run|apply)\b", re.IGNORECASE),
    re.compile(r"\bpress (?:enter|any key|y)\b", re.IGNORECASE),
    re.compile(r"\b(?:proceed|continue)\?\s*$", re.IGNORECASE),
)


class QueueUnavailable(RuntimeError):
    """The queue cannot answer right now — its lock was held past :data:`LOCK_WAIT_S`.

    The name is the seam's from T1's stub (``actions._failure`` maps it to a
    refusal the owner can retry), kept so the actions module never references a
    class that went away with the stub.
    """


class QueueError(OSError):
    """The queue file could not be locked or written; it was left as it was.

    An ``OSError`` so the actions module reports it as ``error:`` (something
    failed), not as a refusal.
    """


class UnknownItemError(LookupError):
    """No queue item matches the reference."""


class AmbiguousItemError(LookupError):
    """The reference is a prefix of more than one item's id."""


class HistoryEntry(BaseModel):
    """One thing that happened to a row after it was first seen."""

    at: datetime
    action: HistoryAction
    how: str | None = None
    """What was done (``resolved``: the owner's words or the tool that acted; ``cleared``:
    what the state is now)."""


class QueueItem(BaseModel):
    """One thing that needs the owner, folded over every time it was asked."""

    id: str
    """``q`` + eight hex digits of :attr:`key`: stable for the row's whole life, short
    enough to say aloud, and a prefix resolves it (:meth:`AttentionQueue.get`)."""
    key: str
    """The dedup key (:func:`dedup_key`); not part of the ``--json`` shape."""
    project: str
    project_name: str
    agent: str | None = None
    """The agent's label (a pane) or the asking session's label or role (a question)."""
    kind: ItemKind
    text: str
    first_seen: datetime
    last_seen: datetime
    count: int = 1
    """How many times this was asked: a repeat raises it, a persisting state does not."""
    source_seq: int | None = None
    """The latest event seq folded into this row (questions), else ``None``."""
    source_ref: str | None = None
    """The event id, task id, agent id or PR number the row came from."""
    card: str | None = None
    """For a question asked about a card: that task's id. A card that closes (done or
    dropped) resolves the question itself, ``how="closed"``."""
    status: ItemStatus = "open"
    snoozed_until: datetime | None = None
    absent_since: datetime | None = None
    """For a state item: when its condition was last seen GONE; ``None`` while it
    persists. A resolved state item re-opens only from an absence."""
    history: list[HistoryEntry] = Field(default_factory=list)


class QueueSnapshot(BaseModel):
    """What one refresh did — the receipt a ``captain_action`` event or a CLI verb prints."""

    refreshed_at: datetime
    open: int = 0
    snoozed: int = 0
    resolved: int = 0
    added: int = 0
    folded: int = 0
    reopened: int = 0
    cleared: int = 0


@dataclass(frozen=True)
class PullRequest:
    """A pull request waiting on the owner, as a provider reports it."""

    number: int
    title: str
    url: str


@dataclass(frozen=True)
class Observation:
    """One thing a source says needs the owner right now."""

    key: str
    project: str
    project_name: str
    agent: str | None
    kind: ItemKind
    text: str
    seen_at: datetime
    source_seq: int | None = None
    source_ref: str | None = None
    card: str | None = None


@dataclass
class Observed:
    """Everything one pass over the sources saw."""

    observations: list[Observation] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    """The highest event seq read per project — the next refresh reads past it."""
    task_status: dict[str, str] = field(default_factory=dict)
    agent_state: dict[str, str] = field(default_factory=dict)


class Sources(Protocol):
    """Where the queue reads from. ``StoreSources`` is the product; tests hand in lists."""

    def projects(self) -> list[ProjectInfo]: ...

    def agents(self, project: ProjectInfo) -> list[FleetAgentStatus]: ...

    def tasks(self, project_id: str) -> list[TeamTask]: ...

    def events(self, project_id: str, *, since_seq: int) -> list[TeamEvent]: ...

    def events_about(
        self, project_id: str, task_id: str, *, since: datetime
    ) -> list[TeamEvent]: ...

    def latest_seq(self, project_id: str) -> int: ...

    def sessions(self, project_id: str) -> list[TeamSession]: ...

    def pane_tail(self, status: FleetAgentStatus) -> list[str]: ...

    def pull_requests(self, project: ProjectInfo) -> list[PullRequest]: ...


class StoreSources:
    """The default sources: the home's store, the fleet's derived states, the pane tails.

    ``pull_requests`` is a provider the caller plugs in (``gh pr list`` behind a
    flag, say); by default there is none, so a refresh never leaves the machine.
    """

    def __init__(
        self,
        *,
        pull_requests: Callable[[ProjectInfo], list[PullRequest]] | None = None,
        tail_lines: int = PANE_TAIL_LINES,
    ) -> None:
        self._pull_requests = pull_requests
        self._tail_lines = tail_lines

    def projects(self) -> list[ProjectInfo]:
        with store_session() as store:
            return store.list_projects()

    def agents(self, project: ProjectInfo) -> list[FleetAgentStatus]:
        return fleet_service.list_agents(project)

    def tasks(self, project_id: str) -> list[TeamTask]:
        with store_session() as store:
            return store.team_tasks(project_id)

    def events(self, project_id: str, *, since_seq: int) -> list[TeamEvent]:
        with store_session() as store:
            return store.events_since(project_id, since_seq, limit=EVENT_LIMIT)

    def events_about(self, project_id: str, task_id: str, *, since: datetime) -> list[TeamEvent]:
        with store_session() as store:
            return store.filtered_events(
                project_id, task_id=task_id, since_iso=since.isoformat(), limit=EVENT_LIMIT
            )

    def latest_seq(self, project_id: str) -> int:
        with store_session() as store:
            return store.latest_seq(project_id)

    def sessions(self, project_id: str) -> list[TeamSession]:
        with store_session() as store:
            return store.team_sessions(project_id)

    def pane_tail(self, status: FleetAgentStatus) -> list[str]:
        agent = status.agent
        try:
            server = fleet_service.server_for(agent.tmux_socket)
            return list(server.capture(agent.pane_id, height=self._tail_lines).lines)
        except TmuxError as exc:
            # Fail-soft, said: the pane is skipped this refresh and the next one
            # reads it again; a y/N line that is really there is a bell missed
            # for one tick, never one invented.
            log.warning(
                "captain queue: could not read the pane of %s (%s): %s",
                agent.label,
                agent.pane_id,
                exc,
            )
            return []

    def pull_requests(self, project: ProjectInfo) -> list[PullRequest]:
        if self._pull_requests is None:
            return []
        return self._pull_requests(project)


# --- keys ---------------------------------------------------------------------------------------


def normalise(text: str) -> str:
    """The comparison form of a request: lower-case, ids, seqs and timestamps stripped,
    whitespace collapsed — so the same ask about a different task id is the same ask."""
    lowered = text.lower()
    for pattern in _ID_PATTERNS:
        lowered = pattern.sub(" ", lowered)
    return _SPACE.sub(" ", lowered).strip()


def dedup_key(project: str, agent: str | None, kind: str, text: str) -> str:
    """The stable key of a request: project, agent, kind and the normalised text."""
    return _digest(f"{project}|{agent or ''}|{kind}|{normalise(text)}")


def _state_key(project: str, agent: str | None, kind: str, ref: str) -> str:
    """The stable key of a state item: the thing itself, whatever its text says today."""
    return _digest(f"{project}|{agent or ''}|{kind}|ref:{ref}")


def _digest(composite: str) -> str:
    return hashlib.sha1(composite.encode("utf-8")).hexdigest()  # a key, not a secret


def _item_id(key: str) -> str:
    return "q" + key[:8]


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of the two texts' token sets, over their normalised forms."""
    first = set(_TOKEN.findall(normalise(a)))
    second = set(_TOKEN.findall(normalise(b)))
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


# --- what counts --------------------------------------------------------------------------------


def is_owner_question(event: TeamEvent, *, roles: Mapping[str, str], manager_live: bool) -> bool:
    """Whether a ``question`` event is the OWNER's to answer.

    The manager's questions are (it asks the owner what the crew cannot decide);
    so is anything addressed to the owner, to everyone, or to nobody in
    particular; and a question to the manager on a board with NO live manager
    falls to the owner, because nobody else will read it. A coder's question to
    a live manager, or to a runner, is that role's business.
    """
    if event.kind != "question":
        return False
    author = roles.get(event.session_id or "")
    if author is not None and team_service.base_role(author) == "manager":
        return True
    to = (event.to_role or "").strip().lower()
    if not to or to in OWNER_ROLES:
        return True
    return to == "manager" and not manager_live


def _manager_live(sessions: Iterable[TeamSession], now: datetime) -> bool:
    return any(
        team_service.base_role(session.role) == "manager"
        and session.ended_at is None
        and now - session.last_seen_at <= STALE_AFTER
        for session in sessions
    )


def _prompt_line(tail: Iterable[str]) -> str | None:
    """The last line of a pane tail that asks a human for a key, plain text, or ``None``."""
    for raw in reversed(list(tail)):
        line = _ANSI.sub("", raw).strip()
        if line and any(pattern.search(line) for pattern in _PROMPT_PATTERNS):
            return line
    return None


@dataclass(frozen=True)
class _PaneSite:
    """One agent's pane, as the place an observation about it comes from."""

    project: str
    project_name: str
    label: str
    agent_id: str

    def observed(self, kind: ItemKind, text: str, at: datetime) -> Observation:
        return Observation(
            key=_state_key(self.project, self.label, kind, self.agent_id),
            project=self.project,
            project_name=self.project_name,
            agent=self.label,
            kind=kind,
            text=text,
            seen_at=at,
            source_ref=self.agent_id,
        )


def observe(sources: Sources, *, cursors: Mapping[str, int], now: datetime) -> Observed:
    """One pass over every project: what needs the owner right now, and where the events stand."""
    seen = Observed()
    for project in sources.projects():
        name = project.root.name or project.id
        sessions = sources.sessions(project.id)
        roles = {session.id: session.role for session in sessions}
        labels = {session.id: session.label or session.role for session in sessions}
        manager_live = _manager_live(sessions, now)
        cursor = cursors.get(project.id)
        horizon: datetime | None = None
        if cursor is None:
            # First sight of this board: the tail of its history, and only what is
            # recent enough to still be an agenda. From here on every event past
            # the cursor is new to the owner, however long the captain was away.
            latest = sources.latest_seq(project.id)
            cursor = max(0, latest - EVENT_LIMIT)
            horizon = now - QUESTION_HORIZON
            seen.cursors[project.id] = latest
        for event in sources.events(project.id, since_seq=cursor):
            seen.cursors[project.id] = max(seen.cursors.get(project.id, cursor), event.seq)
            if event.kind != "question":
                continue
            if horizon is not None and event.created_at < horizon:
                continue
            if not is_owner_question(event, roles=roles, manager_live=manager_live):
                continue
            agent = labels.get(event.session_id or "")
            text = event.text.strip()
            seen.observations.append(
                Observation(
                    key=dedup_key(project.id, agent, "question", text),
                    project=project.id,
                    project_name=name,
                    agent=agent,
                    kind="question",
                    text=text,
                    seen_at=event.created_at,
                    source_seq=event.seq,
                    source_ref=event.id,
                    card=event.task_id,
                )
            )
        for task in sources.tasks(project.id):
            seen.task_status[task.id] = task.status
            if task.status == "blocked":
                seen.observations.append(
                    Observation(
                        key=_state_key(project.id, None, "blocked", task.id),
                        project=project.id,
                        project_name=name,
                        agent=None,
                        kind="blocked",
                        text=task.title,
                        seen_at=task.updated_at,
                        source_ref=task.id,
                    )
                )
            elif task.status == "review":
                gated = any(
                    event.kind in GATE_KINDS
                    for event in sources.events_about(project.id, task.id, since=task.updated_at)
                )
                if not gated:
                    seen.observations.append(
                        Observation(
                            key=_state_key(project.id, None, "review", task.id),
                            project=project.id,
                            project_name=name,
                            agent=None,
                            kind="review",
                            text=f"{task.title} awaits a gate",
                            seen_at=task.updated_at,
                            source_ref=task.id,
                        )
                    )
        for status in sources.agents(project):
            agent_row = status.agent
            label = agent_row.label
            seen.agent_state[agent_row.id] = status.state
            session = status.session
            seen_at = session.last_seen_at if session is not None else now
            pane = _PaneSite(project.id, name, label, agent_row.id)
            if status.state == "attention":
                suffix = f": {status.detail}" if status.detail else ""
                seen.observations.append(
                    pane.observed("waiting", f"{label} waits on you{suffix}", seen_at)
                )
            elif status.state == "waiting":
                if session is not None and now - session.last_seen_at > STALE_AFTER:
                    minutes = int((now - session.last_seen_at).total_seconds() // 60)
                    seen.observations.append(
                        pane.observed(
                            "stale", f"{label} shows no sign of life for {minutes} min", seen_at
                        )
                    )
                else:
                    line = _prompt_line(sources.pane_tail(status))
                    if line is not None:
                        seen.observations.append(
                            pane.observed("waiting", f"{label} asks: {line}", now)
                        )
            elif status.state in ("lost", "unknown"):
                suffix = f": {status.detail}" if status.detail else ""
                seen.observations.append(
                    pane.observed("stale", f"{label} is {status.state}{suffix}", now)
                )
        for pr in sources.pull_requests(project):
            seen.observations.append(
                Observation(
                    key=_state_key(project.id, None, "pr", str(pr.number)),
                    project=project.id,
                    project_name=name,
                    agent=None,
                    kind="pr",
                    text=f"PR #{pr.number} {pr.title} waits on you",
                    seen_at=now,
                    source_ref=str(pr.number),
                )
            )
    return seen


# --- the queue ----------------------------------------------------------------------------------


def queue_path() -> Path:
    """Where the home's queue lives: ``<AISQUARE_HOME>/captain/queue.json``."""
    return paths.aisquare_home() / "captain" / "queue.json"


def rank(items: Iterable[QueueItem]) -> list[QueueItem]:
    """The owner's order: by :data:`RANK`, then the row that has waited longest, then id."""
    return sorted(
        items, key=lambda item: (RANK.get(item.kind, len(RANK)), item.first_seen, item.id)
    )


def as_json(items: Iterable[QueueItem]) -> list[dict[str, object]]:
    """The pinned ``--json`` shape: every field the owner or a tool acts on, JSON-native."""
    return [item.model_dump(mode="json", exclude={"key", "absent_since"}) for item in items]


def as_row(item: QueueItem) -> dict[str, object]:
    """One item in the pinned ``--json`` shape."""
    return as_json([item])[0]


@dataclass
class _State:
    items: dict[str, QueueItem] = field(default_factory=dict)
    """By dedup key."""
    cursors: dict[str, int] = field(default_factory=dict)


class AttentionQueue:
    """The durable queue over one file; every method is a load-act-save under the file's lock.

    ``sources`` default to the store and the fleet; ``clock`` to UTC now. Reads
    (``attention``, ``next``, ``items``, ``get``) take no lock and show a
    snoozed row whose time is up as open; the next write persists that.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        sources: Sources | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path if path is not None else queue_path()
        self._sources: Sources = sources if sources is not None else StoreSources()
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    # -- reads --

    def items(self) -> list[QueueItem]:
        """Every row, whatever its status, in the owner's order."""
        now = self._clock()
        return rank(_effective(item, now) for item in self._load().items.values())

    def attention(self, *, refresh: bool = False) -> list[QueueItem]:
        """The open rows in the owner's order — what "what is up" reads out."""
        if refresh:
            self.refresh()
        return [item for item in self.items() if item.status == "open"]

    def top(self) -> QueueItem | None:
        """Item one: the top of :meth:`attention`, or ``None`` when nothing needs the owner.

        Not ``next``: ``tests/test_config_writes_stay_in_the_cli.py`` builds its
        call graph by NAME, and a method called ``next`` fuses with every builtin
        ``next(...)`` in the CLI; ``_write`` rather than ``_save`` for the same reason.
        """
        ranked = self.attention()
        return ranked[0] if ranked else None

    def get(self, ref: str) -> QueueItem:
        """The row ``ref`` names — its id, or a prefix of it that names exactly one."""
        now = self._clock()
        return _effective(_find(self._load(), ref), now)

    # -- writes --

    def refresh(self) -> QueueSnapshot:
        """Read every source and fold what it says into the rows; the receipt says what changed."""
        now = self._clock()
        # The sources are read OUTSIDE the lock: tmux and the fleet take their
        # time, and a `resolve` from another terminal must not wait on them.
        # Folding is idempotent (a question is keyed by seq, a state item by
        # the thing itself), so two refreshes that observed from the same
        # cursors fold to the same rows.
        seen = observe(self._sources, cursors=self._load().cursors, now=now)
        with self._locked():
            state = self._load()
            snapshot = _fold(state, seen, now)
            for project_id, seq in seen.cursors.items():
                state.cursors[project_id] = max(state.cursors.get(project_id, 0), seq)
            self._write(state)
        return snapshot

    def resolve(self, ref: str, how: str) -> QueueItem:
        """Mark a row resolved and record what was done — a tell, a press, a task verb, a note."""
        now = self._clock()
        with self._locked():
            state = self._load()
            item = _find(state, ref)
            item.status = "resolved"
            item.snoozed_until = None
            item.absent_since = None
            item.history.append(HistoryEntry(at=now, action="resolved", how=how.strip() or None))
            self._write(state)
        return item

    def snooze(self, ref: str, minutes: int) -> QueueItem:
        """Hide a row for ``minutes``; it returns to the list, open, when the time is up."""
        if minutes <= 0:
            raise ValueError("snooze needs a positive number of minutes")
        now = self._clock()
        with self._locked():
            state = self._load()
            item = _find(state, ref)
            item.status = "snoozed"
            item.snoozed_until = now + timedelta(minutes=minutes)
            item.history.append(HistoryEntry(at=now, action="snoozed", how=f"{minutes} min"))
            self._write(state)
        return item

    # -- the file --

    def _load(self) -> _State:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _State()
        except OSError as exc:
            log.warning("captain queue: %s could not be read, starting empty: %s", self.path, exc)
            return _State()
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("the top level is not an object")
            items_raw = body.get("items", [])
            cursors_raw = body.get("cursors", {})
            if not isinstance(items_raw, list) or not isinstance(cursors_raw, dict):
                raise ValueError("items is not a list or cursors is not an object")
            items = [QueueItem.model_validate(row) for row in items_raw]
            cursors = {str(key): int(value) for key, value in cursors_raw.items()}
        except (ValueError, TypeError, ValidationError) as exc:
            # Said, never silent: a corrupt queue costs the owner its history,
            # and the file is rewritten whole on the next write.
            log.warning("captain queue: %s is not a queue file, starting empty: %s", self.path, exc)
            return _State()
        return _State(items={item.key: item for item in items}, cursors=cursors)

    def _write(self, state: _State) -> None:
        body = {
            "version": FILE_VERSION,
            "cursors": dict(sorted(state.cursors.items())),
            "items": [item.model_dump(mode="json") for item in rank(state.items.values())],
        }
        try:
            write_replacing(self.path, json.dumps(body, indent=1, sort_keys=True) + "\n")
        except OSError as exc:
            raise QueueError(f"{self.path} could not be written: {exc}") from exc

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold ``queue.json.lock`` for the block, waiting at most :data:`LOCK_WAIT_S`.

        The same shape as ``state_file._locked``: a lock FILE beside the data
        (the data is replaced by rename, so its own inode cannot be locked),
        taken without blocking and polled, so a stalled holder costs a bounded
        wait; the OS drops it if the process dies.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644
            )
        except OSError as exc:
            raise QueueError(f"{self.path}.lock could not be opened: {exc}") from exc
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    lock_exclusive(fd)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise QueueUnavailable(
                            f"{self.path}.lock is held by another writer — try again"
                        ) from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                try:
                    unlock(fd)
                except OSError as exc:
                    # Safe to go on: the descriptor is closed right after, and the
                    # OS releases the lock with it.
                    log.debug("captain queue: unlock of %s.lock failed: %s", self.path, exc)
        finally:
            os.close(fd)


def _effective(item: QueueItem, now: datetime) -> QueueItem:
    """The row as the owner sees it now: a snooze that ran out reads as open."""
    if item.status == "snoozed" and item.snoozed_until is not None and item.snoozed_until <= now:
        return item.model_copy(update={"status": "open", "snoozed_until": None})
    return item


def _find(state: _State, ref: str) -> QueueItem:
    ref = ref.strip()
    if not ref:
        raise UnknownItemError("an empty reference names no queue item")
    exact = [item for item in state.items.values() if item.id == ref]
    if exact:
        return exact[0]
    matches = [item for item in state.items.values() if item.id.startswith(ref)]
    if not matches:
        raise UnknownItemError(f"no queue item matches {ref!r}")
    if len(matches) > 1:
        raise AmbiguousItemError(f"{ref!r} names {len(matches)} queue items; say more of the id")
    return matches[0]


def _wake(state: _State, now: datetime) -> None:
    for item in state.items.values():
        if (
            item.status == "snoozed"
            and item.snoozed_until is not None
            and item.snoozed_until <= now
        ):
            item.status = "open"
            item.snoozed_until = None


def _near_match(state: _State, obs: Observation) -> QueueItem | None:
    """The row a near-duplicate question folds into: same project, agent and kind, last
    seen within :data:`NEAR_WINDOW`, texts at least :data:`NEAR_THRESHOLD` alike."""
    best: QueueItem | None = None
    best_score = 0.0
    for item in state.items.values():
        if item.kind != "question" or item.project != obs.project or item.agent != obs.agent:
            continue
        if abs(obs.seen_at - item.last_seen) > NEAR_WINDOW:
            continue
        score = similarity(item.text, obs.text)
        if score >= NEAR_THRESHOLD and score > best_score:
            best, best_score = item, score
    return best


def _cleared_how(item: QueueItem, seen: Observed) -> str:
    if item.kind in ("blocked", "review"):
        status = seen.task_status.get(item.source_ref or "")
        return f"the task is {status}" if status else "the task is gone"
    if item.kind in ("waiting", "stale"):
        state = seen.agent_state.get(item.source_ref or "")
        return f"{item.agent} is {state}" if state else f"{item.agent} is gone"
    return "the PR is gone"


def _fold(state: _State, seen: Observed, now: datetime) -> QueueSnapshot:
    """Fold one pass of observations into the rows; the counts are the receipt."""
    _wake(state, now)
    snapshot = QueueSnapshot(refreshed_at=now)
    present: set[str] = set()
    ordered = sorted(seen.observations, key=lambda o: (o.seen_at, o.source_seq or 0))
    for obs in ordered:
        item = state.items.get(obs.key)
        if item is None and obs.kind == "question":
            item = _near_match(state, obs)
        if obs.kind in STATE_KINDS:
            present.add(item.key if item is not None else obs.key)
        if item is None:
            state.items[obs.key] = QueueItem(
                id=_item_id(obs.key),
                key=obs.key,
                project=obs.project,
                project_name=obs.project_name,
                agent=obs.agent,
                kind=obs.kind,
                text=obs.text,
                first_seen=obs.seen_at,
                last_seen=obs.seen_at,
                source_seq=obs.source_seq,
                source_ref=obs.source_ref,
                card=obs.card,
            )
            snapshot.added += 1
            continue
        if obs.kind == "question":
            if (
                obs.source_seq is not None
                and item.source_seq is not None
                and obs.source_seq <= item.source_seq
            ):
                continue  # already folded
            item.count += 1
            item.last_seen = max(item.last_seen, obs.seen_at)
            item.source_seq = obs.source_seq
            item.source_ref = obs.source_ref
            item.card = item.card or obs.card
            if item.status == "resolved":
                _reopen(item, now)
                snapshot.reopened += 1
            else:
                snapshot.folded += 1
            continue
        # A state item: the condition is (still) there.
        item.project_name = obs.project_name
        if item.status == "resolved":
            if item.absent_since is not None:
                item.count += 1
                item.text = obs.text
                item.last_seen = max(item.last_seen, obs.seen_at, now)
                item.absent_since = None
                _reopen(item, now)
                snapshot.reopened += 1
            else:
                item.last_seen = max(item.last_seen, now)
        else:
            item.text = obs.text
            item.last_seen = max(item.last_seen, now)
    for item in state.items.values():
        if item.kind == "question":
            # The rider on the card (seq 13019): a question about a card that has
            # since closed is answered by the closing, whoever did it.
            closed = item.card is not None and seen.task_status.get(item.card) in (
                "done",
                "dropped",
            )
            if closed and item.status in ("open", "snoozed"):
                item.status = "resolved"
                item.snoozed_until = None
                item.history.append(HistoryEntry(at=now, action="cleared", how="closed"))
                snapshot.cleared += 1
            continue
        if item.key in present:
            continue
        if item.status in ("open", "snoozed"):
            item.status = "resolved"
            item.snoozed_until = None
            item.absent_since = now
            item.history.append(
                HistoryEntry(at=now, action="cleared", how=_cleared_how(item, seen))
            )
            snapshot.cleared += 1
        elif item.absent_since is None:
            item.absent_since = now
    for item in state.items.values():
        if item.status == "open":
            snapshot.open += 1
        elif item.status == "snoozed":
            snapshot.snoozed += 1
        else:
            snapshot.resolved += 1
    return snapshot


def _reopen(item: QueueItem, now: datetime) -> None:
    item.status = "open"
    item.snoozed_until = None
    item.history.append(HistoryEntry(at=now, action="reopened"))


# --- the four names T1's tools call (seq 13010), and the refresh that is T7's own ---------------


def default_queue(*, sources: Sources | None = None) -> AttentionQueue:
    """The home's queue over the default sources."""
    return AttentionQueue(sources=sources)


def refresh() -> QueueSnapshot:
    """Read every source into the home's queue — on the captain's tick, or on demand."""
    return default_queue().refresh()


def ranked(limit: int = 10) -> list[dict[str, object]]:
    """``attention(limit)`` in the T1 contract: the open rows, freshly read, in order."""
    if limit <= 0:
        raise ValueError("limit must be a positive number of items")
    return as_json(default_queue().attention(refresh=True)[:limit])


def next_item() -> dict[str, object] | None:
    """``next()`` in the T1 contract: item one, freshly read, or ``None``."""
    queue = default_queue()
    queue.refresh()
    top = queue.top()
    return as_row(top) if top is not None else None


def resolve(item_id: str, how: str) -> dict[str, object]:
    """``resolve(item, how)`` in the T1 contract; ``item_id`` may be a prefix of the id."""
    return as_row(default_queue().resolve(item_id, how))


def snooze(item_id: str, minutes: int) -> dict[str, object]:
    """``snooze(item, minutes)`` in the T1 contract; ``item_id`` may be a prefix of the id."""
    return as_row(default_queue().snooze(item_id, minutes))
