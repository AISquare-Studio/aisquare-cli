"""A terminal window a test can type into, read and photograph — like a person does.

``TerminalSession`` = one ttyd PTY (the user's login shell) + one Chromium page running
the instrumented xterm.js client. Input goes through Chromium's real keyboard and
mouse into the emulator, which encodes it exactly as a user's terminal would
(F12 → ``CSI 24~``, Shift+Tab → ``CSI Z``, clicks → SGR mouse reports). Output is
read back from the emulator's own screen buffer — the text a user sees — and
photographed as real pixels.

A shot is taken only when the screen has SETTLED: no synchronized-output frame open
(DEC 2026, which Textual uses once xterm.js 6 reports it), no write for 250 ms and an
unchanged screen for 600 ms (asq's idle poll is 500 ms). The text is read again after
the PNG; if it moved in between, the shot is retaken, so a PNG always shows exactly
the text its step asserted on.
"""

from __future__ import annotations

import hashlib
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import Browser, BrowserContext, Page

from e2e.runner.machine import Machine
from e2e.runner.server import StaticServer

TRACEBACK = re.compile(r'Traceback \(most recent call last\)|File "[^"]*", line \d+, in |❱')
"""The demo render's own crash detector (tests/demo_tape.py), applied to every screen."""


class StepTimeout(AssertionError):
    """What a user was waiting for never showed up; carries the screen they saw instead."""

    def __init__(self, what: str, screen: Screen, timeout: float) -> None:
        self.screen = screen
        tail = "\n".join(line for line in screen.lines if line.strip())[-1500:]
        super().__init__(f"waited {timeout:.0f}s for {what}; the screen showed:\n{tail}")


