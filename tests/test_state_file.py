"""``state.json`` has one reader and one writer (``core.state_file``).

The theme, the pinned project and the navigator's width each carried their own
read-modify-write of the file and the copies had drifted (review of #167):
one raised on a file whose top level was not an object, one replaced such a
file wholesale, two shared a fixed temp name across processes — and the
per-process temp name alone fixed torn writes, not lost updates.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from aisquare.core import state_file
from aisquare.core.atomic import write_replacing
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.state_file import StateUnwritableError, read_state, update_state
from tests.fsperms import can_deny_reads, can_deny_writes, unwritable

_not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads through any file mode"
)


def _path(isolated_home: Path) -> Path:
    return isolated_home / "state.json"


def _siblings(isolated_home: Path) -> list[str]:
    """Every FILE in the home but the writer's own lock, which stays — a leftover temp is
    `.state.json.<pid>.<hex>.tmp`, and a filter on names starting with `state` let it hide."""
    return sorted(
        p.name for p in isolated_home.iterdir() if p.is_file() and not p.name.endswith(".lock")
    )


def test_read_state_is_empty_for_a_missing_unreadable_or_non_object_file(
    isolated_home: Path,
) -> None:
    assert read_state() == {}  # no home at all yet
    isolated_home.mkdir(parents=True)
    path = _path(isolated_home)
    path.write_text("{not json")
    assert read_state() == {}
    path.write_text('["was", "a", "list"]')
    assert read_state() == {}
    path.write_text('{"board_theme": "nord", "sidebar_width": 44}')
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}


def test_update_state_keeps_the_other_keys_and_none_drops_one(isolated_home: Path) -> None:
    update_state("board_theme", "nord")  # creates the home and the file
    update_state("sidebar_width", 44)
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}
    update_state("sidebar_width", None)
    assert read_state() == {"board_theme": "nord"}
    update_state("never_there", None)  # dropping what is not there is fine
    assert json.loads(_path(isolated_home).read_text()) == {"board_theme": "nord"}
    assert _siblings(isolated_home) == ["state.json"]


@pytest.mark.parametrize("body", ['["was", "a", "list"]\n', "{not json\n", '"just a string"\n'])
def test_update_state_refuses_a_file_that_is_not_an_object_and_leaves_it_as_it_is(
    isolated_home: Path, body: str
) -> None:
    isolated_home.mkdir(parents=True)
    _path(isolated_home).write_text(body)
    with pytest.raises(StateUnwritableError, match=r"state\.json is not a JSON object"):
        update_state("sidebar_width", 44)
    assert _path(isolated_home).read_text() == body
    assert _siblings(isolated_home) == ["state.json"]


@pytest.mark.parametrize("body", ["", "  \n\n"])
def test_an_empty_file_is_healed_not_refused(isolated_home: Path, body: str) -> None:
    """A 0-byte `state.json` — what a crash leaves — has no keys to protect; refusing it
    made `project switch` fail and the theme and width unrememberable until someone deleted
    the file by hand. Before the shared writer the theme's own writer healed this case."""
    isolated_home.mkdir(parents=True)
    _path(isolated_home).write_text(body)
    assert read_state() == {}
    update_state("sidebar_width", 44)
    assert read_state() == {"sidebar_width": 44}


def test_a_pathologically_nested_file_reads_as_empty_and_refuses_an_update(
    isolated_home: Path,
) -> None:
    """`RecursionError` is neither `OSError` nor `ValueError`: it escaped both functions —
    from the fleet UI's mount, the exact place this module was written to make safe."""
    isolated_home.mkdir(parents=True)
    body = "[" * 100_000 + "]" * 100_000
    _path(isolated_home).write_text(body)
    assert read_state() == {}
    with pytest.raises(StateUnwritableError, match="is not a JSON object"):
        update_state("sidebar_width", 44)
    assert _path(isolated_home).read_text() == body


def test_a_value_json_cannot_encode_is_refused_not_raised_as_a_type_error(
    isolated_home: Path,
) -> None:
    update_state("board_theme", "nord")
    with pytest.raises(StateUnwritableError, match="is not JSON"):
        update_state("sidebar_width", object())
    assert read_state() == {"board_theme": "nord"}
    assert _siblings(isolated_home) == ["state.json"]


