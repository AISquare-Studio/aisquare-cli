"""``state.json`` has one reader and one writer (``core.state_file``).

The theme, the pinned project and the navigator's width each carried their own
read-modify-write of the file and the copies had drifted (review of #167):
one raised on a file whose top level was not an object, one replaced such a
file wholesale, two shared a fixed temp name across processes — and the
per-process temp name alone fixed torn writes, not lost updates.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from aisquare.core import state_file
from aisquare.core.state_file import read_state, update_state

_not_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads through any file mode"
)


def _path(isolated_home: Path) -> Path:
    return isolated_home / "state.json"


def _siblings(isolated_home: Path) -> list[str]:
    """Everything beside the file but the writer's own lock, which stays."""
    return sorted(
        p.name
        for p in isolated_home.iterdir()
        if p.name.startswith("state") and not p.name.endswith(".lock")
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
    assert update_state("board_theme", "nord") is True  # creates the home and the file
    assert update_state("sidebar_width", 44) is True
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}
    assert update_state("sidebar_width", None) is True
    assert read_state() == {"board_theme": "nord"}
    assert update_state("never_there", None) is True  # dropping what is not there is fine
    assert json.loads(_path(isolated_home).read_text()) == {"board_theme": "nord"}
    assert _siblings(isolated_home) == ["state.json"]


@pytest.mark.parametrize("body", ['["was", "a", "list"]\n', "{not json\n", '"just a string"\n'])
def test_update_state_refuses_a_file_that_is_not_an_object_and_leaves_it_as_it_is(
    isolated_home: Path, body: str
) -> None:
    isolated_home.mkdir(parents=True)
    _path(isolated_home).write_text(body)
    assert update_state("sidebar_width", 44) is False
    assert _path(isolated_home).read_text() == body
    assert _siblings(isolated_home) == ["state.json"]


def test_a_pathologically_nested_file_reads_as_empty_and_refuses_an_update(
    isolated_home: Path,
) -> None:
    """`RecursionError` is neither `OSError` nor `ValueError`: it escaped both functions —
    from the fleet UI's mount, the exact place this module was written to make safe."""
    isolated_home.mkdir(parents=True)
    body = "[" * 100_000 + "]" * 100_000
    _path(isolated_home).write_text(body)
    assert read_state() == {}
    assert update_state("sidebar_width", 44) is False
    assert _path(isolated_home).read_text() == body


def test_a_value_json_cannot_encode_is_refused_not_raised(isolated_home: Path) -> None:
    assert update_state("board_theme", "nord") is True
    assert update_state("sidebar_width", object()) is False
    assert read_state() == {"board_theme": "nord"}
    assert _siblings(isolated_home) == ["state.json"]


@_not_root
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
    {theme: other} — the width is gone and both returned True. The fleet UI's width debounce,
    `board -w`'s theme autosave and `project switch` all reach this path."""
    assert update_state("board_theme", "nord") is True
    inside, go = threading.Event(), threading.Event()
    real_replace = state_file._replace
    paused = False

    def pausing_replace(target: Path, body: str) -> bool:
        nonlocal paused
        if not paused:  # the first writer, mid-critical-section, waits for the test's go
            paused = True
            inside.set()
            assert go.wait(5), "the test never let the first writer finish"
        return real_replace(target, body)

    monkeypatch.setattr(state_file, "_replace", pausing_replace)
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

    assert update_state("sidebar_width", 44) is True

    assert _path(isolated_home).is_symlink(), "the link is still a link"
    assert json.loads(target.read_text()) == {"board_theme": "nord", "sidebar_width": 44}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert read_state() == {"board_theme": "nord", "sidebar_width": 44}
    assert sorted(p.name for p in dotfiles.iterdir()) == ["aisquare-state.json"], (
        "no temp and no lock left in the dotfiles repo"
    )


def test_the_write_is_a_rename_of_this_processs_own_temp_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`asq` and `board -w` autosaving at once used to share one `state.json.tmp`."""
    renamed: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        renamed.append((os.fspath(src), os.fspath(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    assert update_state("sidebar_width", 44) is True
    ((src, dst),) = renamed
    assert dst == str(_path(isolated_home))
    assert Path(src).parent == isolated_home, "a sibling: the rename stays on one filesystem"
    assert src.endswith(f".{os.getpid()}.tmp")
    assert _siblings(isolated_home) == ["state.json"]


def test_a_failed_rename_leaves_no_temp_file_behind_and_reports_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert update_state("board_theme", "nord") is True
    before = _path(isolated_home).read_text()

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    assert update_state("sidebar_width", 44) is False
    assert _path(isolated_home).read_text() == before
    assert _siblings(isolated_home) == ["state.json"]
