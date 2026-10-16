"""What the Claude accounts share: a folder's trust, and (opt-in) one settings.json.

``core.claude_sharing`` and the service around it, against fake homes only: every
account here is a directory under ``tmp_path`` (``fake_home`` for the plain claude,
``core.create_account`` for the managed slots). What a test asserts is the artefact —
the bytes of each account's ``.claude.json``, the link a ``settings.json`` became —
and the rules are the ones measured on Claude Code 2.1.299 (the module docstring).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import claude_sharing as sharing
from aisquare.core.config import load_config, save_config
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as service
from tests.fsperms import can_symlink

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
CURSOR = "\u276f"
"""Claude Code's pointer in a list and at its prompt."""


# --- helpers -------------------------------------------------------------------------------------


def _key(path: Path) -> str:
    """``path`` as Claude Code keys it: resolved, and with ``/`` on Windows too."""
    return sharing.project_key(Path(os.path.realpath(path)))


def _write_claude_json(
    account: ClaudeAccount, email: str, *, trusted: tuple[str, ...] = ()
) -> Path:
    """A ``.claude.json`` as Claude Code writes one: two-space JSON, no newline at the end."""
    path = core.claude_json_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "numStartups": 7,
        "oauthAccount": {
            "emailAddress": email,
            "organizationName": "AISquare — Studio",
            "accountUuid": f"uuid-{account.slot}",
        },
        "userID": f"user-{account.slot}",
        "hasCompletedOnboarding": True,
        "tipsHistory": {"memory-command": 18},
        "projects": {
            key: {
                "allowedTools": [],
                "mcpServers": {},
                "hasTrustDialogAccepted": True,
                "lastCost": 0.25,
            }
            for key in trusted
        },
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _trusted(account: ClaudeAccount) -> list[str]:
    return sharing.trusted_folders(
        json.loads(core.claude_json_path(account).read_text(encoding="utf-8"))
    )


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.fixture
def three(fake_home: Path) -> tuple[ClaudeAccount, ClaudeAccount, ClaudeAccount]:
    """The plain claude and two managed slots, each signed in, none trusting anything."""
    plain = core.default_account()
    second, third = core.create_account(), core.create_account()
    for account, email in ((plain, "one@x.dev"), (second, "two@x.dev"), (third, "three@x.dev")):
        _write_claude_json(account, email)
    return plain, second, third


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``aisquare launch`` with its exec caught: what it would have run, and with what."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: f"/usr/local/bin/{cmd}")
    return captured


# --- which entries trust a folder ----------------------------------------------------------------


def test_outside_git_the_folder_or_any_ancestor_trusts_it(tmp_path: Path) -> None:
    folder = tmp_path / "plain" / "child"
    folder.mkdir(parents=True)
    scope = sharing.trust_scope(folder)
    real = Path(os.path.realpath(folder))
    assert scope.key == _key(folder)  # what a yes there records
    assert scope.covering == tuple(sharing.project_key(p) for p in (real, *real.parents))
    assert scope.covering[-1] == sharing.project_key(Path(real.anchor))


def test_in_a_checkout_the_root_and_the_folders_up_to_it_trust_it_and_nothing_above(
    tmp_path: Path,
) -> None:
    """Measured: an entry on a folder ABOVE the repository did not stop the question."""
    repo = _repo(tmp_path / "parent" / "repo")
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    scope = sharing.trust_scope(sub)
    root = _key(repo)
    assert scope.key == root
    assert scope.covering == (root, _key(sub), _key(repo / "src"))
    assert _key(tmp_path / "parent") not in scope.covering
    # Control: the same folder outside git is covered by that parent. The .git is moved
    # aside, not removed: Windows refuses to unlink git's read-only object files.
    (repo / ".git").rename(tmp_path / "git-set-aside")
    assert _key(tmp_path / "parent") in sharing.trust_scope(sub).covering


def test_a_worktree_is_trusted_as_its_main_checkout_inside_or_outside_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _git("worktree", "add", "-q", "-b", "inside", str(repo / ".wt" / "x"), cwd=repo)
    _git("worktree", "add", "-q", "-b", "outside", str(tmp_path / "elsewhere"), cwd=repo)
    root = _key(repo)
    for worktree in (repo / ".wt" / "x", tmp_path / "elsewhere"):
        scope = sharing.trust_scope(worktree)
        assert scope.key == root  # what a yes in the worktree records
        assert scope.covering == (root, _key(worktree))


