"""``fleet adopt``: a Claude Code conversation started by hand, brought under the fleet (#290).

Owner scope, 2026-10-23: phase B of the P3 handoff's W5 and a picker, nothing more. The
transcript is looked for under the CLI's KNOWN account config dirs only (its slots; slot 1
is the plain claude's), and the account it resumes on is the dir it lives in, so a resume
never crosses accounts. It is refused while a live ``claude`` still holds the session (two
processes would write one conversation), when a fleet row already owns it, and when the
directory it ran in is no onboarded project (onboarding stays intentional). Otherwise it is
spawned with ``ResumeSpec(id, path)``: ``claude --resume <path>`` keeps the session id, so
the board row is the conversation's own and no second session row appears.

The transcript's format is internal to Claude Code; this reads only what it must and fails
open: the ``cwd`` an entry records, and the first thing the user typed, for the picker.
Never more of the conversation, and none of it is stored.

A session is HELD when Claude Code's own registry says so (``<config dir>/sessions/<pid>
.json`` names its ``sessionId``, and that pid is alive) or a process's arguments name it
(``--resume`` / ``--session-id``, the fleet's own launches and older builds). A stale entry
whose pid was reused can only make it refuse, which is the safe way to be wrong.
"""

from __future__ import annotations

import contextlib
import glob
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import claude_accounts as accounts_core
from aisquare.core.store import store_session
from aisquare.models import AdoptableConversation, ClaudeAccount, ProjectInfo
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service

LIST_LIMIT = 10
"""How many conversations ``fleet adopt --list`` shows unless told."""
SCAN_LINES = 400
"""How far into a transcript its ``cwd`` and first prompt are looked for."""
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


def _transcripts(account: ClaudeAccount) -> Iterator[Path]:
    """The account's conversation transcripts: ``projects/<slug>/<id>.jsonl`` (a subagent's,
    one level further down, is no conversation of its own)."""
    with contextlib.suppress(OSError):
        yield from (account.config_dir / "projects").glob("*/*.jsonl")


def _entries(path: Path) -> Iterator[dict[str, Any]]:
    """The transcript's first :data:`SCAN_LINES` entries that read as JSON objects."""
    with contextlib.suppress(OSError), path.open(encoding="utf-8", errors="replace") as file:
        for number, line in enumerate(file):
            if number >= SCAN_LINES:
                return
            with contextlib.suppress(ValueError):
                entry = json.loads(line)
                if isinstance(entry, dict):
                    yield entry


def _typed(entry: dict[str, Any]) -> str | None:
    """What the user typed in a ``user`` entry: text, never a tool result, a command's
    wrapper (``<command-name>…``) or a meta line."""
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        texts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        content = next((text for text in texts if isinstance(text, str)), None)
    if not isinstance(content, str) or not content.strip() or content.lstrip().startswith("<"):
        return None
    return content


def _read_head(path: Path) -> tuple[str | None, str | None]:
    """``(cwd, first prompt)`` from the head of a transcript; ``None`` for what it lacks."""
    cwd: str | None = None
    first: str | None = None
    for entry in _entries(path):
        if cwd is None and isinstance(entry.get("cwd"), str):
            cwd = entry["cwd"]
        if first is None and (typed := _typed(entry)) is not None:
            line = " ".join(typed.split())
            first = line if len(line) <= PROMPT_CHARS else line[: PROMPT_CHARS - 1] + "…"
        if cwd is not None and first is not None:
            break
    return cwd, first


_STILL_ACTIVE = 259
"""``GetExitCodeProcess``'s answer for a process that has not exited."""


