"""A hand-off from a teammate runs as the persona the Spawn dialog shows.

Two of the review of #240's "also confirmed" items, both in the hand-off (#234), reached with
the captain off:

- **Take over with Persona "(none)".** The replacement RESUMES the source's session, and its
  start hook asks the row: a row with no persona asks for nothing, so the session kept the
  persona it recorded, and every start after the take-over briefed it again.
- **Fork with a changed role.** The dialog sends ``persona=None`` when it shows the
  teammate's own persona, and ``None`` keeps the source's. A fork with another role started
  without it instead (the new role's default, or none).

The fleet is the real one, over the fleet suite's fake tmux, in a real checkout; the start
hook is the real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _coder, _command, _flag, _on_disk

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

pytestmark = pytest.mark.usefixtures("project_capture")


def _briefing(session_id: str, project: ProjectInfo, agent_id: str, mp: pytest.MonkeyPatch) -> str:
    """What the session-start hook briefs ``session_id`` with, in the window of ``agent_id``."""
    mp.setenv("AISQUARE_FLEET_AGENT", agent_id)
    mp.setenv("AISQUARE_ROLE", "coder")
    mp.delenv("AISQUARE_PERSONA", raising=False)
    return team_service.hook_session_start(session_id, project.root, "resume")


def _session_persona(session_id: str) -> str | None:
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None
    return session.persona


def test_a_take_over_to_persona_none_briefs_no_persona_at_its_next_start(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _coder(project, persona="skeptic")
    sid = source.session_id or ""
    _on_disk(source, tmp_path)  # a transcript, so the take-over resumes this session
    with store_session() as store:
        store.set_session_persona(sid, "skeptic")  # what its first start recorded
    assert "<aisquare-persona" in _briefing(sid, project, source.id, monkeypatch), "premise"

    receipt = fleet_service.hand_off(project, source.label, mode="take_over", persona="")

    replaced = receipt.started.agent
    assert receipt.resumed and replaced.session_id == sid, "the same session, resumed"
    assert replaced.persona is None
    assert _session_persona(sid) is None, "the session runs as none now"
    assert "<aisquare-persona" not in _briefing(sid, project, replaced.id, monkeypatch)


def test_a_take_over_that_keeps_the_persona_still_briefs_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the dialog sends ``None`` for the persona it shows, and the resumed
    session keeps it."""
    source = _coder(project, persona="skeptic")
    sid = source.session_id or ""
    _on_disk(source, tmp_path)
    with store_session() as store:
        store.set_session_persona(sid, "skeptic")

    receipt = fleet_service.hand_off(project, source.label, mode="take_over")

    assert receipt.started.agent.persona == "skeptic"
    assert _session_persona(sid) == "skeptic"
    assert "<aisquare-persona" in _briefing(sid, project, receipt.started.agent.id, monkeypatch)


def test_a_fork_as_another_role_keeps_the_persona_the_dialog_shows(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    source = _coder(project, persona="skeptic")

    fork = fleet_service.hand_off(project, source.label, fresh=True, role="tester")

    assert fork.started.agent.role == "tester"
    assert fork.started.agent.persona == "skeptic", "None keeps the source's"
    assert _flag(_command(tmux), "--persona") == "skeptic"


def test_a_fork_as_another_role_with_persona_none_runs_without_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The control: a persona the dialog changed is the dialog's, "(none)" included."""
    source = _coder(project, persona="skeptic")

    fork = fleet_service.hand_off(project, source.label, fresh=True, role="tester", persona="")

    assert fork.started.agent.persona is None
    assert "--persona" not in _command(tmux)
