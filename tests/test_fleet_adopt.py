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
from collections.abc import Iterable
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


@pytest.fixture(autouse=True)
def daemon_sockets(monkeypatch: pytest.MonkeyPatch) -> dict[int, set[int]]:
    """Which processes ``lsof -U`` says are connected to each server's socket
    (``process_table.connected_to``): none told for a server a test names none for, as on a
    machine whose sockets cannot say (``/proc``), and never this machine's."""
    told: dict[int, set[int]] = {}

    def connected_to(server: int, pids: Iterable[int]) -> frozenset[int] | None:
        return frozenset(told[server]) & frozenset(pids) if server in told else None

    monkeypatch.setattr(process_table, "connected_to", connected_to)
    return told


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
#: And two whose word right after their first ``--`` is codex (tester-47b's decoy 1741 live,
#: #47's K4): a tmux server running the fleet's runner, and a human's own tmux starting one.
TMUX_RUNNING_CODEX = (
    "tmux -L asq new-session -d -s asq-coder -c /repo "
    "/usr/local/bin/aisquare agent run --agent codex -- codex -c model=gpt-5.5"
)
TMUX_STARTING_CODEX = "tmux new-session -s work -- codex"
#: A Codex fleet agent's TUI as ``ps`` lists it (#50, coder-1 live): run with ``-c`` overrides,
#: so embedded ("Running without the shared background server: command-line configuration
#: overrides (-c, --enable, --disable, or --search) requires embedded mode", 0.164.0), its
#: own process holding its rollout, never the daemon's client.
CODEX_EMBEDDED_ARGS = (
    "-c mcp_servers.aisquare.command=/usr/local/bin/aisquare -c mcp_servers.aisquare.args=[] "
    "--approve-for-me -c developer_instructions=You are the coder"
)

#: Prompts as ``ps`` lists a ``codex '<prompt>'`` (tester-50's, #50's reopen): words that are
#: Codex's options or subcommands, which are the prompt's and never read as either. Live,
#: ``codex 'use git log -p to explain the last commit'`` ran on the daemon, its client.
PROMPTS = (
    "use git log -p to explain the last commit",
    "add a -c flag",
    "exec the tests",
    "run make -check",
    "fix the exec path",
)
#: Codex processes that run no thread of the daemon's, as ``ps`` lists them (r31): an
#: ``app-server`` of its own (VS Code's extension runs this one; r31's probe named it, pid
#: 9100), one listening on a websocket, the standalone exec-server, a ``review``, and a
#: ``login`` waiting on its browser. Measured on 0.164.0 with the daemon holding a thread:
#: none was connected to its socket (``lsof -U``), the ``review`` holding its own rollouts.
NO_CLIENTS = (
    "app-server --analytics-default-enabled",
    "app-server",
    "app-server --listen ws://127.0.0.1:4500",
    "exec-server",
    "review",
    "login",
)
#: Codex 0.164.0's subcommands its ``--help`` hides (tester-50-51, #51's reopen), as ``ps``
#: lists them: the alias ``cloud-tasks`` (``codex help cloud-tasks`` says ``Usage: codex
#: cloud``), and ``execpolicy`` and ``tcp-tunnel``, which ``codex completion bash`` lists.
#: Each was read as a prompt's first word, a TUI's.
HIDDEN_COMMANDS = (
    "cloud-tasks",
    "execpolicy check --rules /r.rules",
    "tcp-tunnel --proxy-url https://p --proxy-origins-file /o --target 127.0.0.1:9",
)


DaemonTable = tuple[list[tuple[int, int, str]], dict[int, frozenset[str]]]
"""The processes running (pid, parent, args) and what each holds open, as ``lsof`` says."""


