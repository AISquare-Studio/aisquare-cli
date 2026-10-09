"""send-keys and the live pane reach only the row's own pane, and type only into the agent,
never in the middle of another action on it.

A row that outlived its tmux server (a reboot, a hand-run ``tmux -L asq
kill-server``) stays live, and the next server numbers its panes from ``%0``
again: the row's pane id names ANOTHER agent's pane, perhaps another project's.
The listing reads such a row ``lost``, the TUI shows and types into no pane for
it, and the agent actions and quick answers refuse it (FLEET-1). The phone's live
view streamed that agent's screen under the row's label, and a key from the pad
answered its prompt (review of #243, round 2).

And keys take the agent's action lock, as every action and quick answer does: typed
while an Interrupt & tell waited for the prompt, they were submitted with the tell.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import json
import os
import shutil
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core import tmux as tmux_module
from aisquare.core.store import store_session
from aisquare.core.tmux import _SEP, CHECK_SOCKET_SUFFIX, Completed, TmuxError, TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_needs, remote_server
from aisquare.services.remote_server import (
    NoSuchAgent,
    RequestError,
    Sources,
    _live_panes,
    build_app,
    live_writes,
    remote_agent_lock,
)
from tests import fakebin
from tests.remote_kit_helpers import base, frame_within, make_client, make_runtime, unlock

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is not installed; the live test needs it"
)
_SOCKETS = itertools.count()

BORN = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
"""When the row was written."""
OLDER = BORN - timedelta(hours=1)
"""A server that started before the row: the row's own."""
YOUNGER = BORN + timedelta(minutes=30)
"""The next server after a restart: its ``%2`` is another agent's pane."""


