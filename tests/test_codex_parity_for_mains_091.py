"""Codex parity for main's 0.9.1 features (SCOPE #113 091): the rows that say PARITY, each
through the adapter, on scratch accounts of both agents and the fleet's fake tmux.

- A headroom pick, and rank 1, leave a hand-written ``CODEX_HOME`` binding alone as they leave
  a ``CLAUDE_CONFIG_DIR`` one (#9, #406); a hand-over still moves the agent.
- A Codex Stop records the rollout's tokens, and Usage & stats sums them with Claude Code's.
- A Codex agent counts as its role in the anonymous counts, which name no agent.
- The label cap (24) is the fleet's, not an agent's.

The rows that say GAP are in the SCOPE note, each with the lane that closes it; the others
cite the tests of the shared code they run through (``tests/test_project_fresh.py``,
``tests/test_persona_import.py``, ``tests/test_captain_gate.py``).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import agent_events, usage_stats
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    project,
    repo,
    tmux,
)
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    CODEX,
    _claude,
    _codex,
    _pick,
    _usage,
    machine,
)

# --- the headroom pick vs a hand-written CODEX_HOME ---------------------------------------


@pytest.mark.parametrize("pick", ["default", "headroom"])
def test_a_pick_never_overrides_a_hand_written_codex_home_binding(
    machine: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    pick: str,
) -> None:
    """``team bind coder --env CODEX_HOME=/hand/written``: the directory the role runs in is
    the operator's, so neither rank 1 nor the headroom rung applies an account over it
    (``claude_accounts._binding_names_a_directory`` asks each agent's own home variable)."""
    _claude("c@example.com")
    first = _codex("one@example.com")
    second = _codex("two@example.com")
    _usage(monkeypatch, {first.slot: 90, second.slot: 10})
    _pick(pick=pick)
    home_variable = accounts_core.kind(CODEX).home_env
    assert home_variable == "CODEX_HOME"
    settings_service.bind_role("coder", env={home_variable: "/hand/written"})

    choice = accounts_service.choose(role="coder", agent=CODEX)
    assert choice.account is None and choice.source is None  # nothing chose: left as written

    # The control: a role with no such binding is picked for, by the rung the config asks.
    control = accounts_service.choose(role="tester", agent=CODEX)
    assert control.account is not None
    assert control.source == ("headroom" if pick == "headroom" else "preferred")
    # A hand-over still moves the agent, and a named account still wins over the directory.
    moved = accounts_service.choose(role="coder", agent=CODEX, spread=True)
    assert moved.account is not None and moved.account.agent == CODEX
    named = accounts_service.choose(str(second.slot), role="coder", agent=CODEX)
    assert (named.account is not None and named.account.slot, named.source) == (
        second.slot,
        "flag",
    )


# --- usage stats: a Codex Stop is a turn ---------------------------------------------------


@pytest.mark.usefixtures("project_capture")
def test_a_codex_stop_is_in_the_usage_stats(tmp_path: Path) -> None:
    """The sweep reads Claude Code's transcripts alone (the lane for Codex's is in the SCOPE
    note), but a Codex session aisquare launched records its rollout's tokens at its Stop
    (``AgentTranscript.usage``) and Usage & stats sums every recorded turn."""
    from aisquare.core.agent_adapters import get_adapter

    codex = get_adapter("codex")
    rollout = tmp_path / "rollout-2026-10-24T20-40-11-native.jsonl"
    fixture = Path(__file__).parent / "fixtures" / "agent_transcripts"
    rollout.write_bytes((fixture / "codex-usage-0.164.0.jsonl").read_bytes())
    reader = codex.transcript_reader
    assert reader is not None
    expected = reader.usage(rollout, None)
    assert expected is not None and expected.delta.totals().total > 0
    home = tmp_path / ".codex"
    runner = CliRunner()
    for event in ("Stop", "SessionEnd"):
        payload = {
            "session_id": "native",
            "cwd": str(tmp_path),
            "transcript_path": str(rollout),
            "hook_event_name": event,
            "turn_id": "one",
        }
        result = runner.invoke(
            app, ["hook", "codex", "--config-dir", str(home)], input=json.dumps(payload)
        )
        assert result.exit_code == 0, result.output

    board_id = agent_events.session_key(codex, home, "native")
    with store_session() as store:
        assert store.session_usage(board_id) is not None
    stats = usage_stats.stats("lifetime")
    assert stats.sessions == 1 and stats.turns == 1
    assert stats.tokens.total == expected.delta.totals().total


# --- anonymous counts name no agent --------------------------------------------------------


def test_a_codex_agent_counts_as_its_role_in_the_shared_counts(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """``usage_share`` sends agents started by role, never by agent or label: a Codex coder is
    one ``coder`` in the day's counts, as a Claude Code one is."""
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)

    [day] = usage_stats.daily_rollups(date.today(), date.today())

    assert day.fleet.agents_spawned == {"coder": 1}
    assert "codex" not in json.dumps(day.model_dump(mode="json"))


# --- the label cap --------------------------------------------------------------------------


def test_a_codex_spawns_label_is_capped_at_24(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The cap is tmux's target separators' (a label names a window), so it is the fleet's for
    every agent: 24 characters spawn a Codex agent, 25 are refused before any window."""
    windows = len(tmux.spawned)
    with pytest.raises(fleet_service.FleetError, match="at most 24"):
        fleet_service.spawn(project, "coder", agent="codex", label="x" * 25, worktree=False)
    assert len(tmux.spawned) == windows, "no window was opened for the refused label"

    receipt = fleet_service.spawn(project, "coder", agent="codex", label="y" * 24, worktree=False)
    assert receipt.agent.label == "y" * 24
