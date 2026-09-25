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

On NTFS the rename is refused while any other process has the target open,
even only to read it, so it retries through
``paths.despite_windows_contention``: here, not at each caller, so every file
written with this recipe keeps the retry ``save_config`` was given for it.

A file that holds secrets (``core.credentials``) is written ``owner_only``: its
temp is restricted to this account while it is still empty, because on NTFS a
new file carries the DACL it inherits from its directory, and restricting the
target after the rename published the secrets under that DACL first.

Raises ``OSError`` for what the caller has to explain (a directory it cannot
write, a disk that is full); the caller decides whether that is fatal.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

from aisquare.core import paths


def write_replacing(
    target: Path,
    body: str,
    *,
    keep_mode: bool = True,
    durable: bool = True,
    owner_only: bool = False,
) -> bool:
    """Replace ``target``'s contents with ``body`` in one step.

    ``keep_mode`` copies an existing target's permission bits onto the new
    file (a ``chmod 600`` stays a 600); the temp file is otherwise created at
    the umask default. ``durable`` fsyncs the temp before the rename and the
    directory after it — the two steps that make the write survive a crash,
    and the two that cost on a busy or network disk; a cache whose loss is a
    refetch passes ``False`` and keeps the atomicity alone.

    The kept bits are the temp's from its creation, narrowed by the umask
    like any new file's, and set exactly once the body is in. Created at the
    umask default and given them afterwards, a 600 file's contents sat in a
    644 temp for the whole write and fsync, and a kill before the chmod left
    them there (review of the #167 fold, F7).

    ``owner_only`` is for a file of secrets. The temp is created 0600 whatever
    the target's bits are, and ``paths.restrict_to_owner`` narrows it while it
    is still EMPTY, before the body is written. On NTFS the bits protect nothing
    and the temp starts with the DACL its directory hands down (for a home on
    ``D:\\`` that includes Users); a rename keeps a file's security descriptor,
    so the target is owner-only from the moment it has the name. Restricted
    after the rename instead, the secrets were published under the inherited
    DACL for an ``icacls`` call's width, and for good when that call failed,
    where the in-place write it replaced had kept the file's owner-only DACL
    (review of the #65 fold, F2). Returns whether that restriction held, so the
    caller can say so; ``True`` when none was asked for.

    The same write in two steps, for a caller that decides the body under a
    lock, is :func:`replacement`.
    """
    with replacement(
        target, keep_mode=keep_mode, durable=durable, owner_only=owner_only
    ) as pending:
        pending.publish(body)
    return pending.restricted


class Replacement:
    """A temp beside ``target``, created (and restricted, if asked) but not yet written.

    Made by :func:`replacement`. ``restricted`` is whether ``owner_only``'s
    restriction held, ``True`` when none was asked for; ``published``, whether
    :meth:`publish` has renamed the temp over the target.
    """

    def __init__(self, target: Path, temporary: Path, kept: int | None, durable: bool) -> None:
        self.target = target
        self.restricted = True
        self.published = False
        self._temporary = temporary
        self._kept = kept
        self._durable = durable

    def publish(self, body: str) -> None:
        """Write ``body`` into the temp and rename it over the target, once.

        A second call is refused: the temp has become the target, and the file
        it would write is a new one that no restriction was applied to.
        """
        if self.published:
            raise RuntimeError(f"{self.target} was already replaced by this temp")
        with self._temporary.open("w", encoding="utf-8") as handle:
            handle.write(body)
            if self._durable:
                handle.flush()
                os.fsync(handle.fileno())
        if self._kept is not None:
            # Exactly the target's: the umask may have narrowed them.
            os.chmod(self._temporary, self._kept)
        paths.despite_windows_contention(lambda: os.replace(self._temporary, self.target))
        self.published = True
        if self._durable:
            _sync_directory(self.target.parent)


@contextlib.contextmanager
def replacement(
    target: Path,
    *,
    keep_mode: bool = True,
    durable: bool = True,
    owner_only: bool = False,
) -> Iterator[Replacement]:
    """:func:`write_replacing` in two steps: the temp on entry, the body at ``publish``.

    For a caller that decides the body under a lock, as ``core.credentials``'
    writers do. With ``owner_only`` the restriction runs on entry, and on
    Windows that is an ``icacls`` subprocess (and, on a process's first, a
    ``whoami``) with a 15 second timeout each, while a writer waiting for that
    lock gives up after two: a slow start of either tool (an antivirus scan, a
    loaded runner) failed a concurrent ``login`` or ``serve`` with a
    ``TimeoutError``. The temp's name is this process's own and unique, and it
    is empty, so no lock has to cover its restriction; entered before the lock
    is taken, only the write, the fsync and the rename are left inside it
    (review of the #65 fold, round 2, F1). The options mean what they mean for
    :func:`write_replacing`.

    Leaving without a ``publish`` (nothing to write after all, a refusal, an
    exception, ``KeyboardInterrupt`` included) removes the temp.
    """
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    kept: int | None = None
    if owner_only:
        kept = stat.S_IRUSR | stat.S_IWUSR
    elif keep_mode:
        with contextlib.suppress(FileNotFoundError):
            kept = target.stat().st_mode & 0o777
    pending = Replacement(target, temporary, kept, durable)
    try:
        # Created empty with those bits, plus its owner's write until the body is
        # in (a read-only target's temp is written too), then opened by path.
        mode = 0o666 if kept is None else kept | stat.S_IWUSR
        os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode))
        if owner_only:
            pending.restricted = paths.restrict_to_owner(temporary)  # empty: nothing to expose yet
        yield pending
    finally:
        if not pending.published:
            with contextlib.suppress(OSError):
                os.chmod(temporary, 0o600)  # Windows will not delete a read-only file
            with contextlib.suppress(OSError):  # its own block: a filesystem that refuses chmod
                temporary.unlink()  # (vfat, some CIFS) must not keep the temp too


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
