"""asq's ui receiver: the captain's ``ui`` actions arrive over the local socket and run (T4).

Acceptance (card T4, the receiver line): headless tests — ui actions arrive over
the socket and run. Driven with the UI suite's harness: the real store in the
isolated home, ``list_agents`` scripted, every tmux call held to a private
socket. A client dials from a worker thread (``asyncio.to_thread``) while the
pilot's loop keeps running, because the receiver runs every action ON the app's
loop (``call_from_thread``) — a client dialled from the loop itself would wait
for an answer the loop could never give.

Sockets: every test that binds one binds it in a short folder of its own
(:func:`ui_path` patches ``captain_state.ui_socket_path``, T1's ``_asq_socket``
pattern), never in the machine's shared ``/tmp/aisquare-<uid>``; the one test
that leaves the helper unpatched runs under the suite's private short root
(``tests/conftest.py``, ``private_ui_socket_root``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from aisquare.cli.ui import receiver
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import Activatable
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.cli.ui.stop import StopAgentScreen
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.project import ProjectView
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import project_groups as groups_service
from aisquare.services.captain import actions
from aisquare.services.captain import state as captain_state
from tests import test_ui_shell as ui_suite
from tests.test_ui_shell import Script, drive, fleet_app, seed, status

# The UI suite's fixtures, bound here so pytest finds them for this module's tests
# (``no_real_tmux`` is autouse there: every tmux call held to a private socket).
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

UNIX_SOCKETS = pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(socket, "AF_UNIX"),
    reason="the ui socket is a unix socket",
)

SIX = {"open_spawn", "open_stop", "select_project", "select_agent", "copy_row", "focus_project"}


# --- fixtures and helpers -------------------------------------------------------------------


def _unix_socket() -> socket.socket:
    """A unix stream socket. The guard is one mypy reads: Windows typeshed has no AF_UNIX."""
    if sys.platform == "win32":
        raise NotImplementedError("unix sockets: the ui tests skip on Windows")
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


@pytest.fixture
def ui_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The ui socket's path, in a short private folder of this test's own.

    Patched where both sides ask (``ui_socket_path``): the receiver binds it and
    T1's client dials it, and the folder is 0700 and ours, so the client's
    privacy check passes as it would for the real one.
    """
    folder = Path(tempfile.mkdtemp(prefix="asq", dir="/tmp"))
    path = folder / "ui.sock"
    monkeypatch.setattr(captain_state, "ui_socket_path", lambda **_: path)
    try:
        yield path
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def fleet(tmp_path: Path, script: Script) -> ProjectInfo:
    """alpha (coder-1) and beta (coder-2, and a lost row), plus the captain on the home board.

    Returns the home board. The roots exist: the Spawn dialog asks one whether
    it is a git checkout.
    """
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"), ("prj_bbb", "beta", "blue-heron"))
    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
    home = captain_state.home_project()
    script[home.id] = [status(home.id, "captain", "captain", "waiting")]
    script["prj_aaa"] = [status("prj_aaa", "coder-1", "coder", "working")]
    script["prj_bbb"] = [
        status("prj_bbb", "coder-2", "coder", "waiting"),
        status("prj_bbb", "old", "coder", "lost", minute=1),
    ]
    return home


def request(action: str, arg: str | None = None) -> bytes:
    """One request line, the shape T1's client sends."""
    return (json.dumps({"v": 1, "action": action, "arg": arg}) + "\n").encode()


def ask(path: Path, line: bytes, *, timeout: float = 2.0) -> dict[str, Any]:
    """One exchange the way T1's client makes it: one line out, one line back, 2 s at most."""
    with _unix_socket() as conn:
        conn.settimeout(timeout)
        conn.connect(str(path))
        conn.sendall(line)
        with conn.makefile("rb") as stream:
            reply = stream.readline(64 * 1024)
    decoded = json.loads(reply)
    assert isinstance(decoded, dict), reply
    return decoded


async def send(
    pilot: Pilot[None], path: Path, action: str, arg: str | None = None
) -> dict[str, Any]:
    """Ask from a worker thread, then let the message the action posted land."""
    reply = await asyncio.to_thread(ask, path, request(action, arg))
    await pilot.pause()
    await pilot.pause()
    return reply


def receiver_of(app: FleetApp) -> receiver.UiReceiver:
    found = receiver.ui_receiver(app)
    assert found is not None, "asq started no ui receiver"
    return found


