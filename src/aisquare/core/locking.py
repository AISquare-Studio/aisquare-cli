"""The exclusive-lock primitive, per platform — one copy, for every lock FILE in the home.

``core.brain`` guards a brain's directory with it and ``core.state_file``
guards ``state.json``'s read-modify-write. Both take the lock on a small file
that exists only to be locked, never on the data file itself: the data file
is replaced by rename, so a lock on its inode would not exclude a writer that
opened the new one. :func:`holding` is the bounded wait for a caller that words
its own refusals (the fleet's lock over a repo's branches).

Non-blocking on purpose. ``flock`` with ``LOCK_EX`` waits forever and
Windows' ``LK_LOCK`` for about ten seconds, so a holder stalled inside its
critical section — a ``project switch`` stopped with ``^Z`` mid-write — would
hold every other writer with it. The primitive returns at once, raising
``OSError`` when the lock is held, and each caller bounds its own wait. Two
things it cannot promise: on NFS an exclusive ``flock`` is emulated with a
byte-range lock on the whole file and needs a descriptor OPEN FOR WRITING
(``flock(2)``, "NFS details"; the client answers ``EBADF`` otherwise), so a
caller opens its lock file for writing when it can; and there the lock is a
synchronous RPC, so an unreachable lock manager blocks inside the call however
non-blocking the flag — which is why the TUI's saves run on a worker thread
(``cli.ui.autosave``) rather than trusting any bound.

``fcntl`` is POSIX-only and ``msvcrt`` is Windows-only, so the import is
branched on ``sys.platform`` rather than wrapped in ``try``/``except
ImportError``: mypy narrows on ``sys.platform`` and type-checks only the
branch that is real for the platform it runs on, which a ``try`` block would
not do.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def lock_exclusive(fd: int) -> None:
        """Take the lock without blocking; raise ``OSError`` if it is held."""
        # Windows byte-range locks are per handle and mandatory, so locking
        # one byte at offset 0 is exclusive across processes just like flock.
        # The region may sit past EOF, which is what keeps this working on an
        # empty lock file.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def unlock(fd: int) -> None:
        """Release the lock taken by :func:`lock_exclusive`."""
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock_exclusive(fd: int) -> None:
        """Take the lock without blocking; raise ``OSError`` if it is held."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fd: int) -> None:
        """Release the lock taken by :func:`lock_exclusive`."""
        fcntl.flock(fd, fcntl.LOCK_UN)


_HELD = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})
"""The errnos that mean "another holder": POSIX ``flock``'s and Windows ``locking``'s."""

_POLL_S = 0.01


@contextlib.contextmanager
def holding(path: Path, *, wait_s: float) -> Iterator[None]:
    """Hold the lock file ``path`` exclusively for the block, polling for at most ``wait_s``.

    For a caller that words its own refusals: raises :class:`TimeoutError` when another
    holder keeps the lock past ``wait_s`` (``0`` asks once), and the ``OSError`` that
    opening or locking raised otherwise, at once. Opened for writing, created ``0o644``,
    and read-only when writing is refused (a lock file another user left behind), as
    ``core.state_file`` opens its own. The OS drops the lock if the process dies.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except PermissionError:
        fd = os.open(path, os.O_RDONLY)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                lock_exclusive(fd)
                break
            except OSError as exc:
                if exc.errno not in _HELD:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{path} is held by another process") from exc
                time.sleep(_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                unlock(fd)
    finally:
        os.close(fd)
