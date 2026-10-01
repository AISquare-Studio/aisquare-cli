"""A new prompt from the same agent after ``resolve()`` is queued again (review of #240, finding 9).

A parked pane's row is keyed by the agent, so every prompt that agent ever shows is the
same row. Resolved by hand, the row re-opened only from an absence an EARLIER refresh had
recorded, and a refresh runs only on demand: the captain pressed yes and resolved, the
agent's next permission prompt came up before any refresh saw the first one clear, and
``attention()`` and ``next()`` said "nothing needs you" while the agent waited — until it
went stale half an hour later, ranked below every review and pull request.

Now a resolved prompt re-opens when what is observed is not what was resolved: a source
stamp newer than the row's (the Notification hook stamps each prompt), or other words (a
hook-less pane has no stamp but its start, so its prompt line is all there is). The same
stamp and the same words are the prompt the owner already answered, still being drawn,
and that stays resolved. Prompts only: the other state rows keep the absence rule.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core.ids import new_agent_id
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.captain import queue as captain_queue
from aisquare.services.captain.queue import STALE_AFTER, PullRequest
from tests import test_captain_queue as queue_suite
from tests import test_fleet_service as fleet_suite
from tests.test_captain_queue import Fixture, _agent, _session
from tests.test_fleet_service import FakeTmux

# The queue suite's fixture and the fleet suite's, bound here so pytest finds them for
# this module's tests.
fx = queue_suite.fx
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path


def _park(fx: Fixture, agent_id: str, *, stamped: datetime, detail: str = "…Bash") -> None:
    """coder1 parked on a permission prompt whose Notification hook stamped its session
    at ``stamped`` — what the fleet reports as ``attention``."""
    session = _session(fx.alpha, "s-coder1", "coder", last_seen=stamped)
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "coder1", "attention", detail=detail, session=session, agent_id=agent_id)
    ]


# --- the finding's repro ----------------------------------------------------------------


def test_the_next_prompt_after_a_resolve_is_queued_with_no_refresh_in_between(
    fx: Fixture,
) -> None:
    """The three steps: attention() shows coder1's prompt and the captain presses yes and
    resolves; coder1's NEXT prompt appears before any refresh has seen the first one clear;
    attention() and next() must name it, not answer "nothing needs you"."""
    aid = new_agent_id()
    queue = fx.queue()
    _park(fx, aid, stamped=fx.clock.now)
    (row,) = queue.attention(refresh=True)
    assert row.text == "coder1 waits on you: …Bash"
    fx.clock.tick(seconds=20)
    queue.resolve(row.id, "pressed yes")
    fx.clock.tick(seconds=5)
    _park(fx, aid, stamped=fx.clock.now)  # the same words: only the hook's stamp is new
    snapshot = queue.refresh()
    waiting = queue.attention()
    assert [item.id for item in waiting] == [row.id], "coder1 waits, and the owner hears it"
    (back,) = waiting
    assert back.count == 2 and back.last_seen == fx.clock.now
    assert [entry.action for entry in back.history] == ["resolved", "reopened"]
    assert snapshot.reopened == 1
    top = queue.top()
    assert top is not None and top.id == row.id, "next() is that prompt"


def _stamp(session_id: str) -> datetime:
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None
    return session.last_seen_at


def test_the_same_three_steps_through_the_store_the_hook_and_the_real_fleet(
    tmux: FakeTmux, claude_on_path: Path, tmp_path: Path
) -> None:
    """Nothing stood in: a fleet row, its board session, the Notification hook's own stamp
    (``mark_attention`` stamps a session already parked), ``fleet._derive`` reading
    ``attention``, and the four names the captain's tools call."""
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    project = ProjectInfo(id=project_id_for(root), root=root)
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    assert agent.session_id is not None
    fleet_suite._board_session(agent, "working")

    def a_permission_prompt(tool: str) -> None:
        assert agent.session_id is not None
        team_service.hook_notification(
            agent.session_id,
            root,
            f"Claude needs your permission to use {tool}",
            notification_type="permission_prompt",
        )

    a_permission_prompt("Bash")
    (row,) = captain_queue.ranked()
    assert (row["kind"], row["agent"], row["status"]) == ("waiting", agent.label, "open")
    captain_queue.resolve(str(row["id"]), "pressed yes")
    assert captain_queue.ranked() == [], "the answered prompt, still drawn, is not read back"
    answered = _stamp(agent.session_id)
    while datetime.now(tz=UTC) <= answered:  # the clock's own step: 15 ms on Windows
        time.sleep(0.001)
    a_permission_prompt("Edit")  # the next one, with no refresh having seen the pane move on
    assert _stamp(agent.session_id) > answered, "the hook stamps the prompt it is parked on"
    again = captain_queue.ranked()
    assert [item["id"] for item in again] == [row["id"]], "the coder waits; the owner hears it"
    assert again[0]["count"] == 2
    top = captain_queue.next_item()
    assert top is not None and top["id"] == row["id"]