@dataclass(frozen=True)
class Screen:
    cols: int
    rows: int
    lines: list[str]
    alt: bool
    frames: int
    bsu: bool
    since_write_ms: float
    prompts: int
    exit_codes: list[int | None]
    ws: str

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def digest(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()

    def find(
        self, pattern: str | re.Pattern[str], *, cols: tuple[int, int] | None = None
    ) -> tuple[int, int] | None:
        """(col, row) of the first match, optionally inside a column band (e.g. the sidebar)."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        for row, line in enumerate(self.lines):
            lo, hi = cols if cols else (0, len(line))
            match = rx.search(line[lo:hi])
            if match:
                return lo + match.start(), row
        return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


class TerminalSession:
    def __init__(
        self,
        browser: Browser,
        machine: Machine,
        server: StaticServer,
        *,
        cwd: Path,
        title: str,
        cols: int = 160,
        rows: int = 48,
        video_dir: Path | None = None,
        log_dir: Path,
    ) -> None:
        self.machine = machine
        self.cols, self.rows = cols, rows
        port = _free_port()
        log_dir.mkdir(parents=True, exist_ok=True)
        self._ttyd_log = (log_dir / "ttyd.log").open("ab")
        # -W writable, -o one client then exit (closing the window ends the shell, as for a user),
        # -i loopback only; the command is the user's login shell from a clean environment.
        self._ttyd = subprocess.Popen(
            [
                "ttyd",
                "-i",
                "127.0.0.1",
                "-p",
                str(port),
                "-W",
                "-o",
                "-T",
                "xterm-256color",
                "-w",
                str(cwd),
                *machine.shell_argv(),
            ],
            stdout=self._ttyd_log,
            stderr=subprocess.STDOUT,
        )
        self._wait_port(port)
        # Big enough for the window at 160x48 with chrome; the shot crops to the window.
        self.context: BrowserContext = browser.new_context(
            viewport={"width": 1440, "height": 940},
            device_scale_factor=1,
            record_video_dir=str(video_dir) if video_dir else None,
            record_video_size={"width": 1440, "height": 940} if video_dir else None,
        )
        self.page: Page = self.context.new_page()
        query = urlencode(
            {"ws": f"ws://127.0.0.1:{port}/ws", "cols": cols, "rows": rows, "title": title}
        )
        self.page.goto(server.url("/client/index.html?" + query))
        self.page.wait_for_function("window.e2e && window.e2e.ready === true", timeout=20_000)
        self.wait_prompt(1, timeout=30)

    @staticmethod
    def _wait_port(port: int, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(f"ttyd did not listen on {port}")

    # --- reading -----------------------------------------------------------------

    def screen(self) -> Screen:
        raw: dict[str, Any] = self.page.evaluate("window.e2eScreen()")
        return Screen(
            cols=raw["cols"],
            rows=raw["rows"],
            lines=list(raw["lines"]),
            alt=bool(raw["alt"]),
            frames=int(raw["frames"]),
            bsu=bool(raw["bsu"]),
            since_write_ms=float(raw["sinceWrite"]),
            prompts=int(raw["prompts"]),
            exit_codes=list(raw["exitCodes"]),
            ws=str(raw["ws"]),
        )

    def focused(self) -> list[str]:
        """Text runs drawn in reverse video — where Textual shows keyboard focus on a button."""
        runs: list[str] = []
        for y in range(self.rows):
            cells: list[dict[str, Any]] = self.page.evaluate(f"window.e2eRow({y})")
            current = ""
            for cell in cells:
                if cell["inv"]:
                    current += cell["ch"]
                elif current:
                    runs.append(current.strip())
                    current = ""
            if current:
                runs.append(current.strip())
        return [r for r in runs if r]

    def wait_for(
        self,
        pattern: str | re.Pattern[str],
        *,
        timeout: float = 20,
        stable_ms: int = 500,
        cols: tuple[int, int] | None = None,
    ) -> Screen:
        """Wait until ``pattern`` is on screen and the screen has stopped changing."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        seen_since: float | None = None
        last_digest = ""
        screen = self.screen()
        while time.monotonic() < deadline:
            screen = self.screen()
            if screen.ws == "closed":
                raise StepTimeout(f"{rx.pattern!r} (the terminal closed)", screen, timeout)
            if screen.find(rx, cols=cols) is not None:
                digest = screen.digest()
                if digest != last_digest:
                    last_digest, seen_since = digest, time.monotonic()
                elif seen_since is not None and (time.monotonic() - seen_since) * 1000 >= stable_ms:
                    return screen
            else:
                seen_since, last_digest = None, ""
            time.sleep(0.1)
        raise StepTimeout(repr(rx.pattern), screen, timeout)

    def wait_prompt(self, count: int, *, timeout: float = 60) -> Screen:
        """Wait until the shell has drawn its ``count``-th prompt (OSC 133;A)."""
        deadline = time.monotonic() + timeout
        screen = self.screen()
        while time.monotonic() < deadline:
            screen = self.screen()
            if screen.prompts >= count:
                return screen
            time.sleep(0.1)
        raise StepTimeout(f"shell prompt #{count}", screen, timeout)

    def settle(self, *, quiet_ms: int = 600, timeout: float = 10) -> Screen:
        """Wait for a complete, unchanging frame (see the module docstring)."""
        deadline = time.monotonic() + timeout
        last, since = "", time.monotonic()
        screen = self.screen()
        while time.monotonic() < deadline:
            screen = self.screen()
            digest = screen.digest()
            if digest != last:
                last, since = digest, time.monotonic()
            elif (
                not screen.bsu
                and screen.since_write_ms >= 250
                and (time.monotonic() - since) * 1000 >= quiet_ms
            ):
                return screen
            time.sleep(0.08)
        return screen  # an always-busy screen (a spinner) is still worth a picture

    # --- acting --------------------------------------------------------------------

    def _focus(self) -> None:
        self.page.evaluate("window.term.focus()")

    def type(self, text: str, *, delay_ms: int = 12) -> None:
        self._focus()
        self.page.keyboard.type(text, delay=delay_ms)

    def press(self, *keys: str, gap_ms: int = 120) -> None:
        self._focus()
        for key in keys:
            self.page.keyboard.press(key)
            # A lone Esc followed by a letter within ~100 ms reads as Alt+letter; keys
            # land at human-ish speed so the app sees what a person sends.
            time.sleep(gap_ms / 1000)

    def run(self, command: str, *, timeout: float = 120) -> int | None:
        """Type a shell command, press Enter, wait for the next prompt; its exit code."""
        before = self.screen()
        self.type(command)
        self.press("Enter")
        after = self.wait_prompt(before.prompts + 1, timeout=timeout)
        return after.exit_codes[-1] if after.exit_codes else None

    def click_cell(self, col: int, row: int) -> None:
        box = self.page.locator(".xterm-screen").bounding_box()
        if box is None:
            raise RuntimeError("the terminal has no screen element")
        x = box["x"] + (col + 0.5) * box["width"] / self.cols
        y = box["y"] + (row + 0.5) * box["height"] / self.rows
        self.page.mouse.click(x, y)

    def click_text(self, pattern: str, *, cols: tuple[int, int] | None = None) -> None:
        where = self.screen().find(pattern, cols=cols)
        if where is None:
            raise StepTimeout(repr(pattern), self.screen(), 0)
        self.click_cell(*where)

    def press_until_focused(self, label: str, *, key: str = "Tab", max_presses: int = 10) -> None:
        """Move keyboard focus with ``key`` until ``label`` is the focused control."""
        for _ in range(max_presses):
            self.settle(quiet_ms=300, timeout=5)
            if any(label in run for run in self.focused()):
                return
            self.press(key)
        raise StepTimeout(f"focus on {label!r}", self.screen(), 0)

    # --- evidence ------------------------------------------------------------------

    def shot(self, png: Path, txt: Path) -> Screen:
        """A coherent screenshot of the terminal window, plus the same screen as text."""
        png.parent.mkdir(parents=True, exist_ok=True)
        txt.parent.mkdir(parents=True, exist_ok=True)
        screen = self.settle()
        for _ in range(3):
            self.page.evaluate(
                "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
            )
            self.page.locator("#window").screenshot(path=str(png))
            again = self.screen()
            if again.digest() == screen.digest():
                break
            screen = self.settle()
        txt.write_text(screen.text + "\n", encoding="utf-8")
        return screen

    def video_path(self) -> Path | None:
        video = self.page.video
        return Path(video.path()) if video else None

    def close(self) -> Path | None:
        """Close the window (the shell gets SIGHUP, as for a user) and keep the video."""
        video = self.video_path()
        try:
            self.context.close()
        finally:
            try:
                self._ttyd.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._ttyd.kill()
            self._ttyd_log.close()
        return video
