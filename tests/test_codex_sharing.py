"""Codex accounts share folder trust and re-login as Claude Code's do (SCOPE #113, 0.8.1 items 15
and 11): the ``[projects."<dir>"]`` table between the slots' ``config.toml``, and a *Log in* over
an ``auth.json`` still on disk that lands only once its tokens change.

The trust rules are the ones measured on Codex 0.162.1 (``core.codex_sharing``); every home
here is synthetic, and no ``codex`` runs.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import claude_sharing, codex_sharing
from aisquare.core.agent_adapters.types import trusted_by
from aisquare.models import ClaudeAccount, ProjectInfo
from aisquare.services import claude_accounts as service
from aisquare.services import fleet as fleet_service
from tests.fsperms import can_symlink
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - a fixture, by name
from tests.test_codex_accounts import _sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _git,
    clock,
    project,
    repo,
    tmux,
)

#: A slot's config.toml as an operator keeps it: a comment, top-level keys, tables, and the
#: odd spacing Codex itself keeps when it edits the file in place.
SETTINGS = (
    "# my Codex settings\n"
    'model = "gpt-6.1-sol"\n'
    'approval_policy = "on-request"   # asked, then\n'
    "\n"
    "[model_providers.local]\n"
    'name = "Local"\n'
    'base_url = "http://127.0.0.1:9/v1"\n'
    "\n"
    "[tui]\n"
    "screen_reader_detection_done = true"
)


def _table(key: str, level: str = "trusted") -> str:
    return f"\n[projects.{json.dumps(key)}]\ntrust_level = {json.dumps(level)}\n"


def _slot(email: str, settings: str | None = SETTINGS) -> ClaudeAccount:
    account = core.create_account("codex")
    _sign_in(account.config_dir, email)
    if settings is not None:
        codex_sharing.config_path(account.config_dir).write_text(settings, encoding="utf-8")
    return account


def _config(account: ClaudeAccount) -> str:
    return codex_sharing.config_path(account.config_dir).read_text(encoding="utf-8")


def _real(path: Path) -> str:
    return os.path.realpath(path)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A repository with a sub-folder and a worktree, as a fleet's project has."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    (root / "src").mkdir()
    _git("worktree", "add", "-q", "-b", "one", str(root / ".trees" / "one"), cwd=root)
    return root


# --- core: the rules and the file --------------------------------------------------------


def test_codex_reads_the_folders_own_entry_then_its_repository_roots_and_nothing_else(
    checkout: Path,
) -> None:
    """Measured on 0.162.1: an entry on the folder itself decides first, then the one on
    its repository's canonical root (a worktree's is the main checkout's); a parent's, or
    a worktree's for the main checkout, covers nothing."""
    root, sub, tree = _real(checkout), _real(checkout / "src"), _real(checkout / ".trees/one")
    in_sub = codex_sharing.scope(claude_sharing.trust_scope(checkout / "src"))
    assert (in_sub.key, in_sub.covering) == (root, (sub, root))
    in_tree = codex_sharing.scope(claude_sharing.trust_scope(checkout / ".trees" / "one"))
    assert (in_tree.key, in_tree.covering) == (root, (tree, root))
    unmade = claude_sharing.worktree_scope(checkout, checkout / ".trees" / "two")
    assert codex_sharing.scope(unmade).covering == (_real(checkout / ".trees/two"), root)

    assert trusted_by({root: True}, in_sub) == root
    assert trusted_by({sub: True}, in_sub) == sub
    assert trusted_by({_real(checkout.parent): True, tree: True}, in_sub) is None
    assert trusted_by({sub: False, root: True}, in_sub) is None, "its own no decides"
    assert trusted_by({sub: True, root: False}, in_sub) == sub


