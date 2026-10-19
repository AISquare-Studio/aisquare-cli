"""The system clipboard, written by the platform's own tool beside the app's OSC 52.

Textual's ``App.copy_to_clipboard`` writes only an OSC 52 escape, and the terminal
decides what becomes of it. macOS Terminal.app never implemented OSC 52, and iTerm2
drops it unless Settings > General > Selection > "Applications in terminal may access
clipboard" is on, so on a Mac every copy said "copied" and left the clipboard as it was
(hotfix 0.8.2). Windows Terminal accepts OSC 52, which is why WSL2 never showed it.

So a copy on a LOCAL session also runs the native tool (:func:`pick_writer`): macOS
``pbcopy``; Windows and WSL ``clip.exe``; Linux and the BSDs ``wl-copy`` under
Wayland, else ``xclip`` or ``xsel`` with an X display. On a remote session
(:func:`session_is_remote`) the native clipboard is the wrong machine's, and OSC 52
is the only route. Everything that asks the system — finding the tool, writing with
it — runs on one worker thread (:func:`submit`), the newest waiting copy first, each
write bounded by :data:`WRITE_TIMEOUT`; a failure is logged and told to the caller,
never raised.

Nothing here knows Textual: the app half, OSC 52 and the toast, is
``aisquare.cli.ui.clipboard``.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from aisquare.core.browser import REMOTE_MARKERS
from aisquare.core.terminal import sign_of

log = logging.getLogger(__name__)

WRITE_TIMEOUT = 2.0
"""Seconds one native write may take before it is killed and counted as failed."""

KILL_WAIT = 1.0
"""Seconds a killed tool gets to exit: one stuck in uninterruptible sleep never does, and
waiting on it without a bound held up every later copy for good."""

RECHECK = 60.0
"""Seconds a "no tool here" answer stands before the clipboard thread looks again, so a
tool installed while the app runs is found without a restart."""

ASK_TIMEOUT = 1.0
"""Seconds ``tmux`` or ``ps`` gets to answer a question about this session (:func:`_ask`)."""

_SSH_SERVERS = frozenset({"sshd", "sshd-session", "sshd-auth", "mosh-server", "dropbear"})
"""The command names of the servers a remote login hangs from."""

_PROC = Path("/proc")
"""Where Linux says each process's parent, readable for every process whoever owns it."""

_PROC_VERSION = Path("/proc/version")
"""Says ``microsoft`` on a WSL kernel, where ``WSL_DISTRO_NAME`` may not have been passed."""

_BINFMT = Path("/proc/sys/fs/binfmt_misc")
"""Where WSL registers the handler that runs Windows programs, while interop is on."""

_WINDOWS_CLIP = Path("/mnt/c/Windows/System32/clip.exe")
"""Where WSL reaches ``clip.exe`` when ``appendWindowsPath`` keeps Windows off PATH."""

Osc52Fate = Literal["accepts", "drops", "asks", "unknown"]
"""What becomes of an OSC 52 write here (:func:`osc52_terminal`): ``core.terminal.Osc52``
for a terminal it names, ``"unknown"`` where nothing says."""


@dataclass(frozen=True)
class Writer:
    """One native clipboard tool: what to run, and how the text goes to its stdin."""

    argv: tuple[str, ...]
    encoding: str = "utf-8"
    bom: bool = False
    """Lead the payload with U+FEFF, so ``clip.exe`` reads UTF-16LE as text rather than in
    the console's codepage, which mangles every character outside it."""
    env: tuple[tuple[str, str], ...] = ()
    """Set in the tool's environment over this process's own."""

    def payload(self, text: str) -> bytes:
        """``text`` as this tool reads it from stdin."""
        return (("\ufeff" if self.bom else "") + text).encode(self.encoding)


def is_remote(env: Mapping[str, str]) -> bool:
    """Whether the environment says the user sits at another machine: SSH's variables, or a
    Codespace (``core.browser.REMOTE_MARKERS``). Cheap; :func:`session_is_remote` is sure."""
    return any(env.get(name) for name in REMOTE_MARKERS)


