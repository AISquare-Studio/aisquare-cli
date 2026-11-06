"""Every ``aisquare launch`` waiting on its agent on this home, one record each (review r40 of
#113, finding 7).

Where the platform has no exec (Windows), ``aisquare launch`` runs the agent as its child and
waits for it (``cli.launch._exec``), so the tool's ``python.exe``, the ``.pyd`` files it
loaded and ``aisquare.exe`` stay open for the whole session. Windows refuses to replace or
delete a file in use, so a ``uv tool install --force``, ``aisquare upgrade`` or ``uv tool
uninstall`` run beside such a session fails part-way on those files (os error 5 or 32).
On 0.9.2 ``os.execve`` had ended aisquare at once there, holding nothing.

So each waiting launcher records itself here for as long as it waits, and the upgrade and
uninstall plans read the records (``services.lifecycle.running_launchers``) and refuse,
naming each, as they do for an asq UI. ``<home>/launchers/`` is ``0700``; per launcher:

- ``<id>.json`` — what it launched and its pid (:class:`Launcher`).
- ``<id>.lock`` — locked by the launcher for as long as it waits. A pid is recycled; a lock
  is not: the OS drops it with the process, so a record whose lock is free is a launcher that
  ended without its cleanup (killed), and is not one.

A reader writes nothing: a plan's read leaves a dead launcher's files for the next launcher
that ends cleanly, or for ``uninstall --purge``. Every name it builds a path from is twelve
hex digits (:func:`valid_id`), never a record's content.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aisquare.core import atomic, locking, paths

FOLDER = "launchers"
_ID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class Launcher:
    """One waiting ``aisquare launch``: what it started, and its own pid."""

    pid: int
    agent: str
    started_at: datetime

    @property
    def label(self) -> str:
        """How a refusal names it: ``aisquare launch of claude (pid 4242)``."""
        return f"aisquare launch of {self.agent} (pid {self.pid})"


def registry_dir() -> Path:
    """``<home>/launchers``; made by the first launcher to wait, never by a reader."""
    return paths.aisquare_home() / FOLDER


def valid_id(text: str) -> bool:
    return _ID.fullmatch(text) is not None


def _lock_held(lock: Path) -> bool:
    """Whether a running process holds ``lock``. A file that cannot be opened or asked is
    none: what is not known to hold the install's files refuses nothing."""
    try:
        fd = locking.open_lock_file(lock, create=False)
    except OSError:
        return False
    try:
        locking.acquire(fd, wait_s=0)
    except TimeoutError:
        return True
    except OSError:
        return False
    else:
        with contextlib.suppress(OSError):
            locking.unlock(fd)
        return False
    finally:
        os.close(fd)


def _read(record: Path) -> Launcher | None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        return Launcher(
            pid=int(data["pid"]),
            agent=str(data["agent"]),
            started_at=datetime.fromisoformat(str(data["started_at"])),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None


def waiting_launchers() -> tuple[Launcher, ...]:
    """The launchers waiting on their agents on this home, oldest first. Read only."""
    folder = registry_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return ()
    found: list[Launcher] = []
    for name in names:
        stem, dot, suffix = name.partition(".")
        if not dot or suffix != "json" or not valid_id(stem):
            continue
        if not _lock_held(folder / f"{stem}.lock"):
            continue
        launcher = _read(folder / name)
        if launcher is not None:
            found.append(launcher)
    return tuple(sorted(found, key=lambda launcher: launcher.started_at))


@contextlib.contextmanager
def waiting(agent: str) -> Iterator[None]:
    """This process recorded as a launcher waiting on ``agent`` for the block. A home that
    cannot be written costs the record, never the launch: the session starts all the same,
    and an upgrade beside it is not refused for it."""
    record: Path | None = None
    lock: Path | None = None
    fd: int | None = None
    try:
        folder = registry_dir()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        stem = uuid4().hex[:12]
        lock, record = folder / f"{stem}.lock", folder / f"{stem}.json"
        fd = locking.open_lock_file(lock, mode=0o600)
        locking.acquire(fd, wait_s=0)
        body = {"pid": os.getpid(), "agent": agent, "started_at": datetime.now(UTC).isoformat()}
        atomic.write_replacing(
            record, json.dumps(body) + "\n", durable=False, owner_only=sys.platform != "win32"
        )
    except OSError:
        pass
    try:
        yield
    finally:
        # The record first, so no reader finds it beside a lock that is free; the lock's
        # file once closed, which Windows will not delete while it is open.
        if record is not None:
            with contextlib.suppress(OSError):
                record.unlink()
        if fd is not None:
            with contextlib.suppress(OSError):
                locking.unlock(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        if lock is not None:
            with contextlib.suppress(OSError):
                lock.unlink()
