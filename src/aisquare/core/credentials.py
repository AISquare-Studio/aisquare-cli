"""One reader and one writer for ``~/.aisquare/credentials``.

The file had two writers with two formats: ``init --api-key`` replaced the whole
file with a bare key string, and ``serve_token`` read JSON and fell back to
``{}`` on a decode error. Either order destroyed the other's value, silently —
the decode error read a bare key as "no data" rather than as "someone else owns
this file".

Two callers agreeing by careful editing is what produced that. A single
read-merge-write is what stops it recurring, which is why this module exists
rather than a matched pair of fixes.

JSON, because it is the format that can hold two names. A file already holding a
bare key is MIGRATED into ``api_key`` rather than discarded: every machine that
ran ``init --api-key`` before this change has one, and "unparseable therefore
empty" is the exact reading that lost data.

The writers replace the file by rename (``core.atomic.write_replacing``), under
an exclusive lock on ``credentials.lock`` beside it. Both used to rewrite it in
place with ``write_text``, which truncates first, and took no lock. A
``load_all`` in that window read ``""`` or half a document. The half document
then came back as a legacy bare key, the next ``store`` wrote it into
``api_key``, and the serve token and IAM session went with it. Two writers
racing each other also lost whichever key the first one added. Readers take no
lock: a rename leaves them the whole old file or the whole new one.

A rename publishes a NEW file, so what an in-place write got for free is done on
purpose: the temp is restricted to this account before the secrets are written
into it (``write_replacing(owner_only=True)``), and a file that exists but
cannot be read is refused rather than replaced by what this call alone knows.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.core.locking import lock_exclusive, unlock

#: Where a legacy bare-string file is migrated to.
API_KEY = "api_key"

LOCK_WAIT_S = 2.0
"""How long a writer waits for another's turn before giving up: a few writes' worth, not a hang."""

_LOCK_POLL_S = 0.01
_HELD = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})
"""The errnos that mean "another holder", POSIX ``flock``'s and Windows ``locking``'s, as in
``core.state_file``. Any other error from the primitive is raised at once."""


def load_all(*, strict: bool = False) -> dict[str, str]:
    """Everything stored, or ``{}``. Never raises by default — both callers are commands.

    A file that is not JSON is not assumed empty. If it holds a single
    non-blank line that does not open like JSON it is a pre-JSON API key and
    is reported as one; anything else genuinely carries nothing we can name.
    That limit is what keeps a torn document out of ``api_key``: an older
    build, or a crash, can leave ``{"api_key": "...`` cut short, and read as a
    bare key it went into the next ``store``'s write, so the key was replaced
    by a JSON fragment.

    ``strict`` re-raises the ``OSError`` of a file that exists but cannot be
    read, as ``core.state_file.read_state`` does. ``store`` reads through it:
    its write REPLACES the file, so "could not be read" taken as "holds
    nothing" published only this call's keys over everything else (review of
    the #65 fold, F1). The in-place write it replaced raised on such a file.
    """
    path = paths.credentials_path()
    if not path.exists():
        return {}
    try:
        # THROUGH THE RETRY, because on NTFS this read takes an `Access is
        # denied` of its own while another process holds the file for its
        # write — and a bare `except OSError` below reads that as "nothing
        # stored". `store()` is a read-merge-write of the whole file, so an
        # `aisquare` invocation racing another could erase its API key, serve
        # token or IAM session. That is verbatim
        # the loss this module was written to stop; its own header says
        # "'empty' is the exact reading that lost data".
        #
        # The `except` is unchanged and still means what it says — a file that
        # genuinely cannot be read is nothing we can name — but contention is
        # now resolved before it gets there rather than swallowed by it.
        raw = paths.despite_windows_contention(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    try:
        loaded: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _legacy(raw)
    if isinstance(loaded, dict):
        return {str(k): v for k, v in loaded.items() if isinstance(v, str)}
    return {}


def _legacy(raw: str) -> dict[str, str]:
    """A pre-JSON file's key: one non-blank line that could not be the start of a JSON value."""
    legacy = raw.strip()
    if not legacy or "\n" in legacy or legacy[0] in '{["':
        return {}
    return {API_KEY: legacy}


def store(*, replace: Sequence[str] = (), **values: str) -> tuple[dict[str, str], bool]:
    """Merge ``values`` into whatever is already there, owner-only.

    ``replace`` names keys to clear FIRST, in the same read-modify-write. It
    exists because ``drop(*KEYS)`` followed by ``store(**values)`` is the same
    write twice: two whole-file rewrites, two ``restrict_to_owner`` calls — and
    on Windows that is four ``icacls`` subprocesses per sign-in, each with a 15
    second timeout. ``store(**values, replace=KEYS)`` is one of each, and the
    caller gets the report the second write used to throw away.

    It is also the only way to clear a key whose new value is EMPTY. ``values``
    drops blanks on purpose — an omitted claim must not overwrite a good value
    with "" — so an expiry or email that is absent this time would otherwise
    survive from the previous session.

    Returns the merged result and whether the file could actually be restricted
    to this account. The second half is not decoration: on NTFS
    ``chmod(0o600)`` returns cleanly and protects nothing, so a caller that
    assumed success would promise a guard it does not have. The one writer
    reports both facts so neither caller has to ask a second question.

    The read, merge and write run under the writers' lock, so a concurrent
    ``store`` or ``drop`` cannot lose this one's keys or have its own lost.
    Raises ``OSError`` when the file cannot be read or written, or
    ``TimeoutError`` when another writer holds the lock past
    :data:`LOCK_WAIT_S`; the file is then left as it was.
    """
    paths.ensure_home()
    paths.warm_owner_restriction()  # whoami outside the lock, so only icacls runs inside it
    path = paths.credentials_path()
    with _locked(path):
        data = load_all(strict=True)
        for key in replace:
            data.pop(key, None)
        data.update({k: v for k, v in values.items() if v})
        return data, _write(path, data)


def drop(*keys: str) -> tuple[dict[str, str], bool]:
    """Remove ``keys`` from the file, keeping everything else.

    Signing out must not take the explainability key (or any future value)
    with it, and the file must stay valid JSON afterwards, so this is the same
    read-merge-write as ``store`` with a subtraction instead of an addition.
    A missing file is already the wanted state.

    Decided once without the lock, so a sign-out with nothing to drop creates
    no home and no lock file, then again under it, where the file is the
    truth: another writer may have landed in between.

    Returns what remains and whether the file could be restricted to this
    account, as ``store`` does; ``True`` when nothing was written. Dropping
    one key REWRITES the file that still holds the others, as a new file, so
    a restriction that failed leaves those secrets on the DACL the home hands
    down: the caller has something to say about it (review of the #65 fold,
    F2), where the in-place write this replaced kept the file's own.
    """
    data = load_all()
    if not any(key in data for key in keys):
        return data, True
    paths.ensure_home()
    paths.warm_owner_restriction()  # as in `store`
    path = paths.credentials_path()
    with _locked(path):
        data = load_all()
        remaining = {k: v for k, v in data.items() if k not in keys}
        if remaining == data:
            return remaining, True
        return remaining, _write(path, remaining)


def _write(path: Path, data: dict[str, str]) -> bool:
    """Replace the file with ``data`` by rename, owner-only; whether the restriction held.

    Written THROUGH a symlink, as the in-place ``write_text`` it replaces was:
    a rename over the link would swap the user's pointer for a plain file.
    ``owner_only`` creates the temp 0600 and restricts it to this account
    before the body is in it, so the secrets are never in a file readable more
    widely than the one they become: on POSIX by the bits, on NTFS by the DACL
    the rename carries over.
    """
    target = Path(os.path.realpath(path))
    return write_replacing(target, json.dumps(data, indent=2) + "\n", owner_only=True)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively for the block, waiting at most :data:`LOCK_WAIT_S`.

    Beside the path every process opens, not beside a symlink's target, like
    ``core.state_file``'s lock. Taken without blocking and polled, so a writer
    stalled inside its critical section costs a bounded wait, not a hang; only
    "held" is retried. The OS drops the lock if the process dies.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                lock_exclusive(fd)
                break
            except OSError as exc:
                if exc.errno not in _HELD:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        errno.ETIMEDOUT,
                        f"{lock_path} is held by another process (waited {LOCK_WAIT_S:g}s)",
                    ) from exc
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                unlock(fd)
    finally:
        os.close(fd)
