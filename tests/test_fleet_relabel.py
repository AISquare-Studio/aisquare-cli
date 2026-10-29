"""Rename a running fleet agent: free text becomes its label, everywhere (``fleet relabel``).

The owner's words: the manager reports "coder-1 is blocked and needs your review", and a
spawn-time label means nothing to them; a name they chose ("login fix") does. Whatever they
type becomes a valid label (``fleet_service.label_from_text``), and the agent then IS that
label: its row, every lookup by label, its tmux window, the board's lines from then on. It
keeps working where it works: its worktree and branch are not moved, and every later step
that finds its tree (restart, stop, reap, a fresh spawn's pick) finds it by the row.

Driven as ``tests/test_fleet_service.py`` drives the fleet: the real store in an isolated
home, real git under ``tmp_path``, and that suite's in-memory ``FakeTmux``. The CLI's own
contract (one service call, the line it prints, ``--json``) is held as
``tests/test_fleet_cli.py`` holds it, with the service faked on the CLI's module.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import FLEET_RENAMED, SqliteStore, store_session
from aisquare.core.tmux import TmuxError
from aisquare.models import FleetAgent, LaunchSpec, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.fleet import (
    LABEL,
    LABEL_RULE,
    FleetError,
    NoSuchAgent,
    RelabelReceipt,
    label_from_text,
)
from tests import test_fleet_service as fleet_suite
from tests.rendered import plain
from tests.test_fleet_service import (
    FakeTmux,
    _add_task,
    _board_events,
    _board_session,
    _coder,
    _git,
    _settings,
)

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


def _live(project: ProjectInfo) -> list[tuple[str, str]]:
    """``(label, id)`` of every live row, oldest first."""
    with store_session() as store:
        return [(row.label, row.id) for row in store.fleet_agents(project.id, live_only=True)]


def _row(agent_id: str) -> FleetAgent:
    with store_session() as store:
        row = store.get_fleet_agent(agent_id)
    assert row is not None
    return row


# --- the label a typed name becomes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "role", "label"),
    [
        ("Login Fix", "coder", "login-fix"),
        ("Café ✨ review", "coder", "cafe-review"),
        ("2FA work", "coder", "coder-2fa-work"),
        ("  Fix the OAuth callback bug!!! ", "coder", "fix-the-oauth-callback-b"),
        ("login-fix", "coder", "login-fix"),
        ("UI_tester__flow", "tester", "ui-tester-flow"),
        ("\uff26\uff55\uff4c\uff4c \ufb01x", "coder", "full-fix"),  # fullwidth, a ligature
        ("Straße", "coder", "strasse"),
        ("42", "data_eng", "data-eng-42"),  # a bound role's `_` is no label character
        ("--a--b--", "coder", "a-b"),
        ("x" * 23 + " y", "coder", "x" * 23),  # cut at 24, the trailing `-` dropped
    ],
)
def test_a_typed_name_becomes_a_label(typed: str, role: str, label: str) -> None:
    made = label_from_text(typed, role)
    assert made == label
    assert LABEL.match(made) and len(made) <= 24 and not made.endswith("-")


@pytest.mark.parametrize("typed", ["!!!", "x", "7", "", "   ", "✨✨", "é"])
def test_text_that_leaves_under_two_letters_or_digits_is_refused_with_the_rule(
    typed: str,
) -> None:
    with pytest.raises(FleetError) as refused:
        label_from_text(typed, "coder")
    assert LABEL_RULE in str(refused.value)
    assert "at least two letters or digits" in str(refused.value)


def test_whatever_is_typed_is_a_label_or_refused() -> None:
    """Every printable code point, alone and in runs: the result always matches LABEL."""
    samples = [chr(code) * 3 + " ab" for code in range(32, 0x2FFF, 7)]
    samples += [f"{chr(code)}9{chr(code)}" for code in range(32, 0x2FFF, 11)]
    for typed in samples:
        try:
            made = label_from_text(typed, "coder")
        except FleetError:
            continue
        assert LABEL.match(made), (typed, made)


# --- the rename ---------------------------------------------------------------------------


def test_a_rename_moves_the_row_the_lookups_and_the_tmux_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project, label="coder-1")
    receipt = fleet_service.relabel(project, "coder-1", "Login Fix")

    assert isinstance(receipt, RelabelReceipt) and receipt.changed
    assert receipt.said == "coder-1 is now login-fix" and receipt.notes == []
    assert _row(coder.id).label == "login-fix"
    assert tmux.window_name(coder.pane_id) == "login-fix"
    assert tmux.renamed_windows == [(coder.pane_id, "login-fix")]
    [listed] = fleet_service.list_agents(project)
    assert (listed.agent.id, listed.agent.label) == (coder.id, "login-fix")
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "coder-1", live_only=False) is None
        assert store.fleet_agent_by_label(project.id, "login-fix").id == coder.id  # type: ignore[union-attr]


def test_a_taken_name_gets_the_next_free_suffix_and_the_owner_is_told(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    _coder(project, label="login-fix")
    _coder(project, label="login-fix-2")
    third = _coder(project, label="coder-3")

    receipt = fleet_service.relabel(project, "coder-3", "login fix")

    assert receipt.agent.label == "login-fix-3" and receipt.wanted == "login-fix"
    assert receipt.said == "coder-3 is now login-fix-3"
    assert any("login-fix is another live agent's label" in note for note in receipt.notes)
    assert _row(third.id).label == "login-fix-3"
    assert sorted(label for label, _ in _live(project)) == [
        "login-fix",
        "login-fix-2",
        "login-fix-3",
    ]


def test_an_ended_agents_label_is_free_to_take(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Only a LIVE holder suffixes: an agent that ended freed its label (as for a spawn)."""
    _coder(project, label="login-fix")
    fleet_service.stop(project, "login-fix", force=True)
    second = _coder(project, label="coder-2")
    assert fleet_service.relabel(project, "coder-2", "Login fix").agent.label == "login-fix"
    assert _row(second.id).label == "login-fix"


