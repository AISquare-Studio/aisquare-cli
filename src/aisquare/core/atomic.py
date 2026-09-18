"""One durable replace-by-rename, for every small file the home keeps.

``core.config.save_config`` wrote the recipe down first and measured it:
write a SIBLING temp file (``os.replace`` is only atomic within one
filesystem), flush and ``fsync`` it (a crash after the rename must not
publish a file whose contents never reached the disk — on XFS an empty one),
rename it over the target, then ``fsync`` the parent directory so the rename
itself is durable (fail-open: a parent that cannot be synced costs
durability, never the write). Remove the temp on ANY failure, ``KeyboardInterrupt``
included, so a Ctrl-C mid-write leaves nothing beside the file an operator
reads. ``core.state_file`` and ``services.ci_descriptor`` each had their own
copy with parts missing; this is the one they share.

Raises ``OSError`` for what the caller has to explain (a directory it cannot
write, a disk that is full); the caller decides whether that is fatal.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from uuid import uuid4


def write_replacing(
    target: Path, body: str, *, keep_mode: bool = True, durable: bool = True
) -> None:
    """Replace ``target``'s contents with ``body`` in one step.

    ``keep_mode`` copies an existing target's permission bits onto the new
    file (a ``chmod 600`` stays a 600); the temp file is otherwise created at
    the umask default. ``durable`` fsyncs the temp before the rename and the
    directory after it — the two steps that make the write survive a crash,
    and the two that cost on a busy or network disk; a cache whose loss is a
    refetch passes ``False`` and keeps the atomicity alone.
    """
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(body)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        if keep_mode:
            with contextlib.suppress(FileNotFoundError):
                os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)  # Windows will not delete a read-only file
            temporary.unlink()
        raise
    if durable:
        _sync_directory(target.parent)


def _sync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable; fail open, the write itself has already landed."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
