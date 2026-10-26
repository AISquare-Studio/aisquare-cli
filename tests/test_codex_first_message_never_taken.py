"""A first message that never started a turn surfaces on Codex as it does on Claude Code
(#295's W1a, #113's #17).

Claude Code's ``SessionStart`` fires at rest, so its row has a session before any prompt and a
listing can tell "pending, the box idle" from "taken". Codex starts its session with its first
prompt, and with its hooks not trusted never does: its row has no session, so the listing had
nothing to read and said ``pending`` for as long as it lived. What it does have is its rollout,
which records every prompt it ran, hooks or not (measured on 0.164.0), so the listing asks it
(``AgentAdapter.first_prompts_since``, ``codex_prompts``):

- a prompt that reads as the message: it was taken (``acked``);
- none, past ``[fleet] prompt_ack_seconds`` with the pane idle at its empty prompt: it is not
  going to be (``unsent``, ``attention``, one ``prompt_unsent`` on the board);
- no such record (an agent that keeps none), or a pane that is not idle: as it was.

The reader is bounded: it lists the day directories from the spawn on, opens only the rollouts
whose names began after it, and reads the head and tail of one in this folder.
"""

from __future__ import annotations

import json
import os
import tracemalloc
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.agent_adapters import codex_prompts, register, unregister
from aisquare.core.config import FleetSettings
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests.budgets import budget
from tests.fake_agent import FakeAgentAdapter
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import CHEVRON, IDLE_PROMPT
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    clock,
    project,
    repo,
    tmux,
)

MESSAGE = "start on tsk_1 and read the board first"
WORKING = [
    "  >_ OpenAI Codex (v0.164.0)",
    "",
    "• Working (3s • esc to interrupt)",
    "",
    f"{CHEVRON} Ask Codex to do anything",
    "",
    "  gpt-5.5 default · /work/repo",
]


def _local(moment: datetime) -> datetime:
    return moment.astimezone()


def _rollout(
    home: Path,
    cwd: Path,
    prompts: list[str],
    *,
    began: datetime | None = None,
    own_context: bool = True,
) -> Path:
    """One Codex rollout as 0.164.0 lays it out: ``sessions/YYYY/MM/DD/rollout-<local time>-
    <id>.jsonl``, its first line the ``session_meta`` naming the folder, then Codex's own
    context as ``user`` messages (the environment), then the prompts."""
    began = _local(began or datetime.now(tz=UTC))
    day = home / "sessions" / f"{began:%Y}" / f"{began:%m}" / f"{began:%d}"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{began:%Y-%m-%dT%H-%M-%S}-{uuid.uuid4()}.jsonl"
    lines: list[dict[str, Any]] = [
        {"type": "session_meta", "payload": {"id": "thr", "cwd": str(cwd)}},
    ]
    texts = ["<environment_context>\n  <cwd>x</cwd>\n</environment_context>"] if own_context else []
    for text in [*texts, *prompts]:
        content = [{"type": "input_text", "text": text}]
        lines.append(
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": content},
            }
        )
        lines.append({"type": "event_msg", "payload": {"type": "token_count", "info": None}})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


