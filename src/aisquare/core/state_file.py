"""``~/.aisquare/state.json`` — the small runtime-state file, read and written in one place.

Three preferences share it: the project ``project switch`` pinned
(``active_project_id``, :mod:`aisquare.core.workspace`), the board's theme
(``board_theme``, ``cli.watch``) and the fleet UI's navigator width
(``sidebar_width``, ``cli.ui``). Each surface used to carry its own
read-modify-write of the file, and the copies had drifted: one caught only
``JSONDecodeError``, two raised ``AttributeError`` on a file whose top level
was not an object (``.get`` on a list), one wrote in place with no rename, and
two shared one fixed temp name — so one corrupt file produced a different
failure per surface, and two processes autosaving at once could truncate each
other's write. This is the one home; the surfaces keep their keys and call
here.

- :func:`read_state` never raises by default. A missing, empty, corrupt,
  undecodable or non-object file reads as ``{}``: every key is a preference,
  and a preference that cannot be read is one that is not set. With
  ``strict=True`` an UNREADABLE file (a permission error, a half-mounted share
  — not a missing one, and not a corrupt one) raises its ``OSError`` instead:
  the pin asks for that, because a pin that silently reads as absent
  retargets a command at the working directory.
- :func:`update_state` sets or removes ONE key and keeps every other — and
  writes nothing when the file already says so, decided UNDER THE LOCK, since a
  caller's belief about a key another process also writes is not the truth.
  The whole read-modify-write runs under an exclusive lock on a sibling lock file
  (``state.json.lock``; see :func:`_locked` for how it is opened and waited
  for), so the fleet UI's width save, ``board -w``'s theme save and ``project
  switch`` cannot lose each other's key — a per-process temp file alone only
  stops torn writes, not lost updates. It writes THROUGH a symlink
  (``os.path.realpath``, as ``core.config.save_config`` does) with the
  target's mode kept, by ``core.atomic.write_replacing`` (temp, fsync,
  rename, parent fsync). A file that exists but is not a JSON object is left
  exactly as it is and the update is REFUSED with :class:`StateUnwritableError`,
  which names what refused — the file, its lock, the read, the write — so a
  toast or an error line points at the right thing. An EMPTY file (blank, or
  the NULs a crash leaves) is not refused: there is nothing in it to protect.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.core.locking import lock_exclusive, unlock

LOCK_WAIT_S = 2.0
"""How long a writer waits for another's turn before giving up: a few writes' worth, not a hang."""

_LOCK_POLL_S = 0.01
_HELD = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})
"""The errnos that mean "another holder": POSIX ``flock``'s and Windows ``locking``'s. Any other
``OSError`` from the primitive (``EBADF``, ``ENOLCK``, ``EOPNOTSUPP``, ``EIO``) will not clear on a
retry and is refused at once."""
_BLANK = " \t\r\n\x00"
"""What an empty file may hold: whitespace, or the NULs a crash leaves when the size reached the
disk and the data did not."""


class StateUnwritableError(Exception):
    """``state.json`` refused an update and was left as it was; the message names what refused.

    A policy refusal (the file is not a JSON object) or a failed step (its
    lock could not be opened, taken or was held too long; it could not be read
    or written), never a disguised I/O error from somewhere else: callers that
    must report it (``project switch``) catch THIS, so an unrelated
    ``PermissionError`` from the store's own directories is not mislabelled as
    the state file's.
    """


def read_state(*, strict: bool = False) -> dict[str, object]:
    """The file's contents; ``{}`` when it is missing, empty, corrupt or not a JSON object.

    ``strict`` re-raises the ``OSError`` of a file that exists but cannot be
    read, so "the file says nothing" and "the file could not be read" stay
    distinct for the caller that needs them to (the pin). A file that does not
    DECODE is corrupt, not unreadable, and reads as ``{}`` either way.
    """
    try:
        raw = paths.state_path().read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    data = _parse(raw)
    return dict(data) if isinstance(data, dict) else {}


def update_state(key: str, value: object) -> None:
    """Set ``key`` to ``value`` — or, with ``None``, drop it — keeping every other key.

    Raises :class:`StateUnwritableError`, and nothing else, when the file was
    left as it was: it exists but is not a JSON object, its lock could not be
    opened or taken, it could not be read or written, or ``value`` is not JSON.
    A file that already says ``key: value`` (or already lacks ``key``) is not
    rewritten — the decision is taken here, under the lock, where the file's
    contents are the truth. Temp files an earlier process left behind (a quit
    that ran out of time mid-write) are swept while the lock is held.
    """
    path = paths.state_path()
    try:
        paths.ensure_home()
    except OSError as exc:
        raise StateUnwritableError(f"{path.parent} could not be created: {exc}") from exc
    with _locked(path):
        target = Path(os.path.realpath(path))  # through a symlink, never over it
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            raw = b""
        except OSError as exc:
            raise StateUnwritableError(f"{path} could not be read: {exc}") from exc
        data = _parse(raw)
        if not isinstance(data, dict):
            raise StateUnwritableError(f"{path} is not a JSON object")
        _sweep_stale_temps(target)
        if (key not in data) if value is None else (key in data and data[key] == value):
            return  # the file already says so
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        try:
            body = json.dumps(data, indent=2) + "\n"
        except (TypeError, ValueError, RecursionError) as exc:
            raise StateUnwritableError(
                f"a {type(value).__name__} is not JSON and cannot be stored in {path}"
            ) from exc
        try:
            write_replacing(target, body)
        except OSError as exc:
            raise StateUnwritableError(f"{path} could not be written: {exc}") from exc


def _parse(raw: bytes) -> object:
    """The JSON in ``raw``: ``{}`` for a blank body, ``None`` for one that is not JSON at all.

    Decoded as ``utf-8-sig`` so a BOM (Notepad's) in front of a valid object
    is not a refusal; bytes that do not decode are corrupt, like bytes that
    do not parse. ``RecursionError`` is neither ``OSError`` nor ``ValueError``:
    a pathologically nested file used to escape both functions from the fleet
    UI's mount, the exact place this module was written to make safe.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if not text.strip(_BLANK):
        return {}  # nothing in it to protect — and a crash is what leaves one
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


_STALE_TEMP_S = 60.0


def _sweep_stale_temps(target: Path) -> None:
    """Remove ``.state.json.<pid>.<hex>.tmp`` files older than a minute — another process's,
    left when its quit ran out of time mid-write. Under the lock, so nobody is writing one."""
    for temp in target.parent.glob(f".{target.name}.*.tmp"):
        with contextlib.suppress(OSError):
            if time.time() - temp.stat().st_mtime > _STALE_TEMP_S:
                temp.unlink()


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively for the block, waiting at most :data:`LOCK_WAIT_S`.

    Beside the path every process opens (``~/.aisquare/state.json.lock``), not
    beside a symlink's target: a dotfiles repo should not gain a lock file.
    Opened for WRITING first — on NFS an exclusive ``flock`` is emulated with a
    byte-range lock and needs a descriptor open for writing — and read-only
    when that is refused, which is enough everywhere else: a lock file left
    behind by another user (``sudo aisquare …``, created ``0o644`` here for
    that reason) still serves on a local disk instead of refusing every update
    while blaming ``state.json``. Taken without blocking and polled, so a
    holder stalled inside its critical section costs a bounded wait, not a
    hang; only "held" is retried, and every other error from the primitive is
    refused at once with its own message. The OS drops the lock if the process
    dies.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except PermissionError as denied:
        try:
            fd = os.open(lock_path, os.O_RDONLY)
        except FileNotFoundError:
            # A home we cannot write to and no lock yet: the refusal is the
            # permission problem, not a missing file the fallback could not create.
            raise StateUnwritableError(f"{lock_path} could not be opened: {denied}") from denied
        except OSError as exc:
            raise StateUnwritableError(f"{lock_path} could not be opened: {exc}") from exc
    except OSError as exc:
        raise StateUnwritableError(f"{lock_path} could not be opened: {exc}") from exc
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                lock_exclusive(fd)
                break
            except OSError as exc:
                if exc.errno not in _HELD:
                    raise StateUnwritableError(f"{lock_path} could not be locked: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise StateUnwritableError(
                        f"{lock_path} is held by another process (waited {LOCK_WAIT_S:g}s)"
                    ) from exc
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                unlock(fd)
    finally:
        os.close(fd)
