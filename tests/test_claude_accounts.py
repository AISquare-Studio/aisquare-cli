"""Claude Code accounts: the directories the CLI owns, what it reads there, and the commands.

docs/plans/claude-accounts.md. The core owns numbered directories under
``~/.aisquare/claude-accounts``; the service reads what Claude Code writes into
them and reaches the usage endpoint; the commands render. Every test here runs
against a redirected home (``fake_home``), so nothing reads the developer's own
``~/.claude.json``, and every network-shaped question is answered by a scripted
``fetch`` — the ONE recorded payload is the live endpoint's answer of
2026-09-09, so the parser is held to the shape that actually came back.

Every positive claim has its negative beside it (CONTRIBUTING, "Writing a guard
that still guards"): the directory that must NOT count as a slot, the token that
must NOT be sent, the slot that must NOT be removable.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import accounts as accounts_cli
from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import tmux as tmux_core
from aisquare.core.spawn import IDENTITY_ENV_VARS
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from aisquare.services import team as team_service

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

#: What https://api.anthropic.com/api/oauth/usage answered on 2026-09-09 (Claude Code
#: 2.1.266), trimmed to the keys the parser reads plus two it must ignore.
LIVE_USAGE = {
    "five_hour": {
        "utilization": 3.0,
        "resets_at": "2026-09-09T18:29:59.618422+00:00",
        "limit_dollars": None,
        "locked_reason": None,
    },
    "seven_day": {
        "utilization": 7.0,
        "resets_at": "2026-09-09T21:59:59.618443+00:00",
    },
    "seven_day_opus": None,
    "limits": [{"kind": "session", "percent": 3, "severity": "normal"}],
}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home of our own: the default slot's files land here, never in the developer's."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(core, "_home", lambda: home)
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    monkeypatch.delenv(core.CONFIG_DIR_VAR, raising=False)
    monkeypatch.delenv(core.TMPDIR_VAR, raising=False)
    monkeypatch.setattr(core, "keychain_platform", lambda: False)
    return home


def _sign_in(
    account: ClaudeAccount,
    email: str,
    *,
    claude_json: Path | None = None,
    credentials: bool = True,
    expires_in: timedelta = timedelta(hours=7),
    tier: str = "default_claude_max_5x",
) -> None:
    """Write what a signed-in Claude Code leaves in a config directory."""
    target = claude_json if claude_json is not None else core.claude_json_path(account)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": email,
                    "organizationName": "AISquare",
                    "accountUuid": "ad99225d-ca3f-48fd-a50a-1d0971e26c56",
                },
                "hasCompletedOnboarding": True,
            }
        ),
        encoding="utf-8",
    )
    if credentials:
        core.credentials_path(account).write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-ant-oat01-secret",
                        "refreshToken": "sk-ant-ort01-secret",
                        "expiresAt": int((NOW + expires_in).timestamp() * 1000),
                        "scopes": ["user:inference"],
                        "subscriptionType": "team",
                        "rateLimitTier": tier,
                    }
                }
            ),
            encoding="utf-8",
        )


_REAL_WHICH = shutil.which


def _installed(monkeypatch: pytest.MonkeyPatch, path: str | None = "/opt/bin/claude") -> None:
    """Make ``claude`` resolve to ``path`` (or to nothing); every other lookup stays real."""
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: path if name == "claude" else _REAL_WHICH(name)
    )


# --------------------------------------------------------------------------- core: slots


def test_the_default_slot_is_the_plain_claude_and_its_config_lives_beside_the_directory(
    fake_home: Path,
) -> None:
    account = core.default_account()
    assert account.slot == 1 and not account.managed and account.tmp_dir is None
    assert account.config_dir == fake_home / ".claude"
    # Claude Code keeps the default install's .claude.json at ~/.claude.json…
    assert core.claude_json_path(account) == fake_home / ".claude.json"
    assert core.identity(account) is None
    _sign_in(account, "me@example.com")
    identity = core.identity(account)
    assert identity is not None and identity.email == "me@example.com"
    assert identity.organization == "AISquare"
    # …and a copy INSIDE the directory is not what it reads.
    assert not (fake_home / ".claude" / ".claude.json").exists()
    assert core.launch_env(account) == {}


def test_the_default_slot_follows_claude_config_dir_when_the_shell_sets_it(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = fake_home / ".claude-c2"
    elsewhere.mkdir()
    monkeypatch.setenv(core.CONFIG_DIR_VAR, str(elsewhere))
    account = core.default_account()
    assert account.config_dir == elsewhere
    # With the variable set, Claude Code moves the file inside the directory.
    assert core.claude_json_path(account) == elsewhere / ".claude.json"
    assert core.launch_env(account) == {}  # still nothing: the shell already chose


def test_create_takes_the_lowest_free_slot_marks_it_and_makes_both_directories(
    fake_home: Path,
) -> None:
    assert core.managed_accounts() == []
    second = core.create_account()
    assert second.slot == 2 and second.managed
    assert second.config_dir == core.accounts_root() / "2"
    assert second.tmp_dir == core.tmp_root() / "2"
    assert (second.config_dir / core.MARKER).is_file()
    marker = json.loads((second.config_dir / core.MARKER).read_text())
    assert marker["slot"] == 2 and marker["created_by"].startswith("aisquare-cli ")
    if os.name == "posix":
        assert stat.S_IMODE(second.config_dir.stat().st_mode) == 0o700
        assert second.tmp_dir is not None
        assert stat.S_IMODE(second.tmp_dir.stat().st_mode) == 0o700
    third = core.create_account()
    assert third.slot == 3
    assert [a.slot for a in core.list_accounts()] == [1, 2, 3]
    assert core.launch_env(third) == {
        core.CONFIG_DIR_VAR: str(third.config_dir),
        core.TMPDIR_VAR: str(third.tmp_dir),
    }


def test_remove_renames_the_directory_beside_itself_and_frees_the_number(
    fake_home: Path,
) -> None:
    second = core.create_account()
    third = core.create_account()
    _sign_in(second, "two@example.com")
    assert second.tmp_dir is not None and second.tmp_dir.is_dir()

    moved = core.remove_account(second)

    assert moved.parent == core.accounts_root()
    assert moved.name.startswith("2.removed-") and moved.name != "2"
    assert (moved / ".claude.json").is_file()  # kept, byte for byte
    assert not second.config_dir.exists()
    assert not second.tmp_dir.exists()  # the scratch directory is cache and goes
    assert [a.slot for a in core.managed_accounts()] == [3]  # the removed one is not a slot
    assert core.find_account(2) is None and core.find_account(3) == third
    assert core.next_free_slot() == 2  # the number is free again
    assert core.create_account().slot == 2
    # Removing twice in one second must not collide on the name.
    again = core.remove_account(core.find_account(2) or second)
    assert again != moved and again.exists()


def test_remove_and_discard_refuse_the_default_slot(fake_home: Path) -> None:
    with pytest.raises(ValueError, match="default"):
        core.remove_account(core.default_account())
    with pytest.raises(ValueError, match="default"):
        core.discard_account(core.default_account())
    assert (fake_home / ".claude").is_dir()  # nothing happened to it


def test_a_numbered_directory_without_our_marker_is_not_a_slot(fake_home: Path) -> None:
    core.create_account()  # slot 2, marked
    stray = core.accounts_root() / "7"
    stray.mkdir()
    (core.accounts_root() / "1").mkdir()  # never a managed slot, marker or not
    (core.accounts_root() / "1" / core.MARKER).write_text("{}")
    (core.accounts_root() / "notes").mkdir()

    assert [a.slot for a in core.managed_accounts()] == [2]
    assert core.managed_slot(stray) == 7  # …but the PATH is still recognisably ours
    assert core.managed_slot(core.accounts_root() / "1") is None
    assert core.managed_slot(fake_home / ".claude-c2") is None
    assert core.managed_slot(core.accounts_root() / "2" / "projects") is None


def test_the_board_labels_a_managed_slot_by_number_and_anything_else_by_name(
    fake_home: Path,
) -> None:
    second = core.create_account()
    assert team_service.account_label(str(second.config_dir)) == "account 2"
    assert team_service.account_label(str(fake_home / ".claude-c2")) == ".claude-c2"
    assert team_service.account_label(None) is None
    assert core.label(second) == "account 2" and core.label(core.default_account()) == "default"


# --------------------------------------------------------------------- core: what Claude wrote


def test_credentials_are_read_redacted_and_judged_for_expiry(fake_home: Path) -> None:
    account = core.create_account()
    assert core.credentials(account) is None
    assert core.token_state(None) == "missing"
    _sign_in(account, "two@example.com", expires_in=timedelta(minutes=5))
    creds = core.credentials(account)
    assert creds is not None
    assert creds.access_token == "sk-ant-oat01-secret"
    assert "sk-ant" not in repr(creds) and "<redacted>" in repr(creds)
    assert not creds.expired(NOW) and creds.expired(NOW + timedelta(minutes=6))
    assert core.token_state(creds, NOW) == "ok"
    assert core.token_state(creds, NOW + timedelta(hours=1)) == "expired"
    assert core.subscription_label(creds) == "max 5x"
    _sign_in(account, "two@example.com", tier="something_else")
    assert core.subscription_label(core.credentials(account)) == "team"


def test_signed_in_needs_both_files_on_linux_and_only_the_identity_on_a_keychain_platform(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = core.create_account()
    assert not core.signed_in(account)
    _sign_in(account, "two@example.com", credentials=False)
    assert core.identity(account) is not None
    assert not core.signed_in(account)  # /logout deletes the credentials; the identity lingers
    monkeypatch.setattr(core, "keychain_platform", lambda: True)
    assert core.signed_in(account)  # macOS: the file never exists, the identity is the answer
    monkeypatch.setattr(core, "keychain_platform", lambda: False)
    _sign_in(account, "two@example.com")
    assert core.signed_in(account)


def test_a_damaged_claude_json_reads_as_not_signed_in_rather_than_raising(
    fake_home: Path,
) -> None:
    account = core.create_account()
    core.claude_json_path(account).write_text("{not json", encoding="utf-8")
    core.credentials_path(account).write_text("[]", encoding="utf-8")
    assert core.identity(account) is None
    assert core.credentials(account) is None
    assert not core.signed_in(account)


# ------------------------------------------------------------------ service: describe, usage


def test_describe_and_overview_read_offline_facts(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, None)
    second = core.create_account()
    _sign_in(second, "two@example.com")
    overview = service.overview()
    assert not overview.claude.installed and overview.claude.binary is None
    assert [s.account.slot for s in overview.accounts] == [1, 2]
    first, described = overview.accounts
    assert not first.signed_in and first.identity is None and first.label == "default"
    assert described.signed_in and described.identity is not None
    assert described.identity.email == "two@example.com"
    assert described.subscription == "max 5x" and described.token_state == "ok"
    assert described.label == "account 2" and described.usage is None  # never fetched here
    assert not described.hooks_installed
    _installed(monkeypatch)
    assert service.install().installed and service.install().binary == "/opt/bin/claude"


def test_resolve_finds_a_slot_by_number_or_by_email(fake_home: Path) -> None:
    second = core.create_account()
    _sign_in(second, "Two@Example.com")
    assert service.resolve("2") == second
    assert service.resolve(2) == second
    assert service.resolve("two@example.com") == second  # case does not matter for an email
    assert service.resolve("1") == core.default_account()
    with pytest.raises(service.NoSuchAccount, match="slot 9"):
        service.resolve("9")
    with pytest.raises(service.NoSuchAccount, match=r"nobody@example\.com"):
        service.resolve("nobody@example.com")


class _Fetch:
    """A scripted network: records what was asked, answers what it was told."""

    def __init__(
        self, status: int = 200, body: object = LIVE_USAGE, error: Exception | None = None
    ):
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, raw


def test_usage_parses_the_live_payload_and_sends_the_token_only_to_the_usage_endpoint(
    fake_home: Path,
) -> None:
    account = core.create_account()
    _sign_in(account, "two@example.com")
    fetch = _Fetch()

    usage = service.usage(account, now=NOW, fetch=fetch)

    assert usage.available and usage.reason is None
    assert usage.session_percent == 3.0 and usage.week_percent == 7.0
    assert usage.session_resets_at == datetime(2026, 9, 9, 18, 29, 59, 618422, tzinfo=UTC)
    assert usage.week_resets_at == datetime(2026, 9, 9, 21, 59, 59, 618443, tzinfo=UTC)
    assert usage.fetched_at == NOW
    [(url, headers, timeout)] = fetch.calls
    assert url == service.USAGE_URL and url.startswith("https://api.anthropic.com/")
    assert headers["Authorization"] == "Bearer sk-ant-oat01-secret"
    assert headers["anthropic-beta"] == service.OAUTH_BETA
    assert timeout == service.USAGE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("fetch", "expected"),
    [
        (_Fetch(status=401, body={"error": "x"}), "token was rejected"),
        (_Fetch(status=503, body=b"down"), "HTTP 503"),
        (_Fetch(body=b"<html>"), "did not answer with JSON"),
        (_Fetch(body=[1, 2]), "unknown shape"),
        (_Fetch(body={"limits": []}), "without the two windows"),
        (_Fetch(error=OSError("no route")), "could not reach the usage endpoint (OSError)"),
    ],
    ids=["401", "503", "html", "list", "no-windows", "offline"],
)
def test_usage_says_why_when_the_endpoint_does_not_answer_properly(
    fake_home: Path, fetch: _Fetch, expected: str
) -> None:
    account = core.create_account()
    _sign_in(account, "two@example.com")
    usage = service.usage(account, now=NOW, fetch=fetch)
    assert not usage.available
    assert usage.reason is not None and expected in usage.reason
    assert usage.session_percent is None and usage.week_percent is None
    assert "sk-ant" not in usage.reason  # no reason ever carries the token


def test_usage_never_sends_an_expired_or_missing_token(fake_home: Path) -> None:
    account = core.create_account()
    fetch = _Fetch()
    missing = service.usage(account, now=NOW, fetch=fetch)
    assert not missing.available and "not signed in" in (missing.reason or "")
    _sign_in(account, "two@example.com", expires_in=timedelta(minutes=-1))
    expired = service.usage(account, now=NOW, fetch=fetch)
    assert not expired.available and "expired" in (expired.reason or "")
    assert fetch.calls == []  # neither case reached the network
    _sign_in(account, "two@example.com")
    assert service.usage(account, now=NOW, fetch=fetch).available
    assert len(fetch.calls) == 1  # the control: a live token IS sent


def test_the_default_fetch_is_urllib_and_is_imported_lazily() -> None:
    import ast

    tree = ast.parse(Path(service.__file__).read_text(encoding="utf-8"))
    module_level = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    assert "urllib.request" not in module_level  # tests/test_iam_single_reader.py's ratchet
    assert service._http_get.__name__ == "_http_get"


# --------------------------------------------------------------------------- service: the flows


def test_begin_sign_in_refuses_without_claude_code_and_creates_a_slot_with_it(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, None)
    with pytest.raises(service.ClaudeNotInstalled, match="install it first"):
        service.begin_sign_in(None)
    assert core.managed_accounts() == []  # nothing was made for a session that cannot start
    _installed(monkeypatch)
    fresh = service.begin_sign_in(None)
    assert fresh.slot == 2 and core.find_account(2) == fresh
    assert service.begin_sign_in(1) == core.default_account()
    assert service.begin_sign_in(2) == fresh
    with pytest.raises(service.NoSuchAccount):
        service.begin_sign_in(4)


def test_a_sign_in_lands_when_both_files_exist_and_completing_it_installs_the_hooks(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    account = service.begin_sign_in(None)
    assert service.sign_in_landed(account) is None
    _sign_in(account, "two@example.com", credentials=False)
    assert service.sign_in_landed(account) is None  # the identity alone is not a login here
    _sign_in(account, "two@example.com")
    landed = service.sign_in_landed(account)
    assert landed is not None and landed.email == "two@example.com"

    status = service.complete_sign_in(account)

    assert status.signed_in and status.hooks_installed
    settings = json.loads((account.config_dir / "settings.json").read_text())
    assert "SessionStart" in settings["hooks"]
    assert not service.abandon_sign_in(account)  # a signed-in slot is never discarded
    assert account.config_dir.is_dir()


def test_an_abandoned_sign_in_discards_the_fresh_slot_and_only_that(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    fresh = service.begin_sign_in(None)
    (fresh.config_dir / "statsig").mkdir()  # Claude Code writes before any login lands
    assert service.abandon_sign_in(fresh)
    assert not fresh.config_dir.exists()
    assert core.managed_accounts() == []
    assert not service.abandon_sign_in(core.default_account())
    assert (fake_home / ".claude").is_dir()


def test_remove_disconnects_the_hooks_and_renames_but_never_the_default(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    account = service.begin_sign_in(None)
    _sign_in(account, "two@example.com")
    service.complete_sign_in(account)
    from aisquare.core import agents as agent_core

    assert account.config_dir in agent_core.connected_dirs("claude-code")

    moved = service.remove(account)

    assert moved.name.startswith("2.removed-")
    assert account.config_dir not in agent_core.connected_dirs("claude-code")
    with pytest.raises(service.AccountsError, match="/logout"):
        service.remove(core.default_account())


def test_the_sign_in_window_runs_our_own_run_command_in_the_fleet_server(
    fake_home: Path,
) -> None:
    account = core.create_account()
    ran: list[list[str]] = []

    def runner(argv: Any, stdin: bytes | None) -> Completed:
        ran.append(list(argv))
        if "has-session" in argv:
            return Completed(1, "", "no such session")
        if "new-session" in argv:
            return Completed(0, f"@3{tmux_core._SEP}%9\n", "")
        return Completed(0, "", "")

    server = TmuxServer("asq-test-accounts", runner=runner)
    window = service.open_sign_in_window(account, server, cwd=fake_home)

    assert window.pane_id == "%9" and window.session == service.SIGN_IN_SESSION
    assert window.name == "account-2"
    new_session = next(argv for argv in ran if "new-session" in argv)
    assert new_session[new_session.index("-s") + 1] == "asq-accounts"
    assert new_session[new_session.index("-c") + 1] == str(fake_home)
    tail = new_session[new_session.index("--") + 1 :]
    assert tail == service.sign_in_command(account)
    assert tail == [sys.executable, "-m", "aisquare", "accounts", "run", "2"]


def test_session_env_strips_the_tracing_identity_and_adds_the_accounts_variables(
    fake_home: Path,
) -> None:
    account = core.create_account()
    base = {
        "PATH": "/bin",
        "ANTHROPIC_BASE_URL": "http://proxy",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Agent-Name: coder",
        "AISQUARE_PIPELINE_ID": "run_1",
        "AISQUARE_TRACE_AGENT_NAME": "coder",
        core.CONFIG_DIR_VAR: str(fake_home / ".claude-c1"),
    }
    env = service.session_env(account, base)
    assert env["PATH"] == "/bin"
    assert not any(var in env for var in IDENTITY_ENV_VARS)
    assert env[core.CONFIG_DIR_VAR] == str(account.config_dir)  # the slot wins over the shell
    assert env[core.TMPDIR_VAR] == str(account.tmp_dir)
    default_env = service.session_env(core.default_account(), base)
    assert default_env[core.CONFIG_DIR_VAR] == str(fake_home / ".claude-c1")  # untouched
    assert "ANTHROPIC_BASE_URL" not in default_env


# --------------------------------------------------------------------------- the doctor line


def test_doctor_says_nothing_with_no_added_accounts_and_names_each_one_otherwise(
    fake_home: Path,
) -> None:
    assert diagnostics._claude_accounts_checks() == []
    second = core.create_account()
    third = core.create_account()
    _sign_in(second, "two@example.com")

    [check] = diagnostics._claude_accounts_checks()

    assert check.name == "claude-accounts" and check.status.value == "warn"
    assert "2 two@example.com" in check.detail and "3 not signed in" in check.detail
    assert check.fix is not None and "aisquare accounts run 3" in check.fix
    assert "run 2" not in check.fix  # only the slot that needs it
    _sign_in(third, "three@example.com")
    [check] = diagnostics._claude_accounts_checks()
    assert check.status.value == "ok" and check.fix is None
    assert not core.accounts_root().joinpath("4").exists()  # the doctor made nothing


# --------------------------------------------------------------------------- the commands


def test_accounts_lists_offline_and_as_json(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    second = core.create_account()
    _sign_in(second, "two@example.com")
    fetch = _Fetch()
    monkeypatch.setattr(service, "_http_get", fetch)

    plain = runner.invoke(app, ["accounts"])
    assert plain.exit_code == 0, plain.output
    assert "two@example.com" in plain.stdout and "max 5x" in plain.stdout
    assert "not signed in" in plain.stdout  # the default slot, in this home
    assert "aisquare accounts add" in plain.stdout
    assert fetch.calls == []  # bare listing never reaches the network

    as_json = runner.invoke(app, ["--json", "accounts", "list"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["claude"]["installed"] is True
    assert [a["account"]["slot"] for a in payload["accounts"]] == [1, 2]
    assert payload["accounts"][1]["identity"]["email"] == "two@example.com"
    assert payload["accounts"][1]["usage"] is None
    assert "sk-ant" not in as_json.stdout

    with_usage = runner.invoke(app, ["--json", "accounts", "list", "--usage"])
    assert with_usage.exit_code == 0, with_usage.output
    rows = json.loads(with_usage.stdout)["accounts"]
    assert rows[1]["usage"]["session_percent"] == 3.0
    assert rows[0]["usage"] is None  # not signed in: nothing to ask about
    assert len(fetch.calls) == 1


def test_accounts_list_says_when_claude_code_is_missing(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, None)
    result = runner.invoke(app, ["accounts", "list"])
    assert result.exit_code == 0, result.output
    assert "Claude Code is not installed" in result.stdout
    assert core.INSTALL_COMMAND in result.stdout


def test_accounts_usage_renders_the_windows_and_the_reasons(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = core.create_account()
    third = core.create_account()
    _sign_in(second, "two@example.com")
    _sign_in(third, "three@example.com", expires_in=timedelta(hours=-1))
    monkeypatch.setattr(service, "_http_get", _Fetch())

    result = runner.invoke(app, ["accounts", "usage"])
    assert result.exit_code == 0, result.output
    assert "two@example.com" in result.stdout and "3%" in result.stdout and "7%" in result.stdout
    assert "expired" in result.stdout

    one = runner.invoke(app, ["--json", "accounts", "usage", "two@example.com"])
    assert one.exit_code == 0, one.output
    [row] = json.loads(one.stdout)
    assert row["account"]["slot"] == 2 and row["usage"]["week_percent"] == 7.0

    missing = runner.invoke(app, ["--json", "accounts", "usage", "9"])
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"] == "unknown_account"


def test_accounts_remove_renames_and_refuses_the_default(
    fake_home: Path, runner: CliRunner
) -> None:
    second = core.create_account()
    _sign_in(second, "two@example.com")

    result = runner.invoke(app, ["--json", "accounts", "remove", "2"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["removed"] == 2 and payload["email"] == "two@example.com"
    assert Path(payload["moved_to"]).name.startswith("2.removed-")
    assert core.find_account(2) is None

    refused = runner.invoke(app, ["--json", "accounts", "remove", "1"])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"] == "not_removable"
    assert (fake_home / ".claude").is_dir()

    gone = runner.invoke(app, ["accounts", "remove", "2"])
    assert gone.exit_code == 1 and "no Claude account in slot 2" in gone.stderr


def test_accounts_add_refuses_where_it_cannot_hand_over_a_terminal(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    started: list[ClaudeAccount] = []
    monkeypatch.setattr(service, "run_session", lambda account, *a, **k: started.append(account))

    no_tty = runner.invoke(app, ["accounts", "add"])
    assert no_tty.exit_code == 1
    assert "interactive terminal" in no_tty.stderr and "asq" in no_tty.stderr

    as_json = runner.invoke(app, ["--json", "accounts", "add"])
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["error"] == "no_json_form"

    assert started == [] and core.managed_accounts() == []  # no slot was made for either refusal


def test_accounts_add_records_a_landed_sign_in_and_discards_one_that_did_not(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)

    def signs_in(account: ClaudeAccount, *args: Any, **kwargs: Any) -> int:
        _sign_in(account, "two@example.com")
        return 0

    monkeypatch.setattr(service, "run_session", signs_in)
    result = runner.invoke(app, ["accounts", "add"])
    assert result.exit_code == 0, result.output
    assert "Added Claude account 2: two@example.com" in result.stderr
    assert "aisquare accounts run 2" in result.stderr
    assert service.describe(core.find_account(2) or core.default_account()).hooks_installed

    monkeypatch.setattr(service, "run_session", lambda account, *a, **k: 130)
    gave_up = runner.invoke(app, ["accounts", "add"])
    assert gave_up.exit_code == 1
    assert "no sign-in landed in slot 3" in gave_up.stderr and "status 130" in gave_up.stderr
    assert [a.slot for a in core.managed_accounts()] == [2]  # slot 3 was discarded


def test_accounts_run_execs_claude_on_the_slot_with_arguments_forwarded(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    second = core.create_account()
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(accounts_cli, "_exec", fake_exec)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy")

    result = runner.invoke(app, ["accounts", "run", "2", "--model", "opus"])
    assert result.exit_code == 0, result.output
    assert captured["binary"] == "/opt/bin/claude"
    assert captured["argv"] == ["/opt/bin/claude", "--model", "opus"]
    assert captured["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)
    assert captured["env"][core.TMPDIR_VAR] == str(second.tmp_dir)
    assert "ANTHROPIC_BASE_URL" not in captured["env"]
    assert "account 2" in result.stderr and "not signed in yet" in result.stderr

    captured.clear()
    assert runner.invoke(app, ["accounts", "run", "1"]).exit_code == 0
    assert core.CONFIG_DIR_VAR not in captured["env"]  # the default: the shell's own claude

    captured.clear()
    refused = runner.invoke(app, ["--json", "accounts", "run", "2"])
    assert refused.exit_code == 1 and json.loads(refused.stdout)["error"] == "no_json_form"
    unknown = runner.invoke(app, ["accounts", "run", "9"])
    assert unknown.exit_code == 1 and "slot 9" in unknown.stderr
    assert captured == {}  # neither refusal reached the exec

    _installed(monkeypatch, None)
    absent = runner.invoke(app, ["accounts", "run", "2"])
    assert absent.exit_code == 1 and core.INSTALL_COMMAND in absent.stderr
    assert captured == {}


def test_launch_account_sets_the_slots_variables_over_the_binding(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    second = core.create_account()
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: f"/usr/local/bin/{cmd}")

    result = runner.invoke(
        app, ["launch", "coder", "--account", "2", "--env", f"{core.CONFIG_DIR_VAR}=/elsewhere"]
    )
    assert result.exit_code == 0, result.output
    assert captured["env"][core.CONFIG_DIR_VAR] == str(second.config_dir)  # the flag wins
    assert captured["env"][core.TMPDIR_VAR] == str(second.tmp_dir)
    assert captured["env"]["AISQUARE_ROLE"] == "coder"
    assert "[account 2]" in result.stderr

    captured.clear()
    unknown = runner.invoke(app, ["--json", "launch", "coder", "--account", "9"])
    assert unknown.exit_code == 1
    assert json.loads(unknown.stdout)["error"] == "unknown_account"
    assert captured == {}

    captured.clear()
    plain = runner.invoke(app, ["launch", "coder"])
    assert plain.exit_code == 0, plain.output
    assert core.CONFIG_DIR_VAR not in captured["env"]  # no flag: byte-identical to before
