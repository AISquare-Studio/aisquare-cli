"""Detection and integration of coding agents (Claude Code, etc.)."""

from __future__ import annotations

from aisquare.core import agents as agent_core
from aisquare.core.entries import new_entry
from aisquare.core.store import store_session
from aisquare.models import AgentInfo


def list_agents() -> list[AgentInfo]:
    """List agents aisquare knows about and their connection state."""
    return agent_core.detect_all()


def scan() -> list[AgentInfo]:
    """Scan this machine for installed agents."""
    return agent_core.detect_all()


def status(name: str | None = None) -> list[AgentInfo]:
    """Integration state for one agent, or all of them. Raises ``KeyError`` if unknown."""
    if name is None:
        return agent_core.detect_all()
    info = agent_core.detect(name)
    if info is None:
        raise KeyError(name)
    return [info]


def connect(name: str) -> int:
    """Pull an agent's existing context into the user pool; return entries added.

    Raises ``KeyError`` for an unknown agent and ``ValueError`` if it is not
    installed. Live capture (hooks) is not wired yet; this is a one-time ingest.
    """
    info = agent_core.detect(name)
    if info is None:
        raise KeyError(name)
    if not info.detected:
        raise ValueError(f"{name} is not installed on this machine")

    sections: list[str] = []
    for path in agent_core.context_files(name):
        sections.extend(_split_sections(path.read_text(encoding="utf-8")))

    added = 0
    with store_session() as store:
        existing = {entry.text for entry in store.entries("user")}
        for text in sections:
            if text in existing:
                continue
            store.add(new_entry(text, "user", None, [name], name))
            existing.add(text)
            added += 1
    agent_core.set_connected(name, True)
    return added


def disconnect(name: str) -> None:
    """Mark an agent disconnected. Ingested context is kept. Raises ``KeyError``."""
    if agent_core.detect(name) is None:
        raise KeyError(name)
    agent_core.set_connected(name, False)


def _split_sections(text: str) -> list[str]:
    """Split a CLAUDE.md-style document into entries on its top-level headings."""
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("# ") and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    return [section for section in sections if section]
