"""aisquare reads a manager's state file below the home, never through a link, a regular file
of a bounded size or not at all (#78 reopen, SECURITY).

The state directory is the one a sandboxed manager's launch lets its sandbox write
(SMOKE13 N1), and aisquare reads ``state.md`` back outside any sandbox: the start pack at
every manager's SessionStart quotes it, and a quiet compaction goes by its age. The tester
planted ``state.md`` as a link to ``/dev/zero`` (the hook's read grew to ~13.9 GB in a
second) and as a FIFO (the hook hung 120 s); a link to a secret outside (``~/.ssh/…``,
``~/.codex/auth.json``) would have put it in the start pack. Each is now refused with a line
saying why, quickly, its content never quoted: ``paths.read_below`` walks from the home one
name at a time with ``O_NOFOLLOW``, opens the file ``O_NONBLOCK`` and reads only a regular
file of at most ``paths.MANAGER_STATE_LIMIT`` bytes.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest

from aisquare.core import paths
from aisquare.core.orchestrator import team_project
from aisquare.services import quiet_compact
from aisquare.services import team as team_service
from tests.fsperms import can_symlink

pytestmark = pytest.mark.usefixtures("project_capture")

MANAGER = "aaaa1111-0000-0000-0000-000000000000"
SECRET = "SECRET-BYTES-of-the-owner"
T = TypeVar("T")

posix = pytest.mark.skipif(os.name != "posix", reason="FIFOs and /dev/zero are POSIX's")
links = pytest.mark.skipif(not can_symlink(), reason="this machine makes no symbolic link")


@pytest.fixture(autouse=True)
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """A file outside the home that only its owner should read."""
    path = tmp_path / "outside" / "auth.json"
    path.parent.mkdir()
    path.write_text(SECRET, encoding="utf-8")
    return path


def _state(work: Path) -> Path:
    path = paths.manager_state_file(team_project(work).id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _within(seconds: float, call: Callable[[], T], fifo: Path | None = None) -> T:
    """``call()``'s answer, which must come within ``seconds``: a read that blocks fails the
    test, never the run (a FIFO it waits on is opened for writing, which lets it go)."""
    answer: dict[str, object] = {}

    def run() -> None:
        try:
            answer["value"] = call()
        except BaseException as exc:
            answer["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        if fifo is not None and sys.platform != "win32":
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        raise AssertionError(f"it blocked for more than {seconds} s")
    if "error" in answer:
        raise answer["error"]  # type: ignore[misc]
    return answer["value"]  # type: ignore[return-value]


def _pack(monkeypatch: pytest.MonkeyPatch, work: Path) -> str:
    """A manager's start pack, as its SessionStart hook injects it, within 10 s."""
    with monkeypatch.context() as patch:
        patch.setenv("AISQUARE_ROLE", "manager")
        return _within(10, lambda: team_service.hook_session_start(MANAGER, work, "startup"))


# --- read_below ---------------------------------------------------------------------------


