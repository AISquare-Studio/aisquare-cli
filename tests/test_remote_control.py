"""The Remote modal's model without Textual: the ngrok tunnel and the controller.

PLAN §4-H names the executable proxies for the human-only "ngrok present" check:
the URL builder (``build_public_url(host, token)`` is the modal's link text) and
the missing-binary message. The subprocess lifecycle runs against a FAKE ngrok
(a Python script that prints the JSON log lines ngrok prints) because the real
binary is absent here and the real tunnel is Rabia's QA. The controller's
branches — on/off, restore after a restart, auto-off, write actions default OFF
— are plain functions over a fake server module and an isolated home.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from aisquare.cli import remote as remote_cli
from aisquare.cli.app import app as cli
from aisquare.cli.ui import remote_control
from aisquare.cli.ui.remote_control import (
    READ_ONLY_REASON,
    RemoteController,
    RemoteState,
    load_remote_state,
)
from aisquare.cli.watch import _load_saved_theme
from aisquare.core import paths
from aisquare.core.locking import lock_exclusive, unlock
from aisquare.core.state_file import read_state, update_state
from aisquare.services import ngrok_tunnel, remote_server
from aisquare.services.ngrok_tunnel import (
    API_ON,
    AUTHTOKEN_HINT,
    INSTALL_HINT,
    NgrokTunnel,
    api_off_configs,
    build_public_url,
    missing_binary_message,
    ngrok_command,
    ngrok_default_config,
    parse_log_line,
)

STARTED = {"lvl": "info", "msg": "started tunnel", "url": "https://abcd-12.ngrok-free.app"}
OURS = {
    "lvl": "info",
    "msg": "started tunnel",
    "obj": "tunnels",
    "name": "command_line",
    "addr": "http://localhost:8750",
    "url": "https://owner-1234.ngrok-free.app",
}
"""The line ngrok logs for the tunnel ``ngrok http 8750`` asked for, as ngrok v3 logs it."""
WEB_SERVICE = {"lvl": "info", "msg": "starting web service", "obj": "web", "addr": "127.0.0.1:4040"}
"""The line ngrok logs as it starts its local web interface and agent API."""
PORT_ENV = remote_control.PORT_ENV
AUTO_OFF_ENV = "AISQUARE_REMOTE_AUTO_OFF"

# --- the URL builder: the modal's link text ---------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "abcd-12.ngrok-free.app",
        "https://abcd-12.ngrok-free.app",
        "https://abcd-12.ngrok-free.app/",
        "  http://abcd-12.ngrok-free.app  ",
    ],
)
def test_build_public_url_is_one_canonical_form_for_every_host_spelling(host: str) -> None:
    assert build_public_url(host, "tok_123") == "https://abcd-12.ngrok-free.app/r/tok_123/"


def test_the_link_the_modal_shows_is_the_url_builder_verbatim(tmp_path: Path) -> None:
    """PLAN §4-H: ``build_public_url(host, token) == modal text`` — through the controller."""
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url=STARTED["url"]), url_timeout=2
    )
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(timeout=5)
    assert controller.link_url() == build_public_url(STARTED["url"], server.token)
    assert controller.link_url() == f"https://abcd-12.ngrok-free.app/r/{server.token}/"
    assert controller.message is None


# --- the log parser -----------------------------------------------------------------------


def test_parse_log_line_reads_the_started_tunnel_url_and_ignores_noise() -> None:
    assert parse_log_line(json.dumps(STARTED)).url == STARTED["url"]
    assert parse_log_line(json.dumps({"lvl": "info", "msg": "client session established"})) == (
        ngrok_tunnel.LogEvent()
    )
    # A line that is no JSON is no URL and no error: only kept, for an exit with no tunnel.
    assert parse_log_line("not json at all") == ngrok_tunnel.LogEvent(plain="not json at all")
    assert parse_log_line("ERROR:  bad\n") == ngrok_tunnel.LogEvent(plain="bad")
    assert parse_log_line("\n") == ngrok_tunnel.LogEvent()
    assert parse_log_line("[1, 2, 3]") == ngrok_tunnel.LogEvent()


def test_parse_log_line_reads_which_tunnel_started_and_where_the_api_listens() -> None:
    assert parse_log_line(json.dumps(OURS)) == ngrok_tunnel.LogEvent(
        url=OURS["url"], name="command_line", addr="http://localhost:8750"
    )
    assert parse_log_line(json.dumps(WEB_SERVICE)) == ngrok_tunnel.LogEvent(web="127.0.0.1:4040")


def test_ngroks_log_reaches_the_status_line_with_nothing_that_drives_a_terminal() -> None:
    """ngrok's log is in part the agent API's caller's to fill: a tunnel's name, an error about
    it. The status line painted it as it came, and Textual hands an ESC in a sentence to the
    terminal the fleet UI runs in as it is (sweep of #243). It goes as the house's one policy
    for outside bytes has it (``core.injection.sanitise_text``), on one line."""
    escape = "\x1b]0;owned\x07\x1b[2J"
    error = parse_log_line(json.dumps({"lvl": "eror", "err": f"no tunnel {escape}x\n\u2028y"}))
    assert error.error == "no tunnel ]0;owned[2Jx y"
    assert parse_log_line(f"ERROR:  bad {escape}\n").plain == "bad ]0;owned[2J"
    foreign = parse_log_line(json.dumps({**OURS, "name": f"x{escape}", "addr": f"{escape}:9"}))
    assert (foreign.name, foreign.addr) == ("x]0;owned[2J", "]0;owned[2J:9")
    assert parse_log_line(json.dumps({**OURS, "name": "\x1b"})).name is None
    tunnel = NgrokTunnel(8750, command=[sys.executable, "-c", "import time; time.sleep(60)"])
    assert tunnel.start_tunnel() is None
    try:
        tunnel.handle_line(json.dumps({**OURS, "name": f"x{escape}\nmore"}))
        warning = tunnel.api_warning
        assert warning is not None and "\x1b" not in warning and "\n" not in warning
        assert "(x]0;owned[2J more → http://localhost:8750)" in warning
        assert tunnel.public_url is None, "not this Remote's tunnel"
    finally:
        tunnel.stop_tunnel()
    assert tunnel.api_warning is None, "said while ngrok is up, and its API with it"


@pytest.mark.parametrize(
    ("addr", "ours"),
    [
        ("http://localhost:8750", True),
        ("localhost:8750", True),
        ("8750", True),
        ("http://127.0.0.1:8750", True),
        ("http://[::1]:8750", True),
        ("http://localhost:9999", False),
        ("https://example.com:8750", False),
        ("http://localhost", False),
        ("not an address", False),
    ],
)
def test_a_tunnel_forwards_to_this_port_only_on_this_machines_loopback(
    addr: str, ours: bool
) -> None:
    assert ngrok_tunnel.forwards_to(addr, 8750) is ours


def test_parse_log_line_turns_an_authtoken_error_into_the_authtoken_hint() -> None:
    line = json.dumps(
        {"lvl": "eror", "msg": "failed", "err": "authentication failed: ERR_NGROK_4018"}
    )
    assert parse_log_line(line).error == AUTHTOKEN_HINT
    other = json.dumps({"lvl": "eror", "err": "bind: address already in use"})
    assert parse_log_line(other).error == "bind: address already in use"


def test_ngrok_command_is_the_documented_one() -> None:
    """``--inspect=false``: ngrok's inspector keeps every request and answer, the unlock's
    passphrase and every cookie included, on a local web interface that asks for nothing."""
    assert ngrok_command(8750) == [
        "ngrok", "http", "8750", "--log=stdout", "--log-format=json", "--inspect=false"
    ]  # fmt: skip


# --- the missing binary -------------------------------------------------------------------


def test_missing_binary_message_names_the_install_and_the_authtoken() -> None:
    message = missing_binary_message()
    assert message == INSTALL_HINT
    assert "ngrok is not installed" in message
    assert "https://ngrok.com/download" in message
    assert "ngrok config add-authtoken" in message


def test_a_tunnel_without_the_binary_reports_instead_of_raising() -> None:
    tunnel = NgrokTunnel(8750, which=lambda _name: None)
    assert tunnel.start_tunnel() == INSTALL_HINT
    assert tunnel.error == INSTALL_HINT
    assert not tunnel.running
    tunnel.stop_tunnel()  # idempotent with nothing spawned


# --- the subprocess lifecycle against a fake ngrok ----------------------------------------


def fake_ngrok(tmp_path: Path, *lines: dict[str, Any] | float, linger: bool = True) -> list[str]:
    """A command that prints ``lines`` as ngrok's JSON log would, then (optionally) stays up.

    A number among them is a pause of that many seconds: ngrok retrying a session it could
    not open yet, before it announces its tunnel.
    """
    script = tmp_path / "fake-ngrok.py"
    lines_out = [
        f"time.sleep({line})"
        if isinstance(line, float)
        else f"print({json.dumps(json.dumps(line))}, flush=True)"
        for line in lines
    ]
    if linger:
        lines_out.append("time.sleep(60)")
    script.write_text("import sys, time\n" + "\n".join(lines_out) + "\n")
    return [sys.executable, str(script)]


def test_the_tunnel_learns_its_url_from_the_log_and_stop_ends_the_process(
    tmp_path: Path,
) -> None:
    tunnel = NgrokTunnel(8750, command=fake_ngrok(tmp_path, {"lvl": "info", "msg": "hi"}, STARTED))
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(timeout=10) == STARTED["url"]
    assert tunnel.running
    tunnel.stop_tunnel()
    assert not tunnel.running
    assert tunnel.error is None


def test_a_tunnel_that_exits_without_a_url_says_so_instead_of_hanging(tmp_path: Path) -> None:
    """The exit wakes whoever waits for the URL: a first run without an authtoken showed
    "starting ngrok…" for the whole wait before the hint, had the reader not said so."""
    error = {"lvl": "eror", "err": "authentication failed: ERR_NGROK_4018"}
    tunnel = NgrokTunnel(8750, command=fake_ngrok(tmp_path, error, linger=False))
    assert tunnel.start_tunnel() is None
    started = time.monotonic()
    assert tunnel.wait_for_url(timeout=10) is None
    assert time.monotonic() - started < 5, "the exit was heard of only when the wait ran out"
    assert tunnel.error == AUTHTOKEN_HINT
    tunnel.stop_tunnel()


class StubbornNgrok:
    """A process that ignores SIGTERM: ``wait`` runs out until it is killed. Each call the
    tunnel makes is in ``calls``; its log is empty."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stdout = io.StringIO("")
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait {timeout}")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ngrok", timeout or 0)
        return self.returncode


def test_an_ngrok_that_cannot_be_spawned_is_a_sentence_and_remote_stays_local() -> None:
    """A binary on the PATH that will not run (no execute bit, the wrong architecture):
    ``start_tunnel`` says so, and Remote is on locally with that on its status line, never
    an ``OSError`` out of the switch's handler."""

    def refused(command: list[str], **kwargs: object) -> subprocess.Popen[str]:
        raise PermissionError(13, "Permission denied", command[0])

    tunnels: list[NgrokTunnel] = []

    def factory(port: int) -> NgrokTunnel:
        tunnels.append(NgrokTunnel(port, which=lambda _name: "/usr/bin/ngrok", popen=refused))
        return tunnels[-1]

    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=factory)
    controller.turn_on()
    sentence = "could not start ngrok: [Errno 13] Permission denied: 'ngrok'"
    assert controller.running and controller.message == sentence
    assert controller.link_url() == f"http://127.0.0.1:8750/r/{server.token}/"
    assert tunnels[0].error == sentence and not tunnels[0].running


def test_an_ngrok_that_ignores_the_terminate_is_killed() -> None:
    """Stopping waits 5 s for ngrok to end, then kills it: an ngrok left running would hold
    the static domain, and the next Remote's tunnel could not have it (ERR_NGROK_334)."""
    process = StubbornNgrok()
    tunnel = NgrokTunnel(
        8750,
        which=lambda _name: "/usr/bin/ngrok",
        popen=lambda command, **kwargs: cast("subprocess.Popen[str]", process),
    )
    assert tunnel.start_tunnel() is None
    tunnel.wait_for_url(5)  # its empty log ends at once, and the reader asks for the exit code
    tunnel.stop_tunnel()
    reader_asked = f"wait {ngrok_tunnel.EXIT_CODE_WAIT_SECONDS}"
    waited = f"wait {ngrok_tunnel.STOP_SECONDS}"
    assert process.calls == [reader_asked, "terminate", waited, "kill", waited]
    assert ngrok_tunnel.STOP_SECONDS == 5
    assert not tunnel.running


