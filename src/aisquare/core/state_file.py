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

- :func:`read_state` never raises by default. A missing, unreadable or
  non-object file reads as ``{}``: every key is a preference, and a preference
  that cannot be read is one that is not set. With ``strict=True`` an
  UNREADABLE file (a permission error, a half-mounted share — not a missing
  one) raises its ``OSError`` instead: the pin asks for that, because a pin
  that silently reads as absent retargets a command at the working directory.
- :func:`update_state` sets or removes ONE key and keeps every other. The whole
  read-modify-write runs under an exclusive lock on a sibling lock file
  (``state.json.lock``, released by the OS if the process dies), so the fleet
  UI's width debounce, ``board -w``'s theme autosave and ``project switch``
  cannot lose each other's key — the per-process temp file alone only stopped
  torn writes, not lost updates. It writes THROUGH a symlink (``os.path.realpath``,
  as ``core.config.save_config`` does) with the target's mode kept, to a sibling
  temp file named for this process, and ``os.replace``\\ s it over the target.
  A file that exists but is not a JSON object is left exactly as it is and the
  update is refused: the keys in it are a user's, and destroying them to record
  a width is worse than not recording it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from aisquare.core import paths


class StateUnwritableError(Exception):
    """``state.json`` refused an update and was left as it was.

    It is not a JSON object, or it could not be read or written. A policy
    refusal, not an I/O error: callers that must report it (``project switch``)
    catch THIS, so an unrelated ``PermissionError`` from the store's own
    directories is never mislabelled as the state file's.
    """


# The exclusive-lock primitive, per platform — the shape ``core.brain`` uses,
# blocking here because a second writer should wait a few milliseconds, not
# lose. ``fcntl`` is POSIX-only and ``msvcrt`` Windows-only, so the import is
# branched on ``sys.platform`` (mypy narrows on it; a ``try`` would not).
if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        # One byte at offset 0 is exclusive across processes, like flock; the
        # region may sit past EOF, which is what keeps this working on an empty
        # lock file. ``LK_LOCK`` retries for about ten seconds, then raises.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


def read_state(*, strict: bool = False) -> dict[str, object]:
    """The file's contents; ``{}`` when it is missing, corrupt or not a JSON object.

    ``strict`` re-raises the ``OSError`` of a file that exists but cannot be
    read, so "the file says nothing" and "the file could not be read" stay
    distinct for the caller that needs them to (the pin).
    """
    try:
        data = json.loads(paths.state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    except (ValueError, RecursionError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def update_state(key: str, value: object) -> bool:
    """Set ``key`` to ``value`` — or, with ``None``, drop it — keeping every other key.

    ``True`` when the file now says so. ``False`` when it was left as it was:
    it exists but is not a JSON object, it could not be read or written, or
    ``value`` is not JSON. Never raises.
    """
    path = paths.state_path()
    try:
        paths.ensure_home()
        with _locked(path):
            target = Path(os.path.realpath(path))  # through a symlink, never over it
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {}
            except (ValueError, RecursionError):
                return False
            if not isinstance(data, dict):
                return False
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            return _replace(target, json.dumps(data, indent=2) + "\n")
    except (OSError, TypeError, ValueError, RecursionError):
        return False


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively for the block; the OS releases it if the process dies.

    Beside the path every process opens (``~/.aisquare/state.json.lock``), not
    beside a symlink's target: a dotfiles repo should not gain a lock file.
    """
    with path.with_name(f"{path.name}.lock").open("a", encoding="utf-8") as handle:
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)


def _replace(target: Path, body: str) -> bool:
    """Write ``body`` to ``target`` in one step: this process's own temp file, then a rename."""
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(body, encoding="utf-8")
        with contextlib.suppress(FileNotFoundError):
            os.chmod(temporary, target.stat().st_mode & 0o777)  # a chmod 600 stays a 600
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        return False
    return True