class Spawned:
    """A Codex coder given a first message, its pane idle at the empty prompt, no session."""

    def __init__(
        self, where: ProjectInfo, server: FakeTmux, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.project, self.tmux, self.monkeypatch = where, server, monkeypatch
        self.agent: FleetAgent = fleet_service.spawn(
            where, "coder", agent="codex", worktree=False, prompt=MESSAGE
        ).agent
        assert self.agent.session_id is None, "Codex's session binds at its first prompt"
        server.set_command(self.agent.pane_id, "codex")
        server.screens[self.agent.pane_id] = IDLE_PROMPT
        with store_session() as store:
            raw = store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{self.agent.id}")
        assert raw is not None, "the spawn noted what its first message was given as"
        given = json.loads(raw)
        self.home, self.cwd = Path(given["home"]), Path(given["cwd"])

    def later(self, seconds: float) -> None:
        moved = datetime.now(tz=UTC) + timedelta(seconds=seconds)
        self.monkeypatch.setattr(fleet_service, "_now", lambda: moved)

    def row(self) -> FleetAgent:
        with store_session() as store:
            row = store.get_fleet_agent(self.agent.id)
        assert row is not None
        return row

    def listed(self) -> Any:
        [status] = fleet_service.list_agents(self.project)
        return status

    def unsent_events(self) -> list[str]:
        with store_session() as store:
            events = store.events_since(self.project.id, 0, limit=500)
        return [event.text for event in events if event.kind == "prompt_unsent"]


@pytest.fixture
def spawned(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Spawned:
    return Spawned(project, tmux, monkeypatch)


# --- the three outcomes ---------------------------------------------------------------------------


def test_a_prompt_the_rollout_records_is_a_first_message_taken(spawned: Spawned) -> None:
    """The row has no session and no hook ever spoke for it; its rollout holds the message
    among Codex's own context, so it is ``acked`` and no listing reads ``attention``."""
    _rollout(spawned.home, spawned.cwd, [MESSAGE])
    spawned.later(46)

    status = spawned.listed()

    assert status.state != "attention"
    assert spawned.row().first_prompt == "acked"
    assert spawned.unsent_events() == []
    with store_session() as store:
        assert store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}") is None


def test_no_such_prompt_at_an_idle_prompt_is_unsent_and_attention_once(spawned: Spawned) -> None:
    """Codex's rollouts in this folder hold other prompts and not this one, and its pane is
    idle at the empty prompt past ``prompt_ack_seconds``: the message is not going to be
    taken. ``attention`` with what to do, one ``prompt_unsent`` however many listings look;
    when the human's Enter later runs it, the rollout says so and the row is ``acked``."""
    _rollout(spawned.home, spawned.cwd, ["something the human typed instead"])
    spawned.later(46)
    with store_session() as store:
        named = store.get_project(spawned.project.id) or spawned.project  # its codename

    for _ in range(2):
        status = spawned.listed()
        assert status.state == "attention"
        assert status.detail == fleet_service.unsent_detail(named, spawned.agent.label)
    assert spawned.row().first_prompt == "unsent"
    assert len(spawned.unsent_events()) == 1

    _rollout(
        spawned.home, spawned.cwd, [MESSAGE], began=datetime.now(tz=UTC) + timedelta(seconds=50)
    )
    assert spawned.listed().state != "attention"
    assert spawned.row().first_prompt == "acked"


def test_no_rollout_at_all_is_no_prompt_either(spawned: Spawned) -> None:
    """A Codex home with no ``sessions`` directory: it ran nothing, and the idle pane says
    nothing is coming."""
    assert not (spawned.home / "sessions").exists()
    spawned.later(46)

    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


@pytest.mark.parametrize(
    ("screen", "why"),
    [
        (WORKING, "a turn is running at it"),
        ([*IDLE_PROMPT[:2], f"{CHEVRON} half a sentence"], "a draft"),
    ],
    ids=["a-turn-running", "a-draft-in-the-composer"],
)
def test_a_pane_that_is_not_idle_at_an_empty_prompt_is_left_alone(
    spawned: Spawned, screen: list[str], why: str
) -> None:
    spawned.tmux.screens[spawned.agent.pane_id] = screen
    spawned.later(46)

    spawned.listed()

    assert spawned.row().first_prompt == "pending", why
    assert spawned.unsent_events() == []


def test_a_message_still_inside_its_wait_is_left_alone(spawned: Spawned) -> None:
    spawned.listed()
    assert spawned.row().first_prompt == "pending" and spawned.unsent_events() == []


@pytest.mark.parametrize(
    "other",
    ["folder", "before", "context"],
    ids=["another-folder", "began-before-the-spawn", "codexs-own-context"],
)
def test_only_a_rollout_of_this_folder_after_the_spawn_with_the_message_counts(
    spawned: Spawned, tmp_path: Path, other: str
) -> None:
    now = datetime.now(tz=UTC)
    if other == "folder":
        _rollout(spawned.home, tmp_path / "elsewhere", [MESSAGE])
    elif other == "before":
        _rollout(spawned.home, spawned.cwd, [MESSAGE], began=now - timedelta(minutes=5))
    else:
        _rollout(spawned.home, spawned.cwd, [], own_context=True)
    spawned.later(46)

    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_the_message_is_matched_as_the_spawn_wrote_it_whatever_its_whitespace(
    spawned: Spawned,
) -> None:
    _rollout(spawned.home, spawned.cwd, ["  start on tsk_1 and\nread the board   first\n"])
    spawned.later(46)

    spawned.listed()

    assert spawned.row().first_prompt == "acked"


# --- an agent that keeps no such record -------------------------------------------------------


@pytest.fixture
def fake_registered() -> Iterator[FakeAgentAdapter]:
    adapter = FakeAgentAdapter()
    register(adapter)
    try:
        yield adapter
    finally:
        unregister(adapter.id)


def test_an_agent_that_keeps_no_record_of_its_prompts_leaves_the_row_as_it_was(
    fake_registered: FakeAgentAdapter,
    project: ProjectInfo,  # noqa: F811
) -> None:
    """``first_prompts_since`` is ``None`` for it: no record, so no verdict from here."""
    with store_session() as store:
        stored = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_fake",
                project_id=project.id,
                label="coder-1",
                role="coder",
                binary=fake_registered.binary,
                agent=fake_registered.id,
                pane_id="%7",
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
                first_prompt="pending",
                first_prompt_at=datetime.now(tz=UTC),
            )
        )
        store.set_meta(
            f"{fleet_service.FIRST_PROMPT_META}{stored.id}",
            json.dumps(
                {
                    "digest": fleet_service._first_prompt_digest(MESSAGE),
                    "home": str(project.root),
                    "cwd": str(project.root),
                    "at": datetime.now(tz=UTC).isoformat(),
                }
            ),
        )
    assert fake_registered.first_prompts_since(project.root, project.root, datetime.now()) is None
    moved = datetime.now(tz=UTC) + timedelta(seconds=46)

    row = fleet_service._first_prompt_off_its_transcript(
        stored,
        _Pane(),  # type: ignore[arg-type]
        FleetSettings(),
        moved,
    )

    assert row.first_prompt == "pending"


