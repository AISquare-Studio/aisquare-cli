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
from pathlib import Path, PureWindowsPath
from typing import Any

from aisquare.core import paths
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.agent_adapters.types import HookSpec, config_home
from aisquare.models import AgentHookSite, AgentInfo

CONTEXT_HOOK_TIMEOUT_SECONDS = 120
"""How long Claude Code lets the two context-producing hooks run.

Claude Code cancels a command hook at 60 s by default and DISCARDS its output.
The CI test bed's ``prompt_submit`` call may legitimately wait up to the
descriptor's ``client_safety_ms`` (60 000 today) before it degrades and prints
its own decision; under the default the agent would kill the hook first, the
context would be lost, and — worse for the data — the row recording why would
never be written. The installed timeout therefore exceeds that ceiling (seam
decision J4). With the experiment off the hooks finish in well under a second,
so this changes nothing for anyone who has not opted in.
"""

#: Hooks whose stdout becomes the agent's context. The others do bookkeeping
#: and keep Claude Code's default.
_CONTEXT_HOOKS = frozenset({"SessionStart", "UserPromptSubmit"})

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
    hooks: tuple[HookSpec, ...] = ()


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
    specs = []
    for adapter in adapters():
        directory = config_home(adapter, _home(), os.environ, config_dir)
        specs.append(
            AgentSpec(
                adapter.id,
                adapter.label,
                directory,
                adapter.context_files(directory),
                directory / adapter.settings_name,
                adapter.capabilities.hooks,
            )
        )
    # Detection of this legacy IDE entry is preserved; it has no terminal adapter.
    specs.insert(1, AgentSpec("cursor", "Cursor", _home() / ".cursor", ()))
    return specs


_PROGRAM_NAMES = frozenset({"aisquare", "asq"})


def _is_aisquare_program(token: str) -> bool:
    """Whether ``token`` names the aisquare executable itself.

    On Windows the console scripts are ``aisquare.exe`` / ``asq.EXE``, so the
    extension is stripped and the comparison is case-insensitive — the bare
    name never matches there. The Windows form is parsed with an explicit
    ``PureWindowsPath`` because ``Path`` follows the *running* platform, and
    backslashes are ordinary filename characters to a ``PosixPath``.
    """
    if Path(token).name in _PROGRAM_NAMES:
        return True
    if sys.platform != "win32":
        return False
    return PureWindowsPath(token).stem.lower() in _PROGRAM_NAMES


def _quote(path: str) -> str:
    """Quote ``path`` for the shell that will run the hook.

    POSIX quoting is wrong on Windows twice over: ``shlex.quote`` treats the
    ``\\`` in every Windows path as unsafe and wraps the whole thing in single
    quotes, which ``cmd.exe`` has no syntax for and passes through literally —
    so the hook fails to run at all. Windows gets double quotes, and only when
    the path actually needs them.
    """
    if sys.platform != "win32":
        return shlex.quote(path)
    return f'"{path}"' if " " in path else path


def _split_command(command: str) -> list[str]:
    """Tokenise a hook command the way the shell that runs it would.

    ``shlex`` in POSIX mode treats ``\\`` as an escape character, which eats
    the separators in a Windows path and leaves an unrecognisable program
    name, so Windows parses in non-POSIX mode and strips the quotes itself.
    """
    if sys.platform != "win32":
        return shlex.split(command)
    return [token.strip('"') for token in shlex.split(command, posix=False)]


