"""The tmux wrapper: the argv it builds, the output it reads, and what a real server does.

Two layers, because they answer different questions. The fake-runner tests pin
the CONTRACT — which arguments reach tmux for each method and how tmux's
answers are read — and run everywhere, tmux or not. The live tests (skipped
where tmux is absent) pin the ASSUMPTIONS the contract rests on: that ``=name``
is exact, that a dead pane keeps its exit status, that ``display-message`` on a
gone pane succeeds with empty fields, that every bundled option is accepted by
this tmux with the value written. Each live server runs on its own private
socket (``asq-test-<pid>-<n>``) and is killed in teardown, so a failing test
cannot leave a server behind or touch a real ``asq`` fleet.

Every claim carries a negative control where it admits one (CONTRIBUTING,
"Writing a guard that still guards"): exact targeting is proved by a prefix
that must NOT match, the conf check by a conf that must be rejected, the
gone-pane answer by a live pane that must NOT be reported gone.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from aisquare.core import spawn
from aisquare.core import tmux as tmux_module
from aisquare.core.tmux import (
    _SEP,
    BUNDLED_CONF,
    CHECK_SOCKET_SUFFIX,
    CONF_NAME,
    MIN_VERSION,
    PASTE_BUFFER,
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


#: The permission bits below are advice to root, not a refusal, so a suite run
#: as root would prove the opposite of what the test claims.
not_root = pytest.mark.skipif(
    hasattr(os, "getuid") and os.getuid() == 0,
    reason="root writes an unwritable file anyway — the fail-open branch is unreachable",
)


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
    assert server.conf_fallback is None


@not_root
def test_a_conf_that_cannot_be_rewritten_fails_open_instead_of_raising(
    fake_bin: Path, isolated_home: Path
) -> None:
    """An unwritable conf may not cost a fleet COMMAND — only the rewrite.

    ``conf_path`` runs inside ``argv``, i.e. on every tmux invocation, and the
    OSError used to escape the class's contract entirely: ``fleet attach`` ended
    in a ``PermissionError`` traceback with nothing on stdout under ``--json``.
    The artefact is what tmux is handed and that the command completes; the cost
    is on ``conf_fallback`` so a caller can say it out loud.
    """
    isolated_home.mkdir(parents=True, exist_ok=True)
    path = isolated_home / CONF_NAME
    path.write_text("set -g status on  # what the last version wrote\n", encoding="utf-8")
    path.chmod(0o444)

    server = TmuxServer("s", binary=str(fake_bin), runner=FakeTmux())
    # Readable but not writable: tmux gets the stale file, and it is untouched.
    assert server.conf_path() == path
    assert server.argv("has-session")[4] == str(path)
    assert path.read_text(encoding="utf-8").startswith("set -g status on")
    assert server.conf_fallback is not None
    assert CONF_NAME in server.conf_fallback
    assert "whatever the last version wrote" in server.conf_fallback

    fresh = TmuxServer("s", binary=str(fake_bin), runner=FakeTmux())
    assert fresh.has_session("x") is True, "an unwritable conf does not cost the command"

    # Unreadable AND unwritable: measured on 3.7c, handing tmux an unreadable
    # -f file kills the server at startup ("server exited unexpectedly"), while
    # a missing one is fine — so this branch must NOT hand over the path.
    path.chmod(0o000)
    blind = TmuxServer("s", binary=str(fake_bin), runner=FakeTmux())
    assert blind.conf_path() == Path(os.devnull)
    assert blind.conf_fallback is not None and "/dev/null" in blind.conf_fallback

    # Negative control: with the file writable again nothing fails open, and the
    # drift is repaired — so the fallback is a response to the failure, not the
    # new normal.
    path.chmod(0o644)
    healthy = TmuxServer("s", binary=str(fake_bin), runner=FakeTmux())
    assert healthy.conf_path() == path
    assert healthy.conf_fallback is None
    assert path.read_text(encoding="utf-8") == BUNDLED_CONF


def test_a_home_that_cannot_be_created_costs_the_conf_and_not_the_command(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_home`` mkdir failure is the same class of accident, one step earlier."""

    def refuse() -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("aisquare.core.paths.ensure_home", refuse)
    fake = FakeTmux()
    server = TmuxServer("s", binary=str(fake_bin), runner=fake)
    assert server.conf_path() == Path(os.devnull)
    assert server.has_session("x") is True, "the command still runs"
    assert fake.calls[0][0][4] == os.devnull
    assert server.conf_fallback is not None and "aisquare home" in server.conf_fallback