def test_a_url_its_listener_could_not_take_never_ends_the_log_reader(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reader is what drains ngrok's log: a listener that raised on its thread ended it,
    and an ngrok whose log nobody reads stalls once the pipe is full."""
    tunnel = NgrokTunnel(8750, which=lambda _name: None)
    heard: list[str] = []

    def refuse(url: str) -> None:
        heard.append(url)
        raise RuntimeError("the controller is gone")

    tunnel.on_announce = refuse
    with caplog.at_level("WARNING", logger=ngrok_tunnel.__name__):
        tunnel.handle_line(json.dumps(STARTED))
    assert heard == [STARTED["url"]] and tunnel.public_url == STARTED["url"]
    assert "the announced URL could not be taken" in caplog.text


def ngrok_printing(tmp_path: Path, body: str) -> list[str]:
    """A command running ``body``, a Python script standing in for ngrok, or for whatever
    runs in its place (``sys`` and ``time`` imported)."""
    script = tmp_path / "printing-ngrok.py"
    script.write_text("import sys, time\n" + body)
    return [sys.executable, str(script)]


def test_ngroks_log_is_read_as_utf8_whatever_the_locale(tmp_path: Path) -> None:
    """ngrok writes UTF-8. Decoded with the locale's codec, a byte that codec could not take
    (``C:\\Users\\Иван`` on cp1251, a Latin-1 path under UTF-8) raised out of the log reader,
    which took it for its pipe closed and ended without a word: the panel waited out its
    15 s and said ngrok never announced a tunnel, while ngrok ran on with nobody draining
    its log (sweep of #243)."""
    seen: dict[str, object] = {}

    def recording(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        seen.update(kwargs)
        return subprocess.Popen(command, **kwargs)

    body = (
        "sys.stdout.buffer.write(b'open config file at /home/\\xd0\\x98\\xff\\xfe/ngrok.yml\\n')\n"
        f"print({json.dumps(json.dumps(STARTED))}, flush=True)\n"
        "time.sleep(60)\n"
    )
    tunnel = NgrokTunnel(8750, command=ngrok_printing(tmp_path, body), popen=recording)
    assert tunnel.start_tunnel() is None
    try:
        assert tunnel.wait_for_url(timeout=5) == STARTED["url"]
        assert tunnel.error is None
    finally:
        tunnel.stop_tunnel()
    assert (seen["encoding"], seen["errors"]) == ("utf-8", "replace")


class UnreadableLog:
    """A log that raises as it is read: a line the reader cannot take, as a decode error was."""

    def __iter__(self) -> UnreadableLog:
        return self

    def __next__(self) -> str:
        raise ValueError("not a line")

    def close(self) -> None:
        return None


class UpNgrok(StubbornNgrok):
    """An ngrok that stays up, its log unreadable."""

    def __init__(self) -> None:
        super().__init__()
        self.stdout = UnreadableLog()  # type: ignore[assignment]


def test_a_log_the_reader_cannot_read_is_said_not_taken_for_a_closed_pipe() -> None:
    """Every ``ValueError`` out of the log was read as the pipe ``stop_tunnel`` closed: the
    reader ended without a word, the wait for the URL ran out, and the status line blamed
    ngrok for a tunnel it may well have announced (sweep of #243)."""
    process = UpNgrok()
    tunnel = NgrokTunnel(
        8750,
        which=lambda _name: "/usr/bin/ngrok",
        popen=lambda command, **kwargs: cast("subprocess.Popen[str]", process),
    )
    assert tunnel.start_tunnel() is None
    started = time.monotonic()
    assert tunnel.wait_for_url(timeout=10) is None
    assert time.monotonic() - started < 5, "the reader ended without waking the wait"
    assert tunnel.error == "ngrok's log could not be read: not a line"
    process.returncode = 0
    tunnel.stop_tunnel()


@pytest.mark.parametrize(
    ("printed", "said"),
    [
        (
            [
                "ERROR:  Error reading configuration file '/home/u/.config/ngrok/ngrok.yml': "
                "yaml: line 3: mapping values are not allowed in this context",
                "ERROR:  ",
                "ERROR:  ERR_NGROK_1001",
            ],
            "ngrok exited (code 1) before it announced a tunnel: Error reading configuration "
            "file '/home/u/.config/ngrok/ngrok.yml': yaml: line 3: mapping values are not "
            "allowed in this context",
        ),
        (
            [
                "mise ERROR No version is set for shim: ngrok",
                "Set a global default version with one of the following:",
            ],
            "ngrok exited (code 1) before it announced a tunnel: "
            "mise ERROR No version is set for shim: ngrok",
        ),
        (
            ["ERROR:  authentication failed: Usage of ngrok requires an authtoken."],
            AUTHTOKEN_HINT,
        ),
    ],
    ids=["a config ngrok cannot read", "a shim that cannot run it", "no authtoken"],
)
def test_what_ngrok_printed_before_its_json_log_is_why_it_exited(
    tmp_path: Path, printed: list[str], said: str
) -> None:
    """ngrok, a launcher or a version manager's shim prints in plain text what stops it
    before the JSON log starts, and every such line was dropped: the status line said only
    "ngrok exited (code 1)", or "(code None)" when it asked before the exit landed, and the
    watchdog leaves a first tunnel that never came up to that sentence (sweep of #243)."""
    body = "".join(f"print({line!r}, file=sys.stderr)\n" for line in printed) + "sys.exit(1)\n"
    tunnel = NgrokTunnel(8750, command=ngrok_printing(tmp_path, body))
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(timeout=10) is None
    assert tunnel.error == said
    tunnel.stop_tunnel()


def launched_ngrok(tmp_path: Path, launcher: str) -> tuple[list[str], Path]:
    """A command that runs a stand-in ngrok the way ``launcher`` does, and the file where the
    stand-in writes its pid: it announces :data:`STARTED`, then stays up, logging nothing.

    ``pyngrok`` runs it as its child, as pyngrok's ``ngrok`` console script does
    (``subprocess.call``); ``sh`` runs it from a wrapper script without ``exec``."""
    pid_file = tmp_path / "real-ngrok.pid"
    real = tmp_path / "real-ngrok.py"
    real.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"print({json.dumps(json.dumps(STARTED))}, flush=True)\n"
        "time.sleep(60)\n"
    )
    if launcher == "pyngrok":
        script = tmp_path / "pyngrok-launcher.py"
        script.write_text(
            f"import subprocess, sys\nsys.exit(subprocess.call([sys.executable, {str(real)!r}]))\n"
        )
        return [sys.executable, str(script)], pid_file
    wrapper = tmp_path / "ngrok-wrapper.sh"
    wrapper.write_text(f'#!/bin/sh\n"{sys.executable}" "{real}"\nstatus=$?\nexit $status\n')
    wrapper.chmod(0o755)
    return [str(wrapper)], pid_file


def pid_in(pid_file: Path) -> int:
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline, "the stand-in ngrok never started"
        time.sleep(0.02)
    return int(pid_file.read_text())


def gone(pid: int, seconds: float = 10.0) -> bool:
    """Whether process ``pid`` has ended within ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def finished(call: Callable[[], object], seconds: float = 15.0) -> bool:
    """Run ``call`` on a thread of its own; whether it returned within ``seconds``."""
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive()


@pytest.mark.parametrize("launcher", ["pyngrok", "sh"])
def test_stopping_an_ngrok_a_launcher_runs_stops_the_real_one_and_returns(
    tmp_path: Path, launcher: str
) -> None:
    """The ``ngrok`` on a PATH may run the real binary as its child: pyngrok's console script,
    a wrapper without ``exec``. Stopping signalled the launcher alone, so the real ngrok
    stayed up, holding the tunnel and the static domain, and closing the log's pipe then
    waited, for good, on the reader blocked in a read the real ngrok kept open: turning
    Remote off never ended, and quitting hung on it (sweep of #243)."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("process groups are POSIX")
    command, pid_file = launched_ngrok(tmp_path, launcher)
    tunnel = NgrokTunnel(8750, command=command)
    assert tunnel.start_tunnel() is None
    real = pid_in(pid_file)
    try:
        assert tunnel.wait_for_url(timeout=10) == STARTED["url"]
        assert finished(tunnel.stop_tunnel), "stopping hung on the log's pipe"
        assert gone(real), "the real ngrok outlived the launcher it ran under"
        assert not tunnel.running
    finally:
        with contextlib.suppress(OSError):
            os.kill(real, signal.SIGKILL)


def test_turning_off_a_remote_whose_ngrok_a_launcher_runs_ends_and_stops_it(
    tmp_path: Path,
) -> None:
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("process groups are POSIX")
    command, pid_file = launched_ngrok(tmp_path, "pyngrok")
    controller = RemoteController(
        server=fake_server(), tunnel_factory=lambda port: NgrokTunnel(port, command=command)
    )
    controller.turn_on()
    real = pid_in(pid_file)
    try:
        assert controller._waiter is not None
        controller._waiter.join(10)
        assert controller.public_url is not None
        controller.turn_off(wait=False)
        assert controller.wait_until_off(15), "still turning Remote off"
        assert gone(real), "the real ngrok kept the tunnel up"
        assert controller.message is None
    finally:
        with contextlib.suppress(OSError):
            os.kill(real, signal.SIGKILL)


def test_reviving_a_tunnel_whose_launcher_died_never_waits_on_textuals_thread(
    tmp_path: Path,
) -> None:
    """A launcher killed from outside leaves the real ngrok up, its log's pipe open: the
    watchdog, on Textual's thread, stopped the dead tunnel by closing that pipe, and the
    fleet UI froze for good. The dead one is stopped on a thread of its own, the real ngrok
    with it."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("process groups are POSIX")
    command, pid_file = launched_ngrok(tmp_path, "pyngrok")
    made: list[NgrokTunnel] = []

    def factory(port: int) -> NgrokTunnel:
        made.append(NgrokTunnel(port, command=command))
        return made[-1]

    controller = RemoteController(server=fake_server(), tunnel_factory=factory)
    controller.turn_on()
    first = pid_in(pid_file)
    pid_file.unlink()
    try:
        assert controller._waiter is not None
        controller._waiter.join(10)
        launcher = made[0]._process
        assert launcher is not None
        os.kill(launcher.pid, signal.SIGKILL)  # the launcher alone, from outside
        launcher.wait(5)
        assert finished(controller.revive_tunnel_if_dead, 2.0), "the watchdog froze the UI"
        assert controller.tunnel is made[1]
        assert gone(first), "the dead tunnel's real ngrok was left up"
    finally:
        with contextlib.suppress(OSError):
            os.kill(first, signal.SIGKILL)
        controller.turn_off()
        if pid_file.exists():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


# By name, resolved past the skip: Windows has no SIGHUP, and a parameter that was
# signal.SIGHUP failed the collection of this whole module there.
@pytest.mark.parametrize("name", ["SIGHUP", "SIGTERM"], ids=["hangup", "sigterm"])
def test_a_fleet_ui_ended_by_a_hangup_or_a_sigterm_ends_its_ngrok_first(
    tmp_path: Path, name: str
) -> None:
    """ngrok runs in a process group of its own, which a closed terminal's hangup does not
    reach: the fleet UI died of it and its ngrok ran on, holding the static domain, so the
    next start's ngrok could not have it. The fleet UI's ending signals end its ngrok, and
    then the UI as they always did."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("POSIX signals")
    signum = signal.Signals[name]
    command, pid_file = launched_ngrok(tmp_path, "pyngrok")
    ui = tmp_path / "fleet-ui.py"
    ui.write_text(
        "import signal, time\n"
        # As from a terminal: a suite run under nohup hands its children SIGHUP ignored,
        # which ngrok_ends_with leaves as it is, and the hangup then ends nothing.
        f"signal.signal(signal.{name}, signal.SIG_DFL)\n"
        "from aisquare.cli.ui.remote_control import ngrok_ends_with\n"
        "from aisquare.services.ngrok_tunnel import NgrokTunnel\n"
        f"tunnel = NgrokTunnel(8750, command={command!r})\n"
        "assert tunnel.start_tunnel() is None and tunnel.wait_for_url(10)\n"
        "with ngrok_ends_with():\n"
        "    print('ready', flush=True)\n"
        "    time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(ui)], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True
    )
    real = None
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        real = pid_in(pid_file)
        process.send_signal(signum)
        assert process.wait(10) == -signum, "the signal no longer ends the fleet UI"
        assert gone(real), "the fleet UI's ngrok outlived it"
    finally:
        process.kill()
        process.wait(5)
        if real is not None:
            with contextlib.suppress(OSError):
                os.kill(real, signal.SIGKILL)


def test_an_ngrok_that_ended_on_its_own_leaves_no_group_for_a_stop_to_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once ngrok has exited, reaped, with nothing left in its group, the group's number is
    free for any other program's group to take. A stop that came later (the watchdog's, or
    turning off a Remote whose first ngrok never came up, hours after) signalled that number
    all the same: SIGTERM, then SIGKILL, to whatever group had it by then."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("process groups are POSIX")
    signalled: list[tuple[int, int]] = []
    killpg = os.killpg

    def recording(group: int, signum: int) -> None:
        if signum:
            signalled.append((group, signum))
        killpg(group, signum)

    monkeypatch.setattr(os, "killpg", recording)
    tunnel = NgrokTunnel(8750, command=fake_ngrok(tmp_path, {"lvl": "info"}, linger=False))
    assert tunnel.start_tunnel() is None
    assert tunnel in ngrok_tunnel._LIVE
    assert tunnel.wait_for_url(timeout=10) is None
    assert tunnel._reader is not None
    tunnel._reader.join(10)
    assert tunnel not in ngrok_tunnel._LIVE, "nothing of it is left to signal as the UI ends"
    tunnel.stop_tunnel()
    assert signalled == [], "a group nothing was left in was signalled"


def test_every_ngrok_this_process_started_is_signalled_as_it_ends_not_only_a_remotes(
    tmp_path: Path,
) -> None:
    """A hangup's handler signalled the tunnel a Remote held and the one it was stopping: a dead
    one the watchdog was still stopping on a thread of its own, or one a start had spawned
    and not yet handed over, ran on, holding the static domain, once the fleet UI was gone."""
    if sys.platform == "win32":  # an `if`, not a skipif: mypy's platform check reads only this
        pytest.skip("process groups are POSIX")
    command, pid_file = launched_ngrok(tmp_path, "pyngrok")
    tunnel = NgrokTunnel(8750, command=command)  # held by no Remote
    assert tunnel.start_tunnel() is None
    real = pid_in(pid_file)
    try:
        assert tunnel.wait_for_url(timeout=10) == STARTED["url"]
        ngrok_tunnel.end_every_tunnel_now()
        assert gone(real), "an ngrok no Remote held outlived the UI"
    finally:
        with contextlib.suppress(OSError):
            os.kill(real, signal.SIGKILL)
        tunnel.stop_tunnel()
    assert tunnel not in ngrok_tunnel._LIVE


# --- ngrok's agent API (sweep of #243) -----------------------------------------------------


@pytest.mark.parametrize(
    "foreign",
    [
        {
            "name": "x",
            "addr": "http://localhost:9999",
            "url": "https://attacker-5678.ngrok-free.app",
        },
        {"name": "command_line", "addr": "http://localhost:9999", "url": "https://a-1.ngrok.app"},
        {"name": "y", "addr": "http://localhost:8750", "url": "https://inspected.ngrok-free.app"},
        {"name": "z", "addr": "https://example.com:443", "url": "https://example.com"},
    ],
    ids=["another port", "our name, another port", "our port, another name", "another host"],
)
def test_a_tunnel_ngroks_api_started_is_never_the_link_nor_where_a_push_leads(
    tmp_path: Path, foreign: dict[str, str]
) -> None:
    """ngrok's agent API on 127.0.0.1:4040 asks no one for a password: any user of the machine
    could start a tunnel in the panel's ngrok, which logged it as it logs its own, and the
    panel made it the link, the QR and where every notification leads. A tap then carried
    the token to that user's host, which asked for the passphrase (sweep of #243). Only the
    tunnel ``ngrok http`` asked for, to this port, is Remote's; the status line says another
    was started."""
    server = fake_server()
    command = fake_ngrok(tmp_path, OURS, 0.2, {**OURS, **foreign})
    controller = RemoteController(
        server=server, tunnel_factory=lambda port: NgrokTunnel(port, command=command)
    )
    controller.turn_on()
    try:
        assert controller._waiter is not None
        controller._waiter.join(10)
        link = build_public_url(OURS["url"], server.token)
        assert controller.link_url() == link
        tunnel = controller.tunnel
        assert tunnel is not None
        deadline = time.monotonic() + 10
        while tunnel.foreign is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert controller.link_url() == link, "another tunnel became the link"
        assert server.public_urls == [link], "and where a notification leads"
        assert tunnel.public_url == OURS["url"]
        assert "ngrok's local API started a tunnel that is not Remote's" in (
            controller.status_line()
        )
    finally:
        controller.turn_off()


def write_ngrok_config(tmp_path: Path, text: str) -> Path:
    """An ngrok config with ``text``, as the human's own, and its path. Handed to
    :func:`api_off_configs` as ``own``: where ngrok keeps its own depends on the platform
    the suite runs on (:func:`ngrok_default_config`), and an environment that points there
    on Linux pointed nowhere on Windows, nor on macOS, where the human's real one was read."""
    own = tmp_path / "ngrok" / "ngrok.yml"
    own.parent.mkdir(parents=True)
    own.write_text(text)
    return own


@pytest.mark.parametrize(
    ("version", "line", "api"),
    [
        ("2", 'version: "2"', "web_addr: false"),
        ("3", "version: 3", "agent:\n  web_addr: false"),
        ("3", "version: '3'  # upgraded", "agent:\n  web_addr: false"),
    ],
)
def test_ngroks_api_is_turned_off_in_a_config_merged_over_the_humans_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str, line: str, api: str
) -> None:
    """``web_addr: false`` turns ngrok's agent API off, and a flag cannot: it goes in a config
    of ours, of the version the human's own is, named after theirs (``--config`` replaces
    where ngrok looks), so their authtoken and reserved domain still hold."""
    own = write_ngrok_config(tmp_path, f"{line}\nauthtoken: tok_123\n")
    configs = api_off_configs(own=own)
    assert configs is not None
    first, ours = configs
    assert first == own
    assert ours == paths.aisquare_home() / f"remote-ngrok-v{version}.yml"
    assert ours.read_text().endswith(f'version: "{version}"\n{api}\n')
    assert api_off_configs(own=own) == configs, "written once, the same each start"
    command = ngrok_command(8750, configs=configs)
    assert command[-1] == f"--config={own},{ours}"
    monkeypatch.setattr(ngrok_tunnel, "ngrok_default_config", lambda **_: own)
    assert api_off_configs() == configs, "the human's own is where ngrok keeps it"


def test_without_a_config_of_the_humans_ngrok_starts_as_it_always_did(tmp_path: Path) -> None:
    """No config to keep and nothing to sign in with: ngrok says so itself, once. An authtoken
    in the environment signs it in with ours alone; a version not known here, or a path
    ``--config`` would split, keeps ngrok as it always was."""
    missing = tmp_path / "nowhere" / "ngrok.yml"
    assert api_off_configs(own=missing, environ={}) is None
    signed_in = api_off_configs(own=missing, environ={"NGROK_AUTHTOKEN": "tok_123"})
    assert signed_in == [paths.aisquare_home() / "remote-ngrok-v2.yml"]
    own = write_ngrok_config(tmp_path, "authtoken: tok_123\n")
    assert api_off_configs(own=own, environ={}) is None, "a config with no version"
    own = write_ngrok_config(tmp_path / "v1", 'version: "1"\n')
    assert api_off_configs(own=own, environ={}) is None
    own = write_ngrok_config(tmp_path / "a,b", 'version: "2"\n')
    assert api_off_configs(own=own, environ={}) is None, "--config splits on the comma"


@pytest.mark.parametrize(
    ("platform", "environ", "where"),
    [
        ("linux", {}, ".config/ngrok/ngrok.yml"),
        ("linux", {"XDG_CONFIG_HOME": "relative"}, ".config/ngrok/ngrok.yml"),
        ("darwin", {"XDG_CONFIG_HOME": "/xdg"}, "Library/Application Support/ngrok/ngrok.yml"),
        ("win32", {}, "AppData/Local/ngrok/ngrok.yml"),
    ],
)
def test_ngroks_own_config_is_where_ngroks_docs_place_it(
    tmp_path: Path, platform: str, environ: dict[str, str], where: str
) -> None:
    assert ngrok_default_config(platform=platform, environ=environ, home=tmp_path) == (
        tmp_path / where
    )
    xdg = tmp_path / "xdg"  # absolute on every platform the suite runs on, as "/xdg" is not
    assert ngrok_default_config(
        platform="linux", environ={"XDG_CONFIG_HOME": str(xdg)}, home=tmp_path
    ) == (xdg / "ngrok" / "ngrok.yml")
    assert (
        ngrok_default_config(
            platform="win32", environ={"LOCALAPPDATA": str(tmp_path / "local")}, home=tmp_path
        )
        == tmp_path / "local" / "ngrok" / "ngrok.yml"
    )


def test_a_config_of_ours_that_is_no_utf8_is_written_again_not_raised_into_the_ui(
    tmp_path: Path,
) -> None:
    """Our config was read as strict UTF-8, and a byte that is none raised a UnicodeDecodeError,
    no OSError, out of every start of ngrok: the watchdog's, on Textual's thread, ended the
    fleet UI (sweep of #243, as ngrok's log was)."""
    own = write_ngrok_config(tmp_path, 'version: "2"\nauthtoken: tok_123\n')
    paths.ensure_home()
    ours = paths.aisquare_home() / "remote-ngrok-v2.yml"
    ours.write_bytes(b"web_addr: \xff\n")
    assert api_off_configs(own=own) == [own, ours]
    assert ours.read_text(encoding="utf-8").endswith('version: "2"\nweb_addr: false\n')


def test_an_ngrok_whose_api_off_config_cannot_be_had_starts_as_before(tmp_path: Path) -> None:
    """Whatever finding the config raises is no reason to keep Remote without ngrok, nor to
    raise into the fleet UI, where the watchdog starts ngrok on Textual's thread."""

    def unhad() -> list[Path] | None:
        raise UnicodeError("unreadable")

    seen: list[list[str]] = []

    def recording(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        seen.append(command)
        return subprocess.Popen(command, **kwargs)

    tunnel = NgrokTunnel(
        8750,
        binary=sys.executable,
        which=lambda name: name,
        popen=recording,
        api_off=True,
        configs=unhad,
    )
    assert tunnel.start_tunnel() is None
    tunnel.stop_tunnel()
    assert seen == [ngrok_command(8750, sys.executable, url=tunnel.static_host)], "as before"


OUR_CONFIG = "remote-ngrok-v2.yml"
"""The file name of our config, for a human's own ngrok.yml of version 2."""


def ngrok_binary(
    tmp_path: Path, *, with_our_config: str | None = None
) -> tuple[Callable[..., subprocess.Popen[str]], list[list[str]]]:
    """A ``popen`` that runs a stand-in for the ngrok binary on the command it is handed, and
    every command it was handed. The stand-in serves its API, and says so, only when run
    without our config; run with it, it prints ``with_our_config``, if given, and exits 1,
    as an ngrok that will not go on does.

    The stand-in is a Python script this interpreter runs: an extensionless file with a
    shebang is no program on Windows (``tests/fakebin.py``), which ran nothing there."""
    script = tmp_path / "ngrok-binary.py"
    refuse = (
        f"    print({with_our_config!r}, file=sys.stderr)\n    sys.exit(1)\n"
        if with_our_config is not None
        else "    pass\n"
    )
    script.write_text(
        "import sys, time\n"
        "if any(arg.startswith('--config=') for arg in sys.argv):\n"
        f"{refuse}"
        "else:\n"
        f"    print({json.dumps(json.dumps(WEB_SERVICE))}, flush=True)\n"
        f"print({json.dumps(json.dumps(OURS))}, flush=True)\n"
        "time.sleep(60)\n"
    )
    runs: list[list[str]] = []

    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        runs.append(command)
        return subprocess.Popen([sys.executable, str(script), *command[1:]], **kwargs)

    return popen, runs


def panels_ngrok(port: int, own: Path, popen: Callable[..., subprocess.Popen[str]]) -> NgrokTunnel:
    """The panel's ngrok, its API off over the human's ``own`` config, run by ``popen``."""
    return NgrokTunnel(
        port,
        which=lambda name: name,
        popen=popen,
        api_off=True,
        configs=lambda: api_off_configs(own=own),
    )


def test_the_panels_ngrok_runs_with_its_agent_api_off(tmp_path: Path) -> None:
    """The panel's ngrok served its agent API, which any user of the machine could use to stop
    Remote's tunnel and start it again with the inspector on, reading the passphrase and every
    cookie off it (sweep of #243). It runs with the API off, and says nothing of it."""
    own = write_ngrok_config(tmp_path, 'version: "2"\nauthtoken: tok_123\n')
    popen, runs = ngrok_binary(tmp_path)
    tunnel = panels_ngrok(8750, own, popen)
    assert tunnel.start_tunnel() is None
    try:
        assert tunnel.wait_for_url(10) == OURS["url"]
        (run,) = runs
        assert run[-1] == f"--config={own},{paths.aisquare_home() / OUR_CONFIG}"
        assert "--inspect=false" in run
        assert tunnel.api_warning is None and tunnel.api_refused is None
    finally:
        tunnel.stop_tunnel()


@pytest.mark.parametrize(
    "refused",
    [
        f"ERROR:  open /home/u/.aisquare/{OUR_CONFIG}: permission denied",
        json.dumps(
            {
                "lvl": "crit",
                "msg": "failed to read configuration",
                "path": f"C:\\Users\\u\\.aisquare\\{OUR_CONFIG}",
                "err": "Access is denied.",
            }
        ),
    ],
    ids=["said in plain text", "said in its JSON log"],
)
def test_an_ngrok_that_cannot_read_our_config_runs_as_before_and_the_panel_says_its_api_is_on(
    tmp_path: Path, refused: str
) -> None:
    """Merging is ngrok's to judge, and a Remote with ngrok's API on is better than none: an
    ngrok that ends before it announces, run with our config and saying that config is why
    (a snap that may not read ~/.aisquare), is run again without it, and the status line
    says the API is on, and what to set."""
    own = write_ngrok_config(tmp_path, 'version: "2"\nauthtoken: tok_123\n')
    popen, runs = ngrok_binary(tmp_path, with_our_config=refused)
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=lambda port: panels_ngrok(port, own, popen)
    )
    controller.turn_on()
    try:
        assert controller._waiter is not None
        controller._waiter.join(10)
        assert controller.link_url() == build_public_url(OURS["url"], server.token)
        first, second = runs
        assert first[-1].startswith("--config=") and not any(
            arg.startswith("--config=") for arg in second
        )
        tunnel = controller.tunnel
        assert tunnel is not None
        assert tunnel.api_refused
        assert controller.message is None
        assert controller.status_line() == API_ON.format(addr="127.0.0.1:4040")
    finally:
        controller.turn_off()


@pytest.mark.parametrize(
    ("ended", "said"),
    [
        (
            json.dumps(
                {
                    "lvl": "eror",
                    "msg": "failed to start tunnel",
                    "err": "The endpoint 'https://x.ngrok-free.app' is already online. "
                    "ERR_NGROK_334",
                }
            ),
            "The endpoint 'https://x.ngrok-free.app' is already online. ERR_NGROK_334",
        ),
        ("ERROR:  authentication failed: Usage of ngrok requires an authtoken.", AUTHTOKEN_HINT),
    ],
    ids=["its static domain still held", "no authtoken"],
)
def test_an_ngrok_that_ends_for_a_reason_of_its_own_is_never_run_again_with_its_api_on(
    tmp_path: Path, ended: str, said: str
) -> None:
    """Any end of the first ngrok before it announced a tunnel ran it again without our config:
    a watchdog's restart whose ngrok found the static domain still held by the session it
    replaces (ERR_NGROK_334), started again a moment later, came up with its API on for
    the rest of its run, which our config is there to keep off. Only an ngrok that says our
    config is why is run without it; any other says why it ended, as ever."""
    own = write_ngrok_config(tmp_path, 'version: "2"\nauthtoken: tok_123\n')
    popen, runs = ngrok_binary(tmp_path, with_our_config=ended)
    tunnel = panels_ngrok(8750, own, popen)
    assert tunnel.start_tunnel() is None
    try:
        assert tunnel.wait_for_url(10) is None
        assert len(runs) == 1, "run again without our config, its API on"
        assert tunnel.error == said
        assert tunnel.api_refused is None and tunnel.api_addr is None
    finally:
        tunnel.stop_tunnel()


def test_only_a_line_that_names_our_config_with_an_error_says_ngrok_cannot_take_it() -> None:
    path = f"/home/u/.aisquare/{OUR_CONFIG}"
    opened = {"lvl": "info", "msg": "open config file", "path": path}
    blames = ngrok_tunnel.says_trouble_with
    assert blames(f"ERROR:  open {path}: permission denied", OUR_CONFIG)
    assert blames(json.dumps({**opened, "err": "open: permission denied"}), OUR_CONFIG)
    assert blames(json.dumps({"lvl": "crit", "msg": "bad config", "path": path}), OUR_CONFIG)
    windows = f"C:\\Users\\u\\.aisquare\\{OUR_CONFIG}"  # escaped in the JSON: the name is not
    assert blames(json.dumps({**opened, "path": windows, "err": "Access is denied."}), OUR_CONFIG)
    for fine in (None, "", "<nil>"):
        assert not blames(json.dumps({**opened, "err": fine}), OUR_CONFIG), "ngrok opened it"
    assert not blames(json.dumps(opened), OUR_CONFIG)
    assert not blames("ERROR:  authentication failed: ERR_NGROK_4018", OUR_CONFIG)
    assert not blames(json.dumps({"lvl": "eror", "err": "ERR_NGROK_334"}), OUR_CONFIG)
    assert not blames(json.dumps([path]), OUR_CONFIG)


def test_the_panel_starts_its_ngrok_with_the_api_off() -> None:
    default = inspect.signature(RemoteController).parameters["tunnel_factory"].default
    assert default is remote_control.ngrok_without_its_api
    tunnel = remote_control.ngrok_without_its_api(8750)
    assert tunnel.api_off and tunnel.port == 8750


def test_the_panel_says_why_ngrok_exited_before_it_announced_a_tunnel(tmp_path: Path) -> None:
    cause = "Error reading configuration file '/home/u/.config/ngrok/ngrok.yml': EOF"
    body = f"print({'ERROR:  ' + cause!r}, file=sys.stderr)\nsys.exit(1)\n"
    command = ngrok_printing(tmp_path, body)
    controller = RemoteController(
        server=fake_server(),
        tunnel_factory=lambda port: NgrokTunnel(port, command=command),
        url_timeout=10,
    )
    heard = heard_news(controller)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(10)
    said = f"ngrok exited (code 1) before it announced a tunnel: {cause}"
    assert controller.message == said
    assert heard == [(f"{remote_control.UNREACHABLE} — {said}", True)]
    controller.turn_off()


# --- the controller -------------------------------------------------------------------------


#: Every ``remote_server`` module function the controller calls: turning Remote off
#: revokes every device, the TUI notes ngrok's URL and adopts a deadline the phone
#: extended. The fake answers all of them, and the test below holds it to the module.
SERVER_CALLS = (
    "start_remote_server",
    "stop_remote_server",
    "remote_server_status",
    "revoke_remote_device",
    "set_allow_write",
    "set_auto_off",
    "regenerate_password",
    "note_public_url",
    "revoke_every_remote_device",
    "remote_auto_off_at",
    "remote_allow_write",
    "remote_password",
    "remote_served_elsewhere",
)


class FakeServer(types.ModuleType):
    """The server module as the controller sees it (:data:`SERVER_CALLS`), recording every
    call; devices are a plain list, and ``calls`` has every call's name in order."""

    def __init__(self) -> None:
        super().__init__("fake_remote_server")
        self.token = "tok_TEST"
        self.password = "amber-birch-cedar-delta"
        self.running = False
        self.allow_write = False
        """``remote.json``'s write switch, as the fake keeps it."""
        self.allow_write_calls: list[bool] = []
        self.auto_off_calls: list[datetime | None] = []
        self.revoked: list[str] = []
        self.fail_start: Exception | None = None
        self.devices: list[dict[str, Any]] = []
        self.failed_unlocks = 0
        self.locked_out_until: str | None = None
        self.revoked_every: list[str] = []
        """``revoke_every_remote_device`` reasons, in order."""
        self.public_urls: list[str | None] = []
        """``note_public_url`` calls, in order; ``None`` is the origin forgotten."""
        self.server_auto_off_at: datetime | None = None
        """What ``remote_auto_off_at()`` answers: the last ``set_auto_off``, to the second as
        ``remote.json`` keeps it, or a deadline a test sets to stand for a phone's extension
        or for the file's own, read again."""
        self.unwritable: OSError | None = None
        """Raised by every call that writes ``remote.json`` once it has made its change, as
        the real module's ``Runtime`` does: it changes its state in memory, then the rename
        over the file fails (a full disk, a read-only home), and nothing rolls the change
        back. The server keeps it until it reads the file again after another process
        rewrote it, which a test stands for by setting the field back."""
        self.calls: list[str] = []
        self.served_elsewhere = False
        """What ``remote_served_elsewhere()`` answers: another process serves this home."""
        self.DEFAULT_PORT = 8750
        self.RemoteInfo = remote_server.RemoteInfo

    def _write_remote_json(self) -> None:
        """The rename over ``remote.json``, after the change in memory."""
        if self.unwritable is not None:
            raise self.unwritable

    def start_remote_server(
        self, dist_dir: Path | None, port: int = 8750
    ) -> remote_server.RemoteInfo:
        if self.fail_start is not None:
            raise self.fail_start
        self.running = True
        return remote_server.RemoteInfo(
            self.token, self.password, f"http://127.0.0.1:{port}/r/{self.token}/"
        )

    def stop_remote_server(self) -> None:
        self.calls.append("stop_remote_server")
        self.running = False

    def remote_server_status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "devices": list(self.devices),
            "failed_unlocks": self.failed_unlocks,
            "locked_out_until": self.locked_out_until,
        }

    def revoke_remote_device(self, device_id: str) -> None:
        self.revoked.append(device_id)
        self.devices = [d for d in self.devices if d.get("id") != device_id]
        self._write_remote_json()

    def set_allow_write(self, enabled: bool) -> None:
        self.allow_write_calls.append(enabled)
        self.allow_write = enabled
        self._write_remote_json()

    def set_auto_off(self, at: datetime | None) -> None:
        self.auto_off_calls.append(at)
        self.server_auto_off_at = None if at is None else at.replace(microsecond=0)
        self._write_remote_json()

    def regenerate_password(self, new_link: bool = False) -> str:
        self.password = "ember-glade-heron-indigo"
        self._write_remote_json()
        return self.password

    def revoke_every_remote_device(self, reason: str) -> None:
        self.calls.append("revoke_every_remote_device")
        self.revoked_every.append(reason)
        self.devices = []
        self._write_remote_json()

    def note_public_url(self, url: str | None) -> None:
        self.calls.append("note_public_url")
        self.public_urls.append(url)

    def remote_auto_off_at(self) -> datetime | None:
        return self.server_auto_off_at

    def remote_allow_write(self) -> bool:
        return self.allow_write

    def remote_password(self) -> str:
        return self.password

    def remote_served_elsewhere(self) -> bool:
        return self.served_elsewhere