def test_renaming_an_agent_to_its_own_label_changes_nothing(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project, label="login-fix")
    receipt = fleet_service.relabel(project, "login-fix", "  Login FIX ")
    assert not receipt.changed and receipt.agent.id == coder.id
    assert receipt.said == "login-fix is already called login-fix — nothing changed"
    assert tmux.renamed_windows == [] and _board_events(project, "agent_renamed") == []


def test_a_suffix_that_lands_on_its_own_label_keeps_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``login-fix-2`` asked for ``login fix`` while another holds it: the next free one is its
    own, so nothing is renamed — and it is said why."""
    _coder(project, label="login-fix")
    _coder(project, label="login-fix-2")
    receipt = fleet_service.relabel(project, "login-fix-2", "Login fix")
    assert not receipt.changed and receipt.agent.label == "login-fix-2"
    assert receipt.notes == ["login-fix is another live agent's label — login-fix-2 keeps its own"]
    assert tmux.renamed_windows == []


# --- atomic against the live-label index ------------------------------------------------------


def test_the_store_refuses_a_live_label_another_row_holds_and_writes_nothing(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    first = _coder(project, label="login-fix")
    second = _coder(project, label="coder-2")
    with store_session() as store:
        store.set_ui_state(f"fleet-launched:{project.id}:coder-2", '{"agent": "x"}')
        with pytest.raises(sqlite3.IntegrityError):
            store.relabel_fleet_agent(second.id, "coder-2", "login-fix")
        assert store.ui_state(f"fleet-launched:{project.id}:coder-2") == '{"agent": "x"}'
        assert store.ui_state(f"{FLEET_RENAMED}{project.id}:coder-2") is None
        # A stale read (the row is no longer `old`) writes nothing either.
        assert store.relabel_fleet_agent(second.id, "coder-9", "other") is None
    assert _row(first.id).label == "login-fix" and _row(second.id).label == "coder-2"


def test_a_rename_that_loses_the_label_to_a_racer_takes_the_next_suffix(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the read of who holds what and the write, another rename takes the label: the
    index refuses the write, and the rename picks again from a fresh read."""
    racer = _coder(project, label="coder-1")
    mine = _coder(project, label="coder-2")
    real = SqliteStore.relabel_fleet_agent
    raced: list[str] = []

    def racing(self: SqliteStore, agent_id: str, old: str, new: str) -> FleetAgent | None:
        if not raced:
            raced.append(new)
            assert real(self, racer.id, "coder-1", new) is not None  # the racer lands first
        return real(self, agent_id, old, new)

    monkeypatch.setattr(SqliteStore, "relabel_fleet_agent", racing)
    receipt = fleet_service.relabel(project, "coder-2", "login fix")

    assert raced == ["login-fix"]
    assert (_row(racer.id).label, _row(mine.id).label) == ("login-fix", "login-fix-2")
    assert receipt.agent.label == "login-fix-2"
    assert len({label for label, _ in _live(project)}) == len(_live(project))


