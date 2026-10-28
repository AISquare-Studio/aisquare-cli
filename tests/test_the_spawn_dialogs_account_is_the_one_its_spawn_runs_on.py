"""The Spawn dialog's default path: the account its label names is the one the spawn runs on.

Review r28 of #113, M1, reopened (tester-43, seq 4273): with the Agent field left at
``(role's: Claude Code)`` the dialog sends no ``--agent``, so the spawn's agent is the agent
rung's (``agent_launch.resolve``, ``claude_accounts.agent_pick``), which picked an account
across both agents with no model family; ``choose`` took it. The label read ``choose`` with
no pick, by the family the spawn runs. On a home where work (rank 1) is out of its Opus and
Fable weeks and spare out of its Sonnet and Haiku weeks, a Fable or an Opus spawn ran on
work under a label naming spare. The agent rung weighs the family now, and the label reads
the spawn's own steps (``fleet.launch_default``). The dialog is driven as the owner leaves
it, and the spawn is sent what the dialog sends.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Static

from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.config import (
    AccountsSettings,
    AgentModelDefault,
    AgentModelSettings,
    FleetRoleSettings,
    load_config,
    save_config,
)
from aisquare.models import ClaudeAccount, ClaudeUsage, ProjectInfo, UsageWindow
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import model_memory
from tests.test_claude_accounts import _sign_in as _claude_sign_in
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in as _codex_sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    project,
    repo,
    tmux,
)
from tests.test_ui_spawn import Host, account_field, drive, select, settle, shown

RESETS = datetime.now(tz=UTC) + timedelta(days=3)


def _reading(spent: Iterable[str] = ()) -> ClaudeUsage:
    """Room in every window, and each family of ``spent`` out of its week."""
    families = list(spent)
    return ClaudeUsage(
        available=True,
        windows=(
            UsageWindow(name="session", minutes=300, used_percent=10.0),
            UsageWindow(name="week", minutes=10080, used_percent=10.0, resets_at=RESETS),
        ),
        family_week_percent=dict.fromkeys(families, 100.0),
        family_week_resets_at=dict.fromkeys(families, RESETS),
    )


def _scripted(monkeypatch: pytest.MonkeyPatch, readings: Mapping[int, ClaudeUsage]) -> None:
    """Every account's usage, as the cache (and a live read) answers it: nothing is read."""

    def cached(accounts: Iterable[ClaudeAccount], **_: Any) -> dict[int, ClaudeUsage]:
        return {account.slot: readings[account.slot] for account in accounts}

    monkeypatch.setattr(accounts_service, "cached_usage", cached)
    monkeypatch.setattr(accounts_service, "read_usage", lambda accounts, **_: cached(accounts))


def _home() -> dict[str, ClaudeAccount]:
    """The tester's home: two Claude Code slots, work (first) and spare, then two of Codex's,
    in one order, picked by headroom."""
    made: dict[str, ClaudeAccount] = {}
    for name in ("work", "spare"):
        account = accounts_core.create_account()
        _claude_sign_in(account, f"{name}@example.com")
        accounts_service.set_alias(str(account.slot), name)
        made[name] = account
    for name in ("codex-a", "codex-b"):
        account = accounts_core.create_account("codex")
        _codex_sign_in(account.config_dir, f"{name}@example.com")
        accounts_service.set_alias(str(account.slot), name)
        made[name] = account
    config = load_config()
    config.accounts = AccountsSettings(pick="headroom")
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(config)
    return made


