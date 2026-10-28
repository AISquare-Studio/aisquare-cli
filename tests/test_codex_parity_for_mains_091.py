"""Codex parity for main's 0.9.1 features (SCOPE #113 091): the rows that say PARITY, each
through the adapter, on scratch accounts of both agents and the fleet's fake tmux.

- A headroom pick, and rank 1, leave a hand-written ``CODEX_HOME`` binding alone as they leave
  a ``CLAUDE_CONFIG_DIR`` one (#9, #406); a hand-over still moves the agent.
- A Codex Stop records the rollout's tokens, and Usage & stats sums them with Claude Code's.
- A Codex agent counts as its role in the local anonymous counts, which name no agent, and in
  no shared day: usage sharing is Claude Code's alone (owner, 2026-10-27).
- The keys a Codex launch leaves on the board (its launch and fleet sightings, its agent events,
  its continuation prompt) go with a project's reset (``project fresh``) and its purge, and the
  trees it removes are the ones the rows' launch records name (item 6's GAP, closed).
- The label cap (24) is the fleet's, not an agent's.

The rows that say GAP are in the SCOPE note, each with the lane that closes it; the others
cite the tests of the shared code they run through (``tests/test_project_fresh.py``,
``tests/test_persona_import.py``, ``tests/test_captain_gate.py``).
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import launch_seal
from aisquare.core.ids import new_event_id
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession
from aisquare.services import agent_events, usage_stats
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.gemini_shaped_agent import GeminiShapedAdapter
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
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


def _a_task_done_by(on: ProjectInfo, session: str | None) -> None:
    """A ``task_done`` on ``on``'s board, marked by ``session`` (``None``: at the shell)."""
    with store_session() as store:
        store.add_team_event(
            TeamEvent(
                id=new_event_id(),
                project_id=on.id,
                session_id=session,
                kind="task_done",
                text="done",
                created_at=datetime.now(tz=UTC),
            )
        )


def test_a_codex_agent_counts_as_its_role_locally_and_in_no_shared_day(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The anonymous counts name a role, never an agent or a label: a Codex coder is one
    ``coder`` in the day's local counts, as a Claude Code one is. Usage sharing is Claude
    Code's alone (owner, 2026-10-27), its fleet counts too (review r28 of #113, m2): the shared
    path (``shared_only``, what ``usage_share`` builds from) holds no Codex spawn and no task a
    Codex session marked done, and a day of Codex's alone is no shared day at all."""
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    with store_session() as store:
        [codex] = store.fleet_agents(project.id)
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET session_id = 'ses-codex-agent' WHERE id = ?", (codex.id,)
        )
        store._conn.commit()  # type: ignore[attr-defined]
        store.upsert_session(
            TeamSession(
                id="ses-codex",
                agent="codex",
                project_id=project.id,
                started_at=datetime.now(tz=UTC),
                last_seen_at=datetime.now(tz=UTC),
            )
        )
    _a_task_done_by(project, "ses-codex")  # by a Codex session no fleet agent is
    _a_task_done_by(project, "ses-codex-agent")  # by the Codex fleet agent's session

    [day] = usage_stats.daily_rollups(date.today(), date.today())
    shared = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert codex.agent == "codex"
    assert (day.fleet.agents_spawned, day.fleet.tasks_done) == ({"coder": 1}, 2)
    assert "codex" not in json.dumps(day.model_dump(mode="json"))
    assert shared == [], "a day of Codex's alone is no shared day"

    fleet_service.spawn(project, "tester", worktree=False)  # Claude Code's
    _a_task_done_by(project, None)  # at the shell: no agent's

    [day] = usage_stats.daily_rollups(date.today(), date.today())
    [shared_day] = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert day.fleet.agents_spawned == {"coder": 1, "tester": 1}
    assert day.fleet.tasks_done == 3
    assert (shared_day.fleet.agents_spawned, shared_day.fleet.tasks_done) == ({"tester": 1}, 1)


def _a_session(on: ProjectInfo, session: str, agent: str) -> None:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(id=session, agent=agent, project_id=on.id, started_at=now, last_seen_at=now)
        )


