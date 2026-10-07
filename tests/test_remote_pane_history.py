"""Pane history on the wire (PLAN §4-L) — scrollback, not just the live screen.

The human, testing live: "once a previous session is active we need to also
cache all the conversation from that session so it can be shown in UI, right now
the old prompts are blank." Opening a pane served only the live screen, so
scrolling up in the terminal found nothing and everything the agent had already
said was invisible.

The live tests here drive a REAL tmux pane with real scrollback, because the
thing under test is what tmux does with ``capture-pane -S -<n>`` — a fake that
returns the rows we expect would only confirm our own assumptions about it.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from aisquare.core.paths import remote_audit_path, remote_state_path
from aisquare.core.tmux import CHECK_SOCKET_SUFFIX, TmuxError, TmuxServer
from aisquare.services.remote_server import (
    HISTORY_CAP,
    NoSuchAgent,
    NoSuchProject,
    Runtime,
    Sources,
    _history_param,
    _pane_payload,
    build_app,
)
from tests.remote_kit_helpers import frame_within, make_client

PASSWORD = "Test1234"
requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed; the live tests need it"
)
_SOCKETS = itertools.count()


# --- ?history= parsing -------------------------------------------------------------------


def test_absent_and_empty_both_mean_no_history() -> None:
    assert _history_param(None) == 0
    assert _history_param("") == 0
    assert _history_param("0") == 0


def test_a_line_count_is_read_as_itself() -> None:
    assert _history_param("50") == 50
    assert _history_param("50000") == 50000, "the CAP is applied later, not while parsing"


@pytest.mark.parametrize("raw", ["lots", "5.5", "1e3", "--1", " "])
def test_a_non_number_is_refused_rather_than_read_as_zero(raw: str) -> None:
    """Silently reading it as 0 would hand back a live-only frame to a client
    that believes it asked for scrollback — an empty conversation, no reason."""
    with pytest.raises(ValueError, match="whole number"):
        _history_param(raw)


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        _history_param("-5")


# --- a real tmux pane with real scrollback -------------------------------------------------


@pytest.fixture
def live() -> Iterator[TmuxServer]:
    """A tmux server on a private socket, killed whatever the test did."""
    server = TmuxServer(f"asq-hist-{os.getpid()}-{next(_SOCKETS)}")
    try:
        yield server
    finally:
        for socket in (server.socket, server.socket + CHECK_SOCKET_SUFFIX):
            with contextlib.suppress(TmuxError):
                TmuxServer(socket).kill_server()
            with contextlib.suppress(OSError):
                TmuxServer(socket).socket_path().unlink()


@pytest.fixture
def seeded(live: TmuxServer) -> tuple[TmuxServer, str, int]:
    """A pane that has printed 200 numbered lines — 200 - height of them scrolled off."""
    window = live.spawn_window(
        "asq-hist",
        name="w0",
        cwd=Path("/tmp"),
        command=["sh", "-c", "for i in $(seq 1 200); do echo line-$i; done; sleep 300"],
        width=80,
        height=24,
    )
    pane = window.pane_id
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if live.capture(pane).facts.history_size >= 150:
            break
        time.sleep(0.05)
    facts = live.capture(pane).facts
    assert facts.history_size >= 150, f"pane never filled: history_size={facts.history_size}"
    return live, pane, facts.height


@requires_tmux
def test_capture_is_still_one_screen_and_capture_history_is_more(
    seeded: tuple[TmuxServer, str, int],
) -> None:
    server, pane, height = seeded
    assert len(server.capture(pane).lines) == height
    assert len(server.capture_history(pane, history=50).lines) == 50 + height


@requires_tmux
def test_history_comes_back_oldest_first_and_joins_the_screen(
    seeded: tuple[TmuxServer, str, int],
) -> None:
    """One contiguous block: the scrollback, then the live screen, in order."""
    server, pane, _height = seeded
    live_screen = server.capture(pane).lines
    frame = server.capture_history(pane, history=50)

    assert frame.lines[50:] == live_screen, "the live screen is the tail, unchanged"
    assert frame.scrollback == 50
    numbered = [row for row in frame.lines if row.startswith("line-")]
    seen = [int(row.removeprefix("line-").strip()) for row in numbered]
    assert seen == sorted(seen), "oldest first, no seam out of order"
    assert len(set(seen)) == len(seen), "no row duplicated at the seam"
    assert max(seen) == 200, "the newest line is present"


@requires_tmux
def test_asking_deeper_than_the_pane_returns_what_exists(
    seeded: tuple[TmuxServer, str, int],
) -> None:
    server, pane, height = seeded
    available = server.capture(pane).facts.history_size
    frame = server.capture_history(pane, history=available + 500)
    assert frame.scrollback == available, "what tmux had, measured not predicted"
    assert len(frame.lines) == available + height


@requires_tmux
def test_history_of_zero_is_the_live_screen(seeded: tuple[TmuxServer, str, int]) -> None:
    server, pane, _height = seeded
    assert server.capture_history(pane, history=0).lines == server.capture(pane).lines


# --- the endpoint over a real pane ----------------------------------------------------------


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    rt = Runtime(remote_state_path(), remote_audit_path())
    rt._state.password = PASSWORD
    rt._save_state()
    return rt


def _sources(panes: Any) -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=panes,
        explainability=lambda agent, project: {"available": False},
    )


def _client(runtime: Runtime, panes: Any, tmp_path: Path) -> TestClient:
    client = make_client(build_app(runtime, sources=_sources(panes), dist_dir=tmp_path))
    assert (
        client.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD}).status_code
        == 200
    )
    return client


@pytest.fixture
def live_panes(seeded: tuple[TmuxServer, str, int], monkeypatch: pytest.MonkeyPatch) -> Any:
    """``_live_panes`` over the real seeded pane, with the store lookup faked.

    Only the agent row is faked — the capture is genuinely tmux's.
    """
    from aisquare.models import FleetAgent
    from aisquare.services import fleet as fleet_service
    from aisquare.services import remote_server

    server, pane, _height = seeded
    agent = FleetAgent(
        id="agt_h",
        project_id="prj_h",
        label="coder-1",
        role="coder",
        pane_id=pane,
        cwd=Path("/tmp"),
        created_at=datetime.now(UTC),
    )

    class _Store:
        def fleet_agent_by_label(
            self, project_id: str, label: str, *, live_only: bool = True
        ) -> FleetAgent | None:
            return agent if label == "coder-1" else None

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(remote_server, "_resolve_project", lambda ref: _project_stub(ref))
    monkeypatch.setattr("aisquare.core.store.store_session", lambda: _Store())
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: server)
    return remote_server._live_panes


def _project_stub(ref: str | None) -> Any:
    if ref not in (None, "prj_h"):
        raise NoSuchProject(f"no project matches {ref!r} (id prefix, name or codename)")

    class _P:
        id = "prj_h"
        root = Path("/tmp/prj_h")

    return _P()


LIVE_KEYS = {"rows", "cursor", "width", "height", "cursor_visible"}
"""What the live stream's frame holds: §4-D's four keys, and whether the cursor shows."""


@requires_tmux
def test_omitted_history_is_byte_identical_to_today(live_panes: Any) -> None:
    """§4-L: nothing existing changes — the live frame's keys, no history keys at all."""
    today = live_panes("coder-1", None, 0)
    assert set(today) == LIVE_KEYS
    assert "history" not in today and "history_size" not in today
    assert len(today["rows"]) == today["height"]


