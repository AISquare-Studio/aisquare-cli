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
    process_table._BLIND_UNTIL[0] = 0.0
    yield
    process_table._HELD.clear()
    process_table._BLIND_UNTIL[0] = 0.0


#: The real-process tests need the tools the table reads: ``ps`` for the tree, and for the files
#: ``/proc`` where there is one, else ``lsof``. Windows has none of them, and a machine with a
#: PATH that lacks one (macOS without /usr/sbin has no ``lsof``) skips what asks for it: the
#: table tells nothing there (``None``), which ``test_a_tree_ps_cannot_give_is_not_told`` and
#: the mocked tests below pin on every platform.
needs_ps = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("ps") is None,
    reason="no ps on this machine: the tree is not told",
)
needs_a_file_table = pytest.mark.skipif(
    sys.platform == "win32" or not (process_table.PROC.is_dir() or shutil.which("lsof")),
    reason="no /proc and no lsof on this machine: the files are not told",
)


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


@needs_ps
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


@needs_a_file_table
def test_a_file_a_process_holds_is_among_its_open_files_and_one_it_closed_is_not(
    holder: tuple[subprocess.Popen[str], Path],
) -> None:
    child, path = holder

    held = process_table.open_files([child.pid])

    assert held is not None and os.path.realpath(path) in held
    assert process_table.open_files([os.getpid()]) is not None
    assert os.path.realpath(path) not in (process_table.open_files([os.getpid()]) or ())


@needs_a_file_table
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


# --- who is connected to a server's socket

#: ``lsof -n -P -w -a -U -F pdn`` on Codex 0.164.0's managed daemon (8465) and the native
#: binary of a TUI on it (8455), as macOS printed them (r31): the daemon's listening socket
#: and the one it accepted the TUI on are named by the path it listens on, the TUI's by the
#: address of the daemon's end, and each one's own pairs by each other.
DAEMON_AND_TUI = """p8455
f7
d0x1d1432ccc9653db0
n->0xadb288b360af64ac
f8
d0xadb288b360af64ac
n->0x1d1432ccc9653db0
f34
d0xbd36af0859b1a9e0
n->0x82db500f89b6c624
p8465
f7
d0x23914f2eff05f361
n->0x4efb5832c07efb84
f8
d0x4efb5832c07efb84
n->0x23914f2eff05f361
f32
d0x7a8355fc85ebbfac
n/private/tmp/codex-daemon-501/4277e226934b024274daf3c36c8922af8d10a0b42f9a4c9ce9351b810b1f01ed
f33
d0x82db500f89b6c624
n/private/tmp/codex-daemon-501/4277e226934b024274daf3c36c8922af8d10a0b42f9a4c9ce9351b810b1f01ed
"""


def _lsof_says(monkeypatch: pytest.MonkeyPatch, out: str | None) -> list[list[str]]:
    """``lsof`` printing ``out`` (``None``: it could not run); the commands it was given."""
    asked: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        asked.append(argv)
        return None if out is None else subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(process_table, "_run", run)
    return asked


