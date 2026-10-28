"""``fleet adopt``: a conversation started by hand, brought under the fleet (#290).

Owner scope, 2026-10-23: phase B of the P3 handoff's W5 and a picker, nothing more. The
transcript is looked for under the CLI's KNOWN account config dirs only (each agent's slots and
its plain home), and the account it resumes on is the dir it lives in, so a resume never
crosses accounts. It is refused while a live process still holds the conversation (two
processes would write one conversation), when a fleet row already owns it, and when the
directory it ran in is no onboarded project (onboarding stays intentional). Otherwise it is
spawned on its own agent with ``ResumeSpec``: the agent's own resume (``AgentAdapter.
resume_args``: ``claude --resume <path>``, ``codex resume <id>``) keeps the session, so the
board row is the conversation's own and no second session row appears.

Every agent's transcript is its adapter's to read (``AgentTranscript``): which conversations a
config dir keeps (``conversations``), and of one only what this must: the folder it ran in and
the first thing the user typed, for the picker (``opening``). Never more of the conversation,
and none of it is stored. A transcript that cannot be read fails open.

A conversation is HELD when Claude Code's own registry says so (``<config dir>/sessions/<pid>
.json`` names its ``sessionId``, and that pid is alive), when a process's arguments name it
(``AgentAdapter.adoptable``: a ``claude --resume``/``--session-id``, the fleet's own launches
and older builds, or a ``codex resume <id>``), or, for an agent whose writer holds its
transcript open (``AgentTranscript.held_open``, Codex's), when a process of that agent's holds
it open (``core.process_table``): a ``codex`` started fresh names no thread. Codex's managed
app-server daemon, which runs a ``codex`` started with no arguments and keeps its rollout open
for minutes after it quit, holds it only while a client of Codex's runs. A stale entry
whose pid was reused can only make it refuse, which is the safe way to be wrong; a process
table that cannot be read says nothing, as a registry that cannot be read does.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import process_table, procs
from aisquare.core.agent_adapters import adapters, default_adapter, recorded_adapter
from aisquare.core.agent_adapters.types import (
    AgentAdapter,
    AgentTranscript,
    HoldsOpen,
    executable_name,
)
from aisquare.core.store import store_session
from aisquare.models import AdoptableConversation, ClaudeAccount, ProjectInfo
from aisquare.services import agent_events
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service

LIST_LIMIT = 10
"""How many conversations ``fleet adopt --list`` shows unless told."""
PROMPT_CHARS = 80
"""How much of the first prompt the picker shows."""
PS_TIMEOUT = 5.0
_SESSION_CHARS = re.compile(r"[0-9A-Za-z-]+")
"""What a session id (or the start of one) is made of."""


class AdoptRefused(fleet_service.FleetError):
    """``fleet adopt`` will not take this conversation; ``code`` says why, the message how."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Found:
    """One conversation on disk: its agent's own id for it, its transcript, the account whose
    home holds it and that account's agent."""

    native: str
    path: Path
    account: ClaudeAccount
    adapter: AgentAdapter
    reader: AgentTranscript

    @property
    def board_id(self) -> str:
        """The board's id for it (``agent_events.session_key``): the native id where the agent
        names its sessions at launch (Claude Code's), else one of its home and that id."""
        home = accounts_core.agent_config_dir(self.account)
        return agent_events.session_key(self.adapter, home, self.native)


def _found_in(account: ClaudeAccount) -> Iterator[_Found]:
    """The conversations the account's home keeps, as its agent's transcript reader lists
    them; none for an agent without one."""
    adapter = recorded_adapter(account.agent)
    reader = adapter.transcript_reader if adapter is not None else None
    if adapter is None or reader is None:
        return
    home = accounts_core.agent_config_dir(account)
    for native, path in reader.conversations(home):
        yield _Found(native, path, account, adapter, reader)


def _registry_holders(accounts: Sequence[ClaudeAccount]) -> dict[str, int]:
    """Session id → live pid, from each account's Claude Code session registry."""
    held: dict[str, int] = {}
    for account in accounts:
        with contextlib.suppress(OSError):
            for entry in (account.config_dir / "sessions").glob("*.json"):
                with contextlib.suppress(OSError, ValueError):
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    session, pid = data.get("sessionId"), data.get("pid")
                    if isinstance(session, str) and isinstance(pid, int) and procs.alive(pid):
                        held[session] = pid
    return held


Process = tuple[int, int, str]
"""``(pid, parent pid, arguments)`` of one process, as ``ps`` lists it."""


def _process_args() -> list[Process]:
    """Every process this user can see; none where ``ps`` cannot run."""
    if sys.platform == "win32":
        return []
    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    listed: list[Process] = []
    for line in done.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            listed.append((int(fields[0]), int(fields[1]), fields[2]))
    return listed


def _ancestors(processes: Sequence[Process], pid: int) -> set[int]:
    """``pid`` and every process above it: the shell and the agent that run this command are
    no holder of the session it adopts (review of #370)."""
    parents = {child: parent for child, parent, _args in processes}
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        pid = parents.get(pid, 0)
    return seen


