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

A tool that fails is not the end of the copy: the next one the platform has is tried
(:func:`fallback_writers`, at most :data:`MAX_TRIES` a copy), and a tool that failed is
passed over for :data:`RECHECK`. Inside the user's own tmux, tmux itself is one more
route (:func:`relay_writer`, ``tmux load-buffer -w``): the one that can reach a terminal
over SSH, where tmux keeps an app's own OSC 52 to itself by default. What became of a
copy is told as a :class:`CopyResult` — copied, only sent, or failed, and why
(:func:`verdict`) — so every surface says what is true (R13, #146).

Nothing here knows Textual: the app half, OSC 52 and the toast, is
``aisquare.cli.ui.clipboard``.
"""

from __future__ import annotations

import atexit
import base64
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
from aisquare.core.terminal import multiplexed, sign_of

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

_ATTACH_MARKERS = ("SSH_CONNECTION", "SSH_TTY")
"""The ``REMOTE_MARKERS`` that name the client a session started under, which a multiplexer's
reattach leaves stale; the others say where the machine itself is."""

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

_WINDOWS_POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
"""Where WSL reaches Windows PowerShell when ``appendWindowsPath`` keeps Windows off PATH."""

POWERSHELL_SET = (
    "[Console]::InputEncoding = [Text.Encoding]::UTF8; "
    "Set-Clipboard -Value ([Console]::In.ReadToEnd())"
)
"""What Windows PowerShell runs to put its stdin, read as UTF-8, on the clipboard: the route
after ``clip.exe``, which a stale WSL interop or a broken console can fail."""

MAX_TRIES = 2
"""Tools one copy may try, in the platform's order, before it is OSC 52's alone: two writes'
worth of waiting at most, and a tool that failed is passed over for :data:`RECHECK`, so the
next copy starts at the next one."""

OSC52_LIMIT = 100_000
"""The most base64 one OSC 52 write carries. Terminals and tmux drop a longer one without a
word, so a copy past it is not sent at all, and says so (:func:`verdict`)."""

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
    relay: bool = False
    """It hands the text to a multiplexer (``tmux load-buffer -w``), which passes it on to
    the terminal by its own settings: taken, the copy was SENT, not known to be copied."""

    @property
    def name(self) -> str:
        """The tool's own name, as a copy's outcome says it: ``pbcopy``, ``clip.exe``."""
        return os.path.basename(self.argv[0])

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

    Inside tmux, tmux's answer decides whichever way it points, and the process's own SSH
    variables only when tmux says nothing: they are as stale after a reattach in the other
    direction. A tmux created over SSH and attached at the machine later still had them,
    and ``pbcopy`` never ran (review of #266, round 3). A Codespace is remote whoever
    attaches.
    """
    if any(env.get(name) for name in REMOTE_MARKERS if name not in _ATTACH_MARKERS):
        return True
    if env.get("TMUX"):
        said = _ask(["tmux", "show-environment", "SSH_CONNECTION"])
        if said is not None:
            return said.startswith("SSH_CONNECTION=")
        return is_remote(env)
    return is_remote(env) or _under_ssh_server()


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
    """The native tool that writes the clipboard here first, or ``None`` when there is none:
    the first of :func:`pick_writers`."""
    found = pick_writers(env, platform=platform, which=which, wsl=wsl, interop=interop)
    return found[0] if found else None


def pick_writers(
    env: Mapping[str, str],
    *,
    platform: str,
    which: Callable[[str], str | None] | None = None,
    wsl: bool | None = None,
    interop: bool | None = None,
) -> tuple[Writer, ...]:
    """Every native tool that writes the clipboard here, in the order a copy tries them.

    ``pbcopy`` reads stdin in the locale's encoding, which is MacRoman with no locale
    set, so it is run under a UTF-8 one. ``clip.exe`` — native Windows, where
    ``aisquare watch`` and asq's Welcome page run in a conhost that knows no OSC 52,
    and WSL — gets UTF-16LE after a BOM
    (:attr:`Writer.bom`), when WSL can run it (:func:`wsl_interop`, or ``interop`` when
    given); after it Windows PowerShell's ``Set-Clipboard`` (:data:`POWERSHELL_SET`).
    Without them a WSLg session still has the Linux tools.
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
        return (Writer((found,), env=(("LC_ALL", "en_US.UTF-8"),)),) if found else ()
    writers: list[Writer] = []
    if platform == "win32":
        if found := which("clip.exe"):
            writers.append(Writer((found,), encoding="utf-16-le", bom=True))
        if found := which("powershell.exe"):
            writers.append(_powershell(found))
        return tuple(writers)
    if (
        platform.startswith("linux")
        and (in_wsl(env) if wsl is None else wsl)
        and (wsl_interop(env) if interop is None else interop)
    ):
        if found := which("clip.exe") or _on_disk(_WINDOWS_CLIP):
            writers.append(Writer((found,), encoding="utf-16-le", bom=True))
        if found := which("powershell.exe") or _on_disk(_WINDOWS_POWERSHELL):
            writers.append(_powershell(found))
    # Linux, WSLg, the BSDs and the other unixes: the Wayland tool, else the X ones.
    if env.get("WAYLAND_DISPLAY") and (found := which("wl-copy")):
        writers.append(Writer((found, "--type", "text/plain")))
    if env.get("DISPLAY"):
        if found := which("xclip"):
            writers.append(Writer((found, "-selection", "clipboard")))
        if found := which("xsel"):
            writers.append(Writer((found, "--clipboard", "--input")))
    return tuple(writers)


def _on_disk(path: Path) -> str | None:
    return str(path) if path.is_file() else None


def _powershell(found: str) -> Writer:
    return Writer((found, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SET))


def fallback_writers(first: Writer, *, remote: bool = False) -> tuple[Writer, ...]:
    """The tools a copy tries after ``first``, should it fail: the rest of
    :func:`pick_writers`, none on a remote session. It scans PATH, so it runs on the
    clipboard thread only."""
    if remote:
        return ()
    found = pick_writers(os.environ, platform=sys.platform)
    return tuple(writer for writer in found if writer.argv[0] != first.argv[0])


def relay_writer() -> Writer | None:
    """Inside the user's own tmux, tmux itself: ``tmux load-buffer -w -`` puts the text in
    a tmux buffer and sends it to the terminal of the client attached (tmux 3.2 and later),
    which an app's own OSC 52 does not reach under tmux's default ``set-clipboard
    external``. ``None`` outside tmux, or with no ``tmux`` on PATH. It scans PATH, so it
    runs on the clipboard thread only."""
    if not os.environ.get("TMUX"):
        return None
    found = shutil.which("tmux")
    return Writer((found, "load-buffer", "-w", "-"), relay=True) if found else None


def local_writer() -> Writer | None:
    """The native tool for THIS session: none when it is remote (:func:`session_is_remote`),
    else :func:`native_writer`'s.

    It asks the system, so it runs on the clipboard thread only (:func:`_resolve`).
    """
    if session_is_remote(os.environ):
        return None
    return native_writer()


def native_writer() -> Writer | None:
    """This machine's tool (:func:`pick_writer`), whether or not the user sits at it: what
    :func:`_resolve` keeps inside tmux, where only the remote question is asked again.

    It scans PATH and reads files, so it runs on the clipboard thread only.
    """
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
    "TMUX",
)
"""Every variable :func:`local_writer`'s answer depends on: a change to one asks again.
``TMUX`` too, since inside tmux the cache holds another question's answer (:func:`_resolve`)."""

_cache: tuple[tuple[str, ...], tuple[Writer, ...], float] | None = None
"""``(environment key, writers, when found)``, the last answer :func:`_resolve` found, in the
order a copy tries them: outside tmux :func:`local_writer`'s tool and its
:func:`fallback_writers`, inside it :func:`native_writer`'s and theirs, then
:func:`relay_writer`'s."""
_failed: dict[str, float] = {}
"""When each tool's write last failed, by its path: passed over for :data:`RECHECK`."""
_cache_lock = threading.Lock()
_clock = time.monotonic


def _writer_key() -> tuple[str, ...]:
    return (sys.platform, *(os.environ.get(name, "") for name in _KEY_VARIABLES))


def cached_writer() -> Writer | Ask | None:
    """The writer last found for this environment, read without asking the system.

    No PATH scan, no file read, no process: the event loop calls this at every copy,
    and a ``which`` there cost a drag release several to tens of milliseconds on WSL,
    where every Windows PATH directory is a stat over 9p (review of #266, round 1).
    ``None`` when there is no tool, or every tool's last write failed (:func:`_tool_failed`).
    :attr:`Ask.WORKER` when nothing is known for the environment as it is now (a
    variable in :data:`_KEY_VARIABLES` changed, or a "no tool" answer is older than
    :data:`RECHECK`), and inside the user's tmux whenever there is a tool, since its
    client may have reattached from another machine since (:func:`session_is_remote`).
    """
    return _known()


def _known() -> Writer | Ask | None:
    """:func:`cached_writer`'s answer, which the clipboard thread asks under its own name: the
    first tool not passed over, ``None`` for none, :attr:`Ask.WORKER` when the search is
    due again (the environment changed, a "no tool" answer or a tool's failure is older
    than :data:`RECHECK`, or this is the user's tmux, where whose clipboard a tool writes is
    asked at each copy)."""
    if _due():
        return Ask.WORKER
    with _cache_lock:
        writers = _cache[1] if _cache is not None else ()
        usable = [w for w in writers if w.argv[0] not in _failed]
    if not usable:
        return None
    if os.environ.get("TMUX"):
        return Ask.WORKER  # the tools are known; whose clipboard they write is asked per copy
    return usable[0]


def _due() -> bool:
    """Whether the tools are to be looked for again: nothing is known for the environment as
    it is now, a "no tool" answer is older than :data:`RECHECK`, or so is a tool's failure."""
    key = _writer_key()
    with _cache_lock:
        if _cache is None or _cache[0] != key:
            return True
        _, writers, found = _cache
        failed = dict(_failed)
    now = _clock()
    if not writers:
        return now - found > RECHECK
    return any(now - failed[w.argv[0]] > RECHECK for w in writers if w.argv[0] in failed)


def forget_writer() -> None:
    """Drop the cached answer and every failure held: the next copy looks for its tools again."""
    global _cache
    with _cache_lock:
        _cache = None
        _failed.clear()


def _tool_failed(writer: Writer) -> None:
    """Pass ``writer`` over for :data:`RECHECK`: the copies meanwhile start at the next tool,
    or are OSC 52's alone, told at once, when it was the last.

    Only dropped from the cache, a broken tool was found again by the next copy and run
    again. An ``xclip`` that hangs on a stale ``DISPLAY`` then held every copy's toast
    for :data:`WRITE_TIMEOUT` and :data:`KILL_WAIT` more, for as long as the app ran
    (review of #266, round 2).
    """
    with _cache_lock:
        _failed[writer.argv[0]] = _clock()


def _resolve() -> tuple[Writer, ...]:
    """On the clipboard thread: the tools to try for this copy, in order, cached or found now.

    Inside the user's tmux the cache keeps the machine's tools (:func:`native_writer`) and
    tmux's own route (:func:`relay_writer`), and only tmux's word on its last attach is
    asked at each copy (:func:`session_is_remote`): the rest of the search cannot change on
    a reattach, and run at every copy it cost each toast a ``which`` per tool, on WSL a
    scan of the Windows PATH over 9p (review of #266, round 2). A remote attach leaves
    tmux's route alone. Outside tmux nothing in this process changes whether the session
    is remote, so the cache keeps :func:`local_writer`'s whole answer.
    """
    global _cache
    tmux = bool(os.environ.get("TMUX"))
    if _due():
        key = _writer_key()
        with _cache_lock:
            now = _clock()
            for path, when in list(_failed.items()):
                if now - when > RECHECK:
                    del _failed[path]
        first = native_writer() if tmux else local_writer()
        found: tuple[Writer, ...] = () if first is None else (first, *fallback_writers(first))
        relay = relay_writer() if tmux else None
        if relay is not None:
            found = (*found, relay)
        with _cache_lock:
            _cache = (key, found, _clock())
    with _cache_lock:
        writers = _cache[1] if _cache is not None else ()
        usable = tuple(w for w in writers if w.argv[0] not in _failed)
    if tmux and session_is_remote(os.environ):
        return tuple(w for w in usable if w.relay)
    return usable


def write(writer: Writer, text: str, *, timeout: float | None = None) -> bool:
    """Run ``writer`` with ``text`` on its stdin; whether it took it. Never raises.

    stdout and stderr go to the null device: ``xclip`` and ``wl-copy`` fork a child that
    keeps serving the selection, and it would hold a pipe open until another copy
    replaced it, so a read of either waited that long. The tool runs in a session of
    its own, so neither the terminal's hangup nor a suspend of this app stops that
    child. ``timeout`` defaults to :data:`WRITE_TIMEOUT`, read at the call; a tool still
    running then is killed and given :data:`KILL_WAIT` to go (:func:`_stop`).

    Its stdin is fed on a thread of its own (:func:`_feed`), and the timeout is the
    wait's. ``Popen.communicate`` on Windows writes the whole of stdin before its
    timeout starts (CPython's win32 ``_communicate``), so a ``clip.exe`` that stopped
    reading a copy larger than the pipe held the one clipboard thread for good (review
    of #266, round 2). Killed, the tool takes its end of the pipe with it, and the
    feeding thread ends.

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
    feeding = threading.Thread(
        target=_feed, args=(process, writer.payload(text)), name="clipboard-feed", daemon=True
    )
    try:
        feeding.start()
        process.wait(timeout=WRITE_TIMEOUT if timeout is None else timeout)
    except subprocess.TimeoutExpired:
        log.warning("clipboard: %s did not finish in time and was stopped", tool)
        _stop(process, tool)
        return False
    except Exception:
        log.warning("clipboard: writing to %s failed", tool, exc_info=True)
        _stop(process, tool)
        return False
    feeding.join(KILL_WAIT)  # over already: the tool read to the end before it exited
    if process.returncode != 0:
        log.warning("clipboard: %s exited %d", tool, process.returncode)
        return False
    return True


def _feed(process: subprocess.Popen[bytes], payload: bytes) -> None:
    """Hand ``payload`` to the tool's stdin and close it, on a thread of its own; never
    raises. A tool that exits or is killed before reading it all is a broken pipe, which
    ``communicate`` ignored too. The stdin is this thread's to close: closed from another
    while this one is blocked writing, it waited on the same lock (:func:`write`)."""
    stdin = process.stdin
    if stdin is None:
        return
    with contextlib.suppress(OSError, ValueError):
        stdin.write(payload)
    with contextlib.suppress(OSError, ValueError):
        stdin.close()


def _stop(process: subprocess.Popen[bytes], tool: str) -> None:
    """Kill ``process`` and wait :data:`KILL_WAIT` for it, no longer: ``subprocess.run``'s own
    kill waited without a bound, and a tool in uninterruptible sleep never returned. Its
    stdin is left to :func:`_feed`, which the kill unblocks."""
    with contextlib.suppress(OSError):
        process.kill()
    try:
        process.wait(timeout=KILL_WAIT)
    except subprocess.TimeoutExpired:
        log.warning("clipboard: %s did not exit when killed, and was left behind", tool)


def osc52_terminal(env: Mapping[str, str]) -> Osc52Fate:
    """What becomes of an OSC 52 write here, as far as the environment says.

    The terminal is the first of ``core.terminal.SIGNS`` the environment shows — the
    table doctor names the terminal by, so terminal-specific variables outrank a
    ``TERM_PROGRAM`` an outer shell left behind. Terminal.app drops the write, iTerm2
    takes it only with its own setting on, and kitty, Ghostty, WezTerm, foot,
    alacritty and Windows Terminal take it. Behind a multiplexer (tmux, screen) it is
    always ``"unknown"``: the multiplexer keeps or passes the write by its own setting,
    and the variables that name the terminal are those of whichever one started it
    (:func:`~aisquare.core.terminal.multiplexed`), so an iTerm2 hint went to whoever
    reattached from kitty (review of #266, round 2). Anything else is ``"unknown"`` too:
    Terminal.app over SSH among them, whose ``TERM_PROGRAM`` ssh does not send.
    """
    if multiplexed(env):
        return "unknown"
    sign = sign_of(env)
    return "unknown" if sign is None or sign.osc52 is None else sign.osc52


def osc52_writes(text: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """The escape sequences that carry ``text`` to the terminal's clipboard by OSC 52, as
    the app writes them: none for ``""`` (an empty OSC 52 CLEARS the clipboard) or past
    :data:`OSC52_LIMIT`.

    Outside a multiplexer, the sequence alone. Inside tmux, the sequence (which tmux keeps
    or passes by its ``set-clipboard``) and the same wrapped for tmux's passthrough, which
    reaches the terminal where ``allow-passthrough`` is on (tmux 3.3 and later). Inside
    GNU screen, which keeps an app's OSC 52 to itself, the sequence wrapped in screen's
    passthrough, cut in 76-character pieces as screen takes them.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if not payload or len(payload) > OSC52_LIMIT:
        return ()
    raw = f"\x1b]52;c;{payload}\a"
    if env.get("TMUX"):
        return (raw, "\x1bPtmux;" + raw.replace("\x1b", "\x1b\x1b") + "\x1b\\")
    if env.get("STY"):
        pieces = [raw[start : start + 76] for start in range(0, len(raw), 76)]
        return ("\x1bP" + "\x1b\\\x1bP".join(pieces) + "\x1b\\",)
    return (raw,)


Verdict = Literal["copied", "sent", "failed"]
"""What a copy can say of itself: ``copied`` when a tool took it, or OSC 52 went to a
terminal known to take it; ``sent`` when it went where nobody can say what becomes of it
(a terminal no sign names, iTerm2's setting, a multiplexer); ``failed`` when nothing could
carry it."""


@dataclass(frozen=True)
class CopyResult:
    """What became of one copy, for the surface that made it to say (:func:`verdict`)."""

    verdict: Verdict
    route: Literal["native", "tmux", "osc52"] | None = None
    tool: str | None = None
    fate: Osc52Fate | None = None
    """What the terminal makes of OSC 52 (:func:`osc52_terminal`), when the copy rests on it."""
    reason: str | None = None
    """One line on why it is not ``copied``."""

    @property
    def ok(self) -> bool:
        return self.verdict == "copied"


TOO_LARGE = "too large for the terminal's clipboard, and no clipboard tool took it"
NO_ROUTE = "no clipboard tool took it, and there is no terminal to send it to"


def osc52_fits(text: str) -> bool:
    """Whether ``text`` is within what one OSC 52 write carries (:data:`OSC52_LIMIT`)."""
    return len(base64.b64encode(text.encode("utf-8"))) <= OSC52_LIMIT


def verdict(
    written: Written, *, osc52: bool, fate: Osc52Fate, too_large: bool = False
) -> CopyResult:
    """What one copy says, from its native write (``written``), whether an OSC 52 write went
    out (``osc52``), what the terminal makes of one (``fate``), and whether the text was
    past :data:`OSC52_LIMIT` (``too_large``).

    A tool that took it is a copy, unless it was a multiplexer's, which passes it on by its
    own settings. Else the copy rests on OSC 52: a copy when the terminal is known to take
    it, sent where nobody can say, failed on Terminal.app (which drops it), past
    :data:`OSC52_LIMIT`, or with no terminal to send it to.
    """
    if written.landed and not written.relayed:
        return CopyResult("copied", "native", written.tool)
    if written.landed:
        return CopyResult("sent", "tmux", written.tool, "unknown")
    if not osc52:
        return CopyResult("failed", reason=TOO_LARGE if too_large else NO_ROUTE)
    if fate == "accepts":
        return CopyResult("copied", "osc52", fate=fate)
    if fate == "drops":
        return CopyResult("failed", "osc52", fate=fate, reason="the terminal ignores OSC 52")
    return CopyResult("sent", "osc52", fate=fate)


Done = Callable[[bool], None]
"""Told, on the worker thread, whether the native write took the text."""


@dataclass(frozen=True)
class Written:
    """What became of a copy's native write: which tool took it, if one did."""

    landed: bool
    tool: str | None = None
    """The tool that took the text (:attr:`Writer.name`)."""
    relayed: bool = False
    """The tool was a multiplexer's (:attr:`Writer.relay`): passed on, not known to be copied."""
    tried: tuple[str, ...] = ()
    """Every tool this copy ran, in order."""


_jobs: queue.SimpleQueue[tuple[str, Callable[[Written], None]]] = queue.SimpleQueue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_pending = 0
"""Copies submitted and not yet told: what :func:`flush` waits for."""
_idle = threading.Condition()

FLUSH_TIMEOUT = 2 * (ASK_TIMEOUT + MAX_TRIES * (WRITE_TIMEOUT + KILL_WAIT))
"""Seconds :func:`flush` waits at exit: the longest two batches can take, each a question
about the session (:data:`ASK_TIMEOUT`, ``tmux`` or ``ps`` in :func:`_resolve`) and up to
:data:`MAX_TRIES` writes. The copy made at quit can wait behind the write in progress, so
it is in the second; one write's worth gave up before it was made (review of #266, round 2)."""


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
    copy_later(text, lambda written: done(written.landed))


def copy_later(text: str, done: Callable[[Written], None]) -> None:
    """:func:`submit`, told the whole outcome: which tool took the text, if one did."""
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
        batch: list[tuple[str, Callable[[Written], None]]] = []
        with _worker_lock:
            with contextlib.suppress(queue.Empty):
                while True:
                    batch.append(_jobs.get_nowait())
            if not batch:
                _worker = None
                return
        text, _ = batch[-1]
        written = _write_any(text)
        for _, done in batch:
            try:
                done(written)
            except Exception:
                log.warning("clipboard: the copy's outcome could not be told", exc_info=True)
        _settled(len(batch))


def _write_any(text: str) -> Written:
    """On the clipboard thread: ``text`` to the first tool that takes it, of at most
    :data:`MAX_TRIES`; each that fails is passed over (:func:`_tool_failed`). Never raises."""
    tried: list[str] = []
    try:
        for writer in _resolve()[:MAX_TRIES]:
            tried.append(writer.name)
            if write(writer, text):
                return Written(True, writer.name, writer.relay, tuple(tried))
            _tool_failed(writer)  # gone, or broken: passed over until it is looked for
    except Exception:
        log.warning("clipboard: a write failed unexpectedly", exc_info=True)
    return Written(False, tried=tuple(tried))


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
