"""Detect coding agents on this machine and track which are connected.

Detection is read-only: an agent is "detected" when its config directory (or a
known context file) exists. The set of connected agents is persisted in
``~/.aisquare/agents.json``. Reading an agent's context (e.g. Claude Code's
``CLAUDE.md``) is what ``agents connect`` ingests into the context pools.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.models import AgentHookSite, AgentInfo

# Claude Code lifecycle events aisquare hooks into → the `aisquare hook` subcommand.
_HOOKS = (
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "user-prompt-submit"),
    ("SessionEnd", "session-end"),
    ("Stop", "stop"),
    ("Notification", "notification"),
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


def _claude_home(config_dir: Path | None = None) -> Path:
    """Claude Code's config directory.

    Users run parallel Claude installs via ``CLAUDE_CONFIG_DIR`` (e.g. an
    alias pointing at ``~/.claude4``); hooks must land in the directory the
    actual ``claude`` command reads. Priority: explicit ``--config-dir``,
    then ``CLAUDE_CONFIG_DIR``, then ``~/.claude``.
    """
    if config_dir is not None:
        return config_dir.expanduser()
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return _home() / ".claude"


def _specs(config_dir: Path | None = None) -> list[AgentSpec]:
    home = _home()
    claude = _claude_home(config_dir)
    return [
        AgentSpec(
            "claude-code",
            "Claude Code",
            claude,
            (claude / "CLAUDE.md",),
            settings_path=claude / "settings.json",
        ),
        AgentSpec("cursor", "Cursor", home / ".cursor", ()),
        AgentSpec("codex", "Codex", home / ".codex", ()),
    ]


def _aisquare_command() -> str:
    """The command hooks should run — an absolute path that works in any shell.

    The running executable wins: whoever installs hooks is the aisquare the
    hooks should call, even when it was invoked as ``.venv/bin/aisquare``
    without being on PATH. Falls back to PATH lookup, then to ``python -m
    aisquare`` via the current interpreter — never to a bare name a hook
    shell might not resolve.
    """
    argv0 = Path(sys.argv[0])
    if argv0.name in ("aisquare", "asq") and argv0.exists():
        return shlex.quote(str(argv0.resolve()))
    found = shutil.which("aisquare")
    if found:
        return shlex.quote(found)
    return f"{shlex.quote(sys.executable)} -m aisquare"


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_aisquare_hook_command(command: str) -> bool:
    """Whether ``command`` is one of aisquare's own hook invocations.

    Deliberately strict: the command must *end* with ``hook <subcommand>``
    AND the invoked program must be aisquare itself (an ``aisquare``/``asq``
    executable, or ``python -m aisquare``). A bare-substring match would
    classify unrelated user hooks like ``webhook stop`` or ``~/bin/my-hook
    stop`` as ours and silently delete them on connect/disconnect. Parsing
    uses shlex so aisquare paths containing spaces (quoted at install time)
    keep matching.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[-2] != "hook":
        return False
    if tokens[-1] not in {subcommand for _, subcommand in _HOOKS}:
        return False
    program = Path(tokens[0]).name
    return program in ("aisquare", "asq") or tokens[-4:-2] == ["-m", "aisquare"]


def _is_aisquare_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and _is_aisquare_hook_command(item["command"])
        for item in hooks
    )


