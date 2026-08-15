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


class RoleLaunchProfile(BaseModel):
    """One role's launch spec, carried verbatim and never interpreted.

    ``bin`` is the executable, ``env`` the variables to set, ``args`` extra
    arguments appended to the command. Values in ``env`` get ``~`` and ``$VAR``
    expanded at launch so they read exactly like the shell line they replace::

        [team.profiles.coder1]
        bin = "claude"
        args = ["--model", "opus"]

        [team.profiles.coder1.env]
        CLAUDE_CONFIG_DIR  = "$HOME/.claude2"
        CLAUDE_CODE_TMPDIR = "$HOME/.cache/claude2"

    Nothing here knows what any of those variables MEAN, which is the point.
    An earlier cut understood "accounts" and expanded a bare name into a pair
    of directories — one operator's convention baked into a tool with no
    business knowing it, unusable by anyone laid out differently and liable to
    break for its author the day they reorganised. The operator states the
    spec; we carry it.
    """

    bin: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    args: list[str] = Field(default_factory=list)


class TeamSettings(BaseModel):
    """Per-role launch settings for ``aisquare team``.

    ``profiles`` maps a role to its full launch spec — see
    :class:`RoleLaunchProfile`. This is the general mechanism: it covers
    parallel agent installs, wrapper scripts, proxies, regions, or any other
    knob, without this file learning about any of them.

    ``bins`` is the older, narrower shorthand from #52 for the executable
    alone; it still works. A role's ``profiles.<role>.bin`` wins over its
    ``bins`` entry. Full order is flag > env > profile > bins > default; see
    ``aisquare.core.harness.resolve_binary`` and ``resolve_profile``.
    """

    bins: dict[str, str] = Field(default_factory=dict)
    profiles: dict[str, RoleLaunchProfile] = Field(default_factory=dict)


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

    ``exclude_none`` because **TOML has no null**: ``tomli_w`` raises
    ``TypeError`` on ``None`` rather than writing anything, so an optional
    field left unset would make the whole file unwritable. Omitting the key is
    also the correct round-trip — it reloads as the model's default, which is
    the ``None`` we dropped.
    """
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        tomli_w.dumps(config.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
    )
    return target
