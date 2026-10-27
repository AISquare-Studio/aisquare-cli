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
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.adopt_picker import adoptable, conversation_text
from aisquare.core import claude_accounts as core
from aisquare.core import launch_seal, process_table
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ProjectInfo
from aisquare.services import adopt as adopt_service
from aisquare.services import agent_events
from tests import test_fleet_service as fleet_suite
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
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
