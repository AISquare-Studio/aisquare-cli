"""Which ACCOUNT runs a role's agent — the third launch axis.

The ladder decides WHAT model an agent runs on, ``resolve_binary`` decides
WHICH executable runs it, and this decides WHOSE install it runs under: which
credentials, history, settings and MCP servers.

It exists because ``--bin`` cannot serve the setup people actually have.
Parallel accounts are reached through shell aliases::

    alias claude2='CLAUDE_CONFIG_DIR=~/.claude2 CLAUDE_CODE_TMPDIR=~/.cache/claude2 command claude'

An alias is not an executable, so ``shutil.which("claude2")`` is None and
``--bin claude2`` can only ever fail. The alias is two env vars around one
binary, so that is what we set — and the property that matters most is that we
set BOTH. Config dir alone gives a session the right credentials and a scratch
directory silently shared with every other account, which looks correctly
isolated right up until two parallel sessions collide in temp.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import harness
from aisquare.core.config import load_config, save_config

ROLE = "coder"


@pytest.fixture(autouse=True)
def _no_ambient_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer running the suite may well have these set."""
    for var in (harness._ACCOUNT_ENV_GLOBAL, harness._account_env_var(ROLE)):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway ``$HOME``.

    ``isolated_home`` in conftest redirects AISQUARE_HOME, not HOME, and this
    axis resolves ``~/.claude2`` and ``~/.cache/claude2`` — so without this a
    test would read (and ``account_env`` would CREATE) directories in the real
    home of whoever runs the suite.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def _account(home: Path, name: str) -> Path:
    """Create ``~/.<name>`` so it passes the existence check."""
    config_dir = home / f".{name}"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _bind(role: str, account: str) -> None:
    config = load_config()
    config.team.accounts[role] = account
    save_config(config)


class TestPrecedence:
    def test_the_default_is_the_agents_own_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(harness, "load_config", None, raising=False)
        got = harness.resolve_account(ROLE)
        assert (got.account, got.source) == ("claude", "default")

    def test_a_flag_wins_over_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(harness._account_env_var(ROLE), "from-env")
        got = harness.resolve_account(ROLE, override="from-flag")
        assert (got.account, got.source) == ("from-flag", "flag")

    def test_a_per_role_env_var_beats_the_global_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(harness._ACCOUNT_ENV_GLOBAL, "everywhere")
        monkeypatch.setenv(harness._account_env_var(ROLE), "just-this-role")
        got = harness.resolve_account(ROLE)
        assert (got.account, got.source) == ("just-this-role", "env")

    def test_the_global_env_var_applies_when_no_role_specific_one_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(harness._ACCOUNT_ENV_GLOBAL, "everywhere")
        got = harness.resolve_account(ROLE)
        assert (got.account, got.source) == ("everywhere", "env:global")

    def test_the_config_map_is_used_when_no_env_says_otherwise(self) -> None:
        _bind(ROLE, "claude2")
        got = harness.resolve_account(ROLE)
        assert (got.account, got.source) == ("claude2", "config")

    def test_an_env_var_beats_the_config_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind(ROLE, "claude2")
        monkeypatch.setenv(harness._account_env_var(ROLE), "claude3")
        got = harness.resolve_account(ROLE)
        assert (got.account, got.source) == ("claude3", "env")

    def test_a_role_the_map_does_not_mention_still_gets_the_default(self) -> None:
        _bind("planner", "claude2")
        got = harness.resolve_account("runner")
        assert (got.account, got.source) == ("claude", "default")


class TestItFailsOpenOnConfig:
    def test_an_unreadable_config_costs_the_mapping_never_the_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same bar as every other observer in this codebase: a broken config
        # may cost a convenience, never the launch itself.
        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("config is a directory")

        monkeypatch.setattr("aisquare.core.config.load_config", explode)
        got = harness.resolve_account(ROLE)
        assert (got.account, got.source) == ("claude", "default")


class TestResolutionDoesNotProbeTheFilesystem:
    def test_it_answers_what_was_asked_for_not_what_exists(self) -> None:
        # Fusing resolution with existence would turn a typo into a silent
        # fall-back to the DEFAULT account — the right role name on the wrong
        # credentials. Callers check existence and report separately.
        got = harness.resolve_account(ROLE, override="definitely-not-installed")
        assert (got.account, got.source) == ("definitely-not-installed", "flag")


class TestAccountPaths:
    def test_a_bare_name_follows_the_alias_convention(self, fake_home: Path) -> None:
        paths = harness.account_paths("claude2")
        assert paths.config_dir == fake_home / ".claude2"
        assert paths.tmp_dir == fake_home / ".cache" / "claude2"

    def test_an_explicit_path_is_used_as_given(self, fake_home: Path) -> None:
        paths = harness.account_paths(str(fake_home / ".claude-work"))
        assert paths.config_dir == fake_home / ".claude-work"

    def test_an_explicit_path_still_gets_its_own_scratch_dir(self, fake_home: Path) -> None:
        # Deriving from the directory's own name (dot stripped) rather than
        # falling back to the shared default is what keeps an explicitly-pathed
        # account as isolated as a named one.
        paths = harness.account_paths(str(fake_home / ".claude-work"))
        assert paths.tmp_dir == fake_home / ".cache" / "claude-work"

    def test_a_tilde_path_is_expanded(self, fake_home: Path) -> None:
        paths = harness.account_paths("~/.claude9")
        assert paths.config_dir == fake_home / ".claude9"