def _positional(function: Any) -> int:
    return sum(
        param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        for param in inspect.signature(function).parameters.values()
    )


def test_the_fake_server_answers_every_call_the_real_module_does() -> None:
    """A fake that drifts from the module tests a controller nobody runs. Where the module
    already has a name, the fake takes the same arguments; where it does not yet (a lane
    still to land), the fake leads and the controller can be written against it."""
    fake = FakeServer()
    for name in SERVER_CALLS:
        real = getattr(remote_server, name, None)
        assert callable(getattr(fake, name)), name
        if real is not None:
            assert _positional(getattr(fake, name)) == _positional(real), name
    assert {"start_remote_server", "note_public_url"} <= {
        name for name in SERVER_CALLS if hasattr(remote_server, name)
    }, "the comparison above compares something"


def fake_server() -> FakeServer:
    return FakeServer()


class FakeTunnel(NgrokTunnel):
    """An ``NgrokTunnel`` that never spawns: ``url`` arrives at once, or ``failure`` is returned."""

    def __init__(self, port: int, *, url: str | None, failure: str | None) -> None:
        super().__init__(port, which=lambda _name: None)
        self._fake_url = url
        self._failure = failure
        self.stopped = False

    def start_tunnel(self) -> str | None:
        if self._failure is not None:
            self.error = self._failure
            return self._failure
        self.public_url = self._fake_url
        self._url_ready.set()
        return None

    def stop_tunnel(self) -> None:
        self.stopped = True


