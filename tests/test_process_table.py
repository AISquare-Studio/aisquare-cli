"""A pane's processes and the files they hold open (``core.process_table``, #113's #24).

What ties a Codex rollout to the row whose agent wrote it: the process that writes one holds
it open, and that process is below the pane's own. These run against real processes (a child
tree, a child holding a file) on whichever platform runs them (``lsof`` on macOS,
``/proc/<pid>/fd`` where there is one), and against a made-up ``/proc`` for the Linux branch
and its refusals, which this machine may not have.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from aisquare.core import process_table
from aisquare.core.process_table import PaneProcesses


@pytest.fixture(autouse=True)
def forgotten() -> Iterator[None]:
    process_table._HELD.clear()
    yield
    process_table._HELD.clear()


def _until(check: object, what: str, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():  # type: ignore[operator]
        if time.monotonic() >= deadline:
            raise AssertionError(what)
        time.sleep(0.05)


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[str], Path]]:
    """A child that opened ``held.txt`` and sits on it."""
    path = tmp_path / "held.txt"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time; f = open({str(path)!r}, 'w'); print('ready', flush=True); "
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stdout.readline().strip() == "ready"
    try:
        yield child, path
    finally:
        child.kill()
        child.wait()
        child.stdout.close()


# --- the tree


def test_a_panes_tree_is_its_pid_and_everything_below_it_and_nothing_beside_it() -> None:
    """The pane's pid, its child, and that child's own, as Codex's is (the pane's pid is its
    node wrapper's, the native binary that holds the rollout is a child of that, measured on
    0.164.0): not the process that asked, nor a sibling of the pane's."""
    pane = subprocess.Popen(["sh", "-c", "sh -c 'sleep 60 & wait' & wait"])
    sibling = subprocess.Popen(["sleep", "60"])
    try:
        _until(
            lambda: len(process_table.descendants(pane.pid) or ()) == 3,
            "the pane's sleep never showed two below it",
        )
        tree = process_table.descendants(pane.pid)
        assert tree is not None and pane.pid in tree
        assert os.getpid() not in tree and sibling.pid not in tree
    finally:
        for process in (pane, sibling):
            process.kill()
            process.wait()


