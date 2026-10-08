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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from aisquare.core import paths, selfcli
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
    # Fires INSTEAD of Stop when the turn ends on an API error — a usage limit
    # above all (#146). Its output is ignored by Claude Code, so it is pure
    # bookkeeping: the board learns the session is `limited` and when the limit
    # lifts. An install from before this event reads as partial, and `doctor`
    # says to re-run `agents connect`, exactly as it did when Stop and
    # Notification arrived.
    ("StopFailure", "stop-failure"),
)

#: The Claude Code plugin route (``plugins/claude-code`` in this repo): the plugin and
#: the marketplace that lists it, as ``/plugin install aisquare@aisquare-cli`` names
#: them and ``enabledPlugins`` keys them. Its hooks run the same ``aisquare hook
#: <event>`` as :data:`_HOOKS`, and stand down for any event settings.json already
#: runs. tests/test_claude_plugin.py holds both manifests to these names.
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
    planned: str | None = None
    """The release planned to connect this agent, while aisquare can only detect it."""

    @property
    def connectable(self) -> bool:
        """Whether ``agents connect`` installs anything: aisquare has hooks for this agent."""
        return self.settings_path is not None


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
        # Detected only: no hooks yet, so `agents connect` refuses them. The doctor's
        # row names a release only where the release plan has one (Codex: 10.1);
        # tests/test_agent_adapters.py fails once that release is the running one.
        AgentSpec("cursor", "Cursor", home / ".cursor", ()),
        AgentSpec("codex", "Codex", home / ".codex", (), planned="0.10"),
    ]


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