def test_a_servers_client_is_the_process_whose_socket_is_connected_to_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The TUI is the daemon's client by its socket's peer, one of the daemon's on the path it
    listens on. A process whose peer is another server's (another home's daemon), or one of the
    daemon's own pairs (a child of the daemon's holding one end), is none; so is one that
    holds no socket (its npm wrapper), and a process ``lsof`` did not list."""
    other = "p9000\nf5\nd0x1\nn->0x7a8355fc85ebbfa\np9001\nf5\nd0x2\nn->0x23914f2eff05f361\n"
    asked = _lsof_says(monkeypatch, DAEMON_AND_TUI + other)
    no_proc = tmp_path / "no-proc"

    connected = process_table.connected_to(8465, [8453, 8455, 9000, 9001, 8465], proc=no_proc)

    assert connected == frozenset({8455})
    assert asked == [
        ["lsof", "-n", "-P", "-w", "-a", "-U", "-F", "pdn", "-p", "8465,8453,8455,9000,9001"]
    ]
    assert process_table.connected_to(8465, [8465], proc=no_proc) == frozenset()
    assert len(asked) == 1, "nothing asked of lsof for no process"


def test_who_is_connected_is_not_told_where_the_sockets_cannot_say(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not on a machine with ``/proc``, whose ``lsof`` names no peer (unmeasured: no Linux
    Codex at hand), and never asked there; nor when ``lsof`` cannot run, nor when it does not
    list the server or lists no socket of it named by a path: "cannot tell", never "none"."""
    asked = _lsof_says(monkeypatch, DAEMON_AND_TUI)
    proc = tmp_path / "proc"
    proc.mkdir()
    assert process_table.connected_to(8465, [8455], proc=proc) is None
    assert asked == []

    no_proc = tmp_path / "no-proc"
    _lsof_says(monkeypatch, None)
    assert process_table.connected_to(8465, [8455], proc=no_proc) is None
    _lsof_says(monkeypatch, DAEMON_AND_TUI)
    assert process_table.connected_to(8466, [8455], proc=no_proc) is None, "server unlisted"
    pairs_only = DAEMON_AND_TUI.split("f32\n")[0]
    _lsof_says(monkeypatch, pairs_only)
    assert process_table.connected_to(8465, [8455], proc=no_proc) is None, "no listening one"


def test_a_descriptor_lsof_gives_no_address_takes_none_of_the_one_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A socket listed with no ``d`` line has no address, never the last descriptor's: the
    server's pair is not taken for the path it listens on, nor its pair's peer for a client."""
    _lsof_says(monkeypatch, "p1\nf3\nd0xa\nn->0xb\nf4\nn/run/s.sock\np2\nf3\nd0xc\nn->0xa\n")
    assert process_table.connected_to(1, [2], proc=tmp_path / "no-proc") is None


@pytest.mark.skipif(
    sys.platform == "win32" or process_table.PROC.is_dir() or shutil.which("lsof") is None,
    reason="the sockets say who is connected only through lsof where there is no /proc",
)
def test_a_real_client_is_told_by_its_connection_to_a_real_server() -> None:
    """Against real processes: a server listening on a Unix socket, a client connected to it,
    and a process beside them with a socket pair of its own."""
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="asqct", dir="/tmp"))
    path = str(folder / "s")
    serve = (
        "import socket, time; s = socket.socket(socket.AF_UNIX); "
        f"s.bind({path!r}); s.listen(); print('ready', flush=True); c, _ = s.accept(); "
        "time.sleep(60)"
    )
    connect = (
        "import socket, time; s = socket.socket(socket.AF_UNIX); "
        f"s.connect({path!r}); print('ready', flush=True); time.sleep(60)"
    )
    paired = (
        "import socket, time; a = socket.socketpair(); print('ready', flush=True); time.sleep(60)"
    )
    children: list[subprocess.Popen[str]] = []
    try:
        for script in (serve, connect, paired):
            child = subprocess.Popen(
                [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
            )
            children.append(child)
            assert child.stdout is not None and child.stdout.readline().strip() == "ready"
        server, client, beside = (child.pid for child in children)
        _until(
            lambda: process_table.connected_to(server, [client, beside]) == frozenset({client}),
            f"the client was never told: {process_table.connected_to(server, [client, beside])}",
        )
    finally:
        for child in children:
            child.kill()
            child.wait()
            if child.stdout is not None:
                child.stdout.close()
        shutil.rmtree(folder, ignore_errors=True)


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
    Stamped when the read ended, the next listing inside the moment makes no call, and after
    it the back-off (r24 8) keeps the tool from being asked until :data:`BACKOFF` has passed."""
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

    now[0] += process_table.HELD_FOR  # the moment passes: the tool is still being left alone
    assert process_table.held_by_pane(7) is None
    assert calls == ["lsof"]

    now[0] += process_table.BACKOFF  # and when the back-off is over, it is tried once more
    assert process_table.held_by_pane(7) is None
    assert calls == ["lsof", "lsof"]