class _Pane:
    """A pane that would say idle, if it were asked: it must not be."""

    def captured(self) -> list[str]:
        raise AssertionError("an agent with no record of its prompts is never asked about its pane")


# --- bounded ------------------------------------------------------------------------------------


def test_a_huge_rollout_beside_the_row_is_read_at_its_ends_and_in_time(
    spawned: Spawned, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 64 MB rollout of this folder, begun after the spawn (another agent on the same
    account home, working in the same folder), and 1,500 old ones in the days before: the
    check opens the one, reads a megabyte at each end of it, and none of the others."""
    old = datetime.now(tz=UTC) - timedelta(days=3)
    for index in range(1_500):
        began = old - timedelta(days=index % 20, seconds=index)
        _rollout(spawned.home, spawned.cwd, [f"old {index}"], began=began)
    big = _rollout(spawned.home, spawned.cwd, [MESSAGE])
    with big.open("r+b") as handle:
        handle.truncate(64 * 1024 * 1024)  # the rest is the working agent's, nothing readable
    spawned.later(46)
    codex_prompts._KEPT.clear()
    listed: list[str] = []
    real_listdir = os.listdir

    def listdir(folder: Any) -> list[str]:
        listed.append(str(folder))
        return real_listdir(folder)

    monkeypatch.setattr(os, "listdir", listdir)

    tracemalloc.start()
    started = os.times()[4]
    try:
        spawned.listed()
        elapsed = os.times()[4] - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert spawned.row().first_prompt == "acked", "the message is in its head"
    today = datetime.now().astimezone()
    days = [f for f in listed if f.count(os.sep) > str(spawned.home).count(os.sep) + 3]
    assert all(f.endswith(f"{today:%Y}/{today:%m}/{today:%d}") for f in days), (
        "a day before the spawn was listed",
        days,
    )
    assert peak < 16 * 1024 * 1024, f"a whole rollout was read ({peak:,} bytes held at once)"
    assert elapsed < budget(2.0), f"the check took {elapsed:.2f} s"


def test_a_rollout_unchanged_since_it_was_read_is_not_read_again(
    spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listings come every second or two for as long as the row is pending or unsent: what a
    rollout gave is kept against its size and modification time."""
    path = _rollout(spawned.home, spawned.cwd, ["something else"])
    codex_prompts._KEPT.clear()
    reads: list[Path] = []
    real = codex_prompts._read

    def counting(read: Path, folder: str, size: int) -> list[str]:
        reads.append(read)
        return real(read, folder, size)

    monkeypatch.setattr(codex_prompts, "_read", counting)
    spawned.later(46)

    spawned.listed()
    spawned.listed()
    spawned.listed()
    assert reads == [path], "read once, however many listings asked"

    with path.open("a", encoding="utf-8") as handle:  # the agent wrote on: read again
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
    spawned.listed()
    assert reads == [path, path]


def test_a_rollouts_head_and_tail_are_read_and_the_middle_is_not(tmp_path: Path) -> None:
    """A prompt in the first megabyte or the last is found; one deep in the middle of a
    rollout longer than both is not: the first message is near a session's start, and a
    resumed session's newest prompts are at its end."""
    folder = tmp_path / "work"
    folder.mkdir()
    path = _rollout(tmp_path / "home", folder, ["head prompt"])
    middle = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "middle prompt"}],
            },
        }
    )
    filler = json.dumps({"type": "event_msg", "payload": {"pad": "x" * 4_000}})
    tail = middle.replace("middle prompt", "tail prompt")
    with path.open("a", encoding="utf-8") as handle:
        for _ in range(codex_prompts.HEAD_BYTES // 4_000 + 40):
            handle.write(filler + "\n")
        handle.write(middle + "\n")
        for _ in range(codex_prompts.TAIL_BYTES // 4_000 + 40):
            handle.write(filler + "\n")
        handle.write(tail + "\n")

    found = codex_prompts.prompts_since(
        tmp_path / "home", folder, datetime.now(tz=UTC) - timedelta(minutes=1)
    )

    assert "head prompt" in found and "tail prompt" in found
    assert "middle prompt" not in found