@pytest.fixture
def project(isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """The current project, with one live row: ``coder-1`` in pane ``%2``, written at BORN."""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    assert CliRunner().invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    target = fleet_service.resolve_project(None)
    _seed(target, "coder-1", "%2", BORN)
    return target


def _seed(project: ProjectInfo, label: str, pane_id: str, born: datetime, **kw: Any) -> FleetAgent:
    row = FleetAgent(
        id=f"agt_{label}",
        project_id=project.id,
        label=label,
        role="coder",
        pane_id=pane_id,
        cwd=project.root,
        created_at=born,
        **kw,
    )
    with store_session() as store:
        store.upsert_fleet_agent(row)
    return row


class Tmux:
    """The server on the row's socket: when it started, what runs in the pane, what arrived.

    On a :data:`YOUNGER` server the pane under the row's id is another agent's,
    and it runs ``claude`` too: only when the server started tells the two apart.
    """

    def __init__(
        self,
        started: datetime | None,
        *,
        command: str = "claude",
        dead: bool = False,
        gone: bool = False,
    ) -> None:
        self.started = started
        self.command = command
        self.dead = dead
        self.gone = gone
        self.sent: list[tuple[str, ...]] = []

    def started_at(self) -> datetime | None:
        return self.started

    def pane_facts(self, pane_id: str) -> SimpleNamespace | None:
        if self.gone:
            return None
        return SimpleNamespace(
            pane_id=pane_id,
            dead=self.dead,
            dead_status=None,
            current_command=self.command,
            cursor_x=0,
            cursor_y=0,
            width=132,
            height=1,
            cursor_visible=False,
            history_size=0,
            server_started=self.started,  # as tmux says it with the rest of the facts
        )

    def capture(self, pane_id: str, **kwargs: Any) -> SimpleNamespace:
        facts = self.pane_facts(pane_id)
        return SimpleNamespace(lines=[f"the screen of {pane_id}"], facts=facts, scrollback=0)

    def capture_history(self, pane_id: str, *, history: int) -> SimpleNamespace:
        return self.capture(pane_id)

    def send_literal(self, pane_id: str, text: str) -> None:
        self.sent.append(("literal", pane_id, text))

    def send_keys(self, pane_id: str, *keys: str) -> None:
        self.sent.append(("keys", pane_id, *keys))


def _serving(monkeypatch: pytest.MonkeyPatch, tmux: Tmux) -> Tmux:
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    return tmux


# --- a row that outlived its tmux server --------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{"keys": ["1"]}, {"keys": ["Enter"]}, {"text": "yes", "enter": True}],
    ids=["a digit", "Enter", "typed text"],
)
def test_send_keys_types_nothing_into_the_pane_a_younger_tmux_gave_another_agent(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """The pane answers as ``claude``, because another agent runs in it: the ``1`` would
    have approved that agent's permission prompt."""
    tmux = _serving(monkeypatch, Tmux(YOUNGER))
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["send-keys"]({"agent": "coder-1", **body})
    assert (refused.value.status, refused.value.error) == (409, "not_agent")
    assert "tmux restarted after coder-1 started" in refused.value.message
    assert refused.value.message.endswith("nothing was sent")
    assert tmux.sent == []


@pytest.mark.parametrize("history", [0, 50], ids=["the live stream", "a history fetch"])
def test_the_live_pane_of_such_a_row_is_refused_never_another_agents_screen(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, history: int
) -> None:
    _serving(monkeypatch, Tmux(YOUNGER))
    with pytest.raises(RequestError) as refused:
        _live_panes("coder-1", None, history)
    assert (refused.value.status, refused.value.error) == (409, "not_agent")
    assert "its pane id is another agent's now" in refused.value.message


def test_a_transcript_wraps_at_80_columns_not_at_another_agents_width(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, "coder-1", live_only=True)
    assert row is not None
    _serving(monkeypatch, Tmux(YOUNGER))
    assert remote_server._pane_width(row) == 80
    _serving(monkeypatch, Tmux(OLDER))
    assert remote_server._pane_width(row) == 132, "its own pane's width"


@pytest.mark.parametrize("started", [OLDER, None], ids=["its own server", "tmux did not say"])
def test_the_rows_own_pane_still_streams_and_takes_keys(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, started: datetime | None
) -> None:
    """A start tmux will not give judges nothing, as ``fleet._pane_alive`` has it."""
    tmux = _serving(monkeypatch, Tmux(started))
    result, _summary = live_writes().handlers["send-keys"]({"agent": "coder-1", "keys": ["1"]})
    assert result["sent"] is True
    assert tmux.sent == [("keys", "%2", "1")]
    assert _live_panes("coder-1", None, 0)["rows"] == ["the screen of %2"]


def test_a_refused_ctrl_c_is_no_first_press_for_the_double_press_guard(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane is judged before the guard notes a press: a Ctrl-C that never left the
    machine must not turn the next one into a 409 ``double_press``."""
    send = live_writes().handlers["send-keys"]
    tmux = _serving(monkeypatch, Tmux(YOUNGER))
    with pytest.raises(RequestError) as refused:
        send({"agent": "coder-1", "keys": ["C-c"]})
    assert refused.value.error == "not_agent"
    tmux.started = OLDER
    send({"agent": "coder-1", "keys": ["C-c"]})
    assert tmux.sent == [("keys", "%2", "C-c")]


# --- a pane that is not running the agent -------------------------------------------------


@pytest.mark.parametrize(
    "pane",
    [{"command": "zsh"}, {"command": "python3.13"}, {"dead": True}, {"gone": True}],
    ids=["a shell an agent left", "the launcher", "a dead pane", "no pane"],
)
def test_send_keys_types_nothing_into_a_pane_not_running_the_agent(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, pane: dict[str, Any]
) -> None:
    """What the actions and the quick answers already refuse: ``yes`` and Enter into the
    shell an agent left behind is a command line, run."""
    tmux = _serving(monkeypatch, Tmux(OLDER, **pane))
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["send-keys"]({"agent": "coder-1", "text": "yes", "enter": True})
    assert (refused.value.status, refused.value.error) == (409, "not_agent")
    assert refused.value.message == "coder-1's pane is not running the agent — nothing was sent"
    assert tmux.sent == []


# --- over HTTP and on the stream -----------------------------------------------------------


def _sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=_live_panes,
        explainability=lambda agent, project: {"available": False},
    )


def _scripted_tmux(started: datetime, calls: list[list[str]], tmp_path: Path) -> TmuxServer:
    """A real :class:`TmuxServer` whose tmux is a script: every process it starts is recorded,
    and each answers as a server that started at ``started`` would."""

    def tmux(argv: Sequence[str], stdin: bytes | None) -> Completed:
        calls.append(list(argv))
        epoch = str(int(started.timestamp()))
        facts = dict.fromkeys(tmux_module._FACTS_FIELDS, "")
        facts.update(pane_id="%2", pane_width="132", pane_height="1", start_time=epoch)
        facts.update(pane_current_command="claude", window_activity=epoch)
        line = _SEP.join(facts.values())
        if "capture-pane" in argv:
            return Completed(0, f"the screen of %2\n{line}\n", "")
        if argv[-1] == tmux_module._FACTS_FORMAT:
            return Completed(0, f"{line}\n", "")
        return Completed(0, f"{epoch}\n", "")  # #{start_time}, asked on its own

    binary = fakebin.executable_fake(tmp_path / "bin", "tmux", posix="", windows="")
    return TmuxServer("asq", runner=tmux, binary=str(binary), conf=tmp_path / "fleet.conf")


@pytest.mark.parametrize(
    ("started", "width"), [(OLDER, 132), (YOUNGER, 80)], ids=["its own server", "a younger one"]
)
def test_a_live_frame_and_a_transcripts_width_each_cost_one_tmux_process(
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    started: datetime,
    width: int,
) -> None:
    """The stream takes a frame of every watched pane every tick, and each frame asked tmux in
    a second process when its server started: half the stream's tmux processes. Every
    transcript page did the same after a whole capture, for a width. Their own command says
    it now, so the server that answered is the one judged."""
    calls: list[list[str]] = []
    server = _scripted_tmux(started, calls, tmp_path)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: server)
    if started == OLDER:
        assert _live_panes("coder-1", None, 0)["rows"] == ["the screen of %2"]
    else:
        with pytest.raises(RequestError) as refused:
            _live_panes("coder-1", None, 0)
        assert refused.value.error == "not_agent"
    assert len(calls) == 1, [argv[5:7] for argv in calls]
    calls.clear()
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, "coder-1", live_only=True)
    assert row is not None
    assert remote_server._pane_width(row) == width
    assert len(calls) == 1, [argv[5:7] for argv in calls]


@pytest.mark.parametrize("started", [OLDER, YOUNGER], ids=["its own server", "a younger one"])
def test_a_key_and_a_poll_of_an_action_each_ask_tmux_once_about_the_pane(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, started: datetime
) -> None:
    """Whether a pane is the row's agent was two processes, its facts and then when its server
    started, though the facts say that too: every key tapped paid both before it was sent.
    Needs-you's snapshot, which an action polls every quarter second while its Escape lands,
    asked a third, for when the pane last printed (review of #243, round 4)."""
    calls: list[list[str]] = []
    server = _scripted_tmux(started, calls, tmp_path)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: server)
    send = live_writes().handlers["send-keys"]
    if started == OLDER:
        assert send({"agent": "coder-1", "keys": ["1"]})[0]["sent"] is True
    else:
        with pytest.raises(RequestError) as refused:
            send({"agent": "coder-1", "keys": ["1"]})
        assert refused.value.error == "not_agent"
    assert [argv[5] for argv in calls] == (
        ["display-message", "send-keys"] if started == OLDER else ["display-message"]
    ), [argv[5:7] for argv in calls]
    calls.clear()
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, "coder-1", live_only=True)
    assert row is not None
    snap = remote_needs._needs_snapshot(
        project, FleetAgentStatus(agent=row, state="working"), None, (), BORN
    )
    assert len(calls) == 1, [argv[5:7] for argv in calls]
    expected = (True, True) if started == OLDER else (False, None)
    assert (snap.pane_is_agent, snap.pane_quiet) == expected, "printed last when it started"


def test_over_http_and_on_the_stream_such_a_row_is_not_agent_and_an_error_frame(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tmux = _serving(monkeypatch, Tmux(YOUNGER))
    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(), writes=live_writes(), dist_dir=tmp_path, tick=0.05)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    runtime.set_allow_write(True)
    fetched = client.get(f"{base(runtime)}/api/panes/coder-1", params={"history": "50"})
    assert fetched.status_code == 409 and fetched.json()["error"] == "not_agent"
    typed = client.post(f"{base(runtime)}/api/send-keys", json={"agent": "coder-1", "keys": ["1"]})
    assert typed.status_code == 409 and typed.json()["error"] == "not_agent"
    assert tmux.sent == []
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "coder-1"}))
        frame = frame_within(ws)
        while frame["type"] != "pane":
            frame = frame_within(ws)
    assert frame["agent"] == "coder-1" and frame["payload"]["rows"] == []
    assert "its pane id is another agent's now" in frame["payload"]["error"]


