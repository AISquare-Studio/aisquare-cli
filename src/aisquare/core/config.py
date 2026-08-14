"""Typed TOML configuration: schema, defaults, load and save.

Loading and saving are real; everything that *uses* the config is still
stubbed. Unknown keys in the file are ignored so old configs keep loading.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field

from aisquare.core import paths
from aisquare.models import Pool, RedactionLevel


class CaptureSettings(BaseModel):
    """Settings for the background capture pipeline."""

    enabled: bool = True


class RedactionSettings(BaseModel):
    """Settings controlling how captured data is scrubbed."""

    level: RedactionLevel = RedactionLevel.standard


class ExplainabilitySettings(BaseModel):
    """Settings for tracing agent sessions through the explainability proxy.

    ``enabled`` is False until the stg pipeline is verified green for this
    team — flipping it on is the only opt-in, and every other safeguard
    (proxy health probe, mode check, pre-existing env detection) fails open:
    a session always launches, at worst untraced with a warning.
    """

    enabled: bool = False
    proxy_url: str = "http://127.0.0.1:9090"
    agent_name_template: str = "aisquare-{role}"


class TeamSettings(BaseModel):
    """Per-role launch settings for ``aisquare team``.

    ``bins`` maps a role to the executable that runs its agent, so a person
    holding several parallel agent installs — ``claude``, ``claude2``, a
    wrapper script — can put a role on the one they mean without retyping a
    flag every spawn. Resolution order is flag > env > this map > default;
    see ``aisquare.core.harness.resolve_binary``.
    """

    bins: dict[str, str] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Root configuration object persisted at ``~/.aisquare/config.toml``."""

    profile: str = "default"
    api_url: str = "https://api.aisquare.studio"
    default_pool: Pool = "project"
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    explainability: ExplainabilitySettings = Field(default_factory=ExplainabilitySettings)
    team: TeamSettings = Field(default_factory=TeamSettings)


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from ``path`` (default: the standard location).

    A missing file yields the built-in defaults.
    """
    target = path or paths.config_path()
    if not target.exists():
        return AppConfig()
    with target.open("rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return AppConfig.model_validate(data)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Write ``config`` as TOML to ``path`` (default: the standard location).

    Parent directories are created on demand. Returns the written path.
    """
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomli_w.dumps(config.model_dump(mode="json")), encoding="utf-8")
    return target