def test_two_renames_at_once_never_leave_two_live_rows_with_one_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Two threads, two connections, one name, released together."""
    first = _coder(project, label="coder-1")
    second = _coder(project, label="coder-2")
    start = threading.Barrier(2)
    done: dict[str, RelabelReceipt | BaseException] = {}

    def rename(label: str) -> None:
        start.wait()
        try:
            done[label] = fleet_service.relabel(project, label, "Login fix")
        except BaseException as exc:  # pragma: no cover - reported below
            done[label] = exc

    threads = [threading.Thread(target=rename, args=(label,)) for label in ("coder-1", "coder-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert all(isinstance(result, RelabelReceipt) for result in done.values()), done
    labels = {_row(first.id).label, _row(second.id).label}
    assert labels == {"login-fix", "login-fix-2"}


def test_a_spawn_racing_a_rename_never_shares_its_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn picked ``login-fix`` from a snapshot taken before the rename took it: its row
    trips the index and is recorded under the next free label, as for a spawn that raced a
    spawn (``_relabel``): the one the rename freed."""
    _coder(project, label="coder-1")
    fleet_service.relabel(project, "coder-1", "login fix")
    real = fleet_service.next_label
    stale = {"once": True}

    def snapshot_from_before(*args: Any, **kwargs: Any) -> str:
        if stale.pop("once", False):
            return "login-fix"
        return real(*args, **kwargs)

    monkeypatch.setattr(fleet_service, "next_label", snapshot_from_before)
    receipt = fleet_service.spawn(project, "coder", worktree=False)
    assert receipt.agent.label == "coder-1"
    assert any("'login-fix' was taken while starting" in note for note in receipt.notes)
    labels = [label for label, _ in _live(project)]
    assert sorted(labels) == ["coder-1", "login-fix"]


# --- what cannot be renamed -------------------------------------------------------------------


def test_the_manager_is_not_renamed_and_no_agent_takes_its_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    _coder(project, label="coder-1")
    with pytest.raises(FleetError, match="the manager is always labelled 'manager'"):
        fleet_service.relabel(project, "manager", "boss")
    for name in ("Manager", "  MANAGER!", "captain"):
        with pytest.raises(FleetError, match=r"that label is reserved for the (manager|captain)"):
            fleet_service.relabel(project, "coder-1", name)
    assert sorted(label for label, _ in _live(project)) == ["coder-1", "manager"]
    assert tmux.renamed_windows == []


def test_the_captains_row_and_an_ended_agent_offer_no_rename(tmp_path: Path) -> None:
    """The rule the sidebar asks too, with no store read."""
    base = FleetAgent(
        id="agt_x",
        project_id="prj_x",
        label="coder-1",
        role="coder",
        pane_id="%1",
        cwd=tmp_path,
        created_at=datetime(2026, 10, 28, tzinfo=UTC),
    )
    assert fleet_service.relabel_refusal(base) is None
    captain = base.model_copy(update={"role": "captain", "label": "captain"})
    assert "always labelled 'captain'" in (fleet_service.relabel_refusal(captain) or "")
    manager = base.model_copy(update={"role": "manager", "label": "manager"})
    assert "always labelled 'manager'" in (fleet_service.relabel_refusal(manager) or "")
    ended = base.model_copy(update={"ended_at": datetime(2026, 10, 28, 1, tzinfo=UTC)})
    assert fleet_service.relabel_refusal(ended) == (
        "coder-1 has ended — only a running agent is renamed"
    )
    # The captain's launch flags alone, off the home board, make no captain's row
    # (`is_the_captains_row` asks both): a coder launched alike is renamed as any other.
    launched = base.model_copy(
        update={
            "launch_spec": LaunchSpec(
                binary="claude", extra_args=["--strict-mcp-config", "--tools", ""]
            ),
            "cwd": tmp_path,
        }
    )
    assert fleet_service.relabel_refusal(launched) is None, "not the home's: an ordinary row"


def test_an_ended_agent_is_refused_and_an_unknown_one_is_no_such_agent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    _coder(project, label="coder-1")
    fleet_service.stop(project, "coder-1", force=True)
    with pytest.raises(FleetError, match="coder-1 has ended — only a running agent is renamed"):
        fleet_service.relabel(project, "coder-1", "login fix")
    with pytest.raises(NoSuchAgent, match="no live agent 'nobody'"):
        fleet_service.relabel(project, "nobody", "login fix")


