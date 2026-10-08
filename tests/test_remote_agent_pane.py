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
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_server
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


def test_a_label_no_row_holds_makes_no_lock(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock registry is process-wide and never shrinks, and a label is whatever a body
    says: only a label that names a row gets a lock."""
    _serving(monkeypatch, Tmux(OLDER))
    with pytest.raises(NoSuchAgent):
        live_writes().handlers["send-keys"]({"agent": "ghost", "keys": ["Up"]})
    assert (project.id, "ghost") not in remote_server._agent_locks