def fake_tunnel_factory(
    *, url: str | None = None, failure: str | None = None
) -> remote_control.TunnelFactory:
    return lambda port: FakeTunnel(port, url=url, failure=failure)


def test_fresh_state_is_off_and_one_hour_and_the_sentence_names_both_switches() -> None:
    assert load_remote_state() == RemoteState(remote_enabled=False, auto_off_minutes=60)
    assert READ_ONLY_REASON is remote_server.READ_ONLY_REASON, "one sentence, the server's"
    assert "aisquare remote allow-write on" in READ_ONLY_REASON
    assert "Allow write actions in the R panel" in READ_ONLY_REASON


def test_turn_on_starts_the_server_leaves_the_write_switch_alone_and_persists() -> None:
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x.app"))
    controller.turn_on()
    assert server.running
    assert server.allow_write_calls == [], "the write switch is remote.json's, not turn_on's"
    assert controller.write_actions_allowed() is False  # never on by default
    assert controller.password() == server.password
    assert controller.state.remote_enabled is True
    assert read_state()["remote_enabled"] is True
    assert "allow_write" not in read_state()
    controller.turn_off()
    assert not server.running
    assert controller.link_url() is None
    assert read_state()["remote_enabled"] is False


def test_without_ngrok_remote_is_on_locally_and_the_status_line_says_how_to_install() -> None:
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(failure=missing_binary_message())
    )
    controller.turn_on()
    assert controller.running and server.running
    assert controller.tunnel is None
    assert controller.message == INSTALL_HINT
    assert controller.link_url() == f"http://127.0.0.1:8750/r/{server.token}/"  # §6 fallback