# --- it fully switches over -------------------------------------------------------------------


def test_the_old_label_resolves_nothing_and_says_what_the_agent_is_called_now(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project, label="coder-1")
    fleet_service.relabel(project, "coder-1", "login fix")

    for act in (
        lambda label: fleet_service.tell(project, label, "hello"),
        lambda label: fleet_service.switch(project, label, to="2"),
        lambda label: fleet_service.stop(project, label, force=True),
    ):
        with pytest.raises(NoSuchAgent) as refused:
            act("coder-1")
        assert "no live agent 'coder-1'" in str(refused.value)
        assert "(renamed: it is login-fix now)" in str(refused.value)
    with pytest.raises(NoSuchAgent, match="no agent 'coder-1'"):
        fleet_service.restart(project, "coder-1")

    told = fleet_service.tell(project, "login-fix", "hello")
    assert told.delivered or told.typed in ("yes", "no", "maybe")
    stopped = fleet_service.stop(project, "login-fix", force=True)
    assert stopped.agent.id == coder.id and stopped.agent.label == "login-fix"


def test_the_hint_is_gone_once_another_agent_takes_the_old_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    _coder(project, label="coder-1")
    fleet_service.relabel(project, "coder-1", "login fix")
    newer = _coder(project, label="coder-1")
    fleet_service.stop(project, "coder-1", force=True)
    with pytest.raises(NoSuchAgent) as refused:
        fleet_service.tell(project, "coder-1", "hello")
    assert "renamed" not in str(refused.value), "coder-1 is the newer agent's label now"
    assert _row(newer.id).ended_at is not None


def test_a_view_pinned_to_the_row_still_finds_it_after_a_rename(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The agent view and the Stop dialog read the row before the rename, and pin it by id."""
    coder = _coder(project, label="coder-1")
    fleet_service.relabel(project, "coder-1", "login fix")
    stopped = fleet_service.stop(project, "coder-1", force=True, agent_id=coder.id)
    assert stopped.agent.id == coder.id and stopped.agent.ended_at is not None


def test_the_tmux_rename_fails_open_with_a_note(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    coder = _coder(project, label="coder-1")

    def refusing(pane_id: str, name: str) -> None:
        raise TmuxError("no server running on /tmp/tmux-1000/asq")

    monkeypatch.setattr(tmux, "rename_window", refusing)
    receipt = fleet_service.relabel(project, "coder-1", "login fix")

    assert receipt.changed and _row(coder.id).label == "login-fix"
    [note] = receipt.notes
    assert "tmux could not rename its window (no server running" in note
    assert f"rename-window -t {coder.pane_id} login-fix" in note
    assert "address panes by id" in note


def test_no_window_is_renamed_on_a_server_that_is_not_the_rows(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A server started since numbers its panes from %0 again: the row's pane id is another
    agent's there, and its window keeps its name."""
    coder = _coder(project, label="coder-1")
    with store_session() as store:
        store.upsert_fleet_agent(
            coder.model_copy(update={"server_started_at": datetime(2026, 1, 1, tzinfo=UTC)})
        )
    tmux.started = datetime(2026, 2, 1, tzinfo=UTC)
    receipt = fleet_service.relabel(project, "coder-1", "login fix")
    assert receipt.changed and tmux.renamed_windows == []
    assert any("not the one it was started on" in note for note in receipt.notes)


def test_its_earlier_rows_and_label_state_move_with_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A restart leaves the agent's earlier rows under its label, and Restart all and the
    manager's dead-team line read the newest row of each label: left behind, the one before
    the rename would read as a dead ``coder-1`` to bring back."""
    live = _coder(project, label="coder-1")
    earlier = live.model_copy(
        update={
            "id": "agt_earlier",
            "pane_id": "%99",
            "created_at": live.created_at - timedelta(minutes=5),
            "ended_at": live.created_at - timedelta(minutes=1),
            "exit_status": 137,
            "ended_by": "died",
        }
    )
    with store_session() as store:
        store.upsert_fleet_agent(earlier)
        store.set_ui_state(f"fleet-launched:{project.id}:coder-1", '{"agent": "mine"}')
        store.set_ui_state(f"before-limit:{project.id}:login-fix", '{"stale": true}')

    fleet_service.relabel(project, "coder-1", "login fix")

    assert _row("agt_earlier").label == "login-fix"
    with store_session() as store:
        heads = {row.label: row.id for row in store.fleet_label_heads(project.id)}
        assert store.ui_state(f"fleet-launched:{project.id}:coder-1") is None
        assert store.ui_state(f"fleet-launched:{project.id}:login-fix") == '{"agent": "mine"}'
        assert store.ui_state(f"before-limit:{project.id}:login-fix") is None, "not ours"
        renamed = json.loads(store.ui_state(f"{FLEET_RENAMED}{project.id}:coder-1") or "{}")
    assert heads == {"login-fix": live.id}
    assert renamed["agent"] == live.id
    assert fleet_service.dead_team(project) == []


def test_the_board_says_who_it_is_now_and_its_posts_and_claims_still_work(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Its identity is the row's id (AISQUARE_FLEET_AGENT) and its session (--as): neither is
    the label, so its claims and posts go on, and the lines that name it read the row."""
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project, label="coder-1")
    _board_session(coder, "working")
    _board_session(manager, "waiting")
    task = _add_task(project, "Fix the login")

    fleet_service.relabel(project, "coder-1", "login fix")

    [event] = _board_events(project, "agent_renamed")
    assert event.text == "coder-1 is now login-fix"
    assert event.session_id is None, "no session's: the agent's own delta carries it too"
    assert event.meta == {"agent": coder.id, "from": "coder-1", "to": "login-fix"}
    claimed = team_service.claim_task(task.id, session_ref=coder.session_id)
    assert claimed.claimed_by == coder.session_id
    team_service.add_note("can I merge?", session_ref=coder.session_id, kind="question")
    with store_session() as store:
        me = store.get_session(manager.session_id or "")
        assert me is not None
        pack = team_service._start_pack(
            store, project, me, assigned=None, persona_note=None, fleet=None, cwd=None
        )
    # The manager's own view of the board: the question, the live agent, the claim.
    assert "login-fix question: can I merge?" in pack, pack
    assert "  - login-fix coder" in pack and "@login-fix]" in pack, pack
    assert "coder-1" not in pack, pack