def session_is_remote(env: Mapping[str, str]) -> bool:
    """Whether the user is looking at this machine from another one, where the native
    clipboard is the wrong one.

    The environment alone missed two sessions (review of #266, round 1). asq started
    in the user's own tmux and reattached over SSH keeps the environment it started
    with, so tmux is asked how its client attached last (``SSH_CONNECTION`` is in its
    ``update-environment``). And ``sudo`` or ``su`` over SSH strips SSH's variables,
    while the ``sshd`` the login hangs from is still above this process
    (:func:`_under_ssh_server`). It asks tmux, reads ``/proc`` or runs ``ps``, so it runs
    on the clipboard thread only.
    """
    if is_remote(env):
        return True
    if env.get("TMUX"):
        said = _ask(["tmux", "show-environment", "SSH_CONNECTION"])
        return said is not None and said.startswith("SSH_CONNECTION=")
    return _under_ssh_server()


def _under_ssh_server(pid: int | None = None) -> bool:
    """Whether an SSH or mosh server is among ``pid``'s ancestors (this process's)."""
    return not _SSH_SERVERS.isdisjoint(_ancestors(os.getpid() if pid is None else pid))


def _ancestors(pid: int) -> list[str]:
    """The command names of ``pid`` and its ancestors, nearest first; none when nobody says.

    Linux's ``/proc`` says without a process; elsewhere one ``ps`` lists them all."""
    if _PROC.is_dir():
        return _proc_ancestors(pid)
    if sys.platform == "win32":
        return []
    listed = _ask(["ps", "-A", "-o", "pid=,ppid=,comm="]) or ""
    parents: dict[int, tuple[int, str]] = {}
    for line in listed.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = (int(fields[1]), os.path.basename(fields[2].strip()))
    names: list[str] = []
    while pid in parents and len(names) < 64:
        pid, name = parents[pid]
        names.append(name)
    return names


def _proc_ancestors(pid: int) -> list[str]:
    names: list[str] = []
    while pid > 0 and len(names) < 64:
        try:
            stat = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        head, _, rest = stat.rpartition(")")  # "pid (comm) state ppid …"; comm may hold ")"
        fields = rest.split()
        if len(fields) < 2 or not fields[1].isdigit():
            break
        names.append(head.partition("(")[2])
        pid = int(fields[1])
    return names


def _ask(argv: list[str]) -> str | None:
    """What ``argv`` prints, or ``None`` when it cannot run, fails, or outlasts
    :data:`ASK_TIMEOUT`. Neither ``tmux show-environment`` nor ``ps`` forks a child that
    keeps a pipe open, so their output is read."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=ASK_TIMEOUT, check=False
        )
    except Exception:
        return None
    return done.stdout if done.returncode == 0 else None


def in_wsl(env: Mapping[str, str]) -> bool:
    """Whether this Linux runs under WSL: its own variables, else the kernel's name."""
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    with contextlib.suppress(OSError, UnicodeDecodeError):
        return "microsoft" in _PROC_VERSION.read_text(encoding="utf-8").lower()
    return False


def wsl_interop(env: Mapping[str, str]) -> bool:
    """Whether this WSL can run Windows programs at all.

    ``[interop] enabled=false`` in wsl.conf leaves ``/mnt/c`` mounted and ``clip.exe`` on
    disk, and running it fails with ENOEXEC: every copy then logged a failure, and the
    WSLg ``wl-copy`` that would have worked was never tried (review of #266, round 1).
    Interop on registers ``WSLInterop`` (``WSLInterop-late`` on newer WSL) in
    binfmt_misc; with binfmt_misc not mounted to say, ``WSL_INTEROP`` is the sign.
    """
    if (_BINFMT / "status").exists():
        return any((_BINFMT / name).exists() for name in ("WSLInterop", "WSLInterop-late"))
    return bool(env.get("WSL_INTEROP"))


