"""``fleet adopt``: a conversation started by hand, brought under the fleet (#290, phase B).

Owner scope 2026-10-23: the transcript is found under the CLI's known account dirs only,
it resumes on the account it lives in, keeping its session id, and it is refused while a
live claude holds it, when the fleet owns it, and when its directory is no onboarded
project. The picker lists the newest conversations no fleet row owns. Every account here
is under ``fake_home`` (slot 1 is its ``.claude``), and the process scan is stubbed, so
no test reads this machine's sessions or processes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.adopt_picker import adoptable, conversation_text
from aisquare.core import claude_accounts as core
from aisquare.core import launch_seal, process_table
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo
from aisquare.services import adopt as adopt_service
from aisquare.services import agent_events
from aisquare.services import fleet as fleet_service
from tests import test_fleet_service as fleet_suite
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import Codex
from tests.test_fleet_service import FakeTmux, _command, _flag

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

SESSION = "11111111-2222-3333-4444-555555555555"
THREAD = "019a2b3c-4d5e-7f00-8a9b-0c1d2e3f4a5b"
"""A Codex thread id: a UUID (v7), as its rollout's name ends in it."""
CODEX_NATIVE = "/opt/codex/vendor/aarch64-apple-darwin/codex/codex"
"""The image of Codex's native binary, below its npm wrapper (``node …/bin/codex``)."""


@pytest.fixture(autouse=True)
def no_processes(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, str]]:
    """No real `ps`: the processes a test says are running (pid, parent, args), none else."""
    running: list[tuple[int, int, str]] = []
    monkeypatch.setattr(adopt_service, "_process_args", lambda: list(running))
    return running


def _transcript(
    account: ClaudeAccount,
    session: str,
    cwd: Path | str,
    prompt: str = "fix the login bug",
    *,
    age: float = 0.0,
) -> Path:
    """A transcript as Claude Code leaves one, enough of it for what adopt reads."""
    # Claude Code's folder name: every character but a letter or digit is a dash, so a
    # Windows path (C:\Users\…) is one folder too, as the lookup's */*.jsonl expects.
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    path = account.config_dir / "projects" / slug / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [
        {"type": "user", "isMeta": True, "cwd": str(cwd), "message": {"content": "<caveat>"}},
        {
            "type": "user",
            "cwd": str(cwd),
            "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
        },
        {
            "type": "user",
            "cwd": str(cwd),
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "on it"}]}},
    ]
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    stamp = path.stat().st_mtime - age
    os.utime(path, (stamp, stamp))
    return path


