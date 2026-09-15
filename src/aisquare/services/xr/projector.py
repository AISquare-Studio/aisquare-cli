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

    **The attention flag is checked BEFORE the horizon, and that order is the
    point.** A session waiting on the operator — a permission prompt, the idle
    notification after a finished turn — writes ``last_seen_at`` exactly once,
    from the Notification hook, and then nothing runs on it until the operator
    answers: Claude Code does not re-notify, and no other hook fires while it
    is parked. With the horizon applied first, thirty-one minutes into exactly
    the walk-away the alert exists for the poll sent ``delta.removed`` for it:
    the ``needs_you`` panel and its bar vanished, **B** (jump to alert) found
    nothing, a headset connecting later never saw it, and the board's own
    ``aisquare board`` still listed the row as NEEDS YOU. So a flagged session
    stays on the ring, alert standing, until a hook says otherwise — the next
    prompt, tool call or session end clears the flag — and only a session with
    no attention flag ages out on the clock.
    """
    if session.ended_at is not None:
        return "gone"
    if session.state == "attention":
        return "needs_you"
    if now - session.last_seen_at > _STALE_AFTER:
        return "gone"
    if session.state == "waiting":
        return "waiting"
    return "working"


#: The one word a task event keeps in front of its text, by kind. The text of
#: every ``task_*`` event is the task's TITLE (``team.py`` writes the title, or
#: ``title — note``), so without a verb "wiring JWT into the refresh path"
#: reads the same whether the task was just claimed, sent to review, released
#: or finished — and the operator reads a finished task as still in progress.
#: The vocabulary is the board's own status words, which the operator already
#: knows from ``aisquare team task list``; each is short because the verb and
#: the first word of the title have to share about thirteen legible characters.
_TASK_VERBS: dict[str, str] = {
    "task_added": "todo:",
    "task_claimed": "doing:",
    "task_review": "review:",
    "task_done": "done:",
    "task_released": "released:",
    "task_blocked": "blocked:",
    "task_reopened": "reopened:",
    "task_dropped": "dropped:",
}


def summarize(event: TeamEvent | None) -> str:
    """A board event as at most :data:`SUMMARY_WORDS` words; a task event leads with a verb.

    **The event KIND is not spelled out, and that is a decision** (board seq
    293), not an omission. The cap here is six words, but the ambient tier is
    a panel at arm's length with a 1.5-degree cap-height floor — §8's floor,
    which exists because passthrough washes out low contrast — and the
    arithmetic of those two numbers is about thirteen legible characters. Six
    words do not fit in thirteen characters, so the client truncates, and
    whatever leads is the whole of what the operator actually reads.

    Leading with the kind spent all thirteen of them on it: ``task_claimed``
    plus a task id rendered as ``task_claimed…``, and an id is not something
    anyone reads off a wall. Leading with the text alone, which this function
    did for one release, spent them on ``wiring JWT i`` and lost what had
    HAPPENED to the task: ``team.py`` writes the task title as the text of
    every ``task_*`` event, so a claim and a review, or a release and a
    completion, rendered identical panels. No other part of the panel carries
    it — the state chip is the session's ``working``/``waiting``/``needs_you``,
    not the event's kind — so the summary has to. A task event therefore leads
    with one short board-status word (:data:`_TASK_VERBS`): ``doing: wiring
    JWT`` and ``done: wiring JWT`` are different panels, and the verb costs
    the title five to nine of the thirteen characters, which is the trade.
    Every other kind (a note, a result, a question, a decision) carries its
    own words and gets none.

    The cap stays at six words, verb included, because it is what the
    protocol promises and the FOCUS tier renders the same field in full —
    that two-tier split is what §5 and §7 are for. Clipping mid-sentence is
    fine at both sizes: this is a glance target, and the operator who wants the
    rest focuses the panel.

    An event with no text at all falls back to its kind, because a blank panel
    line says less than ``heartbeat`` does. That is a fallback and not the old
    prefix: it can only appear when there is no content to displace.
    """
    if event is None:
        return ""
    words = event.text.split()
    if not words:
        return " ".join(event.kind.split()[:SUMMARY_WORDS])
    verb = _TASK_VERBS.get(event.kind)
    if verb is not None:
        words = [verb, *words]
    return " ".join(words[:SUMMARY_WORDS])


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
