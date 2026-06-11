"""Config defaults, round-trips, and the home directory layout."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.config import (
    AppConfig,
    CaptureSettings,
    RedactionSettings,
    load_config,
    save_config,
)
from aisquare.core.paths import aisquare_home, cache_dir, config_path, ensure_home, log_dir
from aisquare.models import RedactionLevel


def test_load_missing_file_returns_defaults() -> None:
    assert load_config() == AppConfig()


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