def test_a_yes_is_a_table_appended_and_every_byte_before_it_stays(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    config = codex_sharing.config_path(home)
    config.write_text(SETTINGS, encoding="utf-8")  # no newline at its end, as a hand edit
    before = tomllib.loads(SETTINGS)

    assert codex_sharing.grant_trust(home, "/work/repo", "/work/other") == [
        "/work/repo",
        "/work/other",
    ]

    text = config.read_text(encoding="utf-8")
    assert text == SETTINGS + "\n" + _table("/work/repo") + _table("/work/other")
    after = tomllib.loads(text)
    assert after.pop("projects") == {
        "/work/repo": {"trust_level": "trusted"},
        "/work/other": {"trust_level": "trusted"},
    }
    assert after == before, "the top-level keys stay top-level: never prepended"
    assert codex_sharing.read_trust(home) == {"/work/repo": True, "/work/other": True}
    assert codex_sharing.grant_trust(home, "/work/repo") == []
    assert config.read_text(encoding="utf-8") == text


def test_a_home_without_config_toml_gets_one_holding_the_table_alone(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    assert codex_sharing.read_trust(home) == {}, "Codex runs without one"
    assert codex_sharing.grant_trust(home, "/work/repo") == ["/work/repo"]
    assert codex_sharing.config_path(home).read_text(encoding="utf-8") == (
        '[projects."/work/repo"]\ntrust_level = "trusted"\n'
    )


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (SETTINGS + _table("/work/repo", "untrusted"), 'trust_level = "untrusted"'),
        (SETTINGS + '\n[projects."/work/repo"]\nmodel = "x"\n', "has a table for /work/repo"),
        ('projects = { "/elsewhere" = { trust_level = "trusted" } }\n', "at its end"),
        ("projects = 3\n", "not a table"),
        ("model = \n", "not valid TOML"),
    ],
    ids=["its-own-no", "its-own-table", "inline-projects", "projects-not-a-table", "not-toml"],
)
def test_a_file_a_table_cannot_be_appended_to_is_left_as_it_was(
    tmp_path: Path, body: str, why: str
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    config = codex_sharing.config_path(home)
    config.write_text(body, encoding="utf-8")
    with pytest.raises(claude_sharing.SharingError, match=why):
        codex_sharing.grant_trust(home, "/work/repo")
    assert config.read_text(encoding="utf-8") == body


@pytest.mark.skipif(not can_symlink(), reason="needs symlinks")
def test_a_linked_config_toml_is_written_through_and_stays_a_link(tmp_path: Path) -> None:
    shared = tmp_path / "dotfiles" / "codex.toml"
    shared.parent.mkdir()
    shared.write_text(SETTINGS + "\n", encoding="utf-8")
    home = tmp_path / "codex"
    home.mkdir()
    codex_sharing.config_path(home).symlink_to(shared)
    codex_sharing.grant_trust(home, "/work/repo")
    assert codex_sharing.config_path(home).is_symlink()
    assert shared.read_text(encoding="utf-8") == SETTINGS + "\n" + _table("/work/repo")


# --- the service: a launch, a spawn, accounts trust ------------------------------------


def test_a_codex_launch_gets_the_trust_another_codex_slot_gave_and_says_so(
    checkout: Path, fake_home: Path
) -> None:
    """Slot 2 said yes to the repository; slot 3, never asked, is given the table at its
    launch, and nothing else of its config.toml changes."""
    root = _real(checkout)
    given, asked = _slot("two@x.dev"), _slot("three@x.dev")
    codex_sharing.grant_trust(given.config_dir, root)
    before = _config(asked)

    notes = service.share_for_launch(checkout / "src", account=asked)

    assert notes == [f"{root} is trusted on account 2 — now on account 3 too"]
    assert isinstance(notes[0], service.SharedNote)
    assert _config(asked) == before + "\n" + _table(root)
    assert service.share_for_launch(checkout / "src", account=asked) == []
    # Without an account the launch's CODEX_HOME says which slot it is.
    third = _slot("four@x.dev")
    env = {"CODEX_HOME": str(third.config_dir)}
    assert service.share_for_launch(checkout, env=env, agent="codex") == [
        f"{root} is trusted on account 2 — now on account 4 too"
    ]
    assert not core.claude_json_path(core.default_account()).exists(), "no Claude file made"


def test_a_codex_no_is_never_overridden_and_accounts_trust_says_so(
    checkout: Path, fake_home: Path, runner: CliRunner
) -> None:
    root = _real(checkout)
    given = _slot("two@x.dev")
    refused = _slot("three@x.dev", settings=SETTINGS + _table(root, "untrusted"))
    codex_sharing.grant_trust(given.config_dir, root)
    before = _config(refused)

    assert service.share_for_launch(checkout, account=refused) == []
    assert _config(refused) == before

    result = runner.invoke(app, ["accounts", "trust", str(checkout)])
    assert result.exit_code == 1
    assert (
        f"account 3: NOT trusted — its config.toml says not to trust {root} — change it there"
        in result.stdout
    )
    assert _config(refused) == before


def test_accounts_trust_and_sync_reach_every_codex_slot_in_its_own_file(
    checkout: Path, fake_home: Path, runner: CliRunner
) -> None:
    """``accounts trust <dir>`` writes each agent's yes in its own file; ``--sync`` copies
    a folder one Codex slot trusts into the others, never into a Claude account's file."""
    root = _real(checkout)
    two, three = _slot("two@x.dev"), _slot("three@x.dev", settings=None)

    result = runner.invoke(app, ["--json", "accounts", "trust", str(checkout / "src")])

    assert result.exit_code == 0, result.output
    rows = {row["slot"]: row for row in json.loads(result.stdout)["accounts"]}
    assert rows[two.slot]["outcome"] == rows[three.slot]["outcome"] == "trusted"
    assert rows[two.slot]["agent"] == "codex" and rows[two.slot]["folders"] == [root]
    assert codex_sharing.read_trust(two.config_dir) == {root: True}
    assert _config(three) == _table(root).lstrip("\n")

    elsewhere = checkout.parent / "elsewhere"
    elsewhere.mkdir()
    _git("init", "-q", "-b", "main", cwd=elsewhere)
    codex_sharing.grant_trust(two.config_dir, _real(elsewhere))
    synced = runner.invoke(app, ["--json", "accounts", "trust", "--sync"])
    assert synced.exit_code == 0, synced.output
    assert codex_sharing.read_trust(three.config_dir) == {root: True, _real(elsewhere): True}
    plain = core.claude_json_path(core.default_account())
    assert not plain.exists(), "Codex's yes is Codex's: no Claude account is given it"


def test_sync_gives_a_codex_slot_every_folder_but_the_one_its_file_refuses(
    tmp_path: Path, fake_home: Path, runner: CliRunner
) -> None:
    """Review r16 of #113, 4/8: ``--sync`` granted every missing folder in one write, and a
    ``[projects."<key>"]`` table of the slot's own with no ``trust_level`` refused it whole:
    the account was ``failed``, given none of the others. Each folder is its own grant now,
    the one refused reported, the rest written."""
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    given = _slot("two@x.dev")
    codex_sharing.grant_trust(given.config_dir, _real(one), _real(two))
    held = _slot("three@x.dev", settings=SETTINGS + f"\n[projects.{json.dumps(_real(two))}]\n")
    assert codex_sharing.read_trust(held.config_dir) == {}, "the premise: no trust_level"

    synced = runner.invoke(app, ["--json", "accounts", "trust", "--sync"])

    assert synced.exit_code == 1, synced.output
    rows = [row for row in json.loads(synced.stdout)["accounts"] if row["slot"] == held.slot]
    assert [(row["outcome"], row["folders"]) for row in rows] == [
        ("trusted", [_real(one)]),
        ("failed", []),
    ]
    assert rows[1]["reason"] == (
        f"its config.toml has a table for {_real(two)} already — set trust_level = "
        '"trusted" there if you trust it'
    )
    assert codex_sharing.read_trust(held.config_dir) == {_real(one): True}

    again = runner.invoke(app, ["accounts", "trust", "--sync"])
    assert again.exit_code == 1, again.output
    lines = [line for line in again.stdout.splitlines() if "account 3:" in line]
    assert lines == [f"  · account 3: NOT trusted — {rows[1]['reason']}"], again.stdout


def test_a_codex_spawn_shares_the_trust_and_its_receipt_says_so(
    fake_home: Path,
    tmux: FakeTmux,  # noqa: F811 - a fixture, by name
    codex_on_path: Path,  # noqa: F811 - a fixture, by name
    project: ProjectInfo,  # noqa: F811 - a fixture, by name
) -> None:
    """A Codex coder spawned on slot 3 in a worktree of a repository slot 2 trusts starts
    without Codex's question, and the receipt says why, with a ✓ (``receipt.shared``)."""
    root = _real(project.root)
    given, asked = _slot("two@x.dev"), _slot("three@x.dev")
    codex_sharing.grant_trust(given.config_dir, root)

    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=True, account=str(asked.slot)
    )

    assert receipt.agent.cwd != project.root, "its own worktree, covered by the repository"
    shared = f"accounts: {root} is trusted on account 2 — now on account 3 too"
    assert receipt.shared == [shared] and shared in receipt.notes
    assert codex_sharing.read_trust(asked.config_dir) == {root: True}
    told = tmux.spawned[-1]["env"]
    assert isinstance(told, dict) and told[service.SHARED_ENV] == str(asked.slot)


