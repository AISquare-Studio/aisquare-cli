"""A running server picks up ``remote.json`` written by ANOTHER process (mtime re-read)."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
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
    Device,
    Runtime,
    Sources,
    build_app,
)
from tests.remote_kit_helpers import frame_within, make_client, receive_within

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
        frame = frame_within(ws)
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
            message = receive_within(ws)
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


def _while_the_next_check_reads(
    monkeypatch: pytest.MonkeyPatch, meanwhile: Callable[[Runtime], object]
) -> None:
    """Replay the race: the next check reads the file, and ``meanwhile`` runs before that
    check compares, as another thread of this process can: it takes the lock first."""
    read = Runtime._signature
    raced = False

    def overtaken(self: Runtime) -> tuple[bytes, bytes] | None:
        nonlocal raced
        signature = read(self)
        if not raced:
            raced = True
            meanwhile(self)
        return signature

    monkeypatch.setattr(Runtime, "_signature", overtaken)


def test_a_device_revoked_while_its_request_read_the_file_is_refused(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check read the file, the revoke here renamed its own write into place, and the
    check, comparing after it, adopted the file the revoke had replaced: the revoked device
    was let through (review of #243, round 2)."""
    unlocked = runtime.unlock_device(PASSWORD, "Phone")
    assert unlocked is not None
    secret, device = unlocked
    reads = runtime.reads
    _while_the_next_check_reads(monkeypatch, lambda rt: rt.revoke_device(device.id))
    assert runtime.device_for_cookie(secret) is None
    assert runtime.reads == reads, "the file the revoke replaced was never adopted"


def test_a_device_unlocked_while_a_check_read_the_file_stays_known(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlocked: list[tuple[str, Device] | None] = []
    _while_the_next_check_reads(
        monkeypatch, lambda rt: unlocked.append(rt.unlock_device(PASSWORD, "Phone"))
    )
    known = runtime.device_ids()
    assert unlocked[0] is not None
    assert known == [unlocked[0][1].id], "the new device was dropped until the next check"


def test_a_check_never_adopts_a_file_older_than_one_another_check_adopted(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    other_process_writes(lambda raw: raw.__setitem__("allow_write", True))

    def newer_adopted(rt: Runtime) -> None:
        other_process_writes(lambda raw: raw.__setitem__("auto_off_at", later))
        assert rt.reload_if_changed() is True

    _while_the_next_check_reads(monkeypatch, newer_adopted)
    assert runtime.reload_if_changed() is False, "it read the older file"
    assert (runtime._state.allow_write, runtime._state.auto_off_at) == (True, later)


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


# --- one check a request, one a tick ----------------------------------------------------


def _counting_checks(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How often the file is read and digested to check it (``Runtime._signature``)."""
    checks = [0]
    read = Runtime._signature

    def counted(self: Runtime) -> tuple[bytes, bytes] | None:
        checks[0] += 1
        return read(self)

    monkeypatch.setattr(Runtime, "_signature", counted)
    return checks


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "remote", None),
        ("GET", "fleet", None),
        ("GET", "devices", None),
        ("POST", "note", {"text": "x"}),
        ("POST", "nothing-here", {}),
    ],
)
def test_a_request_checks_the_file_once_for_its_gates_and_its_route(
    client: TestClient,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """The token, the deadline and the cookie's device at the gate, then the route's own
    read (``remote_json``, the write gate): ``GET api/remote`` read and digested the file
    four times on the event loop, ``api/fleet`` and ``POST api/note`` three (review of
    #243, round 3). A write is checked once more, off the loop, in the thread that runs
    it: what let it in may have changed while it waited for that thread (sweep 2 of #243)."""
    runtime.set_allow_write(True)
    where: list[str] = []
    read = Runtime._signature

    def counted(self: Runtime) -> tuple[bytes, bytes] | None:
        where.append(threading.current_thread().name)
        return read(self)

    monkeypatch.setattr(Runtime, "_signature", counted)
    client.request(method, f"{base(runtime)}/api/{path}", json=body)
    in_a_write = [name for name in where if name.startswith("asq-remote-write")]
    assert len(where) - len(in_a_write) == 1, where
    assert len(in_a_write) == (1 if path == "note" else 0), where


def test_a_socket_checks_the_file_once_a_tick(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote off, the device, and the ``remote`` frame were each a check, every tick of
    every socket (review of #243, round 3)."""
    ticks: list[str] = []
    live = Runtime.device_is_live

    def ticked(self: Runtime, device_id: str) -> bool:
        ticks.append(device_id)  # asked once a tick, after the tick's check
        return live(self, device_id)

    monkeypatch.setattr(Runtime, "device_is_live", ticked)
    checks = _counting_checks(monkeypatch)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        _frames_until(ws, "remote")
        before = (checks[0], len(ticks))
        deadline = time.monotonic() + 10
        while len(ticks) < before[1] + 10 and time.monotonic() < deadline:
            time.sleep(0.02)
        after = (checks[0], len(ticks))
    tick_count = after[1] - before[1]
    assert tick_count >= 10
    assert after[0] - before[0] <= tick_count + 1, "one check a tick, give or take the edge"


def test_a_write_inside_a_checked_request_still_starts_from_the_file(runtime: Runtime) -> None:
    """Inside a request the gate's check stands for the route's reads; a read-modify-write
    still reads the file under its lock, or it would write back what another process had
    just changed."""
    with runtime.remote_state_checked():
        other_process_writes(lambda raw: raw.__setitem__("allow_write", True))
        assert runtime.allow_write is False, "checked once already: the request's view"
        later = datetime.now(UTC) + timedelta(hours=1)
        runtime.set_auto_off(later)
    on_disk = json.loads(remote_state_path().read_bytes())
    assert on_disk["allow_write"] is True, "the other process's switch survived the write"
    assert on_disk["auto_off_at"] is not None
    assert runtime.allow_write is True, "and the next request sees it"