def cursor_keys(app: FleetApp) -> list[str]:
    return [row.selection_key for row in app.query(".cursor").results(Activatable)]


# --- the six actions: each has its effect, and says so --------------------------------------


@UNIX_SOCKETS
def test_open_spawn_opens_the_spawn_dialog_for_the_project_it_names(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], str]:
        app = fleet_app(pilot)
        reply = await send(pilot, ui_path, "open_spawn", "blue-heron")
        dialog = app.screen
        assert isinstance(dialog, SpawnDialog), f"open_spawn opened {type(dialog).__name__}"
        return reply, dialog.project.id

    reply, project_id = drive(body)
    assert reply == {"ok": True, "said": "spawn dialog open for beta"}
    assert project_id == "prj_bbb"  # the codename named beta, not the first card


@UNIX_SOCKETS
def test_open_spawn_with_no_project_takes_the_one_selected_in_the_sidebar(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> list[Any]:
        app = fleet_app(pilot)
        seen: list[Any] = []
        seen.append(await send(pilot, ui_path, "open_spawn"))  # nothing selected yet
        seen.append(await send(pilot, ui_path, "select_agent", "captain"))
        seen.append(await send(pilot, ui_path, "open_spawn"))  # the captain's board is no project
        seen.append(await send(pilot, ui_path, "select_agent", "beta/coder-2"))
        seen.append(await send(pilot, ui_path, "open_spawn"))  # an agent row: its project
        dialog = app.screen
        seen.append(dialog.project.id if isinstance(dialog, SpawnDialog) else None)
        return seen

    nothing, _captain, on_captain, _coder, reply, project_id = drive(body)
    assert nothing["ok"] is False and nothing["said"].startswith("which project?")
    assert on_captain["ok"] is False and on_captain["said"].startswith("which project?")
    assert "captain" in on_captain["said"]
    assert reply == {"ok": True, "said": "spawn dialog open for beta"}
    assert project_id == "prj_bbb"


@UNIX_SOCKETS
def test_open_stop_opens_the_stop_dialog_for_that_agent_and_stops_nothing(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet(tmp_path, script)
    # The suite's stop double: it carries the service's full signature, which the
    # dialog reads for its grace period (``stop.grace_seconds``).
    recorder = ui_suite.stopper(monkeypatch, status("prj_aaa", "coder-1", "coder", "working").agent)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], str, str]:
        app = fleet_app(pilot)
        reply = await send(pilot, ui_path, "open_stop", "alpha/coder-1")
        dialog = app.screen
        assert isinstance(dialog, StopAgentScreen), f"open_stop opened {type(dialog).__name__}"
        return reply, dialog.project.id, dialog.status.agent.id

    reply, project_id, agent_id = drive(body)
    assert reply == {"ok": True, "said": "stop dialog open for coder-1 (alpha)"}
    assert (project_id, agent_id) == ("prj_aaa", "agt_aaa_coder-1")
    assert recorder.calls == [], "the dialog asks the owner; the action itself stops nothing"


@UNIX_SOCKETS
def test_open_stop_reaches_the_captain_by_its_label_on_the_home_board(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    home = fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], str | None]:
        app = fleet_app(pilot)
        reply = await send(pilot, ui_path, "open_stop", "captain")
        dialog = app.screen
        return reply, dialog.project.id if isinstance(dialog, StopAgentScreen) else None

    reply, board = drive(body)
    assert reply == {"ok": True, "said": "stop dialog open for captain (home)"}
    assert board == home.id, "the captain's row stops against the home board"


@UNIX_SOCKETS
def test_open_stop_refuses_a_row_there_is_nothing_to_stop_on(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    """The sidebar's ``x`` and the view's button ask ``STOP_STATES``; so does the action."""
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], str]:
        app = fleet_app(pilot)
        reply = await send(pilot, ui_path, "open_stop", "beta/old")
        return reply, type(app.screen).__name__

    reply, screen = drive(body)
    assert reply["ok"] is False
    assert reply["said"].startswith("old is lost")
    assert screen != "StopAgentScreen"


@UNIX_SOCKETS
def test_a_dialog_is_not_stacked_on_a_dialog_that_is_open(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], dict[str, Any], int]:
        app = fleet_app(pilot)
        await send(pilot, ui_path, "open_spawn", "alpha")
        again = await send(pilot, ui_path, "open_spawn", "beta")
        stop = await send(pilot, ui_path, "open_stop", "alpha/coder-1")
        return again, stop, len(app.screen_stack)

    again, stop, depth = drive(body)
    assert again["ok"] is False and "SpawnDialog is open" in again["said"]
    assert stop["ok"] is False and "SpawnDialog is open" in stop["said"]
    assert depth == 2, "one dialog over the shell, not three"


