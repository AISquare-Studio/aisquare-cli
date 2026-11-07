"""aisquare reads a manager's state file below the home, never through a link, a regular file
of a bounded size or not at all (#78 reopen, SECURITY).

The state directory is the one a sandboxed manager's launch lets its sandbox write
(SMOKE13 N1), and aisquare reads ``state.md`` back outside any sandbox: the start pack at
every manager's SessionStart quotes it, and a quiet compaction goes by its age. The tester
planted ``state.md`` from a real Codex manager's sandbox as a link to ``/dev/zero`` (one read
grew to ~13.9 GB in a second) and as a FIFO (the hook hung); a link to a secret outside
(``~/.ssh/…``, ``~/.codex/auth.json``) would have put it in the start pack. Every manager seat
shares the directory, so a sandboxed Codex ``manager2`` plants for a Claude Code ``manager``'s
start too: the rule is the reader's, whatever the seat or the agent. Each plant is refused with
a line saying why, quickly, its content never quoted: ``paths.read_below`` walks from the home
one name at a time with ``O_NOFOLLOW``, opens the file ``O_NONBLOCK`` and reads only a regular
file of at most ``paths.MANAGER_STATE_LIMIT`` bytes; ``paths.stat_below`` gives its ``lstat``.

Every pin is bounded: in a thread with a timeout (:func:`_in_time`, as
tests/test_a_brokered_detail_file_reads_only_the_tree.py bounds its broker), and a read of
``/dev/zero`` in a child process ended on time and on memory (:func:`_in_a_child`), so a
reader that reads without end (a plain ``read_text``, the mutant) fails inside the bound and
never runs unbounded.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
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
#: How long a read may take before the pin fails: far past a bounded one's milliseconds.
WAIT = 10.0
#: The most a child reading the state file may grow to before it is ended (KiB).
MAX_RSS_KIB = 512 * 1024
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


def _in_time(call: Callable[[], T], fifo: Path | None = None) -> T:
    """``call()``'s answer, given within :data:`WAIT`; past it the test fails, and a FIFO the
    call waits on is opened for writing, which lets it go, so it does not outlive the test."""
    answers: dict[str, object] = {}

    def run() -> None:
        try:
            answers["value"] = call()
        except BaseException as exc:
            answers["error"] = exc

    asking = threading.Thread(target=run, daemon=True)
    asking.start()
    asking.join(WAIT)
    held = asking.is_alive()
    if held and fifo is not None and sys.platform != "win32":
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        asking.join(WAIT)
    assert not held, f"the read did not end within {WAIT} s"
    if "error" in answers:
        raise answers["error"]  # type: ignore[misc]
    return answers["value"]  # type: ignore[return-value]


def _rss_kib(pid: int) -> int:
    """The resident size of process ``pid`` in KiB; 0 once it is gone."""
    if sys.platform == "win32":  # no /dev/zero to read there, so nothing to measure
        return 0
    statm = Path(f"/proc/{pid}/statm")
    if statm.exists():
        try:
            return int(statm.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 1024
        except (OSError, IndexError, ValueError):
            return 0
    said = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(said.stdout.strip() or 0)


def _in_a_child(project_id: str, work: Path) -> str:
    """The start pack's resume lines (``team._state_lines``, what every manager's SessionStart
    quotes) for ``project_id``, built in a child process that is ended past :data:`WAIT` or
    past :data:`MAX_RSS_KIB`: a read of ``/dev/zero`` grows by gigabytes a second, which no
    thread can be stopped from doing."""
    code = (
        "import sys; from datetime import UTC, datetime; "
        "from aisquare.services import team; "
        "print('\\n'.join(team._state_lines(sys.argv[1], datetime.now(UTC))))"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, project_id],
        cwd=work,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline, grown = time.monotonic() + WAIT, 0
    while child.poll() is None:
        grown = max(grown, _rss_kib(child.pid))
        if grown > MAX_RSS_KIB or time.monotonic() > deadline:
            child.kill()
            child.communicate()
            raise AssertionError(f"the read did not stay bounded: {grown:,} KiB, ended")
        time.sleep(0.005)
    out, err = child.communicate()
    assert child.returncode == 0, err
    return out


def _pack(monkeypatch: pytest.MonkeyPatch, work: Path, role: str = "manager") -> str:
    """A manager seat's start pack, as its SessionStart hook injects it, within :data:`WAIT`."""
    with monkeypatch.context() as patch:
        patch.setenv("AISQUARE_ROLE", role)
        return _in_time(lambda: team_service.hook_session_start(MANAGER, work, "startup"))


# --- read_below and stat_below ------------------------------------------------------------