def test_accounts_run_on_a_codex_slot_shares_the_folders_trust_first(
    checkout: Path,
    fake_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.cli import accounts as accounts_cli

    root = _real(checkout)
    given, asked = _slot("two@x.dev"), _slot("three@x.dev")
    codex_sharing.grant_trust(given.config_dir, root)
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: sys.executable if name == "codex" else real_which(name),
    )
    monkeypatch.setattr(accounts_cli, "_exec", lambda binary, argv, env: None)
    monkeypatch.chdir(checkout)

    result = runner.invoke(app, ["accounts", "run", str(asked.slot)])

    assert result.exit_code == 0, result.output
    assert f"accounts: {root} is trusted on account 2 — now on account 3 too" in result.stderr
    assert codex_sharing.read_trust(asked.config_dir) == {root: True}


# --- a re-login: the auth.json tokens ----------------------------------------------------


def _relogin(account: ClaudeAccount, **tokens: str) -> None:
    path = account.config_dir / "auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["tokens"].update(tokens)
    data["last_refresh"] = "2026-10-19T12:00:00Z"
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_codex_log_in_over_a_stale_auth_json_lands_only_once_its_tokens_change(
    fake_home: Path,
) -> None:
    """*Log in* on a Codex row opens ``codex login`` over the auth.json still there: it lands
    once ``codex login`` wrote new tokens, not at the first poll on the old ones."""
    account = _slot("me@x.dev", settings=None)
    since = service.login_fingerprint(account)
    assert since is not None
    assert service.sign_in_landed(account) is not None, "a fresh sign-in has nothing to wait out"
    assert service.sign_in_landed(account, since=since) is None

    auth = account.config_dir / "auth.json"
    auth.write_bytes(auth.read_bytes())  # written again as it was: not a new login
    assert service.sign_in_landed(account, since=since) is None
    _relogin(account, access_token="access-new", refresh_token="refresh-new")
    landed = service.sign_in_landed(account, since=since)
    assert landed is not None and landed.email == "me@x.dev"
    assert service.login_fingerprint(account) != since
    assert "access-new" not in str(service.login_fingerprint(account))

    (account.config_dir / "auth.json").unlink()
    assert service.login_fingerprint(account) is None


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, not root"
)
def test_an_auth_json_that_cannot_be_read_opens_no_log_in(fake_home: Path) -> None:
    account = _slot("me@x.dev", settings=None)
    auth = account.config_dir / "auth.json"
    auth.chmod(0)
    try:
        with pytest.raises(service.LoginUnreadable, match=r"auth\.json cannot be read"):
            service.login_fingerprint(account)
        assert service.sign_in_landed(account) is None
    finally:
        auth.chmod(0o600)
