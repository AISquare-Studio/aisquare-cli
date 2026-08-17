"""The rename must be flushed, and flushing it must never cost the write.

``save_config`` writes a sibling temp file, fsyncs it, and renames it over the
target. That is atomic — a reader never sees a partial file — but not yet
durable: until the PARENT DIRECTORY entry is flushed, a hard kill or power loss
can revert the file to its previous contents. @9bbc8ed7 spotted the missing last
step of the standard recipe while checking the atomicity fix.

The durability property itself is NOT asserted here, on purpose: proving it needs
a power cut or a filesystem fault injector, and neither belongs on a machine
someone is using. What is asserted is the SEQUENCE that produces it, which is the
same discipline the atomicity guard uses — a race would have measured the
machine's mood instead of the code.

Each assertion was made to fail before being kept, against a DIFFERENT wrong
implementation: removing the directory flush fails the ordering test, and
removing its fail-open guards (letting the OSError propagate) fails the two
fail-open tests. The temp-file hygiene case is a regression guard and passes
either way by design.

Cost was measured before the call was made rather than assumed: +2.15 ms median
per write (2.695 -> 4.845 ms, 200 interleaved samples, ext4 on a native WSL2
disk). All ten save_config call sites are explicit operator commands, none on the
launch, session or heartbeat path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.config import AppConfig, load_config, save_config


def _record(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Log ``replace`` and ``fsync`` in call order, tagging what was synced."""
    events: list[tuple[str, str]] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def _replace(src: Any, dst: Any, **kwargs: Any) -> None:
        events.append(("replace", str(dst)))
        real_replace(src, dst, **kwargs)

    def _fsync(fd: int) -> None:
        kind = "dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "file"
        events.append(("fsync", kind))
        real_fsync(fd)

    monkeypatch.setattr(os, "replace", _replace)
    monkeypatch.setattr(os, "fsync", _fsync)
    return events


def test_the_parent_directory_is_flushed_after_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole recipe, in order: fsync(file) -> rename -> fsync(dir).

    Order is the point. A directory flush BEFORE the rename would sync a
    directory that does not yet contain the new entry, which is a no-op wearing
    the appearance of durability.
    """
    target = tmp_path / "config.toml"
    events = _record(monkeypatch)

    save_config(AppConfig(), target)
    monkeypatch.undo()

    assert ("fsync", "dir") in events, (
        f"the parent directory was never flushed after the rename: {events}"
    )
    kinds = [f"{what}:{detail}" for what, detail in events]
    file_sync = kinds.index("fsync:file")
    renamed = next(i for i, k in enumerate(kinds) if k.startswith("replace:"))
    dir_sync = kinds.index("fsync:dir")
    assert file_sync < renamed < dir_sync, f"durable-replace out of order: {kinds}"


def test_a_directory_that_cannot_be_synced_does_not_cost_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open, and this is the assertion that keeps the addition safe.

    By the time the directory is flushed the caller's change is already on disk.
    A parent that cannot be opened or synced — read-only mount, exotic
    filesystem — must cost DURABILITY and never the write, or a hardening step
    becomes a new way for `explainability enable` to fail.
    """
    target = tmp_path / "config.toml"
    config = AppConfig()
    config.explainability.gateway_url = "https://stg.example"

    real_fsync = os.fsync

    def _fail_on_directories(fd: int) -> None:
        if os.path.isdir(f"/proc/self/fd/{fd}"):
            raise OSError("this filesystem does not permit directory fsync")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fail_on_directories)

    save_config(config, target)  # must not raise
    monkeypatch.undo()

    assert load_config(target).explainability.gateway_url == "https://stg.example", (
        "the write was lost because the durability step failed"
    )


def test_a_parent_that_cannot_be_opened_does_not_cost_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of fail-open: os.open on the directory raising."""
    target = tmp_path / "config.toml"
    real_open = os.open

    def _refuse_directories(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.path.isdir(path):
            raise PermissionError("no directory handles here")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _refuse_directories)

    save_config(AppConfig(), target)  # must not raise
    monkeypatch.undo()

    assert load_config(target).profile == "default"


def test_no_stray_temp_file_survives_a_successful_write(tmp_path: Path) -> None:
    """The directory now gets flushed; make sure it has nothing extra in it.

    A leftover dotfile beside config.toml would be litter in the directory an
    operator reads, and it would also mean the rename did not consume the temp.
    """
    target = tmp_path / "config.toml"
    save_config(AppConfig(), target)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]
