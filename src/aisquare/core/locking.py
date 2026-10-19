"""The exclusive-lock primitive, per platform — one copy, for every lock FILE in the home.

``core.brain`` guards a brain's directory with it, ``core.state_file``
guards ``state.json``'s read-modify-write and ``core.credentials`` the
credentials file's; Remote guards ``remote.json``'s and claims its home with it
(``services.remote_server``). Each takes the lock on a small file that exists
only to be locked, never on the data file itself: the data file is replaced by
rename, so a lock on its inode would not exclude a writer that opened the new one.
:func:`holding` is the one bounded wait, for every caller, and each words its
own refusals (the fleet's lock over a repo's branches too); :func:`open_lock_file`
and :func:`held` are its two steps, for a caller that words them apart, and
:func:`acquire` is the wait itself, for a lock held past any one block.

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
"""The errnos that mean "another holder": POSIX ``flock``'s and Windows ``locking``'s. Any other
``OSError`` from the primitive (``EBADF``, ``ENOLCK``, ``EOPNOTSUPP``, ``EIO``) will not clear on a
retry and is raised at once."""

_POLL_S = 0.01


def open_lock_file(path: Path, *, mode: int = 0o644, create: bool = True) -> int:
    """``path`` opened to be locked, as a descriptor the caller closes.

    For writing first — on NFS an exclusive ``flock`` is emulated with a byte-range lock and
    needs a descriptor open for writing — created ``mode``; read-only when writing is
    refused, which serves everywhere else (a lock file another user left behind, ``0o644``
    for that reason). With no lock file yet and writing refused, the read-only retry has no
    file to open, and what is raised is the permission problem, not its ``ENOENT``.

    ``create=False`` for a reader that asks whether another process holds the lock and may
    make nothing: none there raises ``FileNotFoundError``. The same rule otherwise, so the
    one that asks opens the file the holder could (review of #259, round 19).
    """
    try:
        return os.open(path, os.O_RDWR | (os.O_CREAT if create else 0), mode)
    except PermissionError as denied:
        try:
            return os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            raise denied from None


def acquire(fd: int, *, wait_s: float) -> None:
    """Lock the open lock file ``fd`` exclusively, polling for at most ``wait_s``; the caller
    lets it go (:func:`unlock`, or closing ``fd``).

    THE poll loop: only "held" (:data:`_HELD`) is retried, so a holder stalled inside its
    critical section costs a bounded wait, not a hang. Raises :class:`TimeoutError` when
    another holder keeps the lock past ``wait_s`` (``0`` asks once), and any other
    ``OSError`` from the primitive at once. The OS drops the lock if the process dies.
    :func:`held` is it for a block; Remote's claim of its home holds it for as long as it
    serves, and asks with it whether another process does.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            lock_exclusive(fd)
            return
        except OSError as exc:
            if exc.errno not in _HELD:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(errno.ETIMEDOUT, "held by another process") from exc
            time.sleep(_POLL_S)


@contextlib.contextmanager
def held(fd: int, *, wait_s: float) -> Iterator[None]:
    """Lock the open lock file ``fd`` exclusively for the block, polling for at most ``wait_s``
    (:func:`acquire`, which says what it raises)."""
    acquire(fd, wait_s=wait_s)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            unlock(fd)


@contextlib.contextmanager
def holding(path: Path, *, wait_s: float, mode: int = 0o644) -> Iterator[None]:
    """Hold the lock file ``path`` exclusively for the block, polling for at most ``wait_s``.

    :func:`open_lock_file` (created ``mode``), then :func:`held`: raises
    :class:`TimeoutError` when another holder keeps the lock past ``wait_s``, and the
    ``OSError`` that opening or locking raised otherwise, at once. A caller words its own
    refusals around it, and enters it on a :class:`contextlib.ExitStack` to do so, so that
    nothing its block raises is taken for a refusal of the lock.
    """
    fd = open_lock_file(path, mode=mode)
    try:
        with held(fd, wait_s=wait_s):
            yield
    finally:
        os.close(fd)
