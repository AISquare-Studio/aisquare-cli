"""The tunnel's side of Web Push (SPEC §5.8): a static ngrok domain, and ngrok's own word.

No test here reaches the network: ngrok's agent API is a canned answer each test
hands in, and an autouse guard stands in for the real one, so a test that forgot
fails instead of asking whatever listens on 127.0.0.1:4040.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.services import ngrok_tunnel
from aisquare.services.ngrok_tunnel import (
    TOO_OLD_HINT,
    NgrokTunnel,
    discover_ngrok_public_url,
    ngrok_command,
    ngrok_static_host,
    parse_log_line,
)

# --- stand-ins and guards ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_ngrok_agent_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """ngrok's real agent API, replaced by a refusal the test fails on: a test that forgot to
    hand in its own answer is red, never a request to whatever listens on 127.0.0.1:4040."""
    reached: list[str] = []

    def refuse_agent_api(url: str, timeout: float) -> bytes:
        reached.append(url)
        raise OSError("a test reached ngrok's real agent API")

    monkeypatch.setattr(ngrok_tunnel, "_ngrok_agent_api_get", refuse_agent_api)
    yield reached
    assert reached == [], f"a test reached the network: {reached}"


# --- the public URL (SPEC §5.8, §5.10 item 9) -------------------------------------------------


TUNNELS = {
    "tunnels": [
        {"public_url": "http://abcd-12.ngrok-free.app", "proto": "http",
         "config": {"addr": "http://localhost:8750"}},
        {"public_url": "https://other.ngrok-free.app", "proto": "https",
         "config": {"addr": "http://localhost:18750"}},
        {"public_url": "https://abcd-12.ngrok-free.app", "proto": "https",
         "config": {"addr": "http://localhost:8750"}},
    ]
}  # fmt: skip


def test_discovery_picks_ngroks_https_tunnel_to_this_port() -> None:
    asked: list[tuple[str, float]] = []

    def agent_api(url: str, timeout: float) -> bytes:
        asked.append((url, timeout))
        return json.dumps(TUNNELS).encode()

    assert discover_ngrok_public_url(8750, fetch=agent_api) == "https://abcd-12.ngrok-free.app"
    assert asked == [("http://127.0.0.1:4040/api/tunnels", 1.0)], "loopback, one second"
    assert discover_ngrok_public_url(18750, fetch=agent_api) == "https://other.ngrok-free.app"
    assert discover_ngrok_public_url(750, fetch=agent_api) is None, "':750' is not ':8750'"


@pytest.mark.parametrize(
    "answer",
    [b"<html>ngrok is not running</html>", b"[]", b'{"tunnels": "none"}', b'{"tunnels": [1]}'],
)
def test_discovery_says_none_for_anything_but_a_tunnel_list(answer: bytes) -> None:
    assert discover_ngrok_public_url(8750, fetch=lambda url, timeout: answer) is None


def test_discovery_without_ngrok_running_says_none() -> None:
    def refused(url: str, timeout: float) -> bytes:
        raise ConnectionRefusedError("nothing listens on 4040")

    assert discover_ngrok_public_url(8750, fetch=refused) is None


# --- ngrok: the static domain and the old-ngrok hint (SPEC §5.8, §5.10 item 12) ---------------


def _recording_popen(spawned: list[list[str]]) -> Callable[..., subprocess.Popen[str]]:
    def popen(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        spawned.append(list(command))
        return subprocess.Popen([sys.executable, "-c", "pass"], **kwargs)

    return popen


def test_ngrok_is_told_the_static_domain_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISQUARE_REMOTE_NGROK_URL", "https://remote-anmol.ngrok-free.app/")
    spawned: list[list[str]] = []
    tunnel = NgrokTunnel(
        8750, which=lambda _name: "/usr/bin/ngrok", popen=_recording_popen(spawned)
    )
    assert tunnel.start_tunnel() is None
    tunnel.wait_for_url(5)
    tunnel.stop_tunnel()
    assert spawned == [
        ["ngrok", "http", "8750", "--log=stdout", "--log-format=json",
         "--url=remote-anmol.ngrok-free.app"]
    ]  # fmt: skip


def test_without_a_static_domain_ngrok_picks_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISQUARE_REMOTE_NGROK_URL", raising=False)
    spawned: list[list[str]] = []
    tunnel = NgrokTunnel(
        8750, which=lambda _name: "/usr/bin/ngrok", popen=_recording_popen(spawned)
    )
    assert tunnel.start_tunnel() is None
    tunnel.wait_for_url(5)
    tunnel.stop_tunnel()
    assert spawned == [ngrok_command(8750)]
    assert tunnel.static_host is None


@pytest.mark.parametrize(
    ("written", "host"),
    [
        ("remote-anmol.ngrok-free.app", "remote-anmol.ngrok-free.app"),
        ("https://remote-anmol.ngrok-free.app", "remote-anmol.ngrok-free.app"),
        ("https://Remote-Anmol.ngrok-free.app/r/x/", "remote-anmol.ngrok-free.app"),
        ("  remote-anmol.ngrok-free.app/  ", "remote-anmol.ngrok-free.app"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_a_static_domain_is_read_however_it_was_written(
    written: str | None, host: str | None
) -> None:
    assert ngrok_static_host(written) == host


def test_an_ngrok_too_old_for_url_says_to_update_it(tmp_path: Path) -> None:
    assert parse_log_line("ERROR:  unknown flag: --url\n").error == TOO_OLD_HINT
    assert parse_log_line("Incorrect Usage. flag provided but not defined: -url").error == (
        TOO_OLD_HINT
    )
    assert parse_log_line("ERROR:  unknown flag: --log-format").error is None, "not ours"
    assert parse_log_line("t=2026 lvl=info msg=hello").error is None
    script = tmp_path / "old-ngrok.py"
    script.write_text(
        "import sys\nprint('ERROR:  unknown flag: --url', file=sys.stderr)\nsys.exit(1)\n"
    )
    tunnel = NgrokTunnel(8750, command=[sys.executable, str(script)])
    assert tunnel.start_tunnel() is None
    assert tunnel.wait_for_url(10) is None
    assert tunnel.error == TOO_OLD_HINT
    tunnel.stop_tunnel()