def _rollout(
    account: ClaudeAccount,
    thread: str,
    cwd: Path | str,
    prompt: str = "tidy the parser",
    *,
    age: float = 0.0,
) -> Path:
    """A rollout as Codex leaves one, enough of it for what adopt reads: its ``session_meta``,
    Codex's own context as a ``user`` message, the prompt, a reply."""
    path = account.config_dir / "sessions" / "2026" / "10" / "27"
    path = path / f"rollout-2026-10-27T10-00-00-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    def said(role: str, text: str) -> dict[str, object]:
        kind = "input_text" if role == "user" else "output_text"
        content = [{"type": kind, "text": text}]
        return {
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": content},
        }

    records = [
        {
            "type": "session_meta",
            "payload": {"id": thread, "cwd": str(cwd), "cli_version": "0.164.0"},
        },
        said("user", f"<environment_context>\n  <cwd>{cwd}</cwd>\n</environment_context>"),
        said("user", prompt),
        said("assistant", "on it"),
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    stamp = path.stat().st_mtime - age
    os.utime(path, (stamp, stamp))
    return path


# --------------------------------------------------------------------------- adopt


def test_adopting_resumes_on_its_own_account_in_its_own_directory_keeping_its_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The acceptance case: one not held starts a fleet row whose session id is the
    adopted one, on the account its transcript lives in; no second row appears."""
    slot = core.create_account()
    path = _transcript(slot, SESSION, project.root)

    receipt = adopt_service.adopt(SESSION, role="coder")

    agent = receipt.agent
    assert agent.session_id == SESSION
    assert agent.account_slot == slot.slot
    assert _flag(_command(tmux), "--resume") == str(path)
    with store_session() as store:
        rows = store.fleet_agents(project.id, live_only=False)
    assert [row.session_id for row in rows] == [SESSION], "one row, the conversation's own"


def test_a_conversation_a_live_claude_holds_is_refused(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    no_processes: list[tuple[int, int, str]],
) -> None:
    """Two processes would write one conversation: held by Claude Code's own registry, or by
    a process whose arguments name it. Nothing is spawned."""
    slot = core.create_account()
    _transcript(slot, SESSION, project.root)
    registry = slot.config_dir / "sessions" / f"{os.getpid()}.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"pid": os.getpid(), "sessionId": SESSION}))

    with pytest.raises(adopt_service.AdoptRefused) as by_registry:
        adopt_service.adopt(SESSION)
    registry.unlink()
    no_processes.append((4242, 1, f"claude --resume {SESSION}"))
    with pytest.raises(adopt_service.AdoptRefused) as by_args:
        adopt_service.adopt(SESSION)

    assert by_registry.value.code == "held" and str(os.getpid()) in str(by_registry.value)
    assert by_args.value.code == "held" and "4242" in str(by_args.value)
    assert tmux.spawned == []
    # A registry entry whose process is gone holds nothing.
    no_processes.clear()
    registry.write_text(json.dumps({"pid": 999_999_999, "sessionId": SESSION}))
    assert adopt_service.adopt(SESSION).agent.session_id == SESSION


def test_the_adopting_command_and_its_own_shell_and_claude_hold_nothing(tmp_path: Path) -> None:
    """Review of #370: run from an agent, adopt's parent is Claude Code's Bash wrapper
    (``zsh -c source ~/.claude/shell-snapshots/… && eval 'aisquare fleet adopt <id>'``),
    whose arguments hold 'claude' and the id: every adopt was refused 'held'. Only a claude
    program given the session by a session flag holds it, and never one above this command."""
    me = os.getpid()
    transcript = tmp_path / f"{SESSION}.jsonl"
    above = [
        (me, 500, f"python -m aisquare fleet adopt {SESSION}"),
        (
            500,
            400,
            "zsh -c source ~/.claude/shell-snapshots/s.sh && "
            f"eval 'aisquare fleet adopt {SESSION}'",
        ),
        (400, 1, "/usr/local/bin/claude --session-id 99999999-aaaa"),
        (900, 1, f"grep {SESSION} claude.log"),  # names the id, is no claude given it
    ]
    assert adopt_service.holder(SESSION, transcript, [], processes=above) is None
    elsewhere = [*above, (901, 1, f"/usr/local/bin/claude --resume={transcript}")]
    assert adopt_service.holder(SESSION, transcript, [], processes=elsewhere) == 901
    an_ancestor = [(me, 400, "aisquare fleet adopt"), (400, 1, f"claude --resume {SESSION}")]
    assert adopt_service.holder(SESSION, transcript, [], processes=an_ancestor) is None


def test_the_listed_short_id_adopts_and_a_glob_or_an_ambiguous_start_is_refused(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #370: --list shows eight characters, which find() could not take, and a
    ref went into glob unescaped ('*' matched every transcript)."""
    slot = core.create_account()
    _transcript(slot, SESSION, project.root)
    _transcript(slot, "abcdef00-1111-2222-3333-444444444444", project.root)
    _transcript(slot, "abcdef00-5555-6666-7777-888888888888", project.root)

    assert adopt_service.adopt(SESSION[:8]).agent.session_id == SESSION
    with pytest.raises(adopt_service.AdoptRefused) as starred:
        adopt_service.adopt("*")
    with pytest.raises(adopt_service.AdoptRefused) as ambiguous:
        adopt_service.adopt("abcdef00")
    assert starred.value.code == "no_such_conversation"
    assert ambiguous.value.code == "ambiguous" and "abcdef00-1111" in str(ambiguous.value)


def test_a_directory_no_project_holds_is_refused_with_how_to_onboard_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _transcript(core.create_account(), SESSION, elsewhere)
    with pytest.raises(adopt_service.AdoptRefused) as refused:
        adopt_service.adopt(SESSION)
    assert refused.value.code == "not_onboarded"
    assert f"aisquare project onboard {elsewhere}" in str(refused.value)
    assert tmux.spawned == []


def test_a_conversation_the_fleet_owns_is_refused(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    _transcript(core.create_account(), SESSION, project.root)
    first = adopt_service.adopt(SESSION).agent
    with pytest.raises(adopt_service.AdoptRefused) as refused:
        adopt_service.adopt(SESSION)
    assert refused.value.code == "owned_by_the_fleet" and first.label in str(refused.value)


def test_only_the_known_accounts_are_searched(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """A transcript in a directory the CLI did not create (a hand-made ~/.claude2) is not
    adopted: phase A, adopting such directories, is out (owner, 2026-10-23)."""
    foreign = ClaudeAccount(slot=9, config_dir=tmp_path / ".claude2", managed=False)
    path = _transcript(foreign, SESSION, project.root)
    with pytest.raises(adopt_service.AdoptRefused) as by_id:
        adopt_service.adopt(SESSION)
    with pytest.raises(adopt_service.AdoptRefused) as by_path:
        adopt_service.adopt(str(path))
    assert by_id.value.code == "no_such_conversation"
    assert by_path.value.code == "not_on_a_known_account"


def test_reap_leaves_the_owners_tree_an_adopted_row_ran_in_though_its_store_says_tree(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """An adopted conversation resumes in its own directory, here the owner's worktree, and
    its row has no tree of its own: its launch record says ``worktree`` False. Whether a row
    has a tree is its record's to say, as where it is (seq 4207, MD): with the store's
    ``worktree`` column rewritten to 1, the ended row's reap leaves the owner's tree."""
    owners = project.root / "owners-tree"
    git = ["git", "-C", str(project.root)]
    subprocess.run([*git, "worktree", "add", "-q", "-b", "owners", str(owners)], check=True)
    _transcript(core.create_account(), SESSION, owners, age=5)
    agent = adopt_service.adopt(SESSION).agent
    assert not agent.worktree and agent.cwd.resolve() == owners.resolve(), "the fixture"
    fleet_service.stop(project, agent.label, force=True)
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE fleet_agent SET worktree = 1 WHERE id = ?", (agent.id,))
        store._conn.commit()

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and owners.is_dir(), "the owner's tree is no row's"


# ------------------------------------------------------------- Codex (SCOPE #113 091, 13)


def test_a_hand_started_codex_thread_adopts_into_a_codex_row_that_resumes_it(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
    no_processes: list[tuple[int, int, str]],
) -> None:
    """The acceptance case of SCOPE #113 091's item 13: a ``codex resume <thread>`` in the
    process list holds the thread (its adapter's ``adoptable`` reads the arguments), and once
    it is gone the thread adopts into a fleet row whose launch record says codex, resumed by
    ``codex resume <thread>`` on the Codex slot whose home holds its rollout, in its own
    directory. The row is on the board id its hooks will bring, so no second session appears."""
    slot = core.create_account("codex")
    rollout = _rollout(slot, THREAD, project.root)
    no_processes.append((5150, 1, f"node /usr/local/bin/codex resume {THREAD}"))
    no_processes.append((5151, 5150, f"{CODEX_NATIVE} resume {THREAD}"))

    with pytest.raises(adopt_service.AdoptRefused) as held:
        adopt_service.adopt(THREAD)
    assert held.value.code == "held" and "pid 5150" in str(held.value)
    assert "Codex" in str(held.value) and tmux.spawned == []

    no_processes.clear()
    receipt = adopt_service.adopt(THREAD[:8], role="coder")

    agent = receipt.agent
    board_id = agent_events.session_key(get_adapter("codex"), slot.config_dir, THREAD)
    assert (agent.agent, agent.session_id, agent.account_slot) == ("codex", board_id, slot.slot)
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord)
    assert record.agent == "codex" and record.account_slot == slot.slot
    assert Path(record.cwd).resolve() == project.root.resolve()
    native = _command(tmux)[_command(tmux).index("--") + 1 :]
    at = native.index("resume")
    assert native[at : at + 2] == ["resume", THREAD], "the thread by its own id"
    assert "--resume" not in _command(tmux), "no Claude Code flag"
    with store_session() as store:
        rows = store.fleet_agents(project.id, live_only=False)
    assert [row.session_id for row in rows] == [board_id], "one row, the thread's own"
    # The board id is the one the thread's own hooks key it by: they join this row.
    assert rollout.is_file()
    with pytest.raises(adopt_service.AdoptRefused) as owned:
        adopt_service.adopt(THREAD)
    assert owned.value.code == "owned_by_the_fleet"


def test_a_codex_started_fresh_holds_the_rollout_it_has_open(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
    no_processes: list[tuple[int, int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``codex`` started with no ``resume`` names no thread, but its native binary holds
    its rollout open for as long as the session lives (``AgentTranscript.held_open``,
    ``core.process_table``): it is held. Only Codex's processes are asked, and a table that
    cannot be read says nothing."""
    slot = core.create_account("codex")
    rollout = _rollout(slot, THREAD, project.root)
    no_processes.append((6000, 1, "/bin/zsh"))
    no_processes.append((6001, 6000, "node /usr/local/bin/codex"))
    no_processes.append((6002, 6001, CODEX_NATIVE))
    no_processes.append((6003, 6000, "/usr/local/bin/claude --resume elsewhere"))
    asked: list[list[int]] = []
    holds = {6002: frozenset({os.path.realpath(rollout)})}

    def open_files(pids: list[int]) -> frozenset[str] | None:
        asked.append(sorted(pids))
        return frozenset().union(*(holds.get(pid, frozenset()) for pid in pids))

    monkeypatch.setattr(process_table, "open_files", open_files)

    with pytest.raises(adopt_service.AdoptRefused) as held:
        adopt_service.adopt(THREAD)
    assert held.value.code == "held" and "pid 6002" in str(held.value)
    assert asked[0] == [6001, 6002], "Codex's processes alone"

    [listed] = adopt_service.recent()
    assert listed.held_by == 6002 and not adoptable(listed)

    monkeypatch.setattr(process_table, "open_files", lambda pids: None)
    assert adopt_service.adopt(THREAD).agent.agent == "codex"


#: Codex 0.164.0's managed app-server daemon, as ``ps`` lists it (SMOKE11, 5A, and #47's reopen,
#: seq 4382): a ``codex`` with no arguments starts these two and runs its thread in the first,
#: and both live on for minutes after that ``codex`` quit, the first holding its rollout open.
CODEX_DAEMON_BIN = (
    "/home/dev/.codex/packages/app-server-daemon/releases/0.164.0-aarch64-apple-darwin/bin/codex"
)
CODEX_DAEMON = (
    f"{CODEX_DAEMON_BIN} app-server --listen unix:// --analytics-default-enabled --managed-daemon"
)
CODEX_DAEMON_LOOP = f"{CODEX_DAEMON_BIN} app-server daemon pid-update-loop"
#: Processes whose arguments mention codex and run none: another Claude Code session's Bash
#: (seq 4382 named one, pid 392) and the fleet's tmux server for a Codex spawn.
ZSH_NAMING_CODEX = (
    "/bin/zsh -c source /home/dev/.claude/shell-snapshots/snapshot-zsh.sh && "
    "eval aisquare fleet spawn coder --agent codex && which codex < /dev/null"
)
TMUX_NAMING_CODEX = (
    "tmux -L asq new-session -d -s asq-coder -c /repo -- "
    "/usr/local/bin/aisquare agent run --agent codex -- codex -c model=gpt-5.5"
)


@pytest.fixture
def codex_daemon(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
    no_processes: list[tuple[int, int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, int, str]]:
    """A thread a no-argument ``codex`` ran, that ``codex`` quit: Codex's two daemon processes
    left, reparented to 1, the ``--managed-daemon`` one (13090) alone holding the rollout
    (``lsof`` in seq 4382), the ``pid-update-loop`` (13100) none."""
    slot = core.create_account("codex")
    rollout = _rollout(slot, THREAD, project.root)
    no_processes.append((13090, 1, CODEX_DAEMON))
    no_processes.append((13100, 1, CODEX_DAEMON_LOOP))
    holds = {13090: frozenset({os.path.realpath(rollout)})}

    def open_files(pids: list[int]) -> frozenset[str] | None:
        return frozenset().union(*(holds.get(pid, frozenset()) for pid in pids))

    monkeypatch.setattr(process_table, "open_files", open_files)
    return no_processes


def test_codexs_two_daemon_processes_alone_hold_no_thread(
    codex_daemon: list[tuple[int, int, str]],
) -> None:
    """#47's reopen: the daemon's ``pid-update-loop`` was taken for a client, so adopt refused
    for as long as the daemon lived, naming it. Both of the daemon's processes are the daemon:
    with no client of Codex's running, the thread is adoptable."""
    [listed] = adopt_service.recent()
    assert listed.held_by is None and adoptable(listed)
    assert adopt_service.adopt(THREAD).agent.agent == "codex"


def test_codexs_daemon_holds_a_thread_while_a_codex_tui_runs_which_is_named(
    codex_daemon: list[tuple[int, int, str]],
) -> None:
    """SMOKE11, 5A: with a TUI of Codex's running too (its npm wrapper and the native binary
    below it), the daemon's hold counts and the TUI is named, the process to quit, never the
    daemon; so too with a TUI on another thread, which the table cannot tell apart."""
    codex_daemon.append((7001, 1, "/bin/zsh"))
    codex_daemon.append((7002, 7001, "node /usr/local/bin/codex"))
    codex_daemon.append((7003, 7002, CODEX_NATIVE))

    with pytest.raises(adopt_service.AdoptRefused) as held:
        adopt_service.adopt(THREAD)
    assert held.value.code == "held" and "pid 7002" in str(held.value), "the TUI, not the daemon"
    [listed] = adopt_service.recent()
    assert listed.held_by == 7002 and not adoptable(listed)

    del codex_daemon[2:]  # the TUI quit: the daemon alone has the rollout open
    [listed] = adopt_service.recent()
    assert listed.held_by is None and adoptable(listed)
    assert adopt_service.adopt(THREAD).agent.agent == "codex"


def test_a_process_that_only_mentions_codex_is_no_client_of_its_daemon(
    codex_daemon: list[tuple[int, int, str]],
) -> None:
    """#47's reopen: a client was any process with a word naming codex, so a shell of another
    session's (``zsh -c "… --agent codex"``) or the fleet's tmux server for a Codex spawn
    made the daemon's hold count, and adopt named that unrelated process. A client is told by
    what it runs: with neither run by Codex, the thread is adoptable and neither is named."""
    codex_daemon.append((392, 1, ZSH_NAMING_CODEX))
    codex_daemon.append((8000, 1, TMUX_NAMING_CODEX))

    [listed] = adopt_service.recent()
    assert listed.held_by is None and adoptable(listed)
    assert adopt_service.adopt(THREAD).agent.agent == "codex"


def test_a_codex_process_says_how_it_holds_what_it_writes() -> None:
    """``held_open``: every process of Codex's daemon for its clients, its TUI and ``exec``
    (the native binary or its npm wrapper) for their own session; a process that runs no
    Codex, whatever its arguments say, none; Claude Code's none."""
    reader = get_adapter("codex").transcript_reader
    claude = get_adapter("claude-code").transcript_reader
    assert reader is not None and claude is not None
    assert reader.held_open(CODEX_DAEMON.split()) == "clients"
    assert reader.held_open(CODEX_DAEMON_LOOP.split()) == "clients"
    assert reader.held_open([CODEX_DAEMON_BIN, "app-server"]) == "clients", "run from its dir"
    assert reader.held_open(f"{CODEX_NATIVE} app-server daemon stop".split()) == "clients"
    assert reader.held_open(f"{CODEX_NATIVE} app-server --managed-daemon".split()) == "clients"
    assert reader.held_open(f"{CODEX_NATIVE} app-server --listen stdio://".split()) == "session"
    assert reader.held_open(f"{CODEX_NATIVE} exec --json daemon".split()) == "session"
    assert reader.held_open([CODEX_NATIVE]) == "session"
    assert reader.held_open(["node", "/usr/local/bin/codex"]) == "session"
    assert reader.held_open(["node", "--no-warnings", "/usr/local/bin/codex", "exec"]) == "session"
    assert reader.held_open(ZSH_NAMING_CODEX.split()) is None
    assert reader.held_open(TMUX_NAMING_CODEX.split()) is None
    assert reader.held_open(["node", "/usr/local/bin/claude", "codex"]) is None
    assert reader.held_open([]) is None
    assert claude.held_open(["claude"]) is None


def test_a_stopped_fleet_codex_rows_thread_is_the_fleets_and_never_listed(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMOKE11, 5B: a plain-spawned Codex row's session landed only in ``fleet-session:<id>``,
    its column NULL, and ``fleet_session_ids`` read the column alone: ``fleet adopt --list``
    listed the row's own thread, and once stopped it was adopted into a second row. The
    landing writes the column, as a Claude row's spawn does, and a row from before it (its
    column empty) is read by its binding: refused as a stopped Claude row's is."""
    slot = core.create_account("codex")
    receipt = fleet_service.spawn(
        project, "coder", agent="codex", account=str(slot.slot), worktree=False
    )
    codex = Codex(receipt.agent, project, slot.config_dir, tmux, monkeypatch)
    codex.start(THREAD)
    _rollout(slot, THREAD, project.root)
    board_id = codex.board_id(THREAD)

    def column() -> str | None:
        with store_session() as store:
            assert isinstance(store, SqliteStore)
            row = store._conn.execute(
                "SELECT session_id FROM fleet_agent WHERE id = ?", (receipt.agent.id,)
            ).fetchone()
        return None if row[0] is None else str(row[0])

    assert column() == board_id, "the session landed in the row's own column"
    assert adopt_service.recent() == []
    fleet_service.stop(project, receipt.agent.label, force=True)
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn.execute(
            "UPDATE fleet_agent SET session_id = NULL WHERE id = ?", (receipt.agent.id,)
        )
        store._conn.commit()
    assert column() is None, "a row from before the landing wrote the column"

    assert adopt_service.recent() == []
    with pytest.raises(adopt_service.AdoptRefused) as refused:
        adopt_service.adopt(THREAD, label="stolen")
    assert refused.value.code == "owned_by_the_fleet"
    assert "was a fleet agent's: restart that agent" in str(refused.value)


def test_a_codex_row_recorded_after_its_session_landed_takes_it_into_its_column(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    """The window opens before the spawn records the row, so the agent's start may bind the
    launch first: the row's insert takes the binding, as the landing would have written it."""
    with store_session() as store:
        store.set_meta("fleet-session:row-late", "the-thread")
        stored = store.upsert_fleet_agent(
            FleetAgent(
                id="row-late",
                project_id=project.id,
                label="late",
                role="coder",
                binary="codex",
                tmux_socket="fleet",
                pane_id="%9",
                session_id=None,
                cwd=project.root,
                worktree=False,
                spawned_by="test",
                created_at=datetime.now(tz=UTC),
                agent="codex",
            )
        )
        assert isinstance(store, SqliteStore)
        raw = store._conn.execute(
            "SELECT session_id FROM fleet_agent WHERE id = 'row-late'"
        ).fetchone()
    assert stored.session_id == raw[0] == "the-thread"


def test_the_list_holds_both_agents_conversations_and_the_picker_names_each(
    tmux: FakeTmux,
    claude_on_path: Path,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
) -> None:
    """``fleet adopt --list`` and the picker read every agent's homes through its transcript
    reader: a Codex thread beside a Claude Code session, each with its first prompt (Codex's
    own context left out), its agent named on the row."""
    claude = core.create_account()
    codex = core.create_account("codex")
    _transcript(claude, SESSION, project.root, "fix the login bug", age=20)
    _rollout(codex, THREAD, project.root, "tidy the parser", age=10)

    listed = adopt_service.recent()

    assert [(c.agent, c.session_id) for c in listed] == [
        ("codex", THREAD),
        ("claude-code", SESSION),
    ]
    thread = listed[0]
    assert thread.first_prompt == "tidy the parser"
    assert (thread.slot, thread.project_id, thread.cwd) == (
        codex.slot,
        project.id,
        str(project.root),
    )
    assert adoptable(thread)
    row = conversation_text(thread, thread.last_activity).plain
    assert row.startswith(f"{THREAD[:8]}  Codex  ") and "tidy the parser" in row


def test_a_codex_thread_the_fleet_owns_is_left_out_of_the_list(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
) -> None:
    """The list leaves out a Codex thread a fleet row owns by the board id the row holds
    (``agent_events.session_key``), never by the thread's own id, which no row names: read by
    that, an adopted thread was offered to adopt again (seq 4186, M5)."""
    slot = core.create_account("codex")
    _rollout(slot, THREAD, project.root)
    assert [c.session_id for c in adopt_service.recent()] == [THREAD], "the fixture"

    adopt_service.adopt(THREAD)

    assert adopt_service.recent() == [], "owned by the fleet now"


def test_a_codex_process_names_the_thread_it_resumes_and_no_other() -> None:
    """``AgentAdapter.adoptable``: the thread after ``resume`` past the options and their
    values, ``exec resume`` too; a fresh start, ``resume --last``, a ``fork``, a prompt after
    ``--`` and another program name none. A word that only mentions the id is no holder,
    as for Claude Code (review of #370); one that reads as a holder only refuses."""
    codex = get_adapter("codex")
    named = {
        f"codex resume {THREAD}": THREAD,
        f"node /usr/local/bin/codex -m gpt-5 resume -c x=1 {THREAD} -- go on": THREAD,
        f"{CODEX_NATIVE} --no-alt-screen exec resume {THREAD}": THREAD,
        "codex": None,
        "codex resume": None,
        "codex resume --last": None,
        f"codex fork {THREAD}": None,
        f"codex -- resume {THREAD}": None,
        f"grep {THREAD} codex.log": None,
        f"claude --resume {THREAD}": None,
    }
    assert {args: codex.adoptable(args.split()) for args in named} == named


def test_a_claude_process_names_the_session_its_flags_give_and_no_other_program_does() -> None:
    """Claude Code's ``adoptable``: a word running ``claude`` and a session flag of its
    ``session_flags`` with its value (``--flag value`` or ``--flag=value``, the id or the
    transcript's path). Another program given the same flag, a bare ``--resume`` (its
    picker) and a fresh start name none."""
    claude = get_adapter("claude-code")
    path = f"/home/dev/.claude/projects/-w/{SESSION}.jsonl"
    named = {
        f"claude --resume {SESSION}": SESSION,
        f"/usr/local/bin/claude -r {SESSION}": SESSION,
        f"node /opt/claude/cli.js claude --resume={path}": path,
        f"claude --model opus --session-id {SESSION}": SESSION,
        "claude": None,
        "claude --resume": None,
        "claude --resume --model opus": None,
        f"aider --resume {SESSION}": None,
        f"codex resume {SESSION}": None,
    }
    assert {args: claude.adoptable(args.split()) for args in named} == named


# --------------------------------------------------------------------------- the picker


def test_the_list_is_newest_first_limited_and_leaves_out_what_the_fleet_owns(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    no_processes: list[tuple[int, int, str]],
) -> None:
    plain, second = core.default_account(), core.create_account()
    owned = "00000000-0000-0000-0000-00000000000a"
    _transcript(second, owned, project.root, age=10)
    adopt_service.adopt(owned)
    _transcript(plain, "00000000-0000-0000-0000-00000000000b", project.root, "the newest", age=20)
    _transcript(second, "00000000-0000-0000-0000-00000000000c", "/nowhere", "x" * 200, age=30)
    _transcript(plain, "00000000-0000-0000-0000-00000000000d", project.root, "oldest", age=40)
    no_processes.append((77, 1, "claude --resume 00000000-0000-0000-0000-00000000000c"))

    listed = adopt_service.recent(limit=2)

    assert [c.session_id[-1] for c in listed] == ["b", "c"], "newest first, the owned one out"
    newest, held = listed
    assert newest.first_prompt == "the newest", "the first thing typed, not a command or meta"
    assert (newest.slot, newest.project_id) == (plain.slot, project.id)
    assert held.held_by == 77 and held.project_id is None
    assert held.first_prompt is not None and len(held.first_prompt) == adopt_service.PROMPT_CHARS


def test_the_cli_lists_as_json_and_adopts(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, runner: CliRunner
) -> None:
    _transcript(core.create_account(), SESSION, project.root)

    listed = runner.invoke(app, ["--json", "fleet", "adopt", "--list", "--limit", "5"])
    assert listed.exit_code == 0, listed.output
    assert [row["session_id"] for row in json.loads(listed.stdout)] == [SESSION]

    adopted = runner.invoke(app, ["--json", "fleet", "adopt", SESSION, "--label", "coder-login"])
    assert adopted.exit_code == 0, adopted.output
    agent = json.loads(adopted.stdout)["agent"]
    assert (agent["session_id"], agent["label"]) == (SESSION, "coder-login")

    again = runner.invoke(app, ["--json", "fleet", "adopt", SESSION])
    assert again.exit_code == 1
    assert json.loads(again.stdout)["error"] == "owned_by_the_fleet"


def test_the_cli_list_names_each_conversations_agent(
    tmux: FakeTmux,
    claude_on_path: Path,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
    runner: CliRunner,
) -> None:
    """``fleet adopt --list`` names the agent of each row, as the picker does: a Codex thread
    beside a Claude Code session (seq 4186, M14)."""
    _transcript(core.create_account(), SESSION, project.root, age=20)
    _rollout(core.create_account("codex"), THREAD, project.root, age=10)

    listed = runner.invoke(app, ["fleet", "adopt", "--list"])

    assert listed.exit_code == 0, listed.output
    assert f"{THREAD[:8]}  codex  " in listed.stdout, listed.stdout
    assert f"{SESSION[:8]}  claude-code  " in listed.stdout, listed.stdout


def test_the_cli_list_names_no_agent_when_one_agent_is_listed(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, runner: CliRunner
) -> None:
    """With one agent's conversations listed (Claude Code's alone, on a machine without
    Codex), ``fleet adopt --list`` has no agent column: it tells no rows apart (review r28 of
    #113, n5)."""
    _transcript(core.create_account(), SESSION, project.root)
    (row,) = adopt_service.recent()

    listed = runner.invoke(app, ["fleet", "adopt", "--list"])

    assert listed.exit_code == 0, listed.output
    assert f"{SESSION[:8]}  {row.account}  " in listed.stdout, listed.stdout
    assert "claude-code" not in listed.stdout, listed.stdout
