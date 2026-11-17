"""What the Claude accounts share: a folder's trust, and (opt-in) one settings.json.

``core.claude_sharing`` and the service around it, against fake homes only: every
account here is a directory under ``tmp_path`` (``fake_home`` for the plain claude,
``core.create_account`` for the managed slots). What a test asserts is the artefact —
the bytes of each account's ``.claude.json``, the link a ``settings.json`` became —
and the rules are the ones measured on Claude Code 2.1.299 (the module docstring).
"""

from __future__ import annotations

import json
import ntpath
import os
import shutil
import subprocess
import time
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import claude_sharing as sharing
from aisquare.core.agent_adapters import claude_screen
from aisquare.core.config import load_config, save_config
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as service
from tests import added_folder_screens as folder_screens
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


def test_a_worktree_not_made_yet_has_the_scope_it_will_have_once_made(tmp_path: Path) -> None:
    """Review of #259, round 22, 8/9: a spawn shares the trust before ``git worktree add``
    makes the tree. Asked of that path, ``trust_scope`` walked up into the main checkout and
    counted the repository's folders, an entry on any of which does not cover the tree once
    it is made. ``worktree_scope`` gives the scope the tree will have, from its root or from a
    sub-folder of the repository alike, and a tree that exists is read as it is."""
    repo = _repo(tmp_path / "repo")
    sub = repo / "sub"
    sub.mkdir()
    for root in (repo, sub):
        tree = root / ".wt" / "coder-1"
        before = sharing.worktree_scope(root, tree)
        assert before.covering == (_key(repo), _key(tree)), root
        assert _key(root) not in before.covering[1:] and before.key == _key(repo)
        assert sharing.trust_scope(tree).covering != before.covering, "the walk it replaces"
        _git("worktree", "add", "-q", "-b", f"b-{root.name}", str(tree), cwd=repo)
        assert sharing.worktree_scope(root, tree) == before == sharing.trust_scope(tree)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sharing.worktree_scope(plain, plain / "x") == sharing.trust_scope(plain / "x")


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
    assert "rlx accounts trust" in stderr
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


