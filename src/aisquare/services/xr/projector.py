"""Board rows -> wire models, and the diff between two of those.

Everything here is a pure function of a :class:`ContextStore` read plus a clock:
no sockets, no polling, no state of its own. The server calls
:func:`snapshot` on a timer and :func:`delta` against what it last sent, which
is what makes both testable against a seeded board with no server running.

**The summary rule is a hard one.** ``Session.summary`` is computed from the
session's most recent BOARD EVENT — never from its transcript. The ambient tier
of the ring renders a summary per panel, ten panels at a time, and transcript
text there is both a frame-budget problem and a privacy one: the operator
subscribes to exactly one session when they want to read it. A test writes a
sentinel string into a seeded transcript file and asserts it appears in no
snapshot and no delta.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from aisquare.core.harness import base_role
from aisquare.core.store import ContextStore
from aisquare.models import FleetAgent, TeamEvent, TeamSession, TeamTask
from aisquare.services.team import _STALE_AFTER
from aisquare.services.xr.protocol import (
    ColorKey,
    Delta,
    Session,
    SessionRole,
    SessionState,
    Snapshot,
    Task,
)

SUMMARY_WORDS = 6
"""Hard cap on ``Session.summary``. §5 of the plan: a glanceable ring, not text."""

_EVENT_SCAN = 500
"""How far back the SUMMARY pass reads the event pipe, and the per-session cap on
an unread badge.

Deep enough that every session doing anything has said something inside it, and
bounded so a board with a hundred thousand events costs the same per poll as a
board with two hundred.

It has two distinct jobs, and they are not the same read:

- **Summaries** take the newest ``_EVENT_SCAN`` events of the whole board
  (:meth:`~aisquare.core.store.ContextStore.recent_events`, ``ORDER BY seq
  DESC``): a session whose last word has already scrolled past that depth has
  nothing glanceable to say, which is fine for a one-line summary.
- **Unread badges** must NOT use that board-wide window — it is exactly what let
  a busy session's traffic evict a quiet session's unread events, dropping its
  badge to 0 with no subscribe. So :func:`_unread_counts` counts each session
  from its OWN watermark
  (:meth:`~aisquare.core.store.ContextStore.unread_counts`) and uses this number
  only as the per-session CAP on the answer — a badge cannot report more than
  this, and that ceiling is reached only by a session that genuinely has this
  many unread events.
