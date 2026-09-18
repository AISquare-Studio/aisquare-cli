"""The exclusive-lock primitive, per platform — one copy, for every lock FILE in the home.

``core.brain`` guards a brain's directory with it and ``core.state_file``
guards ``state.json``'s read-modify-write. Both take the lock on a small file
that exists only to be locked, never on the data file itself: the data file
is replaced by rename, so a lock on its inode would not exclude a writer that
opened the new one.

Non-blocking on purpose. ``flock`` with ``LOCK_EX`` waits forever and
Windows' ``LK_LOCK`` for about ten seconds; both callers run on a UI thread
at times (the fleet UI's save timer, the board's theme autosave), where a
holder stalled inside its critical section — a ``project switch`` stopped
with ``^Z`` mid-write, a home on a share whose lock manager is unreachable —
would freeze the whole TUI. So the primitive returns at once, raising
``OSError`` when the lock is held, and each caller bounds its own wait.

``fcntl`` is POSIX-only and ``msvcrt`` is Windows-only, so the import is
branched on ``sys.platform`` rather than wrapped in ``try``/``except
ImportError``: mypy narrows on ``sys.platform`` and type-checks only the
branch that is real for the platform it runs on, which a ``try`` block would
not do.
"""

from __future__ import annotations

import os
import sys

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