def test_check_conf_sources_the_file_on_a_throwaway_server(fake_bin: Path, conf: Path) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf, socket="asq")
    assert _completes(server.check_conf)
    check = _prefix(fake_bin, Path(os.devnull), "asq" + CHECK_SOCKET_SUFFIX)
    assert fake.calls == [
        ([*check, "start-server", ";", "source-file", str(conf)], None),
        ([*check, "kill-server"], None),
    ]

    other = conf.with_name("other.conf")
    fake = FakeTmux()
    _server(fake, fake_bin, conf, socket="asq").check_conf(other)
    assert fake.calls[0][0][-1] == str(other)


def test_check_conf_reports_what_tmux_rejected(fake_bin: Path, conf: Path) -> None:
    # tmux 3.7c: `start-server ; source-file bad` exits 1 with the complaint on stderr.
    loud = FakeTmux(Completed(1, "", "invalid option: no-such-option\n"))
    with pytest.raises(TmuxError, match="invalid option: no-such-option"):
        _server(loud, fake_bin, conf).check_conf()
    assert len(loud.calls) == 2, "the throwaway server is still killed after a rejection"

    # A complaint with status 0 is still a rejection: tmux 3.7c was seen to print
    # `<file>:185: syntax error` and exit 0 when the file also held valid commands
    # (a Python module fed to source-file), so the status alone does not decide.
    quiet_rc = FakeTmux(Completed(0, "", "bad.conf:185: syntax error\n"))
    with pytest.raises(TmuxError, match="185: syntax error"):
        _server(quiet_rc, fake_bin, conf).check_conf()

    mute = FakeTmux(Completed(1, "", ""))
    with pytest.raises(TmuxError, match="without saying why"):
        _server(mute, fake_bin, conf).check_conf()


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
    assert "-x" not in new_window, "an existing session's size is the session's"
    assert (info.window_id, info.pane_id, info.current_command) == ("@5", "%10", "sh")


def test_spawn_window_without_env_passes_no_dash_e(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    fake = FakeTmux(OK, Completed(0, f"@1{_SEP}%1\n", ""))
    _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox", name="w", cwd=tmp_path, command=["cat"]
    )
    assert "-e" not in fake.commands()[1]


def test_spawn_window_escapes_the_separator_in_every_argument_it_carries(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    """An argument ending in ``;`` would end the tmux command; it must arrive escaped.

    The launch command reaches here from ``fleet spawn … -- <args>`` and from a
    role's configured ``extra_args``, so a ``;`` in it is caller data, not tmux
    syntax. What escaping prevents is measured live below.
    """
    directory = tmp_path / "dir;"
    directory.mkdir()
    fake = FakeTmux(OK, Completed(0, f"@1{_SEP}%1\n", ""))
    _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox",
        name="coder;",
        cwd=directory,
        command=["claude", "--flag", "a;b", ";", "kill-server", "trailing;"],
        env={"K": "v;"},
    )
    assert fake.commands()[1] == [
        "new-window", "-d", "-P", "-F", f"#{{window_id}}{_SEP}#{{pane_id}}",
        "-t", "=asq-amber-fox:", "-n", "coder\\;", "-c", f"{tmp_path}/dir\\;",
        "-e", "K=v\\;",
        # "a;b" is data to tmux already: only a LAST ";" separates commands.
        "--", "claude", "--flag", "a;b", "\\;", "kill-server", "trailing\\;",
    ]  # fmt: skip


