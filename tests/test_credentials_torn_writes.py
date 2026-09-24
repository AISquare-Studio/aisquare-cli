"""``~/.aisquare/credentials`` is replaced by rename under a lock, never rewritten in place.

``store`` and ``drop`` used to ``write_text`` the whole file, which truncates it
first, and took no lock. A ``load_all`` in that window read ``""`` or half a
document, and the legacy branch, meant for a pre-JSON bare key, returned the
half document AS the API key. The next ``store`` wrote it back: the key replaced
by a JSON fragment, the serve token and the IAM session gone. Two writers at
once also lost whichever key the first one added.

No fixture here is credential-shaped: values are assembled from obviously
synthetic parts, as in ``test_credentials_single_format.py``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from aisquare.core import credentials, paths
from aisquare.core.atomic import Replacement
from aisquare.core.locking import lock_exclusive, unlock
from tests.fsperms import can_deny_reads, can_symlink

_KEY = "-".join(["not", "a", "real", "key"])
_TOKEN = "-".join(["not", "a", "real", "token"])


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="NTFS refuses a rename over a file another handle has open; the retry covers that",
)
def test_a_reader_mid_read_keeps_the_whole_old_document(isolated_home: Path) -> None:
    """The torn read itself. In place, the store truncated the file under the reader, which
    went on reading the NEW bytes from its old offset. Replaced by rename, the reader keeps
    the file it opened, whole."""
    credentials.store(api_key=_KEY, serve_token=_TOKEN)
    with paths.credentials_path().open(encoding="utf-8") as reader:
        head = reader.read(5)
        credentials.store(iam_token="t")
        rest = reader.read()
    assert json.loads(head + rest) == {"api_key": _KEY, "serve_token": _TOKEN}
    assert credentials.load_all() == {"api_key": _KEY, "serve_token": _TOKEN, "iam_token": "t"}


@pytest.mark.parametrize(
    "torn",
    [
        '{\n  "api_key": "' + _KEY[:6],
        "{",
        '["api_key", ',
        '"' + _KEY,
        "first line\nsecond line\n",
    ],
    ids=["cut-object", "brace", "cut-array", "cut-string", "two-lines"],
)
def test_a_torn_or_multi_line_file_is_not_migrated_as_a_bare_key(
    isolated_home: Path, torn: str
) -> None:
    """Only one non-blank line that could not open a JSON value is a pre-JSON key. Anything
    else read as one went into ``api_key`` on the next store."""
    paths.ensure_home()
    paths.credentials_path().write_text(torn, encoding="utf-8")
    assert credentials.load_all() == {}
    stored, _ = credentials.store(serve_token=_TOKEN)
    assert stored == {"serve_token": _TOKEN}
    assert credentials.load_all() == {"serve_token": _TOKEN}


def test_a_bare_key_with_its_trailing_newline_is_still_migrated(isolated_home: Path) -> None:
    """The control: the limit must not cost the machines that ran ``init --api-key`` before
    the file was JSON."""
    paths.ensure_home()
    paths.credentials_path().write_text(f"  {_KEY}\n", encoding="utf-8")
    assert credentials.load_all() == {"api_key": _KEY}


def test_two_writers_cannot_lose_each_others_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the lock: A reads {api_key}, B reads {api_key}, A writes {api_key, serve_token},
    B writes {api_key, iam_token}: the serve token is gone and both returned. ``init``,
    ``serve`` and ``login`` all reach this path."""
    credentials.store(api_key=_KEY)
    inside, go = threading.Event(), threading.Event()
    paused = False
    real_publish = Replacement.publish

    def pausing_publish(pending: Replacement, body: str) -> None:
        nonlocal paused
        if not paused:  # the first writer, mid-critical-section, waits for the test's go
            paused = True
            inside.set()
            assert go.wait(5), "the test never let the first writer finish"
        real_publish(pending, body)

    monkeypatch.setattr(Replacement, "publish", pausing_publish)
    first = threading.Thread(target=credentials.store, kwargs={"serve_token": _TOKEN})
    first.start()
    assert inside.wait(5), "the first writer never reached its write"
    second = threading.Thread(target=credentials.drop, args=("api_key",))
    second.start()
    second.join(0.3)
    waited = second.is_alive()  # held at the lock while the first writer is inside
    go.set()
    first.join(5)
    second.join(5)
    assert waited, "the second writer went ahead while the first was mid-write"
    assert credentials.load_all() == {"serve_token": _TOKEN}


