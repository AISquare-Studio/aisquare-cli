"""``core.procs``: one process's parent, start token and image, the same on every platform.

The parsers run on every platform against the text each source really prints; the real-process
tests run on every platform against the machine's own tree, which is what the Linux, macOS and
Windows legs of CI are for (Windows reads the Toolhelp snapshot and ``GetProcessTimes``, which
no parser covers).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core import procs

# --- the parsers ------------------------------------------------------------------------------

#: ``/proc/<pid>/stat`` of a shell: field 22, ``starttime``, is 8806317.
STAT = (
    "35373 (zsh) S 12998 35373 35373 34816 35373 4194560 3191 69172 0 8 15 29 90 134 20 0 1 0 "
    "8806317 3338981376 3140 18446744073709551615 4194304 4285989 140737488 0 0 0 2 0 3670020"
)


def test_stat_gives_the_parent_and_the_start_tick() -> None:
    assert procs.parse_stat(STAT) == (12998, "8806317")


def test_stat_counts_its_fields_from_the_last_parenthesis() -> None:
    """A command may hold spaces and parentheses, and the fields after it must not move."""
    tricky = STAT.replace("(zsh)", "(a b) (c))")
    assert procs.parse_stat(tricky) == (12998, "8806317")


@pytest.mark.parametrize("text", ["", "no parentheses here", "1 (x) S", "1 (x) S abc " + "1 " * 30])
def test_a_line_that_is_not_stat_is_not_read(text: str) -> None:
    assert procs.parse_stat(text) is None


def test_ps_gives_the_parent_the_start_and_the_command() -> None:
    line = " 20171 Mon Oct 26 00:11:43 2026 /Users/dev/.local/bin/claude\n"
    assert procs.parse_ps(line) == procs.ProcInfo(
        20171, "Mon Oct 26 00:11:43 2026", "/Users/dev/.local/bin/claude"
    )


def test_ps_reads_a_day_padded_with_a_space_and_a_command_with_spaces() -> None:
    line = "    1 Sun Oct  4 09:00:01 2026 /Applications/Claude Code.app/Contents/MacOS/claude"
    info = procs.parse_ps(line)
    assert info == procs.ProcInfo(
        1, "Sun Oct 4 09:00:01 2026", "/Applications/Claude Code.app/Contents/MacOS/claude"
    )


@pytest.mark.parametrize("text", ["", "abc Mon Oct 26 00:11:43 2026 /bin/x", "12 Mon Oct 26"])
def test_a_line_that_is_not_ps_is_not_read(text: str) -> None:
    assert procs.parse_ps(text) is None


@pytest.mark.parametrize(
    ("image", "stem"),
    [
        ("/usr/local/bin/uv", "uv"),
        ("C:\\Tools\\UV.EXE", "uv"),
        ("C:\\x\\Scripts\\aisquare.exe", "aisquare"),
        ("uvx", "uvx"),
        ("", ""),
        ("/opt/Claude Code/claude", "claude"),
    ],
)
def test_an_image_goes_by_its_stem(image: str, stem: str) -> None:
    assert procs.image_stem(image) == stem


# --- each source, on any platform: a /proc tree and a ps of our own -----------------------------


def _fake_proc(root: Path, pid: int, ppid: int, ticks: int, exe: str | None) -> None:
    where = root / str(pid)
    where.mkdir(parents=True)
    fields = " ".join(["0"] * 17)
    (where / "stat").write_text(f"{pid} (x y) S {ppid} {fields} {ticks} 0 0\n", encoding="utf-8")
    if exe is not None:
        (where / "exe").symlink_to(exe)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_linux_reads_its_processes_from_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proc"
    _fake_proc(root, 500, 1, 424242, "/usr/bin/claude")
    _fake_proc(root, 501, 500, 424300, None)  # another user's: no readable exe
    boot = root / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("0a1b2c3d-1111-2222-3333-444455556666\n", encoding="ascii")
    monkeypatch.setattr(procs, "_PROC", root)
    monkeypatch.setattr(sys, "platform", "linux")

    assert procs.inspect(500) == procs.ProcInfo(1, "0a1b2c3d.424242", "/usr/bin/claude")
    assert procs.inspect(501) == procs.ProcInfo(500, "0a1b2c3d.424300", "")
    assert procs.inspect(502) is None, "a pid with no /proc entry"
    (root / "501" / "stat").write_text("garbage", encoding="utf-8")
    assert procs.inspect(501) is None, "a stat that is not one"


def test_a_start_from_another_boot_is_another_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same pid and the same tick count after a reboot are not the same process."""
    root = tmp_path / "proc"
    (root / "sys/kernel/random").mkdir(parents=True)
    _fake_proc(root, 500, 1, 1000, None)
    monkeypatch.setattr(procs, "_PROC", root)
    monkeypatch.setattr(sys, "platform", "linux")
    (root / "sys/kernel/random/boot_id").write_text("aaaaaaaa-0\n", encoding="ascii")
    first = procs.inspect(500)
    (root / "sys/kernel/random/boot_id").write_text("bbbbbbbb-0\n", encoding="ascii")
    assert first is not None and procs.inspect(500) != first


