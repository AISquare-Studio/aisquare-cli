"""Detect coding agents on this machine and track which are connected.

Detection is read-only: an agent is "detected" when its config directory (or a
known context file) exists. The set of connected agents is persisted in
``~/.aisquare/agents.json``. Reading an agent's context (e.g. Claude Code's
``CLAUDE.md``) is what ``agents connect`` ingests into the context pools.
"""

from __future__ import annotations

import errno
import functools
import importlib.metadata
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

from aisquare.core import atomic, paths, selfcli
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.agent_adapters.claude_code import ClaudeCodeAdapter
from aisquare.core.agent_adapters.types import (
    GENERIC_HOOK,
    AgentAdapter,
    AgentPlugin,
    HookSpec,
    HooksSwitch,
    PluginInstall,
    hook_args,
)
from aisquare.core.agent_adapters.types import version_in as native_version
from aisquare.core.version import DISTRIBUTION, __version__
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

#: Claude Code's lifecycle events → the ``aisquare hook`` subcommand, as its adapter installs
#: them (``ClaudeCodeAdapter.capabilities.hooks``). ``StopFailure`` fires INSTEAD of Stop
#: when the turn ends on an API error — a usage limit above all (#146); an install from
#: before an event reads as partial, and ``doctor`` says to re-run ``agents connect``.
_HOOKS = tuple((hook.event, hook.command) for hook in ClaudeCodeAdapter.capabilities.hooks)

#: The Claude Code plugin route (``plugins/claude-code`` in this repo): the plugin and
#: the marketplace that lists it, as ``/plugin install aisquare@aisquare-cli`` names
#: them and ``enabledPlugins`` keys them. Its hooks run the same ``aisquare hook
#: <event>`` as :data:`_HOOKS`, and stand down for any event settings.json already
#: runs. tests/test_claude_plugin.py holds both manifests to these names.
_CLAUDE_CODE = ClaudeCodeAdapter.id
"""Claude Code's id, for the readers here that are Claude Code's alone (its plugin files,
its ``~/.claude.json``)."""

CLAUDE_PLUGIN = "aisquare"
CLAUDE_PLUGIN_MARKETPLACE = "aisquare-cli"
CLAUDE_PLUGIN_ID = f"{CLAUDE_PLUGIN}@{CLAUDE_PLUGIN_MARKETPLACE}"


@dataclass(frozen=True)
class AgentSpec:
    """A coding agent aisquare knows how to detect, and to connect when it has hooks for it."""

    name: str
    label: str
    home: Path
    context_files: tuple[Path, ...]
    settings_path: Path | None = None  # where aisquare installs hooks, if supported
    hooks: tuple[HookSpec, ...] = ()
    first_context_file_only: bool = False
    planned: str | None = None
    """The release planned to connect this agent, while aisquare can only detect it."""

    @property
    def connectable(self) -> bool:
        """Whether ``agents connect`` installs anything: aisquare has hooks for this agent."""
        return self.settings_path is not None


def _home() -> Path:
    """The user's home directory (indirection so tests can redirect it)."""
    return Path.home()


def home() -> Path:
    """:func:`_home` for callers outside this module (``services.lifecycle``).

    The home every path here is under, so a test that redirects it redirects theirs.
    """
    return _home()


def xdg_cache_home(value: str | None = None) -> Path:
    """Where a user's caches go: ``$XDG_CACHE_HOME``, else ``~/.cache`` under :func:`home`.

    ``value`` stands for the variable, this process's when ``None``. One that is unset,
    empty or relative is ignored, as the XDG spec says and uv does. The one rule for the
    plugin's answers (``lifecycle.plugin_answers``), which the plugin's launcher keeps
    too, and for uv's own cache (``install_route._uv_cache_roots``), so a test that
    redirects the home moves both (round 27 of #257).
    """
    if value is None:
        value = os.environ.get("XDG_CACHE_HOME", "")
    if value and os.path.isabs(value):
        return Path(value)
    return home() / ".cache"