def _alive_on_windows(pid: int) -> bool:
    """Ask Windows whether ``pid`` runs: open it for a query and read its exit code. A
    process this user may not open (``ERROR_ACCESS_DENIED``) is someone else's, alive."""
    if sys.platform != "win32":  # the guard mypy reads: WinDLL exists on Windows only
        return False
    import ctypes  # Windows only, off every other platform's import

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return bool(ctypes.get_last_error() == 5)  # ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _alive(pid: int) -> bool:
    """Whether process ``pid`` runs. Never ``os.kill(pid, 0)`` on Windows: signal 0 is
    ``CTRL_C_EVENT`` there, sent to the console group ``pid`` is in (rc/p3's Windows leg:
    adopt, asked about the test runner's own pid, interrupted the whole suite)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _alive_on_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, alive
    except OSError:
        return False
    return True


def _registry_holders(accounts: Sequence[ClaudeAccount]) -> dict[str, int]:
    """Session id → live pid, from each account's Claude Code session registry."""
    held: dict[str, int] = {}
    for account in accounts:
        with contextlib.suppress(OSError):
            for entry in (account.config_dir / "sessions").glob("*.json"):
                with contextlib.suppress(OSError, ValueError):
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    session, pid = data.get("sessionId"), data.get("pid")
                    if isinstance(session, str) and isinstance(pid, int) and _alive(pid):
                        held[session] = pid
    return held


Process = tuple[int, int, str]
"""``(pid, parent pid, arguments)`` of one process, as ``ps`` lists it."""

_SESSION_FLAGS = frozenset({"--resume", "-r", "--session-id"})
"""The ``claude`` flags that name the session a process runs."""


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
    """``pid`` and every process above it: the shell and the ``claude`` that run this
    command are no holder of the session it adopts (review of #370)."""
    parents = {child: parent for child, parent, _args in processes}
    seen: set[int] = set()
    while pid and pid not in seen:
        seen.add(pid)
        pid = parents.get(pid, 0)
    return seen


def _names_session(args: str, session_id: str, transcript: Path) -> bool:
    """Whether ``args`` is a ``claude`` program given this session by a session flag: a
    word naming ``claude`` and ``--resume``/``-r``/``--session-id`` followed by the id or
    the transcript. A shell whose command line merely mentions the id is no holder."""
    words = args.split()
    if not any(Path(word).name in {"claude", "claude.exe"} for word in words):
        return False
    named = {session_id, str(transcript)}
    given = list(itertools.pairwise(words))
    given += [(flag, value) for flag, _, value in (w.partition("=") for w in words if "=" in w)]
    return any(flag in _SESSION_FLAGS and value in named for flag, value in given)


def holder(
    session_id: str,
    transcript: Path,
    accounts: Sequence[ClaudeAccount],
    *,
    processes: list[Process] | None = None,
) -> int | None:
    """The pid of a live process holding the conversation, or ``None``: the registry first,
    then a ``claude`` given this session by a session flag, never this command's own
    process or one above it."""
    if (pid := _registry_holders(accounts).get(session_id)) is not None:
        return pid
    listed = _process_args() if processes is None else processes
    mine = _ancestors(listed, os.getpid())
    for pid, _parent, args in listed:
        if pid not in mine and _names_session(args, session_id, transcript):
            return pid
    return None


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
    path: Path,
    account: ClaudeAccount,
    projects: Sequence[ProjectInfo],
    held: int | None,
) -> AdoptableConversation:
    cwd, first = _read_head(path)
    project = _project_for(cwd, projects)
    return AdoptableConversation(
        session_id=path.stem,
        transcript_path=str(path),
        slot=account.slot,
        account=accounts_core.label(account),
        cwd=cwd,
        project_id=project.id if project is not None else None,
        last_activity=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        first_prompt=first,
        held_by=held,
    )


def _known() -> list[ClaudeAccount]:
    return accounts_service.list_accounts()


def recent(limit: int = LIST_LIMIT) -> list[AdoptableConversation]:
    """The newest conversations across the known accounts that no fleet row owns, newest
    first, at most ``limit``: the picker the manager shows when the user asks what they
    were working on in Claude outside the CLI. On request only, never on its own."""
    accounts = _known()
    found: list[tuple[float, Path, ClaudeAccount]] = []
    seen: set[str] = set()
    for account in accounts:
        for path in _transcripts(account):
            key = os.path.realpath(path)  # a `projects/` shared through a link is one file
            if key in seen:
                continue
            seen.add(key)
            with contextlib.suppress(OSError):
                found.append((path.stat().st_mtime, path, account))
    with store_session() as store:
        owned = store.fleet_session_ids()
        projects = store.list_projects()
    found.sort(key=lambda item: item[0], reverse=True)
    held = _registry_holders(accounts)
    processes = _process_args()
    listed: list[AdoptableConversation] = []
    for _mtime, path, account in found:
        if len(listed) >= max(limit, 1):
            break
        if path.stem in owned or path.stem in {c.session_id for c in listed}:
            continue
        pid = held.get(path.stem) or holder(path.stem, path, [], processes=processes)
        with contextlib.suppress(OSError):
            listed.append(_conversation(path, account, projects, pid))
    return listed