@pytest.fixture
def codex_daemon(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,
    no_processes: list[tuple[int, int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> DaemonTable:
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
    return no_processes, holds


def _refused(pid: int) -> str:
    """``fleet adopt``'s refusal of the thread, which names ``pid`` as its holder."""
    with pytest.raises(adopt_service.AdoptRefused) as held:
        adopt_service.adopt(THREAD)
    assert held.value.code == "held" and f"(pid {pid})" in str(held.value)
    [listed] = adopt_service.recent()
    assert listed.held_by == pid and not adoptable(listed)
    return str(held.value)


def test_codexs_daemon_holding_a_thread_after_its_tui_quit_is_named_and_says_how_it_lets_go(
    codex_daemon: DaemonTable,
) -> None:
    """#47's reopen, the manager's rule: Codex resumes no thread its managed daemon has open
    (an adopted ``codex resume`` sat on "This conversation is open in another app", 0.164.0),
    so while the ``--managed-daemon`` process holds the rollout, adopt refuses, naming it,
    and says it is Codex's background app-server, that it lets go by itself and how to stop
    it. The ``pid-update-loop``, which holds nothing and was named before, never is."""
    processes, _holds = codex_daemon
    said = _refused(13090)
    assert "13100" not in said
    assert "Codex's background app-server" in said and "lets go of it by itself" in said
    [listed] = adopt_service.recent()
    stop = f"`aisquare accounts run {listed.slot} app-server daemon stop`"
    assert stop in said, "Codex's own stop, on the thread's account"
    assert "quit it first" not in said

    processes.reverse()  # the loop listed first: the one that holds the rollout is named
    _refused(13090)


def test_codexs_daemon_holds_a_thread_while_a_codex_tui_runs_which_is_named(
    codex_daemon: DaemonTable,
) -> None:
    """SMOKE11, 5A: with a TUI of Codex's running too (its npm wrapper and the native binary
    below it), the TUI is named, the process to quit, before the daemon; so too with a TUI
    on another thread, which the table cannot tell apart. Once it quit, the daemon is."""
    processes, _holds = codex_daemon
    processes.append((7001, 1, "/bin/zsh"))
    processes.append((7002, 7001, "node /usr/local/bin/codex"))
    processes.append((7003, 7002, CODEX_NATIVE))

    assert "quit it first" in _refused(7002), "the TUI, not the daemon"

    del processes[2:]  # the TUI quit: the daemon alone has the rollout open
    _refused(13090)


def test_a_process_that_only_mentions_codex_is_never_named_for_its_daemon(
    codex_daemon: DaemonTable,
) -> None:
    """#47's reopen: a client was any process with a word naming codex, so adopt named a
    shell of another session's (``zsh -c "… --agent codex"``, pid 392 live) or the fleet's
    tmux server for a Codex spawn. A client is told by what it runs: with neither run by
    Codex, the daemon holding the rollout is named, never they."""
    processes, _holds = codex_daemon
    processes.append((392, 1, ZSH_NAMING_CODEX))
    processes.append((8000, 1, TMUX_NAMING_CODEX))
    processes.append((8001, 1, TMUX_RUNNING_CODEX))
    processes.append((8002, 1, TMUX_STARTING_CODEX))

    said = _refused(13090)
    assert not {"392", "8000", "8001", "8002"} & set(re.findall(r"\d+", said))


def _says_the_daemon(said: str) -> None:
    """The refusal of the daemon itself (:func:`_refused` named it): what it is, how it lets
    go and Codex's own stop on the thread's account, never "quit it"."""
    [listed] = adopt_service.recent()
    assert "Codex's background app-server" in said and "lets go of it by itself" in said
    assert f"`aisquare accounts run {listed.slot} app-server daemon stop`" in said
    assert "quit it first" not in said


def test_a_codex_that_runs_embedded_is_never_named_as_its_daemons_client(
    codex_daemon: DaemonTable,
) -> None:
    """#50: with a Codex fleet agent running (``-c`` overrides, so embedded: its own process
    holds its own rollout, no daemon socket), a thread the daemon held was refused naming the
    agent, "quit it first", and a human who obeyed quit a working agent for nothing. One that
    runs embedded is no client of the daemon's: the daemon is named, with its note and stop."""
    processes, _holds = codex_daemon
    processes.append((7101, 1, f"node /usr/local/bin/codex {CODEX_EMBEDDED_ARGS}"))
    processes.append((7102, 7101, f"{CODEX_NATIVE} {CODEX_EMBEDDED_ARGS}"))
    processes.append((7103, 1, f"{CODEX_NATIVE} exec --json"))  # its prompt on stdin

    _says_the_daemon(_refused(13090))

    processes.append((7001, 1, "node /usr/local/bin/codex"))  # a TUI on the daemon: its client
    assert "quit it first" in _refused(7001), "a plain TUI is still named, embedded ones listed"


def test_a_codex_that_runs_embedded_and_holds_the_thread_itself_is_named(
    codex_daemon: DaemonTable,
) -> None:
    """An embedded Codex that does hold the thread (``lsof`` says so) is its holder, the one to
    quit, the daemon holding it too or not."""
    processes, holds = codex_daemon
    processes.append((7101, 1, f"node /usr/local/bin/codex {CODEX_EMBEDDED_ARGS}"))
    processes.append((7102, 7101, f"{CODEX_NATIVE} {CODEX_EMBEDDED_ARGS}"))
    holds[7102] = holds[13090]

    assert "quit it first" in _refused(7102)

    del holds[13090]
    assert "quit it first" in _refused(7102)


def test_a_fleet_agents_codex_is_never_named_as_its_daemons_client(
    codex_daemon: DaemonTable, tmux: FakeTmux, project: ProjectInfo
) -> None:
    """#50: a fleet agent shows its own thread, never the one adopted, so its process (the one
    tmux started in its row's pane, and those below it) is never named as the daemon's client,
    even one with no ``-c`` (MCP and the persona's system prompt off) that is one."""
    processes, _holds = codex_daemon
    fleet = core.create_account("codex")
    row = fleet_service.spawn(
        project, "coder", agent="codex", account=str(fleet.slot), worktree=False
    ).agent
    tmux.pids[row.pane_id] = 7201
    processes.append((7201, 1, "node /usr/local/bin/codex --approve-for-me"))
    processes.append((7202, 7201, f"{CODEX_NATIVE} --approve-for-me"))

    _says_the_daemon(_refused(13090))

    processes.append((7001, 1, "node /usr/local/bin/codex"))  # the human's own TUI
    assert "quit it first" in _refused(7001)


def _codex(pid: int, words: str, *, shim: bool = False) -> tuple[int, int, str]:
    """A Codex process as ``ps`` lists it: its native binary, or its npm wrapper (``node …``)."""
    return (pid, 1, f"{'node /usr/local/bin/codex' if shim else CODEX_NATIVE} {words}".strip())


def test_a_codex_that_runs_no_thread_of_the_daemons_is_never_named_as_its_client(
    codex_daemon: DaemonTable, daemon_sockets: dict[int, set[int]]
) -> None:
    """r31: every Codex that was neither the daemon nor embedded was its client, so with the
    daemon holding the thread adopt named an ``app-server`` of its own (VS Code's, r31's probe:
    "a live Codex (pid 9100) … quit it first"), and quitting it released nothing. None of
    these runs a thread of the daemon's (measured: none connected to its socket): the daemon
    is named, with its note and stop, whether the socket says so or cannot say."""
    processes, _holds = codex_daemon
    others = [_codex(9100 + at, words) for at, words in enumerate(NO_CLIENTS)]
    processes.extend(others)
    pids = {str(pid) for pid, _parent, _args in others}

    said = _refused(13090)  # the socket cannot say: told by their words
    _says_the_daemon(said)
    assert not pids & set(re.findall(r"\d+", said))

    daemon_sockets[13090] = set()  # it says: none connected
    said = _refused(13090)
    _says_the_daemon(said)
    assert not pids & set(re.findall(r"\d+", said))


@pytest.mark.parametrize("words", HIDDEN_COMMANDS)
def test_a_codex_subcommand_its_help_hides_is_never_named_as_its_daemons_client(
    codex_daemon: DaemonTable, words: str
) -> None:
    """#51's reopen (tester-50-51): ``cloud-tasks``, ``execpolicy`` and ``tcp-tunnel`` were no
    subcommands to the classifier, so each read as a prompt, a TUI, and with the socket unable
    to say adopt named it as the daemon's client, "quit it first" (pid 9100). None is a TUI
    (``execpolicy check`` is past its own subcommand): the daemon is named, note and stop."""
    processes, _holds = codex_daemon
    processes.append(_codex(9100, words))
    said = _refused(13090)
    _says_the_daemon(said)
    assert "9100" not in said


@pytest.mark.parametrize(
    "words",
    [
        "",
        f"resume {THREAD}",
        f"fork {THREAD}",
        "agents",
        "app-server proxy",
        "--remote unix:// -c model=gpt-5.5",
        "-- -c is broken",
    ],
)
def test_a_codex_that_can_be_the_daemons_client_is_named_before_it(
    codex_daemon: DaemonTable, words: str
) -> None:
    """Codex's TUI with no subcommand, ``resume <id>`` and ``fork <id>``, and its ``agents``
    and ``app-server proxy``, each measured connected to the daemon's socket (r31): with the
    socket unable to say, each is named before the daemon, "quit it first", unchanged. So is
    a TUI given ``--remote`` (measured connected with a ``-c``, tester-50-51: the MANAGER
    DECISION, #51's reopen) and one whose prompt follows ``--`` (``-c`` its prompt's)."""
    processes, _holds = codex_daemon
    processes.append(_codex(7001, words))
    assert "quit it first" in _refused(7001)


@pytest.mark.parametrize("shim", [False, True], ids=["native", "npm"])
@pytest.mark.parametrize("prompt", PROMPTS)
def test_a_prompt_is_never_read_for_codexs_options(
    codex_daemon: DaemonTable, prompt: str, shim: bool
) -> None:
    """#50's reopen (tester-50): a prompt's words were read as options, ``-p`` in ``codex
    'use git log -p to explain the last commit'`` making that TUI embedded, so adopt named the
    daemon under it and its printed stop broke the human's open TUI. The reading ends at the
    prompt: the TUI is named, a possible client, the lesser harm (the MANAGER DECISION)."""
    processes, _holds = codex_daemon
    processes.append(_codex(7401, prompt, shim=shim))
    assert "quit it first" in _refused(7401)


@pytest.mark.parametrize("words", [*NO_CLIENTS, *HIDDEN_COMMANDS])
def test_a_codex_that_is_no_client_and_holds_the_thread_itself_is_named(
    codex_daemon: DaemonTable, words: str
) -> None:
    """One that is never the daemon's client but holds the thread (``lsof`` says so) is its
    holder, the one to quit, the daemon holding it too or not."""
    processes, holds = codex_daemon
    processes.append(_codex(9100, words))
    holds[9100] = holds[13090]

    assert "quit it first" in _refused(9100)

    del holds[13090]
    assert "quit it first" in _refused(9100)


def test_a_daemons_client_is_the_codex_connected_to_its_socket_when_that_can_be_told(
    codex_daemon: DaemonTable,
    daemon_sockets: dict[int, set[int]],
    tmux: FakeTmux,
    project: ProjectInfo,
) -> None:
    """The MANAGER DECISION's best way (r31, 8a): a client is told by its connection, not its
    words, which ``ps`` loses the quoting of (``codex 'review the diff with -c and -p'`` lists
    as a ``review``). Measured on 0.164.0 (``lsof -U``), the TUI's native binary is connected
    to the daemon's socket and its npm wrapper is not. So the connected one is named whatever
    its words say, and one whose words say it can be a client but is not connected (a TUI on
    another home's daemon, an ``exec``) is not; a fleet agent's, connected or not, never."""
    processes, _holds = codex_daemon
    processes.append(_codex(7001, "", shim=True))  # a TUI on another home's daemon
    processes.append((7002, 7001, CODEX_NATIVE))
    processes.append(_codex(7003, "exec say hi"))
    processes.append(_codex(7101, "review the diff with -c and -p", shim=True))
    processes.append((7102, 7101, f"{CODEX_NATIVE} review the diff with -c and -p"))
    daemon_sockets[13090] = {7102}

    assert "quit it first" in _refused(7102)

    daemon_sockets[13090] = set()
    _says_the_daemon(_refused(13090))

    fleet = core.create_account("codex")
    row = fleet_service.spawn(
        project, "coder", agent="codex", account=str(fleet.slot), worktree=False
    ).agent
    tmux.pids[row.pane_id] = 7201
    processes.append((7201, 1, "node /usr/local/bin/codex --approve-for-me"))
    processes.append((7202, 7201, f"{CODEX_NATIVE} --approve-for-me"))
    daemon_sockets[13090] = {7202}
    _says_the_daemon(_refused(13090))


def test_a_codex_connected_to_its_daemon_is_named_whatever_its_words_say(
    codex_daemon: DaemonTable, daemon_sockets: dict[int, set[int]]
) -> None:
    """r32's nit 1 (tester-50-51's M16): a TUI given the prompt ``'review -c model=gpt-5.5'``
    lists as a ``review`` with a ``-c``, words that read as never the daemon's client
    (``alone``). Its socket says it is one: where the socket can say, it is named, "quit it
    first", whatever its words say, and never the daemon under it."""
    processes, _holds = codex_daemon
    words = "review -c model=gpt-5.5"
    reader = get_adapter("codex").transcript_reader
    assert reader is not None and reader.held_open([CODEX_NATIVE, *words.split()]) == "alone"
    processes.append(_codex(7601, words))
    daemon_sockets[13090] = {7601}

    assert "quit it first" in _refused(7601)


def test_adopt_run_from_a_shell_the_daemon_runs_still_counts_the_daemon(
    codex_daemon: DaemonTable, daemon_sockets: dict[int, set[int]]
) -> None:
    """r32's question, measured on 0.164.0: the daemon runs the shell commands of every TUI
    on it (``zsh -c …`` a child of the ``--managed-daemon`` process, whose parent was the TUI
    that had started it). adopt dropped its own ancestors, so run from such a shell it dropped
    the daemon and that TUI and took a thread the daemon held (live: adoptable). The daemon
    and those above it are never this command's own: with no client running, the daemon is
    named, with its note and stop; with the TUI that started it connected, that TUI."""
    processes, _holds = codex_daemon
    me = os.getpid()
    processes.append((8800, 13090, "/bin/zsh -c aisquare fleet adopt"))
    processes.append((me, 8800, f"python -m aisquare fleet adopt {THREAD}"))

    _says_the_daemon(_refused(13090))

    processes[0] = (13090, 7001, CODEX_DAEMON)  # the TUI that started it, still running
    processes.append((7000, 1, "node /usr/local/bin/codex"))
    processes.append((7001, 7000, CODEX_NATIVE))
    daemon_sockets[13090] = {7001}
    assert "quit it first" in _refused(7001)


def test_a_codex_tui_of_another_home_is_never_named_as_this_daemons_client(
    codex_daemon: DaemonTable, daemon_sockets: dict[int, set[int]]
) -> None:
    """SMOKE11's re-run, item 5 (seq 4804): with a plain ``codex`` idle at its prompt (6921, its
    native 6923), a thread another home's daemon alone held was refused naming that TUI,
    "quit it first", and the daemon's stop never showed. A TUI is connected to its own home's
    daemon, and a client of this one is connected to this one: the daemon is named, with its
    note and stop. One of this home's that is connected is still named, though it may show
    another thread: a process table names none (stated in the CHANGELOG)."""
    processes, _holds = codex_daemon
    home = "/home/dev/.aisquare/codex-accounts/5/packages/app-server-daemon/releases/0.164.0"
    processes.append((7061, 1, f"{home}/bin/codex app-server --listen unix:// --managed-daemon"))
    processes.append((7062, 1, f"{home}/bin/codex app-server daemon pid-update-loop"))
    processes.append((6921, 1, "node /usr/local/bin/codex"))
    processes.append((6923, 6921, CODEX_NATIVE))
    daemon_sockets[13090] = set()
    daemon_sockets[7061] = {6923}

    _says_the_daemon(_refused(13090))

    processes.append((7001, 1, "node /usr/local/bin/codex"))  # a TUI of this home's
    processes.append((7002, 7001, CODEX_NATIVE))
    daemon_sockets[13090] = {7002}
    assert "quit it first" in _refused(7002)


def test_codexs_daemon_that_holds_no_rollout_leaves_the_thread_adoptable(
    codex_daemon: DaemonTable,
) -> None:
    """The daemon alive but holding the thread no more (it let go after its idle minutes,
    ``lsof`` naming no holder), or its ``pid-update-loop`` alone (the daemon stopped by
    ``codex app-server daemon stop``, which leaves it running): adoptable."""
    processes, holds = codex_daemon
    holds.clear()  # let go
    [listed] = adopt_service.recent()
    assert listed.held_by is None and adoptable(listed)

    holds[13100] = holds.get(13090, frozenset())  # nothing, as lsof says of it
    del processes[0]  # stopped: its pid-update-loop alone
    [listed] = adopt_service.recent()
    assert listed.held_by is None and adoptable(listed)
    assert adopt_service.adopt(THREAD).agent.agent == "codex"


def test_a_codex_process_says_how_it_holds_what_it_writes() -> None:
    """``held_open``: every process of Codex's daemon for its clients; for its own session, a
    form that can be the daemon's client (``session``: its TUI with no subcommand or a
    prompt, ``resume``, ``fork``, ``--remote`` whatever else it is given, ``agents``,
    ``app-server proxy``, each measured connected to the daemon's socket, r31 and #51's
    reopen) or one that never is (``alone``: one that runs embedded, #50, and every other
    subcommand, an ``app-server`` of its own, ``exec-server``, ``review``, ``login``…, those
    its help hides too, measured not connected or holding their own rollouts); never
    ``--enable`` nor ``--disable``, which ran on the daemon when it matched them. A prompt is
    never read for options (tester-50: ``ps`` loses quoting), nor what follows ``--``, and a
    subcommand's name followed by what could be a prompt's words, past its own subcommand,
    reads as a prompt's, a possible client. A process that runs no Codex, whatever its
    arguments say, none; Claude Code's none."""
    reader = get_adapter("codex").transcript_reader
    claude = get_adapter("claude-code").transcript_reader
    assert reader is not None and claude is not None
    assert reader.held_open(CODEX_DAEMON.split()) == "clients"
    assert reader.held_open(CODEX_DAEMON_LOOP.split()) == "clients"
    assert reader.held_open([CODEX_DAEMON_BIN, "app-server"]) == "clients", "run from its dir"
    assert reader.held_open(f"{CODEX_NATIVE} app-server daemon stop".split()) == "clients"
    assert reader.held_open(f"{CODEX_NATIVE} app-server --managed-daemon".split()) == "clients"
    assert reader.held_open([CODEX_NATIVE]) == "session"
    assert reader.held_open(["node", "/usr/local/bin/codex"]) == "session"
    for plain in (
        "--enable hooks",
        "--disable artifact",
        "-m gpt-5.5 --approve-for-me --no-alt-screen",
        "-C /repo -s workspace-write -a never",
        "--remote unix://",
        "-m exec",  # a model named exec, no subcommand
        *PROMPTS,
        "review the diff with -c and -p",  # a prompt, measured connected
        "exec say hi",  # or ``codex 'exec say hi'``, which ps lists alike
        "e say hi",
        "-m gpt-5.5 exec --json say hi",
        "app-server is down, fix it",
        "-- use -c and --search",  # a prompt
        "-- -c is broken",  # ``-c`` the prompt's, after ``--``
        "resume -- -c is broken",
        "--remote unix:// -c model=gpt-5.5",  # measured connected, its ``-c`` notwithstanding
        "-c model=gpt-5.5 --remote unix://",
        "--remote unix:// -c model=gpt-5.5 fix the bug",  # and its prompt
        "--remote=ws://127.0.0.1:4500 --search",
        f"resume --remote unix:// {THREAD} -c model=gpt-5.5",
        f"--remote unix:// fork {THREAD} --profile p2",
        "exec review the diff",  # a prompt past ``exec``'s own subcommand
        "exec review fork",  # its own subcommand is the first word only
        "resume",
        "resume --last",
        f"resume {THREAD}",
        f"fork {THREAD}",
        f"-C /repo resume {THREAD} carry on",
        "resume 019a2b3c -c model=gpt-5.5",  # a session's name, then its prompt
        "resume work -c flag",  # ``codex 'resume work -c flag'``
        "resume --last use -c",  # its prompt
        "agents",
        "app-server proxy",
    ):
        assert reader.held_open([CODEX_NATIVE, *plain.split()]) == "session", plain
    for prompt in PROMPTS:
        node = ["node", "/usr/local/bin/codex", *prompt.split()]
        assert reader.held_open(node) == "session", prompt
    for alone in (
        CODEX_EMBEDDED_ARGS,
        "-c model=gpt-5.5",
        "-cmodel=gpt-5.5",
        "--config model=gpt-5.5",
        "--config=model=gpt-5.5",
        "--search",
        "--no-daemon",
        "--strict-config",
        "--dangerously-bypass-hook-trust",
        "--profile p2",
        "-p p2",
        "-pp2",
        "--profile=p2",
        "-c model=gpt-5.5 exec the tests",  # an option before it: embedded, whatever follows
        f"resume {THREAD} -c model=gpt-5.5",
        f"fork {THREAD} --profile p2",
        f"resume --last -c model=gpt-5.5 {THREAD}",
        *NO_CLIENTS,
        "app-server --listen stdio://",
        "exec",
        "exec --json daemon",
        "exec --json",
        "e --skip-git-repo-check",
        "review --uncommitted",
        "review --base main",
        "login --device-auth",
        "logout",
        "mcp",
        "cloud",
        "stdio-to-uds",
        *HIDDEN_COMMANDS,
        "app-server generate-ts",  # its own subcommand (``codex completion bash``)
        "exec resume --last",
        "mcp list",
        "login status",
        "--remote-auth-token-env TOKEN -c model=gpt-5.5",  # no ``--remote``
    ):
        assert reader.held_open([CODEX_NATIVE, *alone.split()]) == "alone", alone
    assert reader.held_open(["node", "--no-warnings", "/usr/local/bin/codex", "exec"]) == "alone"
    assert reader.held_open(f"node /usr/local/bin/codex {CODEX_EMBEDDED_ARGS}".split()) == "alone"
    assert reader.held_open(ZSH_NAMING_CODEX.split()) is None
    assert reader.held_open(TMUX_NAMING_CODEX.split()) is None
    assert reader.held_open(TMUX_RUNNING_CODEX.split()) is None
    assert reader.held_open(TMUX_STARTING_CODEX.split()) is None
    runner = ["/usr/local/bin/aisquare", "agent", "run", "--agent", "codex", "--", "codex"]
    assert reader.held_open(runner) is None
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