@pytest.mark.skipif(not can_deny_reads(), reason="mode 000 does not stop this user from reading")
def test_strict_reading_raises_for_an_unreadable_file_but_not_a_missing_one(
    isolated_home: Path,
) -> None:
    """The pin must tell "the file says nothing" from "the file could not be read": read as no
    pin, a permission error points every project-scoped command at the working directory."""
    assert read_state(strict=True) == {}  # missing: nothing to say, nothing wrong
    isolated_home.mkdir(parents=True)
    path = _path(isolated_home)
    path.write_text('{"active_project_id": "prj_abc"}')
    path.chmod(0)
    try:
        assert read_state() == {}, "lenient: a preference that cannot be read is not set"
        with pytest.raises(PermissionError):
            read_state(strict=True)
    finally:
        path.chmod(0o600)


def test_two_writers_cannot_lose_each_others_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the lock: A reads {theme}, B reads {theme}, A writes {theme, width}, B writes
    {theme: other} — the width is gone and both returned. The fleet UI's width debounce,
    `board -w`'s theme autosave and `project switch` all reach this path."""
    update_state("board_theme", "nord")
    inside, go = threading.Event(), threading.Event()
    paused = False

    def pausing_write(target: Path, body: str, *, keep_mode: bool = True) -> None:
        nonlocal paused
        if not paused:  # the first writer, mid-critical-section, waits for the test's go
            paused = True
            inside.set()
            assert go.wait(5), "the test never let the first writer finish"
        write_replacing(target, body, keep_mode=keep_mode)

    monkeypatch.setattr(state_file, "write_replacing", pausing_write)
    first = threading.Thread(target=update_state, args=("sidebar_width", 61))
    first.start()
    assert inside.wait(5), "the first writer never reached its write"
    second = threading.Thread(target=update_state, args=("board_theme", "dracula"))
    second.start()
    second.join(0.3)
    waited = second.is_alive()  # held at the lock while the first writer is inside
    go.set()
    first.join(5)
    second.join(5)
    assert waited, "the second writer went ahead while the first was mid-write"
    assert read_state() == {"board_theme": "dracula", "sidebar_width": 61}


def test_a_lock_held_too_long_is_a_refusal_that_names_the_lock(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking `flock` waited forever — on the fleet UI's thread. The wait is bounded and
    the refusal says which file is in the way, so a toast does not blame `state.json`."""
    monkeypatch.setattr(state_file, "LOCK_WAIT_S", 0.2)
    update_state("board_theme", "nord")  # creates the lock file
    fd = os.open(isolated_home / "state.json.lock", os.O_RDONLY)
    lock_exclusive(fd)
    try:
        with pytest.raises(StateUnwritableError, match=r"state\.json\.lock is held by another"):
            update_state("sidebar_width", 44)
    finally:
        unlock(fd)
        os.close(fd)
    assert read_state() == {"board_theme": "nord"}
    update_state("sidebar_width", 44)  # released: back in business
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}


@_not_root
def test_a_read_only_lock_file_still_serves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened for append, a lock file this user cannot write to refused every update — and the
    refusal blamed `state.json`. It is opened for WRITING first (NFS emulates an exclusive
    `flock` with a byte-range lock and needs that) and read-only when that is refused, which
    is enough on a local disk. Created `0o644`, so a lock left by another user can be read."""
    opened: list[int] = []
    real_open = os.open

    def spy(path: str | os.PathLike[str], flags: int, mode: int = 0o777, *args: object) -> int:
        if os.fspath(path).endswith(".lock"):
            opened.append(flags)
        return real_open(path, flags, mode, *args)

    monkeypatch.setattr(os, "open", spy)
    update_state("board_theme", "nord")
    lock = isolated_home / "state.json.lock"
    assert opened == [os.O_RDWR | os.O_CREAT], "for writing, as NFS needs"
    assert stat.S_IMODE(lock.stat().st_mode) & 0o004, "readable by another user"
    lock.chmod(0o444)
    update_state("sidebar_width", 44)
    assert opened[1:] == [os.O_RDWR | os.O_CREAT, os.O_RDONLY], "refused for writing: read-only"
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}


