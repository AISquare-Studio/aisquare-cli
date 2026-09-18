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

- :func:`read_state` never raises by default. A missing, empty, corrupt or
  non-object file reads as ``{}``: every key is a preference, and a preference
  that cannot be read is one that is not set. With ``strict=True`` an
  UNREADABLE file (a permission error, a half-mounted share — not a missing
  one) raises its ``OSError`` instead: the pin asks for that, because a pin
  that silently reads as absent retargets a command at the working directory.
- :func:`update_state` sets or removes ONE key and keeps every other. The whole
  read-modify-write runs under an exclusive lock on a sibling lock file
  (``state.json.lock``, opened read-only so a lock left by another user still
  serves, taken without blocking and waited for at most :data:`LOCK_WAIT_S`,
  released by the OS if the process dies), so the fleet UI's width debounce,
  ``board -w``'s theme autosave and ``project switch`` cannot lose each
  other's key — a per-process temp file alone only stops torn writes, not lost
  updates. It writes THROUGH a symlink (``os.path.realpath``, as
  ``core.config.save_config`` does) with the target's mode kept, to a sibling
  temp file named for this process, fsyncs it (a crash after the rename must
  not publish an empty file) and ``os.replace``\\ s it over the target. A file
  that exists but is not a JSON object is left exactly as it is and the update
  is REFUSED with :class:`StateUnwritableError`, which names what refused —
  the file, its lock, or the write — so a toast or an error line can point at
  the right thing. An EMPTY file is not refused: there is nothing in it to
  protect, and a crash is what leaves one.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from aisquare.core import paths
from aisquare.core.locking import lock_exclusive, unlock

LOCK_WAIT_S = 2.0
"""How long a writer waits for another's turn before giving up: a few writes' worth, not a hang."""

_LOCK_POLL_S = 0.01


class StateUnwritableError(Exception):
    """``state.json`` refused an update and was left as it was; the message names what refused.

    A policy refusal (the file is not a JSON object) or a failed step (its
    lock could not be taken; it could not be read or written), never a
    disguised I/O error from somewhere else: callers that must report it
    (``project switch``) catch THIS, so an unrelated ``PermissionError`` from
    the store's own directories is not mislabelled as the state file's.
    """


def read_state(*, strict: bool = False) -> dict[str, object]:
    """The file's contents; ``{}`` when it is missing, empty, corrupt or not a JSON object.

    ``strict`` re-raises the ``OSError`` of a file that exists but cannot be
    read, so "the file says nothing" and "the file could not be read" stay
    distinct for the caller that needs them to (the pin).
    """
    try:
        text = paths.state_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    data = _parse(text)
    return dict(data) if isinstance(data, dict) else {}


def update_state(key: str, value: object) -> None:
    """Set ``key`` to ``value`` — or, with ``None``, drop it — keeping every other key.

    Raises :class:`StateUnwritableError`, and nothing else, when the file was
    left as it was: it exists but is not a JSON object, its lock could not be
    taken, it could not be read or written, or ``value`` is not JSON.
    """
    path = paths.state_path()
    try:
        paths.ensure_home()
    except OSError as exc:
        raise StateUnwritableError(f"{path.parent} could not be created: {exc}") from exc
    with _locked(path):
        target = Path(os.path.realpath(path))  # through a symlink, never over it
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            raise StateUnwritableError(f"{path} could not be read: {exc}") from exc
        data = _parse(text)
        if not isinstance(data, dict):
            raise StateUnwritableError(f"{path} is not a JSON object")
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
        _replace(path, target, body)


def _parse(text: str) -> object:
    """The JSON in ``text``: ``{}`` for a blank body, ``None`` for one that is not JSON at all.

    ``RecursionError`` is neither ``OSError`` nor ``ValueError``: a
    pathologically nested file used to escape both functions from the fleet
    UI's mount, the exact place this module was written to make safe.
    """
    if not text.strip():
        return {}  # nothing in it to protect — and a crash is what leaves one
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively for the block, waiting at most :data:`LOCK_WAIT_S`.

    Beside the path every process opens (``~/.aisquare/state.json.lock``), not
    beside a symlink's target: a dotfiles repo should not gain a lock file.
    Opened READ-ONLY (created if missing): neither ``flock`` nor Windows'
    byte-range lock needs write access, so a lock file left behind by another
    user, or restored read-only, still serves instead of refusing every update
    while blaming ``state.json``. The OS drops the lock if the process dies.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        raise StateUnwritableError(f"{lock_path} could not be opened: {exc}") from exc
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                lock_exclusive(fd)
                break
            except OSError as exc:
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


def _replace(path: Path, target: Path, body: str) -> None:
    """Write ``body`` to ``target`` in one step: our own temp file, fsynced, then renamed over it.

    ``core.config.save_config``'s recipe. Without the fsync a crash after the
    rename can publish a file whose contents never reached the disk — on XFS
    an empty one — and ``path`` is the name the refusal quotes.
    """
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(FileNotFoundError):
            os.chmod(temporary, target.stat().st_mode & 0o777)  # a chmod 600 stays a 600
        os.replace(temporary, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise StateUnwritableError(f"{path} could not be written: {exc}") from exc