def test_spawn_window_leaves_an_argument_without_a_trailing_separator_alone(
    fake_bin: Path, conf: Path, tmp_path: Path
) -> None:
    """The negative control on the escape: it fires only where tmux would split."""
    command = ["claude", "-p", "a;b", "--append", "one; two", "x\\"]
    fake = FakeTmux(OK, Completed(0, f"@1{_SEP}%1\n", ""))
    _server(fake, fake_bin, conf).spawn_window(
        "asq-amber-fox", name="coder-1", cwd=tmp_path, command=command, env={"K": "v"}
    )
    assert "\\;" not in " ".join(fake.commands()[1])
    assert fake.commands()[1][-len(command) :] == command


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
    fake = FakeTmux()
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
    """…including the escape send_literal's docstring records.

    A TRAILING ``;`` is tmux's command separator even after ``-l --``: measured
    on 3.7c, ``send-keys -l -- 'a;'`` puts ``a`` in the pane and drops the
    semicolon (proved live in ``test_live_send_literal_delivers_a_trailing_semicolon``).
    A ``;`` anywhere else is already data — the negative control on the escape.
    """
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    server.send_keys("%3", "C-c", "Enter")
    server.send_literal("%3", "-dash text; not a command")
    server.send_literal("%3", "a;")
    server.send_keys("%3")
    server.send_literal("%3", "")
    assert fake.commands() == [
        ["send-keys", "-t", "%3", "C-c", "Enter"],
        ["send-keys", "-t", "%3", "-l", "--", "-dash text; not a command"],
        ["send-keys", "-t", "%3", "-l", "--", "a\\;"],
    ], "nothing to send is not a tmux call"


def test_paste_loads_the_buffer_from_stdin_then_pastes_bracketed(
    fake_bin: Path, conf: Path
) -> None:
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    server.paste("%3", "line one\nline two\n")
    server.paste("%3", "")
    load, pasted = fake.commands()
    buffer_name = load[2]
    assert load == ["load-buffer", "-b", buffer_name, "-"]
    assert pasted == ["paste-buffer", "-p", "-d", "-b", buffer_name, "-t", "%3"]
    assert fake.calls[0][1] == b"line one\nline two\n"
    assert fake.calls[1][1] is None


def test_every_paste_loads_a_buffer_of_its_own(fake_bin: Path, conf: Path) -> None:
    """Two pastes may not share a buffer name — one shared name crossed panes.

    ``load`` and ``paste`` are separate invocations, so with a single fixed name
    an interleaved pair delivers B's text to pane A (the live test reproduces
    exactly that as its control). The name therefore carries the pid, which
    separates processes, AND a counter, which separates calls in one.
    """
    fake = FakeTmux()
    server = _server(fake, fake_bin, conf)
    server.paste("%3", "for the first agent")
    server.paste("%4", "for the second agent")
    first_load, first_paste, second_load, second_paste = fake.commands()

    first, second = first_load[2], second_load[2]
    assert first_paste[4] == first, "a paste must name the buffer its own load created"
    assert second_paste[4] == second
    assert first != second, "two pastes in one process share a name"
    for name in (first, second):
        assert name.startswith(f"{PASTE_BUFFER}-{os.getpid()}-"), name


def test_a_paste_that_fails_does_not_leave_its_text_on_the_server(
    fake_bin: Path, conf: Path
) -> None:
    """A per-call name is never reused, so a failed paste's buffer would linger.

    It holds an agent's prompt on a server the user can attach to, where
    ``prefix-]`` would deliver it to whatever pane they are looking at.
    """
    fake = FakeTmux(OK, Completed(1, "", "can't find pane: %9\n"))
    with pytest.raises(TmuxError, match="can't find pane: %9"):
        _server(fake, fake_bin, conf).paste("%9", "unsent prompt")
    load, pasted, deleted = fake.commands()
    assert deleted == ["delete-buffer", "-b", load[2]]
    assert pasted[4] == load[2]

    # Negative control: a paste that works does not delete anything itself —
    # `-d` already did, and a third invocation per keystroke is not free.
    quiet = FakeTmux()
    _server(quiet, fake_bin, conf).paste("%3", "sent")
    assert [command[0] for command in quiet.commands()] == ["load-buffer", "paste-buffer"]


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