@pytest.mark.parametrize("held", sorted({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}))
def test_every_errno_that_means_held_is_waited_for_not_refused(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, held: int
) -> None:
    """`EACCES` is what Windows' `msvcrt.locking(LK_NBLCK)` raises for a held lock; the Linux-only
    CI would not notice it dropping out of `_HELD`, and every contended save on Windows would
    then be refused at once."""
    update_state("board_theme", "nord")
    attempts: list[int] = []

    def held_twice(fd: int) -> None:
        attempts.append(fd)
        if len(attempts) <= 2:
            raise OSError(held, "held")
        lock_exclusive(fd)

    monkeypatch.setattr(state_file, "lock_exclusive", held_twice)
    update_state("sidebar_width", 44)
    assert len(attempts) == 3, "waited through two 'held' answers, then took the lock"
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}


def test_a_file_that_already_says_so_is_not_rewritten(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is taken here, under the lock, where the file is the truth — not on a
    caller's memory of what it last wrote, which another process may have changed since."""
    update_state("sidebar_width", 44)
    rewrites: list[str] = []

    def spy(target: Path, body: str, *, keep_mode: bool = True, durable: bool = True) -> None:
        rewrites.append(body)
        write_replacing(target, body, keep_mode=keep_mode, durable=durable)

    monkeypatch.setattr(state_file, "write_replacing", spy)
    update_state("sidebar_width", 44)  # says so already
    update_state("never_there", None)  # already absent
    assert rewrites == []
    update_state("sidebar_width", 45)
    update_state("sidebar_width", None)
    assert len(rewrites) == 2
    assert read_state() == {}


def test_a_temp_another_process_left_behind_is_swept_on_the_next_update(
    isolated_home: Path,
) -> None:
    """A quit that ran out of time mid-write leaves `.state.json.<pid>.<hex>.tmp`, named for a
    pid nothing will reuse; older than a minute it is swept under the lock. A fresh one is
    someone's write in progress and stays."""
    update_state("board_theme", "nord")
    stale = isolated_home / ".state.json.99999.deadbeef.tmp"
    stale.write_text("{}\n")
    old = time.time() - 120
    os.utime(stale, (old, old))
    fresh = isolated_home / ".state.json.99998.cafef00d.tmp"
    fresh.write_text("{}\n")
    update_state("sidebar_width", 44)
    assert not stale.exists()
    assert fresh.exists()
    fresh.unlink()
    assert _siblings(isolated_home) == ["state.json"]


@pytest.mark.skipif(not can_deny_writes(), reason="writes into a directory cannot be denied here")
def test_a_home_we_cannot_write_to_is_refused_as_a_permission_problem(
    isolated_home: Path,
) -> None:
    """With no lock file yet, the read-only fallback (`O_RDONLY` without `O_CREAT`) raised
    `ENOENT`, so the toast and `project switch` read 'No such file or directory' for what was a
    permission problem. The home is denied through `fsperms.unwritable`: a `chmod(0o555)` on a
    directory is a no-op on NTFS, and advice to root."""
    update_state("board_theme", "nord")
    (isolated_home / "state.json.lock").unlink()
    with unwritable(isolated_home):
        with pytest.raises(StateUnwritableError, match=r"state\.json\.lock could not be opened"):
            update_state("sidebar_width", 44)
        with pytest.raises(StateUnwritableError, match="Permission denied"):
            update_state("sidebar_width", 44)
    assert read_state() == {"board_theme": "nord"}


def test_a_lock_error_that_is_not_contention_is_refused_at_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `OSError` from the primitive was polled for `LOCK_WAIT_S` and then blamed on
    another process — `ENOLCK`, `EOPNOTSUPP`, `EBADF` (the NFS read-only case) included,
    none of which a retry can clear."""
    update_state("board_theme", "nord")

    def no_locks(fd: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(state_file, "lock_exclusive", no_locks)
    started = time.monotonic()
    with pytest.raises(StateUnwritableError, match=r"state\.json\.lock could not be locked"):
        update_state("sidebar_width", 44)
    assert time.monotonic() - started < state_file.LOCK_WAIT_S / 2, "refused at once, no poll"
    assert read_state() == {"board_theme": "nord"}


def test_a_file_that_does_not_decode_is_corrupt_not_unreadable(isolated_home: Path) -> None:
    """`UnicodeDecodeError` escaped both functions once the read was split from the parse — from
    `project info` and from both TUIs' mount."""
    isolated_home.mkdir(parents=True)
    body = b'{"board_theme": "caf\xe9"}\n'  # a Latin-1 edit
    _path(isolated_home).write_bytes(body)
    assert read_state() == {}
    assert read_state(strict=True) == {}, "corrupt, not unreadable: strict has nothing to raise"
    with pytest.raises(StateUnwritableError, match="is not a JSON object"):
        update_state("sidebar_width", 44)
    assert _path(isolated_home).read_bytes() == body


def test_the_other_shapes_a_crash_or_an_editor_leaves_are_healed_or_read(
    isolated_home: Path,
) -> None:
    """NULs (the size reached the disk, the data did not) are an empty file; a BOM (Notepad)
    in front of a valid object is that object."""
    isolated_home.mkdir(parents=True)
    _path(isolated_home).write_bytes(b"\x00" * 64)
    assert read_state() == {}
    update_state("sidebar_width", 44)
    assert read_state() == {"sidebar_width": 44}
    _path(isolated_home).write_bytes("\ufeff".encode() + b'{"board_theme": "nord"}\n')
    assert read_state() == {"board_theme": "nord"}
    update_state("sidebar_width", 44)
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}


