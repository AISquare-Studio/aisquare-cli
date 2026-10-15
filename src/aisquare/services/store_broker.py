"""The broker a sandboxed agent's board calls go through.

An agent that sandboxes its tools (Codex, ``capabilities.sandboxes_tools``) has the
aisquare store read-only in its sandbox (``AgentAdapter.store_access``): nothing a process
there writes reaches the store, so nothing it writes can reach a replay, a kill, a typed
line, a launch, a model or account choice, a worktree removal or a paste outside it. The
board calls its role protocol makes still work there, unchanged (``aisquare note …``,
``aisquare task claim … --as <id>``): the CLI, finding the store read-only and its launch's
broker in its environment (:data:`DIR_ENV`), hands its argv and cwd to the broker and
prints what the command printed, with its exit status (:func:`forward`).

The broker is one process per launch, which ``aisquare launch`` starts outside the sandbox
before it execs the agent, as it starts the telemetry receiver
(``services.native_telemetry``); it exits with the agent (:func:`start_for_launch`, :func:`serve`).
It runs a request as ``aisquare <argv>`` in the LAUNCH's environment, never the caller's,
when the argv names a command on :data:`WHITELIST`, the cwd is on the launch's own board
and the request is signed with the launch's nonce (:data:`NONCE_ENV`, :func:`signature`),
once; anything else is refused with a line saying why. The command it runs acts only as
the launch's own board sessions (:data:`CALLER_ENV`, held where every ``--as`` resolves,
``services.team``): the ones the agent's own hooks, which its agent runs outside the
sandbox, bound to this launch (``core.agent_sessions.bind_launch_session``). It never runs
a fleet operation.

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
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
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
    """Whether this process may write in ``home``: not on the sandbox's read-only mount
    (``EROFS``), nor where its permissions say no."""
    return os.access(home, os.W_OK)


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


def _ring(bell: Path) -> None:
    """One byte to the broker's bell; :class:`BrokerUnavailable` when no broker listens
    (``ENXIO``: a FIFO with no reader)."""
    if sys.platform == "win32":
        raise BrokerUnavailable("a broker's bell is a FIFO, which Windows has not")
    try:
        fd = os.open(bell, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise BrokerUnavailable(
            f"this agent's broker is not running ({exc.strerror}): the aisquare store is "
            "read-only in its sandbox, so restart the agent to have its board calls work"
        ) from exc
    try:
        with suppress(BlockingIOError):
            os.write(fd, b"\0")
    finally:
        os.close(fd)


def signature(nonce: str, body: Mapping[str, object]) -> str:
    """The request ``body``'s MAC under the launch's nonce: a request names its launch by
    it, and a request file, which every sandbox may read (the store is readable), never
    shows the nonce itself."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(nonce.encode(), canonical, "sha256").hexdigest()


def ask(broker: Path, argv: list[str], *, wait: float = ANSWER_WAIT) -> dict[str, Any]:
    """Hand ``argv`` to the broker in ``broker`` and wait for its answer."""
    rid = uuid.uuid4().hex
    body = {"v": 1, "rid": rid, "argv": argv, "cwd": os.getcwd(), "columns": _columns()}
    request = {**body, "mac": signature(os.environ.get(NONCE_ENV, ""), body)}
    inbox = broker / INBOX
    part = inbox / f"{rid}.part"
    try:
        with part.open("x", encoding="utf-8") as handle:
            json.dump(request, handle)
        os.replace(part, inbox / f"{rid}.json")
    except OSError as exc:
        with suppress(OSError):
            part.unlink()
        raise BrokerUnavailable(
            f"cannot hand the command to this agent's broker in {broker} ({exc})"
        ) from exc
    bell = broker / BELL
    _ring(bell)
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
        if now >= deadline:
            raise BrokerUnavailable(f"this agent's broker did not answer in {wait:.0f}s")
        if now - checked >= 1.0:
            checked = now
            _ring(bell)  # still listening: else the command can never be answered
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


def _decoded(data: bytes) -> str:
    return data[:MAX_OUTPUT].decode("utf-8", errors="replace")


def _take(inbox_fd: int, name: str) -> bytes | str:
    """The request ``name`` in ``in/``, removed; or why it is not one. Opened by name in
    the directory the broker made, never through a link, never blocking on a FIFO."""
    if sys.platform == "win32":
        return "the broker runs on POSIX only"
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox_fd)
    except OSError as exc:
        with suppress(OSError):
            os.unlink(name, dir_fd=inbox_fd)
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
        with suppress(OSError):
            os.unlink(name, dir_fd=inbox_fd)


