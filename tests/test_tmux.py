"""The tmux wrapper: the argv it builds, the output it reads, and what a real server does.

Two layers, because they answer different questions. The fake-runner tests pin
the CONTRACT — which arguments reach tmux for each method and how tmux's
answers are read — and run everywhere, tmux or not. The live tests (skipped
where tmux is absent) pin the ASSUMPTIONS the contract rests on: that ``=name``
is exact, that a dead pane keeps its exit status, that ``display-message`` on a
gone pane succeeds with empty fields, that every option the fleet sets — the
file at server start and the per-window ones after it — is accepted by this
tmux with the value written, that the server survives reading that file and
then holding the windows it exists for, and that a window is the size the fleet
asked for no matter who is attached (which needs a REAL client, so one live
test forks a pty and attaches one). Each live server runs on its own private
socket (``asq-test-<pid>-<n>``) and is killed in teardown, so a failing test
cannot leave a server behind or touch a real ``asq`` fleet.

Every claim carries a negative control where it admits one (CONTRIBUTING,
"Writing a guard that still guards"): exact targeting is proved by a prefix
that must NOT match, the conf check by a conf that must be rejected, the
gone-pane answer by a live pane that must NOT be reported gone.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import itertools
import os
import pty
import re
import shutil
import signal
import statistics
import struct
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from aisquare.core import tmux as tmux_module
from aisquare.core.tmux import (
    _SEP,
    BUNDLED_CONF,
    CHECK_SOCKET_SUFFIX,
    CONF_NAME,
    MIN_VERSION,
    PASTE_BUFFER,
    WINDOW_OPTIONS,
    Capture,
    Completed,
    PaneFacts,
    TmuxError,
    TmuxServer,
    TmuxUnavailable,
    WindowInfo,
    _tmux,
    parse_version,
)

OK = Completed(0, "", "")
FACTS_FIELDS = 13
WINDOW_FIELDS = 7
#: One ``set`` line of BUNDLED_CONF: the flag, the option and its value.
_SET = re.compile(r"^set (-g|-s|-ga) (\S+) (.+)$")


@pytest.fixture(autouse=True, scope="module")
def private_socket_directory() -> Iterator[Path]:
    """Send every socket path this module can compute into a directory of its own.

    A guard, not a convenience. :meth:`TmuxServer.check_conf` now sweeps tmux's
    socket directory for probe servers an earlier run abandoned and UNLINKS what
    it reclaims, and the fake-runner tests below call it with a runner that
    cannot actually kill anything. Aimed at a developer's real
    ``/tmp/tmux-<uid>``, one of those tests would unlink the socket file of a
    live probe server nobody had killed — leaving exactly the unaddressable
    server the sweep exists to remove. tmux reads ``$TMUX_TMPDIR`` and so does
    :meth:`TmuxServer.socket_path`, freshly on each call, and ``untraced_env``
    copies the variable into every real tmux the live tests start, so one
    redirection covers both layers.

    ``autouse`` at MODULE scope is the point: no test has to remember it, and a
    test added later cannot forget it. Short on purpose too — an AF_UNIX address
    has about 100 bytes and pytest's own ``tmp_path`` spends most of them on the
    test's name.
    """
    directory = Path(tempfile.mkdtemp(prefix="asq-tmux-"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TMUX_TMPDIR", str(directory))
        yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _socket_directory(server: TmuxServer) -> Path:
    """tmux's socket directory for ``server``, created if tmux has not made it yet.

    ``0o700`` because that is what tmux makes it and what it insists on finding:
    a directory this module created more permissively fails every live test on
    the same socket root with ``has unsafe permissions``.
    """
    directory = server.socket_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _plant(directory: Path, name: str, *, age: float = 0.0) -> Path:
    """A stand-in for the socket file tmux leaves behind, ``age`` seconds old."""
    path = directory / name
    path.touch()
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


# --- the fake runner -----------------------------------------------------------------


class FakeTmux:
    """A scripted runner: records every argv and stdin, answers from a queue.

    An exhausted queue answers ``OK`` so a method that makes one more call than
    the test scripted still gets a well-formed reply — and the test sees the
    extra call in ``calls`` rather than an IndexError inside the wrapper.
    """

    def __init__(self, *answers: Completed) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
        self.calls.append((list(argv), stdin))
        return self.answers.pop(0) if self.answers else OK

    def commands(self) -> list[list[str]]:
        """Each call's arguments after the fixed ``tmux -L sock -f conf`` prefix."""
        return [argv[5:] for argv, _ in self.calls]


def _facts_line(**overrides: str) -> str:
    """A ``display-message`` answer for a live 80x24 pane, fields overridable by name."""
    values = {
        "pane_id": "%3",
        "pane_width": "80",
        "pane_height": "24",
        "cursor_x": "5",
        "cursor_y": "7",
        "cursor_flag": "1",
        "alternate_on": "0",
        "history_size": "120",
        "pane_dead": "0",
        "pane_dead_status": "",
        "pane_in_mode": "0",
        "pane_current_command": "claude",
        "pane_title": "fedora",
    }
    values.update(overrides)
    return _SEP.join(values.values())


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """An executable that exists, so ``binary()`` resolves without real tmux."""
    path = tmp_path / "bin" / "tmux"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def conf(tmp_path: Path) -> Path:
    return tmp_path / "fleet.conf"


def _server(fake: FakeTmux, fake_bin: Path, conf: Path, socket: str = "sock") -> TmuxServer:
    return TmuxServer(socket, binary=str(fake_bin), conf=conf, runner=fake)


def _prefix(fake_bin: Path, conf: Path, socket: str = "sock") -> list[str]:
    return [str(fake_bin), "-L", socket, "-f", str(conf)]


def _completes(call: Callable[[], None]) -> bool:
    """True when ``call`` returns — the assertion that a call does not raise."""
    call()
    return True


# --- availability -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tmux 3.7c\n", (3, 7)),
        ("tmux next-3.5", (3, 5)),
        ("tmux 3.3a-rc2", (3, 3)),
        ("garbage", None),
        ("", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int] | None) -> None:
    assert parse_version(text) == expected


def test_a_missing_binary_is_unavailable_and_a_present_one_is_not(fake_bin: Path) -> None:
    missing = TmuxServer("sock", binary=f"asq-no-such-tmux-{os.getpid()}", runner=FakeTmux())
    assert missing.available() is False
    with pytest.raises(TmuxUnavailable, match="not installed"):
        missing.binary()
    with pytest.raises(TmuxUnavailable):
        missing.version()  # every method goes through binary()

    present = TmuxServer("sock", binary=str(fake_bin), runner=FakeTmux())
    assert present.available() is True
    assert present.binary() == str(fake_bin)


