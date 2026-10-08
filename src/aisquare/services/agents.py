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
    return _refusals_named(agent_core.detect_all())


def scan() -> list[AgentInfo]:
    """Scan this machine for installed agents."""
    return _refusals_named(agent_core.detect_all())


def status(name: str | None = None) -> list[AgentInfo]:
    """Integration state for one agent, or all of them. Raises ``KeyError`` if unknown."""
    if name is None:
        return _refusals_named(agent_core.detect_all())
    info = agent_core.detect(name)
    if info is None:
        raise KeyError(name)
    return _refusals_named([info])


def _refusals_named(agents: list[AgentInfo]) -> list[AgentInfo]:
    """``agents``, with each site whose hooks are not installed (and not switched off) given
    the reason `agents connect` would refuse it (:func:`connect_refusal`), as the doctor and
    Welcome name it. Read as "missing", a settings.json with our hooks and one trailing comma
    sent the user to Connect, which can only fail there (review of #257)."""
    for agent in agents:
        for site in agent.sites:
            if not site.hooks_installed and site.hooks_off is None:
                site.refused = connect_refusal(agent.name, site.config_dir)
    return agents


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


def plugin_beside_note(name: str, config_dir: Path | None = None) -> str | None:
    """What connecting ``name`` in ``config_dir`` says when the aisquare plugin is enabled
    there too, else ``None``: its hooks stand down beside these, and the doctor warns of
    two routes. One sentence for `agents connect` and `init --agent`, whose Quickstart
    wrote the hooks beside the plugin and said nothing (review of #257). A plugin enabled
    for one repository is not named: beside the hooks it doubles nothing there.
    """
    plugin = claude_plugin(config_dir) if name == "claude-code" else None
    if plugin is None:
        return None
    return (
        f"the aisquare plugin is enabled in {plugin.config_dir} too — its hooks stand down "
        "while these run; keep one route (aisquare doctor says how)"
    )


def disconnect_notes(name: str, config_dir: Path | None = None, *, removed: bool) -> list[str]:
    """What `agents disconnect` says beside its ✓, given whether it ``removed`` any hooks.

    Each aisquare plugin that still runs aisquare for ``config_dir``, with the command that
    stops it: the plugin's hooks stand down only while settings.json runs aisquare's, so
    removing those hands every event to the plugin. At user scope it runs in every
    session; at project or local scope, in its repository's, where `agents status` and
    the doctor still said connected after a bare "✓ disconnected" (review of #257). Where
    nothing was removed and no plugin runs, the hooks may be in another config dir. Nothing
    for an agent aisquare has no hooks for (Codex, Cursor): the record an older aisquare's
    `agents connect` wrote is all disconnect clears, and no hooks were ever there.
    """
    spec = agent_core.spec(name, config_dir)
    if spec is None or not spec.connectable:
        return []
    notes: list[str] = []
    user = claude_plugin(config_dir) if name == "claude-code" else None
    if user is not None:
        notes.append(
            f"the aisquare plugin is still enabled in {user.config_dir}, so aisquare keeps "
            f"running there — to stop it: {claude_plugin_command('disable', user.config_dir)}"
        )
    if name == "claude-code" and agent_core.plugin_route_supported():
        notes.extend(
            f"the aisquare plugin is still enabled in {plugin.project} ({plugin.scope} scope), "
            "so aisquare keeps running in sessions started there — to stop it: "
            + agent_core.claude_plugin_command(
                "uninstall", plugin.config_dir, scope=plugin.scope, project=plugin.project
            )
            for plugin in agent_core.claude_repo_plugins(config_dir)
        )
    if not removed and not notes:
        notes.append(
            "no aisquare hooks found in that config dir — if you connected with "
            "--config-dir, disconnect with the same one"
        )
    return notes


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
    The message names both, and ``path`` is the file.
    """

    def __init__(self, message: str, path: Path | None = None) -> None:
        super().__init__(message)
        self.path = path


def _read_agent_file(path: Path) -> str | None:
    """The text of one of the agent's own files, or ``None`` when there is none."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise AgentFileUnreadableError(f"can't read {path}: it is not UTF-8 text", path) from exc
    except OSError as exc:
        raise AgentFileUnreadableError(f"can't read {path}: {exc.strerror or exc}", path) from exc


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
        raise AgentFileUnreadableError(str(exc), path) from exc
    unwritable = settings_unwritable(path)
    if unwritable is not None:
        raise AgentFileUnreadableError(f"can't write {path}: {unwritable}", path)