def _claude_home(
    config_dir: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Claude Code's config directory.

    Users run parallel Claude installs via ``CLAUDE_CONFIG_DIR`` (e.g. an
    alias pointing at ``~/.claude4``); hooks must land in the directory the
    actual ``claude`` command reads. Priority: explicit ``--config-dir``,
    then ``CLAUDE_CONFIG_DIR`` (in ``env``, this process's when ``None``), then
    ``~/.claude``. As written, links and all: a link that leads nowhere is named as the
    link (``agent_adapters.types.config_home`` resolves another agent's).
    """
    if config_dir is not None:
        return paths.expand_user(config_dir)
    value = (os.environ if env is None else env).get("CLAUDE_CONFIG_DIR", "").strip()
    if value:
        return paths.expand_user(Path(value))
    return _home() / ".claude"


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
        directory = adapter.config_dir(_home(), os.environ, config_dir)
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
    # Detected only: no terminal adapter and no hooks, so `agents connect` refuses it.
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


def _aisquare_command(isolated: bool = False) -> str:
    """The command hooks should run — an absolute path that works in any shell.

    ``isolated`` for an agent that sandboxes its tools (Codex): ``<this interpreter> -E -P
    -m aisquare`` (:func:`selfcli.isolated_argv_for`), whatever else would be written, as
    such an agent's hooks run outside its sandbox in its environment, whose
    ``PYTHONPATH`` the agent's own tools need and its sandbox may write (an empty entry
    is the workspace); ``-E`` keeps the hook's interpreter from importing from it, and a
    console script cannot carry the flag (review r14 of #113, finding 1). The doctor
    grades the module form by its interpreter (:func:`_same_install`).

    The running executable wins: whoever installs hooks is the aisquare the
    hooks should call, even when it was invoked as ``.venv/bin/aisquare``
    without being on PATH. Run as ``python -m aisquare`` (every button that
    runs the CLI, through :mod:`selfcli`), that is the console script this
    distribution installed (:func:`current_install`), then PATH lookup; either only
    when it can start (:func:`_starts`), or a stale install first on PATH whose
    Python is gone was written back and Connect never cleared "cannot start"
    (review of #257). Then ``python -P -m
    aisquare`` via the current interpreter (:func:`selfcli.argv_for`, so the
    ``-P`` that keeps a project's own ``aisquare/`` off ``sys.path`` has one
    home — #81) — never to a bare name a hook shell might not resolve. A flag
    rather than ``env PYTHONSAFEPATH=1 …`` because hooks run under ``cmd.exe``
    too, where ``env`` is not a program.
    """
    if isolated:
        return " ".join(_quote(part) for part in selfcli.isolated_argv_for([]))
    argv0 = Path(sys.argv[0])
    if _is_aisquare_program(argv0.name) and argv0.exists():
        return _quote(str(argv0.resolve()))
    installed = current_install()
    if _starts(installed):
        return _quote(str(installed))
    found = shutil.which("aisquare")
    if found and _starts(Path(found)):
        return _quote(found)
    return " ".join(_quote(part) for part in selfcli.argv_for([]))


class AgentSettingsError(ValueError):
    """A native settings file cannot be safely inspected or changed."""


class SettingsNotAnObjectError(AgentSettingsError):
    """A ``settings.json`` whose text is not a JSON object, which the hook writers leave alone.

    ``install_hooks`` edits the object it reads and writes it back. Text that did
    not parse was read as ``{}``, so one trailing comma cost the user every other
    setting: the file came back holding only ``hooks`` (review of #257). The
    message names the file and what is wrong with it.
    """


def read_settings(path: Path) -> dict[str, Any]:
    """The object in ``path`` that the hook writers edit: ``{}`` when there is no file.

    The writers' reader, deliberately not :func:`read_json`, because what it returns
    is written back. Text that is not a JSON object raises
    :class:`SettingsNotAnObjectError`, and a file that cannot be read raises its
    ``OSError`` or ``UnicodeDecodeError``. An empty file holds nothing to lose.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SettingsNotAnObjectError(
            f"can't read {path}: it is not valid JSON "
            f"({exc.msg}, line {exc.lineno} column {exc.colno})"
        ) from exc
    if not isinstance(data, dict):
        raise SettingsNotAnObjectError(f"can't read {path}: it is not a JSON object")
    return data


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
    key = _dir_key(path)
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
    """:func:`read_settings` for inspection: every failure an :class:`AgentSettingsError`."""
    try:
        _regular_settings(path)
        return read_settings(path)
    except FileNotFoundError:
        return {}
    except AgentSettingsError:
        if strict:
            raise
        return {}
    except (OSError, UnicodeError) as exc:
        if strict:
            raise AgentSettingsError(
                f"Cannot read {path}: {exc}. Existing settings preserved; "
                "repair the file and retry."
            ) from exc
        return {}


#: The options ``hook_args`` puts after the subcommand, and ``--definition`` (a hook that
#: needs trust), in the order they are written back.
_HOOK_OPTIONS = ("--definition", "--agent", "--event", "--config-dir")


def _hook_command_parts(command: str) -> tuple[list[str], dict[str, str]]:
    """Separate trailing hook options, independent of their order."""
    tokens = _split_command(command)
    options: dict[str, str] = {}
    while len(tokens) >= 2 and tokens[-2] in _HOOK_OPTIONS:
        options.setdefault(tokens[-2], tokens[-1])
        tokens = tokens[:-2]
    return tokens, options


def _without_hook_definition(command: str) -> str:
    tokens, options = _hook_command_parts(command)
    for option in _HOOK_OPTIONS[1:]:
        if option in options:
            tokens += [option, options[option]]
    return " ".join(_quote(token) for token in tokens)


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Write ``settings`` back to ``path`` the way Claude Code writes the file: UTF-8.

    ``json.dumps`` escapes every non-ASCII character unless told not to, so a hook
    naming ``~/Développement/…/aisquare`` was stored as ``D\\u00e9veloppement``. The
    plugin's launcher reads the file as text and cannot read a program out of an
    escape, so it trusted such a hook: when that program was deleted it stood down
    beside a hook that fails on every event, and no aisquare ran at all (review of
    #257). A lone surrogate (a path that is not UTF-8) cannot be written as UTF-8 and
    keeps the escape ``json.dumps`` would give it.

    Replaced, not rewritten in place: a write cut off partway (a full disk, a kill during
    ``connect`` or ``uninstall``) left a truncated file that is not JSON, which every
    surface then refuses and Claude Code runs without, and a session reading it between
    the truncate and the write saw it empty (round 19 of #257). Through a link, the file
    it points at is replaced, as ``save_config`` does. A file this user may not write is
    not replaced either.

    In place, as before, wherever a new file renamed over it would not be the same file
    to everyone else (round 20): where its folder takes no new file; where the rename is
    refused because the file is a mount point of its own (``EBUSY``, ``EXDEV``: a
    container's single-file bind mount); and where :func:`_as_it_was` finds the temp
    differs, in its links, owner, group, device or Windows DACL.

    No change means no rewrite: Codex trust refers to the installed definition.
    """
    snapshot = _SETTINGS_SNAPSHOT.get()
    if snapshot is not None:
        snapshot.pop(_dir_key(path), None)
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    body = text.encode("utf-8", errors="backslashreplace").decode("utf-8")
    if _regular_settings(path) is not None:
        with suppress(OSError, UnicodeError):
            if path.read_text(encoding="utf-8") == body:
                return
    target = Path(os.path.realpath(path))
    there = os.path.exists(target)
    if there and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
    made = in_place = False
    try:
        with atomic.replacement(target) as pending:
            made = True
            if there and not _as_it_was(target, pending.temporary):
                in_place = True  # leaving without a publish removes the temp
            else:
                pending.publish(body)
    except PermissionError:
        if made or not there:
            raise
        in_place = True  # no temp could be made beside it: the folder takes no new file
    except OSError as exc:
        if not there or exc.errno not in {errno.EBUSY, errno.EXDEV}:
            raise
        in_place = True  # renamed over, the file is a mount point and stays one
    if in_place:
        target.write_text(body, encoding="utf-8")


def _as_it_was(target: Path, temporary: Path) -> bool:
    """Whether ``temporary``, renamed over ``target``, is the same file to everyone else.

    Not when ``target`` has a second name (a dotfiles tool that hard-links): the rename
    splits the link, and the other name keeps the old hooks. Not when the temp's owner or
    group is another than the file's: a file another user owns, which this user writes
    through its group, would become this user's, and the group's write would go with it.
    Nor across devices, nor on Windows when the temp's DACL, the one its folder hands
    down, is not the file's own: a file whose DACL denies this user writing would be
    replaced all the same, as ``os.access`` there reads the read-only flag alone
    (round 20 of #257). Unreadable is not the same.
    """
    try:
        was, new = target.stat(), temporary.stat()
    except OSError:
        return False
    if was.st_nlink > 1:
        return False
    if (was.st_uid, was.st_gid, was.st_dev) != (new.st_uid, new.st_gid, new.st_dev):
        return False
    if sys.platform == "win32":
        dacl = paths.dacl_sddl(target)
        return dacl is not None and dacl == paths.dacl_sddl(temporary)
    return True


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
        tokens, _ = _hook_command_parts(command)
    except ValueError:
        return False
    if len(tokens) < 3 or tokens[-2] != "hook":
        return False
    if tokens[-1] not in {
        GENERIC_HOOK,
        *(hook.command for adapter in adapters() for hook in adapter.capabilities.hooks),
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


def _regular_settings(path: Path) -> os.stat_result | None:
    """``path``'s stat, ``None`` where nothing is there (:func:`nothing_there`), refused as
    :class:`AgentSettingsError` when it is not a regular file."""
    try:
        info = path.stat()
    except OSError as exc:
        if nothing_there(exc):
            return None
        raise
    if not stat.S_ISREG(info.st_mode):
        raise AgentSettingsError(f"{path} is not a regular file; existing settings preserved.")
    return info


def _adapter(name: str) -> AgentAdapter | None:
    """The adapter registered as ``name``; ``None`` for an agent aisquare only detects."""
    try:
        return get_adapter(name)
    except ValueError:
        return None


def hooks_shape_strict(name: str) -> bool:
    """Whether ``name``'s hooks file is refused, never rewritten, when its ``hooks`` is not an
    object of arrays (``capabilities.strict_hooks_file``: Codex's ``hooks.json``, #113).
    Claude Code's ``settings.json`` reads such a value as no groups, and ``install_hooks``
    replaces it with ours."""
    adapter = _adapter(name)
    return adapter is not None and adapter.capabilities.strict_hooks_file


def _hooks_of_their_shape(path: Path, value: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    """``value``, refused when ``strict`` and its ``hooks`` is not an object of arrays."""
    if not strict:
        return value
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


def settings_for_write(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """What the hook writers edit (:func:`read_settings`, which raises as it says), refused
    as :class:`AgentSettingsError` when it is not a regular file or, ``strict``
    (:func:`hooks_shape_strict`), its hooks have another shape. ``services.agents`` reads
    it before anything is written, so ``agents connect`` refuses what ``install_hooks``
    would."""
    _regular_settings(path)
    return _hooks_of_their_shape(path, read_settings(path), strict=strict)


def _inspected_settings(path: Path, *, strict: bool) -> dict[str, Any]:
    """:func:`settings_for_write` for a reader that must tell an unreadable file apart:
    through the inspection snapshot, every failure an :class:`AgentSettingsError`."""
    return _hooks_of_their_shape(path, _read_settings(path, strict=True), strict=strict)


def _without_aisquare(groups: list[Any]) -> list[Any]:
    """``groups`` with aisquare's hook ENTRIES taken out, and only emptied groups dropped.

    A group can hold a user's command beside ours, from a hand edit or another
    tool that appends to the group it finds. Dropping every group that held one
    of ours took the user's command with it (review of #253). So each group
    keeps its other entries and its other keys (``matcher``); a group goes only
    when nothing of the user's is left in it.
    """
    kept: list[Any] = []
    for group in groups:
        if not _is_aisquare_group(group):
            kept.append(group)
            continue
        others = [
            item
            for item in group["hooks"]
            if not (
                isinstance(item, dict)
                and isinstance(item.get("command"), str)
                and _is_aisquare_hook_command(item["command"])
            )
        ]
        if others:
            kept.append({**group, "hooks": others})
    return kept


def install_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Merge the agent's native hooks, keeping every other handler. False if unsupported.

    Raises :class:`SettingsNotAnObjectError` for a settings file it must not
    rewrite, :class:`AgentSettingsError` for one that is not a regular file or whose
    hooks have another shape where that is refused (:func:`hooks_shape_strict`), and
    ``OSError`` or ``UnicodeDecodeError`` for one it cannot read.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    # Raises rather than lose what is in it.
    settings = settings_for_write(spec.settings_path, strict=hooks_shape_strict(name))
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    adapter = get_adapter(name)
    # already shell-quoted where needed
    command = _aisquare_command(isolated=adapter.capabilities.sandboxes_tools)
    for hook in spec.hooks:
        groups = hooks.get(hook.event)
        kept = _without_aisquare(groups) if isinstance(groups, list) else []
        argv = " ".join(_quote(arg) for arg in hook_args(adapter, hook, spec.home))
        # The agent's own handler shape, the timeout in its own unit (Gemini CLI's is ms).
        entry = adapter.hook_entry(hook, f"{command} hook {argv}")
        if isinstance(entry.get("timeout"), int):
            # Never below the ceiling the CI hook may wait for; never *reducing*
            # a longer one the operator chose deliberately.
            entry["timeout"] = max(entry["timeout"], _installed_timeout(groups, hook.event) or 0)
        group: dict[str, Any] = {"hooks": [entry]}
        if hook.matcher is not None:
            group["matcher"] = hook.matcher
        kept.append(group)
        hooks[hook.event] = kept
    settings["hooks"] = hooks
    if adapter.capabilities.requires_hook_trust:
        definition = _definition_fingerprint(settings)
        for groups in hooks.values():
            for group in groups if isinstance(groups, list) else []:
                for handler in _owned_handlers(group):
                    handler["command"] = (
                        _without_hook_definition(handler["command"]) + f" --definition {definition}"
                    )
    if paths.names_no_home(spec.settings_path):
        # A `~olduser/.claude` read as written (paths.expand_user): made, it landed in the
        # cwd, and connect said ✓ (sweep of #257).
        raise FileNotFoundError(
            errno.ENOENT, "no such home on this machine", str(spec.settings_path)
        )
    spec.settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_settings(spec.settings_path, settings)
    return True


def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Remove aisquare's hooks from the agent's settings. True if any were removed.

    From every event, as :func:`hook_commands` finds them: one under an event this
    version does not install (an older install's, a hand-added one, another agent's
    ``hook`` subcommand) is still ours, and left behind it failed every uninstall.

    A file that is not a JSON object, or not UTF-8, holds nothing of ours to take out and
    is left alone; where such a file is refused (:func:`hooks_shape_strict`), it and one
    whose hooks have another shape raise :class:`AgentSettingsError`.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    try:
        settings = settings_for_write(spec.settings_path, strict=hooks_shape_strict(name))
    except (SettingsNotAnObjectError, UnicodeDecodeError) as exc:
        if not hooks_shape_strict(name):
            # Nothing of ours can be found in it to take out, and it is left alone. One that
            # holds our hooks all the same is refused before this (services.agents.access).
            return False
        if isinstance(exc, SettingsNotAnObjectError):
            raise
        raise AgentSettingsError(f"can't read {spec.settings_path}: it is not UTF-8 text") from exc
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        kept = _without_aisquare(groups)
        if kept != groups:
            removed = True
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
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


#: Each agent's switch for every hook, as messages that name it without a file write it.
HOOKS_OFF_SETTINGS = {adapter.id: adapter.hooks_off_setting for adapter in adapters()}


def hooks_switch(
    name: str, config_dir: Path | None = None, args: Sequence[str] = ()
) -> HooksSwitch | None:
    """What switches every hook off for ``name`` in ``config_dir``, launched with ``args``
    (:meth:`AgentAdapter.hooks_switch`: Claude Code's ``"disableAllHooks": true``, Codex's
    ``features.hooks = false``). The agent then runs no hook from that directory, so none
    of ours fire however complete they are, and ``agents connect`` leaves the setting
    alone: Connect cannot change it. Read-only; never raises.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return None
    return get_adapter(name).hooks_switch(spec.home, spec.settings_path, args)


def hooks_disabled(name: str, config_dir: Path | None = None) -> bool:
    """Whether the agent's own config switches every hook off (:func:`hooks_switch`)."""
    return hooks_switch(name, config_dir) is not None


def hooks_off(name: str, config_dir: Path | None = None) -> Path | None:
    """The file that switches every hook off (:func:`hooks_switch`), else ``None``.

    What `agents connect`, `agents list` and `agents status` name in place of a
    connection or a "missing": the cause is that setting, and Connect cannot change it.
    """
    switch = hooks_switch(name, config_dir)
    return switch.path if switch is not None else None


def connected(name: str, config_dir: Path | None = None, *, cwd: Path | None = None) -> bool:
    """Whether ``name`` in ``config_dir`` runs aisquare: the one "connected?" answer.

    ``cwd`` is the folder a session would start in, for a project- or local-scope
    plugin (:meth:`AgentPlugin.repo_install_here`); this process's working directory
    unless given.

    Two routes, in a directory whose own config does not switch hooks off
    (:func:`hooks_switch`): every lifecycle hook ``agents connect`` installs is in its
    hooks file, or, for an agent with a plugin route (:func:`plugin_of`), the aisquare
    plugin is installed and enabled there, or, where that file has none of our hooks,
    installed at project or local scope for the repository a session started in ``cwd``
    loads it from. The switch comes first because it silences every route, the
    plugin's included. Never raises.
    """
    if hooks_disabled(name, config_dir):
        return False
    if hooks_installed(name, config_dir):
        return True
    plugin = plugin_of(name)
    if plugin is None:
        return False
    if plugin.installed(config_dir) is not None and (
        plugin.stands_down_per_event or not plugin_stands_down(name, config_dir)
    ):
        # A launcher that stands down only where the hooks file runs an event covers a
        # partial install; one that stands down beside any runnable hook of ours covers
        # it only where none can start (its hooks run in their place).
        return True
    if hook_commands(name, config_dir):
        # Its own hooks, partial ones included, are what every other repository runs on;
        # a repository's plugin covers its repository alone (as in `hook_site_health`).
        # Counted, five of six hooks read "all lifecycle hooks installed" there, with no
        # Connect to add the sixth (review of #257).
        return False
    return plugin.repo_install_here(config_dir, cwd) is not None


def claude_code_connected(config_dir: Path | None = None, *, cwd: Path | None = None) -> bool:
    """Whether Claude Code in ``config_dir`` runs aisquare (:func:`connected`).

    ``services.agents.claude_code_connected`` is its public face and says who asks
    and why. It is implemented here so this module's own per-directory readers ask
    it too: ``_to_info`` reports it as each site's ``hooks_installed`` in ``agents
    list`` and ``agents status``.
    """
    return connected(_CLAUDE_CODE, config_dir, cwd=cwd)


def plugin_of(name: str) -> AgentPlugin | None:
    """``name``'s aisquare plugin route where it runs on this platform, else ``None``."""
    adapter = _adapter(name)
    plugin = adapter.plugin if adapter is not None else None
    return plugin if plugin is not None and plugin.supported() else None


def claude_repo_plugin_here(
    config_dir: Path | None = None, cwd: Path | None = None
) -> ClaudePlugin | None:
    """The project- or local-scope install (:func:`claude_repo_plugins`) that a session
    of ``config_dir`` started in ``cwd`` loads, else ``None``. ``cwd`` is this process's
    working directory unless given: the current project.

    Claude Code 2.1.294's settings loader reads project settings from the directory
    the session starts in, never a parent, so a project-scope install counts only
    there; and local settings from :func:`_local_settings_root`. Told "not connected"
    in that repository, a user with only such an install was offered Connect, which
    installs the settings.json hooks beside it (review of #257). Read-only; never
    raises.
    """
    try:
        here = _dir_key(cwd if cwd is not None else Path.cwd())
    except OSError:
        return None  # the working directory was removed
    plugins = claude_repo_plugins(config_dir)
    if not plugins:
        return None
    reads = {"project": here, "local": _local_settings_root(here)}
    for plugin in plugins:
        if plugin.project is not None and _dir_key(plugin.project) == reads.get(plugin.scope):
            return plugin
    return None


def _local_settings_root(directory: Path) -> Path:
    """Where Claude Code reads ``.claude/settings.local.json`` for a session started in
    ``directory`` (resolved): the root of the git repository it is in, when that root,
    its ``.git`` and its ``.claude`` belong to this user and it is not the home
    directory; else ``directory`` itself (Claude Code 2.1.294).

    Claude Code follows a linked worktree to its main repository; a ``.git`` that is
    not a directory is not followed here, so such an install reads as not loaded.
    """
    geteuid = getattr(os, "geteuid", None)
    for root in (directory, *directory.parents):
        try:
            git = os.lstat(root / ".git")
        except OSError:
            continue
        if geteuid is None or not stat.S_ISDIR(git.st_mode) or root == _dir_key(_home()):
            return directory
        try:
            owners = [os.stat(root).st_uid, git.st_uid]
            if os.path.lexists(root / ".claude"):
                owners.append(os.lstat(root / ".claude").st_uid)
        except OSError:
            return directory
        return root if all(owner == geteuid() for owner in owners) else directory
    return directory


def _missing_events(name: str, config_dir: Path | None, *, reconciled: bool) -> list[str]:
    """Lifecycle events with no aisquare group — or, with ``reconciled``, none
    whose context timeout reaches :data:`CONTEXT_HOOK_TIMEOUT_SECONDS`.

    Read as :func:`read_json` reads, this module's rule for a file it only reads (through
    the inspection snapshot, :func:`_read_settings`): a ``settings.json`` that is missing,
    unreadable, not UTF-8 or a directory holds no hooks. Read through
    :func:`read_settings`, which must raise for the writers, each of those cost
    ``aisquare doctor`` its whole report.
    """
    spec = _spec(name, config_dir)
    if spec is None:
        return [event for event, _ in _HOOKS]
    events = [hook.event for hook in spec.hooks]
    if spec.settings_path is None:
        return events
    hooks = _read_settings(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return events
    accepts = _is_current_aisquare_group if reconciled else (lambda g, _e: _is_aisquare_group(g))
    return [
        event
        for event in events
        if not any(accepts(group, event) for group in _event_groups(hooks, event))
    ]


def _event_groups(hooks: dict[str, Any], event: str) -> list[Any]:
    """The hook groups ``settings.json`` lists under ``event``; anything but a list is none.

    The file is hand-edited, and a number or ``true`` under an event made every
    reader raise ``TypeError``: ``aisquare doctor`` printed a traceback instead of
    its claude-code row, and the "connected?" check raised with it. The writers
    (``install_hooks``, ``remove_hooks``) already treated such a value as no groups.
    """
    groups = hooks.get(event)
    return groups if isinstance(groups, list) else []


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


def specs() -> list[AgentSpec]:
    """Every coding agent aisquare knows, in the registry's order: one doctor row each."""
    return _specs()


def spec(name: str, config_dir: Path | None = None) -> AgentSpec | None:
    """The registry entry for ``name``, or ``None`` when aisquare knows no such agent."""
    return _spec(name, config_dir)


def read_json(path: Path) -> dict[str, Any]:
    """The JSON object at ``path``, or ``{}`` — absent, unreadable, invalid or not an object.

    The rule for a config file this package only READS: one that cannot be read
    is ``{}``, said once here for the agent registry and for every
    ``.claude.json`` / ``settings.json`` the doctor scans, rather than a copy of
    the same three lines per caller (review of #203). :func:`read_settings`
    is deliberately NOT this: it feeds a read-modify-WRITE of the operator's
    ``settings.json``, where a permission error or a trailing comma swallowed
    into ``{}`` would be written back over everything in it — so it raises.
    Within :func:`reads_once`, a path read before answers as it did, unparsed again.
    """
    once = _READ_ONCE.get()
    if once is not None and path in once:
        return once[path]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    found: dict[str, Any] = data if isinstance(data, dict) else {}
    if once is not None:
        once[path] = found
    return found


_READ_ONCE: ContextVar[dict[Path, dict[str, Any]] | None] = ContextVar("_READ_ONCE", default=None)


@contextmanager
def reads_once() -> Iterator[None]:
    """Within it, :func:`read_json` parses each path once, for a reader that asks several
    things of one file: ``aisquare uninstall``'s plan reads a slot's ``.claude.json`` for
    its MCP servers (the adapter's ``lasting_installs``) and for its login, and such a file
    can reach tens of MB (round 23 of #257). What it answers is the reader's to read, never
    to change; nothing written in the meantime is seen."""
    token = _READ_ONCE.set({})
    try:
        yield
    finally:
        _READ_ONCE.reset(token)


def enabled_plugins(config_dir: Path) -> list[str]:
    """The plugins ``config_dir``'s ``settings.json`` enables: its ``enabledPlugins`` keys.

    Each is ``<plugin>@<marketplace>`` (or a bare name). One set to ``false`` is
    installed but disabled -- ``/plugin disable`` writes that -- so it is left
    out. The one reader of the key: the doctor's browser-tools row and the
    plugin route (:func:`claude_plugin`) both ask here. Read-only; a file that
    cannot be read enables nothing (:func:`read_json`).
    """
    return _enabled_in(config_dir / "settings.json")


def _enabled_in(settings: Path) -> list[str]:
    """The ``enabledPlugins`` keys one settings file sets to true: a config dir's
    (:func:`enabled_plugins`) or a repository's (:func:`claude_repo_plugins`)."""
    plugins = read_json(settings).get("enabledPlugins")
    if not isinstance(plugins, dict):
        return []
    return [str(key) for key, enabled in plugins.items() if enabled]


ClaudePlugin = PluginInstall
"""The aisquare plugin, installed in one Claude Code config directory and enabled there
(user scope) or in one repository (project and local scope): its ``version`` is what
``plugins/installed_plugins.json`` records, its ``scope`` the one ``claude plugin
install`` was given."""


def plugin_stands_down(name: str, config_dir: Path | None = None) -> bool:
    """Whether the plugin's launcher stands down beside ``config_dir``'s own hooks: they
    hold an aisquare hook whose program can start (:func:`_starts`), as the launcher
    grades it (``plugins/*/scripts/aisquare-hook``). Beside hooks naming a program that
    is gone, the plugin runs in their place. For Claude Code the launcher decides per
    event; this is any event. Read-only."""
    for command in hook_commands(name, config_dir):
        binary = hook_binary(command)
        if binary is not None and _starts(binary.program):
            return True
    return False


#: :func:`hook_route`'s answers.
ROUTE_HOOKS = "hooks"
ROUTE_PLUGIN = "plugin"
ROUTE_BOTH = "both"


def hook_route(name: str, config_dir: Path | None = None) -> str | None:
    """How aisquare's hooks reach ``name``'s sessions in ``config_dir``, as installed:
    :data:`ROUTE_HOOKS` (the agent's own hooks file, which ``agents connect`` writes),
    :data:`ROUTE_PLUGIN` (aisquare's plugin installed there, :func:`plugin_of`),
    :data:`ROUTE_BOTH` (the plugin standing down beside those hooks), or ``None``. Whether
    they run -- trust, a switch that turns hooks off -- is :func:`integration_readiness`'s
    and the doctor's question. Read-only; never raises."""
    spec = _spec(name, config_dir)
    if spec is None or not spec.connectable:
        return None
    directory = _hook_dir(spec)
    hooks = bool(hook_commands(name, directory))
    route = plugin_of(name)
    plugin = route is not None and route.installed(directory) is not None
    if hooks and plugin:
        return ROUTE_BOTH
    if plugin:
        return ROUTE_PLUGIN
    return ROUTE_HOOKS if hooks else None


def plugin_label(plugin: PluginInstall) -> str:
    """``the aisquare plugin <version>``, as the doctor and ``agents status`` name it."""
    return f"the aisquare plugin {plugin.version}" if plugin.version else "the aisquare plugin"


def plugin_route_supported() -> bool:
    """Whether the plugin route runs here: its hooks run ``sh``, so macOS, Linux and WSL.

    Native Windows keeps the settings.json route (docs/claude-code-plugin.md): there
    Claude Code runs hook commands through ``cmd.exe``, which has no ``sh`` unless Git
    Bash put one on PATH. So on win32 the connected check and doctor read only the
    settings.json hooks, and a plugin-only directory is offered Connect, beside which
    the plugin's launcher stands down if it ever does run.
    """
    return sys.platform != "win32"


def claude_plugin(config_dir: Path | None = None) -> ClaudePlugin | None:
    """The aisquare plugin when ``config_dir`` enables AND installed it, else ``None``.

    Enabled is ``settings.json``'s ``enabledPlugins`` holding
    :data:`CLAUDE_PLUGIN_ID` as true; installed is ``plugins/installed_plugins.json``
    listing it, which is also where the version is read. Both, because an enabled
    key with nothing installed runs nothing. ``/plugin disable`` sets the key to
    false and ``/plugin uninstall`` removes both (measured on Claude Code 2.1.292,
    which writes ``{"version": 2, "plugins": {id: [records]}}``).

    The version is the user-scope record's. A project- or local-scope record is
    another install, for one repository (:func:`claude_repo_plugins`), and may be
    an older release: listed first, it named the wrong version and uvx pin (sweep
    of #257). A record with no scope is the single record from before version 2.
    Any record's, where none is the user scope's.

    Per directory, like the hooks: Claude Code keeps plugins under each config
    dir, so one installed in ``~/.claude`` does not reach ``~/.claude-c2``.
    ``None`` is the directory a session from this shell reads
    (``CLAUDE_CONFIG_DIR``, else ``~/.claude``). Read-only; never raises.
    """
    directory = _claude_home(config_dir)
    if CLAUDE_PLUGIN_ID not in enabled_plugins(directory):
        return None
    records = _plugin_records(directory)
    if not records:
        return None
    user = [r for r in records if isinstance(r, dict) and r.get("scope", "user") == "user"]
    versions = [record.get("version") for record in user or records if isinstance(record, dict)]
    version = next((found for found in versions if isinstance(found, str) and found), None)
    return ClaudePlugin(config_dir=directory, version=version)


def _plugin_records(config_dir: Path) -> list[Any]:
    """The aisquare plugin's install records in ``config_dir``'s
    ``plugins/installed_plugins.json``: a list since its version 2, one record before."""
    installed = read_json(config_dir / "plugins" / "installed_plugins.json").get("plugins")
    records = installed.get(CLAUDE_PLUGIN_ID) if isinstance(installed, dict) else None
    if isinstance(records, dict):
        return [records]
    return records if isinstance(records, list) else []


#: The file in a repository's ``.claude`` that enables a plugin installed at each
#: repository scope (``claude plugin install --scope``, or the ``/plugin`` dialog).
_REPO_SCOPES = {"project": "settings.json", "local": "settings.local.json"}


def claude_repo_plugins(config_dir: Path | None = None) -> list[ClaudePlugin]:
    """The project- and local-scope installs of the aisquare plugin that ``config_dir``
    records and whose repository still enables it.

    Those scopes enable the plugin in the repository, in ``.claude/settings.json``
    (project, shared with the team) or ``.claude/settings.local.json`` (local), and
    not in ``config_dir``'s own settings.json, so :func:`claude_plugin` does not see
    them. ``plugins/installed_plugins.json`` records each with its ``scope`` and
    ``projectPath``, and a session of ``config_dir`` started in that repository runs
    it (measured on Claude Code 2.1.294). Read-only; never raises.
    """
    directory = _claude_home(config_dir)
    found: list[ClaudePlugin] = []
    for record in _plugin_records(directory):
        if not isinstance(record, dict):
            continue
        scope, project = record.get("scope"), record.get("projectPath")
        if not isinstance(scope, str) or scope not in _REPO_SCOPES:
            continue
        if not isinstance(project, str) or not Path(project).is_absolute():
            continue  # Claude Code records an absolute path; any other would read the cwd's
        repo = Path(project)
        if CLAUDE_PLUGIN_ID not in _enabled_in(repo / ".claude" / _REPO_SCOPES[scope]):
            continue  # removed or disabled there: nothing runs
        version = record.get("version")
        recorded = version if isinstance(version, str) and version else None
        found.append(ClaudePlugin(directory, recorded, scope=scope, project=repo))
    return found


#: How the second line of pip's and uv's ``#!/bin/sh`` trampoline opens.
_TRAMPOLINE = b"'''exec' "


def _trampoline_python(second: bytes) -> Path | None:
    """The Python a trampoline's ``second`` line execs, or ``None`` when it is not one.

    The word after ``'''exec'`` read as the shell reads it, quotes and all: an
    apostrophe in the path is ``'"'"'`` inside uv's single quotes and bare inside pip's
    double ones, and a class of quote-free characters cut the first at the apostrophe
    (``Jane'``, so a working script "could not start") and skipped the second, where the
    launcher read it (review of #257). Unbalanced quotes are no path to read.
    """
    if not second.startswith(_TRAMPOLINE):
        return None
    try:
        words = shlex.split(second[len(_TRAMPOLINE) :].decode(errors="replace"))
    except ValueError:
        return None
    return Path(words[0]) if words else None


def _starts(program: Path) -> bool:
    """The launcher's ``_starts``: an executable file whose absolute ``#!`` interpreter,
    when it has one, is an executable file too. A console script whose environment lost
    its Python stays executable and fails every run; ``env`` lines and binaries are
    trusted, as the launcher trusts them. A ``#!/bin/sh`` trampoline, which pip and uv
    write for a Python whose path has a space or is long, is judged by the Python it
    execs."""
    # os.path.isfile, here and below: a #! may name a path this user cannot reach, where
    # Path.is_file raises PermissionError on 3.11 to 3.13 (measured; 3.14 answers False);
    # the launcher's `[ -f ]` answers no.
    if not (os.path.isfile(program) and os.access(program, os.X_OK)):
        return False
    try:
        with program.open("rb") as handle:
            first = handle.readline(4096)
            second = handle.readline(4096)
    except OSError:
        return True  # unreadable: trusted, as the launcher's failed `read` is
    if not first.startswith(b"#!"):
        return True
    line = first[2:].rstrip(b"\n").lstrip(b" \t")
    interpreter = Path(re.split(rb"[ \t]", line, maxsplit=1)[0].decode(errors="replace"))
    # pip and uv write a /bin/sh trampoline in place of #!<python> when that path has a
    # space or is over 127 bytes, and its second line execs the Python that must exist:
    # '''exec' "<python>" "$0" "$@" (uv quotes it with '; pip quotes only a path with a
    # space, so a long one is bare) (review of #257).
    execs = _trampoline_python(second) if interpreter == Path("/bin/sh") else None
    if execs is not None:
        interpreter = execs
    if not interpreter.is_absolute():
        return True
    return os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)


def starts(program: Path) -> bool:
    """:func:`_starts` for callers outside this module (``services.lifecycle``).

    One answer to "can this hook's program start", the plugin launcher's, for the
    doctor's rows and upgrade's refresh plan alike, rather than a copy that could drift.
    """
    return _starts(program)


def hooks_can_start(name: str, config_dir: Path) -> bool:
    """Whether every aisquare hook in ``config_dir`` names a program that can start
    (:func:`_starts`; for a module-form hook, its interpreter): the path-only half of
    :func:`classify_hook_binary`'s verdict, asked with no process, as Welcome asks it."""
    found = (hook_binary(command) for command in hook_commands(name, config_dir))
    return all(binary is None or _starts(binary.program) for binary in found)


def launcher_finds(name: str, path: str | None = None) -> Path | None:
    """The program ``name`` where the plugin's launcher looks for it, as THIS process sees,
    or as a process with ``path`` for its ``PATH`` does (a launch's, whose hooks run with it:
    ``store_broker.install_paths``).

    The launcher's ``_find`` (``plugins/claude-code/scripts/aisquare-hook``): on PATH,
    then ``~/.local/bin`` and ``~/.cargo/bin``, for ``aisquare`` and for ``uvx`` alike,
    passing over one that cannot start (:func:`_starts`). One search here for both, so
    the doctor's answer cannot drift from the launcher's (review of #257). A Claude Code
    started from a desktop app may see another PATH; this is the best a doctor run can
    see.
    """
    found = shutil.which(name, path=path)
    if found and _starts(Path(found)):
        return Path(found)
    for candidate in (_home() / ".local" / "bin", _home() / ".cargo" / "bin"):
        program = candidate / name
        if _starts(program):
            return program
    return None


def plugin_runner() -> Path | None:
    """The aisquare the plugin's launcher would run (:func:`launcher_finds`).

    ``None`` means it falls back to the pinned release through uvx, or to nothing.
    """
    return launcher_finds("aisquare")


def claude_plugin_command(
    verb: str, config_dir: Path, *, scope: str = "user", project: Path | None = None
) -> str:
    """``claude plugin <verb> aisquare@aisquare-cli``, aimed at ``config_dir``.

    Plugins belong to one config dir, and ``claude`` acts on the one it starts in, so
    a bare ``/plugin`` typed into the usual session would act on the wrong one for a
    fleet account dir. The ambient dir needs nothing; ``~/.claude`` needs
    ``CLAUDE_CONFIG_DIR`` unset (pointed at it, Claude Code would look for
    ``.claude.json`` inside it); any other dir is named. A project- or local-scope
    install (:func:`claude_repo_plugins`) takes its ``--scope``, run from inside its
    ``project``: without it ``claude plugin uninstall`` acts on the user scope, and
    run anywhere else it answers that the plugin is not installed at that scope
    (measured on Claude Code 2.1.294).
    """
    command = f"claude plugin {verb} {CLAUDE_PLUGIN_ID}"
    if scope != "user":
        command += f" --scope {scope}"
    key = _dir_key(config_dir)
    if key == _dir_key(_claude_home()):
        line = command
    elif key == _dir_key(_home() / ".claude"):
        line = f"env -u CLAUDE_CONFIG_DIR {command}"
    else:
        line = f"CLAUDE_CONFIG_DIR={_quote(str(config_dir))} {command}"
    return line if project is None else f"cd {_quote(str(project))} && {line}"


def _registry() -> dict[str, Any]:
    """The raw agent registry, or ``{}`` when absent or unreadable."""
    return read_json(paths.agents_registry_path())


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


#: What ``Path.exists`` reads as "nothing there" (pathlib's ignored errnos and Windows
#: errors): no such file, not a directory, a bad descriptor, a symlink loop.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})
_ABSENT_WINERRORS = frozenset({21, 123, 1921})


def nothing_there(exc: OSError) -> bool:
    """Whether ``exc``, from opening or stat'ing a path, means nothing is there: what
    ``Path.exists`` reads as absence. The one notion of "missing" for every reader of an
    agent's files (:func:`present`, ``lifecycle.settings_unreadable``), so the doctor,
    connect, disconnect and uninstall never disagree about one directory: a recorded one
    that became a symlink loop was "does not exist" to the doctor and "could not be read"
    to disconnect, which then refused the doctor's own fix (review of #257)."""
    return exc.errno in _ABSENT_ERRNOS or getattr(exc, "winerror", None) in _ABSENT_WINERRORS


def present(path: Path) -> bool:
    """Whether there is something at ``path`` for a reader to open; never raises.

    Absent where ``Path.exists`` reads absence (:func:`nothing_there`), and where the path
    still starts with a ``~user`` this machine does not have (``paths.names_no_home``):
    read as written it would be looked up in the cwd. Every other error from stat (a link
    into a directory this user cannot search, EIO, ESTALE) counts as present, so the
    reader opens it and names it: ``Path.exists`` raised those on 3.11 to 3.13, which cost
    the doctor and `agents list` their whole output for one CLAUDE.md, and answers False
    on 3.14, where connect skipped a CLAUDE.md it never read (review of #257).
    """
    if paths.names_no_home(path):
        return False
    try:
        os.stat(path)
    except OSError as exc:
        return not nothing_there(exc)
    except ValueError:  # an embedded NUL: no such file can exist
        return False
    return True


def detected(spec: AgentSpec) -> bool:
    """Whether ``spec``'s agent is on this machine: its home or one of its context files exists.

    Paths only. The registry says what this home connected, which is a different
    question, so the doctor's rows for agents aisquare cannot connect ask here
    rather than through :func:`detect`.
    """
    return present(spec.home) or any(present(path) for path in spec.context_files)


def on_path(name: str) -> str | None:
    """Where agent ``name``'s binary is on PATH, if it is (an indirection so tests can decide).

    npm and Homebrew make Claude Code's config dir only when ``claude`` first runs, and
    Codex makes ``~/.codex`` only then too, so before then the binary is how detection,
    ``agents connect`` and the doctor know it is installed (review of #257).
    """
    adapter = _adapter(name)
    return shutil.which(adapter.binary) if adapter is not None else None


def claude_on_path() -> str | None:
    """Where ``claude`` is on PATH, if it is (:func:`on_path`)."""
    return on_path(_CLAUDE_CODE)


def _to_info(spec: AgentSpec, registry: dict[str, Any], *, ambient: bool = False) -> AgentInfo:
    context = inspect_context(spec.name, spec.home, read_documents=False)
    # A record of an agent aisquare has no hooks for is no connection: 0.7.0's `agents
    # connect codex` wrote one and installed nothing, and it read as connected beside the
    # doctor's "can't connect it yet" (review of #257). `agents disconnect` clears it.
    recorded = connected_dirs(spec.name, registry) if spec.connectable else []
    sites = [
        AgentHookSite(
            config_dir=directory,
            # Each site reports the shared answer, as the doctor's row does.
            hooks_installed=connected(spec.name, directory),
            # And why it is False where hooks are switched off: read as "missing", it
            # sent the user to Connect, which cannot change it (review of #257).
            hooks_off=hooks_off(spec.name, directory),
        )
        for directory in recorded
    ]
    readiness, detail = integration_readiness(spec.name, _hook_dir(spec))
    found = detected(spec)
    is_connected = False
    if spec.connectable:
        # The answer doctor and Welcome give, so `agents list`/`status` and `aisquare
        # status` agree with them (review of #257). A directory whose hooks or plugin
        # run aisquare is connected, whoever wrote them: told "not connected", a
        # plugin user ran `agents connect` and got both routes. And a recorded one
        # whose hooks are switched off or gone is not: the registry's word kept it
        # "connected" beside the doctor's "switched off" and Welcome's Connect.
        directory = _hook_dir(spec)
        if all(_dir_key(site.config_dir) != _dir_key(directory) for site in sites) and connected(
            spec.name, directory
        ):
            sites.append(AgentHookSite(config_dir=directory, hooks_installed=True))
        is_connected = any(site.hooks_installed for site in sites)
        # An agent that has never started has no config dir yet; its binary on PATH
        # says it is installed, for the directory a session from this shell reads (never
        # a --config-dir, which a typo could name), as `agents connect` treats it.
        if not found and ambient and on_path(spec.name) is not None:
            found = True
    route = hook_route(spec.name, _hook_dir(spec))
    plugin = plugin_of(spec.name) if route == ROUTE_BOTH else None
    install = plugin.installed(_hook_dir(spec)) if plugin is not None else None
    both = (
        f"aisquare runs two ways here: the {spec.settings_path.name} hooks and "
        f"{plugin_label(install)}, whose hooks stand down beside them; keep one "
        "(aisquare doctor says how)."
        if install is not None and spec.settings_path is not None
        else ""
    )
    if both and detail and not detail.endswith("."):
        detail += "."
    return AgentInfo(
        readiness=readiness,
        detail=" ".join(filter(None, [detail, both, *context.notes])),
        name=spec.name,
        detected=found,
        config_paths=context.paths,
        connected=is_connected,
        route=route,
        sites=sites,
    )


def detect_all() -> list[AgentInfo]:
    """Detection state for every agent aisquare knows about."""
    registry = _registry()
    return [_to_info(spec, registry, ambient=True) for spec in _specs()]


def detect(name: str, config_dir: Path | None = None) -> AgentInfo | None:
    """Detection state for one agent, or ``None`` if the name is unknown."""
    spec = _spec(name, config_dir)
    return _to_info(spec, _registry(), ambient=config_dir is None) if spec is not None else None


def context_files(name: str, config_dir: Path | None = None) -> list[Path]:
    """An agent's context files that are there to read (:func:`present`), for ingestion.

    Every one that is there, for ``agents connect`` to read and to refuse, naming it, when
    it cannot (``services.agents``). An agent that reads only its first nonblank file
    (Codex's ``AGENTS.override.md`` before ``AGENTS.md``) has that one alone, by the
    precedence of :func:`read_context`: one that cannot be read is left out, with a note.
    """
    spec = _spec(name, config_dir)
    if spec is None:
        return []
    if not spec.first_context_file_only:
        return [path for path in spec.context_files if present(path)]
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
    """Keep failed candidates visible, while ingesting only effective regular files.

    A file is missing where :func:`present` says so (:func:`nothing_there`, or a ``~user``
    this machine does not have), so detection, connect and the doctor agree.
    """
    spec = _spec(name, config_dir)
    result = ContextInspection([], {}, [])
    if spec is None:
        return result
    for path in spec.context_files:
        if paths.names_no_home(path):
            continue
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
        except OSError as exc:
            if nothing_there(exc):
                continue
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
    return _definition_fingerprint(
        _inspected_settings(spec.settings_path, strict=hooks_shape_strict(name))
    )


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
                    key: _without_hook_definition(value)
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
        observation.parent.mkdir(parents=True, exist_ok=True)
        _write_settings(
            observation,
            {"fingerprint": definition, "settings_stamp": stamp},
        )


def integration_readiness(name: str, config_dir: Path) -> tuple[str, str]:
    """The native integration's state in ``config_dir``: ``unsupported``, ``unreadable``,
    ``not_configured``, ``configured``, or for an agent whose hooks need trust,
    ``unverified`` until AISquare has seen this definition run, then ``observed``.

    ``configured`` is :func:`connected`, the one "connected?" answer, so a plugin-only
    directory reads as configured here too, or ``unverified`` while the agent has not
    trusted the plugin's hooks (``PluginInstall.trusted``).
    """
    try:
        adapter = get_adapter(name)
    except ValueError:
        return "unsupported", "No terminal integration is available"
    spec = _spec(name, config_dir)
    try:
        if spec is not None and spec.settings_path is not None:
            _inspected_settings(spec.settings_path, strict=hooks_shape_strict(name))
    except AgentSettingsError as exc:
        return "unreadable", str(exc)
    if not connected(name, config_dir):
        return "not_configured", ""
    if not hooks_installed(name, config_dir):
        # Through the plugin alone: its hooks carry no definition of ours to observe, and
        # the agent's own record says whether it runs them.
        route = plugin_of(name)
        install = route.installed(config_dir) if route is not None else None
        if install is not None and not install.trusted:
            return (
                "unverified",
                f"Through {plugin_label(install)}, whose hooks {adapter.label} skips until "
                f"they are trusted: open /hooks in {adapter.label} to review and trust them.",
            )
        return "configured", ""
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
        f"Hooks configured; open /hooks in {adapter.label} to review and trust them. "
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
"""The hooks run this install: the console script this distribution installed
(:func:`current_install`), or one that reports the same version."""
HOOK_BINARY_STALE = "stale"
"""The hooks run an aisquare that reports a different version from this one."""
HOOK_BINARY_MISSING = "missing"
"""The program the hooks name is not on disk, or cannot start (:func:`_starts`: a script
whose ``#!`` interpreter is gone) — every hook fails, every session."""
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
def version_in(output: str) -> str | None:
    """The version an ``aisquare --version`` printed, or ``None`` when it printed none.

    Public because ``aisquare upgrade`` reads the new install's answer the same
    way this module reads a hook binary's: one parse (an agent's version probe's too,
    ``agent_adapters.types.version_in``), so a change to what ``--version`` prints has
    one place to land.
    """
    return native_version(output)


@dataclass(frozen=True)
class HookBinary:
    """The program one hook command would start, as the hook's shell would resolve it.

    ``module_form`` is the ``<python> -m aisquare`` fallback ``_aisquare_command``
    writes when no console script is findable: then ``program`` is an
    interpreter and the install is the one in that interpreter's environment.
    """

    program: Path
    module_form: bool = False
    isolated: bool = False
    """The module form with ``-E`` (or ``-I``), which ignores ``PYTHONPATH`` and
    ``PYTHONHOME``: what :func:`_aisquare_command` writes for an agent that sandboxes its
    tools (:func:`hook_runs_isolated`)."""

    def version_argv(self) -> list[str]:
        argv = [str(self.program)]
        if self.module_form:
            # -P keeps the probe's cwd off sys.path (#81): doctor run from a
            # checkout that ships its own `aisquare/` package would otherwise
            # import THAT instead of the hook's install and grade a healthy
            # hook `unknown`. Same flag `selfcli.argv_for` writes into hooks; and
            # -E where the hook has it, so the probe imports what the hook imports, not a
            # tree's on PYTHONPATH (review r15 of #113, finding 12).
            argv += ["-E", "-P", "-m", "aisquare"] if self.isolated else ["-P", "-m", "aisquare"]
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
    plugin: ClaudePlugin | None = None
    """The aisquare plugin, when this directory has it enabled (:func:`claude_plugin`), or
    a project- or local-scope install of it a session started here loads."""


def hook_commands(name: str, config_dir: Path | None = None) -> list[str]:
    """Every aisquare hook command in the agent's settings, across all events.

    Any event counts, not only the full set ``hooks_installed`` demands: a
    partial install from an older version still RUNS on the events it has, so
    what it runs is still worth grading. Read-only, so as :func:`read_json` reads
    (see ``_missing_events``): a file that cannot be read names no command.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return []
    return _aisquare_commands(_read_settings(spec.settings_path).get("hooks"))


def _aisquare_commands(hooks: object) -> list[str]:
    """The aisquare hook commands in a settings file's ``hooks`` value (:func:`hook_commands`)."""
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
    returned as written so it grades as missing rather than crashing. ``$HOME/``
    and ``${HOME}/`` are expanded as ``~/`` is: the hook's shell expands them, and
    read literally a working hook graded as missing (review of #257). So is a
    ``~olduser/…`` naming a user this machine does not have, on which pathlib
    raises RuntimeError: upgrade, its ``--check`` and doctor ended in a traceback
    (sweep of #257).
    """
    try:
        for prefix in ("$HOME/", "${HOME}/"):
            if token.startswith(prefix):
                token = str(Path("~").expanduser() / token[len(prefix) :])
                break
        path = Path(token).expanduser()
    except RuntimeError:
        path = Path(token)
    if not path.is_absolute() and path.parent == Path("."):
        found = shutil.which(token)
        return Path(found) if found else path
    return path


def hook_runs_isolated(command: str) -> bool:
    """Whether the aisquare hook ``command`` runs its interpreter with ``-E`` (or ``-I``),
    which ignores ``PYTHONPATH`` and ``PYTHONHOME``: the module form
    :func:`_aisquare_command` writes for an agent that sandboxes its tools (review r14 of
    #113, finding 1). A console script's interpreter reads them."""
    if not _is_aisquare_hook_command(command):
        return False
    tokens, _ = _hook_command_parts(command)
    if tokens[-4:-2] != ["-m", "aisquare"]:
        return False
    return any(token in ("-E", "-I") for token in tokens[1:-4])


def hook_binary(command: str) -> HookBinary | None:
    """The program ``command`` would run, or ``None`` when it is not one of our hooks."""
    if not _is_aisquare_hook_command(command):
        return None
    tokens, _ = _hook_command_parts(command)
    if tokens[-4:-2] == ["-m", "aisquare"]:
        return HookBinary(
            _resolve_program(tokens[0]),
            module_form=True,
            isolated=any(token in ("-E", "-I") for token in tokens[1:-4]),
        )
    return HookBinary(_resolve_program(tokens[0]))


@functools.cache
def current_install() -> Path:
    """The ``aisquare`` console script THIS distribution installed.

    What ``agents connect`` run from here would write into the hooks, and so the
    thing every hook binary is compared against. The one the distribution's RECORD
    lists, else the one beside this interpreter. Beside the interpreter alone it could
    be another install's: ``python3 -m aisquare`` on the system Python runs the package
    from the user site, where ``pip install --user`` put its script in ``~/.local/bin``,
    and ``/usr/bin/aisquare`` beside that Python is whatever an older ``sudo pip
    install`` left. Connect wrote that one into the hooks, and the doctor graded it
    current by its path and never asked its version (review of #257). It may not
    exist (a checkout driven as ``python -m aisquare``); the directory is what the
    comparison uses.

    Once per process: it locates the distribution and walks its RECORD, and Connect's
    command, every hook binary's comparison, the doctor's install row and each "this
    install is" line ask it again (round 21 of #257). The answer is this process's own
    install, which an upgrade does not change under it: nothing in the process asks
    after the installer has run (``services.lifecycle.upgrade``).
    """
    name = "aisquare.exe" if sys.platform == "win32" else "aisquare"
    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION)
        listed = [file for file in distribution.files or () if file.name == name]
    except Exception:  # no metadata, or a RECORD that does not read: the sibling, as before
        listed = []
    if listed:
        return Path(os.path.normpath(str(distribution.locate_file(listed[0]))))
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

    A console script reached through a link is the exception that resolving gets
    right: uv's ``~/.local/bin/aisquare`` IS this install's script when it resolves
    to it, the same file and so the same interpreter and package. Compared
    unresolved, the doctor ran it for its version on every run (review of #257).
    """
    program = binary.program
    this = Path(sys.executable) if binary.module_form else current_install()
    if program == this:
        return True
    if not binary.module_form and os.path.realpath(program) == os.path.realpath(this):
        return True
    if program.parent != this.parent:
        return False
    try:
        return program.resolve() == this.resolve()
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop, on 3.11 and 3.12
        return False


def same_install(binary: HookBinary) -> bool:
    """:func:`_same_install` for callers outside this module (``services.diagnostics``).

    The doctor's install row asks it of the program on PATH, so "this install" means the
    same thing there as in the hooks' rows.
    """
    return _same_install(binary)


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
    return version_in(completed.stdout)


def classify_hook_binary(binary: HookBinary) -> tuple[str, str | None]:
    """``(state, version)`` for one hook binary, measured against this install.

    Path first, process second: the common case — hooks written by this very
    install — is decided without starting anything. Only a program in another
    directory is asked its version, and it is asked ONCE per doctor run however
    many directories name it (see ``hook_site_health``'s cache).
    """
    # The launcher's own test (_starts), which never raises: a program that is gone, in a
    # directory this user cannot enter, or a script whose #! interpreter is gone fails
    # every event, and its --version probe could only fail (review of #257).
    if not _starts(binary.program):
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
    cwd: Path | None = None,
) -> HookSiteHealth:
    """Grade one config directory: are the hooks all there, and what do they run?

    Its ``plugin`` is the user-scope install; else, only where the directory runs no
    hooks of ours, a project- or local-scope one a session started in ``cwd`` loads
    (:func:`claude_repo_plugin_here`). That one covers sessions in its repository
    alone. Beside the directory's own hooks it doubles nothing (the launcher stands
    down there), and graded as the directory's plugin it read as "two ways" and the
    advice removed the hooks every other repository runs on (review of #257).

    Its ``error`` says why a settings file that cannot be inspected holds no hooks.
    """
    error = None
    spec = _spec(name, config_dir)
    if spec is not None and spec.settings_path is not None:
        try:
            _inspected_settings(spec.settings_path, strict=hooks_shape_strict(name))
        except AgentSettingsError as exc:
            error = str(exc)
    installed = hooks_installed(name, config_dir)
    commands = hook_commands(name, config_dir)
    plugin = None
    if (route := plugin_of(name)) is not None:
        plugin = route.installed(config_dir)
        if plugin is None and not commands:
            plugin = route.repo_install_here(config_dir, cwd)
    binaries: list[HookBinary] = []
    for command in commands:
        binary = hook_binary(command)
        if binary is not None and binary not in binaries:
            binaries.append(binary)
    if not binaries:
        return HookSiteHealth(
            config_dir=config_dir,
            hooks_installed=installed,
            recorded=recorded,
            plugin=plugin,
            error=error,
        )
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
        plugin=plugin,
        error=error,
    )


def _agent_dirs_on_disk(name: str) -> list[Path]:
    """``name``'s config dirs on this machine that carry our hooks or plugin.

    The one this shell's sessions read (``$CLAUDE_CONFIG_DIR``, ``$CODEX_HOME``), the
    default home and every sibling named like it — the ``~/.claude[0-9]`` of a
    parallel-install setup, the ``~/.claude-account1`` the README documents. Only
    directories whose hooks file holds at least one aisquare hook, or that enable the
    aisquare plugin (:func:`plugin_of`), are returned: a hook that is on disk runs
    whether or not this home ever heard of the directory, and that is the only thing
    that makes a directory doctor's business. Read-only discovery supplements the
    registry for a fresh AISQUARE_HOME.

    A candidate whose settings this user cannot read — another account's
    ``~/.claude-archived``, a backup left at mode 000 — is skipped, not raised: it
    cannot be shown to carry our hooks, and one unreadable sibling must not cost
    doctor every other row.
    """
    adapter = _adapter(name)
    if adapter is None:
        return []
    home = _home()
    candidates = [adapter.config_dir(home, os.environ), home / adapter.home_name]
    with suppress(OSError):
        candidates.extend(sorted(home.glob(adapter.home_name + "*")))
    plugin = plugin_of(name)
    seen: set[Path] = set()
    found: list[Path] = []
    for candidate in candidates:
        key = _dir_key(candidate)
        if key in seen or not os.path.isdir(candidate):
            continue
        seen.add(key)
        # Unreadable settings name no command.
        if hook_commands(name, candidate) or (
            plugin is not None and plugin.installed(candidate) is not None
        ):
            found.append(candidate)
    return found


def _holds_aisquare(config_dir: Path) -> bool:
    """Whether ``config_dir``'s settings.json holds an aisquare hook or enables the aisquare
    plugin that is installed there (:func:`claude_plugin`'s two halves), reading it once;
    one that cannot be read holds neither (:func:`read_json`)."""
    home = _claude_home(config_dir)
    settings = read_json(home / "settings.json")
    enabled = settings.get("enabledPlugins")
    return bool(_aisquare_commands(settings.get("hooks"))) or (
        plugin_route_supported()
        and isinstance(enabled, dict)
        and bool(enabled.get(CLAUDE_PLUGIN_ID))
        and bool(_plugin_records(home))
    )


def claude_dir_siblings() -> list[Path]:
    """Every ``~/.claude*`` directory, sorted, hooked or not: ``~/.claude``, the ``[0-9]``
    siblings of a parallel install, ``~/.claude-account1``. The one listing the doctor's
    scan (:func:`_agent_dirs_on_disk`, :func:`found_on_disk`) and uninstall's MCP and
    plugin scan share, so they agree on which directories there are (round 25 of #257).

    Each is asked with ``os.path.isdir``, which never raises: ``Path.is_dir`` raised
    PermissionError on 3.11 to 3.13 for a link into a folder this user cannot enter, and
    cost the doctor its output, and uninstall every sibling (review of #257). Such a
    sibling is skipped; only the listing itself is guarded.
    """
    try:
        found = sorted(_home().glob(".claude*"))
    except OSError:
        return []
    return [path for path in found if os.path.isdir(path)]


def found_on_disk(config_dir: Path) -> bool:
    """Whether the doctor finds ``config_dir`` whatever ``CLAUDE_CONFIG_DIR`` says: a
    ``~/.claude*`` directory holding aisquare's hooks or plugin (:func:`_agent_dirs_on_disk`),
    reading no sibling's settings but its own."""
    key = _dir_key(config_dir)
    named = _home() / ".claude", *claude_dir_siblings()
    return any(_dir_key(p) == key for p in named) and _holds_aisquare(config_dir)


def _dir_key(path: Path) -> Path:
    """One identity for the several spellings of a directory (``~``, symlinks).

    pathlib raises RuntimeError, not OSError, for a symlink loop on 3.11 and 3.12, and
    for a ``~olduser`` naming no user here (``paths.expand_user`` keeps that as written).
    A recorded directory of either kind ended upgrade and uninstall in a traceback
    here, and the ``~olduser`` one doctor, status and ``agents list`` in
    :func:`_claude_home` (sweep of #257).
    """
    path = paths.expand_user(path)
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def dir_identity(path: Path) -> Path:
    """:func:`_dir_key` for callers outside this module (``services.lifecycle``).

    One notion of "the same directory" for every dedupe of hook sites, rather
    than a copy that could drift from this one.
    """
    return _dir_key(path)


def claude_config_dirs() -> list[Path]:
    """The Claude Code directories an agent of THIS home could start in.

    The dirs this home connected plus the ambient one (``CLAUDE_CONFIG_DIR``,
    else ``~/.claude``), one entry per directory identity — and deliberately
    NOT :func:`hook_sites`, for two measured reasons.

    It GRADES every site: ``hook_site_health`` runs ``classify_hook_binary``,
    which runs a real ``<that install's aisquare> --version`` subprocess with a
    10 s timeout, and its dedupe cache is built fresh per call. The doctor's
    ``_check_claude_code`` already calls it once per run, so a second call
    re-ran every probe: 1 → 2 scans, 3 → 6 subprocesses, 683 ms → 1246 ms on a
    four-directory machine (+82% on the whole run) for grading the browser-tools
    row never reads — on a path the fleet UI re-runs on every project switch,
    every Doctor-tab activation and every one-click fix.

    And it includes directories this home never connected
    (``_agent_dirs_on_disk``, the #84 gap), which answers a different question:
    "does ANY Claude install on this box declare a browser tool" rather than
    "will the ui-tester's window find one". A playwright MCP in ``~/.claude4``
    made the row green while the fleet spawned its ui-tester on ``~/.claude``,
    where nothing answered.

    Public so ``services.diagnostics`` reads it by name rather than through
    ``_claude_home`` and ``_dir_key``, which are this module's own (review of
    #203).
    """
    dirs = [*connected_dirs(_CLAUDE_CODE), _claude_home()]
    seen: set[Path] = set()
    unique: list[Path] = []
    for directory in dirs:
        key = _dir_key(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def hook_sites(name: str, *, cwd: Path | None = None) -> list[HookSiteHealth]:
    """Every config directory doctor should grade, each with its verdict. ``cwd`` is
    where the sessions in question start (:func:`hook_site_health`).

    Recorded sites first (the registry's order), then the ambient directory a
    session from THIS shell would use, then anything found on disk with our
    hooks in it. The first group is ``recorded``; the rest are not — they are
    real installs this home never connected, which is the gap a fresh
    ``AISQUARE_HOME`` opens (#84). Each directory appears once however many
    lists name it.
    """
    cache: dict[HookBinary, tuple[str, str | None]] = {}
    return [
        hook_site_health(name, path, recorded=recorded, cache=cache, cwd=cwd)
        for path, recorded in _hook_dir_candidates(name)
    ]


def _hook_dir_candidates(name: str) -> list[tuple[Path, bool]]:
    """``(directory, recorded)`` for every place ``name``'s hooks may live, each once.

    The one list :func:`hook_sites` grades and :func:`hook_dirs` returns as it
    is, so doctor and uninstall cannot disagree about which directories exist.
    """
    sites: list[tuple[Path, bool]] = []
    seen: set[Path] = set()

    def add(path: Path, *, recorded: bool) -> None:
        key = _dir_key(path)
        if key not in seen:
            seen.add(key)
            sites.append((path, recorded))

    for path in connected_dirs(name, _registry()):
        add(path, recorded=True)
    ambient = ambient_hook_dir(name)
    # os.path.isdir: for a CLAUDE_CONFIG_DIR in a folder this user cannot enter,
    # ``Path.is_dir`` raised on 3.11 to 3.13, and the doctor printed nothing and
    # uninstall a traceback (review of #257).
    if ambient is not None and os.path.isdir(ambient):
        add(ambient, recorded=False)
    for path in _agent_dirs_on_disk(name):
        add(path, recorded=False)
    return sites


def hook_dirs(name: str) -> list[Path]:
    """Every config directory that may carry ``name``'s hooks — found, never graded.

    The directories :func:`hook_sites` grades, without the grading:
    ``hook_sites`` asks each hook's program its version, which is the wrong
    thing to do while removing that program, so ``uninstall`` asks this
    instead. Reads only; each directory appears once.
    """
    return [path for path, _recorded in _hook_dir_candidates(name)]
