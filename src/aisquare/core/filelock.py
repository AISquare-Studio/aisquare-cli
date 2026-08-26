"""One exclusive interprocess lock primitive, per platform.

Extracted from ``core/brain.py``, which had the only copy. A second copy was
about to be written for the explainability proxy lifecycle, and platform locking
is exactly the code that should exist once: the Windows and POSIX calls have
different shapes, different failure modes, and only one of them is exercised on
any given machine, so a divergence between two copies is invisible until it is
someone's Windows bug report.

``fcntl`` is POSIX-only and ``msvcrt`` is Windows-only, so the import is branched
on ``sys.platform`` rather than wrapped in ``try``/``except ImportError``: mypy
narrows on ``sys.platform`` and type-checks only the branch that is real for the
platform it runs on, which a ``try`` block would not do.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if sys.platform == "win32":
    import msvcrt

    def lock_exclusive(handle: IO[str]) -> None:
        """Take the lock without blocking; raise ``OSError`` if held."""
        # Windows byte-range locks are per handle and mandatory, so locking
        # one byte at offset 0 is exclusive across processes just like flock.
        # The region may sit past EOF, which is what keeps this working on the
        # empty lock file.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def unlock(handle: IO[str]) -> None:
        """Release the lock taken by :func:`lock_exclusive`."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock_exclusive(handle: IO[str]) -> None:
        """Take the lock without blocking; raise ``OSError`` if held."""
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(handle: IO[str]) -> None:
        """Release the lock taken by :func:`lock_exclusive`."""
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def held(path: Path, *, wait_s: float) -> Iterator[bool]:
    """Hold an exclusive lock on ``path``; yield whether it was won.

    ``wait_s == 0`` is a try-lock; otherwise the lock is polled for up to
    ``wait_s`` seconds. Yielding False rather than raising leaves the decision
    with the caller: a background drain skips, while a lifecycle command that
    must not run twice reports why it did not run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                lock_exclusive(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        try:
            yield True
        finally:
            unlock(handle)
    finally:
        handle.close()
