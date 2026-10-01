"""Attaching a persona to a BUSY agent puts one line on the board, and never the persona.

Review of #240, finding 7. ``fleet.attach_persona`` delivered its briefing through
``tell``, and ``tell``'s fallback for an agent it may not type into is a board note. So
for a busy agent the whole fenced persona body and its guard sentence became an ordinary
``note``: every OTHER session on the board read "Persona "skeptic" shapes how you work…"
in its next prompt delta as if it were addressed to it, ``aisquare board`` printed it, a
teammate starting on that board was briefed with the block although its own persona is
none, and the distiller wrote it into the project brain as a fact. docs/personas.md and
docs/plans/spawn-personas.md §3.1 say the opposite: the board gets one
``persona_attached`` line, with the name and never the body, and no factual surface
carries persona text.

The rule these pins hold: only an agent that is waiting is typed the briefing. For any
other, nothing is filed in its place: the board carries the one ``persona_attached``
line, the persona is on the agent's rows, and the agent is handed the briefing by its
own next prompt, for it alone
(``tests/test_a_busy_agent_is_briefed_privately_at_its_next_prompt.py``), or by a
session start that comes first. The receipt and the CLI say that, not that anything was
delivered; the Personas tab's toast is pinned in
``tests/test_the_attach_toast_says_when_the_persona_applies.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import personas
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent
from aisquare.services import distill as distill_service
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.rendered import plain
from tests.test_fleet_service import PANE_PID, FakeTmux, _become, _board_session, _clear, _coder

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

REVIEWER = "22222222-3333-4444-5555-666666666666"
"""A second session on the same board: the teammate whose delta carried the persona."""
TESTER = "33333333-4444-5555-6666-777777777777"
"""A session that starts on the board after the attach, with no persona of its own."""
CLEARED = "44444444-5555-6666-7777-888888888888"
"""The id Claude Code gives the busy agent's pane after its next ``/clear``."""

LINE = "persona skeptic attached to coder-1"
"""All the board says about an attachment: who got which persona."""
LATER = (
    "so the briefing was not typed: skeptic is recorded and reaches it with its next "
    "prompt, for it alone; the board carries the name only"
)
"""What a caller is told when the agent could not be typed into, after the reason."""


@pytest.fixture
def busy(tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo) -> FleetAgent:
    """coder-1 in the middle of a turn: nothing may be typed into it."""
    agent = _coder(project)
    _board_session(agent, "working")
    return agent


def _events(project: ProjectInfo) -> list[TeamEvent]:
    with store_session() as store:
        return store.filtered_events(project.id, since_seq=0, limit=200)


def _leaked(surface: str, project: ProjectInfo) -> list[str]:
    """Every line of skeptic's briefing — its frame, its body, its guard sentence — that
    ``surface`` carries. Compared flattened, so a line the board wrapped cannot hide."""
    flat = plain(surface)
    briefing = personas.briefing(personas.resolve("skeptic", project.root))
    return [line for line in briefing if line.strip() and plain(line) in flat]


