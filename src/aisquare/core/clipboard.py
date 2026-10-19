"""The system clipboard, written by the platform's own tool beside the app's OSC 52.

Textual's ``App.copy_to_clipboard`` writes only an OSC 52 escape, and the terminal
decides what becomes of it. macOS Terminal.app never implemented OSC 52, and iTerm2
drops it unless Settings > General > Selection > "Applications in terminal may access
clipboard" is on, so on a Mac every copy said "copied" and left the clipboard as it was
(hotfix 0.8.2). Windows Terminal accepts OSC 52, which is why WSL2 never showed it.

So a copy on a LOCAL session also runs the native tool (:func:`pick_writer`): macOS
``pbcopy``; WSL ``clip.exe``; Linux ``wl-copy`` under Wayland, else ``xclip`` or
``xsel`` with an X display. Over SSH (:func:`is_remote`) the native clipboard is the
wrong machine's, and OSC 52 is the only route. The write runs on one worker thread
(:func:`submit`), in the order the copies were made, each bounded by
:data:`WRITE_TIMEOUT`; a failure is logged and told to the caller, never raised.

Nothing here knows Textual: the app half, OSC 52 and the toast, is
``aisquare.cli.ui.clipboard``.
"""

from __future__ import annotations

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

log = logging.getLogger(__name__)

WRITE_TIMEOUT = 2.0
"""Seconds one native write may take before it is killed and counted as failed."""

KILL_WAIT = 1.0
"""Seconds a killed tool gets to exit: one stuck in uninterruptible sleep never does, and
waiting on it without a bound held up every later copy for good."""

RECHECK = 60.0
"""Seconds a "no tool here" answer stands before the clipboard thread looks again, so a
tool installed while the app runs is found without a restart."""

_PROC_VERSION = Path("/proc/version")
"""Says ``microsoft`` on a WSL kernel, where ``WSL_DISTRO_NAME`` may not have been passed."""

_WINDOWS_CLIP = Path("/mnt/c/Windows/System32/clip.exe")
"""Where WSL reaches ``clip.exe`` when ``appendWindowsPath`` keeps Windows off PATH."""

Osc52Terminal = Literal["Terminal.app", "iTerm2", "accepts", "unknown"]
"""What becomes of an OSC 52 write (:func:`osc52_terminal`): dropped by the terminal
named, taken by a terminal known to take it, or nobody can say."""

_ACCEPTING_PROGRAMS = ("WezTerm", "ghostty")
"""``TERM_PROGRAM`` of the terminals that take an OSC 52 write as they come."""

_ACCEPTING_TERMS = ("xterm-kitty", "xterm-ghostty", "wezterm", "foot", "foot-extra")
"""Their own ``TERM``: kitty, Ghostty, WezTerm and foot, which ssh carries to the host."""


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
    """Whether this session came in over SSH, where the native clipboard is another
    machine's than the one the user is looking at."""
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))


def in_wsl(env: Mapping[str, str]) -> bool:
    """Whether this Linux runs under WSL: its own variables, else the kernel's name."""
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    with contextlib.suppress(OSError, UnicodeDecodeError):
        return "microsoft" in _PROC_VERSION.read_text(encoding="utf-8").lower()
    return False


def pick_writer(
    env: Mapping[str, str],
    *,
    platform: str,
    which: Callable[[str], str | None] | None = None,
    wsl: bool | None = None,
) -> Writer | None:
    """The native tool that writes the clipboard here, or ``None`` when there is none.

    ``pbcopy`` reads stdin in the locale's encoding, which is MacRoman with no locale
    set, so it is run under a UTF-8 one. ``clip.exe`` gets UTF-16LE after a BOM
    (:attr:`Writer.bom`); with no ``clip.exe`` a WSLg session still has the Linux tools.
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
    if not platform.startswith("linux"):
        return None
    if in_wsl(env) if wsl is None else wsl:
        found = which("clip.exe") or (str(_WINDOWS_CLIP) if _WINDOWS_CLIP.is_file() else None)
        if found:
            return Writer((found,), encoding="utf-16-le", bom=True)
    if env.get("WAYLAND_DISPLAY") and (found := which("wl-copy")):
        return Writer((found, "--type", "text/plain"))
    if env.get("DISPLAY"):
        if found := which("xclip"):
            return Writer((found, "-selection", "clipboard"))
        if found := which("xsel"):
            return Writer((found, "--clipboard", "--input"))
    return None


def local_writer() -> Writer | None:
    """The native tool for THIS session: none over SSH, else :func:`pick_writer`'s.

    It asks the system, so it runs on the clipboard thread only (:func:`_resolve`).
    """
    if is_remote(os.environ):
        return None
    return pick_writer(os.environ, platform=sys.platform)


class Ask(Enum):
    """:func:`cached_writer`'s answer when nothing is known yet for this environment."""

    WORKER = "worker"
    """The clipboard thread finds out, at the copy that needs it."""


_KEY_VARIABLES = (
    "PATH",
    "SSH_CONNECTION",
    "SSH_TTY",
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
    answer is older than :data:`RECHECK`).
    """
    return _known()


def _known() -> Writer | Ask | None:
    """:func:`cached_writer`'s answer, which the clipboard thread asks under its own name."""
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


def osc52_terminal(env: Mapping[str, str]) -> Osc52Terminal:
    """What becomes of an OSC 52 write here, as far as the environment says.

    Terminal.app and iTerm2 drop it as they come. ``TERM_PROGRAM`` names them locally;
    over SSH or inside the user's own tmux it reads nothing or ``tmux``, and iTerm2's
    ``LC_TERMINAL`` — an ``LC_*`` variable, which macOS's ssh sends by default — still
    names iTerm2. A multiplexer in between (tmux, screen) keeps or passes the write by
    its own setting, so nothing behind one is known to take it. Known to take it:
    WezTerm and Ghostty by ``TERM_PROGRAM``, kitty by ``KITTY_WINDOW_ID``, Windows
    Terminal by ``WT_SESSION``, and kitty, Ghostty, WezTerm and foot by the ``TERM``
    they set, which ssh carries. Anything else is ``"unknown"``: Terminal.app over SSH
    among them, whose ``TERM_PROGRAM`` ssh does not send.
    """
    program = env.get("TERM_PROGRAM", "")
    term = env.get("TERM", "")
    if program == "Apple_Terminal":
        return "Terminal.app"
    if program == "iTerm.app" or (program in ("", "tmux") and env.get("LC_TERMINAL") == "iTerm2"):
        return "iTerm2"
    if (
        program == "tmux"
        or env.get("TMUX")
        or env.get("STY")
        or term.startswith(("tmux", "screen"))
    ):
        return "unknown"
    if (
        program in _ACCEPTING_PROGRAMS
        or env.get("KITTY_WINDOW_ID")
        or env.get("WT_SESSION")
        or term in _ACCEPTING_TERMS
    ):
        return "accepts"
    return "unknown"


Done = Callable[[bool], None]
"""Told, on the worker thread, whether the native write took the text."""

_jobs: queue.SimpleQueue[tuple[str, Done]] = queue.SimpleQueue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


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
    global _worker
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
    """
    while True:
        batch = [_jobs.get()]
        with contextlib.suppress(queue.Empty):
            while True:
                batch.append(_jobs.get_nowait())
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
