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
    return sharing.trusted_folders(json.loads(core.claude_json_path(account).read_text()))


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
    # Control: the same folder outside git is covered by that parent.
    shutil.rmtree(repo / ".git")
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
    monkeypatch.setattr(sharing.os, "name", "nt")
    assert sharing.project_key(native) == "C:/Users/me/work"
    monkeypatch.setattr(sharing.os, "name", "posix")
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
    data = json.loads(path.read_text())
    data["projects"][str(tmp_path / "older")]["hasTrustDialogAccepted"] = False
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    written = sharing.grant_trust(path, str(tmp_path / "older"), str(folder))

    assert written == [str(tmp_path / "older"), str(folder)]
    data["projects"][str(tmp_path / "older")]["hasTrustDialogAccepted"] = True
    data["projects"][str(folder)] = {"hasTrustDialogAccepted": True}
    assert path.read_text(encoding="utf-8") == json.dumps(data, indent=2, ensure_ascii=False)
    after = json.loads(path.read_text())
    assert after["oauthAccount"] == json.loads(before)["oauthAccount"]  # the login, untouched
    assert after["userID"] == "user-2"
    assert not path.with_name(".claude.json.lock").exists()  # the lock was let go
    # A second grant finds nothing to do and writes nothing.
    stamp = path.stat().st_mtime_ns
    assert sharing.grant_trust(path, str(folder)) == []
    assert path.stat().st_mtime_ns == stamp


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
    assert sharing.trusted_folders(json.loads(kept.read_text())) == [str(tmp_path)]


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
    before = json.loads(core.claude_json_path(third).read_text())
    monkeypatch.chdir(folder)

    result = runner.invoke(app, ["launch", "coder", "--account", "3"])

    assert result.exit_code == 0, result.output
    assert launched["env"][core.CONFIG_DIR_VAR] == str(third.config_dir)
    assert _trusted(third) == [key]
    after = json.loads(core.claude_json_path(third).read_text())
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
    assert f"✓ {root} on every account (it covers {os.path.realpath(sub)})" in human.stdout
    assert "account 3: already trusted" in human.stdout


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
    assert sharing.settings_path(third).is_symlink()
    assert not sharing.settings_path(second).exists()  # only the one launched
    assert service.share_for_launch(tmp_path, account=third) == []  # already
    assert service.share_for_launch(tmp_path, account=plain) == []  # it IS the source
