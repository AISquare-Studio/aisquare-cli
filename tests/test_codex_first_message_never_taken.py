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

Whose prompt it is (#24, tester-24) is ``tests/test_codex_first_message_whose_rollout.py``'s:
here every rollout is held by the row's own pane, as the one its agent writes is.
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

from aisquare.core import process_table
from aisquare.core.agent_adapters import codex_prompts, register, unregister
from aisquare.core.config import FleetSettings
from aisquare.core.process_table import PaneProcesses
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
PANE_PID = 4100
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


class Processes:
    """The machine's process table as a listing reads it (``core.process_table``), made up: which
    processes are below which pane's, and which files each holds open. A pane's agent, as Codex's
    is, sits two below the pane's own pid and holds its rollout from its first prompt on.
    ``readable`` false is a machine whose table cannot be read (no ``lsof``, a process that hides
    its descriptors)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.below: dict[int, set[int]] = {}
        self.files: dict[int, set[str]] = {}
        self.readable = True
        self.reads = 0
        monkeypatch.setattr(process_table, "HELD_FOR", 0.0)
        monkeypatch.setattr(process_table, "descendants", self._tree)
        monkeypatch.setattr(process_table, "open_files", self._open)

    def _tree(self, root: int) -> frozenset[int] | None:
        return frozenset({root, *self.below.get(root, ())}) if self.readable else None

    def _open(self, pids: Any) -> frozenset[str] | None:
        self.reads += 1
        if not self.readable:
            return None
        return frozenset(path for pid in pids for path in self.files.get(pid, ()))

    def pane(self, root: int) -> int:
        """A pane whose pid is ``root``, its agent below it; the agent's pid."""
        self.below[root] = {root + 1, root + 2}
        return root + 2

    def hold(self, agent_pid: int, path: Path) -> None:
        self.files.setdefault(agent_pid, set()).add(os.path.realpath(path))


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch) -> Processes:
    return Processes(monkeypatch)


class Spawned:
    """A Codex coder given a first message, its pane idle at the empty prompt, no session."""

    def __init__(
        self,
        where: ProjectInfo,
        server: FakeTmux,
        monkeypatch: pytest.MonkeyPatch,
        table: Processes,
        *,
        message: str = MESSAGE,
        pane_pid: int = PANE_PID,
    ) -> None:
        self.project, self.tmux, self.monkeypatch, self.table = where, server, monkeypatch, table
        self.agent: FleetAgent = fleet_service.spawn(
            where, "coder", agent="codex", worktree=False, prompt=message
        ).agent
        assert self.agent.session_id is None, "Codex's session binds at its first prompt"
        server.set_command(self.agent.pane_id, "codex")
        server.screens[self.agent.pane_id] = IDLE_PROMPT
        server.pids[self.agent.pane_id] = pane_pid
        self.pid = table.pane(pane_pid)
        with store_session() as store:
            raw = store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{self.agent.id}")
        assert raw is not None, "the spawn noted what its first message was given as"
        given = json.loads(raw)
        self.home, self.cwd = Path(given["home"]), Path(given["cwd"])

    def rollout(
        self,
        prompts: list[str],
        *,
        began: datetime | None = None,
        cwd: Path | None = None,
        held: bool = True,
        own_context: bool = True,
    ) -> Path:
        """A rollout of ``cwd`` (this row's folder by default), held open by this row's agent
        (``held``) or by some other process, another agent's, which is not this row's."""
        path = _rollout(self.home, cwd or self.cwd, prompts, began=began, own_context=own_context)
        if held:
            self.table.hold(self.pid, path)
        return path

    def given_at(self) -> datetime:
        """When the spawn gave the message (``_note_first_prompt``), as it noted it."""
        with store_session() as store:
            raw = store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{self.agent.id}")
        assert raw is not None
        return datetime.fromisoformat(json.loads(raw)["at"])

    def later(self, seconds: float) -> None:
        moved = datetime.now(tz=UTC) + timedelta(seconds=seconds)
        self.monkeypatch.setattr(fleet_service, "_now", lambda: moved)

    def row(self) -> FleetAgent:
        with store_session() as store:
            row = store.get_fleet_agent(self.agent.id)
        assert row is not None
        return row

    def listed(self) -> Any:
        """This row's listing, among the project's."""
        return next(
            s for s in fleet_service.list_agents(self.project) if s.agent.id == self.agent.id
        )

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
    processes: Processes,
) -> Spawned:
    return Spawned(project, tmux, monkeypatch, processes)


