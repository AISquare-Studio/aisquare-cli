"""Plain ``init`` must leave an existing config alone.

``--reinit`` resetting config is pinned by
``tests/test_reinit_discards_bindings.py``. The other half — that init WITHOUT
that flag preserves what is already there — was measured twice and pinned
neither time: @9bbc8ed7 checked that ``init --explainability`` keeps five bound
seats, and this cycle it explained why ``init --yes`` exits 0 on a machine whose
config file cannot be written (it never attempts the write; it prints
"✓ aisquare already initialized").

That asymmetry is worth closing on its own terms: the DESTRUCTIVE path is
guarded and the SAFE path is not, so a change that made init start rewriting
config would break the runbook's own next step — ``aisquare init
--explainability`` at line 489, run on a machine with five seats bound — while
every existing test stayed green.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import AppConfig, RoleLaunchProfile, load_config, save_config
from aisquare.core.paths import config_path


def _bound() -> AppConfig:
    config = AppConfig()
    config.team.profiles["coder1"] = RoleLaunchProfile(env={"CLAUDE_CONFIG_DIR": "$HOME/.claude2"})
    config.explainability.gateway_url = "https://stg.example"
    return config


def test_plain_init_keeps_bound_seats(runner: CliRunner) -> None:
    """The state this machine is actually in: seats bound, cutover pending."""
    save_config(_bound())

    result = runner.invoke(app, ["init", "--yes"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    profiles = load_config().team.profiles
    assert "coder1" in profiles, "init discarded a bound seat without --reinit"
    assert profiles["coder1"].env["CLAUDE_CONFIG_DIR"] == "$HOME/.claude2"


def test_plain_init_keeps_explainability_settings(runner: CliRunner) -> None:
    """The runbook's own next step is `init --explainability` on a configured box."""
    save_config(_bound())

    result = runner.invoke(app, ["init", "--explainability", "--yes"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert load_config().explainability.gateway_url == "https://stg.example"


def test_plain_init_does_not_rewrite_an_existing_config(runner: CliRunner) -> None:
    """It does not merely preserve the VALUES — it does not write at all.

    That distinction is load-bearing rather than pedantic. Because init
    short-circuits, it succeeds on a machine where the config file could not be
    written — measured against a symlink into a read-only directory, where every
    other config-writing command correctly fails. Preserving-by-rewriting would
    turn that into a failure of the first command a new user runs.
    """
    save_config(_bound())
    before = config_path().read_bytes()

    result = runner.invoke(app, ["init", "--yes"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert config_path().read_bytes() == before, (
        "init rewrote a config it did not need to touch — on a machine where "
        "that write cannot succeed, this is the difference between working and "
        "failing on the first command"
    )


def test_a_first_init_still_writes_a_config(tmp_path: Path, runner: CliRunner) -> None:
    """The control, so the three tests above cannot pass by init doing nothing."""
    assert not config_path().exists()

    result = runner.invoke(app, ["init", "--yes"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert config_path().exists(), "init no longer creates a config on a fresh machine"
