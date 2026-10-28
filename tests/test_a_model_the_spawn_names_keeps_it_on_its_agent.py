"""A model a spawn names keeps it on the agent that model is: Claude Code's, Codex's, a third's.

Review r29 of #113, m1, kept a Claude model on Claude Code; its delta review (5678151570)
found the mirror: ``fleet spawn coder --model gpt-5.4`` (or ``gpt-5.4`` typed into the Spawn
dialog's Model field) gave the agent rung no family and nothing to keep to, so with a Claude
account first in the order and room on it, the spawn ran ``claude --model gpt-5.4`` and every
turn failed. Each agent answers which models are its own, through what it already declares
(``agent_launch.model_owner``: Claude Code's model ladders, each agent's price table), with no
agent's name in shared code. A model no agent claims stays on the agent the order puts first
and never crosses for an account's room. And when the agent a model chose is not installed,
the refusal says that the model chose it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Static

from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.core.agent_adapters import register, unregister
from aisquare.core.agent_adapters.types import PriceTable
from aisquare.core.config import AgentModelDefault, AgentModelSettings, load_config, save_config
from aisquare.core.pricing import Price
from aisquare.models import ClaudeUsage, ProjectInfo, UsageWindow
from aisquare.services import agent_launch
from aisquare.services import fleet as fleet_service
from tests.fake_agent import FakeAgentAdapter
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    project,
    repo,
    tmux,
)
from tests.test_the_spawn_dialogs_account_is_the_one_its_spawn_runs_on import (
    RESETS,
    _home,
    _reading,
    _scripted,
)
from tests.test_ui_spawn import Host, account_field, drive, select, settle, shown


@pytest.fixture
def third() -> Iterator[FakeAgentAdapter]:
    """The fake agent, registered: its price table names ``swift`` and ``deep``."""
    agent = FakeAgentAdapter()
    register(agent)
    try:
        yield agent
    finally:
        unregister(agent.id)


def test_each_agent_claims_its_own_models_and_a_model_two_claim_is_no_ones(
    third: FakeAgentAdapter,
) -> None:
    """What each agent already declares says whose a model is: Claude Code's ladders (a
    model with a family, an alias or an id), Codex's price table (a dated snapshot too), the
    fake agent's own table. A model none names, or two, is no one's."""
    owner = agent_launch.model_owner
    assert [owner(m) for m in ("fable", "opus", "claude-opus-5-5[1m]")] == ["claude-code"] * 3
    assert [owner(m) for m in ("gpt-5.4", "gpt-5.4-2026-03-05", "openai/gpt-6-sol")] == [
        "codex"
    ] * 3
    assert owner("deep") == third.id
    assert [owner(m) for m in ("my-own-model", "", None)] == [None, None, None]

    third.price_table = _naming("gpt-5.4")
    assert owner("gpt-5.4") is None, "two agents name it"


def _naming(model: str) -> PriceTable:
    """A price table that names ``model`` alone."""
    return PriceTable("2026-10-28", "a table of the test's", {model: Price(1.0, 2.0)}, str.lower)


def _claude_first_with_room(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The tester's home with room everywhere: work, a Claude Code slot, first in the order."""
    home = _home()
    _scripted(monkeypatch, {account.slot: _reading() for account in home.values()})
    return home


@pytest.mark.parametrize(
    "named",
    [{"model": "gpt-5.4"}, {"agent_args": ["--model", "gpt-5.4"]}],
    ids=["model", "extra-args"],
)
def test_a_codex_model_the_spawn_names_keeps_it_on_codex_with_a_claude_account_first(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    named: dict[str, Any],
) -> None:
    """The delta review's m1: with work first and room on it, ``--model gpt-5.4`` ran
    ``claude --model gpt-5.4``. It runs Codex, on the first Codex slot, with that model."""
    home = _claude_first_with_room(monkeypatch)

    receipt = fleet_service.spawn(project, "coder", **named)

    assert receipt.agent.agent == "codex", f"ran {receipt.agent.agent}"
    assert receipt.agent.account_slot == home["codex-a"].slot
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list) and "gpt-5.4" in command
    assert command[command.index("--agent") + 1] == "codex"
    # And with nothing named, the same home runs Claude Code on work, as before.
    plain = fleet_service.spawn(project, "coder")
    assert (plain.agent.agent, plain.agent.account_slot) == ("claude-code", home["work"].slot)