# --- the three outcomes ---------------------------------------------------------------------------


def test_a_prompt_the_rollout_records_is_a_first_message_taken(spawned: Spawned) -> None:
    """The row has no session and no hook ever spoke for it; its rollout holds the message
    among Codex's own context, so it is ``acked`` and no listing reads ``attention``."""
    spawned.rollout([MESSAGE])
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
    spawned.rollout(["something the human typed instead"])
    spawned.later(46)
    with store_session() as store:
        named = store.get_project(spawned.project.id) or spawned.project  # its codename

    for _ in range(2):
        status = spawned.listed()
        assert status.state == "attention"
        assert status.detail == fleet_service.unsent_detail(named, spawned.agent.label)
    assert spawned.row().first_prompt == "unsent"
    assert len(spawned.unsent_events()) == 1

    spawned.rollout([MESSAGE], began=datetime.now(tz=UTC) + timedelta(seconds=50))
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
        spawned.rollout([MESSAGE], cwd=tmp_path / "elsewhere")
    elif other == "before":
        spawned.rollout([MESSAGE], began=now - timedelta(minutes=5))
    else:
        spawned.rollout([], own_context=True)
    spawned.later(46)

    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_a_rollout_begun_a_few_seconds_before_the_spawns_clock_is_still_the_rows(
    spawned: Spawned,
) -> None:
    """The spawn notes its clock when it gives the message, and Codex names its rollout when
    it starts, which a loaded machine can put either side of it: one within
    ``FIRST_PROMPT_LEAD`` before is the row's, one a quarter minute before is not."""
    assert timedelta(seconds=10) == fleet_service.FIRST_PROMPT_LEAD
    given = spawned.given_at()
    spawned.rollout([MESSAGE], began=given - timedelta(seconds=5))
    spawned.later(46)

    spawned.listed()

    assert spawned.row().first_prompt == "acked"


def test_a_rollout_begun_beyond_the_lead_before_the_spawns_clock_is_not_the_rows(
    spawned: Spawned,
) -> None:
    given = spawned.given_at()
    spawned.rollout([MESSAGE], began=given - timedelta(seconds=25))
    spawned.later(46)

    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_a_rollouts_name_may_be_two_seconds_older_than_the_moment_asked_about(
    tmp_path: Path,
) -> None:
    """Its name holds whole seconds: one begun at 11:59:59.9 is named 11:59:59, a second
    before 12:00:00.0 and still the moment's (``SLACK``)."""
    folder = tmp_path / "work"
    folder.mkdir()
    since = datetime(2026, 10, 26, 12, 0, 0).astimezone()
    assert timedelta(seconds=2) == codex_prompts.SLACK
    for ago, found in ((0, True), (1, True), (2, True), (3, False), (60, False)):
        home = tmp_path / f"home{ago}"
        _rollout(home, folder, ["a prompt"], began=since - timedelta(seconds=ago))

        got = codex_prompts.prompts_since(home, folder, since, PaneProcesses(lambda: None))

        assert (got.prompts == ("a prompt",)) is found, ago


# --- with the agent's hooks not trusted, a later prompt takes the row ---------------------------