def test_the_seam_strips_the_whole_tracing_identity_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every name of the identity, in the child's OWN environment.

    The marker pair matters as much as the headers here: this server hands its
    environment to every window, and an agent that launches untraced (the
    default) keeps whatever it inherited — ``core.insights.run_key`` then files
    that agent's insights under the Run of whoever started the server, and the
    hook writes a session→Run join that is not true. docs/fleet.md: "the tmux
    server inherits nothing of a tracing identity from whoever started it".
    """
    for name in spawn.IDENTITY_ENV_VARS:
        monkeypatch.setenv(name, f"parent-{name}")
    monkeypatch.setenv("ASQ_TEST_PASSTHROUGH", "kept")
    probe = "import os,sys; print(' '.join(os.environ.get(n, '<unset>') for n in sys.argv[1:]))"
    names = [*spawn.IDENTITY_ENV_VARS, "ASQ_TEST_PASSTHROUGH"]
    completed = _tmux([sys.executable, "-c", probe, *names], None)
    assert completed.returncode == 0
    assert completed.stdout.split() == ["<unset>"] * len(spawn.IDENTITY_ENV_VARS) + ["kept"]

    # Negative control on the probe: without the strip it reports the values, so
    # a row of "<unset>" is the seam's work and not a blind reader.
    unstripped = subprocess.run(
        [sys.executable, "-c", probe, *names], capture_output=True, text=True, check=True
    )
    assert unstripped.stdout.split() == [f"parent-{n}" for n in spawn.IDENTITY_ENV_VARS] + ["kept"]


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


@pytest.fixture
def live() -> Iterator[TmuxServer]:
    """A TmuxServer on a private socket of its own, killed whatever the test did."""
    server = TmuxServer(f"asq-test-{os.getpid()}-{next(_SOCKETS)}")
    try:
        yield server
    finally:
        for socket in (server.socket, server.socket + CHECK_SOCKET_SUFFIX):
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
def test_live_two_pastes_at_once_reach_the_pane_they_were_addressed_to(live: TmuxServer) -> None:
    """The race, staged deterministically: B's whole paste runs inside A's.

    ``paste`` is ``load-buffer`` then ``paste-buffer``, and two processes can
    interleave there — two ``fleet tell``s, a ``spawn --prompt`` beside a
    ``tell``, the UI's ``on_paste`` beside either. The artefact is the panes:
    each must hold the text ADDRESSED to it and not the other's. The control at
    the end is the old mechanism — one fixed buffer name — which delivers B's
    text to pane A and then cannot paste to B at all.
    """
    first = _spawn(live, "asq-test-fox", "w0", CAT)
    second = _spawn(live, "asq-test-fox", "w1", CAT)
    assert _wait(lambda: "plain" in _screen(live, first.pane_id))
    assert _wait(lambda: "plain" in _screen(live, second.pane_id))

    other = TmuxServer(live.socket)
    staged = {"fired": False}

    def interleave(argv: Sequence[str], stdin: bytes | None) -> Completed:
        """The real seam, with the OTHER paste squeezed in after the load."""
        result = _tmux(argv, stdin)
        if not staged["fired"] and "load-buffer" in argv:
            staged["fired"] = True
            other.paste(second.pane_id, "FOR-THE-SECOND-AGENT\n")
        return result

    TmuxServer(live.socket, runner=interleave).paste(first.pane_id, "FOR-THE-FIRST-AGENT\n")
    assert staged["fired"], "the interleaved paste never ran — the race was not staged"

    assert _wait(lambda: "FOR-THE-FIRST-AGENT" in _screen(live, first.pane_id))
    assert _wait(lambda: "FOR-THE-SECOND-AGENT" in _screen(live, second.pane_id))
    assert "FOR-THE-SECOND-AGENT" not in _screen(live, first.pane_id)
    assert "FOR-THE-FIRST-AGENT" not in _screen(live, second.pane_id)
    assert live.run("list-buffers") == "", "each call's own -d deleted its own buffer"

    # The control on the mechanism: ONE name, the same interleaving.
    shared = "asq-test-one-name"
    live.run("load-buffer", "-b", shared, "-", stdin=b"CROSSED-FIRST\n")
    live.run("load-buffer", "-b", shared, "-", stdin=b"CROSSED-SECOND\n")
    live.run("paste-buffer", "-p", "-d", "-b", shared, "-t", first.pane_id)
    assert _wait(lambda: "CROSSED-SECOND" in _screen(live, first.pane_id))
    assert "CROSSED-FIRST" not in _screen(live, first.pane_id), "the loser's text is gone"
    with pytest.raises(TmuxError, match=f"no buffer {shared}"):
        live.run("paste-buffer", "-p", "-d", "-b", shared, "-t", second.pane_id)
    assert "CROSSED-SECOND" not in _screen(live, second.pane_id)


@requires_tmux
def test_live_send_literal_delivers_a_trailing_semicolon(live: TmuxServer) -> None:
    """``send_literal``'s escape, measured against the pane it types into.

    tmux ends a command at ANY argument whose last character is ``;`` — ``-l --``
    does not stop it — so unescaped, the semicolon never reaches the agent and
    whatever followed it in the text would be run as a tmux command. The control
    at the end is the same text sent raw: the ``;`` is dropped.
    """
    window = _spawn(live, "asq-test-fox", "w0", CAT)
    assert _wait(lambda: "plain" in _screen(live, window.pane_id))

    live.send_literal(window.pane_id, "run make;")
    live.send_literal(window.pane_id, "END")
    live.send_keys(window.pane_id, "Enter")
    assert _wait(lambda: "run make;END" in _screen(live, window.pane_id))

    # The control on the mechanism: raw, tmux eats the separator.
    live.run("send-keys", "-t", window.pane_id, "-l", "--", "run make;")
    live.send_literal(window.pane_id, "RAW")
    live.send_keys(window.pane_id, "Enter")
    assert _wait(lambda: "run makeRAW" in _screen(live, window.pane_id))


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


@requires_tmux
def test_live_a_finished_command_is_a_dead_pane_with_its_status(live: TmuxServer) -> None:
    keeper = _spawn(live, "asq-test-fox", "w0", CAT)
    exiting = _spawn(live, "asq-test-fox", "w1", EXIT_3)

    def dead() -> WindowInfo | None:
        return next(
            (w for w in live.list_windows("asq-test-fox") if w.pane_id == exiting.pane_id), None
        )

    assert _wait(lambda: (found := dead()) is not None and found.dead)
    found = dead()
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
def test_live_spawn_cannot_be_talked_into_running_a_second_tmux_command(
    live: TmuxServer, tmp_path: Path
) -> None:
    """A ``;`` in the launch command must reach the AGENT, never tmux's parser.

    The artefact is the pane's own argv, written to a file, plus a server option
    the injected command would have changed. The control at the end is the same
    argv sent to tmux unescaped: it DOES run, which is what the escape prevents
    — with ``kill-server`` in place of ``set`` it would take every agent on the
    private server with it.
    """
    argv_file = tmp_path / "argv.txt"
    injection = [";", "set", "-g", "history-limit", "1"]
    command = ["sh", "-c", f'printf "[%s]\\n" "$@" > {argv_file}; exec cat', "sh", *injection]

    window = live.spawn_window(
        "asq-test-fox", name="w0", cwd=tmp_path, command=command, width=80, height=24
    )
    assert _wait(lambda: argv_file.exists())
    assert argv_file.read_text(encoding="utf-8").splitlines() == [f"[{arg}]" for arg in injection]
    assert live.pane_facts(window.pane_id) is not None, "the window is alive, not a parse error"
    assert live.run("show", "-g", "history-limit").strip() == "history-limit 50000"

    # The control on the mechanism: unescaped, tmux runs the tail itself.
    live.run(
        "new-window", "-d", "-t", "=asq-test-fox:", "-n", "raw", "-c", str(tmp_path),
        "--", *command,
    )  # fmt: skip
    assert live.run("show", "-g", "history-limit").strip() == "history-limit 1"


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
    assert TmuxServer(live.socket + CHECK_SOCKET_SUFFIX).list_sessions() == []
    assert live.list_sessions() == [], "the check never starts the fleet's own server"

    bad = tmp_path / "bad.conf"
    bad.write_text(BUNDLED_CONF + "set -g no-such-option on\n", encoding="utf-8")
    with pytest.raises(TmuxError, match="invalid option: no-such-option"):
        live.check_conf(bad)
    assert TmuxServer(live.socket + CHECK_SOCKET_SUFFIX).list_sessions() == []


_SET = re.compile(r"^set (-g|-s|-ga) (\S+) (.+)$")


@requires_tmux
def test_live_every_bundled_option_is_applied_with_its_value(live: TmuxServer) -> None:
    """Task (c) of the spike: each option in BUNDLED_CONF loads on this tmux.

    Asserted on the server's own view of each option, not on the exit status of
    ``-f`` — which is 0 whatever the file says (module docstring). Every
    non-comment line must parse, so a line this test does not understand fails
    it rather than escaping it.
    """
    _spawn(live, "asq-test-fox", "w0", CAT)
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
