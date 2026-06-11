"""Setup, upgrade and removal of aisquare itself."""

from __future__ import annotations

from pathlib import Path

from aisquare.core.stubs import stub


def initialize(
    path: Path | None,
    *,
    api_key: str | None,
    local: bool,
    agents: list[str],
    onboard: bool,
    reinit: bool,
    assume_yes: bool,
) -> None:
    """Set up ``~/.aisquare`` and install hooks into the selected agents."""
    stub("init")


def upgrade() -> None:
    """Upgrade aisquare and refresh its agent hooks."""
    stub("upgrade")


def uninstall() -> None:
    """Remove agent hooks and optionally wipe local data."""
    stub("uninstall")
