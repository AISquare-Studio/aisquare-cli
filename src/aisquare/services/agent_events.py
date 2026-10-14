"""Normalize native lifecycle observations into the shared AISquare services."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from aisquare.core import agents
from aisquare.core.agent_adapters.types import AgentAdapter, AgentEvent
from aisquare.core.claude_accounts import LimitNotice
from aisquare.core.store import store_session
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
        with store_session() as store:
            if store.get_session(session_id):
                store.touch_session(session_id, state="working")
                store.renew_leases(session_id, harness_lease())
    return None


def _usage_limit(
    adapter: AgentAdapter, event: AgentEvent, session_id: str | None
) -> LimitNotice | None:
    """The usage limit the session's transcript records at its end, or ``None``.

    The transcript is the event's ``transcript_path``, else the one the session's
    start recorded. Fails open: a transcript that cannot be read is no limit, and
    the event is handled as it always was.
    """
    try:
        if adapter.accounts is None or session_id is None:
            return None
        transcript = event.transcript_path
        if transcript is None:
            with store_session() as store:
                session = store.get_session(session_id)
            transcript = session.transcript_path if session is not None else None
        if not transcript:
            return None
        return adapter.accounts.limit_from_transcript(Path(transcript))
    except Exception:
        return None


def harness_lease() -> datetime:
    from datetime import UTC, datetime, timedelta

    from aisquare.core.orchestrator import lease_minutes

    return datetime.now(UTC) + timedelta(minutes=lease_minutes())
