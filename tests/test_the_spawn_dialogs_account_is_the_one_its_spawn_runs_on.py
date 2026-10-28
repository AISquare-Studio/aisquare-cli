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
from tests.test_ui_spawn import Host, account_field, drive

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
        return account_field(dialog)[0], dialog.spawn_kwargs()

    label, sent = drive(project, scenario)
    receipt = fleet_service.spawn(project, "coder", **sent)

    assert sent["agent"] is None and sent["account"] is None, "the dialog's default path"
    ran = home[runs_on]
    assert receipt.agent.account_slot == ran.slot, f"ran on slot {receipt.agent.account_slot}"
    assert receipt.agent.agent == ran.agent
    assert label == f"(default: headroom · {runs_on})", "the label names where it ran"