class SettingsNotAnObjectError(ValueError):
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


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Write ``settings`` back to ``path`` the way Claude Code writes the file: UTF-8.

    ``json.dumps`` escapes every non-ASCII character unless told not to, so a hook
    naming ``~/Développement/…/aisquare`` was stored as ``D\\u00e9veloppement``. The
    plugin's launcher reads the file as text and cannot read a program out of an
    escape, so it trusted such a hook: when that program was deleted it stood down
    beside a hook that fails on every event, and no aisquare ran at all (review of
    #257). A lone surrogate (a path that is not UTF-8) cannot be written as UTF-8 and
    keeps the escape ``json.dumps`` would give it.
    """
    text = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", errors="backslashreplace")


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
    if len(tokens) < 3 or tokens[-2] != "hook":
        return False
    if tokens[-1] not in {subcommand for _, subcommand in _HOOKS}:
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
    """Install aisquare's lifecycle hooks. False if the agent is unsupported.

    Raises :class:`SettingsNotAnObjectError` for a settings file it must not
    rewrite, and ``OSError`` or ``UnicodeDecodeError`` for one it cannot read.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    settings = read_settings(spec.settings_path)  # raises rather than lose what is in it
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    command = _aisquare_command()  # already shell-quoted where needed
    for event, subcommand in _HOOKS:
        groups = hooks.get(event)
        kept = _without_aisquare(groups) if isinstance(groups, list) else []
        entry: dict[str, Any] = {"type": "command", "command": f"{command} hook {subcommand}"}
        if event in _CONTEXT_HOOKS:
            # Never below the ceiling the CI hook may wait for; never *reducing*
            # a longer one the operator chose deliberately.
            existing = _installed_timeout(groups, event)
            entry["timeout"] = max(CONTEXT_HOOK_TIMEOUT_SECONDS, existing or 0)
        kept.append({"hooks": [entry]})
        hooks[event] = kept
    settings["hooks"] = hooks
    spec.settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_settings(spec.settings_path, settings)
    return True


def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Remove aisquare's hooks from the agent's settings. True if any were removed."""
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None or not spec.settings_path.exists():
        return False
    try:
        settings = read_settings(spec.settings_path)
    except SettingsNotAnObjectError:
        return False  # nothing of ours can be found in it to take out, and it is left alone
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for event, _ in _HOOKS:
        groups = hooks.get(event)
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
    return not _missing_events(name, config_dir, reconciled=False)


def hook_timeout_shortfall(name: str, config_dir: Path | None = None) -> list[str]:
    """Context events whose installed ``timeout`` is below what the CI hook needs.

    Empty when there is nothing to reconcile — including when the hooks are not
    installed at all, which :func:`hooks_installed` is the question for.
    """
    if not hooks_installed(name, config_dir):
        return []
    return _missing_events(name, config_dir, reconciled=True)


def hooks_disabled(name: str, config_dir: Path | None = None) -> bool:
    """Whether the agent's settings file switches every hook off (``"disableAllHooks": true``).

    Claude Code then runs no hook from that directory, its plugins' included, so
    none of ours fire however complete they are, and ``agents connect`` leaves the
    key alone: Connect cannot change it. Only a literal ``true`` counts, as Claude
    Code reads it. Read-only (:func:`read_json`); never raises.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return False
    return read_json(spec.settings_path).get("disableAllHooks") is True


def hooks_off(name: str, config_dir: Path | None = None) -> Path | None:
    """The settings file that switches every hook off (:func:`hooks_disabled`), else ``None``.

    What `agents connect`, `agents list` and `agents status` name in place of a
    connection or a "missing": the cause is that key, and Connect cannot change it.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None or not hooks_disabled(name, config_dir):
        return None
    return spec.settings_path


def claude_code_connected(config_dir: Path | None = None) -> bool:
    """Whether Claude Code in ``config_dir`` runs aisquare: the one "connected?" answer.

    ``services.agents.claude_code_connected`` is its public face and says who asks
    and why. It is implemented here so this module's own per-directory readers ask
    it too: ``_to_info`` reports it as each site's ``hooks_installed`` in ``agents
    list`` and ``agents status``.

    Two routes, in a directory whose ``settings.json`` does not switch hooks off:
    every lifecycle hook ``agents connect`` installs is in that file, or the
    aisquare Claude Code plugin is installed and enabled there
    (:func:`claude_plugin`), or installed at project or local scope for the
    repository a session started in this process's working directory loads it
    from (:func:`claude_repo_plugin_here`). The switch comes first because it
    silences every route, the plugin's included. Never raises: everything it
    reads goes through :func:`read_json`.
    """
    if hooks_disabled("claude-code", config_dir):
        return False
    if hooks_installed("claude-code", config_dir):
        return True
    if not plugin_route_supported():
        return False
    return claude_plugin(config_dir) is not None or claude_repo_plugin_here(config_dir) is not None


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

    Read through :func:`read_json`, this module's rule for a file it only reads:
    a ``settings.json`` that is missing, unreadable, not UTF-8 or a directory
    holds no hooks. Read through :func:`read_settings`, which must raise for the
    writers, each of those cost ``aisquare doctor`` its whole report.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return [event for event, _ in _HOOKS]
    hooks = read_json(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return [event for event, _ in _HOOKS]
    accepts = _is_current_aisquare_group if reconciled else (lambda g, _e: _is_aisquare_group(g))
    return [
        event
        for event, _ in _HOOKS
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
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


@dataclass(frozen=True)
class ClaudePlugin:
    """The aisquare plugin, installed in one Claude Code config directory and enabled
    there (user scope) or in one repository (project and local scope)."""

    config_dir: Path
    version: str | None
    """What ``plugins/installed_plugins.json`` records; ``None`` when it records none."""
    scope: str = "user"
    """``user``, or ``project``/``local``: the scope ``claude plugin install`` was given."""
    project: Path | None = None
    """The repository a project- or local-scope install is enabled in; ``None`` at user scope."""


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
    versions = [record.get("version") for record in records if isinstance(record, dict)]
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


def _starts(program: Path) -> bool:
    """The launcher's ``_starts``: an executable file whose absolute ``#!`` interpreter,
    when it has one, is an executable file too. A console script whose environment lost
    its Python stays executable and fails every run; ``env`` lines and binaries are
    trusted, as the launcher trusts them."""
    # os.path.isfile, here and below: a #! may name a path this user cannot reach, where
    # Path.is_file raises PermissionError on 3.11 to 3.13 (measured; 3.14 answers False);
    # the launcher's `[ -f ]` answers no.
    if not (os.path.isfile(program) and os.access(program, os.X_OK)):
        return False
    try:
        with program.open("rb") as handle:
            first = handle.readline(4096)
    except OSError:
        return True  # unreadable: trusted, as the launcher's failed `read` is
    if not first.startswith(b"#!"):
        return True
    line = first[2:].rstrip(b"\n").lstrip(b" \t")
    interpreter = Path(re.split(rb"[ \t]", line, maxsplit=1)[0].decode(errors="replace"))
    if not interpreter.is_absolute():
        return True
    return os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)


def launcher_finds(name: str) -> Path | None:
    """The program ``name`` where the plugin's launcher looks for it, as THIS process sees.

    The launcher's ``_find`` (``plugins/claude-code/scripts/aisquare-hook``): on PATH,
    then ``~/.local/bin`` and ``~/.cargo/bin``, for ``aisquare`` and for ``uvx`` alike,
    passing over one that cannot start (:func:`_starts`). One search here for both, so
    the doctor's answer cannot drift from the launcher's (review of #257). A Claude Code
    started from a desktop app may see another PATH; this is the best a doctor run can
    see.
    """
    found = shutil.which(name)
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


def detected(spec: AgentSpec) -> bool:
    """Whether ``spec``'s agent is on this machine: its home or one of its context files exists.

    Paths only. The registry says what this home connected, which is a different
    question, so the doctor's rows for agents aisquare cannot connect ask here
    rather than through :func:`detect`.
    """
    return spec.home.exists() or any(path.exists() for path in spec.context_files)


def claude_on_path() -> str | None:
    """Where ``claude`` is on PATH, if it is (an indirection so tests can decide).

    npm and Homebrew make Claude Code's config dir only when ``claude`` first runs,
    so before then the binary is how detection, ``agents connect`` and the doctor know
    it is installed (review of #257). ``claude`` is ``harness.DEFAULT_AGENT_BINARY``,
    named here so this module stays light to import.
    """
    return shutil.which("claude")


def _to_info(spec: AgentSpec, registry: dict[str, Any], *, ambient: bool = False) -> AgentInfo:
    existing = [path for path in spec.context_files if path.exists()]
    sites = [
        AgentHookSite(
            config_dir=directory,
            # Claude Code's sites report the shared answer, as the doctor's row does.
            hooks_installed=(
                claude_code_connected(directory)
                if spec.name == "claude-code"
                else hooks_installed(spec.name, directory)
            ),
            # And why it is False where hooks are switched off: read as "missing", it
            # sent the user to Connect, which cannot change it (review of #257).
            hooks_off=hooks_off(spec.name, directory),
        )
        for directory in connected_dirs(spec.name, registry)
    ]
    found = detected(spec)
    connected = spec.name in _connected_set(registry)
    if spec.name == "claude-code":
        # The answer doctor and Welcome give, so `agents list`/`status` and `aisquare
        # status` agree with them (review of #257). A directory whose hooks or plugin
        # run aisquare is connected, whoever wrote them: told "not connected", a
        # plugin user ran `agents connect` and got both routes.
        directory = _hook_dir(spec)
        if claude_code_connected(directory):
            connected = True
            if all(_dir_key(site.config_dir) != _dir_key(directory) for site in sites):
                sites.append(AgentHookSite(config_dir=directory, hooks_installed=True))
        # A Claude Code that has never started has no config dir yet; its binary on PATH
        # says it is installed, for the directory a session from this shell reads (never
        # a --config-dir, which a typo could name), as `agents connect` treats it.
        if not found and ambient and claude_on_path() is not None:
            found = True
    return AgentInfo(
        name=spec.name,
        detected=found,
        config_paths=existing,
        connected=connected,
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
    """Existing context files for an agent (its content, for ingestion)."""
    spec = _spec(name, config_dir)
    return [path for path in spec.context_files if path.exists()] if spec else []


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


def version_in(output: str) -> str | None:
    """The version an ``aisquare --version`` printed, or ``None`` when it printed none.

    Public because ``aisquare upgrade`` reads the new install's answer the same
    way this module reads a hook binary's: one parse, so a change to what
    ``--version`` prints has one place to land.
    """
    match = _VERSION_TOKEN.search(output)
    return match.group(0) if match else None


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
    plugin: ClaudePlugin | None = None
    """The aisquare plugin, when this directory has it enabled (:func:`claude_plugin`), or
    a project- or local-scope install of it a session started here loads."""


def hook_commands(name: str, config_dir: Path | None = None) -> list[str]:
    """Every aisquare hook command in the agent's settings, across all events.

    Any event counts, not only the full set ``hooks_installed`` demands: a
    partial install from an older version still RUNS on the events it has, so
    what it runs is still worth grading. Read-only, so through :func:`read_json`
    (see ``_missing_events``): a file that cannot be read names no command.
    """
    spec = _spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return []
    hooks = read_json(spec.settings_path).get("hooks")
    if not isinstance(hooks, dict):
        return []
    found: list[str] = []
    for event, _ in _HOOKS:
        for group in _event_groups(hooks, event):
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
    read literally a working hook graded as missing (review of #257).
    """
    for prefix in ("$HOME/", "${HOME}/"):
        if token.startswith(prefix):
            token = str(Path("~").expanduser() / token[len(prefix) :])
            break
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
    return version_in(completed.stdout)


def classify_hook_binary(binary: HookBinary) -> tuple[str, str | None]:
    """``(state, version)`` for one hook binary, measured against this install.

    Path first, process second: the common case — hooks written by this very
    install — is decided without starting anything. Only a program in another
    directory is asked its version, and it is asked ONCE per doctor run however
    many directories name it (see ``hook_site_health``'s cache).
    """
    # os.path.exists: Path.exists raises PermissionError on 3.11 to 3.13 (3.14 answers
    # False) for a program in a directory this user cannot enter, which cost `doctor` its
    # report; such a program is as good as gone.
    if not os.path.exists(binary.program):
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
    """Grade one config directory: are the hooks all there, and what do they run?

    Its ``plugin`` is the user-scope install; else, only where the directory runs no
    hooks of ours, a project- or local-scope one a session started here loads
    (:func:`claude_repo_plugin_here`). That one covers sessions in its repository
    alone. Beside the directory's own hooks it doubles nothing (the launcher stands
    down there), and graded as the directory's plugin it read as "two ways" and the
    advice removed the hooks every other repository runs on (review of #257).
    """
    installed = hooks_installed(name, config_dir)
    commands = hook_commands(name, config_dir)
    plugin = None
    if name == "claude-code" and plugin_route_supported():
        plugin = claude_plugin(config_dir)
        if plugin is None and not commands:
            plugin = claude_repo_plugin_here(config_dir)
    binaries: list[HookBinary] = []
    for command in commands:
        binary = hook_binary(command)
        if binary is not None and binary not in binaries:
            binaries.append(binary)
    if not binaries:
        return HookSiteHealth(
            config_dir=config_dir, hooks_installed=installed, recorded=recorded, plugin=plugin
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
    )


def _claude_dirs_on_disk() -> list[Path]:
    """Claude Code config directories on this machine that carry our hooks.

    ``$CLAUDE_CONFIG_DIR``, ``~/.claude`` and every ``~/.claude*`` directory —
    the ``[0-9]`` siblings of a parallel-install setup and the
    ``~/.claude-account1`` naming the README documents. Only directories whose
    ``settings.json`` holds at least one aisquare hook, or enables the aisquare
    plugin (:func:`claude_plugin`), are returned: a hook that is on disk runs
    whether or not this home ever heard of the directory, and that is the only
    thing that makes a directory doctor's business.

    A candidate whose ``settings.json`` this user cannot read — another
    account's ``~/.claude-archived``, a backup left at mode 000 — is skipped, not
    raised: it cannot be shown to carry our hooks, and one unreadable sibling
    must not cost doctor every other row.
    """
    candidates: list[Path] = []
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    home = _home()
    candidates.append(home / ".claude")
    if home.is_dir():
        candidates.extend(sorted(path for path in home.glob(".claude*") if path.is_dir()))
    seen: set[Path] = set()
    found: list[Path] = []
    for candidate in candidates:
        key = _dir_key(candidate)
        if key in seen or not candidate.is_dir():
            continue
        seen.add(key)
        try:
            ours = bool(hook_commands("claude-code", candidate)) or (
                plugin_route_supported() and claude_plugin(candidate) is not None
            )
        except (OSError, ValueError):
            continue  # unreadable or undecodable settings.json — see the docstring
        if ours:
            found.append(candidate)
    return found


def _dir_key(path: Path) -> Path:
    """One identity for the several spellings of a directory (``~``, symlinks)."""
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


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
    (``_claude_dirs_on_disk``, the #84 gap), which answers a different question:
    "does ANY Claude install on this box declare a browser tool" rather than
    "will the ui-tester's window find one". A playwright MCP in ``~/.claude4``
    made the row green while the fleet spawned its ui-tester on ``~/.claude``,
    where nothing answered.

    Public so ``services.diagnostics`` reads it by name rather than through
    ``_claude_home`` and ``_dir_key``, which are this module's own (review of
    #203).
    """
    dirs = [*connected_dirs("claude-code"), _claude_home()]
    seen: set[Path] = set()
    unique: list[Path] = []
    for directory in dirs:
        key = _dir_key(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def hook_sites(name: str) -> list[HookSiteHealth]:
    """Every config directory doctor should grade, each with its verdict.

    Recorded sites first (the registry's order), then the ambient directory a
    session from THIS shell would use, then anything found on disk with our
    hooks in it. The first group is ``recorded``; the rest are not — they are
    real installs this home never connected, which is the gap a fresh
    ``AISQUARE_HOME`` opens (#84). Each directory appears once however many
    lists name it.
    """
    cache: dict[HookBinary, tuple[str, str | None]] = {}
    return [
        hook_site_health(name, path, recorded=recorded, cache=cache)
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
    if ambient is not None and ambient.is_dir():
        add(ambient, recorded=False)
    if name == "claude-code":
        for path in _claude_dirs_on_disk():
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