@requires_tmux
def test_a_frame_says_whether_the_program_shows_its_cursor(live: TmuxServer) -> None:
    """Claude Code hides the terminal's cursor (``ESC[?25l``), and the page drew it
    anyway: a stray inverted cell after a dialog's last line, where the hidden cursor
    rested. The frame carries tmux's own word on it now, which the page follows."""
    shown = live.spawn_window(
        "asq-cursor", name="shown", cwd=Path("/tmp"), command=["sleep", "300"], width=80, height=24
    )
    hidden = live.spawn_window(
        "asq-cursor",
        name="hidden",
        cwd=Path("/tmp"),
        command=["sh", "-c", r"printf '\033[?25l'; sleep 300"],
        width=80,
        height=24,
    )
    deadline = time.monotonic() + 10
    while live.capture(hidden.pane_id).facts.cursor_visible and time.monotonic() < deadline:
        time.sleep(0.05)

    assert _pane_payload(live.capture(shown.pane_id))["cursor_visible"] is True
    assert _pane_payload(live.capture(hidden.pane_id))["cursor_visible"] is False


@requires_tmux
def test_history_adds_the_scrollback_and_reports_both_counts(live_panes: Any) -> None:
    frame = live_panes("coder-1", None, 50)
    assert set(frame) == LIVE_KEYS | {"history_size", "history"}
    assert frame["history"] == 50
    assert frame["history_size"] >= 150, "what the pane actually holds"
    assert len(frame["rows"]) == 50 + frame["height"]
    assert frame["rows"][50:] == live_panes("coder-1", None, 0)["rows"]


