"""Detection and integration of coding agents (Claude Code, etc.)."""

from __future__ import annotations

import os
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


def claude_code_connected(config_dir: Path | None = None, *, cwd: Path | None = None) -> bool:
    """Whether Claude Code in ``config_dir`` runs aisquare: the one "connected?" answer.

    ``cwd`` is the folder the sessions in question start in: it decides a project- or
    local-scope plugin. Welcome passes the project step 1 chose, where its fleet starts;
    unless given, this process's working directory (review of #257).

    Asked wherever Claude Code is reported connected or offered Connect: the
    doctor's ``claude-code`` row (whose ``agents connect`` fix is a button in asq),
    ``agents list`` and ``agents status``, the Accounts page and the Welcome view.
    The Claude Code plugin route extends it rather than adding a check of its
    own. Two answers to one question is how the hooks get installed twice and
    every prompt captured twice. It is implemented in ``core.agents``
    (:func:`aisquare.core.agents.claude_code_connected`) so the readers there
    can ask it too.

    ``config_dir`` is a Claude Code config directory; ``None`` is the one a
    session started from this shell reads (``CLAUDE_CONFIG_DIR``, else
    ``~/.claude``), as ``agents connect`` means it.

    Two routes connect it, in a directory whose ``settings.json`` does not switch
    hooks off: every lifecycle hook ``agents connect`` installs is in that file,
    or the aisquare Claude Code plugin is installed and enabled there
    (:func:`claude_plugin`), whose hooks run the same ``aisquare hook <event>``,
    or installed at project or local scope for the repository a session started
    in this process's working directory loads it from
    (``agent_core.claude_repo_plugin_here``).
    A partial install from an older version answers False, because Connect is
    what completes it. So does ``"disableAllHooks": true``, which Connect
    cannot change: a surface that offers Connect asks
    ``agent_core.hooks_disabled`` first and says so instead, as the doctor's row
    does. Which aisquare the hooks run is the doctor's question, not this one:
    answering it can start a process.

    Read-only and offline, and it never raises: a few files are read and nothing
    is written, so no ``~/.aisquare`` appears. ``agents.json`` is not consulted,
    because hooks on disk run whether or not this home recorded them (#84). A
    ``settings.json`` that cannot be read answers False: nothing shows our hooks
    are there.
    """
    return agent_core.claude_code_connected(config_dir, cwd=cwd)


def claude_plugin(config_dir: Path | None = None) -> agent_core.ClaudePlugin | None:
    """The aisquare Claude Code plugin in ``config_dir``, when it is installed and enabled.

    ``None`` where the plugin route does not run (native Windows), as doctor and
    uninstall read it: there `connect` and `disconnect` must not say the plugin runs
    aisquare, nor offer an ``env -u`` command cmd and PowerShell cannot run.
    """
    if not agent_core.plugin_route_supported():
        return None
    return agent_core.claude_plugin(config_dir)


def claude_plugin_command(verb: str, config_dir: Path) -> str:
    """``claude plugin <verb> aisquare@aisquare-cli`` aimed at ``config_dir``."""
    return agent_core.claude_plugin_command(verb, config_dir)


class UnsupportedAgentError(ValueError):
    """An agent aisquare can detect but has no hooks for yet (Codex, Cursor).

    Connecting one used to exit 0 and record it as connected in ``agents.json``
    while installing nothing. A ``ValueError``, so ``init --agent`` reports it in
    its notes the way it reports an agent that is not installed.
    """


class AgentNotInstalledError(ValueError):
    """The agent is not on this machine: the one ``ValueError`` reported as ``not_installed``."""


class AgentFileUnreadableError(ValueError):
    """A file ``connect`` must read (``CLAUDE.md``, ``settings.json``) is not readable UTF-8
    text, or a ``settings.json`` that is not a JSON object, which is never rewritten, or
    one this user may not write.

    Any ``ValueError`` used to be reported as ``not_installed``, which is what an
    undecodable ``CLAUDE.md`` became, and an ``OSError`` (a directory where the
    file should be, a file this user may not read) was a traceback with nothing
    on stdout. So asq's Connect button named neither the file nor the reason.
    The message names both.
    """