def pick_writer(
    env: Mapping[str, str],
    *,
    platform: str,
    which: Callable[[str], str | None] | None = None,
    wsl: bool | None = None,
    interop: bool | None = None,
) -> Writer | None:
    """The native tool that writes the clipboard here, or ``None`` when there is none.

    ``pbcopy`` reads stdin in the locale's encoding, which is MacRoman with no locale
    set, so it is run under a UTF-8 one. ``clip.exe`` — native Windows, where
    ``aisquare watch`` and asq's Welcome page run in a conhost that knows no OSC 52,
    and WSL — gets UTF-16LE after a BOM
    (:attr:`Writer.bom`), when WSL can run it (:func:`wsl_interop`, or ``interop`` when
    given); without it a WSLg session still has the Linux tools.
    ``wl-copy`` and ``xclip`` stay alive after the write to serve the selection, which
    is why :func:`write` never reads their output. ``wsl`` is :func:`in_wsl`'s answer,
    asked only when this is Linux and the caller did not give it. ``which`` is
    ``shutil.which`` unless given.

    It scans PATH and reads files, so the event loop never calls it: the clipboard
    thread does (:func:`_resolve`).
    """
    which = shutil.which if which is None else which
    if platform == "darwin":
        found = which("pbcopy")
        return Writer((found,), env=(("LC_ALL", "en_US.UTF-8"),)) if found else None
    if platform == "win32":
        found = which("clip.exe")
        return Writer((found,), encoding="utf-16-le", bom=True) if found else None
    if (
        platform.startswith("linux")
        and (in_wsl(env) if wsl is None else wsl)
        and (wsl_interop(env) if interop is None else interop)
    ):
        found = which("clip.exe") or (str(_WINDOWS_CLIP) if _WINDOWS_CLIP.is_file() else None)
        if found:
            return Writer((found,), encoding="utf-16-le", bom=True)
    # Linux, WSLg, the BSDs and the other unixes: the Wayland tool, else the X ones.
    if env.get("WAYLAND_DISPLAY") and (found := which("wl-copy")):
        return Writer((found, "--type", "text/plain"))
    if env.get("DISPLAY"):
        if found := which("xclip"):
            return Writer((found, "-selection", "clipboard"))
        if found := which("xsel"):
            return Writer((found, "--clipboard", "--input"))
    return None


def local_writer() -> Writer | None:
    """The native tool for THIS session: none when it is remote (:func:`session_is_remote`),
    else :func:`pick_writer`'s.

    It asks the system, so it runs on the clipboard thread only (:func:`_resolve`).
    """
    if session_is_remote(os.environ):
        return None
    return pick_writer(os.environ, platform=sys.platform)


class Ask(Enum):
    """:func:`cached_writer`'s answer when nothing is known yet for this environment."""

    WORKER = "worker"
    """The clipboard thread finds out, at the copy that needs it."""


_KEY_VARIABLES = (
    "PATH",
    *REMOTE_MARKERS,
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
)
"""Every variable :func:`local_writer`'s answer depends on: a change to one asks again."""

_cache: tuple[tuple[str, ...], Writer | None, float] | None = None
"""``(environment key, writer, when found)``, the last answer :func:`_resolve` found."""
_cache_lock = threading.Lock()
_clock = time.monotonic


def _writer_key() -> tuple[str, ...]:
    return (sys.platform, *(os.environ.get(name, "") for name in _KEY_VARIABLES))


def cached_writer() -> Writer | Ask | None:
    """The writer last found for this environment, read without asking the system.

    No PATH scan, no file read, no process: the event loop calls this at every copy,
    and a ``which`` there cost a drag release several to tens of milliseconds on WSL,
    where every Windows PATH directory is a stat over 9p (review of #266, round 1).
    :attr:`Ask.WORKER` when nothing is known for the environment as it is now (a
    variable in :data:`_KEY_VARIABLES` changed, the last write failed, or a "no tool"
    answer is older than :data:`RECHECK`), and always inside the user's tmux, whose
    client may have reattached from another machine since (:func:`session_is_remote`).
    """
    return _known()


