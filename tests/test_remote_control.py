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

import inspect
import json
import socket
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.cli.ui import remote_control
from aisquare.cli.ui.remote_control import (
    READ_ONLY_REASON,
    RemoteController,
    RemoteState,
    load_remote_state,
)
from aisquare.cli.watch import _load_saved_theme
from aisquare.core import paths
from aisquare.core.state_file import read_state, update_state
from aisquare.services import ngrok_tunnel, remote_server
from aisquare.services.ngrok_tunnel import (
    AUTHTOKEN_HINT,
    INSTALL_HINT,
    NgrokTunnel,
    build_public_url,
    missing_binary_message,
    ngrok_command,
    parse_log_line,
)

STARTED = {"lvl": "info", "msg": "started tunnel", "url": "https://abcd-12.ngrok-free.app"}

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
    assert parse_log_line("not json at all") == ngrok_tunnel.LogEvent()
    assert parse_log_line("[1, 2, 3]") == ngrok_tunnel.LogEvent()


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


def fake_ngrok(tmp_path: Path, *lines: dict[str, Any], linger: bool = True) -> list[str]:
    """A command that prints ``lines`` as ngrok's JSON log would, then (optionally) stays up."""
    script = tmp_path / "fake-ngrok.py"
    lines_out = [f"print({json.dumps(json.dumps(line))}, flush=True)" for line in lines]
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
    error = {"lvl": "eror", "err": "authentication failed: ERR_NGROK_4018"}
    tunnel = NgrokTunnel(8750, command=fake_ngrok(tmp_path, error, linger=False))
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(timeout=10) is None
    assert tunnel.error == AUTHTOKEN_HINT
    tunnel.stop_tunnel()


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
        """What ``remote_auto_off_at()`` answers: the last ``set_auto_off``, or a later
        deadline a test sets to stand for one the phone extended."""
        self.calls: list[str] = []
        self.DEFAULT_PORT = 8750
        self.RemoteInfo = remote_server.RemoteInfo

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

    def set_allow_write(self, enabled: bool) -> None:
        self.allow_write_calls.append(enabled)
        self.allow_write = enabled

    def set_auto_off(self, at: datetime | None) -> None:
        self.auto_off_calls.append(at)
        self.server_auto_off_at = at

    def regenerate_password(self, new_link: bool = False) -> str:
        self.password = "ember-glade-heron-indigo"
        return self.password

    def revoke_every_remote_device(self, reason: str) -> None:
        self.calls.append("revoke_every_remote_device")
        self.revoked_every.append(reason)
        self.devices = []

    def note_public_url(self, url: str | None) -> None:
        self.calls.append("note_public_url")
        self.public_urls.append(url)

    def remote_auto_off_at(self) -> datetime | None:
        return self.server_auto_off_at

    def remote_allow_write(self) -> bool:
        return self.allow_write

    def remote_password(self) -> str:
        return self.password


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
    controller.revoke_device("dev_0000000a")
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