@requires_tmux
def test_a_younger_pane_returns_what_it_has_and_says_how_much(live_panes: Any) -> None:
    deep = live_panes("coder-1", None, 100_000)
    assert deep["history"] == deep["history_size"], "all of it, and it says so"
    assert deep["history"] < 100_000


@requires_tmux
def test_the_cap_is_enforced_and_reported_not_silent(live_panes: Any) -> None:
    """A short answer must never be mistakable for a short pane."""
    asked = HISTORY_CAP + 1
    frame = live_panes("coder-1", None, asked)
    assert frame["history_capped"] == HISTORY_CAP
    assert frame["history"] <= HISTORY_CAP
    assert "history_capped" not in live_panes("coder-1", None, HISTORY_CAP)


@requires_tmux
def test_history_combines_with_an_unknown_project(live_panes: Any) -> None:
    with pytest.raises(NoSuchProject):
        live_panes("coder-1", "no-such-project", 50)


@requires_tmux
def test_an_unknown_agent_still_says_so_when_history_is_asked_for(live_panes: Any) -> None:
    with pytest.raises(NoSuchAgent):
        live_panes("ghost", None, 50)


# --- the query parameter end to end ----------------------------------------------------------


def _echo_panes(agent: str, project: str | None, history: int) -> dict[str, object]:
    return {"rows": [f"{agent}/{project}/{history}"], "width": 1, "height": 1}


def test_the_route_forwards_history_and_project_together(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _echo_panes, tmp_path)
    base = f"/r/{runtime.token}/api/panes/coder-1"
    assert client.get(base).json()["rows"] == ["coder-1/None/0"]
    assert client.get(base, params={"history": 50}).json()["rows"] == ["coder-1/None/50"]
    both = client.get(base, params={"project": "prj_x", "history": 200})
    assert both.json()["rows"] == ["coder-1/prj_x/200"]


def test_a_bad_history_value_is_a_400_not_a_silent_zero(runtime: Runtime, tmp_path: Path) -> None:
    client = _client(runtime, _echo_panes, tmp_path)
    base = f"/r/{runtime.token}/api/panes/coder-1"
    for raw in ("lots", "-5", "5.5"):
        response = client.get(base, params={"history": raw})
        assert response.status_code == 400, raw
        assert response.json()["error"] == "invalid"
    assert client.get(base, params={"history": ""}).json()["rows"] == ["coder-1/None/0"]


def test_history_does_not_loosen_the_gates(runtime: Runtime, tmp_path: Path) -> None:
    app = build_app(runtime, sources=_sources(_echo_panes), dist_dir=tmp_path)
    anonymous = make_client(app)
    with_history = {"history": 50}
    assert (
        anonymous.get(f"/r/{runtime.token}/api/panes/coder-1", params=with_history).status_code
        == 401
    ), "no cookie is still 401, even asking for history"
    assert (
        anonymous.get("/r/wrong-token/api/panes/coder-1", params=with_history).status_code == 404
    ), "a wrong token is still 404 and leaks nothing"


def test_the_ws_pane_frame_still_asks_for_no_history(runtime: Runtime, tmp_path: Path) -> None:
    """§4-L: history is a fetch, live stays a stream (the §4-D frame is untouched)."""
    import json

    asked: list[int] = []

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        asked.append(history)
        return {"rows": ["x"], "width": 1, "height": 1}

    app = build_app(runtime, sources=_sources(panes), dist_dir=tmp_path, tick=0.02)
    streaming = make_client(app)
    assert (
        streaming.post(f"/r/{runtime.token}/api/unlock", json={"password": PASSWORD}).status_code
        == 200
    )
    with streaming.websocket_connect(f"/r/{runtime.token}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "coder-1"}))
        for _ in range(20):
            frame = frame_within(ws)
            if frame["type"] == "pane":
                assert set(frame) == {"type", "agent", "payload", "ts"}
                break
        else:  # pragma: no cover - the stream is expected to deliver one
            raise AssertionError("no pane frame arrived")
    assert asked and set(asked) == {0}, "the stream never asks for scrollback"
