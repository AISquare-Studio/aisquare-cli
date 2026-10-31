"""Every process of the machine at once, and what the tree under one pid consumes.

What ``aisquare fleet top`` and asq's *Agent resources* page read (``services.fleet_top``):
each agent is the process its tmux pane started plus everything under it (``claude``, the
``node`` of its MCP servers, the ``pytest`` or the build it runs), so the figures are a tree's,
summed. Read with ONE ``ps -A`` per refresh whatever the number of agents — never one per
agent — and walked here in memory: about a thousand processes cost some 30 ms on macOS.

``ps -A -o pid=,ppid=,pcpu=,rss=,etime=`` reads the same on macOS, the BSDs and Linux's
procps, with the locale pinned so ``pcpu`` is written with a point. Its ``pcpu`` is the
kernel's figure at the moment of the read: on macOS and the BSDs a decaying average over
about the last minute, on Linux the process's CPU time over its lifetime. ``rss`` is in KiB;
``etime`` is ``[[dd-]hh:]mm:ss``. Windows has no fleet (no tmux) and reads as no table.

A snapshot is one instant: a process that exits after it is not in the next one, and a child
whose parent exited before it was taken hangs under ``launchd``/``init`` there, not under the
agent — the tree is what the table says, never patched from an earlier read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

#: Where macOS and the BSDs keep ``ps`` (as ``core.procs`` reads it); Linux's is found on PATH.
_BSD_PS = "/bin/ps"

_COLUMNS = "pid=,ppid=,pcpu=,rss=,etime="


class Proc(NamedTuple):
    """One process as one ``ps`` line has it."""

    pid: int
    ppid: int
    cpu: float
    """Percent of one core (100 is one core busy; a process on four cores can read 400)."""
    rss_kb: int
    """Resident memory, KiB."""
    elapsed: int | None
    """Seconds since it started; ``None`` where ``etime`` did not parse."""


class TreeUse(NamedTuple):
    """What a tree of processes consumes, its root included."""

    cpu: float
    rss_kb: int
    processes: int
    uptime: int | None
    """The ROOT's age, in seconds: how long the agent itself has run."""


@dataclass(frozen=True)
class Table:
    """Every process one ``ps`` read listed, by pid, with each one's children."""

    procs: dict[int, Proc]
    children: dict[int, list[int]] = field(default_factory=dict)

    @classmethod
    def of(cls, procs: Iterable[Proc]) -> Table:
        by_pid = {proc.pid: proc for proc in procs}
        children: dict[int, list[int]] = {}
        for proc in by_pid.values():
            if proc.ppid != proc.pid:  # pid 0 on macOS is its own parent
                children.setdefault(proc.ppid, []).append(proc.pid)
        return cls(by_pid, children)

    def tree(self, root: int, *, skip: int | None = None) -> TreeUse | None:
        """What ``root`` and every process under it consume, or ``None`` when ``root`` is not
        in the table (it exited before the read, or was never there).

        ``skip``: a pid whose whole subtree is left out — the reader's own, so an agent that
        runs ``fleet top`` is not charged for the ``aisquare`` and the ``ps`` it started to
        look. Walked with a seen set: a table is one instant, but a pid recycled between two
        lines of it must not loop the walk."""
        top = self.procs.get(root)
        if top is None:
            return None
        cpu, rss, count = 0.0, 0, 0
        seen: set[int] = set()
        stack = [root]
        while stack:
            pid = stack.pop()
            if pid in seen or (pid == skip and pid != root):
                continue
            seen.add(pid)
            proc = self.procs.get(pid)
            if proc is None:
                continue
            cpu += proc.cpu
            rss += proc.rss_kb
            count += 1
            stack.extend(self.children.get(pid, ()))
        return TreeUse(round(cpu, 1), rss, count, top.elapsed)


def parse_etime(text: str) -> int | None:
    """``ps``'s ``etime`` as seconds: ``05:09``, ``03:05:09``, ``2-03:05:09``; else ``None``."""
    days, _, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    if days and not days.isdigit():
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def parse(text: str) -> Table:
    """A table out of ``ps -A -o pid=,ppid=,pcpu=,rss=,etime=``'s output. A line that is not
    five fields of those shapes is skipped: one odd line costs that process, not the read."""
    procs: list[Proc] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 5:
            continue
        pid, ppid, cpu, rss, etime = fields
        if not (pid.isdigit() and ppid.isdigit() and rss.isdigit()):
            continue
        try:
            percent = float(cpu.replace(",", "."))
        except ValueError:
            continue
        procs.append(Proc(int(pid), int(ppid), percent, int(rss), parse_etime(etime)))
    return Table.of(procs)


def ps_argv() -> list[str] | None:
    """The one ``ps`` command a read runs, or ``None`` where there is none to run."""
    if sys.platform == "win32":
        return None
    if not sys.platform.startswith("linux") and os.path.exists(_BSD_PS):
        binary: str | None = _BSD_PS
    else:
        binary = shutil.which("ps")
    if binary is None:
        return None
    return [binary, "-A", "-o", _COLUMNS]


def ps_text() -> str | None:
    """``ps``'s output for every process, or ``None`` when it could not be read (no ``ps``,
    a failure, a timeout). Never raises."""
    argv = ps_argv()
    if argv is None:
        return None
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


Reader = Callable[[], str | None]
"""How a table's text is read: :func:`ps_text`, or a test's fake."""


def read(reader: Reader | None = None) -> Table | None:
    """Every process now, from one run of ``reader`` (:func:`ps_text`); ``None`` when it read
    nothing."""
    text = (reader or ps_text)()
    if text is None:
        return None
    return parse(text)