def test_a_server_that_cannot_start_is_a_sentence_in_the_modal_not_a_crash() -> None:
    """The real module raises RemoteUnavailable (extra missing) or RemoteError (port busy)."""
    server = fake_server()
    server.fail_start = remote_server.RemoteUnavailable("the remote extra is not installed")
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.turn_on()
    assert not controller.running and not server.running
    assert controller.message == "Remote could not start — the remote extra is not installed"
    assert controller.state.remote_enabled is False  # a restart must not retry blindly
    assert read_state() == {}  # nothing was persisted by a failed start


def test_the_switches_survive_a_restart_of_the_tui_next_to_the_theme_key() -> None:
    update_state("board_theme", "nord")
    server = fake_server()
    first = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    first.set_allow_write(True)
    first.set_auto_off(120)
    first.turn_on()
    first.shutdown_for_exit()  # the TUI exits: processes end, the saved switches stay
    saved = json.loads(paths.state_path().read_text())
    assert saved["board_theme"] == "nord"  # the theme key is untouched by our merge
    assert (saved["remote_enabled"], saved["auto_off_minutes"]) == (True, 120)
    assert "allow_write" not in saved, "the write switch lives in remote.json alone"
    assert server.allow_write_calls == [True], "flipped while Remote was off, it still landed"
    assert _load_saved_theme() == "nord"

    second = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert second.state == RemoteState(remote_enabled=True, auto_off_minutes=120)
    assert not second.running
    second.restore()
    assert second.running
    assert server.allow_write_calls == [True], "a restart does not write the switch again"
    assert second.write_actions_allowed() is True


def test_a_switch_saves_its_own_key_and_never_the_other_tuis_start_of_day() -> None:
    """Every save wrote both keys from what this TUI read at its start. Two ``asq ui`` open:
    A turns Remote off, then B, which read it as on, picks an auto-off, and B's save wrote
    Remote back on; the next start brought the public tunnel back against the human's last
    off. Picking Never did it with no timer at all (sweep of #243)."""
    server = fake_server()
    first = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    first.turn_on()
    second = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert second.state.remote_enabled is True, "B read Remote as on at its start"
    first.turn_off()
    second.set_auto_off(30)
    second.set_auto_off(None)
    saved = read_state()
    assert (saved["remote_enabled"], saved["auto_off_minutes"]) == (False, "never")
    third = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    third.restore()
    assert not third.running, "the next start leaves the Remote turned off off"

    first.set_auto_off(120)  # and the other way round: A's pick keeps B's Remote on
    second.turn_on()
    first.set_auto_off(30)
    assert read_state()["remote_enabled"] is True
    second.turn_off()


def test_a_switch_state_json_refuses_is_said_until_a_save_of_it_lands() -> None:
    """A refused save of a switch was dropped without a word: a refused off brought Remote
    back at the next start with nothing said, nor anything at quit (sweep of #243)."""
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    paths.ensure_home()
    paths.state_path().write_text("[]")  # not an object: update_state refuses every key
    controller.turn_on()
    assert controller.running
    line = controller.status_line()
    assert line.startswith("Remote's on/off switch could not be saved — ")
    assert f"{paths.state_path()} is not a JSON object" in line
    controller.set_auto_off(30)
    assert "the auto-off timer could not be saved — " in controller.status_line()
    paths.state_path().write_text("{}")
    controller.set_auto_off(120)
    assert controller.status_line().startswith("Remote's on/off switch could not be saved")
    assert "auto-off timer" not in controller.status_line(), "its own save landed"
    controller.turn_off()
    assert controller.status_line() == ""
    assert read_state() == {"auto_off_minutes": 120, "remote_enabled": False}


def test_restore_leaves_a_remote_that_was_off_alone() -> None:
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.restore()
    assert not controller.running and not server.running


def test_auto_off_turns_remote_off_when_the_timer_runs_out() -> None:
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.set_auto_off(30)
    controller.turn_on()
    deadline = datetime(2026, 9, 11, 18, 30, tzinfo=UTC)
    assert controller.auto_off_at == deadline
    assert server.auto_off_calls == [deadline]  # shown via GET /api/remote
    clock[0] += timedelta(minutes=29)
    assert controller.enforce_auto_off() is False and controller.running
    clock[0] += timedelta(minutes=1)
    assert controller.enforce_auto_off() is True
    assert not controller.running and not server.running
    assert server.auto_off_calls[-1] is None  # cleared on the way off
    assert server.revoked_every == ["auto-off"], "every device revoked, the farewell first"
    assert controller.message is not None and "auto-off" in controller.message
    with pytest.raises(ValueError):
        controller.set_auto_off(45)


def test_regenerate_devices_and_revoke_go_through_the_server() -> None:
    server = fake_server()
    server.devices = [
        {"id": "dev_0000000a", "ua": "iPhone", "first_seen": "t0", "last_seen": "t1"},
        {"id": "dev_0000000b", "ua": "Pixel", "first_seen": "t0", "last_seen": "t1"},
        {"broken": True},
    ]
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert controller.regenerate_password() is None  # off: nothing to unlock
    controller.turn_on()
    assert controller.regenerate_password() == "ember-glade-heron-indigo"
    assert controller.password() == "ember-glade-heron-indigo"
    assert [d["id"] for d in controller.devices()] == ["dev_0000000a", "dev_0000000b"]
    assert controller.revoke_device("dev_0000000a") is True
    assert server.revoked == ["dev_0000000a"]
    assert [d["id"] for d in controller.devices()] == ["dev_0000000b"]


def test_the_deadline_is_an_instant_with_its_offset_not_naive_local_time() -> None:
    """Naive local time plus an hour ran an hour long across a DST fall-back, and went out
    without an offset a phone in another timezone read as its own (review of #243)."""
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    before = datetime.now(UTC)
    controller.turn_on()
    deadline = server.auto_off_calls[-1]
    assert deadline is not None and deadline.tzinfo is not None
    assert before + timedelta(minutes=59) < deadline <= datetime.now(UTC) + timedelta(minutes=60)


def test_the_password_is_read_from_the_server_every_time() -> None:
    """``asq remote regenerate-password`` in another shell made the shown passphrase wrong
    until Remote was turned off and on (review of #243)."""
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert controller.password() is None, "off: nothing to unlock"
    controller.turn_on()
    server.password = "anchor-badger-cactus-dolphin"  # what the shell's regenerate wrote
    assert controller.password() == "anchor-badger-cactus-dolphin"


def tunnel_announced(controller: RemoteController, server: FakeServer) -> None:
    """Wait for the tunnel's URL and forget the call announcing it made: noted for push
    links (SPEC §5.8), it lands between turning on and off, from the ``ngrok-url`` thread,
    and what turning off calls, in order, is what is asserted."""
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert server.calls == ["note_public_url"] and server.public_urls == [controller.public_url]
    server.calls.clear()
    server.public_urls.clear()


def test_turning_remote_off_revokes_every_device_before_the_server_stops() -> None:
    """The farewell push goes from inside the revoke, so the order is the contract: revoke
    (farewell first), forget the public origin, then stop. Leaving the TUI revokes nothing."""
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.turn_on()
    tunnel_announced(controller, server)
    controller.turn_off()
    assert server.calls == ["revoke_every_remote_device", "note_public_url", "stop_remote_server"]
    assert server.revoked_every == ["remote off"] and server.public_urls == [None]
    exiting = fake_server()
    leaving = RemoteController(server=exiting, tunnel_factory=fake_tunnel_factory(url="x"))
    leaving.turn_on()
    tunnel_announced(leaving, exiting)
    leaving.shutdown_for_exit()
    assert exiting.revoked_every == [] and exiting.calls == [
        "note_public_url",
        "stop_remote_server",
    ]


def test_a_url_ngrok_announces_after_the_wait_is_the_link_and_the_push_origin_all_the_same(
    tmp_path: Path,
) -> None:
    """``restore()`` brings Remote back at a TUI start, often before a waking laptop's Wi-Fi
    is up, and ngrok retries its session until it is. The URL it announced a minute later
    was never taken: the panel kept "ngrok did not announce a tunnel in time" and the local
    link, and push links had no origin, until Remote was turned off and on (r3 review of
    #243). A real tunnel's log reader hands it over whenever it comes."""
    server = fake_server()
    command = fake_ngrok(tmp_path, 1.0, STARTED)
    controller = RemoteController(
        server=server,
        tunnel_factory=lambda port: NgrokTunnel(port, command=command),
        url_timeout=0.2,
    )
    controller.turn_on()
    try:
        assert controller._waiter is not None
        controller._waiter.join(5)
        assert controller.message == "ngrok did not announce a tunnel in time"
        assert controller.link_url() == f"http://127.0.0.1:8750/r/{server.token}/"
        assert server.public_urls == []
        deadline = time.monotonic() + 15
        while controller.public_url is None and time.monotonic() < deadline:
            time.sleep(0.02)
        link = build_public_url(STARTED["url"], server.token)
        assert controller.link_url() == link, "the URL that came late is the link"
        assert server.public_urls == [link], "and where push links lead"
        assert controller.message is None
    finally:
        controller.turn_off()


def test_a_url_announced_again_is_taken_once_and_one_that_changed_is_taken_again() -> None:
    """A URL is noted once however often it is announced: the thread that waited for it and
    the log reader both hand it over. A new one, ngrok's session back on another address, is
    the link from then on."""
    server = fake_server()
    tunnels: list[FakeTunnel] = []

    def factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    controller = RemoteController(server=server, tunnel_factory=factory)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert controller.message == "ngrok did not announce a tunnel in time"
    (tunnel,) = tunnels
    tunnel.handle_line(json.dumps(STARTED))
    tunnel.handle_line(json.dumps(STARTED))
    link = build_public_url(STARTED["url"], server.token)
    assert controller.link_url() == link and controller.message is None
    assert server.public_urls == [link]
    moved = {**STARTED, "url": "https://efgh-34.ngrok-free.app"}
    tunnel.handle_line(json.dumps(moved))
    assert controller.link_url() == build_public_url(moved["url"], server.token)
    assert server.public_urls == [link, controller.link_url()]


class QuietTunnel(FakeTunnel):
    """A tunnel that is up and has not announced yet: its URL comes when the test feeds its
    log a line, and its wait ends when the test says (``give_up``), whatever the timeout."""

    def __init__(self, port: int) -> None:
        super().__init__(port, url=None, failure=None)
        self.give_up = threading.Event()

    def start_tunnel(self) -> str | None:
        return None

    def wait_for_url(self, timeout: float = 15.0) -> str | None:
        self.give_up.wait(10)
        return self.public_url


def test_an_old_tunnels_late_word_never_lands_on_the_remote_after_it() -> None:
    """Remote off and on again while the old tunnel still had not announced: its wait running
    out put "ngrok did not announce a tunnel in time" on the new Remote, whose own tunnel was
    still starting, and its URL, landing late, became the new Remote's link and push origin."""
    server = fake_server()
    old, new = QuietTunnel(8750), QuietTunnel(8750)
    made = [old, new]
    controller = RemoteController(server=server, tunnel_factory=lambda port: made.pop(0))
    controller.turn_on()
    old_waiter = controller._waiter
    assert old_waiter is not None
    controller.turn_off()
    controller.turn_on()
    new_waiter = controller._waiter
    assert new_waiter is not None and new_waiter is not old_waiter
    assert controller.message == "starting ngrok…"

    old.give_up.set()  # the old tunnel's wait runs out now
    old_waiter.join(5)
    assert controller.message == "starting ngrok…", "the old tunnel's timeout is not this one's"
    new.handle_line(json.dumps({**STARTED, "url": "https://new-56.ngrok-free.app"}))
    link = build_public_url("https://new-56.ngrok-free.app", server.token)
    assert controller.link_url() == link and controller.message is None
    old.handle_line(json.dumps(STARTED))  # and the old one's URL lands after all
    assert controller.link_url() == link
    assert server.public_urls[-1] == link, "push links still lead to the new tunnel"
    new.give_up.set()
    new_waiter.join(5)
    assert server.public_urls.count(link) == 1


def heard_news(controller: RemoteController) -> list[tuple[str, bool]]:
    """What ``controller`` tells the fleet UI, and whether it is trouble, in order."""
    heard: list[tuple[str, bool]] = []
    controller.on_news = lambda text, trouble: heard.append((text, trouble))
    return heard


