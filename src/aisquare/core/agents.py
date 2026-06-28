"""Detect coding agents on this machine and track which are connected.

Detection is read-only: an agent is "detected" when its config directory (or a
known context file) exists. The set of connected agents is persisted in
``~/.aisquare/agents.json``. Reading an agent's context (e.g. Claude Code's
``CLAUDE.md``) is what ``agents connect`` ingests into the context pools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from aisquare.core import paths
from aisquare.models import AgentInfo


@dataclass(frozen=True)
class AgentSpec:
    """A coding agent aisquare knows how to detect."""

    name: str
    label: str
    home: Path
    context_files: tuple[Path, ...]


def _home() -> Path:
    """The user's home directory (indirection so tests can redirect it)."""
    return Path.home()


def _specs() -> list[AgentSpec]:
    home = _home()
    return [
        AgentSpec(
            "claude-code", "Claude Code", home / ".claude", (home / ".claude" / "CLAUDE.md",)
        ),
        AgentSpec("cursor", "Cursor", home / ".cursor", ()),
        AgentSpec("codex", "Codex", home / ".codex", ()),
    ]


def _spec(name: str) -> AgentSpec | None:
    return next((spec for spec in _specs() if spec.name == name), None)


def _connected_set() -> set[str]:
    path = paths.agents_registry_path()
    if not path.exists():
        return set()
    connected = json.loads(path.read_text(encoding="utf-8")).get("connected", [])
    return set(connected) if isinstance(connected, list) else set()


def set_connected(name: str, connected: bool) -> None:
    """Record (or clear) an agent's connected state in the registry."""
    paths.ensure_home()
    current = _connected_set()
    current.add(name) if connected else current.discard(name)
    paths.agents_registry_path().write_text(
        json.dumps({"connected": sorted(current)}, indent=2) + "\n", encoding="utf-8"
    )


def _to_info(spec: AgentSpec, connected: set[str]) -> AgentInfo:
    existing = [path for path in spec.context_files if path.exists()]
    return AgentInfo(
        name=spec.name,
        detected=spec.home.exists() or bool(existing),
        config_paths=existing,
        connected=spec.name in connected,
    )


def detect_all() -> list[AgentInfo]:
    """Detection state for every agent aisquare knows about."""
    connected = _connected_set()
    return [_to_info(spec, connected) for spec in _specs()]


def detect(name: str) -> AgentInfo | None:
    """Detection state for one agent, or ``None`` if the name is unknown."""
    spec = _spec(name)
    return _to_info(spec, _connected_set()) if spec is not None else None


def context_files(name: str) -> list[Path]:
    """Existing context files for an agent (its content, for ingestion)."""
    spec = _spec(name)
    return [path for path in spec.context_files if path.exists()] if spec else []
