"""A persona attached to a BUSY agent reaches that agent with its next prompt, and it alone.

Review of #240, finding 7, as the owner ruled it. ``fleet.attach_persona`` types its
briefing into an agent that is waiting. For one that is busy it puts nothing of the
persona on the board, where every other session would read it
(``tests/test_attaching_to_a_busy_agent_keeps_the_persona_off_the_board.py``); it records
that the agent is OWED the briefing, under the agent's fleet row in ``team_meta``. The
agent's own ``UserPromptSubmit`` hook hands it over: the output of its next prompt
carries the preface line the typed path uses and the block of the row's current
persona, once, and the marker is gone.

What these pins hold besides: a session start briefs the row's persona itself, so it
settles what was owed; a later attach replaces what an earlier one owed; a persona
removed in between costs one line; a session that is not the row's agent is handed
nothing and uses nothing up, although a child process inherits the window's
``AISQUARE_FLEET_AGENT``; and a session that is owed nothing reads byte for byte what it
read before, without a query when it sits in no fleet window.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import personas
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state
from tests import test_fleet_service as fleet_suite
from tests.rendered import plain
from tests.test_fleet_service import (
    CHILD_PID,
    PANE_PID,
    FakeTmux,
    _become,
    _board_session,
    _clear,
    _coder,
    _preface,
    _stranger,
)

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

REVIEWER = "22222222-3333-4444-5555-666666666666"
"""A second session on the same board, in no fleet window."""
TESTER = "33333333-4444-5555-6666-777777777777"
"""A session that starts on the board after the attach."""
CLEARED = "44444444-5555-6666-7777-888888888888"
"""The id Claude Code gives the busy agent's pane after a ``/clear``."""
CHILD = "55555555-6666-7777-8888-999999999999"
"""A ``claude -p`` the agent started from its own shell: it inherits the window's variables."""

OWED = "persona_owed:"
"""The ``team_meta`` key prefix of the marker; the fleet row's id follows."""


def _attached(name: str, *, replaces: str | None = None) -> str:
    """The board's one line about an attachment, as a delta renders it for coder-1."""
    tail = f" (replaces {replaces})" if replaces else ""
    return f"cli persona_attached → coder-1: persona {name} attached to coder-1{tail}"


def _delta(*updates: str) -> str:
    """The per-prompt delta of the ``repo`` board carrying exactly ``updates``."""
    return "\n".join(
        [
            "<aisquare-team-delta>",
            f"{len(updates)} teammate update(s) since your last prompt:",
            *(f"- [repo] {update}" for update in updates),
            "</aisquare-team-delta>",
        ]
    )


def _briefing(name: str, project: ProjectInfo, *, replaces: str | None = None) -> str:
    """What the typed path pastes: the preface, the fenced body, the guard sentence."""
    block = personas.briefing(personas.resolve(name, project.root))
    return "\n".join([_preface(name, replaces=replaces), *block]) + "\n"


@pytest.fixture
def busy(tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo) -> FleetAgent:
    """coder-1 in the middle of a turn: nothing may be typed into it."""
    agent = _coder(project)
    _board_session(agent, "working")
    return agent


@pytest.fixture
def mentor(tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo) -> FleetAgent:
    """coder-1, launched as mentor, in the middle of a turn."""
    agent = _coder(project, persona="mentor")
    _board_session(agent, "working")
    return agent


def _prompt(agent: FleetAgent, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> str:
    """The agent's next ``UserPromptSubmit``, as its own hook runs it: in its window, as
    the process in its pane, for the session its row records."""
    assert agent.session_id is not None
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role=agent.role)
    return team_service.hook_prompt_heartbeat(agent.session_id, agent.cwd)


@pytest.fixture
def meta_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every ``team_meta`` key read from here on, in order."""
    keys: list[str] = []
    read = SqliteStore.get_meta

    def recording(self: SqliteStore, key: str) -> str | None:
        keys.append(key)
        return read(self, key)

    monkeypatch.setattr(SqliteStore, "get_meta", recording)
    return keys


# --- the briefing reaches the agent it is for, once ---------------------------------------


def test_a_busy_agents_next_prompt_carries_the_preface_and_the_block_once(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet_service.attach_persona(project, "coder-1", "skeptic")

    first = _prompt(busy, tmux, monkeypatch)
    second = _prompt(busy, tmux, monkeypatch)

    assert first == _briefing("skeptic", project) + _delta(_attached("skeptic"))
    assert second == "", "handed over once: a second prompt adds nothing"


def test_the_briefing_arrives_without_a_delta_to_ride_on(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A muted delta (``AISQUARE_TEAM_DELTA=0``) takes the hook's early return. The briefing
    is not teammate traffic and must not wait for some: it survives that return, as the
    collision banner and the late assignment do."""
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    monkeypatch.setenv("AISQUARE_TEAM_DELTA", "0")

    assert _prompt(busy, tmux, monkeypatch) == _briefing("skeptic", project)
    assert _prompt(busy, tmux, monkeypatch) == ""


