"""Normalize native lifecycle observations into the shared AISquare services."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from aisquare.core import agents, launch_seal, orchestrator
from aisquare.core.agent_adapters import recorded_adapter
from aisquare.core.agent_adapters.types import AgentAdapter, AgentEvent
from aisquare.core.claude_accounts import LimitNotice
from aisquare.core.store import ContextStore, store_session
from aisquare.models import TeamSession
from aisquare.services import hooks, metrics


def session_key(adapter: AgentAdapter, config_dir: Path, native_id: str) -> str:
    """The board's id for a native session: the native id where AISquare named the
    session at launch (``assigns_session_id``, which keeps existing board references),
    else one derived from the config dir, as a native id is unique within an agent
    installation, not across vendors."""
    if adapter.capabilities.assigns_session_id:
        return native_id
    return str(uuid5(NAMESPACE_URL, f"aisquare:{adapter.id}:{config_dir.resolve()}:{native_id}"))


def handle(
    adapter: AgentAdapter,
    payload: dict[str, Any],
    config_dir: Path,
    *,
    definition: str | None = None,
) -> str:
    """One native hook call of ``adapter``'s, through the shared lifecycle services.

    ``payload`` names its event in ``hook_event_name``; ``config_dir`` is the home the
    hook ran for; ``definition`` the fingerprint a hook that needs trust was installed
    with. Returns what the hook prints (:meth:`AgentAdapter.encode_reply`): context, a
    Stop decision, or ``""``. A redelivered prompt or stop of the same turn answers as
    the first did, and is not handled twice.
    """
    event = adapter.decode_event(payload)
    if event is None:
        return ""
    session_id = session_key(adapter, config_dir, event.session_id) if event.session_id else None
    if adapter.capabilities.requires_hook_trust:
        agents.observe_hooks(adapter.id, config_dir, definition)
    cacheable = bool(session_id and event.turn_id) and event.kind in {"prompt", "stop"}
    cached_key = f"agent-event:{session_id}:{event.kind}:{event.turn_id}:{event.stop_hook_active}"
    if event.kind == "prompt":
        # A prompt submitted while a turn runs (Codex's steer) comes under that turn's
        # id (measured on 0.162.1): another prompt, not a redelivery of the first, so
        # its text is part of what makes it the same event.
        digest = hashlib.sha256((event.prompt or "").encode()).hexdigest()[:16]
        cached_key = f"{cached_key}:{digest}"
    pending = ""
    if session_id and (cacheable or not adapter.capabilities.assigns_session_id):
        # These are exact launch tokens supplied by AISquare, never a search for
        # the newest transcript. Metadata may precede the fleet row without a race.
        with store_session() as store:
            from aisquare.core.agent_sessions import bind_launch_session, own_process

            if not adapter.capabilities.assigns_session_id:
                bind_launch_session(store, session_id, own=own_process(adapter))
            if cacheable:
                pending = json.dumps(
                    {
                        "pending_at": time.time(),
                        "monotonic_at": time.monotonic(),
                        "owner": uuid4().hex,
                    }
                )
                claimed = store.set_meta_once(cached_key, pending)
                if claimed and event.kind == "prompt":
                    # A prompt in a turn whose Stop was already answered (steered in as
                    # the turn ended) reopens it: that turn's next Stop is a new one,
                    # not the redelivery the cache answers. Measured on Codex 0.162.1.
                    for active in (False, True):
                        store.delete_meta(f"agent-event:{session_id}:stop:{event.turn_id}:{active}")
                cached = store.get_meta(cached_key)
                # A failed owner may have released the row between our INSERT
                # and SELECT. We still need a claim before dispatching.
                if not claimed and cached is None and not store.set_meta_once(cached_key, pending):
                    return ""
                if not claimed and cached is not None:
                    try:
                        value = json.loads(cached)
                    except ValueError:
                        value = None
                    if isinstance(value, str):
                        return value
                    if _pending_fresh(value):
                        return ""
                    if not store.compare_meta(cached_key, cached, pending):
                        return ""
    try:
        output = adapter.encode_reply(event.kind, _dispatch(adapter, event, config_dir, session_id))
    except Exception:
        if cacheable:
            with store_session() as store:
                store.delete_meta(cached_key, expected=pending)
        raise
    if cacheable:
        with store_session() as store:
            store.compare_meta(cached_key, pending, json.dumps(output))
    return output


def _pending_fresh(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    wall = value.get("pending_at")
    monotonic = value.get("monotonic_at")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool):
        return False
    wall_age = time.time() - wall
    if monotonic is None:
        return 0 <= wall_age < 180
    if not isinstance(monotonic, (int, float)) or isinstance(monotonic, bool):
        return False
    age = time.monotonic() - monotonic
    # Monotonic is shared between processes on supported platforms, but may
    # pause during suspend. Wall time also bounds a claim across sleep; a
    # backwards wall adjustment alone does not steal an active owner's claim.
    return 0 <= age < 180 and wall_age < 180


def _dispatch(
    adapter: AgentAdapter, event: AgentEvent, config_dir: Path, session_id: str | None
) -> str | None:
    """Run ``event`` through the shared services; the reply to encode, if any."""
    cwd = event.cwd
    # The account a session runs under, where the hook names its home (else the services
    # read it off the transcript path).
    account = str(config_dir) if adapter.capabilities.hook_names_home else None
    notice = (
        _usage_limit(adapter, event, session_id)
        if adapter.capabilities.limits_in_transcript
        and event.kind in {"stop", "interrupt", "session_end"}
        else None
    )
    if notice is not None:
        # In place of the Stop (or the Interrupt's `waiting`), as StopFailure is
        # Claude Code's: parked `limited`, the manager told, and — unless the
        # session is ending, with nothing left to move — the hand-over if configured.
        hooks.turn_failed(
            session_id=session_id,
            error="rate_limit",
            message=f"{adapter.label} hit its {notice.window} limit",
            notice=notice,
            hand_over=event.kind != "session_end",
        )
        if event.kind == "session_end":
            hooks.session_ended(cwd, session_id=session_id)
            metrics.close_turn(session_id)
        return None
    if event.kind == "session_start":
        return hooks.session_start_context(
            cwd,
            session_id=session_id,
            source=event.source,
            transcript_path=event.transcript_path,
            model=event.model,
            effort=event.effort,
            agent=adapter.id,
            native_session_id=event.session_id,
            account=account,
        )
    if event.kind == "prompt":
        source = adapter.id
        if event.prompt and session_id and adapter.capabilities.stop_reason_as_prompt:
            with store_session() as store:
                expected = store.get_meta(f"continuation-prompt:{session_id}")
                if expected == hashlib.sha256(event.prompt.encode()).hexdigest():
                    source = f"{adapter.id}:continuation"
                    store.set_meta(f"continuation-prompt:{session_id}", "")
        return hooks.prompt_submitted(
            event.prompt,
            cwd,
            session_id=session_id,
            transcript_path=event.transcript_path,
            model=event.model,
            effort=event.effort,
            source=source,
            agent=adapter.id,
            native_session_id=event.session_id,
            account=account,
        )
    if event.kind == "stop":
        decision = hooks.turn_stopped(
            cwd, session_id=session_id, stop_hook_active=event.stop_hook_active
        )
        _deliver_owed_nudge(_nudge_owed_after(adapter, session_id))
        if decision is None:
            return None
        if session_id and adapter.capabilities.stop_reason_as_prompt:
            with store_session() as store:
                store.set_meta(
                    f"continuation-prompt:{session_id}",
                    hashlib.sha256(decision.reason.encode()).hexdigest(),
                )
        return decision.reason
    if event.kind == "session_end":
        hooks.session_ended(cwd, session_id=session_id, reason=event.reason)
        metrics.close_turn(session_id)
    elif event.kind == "turn_failed":
        hooks.turn_failed(
            session_id=session_id, error=event.error, message=event.message, details=event.details
        )
    elif event.kind in {"attention", "notification"}:
        hooks.needs_attention(
            cwd,
            session_id=session_id,
            message=event.message,
            notification_type=event.notification_type,
        )
    elif event.kind == "interrupt" and session_id:
        with store_session() as store:
            if store.get_session(session_id):
                store.touch_session(session_id, state="waiting")
        metrics.close_turn(session_id)
    elif event.kind == "tool_done" and session_id:
        owed = None
        with store_session() as store:
            session = store.get_session(session_id)
            if session:
                store.touch_session(session_id, state="working")
                store.renew_leases(session_id, harness_lease())
                owed = _nudge_owed(adapter, store, session.project_id)
        _deliver_owed_nudge(owed)
    return None


def _nudge_owed(adapter: AgentAdapter, store: ContextStore, project_id: str) -> str | None:
    """``project_id`` when a write of this agent's tools owed its manager a nudge
    (``services.fleet.NUDGE_OWED``), where the agent sandboxes them
    (``capabilities.sandboxes_tools``): tmux refuses its sandbox, and its hooks run
    outside it. Read in the store session the hook has open, so a hook with nothing owed
    opens the store no further time (review r4 of #113, finding 13). An agent whose
    tools run unsandboxed (Claude Code) sent its nudges itself. Never raises."""
    if not adapter.capabilities.sandboxes_tools:
        return None
    try:
        from aisquare.services import fleet  # lazy: the fleet imports the team service

        return project_id if store.get_meta(fleet.NUDGE_OWED + project_id) is not None else None
    except Exception:  # the hook's own work is done; a lost nudge is the whole cost
        return None


def _nudge_owed_after(adapter: AgentAdapter, session_id: str | None) -> str | None:
    """:func:`_nudge_owed` for ``session_id``'s project, in one store session of its own
    (a ``Stop``, whose own writes are ``hooks.turn_stopped``'s). Never raises."""
    if not adapter.capabilities.sandboxes_tools or session_id is None:
        return None
    try:
        with store_session() as store:
            session = store.get_session(session_id)
            return None if session is None else _nudge_owed(adapter, store, session.project_id)
    except Exception:
        return None


def _deliver_owed_nudge(project_id: str | None) -> None:
    """Send ``project_id``'s manager the nudge :func:`_nudge_owed` found owed
    (``services.fleet.deliver_owed_nudge``). After the hook's own writes, as a write's
    nudge comes after the write; it never raises."""
    if project_id is None:
        return
    try:
        from aisquare.services import fleet  # lazy: the fleet imports the team service

        fleet.deliver_owed_nudge(project_id)
    except Exception:  # the hook's own work is done; a lost nudge is the whole cost
        return


def _usage_limit(
    adapter: AgentAdapter, event: AgentEvent, session_id: str | None
) -> LimitNotice | None:
    """The usage limit the session's transcript records at its end, or ``None``.

    The transcript is the event's ``transcript_path``, handed to this hook by the agent,
    else the one this window's launch record holds for the session (``core.launch_seal``),
    as the agent's own hook reported it: never the board session's ``transcript_path``,
    which an agent's sandbox writes, so a transcript of its making parked the row
    ``limited`` and handed it over (review r5 of #113, finding 2). Fails open: a
    transcript that cannot be read is no limit, and the event is handled as it always was.
    """
    try:
        if adapter.accounts is None or session_id is None:
            return None
        transcript = event.transcript_path or _recorded_transcript(session_id)
        if not transcript:
            return None
        return adapter.accounts.limit_from_transcript(Path(transcript))
    except Exception:
        return None


def _recorded_transcript(session_id: str) -> str | None:
    """The transcript the launch record of this hook's window (``AISQUARE_FLEET_AGENT``)
    holds for ``session_id``; ``None`` outside the fleet, or for another session."""
    agent_id = orchestrator.env_fleet_agent()
    record = launch_seal.read(agent_id) if agent_id is not None else None
    facts = record.session if isinstance(record, launch_seal.LaunchRecord) else None
    return facts.transcript if facts is not None and facts.id == session_id else None


def failed_turn(
    agent_id: str,
    session: TeamSession,
    record: launch_seal.LaunchRecord | str | None = None,
) -> bool:
    """Record the turn ``session``'s transcript says ended on an error that fired no hook
    (``AgentAccounts.failed_turn``: Codex 0.162.1 fires no ``Stop`` for a failed turn);
    ``True`` when this call recorded it. ``session`` is the board's session of the fleet
    row ``agent_id``; ``record`` its launch record, when the caller has read it.

    The transcript read, and the agent whose reader reads it, are the row's launch
    record's (``core.launch_seal``): the session the agent's own hook reported, never
    ``session``'s ``transcript_path`` or ``agent``, which an agent's sandbox writes for any
    row — a transcript of its making ending in a usage limit parked the row ``limited``
    and detached a hand-over (review r5 of #113, finding 2). A record that holds no
    session, or another, reads nothing. A failure the transcript recorded before the
    session's last hook is an earlier turn's, read before the turn that hook's prompt
    began wrote its start: it is not this turn's to end (r5, finding 7).

    As Claude Code's ``StopFailure`` is recorded (``hooks.turn_failed``): a usage limit
    parks the session ``limited`` with the reset ``limit_from_transcript`` reads, tells the
    manager and hands a fleet agent over where ``[accounts] on_limit`` says so; any other
    error returns it to ``waiting`` with a ``turn_failed`` line. Once per turn, whoever
    reads it first — every fleet listing reads a working row's transcript
    (``services.fleet``) — under the ``agent-event:`` key of a ``turn_failed`` of that
    turn, pruned with the session's others; the limit's window is read only by the one
    that records it. Never raises: a transcript that cannot be read is no failure, and a
    record that could not be written gives the turn back to the next read.
    """
    record = launch_seal.read(agent_id) if record is None else record
    facts = record.session if isinstance(record, launch_seal.LaunchRecord) else None
    if facts is None or facts.id != session.id or not facts.transcript:
        return False
    adapter = recorded_adapter(facts.agent)
    accounts = adapter.accounts if adapter is not None else None
    if accounts is None or adapter is None or not adapter.capabilities.limits_in_transcript:
        return False
    transcript = facts.transcript
    try:
        failure = accounts.failed_turn(Path(transcript))
    except Exception:
        return False
    if failure is None or (failure.at is not None and failure.at < session.last_seen_at):
        return False
    # A turn with no id of its own is the one the prompt that stamped `last_seen_at` began.
    turn = failure.turn_id or session.last_seen_at.isoformat()
    key = f"agent-event:{session.id}:turn_failed:{turn}"
    claim = json.dumps({"pending_at": time.time(), "owner": uuid4().hex})
    try:
        with store_session() as store:
            if not store.set_meta_once(key, claim):
                return False
        limit = failure.error == "rate_limit"
        hooks.turn_failed(
            session_id=session.id,
            error=failure.error,
            message=failure.message,
            notice=accounts.limit_from_transcript(Path(transcript)) if limit else None,
        )
    except Exception:
        with suppress(Exception), store_session() as store:
            store.delete_meta(key, expected=claim)
        return False
    return True


def harness_lease() -> datetime:
    from datetime import UTC, datetime, timedelta

    from aisquare.core.orchestrator import lease_minutes

    return datetime.now(UTC) + timedelta(minutes=lease_minutes())
