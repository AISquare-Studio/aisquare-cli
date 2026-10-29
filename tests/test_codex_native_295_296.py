"""#295's and #296's gaps for Codex, live, on loopback only (#113's #17).

Run ``AISQUARE_TEST_CODEX=1 pytest tests/test_codex_native_295_296.py``, in the pattern of
``tests/test_codex_native.py`` (its loopback provider, its helpers): a scratch ``CODEX_HOME``,
a throwaway tmux server, no account, login, provider credential or model spend.

- #296 WS-4, the quiet-point compaction of a manager: a Codex manager waiting at its prompt
  with its context over the line is sent a bare ``/compact`` by the worker its ``Stop`` armed;
  Codex compacts (a ``compacted`` record in its rollout, no hook at that moment), the
  rollout's record acknowledges the mark, and the resume line typed after it runs as the
  next turn, in front of which Codex's ``SessionStart`` (source ``compact``) hands the start
  pack back. ``/compact <text>`` typed by hand is a prompt, a turn and no compaction.
- #295 W1a, a first message that never started a turn: Codex has no session before its first
  prompt (and none at all with its hooks untrusted), so the listing asks its rollout. A message
  held behind the hook review that runs once the review is passed reads ``acked``; one that
  never ran, past ``prompt_ack_seconds`` with the pane idle at its empty prompt, reads
  ``attention`` (one ``prompt_unsent`` on the board), and when the human's Enter runs it, the
  rollout says so and the row is ``acked``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents, paths, process_table, selfcli
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet, quiet_compact, team
from tests.test_codex_native import _answer, _message, _user_texts

pytestmark = pytest.mark.skipif(
    os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test"
)

#: Only this test's owned hook definitions are trusted by this flag.
TRUSTED = ["--dangerously-bypass-hook-trust"]
#: What Codex asks its model when it compacts: the checkpoint prompt.
CHECKPOINT = "CONTEXT CHECKPOINT COMPACTION"


@dataclass
class Loopback:
    """A scratch Codex home on a loopback provider that answers every request ``ok``, and the
    throwaway tmux server its agents run in."""

    home: Path
    workspace: Path
    requests: list[dict[str, Any]]
    project: ProjectInfo
    srv: TmuxServer

    def screen(self, pane: str) -> str:
        return self.srv.run("capture-pane", "-p", "-t", pane)

    def until(self, check: Callable[[], bool], seconds: float, what: str, pane: str) -> None:
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() >= deadline:
                raise AssertionError(f"{what}:\n{self.screen(pane)}")
            time.sleep(0.25)

    def ran(self, text: str) -> bool:
        return any(text in _user_texts(request) for request in self.requests)

    def records(self) -> list[dict[str, Any]]:
        """Every record of every rollout under the scratch home, oldest rollout first."""
        found: list[dict[str, Any]] = []
        for path in sorted((self.home / "sessions").rglob("rollout-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    found.append(json.loads(line))
        return found

    def compactions(self) -> int:
        return sum(1 for record in self.records() if record.get("type") == "compacted")

    def type_into(self, pane: str, text: str) -> None:
        """What a human does: the text, then Enter."""
        self.srv.run("send-keys", "-t", pane, "-l", text)
        time.sleep(0.5)
        self.srv.run("send-keys", "-t", pane, "Enter")

    def quiet_at_its_prompt(self, agent: FleetAgent) -> bool:
        lines = [line.strip() for line in self.screen(agent.pane_id).splitlines() if line.strip()]
        return (
            fleet.status_of(agent).state == "waiting"
            and bool(lines)
            and lines[-1].startswith("? for shortcuts")
        )


@pytest.fixture
def loopback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Loopback]:
    assert shutil.which("codex"), "AISQUARE_TEST_CODEX requires an installed Codex"
    home = tmp_path / "codex"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if not self.path.endswith("/responses"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            requests.append(json.loads(raw))
            _answer(self, _message("ok"))

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.setenv("CODEX_HOME", str(home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", home)
        config = load_config()
        config.fleet.tmux_socket = f"asq-codex-295-{os.getpid()}"
        save_config(config)
        project = team.activate(workspace)
        srv = fleet.server(config.fleet)
        try:
            yield Loopback(home, workspace, requests, project, srv)
        finally:
            with contextlib.suppress(Exception):
                srv.run("kill-server")
            server.shutdown()
            thread.join(timeout=2)


def test_real_codex_manager_is_compacted_at_a_quiet_point(loopback: Loopback) -> None:
    """A Codex manager over the line, idle at its prompt, is sent ``/compact`` by the worker
    its ``Stop`` armed; its rollout acknowledges it, and the resume line typed after runs with
    the start pack in front of it. The same command with text after it is a prompt."""
    config = load_config()
    config.fleet.quiet_compact_at = 10  # the loopback reports 19 input tokens a request
    config.fleet.quiet_compact_delay = 1.0
    config.fleet.quiet_compact_resume = True
    save_config(config)
    state = paths.manager_state_file(loopback.project.id)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("goal: ship the quiet compaction\n", encoding="utf-8")

    manager = fleet.spawn(
        loopback.project,
        "manager",
        agent="codex",
        worktree=False,
        prompt="manager fixture",
        agent_args=TRUSTED,
    ).agent

    def resumed() -> bool:
        return loopback.ran(quiet_compact.RESUME_LINE)

    loopback.until(resumed, 90, "the resume line ran after the compaction", manager.pane_id)

    # Codex compacted: it asked its model for the checkpoint, wrote its record, and said so.
    assert any(CHECKPOINT in text for r in loopback.requests for text in _user_texts(r))
    assert loopback.compactions() == 1
    assert "Context compacted" in loopback.screen(manager.pane_id)
    # The bare command, never the instructions Claude Code's takes: they would have been a prompt.
    assert not any(
        text.startswith("/compact") and text != "/compact"
        for request in loopback.requests
        for text in _user_texts(request)
    )
    # The rollout's record acknowledged the mark.
    with store_session() as store:
        row = store.get_fleet_agent(manager.id)
        assert row is not None and row.session_id is not None
        mark = team.read_quiet_mark(store, row.session_id)
    assert mark is not None and mark.acked_at is not None
    # The start pack came back with the next turn, ahead of the line that asked for it.
    after = next(r for r in loopback.requests if quiet_compact.RESUME_LINE in _user_texts(r))
    items = after["input"]
    pack = next(i for i, item in enumerate(items) if "<aisquare-team>" in json.dumps(item))
    line = next(i for i, item in enumerate(items) if quiet_compact.RESUME_LINE in json.dumps(item))
    assert pack < line, "the pack is above the line that says so"
    assert "goal: ship the quiet compaction" in json.dumps(items[pack]), "its state file"

    # By hand, `/compact <text>` is a prompt: a turn, and no second compaction.
    loopback.until(
        lambda: loopback.quiet_at_its_prompt(manager), 30, "the manager idle", manager.pane_id
    )
    loopback.type_into(manager.pane_id, "/compact keep the goal")
    loopback.until(
        lambda: loopback.ran("/compact keep the goal"),
        20,
        "the line ran as a prompt",
        manager.pane_id,
    )
    time.sleep(2)
    assert loopback.compactions() == 1


def test_real_codex_first_message_held_behind_the_hook_review_is_acked_once_it_runs(
    loopback: Loopback,
) -> None:
    """Codex's hooks not trusted: it opens at the hook review with the message held, and no
    hook ever fires, so the row has no session. At the dialog the listing says so and not
    "not taken"; passed with Esc, the message runs, and the rollout takes it."""
    held = "first message held behind the review"
    agent = fleet.spawn(loopback.project, "coder", agent="codex", worktree=False, prompt=held).agent

    def at_the_review() -> bool:
        return "Hooks need review" in loopback.screen(agent.pane_id)

    loopback.until(at_the_review, 30, "the hook review", agent.pane_id)
    [status] = fleet.list_agents(loopback.project)
    assert status.state == "attention" and fleet.AT_A_DIALOG in (status.detail or "")
    assert status.agent.first_prompt == "pending" and not loopback.ran(held)

    loopback.srv.run("send-keys", "-t", agent.pane_id, "Escape")
    loopback.until(lambda: loopback.ran(held), 30, "the held message ran", agent.pane_id)

    def acked() -> bool:
        fleet.list_agents(loopback.project)
        with store_session() as store:
            row = store.get_fleet_agent(agent.id)
        return row is not None and row.first_prompt == "acked"

    loopback.until(acked, 20, "the rollout took the message", agent.pane_id)
    assert fleet.status_of(agent).session is None, "no hook ever fired: this is all it had"


def test_real_codex_first_message_that_never_ran_reads_attention_until_the_humans_enter(
    loopback: Loopback,
) -> None:
    """A message the row was given and Codex never took: the pane is idle at its empty
    prompt past ``prompt_ack_seconds`` and its rollouts hold no such prompt, so the row reads
    ``attention`` with one ``prompt_unsent``; the human's Enter runs it and the row is
    ``acked``. (The loss itself is the row's: nothing in Codex drops a message on demand.)"""
    lost = "a first message that never reached Codex"
    config = load_config()
    config.fleet.prompt_ack_seconds = 3.0
    save_config(config)
    agent = fleet.spawn(
        loopback.project, "coder", agent="codex", worktree=False, agent_args=TRUSTED
    ).agent
    loopback.until(
        lambda: loopback.quiet_at_its_prompt(agent), 30, "a quiet empty prompt", agent.pane_id
    )
    assert fleet.status_of(agent).session is None, "no hook before the first prompt"
    home, cwd = agent_home_and_cwd(loopback, agent)
    with store_session() as store:  # the row as a spawn that gave it a message leaves it
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET first_prompt = 'pending', first_prompt_at = ? WHERE id = ?",
            (fleet._now().isoformat(), agent.id),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    # Noted as of the spawn, as a real spawn notes it. On 0.164.0 Codex makes no rollout until its
    # first prompt runs (none after 33 s at a quiet prompt), so the human's Enter below is what
    # creates it, after the note whichever moment this gives; a Codex that named its rollout when
    # it starts would put it before a note made as of "now", which is what the lead is for.
    fleet._note_first_prompt(agent.id, lost, home, cwd, at=agent.created_at)

    def unsent() -> bool:
        [status] = fleet.list_agents(loopback.project)
        return status.state == "attention"

    loopback.until(unsent, 30, "the message read as not taken", agent.pane_id)
    [status] = fleet.list_agents(loopback.project)
    assert status.agent.first_prompt == "unsent"
    assert status.detail is not None and status.detail.startswith("first prompt not taken")
    with store_session() as store:
        events = store.events_since(loopback.project.id, 0, limit=500)
    assert len([e for e in events if e.kind == "prompt_unsent"]) == 1

    loopback.type_into(agent.pane_id, lost)
    loopback.until(lambda: loopback.ran(lost), 30, "the human's Enter ran it", agent.pane_id)

    def acked() -> bool:
        [now] = fleet.list_agents(loopback.project)
        return now.agent.first_prompt == "acked" and now.state != "attention"

    loopback.until(acked, 20, "the rollout took the message", agent.pane_id)


def agent_home_and_cwd(loopback: Loopback, agent: FleetAgent) -> tuple[Path, Path]:
    """The home the agent records its sessions under and the folder it runs in."""
    return loopback.home, Path(agent.cwd)


def test_real_codex_with_its_hooks_untrusted_a_tell_of_other_words_takes_an_unsent_row(
    loopback: Loopback,
) -> None:
    """The tester's (b) on #17: with Codex's hooks not trusted (reviewed and passed with Esc, so
    none ever fires) the row's attention said ``tell …`` and a tell of other words ran but did
    not clear it, because only the message's own digest did. The rollout is all such a row has,
    and it shows the agent took input after the row read ``unsent``: that clears it."""
    lost = "a first message that never reached Codex"
    config = load_config()
    config.fleet.prompt_ack_seconds = 3.0
    save_config(config)
    agent = fleet.spawn(loopback.project, "coder", agent="codex", worktree=False).agent
    loopback.until(
        lambda: "Hooks need review" in loopback.screen(agent.pane_id),
        30,
        "the hook review",
        agent.pane_id,
    )
    loopback.srv.run("send-keys", "-t", agent.pane_id, "Escape")
    loopback.until(
        lambda: loopback.quiet_at_its_prompt(agent), 30, "a quiet empty prompt", agent.pane_id
    )
    assert fleet.status_of(agent).session is None, "hooks unreviewed: none fires"
    home, cwd = agent_home_and_cwd(loopback, agent)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET first_prompt = 'pending', first_prompt_at = ? WHERE id = ?",
            (fleet._now().isoformat(), agent.id),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    fleet._note_first_prompt(agent.id, lost, home, cwd, at=agent.created_at)

    def unsent() -> bool:
        [status] = fleet.list_agents(loopback.project)
        return status.state == "attention"

    loopback.until(unsent, 30, "the message read as not taken", agent.pane_id)
    other = "do something else instead"
    assert fleet.tell(loopback.project, agent.label, other).delivered
    loopback.until(lambda: loopback.ran(other), 30, "the tell ran", agent.pane_id)

    def cleared() -> bool:
        [now] = fleet.list_agents(loopback.project)
        return now.agent.first_prompt == "acked" and now.state != "attention"

    loopback.until(cleared, 20, "the rollout took the tell", agent.pane_id)
    assert fleet.status_of(agent).session is None, "no hook ever fired: the rollout alone did it"


def test_real_codex_with_its_hooks_untrusted_another_agents_prompt_in_the_folder_is_not_the_rows(
    loopback: Loopback,
) -> None:
    """tester-24's live repro on #24: the row read ``unsent`` (its agent at the empty prompt,
    hooks unreviewed so none ever fires), and ANOTHER Codex agent in the same folder on the same
    home ran its own task. That prompt is in the folder's rollouts, but it is not the row's: its
    agent ran nothing and its message is lost, so the attention stays. The rollout is told
    apart by the process that holds it, which is below the pane's own (``core.process_table``);
    the row's own ``fleet tell`` still clears it."""
    lost = "a first message that never reached Codex"
    theirs = "agent B own task"
    config = load_config()
    config.fleet.prompt_ack_seconds = 3.0
    save_config(config)
    a = fleet.spawn(loopback.project, "coder", agent="codex", worktree=False).agent
    loopback.until(
        lambda: "Hooks need review" in loopback.screen(a.pane_id), 30, "the hook review", a.pane_id
    )
    loopback.srv.run("send-keys", "-t", a.pane_id, "Escape")
    loopback.until(lambda: loopback.quiet_at_its_prompt(a), 30, "a quiet empty prompt", a.pane_id)
    home, cwd = agent_home_and_cwd(loopback, a)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET first_prompt = 'pending', first_prompt_at = ? WHERE id = ?",
            (fleet._now().isoformat(), a.id),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    fleet._note_first_prompt(a.id, lost, home, cwd, at=a.created_at)

    def status_of_a() -> FleetAgentStatus:
        return next(s for s in fleet.list_agents(loopback.project) if s.agent.id == a.id)

    loopback.until(lambda: status_of_a().state == "attention", 30, "A read as unsent", a.pane_id)
    assert status_of_a().agent.first_prompt == "unsent"

    # B: the same folder, the same home, its hooks trusted, its own task.
    b = fleet.spawn(
        loopback.project,
        "coder",
        agent="codex",
        worktree=False,
        agent_args=TRUSTED,
        prompt=theirs,
    ).agent
    assert b.cwd == a.cwd
    loopback.until(lambda: loopback.ran(theirs), 60, "B ran its own task", b.pane_id)

    # Several listings, for as long as a false clear took to show (the first one after B's
    # rollout appeared): A's agent is at its empty prompt and its message has not run.
    for _ in range(6):
        now = status_of_a()
        assert now.state == "attention" and now.agent.first_prompt == "unsent", loopback.screen(
            a.pane_id
        )
        time.sleep(1)
    assert "Ask Codex to do anything" in loopback.screen(a.pane_id)
    assert not loopback.ran(lost)

    pids = {}
    for row in (a, b):
        placed = fleet.resolve_place(row)
        assert isinstance(placed, fleet.Placed)
        pids[row.id] = placed.pane.pid()
    held_a = process_table.held_by_pane(pids[a.id] or 0)
    held_b = process_table.held_by_pane(pids[b.id] or 0)
    rollouts = {os.path.realpath(p) for p in (loopback.home / "sessions").rglob("rollout-*.jsonl")}
    if held_b is not None:  # the process table can be read on this machine
        assert held_a is not None
        assert rollouts and rollouts <= held_b, "B's agent holds the rollout its task made"
        assert not rollouts & held_a, "A's agent holds none: it ran nothing"
    else:  # a machine that hides a process's descriptors: the next test is its rule
        pytest.skip("this machine hides the descriptors of a pane's processes")

    other = "do something else instead"
    assert fleet.tell(loopback.project, a.label, other).delivered
    loopback.until(lambda: loopback.ran(other), 30, "the tell ran", a.pane_id)

    def cleared() -> bool:
        now = status_of_a()
        return now.agent.first_prompt == "acked" and now.state != "attention"

    loopback.until(cleared, 20, "the rollout took the row's own tell", a.pane_id)


def test_real_codex_with_the_process_table_unreadable_an_ended_agents_prompt_is_not_the_rows(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tester-24b's live repro on #24: no ``lsof`` (or a Linux that hides a process's descriptors,
    or an ``lsof`` that hangs), so no rollout can be told from another's. A's message is lost;
    B, in the same folder on the same home, runs its own task, and is then stopped. A is "alone"
    in its folder now, and B's old prompt is in the folder's rollouts and was not among those A
    saw when it read ``unsent``: it must not take A, whose agent ran nothing and idles at its
    empty prompt. Only A's message itself does, and the detail says so."""
    monkeypatch.setattr(process_table, "open_files", lambda pids: None)
    process_table._HELD.clear()
    lost = "a first message that never reached Codex"
    theirs = "agent B own task"
    config = load_config()
    config.fleet.prompt_ack_seconds = 3.0
    save_config(config)
    a = fleet.spawn(loopback.project, "coder", agent="codex", worktree=False).agent
    loopback.until(
        lambda: "Hooks need review" in loopback.screen(a.pane_id), 30, "the hook review", a.pane_id
    )
    loopback.srv.run("send-keys", "-t", a.pane_id, "Escape")
    loopback.until(lambda: loopback.quiet_at_its_prompt(a), 30, "a quiet empty prompt", a.pane_id)
    home, cwd = agent_home_and_cwd(loopback, a)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE fleet_agent SET first_prompt = 'pending', first_prompt_at = ? WHERE id = ?",
            (fleet._now().isoformat(), a.id),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    fleet._note_first_prompt(a.id, lost, home, cwd, at=a.created_at)

    def status_of_a() -> FleetAgentStatus:
        return next(s for s in fleet.list_agents(loopback.project) if s.agent.id == a.id)

    loopback.until(lambda: status_of_a().state == "attention", 30, "A read as unsent", a.pane_id)
    b = fleet.spawn(
        loopback.project,
        "coder",
        agent="codex",
        worktree=False,
        agent_args=TRUSTED,
        prompt=theirs,
    ).agent
    loopback.until(lambda: loopback.ran(theirs), 60, "B ran its own task", b.pane_id)
    for _ in range(3):
        assert status_of_a().state == "attention"
        time.sleep(1)

    fleet.stop(loopback.project, b.label, force=True)
    for _ in range(6):  # B is gone, A is alone: this is where the false clear showed
        now = status_of_a()
        assert now.state == "attention" and now.agent.first_prompt == "unsent", loopback.screen(
            a.pane_id
        )
        time.sleep(1)
    assert "Ask Codex to do anything" in loopback.screen(a.pane_id)
    assert not loopback.ran(lost)
    assert now.detail is not None and fleet.BLIND_DETAIL in now.detail

    other = "do something else instead"  # A's own agent runs a tell of other words
    assert fleet.tell(loopback.project, a.label, other).delivered
    loopback.until(lambda: loopback.ran(other), 30, "the tell ran", a.pane_id)
    for _ in range(4):  # it cannot be told from B's: only the message itself clears this
        assert status_of_a().state == "attention"
        time.sleep(1)

    loopback.until(lambda: loopback.quiet_at_its_prompt(a), 30, "the tell's turn done", a.pane_id)
    assert fleet.tell(loopback.project, a.label, lost).delivered
    loopback.until(lambda: loopback.ran(lost), 30, "the message ran", a.pane_id)

    def cleared() -> bool:
        now = status_of_a()
        return now.agent.first_prompt == "acked" and now.state != "attention"

    loopback.until(cleared, 20, "the message's own words took the row", a.pane_id)