def test_a_cli_rename_names_who_asked_on_the_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    _coder(project, label="coder-1")
    fleet_service.relabel(project, "coder-1", "login fix", actor="user")
    [event] = _board_events(project, "agent_renamed")
    assert event.text == "coder-1 is now login-fix — renamed by user"


# --- the agent keeps working where it works -----------------------------------------------------


def test_a_renamed_worktree_agent_keeps_its_tree_through_restart_spawn_stop_and_reap(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = fleet_service.spawn(project, "coder", label="coder-1", worktree=True).agent
    tree = coder.cwd
    trees = project.root / fleet_service.settings().worktree_dir
    assert tree == trees / "coder-1"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree)

    fleet_service.relabel(project, "coder-1", "Login Fix")

    # Not moved: the tree, the branch, and no tree under the new label.
    assert _row(coder.id).cwd == tree and tree.is_dir()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree) == branch
    assert not (trees / "login-fix").exists()

    # A restart carries on in its own tree, under its new label.
    restarted = fleet_service.restart(project, "login-fix").started
    assert (restarted.label, restarted.cwd) == ("login-fix", tree)
    assert not (trees / "login-fix").exists()
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree) == branch

    # A fresh spawn never starts in it: `coder-1` is free as a label, not as a tree.
    fresh = fleet_service.spawn(project, "coder", worktree=True).agent
    assert fresh.label != "coder-1" and fresh.cwd != tree
    asked = fleet_service.spawn(project, "coder", label="coder-1", worktree=True)
    assert asked.agent.label == "coder-1-2" and asked.agent.cwd == trees / "coder-1-2"

    # Stopped, then reaped: the tree is found by the row and handled.
    fleet_service.stop(project, "login-fix", force=True)
    report = fleet_service.reap(project)
    handled = report.worktrees_removed + [path for path, _ in report.worktrees_kept]
    assert tree in handled, report
    assert tree in report.worktrees_removed and not tree.exists(), "clean, on main's commit"