def test_a_remote_that_does_not_come_back_at_start_is_news() -> None:
    """``restore()`` runs as the human sits down, often just before leaving the desk with the
    phone. A Remote that did not come back, or came back with no tunnel, was said only on
    the R panel's status line: the human found out from the phone (sweep of #243)."""
    server = fake_server()
    busy = "the remote server did not come up on 127.0.0.1:8750 — is the port in use?"
    server.fail_start = remote_server.RemoteError(busy)
    enabled = RemoteState(remote_enabled=True)
    held = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), state=enabled
    )
    heard = heard_news(held)
    held.restore()
    assert heard == [(f"Remote could not start — {busy}", True)]

    server.fail_start = None
    no_ngrok = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(failure=INSTALL_HINT), state=enabled
    )
    heard = heard_news(no_ngrok)
    no_ngrok.restore()
    assert no_ngrok.running
    assert heard == [(f"Remote is on, but phones cannot reach it — {INSTALL_HINT}", True)]
    no_ngrok.turn_off()

    back = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), state=enabled
    )
    heard = heard_news(back)
    back.restore()
    assert back._waiter is not None
    back._waiter.join(5)
    back.turn_off()
    assert back.running is False and heard == [], "a Remote back as it was, and turned off, is none"


def test_a_tunnel_that_does_not_come_up_is_news_and_so_is_its_url_when_it_comes() -> None:
    server = fake_server()
    tunnels: list[FakeTunnel] = []

    def factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    controller = RemoteController(server=server, tunnel_factory=factory)
    heard = heard_news(controller)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert heard == [
        ("Remote is on, but phones cannot reach it — ngrok did not announce a tunnel in time", True)
    ]
    tunnels[0].handle_line(json.dumps(STARTED))
    assert heard[1:] == [("ngrok is up — phones can reach Remote now", False)]


def test_a_url_that_lands_as_the_wait_runs_out_is_told_as_up_after_the_trouble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The waiter decided on the trouble under the lock and told it after letting go. ngrok's
    log reader adopting the URL in between found nothing told yet, so said nothing, and the
    trouble came after it: "phones cannot reach it" for a Remote that had its public link
    and push origin, and no word after (verification of c6e1e149, by forced interleaving)."""
    server = fake_server()
    tunnels: list[FakeTunnel] = []

    def factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url=None, failure=None))
        return tunnels[-1]

    controller = RemoteController(server=server, tunnel_factory=factory, url_timeout=0.1)
    heard = heard_news(controller)
    told = controller._unreachable
    readers: list[threading.Thread] = []

    def the_url_lands_as_the_trouble_is_told(why: str | None) -> None:
        reader = threading.Thread(target=tunnels[0].handle_line, args=(json.dumps(STARTED),))
        readers.append(reader)
        reader.start()
        reader.join(0.5)  # ngrok's log reader, adopting the URL wherever it can
        told(why)

    monkeypatch.setattr(controller, "_unreachable", the_url_lands_as_the_trouble_is_told)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    readers[0].join(5)
    assert controller.link_url() == build_public_url(STARTED["url"], server.token)
    assert controller.message is None
    assert heard == [
        (
            "Remote is on, but phones cannot reach it — ngrok did not announce a tunnel in time",
            True,
        ),
        ("ngrok is up — phones can reach Remote now", False),
    ]


def test_auto_off_and_what_turning_off_could_not_do_are_news() -> None:
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    server = fake_server()

    def unwritable(reason: str) -> None:
        raise OSError("remote.json: read-only file system")

    server.revoke_every_remote_device = unwritable  # type: ignore[method-assign]
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    heard = heard_news(controller)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    clock[0] += timedelta(minutes=60)
    assert controller.enforce_auto_off() is True
    assert heard == [
        ("Remote turned off — the auto-off timer ran out", True),
        (
            "Remote is off, but its devices could not be revoked — "
            "remote.json: read-only file system",
            True,
        ),
    ]


def test_ngrok_back_on_a_new_link_is_news_and_a_restart_failing_alike_is_said_once() -> None:
    """A restart on a new link leaves every phone on a dead one, and the human at the desk
    has the new one to give; a restart that keeps failing the same way, once a minute, is
    said once, not every minute."""
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    made = [
        FakeTunnel(8750, url="https://first.ngrok-free.app", failure=None),
        FakeTunnel(8750, url="https://second.ngrok-free.app", failure=None),
        FakeTunnel(8750, url=None, failure="could not start ngrok: gone"),
        FakeTunnel(8750, url=None, failure="could not start ngrok: gone"),
    ]
    controller = RemoteController(
        server=fake_server(), tunnel_factory=lambda port: made.pop(0), now=lambda: clock[0]
    )
    heard = heard_news(controller)
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    assert controller.revive_tunnel_if_dead() is True  # a FakeTunnel never runs: it died
    controller._waiter.join(5)
    assert heard == [("ngrok stopped and came back on a new link — R shows it", True)]
    for _ in range(2):
        clock[0] += timedelta(minutes=1)
        assert controller.revive_tunnel_if_dead() is False
    assert heard[1:] == [
        ("Remote is on, but phones cannot reach it — could not start ngrok: gone", True)
    ]


class SlowServer(FakeServer):
    """A server whose stop takes until the test says: uvicorn waiting for a needs scan in
    flight and the push sender, ngrok given its seconds to exit."""

    def __init__(self, *, patience: float = 10.0) -> None:
        super().__init__()
        self.stopping = threading.Event()
        self.release = threading.Event()
        self.patience = patience

    def stop_remote_server(self) -> None:
        self.stopping.set()
        self.release.wait(self.patience)
        super().stop_remote_server()


class LockedServer(FakeServer):
    """A server whose every write of ``remote.json`` waits for the file's lock, which another
    process holds until the test lets it go (:attr:`free`); ``waiting`` is set as one waits."""

    def __init__(self) -> None:
        super().__init__()
        self.free = threading.Event()
        self.waiting = threading.Event()

    def _write_remote_json(self) -> None:
        self.waiting.set()
        assert self.free.wait(10), "the lock was never let go"
        super()._write_remote_json()


def test_the_panels_controls_write_remote_json_on_a_thread_of_their_own() -> None:
    """The write switch, the Auto-off picker, Regenerate, Revoke and the start's deadline each
    wrote ``remote.json`` on the caller's thread, Textual's in the fleet UI, waiting for the
    file's lock: two seconds a press while another process held it, the fleet UI frozen for
    them (sweep of #243). Each returns at once now, shows what it asked at once, says it is
    saving while the write waits, and lands, in the order asked, once the lock is free."""
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    server = LockedServer()
    server.devices = [{"id": "dev_0000000a", "ua": "iPhone", "first_seen": "t0", "last_seen": "t1"}]
    tunnels: list[FakeTunnel] = []

    def factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url="https://a.ngrok-free.app", failure=None))
        return tunnels[-1]

    controller = RemoteController(server=server, tunnel_factory=factory, now=lambda: clock[0])
    done = heard_news(controller)
    controller.on_done, controller.on_news = controller.on_news, None
    started = time.monotonic()
    controller.turn_on(wait=False)
    controller.set_allow_write(True, wait=False)
    controller.set_auto_off(30, wait=False)
    assert controller.regenerate_password(wait=False) is None
    assert controller.revoke_device("dev_0000000a", wait=False) is False
    assert time.monotonic() - started < 1.0, "a control waited for remote.json's lock"
    assert server.waiting.wait(5)
    assert controller.running and controller.message == remote_control.STARTING
    assert tunnels == [], "no ngrok before the start's deadline is written"
    assert controller.write_actions_allowed() is True, "the switch shows the press"
    assert controller.auto_off_at == clock[0] + timedelta(minutes=30)
    assert controller.adopt_server_deadline() == clock[0] + timedelta(minutes=30)
    time.sleep(remote_control.SAVING_AFTER)
    assert remote_control.SAVING in controller.status_line().splitlines()
    assert not controller.writes_done(0.1)

    server.free.set()
    assert controller.writes_done(5)
    assert server.auto_off_calls == [
        clock[0] + timedelta(minutes=60),
        clock[0] + timedelta(minutes=30),
    ], "in the order asked"
    assert server.allow_write_calls == [True] and server.allow_write is True
    assert server.revoked == ["dev_0000000a"] and server.password == "ember-glade-heron-indigo"
    assert done == [
        ("New password — every device has to unlock again", False),
        ("Revoked dev_0000000a", False),
    ]
    assert len(tunnels) == 1 and controller.tunnel is tunnels[0]
    assert read_state()["remote_enabled"] is True, "saved once the deadline was written"
    assert remote_control.SAVING not in controller.status_line()
    controller.turn_off()


def test_turning_off_while_a_starts_deadline_waits_leaves_nothing_of_that_start() -> None:
    """A Remote turned off before its start's deadline was written: the stopping waits for that
    write, then clears it, and nothing else of the start follows: no ngrok, no saved on."""
    server = LockedServer()
    tunnels: list[FakeTunnel] = []

    def factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url="https://a.ngrok-free.app", failure=None))
        return tunnels[-1]

    controller = RemoteController(server=server, tunnel_factory=factory)
    controller.turn_on(wait=False)
    assert server.waiting.wait(5)
    controller.turn_off(wait=False)
    assert not controller.running
    server.free.set()
    assert controller.wait_until_off(5) and controller.writes_done(5)
    assert server.auto_off_calls[-1] is None, "cleared after the start's deadline landed"
    assert tunnels == [] and not server.running
    assert read_state()["remote_enabled"] is False


def test_a_remote_still_starting_as_the_ui_quits_is_saved_as_on_for_the_next_start() -> None:
    """A start saves its switch only once its deadline is written, on the thread that drives
    the controller: a fleet UI that quit before then (a press, then q, while another process
    held remote.json's lock) took no more steps, and the Remote the human turned on did not
    come back at the next start."""
    server = LockedServer()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    dropped: list[Callable[[], None]] = []
    controller.call_back = dropped.append  # the UI is gone: what it was handed never runs
    controller.turn_on(wait=False)
    assert server.waiting.wait(5)
    controller.shutdown_for_exit(wait=False)
    assert read_state()["remote_enabled"] is True, "saved as on, for restore()"
    server.free.set()
    assert controller.wait_until_off(5) and controller.writes_done(5)
    assert read_state()["remote_enabled"] is True
    assert load_remote_state().remote_enabled is True


def test_a_starts_failure_found_on_the_writers_thread_stops_only_that_start() -> None:
    """A start whose deadline would not write is stopped from the writer's thread, maybe after
    the human turned Remote off and on again: the Remote that start began, never another."""
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.turn_on()
    other = remote_server.RemoteInfo("tok_OTHER", "pw", "http://127.0.0.1:8750/r/tok_OTHER/")
    assert controller.turn_off(persist=False, serving=other) is False
    assert controller.running and server.running
    assert controller.turn_off(serving=controller.info) is True
    assert not controller.running and not server.running


def test_turning_remote_off_reads_off_at_once_and_stops_on_a_thread_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch, auto-off and quit stopped uvicorn and ngrok on Textual's thread, which
    froze the fleet UI for as long as they took: seconds when a needs scan was in flight
    (r3 review of #243). The controller reads off at once, the status line says Remote is
    turning off until the stopping is done, and a Remote turned on meanwhile waits a moment
    for it and then says to try again, rather than starting one the old stop would undo."""
    monkeypatch.setattr(remote_control, "OFF_WAIT_SECONDS", 0.2)
    server = SlowServer()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.turn_on()
    tunnel = controller.tunnel
    assert isinstance(tunnel, FakeTunnel)
    started = time.monotonic()
    controller.turn_off(wait=False)
    assert time.monotonic() - started < 1.0, "turning off waited for the server to stop"
    assert server.stopping.wait(5), "the stopping runs all the same"
    assert not controller.running and controller.link_url() is None
    assert controller.message == remote_control.TURNING_OFF
    assert read_state()["remote_enabled"] is False
    assert server.running and not tunnel.stopped, "still winding down"
    assert controller.wait_until_off(0.05) is False

    controller.turn_on()
    assert not controller.running and server.running
    assert controller.message == remote_control.STILL_TURNING_OFF

    server.release.set()
    assert controller.wait_until_off(5)
    assert not server.running and tunnel.stopped, "ngrok goes last, as ever"
    assert server.revoked_every == ["remote off"] and server.public_urls[-1] is None
    assert controller.message is None
    controller.turn_on()
    assert controller.running


def test_auto_off_stops_on_a_thread_and_says_what_the_stopping_could_not_do() -> None:
    """The app's 30 s auto-off timer froze the fleet view for as long as the stopping took.
    The sentence that Remote is off shows at once; what the stopping could not do follows
    it once that is known."""
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    server = SlowServer()

    def unwritable(reason: str) -> None:
        raise OSError("remote.json: read-only file system")

    server.revoke_every_remote_device = unwritable  # type: ignore[method-assign]
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.turn_on()
    clock[0] += timedelta(minutes=60)
    started = time.monotonic()
    assert controller.enforce_auto_off(wait=False) is True
    assert time.monotonic() - started < 1.0, "auto-off waited for the server to stop"
    assert not controller.running
    assert controller.message == "Remote turned off — the auto-off timer ran out"
    assert server.stopping.wait(5)
    server.release.set()
    assert controller.wait_until_off(5) and not server.running
    assert controller.message == (
        "Remote turned off — the auto-off timer ran out. Remote is off, but its devices "
        "could not be revoked — remote.json: read-only file system"
    )


def test_enforce_auto_off_adopts_a_later_deadline_the_phone_set() -> None:
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.set_auto_off(30)
    controller.turn_on()
    server.server_auto_off_at = datetime(2026, 9, 11, 19, 30, tzinfo=UTC)  # extended twice
    clock[0] += timedelta(minutes=45)
    assert controller.enforce_auto_off() is False and controller.running
    assert controller.auto_off_at == server.server_auto_off_at
    clock[0] += timedelta(minutes=45)
    assert controller.enforce_auto_off() is True and not controller.running


def test_the_write_switch_reaches_remote_json_while_remote_is_off() -> None:
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert not controller.running
    controller.set_allow_write(True)
    assert server.allow_write_calls == [True] and controller.write_actions_allowed() is True


def test_a_revoke_that_cannot_be_written_still_turns_remote_off_and_says_so() -> None:
    server = fake_server()

    def unwritable(reason: str) -> None:
        raise OSError("remote.json: read-only file system")

    server.revoke_every_remote_device = unwritable  # type: ignore[method-assign]
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.turn_on()
    controller.turn_off()
    assert not server.running and not controller.running
    assert controller.message is not None and "could not be revoked" in controller.message


DENIED = PermissionError(13, "Permission denied", "remote.json")
"""What a read-only home, or a full disk's twin, raises from every write of ``remote.json``."""


def test_a_remote_json_that_will_not_write_keeps_remote_off_at_start_and_says_why() -> None:
    """``restore()`` runs in ``FleetApp.on_mount``, and the deadline ``turn_on`` could not
    record raised out of it: ``asq ui`` ended at start while the server kept serving in its
    thread (r2 review of #243). A Remote that cannot write ``remote.json`` cannot sign a
    phone in either, so it does not stay on; the saved switch stays on for the next start."""
    server = fake_server()
    server.unwritable = DENIED
    tunnels: list[FakeTunnel] = []

    def tunnel_factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url="x.app", failure=None))
        return tunnels[-1]

    controller = RemoteController(
        server=server, tunnel_factory=tunnel_factory, state=RemoteState(remote_enabled=True)
    )
    controller.restore()
    assert not controller.running and not server.running
    assert controller.link_url() is None and controller.auto_off_at is None
    assert tunnels == [], "ngrok is never started for a Remote that did not come on"
    assert controller.message == (
        "Remote could not start — remote.json could not be written: "
        "[Errno 13] Permission denied: 'remote.json'"
    )
    assert controller.state.remote_enabled is True, "the next start tries again"


