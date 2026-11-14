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

import ast
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

from aisquare.models import ProjectInfo
from aisquare.services.captain import actions
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _coder, _spawned_on

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
    # Spawned there: its row and its launch record's place both say so, as a spawn writes
    # them (`_spawned_on`), for the fleet places a row by its record alone.
    _spawned_on(_coder(project, label="coder-9"), socket_path=APART, pane_id=window.pane_id)

    said = json.loads(
        actions.read_pane(project.root.name, "coder-9", utterance="what is coder-9 doing")
    )

    assert said["lines"] == ["the row's own agent, on its own server"]


# --- every door, by its source (review of #240: the pin above drives read_pane alone) ------

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"
CAPTAIN_SOURCES = (
    *sorted((SRC / "services" / "captain").glob("*.py")),
    *sorted((SRC / "cli").glob("captain*.py")),
    SRC / "cli" / "ui" / "views" / "captain.py",
)
DOORS = (
    ("services/captain/actions.py", "_pane_tail"),  # reading a pane (the pin above)
    ("services/captain/actions.py", "_ready"),  # the screen check before it types
    ("services/captain/actions.py", "_before_telling"),  # the prompt check before a tell
    ("services/captain/actions.py", "_wololo"),  # the prompt check before a conversion
    ("services/captain/brain.py", "_wait_until_ready"),  # its own first prompt
    ("services/captain/queue.py", "StoreSources"),  # the queue's prompt scan
)
"""50f1dc43's six doors: each asks ``fleet.server_of(agent)``."""


def _calls(node: ast.AST, name: str) -> list[int]:
    """The lines where ``node`` calls something by the attribute or the bare name ``name``."""
    return [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Attribute) and call.func.attr == name)
            or (isinstance(call.func, ast.Name) and call.func.id == name)
        )
    ]


def test_no_captain_module_asks_a_server_by_its_name_alone() -> None:
    """``fleet.server_for`` asks ``tmux -L <name>`` from this shell: under another
    ``TMUX_TMPDIR`` that is another server, whose pane under the row's id is another
    agent's. Not one call to it anywhere in the captain's code."""
    found = {
        str(path.relative_to(SRC)): lines
        for path in CAPTAIN_SOURCES
        if (lines := _calls(ast.parse(path.read_text(encoding="utf-8")), "server_for"))
    }
    assert found == {}


@pytest.mark.parametrize(("module", "door"), DOORS, ids=[door for _, door in DOORS])
def test_each_door_asks_the_server_its_row_recorded(module: str, door: str) -> None:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    (node,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.ClassDef) and n.name == door
    ]
    assert _calls(node, "server_of"), f"{module}:{door} no longer asks fleet.server_of(agent)"
