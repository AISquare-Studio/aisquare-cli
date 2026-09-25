"""Config defaults, round-trips, and the home directory layout."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import snapshot
from aisquare.core.config import (
    AppConfig,
    CaptureSettings,
    ExperimentSettings,
    RedactionSettings,
    SnapshotSettings,
    load_config,
    save_config,
)
from aisquare.core.paths import aisquare_home, cache_dir, config_path, ensure_home, log_dir
from aisquare.models import RedactionLevel


def test_load_missing_file_returns_defaults() -> None:
    assert load_config() == AppConfig()


def test_an_unknown_key_in_the_file_still_loads(tmp_path: Path) -> None:
    # The safety net under every key this schema has ever dropped, and the
    # reason `team.bins` could simply be deleted instead of deprecated. Extras
    # are ignored by default in pydantic, which makes it an accident waiting to
    # be configured away — a later `extra="forbid"` would turn one stale line in
    # a hand-written config into a CLI that cannot start at all.
    target = tmp_path / "config.toml"
    target.write_text(
        'profile = "work"\n\n[team.bins]\ncoder = "claude2"\n\n[nonsense]\nx = 1\n',
        encoding="utf-8",
    )
    config = load_config(target)
    assert config.profile == "work"
    assert config.team.profiles == {}


def test_a_config_saved_with_a_utf8_bom_loads_and_a_save_keeps_its_unknown_keys(
    tmp_path: Path,
) -> None:
    """Windows PowerShell 5.1's ``Set-Content -Encoding UTF8`` and Notepad's "UTF-8 with
    BOM" put U+FEFF in front, which ``tomllib`` refuses: every command raised, and a
    save could not read the file it merges into, so a section this build does not know
    was dropped (review of the #203 store fixes, round 1). Read past the BOM, the file
    loads, a save keeps the section, and the file is written back without the BOM."""
    target = tmp_path / "config.toml"
    body = 'profile = "work"\n\n[future_feature]\nsomething = 42\n'
    target.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    config = load_config(target)
    assert config.profile == "work"
    save_config(config, target)

    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    written = tomllib.loads(raw.decode("utf-8"))
    assert written["profile"] == "work"
    assert written["future_feature"] == {"something": 42}


def test_a_save_over_a_config_that_is_not_utf8_writes_the_model(tmp_path: Path) -> None:
    """Windows PowerShell 5.1's ``>`` and ``Out-File`` write UTF-16. ``load_config`` raises
    on such a file, which is how ``doctor`` reports it and points at ``init --reinit``; but
    the save that reset makes reads the file first to keep its unknown keys, and failed
    open only on a ``TOMLDecodeError``, so the ``UnicodeDecodeError`` escaped and the
    documented recovery crashed on the file it exists to replace (review of the #203
    store fixes, round 2). A config we cannot decode is one we cannot parse: the save
    writes the model's view, and loading still raises until it does."""
    target = tmp_path / "config.toml"
    target.write_bytes('profile = "work"\n'.encode("utf-16"))
    with pytest.raises(UnicodeDecodeError):
        load_config(target)

    save_config(AppConfig(profile="home"), target)

    assert load_config(target).profile == "home"


def test_round_trip_explicit_path(tmp_path: Path) -> None:
    config = AppConfig(
        profile="work",
        default_pool="user",
        capture=CaptureSettings(enabled=False),
        redaction=RedactionSettings(level=RedactionLevel.strict),
    )
    target = save_config(config, tmp_path / "nested" / "config.toml")
    assert target.is_file()
    assert load_config(target) == config


def test_round_trip_default_location(isolated_home: Path) -> None:
    config = AppConfig(profile="laptop")
    target = save_config(config)
    assert target == isolated_home / "config.toml"
    assert target == config_path()
    assert load_config() == config


def test_ensure_home_creates_layout(isolated_home: Path) -> None:
    home = ensure_home()
    assert home == isolated_home
    assert home == aisquare_home()
    assert cache_dir().is_dir()
    assert log_dir().is_dir()


# --- [snapshot] max_tokens (#82) -----------------------------------------------------------


def test_snapshot_budget_defaults_to_the_packers_constant() -> None:
    """The knob exists, and its default IS the 150 000 the packer used to hardcode.

    Nobody who has not set it sees a change: ``core.snapshot.MAX_TOKENS`` is now
    read off this default rather than the other way round, so the two cannot
    drift.
    """
    assert AppConfig().snapshot == SnapshotSettings(max_tokens=150_000)
    assert AppConfig().snapshot.max_tokens == snapshot.MAX_TOKENS


def test_snapshot_budget_loads_from_the_file_and_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[snapshot]\nmax_tokens = 300000\n", encoding="utf-8")
    loaded = load_config(target)
    assert loaded.snapshot.max_tokens == 300_000
    save_config(loaded, target)
    assert load_config(target).snapshot.max_tokens == 300_000
    assert "[snapshot]" in target.read_text(encoding="utf-8")


def test_config_set_writes_the_snapshot_budget_and_rejects_a_non_number(
    runner: CliRunner,
) -> None:
    """The command the failure message names, typed the way it names it."""
    result = runner.invoke(app, ["config", "set", "snapshot.max_tokens", "300000"])
    assert result.exit_code == 0, result.output
    assert load_config().snapshot.max_tokens == 300_000
    shown = runner.invoke(app, ["config", "get", "snapshot.max_tokens"])
    assert shown.stdout.strip() == "snapshot.max_tokens = 300000"

    rejected = runner.invoke(app, ["config", "set", "snapshot.max_tokens", "lots"])
    assert rejected.exit_code != 0
    assert load_config().snapshot.max_tokens == 300_000, "a rejected value must not land"


def test_config_set_takes_the_snapshot_ignore_list_comma_separated(runner: CliRunner) -> None:
    """A list key is settable from a shell at all — comma-separated, the way repomix's own flag is.

    Whitespace around items is trimmed, ``get`` prints the same shape ``set``
    accepts, and an empty string clears the list.
    """
    assert AppConfig().snapshot.ignore == []
    result = runner.invoke(
        app, ["config", "set", "snapshot.ignore", "**/fixtures/**, docs/generated/**"]
    )
    assert result.exit_code == 0, result.output
    assert load_config().snapshot.ignore == ["**/fixtures/**", "docs/generated/**"]
    shown = runner.invoke(app, ["config", "get", "snapshot.ignore"])
    assert shown.stdout.strip() == "snapshot.ignore = **/fixtures/**,docs/generated/**"

    cleared = runner.invoke(app, ["config", "set", "snapshot.ignore", ""])
    assert cleared.exit_code == 0, cleared.output
    assert load_config().snapshot.ignore == []


def test_experiment_is_off_by_default() -> None:
    """The state every existing user is in. ``prompt_submit`` runs
    synchronously in front of a developer who has just hit enter, so off has to
    be the default rather than something a release turns on for everyone."""
    config = AppConfig()
    assert config.experiment.enabled is False
    assert config.experiment.url == ""


def test_the_experiment_has_no_delivery_flags_of_its_own() -> None:
    """Which hooks call the server and whether the recall tool is exposed are
    decided by the server's delivery descriptor. A client-side flag would be a
    second place the experiment's shape lives, and the two would disagree."""
    for flag in ("push", "pull", "arm", "architecture"):
        assert flag not in ExperimentSettings.model_fields


def test_experiment_settings_round_trip(tmp_path: Path) -> None:
    config = AppConfig(
        experiment=ExperimentSettings(enabled=True, url="http://ci.internal", run="run_kernel0001")
    )
    target = save_config(config, tmp_path / "config.toml")
    assert load_config(target) == config
    assert load_config(target).experiment.run == "run_kernel0001"


def test_a_config_from_the_v1_branch_still_loads(tmp_path: Path) -> None:
    """``push``/``pull`` were fields once; a file that still carries them loads,
    because unknown keys are ignored — and they change nothing."""
    target = tmp_path / "config.toml"
    target.write_text("[experiment]\nenabled = true\npush = false\npull = true\n", encoding="utf-8")
    loaded = load_config(target)
    assert loaded.experiment.enabled is True
    assert loaded.experiment.run == ""


def test_experiment_has_nowhere_to_put_a_key() -> None:
    """config.toml is a file people diff, paste into issues and copy between
    machines; the bearer token is read from the environment only."""
    assert "key" not in ExperimentSettings.model_fields


def test_a_config_without_an_experiment_section_still_loads(tmp_path: Path) -> None:
    """Every config written before this release."""
    target = tmp_path / "config.toml"
    target.write_text('profile = "default"\n', encoding="utf-8")
    assert load_config(target).experiment.enabled is False
