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
import json
import os
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


def _alive(pid: int) -> bool:
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


def _process_args() -> list[tuple[int, str]]:
    """``(pid, arguments)`` of every process this user can see; none where ``ps`` cannot run."""
    if sys.platform == "win32":
        return []
    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    listed: list[tuple[int, str]] = []
    for line in done.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            listed.append((int(pid), args))
    return listed


def holder(
    session_id: str,
    transcript: Path,
    accounts: Sequence[ClaudeAccount],
    *,
    processes: list[tuple[int, str]] | None = None,
) -> int | None:
    """The pid of a live process holding the conversation, or ``None``: the registry first,
    then a ``claude`` whose arguments name the session or its transcript."""
    if (pid := _registry_holders(accounts).get(session_id)) is not None:
        return pid
    for pid, args in _process_args() if processes is None else processes:
        if (
            pid != os.getpid()
            and "claude" in args
            and (session_id in args or str(transcript) in args)
        ):
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
        matches = [
            (path, account)
            for account in accounts
            for path in (account.config_dir / "projects").glob(f"*/{ref}.jsonl")
        ]
        if not matches:
            raise AdoptRefused(
                "no_such_conversation",
                f"no conversation {ref!r} under the accounts the CLI knows — "
                "`aisquare fleet adopt --list` shows the recent ones",
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
