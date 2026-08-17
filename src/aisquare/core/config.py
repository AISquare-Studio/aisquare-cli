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

    Two independent lanes share this section, and either may run without the
    other. ``enabled`` governs the PROXY lane: model traffic from a launched
    agent, routed via ``ANTHROPIC_BASE_URL``. ``ship`` governs the CLIENT lane:
    the insights the CLI itself holds — human prompts and board events — which
    no proxy can see because they never touch the model API. They meet at the
    gateway, in one Run per session, because both key on the board session id.

    ``ship`` is the single predicate on the primary path, so it carries the
    whole "is this configured" question: it is only ever written True once a
    gateway URL and a usable key both exist. No key or no config therefore
    means nothing is captured at all — not captured-then-discarded.

    The key itself is NOT here. ``config.toml`` is a readable settings file
    people paste into issues; a workspace credential lives in
    ``~/.aisquare/explainability-key`` at mode 600, or in
    ``EXPLAINABILITY_API_KEY``.
    """

    enabled: bool = False
    proxy_url: str = "http://127.0.0.1:9090"
    agent_name_template: str = "aisquare-{role}"
    ship: bool = False
    gateway_url: str = ""


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

    ONE map on purpose. #52 landed a narrower ``bins`` (role → executable)
    beside it — a strict subset of ``profiles.<role>.bin``, so two homes for
    one concept and a precedence rule every reader had to carry. It was
    deleted rather than deprecated because #52 is unreleased: no config file
    anywhere holds a ``bins`` key, and a hand-written one still loads, because
    unknown keys are ignored (see the module docstring).

    Resolution order is flag > env > profile > default; see
    ``aisquare.core.harness.resolve_binary`` and ``resolve_profile``.
    """

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
