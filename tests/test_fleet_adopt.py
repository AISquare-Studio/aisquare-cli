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
def no_processes(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """No real `ps`: the processes a test says are running, and none else."""
    running: list[tuple[int, str]] = []
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
    path = account.config_dir / "projects" / str(cwd).replace("/", "-") / f"{session}.jsonl"
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
    no_processes: list[tuple[int, str]],
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
    no_processes.append((4242, f"claude --resume {SESSION}"))
    with pytest.raises(adopt_service.AdoptRefused) as by_args:
        adopt_service.adopt(SESSION)

    assert by_registry.value.code == "held" and str(os.getpid()) in str(by_registry.value)
    assert by_args.value.code == "held" and "4242" in str(by_args.value)
    assert tmux.spawned == []
    # A registry entry whose process is gone holds nothing.
    no_processes.clear()
    registry.write_text(json.dumps({"pid": 999_999_999, "sessionId": SESSION}))
    assert adopt_service.adopt(SESSION).agent.session_id == SESSION


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
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, no_processes: list[tuple[int, str]]
) -> None:
    plain, second = core.default_account(), core.create_account()
    owned = "00000000-0000-0000-0000-00000000000a"
    _transcript(second, owned, project.root, age=10)
    adopt_service.adopt(owned)
    _transcript(plain, "00000000-0000-0000-0000-00000000000b", project.root, "the newest", age=20)
    _transcript(second, "00000000-0000-0000-0000-00000000000c", "/nowhere", "x" * 200, age=30)
    _transcript(plain, "00000000-0000-0000-0000-00000000000d", project.root, "oldest", age=40)
    no_processes.append((77, "claude --resume 00000000-0000-0000-0000-00000000000c"))

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
