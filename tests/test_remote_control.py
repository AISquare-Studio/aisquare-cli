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

import json
import sys
import types
from datetime import datetime, timedelta
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
from aisquare.cli.watch import _load_saved_theme, _read_state, _save_theme
from aisquare.core import paths
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
    assert build_public_url(host, "tok_123") == "https://abcd-12.ngrok-free.app/r/tok_123"


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
    assert controller.link_url() == f"https://abcd-12.ngrok-free.app/r/{server.token}"
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
    assert ngrok_command(8748) == ["ngrok", "http", "8748", "--log=stdout", "--log-format=json"]


# --- the missing binary -------------------------------------------------------------------


def test_missing_binary_message_names_the_install_and_the_authtoken() -> None:
    message = missing_binary_message()
    assert message == INSTALL_HINT
    assert "ngrok is not installed" in message
    assert "https://ngrok.com/download" in message
    assert "ngrok config add-authtoken" in message


def test_a_tunnel_without_the_binary_reports_instead_of_raising() -> None:
    tunnel = NgrokTunnel(8748, which=lambda _name: None)
    assert tunnel.start() == INSTALL_HINT
    assert tunnel.error == INSTALL_HINT
    assert not tunnel.running
    tunnel.stop()  # idempotent with nothing spawned


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
    tunnel = NgrokTunnel(8748, command=fake_ngrok(tmp_path, {"lvl": "info", "msg": "hi"}, STARTED))
    assert tunnel.start() is None
    assert tunnel.wait_for_url(timeout=10) == STARTED["url"]
    assert tunnel.running
    tunnel.stop()
    assert not tunnel.running
    assert tunnel.error is None


def test_a_tunnel_that_exits_without_a_url_says_so_instead_of_hanging(tmp_path: Path) -> None:
    error = {"lvl": "eror", "err": "authentication failed: ERR_NGROK_4018"}
    tunnel = NgrokTunnel(8748, command=fake_ngrok(tmp_path, error, linger=False))
    assert tunnel.start() is None
    assert tunnel.wait_for_url(timeout=10) is None
    assert tunnel.error == AUTHTOKEN_HINT
    tunnel.stop()


# --- the controller -------------------------------------------------------------------------


class FakeServer(types.ModuleType):
    """PLAN §4-F's six names, recording every call; sessions are a plain list."""

    def __init__(self) -> None:
        super().__init__("fake_remote_server")
        self.token = "tok_TEST"
        self.password = "amber-birch-cedar-delta"
        self.running = False
        self.allow_write_calls: list[bool] = []
        self.revoked: list[str] = []
        self.sessions: list[dict[str, Any]] = []
        self.DEFAULT_PORT = 8748
        self.RemoteInfo = remote_server.RemoteInfo

    def start(self, dist_dir: Path | None, port: int = 8748) -> remote_server.RemoteInfo:
        self.running = True
        return remote_server.RemoteInfo(
            self.token, self.password, f"http://127.0.0.1:{port}/r/{self.token}"
        )

    def stop(self) -> None:
        self.running = False

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "sessions": list(self.sessions)}

    def revoke(self, sid: str) -> None:
        self.revoked.append(sid)
        self.sessions = [s for s in self.sessions if s.get("sid") != sid]

    def set_allow_write(self, enabled: bool) -> None:
        self.allow_write_calls.append(enabled)

    def regenerate_password(self) -> str:
        self.password = "ember-fjord-glade-harbor"
        return self.password


def fake_server() -> FakeServer:
    return FakeServer()


class FakeTunnel(NgrokTunnel):
    """An ``NgrokTunnel`` that never spawns: ``url`` arrives at once, or ``failure`` is returned."""

    def __init__(self, port: int, *, url: str | None, failure: str | None) -> None:
        super().__init__(port, which=lambda _name: None)
        self._fake_url = url
        self._failure = failure
        self.stopped = False

    def start(self) -> str | None:
        if self._failure is not None:
            self.error = self._failure
            return self._failure
        self.public_url = self._fake_url
        self._url_ready.set()
        return None

    def stop(self) -> None:
        self.stopped = True


def fake_tunnel_factory(
    *, url: str | None = None, failure: str | None = None
) -> remote_control.TunnelFactory:
    return lambda port: FakeTunnel(port, url=url, failure=failure)


def test_fresh_state_is_off_read_only_and_one_hour() -> None:
    assert load_remote_state() == RemoteState(
        remote_enabled=False, allow_write=False, auto_off_minutes=60
    )
    assert "allow write actions is off" in READ_ONLY_REASON


def test_turn_on_starts_the_server_hands_it_allow_write_false_and_persists() -> None:
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x.app"))
    controller.turn_on()
    assert server.running
    assert server.allow_write_calls == [False]  # never on by default
    assert controller.password() == server.password
    assert controller.state.remote_enabled is True
    assert _read_state()["remote_enabled"] is True
    assert _read_state()["allow_write"] is False
    controller.turn_off()
    assert not server.running
    assert controller.link_url() is None
    assert _read_state()["remote_enabled"] is False


