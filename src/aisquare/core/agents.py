"""Detect coding agents on this machine and track which are connected.

Detection is read-only: an agent is "detected" when its config directory (or a
known context file) exists. The set of connected agents is persisted in
``~/.aisquare/agents.json``. Reading an agent's context (e.g. Claude Code's
``CLAUDE.md``) is what ``agents connect`` ingests into the context pools.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.models import AgentInfo

# Claude Code lifecycle events aisquare hooks into → the `aisquare hook` subcommand.
_HOOKS = (
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "user-prompt-submit"),
    ("SessionEnd", "session-end"),
)


@dataclass(frozen=True)
class AgentSpec:
    """A coding agent aisquare knows how to detect."""

    name: str
    label: str
    home: Path
    context_files: tuple[Path, ...]
    settings_path: Path | None = None  # where aisquare installs hooks, if supported


def _home() -> Path:
    """The user's home directory (indirection so tests can redirect it)."""
    return Path.home()


def _specs() -> list[AgentSpec]:
    home = _home()
    return [
        AgentSpec(
            "claude-code",
            "Claude Code",
            home / ".claude",
            (home / ".claude" / "CLAUDE.md",),
            settings_path=home / ".claude" / "settings.json",
        ),
        AgentSpec("cursor", "Cursor", home / ".cursor", ()),
        AgentSpec("codex", "Codex", home / ".codex", ()),
    ]


def _aisquare_command() -> str:
    """Absolute path to the aisquare executable, for use inside hook commands."""
    found = shutil.which("aisquare")
    return found or "aisquare"


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_aisquare_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    known = tuple(f"hook {subcommand}" for _, subcommand in _HOOKS)
    return any(
        isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and any(marker in item["command"] for marker in known)
        for item in hooks
    )


def install_hooks(name: str) -> bool:
    """Install aisquare's SessionStart/UserPromptSubmit hooks. False if unsupported."""
    spec = _spec(name)
    if spec is None or spec.settings_path is None:
        return False
    settings = _read_settings(spec.settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    command = _aisquare_command()
    for event, subcommand in _HOOKS:
        groups = hooks.get(event)
        kept = [g for g in groups if not _is_aisquare_group(g)] if isinstance(groups, list) else []
        kept.append({"hooks": [{"type": "command", "command": f"{command} hook {subcommand}"}]})
        hooks[event] = kept
    settings["hooks"] = hooks
    spec.settings_path.parent.mkdir(parents=True, exist_ok=True)
    spec.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def remove_hooks(name: str) -> bool:
    """Remove aisquare's hooks from the agent's settings. True if any were removed."""
    spec = _spec(name)
    if spec is None or spec.settings_path is None or not spec.settings_path.exists():
        return False
    settings = _read_settings(spec.settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for event, _ in _HOOKS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not _is_aisquare_group(g)]
        if len(kept) != len(groups):
            removed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    if removed:
        spec.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return removed


def hooks_installed(name: str) -> bool:
    """Whether aisquare's hooks are present in the agent's settings."""
    spec = _spec(name)
    if spec is None or spec.settings_path is None or not spec.settings_path.exists():
        return False
    hooks = _read_settings(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return False
    return any(
        _is_aisquare_group(group) for event, _ in _HOOKS for group in (hooks.get(event) or [])
    )


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
