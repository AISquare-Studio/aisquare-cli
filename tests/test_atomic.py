"""``core.atomic.write_replacing`` — the one durable replace-by-rename (review of #167)."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from aisquare.core.atomic import replacement, write_replacing

#: The directory fsync after a rename, where the platform can open a directory to sync it.
_DIRECTORY_SYNC = [] if sys.platform == "win32" else ["fsync"]


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def test_a_keyboard_interrupt_mid_write_leaves_no_temp_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C at `project switch` on a slow disk used to leave `state.json.<pid>.tmp` beside the
    file an operator reads; only `OSError` was cleaned up after."""
    target = tmp_path / "state.json"
    target.write_text("old\n")

    def interrupt(fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupt)
    with pytest.raises(KeyboardInterrupt):
        write_replacing(target, "new\n")
    assert target.read_text() == "old\n"
    assert _leftovers(tmp_path) == ["state.json"]


def test_a_read_only_temp_is_still_removed_when_the_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `keep_mode` copying a read-only target's bits onto the temp, a failed replace left a
    temp Windows would not delete; the cleanup makes it writable first. Read as the owner's
    write bit, the one bit NTFS keeps: there ``st_mode`` is 0o444 or 0o666, never 0o600."""
    target = tmp_path / "config.toml"
    target.write_text("old\n")
    target.chmod(0o444)
    unlinked: list[bool] = []
    real_unlink = os.unlink

    def spying_unlink(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
        unlinked.append(bool(os.stat(path).st_mode & stat.S_IWUSR))
        real_unlink(path, dir_fd=dir_fd)

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(os, "unlink", spying_unlink)
    try:
        with pytest.raises(PermissionError):
            write_replacing(target, "new\n", keep_mode=True)
    finally:
        target.chmod(0o644)
    assert unlinked == [True], "made writable before the unlink"
    assert _leftovers(tmp_path) == ["config.toml"]


def test_a_filesystem_that_refuses_chmod_still_gets_its_temp_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cleanup's chmod and unlink shared one `suppress`: a chmod that raised (vfat, some
    CIFS) skipped the unlink, the opposite of what the line was added for."""
    target = tmp_path / "state.json"
    target.write_text("old\n")

    def refuse_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    def busy(src: object, dst: object) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "chmod", refuse_chmod)
    monkeypatch.setattr(os, "replace", busy)
    with pytest.raises(OSError, match="busy"):  # the replace's error, not the cleanup's
        write_replacing(target, "new\n", keep_mode=False)
    assert target.read_text() == "old\n"
    assert _leftovers(tmp_path) == ["state.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_keep_mode_copies_the_targets_bits_and_off_takes_the_umask_default(
    tmp_path: Path,
) -> None:
    kept = tmp_path / "kept.json"
    kept.write_text("{}\n")
    kept.chmod(0o600)
    write_replacing(kept, '{"a": 1}\n', keep_mode=True)
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    reset = tmp_path / "reset.json"
    reset.write_text("{}\n")
    reset.chmod(0o600)
    write_replacing(reset, '{"a": 1}\n', keep_mode=False)
    assert stat.S_IMODE(reset.stat().st_mode) == 0o666 & ~_umask()
    assert kept.read_text() == reset.read_text() == '{"a": 1}\n'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_keep_mode_creates_the_temp_with_the_targets_bits_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the #167 fold, F7. The temp was created at the umask default and given the
    target's bits only after the body was written and fsynced: a `chmod 600` file's contents
    sat in a 644 file for the whole write, and a kill before the chmod left them there. Read
    at the fsync, the temp is never readable more widely than its target. The negative
    halves: bits the umask narrows (664 under 022) are still the target's exactly once it
    lands, and a read-only target is still written — its temp is its owner's to write until
    the body is in."""
    at_fsync: list[int] = []
    real_fsync = os.fsync

    def fsync_spy(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):  # the temp, not the directory synced after the rename
            at_fsync.append(stat.S_IMODE(mode))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_spy)
    secret = tmp_path / "secret.json"
    secret.write_text("{}\n")
    secret.chmod(0o600)
    shared = tmp_path / "shared.json"
    shared.write_text("{}\n")
    shared.chmod(0o664)
    frozen = tmp_path / "frozen.json"
    frozen.write_text("{}\n")
    frozen.chmod(0o444)
    previous = os.umask(0o022)
    try:
        write_replacing(secret, '{"token": 1}\n')
        write_replacing(shared, '{"a": 1}\n')
        write_replacing(frozen, '{"b": 1}\n')
    finally:
        os.umask(previous)
    assert at_fsync == [0o600, 0o644, 0o644], [oct(mode) for mode in at_fsync]
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(shared.stat().st_mode) == 0o664
    assert stat.S_IMODE(frozen.stat().st_mode) == 0o444
    assert secret.read_text() == '{"token": 1}\n' and shared.read_text() == '{"a": 1}\n'
    assert frozen.read_text() == '{"b": 1}\n'
    assert _leftovers(tmp_path) == ["frozen.json", "secret.json", "shared.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_owner_only_restricts_the_empty_temp_whatever_the_targets_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a file of secrets (review of the #65 fold, F2). The temp is 0600 from its creation
    and restricted to this account before the body is in it, where restricting the target after
    the rename published the secrets under the DACL the temp inherited. A target someone left
    0644 lands 0600: ``keep_mode`` does not carry a wider mode onto secrets. A restriction that
    fails is returned, and the write still lands."""
    from aisquare.core import paths

    at_restriction: list[tuple[str, int, int]] = []
    answer = True

    def spy(path: Path) -> bool:
        info = path.stat()
        at_restriction.append((path.name, info.st_size, stat.S_IMODE(info.st_mode)))
        return answer

    monkeypatch.setattr(paths, "restrict_to_owner", spy)
    secret = tmp_path / "credentials"
    secret.write_text("{}\n")
    secret.chmod(0o644)
    previous = os.umask(0o022)
    try:
        assert write_replacing(secret, '{"token": 1}\n', owner_only=True) is True
        answer = False
        assert write_replacing(secret, '{"token": 2}\n', owner_only=True) is False
        assert write_replacing(tmp_path / "plain.json", "{}\n") is True  # nothing asked
    finally:
        os.umask(previous)
    assert [(size, mode) for _, size, mode in at_restriction] == [(0, 0o600), (0, 0o600)]
    assert all(name.startswith(".credentials.") for name, _, _ in at_restriction)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.read_text() == '{"token": 2}\n'
    assert _leftovers(tmp_path) == ["credentials", "plain.json"]