def _session_of(agent: FleetAgent, session: str) -> None:
    """Fleet agent ``agent`` bound to board session ``session``, as its hook binds it."""
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET session_id = ? WHERE id = ?", (session, agent.id)
        )
        store._conn.commit()  # type: ignore[attr-defined]


def test_a_task_done_is_its_fleet_agent_s_before_its_session_s(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """tester-42 on #42 (b), mutant L: whose a task done is, where the two disagree, is the
    fleet agent's bound to the session that marked it, before the session's own
    (``store.fleet_activity``): a Codex agent's mark is in no shared count though its session
    reads as Claude Code's, and a Claude Code agent's is though its session reads as Codex's."""
    codex = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    _session_of(codex, "ses-codex-agent")
    _a_session(project, "ses-codex-agent", "claude-code")  # the session disagrees
    _a_task_done_by(project, "ses-codex-agent")

    [day] = usage_stats.daily_rollups(date.today(), date.today())
    shared = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert day.fleet.tasks_done == 1
    assert shared == [], "the Codex agent's mark, whatever its session says"

    claude = fleet_service.spawn(project, "tester", worktree=False).agent
    _session_of(claude, "ses-claude-agent")
    _a_session(project, "ses-claude-agent", "codex")  # the session disagrees
    _a_task_done_by(project, "ses-claude-agent")

    [shared_day] = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert claude.agent == "claude-code"
    assert (shared_day.fleet.agents_spawned, shared_day.fleet.tasks_done) == ({"tester": 1}, 1)


def _done_by(on: ProjectInfo, session: str, env: dict[str, str]) -> None:
    """``aisquare task done`` of a task ``session`` claimed, run in ``env``."""
    from aisquare.services import team as team_service

    task, _ = team_service.add_task("a task", cwd=Path(on.root))
    team_service.claim_task(task.id, session_ref=session)
    done = CliRunner().invoke(app, ["task", "done", task.id, "--as", session], env=env)
    assert done.exit_code == 0, done.output


def test_a_codex_launch_s_commands_are_in_no_count_and_a_person_s_are(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tester-42 on #42 (1): every ``aisquare`` command was counted in the feature spool
    whoever ran it, so a Codex agent's ``aisquare task done`` (its board's protocol) made a
    Codex-only day a shared day of ``{cli.task.done: 1}``. A command run in the launch of an
    agent whose usage is not shared (``AISQUARE_LAUNCH_AGENT``, which a launch exports to its
    tools' shells) is not counted; its broker's are ``tests/test_store_broker.py``. A
    person's is, and a Claude Code launch's, as before."""
    from aisquare.core.spawn import LAUNCH_AGENT_ENV

    monkeypatch.chdir(project.root)
    monkeypatch.delenv(LAUNCH_AGENT_ENV, raising=False)
    codex = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    _session_of(codex, "ses-codex")
    _a_session(project, "ses-codex", "codex")
    _done_by(project, "ses-codex", {LAUNCH_AGENT_ENV: "codex"})
    listed = CliRunner().invoke(app, ["task", "list"], env={LAUNCH_AGENT_ENV: "codex"})
    assert listed.exit_code == 0, listed.output

    [day] = usage_stats.daily_rollups(date.today(), date.today())

    assert day.fleet.tasks_done == 1 and day.features == {}, "not even counted locally"
    assert usage_stats.daily_rollups(date.today(), date.today(), shared_only=True) == []

    for env in ({}, {LAUNCH_AGENT_ENV: "claude-code"}, {LAUNCH_AGENT_ENV: ""}):
        result = CliRunner().invoke(app, ["task", "list"], env=env)
        assert result.exit_code == 0, result.output

    [shared_day] = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert shared_day.features == {"cli.task.list": 3}, "a person's, a Claude Code launch's"
    assert (shared_day.fleet.agents_spawned, shared_day.fleet.tasks_done) == ({}, 0)
    unknown = CliRunner().invoke(app, ["task", "list"], env={LAUNCH_AGENT_ENV: "an-agent-x"})
    assert unknown.exit_code == 0, unknown.output
    [shared_day] = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)
    assert shared_day.features == {"cli.task.list": 3}, "an agent unknown here is not shared"


class _MarkedThird(GeminiShapedAdapter):
    """A third agent that marks its tools' shells, as Codex does, and is not in the share."""

    capabilities = dataclasses.replace(
        GeminiShapedAdapter.capabilities, tool_shell_env="GEMINI_SHAPED_TOOL_CALL"
    )


@pytest.fixture(params=["codex", "third"])
def started_by_hand(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, dict[str, str]]]:
    """An agent started by hand (no ``aisquare launch``, so no ``AISQUARE_LAUNCH_AGENT``) and
    the environment of its tools' shells, as its own mark (``tool_shell_env``) says: Codex's,
    or a third agent's registered here, so no agent's name is what is read."""
    from aisquare.core.agent_adapters import get_adapter, register, unregister
    from aisquare.core.spawn import LAUNCH_AGENT_ENV

    monkeypatch.delenv(LAUNCH_AGENT_ENV, raising=False)
    third = _MarkedThird()
    register(third)
    try:
        agent = "codex" if request.param == "codex" else third.id
        for adapter in (get_adapter("codex"), third):
            assert adapter.capabilities.tool_shell_env is not None
            monkeypatch.delenv(adapter.capabilities.tool_shell_env, raising=False)
        mark = get_adapter(agent).capabilities.tool_shell_env
        assert mark is not None
        yield agent, {mark: "019a3c2e-thread"}
    finally:
        unregister(third.id)


def test_a_hand_started_codex_s_commands_are_in_no_count_and_its_task_events_name_it(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    started_by_hand: tuple[str, dict[str, str]],
) -> None:
    """Review r29 of #113, m2: only ``AISQUARE_LAUNCH_AGENT`` was read, which ``aisquare
    launch`` alone exports. A ``codex`` started by hand and hooked by ``agents connect`` joins
    the board, and its tools' shells carry its ``CODEX_THREAD_ID`` and no launch agent: its
    ``aisquare task list`` was shared as ``{cli.task.list: 1}``, and a ``task done`` it ran
    with no ``--as`` was a person's. Each agent's own mark of its tools' shells is read too
    (``tool_shell_env``, through the adapters: a third agent's is). Where a Claude Code
    launch's agent and that mark are both in a shell, the one the share leaves out is the
    answer. A person's at a plain shell and a Claude Code launch's are counted, as before."""
    from aisquare.core.spawn import LAUNCH_AGENT_ENV

    agent, its_shell = started_by_hand
    monkeypatch.chdir(project.root)
    for env in (its_shell, {LAUNCH_AGENT_ENV: "claude-code", **its_shell}):
        listed = CliRunner().invoke(app, ["task", "list"], env=env)
        assert listed.exit_code == 0, listed.output
        _done_in(project, env)

    [day] = usage_stats.daily_rollups(date.today(), date.today())

    assert (day.features, day.fleet.tasks_done) == ({}, 2), "not counted, its tasks done local"
    assert usage_stats.daily_rollups(date.today(), date.today(), shared_only=True) == []
    with store_session() as store:
        marked = store.filtered_events(project.id, kind="task_done")
    assert [(done.session_id, done.meta) for done in marked] == [
        (None, {"launch_agent": agent})
    ] * 2, "its agent's"

    for env in ({}, {LAUNCH_AGENT_ENV: "claude-code"}):
        result = CliRunner().invoke(app, ["task", "list"], env=env)
        assert result.exit_code == 0, result.output
    _done_in(project, {})

    [shared_day] = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert shared_day.features == {"cli.task.list": 2, "cli.task.done": 1}
    assert shared_day.fleet.tasks_done == 1, "a person's"


def _done_in(
    on: ProjectInfo, env: dict[str, str], *, claimed_by: str | None = None, as_: str | None = None
) -> None:
    """``aisquare task done`` of a new task, run in ``env``, with no ``--as`` (else
    ``--as as_``); ``claimed_by`` claimed it there first (``--as``) when given."""
    from aisquare.services import team as team_service

    task, _ = team_service.add_task("a task", cwd=Path(on.root))
    if claimed_by is not None:
        claim = ["task", "claim", task.id, "--as", claimed_by]
        claimed = CliRunner().invoke(app, claim, env=env)
        assert claimed.exit_code == 0, claimed.output
    named = ["--as", as_] if as_ is not None else []
    done = CliRunner().invoke(app, ["task", "done", task.id, *named], env=env)
    assert done.exit_code == 0, done.output


def test_a_task_a_codex_launch_marks_done_without_as_is_codex_s(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tester-42b on #42 (1): a Codex agent's ``task done`` that names no session wrote an
    event naming nobody, which the fleet counts read as a person's at the shell, so a day of
    Codex's alone shipped ``{tasks_done: 1}``. A task event written in a launch with no
    session names the launch's agent (``AISQUARE_LAUNCH_AGENT``), in a fleet window and in a
    launch by hand; its broker's is ``tests/test_store_broker.py``. A Claude Code launch's
    and a person's are counted, as before, and a person's ``--as`` a Codex session is not."""
    from aisquare.core.spawn import LAUNCH_AGENT_ENV

    monkeypatch.chdir(project.root)
    monkeypatch.delenv(LAUNCH_AGENT_ENV, raising=False)
    codex = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    _session_of(codex, "ses-codex")
    _a_session(project, "ses-codex", "codex")
    in_its_window = {LAUNCH_AGENT_ENV: "codex", "AISQUARE_FLEET_AGENT": codex.id}
    _done_in(project, in_its_window, claimed_by="ses-codex")  # tester-42b's repro
    _done_in(project, {LAUNCH_AGENT_ENV: "codex"})  # `aisquare launch codex`, no fleet

    [day] = usage_stats.daily_rollups(date.today(), date.today())

    assert (day.fleet.agents_spawned, day.fleet.tasks_done) == ({"coder": 1}, 2), "local"
    assert usage_stats.daily_rollups(date.today(), date.today(), shared_only=True) == []

    _done_in(project, {LAUNCH_AGENT_ENV: "claude-code"})
    _done_in(project, {})
    _done_in(project, {}, as_="ses-codex")  # a person's, as the Codex session

    [shared_day] = usage_stats.daily_rollups(date.today(), date.today(), shared_only=True)

    assert (shared_day.fleet.agents_spawned, shared_day.fleet.tasks_done) == ({}, 2)
    assert shared_day.features["cli.task.done"] == 3, "a Claude Code launch's, a person's two"


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
    lists none for it: its store columns are all it has, and reap removes nothing there.
    The tree they name is kept, and the report says so and why (seq 4207), as it says of
    every tree it keeps."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    fleet_service.stop(project, agent.label, force=True)
    launch_seal._path(agent.id).unlink()

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and agent.cwd.is_dir()
    assert [(tree, why.split(":")[0]) for tree, why in report.worktrees_kept] == [
        (agent.cwd, f"no launch record places {agent.label}")
    ]
    assert "no launch record was written for it" in report.worktrees_kept[0][1], "and why"


def test_reap_says_a_tree_a_placed_rows_record_names_for_that_row_alone(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """A label's older row no record places and its respawn, which reused the tree, both
    ended: the tree is the respawn's, by its record, and kept for what that row left there.
    It is on the report once, for that, not again as the unplaced row's."""
    older = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    fleet_service.stop(project, older.label, force=True)
    launch_seal._path(older.id).unlink()
    newer = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    assert newer.cwd.resolve() == older.cwd.resolve(), "the fixture: the respawn reused the tree"
    (newer.cwd / "notes.txt").write_text("work in progress\n", encoding="utf-8")
    fleet_service.stop(project, newer.label, force=True)

    report = fleet_service.reap(project)

    assert report.worktrees_kept == [(newer.cwd, "uncommitted changes")]


def test_reap_says_nothing_of_an_unplaced_rows_tree_in_use(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """An ended row no record places whose store ``cwd`` names the project's root: the root
    is in use, never a tree reap keeps for a row, and the report does not say it is."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    fleet_service.stop(project, agent.label, force=True)
    launch_seal._path(agent.id).unlink()
    _rewrite_cwd(agent.id, project.root)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and report.worktrees_kept == []
    assert agent.cwd.is_dir() and project.root.is_dir()


def test_reap_says_nothing_of_an_unplaced_rows_tree_gone_already(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """An ended row no record places whose tree was removed by hand: nothing is kept there,
    and the report does not say it is (tester-40c, seq 4239, c1)."""
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=True).agent
    fleet_service.stop(project, agent.label, force=True)
    launch_seal._path(agent.id).unlink()
    git = ["git", "-C", str(project.root)]
    subprocess.run([*git, "worktree", "remove", "--force", str(agent.cwd)], check=True)
    assert not agent.cwd.exists(), "the fixture: the tree is gone"

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and report.worktrees_kept == []


def test_reap_says_nothing_of_a_live_unplaced_rows_tree(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """A LIVE row no record places: its tree is in use, not one reap keeps for an ended row,
    and the report does not say it is (c2). An ended placed row has reap look at the trees,
    and its own tree goes."""
    done = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="done").agent
    fleet_service.stop(project, done.label, force=True)
    live = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    launch_seal._path(live.id).unlink()

    report = fleet_service.reap(project)

    assert report.worktrees_kept == [] and live.cwd.is_dir()
    assert [tree.resolve() for tree in report.worktrees_removed] == [done.cwd.resolve()]


def test_reap_says_nothing_for_an_unplaced_row_that_had_no_tree(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    tmp_path: Path,
) -> None:
    """An ended row no record places that ran with no tree of its own, its store ``cwd``
    rewritten to a folder elsewhere: that folder was never the fleet's tree, and the report
    does not say it is kept (c3)."""
    done = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="done").agent
    fleet_service.stop(project, done.label, force=True)
    plain = fleet_service.spawn(project, "coder", agent="codex", worktree=False, label="pl").agent
    fleet_service.stop(project, plain.label, force=True)
    launch_seal._path(plain.id).unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _rewrite_cwd(plain.id, elsewhere)

    report = fleet_service.reap(project)

    assert report.worktrees_kept == [] and elsewhere.is_dir()


def test_reap_says_an_unplaced_tree_once_for_the_newest_row_there(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """Two ended rows no record places at one tree (the newer's store ``cwd`` rewritten to the
    older's tree): the tree is on the report once, for the newest row there, as ``owners``
    holds a placed row's tree (c4)."""
    a = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx-a").agent
    fleet_service.stop(project, a.label, force=True)
    launch_seal._path(a.id).unlink()
    b = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx-b").agent
    fleet_service.stop(project, b.label, force=True)
    launch_seal._path(b.id).unlink()
    _rewrite_cwd(b.id, a.cwd)

    report = fleet_service.reap(project)

    assert [(tree, why.split(":")[0]) for tree, why in report.worktrees_kept] == [
        (a.cwd, "no launch record places cx-b")
    ]


def test_reap_says_nothing_of_an_unplaced_rows_tree_a_live_respawn_works_in(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """An ended row no record places, and its label's respawn, live, which reused the tree and
    whose record is gone too: the tree is the live agent's, in use, and the report does not
    say it is kept for the ended row (c5). Read by the records alone, it did."""
    ended = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    fleet_service.stop(project, ended.label, force=True)
    launch_seal._path(ended.id).unlink()
    live = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    assert live.cwd.resolve() == ended.cwd.resolve(), "the fixture: the respawn reused the tree"
    launch_seal._path(live.id).unlink()

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and report.worktrees_kept == []
    assert live.cwd.is_dir()


def test_reap_says_nothing_of_an_unplaced_rows_tree_a_live_rows_store_names(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """An ended row no record places, at its tree, and a live placed row with no tree whose
    store ``cwd`` was rewritten onto it: ``_reap_tree`` leaves a tree a live row's store names
    unsaid, as in use, and the report says nothing of it either (c6)."""
    ended = fleet_service.spawn(project, "coder", agent="codex", worktree=True, label="cx").agent
    fleet_service.stop(project, ended.label, force=True)
    launch_seal._path(ended.id).unlink()
    live = fleet_service.spawn(project, "coder", agent="codex", worktree=False, label="lv").agent
    _rewrite_cwd(live.id, ended.cwd)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and report.worktrees_kept == []
    assert ended.cwd.is_dir()


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