def _rows(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


ADDED = _rows("added_folder_trust_2.1.326.txt")
"""Claude Code 2.1.326 at its start, asking about an ``--add-dir`` folder no trust covers, as
``capture-pane -e`` showed a fleet window of it at 120 columns (R19); its temp path masked."""
ADDED_48 = _rows("added_folder_trust_2.1.326_48_columns.txt")
"""The same question at 48 columns, about a folder whose name has spaces and a word wider than
the pane: its rows wrap at the spaces, which they drop, and inside that word."""
COMPACT = _rows("added_folder_trust_2.1.326_compact_50x22.txt")
"""The same on a pane under 30 rows tall, which draws it with no blank row: at 50x22, about
the agent pane asq gives on an 80x24 terminal (review of #520). Its folder, its name with
spaces, wraps onto a second row, right over the question's first."""
COMPACT_ONE_ROW = _rows("added_folder_trust_2.1.326_compact_120x12.txt")
"""The same at 120x12: the folder on one row, the question's first row right under it."""
CUT_OFF = _rows("added_folder_trust_2.1.326_cut_off_50x14.txt")
"""The same on a pane too short for even that, 50x14: its top cut off, title and folder
and all, and the folder named under its key hint, wrapped (``Adding: …``)."""
CUT_OFF_SHORT = _rows("added_folder_trust_2.1.326_cut_off_50x8.txt")
"""And at 50x8: neither the question's words nor "Yes, add it" left; its "No, exit", the
key hint and the folder under it."""
REPOSITORY = _rows("added_folder_trust_2.1.326_repository.txt")
"""The same about a folder in a git repository, at 120 columns: a note under the folder,
over the question, says that trusting it trusts that whole repository."""
REPOSITORY_COMPACT = _rows("added_folder_trust_2.1.326_repository_compact_50x22.txt")
"""And at 50x22, the note right under the folder."""
HELD = _rows("added_folder_trust_2.1.326_held_50x22.txt")
"""The question at 50x22 once it refused a key (text typed into it, an Enter right after):
its key hint is ``Esc to exit \u00b7 To add it, press \u2193 to Yes, then Enter (your text is
kept)``, wrapped onto two rows."""
HELD_CUT_OFF = _rows("added_folder_trust_2.1.326_held_cut_off_50x8.txt")
"""And so at 50x8, cut off: that key hint over the folder."""
CUT_OFF_ALONE = _rows("added_folder_trust_2.1.326_cut_off_alone_50x8.txt")
"""Cut off at 50x8 about a folder 45 characters long with no space in it, which fits on no
row with ``Adding:`` but does on the next: ``Adding:`` alone, the folder under it (brief
217). Masked at the temp folder's own length, so its rows break where they did."""
ALONE = "/private/var/tmp/asq-native00/fffffffffffffff"
"""The folder that capture names."""
MASKED = "/private/var/tmp/asq-native"
SPACES = f"{MASKED}/extra dir with spaces"
"""The folder the compact, cut-off and held captures name, masked."""
IN_A_REPOSITORY = f"{MASKED}/other repo/sub dir"
"""The folder the repository captures name, masked."""
LAYOUTS = {
    "120 columns": ADDED,
    "48 columns": ADDED_48,
    "50x22": COMPACT,
    "120x12": COMPACT_ONE_ROW,
    "50x14": CUT_OFF,
    "50x8": CUT_OFF_SHORT,
    "in a repository, 120 columns": REPOSITORY,
    "in a repository, 50x22": REPOSITORY_COMPACT,
    "held, 50x22": HELD,
    "held, cut off, 50x8": HELD_CUT_OFF,
    "cut off, the folder on a row of its own, 50x8": CUT_OFF_ALONE,
}
"""Each layout of 2.1.326's question captured: measured from 40 rows down to 4, at 30, 48, 50,
80 and 120 columns. Under 30 rows it draws no blank row; where even that does not fit, it
cuts the top off and names the folder under its key hint, down to 4 rows, at which only the
key hint and the folder are left. A folder in a git repository has a note under it; a
question that refused a key says so in its key hint."""


@pytest.mark.parametrize("size", list(LAYOUTS))
def test_claude_code_2_1_326s_question_about_an_added_folder_is_its_folder_trust_question(
    size: str,
) -> None:
    """R19: 2.1.326 asks at its start about each ``--add-dir`` folder no trust covers, over a
    highlighted "No, exit". It was read as no question, so the fleet listed the agent a
    healthy ``working``. Read as the question now, fail-closed as the other one is: not with
    an input box under it. In each of its layouts: cut off on a pane too short for it, at
    50x8, it was read as no question (review of #520)."""
    screen = LAYOUTS[size]
    assert "\x1b[" in "".join(screen)  # as `capture-pane -e` gives it, escapes and all
    assert sharing.shows_trust_prompt(screen)
    assert sharing.shows_trust_prompt([*screen, "", "", ""])
    assert not sharing.shows_trust_prompt([*screen, "─" * 40, f"{CURSOR} ", "─" * 40])
    # Neither its words, its "Yes, add it", nor the folder under its key hint: no question.
    told = ("one you trust", "Yes, add it", "Adding:")
    assert not sharing.shows_trust_prompt([r for r in screen if not any(w in r for w in told)])


def test_the_question_s_words_or_the_folder_under_its_key_hint_make_it_the_question() -> None:
    """Its "No, exit" alone is not the question; with its words, or its "Yes, add it" (all a
    pane may show of it, as the screen reader reads it), it is. Cut off, the folder under its
    key hint is the question with no "No, exit" left, as at 4 rows."""
    assert not sharing.shows_trust_prompt([r for r in ADDED if "No, exit" not in r])
    assert sharing.shows_trust_prompt(ADDED[-8:])  # "No, exit", "Yes, add it", the key hint
    assert not sharing.shows_trust_prompt([r for r in ADDED[-8:] if "Yes, add it" not in r])
    assert sharing.shows_trust_prompt(CUT_OFF_SHORT[-3:])  # the key hint, the folder: 50x4
    # The folder line is the question only under its key hint, and above no input box.
    assert not sharing.shows_trust_prompt([f" Adding: {SPACES}", "with spaces"])
    folder_line = [r for r in CUT_OFF_SHORT if "No, exit" not in r]
    assert not sharing.shows_trust_prompt([*folder_line, "─" * 40, f"{CURSOR} ", "─" * 40])


def test_the_fleets_reader_and_the_screen_reader_agree_on_each_foot_of_the_question() -> None:
    """Review of #520: with only the foot of a question on the screen, the fleet's reader
    said no question where the screen reader (``agent_adapters.claude_screen``, ``say`` and
    ``press``) read it; and on a pane too short for it, the screen reader read nothing at
    all, while 2.1.326 had cut the question off and named the folder under its key hint.
    They agree on every foot of each capture now, but one: "Yes, …" under the key hint with
    the highlighted "No, exit" over it gone, which the screen reader refuses and the fleet's
    reader does not read. Claude Code never draws it: its "No, exit" stays down to a pane 5
    rows tall, and at 4 the folder under its key hint is left, which both read."""
    workspace = _rows("trust_prompt_2.1.299.txt")
    for size, screen in {"2.1.299": workspace, **LAYOUTS}.items():
        for count in range(1, len(screen) + 1):
            foot = screen[-count:]
            shown = [row.strip() for row in claude_screen.strip_escapes(foot) if row.strip()]
            read = claude_screen.prompt_showing(foot)
            screen_reader = read is not None and read.shape == "trust"
            fleet_reader = sharing.shows_trust_prompt(foot)
            asked = any("No, exit" in row or row.startswith("Adding: ") for row in shown)
            if not asked and any(row.startswith("Yes, ") for row in shown):
                assert (screen_reader, fleet_reader) == (True, False), (size, count)
            else:
                assert fleet_reader == screen_reader, (size, count, shown)


def test_the_screen_reader_names_the_question_by_its_title_or_cut_off_by_its_folder() -> None:
    """The question the screen reader names (``ScreenPrompt.question``): 2.1.326's title,
    "Adding a new directory:", or cut off, the folder under its key hint; the other one's
    "Quick safety check". Unpinned, the title read "the trust dialog" (review of #520)."""
    for screen in (ADDED, COMPACT, COMPACT_ONE_ROW):
        read = claude_screen.prompt_showing(screen)
        assert read is not None and read.question == "Adding a new directory:"
    read = claude_screen.prompt_showing(CUT_OFF)
    assert read is not None and read.question == f"Adding: {MASKED}/extra dir"
    read = claude_screen.prompt_showing(_rows("trust_prompt_2.1.299.txt"))
    assert read is not None and read.question.startswith("Quick safety check: Is this a project")


def test_the_folder_trust_question_names_the_added_folder_it_asks_about(tmp_path: Path) -> None:
    """The folder ``accounts trust`` must be told: the ``--add-dir`` folder 2.1.326's question
    names, not the one the agent starts in, which is trusted already; the question about
    that one asks about it. A folder wrapped across rows is the one way of joining them
    that is a directory here: the wrap drops a space, and breaks a word wider than the pane
    inside it."""
    start = tmp_path / "w"
    assert sharing.trust_prompt_folder(ADDED, start) == Path(f"{MASKED}/extra-dir")
    workspace = (FIXTURES / "trust_prompt_2.1.299.txt").read_text(encoding="utf-8").splitlines()
    assert sharing.trust_prompt_folder(workspace, start) == start
    assert sharing.trust_prompt_folder(workspace, None) is None

    long = f"{MASKED}/a long folder name {'x' * 50}/{'y' * 40}"
    asked: list[str] = []

    def is_dir(spelling: str) -> bool:
        asked.append(spelling)
        return spelling == long

    assert sharing.trust_prompt_folder(ADDED_48, start, is_dir=is_dir) == Path(long)
    assert long in asked and len(asked) <= 2 ** (4 - 1), "its four rows, joined every way"
    # A folder that is not on this machine is not guessed at: a command names `<dir>`.
    assert sharing.trust_prompt_folder(ADDED_48, start, is_dir=lambda _: False) is None
    # On disk, by default: a folder of the test's own, wrapped as Claude Code wraps it at 48
    # columns, wherever the test's temp folder is (one with spaces in it too; brief 217).
    folder = tmp_path / "two words" / "x"
    folder.mkdir(parents=True)
    rows = [f" \x1b[1m{row}\x1b[0m" for row in folder_screens.wrapped(str(folder), 48 - 2)]
    screen = [" Adding a new directory:", "", *rows]
    assert sharing.trust_prompt_folder([*screen, "", " This session…"], start) == folder


def test_the_question_names_its_folder_in_its_compact_and_cut_off_layouts(tmp_path: Path) -> None:
    """Review of #520: on a pane under 30 rows tall, 2.1.326 draws the question with no blank
    row, so the folder's rows ran on into the question's and no folder was named, even one
    on a single row that is a directory; cut off, the folder was the one the agent starts
    in. Its rows end at the question's first row now, or the note over it for a folder in a
    git repository, and cut off, the folder is the one named under its key hint."""
    start = tmp_path / "w"

    def never(spelling: str) -> bool:
        raise AssertionError(f"one row is the folder as it reads, not looked up: {spelling}")

    assert sharing.trust_prompt_folder(COMPACT_ONE_ROW, start, is_dir=never) == Path(SPACES)
    # In a git repository: its rows end at the note under them, over the question.
    for screen in (REPOSITORY, REPOSITORY_COMPACT):
        assert sharing.trust_prompt_folder(screen, start, is_dir=never) == Path(IN_A_REPOSITORY)
    # Wrapped: the one way of joining its rows that is a directory, under the title, under
    # the key hint cut off, and under the key hint that says a key was refused.
    for screen in (COMPACT, CUT_OFF, CUT_OFF_SHORT, HELD, HELD_CUT_OFF):
        asked: list[str] = []

        def is_dir(spelling: str, asked: list[str] = asked) -> bool:
            asked.append(spelling)
            return spelling == SPACES

        assert sharing.trust_prompt_folder(screen, start, is_dir=is_dir) == Path(SPACES)
        assert len(asked) <= 2, f"its two rows, joined both ways, and no more: {asked}"
        assert sharing.trust_prompt_folder(screen, start, is_dir=lambda _: False) is None


def test_the_question_names_no_folder_it_cannot_see_and_reads_rows_it_cannot_end() -> None:
    """Neither its title nor the folder under its key hint on a screen of 2.1.326's question
    (its foot alone): no folder, so a command names ``<dir>``, never the folder the agent
    starts in, which is trusted. Rows whose end is not seen, as when a build to come words
    the question's first row otherwise: the most of the first of them that is a directory."""
    start = Path("/w")
    assert sharing.shows_trust_prompt(ADDED[-8:])
    assert sharing.trust_prompt_folder(ADDED[-8:], start) is None
    reworded = [row.replace("This session hasn", "Its session hasn") for row in COMPACT]
    assert reworded != COMPACT
    first = f"{MASKED}/extra dir with"
    both = {SPACES, first}
    assert sharing.trust_prompt_folder(reworded, start, is_dir=both.__contains__) == Path(SPACES)
    assert sharing.trust_prompt_folder(reworded, start, is_dir={first}.__contains__) == Path(first)
    assert sharing.trust_prompt_folder(reworded, start, is_dir=lambda _: False) is None


DRAWN = {
    "120 columns": (ADDED, f"{MASKED}/extra-dir", 120),
    "50x22": (COMPACT, SPACES, 50),
    "120x12": (COMPACT_ONE_ROW, SPACES, 120),
    "50x14": (CUT_OFF, SPACES, 50),
    "50x8": (CUT_OFF_SHORT, SPACES, 50),
    "in a repository, 50x22": (REPOSITORY_COMPACT, IN_A_REPOSITORY, 50),
    "held, 50x22": (HELD, SPACES, 50),
    "held, cut off, 50x8": (HELD_CUT_OFF, SPACES, 50),
    "the folder on a row of its own, 50x8": (CUT_OFF_ALONE, ALONE, 50),
}
"""Each capture, the folder it names and its width: what ``tests.added_folder_screens.drawn``
draws another folder into."""


@pytest.mark.parametrize("size", list(DRAWN))
def test_a_folder_drawn_into_a_capture_is_drawn_as_claude_code_draws_it(size: str) -> None:
    """Brief 217: the tests that read the folder off the disk draw a real one into a capture
    (``tests.added_folder_screens``), wrapped as Claude Code's renderer wraps it (Bun's
    ``wrap-ansi``: at a space, and a word wider than the pane broken inside it). That model
    finds each capture's own folder where Claude Code drew it, row for row."""
    screen, masked, columns = DRAWN[size]
    again = folder_screens.drawn(screen, masked, masked, columns)
    assert folder_screens.plain(again) == folder_screens.plain(screen)
    # The 48-column capture's temp folder was masked 2 characters short, which moves a break
    # that fell at a full row; at its own length the model breaks it as Claude Code did.
    real = f"/private/var/tmp/asq-abcdefgh/a long folder name {'x' * 50}/{'y' * 40}"
    rows = [row.strip() for row in folder_screens.wrapped(real, 48 - 2)]
    shown = [row.strip() for row in folder_screens.plain(ADDED_48)[4:8]]
    assert rows[1:] == shown[1:] and rows[0] == shown[0].replace("asq-native", "asq-abcdefgh")


def test_a_folder_on_a_row_of_its_own_under_adding_is_the_folder_cut_off() -> None:
    """Brief 217, measured on 2.1.326: cut off at 50 columns, a folder 45 characters long with
    no space in it starts a row of its own, under ``Adding:`` alone. Both readers read the
    question there now, and the folder; before, it was no question, its folder the one the
    agent starts in, and the row read a healthy ``working``."""

    def never(spelling: str) -> bool:
        raise AssertionError(f"one row is the folder as it reads, not looked up: {spelling}")

    assert sharing.shows_trust_prompt(CUT_OFF_ALONE)
    assert sharing.trust_prompt_folder(CUT_OFF_ALONE, Path("/w"), is_dir=never) == Path(ALONE)
    read = claude_screen.prompt_showing(CUT_OFF_ALONE)
    assert read is not None and read.shape == "trust"
    assert claude_screen.modal_showing(CUT_OFF_ALONE) == "the trust dialog"
    # Wrapped under it, as a longer one breaks: still the one way that is a directory.
    longer = f"/private/var/tmp/asq-native00/{'f' * 60}/x y"
    screen = folder_screens.drawn(CUT_OFF_ALONE, ALONE, longer, 50)
    assert sharing.shows_trust_prompt(screen)
    assert sharing.trust_prompt_folder(screen, Path("/w"), is_dir={longer}.__contains__) == Path(
        longer
    )


WINDOWS_FOLDERS = [
    str(PureWindowsPath(r"C:\Users\me\Work\extra dir with spaces")),
    str(PureWindowsPath(r"C:\Users\RUNNER~1\AppData\Local\Temp\pytest-of-me\pytest-0") / "w x"),
    str(PureWindowsPath(r"D:\work\rlx\abcdefghijklmnopqrstuvwxyz0123456789")),
]
"""Folders as Windows spells them (``PureWindowsPath``): with spaces; long enough to break
inside a word and to wrap onto rows; and 45 characters with no space, which starts a row of
its own under ``Adding:``."""


@pytest.mark.parametrize("folder", WINDOWS_FOLDERS)
@pytest.mark.parametrize("size", list(DRAWN))
def test_a_windows_folder_on_the_question_is_read_or_named_dir_never_misnamed(
    size: str, folder: str
) -> None:
    """Brief 217: the fleet runs where tmux does (on Windows, under WSL, with POSIX paths),
    but a folder Windows spells (``C:\\…``) drawn on the question must not be misread: the
    screen is the question, so nothing is typed into it; its folder is the one drawn, found
    with the disk's own case rules (``ntpath``), or none (``<dir>``) where it is not one;
    never the folder the agent starts in."""
    screen, masked, columns = DRAWN[size]
    drawn = folder_screens.drawn(screen, masked, folder, columns)
    start = Path("/the/folder/it/starts/in")
    assert sharing.shows_trust_prompt(drawn)
    read = claude_screen.prompt_showing(drawn)
    assert read is not None and read.shape == "trust" and read.yes_key is None

    def is_dir(spelling: str) -> bool:
        return ntpath.normcase(spelling) == ntpath.normcase(folder)

    found = sharing.trust_prompt_folder(drawn, start, is_dir=is_dir)
    assert found is not None and str(found) == str(Path(folder)), found
    missing = sharing.trust_prompt_folder(drawn, start, is_dir=lambda _: False)
    assert missing in (None, Path(folder)), "the one drawn, or none"
    assert missing != start


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
    head = f"✓ {root} on every account that can hold it (it covers {os.path.realpath(sub)})"
    assert head in human.stdout
    assert "account 3: already trusted" in human.stdout


def test_accounts_trust_heads_its_report_with_the_accounts_that_can_hold_it(
    three: tuple[ClaudeAccount, ...], tmp_path: Path, runner: CliRunner
) -> None:
    """Final gate NIT 2: the heading said '✓ <key> on every account' over a row that said
    'account 3: skipped': an account Claude Code never started on is given nothing. It then
    said '… on every account that has started', and a Codex home that never started is given
    the table, in a config.toml made for it (SMOKE7 of #113): every account that can hold
    it."""
    _plain, _second, third = three
    core.claude_json_path(third).unlink()  # never started
    folder = tmp_path / "work"
    folder.mkdir()
    result = runner.invoke(app, ["accounts", "trust", str(folder)])
    assert result.exit_code == 0, result.output
    assert f"✓ {_key(folder)} on every account that can hold it\n" in result.stdout
    assert "  · account 3: skipped" in result.stdout
    synced = runner.invoke(app, ["accounts", "trust", "--sync"])
    assert synced.exit_code == 0, synced.output
    assert "✓ every trusted folder on every account that can hold it\n" in synced.stdout
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


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_a_second_link_within_the_same_second_keeps_the_first_copy(
    three: tuple[ClaudeAccount, ...],
) -> None:
    """Review of #259, round 32, 8/9: the account's own settings.json was kept as
    ``settings.json.own-<second>`` and copied over whatever held that name. A sync, an
    unshare (which writes the shared content as the account's own) and a sync again within
    one second replaced the copy of the account's original with the shared settings. Each
    copy is created anew, the second under ``-2``."""
    from datetime import UTC, datetime

    plain, second, _ = three
    source = _settings(plain, {"model": "opus"})
    _settings(second, {"model": "sonnet"})
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

    first = sharing.link_settings(second, source, now=now)
    assert sharing.unlink_settings(second, source)
    again = sharing.link_settings(second, source, now=now)

    assert first is not None and first.name == "settings.json.own-20261016T120000Z"
    assert again is not None and again.name == "settings.json.own-20261016T120000Z-2"
    assert json.loads(first.read_text()) == {"model": "sonnet"}, "the original is kept"
    assert json.loads(again.read_text()) == {"model": "opus"}


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


# --- [accounts] share: skills, agents, commands, CLAUDE.md, memory (P2-W7, #198) ----------

NOW = __import__("datetime").datetime(2026, 10, 23, 12, 0, tzinfo=__import__("datetime").UTC)


def _tree(root: Path) -> dict[str, str | None]:
    """Every entry under ``root``: a file's text, a link's target, ``None`` for a dir."""
    found: dict[str, str | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[rel] = "->" + os.readlink(path)
        elif path.is_file():
            found[rel] = path.read_text(encoding="utf-8")
        elif path.is_dir():
            found[rel] = None
    return found


@pytest.fixture
def skilled(three: tuple[ClaudeAccount, ...]) -> tuple[ClaudeAccount, ...]:
    """The plain claude with a skill and a CLAUDE.md; slot 2 with its own skill and CLAUDE.md;
    slot 3 with none of either."""
    if not can_symlink():  # pragma: no cover - a privilege CI holds, a laptop may not
        pytest.skip("this machine cannot create symlinks")
    plain, second, _third = three
    (plain.config_dir / "skills" / "shared-skill").mkdir(parents=True)
    (plain.config_dir / "skills" / "shared-skill" / "SKILL.md").write_text("shared\n")
    (plain.config_dir / "CLAUDE.md").write_text("plain rules\n")
    (second.config_dir / "skills" / "mine").mkdir(parents=True)
    (second.config_dir / "skills" / "mine" / "SKILL.md").write_text("slot two's\n")
    (second.config_dir / "CLAUDE.md").write_text("slot two rules\n")
    return three


def test_enable_links_every_slot_and_keeps_its_own_and_disable_restores_it_exactly(
    skilled: tuple[ClaudeAccount, ...],
) -> None:
    _plain, second, third = skilled
    before = {account.slot: _tree(account.config_dir) for account in (second, third)}

    changes = service.enable_share(["skills", "claude_md"], now=NOW)

    outcome = {(c.account.slot, c.part): (c.outcome, c.detail) for c in changes}
    assert outcome == {
        (2, "skills"): ("linked", "skills.own-20261023T120000Z"),
        (3, "skills"): ("linked", None),
        (2, "claude_md"): ("linked", "CLAUDE.md.own-20261023T120000Z"),
        (3, "claude_md"): ("linked", None),
    }
    for account in (second, third):
        assert (
            account.config_dir / "skills" / "shared-skill" / "SKILL.md"
        ).read_text() == "shared\n"
        assert (account.config_dir / "CLAUDE.md").read_text() == "plain rules\n"
    kept = second.config_dir / "skills.own-20261023T120000Z"
    assert (kept / "mine" / "SKILL.md").read_text() == "slot two's\n"
    assert load_config().accounts.share == ["skills", "claude_md"]

    again = service.enable_share(["skills", "claude_md"], now=NOW)
    assert {c.outcome for c in again} == {"already"}, "idempotent"

    service.disable_share(["skills", "claude_md"])

    assert {account.slot: _tree(account.config_dir) for account in (second, third)} == before
    assert load_config().accounts.share == []
    again_off = service.disable_share(["skills"])
    assert [(c.account.slot, c.outcome) for c in again_off] == [(2, "own"), (3, "none")]
    assert {account.slot: _tree(account.config_dir) for account in (second, third)} == before


def test_an_entry_linked_elsewhere_is_refused_and_left_alone(
    skilled: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    _plain, second, _third = skilled
    theirs = tmp_path / "someone-elses-agents"
    theirs.mkdir()
    (second.config_dir / "agents").symlink_to(theirs, target_is_directory=True)

    [refused] = [c for c in service.enable_share(["agents"], now=NOW) if c.account.slot == 2]

    assert refused.outcome == "refused" and "not ours to replace" in (refused.detail or "")
    assert f"links to {theirs} " in (refused.detail or "")
    # Still linked there: by realpath, since Windows reads a link back as ``\\?\C:\...``.
    entry = second.config_dir / "agents"
    assert sharing._is_link(entry) and os.path.realpath(entry) == os.path.realpath(theirs)


def test_a_refusal_names_the_link_as_written_without_windows_prefix(
    skilled: tuple[ClaudeAccount, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows reads a link back with its extended-length prefix (run 40073711742): the
    refusal names the path as the operator wrote it."""
    _plain, second, _third = skilled
    theirs = tmp_path / "someone-elses-agents"
    theirs.mkdir()
    (second.config_dir / "agents").symlink_to(theirs, target_is_directory=True)
    real_readlink = os.readlink
    prefix = "\\\\?\\"
    # Windows' own readlink already has it (rc/p2 push run 40087282716): once, as there.
    monkeypatch.setattr(
        os, "readlink", lambda path: prefix + real_readlink(path).removeprefix(prefix)
    )

    [refused] = [c for c in service.enable_share(["agents"], now=NOW) if c.account.slot == 2]

    assert refused.outcome == "refused"
    assert f"links to {theirs} — not ours to replace" in (refused.detail or ""), refused.detail


def test_a_refusal_names_a_share_as_written(
    skilled: tuple[ClaudeAccount, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link to a share reads back on Windows as ``UNC`` under the extended-length prefix
    (review of #386): the refusal names it by its two leading backslashes, as typed."""
    _plain, second, _third = skilled
    theirs = tmp_path / "someone-elses-agents"
    theirs.mkdir()
    (second.config_dir / "agents").symlink_to(theirs, target_is_directory=True)
    monkeypatch.setattr(os, "readlink", lambda path: "\\\\?\\UNC\\srv\\team\\agents")

    [refused] = [c for c in service.enable_share(["agents"], now=NOW) if c.account.slot == 2]

    assert "links to \\\\srv\\team\\agents — not ours" in (refused.detail or ""), refused.detail


def test_on_windows_a_directory_is_a_junction_when_a_symlink_needs_a_privilege(
    skilled: tuple[ClaudeAccount, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _plain, _second, third = skilled
    made: list[tuple[Path, Path]] = []
    real_symlink = os.symlink

    def refuse(src: object, dst: object, target_is_directory: bool = False) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(sharing, "_platform", lambda: "nt")
    monkeypatch.setattr(os, "symlink", refuse)

    def junction(source: Path, entry: Path) -> None:
        made.append((source, entry))
        real_symlink(source, entry, target_is_directory=True)  # stands in for the junction

    monkeypatch.setattr(sharing, "_junction", junction)
    change = service._link_part(third, "agents", core.default_account().config_dir, NOW)
    assert change.outcome == "linked"
    assert made == [(core.default_account().config_dir / "agents", third.config_dir / "agents")]


def test_a_launch_links_a_slot_made_after_the_part_was_turned_on(
    skilled: tuple[ClaudeAccount, ...],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    service.enable_share(["skills"], now=NOW)
    fourth = core.create_account()
    _write_claude_json(fourth, "four@x.dev")
    folder = tmp_path / "work"
    folder.mkdir()
    monkeypatch.chdir(folder)

    result = runner.invoke(app, ["launch", "coder", "--account", str(fourth.slot)])

    assert result.exit_code == 0, result.output
    assert sharing.part_state(fourth.config_dir, "skills", core.default_account().config_dir) == (
        "shared"
    )
    assert "now shares the plain claude's skills" in result.stderr


def test_memory_on_every_launch_carries_the_shared_directory_for_its_project(
    three: tuple[ClaudeAccount, ...],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    folder = tmp_path / "work.repo"
    folder.mkdir()
    monkeypatch.chdir(folder)
    service.enable_share(["memory"])
    for slot in ("1", "3"):
        result = runner.invoke(app, ["launch", "coder", "--account", slot])
        assert result.exit_code == 0, result.output
        argv = launched["argv"]
        assert argv.count("--settings") == 1, "one --settings for every key aisquare sets"
        settings = json.loads(argv[argv.index("--settings") + 1])
        want = service.shared_memory_root() / sharing.memory_key(Path.cwd())
        # The coder's skill listing (P1 WS-1) and the shared memory (P2-W7), both: the
        # memory stood down on the role's own --settings, so no coder shared any (sync #367).
        assert settings == {"skillListingMaxDescChars": 1, "autoMemoryDirectory": str(want)}
        assert sharing.memory_key(Path.cwd()).endswith("-work-repo")
    # The operator's own --settings wins over both, and the launch says why.
    result = runner.invoke(app, ["launch", "coder", "--account", "3", "--settings", "{}"])
    assert launched["argv"].count("--settings") == 1
    assert launched["argv"][launched["argv"].index("--settings") + 1] == "{}"
    assert "its own --settings wins" in result.stderr
    # A manager keeps the full skill listing: its --settings is the memory's alone.
    result = runner.invoke(app, ["launch", "manager", "--account", "3"])
    assert result.exit_code == 0, result.output
    argv = launched["argv"]
    assert argv.count("--settings") == 1
    assert json.loads(argv[argv.index("--settings") + 1]) == {"autoMemoryDirectory": str(want)}


def test_memory_off_adds_nothing_to_a_launch(
    three: tuple[ClaudeAccount, ...],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["launch", "coder", "--account", "3"])
    argv = launched["argv"]
    # The coder's own skill listing (P1 WS-1) and nothing of the memory's.
    assert argv.count("--settings") == 1
    assert json.loads(argv[argv.index("--settings") + 1]) == {"skillListingMaxDescChars": 1}
    runner.invoke(app, ["launch", "manager", "--account", "3"])
    assert "--settings" not in launched["argv"]


def test_the_first_memory_enable_merges_every_slots_memory_without_overwriting(
    three: tuple[ClaudeAccount, ...],
) -> None:
    plain, second, third = three
    key = "-work-repo"

    def memory(account: ClaudeAccount, files: dict[str, str]) -> None:
        folder = account.config_dir / "projects" / key / "memory"
        folder.mkdir(parents=True)
        for name, text in files.items():
            (folder / name).write_text(text, encoding="utf-8")

    memory(plain, {"MEMORY.md": "- [Tabs](tabs.md)\n", "tabs.md": "use tabs\n"})
    memory(
        second,
        {
            "MEMORY.md": "- [Tabs](tabs.md)\n- [Tests](tests.md)\n",
            "tabs.md": "use spaces\n",
            "tests.md": "pytest\n",
        },
    )
    memory(third, {"tests.md": "pytest\n"})

    changes = service.enable_share(["memory"])

    shared = service.shared_memory_root() / key
    assert (shared / "MEMORY.md").read_text() == "- [Tabs](tabs.md)\n- [Tests](tests.md)\n"
    assert (shared / "tabs.md").read_text() == "use tabs\n", "the first one in stays"
    assert (shared / "tabs.slot2.md").read_text() == "use spaces\n", "the other is kept beside"
    assert (shared / "tests.md").read_text() == "pytest\n"
    assert [(c.account.slot, c.outcome) for c in changes] == [(2, "merged")]
    assert service.enable_share(["memory"]) == [], "only the first enable merges"


def test_share_cli_json_status_enable_disable(
    skilled: tuple[ClaudeAccount, ...], runner: CliRunner
) -> None:
    on = runner.invoke(app, ["--json", "accounts", "share", "--enable", "agents,memory"])
    assert on.exit_code == 0, on.output
    report = json.loads(on.output)
    assert report["share"] == ["memory", "agents"]
    assert {(c["slot"], c["part"], c["outcome"]) for c in report["changes"]} == {
        (2, "agents", "linked"),
        (3, "agents", "linked"),
    }
    status = json.loads(runner.invoke(app, ["--json", "accounts", "share", "--status"]).output)
    held = {(c["slot"], c["part"]): c["outcome"] for c in status["status"]}
    assert held[(2, "agents")] == "shared" and held[(1, "memory")] == "shared"
    bad = runner.invoke(app, ["accounts", "share", "--enable", "transcripts"])
    assert bad.exit_code == 2 and "parts are memory" in bad.output
    off = runner.invoke(app, ["--json", "accounts", "share", "--disable", "agents"])
    assert json.loads(off.output)["share"] == ["memory"]


def test_the_doctor_row_warns_on_a_part_a_slot_does_not_share_and_a_linked_projects(
    skilled: tuple[ClaudeAccount, ...], tmp_path: Path
) -> None:
    from aisquare.services import diagnostics

    _plain, second, third = skilled
    managed = core.managed_accounts()
    assert diagnostics._claude_accounts_sharing_check(managed) is None, "nothing shared: no row"

    service.enable_share(["skills", "memory"], now=NOW)
    row = diagnostics._claude_accounts_sharing_check(managed)
    assert row is not None and row.status.value == "ok"
    assert row.detail.startswith("sharing skills, memory; memory in ")

    sharing.unlink_part(third.config_dir, "skills", core.default_account().config_dir)
    (second.config_dir / "projects").symlink_to(
        core.default_account().config_dir, target_is_directory=True
    )
    row = diagnostics._claude_accounts_sharing_check(managed)
    assert row is not None and row.status.value == "warn"
    assert "slot 3 has no skills linked yet" in row.detail
    assert "slot 2's projects/ is a symlink" in row.detail


# --- review of #360's minors -------------------------------------------------------------


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_sharing_a_part_the_plain_claude_has_none_of_keeps_the_slots_own_in_sight(
    tmp_path: Path,
) -> None:
    """Linked to an empty skills/ the plain claude was given, a slot's own skills vanished
    from it. What the slot has and the plain claude lacks goes there first, never over an
    entry it has; the slot's own copy is kept beside as before."""
    plain, slot = tmp_path / "plain", tmp_path / "slot"
    (slot / "skills" / "deploy").mkdir(parents=True)
    (slot / "skills" / "deploy" / "SKILL.md").write_text("mine\n")
    (slot / "skills" / "review").mkdir()
    (slot / "skills" / "review" / "SKILL.md").write_text("slot's review\n")
    (plain / "skills" / "review").mkdir(parents=True)
    (plain / "skills" / "review" / "SKILL.md").write_text("plain's review\n")

    kept = sharing.link_part(slot, "skills", plain, now=NOW)

    seen = slot / "skills"
    assert (seen / "deploy" / "SKILL.md").read_text() == "mine\n", "the slot's own, in sight"
    assert (seen / "review" / "SKILL.md").read_text() == "plain's review\n", "never over it"
    assert kept is not None and (kept / "review" / "SKILL.md").read_text() == "slot's review\n"


def test_a_differing_note_is_kept_under_a_name_that_stays_markdown(tmp_path: Path) -> None:
    """The fallback was ``note.slotb.md-2``, which no longer reads as a .md, and the note
    named the first candidate, not the file written."""
    target, a, b = tmp_path / "shared", tmp_path / "a", tmp_path / "b"
    for directory, body in ((a, "one\n"), (b, "two\n")):
        directory.mkdir()
        (directory / "note.md").write_text(body)
    target.mkdir()
    (target / "note.md").write_text("zero\n")
    (target / "note.slotb.md").write_text("an older different copy\n")

    notes = sharing.merge_memory([("slota", a), ("slotb", b)], target)

    assert (target / "note.slota.md").read_text() == "one\n"
    assert (target / "note.slotb-2.md").read_text() == "two\n"
    assert ("slotb", "note.md differed: kept as note.slotb-2.md") in notes
    assert sharing.merge_memory([("slotb", b)], target) == [], "the same bytes: kept once"


def test_merging_memory_md_keeps_its_blank_lines(tmp_path: Path) -> None:
    target, a, b = tmp_path / "shared", tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "MEMORY.md").write_text("# Index\n\n- [one](one.md)\n\n- [two](two.md)\n")
    (b / "MEMORY.md").write_text("# Index\n\n- [two](two.md)\n\n## Later\n\n- [three](three.md)\n")

    sharing.merge_memory([("a", a), ("b", b)], target)

    # The first index whole, blank lines and all; then what the second adds, its own blank
    # lines kept between its new lines.
    assert (target / "MEMORY.md").read_text() == (
        "# Index\n\n- [one](one.md)\n\n- [two](two.md)\n## Later\n\n- [three](three.md)\n"
    )
