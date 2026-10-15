"""The broker a sandboxed agent's board calls go through.

An agent that sandboxes its tools (Codex, ``capabilities.sandboxes_tools``) has the
aisquare store read-only in its sandbox (``AgentAdapter.store_access``), and with it what
the project's trees hold that a launch outside any sandbox reads, the other agents'
worktrees and the tmux sockets' directories (:func:`launch_paths`): nothing a process
there writes reaches any of them, so nothing it writes can reach a replay, a kill, a
typed line, a launch, a model or account choice, a worktree removal or a paste outside
it. The board calls its role protocol makes still work there, unchanged (``aisquare note …``,
``aisquare task claim … --as <id>``): the CLI, finding the store read-only and its launch's
broker in its environment (:data:`DIR_ENV`), hands its argv and cwd to the broker and
prints what the command printed, with its exit status (:func:`forward`).

The broker is one process per launch, which ``aisquare launch`` starts outside the sandbox
before it execs the agent, as it starts the telemetry receiver
(``services.native_telemetry``); it exits with the agent (:func:`start_for_launch`, :func:`serve`).
It runs a request as ``aisquare <argv>`` in the LAUNCH's environment, never the caller's,
when the argv names a command on :data:`WHITELIST`, the cwd is in the launch's own tree
(its worktree or its project's directory) and the request is signed with the launch's
nonce (:data:`NONCE_ENV`, :func:`signature`), once; anything else is refused with a line
saying why, logged (:func:`record_refusal`) and, for a forged identity, another board's
task or a fleet operation, put on its board once. The command it runs acts only as the
launch's own board sessions (:data:`CALLER_ENV`, held where every ``--as`` resolves,
``services.team``): the ones the agent's own hooks, which its agent runs outside the
sandbox, bound to this launch (``core.agent_sessions.bind_launch_session``); only on the
launch's own board (:data:`BOARD_ENV`: a task of another board is not one it may name)
and in its own project's memory pool (:data:`POOL_ENV`: never the user pool, which every
project's agents are given). It never runs a fleet operation.

The transport, measured on codex-cli 0.162.1 (Linux bwrap, a real ``exec_command``): the
sandbox's seccomp filter refuses ``connect`` on every unix socket (EPERM: a path one on a
read-only mount and on a writable one, an abstract one), so no socket can carry a request.
A file can, in a directory the profile makes writable, and a FIFO can, opened for writing
on a read-only path too. So each launch has its own directory ``<home>/broker/<launch>/``
(:func:`launch_dir`): ``in/``, the one path in the store that launch's sandbox may write
(``AgentAdapter.broker_access`` names it in that launch's profile and no other), where the
CLI puts a request; ``bell``, a FIFO it writes a byte to; and ``out/``, read-only to the
sandbox, where the broker answers. No command on the whitelist reads its stdin, so none
is forwarded: the broker's command reads ``/dev/null``.
"""

from __future__ import annotations

import argparse
import errno
import hmac
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aisquare.core import paths, selfcli

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.types import AgentAdapter

DIR_NAME = "broker"
"""The launches' broker directories, under the aisquare home (:func:`launch_dir`)."""
DIR_ENV = "AISQUARE_BROKER"
"""The launch's broker directory, in the agent's environment and so its tools' shells."""
NONCE_ENV = "AISQUARE_BROKER_NONCE"
"""The nonce every request of the launch carries. Not ``*TOKEN*``/``*KEY*``/``*SECRET*``:
Codex keeps variables named so out of its tools' shells by default
(``shell_environment_policy``), where the CLI must read it."""
CALLER_ENV = "AISQUARE_BROKERED"
"""Set (``1``) in the environment of the command a broker runs: it acts only as its
launch's own sessions (``services.team``) and sets none of the fleet's own signals."""
BOARD_ENV = "AISQUARE_BROKERED_BOARD"
"""The launch's board (a project id), in the environment of the command a broker runs:
the one board it writes and the one whose tasks it may name (``services.team``)."""
POOL_ENV = "AISQUARE_BROKERED_POOL"
"""The launch's project (an id), in the environment of the command a broker runs: the one
memory pool a brokered ``remember`` or ``context add`` writes (``services.context``)."""
LOG_NAME = "store-broker.log"
"""The brokers' refusals, one line each, in the aisquare home's log directory."""
#: The refusals put on the launch's board as well as logged, once per launch and kind.
NOTED = frozenset({"identity", "board", "fleet", "signature"})
INBOX, OUTBOX, BELL, READY = "in", "out", "bell", "ready.json"
MAX_REQUEST = 1_000_000
MAX_OUTPUT = 4_000_000
COMMAND_TIMEOUT = 300.0
"""Seconds a command may run: a board call waits out a locked store for its retry
budget (``core.store``), never this long."""
ANSWER_WAIT = COMMAND_TIMEOUT + 30.0
RUNNING_AT_ONCE = 4
ANSWER_TTL = 120.0
"""Seconds an answer, or a part a caller left, stays before the broker removes it."""
_POLL = 0.5
_RID = re.compile(r"[0-9a-f]{32}")

