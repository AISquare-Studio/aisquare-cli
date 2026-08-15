"""User-facing configuration commands, backed by ``core.config``."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aisquare.core import paths
from aisquare.core.config import AppConfig, RoleLaunchProfile, load_config, save_config
from aisquare.models import RedactionLevel


def list_values() -> AppConfig:
    """Return the fully-resolved configuration."""
    return load_config()


def get_value(key: str) -> str:
    """Return one configuration value as a string. Raises ``KeyError`` if unknown."""
    return _format(_navigate(load_config().model_dump(mode="json"), key))


def set_value(key: str, value: str) -> str:
    """Set one configuration value, save, and return the stored value.

    Raises ``KeyError`` for an unknown key and ``ValueError`` for an invalid value.
    """
    data = load_config().model_dump(mode="json")
    _assign(data, key, value)
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"invalid value for {key!r}: {value!r}") from exc
    save_config(config)
    return get_value(key)


def set_redaction(level: RedactionLevel) -> str:
    """Set the capture redaction level and return it."""
    config = load_config()
    config.redaction.level = level
    save_config(config)
    return level.value


# ─── Role launch bindings (`aisquare team bind`) ─────────────────────────────
#
# Config mutation lives here rather than in `cli/team.py` so the CLI layer stays
# presentation-only: parse flags, call one function, render the result. The
# merge rules below are domain decisions, and a decision buried in a command
# body can only be tested through a CliRunner.


def role_bindings() -> dict[str, RoleLaunchProfile]:
    """Every role's launch binding, keyed by role."""
    return load_config().team.profiles


def bind_role(
    role: str,
    *,
    agent_bin: str | None = None,
    env: dict[str, str] | None = None,
    unset: Sequence[str] = (),
    args: Sequence[str] = (),
) -> RoleLaunchProfile:
    """Merge a launch binding into ``role`` and persist it.

    Env merges **per key** and args **append**, so a second call adds to the
    binding rather than replacing it — otherwise setting a second variable
    would silently drop the first, and the operator would not find out until a
    launch came up on the wrong install. ``unset`` is applied after the merge,
    which makes "replace this one key" a single call.
    """
    config = load_config()
    profile = config.team.profiles.setdefault(role, RoleLaunchProfile())
    if agent_bin is not None:
        profile.bin = agent_bin
    profile.env.update(env or {})
    for key in unset:
        profile.env.pop(key, None)
    profile.args.extend(args)
    save_config(config)
    return profile


def clear_role_binding(role: str) -> None:
    """Remove ``role``'s binding, including its legacy ``team.bins`` entry.

    Both homes are cleared because a `bins` entry surviving a `--clear` would
    keep steering the role's binary while the operator believed the binding was
    gone — the kind of leftover that reads as a bug in something else entirely.
    """
    config = load_config()
    config.team.profiles.pop(role, None)
    config.team.bins.pop(role, None)
    save_config(config)


def config_path() -> Path:
    """Where bindings are persisted — shown so the operator can hand-edit."""
    return paths.config_path()


def _navigate(data: dict[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    if isinstance(node, dict):
        raise KeyError(key)  # not a leaf value
    return node


def _assign(data: dict[str, Any], key: str, value: str) -> None:
    parts = key.split(".")
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        raise KeyError(key)
    node[parts[-1]] = value


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