@pytest.mark.parametrize(
    ("case", "runs_on"),
    [
        ("configured", "spare"),  # [agents.models.claude-code] coder = fable
        ("last-pick", "spare"),  # the owner's last pick, opus
        ("neither", "work"),  # no model: the account's default, no family
        ("every-claude-slot-spent", "codex-a"),  # fable spent on both: Codex has room
    ],
)
def test_the_label_of_the_dialogs_default_path_is_the_account_the_spawn_runs_on(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    runs_on: str,
) -> None:
    home = _home()
    spare_spent = ("sonnet", "haiku", "fable") if case == "every-claude-slot-spent" else ()
    _scripted(
        monkeypatch,
        {
            home["work"].slot: _reading(("opus", "fable")),
            home["spare"].slot: _reading(spare_spent or ("sonnet", "haiku")),
            home["codex-a"].slot: _reading(),
            home["codex-b"].slot: _reading(),
        },
    )
    if case in ("configured", "every-claude-slot-spent"):
        config = load_config()
        config.agents.models["claude-code"] = AgentModelSettings(
            roles={"coder": AgentModelDefault(model="fable", effort="high")}
        )
        save_config(config)
    elif case == "last-pick":
        model_memory.remember(model="opus", effort="high")

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> Any:
        return account_field(dialog)[0], _agent_field(dialog), dialog.spawn_kwargs()

    label, agent_field, sent = drive(project, scenario)
    receipt = fleet_service.spawn(project, "coder", **sent)

    assert sent["agent"] is None and sent["account"] is None, "the dialog's default path"
    ran = home[runs_on]
    assert receipt.agent.account_slot == ran.slot, f"ran on slot {receipt.agent.account_slot}"
    assert receipt.agent.agent == ran.agent
    assert label == f"(default: headroom · {runs_on})", "the label names where it ran"
    assert agent_field == f"(role's: {_LABELS[ran.agent]})", "the Agent field names what ran"


_LABELS = {"claude-code": "Claude Code", "codex": "Codex"}


def _agent_field(dialog: SpawnDialog) -> str:
    """What the Agent field shows closed: ``(role's: …)`` on the dialog's default path."""
    return shown(select(dialog, "agent").query_one("SelectCurrent #label", Static))


def _every_claude_week_of_fable_spent(monkeypatch: pytest.MonkeyPatch) -> dict[str, ClaudeAccount]:
    """The tester's home, work out of its Opus and Fable weeks and spare out of every
    family's, both Codex slots with room."""
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
    return home


@pytest.mark.parametrize(
    "named",
    [{"model": "fable"}, {"model": "opus"}, {"agent_args": ["--model", "fable"]}],
    ids=["model", "another-family", "extra-args"],
)
def test_a_claude_model_the_spawn_names_keeps_it_on_claude_code_with_every_week_spent(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    named: dict[str, Any],
) -> None:
    """Review r29 of #113, m1: the agent rung weighed the family of a model the spawn named
    across agents, and with every Claude account out of that family's week it picked a Codex
    account with room, which took ``--model fable`` and failed every turn (before f163791d it
    ran Claude Code on slot 2). A model the spawn names is its agent's: the rung keeps to
    Claude Code, whose ``choose`` finds no account of it with room and runs on the first in
    the order, as main's did."""
    home = _every_claude_week_of_fable_spent(monkeypatch)

    receipt = fleet_service.spawn(project, "coder", **named)

    assert receipt.agent.agent == "claude-code", f"ran {receipt.agent.agent}"
    assert receipt.agent.account_slot == home["work"].slot, "no Claude slot has room: the first"


@pytest.mark.parametrize(("field", "typed"), [("model", "fable"), ("args", "--model fable")])
def test_the_dialogs_agent_field_says_the_agent_its_spawn_runs_crossing_or_kept_by_a_model(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    typed: str,
) -> None:
    """Review r29 of #113, m1, the dialog: its Agent field asked the role's agent with no
    family, so with ``[agents.models.claude-code] coder = fable`` spent on every Claude slot
    it read ``(role's: Claude Code)`` while its label named a Codex slot and its spawn ran
    Codex (seq 4283). It asks the spawn's own steps now (``fleet.role_agent``). And ``fable``
    typed into the Model field, or ``--model fable`` into *Extra agent args*, is sent, so the
    spawn stays on Claude Code, and the field and the label say so. The Model field read
    against Claude Code's pick, which is that same ``fable``, flipped back to unsent once the
    form followed Claude Code, and the spawn crossed."""
    home = _every_claude_week_of_fable_spent(monkeypatch)
    config = load_config()
    config.agents.models["claude-code"] = AgentModelSettings(
        roles={"coder": AgentModelDefault(model="fable")}
    )
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> Any:
        crossing = (_agent_field(dialog), account_field(dialog)[0], dialog.spawn_kwargs())
        dialog.query_one(f"#spawn-{field}", Input).value = typed
        await settle(pilot)
        kept = (_agent_field(dialog), account_field(dialog)[0], dialog.spawn_kwargs())
        return crossing, kept

    crossing, kept = drive(project, scenario)

    agent_field, label, sent = crossing
    receipt = fleet_service.spawn(project, "coder", **sent)
    assert (receipt.agent.agent, receipt.agent.account_slot) == ("codex", home["codex-a"].slot)
    assert (agent_field, label) == ("(role's: Codex)", "(default: headroom · codex-a)")

    agent_field, label, sent = kept
    receipt = fleet_service.spawn(project, "coder", **sent)
    assert (sent["model"], sent["agent_args"]) == (
        ("fable", []) if field == "model" else (None, ["--model", "fable"])
    ), "typed, so sent"
    assert (receipt.agent.agent, receipt.agent.account_slot) == ("claude-code", home["work"].slot)
    assert (agent_field, label) == ("(role's: Claude Code)", "(default: rank 2 · work)")