@UNIX_SOCKETS
def test_select_project_shows_that_project(tmp_path: Path, script: Script, ui_path: Path) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[list[dict[str, Any]], str | None, str | None]:
        app = fleet_app(pilot)
        replies = [await send(pilot, ui_path, "select_project", "prj_a")]  # an id prefix
        replies.append(await send(pilot, ui_path, "select_project", "BETA"))  # a name, any case
        view = app.current_view()
        shown = view.project.id if isinstance(view, ProjectView) else None
        return replies, shown, app.sidebar.selected_key

    replies, shown, selected = drive(body)
    assert replies == [
        {"ok": True, "said": "selected alpha"},
        {"ok": True, "said": "selected beta"},
    ]
    assert shown == "prj_bbb"
    assert selected == "project:prj_bbb"


@UNIX_SOCKETS
def test_select_agent_shows_that_agent(tmp_path: Path, script: Script, ui_path: Path) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[list[dict[str, Any]], str | None, str | None]:
        app = fleet_app(pilot)
        replies = [await send(pilot, ui_path, "select_agent", "agt_bbb_coder-2")]  # an agent id
        replies.append(await send(pilot, ui_path, "select_agent", "coder-1"))  # a unique label
        view = app.current_view()
        shown = view.status.agent.id if isinstance(view, AgentView) else None
        return replies, shown, app.sidebar.selected_key

    replies, shown, selected = drive(body)
    assert replies == [
        {"ok": True, "said": "selected coder-2 (beta)"},
        {"ok": True, "said": "selected coder-1 (alpha)"},
    ]
    assert shown == "agt_aaa_coder-1"
    assert selected == "agent:agt_aaa_coder-1"


@UNIX_SOCKETS
def test_copy_row_copies_an_agents_row_and_a_projects_row(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], str, dict[str, Any], str, str]:
        app = fleet_app(pilot)
        agent = await send(pilot, ui_path, "copy_row", "alpha/coder-1")
        agent_text = app.clipboard
        project = await send(pilot, ui_path, "copy_row", "prj_bbb")  # the id
        beta = app.snapshot.project("prj_bbb") if app.snapshot else None
        assert beta is not None
        return agent, agent_text, project, app.clipboard, str(beta.root)

    agent, agent_text, project, project_text, root = drive(body)
    assert agent == {"ok": True, "said": "copied coder-1's row"}
    assert agent_text == "coder-1  coder  working  %1"
    assert project == {"ok": True, "said": "copied beta's row"}
    assert project_text == f"beta  blue-heron  {root}"


@UNIX_SOCKETS
def test_focus_project_selects_it_and_hands_the_keyboard_to_its_card(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    home = fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> list[Any]:
        app = fleet_app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        for _ in range(2):  # the arrows leave the cursor on a row of their own: the captain's
            await pilot.press("down")
        app.set_focus(None)
        await pilot.pause()
        before = (app.focused, cursor_keys(app))
        reply = await send(pilot, ui_path, "focus_project", "beta")
        view = app.current_view()
        after = [
            reply,
            app.focused is app.sidebar,
            cursor_keys(app),
            app.sidebar.selected_key,
            view.project.id if isinstance(view, ProjectView) else None,
        ]
        await pilot.press("down")  # the cursor itself moved, not just its highlight
        await pilot.pause()
        return [before, *after, cursor_keys(app)]

    before, reply, focused, cursor, selected, shown, next_row = drive(body)
    assert before == (None, [f"agent:{home.id.replace('prj_', 'agt_')}_captain"]), (
        "the premise: the keyboard was elsewhere, and the cursor on another row"
    )
    assert reply == {"ok": True, "said": "focused beta in the sidebar"}
    assert focused
    assert cursor == ["project:prj_bbb"]
    assert selected == "project:prj_bbb" and shown == "prj_bbb"
    assert next_row == ["agent:agt_bbb_coder-2"], "↓ continues from beta's card"


@UNIX_SOCKETS
def test_focus_project_on_a_card_in_a_folded_group_selects_it_and_says_the_cursor_stayed(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    """The arrows never land on a row that is not on screen; neither does the action."""
    fleet(tmp_path, script)
    with store_session() as store:
        group, _ = groups_service.create_group(store, "later", ["prj_bbb"])
        groups_service.set_collapsed(store, group.id, True)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], list[str], str | None]:
        app = fleet_app(pilot)
        reply = await send(pilot, ui_path, "focus_project", "beta")
        return reply, cursor_keys(app), app.sidebar.selected_key

    reply, cursor, selected = drive(body)
    assert reply == {
        "ok": True,
        "said": "selected beta; its card is in a folded group, so the sidebar cursor stayed "
        "where it was",
    }
    assert cursor == [], "no cursor on a row nobody can see"
    assert selected == "project:prj_bbb"