def test_without_ngrok_remote_is_on_locally_and_the_status_line_says_how_to_install() -> None:
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(failure=missing_binary_message())
    )
    controller.turn_on()
    assert controller.running and server.running
    assert controller.tunnel is None
    assert controller.message == INSTALL_HINT
    assert controller.link_url() == f"http://127.0.0.1:8748/r/{server.token}"  # PLAN §6 fallback


def test_the_switches_survive_a_restart_of_the_tui_next_to_the_theme_key() -> None:
    _save_theme("nord")
    first = RemoteController(server=fake_server(), tunnel_factory=fake_tunnel_factory(url="x"))
    first.set_allow_write(True)
    first.set_auto_off(120)
    first.turn_on()
    first.shutdown()  # the TUI exits: processes end, the saved switches stay
    saved = json.loads(paths.state_path().read_text())
    assert saved["board_theme"] == "nord"  # the theme key is untouched by our merge
    assert (saved["remote_enabled"], saved["allow_write"], saved["auto_off_minutes"]) == (
        True,
        True,
        120,
    )
    assert _load_saved_theme() == "nord"

    server = fake_server()
    second = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert second.state == RemoteState(remote_enabled=True, allow_write=True, auto_off_minutes=120)
    assert not second.running
    second.restore()
    assert second.running
    assert server.allow_write_calls == [True]  # the user's saved choice, not a default


def test_restore_leaves_a_remote_that_was_off_alone() -> None:
    server = fake_server()
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    controller.restore()
    assert not controller.running and not server.running


def test_auto_off_turns_remote_off_when_the_timer_runs_out() -> None:
    clock = [datetime(2026, 9, 11, 18, 0)]
    server = fake_server()
    controller = RemoteController(
        server=server, tunnel_factory=fake_tunnel_factory(url="x"), now=lambda: clock[0]
    )
    controller.set_auto_off(30)
    controller.turn_on()
    assert controller.auto_off_at == datetime(2026, 9, 11, 18, 30)
    clock[0] += timedelta(minutes=29)
    assert controller.enforce_auto_off() is False and controller.running
    clock[0] += timedelta(minutes=1)
    assert controller.enforce_auto_off() is True
    assert not controller.running and not server.running
    assert controller.message is not None and "auto-off" in controller.message
    with pytest.raises(ValueError):
        controller.set_auto_off(45)


def test_regenerate_devices_and_revoke_go_through_the_server() -> None:
    server = fake_server()
    server.sessions = [
        {"sid": "sid_a", "ua": "iPhone", "first_seen": "t0", "last_seen": "t1"},
        {"sid": "sid_b", "ua": "Pixel", "first_seen": "t0", "last_seen": "t1"},
        {"broken": True},
    ]
    controller = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    assert controller.regenerate_password() is None  # off: nothing to unlock
    controller.turn_on()
    assert controller.regenerate_password() == "ember-fjord-glade-harbor"
    assert controller.password() == "ember-fjord-glade-harbor"
    assert [d["sid"] for d in controller.devices()] == ["sid_a", "sid_b"]
    controller.revoke("sid_a")
    assert server.revoked == ["sid_a"]
    assert [d["sid"] for d in controller.devices()] == ["sid_b"]


# --- the stub server module keeps remote.json in the PLAN §1 shape -----------------------------


def test_the_stub_server_mints_token_and_password_and_keeps_remote_json_private() -> None:
    info = remote_server.start(None)
    path = remote_server.remote_state_path()
    assert path.parent == paths.aisquare_home()
    assert (path.stat().st_mode & 0o777) == 0o600
    data = json.loads(path.read_text())
    assert set(data) == {"token", "password", "allow_write", "auto_off_at", "sessions"}
    assert data["allow_write"] is False
    assert info.token == data["token"] and info.password == data["password"]
    assert info.url_local == f"http://127.0.0.1:8748/r/{info.token}"
    assert len(info.password.split("-")) == 4
    assert remote_server.status()["running"] is True
    assert remote_server.start(None).token == info.token  # reused, not re-minted
    new = remote_server.regenerate_password()
    assert new != info.password and json.loads(path.read_text())["password"] == new
    remote_server.set_allow_write(True)
    assert json.loads(path.read_text())["allow_write"] is True
    remote_server.set_allow_write(False)
    data = json.loads(path.read_text())
    data["sessions"] = [{"sid": "s1", "ua": "x", "first_seen": "a", "last_seen": "b"}]
    path.write_text(json.dumps(data))
    assert remote_server.status()["sessions"][0]["sid"] == "s1"
    remote_server.revoke("s1")
    assert remote_server.status()["sessions"] == []
    remote_server.stop()
    assert remote_server.status()["running"] is False