def test_a_busy_agent_gets_one_persona_attached_line_and_nothing_else_on_the_board(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    drains: list[Path | None] = []
    monkeypatch.setattr(
        distill_service, "spawn_drain", lambda cwd=None, *, root=None: drains.append(root)
    )
    before = len(_events(project))

    receipt = fleet_service.attach_persona(project, "coder-1", "skeptic")

    written = _events(project)[before:]
    assert [(event.kind, event.text, event.to_role) for event in written] == [
        ("persona_attached", LINE, "coder-1")
    ], "exactly one event, which names the persona — and no note beside it"
    assert _leaked("\n".join(event.text for event in written), project) == []
    assert drains == [], "nothing went to the distiller: the brain learns no persona as a fact"
    assert tmux.typed == [], "never typed into a busy agent"
    assert (receipt.persona, receipt.delivered) == ("skeptic", "noted")


def test_no_other_session_reads_the_persona_after_an_attach_to_a_busy_agent(
    busy: FleetAgent, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The reviewer's next delta, ``aisquare board`` and a tester starting on the board:
    each shows the one line, and none of them a line of the persona."""
    monkeypatch.setenv("AISQUARE_ROLE", "reviewer")
    team_service.hook_session_start(REVIEWER, project.root, "startup")

    fleet_service.attach_persona(project, "coder-1", "skeptic")

    delta = team_service.hook_prompt_heartbeat(REVIEWER, project.root)
    monkeypatch.chdir(project.root)
    board = runner.invoke(app, ["board"])
    assert board.exit_code == 0, board.output
    monkeypatch.setenv("AISQUARE_ROLE", "tester")
    briefing = team_service.hook_session_start(TESTER, project.root, "startup")
    surfaces = {"delta": delta, "board": board.stdout, "briefing": briefing}
    for name, surface in surfaces.items():
        assert LINE in plain(surface), f"the {name} was empty of it — nothing was checked"
    assert {name: _leaked(surface, project) for name, surface in surfaces.items()} == {
        "delta": [],
        "board": [],
        "briefing": [],
    }


def test_a_session_start_of_the_busy_agent_briefs_it_once_from_its_rows(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows record the persona, and the session-start hook reads the fleet row, so a
    start that follows a ``/clear`` in the agent's own pane carries the block: once, in
    its place before the closing tag, and not a second time among the board's recent
    updates."""
    fleet_service.attach_persona(project, "coder-1", "skeptic")

    assert busy.session_id is not None
    with store_session() as store:
        row = store.get_fleet_agent(busy.id)
        session = store.get_session(busy.session_id)
    assert row is not None and row.persona == "skeptic"
    assert session is not None and session.persona == "skeptic"
    _become(busy, tmux, monkeypatch, pid=PANE_PID, role="coder")
    lines = _clear(busy.session_id, CLEARED, project).split("\n")
    block = personas.briefing(personas.resolve("skeptic", project.root))
    assert sum(line.startswith("<aisquare-persona ") for line in lines) == 1
    assert lines[-len(block) - 1 :] == [*block, "</aisquare-team>"]
    with store_session() as store:
        cleared = store.get_session(CLEARED)
    assert cleared is not None and cleared.persona == "skeptic"


@pytest.mark.parametrize(
    ("obstacle", "reason"),
    [
        ("working", "it is working"),
        ("attention", "it is attention"),
        (
            "launcher",
            "it reads as waiting, but its pane is not running the agent yet (the launcher "
            "or a shell is in the foreground)",
        ),
        ("tmux", "tmux could not type it (send-keys failed (fake))"),
    ],
)
def test_whatever_keeps_the_briefing_from_being_typed_nothing_is_filed_in_its_place(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, obstacle: str, reason: str
) -> None:
    """``tell`` has three reasons not to type, and files a note for each. The attachment
    takes none of them as a reason to put the persona on the board: the receipt gives the
    reason in ``tell``'s words, then says when the persona applies."""
    agent = _coder(project)
    _board_session(agent, obstacle if obstacle in ("working", "attention") else "waiting")
    if obstacle == "tmux":
        tmux.set_command(agent.pane_id, "claude")
        tmux.fail_input = True

    receipt = fleet_service.attach_persona(project, "coder-1", "skeptic")

    assert (receipt.delivered, receipt.how) == ("noted", f"{reason}, {LATER}")
    kinds = [event.kind for event in _events(project)]
    assert kinds.count("persona_attached") == 1 and "note" not in kinds, kinds
    assert tmux.typed == []
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.persona == "skeptic"


def test_the_cli_says_how_the_persona_reaches_the_agent_and_claims_no_delivery(
    busy: FleetAgent, project: ProjectInfo, runner: CliRunner
) -> None:
    said = runner.invoke(app, ["persona", "attach", "skeptic", "--to", "coder-1", "-P", project.id])
    as_json = runner.invoke(
        app, ["--json", "persona", "attach", "skeptic", "--to", "coder-1", "-P", project.id]
    )

    assert said.exit_code == 0, said.output
    assert said.stdout.splitlines() == [
        "✓ attached skeptic to coder-1 (noted)",
        f"  it is working, {LATER}",
    ]
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert (payload["delivered"], payload["how"]) == ("noted", f"it is working, {LATER}")