class _Holders:
    """Who holds a conversation, read once for a listing: the registries, the processes this
    command does not run under, and, for an agent whose writer holds its transcript open, what
    that agent's processes hold (one read of the process table, asked of the first such
    conversation only)."""

    def __init__(
        self, accounts: Sequence[ClaudeAccount], processes: Sequence[Process] | None
    ) -> None:
        self._registry = _registry_holders(accounts)
        listed = _process_args() if processes is None else processes
        mine = _ancestors(listed, os.getpid())
        self._others = [(pid, args.split()) for pid, _parent, args in listed if pid not in mine]
        self._open: dict[str, tuple[list[tuple[int, HoldsOpen]], frozenset[str] | None]] = {}

    def of(self, native: str, transcript: Path, adapter: AgentAdapter) -> int | None:
        if (pid := self._registry.get(native)) is not None:
            return pid
        named = {native, str(transcript)}
        for pid, words in self._others:
            if any(each.adoptable(words) in named for each in adapters()):
                return pid
        return self._holding_open(transcript, adapter)

    def _holding_open(self, transcript: Path, adapter: AgentAdapter) -> int | None:
        """A process of ``adapter``'s that holds ``transcript`` open, as the agent's writer
        holds one (``AgentTranscript.held_open``): one that holds it for its own session (a
        TUI, an ``exec``); or one that holds it for the clients it serves (Codex's managed
        app-server daemon), held only while a client of the agent's runs, which is named, as
        the process to quit. ``None`` when none does or the process table cannot say.

        A process table names no thread a client shows: a daemon's hold counts while any
        process of the agent's that holds for its own session runs, one showing another
        thread or attached to another home's daemon among them. That refuses where it might
        not have to, which is the safe way to be wrong: two writers on one rollout are not."""
        reader = adapter.transcript_reader
        if reader is None:
            return None
        if adapter.id not in self._open:
            runs = [
                (pid, kind)
                for pid, words in self._others
                if any(executable_name(word) == adapter.binary for word in words)
                and (kind := reader.held_open(words)) is not None
            ]
            self._open[adapter.id] = (runs, process_table.open_files([pid for pid, _ in runs]))
        runs, held = self._open[adapter.id]
        real = os.path.realpath(transcript)
        if not held or real not in held:
            return None
        # Which of them: asked one by one, now that one of them is known to hold it.
        holders = [
            (pid, kind) for pid, kind in runs if real in (process_table.open_files([pid]) or ())
        ]
        own = next((pid for pid, kind in holders if kind == "session"), None)
        clients = [pid for pid, kind in runs if kind == "session"]
        return own if own is not None else clients[0] if clients else None


def holder(
    session_id: str,
    transcript: Path,
    accounts: Sequence[ClaudeAccount],
    *,
    agent: str | None = None,
    processes: list[Process] | None = None,
) -> int | None:
    """The pid of a live process holding the conversation ``session_id`` (``agent``'s own
    id for it), or ``None``: the registry first, then a process of any agent's whose
    arguments name it (``AgentAdapter.adoptable``), then one of ``agent``'s that holds its
    transcript open where its writer does; never this command's own process or one above it."""
    adapter = recorded_adapter(agent) or default_adapter()
    return _Holders(accounts, processes).of(session_id, transcript, adapter)


def _project_for(cwd: str | None, projects: Sequence[ProjectInfo]) -> ProjectInfo | None:
    """The onboarded project holding ``cwd``: the deepest root it is at or under."""
    if cwd is None:
        return None
    try:
        where = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return None
    roots = {p.id: p.root.resolve() for p in projects}
    holding = [p for p in projects if where == roots[p.id] or roots[p.id] in where.parents]
    return max(holding, key=lambda p: len(roots[p.id].parts), default=None)


def _conversation(
    found: _Found, projects: Sequence[ProjectInfo], held: int | None
) -> AdoptableConversation:
    cwd, typed = found.reader.opening(found.path)
    first: str | None = None
    if typed is not None:
        line = " ".join(typed.split())
        first = line if len(line) <= PROMPT_CHARS else line[: PROMPT_CHARS - 1] + "…"
    project = _project_for(cwd, projects)
    return AdoptableConversation(
        session_id=found.native,
        agent=found.adapter.id,
        transcript_path=str(found.path),
        slot=found.account.slot,
        account=accounts_core.label(found.account),
        cwd=cwd,
        project_id=project.id if project is not None else None,
        last_activity=datetime.fromtimestamp(found.path.stat().st_mtime, tz=UTC),
        first_prompt=first or None,
        held_by=held,
    )