WHITELIST: dict[tuple[str, ...], str] = {
    ("note",): "share a note, decision, question or result",
    ("board",): "read the board (not --watch)",
    ("recall",): "ask the project brain",
    ("task", "add"): "add a task",
    ("task", "list"): "list tasks",
    ("task", "show"): "show a task",
    ("task", "claim"): "claim a task",
    ("task", "next"): "take the next task",
    ("task", "review"): "send a task to review",
    ("task", "reopen"): "reopen a task",
    ("task", "done"): "finish a task",
    ("task", "block"): "block a task",
    ("task", "drop"): "drop a task",
    ("task", "release"): "release a claim",
    ("team", "status"): "read the board",
    ("team", "focus"): "announce your focus",
    ("team", "log"): "read the team pipe",
    ("team", "verify"): "re-check a receipt",
    ("team", "signal"): "set or read a board signal (not the fleet's own)",
    ("team", "signals"): "list board signals",
    ("remember",): "remember a fact",
    ("inject",): "inject context",
    ("log",): "show captured prompts",
    ("context", "list"): "list context entries",
    ("context", "show"): "show a context entry",
    ("context", "search"): "search context entries",
    ("context", "preview"): "preview injected context",
    ("context", "add"): "add a context entry",
}
"""Every command a sandboxed agent's broker runs, by its path: what the role protocols
(``core.harness``) and the memory use, the MCP server's tools' set
(``services.mcp_server``). Never a fleet operation (spawn, stop, restart, switch, tell,
shutdown …), a launch, a hook, a binding, the config or an account: those choose or run
something outside the sandbox."""
#: The CLI's command groups, whose commands are two words, by every name they go by.
_GROUPS = {
    **{
        name: name
        for name in (
            "accounts",
            "agents",
            "auth",
            "config",
            "context",
            "explainability",
            "fleet",
            "hook",
            "project",
            "task",
            "team",
        )
    },
    "ctx": "context",
    "workspace": "project",
}
_GLOBAL_FLAGS = frozenset({"--json", "--verbose", "-v", "--quiet", "-q", "--no-color"})
_HELP = frozenset({"--help", "-h"})


class BrokerUnavailable(RuntimeError):
    """The launch's broker could not take or answer a request."""


class BrokerGone(BrokerUnavailable):
    """No broker listens on the launch's bell (``ENXIO``: a FIFO with no reader)."""


class OutOfScope(PermissionError):
    """A command a sandboxed agent's broker runs reached past its launch's own board or
    project pool (:data:`BOARD_ENV`, :data:`POOL_ENV`)."""

    def __init__(self, message: str, *, kind: str = "board") -> None:
        super().__init__(message)
        self.kind = kind


