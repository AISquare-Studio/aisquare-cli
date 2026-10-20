"""Copies reach the system clipboard beside OSC 52, and the toast says so only when true.

Hotfix 0.8.2: on macOS every copy said "copied N characters" and left the clipboard
unchanged, because Textual writes only an OSC 52 escape, which Terminal.app never
implemented and iTerm2 drops unless a setting is on. ``core.clipboard`` picks the
platform's own tool and runs it off the event loop; ``cli.ui.clipboard.ClipboardApp``
sends the OSC 52 as before and raises the copy's toast once that write is over.

The writer tables are asked with a fake ``which``. Everything else runs a real fake
tool on PATH (tests/fake_clipboard.py): the pane's own copies, the agent-selection
mirror and Textual's ``Input`` through the headless ``Host`` of test_terminal_pane;
the board's ``c`` and the R panel's Copy link have theirs in test_watch and
test_ui_remote.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.app import App
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.selection import Selection
from textual.widgets import Input, Static

from aisquare.cli.ui.clipboard import (
    HEDGED_TIMEOUT,
    MISSED_TIMEOUT,
    Toast,
    copy_and_tell,
    copy_toast,
)
from aisquare.core import clipboard
from aisquare.core.clipboard import Writer, osc52_terminal, pick_writer
from tests.fake_clipboard import NEEDS_POSIX, TEXT, install
from tests.pane_harness import FakePane, FakeTmux, drag
from tests.test_terminal_pane import Host, run, synced, wait_until

TERMINAL_APP_MISSED = copy_toast("drops", "copied", "N characters", None).message


def iterm_hedge(sent: str) -> str:
    """What a copy only OSC 52 carried to iTerm2 says of ``sent``."""
    return copy_toast("asks", "copied", sent, None).message


def hedged_copy(sent: str) -> str:
    """What a copy only OSC 52 carried to an unnamed terminal says of ``sent``."""
    return copy_toast("unknown", "copied", sent, None).message


def osc52(text: str) -> str:
    return f"\x1b]52;c;{base64.b64encode(text.encode('utf-8')).decode('ascii')}\a"


TOOLS = ("pbcopy", "clip.exe", "wl-copy", "xclip", "xsel")
"""Every clipboard tool ``pick_writer`` asks ``which`` for."""


def tools(*names: str) -> Callable[[str], str | None]:
    """A ``which`` that finds exactly ``names``, under /fake/bin."""
    return lambda name: f"/fake/bin/{name}" if name in names else None


@pytest.fixture
def fake() -> FakeTmux:
    tmux = FakeTmux()
    tmux.panes["%1"] = FakePane(screen=[TEXT, "second row", "third row"], cursor=(2, 2))
    return tmux


# --- which tool writes the clipboard ---------------------------------------------------


def test_macos_writes_with_pbcopy_under_a_utf8_locale() -> None:
    """``pbcopy`` reads stdin in the locale's encoding, MacRoman with none set."""
    writer = pick_writer({}, platform="darwin", which=tools("pbcopy", "xclip"))
    assert writer == Writer(("/fake/bin/pbcopy",), env=(("LC_ALL", "en_US.UTF-8"),))
    assert writer.payload(TEXT) == TEXT.encode("utf-8")
    assert pick_writer({"DISPLAY": ":0"}, platform="darwin", which=tools("xclip")) is None


WAYLAND = {"WAYLAND_DISPLAY": "wayland-0"}
X11 = {"DISPLAY": ":0"}
WL_COPY = ("wl-copy", "--type", "text/plain")
XCLIP = ("xclip", "-selection", "clipboard")
XSEL = ("xsel", "--clipboard", "--input")


@pytest.mark.parametrize(
    ("env", "found", "argv"),
    [
        (WAYLAND, ("wl-copy", "xclip"), WL_COPY),
        (WAYLAND | X11, ("xclip",), XCLIP),
        (X11, ("wl-copy", "xclip", "xsel"), XCLIP),
        (X11, ("xsel",), XSEL),
        (X11, (), None),
        (WAYLAND, ("xclip", "xsel"), None),
        ({}, ("wl-copy", "xclip", "xsel", "clip.exe"), None),
    ],
    ids=[
        "wayland",
        "xwayland-without-wl-copy",
        "x11-xclip",
        "x11-xsel",
        "x11-no-tool",
        "wayland-no-wl-copy",
        "no-display",
    ],
)
def test_linux_picks_wl_copy_then_xclip_then_xsel_by_display(
    env: dict[str, str], found: tuple[str, ...], argv: tuple[str, ...] | None
) -> None:
    writer = pick_writer(env, platform="linux", which=tools(*found), wsl=False)
    if argv is None:
        assert writer is None
        return
    assert writer is not None
    assert writer.argv == (f"/fake/bin/{argv[0]}", *argv[1:])
    assert writer.payload(TEXT) == TEXT.encode("utf-8")


def _binfmt(root: Path, *entries: str) -> Path:
    """A mounted binfmt_misc holding ``entries``."""
    binfmt = root / "binfmt_misc"
    binfmt.mkdir()
    for name in ("status", "register", *entries):
        (binfmt / name).write_text("enabled\n")
    return binfmt