def _known() -> list[ClaudeAccount]:
    return accounts_service.list_accounts()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def recent(limit: int = LIST_LIMIT) -> list[AdoptableConversation]:
    """The newest conversations across the known accounts that no fleet row owns, newest
    first, at most ``limit``: the picker the manager shows when the user asks what they
    were working on outside the CLI. On request only, never on its own."""
    accounts = _known()
    found: list[tuple[float, _Found]] = []
    seen: set[str] = set()
    for account in accounts:
        for each in _found_in(account):
            key = os.path.realpath(each.path)  # a `projects/` shared through a link is one file
            if key in seen:
                continue
            seen.add(key)
            with contextlib.suppress(OSError):
                found.append((each.path.stat().st_mtime, each))
    with store_session() as store:
        owned = store.fleet_session_ids()
        projects = store.list_projects()
    found.sort(key=lambda item: item[0], reverse=True)
    holders = _Holders(accounts, None)
    listed: list[AdoptableConversation] = []
    for _mtime_at, each in found:
        if len(listed) >= max(limit, 1):
            break
        if each.board_id in owned or each.native in {c.session_id for c in listed}:
            continue
        pid = holders.of(each.native, each.path, each.adapter)
        with contextlib.suppress(OSError):
            listed.append(_conversation(each, projects, pid))
    return listed


def _find(ref: str, accounts: Sequence[ClaudeAccount]) -> _Found:
    """The conversation ``ref`` names, its agent's id for it (or the start of one) or its
    transcript's path, among the known accounts' (:class:`AdoptRefused` when it is none)."""
    if ref.endswith(".jsonl"):
        given = Path(ref).expanduser()
        real = os.path.realpath(given) if given.is_file() else None
        homes = [
            each
            for account in accounts
            for each in _found_in(account)
            if real is not None and os.path.realpath(each.path) == real
        ]
        if not homes:
            raise AdoptRefused(
                "not_on_a_known_account",
                f"{ref} is no conversation under an account the CLI knows (its slots, and each "
                "agent's plain home): `aisquare accounts list` shows them",
            )
        return homes[0]
    # An id or the start of one (the list shows eight characters): only the characters a
    # session id has, and a prefix must name ONE conversation (review of #370: `*` matched
    # every transcript).
    if not ref or not _SESSION_CHARS.fullmatch(ref):
        raise AdoptRefused(
            "no_such_conversation",
            f"{ref!r} is no session id (or the start of one) — "
            "`aisquare fleet adopt --list` shows the recent ones",
        )
    matches = [
        each for account in accounts for each in _found_in(account) if each.native.startswith(ref)
    ]
    if not matches:
        raise AdoptRefused(
            "no_such_conversation",
            f"no conversation {ref!r} under the accounts the CLI knows — "
            "`aisquare fleet adopt --list` shows the recent ones",
        )
    sessions = sorted({each.native for each in matches})
    if len(sessions) > 1:
        raise AdoptRefused(
            "ambiguous",
            f"{ref!r} starts {len(sessions)} conversations ({', '.join(sessions[:4])}"
            f"{', …' if len(sessions) > 4 else ''}): give more of the id",
        )
    # The newest copy: a resume on another account goes on writing the original.
    return max(matches, key=lambda each: _mtime(each.path))


def find(ref: str) -> tuple[AdoptableConversation, Path]:
    """The conversation ``ref`` names, a session id (its agent's own) or a transcript's path,
    under a known account's home; :class:`AdoptRefused` when it is not there."""
    found = _find(ref, _known())
    with store_session() as store:
        projects = store.list_projects()
    return _conversation(found, projects, None), found.path


def adopt(ref: str, *, role: str = "coder", label: str | None = None) -> fleet_service.SpawnReceipt:
    """Bring the conversation ``ref`` names under the fleet: a row on its project, resumed
    by its own agent on its own account in its own directory, keeping its session."""
    accounts = _known()
    found = _find(ref, accounts)
    board_id = found.board_id
    with store_session() as store:
        owner = store.fleet_agent_for_session(board_id)
        if owner is None and board_id in store.fleet_session_ids():
            raise AdoptRefused(
                "owned_by_the_fleet",
                f"{found.native} was a fleet agent's: restart that agent (its sidebar row's "
                "Restart) rather than adopt it",
            )
        projects = store.list_projects()
    if owner is not None:
        raise AdoptRefused(
            "owned_by_the_fleet", f"{found.native} is already {owner.label} in the fleet"
        )
    if (pid := _Holders(accounts, None).of(found.native, found.path, found.adapter)) is not None:
        raise AdoptRefused(
            "held",
            f"a live {found.adapter.label} (pid {pid}) still holds {found.native}: quit it "
            "first, then adopt — two processes would write one conversation",
        )
    conversation = _conversation(found, projects, None)
    project = _project_for(conversation.cwd, projects)
    if project is None:
        where = conversation.cwd or "a directory its transcript does not record"
        raise AdoptRefused(
            "not_onboarded",
            f"{found.native} ran in {where}, which no onboarded project holds: "
            f"`aisquare project onboard {conversation.cwd or '<dir>'}` first",
        )
    return fleet_service.spawn(
        project,
        role,
        label=label,
        worktree=False,
        cwd=Path(conversation.cwd) if conversation.cwd else None,
        agent=found.adapter.id,
        account=str(found.account.slot),
        resume=fleet_service.ResumeSpec(
            board_id, found.path, found.native if found.native != board_id else None
        ),
    )
