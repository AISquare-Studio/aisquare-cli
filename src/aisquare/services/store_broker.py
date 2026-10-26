"""The broker a sandboxed agent's board calls go through.

An agent that sandboxes its tools (Codex, ``capabilities.sandboxes_tools``) is trusted to
the extent of the tree it may write, as a Claude Code agent and a human contributor are
(``core.launch_seal``'s boundary). The aisquare store is read-only in its sandbox under
the store's profile (``AgentAdapter.store_access``; a native ``--sandbox`` replaces it),
all of it but its launch's inbox here, and with it the launch inputs at the tops of its
trees, ``.git``, the other agents' worktrees and the tmux sockets (:func:`launch_paths`);
whatever its sandbox, a launch is refused where its sandbox may write an aisquare its
hooks or this broker run (:func:`refuse_inside`, from ``AgentAdapter.sandbox_writes``); the
Python they and this broker start ignores ``PYTHONPATH`` and ``PYTHONHOME`` (``-E``), which
the agent keeps for its own tools; a ``PATH`` entry it may write is the user's to keep out,
and warned of (:func:`sandbox_environment`).
Under the profile, nothing a process there writes chooses a replay, a kill, a typed line,
a launch, a model or account choice, a worktree removal or a paste the fleet makes. The
board calls its role protocol makes still work there, unchanged (``aisquare note …``,
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
saying why. Every refusal it makes, and every scope refusal of a command it runs, is
logged (:func:`record_refusal`); those of the kinds in :data:`NOTED` (another session,
another board or pool, a fleet operation, a request its launch did not sign) are put on
its board too, once per launch and kind. The command it runs acts only as the
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

``aisquare exec`` runs a command and saves a report of it; the broker runs outside the
sandbox, so it never runs one (:data:`LOCAL`: the CLI runs it in the sandbox, where its
child is as sandboxed as the agent's own commands). Its report goes to the launch's own
``reports/`` (:func:`launch_dir`), the second path in the store that launch's sandbox may
write, and ``reports seal`` (the one report command on the whitelist) has the broker record
what the report held when the run ended, where the sandbox cannot write
(``seals/``). ``brief evidence --report`` then adopts the report into the home's
``reports/`` (``services.command_reports.adopt_sandboxed``): only a sealed, unchanged,
owner-only report of this launch's tree and board, copied from the bytes that were checked.
"""

from __future__ import annotations