def test_version_asks_dash_V_without_a_server(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux(Completed(0, "tmux 3.7c\n", ""))
    assert _server(fake, fake_bin, conf).version() == (3, 7)
    # -V needs no socket and no conf: it must work when no server is running.
    assert fake.calls == [([str(fake_bin), "-V"], None)]

    failing = FakeTmux(Completed(1, "", "boom"))
    assert _server(failing, fake_bin, conf).version() is None


def test_require_rejects_old_accepts_new_and_fails_open_on_unparseable(
    fake_bin: Path, conf: Path
) -> None:
    old = f"tmux {MIN_VERSION[0]}.{MIN_VERSION[1] - 1}\n"
    with pytest.raises(TmuxUnavailable, match="too old"):
        _server(FakeTmux(Completed(0, old, "")), fake_bin, conf).require()

    new = f"tmux {MIN_VERSION[0]}.{MIN_VERSION[1]}\n"
    assert _completes(_server(FakeTmux(Completed(0, new, "")), fake_bin, conf).require)

    # Fail open on a banner we cannot read: a fork is not refused on a guess.
    odd = FakeTmux(Completed(0, "tmux openbsd\n", ""))
    assert _completes(_server(odd, fake_bin, conf).require)


# --- running commands ---------------------------------------------------------------------


def test_argv_names_the_socket_and_the_conf(fake_bin: Path, conf: Path) -> None:
    server = _server(FakeTmux(), fake_bin, conf, socket="asq-x")
    assert server.argv("list-sessions", "-F", "#S") == [
        *_prefix(fake_bin, conf, "asq-x"),
        "list-sessions",
        "-F",
        "#S",
    ]


def test_run_maps_a_failure_to_tmux_error_carrying_stderr(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux(Completed(1, "", "can't find pane: %9\n"))
    with pytest.raises(TmuxError, match=r"can't find pane: %9$"):
        _server(fake, fake_bin, conf).run("kill-window", "-t", "%9")

    silent = FakeTmux(Completed(2, "", ""))
    with pytest.raises(TmuxError, match="tmux kill-window -t %9 failed"):
        _server(silent, fake_bin, conf).run("kill-window", "-t", "%9")

    ok = FakeTmux(Completed(0, "out\n", "noise on stderr is not an error"))
    assert _server(ok, fake_bin, conf).run("x") == "out\n"


def test_conf_path_repairs_drift_and_leaves_a_correct_file_alone(
    fake_bin: Path, isolated_home: Path
) -> None:
    path = isolated_home / CONF_NAME
    isolated_home.mkdir(parents=True, exist_ok=True)
    path.write_text("set -g status on  # someone edited it\n", encoding="utf-8")

    assert TmuxServer("s", binary=str(fake_bin), runner=FakeTmux()).conf_path() == path
    assert path.read_text(encoding="utf-8") == BUNDLED_CONF

    # Negative control: a correct file is not rewritten (its mtime is untouched).
    ancient = 1_000_000_000
    os.utime(path, (ancient, ancient))
    server = TmuxServer("s", binary=str(fake_bin), runner=FakeTmux())
    server.conf_path()
    assert int(path.stat().st_mtime) == ancient

    # Verified once per instance: a second call does not even read the file.
    path.write_text("drift after the first check\n", encoding="utf-8")
    assert server.conf_path() == path
    assert path.read_text(encoding="utf-8") == "drift after the first check\n"
    # …and a NEW instance repairs it, which is what the next `asq` does.
    TmuxServer("s", binary=str(fake_bin), runner=FakeTmux()).conf_path()
    assert path.read_text(encoding="utf-8") == BUNDLED_CONF


def test_an_explicit_conf_is_used_verbatim_and_never_written(fake_bin: Path, conf: Path) -> None:
    server = _server(FakeTmux(), fake_bin, conf)
    assert server.conf_path() == conf
    assert not conf.exists()


def test_no_window_option_is_written_into_the_file_the_server_starts_with() -> None:
    """The split that keeps tmux alive, pinned where a future edit would undo it.

    ``window-size manual`` among the GLOBAL window options segfaults every tmux
    below 3.7 the next time a window is created — from the ``-f`` file it is the
    first ``new-session``, from a ``set -g`` on a running server it is the next
    spawn (module docstring). The fleet sets it on each window instead, so an
    option in WINDOW_OPTIONS must never also be a line of BUNDLED_CONF.
    """
    lines = [line for line in BUNDLED_CONF.splitlines() if line and not line.startswith("#")]
    rules = [_SET.match(line) for line in lines]
    assert rules and all(rules), f"every line is a `set`: {lines}"
    in_conf = {rule.group(2) for rule in rules if rule is not None}

    assert "window-size" not in in_conf, "a global window-size is what killed the server"
    assert in_conf.isdisjoint({option for option, _ in WINDOW_OPTIONS})
    # Negative control on the reading: the conf's other options ARE found this way.
    assert {"status", "remain-on-exit", "history-limit"} <= in_conf


#: The socket check_conf invents for one call: the fleet's own, the suffix, a
#: random tail. ``base`` is the fleet socket it must never be confused with.
_CHECK_SOCKET = re.compile(r"(?P<base>.+)" + re.escape(CHECK_SOCKET_SUFFIX) + r"-[0-9a-f]{8}\Z")


def _sockets_used(fake: FakeTmux) -> list[str]:
    """The ``-L`` argument of every call — the socket each one was aimed at."""
    return [argv[argv.index("-L") + 1] for argv, _ in fake.calls]


def test_check_conf_starts_a_throwaway_server_with_the_file_and_gives_it_a_window(
    fake_bin: Path, conf: Path
) -> None:
    """The check runs the conf the way the fleet does, which is the only way it can fail.

    ``-f <file>`` at server start, then a session — because a file that kills
    the server takes it down when the first WINDOW is created, not at start, and
    a check that only sourced the file onto an ``-f /dev/null`` server would
    watch the fatal line load without a word (module docstring). The probe
    window then gets what a real one gets, ``set-option`` before
    ``resize-window``, and ``source-file`` last is what turns tmux's swallowed
    complaints into stderr — with ``kill-session`` behind it, which empties the
    server so tmux's own ``exit-empty`` ends it without anybody asking.

    The socket is the fleet's plus a RANDOM tail, and both calls are aimed at
    the same one: a fixed name is shared state between concurrent checks, and
    the cure for it here is not to share.
    """
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf, socket="asq")
    assert _completes(server.check_conf)
    socket = _sockets_used(fake)[0]
    named = _CHECK_SOCKET.fullmatch(socket)
    assert named is not None, f"{socket!r} is not <socket>-check-<random>"
    assert named.group("base") == "asq", "…derived from the fleet's socket, and never it"
    assert _sockets_used(fake) == [socket, socket], "one socket, used by both calls"
    assert fake.calls == [
        ([*_prefix(fake_bin, conf, socket),
          "new-session", "-d", "-s", "asq-conf-check", "-c", "/", "-x", "80", "-y", "24",
          "--", "true",
          ";", "set-option", "-w", "-t", "=asq-conf-check:", "window-size", "manual",
          ";", "resize-window", "-t", "=asq-conf-check:", "-x", "80", "-y", "24",
          ";", "source-file", str(conf),
          ";", "kill-session", "-t", "=asq-conf-check"], None),
        ([*_prefix(fake_bin, Path(os.devnull), socket), "kill-server"], None),
    ]  # fmt: skip
    # The order inside the chain is load-bearing: a kill-session before
    # source-file would end the server before it could be asked anything.
    chained = fake.calls[0][0]
    assert chained.index("source-file") < chained.index("kill-session")

    other = conf.with_name("other.conf")
    fake = FakeTmux()
    _server(fake, fake_bin, conf, socket="asq").check_conf(other)
    argv = fake.calls[0][0]
    assert argv[:5] == _prefix(fake_bin, other, _sockets_used(fake)[0])
    # The argument source-file READS, by its position after that word — not
    # merely "appears somewhere in argv", which the -f above already guarantees.
    assert argv[argv.index("source-file") + 1] == str(other), "…the same file, sourced"
    assert str(conf) not in argv, "…and the conf the server was built with is nowhere in it"


def test_check_conf_invents_a_new_socket_every_call_so_two_at_once_cannot_meet(
    fake_bin: Path, conf: Path
) -> None:
    """A fixed check socket is what made two concurrent doctors reject a good conf.

    With one name, each call cleared the socket before using it, so A could
    create ``asq-conf-check`` between B's kill and B's own ``new-session`` — and
    B got ``duplicate session: asq-conf-check`` (reproduced at the tmux level on
    3.4), which :meth:`TmuxServer.check_conf` can only report as tmux rejecting
    the user's configuration. Dropping the pre-emptive kill instead would let a
    server left behind by a killed run answer for a file nobody asked about,
    since tmux reads ``-f`` only when it STARTS a server. A per-call socket is
    what closes both: nothing to clear, nothing to inherit.
    """
    seen = set()
    for _ in range(20):
        fake = FakeTmux()
        _server(fake, fake_bin, conf, socket="asq").check_conf()
        socket = _sockets_used(fake)[0]
        assert _CHECK_SOCKET.fullmatch(socket), f"{socket!r} is not <socket>-check-<random>"
        seen.add(socket)
    assert len(seen) == 20, "every call gets a socket of its own"


def test_check_conf_reports_what_tmux_rejected(fake_bin: Path, conf: Path) -> None:
    # tmux 3.7c: `source-file bad` exits 1 with the complaint on stderr.
    loud = FakeTmux(Completed(1, "", "invalid option: no-such-option\n"))
    with pytest.raises(TmuxError, match="invalid option: no-such-option"):
        _server(loud, fake_bin, conf).check_conf()
    assert loud.commands()[1:] == [["kill-server"]], "still killed after a rejection"

    # A complaint with status 0 is still a rejection: tmux 3.7c was seen to print
    # `<file>:185: syntax error` and exit 0 when the file also held valid commands
    # (a Python module fed to source-file), so the status alone does not decide.
    quiet_rc = FakeTmux(Completed(0, "", "bad.conf:185: syntax error\n"))
    with pytest.raises(TmuxError, match="185: syntax error"):
        _server(quiet_rc, fake_bin, conf).check_conf()

    # The failure the old shape could not see at all: a conf that KILLS the
    # server it starts. Every command after the death answers this, status 1.
    dead = FakeTmux(Completed(1, "", "server exited unexpectedly\n"))
    with pytest.raises(TmuxError, match="server exited unexpectedly"):
        _server(dead, fake_bin, conf).check_conf()

    mute = FakeTmux(Completed(1, "", ""))
    with pytest.raises(TmuxError, match="without saying why"):
        _server(mute, fake_bin, conf).check_conf()

    # …and the kill after it cannot itself fail the check: "no server running"
    # is an ordinary answer there, and it is not the conf's fault.
    tidy_fails = FakeTmux(OK, Completed(1, "", "no server running on /tmp/tmux-1000/asq-check\n"))
    assert _completes(_server(tidy_fails, fake_bin, conf).check_conf)


def test_check_conf_kills_its_server_even_when_the_check_itself_raises(
    fake_bin: Path, conf: Path
) -> None:
    """The regression: a throwaway server outliving the run that started it, forever.

    The tidy ``kill-server`` used to sit after the checking call, so anything
    that raised out of that call skipped it — a ``_COMMAND_TIMEOUT`` on a loaded
    CI box, an ``OSError`` from ``subprocess``, a signal. And this server does
    not go on its own: it holds a session whose pane the bundled
    ``remain-on-exit on`` keeps after ``true`` returns, so ``exit-empty`` never
    fires (measured on tmux 3.4 — the session was still listed two seconds
    later, where the old session-less probe server was gone within one).

    Both ways the middle call can raise, since they take different paths out.
    """

    class Exploding(FakeTmux):
        def __init__(self, boom: Exception) -> None:
            super().__init__()
            self.boom = boom

        def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
            recorded = super().__call__(argv, stdin)
            if "kill-server" not in argv:
                raise self.boom
            return recorded

    for boom in (TmuxError("tmux did not answer within 30s"), OSError("socket gone")):
        fake = Exploding(boom)
        with pytest.raises(type(boom)):
            _server(fake, fake_bin, conf).check_conf()
        started, *rest = fake.commands()
        assert started[0] == "new-session", "the call that raised is the one under test"
        assert rest == [["kill-server"]], f"the server is killed on the way out of {boom!r}"
        first, second = _sockets_used(fake)
        assert first == second and _CHECK_SOCKET.fullmatch(first), "…and on its own socket"


def test_check_conf_removes_its_socket_file_but_only_once_the_kill_has_answered(
    fake_bin: Path, conf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmux never unlinks a socket, and a per-call socket name makes that litter.

    One file per check, forever, in ``/tmp/tmux-<uid>`` — which the fixed name
    at least reused. So the check removes its own. But only after the
    ``kill-server`` has come back: a kill that timed out leaves a server that
    may still be running, and a running server whose socket file is gone can
    never be addressed again, by this process or any later one. Litter is the
    lesser harm and this pins which one is chosen.
    """
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    server = _server(FakeTmux(), fake_bin, conf, socket="asq")
    sockets = server.socket_path().parent
    sockets.mkdir(parents=True, exist_ok=True)

    def leftover(fake: FakeTmux) -> Path:
        """The socket file tmux would have made for the check that just ran."""
        return sockets / _sockets_used(fake)[0]

    for answer in (OK, Completed(1, "", "no server running on /tmp/tmux-0/asq-check-0")):
        fake = FakeTmux(OK, answer)
        checked = _server(fake, fake_bin, conf, socket="asq")
        # Not a placeholder of some other shape: a socket file exactly like the
        # one this check makes, so "only its own" is a real claim and not one
        # the sweep's name rule would have granted anyway.
        touched = _plant(sockets, f"asq{CHECK_SOCKET_SUFFIX}-1234abcd")
        checked.check_conf()
        assert not leftover(fake).exists(), f"the socket file survived a kill answering {answer}"
        assert touched.exists(), "…and only its OWN file: nothing else was swept up"
        touched.unlink()

    # A kill that never answered: the server may be alive, so the file stays.
    class DeadKill(FakeTmux):
        def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
            recorded = super().__call__(argv, stdin)
            if "kill-server" in argv:
                raise TmuxError("tmux did not answer within 30s")
            return recorded

    fake = DeadKill()
    stubborn = _server(fake, fake_bin, conf, socket="asq")
    assert _completes(stubborn.check_conf), "a cleanup that failed is not a bad conf"
    stranded = leftover(fake)
    stranded.touch()  # stands in for the file tmux left for a server still up
    stubborn._discard_server(stranded.name)
    assert stranded.exists(), "a server that may still be running keeps its address"


#: Comfortably past the age at which the sweep may touch a probe socket.
_ABANDONED = tmux_module._PROBE_ABANDONED_AFTER + 5.0


def _servers_killed(fake: FakeTmux) -> list[str]:
    """The socket of every ``kill-server`` the run made, in order."""
    return [argv[argv.index("-L") + 1] for argv, _ in fake.calls if "kill-server" in argv]


def test_check_conf_reclaims_a_probe_socket_no_run_can_name_any_more(
    fake_bin: Path, conf: Path
) -> None:
    """SIGKILL is not a ``finally``, and a random socket is a name nothing recorded.

    ``check_conf`` cleans up from a ``finally``, which covers returning and
    raising and not being killed. What a killed run leaves is a server holding a
    session the conf's own ``remain-on-exit on`` will not let go, on a socket
    whose name existed only in the dead process: unreachable and unable to end
    itself. The file tmux left in ``/tmp/tmux-<uid>`` is the last handle on it,
    so a later check reads the directory back.

    Everything it must NOT take is planted here as old as the one it must:
    only the name rule and the age rule may decide, never the clock alone.
    """
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf, socket="asq")
    sockets = _socket_directory(server)
    abandoned = _plant(sockets, f"asq{CHECK_SOCKET_SUFFIX}-0123abcd", age=_ABANDONED)
    spared = {
        # A check that may be running RIGHT NOW: the whole reason the name is
        # random is that one call must not touch another call's live probe.
        "a probe younger than the longest a live check can hold one": _plant(
            sockets, f"asq{CHECK_SOCKET_SUFFIX}-89abcdef"
        ),
        "the fleet's own server": _plant(sockets, "asq", age=_ABANDONED),
        "a fleet whose socket merely starts the same way": _plant(
            sockets, f"asq-other{CHECK_SOCKET_SUFFIX}-0123abcd", age=_ABANDONED
        ),
        "a tail that is not the eight hex digits a check writes": _plant(
            sockets, f"asq{CHECK_SOCKET_SUFFIX}-zzzzzzzz", age=_ABANDONED
        ),
        "a tail that is hex but longer": _plant(
            sockets, f"asq{CHECK_SOCKET_SUFFIX}-0123abcde", age=_ABANDONED
        ),
        "the prefix with nothing after it": _plant(
            sockets, f"asq{CHECK_SOCKET_SUFFIX}", age=_ABANDONED
        ),
    }

    assert _completes(server.check_conf)

    killed = _servers_killed(fake)
    assert killed[0] == abandoned.name, "the abandoned probe is killed before anything else"
    assert len(killed) == 2, f"…and then only this check's own probe: {killed}"
    assert _CHECK_SOCKET.fullmatch(killed[1]), "…which is the socket it just invented"
    assert not abandoned.exists(), "the socket file goes with the server it addressed"
    for why, path in spared.items():
        assert path.exists(), f"the sweep took {path.name}, which is {why}"


def test_the_sweep_cannot_be_pointed_at_another_fleet_by_the_socket_name(
    fake_bin: Path, conf: Path
) -> None:
    """``FleetSettings.tmux_socket`` is the user's to choose, so it is never a pattern.

    ``Path.glob(f"{socket}-check-*")`` reads the socket name as a PATTERN: for a
    fleet on ``asq[1]`` that is a character class, and this exact pair is what it
    does — ``fnmatch("asq1-check-0123abcd", "asq[1]-check-*")`` is true and
    ``fnmatch("asq[1]-check-0123abcd", "asq[1]-check-*")`` is false. So the glob
    would spare this fleet's own leftover and reclaim the neighbour's instead —
    killing a server that is not ours on a socket we were never told about.
    ``str.startswith`` on a literal prefix has no such reading.
    """
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf, socket="asq[1]")
    sockets = _socket_directory(server)
    ours = _plant(sockets, f"asq[1]{CHECK_SOCKET_SUFFIX}-0123abcd", age=_ABANDONED)
    theirs = _plant(sockets, f"asq1{CHECK_SOCKET_SUFFIX}-89abcdef", age=_ABANDONED)

    assert _completes(server.check_conf)

    assert not ours.exists(), "the literal name is the one that gets reclaimed"
    assert theirs.exists(), "…and a glob would have reclaimed this one instead"
    assert _servers_killed(fake)[0] == ours.name


def test_the_sweep_is_bounded_by_the_clock_and_not_by_the_number_of_stale_sockets(
    fake_bin: Path, conf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``kill-server`` per stale socket at ``_COMMAND_TIMEOUT`` each is O(N x 30s).

    ``check_conf`` is what the doctor calls, and it used to cost a flat two tmux
    round trips. A sweep that pays for every file it finds would make a
    directory nobody had tidied for a month into a doctor that appears to hang.
    So the sweep stops at :data:`_SWEEP_BUDGET` and leaves the rest — still
    there, and still older — for the next call.
    """

    per_kill = 0.05

    class SlowKill(FakeTmux):
        """A ``kill-server`` that costs measurable time, as a hung one would."""

        def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
            if "kill-server" in argv:
                time.sleep(per_kill)
            return super().__call__(argv, stdin)

    monkeypatch.setattr(tmux_module, "_SWEEP_BUDGET", 3 * per_kill)
    fake = SlowKill()
    # A socket of its own, so nothing another test planted can be counted here.
    server = _server(fake, fake_bin, conf, socket="asq-budget")
    sockets = _socket_directory(server)
    stale = [
        _plant(sockets, f"asq-budget{CHECK_SOCKET_SUFFIX}-{index:08x}", age=_ABANDONED)
        for index in range(40)
    ]

    started = time.monotonic()
    assert _completes(server.check_conf)
    elapsed = time.monotonic() - started

    planted = {path.name for path in stale}
    swept = [socket for socket in _servers_killed(fake) if socket in planted]
    assert 1 <= len(swept) < len(stale), f"the sweep did {len(swept)} of {len(stale)} kills"
    assert elapsed < len(stale) * per_kill, "…and did not pay for every file it found"
    left = [path for path in stale if path.exists()]
    assert len(left) == len(stale) - len(swept), "the rest wait their turn, still there"


def test_a_sweep_that_cannot_read_the_directory_is_not_a_bad_configuration(
    fake_bin: Path, conf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer this method gives is about the user's CONF; litter is nobody's verdict."""
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "tmux-has-not-run-here"))
    fake = FakeTmux()
    assert _completes(_server(fake, fake_bin, conf, socket="asq").check_conf)
    assert _servers_killed(fake) == [_sockets_used(fake)[0]], "only its own probe was killed"


# --- sessions and windows -------------------------------------------------------------------


def test_list_sessions_reads_names_and_is_empty_without_a_server(
    fake_bin: Path, conf: Path
) -> None:
    fake = FakeTmux(Completed(0, "asq-amber-fox\n\nasq-blue-owl\n", ""))
    assert _server(fake, fake_bin, conf).list_sessions() == ["asq-amber-fox", "asq-blue-owl"]
    assert fake.commands() == [["list-sessions", "-F", "#{session_name}"]]

    down = FakeTmux(Completed(1, "", "no server running on /tmp/tmux-1000/sock"))
    assert _server(down, fake_bin, conf).list_sessions() == []


def test_has_session_targets_the_name_exactly(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux(OK, Completed(1, "", "can't find session: asq-amber"))
    server = _server(fake, fake_bin, conf)
    assert server.has_session("asq-amber-fox") is True
    assert server.has_session("asq-amber") is False
    assert fake.commands() == [
        ["has-session", "-t", "=asq-amber-fox"],
        ["has-session", "-t", "=asq-amber"],
    ]


def test_spawn_window_creates_the_session_when_it_is_absent(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    fake = FakeTmux(
        Completed(1, "", "can't find session: asq-amber-fox"),  # has-session
        Completed(0, f"@4{_SEP}%9\n", ""),  # new-session -P
    )
    info = _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox",
        name="coder-1",
        cwd=tmp_path,
        command=["claude", "--name", "coder-1", "-p", "--dangerously-skip"],
        env={"AISQUARE_ROLE": "coder", "X": "a=b"},
        width=120,
        height=40,
    )
    assert fake.commands()[1] == [
        "new-session", "-d", "-P", "-F", f"#{{window_id}}{_SEP}#{{pane_id}}",
        "-s", "asq-amber-fox", "-n", "coder-1", "-c", str(tmp_path), "-x", "120", "-y", "40",
        "-e", "AISQUARE_ROLE=coder", "-e", "X=a=b",
        "--", "claude", "--name", "coder-1", "-p", "--dangerously-skip",
    ]  # fmt: skip
    assert fake.commands()[2:] == [[
        "set-option", "-w", "-t", "@4", "window-size", "manual",
        ";", "resize-window", "-t", "@4", "-x", "120", "-y", "40",
    ]], "pinned and sized by its own id, in ONE command right after it exists"  # fmt: skip
    assert info == WindowInfo(
        session="asq-amber-fox",
        window_id="@4",
        name="coder-1",
        pane_id="%9",
        dead=False,
        dead_status=None,
        current_command="claude",
        activity=False,
    )


def test_spawn_window_adds_a_window_when_the_session_exists(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    fake = FakeTmux(OK, Completed(0, f"@5{_SEP}%10\n", ""))
    info = _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox", name="reviewer", cwd=tmp_path, command=["sh", "-c", "exit 3"]
    )
    new_window = fake.commands()[1]
    assert new_window == [
        "new-window", "-d", "-P", "-F", f"#{{window_id}}{_SEP}#{{pane_id}}",
        "-t", "=asq-amber-fox:", "-n", "reviewer", "-c", str(tmp_path),
        "--", "sh", "-c", "exit 3",
    ]  # fmt: skip
    assert "-x" not in new_window, "new-window has no -x on tmux 3.4; resize-window sizes it"
    assert fake.commands()[2:] == [[
        "set-option", "-w", "-t", "@5", "window-size", "manual",
        ";", "resize-window", "-t", "@5", "-x", "200", "-y", "50",
    ]], "@5 — never `=session:`, which is whichever window that session is on"  # fmt: skip
    assert (info.window_id, info.pane_id, info.current_command) == ("@5", "%10", "sh")


def test_spawn_window_clamps_a_size_tmux_would_refuse_the_same_way_resize_does(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    """One rule for both commands, because tmux applies one rule to both.

    ``new-session -x 0`` and ``resize-window -x 0`` are both ``width too small``
    on tmux 3.4. Unclamped, a 0 would therefore be a clean refusal on the branch
    that creates the session (nothing exists yet) and a window created and then
    destroyed on the branch that does not — the same call, two outcomes, neither
    the caller's intent. :meth:`TmuxServer.resize` has always clamped to 1; this
    is that rule, one line earlier.
    """
    fake = FakeTmux(Completed(1, "", "no session"), Completed(0, f"@1{_SEP}%1\n", ""))
    _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"], width=0, height=-5
    )
    created, configured = fake.commands()[1], fake.commands()[2]
    assert created[created.index("-x") : created.index("-x") + 4] == ["-x", "1", "-y", "1"]
    assert configured[-4:] == ["-x", "1", "-y", "1"], "…and the resize agrees with it"


def test_spawn_window_without_env_passes_no_dash_e(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    fake = FakeTmux(OK, Completed(0, f"@1{_SEP}%1\n", ""))
    _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"]
    )
    assert "-e" not in fake.commands()[1]


@pytest.mark.parametrize("name", ["a.b", "asq:fox", "", "with.dot:and-colon"])
def test_spawn_and_rename_refuse_a_session_name_tmux_could_not_target(
    name: str, fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    with pytest.raises(TmuxError, match="cannot be targeted"):
        server.spawn_window(name, name="w", cwd=tmp_path, command=["cat"])
    with pytest.raises(TmuxError, match="cannot be targeted"):
        server.rename_session("asq-amber-fox", name)
    assert fake.calls == [], "refused before anything reached tmux"

    # Negative control: a codename-shaped name goes through to tmux.
    server.rename_session("asq-amber-fox", "asq-blue-owl")
    assert fake.commands() == [["rename-session", "-t", "=asq-amber-fox", "asq-blue-owl"]]


def test_spawn_window_refuses_a_cwd_tmux_would_replace_with_home(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    # Answers for the negative control at the end; a refusal consumes none.
    fake = FakeTmux(OK, Completed(0, f"@1{_SEP}%1\n", ""))
    server = _server(fake, fake_bin, conf)
    missing = tmp_path / "worktree-not-yet-created"
    with pytest.raises(TmuxError, match="is not a directory"):
        server.spawn_window("asq-amber-fox", name="w", cwd=missing, command=["cat"])
    regular_file = tmp_path / "a-file"
    regular_file.write_text("", encoding="utf-8")
    with pytest.raises(TmuxError, match="is not a directory"):
        server.spawn_window("asq-amber-fox", name="w", cwd=regular_file, command=["cat"])
    assert fake.calls == [], "refused before anything reached tmux"

    # Negative control: the directory, once it exists, goes through.
    missing.mkdir()
    server.spawn_window("asq-amber-fox", name="w", cwd=missing, command=["cat"])
    assert fake.commands()[0] == ["has-session", "-t", "=asq-amber-fox"]


@pytest.mark.parametrize("answer", ["", "@4", f"{_SEP}%9", f"@4{_SEP}", f"@4{_SEP}9", f"4{_SEP}%9"])
def test_spawn_window_refuses_an_answer_that_names_no_window(
    answer: str, fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    """A half-read ``-P -F`` line must not become a ``set-option`` with a target it invented.

    ``-t ''`` is not a no-op to tmux: it resolves to whatever window is current,
    so pinning and RESIZING one with an id tmux did not give would resize
    somebody else's agent — and the caller would get a WindowInfo it could never
    target either. ``@``/``%`` is what tmux has printed for these two formats
    since 1.6, far below :data:`MIN_VERSION`, so an answer without them is not
    an old tmux, it is not tmux.
    """
    fake = FakeTmux(OK, Completed(0, answer + "\n", ""))
    with pytest.raises(TmuxError, match="did not name the new window"):
        _server(fake, fake_bin, conf).spawn_window(
            "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"]
        )
    assert len(fake.calls) == 2, "nothing was aimed at a window the answer did not name"

    # On the branch that CREATED the session there is still a handle — the
    # session name this call chose — so the unnameable window goes with it.
    made = FakeTmux(Completed(1, "", "can't find session"), Completed(0, answer + "\n", ""))
    with pytest.raises(TmuxError, match="did not name the new window"):
        _server(made, fake_bin, conf).spawn_window(
            "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"]
        )
    assert made.commands()[2:] == [["kill-session", "-t", "=asq-amber-fox"]]


@pytest.mark.parametrize("existing", [True, False])
def test_spawn_window_leaves_no_window_behind_when_configuring_it_fails(
    existing: bool, fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    """The regression: an agent running in a window whose caller was told nothing started.

    Sizing the window is a SECOND round trip — it needs the ``@id`` the creating
    command has not printed yet — so it can fail after the window exists and its
    agent is already running: the 30s ``_COMMAND_TIMEOUT`` on a loaded CI box,
    the server dying in between. ``services.fleet`` turns any TmuxError from
    here into "tmux could not start the window", so the window has to be gone by
    the time that is true — otherwise a real agent runs unsupervised, invisible
    to the store, and was seen still listed by ``list_windows`` after the raise.

    Killing the WINDOW covers both branches: on the one that created the session
    it is that session's only window and tmux takes the session with it
    (measured on 3.4), and on the other the session belongs to agents this call
    must not touch.
    """
    fake = FakeTmux(
        OK if existing else Completed(1, "", "can't find session"),
        Completed(0, f"@7{_SEP}%7\n", ""),
        Completed(1, "", "tmux did not answer within 30s"),
    )
    with pytest.raises(TmuxError, match="did not answer"):
        _server(fake, fake_bin, conf).spawn_window(
            "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"]
        )
    assert fake.commands()[3:] == [["kill-window", "-t", "@7"]], (
        "the window that exists but is not configured is destroyed before the raise"
    )


def test_spawn_window_reports_the_original_failure_when_the_cleanup_also_fails(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    """The undo is a second line of defence; it must never become the story.

    A cleanup that raised would replace the reason the spawn failed with a
    complaint about the kill — and on the commonest cause, a server that died,
    the kill fails for exactly that reason (``no server running``) while the
    window is already gone with it.
    """
    fake = FakeTmux(
        OK,
        Completed(0, f"@7{_SEP}%7\n", ""),
        Completed(1, "", "server exited unexpectedly"),
        Completed(1, "", "no server running on /tmp/tmux-1000/sock"),
    )
    with pytest.raises(TmuxError, match="server exited unexpectedly"):
        _server(fake, fake_bin, conf).spawn_window(
            "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"]
        )
    assert fake.commands()[3:] == [["kill-window", "-t", "@7"]], "…and it was still attempted"


def test_list_windows_parses_each_pane_and_skips_a_malformed_line(
    fake_bin: Path, conf: Path
) -> None:
    live = _SEP.join(["@0", "manager", "%0", "0", "", "claude", "1"])
    dead = _SEP.join(["@1", "coder-1", "%1", "1", "3", "sh", "0"])
    broken = _SEP.join(["@2", "only-two-fields"])
    fake = FakeTmux(Completed(0, f"{live}\n{dead}\n{broken}\n", ""))
    windows = _server(fake, fake_bin, conf).list_windows("asq-amber-fox")
    assert fake.commands() == [
        ["list-panes", "-s", "-t", "=asq-amber-fox", "-F", tmux_module._WINDOW_FORMAT]
    ]
    assert windows == [
        WindowInfo("asq-amber-fox", "@0", "manager", "%0", False, None, "claude", True),
        WindowInfo("asq-amber-fox", "@1", "coder-1", "%1", True, 3, "sh", False),
    ]

    gone = FakeTmux(Completed(1, "", "can't find session: asq-amber-fox"))
    assert _server(gone, fake_bin, conf).list_windows("asq-amber-fox") == []


def test_pane_facts_parses_a_live_pane(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux(Completed(0, _facts_line() + "\n", ""))
    facts = _server(fake, fake_bin, conf).pane_facts("%3")
    assert fake.commands() == [["display-message", "-p", "-t", "%3", tmux_module._FACTS_FORMAT]]
    assert facts == PaneFacts(
        pane_id="%3",
        width=80,
        height=24,
        cursor_x=5,
        cursor_y=7,
        cursor_visible=True,
        alternate_on=False,
        history_size=120,
        dead=False,
        dead_status=None,
        in_mode=False,
        current_command="claude",
        title="fedora",
    )


def test_pane_facts_reads_a_dead_pane_and_the_flags(fake_bin: Path, conf: Path) -> None:
    line = _facts_line(
        pane_dead="1", pane_dead_status="3", cursor_flag="0", alternate_on="1", pane_in_mode="1"
    )
    facts = _server(FakeTmux(Completed(0, line, "")), fake_bin, conf).pane_facts("%3")
    assert facts is not None
    assert (facts.dead, facts.dead_status) == (True, 3)
    assert (facts.cursor_visible, facts.alternate_on, facts.in_mode) == (False, True, True)


def test_pane_facts_is_none_for_a_gone_pane_in_every_shape_tmux_answers(
    fake_bin: Path, conf: Path
) -> None:
    # No server: an exit status.
    down = FakeTmux(Completed(1, "", "no server running"))
    assert _server(down, fake_bin, conf).pane_facts("%3") is None
    # tmux 3.7c on an unknown pane: status 0, every field empty (CMD_FIND_CANFAIL).
    empty = FakeTmux(Completed(0, _SEP * (FACTS_FIELDS - 1) + "\n", ""))
    assert _server(empty, fake_bin, conf).pane_facts("%3") is None
    # A client attached elsewhere could make it answer about ITS pane: not ours.
    other = FakeTmux(Completed(0, _facts_line(pane_id="%0"), ""))
    assert _server(other, fake_bin, conf).pane_facts("%3") is None
    # Negative control: the same answer for the pane asked about is a pane.
    ours = FakeTmux(Completed(0, _facts_line(pane_id="%0"), ""))
    facts = _server(ours, fake_bin, conf).pane_facts("%0")
    assert facts is not None and facts.pane_id == "%0"


def test_facts_tolerate_a_separator_in_the_title_but_not_a_short_line() -> None:
    perverse = _facts_line(pane_title=f"a{_SEP}b{_SEP}c")
    assert tmux_module._facts(perverse).title == f"a{_SEP}b{_SEP}c"

    with pytest.raises(TmuxError, match="unexpected display-message output"):
        tmux_module._facts(_SEP.join(["%3", "80", "24"]))
    with pytest.raises(TmuxError, match="unexpected display-message output"):
        tmux_module._facts("")


def test_numeric_fields_never_raise_on_junk() -> None:
    line = _facts_line(pane_width="wide", pane_dead_status="--1", history_size="")
    facts = tmux_module._facts(line)
    assert (facts.width, facts.dead_status, facts.history_size) == (0, None, 0)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="no uid, no tmux socket (see the next test)")
def test_socket_path_follows_tmux_tmpdir_then_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = os.getuid()
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    assert TmuxServer("asq").socket_path() == Path("/tmp") / f"tmux-{uid}" / "asq"
    monkeypatch.setenv("TMUX_TMPDIR", "/run/user/1000")
    assert TmuxServer("asq").socket_path() == Path("/run/user/1000") / f"tmux-{uid}" / "asq"
    monkeypatch.setenv("TMUX_TMPDIR", "")
    assert TmuxServer("asq").socket_path().parent.parent == Path("/tmp"), "empty is unset to tmux"


def test_socket_path_is_unavailable_where_there_is_no_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows outside WSL (plan §3.9): the module imports, and the answer is "no tmux here".
    monkeypatch.delattr(os, "getuid", raising=False)
    with pytest.raises(TmuxUnavailable, match="POSIX host"):
        TmuxServer("asq").socket_path()


def test_kill_and_rename_and_attach_build_exact_targets(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf, socket="asq")
    server.kill_window("%7")
    server.kill_session("asq-amber-fox")
    server.rename_session("asq-amber-fox", "asq-blue-owl")
    server.kill_server()
    assert fake.commands() == [
        ["kill-window", "-t", "%7"],
        ["kill-session", "-t", "=asq-amber-fox"],
        ["rename-session", "-t", "=asq-amber-fox", "asq-blue-owl"],
        ["kill-server"],
    ]
    assert server.attach_argv("asq-amber-fox") == [
        *_prefix(fake_bin, conf, "asq"),
        "attach-session",
        "-t",
        "=asq-amber-fox",
    ]


# --- the screen -------------------------------------------------------------------------------


def _frame(rows: Sequence[str], facts: str) -> Completed:
    return Completed(0, "\n".join([*rows, facts]) + "\n", "")


def test_capture_is_one_process_and_keeps_blank_rows(fake_bin: Path, conf: Path) -> None:
    rows = ["\x1b[31mred\x1b[39m plain   ", "", "$ ", *[""] * 21]
    fake = FakeTmux(_frame(rows, _facts_line()))
    capture = _server(fake, fake_bin, conf).capture("%3")
    assert fake.commands() == [
        [
            "capture-pane", "-p", "-e", "-N", "-S", "0", "-t", "%3",
            ";", "display-message", "-p", "-t", "%3", tmux_module._FACTS_FORMAT,
        ]
    ]  # fmt: skip
    assert capture == Capture(lines=rows, facts=tmux_module._facts(_facts_line()), scrollback=0)
    assert len(capture.lines) == 24


def test_capture_slices_a_scrolled_frame_to_the_screen_height(fake_bin: Path, conf: Path) -> None:
    history = [f"h{i}" for i in range(5)]
    screen = [f"s{i}" for i in range(24)]
    fake = FakeTmux(_frame([*history, *screen], _facts_line()))
    capture = _server(fake, fake_bin, conf).capture("%3", scrollback=5)
    assert fake.commands()[0][:6] == ["capture-pane", "-p", "-e", "-N", "-S", "-5"]
    assert "-E" not in fake.commands()[0], "unbounded without a height hint"
    assert capture.lines == [*history, *screen[:19]]
    assert capture.scrollback == 5


def test_capture_bounds_the_transfer_when_the_height_is_known(fake_bin: Path, conf: Path) -> None:
    rows = [f"h{i}" for i in range(5)] + [f"s{i}" for i in range(19)]
    fake = FakeTmux(_frame(rows, _facts_line()))
    capture = _server(fake, fake_bin, conf).capture("%3", scrollback=5, height=24)
    assert fake.commands()[0][:8] == ["capture-pane", "-p", "-e", "-N", "-S", "-5", "-E", "18"]
    assert len(fake.calls) == 1
    assert capture.lines == rows
    assert capture.scrollback == 5

    # Negative control: a live frame is never bounded, whatever the hint.
    fake = FakeTmux(_frame([""] * 24, _facts_line()))
    _server(fake, fake_bin, conf).capture("%3", scrollback=0, height=24)
    assert "-E" not in fake.commands()[0]


def test_capture_refetches_unbounded_when_the_bound_returned_a_short_frame(
    fake_bin: Path, conf: Path
) -> None:
    # history_size is 2, the caller asked for 30: `-S -30 -E -7` yields 3 rows.
    short = _frame(["h0", "h1", "s0"], _facts_line(history_size="2"))
    full = _frame(["h0", "h1", *[f"s{i}" for i in range(24)]], _facts_line(history_size="2"))
    fake = FakeTmux(short, full)
    capture = _server(fake, fake_bin, conf).capture("%3", scrollback=30, height=24)
    assert [command[:8] for command in fake.commands()] == [
        ["capture-pane", "-p", "-e", "-N", "-S", "-30", "-E", "-7"],
        ["capture-pane", "-p", "-e", "-N", "-S", "-30", "-t", "%3"],
    ]
    assert len(capture.lines) == 24
    assert capture.scrollback == 2, "the offset tmux honoured, not the one asked for"


def test_capture_clamps_a_negative_request_to_live(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux(_frame([""] * 24, _facts_line()))
    capture = _server(fake, fake_bin, conf).capture("%3", scrollback=-9)
    assert fake.commands()[0][4:6] == ["-S", "0"]
    assert capture.scrollback == 0


def test_capture_raises_when_the_pane_is_gone(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux(Completed(1, "", "can't find pane: %3\n"))
    with pytest.raises(TmuxError, match="can't find pane: %3"):
        _server(fake, fake_bin, conf).capture("%3")


# --- input --------------------------------------------------------------------------------------


def test_send_keys_and_send_literal_build_their_argv(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    server.send_keys("%3", "C-c", "Enter")
    server.send_literal("%3", "-dash text; not a command")
    server.send_keys("%3")
    server.send_literal("%3", "")
    assert fake.commands() == [
        ["send-keys", "-t", "%3", "C-c", "Enter"],
        ["send-keys", "-t", "%3", "-l", "--", "-dash text; not a command"],
    ], "nothing to send is not a tmux call"


def test_paste_loads_the_buffer_from_stdin_then_pastes_bracketed(
    fake_bin: Path, conf: Path
) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    server.paste("%3", "line one\nline two\n")
    server.paste("%3", "")
    assert fake.commands() == [
        ["load-buffer", "-b", PASTE_BUFFER, "-"],
        ["paste-buffer", "-p", "-d", "-b", PASTE_BUFFER, "-t", "%3"],
    ]
    assert fake.calls[0][1] == b"line one\nline two\n"
    assert fake.calls[1][1] is None


def test_resize_never_asks_for_a_zero_sized_window(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    server.resize("%3", 120, 40)
    server.resize("%3", 0, -5)
    assert fake.commands() == [
        ["resize-window", "-t", "%3", "-x", "120", "-y", "40"],
        ["resize-window", "-t", "%3", "-x", "1", "-y", "1"],
    ]


# --- the seam itself ------------------------------------------------------------------------------


def test_the_seam_maps_a_vanished_binary_to_unavailable() -> None:
    with pytest.raises(TmuxUnavailable, match="not runnable"):
        _tmux([f"/nonexistent/asq-tmux-{os.getpid()}", "-V"], None)


def test_the_seam_maps_a_hung_server_to_tmux_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_module, "_COMMAND_TIMEOUT", 0.2)
    with pytest.raises(TmuxError, match="did not answer within 0s"):
        _tmux([sys.executable, "-c", "import time; time.sleep(5)"], None)


def test_the_seam_strips_the_tracing_identity_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.example")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Agent-Name: coder")
    monkeypatch.setenv("ASQ_TEST_PASSTHROUGH", "kept")
    probe = (
        "import os; print(os.environ.get('ANTHROPIC_BASE_URL', '<unset>'), "
        "os.environ.get('ANTHROPIC_CUSTOM_HEADERS', '<unset>'), "
        "os.environ.get('ASQ_TEST_PASSTHROUGH', '<unset>'))"
    )
    completed = _tmux([sys.executable, "-c", probe], None)
    assert completed.returncode == 0
    assert completed.stdout.split() == ["<unset>", "<unset>", "kept"]


def test_the_seam_feeds_stdin_and_returns_both_streams() -> None:
    probe = (
        "import sys; d=sys.stdin.read(); sys.stdout.write(d); sys.stderr.write('e:'+d); sys.exit(3)"
    )
    completed = _tmux([sys.executable, "-c", probe], b"payload")
    assert completed == Completed(3, "payload", "e:payload")


# --- a real server ------------------------------------------------------------------------

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed; the live tests need it"
)
_SOCKETS = itertools.count()
#: Prints one coloured line, then becomes `cat` so typed and pasted text echoes back.
CAT = ["sh", "-c", 'printf "\\033[31mred\\033[0m plain\\n"; exec cat']
EXIT_3 = ["sh", "-c", "exit 3"]


def _wait(predicate: Callable[[], bool], *, timeout: float = 8.0) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` passes; the last answer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _check_leftovers(live: TmuxServer) -> list[str]:
    """Socket files ``check_conf`` left in tmux's socket directory for this server.

    The check invents ``<socket>-check-<random>`` per call, so what proves it
    tidied up is not one name but the ABSENCE of every name of that shape: a
    file here means either a server still running or the socket of one that is
    not — and a per-call name turns the second into litter that accumulates,
    one file per doctor run, where the old fixed name at least reused itself.
    """
    directory = live.socket_path().parent
    return sorted(p.name for p in directory.glob(f"{live.socket}{CHECK_SOCKET_SUFFIX}-*"))


@pytest.fixture
def live() -> Iterator[TmuxServer]:
    """A TmuxServer on a private socket of its own, killed whatever the test did."""
    server = TmuxServer(f"asq-test-{os.getpid()}-{next(_SOCKETS)}")
    try:
        yield server
    finally:
        # _check_leftovers returns socket NAMES, so they address servers directly.
        for socket in (server.socket, *_check_leftovers(server)):
            # No server running is the normal end state of check_conf's socket.
            with contextlib.suppress(TmuxError):
                TmuxServer(socket).kill_server()
            # tmux leaves the socket file behind; a run must not litter /tmp.
            with contextlib.suppress(OSError):
                TmuxServer(socket).socket_path().unlink()


def _spawn(live: TmuxServer, session: str, name: str, command: Sequence[str]) -> WindowInfo:
    return live.spawn_window(
        session, name=name, cwd=Path("/tmp"), command=command, width=80, height=24
    )


def _screen(live: TmuxServer, pane_id: str) -> str:
    return "\n".join(live.capture(pane_id).lines)


@requires_tmux
def test_live_spawn_creates_the_session_then_adds_a_window(live: TmuxServer) -> None:
    assert live.list_sessions() == []
    first = _spawn(live, "asq-test-fox", "w0", CAT)
    second = _spawn(live, "asq-test-fox", "w1", CAT)
    assert live.list_sessions() == ["asq-test-fox"]
    assert first.window_id != second.window_id and first.pane_id != second.pane_id

    windows = live.list_windows("asq-test-fox")
    assert [(w.window_id, w.name, w.pane_id) for w in windows] == [
        (first.window_id, "w0", first.pane_id),
        (second.window_id, "w1", second.pane_id),
    ]
    assert all(not w.dead for w in windows)
    assert live.list_windows("asq-test-owl") == []


@requires_tmux
def test_live_has_session_is_exact_because_of_the_equals(live: TmuxServer) -> None:
    _spawn(live, "asq-test-fox", "w0", CAT)
    assert live.has_session("asq-test-fox") is True
    assert live.has_session("asq-test-fo") is False
    # The control on the mechanism: without `=`, tmux matches the prefix.
    assert live.run("has-session", "-t", "asq-test-fo") == ""


@requires_tmux
def test_live_capture_returns_the_screen_with_colours_and_consumes_the_facts_line(
    live: TmuxServer,
) -> None:
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    assert _wait(lambda: "plain" in _screen(live, window.pane_id))

    capture = live.capture(window.pane_id)
    assert capture.lines[0].startswith("\x1b[31mred\x1b[39m plain")
    assert len(capture.lines) == capture.facts.height == 24
    assert capture.facts.width == 80
    assert all(_SEP not in line for line in capture.lines)
    assert capture.facts.pane_id == window.pane_id
    assert capture.facts.dead is False and capture.facts.dead_status is None
    assert capture.scrollback == 0
    assert _wait(lambda: live.capture(window.pane_id).facts.current_command == "cat")


@requires_tmux
def test_live_send_literal_and_enter_reach_cat(live: TmuxServer) -> None:
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    assert _wait(lambda: "plain" in _screen(live, window.pane_id))
    assert "-dash hello" not in _screen(live, window.pane_id)

    live.send_literal(window.pane_id, "-dash hello")
    live.send_keys(window.pane_id, "Enter")
    # Once as the tty's echo of the typing, once more as cat's output.
    assert _wait(lambda: _screen(live, window.pane_id).count("-dash hello") >= 2)


@requires_tmux
def test_live_paste_delivers_every_line(live: TmuxServer) -> None:
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    assert _wait(lambda: "plain" in _screen(live, window.pane_id))

    live.paste(window.pane_id, "alpha one\nbeta two\ngamma three\n")
    assert _wait(
        lambda: all(
            word in _screen(live, window.pane_id)
            for word in ("alpha one", "beta two", "gamma three")
        )
    )
    assert live.run("list-buffers") == "", "the paste buffer is deleted after use (-d)"


@requires_tmux
def test_live_resize_changes_the_pane_and_the_frame(live: TmuxServer) -> None:
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    before = live.pane_facts(window.pane_id)
    assert before is not None and (before.width, before.height) == (80, 24)

    live.resize(window.pane_id, 120, 40)
    after = live.pane_facts(window.pane_id)
    assert after is not None and (after.width, after.height) == (120, 40)
    assert len(live.capture(window.pane_id).lines) == 40


@requires_tmux
def test_live_scrollback_is_bounded_and_clamped(live: TmuxServer) -> None:
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    assert _wait(lambda: "plain" in _screen(live, window.pane_id))
    for i in range(40):
        live.send_literal(window.pane_id, f"row {i:02d}")
        live.send_keys(window.pane_id, "Enter")
    assert _wait(lambda: "row 39" in _screen(live, window.pane_id))
    facts = live.pane_facts(window.pane_id)
    assert facts is not None and facts.history_size > 5

    bounded = live.capture(window.pane_id, scrollback=5, height=24)
    unbounded = live.capture(window.pane_id, scrollback=5)
    assert bounded.lines == unbounded.lines
    assert len(bounded.lines) == 24 and bounded.scrollback == 5
    assert bounded.lines != live.capture(window.pane_id).lines

    deep = live.capture(window.pane_id, scrollback=10_000, height=24)
    assert deep.scrollback == facts.history_size
    assert len(deep.lines) == 24
    assert deep.lines[0].startswith("\x1b[31mred"), "the top of history is the first line printed"


#: How long a pane that already reads dead is given to also carry its status.
#: The two are not one event: tmux marks a pane dead when it closes the pane's
#: fd (``pane_dead`` is ``wp->fd == -1``) and can only fill ``pane_dead_status``
#: from the SIGCHLD that reaps the child, which sets ``PANE_STATUSREADY`` —
#: ``server_destroy_pane`` and ``server_child_exited`` in tmux 3.4's source. So
#: waiting for the first and asserting the second is a race, and this is the
#: bound on the wait for the second. Generous on purpose: over 250 exiting
#: panes measured here on 3.4 — 100 idle and 150 under 16 busy-loop CPU hogs —
#: the status was present on the FIRST read after ``pane_dead`` every time it
#: arrived at all and not once arrived late, so nothing but the defect below
#: ever reaches this bound.
_DEAD_STATUS_TIMEOUT = 8.0

#: How many exiting panes this tmux may be asked for before the test gives up.
#: tmux 3.4 sometimes loses a pane's exit status outright — ``pane_dead=1`` with
#: ``pane_dead_status``, ``pane_dead_signal`` and ``pane_dead_time`` all empty,
#: and still empty ``_DEAD_STATUS_TIMEOUT`` later. Measured on this box: 5 of
#: 150 panes under 16 CPU hogs with PLAIN tmux and no fleet code in the way
#: (11 of 150 through :meth:`TmuxServer.spawn_window`), 2 of 280 idle, and 0 of
#: 150 on tmux 3.5a under the same load — a 3.4 defect, on the tmux CI runs.
#:
#: It is independent per pane: across 120 runs of three consecutive panes, no
#: run lost more than one. So a retry is not a way to tolerate a wrong answer —
#: a status that is present and not 3 fails on the spot, and a tmux that reports
#: none at all fails every attempt and the test still reds — only a way to stop
#: one pane in ~30 deciding a whole CI run.
_DEAD_STATUS_ATTEMPTS = 3

#: What tmux itself says about a dead pane, for a failure to quote back.
_DEAD_FORMAT = (
    "pane_dead=#{pane_dead} status=[#{pane_dead_status}] "
    "signal=[#{pane_dead_signal}] time=[#{pane_dead_time}]"
)


def _reports_a_status(live: TmuxServer, session: str, pane_id: str) -> bool:
    """Has tmux filled the exit status in, in BOTH places the test reads it?

    Deliberately not "is it 3": what may be waited for is tmux being READY to
    answer, and what the answer says is then asserted once, where a wrong status
    fails instead of quietly costing another attempt.
    """
    window = next((w for w in live.list_windows(session) if w.pane_id == pane_id), None)
    facts = live.pane_facts(pane_id)
    return (
        window is not None
        and window.dead
        and window.dead_status is not None
        and facts is not None
        and facts.dead
        and facts.dead_status is not None
    )


def _exited_pane(live: TmuxServer, session: str, name: str) -> WindowInfo:
    """A window whose ``exit 3`` has finished AND whose status tmux still holds.

    Waits for the condition the caller ASSERTS rather than for the earlier
    ``pane_dead`` the assertion does not mention (:data:`_DEAD_STATUS_TIMEOUT`),
    and gives up loudly, in tmux's own words, when no pane ever reports one
    (:data:`_DEAD_STATUS_ATTEMPTS`). A pane that lost its status is killed before
    the next attempt, so the caller still gets a session with one dead window.
    """
    unanswered: list[str] = []
    for attempt in range(_DEAD_STATUS_ATTEMPTS):
        window = _spawn(live, session, f"{name}-{attempt}", EXIT_3)
        ready = functools.partial(_reports_a_status, live, session, window.pane_id)
        if _wait(ready, timeout=_DEAD_STATUS_TIMEOUT):
            return window
        unanswered.append(
            live.run("display-message", "-p", "-t", window.pane_id, _DEAD_FORMAT).strip()
        )
        live.kill_window(window.pane_id)
    raise AssertionError(
        f"{_DEAD_STATUS_ATTEMPTS} panes ran `{' '.join(EXIT_3)}` and not one reported an exit "
        f"status within {_DEAD_STATUS_TIMEOUT:.0f}s. tmux's own answer for each: "
        + "; ".join(unanswered)
    )


@requires_tmux
def test_live_a_finished_command_is_a_dead_pane_with_its_status(live: TmuxServer) -> None:
    """``remain-on-exit`` keeps the window, and the exit status is readable from it.

    What the fleet needs from a finished agent is the number it exited with, and
    it reads that two ways — out of ``list-panes`` for the whole session, and out
    of ``display-message`` for the one pane. Both are pinned here, on the same
    pane, and both must say 3.
    """
    keeper = _spawn(live, "asq-test-fox", "w0", CAT)
    exiting = _exited_pane(live, "asq-test-fox", "w1")

    found = next(
        (w for w in live.list_windows("asq-test-fox") if w.pane_id == exiting.pane_id), None
    )
    assert found is not None and (found.dead, found.dead_status) == (True, 3)
    facts = live.pane_facts(exiting.pane_id)
    assert facts is not None and (facts.dead, facts.dead_status) == (True, 3)
    # remain-on-exit: the window is still there to read…
    assert len(live.list_windows("asq-test-fox")) == 2

    alive = live.pane_facts(keeper.pane_id)
    assert alive is not None and (alive.dead, alive.dead_status) == (False, None)

    # …and kill_window removes a dead window as it does a live one.
    live.kill_window(exiting.pane_id)
    assert [w.pane_id for w in live.list_windows("asq-test-fox")] == [keeper.pane_id]


@requires_tmux
def test_live_activity_is_always_set_on_a_headless_server(live: TmuxServer) -> None:
    """Pins the WindowInfo.activity caveat: set at creation, never cleared, so no signal.

    The plan (§3.1) hoped ``monitor-activity`` would drive a pulse without
    capturing every pane. On tmux 3.7c a window nobody is attached to reports
    the flag from the moment it exists — before its command has written a
    byte — and nothing headless clears it: not output, not a capture, not
    ``select-window``. A pulse must come from ``history_size`` or the cursor
    changing between frames instead.
    """
    quiet = _spawn(live, "asq-test-fox", "w0", ["cat"])  # writes nothing until fed
    assert {w.name: w.activity for w in live.list_windows("asq-test-fox")} == {"w0": True}
    assert live.capture(quiet.pane_id).facts.history_size == 0, "…and it had written nothing"

    loud = _spawn(live, "asq-test-fox", "w1", CAT)
    assert _wait(lambda: "plain" in _screen(live, loud.pane_id))
    live.send_literal(quiet.pane_id, "now it prints")
    live.send_keys(quiet.pane_id, "Enter")
    for _ in range(3):
        live.capture(quiet.pane_id)
        live.capture(loud.pane_id)
    live.run("select-window", "-t", quiet.window_id)
    live.run("select-window", "-t", loud.window_id)
    assert {w.name: w.activity for w in live.list_windows("asq-test-fox")} == {
        "w0": True,
        "w1": True,
    }

    # Control on the instrument: the same query does read a 0 — the bell flag of
    # the same windows — so "always 1" is tmux's answer, not a blind parser.
    flags = live.run(
        "list-windows", "-t", "=asq-test-fox", "-F", "#{window_activity_flag} #{window_bell_flag}"
    ).splitlines()
    assert flags == ["1 0", "1 0"]


@requires_tmux
def test_live_spawn_refuses_the_cwd_tmux_would_silently_replace(
    live: TmuxServer, tmp_path: Path
) -> None:
    _spawn(live, "asq-test-fox", "w0", CAT)
    missing = tmp_path / "never-created"
    with pytest.raises(TmuxError, match="is not a directory"):
        live.spawn_window("asq-test-fox", name="w1", cwd=missing, command=CAT)
    assert [w.name for w in live.list_windows("asq-test-fox")] == ["w0"]

    # The control on the mechanism: tmux itself accepts the path and uses $HOME.
    out = live.run(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "=asq-test-fox:", "-n", "raw",
        "-c", str(missing), "--", *CAT,
    )  # fmt: skip
    pane_id = out.strip()
    assert _wait(lambda: "plain" in _screen(live, pane_id))
    where = live.run("display-message", "-p", "-t", pane_id, "#{pane_current_path}").strip()
    assert where and where != str(missing)
    assert where == os.path.expanduser("~")


@requires_tmux
def test_live_kill_window_removes_it_and_the_pane_reads_as_gone(live: TmuxServer) -> None:
    keeper = _spawn(live, "asq-test-fox", "w0", CAT)
    victim = _spawn(live, "asq-test-fox", "w1", CAT)
    assert live.pane_facts(victim.pane_id) is not None

    live.kill_window(victim.pane_id)
    assert [w.pane_id for w in live.list_windows("asq-test-fox")] == [keeper.pane_id]
    assert live.pane_facts(victim.pane_id) is None
    with pytest.raises(TmuxError, match="can't find pane"):
        live.capture(victim.pane_id)
    with pytest.raises(TmuxError, match="can't find pane"):
        live.send_literal(victim.pane_id, "x")
    assert live.pane_facts(keeper.pane_id) is not None


@requires_tmux
def test_live_rename_session_moves_the_windows_with_it(live: TmuxServer) -> None:
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    live.rename_session("asq-test-fox", "asq-test-owl")
    assert live.has_session("asq-test-owl") is True
    assert live.has_session("asq-test-fox") is False
    assert [w.pane_id for w in live.list_windows("asq-test-owl")] == [window.pane_id]

    with pytest.raises(TmuxError, match="cannot be targeted"):
        live.rename_session("asq-test-owl", "asq.test")
    assert live.list_sessions() == ["asq-test-owl"]


@requires_tmux
def test_live_kill_session_then_kill_server(live: TmuxServer) -> None:
    _spawn(live, "asq-test-fox", "w0", CAT)
    _spawn(live, "asq-test-owl", "w0", CAT)
    live.kill_session("asq-test-fox")
    assert live.list_sessions() == ["asq-test-owl"]
    live.kill_server()
    assert live.list_sessions() == []
    with pytest.raises(TmuxError, match="no server running"):
        live.kill_server()


@requires_tmux
def test_live_socket_path_is_where_tmux_put_it_and_outlives_the_server(live: TmuxServer) -> None:
    _spawn(live, "asq-test-fox", "w0", CAT)
    assert live.run("display-message", "-p", "#{socket_path}").strip() == str(live.socket_path())
    assert live.socket_path().is_socket()
    live.kill_server()
    assert live.list_sessions() == []
    assert live.socket_path().exists(), "the file tmux leaves behind — what the fixture tidies"


@requires_tmux
def test_live_check_conf_accepts_the_bundled_conf_and_rejects_a_bad_one(
    live: TmuxServer, tmp_path: Path
) -> None:
    assert _completes(live.check_conf)
    assert _check_leftovers(live) == [], "the throwaway server and its socket file are gone"
    assert live.list_sessions() == [], "the check never starts the fleet's own server"

    bad = tmp_path / "bad.conf"
    bad.write_text(BUNDLED_CONF + "set -g no-such-option on\n", encoding="utf-8")
    with pytest.raises(TmuxError, match="invalid option: no-such-option"):
        live.check_conf(bad)
    assert _check_leftovers(live) == [], "…including on the way out of a rejection"

    # The conf that started all this: a GLOBAL `window-size manual`, which below
    # tmux 3.7 is a null dereference in clients_calculate_size the first time a
    # window is created and above it is merely pointless. What check_conf owes
    # is not a fixed verdict but the RIGHT one — so the answer is measured on
    # this tmux, by doing to a scratch server exactly what the fleet does to
    # its own, and the check is then required to agree with it. (This is also
    # what the pre-round-1 shape could not see: `start-server ; source-file` on
    # a session-less server exits 0 for this file, measured on 3.4.)
    fatal = tmp_path / "fatal.conf"
    fatal.write_text(BUNDLED_CONF + "set -g window-size manual\n", encoding="utf-8")
    scratch = TmuxServer(live.socket + "-fatal", conf=fatal)
    try:
        scratch.run("new-session", "-d", "-s", "probe", "-c", "/", "--", "true")
        died: str | None = None
    except TmuxError as exc:
        died = str(exc)
    finally:
        with contextlib.suppress(TmuxError):
            scratch.kill_server()
        with contextlib.suppress(OSError):
            scratch.socket_path().unlink()

    if died is not None:
        assert "server exited unexpectedly" in died, f"an unexpected way to die: {died}"
        with pytest.raises(TmuxError, match="server exited unexpectedly"):
            live.check_conf(fatal)
    else:
        assert _completes(lambda: live.check_conf(fatal)), (
            "this tmux ran the conf without dying, so the check must not call it broken"
        )
    assert _check_leftovers(live) == []


@requires_tmux
def test_live_checks_running_at_once_do_not_report_each_other_as_a_bad_conf(
    live: TmuxServer,
) -> None:
    """The regression: one doctor's probe session read as the other's bad conf.

    The throwaway server used to live on a fixed socket that every check
    cleared before using, so two of them interleaved into A creating
    ``asq-conf-check`` between B's kill and B's own ``new-session`` — and tmux's
    answer to B, ``duplicate session: asq-conf-check`` (reproduced on 3.4), is
    on the one stderr this method reads as "tmux rejected your configuration".
    Nothing about the conf was wrong.

    A socket per call is what makes this a non-question, and this is the
    end-to-end statement of that: real servers, at once, on one fleet socket,
    all agreeing the conf is fine and none leaving a thing behind.
    """
    live.conf_path()  # write the conf once here, not from four threads at once
    ready = threading.Barrier(4)
    outcomes: list[BaseException | None] = []
    guard = threading.Lock()

    def check() -> None:
        ready.wait(timeout=10)
        try:
            live.check_conf()
        except BaseException as exc:  # a thread that raises must report, not vanish
            caught: BaseException | None = exc
        else:
            caught = None
        with guard:
            outcomes.append(caught)

    threads = [threading.Thread(target=check, name=f"check-{n}") for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a check never finished"
    assert outcomes == [None] * 4, f"a concurrent check called a good conf bad: {outcomes}"
    assert _check_leftovers(live) == []
    assert live.list_sessions() == [], "and none of them touched the fleet's own server"


def _server_is_up(socket: str) -> bool:
    """Does a tmux server answer on ``socket``? tmux's own answer, not the file's.

    ``display-message`` carries no ``CMD_STARTSERVER``, so asking cannot create
    what it is asking about — measured on 3.4: against a socket with no server
    it exits 1 with ``error connecting to …`` and leaves the directory empty.
    """
    server = TmuxServer(socket)
    return _tmux(server.argv("display-message", "-p", "#{pid}"), None).returncode == 0


def _strand_a_probe(live: TmuxServer, socket: str) -> Path:
    """A probe server in the state a run killed mid-chain leaves behind.

    The same session name, on the same kind of socket, under the same bundled
    conf — but without the ``kill-session`` that ends ``check_conf``'s chain. That
    conf's ``remain-on-exit on`` keeps the finished ``true`` pane, the pane keeps
    the window, the window keeps the session and the session keeps the server,
    so ``exit-empty`` never fires and nothing about this server ends on its own.
    """
    stranded = TmuxServer(socket)
    stranded.run("new-session", "-d", "-s", "asq-conf-check", "-c", "/", "--", "true")
    assert _server_is_up(socket), "the stranded probe never came up"
    return stranded.socket_path()


@requires_tmux
def test_live_a_probe_server_ends_itself_when_the_run_that_made_it_does_not(
    live: TmuxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``finally`` covers returning and raising. It does not cover SIGKILL.

    So the chain ends by killing its own session, which empties the server, and
    tmux's default ``exit-empty on`` does the rest — no cleanup code involved.
    This disables the cleanup entirely to prove that, keeping the socket name the
    disabled call was given so the abandoned server can still be looked at.

    What that is worth is measured by its opposite: :func:`_strand_a_probe`
    builds the same server WITHOUT that command, and the test below finds it
    still running until something reclaims it.
    """
    abandoned: list[str] = []
    monkeypatch.setattr(live, "_discard_server", abandoned.append)

    assert _completes(live.check_conf), "the conf is good; only the tidying was removed"
    assert len(abandoned) == 1, f"the cleanup this test replaced ran once: {abandoned}"
    socket = abandoned[0]

    assert _wait(lambda: not _server_is_up(socket)), (
        f"the probe server on {socket} outlived the run and nothing can name it now"
    )
    # The FILE is what tmux never removes, and what _discard_server would have.
    assert TmuxServer(socket).socket_path().exists()
    assert _check_leftovers(live) == [socket], "…so exactly one piece of litter, no server"


@requires_tmux
def test_live_check_conf_reclaims_a_probe_server_an_earlier_run_abandoned(
    live: TmuxServer,
) -> None:
    """The socket file is the only handle left on a server whose run was killed.

    Two of them here, in the state :func:`_strand_a_probe` describes, differing
    in one thing: how old their socket file is. Age is what separates a probe
    nobody will ever come back for from one a check running right now is in the
    middle of using — the situation the random socket name exists to protect —
    so the young one must survive a sweep that takes the old one.
    """
    old, young = (f"{live.socket}{CHECK_SOCKET_SUFFIX}-{tail}" for tail in ("0123abcd", "89abcdef"))
    old_file, young_file = _strand_a_probe(live, old), _strand_a_probe(live, young)
    stamp = time.time() - _ABANDONED
    os.utime(old_file, (stamp, stamp))

    assert _completes(live.check_conf), "reclaiming litter is not a verdict on the conf"

    assert not _server_is_up(old), "the abandoned probe server is still running"
    assert not old_file.exists(), "…and its socket file is still in tmux's directory"
    assert _server_is_up(young), "a probe young enough to belong to a running check was killed"
    assert young_file.exists()
    assert _check_leftovers(live) == [young], "nothing else of that shape was touched"
    assert live.list_sessions() == [], "and the fleet's own server was never started"


@requires_tmux
def test_live_the_conf_the_server_starts_with_survives_the_windows_it_is_there_to_hold(
    live: TmuxServer,
) -> None:
    """The regression: ``set -g window-size manual`` in that file killed the server.

    Below tmux 3.7 a GLOBAL ``window-size manual`` segfaults the server the next
    time any window is created — so a fleet whose ``-f`` file carried it died on
    the very first ``new-session`` (tmux 3.4, what ``ubuntu-latest`` ships) and
    every command after that answered ``server exited unexpectedly``. Moving the
    option onto a running server only moves the crash to the next spawn, which
    is why this walks the whole sequence: the server is born, takes a second
    window, then a second session, and every pane is still there.

    The option still has to end up ``manual``, so the test also pins where it
    now is (each window's own options) and where it must never be again (the
    server's globals).
    """
    first = _spawn(live, "asq-test-fox", "w0", CAT)  # the server comes into existence here
    second = _spawn(live, "asq-test-fox", "w1", CAT)  # …and survives another window
    third = _spawn(live, "asq-test-owl", "w0", CAT)  # …and another session
    windows = (first, second, third)

    assert live.list_sessions() == ["asq-test-fox", "asq-test-owl"]
    assert all(live.pane_facts(w.pane_id) is not None for w in windows)
    assert live.run("show-options", "-gv", "window-size").strip() != "manual", (
        "the global option is the one that kills tmux below 3.7 — the fleet never sets it"
    )
    assert [
        live.run("show-options", "-wv", "-t", w.window_id, "window-size").strip() for w in windows
    ] == ["manual", "manual", "manual"], "…and every window the fleet made is pinned"

    # Control on the instrument: a dead server does not quietly agree with any
    # of it — without which "every pane is still there" would prove nothing.
    live.kill_server()
    assert live.pane_facts(first.pane_id) is None
    with pytest.raises(TmuxError, match="no server running"):
        live.run("show-options", "-gv", "window-size")


@contextlib.contextmanager
def _attached_client(live: TmuxServer, session: str, width: int, height: int) -> Iterator[None]:
    """A REAL tmux client of exactly ``width`` x ``height`` attached to ``session``.

    Nothing else reproduces what this file is here to guard: ``window-size``
    decisions are made about CLIENTS, and tmux only counts a client that holds
    a terminal — ``refresh-client -C`` needs one to already exist, and
    ``new-session -x -y`` creates none. So this forks a pty, execs a genuine
    ``tmux attach`` in the child, and sets the pty's window size with
    ``TIOCSWINSZ`` before tmux measures it.

    A thread empties the master end for as long as the client lives. An
    attached client is sent the whole screen on every redraw, and a pty nobody
    reads holds about 64 KB before it backs up into tmux — so the drain is what
    keeps this a test of window sizes rather than of buffer sizes. It ends by
    itself: reading a pty master whose slave has gone raises ``EIO``.
    """
    argv = live.argv("attach", "-t", f"={session}")
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover — the child execs and never comes back
        try:
            os.execve(argv[0], argv, {**os.environ, "TERM": "xterm-256color"})
        finally:
            os._exit(127)

    def drain() -> None:
        with contextlib.suppress(OSError, ValueError):
            while os.read(master, 65536):
                pass

    reader = threading.Thread(target=drain, name="pty-drain", daemon=True)
    reader.start()

    def sized() -> bool:
        clients = live.run("list-clients", "-F", "#{client_width}x#{client_height}")
        return f"{width}x{height}" in clients.split()

    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        assert _wait(sized), f"no {width}x{height} client attached to {session}"
        yield
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)
        reader.join(timeout=5)  # the read ends with the child; close only after it
        with contextlib.suppress(OSError):
            os.close(master)


def _size(live: TmuxServer, window_id: str) -> str:
    return live.run(
        "display-message", "-p", "-t", window_id, "#{window_width}x#{window_height}"
    ).strip()


@requires_tmux
def test_live_a_window_is_born_the_size_the_fleet_asked_for_whoever_is_attached(
    live: TmuxServer,
) -> None:
    """The regression: the escape hatch decided what size every later agent got.

    Dropping the global ``window-size manual`` handed the CREATION-time rule
    back to tmux's default ``latest``, and ``latest`` means the newest CLIENT on
    the server — any client, on any session. So once a user opened the
    documented ``tmux -L asq attach`` from an 80x24 terminal, every window the
    fleet made afterwards was born 80x24 and the per-window pin then froze it
    there for the life of the agent. Reproduced on tmux 3.4 before the fix, in
    both shapes: a ``new-window`` into a 200x50 session, and a fresh
    ``new-session -d -x 200 -y 50`` on a session that client never touched.

    ``spawn_window`` now sizes the window itself, so the answer is the same
    number in every one of those situations. The last block is the control that
    makes the rest mean something: a window created the plain tmux way, with the
    same client attached, IS 80x24 — the client really is being offered and the
    fleet really is the thing refusing it.
    """
    asked, client = (132, 40), (80, 24)

    def spawn(session: str, name: str) -> WindowInfo:
        return live.spawn_window(
            session, name=name, cwd=Path("/tmp"), command=CAT, width=asked[0], height=asked[1]
        )

    headless_first = spawn("asq-test-fox", "w0")  # the server starts here, no client yet
    headless_next = spawn("asq-test-fox", "w1")
    assert [_size(live, w.window_id) for w in (headless_first, headless_next)] == ["132x40"] * 2

    with _attached_client(live, "asq-test-fox", *client):
        assert [_size(live, w.window_id) for w in (headless_first, headless_next)] == [
            "132x40"
        ] * 2, "the pin holds the windows that already existed"

        watched = spawn("asq-test-fox", "w2")  # a window in the session being watched
        elsewhere = spawn("asq-test-owl", "w0")  # …and a session of its own
        assert [_size(live, w.window_id) for w in (watched, elsewhere)] == ["132x40"] * 2

        raw = live.run(
            "new-window", "-d", "-P", "-F", "#{window_id}", "-t", "=asq-test-fox:",
            "-n", "raw", "-c", "/tmp", "--", *CAT,
        ).strip()  # fmt: skip
        assert _size(live, raw) == "80x24", (
            "control: tmux hands an unconfigured window the client's size, so the "
            "132x40 above is spawn_window's doing and not this tmux being polite"
        )

    assert [
        _size(live, w.window_id) for w in (headless_first, headless_next, watched, elsewhere)
    ] == ["132x40"] * 4, "…and none of them moved when the client went away"


@requires_tmux
def test_live_spawn_window_leaves_nothing_behind_when_it_cannot_configure_the_window(
    live: TmuxServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression, on a real server: the window outliving the error that denied it.

    Sizing a window is a second round trip, so it can fail once the window —
    and the agent command in it — is already running. ``services.fleet`` turns
    that TmuxError into "tmux could not start the window", and the window was
    measured still listed by ``list_windows`` afterwards, its process running,
    known to nothing.

    The failure is injected at :func:`_configure_window` because that is the
    only step that can fail there; what it emits instead is a command tmux
    genuinely refuses, aimed at the real window, so the server does the same
    thing it would do on a timeout or a rejection.
    """

    # Identity only: `current_command` walks from `sh` to `cat` as CAT execs and
    # `activity` is always true on a server nobody is attached to (module
    # docstring), so a whole-WindowInfo comparison would flake on both.
    def present(session: str) -> list[tuple[str, str, str]]:
        return [(w.window_id, w.name, w.pane_id) for w in live.list_windows(session)]

    live.spawn_window("asq-test-fox", name="w0", cwd=Path("/tmp"), command=CAT)
    before = present("asq-test-fox")

    def refuse(target: str, width: int, height: int) -> list[list[str]]:
        return [["set-option", "-w", "-t", target, "no-such-option", "on"]]

    monkeypatch.setattr(tmux_module, "_configure_window", refuse)
    with pytest.raises(TmuxError, match="invalid option"):
        live.spawn_window("asq-test-fox", name="doomed", cwd=Path("/tmp"), command=CAT)
    assert present("asq-test-fox") == before, "the window it made is gone again"
    assert "doomed" not in live.run("list-windows", "-a", "-F", "#{window_name}")

    # …and when the session was created by the same call, it goes too rather
    # than being left as an empty shell no agent will ever join.
    with pytest.raises(TmuxError, match="invalid option"):
        live.spawn_window("asq-test-owl", name="doomed", cwd=Path("/tmp"), command=CAT)
    assert live.list_sessions() == ["asq-test-fox"]

    # Control: with the real configure step restored the same spawn succeeds,
    # so the cleanup above is not hiding a spawn that never worked.
    monkeypatch.undo()
    survivor = live.spawn_window("asq-test-owl", name="fine", cwd=Path("/tmp"), command=CAT)
    assert present("asq-test-owl") == [(survivor.window_id, "fine", survivor.pane_id)]
    assert _size(live, survivor.window_id) == "200x50", "…configured, not merely created"


@requires_tmux
def test_live_every_bundled_option_is_applied_with_its_value(live: TmuxServer) -> None:
    """Task (c) of the spike: every option the fleet sets is live on this tmux.

    Two halves, because they are applied two ways (module docstring): BUNDLED_CONF,
    which the server reads at start, read back from its global options; and
    WINDOW_OPTIONS, which go on each window as it is created, read back from that
    window's own. Asserted on the server's view of each option, never on the exit
    status of ``-f`` — which is 0 whatever the file says. Every non-comment line
    must parse, so a line this test does not understand fails it rather than
    escaping it.
    """
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    lines = [line for line in BUNDLED_CONF.splitlines() if line and not line.startswith("#")]
    rules = [_SET.match(line) for line in lines]
    assert rules and all(rules), f"every line is a `set`: {lines}"

    for rule in rules:
        assert rule is not None
        flag, option, raw = rule.groups()
        value = raw.strip("'\"")
        shown = live.run("show-options", "-gv", option).splitlines()
        if flag == "-ga":
            assert value.lstrip(",") in shown, f"{option}: {value!r} not among {shown}"
        else:
            assert shown == [value], f"{option}: wanted {value!r}, server has {shown}"

    for option, value in WINDOW_OPTIONS:
        shown = live.run("show-options", "-wv", "-t", window.window_id, option).splitlines()
        assert shown == [value], f"{option}: wanted {value!r} on {window.window_id}, got {shown}"

    # Negative control on the second half: a window this module did NOT create
    # has the option unset — so `manual` above is spawn_window's doing and not
    # something every window on this tmux would have said.
    raw = live.run(
        "new-window", "-d", "-P", "-F", "#{window_id}", "-t", "=asq-test-fox:",
        "-n", "raw", "-c", "/tmp", "--", *CAT,
    ).strip()  # fmt: skip
    for option, _ in WINDOW_OPTIONS:
        assert live.run("show-options", "-wv", "-t", raw, option).splitlines() == []

    # Negative control on the instrument: an option that does not exist is an error.
    with pytest.raises(TmuxError, match="invalid option"):
        live.run("show-options", "-gv", "no-such-option")


@requires_tmux
def test_live_tmux_here_is_new_enough_for_the_fleet(live: TmuxServer) -> None:
    version = live.version()
    assert version is not None and version >= MIN_VERSION
    assert _completes(live.require)


@requires_tmux
@pytest.mark.parametrize(("width", "height"), [(80, 24), (200, 60)])
def test_live_a_frame_fits_the_render_budget(live: TmuxServer, width: int, height: int) -> None:
    """The plan's budget is 20 fps (§3.1): 50 ms a frame. Measured 5 to 7 ms here.

    The bound is loose on purpose — a CI runner is not this laptop — and exists
    to catch a pathological regression (a second process per frame, a
    per-command config rewrite, a 30 s timeout) rather than to pin a number.
    The measured medians are printed for the record (``pytest -s``).
    """
    filler = (
        "i=0; while [ $i -lt 400 ]; do printf '\\033[3%dm%s\\033[0m ' $((i%8)) "
        "'lorem ipsum dolor sit amet'; i=$((i+1)); done; exec cat"
    )
    window = live.spawn_window(
        "asq-test-fox", name="w0", cwd=Path("/tmp"), command=["sh", "-c", filler],
        width=width, height=height,
    )  # fmt: skip
    assert _wait(lambda: live.capture(window.pane_id).facts.current_command == "cat")

    samples: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        capture = live.capture(window.pane_id)
        samples.append((time.perf_counter() - started) * 1000)
    median = statistics.median(samples)
    print(f"\ncapture {width}x{height}: median {median:.1f} ms, max {max(samples):.1f} ms")
    assert len(capture.lines) == height and capture.facts.width == width
    assert median < 200, f"a {width}x{height} frame took {median:.0f} ms (median of 20)"