class Broker:
    """One launch's broker: its directory, its nonce and the environment its commands run
    in (the launch's, less the broker's own variables)."""

    def __init__(
        self,
        directory: Path,
        nonce: str,
        environ: Mapping[str, str],
        *,
        board: Callable[[Path], str] | None = None,
    ) -> None:
        self.directory = directory
        self.nonce = nonce
        self.environ = {
            key: value for key, value in environ.items() if key not in (DIR_ENV, NONCE_ENV)
        }
        self.environ[CALLER_ENV] = "1"
        self._board = board or _board_id
        self._own_board: str | None = None
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}
        self._seen: set[str] = set()

    def own_board(self) -> str:
        """The launch's board: the one its own cwd resolves to, under its environment."""
        if self._own_board is None:
            self._own_board = self._board(Path.cwd())
        return self._own_board

    def answer(self, raw: bytes, rid: str) -> dict[str, object]:
        """What the request ``raw`` (``in/<rid>.json``) gets: its command's output and
        status, or a refusal. Each request is run once: a replayed one is refused."""
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
            return _refused("the request is not from this agent's own launch", code=77)
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
            return _refused(why)
        cwd = request.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return _refused("the command's directory is not one the broker can run in")
        try:
            board, own = self._board(Path(cwd)), self.own_board()
        except Exception as exc:
            return _refused(f"the command's board could not be resolved ({exc})")
        if board != own:
            return _refused(
                f"{cwd} is on another board than this agent's: its broker runs commands "
                "on its own board only (run it from the agent's worktree)"
            )
        env = dict(self.environ)
        columns = request.get("columns")
        if isinstance(columns, int) and 20 <= columns <= 1000:
            env["COLUMNS"] = str(columns)
        try:
            done = subprocess.run(
                selfcli.argv_for(argv),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _refused(f"the command ran past {COMMAND_TIMEOUT:.0f}s and was stopped", 124)
        except OSError as exc:
            return _refused(f"the command could not start ({exc})", 126)
        return {
            "code": done.returncode,
            "stdout": _decoded(done.stdout),
            "stderr": _decoded(done.stderr),
        }

    def _handle(self, rid: str, raw: bytes | str) -> None:
        try:
            answer = _refused(raw) if isinstance(raw, str) else self.answer(raw, rid)
        except Exception as exc:  # the broker outlives any one request
            answer = _refused(f"the broker failed on this request ({type(exc).__name__}: {exc})")
        _write_answer(self.directory / OUTBOX, rid, answer)
        with self._lock:
            self._running.pop(rid, None)

    def take_requests(self, inbox_fd: int) -> None:
        """Start each request waiting in ``in/``, up to :data:`RUNNING_AT_ONCE` at once."""
        try:
            names = sorted(entry.name for entry in os.scandir(inbox_fd))
        except OSError:
            return
        for name in names:
            rid, _, kind = name.partition(".")
            if kind != "json" or not _RID.fullmatch(rid):
                continue
            with self._lock:
                if rid in self._running or len(self._running) >= RUNNING_AT_ONCE:
                    continue
                raw = _take(inbox_fd, name)
                worker = threading.Thread(target=self._handle, args=(rid, raw), daemon=True)
                self._running[rid] = worker
            worker.start()

    def tidy(self, inbox_fd: int) -> None:
        """Remove answers nobody read and parts nobody finished, past :data:`ANSWER_TTL`."""
        cutoff = time.time() - ANSWER_TTL
        outbox = self.directory / OUTBOX
        with suppress(OSError):
            for entry in os.scandir(outbox):
                with suppress(OSError):
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
        with suppress(OSError):
            for entry in os.scandir(inbox_fd):
                if entry.name.endswith(".json"):
                    continue
                with suppress(OSError):
                    if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.name, dir_fd=inbox_fd)

    def idle(self) -> bool:
        with self._lock:
            return not self._running


def _board_id(cwd: Path) -> str:
    from aisquare.core.orchestrator import team_project

    return team_project(cwd).id


def serve(
    directory: Path,
    owner_pid: int,
    *,
    nonce: str | None = None,
    owner_alive: Callable[[], bool] | None = None,
) -> None:
    """Answer the requests in ``directory`` while the agent ``owner_pid`` runs, then remove
    the directory. The layout is :func:`start_for_launch`'s, made before the agent's
    sandbox could."""
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
        deadline = time.monotonic() + 5
        while not broker.idle() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
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
    access = adapter.broker_access(argv, store, directory / INBOX)
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