import argparse
import errno
import hmac
import json
import logging
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

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from aisquare.core.agent_adapters.types import AgentAdapter, SandboxWrites

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
REPORTS_ENV = "AISQUARE_BROKERED_REPORTS"
"""The launch's ``reports/`` (:data:`REPORTS`), in the environment of the command a broker
runs: where a sandboxed ``asq exec`` saved its reports, which the command adopts or seals
(``services.command_reports``)."""
SEALS_ENV = "AISQUARE_BROKERED_SEALS"
"""The launch's ``seals/`` (:data:`SEALS`), in the environment of the command a broker runs."""
ROOTS_ENV = "AISQUARE_BROKERED_ROOTS"
"""The launch's tree (its directories, ``os.pathsep``-joined), in the environment of the
command a broker runs: the command refuses to run anywhere else (:func:`check_cwd`)."""
LOG_NAME = "store-broker.log"
"""The brokers' refusals, one line each, in the aisquare home's log directory."""
#: The refusals put on the launch's board as well as logged, once per launch and kind: a
#: session not its own, another board's task, another project's pool, a fleet operation
#: (its signals included), a request not signed by its launch.
NOTED = frozenset({"identity", "board", "pool", "fleet", "signature"})
#: The commands that only read: one asked from outside the launch's tree is refused and
#: logged, never put on the board (``cd /tmp && aisquare board`` is no misbehaviour).
READS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("board",),
        ("recall",),
        ("log",),
        ("inject",),
        ("task", "list"),
        ("task", "show"),
        ("team", "status"),
        ("team", "log"),
        ("team", "verify"),
        ("team", "signals"),
        ("context", "list"),
        ("context", "show"),
        ("context", "search"),
        ("context", "preview"),
        ("code", "find"),
        ("code", "refs"),
        ("code", "similar"),
        ("brief", "list"),
        ("brief", "show"),
        ("brief", "check"),
    }
)
INBOX, OUTBOX, BELL, READY = "in", "out", "bell", "ready.json"
REPORTS, SEALS = "reports", "seals"
"""A launch's directories beside :data:`INBOX`: ``reports/``, which its sandbox may write (the
reports a sandboxed ``asq exec`` saves), and ``seals/``, which it may not (what the broker
saw of each report when the run ended)."""
LOCAL = frozenset({"exec", "reports"})
"""The commands the CLI runs in the sandbox itself instead of handing them to the broker:
``exec`` runs the agent's own command, which the broker, outside the sandbox, must never
run for it; ``reports`` reads what ``exec`` saved. Neither writes the store."""
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
    ("code", "find"): "find where a name is defined",
    ("code", "refs"): "list the uses of a name",
    ("code", "similar"): "find helpers like a name",
    ("brief", "create"): "create a work brief",
    ("brief", "list"): "list the work briefs",
    ("brief", "show"): "show a work brief",
    ("brief", "update"): "correct a work brief",
    ("brief", "link"): "link a task to requirements",
    ("brief", "evidence"): "record a check, backed by a report or a file",
    ("brief", "check"): "check a work brief against the source",
    ("brief", "finding"): "record a failure found in a task's work",
    ("reports", "seal"): "seal the report an `asq exec` saved here (for `brief evidence`)",
}
"""Every command a sandboxed agent's broker runs, by its path: what the role protocols
(``core.harness``) and the memory use, the MCP server's tools' set
(``services.mcp_server``), the symbol index's reads (``code find|refs|similar``: ``code
index`` writes the index, so it stays out) and the work briefs (``brief …``: store-only, the
paths they name held to the launch's tree, :func:`outside_tree`). Never a fleet operation
(spawn, stop, restart, switch, tell, shutdown …), a launch, a hook, a binding, the config
or an account: those choose or run something outside the sandbox. Never ``exec``, which
would run the agent's argv unsandboxed (:data:`LOCAL`)."""
#: The CLI's command groups, whose commands are two words, by every name they go by.
_GROUPS = {
    **{
        name: name
        for name in (
            "accounts",
            "agents",
            "auth",
            "brief",
            "code",
            "config",
            "context",
            "explainability",
            "fleet",
            "hook",
            "project",
            "reports",
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


class LaunchRefused(RuntimeError):
    """A sandboxed launch that must not start: the aisquare its hooks and broker run,
    outside the sandbox, lives where the sandbox may write (:func:`install_inside`)."""


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


def check_cwd(environ: Mapping[str, str] | None = None) -> str | None:
    """Why a command a broker runs may not run where it is, or ``None``: its cwd, as the
    kernel has it now (``os.getcwd``), must be in the launch's tree (:data:`ROOTS_ENV`).
    The broker checks the request's directory, then starts the command there; a
    directory the sandbox swapped for a link in between is caught here (review r11 of
    #113, finding 11)."""
    source = os.environ if environ is None else environ
    if not brokered_call(source) or not source.get(ROOTS_ENV):
        return None
    roots = [Path(root) for root in source[ROOTS_ENV].split(os.pathsep) if root]
    try:
        here = Path(os.getcwd())
    except OSError as exc:
        return f"its directory is gone ({exc.strerror})"
    if any(here == root or here.is_relative_to(root) for root in roots):
        return None
    return f"{here} is outside this agent's tree: its broker runs commands only there"


def outside_tree(path: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Why a command a broker runs may not read ``path`` (an artifact, a checkout), or
    ``None``: it must be in the launch's tree (:data:`ROOTS_ENV`), as the command's own
    directory must (:func:`check_cwd`). The broker reads outside the sandbox, so a path the
    agent names is read as the user: ``/etc``, ``~/.ssh`` and another project's files would
    be read, hashed and (an artifact) kept in the store, which the sandbox reads. ``None``
    for any command that is not brokered."""
    source = os.environ if environ is None else environ
    if not brokered_call(source) or not source.get(ROOTS_ENV):
        return None
    roots = [Path(root) for root in source[ROOTS_ENV].split(os.pathsep) if root]
    there = _resolved(path.expanduser())
    if any(there == root or there.is_relative_to(root) for root in roots):
        return None
    return f"{path} is outside this agent's tree: its broker reads only there"


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
    if kind not in NOTED or board is None or launch == "?":
        return  # no launch to name (a child of a brokered command's): logged only
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


_WRITABLE: dict[tuple[int, str], bool] = {}
""":func:`_writable`'s answer, once per process and home: every hook a Codex agent fires
names its broker (review r11 of #113, finding 14)."""


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
    if not home.is_dir():
        return None
    key = (os.getpid(), str(home))
    if key not in _WRITABLE:
        _WRITABLE[key] = _writable(home)
    return None if _WRITABLE[key] else Path(where)


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
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            # A full disk is no sandbox: cannot tell, so this process is not sandboxed, and
            # a hook outside one runs as it always did (review r11 of #113, finding 14).
            _log.warning("aisquare home %s is full (%s): taken as unsandboxed", home, exc)
            return True
        return False
    os.close(fd)
    with suppress(OSError):
        os.unlink(name)
    return True


def forward(argv: Sequence[str]) -> int | None:
    """Run ``argv`` through the launch's broker when this process is sandboxed
    (:func:`brokered`), printing what the command printed; its exit status, or ``None``
    when the CLI runs it itself. ``--version`` reads nothing and runs here, so do the
    commands of :data:`LOCAL` (``exec`` and ``reports``), and a hook
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
    if first in LOCAL:
        return None
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
    """The launch's own board, pool and tree, from the launch's own directory, never a
    request's: its board as every board call resolves one (``orchestrator.team_project``,
    under the launch's ``AISQUARE_TEAM_HUB``: a fleet record's project is spawn's, which is
    not the hub's, and every call under a hub was refused: review r11 of #113, finding 1);
    its project's pool (``workspace.current_project``, never a ``project switch`` pin); and
    its tree: its checkout and its project's principal one, a fleet agent's launch record's
    directories too (``core.launch_seal``, read-only to the sandbox), so a hand launch in a
    subdirectory may still run a call from its project's directory (finding 8)."""
    from aisquare.core import launch_seal
    from aisquare.core.orchestrator import team_project
    from aisquare.core.workspace import current_project, find_project_root, git_common_root

    roots: list[Path] = [cwd]
    top = _tree_top(cwd)
    if top is not None:
        roots.append(top)
    with suppress(Exception):
        principal = git_common_root(cwd)
        if principal is not None:
            roots.append(principal)
    with suppress(Exception):
        roots.append(find_project_root(cwd))  # a non-git project's marker (r12, finding 13)
    agent = environ.get("AISQUARE_FLEET_AGENT", "").strip()
    if agent:
        record = launch_seal.read(agent)
        if isinstance(record, launch_seal.LaunchRecord):
            roots += [Path(record.project_root), Path(record.cwd)]
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
            key: value
            for key, value in environ.items()
            if key not in (DIR_ENV, NONCE_ENV, REPORTS_ENV, SEALS_ENV)
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
        """A refusal, recorded: its line whatever else fails, its note when its kind is one
        (:data:`NOTED`; review r12 of #113, finding 11)."""
        board: str | None = None
        with suppress(Exception):
            board = self.scope().board
        with suppress(Exception):
            record_refusal(kind, message, board=board)
        return _refused(message, code)

    @staticmethod
    def _exited() -> dict[str, object]:
        """The refusal of a request that comes once the agent has exited, logged (kind
        ``closed``): no scope is resolved for it, so it never waits on the store."""
        message = "this agent has exited: its broker runs nothing more"
        with suppress(Exception):
            record_refusal("closed", message)
        return _refused(message, 75)

    def answer(self, raw: bytes, rid: str) -> dict[str, object]:
        """What the request ``raw`` (``in/<rid>.json``) gets: its command's output and
        status, or a refusal. Each request is run once: a replayed one is refused."""
        if self._closing.is_set():
            return self._exited()
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            return self._refuse("request", "the broker's request did not parse")
        if not isinstance(request, dict) or request.get("v") != 1:
            return self._refuse("request", "the broker's request is not one this broker reads")
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
                return self._refuse("replay", "the request was run already", code=77)
            self._seen.add(rid)
        argv = request.get("argv")
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            return self._refuse("request", "the broker's request names no command")
        if any("\0" in arg for arg in argv):
            return self._refuse("request", "an argument holds a NUL byte")
        why = vet(argv)
        if why is not None:
            return self._refuse(refusal_kind(argv), why)
        cwd = request.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return self._refuse(
                "request", "the command's directory is not one the broker can run in"
            )
        try:
            scope = self.scope()
        except Exception as exc:
            return self._refuse("request", f"this agent's board could not be resolved ({exc})")
        if not scope.holds(Path(cwd)):
            named = command_path(argv)
            reads = not isinstance(named, str) and named[0] in READS
            return self._refuse(
                "read" if reads else "board",
                f"{cwd} is outside this agent's tree: its broker runs commands only in its "
                "own checkout or its project's directory",
            )
        env = dict(self.environ)
        env[BOARD_ENV], env[POOL_ENV] = scope.board, scope.pool
        env[ROOTS_ENV] = os.pathsep.join(str(root) for root in scope.roots)
        env[REPORTS_ENV] = str(self.directory / REPORTS)
        env[SEALS_ENV] = str(self.directory / SEALS)
        columns = request.get("columns")
        if isinstance(columns, int) and 20 <= columns <= 1000:
            env["COLUMNS"] = str(columns)
        # The directory checked, never the request's spelling of it, and checked again by
        # the command itself where it runs (:func:`check_cwd`; review r11, finding 11).
        return self._run(argv, str(_resolved(Path(cwd))), env)

    def _run(self, argv: list[str], cwd: str, env: Mapping[str, str]) -> dict[str, object]:
        """``aisquare <argv>`` in ``cwd``, its output capped (:data:`MAX_OUTPUT`) and a cut
        said: an exit status of 74 when it had none, and a line on stderr (review r10,
        finding 10). Tracked, so the broker ends it when the agent exits (:meth:`close`):
        started and registered under the lock :meth:`close` takes its list under, after a
        last look at whether the agent has exited (review r11, finding 9)."""
        with self._lock:
            if self._closing.is_set():
                return self._exited()
            try:
                child = subprocess.Popen(
                    selfcli.isolated_argv_for(argv),
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                return _refused(f"the command could not start ({exc})", 126)
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
            answer = self._refuse("request", raw) if isinstance(raw, str) else self.answer(raw, rid)
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
        with self._lock:
            self._closing.set()
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
        selfcli.isolated_argv_for(
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


#: What a project tree holds that aisquare's own launches read whatever the agent:
#: aisquare's root marker (``.aisquare``) and the repository (``.git``: its hooks and
#: config run on the fleet's checkouts). Each agent's own are its adapter's
#: (``AgentCapabilities.launch_inputs``), joined in :func:`launch_inputs`.
OWN_LAUNCH_INPUTS = (".aisquare", ".git")


def launch_inputs() -> tuple[str, ...]:
    """What a project tree holds that a launch outside any sandbox reads, read-only in a
    sandboxed agent's sandbox (review r10 of #113, finding 1): every registered agent's
    own (``AgentCapabilities.launch_inputs``: Claude Code's project settings, hooks and MCP
    servers, ``.claude/`` and ``.mcp.json``; Codex's project config and agents, ``.codex/``
    and ``.agents/``), each agent's whichever is sandboxed, as a sandboxed Codex in a
    project could otherwise plant what another agent's next start there runs (GATE #113),
    and :data:`OWN_LAUNCH_INPUTS`. ``AGENTS.md`` and ``CLAUDE.md`` are content an agent
    reads, not what runs: they stay writable."""
    from aisquare.core.agent_adapters import adapters

    names: list[str] = []
    for adapter in adapters():
        names += adapter.capabilities.launch_inputs
    return tuple(dict.fromkeys([*names, *OWN_LAUNCH_INPUTS]))


def _tree_top(start: Path) -> Path | None:
    """The checkout ``start`` is in (a worktree's own top, never its principal's): the
    nearest directory with a ``.git``, file or directory."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def launch_paths(
    env: Mapping[str, str], cwd: Path, given: Sequence[Path] = ()
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """What a sandboxed launch keeps read-only outside the store, and what stays writable
    within it, for ``AgentAdapter.broker_access``. Read-only: :func:`launch_inputs` of
    every tree the agent's launch touches (its own checkout, its project's principal
    one, its launch record's), every worktree of the project (the fleet's worktree
    directory) but its own, and the directories of the tmux sockets (review r10, finding
    4: from ``/tmp``, which every sandbox may write, the fleet's socket could be moved and
    the fleet cut off its agents). Writable: the agent's own worktree. A path is named
    whether or not it exists yet: then it cannot be made (measured on 0.162.1)."""
    from aisquare.core import launch_seal
    from aisquare.core.config import FleetSettings, load_config
    from aisquare.core.workspace import find_project_root, git_common_root

    try:
        fleet = load_config().fleet
    except Exception:
        fleet = FleetSettings()
    trees: list[Path] = [cwd, *given]
    own = _tree_top(cwd)
    if own is not None:
        trees.append(own)
    with suppress(Exception):
        trees.append(find_project_root(cwd))
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
    inputs = launch_inputs()
    protect: list[Path] = [tree / name for tree in trees for name in inputs]
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
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        dirs.append(Path(env.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{getuid()}")
    pane_server = env.get("TMUX", "").split(",", 1)[0].strip()
    if pane_server:
        dirs.append(Path(pane_server).parent)
    if "/" in configured:
        # `~olduser` names no home here: read as written (review r15 of #113, finding 13),
        # never a RuntimeError, which a sandboxed launch now refuses on.
        dirs.append(paths.expand_user(Path(configured)).parent)
    # A socket made straight in a shared directory (`tmux -S /tmp/x`) names that directory:
    # held read-only, every write of the session there would fail (review r11, finding 6).
    shared = {
        _resolved(Path(place))
        for place in ("/", "/tmp", "/var/tmp", env.get("TMPDIR") or "/tmp", Path.home())
    }
    kept = [d for d in dict.fromkeys(_resolved(directory) for directory in dirs) if d not in shared]
    if pane_server and _resolved(Path(pane_server).parent) in shared:
        # Its directory cannot be held, so the socket itself is: measured on 0.162.1, a
        # read-only socket file in a writable /tmp refuses `mv` and `rm` (EBUSY) (review r12
        # of #113, finding 4).
        kept.append(_resolved(Path(pane_server)))
    return kept


#: What holds a Python environment's code under its prefix (a virtualenv, an installation,
#: a ``bin/``'s parent): its scripts and interpreter, its standard library and
#: site-packages (whose ``.pth`` files run at every start), Windows' ``Lib`` and ``DLLs``, a
#: macOS framework build's ``Resources`` (the interpreter's app) and a virtualenv's
#: ``pyvenv.cfg`` (its base, and whether it loads the system's site). Not the prefix: a
#: Python built with ``--prefix=$HOME``, or an ``aisquare`` in ``~/bin``, has the home for
#: one, which holds every project under it (GATE #113 at 303042b0, finding 1).
ENVIRONMENT_PARTS = ("bin", "Scripts", "lib", "lib64", "Lib", "DLLs", "Resources", "pyvenv.cfg")


def _environment(prefix: Path) -> list[Path]:
    """The parts of the Python environment at ``prefix`` that hold its code
    (:data:`ENVIRONMENT_PARTS`)."""
    return [prefix / part for part in ENVIRONMENT_PARTS]


def _with_environment(program: Path) -> list[Path]:
    """``program`` and the environment it starts in (a ``bin/`` or ``Scripts/`` parent's
    parent, by the parts that hold its code: its ``.pth`` files, its packages), as named
    and as resolved: a venv's ``python`` is a link to the base interpreter, and only the
    name it is reached by is in the venv (review r13 of #113, finding 8)."""
    found: list[Path] = []
    for named in dict.fromkeys((program.absolute(), _resolved(program))):
        found.append(named)
        if named.parent.name in ("bin", "Scripts"):
            found += _environment(named.parent.parent)
    return found


def install_paths(
    config_dir: Path | None = None,
    agent: str | None = None,
    path: str | None = None,
    args: Sequence[str] = (),
) -> tuple[Path, ...]:
    """Where the aisquare that runs outside an agent's sandbox on its behalf lives: this one,
    which the broker runs (``sys.executable -P -m aisquare``: the interpreter and the
    installation it comes from, its environment and the user site it loads, whose ``.pth``
    files run at every start, the package's own directory, an editable install's source
    tree), and what each of the agent's aisquare hooks runs, with its environment: the
    program its hooks' config in ``config_dir`` names (hooks connected from another install
    keep running that one: review r12 of #113, finding 1), and, where the agent's plugin is
    installed there, trusted, and runs (the hooks file runs aisquare for none, or not every,
    event, which it stands down for), what the plugin's launcher runs: the ``aisquare`` on
    ``path`` (the launch's ``PATH``) and ``~/.local/bin``'s, or, with neither, the pinned
    release through ``uvx``, from uv's cache (r13, finding 3; r14, finding 2: counted only
    where it would run). An environment counts by the parts that hold its code
    (:func:`_environment`) and the site-packages its interpreter loads, wherever they are
    (Debian's ``/usr/local/lib/python3/dist-packages``), never by its prefix (GATE #113 at
    303042b0, finding 1). ``PYTHONPATH`` is not here: the interpreters aisquare starts for
    a sandboxed agent ignore it (``selfcli.isolated_argv_for``)."""
    import site

    import aisquare
    from aisquare.core import agents

    found = [
        *_environment(Path(sys.prefix)),
        *_environment(Path(sys.base_prefix)),
        *_with_environment(Path(sys.executable)),
        Path(aisquare.__file__).parent,
    ]
    with suppress(Exception):
        found += [Path(place) for place in site.getsitepackages()]
    with suppress(Exception):
        found.append(Path(agents.current_install()))
    if agent is not None:
        with suppress(Exception):
            for command in agents.hook_commands(agent, config_dir):
                hook = agents.hook_binary(command)
                if hook is not None:
                    found += _with_environment(hook.program)
        with suppress(Exception):
            found += _plugin_launches(agent, config_dir, path, args)
    with suppress(Exception):
        if _loads_user_site():
            found.append(Path(site.getusersitepackages()))
    return tuple(dict.fromkeys(_resolved(path) for path in found))


def _loads_user_site() -> bool:
    """Whether an interpreter aisquare starts with ``-E`` loads the user site, ``.pth`` files
    and all: ``-E`` ignores ``PYTHONNOUSERSITE`` too, so whatever this process's
    ``site.ENABLE_USER_SITE`` says, only a venv that keeps the system's site out (its
    ``pyvenv.cfg``) stops it (review r15 of #113, finding 8)."""
    if sys.prefix == sys.base_prefix:
        return True
    try:
        config = (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return True
    for line in config.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "include-system-site-packages":
            return value.strip().lower() == "true"
    return False


def _xdg(variable: str, fallback: Path) -> Path:
    """``$<variable>`` where it is absolute, as uv and the plugin's launcher read it (a
    relative one the XDG spec says to ignore), else ``fallback``."""
    value = os.environ.get(variable, "")
    return Path(value) if value.startswith("/") else fallback


def _cache_home() -> Path:
    """``$XDG_CACHE_HOME``, else ``~/.cache``."""
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache")


def _uv_setting(key: str) -> Path | None:
    """``key`` in uv's own config (``uv.toml``: the user's, ``$XDG_CONFIG_HOME/uv``, then the
    system's, ``/etc/uv``), as ``uvx`` reads it; ``None`` where neither sets it."""
    import tomllib

    for config in (
        _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "uv" / "uv.toml",
        Path("/etc/uv/uv.toml"),
    ):
        try:
            with config.open("rb") as handle:
                value = tomllib.load(handle).get(key)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(value, str) and value:
            return paths.expand_user(Path(value))
    return None


def _plugin_launches(
    agent: str, config_dir: Path | None, path: str | None, args: Sequence[str] = ()
) -> list[Path]:
    """What the agent's plugin launcher (``plugins/*/scripts/aisquare-hook``) runs, with
    each one's environment, where it runs at all: the plugin installed for ``config_dir``
    and its hooks trusted, or run regardless (``--dangerously-bypass-hook-trust`` in the
    launch's ``args``: Codex runs an untrusted plugin's hooks then; review r15 of #113,
    finding 2), and not standing down beside a hook of ours in the hooks file whose
    program can start (``agents.plugin_stands_down``, the launcher's own test). Then what
    the launcher's ``_find`` finds, on ``path`` then in ``~/.local/bin`` and
    ``~/.cargo/bin``, passing over one that cannot start (``agents.launcher_finds``;
    r15, finding 3): its ``aisquare``, or, with none, its ``uvx`` and what that runs: uv's
    cache (``$UV_CACHE_DIR``, else uv.toml's ``cache-dir``, else ``$XDG_CACHE_HOME/uv``),
    the uv-managed Python the cached environment links to (``$UV_PYTHON_INSTALL_DIR``,
    else uv.toml's ``python-install-dir``, else ``$XDG_DATA_HOME/uv/python``) and the
    launcher's answers (``$XDG_CACHE_HOME/aisquare``) (r15, finding 9)."""
    from aisquare.core import agents

    plugin = agents.plugin_of(agent)
    install = plugin.installed(config_dir) if plugin is not None else None
    if install is None:
        return []
    if not (install.trusted or "--dangerously-bypass-hook-trust" in args):
        return []
    if agents.plugin_stands_down(agent, config_dir):
        return []
    runner = agents.launcher_finds("aisquare", path)
    if runner is not None:
        return _with_environment(runner)
    uvx = agents.launcher_finds("uvx", path)
    if uvx is None:
        return []  # it runs nothing, and says so once
    cache = os.environ.get("UV_CACHE_DIR")
    pythons = os.environ.get("UV_PYTHON_INSTALL_DIR")
    data = _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return [
        *_with_environment(uvx),
        Path(cache) if cache else _uv_setting("cache-dir") or _cache_home() / "uv",
        Path(pythons) if pythons else _uv_setting("python-install-dir") or data / "uv" / "python",
        _cache_home() / "aisquare",
    ]


def _resolved_writes(writes: SandboxWrites) -> SandboxWrites:
    """``writes`` as the kernel resolves its paths (``/tmp`` is ``/private/tmp`` on macOS)."""
    from aisquare.core.agent_adapters.types import SandboxWrites as Writes

    return Writes(
        tuple(_resolved(path) for path in writes.writable),
        tuple(_resolved(path) for path in writes.read_only),
        _resolved(writes.workspace) if writes.workspace is not None else None,
        tuple(_resolved(path) for path in writes.temporary),
    )


def install_inside(writes: SandboxWrites, installs: Sequence[Path]) -> Path | None:
    """Where ``writes`` lets a sandbox write an aisquare of ``installs``, or ``None``:
    compared as the kernel resolves them, in either direction. An install under a root the
    sandbox may write (the install, named); or a root the sandbox may write under an
    install (that root, named: ``--add-dir <venv>/lib``, a ``writable_roots`` entry in
    site-packages or the package's own directory, ``-C`` into it, a profile's ``write``
    path), where it writes a ``.pth``, which runs at every start of that interpreter,
    ``-E`` or not, or a module of the package (SMOKE6: one direction was asked). An
    environment is in ``installs`` by its parts, not its prefix (:func:`install_paths`), so
    a project under a prefix of ``$HOME`` is no root inside an install."""
    real = _resolved_writes(writes)
    for install in (_resolved(path) for path in installs):
        if real.may_write(install):
            return install
        for root in real.writable:
            if root.is_relative_to(install) and real.may_write(root):
                return root
    return None


def _remedy(inside: Path, writes: SandboxWrites) -> str:
    """What to do about an aisquare at ``inside`` that ``writes`` allows writing, by the
    root that allows it (review r13 of #113, finding 10): a temporary directory, move it
    out; a directory given the sandbox (``--add-dir``, ``writable_roots``, a profile's
    ``write`` path), drop it or move the install; the workspace, inside the project,
    install it elsewhere, above the project (a launch from the home), launch from the
    project (r12, findings 6 and 14). The distribution is ``aisquare-cli``."""
    from aisquare.core.workspace import project_tree, real_project_root

    real = _resolved_writes(writes)
    root = real.root_of(_resolved(inside))
    elsewhere = "(`uv tool install aisquare-cli`, or `pipx install aisquare-cli`)"
    if root is not None and root in real.temporary:
        return (
            f"install aisquare outside {root}, which every such sandbox may write "
            f"{elsewhere}, connect the agent's hooks with that install, then launch again"
        )
    if root is not None and root != real.workspace:
        return (
            f"drop {root} from what the launch gives its sandbox to write (`--add-dir`, "
            f"`writable_roots`, a profile's `write` path), or install aisquare outside it "
            f"{elsewhere}"
        )
    workspace = real.workspace or root
    with suppress(Exception):
        # The checkout the launch is in: a linked worktree's own, outside its repository
        # too, whose principal is named beside it (review r14 of #113, finding 8).
        tree = project_tree(workspace) if workspace is not None else None
        if tree is not None and _resolved(inside).is_relative_to(tree):
            principal = real_project_root(tree)
            which = (
                f"the project's checkout ({tree}, a worktree of {principal})"
                if principal is not None and principal != tree
                else "the project"
            )
            return (
                f"install aisquare outside {which} {elsewhere}, connect the agent's "
                "hooks with that install (its own `aisquare agents connect codex`), "
                "then launch again"
            )
    return (
        f"launch the agent from its project's directory, not {workspace}, whose tree holds "
        f"that aisquare, or install aisquare outside every directory the agent may write "
        f"{elsewhere}"
    )


def _sentence(text: str) -> str:
    """``text`` with its first letter capital and nothing else changed: a path in it is
    the path it names (``str.capitalize`` lowered the rest; review r13 of #113, 9)."""
    return text[:1].upper() + text[1:]


def refuse_inside(
    argv: Sequence[str],
    cwd: Path,
    adapter: AgentAdapter,
    config_dir: Path | None = None,
    path: str | None = None,
) -> None:
    """Refuse the launch of ``adapter``'s agent with ``argv`` in ``cwd`` when its sandbox may
    write an aisquare its hooks or broker run outside it (:func:`install_paths`, ``path``
    the launch's ``PATH``): the next hook or brokered call would run what it wrote (review
    r11 of #113, finding 2). Decided from what that sandbox may write
    (``AgentAdapter.sandbox_writes``: the store's profile or a native sandbox, from the
    agent's config in ``config_dir``, its profile and ``argv``; its workspace and added
    directories; a read-only seat writing none), never from whether the store's profile is
    there (r12, findings 3 and 7, R1; r13, finding 6); an unsandboxed agent is trusted as
    its user is. :class:`LaunchRefused`."""
    writes = adapter.sandbox_writes(list(argv)[1:], cwd, config_dir)
    if writes is None:
        return
    inside = install_inside(writes, install_paths(config_dir, adapter.id, path, list(argv)[1:]))
    if inside is None:
        return
    raise LaunchRefused(
        f"{adapter.label} runs its tools in a sandbox that may write {inside}, which is (or "
        f"is in) an aisquare its hooks or its store broker run outside that sandbox: what it "
        f"wrote there would run unsandboxed. {_sentence(_remedy(inside, writes))}"
    )


#: The variables naming where a Python imports from first, before the standard library.
PYTHON_PATHS = ("PYTHONPATH", "PYTHONHOME")


def _writable_entries(value: str, writes: SandboxWrites, cwd: Path) -> list[str]:
    """The entries of the path list ``value`` that ``writes`` allows writing, as a process
    in ``cwd`` reads them: an empty entry is ``cwd`` itself, a relative one is under it
    (review r14 of #113, finding 3); neither ``PATH`` nor ``PYTHONPATH`` expands ``~``.
    An unset or empty variable names nothing."""
    if not value:
        return []
    real = _resolved_writes(writes)
    return [
        entry or "(an empty entry: the directory it runs in)"
        for entry in value.split(os.pathsep)
        if real.may_write(_resolved(cwd / entry if entry else cwd))
    ]


def _hooks_isolated(adapter: AgentAdapter, config_dir: Path | None) -> bool:
    """Whether every aisquare hook ``adapter``'s agent runs from its hooks file in
    ``config_dir`` runs with ``-E`` (``agents.install_hooks`` writes it so for an agent that
    sandboxes its tools, from review r14 of #113): an older connect wrote the console
    script, whose interpreter reads ``PYTHONPATH``. The plugin's launcher unsets it."""
    from aisquare.core import agents

    with suppress(Exception):
        return all(
            agents.hook_runs_isolated(c) for c in agents.hook_commands(adapter.id, config_dir)
        )
    return False


def sandbox_environment(
    env: Mapping[str, str], writes: SandboxWrites | None, cwd: Path, hooks_isolated: bool
) -> list[str]:
    """What a launch says of its environment, which the agent keeps whole, its tools run
    in, and its hooks and broker inherit outside its sandbox (review r13 of #113, finding
    1; r14, findings 1 and 3). Nothing is changed in it: the agent's own tools need its
    ``PYTHONPATH`` (direnv's ``PYTHONPATH=$PWD/src``), and the interpreters aisquare
    starts for it, its hooks as this version connects them included, ignore ``PYTHONPATH``
    and ``PYTHONHOME`` (``-E``: ``selfcli.isolated_argv_for``). Warned of, in ``cwd``'s
    terms (an empty entry is the directory a process runs in): a ``PATH`` entry the
    sandbox may write, as Codex itself, and aisquare's hooks and broker, run the ``git``
    (and ``gbrain``, ``tmux``) found there outside the sandbox; and, where the agent's
    hooks were connected by an older aisquare and so read them (``hooks_isolated``
    false), a ``PYTHONPATH`` or ``PYTHONHOME`` entry the sandbox may write. Nothing for an
    unsandboxed agent."""
    if writes is None:
        return []
    said: list[str] = []
    named = _writable_entries(env.get("PATH", ""), writes, cwd)
    if named:
        said.append(
            f"PATH names {', '.join(named)}, which this agent's sandbox may write: Codex "
            "itself, and aisquare's hooks and store broker, run the git (and gbrain, tmux) "
            "found there outside the sandbox. Launch from a shell whose PATH names none of "
            "them (deactivate the project's virtualenv)"
        )
    if not hooks_isolated:
        for key in PYTHON_PATHS:
            entries = _writable_entries(env.get(key, ""), writes, cwd)
            if entries:
                said.append(
                    f"{key} names {', '.join(entries)}, which this agent's sandbox may "
                    "write, and its hooks, connected by an older aisquare, would import "
                    "from there outside the sandbox: connect them again "
                    "(`aisquare agents connect codex`), which runs them with -E"
                )
    return said


def start_for_launch(
    env: dict[str, str], adapter: AgentAdapter, argv: list[str]
) -> tuple[list[str], str]:
    """``argv`` with this launch's broker inbox the one path in the store its sandbox may
    write (``AgentAdapter.broker_access``), and the broker started; with a line to print
    when it could not be (the launch goes on: its board calls then say what failed), and
    what :func:`sandbox_environment` says of ``env``. ``argv`` as it is for an agent that
    runs its tools unsandboxed or under a sandbox of the operator's own (no store profile
    in ``argv``)."""
    from aisquare.core.agent_adapters.types import store_dir

    if not adapter.capabilities.sandboxes_tools:
        return argv, ""
    from aisquare.core import agents

    store = store_dir()
    launch = env.get("AISQUARE_LAUNCH_ID", "").strip()
    directory = launch_dir(store, launch or uuid.uuid4().hex)
    cwd = Path.cwd()
    home = adapter.config_dir(agents._home(), env)
    # The trees Codex is given beyond the launch's directory (`-C`, `--add-dir`) hold
    # launch inputs too (review r12 of #113, finding 7).
    given = adapter.sandbox_writes(argv[1:], cwd, home)
    shared = {_resolved(Path(place)) for place in ("/tmp", os.environ.get("TMPDIR") or "/tmp")}
    trees = [root for root in (given.writable if given else ()) if _resolved(root) not in shared]
    protect, writable = launch_paths(env, cwd, trees)
    if given is None or not given.may_write(cwd):
        # Its own worktree is writable again within the read-only worktree directory only
        # for a seat that writes its workspace: a `:read-only` one (a reviewer's, in its
        # own worktree) writes none of it.
        writable = ()
    # The reports its `asq exec` saves: writable to every seat, as its inbox is, since a
    # reviewer's checks are reports too. The seals beside them are not.
    writable = (*writable, directory / REPORTS)
    access = adapter.broker_access(
        argv, store, directory / INBOX, protect=protect, writable=writable
    )
    # Whatever sandbox it runs in, the store's profile or a native one (finding 3).
    refuse_inside(access or argv, cwd, adapter, home, env.get("PATH"))
    final = adapter.sandbox_writes((access or argv)[1:], cwd, home)
    said = sandbox_environment(
        env, final, (final.workspace if final else None) or cwd, _hooks_isolated(adapter, home)
    )
    if access is None:
        return argv, "\n".join(said)
    if sys.platform == "win32":
        said.append(
            f"{adapter.label}: the aisquare store is read-only in its sandbox, and its "
            "board calls go through a broker AISquare runs on POSIX only (or WSL)"
        )
        return access, "\n".join(said)
    import secrets

    prune(store / DIR_NAME)
    nonce = secrets.token_urlsafe(32)
    try:
        directory.mkdir(parents=True, mode=0o700)
        (directory / INBOX).mkdir(mode=0o700)
        (directory / OUTBOX).mkdir(mode=0o700)
        (directory / REPORTS).mkdir(mode=0o700)
        (directory / SEALS).mkdir(mode=0o700)
        os.mkfifo(directory / BELL, 0o600)
        broker_env = {key: value for key, value in env.items() if key != DIR_ENV}
        broker_env[NONCE_ENV] = nonce
        run_broker(directory, broker_env)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        said.append(
            f"{adapter.label}: the aisquare store is read-only in its sandbox and its broker "
            f"did not start ({exc}), so its board calls will fail there until a restart"
        )
        return access, "\n".join(said)
    env[DIR_ENV] = str(directory)
    env[NONCE_ENV] = nonce
    return access, "\n".join(said)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    options = parser.parse_args()
    serve(options.dir, options.owner_pid)