def _read_agent_file(path: Path) -> str | None:
    """The text of one of the agent's own files, or ``None`` when there is none."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise AgentFileUnreadableError(f"can't read {path}: it is not UTF-8 text") from exc
    except OSError as exc:
        raise AgentFileUnreadableError(f"can't read {path}: {exc.strerror or exc}") from exc


def _check_settings(path: Path) -> None:
    """Refuse, naming it, a settings file the hooks cannot be written into: before any write.

    ``install_hooks`` edits the object it parses and writes it back, so it raises
    for text that is not a JSON object rather than replace it, and a file this user
    may not write (mode 444, or a link into a read-only ``/nix/store``) ended in a
    traceback after the context was ingested (review of #257). Asked here first, so
    a refusal comes before the context is ingested.
    """
    _read_agent_file(path)
    try:
        agent_core.read_settings(path)
    except agent_core.SettingsNotAnObjectError as exc:
        raise AgentFileUnreadableError(str(exc)) from exc
    unwritable = settings_unwritable(path)
    if unwritable is not None:
        raise AgentFileUnreadableError(f"can't write {path}: {unwritable}")


def connect_refusal(name: str, config_dir: Path | None = None) -> str | None:
    """Why `agents connect` would refuse ``config_dir``'s settings file, in its own words,
    or ``None`` when it would write the hooks. Reads only.

    The doctor asks it before offering Connect: a settings.json that is not a JSON
    object, or that this user may not write, can only fail the click (review of
    #257).
    """
    spec = agent_core.spec(name, config_dir)
    if spec is None or spec.settings_path is None:
        return None
    try:
        _check_settings(spec.settings_path)
    except AgentFileUnreadableError as exc:
        return str(exc)
    return None


def settings_unwritable(path: Path) -> str | None:
    """Why the hooks cannot be written into ``path``, or ``None`` when they can.

    The one rule for `connect`, `refresh-hooks`, and the upgrade and uninstall plans
    that promise them (review of #257). It asks about the file when it is there, else
    the directory it will be made in. access(2) follows a link and reports a
    read-only file system as well.
    """
    try:
        target = path if path.exists() else path.parent
        if not target.exists() or os.access(target, os.W_OK):
            return None
    except OSError as exc:
        return f"it cannot be reached ({exc.strerror or exc})"
    return "this user may not write it (it is read-only, or on a read-only file system)"


def _install_hooks(name: str, config_dir: Path | None, path: Path | None) -> bool:
    """``install_hooks``, with a write that fails anyway (the file changed since it was
    checked) reported like the check's refusal, never as a traceback."""
    try:
        return agent_core.install_hooks(name, config_dir)
    except OSError as exc:
        raise AgentFileUnreadableError(f"can't write {path}: {exc.strerror or exc}") from exc


def _make_first_run_dir(name: str, config_dir: Path | None) -> None:
    """Make the config dir an installed Claude Code that has never started has not made.

    npm and Homebrew create ``~/.claude`` only when ``claude`` first runs, and a
    missing directory reads as "not installed" (detected means the directory
    exists), so `agents connect`, `init --agent claude-code` and Welcome's Connect
    refused a Claude Code that is on PATH. Welcome alone used to make it (review of
    #257). With ``claude`` on PATH, the directory a session from this shell reads is
    made, as that first start would make it. A ``--config-dir`` is never made: a
    typo must not get hooks.
    """
    if name != "claude-code" or config_dir is not None or agent_core.claude_on_path() is None:
        return
    where = agent_core.ambient_hook_dir(name)
    if where is None or where.exists():
        return
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentFileUnreadableError(f"can't create {where}: {exc.strerror or exc}") from exc


def connect(name: str, config_dir: Path | None = None) -> AgentConnection:
    """Install aisquare's hooks into the agent and ingest its existing context.

    Installs SessionStart/UserPromptSubmit hooks (so the agent auto-injects
    aisquare context and aisquare captures prompts), then one-time-ingests the
    agent's context files (e.g. ``~/.claude/CLAUDE.md``) into the user pool.
    Raises ``KeyError`` for an unknown agent, :class:`UnsupportedAgentError`
    for one aisquare cannot connect yet, :class:`AgentNotInstalledError` if it
    is not installed, and :class:`AgentFileUnreadableError` for a file of its
    that cannot be read. The last three are ``ValueError``.

    A settings file that switches every hook off (``"disableAllHooks": true``)
    still gets the hooks, so they run once that key goes, and the result names it
    (``hooks_off``): until then Claude Code runs none of them, and "connected"
    there was not true (review of #257).
    """
    spec = agent_core.spec(name, config_dir)
    if spec is None:
        raise KeyError(name)
    if not spec.connectable:
        # Before anything is read or written: no context ingested, nothing in
        # agents.json, no ~/.aisquare created for a connection that installs nothing.
        planned = f"; support is planned for {spec.planned}" if spec.planned else ""
        raise UnsupportedAgentError(f"aisquare can't connect {spec.label} yet{planned}")
    _make_first_run_dir(name, config_dir)
    info = agent_core.detect(name, config_dir)
    if info is None or not info.detected:
        raise AgentNotInstalledError(f"{name} is not installed on this machine")

    # Every file is read before anything is written: a settings.json that cannot
    # be read stopped connect after the context was ingested and the home built.
    if spec.settings_path is not None:
        _check_settings(spec.settings_path)
    sections: list[str] = []
    for path in agent_core.context_files(name, config_dir):
        sections.extend(_split_sections(_read_agent_file(path) or ""))

    added = 0
    with store_session() as store:
        existing = {entry.text for entry in store.entries("user")}
        for text in sections:
            if text in existing:
                continue
            store.add(new_entry(text, "user", None, [name], name))
            existing.add(text)
            added += 1

    if not _install_hooks(name, config_dir, spec.settings_path):
        # Unreachable while `connectable` means a settings file to write. Raised, not
        # reported: a connection that installed nothing is what the refusal above is
        # for, and it must never be recorded, or printed, as connected.
        raise UnsupportedAgentError(f"aisquare can't connect {spec.label} yet")
    agent_core.set_connected(name, True, config_dir)
    return AgentConnection(
        name=name,
        hooks_installed=True,
        imported=added,
        hooks_off=agent_core.hooks_off(name, config_dir),
    )


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


def refresh_hooks(name: str, config_dir: Path | None = None) -> bool:
    """Rewrite aisquare's hooks in one directory for THIS version, and nothing else.

    What ``aisquare upgrade`` asks the new install to do for every directory it
    re-connects. :func:`connect` also re-reads the agent's context files and adds
    every section it does not already hold, so running it on each upgrade brought
    back a ``CLAUDE.md`` section the user had removed and added an edited one
    beside its old text. A refresh imports nothing and never opens the store.
    Returns whether hooks were written; raises ``KeyError`` for an unknown agent,
    :class:`AgentFileUnreadableError` for a settings file it may not rewrite, and
    ``ValueError`` when the agent is not installed there.
    """
    info = agent_core.detect(name, config_dir)
    if info is None:
        raise KeyError(name)
    if not info.detected:
        raise ValueError(f"{name} is not installed on this machine")
    spec = agent_core.spec(name, config_dir)
    path = spec.settings_path if spec is not None else None
    if path is not None:
        _check_settings(path)
    written = _install_hooks(name, config_dir, path)
    if written:
        agent_core.set_connected(name, True, config_dir)
    return written