# --- T1's real client, end to end ----------------------------------------------------------


@UNIX_SOCKETS
def test_the_captains_ui_tool_reaches_asq_and_the_action_runs(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], str | None]:
        app = fleet_app(pilot)
        result = await asyncio.to_thread(actions.ui, "select_project", "alpha")
        await pilot.pause()
        await pilot.pause()
        view = app.current_view()
        return json.loads(result), view.project.id if isinstance(view, ProjectView) else None

    result, shown = drive(body)
    assert (result["delivered"], result["said"]) == (True, "selected alpha")
    assert shown == "prj_aaa"


@UNIX_SOCKETS
def test_the_captains_ui_tool_is_told_why_asq_said_no(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    pytest.importorskip("mcp", reason="the [serve] extra is not installed")
    from mcp.server.mcpserver.exceptions import ToolError

    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> str:
        with pytest.raises(ToolError) as caught:
            await asyncio.to_thread(actions.ui, "select_project", "zeta")
        return str(caught.value)

    message = drive(body)
    assert message.startswith("refused: asq said: no project matches 'zeta'")


@UNIX_SOCKETS
def test_under_a_long_home_the_receiver_binds_where_the_ui_tool_dials(
    tmp_path: Path, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper unpatched: a home too long for a unix socket, so the short-root folder.

    That folder is the suite's private one (``conftest.private_ui_socket_root``),
    never the machine's shared ``/tmp/aisquare-<uid>``.
    """
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / ("h" * 60) / ("o" * 60) / "home"))
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[Path | None, dict[str, Any]]:
        app = fleet_app(pilot)
        result = await asyncio.to_thread(actions.ui, "select_agent", "alpha/coder-1")
        return receiver_of(app).path, json.loads(result)

    path, result = drive(body)
    shared = Path("/tmp") / f"aisquare-{captain_state._user_tag()}"
    assert path is not None and not path.is_relative_to(shared)
    assert (result["delivered"], result["said"]) == (True, "selected coder-1 (alpha)")


# --- refusals are answers ------------------------------------------------------------------


@UNIX_SOCKETS
def test_an_unknown_action_is_answered_no_with_the_known_actions(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> dict[str, Any]:
        return await send(pilot, ui_path, "fly", "alpha")

    reply = drive(body)
    assert reply["ok"] is False
    assert reply["said"].startswith("unknown ui action 'fly' — known: ")
    assert all(name in reply["said"] for name in SIX)


@UNIX_SOCKETS
def test_an_unknown_or_ambiguous_ref_is_answered_no_naming_it(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    home = fleet(tmp_path, script)
    seed(tmp_path, ("prj_ccc", "one/api", None), ("prj_ddd", "two/api", None))
    script["prj_ccc"] = [status("prj_ccc", "coder-1", "coder", "waiting")]

    async def body(pilot: Pilot[None]) -> dict[str, dict[str, Any]]:
        return {
            "unknown project": await send(pilot, ui_path, "select_project", "zeta"),
            "ambiguous project": await send(pilot, ui_path, "select_project", "api"),
            "the home": await send(pilot, ui_path, "focus_project", home.id),
            "no project": await send(pilot, ui_path, "select_project"),
            "unknown agent": await send(pilot, ui_path, "select_agent", "alpha/nobody"),
            "ambiguous agent": await send(pilot, ui_path, "select_agent", "coder-1"),
            "unknown row": await send(pilot, ui_path, "copy_row", "ghost"),
            "ambiguous row": await send(pilot, ui_path, "copy_row", "api"),
            "long ref": await send(pilot, ui_path, "select_project", "z" * 5000),
        }

    said = {what: reply["said"] for what, reply in drive(body).items() if not reply["ok"]}
    assert said["unknown project"].startswith("no project matches 'zeta'")
    assert said["ambiguous project"].startswith("'api' matches several projects: ")
    assert "prj_ccc" in said["ambiguous project"] and "prj_ddd" in said["ambiguous project"]
    assert said["the home"].startswith(f"no project matches {home.id!r}")
    assert said["no project"] == "select_project needs a project"
    assert said["unknown agent"] == "no agent 'nobody' in alpha"
    assert said["ambiguous agent"].startswith("'coder-1' names several agents: ")
    assert "alpha/coder-1" in said["ambiguous agent"] and "api/coder-1" in said["ambiguous agent"]
    assert said["unknown row"].startswith("no agent or project matches 'ghost'")
    assert said["ambiguous row"].startswith("'api' names several rows: ")
    assert said["long ref"].endswith("...' (an id, an id prefix, a codename or a name)")
    assert len(said["long ref"]) < 200, "a refusal quotes what it was asked, cut short"
    assert len(said) == 9, "every one of them is a no"


@UNIX_SOCKETS
def test_a_malformed_request_is_answered_and_the_receiver_serves_the_next(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)
    bad = [
        b"not json\n",
        b"\xff\xfe\n",
        b"[1, 2]\n",
        b'{"v": 2, "action": "select_project", "arg": "alpha"}\n',
        b'{"v": true, "action": "select_project", "arg": "alpha"}\n',
        b'{"v": 1, "arg": "alpha"}\n',
        b'{"v": 1, "action": "select_project", "arg": 5}\n',
        b"\n",
        b"x" * (receiver.LINE_MAX + 10) + b"\n",
    ]

    async def body(pilot: Pilot[None]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        answers = [await asyncio.to_thread(ask, ui_path, line) for line in bad]
        good = await send(pilot, ui_path, "select_project", "alpha")
        return answers, good

    answers, good = drive(body)
    assert [answer["ok"] for answer in answers] == [False] * len(bad)
    assert [answer["said"].removeprefix("not a request: ") for answer in answers] == [
        "the line is not JSON",
        "the line is not JSON",
        "the line is not a JSON object",
        "v must be 1, not '2'",
        "v must be 1, not 'true'",  # true == 1 in Python; it is no version
        "action must be a string",
        "arg must be a string or null",
        "the line is empty",
        "the line is longer than 64 KiB",
    ]
    assert good == {"ok": True, "said": "selected alpha"}


@UNIX_SOCKETS
def test_a_handler_that_raises_is_an_answer_and_the_receiver_lives_on(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet(tmp_path, script)

    def boom(app: FleetApp, arg: str | None) -> str:
        raise RuntimeError("the frame fell over")

    monkeypatch.setitem(receiver.ACTIONS, "select_project", boom)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], dict[str, Any], bool]:
        app = fleet_app(pilot)
        failed = await send(pilot, ui_path, "select_project", "alpha")
        after = await send(pilot, ui_path, "select_agent", "alpha/coder-1")
        return failed, after, app.is_running

    failed, after, running = drive(body)
    assert failed == {"ok": False, "said": "error: RuntimeError: the frame fell over"}
    assert after == {"ok": True, "said": "selected coder-1 (alpha)"}
    assert running


# --- the socket's life: bound private, never stolen, unlinked only when ours ----------------


@UNIX_SOCKETS
def test_the_socket_is_private_and_quitting_unlinks_it_and_ends_the_thread(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[receiver.UiReceiver, int, bool]:
        rx = receiver_of(fleet_app(pilot))
        facts = os.lstat(ui_path)
        return rx, stat.S_IMODE(facts.st_mode), stat.S_ISSOCK(facts.st_mode)

    rx, mode, is_socket = drive(body)
    assert is_socket and mode == 0o600, f"the socket is {oct(mode)}"
    assert not ui_path.exists(), "quitting unlinks our socket"
    assert not rx.alive, "quitting ends the receiver's thread"


@UNIX_SOCKETS
def test_a_socket_a_crashed_asq_left_is_replaced(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)
    stale = _unix_socket()
    stale.bind(str(ui_path))
    stale.close()  # the file stays; nothing listens on it

    async def body(pilot: Pilot[None]) -> tuple[str | None, dict[str, Any]]:
        rx = receiver_of(fleet_app(pilot))
        return rx.reason, await send(pilot, ui_path, "select_project", "alpha")

    reason, reply = drive(body)
    assert reason is None
    assert reply == {"ok": True, "said": "selected alpha"}


@UNIX_SOCKETS
def test_a_live_listener_is_left_alone_and_survives_the_quit(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)
    other = _unix_socket()
    other.bind(str(ui_path))
    other.listen(4)
    inode = os.lstat(ui_path).st_ino
    try:

        async def body(pilot: Pilot[None]) -> tuple[bool, str | None]:
            rx = receiver_of(fleet_app(pilot))
            return rx.listening, rx.reason

        listening, reason = drive(body)
        survived = os.lstat(ui_path).st_ino
        with _unix_socket() as dial:
            dial.settimeout(2)
            dial.connect(str(ui_path))  # the other listener still takes a connection
    finally:
        other.close()
        ui_path.unlink(missing_ok=True)
    assert not listening
    assert reason is not None and reason.startswith("another asq already listens at")
    assert survived == inode, "the other listener's socket is not unlinked at quit"


@UNIX_SOCKETS
def test_a_second_asqs_socket_survives_the_firsts_quit(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)
    second = _unix_socket()
    try:

        async def body(pilot: Pilot[None]) -> int:
            receiver_of(fleet_app(pilot))
            ui_path.unlink()  # ours is gone from the folder, and a second asq bound the path
            second.bind(str(ui_path))
            second.listen(4)
            return os.lstat(ui_path).st_ino

        inode = drive(body)
        survived = os.lstat(ui_path).st_ino if ui_path.exists() else None
    finally:
        second.close()
        ui_path.unlink(missing_ok=True)
    assert survived == inode, "the first asq's quit unlinked the second asq's socket"


@UNIX_SOCKETS
def test_something_that_is_not_a_socket_at_the_path_is_left_alone(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)
    ui_path.write_text("mine", encoding="utf-8")

    async def body(pilot: Pilot[None]) -> str | None:
        return receiver_of(fleet_app(pilot)).reason

    reason = drive(body)
    assert reason is not None and "is not a socket" in reason
    assert ui_path.read_text(encoding="utf-8") == "mine"


@UNIX_SOCKETS
def test_a_squatted_socket_folder_means_no_receiver_said_once_and_asq_runs(
    tmp_path: Path,
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fleet(tmp_path, script)

    def squatted(**_: object) -> Path:
        raise OSError("refusing the ui socket folder /tmp/x: not a private folder (0700)")

    monkeypatch.setattr(captain_state, "ui_socket_path", squatted)

    async def body(pilot: Pilot[None]) -> tuple[bool, str | None, bool]:
        app = fleet_app(pilot)
        rx = receiver_of(app)
        return rx.listening, rx.reason, app.is_running

    with caplog.at_level(logging.WARNING, logger=receiver.__name__):
        listening, reason, running = drive(body)
    said = [record for record in caplog.records if record.name == receiver.__name__]
    assert running and not listening
    assert reason == "refusing the ui socket folder /tmp/x: not a private folder (0700)"
    assert len(said) == 1 and reason in said[0].getMessage()


def test_without_unix_sockets_the_receiver_is_a_said_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no ``AF_UNIX``; asq runs there without a receiver, and binds nothing."""
    made: list[bool] = []
    monkeypatch.setattr(captain_state, "ui_socket_path", lambda **_: made.append(True))
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    app = FleetApp(refresh_seconds=3600, doctor=lambda: [])
    rx = receiver.listen_for_ui(app)
    try:
        assert not rx.listening
        assert rx.reason is not None and "unix sockets" in rx.reason
        assert made == [], "no socket path was even asked for"
    finally:
        receiver.stop_listening_for_ui(app)


@UNIX_SOCKETS
def test_a_quit_while_an_action_waits_for_the_loop_does_not_wait_for_it(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quit runs on the app's loop, and an action in ``call_from_thread`` waits for that loop.

    Joining such a receiver would hold the quit for the whole join timeout (a
    deadlock, without one). The loop is held here the way a quit holds it,
    with an action already waiting on it.
    """
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[float, list[dict[str, Any]], receiver.UiReceiver]:
        app = fleet_app(pilot)
        rx = receiver_of(app)
        entered = threading.Event()
        real: Callable[..., Any] = app.call_from_thread

        def spy(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            return real(*args, **kwargs)

        monkeypatch.setattr(app, "call_from_thread", spy)
        replies: list[dict[str, Any]] = []
        client = threading.Thread(
            target=lambda: replies.append(ask(ui_path, request("select_project", "alpha"))),
            daemon=True,
        )
        client.start()
        assert entered.wait(5), "the action never reached the dispatch"
        began = time.monotonic()
        receiver.stop_listening_for_ui(app)  # on the loop, with the action waiting for it
        took = time.monotonic() - began
        await asyncio.to_thread(client.join, 5)
        await asyncio.to_thread(rx.join, 5)
        await pilot.pause()  # the selection the action posted lands before the app quits
        await pilot.pause()
        return took, replies, rx

    took, replies, rx = drive(body)
    assert took < receiver.JOIN_S / 2, f"quit waited {took:.2f}s on an action that needed it"
    assert replies == [{"ok": True, "said": "selected alpha"}], "the waiting action still answered"
    assert not rx.alive and not ui_path.exists()


@UNIX_SOCKETS
def test_every_action_runs_on_the_apps_thread(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Textual is not thread-safe: the receiver's thread hands each action to the loop."""
    fleet(tmp_path, script)
    ran_on: list[int] = []

    def where(app: FleetApp, arg: str | None) -> str:
        ran_on.append(threading.get_ident())
        return "here"

    for name in SIX:
        monkeypatch.setitem(receiver.ACTIONS, name, where)

    async def body(pilot: Pilot[None]) -> tuple[int, list[dict[str, Any]]]:
        loop_thread = threading.get_ident()  # the body runs on the app's loop
        return loop_thread, [await send(pilot, ui_path, name, "alpha") for name in sorted(SIX)]

    loop_thread, replies = drive(body)
    assert replies == [{"ok": True, "said": "here"}] * len(SIX)
    assert ran_on == [loop_thread] * len(SIX)


@UNIX_SOCKETS
def test_a_long_answer_is_cut_to_fit_the_clients_read(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet(tmp_path, script)

    def verbose(app: FleetApp, arg: str | None) -> str:
        raise RuntimeError("x" * 100_000)

    monkeypatch.setitem(receiver.ACTIONS, "copy_row", verbose)

    async def body(pilot: Pilot[None]) -> dict[str, Any]:
        return await send(pilot, ui_path, "copy_row", "alpha")

    reply = drive(body)
    assert reply["ok"] is False
    assert reply["said"].startswith("error: RuntimeError: xxx")
    assert len(reply["said"]) == receiver.SAID_MAX


@UNIX_SOCKETS
def test_a_client_that_says_nothing_is_cut_off_in_time_for_the_next(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    """One connection at a time: a silent one may hold the next up, but not past T1's 2 s."""
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[dict[str, Any], float, dict[str, Any]]:
        with _unix_socket() as silent:
            silent.settimeout(5)
            silent.connect(str(ui_path))
            began = time.monotonic()
            reply = await send(pilot, ui_path, "select_project", "alpha")  # 2 s, as T1 waits
            took = time.monotonic() - began
            with silent.makefile("rb") as stream:
                told = json.loads(stream.readline(64 * 1024))
        return reply, took, told

    reply, took, told = drive(body)
    assert reply == {"ok": True, "said": "selected alpha"}
    assert took < actions.UI_TIMEOUT_S
    assert told == {"ok": False, "said": f"not a request: no line within {receiver.READ_S:g}s"}


@UNIX_SOCKETS
def test_quit_does_not_wait_out_a_client_that_says_nothing(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[float, dict[str, Any]]:
        app = fleet_app(pilot)
        with _unix_socket() as silent:
            silent.settimeout(5)
            silent.connect(str(ui_path))
            await asyncio.sleep(0.1)  # let the receiver take it and start waiting for its line
            began = time.monotonic()
            receiver.stop_listening_for_ui(app)
            took = time.monotonic() - began
            with silent.makefile("rb") as stream:
                told = json.loads(stream.readline(64 * 1024))
        return took, told

    took, told = drive(body)
    assert took < receiver.READ_S / 2, f"quit waited {took:.2f}s for a line that never came"
    assert told == {"ok": False, "said": "asq is quitting"}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux blocks a connect to a full backlog")
def test_a_socket_that_cannot_say_whether_it_is_live_is_left_alone(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    """A listener too busy to take the dial (a full backlog) times it out: taken as live."""
    fleet(tmp_path, script)
    busy = _unix_socket()
    busy.bind(str(ui_path))
    busy.listen(0)
    queued = _unix_socket()
    queued.connect(str(ui_path))  # the one connection a zero backlog holds
    try:

        async def body(pilot: Pilot[None]) -> str | None:
            return receiver_of(fleet_app(pilot)).reason

        reason = drive(body)
        still = stat.S_ISSOCK(os.lstat(ui_path).st_mode)
    finally:
        queued.close()
        busy.close()
        ui_path.unlink(missing_ok=True)
    assert reason is not None and reason.startswith("another asq already listens at")
    assert still


@UNIX_SOCKETS
def test_stopping_the_receiver_waits_for_its_thread(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop returns once the thread has ended — here after one slow last answer.

    The slow answer is what makes the wait visible: a thread that only has to
    wake and close up can end before the caller looks, join or no join.
    """
    fleet(tmp_path, script)
    real = receiver._reply

    def slow(conn: socket.socket, ok: bool, said: str) -> None:
        time.sleep(0.3)
        real(conn, ok, said)

    monkeypatch.setattr(receiver, "_reply", slow)

    async def body(pilot: Pilot[None]) -> tuple[bool, bool, bool, bool, dict[str, Any]]:
        app = fleet_app(pilot)
        rx = receiver_of(app)
        again = receiver.listen_for_ui(app) is rx  # one receiver per app, however often asked
        with _unix_socket() as silent:
            silent.settimeout(5)
            silent.connect(str(ui_path))
            await asyncio.sleep(0.1)  # the thread takes it and waits for its line
            receiver.stop_listening_for_ui(app)
            alive = rx.alive
            with silent.makefile("rb") as stream:
                told = json.loads(stream.readline(64 * 1024))
        return again, alive, rx.listening, ui_path.exists(), told

    again, alive, listening, exists, told = drive(body)
    assert again
    assert not alive, "stop returned with the receiver's thread still running"
    assert told == {"ok": False, "said": "asq is quitting"}, "its last answer went out first"
    assert not listening and not exists


@UNIX_SOCKETS
def test_an_action_read_just_before_quit_is_told_asq_is_quitting(
    tmp_path: Path, script: Script, ui_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the quit race: the line is read before quit, dispatched after it.

    Dispatched, it would wait in ``call_from_thread`` for the loop that quit is
    holding while it joins — the join would run out its whole timeout.
    """
    fleet(tmp_path, script)
    parsed = threading.Event()
    release = threading.Event()
    real = receiver._parse

    def held(line: bytes) -> tuple[str, str | None]:
        parsed.set()
        release.wait(5)
        return real(line)

    monkeypatch.setattr(receiver, "_parse", held)

    async def body(pilot: Pilot[None]) -> tuple[float, list[dict[str, Any]], receiver.UiReceiver]:
        app = fleet_app(pilot)
        rx = receiver_of(app)
        replies: list[dict[str, Any]] = []
        client = threading.Thread(
            target=lambda: replies.append(ask(ui_path, request("select_project", "alpha"))),
            daemon=True,
        )
        client.start()
        assert await asyncio.to_thread(parsed.wait, 5), "the line never reached the parse"
        threading.Timer(0.1, release.set).start()  # the thread goes on while quit waits for it
        began = time.monotonic()
        receiver.stop_listening_for_ui(app)
        took = time.monotonic() - began
        await asyncio.to_thread(client.join, 5)
        return took, replies, rx

    took, replies, rx = drive(body)
    assert replies == [{"ok": False, "said": "asq is quitting"}]
    assert took < receiver.JOIN_S / 2, f"quit waited {took:.2f}s"
    assert not rx.alive


# --- the vocabulary and the suite's own socket root -----------------------------------------


def test_every_bundled_ui_step_names_an_action_the_receiver_knows() -> None:
    steps = [step for steps in actions.BUNDLED_ACTIONS.values() for step in steps]
    named = {step.split()[1] for step in steps if step.split()[0] == "ui"}
    assert named, "the premise: the bundled action list has ui steps"
    assert named <= set(receiver.ACTIONS)
    assert set(receiver.ACTIONS) == SIX
    assert all(actions._UI_ACTION.fullmatch(name) for name in receiver.ACTIONS)


def test_a_long_homes_socket_folder_is_the_tests_own_never_the_shared_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asq binds its receiver on mount, so every test that drives the shell binds one.

    A home too long for a unix socket (a macOS ``$TMPDIR``, a long user name)
    puts it in the short root; the suite points that root at a folder of the
    test's own. Computed only here — nothing is made, so a broken fixture fails
    this test without touching the shared folder.
    """
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / ("a" * 70) / ("b" * 40)))
    path = captain_state.ui_socket_path()
    shared = Path(tempfile.gettempdir() if sys.platform == "win32" else "/tmp")
    assert path.parent.name == f"aisquare-{captain_state._user_tag()}", (
        "the premise: the short root"
    )
    assert path.parent != shared / f"aisquare-{captain_state._user_tag()}"