def test_on_windows_a_key_is_spelled_with_forward_slashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code's path normaliser turns every backslash into ``/`` on Windows, so its
    ``projects`` keys read ``C:/Users/me/work``; an entry spelled the native way is none."""
    native = Path("C:\\Users\\me\\work")  # a Windows path's text, built on any platform
    monkeypatch.setattr(os, "name", "nt")
    assert sharing.project_key(native) == "C:/Users/me/work"
    monkeypatch.setattr(os, "name", "posix")
    assert sharing.project_key(native) == str(native)  # elsewhere, the path as it is


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_a_folder_reached_through_a_link_is_trusted_by_its_real_path(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    scope = sharing.trust_scope(tmp_path / "link")
    assert scope.key == _key(real)
    assert sharing.project_key(tmp_path / "link") not in scope.covering


# --- the one write into .claude.json -------------------------------------------------------------


def test_a_grant_changes_the_one_value_and_every_other_byte_stays(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, _third = three
    folder = tmp_path / "work"
    folder.mkdir()
    path = _write_claude_json(second, "two@x.dev", trusted=(str(tmp_path / "older"),))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["projects"][str(tmp_path / "older")]["hasTrustDialogAccepted"] = False
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    written = sharing.grant_trust(path, str(tmp_path / "older"), str(folder))

    assert written == [str(tmp_path / "older"), str(folder)]
    data["projects"][str(tmp_path / "older")]["hasTrustDialogAccepted"] = True
    data["projects"][str(folder)] = {"hasTrustDialogAccepted": True}
    assert path.read_text(encoding="utf-8") == json.dumps(data, indent=2, ensure_ascii=False)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["oauthAccount"] == json.loads(before)["oauthAccount"]  # the login, untouched
    assert after["userID"] == "user-2"
    assert not path.with_name(".claude.json.lock").exists()  # the lock was let go
    # A second grant finds nothing to do and writes nothing.
    stamp = path.stat().st_mtime_ns
    assert sharing.grant_trust(path, str(folder)) == []
    assert path.stat().st_mtime_ns == stamp


def test_a_lone_surrogate_node_wrote_is_written_back_as_the_escape_it_was(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    """Review of #259, round 21: Node writes a string cut between the halves of a pair as
    ``"\\ud83d"``. ``json.loads`` reads a lone surrogate, which UTF-8 cannot hold, so the
    write raised and every launch on the account said the trust could not be shared."""
    _plain, second, third = three
    folder = tmp_path / "work"
    folder.mkdir()
    _write_claude_json(second, "two@x.dev", trusted=(_key(folder),))
    path = core.claude_json_path(third)
    text = path.read_text(encoding="utf-8")
    cut = text[:-1] + ',\n  "history": "abc \\ud83d",\n  "pair": "\\ud83d\\ude00 \\udc00x"\n}'
    path.write_text(cut, encoding="utf-8")
    before = json.loads(cut)

    notes = service.share_for_launch(folder, account=third)

    assert notes == [f"{_key(folder)} is trusted on account 2 — now on account 3 too"]
    written = path.read_text(encoding="utf-8")
    assert '"history": "abc \\ud83d"' in written and "\\udc00x" in written
    after = json.loads(written)
    assert after["projects"].pop(_key(folder)) == {"hasTrustDialogAccepted": True}
    assert after == before  # the halves and the whole pair read back as they were


def test_a_damaged_or_missing_file_is_never_written(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, third = three
    damaged = core.claude_json_path(second)
    damaged.write_text('{"oauthAccount": {"emailAddress": "two@x.dev"', encoding="utf-8")
    with pytest.raises(sharing.SharingError, match="not valid JSON"):
        sharing.grant_trust(damaged, str(tmp_path))
    assert damaged.read_text(encoding="utf-8") == '{"oauthAccount": {"emailAddress": "two@x.dev"'
    missing = core.claude_json_path(third)
    missing.unlink()
    with pytest.raises(sharing.NotStartedYet):
        sharing.grant_trust(missing, str(tmp_path))
    assert not missing.exists()


def test_a_grant_waits_for_claude_codes_lock_and_takes_over_a_dead_one(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plain, second, _third = three
    path = core.claude_json_path(second)
    lock = path.with_name(".claude.json.lock")
    lock.mkdir()  # a session saving right now
    monkeypatch.setattr(sharing, "LOCK_WAIT_SECONDS", 0.0)
    before = path.read_bytes()
    with pytest.raises(sharing.SharingError, match="lock"):
        sharing.grant_trust(path, str(tmp_path))
    assert path.read_bytes() == before and lock.is_dir()  # its lock is its own
    old = time.time() - sharing.LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))  # a session that died holding it
    assert sharing.grant_trust(path, str(tmp_path)) == [str(tmp_path)]
    assert not lock.exists()


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_a_linked_claude_json_is_written_through_and_stays_a_link(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, _third = three
    path = core.claude_json_path(second)
    kept = tmp_path / "dotfiles" / "claude2.json"
    kept.parent.mkdir()
    path.rename(kept)
    path.symlink_to(kept)
    sharing.grant_trust(path, str(tmp_path))
    assert path.is_symlink()
    assert sharing.trusted_folders(json.loads(kept.read_text(encoding="utf-8"))) == [str(tmp_path)]


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_a_linked_claude_json_is_locked_by_the_name_claude_code_locks_it_by(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final gate NIT 4: ``proper-lockfile`` resolves the file before it names the lock, so
    Claude Code locks a linked .claude.json as ``<the file it points at>.lock``. Locked as
    ``<the link>.lock``, a write here and a session saving through the link each held a
    lock of its own, and the later save dropped the entry."""
    _plain, second, _third = three
    path = core.claude_json_path(second)
    kept = tmp_path / "dotfiles" / "claude2.json"
    kept.parent.mkdir()
    path.rename(kept)
    path.symlink_to(kept)
    held = kept.with_name("claude2.json.lock")
    held.mkdir()  # a session saving through the link right now
    monkeypatch.setattr(sharing, "LOCK_WAIT_SECONDS", 0.0)
    before = kept.read_bytes()
    with pytest.raises(sharing.SharingError, match="lock"):
        sharing.grant_trust(path, str(tmp_path))
    assert kept.read_bytes() == before and held.is_dir()  # its lock is its own
    assert not path.with_name(".claude.json.lock").exists()  # none under the link's name
    held.rmdir()
    assert sharing.grant_trust(path, str(tmp_path)) == [str(tmp_path)]
    assert not held.exists() and not path.with_name(".claude.json.lock").exists()


