"""The Remote Control server (PLAN §4): gates, JSON parity, the stream, the module API."""

from __future__ import annotations

import json
import logging
import socket
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.services import remote_server
from aisquare.services.remote_server import (
    COOKIE,
    READ_ONLY_REASON,
    WRITE_ENDPOINTS,
    NoSuchAgent,
    RequestError,
    Runtime,
    Sources,
    Writes,
    build_app,
)
from tests.remote_kit_helpers import make_client

PASSWORD = "Test1234"


# --- fixtures -------------------------------------------------------------------------


class Fake:
    """Fake sources: a mutable board so the stream has something to notice."""

    def __init__(self) -> None:
        self.board: dict[str, object] = {"project": {"id": "p1"}, "tasks": [], "events": []}
        self.fleet: dict[str, object] = {"name": "demo", "agents": []}
        self.pane_calls: list[tuple[str, str | None, int]] = []
        self.fleet_calls: list[str | None] = []
        self.written: list[tuple[str, dict[str, Any]]] = []

    def sources(self) -> Sources:
        def fleet(project: str | None = None) -> object:
            self.fleet_calls.append(project)
            return self.fleet

        def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
            self.pane_calls.append((agent, project, history))
            if agent == "ghost":
                raise NoSuchAgent("no live agent 'ghost'")
            return {
                "rows": [f"\x1b[32m{agent}\x1b[0m $ "],
                "cursor": [3, 0],
                "width": 80,
                "height": 1,
            }

        return Sources(
            projects=lambda: [{"id": "p1", "name": "demo"}],
            fleet=fleet,
            board=lambda project: self.board,
            tasks=lambda project: [{"id": "t1", "title": "ship it"}],
            memory=lambda project: [{"id": "m1", "text": "remember"}],
            panes=panes,
        )

    def writes(self) -> Writes:
        def handler(name: str) -> remote_server.WriteHandler:
            def run(body: dict[str, Any]) -> tuple[dict[str, object], str]:
                self.written.append((name, body))
                if body.get("boom"):
                    raise RequestError(422, "refused", "the fake said no")
                return {"ok": True, "endpoint": name}, f"summary of {name}"

            return run

        return Writes({name: handler(name) for name in WRITE_ENDPOINTS})


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    rt = Runtime(remote_state_path(), remote_audit_path())
    # A known password for the tests; the file keeps the generated token.
    rt._state.password = PASSWORD
    rt._save_state()
    return rt


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>asq remote</title>")
    (root / "assets" / "app.js").write_text("console.log('hi')")
    return root


@pytest.fixture
def client(runtime: Runtime, fake: Fake, dist: Path) -> TestClient:
    app = build_app(runtime, sources=fake.sources(), writes=fake.writes(), dist_dir=dist, tick=0.02)
    return make_client(app)


def base(runtime: Runtime) -> str:
    return f"/r/{runtime.token}"


def unlock(client: TestClient, runtime: Runtime, password: str = PASSWORD) -> Any:
    return client.post(f"{base(runtime)}/api/unlock", json={"password": password})


# --- state file -----------------------------------------------------------------------


