"""``state.json`` has one reader and one writer (``core.state_file``).

The theme, the pinned project and the navigator's width each carried their own
read-modify-write of the file and the copies had drifted (review of #167):
one raised on a file whose top level was not an object, one replaced such a
file wholesale, two shared a fixed temp name across processes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aisquare.core.state_file import read_state, update_state


def _path(isolated_home: Path) -> Path:
    return isolated_home / "state.json"


def _siblings(isolated_home: Path) -> list[str]:
    return sorted(p.name for p in isolated_home.iterdir() if p.name.startswith("state"))


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