# --- a real tmux server, started after the row ----------------------------------------------


@requires_tmux
def test_a_real_tmux_server_started_after_the_row_never_answers_for_it(
    project: ProjectInfo,
) -> None:
    """Measured, not assumed: a fresh server's first pane is ``%0`` whatever came before,
    and ``cat`` in it reads as a running agent, as another agent's ``claude`` would."""
    socket = f"asq-outlived-{os.getpid()}-{next(_SOCKETS)}"
    stale = _seed(project, "stale", "%0", datetime.now(UTC) - timedelta(days=1), tmux_socket=socket)
    server = TmuxServer(socket)
    try:
        window = server.spawn_window(
            "asq-outlived", name="w0", cwd=Path("/tmp"), command=["cat"], width=80, height=8
        )
        assert window.pane_id == stale.pane_id, "the next server numbers its panes from %0"
        _seed(project, "fresh", "%0", datetime.now(UTC), tmux_socket=socket)
        time.sleep(0.4)
        send = live_writes().handlers["send-keys"]
        with pytest.raises(RequestError) as refused:
            send({"agent": "stale", "text": "approve"})
        assert refused.value.error == "not_agent"
        with pytest.raises(RequestError):
            _live_panes("stale", None, 0)
        send({"agent": "fresh", "text": "mine"})
        time.sleep(0.3)
        rows = _live_panes("fresh", None, 0)["rows"]
        assert isinstance(rows, list)
        screen = "\n".join(rows)
        assert "mine" in screen and "approve" not in screen
    finally:
        for name in (socket, socket + CHECK_SOCKET_SUFFIX):
            with contextlib.suppress(TmuxError):
                TmuxServer(name).kill_server()
            with contextlib.suppress(OSError):
                TmuxServer(name).socket_path().unlink()


