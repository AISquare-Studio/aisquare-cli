"""A table of processes for the own-row proof to walk, instead of the machine's. Not a test module.

``services.mcp_host`` asks two things of the process tree: which process a hook runs under
(``CLAUDE_PID``, one of its ancestors) and which process started an MCP server (its parent,
launchers skipped). A test builds the tree it means, with the images the platforms really
show (``claude``, ``zsh``, ``uv``, ``aisquare.exe``), says whose child the code under test is,
and reads what the proof makes of it. :class:`Tree` takes the place of the machine for one
test: :func:`install` points ``procs.inspect`` and ``mcp_host.parent_pid`` at it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from aisquare.core import procs
from aisquare.services import mcp_host, mcp_server


class Tree:
    """Processes by pid; a pid nobody added is a process that cannot be read."""

    def __init__(self) -> None:
        self.table: dict[int, procs.ProcInfo] = {}
        self._next = 4000

    def spawn(self, image: str, parent: int = 1, *, started: str | None = None) -> int:
        """A new process running ``image`` under ``parent``; its pid."""
        pid = self._next
        self._next += 1
        self.table[pid] = procs.ProcInfo(parent, started or f"t{pid}", image)
        return pid

    def reuse(self, pid: int, image: str, parent: int = 1) -> None:
        """``pid`` handed to a NEW process after its holder went: the same pid, a later start."""
        self.table[pid] = procs.ProcInfo(parent, f"t{pid}-again", image)

    def look(self, pid: int) -> procs.ProcInfo | None:
        return self.table.get(pid)

    def gone(self, pid: int) -> None:
        del self.table[pid]

    @contextlib.contextmanager
    def child_of(self, pid: int, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """While the block runs, the code under test is a child of ``pid``."""
        with monkeypatch.context() as scoped:
            scoped.setattr(mcp_host, "parent_pid", lambda: pid)
            yield


def install(monkeypatch: pytest.MonkeyPatch, *, stdio: bool = True) -> Tree:
    """A tree in place of the machine's, a stdio server (``stdio=False``: an HTTP one), and an
    empty memory of who started it."""
    tree = Tree()
    monkeypatch.setattr(procs, "inspect", tree.look)
    monkeypatch.setattr(mcp_host, "_serving", {})
    monkeypatch.setattr(mcp_server, "_stdio", stdio)
    return tree
