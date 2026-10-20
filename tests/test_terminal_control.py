"""The visible pane over ONE tmux control-mode client (``core.tmux_control``), and the
polling it falls back to.

Two halves. Against a real tmux (skipped without one; CI runs them on ubuntu-24.04's
tmux 3.4, and ``pytest -s`` prints the measurements): the latency a key takes to
reach the screen, the frames a wheel gesture gets while it lasts, that no tmux
process is started per key or per frame, that hidden panes send nothing and keep
running, that a killed client hands the pane back to polling, that no window
changes size, and that ``fleet attach``'s raw client and its F12 detach leave the
control client alone. Against the fake tmux: the polling loop itself — input
throttles the next frame rather than postponing it, and an echo slower than one
tick still shows without the idle wait — and the protocol pieces (quoting, the
``%begin``/``%end`` framing, the coalesced wake) driven line by line.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import shutil
import signal
import statistics
import string
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from textual import events

from aisquare.cli.ui import terminal as terminal_module
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.core import tmux_control
from aisquare.core.tmux import (
    BUNDLED_CONF,
    Capture,
    Completed,
    TmuxError,
    TmuxServer,
    TmuxTimedOut,
    _tmux,
)
from aisquare.core.tmux_control import ControlClient, Reply, quote
from tests.pane_harness import FakePane, FakeTmux, mouse_event
from tests.test_terminal_pane import Host, run, screen_text, wait_until

_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


# --- the real tmux ----------------------------------------------------------------------


def _counting(calls: list[tuple[str, ...]]) -> Callable[[Sequence[str], bytes | None], Completed]:
    """The real tmux runner, every process it starts counted."""

    def runner(argv: Sequence[str], stdin: bytes | None) -> Completed:
        calls.append(tuple(argv))
        return _tmux(argv, stdin)

    return runner


@pytest.fixture
def calls() -> list[tuple[str, ...]]:
    return []


@pytest.fixture
def live(tmp_path: Path, calls: list[tuple[str, ...]]) -> Iterator[TmuxServer]:
    """A private real server whose tmux processes are counted; control mode allowed."""
    conf = tmp_path / "tmux.conf"
    conf.write_text(BUNDLED_CONF, encoding="utf-8")
    server = TmuxServer(
        f"asq-test-{os.getpid()}-control", conf=conf, runner=_counting(calls), control=True
    )
    terminal_module.forget_server_versions()
    try:
        yield server
    finally:
        with contextlib.suppress(TmuxError):
            server.run("kill-server")
        terminal_module.forget_server_versions()


def _ticker(name: str) -> list[str]:
    """A program that prints a numbered line every 10 ms, forever."""
    return ["sh", "-c", 'i=0; while :; do i=$((i+1)); echo "$0 $i"; sleep 0.01; done', name]


def _window(
    server: TmuxServer,
    session: str,
    tmp_path: Path,
    command: list[str],
    *,
    width: int = 80,
    height: int = 24,
) -> str:
    return server.spawn_window(
        session, name="w", cwd=tmp_path, command=command, width=width, height=height
    ).pane_id


def _serving(pane: TerminalPane) -> bool:
    return pane._serving() is not None


def _client(pane: TerminalPane) -> ControlClient:
    link = pane._serving()
    assert link is not None, "the pane is not served by a control client"
    return link.client


def _mouse_echo(tmp_path: Path) -> Path:
    """A fullscreen program that tracks the mouse (``?1000`` + ``?1006``) and prints
    every report it reads, one line each — Claude Code's shape, made observable."""
    script = tmp_path / "mouse_echo.py"
    script.write_text(
        "\n".join(
            [
                "import sys, tty",
                "out = sys.stdout",
                "out.write('\\x1b[?1049h\\x1b[?1000h\\x1b[?1006h'); out.flush()",
                "tty.setraw(sys.stdin.fileno())",
                "n = 0",
                "while True:",
                "    data = sys.stdin.buffer.read1(256)",
                "    if not data: break",
                "    n += 1",
                "    out.write(f'read {n} ' + repr(data.decode('latin-1')) + '\\r\\n')",
                "    out.flush()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def _last_frame_shows(frames: list[tuple[float, str]], text: str) -> Callable[[], bool]:
    return lambda: bool(frames) and text in frames[-1][1]


def _row_shows(pane: TerminalPane, text: str, row: int = 0) -> Callable[[], bool]:
    return lambda: text in screen_text(pane)[row]


def _key_latencies(
    server: TmuxServer, pane_id: str, monkeypatch: pytest.MonkeyPatch, keys: int
) -> list[float]:
    """Type ``keys`` letters into the ``cat`` in ``pane_id``, one at a time, and return each
    key's latency in milliseconds: from the pane forwarding it to the first frame that
    shows it. Served by the control client when one comes up, polled when not."""
    sent: list[float] = []
    frames: list[tuple[float, str]] = []

    async def drive() -> list[float]:
        host = Host(server, pane_id)
        typed = ""
        latencies: list[float] = []
        async with host.run_test(size=(100, 10)) as pilot:
            pane = host.pane
            forward, show = pane._send, pane._show_capture

            def recording_send(translation: Any) -> None:
                sent.append(time.monotonic())
                forward(translation)

            def recording_show(capture: Capture) -> bool:
                changed = show(capture)
                frames.append((time.monotonic(), capture.lines[0] if capture.lines else ""))
                return changed

            monkeypatch.setattr(pane, "_send", recording_send)
            monkeypatch.setattr(pane, "_show_capture", recording_show)
            pane.focus()
            await wait_until(pilot, lambda: pane.facts is not None, timeout=5.0)
            if terminal_module.CONTROL_MODE:
                await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            await pilot.pause(0.3)
            for index in range(keys):
                typed += string.ascii_lowercase[index % 26]
                await pilot.press(typed[-1])
                await wait_until(pilot, _last_frame_shows(frames, typed), timeout=3.0)
                at = sent[-1]
                shown = next(when for when, row in frames if when >= at and typed in row)
                latencies.append((shown - at) * 1000)
        return latencies

    return run(drive())


@_needs_tmux
def test_real_tmux_a_key_reaches_the_screen_within_15_ms_over_the_control_client(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 1. Key to frame, median over 60 keys into ``cat``: at most 15 ms.

    Polled, a key was one ``send-keys`` process (~5 ms), then the frame timer —
    re-armed by the key to 50 ms — then a ``capture-pane`` process: 60 ms at best.
    Over the control client the key is a line down the pipe, the frame is asked
    for at once, and the echo's ``%output`` asks for the next one. The polled
    numbers are measured beside it, with the fixed throttle, for the record."""
    control_pane = _window(live, "asq-lat", tmp_path, ["cat"], width=100, height=10)
    polled_pane = _window(live, "asq-lat", tmp_path, ["cat"], width=100, height=10)
    control = _key_latencies(live, control_pane, monkeypatch, 60)
    monkeypatch.setattr(terminal_module, "CONTROL_MODE", False)
    polled = _key_latencies(live, polled_pane, monkeypatch, 30)

    def summary(values: list[float]) -> str:
        ordered = sorted(values)
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        return (
            f"median {statistics.median(values):.1f} ms, p95 {p95:.1f} ms, "
            f"max {max(values):.1f} ms over {len(values)} keys"
        )

    print(f"\nkey to frame, control client: {summary(control)}")
    print(f"key to frame, polling:        {summary(polled)}")
    assert statistics.median(control) <= 15.0, summary(control)
    assert statistics.median(control) < statistics.median(polled)


@_needs_tmux
def test_real_tmux_a_wheel_gesture_gets_frames_while_it_lasts(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Acceptance 2. A wheel notch every 10 ms for 500 ms into a program that tracks the
    mouse: at least 10 frames that changed arrive DURING the gesture.

    Polled, every 20 ms flush re-armed the frame timer, so no frame came until the
    wheel stopped — "scroll is not instant"."""
    script = _mouse_echo(tmp_path)
    pane_id = _window(live, "asq-wheel", tmp_path, [sys.executable, str(script)])

    async def drive() -> tuple[int, int]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None and pane.facts.mouse_on, 5.0)
            await wait_until(pilot, lambda: _serving(pane), timeout=3.0)
            await pilot.pause(0.2)
            changed: list[float] = []
            show = pane._show_capture

            def recording(capture: Capture) -> bool:
                moved = show(capture)
                if moved:
                    changed.append(time.monotonic())
                return moved

            pane._show_capture = recording  # type: ignore[method-assign]
            start = time.monotonic()
            for _ in range(50):
                host.post_message(mouse_event(events.MouseScrollDown, pane, (5, 5), 0))
                await asyncio.sleep(0.01)
            end = time.monotonic()
            during = sum(1 for when in changed if start <= when <= end)
            reads = max(
                (int(row.split()[1]) for row in screen_text(pane) if row.startswith("read ")),
                default=0,
            )
            return during, reads

    during, reads = run(drive())
    print(f"\nwheel, 50 notches over 500 ms: {during} changed frames during the gesture")
    assert reads > 0, "the program never read a notch"
    assert during >= 10, during


@_needs_tmux
def test_real_tmux_no_tmux_process_is_started_per_key_or_per_frame(
    live: TmuxServer, tmp_path: Path, calls: list[tuple[str, ...]]
) -> None:
    """Acceptance 3. With the control client up, typing and the frames it brings start
    no tmux process: the runner — every polled command's one way out — counts none."""
    pane_id = _window(live, "asq-count", tmp_path, ["cat"])

    async def drive() -> tuple[int, int]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            await pilot.pause(0.3)
            calls.clear()
            frames = pane.frames
            typed = ""
            for letter in "controlmode" * 3:
                typed += letter
                await pilot.press(letter)
                await wait_until(pilot, _row_shows(pane, typed))
            await pilot.pause(0.6)  # the idle net's frames too
            return len(calls), pane.frames - frames

    started, frames = run(drive())
    assert frames >= 33, frames
    assert started == 0, calls


@_needs_tmux
def test_real_tmux_hidden_panes_send_nothing_and_keep_running(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Acceptance 4. While pane A is on screen, B (A's session, hidden) and C (another
    session) print every 10 ms and none of it reaches the pipe — muted with ``pause``,
    which leaves them RUNNING: their history grows and ``#{window_activity}``, the busy
    pulse, moves. ``off`` would have frozen them (``core.tmux_control``'s docstring).
    Shown, B is followed and A goes quiet; a pane hidden in the UI is paused too."""
    a = _window(live, "asq-mute", tmp_path, _ticker("A"))
    b = _window(live, "asq-mute", tmp_path, _ticker("B"))
    c = _window(live, "asq-other", tmp_path, _ticker("C"))

    def quiet(client: ControlClient, panes: list[str]) -> dict[str, int]:
        return {pane: client.outputs.get(pane, 0) for pane in panes}

    async def drive() -> dict[str, Any]:
        host = Host(live, a)
        found: dict[str, Any] = {}
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            client = _client(pane)
            await pilot.pause(0.3)
            before = quiet(client, [a, b, c])
            facts = {p: live.pane_facts(p) for p in (b, c)}
            await pilot.pause(1.6)
            after = quiet(client, [a, b, c])
            found["on A"] = {p: after[p] - before[p] for p in (a, b, c)}
            later = {p: live.pane_facts(p) for p in (b, c)}
            found["running"] = {
                p: (
                    later[p].history_size > facts[p].history_size  # type: ignore[union-attr]
                    and later[p].last_output > facts[p].last_output  # type: ignore[operator, union-attr]
                )
                for p in (b, c)
            }
            pane.attach(b)
            await wait_until(pilot, lambda: _serving(pane) and pane.facts is not None, 3.0)
            await pilot.pause(0.3)
            before = quiet(client, [a, b, c])
            await pilot.pause(0.5)
            after = quiet(client, [a, b, c])
            found["on B"] = {p: after[p] - before[p] for p in (a, b, c)}
            pane.display = False  # the tab goes behind another
            await pilot.pause(0.3)
            before = quiet(client, [a, b, c])
            await pilot.pause(0.5)
            after = quiet(client, [a, b, c])
            found["hidden"] = {p: after[p] - before[p] for p in (a, b, c)}
        return found

    found = run(drive())
    print(f"\n%output lines per pane: {found}")
    on_a, on_b, hidden = found["on A"], found["on B"], found["hidden"]
    assert on_a[a] > 0 and on_a[b] == 0 and on_a[c] == 0, on_a
    assert found["running"] == {b: True, c: True}, found["running"]
    assert on_b[b] > 0 and on_b[a] == 0 and on_b[c] == 0, on_b
    assert hidden == {a: 0, b: 0, c: 0}, hidden


@_needs_tmux
def test_real_tmux_a_killed_control_client_hands_the_pane_to_polling_within_a_second(
    live: TmuxServer, tmp_path: Path, calls: list[tuple[str, ...]]
) -> None:
    """Acceptance 5. The control client killed outright: the pane keeps rendering, by
    polling, within a second — no toast."""
    # Skipped without tmux already, and Windows has none; ``pytest.skip`` is typed
    # ``NoReturn``, so this also narrows the platform for mypy, which checks the suite
    # under Windows too: ``signal.SIGKILL`` is POSIX-only in typeshed.
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id = _window(live, "asq-kill", tmp_path, ["cat"])

    async def drive() -> tuple[float, bool, list[str]]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            pid = _client(pane).pid
            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            killed = time.monotonic()
            live.run("send-keys", "-t", pane_id, "-l", "after the kill")
            await wait_until(pilot, lambda: "after the kill" in screen_text(pane)[0], timeout=1.0)
            return time.monotonic() - killed, pane._link is None, list(host.notices)

    took, polling, notices = run(drive())
    print(f"\nkilled control client: the pane rendered by polling {took * 1000:.0f} ms later")
    assert polling
    assert took < 1.0
    assert notices == [], notices


@_needs_tmux
def test_real_tmux_no_window_changes_size_when_the_control_client_attaches(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Acceptance 6. ``window-size manual`` is deliberately absent (tmux 3.4 crashes on
    it), so a window nobody pinned follows the latest client's size — a session's
    first window is never pinned (``spawn_window``). The control client attaches with
    ``ignore-size`` and is never given a size: every window keeps its size while it
    attaches, follows another session, and ends. CI runs this on tmux 3.4."""
    shown = _window(live, "asq-size", tmp_path, ["cat"], width=80, height=24)
    _window(live, "asq-size", tmp_path, ["cat"], width=132, height=43)  # pinned
    _window(live, "asq-elsewhere", tmp_path, ["cat"], width=101, height=31)  # first: unpinned
    other = _window(live, "asq-elsewhere", tmp_path, ["cat"], width=80, height=24)

    def sizes() -> list[str]:
        out = live.run(
            "list-windows",
            "-a",
            "-F",
            "#{session_name}:#{window_index} #{window_width}x#{window_height}",
        )
        return sorted(out.split("\n"))

    before = sizes()

    async def drive() -> tuple[list[str], list[str], str]:
        host = Host(live, shown)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            await pilot.pause(0.3)
            attached = sizes()
            pane.attach(other)
            await wait_until(pilot, lambda: _serving(pane) and pane.facts is not None, 3.0)
            await pilot.pause(0.3)
            clients = live.run("list-clients", "-F", "#{client_flags}")
            return attached, sizes(), clients

    attached, followed, clients = run(drive())
    assert "control-mode" in clients and "ignore-size" in clients, clients
    assert attached == before, (before, attached)
    assert followed == before, (before, followed)
    assert sizes() == before


@_needs_tmux
def test_real_tmux_fleet_attach_and_its_f12_detach_leave_the_control_client_alone(
    live: TmuxServer, tmp_path: Path
) -> None:
    """``fleet attach`` is a raw tmux client on the same session, and F12 is bound to
    ``detach-client``: that detaches the raw client, never the UI's control client,
    which goes on bringing frames."""
    # Skipped without tmux already, and Windows has none; ``pytest.skip`` is typed
    # ``NoReturn``, so this also narrows the platform for mypy, which checks the suite
    # under Windows too: ``pty`` is POSIX-only in typeshed.
    if sys.platform == "win32":
        pytest.skip("no pty on Windows; fleet attach needs a terminal")
    pane_id = _window(live, "asq-attach", tmp_path, ["cat"])
    session = "asq-attach"

    async def drive() -> tuple[int, bool, bool]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            client = _client(pane)
            import pty

            fd, terminal = pty.openpty()
            raw = subprocess.Popen(
                live.attach_argv(session),
                stdin=terminal,
                stdout=terminal,
                stderr=terminal,
                start_new_session=True,
                # A terminal's own: the suite's TERM may be one tmux cannot draw on.
                env={**os.environ, "TERM": "xterm-256color"},
            )
            os.close(terminal)
            try:
                await wait_until(
                    pilot,
                    lambda: len(live.run("list-clients", "-F", "#{client_name}").split()) == 2,
                    timeout=5.0,
                )
                os.write(fd, b"\x1b[24~")  # F12
                await wait_until(
                    pilot,
                    lambda: len(live.run("list-clients", "-F", "#{client_name}").split()) == 1,
                    timeout=5.0,
                )
                detached = raw.wait(timeout=5) == 0
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)
                if raw.poll() is None:
                    raw.kill()
                    raw.wait()
            await pilot.press("z")
            await wait_until(pilot, lambda: "z" in screen_text(pane)[0], timeout=2.0)
            left = len(live.run("list-clients", "-F", "#{client_name}").split())
            return left, client.alive and _serving(pane), detached

    left, served, detached = run(drive())
    assert detached
    assert left == 1
    assert served


@_needs_tmux
def test_real_tmux_a_command_that_fails_sends_the_pane_back_to_polling(
    live: TmuxServer, tmp_path: Path
) -> None:
    """A control command that errors — here the pane's window killed under it — puts
    the pane back on the polling loop for the rest of the attach, which says
    ``(pane gone)`` the way it always has."""
    pane_id = _window(live, "asq-gone", tmp_path, ["cat"])
    _window(live, "asq-gone", tmp_path, ["cat"])  # the session outlives the window

    async def drive() -> tuple[bool, str | None]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            await pilot.pause(0.3)  # the debounced resize has landed: it cannot answer first
            live.run("kill-window", "-t", pane_id)
            # Nothing prints in a killed pane: the idle net's frame finds it gone.
            await wait_until(pilot, lambda: pane._polling, timeout=1.5)
            await wait_until(pilot, lambda: pane.notice is not None, timeout=1.0)
            return pane._link is None, pane.notice

    polling, notice = run(drive())
    assert polling
    assert notice == terminal_module.PANE_GONE
    # The frame's two commands are ONE chain: on tmux 3.2a a lone ``display-message``
    # of the gone pane's ``#{window_activity}`` killed the server and every agent on it.
    assert live.run("list-sessions", "-F", "#{session_name}").split() == ["asq-gone"]


@_needs_tmux
def test_real_tmux_quoting_survives_the_command_parser(live: TmuxServer, tmp_path: Path) -> None:
    """What :func:`quote` makes of an argument is what tmux's parser hands the command:
    every character of tmux's command language that means something on a line —
    quotes, ``;``, ``$``, ``#``, braces, ``~``, ``%``, a backslash, a tab, a newline."""
    _window(live, "asq-quote", tmp_path, ["cat"])
    weird = "a;b 'c' \"d\" $HOME ${X} #{session_name}x ~/y {z} %if \\e\tt\nn ;"
    answers: list[Reply] = []

    client = ControlClient(
        live, deliver=lambda call: call(), on_output=lambda _: None, on_exit=lambda _: None
    )
    client.start_client(live.run("list-panes", "-a", "-F", "#{pane_id}").split()[0])
    try:
        client.command("set-buffer", "-b", "asq-quote-probe", "--", weird, done=answers.append)
        deadline = time.monotonic() + 3
        while not answers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert answers and answers[0].ok, answers
        assert live.run("show-buffer", "-b", "asq-quote-probe") == weird
    finally:
        client.close_client()


def _set_buffers(live: TmuxServer, texts: list[str]) -> dict[str, str | None]:
    """Each of ``texts`` through ``set-buffer`` down a control client, read back by the
    polled ``show-buffer``: what tmux's parser made of :func:`quote`'s token (``None``
    for a command tmux refused)."""
    answers: list[Reply] = []
    client = ControlClient(
        live, deliver=lambda call: call(), on_output=lambda _: None, on_exit=lambda _: None
    )
    client.start_client(live.run("list-panes", "-a", "-F", "#{pane_id}").split()[0])
    try:
        for index, text in enumerate(texts):
            client.command(
                "set-buffer", "-b", f"asq-tilde-{index}", "--", text, done=answers.append
            )
        deadline = time.monotonic() + 3
        while len(answers) < len(texts) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(answers) == len(texts), answers
        return {
            text: live.show_buffer(f"asq-tilde-{index}") if answers[index].ok else None
            for index, text in enumerate(texts)
        }
    finally:
        client.close_client()


@_needs_tmux
def test_real_tmux_a_tilde_anywhere_in_a_pasted_text_arrives_as_itself(
    live: TmuxServer, tmp_path: Path
) -> None:
    """tmux expands a ``~`` that opens a double-quoted string into a home directory: a
    paste of ``~/x`` arrived as ``$HOME/x``, and ``~~strike~~`` or ``~nosuchuser`` did
    not parse at all, so the paste was dropped (review of #272). Every ``~`` is spelt
    ``\\176``, at the start, in the middle, alone, and after another quote."""
    _window(live, "asq-tilde", tmp_path, ["cat"])
    texts = ["~/code/app", "~ approx 5", "~", "~root", "~~strike~~ done", "~nosuchuser1234/x"]
    texts += ["a ~/b", 'x"~"', "trailing ~"]
    got = _set_buffers(live, texts)
    assert got == {text: text for text in texts}, got


@_needs_tmux
def test_real_tmux_following_a_pane_leaves_its_sessions_current_window_alone(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Every client attached to a session shares its current window, so following a pane
    by ``%pane`` (``attach-session -t``/``switch-client -t``) moved a ``fleet attach``
    client in another terminal onto that agent's window, keys and all (review of #272:
    a1 → a3 → a2). The client follows the SESSION: the current window stays a1 while the
    pane on screen is a3, then a2 — and their ``%output`` still arrives."""
    a1 = _window(live, "asq-current", tmp_path, ["cat"])
    a2 = _window(live, "asq-current", tmp_path, ["cat"])
    a3 = _window(live, "asq-current", tmp_path, ["cat"])
    live.run("select-window", "-t", a1)

    def current() -> str:
        # A window's ``#{pane_id}`` is its active pane; the session's current window says 1.
        return live.run(
            "list-windows", "-t", "=asq-current", "-F", "#{?window_active,#{pane_id},}"
        ).split()[0]

    async def drive() -> list[tuple[str, str, int]]:
        host = Host(live, a3)
        seen: list[tuple[str, str, int]] = []
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            for shown in (a3, a2):
                if shown != pane.pane_id:
                    pane.attach(shown)
                await wait_until(pilot, lambda: _serving(pane) and pane.facts is not None, 5.0)
                await pilot.pause(0.2)
                before = _client(pane).outputs.get(shown, 0)
                await pilot.press("q")
                await wait_until(pilot, _row_shows(pane, "q"), timeout=2.0)
                seen.append((shown, current(), _client(pane).outputs.get(shown, 0) - before))
        return seen

    seen = run(drive())
    assert [(shown, now) for shown, now, _ in seen] == [(a3, a1), (a2, a1)], seen
    assert all(outputs > 0 for _, _, outputs in seen), seen


@_needs_tmux
def test_real_tmux_a_stalled_client_killed_waits_out_the_retry_before_another_starts(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that stops answering is killed and its panes poll — and the server rests
    for :data:`~aisquare.cli.ui.terminal.CONTROL_RETRY` before it is offered another. When
    the pane was the client's only user, its release closed the client before the end of
    file, no end was delivered, nothing was recorded, and a new client started 634 ms
    later (review of #272): a server that keeps wedging cost a spawn and a kill a tick."""
    pane_id = _window(live, "asq-stall", tmp_path, ["cat"])
    stalls = [True]

    def stalled(self: ControlClient, limit: float) -> bool:
        return bool(stalls) and stalls.pop()

    async def drive() -> tuple[int | None, list[int | None], float]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            first = _client(pane).pid
            monkeypatch.setattr(ControlClient, "stalled", stalled)
            await wait_until(pilot, lambda: pane._link is None, timeout=2.0)
            killed = time.monotonic()
            seen: list[int | None] = []
            for _ in range(15):  # 1.5 s: three idle ticks, each of which could start one
                await pilot.pause(0.1)
                link = pane._link
                seen.append(link.client.pid if link is not None else None)
            key = terminal_module._server_key(live)
            return first, seen, terminal_module._CONTROL_RETRY_AT.get(key, 0.0) - killed

    first, seen, rests = run(drive())
    assert first is not None
    assert seen == [None] * len(seen), f"a new client started: {seen}"
    assert rests >= terminal_module.CONTROL_RETRY - 1, rests


@_needs_tmux
def test_real_tmux_the_followed_project_ending_does_not_cost_the_other_panes_their_client(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Stopping the last agent of the project on screen destroys its session, and tmux
    detaches a client from a destroyed session (``%exit``): every pane on the server then
    polled for :data:`~aisquare.cli.ui.terminal.CONTROL_RESTART`, after an ordinary stop
    (review of #272). The client asks to stay (``no-detach-on-destroy``, which tmux 3.8
    honours), and a plain detach is offered a new client at once (3.2a and 3.4 detach
    whatever the flag says): the next pane shown is served within a second either way."""
    gone = _window(live, "asq-ends", tmp_path, ["cat"])
    stays = _window(live, "asq-stays", tmp_path, ["cat"])

    async def drive() -> tuple[float, list[str]]:
        host = Host(live, gone)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            live.run("kill-window", "-t", gone)  # its session's last window
            ended = time.monotonic()
            pane.attach(stays)
            await wait_until(pilot, lambda: _serving(pane), timeout=3.0)
            return time.monotonic() - ended, list(host.notices)

    took, notices = run(drive())
    print(f"\nthe followed session ended: the next pane served again {took * 1000:.0f} ms later")
    assert took < 1.5, took
    assert notices == [], notices


@_needs_tmux
def test_real_tmux_keys_in_flight_when_the_client_dies_arrive_once_each_by_polling(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input sent down the pipe and not yet answered when the client dies is not lost,
    and not doubled: what tmux ran is read off the pane's stamp (``@asq-sent``, set in
    the same chain as each input) and the rest is sent again by polling, in order, ahead
    of whatever was typed meanwhile. It was lost: killing the client 3 ms into a typed
    burst lost ``56789abcdef``, or the whole burst (tester of #272, 2 of 6 trials).

    Five bursts of forty keys, the client killed right after the tenth goes down the
    pipe; the program records what it reads, raw, and must read every key exactly once.
    tmux reads a control client's stdin itself, so what was written may still run after
    the kill — the even trials leave that to the platform's timing. The odd ones make
    the ten unrun (written where nothing reads them) and hold the client's end until the
    rest of the burst is handled, the order Linux showed: the rest finds the client
    ended (``_control_link``) before its end is delivered. Without recovery 60 of 200
    keys arrived; with it but not from that path, all 200, out of order."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    record = tmp_path / "typed"
    pane_id = _window(
        live, "asq-burst", tmp_path, ["sh", "-c", f"stty raw -echo; exec cat > {record}"]
    )
    monkeypatch.setattr(terminal_module, "CONTROL_RESTART", 0.0)  # a client again at once
    sent_over = ControlClient.send_literal
    kill_after = [0]
    held_end: dict[str, list[Callable[[], object]] | None] = {"calls": None}
    """While a list, the dying client's end is held there and delivered late."""
    released: list[Callable[[Callable[[], object]], object]] = []

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        late_end = held_end["calls"]
        if late_end is not None and kill_after[0] == 10:
            # tmux reads a control client's stdin itself, so killing the client does not
            # stop what was written reaching tmux: the keys tmux would have read and freed
            # with the dead client, or never read, are made so here — written to a pipe
            # nobody drains — so all ten are unrun, whatever the platform's timing.
            self._outbox = queue.SimpleQueue()
        sent_over(self, pane, text, **kwargs)
        kill_after[0] -= 1
        if kill_after[0] == 0 and self.pid is not None:
            if late_end is not None:
                # The order Linux showed: the rest of the burst finds the client ended
                # before its end is delivered (``_control_link``, not ``_control_lost``).
                released.append(self._deliver)
                self._deliver = late_end.append
            os.kill(self.pid, signal.SIGKILL)  # mid-burst: some ran, some are on their way
            deadline = time.monotonic() + 2
            while late_end is not None and self.alive and time.monotonic() < deadline:
                time.sleep(0.001)

    monkeypatch.setattr(ControlClient, "send_literal", send_literal)
    letters = string.ascii_letters

    async def drive() -> tuple[str, str]:
        host = Host(live, pane_id)
        expected = ""
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            for trial in range(5):
                # Answered once: a client killed before it ever answered rests the server for
                # CONTROL_RETRY, as one that never started does.
                await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
                burst = "".join(letters[(trial * 7 + i) % len(letters)] for i in range(40))
                kill_after[0] = 10
                late_end: list[Callable[[], object]] | None = [] if trial % 2 else None
                held_end["calls"] = late_end  # both orders of the end and the rest
                for char in burst:
                    pane.post_message(events.Key(char, char))
                expected += burst
                if late_end is not None:
                    await pilot.pause(0.3)  # the whole burst handled, the end still held
                    for call in late_end:
                        released[-1](call)
                deadline = time.monotonic() + 5.0
                while len(record.read_text()) < len(expected) and time.monotonic() < deadline:
                    await pilot.pause(0.05)
            await pilot.pause(0.5)  # anything doubled late has arrived by now
        return expected, record.read_text()

    expected, typed = run(drive())
    assert typed == expected, (len(expected), len(typed))


def _raw_client(live: TmuxServer, session: str) -> tuple[subprocess.Popen[bytes], int]:
    """``fleet attach``'s argv on a pty of its own: a raw tmux client, and its terminal."""
    if sys.platform == "win32":  # its callers skip there; this tells mypy, as pty is POSIX's
        raise AssertionError("fleet attach needs a terminal")
    import pty

    fd, terminal = pty.openpty()
    raw = subprocess.Popen(
        live.attach_argv(session),
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(terminal)
    return raw, fd


def _drained(fd: int) -> int:
    """How many bytes the raw client's terminal has been sent since the last drain."""
    import select

    count = 0
    while select.select([fd], [], [], 0)[0]:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        count += len(chunk)
    return count


@_needs_tmux
def test_real_tmux_typing_into_an_agent_sends_nothing_to_a_raw_client_elsewhere(
    live: TmuxServer, tmp_path: Path
) -> None:
    """The stamp each input sets (what tells, if the client dies, which of it tmux ran)
    must cost a ``fleet attach`` client nothing. ``set-option -p`` repainted EVERY attached
    client on the server at every key, whatever session it was on: 40 keys into one agent
    sent ~9.8 KB a key to a 200x50 raw client on another project (re-review of #272). A
    hidden session variable (``set-environment -h``) costs it nothing (measured on 3.2a,
    3.4 and 3.8)."""
    if sys.platform == "win32":
        pytest.skip("no pty on Windows; fleet attach needs a terminal")
    typed_into = _window(live, "asq-typing", tmp_path, ["cat"], width=100, height=24)
    _window(live, "asq-elsewhere", tmp_path, ["cat"], width=100, height=24)

    async def drive() -> tuple[int, str]:
        host = Host(live, typed_into)
        async with host.run_test(size=(100, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            raw, fd = _raw_client(live, "asq-elsewhere")
            try:
                await wait_until(
                    pilot,
                    lambda: len(live.run("list-clients", "-F", "#{client_name}").split()) == 2,
                    timeout=5.0,
                )
                # The raw client's own paint, and the redraw tmux makes when its queries
                # of the terminal time out (5 s: this pty answers none) — then quiet.
                attached, quiet_since = time.monotonic(), time.monotonic()
                while time.monotonic() - attached < 12:
                    await pilot.pause(0.1)
                    if _drained(fd):
                        quiet_since = time.monotonic()
                    elif time.monotonic() - attached > 5.5 and time.monotonic() - quiet_since > 1:
                        break
                typed = ""
                for letter in "stampsmustcostnothing" * 2:
                    typed += letter
                    await pilot.press(letter)
                    await wait_until(pilot, _row_shows(pane, typed), timeout=2.0)
                await pilot.pause(0.3)
                return _drained(fd), screen_text(pane)[0]
            finally:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raw.kill()
                raw.wait()

    sent, row = run(drive())
    assert row.startswith("stampsmustcostnothing"), row
    assert sent == 0, f"{sent} bytes to a raw client on another session while typing 42 keys"


def _recording_pane(live: TmuxServer, tmp_path: Path, session: str) -> tuple[str, Path]:
    """A pane whose program records what it reads, raw: what arrived, and in what order."""
    record = tmp_path / f"{session}.typed"
    pane_id = _window(live, session, tmp_path, ["sh", "-c", f"stty raw -echo; exec cat > {record}"])
    return pane_id, record


async def _settled(pilot: Any, record: Path, length: int, seconds: float = 3.0) -> str:
    """The record once it holds ``length`` characters, or what it holds after ``seconds``."""
    deadline = time.monotonic() + seconds
    while len(record.read_text()) < length and time.monotonic() < deadline:
        await pilot.pause(0.05)
    return record.read_text()


def _unread_from(client: ControlClient) -> None:
    """What ``client`` is sent from now on is written where nothing reads it: unrun, and
    unanswered, however the platform times the kill (tmux reads a control client's
    stdin itself, so a kill alone does not stop what was written)."""
    client._outbox = queue.SimpleQueue()


@_needs_tmux
def test_real_tmux_a_key_sent_as_the_client_ends_waits_behind_the_keys_recovered(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key whose write finds the client ended — after ``_control_link`` handed the pane
    a live link — went the polled way at once, AHEAD of the keys in flight that the
    recovery was about to send again (re-review of #272). It waits behind them, as every
    key typed during a recovery does."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id, record = _recording_pane(live, tmp_path, "asq-ahead")
    sent_over = ControlClient.send_literal
    calls = [0]

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        calls[0] += 1
        if calls[0] == 1:
            _unread_from(self)  # keys 1-10: in flight, never run
        if calls[0] == 11 and self.pid is not None:
            os.kill(self.pid, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while self.alive and time.monotonic() < deadline:
                time.sleep(0.001)  # ended as this key is written: the write raises
        sent_over(self, pane, text, **kwargs)

    async def drive() -> str:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            monkeypatch.setattr(ControlClient, "send_literal", send_literal)
            for char in string.ascii_lowercase[:20]:
                pane.post_message(events.Key(char, char))
            return await _settled(pilot, record, 20)

    assert run(drive()) == string.ascii_lowercase[:20]


@_needs_tmux
def test_real_tmux_input_in_flight_at_a_deliberate_close_is_settled_there_not_later(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control command failed (``_control_failed``), and the pane let its client go
    with keys still in flight: they stayed in ``_inflight``, unanswered, and were sent
    again at some later, unrelated client end — long after, and out of place (re-review
    of #272). They are recovered at the close, and nothing is sent again later."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id, record = _recording_pane(live, tmp_path, "asq-close")
    monkeypatch.setattr(terminal_module, "CONTROL_RESTART", 0.0)
    sent_over = ControlClient.send_literal
    unread = [True]

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        if unread[0]:
            _unread_from(self)
            unread[0] = False
        sent_over(self, pane, text, **kwargs)

    async def drive() -> tuple[str, str]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            monkeypatch.setattr(ControlClient, "send_literal", send_literal)
            for char in "closed":
                pane.post_message(events.Key(char, char))
            await wait_until(pilot, lambda: len(pane._inflight) == len("closed"), 2.0)
            pane._control_failed("a command failed")  # the pane lets its only client go
            at_close = await _settled(pilot, record, len("closed"), 1.0)
            pane.attach(pane_id)  # a new attach: control again, and a client to end later
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            pid = _client(pane).pid
            assert pid is not None
            os.kill(pid, signal.SIGKILL)  # an unrelated end, nothing of its own in flight
            await pilot.pause(0.5)
            return at_close, record.read_text()

    at_close, later = run(drive())
    assert at_close == "closed"
    assert later == "closed", later


@_needs_tmux
def test_real_tmux_another_asq_typing_into_the_pane_does_not_hide_this_ones_lost_keys(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two asq processes on one server each have their own control client, and both may
    type into one pane. With one stamp per pane, the other's later stamp read as this
    one's own, and this one's unrun keys were taken for run and dropped. Each process
    keeps its own stamp, by its pid (re-review of #272: the docstring said so, the code
    did not)."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id, record = _recording_pane(live, tmp_path, "asq-two")
    sent_over = ControlClient.send_literal
    unread = [True]

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        if unread[0]:
            _unread_from(self)
            unread[0] = False
        sent_over(self, pane, text, **kwargs)

    async def drive() -> str:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            mine = _client(pane)
            monkeypatch.setattr(ControlClient, "send_literal", send_literal)
            for char in "mine":
                pane.post_message(events.Key(char, char))
            await wait_until(pilot, lambda: len(pane._inflight) == len("mine"), 2.0)
            monkeypatch.setattr(ControlClient, "send_literal", sent_over)
            # Another asq process: its own client, its own pid, a later stamp, one key.
            other = ControlClient(
                live, deliver=lambda call: call(), on_output=lambda _: None, on_exit=lambda _: None
            )
            other._pid = 4_000_000
            other.start_client(pane_id)
            answered: list[Reply] = []
            later = terminal_module._next_stamp()
            other.send_literal(pane_id, "Z", done=answered.append, stamp=later)
            await wait_until(pilot, lambda: bool(answered), 2.0)
            other.close_client()
            assert mine.pid is not None
            os.kill(mine.pid, signal.SIGKILL)  # this one's four keys: in flight, never run
            return await _settled(pilot, record, len("Zmine"))

    assert run(drive()) == "Zmine"


@_needs_tmux
def test_real_tmux_a_stamp_that_cannot_be_read_is_asked_again_not_taken_for_a_gone_pane(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp read that FAILS — a server wedged past the command timeout — says nothing
    about what ran, and was read as "the pane is gone": the input in flight was dropped,
    though tmux might still run it later (tester of #272). It is asked again, the keys
    held meanwhile, and once it answers each key arrives exactly once."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id, record = _recording_pane(live, tmp_path, "asq-unread")
    sent_over = ControlClient.send_literal
    unread = [True]
    timeouts = [1]
    run_tmux = TmuxServer.run

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        if unread[0]:
            _unread_from(self)
            unread[0] = False
        sent_over(self, pane, text, **kwargs)

    def wedged_once(self: TmuxServer, *args: str, stdin: bytes | None = None) -> str:
        if args[:1] == ("show-environment",) and timeouts[0]:
            timeouts[0] -= 1
            raise TmuxTimedOut("tmux did not answer within 30s: show-environment …")
        return run_tmux(self, *args, stdin=stdin)

    async def drive() -> str:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            monkeypatch.setattr(ControlClient, "send_literal", send_literal)
            monkeypatch.setattr(TmuxServer, "run", wedged_once)
            for char in "wedge":
                pane.post_message(events.Key(char, char))
            await wait_until(pilot, lambda: len(pane._inflight) == len("wedge"), 2.0)
            pid = _client(pane).pid
            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            await wait_until(pilot, lambda: pane._held is not None, 2.0)
            pane.post_message(events.Key("!", "!"))  # typed while the stamp cannot be read
            return await _settled(pilot, record, len("wedge!"), 5.0)

    assert run(drive()) == "wedge!"
    assert timeouts == [0], "the stamp read never timed out: this test tested nothing"


@_needs_tmux
def test_real_tmux_a_stamp_that_keeps_failing_fast_ends_the_wait_and_says_so(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp read that keeps FAILING — not timing out: an error that is not tmux's
    answer about the pane — held the keys typed behind it forever, said only in a log
    line (review of #272, follow-up). After :attr:`TerminalPane.INPUT_RECOVERY_ATTEMPTS`
    such failures the wait ends, and visibly: the pane's notice says input is
    unconfirmed, a warning names the agent and counts both kinds, and the SAFE choice is
    made for each — the input in flight is not sent again (tmux may still run it, and
    twice is the worse failure: a second Enter submits twice), and what was typed since
    is not sent either (it would go ahead of input tmux may yet run)."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id, record = _recording_pane(live, tmp_path, "asq-never")
    monkeypatch.setattr(TerminalPane, "INPUT_RECOVERY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(TerminalPane, "INPUT_RECOVERY_RETRY", 0.1)
    sent_over = ControlClient.send_literal
    unread = [True]
    run_tmux = TmuxServer.run
    failures = [0]

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        if unread[0]:
            _unread_from(self)
            unread[0] = False
        sent_over(self, pane, text, **kwargs)

    def failing(self: TmuxServer, *args: str, stdin: bytes | None = None) -> str:
        if args[:1] == ("show-environment",):
            failures[0] += 1
            raise TmuxError("could not run tmux: [Errno 24] Too many open files")
        return run_tmux(self, *args, stdin=stdin)

    async def drive() -> tuple[str, str | None, bool, list[str]]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.label = "coder-auth"  # what the agent view names its pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            monkeypatch.setattr(ControlClient, "send_literal", send_literal)
            monkeypatch.setattr(TmuxServer, "run", failing)
            for char in "lost":
                pane.post_message(events.Key(char, char))
            await wait_until(pilot, lambda: len(pane._inflight) == len("lost"), 2.0)
            pid = _client(pane).pid
            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            await wait_until(pilot, lambda: pane._held is not None, 2.0)
            for char in "late":
                pane.post_message(events.Key(char, char))
            await wait_until(pilot, lambda: pane._held is None, 3.0)  # the wait has ended
            monkeypatch.setattr(TmuxServer, "run", run_tmux)
            frames = pane.frames
            await wait_until(pilot, lambda: pane.frames > frames + 2, 3.0)  # good frames since
            notice = pane.notice  # still there: a frame alone does not clear it
            typed = record.read_text()
            await pilot.press("x")  # the next input has read it
            await wait_until(pilot, lambda: pane.notice is None, 2.0)
            return typed, notice, not pane._inflight, list(host.notices)

    typed, notice, settled, notices = run(drive())
    assert typed == "", typed  # neither sent twice nor sent out of order
    assert notice == terminal_module.INPUT_UNCONFIRMED, notice
    assert settled
    assert failures[0] == 3, failures  # counted by attempts, not by the clock
    said = [line for line in notices if "4 inputs sent" in line and "4 typed since" in line]
    assert said and "coder-auth" in said[0] and pane_id not in said[0], notices


def test_a_give_up_over_one_input_counts_it_in_the_singular(tmp_path: Path) -> None:
    """One input in flight and one typed since: the warning said "1 inputs … they were …
    the 1 typed since were" (re-review of #272, nit)."""
    fake = FakeTmux()
    fake.panes["%1"] = FakePane(screen=["$ "])

    server = fake.server(tmp_path)

    async def drive() -> list[str]:
        host = Host(server, "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.label = "coder-auth"
            await wait_until(pilot, lambda: pane.facts is not None)
            sent = terminal_module._Input(1, server, "%1", "key", "Enter")
            pane._inflight, pane._held = [sent], [sent._replace(stamp=2)]
            pane._give_up_recovery(tmux_control.StampUnknown("refused", timed_out=False))
            await pilot.pause()
            return list(host.notices)

    said = [line for line in run(drive()) if "coder-auth" in line]
    assert said, said
    assert "whether 1 input sent to coder-auth" in said[0], said
    assert "it was not sent again" in said[0] and "the 1 typed since was not sent" in said[0]


@_needs_tmux
def test_real_tmux_a_stamp_read_that_times_out_is_waited_out_however_long_the_freeze(
    live: TmuxServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp read that TIMES OUT is a wedged server, and a wedged server answers once it
    resumes: it never counts toward giving up. Counted by the clock from the recovery's
    start, the first 30 s timeout was already past a 10 s cap — it gave up without one
    retry and dropped the keys held behind it: a 130 s freeze delivered 'abcdefghijK'
    where 'abcdefghijKLMNO' was typed (re-review of #272). Here each timed-out read
    outlasts the old cap, and every key arrives once, in order."""
    if sys.platform == "win32":
        pytest.skip("no SIGKILL on Windows")
    pane_id, record = _recording_pane(live, tmp_path, "asq-frozen")
    monkeypatch.setattr(TerminalPane, "INPUT_RECOVERY_GIVE_UP", 0.3, raising=False)  # the old cap
    monkeypatch.setattr(TerminalPane, "INPUT_RECOVERY_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(TerminalPane, "INPUT_RECOVERY_RETRY", 0.1)
    sent_over = ControlClient.send_literal
    unread = [True]
    run_tmux = TmuxServer.run
    timeouts = [3]

    def send_literal(self: ControlClient, pane: str, text: str, **kwargs: Any) -> None:
        if unread[0]:
            _unread_from(self)
            unread[0] = False
        sent_over(self, pane, text, **kwargs)

    def frozen(self: TmuxServer, *args: str, stdin: bytes | None = None) -> str:
        if args[:1] == ("show-environment",) and timeouts[0]:
            timeouts[0] -= 1
            time.sleep(0.4)  # longer than the old cap, as 30 s was longer than 10
            raise TmuxTimedOut("tmux did not answer within 30s: show-environment …")
        return run_tmux(self, *args, stdin=stdin)

    async def drive() -> str:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane) and _client(pane).answered, 5.0)
            monkeypatch.setattr(ControlClient, "send_literal", send_literal)
            monkeypatch.setattr(TmuxServer, "run", frozen)
            for char in "abcdefghij":
                pane.post_message(events.Key(char, char))
            await wait_until(pilot, lambda: len(pane._inflight) == 10, 2.0)
            pid = _client(pane).pid
            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            await wait_until(pilot, lambda: pane._held is not None, 2.0)
            for char in "KLMNO":
                pane.post_message(events.Key(char, char))
            return await _settled(pilot, record, len("abcdefghijKLMNO"), 6.0)

    assert run(drive()) == "abcdefghijKLMNO"
    assert timeouts == [0]


@_needs_tmux
def test_real_tmux_quitting_with_the_server_frozen_returns_at_once(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Unsetting the stamps at the client's close was one polled call per stamped pane,
    on the UI thread: with the server frozen, 30 s each — quitting with two stamped panes
    took 60 s, where it took none before (re-review of #272). The unsets go down the pipe
    and stdin closes; tmux runs them when it can, and nothing waits for it."""
    if sys.platform == "win32":
        pytest.skip("no SIGSTOP on Windows")
    first = _window(live, "asq-freeze", tmp_path, ["cat"])
    second = _window(live, "asq-freeze", tmp_path, ["cat"])
    server_pid = int(live.run("display-message", "-p", "#{pid}").strip())

    stopped: list[float] = []

    async def drive() -> None:
        host = Host(live, first)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            for shown in (first, second):  # two panes stamped
                if shown != pane.pane_id:
                    pane.attach(shown)
                await wait_until(pilot, lambda: _serving(pane) and pane.facts is not None, 5.0)
                await pilot.press("s")
                await wait_until(pilot, _row_shows(pane, "s"), timeout=2.0)
                await wait_until(pilot, lambda: not pane._inflight, timeout=2.0)
            os.kill(server_pid, signal.SIGSTOP)  # frozen as the app quits
            stopped.append(time.monotonic())

    try:
        run(drive())
        took = time.monotonic() - stopped[0]
    finally:
        os.kill(server_pid, signal.SIGCONT)
    deadline = time.monotonic() + 3
    while _stamps(live, "asq-freeze", os.getpid()) and time.monotonic() < deadline:
        time.sleep(0.05)
    print(f"\nquit with the server frozen: the app ended in {took:.2f} s")
    assert took < 1.0, took
    assert _stamps(live, "asq-freeze", os.getpid()) == []  # ...and tmux ran the unsets after


def _stamps(live: TmuxServer, session: str, pid: int) -> list[str]:
    """This process's stamp variables in ``session``: the hidden ones, ``ASQ_SENT_<pid>_*``."""
    out = live.run("show-environment", "-h", "-t", f"={session}")
    return [line for line in out.splitlines() if line.startswith(f"ASQ_SENT_{pid}_")]


@_needs_tmux
def test_real_tmux_this_processes_stamps_are_unset_when_its_client_closes(
    live: TmuxServer, tmp_path: Path
) -> None:
    """Each input sets a stamp variable named by asq's pid, and they piled up: one set per
    pane per asq launch, for the life of the session (review of #272, follow-up). The
    client unsets this process's own at a deliberate close — the app's end included —
    and leaves another process's alone."""
    pane_id = _window(live, "asq-tidy", tmp_path, ["cat"])
    live.run("set-environment", "-h", "-t", pane_id, "ASQ_SENT_4000000_0", "7")  # another asq's

    async def drive() -> list[str]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            await pilot.press("t", "i", "d", "y")
            await wait_until(pilot, _row_shows(pane, "tidy"), timeout=2.0)
            await pilot.pause(0.2)
            return _stamps(live, "asq-tidy", os.getpid())

    while_typing = run(drive())  # the app ends: its panes unmount, the last closes the client
    assert len(while_typing) == 1, while_typing  # it did stamp, so the test tests something
    deadline = time.monotonic() + 1  # down the pipe: tmux runs the unsets as the client ends
    while _stamps(live, "asq-tidy", os.getpid()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _stamps(live, "asq-tidy", os.getpid()) == []
    assert _stamps(live, "asq-tidy", 4_000_000) == ["ASQ_SENT_4000000_0=7"]


@_needs_tmux
def test_real_tmux_a_deliberate_close_unsets_the_stamps_nothing_waits_to_read(
    live: TmuxServer, tmp_path: Path
) -> None:
    """A control command failed and the pane let its only client go, with nothing in
    flight: the stamps it set go with it, there and then. (A pane with input in flight
    keeps its stamp until the recovery has read it; the app's end tidies it then.)"""
    pane_id = _window(live, "asq-closing", tmp_path, ["cat"])

    async def drive() -> tuple[list[str], list[str]]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            await pilot.press("o", "k")
            await wait_until(pilot, _row_shows(pane, "ok"), timeout=2.0)
            await wait_until(pilot, lambda: not pane._inflight, timeout=2.0)
            stamped = _stamps(live, "asq-closing", os.getpid())
            pane._control_failed("a command failed")  # the pane lets its only client go
            deadline = time.monotonic() + 1  # down the pipe, run as the client ends
            while _stamps(live, "asq-closing", os.getpid()) and time.monotonic() < deadline:
                await pilot.pause(0.02)
            return stamped, _stamps(live, "asq-closing", os.getpid())

    stamped, after = run(drive())
    assert len(stamped) == 1, stamped
    assert after == [], after


# --- the protocol, line by line ----------------------------------------------------------


def _detached(server: TmuxServer) -> tuple[ControlClient, list[object]]:
    """A client with no process behind it, driven by feeding it tmux's lines."""
    delivered: list[object] = []

    def deliver(call: Callable[[], object]) -> None:
        delivered.append(call)
        call()

    client = ControlClient(
        server, deliver=deliver, on_output=delivered.append, on_exit=delivered.append
    )
    client._alive = True
    return client, delivered


def test_quote_leaves_plain_tokens_and_spells_the_rest_as_one_token() -> None:
    assert quote("send-keys") == "send-keys"
    assert quote("%12") == "%12"
    assert quote("-t") == "-t"
    assert quote("=asq-quiet-lynx") == "=asq-quiet-lynx"
    # A token that starts with % and is not a pane id is a parser directive.
    assert quote("%12:pause") == '"%12:pause"'
    assert quote("a;") == '"a;"'
    assert quote('say "$X"\\') == '"say \\"\\$X\\"\\\\"'
    assert quote("two\nlines\t") == '"two\\012lines\\011"'
    assert quote("") == '""'
    with pytest.raises(TmuxError):
        quote("nul\x00")
    # tmux reads a ~ opening a quoted string as a home directory: every one is octal.
    assert quote("~/x") == '"\\176/x"'
    assert quote("~") == '"\\176"'
    assert quote("a~b") == '"a\\176b"'


class _Answering(TmuxServer):
    """A server whose every command is answered ``answer`` — a string, or a raised error."""

    def __init__(self, answer: str | TmuxError) -> None:
        super().__init__("asq-test-stamp", control=False)
        self.answer = answer

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        if isinstance(self.answer, TmuxError):
            raise self.answer
        return self.answer


def test_a_stamp_read_tells_a_gone_pane_from_an_answer_it_could_not_get() -> None:
    """``None`` is tmux saying the pane (or its whole server) is gone: nothing to type
    into. A timeout, or any error that is not tmux's answer about the pane, is
    :class:`~aisquare.core.tmux_control.StampUnknown` — what ran is not known, and the
    recovery asks again (tester of #272: it was ``None`` too, and the input dropped)."""
    name = tmux_control.stamp_name("%5")
    assert tmux_control.sent_stamp(_Answering(f"{name}=17\n"), "%5") == 17
    assert tmux_control.sent_stamp(_Answering(TmuxError(f"unknown variable: {name}")), "%5") == 0
    assert tmux_control.sent_stamp(_Answering(TmuxError("no such session: %5")), "%5") is None
    gone = TmuxError("no server running on /tmp/tmux-501/asq")
    assert tmux_control.sent_stamp(_Answering(gone), "%5") is None
    for unknown in (
        TmuxTimedOut("tmux did not answer within 30s: show-environment …"),
        TmuxError("could not run tmux: [Errno 24] Too many open files"),
    ):
        with pytest.raises(tmux_control.StampUnknown):
            tmux_control.sent_stamp(_Answering(unknown), "%5")


def test_an_empty_batch_sends_nothing_rather_than_the_line_that_detaches(tmp_path: Path) -> None:
    """An empty line detaches a control client; a batch of nothing is no line at all."""
    client, _ = _detached(FakeTmux().server(tmp_path))
    client._pipe_send([])
    client._pipe_send([], chain=True)
    assert client._outbox.empty()
    assert not client._pending


def test_a_pane_two_widgets_show_stays_wanted_until_both_have_left(tmp_path: Path) -> None:
    """The manager's pane is shown by the Manager tab and by the manager's own agent view.
    Muted by pane id, one widget's Hide arriving after the other's Show paused the pane the
    second still showed (review of #272): it is wanted while ANY widget shows it."""
    client, _ = _detached(FakeTmux().server(tmp_path))
    client._followed = "%M"  # followed already: a watch is a continue, not a new follow
    client.watch_pane("%M")  # the manager's agent view, shown
    client.watch_pane("%M")  # the Manager tab, shown before the agent view's Hide
    client.unwatch_pane("%M")  # …which arrives now
    sent = []
    while not client._outbox.empty():
        data = client._outbox.get()
        assert data is not None
        sent.append(data)
    assert "%M" in client._wanted
    assert b'refresh-client -A "%M:pause"\n' not in sent, sent
    client.unwatch_pane("%M")  # the second widget leaves too
    assert "%M" not in client._wanted
    assert client._outbox.get() == b'refresh-client -A "%M:pause"\n'


def test_an_answer_is_matched_by_its_whole_end_line_and_only_ours_are_answers(
    tmp_path: Path,
) -> None:
    """A ``%begin`` block ends at the ``%end`` (or ``%error``) that repeats its time,
    number and flags — a pane row that happens to read ``%end …`` is a row of the
    answer — and only a block with flags 1 answers a command of this client."""
    client, _ = _detached(FakeTmux().server(tmp_path))
    answers: list[Reply] = []
    client.command("capture-pane", done=answers.append)
    client.command("no-such-command", done=answers.append)
    for line in [
        b"%begin 100 7 0",  # the attach-session itself: nobody's answer
        b"%end 100 7 0",
        b"%begin 100 8 1",
        b"row one",
        b"%end 100 9 1",  # not this block's end
        b"%end 100 8 1",
        b"%begin 100 10 1",
        b"parse error: unknown command: no-such-command",
        b"%error 100 10 1",
    ]:
        client._heard(line)
    assert answers == [
        Reply(True, ["row one", "%end 100 9 1"]),
        Reply(False, ["parse error: unknown command: no-such-command"]),
    ]


def test_a_chain_that_fails_early_is_answered_once_and_its_rest_with_that_failure(
    tmp_path: Path,
) -> None:
    """``capture-pane ; display-message`` on one line: when the capture fails tmux runs
    and answers nothing after it, so the display-message is answered with the capture's
    failure — and the answer after the chain goes to the command after the chain."""
    client, _ = _detached(FakeTmux().server(tmp_path))
    answers: list[tuple[str, Reply]] = []
    client._pipe_send(
        [
            (["capture-pane", "-t", "%7"], lambda r: answers.append(("capture", r)), False),
            (["display-message", "-t", "%7"], lambda r: answers.append(("display", r)), False),
        ],
        chain=True,
    )
    client.command("display-message", "-p", "next", done=lambda r: answers.append(("next", r)))
    data = client._outbox.get()
    assert data == b"capture-pane -t %7 ; display-message -t %7\n"
    for line in [
        b"%begin 5 1 1",
        b"can't find pane: %7",
        b"%error 5 1 1",
        b"%begin 5 2 1",
        b"next",
        b"%end 5 2 1",
    ]:
        client._heard(line)
    gone = Reply(False, ["can't find pane: %7"])
    assert answers == [("capture", gone), ("display", gone), ("next", Reply(True, ["next"]))]


def test_output_wakes_once_however_much_arrives_and_pauses_a_pane_nobody_shows(
    tmp_path: Path,
) -> None:
    client, delivered = _detached(FakeTmux().server(tmp_path))
    client._wanted = ["%1"]
    woken: list[set[str]] = []
    client._on_output = woken.append
    client._deliver = delivered.append  # held, as the UI's loop holds it until its turn
    for _ in range(3):
        client._heard(b"%output %1 hello\\015\\012")
    client._heard(b"%output %9 from a window spawned since")
    assert len(delivered) == 1  # one wake for every output since the last
    queued: list[bytes] = []
    while not client._outbox.empty():
        data = client._outbox.get()
        assert data is not None
        queued.append(data)
    assert queued == [b'refresh-client -A "%9:pause"\n']
    wake = delivered[0]
    assert callable(wake)
    wake()
    assert woken == [{"%1"}]
    assert client.outputs == {"%1": 3, "%9": 1}


def test_an_ended_client_answers_what_waits_and_says_why(tmp_path: Path) -> None:
    client, delivered = _detached(FakeTmux().server(tmp_path))
    answers: list[Reply] = []
    client.command("display-message", done=answers.append)
    process = subprocess.Popen([sys.executable, "-c", "pass"], stderr=subprocess.PIPE)
    client._heard(b"%exit server exited")
    client._client_ended(process)
    assert answers == [Reply(False, ["server exited"])]
    assert "server exited" in delivered
    assert not client.alive
    with pytest.raises(TmuxError):
        client.command("display-message")


def test_the_control_client_is_started_without_the_tracing_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The seam strips the identity, as ``_tmux`` does: ``attach-session`` can start the
    server, which hands its environment to every window."""
    from aisquare.core.spawn import IDENTITY_ENV_VARS

    seen: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs, argv=argv)
        raise OSError("not started")

    name = next(iter(IDENTITY_ENV_VARS))
    monkeypatch.setenv(name, "inherited")
    monkeypatch.setattr(subprocess, "Popen", popen)  # what ``_spawn_client`` calls
    with pytest.raises(TmuxError):
        tmux_control._spawn_client(["tmux", "-C", "attach-session"])
    assert name not in seen["env"]
    assert seen["start_new_session"] is True


# --- which panes try the control client ---------------------------------------------------


def test_a_server_too_old_or_a_fake_runner_never_starts_a_control_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake runner plays tmux for the commands it is handed and has no ``tmux -C``
    behind it: a pane on it polls without trying. Nor does a pane on a server older
    than 3.2, whatever its handle allows."""
    started: list[list[str]] = []

    def spawn(argv: list[str]) -> Any:
        started.append(argv)
        raise TmuxError("not in this test")

    monkeypatch.setattr(tmux_control, "_spawn_client", spawn)
    for version, control in (("tmux 3.7c", None), ("tmux 3.1c", True)):
        fake = FakeTmux()
        fake.version = version
        fake.panes["%1"] = FakePane(screen=["hello"])
        server = TmuxServer(
            "fake", binary=sys.executable, conf=tmp_path / "fake.conf", runner=fake, control=control
        )

        async def drive(server: TmuxServer = server) -> bool:
            host = Host(server, "%1")
            async with host.run_test(size=(40, 6)) as pilot:
                await wait_until(pilot, lambda: host.pane.facts is not None)
                await pilot.pause(0.2)
                return host.pane._link is None

        assert run(drive())
    assert started == []


def test_a_control_client_that_will_not_start_leaves_the_pane_polling_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []

    def spawn(argv: list[str]) -> Any:
        started.append(argv)
        raise TmuxError("refused")

    monkeypatch.setattr(tmux_control, "_spawn_client", spawn)
    fake = FakeTmux()
    fake.panes["%1"] = FakePane(screen=["hello"])
    server = TmuxServer(
        "fake", binary=sys.executable, conf=tmp_path / "fake.conf", runner=fake, control=True
    )

    async def drive() -> tuple[bool, list[str]]:
        host = Host(server, "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            await wait_until(pilot, lambda: host.pane.facts is not None)
            await pilot.pause(1.2)  # two idle ticks, at least
            return host.pane._link is None, screen_text(host.pane)

    polling, shown = run(drive())
    assert polling
    assert shown[0] == "hello"
    assert len(started) == 1, started  # once, then the backoff
    # By session, never by pane: a pane target moves the session's current window.
    assert started[0][-5:] == ["attach-session", "-t", "$0", "-f", tmux_control.CLIENT_FLAGS]
    assert tmux_control.CLIENT_FLAGS == "ignore-size,no-output,no-detach-on-destroy"


# --- the polling fallback (acceptance 7) --------------------------------------------------


def test_polling_continuous_input_does_not_postpone_the_next_frame(
    tmp_path: Path,
) -> None:
    """A key every 15 ms for 600 ms: frames keep coming at the fast cadence while the
    keys do. Each key used to stop the frame timer and arm a new one 50 ms out, so
    none came until the typing stopped."""
    fake = FakeTmux()
    fake.panes["%1"] = FakePane(screen=["$ "])

    async def drive() -> int:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(0.6)
            captures = len(fake.captures)
            start = time.monotonic()
            while time.monotonic() - start < 0.6:
                pane.post_message(events.Key("x", "x"))
                await asyncio.sleep(0.015)
            return len(fake.captures) - captures

    during = run(drive())
    assert during >= 6, during  # ~12 at 50 ms; 0 or 1 when every key re-armed the timer


def test_polling_an_echo_slower_than_one_tick_shows_without_the_idle_wait(
    tmp_path: Path,
) -> None:
    """An echo that lands 80 ms after its key: the frame after it shows it, not one
    500 ms later. The tick at 50 ms finds nothing new, and used to back off to the
    idle interval there."""
    fake = FakeTmux()
    fake.panes["%1"] = FakePane(screen=["$ "])
    pressed: list[float] = []

    def echo(pane: FakePane) -> None:
        if pressed and time.monotonic() - pressed[0] >= 0.08:
            pane.screen = ["$ x"]

    fake.before_capture = echo

    async def drive() -> float:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: pane.facts is not None)
            await pilot.pause(1.2)  # long idle: the loop is at its idle interval
            pressed.append(time.monotonic())
            await pilot.press("x")
            await wait_until(pilot, lambda: screen_text(pane)[0] == "$ x", timeout=2.0)
            return time.monotonic() - pressed[0]

    took = run(drive())
    print(f"\npolling, an 80 ms echo: on screen {took * 1000:.0f} ms after its key")
    assert took < 0.25, took
