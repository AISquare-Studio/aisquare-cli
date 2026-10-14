"""Exact launch identities shared by native hooks and local MCP servers."""

from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from time import time as _now
from typing import TYPE_CHECKING

from aisquare.core.orchestrator import lease_minutes
from aisquare.core.store import ContextStore
from aisquare.models import TeamSession

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.types import AgentAdapter

NATIVE_METADATA_TTL = 24 * 60 * 60
METADATA_PRUNE_INTERVAL = 60
_PRUNED_AT = "native-maintenance:pruned-at"


def prune_metadata(store: ContextStore) -> None:
    """Rate-limit maintenance across hook/receiver processes sharing this store."""

    def due() -> bool:
        try:
            last = float(store.get_meta(_PRUNED_AT) or "0")
        except ValueError:
            return True
        return not 0 <= _now() - last < METADATA_PRUNE_INTERVAL

    if not due():
        return  # Ordinary hooks need only an indexed read, no writer lock.
    with store.transaction():
        if due():
            now = _now()
            store.expire_native_launches(now - NATIVE_METADATA_TTL)
            store.set_meta(_PRUNED_AT, str(now))


_LAUNCH = "launch"
_FLEET = "fleet"


def launch_tokens() -> list[tuple[str, str]]:
    return [
        (prefix, token)
        for key, prefix in (
            ("AISQUARE_LAUNCH_ID", _LAUNCH),
            ("AISQUARE_FLEET_AGENT", _FLEET),
        )
        if (token := os.environ.get(key))
    ]


#: The ``SessionStart`` source of a session the agent went back to (``/resume``).
RESUME_SOURCE = "resume"


def own_process(adapter: AgentAdapter) -> bool | None:
    """Whether this hook runs in the agent process ``aisquare launch`` started (``True``)
    or in one a tool of some agent started (``False``) — for an agent that marks the
    shells its tools run in (``capabilities.tool_shell_env``: Codex's ``CODEX_THREAD_ID``,
    which its own hooks never see and a nested ``codex exec``'s always inherit, measured
    on 0.162.1). ``None`` for an agent that does not."""
    variable = adapter.capabilities.tool_shell_env
    if variable is None:
        return None
    return not os.environ.get(variable)


def bind_launch_session(
    store: ContextStore,
    session_id: str,
    *,
    started: bool = False,
    source: str | None = None,
    own: bool | None = None,
) -> None:
    """Bind ``session_id`` to this process's launch tokens (:func:`launch_tokens`).

    The first session to send an event under a token is bound to it. A session that
    STARTS later under a launch token takes that binding over (a ``/clear`` is the
    launch's newest session), but never a fleet row's: a nested child inherits the
    window's ``AISQUARE_FLEET_AGENT`` as much as the agent's own ``/clear`` does, and
    the row moves only with its claims (``services.team._adopt``). A session started
    before takes the launch back only when the launch's own process (``own``,
    :func:`own_process`) went back to it (``source`` ``resume``): a late redelivery of
    its first start must not. A hook ``own`` says a tool started binds nothing.
    """
    if own is False:
        return
    tokens = launch_tokens()
    if not tokens:
        if started:
            with suppress(Exception):
                prune_metadata(store)
        return
    if not started and all(store.get_meta(f"{prefix}-session:{token}") for prefix, token in tokens):
        return  # ordinary events already have their exact binding
    with store.transaction():
        for prefix, token in tokens:
            binding = f"{prefix}-session:{token}"
            if started:
                seen = f"{prefix}-seen:{token}:{session_id}"
                if store.set_meta_once(seen, "1"):
                    if prefix == _FLEET:
                        store.set_meta_once(binding, session_id)
                    else:
                        store.set_meta(binding, session_id)
                else:
                    store.touch_meta(seen)
                    current = store.get_meta(binding)
                    if current is None:
                        store.set_meta(binding, session_id)
                    elif current == session_id:
                        store.touch_meta(binding)
                    elif own and source == RESUME_SOURCE and prefix != _FLEET:
                        store.set_meta(binding, session_id)
            else:
                store.set_meta_once(binding, session_id)
    if started:
        # Refresh the current binding before expiring abandoned identities.
        # A replay from an older session must never replace a newer binding.
        with suppress(Exception):
            prune_metadata(store)


def provisional_id(project_id: str, prefix: str, token: str) -> str:
    digest = hashlib.sha256(f"{project_id}\0{prefix}\0{token}".encode()).hexdigest()[:32]
    return f"mcp:local:{digest}"


def adopt_local_session(store: ContextStore, session: TeamSession) -> TeamSession:
    """Move pre-hook MCP work to the native row without releasing its claims."""
    # A launch ID distinguishes panes even when they inherited a fleet token.
    # Only the identity this MCP process would have used belongs to this join.
    tokens = launch_tokens()
    if tokens:
        prefix, token = tokens[0]
        provisional = provisional_id(session.project_id, prefix, token)
        # Most heartbeats have no provisional work. Avoid taking the WAL
        # writer lock; adoption rechecks under its transaction when needed.
        source = store.get_session(provisional)
        if source is not None and source.id == provisional:
            store.adopt_session(
                provisional, session.id, datetime.now(UTC) + timedelta(minutes=lease_minutes())
            )
    return store.get_session(session.id) or session