"""

#: ``base_role`` output -> palette slot. Every role this repo profiles is
#: listed; anything else (a bound role called ``bot7``, a seat whose base is
#: not a profiled role, an MCP client's ``remote``) is a worker and gets the
#: runner slot rather than no colour at all.
_COLOR_KEYS: dict[str, ColorKey] = {
    "planner": "planner",
    "manager": "planner",
    "coder": "coder",
    "reviewer": "coder",
    "runner": "runner",
    "tester": "runner",
    "validator": "runner",
    "unassigned": "runner",
}


def color_key(role: str) -> ColorKey:
    """Which of the three palette slots a role paints with."""
    return _COLOR_KEYS.get(base_role(role), "runner")


def wire_role(role: str) -> SessionRole:
    """A board role narrowed to the four the client knows how to label.

    An MCP client keeps its own bucket (``services.mcp_server._client_role``
    writes ``remote``) because "a browser agent in the desktop app" is a
    different thing to an operator than a pane on their own machine. Everything
    else follows the colour buckets, so the label and the colour of a panel can
    never disagree.
    """
    if base_role(role) == "remote":
        return "remote"
    return color_key(role)


def classify(session: TeamSession, *, now: datetime) -> SessionState:
    """``working`` / ``waiting`` / ``needs_you`` / ``gone`` for one row.

    ``gone`` covers both ways a session leaves: it said so (``ended_at``), or
    it stopped saying anything. The staleness horizon is
    ``services.team._STALE_AFTER``, imported rather than re-chosen, so a panel
    disappears from the ring at the same moment the board stops counting the
    session as present.
    """
    if session.ended_at is not None:
        return "gone"
    if now - session.last_seen_at > _STALE_AFTER:
        return "gone"
    if session.state == "attention":
        return "needs_you"
    if session.state == "waiting":
        return "waiting"
    return "working"


def summarize(event: TeamEvent | None) -> str:
    """A board event as at most :data:`SUMMARY_WORDS` words.

    The event ``kind`` leads because it is the part that is always meaningful
    (``note``, ``result``, ``task_claim``) and the text is whatever a session
    happened to write. Clipping mid-sentence is fine here: this is a glance
    target, and the operator who wants the rest focuses the panel.
    """
    if event is None:
        return ""
    words = f"{event.kind} {event.text}".split()
    clipped = words[:SUMMARY_WORDS]
    return " ".join(clipped)


def _title(session: TeamSession, task: TeamTask | None, agent: FleetAgent | None) -> str:
    """The panel's heading: the work, else the intent, else the name.

    A claimed task's title is the best answer because it is the thing the
    operator assigned. ``focus`` is the session's own account of what it is
    doing. The label (``coder-xr-server``) is last but never empty, so a panel
    always has a heading — an untitled rectangle in a ring is unidentifiable.
    """
    if task is not None and task.title.strip():
        return task.title.strip()
    if session.focus and session.focus.strip():
        return session.focus.strip()
    if agent is not None and agent.label.strip():
        return agent.label.strip()
    if session.label and session.label.strip():
        return session.label.strip()
    return session.role


def _claimed(tasks: Sequence[TeamTask], session_id: str) -> TeamTask | None:
    """The task this session holds, if any. ``doing`` wins over anything else."""
    held = [task for task in tasks if task.claimed_by == session_id]
    if not held:
        return None
    doing = [task for task in held if task.status == "doing"]
    chosen = doing or held
    return max(chosen, key=lambda task: task.updated_at)


def _latest_events(events: Sequence[TeamEvent]) -> dict[str, TeamEvent]:
    """The most recent event per session, for summaries.

    Takes the already-read board window rather than issuing its own query: the
    identical ``recent_events(_EVENT_SCAN)`` read was being made twice per
    :func:`sessions` call, here and in the unread pass, so :func:`sessions`
    reads it once and hands it to both. ``events`` is oldest-first (the store
    reverses its ``DESC`` read), so the last write per session id wins and
    ``latest`` ends up holding each session's newest event.
    """
    latest: dict[str, TeamEvent] = {}
    for event in events:
        if event.session_id:
            latest[event.session_id] = event
    return latest


def _unread_counts(
    store: ContextStore,
    project_id: str,
    rows: Sequence[TeamSession],
    since: Mapping[str, int],
    floor: int,
) -> dict[str, int]:
    """Unread events per session on the ring, each from its own watermark.

    ``since`` is owned by the connection: it holds the board position at which
    the operator last subscribed to (focused) a session, so "unread" means
    "since you last looked at this one". A session the connection has no
    watermark for — a late joiner spawned while the headset was on — counts from
    ``floor``, the board position this connection started at, so its badge fills
    with everything it has done since it appeared rather than reading 0 forever.
    Defaulting here is what lets the poll loop drop its per-tick re-seeding pass
    (a ``latest_seq`` plus a second full ``team_sessions`` read) entirely.

    The counting itself — per-session from each watermark, capped at
    :data:`_EVENT_SCAN`, in one indexed pass — lives in
    :meth:`~aisquare.core.store.ContextStore.unread_counts`, which documents why
    it must not be a board-wide window.
    """
    floors = {row.id: since.get(row.id, floor) for row in rows}
    return store.unread_counts(project_id, floors, cap=_EVENT_SCAN)


def sessions(
    store: ContextStore,
    project_id: str,
    *,
    now: datetime | None = None,
    unread_since: Mapping[str, int] | None = None,
    unread_floor: int = 0,
) -> list[Session]:
    """Every session that is still on the ring, as wire models.

    Departures are not in this list: a ``gone`` session is reported once as a
    ``delta.removed`` id (:func:`delta`) and then forgotten, which is what the
    client needs to tear a panel down. Ordering is the store's own, so a
    reconnecting client lays the ring out the same way it did before.

    ``unread_floor`` is the board position the connection started at: a session
    the connection has never watermarked (``unread_since``) counts its unread
    from here, so a late joiner gets a real badge without the poll loop having
    to re-seed watermarks every tick.
    """
    moment = now or datetime.now(tz=UTC)
    tasks = store.team_tasks(project_id)
    agents = {
        agent.session_id: agent
        for agent in store.fleet_agents(project_id, live_only=True)
        if agent.session_id
    }
    rows = store.team_sessions(project_id)
    # One board-window read, shared by summaries and (its own indexed query) the
    # unread pass — the two used to issue the identical recent_events twice.
    window = store.recent_events(project_id, limit=_EVENT_SCAN)
    latest = _latest_events(window)
    counts = _unread_counts(store, project_id, rows, unread_since or {}, unread_floor)
    out: list[Session] = []
    for row in rows:
        state = classify(row, now=moment)
        if state == "gone":
            continue
        task = _claimed(tasks, row.id)
        agent = agents.get(row.id)
        out.append(
            Session(
                id=row.id,
                role=wire_role(row.role),
                title=_title(row, task, agent),
                state=state,
                summary=summarize(latest.get(row.id)),
                task_id=task.id if task is not None else None,
                color_key=color_key(row.role),
                last_activity_at=row.last_seen_at.isoformat(),
                unread=counts.get(row.id, 0),
            )
        )
    return out


def tasks_of(store: ContextStore, project_id: str) -> list[Task]:
    """Open board tasks as wire models. Closed ones are not ring furniture."""
    return [
        Task(
            id=task.id,
            title=task.title,
            status=task.status,
            role=task.role,
            claimed_by=task.claimed_by,
        )
        for task in store.team_tasks(project_id)
        if task.status not in ("done", "dropped")
    ]


def snapshot(
    store: ContextStore,
    project_id: str,
    *,
    now: datetime | None = None,
    unread_since: Mapping[str, int] | None = None,
    unread_floor: int = 0,
) -> Snapshot:
    """The whole board for one connection.

    ``groups`` is empty and stays that way until groups ship; it is in the
    frame from day one because adding a required key to a message a client is
    already parsing is the change this protocol exists to avoid.
    """
    return Snapshot(
        sessions=sessions(
            store, project_id, now=now, unread_since=unread_since, unread_floor=unread_floor
        ),
        tasks=tasks_of(store, project_id),
        groups=[],
    )


def delta(previous: Sequence[Session], current: Sequence[Session]) -> Delta | None:
    """What changed between two session lists, or ``None`` when nothing did.

    ``None`` rather than an empty ``Delta`` because the server polls twice a
    second and an idle board must put nothing on the wire — a socket that
    speaks only when something happened is also the one a client can watch in
    a console.

    A session counts as changed when ANY field differs, compared on the dumped
    models so a new field is covered the day it is added rather than the day
    someone remembers to extend a hand-written comparison. ``unread`` is one of
    those fields, which is deliberate: a badge that changed is a change.
    """
    before = {session.id: session for session in previous}
    after = {session.id: session for session in current}
    changed = [
        session
        for sid, session in after.items()
        if sid not in before or before[sid].model_dump() != session.model_dump()
    ]
    removed = [sid for sid in before if sid not in after]
    if not changed and not removed:
        return None
    return Delta(changed=changed, removed=removed)
