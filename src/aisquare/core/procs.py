"""What one process is: its parent, when it started, and what it runs.

The proof behind a session's own MCP server row (``services/mcp_host``, option B of
docs/plans/mcp-own-row.md) is about processes: the server runs as a child of the Claude Code
process that registered the session, and a pid alone proves nothing, because pids are
recycled. So a process is a :class:`ProcessRef` (its pid and a *start token*), and one that
cannot be inspected is ``None``, never a guess: every caller reads ``None`` as "no proof",
which is the safe side (the hard rule of the plan: no member of a team may read as solo).

One question, three sources, the same answer:

- **Linux and WSL**: ``/proc/<pid>/stat`` (the parent, and ``starttime`` in clock ticks
  since boot) and ``/proc/<pid>/exe``. The token carries this boot's id, so a pid and a tick
  count from another boot do not match.
- **macOS and the BSDs**: ``ps -o ppid=,lstart=,comm=`` (one run per question, with the
  locale AND the time zone pinned: ``lstart`` is written in the reader's zone, and a hook
  and a server that disagree on it would never match). ``lstart`` has a second's
  resolution: a pid recycled inside the second its holder started is what that cannot see.
- **Windows**: the Toolhelp snapshot for the parent and the image, ``GetProcessTimes`` for
  the creation time (a ``FILETIME``). Windows keeps the pid of whoever *created* a process,
  and does not clear it when that process exits, so a "parent" that started after its child
  is a recycled pid, not a parent (read as ``None``).

Measured on macOS with Claude Code 2.1.310 (docs/plans/mcp-own-row.md, *What was measured*).
Linux and Windows are read from their documented interfaces and run by CI's legs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

#: Deep enough for every launcher chain measured (a shell, ``uv``, a trampoline) and the
#: hook's own ``sh -c``; a walk that is longer than this is not one worth trusting.
MAX_DEPTH = 6


class ProcessRef(NamedTuple):
    """A process the way a recycled pid cannot be: its pid and when it started."""

    pid: int
    started: str
    """An opaque token, equal for the same process and different for another that got its
    pid later. Compare it; do not parse it."""


class ProcInfo(NamedTuple):
    """What :func:`inspect` learns of one process."""

    ppid: int
    started: str
    image: str
    """The executable's path where the platform gives one, else its name. ``""``: unknown."""


Inspect = Callable[[int], ProcInfo | None]


def image_stem(image: str) -> str:
    """The name an image goes by, lower case, without the directory or ``.exe``:
    ``C:\\Tools\\UV.EXE`` and ``/usr/local/bin/uv`` are both ``uv``."""
    name = image.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return name.removesuffix(".exe")


def inspect(pid: int) -> ProcInfo | None:
    """The process ``pid`` is, or ``None`` when it cannot be told (gone, not ours to read,
    the platform's tool missing): never an exception, never a partial answer."""
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _inspect_windows(pid)
        if sys.platform.startswith("linux"):
            return _inspect_proc(pid)
        return _inspect_ps(pid)
    except Exception:
        return None


# --- Linux and WSL ----------------------------------------------------------------------

#: Where Linux keeps its processes; a test points it at a tree of its own.
_PROC = Path("/proc")


def parse_stat(text: str) -> tuple[int, str] | None:
    """``(ppid, starttime)`` out of a ``/proc/<pid>/stat`` line, or ``None`` if it is not one.

    The command is in parentheses and may hold spaces and parentheses itself, so the
    fields are counted from the LAST ``)``: ``state`` is the first after it, ``ppid`` the
    second, and ``starttime`` (field 22 of the file) the twentieth."""
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 1 :].split()
    if len(fields) < 20 or not fields[1].lstrip("-").isdigit() or not fields[19].isdigit():
        return None
    return int(fields[1]), fields[19]


def _inspect_proc(pid: int) -> ProcInfo | None:
    where = _PROC / str(pid)
    stat = parse_stat((where / "stat").read_text(encoding="utf-8", errors="replace"))
    if stat is None:
        return None
    ppid, ticks = stat
    try:
        boot = (_PROC / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()[:8]
    except OSError:
        boot = ""
    try:
        # a binary replaced while it runs (``uv self update``) reads ``/path/uv (deleted)``
        image = os.readlink(where / "exe").removesuffix(" (deleted)")
    except OSError:  # another user's process, or a kernel thread
        image = ""
    return ProcInfo(ppid, f"{boot}.{ticks}", image)


# --- macOS and the BSDs -------------------------------------------------------------------


def parse_ps(text: str) -> ProcInfo | None:
    """A ``ps -o ppid=,lstart=,comm=`` line as a :class:`ProcInfo`, or ``None``.

    ``lstart`` is five words (``Mon Oct 26 00:01:10 2026``, the day padded with a space
    below 10), so the command is everything after the sixth word, spaces and all."""
    words = text.split()
    if len(words) < 7 or not words[0].isdigit():
        return None
    return ProcInfo(int(words[0]), " ".join(words[1:6]), " ".join(words[6:]))


#: Where macOS and the BSDs keep ``ps``. Not found on PATH: a ``procps`` a package manager put
#: first writes its columns another way, and the token must read the same in every process.
_PS = "/bin/ps"


def _inspect_ps(pid: int) -> ProcInfo | None:
    result = subprocess.run(
        [_PS, "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
        stdin=subprocess.DEVNULL,  # an MCP server's own stdin is the protocol pipe
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
    )
    if result.returncode != 0:
        return None
    return parse_ps(result.stdout)


# --- Windows ------------------------------------------------------------------------------


def _inspect_windows(pid: int) -> ProcInfo | None:
    if sys.platform != "win32":  # also tells mypy the ctypes names below exist
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Entry(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    class _FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(_FileTime)] * 4)]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def created(process_id: int) -> int | None:
        handle = kernel32.OpenProcess(
            0x1000, False, process_id
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            made, spent = _FileTime(), (_FileTime(), _FileTime(), _FileTime())
            if not kernel32.GetProcessTimes(handle, made, *spent):
                return None
            return int((made.high << 32) | made.low)
        finally:
            kernel32.CloseHandle(handle)

    def image_of(process_id: int, fallback: str) -> str:
        handle = kernel32.OpenProcess(0x1000, False, process_id)
        if not handle:
            return fallback
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return fallback
        finally:
            kernel32.CloseHandle(handle)

    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:  # NULL, or INVALID_HANDLE_VALUE
        return None
    entry = _Entry()
    entry.dwSize = ctypes.sizeof(_Entry)
    ppid, exe = None, ""
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ProcessID == pid:
                ppid, exe = int(entry.th32ParentProcessID), entry.szExeFile
                break
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    started = created(pid)
    if ppid is None or started is None:
        return None
    parent_started = created(ppid)
    if parent_started is None or parent_started > started:
        ppid = 0  # the pid of a parent that has gone, taken by a newer process
    return ProcInfo(ppid, str(started), image_of(pid, exe))