# --- a launch shares the trust -------------------------------------------------------------------


def test_a_launch_on_account_3_gets_the_trust_account_2_gave_and_says_so(
    three: tuple[ClaudeAccount, ...],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    plain, second, third = three
    folder = tmp_path / "work"
    folder.mkdir()
    key = _key(folder)
    _write_claude_json(second, "two@x.dev", trusted=(key,))
    before = json.loads(core.claude_json_path(third).read_text(encoding="utf-8"))
    monkeypatch.chdir(folder)

    result = runner.invoke(app, ["launch", "coder", "--account", "3"])

    assert result.exit_code == 0, result.output
    assert launched["env"][core.CONFIG_DIR_VAR] == str(third.config_dir)
    assert _trusted(third) == [key]
    after = json.loads(core.claude_json_path(third).read_text(encoding="utf-8"))
    assert after["projects"].pop(key) == {"hasTrustDialogAccepted": True}
    assert after == before  # nothing else changed
    assert f"{key} is trusted on account 2 — now on account 3 too" in result.stderr
    assert _trusted(plain) == []  # only the account the launch runs on

    # The plain claude (no --account, no default): the launch's own environment decides.
    plain_run = runner.invoke(app, ["launch", "coder"])
    assert plain_run.exit_code == 0, plain_run.output
    assert core.CONFIG_DIR_VAR not in launched["env"]
    assert _trusted(plain) == [key]
    assert "now on plain claude too" in plain_run.stderr

    # Once there, a launch has nothing to say.
    again = runner.invoke(app, ["launch", "coder", "--account", "3"])
    assert again.exit_code == 0 and "trusted on" not in again.stderr


def test_a_launch_a_spawn_shared_for_reads_no_claude_json_again(
    three: tuple[ClaudeAccount, ...],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    """Review of #259, round 21: a fleet spawn shares into the account it picked before its
    window opens, and the window's `aisquare launch` read every account's .claude.json
    again for the same share. The spawn says so on the window (``SHARED_ENV``), for that
    slot only, and the agent does not inherit it."""
    _plain, second, _third = three
    folder = tmp_path / "work"
    folder.mkdir()
    _write_claude_json(second, "two@x.dev", trusted=(_key(folder),))
    reads: list[Path] = []
    real = sharing.read_claude_json

    def read(path: Path) -> dict[str, Any]:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(sharing, "read_claude_json", read)
    monkeypatch.chdir(folder)
    monkeypatch.setenv(service.SHARED_ENV, "3")

    told = runner.invoke(app, ["launch", "coder", "--account", "3"])

    assert told.exit_code == 0, told.output
    assert reads == [] and "trusted on" not in told.stderr
    assert service.SHARED_ENV not in launched["env"], "not the agent's to inherit"
    # Control: told of another slot, this launch shares as before.
    monkeypatch.setenv(service.SHARED_ENV, "2")
    other = runner.invoke(app, ["launch", "coder", "--account", "3"])
    assert other.exit_code == 0, other.output
    assert reads and "now on account 3 too" in other.stderr


def test_a_machine_with_one_account_reads_no_claude_json_to_share(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 21: with only the plain claude there is no trust to copy from
    and no settings.json to link to it, yet every launch read its .claude.json."""
    plain = core.default_account()
    _write_claude_json(plain, "one@x.dev")
    reads: list[Path] = []

    def read(path: Path) -> dict[str, Any]:
        reads.append(path)
        return {}

    monkeypatch.setattr(sharing, "read_claude_json", read)
    assert service.share_for_launch(tmp_path, account=plain) == []
    assert reads == []
    core.create_account()  # control: a second account, and the plain claude's file is read
    assert service.share_for_launch(tmp_path, account=plain) == []
    assert reads


def test_a_parents_trust_is_copied_as_the_parents_entry(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, third = three
    parent = tmp_path / "code"
    child = parent / "notes" / "today"
    child.mkdir(parents=True)
    _write_claude_json(second, "two@x.dev", trusted=(_key(parent),))
    notes = service.share_for_launch(child, account=third)
    assert _trusted(third) == [_key(parent)]
    assert notes == [f"{_key(parent)} is trusted on account 2 — now on account 3 too"]
    assert isinstance(notes[0], service.SharedNote)  # a share made: the receipt's ✓


def test_no_account_trusts_the_folder_so_nothing_is_written(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, third = three
    folder = _repo(tmp_path / "repo")
    # Account 2 trusts the folder ABOVE the repository, which Claude Code does not honour.
    _write_claude_json(second, "two@x.dev", trusted=(_key(tmp_path),))
    before = core.claude_json_path(third).read_bytes()
    assert service.share_for_launch(folder, account=third) == []
    assert core.claude_json_path(third).read_bytes() == before


def test_a_damaged_target_costs_the_trust_with_a_note_never_the_launch(
    three: tuple[ClaudeAccount, ...],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    _plain, second, third = three
    folder = tmp_path / "work"
    folder.mkdir()
    _write_claude_json(second, "two@x.dev", trusted=(_key(folder),))
    core.claude_json_path(third).write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(folder)
    result = runner.invoke(app, ["launch", "coder", "--account", "3"])
    assert result.exit_code == 0, result.output
    assert launched["argv"][0] == "claude"  # launched all the same
    assert core.claude_json_path(third).read_text(encoding="utf-8") == "{not json"
    stderr = " ".join(result.stderr.split())
    assert (
        "folder trust could not be shared into account 3: its .claude.json is not valid JSON"
        in stderr
    )
    assert "aisquare accounts trust" in stderr
    notes = service.share_for_launch(folder, account=third)
    assert notes and not any(isinstance(note, service.SharedNote) for note in notes)  # a ⚠


def test_with_share_trust_off_a_launch_writes_nothing(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, third = three
    folder = tmp_path / "work"
    folder.mkdir()
    _write_claude_json(second, "two@x.dev", trusted=(_key(folder),))
    config = load_config()
    config.accounts.share_trust = False
    save_config(config)
    before = core.claude_json_path(third).read_bytes()
    assert service.share_for_launch(folder, account=third) == []
    assert core.claude_json_path(third).read_bytes() == before
    # Control: on again, the same call copies it.
    config.accounts.share_trust = True
    save_config(config)
    assert service.share_for_launch(folder, account=third) != []
    assert _trusted(third) == [_key(folder)]


# --- the question on screen ----------------------------------------------------------------------


def test_the_captured_trust_question_is_recognised_and_ordinary_screens_are_not() -> None:
    screen = (FIXTURES / "trust_prompt_2.1.299.txt").read_text(encoding="utf-8").splitlines()
    assert "\x1b[" in "".join(screen)  # as `capture-pane -e` gives it, escapes and all
    assert sharing.shows_trust_prompt(screen)
    older = ["Do you trust the files in this folder?", f"{CURSOR} 1. Yes, proceed", "  2. No, exit"]
    assert sharing.shows_trust_prompt(older)
    # An agent at work that talks about trust is not the dialog; nor is a blank pane.
    chat = ["⏺ Should I trust this folder? Yes, I trust this folder.", f"{CURSOR} "]
    assert not sharing.shows_trust_prompt(chat)
    assert not sharing.shows_trust_prompt([])
    session = [line for line in screen if "No," not in line]
    assert not sharing.shows_trust_prompt(session)  # each phrase alone is not enough


def test_the_trust_questions_words_on_a_screen_at_work_are_not_the_question() -> None:
    """Review of #259, round 14: both phrases anywhere on the screen read an agent at work
    as at the question — one that ``cat``s the captured screen, or reads this module. The
    dialog takes the input box's place, so its "No, exit" is an option of its own among
    the screen's last rows; at work, the input box is what the screen ends on."""
    screen = (FIXTURES / "trust_prompt_2.1.299.txt").read_text(encoding="utf-8").splitlines()
    input_box = ["", "─" * 40, f"{CURSOR} ", "─" * 40, "  ? for shortcuts"]
    assert not sharing.shows_trust_prompt(
        ["⏺ Bash(cat trust_prompt_2.1.299.txt)", *screen, *input_box]
    )
    source = ['_TRUST_EXIT = re.compile(r"No, exit")', '"Yes, I trust this folder"', *input_box]
    assert not sharing.shows_trust_prompt(source)
    # "No, exit" in a sentence of the foot is no option either.
    assert not sharing.shows_trust_prompt(["trust this folder", "Pick No, exit to leave"])
    # Control: the question with blank rows under it, as a taller pane shows it.
    assert sharing.shows_trust_prompt([*screen, "", "", ""])


def test_the_trust_question_in_any_layout_is_the_question_until_an_input_box_is_under_it() -> None:
    """Review of #259, round 15: "No, exit" had to be a whole row among the screen's last
    three, so a dialog drawn in a box, a key hint wrapped in a narrow pane, or a row added
    under it read as no question — and the prompt was typed into it, where Enter picks
    "No, exit". Read fail-closed now: both there and no input box under the option."""
    boxed = [
        "╭──────────────────────────────────────────╮",
        "│ Do you trust the files in this folder?   │",
        "│                                          │",
        f"│ {CURSOR} 1. Yes, proceed                        │",
        "│   2. No, exit                            │",
        "╰──────────────────────────────────────────╯",
        "   Enter to confirm · Esc to exit",
    ]
    assert sharing.shows_trust_prompt(boxed)
    screen = (FIXTURES / "trust_prompt_2.1.299.txt").read_text(encoding="utf-8").splitlines()
    *dialog, hint = [line for line in screen if line.strip()]
    assert "confirm" in hint and "Esc" in hint
    assert sharing.shows_trust_prompt([*dialog, " Enter to confirm · Esc to", " cancel"])
    third = [*dialog[:-1], "   Yes, and remember this for every folder", dialog[-1], hint]
    assert sharing.shows_trust_prompt(third)
    assert sharing.shows_trust_prompt([*screen, " Press Ctrl-C again to quit"])
    # The words above an input box are an agent at work, whatever its box shows.
    for prompt in (f"{CURSOR} ", f'{CURSOR} Try "fix the lint errors"', "│ > │"):
        assert not sharing.shows_trust_prompt([*screen, "─" * 40, prompt, "─" * 40]), prompt
    assert not sharing.shows_trust_prompt([*boxed, "╭────╮", "│ > │", "╰────╯"])


# --- aisquare accounts trust ---------------------------------------------------------------------


def test_accounts_trust_trusts_the_repository_on_every_account_at_once(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, runner: CliRunner
) -> None:
    plain, second, third = three
    repo = _repo(tmp_path / "repo")
    sub = repo / "docs"
    sub.mkdir()
    root = _key(repo)
    _write_claude_json(second, "two@x.dev", trusted=(root,))

    result = runner.invoke(app, ["--json", "accounts", "trust", str(sub)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["trusted_as"] == root and payload["folder"] == os.path.realpath(sub)
    outcomes = {row["slot"]: (row["outcome"], row["folders"]) for row in payload["accounts"]}
    assert outcomes == {1: ("trusted", [root]), 2: ("already", []), 3: ("trusted", [root])}
    assert _trusted(plain) == _trusted(second) == _trusted(third) == [root]

    human = runner.invoke(app, ["accounts", "trust", str(sub)])
    assert human.exit_code == 0
    head = f"✓ {root} on every account that has started (it covers {os.path.realpath(sub)})"
    assert head in human.stdout
    assert "account 3: already trusted" in human.stdout


def test_accounts_trust_heads_its_report_with_the_accounts_that_have_started(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, runner: CliRunner
) -> None:
    """Final gate NIT 2: the heading said '✓ <key> on every account' over a row that said
    'account 3: skipped': an account Claude Code never started on is given nothing."""
    _plain, _second, third = three
    core.claude_json_path(third).unlink()  # never started
    folder = tmp_path / "work"
    folder.mkdir()
    result = runner.invoke(app, ["accounts", "trust", str(folder)])
    assert result.exit_code == 0, result.output
    assert f"✓ {_key(folder)} on every account that has started\n" in result.stdout
    assert "  · account 3: skipped" in result.stdout
    synced = runner.invoke(app, ["accounts", "trust", "--sync"])
    assert synced.exit_code == 0, synced.output
    assert "✓ every trusted folder on every account that has started\n" in synced.stdout
    assert "  · account 3: skipped" in synced.stdout


def test_accounts_trust_reports_an_account_it_could_not_write_and_fails(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, runner: CliRunner
) -> None:
    _plain, second, third = three
    core.claude_json_path(second).write_text("[]", encoding="utf-8")
    core.claude_json_path(third).unlink()  # never started: skipped, not failed
    result = runner.invoke(app, ["accounts", "trust", str(tmp_path)])
    assert result.exit_code == 1
    assert "account 2: NOT trusted — its .claude.json is not a JSON object" in result.stdout
    assert "account 3: skipped — Claude Code has never started there" in result.stdout
    assert "plain claude: trusted now" in result.stdout
    assert runner.invoke(app, ["accounts", "trust", str(tmp_path / "nope")]).exit_code != 0


def test_accounts_trust_sync_copies_every_trusted_folder_everywhere(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, runner: CliRunner
) -> None:
    plain, second, third = three
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    gone = tmp_path / "removed-since"
    _write_claude_json(plain, "one@x.dev", trusted=(_key(a),))
    _write_claude_json(second, "two@x.dev", trusted=(_key(b), str(gone)))

    result = runner.invoke(app, ["--json", "accounts", "trust", "--sync"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["folders_gone"] == 1
    real_a, real_b = _key(a), _key(b)
    assert sorted(_trusted(third)) == sorted([real_a, real_b])
    assert sorted(_trusted(plain)) == sorted([real_a, real_b])
    assert sorted(_trusted(second)) == sorted([real_b, str(gone), real_a])
    assert str(gone) not in _trusted(third)  # a folder that is gone is trusted nowhere new
    assert runner.invoke(app, ["accounts", "trust", str(a), "--sync"]).exit_code != 0


# --- one settings.json ---------------------------------------------------------------------------


def _settings(account: ClaudeAccount, data: dict[str, Any]) -> Path:
    path = sharing.settings_path(account)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_accounts_settings_sync_links_each_managed_account_to_the_plain_claudes(
    three: tuple[ClaudeAccount, ...], runner: CliRunner
) -> None:
    plain, second, third = three
    source = _settings(plain, {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "opus"})
    own = _settings(second, {"model": "sonnet"})
    credentials = core.credentials_path(second)
    credentials.write_text('{"claudeAiOauth": {"accessToken": "tok"}}', encoding="utf-8")
    login_before = core.claude_json_path(second).read_bytes()

    result = runner.invoke(app, ["--json", "accounts", "settings", "--sync"])

    assert result.exit_code == 0, result.output
    rows = {row["slot"]: row for row in json.loads(result.stdout)["accounts"]}
    assert rows[2]["outcome"] == "shared" and rows[3]["outcome"] == "shared"
    for account in (second, third):
        path = sharing.settings_path(account)
        assert path.is_symlink() and os.path.realpath(path) == os.path.realpath(source)
    kept = Path(rows[2]["kept"])
    assert json.loads(kept.read_text()) == {"model": "sonnet"} and kept.parent == own.parent
    assert rows[3]["kept"] is None  # it had none to keep
    # The logins are never part of it.
    assert core.claude_json_path(second).read_bytes() == login_before
    assert credentials.read_text() == '{"claudeAiOauth": {"accessToken": "tok"}}'
    assert not core.claude_json_path(second).is_symlink() and not credentials.is_symlink()

    shown = runner.invoke(app, ["accounts", "settings"])
    assert "account 2 shares it" in shown.stdout and "account 3 shares it" in shown.stdout

    undone = runner.invoke(app, ["accounts", "settings", "--unshare"])
    assert undone.exit_code == 0, undone.output
    path = sharing.settings_path(second)
    assert not path.is_symlink()
    assert json.loads(path.read_text()) == json.loads(source.read_text())


def test_a_plain_settings_json_that_decides_a_login_is_never_shared(
    three: tuple[ClaudeAccount, ...], runner: CliRunner
) -> None:
    plain, second, _third = three
    _settings(plain, {"env": {"ANTHROPIC_API_KEY": "sk-ant-x"}, "model": "opus"})
    own = _settings(second, {"model": "sonnet"})
    result = runner.invoke(app, ["accounts", "settings", "--sync"])
    assert result.exit_code == 1
    assert "env.ANTHROPIC_API_KEY" in " ".join(result.output.split())
    assert not own.is_symlink() and json.loads(own.read_text()) == {"model": "sonnet"}


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_an_accounts_own_login_setting_keeps_its_own_file(
    three: tuple[ClaudeAccount, ...], runner: CliRunner
) -> None:
    plain, second, third = three
    _settings(plain, {"model": "opus"})
    own = _settings(second, {"forceLoginOrgUUID": "org-2"})
    result = runner.invoke(app, ["accounts", "settings", "--sync"])
    assert result.exit_code == 0, result.output
    assert "account 2 keeps its own — its own settings.json sets forceLoginOrgUUID" in result.stdout
    assert not own.is_symlink()
    assert sharing.settings_path(third).is_symlink()


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_share_settings_links_the_account_a_launch_runs_on(
    three: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    plain, second, third = three
    _settings(plain, {"model": "opus"})
    config = load_config()
    config.accounts.share_settings = True
    save_config(config)
    notes = service.share_for_launch(tmp_path, account=third)
    assert notes == ["account 3 now reads the plain claude's settings.json"]
    assert isinstance(notes[0], service.SharedNote)
    assert sharing.settings_path(third).is_symlink()
    assert not sharing.settings_path(second).exists()  # only the one launched
    assert service.share_for_launch(tmp_path, account=third) == []  # already
    assert service.share_for_launch(tmp_path, account=plain) == []  # it IS the source
