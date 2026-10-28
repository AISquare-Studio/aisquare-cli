"""Codex parity for main's 0.9.1 features (SCOPE #113 091): the rows that say PARITY, each
through the adapter, on scratch accounts of both agents and the fleet's fake tmux.

- A headroom pick, and rank 1, leave a hand-written ``CODEX_HOME`` binding alone as they leave
  a ``CLAUDE_CONFIG_DIR`` one (#9, #406); a hand-over still moves the agent.
- A Codex Stop records the rollout's tokens, and Usage & stats sums them with Claude Code's.
- A Codex agent counts as its role in the anonymous counts, which name no agent.
- The keys a Codex launch leaves on the board (its launch and fleet sightings, its agent events,
  its continuation prompt) go with a project's reset (``project fresh``) and its purge, and the
  trees it removes are the ones the rows' launch records name (item 6's GAP, closed).
- The label cap (24) is the fleet's, not an agent's.

The rows that say GAP are in the SCOPE note, each with the lane that closes it; the others
cite the tests of the shared code they run through (``tests/test_project_fresh.py``,
``tests/test_persona_import.py``, ``tests/test_captain_gate.py``).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import launch_seal
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import ProjectInfo, TeamSession
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
    """A Codex session aisquare launched records its rollout's tokens at its Stop
    (``AgentTranscript.usage``) and Usage & stats sums every recorded turn; the sweep over the
    rollouts no hook watched is ``tests/test_codex_usage_sweep.py``."""
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


# --- project fresh and a purge clear what a launch of any agent leaves ----------------------


def _a_launch_left_keys(of: ProjectInfo, session: str) -> list[str]:
    """A session of ``of`` and the ``team_meta`` keys a Codex launch and its hooks keep for
    it; the keys, so a test can read them back."""
    now = datetime.now(tz=UTC)
    keys = [
        f"continuation-prompt:{session}",
        f"agent-event:{session}:turn-1",
        f"launch-seen:codex:{session}",
        f"fleet-seen:asq:{session}",
        f"nudge:{session}",
    ]
    with store_session() as store:
        store.upsert_session(
            TeamSession(id=session, project_id=of.id, started_at=now, last_seen_at=now)
        )
        for key in keys:
            store.set_meta(key, "1")
    return keys


@pytest.mark.parametrize("how", ["reset", "purge"])
def test_the_keys_a_launch_leaves_go_with_a_projects_reset_and_purge(
    project: ProjectInfo,  # noqa: F811
    how: str,
) -> None:
    """``store._session_meta_terms`` is the one list both walk: the per-session keys main
    kept (``nudge``…) and the ones a launch through the adapter leaves."""
    keys = _a_launch_left_keys(project, "ses-codex-1")
    with store_session() as store:
        assert all(store.get_meta(key) == "1" for key in keys), "the fixture"
        if how == "reset":
            store.reset_project(project.id, apply=True)
        else:
            store.purge_project(project.id)
        assert [key for key in keys if store.get_meta(key) is not None] == []


def test_a_codex_rows_tree_is_the_one_its_launch_record_names_not_its_rewritten_cwd(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """SCOPE #113 091, item 6: ``project fresh`` removes the trees ``fleet.fleet_trees`` names,
    ``--force`` when told. A Codex row whose ``cwd`` was rewritten in the store (a sandbox can
    write the store before its broker) to another worktree of the repository, the owner's own,
    is not followed: the tree is the one its launch record names (``resolve_place``), as stop
    and reap read it."""
    receipt = fleet_service.spawn(project, "coder", agent="codex", worktree=True)
    agent = receipt.agent
    assert agent.agent == "codex" and agent.worktree, "the fixture: a Codex row with a tree"
    owners = tmp_path / "owners-tree"
    git = ["git", "-C", str(project.root)]
    subprocess.run([*git, "worktree", "add", "-q", "-b", "owners", str(owners)], check=True)

    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE fleet_agent SET cwd = ? WHERE id = ?", (str(owners), agent.id))
        rows = store.fleet_agents(project.id, live_only=False)
    assert [row.cwd for row in rows] == [owners], "the store names the owner's tree now"

    trees = fleet_service.fleet_trees(project, rows)

    assert [tree.resolve() for tree in trees] == [agent.cwd.resolve()]
    assert owners.resolve() not in {tree.resolve() for tree in trees}


def _rewrite_cwd(agent_id: str, cwd: Path) -> None:
    """``cwd`` in the store for the row ``agent_id``, as a sandbox could write it."""
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE fleet_agent SET cwd = ? WHERE id = ?", (str(cwd), agent_id))
        store._conn.commit()
        row = store.get_fleet_agent(agent_id)
    assert row is not None and row.cwd == cwd, "the store names the rewritten tree now"


def test_reap_removes_the_tree_a_codex_rows_launch_record_names_not_its_rewritten_cwd(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Item 6's sibling (seq 4187 a): reap, as ``fleet_trees``, takes an ended row's tree from
    its launch record. A Codex row whose ``cwd`` was rewritten in the store to the owner's
    own worktree, clean and on the default branch as a reaped tree is, is not reaped there:
    the owner's tree stays, and the row's own, which its record names, goes."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    owners = tmp_path / "owners-tree"
    git = ["git", "-C", str(project.root)]
    subprocess.run([*git, "worktree", "add", "-q", "-b", "owners", str(owners)], check=True)
    fleet_service.stop(project, agent.label, force=True)
    _rewrite_cwd(agent.id, owners)

    report = fleet_service.reap(project)

    assert owners.is_dir(), "the owner's tree is no row's to reap"
    assert [tree.resolve() for tree in report.worktrees_removed] == [agent.cwd.resolve()]
    assert not agent.cwd.exists()


def test_reap_removes_no_tree_for_a_row_no_launch_record_places(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """A row whose launch record is gone (``Unplaced``) names no tree, as ``fleet_trees``
    lists none for it: its store columns are all it has, and reap removes nothing there."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    fleet_service.stop(project, agent.label, force=True)
    launch_seal._path(agent.id).unlink()

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and agent.cwd.is_dir()


