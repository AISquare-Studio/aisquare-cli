"""``core.atomic.write_replacing`` — the one durable replace-by-rename (review of #167)."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from aisquare.core.atomic import write_replacing


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
    temp Windows would not delete; the cleanup makes it writable first."""
    target = tmp_path / "config.toml"
    target.write_text("old\n")
    target.chmod(0o444)
    unlinked: list[int] = []
    real_unlink = os.unlink

    def spying_unlink(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
        unlinked.append(stat.S_IMODE(os.stat(path).st_mode))
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
    assert unlinked == [0o600], "made writable before the unlink"
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


def test_durable_syncs_the_file_then_the_directory_and_not_durable_syncs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache on the hook path pays no fsync; a preference file pays both."""
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
    assert calls == ["fsync", "replace", "fsync"]
    calls.clear()
    write_replacing(tmp_path / "cache.json", "{}\n", durable=False)
    assert calls == ["replace"]
    assert _leftovers(tmp_path) == ["cache.json", "durable.json"]


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current