def _unsent(spawned: Spawned, instead: str = "something the human typed instead") -> None:
    spawned.rollout([instead])
    spawned.later(46)
    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_a_prompt_the_agent_runs_after_the_row_read_unsent_takes_it(spawned: Spawned) -> None:
    """No hook fires for an agent whose hooks are not trusted, so nothing but its rollout can
    tell a ``fleet tell`` of other words from nothing: the attention's own advice (``tell …``)
    cleared a Claude Code row by its hook and left this one to read ``attention`` for good."""
    _unsent(spawned)
    for _ in range(2):  # the prompt it already ran when the row was read does not clear it
        assert spawned.listed().state == "attention"
    spawned.rollout(
        ["a fleet tell of other words"], began=datetime.now(tz=UTC) + timedelta(seconds=50)
    )

    status = spawned.listed()

    assert status.state != "attention"
    assert spawned.row().first_prompt == "acked"
    with store_session() as store:
        assert store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}") is None
    assert len(spawned.unsent_events()) == 1, "said once, and not said again"


def test_a_row_read_unsent_by_a_build_that_kept_no_record_of_what_it_saw_is_not_cleared(
    spawned: Spawned,
) -> None:
    """Without what the record held when the row was read, a prompt cannot be told from one
    that was there already: the row stays as it was."""
    _unsent(spawned)
    with store_session() as store:
        key = f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}"
        kept = json.loads(store.get_meta(key) or "{}")
        kept.pop("seen")
        store.set_meta(key, json.dumps(kept))
    spawned.rollout(["another prompt"], began=datetime.now(tz=UTC) + timedelta(seconds=50))

    assert spawned.listed().state == "attention"
    assert spawned.row().first_prompt == "unsent"


def test_what_the_record_held_when_the_row_read_unsent_is_kept_as_digests_never_text(
    spawned: Spawned,
) -> None:
    _unsent(spawned, "a zebrasecret thing the human typed")

    with store_session() as store:
        raw = store.get_meta(f"{fleet_service.FIRST_PROMPT_META}{spawned.agent.id}") or ""
    kept = json.loads(raw)

    assert kept["seen"] == [
        fleet_service._first_prompt_digest("a zebrasecret thing the human typed")
    ]
    assert "zebrasecret" not in raw


def test_a_message_noted_late_is_noted_as_of_the_moment_it_was_given() -> None:
    moment = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)

    fleet_service._note_first_prompt("agt_x", "hello", Path("/h"), Path("/c"), at=moment)

    with store_session() as store:
        raw = store.get_meta(f"{fleet_service.FIRST_PROMPT_META}agt_x") or "{}"
    assert datetime.fromisoformat(json.loads(raw)["at"]) == moment


def test_the_message_is_matched_as_the_spawn_wrote_it_whatever_its_whitespace(
    spawned: Spawned,
) -> None:
    spawned.rollout(["  start on tsk_1 and\nread the board   first\n"])
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
    assert (
        fake_registered.first_prompts_since(
            project.root, project.root, datetime.now(), PaneProcesses(lambda: None)
        )
        is None
    )
    moved = datetime.now(tz=UTC) + timedelta(seconds=46)

    row, blind = fleet_service._first_prompt_off_its_transcript(
        stored,
        _Pane(),  # type: ignore[arg-type]
        FleetSettings(),
        moved,
    )

    assert row.first_prompt == "pending"
    assert not blind


class _Pane:
    """A pane that would say idle, if it were asked: it must not be."""

    def captured(self) -> list[str]:
        raise AssertionError("an agent with no record of its prompts is never asked about its pane")

    def pid(self) -> int:
        raise AssertionError("nor about its processes")


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
    big = spawned.rollout([MESSAGE])
    with big.open("r+b") as handle:
        handle.truncate(64 * 1024 * 1024)  # the rest is the working agent's, nothing readable
    spawned.later(46)
    codex_prompts._KEPT.clear()
    codex_prompts._FOLDERS.clear()
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
    path = spawned.rollout(["something else"])
    codex_prompts._KEPT.clear()
    codex_prompts._FOLDERS.clear()
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


