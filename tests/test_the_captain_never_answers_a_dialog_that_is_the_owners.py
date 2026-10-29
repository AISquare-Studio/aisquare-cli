"""The captain's press and paste refuse every dialog whose answer is the owner's.

Review r17 of #113, finding 5 (adb1c980). ``press`` and ``paste`` refused only a ``trust``
prompt. Codex's other dialogs of its own (``ScreenPrompt`` shape ``dialog``: its hook
review, ``Switch to gpt-6-luna?``, ``Implement this plan?``, its fork's ``Working
directory`` chooser, its update dialog) read ``attention``, so a literal key (``enter``,
``1``) and a paste with its Enter went straight in: "press enter on coder-2" at *Hooks need
review* trusted new hook commands that run on every event, the same class of decision as
folder trust. Every ``trust`` and ``dialog`` screen of each agent's own (``tests/fixtures/
agent_screens/<id>.json``) now refuses every key and every paste, naming it; a ``chooser``
and a ``[y/N]`` line stay answerable.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import screen_of
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services.captain import actions
from tests import test_captain_actions as suite
from tests.captain_screens import INPUT_BOX
from tests.test_captain_actions import Fleet, Pane, ok, refused

projects = suite.projects
alpha = suite.alpha
fleet_rec = suite.fleet_rec

#: The pane's process for each agent, and the screen it shows at its empty prompt.
AGENTS = {"claude-code": "claude", "codex": "codex"}
FIXTURES = Path(__file__).parent / "fixtures" / "agent_screens"


def _dialogs(agent: str) -> list[dict[str, Any]]:
    screens: dict[str, Any] = json.loads((FIXTURES / f"{agent}.json").read_text("utf-8"))
    dialogs: list[dict[str, Any]] = screens["dialogs"]
    return dialogs


def _idle(agent: str) -> list[str]:
    if agent == "claude-code":
        return list(INPUT_BOX)
    screens: dict[str, Any] = json.loads((FIXTURES / f"{agent}.json").read_text("utf-8"))
    return list(next(s["rows"] for s in screens["not_dialogs"] if s.get("ready")))


def _shape(agent: str, rows: list[str]) -> str:
    read = screen_of(get_adapter(agent)).screen_prompt
    assert read is not None
    prompt = read(rows)
    assert prompt is not None
    return prompt.shape


def _cases(owners: bool) -> list[Any]:
    """Each agent's recorded dialogs: the owner's (``trust``, ``dialog``), or the others."""
    return [
        pytest.param(agent, shown["rows"], id=f"{agent}: {shown['what'][:48]}")
        for agent in AGENTS
        for shown in _dialogs(agent)
        if (_shape(agent, shown["rows"]) in ("trust", "dialog")) is owners
    ]


@pytest.fixture
def pane(request: pytest.FixtureRequest, alpha: ProjectInfo, fleet_rec: Fleet) -> Pane:
    """coder-1 on ``request.param``'s agent, asking (``attention``)."""
    agent: str = request.param
    row = suite._row(alpha, "coder-1", "coder", "%1").model_copy(update={"agent": agent})
    with store_session() as store:
        store.upsert_fleet_agent(row)
    shown = Pane(command=AGENTS[agent], screen=_idle(agent))
    fleet_rec.panes["%1"] = shown
    fleet_rec.states["coder-1"] = "attention"
    return shown


@pytest.mark.parametrize(("pane", "rows"), _cases(owners=True), indirect=["pane"])
def test_every_key_and_every_paste_at_a_dialog_of_the_owners_is_refused(
    alpha: ProjectInfo, pane: Pane, rows: list[str]
) -> None:
    pane.screen = list(rows)
    for key in ("yes", "no", "enter", "esc", "1", "2", "down"):
        message = refused(partial(actions.press, "alpha", "coder-1", key))
        assert "the owner's" in message, (key, message)
    for submit in (True, False):
        message = refused(partial(actions.paste, "alpha", "coder-1", "carry on", submit=submit))
        assert "the owner's" in message, (submit, message)
    assert pane.typed == []


@pytest.mark.parametrize(("pane", "rows"), _cases(owners=False), indirect=["pane"])
def test_a_chooser_or_a_yes_no_line_stays_the_captains_to_answer(
    alpha: ProjectInfo, pane: Pane, rows: list[str], request: pytest.FixtureRequest
) -> None:
    """The control: ``press no`` at a chooser or a ``[y/N]`` line sends its no."""
    agent = request.node.callspec.params["pane"]
    pane.screen = list(rows)
    read = screen_of(get_adapter(agent)).screen_prompt
    assert read is not None
    prompt = read(rows)
    assert prompt is not None and prompt.no_key is not None
    pane.answers = {prompt.no_key: _idle(agent)}
    result = ok(actions.press("alpha", "coder-1", "no"))
    assert result["sent"] == prompt.no_key and result["answered"] is True


def test_both_agents_bring_an_owners_dialog_and_one_the_captain_answers() -> None:
    """The parametrization is not vacuous: Codex's hook review is a ``dialog``, and each
    agent has a chooser."""
    shapes = {(agent, _shape(agent, rows)) for agent in AGENTS for rows in (
        shown["rows"] for shown in _dialogs(agent))}  # fmt: skip
    assert {("codex", "dialog"), ("codex", "trust"), ("claude-code", "trust")} <= shapes
    assert {("codex", "chooser"), ("claude-code", "chooser"), ("claude-code", "yn")} <= shapes