def find(ref: str) -> tuple[AdoptableConversation, Path]:
    """The conversation ``ref`` names, a session id or a ``.jsonl`` path, under a known
    account's ``projects/``; :class:`AdoptRefused` when it is not there."""
    accounts = _known()
    with store_session() as store:
        projects = store.list_projects()
    as_path = Path(ref).expanduser()
    if ref.endswith(".jsonl"):
        candidates = [as_path] if as_path.is_file() else []
        homes = [
            account
            for account in accounts
            for path in candidates
            if (account.config_dir / "projects").resolve() in path.resolve().parents
        ]
        if not homes:
            raise AdoptRefused(
                "not_on_a_known_account",
                f"{ref} is no transcript under an account the CLI knows (its slots, and the "
                "plain claude's ~/.claude): `aisquare accounts list` shows them",
            )
        path, account = candidates[0], homes[0]
    else:
        # An id or the start of one (the list shows eight characters): only the characters
        # a session id has, escaped all the same, and a prefix must name ONE conversation
        # (review of #370: `*` matched every transcript).
        if not ref or not _SESSION_CHARS.fullmatch(ref):
            raise AdoptRefused(
                "no_such_conversation",
                f"{ref!r} is no session id (or the start of one) — "
                "`aisquare fleet adopt --list` shows the recent ones",
            )
        matches = [
            (path, account)
            for account in accounts
            for path in (account.config_dir / "projects").glob(f"*/{glob.escape(ref)}*.jsonl")
        ]
        if not matches:
            raise AdoptRefused(
                "no_such_conversation",
                f"no conversation {ref!r} under the accounts the CLI knows — "
                "`aisquare fleet adopt --list` shows the recent ones",
            )
        sessions = sorted({path.stem for path, _account in matches})
        if len(sessions) > 1:
            raise AdoptRefused(
                "ambiguous",
                f"{ref!r} starts {len(sessions)} conversations ({', '.join(sessions[:4])}"
                f"{', …' if len(sessions) > 4 else ''}): give more of the id",
            )
        # The newest copy: a resume on another account goes on writing the original.
        path, account = max(matches, key=lambda m: m[0].stat().st_mtime)
    return _conversation(path, account, projects, None), path


def adopt(ref: str, *, role: str = "coder", label: str | None = None) -> fleet_service.SpawnReceipt:
    """Bring the conversation ``ref`` names under the fleet: a row on its project, resumed
    on its own account in its own directory, keeping its session id."""
    conversation, path = find(ref)
    with store_session() as store:
        owner = store.fleet_agent_for_session(conversation.session_id)
        if owner is None and conversation.session_id in store.fleet_session_ids():
            raise AdoptRefused(
                "owned_by_the_fleet",
                f"{conversation.session_id} was a fleet agent's: restart that agent (its "
                "sidebar row's Restart) rather than adopt it",
            )
        projects = store.list_projects()
    if owner is not None:
        raise AdoptRefused(
            "owned_by_the_fleet", f"{conversation.session_id} is already {owner.label} in the fleet"
        )
    if (pid := holder(conversation.session_id, path, _known())) is not None:
        raise AdoptRefused(
            "held",
            f"a live claude (pid {pid}) still holds {conversation.session_id}: quit it first, "
            "then adopt — two processes would write one conversation",
        )
    project = _project_for(conversation.cwd, projects)
    if project is None:
        where = conversation.cwd or "a directory its transcript does not record"
        raise AdoptRefused(
            "not_onboarded",
            f"{conversation.session_id} ran in {where}, which no onboarded project holds: "
            f"`aisquare project onboard {conversation.cwd or '<dir>'}` first",
        )
    return fleet_service.spawn(
        project,
        role,
        label=label,
        worktree=False,
        cwd=Path(conversation.cwd) if conversation.cwd else None,
        account=str(conversation.slot),
        resume=fleet_service.ResumeSpec(conversation.session_id, path),
    )