def test_several_rollouts_of_this_folder_are_each_read_once_however_many_listings_ask(
    spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache of what a rollout gave holds more than the one: a folder where several agents
    have worked since a long-lived unsent row (tester-24: a ``_KEPT_MAX`` of 1 passed every test
    and opened each of them again at every listing)."""
    paths = [spawned.rollout([f"prompt {n}"]) for n in range(4)]
    codex_prompts._KEPT.clear()
    codex_prompts._FOLDERS.clear()
    reads: list[Path] = []
    real = codex_prompts._read

    def counting(read: Path, folder: str, size: int) -> list[str]:
        reads.append(read)
        return real(read, folder, size)

    monkeypatch.setattr(codex_prompts, "_read", counting)
    spawned.later(46)

    for _ in range(3):
        spawned.listed()

    assert sorted(reads) == sorted(paths), "each read once, however many listings asked"


def test_the_cache_drops_the_rollout_least_recently_asked_about_not_the_oldest(
    spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listings ask about the same few rollouts in turn: one asked about again is kept over one
    that was not, however much older its first reading is."""
    first, second, third = (spawned.rollout([f"prompt {n}"]) for n in range(3))
    folder = os.path.realpath(spawned.cwd)
    codex_prompts._KEPT.clear()
    codex_prompts._FOLDERS.clear()
    monkeypatch.setattr(codex_prompts, "_KEPT_MAX", 2)
    reads: list[Path] = []
    real = codex_prompts._read

    def counting(read: Path, where: str, size: int) -> list[str]:
        reads.append(read)
        return real(read, where, size)

    monkeypatch.setattr(codex_prompts, "_read", counting)

    for path in (first, second, first, third, first):
        codex_prompts._prompts_of(path, folder)

    assert reads == [first, second, third], "the first, asked about again, stayed; the second went"


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
        tmp_path / "home",
        folder,
        datetime.now(tz=UTC) - timedelta(minutes=1),
        PaneProcesses(lambda: None),
    ).prompts

    assert "head prompt" in found and "tail prompt" in found
    assert "middle prompt" not in found


def test_a_busy_home_does_not_thrash_the_cache_of_a_row_waiting_for_days(
    spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More than 256 rollouts of other folders begun since a long-lived unsent row (a shared
    home with other agents at work): the cache of what a rollout gave held 256 and lost the
    row's own to them, so every listing opened all of them again (270 ms at 1,100, tester on
    #17). Which folder a rollout began in never changes: it is read once."""
    for index in range(1_100):
        _rollout(spawned.home, spawned.cwd / f"other-{index}", [f"theirs {index}"])
    mine = spawned.rollout(["something the human typed instead"])
    codex_prompts._KEPT.clear()
    codex_prompts._FOLDERS.clear()
    spawned.later(46)
    assert spawned.listed().state == "attention"  # the first listing reads each first line

    opened: list[str] = []
    real_open = Path.open

    def counting(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.startswith("rollout-"):
            opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    started = os.times()[4]
    for _ in range(3):
        assert spawned.listed().state == "attention"
    elapsed = os.times()[4] - started

    assert opened == [], ("a rollout was opened again", len(opened), mine.name)
    assert elapsed < budget(1.5), f"three listings took {elapsed:.2f} s"


def test_the_folder_a_rollout_began_in_is_read_once_and_a_file_not_yet_written_is_not_kept(
    tmp_path: Path,
) -> None:
    codex_prompts._FOLDERS.clear()
    folder = tmp_path / "work"
    folder.mkdir()
    path = _rollout(tmp_path / "home", folder, ["x"])
    young = path.with_name("rollout-2026-10-26T12-00-00-young.jsonl")
    young.write_bytes(b"")

    assert codex_prompts._folder_of(path) == os.path.realpath(folder)
    path.unlink()  # the answer outlives the file: a session_meta never changes
    assert codex_prompts._folder_of(path) == os.path.realpath(folder)
    assert codex_prompts._folder_of(young) is None
    assert str(young) not in codex_prompts._FOLDERS, "an empty file may still become anyone's"
    young.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(folder)}}) + "\n",
        encoding="utf-8",
    )
    assert codex_prompts._folder_of(young) == os.path.realpath(folder)
