"""No Codex agent starts blind on a slot without aisquare's hooks (SMOKE12 O1 of #113).

On real Codex 0.166.0, ``fleet switch coder2 --to 2`` onto a Codex slot made by hand (a
login copied in, past the hook install ``accounts add`` does) said nothing and started the
agent there with no ``hooks.json``: it ran its hand-off prompt, but no session registered,
its claim stayed on the ended session, and no board update reached it. The next switch then
brought that ended session back to the board as a live coder (``mark_handover`` cleared its
``ended_at``), where it stayed. A spawn, restart or switch now installs the hooks where the
window starts before anything is stopped or started, or refuses saying why; and a hand-over
mark leaves an ended session ended. On the fleet's fake tmux, as ``test_codex_account_switch``.

Only where the agent's hooks reach it from its home alone (``hooks_before_launch``, Codex's),
and only into a slot aisquare made (review r37 of #113, finding 2): Codex's own home, after
``agents disconnect``, is refused before anything is stopped, nothing written; and a Claude
Code spawn, restart or switch writes nothing in its home and refuses nothing, as on main.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import agents
from aisquare.core import claude_accounts as core
from aisquare.core.agent_adapters import codex_config, get_adapter
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo, TeamSession
from aisquare.services import agents as agents_service
from aisquare.services import fleet as fleet_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_hand_off_across_agents import _claude_slots


def _slot(email: str) -> ClaudeAccount:
    """A Codex slot as a hand-made rig leaves one: signed in, no hooks."""
    slot = core.create_account("codex")
    _sign_in(slot.config_dir, email)
    assert not (slot.config_dir / "hooks.json").exists()
    return slot


def _bound(work: ProjectInfo, agent: FleetAgent, slot: ClaudeAccount, sid: str) -> None:
    """The agent's row bound to session ``sid``, as its first hook binds a Codex thread."""
    now = datetime.now(tz=UTC)
    rollout = slot.config_dir / "sessions" / f"rollout-{sid}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("{}\n", encoding="utf-8")
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=sid,
                project_id=work.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                transcript_path=str(rollout),
                agent="codex",
                account=str(slot.config_dir),
            )
        )
        stored = store.get_fleet_agent(agent.id)
        assert stored is not None
        store.upsert_fleet_agent(stored.model_copy(update={"session_id": sid}))


def _trusted(home: Path) -> int:
    """How many of aisquare's hooks in ``home`` its config.toml trusts, as Codex reads it."""
    return sum(
        1
        for key in codex_config.hook_states(home)
        if key.startswith(str((home / "hooks.json").resolve()))
    )


def _installed_note(home: Path) -> str:
    return f"Codex's hooks were not installed in {home} — installed them before it started there"


def test_a_spawn_onto_a_slot_with_no_hooks_installs_and_trusts_them_first(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    slot = _slot("one@example.com")

    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    )

    assert agents.hooks_installed("codex", slot.config_dir)
    assert _trusted(slot.config_dir) == 8, "no 'Hooks need review' to stall its start"
    assert _installed_note(slot.config_dir) in receipt.notes, receipt.notes
    # Installed, the next spawn there has nothing to say.
    again = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    )
    assert not [note for note in again.notes if "hooks were not installed" in note]