def test_the_write_goes_through_a_symlink_and_keeps_the_targets_mode(
    isolated_home: Path, tmp_path: Path
) -> None:
    """`os.replace` on the link itself severed it: a dotfiles target went stale for good while
    `git status` showed nothing — and the umask-default temp turned a chmod 600 into a 644."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / "aisquare-state.json"
    target.write_text('{"board_theme": "nord"}\n')
    target.chmod(0o600)
    isolated_home.mkdir(parents=True)
    _path(isolated_home).symlink_to(target)

    update_state("sidebar_width", 44)

    assert _path(isolated_home).is_symlink(), "the link is still a link"
    assert json.loads(target.read_text()) == {"board_theme": "nord", "sidebar_width": 44}
    if sys.platform != "win32":  # NTFS keeps one bit of the mode: 0o666 or 0o444
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}
    assert sorted(p.name for p in dotfiles.iterdir()) == ["aisquare-state.json"], (
        "no temp and no lock left in the dotfiles repo"
    )


def test_the_write_is_a_rename_of_this_processs_own_fsynced_temp_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`asq` and `board -w` autosaving at once used to share one `state.json.tmp`; and without
    the fsync a crash after the rename can publish an empty file (XFS), which the
    refuse-on-corrupt policy would then have made permanent."""
    calls: list[str] = []
    renamed: list[tuple[str, str]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync_spy(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace_spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls.append("replace")
        renamed.append((os.fspath(src), os.fspath(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync_spy)
    monkeypatch.setattr(os, "replace", replace_spy)
    update_state("sidebar_width", 44)
    # Windows cannot open a directory to sync it, and that flush fails open there.
    directory_sync = [] if sys.platform == "win32" else ["fsync"]
    assert calls == ["fsync", "replace", *directory_sync], (
        "the temp reaches the disk before it is published, and the rename is made durable"
    )
    ((src, dst),) = renamed
    assert dst == str(_path(isolated_home))
    assert Path(src).parent == isolated_home, "a sibling: the rename stays on one filesystem"
    assert Path(src).name.startswith(".state.json.") and f".{os.getpid()}." in Path(src).name
    assert _siblings(isolated_home) == ["state.json"]


def test_a_failed_rename_leaves_no_temp_file_behind_and_says_so(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    update_state("board_theme", "nord")
    before = _path(isolated_home).read_text()

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(StateUnwritableError, match=r"state\.json could not be written"):
        update_state("sidebar_width", 44)
    assert _path(isolated_home).read_text() == before
    assert _siblings(isolated_home) == ["state.json"]


def test_the_same_value_in_another_json_type_is_rewritten(isolated_home: Path) -> None:
    """`60.0 == 60` and `True == 1` in Python: a width stored as a float by a hand edit read as
    "already says so" and was never rewritten — while the divider ignores a width that is not
    an `int`, so the preference could never be saved and nothing said so."""
    isolated_home.mkdir(parents=True)
    _path(isolated_home).write_text('{"sidebar_width": 60.0, "flag": true}\n')
    update_state("sidebar_width", 60)
    update_state("flag", 1)
    raw = json.loads(_path(isolated_home).read_text())
    assert raw == {"sidebar_width": 60, "flag": 1}
    assert type(raw["sidebar_width"]) is int and type(raw["flag"]) is int
