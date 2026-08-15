"""Which executable runs a role's agent (#52).

Orthogonal to the model ladder: that decides WHAT the agent runs on, this
decides WHICH install runs it. People hold several parallel agent binaries —
`claude`, `claude2`, a wrapper script — and want a role pinned to one without
retyping a flag on every spawn.

The property that matters most here is the refusal: an unresolvable binary
must STOP the launch and name what it tried, never fall back to the default.
Falling back runs the *wrong agent under the right role name*, which is worse
than not launching, and is exactly the class of silent surprise this tool
exists to remove.

This file is the ONE home for the binary axis, config included. There is a
single config map now (``team.profiles``), so `bin`-from-config is just the
bottom rung of the ladder below and belongs beside the rungs above it, not in
a second file. ``test_role_profile.py`` owns the env/args axis.
"""

from __future__ import annotations

import pytest

from aisquare.core import harness
from aisquare.core.config import RoleLaunchProfile, load_config, save_config

ROLE = "coder"


@pytest.fixture(autouse=True)
def _no_ambient_bin_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer running the suite may well have these set."""
    for var in (harness._BIN_ENV_GLOBAL, harness._bin_env_var(ROLE)):
        monkeypatch.delenv(var, raising=False)


def _bind_bin(role: str, command: str) -> None:
    """Pin ``role`` to ``command`` through the real config, in the isolated home.

    The real model rather than a stand-in: a hand-rolled stub of the config
    object is a second copy of the schema, and when the schema moved (two maps
    collapsing into one) the stub kept passing against a shape that no longer
    existed. A round-trip through TOML also pins the field NAME, which is the
    part an operator hand-editing the file depends on.
    """
    config = load_config()
    config.team.profiles[role] = RoleLaunchProfile(bin=command)
    save_config(config)


class TestPrecedence:
    def test_the_default_is_claude(self) -> None:
        got = harness.resolve_binary(ROLE)
        assert got.binary == "claude"
        assert got.source == "default"

    def test_a_flag_wins_over_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(harness._bin_env_var(ROLE), "from-env")
        got = harness.resolve_binary(ROLE, override="from-flag")
        assert (got.binary, got.source) == ("from-flag", "flag")

    def test_a_per_role_env_var_beats_the_global_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(harness._BIN_ENV_GLOBAL, "everywhere")
        monkeypatch.setenv(harness._bin_env_var(ROLE), "just-this-role")
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("just-this-role", "env")

    def test_the_global_env_var_applies_when_no_role_specific_one_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(harness._BIN_ENV_GLOBAL, "everywhere")
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("everywhere", "env:global")

    def test_the_configured_profile_is_used_when_no_env_says_otherwise(self) -> None:
        _bind_bin(ROLE, "claude2")
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("claude2", "config")

    def test_an_env_var_beats_the_configured_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _bind_bin(ROLE, "from-config")
        monkeypatch.setenv(harness._bin_env_var(ROLE), "from-env")
        assert harness.resolve_binary(ROLE).source == "env"

    def test_a_flag_beats_the_configured_profile(self) -> None:
        _bind_bin(ROLE, "from-config")
        assert harness.resolve_binary(ROLE, override="from-flag").source == "flag"

    def test_a_role_the_map_does_not_mention_still_gets_the_default(self) -> None:
        _bind_bin("planner", "claude2")
        got = harness.resolve_binary("runner")
        assert (got.binary, got.source) == ("claude", "default")

    def test_a_profile_without_a_bin_falls_through_to_the_default(self) -> None:
        # A role bound for env alone must not be read as "pinned to something".
        # `bin` is optional in the one map, so an unset one has to keep walking
        # the ladder rather than resolving to an empty command.
        config = load_config()
        config.team.profiles[ROLE] = RoleLaunchProfile(env={"CLAUDE_CONFIG_DIR": "/tmp/x"})
        save_config(config)
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("claude", "default")


class TestItFailsOpenOnConfig:
    def test_an_unreadable_config_costs_the_mapping_never_the_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken config file must not stop a launch — the same fail-open the
        explainability wiring already uses two lines away in `spawn`."""

        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("config is a directory")

        monkeypatch.setattr("aisquare.core.config.load_config", _boom, raising=False)
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("claude", "default")


class TestTheEnvVarName:
    def test_it_is_derived_from_the_role(self) -> None:
        assert harness._bin_env_var("coder") == "AISQUARE_BIN_CODER"

    def test_non_alphanumerics_become_underscores(self) -> None:
        """`code-reviewer` has to reach a var a shell can actually export."""
        assert harness._bin_env_var("code-reviewer") == "AISQUARE_BIN_CODE_REVIEWER"


class TestResolutionDoesNotProbePath:
    def test_it_answers_what_was_asked_for_not_what_exists(self) -> None:
        """Resolution and existence are deliberately separate. If they were
        fused, a missing binary would silently become the default — running the
        WRONG agent under the right role name. The caller checks PATH and
        refuses; that refusal is the feature."""
        got = harness.resolve_binary(ROLE, override="definitely-not-installed-xyz")
        assert got.binary == "definitely-not-installed-xyz"
        assert got.source == "flag"