def test_reap_keeps_a_tree_a_live_rows_store_names_whatever_the_records_say(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Keeping a tree is the safe side: one a live row's store columns name stays, though no
    launch record puts that row there (``_reap_tree`` asks the store's rows under the branch
    lock), so reading the records never takes a tree from under a live agent."""
    ended = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    fleet_service.stop(project, ended.label, force=True)
    live = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    _rewrite_cwd(live.id, ended.cwd)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and ended.cwd.is_dir()


def test_reap_keeps_a_tree_a_live_rows_launch_record_names_though_its_store_cwd_moved(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The other half of what a tree is in use by (seq 4207, MI): the tree a LIVE row's
    launch record names. A respawn of the label reuses the ended row's tree, and the live
    row's store ``cwd`` is then rewritten away: the tree is still the live agent's, never
    reaped for the ended row its record names too."""
    ended = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    fleet_service.stop(project, ended.label, force=True)
    live = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    assert live.cwd.resolve() == ended.cwd.resolve(), "the fixture: the respawn reused the tree"
    _rewrite_cwd(live.id, tmp_path)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and live.cwd.is_dir(), "a live agent's tree stays"


def test_reap_removes_an_ended_rows_recorded_tree_though_its_store_cwd_names_the_root(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """And the tree an ENDED row's launch record names is the one reap asks about whatever
    its store ``cwd`` says (seq 4207, MI): rewritten to the project's root, which is always in
    use, the row's own clean, ended tree still goes."""
    ended = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    fleet_service.stop(project, ended.label, force=True)
    _rewrite_cwd(ended.id, project.root)

    report = fleet_service.reap(project)

    assert [tree.resolve() for tree in report.worktrees_removed] == [ended.cwd.resolve()]
    assert project.root.is_dir() and not ended.cwd.exists()


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