@pytest.mark.parametrize(
    ("how", "runs_on", "label"),
    [
        ("explicit-claude-code", "work", "(default: rank 2 · work)"),
        ("binary-claude", "work", "(default: rank 2 · work)"),
        ("explicit-codex", "codex-a", "(default: headroom · codex-a)"),
    ],
)
def test_the_label_follows_what_the_dialog_sends_not_only_the_agent_it_follows(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    how: str,
    runs_on: str,
    label: str,
) -> None:
    """tester-43b on #43, (1): with ``coder = fable`` configured and Fable spent on every
    Claude slot, the default path labels codex-a. Picking *Claude Code* in the Agent field, or
    typing ``claude`` in Binary, left the label on codex-a while the spawn ran on work: the
    read was keyed on the agent the form follows, and ``(role's: Claude Code)`` and *Claude
    Code* are both claude-code, sent as no agent and as claude-code. It is keyed on what the
    dialog sends, the agent and the Binary among it. An explicit Codex is read as Codex's."""
    home = _every_claude_week_of_fable_spent(monkeypatch)
    config = load_config()
    config.agents.models["claude-code"] = AgentModelSettings(
        roles={"coder": AgentModelDefault(model="fable", effort="high")}
    )
    save_config(config)

    async def scenario(pilot: Pilot[None], host: Host, dialog: SpawnDialog) -> Any:
        before = account_field(dialog)[0]
        if how == "binary-claude":
            dialog.query_one("#spawn-binary", Input).value = "claude"
        else:
            select(dialog, "agent").value = how.removeprefix("explicit-")
        await settle(pilot)
        return before, account_field(dialog)[0], dialog.spawn_kwargs()

    before, shown_label, sent = drive(project, scenario)
    receipt = fleet_service.spawn(project, "coder", **sent)

    assert before == "(default: headroom · codex-a)", "the default path crosses"
    assert receipt.agent.account_slot == home[runs_on].slot, f"ran on {receipt.agent.account_slot}"
    assert shown_label == label, "the label names where it ran"


def test_a_cleared_model_field_weighs_no_family_where_the_last_pick_would(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tester-43b's MA: the agent rung's family read without the spawn's own Model field.
    With the last pick Fable and Fable spent on every Claude slot, a spawn given no model
    weighs Fable and crosses to codex-a; one whose Model field is cleared runs the account's
    default, no family, and stays on work, where its label says it runs."""
    home = _every_claude_week_of_fable_spent(monkeypatch)
    model_memory.remember(model="fable", effort="high")

    crossed = fleet_service.launch_default(project, "coder").choice.account
    cleared = fleet_service.launch_default(project, "coder", model="").choice.account
    receipt = fleet_service.spawn(project, "coder", model="")

    assert crossed is not None and crossed.slot == home["codex-a"].slot
    assert cleared is not None and cleared.slot == home["work"].slot
    assert (receipt.agent.agent, receipt.agent.account_slot) == ("claude-code", home["work"].slot)
