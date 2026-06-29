"""User-facing configuration commands, backed by ``core.config``."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from aisquare.core.config import AppConfig, load_config, save_config
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