def test_a_regular_file_is_read_with_the_stat_it_was_read_through(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.md").write_bytes(b"goal: ship")

    read = paths.read_below(tmp_path, ("a", "b", "f.md"), 64)

    assert read.data == b"goal: ship" and read.info.st_size == 10
    with pytest.raises(FileNotFoundError):
        paths.read_below(tmp_path, ("a", "b", "missing.md"), 64)
    with pytest.raises(FileNotFoundError):
        paths.read_below(tmp_path, ("nothing", "f.md"), 64)


@links
def test_a_link_is_never_followed_at_the_file_or_on_the_way(tmp_path: Path, secret: Path) -> None:
    """Mutants: ``O_NOFOLLOW`` dropped (the file's link is read), the walk replaced by one open
    of the whole path (the link on the way is followed). Each reads the secret and dies."""
    root = tmp_path / "home"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f.md").symlink_to(secret)
    (root / "b").symlink_to(secret.parent, target_is_directory=True)

    for names in (("a", "f.md"), ("b", "auth.json")):
        with pytest.raises(paths.NotReadBelow) as refused:
            paths.read_below(root, names, 1024)
        assert SECRET not in str(refused.value)


def test_no_name_climbs_out_of_the_root(tmp_path: Path, secret: Path) -> None:
    """``..``, ``.``, an empty name and one with a separator are refused before anything is
    opened. Mutant (the check dropped): ``a/../../outside/auth.json`` reads the secret."""
    root = tmp_path / "home"
    (root / "a").mkdir(parents=True)
    for names in (
        ("a", "..", "..", "outside", "auth.json"),
        ("a", ".", "f"),
        ("", "f"),
        ("a/../../outside", "auth.json"),
        (),
    ):
        with pytest.raises(paths.NotReadBelow):
            paths.read_below(root, names, 1024)


def test_a_directory_is_refused(tmp_path: Path) -> None:
    """Mutant (the regular-file check dropped): a read of the directory is not a refusal."""
    (tmp_path / "a" / "f.md").mkdir(parents=True)
    with pytest.raises(paths.NotReadBelow, match="a directory"):
        paths.read_below(tmp_path, ("a", "f.md"), 1024)


def test_a_file_over_the_limit_is_refused_and_one_at_it_is_read(tmp_path: Path) -> None:
    """Mutant (no cap): the oversize file is read whole."""
    (tmp_path / "big.md").write_bytes(b"x" * 1025)
    (tmp_path / "just.md").write_bytes(b"x" * 1024)
    with pytest.raises(paths.NotReadBelow, match="over 1,024"):
        paths.read_below(tmp_path, ("big.md",), 1024)
    assert len(paths.read_below(tmp_path, ("just.md",), 1024).data) == 1024


@posix
def test_a_fifo_is_refused_at_once_never_waited_on(tmp_path: Path) -> None:
    """Mutants: ``O_NONBLOCK`` dropped (the open waits for a writer: blocked), the
    regular-file check dropped (the FIFO reads as an empty file)."""
    if sys.platform == "win32":  # what mypy narrows; the mark alone it does not read
        pytest.skip("FIFOs are POSIX's")
    fifo = tmp_path / "f.md"
    os.mkfifo(fifo)

    def read() -> object:
        return paths.read_below(tmp_path, ("f.md",), 1024)

    with pytest.raises(paths.NotReadBelow, match="a FIFO"):
        _within(5, read, fifo)


@posix
def test_a_link_to_dev_zero_is_refused_at_once(tmp_path: Path) -> None:
    (tmp_path / "f.md").symlink_to("/dev/zero")
    with pytest.raises(paths.NotReadBelow):
        _within(5, lambda: paths.read_below(tmp_path, ("f.md",), 1024))


# --- the start pack, as a manager's SessionStart hook builds it ---------------------------


@links
def test_the_start_pack_never_quotes_what_a_link_in_the_state_dir_leads_to(
    work: Path, monkeypatch: pytest.MonkeyPatch, secret: Path
) -> None:
    """The exfiltration: ``state.md`` a link to a secret outside the home. The pack says the
    file was not read and why, and holds none of the secret."""
    state = _state(work)
    state.symlink_to(secret)

    pack = _pack(monkeypatch, work)

    assert SECRET not in pack
    assert f"Resume: your state file {state} was not read (" in pack, pack
    assert "write it there again as a plain file before you stop." in pack


@posix
@pytest.mark.parametrize("plant", ["dev-zero", "fifo", "directory", "oversize"])
def test_the_start_pack_says_why_it_read_no_state_file_and_never_hangs(
    work: Path, monkeypatch: pytest.MonkeyPatch, plant: str
) -> None:
    """``/dev/zero`` (the read grew to ~13.9 GB in a second), a FIFO (the hook hung 120 s), a
    directory and a file over ``paths.MANAGER_STATE_LIMIT``: each is a line saying why, within
    seconds, and nothing of it in the pack."""
    if sys.platform == "win32":  # what mypy narrows; the mark alone it does not read
        pytest.skip("FIFOs and /dev/zero are POSIX's")
    state = _state(work)
    if plant == "dev-zero":
        state.symlink_to("/dev/zero")
    elif plant == "fifo":
        os.mkfifo(state)
    elif plant == "directory":
        state.mkdir()
    else:
        state.write_text("OVERSIZE " * (paths.MANAGER_STATE_LIMIT // 9 + 1), encoding="utf-8")

    pack = _pack(monkeypatch, work)

    assert f"Resume: your state file {state} was not read (" in pack, pack
    assert "OVERSIZE" not in pack
    assert len(pack) <= 20_000


def test_a_plain_state_file_is_still_quoted(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state(work)
    state.write_text("goal: ship N1\nnext: review #78\n", encoding="utf-8")

    pack = _pack(monkeypatch, work)

    assert f"Resume: your state file {state} (written " in pack
    assert "next: review #78" in pack


# --- the quiet compaction's freshness --------------------------------------------------------


@links
def test_a_link_never_passes_for_a_fresh_state_file(work: Path, secret: Path) -> None:
    """A quiet compaction waits for a fresh state file: a link to a file someone else keeps
    fresh was taken for one, by the age of what it leads to."""
    state = _state(work)
    state.symlink_to(secret)
    project = team_project(work).id

    said = quiet_compact._state_age(project)

    assert isinstance(said, str) and f"its state file at {state} was not read (" in said
    state.unlink()
    state.write_text("goal", encoding="utf-8")
    written = quiet_compact._state_age(project)
    assert isinstance(written, datetime)
    assert abs((datetime.now(UTC) - written).total_seconds()) < 60
