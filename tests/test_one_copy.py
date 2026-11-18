"""ONE copy (R13, #146): every copy in the app is ``ClipboardApp.copy_text``, and it says
what became of it: copied, only sent, or failed and why.

What it adds to the copy 0.8.2 made true (tests/test_clipboard.py holds that, unchanged):

- a tool that fails is not the end of the copy: the platform's next one is tried, at most
  two a copy, and a tool that failed is passed over for a while (Windows PowerShell after
  ``clip.exe``, ``xsel`` after ``xclip``);
- inside the user's own tmux, tmux itself is a route (``tmux load-buffer -w``), the one
  that reaches the terminal over SSH, and the OSC 52 goes again in tmux's passthrough;
  GNU screen gets it in screen's;
- an OSC 52 past what a terminal takes is not sent, and the copy says so;
- the outcome is a value (``CopyResult``), so the receiver's ``copy_row`` no longer claims
  a copy, and every toast tells the truth.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import ComposeResult
from textual.widgets import Static

from aisquare.cli.ui.clipboard import ClipboardApp
from aisquare.core import clipboard
from aisquare.core.clipboard import (
    OSC52_LIMIT,
    POWERSHELL_SET,
    CopyResult,
    Written,
    osc52_writes,
    pick_writers,
    verdict,
)
from tests.fake_clipboard import NEEDS_POSIX, TEXT, install

REAL_FALLBACK_WRITERS = clipboard.fallback_writers
REAL_RELAY_WRITER = clipboard.relay_writer
SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"


def tools(*names: str) -> Callable[[str], str | None]:
    return lambda name: f"/fake/bin/{name}" if name in names else None


def _done(count: int, told: list[Written]) -> None:
    deadline = time.monotonic() + 10
    while len(told) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(told) == count, "the clipboard thread never told the copy"


# --- the tools, in order ---------------------------------------------------------------------


def test_windows_tries_powershell_after_clip_exe() -> None:
    found = pick_writers({}, platform="win32", which=tools("clip.exe", "powershell.exe"))
    alone = pick_writers({}, platform="win32", which=tools("powershell.exe"))

    assert [writer.name for writer in found] == ["clip.exe", "powershell.exe"]
    assert found[1].argv[1:] == ("-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SET)
    assert found[1].encoding == "utf-8" and not found[1].bom, "PowerShell reads stdin as UTF-8"
    assert [writer.name for writer in alone] == ["powershell.exe"]


def test_wsl_tries_clip_exe_then_powershell_then_the_linux_tools() -> None:
    env = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
    found = pick_writers(
        env,
        platform="linux",
        which=tools("clip.exe", "powershell.exe", "wl-copy", "xclip", "xsel"),
        wsl=True,
        interop=True,
    )
    off = pick_writers(
        env, platform="linux", which=tools("clip.exe", "wl-copy"), wsl=True, interop=False
    )

    assert [w.name for w in found] == ["clip.exe", "powershell.exe", "wl-copy", "xclip", "xsel"]
    assert [w.name for w in off] == ["wl-copy"], "interop off: Windows' tools cannot run"


def test_the_first_tool_is_the_one_0_8_2_picked() -> None:
    """``pick_writer`` is the first of :func:`pick_writers`, as before (test_clipboard pins it)."""
    for platform, names in (("darwin", ("pbcopy",)), ("win32", ("clip.exe", "powershell.exe"))):
        first = clipboard.pick_writer({}, platform=platform, which=tools(*names))
        assert first is not None and first.name == names[0]


@NEEDS_POSIX
def test_a_tool_that_fails_hands_the_copy_to_the_next_and_is_passed_over_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xclip = install(tmp_path, monkeypatch, "xclip", platform="linux", behaviour="fail")
    xsel = install(tmp_path, monkeypatch, "xsel", platform="linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clipboard, "fallback_writers", REAL_FALLBACK_WRITERS)
    told: list[Written] = []

    clipboard.copy_later(TEXT, told.append)
    _done(1, told)
    clipboard.copy_later("second", told.append)
    _done(2, told)

    assert told[0] == Written(True, "xsel", False, ("xclip", "xsel")), "xclip failed, xsel took it"
    assert told[1] == Written(True, "xsel", False, ("xsel",)), "the broken xclip was passed over"
    assert xclip.started() == 1 and xsel.texts() == [TEXT, "second"]


@NEEDS_POSIX
def test_a_copy_tries_two_tools_at_most(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two writes' worth of waiting at most: a third tool waits for the next copy."""
    first = install(tmp_path, monkeypatch, "wl-copy", platform="linux", behaviour="fail")
    second = install(tmp_path, monkeypatch, "xclip", platform="linux", behaviour="fail")
    third = install(tmp_path, monkeypatch, "xsel", platform="linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clipboard, "fallback_writers", REAL_FALLBACK_WRITERS)
    told: list[Written] = []

    clipboard.copy_later(TEXT, told.append)
    _done(1, told)
    clipboard.copy_later(TEXT, told.append)
    _done(2, told)

    assert told[0] == Written(False, tried=("wl-copy", "xclip"))
    assert told[1].landed and told[1].tool == "xsel", "the next copy started at the next tool"
    assert (first.started(), second.started(), third.started()) == (1, 1, 1)
    assert clipboard.MAX_TRIES == 2


@NEEDS_POSIX
def test_inside_tmux_over_ssh_tmux_load_buffer_carries_the_copy_and_it_is_only_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap 0.8.2 left: asq in the user's tmux over SSH. No native tool runs (the session
    is remote), and tmux keeps an app's own OSC 52 to itself by default; ``load-buffer -w``
    hands it to tmux, which sends it to the terminal it is attached from."""
    pbcopy = install(tmp_path, monkeypatch)
    tmux = install(tmp_path, monkeypatch, "tmux")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setattr(
        clipboard, "_ask", lambda argv: "SSH_CONNECTION=10.0.0.2 52144 10.0.0.1 22\n"
    )
    monkeypatch.setattr(clipboard, "relay_writer", REAL_RELAY_WRITER)
    told: list[Written] = []

    clipboard.copy_later(TEXT, told.append)
    _done(1, told)

    assert told == [Written(True, "tmux", True, ("tmux",))]
    assert pbcopy.calls() == [], "remote: the Mac's own clipboard is not the user's"
    assert tmux.calls()[0]["argv"] == ["load-buffer", "-w", "-"] and tmux.texts() == [TEXT]
    said = verdict(told[0], osc52=True, fate="unknown")
    assert (said.verdict, said.route, said.tool) == ("sent", "tmux", "tmux")


def test_outside_tmux_there_is_no_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    assert REAL_RELAY_WRITER() is None


# --- OSC 52 ----------------------------------------------------------------------------------


def _osc52(text: str) -> str:
    return f"\x1b]52;c;{base64.b64encode(text.encode()).decode()}\a"


def test_osc52_goes_alone_outside_a_multiplexer_and_wrapped_again_inside_tmux_or_screen() -> None:
    raw = _osc52("héllo")
    long = "x" * 200

    assert osc52_writes("héllo", {}) == (raw,)
    assert osc52_writes("héllo", {"TMUX": "/tmp/t,1,0"}) == (
        raw,
        "\x1bPtmux;" + raw.replace("\x1b", "\x1b\x1b") + "\x1b\\",
    )
    (screen,) = osc52_writes(long, {"STY": "123.pts-0.host"})
    pieces = re.findall(r"\x1bP(.*?)\x1b\\", screen, re.DOTALL)
    assert "".join(pieces) == _osc52(long), "screen's passthrough carries the sequence whole"
    assert all(len(piece) <= 76 for piece in pieces), "in pieces screen takes"


def test_no_osc52_for_nothing_or_past_what_a_terminal_takes() -> None:
    fits = "a" * (OSC52_LIMIT // 4 * 3)
    assert osc52_writes("", {}) == (), "an empty OSC 52 clears the clipboard"
    assert osc52_writes(fits, {}) != ()
    assert osc52_writes(fits + "aaa", {}) == ()
    assert clipboard.osc52_fits(fits) and not clipboard.osc52_fits(fits + "aaa")


# --- the verdict -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "osc52", "fate", "too_large", "said"),
    [
        (Written(True, "pbcopy"), True, "drops", False, ("copied", "native", None)),
        (Written(True, "tmux", True), True, "unknown", False, ("sent", "tmux", None)),
        (Written(False), True, "accepts", False, ("copied", "osc52", None)),
        (Written(False), True, "asks", False, ("sent", "osc52", None)),
        (Written(False), True, "unknown", False, ("sent", "osc52", None)),
        (Written(False), True, "drops", False, ("failed", "osc52", "the terminal ignores OSC 52")),
        (Written(False), False, "accepts", True, ("failed", None, clipboard.TOO_LARGE)),
        (Written(False), False, "accepts", False, ("failed", None, clipboard.NO_ROUTE)),
    ],
)
def test_what_a_copy_says_of_itself(
    written: Written,
    osc52: bool,
    fate: clipboard.Osc52Fate,
    too_large: bool,
    said: tuple[str, str | None, str | None],
) -> None:
    result = verdict(written, osc52=osc52, fate=fate, too_large=too_large)
    assert (result.verdict, result.route, result.reason) == said
    assert result.ok == (said[0] == "copied")


# --- through the app ------------------------------------------------------------------------


class Host(ClipboardApp[None]):
    def compose(self) -> ComposeResult:
        yield Static("x")


def _copy(text: str, monkeypatch: pytest.MonkeyPatch) -> tuple[CopyResult, list[str]]:
    async def run() -> tuple[CopyResult, list[str]]:
        app = Host()
        told: list[CopyResult] = []
        writes: list[str] = []
        async with app.run_test(size=(60, 10)) as pilot:
            assert app._driver is not None
            monkeypatch.setattr(app._driver, "write", writes.append)
            app.copy_text(text, cue=told.append)
            await pilot.pause()
        return told[0], writes

    return asyncio.run(run())


def test_copy_text_tells_its_caller_what_became_of_the_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied, writes = _copy("abc", monkeypatch)
    too_large, big_writes = _copy("x" * (OSC52_LIMIT + 1), monkeypatch)
    no_terminal: list[CopyResult] = []
    Host().copy_text("abc", cue=no_terminal.append)  # an app with no terminal to write to

    assert copied == CopyResult("copied", "osc52", fate="accepts") and writes == [_osc52("abc")]
    assert too_large.reason == clipboard.TOO_LARGE and big_writes == [], "never a clipped OSC 52"
    assert [(r.verdict, r.reason) for r in no_terminal] == [("failed", clipboard.NO_ROUTE)]


def test_a_copy_that_did_not_land_says_so_in_its_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    app = Host()  # no terminal to send OSC 52 to, and no native tool (the suite's stand-in)
    said: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "notify", lambda message, **kw: said.append((message, kw["severity"])))

    app.copy_to_clipboard("the link", toast="copied the link")

    assert said == [(f"could not copy 8 characters: {clipboard.NO_ROUTE}", "warning")]


# --- one function ----------------------------------------------------------------------------


def test_no_other_module_writes_the_clipboard_itself() -> None:
    """Every copy goes through ``ClipboardApp.copy_text`` and ``core.clipboard``: nothing else
    in the app builds an OSC 52 or names a clipboard tool."""
    owners = {SRC / "core" / "clipboard.py", SRC / "cli" / "ui" / "clipboard.py"}
    tool = re.compile(r"\b(pbcopy|xclip|xsel|wl-copy|clip\.exe|Set-Clipboard)\b|\]52;")
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in owners:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and tool.search(node.value)
            ):
                found.append(
                    f"{path.relative_to(SRC).as_posix()}:{node.lineno}: {node.value[:60]!r}"
                )
    assert found == []


def test_the_receiver_never_claims_a_copy_it_cannot_see_land() -> None:
    source = (SRC / "cli" / "ui" / "receiver.py").read_text(encoding="utf-8")
    assert 'return f"copied ' not in source
    assert "rlx's toast says whether it reached the clipboard" in source
