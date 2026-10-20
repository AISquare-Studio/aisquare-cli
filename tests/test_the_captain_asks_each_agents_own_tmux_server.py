"""The captain asks each agent's pane of the server its row recorded, as every fleet command does.

0.8.0 (round 28 of #257, docs/fleet.md "Each agent is asked of its own server"): a spawn
records the socket its server says it listens on, and every question about one row's pane
is put to that server (``fleet.server_of``), never to ``tmux -L <name>`` from the asking
shell. Under another ``TMUX_TMPDIR`` that name reaches another server, or none, and every
server numbers its panes from ``%0``, so its pane under the row's id is another agent's.

The captain was written against 0.7.0 and asked by name (``fleet.server_for``) at each of
its doors: reading a pane, the screen check before it types, the prompt check before a
tell, the queue's prompt scan and its own first prompt. Here a row's server listens at
another path than this shell's ``-L`` finds, and the server ``-L`` finds holds a pane under
the same id: the captain reads the row's own.

The fleet is the real one, over the fleet suite's fake tmux, in a real checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

from aisquare.core.ids import new_agent_id
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services.captain import actions
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _coder

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

APART = Path("/elsewhere/tmux-1000/asq-test")
"""Another ``TMUX_TMPDIR``'s server, still up: where the row's server said it listened."""


def test_the_captain_reads_a_pane_on_the_server_its_row_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    here = _coder(project)  # a pane under some id on the server -L finds from this shell
    elsewhere = tmux.per_path[APART] = FakeTmux()
    elsewhere.started = tmux.started
    window = elsewhere.spawn_window("asq-x", name="coder-9", cwd=project.root, command=["c"])
    assert window.pane_id == here.pane_id, "the premise: both servers number their panes alike"
    elsewhere.set_command(window.pane_id, "claude")
    elsewhere.screens[window.pane_id] = ["the row's own agent, on its own server"]
    tmux.screens[here.pane_id] = ["another agent's screen, under the same pane id"]
    moved = {
        "id": new_agent_id(),
        "socket_path": APART,
        "pane_id": window.pane_id,
        "label": "coder-9",
    }
    with store_session() as store:
        store.upsert_fleet_agent(_coder(project).model_copy(update=moved))

    said = json.loads(
        actions.read_pane(project.root.name, "coder-9", utterance="what is coder-9 doing")
    )

    assert said["lines"] == ["the row's own agent, on its own server"]