def _known() -> Writer | Ask | None:
    """:func:`cached_writer`'s answer, which the clipboard thread asks under its own name."""
    if os.environ.get("TMUX"):
        return Ask.WORKER
    key = _writer_key()
    with _cache_lock:
        if _cache is None or _cache[0] != key:
            return Ask.WORKER
        _, writer, found = _cache
    if writer is None and _clock() - found > RECHECK:
        return Ask.WORKER
    return writer


def forget_writer() -> None:
    """Drop the cached answer: the next copy's writer is looked for again."""
    global _cache
    with _cache_lock:
        _cache = None


def _resolve() -> Writer | None:
    """On the clipboard thread: the writer for this environment, cached or found now."""
    global _cache
    known = _known()
    if not isinstance(known, Ask):
        return known
    key = _writer_key()
    writer = local_writer()
    with _cache_lock:
        _cache = (key, writer, _clock())
    return writer


def write(writer: Writer, text: str, *, timeout: float | None = None) -> bool:
    """Run ``writer`` with ``text`` on its stdin; whether it took it. Never raises.

    stdout and stderr go to the null device: ``xclip`` and ``wl-copy`` fork a child that
    keeps serving the selection, and it would hold a pipe open until another copy
    replaced it, so a read of either waited that long. The tool runs in a session of
    its own, so neither the terminal's hangup nor a suspend of this app stops that
    child. ``timeout`` defaults to :data:`WRITE_TIMEOUT`, read at the call; a tool still
    running then is killed and given :data:`KILL_WAIT` to go (:func:`_stop`).

    Whatever goes wrong is a ``False``: an ``os.environ`` that another thread changes
    while it is copied raises ``RuntimeError``, and so does a spawn during interpreter
    shutdown (review of #266, round 1).
    """
    tool = writer.argv[0]
    try:
        process = subprocess.Popen(
            writer.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **dict(writer.env)} if writer.env else None,
            start_new_session=True,
        )
    except OSError as error:
        log.warning("clipboard: %s could not be run: %s", tool, error)
        return False
    except Exception:
        log.warning("clipboard: %s could not be run", tool, exc_info=True)
        return False
    try:
        process.communicate(
            writer.payload(text), timeout=WRITE_TIMEOUT if timeout is None else timeout
        )
    except subprocess.TimeoutExpired:
        log.warning("clipboard: %s did not finish in time and was stopped", tool)
        _stop(process, tool)
        return False
    except Exception:
        log.warning("clipboard: writing to %s failed", tool, exc_info=True)
        _stop(process, tool)
        return False
    if process.returncode != 0:
        log.warning("clipboard: %s exited %d", tool, process.returncode)
        return False
    return True


def _stop(process: subprocess.Popen[bytes], tool: str) -> None:
    """Kill ``process`` and wait :data:`KILL_WAIT` for it, no longer: ``subprocess.run``'s own
    kill waited without a bound, and a tool in uninterruptible sleep never returned."""
    with contextlib.suppress(OSError):
        process.kill()
    try:
        process.wait(timeout=KILL_WAIT)
    except subprocess.TimeoutExpired:
        log.warning("clipboard: %s did not exit when killed, and was left behind", tool)
    if process.stdin is not None:
        with contextlib.suppress(OSError, ValueError):
            process.stdin.close()


def osc52_terminal(env: Mapping[str, str]) -> Osc52Fate:
    """What becomes of an OSC 52 write here, as far as the environment says.

    The terminal is the first of ``core.terminal.SIGNS`` the environment shows — the
    table doctor names the terminal by, so terminal-specific variables outrank a
    ``TERM_PROGRAM`` an outer shell left behind. Terminal.app drops the write, iTerm2
    takes it only with its own setting on, and kitty, Ghostty, WezTerm, foot,
    alacritty and Windows Terminal take it. A multiplexer in between (tmux, screen)
    keeps or passes it by its own setting, so nothing behind one is known to take it.
    Anything else is ``"unknown"``: Terminal.app over SSH among them, whose
    ``TERM_PROGRAM`` ssh does not send.
    """
    sign = sign_of(env)
    fate: Osc52Fate = "unknown" if sign is None or sign.osc52 is None else sign.osc52
    if fate == "accepts" and _multiplexed(env):
        return "unknown"
    return fate


