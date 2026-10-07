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


def claude_code_connected(config_dir: Path | None = None) -> bool:
    """Whether Claude Code in ``config_dir`` runs aisquare: the one "connected?" answer.

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
    (:func:`claude_plugin`), whose hooks run the same ``aisquare hook <event>``.
    A partial install from an older version answers False, because Connect is
    what completes it. So does ``"disableAllHooks": true``, which Connect
    cannot change: a surface that offers Connect asks
    ``agent_core.hooks_disabled`` first and says so instead, as the doctor's row
    does. Which aisquare the hooks run is the doctor's question, not this one:
    answering it can start a process.

    Read-only and offline, and it never raises: one file is read and nothing is
    written, so no ``~/.aisquare`` appears. ``agents.json`` is not consulted,
    because hooks on disk run whether or not this home recorded them (#84). A
    ``settings.json`` that cannot be read answers False: nothing shows our hooks
    are there.
    """
    return agent_core.claude_code_connected(config_dir)


def claude_plugin(config_dir: Path | None = None) -> agent_core.ClaudePlugin | None:
    """The aisquare Claude Code plugin in ``config_dir``, when it is installed and enabled."""
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
    """A file ``connect`` must read (``CLAUDE.md``, ``settings.json``) is not readable UTF-8 text.

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


def connect(name: str, config_dir: Path | None = None) -> AgentConnection:
    """Install aisquare's hooks into the agent and ingest its existing context.

    Installs SessionStart/UserPromptSubmit hooks (so the agent auto-injects
    aisquare context and aisquare captures prompts), then one-time-ingests the
    agent's context files (e.g. ``~/.claude/CLAUDE.md``) into the user pool.
    Raises ``KeyError`` for an unknown agent, :class:`UnsupportedAgentError`
    for one aisquare cannot connect yet, :class:`AgentNotInstalledError` if it
    is not installed, and :class:`AgentFileUnreadableError` for a file of its
    that cannot be read. The last three are ``ValueError``.
    """
    spec = agent_core.spec(name, config_dir)
    if spec is None:
        raise KeyError(name)
    if not spec.connectable:
        # Before anything is read or written: no context ingested, nothing in
        # agents.json, no ~/.aisquare created for a connection that installs nothing.
        planned = f"; support is planned for {spec.planned}" if spec.planned else ""
        raise UnsupportedAgentError(f"aisquare can't connect {spec.label} yet{planned}")
    info = agent_core.detect(name, config_dir)
    if info is None or not info.detected:
        raise AgentNotInstalledError(f"{name} is not installed on this machine")

    # Every file is read before anything is written: a settings.json that cannot
    # be read stopped connect after the context was ingested and the home built.
    if spec.settings_path is not None:
        _read_agent_file(spec.settings_path)
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

    if not agent_core.install_hooks(name, config_dir):
        # Unreachable while `connectable` means a settings file to write. Raised, not
        # reported: a connection that installed nothing is what the refusal above is
        # for, and it must never be recorded, or printed, as connected.
        raise UnsupportedAgentError(f"aisquare can't connect {spec.label} yet")
    agent_core.set_connected(name, True, config_dir)
    return AgentConnection(name=name, hooks_installed=True, imported=added)


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
