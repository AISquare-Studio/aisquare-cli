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
    pane_id = _window(live, "asq-attach", tmp_path, ["cat"])
    session = "asq-attach"

    async def drive() -> tuple[int, bool, bool]:
        host = Host(live, pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: _serving(pane), timeout=5.0)
            client = _client(pane)
            import pty  # Unix only, like everything this test drives

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
    client.start(live.run("list-panes", "-a", "-F", "#{pane_id}").split()[0])
    try:
        client.command("set-buffer", "-b", "asq-quote-probe", "--", weird, done=answers.append)
        deadline = time.monotonic() + 3
        while not answers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert answers and answers[0].ok, answers
        assert live.run("show-buffer", "-b", "asq-quote-probe") == weird
    finally:
        client.close()


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
    client._send(
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
    client._ended(process)
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
    monkeypatch.setattr(subprocess, "Popen", popen)  # what ``_spawn`` calls
    with pytest.raises(TmuxError):
        tmux_control._spawn(["tmux", "-C", "attach-session"])
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

    monkeypatch.setattr(tmux_control, "_spawn", spawn)
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

    monkeypatch.setattr(tmux_control, "_spawn", spawn)
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
    assert started[0][-5:] == ["attach-session", "-t", "%1", "-f", "ignore-size,no-output"]


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
