"""A running server picks up ``remote.json`` written by ANOTHER process (mtime re-read)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.services import remote_server
from aisquare.services.remote_server import (
    READ_ONLY_REASON,
    WS_CLOSE_UNAUTHORIZED,
    Runtime,
    Sources,
    build_app,
)
from tests.remote_kit_helpers import make_client

PASSWORD = "Test1234"


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    rt = Runtime(remote_state_path(), remote_audit_path())
    rt._state.password = PASSWORD
    rt._save_state()
    return rt


@pytest.fixture
def client(runtime: Runtime, tmp_path: Path) -> TestClient:
    sources = Sources(
        projects=lambda: [],
        fleet=lambda project: {"agents": []},
        board=lambda project: {"tasks": []},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        explainability=lambda agent, project: {"available": False},
    )
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02))
    assert (
        client.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD}).status_code
        == 200
    )
    return client


def other_process_writes(mutate: Any) -> None:
    """What ``aisquare remote …`` in a second shell does: rewrite the file atomically."""
    path = remote_state_path()
    raw = json.loads(path.read_bytes())
    mutate(raw)
    tmp = path.with_name(path.name + ".other")
    # Bytes, as Runtime._write writes them: text mode on Windows would add a CR per
    # line, and the same-size premise below would fail before the server is asked.
    tmp.write_bytes(json.dumps(raw, indent=2).encode("utf-8"))
    os.replace(tmp, path)
    # No utime nudge on purpose: the fingerprint has to notice a rewrite that
    # lands in the same mtime tick with the same size (measured 195/200 here).


def base(runtime: Runtime) -> str:
    return f"/r/{runtime.token}"


def _frames_until(ws: Any, kind: str, *, limit: int = 12) -> dict[str, Any]:
    for _ in range(limit):
        frame: dict[str, Any] = json.loads(ws.receive_text())
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind} frame")


def test_allow_write_turned_on_externally_is_seen_by_the_next_get_and_pushed_on_ws(
    client: TestClient, runtime: Runtime
) -> None:
    assert client.get(f"{base(runtime)}/api/remote").json()["allow_write"] is False
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        assert _frames_until(ws, "remote")["payload"]["allow_write"] is False
        other_process_writes(lambda raw: raw.__setitem__("allow_write", True))
        pushed = _frames_until(ws, "remote", limit=30)
        assert pushed["payload"]["allow_write"] is True
    assert client.get(f"{base(runtime)}/api/remote").json()["allow_write"] is True
    assert runtime.allow_write is True


def test_allow_write_turned_off_externally_makes_the_next_write_403(
    client: TestClient, runtime: Runtime
) -> None:
    runtime.set_allow_write(True)
    ok = client.post(f"{base(runtime)}/api/note", json={"text": "x"})
    assert ok.status_code != 403
    other_process_writes(lambda raw: raw.__setitem__("allow_write", False))
    refused = client.post(f"{base(runtime)}/api/note", json={"text": "x"})
    assert refused.status_code == 403
    assert refused.json() == {"error": "read_only", "message": READ_ONLY_REASON}


def test_revoke_from_another_process_closes_the_socket_and_the_cookie(
    client: TestClient, runtime: Runtime
) -> None:
    (device,) = client.get(f"{base(runtime)}/api/devices").json()
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        _frames_until(ws, "remote")
        other_process_writes(
            lambda raw: raw.__setitem__(
                "devices", [row for row in raw["devices"] if row["id"] != device["id"]]
            )
        )
        closed = None
        for _ in range(40):
            message = ws.receive()
            if message["type"] == "websocket.close":
                closed = message
                break
        assert closed is not None and closed["code"] == WS_CLOSE_UNAUTHORIZED
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    assert runtime.device_rows() == []


def test_an_unchanged_file_is_never_re_read(client: TestClient, runtime: Runtime) -> None:
    reads = runtime.reads
    for _ in range(20):
        assert client.get(f"{base(runtime)}/api/remote").status_code == 200
        assert client.get(f"{base(runtime)}/api/board").status_code == 200
    assert runtime.reads == reads
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    other_process_writes(lambda raw: raw.__setitem__("auto_off_at", later))
    assert client.get(f"{base(runtime)}/api/remote").json()["auto_off_at"] == later
    assert runtime.reads == reads + 1
    client.get(f"{base(runtime)}/api/remote")
    assert runtime.reads == reads + 1


def test_own_writes_do_not_trigger_a_re_read(runtime: Runtime) -> None:
    reads = runtime.reads
    runtime.set_allow_write(True)
    runtime.set_allow_write(False)
    runtime.flush_last_seen()
    assert runtime.reload_if_changed() is False
    assert runtime.reads == reads


def test_flush_does_not_overwrite_a_fresher_file(runtime: Runtime) -> None:
    other_process_writes(lambda raw: raw.__setitem__("allow_write", True))
    runtime.flush_last_seen()
    assert json.loads(remote_state_path().read_text())["allow_write"] is True
    assert runtime.allow_write is True


def test_regenerated_password_from_another_process_applies(
    client: TestClient, runtime: Runtime
) -> None:
    other_process_writes(
        lambda raw: raw.update({"password": "amber-birch-cedar-delta", "devices": []})
    )
    assert client.get(f"{base(runtime)}/api/board").status_code == 401
    assert (
        client.post(f"{base(runtime)}/api/unlock", json={"password": PASSWORD}).status_code == 401
    )
    fresh = client.post(f"{base(runtime)}/api/unlock", json={"password": "amber-birch-cedar-delta"})
    assert fresh.status_code == 200


def test_a_same_size_rewrite_in_the_same_mtime_tick_is_seen(runtime: Runtime) -> None:
    """The measured failure: equal-length passphrase, same size, same mtime."""
    path = remote_state_path()
    before = path.stat()
    current = runtime.password
    swapped = current[::-1] if current[::-1] != current else current[1:] + current[0]
    assert len(swapped) == len(current)
    other_process_writes(lambda raw: raw.__setitem__("password", swapped))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # pin the SAME mtime
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert runtime.reload_if_changed() is True
    assert runtime.password == swapped


def test_same_size_regenerations_in_a_tight_loop_are_all_seen(isolated_home: Path) -> None:
    server = Runtime(remote_state_path(), remote_audit_path())
    shell = Runtime(remote_state_path(), remote_audit_path())
    seen = 0
    for _ in range(50):
        shell.set_allow_write(not shell.allow_write)
        seen += server.allow_write == shell.allow_write
    assert seen == 50


def test_a_half_written_file_keeps_the_state_in_hand(runtime: Runtime) -> None:
    path = remote_state_path()
    path.write_text("{ not json")
    assert runtime.reload_if_changed() is False
    assert runtime.password == PASSWORD
    assert runtime.allow_write is False


def test_the_cli_toggle_in_this_process_reaches_a_separate_runtime(isolated_home: Path) -> None:
    """The real shape: two Runtime instances over one file — the server's and the shell's."""
    server = Runtime(remote_state_path(), remote_audit_path())
    shell = Runtime(remote_state_path(), remote_audit_path())
    shell.set_allow_write(True)
    assert server.remote_json()["allow_write"] is True
    shell.regenerate_password()
    assert server.password == shell.password
    assert remote_server.WRITE_ENDPOINTS  # module still importable with the singleton untouched