def _aisquare_command() -> str:
    """The command hooks should run — an absolute path that works in any shell.

    The running executable wins: whoever installs hooks is the aisquare the
    hooks should call, even when it was invoked as ``.venv/bin/aisquare``
    without being on PATH. Falls back to PATH lookup, then to ``python -m
    aisquare`` via the current interpreter — never to a bare name a hook
    shell might not resolve.
    """
    argv0 = Path(sys.argv[0])
    if _is_aisquare_program(argv0.name) and argv0.exists():
        return _quote(str(argv0.resolve()))
    found = shutil.which("aisquare")
    if found:
        return _quote(found)
    return f"{_quote(sys.executable)} -m aisquare"


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

    This must stay the exact inverse of :func:`_aisquare_command`: if the two
    ever disagree, ``connect`` stops recognising its own hooks and appends a
    duplicate, and ``disconnect`` cannot remove them.
    """
    try:
        tokens = _split_command(command)
    except ValueError:
        return False
    if len(tokens) >= 2 and tokens[-2] == "--config-dir":
        tokens = tokens[:-2]
    if len(tokens) < 3 or tokens[-2] != "hook":
        return False
    if tokens[-1] not in {"codex", *(subcommand for _, subcommand in _HOOKS)}:
        return False
    return _is_aisquare_program(tokens[0]) or tokens[-4:-2] == ["-m", "aisquare"]


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


def _without_owned(groups: Any) -> list[Any]:
    """Remove our handlers, retaining unrelated handlers even in the same group."""
    if not isinstance(groups, list):
        return []
    kept: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        handlers = [
            item
            for item in group["hooks"]
            if not (
                isinstance(item, dict)
                and isinstance(item.get("command"), str)
                and _is_aisquare_hook_command(item["command"])
            )
        ]
        if handlers:
            kept.append({**group, "hooks": handlers})
    return kept


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    import tempfile

    payload = json.dumps(settings, indent=2) + "\n"
    # No change means no rewrite: Codex trust refers to the installed definition.
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _settings_for_write(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object; existing settings preserved")
    return value


def install_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Merge native hooks; preserve other handlers and avoid partial settings writes."""
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    settings = _settings_for_write(spec.settings_path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{spec.settings_path}: hooks must be an object")
    command = _aisquare_command()
    for hook in spec.hooks:
        groups = hooks.get(hook.event)
        kept = _without_owned(groups)
        suffix = f" --config-dir {_quote(str(spec.home))}" if hook.command == "codex" else ""
        entry: dict[str, Any] = {
            "type": "command",
            "command": f"{command} hook {hook.command}{suffix}",
        }
        if hook.timeout is not None:
            entry["timeout"] = max(hook.timeout, _installed_timeout(groups, hook.event) or 0)
        group: dict[str, Any] = {"hooks": [entry]}
        if hook.matcher is not None:
            group["matcher"] = hook.matcher
        kept.append(group)
        hooks[hook.event] = kept
    settings["hooks"] = hooks
    _write_settings(spec.settings_path, settings)
    return True


def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None or not spec.settings_path.exists():
        return False
    settings = _settings_for_write(spec.settings_path)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for hook in spec.hooks:
        groups = hooks.get(hook.event)
        kept = _without_owned(groups)
        if isinstance(groups, list) and kept != groups:
            removed = True
            if kept:
                hooks[hook.event] = kept
            else:
                hooks.pop(hook.event, None)
    if not hooks:
        settings.pop("hooks", None)
    if removed:
        _write_settings(spec.settings_path, settings)
    return removed


def ambient_hook_dir(name: str) -> Path | None:
    """The config dir a session launched from THIS shell would use.

    Resolution mirrors the agent's own: ``CLAUDE_CONFIG_DIR`` when set, the
    default home otherwise. Health recorded in the registry says nothing about
    this directory unless it happens to be registered — callers that report on
    hook health must check it in addition to the recorded sites.
    """
    spec = _spec(name)
    return _hook_dir(spec) if spec is not None else None


def hooks_installed(name: str, config_dir: Path | None = None) -> bool:
    """Whether aisquare's hooks are FULLY installed (every lifecycle event).

    A partial install (e.g. from a version that knew fewer events) returns
    False so ``doctor`` tells the user to re-run ``agents connect`` — an
    any-marker check would report healthy while Stop/Notification/SessionEnd
    silently never fire.

    A short or missing context-hook ``timeout`` is NOT counted here: the hooks
    are installed and firing, and calling that "not installed" made ``doctor``
    misdescribe a working install (a settings.json from 0.6.0, or one an
    operator hand-edited). :func:`hook_timeout_shortfall` reports that, on its
    own line, with the same fix.
    """
    spec = _spec(name, config_dir)
    return bool(spec and spec.hooks) and not _missing_events(name, config_dir, reconciled=False)


def hook_timeout_shortfall(name: str, config_dir: Path | None = None) -> list[str]:
    """Context events whose installed ``timeout`` is below what the CI hook needs.

    Empty when there is nothing to reconcile — including when the hooks are not
    installed at all, which :func:`hooks_installed` is the question for.
    """
    if not hooks_installed(name, config_dir):
        return []
    return _missing_events(name, config_dir, reconciled=True)


def _missing_events(name: str, config_dir: Path | None, *, reconciled: bool) -> list[str]:
    """Lifecycle events with no aisquare group — or, with ``reconciled``, none
    whose context timeout reaches :data:`CONTEXT_HOOK_TIMEOUT_SECONDS`."""
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None or not spec.settings_path.exists():
        return [hook.event for hook in spec.hooks] if spec else [event for event, _ in _HOOKS]
    hooks = _read_settings(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return [hook.event for hook in spec.hooks] if spec else [event for event, _ in _HOOKS]
    accepts = _is_current_aisquare_group if reconciled else (lambda g, _e: _is_aisquare_group(g))
    return [
        event
        for event in (hook.event for hook in spec.hooks)
        if not any(accepts(group, event) for group in (hooks.get(event) or []))
    ]


def _installed_timeout(groups: Any, event: str) -> int | None:
    """The ``timeout`` an existing aisquare entry for ``event`` already carries.

    Read before rewriting so ``connect`` raises a short one to our ceiling and
    leaves a longer one alone — an operator who set 180 chose more headroom
    than we need, and reconciling that down would discard their choice.
    """
    if event not in _CONTEXT_HOOKS or not isinstance(groups, list):
        return None
    for group in groups:
        if not _is_aisquare_group(group):
            continue
        for item in (group or {}).get("hooks", []) if isinstance(group, dict) else []:
            if not isinstance(item, dict) or not isinstance(item.get("command"), str):
                continue
            if not _is_aisquare_hook_command(item["command"]):
                continue
            value = item.get("timeout")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _is_current_aisquare_group(group: Any, event: str) -> bool:
    """An aisquare hook group whose context events carry a sufficient timeout.

    Presence alone reported a settings file written before the context hooks
    carried ``timeout`` as healthy, so ``doctor`` said the hooks were installed
    while Claude Code cut the CI hook off at its 60 s default.

    "Sufficient", not "equal to ours": an operator who set 180 chose a longer
    ceiling than we need and reconciling that back down to 120 would discard
    their choice. Only a missing or too-short value is a shortfall.
    """
    if not _is_aisquare_group(group):
        return False
    if event not in _CONTEXT_HOOKS:
        return True
    return any(
        isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and _is_aisquare_hook_command(item["command"])
        and isinstance(item.get("timeout"), int)
        and not isinstance(item.get("timeout"), bool)
        and item["timeout"] >= CONTEXT_HOOK_TIMEOUT_SECONDS
        for item in group.get("hooks", [])
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
    readiness, detail = integration_readiness(spec.name, _hook_dir(spec))
    return AgentInfo(
        readiness=readiness,
        detail=detail,
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


def hook_fingerprint(name: str, config_dir: Path) -> str:
    import hashlib

    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return ""
    settings = _read_settings(spec.settings_path)
    return hashlib.sha256(
        json.dumps(settings.get("hooks", {}), sort_keys=True).encode()
    ).hexdigest()


def _observation_path(name: str, config_dir: Path) -> Path:
    import hashlib

    key = hashlib.sha256(f"{name}:{config_dir.resolve()}".encode()).hexdigest()[:24]
    return paths.aisquare_home() / "cache" / f"agent-hooks-{key}.json"


def observe_hooks(name: str, config_dir: Path) -> None:
    """Evidence that the current native hook definition actually executed."""
    _write_settings(
        _observation_path(name, config_dir),
        {
            "fingerprint": hook_fingerprint(name, config_dir),
        },
    )


def integration_readiness(name: str, config_dir: Path) -> tuple[str, str]:
    if not hooks_installed(name, config_dir):
        return "not_configured", ""
    try:
        adapter = get_adapter(name)
    except ValueError:
        return "unsupported", "No terminal integration is available"
    if not adapter.capabilities.requires_hook_trust:
        return "configured", ""
    observed = _read_settings(_observation_path(name, config_dir))
    if observed.get("fingerprint") == hook_fingerprint(name, config_dir):
        return "observed", "Native hooks observed working; current session policy still applies"
    return (
        "unverified",
        "Hooks configured; open /hooks in Codex to review and trust them. "
        "AISquare has not yet observed this definition run.",
    )
