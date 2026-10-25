"""Quiet-point compaction of a long-lived manager (P1 WS-4, #281).

Every request re-sends a manager's whole context, and nothing in aisquare compacted it: it
grew until Claude Code's own auto-compaction (~967K tokens on 1M models) or idle compaction
(about 54 idle minutes) fired, a threshold or a timer that knows nothing about the plan in
flight. A quiet point does: the manager idle at its prompt, nothing pending that would wake
it, its last turn not a question or a plan, and a state file freshly written.

Two halves, like the usage-limit hand-over:

- :func:`arm`, from the Stop hook (``services.hooks.turn_stopped``), after ``hook_stop``
  decided not to keep the manager going. It refuses at once a turn that ended on a question,
  plan mode and a row that needs the human; otherwise it starts ``aisquare hook
  compact-check`` in a session of its own and returns.
- :func:`check`, that worker. It waits ``[fleet] quiet_compact_delay`` seconds, asks every
  condition again (:func:`refusal`), asks the pane (``fleet.quiet_prompt_refusal``: its
  agent idle at its prompt, the input box empty), leaves the mark
  (``team.write_quiet_mark``) and types its agent's compaction line (:func:`compact_line`:
  Claude Code's ``/compact`` with instructions, Codex's bare) through the first message's
  verified delivery. The compaction's own SessionStart acknowledges the mark and puts the
  row back to ``waiting`` (``team._take_quiet_compaction``), or, for an agent whose
  compaction fires no hook (Codex), its transcript's record of it does
  (``AgentTranscript.compacted_since``); no acknowledgement within
  :data:`ACK_WAIT` is one human-only board line, and no retry before the cooldown.

``[fleet] quiet_compact_at = 0`` (the default) is off: nothing is armed. Claude Code's own
auto and idle compaction are never lowered from here; they stay the backstops.
Every refusal is kept, the newest, under ``team_meta`` ``compact-refused:<session>``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import paths, selfcli
from aisquare.core.agent_adapters import recorded_adapter
from aisquare.core.agent_adapters.types import AgentTranscript, CompactCommand
from aisquare.core.config import FleetSettings, load_config
from aisquare.core.store import ContextStore, store_session
from aisquare.models import FleetAgent, TeamSession
from aisquare.services import team as team_service

COMPACT_INSTRUCTIONS = (
    "Keep the goal, decisions and why, every open task by #number with owner and "
    "state, live agents by label with their task, unanswered questions, and the state file "
    "path. Drop tool output, board updates already acted on, and file contents."
)
"""What the summary keeps, for an agent whose command takes instructions."""
COMPACT_LINE = f"/compact {COMPACT_INSTRUCTIONS}"
"""What is typed into Claude Code: its ``/compact`` with the instructions its summary keeps
(:func:`compact_line`)."""


def compact_line(command: CompactCommand) -> str:
    """The line that compacts an agent's session (``capabilities.compact``): its command, with
    :data:`COMPACT_INSTRUCTIONS` where it takes them (Claude Code's), bare where text after
    it would run as a prompt instead (Codex's)."""
    return f"{command.line} {COMPACT_INSTRUCTIONS}" if command.instructions else command.line


RESUME_LINE = "Read the start pack above; if nothing needs you, stop."
"""Typed after an acknowledged compaction under ``[fleet] quiet_compact_resume``."""
ACK_WAIT = 120.0
"""How long the compaction's SessionStart has to acknowledge the mark, in seconds."""
ACK_POLL = 2.0

_sleep: Callable[[float], None] = time.sleep


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _settings() -> FleetSettings:
    return load_config().fleet


def _refuse(store: ContextStore, session_id: str, reason: str) -> str:
    store.set_meta(
        team_service.compact_refusal_key(session_id),
        json.dumps({"at": _now().isoformat(), "reason": reason}),
    )
    return reason


def arm(
    session_id: str,
    *,
    last_message: str | None,
    permission_mode: str | None,
    detach: Callable[[list[str]], None],
) -> str | None:
    """Start the quiet compaction's worker for a manager whose turn just ended, unless the
    turn itself rules it out. Returns why not, or ``None`` when the worker was started or
    the session is none of this feature's (off, not a live fleet manager).

    Every Stop of a manager writes a new arm token (:func:`team.compact_arm_key`), whether
    it arms or refuses: a worker armed by an earlier turn finds the token no longer its own
    and does nothing. A turn that ended on a question 20 s after a quiet one is the last
    turn, and the earlier worker typed ``/compact`` right after it (review of #361, 1)."""
    if _settings().quiet_compact_at <= 0:
        return None
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None or session.ended_at is not None:
            return None
        if team_service.base_role(session.role) != team_service.MANAGER_ROLE:
            return None
        agent = store.fleet_agent_for_session(session.id)
        if agent is None or agent.ended_at is not None:
            return None  # a hand-launched manager: no pane of ours to type into
        token = uuid.uuid4().hex
        store.set_meta(team_service.compact_arm_key(session.id), token)
        if (last_message or "").rstrip().endswith("?"):
            return _refuse(store, session.id, "its turn ended on a question")
        if permission_mode == "plan":
            return _refuse(store, session.id, "it is in plan mode")
        if session.state == "attention":
            return _refuse(store, session.id, "it needs the human")
        cursor = session.cursor
    detach(
        selfcli.argv_for(
            [
                "--quiet",
                "hook",
                "compact-check",
                session_id,
                "--cursor",
                str(cursor),
                "--token",
                token,
            ]
        )
    )
    return None


def _reader(session: TeamSession | None) -> AgentTranscript | None:
    """The transcript reader of ``session``'s agent (Claude Code's for a session that never
    said), or ``None``."""
    adapter = recorded_adapter(session.agent if session is not None else None)
    return adapter.transcript_reader if adapter is not None else None


def _state_age(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _last_wake(store: ContextStore, session: TeamSession, cursor: int) -> datetime | None:
    """When the newest wake-class event the manager has been handed (at or before its
    cursor) was written: its state file must be newer than the last decision it acted on."""
    for event in store.recent_events(session.project_id, limit=100):
        if event.seq <= cursor and team_service.wake_class(event, session) == "wake":
            return event.created_at
    return None


def refusal(
    store: ContextStore,
    session: TeamSession | None,
    agent: FleetAgent | None,
    cursor: int,
    settings: FleetSettings,
) -> str | None:
    """Why the manager may not be compacted now, or ``None``: every condition but the pane's.

    Still ``waiting`` (not working, limited, mid-hand-over or needing the human); its
    cursor where it was when its turn ended; no wake-class event past it; a context of at
    least ``quiet_compact_at`` tokens (its transcript's last request, as its agent's reader
    reads it: ``AgentTranscript.context_tokens``); no quiet compaction
    within the cooldown; and a state file written within ``quiet_compact_state_max_age``
    minutes and after the last decision it was woken for.
    """
    if session is None or session.ended_at is not None or agent is None:
        return "it has ended"
    if session.state != "waiting":
        return f"it reads {session.state}, not waiting"
    if session.cursor != cursor:
        return "it has read the board since its turn ended"
    for event in store.events_since(
        session.project_id, cursor, exclude_session=session.id, exclude_actor=session.id, limit=500
    ):
        if team_service.wake_class(event, session) == "wake":
            return f"a {event.kind} is pending (#{event.seq})"
    reader = _reader(session)
    size = (
        reader.context_tokens(Path(session.transcript_path))
        if reader is not None and session.transcript_path
        else None
    )
    if size is None:
        return "its context size cannot be read"
    if size < settings.quiet_compact_at:
        return f"its context is {size:,} tokens, under {settings.quiet_compact_at:,}"
    now = _now()
    mark = team_service.read_quiet_mark(store, session.id)
    if mark is not None and now - mark.typed_at < timedelta(
        minutes=settings.quiet_compact_cooldown
    ):
        return f"it was compacted {int((now - mark.typed_at).total_seconds() // 60)} min ago"
    state = paths.manager_state_file(agent.project_id)
    written = _state_age(state)
    if written is None:
        return f"it has no state file at {state}"
    if now - written > timedelta(minutes=settings.quiet_compact_state_max_age):
        return f"its state file is {int((now - written).total_seconds() // 60)} min old"
    woken = _last_wake(store, session, cursor)
    if woken is not None and written <= woken:
        return "its state file predates the last decision it was woken for"
    return None


def check(session_id: str, cursor: int, token: str) -> str:
    """The worker (``aisquare hook compact-check``): wait, ask again, type ``/compact``.

    Returns what happened, for the caller's log: ``compacted``, or why not. ``token`` is
    the one its Stop wrote (:func:`arm`): a later Stop's replaces it, and this worker stands
    down.
    """
    # Here, not at the top: the Stop hook imports this module on every turn of every agent,
    # and the fleet module is only this worker's.
    from aisquare.services import fleet as fleet_service
    from aisquare.services.delivery import TellResult

    settings = _settings()
    if settings.quiet_compact_at <= 0:
        return "quiet compaction is off"
    _sleep(settings.quiet_compact_delay)
    with store_session() as store:
        session = store.get_session(session_id)
        agent = store.fleet_agent_for_session(session_id) if session is not None else None
        if agent is not None and agent.ended_at is not None:
            agent = None
        if store.get_meta(team_service.compact_arm_key(session_id)) != token:
            # Not a refusal of the manager's state: the later turn's Stop decided, and its
            # own refusal (or its own worker) is the one kept.
            return "a later turn ended since it was armed"
        why = refusal(store, session, agent, cursor, settings)
        if why is not None:
            return _refuse(store, session_id, why)
        assert session is not None and agent is not None  # refusal() said so
        adapter = recorded_adapter(agent.agent)
        command = adapter.capabilities.compact if adapter is not None else None
        if command is None:
            return _refuse(store, session_id, "its agent has no command that compacts it")
        why = fleet_service.quiet_prompt_refusal(agent)
        if why is not None:
            return _refuse(store, session_id, why)
        typed_at = _now()
        team_service.write_quiet_mark(store, session_id, team_service.QuietMark(typed_at, None))

    reader = _reader(session)
    transcript = Path(session.transcript_path) if session.transcript_path else None

    def acked() -> bool:
        """The compaction's ``SessionStart`` acknowledged the mark (Claude Code's), or the
        transcript records a compaction since it was typed (an agent whose compaction fires
        no hook: Codex's ``compacted`` record), which acknowledges it here."""
        with store_session() as store:
            mark = team_service.read_quiet_mark(store, session_id)
            if mark is None or mark.typed_at != typed_at:
                return False
            if mark.acked_at is not None:
                return True
            if reader is None or transcript is None:
                return False
            if not reader.compacted_since(transcript, typed_at):
                return False
            team_service.write_quiet_mark(store, session_id, mark._replace(acked_at=_now()))
            return True

    done = fleet_service.type_line(agent, compact_line(command), acked=acked)
    if done.told.typed != "yes":
        told = done.told
        said = told.how if isinstance(told, TellResult) else f"{told.what}: {told.typed}"
        with store_session() as store:
            return _refuse(store, session_id, f"/compact not typed ({said})")
    waited = 0.0
    while not acked():
        if waited >= ACK_WAIT:
            team_service.hook_notice(
                session.project_id,
                f"{agent.label}: quiet compaction typed /compact and no compaction started "
                f"within {int(ACK_WAIT)} s; it is not tried again for "
                f"{int(settings.quiet_compact_cooldown)} min",
                session_id=session_id,
            )
            with store_session() as store:
                return _refuse(store, session_id, "no compaction followed /compact")
        _sleep(ACK_POLL)
        waited += ACK_POLL
    if settings.quiet_compact_resume and fleet_service.quiet_prompt_refusal(agent) is None:
        since = _now()

        def prompted() -> bool:  # its UserPromptSubmit stamps prompted_at (P0-W1a)
            with store_session() as store:
                now = store.get_session(session_id)
            return now is not None and now.prompted_at is not None and now.prompted_at >= since

        fleet_service.type_line(agent, RESUME_LINE, acked=prompted)
    return "compacted"
