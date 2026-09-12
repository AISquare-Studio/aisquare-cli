"""Exact launch identities shared by native hooks and local MCP servers."""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from aisquare.core.orchestrator import lease_minutes
from aisquare.core.store import ContextStore
from aisquare.models import TeamSession

NATIVE_METADATA_TTL = 24 * 60 * 60
METADATA_PRUNE_INTERVAL = 60
_PRUNED_AT = "native-maintenance:pruned-at"


def prune_metadata(store: ContextStore) -> None:
    """Rate-limit maintenance across hook/receiver processes sharing this store."""
    now = time.time()

    def due() -> bool:
        try:
            last = float(store.get_meta(_PRUNED_AT) or "0")
        except ValueError:
            return True
        return not 0 <= now - last < METADATA_PRUNE_INTERVAL

    if not due():
        return  # Ordinary hooks need only an indexed read, no writer lock.
    with store.transaction():
        if due():
            store.expire_native_launches(now - NATIVE_METADATA_TTL)
            store.set_meta(_PRUNED_AT, str(now))


def launch_tokens() -> list[tuple[str, str]]:
    return [
        (prefix, token)
        for key, prefix in (
            ("AISQUARE_LAUNCH_ID", "launch"),
            ("AISQUARE_FLEET_AGENT", "fleet"),
        )
        if (token := os.environ.get(key))
    ]


def bind_launch_session(store: ContextStore, session_id: str, *, started: bool = False) -> None:
    tokens = launch_tokens()
    if not tokens:
        return
    if not started and all(store.get_meta(f"{prefix}-session:{token}") for prefix, token in tokens):
        return  # ordinary events already have their exact binding
    with store.transaction():
        for prefix, token in tokens:
            binding = f"{prefix}-session:{token}"
            if started:
                seen = f"{prefix}-seen:{token}:{session_id}"
                if store.set_meta_once(seen, "1"):
                    store.set_meta(binding, session_id)
                else:
                    store.set_meta(seen, "1")  # Refresh replay history without re-binding.
            else:
                store.set_meta_once(binding, session_id)
    if started:
        # Tokenless native sessions do no launch maintenance. Bind first so a
        # resumed pane is protected before expiring abandoned identities.
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
        if source is not None and store.get_meta(f"session-alias:{provisional}") is None:
            store.adopt_session(
                provisional, session.id, datetime.now(UTC) + timedelta(minutes=lease_minutes())
            )
    return store.get_session(session.id) or session