def test_a_switch_onto_a_slot_with_no_hooks_installs_them_before_the_stop(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    here, there = _slot("here@example.com"), _slot("there@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(here.slot)
    ).agent
    _bound(project, agent, here, "codex-thread-here")

    receipt = fleet_service.switch(project, agent.label, to=str(there.slot))

    assert receipt.to_slot == there.slot and receipt.started.account_slot == there.slot
    assert agents.hooks_installed("codex", there.config_dir)
    assert _trusted(there.config_dir) == 8
    assert _installed_note(there.config_dir) in receipt.notes, receipt.notes


@pytest.mark.parametrize("verb", ["switch", "restart"])
def test_a_hand_over_onto_a_home_its_hooks_cannot_go_in_is_refused_before_the_stop(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    verb: str,
) -> None:
    """A ``hooks.json`` that is a directory cannot be written: the agent is left running, its
    window unkilled, and the refusal says why and what installs them."""
    here, there = _slot("here@example.com"), _slot("there@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(here.slot)
    ).agent
    _bound(project, agent, here, "codex-thread-here")
    target = there if verb == "switch" else here
    hooks = target.config_dir / "hooks.json"
    if hooks.exists():
        hooks.unlink()
    hooks.mkdir()

    with pytest.raises(fleet_service.FleetError) as refused:
        if verb == "switch":
            fleet_service.switch(project, agent.label, to=str(there.slot))
        else:
            fleet_service.restart(project, agent.label)

    said = str(refused.value)
    assert f"Codex's hooks are not installed in {target.config_dir}" in said
    assert "nothing was stopped" in said and "agents connect codex --config-dir" in said
    assert agent.pane_id not in tmux.killed
    assert not [typed for typed in tmux.typed if typed[0] == agent.pane_id], "no /exit typed"
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.ended_at is None, "still running where it was"


def test_a_restart_puts_back_hooks_taken_out_since_its_spawn(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    slot = _slot("one@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    ).agent
    _bound(project, agent, slot, "codex-thread-one")
    agents.remove_hooks("codex", slot.config_dir)
    assert not agents.hooks_installed("codex", slot.config_dir)

    receipt = fleet_service.restart(project, agent.label)

    assert agents.hooks_installed("codex", slot.config_dir)
    assert _installed_note(slot.config_dir) in receipt.notes, receipt.notes


def test_a_switch_of_an_agent_whose_session_ended_leaves_that_session_ended(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The row still names the session that ended when its last hand-over stopped it (its
    replacement, started blind, never registered one). The mark the next switch sets on it
    is no sign of life: it stays ended, and off the board's live sessions."""
    here, there = _slot("here@example.com"), _slot("there@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(here.slot)
    ).agent
    _bound(project, agent, here, "codex-thread-ended")
    with store_session() as store:
        store.end_session("codex-thread-ended", release_claims=False)
        ended = store.get_session("codex-thread-ended")
    assert ended is not None and ended.ended_at is not None

    fleet_service.switch(project, agent.label, to=str(there.slot))

    with store_session() as store:
        after = store.get_session("codex-thread-ended")
        live = {s.id for s in store.team_sessions(project.id) if s.ended_at is None}
    assert after is not None and after.ended_at is not None, "revived by the hand-over mark"
    assert "codex-thread-ended" not in live


def test_a_hand_over_mark_on_an_ended_session_marks_it_and_keeps_it_ended() -> None:
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="sess-ended", project_id="prj_x", role="coder", started_at=now, last_seen_at=now
            )
        )
        store.end_session("sess-ended", release_claims=False)
        assert store.mark_handover("sess-ended", held_after=now - timedelta(minutes=5))
        marked = store.get_session("sess-ended")
        store.upsert_session(
            TeamSession(
                id="sess-live", project_id="prj_x", role="coder", started_at=now, last_seen_at=now
            )
        )
        assert store.mark_handover("sess-live", held_after=now - timedelta(minutes=5))
        live = store.get_session("sess-live")
    assert marked is not None and marked.state == "switching" and marked.ended_at is not None
    assert live is not None and live.state == "switching" and live.ended_at is None


# --- whose homes a launch writes (review r37 of #113, finding 2) -----------------------------


def _files(home: Path) -> dict[str, bytes]:
    """Every file under ``home``, by its path there, and its bytes."""
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    "settings",
    [b'{\n  "theme": "dark"\n}\n', b'{\n  "theme": "dark",\n}\n'],
    ids=["unconnected", "unparsable"],
)
@pytest.mark.parametrize("where", ["plain-home", "slot"])
def test_a_claude_code_spawn_restart_and_switch_write_nothing_in_its_home_and_refuse_nothing(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    settings: bytes,
    where: str,
) -> None:
    """As on main: Claude Code's hooks come with aisquare's plugin and its launch
    (``hooks_before_launch`` False), so a spawn, a restart and a switch onto a home where it
    is not connected, the user's own after ``agents disconnect`` included, leave its
    ``settings.json`` byte for byte, and one that is no JSON refuses none of them. Before,
    each wrote aisquare's hooks into it, and the unparsable one refused the spawn."""
    assert not get_adapter(core.AGENT).capabilities.hooks_before_launch
    plain = core.default_account()
    first, second = _claude_slots(monkeypatch, 10, 10)
    start = plain if where == "plain-home" else first
    homes = [plain.config_dir, first.config_dir, second.config_dir]
    for home in homes:
        home.mkdir(parents=True, exist_ok=True)
        (home / "settings.json").write_bytes(settings)
        assert not agents.connected(core.AGENT, home)
    before = {home: _files(home) for home in homes}

    spawned = fleet_service.spawn(project, "coder", worktree=False, account=str(start.slot))
    restarted = fleet_service.restart(project, spawned.agent.label)
    switched = fleet_service.switch(project, spawned.agent.label, to=str(second.slot))

    assert switched.started.account_slot == second.slot
    for home in homes:
        assert _files(home) == before[home], f"{home} written"
    said = [*spawned.notes, *restarted.notes, *switched.notes]
    assert not [note for note in said if "hooks were not installed" in note], said


def _disconnected_plain_codex_home() -> Path:
    """Codex's own home, signed in, its hooks connected and then disconnected by the user."""
    plain = core.default_account(agent="codex").config_dir
    plain.mkdir(parents=True, exist_ok=True)
    _sign_in(plain, "mine@example.com")
    assert core.managed_slot(plain, "codex") is None, "a home the user owns"
    agents_service.connect("codex", plain)
    assert agents.hooks_installed("codex", plain) and _trusted(plain) == 8
    agents_service.disconnect("codex", plain)
    assert not agents.connected("codex", plain, cwd=plain)
    return plain


def _refused_for_its_home(said: str, home: Path, nothing: str) -> None:
    assert f"Codex's hooks are not installed in {home}, a home of yours" in said, said
    assert f"{nothing}; `aisquare agents connect codex --config-dir {home}`" in said, said


def test_a_codex_spawn_onto_its_disconnected_plain_home_is_refused_and_writes_nothing(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """``agents disconnect codex`` is the user's choice: their own home is never written at a
    launch, and the spawn is refused, naming the command that connects it again."""
    plain = _disconnected_plain_codex_home()
    before = _files(plain)

    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.spawn(
            project,
            "coder",
            agent="codex",
            worktree=False,
            account=str(core.default_account(agent="codex").slot),
        )

    _refused_for_its_home(str(refused.value), plain, "nothing was started")
    assert _files(plain) == before, "nothing written in the user's home"
    assert tmux.spawned == [], "no window"
    with store_session() as store:
        assert store.live_fleet_agents() == []


@pytest.mark.parametrize("verb", ["switch", "restart"])
def test_a_codex_hand_over_onto_its_disconnected_plain_home_is_refused_before_the_stop(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    verb: str,
) -> None:
    """A switch onto the user's own disconnected home, and a restart of an agent that runs
    there after the user disconnected it, are refused before anything is stopped: the agent
    runs on, its window unkilled, and the home is left as the user left it."""
    plain_account = core.default_account(agent="codex")
    plain = plain_account.config_dir
    if verb == "switch":
        here = _slot("here@example.com")
        plain = _disconnected_plain_codex_home()
        agent = fleet_service.spawn(
            project, "coder", agent="codex", worktree=False, account=str(here.slot)
        ).agent
        _bound(project, agent, here, "codex-thread-here")
    else:
        plain.mkdir(parents=True, exist_ok=True)
        _sign_in(plain, "mine@example.com")
        agents_service.connect("codex", plain)
        agent = fleet_service.spawn(
            project, "coder", agent="codex", worktree=False, account=str(plain_account.slot)
        ).agent
        _bound(project, agent, plain_account, "codex-thread-mine")
        agents_service.disconnect("codex", plain)
    before = _files(plain)

    with pytest.raises(fleet_service.FleetError) as refused:
        if verb == "switch":
            fleet_service.switch(project, agent.label, to=str(plain_account.slot))
        else:
            fleet_service.restart(project, agent.label)

    _refused_for_its_home(str(refused.value), plain, "nothing was stopped")
    assert _files(plain) == before, "nothing written in the user's home"
    assert agent.pane_id not in tmux.killed
    assert not [typed for typed in tmux.typed if typed[0] == agent.pane_id], "no /exit typed"
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.ended_at is None, "still running where it was"
