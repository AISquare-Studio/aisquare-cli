"""Detect coding agents on this machine and track which are connected.

Detection is read-only: an agent is "detected" when its config directory (or a
known context file) exists. The set of connected agents is persisted in
``~/.aisquare/agents.json``. Reading an agent's context (e.g. Claude Code's
``CLAUDE.md``) is what ``agents connect`` ingests into the context pools.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from aisquare.core import paths, selfcli
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.agent_adapters.types import HookSpec, config_home
from aisquare.core.version import __version__
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
    first_context_file_only: bool = False


def _home() -> Path:
    """The user's home directory (indirection so tests can redirect it)."""
    return Path.home()


def _claude_home(
    config_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Claude Code's config directory.

    Users run parallel Claude installs via ``CLAUDE_CONFIG_DIR`` (e.g. an
    alias pointing at ``~/.claude4``); hooks must land in the directory the
    actual ``claude`` command reads. Priority: explicit ``--config-dir``,
    then ``CLAUDE_CONFIG_DIR``, then ``~/.claude``.
    """
    return config_home(
        get_adapter("claude-code"), _home(), os.environ if env is None else env, config_dir
    )


def claude_json_paths(config_dir: Path) -> list[Path]:
    """Every place Claude Code could be keeping ``config_dir``'s ``.claude.json``.

    Inside the directory — where it lands whenever ``CLAUDE_CONFIG_DIR`` names
    it, so every parallel-install slot (``~/.claude-c2``) has it there — and,
    for the default ``~/.claude``, ALSO beside it at ``~/.claude.json``, which
    is where a plain install keeps it and where ``claude mcp add`` writes.

    Keyed on the DIRECTORY and never on this process's environment. A rule that
    read ``CLAUDE_CONFIG_DIR`` would answer "inside" for a recorded ``~/.claude``
    whenever the shell running doctor happened to point elsewhere — the same
    class of dead layer as probing only inside, moved to a different machine.
    Both paths are returned rather than one chosen, because a caller reading
    files can read two and a missing one costs nothing.
    """
    paths = [config_dir / ".claude.json"]
    if _dir_key(config_dir) == _dir_key(_home() / ".claude"):
        paths.append(_home() / ".claude.json")
    return paths


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
                adapter.capabilities.first_context_file_only,
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
    without being on PATH. Falls back to PATH lookup, then to ``python -P -m
    aisquare`` via the current interpreter (:func:`selfcli.argv_for`, so the
    ``-P`` that keeps a project's own ``aisquare/`` off ``sys.path`` has one
    home — #81) — never to a bare name a hook shell might not resolve. A flag
    rather than ``env PYTHONSAFEPATH=1 …`` because hooks run under ``cmd.exe``
    too, where ``env`` is not a program.
    """
    argv0 = Path(sys.argv[0])
    if _is_aisquare_program(argv0.name) and argv0.exists():
        return _quote(str(argv0.resolve()))
    found = shutil.which("aisquare")
    if found:
        return _quote(found)
    return " ".join(_quote(part) for part in selfcli.argv_for([]))


_SETTINGS_SNAPSHOT: ContextVar[dict[Path, dict[str, Any]] | None] = ContextVar(
    "agent_settings_snapshot", default=None
)


@contextmanager
def inspection_snapshot() -> Iterator[None]:
    if _SETTINGS_SNAPSHOT.get() is not None:
        yield
        return
    token = _SETTINGS_SNAPSHOT.set({})
    try:
        yield
    finally:
        _SETTINGS_SNAPSHOT.reset(token)


def _read_settings(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Optional caches fail open; hook inspection must distinguish unreadable files."""
    import copy

    snapshot = _SETTINGS_SNAPSHOT.get()
    key = path.absolute()
    if snapshot is not None and key in snapshot:
        return copy.deepcopy(snapshot[key])
    try:
        result = _read_settings_file(path, strict=True)
    except AgentSettingsError:
        if strict:
            raise
        return {}
    if snapshot is not None:
        snapshot[key] = copy.deepcopy(result)
    return result


def _read_settings_file(path: Path, *, strict: bool = False) -> dict[str, Any]:
    try:
        _regular_settings(path)
        content = path.read_text(encoding="utf-8")
        data = json.loads(content) if content.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("must contain a JSON object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        if strict:
            raise AgentSettingsError(
                f"Cannot read {path}: {exc}. Existing settings preserved; "
                "repair the file and retry."
            ) from exc
        return {}


def _is_aisquare_hook_command(command: str) -> bool:
    """Whether ``command`` is one of aisquare's own hook invocations.

    Deliberately strict: the command must *end* with ``hook <subcommand>``
    AND the invoked program must be aisquare itself (an ``aisquare``/``asq``
    executable, or ``python [-P] -m aisquare`` — the ``-m aisquare`` pair is
    matched by position, so hooks written before ``-P`` still count as ours
    and ``connect`` replaces rather than duplicates them). A bare-substring match would
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
    if len(tokens) >= 2 and tokens[-2] == "--definition":
        tokens = tokens[:-2]
    if len(tokens) >= 2 and tokens[-2] == "--config-dir":
        tokens = tokens[:-2]
    if len(tokens) < 3 or tokens[-2] != "hook":
        return False
    if tokens[-1] not in {
        hook.command for adapter in adapters() for hook in adapter.capabilities.hooks
    }:
        return False
    return _is_aisquare_program(tokens[0]) or tokens[-4:-2] == ["-m", "aisquare"]


def _owned_handlers(group: Any) -> list[dict[str, Any]]:
    """The handlers installed by AISquare, including those in mixed groups."""
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [
        item
        for item in hooks
        if isinstance(item, dict)
        and isinstance(item.get("command"), str)
        and _is_aisquare_hook_command(item["command"])
    ]


def _is_aisquare_group(group: Any) -> bool:
    return bool(_owned_handlers(group))


def _without_owned(groups: Any) -> list[Any]:
    """Remove our handlers, retaining unrelated handlers even in the same group."""
    if not isinstance(groups, list):
        return []
    kept: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        owned = _owned_handlers(group)
        if not owned:
            kept.append(group)
            continue
        handlers = [item for item in group["hooks"] if item not in owned]
        if handlers:
            kept.append({**group, "hooks": handlers})
    return kept


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    import tempfile

    snapshot = _SETTINGS_SNAPSHOT.get()
    if snapshot is not None:
        snapshot.pop(path.resolve(), None)
    payload = json.dumps(settings, indent=2) + "\n"
    existing = _regular_settings(path)
    # No change means no rewrite: Codex trust refers to the installed definition.
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return
    # Atomic replacement updates this directory entry; other hard links keep
    # their original bytes. Symlinks continue to point to the updated target.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if existing is not None:
                os.chmod(temp, stat.S_IMODE(existing.st_mode))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


class AgentSettingsError(ValueError):
    """A native settings file cannot be safely inspected or changed."""


def _regular_settings(path: Path) -> os.stat_result | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise AgentSettingsError(f"{path} is not a regular file; existing settings preserved.")
    return info


def _settings_for_write(path: Path) -> dict[str, Any]:
    value = _read_settings(path, strict=True)
    if "hooks" in value and not isinstance(value["hooks"], dict):
        raise AgentSettingsError(
            f"{path}: hooks must be an object; existing settings preserved. "
            "Repair the file and retry agents connect/disconnect."
        )
    for event, groups in value.get("hooks", {}).items():
        if not isinstance(groups, list):
            raise AgentSettingsError(
                f"{path}: hooks.{event} must be an array; existing settings preserved. "
                "Repair the file and retry agents connect/disconnect."
            )
    return value


def install_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Merge native hooks; preserve other handlers and avoid partial settings writes."""
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    settings = _settings_for_write(spec.settings_path)
    hooks = settings.get("hooks", {})
    command = _aisquare_command()
    for hook in spec.hooks:
        groups = hooks.get(hook.event)
        kept = _without_owned(groups)
        suffix = f" --config-dir {_quote(str(spec.home))}" if hook.command == "codex" else ""
        entry: dict[str, Any] = {
            "type": "command",
            "command": f"{command} hook {hook.command}{suffix}",
        }
        installed_timeout = _installed_timeout(groups, hook.event)
        if hook.timeout is not None:
            entry["timeout"] = max(hook.timeout, installed_timeout or 0)
        group: dict[str, Any] = {"hooks": [entry]}
        if hook.matcher is not None:
            group["matcher"] = hook.matcher
        kept.append(group)
        hooks[hook.event] = kept
    settings["hooks"] = hooks
    if get_adapter(name).capabilities.requires_hook_trust:
        definition = _definition_fingerprint(settings)
        for groups in hooks.values():
            for group in groups if isinstance(groups, list) else []:
                for handler in _owned_handlers(group):
                    handler["command"] = (
                        re.sub(r" --definition [0-9a-f]{64}$", "", handler["command"])
                        + f" --definition {definition}"
                    )
    _write_settings(spec.settings_path, settings)
    return True


def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
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
    if spec is None or spec.settings_path is None:
        return [hook.event for hook in spec.hooks] if spec else [event for event, _ in _HOOKS]
    hooks = _settings_for_write(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return [hook.event for hook in spec.hooks] if spec else [event for event, _ in _HOOKS]
    accepts = _is_current_aisquare_group if reconciled else (lambda g, _e: _is_aisquare_group(g))
    return [
        event
        for event in (hook.event for hook in spec.hooks)
        if not any(accepts(group, event) for group in (hooks.get(event) or []))
    ]


def _installed_timeout(groups: Any, event: str) -> int | None:
    """Preserve extra headroom only for the two context-producing hooks.

    UI and decision hooks must reconcile to their adapter's bounded timeout;
    Claude hooks without a spec timeout retain the native default.
    """
    if event not in _CONTEXT_HOOKS or not isinstance(groups, list):
        return None
    timeouts: list[int] = []
    for group in groups:
        for item in _owned_handlers(group):
            value = item.get("timeout")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                timeouts.append(value)
    return max(timeouts, default=None)


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
        isinstance(item.get("timeout"), int)
        and not isinstance(item.get("timeout"), bool)
        and item["timeout"] >= CONTEXT_HOOK_TIMEOUT_SECONDS
        for item in _owned_handlers(group)
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
    context = inspect_context(spec.name, spec.home, read_documents=False)
    sites = [
        AgentHookSite(
            config_dir=directory,
            hooks_installed=integration_readiness(spec.name, directory)[0]
            in {"configured", "observed", "unverified"},
        )
        for directory in connected_dirs(spec.name, registry)
    ]
    readiness, detail = integration_readiness(spec.name, _hook_dir(spec))
    return AgentInfo(
        readiness=readiness,
        detail=" ".join(filter(None, [detail, *context.notes])),
        name=spec.name,
        detected=spec.home.exists() or bool(context.paths),
        config_paths=context.paths,
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
    """Readable, effective context files, using the same precedence as connect."""
    documents, _ = read_context(name, config_dir)
    return list(documents)


def read_context(
    name: str,
    config_dir: Path | None = None,
) -> tuple[dict[Path, str], list[str]]:
    """Read effective instructions; missing/unreadable docs never block hook setup."""
    context = inspect_context(name, config_dir)
    return context.documents, context.notes


@dataclass
class ContextInspection:
    paths: list[Path]
    documents: dict[Path, str]
    notes: list[str]


def inspect_context(
    name: str, config_dir: Path | None = None, *, read_documents: bool = True
) -> ContextInspection:
    """Keep failed candidates visible, while ingesting only effective regular files."""
    spec = _spec(name, config_dir)
    result = ContextInspection([], {}, [])
    if spec is None:
        return result
    for path in spec.context_files:
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                result.paths.append(path)
                result.notes.append(f"Skipped context file {path}: not a regular file")
                continue
            if read_documents:
                content = path.read_text(encoding="utf-8", errors="replace")
            else:
                # Codex uses the first nonblank file. Stream only until a
                # nonblank chunk, never load large context into a status row.
                content = ""
                with path.open(encoding="utf-8", errors="replace") as handle:
                    if not spec.first_context_file_only:
                        content = "present"
                    while spec.first_context_file_only and (chunk := handle.read(4096)):
                        if chunk.strip():
                            content = "present"
                            break
        except FileNotFoundError:
            continue
        except OSError as exc:
            result.paths.append(path)
            result.notes.append(f"Skipped context file {path}: {exc}")
            continue
        if spec.first_context_file_only and not content.strip():
            continue
        result.paths.append(path)
        result.documents[path] = content
        if spec.first_context_file_only:
            break
    return result


def hook_fingerprint(name: str, config_dir: Path) -> str:
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return ""
    return _definition_fingerprint(_settings_for_write(spec.settings_path))


def _definition_fingerprint(settings: dict[str, Any]) -> str:
    import hashlib

    owned: dict[str, list[dict[str, Any]]] = {}
    hooks = settings.get("hooks", {})
    for event, groups in hooks.items() if isinstance(hooks, dict) else []:
        if not isinstance(groups, list):
            continue
        for group in groups:
            handlers = _owned_handlers(group)
            if not handlers:
                continue
            # Annotations do not affect execution. Preserve every other key,
            # including future native execution controls we do not yet know.
            annotations = {"description", "note", "comment"}
            definition = {
                key: value
                for key, value in group.items()
                if key not in annotations and key != "hooks"
            }
            definition["hooks"] = [
                {
                    key: re.sub(r" --definition [0-9a-f]{64}$", "", value)
                    if key == "command" and isinstance(value, str)
                    else value
                    for key, value in handler.items()
                    if key not in annotations
                }
                for handler in handlers
            ]
            owned.setdefault(event, []).append(definition)
    return hashlib.sha256(json.dumps(owned, sort_keys=True).encode()).hexdigest()


def _observation_path(name: str, config_dir: Path) -> Path:
    import hashlib

    key = hashlib.sha256(f"{name}:{config_dir.resolve()}".encode()).hexdigest()[:24]
    return paths.cache_dir() / f"agent-hooks-{key}.json"


def observe_hooks(name: str, config_dir: Path, definition: str | None = None) -> None:
    """Evidence that the current native hook definition actually executed."""
    if not definition:
        return  # Legacy commands carry no evidence about the definition they ran.
    # Disposable evidence cannot block context/board/Stop processing.
    # Without a successful write, readiness remains unverified.
    with suppress(OSError, UnicodeError, AgentSettingsError):
        spec = _spec(name, config_dir)
        if spec is None or spec.settings_path is None:
            return
        info = _regular_settings(spec.settings_path)
        if info is None:
            return
        stamp = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        observation = _observation_path(name, config_dir)
        previous = _read_settings(observation)
        if previous.get("settings_stamp") == stamp and previous.get("fingerprint") == definition:
            return
        if hook_fingerprint(name, config_dir) != definition:
            return
        _write_settings(
            observation,
            {"fingerprint": definition, "settings_stamp": stamp},
        )


def integration_readiness(name: str, config_dir: Path) -> tuple[str, str]:
    try:
        adapter = get_adapter(name)
    except ValueError:
        return "unsupported", "No terminal integration is available"
    try:
        if not hooks_installed(name, config_dir):
            return "not_configured", ""
    except AgentSettingsError as exc:
        return "unreadable", str(exc)
    if not adapter.capabilities.requires_hook_trust:
        return "configured", ""
    observed = _read_settings(_observation_path(name, config_dir))
    try:
        fingerprint = hook_fingerprint(name, config_dir)
    except AgentSettingsError as exc:
        return "unreadable", str(exc)
    if observed.get("fingerprint") == fingerprint:
        return "observed", "Native hooks observed working; current session policy still applies"
    return (
        "unverified",
        "Hooks configured; open /hooks in Codex to review and trust them. "
        "AISquare has not yet observed this definition run.",
    )


# --- which aisquare do the hooks actually RUN? (#84) -------------------------------------
#
# ``_is_aisquare_hook_command`` recognises a hook by its text, which is the right
# test for "is this ours to rewrite on connect/disconnect" and the wrong one for
# "is this healthy". Measured 2026-09-04: every hook in ``~/.claude`` and
# ``~/.claude3`` named ``…/aisquare-cli/.venv/bin/aisquare`` — an editable install
# of a 0.3-era checkout — while the live ``aisquare`` was 0.6.0, and doctor said
# "all lifecycle hooks installed" for weeks because the TEXT was ours. What the
# hooks run is the program the text names; whether that is this install is what
# these functions answer. Nothing here writes: doctor reports, ``connect`` fixes.

HOOK_BINARY_CURRENT = "current"
"""The hooks run this install: the console script beside this interpreter, or one
that reports the same version."""
HOOK_BINARY_STALE = "stale"
"""The hooks run an aisquare that reports a different version from this one."""
HOOK_BINARY_MISSING = "missing"
"""The program the hooks name is not on disk — every hook fails, every session."""
HOOK_BINARY_UNKNOWN = "unknown"
"""The program is on disk but its version could not be read: it did not run, exited
non-zero, or printed nothing that parses as a version."""

#: Worst-first, so a directory whose five hooks disagree is graded by its worst one.
_HOOK_BINARY_SEVERITY = {
    HOOK_BINARY_CURRENT: 0,
    HOOK_BINARY_UNKNOWN: 1,
    HOOK_BINARY_STALE: 2,
    HOOK_BINARY_MISSING: 3,
}

#: The first thing in ``aisquare --version`` output that reads as a version
#: (``0.6.0``, ``0.4.0rc1``, ``1.0.0+local``). Anchored on nothing else because
#: the prefix has already changed once and may again.
_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+[0-9A-Za-z.+!-]*")


@dataclass(frozen=True)
class HookBinary:
    """The program one hook command would start, as the hook's shell would resolve it.

    ``module_form`` is the ``<python> -m aisquare`` fallback ``_aisquare_command``
    writes when no console script is findable: then ``program`` is an
    interpreter and the install is the one in that interpreter's environment.
    """

    program: Path
    module_form: bool = False

    def version_argv(self) -> list[str]:
        argv = [str(self.program)]
        if self.module_form:
            # -P keeps the probe's cwd off sys.path (#81): doctor run from a
            # checkout that ships its own `aisquare/` package would otherwise
            # import THAT instead of the hook's install and grade a healthy
            # hook `unknown`. Same flag `selfcli.argv_for` writes into hooks.
            argv += ["-P", "-m", "aisquare"]
        return [*argv, "--version"]


@dataclass(frozen=True)
class HookSiteHealth:
    """One config directory, graded for doctor.

    ``recorded`` says whether THIS ``AISQUARE_HOME`` connected the directory; a
    site found on disk with our hooks in it is graded the same way and labelled,
    because a fresh home knows no sites and would otherwise never see them.
    ``binary_state`` is ``None`` when the directory carries no hooks of ours at
    all — there is nothing to compare.
    """

    config_dir: Path
    hooks_installed: bool
    recorded: bool
    binary: Path | None = None
    binary_version: str | None = None
    binary_state: str | None = None
    error: str | None = None


def hook_commands(name: str, config_dir: Path | None = None) -> list[str]:
    """Every aisquare hook command in the agent's settings, across all events.

    Any event counts, not only the full set ``hooks_installed`` demands: a
    partial install from an older version still RUNS on the events it has, so
    what it runs is still worth grading.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return []
    hooks = _settings_for_write(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return []
    found: list[str] = []
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not _is_aisquare_group(group):
                continue
            for item in group["hooks"]:
                command = item.get("command") if isinstance(item, dict) else None
                if isinstance(command, str) and _is_aisquare_hook_command(command):
                    found.append(command)
    return found


def _resolve_program(token: str) -> Path:
    """An absolute path for the program token, the way the hook's shell would find it.

    ``_aisquare_command`` has written absolute paths since the bare-name bug was
    fixed, but hooks installed before that are still on disk; a bare name goes
    through PATH exactly as the shell would send it, and an unfindable one is
    returned as written so it grades as missing rather than crashing.
    """
    path = Path(token).expanduser()
    if not path.is_absolute() and path.parent == Path("."):
        found = shutil.which(token)
        return Path(found) if found else path
    return path


def hook_binary(command: str) -> HookBinary | None:
    """The program ``command`` would run, or ``None`` when it is not one of our hooks."""
    if not _is_aisquare_hook_command(command):
        return None
    tokens = _split_command(command)
    if len(tokens) >= 2 and tokens[-2] == "--definition":
        tokens = tokens[:-2]
    if len(tokens) >= 2 and tokens[-2] == "--config-dir":
        tokens = tokens[:-2]
    if tokens[-4:-2] == ["-m", "aisquare"]:
        return HookBinary(_resolve_program(tokens[0]), module_form=True)
    return HookBinary(_resolve_program(tokens[0]))


def current_install() -> Path:
    """The ``aisquare`` console script beside THIS interpreter.

    What ``agents connect`` run from here would write into the hooks, and so the
    thing every hook binary is compared against. It may not exist (a checkout
    driven as ``python -m aisquare``); the directory is what the comparison uses.
    """
    name = "aisquare.exe" if sys.platform == "win32" else "aisquare"
    return Path(sys.executable).with_name(name)


def _same_install(binary: HookBinary) -> bool:
    """Whether ``binary`` is THIS program, decided from paths alone — no process.

    Identity, not neighbourhood. Two interpreters can share a directory and
    nothing else: ``/usr/bin/python3.11`` and ``/usr/bin/python3.12`` each have
    their own site-packages, so a hook on one is not "current" because doctor
    happens to run on the other — and two console scripts in one ``bin`` are no
    more alike. Sharing the directory used to skip the probe and report this
    process's version for a sibling that answers 0.3.0rc1.

    So the shortcut needs the same directory AND the same file. The directory is
    compared UNRESOLVED: every venv's ``python`` is a symlink to one base
    interpreter, so a resolved path alone would make ``~/a/.venv`` and
    ``~/b/.venv`` one install, and ``-m aisquare`` picks its package from the
    venv beside the symlink, not the target. The file is compared RESOLVED, so
    ``python`` and ``python3`` in one venv — links to the same interpreter — are
    one install. Anything else is asked its version, once per doctor run.
    """
    program = binary.program
    this = Path(sys.executable) if binary.module_form else current_install()
    if program == this:
        return True
    if program.parent != this.parent:
        return False
    try:
        return program.resolve() == this.resolve()
    except OSError:
        return False


def hook_binary_version(argv: Sequence[str], *, timeout: float = 10.0) -> str | None:
    """Run ``<program> --version`` and return the version it prints, or ``None``.

    A registered spawn seam (``core.spawn.SEAMS``), EXCLUDED and not stripped:
    it is another install of this CLI asked for a string, and ``--version`` is
    an eager callback that exits before any command runs. ``None`` for every
    way the question can fail to be answered — the program will not start, exits
    non-zero, hangs past ``timeout``, or prints nothing that reads as a version
    — so the caller reports "could not read" rather than guessing.
    """
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = _VERSION_TOKEN.search(completed.stdout)
    return match.group(0) if match else None


def classify_hook_binary(binary: HookBinary) -> tuple[str, str | None]:
    """``(state, version)`` for one hook binary, measured against this install.

    Path first, process second: the common case — hooks written by this very
    install — is decided without starting anything. Only a program in another
    directory is asked its version, and it is asked ONCE per doctor run however
    many directories name it (see ``hook_site_health``'s cache).
    """
    if not binary.program.exists():
        return HOOK_BINARY_MISSING, None
    if _same_install(binary):
        return HOOK_BINARY_CURRENT, __version__
    found = hook_binary_version(binary.version_argv())
    if found is None:
        return HOOK_BINARY_UNKNOWN, None
    return (HOOK_BINARY_CURRENT if found == __version__ else HOOK_BINARY_STALE), found


def hook_site_health(
    name: str,
    config_dir: Path,
    *,
    recorded: bool,
    cache: dict[HookBinary, tuple[str, str | None]] | None = None,
) -> HookSiteHealth:
    """Grade one config directory: are the hooks all there, and what do they run?"""
    try:
        installed = hooks_installed(name, config_dir)
        commands = hook_commands(name, config_dir)
    except AgentSettingsError as exc:
        return HookSiteHealth(config_dir, False, recorded, error=str(exc))
    binaries: list[HookBinary] = []
    for command in commands:
        binary = hook_binary(command)
        if binary is not None and binary not in binaries:
            binaries.append(binary)
    if not binaries:
        return HookSiteHealth(config_dir=config_dir, hooks_installed=installed, recorded=recorded)
    verdicts = cache if cache is not None else {}
    graded: list[tuple[HookBinary, str, str | None]] = []
    for binary in binaries:
        if binary not in verdicts:
            verdicts[binary] = classify_hook_binary(binary)
        state, version = verdicts[binary]
        graded.append((binary, state, version))
    worst, state, version = max(graded, key=lambda item: _HOOK_BINARY_SEVERITY[item[1]])
    return HookSiteHealth(
        config_dir=config_dir,
        hooks_installed=installed,
        recorded=recorded,
        binary=worst.program,
        binary_version=version,
        binary_state=state,
    )


def _agent_dirs_on_disk(name: str) -> list[Path]:
    """Native config homes on disk carrying our hooks, including account siblings.

    Read-only discovery supplements the registry for a fresh AISQUARE_HOME.
    Only homes with a recognizable AISquare command count; unreadable sibling
    settings do not hide the other sites from doctor.
    """
    try:
        adapter = get_adapter(name)
    except ValueError:
        return []
    candidates: list[Path] = []
    home = _home()
    candidates.append(config_home(adapter, home, os.environ))
    candidates.append(home / adapter.home_name)
    if home.is_dir():
        candidates.extend(
            sorted(path for path in home.glob(adapter.home_name + "*") if path.is_dir())
        )
    seen: set[Path] = set()
    found: list[Path] = []
    for candidate in candidates:
        key = _dir_key(candidate)
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        try:
            ours = bool(hook_commands(name, candidate))
        except (OSError, AgentSettingsError):
            continue  # unreadable settings.json — see the docstring
        if ours:
            found.append(candidate)
    return found


def _dir_key(path: Path) -> Path:
    """One identity for the several spellings of a directory (``~``, symlinks)."""
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def hook_sites(name: str) -> list[HookSiteHealth]:
    """Every config directory doctor should grade, each with its verdict.

    Recorded sites first (the registry's order), then the ambient directory a
    session from THIS shell would use, then anything found on disk with our
    hooks in it. The first group is ``recorded``; the rest are not — they are
    real installs this home never connected, which is the gap a fresh
    ``AISQUARE_HOME`` opens (#84). Each directory appears once however many
    lists name it.
    """
    registry = _registry()
    sites: list[tuple[Path, bool]] = []
    seen: set[Path] = set()

    def add(path: Path, *, recorded: bool) -> None:
        key = _dir_key(path)
        if key not in seen:
            seen.add(key)
            sites.append((path, recorded))

    for path in connected_dirs(name, registry):
        add(path, recorded=True)
    ambient = ambient_hook_dir(name)
    if ambient is not None and ambient.is_dir():
        add(ambient, recorded=False)
    for path in _agent_dirs_on_disk(name):
        add(path, recorded=False)

    cache: dict[HookBinary, tuple[str, str | None]] = {}
    return [
        hook_site_health(name, path, recorded=recorded, cache=cache) for path, recorded in sites
    ]
