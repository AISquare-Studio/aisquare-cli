"""A role's LAUNCH PROFILE — the env and args it runs with.

The third launch axis, and deliberately the dumbest. The ladder decides what
model a role runs on and ``resolve_binary`` decides which executable runs it;
this carries whatever else the operator wants, verbatim.

**The property under test is ignorance.** An earlier cut of this understood
"accounts" and expanded a bare name like ``claude2`` into ``~/.claude2`` plus
``~/.cache/claude2`` — one operator's directory convention baked into a tool
with no business knowing it: unusable by anyone laid out differently, and
liable to break for its author the day they reorganised. Nothing here may know
what any variable MEANS. The tests below assert that: arbitrary keys survive
untouched, and no path is ever inferred, validated or invented.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import harness, paths
from aisquare.core.config import (
    FLEET_DENY_TOOLS,
    ExplainabilitySettings,
    FleetRoleSettings,
    FleetSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.services import settings as settings_service
from tests.role_policy import policy_args

ROLE = "coder"


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


def _tracing_on(monkeypatch: pytest.MonkeyPatch, proxy_url: str = "http://127.0.0.1:9") -> None:
    """Enable tracing and fake ONLY the network probe.

    The real ``wire_session`` runs — identity planning, header building, the
    reserved-var guard — because that logic is what these tests are about. Only
    the socket is replaced.

    This used to swap out ``wire_session`` itself with a wrapper that injected
    a fake prober, because ``prober`` was a default argument and a module-level
    patch could not reach it. It resolves at call time now, so the probe is
    replaced directly and the function under test is no longer a stand-in for
    itself.
    """
    from aisquare.services import explainability as explainability_service
    from aisquare.services.explainability import ProxyProbe

    config = load_config()
    config.explainability = ExplainabilitySettings(enabled=True, proxy_url=proxy_url)
    save_config(config)

    monkeypatch.setattr(
        explainability_service, "probe_proxy", lambda _url: ProxyProbe(True, "test")
    )


def _bind(role: str, **kwargs: Any) -> None:
    config = load_config()
    config.team.profiles[role] = RoleLaunchProfile(**kwargs)
    save_config(config)


class TestExpansion:
    def test_dollar_vars_expand(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `$HOME/.claude2` must become a real path — that is what lets one
        # binding follow the operator across machines with different homes.
        monkeypatch.setenv("HOME", "/home/someone")
        assert harness.expand_value("$HOME/.claude2") == "/home/someone/.claude2"

    def test_tilde_expands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both, because `expanduser` reads a different variable per platform:
        # HOME on POSIX, USERPROFILE on Windows (where HOME is ignored
        # outright). Setting only HOME asserts the POSIX half of a call the
        # product makes on both.
        monkeypatch.setenv("HOME", "/home/someone")
        monkeypatch.setenv("USERPROFILE", "/home/someone")
        assert harness.expand_value("~/.cache/claude2") == "/home/someone/.cache/claude2"

    def test_an_undefined_var_is_left_verbatim_not_blanked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A silently EMPTY CLAUDE_CONFIG_DIR would start a fresh unauthenticated
        # profile, which surfaces as a login failure hours later rather than as
        # the typo it is. Leaving the text intact fails visibly instead.
        monkeypatch.delenv("NOPE_NOT_SET", raising=False)
        assert harness.expand_value("$NOPE_NOT_SET/x") == "$NOPE_NOT_SET/x"

    def test_a_plain_value_is_untouched(self) -> None:
        assert harness.expand_value("us-east-2") == "us-east-2"


class TestParseEnvPairs:
    def test_it_splits_on_the_first_equals_only(self) -> None:
        # Values legitimately contain '=' (tokens, query strings, base64).
        got = harness.parse_env_pairs(["A=1", "B=x=y=z"])
        assert got == {"A": "1", "B": "x=y=z"}

    def test_an_empty_value_is_allowed(self) -> None:
        assert harness.parse_env_pairs(["A="]) == {"A": ""}

    @pytest.mark.parametrize("bad", ["no-equals", "=novalue"])
    def test_a_malformed_pair_is_an_error_not_a_silent_drop(self, bad: str) -> None:
        # Dropping it would leave the operator convinced they had set something.
        with pytest.raises(ValueError):
            harness.parse_env_pairs([bad])


class TestResolveProfile:
    def test_an_unbound_role_carries_nothing(self) -> None:
        got = harness.resolve_profile("nobody")
        assert got.env == {} and got.args == []
        assert got.is_empty

    def test_config_env_is_carried_and_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/someone")
        _bind(ROLE, env={"CLAUDE_CONFIG_DIR": "$HOME/.claude2"})
        got = harness.resolve_profile(ROLE)
        assert got.env["CLAUDE_CONFIG_DIR"] == "/home/someone/.claude2"
        assert got.env_sources["CLAUDE_CONFIG_DIR"] == "config"

    def test_it_carries_variables_it_has_never_heard_of(self) -> None:
        # The whole design: a proxy, a region, a wrapper's own knob — all work
        # without this module learning anything about them.
        _bind(ROLE, env={"HTTPS_PROXY": "http://127.0.0.1:8080", "AWS_REGION": "us-east-2"})
        got = harness.resolve_profile(ROLE)
        assert got.env == {"HTTPS_PROXY": "http://127.0.0.1:8080", "AWS_REGION": "us-east-2"}

    def test_a_flag_overrides_one_key_without_discarding_the_rest(self) -> None:
        # Per-key merge is what makes a one-off tweak cheap; replacing the map
        # would silently drop the sibling variable and break the launch.
        _bind(ROLE, env={"A": "from-config", "B": "keep-me"})
        got = harness.resolve_profile(ROLE, env_overrides={"A": "from-flag"})
        assert got.env == {"A": "from-flag", "B": "keep-me"}
        assert got.env_sources["A"] == "flag"
        assert got.env_sources["B"] == "config"

    def test_extra_args_append_to_the_configured_ones(self) -> None:
        # Configured args are the role's standing shape; the caller's are
        # additions to it, not a replacement.
        _bind(ROLE, args=["--model", "opus"])
        got = harness.resolve_profile(ROLE, extra_args=["-p", "go"])
        assert got.args == ["--model", "opus", "-p", "go"]

    def test_a_broken_config_costs_the_profile_never_the_launch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("config is a directory")

        monkeypatch.setattr("aisquare.core.config.load_config", explode)
        assert harness.resolve_profile(ROLE).is_empty


class TestNoPathIsEverInferred:
    def test_a_bare_name_is_not_turned_into_a_directory(self) -> None:
        # The regression that motivated this rewrite. `claude2` is a value, not
        # a convention to be expanded into ~/.claude2 + ~/.cache/claude2.
        _bind(ROLE, env={"CLAUDE_CONFIG_DIR": "claude2"})
        assert harness.resolve_profile(ROLE).env["CLAUDE_CONFIG_DIR"] == "claude2"

    def test_a_second_variable_is_never_invented(self) -> None:
        # Setting a config dir must NOT conjure a matching tmpdir: guessing the
        # operator's layout is the coupling this design removes.
        _bind(ROLE, env={"CLAUDE_CONFIG_DIR": "/somewhere/else"})
        assert list(harness.resolve_profile(ROLE).env) == ["CLAUDE_CONFIG_DIR"]

    def test_a_nonexistent_path_still_resolves(self) -> None:
        # Resolution answers "what was asked for". Validating would require
        # knowing which keys name directories, which this layer must not know.
        _bind(ROLE, env={"CLAUDE_CONFIG_DIR": "/no/such/place"})
        assert harness.resolve_profile(ROLE).env["CLAUDE_CONFIG_DIR"] == "/no/such/place"


class TestLaunchWiring:
    def test_a_bound_profile_reaches_the_agent_env(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        _bind("coder", env={"CLAUDE_CONFIG_DIR": "/tmp/acct2", "ANYTHING": "else"})
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["CLAUDE_CONFIG_DIR"] == "/tmp/acct2"
        assert spy["env"]["ANYTHING"] == "else"

    def test_env_flag_overrides_the_binding_for_one_launch(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        _bind("coder", env={"CLAUDE_CONFIG_DIR": "/tmp/acct2"})
        result = runner.invoke(app, ["launch", "coder", "--env", "CLAUDE_CONFIG_DIR=/tmp/other"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["CLAUDE_CONFIG_DIR"] == "/tmp/other"

    def test_bound_args_precede_forwarded_ones(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        _bind("coder", args=["--model", "opus"])
        result = runner.invoke(app, ["launch", "coder", "-p", "go"])

        assert result.exit_code == 0, result.output
        assert spy["argv"] == ["claude", "--model", "opus", *policy_args("coder"), "-p", "go"]

    def test_the_lines_own_model_and_effort_replace_the_bindings(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        """#16, seq 3424 (c): a binding's ``--model`` and a fleet window's own (the Spawn
        dialog's Model field, a spawn's ``--model``) both reached Claude Code. The line's
        own wins, as a spawn's args win over the role's settings; the binding's other
        flags stay."""
        _bind("coder", args=["--model", "sonnet", "--effort", "low", "--verbose"])
        result = runner.invoke(app, ["launch", "coder", "--model", "opus"])

        assert result.exit_code == 0, result.output
        argv = spy["argv"]
        assert argv.count("--model") == 1 and argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--effort") + 1] == "low" and "--verbose" in argv

    def test_a_fleet_window_starts_its_agent_without_an_inherited_claude_effort(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#16, seq 3513 (iii): a fleet window inherits the tmux server's environment, a
        ``CLAUDE_EFFORT`` included when a Claude Code session started the server. The agent
        runs its own ``--effort`` or the account's default, never that."""
        from aisquare.cli import launch as launch_module

        monkeypatch.setattr(launch_module, "_await_fleet_row", lambda **kwargs: None)
        monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_inawindow")
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert "CLAUDE_EFFORT" not in spy["env"]

    def test_an_unbound_role_leaves_the_ambient_env_alone(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The default launch must stay byte-identical to what it was before this
        # axis existed. Cleared first because the suite may itself run inside an
        # agent session whose values `launch` correctly INHERITS.
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert "CLAUDE_CONFIG_DIR" not in spy["env"]
        assert "CLAUDE_CODE_TMPDIR" not in spy["env"]

    def test_a_malformed_env_flag_is_refused(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        result = runner.invoke(app, ["launch", "coder", "--env", "OOPS"])

        assert result.exit_code == 1
        assert "KEY=VALUE" in result.output
        assert not spy


class TestNumberedSeats:
    def test_a_numbered_seat_of_a_first_class_role_is_accepted(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        # A crew runs several agents in one role and needs them apart on the
        # board; the work cycle is still the role's.
        result = runner.invoke(app, ["launch", "coder1"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["AISQUARE_ROLE"] == "coder1"

    def test_a_bound_role_is_accepted(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        _bind("reviewer", env={"X": "1"})
        result = runner.invoke(app, ["launch", "reviewer"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["AISQUARE_ROLE"] == "reviewer"

    def test_a_typo_is_still_refused(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        # `codr` silently producing an unattached session was the original
        # footgun; staying permissive for real shapes must not cost that.
        result = runner.invoke(app, ["launch", "codr"])

        assert result.exit_code == 1
        assert "unknown role" in result.output
        assert not spy


class TestBind:
    def test_it_persists_env(self, runner: CliRunner, work_dir: Path) -> None:
        result = runner.invoke(
            app, ["team", "bind", "coder1", "--env", "CLAUDE_CONFIG_DIR=$HOME/.claude2"]
        )

        assert result.exit_code == 0, result.output
        # Stored VERBATIM, not expanded — that is what keeps the binding
        # portable across machines with different homes.
        assert load_config().team.profiles["coder1"].env == {"CLAUDE_CONFIG_DIR": "$HOME/.claude2"}

    def test_repeated_env_flags_all_land(self, runner: CliRunner, work_dir: Path) -> None:
        result = runner.invoke(app, ["team", "bind", "coder1", "--env", "A=1", "--env", "B=2"])

        assert result.exit_code == 0, result.output
        assert load_config().team.profiles["coder1"].env == {"A": "1", "B": "2"}

    def test_a_later_bind_merges_rather_than_replacing(
        self, runner: CliRunner, work_dir: Path
    ) -> None:
        # Adding a second variable must not silently drop the first.
        runner.invoke(app, ["team", "bind", "coder1", "--env", "A=1"])
        result = runner.invoke(app, ["team", "bind", "coder1", "--env", "B=2"])

        assert result.exit_code == 0, result.output
        assert load_config().team.profiles["coder1"].env == {"A": "1", "B": "2"}

    def test_unset_removes_one_key(self, runner: CliRunner, work_dir: Path) -> None:
        runner.invoke(app, ["team", "bind", "coder1", "--env", "A=1", "--env", "B=2"])
        result = runner.invoke(app, ["team", "bind", "coder1", "--unset", "A"])

        assert result.exit_code == 0, result.output
        assert load_config().team.profiles["coder1"].env == {"B": "2"}

    def test_it_binds_a_binary_and_args(self, runner: CliRunner, work_dir: Path) -> None:
        result = runner.invoke(
            app, ["team", "bind", "runner", "--bin", "wrapper", "--arg", "--model", "--arg", "opus"]
        )

        assert result.exit_code == 0, result.output
        profile = load_config().team.profiles["runner"]
        assert profile.bin == "wrapper"
        assert profile.args == ["--model", "opus"]

    def test_clear_removes_the_binding(self, runner: CliRunner, work_dir: Path) -> None:
        runner.invoke(app, ["team", "bind", "coder1", "--env", "A=1"])
        result = runner.invoke(app, ["team", "bind", "coder1", "--clear"])

        assert result.exit_code == 0, result.output
        assert "coder1" not in load_config().team.profiles

    def test_binding_nothing_is_refused(self, runner: CliRunner, work_dir: Path) -> None:
        result = runner.invoke(app, ["team", "bind", "coder1"])

        assert result.exit_code == 1
        assert "nothing to bind" in result.output

    def test_a_malformed_env_pair_is_refused(self, runner: CliRunner, work_dir: Path) -> None:
        result = runner.invoke(app, ["team", "bind", "coder1", "--env", "OOPS"])

        assert result.exit_code == 1
        assert "KEY=VALUE" in result.output

    def test_two_roles_may_share_the_same_values(self, runner: CliRunner, work_dir: Path) -> None:
        # Seat mapping is the operator's call. Nothing here may object to two
        # seats pointing at the same install.
        runner.invoke(app, ["team", "bind", "coder4", "--env", "CLAUDE_CONFIG_DIR=/tmp/a5"])
        result = runner.invoke(
            app, ["team", "bind", "runner", "--env", "CLAUDE_CONFIG_DIR=/tmp/a5"]
        )

        assert result.exit_code == 0, result.output
        profiles = load_config().team.profiles
        assert profiles["coder4"].env == profiles["runner"].env

    def test_listing_shows_the_bindings(self, runner: CliRunner, work_dir: Path) -> None:
        runner.invoke(app, ["team", "bind", "coder1", "--env", "CLAUDE_CONFIG_DIR=$HOME/.claude2"])
        result = runner.invoke(app, ["team", "bind"])

        assert result.exit_code == 0, result.output
        assert "coder1" in result.output
        # Unexpanded, because this view is for editing.
        assert "$HOME/.claude2" in result.output


class TestBindingService:
    """The merge rules, tested WITHOUT a CliRunner.

    This is the payoff of keeping the CLI presentation-only: these are domain
    decisions, and a decision buried in a command body can only be reached
    through an invoked process, which is slower and fails for more reasons than
    the one under test.
    """

    def test_bind_merges_env_per_key(self) -> None:
        settings_service.bind_role(ROLE, env={"A": "1"})
        profile = settings_service.bind_role(ROLE, env={"B": "2"})
        assert profile.env == {"A": "1", "B": "2"}

    def test_bind_overwrites_a_key_it_is_given_again(self) -> None:
        settings_service.bind_role(ROLE, env={"A": "1"})
        assert settings_service.bind_role(ROLE, env={"A": "2"}).env == {"A": "2"}

    def test_unset_is_applied_after_the_merge(self) -> None:
        # So "replace this one key" is a single call rather than two.
        settings_service.bind_role(ROLE, env={"A": "1", "B": "2"})
        profile = settings_service.bind_role(ROLE, env={"C": "3"}, unset=["A"])
        assert profile.env == {"B": "2", "C": "3"}

    def test_args_append(self) -> None:
        settings_service.bind_role(ROLE, args=["--model"])
        assert settings_service.bind_role(ROLE, args=["opus"]).args == ["--model", "opus"]

    def test_clear_removes_the_whole_binding(self) -> None:
        # One map, so a --clear leaves nothing behind that could keep steering
        # the role. Bound on all three fields to pin that: bin used to live in
        # a second map that survived the clear.
        settings_service.bind_role(ROLE, agent_bin="wrapper", env={"A": "1"}, args=["--model"])

        settings_service.clear_role_binding(ROLE)

        assert ROLE not in load_config().team.profiles


class TestUnreadableConfigIsNeverSilent:
    """No silent fail-soft: an unreadable config means the role launches
    UNBOUND — possibly on a different install than the operator believes — so
    it must be said out loud, not merely survived."""

    @staticmethod
    def _break_config(monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("config is a directory")

        monkeypatch.setattr("aisquare.core.config.load_config", explode)

    def test_the_profile_carries_the_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._break_config(monkeypatch)
        profile = harness.resolve_profile(ROLE)
        assert profile.is_empty
        assert profile.notice is not None
        # The exception CLASS, so the reader can tell a missing file from a
        # parse error without opening anything.
        assert "OSError" in profile.notice

    def test_launch_says_so_on_stderr(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._break_config(monkeypatch)
        result = runner.invoke(app, ["launch", "coder"])

        # Fail-OPEN: the launch still happens. Fail-SILENT: it must not.
        assert result.exit_code == 0, result.output
        assert spy
        assert "launching unbound" in result.output

    def test_spawn_says_so_on_stderr(
        self, runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._break_config(monkeypatch)
        monkeypatch.setenv("AISQUARE_TEAM", "1")
        result = runner.invoke(app, ["team", "spawn", "coder", "--no-probe"])

        assert "launching unbound" in result.output


class TestAStaleBinsKeyIsInert:
    """`team.bins` (PR #52) was deleted rather than deprecated — no released
    version ever had it, so no config can hold one. A hand-written or
    branch-era file still LOADS (unknown keys are ignored, pinned in
    tests/test_config.py); these pin that it also has no EFFECT, which is the
    half a load-test cannot see."""

    @staticmethod
    def _write_stale(text: str) -> None:
        target = paths.config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_it_no_longer_steers_the_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(harness._BIN_ENV_GLOBAL, raising=False)
        monkeypatch.delenv(harness._bin_env_var(ROLE), raising=False)
        self._write_stale('[team.bins]\ncoder = "claude2"\n')
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("claude", "default")

    def test_it_no_longer_declares_a_role(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        # The subtle half: `_declared_roles` used to union profiles|bins, so a
        # role named ONLY in bins was launchable. It must not be now — otherwise
        # a deleted map keeps quietly widening the role whitelist.
        self._write_stale('[team.bins]\ndeploybot = "claude2"\n')
        result = runner.invoke(app, ["launch", "deploybot"])
        assert result.exit_code == 1, result.output
        assert "unknown role" in result.output.lower()
        assert not spy


class TestLaunchHonoursTheBoundBinary:
    """`launch` used to read --command alone and ignore the binding entirely.

    Worse than not supporting it: the docstring promised the profile supplied
    the binary, so a role bound to a wrapper silently started the DEFAULT agent
    under the right role name and exited 0 — success reported for the wrong
    program. `team spawn` honoured the binding correctly the whole time, so the
    two entry points disagreed about what a binding meant.
    """

    def test_a_bound_bin_is_what_gets_executed(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        _bind("coder", bin="claude-wrapper")
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert spy["argv"][0] == "claude-wrapper"
        assert spy["binary"] == "/usr/local/bin/claude-wrapper"

    def test_command_flag_still_overrides_the_binding(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        _bind("coder", bin="from-config")
        result = runner.invoke(app, ["launch", "coder", "--command", "from-flag"])

        assert result.exit_code == 0, result.output
        assert spy["argv"][0] == "from-flag"

    def test_an_unbound_role_still_launches_the_default(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        # The no-binding path carries the role's own flags and nothing else.
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert spy["argv"] == ["claude", *policy_args("coder")]

    def test_a_missing_binary_names_who_chose_it(
        self, runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bare "not on your PATH" sends the reader hunting through flag, env
        # and config to work out which of them picked the thing that is absent.
        _bind("coder", bin="nowhere-to-be-found")
        monkeypatch.setattr(shutil, "which", lambda _cmd: None)
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 1
        assert "nowhere-to-be-found" in result.output
        assert "chosen by: config" in result.output

    def test_spawn_and_launch_agree_on_the_binary(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any]
    ) -> None:
        # The invariant the defect broke: two entry points, one answer.
        _bind("coder", bin="claude-wrapper")
        assert harness.resolve_binary("coder").binary == "claude-wrapper"
        runner.invoke(app, ["launch", "coder"])
        assert spy["argv"][0] == harness.resolve_binary("coder").binary


class TestTracingReadsTheBoundBinaryNotTheFlag:
    """Where the bound-binary fold and the correlation-spine fold meet.

    Session pinning asks "does this agent accept ``--session-id``?" — and the
    honest answer depends on what will ACTUALLY run, which is the role's bound
    binary, not the ``--command`` flag. The flag is ``None`` on every launch
    that does not type it, so reading it here would hand ``None`` to
    ``os.path.basename`` and crash the launch outright: tracing costing a
    launch, the one thing the wiring exists to never do. Neither branch could
    have caught this alone — the spine was cut before the binding landed.
    """

    def test_a_wrapper_bound_role_launches_traced_and_unpinned(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _tracing_on(monkeypatch)
        _bind("coder", bin="my-agent-wrapper")

        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert spy["argv"][0] == "my-agent-wrapper"
        assert "--session-id" not in spy["argv"], (
            "a wrapper is not known to accept the flag — pinning it would kill the launch"
        )
        assert "ANTHROPIC_BASE_URL" in spy["env"], "the trace itself must survive"

    def test_an_unbound_role_still_gets_its_id_pinned(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The positive half: the default binary DOES accept the flag, so the
        # common launch keeps its board-row-to-Run join.
        _tracing_on(monkeypatch)

        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert "--session-id" in spy["argv"]
        pinned = spy["argv"][spy["argv"].index("--session-id") + 1]
        assert f"X-Pipeline-Id: {pinned}" in spy["env"]["ANTHROPIC_CUSTOM_HEADERS"], (
            "the id the agent is started on and the id the Run is filed under must be one id"
        )


# --- a variable bound to a file: a secret no printed line or config file holds --------------

TOKEN = "github_pat_reviewer_cannot_push_0123456789"


def _token_file(tmp_path: Path, text: str = TOKEN + "\n") -> Path:
    path = tmp_path / "secrets" / "reviewer-gh-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestEnvFiles:
    """`env_files`: a variable's value read from a file at every launch (follow-up of #257).

    For the reviewer: a `GH_TOKEN` that cannot write the repository's contents, so a prompt
    injected through the PR it reads cannot push or merge through `gh` (docs/fleet.md). The
    property under test is the secret's: it reaches the agent's environment and nothing
    that prints, and a file that cannot be read refuses the launch rather than start the
    role on this shell's own credentials. Still nothing here knows what `GH_TOKEN` means."""

    def test_the_files_text_is_the_value_and_only_its_path_may_be_shown(
        self, tmp_path: Path
    ) -> None:
        token = _token_file(tmp_path)
        _bind("reviewer", env_files={"GH_TOKEN": str(token)})

        got = harness.resolve_profile("reviewer")

        assert got.file_env == {"GH_TOKEN": TOKEN}, "its text, the newline dropped"
        assert got.env_files == {"GH_TOKEN": str(token)} and got.env_sources["GH_TOKEN"] == "file"
        assert "GH_TOKEN" not in got.env and not got.is_empty
        assert TOKEN not in got.model_dump_json() and TOKEN not in repr(got), "never printed"

    def test_the_file_wins_over_the_bindings_env_and_a_flag_wins_over_the_file(
        self, tmp_path: Path
    ) -> None:
        token = _token_file(tmp_path)
        _bind("reviewer", env={"GH_TOKEN": "a-full-token"}, env_files={"GH_TOKEN": str(token)})

        bound = harness.resolve_profile("reviewer")
        flagged = harness.resolve_profile("reviewer", env_overrides={"GH_TOKEN": "for-once"})

        assert bound.file_env == {"GH_TOKEN": TOKEN} and "GH_TOKEN" not in bound.env
        assert flagged.env["GH_TOKEN"] == "for-once" and flagged.file_env == {}
        assert flagged.refusal is None and flagged.env_files == {}

    @pytest.mark.parametrize("shape", ["missing", "empty"])
    def test_a_file_that_cannot_be_read_refuses_the_launch(
        self, tmp_path: Path, shape: str
    ) -> None:
        path = tmp_path / "gone" if shape == "missing" else _token_file(tmp_path, "\n")
        _bind("reviewer", env_files={"GH_TOKEN": str(path)})

        got = harness.resolve_profile("reviewer")

        assert got.refusal is not None and got.file_env == {}
        assert got.refusal.startswith(f"reviewer reads GH_TOKEN from {path}, which "), got.refusal
        assert ("cannot be read" if shape == "missing" else "is empty") in got.refusal
        assert "aisquare team bind reviewer --unset GH_TOKEN" in got.refusal

    def test_launch_hands_the_value_to_the_agent_and_prints_none_of_it(
        self, runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
    ) -> None:
        _bind("reviewer", env_files={"GH_TOKEN": str(_token_file(tmp_path))})

        result = runner.invoke(app, ["launch", "reviewer"])

        assert result.exit_code == 0, result.output
        assert spy["env"]["GH_TOKEN"] == TOKEN
        assert "GH_TOKEN" in result.output, "the key is named"
        assert TOKEN not in result.output

    def test_launch_refuses_rather_than_start_on_this_shells_credentials(
        self,
        runner: CliRunner,
        work_dir: Path,
        spy: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", "this-shells-token-that-can-push")
        _bind("reviewer", env_files={"GH_TOKEN": str(tmp_path / "gone")})

        result = runner.invoke(app, ["--json", "launch", "reviewer"])

        payload = json.loads(result.stdout)
        assert result.exit_code == 1, result.output
        assert payload["error"] == "env_file_unreadable", payload
        assert payload["detail"].startswith("reviewer reads GH_TOKEN from "), payload
        assert not spy, "nothing launched"

    def test_spawns_printed_command_reads_the_file_and_holds_no_value(
        self,
        runner: CliRunner,
        work_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = _token_file(tmp_path / "a dir")
        _bind("reviewer", env_files={"GH_TOKEN": str(token)})
        monkeypatch.setenv("AISQUARE_TEAM", "1")

        human = runner.invoke(app, ["team", "spawn", "reviewer", "--no-probe"])
        machine = runner.invoke(app, ["--json", "team", "spawn", "reviewer", "--no-probe"])

        assert human.exit_code == 0 and machine.exit_code == 0, human.output + machine.output
        read = f"aisquare team read-env-file GH_TOKEN {shlex.quote(str(token))}"
        assert f'GH_TOKEN="$({read})"' in human.output
        assert "GH_TOKEN [file]" in human.output
        assert TOKEN not in human.output and TOKEN not in machine.output
        assert json.loads(machine.stdout)["env_files"] == {"GH_TOKEN": str(token)}

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("sh") is None,
        reason="the paste runs in a POSIX shell",
    )
    def test_the_printed_command_reads_the_file_as_launch_does(
        self,
        runner: CliRunner,
        work_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review of #259, round 2: the printed `GH_TOKEN="$(cat path)"` skipped what
        `read_env_file` does. Pasted after the file had gone, or gone empty, it started the
        role with `GH_TOKEN=""`, the other login the refusal exists to prevent, said by a
        `cat:` line alone; saved with CRLF, it gave `token\\r` where `--exec` and `fleet
        spawn` give `token`. The paste reads it as `launch` does, in a subshell: the value
        without its CR, and a line that starts nothing for a file empty or gone, whatever
        `GH_TOKEN` the shell it is pasted into has."""
        token = _token_file(tmp_path, TOKEN + "\r\n")
        agent = tmp_path / "bin" / "agent"
        agent.parent.mkdir()
        agent.write_text('#!/bin/sh\nprintf "[%s]" "$GH_TOKEN"\n', encoding="utf-8")
        agent.chmod(0o755)
        # The `aisquare` the role's terminal has on PATH: this checkout's.
        cli = agent.with_name("aisquare")
        cli.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m aisquare "$@"\n')
        cli.chmod(0o755)
        _bind("reviewer", bin=str(agent), env_files={"GH_TOKEN": str(token)})
        monkeypatch.setenv("AISQUARE_TEAM", "1")
        result = runner.invoke(app, ["--json", "team", "spawn", "reviewer", "--no-probe"])
        assert result.exit_code == 0, result.output
        command = json.loads(result.stdout)["command"]
        assert TOKEN not in command, "the line holds the path, never the value"
        assert command.startswith("(GH_TOKEN="), "in a subshell: the paste sets nothing here"
        shell = {
            "PATH": f"{agent.parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GH_TOKEN": "this-shells-token-that-can-push",
        }

        def paste() -> tuple[int, str, str]:
            done = subprocess.run(
                ["sh", "-c", command], env=shell, capture_output=True, check=False, timeout=120
            )  # bytes: text mode would read a CR in the value as a newline
            return done.returncode, done.stdout.decode(), done.stderr.decode()

        assert paste() == (0, f"[{TOKEN}]", ""), "the value as launch reads it, no CR"
        # Review of #259, round 3: a token saved with a byte-order mark (PowerShell 5.1's
        # `Set-Content -Encoding UTF8`, an older Notepad; its `>` writes UTF-16) launched as
        # `﻿<token>`, which every `gh` call refused. Round 4: the paste read it in shell,
        # and still not as launch does: a CR inside the value deleted, UTF-16 undecoded (a
        # non-ASCII token mangled), a value of spaces taken. Each file, both ways, the same.
        files = [
            (f"{TOKEN}\r\n".encode("utf-8-sig"), "UTF-8 with its mark"),
            (f"{TOKEN}\r\n".encode("utf-16"), "UTF-16"),
            (f"\ufeff{TOKEN}\r\n".encode("utf-16-le"), "UTF-16 LE"),
            (f"\ufeff{TOKEN}\r\n".encode("utf-16-be"), "UTF-16 BE"),
            ("tökén-ü\r\n".encode("utf-16"), "a non-ASCII token in UTF-16"),
            (b"ab\rcd\r\n", "a CR inside the value"),
            (b"   \n", "spaces alone"),
            (b"\xef\xbb\xbf\n", "a mark and nothing else"),
            (b"\n", "an emptied file"),
        ]
        for content, what in files:
            token.write_bytes(content)
            launched = harness.resolve_profile("reviewer")
            status, out, err = paste()
            if launched.refusal is None:
                assert (status, out, err) == (0, f"[{launched.file_env['GH_TOKEN']}]", ""), what
            else:
                assert (status, out) == (1, ""), f"{what} starts nothing"
                assert err.startswith(f"GH_TOKEN: {token} is empty"), (what, err)
        assert harness.resolve_profile("reviewer").refusal is not None, "control: refused"
        token.unlink()
        status, out, err = paste()
        assert (status, out) == (1, ""), "a file gone starts nothing"
        assert err.startswith(f"GH_TOKEN: {token} cannot be read ("), err

    def test_team_harness_names_the_file_bound_key_and_reads_its_file_once(
        self,
        runner: CliRunner,
        work_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review of #259, round 1: the text matrix named a key bound with `--env-file`,
        and `--json` did not: `env` no longer holds it, and nothing else said it. Each
        role was also resolved three times, each reading every bound file again."""
        token = _token_file(tmp_path)
        _bind("reviewer", env_files={"GH_TOKEN": str(token)})
        monkeypatch.setenv("AISQUARE_TEAM", "1")
        reads: list[str] = []
        read = harness.read_env_file

        def counted(path: str) -> str:
            reads.append(path)
            return read(path)

        monkeypatch.setattr(harness, "read_env_file", counted)

        human = runner.invoke(app, ["team", "harness"])
        read_by_text, reads[:] = list(reads), []
        machine = runner.invoke(app, ["--json", "team", "harness"])

        assert human.exit_code == 0 and machine.exit_code == 0, human.output + machine.output
        assert read_by_text == reads == [str(token)], "the file read once per listing"
        row = {row["role"]: row for row in json.loads(machine.stdout)["roles"]}["reviewer"]
        assert row["env_files"] == {"GH_TOKEN": str(token)} and "GH_TOKEN" not in row["env"]
        assert "env=GH_TOKEN" in human.output
        assert TOKEN not in human.output and TOKEN not in machine.output
        assert row["refusal"] is None

    def test_team_harness_names_a_binding_whose_file_is_gone_and_that_the_role_will_not_launch(
        self, runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #259, round 3: with the reviewer's GH_TOKEN file deleted,
        `resolve_profile` stopped before it named the key, so `team harness` showed no
        `env=GH_TOKEN`, `--json` `"env_files": {}`, and neither said the role cannot launch:
        the command an operator runs to see what a role launches with was silent about the
        binding that refuses it. The key is named with its path, and the refusal said."""
        gone = tmp_path / "secrets" / "gone"
        token = _token_file(tmp_path)
        _bind("reviewer", env_files={"GH_TOKEN": str(gone), "GITHUB_TOKEN": str(token)})
        monkeypatch.setenv("AISQUARE_TEAM", "1")

        human = runner.invoke(app, ["team", "harness"])
        machine = runner.invoke(app, ["--json", "team", "harness"])

        assert human.exit_code == 0 and machine.exit_code == 0, human.output + machine.output
        row = {row["role"]: row for row in json.loads(machine.stdout)["roles"]}["reviewer"]
        assert row["env_files"] == {"GH_TOKEN": str(gone), "GITHUB_TOKEN": str(token)}
        assert row["refusal"].startswith(f"reviewer reads GH_TOKEN from {gone}, which cannot")
        said = " ".join(human.output.split())
        assert "env=GH_TOKEN,GITHUB_TOKEN" in said
        assert f"⚠ reviewer reads GH_TOKEN from {gone}, which cannot be read" in said
        assert TOKEN not in human.output and TOKEN not in machine.output
        launched = harness.resolve_profile("reviewer")
        assert launched.file_env == {}, "a refused launch carries none of the secrets"


class TestBindEnvFile:
    def test_it_stores_the_path_as_typed_and_reads_it_now(
        self, runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECRETS", str(_token_file(tmp_path).parent))

        result = runner.invoke(
            app, ["team", "bind", "reviewer", "--env-file", "GH_TOKEN=$SECRETS/reviewer-gh-token"]
        )

        assert result.exit_code == 0, result.output
        profile = load_config().team.profiles["reviewer"]
        assert profile.env_files == {"GH_TOKEN": "$SECRETS/reviewer-gh-token"}
        assert TOKEN not in paths.config_path().read_text(encoding="utf-8")
        assert TOKEN not in result.output

    def test_a_relative_path_is_stored_as_the_file_it_named_where_it_was_bound(
        self, runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #259, round 3: `team bind` read a relative PATH from the bind shell's
        working directory and stored it relative, so every launch resolved it against its
        own: `cd ~/.config/aisquare && team bind reviewer --env-file GH_TOKEN=reviewer-token`
        succeeded, and every window's `launch`, in its worktree, refused the role, while the
        fleet's pre-check read asq's directory and could pass. It stores the file it read."""
        token = _token_file(tmp_path)
        monkeypatch.chdir(token.parent)
        here = Path.cwd() / token.name

        result = runner.invoke(
            app, ["team", "bind", "reviewer", "--env-file", f"GH_TOKEN={token.name}"]
        )

        assert result.exit_code == 0, result.output
        assert load_config().team.profiles["reviewer"].env_files == {"GH_TOKEN": str(here)}
        monkeypatch.chdir(work_dir)  # where the role launches: its worktree
        launched = harness.resolve_profile("reviewer")
        assert launched.refusal is None and launched.file_env == {"GH_TOKEN": TOKEN}

    def test_a_relative_path_in_the_config_refuses_every_launch_alike(
        self, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One written into config.toml by hand: each launch would look for it in its own
        working directory, so whether the role starts would depend on where it is launched
        from. Every launch refuses it, wherever, naming the way to bind it by its full path."""
        token = _token_file(tmp_path)
        _bind("reviewer", env_files={"GH_TOKEN": token.name})

        for where in (work_dir, token.parent):  # even where the file would be found
            monkeypatch.chdir(where)
            got = harness.resolve_profile("reviewer")
            assert got.refusal is not None and got.file_env == {}
            assert f"reads GH_TOKEN from {token.name}, a relative path" in got.refusal
            assert "aisquare team bind reviewer --env-file GH_TOKEN=<path>" in got.refusal

    @pytest.mark.parametrize("flag", ["--env", "--env-file"])
    @pytest.mark.parametrize("key", ["GH-TOKEN", "GH TOKEN", "X;touch pwned", "1ST"])
    def test_a_key_no_shell_can_set_is_refused_at_bind(
        self, runner: CliRunner, work_dir: Path, tmp_path: Path, flag: str, key: str
    ) -> None:
        """Review of #259, round 3: `parse_env_pairs` took any key with no `=`, and `team
        spawn` prints each bound key as a shell word: `GH-TOKEN="$(tr …)"` is a command
        name, so the paste failed with 'command not found', and a key holding `;` ran what
        followed it. `team bind` refuses such a key, for `--env` and `--env-file` alike."""
        value = str(_token_file(tmp_path)) if flag == "--env-file" else "x"

        result = runner.invoke(app, ["--json", "team", "bind", "reviewer", flag, f"{key}={value}"])

        assert result.exit_code != 0, result.output
        said = json.loads(result.stdout)
        assert said["error"] == "bad_env_pair" and repr(key) in said["detail"], said
        assert "reviewer" not in load_config().team.profiles, "nothing bound"
        assert not (work_dir / "pwned").exists()

    def test_spawn_refuses_to_print_a_line_for_a_key_written_into_the_config_by_hand(
        self, runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config.toml synced from another machine, or edited by hand, can still hold such a
        key: `team spawn` says which, and how to unbind it, rather than print a line that
        breaks when pasted, or runs part of the key."""
        _bind("reviewer", env_files={"GH-TOKEN": str(_token_file(tmp_path))})
        monkeypatch.setenv("AISQUARE_TEAM", "1")

        result = runner.invoke(app, ["--json", "team", "spawn", "reviewer", "--no-probe"])

        assert result.exit_code != 0, result.output
        said = json.loads(result.stdout)
        assert said["error"] == "bad_env_key", said
        assert "aisquare team bind reviewer --unset GH-TOKEN" in said["detail"]
        assert TOKEN not in result.output

    def test_a_file_it_cannot_read_is_refused_at_bind(
        self, runner: CliRunner, work_dir: Path, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            app, ["--json", "team", "bind", "reviewer", "--env-file", f"GH_TOKEN={tmp_path}/no"]
        )

        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "env_file_unreadable"
        assert "reviewer" not in load_config().team.profiles, "nothing bound"

    def test_a_key_lives_in_one_map_and_unset_takes_it_from_either(self, tmp_path: Path) -> None:
        token = str(_token_file(tmp_path))
        settings_service.bind_role(ROLE, env={"GH_TOKEN": "inline", "KEEP": "1"})

        to_file = settings_service.bind_role(ROLE, env_files={"GH_TOKEN": token})
        assert (to_file.env, to_file.env_files) == ({"KEEP": "1"}, {"GH_TOKEN": token})
        back = settings_service.bind_role(ROLE, env={"GH_TOKEN": "inline"})
        assert (back.env, back.env_files) == ({"KEEP": "1", "GH_TOKEN": "inline"}, {})
        settings_service.bind_role(ROLE, env_files={"GH_TOKEN": token})
        unset = settings_service.bind_role(ROLE, unset=["GH_TOKEN"])
        assert (unset.env, unset.env_files) == ({"KEEP": "1"}, {})

    def test_taking_the_account_away_keeps_a_binding_of_files_alone(self, tmp_path: Path) -> None:
        """A binding whose account was its last binding goes with it; one that still reads
        a file is not empty, and was read as empty by `bin or env or args`."""
        settings_service.bind_role(ROLE, env_files={"GH_TOKEN": str(_token_file(tmp_path))})
        settings_service.bind_role(ROLE, account="1")

        settings_service.bind_role(ROLE, clear_account=True)

        assert load_config().team.profiles[ROLE].env_files, "the file binding stays"


def _policy(
    role: str,
    args: Sequence[str] = (),
    *,
    binary: str = "claude",
    effort: str | None = None,
    **role_keys: Any,
) -> harness.ToolPolicy:
    """``harness.tool_policy`` at the built-in config, with ``role_keys`` set on the role."""
    fleet = FleetSettings()
    fleet.roles[role] = fleet.roles.get(role, FleetRoleSettings()).model_copy(update=role_keys)
    return harness.tool_policy(role, binary=binary, args=args, fleet=fleet, effort=effort)


def _denied_by(flags: Sequence[str]) -> list[str]:
    """The names of the ONE ``--disallowedTools`` token in ``flags``."""
    tokens = [flag for flag in flags if flag.startswith("--disallowedTools")]
    assert len(tokens) == 1, flags
    return tokens[0].partition("=")[2].split(" ")


class TestToolPolicy:
    """#280: the role's tools and skill listing, one home, applied wherever it starts."""

    def test_the_reviewer_is_denied_its_editing_tools_and_the_fleets_eight(self) -> None:
        policy = _policy("reviewer")
        assert _denied_by(policy.args) == sorted(
            {"Edit", "Write", "NotebookEdit", *FLEET_DENY_TOOLS}
        )
        assert policy.denied == _denied_by(policy.args)

    def test_a_numbered_seat_has_its_roles_safety_list(self) -> None:
        assert {"Edit", "Write", "NotebookEdit"} <= set(_policy("reviewer2").denied)

    def test_every_deny_already_given_is_merged_into_one_token_and_stripped(self) -> None:
        args = [
            "--disallowed-tools",
            "Bash",
            "Edit",
            "--model",
            "opus",
            "--disallowedTools=Read,Glob",
        ]
        policy = _policy("coder", args)
        assert {"Bash", "Edit", "Read", "Glob"} <= set(_denied_by(policy.args))
        assert policy.strip(args) == ["--model", "opus"]

    def test_nothing_after_a_bare_double_dash_is_read_as_a_flag(self) -> None:
        args = ["--", "--disallowedTools=Edit"]
        policy = _policy("coder", args)
        assert "Edit" not in policy.denied
        assert policy.strip(args) == args

    def test_the_token_is_byte_identical_whatever_order_the_denies_came_in(self) -> None:
        one = _policy("coder", ["--disallowedTools=Bash Read"], deny_tools=["Glob", "Grep"])
        two = _policy("coder", ["--disallowedTools=Read,Bash"], deny_tools=["Grep", "Glob"])
        assert one.args == two.args

    def test_allow_tools_hands_a_safety_listed_tool_back(self) -> None:
        assert "Edit" not in _policy("reviewer", allow_tools=["Edit"]).denied

    def test_allow_tools_never_hands_back_a_deny_on_the_command_line(self) -> None:
        policy = _policy("reviewer", ["--disallowedTools=Edit"], allow_tools=["Edit"])
        assert "Edit" in policy.denied

    def test_a_role_adds_its_own_denies_to_the_fleets(self) -> None:
        assert {"Bash", *FLEET_DENY_TOOLS} <= set(_policy("tester", deny_tools=["Bash"]).denied)

    def test_another_agent_gets_no_flags_and_a_note_says_why(self) -> None:
        args = ["--disallowedTools=Edit"]
        policy = _policy("reviewer", args, binary="/opt/bin/aider")
        assert policy.args == []
        assert policy.strip(args) == args, "another agent's flags are its own"
        (note,) = policy.notes
        assert "withheld" in note and "'aider' is not claude" in note and "Edit" in note

    def test_names_cuts_the_skill_descriptions_and_full_leaves_them(self) -> None:
        assert _policy("coder").args[-2:] == ["--settings", harness.SKILL_NAMES_SETTINGS]
        assert "--settings" not in _policy("manager").args

    def test_names_is_withheld_beside_a_settings_already_given(self) -> None:
        policy = _policy("coder", ["--settings", "{}"])
        assert "--settings" not in policy.args
        assert policy.notes == ["skill_listing withheld: --settings already given"]

    def test_off_denies_the_skill_tool(self) -> None:
        policy = _policy("coder", skill_listing="off")
        assert "Skill" in policy.denied
        assert "--settings" not in policy.args

    def test_ultracode_keeps_workflow_and_says_so(self) -> None:
        """Ultracode is xhigh plus workflow orchestration: the default deny of Workflow
        turned that half off without a word (review of #311)."""
        for args, effort in (
            (["--effort", "ultracode"], None),
            (["--effort=ULTRACODE"], None),
            ([], "ultracode"),
        ):
            policy = _policy("coder", args, effort=effort)
            assert "Workflow" not in policy.denied, (args, effort)
            assert "Workflow kept: effort ultracode orchestrates workflows" in policy.notes

    def test_xhigh_still_denies_workflow(self) -> None:
        assert "Workflow" in _policy("coder", ["--effort", "xhigh"], effort="xhigh").denied

    def test_a_typed_deny_of_workflow_holds_under_ultracode(self) -> None:
        policy = _policy("coder", ["--effort", "ultracode", "--disallowedTools=Workflow"])
        assert "Workflow" in policy.denied
