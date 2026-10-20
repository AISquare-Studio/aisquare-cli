"""Personas under ``AISQUARE_TEAM_HUB``, and the window's persona variable (review of #201).

Since 0.8.1 a fleet seat is in its own project and on the hub's board. The persona code
came from a branch without the hub, and asked the board where it meant the seat:

- F1: ``launch --persona`` and the session-start briefing looked the persona up in the
  hub's layer, while ``fleet spawn`` checked it in the seat's own repository, so a
  project-layer persona passed the spawn and the window died at launch;
- F2: a session start read an attached persona only from a row in the board's project,
  so under a hub an attachment never survived ``/clear``;
- F4: the attachment's board line went to the board the caller resolved, and a hub
  nobody added ended ``persona attach`` in a traceback;

and, hub or not:

- F3: a persona-less spawn left ``AISQUARE_PERSONA`` to the tmux server's environment,
  which is whoever started the server's, so a hand-launched manager's persona briefed
  every persona-less seat on it;
- F5: ``--condense`` kept the source's frontmatter and files only below the hard cap,
  and over it, which is what ``--condense`` is for, kept nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.cli import launch as launch_cli
from aisquare.core import orchestrator
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests import test_persona_import as import_suite
from tests.test_fleet_service import (
    FakeTmux,
    _board_session,
    _clear,
    _coder,
    _events,
    _Hubbed,
    _row,
    _spawned,
)

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
hubbed = fleet_suite.hubbed
engines = import_suite.engines

CLEARED = "44444444-5555-6666-7777-888888888888"


def _project_persona(root: Path, name: str) -> None:
    """A persona in ``root``'s own project layer, which no other project has."""
    skill = root / ".aisquare" / "personas" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Only this repository has it.\n---\n"
        f"You are {name}. Work the way this repository does.\n",
        encoding="utf-8",
    )


def _block(name: str) -> str:
    return f'<aisquare-persona name="{name}"'


# --- F1: the seat's own layer, at launch and at session start ---------------------------


def test_launch_under_a_hub_finds_a_persona_in_its_own_repositorys_layer(
    project: ProjectInfo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window's ``launch`` runs in the seat's repository with the hub exported: the
    board is the hub's, and the persona the spawn checked is in the repository's layer."""
    _project_persona(project.root, "reporev")
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    monkeypatch.setenv(orchestrator.TEAM_HUB_ENV_VAR, str(hub_root))
    monkeypatch.chdir(project.root)
    board = orchestrator.team_project(project.root)
    assert board.root == hub_root.resolve()

    launch_cli._check_persona("reporev", board)  # refuses with typer.Exit when not found


def test_a_seat_under_a_hub_is_briefed_with_its_own_repositorys_persona(
    hubbed: _Hubbed,
) -> None:
    w = hubbed
    _project_persona(w.fleet.root, "reporev")
    _agent, session_id = _spawned(w.fleet, "tester", None, w.tmux, w.monkeypatch)
    w.monkeypatch.setenv(orchestrator.PERSONA_ENV_VAR, "reporev")

    started = team_service.hook_session_start(session_id, w.fleet.root, "startup")

    assert _block("reporev") in started, started[-600:]
    assert "no persona named" not in started


# --- F2: an attachment under a hub survives /clear --------------------------------------


def test_a_persona_attached_to_a_seat_under_a_hub_is_briefed_after_a_clear(
    hubbed: _Hubbed,
) -> None:
    w = hubbed
    _board_session(w.coder, "working")
    fleet_service.attach_persona(w.fleet, w.coder.label, "skeptic")
    assert _row(w.coder.id).persona == "skeptic"

    started = _clear(w.coder_sid, CLEARED, w.fleet)

    assert _block("skeptic") in started, started[-600:]


# --- F3: the window's AISQUARE_PERSONA is the spawn's, blank for none -------------------


def test_a_persona_less_spawn_blanks_the_persona_the_server_may_carry(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private tmux server keeps the environment of the process that started it: a
    manager launched ``--persona skeptic`` gave it ``AISQUARE_PERSONA=skeptic``."""
    monkeypatch.setenv(orchestrator.PERSONA_ENV_VAR, "skeptic")

    plain = fleet_service.spawn(project, "tester", worktree=False)
    fleet_service.spawn(project, "coder", worktree=False, persona="careful")

    assert plain.agent.persona is None
    envs = [spawned["env"] for spawned in tmux.spawned]
    assert all(isinstance(env, dict) for env in envs)
    assert [env[orchestrator.PERSONA_ENV_VAR] for env in envs if isinstance(env, dict)] == [
        "",
        "careful",
    ]


# --- F4: the attachment's line goes to the board the agent reads ------------------------


def test_attach_from_under_a_hub_nobody_added_files_its_line_where_the_agent_reads(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tell``'s rule (review of #259, round 26): the board the row recorded, so the
    caller's hub is never asked; with ``[projects] capture`` off a hub nobody added
    raised ``NotAdded`` out of ``persona attach``."""
    agent = _coder(project)
    _board_session(agent, "working")
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv(orchestrator.TEAM_HUB_ENV_VAR, str(hub))

    receipt = fleet_service.attach_persona(project, "coder-1", "skeptic")

    assert receipt.persona == "skeptic" and agent.board == project.id
    assert _events(project, "persona_attached") == ["persona skeptic attached to coder-1"]
    with store_session() as store:
        assert store.get_fleet_agent(agent.id).persona == "skeptic"  # type: ignore[union-attr]


# --- F5: --condense over the hard cap keeps the rest of the skill -----------------------


def test_condensing_a_skill_over_the_hard_cap_keeps_its_frontmatter_and_files(
    engines: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size over ``BODY_HARD_CAP`` is the case ``--condense`` exists for, and the
    skill did not parse there, so the keep of 0c1f92a5 applied to nothing."""
    from aisquare.core import personas

    source = tmp_path / "huge-skill"
    (source / "reference").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: huge-skill\ndescription: Huge.\nallowed-tools: Read, Grep\n"
        "metadata:\n  owner: platform\n---\n" + "w" * (personas.BODY_HARD_CAP + 1_000) + "\n",
        encoding="utf-8",
    )
    (source / "reference" / "checklist.md").write_text("- one\n", encoding="utf-8")
    import_suite._runs(monkeypatch, import_suite._envelope(name="huge-skill", body="Short."))

    result = import_suite._import(str(source), condense=True)

    front = result.persona.frontmatter
    assert front["allowed-tools"] == "Read, Grep"
    assert front["metadata"] == {"owner": "platform", "persona-source": str(source.resolve())}
    assert result.persona.files == ["reference/checklist.md"]
    assert result.persona.body == "Short."