def test_durable_syncs_the_file_then_the_directory_and_not_durable_syncs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache on the hook path pays no fsync; a preference file pays both. Windows cannot
    open a directory to sync it (``os.open`` refuses one), so there the directory's flush
    fails open, as ``_sync_directory`` promises, and only the file's is seen."""
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync_spy(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace_spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync_spy)
    monkeypatch.setattr(os, "replace", replace_spy)
    write_replacing(tmp_path / "durable.json", "{}\n")
    assert calls == ["fsync", "replace", *_DIRECTORY_SYNC]
    calls.clear()
    write_replacing(tmp_path / "cache.json", "{}\n", durable=False)
    assert calls == ["replace"]
    assert _leftovers(tmp_path) == ["cache.json", "durable.json"]


def test_a_replacement_left_unpublished_removes_its_temp_and_publishes_once(
    tmp_path: Path,
) -> None:
    """``replacement`` is ``write_replacing`` in two steps, for a caller that decides the body
    under a lock (``core.credentials``, review of the #65 fold, round 2, F1). The temp waits
    beside the target between them. A caller that finds nothing to write leaves without a
    publish, and the temp goes too. A second publish is refused: the temp has become the
    target, and a second one would be a new file that no restriction was applied to."""
    target = tmp_path / "credentials"
    target.write_text("old\n")
    with replacement(target, owner_only=True) as pending:
        waiting = _leftovers(tmp_path)
    assert len(waiting) == 2 and waiting[0].startswith(".credentials."), waiting
    assert not pending.published
    assert _leftovers(tmp_path) == ["credentials"] and target.read_text() == "old\n"
    with replacement(target, owner_only=True) as pending:
        pending.publish("new\n")
        with pytest.raises(RuntimeError, match="already replaced"):
            pending.publish("newer\n")
    assert pending.published and target.read_text() == "new\n"
    assert _leftovers(tmp_path) == ["credentials"]


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current