def install_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Install aisquare's lifecycle hooks. False if the agent is unsupported."""
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    settings = _read_settings(spec.settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    command = _aisquare_command()  # already shell-quoted where needed
    for event, subcommand in _HOOKS:
        groups = hooks.get(event)
        kept = [g for g in groups if not _is_aisquare_group(g)] if isinstance(groups, list) else []
        kept.append({"hooks": [{"type": "command", "command": f"{command} hook {subcommand}"}]})
        hooks[event] = kept
    settings["hooks"] = hooks
    spec.settings_path.parent.mkdir(parents=True, exist_ok=True)
    spec.settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return True


def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Remove aisquare's hooks from the agent's settings. True if any were removed."""
    spec = _spec(name, config_dir)
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


def hooks_installed(name: str, config_dir: Path | None = None) -> bool:
    """Whether aisquare's hooks are FULLY installed (every lifecycle event).

    A partial install (e.g. from a version that knew fewer events) returns
    False so ``doctor`` tells the user to re-run ``agents connect`` — an
    any-marker check would report healthy while Stop/Notification/SessionEnd
    silently never fire.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None or not spec.settings_path.exists():
        return False
    hooks = _read_settings(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return False
    return all(
        any(_is_aisquare_group(group) for group in (hooks.get(event) or [])) for event, _ in _HOOKS
    )


def _spec(name: str, config_dir: Path | None = None) -> AgentSpec | None:
    return next((spec for spec in _specs(config_dir) if spec.name == name), None)


def _registry() -> dict[str, Any]:
    """The raw agent registry, or ``{}`` when absent or unreadable."""
    path = paths.agents_registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _connected_set(registry: dict[str, Any] | None = None) -> set[str]:
    connected = (registry if registry is not None else _registry()).get("connected", [])
    return set(connected) if isinstance(connected, list) else set()


def _hook_dir(spec: AgentSpec) -> Path:
    """The directory this spec's hooks live in (its settings file's parent)."""
    return spec.settings_path.parent if spec.settings_path is not None else spec.home


def connected_dirs(name: str, registry: dict[str, Any] | None = None) -> list[Path]:
    """Every config dir ``name`` was connected in, in the order they were added.

    Registries written before multi-directory tracking recorded only a bare
    agent name, which meant "connected in the default directory" — migrate
    that reading on the fly so an old install still reports one site rather
    than none.
    """
    resolved = registry if registry is not None else _registry()
    raw = resolved.get("connections")
    dirs = raw.get(name) if isinstance(raw, dict) else None
    if isinstance(dirs, list):
        return [Path(item) for item in dirs if isinstance(item, str)]
    spec = _spec(name)
    if name in _connected_set(resolved) and spec is not None:
        return [_hook_dir(spec)]
    return []


def set_connected(name: str, connected: bool, config_dir: Path | None = None) -> None:
    """Record (or clear) an agent's connected state for one config directory.

    Connecting the same agent in several directories accumulates sites;
    disconnecting removes only the targeted one, and the agent stops counting
    as connected once its last site is gone.
    """
    paths.ensure_home()
    registry = _registry()
    names = _connected_set(registry)
    sites = {agent: connected_dirs(agent, registry) for agent in {*names, name}}

    spec = _spec(name, config_dir)
    target = _hook_dir(spec) if spec is not None else None
    current = sites.get(name, [])
    if connected:
        if target is not None and target not in current:
            current.append(target)
        names.add(name)
    else:
        current = [path for path in current if path != target]
        if not current:
            names.discard(name)
    sites[name] = current

    paths.agents_registry_path().write_text(
        json.dumps(
            {
                # `connected` stays for compatibility with readers (and older
                # aisquare versions) that only know the boolean form.
                "connected": sorted(names),
                "connections": {
                    agent: [str(path) for path in dirs] for agent, dirs in sorted(sites.items())
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _to_info(spec: AgentSpec, registry: dict[str, Any]) -> AgentInfo:
    existing = [path for path in spec.context_files if path.exists()]
    sites = [
        AgentHookSite(
            config_dir=directory,
            hooks_installed=hooks_installed(spec.name, directory),
        )
        for directory in connected_dirs(spec.name, registry)
    ]
    return AgentInfo(
        name=spec.name,
        detected=spec.home.exists() or bool(existing),
        config_paths=existing,
        connected=spec.name in _connected_set(registry),
        sites=sites,
    )


def detect_all() -> list[AgentInfo]:
    """Detection state for every agent aisquare knows about."""
    registry = _registry()
    return [_to_info(spec, registry) for spec in _specs()]


def detect(name: str, config_dir: Path | None = None) -> AgentInfo | None:
    """Detection state for one agent, or ``None`` if the name is unknown."""
    spec = _spec(name, config_dir)
    return _to_info(spec, _registry()) if spec is not None else None


def context_files(name: str, config_dir: Path | None = None) -> list[Path]:
    """Existing context files for an agent (its content, for ingestion)."""
    spec = _spec(name, config_dir)
    return [path for path in spec.context_files if path.exists()] if spec else []
