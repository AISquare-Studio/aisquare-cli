"""A fleet pane's processes and the files they hold open: what ties a record on disk to the row
whose agent wrote it (``AgentAdapter.first_prompts_since``, #113's #24).

An agent's record of a session (Codex's rollout) is named for the time it began and says the
folder it ran in, and nothing of the process that wrote it. Two agents in one folder on one
account home leave records a listing cannot tell apart by what is in them, nor by when they
appeared: Codex makes none until its first prompt runs (measured on 0.164.0: none after 33 s at
a quiet prompt), so a row's own is born with the prompt that is in question.

The process that writes one holds it open for as long as its session lives, and that process
is below the pane's own. Measured on Codex 0.164.0 on macOS (``lsof``): the native binary, two
below the pane's pid through its node wrapper, holds the rollout from its first prompt on, and
a Codex that has run none holds none. So a record is a row's when the pane's processes hold it.

- **Which processes.** The pane's pid (tmux's ``#{pane_pid}``) and every process below it, from
  one ``ps`` (:func:`descendants`).
- **Which files.** ``lsof -p`` on macOS; ``/proc/<pid>/fd`` where there is a ``/proc``
  (:func:`open_files`). The kernel shows a process's descriptors only to a reader who may
  ptrace it, so a process that marks itself non-dumpable answers ``EACCES``: that is "cannot
  tell" (``None``), never "holds nothing". No Linux Codex was at hand to measure which it is.
  The names ``lsof`` prints are read back from its escapes (:func:`_unescaped`): under
  ``LC_ALL=C`` it writes ``/Users/Émile`` as ``/Users/\\xc3\\x89mile``.
- **Whose.** Held is held: a reader below the pane that has another rollout open (a ``tail -f``
  in the pane's shell) is taken for the pane's own. Inherent to asking who holds a file.
- **When it cannot be told** (no ``ps`` or ``lsof``, a process that hides its descriptors, a
  timeout) every answer here is ``None``, and the caller does not guess: it takes the
  stricter rule (``services.fleet._first_prompt_off_its_transcript``).
- **How often.** A listing comes every second or two for as long as a row is pending or
  unsent, and ``ps`` plus ``lsof`` cost 0.1 to 0.2 s on a loaded machine, so what the pane
  holds is kept for :data:`HELD_FOR` seconds (:func:`held_by_pane`). A rollout a prompt has
  just made is seen by the next listing after that.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

PROC = Path("/proc")
"""Where Linux shows its processes; a machine without it is read with ``lsof``."""
HELD_FOR = 2.0
"""Seconds what a pane holds is kept (:func:`held_by_pane`)."""
_TIMEOUT = 5.0
_HELD: dict[int, tuple[float, frozenset[str] | None]] = {}
_HELD_MAX = 256
_LOCK = threading.Lock()


def descendants(root: int) -> frozenset[int] | None:
    """``root`` and every process below it, from one ``ps``; ``None`` when ``ps`` cannot say."""
    try:
        done = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    children: dict[int, list[int]] = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdecimal() and parts[1].isdecimal():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, pending = {root}, [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return frozenset(found)


def open_files(pids: Iterable[int], *, proc: Path = PROC) -> frozenset[str] | None:
    """The absolute paths of the files ``pids`` hold open now, as the system names them (a
    real path: a file opened through a link is named by where it is); ``None`` when that
    cannot be told. A process that exited since it was listed holds nothing."""
    wanted = sorted(set(pids))
    if not wanted:
        return frozenset()
    return _from_proc(wanted, proc) if proc.is_dir() else _from_lsof(wanted)


def _from_proc(pids: list[int], proc: Path) -> frozenset[str] | None:
    found: set[str] = set()
    for pid in pids:
        fds = proc / str(pid) / "fd"
        try:
            names = os.listdir(fds)
        except (FileNotFoundError, NotADirectoryError, ProcessLookupError):
            continue  # exited since `ps`
        except OSError:
            return None  # EACCES: a process this reader may not ptrace
        for name in names:
            try:
                target = os.readlink(fds / name)
            except FileNotFoundError:
                continue  # closed since it was listed
            except OSError:
                return None
            if target.startswith("/"):
                found.add(target)
    return frozenset(found)


_ESCAPE = re.compile(r"((?:\\x[0-9a-fA-F]{2})+)|\\([\\bfnrt])")
_PLAIN = {"\\": "\\", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _unescaped(name: str) -> str:
    """``name`` as ``lsof`` printed it, read back: in a locale that is not UTF-8 (``LC_ALL=C``)
    it writes a byte it cannot print as ``\\xc3\\x89``, a backslash as ``\\\\`` and a tab as
    ``\\t``, so a home such as ``/Users/Émile`` would match no real path (tester-24b). Bytes
    that are not UTF-8 are kept as ``os.fsdecode`` keeps them, which is how a file opened
    under such a name is named by a real path."""

    def one(found: re.Match[str]) -> str:
        if found.group(1) is not None:
            raw = bytes.fromhex(found.group(1).replace("\\x", ""))
            return raw.decode("utf-8", "surrogateescape")
        return _PLAIN[found.group(2)]

    return _ESCAPE.sub(one, name)


def _from_lsof(pids: list[int]) -> frozenset[str] | None:
    try:
        done = subprocess.run(
            ["lsof", "-n", "-P", "-w", "-F", "pn", "-p", ",".join(map(str, pids))],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None  # no lsof, a timeout, or a name this locale cannot decode: not told
    found: set[str] = set()
    answered = False
    for line in done.stdout.splitlines():
        if line.startswith("p"):
            answered = True  # a process it could see
        elif line.startswith("n/"):
            found.add(_unescaped(line[1:]))
    return frozenset(found) if answered else None


def held_by_pane(root: int) -> frozenset[str] | None:
    """The files held open by ``root`` and everything below it, kept for :data:`HELD_FOR`
    seconds; ``None`` when the processes or their files cannot be told."""
    now = time.monotonic()
    with _LOCK:
        kept = _HELD.get(root)
        if kept is not None and now - kept[0] < HELD_FOR:
            return kept[1]
    tree = descendants(root)
    held = None if tree is None else open_files(tree)
    with _LOCK:
        _HELD.pop(root, None)  # to the end: the oldest read goes first
        # Stamped when the read ended, not when it began: a hung ``lsof`` runs to its timeout
        # (5 s), and an answer already older than :data:`HELD_FOR` would be asked again by the
        # next listing, and the next, for every row, for as long as it hangs (tester-24b).
        _HELD[root] = (time.monotonic(), held)
        while len(_HELD) > _HELD_MAX:
            _HELD.pop(next(iter(_HELD)))
    return held


class PaneProcesses:
    """The processes of one row's pane, asked only when a listing has a record to attribute:
    its pid is read from tmux then, once, and what its tree holds is read at most once per
    :data:`HELD_FOR`. What ``AgentAdapter.first_prompts_since`` is given."""

    def __init__(self, pid: Callable[[], int | None]) -> None:
        self._pid = pid
        self._asked = False
        self._root: int | None = None

    def held_below(self, folder: Path) -> frozenset[str] | None:
        """The real paths of the files below ``folder`` that the pane's processes hold open
        now; empty for a pane whose processes hold none there; ``None`` when that cannot be
        told (no pid, no ``ps``, no ``lsof``, a process that hides its descriptors)."""
        if not self._asked:
            self._asked = True
            try:
                self._root = self._pid()
            except Exception:  # tmux not answering, or a pane that is gone: not told
                self._root = None
        if self._root is None:
            return None
        held = held_by_pane(self._root)
        if held is None:
            return None
        under = os.path.realpath(folder) + os.sep
        return frozenset(path for path in held if path.startswith(under))