def test_two_attaches_while_busy_deliver_the_latest_persona_once(
    mentor: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later persona, once, and its preface names the persona the agent RAN as: it was
    never handed skeptic, so "it replaces skeptic" would name one it has not heard of. The
    board and the receipt keep the rows' account, where careful did replace skeptic."""
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    receipt = fleet_service.attach_persona(project, "coder-1", "careful")

    first = _prompt(mentor, tmux, monkeypatch)
    second = _prompt(mentor, tmux, monkeypatch)

    assert first == _briefing("careful", project, replaces="mentor") + _delta(
        _attached("skeptic", replaces="mentor"), _attached("careful", replaces="skeptic")
    )
    assert first.count("<aisquare-persona ") == 1 and second == ""
    assert receipt.replaced == "skeptic"


def test_attaching_the_same_persona_again_while_busy_still_names_what_it_replaces(
    mentor: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second attach replaces nothing on the rows, which already say skeptic. The agent
    still runs as mentor, and the briefing it is owed still says so."""
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    fleet_service.attach_persona(project, "coder-1", "skeptic")

    first = _prompt(mentor, tmux, monkeypatch)

    assert first.startswith(_briefing("skeptic", project, replaces="mentor")), first


def test_an_attach_that_is_typed_later_leaves_nothing_owed(
    mentor: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Busy at the first attach, waiting at the second: the second is typed, and the prompt
    that types it runs the very hook that hands over what the first one owed. So the typed
    attach settles it BEFORE it types, and the agent reads the block once; what it is told
    the block replaces is mentor, the persona it ran as, not skeptic, which never reached
    it."""
    assert mentor.session_id is not None
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    with store_session() as store:
        store.touch_session(mentor.session_id, state="waiting")
    tmux.set_command(mentor.pane_id, "claude")

    receipt = fleet_service.attach_persona(project, "coder-1", "careful")
    prompt = _prompt(mentor, tmux, monkeypatch)

    assert receipt.delivered == "typed"
    assert tmux.typed[0] == (
        mentor.pane_id,
        "paste",
        _briefing("careful", project, replaces="mentor").rstrip("\n"),
    )
    assert "<aisquare-persona" not in prompt, "the hook adds no second copy to what was typed"


# --- a session start briefs the persona itself -----------------------------------------------


def test_a_clear_before_its_next_prompt_briefs_it_at_the_start_and_the_prompt_adds_nothing(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert busy.session_id is not None
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    _become(busy, tmux, monkeypatch, pid=PANE_PID, role="coder")

    started = _clear(busy.session_id, CLEARED, project).split("\n")
    prompt = team_service.hook_prompt_heartbeat(CLEARED, busy.cwd)

    block = personas.briefing(personas.resolve("skeptic", project.root))
    assert started[-len(block) - 1 :] == [*block, "</aisquare-team>"]
    assert sum(line.startswith("<aisquare-persona ") for line in started) == 1
    assert prompt == "", "the session start was the briefing: nothing is owed after it"


def test_the_captains_session_start_settles_what_it_was_owed_too(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The captain's start returns a briefing of its own, on a branch of its own: it reads no
    board. Its row's persona is in that briefing, so that branch settles the marker as well."""
    captain = brain.start().agent
    home = captain_state.home_project()
    _board_session(captain, "working")
    assert captain.session_id is not None
    fleet_service.attach_persona(home, "captain", "skeptic")
    _become(captain, tmux, monkeypatch, pid=PANE_PID, role="captain")

    started = _clear(captain.session_id, CLEARED, home)
    prompt = team_service.hook_prompt_heartbeat(CLEARED, captain.cwd)

    assert started.count('<aisquare-persona name="skeptic"') == 1
    assert "<aisquare-persona" not in prompt and "the operator attached persona" not in prompt


# --- what can go wrong costs a line at most ----------------------------------------------------


def test_a_persona_removed_before_the_prompt_costs_one_line_and_nothing_else(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = dict(personas.layer_dirs(None))["user"] / "pair"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\ndescription: Pairs on the work.\n---\nYou think aloud, one step at a time.\n",
        encoding="utf-8",
    )
    fleet_service.attach_persona(project, "coder-1", "pair")
    shutil.rmtree(directory)

    first = _prompt(busy, tmux, monkeypatch)
    second = _prompt(busy, tmux, monkeypatch)

    said, _, rest = first.partition("\n")
    assert said.startswith("persona \"pair\": no persona named 'pair' in "), said
    assert said.endswith(" — attached to you without its briefing"), said
    assert rest == _delta(_attached("pair")), "the hook's other output is intact"
    assert "think aloud" not in first and second == ""


def test_a_marker_that_cannot_be_read_costs_the_briefing_and_never_the_hooks_output(
    busy: FleetAgent,
    project: ProjectInfo,
    tmux: FakeTmux,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hook runs on every prompt of every agent. A read that fails is said in the hook's
    log (stderr) and costs this prompt the briefing: the delta arrives as it would have, and
    the marker stands, so the next prompt that can read it hands the briefing over."""
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    read = SqliteStore.get_meta

    def refusing(self: SqliteStore, key: str) -> str | None:
        if key.startswith(OWED):
            raise sqlite3.OperationalError("database is locked")
        return read(self, key)

    with monkeypatch.context() as patched, caplog.at_level(logging.WARNING):
        patched.setattr(SqliteStore, "get_meta", refusing)
        unread = _prompt(busy, tmux, monkeypatch)
    later = _prompt(busy, tmux, monkeypatch)

    assert unread == _delta(_attached("skeptic"))
    assert any("OperationalError" in record.getMessage() for record in caplog.records)
    assert later == _briefing("skeptic", project)


# --- nobody else is handed it, and nobody else pays for it ---------------------------------------


def test_another_session_reads_what_it_read_before_and_asks_the_store_nothing(
    busy: FleetAgent,
    project: ProjectInfo,
    tmux: FakeTmux,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    meta_reads: list[str],
) -> None:
    """A reviewer on the same board, in no fleet window. Before and after the busy agent's own
    prompt took the briefing, the reviewer's delta is the one ``persona_attached`` line and
    then nothing, and its hook never looks for a marker. ``aisquare board`` and a tester
    starting on the board afterwards show the line and no line of the persona."""
    _stranger(monkeypatch, "reviewer")
    team_service.hook_session_start(REVIEWER, project.root, "startup")
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    del meta_reads[:]

    before = team_service.hook_prompt_heartbeat(REVIEWER, project.root)
    asked = [key for key in meta_reads if key.startswith(OWED)]
    handed = _prompt(busy, tmux, monkeypatch)
    _stranger(monkeypatch, "reviewer")
    after = team_service.hook_prompt_heartbeat(REVIEWER, project.root)
    monkeypatch.chdir(project.root)
    board = runner.invoke(app, ["board"])
    _stranger(monkeypatch, "tester")
    briefing = team_service.hook_session_start(TESTER, project.root, "startup")

    assert handed.startswith(_briefing("skeptic", project)), "the agent it is for did get it"
    assert before == _delta(_attached("skeptic")) and after == ""
    assert asked == [], "outside a fleet window the hook reads no marker at all"
    block = personas.briefing(personas.resolve("skeptic", project.root))
    for surface in (board.stdout, briefing):
        assert "persona skeptic attached to coder-1" in plain(surface)
        assert [line for line in block if line.strip() and plain(line) in plain(surface)] == []


def test_a_child_of_the_agent_is_handed_nothing_and_uses_nothing_up(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``claude -p`` started from the agent's shell inherits ``AISQUARE_FLEET_AGENT``. The
    variable names the row; it does not make the child that row's agent (rule 1 of the
    fleet-row section in ``services.team``). Neither the child's session start nor its prompt
    may settle what the agent is owed, or the agent itself would never be briefed."""
    fleet_service.attach_persona(project, "coder-1", "skeptic")
    _become(busy, tmux, monkeypatch, pid=CHILD_PID, role="coder")  # the pane itself stays PANE_PID

    team_service.hook_session_start(CHILD, busy.cwd, "startup")
    childs = team_service.hook_prompt_heartbeat(CHILD, busy.cwd)
    agents = _prompt(busy, tmux, monkeypatch)

    assert "<aisquare-persona" not in childs and _preface("skeptic") not in childs
    assert agents.startswith(_briefing("skeptic", project)), "still owed to the agent itself"


def test_a_fleet_agent_that_is_owed_nothing_reads_what_it_read_before(
    busy: FleetAgent, project: ProjectInfo, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the hook's every-prompt path: no marker, no change. One teammate note
    is the delta, byte for byte; a prompt with no news is the empty string."""
    team_service.add_note("the cut is done", cwd=project.root)

    assert _prompt(busy, tmux, monkeypatch) == _delta("cli note: the cut is done")
    assert _prompt(busy, tmux, monkeypatch) == ""