def test_macos_asks_ps_with_the_locale_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        asked.append((argv, dict(kwargs["env"])))  # type: ignore[call-overload]
        return subprocess.CompletedProcess(argv, 0, " 77 Mon Oct 26 00:01:10 2026 /bin/zsh\n", "")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert procs.inspect(1234) == procs.ProcInfo(77, "Mon Oct 26 00:01:10 2026", "/bin/zsh")
    argv, env = asked[0]
    assert argv == ["ps", "-o", "ppid=,lstart=,comm=", "-p", "1234"]
    assert env["LC_ALL"] == "C"


def test_a_ps_that_fails_or_is_missing_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")
    )
    assert procs.inspect(1234) is None

    def missing(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", missing)
    assert procs.inspect(1234) is None


# --- the machine's own tree ------------------------------------------------------------------


@pytest.fixture
def child() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def test_this_process_is_inspected_with_its_parent_and_a_start() -> None:
    info = procs.inspect(os.getpid())
    assert info is not None
    assert info.ppid == os.getppid()
    assert info.started != ""
    assert info.image != ""
    assert procs.inspect(os.getpid()) == info, "the token is the same every time it is asked"
    assert procs.current() == procs.ProcessRef(os.getpid(), info.started)


def test_a_child_is_seen_under_this_process_with_a_start_of_its_own(
    child: subprocess.Popen[bytes],
) -> None:
    info = procs.inspect(child.pid)
    assert info is not None
    assert info.ppid == os.getpid()
    assert info.started != ""
    assert procs.inspect(child.pid) == info, "a process keeps its token"
    assert procs.image_stem(info.image).startswith("python")


def test_a_process_sees_the_same_token_for_its_parent_as_the_parent_does() -> None:
    """The proof compares a token the hook took with one the server took, in two processes:
    they must agree on what one process's start was."""
    code = (
        "import os, sys\n"
        "from aisquare.core import procs\n"
        "i = procs.inspect(os.getppid())\n"
        "print(os.getppid(), i.started if i else '')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    ).stdout.split(maxsplit=1)
    seen = procs.inspect(int(out[0]))
    assert seen is not None
    assert out[1].strip() == seen.started


def test_a_process_that_is_gone_is_not_a_process() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    deadline = time.monotonic() + 10
    while procs.inspect(proc.pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)  # Windows keeps a finished process readable while a handle is open
    assert procs.inspect(proc.pid) is None


@pytest.mark.parametrize("pid", [0, -1, 2**31 - 2])
def test_a_pid_that_names_nothing_is_none(pid: int) -> None:
    assert procs.inspect(pid) is None