def _multiplexed(env: Mapping[str, str]) -> bool:
    term = env.get("TERM", "")
    return bool(
        env.get("TERM_PROGRAM") == "tmux"
        or env.get("TMUX")
        or env.get("STY")
        or term.startswith(("tmux", "screen"))
    )


Done = Callable[[bool], None]
"""Told, on the worker thread, whether the native write took the text."""

_jobs: queue.SimpleQueue[tuple[str, Done]] = queue.SimpleQueue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_pending = 0
"""Copies submitted and not yet told: what :func:`flush` waits for."""
_idle = threading.Condition()

FLUSH_TIMEOUT = WRITE_TIMEOUT + KILL_WAIT
"""Seconds :func:`flush` waits at exit: the longest one write can take."""


def submit(text: str, done: Done) -> None:
    """Write ``text`` on the clipboard thread, after the write in progress.

    The thread finds the writer (:func:`_resolve`): finding it scans PATH and reads
    files, which the event loop must never wait on. No writer is a ``False``.

    One thread: two copies made in quick succession each spawn a tool, and run side by
    side the first could finish last and leave its own text on the clipboard. Copies
    that queue up behind a slow write are not each written in turn; only the newest
    is, since it is what the clipboard ends with, and the ones it overtook are told
    its outcome (:func:`_drain`). ``done`` is called on that thread once it is known.
    """
    global _worker, _pending
    with _idle:
        _pending += 1
    _jobs.put((text, done))
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="clipboard", daemon=True)
            _worker.start()


def _drain() -> None:
    """The clipboard thread: nothing it runs may end it, or a copy's toast never comes.

    Every job waiting when a write ends is taken at once and only the newest written.
    Each written in turn, five copies behind a tool that takes :data:`WRITE_TIMEOUT` (an
    ``xclip`` at a stale ``DISPLAY``, a cold WSL interop) took ten seconds, their toasts
    trickling in long after the gestures, for a clipboard that holds the last one only
    (review of #266, round 1). The overtaken copies are told in order, with the newest
    write's outcome: their text was meant for the clipboard the newest one now fills.

    The thread ends when none is waiting, and the next :func:`submit` starts another:
    kept for good, it outlived every test that copied, and each ``forkpty`` later in the
    run warned of forking a threaded process. It takes the jobs and gives up its place
    under ``_worker_lock``, which :func:`submit` checks after queueing, so a copy is
    either taken here or finds no thread and starts one.
    """
    global _worker
    while True:
        batch: list[tuple[str, Done]] = []
        with _worker_lock:
            with contextlib.suppress(queue.Empty):
                while True:
                    batch.append(_jobs.get_nowait())
            if not batch:
                _worker = None
                return
        text, _ = batch[-1]
        try:
            writer = _resolve()
            landed = writer is not None and write(writer, text)
            if writer is not None and not landed:
                forget_writer()  # gone, or broken: look again at the next copy
        except Exception:
            log.warning("clipboard: a write failed unexpectedly", exc_info=True)
            landed = False
        for _, done in batch:
            try:
                done(landed)
            except Exception:
                log.warning("clipboard: the copy's outcome could not be told", exc_info=True)
        _settled(len(batch))


def _settled(count: int) -> None:
    global _pending
    with _idle:
        _pending -= count
        if _pending <= 0:
            _idle.notify_all()


def flush(timeout: float | None = None) -> bool:
    """Wait for every copy submitted so far to be written; whether they all were in time.

    The clipboard thread is a daemon, killed where the interpreter stopped: a copy made
    the moment before quitting never reached the tool, or reached it cut short — a
    payload larger than the pipe's buffer, half written when the process died, which
    ``pbcopy`` stores as it got it (review of #266, round 1). So it runs at exit
    (``atexit``, which comes before daemon threads die), for :data:`FLUSH_TIMEOUT`
    unless ``timeout`` says otherwise.
    """
    with _idle:
        return _idle.wait_for(
            lambda: _pending <= 0, timeout=FLUSH_TIMEOUT if timeout is None else timeout
        )


atexit.register(flush)
