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
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ProjectInfo
from aisquare.services import adopt as adopt_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _command, _flag

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

SESSION = "11111111-2222-3333-4444-555555555555"


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


def test_liveness_on_windows_asks_the_process_and_never_signals_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc/p3's Windows leg: ``os.kill(pid, 0)`` is a CTRL_C_EVENT there, and adopt asking
    after the test runner's own pid interrupted the suite. Windows asks the process."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(sys, "platform", "win32")
    asked: list[int] = []

    def query(pid: int) -> bool:
        asked.append(pid)
        return True

    monkeypatch.setattr(adopt_service, "_alive_on_windows", query)

    assert adopt_service._alive(os.getpid()) is True
    assert adopt_service._alive(0) is False
    assert (signalled, asked) == ([], [os.getpid()])


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