def test_a_regular_file_is_read_with_the_stat_it_was_read_through(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.md").write_bytes(b"goal: ship")

    read = paths.read_below(tmp_path, ("a", "b", "f.md"), 64)

    assert read.data == b"goal: ship" and read.info.st_size == 10
    assert paths.stat_below(tmp_path, ("a", "b", "f.md")).st_size == 10
    for missing in (("a", "b", "missing.md"), ("nothing", "f.md")):
        with pytest.raises(FileNotFoundError):
            paths.read_below(tmp_path, missing, 64)
        with pytest.raises(FileNotFoundError):
            paths.stat_below(tmp_path, missing)


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
            _in_time(lambda names=names: paths.read_below(root, names, 1024))  # type: ignore[misc]
        assert SECRET not in str(refused.value)
        with pytest.raises(paths.NotReadBelow):
            paths.stat_below(root, names)


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
        with pytest.raises(paths.NotReadBelow):
            paths.stat_below(root, names)


def test_a_directory_is_refused(tmp_path: Path) -> None:
    """Mutant (the regular-file check dropped): a read of the directory is not a refusal."""
    (tmp_path / "a" / "f.md").mkdir(parents=True)
    with pytest.raises(paths.NotReadBelow, match="a directory"):
        paths.read_below(tmp_path, ("a", "f.md"), 1024)
    with pytest.raises(paths.NotReadBelow, match="a directory"):
        paths.stat_below(tmp_path, ("a", "f.md"))


def test_a_file_over_the_limit_is_refused_and_one_at_it_is_read(tmp_path: Path) -> None:
    """Mutant (no cap): the oversize file is read whole."""
    (tmp_path / "big.md").write_bytes(b"x" * 1025)
    (tmp_path / "just.md").write_bytes(b"x" * 1024)
    with pytest.raises(paths.NotReadBelow, match="over 1,024"):
        paths.read_below(tmp_path, ("big.md",), 1024)
    assert len(paths.read_below(tmp_path, ("just.md",), 1024).data) == 1024


def test_the_windows_walk_refuses_what_the_posix_one_does(tmp_path: Path, secret: Path) -> None:
    """Windows' branch (``_open_below_windows``: each name ``lstat``-ed, then opened), run on
    every platform: a directory is refused before it is opened (Windows raised
    ``PermissionError`` opening one, CI e1778119), and so is a link, at the file or on the
    way; a regular file opens."""
    (tmp_path / "a" / "dir.md").mkdir(parents=True)
    (tmp_path / "a" / "f.md").write_text("goal", encoding="utf-8")
    with pytest.raises(paths.NotReadBelow, match="a directory"):
        paths._open_below_windows(tmp_path, ("a", "dir.md"))
    os.close(paths._open_below_windows(tmp_path, ("a", "f.md")))
    if not can_symlink():
        return
    (tmp_path / "a" / "l.md").symlink_to(secret)
    (tmp_path / "b").symlink_to(secret.parent, target_is_directory=True)
    for names in (("a", "l.md"), ("b", "auth.json")):
        with pytest.raises(paths.NotReadBelow):
            paths._open_below_windows(tmp_path, names)


@pytest.mark.parametrize("swap", ["file", "link"])
def test_the_windows_walk_refuses_a_file_swapped_in_after_its_lstat(
    tmp_path: Path, secret: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    """Windows' branch ``lstat``-s each name, then opens the file by its name, which follows
    a link: ``state.md`` swapped for a link to a secret (or another file) between the two
    was read (review r39 of #113, finding 4). Run on every platform, the swap made right
    after the ``lstat``: the descriptor is not the file the ``lstat`` found, so it is refused,
    the secret never read. Mutant (the ``fstat`` and ``lstat`` not compared): the swapped-in
    file opens."""
    if swap == "link" and not can_symlink():
        pytest.skip("this machine makes no symbolic link")
    (tmp_path / "a").mkdir()
    state = tmp_path / "a" / "f.md"
    state.write_text("goal", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text(SECRET, encoding="utf-8")
    walked = paths._lstat_below_windows

    def then_swapped(root: Path, names: tuple[str, ...]) -> os.stat_result:
        found = walked(root, names)
        if swap == "link":
            state.unlink()
            state.symlink_to(secret)
        else:
            os.replace(other, state)
        return found

    monkeypatch.setattr(paths, "_lstat_below_windows", then_swapped)
    with pytest.raises(paths.NotReadBelow, match="put in another file's place") as refused:
        paths._open_below_windows(tmp_path, ("a", "f.md"))
    assert SECRET not in str(refused.value)


@posix
def test_a_fifo_is_refused_at_once_never_waited_on(tmp_path: Path) -> None:
    """Mutants: ``O_NONBLOCK`` dropped (the open waits for a writer: past the bound), the
    regular-file check dropped (the FIFO reads as an empty file)."""
    if sys.platform == "win32":  # what mypy narrows; the mark alone it does not read
        pytest.skip("FIFOs are POSIX's")
    fifo = tmp_path / "f.md"
    os.mkfifo(fifo)

    with pytest.raises(paths.NotReadBelow, match="a FIFO"):
        _in_time(lambda: paths.read_below(tmp_path, ("f.md",), 1024), fifo)
    with pytest.raises(paths.NotReadBelow, match="a FIFO"):
        _in_time(lambda: paths.stat_below(tmp_path, ("f.md",)), fifo)


@posix
def test_a_link_to_dev_zero_is_refused_at_once(tmp_path: Path) -> None:
    (tmp_path / "f.md").symlink_to("/dev/zero")
    with pytest.raises(paths.NotReadBelow):
        _in_time(lambda: paths.read_below(tmp_path, ("f.md",), 1024))


# --- the start pack, as a manager seat's SessionStart hook builds it ------------------------


@links
@pytest.mark.parametrize("role", ["manager", "manager2"])
def test_the_start_pack_never_quotes_what_a_link_in_the_state_dir_leads_to(
    work: Path, monkeypatch: pytest.MonkeyPatch, secret: Path, role: str
) -> None:
    """The exfiltration: ``state.md`` a link to a secret outside the home, planted by any seat
    that shares the directory, read at any manager seat's start. The pack says the file was
    not read and why, and holds none of the secret."""
    state = _state(work)
    state.symlink_to(secret)

    pack = _pack(monkeypatch, work, role)

    assert SECRET not in pack
    assert f"Resume: your state file {state} was not read (" in pack, pack
    assert "write it there again, a plain file of at most 4,000 chars, before you stop." in pack


@posix
@pytest.mark.parametrize("role", ["manager", "manager2"])
@pytest.mark.parametrize("plant", ["fifo", "directory", "oversize"])
def test_the_start_pack_says_why_it_read_no_state_file_and_never_hangs(
    work: Path, monkeypatch: pytest.MonkeyPatch, plant: str, role: str
) -> None:
    """A FIFO (the hook hung), a directory and a file over ``paths.MANAGER_STATE_LIMIT``: each
    is a line saying why, within the bound, and nothing of it in the pack, at every manager
    seat's start."""
    if sys.platform == "win32":  # what mypy narrows; the mark alone it does not read
        pytest.skip("FIFOs are POSIX's")
    state = _state(work)
    if plant == "fifo":
        os.mkfifo(state)
    elif plant == "directory":
        state.mkdir()
    else:
        state.write_text("OVERSIZE " * (paths.MANAGER_STATE_LIMIT // 9 + 1), encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setenv("AISQUARE_ROLE", role)
        pack = _in_time(
            lambda: team_service.hook_session_start(MANAGER, work, "startup"),
            state if plant == "fifo" else None,
        )

    assert f"Resume: your state file {state} was not read (" in pack, pack
    assert "OVERSIZE" not in pack
    assert len(pack) <= 20_000


@posix
def test_a_link_to_dev_zero_is_a_line_and_its_read_stays_bounded(work: Path) -> None:
    """The ~13.9 GB read: in a child ended on time and memory, so a reader that reads it whole
    (a plain ``read_text``, the mutant) fails here inside the bound, and never runs on."""
    state = _state(work)
    state.symlink_to("/dev/zero")

    said = _in_a_child(team_project(work).id, work)

    assert f"Resume: your state file {state} was not read (" in said, said


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
    fresh was taken for one, by a ``stat`` that followed it. Mutant (a plain ``stat`` back):
    dies here."""
    state = _state(work)
    state.symlink_to(secret)
    project = team_project(work).id

    said = _in_time(lambda: quiet_compact._state_age(project))

    assert isinstance(said, str) and f"its state file at {state} was not read (" in said
    state.unlink()
    state.write_text("goal", encoding="utf-8")
    written = quiet_compact._state_age(project)
    assert isinstance(written, datetime)
    assert abs((datetime.now(UTC) - written).total_seconds()) < 60


@posix
def test_a_fifo_is_no_state_file_to_a_quiet_compaction(work: Path) -> None:
    if sys.platform == "win32":  # what mypy narrows; the mark alone it does not read
        pytest.skip("FIFOs are POSIX's")
    state = _state(work)
    os.mkfifo(state)

    said = _in_time(lambda: quiet_compact._state_age(team_project(work).id), state)

    assert isinstance(said, str) and "a FIFO" in said, said