def test_a_take_over_of_a_renamed_worktree_agent_keeps_its_tree(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """A take-over as another role keeps its source's tree: renamed, that tree is still under
    today's ``worktree_dir``, so the take-over goes ahead there."""
    coder = fleet_service.spawn(project, "coder", label="coder-1", worktree=True).agent
    fleet_service.relabel(project, "coder-1", "login fix")
    receipt = fleet_service.hand_off(project, "login-fix", mode="take_over", role="tester")
    started = receipt.started.agent
    assert (started.label, started.role, started.cwd) == ("login-fix", "tester", coder.cwd)


def test_a_moved_worktree_dir_still_refuses_the_take_over(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ruling a rename must not loosen (test_a_take_over_as_another_role…): a tree that
    today's ``worktree_dir`` puts elsewhere is refused, renamed or not."""
    fleet_service.spawn(project, "coder", label="coder-1", worktree=True)
    fleet_service.relabel(project, "coder-1", "login fix")
    _settings(monkeypatch, worktree_dir=".trees-elsewhere")
    with pytest.raises(FleetError, match="a take-over keeps its source's label and tree"):
        fleet_service.hand_off(project, "login-fix", mode="take_over", role="tester")


# --- the CLI -----------------------------------------------------------------------------------


PROJECT = ProjectInfo(id="prj_01abc", root=Path("/home/me/work/api"), codename="amber-otter")


def _agent(label: str) -> FleetAgent:
    return FleetAgent(
        id="agt_1",
        project_id=PROJECT.id,
        label=label,
        role="coder",
        pane_id="%7",
        cwd=PROJECT.root,
        created_at=datetime(2026, 10, 28, tzinfo=UTC),
    )


def _faked(monkeypatch: pytest.MonkeyPatch, outcome: object) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake(*args: Any, **kwargs: Any) -> Any:
        calls.append((*args, kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def resolve(ref: str | None = None, **_: Any) -> ProjectInfo:
        return PROJECT

    monkeypatch.setattr("aisquare.cli.fleet.fleet_service.relabel", fake)
    monkeypatch.setattr("aisquare.cli.fleet.fleet_service.resolve_project", resolve)
    monkeypatch.setattr("aisquare.cli.fleet.fleet_service.acting_as", lambda ref=None: "user")
    return calls


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("NO_COLOR", "1")
    return CliRunner()


def test_the_cli_prints_what_it_is_called_now_with_the_notes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = RelabelReceipt(
        _agent("login-fix-2"),
        "coder-1",
        "login-fix",
        ["login-fix is another live agent's label — named it login-fix-2"],
    )
    calls = _faked(monkeypatch, receipt)

    result = runner.invoke(app, ["fleet", "relabel", "coder-1", "login", "fix", "-P", "api"])

    assert result.exit_code == 0, result.output
    out = plain(result.stdout)
    assert "✓ coder-1 is now login-fix-2" in out
    assert "· login-fix is another live agent's label — named it login-fix-2" in out
    assert calls == [(PROJECT, "coder-1", "login fix", {"actor": "user"})], "the words joined"


def test_the_cli_json_is_the_label_it_had_and_has(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _faked(monkeypatch, RelabelReceipt(_agent("login-fix"), "coder-1", "login-fix", []))
    result = runner.invoke(app, ["--json", "fleet", "relabel", "coder-1", "Login Fix"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert {k: payload[k] for k in ("label", "was", "asked", "changed", "notes")} == {
        "label": "login-fix",
        "was": "coder-1",
        "asked": "login-fix",
        "changed": True,
        "notes": [],
    }
    assert payload["agent"]["id"] == "agt_1"


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        (FleetError("cannot rename 'manager': the manager is always labelled"), "fleet_error"),
        (NoSuchAgent("no live agent 'coder-1' (renamed: it is login-fix now)"), "no_such_agent"),
    ],
)
def test_the_cli_refuses_with_the_services_reason(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, refusal: Exception, code: str
) -> None:
    _faked(monkeypatch, refusal)
    human = runner.invoke(app, ["fleet", "relabel", "coder-1", "x"])
    assert human.exit_code == 1 and human.stdout == ""
    assert str(refusal) in plain(human.stderr)
    as_json = runner.invoke(app, ["--json", "fleet", "relabel", "coder-1", "x"])
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["error"] == code


def test_the_cli_and_the_real_service_rename_end_to_end(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    coder = _coder(project, label="coder-1")
    monkeypatch.chdir(project.root)
    result = runner.invoke(app, ["fleet", "relabel", "coder-1", "Café", "review"])
    assert result.exit_code == 0, result.output
    assert "✓ coder-1 is now cafe-review" in plain(result.stdout)
    assert _row(coder.id).label == "cafe-review"
