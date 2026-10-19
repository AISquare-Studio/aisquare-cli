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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

WRITE_TIMEOUT = 2.0
"""Seconds one native write may take before it is killed and counted as failed."""

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
    which: Callable[[str], str | None] = shutil.which,
    wsl: bool | None = None,
) -> Writer | None:
    """The native tool that writes the clipboard here, or ``None`` when there is none.

    ``pbcopy`` reads stdin in the locale's encoding, which is MacRoman with no locale
    set, so it is run under a UTF-8 one. ``clip.exe`` gets UTF-16LE after a BOM
    (:attr:`Writer.bom`); with no ``clip.exe`` a WSLg session still has the Linux tools.
    ``wl-copy`` and ``xclip`` stay alive after the write to serve the selection, which
    is why :func:`write` never reads their output. ``wsl`` is :func:`in_wsl`'s answer,
    asked only when this is Linux and the caller did not give it.
    """
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
    """The native tool for THIS session: none over SSH, else :func:`pick_writer`'s."""
    if is_remote(os.environ):
        return None
    return pick_writer(os.environ, platform=sys.platform)


def write(writer: Writer, text: str, *, timeout: float | None = None) -> bool:
    """Run ``writer`` with ``text`` on its stdin; whether it took it. Never raises.

    stdout and stderr go to the null device: ``xclip`` and ``wl-copy`` fork a child that
    keeps serving the selection, and it would hold a pipe open until another copy
    replaced it, so a read of either waited that long. The tool runs in a session of
    its own, so neither the terminal's hangup nor a suspend of this app stops that
    child. ``timeout`` defaults to :data:`WRITE_TIMEOUT`, read at the call.
    """
    tool = writer.argv[0]
    try:
        done = subprocess.run(
            writer.argv,
            input=writer.payload(text),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **dict(writer.env)} if writer.env else None,
            timeout=WRITE_TIMEOUT if timeout is None else timeout,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        log.warning("clipboard: %s did not finish in time and was stopped", tool)
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        log.warning("clipboard: %s could not be run", tool, exc_info=True)
        return False
    if done.returncode != 0:
        log.warning("clipboard: %s exited %d", tool, done.returncode)
        return False
    return True


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

_jobs: queue.SimpleQueue[tuple[Writer, str, Done]] = queue.SimpleQueue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def submit(writer: Writer, text: str, done: Done) -> None:
    """Write ``text`` with ``writer`` on the clipboard thread, behind every earlier copy.

    One thread, first in first out: two copies made in quick succession each spawn a
    tool, and run side by side the first could finish last and leave its own text on
    the clipboard. ``done`` is called on that thread once the write is over.
    """
    global _worker
    _jobs.put((writer, text, done))
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="clipboard", daemon=True)
            _worker.start()


def _drain() -> None:
    while True:
        writer, text, done = _jobs.get()
        landed = write(writer, text)
        try:
            done(landed)
        except Exception:
            log.warning("clipboard: the copy's outcome could not be told", exc_info=True)