def _hanging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, tool: str
) -> tuple[list[float], list[tuple[str, object]]]:
    """``subprocess.run`` with ``tool`` ("ps" or "lsof") hanging to its timeout; the clock
    moves by that wait. Returns the clock and each call (tool, the timeout it was given)."""
    now = [1000.0]
    calls: list[tuple[str, object]] = []

    def run(argv: list[str], **kwargs: object) -> object:
        calls.append((argv[0], kwargs.get("timeout")))
        if argv[0] == tool:
            now[0] += process_table._TIMEOUT
            raise subprocess.TimeoutExpired(argv, process_table._TIMEOUT)
        return subprocess.CompletedProcess(argv, 0, stdout="7 1\n8 7\n9 1\n", stderr="")

    real_open_files = process_table.open_files
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(process_table, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(
        process_table, "open_files", lambda pids: real_open_files(pids, proc=tmp_path / "no-proc")
    )
    return now, calls


@pytest.mark.parametrize("tool", ["lsof", "ps"])
def test_a_hung_tool_is_not_paid_for_by_every_pane_of_a_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tool: str
) -> None:
    """r24 8: the memory is per pane, so five panes with a row pending cost five timeouts on a
    listing, 25 s, on every listing for as long as the tool hangs. The first timeout makes
    every pane's answer "cannot tell" for the back-off, whichever tool it was."""
    now, calls = _hanging(monkeypatch, tmp_path, tool=tool)

    for _listing in range(3):
        for pane in (7, 70, 71, 72, 73):
            assert process_table.held_by_pane(pane) is None
        now[0] += 3.0  # the next listing, past the moment a pane's answer is kept
    assert [name for name, _ in calls].count(tool) == 1, calls

    now[0] += process_table.BACKOFF  # the back-off is over: one pane tries, and is stopped
    assert process_table.held_by_pane(7) is None
    assert [name for name, _ in calls].count(tool) == 2
    assert process_table.held_by_pane(70) is None
    assert [name for name, _ in calls].count(tool) == 2, "and the others are left alone again"


def test_the_tools_are_given_their_timeouts_and_the_back_off_is_half_a_minute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The values themselves: a tool run with no timeout would hang a listing for good, and a
    back-off of nothing is no back-off."""
    _now, calls = _hanging(monkeypatch, tmp_path, tool="never")
    process_table.descendants(7)
    process_table._from_lsof([7])

    assert calls == [("ps", 5.0), ("lsof", 5.0)]
    assert (process_table._TIMEOUT, process_table.BACKOFF) == (5.0, 30.0)
    assert process_table.HELD_FOR < process_table._TIMEOUT < process_table.BACKOFF
    # A tool that cannot be killed is waited for 2 s more, and four are left behind at most
    # (``tests/test_process_table_unkillable.py`` runs these in tenths).
    assert (process_table._GRACE, process_table._STUCK_MAX) == (2.0, 4)
    assert process_table._TIMEOUT + process_table._GRACE < process_table.BACKOFF


def test_a_ps_line_that_is_not_a_pid_pair_is_left_out_of_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = "  PID PPID\n7 1\n8 7\nnine 7\n10 x\n11\n12 7 extra\n13 8\n-14 7\n"

    def run(argv: list[str], **kwargs: object) -> object:
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    assert process_table.descendants(7) == frozenset({7, 8, 13})


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
    mine = os.path.realpath(home / "sessions") + os.sep + "rollout-a.jsonl"
    elsewhere = (
        os.path.realpath(tmp_path) + os.sep + "sessions-and-more" + os.sep + "rollout-b.jsonl"
    )
    _fake_table(monkeypatch, files=frozenset({mine, elsewhere, "/dev/null"}))

    pane = PaneProcesses(lambda: 42)

    assert pane.held_below(home / "sessions") == frozenset({mine})


def test_a_folder_is_met_by_its_real_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    (real / "sessions").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    held = os.path.realpath(real / "sessions") + os.sep + "rollout-a.jsonl"
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