def test_a_lock_held_too_long_is_a_timeout_that_names_the_lock(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled writer costs the next one a bounded wait, and the refusal names the lock
    file, not the credentials, as what was in the way. The file is left as it was, and so is
    the directory: the temp made before the wait goes with the refusal."""
    monkeypatch.setattr(credentials, "LOCK_WAIT_S", 0.2)
    credentials.store(api_key=_KEY)  # creates the lock file
    fd = os.open(isolated_home / "credentials.lock", os.O_RDWR)
    lock_exclusive(fd)
    try:
        with pytest.raises(TimeoutError, match=r"credentials\.lock is held by another"):
            credentials.store(serve_token=_TOKEN)
    finally:
        unlock(fd)
        os.close(fd)
    assert credentials.load_all() == {"api_key": _KEY}
    files = sorted(p.name for p in isolated_home.iterdir() if p.is_file())
    assert files == ["credentials", "credentials.lock"], files
    credentials.store(serve_token=_TOKEN)  # released: back in business
    assert credentials.load_all() == {"api_key": _KEY, "serve_token": _TOKEN}


@pytest.mark.parametrize("write", ["store", "drop"])
def test_the_restriction_runs_before_the_writers_lock_is_taken(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, write: str
) -> None:
    """On Windows the restriction is an ``icacls`` subprocess, and a process's first one runs
    ``whoami`` too, each allowed 15 seconds, while a writer waiting for the lock gives up after
    two. Run inside the lock, a slow ``CreateProcess`` failed a concurrent ``login`` or
    ``serve`` with a ``TimeoutError`` (review of the #65 fold, F3). Asking ``whoami`` early
    still left ``icacls`` inside, and ``whoami`` too after a failed first answer, which is not
    cached (round 2, F1). The restriction targets the empty temp, which is this call's alone,
    so the whole of it comes first. The probe records whether the lock was free when it ran,
    then restricts for real."""
    credentials.store(api_key=_KEY, iam_token="t")
    lock_path = isolated_home / "credentials.lock"
    real = paths.restrict_to_owner
    free_when_restricting: list[bool] = []

    def probe(path: Path) -> bool:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_exclusive(fd)
        except OSError:
            free_when_restricting.append(False)
        else:
            unlock(fd)
            free_when_restricting.append(True)
        finally:
            os.close(fd)
        return real(path)

    monkeypatch.setattr(paths, "restrict_to_owner", probe)
    if write == "store":
        credentials.store(serve_token=_TOKEN)
    else:
        credentials.drop("iam_token")
    assert free_when_restricting == [True], free_when_restricting
    assert credentials.load_all() == (
        {"api_key": _KEY, "iam_token": "t", "serve_token": _TOKEN}
        if write == "store"
        else {"api_key": _KEY}
    )


def test_dropping_what_is_not_there_creates_nothing(isolated_home: Path) -> None:
    """A sign-out on a machine that never signed in writes no home and no lock file."""
    assert credentials.drop("iam_token") == ({}, True)
    assert not isolated_home.exists()


@pytest.mark.skipif(
    not can_symlink(), reason="this machine cannot create symlinks (needs privilege on Windows)"
)
def test_a_symlinked_file_is_written_through_and_the_link_kept(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The in-place write went through a link; a rename over the link's NAME would have
    swapped the user's pointer for a plain file."""
    real = tmp_path / "vault" / "aisquare-credentials"
    real.parent.mkdir()
    real.write_text(json.dumps({"api_key": _KEY}), encoding="utf-8")
    paths.ensure_home()
    paths.credentials_path().symlink_to(real)
    credentials.store(serve_token=_TOKEN)
    assert paths.credentials_path().is_symlink()
    assert json.loads(real.read_text(encoding="utf-8")) == {
        "api_key": _KEY,
        "serve_token": _TOKEN,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_a_new_file_is_owner_only_while_its_contents_are_written(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Created at the umask default, the first key sat in a 0644 file until the chmod after
    the write. The file is created 0600 first, so the temp that becomes it is 0600 too."""
    synced: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            synced.append(stat.S_IMODE(mode))
        real_fsync(fd)

    previous = os.umask(0o022)
    try:
        monkeypatch.setattr(os, "fsync", spy)
        credentials.store(api_key=_KEY)
    finally:
        os.umask(previous)
    assert synced == [0o600], [oct(mode) for mode in synced]
    assert stat.S_IMODE(paths.credentials_path().stat().st_mode) == 0o600


@pytest.mark.skipif(
    not can_deny_reads(), reason="chmod(0) denies nothing here (root, or NTFS where it is advice)"
)
def test_a_file_that_cannot_be_read_is_refused_not_replaced(isolated_home: Path) -> None:
    """``store`` read under the lock through ``load_all``, which reads an unreadable file as
    ``{}``. The in-place write then raised on the same file; the rename does not, so the store
    returned cleanly and the API key and serve token were gone (review of the #65 fold, F1).
    A root-owned 0600 file left by ``sudo`` with ``HOME`` kept is the realistic case."""
    credentials.store(api_key=_KEY, serve_token=_TOKEN)
    creds = paths.credentials_path()
    creds.chmod(0)
    try:
        with pytest.raises(PermissionError):
            credentials.store(iam_token="t")
        assert credentials.load_all() == {}, "the lockless read still reads it as nothing"
    finally:
        creds.chmod(0o600)
    assert credentials.load_all() == {"api_key": _KEY, "serve_token": _TOKEN}


def test_the_secrets_go_into_a_file_already_restricted_to_this_account(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restricted after the rename, the file was published under the DACL its temp inherited
    from the home, for an ``icacls`` call's width, and for good when the call failed; the
    in-place write had kept the owner-only DACL (review of the #65 fold, F2). The spy records
    what each restriction was applied to: the temp, while it was still empty, on every write."""
    real = paths.restrict_to_owner
    applied: list[tuple[str, int]] = []

    def spy(path: Path) -> bool:
        applied.append((path.name, path.stat().st_size))
        return real(path)

    monkeypatch.setattr(paths, "restrict_to_owner", spy)
    credentials.store(api_key=_KEY)  # a new file
    credentials.store(iam_token="t")  # over an existing one
    credentials.drop("iam_token")
    assert len(applied) == 3, applied
    for name, size in applied:
        assert name.startswith(".credentials.") and name.endswith(".tmp"), applied
        assert size == 0, f"restricted with the secrets already in it: {applied}"
    assert credentials.load_all() == {"api_key": _KEY}


def test_a_restriction_that_failed_is_reported_by_both_writers(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``drop`` discarded the answer. Dropping the session rewrites the file that still holds
    the API key, as a new file, so a sign-out whose restriction failed left it readable by
    other accounts with nothing said. Both writers still write: the report is the guard."""
    from aisquare.services import iam

    credentials.store(api_key=_KEY, iam_token="t")
    monkeypatch.setattr(paths, "restrict_to_owner", lambda path: False)
    assert credentials.store(serve_token=_TOKEN) == (
        {"api_key": _KEY, "iam_token": "t", "serve_token": _TOKEN},
        False,
    )
    assert credentials.drop("serve_token") == ({"api_key": _KEY, "iam_token": "t"}, False)
    assert credentials.drop("never_there") == ({"api_key": _KEY, "iam_token": "t"}, True)
    iam.clear_session()
    assert credentials.load_all() == {"api_key": _KEY}
    err = capsys.readouterr().err
    assert "warning: could not restrict" in err and "credentials left in it" in err, err