def test_wsl_writes_with_clip_exe_in_utf16_after_a_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clip.exe`` reads stdin in the console's codepage unless it is told UTF-16."""
    monkeypatch.setattr(clipboard, "_WINDOWS_CLIP", tmp_path / "no-clip.exe")
    monkeypatch.setattr(clipboard, "_BINFMT", _binfmt(tmp_path, "WSLInterop"))
    wsl = {"WSL_DISTRO_NAME": "Ubuntu", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    writer = pick_writer(wsl, platform="linux", which=tools("clip.exe", "wl-copy"))
    assert writer is not None and writer.argv == ("/fake/bin/clip.exe",)
    assert writer.payload(TEXT) == b"\xff\xfe" + TEXT.encode("utf-16-le")
    assert writer.payload(TEXT).decode("utf-16") == TEXT
    # Interop off: no clip.exe, and WSLg's own display still has wl-copy.
    fallback = pick_writer(wsl, platform="linux", which=tools("wl-copy"))
    assert fallback is not None and fallback.argv[0] == "/fake/bin/wl-copy"
    # Windows kept off PATH (appendWindowsPath=false): clip.exe where WSL mounts it.
    monkeypatch.setattr(clipboard, "_WINDOWS_CLIP", tmp_path / "clip.exe")
    (tmp_path / "clip.exe").write_bytes(b"")
    mounted = pick_writer(wsl, platform="linux", which=tools())
    assert mounted is not None and mounted.argv == (str(tmp_path / "clip.exe"),)


def test_the_bom_is_an_escape_in_the_source_not_an_invisible_character() -> None:
    """A literal U+FEFF shows as nothing: an editor or formatter that strips it leaves ""
    with no visible diff, and clip.exe then reads the console's codepage and mangles
    every character outside it (review of #266, round 1)."""
    source = Path(clipboard.__file__).read_text(encoding="utf-8")
    assert "\ufeff" not in source
    assert '"\\ufeff"' in source


@pytest.mark.parametrize(
    ("entries", "env", "interop"),
    [
        (("WSLInterop",), {}, True),
        (("WSLInterop-late",), {}, True),
        ((), {"WSL_INTEROP": "/run/WSL/1_interop"}, False),  # binfmt_misc says off
        (None, {"WSL_INTEROP": "/run/WSL/1_interop"}, True),  # not mounted: the variable says
        (None, {}, False),
    ],
    ids=["registered", "registered-late", "off", "unmounted-variable", "unmounted-nothing"],
)
def test_wsl_interop_is_known_by_binfmt_misc_else_by_its_variable(
    entries: tuple[str, ...] | None,
    env: dict[str, str],
    interop: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binfmt = tmp_path / "unmounted" if entries is None else _binfmt(tmp_path, *entries)
    monkeypatch.setattr(clipboard, "_BINFMT", binfmt)
    assert clipboard.wsl_interop(env) is interop


def test_with_interop_off_wsl_writes_with_wslg_s_tools_not_clip_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[interop] enabled=false`` leaves clip.exe on disk under /mnt/c, and running it
    fails with ENOEXEC at every copy (review of #266, round 1)."""
    clip = tmp_path / "clip.exe"
    clip.write_bytes(b"")
    monkeypatch.setattr(clipboard, "_WINDOWS_CLIP", clip)
    monkeypatch.setattr(clipboard, "_BINFMT", _binfmt(tmp_path))
    wslg = {"WSL_DISTRO_NAME": "Ubuntu", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    writer = pick_writer(wslg, platform="linux", which=tools("clip.exe", "wl-copy"))
    assert writer is not None and writer.argv[0] == "/fake/bin/wl-copy"
    with_x = pick_writer(
        {"WSL_DISTRO_NAME": "Ubuntu", "DISPLAY": ":0"}, platform="linux", which=tools("xclip")
    )
    assert with_x is not None and with_x.argv[0] == "/fake/bin/xclip"
    assert (
        pick_writer({"WSL_DISTRO_NAME": "Ubuntu"}, platform="linux", which=tools("clip.exe"))
        is None
    )


def test_wsl_is_known_by_its_variables_or_by_the_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = tmp_path / "version"
    monkeypatch.setattr(clipboard, "_PROC_VERSION", version)
    assert clipboard.in_wsl({"WSL_DISTRO_NAME": "Ubuntu"})
    assert clipboard.in_wsl({"WSL_INTEROP": "/run/WSL/1_interop"})
    assert not clipboard.in_wsl({}), "no /proc/version to read"
    monkeypatch.setattr(clipboard, "_BINFMT", _binfmt(tmp_path, "WSLInterop-late"))
    version.write_text("Linux version 6.6.87.2-microsoft-standard-WSL2 (gcc 11.2.0)\n")
    assert clipboard.in_wsl({})
    with_x = pick_writer({"DISPLAY": ":0"}, platform="linux", which=tools("clip.exe", "xclip"))
    assert with_x is not None and with_x.argv == ("/fake/bin/clip.exe",)
    version.write_text("Linux version 6.11.4-301.fc41.x86_64 (mockbuild@fedoraproject.org)\n")
    assert not clipboard.in_wsl({})


def test_native_windows_writes_with_clip_exe_in_utf16_after_a_bom() -> None:
    """``aisquare watch`` and asq's Welcome page run on native Windows, in a conhost that
    knows no OSC 52: the board's ``c`` changed nothing there (review of #266, round 1)."""
    writer = pick_writer({}, platform="win32", which=tools("clip.exe", "xclip"))
    assert writer == Writer(("/fake/bin/clip.exe",), encoding="utf-16-le", bom=True)
    assert writer.payload(TEXT) == b"\xff\xfe" + TEXT.encode("utf-16-le")
    assert pick_writer({"DISPLAY": ":0"}, platform="win32", which=tools("xclip")) is None


@pytest.mark.parametrize(
    ("platform", "env", "found", "argv"),
    [
        ("freebsd14", X11, ("xclip", "xsel"), XCLIP),
        ("openbsd7", X11, ("xsel",), XSEL),
        ("netbsd10", WAYLAND, ("wl-copy",), WL_COPY),
        ("freebsd14", {}, ("wl-copy", "xclip", "xsel"), None),
    ],
    ids=["freebsd-xclip", "openbsd-xsel", "netbsd-wayland", "no-display"],
)
def test_the_bsds_write_with_the_wayland_or_x_tools_as_linux_does(
    platform: str, env: dict[str, str], found: tuple[str, ...], argv: tuple[str, ...] | None
) -> None:
    writer = pick_writer(env, platform=platform, which=tools(*found))
    assert (writer.argv if writer else None) == (
        None if argv is None else (f"/fake/bin/{argv[0]}", *argv[1:])
    )


@NEEDS_POSIX
@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_TTY", "CODESPACES"])
def test_over_ssh_the_native_clipboard_is_the_wrong_machine_s(
    variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, monkeypatch)
    assert clipboard.local_writer() is not None, "the premise: pbcopy is found locally"
    monkeypatch.setenv(variable, "10.0.0.2 52144 10.0.0.1 22")
    assert clipboard.local_writer() is None


def _proc_tree(root: Path, processes: dict[int, tuple[str, int]]) -> None:
    """A fake ``/proc``: each pid's ``stat``, ``pid (comm) state ppid …``."""
    for pid, (comm, ppid) in processes.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1\n")


def test_sudo_or_su_over_ssh_is_remote_by_the_sshd_above_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sudo``'s env_reset strips SSH's variables, but the login still hangs from an sshd
    (review of #266, round 1). /proc says every process's parent, whoever owns it."""
    proc = tmp_path / "proc"
    _proc_tree(
        proc,
        {
            100: ("python3", 90),
            90: ("sudo", 80),
            80: ("bash", 70),
            70: ("sshd-session", 60),
            60: ("sshd", 1),
            1: ("systemd", 0),
            200: ("python3", 190),
            190: ("zsh", 180),
            180: ("gnome-terminal-", 1),
            300: ("python3", 290),
            290: ("tmux: server (x)", 1),
        },
    )
    monkeypatch.setattr(clipboard, "_PROC", proc)
    assert clipboard._ancestors(100) == [
        "python3",
        "sudo",
        "bash",
        "sshd-session",
        "sshd",
        "systemd",
    ]
    assert clipboard._under_ssh_server(100)
    assert not clipboard._under_ssh_server(200), "a local terminal"
    assert clipboard._ancestors(300) == ["python3", "tmux: server (x)", "systemd"], "a ')'"
    monkeypatch.setattr(clipboard, "_under_ssh_server", lambda pid=None: True)
    assert clipboard.session_is_remote({})


def test_without_proc_one_ps_names_the_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS has no /proc: ``ps`` lists every process once, with the full path as comm."""
    monkeypatch.setattr(clipboard, "_PROC", tmp_path / "no-proc")
    monkeypatch.setattr(sys, "platform", "darwin")
    asked: list[list[str]] = []
    listing = (
        "  100    90 /usr/local/bin/python3\n"
        "   90    70 sudo\n"
        "   70    60 /usr/sbin/sshd-session\n"
        "   60     1 /usr/sbin/sshd\n"
        "  400   390 /Applications/Visual Studio Code.app/Contents/MacOS/Electron\n"
        "    1     0 /sbin/launchd\n"
    )

    def ask(argv: list[str]) -> str | None:
        asked.append(argv)
        return listing

    monkeypatch.setattr(clipboard, "_ask", ask)
    assert clipboard._ancestors(100) == ["python3", "sudo", "sshd-session", "sshd", "launchd"]
    assert clipboard._under_ssh_server(100)
    assert asked[0] == ["ps", "-A", "-o", "pid=,ppid=,comm="]
    assert not clipboard._under_ssh_server(400)


@pytest.mark.parametrize(
    ("said", "remote"),
    [
        ("SSH_CONNECTION=10.0.0.2 52144 10.0.0.1 22\n", True),
        ("-SSH_CONNECTION\n", False),  # last attached locally: tmux removed it
        (None, False),  # tmux gone, or never told
    ],
    ids=["attached-over-ssh", "attached-locally", "tmux-says-nothing"],
)
def test_inside_the_user_s_tmux_its_last_attach_decides(
    said: str | None, remote: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asq started in tmux at the desk, reattached over SSH, keeps the environment it
    started with: no SSH variables, DISPLAY still set (review of #266, round 1)."""
    asked: list[list[str]] = []

    def ask(argv: list[str]) -> str | None:
        asked.append(argv)
        return said

    monkeypatch.setattr(clipboard, "_ask", ask)
    monkeypatch.setattr(clipboard, "_under_ssh_server", lambda pid=None: True)  # not asked
    assert clipboard.session_is_remote({"TMUX": "/tmp/tmux-1/default,1,0"}) is remote
    assert asked == [["tmux", "show-environment", "SSH_CONNECTION"]]


@pytest.mark.parametrize(
    ("said", "remote"),
    [
        ("-SSH_CONNECTION\n", False),  # attached at the machine since
        ("SSH_CONNECTION=10.0.0.3 40000 10.0.0.1 22\n", True),
        (None, True),  # tmux says nothing: the process's own variables are all there is
    ],
    ids=["attached-locally-since", "attached-over-ssh", "tmux-says-nothing"],
)
def test_a_tmux_created_over_ssh_is_local_once_tmux_says_its_last_attach_was(
    said: str | None, remote: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 3: asq started in a tmux created over SSH keeps SSH's variables
    for good, and they decided before tmux was asked. Attached later at the Mac itself,
    every copy went to OSC 52 alone, which Terminal.app drops: #66 again."""
    monkeypatch.setattr(clipboard, "_ask", lambda argv: said)
    monkeypatch.setattr(clipboard, "_under_ssh_server", lambda pid=None: True)  # not asked
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "SSH_CONNECTION": "10.0.0.2 52144 10.0.0.1 22"}
    assert clipboard.session_is_remote(env) is remote
    assert clipboard.session_is_remote({**env, "SSH_TTY": "/dev/pts/3"}) is remote
    assert clipboard.session_is_remote({**env, "CODESPACES": "true"}), "a Codespace is remote"


def test_inside_the_user_s_tmux_the_writer_is_never_taken_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its client may reattach from another machine at any moment, and nothing in this
    process's environment changes when it does."""
    install(tmp_path, monkeypatch)
    told: list[bool] = []
    clipboard.submit(TEXT, told.append)
    deadline = time.monotonic() + 10
    while not told and time.monotonic() < deadline:
        time.sleep(0.01)
    assert isinstance(clipboard.cached_writer(), Writer), "the premise: it was cached"
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    assert clipboard.cached_writer() is clipboard.Ask.WORKER


def _told(count: int, told: list[bool]) -> None:
    deadline = time.monotonic() + 10
    while len(told) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(told) == count, "the clipboard thread never told the copy"


@NEEDS_POSIX
def test_inside_tmux_the_tool_is_found_once_and_only_its_last_attach_is_asked_each_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: inside tmux every copy ran the whole search again (on WSL a
    scan of the Windows PATH over 9p) when only the remote question can change on a
    reattach. The tool is kept for the environment; tmux is asked at every copy."""
    fake = install(tmp_path, monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    picked: list[str] = []
    real_pick = clipboard.pick_writer

    def picking(*args: Any, **kwargs: Any) -> Writer | None:
        picked.append("pick")
        return real_pick(*args, **kwargs)

    monkeypatch.setattr(clipboard, "pick_writer", picking)
    attached = [None, None, "SSH_CONNECTION=10.0.0.2 51000 10.0.0.1 22\n"]
    asked: list[list[str]] = []

    def ask(argv: list[str]) -> str | None:
        asked.append(argv)
        return attached[len(asked) - 1]

    monkeypatch.setattr(clipboard, "_ask", ask)
    told: list[bool] = []
    for count, text in enumerate(("one", "two", "three"), start=1):
        clipboard.submit(text, told.append)
        _told(count, told)

    assert told == [True, True, False], "reattached over SSH: the native clipboard is not theirs"
    assert fake.texts() == ["one", "two"]
    assert picked == ["pick"]
    assert asked == [["tmux", "show-environment", "SSH_CONNECTION"]] * 3


@NEEDS_POSIX
def test_a_test_s_copy_can_reach_no_tool_but_the_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``install`` kept the developer's DISPLAY and WAYLAND_DISPLAY, so on a Linux desktop
    a copy made with the real ``local_writer`` back ran their own ``wl-copy`` and
    overwrote their clipboard — what ``no_native_clipboard`` exists to prevent (review
    of #266, round 1)."""
    desktop = tmp_path / "desktop-bin"
    desktop.mkdir()
    for tool in ("wl-copy", "xclip", "xsel", "clip.exe"):
        (desktop / tool).write_text("#!/bin/sh\nexit 0\n")
        (desktop / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{desktop}{os.pathsep}{os.environ.get('PATH', '')}")
    for name, value in {
        "WAYLAND_DISPLAY": "wayland-0",
        "DISPLAY": ":0",
        "WSL_DISTRO_NAME": "U",
    }.items():
        monkeypatch.setenv(name, value)
    install(tmp_path, monkeypatch, platform="linux")  # the fake is pbcopy: nothing on Linux
    assert clipboard.local_writer() is None


@NEEDS_POSIX
def test_a_pinned_fake_needs_no_platform_patched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FleetApp test runs Remote's threads, which read ``sys.platform`` themselves: the
    copy test patched it to "darwin" for seconds around the press (review of #266)."""
    platform = sys.platform
    fake = install(tmp_path, monkeypatch, platform=None, pin=True)
    assert sys.platform == platform
    writer = clipboard.local_writer()
    assert writer is not None and clipboard.write(writer, TEXT)
    assert fake.texts() == [TEXT]
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 52144 10.0.0.1 22")
    assert clipboard.local_writer() is None, "remote stays remote"


# --- the write itself ------------------------------------------------------------------


@NEEDS_POSIX
def test_pbcopy_is_handed_the_exact_text_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(tmp_path, monkeypatch)
    writer = clipboard.local_writer()
    assert writer is not None and clipboard.write(writer, TEXT)
    assert fake.texts() == [TEXT]
    assert fake.calls()[0]["lc_all"] == "en_US.UTF-8"


@NEEDS_POSIX
def test_clip_exe_is_handed_utf16_that_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(tmp_path, monkeypatch, "clip.exe", platform="linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(clipboard, "_BINFMT", _binfmt(tmp_path, "WSLInterop"))
    writer = clipboard.local_writer()
    assert writer is not None and clipboard.write(writer, TEXT)
    assert fake.payloads() == [b"\xff\xfe" + TEXT.encode("utf-16-le")]
    assert fake.texts("utf-16") == [TEXT]


@NEEDS_POSIX
@pytest.mark.parametrize("behaviour", ["missing", "fail", "hang"])
def test_a_tool_that_is_missing_fails_or_hangs_is_false_and_never_raises(
    behaviour: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = install(tmp_path, monkeypatch, behaviour=behaviour)
    argv = (
        (str(tmp_path / "gone"),)
        if behaviour == "missing"
        else (str(fake.root / "bin" / "pbcopy"),)
    )
    started = time.monotonic()
    assert clipboard.write(Writer(argv), TEXT, timeout=0.5) is False
    assert time.monotonic() - started < 5, "a hung tool is stopped at the timeout"
    assert fake.calls() == []
    assert [record.levelname for record in caplog.records] == ["WARNING"], caplog.text


@NEEDS_POSIX
def test_a_tool_that_stops_reading_cannot_hold_the_write_past_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: on Windows ``Popen.communicate`` writes the whole of stdin
    before its timeout starts (CPython's win32 ``_communicate``), so a ``clip.exe`` that
    stopped reading a copy larger than the pipe held the one clipboard thread for good.
    Here ``communicate`` writes as it does there."""
    fake = install(tmp_path, monkeypatch, behaviour="stall")

    def win32_communicate(
        self: subprocess.Popen[bytes], input: bytes | None = None, timeout: float | None = None
    ) -> tuple[None, None]:
        if self.stdin is not None:
            self.stdin.write(input or b"")  # all of it, before any timeout logic
            self.stdin.close()
        self.wait(timeout)
        return None, None

    monkeypatch.setattr(subprocess.Popen, "communicate", win32_communicate)
    writer = Writer((str(fake.root / "bin" / "pbcopy"),))
    landed: list[bool] = []
    writing = threading.Thread(
        target=lambda: landed.append(clipboard.write(writer, "x" * 1_000_000, timeout=0.5)),
        daemon=True,
    )
    started = time.monotonic()
    writing.start()
    writing.join(10)

    assert landed == [False], "the write is still blocked on the tool's stdin"
    assert time.monotonic() - started < 5


def test_a_write_that_raises_anything_is_false_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``os.environ`` changed by another thread while it is copied raises RuntimeError,
    as does a spawn during interpreter shutdown; neither may escape (review of #266)."""

    def raising(*args: object, **kwargs: object) -> None:
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(subprocess, "run", raising)
    monkeypatch.setattr(subprocess, "Popen", raising)
    assert clipboard.write(Writer(("/fake/bin/pbcopy",), env=(("LC_ALL", "C"),)), TEXT) is False
    assert [record.levelname for record in caplog.records] == ["WARNING"], caplog.text


def test_the_clipboard_thread_outlives_a_write_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy whose write blew up is still told, and the copy queued behind it still runs
    on the same thread: dead, the thread took that copy's toast with it."""
    calls: list[str] = []
    queued = threading.Event()

    def write(writer: Writer, text: str, **kwargs: object) -> bool:
        calls.append(text)
        if text == "boom":
            queued.wait(5)
            raise RuntimeError("not caught by write itself")
        return True

    monkeypatch.setattr(clipboard, "write", write)
    monkeypatch.setattr(clipboard, "local_writer", lambda: Writer(("/fake/bin/pbcopy",)))
    told: list[tuple[str, bool]] = []
    threads: list[int] = []

    def done(label: str) -> Callable[[bool], None]:
        def tell(landed: bool) -> None:
            told.append((label, landed))
            threads.append(threading.get_ident())

        return tell

    clipboard.submit("boom", done("boom"))
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    clipboard.submit("after", done("after"))
    queued.set()
    deadline = time.monotonic() + 5
    while len(told) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert told == [("boom", False), ("after", True)]
    assert calls == ["boom", "after"] and len(set(threads)) == 1


def test_the_clipboard_thread_ends_when_nothing_is_left_to_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kept for good, an idle thread outlived every test that copied, and each ``forkpty``
    later in the run warned of forking a threaded process. The next copy starts another."""
    monkeypatch.setattr(clipboard, "write", lambda writer, text, **kwargs: True)
    monkeypatch.setattr(clipboard, "local_writer", lambda: Writer(("/fake/bin/pbcopy",)))

    def running() -> list[threading.Thread]:
        return [thread for thread in threading.enumerate() if thread.name == "clipboard"]

    for text in ("first", "second"):
        told: list[bool] = []
        clipboard.submit(text, told.append)
        assert clipboard.flush(5) and told == [True]
        deadline = time.monotonic() + 5
        while running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not running(), f"the thread is still there after {text!r}"


@NEEDS_POSIX
def test_two_quick_writes_leave_the_second_on_the_clipboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first write is the slower one: run side by side, it would land last."""
    fake = install(tmp_path, monkeypatch, behaviour="slow-first")
    told: list[bool] = []
    clipboard.submit("first", told.append)
    clipboard.submit("second", told.append)
    deadline = time.monotonic() + 10
    while len(told) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert told == [True, True]
    assert fake.clipboard == "second", fake.texts()


@NEEDS_POSIX
def test_copies_queued_behind_a_slow_write_are_written_once_as_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five copies behind a tool that takes the whole timeout took ten seconds each in
    turn, for a clipboard that holds only the last (review of #266, round 1). The ones
    the newest overtook are still told, in order, with its outcome."""
    fake = install(tmp_path, monkeypatch, behaviour="slow-first")
    told: list[tuple[str, bool]] = []

    def done(label: str) -> Callable[[bool], None]:
        return lambda landed: told.append((label, landed))

    clipboard.submit("first", done("first"))
    deadline = time.monotonic() + 10
    while fake.started() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    for label in ("second", "third", "fourth"):
        clipboard.submit(label, done(label))
    while len(told) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert told == [("first", True), ("second", True), ("third", True), ("fourth", True)]
    assert fake.texts() == ["first", "fourth"], "the overtaken copies were never run"


def test_a_tool_that_will_not_die_is_left_behind_after_a_bounded_wait(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``subprocess.run`` waited without a bound after its kill, so a tool stuck in
    uninterruptible sleep held up every later copy for good (review of #266, round 1)."""
    waits: list[float | None] = []

    class Unkillable:
        stdin = None
        returncode = None

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            raise subprocess.TimeoutExpired("pbcopy", timeout or 0)

    monkeypatch.setattr(subprocess, "Popen", Unkillable)
    started = time.monotonic()
    assert clipboard.write(Writer(("/fake/bin/pbcopy",)), TEXT, timeout=0.1) is False
    assert time.monotonic() - started < 1
    assert waits == [0.1, clipboard.KILL_WAIT], "the write's own wait, then the kill's"
    assert "left behind" in caplog.text


@NEEDS_POSIX
def test_the_writer_found_is_kept_for_the_environment_and_forgotten_when_it_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, monkeypatch)
    assert clipboard.cached_writer() is clipboard.Ask.WORKER, "nothing known before a copy"
    told: list[bool] = []
    clipboard.submit(TEXT, told.append)
    deadline = time.monotonic() + 10
    while not told and time.monotonic() < deadline:
        time.sleep(0.01)
    known = clipboard.cached_writer()
    assert isinstance(known, Writer) and known.argv[0].endswith("/pbcopy")
    display = os.environ.get("DISPLAY")
    monkeypatch.setenv("DISPLAY", f"{display or ''}:1")  # an environment it was not found for
    assert clipboard.cached_writer() is clipboard.Ask.WORKER
    if display is None:
        monkeypatch.delenv("DISPLAY")
    else:
        monkeypatch.setenv("DISPLAY", display)
    assert clipboard.cached_writer() == known


@NEEDS_POSIX
@pytest.mark.parametrize("tmux", [False, True])
def test_a_tool_that_failed_is_not_run_again_until_it_is_looked_for_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmux: bool
) -> None:
    """Review of #266, round 2: a failed write dropped the cache without a word of why, so
    the next copy found the same broken tool and ran it again. An ``xclip`` hanging on a
    stale DISPLAY held every toast for about three seconds, for as long as asq ran. A
    failure stands as "no tool" stands, for RECHECK: told at once, OSC 52's alone."""
    fake = install(tmp_path, monkeypatch, behaviour="fail")
    if tmux:
        monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
        monkeypatch.setattr(clipboard, "_ask", lambda argv: None)  # attached at the desk
    now = [1000.0]
    monkeypatch.setattr(clipboard, "_clock", lambda: now[0])
    told: list[bool] = []
    clipboard.submit(TEXT, told.append)
    _told(1, told)
    assert (told, fake.started()) == ([False], 1)

    assert clipboard.cached_writer() is None, "known: no tool to wait for"
    clipboard.submit(TEXT, told.append)
    _told(2, told)
    assert (told, fake.started()) == ([False, False], 1), "the broken tool ran again"

    now[0] += clipboard.RECHECK + 1
    fake.behaviour = ""  # mended meanwhile
    assert clipboard.cached_writer() is clipboard.Ask.WORKER
    clipboard.submit(TEXT, told.append)
    _told(3, told)
    assert (told[-1], fake.started()) == (True, 2)


def test_no_tool_found_is_looked_for_again_after_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool installed while the app runs is found without a restart."""
    install(tmp_path, monkeypatch, "not-pbcopy", only=True)
    now = [1000.0]
    monkeypatch.setattr(clipboard, "_clock", lambda: now[0])
    told: list[bool] = []
    clipboard.submit(TEXT, told.append)
    deadline = time.monotonic() + 10
    while not told and time.monotonic() < deadline:
        time.sleep(0.01)
    assert told == [False] and clipboard.cached_writer() is None
    now[0] += clipboard.RECHECK + 1
    assert clipboard.cached_writer() is clipboard.Ask.WORKER


@NEEDS_POSIX
def test_a_copy_made_at_quit_reaches_the_tool_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clipboard thread is a daemon. A process that copies and exits at once (drag,
    then q) killed it mid-write: a payload over the pipe's buffer reached the tool cut
    short, or not at all (review of #266, round 1). ``flush`` runs at exit."""
    fake = install(tmp_path, monkeypatch, behaviour="slow-read")
    big = (TEXT + "\n") * 12_000  # ~200 KB: more than any pipe buffer
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from aisquare.core import clipboard\n"
        "clipboard.local_writer = lambda: clipboard.Writer((sys.argv[1],))\n"
        "clipboard.submit(Path(sys.argv[2]).read_text(encoding='utf-8'), lambda landed: None)\n"
    )
    tool = str(fake.root / "bin" / "pbcopy")
    done = subprocess.run(
        [sys.executable, "-c", script, tool, str(tmp_path / "big.txt")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert fake.texts() == [big]


@NEEDS_POSIX
def test_flush_waits_for_the_queued_copies_and_no_longer_than_it_is_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(tmp_path, monkeypatch, behaviour="slow-read")
    assert clipboard.flush(0.1), "nothing queued: done at once"
    clipboard.submit(TEXT, lambda landed: None)
    assert clipboard.flush(10) and fake.texts() == [TEXT]
    fake.behaviour = "hang"
    monkeypatch.setattr(clipboard, "WRITE_TIMEOUT", 0.5)
    clipboard.submit("stuck", lambda landed: None)
    started = time.monotonic()
    assert clipboard.flush(0.1) is False
    assert time.monotonic() - started < 1
    assert clipboard.flush(10), "and it ends once the stuck tool is killed"


@NEEDS_POSIX
def test_flush_at_exit_waits_for_the_copy_queued_behind_a_slow_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: ``flush`` waited for one write, but the copy made at quit
    can be queued behind the write in progress, and inside tmux each batch asks tmux first.
    It gave up part-way, and the daemon thread died with the last copy unwritten. Here the
    write in progress, then tmux's answer and the quit copy's own write, take about four
    seconds: under the old three, inside the new bound of two whole batches."""
    fake = install(tmp_path, monkeypatch, behaviour="slow-write")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")

    def slow_ask(argv: list[str]) -> str | None:
        time.sleep(0.9)  # tmux, slow to answer, inside ASK_TIMEOUT
        return None

    monkeypatch.setattr(clipboard, "_ask", slow_ask)
    clipboard.submit("first", lambda landed: None)
    deadline = time.monotonic() + 10
    while fake.started() < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    clipboard.submit("second", lambda landed: None)  # the copy made just before quitting

    assert clipboard.flush() is True
    assert fake.texts() == ["first", "second"]


# --- what becomes of an OSC 52 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "terminal"),
    [
        ({"TERM_PROGRAM": "Apple_Terminal"}, "drops"),
        ({"TERM_PROGRAM": "iTerm.app"}, "asks"),
        ({"LC_TERMINAL": "iTerm2"}, "asks"),  # over SSH: macOS's ssh sends LC_*
        ({"TERM_PROGRAM": "WezTerm", "LC_TERMINAL": "iTerm2"}, "accepts"),
        ({"TERM_PROGRAM": "ghostty"}, "accepts"),
        ({"GHOSTTY_RESOURCES_DIR": "/x"}, "accepts"),
        ({"WEZTERM_EXECUTABLE": "/x"}, "accepts"),
        ({"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"}, "accepts"),
        # kitty started from a Terminal.app shell keeps its TERM_PROGRAM: kitty wins.
        ({"KITTY_WINDOW_ID": "1", "TERM_PROGRAM": "Apple_Terminal"}, "accepts"),
        ({"TERM": "xterm-kitty"}, "accepts"),  # kitty over SSH: ssh carries TERM
        ({"TERM": "foot"}, "accepts"),
        ({"TERM": "alacritty"}, "accepts"),
        ({"WT_SESSION": "c0ffee"}, "accepts"),
        # Over SSH from Terminal.app: ssh sends no TERM_PROGRAM, and TERM names nobody.
        ({"TERM": "xterm-256color"}, "unknown"),
        ({}, "unknown"),
        ({"TERM_PROGRAM": "vscode"}, "unknown"),
        ({"VTE_VERSION": "7800"}, "unknown"),
        # A multiplexer between keeps or passes OSC 52 by its own setting.
        ({"TERM_PROGRAM": "tmux", "TERM": "tmux-256color"}, "unknown"),
        # …and keeps the variables of whichever terminal started it: tmux started from
        # iTerm2 over SSH and reattached from kitty still says LC_TERMINAL=iTerm2
        # (review of #266, round 2).
        ({"TERM_PROGRAM": "tmux", "LC_TERMINAL": "iTerm2"}, "unknown"),
        ({"TMUX": "/tmp/tmux-1/default,1,0", "LC_TERMINAL": "iTerm2"}, "unknown"),
        ({"STY": "1.pts-0.host", "TERM_PROGRAM": "Apple_Terminal"}, "unknown"),
        ({"TMUX": "/tmp/tmux-1/default,1,0", "KITTY_WINDOW_ID": "1"}, "unknown"),
        ({"STY": "1.pts-0.host", "TERM": "screen-256color", "WT_SESSION": "c0ffee"}, "unknown"),
    ],
)
def test_osc52_is_dropped_taken_or_unknown_by_what_the_environment_names(
    env: dict[str, str], terminal: str
) -> None:
    assert osc52_terminal(env) == terminal


def test_one_table_names_the_terminal_for_doctor_and_for_the_clipboard() -> None:
    """Doctor's table and the clipboard's disagreed (review of #266, round 1): now both
    read ``core.terminal.SIGNS``. The toast's wording names Terminal.app for "drops" and
    iTerm2 for "asks", so each is the only terminal with that answer."""
    from aisquare.core.terminal import SIGNS, outer_terminal

    assert {sign.name for sign in SIGNS if sign.osc52 == "drops"} == {"Terminal.app"}
    assert {sign.name for sign in SIGNS if sign.osc52 == "asks"} == {"iTerm2"}
    stale = {"KITTY_WINDOW_ID": "1", "TERM_PROGRAM": "Apple_Terminal"}
    assert outer_terminal(stale) == ("kitty", True)
    assert osc52_terminal(stale) == "accepts"


def test_a_copy_only_osc52_carried_says_what_became_of_it() -> None:
    assert copy_toast("accepts", "copied 3 characters", "3 characters", 3.0) == Toast(
        "copied 3 characters", "information", 3.0
    )
    missed = copy_toast("drops", "copied 3 characters", "3 characters", 3.0)
    assert missed.message.startswith("the copy did not reach the clipboard")
    assert "Hold Fn while you drag" in missed.message and "Cmd+C" in missed.message
    assert (missed.severity, missed.timeout) == ("warning", MISSED_TIMEOUT)
    # iTerm2's own setting decides, and nothing outside it can see the setting: never a
    # flat "did not reach", which was wrong for every user who had turned it on.
    iterm = copy_toast("asks", "copied 3 characters", "3 characters", 3.0)
    assert iterm == Toast(
        "sent 3 characters to your terminal's clipboard (OSC 52) — if paste is empty, turn"
        " on iTerm2 > Settings > General > Selection > 'Applications in terminal may"
        " access clipboard', or hold ⌥ while you drag",
        "information",
        MISSED_TIMEOUT,
    )
    hedged = copy_toast("unknown", "copied 12 characters", "12 characters", 3.0)
    assert hedged == Toast(
        "sent 12 characters to your terminal's clipboard (OSC 52) — if paste is empty,"
        " your terminal ignores it (Terminal.app does)",
        "information",
        HEDGED_TIMEOUT,
    )
    assert not hedged_copy("the link").startswith("copied")


def test_an_app_that_is_not_a_clipboard_app_still_never_sends_an_empty_osc52() -> None:
    """``copy_and_tell``'s fallback went straight to Textual's write, which sends an OSC 52
    for "" — one that clears the terminal's clipboard (review of #266, round 1)."""

    class Plain(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.told: list[str] = []

        def notify(self, message: str, **kwargs: Any) -> None:
            self.told.append(message)

    async def drive() -> tuple[list[str], list[str], str]:
        app = Plain()
        writes: list[str] = []
        async with app.run_test() as pilot:
            assert app._driver is not None
            pilot.app._driver.write = writes.append  # type: ignore[method-assign,union-attr]
            copy_and_tell(app, "", "copied 0 characters")
            copy_and_tell(app, "abc", "copied 3 characters")
            await pilot.pause()
            return _osc52s(writes), app.told, app.clipboard

    osc52s, told, held = run(drive())
    assert osc52s == [osc52("abc")] and told == ["copied 3 characters"] and held == "abc"


# --- every entry point, through the app ------------------------------------------------


def _osc52s(writes: list[str]) -> list[str]:
    return [write for write in writes if "\x1b]52;" in write]


@NEEDS_POSIX
def test_a_drag_release_puts_the_exact_text_on_pbcopy_and_still_sends_osc52(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = install(tmp_path, monkeypatch)

    async def drive() -> tuple[str | None, list[str], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            assert host._driver is not None
            monkeypatch.setattr(host._driver, "write", writes.append)
            await drag(pilot, pane, (0, 0), (11, 0))
            await wait_until(pilot, lambda: bool(host.notices))
            return pane.selected_text(), list(host.notices), writes

    selected, notices, writes = run(drive())
    assert selected == TEXT, "the premise: the drag took the whole row"
    assert board.texts() == [TEXT]
    assert _osc52s(writes) == [osc52(TEXT)]
    assert notices == [
        f"copied {len(TEXT)} characters — ctrl+c copies again while the selection stands"
    ]


@NEEDS_POSIX
def test_ctrl_c_on_a_standing_highlight_puts_the_exact_text_on_pbcopy(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = install(tmp_path, monkeypatch)

    async def drive() -> tuple[list[str], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            assert host._driver is not None
            monkeypatch.setattr(host._driver, "write", writes.append)
            # Written as a gesture would leave it, with no copy of its own.
            host.screen.selections = {pane: Selection(Offset(0, 0), Offset(12, 0))}
            assert pane.selected_text() == TEXT, "the premise"
            pane.focus()
            await pilot.pause()
            await pilot.press("ctrl+c")
            await wait_until(pilot, lambda: bool(host.notices))
            return list(host.notices), writes

    notices, writes = run(drive())
    assert board.texts() == [TEXT]
    assert _osc52s(writes) == [osc52(TEXT)]
    assert notices == [f"copied {len(TEXT)} characters"]
    assert fake.sent() == [], "the copy key copied; nothing reached the agent"


@NEEDS_POSIX
def test_the_agent_s_own_selection_is_mirrored_onto_pbcopy(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code inside the fleet's tmux copies into tmux's paste buffer."""
    board = install(tmp_path, monkeypatch)
    pane = fake.panes["%1"]
    pane.alternate_on = pane.mouse_on = pane.mouse_sgr = True

    async def drive() -> tuple[list[str], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            assert host._driver is not None
            monkeypatch.setattr(host._driver, "write", writes.append)
            await pilot.mouse_down(widget, offset=(0, 0))
            fake.buffer = TEXT  # the program's release handler ran
            await pilot.mouse_up(widget, offset=(9, 0))
            await wait_until(pilot, lambda: bool(host.notices))
            return list(host.notices), writes

    notices, writes = run(drive())
    assert board.texts() == [TEXT]
    assert _osc52s(writes) == [osc52(TEXT)]
    assert notices == [f"copied {len(TEXT)} characters — the agent's own selection"]


@NEEDS_POSIX
def test_a_textual_input_copy_puts_the_exact_text_on_pbcopy(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Input``'s copy calls ``app.copy_to_clipboard`` itself, and says nothing."""
    board = install(tmp_path, monkeypatch)

    async def drive() -> tuple[list[str], list[str], str]:
        host = Host(fake.server(tmp_path), "%1", with_input=True)
        writes: list[str] = []
        async with host.run_test(size=(40, 8)) as pilot:
            await wait_until(pilot, lambda: synced(host.pane))
            assert host._driver is not None
            monkeypatch.setattr(host._driver, "write", writes.append)
            field = host.query_one("#other", Input)
            field.value = TEXT
            field.focus()
            field.select_all()
            await pilot.pause()
            await pilot.press("ctrl+c")
            await wait_until(pilot, lambda: bool(board.calls()))
            await pilot.pause(0.1)
            return list(host.notices), writes, host.clipboard

    notices, writes, held = run(drive())
    assert board.texts() == [TEXT]
    assert _osc52s(writes) == [osc52(TEXT)]
    assert held == TEXT, "App.clipboard, which Input's paste reads, is still set"
    assert notices == []


@NEEDS_POSIX
def test_no_path_scan_and_no_process_ever_runs_on_the_event_loop(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding the tool scans PATH, reads /proc and, on WSL, stats every Windows PATH
    directory over 9p: several to tens of milliseconds inside a mouse handler, at every
    copy (review of #266, round 1). The clipboard thread finds it and writes with it;
    the loop reads only what was found."""
    board = install(tmp_path, monkeypatch)
    threads: list[tuple[str, int]] = []
    real_which, real_popen = shutil.which, subprocess.Popen
    real_local_writer = clipboard.local_writer

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        if name in TOOLS:  # the harness asks for tmux on the loop, which is not ours
            threads.append(("which", threading.get_ident()))
        return real_which(name, *args, **kwargs)

    def local_writer() -> Writer | None:
        threads.append(("local_writer", threading.get_ident()))
        return real_local_writer()

    def popen(argv: Any, *args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        threads.append(("Popen", threading.get_ident()))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(clipboard, "local_writer", local_writer)
    monkeypatch.setattr(subprocess, "Popen", popen)

    async def drive() -> int:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            loop = threading.get_ident()
            await wait_until(pilot, lambda: synced(host.pane))
            await drag(pilot, host.pane, (0, 0), (11, 0))
            await wait_until(pilot, lambda: len(host.notices) == 1)
            await drag(pilot, host.pane, (0, 1), (5, 1))  # the second: the writer is known
            await wait_until(pilot, lambda: len(host.notices) == 2)
            return loop

    loop = run(drive())
    assert board.texts() == [TEXT, "second"]
    assert {kind for kind, _ in threads} == {"which", "local_writer", "Popen"}, threads
    assert all(thread != loop for _, thread in threads), threads


@NEEDS_POSIX
def test_an_empty_copy_writes_neither_the_native_clipboard_nor_osc52(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = install(tmp_path, monkeypatch)

    async def drive() -> tuple[list[str], list[str]]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        writes: list[str] = []
        async with host.run_test(size=(40, 8)) as pilot:
            await wait_until(pilot, lambda: synced(host.pane))
            assert host._driver is not None
            monkeypatch.setattr(host._driver, "write", writes.append)
            host.copy_to_clipboard("", toast="copied 0 characters")
            # Textual's own copy of a selection that extracts as nothing.
            header = host.query_one("#other", Static)
            host.screen.selections = {header: Selection(Offset(1, 0), Offset(1, 0))}
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause(0.3)
            return list(host.notices), writes

    notices, writes = run(drive())
    assert board.calls() == []
    assert _osc52s(writes) == []
    assert notices == []


@NEEDS_POSIX
def test_two_quick_copies_leave_the_second_on_the_clipboard_with_a_toast_each(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = install(tmp_path, monkeypatch, behaviour="slow-first")

    async def drive() -> list[str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            await wait_until(pilot, lambda: synced(host.pane))
            host.copy_to_clipboard("first", toast="one")
            host.copy_to_clipboard("second", toast="two")
            await wait_until(pilot, lambda: len(host.notices) == 2, timeout=10)
            return list(host.notices)

    assert run(drive()) == ["one", "two"]
    assert board.clipboard == "second", board.texts()


# --- the toast tells what became of the copy -------------------------------------------


async def _drag_and_wait(
    host: Host, pilot: Pilot[None], writes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await wait_until(pilot, lambda: synced(host.pane))
    assert host._driver is not None
    monkeypatch.setattr(host._driver, "write", writes.append)
    await drag(pilot, host.pane, (0, 0), (11, 0))


COPIED = f"copied {len(TEXT)} characters — ctrl+c copies again while the selection stands"
HEDGED = hedged_copy(f"{len(TEXT)} characters")
SSH = {"SSH_CONNECTION": "10.0.0.2 52144 10.0.0.1 22"}


def _environ(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@NEEDS_POSIX
@pytest.mark.parametrize(
    ("env", "told", "severity"),
    [
        ({"TERM_PROGRAM": "Apple_Terminal"}, TERMINAL_APP_MISSED, "warning"),
        # iTerm2 over SSH, whose setting may be on: sent, and what to do if paste is empty.
        ({"LC_TERMINAL": "iTerm2"}, iterm_hedge(f"{len(TEXT)} characters"), "information"),
        ({"TERM": "xterm-kitty"}, COPIED, "information"),
    ],
    ids=["terminal-app", "iterm2", "kitty"],
)
def test_over_ssh_only_osc52_is_sent_and_a_named_terminal_s_toast_says_if_it_lands(
    env: dict[str, str],
    told: str,
    severity: str,
    fake: FakeTmux,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = install(tmp_path, monkeypatch)
    _environ(monkeypatch, SSH | env)

    async def drive() -> tuple[list[str], list[str], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            await _drag_and_wait(host, pilot, writes, monkeypatch)
            await wait_until(pilot, lambda: bool(host.notices))
            await pilot.pause(0.2)
            return list(host.notices), list(host.severities), writes

    notices, severities, writes = run(drive())
    assert board.calls() == [], "no native write over SSH"
    assert _osc52s(writes) == [osc52(TEXT)]
    assert notices == [told] and severities == [severity]


@NEEDS_POSIX
@pytest.mark.parametrize(
    "env",
    [
        {"TERM": "xterm-256color"},
        {},
        {"TERM_PROGRAM": "tmux", "TMUX": "/tmp/tmux-1/default,1,0", "TERM": "tmux-256color"},
    ],
    ids=["terminal-app-over-ssh", "nothing-named", "the-host-s-own-tmux"],
)
def test_over_ssh_to_an_unknown_terminal_the_toast_says_sent_not_copied(
    env: dict[str, str], fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal.app over SSH is the case: ssh sends no TERM_PROGRAM, so nothing says the
    OSC 52 went to a terminal that drops it, and "copied" was a guess. The toast says it
    was sent through the terminal, and what an empty paste would mean."""
    board = install(tmp_path, monkeypatch)
    _environ(monkeypatch, SSH | env)

    async def drive() -> tuple[list[str], list[str], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            await _drag_and_wait(host, pilot, writes, monkeypatch)
            await wait_until(pilot, lambda: bool(host.notices))
            await pilot.pause(0.2)
            return list(host.notices), list(host.severities), writes

    notices, severities, writes = run(drive())
    assert board.calls() == [], "no native write over SSH"
    assert _osc52s(writes) == [osc52(TEXT)]
    assert notices == [HEDGED] and severities == ["information"]
    assert not any(notice.startswith("copied") for notice in notices)


@NEEDS_POSIX
def test_the_hedge_names_what_was_sent_the_agent_s_selection_or_the_link(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, monkeypatch)
    _environ(monkeypatch, SSH)
    pane = fake.panes["%1"]
    pane.alternate_on = pane.mouse_on = pane.mouse_sgr = True

    async def drive() -> list[str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            await pilot.mouse_down(widget, offset=(0, 0))
            fake.buffer = TEXT  # the program's release handler ran
            await pilot.mouse_up(widget, offset=(9, 0))
            await wait_until(pilot, lambda: bool(host.notices))
            host.copy_to_clipboard("https://x.example/r/t/", toast="Link copied", sent="the link")
            await wait_until(pilot, lambda: len(host.notices) == 2)
            return list(host.notices)

    assert run(drive()) == [
        hedged_copy(f"the agent's own selection ({len(TEXT)} characters)"),
        hedged_copy("the link"),
    ]


@NEEDS_POSIX
@pytest.mark.parametrize("behaviour", ["missing", "fail", "hang"])
@pytest.mark.parametrize(
    ("terminal", "told", "severity"),
    [
        ("Apple_Terminal", TERMINAL_APP_MISSED, "warning"),
        ("", HEDGED, "information"),
        ("WezTerm", COPIED, "information"),
    ],
    ids=["terminal-app", "unknown", "wezterm"],
)
def test_a_native_write_that_misses_keeps_the_ui_running_and_the_toast_honest(
    behaviour: str,
    terminal: str,
    told: str,
    severity: str,
    fake: FakeTmux,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the native write gone, OSC 52 is all there was: "copied" on a terminal known
    to take it, the way round on Terminal.app, and the hedge where nobody can say."""
    if behaviour == "missing":
        board = install(tmp_path, monkeypatch, "not-pbcopy", only=True)
    else:
        board = install(tmp_path, monkeypatch, behaviour=behaviour)
    monkeypatch.setattr(clipboard, "WRITE_TIMEOUT", 0.5)
    if terminal:
        monkeypatch.setenv("TERM_PROGRAM", terminal)
    ticks: list[float] = []

    async def drive() -> tuple[list[str], list[str], list[str], int]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            await _drag_and_wait(host, pilot, writes, monkeypatch)
            host.set_interval(0.05, lambda: ticks.append(time.monotonic()))
            await pilot.pause(0.3)
            during = len(ticks)
            await wait_until(pilot, lambda: bool(host.notices), timeout=5)
            await pilot.pause(0.2)
            return list(host.notices), list(host.severities), writes, during

    notices, severities, writes, during = run(drive())
    assert board.calls() == []
    assert _osc52s(writes) == [osc52(TEXT)]
    assert during >= 3, "the event loop kept running while the tool was written to"
    assert notices == [told] and severities == [severity]


@NEEDS_POSIX
def test_a_copy_pbcopy_took_says_copied_even_on_terminal_app(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = install(tmp_path, monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")

    async def drive() -> list[str]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            await _drag_and_wait(host, pilot, writes, monkeypatch)
            await wait_until(pilot, lambda: bool(host.notices))
            await pilot.pause(0.2)
            return list(host.notices)

    assert run(drive()) == [COPIED]
    assert board.texts() == [TEXT]
