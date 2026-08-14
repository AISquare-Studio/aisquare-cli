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
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aisquare.core import harness

ROLE = "coder"


@pytest.fixture(autouse=True)
def _no_ambient_bins(monkeypatch):
    """The developer running the suite may well have these set."""
    for var in (harness._BIN_ENV_GLOBAL, harness._bin_env_var(ROLE)):
        monkeypatch.delenv(var, raising=False)


class TestPrecedence:
    def test_the_default_is_claude(self, monkeypatch):
        monkeypatch.setattr(harness, "load_config", None, raising=False)
        got = harness.resolve_binary(ROLE)
        assert got.binary == "claude"
        assert got.source == "default"

    def test_a_flag_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv(harness._bin_env_var(ROLE), "from-env")
        got = harness.resolve_binary(ROLE, override="from-flag")
        assert (got.binary, got.source) == ("from-flag", "flag")

    def test_a_per_role_env_var_beats_the_global_one(self, monkeypatch):
        monkeypatch.setenv(harness._BIN_ENV_GLOBAL, "everywhere")
        monkeypatch.setenv(harness._bin_env_var(ROLE), "just-this-role")
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("just-this-role", "env")

    def test_the_global_env_var_applies_when_no_role_specific_one_exists(self, monkeypatch):
        monkeypatch.setenv(harness._BIN_ENV_GLOBAL, "everywhere")
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("everywhere", "env:global")

    def test_the_config_map_is_used_when_no_env_says_otherwise(self, monkeypatch):
        # SimpleNamespace stands in for the pydantic config object: the
        # resolver only reads `.team.bins`.
        _Cfg = SimpleNamespace(team=SimpleNamespace(bins={ROLE: "claude2"}))

        monkeypatch.setattr(
            "aisquare.core.config.load_config", lambda *a, **k: _Cfg, raising=False
        )
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("claude2", "config")

    def test_an_env_var_beats_the_config_map(self, monkeypatch):
        _Cfg = SimpleNamespace(team=SimpleNamespace(bins={ROLE: "from-config"}))

        monkeypatch.setattr(
            "aisquare.core.config.load_config", lambda *a, **k: _Cfg, raising=False
        )
        monkeypatch.setenv(harness._bin_env_var(ROLE), "from-env")
        assert harness.resolve_binary(ROLE).source == "env"

    def test_a_role_the_map_does_not_mention_still_gets_the_default(self, monkeypatch):
        _Cfg = SimpleNamespace(team=SimpleNamespace(bins={"planner": "claude2"}))

        monkeypatch.setattr(
            "aisquare.core.config.load_config", lambda *a, **k: _Cfg, raising=False
        )
        got = harness.resolve_binary("runner")
        assert (got.binary, got.source) == ("claude", "default")


class TestItFailsOpenOnConfig:
    def test_an_unreadable_config_costs_the_mapping_never_the_spawn(self, monkeypatch):
        """A broken config file must not stop a launch — the same fail-open the
        explainability wiring already uses two lines away in `spawn`."""

        def _boom(*_a, **_k):
            raise OSError("config is a directory")

        monkeypatch.setattr("aisquare.core.config.load_config", _boom, raising=False)
        got = harness.resolve_binary(ROLE)
        assert (got.binary, got.source) == ("claude", "default")


class TestTheEnvVarName:
    def test_it_is_derived_from_the_role(self):
        assert harness._bin_env_var("coder") == "AISQUARE_BIN_CODER"

    def test_non_alphanumerics_become_underscores(self):
        """`code-reviewer` has to reach a var a shell can actually export."""
        assert harness._bin_env_var("code-reviewer") == "AISQUARE_BIN_CODE_REVIEWER"


class TestResolutionDoesNotProbePath:
    def test_it_answers_what_was_asked_for_not_what_exists(self, monkeypatch):
        """Resolution and existence are deliberately separate. If they were
        fused, a missing binary would silently become the default — running the
        WRONG agent under the right role name. The caller checks PATH and
        refuses; that refusal is the feature."""
        got = harness.resolve_binary(ROLE, override="definitely-not-installed-xyz")
        assert got.binary == "definitely-not-installed-xyz"
        assert got.source == "flag"