class TestAccountEnv:
    def test_it_sets_BOTH_vars(self, fake_home: Path) -> None:
        # THE regression this axis exists for. CLAUDE_CONFIG_DIR alone gives a
        # session the right credentials and the default scratch directory,
        # silently shared with every other account.
        env = harness.account_env("claude2")
        assert env["CLAUDE_CONFIG_DIR"] == str(fake_home / ".claude2")
        assert env["CLAUDE_CODE_TMPDIR"] == str(fake_home / ".cache" / "claude2")

    def test_it_creates_the_scratch_dir_but_never_the_config_dir(self, fake_home: Path) -> None:
        # The agent must be able to write scratch; an absent CONFIG dir means
        # the account was never set up, and inventing it would start a fresh
        # unauthenticated profile that reads as a login failure hours later.
        harness.account_env("claude7")
        assert (fake_home / ".cache" / "claude7").is_dir()
        assert not (fake_home / ".claude7").exists()


class TestLaunchWiring:
    def test_launch_sets_both_vars_from_a_named_account(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        _account(fake_home, "claude2")
        result = runner.invoke(app, ["launch", "coder", "--account", "claude2"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(fake_home / ".claude2")
        assert spy["env"]["CLAUDE_CODE_TMPDIR"] == str(fake_home / ".cache" / "claude2")

    def test_launch_uses_the_bound_account_with_no_flag(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        # The whole payoff of binding: the operator stops retyping the flag.
        _account(fake_home, "claude3")
        _bind("coder", "claude3")
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(fake_home / ".claude3")

    def test_a_flag_overrides_the_bound_account(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        _account(fake_home, "claude2")
        _account(fake_home, "claude3")
        _bind("coder", "claude3")
        result = runner.invoke(app, ["launch", "coder", "--account", "claude2"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(fake_home / ".claude2")

    def test_no_account_anywhere_leaves_the_ambient_env_alone(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        fake_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The default launch must stay byte-identical to what it was before
        # this axis existed. Both vars are cleared first because the suite may
        # itself be running inside an account-scoped agent session, whose
        # values `launch` correctly INHERITS via os.environ — asserting on the
        # inherited value would test the developer's shell, not this code.
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert "CLAUDE_CONFIG_DIR" not in spy["env"]
        assert "CLAUDE_CODE_TMPDIR" not in spy["env"]

    def test_a_missing_account_dir_stops_the_launch(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        result = runner.invoke(app, ["launch", "coder", "--account", "claude-typo"])

        assert result.exit_code == 1
        assert "no such account config directory" in result.output
        assert not spy


class TestNumberedSeats:
    def test_a_numbered_seat_of_a_first_class_role_is_accepted(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        # A crew runs several agents in one role and needs them apart on the
        # board; the work cycle is still the role's.
        result = runner.invoke(app, ["launch", "coder1"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["AISQUARE_ROLE"] == "coder1"

    def test_a_role_declared_in_config_is_accepted(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        _account(fake_home, "claude2")
        _bind("reviewer", "claude2")
        result = runner.invoke(app, ["launch", "reviewer"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["AISQUARE_ROLE"] == "reviewer"

    def test_a_typo_is_still_refused(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], fake_home: Path
    ) -> None:
        # The whitelist earns its keep here: `codr` silently producing an
        # unattached session was the original footgun, and staying permissive
        # for real shapes must not cost that.
        result = runner.invoke(app, ["launch", "codr"])

        assert result.exit_code == 1
        assert "unknown role" in result.output
        assert not spy


class TestBind:
    def test_it_persists_the_mapping(
        self, runner: CliRunner, work_dir: Path, fake_home: Path
    ) -> None:
        _account(fake_home, "claude2")
        result = runner.invoke(app, ["team", "bind", "coder1", "--account", "claude2"])

        assert result.exit_code == 0, result.output
        assert load_config().team.accounts["coder1"] == "claude2"

    def test_it_verifies_the_account_exists_at_BIND_time(
        self, runner: CliRunner, work_dir: Path, fake_home: Path
    ) -> None:
        # Checked where the operator is looking, rather than in another
        # terminal hours later where a typo reads as a login failure.
        result = runner.invoke(app, ["team", "bind", "coder1", "--account", "claude-typo"])

        assert result.exit_code == 1
        assert "no such account config directory" in result.output
        assert "coder1" not in load_config().team.accounts

    def test_it_can_bind_a_binary_too(
        self, runner: CliRunner, work_dir: Path, fake_home: Path
    ) -> None:
        result = runner.invoke(app, ["team", "bind", "runner", "--bin", "claude-wrapper"])

        assert result.exit_code == 0, result.output
        assert load_config().team.bins["runner"] == "claude-wrapper"

    def test_clear_removes_both_axes(
        self, runner: CliRunner, work_dir: Path, fake_home: Path
    ) -> None:
        _account(fake_home, "claude2")
        runner.invoke(app, ["team", "bind", "coder1", "--account", "claude2", "--bin", "wrapper"])
        result = runner.invoke(app, ["team", "bind", "coder1", "--clear"])

        assert result.exit_code == 0, result.output
        assert "coder1" not in load_config().team.accounts
        assert "coder1" not in load_config().team.bins

    def test_binding_nothing_is_refused_rather_than_silently_doing_nothing(
        self, runner: CliRunner, work_dir: Path, fake_home: Path
    ) -> None:
        result = runner.invoke(app, ["team", "bind", "coder1"])

        assert result.exit_code == 1
        assert "nothing to bind" in result.output