def test_a_third_agents_own_model_keeps_the_spawn_on_it(
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    third: FakeAgentAdapter,
) -> None:
    """Nothing in the shared code names an agent: a third agent's own model (the fake's
    ``deep``, from its price table) keeps the spawn on it, where the order would give Claude
    Code."""
    _claude_first_with_room(monkeypatch)

    assert fleet_service.role_agent(project, "coder").adapter.id == "claude-code"
    assert fleet_service.role_agent(project, "coder", model="deep").adapter.id == third.id
    kept = fleet_service.role_agent(project, "coder", ["--model", "deep"])
    assert (kept.adapter.id, kept.source) == (third.id, "model")


def test_a_model_no_agent_claims_stays_on_the_orders_agent_and_never_crosses(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both Claude Code slots near their limit, both Codex slots with room: a spawn that names
    no model crosses to Codex for the room (f163791d), and one that names a model no agent
    claims stays on Claude Code, the order's first agent, for no agent's room."""
    home = _home()
    busy = ClaudeUsage(
        available=True,
        windows=(
            UsageWindow(name="session", minutes=300, used_percent=97.0, resets_at=RESETS),
            UsageWindow(name="week", minutes=10080, used_percent=97.0, resets_at=RESETS),
        ),
    )
    _scripted(
        monkeypatch,
        {
            home["work"].slot: busy,
            home["spare"].slot: busy,
            home["codex-a"].slot: _reading(),
            home["codex-b"].slot: _reading(),
        },
    )

    crossed = fleet_service.spawn(project, "coder")
    kept = fleet_service.spawn(project, "coder", model="my-own-model")

    assert crossed.agent.agent == "codex", "no model named: it may cross"
    assert kept.agent.agent == "claude-code", "a model no agent claims never crosses"


def test_the_dialogs_codex_model_keeps_its_agent_field_and_its_spawn_on_codex(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delta review's dialog case: ``coder = fable`` configured and Fable spent on every
    Claude slot, so the Agent field reads ``(role's: Codex)``. Typing ``gpt-5.4`` into Model
    took the family away, the field turned to ``(role's: Claude Code)`` and the spawn ran
    ``claude --model gpt-5.4``. It stays on Codex, and the field and the label say so."""
    home = _home()
    _scripted(
        monkeypatch,
        {
            home["work"].slot: _reading(("opus", "fable")),
            home["spare"].slot: _reading(("opus", "sonnet", "haiku", "fable")),
            home["codex-a"].slot: _reading(),
            home["codex-b"].slot: _reading(),
        },
    )
    config = load_config()
    config.agents.models["claude-code"] = AgentModelSettings(
        roles={"coder": AgentModelDefault(model="fable")}
    )
    save_config(config)

    def agent_field(dialog: SpawnDialog) -> str:
        return shown(select(dialog, "agent").query_one("SelectCurrent #label", Static))

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> Any:
        dialog.query_one("#spawn-model", Input).value = "gpt-5.4"
        await settle(pilot)
        return agent_field(dialog), account_field(dialog)[0], dialog.spawn_kwargs()

    field, label, sent = drive(project, scenario)
    receipt = fleet_service.spawn(project, "coder", **sent)

    assert sent["model"] == "gpt-5.4"
    assert (receipt.agent.agent, receipt.agent.account_slot) == ("codex", home["codex-a"].slot)
    assert (field, label) == ("(role's: Codex)", "(default: headroom · codex-a)")


def test_a_refusal_for_an_agent_a_model_chose_names_the_model_and_its_agent(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delta review's nit: on a machine with Codex alone, ``--model fable`` is refused,
    as r29 allowed, and said "(chosen by: accounts)", blaming the accounts. It names the
    model and the agent it chose, and the way out."""
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            entry
            for entry in os.environ["PATH"].split(os.pathsep)
            if not any((Path(entry) / name).exists() for name in ("claude", "claude.cmd"))
        ),
    )

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.spawn(project, "coder", model="fable")

    said = str(refused.value)
    assert "'claude' is not on your PATH" in said
    assert "chosen by: the model 'fable', Claude Code's" in said, said
    assert "pass --agent" in said and "accounts" not in said, said


def test_a_named_model_two_agents_claim_keeps_the_rung_from_crossing(
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    third: FakeAgentAdapter,
) -> None:
    """A model two agents name is no one's (``model_owner``): the spawn stays on the order's
    agent, as for a model none names, and no model rung decides it."""
    _claude_first_with_room(monkeypatch)
    third.price_table = _naming("gpt-5.4")

    shared = fleet_service.role_agent(project, "coder", model="gpt-5.4")

    assert (shared.adapter.id, shared.source) == ("claude-code", "accounts")