def test_each_control_says_when_remote_json_will_not_write_instead_of_raising() -> None:
    """The Auto-off picker, Regenerate and Revoke raised into Textual's handlers, which
    ended the fleet UI with Remote still serving. Each now says what was not saved, while
    what it changed holds in the running server, as a failed write leaves the real one; the
    write switch said it "could not be changed" beside a switch showing the change."""
    clock = [datetime(2026, 9, 11, 18, 0, tzinfo=UTC)]
    server = fake_server()
    server.devices = [{"id": "dev_0000000a", "ua": "iPhone", "first_seen": "t0", "last_seen": "t1"}]
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.turn_on()
    server.unwritable = DENIED

    controller.set_auto_off(30)
    assert controller.save_problem == (
        "auto-off could not be saved to remote.json — [Errno 13] Permission denied: 'remote.json'"
    )
    assert controller.state.auto_off_minutes == 30
    shorter = clock[0] + timedelta(minutes=30)
    assert server.server_auto_off_at == shorter, "the running server took it all the same"
    assert controller.adopt_server_deadline() == shorter

    assert controller.regenerate_password() is None
    assert (controller.save_problem or "").startswith(
        "the new password could not be saved to remote.json — [Errno 13]"
    )
    assert controller.password() == "ember-glade-heron-indigo", "the one phones need now"

    assert controller.revoke_device("dev_0000000a") is False
    assert (controller.save_problem or "").startswith(
        "dev_0000000a could not be revoked in remote.json — [Errno 13]"
    )
    assert controller.devices() == [], "signed out of the running server all the same"

    controller.set_allow_write(True)
    assert controller.save_problem == (
        "write actions could not be saved to remote.json — "
        "[Errno 13] Permission denied: 'remote.json'"
    )
    assert controller.write_actions_allowed() is True
    assert controller.running and server.running
    assert controller.message is None, "what Remote is doing has a line of its own"

    clock[0] += timedelta(minutes=30)
    assert controller.enforce_auto_off() is True, "the timer it could not save still ends it"
    assert not controller.running and not server.running


def test_a_write_that_lands_takes_away_the_sentence_of_one_that_did_not() -> None:
    """Every write of ``remote.json`` puts the whole state the server holds, the change that
    did not land included, so the next one that lands has saved it too. The sentence that
    it had not been saved stayed on the status line all the same, until ngrok had something
    to say: writes off, or a phone revoked, read as not stuck when they had (sweep of #243).
    Whichever control's write lands takes it away, and so does turning Remote off or on."""
    server = fake_server()
    server.devices = [
        {"id": f"dev_0000000{tail}", "ua": "iPhone", "first_seen": "t0", "last_seen": "t1"}
        for tail in "abcd"
    ]
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    attempts: list[tuple[Callable[[], object], Callable[[], object]]] = [
        (lambda: controller.set_allow_write(True), lambda: controller.set_allow_write(False)),
        (lambda: controller.set_auto_off(30), lambda: controller.set_auto_off(120)),
        (controller.regenerate_password, controller.regenerate_password),
        (
            lambda: controller.revoke_device("dev_0000000a"),
            lambda: controller.revoke_device("dev_0000000b"),
        ),
        (
            lambda: controller.revoke_device("dev_0000000c"),
            lambda: controller.set_allow_write(True),
        ),
    ]
    for refused, landed in attempts:
        server.unwritable = DENIED
        refused()
        assert (controller.save_problem or "").endswith(
            "[Errno 13] Permission denied: 'remote.json'"
        )
        assert controller.status_line() == controller.save_problem
        server.unwritable = None
        landed()
        assert controller.save_problem is None and controller.status_line() == ""

    server.unwritable = DENIED
    controller.revoke_device("dev_0000000d")
    server.unwritable = None
    controller.turn_off()
    assert controller.save_problem is None, "turning off wrote the whole state"
    server.unwritable = DENIED
    controller.set_allow_write(False)
    server.unwritable = None
    controller.turn_on()
    assert controller.save_problem is None, "and so did turning on"


def test_a_timer_remote_json_will_not_take_gives_way_to_the_files_once_it_is_read_again() -> None:
    """A timer ``remote.json`` would not take is the running server's too, and its gate and
    the panel agree on it. When another process rewrites the file, the server reads it again
    and goes back to the deadline the write failed to replace, its gate ending Remote there
    (SPEC §2.5), and the panel takes it: a longer timer, or Never, kept in hand showed
    Remote on while every phone was answered as if it were off. The clock's microseconds
    are no part of a deadline: the server keeps it to the second, so the panel took its own,
    cut to the second, for the file's coming back, and stopped looking for the file's."""
    clock = [datetime(2026, 9, 11, 18, 0, 0, 250_000, tzinfo=UTC)]
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.turn_on()
    in_the_file = datetime(2026, 9, 11, 19, 0, tzinfo=UTC)
    assert server.server_auto_off_at == in_the_file
    server.unwritable = DENIED
    for minutes, picked in ((120, datetime(2026, 9, 11, 20, 0, tzinfo=UTC)), (None, None)):
        controller.set_auto_off(minutes)
        assert (controller.save_problem or "").startswith("auto-off could not be saved"), minutes
        assert server.server_auto_off_at == picked, "the running server took it"
        assert controller.adopt_server_deadline() == picked, minutes
        server.server_auto_off_at = in_the_file  # a shell's allow-write: the file, read again
        assert controller.adopt_server_deadline() == in_the_file, minutes
    clock[0] = in_the_file - timedelta(seconds=1)
    assert controller.enforce_auto_off() is False and controller.running
    clock[0] = in_the_file
    assert controller.enforce_auto_off() is True, "off when the server stops waiting"
    assert not controller.running and not server.running


