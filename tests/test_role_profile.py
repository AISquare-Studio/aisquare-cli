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

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import harness, paths
from aisquare.core.config import (
    ExplainabilitySettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.services import settings as settings_service

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
        monkeypatch.setenv("HOME", "/home/someone")
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
        assert spy["argv"] == ["claude", "--model", "opus", "-p", "go"]

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
        # The no-binding path must stay byte-identical to what it always was.
        result = runner.invoke(app, ["launch", "coder"])

        assert result.exit_code == 0, result.output
        assert spy["argv"] == ["claude"]

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