def brokered_call(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process is a command a sandboxed agent's broker runs for it."""
    return (os.environ if environ is None else environ).get(CALLER_ENV) == "1"


_SCOPED_BOARD: ContextVar[str | None] = ContextVar("store_broker_scoped_board", default=None)


@contextmanager
def scoped_board(board: str | None) -> Iterator[None]:
    """Bind what runs inside to ``board`` as a brokered command is (:func:`own_board`): the
    MCP tools of an agent that sandboxes its tools (``services.mcp_server``)."""
    token = _SCOPED_BOARD.set(board)
    try:
        yield
    finally:
        _SCOPED_BOARD.reset(token)


def own_board(environ: Mapping[str, str] | None = None) -> str | None:
    """The launch's board for a brokered command (:data:`BOARD_ENV`), or the board a
    sandboxed agent's MCP tool is bound to (:func:`scoped_board`); ``None`` for any other."""
    source = os.environ if environ is None else environ
    if brokered_call(source):
        return source.get(BOARD_ENV) or None
    return _SCOPED_BOARD.get()


def own_pool(environ: Mapping[str, str] | None = None) -> str | None:
    """The launch's project pool for a brokered command (:data:`POOL_ENV`); ``None`` for any
    other."""
    source = os.environ if environ is None else environ
    return (source.get(POOL_ENV) or None) if brokered_call(source) else None


def record_refusal(kind: str, detail: str, *, board: str | None = None) -> None:
    """Leave a record of a refusal outside the caller's stderr: one line in the brokers'
    log (:data:`LOG_NAME`), and, for the kinds in :data:`NOTED` (a session that is not its
    own, another board's task, a fleet operation, a request not signed by its launch),
    one note on the launch's board (``board``, else :data:`BOARD_ENV`) per launch and
    kind, naming the agent, for its manager and the human. Never raises."""
    from datetime import UTC, datetime

    launch = os.environ.get("AISQUARE_LAUNCH_ID", "").strip() or "?"
    agent = os.environ.get("AISQUARE_FLEET_AGENT", "").strip()
    flat = " ".join(detail.split())
    stamp = datetime.now(UTC).isoformat()
    line = f"{stamp} launch={launch} agent={agent or '-'} {kind}: {flat}\n"
    with suppress(OSError):
        logs = paths.log_dir()
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(line)
    board = board or os.environ.get(BOARD_ENV) or None
    if kind not in NOTED or board is None:
        return
    with suppress(Exception):
        from aisquare.services import team

        team.note_refusal(
            board,
            f"{_who(agent)}: its sandbox asked its store broker for what it may not do, "
            f"refused ({kind}): {flat[:300]} — later refusals of this kind are in "
            f"{paths.log_dir() / LOG_NAME}",
            key=f"broker-refused:{launch}:{kind}",
        )


def _who(agent: str) -> str:
    """The agent a refusal is about, by its label where its launch record names one."""
    if agent:
        with suppress(Exception):
            from aisquare.core import launch_seal

            record = launch_seal.read(agent)
            if isinstance(record, launch_seal.LaunchRecord) and record.label:
                return f"{record.label} ({agent})"
        return agent
    role = os.environ.get("AISQUARE_ROLE", "").strip()
    return f"the {role} launched by hand" if role else "an agent launched by hand"


def launch_dir(store: Path, launch_id: str) -> Path:
    """The broker directory of the launch ``launch_id`` under the aisquare home ``store``."""
    return store / DIR_NAME / launch_id


# --- what runs: the whitelist -----------------------------------------------------------


def command_path(argv: Sequence[str]) -> tuple[tuple[str, ...], list[str]] | str:
    """The command ``argv`` names, canonical (``ctx`` is ``context``), and the arguments
    after it; or why it names none the broker can tell apart. Only the global output flags
    may come before it (``cli.global_flags``)."""
    path: list[str] = []
    rest = list(argv)
    while rest:
        token = rest[0]
        if token in _GLOBAL_FLAGS:
            rest.pop(0)
            continue
        if token.startswith("-"):
            if path or token in _HELP:
                break
            return f"the option {token} before the command"
        path.append(_GROUPS.get(token, token) if not path else token)
        rest.pop(0)
        if len(path) == 2 or path[0] not in _GROUPS.values():
            break
    return tuple(path), rest


def _short_cluster(token: str, flag: str) -> bool:
    return token.startswith("-") and not token.startswith("--") and flag in token[1:]


def vet(argv: Sequence[str]) -> str | None:
    """Why the broker refuses ``argv``, or ``None`` when it runs it."""
    named = command_path(argv)
    if isinstance(named, str):
        return f"aisquare from this sandbox cannot take {named}. {_runs()}"
    path, rest = named
    shown = " ".join(path) or "with no command"
    asks_help = any(token in _HELP for token in rest)
    if path in WHITELIST:
        if path == ("board",) and any(
            token == "--watch" or token.startswith("--watch=") or _short_cluster(token, "w")
            for token in rest
        ):
            return "aisquare board --watch is a full-screen view: read the board without it"
        return None
    if asks_help and any(known[: len(path)] == path for known in WHITELIST):
        return None
    if path and path[0] == "fleet":
        return (
            f"aisquare {shown} is a fleet operation, which the broker never runs for a "
            "sandboxed agent: ask for it to run outside the sandbox (an approval), or "
            f"leave it to the operator. {_runs()}"
        )
    return (
        f"aisquare {shown} cannot run from this agent's sandbox, where the aisquare store "
        f"is read-only. {_runs()}"
    )


def refusal_kind(argv: Sequence[str]) -> str:
    """How :func:`vet`'s refusal of ``argv`` is recorded: ``fleet`` for a fleet operation,
    ``command`` for anything else (:func:`record_refusal`)."""
    named = command_path(argv)
    return "fleet" if not isinstance(named, str) and named[0][:1] == ("fleet",) else "command"


def _runs() -> str:
    names = sorted(" ".join(path) for path in WHITELIST)
    return "Its broker runs only: " + ", ".join(names) + "."


# --- the CLI's side ---------------------------------------------------------------------


def brokered(environ: Mapping[str, str] | None = None) -> Path | None:
    """The launch's broker directory when this process must go through it: one is named
    (:data:`DIR_ENV`) and the aisquare home is there but may not be written, as in the
    agent's sandbox. ``None`` everywhere else: the agent's own hooks and an escalated
    command run outside the sandbox and write the store themselves."""
    source = os.environ if environ is None else environ
    where = source.get(DIR_ENV, "").strip()
    if not where:
        return None
    home = paths.aisquare_home()
    if not home.is_dir() or _writable(home):
        return None
    return Path(where)


def _writable(home: Path) -> bool:
    """Whether this process may write in ``home``, asked of the store itself: a file made
    there and removed. ``access(2)`` alone sees bwrap's read-only mount, not a sandbox that
    refuses at ``open`` (Landlock does not hook ``access``). No variable tells: Codex 0.162.1
    gives a command it escalated out of its sandbox the very environment of a sandboxed
    one (``CODEX_PERMISSION_PROFILE``, ``CODEX_SANDBOX_NETWORK_DISABLED``; measured), and an
    escalated command must write the store itself (a manager's approved ``fleet spawn``)."""
    if not os.access(home, os.W_OK):
        return False
    import tempfile

    try:
        fd, name = tempfile.mkstemp(prefix=".write-probe-", dir=home)
    except OSError:
        return False
    os.close(fd)
    with suppress(OSError):
        os.unlink(name)
    return True


def forward(argv: Sequence[str]) -> int | None:
    """Run ``argv`` through the launch's broker when this process is sandboxed
    (:func:`brokered`), printing what the command printed; its exit status, or ``None``
    when the CLI runs it itself. ``--version`` reads nothing and runs here, and a hook
    here is a nested agent's, whose sessions bind nothing (``core.agent_sessions.
    own_process``): it does nothing, quietly."""
    broker = brokered()
    if broker is None:
        return None
    first = next((token for token in argv if token not in _GLOBAL_FLAGS), None)
    if first in ("--version", "-V"):
        return None
    if first == "hook":
        return 0
    try:
        answer = ask(broker, list(argv))
    except BrokerUnavailable as exc:
        sys.stderr.write(f"aisquare: {exc}\n")
        return 75
    sys.stdout.write(str(answer.get("stdout", "")))
    sys.stdout.flush()
    sys.stderr.write(str(answer.get("stderr", "")))
    code = answer.get("code")
    return code if isinstance(code, int) else 1


def _columns() -> int | None:
    try:
        return os.get_terminal_size(sys.stdout.fileno()).columns
    except (OSError, ValueError):
        return None


def _ring(bell: Path) -> bool:
    """One byte to the broker's bell: ``True`` when rung; ``False`` when this process may
    not open the FIFO (a sandbox that refuses it: the broker polls ``in/`` all the same);
    :class:`BrokerGone` when no broker listens (``ENXIO``: a FIFO with no reader)."""
    if sys.platform == "win32":
        raise BrokerGone("a broker's bell is a FIFO, which Windows has not")
    try:
        fd = os.open(bell, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT):
            raise BrokerGone(
                f"this agent's broker is not running ({exc.strerror}): the aisquare store is "
                "read-only in its sandbox, so restart the agent to have its board calls work"
            ) from exc
        return False
    try:
        with suppress(BlockingIOError):
            os.write(fd, b"\0")
    finally:
        os.close(fd)
    return True


def _withdraw(request: Path) -> bool:
    """Take back ``request`` before the broker takes it: ``True`` when it was still in
    ``in/`` (so it never runs), ``False`` when the broker had taken it (it may run)."""
    try:
        os.unlink(request)
    except OSError:
        return False
    return True


def signature(nonce: str, body: Mapping[str, object]) -> str:
    """The request ``body``'s MAC under the launch's nonce: a request names its launch by
    it, and a request file, which every sandbox may read (the store is readable), never
    shows the nonce itself."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(nonce.encode(), canonical, "sha256").hexdigest()


def ask(broker: Path, argv: list[str], *, wait: float = ANSWER_WAIT) -> dict[str, Any]:
    """Hand ``argv`` to the broker in ``broker`` and wait for its answer.

    A failure is said only when the command did not run: the request is withdrawn from
    ``in/`` first, and one the broker took is waited on. So a retrying agent never files a
    note or a task twice (review r10 of #113, finding 2): a bell this process may not open
    is not a broker that is gone, and the broker polls ``in/`` every half second."""
    rid = uuid.uuid4().hex
    body = {"v": 1, "rid": rid, "argv": argv, "cwd": os.getcwd(), "columns": _columns()}
    request = {**body, "mac": signature(os.environ.get(NONCE_ENV, ""), body)}
    inbox = broker / INBOX
    part, filed = inbox / f"{rid}.part", inbox / f"{rid}.json"
    try:
        with part.open("x", encoding="utf-8") as handle:
            json.dump(request, handle)
        os.replace(part, filed)
    except OSError as exc:
        with suppress(OSError):
            part.unlink()
        raise BrokerUnavailable(
            f"cannot hand the command to this agent's broker in {broker} ({exc})"
        ) from exc
    bell = broker / BELL
    gone: BrokerGone | None = None
    try:
        _ring(bell)
    except BrokerGone as exc:
        gone = exc
    answer = broker / OUTBOX / f"{rid}.json"
    deadline = time.monotonic() + wait
    checked = time.monotonic()
    delay = 0.005
    while True:
        try:
            parsed = json.loads(answer.read_text(encoding="utf-8"))
        except FileNotFoundError:
            parsed = None
        except (OSError, ValueError) as exc:
            raise BrokerUnavailable(f"this agent's broker answered unreadably ({exc})") from exc
        if isinstance(parsed, dict):
            return parsed
        now = time.monotonic()
        if gone is not None:
            if _withdraw(filed):
                raise gone
            # Taken just before its broker went: give it the time to answer, no more.
            deadline = min(deadline, now + 5)
            gone = None
        if now >= deadline:
            if _withdraw(filed):
                raise BrokerUnavailable(
                    f"this agent's broker did not take the command in {wait:.0f}s; it did not run"
                )
            raise BrokerUnavailable(
                f"this agent's broker took the command and did not answer in {wait:.0f}s: it "
                "may have run, so read the board (aisquare team log) before running it again"
            )
        if now - checked >= 1.0:
            checked = now
            try:
                _ring(bell)  # still listening: else the command can never be answered
            except BrokerGone as exc:
                gone = exc
        time.sleep(delay)
        delay = min(delay * 2, 0.05)


# --- the broker's side ------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _write_answer(outbox: Path, rid: str, answer: Mapping[str, object]) -> None:
    """The answer to ``rid``, whole or not at all: ``out/`` is the broker's alone."""
    part = outbox / f".{rid}.part"
    with suppress(OSError):
        part.write_text(json.dumps(answer), encoding="utf-8")
        os.replace(part, outbox / f"{rid}.json")


def _refused(message: str, code: int = 2) -> dict[str, object]:
    return {"code": code, "stdout": "", "stderr": f"aisquare: {message}\n"}


def _remove(inbox_fd: int, name: str) -> bool:
    """Remove the entry ``name`` of ``in/``, whatever the sandbox made it (a file, a link, a
    FIFO, a directory with anything in it), never following a link. ``False`` when it stays."""
    try:
        os.unlink(name, dir_fd=inbox_fd)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass
    try:
        shutil.rmtree(name, dir_fd=inbox_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _take(inbox_fd: int, name: str) -> bytes | str | None:
    """The request ``name`` in ``in/``, taken; why it is not one; or ``None`` when the
    caller withdrew it first. Taken by renaming it out of the CLI's reach in one step, so
    the CLI's withdrawal and the broker's take cannot both succeed (:func:`ask`); then read
    by name in the directory the broker made, never through a link, never blocking on a
    FIFO, never past :data:`MAX_REQUEST`; and removed, whatever it was."""
    if sys.platform == "win32":
        return "the broker runs on POSIX only"
    taken = f".{name}.taken"
    try:
        os.rename(name, taken, src_dir_fd=inbox_fd, dst_dir_fd=inbox_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _remove(inbox_fd, name)
        return f"the request could not be taken ({exc.strerror})"
    try:
        try:
            fd = os.open(taken, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox_fd)
        except OSError as exc:
            return f"the request could not be opened ({exc.strerror})"
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                return "the request is not a file"
            if status.st_size > MAX_REQUEST:
                return f"the request is over {MAX_REQUEST} bytes"
            chunks: list[bytes] = []
            size = 0
            while size <= MAX_REQUEST:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > MAX_REQUEST:
                return f"the request is over {MAX_REQUEST} bytes"
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        _remove(inbox_fd, taken)


def _capped(stream: Any, into: list[bytes], cut: list[bool]) -> None:
    """Read ``stream`` to its end, keeping its first :data:`MAX_OUTPUT` bytes in ``into`` and
    saying in ``cut`` whether more came: a command's output is never held past the cap."""
    kept = 0
    while chunk := stream.read(65536):
        room = MAX_OUTPUT - kept
        if room > 0:
            into.append(chunk[:room])
            kept += min(len(chunk), room)
        if len(chunk) > room:
            cut[0] = True
    stream.close()


@dataclass(frozen=True)
class Scope:
    """Where a launch's broker acts: its board, its project's memory pool, and the
    directories a request may run in (its worktree and its project's directory)."""

    board: str
    pool: str
    roots: tuple[Path, ...]

    def holds(self, cwd: Path) -> bool:
        resolved = _resolved(cwd)
        return any(resolved == root or resolved.is_relative_to(root) for root in self.roots)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def launch_scope(cwd: Path, environ: Mapping[str, str]) -> Scope:
    """The launch's own board, pool and tree: its launch record's project and directories
    for a fleet agent (``core.launch_seal``, written by its spawner, read-only to the
    sandbox), its own directory's otherwise. Never the cwd of a request: with
    ``AISQUARE_TEAM_HUB`` every directory resolves to the hub's board (review r10 of #113,
    finding 3)."""
    from aisquare.core import launch_seal
    from aisquare.core.orchestrator import team_project
    from aisquare.core.workspace import current_project

    roots: list[Path] = [cwd]
    board: str | None = None
    agent = environ.get("AISQUARE_FLEET_AGENT", "").strip()
    if agent:
        record = launch_seal.read(agent)
        if isinstance(record, launch_seal.LaunchRecord):
            board = record.project_id
            roots += [Path(record.project_root), Path(record.cwd)]
    if board is None:
        board = team_project(cwd).id
    pool = current_project(cwd).id
    return Scope(board, pool, tuple(dict.fromkeys(_resolved(root) for root in roots)))


class Broker:
    """One launch's broker: its directory, its nonce, its scope (:class:`Scope`) and the
    environment its commands run in (the launch's, less the broker's own variables)."""

    def __init__(
        self,
        directory: Path,
        nonce: str,
        environ: Mapping[str, str],
        *,
        scope: Scope | None = None,
    ) -> None:
        self.directory = directory
        self.nonce = nonce
        self.environ = {
            key: value for key, value in environ.items() if key not in (DIR_ENV, NONCE_ENV)
        }
        self.environ[CALLER_ENV] = "1"
        self._scope = scope
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}
        self._seen: set[str] = set()
        self._stale: set[str] = set()
        self._children: set[subprocess.Popen[bytes]] = set()
        self._closing = threading.Event()

    def scope(self) -> Scope:
        """The launch's scope, taken once, from the broker's own cwd: the launch's."""
        if self._scope is None:
            self._scope = launch_scope(Path.cwd(), self.environ)
        return self._scope

    def _refuse(self, kind: str, message: str, code: int = 2) -> dict[str, object]:
        with suppress(Exception):
            record_refusal(kind, message, board=self.scope().board)
        return _refused(message, code)

    def answer(self, raw: bytes, rid: str) -> dict[str, object]:
        """What the request ``raw`` (``in/<rid>.json``) gets: its command's output and
        status, or a refusal. Each request is run once: a replayed one is refused."""
        if self._closing.is_set():
            return _refused("this agent has exited: its broker runs nothing more", 75)
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            return _refused("the broker's request did not parse")
        if not isinstance(request, dict) or request.get("v") != 1:
            return _refused("the broker's request is not one this broker reads")
        given = request.pop("mac", None)
        if (
            not isinstance(given, str)
            or not hmac.compare_digest(given, signature(self.nonce, request))
            or request.get("rid") != rid
        ):
            return self._refuse(
                "signature", "the request is not from this agent's own launch", code=77
            )
        with self._lock:
            if rid in self._seen:
                return _refused("the request was run already", code=77)
            self._seen.add(rid)
        argv = request.get("argv")
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            return _refused("the broker's request names no command")
        if any("\0" in arg for arg in argv):
            return _refused("an argument holds a NUL byte")
        why = vet(argv)
        if why is not None:
            return self._refuse(refusal_kind(argv), why)
        cwd = request.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return _refused("the command's directory is not one the broker can run in")
        try:
            scope = self.scope()
        except Exception as exc:
            return _refused(f"this agent's board could not be resolved ({exc})")
        if not scope.holds(Path(cwd)):
            return self._refuse(
                "board",
                f"{cwd} is outside this agent's tree: its broker runs commands only in its "
                "own worktree or its project's directory",
            )
        env = dict(self.environ)
        env[BOARD_ENV], env[POOL_ENV] = scope.board, scope.pool
        columns = request.get("columns")
        if isinstance(columns, int) and 20 <= columns <= 1000:
            env["COLUMNS"] = str(columns)
        return self._run(argv, cwd, env)

    def _run(self, argv: list[str], cwd: str, env: Mapping[str, str]) -> dict[str, object]:
        """``aisquare <argv>`` in ``cwd``, its output capped (:data:`MAX_OUTPUT`) and a cut
        said: an exit status of 74 when it had none, and a line on stderr (review r10,
        finding 10). Tracked, so the broker ends it when the agent exits (:meth:`close`)."""
        try:
            child = subprocess.Popen(
                selfcli.argv_for(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return _refused(f"the command could not start ({exc})", 126)
        with self._lock:
            self._children.add(child)
        out: list[bytes] = []
        err: list[bytes] = []
        cut = [False]
        readers = [
            threading.Thread(target=_capped, args=(child.stdout, out, cut), daemon=True),
            threading.Thread(target=_capped, args=(child.stderr, err, cut), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            code = child.wait(timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            return _refused(f"the command ran past {COMMAND_TIMEOUT:.0f}s and was stopped", 124)
        finally:
            for reader in readers:
                reader.join(timeout=5)
            with self._lock:
                self._children.discard(child)
        stdout = b"".join(out).decode("utf-8", errors="replace")
        stderr = b"".join(err).decode("utf-8", errors="replace")
        if cut[0]:
            stderr += (
                f"aisquare: the store broker cut this command's output at {MAX_OUTPUT} bytes; "
                "narrow it (--limit, a filter) and run it again\n"
            )
            code = code or 74
        if self._closing.is_set() and code < 0:
            stderr += "aisquare: this agent exited while the command ran; it was stopped\n"
        return {"code": code, "stdout": stdout, "stderr": stderr}

    def _handle(self, rid: str, raw: bytes | str) -> None:
        try:
            answer = _refused(raw) if isinstance(raw, str) else self.answer(raw, rid)
        except Exception as exc:  # the broker outlives any one request
            answer = _refused(f"the broker failed on this request ({type(exc).__name__}: {exc})")
        _write_answer(self.directory / OUTBOX, rid, answer)
        with self._lock:
            self._running.pop(rid, None)

    def take_requests(self, inbox_fd: int) -> None:
        """Start each request waiting in ``in/``, up to :data:`RUNNING_AT_ONCE` at once. An
        entry is taken once: whatever it was, it is removed, and one that cannot be is
        never taken again (review r10, finding 9)."""
        if self._closing.is_set():
            return
        try:
            names = sorted(entry.name for entry in os.scandir(inbox_fd))
        except OSError:
            return
        for name in names:
            rid, _, kind = name.partition(".")
            if kind != "json" or not _RID.fullmatch(rid) or name in self._stale:
                continue
            with self._lock:
                if rid in self._running or len(self._running) >= RUNNING_AT_ONCE:
                    continue
                raw = _take(inbox_fd, name)
                if raw is None:
                    continue  # withdrawn by its caller first
                if isinstance(raw, str) and not _remove(inbox_fd, name):
                    self._stale.add(name)
                worker = threading.Thread(target=self._handle, args=(rid, raw), daemon=True)
                self._running[rid] = worker
            worker.start()

    def tidy(self, inbox_fd: int) -> None:
        """Remove answers nobody read, and what in ``in/`` is no request (a part nobody
        finished, a name no request has, a directory), past :data:`ANSWER_TTL`."""
        cutoff = time.time() - ANSWER_TTL
        outbox = self.directory / OUTBOX
        with suppress(OSError):
            for entry in os.scandir(outbox):
                with suppress(OSError):
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
        with suppress(OSError):
            for entry in os.scandir(inbox_fd):
                rid, _, kind = entry.name.partition(".")
                if kind == "json" and _RID.fullmatch(rid) and entry.name not in self._stale:
                    continue
                with suppress(OSError):
                    old = entry.stat(follow_symlinks=False).st_mtime < cutoff
                    if old and _remove(inbox_fd, entry.name):
                        self._stale.discard(entry.name)

    def close(self, grace: float = 2.0) -> None:
        """The agent has exited: take nothing more, end the commands still running
        (``SIGTERM``, then ``SIGKILL`` after ``grace``), so none lands after its session
        ended and its claims were released (review r10, finding 7), and let their answers
        be written."""
        self._closing.set()
        with self._lock:
            children = list(self._children)
        for child in children:
            with suppress(OSError):
                child.terminate()
        deadline = time.monotonic() + grace
        for child in children:
            try:
                child.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    child.kill()
        deadline = time.monotonic() + 5
        while not self.idle() and time.monotonic() < deadline:
            time.sleep(0.05)

    def idle(self) -> bool:
        with self._lock:
            return not self._running


def serve(
    directory: Path,
    owner_pid: int,
    *,
    nonce: str | None = None,
    owner_alive: Callable[[], bool] | None = None,
) -> None:
    """Answer the requests in ``directory`` while the agent ``owner_pid`` runs, then end the
    commands still running and remove the directory. The layout is
    :func:`start_for_launch`'s, made before the agent's sandbox could."""
    if sys.platform == "win32":
        raise OSError("the broker runs on POSIX only")
    broker = Broker(directory, nonce or os.environ.get(NONCE_ENV, ""), os.environ)
    bell = directory / BELL
    bell_fd = os.open(bell, os.O_RDONLY | os.O_NONBLOCK)
    keep_fd = os.open(bell, os.O_WRONLY | os.O_NONBLOCK)  # no EOF while no caller rings
    inbox_fd = os.open(directory / INBOX, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    alive = owner_alive or (lambda: _alive(owner_pid))
    ready = directory / READY
    staging = directory / f".{READY}.part"
    staging.write_text(json.dumps({"pid": os.getpid(), "owner": owner_pid}), encoding="utf-8")
    staging.replace(ready)
    tidied = time.monotonic()
    try:
        while alive():
            readable, _, _ = select.select([bell_fd], [], [], _POLL)
            if readable:
                with suppress(BlockingIOError):
                    os.read(bell_fd, 4096)
            broker.take_requests(inbox_fd)
            if time.monotonic() - tidied >= _POLL * 20:
                broker.tidy(inbox_fd)
                tidied = time.monotonic()
    finally:
        broker.close()
        for fd in (bell_fd, keep_fd, inbox_fd):
            with suppress(OSError):
                os.close(fd)
        shutil.rmtree(directory, ignore_errors=True)


# --- the launch's side ------------------------------------------------------------------


def prune(root: Path) -> None:
    """Remove the directories of brokers that are gone (a broker killed before it could):
    one whose broker's pid answers no more, or that never got ready a minute on."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        ready = Path(entry.path) / READY
        try:
            pid = json.loads(ready.read_text(encoding="utf-8")).get("pid")
        except FileNotFoundError:
            pid = None
            with suppress(OSError):
                if time.time() - entry.stat(follow_symlinks=False).st_mtime < 60:
                    continue
        except (OSError, ValueError, AttributeError):
            pid = None
        if isinstance(pid, int) and pid > 0 and _alive(pid):
            continue
        shutil.rmtree(entry.path, ignore_errors=True)


def run_broker(directory: Path, env: Mapping[str, str], *, owner: int | None = None) -> None:
    """Start the broker of ``directory`` as its own process, in ``env``, for the agent
    ``owner`` (default: the one this process becomes, as ``os.execve`` keeps the pid), and
    wait until it is ready; raises when it is not."""
    owner = os.getpid() if owner is None else owner
    child = subprocess.Popen(
        selfcli.argv_for(
            ["--dir", str(directory), "--owner-pid", str(owner)],
            module="aisquare.services.store_broker",
        ),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not (directory / READY).exists() and child.poll() is None:
        if time.monotonic() >= deadline:
            child.terminate()
            with suppress(subprocess.TimeoutExpired):
                child.wait(timeout=3)
            raise RuntimeError("it did not start")
        time.sleep(0.02)
    if not (directory / READY).exists():
        raise RuntimeError(f"it exited {child.returncode}")


#: What a project tree holds that a launch outside any sandbox reads, read-only in a
#: sandboxed agent's sandbox (review r10 of #113, finding 1): Claude Code's project
#: settings, hooks and MCP servers (``.claude/``, ``.mcp.json``), Codex's project config
#: and agents (``.codex/``, ``.agents/``), aisquare's root marker (``.aisquare``), and the
#: repository (``.git``: its hooks and config run on the fleet's checkouts). ``AGENTS.md``
#: and ``CLAUDE.md`` are content an agent reads, not what runs: they stay writable.
LAUNCH_INPUTS = (".claude", ".mcp.json", ".codex", ".agents", ".aisquare", ".git")


def _tree_top(start: Path) -> Path | None:
    """The checkout ``start`` is in (a worktree's own top, never its principal's): the
    nearest directory with a ``.git``, file or directory."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def launch_paths(env: Mapping[str, str], cwd: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """What a sandboxed launch keeps read-only outside the store, and what stays writable
    within it, for ``AgentAdapter.broker_access``. Read-only: :data:`LAUNCH_INPUTS` of
    every tree the agent's launch touches (its own checkout, its project's principal
    one, its launch record's), every worktree of the project (the fleet's worktree
    directory) but its own, and the directories of the tmux sockets (review r10, finding
    4: from ``/tmp``, which every sandbox may write, the fleet's socket could be moved and
    the fleet cut off its agents). Writable: the agent's own worktree. A path is named
    whether or not it exists yet: then it cannot be made (measured on 0.162.1)."""
    from aisquare.core import launch_seal
    from aisquare.core.config import FleetSettings, load_config
    from aisquare.core.workspace import git_common_root

    try:
        fleet = load_config().fleet
    except Exception:
        fleet = FleetSettings()
    trees: list[Path] = []
    own = _tree_top(cwd)
    trees.append(own or cwd)
    with suppress(Exception):
        principal = git_common_root(cwd)
        if principal is not None:
            trees.append(principal)
    agent = env.get("AISQUARE_FLEET_AGENT", "").strip()
    if agent:
        with suppress(Exception):
            record = launch_seal.read(agent)
            if isinstance(record, launch_seal.LaunchRecord):
                trees += [Path(record.project_root), Path(record.cwd)]
    trees = list(dict.fromkeys(_resolved(tree) for tree in trees))
    protect: list[Path] = [tree / name for tree in trees for name in LAUNCH_INPUTS]
    writable: list[Path] = []
    for tree in trees:
        worktrees = _resolved(tree / fleet.worktree_dir)
        protect.append(worktrees)
        if own is not None and _resolved(own).is_relative_to(worktrees):
            writable.append(_resolved(own))
    protect += _socket_dirs(env, fleet.tmux_socket)
    return tuple(dict.fromkeys(protect)), tuple(dict.fromkeys(writable))


def _socket_dirs(env: Mapping[str, str], configured: str) -> list[Path]:
    """The directories tmux keeps sockets in for this launch: the one its window's server
    listens in (``$TMUX``), tmux's own (``$TMUX_TMPDIR`` or ``/tmp``, then
    ``tmux-<uid>``), and a configured socket path's."""
    dirs: list[Path] = []
    pane_server = env.get("TMUX", "").split(",", 1)[0].strip()
    if pane_server:
        dirs.append(Path(pane_server).parent)
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        dirs.append(Path(env.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{getuid()}")
    if "/" in configured:
        dirs.append(Path(configured).expanduser().parent)
    return [_resolved(directory) for directory in dirs]


def start_for_launch(
    env: dict[str, str], adapter: AgentAdapter, argv: list[str]
) -> tuple[list[str], str]:
    """``argv`` with this launch's broker inbox the one path in the store its sandbox may
    write (``AgentAdapter.broker_access``), and the broker started; with a line to print
    when it could not be (the launch goes on: its board calls then say what failed).
    ``argv`` as it is, with nothing to say, for an agent that runs its tools unsandboxed
    or under a sandbox of the operator's own (no store profile in ``argv``)."""
    from aisquare.core.agent_adapters.types import store_dir

    if not adapter.capabilities.sandboxes_tools:
        return argv, ""
    store = store_dir()
    launch = env.get("AISQUARE_LAUNCH_ID", "").strip()
    directory = launch_dir(store, launch or uuid.uuid4().hex)
    protect, writable = launch_paths(env, Path.cwd())
    access = adapter.broker_access(
        argv, store, directory / INBOX, protect=protect, writable=writable
    )
    if access is None:
        return argv, ""
    if sys.platform == "win32":
        return access, (
            f"{adapter.label}: the aisquare store is read-only in its sandbox, and its "
            "board calls go through a broker AISquare runs on POSIX only (or WSL)"
        )
    import secrets

    prune(store / DIR_NAME)
    nonce = secrets.token_urlsafe(32)
    try:
        directory.mkdir(parents=True, mode=0o700)
        (directory / INBOX).mkdir(mode=0o700)
        (directory / OUTBOX).mkdir(mode=0o700)
        os.mkfifo(directory / BELL, 0o600)
        broker_env = {key: value for key, value in env.items() if key != DIR_ENV}
        broker_env[NONCE_ENV] = nonce
        run_broker(directory, broker_env)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        return access, (
            f"{adapter.label}: the aisquare store is read-only in its sandbox and its broker "
            f"did not start ({exc}), so its board calls will fail there until a restart"
        )
    env[DIR_ENV] = str(directory)
    env[NONCE_ENV] = nonce
    return access, ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    options = parser.parse_args()
    serve(options.dir, options.owner_pid)