def test_a_later_deadline_of_the_servers_is_taken_while_the_timer_in_hand_is_unsaved() -> None:
    """SPEC §2.5: the panel takes a later deadline of the server's, so a phone's extension
    holds. While the timer in hand was one ``remote.json`` had not taken, a later one never
    was: once the disk had room again, the server's own flush saved the timer, a phone
    extended it an hour, and the panel still turned Remote off at the timer, the server and
    the phone both waiting for the hour. A longer deadline the file held, read again after
    a shorter timer failed to save, is taken as well: the gate waits for it too."""
    clock = [datetime(2026, 9, 11, 18, 0, 0, 250_000, tzinfo=UTC)]
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.turn_on()
    server.unwritable = DENIED
    controller.set_auto_off(120)
    server.unwritable = None
    assert server.server_auto_off_at == datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
    clock[0] = datetime(2026, 9, 11, 19, 50, tzinfo=UTC)
    server.server_auto_off_at = extended = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)  # Extend 1 h
    assert controller.enforce_auto_off() is False and controller.auto_off_at == extended
    clock[0] = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
    assert controller.enforce_auto_off() is False and controller.running, "the hour holds"

    server.unwritable = DENIED
    controller.set_auto_off(30)
    assert controller.adopt_server_deadline() == datetime(2026, 9, 11, 20, 30, tzinfo=UTC)
    server.server_auto_off_at = extended  # the file's, read again
    assert controller.adopt_server_deadline() == extended
    clock[0] = extended
    assert controller.enforce_auto_off() is True and not server.running


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_auto_off_with_a_remote_json_that_will_not_write_still_stops_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The real server module, serving on a loopback port. A ``remote.json`` that would not
    write raised out of ``turn_off`` at the server's last flush: ngrok stayed up, the
    controller still read on, and auto-off, a Textual timer, ended the fleet UI with it."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("<!doctype html>", encoding="utf-8")
    tunnels: list[FakeTunnel] = []

    def tunnel_factory(port: int) -> FakeTunnel:
        tunnels.append(FakeTunnel(port, url=STARTED["url"], failure=None))
        return tunnels[-1]

    clock = [datetime.now(UTC)]
    controller = RemoteController(
        tunnel_factory=tunnel_factory,
        dist_dir=page,
        port=_free_port(),
        now=lambda: clock[0],
        state=RemoteState(auto_off_minutes=30),
    )
    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)
    served = remote_server._server
    assert controller.running and served is not None and served.running
    state = remote_server.runtime()
    assert state.unlock_device(state.password, "Pixel") is not None

    def unwritable(path: Path, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(remote_server, "replacement", unwritable)
    clock[0] += timedelta(minutes=30)
    with caplog.at_level("WARNING", logger=remote_server.__name__):
        assert controller.enforce_auto_off() is True
    assert not controller.running and controller.link_url() is None
    assert tunnels[0].stopped, "ngrok was left up"
    assert remote_server._server is None and not served.running
    message = controller.message or ""
    assert message.startswith("Remote turned off — the auto-off timer ran out")
    assert "devices could not be revoked" in message and "Permission denied" in message
    assert "flushing remote.json as the server stopped failed" in caplog.text
    assert read_state()["remote_enabled"] is False
    controller.shutdown_for_exit()  # the TUI's exit after it: nothing left to stop or raise


def test_a_real_remote_json_that_will_not_write_is_a_sentence_for_each_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real module, a ``remote.json`` from an earlier Remote, and a home that refuses
    the file's replacement: ``restore()`` raised ``PermissionError`` with the server up,
    and so did the Auto-off picker, Regenerate and Revoke (r2 review of #243)."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("<!doctype html>", encoding="utf-8")
    earlier = remote_server.runtime()
    assert earlier.unlock_device(earlier.password, "Pixel") is not None
    (device_id,) = earlier.device_ids()
    monkeypatch.setattr(remote_server, "_runtime", None)  # this TUI reads the file afresh

    def unwritable(path: Path, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied", str(path))

    controller = RemoteController(
        tunnel_factory=fake_tunnel_factory(url=STARTED["url"]),
        dist_dir=page,
        port=_free_port(),
        state=RemoteState(remote_enabled=True),
    )
    with pytest.MonkeyPatch.context() as home:
        home.setattr(remote_server, "replacement", unwritable)
        controller.restore()
    assert not controller.running and remote_server._server is None
    assert (controller.message or "").startswith(
        "Remote could not start — remote.json could not be written: [Errno 13] Permission denied"
    )

    controller.turn_on()
    assert controller._waiter is not None
    controller._waiter.join(5)  # the URL lands, and its thread's word on the status line first
    assert controller.running and controller.message is None
    passphrase = controller.password()
    with pytest.MonkeyPatch.context() as home:
        home.setattr(remote_server, "replacement", unwritable)
        controller.set_auto_off(30)
        problem = controller.save_problem or ""
        assert problem.startswith("auto-off could not be saved to remote.json")
        assert controller.revoke_device(device_id) is False
        assert (controller.save_problem or "").startswith(f"{device_id} could not be revoked")
        assert controller.devices() == [], "signed out of the running server all the same"
        assert controller.regenerate_password() is None
        assert (controller.save_problem or "").startswith("the new password could not be saved")
        assert controller.password() != passphrase, "the running server has the new one"
        controller.set_allow_write(True)
        assert (controller.save_problem or "").startswith(
            "write actions could not be saved to remote.json"
        )
        assert controller.write_actions_allowed() is True
        assert controller.running
        controller.shutdown_for_exit()
    assert remote_server._server is None


def test_the_panel_takes_the_files_deadline_once_the_real_server_reads_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real module and a clock with microseconds. A timer ``remote.json`` would not take
    is the running server's (its state changes before the rename fails), and a shell's
    ``allow-write``, rewriting the file, makes it read the file again and go back to the
    hour the file holds: every phone is answered 404 from then on. The panel had taken the
    server's copy of its own deadline, cut to the second, for the file's, and went on
    showing Remote on for the hour more (r2 verification of #243)."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("<!doctype html>", encoding="utf-8")
    start = datetime.now(UTC).replace(microsecond=250_000)
    controller = RemoteController(
        tunnel_factory=fake_tunnel_factory(url=STARTED["url"]),
        dist_dir=page,
        port=_free_port(),
        now=lambda: start,
        state=RemoteState(auto_off_minutes=60),
    )
    controller.turn_on()
    in_the_file = remote_server.remote_auto_off_at()
    assert in_the_file is not None

    def unwritable(path: Path, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied", str(path))

    with pytest.MonkeyPatch.context() as home:
        home.setattr(remote_server, "replacement", unwritable)
        controller.set_auto_off(120)
    picked = in_the_file + timedelta(hours=1)
    assert remote_server.remote_auto_off_at() == picked, "the running server took it"
    assert controller.adopt_server_deadline() == picked, "and the panel agrees with its gate"
    shell = remote_server.Runtime(paths.remote_state_path(), paths.remote_audit_path())
    shell.set_allow_write(True)  # another process: it writes the file as it found it
    assert remote_server.runtime().auto_off_passed(in_the_file), "phones get 404 from then on"
    assert controller.adopt_server_deadline() == in_the_file
    controller.shutdown_for_exit()


def test_status_prints_the_port_the_panel_serves_on_when_one_is_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` and ``regenerate-password`` read ``AISQUARE_REMOTE_PORT`` for the link they
    print, as ``serve`` reads it for its port; the panel always served on 8750, so with the
    variable exported, ``status`` printed a port the TUI was not on (r2 smoke of #243)."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("<!doctype html>", encoding="utf-8")
    port = _free_port()
    monkeypatch.setenv("AISQUARE_REMOTE_PORT", str(port))
    tunnels: list[int] = []

    def tunnel_factory(on: int) -> FakeTunnel:
        tunnels.append(on)
        return FakeTunnel(on, url=None, failure=INSTALL_HINT)

    controller = RemoteController(tunnel_factory=tunnel_factory, dist_dir=page)
    controller.turn_on()
    try:
        served = remote_server._server
        assert controller.running and served is not None and served.port == port
        link = controller.link_url()
        assert link is not None and link.startswith(f"http://127.0.0.1:{port}/r/")
        assert tunnels == [port], "ngrok forwards to the port served"
        status = CliRunner().invoke(cli, ["--json", "remote", "status"])
        assert status.exit_code == 0, status.output
        assert json.loads(status.stdout)["url_local"] == link
    finally:
        controller.shutdown_for_exit()


def test_a_port_the_panel_cannot_use_is_a_sentence_and_unset_is_the_default() -> None:
    """An ``AISQUARE_REMOTE_PORT`` that is no port is a sentence on the status line, never
    Remote served on some other port. Unset, it is 8750; a port the caller gives wins."""
    server = fake_server()
    no_ngrok = fake_tunnel_factory(failure=INSTALL_HINT)
    default = RemoteController(server=server, tunnel_factory=no_ngrok)
    default.turn_on()
    assert default.link_url() == f"http://127.0.0.1:8750/r/{server.token}/"
    default.turn_off()
    for raw in ("eighteen", "0", "70000", "-1"):
        with pytest.MonkeyPatch.context() as env:
            env.setenv("AISQUARE_REMOTE_PORT", raw)
            refused = RemoteController(server=server, tunnel_factory=no_ngrok)
            refused.turn_on()
            assert not refused.running and not server.running, raw
            assert refused.message == (
                f"Remote could not start — AISQUARE_REMOTE_PORT is {raw!r}, not a port"
            )
            given = RemoteController(server=server, tunnel_factory=no_ngrok, port=18999)
            given.turn_on()
            assert given.link_url() == f"http://127.0.0.1:18999/r/{server.token}/", raw
            given.turn_off()


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "70000"])
def test_a_port_no_command_can_use_is_a_usage_error_never_a_dead_link_or_a_traceback(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel refuses an ``AISQUARE_REMOTE_PORT`` that is no port (the test above), and
    every command reads the same variable: ``serve --port 0`` served on a port the system
    picked while its banner printed ``:0`` links that refused every connection, ``status``
    printed ``:70000``, and ``serve --port 70000`` ended in an ``OverflowError`` traceback
    with nothing on stdout under ``--json`` (sweep of #243). The panel's port, and only
    that, is every command's."""

    def served(*args: object, **kwargs: object) -> bool:
        raise AssertionError(f"served on port {raw}")

    monkeypatch.setattr(remote_server, "run_foreground", served)
    runner = CliRunner()
    flagged = [
        ["remote", "serve", "--port", raw],
        ["remote", "status", "--port", raw],
        ["remote", "regenerate-password", "--new-link", "--port", raw],
    ]
    exported = [["remote", "serve"], ["remote", "status"]]
    for command, exports in [(c, {}) for c in flagged] + [(c, {PORT_ENV: raw}) for c in exported]:
        result = runner.invoke(cli, ["--json", *command], env=exports)
        assert result.exit_code == 2, (command, exports, result.output)
        answer = json.loads(result.stdout)
        assert answer["error"] == "usage", (command, exports)
        assert "not in the range 1<=x<=65535" in answer["message"], (command, exports)
    assert not paths.remote_state_path().exists(), "refused before anything was read or made"
    with pytest.MonkeyPatch.context() as env:
        env.setenv(PORT_ENV, raw)
        assert RemoteController(server=fake_server())._port_problem is not None, "the panel too"


def test_an_auto_off_past_a_week_is_a_usage_error_never_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve --auto-off 99999999999`` (or the variable) ended in an ``OverflowError``
    traceback, a date past year 9999 (sweep of #243). A week, the longest a phone stays
    signed in, is the most; ``0`` is never."""
    timers: list[object] = []

    def served(dist: object, port: int, auto_off: int, *args: object, **kwargs: object) -> bool:
        timers.append(auto_off)
        return False

    monkeypatch.setattr(remote_server, "run_foreground", served)
    week = remote_cli.MAX_AUTO_OFF_MINUTES
    assert timedelta(minutes=week) == remote_server.DEVICE_LIFETIME, "the longest sign-in"
    runner = CliRunner()
    for raw in ("10081", "99999999999"):
        flagged = runner.invoke(cli, ["--json", "remote", "serve", "--auto-off", raw])
        exported = runner.invoke(cli, ["--json", "remote", "serve"], env={AUTO_OFF_ENV: raw})
        for result in (flagged, exported):
            assert result.exit_code == 2, result.output
            assert "not in the range 0<=x<=10080" in json.loads(result.stdout)["message"]
    for raw in ("10080", "0"):
        assert runner.invoke(cli, ["remote", "serve", "--auto-off", raw]).exit_code == 0
    assert timers == [10080, 0]


def test_serve_says_to_turn_a_hand_started_ngroks_local_api_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve`` gives the ngrok command to run beside it, with its inspector off; the agent
    API on the same port, which starts and stops tunnels for any user of the machine, is
    turned off only in ngrok's config, and the banner says so (sweep of #243)."""

    def served(dist: object, port: int, auto_off: int, *args: object, **kwargs: Any) -> bool:
        kwargs["ready"]()
        return False

    monkeypatch.setattr(remote_server, "run_foreground", served)
    result = CliRunner().invoke(cli, ["remote", "serve", "--port", "9004"])
    assert result.exit_code == 0, result.output
    assert "ngrok http 9004 --inspect=false" in result.output
    assert "web_addr: false in ngrok.yml" in result.output


# --- another process serving this home (sweep of #243) ----------------------------------------


@contextlib.contextmanager
def another_process_serves() -> Iterator[Callable[[], None]]:
    """``remote-serve.lock`` held as another Remote's process holds it: through a descriptor
    of its own, which conflicts with this process's as another process's does. Yields what
    lets it go, at most once, from any thread: Windows' ``locking`` raises for a lock let go
    already, as ``flock`` does not."""
    paths.ensure_home()
    path = paths.remote_state_path().with_name(remote_server.SERVE_LOCK_NAME)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    once = threading.Lock()

    def release() -> None:
        if once.acquire(blocking=False):
            unlock(fd)

    try:
        lock_exclusive(fd)
        try:
            yield release
        finally:
            release()
    finally:
        os.close(fd)


def test_whether_another_process_serves_this_home_is_asked_of_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel said Remote was off while ``serve`` served this home: nothing asked the lock
    that keeps two Remotes off one home. Asking it takes the lock for a moment, which a claim
    made in that moment waits out rather than refusing."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    assert remote_server.remote_served_elsewhere() is False, "no lock file: nobody serves"
    with another_process_serves():
        assert remote_server.remote_served_elsewhere() is True
    assert remote_server.remote_served_elsewhere() is False
    state = remote_server.runtime()
    with another_process_serves() as release:
        threading.Timer(0.01, release).start()  # a probe, holding it for a moment
        try:
            assert remote_server._claim_remote_home(state) is True, "the probe failed a claim"
            assert remote_server.remote_served_elsewhere() is False, "this process serves"
        finally:
            monkeypatch.setattr(remote_server, "_server", None)
            remote_server._release_remote_home()


def test_the_panel_knows_when_another_process_serves_this_home() -> None:
    """Looked for off Textual's thread, at most every few seconds; a start's sentence that
    another Remote kept it off goes once the home is free."""
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    server.served_elsewhere = True
    looked: list[str] = []
    answer = server.remote_served_elsewhere

    def look() -> bool:
        looked.append(threading.current_thread().name)
        return answer()

    server.remote_served_elsewhere = look  # type: ignore[method-assign]

    def found() -> bool:
        deadline = time.monotonic() + 5
        while controller._elsewhere_looking and time.monotonic() < deadline:
            time.sleep(0.01)
        return controller.elsewhere

    controller.served_elsewhere()
    assert found() is True and controller.served_elsewhere() is True
    controller.served_elsewhere()
    assert looked == ["remote-elsewhere"], "on a thread of its own, at most every few seconds"
    server.fail_start = remote_server.RemoteAlreadyOn(remote_server.REMOTE_ALREADY_ON)
    controller.turn_on()
    assert controller.message == remote_control.ALREADY_ON
    assert controller.password() == server.password, "the Remote that is on is that one's"
    server.served_elsewhere = False
    controller._elsewhere_at = None  # a few seconds on
    controller.served_elsewhere()
    assert found() is False and controller.message is None, "the home is free now"
    assert controller.password() is None
    server.fail_start = None
    controller.turn_on()
    server.served_elsewhere = True
    controller._elsewhere_at = None
    assert controller.served_elsewhere() is False, "this one serves"
    controller._look_elsewhere()  # a look that found this one's own claim, as it started
    assert controller.elsewhere is False
    controller.elsewhere = True
    assert controller.served_elsewhere() is False and controller.elsewhere is False
    controller.turn_off()


def test_status_says_whether_remote_is_on_for_this_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """``asq remote status`` printed the link, the passphrase and the devices, and nothing of
    whether any process served them: a human checking that the fleet was not exposed had
    no way to tell from it, as from the R panel (sweep of #243)."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    runner = CliRunner()
    off = runner.invoke(cli, ["--json", "remote", "status"])
    assert off.exit_code == 0 and json.loads(off.stdout)["serving"] is False
    assert "remote:      off" in runner.invoke(cli, ["remote", "status"]).stdout
    with another_process_serves():
        on = runner.invoke(cli, ["--json", "remote", "status"])
        assert on.exit_code == 0 and json.loads(on.stdout)["serving"] is True
        human = runner.invoke(cli, ["remote", "status"]).stdout
        assert "remote:      on — a process serves this home" in human
