"""Detection and integration of coding agents (Claude Code, etc.)."""

from __future__ import annotations

from pathlib import Path

from aisquare.core import agents as agent_core
from aisquare.core.entries import new_entry
from aisquare.core.store import store_session
from aisquare.models import AgentConnection, AgentInfo


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


def connect(name: str, config_dir: Path | None = None) -> AgentConnection:
    """Install aisquare's hooks into the agent and ingest its existing context.

    Installs SessionStart/UserPromptSubmit hooks (so the agent auto-injects
    aisquare context and aisquare captures prompts), then one-time-ingests the
    agent's context files (e.g. ``~/.claude/CLAUDE.md``) into the user pool.
    Raises ``KeyError`` for an unknown agent and ``ValueError`` if not installed.
    """
    info = agent_core.detect(name, config_dir)
    if info is None:
        raise KeyError(name)
    if not info.detected:
        raise ValueError(f"{name} is not installed on this machine")

    sections: list[str] = []
    for path in agent_core.context_files(name, config_dir):
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

    hooks_installed = agent_core.install_hooks(name, config_dir)
    agent_core.set_connected(name, True, config_dir)
    return AgentConnection(name=name, hooks_installed=hooks_installed, imported=added)


def disconnect(name: str, config_dir: Path | None = None) -> bool:
    """Remove aisquare's hooks and mark the agent disconnected (ingested context kept).

    Returns whether any hooks were actually removed, so the CLI can say
    "nothing to remove here" instead of a false ✓ when the hooks live in a
    different config dir. Raises ``KeyError`` for an unknown agent.
    """
    if agent_core.detect(name, config_dir) is None:
        raise KeyError(name)
    removed = agent_core.remove_hooks(name, config_dir)
    agent_core.set_connected(name, False, config_dir)
    return removed


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