def test_state_file_is_created_with_write_off(runtime: Runtime) -> None:
    path = remote_state_path()
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) == {
        "version",
        "token",
        "password",
        "allow_write",
        "auto_off_at",
        "devices",
        "unlock_failures",
    }
    assert raw["version"] == 2 and raw["allow_write"] is False
    assert len(raw["token"]) == 32
    assert raw["devices"] == [] and raw["unlock_failures"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_state_file_is_0600(runtime: Runtime) -> None:
    assert stat.S_IMODE(remote_state_path().stat().st_mode) == 0o600


def test_both_files_are_restricted_to_the_owner_before_they_hold_anything(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every platform, Windows included, where the 0600 above is skipped: there a new file
    starts with the DACL its directory hands down, so the restriction is the whole
    protection. ``remote.json``'s temp is restricted while still empty, before each write;
    the audit log once, when it is created, before its first line."""
    from aisquare.core import paths

    restricted: list[tuple[str, int]] = []
    real = paths.restrict_to_owner

    def spy(path: Path) -> bool:
        restricted.append((path.name, path.stat().st_size))
        return real(path)

    monkeypatch.setattr(paths, "restrict_to_owner", spy)
    monkeypatch.setattr(remote_server, "restrict_to_owner", spy)
    runtime.set_allow_write(True)
    runtime.audit("sid", "note", "x")
    runtime.audit("sid", "note", "y")
    assert len(restricted) == 2, restricted  # the log once, not once per line
    (temp, temp_size), (log, log_size) = restricted
    assert temp.startswith(f".{remote_state_path().name}.") and temp_size == 0
    assert log == remote_audit_path().name and log_size == 0
    assert len(remote_audit_path().read_bytes().splitlines()) == 2


def test_a_restriction_that_fails_is_said_once_not_at_every_flush(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A serving process rewrites ``remote.json`` every 30 s, so a warning per write would
    repeat for as long as it serves wherever ``icacls`` cannot run. The file is still written."""
    from aisquare.core import paths

    monkeypatch.setattr(paths, "restrict_to_owner", lambda path: False)
    with caplog.at_level(logging.WARNING, logger=remote_server.__name__):
        for _ in range(3):
            runtime.flush_last_seen()
        runtime.set_allow_write(True)
    said = [r.getMessage() for r in caplog.records if r.name == remote_server.__name__]
    assert len(said) == 1 and "could not restrict" in said[0], said
    assert str(remote_state_path()) in said[0]
    assert json.loads(remote_state_path().read_bytes())["allow_write"] is True


def test_state_survives_a_reload(runtime: Runtime) -> None:
    runtime.set_allow_write(True)
    again = Runtime(remote_state_path(), remote_audit_path())
    assert again.token == runtime.token
    assert again.password == PASSWORD
    assert again.allow_write is True


@pytest.mark.parametrize(
    "body", ["{not json", "[1, 2]", '"remote"', '{"version": 2, "token": "kept"'], ids=repr
)
def test_a_corrupt_state_file_is_refused_and_left_as_it_is(isolated_home: Path, body: str) -> None:
    """Replacing it meant a new link and passphrase, and writes off: every phone lost Remote
    to one typo in a hand edit, at the first process that so much as read the file."""
    path = remote_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    with pytest.raises(remote_server.RemoteError, match="is not a JSON object"):
        Runtime(path, remote_audit_path())
    assert path.read_text() == body


@pytest.mark.parametrize("body", [b"", b" \n\t", b"\x00" * 512], ids=["empty", "blank", "NULs"])
def test_an_empty_state_file_is_made_anew(isolated_home: Path, body: bytes) -> None:
    """Nothing in it to keep: what a crash leaves when the size reached the disk and the
    data did not is a run of NULs."""
    path = remote_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    rt = Runtime(path, remote_audit_path())
    assert len(rt.token) == 32 and rt.allow_write is False
    assert json.loads(path.read_bytes())["token"] == rt.token


# --- the token gate (§4-C) --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/r/", "/r/wrong-token/", "/r/wrong-token/api/board", "/api/board", "/r/wrong/ws"],
)
def test_wrong_or_missing_token_is_404_everywhere(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_wrong_token_on_the_websocket_is_404(client: TestClient) -> None:
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/r/nope/ws"):
        pass
    assert denied.value.status_code == 404


def test_the_token_is_compared_whole(client: TestClient, runtime: Runtime) -> None:
    prefix = runtime.token[:-1]
    assert client.get(f"/r/{prefix}/api/remote").status_code == 404
    assert client.get(f"/r/{runtime.token}x/api/remote").status_code == 404


# --- unlock + cookie (§4-C) -------------------------------------------------------------


def test_unlock_wrong_password_is_401(client: TestClient, runtime: Runtime) -> None:
    response = unlock(client, runtime, "nope")
    assert response.status_code == 401
    assert response.json()["error"] == "wrong_password"
    assert COOKIE not in response.cookies


def test_unlock_sets_an_httponly_lax_cookie(client: TestClient, runtime: Runtime) -> None:
    response = unlock(client, runtime)
    assert response.status_code == 200
    (device,) = runtime.device_rows()
    assert response.json() == {
        "ok": True,
        "device": {"id": device["id"], "expires_at": device["expires_at"]},
    }
    header = response.headers["set-cookie"]
    assert header.startswith(f"{COOKIE}=")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Max-Age=604800" in header, "a new device's cookie lives as long as the device"
    assert f"Path={base(runtime)}" in header
    assert device["id"] != response.cookies[COOKIE] and len(response.cookies[COOKIE]) == 43


def test_cookie_is_secure_only_behind_an_https_tunnel(
    client: TestClient, runtime: Runtime, fake: Fake, dist: Path
) -> None:
    """uvicorn turns ngrok's ``X-Forwarded-Proto`` into the scheme, from the trusted hop
    only; the header itself, which anyone can send, decides nothing here."""
    plain = unlock(client, runtime)
    assert "Secure" not in plain.headers["set-cookie"]
    forged = make_client(client.app).post(
        f"{base(runtime)}/api/unlock",
        json={"password": PASSWORD},
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7"},
    )
    assert forged.status_code == 200 and "Secure" not in forged.headers["set-cookie"]
    app = build_app(runtime, sources=fake.sources(), dist_dir=dist)
    tunnelled = unlock(make_client(app, base_url="https://testserver"), runtime)
    assert tunnelled.status_code == 200
    header = tunnelled.headers["set-cookie"]
    assert "Secure" in header and "HttpOnly" in header


def test_unlock_without_a_body_is_400(client: TestClient, runtime: Runtime) -> None:
    response = client.post(f"{base(runtime)}/api/unlock", content=b"garbage")
    assert response.status_code == 400


def test_api_without_cookie_is_401(client: TestClient, runtime: Runtime) -> None:
    for name in ("projects", "fleet", "board", "tasks", "memory", "devices", "remote", "panes/x"):
        response = client.get(f"{base(runtime)}/api/{name}")
        assert response.status_code == 401, name
        assert response.json() == {"error": "unauthorized"}


def test_a_forged_cookie_is_401(client: TestClient, runtime: Runtime) -> None:
    client.cookies.set(COOKIE, "made-up")
    response = client.get(f"{base(runtime)}/api/board")
    assert response.status_code == 401


def test_read_endpoints_return_the_sources_verbatim(
    client: TestClient, runtime: Runtime, fake: Fake
) -> None:
    unlock(client, runtime)
    b = base(runtime)
    assert client.get(f"{b}/api/board").json() == fake.board
    assert client.get(f"{b}/api/fleet").json() == fake.fleet
    assert client.get(f"{b}/api/projects").json() == [{"id": "p1", "name": "demo"}]
    assert client.get(f"{b}/api/tasks").json() == [{"id": "t1", "title": "ship it"}]
    assert client.get(f"{b}/api/memory").json() == [{"id": "m1", "text": "remember"}]
    pane = client.get(f"{b}/api/panes/coder-1").json()
    assert pane["rows"] == ["\x1b[32mcoder-1\x1b[0m $ "]
    assert pane["width"] == 80 and pane["height"] == 1 and pane["cursor"] == [3, 0]
    assert client.get(f"{b}/api/panes/ghost").status_code == 404
    assert client.get(f"{b}/api/nothing-here").status_code == 404


def test_remote_endpoint_carries_the_switches_only(client: TestClient, runtime: Runtime) -> None:
    unlock(client, runtime)
    payload = client.get(f"{base(runtime)}/api/remote").json()
    assert set(payload) == {"allow_write", "auto_off_at", "version"}
    assert payload["allow_write"] is False and payload["auto_off_at"] is None
    assert isinstance(payload["version"], str)


# --- rate limit -------------------------------------------------------------------------


def test_sixth_unlock_attempt_within_a_minute_is_429(
    runtime: Runtime, fake: Fake, dist: Path
) -> None:
    now = [1000.0]
    app = build_app(runtime, sources=fake.sources(), dist_dir=dist, clock=lambda: now[0])
    client = make_client(app)
    for _ in range(5):
        assert unlock(client, runtime, "wrong").status_code == 401
    assert unlock(client, runtime, PASSWORD).status_code == 429
    now[0] += 61
    assert unlock(client, runtime, PASSWORD).status_code == 200


def test_rate_limit_is_per_client(runtime: Runtime, fake: Fake, dist: Path) -> None:
    """Per client as uvicorn resolved it (the scope's peer), never per header."""
    app = build_app(runtime, sources=fake.sources(), dist_dir=dist)
    client = make_client(app, client=("203.0.113.5", 4000))
    for _ in range(5):
        unlock(client, runtime, "wrong")
    assert unlock(client, runtime, PASSWORD).status_code == 429
    other = make_client(app, client=("203.0.113.9", 4000))
    assert unlock(other, runtime, PASSWORD).status_code == 200


# --- write endpoints (§4-E) -------------------------------------------------------------


def test_write_endpoints_exist_and_are_403_by_default(
    client: TestClient, runtime: Runtime, fake: Fake
) -> None:
    unlock(client, runtime)
    for name in WRITE_ENDPOINTS:
        response = client.post(f"{base(runtime)}/api/{name}", json={"ref": "x"})
        assert response.status_code == 403, name
        assert response.json() == {"error": "read_only", "message": READ_ONLY_REASON}
    assert fake.written == []
    assert _audited() == ["unlock"], "only the unlock is on the trail"


def test_write_endpoints_need_the_cookie_before_the_gate(
    client: TestClient, runtime: Runtime
) -> None:
    assert client.post(f"{base(runtime)}/api/note", json={"text": "hi"}).status_code == 401


def test_unknown_write_endpoint_is_404(client: TestClient, runtime: Runtime) -> None:
    unlock(client, runtime)
    runtime.set_allow_write(True)
    assert client.post(f"{base(runtime)}/api/task/nuke", json={}).status_code == 404


def test_allowed_write_runs_the_handler_and_audits(
    client: TestClient, runtime: Runtime, fake: Fake
) -> None:
    device_id = unlock(client, runtime).json()["device"]["id"]
    runtime.set_allow_write(True)
    response = client.post(f"{base(runtime)}/api/task/claim", json={"ref": "tsk_1"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "endpoint": "task/claim"}
    assert fake.written == [("task/claim", {"ref": "tsk_1"})]
    lines = remote_audit_path().read_text().splitlines()
    assert _audited() == ["unlock", "task/claim"]
    ts, who, endpoint, summary = lines[-1].split(" ", 3)
    assert who == device_id and endpoint == "task/claim" and summary == "summary of task/claim"
    assert ts.endswith("+00:00")
    if sys.platform != "win32":  # POSIX file modes; the NTFS half is the spy test above
        assert stat.S_IMODE(remote_audit_path().stat().st_mode) == 0o600


def test_a_refused_write_keeps_its_status_and_is_not_audited(
    client: TestClient, runtime: Runtime
) -> None:
    unlock(client, runtime)
    runtime.set_allow_write(True)
    response = client.post(f"{base(runtime)}/api/note", json={"boom": True})
    assert response.status_code == 422
    assert response.json() == {"error": "refused", "message": "the fake said no"}
    assert _audited() == ["unlock"]


def _audited() -> list[str]:
    """The endpoint of every audit line, in order."""
    return [line.split(" ")[2] for line in remote_audit_path().read_text().splitlines()]


def test_write_body_must_be_an_object(client: TestClient, runtime: Runtime) -> None:
    unlock(client, runtime)
    runtime.set_allow_write(True)
    assert client.post(f"{base(runtime)}/api/note", json=[1, 2]).status_code == 400


# --- devices + revoke (§4-F) ----------------------------------------------------------


def test_devices_lists_devices_and_delete_revokes(client: TestClient, runtime: Runtime) -> None:
    first = unlock(client, runtime).json()["device"]["id"]
    other = make_client(client.app)
    second = unlock(other, runtime).json()["device"]["id"]
    rows = client.get(f"{base(runtime)}/api/devices").json()
    assert [row["id"] for row in rows] == [first, second]  # the name routes take (SPEC §2.3)
    assert set(rows[0]) == {
        "id",
        "ua",
        "first_seen",
        "last_seen",
        "expires_at",
        "signed_in",
        "current",
    }
    assert [row["current"] for row in rows] == [True, False]
    assert [row["signed_in"] for row in rows] == [True, True]
    assert [row["current"] for row in other.get(f"{base(runtime)}/api/devices").json()] == [
        False,
        True,
    ]
    assert set(runtime.device_rows()[0]) == set(rows[0]) - {"current"}  # §4-F status()
    # Revoking ANOTHER device changes who can reach the fleet: a write (SPEC §2.3).
    refused = client.delete(f"{base(runtime)}/api/devices/{second}")
    assert refused.status_code == 403 and refused.json()["error"] == "read_only"
    assert other.get(f"{base(runtime)}/api/board").status_code == 200
    runtime.set_allow_write(True)
    gone = client.delete(f"{base(runtime)}/api/devices/{second}")
    assert gone.status_code == 200
    assert gone.json() == {"ok": True, "id": second, "signed_out": False}
    assert other.get(f"{base(runtime)}/api/board").status_code == 401
    assert client.get(f"{base(runtime)}/api/board").status_code == 200
    assert client.delete(f"{base(runtime)}/api/devices/{second}").status_code == 404


def test_regenerate_password_drops_every_device(client: TestClient, runtime: Runtime) -> None:
    unlock(client, runtime)
    new = runtime.regenerate_password()
    assert new != PASSWORD and len(new.split("-")) == remote_server.PASSPHRASE_WORDS
    assert runtime.device_rows() == []
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    assert unlock(client, runtime, new).status_code == 200


# --- static page (SPA fallback) ------------------------------------------------------


def test_static_page_needs_no_cookie_and_falls_back_to_index(
    client: TestClient, runtime: Runtime
) -> None:
    b = base(runtime)
    assert client.get(f"{b}/").text.startswith("<!doctype html>")
    assert client.get(f"{b}/assets/app.js").text == "console.log('hi')"
    assert client.get(f"{b}/fleet/coder-1").text.startswith("<!doctype html>")
    assert client.get(f"{b}/unlock").status_code == 200
    escaped = client.get(f"{b}/%2e%2e/%2e%2e/etc/passwd")  # httpx would fold a literal ..
    assert escaped.status_code == 200 and escaped.text.startswith("<!doctype html>")


def test_missing_dist_is_a_404_that_says_where_to_put_it(
    runtime: Runtime, fake: Fake, tmp_path: Path
) -> None:
    app = build_app(runtime, sources=fake.sources(), dist_dir=tmp_path / "nope")
    response = make_client(app).get(f"{base(runtime)}/")
    assert response.status_code == 404
    assert response.json()["error"] == "no_dist"
    assert "nope" in response.json()["message"]


# --- the websocket stream (§4-D) --------------------------------------------------------


def test_websocket_without_cookie_is_denied_401(client: TestClient, runtime: Runtime) -> None:
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(f"{base(runtime)}/ws"),
    ):
        pass
    assert denied.value.status_code == 401


def _frames_until(ws: Any, kind: str, *, limit: int = 12) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = json.loads(ws.receive_text())
        seen.append(frame)
        if frame["type"] == kind:
            return seen
    raise AssertionError(f"no {kind} frame in {seen}")


def test_stream_sends_board_fleet_remote_then_only_changes(
    client: TestClient, runtime: Runtime, fake: Fake
) -> None:
    unlock(client, runtime)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        first = _frames_until(ws, "remote")
        kinds = [frame["type"] for frame in first]
        assert kinds == ["board", "fleet", "remote"]
        for frame in first:
            assert set(frame) == {"type", "payload", "ts"}
        assert first[0]["payload"] == fake.board
        assert first[2]["payload"]["allow_write"] is False
        # Nothing changed: no frame arrives for several ticks.
        fake.board = {**fake.board, "events": [{"seq": 1, "text": "hello from the desk"}]}
        changed = json.loads(ws.receive_text())
        assert changed["type"] == "board"
        assert changed["payload"]["events"][0]["text"] == "hello from the desk"
        runtime.set_allow_write(True)
        flipped = _frames_until(ws, "remote")[-1]
        assert flipped["payload"]["allow_write"] is True


def test_pane_frames_only_for_subscribed_agents(
    client: TestClient, runtime: Runtime, fake: Fake
) -> None:
    unlock(client, runtime)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        _frames_until(ws, "remote")
        assert fake.pane_calls == []
        ws.send_text(json.dumps({"subscribe": "coder-1"}))
        pane = _frames_until(ws, "pane")[-1]
        assert pane["agent"] == "coder-1"
        assert set(pane) == {"type", "agent", "payload", "ts"}
        assert pane["payload"]["rows"] == ["\x1b[32mcoder-1\x1b[0m $ "]
        assert set(fake.pane_calls) == {("coder-1", None, 0)}
        ws.send_text(json.dumps({"subscribe": "ghost"}))
        ghost = _frames_until(ws, "pane")[-1]
        assert ghost["agent"] == "ghost" and ghost["payload"]["rows"] == []
        assert "ghost" in ghost["payload"]["error"]
        ws.send_text("not json at all")
        ws.send_text(json.dumps({"unsubscribe": "coder-1"}))


def test_revoke_closes_the_socket_with_4401(client: TestClient, runtime: Runtime) -> None:
    device_id = unlock(client, runtime).json()["device"]["id"]
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        _frames_until(ws, "remote")
        assert remote_server.Runtime.revoke_device(runtime, device_id) is True
        closed = None
        for _ in range(20):
            message = ws.receive()
            if message["type"] == "websocket.close":
                closed = message
                break
        assert closed is not None and closed["code"] == remote_server.WS_CLOSE_UNAUTHORIZED


# --- parity with `asq --json` (§4-B) ----------------------------------------------------


def _json_of(runner: CliRunner, *args: str) -> Any:
    result = runner.invoke(cli, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_live_sources_match_the_json_commands(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    assert runner.invoke(cli, ["team", "on"]).exit_code == 0
    assert runner.invoke(cli, ["task", "add", "wire the phone"]).exit_code == 0
    assert runner.invoke(cli, ["note", "hello from the desk"]).exit_code == 0
    assert runner.invoke(cli, ["context", "add", "--project", "tabs not spaces"]).exit_code == 0

    live = remote_server.live_sources()
    live_projects = live.projects()
    cli_projects = _json_of(runner, "project", "list")
    assert isinstance(live_projects, list)
    # §4-B carve-out for THIS task: the remote payload adds a per-project
    # "agents" summary the plain `--json` command does not print; everything
    # else stays byte-identical.
    assert [{k: v for k, v in row.items() if k != "agents"} for row in live_projects] == (
        cli_projects
    )
    for row in live_projects:
        assert set(row["agents"]) >= {"working", "waiting", "attention", "exited", "lost"}
    assert live.tasks(None) == _json_of(runner, "task", "list")
    assert live.memory(None) == _json_of(runner, "context", "list")
    board = live.board(None)
    expected = _json_of(runner, "board")
    assert board == expected
    assert isinstance(board, dict) and set(board) == {"project", "sessions", "tasks", "events"}
    fleet = runner.invoke(cli, ["--json", "fleet", "ls"])
    if fleet.exit_code == 0:
        assert live.fleet(None) == json.loads(fleet.stdout.strip().splitlines()[-1])


# --- module API (§4-F) over a real socket -----------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_start_status_revoke_stop_over_a_real_port(
    isolated_home: Path, fake: Fake, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    monkeypatch.setattr(remote_server, "live_sources", fake.sources)
    port = _free_port()
    info = remote_server.start_remote_server(dist, port=port)
    server = remote_server._server
    assert server is not None
    served = server._server.config.app
    assert isinstance(served, remote_server._TokenGate) and served.kit.port == port, (
        "the app knows the port it serves"
    )
    try:
        assert info.url_local == f"http://127.0.0.1:{port}/r/{info.token}/"
        assert remote_server.remote_server_status()["running"] is True
        assert remote_server.start_remote_server(dist, port=port) == info  # idempotent
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers={"origin": f"http://127.0.0.1:{port}"}
        ) as http:
            assert http.get("/r/wrong/api/board").status_code == 404
            assert http.get(f"/r/{info.token}/api/board").status_code == 401
            ok = http.post(f"/r/{info.token}/api/unlock", json={"password": info.password})
            assert ok.status_code == 200
            device_id = ok.json()["device"]["id"]
            assert http.get(f"/r/{info.token}/api/board").json() == fake.board
            assert http.get(f"/r/{info.token}/").text.startswith("<!doctype html>")
            devices = remote_server.remote_server_status()["devices"]
            assert isinstance(devices, list) and devices[0]["id"] == device_id
            assert http.post(f"/r/{info.token}/api/note", json={"text": "x"}).status_code == 403
            remote_server.set_allow_write(True)
            assert http.get(f"/r/{info.token}/api/remote").json()["allow_write"] is True
            remote_server.set_allow_write(False)
            assert remote_server.revoke_remote_device(device_id) is True
            assert http.get(f"/r/{info.token}/api/board").status_code == 401
            assert remote_server.revoke_remote_device(device_id) is False
    finally:
        remote_server.stop_remote_server()
    assert remote_server.remote_server_status()["running"] is False
    assert not server._thread.is_alive(), (
        "stop_remote_server() returned with uvicorn's thread still up"
    )
    # Nothing listens any more. Linux refuses at once (ConnectError); Windows retries a
    # refused loopback SYN for about two seconds before WSAECONNREFUSED, so a one-second
    # timeout reports the same fact as ConnectTimeout. A listener that was still open
    # would complete the handshake from its backlog: a response, or a ReadTimeout.
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        httpx.get(f"http://127.0.0.1:{port}/r/{info.token}/", timeout=1.0)


def test_start_reports_a_busy_port(
    isolated_home: Path, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = int(taken.getsockname()[1])
        with pytest.raises(remote_server.RemoteError, match="did not come up"):
            remote_server.start_remote_server(dist, port=port)
    remote_server.stop_remote_server()


def test_start_without_the_extra_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_server, "_remote_dependency_error", lambda: "the remote extra is not installed"
    )
    with pytest.raises(remote_server.RemoteUnavailable, match="not installed"):
        remote_server.start_remote_server(Path("."), port=1)


# --- the CLI surface --------------------------------------------------------------------


def test_cli_status_allow_write_and_regenerate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    runner = CliRunner()
    status = _json_of(runner, "remote", "status")
    assert status["allow_write"] is False
    assert status["url_local"] == f"http://127.0.0.1:8750/r/{status['token']}/"
    assert status["devices"] == []
    assert (status["failed_unlocks"], status["locked_out_until"]) == (0, None)
    assert _json_of(runner, "remote", "allow-write", "on") == {"allow_write": True}
    assert _json_of(runner, "remote", "status")["allow_write"] is True
    assert _json_of(runner, "remote", "allow-write", "off") == {"allow_write": False}
    fresh = _json_of(runner, "remote", "regenerate-password")["password"]
    assert fresh != status["password"] and _json_of(runner, "remote", "status")["password"] == fresh
    bad = runner.invoke(cli, ["--json", "remote", "allow-write", "maybe"])
    assert bad.exit_code == 1 and json.loads(bad.stdout)["error"] == "invalid_switch"
    assert _json_of(runner, "remote", "status")["allow_write"] is False
    missing = runner.invoke(cli, ["--json", "remote", "revoke", "nobody"])
    assert missing.exit_code == 1 and json.loads(missing.stdout)["error"] == "not_found"


def test_cli_serve_without_the_extra_fails_with_the_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_server,
        "_remote_dependency_error",
        lambda: "the remote extra is not installed — pip install x",
    )
    result = CliRunner().invoke(cli, ["--json", "remote", "serve"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "remote_not_installed"


def test_cli_serve_prints_link_and_password_then_serves(
    isolated_home: Path, dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    # `serve` refuses a machine with no page installed (tests/test_remote_install_page.py),
    # and this test is about the banner — so install one first.
    remote_server.install_page(dist)
    served: list[tuple[Path | None, int, int, str | None]] = []

    def run_foreground(
        dist: Path | None,
        port: int,
        auto_off_minutes: int,
        public_url: str | None,
        *,
        ready: Callable[[], None],
    ) -> bool:
        served.append((dist, port, auto_off_minutes, public_url))
        ready()  # the banner, once the port is bound
        return False

    monkeypatch.setattr(remote_server, "run_foreground", run_foreground)
    result = CliRunner().invoke(cli, ["--json", "remote", "serve", "--port", "9001"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url_local"].startswith("http://127.0.0.1:9001/r/")
    assert payload["allow_write"] is False
    assert served == [(None, 9001, 60, None)], "an hour of auto-off unless told otherwise"
    human = CliRunner().invoke(cli, ["remote", "serve", "--port", "9002"])
    assert human.exit_code == 0
    assert "password:" in human.output and "read-only" in human.output
    assert "ngrok http 9002" in human.output


def test_the_write_list_is_the_plan_verbatim() -> None:
    assert WRITE_ENDPOINTS == (
        "task/claim",
        "task/done",
        "note",
        "project/switch",
        "project/add",
        "project/remove",
        "send-keys",
    )


def test_password_is_a_phone_typeable_passphrase_without_lookalikes() -> None:
    from aisquare.services.remote_words import REMOTE_PASSPHRASE_WORDS

    for _ in range(50):
        password = remote_server.new_password()
        words = password.split("-")
        assert len(words) == remote_server.PASSPHRASE_WORDS
        assert len(set(words)) == len(words)  # distinct words
        assert all(word in REMOTE_PASSPHRASE_WORDS for word in words)
        assert password == password.lower() and not set(password) & set("0123456789")
        time.sleep(0)


# --- caching and the SPA fallback (the stale-build faults) ---------------------------------
#
# Measured against the human's own tunnel, two faults that compound: index.html
# went out with NO Cache-Control, so a browser could heuristically cache it and
# a reload kept a STALE index naming a previous build's hashed chunk; and the
# SPA fallback then answered that dead chunk with 200 + the HTML document, so
# the browser refused to run HTML as JavaScript and the app failed to boot with
# no honest error. Hours of bug reports against a healthy server.


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A dist shaped like a real Vite build: an index and content-hashed chunks."""
    root = tmp_path / "built"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/index-NEWHASH1.js"></script>'
    )
    (root / "assets" / "index-NEWHASH1.js").write_text("console.log('current build')")
    (root / "assets" / "logo.svg").write_text("<svg/>")
    return root


@pytest.fixture
def site(runtime: Runtime, fake: Fake, built: Path) -> TestClient:
    client = make_client(build_app(runtime, sources=fake.sources(), dist_dir=built))
    return client


def test_index_is_always_revalidated(site: TestClient, runtime: Runtime) -> None:
    """No directive at all is what let a reload keep a stale document."""
    for path in ("/", "/fleet", "/unlock"):
        response = site.get(f"{base(runtime)}{path}")
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == remote_server.INDEX_CACHE_CONTROL, path
        assert "no-cache" in response.headers["cache-control"]


def test_a_content_hashed_chunk_is_cached_hard(site: TestClient, runtime: Runtime) -> None:
    """The hash IS the version, so the file may be kept for a year."""
    response = site.get(f"{base(runtime)}/assets/index-NEWHASH1.js")
    assert response.status_code == 200
    assert response.text == "console.log('current build')"
    assert response.headers["cache-control"] == remote_server.ASSET_CACHE_CONTROL
    assert "immutable" in response.headers["cache-control"]


def test_an_unhashed_asset_is_not_frozen_for_a_year(site: TestClient, runtime: Runtime) -> None:
    """Marking it immutable would recreate this very bug, one build later."""
    response = site.get(f"{base(runtime)}/assets/logo.svg")
    assert response.status_code == 200
    assert "immutable" not in response.headers["cache-control"]
    assert "no-cache" in response.headers["cache-control"]


def test_a_dead_chunk_is_a_404_and_never_html(site: TestClient, runtime: Runtime) -> None:
    """THE reported fault: a stale index asks for a chunk that no longer exists."""
    response = site.get(f"{base(runtime)}/assets/index-DfFvQnFu.js")
    assert response.status_code == 404
    assert "text/html" not in response.headers["content-type"]
    assert not response.text.lstrip().startswith("<")
    assert response.json()["error"] == "not_found"


def test_a_missing_file_with_an_extension_is_a_404_not_the_document(
    site: TestClient, runtime: Runtime
) -> None:
    for path in ("/assets/gone.css", "/favicon.ico", "/nope.js"):
        response = site.get(f"{base(runtime)}{path}")
        assert response.status_code == 404, path
        assert "text/html" not in response.headers["content-type"], path


def test_a_navigation_route_still_gets_the_app(site: TestClient, runtime: Runtime) -> None:
    """The fallback still exists — it is the reason deep links work at all."""
    for path in ("/", "/unlock", "/fleet/coder-1", "/projects/prj_x/agent"):
        response = site.get(f"{base(runtime)}{path}")
        assert response.status_code == 200, path
        assert response.text.startswith("<!doctype html>"), path


def test_a_browser_asking_for_a_document_still_gets_one(site: TestClient, runtime: Runtime) -> None:
    """Accept decides for a path that has an extension but is not an asset."""
    response = site.get(
        f"{base(runtime)}/some.route",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    assert response.status_code == 200
    assert response.text.startswith("<!doctype html>")


def test_a_dead_chunk_stays_a_404_even_when_the_client_accepts_html(
    site: TestClient, runtime: Runtime
) -> None:
    """assets/ is content-addressed: a miss is a miss, whatever Accept says."""
    response = site.get(
        f"{base(runtime)}/assets/index-OLDHASH.js", headers={"Accept": "text/html,*/*"}
    )
    assert response.status_code == 404


def test_traversal_is_still_defeated(site: TestClient, runtime: Runtime) -> None:
    """The fallback narrowed; it must not have opened a way out of the dist."""
    for path in ("/%2e%2e/%2e%2e/etc/passwd", "/../../etc/passwd", "/assets/%2e%2e/index.html"):
        response = site.get(f"{base(runtime)}{path}")
        assert response.status_code in (200, 404), path
        assert "root:" not in response.text, path
        assert "PATH" not in response.text, path