def _read_before_writing(name: str, config_dir: Path | None) -> list[str]:
    """Every file `agents connect` reads before it writes anything, read: the settings file
    the hooks go into (:func:`_check_settings`), then the context files it imports, whose
    text it returns. Raises :class:`AgentFileUnreadableError` for the first it cannot use.

    One function for :func:`connect` and :func:`refused_file`, so what the doctor, Welcome
    and the installer are told is what connect does: asking about settings.json alone,
    they offered Connect for a CLAUDE.md connect refuses (review of #257).
    """
    spec = agent_core.spec(name, config_dir)
    if spec is not None and spec.settings_path is not None:
        _check_settings(spec.settings_path)
    return [_read_agent_file(path) or "" for path in agent_core.context_files(name, config_dir)]


def refused_file(name: str, config_dir: Path | None = None) -> tuple[Path, str] | None:
    """The file `agents connect` would refuse in ``config_dir``, and why in its own words, or
    ``None`` when it would write the hooks. Reads only, and returns the refusal rather than
    raising it.

    Every check connect makes before it writes (:func:`_read_before_writing`): a
    settings.json that is not a JSON object, or that this user may not write, and a
    context file (``CLAUDE.md``) that is not UTF-8 text this user can read. A Connect
    offered there can only fail the click, so the doctor names the file instead.
    """
    spec = agent_core.spec(name, config_dir)
    if spec is None or not spec.connectable:
        return None
    try:
        _read_before_writing(name, config_dir)
    except AgentFileUnreadableError as exc:
        return (exc.path or spec.settings_path or spec.home), str(exc)
    return None


def connect_refusal(name: str, config_dir: Path | None = None) -> str | None:
    """Why `agents connect` would refuse ``config_dir``, in its own words, or ``None`` when
    it would write the hooks: :func:`refused_file`'s reason. Reads only.

    Asked by Welcome's step 2 and ``agents list``/``status``, and through
    :func:`refused_file` by the doctor's row, before Connect is offered or implied. A
    file connect refuses can only fail the click, and a read-only settings.json
    (home-manager's link into the Nix store) never cleared (review of #257).
    """
    refused = refused_file(name, config_dir)
    return None if refused is None else refused[1]


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
        raise AgentFileUnreadableError(f"can't write {path}: {exc.strerror or exc}", path) from exc


def _make_first_run_dir(name: str, config_dir: Path | None) -> None:
    """Make the config dir an installed Claude Code that has never started has not made.

    npm and Homebrew create ``~/.claude`` only when ``claude`` first runs, and a
    missing directory reads as "not installed" (detected means the directory
    exists), so `agents connect`, `init --agent claude-code` and Welcome's Connect
    refused a Claude Code that is on PATH. Welcome alone used to make it (review of
    #257). With ``claude`` on PATH, the directory a session from this shell reads is
    made, as that first start would make it, however it is named: the doctor names a
    recorded ``~/.claude`` that was removed with ``--config-dir``, and that Connect
    refused it as not installed while the bare one made it. Any other ``--config-dir``
    is never made: a typo must not get hooks.
    """
    if name != "claude-code" or agent_core.claude_on_path() is None:
        return
    where = agent_core.ambient_hook_dir(name)
    if where is None or where.exists():
        return
    if config_dir is not None:
        try:
            elsewhere = agent_core.dir_identity(config_dir) != agent_core.dir_identity(where)
        except RuntimeError:  # pathlib's symlink loop on 3.11 and 3.12: nothing to make
            elsewhere = True
        if elsewhere:
            return
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentFileUnreadableError(
            f"can't create {where}: {exc.strerror or exc}", where
        ) from exc


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
    sections = [
        section
        for text in _read_before_writing(name, config_dir)
        for section in _split_sections(text)
    ]

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