def test_a_tree_ps_cannot_give_is_not_told(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", refuse)
    assert process_table.descendants(os.getpid()) is None

    def failing(*args: object, **kwargs: object) -> object:
        return subprocess.CompletedProcess(["ps"], 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", failing)
    assert process_table.descendants(os.getpid()) is None


# --- the files, on this platform


def test_a_file_a_process_holds_is_among_its_open_files_and_one_it_closed_is_not(
    holder: tuple[subprocess.Popen[str], Path],
) -> None:
    child, path = holder

    held = process_table.open_files([child.pid])

    assert held is not None and os.path.realpath(path) in held
    assert process_table.open_files([os.getpid()]) is not None
    assert os.path.realpath(path) not in (process_table.open_files([os.getpid()]) or ())


def test_a_file_opened_through_a_link_is_named_by_where_it_is(tmp_path: Path) -> None:
    """The home a row records may reach its rollouts through a link (``/var`` on macOS); the
    system names the file by its real path, so that is what the pane's files are compared by."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    child = subprocess.Popen(
        [sys.executable, "-c", f"import time; f = open({str(link / 'x')!r}, 'w'); time.sleep(60)"]
    )
    try:
        _until(
            lambda: str(real / "x") in (process_table.open_files([child.pid]) or ()),
            "the file was not seen by its real path",
        )
        assert str(link / "x") not in (process_table.open_files([child.pid]) or ())
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="no lsof")
def test_lsof_alone_names_the_files_too_wherever_there_is_no_proc(
    holder: tuple[subprocess.Popen[str], Path], tmp_path: Path
) -> None:
    child, path = holder

    held = process_table.open_files([child.pid], proc=tmp_path / "no-proc")

    assert held is not None and os.path.realpath(path) in held


def test_lsof_that_sees_no_process_or_cannot_run_is_not_told(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing printed is a process it could not see, never "holds nothing": a refused ``lsof``
    would otherwise read every rollout as another agent's."""

    def silent(*args: object, **kwargs: object) -> object:
        return subprocess.CompletedProcess(["lsof"], 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", silent)
    assert process_table.open_files([os.getpid()], proc=tmp_path / "no-proc") is None

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(subprocess, "run", missing)
    assert process_table.open_files([os.getpid()], proc=tmp_path / "no-proc") is None

    def answering(*args: object, **kwargs: object) -> object:
        out = "p10\nn/home/x/rollout-a.jsonl\nn->0xdeadbeef\nn*:8080\np11\nn/home/x/b\n"
        return subprocess.CompletedProcess(["lsof"], 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", answering)
    assert process_table.open_files([10, 11], proc=tmp_path / "no-proc") == frozenset(
        {"/home/x/rollout-a.jsonl", "/home/x/b"}
    )


def test_lsof_output_this_locale_cannot_decode_is_not_told(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name that is not valid in the reader's encoding fails the decode of the whole output:
    that is "cannot tell" as any other failure of ``lsof`` is, never an error out of a listing."""

    def undecodable(*args: object, **kwargs: object) -> object:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(subprocess, "run", undecodable)

    assert process_table.open_files([os.getpid()], proc=tmp_path / "no-proc") is None


def test_lsofs_escaped_names_are_read_back_to_the_paths_they_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Under ``LC_ALL=C`` ``lsof`` writes a byte it cannot print as ``\\xNN`` (``/Users/Émile``
    is ``/Users/\\xc3\\x89mile``), a backslash as ``\\\\`` and a tab as ``\\t`` (measured on
    macOS), so the pane's own rollout, held by that name, matched no real path: its message
    read as not taken (tester-24b)."""

    def answering(*args: object, **kwargs: object) -> object:
        out = (
            "p10\n"
            "n/Users/\\xc3\\x89mile/.codex/sessions/rollout-a.jsonl\n"
            "n/Users/Jos\\xc3\\xa9/\\xe2\\x82\\xac/b\n"
            "n/a/back\\\\slash/c\n"
            "n/a/lit\\\\x41/d\n"  # an escaped backslash, then a plain ``x41``
            "n/a/tab\\tx/e\n"
            "n/a/lone\\xffbyte/f\n"
        )
        return subprocess.CompletedProcess(["lsof"], 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", answering)

    held = process_table.open_files([10], proc=tmp_path / "no-proc")

    assert held == frozenset(
        {
            "/Users/Émile/.codex/sessions/rollout-a.jsonl",
            "/Users/José/€/b",
            "/a/back\\slash/c",
            "/a/lit\\x41/d",
            "/a/tab\tx/e",
            "/a/lone\udcffbyte/f",  # as ``os.fsdecode`` keeps a byte that is not UTF-8
        }
    )


@pytest.mark.skipif(shutil.which("lsof") is None, reason="no lsof")
@pytest.mark.parametrize("locale", ["C", "en_US.UTF-8"])
def test_a_file_under_a_non_ascii_folder_is_found_whatever_the_locale_of_lsof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    """Real ``lsof`` on a real process: the folder ``Émile`` is named, under ``LC_ALL=C``
    (which escapes it) and under a UTF-8 locale (which prints it), by the path the file has."""
    monkeypatch.setenv("LC_ALL", locale)
    folder = tmp_path / "Émile"
    folder.mkdir()
    path = folder / "held.txt"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time; f = open({str(path)!r}, 'w'); print('ready', flush=True); "
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "ready"

        held = process_table.open_files([child.pid], proc=tmp_path / "no-proc")

        assert held is not None and os.path.realpath(path) in held
    finally:
        child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def test_no_process_is_no_files() -> None:
    assert process_table.open_files([]) == frozenset()


# --- Linux's /proc, made up


def _proc(tmp_path: Path, **pids: dict[str, str]) -> Path:
    """A ``/proc`` of ``{pid: {fd: target}}``."""
    proc = tmp_path / "proc"
    for pid, fds in pids.items():
        fd_dir = proc / pid.removeprefix("p") / "fd"
        fd_dir.mkdir(parents=True)
        for fd, target in fds.items():
            (fd_dir / fd).symlink_to(target)
    return proc


def test_proc_gives_the_absolute_paths_the_descriptors_point_at(tmp_path: Path) -> None:
    proc = _proc(
        tmp_path,
        p10={"0": "/dev/null", "3": "/home/x/rollout-a.jsonl", "4": "pipe:[123]"},
        p11={"3": "/home/x/rollout-b.jsonl"},
    )

    assert process_table.open_files([10, 11], proc=proc) == frozenset(
        {"/dev/null", "/home/x/rollout-a.jsonl", "/home/x/rollout-b.jsonl"}
    )
    assert process_table.open_files([11], proc=proc) == frozenset({"/home/x/rollout-b.jsonl"})


def test_proc_holds_nothing_for_a_process_that_exited_since_ps(tmp_path: Path) -> None:
    proc = _proc(tmp_path, p10={"3": "/home/x/a"})

    assert process_table.open_files([10, 99], proc=proc) == frozenset({"/home/x/a"})


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,  # mypy narrows on the first; Windows has no uid
    reason="a /proc to hide a process from is POSIX's, and root reads every process",
)
def test_proc_that_hides_a_process_descriptors_is_not_told_never_read_as_holding_none(
    tmp_path: Path,
) -> None:
    """A process that marks itself non-dumpable answers EACCES for its ``fd`` directory: not
    knowing is not "holds nothing", or a row's own rollout would read as another agent's."""
    proc = _proc(tmp_path, p10={"3": "/home/x/a"}, p11={"3": "/home/x/b"})
    hidden = proc / "11" / "fd"
    hidden.chmod(0)
    try:
        assert process_table.open_files([10, 11], proc=proc) is None
        assert process_table.open_files([10], proc=proc) == frozenset({"/home/x/a"})
    finally:
        hidden.chmod(0o755)


# --- what a pane holds, kept a moment


def test_what_a_pane_holds_is_read_once_per_moment_however_many_listings_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ps`` and ``lsof`` cost 0.1 to 0.2 s on a loaded machine and a listing comes every second
    or two for as long as a row is unsent."""
    asked: list[int] = []

    def tree(root: int) -> frozenset[int] | None:
        asked.append(root)
        return frozenset({root, root + 1})

    monkeypatch.setattr(process_table, "descendants", tree)
    monkeypatch.setattr(process_table, "open_files", lambda pids: frozenset({f"/f{max(pids)}"}))
    monkeypatch.setattr(process_table, "HELD_FOR", 1000.0)

    for _ in range(3):
        assert process_table.held_by_pane(7) == frozenset({"/f8"})
    assert process_table.held_by_pane(9) == frozenset({"/f10"})
    assert asked == [7, 9]

    monkeypatch.setattr(process_table, "HELD_FOR", 0.0)
    process_table.held_by_pane(7)
    process_table.held_by_pane(7)
    assert asked == [7, 9, 7, 7], "a moment that has passed is read again"


def test_a_pane_that_could_not_be_read_is_not_told_and_asked_again_after_the_moment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def tree(root: int) -> frozenset[int] | None:
        asked.append(root)
        return None

    monkeypatch.setattr(process_table, "descendants", tree)
    monkeypatch.setattr(process_table, "HELD_FOR", 1000.0)

    assert process_table.held_by_pane(7) is None
    assert process_table.held_by_pane(7) is None
    assert asked == [7], "a refusal is kept too: a timeout is not paid for on every listing"


def test_a_hung_lsof_is_waited_for_once_a_moment_not_on_every_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tester-24b: an ``lsof`` that hangs (a stale NFS mount does that) runs to its timeout, and
    the answer was stamped with the time the read began, so it was already older than
    :data:`HELD_FOR` and asked again by the next listing, for every row: K rows, 5 K seconds.
    Stamped when the read ended, the next listing inside the moment makes no call."""
    now = [1000.0]
    calls: list[str] = []

    def run(argv: list[str], **kwargs: object) -> object:
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, stdout="7 1\n8 7\n", stderr="")
        calls.append(argv[0])
        now[0] += process_table._TIMEOUT  # the wait runs out
        raise subprocess.TimeoutExpired(argv, process_table._TIMEOUT)

    real_open_files = process_table.open_files
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(process_table, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(
        process_table, "open_files", lambda pids: real_open_files(pids, proc=tmp_path / "no-proc")
    )
    assert process_table._TIMEOUT > process_table.HELD_FOR, "the case this is about"

    assert process_table.held_by_pane(7) is None
    assert process_table.held_by_pane(7) is None
    assert process_table.held_by_pane(7) is None
    assert calls == ["lsof"], "a hung lsof is paid for once, not on every listing"

    now[0] += process_table.HELD_FOR  # the moment passes
    assert process_table.held_by_pane(7) is None
    assert calls == ["lsof", "lsof"]


def test_the_kept_panes_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_table, "descendants", lambda root: frozenset({root}))
    monkeypatch.setattr(process_table, "open_files", lambda pids: frozenset())
    monkeypatch.setattr(process_table, "_HELD_MAX", 3)

    for root in range(1, 8):
        process_table.held_by_pane(root)

    assert sorted(process_table._HELD) == [5, 6, 7]


# --- the pane, as a listing asks


def _fake_table(monkeypatch: pytest.MonkeyPatch, *, files: frozenset[str] | None) -> None:
    monkeypatch.setattr(process_table, "descendants", lambda root: frozenset({root}))
    monkeypatch.setattr(process_table, "open_files", lambda pids: files)
    monkeypatch.setattr(process_table, "HELD_FOR", 0.0)


def test_a_pane_gives_the_files_it_holds_below_the_folder_asked_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    mine = os.path.realpath(home / "sessions") + "/rollout-a.jsonl"
    elsewhere = os.path.realpath(tmp_path) + "/sessions-and-more/rollout-b.jsonl"
    _fake_table(monkeypatch, files=frozenset({mine, elsewhere, "/dev/null"}))

    pane = PaneProcesses(lambda: 42)

    assert pane.held_below(home / "sessions") == frozenset({mine})


def test_a_folder_is_met_by_its_real_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    (real / "sessions").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    held = os.path.realpath(real / "sessions") + "/rollout-a.jsonl"
    _fake_table(monkeypatch, files=frozenset({held}))

    assert PaneProcesses(lambda: 42).held_below(link / "sessions") == frozenset({held})


def test_a_pane_whose_pid_or_files_cannot_be_told_is_not_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_table(monkeypatch, files=frozenset({"/x"}))
    assert PaneProcesses(lambda: None).held_below(tmp_path) is None

    def gone() -> int:
        raise OSError("the pane is gone")

    assert PaneProcesses(gone).held_below(tmp_path) is None
    _fake_table(monkeypatch, files=None)
    assert PaneProcesses(lambda: 42).held_below(tmp_path) is None


def test_the_panes_pid_is_asked_of_tmux_once_however_many_folders_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_table(monkeypatch, files=frozenset())
    asked: list[int] = []

    def pid() -> int:
        asked.append(1)
        return 42

    pane = PaneProcesses(pid)
    pane.held_below(tmp_path)
    pane.held_below(tmp_path / "other")

    assert asked == [1]