def test_a_prompt_that_came_up_between_the_press_and_the_resolve_is_queued_too(
    fx: Fixture,
) -> None:
    """The press and the resolve are two tool calls, seconds apart, and a fast command puts
    the next prompt up between them: its stamp is OLDER than the resolve and newer than
    the prompt the row had seen. What was resolved is what the row had seen."""
    aid = new_agent_id()
    queue = fx.queue()
    _park(fx, aid, stamped=fx.clock.now)
    (row,) = queue.attention(refresh=True)
    next_prompt = fx.clock.tick(seconds=2)  # the press landed; the next prompt is up already
    fx.clock.tick(seconds=3)
    queue.resolve(row.id, "pressed yes")  # the captain's bookkeeping, a moment late
    _park(fx, aid, stamped=next_prompt)
    fx.clock.tick(seconds=30)
    queue.refresh()
    waiting = queue.attention()
    assert [item.id for item in waiting] == [row.id], "the prompt that is up is the owner's"
    assert waiting[0].count == 2 and waiting[0].last_seen == next_prompt


def test_a_resolved_prompt_that_is_still_being_drawn_stays_resolved(fx: Fixture) -> None:
    """The control: the same observation — the hook's own stamp, the same words — is the
    prompt the owner already answered. "I told the coder to go ahead" does not bounce back
    on the next tick."""
    aid = new_agent_id()
    queue = fx.queue()
    _park(fx, aid, stamped=fx.clock.now)
    (row,) = queue.attention(refresh=True)
    fx.clock.tick(seconds=20)
    queue.resolve(row.id, "pressed yes")
    for _ in range(3):
        fx.clock.tick(seconds=20)
        snapshot = queue.refresh()  # the pane is still parked on that prompt
        assert snapshot.reopened == 0
    (still,) = queue.items()
    assert still.status == "resolved" and still.count == 1
    assert [entry.action for entry in still.history] == ["resolved"]
    assert queue.attention() == [] and queue.top() is None


# --- a pane with no hooks: its words are all there is -------------------------------------


def test_a_hookless_panes_next_prompt_reopens_by_its_words(fx: Fixture) -> None:
    """A y/N line read off a pane has no stamp but the agent's start, which never moves:
    the same line still showing stays resolved, another line is another prompt."""
    aid = new_agent_id()
    fx.sources._agents[fx.alpha.id] = [
        _agent(fx.alpha, "codex1", "waiting", detail="no hooks", agent_id=aid)
    ]
    fx.sources._tails[aid] = ["Do you want to proceed? [y/N]"]
    queue = fx.queue()
    (row,) = queue.attention(refresh=True)
    queue.resolve(row.id, "pressed y")
    fx.clock.tick(seconds=20)
    queue.refresh()  # still drawing the line the owner answered
    assert queue.get(row.id).status == "resolved" and queue.attention() == []
    fx.clock.tick(seconds=20)
    fx.sources._tails[aid] = ["Overwrite config.toml? (y/n)"]
    queue.refresh()
    waiting = queue.attention()
    assert [item.id for item in waiting] == [row.id], "another line is another prompt"
    (back,) = waiting
    assert back.count == 2 and back.text == "codex1 asks: Overwrite config.toml? (y/n)"
    assert [entry.action for entry in back.history] == ["resolved", "reopened"]


# --- prompts only -----------------------------------------------------------------------


def test_a_resolved_stale_row_does_not_reopen_as_its_minutes_count_up(fx: Fixture) -> None:
    """A stale row's words count the minutes: compared like a prompt's, a resolved one
    would come back on every refresh a minute apart."""
    dark = _session(
        fx.alpha, "s-dark", "coder", last_seen=fx.clock.now - STALE_AFTER - timedelta(minutes=1)
    )
    fx.sources._agents[fx.alpha.id] = [_agent(fx.alpha, "dark", "waiting", session=dark)]
    queue = fx.queue()
    (row,) = queue.attention(refresh=True)
    assert row.kind == "stale" and row.text.endswith("for 31 min")
    queue.resolve(row.id, "left it")
    for _ in range(3):
        fx.clock.tick(minutes=1)
        queue.refresh()
    (still,) = queue.items()
    assert still.status == "resolved" and still.count == 1 and still.text.endswith("for 31 min")


def test_a_resolved_pull_request_does_not_reopen_on_every_refresh(fx: Fixture) -> None:
    """A provider gives no stamp, so a PR is stamped with the tick's own clock: compared
    like a prompt's, a resolved one would come back on every refresh."""
    fx.sources._prs[fx.alpha.id] = [PullRequest(12, "Ship the release", "https://example/12")]
    queue = fx.queue()
    (row,) = queue.attention(refresh=True)
    assert row.kind == "pr"
    queue.resolve(row.id, "merged it")
    for _ in range(3):
        fx.clock.tick(seconds=30)
        queue.refresh()
    (still,) = queue.items()
    assert still.status == "resolved" and still.count == 1
    assert queue.attention() == []
