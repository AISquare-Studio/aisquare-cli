"""Exact launch identities shared by native hooks and local MCP servers."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

from aisquare.core.orchestrator import lease_minutes
from aisquare.core.store import ContextStore
from aisquare.models import TeamSession


def launch_tokens() -> list[tuple[str, str]]:
    return [
        (prefix, token)
        for key, prefix in (
            ("AISQUARE_LAUNCH_ID", "launch"),
            ("AISQUARE_FLEET_AGENT", "fleet"),
        )
        if (token := os.environ.get(key))
    ]


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
        store.adopt_session(
            provisional_id(session.project_id, prefix, token),
            session.id,
            datetime.now(UTC) + timedelta(minutes=lease_minutes()),
        )
    return store.get_session(session.id) or session
