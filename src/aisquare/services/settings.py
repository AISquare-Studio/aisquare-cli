"""User-facing configuration commands (will be backed by ``core.config``)."""

from __future__ import annotations

from aisquare.core.stubs import stub
from aisquare.models import RedactionLevel


def list_values() -> None:
    """Print the fully-resolved configuration."""
    stub("config list")


def get_value(key: str) -> None:
    """Print one configuration value."""
    stub("config get")


def set_value(key: str, value: str) -> None:
    """Set one configuration value and save."""
    stub("config set")


def set_redaction(level: RedactionLevel) -> None:
    """Set the capture redaction level."""
    stub("config redaction")
