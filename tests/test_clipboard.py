"""The platform's own clipboard tool, picked for this session and run beside OSC 52.

Hotfix 0.8.2: on macOS every copy said "copied N characters" and left the clipboard
unchanged, because Textual writes only an OSC 52 escape, which Terminal.app never
implemented and iTerm2 drops unless a setting is on. ``core.clipboard`` picks the
platform's own tool and runs it off the event loop, and says what becomes of an OSC 52
where it was the only route.

The writer tables are asked with a fake ``which``. The writes run a real fake tool on
PATH (tests/fake_clipboard.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.core import clipboard
from aisquare.core.clipboard import Writer, osc52_terminal, pick_writer
from tests.fake_clipboard import NEEDS_POSIX, TEXT, install


def tools(*names: str) -> Callable[[str], str | None]:
    """A ``which`` that finds exactly ``names``, under /fake/bin."""
    return lambda name: f"/fake/bin/{name}" if name in names else None


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


def test_wsl_writes_with_clip_exe_in_utf16_after_a_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clip.exe`` reads stdin in the console's codepage unless it is told UTF-16."""
    monkeypatch.setattr(clipboard, "_WINDOWS_CLIP", tmp_path / "no-clip.exe")
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


def test_wsl_is_known_by_its_variables_or_by_the_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = tmp_path / "version"
    monkeypatch.setattr(clipboard, "_PROC_VERSION", version)
    assert clipboard.in_wsl({"WSL_DISTRO_NAME": "Ubuntu"})
    assert clipboard.in_wsl({"WSL_INTEROP": "/run/WSL/1_interop"})
    assert not clipboard.in_wsl({}), "no /proc/version to read"
    version.write_text("Linux version 6.6.87.2-microsoft-standard-WSL2 (gcc 11.2.0)\n")
    assert clipboard.in_wsl({})
    with_x = pick_writer({"DISPLAY": ":0"}, platform="linux", which=tools("clip.exe", "xclip"))
    assert with_x is not None and with_x.argv == ("/fake/bin/clip.exe",)
    version.write_text("Linux version 6.11.4-301.fc41.x86_64 (mockbuild@fedoraproject.org)\n")
    assert not clipboard.in_wsl({})


@pytest.mark.parametrize("platform", ["win32", "freebsd14", "cygwin"])
def test_elsewhere_there_is_no_native_writer(platform: str) -> None:
    every = tools("pbcopy", "clip.exe", "wl-copy", "xclip", "xsel")
    assert pick_writer({"DISPLAY": ":0"}, platform=platform, which=every) is None


@NEEDS_POSIX
@pytest.mark.parametrize("variable", ["SSH_CONNECTION", "SSH_TTY"])
def test_over_ssh_the_native_clipboard_is_the_wrong_machine_s(
    variable: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(tmp_path, monkeypatch)
    assert clipboard.local_writer() is not None, "the premise: pbcopy is found locally"
    monkeypatch.setenv(variable, "10.0.0.2 52144 10.0.0.1 22")
    assert clipboard.local_writer() is None


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
def test_two_quick_writes_leave_the_second_on_the_clipboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first write is the slower one: run side by side, it would land last."""
    fake = install(tmp_path, monkeypatch, behaviour="slow-first")
    writer = clipboard.local_writer()
    assert writer is not None
    told: list[bool] = []
    clipboard.submit(writer, "first", told.append)
    clipboard.submit(writer, "second", told.append)
    deadline = time.monotonic() + 10
    while len(told) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert told == [True, True]
    assert fake.texts() == ["first", "second"] and fake.clipboard == "second"


# --- what becomes of an OSC 52 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "terminal"),
    [
        ({"TERM_PROGRAM": "Apple_Terminal"}, "Terminal.app"),
        ({"TERM_PROGRAM": "iTerm.app"}, "iTerm2"),
        ({"LC_TERMINAL": "iTerm2"}, "iTerm2"),  # over SSH: macOS's ssh sends LC_*
        ({"TERM_PROGRAM": "tmux", "LC_TERMINAL": "iTerm2"}, "iTerm2"),
        ({"TERM_PROGRAM": "WezTerm", "LC_TERMINAL": "iTerm2"}, "accepts"),
        ({"TERM_PROGRAM": "ghostty"}, "accepts"),
        ({"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"}, "accepts"),
        ({"TERM": "xterm-kitty"}, "accepts"),  # kitty over SSH: ssh carries TERM
        ({"TERM": "foot"}, "accepts"),
        ({"WT_SESSION": "c0ffee"}, "accepts"),
        # Over SSH from Terminal.app: ssh sends no TERM_PROGRAM, and TERM names nobody.
        ({"TERM": "xterm-256color"}, "unknown"),
        ({}, "unknown"),
        ({"TERM_PROGRAM": "vscode"}, "unknown"),
        # A multiplexer between keeps or passes OSC 52 by its own setting.
        ({"TERM_PROGRAM": "tmux", "TERM": "tmux-256color"}, "unknown"),
        ({"TMUX": "/tmp/tmux-1/default,1,0", "KITTY_WINDOW_ID": "1"}, "unknown"),
        ({"STY": "1.pts-0.host", "TERM": "screen-256color", "WT_SESSION": "c0ffee"}, "unknown"),
    ],
)
def test_osc52_is_dropped_taken_or_unknown_by_what_the_environment_names(
    env: dict[str, str], terminal: str
) -> None:
    assert osc52_terminal(env) == terminal