# --- one action at a time on one agent: keys too -------------------------------------------


def test_keys_are_refused_busy_while_an_action_on_the_agent_runs(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Interrupt & tell holds the agent's lock for seconds after its Escape, waiting for
    the prompt to come back. Keys typed meanwhile went into the pane, and the tell's paste
    and Enter then submitted them and the tell as one message; text between a stop's
    ``/exit`` and its Enter made ``/exitfoo``."""
    tmux = _serving(monkeypatch, Tmux(OLDER))
    monkeypatch.setattr(remote_server, "SEND_KEYS_LOCK_WAIT_SECONDS", 0.05)
    action = remote_agent_lock(project.id, "coder-1")
    assert action.acquire(blocking=False)
    try:
        with pytest.raises(RequestError) as refused:
            live_writes().handlers["send-keys"]({"agent": "coder-1", "text": "yes"})
    finally:
        action.release()
    assert (refused.value.status, refused.value.error) == (409, "busy")
    assert refused.value.message == "another action on coder-1 is still running — nothing was sent"
    assert tmux.sent == []


def test_keys_hold_the_agents_lock_while_they_are_typed(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So an action that arrives meanwhile is 409 ``busy``, as behind any other action."""
    held: list[bool] = []

    class Watched(Tmux):
        def send_literal(self, pane_id: str, text: str) -> None:
            held.append(remote_agent_lock(project.id, "coder-1").locked())
            super().send_literal(pane_id, text)

        def send_keys(self, pane_id: str, *keys: str) -> None:
            held.append(remote_agent_lock(project.id, "coder-1").locked())
            super().send_keys(pane_id, *keys)

    _serving(monkeypatch, Watched(OLDER))
    live_writes().handlers["send-keys"]({"agent": "coder-1", "text": "yes", "enter": True})
    assert held == [True, True]
    assert not remote_agent_lock(project.id, "coder-1").locked(), "and let go of after"


def test_keys_sent_together_take_turns_instead_of_refusing_each_other(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pad posts each tap without waiting for the last answer, and a reconnect sends
    every write it lost again at once: a key waits out the few milliseconds another key
    holds the lock, and is never refused for it."""
    tmux = _serving(monkeypatch, Tmux(OLDER))
    other_key = remote_agent_lock(project.id, "coder-1")
    assert other_key.acquire(blocking=False)
    threading.Timer(0.2, other_key.release).start()
    result, _summary = live_writes().handlers["send-keys"]({"agent": "coder-1", "keys": ["Up"]})
    assert result["sent"] is True and tmux.sent == [("keys", "%2", "Up")]


def test_keys_wait_for_a_busy_agent_only_what_is_left_of_their_wait_since_they_arrived(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait counted from when a thread was free to run the keys: tapped in a burst
    during an action, they waited their 2 s each in turn, and the last took the lock when
    the action let it go, seconds after their taps (sweep of #243)."""
    tmux = _serving(monkeypatch, Tmux(OLDER))
    send = live_writes().handlers["send-keys"]
    late = contextvars.copy_context()
    waited = remote_server.SEND_KEYS_LOCK_WAIT_SECONDS + 0.5
    late.run(remote_server._WRITE_ARRIVED.set, time.monotonic() - waited)
    action = remote_agent_lock(project.id, "coder-1")
    assert action.acquire(blocking=False)
    started = time.monotonic()
    try:
        with pytest.raises(RequestError) as refused:
            late.run(send, {"agent": "coder-1", "keys": ["Down"]})
    finally:
        action.release()
    assert time.monotonic() - started < 1.0, "its wait had run out before a thread ran it"
    assert (refused.value.status, refused.value.error) == (409, "busy") and tmux.sent == []
    with pytest.raises(RequestError) as at_a_free_lock:
        late.run(send, {"agent": "coder-1", "keys": ["Down"]})
    assert (at_a_free_lock.value.status, at_a_free_lock.value.error) == (409, "busy")
    assert at_a_free_lock.value.message == (
        "the machine was busy with other actions for 2 s — nothing was sent to coder-1"
    )
    assert tmux.sent == [], "nor at a free lock: it would land seconds after its tap"
    send({"agent": "coder-1", "keys": ["Down"]})
    assert tmux.sent == [("keys", "%2", "Down")], "a key on time takes a free lock at once"


def test_a_burst_of_keys_waiting_on_a_busy_agent_holds_up_no_read(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The keys waited on the loop's default pool, which runs every read and every socket's
    board and fleet snapshot: more taps than it has threads, during a restart, and every
    read and every socket stalled until their waits ran out (sweep of #243)."""
    tmux = _serving(monkeypatch, Tmux(OLDER))
    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(), writes=live_writes(), dist_dir=tmp_path)
    taps = 36  # more than any default pool: min(32, CPUs + 4) threads
    answers: list[tuple[int, float]] = []
    action = remote_agent_lock(project.id, "coder-1")
    assert action.acquire(blocking=False)
    try:
        with make_client(app) as client:
            assert unlock(client, runtime).status_code == 200
            runtime.set_allow_write(True)
            url = f"{base(runtime)}/api/send-keys"

            def tap() -> None:
                sent = time.monotonic()
                response = client.post(url, json={"agent": "coder-1", "keys": ["Down"]})
                answers.append((response.status_code, time.monotonic() - sent))

            tappers = [threading.Thread(target=tap) for _ in range(taps)]
            for tapper in tappers:
                tapper.start()
            time.sleep(0.3)  # every tap is in, and waiting
            asked = time.monotonic()
            read = client.get(f"{base(runtime)}/api/fleet")
            took = time.monotonic() - asked
            for tapper in tappers:
                tapper.join(timeout=30)
    finally:
        action.release()
    assert read.status_code == 200 and took < 1.0, f"the read waited {took:.2f} s"
    assert [status for status, _ in answers] == [409] * taps and tmux.sent == []
    slowest = max(elapsed for _, elapsed in answers)
    assert slowest < remote_server.SEND_KEYS_LOCK_WAIT_SECONDS + 1.0, slowest


def test_keys_queued_behind_actions_holding_every_write_thread_are_not_typed_as_one_ends(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restart holds a thread of the write pool for 20 to 40 s. A key tapped while actions
    held every thread ran as one ended, took its agent's lock, free again, and typed into
    the replacement that restart had started, seconds after its tap (review of #243, round 3)."""
    tmux = _serving(monkeypatch, Tmux(OLDER))
    monkeypatch.setattr(remote_server, "WRITE_WORKERS", 1)  # one restart holds them all
    monkeypatch.setattr(remote_server, "SEND_KEYS_LOCK_WAIT_SECONDS", 0.3)
    restarting, done = threading.Event(), threading.Event()

    def restart(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        with remote_agent_lock(project.id, "coder-1"):
            restarting.set()
            assert done.wait(10), "the test never let the restart end"
        return {"agent": "coder-1"}, "restart coder-1"

    writes = live_writes()
    writes.handlers["agent/restart"] = restart
    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(), writes=writes, dist_dir=tmp_path)
    queued: list[float] = []
    run_write = app.kit.kit_run_write

    async def watched(handler: Any, body: dict[str, Any], arrived: float, *rest: Any) -> Any:
        if handler is not restart:
            queued.append(arrived)
        return await run_write(handler, body, arrived, *rest)

    monkeypatch.setattr(app.kit, "kit_run_write", watched)
    answers: dict[str, int] = {}
    with make_client(app) as client:
        assert unlock(client, runtime).status_code == 200
        runtime.set_allow_write(True)

        def post(name: str, body: dict[str, Any]) -> None:
            answers[name] = client.post(f"{base(runtime)}/api/{name}", json=body).status_code

        key = {"agent": "coder-1", "keys": ["Down"]}
        restarter = threading.Thread(target=post, args=("agent/restart", {"agent": "coder-1"}))
        tapper = threading.Thread(target=post, args=("send-keys", key))
        restarter.start()
        try:
            assert restarting.wait(5)
            tapper.start()
            deadline = time.monotonic() + 5
            while not queued and time.monotonic() < deadline:
                time.sleep(0.01)
            assert queued, "the key never reached the server"
            past_its_wait = queued[0] + remote_server.SEND_KEYS_LOCK_WAIT_SECONDS + 0.1
            time.sleep(max(0.0, past_its_wait - time.monotonic()))
        finally:
            done.set()  # the restart ends: the thread and the agent's lock are free at once
            for thread in (restarter, tapper):
                if thread.is_alive():
                    thread.join(10)
    assert answers == {"agent/restart": 200, "send-keys": 409}
    assert tmux.sent == [], "the key would have landed after its tap's 0.3 s"


def test_a_label_no_row_holds_makes_no_lock(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock registry is process-wide and never shrinks, and a label is whatever a body
    says: only a label that names a row gets a lock."""
    _serving(monkeypatch, Tmux(OLDER))
    with pytest.raises(NoSuchAgent):
        live_writes().handlers["send-keys"]({"agent": "ghost", "keys": ["Up"]})
    assert (project.id, "ghost") not in remote_server._agent_locks
